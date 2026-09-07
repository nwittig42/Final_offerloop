"""
Find Similar Contacts — used by the Chrome extension's email-found card.

After a contact is found via /api/contacts/import-linkedin, the extension
exposes a "Find similar at {Company}" disclosure. Clicking it calls this
endpoint with the source contact's profile and gets back up to 3 contacts
in three buckets so the discovery surface mixes axes:

    A. same company, same/similar role          (lateral peers)
    B. same company, any other role             (warm-intro paths)
    C. different company, same/similar role     (industry peers)

Implementation: 2 PDL queries — one same-company-no-title, one cross-company-
same-title. The same-company query is post-classified into buckets A and B
based on whether each returned contact's title matches the source's title
band. The cross-company query is post-filtered to drop any results PDL still
returned at the source's company.

Assembly is a two-pass fill: each slot gets its preferred bucket first
(A, B, C); empty slots are then backfilled in order A's runner-ups →
B's runner-ups → C's runner-ups. Every row keeps its true bucket label
so the UI can show *why* it was surfaced.

Popup hard-cap is 3 for all tiers — this is a discovery surface, not the
full filtered search. The "See all alumni at {Company}" footer link in
the popup deep-links to /find?tab=people&company=…&role=… on the website
where the user's normal tier limits apply.
"""

import traceback
from typing import Optional

from flask import Blueprint, request, jsonify

from app.extensions import require_firebase_auth, get_db
from app.config import TIER_CONFIGS
from app.services.auth import check_and_reset_credits, deduct_credits_atomic
from app.services.engine_search import engine_search_contacts
from app.services.pdl_client import get_contact_identity
from app.services.feature_flags import PDL_OUTAGE_ACTIVE
from app.routes.runs import _build_exclusion_data_from_firestore


runs_similar_bp = Blueprint("runs_similar", __name__, url_prefix="/api")


# Popup discovery surface — flat cap across all tiers.
POPUP_SIMILAR_CAP = 3

# Per-query PDL fetch budget. With 2 queries × 3 fetched, worst-case PDL
# burn is ~6 records + small buffer. Each query individually caps with
# the standard search_contacts_from_prompt buffer math.
_PDL_FETCH_PER_QUERY = 3

# Flat user-facing credit cost for one disclosure click. Up from 5 (v1
# single-query) to reflect the 2-query bucketed implementation.
SIMILAR_CREDIT_COST = 8


# Bucket identifiers — surfaced in the API response so the popup can
# render the right caption per row without re-deriving classification.
CAT_SAME_ROLE_SAME_CO = "same_role_same_co"
CAT_ANY_ROLE_SAME_CO = "any_role_same_co"
CAT_SAME_ROLE_OTHER_CO = "same_role_other_co"


def _title_bands(title: str) -> list[str]:
    """Expand a single title into a small band of adjacent seniorities so
    "Analyst" surfaces fellow Analysts + Associates + Senior Analysts.

    Keep this list short — PDL `title_variations` is a flat must-match OR
    set, so overly wide bands dilute the search."""
    t = (title or "").strip()
    if not t:
        return []
    tl = t.lower()
    out = [t]
    if "analyst" in tl and "senior" not in tl:
        out += ["Senior Analyst", "Associate"]
    elif "associate" in tl and "senior" not in tl:
        out += ["Senior Associate", "Analyst"]
    elif "consultant" in tl and "senior" not in tl:
        out += ["Senior Consultant", "Associate Consultant"]
    elif "engineer" in tl and "senior" not in tl:
        out += ["Senior Software Engineer", "Software Engineer II"]
    elif "manager" in tl and "senior" not in tl:
        out += ["Senior Manager"]
    seen = set()
    return [x for x in out if not (x.lower() in seen or seen.add(x.lower()))]


def _matches_title_band(contact_title: str, band: list[str]) -> bool:
    """Loose case-insensitive substring match either direction. Used to
    classify a same-company contact as "same role" (bucket A) vs "any
    other role" (bucket B)."""
    if not band:
        return False
    title = (contact_title or "").strip().lower()
    if not title:
        return False
    for b in band:
        bl = b.strip().lower()
        if not bl:
            continue
        if bl in title or title in bl:
            return True
    return False


def _same_company(contact: dict, source_company: str) -> bool:
    """Case-insensitive compare. Close-enough for popup discovery; PDL
    sometimes returns "Acme Inc." vs "Acme" which we treat as same."""
    c = (contact.get("Company") or "").strip().lower()
    s = (source_company or "").strip().lower()
    return bool(c) and bool(s) and (c == s or c.startswith(s) or s.startswith(c))


