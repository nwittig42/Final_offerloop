"""
Run routes - prompt-based contact search endpoint
"""
import json
import os
import time
import traceback
import threading
from datetime import datetime
from typing import Dict, Tuple, Optional
from app.services.pdl_client import get_contact_identity, search_contacts_from_prompt
from app.services.prompt_parser import parse_search_prompt_structured
from app.services import coresignal_client
from flask import Blueprint, request, jsonify

from app.extensions import require_firebase_auth, get_db
from app.services.feature_flags import PDL_OUTAGE_ACTIVE
from app.services.metering import attach_request_context, spend_summary, spend_by_user
from app.services.reply_generation import batch_generate_emails, PURPOSES_INCLUDE_RESUME, email_body_mentions_resume, regenerate_with_feedback
from app.services.gmail_client import _load_user_gmail_creds, download_resume_from_url, clear_user_gmail_integration
from app.services.resume_parser import extract_text_from_pdf_bytes
from app.routes.gmail_oauth import build_gmail_oauth_url_for_user
from app.services.auth import check_and_reset_credits, deduct_credits_atomic
from app.config import TIER_CONFIGS, CREDIT_COSTS
from app.utils.exceptions import OfferloopException, InsufficientCreditsError, ExternalAPIError
from app.utils.warmth_scoring import score_contacts_for_email, score_and_sort_contacts, build_briefing_line
from app.utils.email_quality import check_email_quality, has_specificity_signal
from app.services.email_request_builder import (
    resolve_email_template as _resolve_email_template,
    build_email_gen_request,
)
from app.utils.users import get_outreach_email, merge_persona_fields


def _contact_already_exists(contact, existing_emails_set, existing_name_company_set, existing_linkedins_set=None):
    """Check if a contact already exists in the user's saved contacts."""
    email = (contact.get("Email") or contact.get("WorkEmail") or contact.get("PersonalEmail") or contact.get("email") or "").strip().lower()
    if email and email in existing_emails_set:
        return True
    if existing_linkedins_set:
        linkedin = (contact.get("LinkedIn") or contact.get("linkedinUrl") or "").strip()
        if linkedin and linkedin in existing_linkedins_set:
            return True
    fn = (contact.get("FirstName") or contact.get("firstName") or "").strip().lower()
    ln = (contact.get("LastName") or contact.get("lastName") or "").strip().lower()
    co = (contact.get("Company") or contact.get("company") or "").strip().lower()
    if fn and ln and co and f"{fn}_{ln}_{co}" in existing_name_company_set:
        return True
    return False


# =============================================================================
# EXCLUSION LIST (built fresh from Firestore on every search)
# =============================================================================
#
# Returns a dict of lookup sets used for dedup:
#   {
#     "identity_set": set[str],       # get_contact_identity() keys for PDL-side dedup
#     "email_set": set[str],          # lowercased email addresses
#     "linkedin_set": set[str],       # linkedin URLs (raw)
#     "name_company_set": set[str],   # "first_last_company" lowercased
#   }
#
# This used to be cached in-memory with a 1-hour TTL. That cache went stale on
# delete (deleted contacts stayed filtered out of search for up to an hour), so
# it was removed. We now re-stream the user's contacts on each search — one
# projected Firestore read, cheap at search frequency, and always current.


def _build_exclusion_data_from_firestore(db, user_id: str) -> dict:
    """Stream the user's contacts once and build all four dedup lookup sets."""
    identity_set = set()
    email_set = set()
    linkedin_set = set()
    name_company_set = set()
    contacts_ref = db.collection("users").document(user_id).collection("contacts")
    for doc in contacts_ref.select(
        ["firstName", "lastName", "email", "linkedinUrl", "company"]
    ).stream():
        cd = doc.to_dict() or {}
        first = (cd.get("firstName") or "").strip()
        last = (cd.get("lastName") or "").strip()
        company = (cd.get("company") or "").strip()
        email = (cd.get("email") or "").strip().lower()
        linkedin = (cd.get("linkedinUrl") or "").strip()
        standardized = {
            "FirstName": first,
            "LastName": last,
            "Email": email,
            "LinkedIn": linkedin,
            "Company": company,
        }
        identity_set.add(get_contact_identity(standardized))
        if email:
            email_set.add(email)
        if linkedin:
            linkedin_set.add(linkedin)
        fn = first.lower()
        ln = last.lower()
        co = company.lower()
        if fn and ln and co:
            name_company_set.add(f"{fn}_{ln}_{co}")
    return {
        "identity_set": identity_set,
        "email_set": email_set,
        "linkedin_set": linkedin_set,
        "name_company_set": name_company_set,
    }

runs_bp = Blueprint('runs', __name__, url_prefix='/api')


@runs_bp.route("/contacts/invalidate-cache", methods=["POST"])
@require_firebase_auth
def invalidate_contact_dedup_cache():
    """No-op kept for backward compatibility.

    The Find exclusion list is no longer cached — it's rebuilt from Firestore
    on every search — so there is nothing to invalidate. Retained so any
    existing caller (frontend, extension) doesn't 404.
    """
    return jsonify({"ok": True}), 200


# Find's Cancel (OFF-56). The client tags each search with a searchId; Cancel
# writes users/{uid}/searchCancels/{searchId}, and the search checks it before
# drafting and again before it charges credits or saves contacts. Firestore,
# not memory, because gunicorn runs several workers.
_CANCELLED_RESPONSE = {"cancelled": True, "contacts": [], "already_saved_contacts": [], "total_contacts": 0}


def _search_cancelled(db, user_id, search_id):
    if not (db and user_id and search_id):
        return False
    try:
        return db.collection("users").document(user_id).collection("searchCancels").document(search_id).get().exists
    except Exception as e:
        print(f"[ContactSearch] cancel check failed, continuing: {e}")
        return False


@runs_bp.route("/prompt-search/cancel", methods=["POST"])
@require_firebase_auth
def cancel_prompt_search():
    user_id = request.firebase_user["uid"]
    search_id = str((request.get_json(silent=True) or {}).get("searchId") or "").strip()[:64]
    if not search_id:
        return jsonify({"error": "searchId is required"}), 400
    db = get_db()
    if db:
        db.collection("users").document(user_id).collection("searchCancels").document(search_id).set(
            {"cancelledAt": datetime.utcnow().isoformat() + "Z"}
        )
    return jsonify({"ok": True})


# Contact on a Find card whose search came back without an email (OFF-56).
# The search already charged find_contact (which includes the email), so this
# second try is free, and it runs once per contact. Same ladder as the search's
# email pass (Hunter, then one FullEnrich waterfall) but with a longer wait,
# since the search gives FullEnrich only a few seconds.
_RETRY_FE_WAIT_SECONDS = 45


@runs_bp.route("/prompt-search/find-email", methods=["POST"])
@require_firebase_auth
def find_search_contact_email():
    user_id = request.firebase_user["uid"]
    contact_id = str((request.get_json(silent=True) or {}).get("contactId") or "").strip()
    if not contact_id or "/" in contact_id:
        return jsonify({"error": "contactId is required"}), 400
    db = get_db()
    if not db:
        return jsonify({"error": "Database not available"}), 500

    ref = db.collection("users").document(user_id).collection("contacts").document(contact_id)
    snap = ref.get()
    if not snap.exists:
        return jsonify({"error": "Contact not found"}), 404
    c = snap.to_dict() or {}
    if (c.get("email") or "").strip():
        return jsonify({"email": c["email"].strip()}), 200
    if c.get("source") != "find_search":
        return jsonify({"error": "Only people from a search can be looked up here"}), 400
    # One retry per contact. The claim lives where clients can't write
    # (firestore.rules has no rule for emailRetries), so it can't be reset.
    from google.api_core.exceptions import AlreadyExists
    try:
        db.collection("users").document(user_id).collection("emailRetries").document(contact_id).create(
            {"at": datetime.utcnow().isoformat() + "Z"}
        )
    except AlreadyExists:
        return jsonify({"email": None, "retried": True}), 200

    first = (c.get("firstName") or "").strip()
    last = (c.get("lastName") or "").strip()
    company = (c.get("company") or "").strip()
    linkedin = (c.get("linkedinUrl") or "").strip()
    email, source, verified = "", "", False
    try:
        from app.services import email_fast
        hit = email_fast.hunter_email(first, last, company)
        if hit and hit.get("email"):
            email, source = hit["email"], "hunter_finder"
    except Exception:
        print("[FindEmail] Hunter errored; trying FullEnrich")
    if not email:
        try:
            from app.services import fullenrich_client
            eid = fullenrich_client.start_bulk([("0", first, last, company, linkedin)], db=db)
            if eid:
                _ready, hits = fullenrich_client.fetch_bulk(eid, wait_seconds=_RETRY_FE_WAIT_SECONDS, db=db)
                if (hits or {}).get("0", {}).get("email"):
                    email, source, verified = hits["0"]["email"], "fullenrich_backfill", True
        except Exception:
            print("[FindEmail] FullEnrich errored; contact stays email-less")

    print(f"[FindEmail] uid={user_id} contact={contact_id} found={bool(email)} source={source or '-'}")
    if not email:
        return jsonify({"email": None}), 200
    ref.update({"email": email, "emailSource": source, "emailVerified": verified})
    # Same warehouse re-feed as the search, so the next student gets it free.
    try:
        from app.services.firm_cache.writer import cache_pdl_contacts
        cache_pdl_contacts([{
            "FirstName": first, "LastName": last, "Company": company, "LinkedIn": linkedin,
            "Title": c.get("jobTitle") or "", "College": c.get("college") or "",
            "Email": email, "WorkEmail": email, "EmailSource": source, "EmailVerified": verified,
        }], shape="app", async_write=True)
    except Exception:
        pass
    return jsonify({"email": email}), 200


