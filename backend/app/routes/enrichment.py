"""
Enrichment routes - autocomplete, job title enrichment, and LinkedIn profile enrichment
"""
import logging
import os
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify
import traceback

logger = logging.getLogger(__name__)

from app.extensions import require_firebase_auth, get_db
from app.services.feature_flags import PDL_OUTAGE_ACTIVE
from app.services.pdl_client import get_autocomplete_suggestions, enrich_job_title_with_pdl
from app.services.resume_parser import extract_text_from_file
from app.services.resume_capabilities import is_valid_resume_file, get_file_extension
from app.utils.users import parse_resume_info
from app.utils.linkedin_enrichment import (
    normalize_linkedin_url,
    enrich_linkedin_with_fallback,
    get_enrichment_tiers,
    llm_enrich_profile,
    merge_linkedin_into_resume_parsed,
)

enrichment_bp = Blueprint('enrichment', __name__, url_prefix='/api')


@enrichment_bp.route('/autocomplete/<data_type>', methods=['GET'])
@require_firebase_auth
def autocomplete_api(data_type):
    """Enhanced API endpoint for frontend autocomplete with better error handling"""
    if PDL_OUTAGE_ACTIVE:
        return jsonify({"error": "service_unavailable", "message": "Autocomplete temporarily unavailable.", "code": "PDL_OUTAGE", "suggestions": []}), 503
    try:
        query = request.args.get('query', '').strip()
        
        if not query or len(query) < 2:
            return jsonify({
                'suggestions': [],
                'query': query,
                'data_type': data_type
            })
        
        valid_types = ['job_title', 'company', 'location', 'school', 'skill', 'industry', 'role', 'sub_role']
        if data_type not in valid_types:
            return jsonify({
                'error': f'Invalid data type. Must be one of: {", ".join(valid_types)}',
                'suggestions': []
            }), 400
        
        print(f"Autocomplete request: {data_type} - '{query}'")
        
        suggestions = get_autocomplete_suggestions(query, data_type)
        
        clean_suggestions = []
        for suggestion in suggestions[:10]:
            if isinstance(suggestion, dict) and 'name' in suggestion:
                # Handle PDL's response format: {'name': 'value', 'count': 123}
                clean_suggestions.append(suggestion['name'])
            elif isinstance(suggestion, str) and suggestion.strip():
                clean_suggestions.append(suggestion.strip())
        
        # For location autocomplete, add "United States" if query matches US-related terms
        if data_type == 'location':
            query_lower = query.lower().strip()
            us_keywords = ['united', 'usa', 'us', 'america']
            if any(keyword in query_lower for keyword in us_keywords):
                # Add "United States" at the beginning if not already in suggestions
                if 'United States' not in clean_suggestions:
                    clean_suggestions.insert(0, 'United States')
                # Also ensure it appears even if query is very short
            elif len(query_lower) <= 2 and query_lower in ['us', 'u']:
                if 'United States' not in clean_suggestions:
                    clean_suggestions.insert(0, 'United States')
        
        return jsonify({
            'suggestions': clean_suggestions,
            'query': query,
            'data_type': data_type,
            'count': len(clean_suggestions)
        })
        
    except Exception as e:
        print(f"Autocomplete API error for {data_type} - '{query}': {e}")
        traceback.print_exc()
        
        return jsonify({
            'error': 'Failed to fetch suggestions',
            'suggestions': [],
            'query': query,
            'data_type': data_type
        }), 500


@enrichment_bp.route('/enrich-job-title', methods=['POST'])
@require_firebase_auth
def enrich_job_title_api():
    """API endpoint for job title enrichment"""
    if PDL_OUTAGE_ACTIVE:
        return jsonify({"error": "service_unavailable", "message": "Enrichment temporarily unavailable.", "code": "PDL_OUTAGE"}), 503
    try:
        data = request.json
        job_title = data.get('jobTitle', '').strip()
        
        if not job_title:
            return jsonify({'error': 'Job title is required'}), 400
        
        enrichment = enrich_job_title_with_pdl(job_title)
        
        return jsonify({
            'original': job_title,
            'enrichment': enrichment
        })
        
    except Exception as e:
        print(f"Job title enrichment API error: {e}")
        return jsonify({'error': str(e)}), 500