def _build_same_company_prompt(source: dict) -> dict:
    """Query 1: anyone at the source company. No title filter — we
    classify into buckets A/B on the result side."""
    company = (source.get("company") or "").strip()
    return {
        "original_prompt": f"Anyone at {company}",
        "company_context": "",
        "companies": [{"name": company, "matched_titles": []}] if company else [],
        "locations": [],
        "schools": [],
        "seniority_levels": [],
        "industries": [],
        "title_variations": [],
        "confidence": "high" if company else "low",
    }


def _build_cross_company_prompt(source: dict) -> dict:
    """Query 2: anyone with a similar title at any company. Result side
    post-filters out the source company so bucket C is genuinely cross-co."""
    title = (source.get("title") or "").strip()
    location = (source.get("location") or "").strip()
    title_band = _title_bands(title)
    return {
        "original_prompt": f"{title} at any company",
        "company_context": "",
        "companies": [],
        "locations": [location] if location else [],
        "schools": [],
        "seniority_levels": [],
        "industries": [],
        "title_variations": title_band,
        "confidence": "high" if title_band else "low",
    }


def _normalize_source(payload: dict) -> Optional[dict]:
    if not isinstance(payload, dict):
        return None
    source = payload.get("source") if isinstance(payload.get("source"), dict) else payload
    pick = lambda *keys: next((str(source.get(k)).strip() for k in keys if source.get(k)), "")
    return {
        "firstName": pick("firstName", "FirstName"),
        "lastName": pick("lastName", "LastName"),
        "company": pick("company", "Company"),
        "title": pick("title", "Title", "jobTitle", "JobTitle"),
        "location": pick("location", "Location", "city", "City"),
        "linkedinUrl": pick("linkedinUrl", "LinkedIn", "linkedin_url"),
    }


def _shape_for_popup(contact: dict, category: str) -> dict:
    """Trim the rich PDL contact dict down to what the popup needs to
    render a row. `category` is one of the CAT_* constants so the popup
    can render the right caption."""
    return {
        "firstName": contact.get("FirstName") or "",
        "lastName": contact.get("LastName") or "",
        "title": contact.get("Title") or "",
        "company": contact.get("Company") or "",
        "linkedinUrl": contact.get("LinkedIn") or "",
        "school": contact.get("College") or "",
        "city": contact.get("City") or "",
        "state": contact.get("State") or "",
        "email": (contact.get("Email") or "").strip()
            if (contact.get("Email") or "").strip().lower() not in ("", "not available")
            else "",
        "category": category,
    }


def _assemble_with_backfill(
    bucket_a: list[dict],
    bucket_b: list[dict],
    bucket_c: list[dict],
    cap: int = POPUP_SIMILAR_CAP,
) -> list[tuple[str, dict]]:
    """Two-pass fill — see module docstring for the rule.

    Buckets are mutated (pop from front) so each contact is taken at most
    once. Dedupe across buckets uses get_contact_identity, so the same
    person appearing in two PDL responses is assigned to the first bucket
    that wins them.

    Returns: list of (category, contact) tuples, length ≤ cap.
    """
    seen: set[str] = set()
    out: list[tuple[str, dict]] = []

    def _identity(c: dict) -> str:
        # Standardize keys before calling get_contact_identity which expects
        # the PascalCase shape.
        return get_contact_identity({
            "FirstName": c.get("FirstName", ""),
            "LastName": c.get("LastName", ""),
            "Email": c.get("Email", ""),
            "LinkedIn": c.get("LinkedIn", ""),
            "Company": c.get("Company", ""),
        })

    def take_one(bucket: list[dict], category: str) -> bool:
        """Pop next un-seen contact off the bucket into `out`. Returns
        True iff a slot was filled."""
        while bucket:
            c = bucket.pop(0)
            key = _identity(c)
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            out.append((category, c))
            return True
        return False

    # Pass 1 — give each slot its preferred bucket.
    take_one(bucket_a, CAT_SAME_ROLE_SAME_CO)
    if len(out) < cap:
        take_one(bucket_b, CAT_ANY_ROLE_SAME_CO)
    if len(out) < cap:
        take_one(bucket_c, CAT_SAME_ROLE_OTHER_CO)

    # Pass 2 — backfill empty slots from leftovers, A → B → C.
    for bucket, category in (
        (bucket_a, CAT_SAME_ROLE_SAME_CO),
        (bucket_b, CAT_ANY_ROLE_SAME_CO),
        (bucket_c, CAT_SAME_ROLE_OTHER_CO),
    ):
        while bucket and len(out) < cap:
            if not take_one(bucket, category):
                break

    return out[:cap]