@runs_bp.route("/prompt-search", methods=["POST"])
@require_firebase_auth
def prompt_search():
    """
    Prompt-based contact search for all tiers. Parses natural language prompt,
    runs PDL search, then same email/draft/save pipeline as free-run.
    Request: { "prompt": "...", "batchSize": 5 }
    Response: same shape as free-run plus parsed_query.
    """
    # PDL is the primary provider. PDL_OUTAGE_ACTIVE remains the global
    # kill switch — set to True in feature_flags.py if the operator needs to
    # take all contact search dark (e.g. PDL credits exhausted, vendor outage).
    if PDL_OUTAGE_ACTIVE:
        return jsonify({"error": "service_unavailable", "message": "Contact search temporarily unavailable.", "code": "PDL_OUTAGE"}), 503

    try:
        user_email = request.firebase_user.get("email")
        user_id = request.firebase_user["uid"]
        db = get_db()

        # Set request-scoped context so the @meter_call decorator can attribute
        # every provider HTTP call in this request to the right user / search.
        attach_request_context(user_id=user_id)

        data = request.get_json(silent=True) or {}
        prompt = (data.get("prompt") or "").strip()
        batch_size = data.get("batchSize")
        search_id = str(data.get("searchId") or "").strip()[:64]

        # Validate prompt length
        if not prompt:
            return jsonify({"error": "Prompt is required"}), 400
        if len(prompt) < 3:
            return jsonify({"error": "Prompt must be at least 3 characters"}), 400
        if len(prompt) > 500:
            return jsonify({"error": "Prompt must be at most 500 characters"}), 400

        # Resolve tier and max_contacts
        user_tier = "free"
        credits_available = TIER_CONFIGS["free"]["credits"]
        user_data = None
        exclusion_data = None
        seen_contact_set = set()
        if db and user_id:
            try:
                user_ref = db.collection("users").document(user_id)
                user_doc = user_ref.get()
                if user_doc.exists:
                    user_data = user_doc.to_dict()
                    # Firestore user doc is the source of truth for identity;
                    # the Firebase token's email can be missing (Apple with no
                    # shared email) or a privaterelay.appleid.com address.
                    user_email = user_data.get("email") or user_email
                    credits_available = check_and_reset_credits(user_ref, user_data)
                    user_tier = user_data.get("subscriptionTier", user_data.get("tier", "free"))
                    if user_tier not in TIER_CONFIGS:
                        user_tier = "free"
                    exclusion_data = _build_exclusion_data_from_firestore(db, user_id)
                    seen_contact_set = exclusion_data["identity_set"]
            except Exception as e:
                # Fail closed: if we can't load user data, don't allow search
                print(f"⚠️ Failed to load user profile for {user_id}: {e}")
                return jsonify({"error": "Could not load user profile. Please try again."}), 500

        # Credit check outside try/except — always enforced
        if credits_available < 5:
            return jsonify({
                "error": "Insufficient credits",
                "credits_needed": 5,
                "current_credits": credits_available,
            }), 400

        tier_max = TIER_CONFIGS[user_tier]["max_contacts"]
        try:
            batch_size = int(batch_size) if batch_size is not None else None
        except (TypeError, ValueError):
            batch_size = None
        max_contacts = batch_size if batch_size and 1 <= batch_size <= tier_max else tier_max

        # Outreach mode: "preview" (contacts only, no email, no draft),
        # "draft" (generate email plus create Gmail draft, current behavior),
        # or "send" (generate then send, Elite only, wired in a later chunk).
        # Validated against tier here on the server. We never trust the client
        # mode value: Free is clamped to preview, Pro to draft, Elite to send.
        TIER_ALLOWED_MODES = {
            "free": {"preview"},
            "pro": {"preview", "draft"},
            "elite": {"preview", "draft", "send"},
        }
        allowed_modes = TIER_ALLOWED_MODES.get(user_tier, {"preview"})
        default_mode = "preview" if user_tier == "free" else "draft"
        requested_mode = (data.get("mode") or "").strip().lower()
        if requested_mode not in ("preview", "draft", "send"):
            requested_mode = default_mode
        if requested_mode not in allowed_modes:
            # Requested a mode above this tier. Clamp down to their best allowed.
            requested_mode = "draft" if "draft" in allowed_modes else "preview"
        outreach_mode = requested_mode
        print(f"[Runs] Outreach mode: requested={data.get('mode')!r}, tier={user_tier}, resolved={outreach_mode}")

        # Org alumni directory (OFF-42): a member who picks their org's pill or
        # names it ("TKE alumni at Goldman") searches that directory instead of
        # the provider ladder. Non-members never get here (resolve_org_targets
        # only returns orgs the user is an active member of).
        org_targets: list[str] = []
        org_list: list[dict] = []
        org_prompt = prompt
        org_hint = None
        try:
            from app.services.org_directory import join_hint, list_orgs, resolve_org_targets
            if db:
                raw_filters = data.get("filters") if isinstance(data.get("filters"), dict) else {}
                org_list = list_orgs(db)
                org_targets, org_prompt = resolve_org_targets(prompt, raw_filters.get("organizations"), user_data, org_list)
                # Named an org they haven't joined: the UI offers the join.
                org_hint = join_hint(prompt, user_data, org_list) if not org_targets else None
        except Exception as e:
            print(f"[OrgDirectory] org resolve failed, running a normal search: {e}")
            org_targets = []

        # Parse prompt
        if org_targets:
            # "TKE alumni" alone parses as too vague; in org mode the parse only
            # narrows the directory, so it never blocks the search.
            parsed = parse_search_prompt_structured(org_prompt) if len(org_prompt) >= 3 else {}
            if parsed.get("error"):
                parsed = {}
            parsed = {"companies": [], "title_variations": [], "locations": [], "schools": [], "industries": [],
                      **{k: v for k, v in parsed.items() if v is not None}}
            print(f"[OrgDirectory] org search {org_targets} with remainder {org_prompt!r}")
        else:
            parsed = parse_search_prompt_structured(prompt)
        if parsed.get("error"):
            return jsonify({
                "error": parsed.get("error", "Failed to parse prompt"),
                "parsed_query": {k: parsed.get(k) for k in ("companies", "title_variations", "locations") if k in parsed},
            }), 400
        if parsed.get("confidence") == "low" and not org_targets and org_hint:
            # "TKE alumni" from a non-member: point them at joining instead of
            # calling the search too vague.
            return jsonify({
                "contacts": [],
                "already_saved_contacts": [],
                "total_contacts": 0,
                "tier": user_tier,
                "parsed_query": {"org_hint": org_hint},
                "message": f"{org_hint['shortName']} alumni are in a members-only directory. Join with your chapter's password to search them.",
            }), 200
        if parsed.get("confidence") == "low" and not org_targets:
            return jsonify({
                "error": "Your search was too vague. Please add more specifics (e.g. job title, company, or location).",
                "parsed_query": {k: parsed.get(k) for k in ("companies", "title_variations", "locations") if k in parsed},
            }), 400

        # Provider routing: PDL is primary. The pdl_client cache check happens
        # FIRST inside search_contacts_from_prompt (Firestore, 30-day TTL) so
        # a repeat query returns 0-credit cached contacts. PDL supports the
        # full set of filters (school/alumni, company, title, location), so
        # we no longer need the no-company 503 gate that existed during the
        # Hunter bridge. Coresignal/Hunter remain wired up in code as a
        # reliability fallback only if PDL itself is down.
        #
        # Credit-efficiency: pdl_client.search_contacts_from_prompt now caps
        # the PDL fetch at max_contacts + min(exclude_count, 3), so the
        # worst-case credit burn per search is ~max_contacts + 3.
        # ---- JIT firm-employee cache lookup (Phase 3) ---------------------
        # Check firm_employees BEFORE PDL. Gated by ENABLE_FIRM_CACHE_LOOKUP
        # env flag (independent of the write flag). Returns cache hits in
        # App-shape with Email="" — the existing email-quality machinery
        # downstream flags them so nothing breaks. If cache fills the whole
        # tier limit, PDL is skipped entirely (0 credits). If partial, PDL
        # is called for the remainder and results are deduped.
        cache_hits: list[dict] = []
        cache_hit_metadata: dict | None = None
        # Defaults for when the provider call raises before assigning them
        # (it used to surface as a 500 UnboundLocalError).
        retry_level_used = 0
        already_saved_contacts: list = []
        try:
            from app.services.firm_cache import search_firm_cache
            from app.services.firm_cache.reader import _flag_enabled as _lookup_enabled
            if _lookup_enabled() and not org_targets:
                cache_hits, _, _, cache_hit_metadata = search_firm_cache(
                    parsed, max_contacts, exclude_keys=seen_contact_set
                )
                if cache_hits:
                    print(f"[FirmCache] LOOKUP hit — {len(cache_hits)}/{max_contacts} served from firm_employees")
        except Exception as e:
            print(f"[FirmCache] lookup failed (non-fatal, falling through to PDL): {e}")
            cache_hits = []
            cache_hit_metadata = None

        try:
            if org_targets:
                # Org directory only: no warehouse, no providers. Already-saved
                # members are skipped before the cut so a repeat search shows
                # new people.
                from app.services.org_directory import search_org_directory
                org_hits = search_org_directory(db, org_targets, parsed, org_list, prompt=org_prompt)
                already_saved_contacts = []
                if exclusion_data is not None:
                    fresh = []
                    for c in org_hits:
                        if _contact_already_exists(c, exclusion_data["email_set"], exclusion_data["name_company_set"], exclusion_data["linkedin_set"]):
                            already_saved_contacts.append(c)
                        else:
                            fresh.append(c)
                    org_hits = fresh
                contacts = org_hits[:max_contacts]
                retry_level_used = 0
                adjacency_metadata = {"provider": "org_directory", "org_ids": org_targets}
                if not contacts:
                    names = " / ".join(o["shortName"] for o in org_list if o["id"] in org_targets)
                    adjacency_metadata["message"] = f"No {names} alumni matched that. Try fewer filters."
            elif len(cache_hits) >= max_contacts:
                # Full cache hit — skip PDL entirely (0 credits).
                contacts = cache_hits[:max_contacts]
                retry_level_used = 0
                already_saved_contacts = []
                adjacency_metadata = cache_hit_metadata or {}
                adjacency_metadata.setdefault("provider", "firm_cache")
                print(f"[FirmCache] LOOKUP full-hit — skipped PDL, saved ~${max_contacts * 0.20:.2f} in credits")
            else:
                # Partial or empty cache — call PDL for the remainder. Cache
                # hits are added to the exclusion set so PDL won't re-return them.
                pdl_target = max_contacts - len(cache_hits)
                pdl_exclude = set(seen_contact_set or set())
                for ch in cache_hits:
                    fn = (ch.get("FirstName") or "").strip().lower()
                    ln = (ch.get("LastName") or "").strip().lower()
                    co = (ch.get("Company") or "").strip().lower()
                    if fn and ln and co:
                        pdl_exclude.add((fn, ln, co))
                # Provider ladder unified with the mobile engine
                # (2026-08-27): warehouse hits stay first (free), then
                # Coresignal PRIMARY for the remainder (photo-rich, current,
                # budget-metered), PDL as the reliability fallback, the
                # Hunter bridge last.
                remainder = []
                remainder_meta: dict = {}
                try:
                    if getattr(coresignal_client, "CORESIGNAL_API_KEY", ""):
                        remainder, retry_level_used, already_saved_contacts, remainder_meta = coresignal_client.search_contacts_from_prompt(
                            parsed, pdl_target, exclude_keys=pdl_exclude, user_profile=user_data
                        )
                        remainder_meta = remainder_meta or {}
                        remainder_meta.setdefault("provider", "coresignal")
                        _cs_collected = int(remainder_meta.get("collected_count") or 0)
                        if _cs_collected:
                            try:
                                from google.cloud import firestore as _fs
                                from app.extensions import get_db as _get_db
                                _db = _get_db()
                                if _db is not None:
                                    _db.collection("meta").document("coresignalTestBudget").set(
                                        {"spent_estimate": _fs.Increment(20.0 * _cs_collected),
                                         "collect_count": _fs.Increment(_cs_collected)}, merge=True)
                                from app.services.metering import log_provider_spend
                                log_provider_spend("coresignal", "member_collect", _cs_collected, returned=_cs_collected)
                            except Exception:
                                print("[ContactSearch] coresignal spend mirror failed")
                except Exception as cs_err:
                    print(f"[ContactSearch] Coresignal primary failed ({cs_err!r})")
                    remainder = []
                # The PDL rung is gone (2026-08-31, ported from mobile): the
                # subscription is retired and its key 401s on every call, so
                # a fallback to it could never rescue a search. Coresignal
                # falls straight to the Hunter bridge below.
                contacts = cache_hits + (remainder or [])
                adjacency_metadata = remainder_meta
                if cache_hits:
                    adjacency_metadata["firm_cache_hits"] = len(cache_hits)
                    adjacency_metadata["provider_fills"] = len(remainder or [])
        except Exception as ladder_err:
            print(f"[ContactSearch] provider ladder failed ({ladder_err!r})")
            contacts, retry_level_used, already_saved_contacts, adjacency_metadata = [], 0, [], {}
        try:
            from app.services.warehouse_metrics import log_demand, log_search_mix
            log_demand(parsed, "prompt_search")
            log_search_mix(
                "prompt_search", max_contacts, len(cache_hits or []),
                max(0, len(contacts or []) - len(cache_hits or [])),
            )
        except Exception:
            pass
        # Last rung: the Hunter bridge covers company-targeted searches via
        # Domain Search when both providers came back empty.
        if not contacts and not org_targets and (parsed.get("companies") or []):
            try:
                from app.services.hunter_person_search import search_people_via_hunter
                contacts, retry_level_used, already_saved_contacts, hunter_meta = search_people_via_hunter(
                    parsed, max_contacts, exclude_keys=seen_contact_set, user_profile=user_data
                )
                adjacency_metadata.update(hunter_meta or {})
                adjacency_metadata["fallback_used"] = "hunter"
            except Exception as hunter_err:
                print(f"[ContactSearch] Hunter bridge failed ({hunter_err!r})")
        # Warehouse feed: every collected profile becomes a permanent asset
        # (flag-gated by ENABLE_FIRM_CACHE_WRITE, async).
        try:
            from app.services.firm_cache.writer import cache_pdl_contacts
            # Directory rows stay out of the shared warehouse; the vetted subset
            # is mirrored deliberately by scripts/org_directory_mirror_to_warehouse.py.
            if not org_targets:
                cache_pdl_contacts(contacts, shape="app", async_write=True)
        except Exception:
            print("[ContactSearch] warehouse feed failed")
        search_broadened = retry_level_used >= 1

        # Surface which dimensions were dropped at the rung that succeeded so the
        # frontend can render an honest "we expanded by..." banner.
        broadened_dimensions: list[str] = []
        if retry_level_used >= 1:
            broadened_dimensions.append("title")
        if retry_level_used >= 2:
            broadened_dimensions.append("industry")
        if retry_level_used >= 3:
            broadened_dimensions.append("location")
        if retry_level_used >= 4:
            broadened_dimensions.append("company")
        if retry_level_used >= 5:
            broadened_dimensions = ["title", "industry", "location", "company"]

        def _build_saved_contact_cards(raw_already_saved):
            cards = []
            for c in (raw_already_saved or []):
                cards.append({
                    "FirstName": c.get("FirstName") or c.get("firstName") or "",
                    "LastName": c.get("LastName") or c.get("lastName") or "",
                    "Title": c.get("Title") or c.get("JobTitle") or "",
                    "Company": c.get("Company") or c.get("company") or "",
                    "LinkedIn": c.get("LinkedIn") or c.get("linkedinUrl") or "",
                    "College": c.get("College") or c.get("college") or "",
                    "City": c.get("City") or "",
                    "State": c.get("State") or "",
                    "alreadySaved": True,
                })
            return cards

        parsed_query_payload = {
            "companies": parsed.get("companies", []),
            "title_variations": parsed.get("title_variations", []),
            "locations": parsed.get("locations", []),
            "company_context": parsed.get("company_context", ""),
        }
        if org_targets:
            parsed_query_payload["org_ids"] = org_targets
        if org_hint:
            parsed_query_payload["org_hint"] = org_hint

        if not contacts and not already_saved_contacts:
            response_data = {
                "contacts": [],
                "already_saved_contacts": [],
                "successful_drafts": 0,
                "total_contacts": 0,
                "tier": user_tier,
                "user_email": user_email,
                "parsed_query": parsed_query_payload,
            }
            if adjacency_metadata:
                response_data["adjacency_metadata"] = adjacency_metadata
                response_data["message"] = adjacency_metadata.get("message", "No contacts found. Try broadening your search.")
                # Add cross-tab suggestion for hiring managers if company was specified
                companies = parsed.get("companies") or []
                if companies:
                    company_name = companies[0].get("name", "") if isinstance(companies[0], dict) else str(companies[0])
                    if company_name:
                        response_data["suggestions"] = [{
                            "type": "switch_tab",
                            "label": f"Find recruiters at {company_name}",
                            "tab": "hiring-managers",
                            "prefill": {"company": company_name}
                        }]
            else:
                response_data["message"] = "No contacts found. Try broadening your search."
            return jsonify(response_data)

        # Pre-generation dedup: filter out contacts already in Firestore (avoid generating emails for them)
        # Reuse the exclusion data loaded during auth (no second Firestore stream).
        # Copy the sets because the save loop mutates them to prevent intra-batch duplicates,
        # and we don't want those mutations to leak into the cached exclusion data.
        if exclusion_data is not None:
            existing_emails_set = set(exclusion_data["email_set"])
            existing_linkedins_set = set(exclusion_data["linkedin_set"])
            existing_name_company_set = set(exclusion_data["name_company_set"])
            print(f"[ContactSearch] Reusing exclusion data for pre-gen dedup (saved Firestore re-stream)", flush=True)
        else:
            existing_emails_set = set()
            existing_linkedins_set = set()
            existing_name_company_set = set()

        if db and user_id:
            try:
                before_count = len(contacts)
                newly_skipped = [
                    c for c in contacts
                    if _contact_already_exists(c, existing_emails_set, existing_name_company_set, existing_linkedins_set)
                ]
                contacts = [
                    c for c in contacts
                    if not _contact_already_exists(c, existing_emails_set, existing_name_company_set, existing_linkedins_set)
                ]
                skipped_pre = before_count - len(contacts)
                if skipped_pre:
                    print(f"🔄 Pre-generation dedup: filtered out {skipped_pre} already-contacted people", flush=True)
                    # Surface these as "already saved" so the UI can show them as existing cards.
                    already_saved_contacts = list(already_saved_contacts or []) + newly_skipped

                if not contacts:
                    saved_cards = _build_saved_contact_cards(already_saved_contacts)
                    if saved_cards:
                        message = (
                            f"All {len(saved_cards)} matching contact(s) are already in your tracker. "
                            "Open them in your network, or broaden your search to find new people."
                        )
                    else:
                        message = "No contacts found. Try broadening your search."
                    return jsonify({
                        "contacts": [],
                        "already_saved_contacts": saved_cards,
                        "successful_drafts": 0,
                        "total_contacts": len(saved_cards),
                        "tier": user_tier,
                        "user_email": user_email,
                        "parsed_query": parsed_query_payload,
                        "message": message,
                    }), 200
            except Exception as e:
                print(f"⚠️ Pre-generation dedup failed, continuing: {e}", flush=True)

        # Trim to the originally requested count (whether or not dedup ran)
        contacts = contacts[:max_contacts]

        if _search_cancelled(db, user_id, search_id):
            print(f"[ContactSearch] {search_id} cancelled before enrichment; nothing charged or saved")
            return jsonify(_CANCELLED_RESPONSE), 200

        # Enrichment — deferred out of preview mode. Perplexity talking points,
        # Apify LinkedIn posts, and Perplexity company news are consumed only
        # by batch_generate_emails at draft time. The preview UI renders none
        # of these fields (grep-verified in connect-grow-hire/src/pages/
        # ContactSearchPage.tsx), so running them here just makes GET wait
        # ~10-15s for data no one looks at. When the user hits Draft/Send,
        # batch_generate_emails runs on freshly-enriched contacts as usual.
        enrichment_data = {}
        if outreach_mode != "preview":
            # Non-LinkedIn web presence (Perplexity, parallelized)
            try:
                from app.services.perplexity_client import batch_enrich_contacts
                enrichment_data = batch_enrich_contacts(contacts)
                for idx, contact in enumerate(contacts):
                    enrich = enrichment_data.get(idx, {})
                    if enrich.get("talking_points"):
                        contact["enrichment_talking_points"] = enrich["talking_points"]
                    if enrich.get("recent_activity"):
                        contact["enrichment_recent_activity"] = enrich["recent_activity"]
                    if enrich.get("media_appearances"):
                        contact["perplexity_media_appearances"] = enrich["media_appearances"]
                    if enrich.get("published_writing"):
                        contact["perplexity_published_writing"] = enrich["published_writing"]
                    if enrich.get("news_mentions"):
                        contact["perplexity_news_mentions"] = enrich["news_mentions"]
            except Exception:
                print("⚠️ Contact enrichment failed, continuing without", flush=True)

            # LinkedIn recent posts (Apify, single batched HTTP call)
            try:
                from app.services.apify_client import batch_enrich_linkedin_posts_via_apify
                apify_results = batch_enrich_linkedin_posts_via_apify(contacts) or {}
                for idx, contact in enumerate(contacts):
                    payload = apify_results.get(idx, {})
                    if payload.get("linkedin_recent_posts"):
                        contact["linkedin_recent_posts"] = payload["linkedin_recent_posts"]
            except Exception:
                print("⚠️ Apify LinkedIn enrichment failed, continuing", flush=True)

            # Company news (Perplexity, batched per unique company, parallelized)
            try:
                from app.services.perplexity_client import batch_enrich_company_news
                company_enrichment = batch_enrich_company_news(contacts) or {}
                for idx, contact in enumerate(contacts):
                    co = company_enrichment.get(idx, {})
                    if co.get("company_recent_news"):
                        contact["company_recent_news"] = co["company_recent_news"]
                    if co.get("company_description"):
                        contact["company_description"] = co["company_description"]
            except Exception:
                print("⚠️ Perplexity company enrichment failed, continuing", flush=True)
        else:
            print(f"[Runs] Preview mode: skipping Perplexity + Apify enrichment for {len(contacts)} contacts")

        # Cost telemetry — single line per search so spend impact is visible.
        try:
            _apify_post_count = sum(len(c.get("linkedin_recent_posts") or []) for c in contacts)
            _pplx_person_hits = sum(
                1 for c in contacts
                if c.get("perplexity_media_appearances")
                or c.get("perplexity_published_writing")
                or c.get("perplexity_news_mentions")
                or c.get("enrichment_talking_points")
            )
            _unique_companies = len({
                (c.get("Company") or "").strip().lower()
                for c in contacts if (c.get("Company") or "").strip()
            })
            print(
                f"[Enrich] uid={user_id} contacts={len(contacts)} "
                f"apify_posts={_apify_post_count} perplexity_person_hits={_pplx_person_hits} "
                f"perplexity_company_unique={_unique_companies}",
                flush=True,
            )
        except Exception:
            pass

        # Same pipeline as free-run: template, emails, drafts, deduct, save
        # A4: Build rich user profile from root doc + professionalInfo subcollection
        user_profile = data.get("userProfile") or (user_data or {}).get("userProfile")
        if not user_profile and db and user_id:
            try:
                prof_ref = db.collection("users").document(user_id).collection("professionalInfo").document("info")
                prof_doc = prof_ref.get()
                if prof_doc.exists:
                    pi = prof_doc.to_dict()
                    user_profile = {
                        "name": f"{pi.get('firstName', '')} {pi.get('lastName', '')}".strip() or user_email or "",
                        "email": user_email or "",
                        "university": pi.get("university", ""),
                        "major": pi.get("fieldOfStudy", ""),
                        "year": pi.get("graduationYear", ""),
                        "graduationYear": pi.get("graduationYear", ""),
                        "degree": pi.get("currentDegree", ""),
                    }
            except Exception:
                pass
        if not user_profile:
            user_profile = {"name": "", "email": user_email or ""}
        # A4: Enrich user_profile with onboarding data from root user document
        if user_data:
            for key in ("resumeParsed", "academics", "goals", "careerTrack",
                        "dreamCompanies", "hometown", "location", "pastCompanies"):
                if key in user_data and key not in user_profile:
                    user_profile[key] = user_data[key]
            # Professional persona: userType + currentRole/currentCompany ride
            # along so batch_generate_emails can drop the student framing.
            merge_persona_fields(user_profile, user_data)
        # Prefer the user's .edu as the outreach identity. Sets the email used in
        # the LLM body signature (batch_generate_emails) and the draft/send MIME
        # signature (user_info below). Falls back to the primary email.
        _outreach_email = get_outreach_email(user_data)
        if _outreach_email:
            user_profile["email"] = _outreach_email
        career_interests = data.get("careerInterests") or (user_data or {}).get("careerInterests", [])
        template_instructions, email_template_purpose, template_subject_line, signoff_config = _resolve_email_template(data.get("emailTemplate"), user_id, db, user_data=user_data)
        # Get resume filename for email body reference
        user_resume_filename = (user_data or {}).get("resumeFileName")

        auth_display_name = (getattr(request, "firebase_user", None) or {}).get("name") or ""

        # Resume assets — only fetched when we're actually going to draft/send.
        # Preview mode returns contact info only, so skip the download entirely.
        resume_url = (user_data or {}).get("resumeUrl") or (user_data or {}).get("resumeURL")
        resume_text = None
        resume_content = None
        resume_filename = None
        if outreach_mode != "preview" and resume_url:
            # Prefer the pre-extracted resumeText saved on the user doc at upload
            # (see resume.py:save_resume_to_firebase). Avoids a pdfplumber pass
            # every run — the text hasn't changed since upload.
            _cached_text = (user_data or {}).get("resumeText")
            if _cached_text and len(_cached_text.strip()) > 50:
                resume_text = _cached_text
                print(f"[Runs] Using cached resumeText from user doc ({len(resume_text)} chars)")

            try:
                print(f"[Runs] Downloading resume bytes for attachment: {resume_url[:80]}...")
                _content, _fname = download_resume_from_url(resume_url)
                if _content:
                    resume_content = _content
                    resume_filename = _fname
                    # Only re-extract if the cached text was missing / too short.
                    if not resume_text:
                        resume_text = extract_text_from_pdf_bytes(_content)
                        if resume_text and len(resume_text.strip()) > 50:
                            print(f"[Runs] Extracted {len(resume_text)} chars from resume PDF")
                        else:
                            print(f"[Runs] Resume text extraction too short ({len(resume_text or '')} chars)")
                            resume_text = None
                else:
                    print("[Runs] Resume download returned no content")
            except Exception as e:
                print(f"[Runs] Could not download/extract resume: {e}")

        # 1A: Score, sort by warmth (dream companies first), and attach fields
        try:
            contacts = score_and_sort_contacts(user_profile, contacts, search_context=parsed_query_payload)
            warmth_data = {
                i: {"tier": c.get("warmth_tier", ""), "score": c.get("warmth_score", 0), "label": c.get("warmth_label", ""), "signals": c.get("warmth_signals", [])}
                for i, c in enumerate(contacts)
            }
        except Exception:
            warmth_data = score_contacts_for_email(user_profile, contacts, search_context=parsed_query_payload)

        # 1B: Attach briefing lines (deterministic, no LLM)
        for contact in contacts:
            signals = contact.get("warmth_signals", [])
            briefing = build_briefing_line(contact, signals)
            if briefing:
                contact["briefing"] = briefing

        # Email pass (2026-08-27, runs in EVERY mode including preview).
        # PDL used to hand us emails inline with each person record, so the
        # preview response already carried them and the Draft button just
        # worked. Coresignal records carry no emails, so preview must earn
        # them here or the whole website Find returns email-less contacts
        # and drafting is a no-op. FullEnrich-only as of 2026-09-01 (per the
        # reseller agreement thread: Hunter is one of FullEnrich's own
        # upstream providers and verification is included in their 1-credit
        # cost, so the Hunter rung here paid twice for the same lookup):
        # warehouse hits arrive with emails already, then ONE FullEnrich
        # bulk waterfall for whoever is left, with a bounded wait. Misses
        # stay email-less rather than blocking forever.
        _FE_BACKFILL_CAP = 6
        _fe_wait = int(os.environ.get("FE_SEARCH_WAIT_SECONDS", "75"))
        _gained_email = []
        _missing = [c for c in contacts
                    if not (c.get("Email") or c.get("email") or "").strip()][:_FE_BACKFILL_CAP]
        if _missing:
            # Hunter first (2026-09-07): one database, 2 to 3 seconds each,
            # run in parallel, accepted at confidence 90+. Whoever it finds
            # skips the waterfall; the rest go to FullEnrich as before. Same
            # ladder as the extension and the deck reveal (email_fast).
            try:
                from app.services import email_fast as _ef
                from concurrent.futures import ThreadPoolExecutor as _TPE
                def _hunt(_c):
                    return _ef.hunter_email(
                        (_c.get("FirstName") or "").strip(),
                        (_c.get("LastName") or "").strip(),
                        (_c.get("Company") or "").strip(),
                    )
                with _TPE(max_workers=max(1, len(_missing))) as _ex:
                    _hunter_hits = list(_ex.map(_hunt, _missing))
                for _c, _h in zip(_missing, _hunter_hits):
                    if _h and _h.get("email"):
                        _c["Email"] = _h["email"]
                        _c["WorkEmail"] = _h["email"]
                        _c["EmailSource"] = "hunter_finder"
                        _c["EmailVerified"] = False
                        _c["EmailConfidenceScore"] = int(_h.get("confidence") or 0)
                print(f"[ContactSearch] Hunter fast rung: "
                      f"{sum(1 for _h in _hunter_hits if _h)}/{len(_missing)} found")
            except Exception:
                print("[ContactSearch] Hunter fast rung errored; everyone goes to FullEnrich")
            _still = [c for c in _missing if not (c.get("Email") or "").strip()]
            if _still:
                try:
                    from app.services import fullenrich_client
                    from app.extensions import get_db as _get_db
                    _fe_db = _get_db()
                    _people = [(str(i),
                                (c.get("FirstName") or "").strip(),
                                (c.get("LastName") or "").strip(),
                                (c.get("Company") or "").strip(),
                                (c.get("LinkedIn") or "").strip())
                               for i, c in enumerate(_still)]
                    _eid = fullenrich_client.start_bulk(_people, db=_fe_db)
                    if _eid:
                        _ready, _hits = fullenrich_client.fetch_bulk(
                            _eid, wait_seconds=_fe_wait, db=_fe_db)
                        for _i, _hit in (_hits or {}).items():
                            _c = _still[int(_i)]
                            _c["Email"] = _hit["email"]
                            _c["WorkEmail"] = _hit["email"]
                            _c["EmailSource"] = "fullenrich_backfill"
                            _c["EmailVerified"] = True
                            _gained_email.append(_c)
                        print(f"[ContactSearch] FullEnrich search pass: "
                              f"{len(_hits or {})}/{len(_still)} found (ready={_ready})")
                except Exception:
                    print("[ContactSearch] FullEnrich search pass errored; contacts stay email-less")
        # Re-feed the warehouse for anyone who just gained a verified address,
        # so the next student's search serves this email instantly and free.
        if _gained_email:
            try:
                from app.services.firm_cache.writer import cache_pdl_contacts as _cache_feed
                _cache_feed(_gained_email, shape="app", async_write=True)
            except Exception:
                pass

        # Generate emails with resume text. Skipped entirely in preview mode:
        # preview returns contact info only, so no email is written and (because
        # contacts_with_emails stays empty below) no Gmail draft is created.
        if outreach_mode == "preview":
            email_results = {}
            print(f"[Runs] Preview mode: skipping email generation and drafting for {len(contacts)} contacts")
        else:
            try:
                email_results = batch_generate_emails(
                    contacts=contacts,
                    resume_text=resume_text,
                    user_profile=user_profile,
                    career_interests=career_interests,
                    fit_context=None,
                    pre_parsed_user_info=(user_data or {}).get("resumeParsed"),
                    template_instructions=template_instructions,
                    email_template_purpose=email_template_purpose,
                    resume_filename=user_resume_filename,
                    subject_line=template_subject_line,
                    signoff_config=signoff_config,
                    auth_display_name=auth_display_name,
                    warmth_data=warmth_data,
                    uid=user_id,
                    enrichment_data=enrichment_data,
                    # The About box and target firms never reached generation
                    # from this pipeline; both params existed and defaulted
                    # empty (ported from the mobile fix, 2026-08-31).
                    personal_note=(user_data or {}).get("personalNote") or "",
                    dream_companies=(user_data or {}).get("dreamCompanies"),
                )
            except Exception as e:
                print(f"[Runs] Email generation failed (prompt-search): {e}")
                email_results = {}

        contacts_with_emails = []
        for i, contact in enumerate(contacts):
            key = str(i)
            email_result = email_results.get(i) or email_results.get(key) or email_results.get(f"{i}")
            if email_result and isinstance(email_result, dict):
                subject = email_result.get("subject", "")
                body = email_result.get("plain_body") or email_result.get("body", "")
                if subject and body:
                    contact["emailSubject"] = subject
                    contact["emailBody"] = body
                    # Attach personalization metadata (label, commonality_type, anchor)
                    if email_result.get("personalization"):
                        contact["personalization"] = email_result["personalization"]
                    attach_resume = (email_template_purpose in PURPOSES_INCLUDE_RESUME) or email_body_mentions_resume(body)
                    contacts_with_emails.append({
                        "index": i,
                        "contact": contact,
                        "email_subject": subject,
                        "email_body": body,
                        "attach_resume": attach_resume,
                    })

        # 1E: Silent email quality gate — check + parallel regen for failures
        try:
            from concurrent.futures import ThreadPoolExecutor, as_completed

            _qg_user_university = (user_profile or {}).get("university", "")

            def _quality_check_and_regen(item):
                """Check one email; regen if needed. Stamps the item with the
                final quality result under `_finalQualityResult` so the
                Firestore log loop below can reuse it instead of running
                check_email_quality a third time. Returns (index, was_regenerated).
                """
                contact = item["contact"]
                subject = item["email_subject"]
                body = item["email_body"]
                result = check_email_quality(subject, body, contact, _qg_user_university)
                if result["passed"]:
                    item["_finalQualityResult"] = result
                    return (item["index"], False)
                # Attempt regeneration
                original = {"subject": subject, "body": body}
                _regen_sender = (auth_display_name or (user_profile or {}).get("name") or "").strip()
                improved = regenerate_with_feedback(
                    contact,
                    user_profile,
                    original,
                    result["failures"],
                    sender_name=_regen_sender or None,
                    signoff_config=signoff_config,
                )
                # Compare: pick the one that passes, or the one with fewer failures
                improved_result = check_email_quality(improved["subject"], improved["body"], contact, _qg_user_university)
                if improved_result["passed"] or len(improved_result["failures"]) < len(result["failures"]):
                    contact["emailSubject"] = improved["subject"]
                    contact["emailBody"] = improved["body"]
                    item["email_subject"] = improved["subject"]
                    item["email_body"] = improved["body"]
                    item["_finalQualityResult"] = improved_result
                else:
                    item["_finalQualityResult"] = result
                contact["_qualityRegenerated"] = True
                return (item["index"], True)

            regen_count = 0
            if contacts_with_emails:
                with ThreadPoolExecutor(max_workers=5) as executor:
                    futures = {executor.submit(_quality_check_and_regen, item): item for item in contacts_with_emails}
                    for future in as_completed(futures):
                        try:
                            _, was_regen = future.result()
                            if was_regen:
                                regen_count += 1
                        except Exception:
                            pass

            if regen_count > 0:
                print(f"[Runs] Quality gate: regenerated {regen_count}/{len(contacts_with_emails)} emails")

            # Log quality gate results to Firestore — reuse the final result
            # stamped on each item by the executor above. Saves one full
            # check_email_quality call per contact.
            if db and user_id and contacts_with_emails:
                try:
                    quality_ref = db.collection("email_quality_logs")
                    for item in contacts_with_emails:
                        contact = item["contact"]
                        qr = item.get("_finalQualityResult") or check_email_quality(
                            item["email_subject"], item["email_body"], contact, _qg_user_university
                        )
                        quality_ref.add({
                            "userId": user_id,
                            "contactId": contact.get("pdlId", ""),
                            "passed": qr["passed"],
                            "failures": qr["failures"],
                            "regenerated": bool(contact.get("_qualityRegenerated")),
                            "timestamp": datetime.utcnow().isoformat() + "Z",
                        })
                except Exception as qlog_err:
                    print(f"[Runs] Quality log write failed: {qlog_err}")
        except Exception as qgate_err:
            print(f"[Runs] Quality gate error (non-blocking): {qgate_err}")

        # Send-mode guardrails (Elite only)
        # 1) Quality gate: any email that fails check_email_quality is routed to
        #    the draft path instead of being sent. The user sees the questionable
        #    ones in their Gmail drafts for manual review.
        # 2) Daily send cap: ELITE_DAILY_SEND_CAP sends per UTC day. Overflow
        #    falls back to drafts. Counter lives on the user doc and resets when
        #    the calendar date changes.
        ELITE_DAILY_SEND_CAP = 20
        guardrail_blocked_count = 0
        cap_blocked_count = 0
        successful_drafts = 0
        successful_sends = 0
        user_info = {"name": user_profile.get("name", ""), "email": user_profile.get("email", ""), "phone": "", "linkedin": ""}
        try:
            creds = _load_user_gmail_creds(user_id) if user_id else None
            if creds and contacts_with_emails and outreach_mode == "send":
                # Split by quality first
                send_queue: list = []
                draft_fallback_queue: list = []
                for item in contacts_with_emails:
                    contact = item["contact"]
                    try:
                        qr = check_email_quality(
                            item["email_subject"], item["email_body"], contact, _qg_user_university
                        )
                        if qr.get("passed", False):
                            send_queue.append(item)
                        else:
                            contact["_sendBlockedByQuality"] = True
                            contact["_qualityFailures"] = qr.get("failures", [])
                            draft_fallback_queue.append(item)
                            guardrail_blocked_count += 1
                    except Exception as q_err:
                        # If the quality check itself errors, fail-safe: route to draft.
                        print(f"[Runs] Quality check error, routing to draft: {q_err}")
                        contact["_sendBlockedByQuality"] = True
                        draft_fallback_queue.append(item)
                        guardrail_blocked_count += 1

                # Daily send cap: read counter, clamp send_queue
                today_str = datetime.utcnow().strftime("%Y-%m-%d")
                sends_today = 0
                user_doc_ref = None
                if db and user_id:
                    try:
                        user_doc_ref = db.collection("users").document(user_id)
                        user_snap = user_doc_ref.get()
                        if user_snap.exists:
                            user_dict = user_snap.to_dict() or {}
                            if user_dict.get("dailySendDate") == today_str:
                                sends_today = int(user_dict.get("dailySendCount", 0) or 0)
                    except Exception as cap_read_err:
                        print(f"[Runs] Daily send cap read failed: {cap_read_err}")

                cap_remaining = max(0, ELITE_DAILY_SEND_CAP - sends_today)
                if cap_remaining < len(send_queue):
                    overflow = send_queue[cap_remaining:]
                    for item in overflow:
                        item["contact"]["_sendBlockedByDailyCap"] = True
                        cap_blocked_count += 1
                    draft_fallback_queue.extend(overflow)
                    send_queue = send_queue[:cap_remaining]

                print(
                    f"[Runs] Send guardrails: {len(send_queue)} approved to send, "
                    f"{guardrail_blocked_count} blocked by quality, "
                    f"{cap_blocked_count} blocked by daily cap (sent_today={sends_today}, cap={ELITE_DAILY_SEND_CAP})"
                )

                # Send the approved batch
                if send_queue:
                    from app.services.gmail_client import send_emails_parallel
                    send_results = send_emails_parallel(
                        send_queue,
                        resume_bytes=resume_content,
                        resume_filename=resume_filename,
                        user_info=user_info,
                        user_id=user_id,
                        tier=user_tier,
                        user_email=user_email,
                        resume_url=resume_url,
                    )
                    for item, send_result in zip(send_queue, send_results):
                        contact = item["contact"]
                        message_id = send_result.get("message_id", "") if isinstance(send_result, dict) else ""
                        if message_id and not str(message_id).startswith("mock_"):
                            successful_sends += 1
                            contact["emailSent"] = True
                            contact["gmailMessageId"] = message_id
                            if send_result.get("thread_id"):
                                contact["gmailThreadId"] = send_result["thread_id"]
                            if send_result.get("recipient_email"):
                                contact["_sentRecipientEmail"] = send_result["recipient_email"]

                    # Bump daily counter for the actually-sent emails
                    if user_doc_ref and successful_sends > 0:
                        try:
                            user_doc_ref.set(
                                {
                                    "dailySendCount": sends_today + successful_sends,
                                    "dailySendDate": today_str,
                                },
                                merge=True,
                            )
                        except Exception as cap_write_err:
                            print(f"[Runs] Daily send counter write failed: {cap_write_err}")

                # Draft the fallback batch (quality-blocked + cap-overflow)
                if draft_fallback_queue:
                    from app.services.gmail_client import create_drafts_parallel
                    fb_draft_results = create_drafts_parallel(
                        draft_fallback_queue,
                        resume_bytes=resume_content,
                        resume_filename=resume_filename,
                        user_info=user_info,
                        user_id=user_id,
                        tier=user_tier,
                        user_email=user_email,
                        resume_url=resume_url,
                    )
                    for item, draft_result in zip(draft_fallback_queue, fb_draft_results):
                        contact = item["contact"]
                        draft_id = draft_result.get("draft_id", "") if isinstance(draft_result, dict) else (draft_result or "")
                        if draft_id and not str(draft_id).startswith("mock_"):
                            successful_drafts += 1
                            contact["gmailDraftId"] = draft_id
                            if isinstance(draft_result, dict):
                                if draft_result.get("draft_url"):
                                    contact["gmailDraftUrl"] = draft_result["draft_url"]
                                if draft_result.get("recipient_email"):
                                    contact["_draftRecipientEmail"] = draft_result["recipient_email"]
            elif creds and contacts_with_emails:
                from app.services.gmail_client import create_drafts_parallel
                draft_results = create_drafts_parallel(
                    contacts_with_emails,
                    resume_bytes=resume_content,
                    resume_filename=resume_filename,
                    user_info=user_info,
                    user_id=user_id,
                    tier=user_tier,
                    user_email=user_email,
                    resume_url=resume_url,
                )
                for item, draft_result in zip(contacts_with_emails, draft_results):
                    contact = item["contact"]
                    draft_id = draft_result.get("draft_id", "") if isinstance(draft_result, dict) else (draft_result or "")
                    if draft_id and not str(draft_id).startswith("mock_"):
                        successful_drafts += 1
                        contact["gmailDraftId"] = draft_id
                        if isinstance(draft_result, dict):
                            if draft_result.get("draft_url"):
                                contact["gmailDraftUrl"] = draft_result["draft_url"]
                            if draft_result.get("recipient_email"):
                                contact["_draftRecipientEmail"] = draft_result["recipient_email"]
        except Exception as gmail_error:
            err_str = str(gmail_error).lower()
            if "invalid_grant" in err_str or "token has been expired or revoked" in err_str:
                # Still deduct credits and save contacts even though drafts failed
                if db and user_id:
                    try:
                        deduct_credits_atomic(user_id, CREDIT_COSTS['find_contact'] * len(contacts), "prompt_search")
                    except Exception:
                        pass
                return jsonify({
                    "error": "gmail_token_expired",
                    "message": "Your Gmail connection has expired. Please reconnect your Gmail account.",
                    "require_reauth": True,
                    "contacts": contacts,
                }), 401
            print(f"[Runs] Gmail draft error (prompt-search): {gmail_error}")

        if _search_cancelled(db, user_id, search_id):
            print(f"[ContactSearch] {search_id} cancelled before save; nothing charged or saved")
            return jsonify(_CANCELLED_RESPONSE), 200

        if not (db and user_id):
            print(f"[Runs] Prompt-search: skipping Firestore save")
        credits_used = 0
        credits_remaining = None
        if db and user_id:
            try:
                credits_amount = CREDIT_COSTS['find_contact'] * len(contacts)
                success, remaining = deduct_credits_atomic(user_id, credits_amount, "prompt_search")
                if success:
                    credits_used = credits_amount
                    credits_remaining = remaining
                else:
                    print(f"⚠️ Credit deduction failed for {user_id}: insufficient credits (have {remaining}, need {credits_amount})")
                    credits_remaining = remaining
            except Exception as credit_error:
                print(f"⚠️ Credit deduction error for {user_id}: {credit_error}")
                traceback.print_exc()
            try:
                print(f"💾 Saving {len(contacts)} contacts to Firestore (prompt-search)...")
                contacts_ref = db.collection("users").document(user_id).collection("contacts")
                # Reuse pre-gen dedup sets instead of re-streaming Firestore
                today = datetime.now().strftime("%m/%d/%Y")
                saved_count = 0
                skipped_count = 0
                for contact in contacts:
                    if _contact_already_exists(contact, existing_emails_set, existing_name_company_set, existing_linkedins_set):
                        skipped_count += 1
                        continue
                    first_name = (contact.get("FirstName") or contact.get("firstName") or "").strip()
                    last_name = (contact.get("LastName") or contact.get("lastName") or "").strip()
                    email = (contact.get("Email") or contact.get("WorkEmail") or contact.get("PersonalEmail") or contact.get("email") or "").strip().lower()
                    linkedin = (contact.get("LinkedIn") or contact.get("linkedinUrl") or "").strip()
                    company = (contact.get("Company") or contact.get("company") or "").strip()
                    contact_doc = {
                        "firstName": first_name,
                        "lastName": last_name,
                        "email": contact.get("Email") or contact.get("WorkEmail") or contact.get("PersonalEmail") or "",
                        "linkedinUrl": linkedin,
                        "company": company,
                        "jobTitle": contact.get("Title") or contact.get("jobTitle") or "",
                        "college": contact.get("College") or contact.get("college") or "",
                        "location": contact.get("location") or "",
                        "city": contact.get("City") or "",
                        "state": contact.get("State") or "",
                        "firstContactDate": today,
                        "status": "Not Contacted",
                        "lastContactDate": today,
                        "userId": user_id,
                        "createdAt": datetime.utcnow().isoformat() + "Z",  # TODO: deprecated in Python 3.12
                        # pdlId persists the PDL stable identifier for queue dedup.
                        "pdlId": contact.get("pdlId") or "",
                    }
                    # Email confidence metadata (set from the waterfall in
                    # pdl_client.extract_contact_from_pdl_person_enhanced).
                    # Persisted so My Network can render a "Verified" /
                    # "Best guess" badge and we have data for tuning.
                    contact_doc["emailSource"] = contact.get("EmailSource") or None
                    # Find's "Recently found" feed (OFF-56) groups these by the
                    # search that produced them.
                    contact_doc["source"] = "find_search"
                    contact_doc["foundQuery"] = prompt[:200]
                    # Face for Inbox/Network rows; clients fall back to
                    # initials when the hotlink expires.
                    contact_doc["photoUrl"] = (contact.get("PhotoUrl") or "").strip()
                    contact_doc["emailVerified"] = bool(contact.get("EmailVerified"))
                    contact_doc["emailConfidenceScore"] = int(contact.get("EmailConfidenceScore") or 0)
                    if contact.get("emailSubject"):
                        contact_doc["emailSubject"] = contact["emailSubject"]
                    if contact.get("emailBody"):
                        contact_doc["emailBody"] = contact["emailBody"]
                    if contact.get("gmailDraftId"):
                        contact_doc["gmailDraftId"] = contact["gmailDraftId"]
                    if contact.get("gmailDraftUrl"):
                        contact_doc["gmailDraftUrl"] = contact["gmailDraftUrl"]
                    if contact.get("warmth_score") is not None:
                        contact_doc["warmthScore"] = contact["warmth_score"]
                        contact_doc["warmthTier"] = contact.get("warmth_tier", "")
                        contact_doc["warmthLabel"] = contact.get("warmth_label", "")
                        contact_doc["warmthSignals"] = contact.get("warmth_signals", [])
                    if contact.get("personalization"):
                        contact_doc["personalizationLabel"] = contact["personalization"].get("label", "")
                        contact_doc["personalizationType"] = contact["personalization"].get("commonality_type", "")
                    if contact.get("_qualityRegenerated"):
                        contact_doc["qualityRegenerated"] = True
                    if contact.get("briefing"):
                        contact_doc["briefing"] = contact["briefing"]
                    if contact.get("enrichment_talking_points"):
                        contact_doc["enrichmentTalkingPoints"] = contact["enrichment_talking_points"][:5]
                    if contact.get("enrichment_recent_activity"):
                        contact_doc["enrichmentRecentActivity"] = contact["enrichment_recent_activity"]
                    if contact.get("perplexity_media_appearances"):
                        contact_doc["perplexityMediaAppearances"] = contact["perplexity_media_appearances"][:5]
                    if contact.get("perplexity_published_writing"):
                        contact_doc["perplexityPublishedWriting"] = contact["perplexity_published_writing"][:5]
                    if contact.get("perplexity_news_mentions"):
                        contact_doc["perplexityNewsMentions"] = contact["perplexity_news_mentions"][:5]
                    if contact.get("linkedin_recent_posts"):
                        contact_doc["linkedinRecentPosts"] = contact["linkedin_recent_posts"][:5]
                    if contact.get("company_recent_news"):
                        contact_doc["companyRecentNews"] = contact["company_recent_news"][:5]
                    if contact.get("company_description"):
                        contact_doc["companyDescription"] = contact["company_description"][:1000]
                    if contact.get("gmailDraftId") or contact.get("gmailDraftUrl"):
                        contact_doc["pipelineStage"] = "draft_created"
                    now_iso = datetime.utcnow().isoformat() + "Z"  # TODO: deprecated in Python 3.12
                    # Preview writes no email, so the contact isn't in the
                    # outbox yet; marking it there made Find show "Drafted".
                    if outreach_mode == "preview":
                        contact_doc["inOutbox"] = False
                    else:
                        contact_doc["inOutbox"] = True
                        contact_doc["draftToEmail"] = contact.get("_draftRecipientEmail") or email
                        contact_doc["draftCreatedAt"] = now_iso
                        contact_doc["emailGeneratedAt"] = now_iso
                        contact_doc["draftStillExists"] = True
                    contact_doc["lastActivityAt"] = now_iso
                    contact_doc["hasUnreadReply"] = False
                    contact_doc["gmailMessageId"] = contact.get("gmailMessageId") or None
                    # Send mode: overlay the fields that mark this contact as
                    # "sent" in the tracker. These mirror what the Gmail webhook
                    # writes when it detects a SENT message, so a direct send and
                    # a later webhook event converge instead of conflicting.
                    if contact.get("emailSent"):
                        contact_doc["pipelineStage"] = "waiting_on_reply"
                        contact_doc["emailSentAt"] = now_iso
                        contact_doc["draftStillExists"] = False
                        contact_doc["draftToEmail"] = contact.get("_sentRecipientEmail") or contact_doc["draftToEmail"]
                        if contact.get("gmailThreadId"):
                            contact_doc["gmailThreadId"] = contact["gmailThreadId"]
                    contacts_ref.add(contact_doc)
                    saved_count += 1
                    # Avoid duplicates within same batch
                    if email:
                        existing_emails_set.add(email)
                    if linkedin:
                        existing_linkedins_set.add(linkedin)
                    if first_name and last_name and company:
                        existing_name_company_set.add(f"{first_name}_{last_name}_{company}".lower().strip())
                print(f"✅ Prompt-search: saved {saved_count} new contacts to Firestore, skipped {skipped_count} duplicates")
            except Exception as save_error:
                print(f"⚠️ Error saving contacts (prompt-search): {save_error}")
                traceback.print_exc()

        # Metrics: log email_generated per contact with email (outside save block
        # so events fire even if Firestore save fails — emails were already generated)
        try:
            from app.utils.metrics_events import log_event
            for contact in contacts:
                if contact.get("emailSubject"):
                    body = contact.get("emailBody", "")
                    log_event(user_id, "email_generated", {
                        "contact_id": contact.get("pdlId") or "",
                        "email_length": len(body.split()),
                        "has_specificity_signal": has_specificity_signal(body, contact),
                        "quality_regenerated": bool(contact.get("_qualityRegenerated")),
                    })
        except Exception:
            pass

        # Build lightweight already-saved contact cards (no emails, no credits)
        saved_contact_cards = _build_saved_contact_cards(already_saved_contacts)

        response_data = {
            "contacts": contacts,
            "already_saved_contacts": saved_contact_cards,
            "successful_drafts": successful_drafts,
            "successful_sends": successful_sends,
            "send_blocked_by_quality": guardrail_blocked_count,
            "send_blocked_by_daily_cap": cap_blocked_count,
            "daily_send_cap": ELITE_DAILY_SEND_CAP,
            "mode": outreach_mode,
            "total_contacts": len(contacts),
            "tier": user_tier,
            "user_email": user_email,
            "credits_used": credits_used,
            "parsed_query": parsed_query_payload,
            "search_broadened": search_broadened,
            "retry_level_used": retry_level_used,
            "broadened_dimensions": broadened_dimensions,
            "provider": (adjacency_metadata or {}).get("provider", "pdl"),
        }
        if search_broadened:
            response_data["broadening_level"] = retry_level_used
        if adjacency_metadata and adjacency_metadata.get("drop_reasons"):
            response_data["adjacency_metadata"] = adjacency_metadata
        if credits_remaining is not None:
            response_data["credits_remaining"] = credits_remaining

        # Metrics: log search_performed
        try:
            from app.utils.metrics_events import log_event
            top_tier = ""
            if warmth_data:
                tiers = [v.get("tier", "") for v in warmth_data.values()]
                for t in ["warm", "neutral", "cold"]:
                    if t in tiers:
                        top_tier = t
                        break
            log_event(user_id, "search_performed", {
                "companies": parsed_query_payload.get("companies", []),
                "titles": parsed_query_payload.get("title_variations", [])[:5],
                "locations": parsed_query_payload.get("locations", []),
                "results_count": len(contacts),
                "top_warmth_tier": top_tier,
            })
        except Exception:
            pass

        # Recommendation events: log each shown contact with rank and features
        try:
            from app.utils.recommendation_events import log_recommendation_event
            for rank_idx, contact in enumerate(contacts):
                wd = warmth_data.get(rank_idx, {})
                log_recommendation_event(
                    "recommendation_shown",
                    user_id,
                    contact_id=contact.get("pdlId", ""),
                    contact_email=contact.get("Email") or contact.get("WorkEmail") or "",
                    rank=rank_idx,
                    score=wd.get("score"),
                    surface="find_search",
                    search_query=parsed_query_payload,
                    features_snapshot={
                        "warmth_tier": wd.get("tier", ""),
                        "warmth_score": wd.get("score", 0),
                        "warmth_signals": wd.get("signals", []),
                        "quality_regenerated": bool(contact.get("_qualityRegenerated")),
                    },
                )
        except Exception:
            pass

        return jsonify(response_data)
    except (OfferloopException, InsufficientCreditsError, ExternalAPIError):
        raise
    except Exception as e:
        print(f"Prompt-search error: {e}")
        traceback.print_exc()
        raise OfferloopException(f"Prompt search failed: {str(e)}", error_code="PROMPT_SEARCH_ERROR")