@enrichment_bp.route('/enrich-linkedin-onboarding', methods=['POST'])
@require_firebase_auth
def enrich_linkedin_for_onboarding():
    """
    Enrich a LinkedIn profile for onboarding personalization.

    Normal mode: { "linkedin_url": "..." }
      - Enriches via PDL → Bright Data fallback
      - Structures via LLM into resumeParsed format
      - Writes to Firestore, returns academics for auto-fill

    Merge-only mode: { "merge_only": true }
      - Re-merges stored linkedinResumeParsed into current resumeParsed
      - Used after resume upload to backfill LinkedIn-only fields
    """
    try:
        uid = request.firebase_user['uid']
        db = get_db()
        user_ref = db.collection('users').document(uid)
        data = request.get_json() or {}

        # ── Merge-only mode ─────────────────────────────────────────
        if data.get('merge_only'):
            return _handle_merge_only(user_ref)

        # ── Normal enrichment mode ──────────────────────────────────
        linkedin_url = data.get('linkedin_url', '').strip()
        if not linkedin_url:
            return jsonify({'success': False, 'enriched': False, 'error': 'linkedin_url is required'}), 200

        normalized = normalize_linkedin_url(linkedin_url)
        if not normalized:
            return jsonify({'success': False, 'enriched': False, 'error': 'Invalid LinkedIn URL'}), 200

        # Check for cached enrichment (per-user, keyed by uid via user_ref)
        # Only return cache if the stored URL matches the requested URL.
        # This prevents returning stale data if the user changes their LinkedIn URL.
        # If Firestore is transiently unreachable, skip the cache check and proceed
        # to a fresh enrichment rather than failing the entire request.
        user_data = {}
        try:
            user_doc = user_ref.get(timeout=8.0)
            user_data = user_doc.to_dict() if user_doc.exists else {}
        except Exception as cache_err:
            logger.warning(
                f"[LinkedIn Enrich] Firestore cache read failed (proceeding with fresh enrichment): {cache_err}"
            )

        if user_data.get('linkedinEnrichmentData') and user_data.get('linkedinUrl') == normalized:
            cached_parsed = user_data.get('linkedinResumeParsed', {})
            edu = cached_parsed.get('education', {}) if isinstance(cached_parsed, dict) else {}
            contact = cached_parsed.get('contact', {}) if isinstance(cached_parsed, dict) else {}
            name = cached_parsed.get('name', '') if isinstance(cached_parsed, dict) else ''
            name_parts = (name or '').strip().split(' ', 1)
            return jsonify({
                'success': True,
                'enriched': True,
                'cached': True,
                'profile': {
                    'firstName': name_parts[0] if name_parts else '',
                    'lastName': name_parts[1] if len(name_parts) > 1 else '',
                    'email': contact.get('email'),
                    'phone': contact.get('phone'),
                },
                'academics': {
                    'university': edu.get('university'),
                    'major': edu.get('major'),
                    'degree': edu.get('degree'),
                    'graduationYear': edu.get('graduation'),
                },
            })

        # Loop through scrape-first tiers, running LLM structuring on each
        # tier's output and falling through if the structured result lacks a
        # usable name. Chain is [Apify -> PDL] for user-LinkedIn onboarding.
        raw_data = None
        source = ""
        linkedin_parsed = None
        attempted_sources = []

        for tier in get_enrichment_tiers(prefer_scrape=True):
            try:
                tier_data, tier_source = tier(normalized)
            except Exception as tier_err:
                logger.warning(f"[LinkedIn Enrich] Tier raised: {tier_err}")
                continue
            if not tier_data:
                continue

            attempted_sources.append(tier_source)
            structured = llm_enrich_profile(tier_data, tier_source)
            if structured and structured.get('name'):
                raw_data = tier_data
                source = tier_source
                linkedin_parsed = structured
                break

            logger.info(
                f"[LinkedIn Enrich] {tier_source} returned data but LLM extracted no name "
                f"(likely login wall / blocked); trying next tier"
            )

        # D9: Apify is always the intended first tier. If a later tier won,
        # tag the stored source so the frontend can show the fallback toast
        # and observability can count fallbacks.
        fallback_used = bool(source and source != "apify")
        if fallback_used:
            source = f"{source}-fallback"

        if not raw_data or not linkedin_parsed:
            tried = ', '.join(attempted_sources) if attempted_sources else 'no sources'
            return jsonify({
                'success': False,
                'enriched': False,
                'error': (
                    'Could not extract profile data — LinkedIn may be blocking automated access '
                    f'for this profile (tried: {tried}).'
                ),
            }), 200

        # Write enrichment metadata to Firestore
        enrichment_update = {
            'linkedinUrl': normalized,
            'linkedinEnrichmentData': raw_data,
            'linkedinEnrichmentSource': source,
            'linkedinEnrichedAt': datetime.now(timezone.utc).isoformat(),
            'linkedinResumeParsed': linkedin_parsed,
        }

        # Determine whether to write or merge resumeParsed
        existing_parsed = user_data.get('resumeParsed')
        if existing_parsed and isinstance(existing_parsed, dict) and existing_parsed.get('name'):
            # Merge: resume is primary, LinkedIn supplements
            merged = merge_linkedin_into_resume_parsed(existing_parsed, linkedin_parsed)
            enrichment_update['resumeParsed'] = merged
        else:
            # No resume — LinkedIn becomes the resumeParsed
            enrichment_update['resumeParsed'] = linkedin_parsed

        try:
            user_ref.set(enrichment_update, merge=True, timeout=15.0)
            # LinkedIn instead of (or on top of) a resume: same head start
            # on the People deck as a resume upload.
            try:
                from app.services.people_deck_cache import schedule_build
                schedule_build(uid, "linkedin")
            except Exception:
                logger.debug("[LinkedIn Enrich] people deck build not queued")
        except Exception as write_err:
            logger.error(f"[LinkedIn Enrich] Firestore write failed: {write_err}")
            return jsonify({
                'success': False,
                'enriched': False,
                'error': 'Enrichment fetched but could not save — please retry.',
            }), 200

        # Extract profile + academics for frontend auto-fill
        edu = linkedin_parsed.get('education', {}) if isinstance(linkedin_parsed, dict) else {}
        contact = linkedin_parsed.get('contact', {}) if isinstance(linkedin_parsed, dict) else {}
        name = linkedin_parsed.get('name', '') if isinstance(linkedin_parsed, dict) else ''
        name_parts = (name or '').strip().split(' ', 1)
        return jsonify({
            'success': True,
            'enriched': True,
            'cached': False,
            'source': source,
            # D9: surfaced so the frontend can show the "LinkedIn enrichment
            # using fallback - we'll refresh later" toast when Apify failed
            # but PDL caught us. Quiet UX, no user action required.
            'fallback_used': fallback_used,
            'profile': {
                'firstName': name_parts[0] if name_parts else '',
                'lastName': name_parts[1] if len(name_parts) > 1 else '',
                'email': contact.get('email'),
                'phone': contact.get('phone'),
            },
            'academics': {
                'university': edu.get('university'),
                'major': edu.get('major'),
                'degree': edu.get('degree'),
                'graduationYear': edu.get('graduation'),
            },
        })

    except Exception as e:
        print(f"[LinkedIn Enrich] Unhandled error for uid={request.firebase_user.get('uid', '?')}: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'enriched': False, 'error': 'Internal error'}), 200


# ── D11: Lazy-on-login Apify backfill ─────────────────────────────────────────

# Minimum gap between attempted backfills for the same user. Prevents the
# client (which calls the endpoint on every app boot) from thundering on
# Apify when an attempt has just failed. 24h is long enough to ride out most
# transient Apify outages and short enough to recover within one user-day.
_BACKFILL_RETRY_COOLDOWN_S = 24 * 60 * 60


def _user_needs_apify_backfill(user_data: dict, now_ts: float) -> tuple[bool, str]:
    """Decide whether a user should be backfilled to Apify-sourced LinkedIn.

    Returns (should_backfill, reason_when_skipped). Pure function so the
    decision logic is testable without Firestore.

    Skip reasons:
      "already_apify"    - source is already 'apify' or backfill flag is set
      "no_url"           - user has no LinkedIn URL on file to scrape
      "recent_attempt"   - last backfill attempt within cooldown window
    """
    # Source-of-truth: explicit flag wins.
    if user_data.get("apifyBackfilled") is True:
        return False, "already_apify"

    # If the user was just onboarded post-Apify-flip, the source is already
    # right and no backfill is needed. We don't set the flag in onboarding
    # itself - we set it here on first observation - to keep the onboarding
    # write path narrow.
    if user_data.get("linkedinEnrichmentSource") == "apify":
        return False, "already_apify"

    if not user_data.get("linkedinUrl"):
        return False, "no_url"

    last_attempt_iso = user_data.get("apifyBackfillLastAttempt")
    if isinstance(last_attempt_iso, str) and last_attempt_iso:
        try:
            last_dt = datetime.fromisoformat(last_attempt_iso.replace("Z", "+00:00"))
            if (now_ts - last_dt.timestamp()) < _BACKFILL_RETRY_COOLDOWN_S:
                return False, "recent_attempt"
        except (ValueError, TypeError):
            # Malformed timestamp - treat as no prior attempt.
            pass

    return True, ""


def _run_apify_backfill(uid: str, linkedin_url: str) -> None:
    """Background worker: re-scrape one user's LinkedIn via Apify and write.

    Always writes the last-attempt timestamp so a failure does not get
    retried immediately. On success, sets apifyBackfilled and overwrites
    linkedinResumeParsed + linkedinEnrichmentSource. The user's existing
    resumeParsed is left untouched unless they have no resume at all - we
    are upgrading the LinkedIn data source, not blowing away an uploaded
    resume.
    """
    db = get_db()
    user_ref = db.collection("users").document(uid)
    now_iso = datetime.now(timezone.utc).isoformat()

    try:
        # Lazy import keeps the test isolation simple.
        try:
            from app.services.apify_client import enrich_user_linkedin_profile_via_apify
        except ImportError:
            from backend.app.services.apify_client import enrich_user_linkedin_profile_via_apify

        envelope = enrich_user_linkedin_profile_via_apify(linkedin_url)
        if not envelope.get("ok") or not envelope.get("data"):
            logger.info(
                f"[Apify Backfill] {uid}: Apify did not return usable data "
                f"(error={envelope.get('error')}); marking attempt and stopping"
            )
            user_ref.set(
                {"apifyBackfillLastAttempt": now_iso},
                merge=True,
            )
            return

        structured = llm_enrich_profile(envelope["data"], "apify")
        if not structured or not structured.get("name"):
            logger.info(
                f"[Apify Backfill] {uid}: LLM extracted no name from Apify data; "
                "marking attempt and stopping"
            )
            user_ref.set(
                {"apifyBackfillLastAttempt": now_iso},
                merge=True,
            )
            return

        # Build the update. linkedinResumeParsed is fully owned by enrichment;
        # resumeParsed is merged only when the user has none of their own.
        updates = {
            "linkedinEnrichmentData": envelope["data"],
            "linkedinEnrichmentSource": "apify",
            "linkedinEnrichedAt": now_iso,
            "linkedinResumeParsed": structured,
            "apifyBackfilled": True,
            "apifyBackfilledAt": now_iso,
            "apifyBackfillLastAttempt": now_iso,
        }
        try:
            snap = user_ref.get(timeout=8.0)
            existing = snap.to_dict() if snap.exists else {}
        except Exception:
            existing = {}
        existing_parsed = existing.get("resumeParsed")
        if not (
            existing_parsed
            and isinstance(existing_parsed, dict)
            and existing_parsed.get("name")
        ):
            updates["resumeParsed"] = structured

        user_ref.set(updates, merge=True)
        logger.info(f"[Apify Backfill] {uid}: backfill complete (source=apify)")
    except Exception as e:
        logger.error(f"[Apify Backfill] {uid}: unexpected error: {e}", exc_info=True)
        try:
            user_ref.set(
                {"apifyBackfillLastAttempt": now_iso},
                merge=True,
            )
        except Exception:
            pass


@enrichment_bp.route('/users/me/sync-linkedin', methods=['POST'])
@require_firebase_auth
def sync_linkedin_via_apify():
    """D11 lazy-on-login backfill: kick the Apify scrape off the critical path.

    Frontend calls this on app boot after Firebase auth resolves. The endpoint
    decides whether a backfill is needed (flag on, not already Apify, has URL,
    not in cooldown) and if so, returns 202 with status='scheduled' immediately
    while a background thread does the actual Apify call + Firestore write.

    Idempotent: re-calling after backfill completes returns status='skipped'
    with reason='already_apify' and no work happens. The endpoint never fails
    user-visible — onboarding and login must keep working even if Apify is
    down.
    """
    try:
        uid = request.firebase_user['uid']
        db = get_db()
        user_ref = db.collection('users').document(uid)
        try:
            snap = user_ref.get(timeout=8.0)
            user_data = snap.to_dict() if snap.exists else {}
        except Exception as read_err:
            logger.warning(
                f"[Apify Backfill] {uid}: Firestore read failed: {read_err}; skipping"
            )
            return jsonify({"status": "skipped", "reason": "read_error"}), 200

        now_ts = datetime.now(timezone.utc).timestamp()
        should_backfill, skip_reason = _user_needs_apify_backfill(user_data, now_ts)
        if not should_backfill:
            return jsonify({"status": "skipped", "reason": skip_reason}), 200

        # Fire-and-forget background thread. We do NOT block the response on
        # the Apify call (60s timeout would tank app-boot latency).
        import threading
        threading.Thread(
            target=_run_apify_backfill,
            args=(uid, user_data["linkedinUrl"]),
            daemon=True,
            name=f"apify-backfill-{uid[:8]}",
        ).start()
        return jsonify({"status": "scheduled"}), 202
    except Exception as e:
        logger.error(f"[Apify Backfill] Unhandled error: {e}", exc_info=True)
        # Never user-visible: sync is best-effort.
        return jsonify({"status": "skipped", "reason": "internal_error"}), 200


def _handle_merge_only(user_ref):
    """Re-merge stored LinkedIn data into current resumeParsed."""
    try:
        user_doc = user_ref.get()
        if not user_doc.exists:
            return jsonify({'success': False, 'merged': False, 'error': 'User not found'}), 200

        user_data = user_doc.to_dict()
        linkedin_parsed = user_data.get('linkedinResumeParsed')
        existing_parsed = user_data.get('resumeParsed')

        if not linkedin_parsed or not isinstance(linkedin_parsed, dict):
            return jsonify({'success': False, 'merged': False, 'error': 'No LinkedIn data to merge'}), 200

        if not existing_parsed or not isinstance(existing_parsed, dict):
            # No resume yet — just write LinkedIn as resumeParsed
            user_ref.set({'resumeParsed': linkedin_parsed}, merge=True)
            return jsonify({'success': True, 'merged': True})

        merged = merge_linkedin_into_resume_parsed(existing_parsed, linkedin_parsed)
        user_ref.set({'resumeParsed': merged}, merge=True)
        return jsonify({'success': True, 'merged': True})

    except Exception as e:
        print(f"[LinkedIn Merge] Error: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'merged': False, 'error': 'Merge failed'}), 200


# ── School hometown lookup ──────────────────────────────────────────────────
#
# The frontend's static SCHOOL_HOMETOWN_LOCATION map only covers ~30 elite-tier
# schools. For everything else — international schools (Bocconi, LSE, HEC),
# regional US schools (Louisville, Ball State, Adams State, etc.), community
# colleges — we ask an LLM where the school's primary campus is and cache
# the result in Firestore so it only costs us one OpenAI call ever.

@enrichment_bp.route('/school/lookup', methods=['GET'])
@require_firebase_auth
def school_lookup():
    """Resolve a school name → primary campus location. Cached forever."""
    import json as _json
    import re as _re_inner

    school = (request.args.get('school') or '').strip()
    if not school:
        return jsonify({'error': 'school param required'}), 400
    if len(school) > 200:
        school = school[:200]

    cache_key = _re_inner.sub(r'[^a-z0-9]+', '_', school.lower()).strip('_')[:128]
    if not cache_key:
        return jsonify({'error': 'invalid school name'}), 400

    db = get_db()
    cache_ref = db.collection('school_hometown_cache').document(cache_key)

    # Cache hit
    try:
        snap = cache_ref.get(timeout=6.0)
        if snap.exists:
            data = snap.to_dict() or {}
            if data.get('formatted'):
                return jsonify(data)
    except Exception as cache_err:
        logger.warning(f"[School Lookup] cache read failed: {cache_err}")

    # Cache miss → LLM lookup
    try:
        try:
            from app.services.openai_client import client as openai_client
        except ImportError:
            from backend.app.services.openai_client import client as openai_client
        if not openai_client:
            return jsonify({'error': 'LLM unavailable'}), 503

        prompt = (
            "You are a geography lookup. Return JSON with the primary campus "
            "location of the university or school named below.\n\n"
            "Schema: {\"city\": \"City Name\", \"state_or_region\": \"State or Region (US: full state name; international: leave empty if not applicable)\", \"country\": \"Country\", \"valid\": true/false}\n\n"
            "Rules:\n"
            "- If the school is real, set valid=true.\n"
            "- If you cannot identify the school with high confidence, set valid=false and leave fields empty.\n"
            "- For US schools, use full state name (\"California\", not \"CA\").\n"
            "- For international, leave state_or_region empty unless the school is in a clearly named region (\"Lombardy\", \"Ontario\").\n"
            "- Output ONLY the JSON object.\n\n"
            f"School: {school}"
        )
        response = openai_client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "system", "content": prompt}],
            temperature=0.0,
            max_tokens=200,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content.strip()
        parsed = _json.loads(content)

        if not parsed.get('valid') or not parsed.get('city'):
            result = {'valid': False, 'formatted': '', 'city': '', 'state_or_region': '', 'country': ''}
        else:
            city = (parsed.get('city') or '').strip()
            country = (parsed.get('country') or '').strip()
            state = (parsed.get('state_or_region') or '').strip()
            if country and country.lower() in ('united states', 'usa', 'us'):
                # US format: "City, ST"
                state_abbr = _state_abbr(state)
                formatted = f"{city}, {state_abbr}" if state_abbr else city
            elif country:
                formatted = f"{city}, {country}"
            else:
                formatted = city
            result = {
                'valid': True,
                'formatted': formatted,
                'city': city,
                'state_or_region': state,
                'country': country,
            }

        try:
            cache_ref.set(result, merge=True, timeout=8.0)
        except Exception as cache_err:
            logger.warning(f"[School Lookup] cache write failed: {cache_err}")

        return jsonify(result)

    except Exception as e:
        logger.error(f"[School Lookup] {e}")
        return jsonify({'error': str(e), 'valid': False, 'formatted': ''}), 200