@runs_similar_bp.route("/contacts/find-similar", methods=["POST"])
@require_firebase_auth
def find_similar():
    """Return up to POPUP_SIMILAR_CAP bucketed contacts similar to the source.

    Request body:  { "source": { company, title, location?, linkedinUrl?, firstName?, lastName? } }
    Response:      { "contacts": [{..., category}, ...], "cap": 3, "credits_used": 8, "credits_remaining": int }
    """
    if PDL_OUTAGE_ACTIVE:
        return jsonify({
            "error": "service_unavailable",
            "message": "Contact search temporarily unavailable.",
            "code": "PDL_OUTAGE",
        }), 503

    try:
        user_id = request.firebase_user["uid"]
        db = get_db()

        payload = request.get_json(silent=True) or {}
        source = _normalize_source(payload)
        if not source or not source.get("company"):
            return jsonify({
                "error": "missing_source_company",
                "message": "Source contact's company is required to find similar contacts.",
            }), 400

        user_data = None
        credits_available = TIER_CONFIGS["free"]["credits"]
        exclusion_keys: set = set()
        if db and user_id:
            try:
                user_ref = db.collection("users").document(user_id)
                user_doc = user_ref.get()
                if user_doc.exists:
                    user_data = user_doc.to_dict()
                    credits_available = check_and_reset_credits(user_ref, user_data)
                    exclusion_data = _build_exclusion_data_from_firestore(db, user_id)
                    exclusion_keys = set(exclusion_data["identity_set"])
            except Exception as e:
                print(f"⚠️ find-similar: failed to load user profile for {user_id}: {e}")
                return jsonify({"error": "Could not load user profile. Please try again."}), 500

        if credits_available < SIMILAR_CREDIT_COST:
            return jsonify({
                "error": "Insufficient credits",
                "credits_needed": SIMILAR_CREDIT_COST,
                "current_credits": credits_available,
            }), 400

        # Always exclude the source contact so they can't show up in their own list.
        if source.get("linkedinUrl"):
            exclusion_keys.add(get_contact_identity({
                "FirstName": source.get("firstName", ""),
                "LastName": source.get("lastName", ""),
                "Email": "",
                "LinkedIn": source.get("linkedinUrl", ""),
                "Company": source.get("company", ""),
            }))

        title_band = _title_bands(source.get("title") or "")
        source_company = source.get("company") or ""

        # ----- Query 1: same company, no title filter -----
        same_co_results: list[dict] = []
        try:
            sc_contacts, _, _, _ = engine_search_contacts(
                _build_same_company_prompt(source),
                _PDL_FETCH_PER_QUERY,
                exclude_keys=exclusion_keys,
                user_profile=user_data,
                source="find_similar",
            )
            same_co_results = sc_contacts or []
        except Exception as engine_err:
            print(f"[find-similar] same-company engine call failed: {engine_err}")

        # Classify same-company results into A (same role) vs B (any other role).
        bucket_a: list[dict] = []
        bucket_b: list[dict] = []
        for c in same_co_results:
            if _matches_title_band(c.get("Title") or "", title_band):
                bucket_a.append(c)
            else:
                bucket_b.append(c)

        # ----- Query 2: cross-company, same title band -----
        cross_co_results: list[dict] = []
        if title_band:
            try:
                cc_contacts, _, _, _ = engine_search_contacts(
                    _build_cross_company_prompt(source),
                    _PDL_FETCH_PER_QUERY + 2,  # extra buffer — we'll drop same-co
                    exclude_keys=exclusion_keys,
                    user_profile=user_data,
                    source="find_similar",
                )
                cross_co_results = cc_contacts or []
            except Exception as engine_err:
                print(f"[find-similar] cross-company engine call failed: {engine_err}")

        # Bucket C — drop any PDL still surfaced at the source company.
        bucket_c: list[dict] = [c for c in cross_co_results if not _same_company(c, source_company)]

        assembled = _assemble_with_backfill(bucket_a, bucket_b, bucket_c)

        shaped = [_shape_for_popup(contact, category) for category, contact in assembled]

        credits_used = 0
        credits_remaining = credits_available
        if shaped and db and user_id:
            try:
                success, remaining = deduct_credits_atomic(
                    user_id, SIMILAR_CREDIT_COST, "find_similar_contacts"
                )
                if success:
                    credits_used = SIMILAR_CREDIT_COST
                credits_remaining = remaining
            except Exception as credit_err:
                print(f"⚠️ find-similar: credit deduction error for {user_id}: {credit_err}")

        return jsonify({
            "contacts": shaped,
            "cap": POPUP_SIMILAR_CAP,
            "credits_used": credits_used,
            "credits_remaining": credits_remaining,
        }), 200

    except Exception as e:
        print(f"⚠️ find-similar: unhandled error: {e}")
        traceback.print_exc()
        return jsonify({"error": "internal_error", "message": "Something went wrong."}), 500