# =============================================================================
# ADMIN METERING ENDPOINTS
# Read-only dashboards for provider spend. Gated to UIDs listed in the
# ADMIN_UIDS env var (comma-separated) so only the founder can see them.
# Lives in runs.py to avoid wsgi.py blueprint-registration churn.
# =============================================================================


def _is_admin(uid: str) -> bool:
    """True if uid is in the comma-separated ADMIN_UIDS env var."""
    import os as _os
    admins = (_os.environ.get("ADMIN_UIDS") or "").split(",")
    return bool(uid) and uid in {a.strip() for a in admins if a.strip()}


@runs_bp.route("/admin/spend-check", methods=["POST", "GET"])
def admin_spend_check():
    """
    Run the provider-spend safeguard: total today + month-to-date spend and fire
    a Telegram alert on any newly-crossed budget threshold (50/80/100%).

    Auth is a shared secret (header `X-Spend-Token` or `?token=`) matching the
    SPEND_CHECK_TOKEN env var, so a GitHub Actions cron can hit it without a
    Firebase session. Fails closed if the token isn't configured.

    `?force=1` re-sends the current highest threshold even if already alerted
    (manual test). `?dry=1` returns the spend snapshot without sending alerts.
    """
    import os as _os
    expected = _os.environ.get("SPEND_CHECK_TOKEN")
    provided = request.headers.get("X-Spend-Token") or request.args.get("token")
    if not expected or provided != expected:
        return jsonify({"error": "forbidden"}), 403

    from app.services.spend_alerts import check_and_alert, compute_spend
    if request.args.get("dry") in ("1", "true"):
        return jsonify({"ok": True, "dry_run": True, "spend": compute_spend()}), 200

    force = request.args.get("force") in ("1", "true")
    return jsonify(check_and_alert(force=force)), 200