_STATE_ABBR_MAP = {
    'alabama': 'AL', 'alaska': 'AK', 'arizona': 'AZ', 'arkansas': 'AR',
    'california': 'CA', 'colorado': 'CO', 'connecticut': 'CT', 'delaware': 'DE',
    'florida': 'FL', 'georgia': 'GA', 'hawaii': 'HI', 'idaho': 'ID',
    'illinois': 'IL', 'indiana': 'IN', 'iowa': 'IA', 'kansas': 'KS',
    'kentucky': 'KY', 'louisiana': 'LA', 'maine': 'ME', 'maryland': 'MD',
    'massachusetts': 'MA', 'michigan': 'MI', 'minnesota': 'MN', 'mississippi': 'MS',
    'missouri': 'MO', 'montana': 'MT', 'nebraska': 'NE', 'nevada': 'NV',
    'new hampshire': 'NH', 'new jersey': 'NJ', 'new mexico': 'NM', 'new york': 'NY',
    'north carolina': 'NC', 'north dakota': 'ND', 'ohio': 'OH', 'oklahoma': 'OK',
    'oregon': 'OR', 'pennsylvania': 'PA', 'rhode island': 'RI', 'south carolina': 'SC',
    'south dakota': 'SD', 'tennessee': 'TN', 'texas': 'TX', 'utah': 'UT',
    'vermont': 'VT', 'virginia': 'VA', 'washington': 'WA', 'west virginia': 'WV',
    'wisconsin': 'WI', 'wyoming': 'WY', 'district of columbia': 'DC',
}