# ---------------------------------------------------------------------------
# Company people (2026-09-07): the Chrome extension's people-first door.
#
# On a LinkedIn company page the side panel lists people at that company
# BEFORE anyone pays for an email; each row then offers Find email (the
# existing import-linkedin path, charged only when an address lands). Same
# engine door as every other surface: warehouse first, Coresignal for the
# remainder, every vendor fill written back to the shelf. Browsing costs the
# student zero credits.
# ---------------------------------------------------------------------------

COMPANY_PEOPLE_CAP = 10


@runs_similar_bp.route("/contacts/company-people", methods=["POST"])
@require_firebase_auth
def company_people():
    """Return up to COMPANY_PEOPLE_CAP people at one company, no email pass.

    Request body:  { "company": str, "limit"?: int, "role"?: str }
    Response:      { "contacts": [row, ...], "cap": int, "credits_used": 0,
                     "credits_remaining": int }
    Rows use the same shape as find-similar so the panel renders them with
    the same code (firstName, lastName, title, company, linkedinUrl, school,
    email when the warehouse already holds a verified one).
    """
    try:
        user_id = request.firebase_user["uid"]
        db = get_db()

        payload = request.get_json(silent=True) or {}
        company = str(payload.get("company") or "").strip()
        role = str(payload.get("role") or "").strip()
        if not company:
            return jsonify({
                "error": "missing_company",
                "message": "A company name is required.",
            }), 400
        try:
            cap = int(payload.get("limit") or COMPANY_PEOPLE_CAP)
        except (TypeError, ValueError):
            cap = COMPANY_PEOPLE_CAP
        cap = max(1, min(cap, COMPANY_PEOPLE_CAP))

        user_data = None
        credits_available = None
        exclusion_keys: set = set()
        if db and user_id:
            try:
                user_ref = db.collection("users").document(user_id)
                user_doc = user_ref.get()
                if user_doc.exists:
                    user_data = user_doc.to_dict()
                    credits_available = check_and_reset_credits(user_ref, user_data)
                    exclusion_data = _build_exclusion_data_from_firestore(db, user_id)
                    exclusion_keys = set(exclusion_data["identity_set"])
            except Exception as e:
                print(f"[company-people] failed to load user profile for {user_id}: {e}")

        # Same alias map the paid path uses (McKinsey, Goldman, etc). Rule 1
        # from the app: no location means the US, not the planet.
        try:
            from app.services.pdl_client import clean_company_name
            canonical = clean_company_name(company) or company
        except Exception:
            canonical = company
        parsed = {
            "companies": [{"name": canonical}],
            "title_variations": [role] if role else [],
            "locations": ["United States"],
        }

        contacts, _rl, _saved, meta = engine_search_contacts(
            parsed, cap, exclude_keys=exclusion_keys, user_profile=user_data,
            source="extension_company",
        )
        rows = []
        for c in contacts or []:
            first = (c.get("FirstName") or "").strip()
            last = (c.get("LastName") or "").strip()
            if not (first or last):
                continue
            rows.append(_shape_for_popup(c, CAT_ANY_ROLE_SAME_CO))
            if len(rows) >= cap:
                break

        return jsonify({
            "contacts": rows,
            "company": company,
            "cap": COMPANY_PEOPLE_CAP,
            "credits_used": 0,
            "credits_remaining": credits_available,
            "provider": (meta or {}).get("provider"),
        }), 200
    except Exception as e:
        print(f"[company-people] unhandled error: {e}")
        return jsonify({"error": "Could not load people at this company. Please try again."}), 500