@runs_bp.route("/admin/metering/spend-by-provider", methods=["GET"])
@require_firebase_auth
def admin_spend_by_provider():
    """GET /api/admin/metering/spend-by-provider?days=7
    Returns total calls / credits / $cost grouped by provider × endpoint."""
    uid = request.firebase_user.get("uid")
    if not _is_admin(uid):
        return jsonify({"error": "forbidden"}), 403
    try:
        days = max(1, min(90, int(request.args.get("days", 7))))
    except (TypeError, ValueError):
        days = 7
    return jsonify(spend_summary(days=days)), 200


@runs_bp.route("/admin/metering/spend-by-user", methods=["GET"])
@require_firebase_auth
def admin_spend_by_user():
    """GET /api/admin/metering/spend-by-user?days=7&limit=25
    Returns top users by est_cost_usd over the window."""
    uid = request.firebase_user.get("uid")
    if not _is_admin(uid):
        return jsonify({"error": "forbidden"}), 403
    try:
        days = max(1, min(90, int(request.args.get("days", 7))))
        limit = max(1, min(100, int(request.args.get("limit", 25))))
    except (TypeError, ValueError):
        days, limit = 7, 25
    return jsonify(spend_by_user(days=days, limit=limit)), 200


@runs_bp.route("/admin/metering/ping", methods=["GET"])
@require_firebase_auth
def admin_metering_ping():
    """Health check: confirms admin auth works and metering module is loaded."""
    uid = request.firebase_user.get("uid")
    if not _is_admin(uid):
        return jsonify({"error": "forbidden"}), 403
    return jsonify({"ok": True, "uid": uid, "admin": True}), 200