def _state_abbr(state: str) -> str:
    if not state:
        return ''
    return _STATE_ABBR_MAP.get(state.strip().lower(), state.strip())


# ── School detection from free-text prompt ──────────────────────────────────
#
# Frontend SCHOOL_ALIASES has elite tier (USC, NYU, MIT...). universities.ts
# has 1019 US entries — missing University of Louisville, Bocconi, LSE, IE,
# HEC, IIT Bombay, and ~thousands of others.
#
# This endpoint asks an LLM "did the user mention a school here?", handles
# typos ("louiville" → University of Louisville), accepts international and
# regional schools, and returns canonical name + city in one round-trip.
# Cached by prompt-hash in Firestore so repeated queries don't re-spend.

@enrichment_bp.route('/search/detect-school', methods=['POST'])
@require_firebase_auth
def detect_school():
    """LLM-backed school detection for prompts our static lexicon misses."""
    import json as _json
    import hashlib as _hashlib

    payload = request.get_json() or {}
    prompt = (payload.get('prompt') or '').strip()
    if len(prompt) < 8 or len(prompt) > 500:
        return jsonify({'detected': False})

    cache_key = _hashlib.sha256(prompt.lower().encode('utf-8')).hexdigest()[:32]
    db = get_db()
    cache_ref = db.collection('school_detect_cache').document(cache_key)

    try:
        snap = cache_ref.get(timeout=6.0)
        if snap.exists:
            return jsonify(snap.to_dict() or {'detected': False})
    except Exception as cache_err:
        logger.warning(f"[Detect School] cache read failed: {cache_err}")

    try:
        try:
            from app.services.openai_client import client as openai_client
        except ImportError:
            from backend.app.services.openai_client import client as openai_client
        if not openai_client:
            return jsonify({'detected': False})

        system_prompt = (
            "Identify if the user's networking-search prompt mentions a specific "
            "university, college, or school. Tolerate typos (louiville → "
            "University of Louisville). Tolerate abbreviations the user might use "
            "(u of l, ttu, asu, etc.). Recognize international schools (Bocconi, "
            "LSE, IE Business School, HEC Paris, IIT Bombay, NUS, etc.).\n\n"
            "Output JSON ONLY:\n"
            "{\n"
            "  \"detected\": true|false,\n"
            "  \"school\": \"Canonical full school name\" or null,\n"
            "  \"matched\": \"the substring from the user's prompt that suggested this school\" or null,\n"
            "  \"city\": \"Primary campus city\" or null,\n"
            "  \"state_or_region\": \"US state full name; or international region (empty if none)\" or null,\n"
            "  \"country\": \"Country\" or null\n"
            "}\n\n"
            "Rules:\n"
            "- detected=false unless you're confident a school is named.\n"
            "- For US schools: use full state name; for international, leave state_or_region empty unless explicit.\n"
            "- Output ONLY the JSON object.\n"
        )
        user_msg = f"Prompt: {prompt}"

        response = openai_client.chat.completions.create(
            model='gpt-4o-mini',
            messages=[
                {'role': 'system', 'content': system_prompt},
                {'role': 'user', 'content': user_msg},
            ],
            temperature=0.0,
            max_tokens=250,
            response_format={'type': 'json_object'},
        )
        parsed = _json.loads(response.choices[0].message.content.strip())

        if not parsed.get('detected') or not parsed.get('school'):
            result = {'detected': False, 'formatted': '', 'school': '', 'matched': ''}
        else:
            city = (parsed.get('city') or '').strip()
            country = (parsed.get('country') or '').strip()
            state = (parsed.get('state_or_region') or '').strip()
            if country and country.lower() in ('united states', 'usa', 'us'):
                state_abbr = _state_abbr(state)
                formatted = f"{city}, {state_abbr}" if (city and state_abbr) else city
            elif country and city:
                formatted = f"{city}, {country}"
            else:
                formatted = city or ''
            result = {
                'detected': True,
                'school': (parsed.get('school') or '').strip(),
                'matched': (parsed.get('matched') or '').strip(),
                'city': city,
                'state_or_region': state,
                'country': country,
                'formatted': formatted,
            }

        try:
            cache_ref.set(result, merge=True, timeout=8.0)
        except Exception as cache_err:
            logger.warning(f"[Detect School] cache write failed: {cache_err}")

        return jsonify(result)

    except Exception as e:
        logger.error(f"[Detect School] {e}")
        return jsonify({'detected': False, 'error': str(e)}), 200


# ── Direction extraction ────────────────────────────────────────────────────
#
# Takes the user's free-text career narrative ("I'm good with numbers but hate
# spreadsheets, like talking to people…") and returns structured chips:
# industries, roles, firms, locations, and recruiting cycle. The chips become
# the receipt the user sees and edits below the textarea on the Profile page.
#
# Conservative by design — better to leave a slot empty than hallucinate.

DIRECTION_EXTRACTION_PROMPT = """You are a career-direction interpreter for college students recruiting for internships and full-time roles. Read their narrative and produce a complete, opinionated picture of where they're aimed.

YOUR JOB IS TO INTERPRET, NOT JUST EXTRACT.

The student is describing their strengths, interests, and preferences in plain English. Combine stated AND implied signals. Don't just match surface keywords — synthesize.

EXAMPLES OF SYNTHESIS:
- "Good with people" + "finance experience" + "startup vibe" → FinTech, Venture Capital, Sales / BD, Tech — Startup; roles: Growth Analyst, Business Development Analyst, Product Manager, VC Analyst, Customer Success Associate.
- "Numbers but hate spreadsheets" + "talking to people" → Sales / BD, Tech — Startup, Marketing, Consulting (Strategy); roles: Account Executive, Sales Development Representative, Strategy Analyst, Customer Success Associate, Growth Analyst.
- "Want to ship fast" + "small team" + "AI" → Tech — Startup, AI / ML, Developer Tools; roles: Product Engineer, Forward-Deployed Engineer, Software Engineer, ML Engineer, Founding Engineer.
- "Math major, intellectually rigorous, like markets" → Quant Trading, Hedge Funds, Investment Banking; roles: Quant Researcher, Trading Analyst, Equity Research Analyst, Investment Banking Analyst.

OUTPUT QUALITY BAR:
- 3–5 industries — broad enough to give the student a real picture, specific enough to be useful. Cover plausible adjacent fits, not just one literal match.
- 4–6 specific entry-level / new-grad role titles — concrete job titles a student would search for, e.g., "Investment Banking Analyst" not "finance role". Mix conventional and adjacent roles.
- An empty list is acceptable ONLY if the narrative truly contains no signal in that dimension (e.g., student didn't mention any firms by name → firms: []).
- Bias toward expanding the student's view: if they say "BD at a tech company", also consider Account Executive, Customer Success Associate, Growth Analyst — the related roles they may not have named.

CRITICAL RULES:
1. Output ONLY valid JSON. No markdown, no preamble, no explanation.
2. Industries MUST come from this list (use exact strings):
   ["Investment Banking", "Consulting (MBB)", "Consulting (Big 4)", "Consulting (Strategy)",
    "Tech — Big", "Tech — Startup", "Private Equity", "Venture Capital", "Hedge Funds",
    "Quant Trading", "FinTech", "Healthcare", "AI / ML", "Developer Tools", "Marketing",
    "Sales / BD", "Product Management", "Real Estate", "Energy", "Media & Entertainment",
    "Government / Policy", "Nonprofit"]
3. Roles can be free-form but should be STANDARD job-title strings a student would see on a posting. Don't make up creative titles.
4. Firms — only company names the user explicitly mentioned by name. Don't invent.
5. Locations — only cities/regions the user mentioned. Don't invent.
6. recruitingCycle — one of "summer-sa", "fulltime", "off-cycle", "exploring", or null.
7. cycleYear — number only if user mentions a year (e.g., "2027"). Else null.

OUTPUT SCHEMA:
{
  "industries": ["string"],
  "roles": ["string"],
  "firms": ["string"],
  "locations": ["string"],
  "recruitingCycle": "summer-sa | fulltime | off-cycle | exploring | null",
  "cycleYear": null
}

NARRATIVE:
"""


@enrichment_bp.route('/extract-direction', methods=['POST'])
@require_firebase_auth
def extract_direction():
    """LLM-extract industries/roles/firms/locations/cycle from a career narrative."""
    import json as _json
    try:
        try:
            from app.services.openai_client import client as openai_client
        except ImportError:
            from backend.app.services.openai_client import client as openai_client
        if not openai_client:
            return jsonify({'success': False, 'error': 'LLM unavailable'}), 503

        data = request.get_json() or {}
        narrative = (data.get('narrative') or '').strip()
        if len(narrative) < 8:
            return jsonify({'success': False, 'error': 'Narrative too short — write at least a sentence.'}), 400

        response = openai_client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role": "system", "content": DIRECTION_EXTRACTION_PROMPT + narrative}],
            temperature=0.3,
            max_tokens=700,
            response_format={"type": "json_object"},
        )
        content = response.choices[0].message.content.strip()
        extracted = _json.loads(content)

        # Light sanity-clean: strings only, dedupe
        def _strs(v):
            if not isinstance(v, list):
                return []
            seen = set()
            out = []
            for item in v:
                if isinstance(item, str) and item.strip():
                    s = item.strip()
                    if s.lower() not in seen:
                        out.append(s)
                        seen.add(s.lower())
            return out

        clean = {
            'industries': _strs(extracted.get('industries')),
            'roles': _strs(extracted.get('roles')),
            'firms': _strs(extracted.get('firms')),
            'locations': _strs(extracted.get('locations')),
            'recruitingCycle': extracted.get('recruitingCycle') if extracted.get('recruitingCycle') in ('summer-sa', 'fulltime', 'off-cycle', 'exploring') else None,
            'cycleYear': extracted.get('cycleYear') if isinstance(extracted.get('cycleYear'), (int, float)) else None,
        }
        return jsonify({'success': True, 'extracted': clean})

    except Exception as e:
        logger.error(f"[Extract Direction] {e}")
        return jsonify({'success': False, 'error': str(e)}), 500


# ── LinkedIn PDF upload ─────────────────────────────────────────────────────
#
# LinkedIn lets every user export their own profile as a PDF directly from the
# site (More menu → Save to PDF). The PDF is a clean, full-text export of
# everything visible: about, all experience with bullets, education, skills,
# certifications, recommendations, etc. It's the most reliable LinkedIn data
# source we have because it bypasses LinkedIn's anti-scrape defenses entirely
# (the user is exporting their own data with their own session).
#
# We reuse the existing resume parsing pipeline (PDF → text → structured)
# and store the result under LinkedIn-specific fields.

@enrichment_bp.route('/parse-linkedin-pdf', methods=['POST'])
@require_firebase_auth
def parse_linkedin_pdf():
    """Parse a LinkedIn-exported PDF, store under linkedin* fields, merge into resume."""
    try:
        from datetime import datetime as _dt
        from firebase_admin import storage as fb_storage

        uid = request.firebase_user['uid']
        db = get_db()
        user_ref = db.collection('users').document(uid)

        if 'file' not in request.files:
            return jsonify({'success': False, 'error': 'No PDF file provided'}), 400
        file = request.files['file']
        if not file.filename:
            return jsonify({'success': False, 'error': 'No file selected'}), 400
        if not is_valid_resume_file(file.filename, file.mimetype):
            return jsonify({'success': False, 'error': 'Please upload a PDF, DOC, or DOCX file.'}), 400

        ext = get_file_extension(file.filename, file.mimetype)
        text = extract_text_from_file(file, ext)
        if not text or len(text) < 200:
            return jsonify({'success': False, 'error': 'Could not extract text from this PDF.'}), 400

        parsed = parse_resume_info(text)
        if not parsed or not parsed.get('name'):
            return jsonify({
                'success': False,
                'error': 'Could not identify a profile in this PDF. Make sure it is a LinkedIn profile export.',
            }), 400

        # Upload PDF to Storage at resumes/{uid}/linkedin/* (existing storage rule prefix)
        pdf_url = None
        try:
            bucket = fb_storage.bucket()
            blob = bucket.blob(f'resumes/{uid}/linkedin/{int(_dt.now().timestamp())}-{file.filename}')
            file.seek(0)
            blob.upload_from_file(file, content_type='application/pdf')
            blob.make_public()
            pdf_url = blob.public_url
        except Exception as e:
            logger.warning(f"[LinkedIn PDF] Storage upload failed (continuing): {e}")

        # Build update — LinkedIn-specific fields, resumeParsed merged if a resume already exists
        update = {
            'linkedinResumeParsed': parsed,
            'linkedinEnrichmentSource': 'user_pdf',
            'linkedinEnrichedAt': datetime.now(timezone.utc).isoformat(),
            'linkedinPdfUrl': pdf_url or '',
            'linkedinPdfFileName': file.filename,
        }

        # If the PDF surfaced a LinkedIn URL and the user doesn't have one stored, capture it
        try:
            existing_doc = user_ref.get(timeout=8.0)
            existing = existing_doc.to_dict() if existing_doc.exists else {}
        except Exception as e:
            logger.warning(f"[LinkedIn PDF] Pre-read failed: {e}")
            existing = {}

        contact = parsed.get('contact') or {}
        extracted_url = (contact.get('linkedin') or '').strip()
        if extracted_url and not existing.get('linkedinUrl'):
            normalized = extracted_url if extracted_url.startswith('http') else f'https://{extracted_url}'
            update['linkedinUrl'] = normalized

        # Merge the LinkedIn-derived data into resumeParsed (resume primary)
        if isinstance(existing.get('resumeParsed'), dict) and existing['resumeParsed'].get('name'):
            try:
                merged = merge_linkedin_into_resume_parsed(existing['resumeParsed'], parsed)
                update['resumeParsed'] = merged
            except Exception as e:
                logger.warning(f"[LinkedIn PDF] Merge failed: {e}")
        else:
            update['resumeParsed'] = parsed

        try:
            user_ref.set(update, merge=True, timeout=15.0)
        except Exception as write_err:
            logger.error(f"[LinkedIn PDF] Firestore write failed: {write_err}")
            return jsonify({
                'success': False,
                'error': 'Parsed your PDF but could not save — please retry.',
            }), 200

        return jsonify({
            'success': True,
            'enriched': True,
            'source': 'user_pdf',
            'profile': {
                'name': parsed.get('name', ''),
            },
        })

    except Exception as e:
        logger.error(f"[LinkedIn PDF] Unhandled: {e}")
        traceback.print_exc()
        return jsonify({'success': False, 'error': 'Internal error'}), 500


