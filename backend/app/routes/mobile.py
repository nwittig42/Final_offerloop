"""
Mobile app thin API shims (/api/mobile/*).

The Expo app reuses the existing web endpoints directly wherever their shapes
fit; this blueprint adds only the few things that genuinely don't exist as a
single mobile-shaped call. Everything is behind @require_firebase_auth and
reuses existing services, not new business logic.
"""
import calendar
import hashlib
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _FuturesTimeout
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request

# Bounded pool for interactive Scout contact searches. A single PDL search can
# retry 3x at a 30s timeout (fine for background batch drafts, brutal for a live
# Scout ask), so we run it here and enforce a hard wall-clock budget — the orphan
# thread finishes into cache in the background rather than pinning the UI to the
# 40s client watchdog with dead air.
_scout_find_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="scout-find")
_SCOUT_FIND_BUDGET_S = 24

from app.config import TIER_CONFIGS
from app.extensions import get_db, require_firebase_auth
from app.models.users import (
    create_user_data,
    get_structured_school,
    get_structured_target_industries,
)

import logging

from app.config import CREDIT_COSTS
from app.services.credit_ledger import SystemClock, expire_promos, state_from_user_dict

logger = logging.getLogger(__name__)

mobile_bp = Blueprint('mobile', __name__, url_prefix='/api/mobile')

# Anti-burst guard on drafting. Credits are the only meter the user sees, and
# they bound the MONTH — nothing in them stops someone firing thirty cold emails
# in ninety seconds, which is how a sending domain gets burned. The old client
# swipe-cooldown nominally paced this, but it was React state a force-quit reset,
# so it enforced nothing. This is the real pacing, server-side where it can't be
# bypassed. Deliberately loose: a normal session should never touch it.
DRAFT_BURST_MAX = 10               # drafts…
DRAFT_BURST_WINDOW_SECONDS = 600   # …per 10 minutes

# Founder accounts that get a push when new feedback lands. Comma-separated
# emails, env-overridable (FOUNDER_ALERT_EMAILS). Each must be signed into the
# app with notifications on to receive it. Resolved to uids once and cached.
FOUNDER_ALERT_EMAILS = [
    e.strip().lower()
    for e in (os.getenv('FOUNDER_ALERT_EMAILS')
              or 'rylanbohnett@gmail.com,deena.bandi004@gmail.com').split(',')
    if e.strip()
]
_founder_uid_cache: list | None = None


def _founder_alert_uids() -> list:
    """Resolve founder emails -> uids (cached). Best-effort; a lookup miss just
    drops that founder from the alert list."""
    global _founder_uid_cache
    if _founder_uid_cache is not None:
        return _founder_uid_cache
    from firebase_admin import auth as _auth
    out = []
    for em in FOUNDER_ALERT_EMAILS:
        try:
            out.append(_auth.get_user_by_email(em).uid)
        except Exception:
            logger.warning('founder alert: no account for %s', em)
    _founder_uid_cache = out
    return out

# App Store demo account(s), exempt from the anti-burst guard so a reviewer can
# hammer the swipe feature without hitting a 429 mid-review. Overridable via env
# (REVIEWER_EMAILS, comma-separated) if the demo account ever changes.
REVIEWER_EMAILS = {
    e.strip().lower()
    for e in (os.getenv('REVIEWER_EMAILS') or 'applereview@offerloop.ai').split(',')
    if e.strip()
}

PLAN_LABEL = {'free': 'Free', 'pro': 'Pro', 'elite': 'Elite'}


def _ensure_user_doc(db, uid: str, email: str, name: str = '') -> dict:
    """Email-link users sign in purely via Firebase, so their Firestore doc may
    not exist yet on the first authenticated call. Provision it with free-tier
    defaults (mirrors the google-extension path) so /me always has real data."""
    ref = db.collection('users').document(uid)
    snap = ref.get()
    if snap.exists:
        return snap.to_dict() or {}
    tier = 'free'
    cfg = TIER_CONFIGS.get(tier, TIER_CONFIGS['free'])
    data = create_user_data(
        uid=uid,
        email=email,
        tier=tier,
        name=name,
        credits=cfg.get('credits'),
        max_credits=cfg.get('credits'),
    )
    ref.set(data)
    return data


def _gmail_status(db, uid: str, fallback_email: str):
    """Connected iff the integrations/gmail doc exists with an access token."""
    try:
        snap = db.collection('users').document(uid).collection('integrations').document('gmail').get()
        if snap.exists:
            d = snap.to_dict() or {}
            if d.get('token'):
                return True, (d.get('gmailAddress') or fallback_email or '')
    except Exception:
        pass
    return False, ''


def _reset_label() -> str:
    """Credits reset at the calendar month boundary; label the next one."""
    now = datetime.now()
    next_month = now.month % 12 + 1
    return f"Resets {calendar.month_abbr[next_month]} 1"


def _relative_label(iso: str) -> str:
    """Short relative time ('Just now', '2h', '3d') for notification rows."""
    if not iso:
        return ''
    try:
        dt = datetime.fromisoformat(str(iso).replace('Z', '+00:00'))
    except Exception:
        return ''
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    if secs < 60:
        return 'Just now'
    mins = int(secs // 60)
    if mins < 60:
        return f'{mins}m'
    hours = mins // 60
    if hours < 24:
        return f'{hours}h'
    days = hours // 24
    if days < 7:
        return f'{days}d'
    weeks = days // 7
    if weeks < 5:
        return f'{weeks}w'
    try:
        return dt.strftime('%b %-d')
    except Exception:
        return dt.strftime('%b %d')


def _map_notification_item(item: dict) -> dict:
    """Map a stored notifications/outbox item onto the mobile NotificationItem
    shape ({id, kind, text, timeLabel, read, outreachId?}). Reply items deep-
    link to the conversation; loop-run items are informational."""
    is_loop = item.get('kind') == 'loop_run'
    snippet = (item.get('snippet') or '').strip()
    ts = item.get('timestamp') or ''
    read = bool(item.get('read'))

    if item.get('kind') == 'auto_apply_attention':
        company = item.get('company') or 'A job'
        count = int(item.get('count') or 0)
        qword = 'question' if count == 1 else 'questions'
        aid = item.get('autoApplyId') or ''
        return {
            'id': f'aa:{aid}',
            'kind': 'auto_apply_attention',
            'text': f'{company} needs your input — {count} {qword} to finish applying',
            'timeLabel': _relative_label(ts),
            'read': read,
            'autoApplyId': aid,
        }

    if item.get('kind') == 'auto_apply_result':
        title = item.get('jobTitle') or 'a job'
        company = item.get('company') or 'the company'
        outcome = item.get('outcome') or 'submitted'
        aid = item.get('autoApplyId') or ''
        if outcome == 'submitted':
            text = f'Application submitted — {title} at {company} is in'
        elif outcome == 'needs_verification':
            text = f'One tap left: {company} needs a quick human check to finish {title}'
        else:
            text = f"Application to {company} didn't go through — tap to see what happened"
        return {
            'id': f'aar:{aid}',
            'kind': 'auto_apply_result',
            'outcome': outcome,
            'text': text,
            'timeLabel': _relative_label(ts),
            'read': read,
            'autoApplyId': aid,
        }

    if item.get('kind') == 'draft_ready':
        who = item.get('contactName') or 'a contact'
        company = item.get('company') or ''
        sent = bool(item.get('sent'))
        contact_id = item.get('contactId') or ''
        at = f' at {company}' if company else ''
        text = (
            f'Sent to {who}{at}'
            if sent
            else f'Outreach to {who}{at} is ready to review'
        )
        return {
            'id': f'draft:{contact_id}:{ts}' if contact_id else f'draft:{ts}',
            'kind': 'draft_ready',
            'text': text,
            'timeLabel': _relative_label(ts),
            'read': read,
            'outreachId': contact_id,
        }

    if is_loop:
        name = item.get('loopName') or item.get('contactName') or 'Your Loop'
        text = f'{name}: {snippet}' if snippet else f'{name} ran'
        return {
            'id': item.get('cycleId') or f"{item.get('loopId', 'loop')}:{ts}",
            'kind': 'loop',
            'text': text,
            'timeLabel': _relative_label(ts),
            'read': read,
        }

    name = item.get('contactName') or item.get('company') or 'Someone'
    text = f'{name} replied: {snippet}' if snippet else f'{name} replied'
    contact_id = item.get('contactId') or ''
    return {
        'id': item.get('messageId') or (f'{contact_id}:{ts}' if contact_id else ts),
        'kind': 'reply',
        'text': text,
        'timeLabel': _relative_label(ts),
        'read': read,
        'outreachId': contact_id,
    }


def _grad_year(u: dict, prof: dict, academics: dict) -> str:
    """Grad year, preferring explicit profile fields, then auto-extracted from
    the parsed resume's education.graduation (e.g. 'May 2024' -> '2024')."""
    explicit = academics.get('gradYear') or prof.get('gradYear') or u.get('gradYear')
    if explicit:
        return str(explicit)
    edu = (u.get('resumeParsed') or {}).get('education') or {}
    grad = str(edu.get('graduation') or '')
    m = re.search(r'(19|20)\d{2}', grad)
    return m.group(0) if m else ''


def _linkedin_highlights(u: dict) -> list:
    """Compose the Profile 'what we gathered from your LinkedIn' bullets from the
    structured linkedinResumeParsed (written by the LinkedIn enrichment flow,
    routes/enrichment.py). Real data only — empty list when not enriched, so the
    section hides rather than showing fabricated bullets."""
    lp = u.get('linkedinResumeParsed') or {}
    if not isinstance(lp, dict):
        return []
    out: list = []
    objective = str(lp.get('objective') or '').strip()
    if objective:
        out.append(objective)
    skills = lp.get('skills') or {}
    flat: list = []
    if isinstance(skills, dict):
        for k in ('technical', 'tools', 'soft_skills', 'languages'):
            flat.extend(s.strip() for s in (skills.get(k) or []) if isinstance(s, str) and s.strip())
    elif isinstance(skills, list):
        flat.extend(s.strip() for s in skills if isinstance(s, str) and s.strip())
    if flat:
        out.append('Skills: ' + ', '.join(flat[:8]))
    for e in (lp.get('extracurriculars') or [])[:3]:
        if isinstance(e, dict):
            label = (e.get('organization') or e.get('activity') or '').strip()
            role = (e.get('role') or '').strip()
            if role and label:
                out.append(f'{role} — {label}')
            elif label:
                out.append(label)
        elif isinstance(e, str) and e.strip():
            out.append(e.strip())
    return out[:6]


def _credit_display(u: dict, clock=None) -> dict:
    """Spendable balance across ALL ledger buckets (monthly + purchased bonus
    + unexpired promo), computed by the same pure functions the deduct path
    uses so display and spend can never disagree. Before this the app showed
    only the monthly bucket, which made top-up packs granted by the
    RevenueCat webhook invisible (2026-07-31). The promo sweep here is
    read-only; the deduct path persists expiry."""
    ledger = expire_promos(state_from_user_dict(u), clock or SystemClock()).new_state
    return {
        'credits': ledger.total(),
        'monthlyCredits': max(0, ledger.monthly),
        'bonusCredits': max(0, ledger.bonus),
        'promoCredits': max(0, ledger.promo),
    }


def _map_resume_experiences(parsed: dict) -> list:
    """Map resumeParsed.experience[] (written by the web resume parser, see
    routes/resume.py) onto the mobile ResumeExperience shape. Real data only —
    entries with no role AND no org are skipped so the Profile parse view never
    shows empty/placeholder cards."""
    out = []
    for i, exp in enumerate((parsed or {}).get('experience') or []):
        if not isinstance(exp, dict):
            continue
        role = (exp.get('title') or '').strip()
        org = (exp.get('company') or '').strip()
        if not role and not org:
            continue
        bullets = [b.strip() for b in (exp.get('bullets') or []) if isinstance(b, str) and b.strip()]
        out.append({
            'id': f'exp-{i}',
            'role': role or 'Role',
            'org': org,
            'dates': (exp.get('dates') or '').strip(),
            'location': (exp.get('location') or '').strip() or None,
            'bullets': bullets,
        })
    return out


@mobile_bp.get('/me')
@require_firebase_auth
def me():
    """One call for everything Profile + the guardrails need. Maps the user doc
    onto the shape the mobile profile object expects."""
    db = get_db()
    fb = request.firebase_user
    uid = fb['uid']
    email = fb.get('email', '') or ''
    name = fb.get('name') or ''

    u = _ensure_user_doc(db, uid, email, name)

    tier = str(u.get('subscriptionTier') or u.get('tier') or 'free').lower()
    prof = u.get('professionalInfo') or {}
    academics = u.get('academics') or {}
    gmail_connected, gmail_address = _gmail_status(db, uid, email)

    target_roles = u.get('targetRoles')
    if not isinstance(target_roles, list) or not target_roles:
        ct = (
            u.get('careerTrack')
            or (u.get('goals') or {}).get('careerTrack')
            or prof.get('careerTrack')
        )
        target_roles = [ct] if ct else []

    resume_parsed = u.get('resumeParsed') or {}
    resume_url = u.get('resumeUrl') or prof.get('resumeUrl')
    # `resume` means a FILE we can attach to an application — nothing else.
    # This used to fall back to ('Resume' if resume_parsed) so a parsed resume
    # "counted as on-file". That was a lie with teeth: onboarding/LinkedIn set
    # resumeParsed WITHOUT ever storing a file, so the app showed "Attached"
    # with nothing attached, the user couldn't detach it, and every auto-apply
    # left the Greenhouse Resume field empty (2026-07-12). Parsed text still
    # powers email personalization via resumeExperiences below — that's a
    # separate thing from having a document to upload.
    resume_name = (
        u.get('resumeFileName')
        or prof.get('resumeFileName')
        or ('Resume.pdf' if resume_url else None)
    )
    # Some resumes were stored with the URL-ENCODED filename rather than the
    # human one, so the app rendered "Rylan%20Bohnett%20Resume%20April%202025.pdf"
    # (the storage URL even double-encodes it to %2520). Decode for display —
    # this fixes every existing user without a migration. Guarded: a literal '%'
    # in a real filename is left alone if unquote can't improve it.
    if resume_name and '%' in resume_name:
        try:
            from urllib.parse import unquote
            decoded = unquote(resume_name)
            if decoded and decoded != resume_name:
                resume_name = decoded
        except Exception:
            pass
    resume_experiences = _map_resume_experiences(resume_parsed)

    return jsonify({
        'name': u.get('name') or name or (email.split('@')[0] if email else 'You'),
        'email': email,
        'photoUrl': u.get('picture') or u.get('photoURL') or fb.get('picture') or '',
        'school': get_structured_school(u),
        'plan': PLAN_LABEL.get(tier, 'Free'),
        'tier': tier,
        **_credit_display(u),
        'maxCredits': int(u.get('maxCredits') or 0),
        'resetLabel': _reset_label(),
        # Per-CONTACT price of a swipe. Must match the actual charge in
        # runs.py prompt_search (5 × contacts drafted) — the app multiplies
        # this by its contacts-per-swipe setting for the optimistic decrement.
        # Straight from CREDIT_COSTS, so the price the app quotes and the price
        # the backend charges can never drift apart again.
        'creditsPerSwipe': CREDIT_COSTS['find_contact'],
        'gmailConnected': gmail_connected,
        'gmailAddress': gmail_address,
        'resume': (
            {'name': resume_name, 'url': resume_url, 'thumbUrl': u.get('resumeThumbUrl') or prof.get('resumeThumbUrl') or None}
            if resume_name else None
        ),
        'linkedinUrl': u.get('linkedinUrl') or prof.get('linkedinUrl') or '',
        # Phone for pre-filling the auto-apply Application Profile. Same source
        # order job_board.py uses: resume-parsed contact, then the user doc.
        'phone': (
            (resume_parsed.get('contact') or {}).get('phone')
            or u.get('phone')
            or prof.get('phone')
            or ''
        ),
        'targetRoles': target_roles,
        # Job location preferences — the feed already reads this as a soft geo
        # ranking signal (jobs.py _signals / job_board ranking). Surfaced so the
        # app's Location preferences editor can show + update it.
        'preferredLocations': (
            u.get('preferredLocations')
            or u.get('targetLocations')
            or ((u.get('location') or {}).get('preferredLocation')
                if isinstance(u.get('location'), dict) else None)
            or []
        ),
        'industries': get_structured_target_industries(u),
        'gradYear': _grad_year(u, prof, academics),
        # About: what the user wrote, else what we already know about them. The
        # LinkedIn enrichment writes a one-line summary into
        # linkedinResumeParsed.objective ("Co-founder at offerloop.ai in the
        # financial services industry") — we were sitting on it and never
        # reading it, so About rendered blank for enriched users whose uploaded
        # resume happened to have no objective line. Falling through to it means
        # the field arrives pre-filled from the LinkedIn + resume parse instead
        # of empty; it stays fully editable and the user's own text always wins.
        'about': (
            u.get('personalNote')
            or u.get('about')
            or resume_parsed.get('objective')
            or (u.get('linkedinResumeParsed') or {}).get('objective')
            or (u.get('linkedinEnrichmentData') or {}).get('summary')
            or ''
        ),
        'resumeExperiences': resume_experiences,
        'linkedinHighlights': _linkedin_highlights(u),
        # The one explicit, server-side "this account finished onboarding"
        # fact (Rylan 2026-09-11). Everything else /me exposes is real
        # profile data that can be true from just the first onboarding step
        # (a LinkedIn URL, a target track) — useful as a heuristic for "does
        # this look like an established account", useless as proof onboarding
        # actually finished. This field is only ever set by POST
        # /api/mobile/preferences {onboardingComplete: true}, which the app
        # calls exactly once, the moment its own onboarding flow completes.
        'onboardingComplete': bool(u.get('onboardingComplete')),
    }), 200


@mobile_bp.get('/flags')
@require_firebase_auth
def feature_flags():
    """Remote feature flags: a kill switch / gradual rollout lever so a risky
    new feature can ship inside a native build turned OFF, then be dialed on
    (or back off) for some or all users with zero app update — not even OTA
    (Rylan 2026-09-11, after the dark-mode/resume-upload native-build saga
    made it clear how expensive it is to be wrong on a brand-new user's first
    launch). Toggling is a single Firestore doc edit, no deploy required.

    Doc shape (config/featureFlags), every key optional:
      {"someFlag": {"enabled": true, "rolloutPercent": 25}}
    rolloutPercent (0-100, default 100 when omitted) gates a STABLE per-user
    slice via a hash of uid+key, so one user's flag never flips between
    requests as a rollout dials up.

    Resolves to plain booleans so the client never re-implements the rollout
    math. A missing or unreadable doc resolves to {} (every flag off) rather
    than a 500 — a broken flags fetch must never be able to block the app.
    """
    fb = request.firebase_user
    uid = fb['uid']
    try:
        db = get_db()
        raw = db.collection('config').document('featureFlags').get().to_dict() or {}
    except Exception:
        logger.warning('feature flags: config read failed, defaulting to all-off', exc_info=True)
        raw = {}

    out = {}
    for key, cfg in raw.items():
        if not isinstance(cfg, dict) or not cfg.get('enabled'):
            out[key] = False
            continue
        pct = cfg.get('rolloutPercent')
        pct = 100 if pct is None else max(0, min(100, int(pct)))
        if pct >= 100:
            out[key] = True
        else:
            bucket = int(hashlib.sha256(f'{uid}:{key}'.encode()).hexdigest(), 16) % 100
            out[key] = bucket < pct
    return jsonify(out), 200


@mobile_bp.post('/device')
@require_firebase_auth
def register_device():
    """Store an Expo push token for the signed-in user's device so the backend
    can push reply / draft-ready alerts. Idempotent (keyed by token hash)."""
    uid = request.firebase_user['uid']
    data = request.get_json(silent=True) or {}
    token = (data.get('token') or '').strip()
    platform = (data.get('platform') or '').strip()
    if not token:
        return jsonify({'error': 'Missing token'}), 400

    from app.services.push_service import is_expo_token, register_device as store_token
    if not is_expo_token(token):
        return jsonify({'error': 'Invalid push token'}), 400
    if not store_token(uid, token, platform):
        return jsonify({'error': 'Could not register device'}), 500
    return jsonify({'registered': True}), 200


@mobile_bp.get('/notifications')
@require_firebase_auth
def notifications():
    """The header-bell feed. Reads users/{uid}/notifications/outbox (the same
    doc the reply webhook + loop cycles append to) and maps it to the mobile
    NotificationItem shape, so a push that just landed shows up in the list."""
    db = get_db()
    uid = request.firebase_user['uid']
    try:
        snap = db.collection('users').document(uid).collection('notifications').document('outbox').get()
        data = (snap.to_dict() or {}) if snap.exists else {}
    except Exception:
        data = {}
    raw_items = list(data.get('items') or [])
    items = [_map_notification_item(it) for it in raw_items]
    unread = sum(1 for it in items if not it['read'])
    return jsonify({'items': items, 'unreadCount': unread}), 200


def _clean_str_list(value, cap=20):
    """Dedupe + trim a list of strings, dropping blanks. Returns None if the
    input isn't a list (so callers can tell 'omitted' from 'empty')."""
    if not isinstance(value, list):
        return None
    out = []
    for x in value:
        s = str(x).strip()
        if s and s not in out:
            out.append(s)
    return out[:cap]


# --- Company inventory counts (Piece 2: pool-aware Scout suggestions) ---------
# In-process cache: counts barely move, and Scout re-asks the same vibe firms.
_company_count_cache: dict = {}
_company_count_lock = threading.Lock()
_COMPANY_COUNT_TTL = 3600
_company_count_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="co-count")


def _slugify_company(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")


def _company_job_count(name: str) -> int:
    """Total job count for a company, from Sid's Piece-1 `companies` index.

    Slug the input DIRECTLY — do NOT re-canonicalize here. This app backend still
    carries the OLD passthrough canonicalize_company; Sid's real normalizer is on
    main and is what wrote the index + job `company` fields. Re-normalizing with
    the stale function would MISS the index for anything it should merge (Palantir
    Technologies, Morgan Stanley…). Suggestion names arrive already-canonical and
    the index slug is case-insensitive, so a direct slug hits reliably."""
    from app.extensions import get_db
    nm = (name or "").strip()
    if not nm:
        return 0
    slug = _slugify_company(nm)
    if not slug:
        return 0
    now = time.time()
    with _company_count_lock:
        hit = _company_count_cache.get(slug)
        if hit and now - hit[0] < _COMPANY_COUNT_TTL:
            return hit[1]
    db = get_db()
    total = 0
    try:
        idx = db.collection("companies").document(slug).get()
        if idx.exists:
            jc = (idx.to_dict() or {}).get("jobCount") or {}
            total = int(jc.get("total", 0)) or (
                int(jc.get("tier1", 0)) + int(jc.get("tier2", 0)) + int(jc.get("tier3", 0))
            )
        else:
            from google.cloud.firestore_v1.base_query import FieldFilter
            total = int(db.collection("jobs").where(
                filter=FieldFilter("company", "==", nm)).count().get()[0][0].value)
    except Exception:
        total = 0
    with _company_count_lock:
        _company_count_cache[slug] = (now, total)
    return total


@mobile_bp.get('/company-counts')
@require_firebase_auth
def company_counts():
    """Batch: how many open roles we have per company. Powers grounding Scout's
    company suggestions in real inventory (drop firms with 0, rank by count).
    Query: ?companies=Stripe,Plaid,Circle  ->  {counts: {Stripe: 714, ...}}."""
    raw = (request.args.get('companies') or '').strip()
    names = [c.strip() for c in raw.split(',') if c.strip()][:8]
    if not names:
        return jsonify({'counts': {}}), 200
    futs = {name: _company_count_pool.submit(_company_job_count, name) for name in names}
    counts = {}
    for name, fut in futs.items():
        try:
            counts[name] = int(fut.result(timeout=6) or 0)
        except Exception:
            counts[name] = 0
    return jsonify({'counts': counts}), 200


# --- Sector-derived vibe companies (Piece 3) ---------------------------------
_vibe_sector_cache: dict = {}      # sector -> (ts, [names ranked by inventory])
_vibe_sector_lock = threading.Lock()
_VIBE_SECTOR_TTL = 3600
_VIBE_SKIP_SECTORS = {"", "other", "none"}
# Drop obvious non-employer noise from sector suggestions — staffing/lead-gen
# shells and canonicalization leftovers ("…glassdoor") a student wouldn't mean.
_VIBE_JUNK_RE = re.compile(r"glassdoor|staffing|talent platform|recruit(?:ing|ers?)|lead gen", re.I)


def _top_companies_in_sector(sector: str, limit: int = 10) -> list:
    """Top companies in a sector, ranked by real job inventory (jobCount.total).
    Reads Sid's `companies` index (sector tags from Piece 3). Cached per sector."""
    s = (sector or "").strip().lower()
    if s in _VIBE_SKIP_SECTORS:
        return []
    now = time.time()
    with _vibe_sector_lock:
        hit = _vibe_sector_cache.get(s)
        if hit and now - hit[0] < _VIBE_SECTOR_TTL:
            return hit[1][:limit]
    from app.extensions import get_db
    from google.cloud.firestore_v1.base_query import FieldFilter
    db = get_db()
    ranked: list = []
    try:
        rows = []
        for d in (db.collection("companies")
                    .where(filter=FieldFilter("sector", "==", s))
                    .select(["name", "jobCount"]).limit(1500).stream()):
            j = d.to_dict() or {}
            nm = j.get("name")
            if not nm or _VIBE_JUNK_RE.search(str(nm)):
                continue
            rows.append((int((j.get("jobCount") or {}).get("total") or 0), str(nm)))
        rows.sort(key=lambda r: r[0], reverse=True)
        ranked = [nm for _, nm in rows]
    except Exception:
        ranked = []
    with _vibe_sector_lock:
        _vibe_sector_cache[s] = (now, ranked)
    return ranked[:limit]


@mobile_bp.get('/vibe-companies')
@require_firebase_auth
def vibe_companies():
    """Live pool companies for a vibe, ranked by inventory. Powers pool-derived
    Scout suggestions: ?sector=fintech, or ?like=Stripe (resolves Stripe's sector
    from the index, returns its peers). Empty when the sector is unknown/other or
    the reference company isn't classified yet — the app then falls back to its
    curated maps."""
    sector = (request.args.get('sector') or '').strip().lower()
    like = (request.args.get('like') or '').strip()
    try:
        limit = max(1, min(int(request.args.get('limit', 10)), 20))
    except Exception:
        limit = 10
    resolved = sector
    if like and not resolved:
        from app.extensions import get_db
        try:
            d = get_db().collection('companies').document(_slugify_company(like)).get()
            if d.exists:
                resolved = str((d.to_dict() or {}).get('sector') or '').strip().lower()
        except Exception:
            resolved = ''
    names = _top_companies_in_sector(resolved, limit) if resolved else []
    if like:  # don't suggest the reference company back to itself
        names = [n for n in names if n.strip().lower() != like.strip().lower()]
    return jsonify({'sector': resolved or None, 'companies': names[:limit]}), 200


# --- Sector jobs: entry-friendly, US, from the full pool (not the feed) --------
_sector_jobs_cache: dict = {}      # sector -> (ts, [serialized jobs])
_sector_jobs_lock = threading.Lock()
_SECTOR_JOBS_TTL = 1800


_US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID", "IL",
    "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS", "MO", "MT",
    "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK", "OR", "PA", "RI",
    "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV", "WI", "WY", "DC",
}
_US_STATE_NAMES_RE = re.compile(
    r"\b(alabama|alaska|arizona|arkansas|california|colorado|connecticut|delaware|"
    r"florida|georgia|hawaii|idaho|illinois|indiana|iowa|kansas|kentucky|louisiana|"
    r"maine|maryland|massachusetts|michigan|minnesota|mississippi|missouri|montana|"
    r"nebraska|nevada|new hampshire|new jersey|new mexico|new york|north carolina|"
    r"north dakota|ohio|oklahoma|oregon|pennsylvania|rhode island|south carolina|"
    r"south dakota|tennessee|texas|utah|vermont|virginia|washington|west virginia|"
    r"wisconsin|wyoming)\b", re.I)


def _job_looks_us(job: dict) -> bool:
    """POSITIVE US-detection — keep only jobs that affirmatively read US, instead
    of trusting the blocklist (which misses bare foreign cities like 'Faridabad').
    Remote / unknown pass; a US state/'United States'/', NY' passes; everything
    else is treated as non-US."""
    loc = job.get("location")
    if isinstance(loc, dict):
        loc = loc.get("city") or loc.get("location") or ""
    s = str(loc or "").strip()
    if not s:
        return True  # unknown — don't over-filter
    low = s.lower()
    if "remote" in low or "united states" in low or "usa" in low or "u.s." in low:
        return True
    if _US_STATE_NAMES_RE.search(low):
        return True
    m = re.search(r",\s*([A-Za-z]{2})\b", s)
    return bool(m and m.group(1).upper() in _US_STATES)


def _serialize_pool_job(j: dict) -> dict:
    """Slim job for Scout's job cards — matches what the app's mapJob reads."""
    from app.services.auto_apply.ats_detector import is_eligible, detect_platform
    return {
        "job_id": j.get("job_id"),
        "title": j.get("title"),
        "company": j.get("company"),
        "location": j.get("location"),
        "remote": j.get("remote"),
        "type": j.get("type"),
        "salary_display": j.get("salary_display"),
        "structured": j.get("structured"),
        "employer_logo": j.get("employer_logo"),
        "apply_url": j.get("apply_url"),
        "ats_platform": detect_platform(j),
        "auto_apply_eligible": is_eligible(j),
    }


@mobile_bp.get('/sector-jobs')
@require_firebase_auth
def sector_jobs():
    """Entry-friendly (tier 1-2), US, recent jobs at a sector's top companies —
    from the FULL 129k pool, not the user's ranked feed. Powers Scout job search
    for 'fintech jobs for a college junior' etc. ?sector=fintech&limit=8."""
    sector = (request.args.get('sector') or '').strip().lower()
    try:
        limit = max(1, min(int(request.args.get('limit', 8)), 15))
    except Exception:
        limit = 8
    if not sector or sector in _VIBE_SKIP_SECTORS:
        return jsonify({'jobs': []}), 200
    now = time.time()
    with _sector_jobs_lock:
        hit = _sector_jobs_cache.get(sector)
        if hit and now - hit[0] < _SECTOR_JOBS_TTL:
            return jsonify({'jobs': hit[1][:limit]}), 200

    from app.extensions import get_db
    from google.cloud.firestore_v1.base_query import FieldFilter
    from app.utils.job_ranking import _is_non_us, _is_excluded
    db = get_db()
    companies = _top_companies_in_sector(sector, 8)

    def _company_jobs(name: str) -> list:
        out = []
        try:
            for d in (db.collection("jobs")
                        .where(filter=FieldFilter("company", "==", name))
                        .order_by("posted_at", direction="DESCENDING").limit(15).stream()):
                job = d.to_dict() or {}
                # Entry-friendly for an EXPLICIT sector search = US + not senior.
                # POSITIVE US-detection (not the blocklist — it misses bare foreign
                # cities like Faridabad); _is_excluded drops senior/manager titles,
                # the real "not entry-level" filter. No tier-1/2 gate: that's the
                # passive feed's personalization signal and it's too scarce (~1-10%
                # of a company's jobs) — it emptied the recent window.
                if not _job_looks_us(job) or _is_excluded(job):
                    continue
                # Foreign-language title (accented chars) → foreign-remote leak
                # (e.g. Pennylane's French "Comptabilité" roles marked Remote).
                if re.search(r"[À-ÿ]", str(job.get("title") or "")):
                    continue
                out.append(job)
                if len(out) >= 5:
                    break
        except Exception:
            pass
        return out

    per: list = []
    if companies:
        with ThreadPoolExecutor(max_workers=min(8, len(companies))) as ex:
            per = list(ex.map(_company_jobs, companies))
    # Round-robin interleave so no one employer dominates. Dedup by job_id AND by
    # (title, company) — the same role posted across cities showed as "Finance
    # Associate @ Brex" three times.
    interleaved, seen_ids, seen_tc = [], set(), set()
    for i in range(5):
        for jobs in per:
            if i < len(jobs):
                j = jobs[i]
                jid = j.get("job_id")
                tc = ((j.get("title") or "").strip().lower(), (j.get("company") or "").strip().lower())
                if jid and jid not in seen_ids and tc not in seen_tc:
                    seen_ids.add(jid)
                    seen_tc.add(tc)
                    interleaved.append(_serialize_pool_job(j))
    with _sector_jobs_lock:
        _sector_jobs_cache[sector] = (now, interleaved)
    return jsonify({'jobs': interleaved[:limit]}), 200


_REFERRAL_GOAL = 5


@mobile_bp.get('/referral')
@require_firebase_auth
def get_referral():
    """The signed-in user's referral code, share link, and live progress toward
    the free-Elite reward. The app only DISPLAYS this + shares the link — the
    reward (Elite at goal) is granted web-side, so the app sells/grants nothing."""
    import hashlib
    uid = request.firebase_user['uid']
    db = get_db()
    user_ref = db.collection('users').document(uid)
    u = (user_ref.get().to_dict() or {})
    code = u.get('referralCode')
    if not code:
        code = hashlib.sha1(uid.encode()).hexdigest()[:7].upper()
        try:
            user_ref.set({'referralCode': code}, merge=True)
        except Exception:
            logger.exception('referral: could not persist code for %s', uid)
    # Live count of users who joined with this code. Falls back to a stored
    # counter the web can maintain if the query isn't indexed.
    joined = None
    try:
        from google.cloud.firestore_v1.base_query import FieldFilter
        joined = int(db.collection('users').where(
            filter=FieldFilter('referredBy', '==', code)).count().get()[0][0].value)
    except Exception:
        joined = int(u.get('referralJoinedCount') or 0)
    return jsonify({
        'code': code,
        'shareUrl': f'https://offerloop.ai/?ref={code}',
        'joined': joined,
        'goal': _REFERRAL_GOAL,
        'unlocked': joined >= _REFERRAL_GOAL,
    }), 200


@mobile_bp.post('/account/delete')
@require_firebase_auth
def delete_account():
    """Permanently delete the signed-in user's account + data. REQUIRED by Apple
    5.1.1(v) for any app with account creation — no store approval without it.
    Removes the Firestore user doc (and its subcollections) and the Firebase Auth
    user, which is what actually revokes the account."""
    uid = request.firebase_user['uid']
    db = get_db()
    user_ref = db.collection('users').document(uid)
    # 1. Firestore data — recursive delete (doc + all subcollections). Fall back
    #    to sweeping the known subcollections if recursive_delete isn't available.
    try:
        db.recursive_delete(user_ref)
    except Exception:
        try:
            for sub in ('contacts', 'integrations', 'recruiters', 'manual_firms',
                        'scoutConversations', 'coffee-chat-preps', 'scoutChats',
                        'messages', 'notifications', 'activity', 'searchHistory',
                        'firmSearches', 'exports', 'goals', 'autoApplyJobs',
                        'devices', 'feedback', 'jobPreferences', 'calendar_events'):
                for d in user_ref.collection(sub).stream():
                    d.reference.delete()
            user_ref.delete()
        except Exception:
            logger.exception('account delete: firestore cleanup failed for %s', uid)
    # 2. The Auth user — this is what actually removes the account. If this fails
    #    we must NOT report success (Apple treats a still-usable login as no delete).
    try:
        from firebase_admin import auth as fb_auth
        fb_auth.delete_user(uid)
    except Exception:
        logger.exception('account delete: auth delete failed for %s', uid)
        return jsonify({'ok': False, 'error': 'delete_failed'}), 500
    logger.info('account deleted for %s', uid)
    return jsonify({'ok': True}), 200


@mobile_bp.post('/preferences')
@require_firebase_auth
def save_preferences():
    """Persist onboarding + preference answers onto the user doc. Each field is
    optional, so the mobile onboarding can POST them independently:
      - preferredLocations — soft geo ranking signal the feed already reads
        (jobs.py _signals + job_board ranking). Soft, not a hard filter.
      - targetRoles / industries — the onboarding goal (IB / Consulting / Tech);
        /me already surfaces these and the feed ranks against them.
      - referralSource — acquisition attribution ("How did you hear about us?").
      - about / gradYear / linkedinUrl — the rest of the Profile tab. These were
        collected by the app's Save button but had nowhere to go: the endpoint
        didn't accept them, so they lived only in the local React Query cache and
        were silently lost on the next refetch. Same "I edited my profile and
        nothing changed" bug that targetRoles/industries already got fixed for —
        this finishes the job.
    Only the fields present in the body are written (merge=True), so a
    locations-only or goal-only POST never clobbers the others."""
    db = get_db()
    uid = request.firebase_user['uid']
    data = request.get_json(silent=True) or {}

    patch = {}
    if 'preferredLocations' in data:
        locs = _clean_str_list(data.get('preferredLocations'))
        if locs is None:
            return jsonify({'error': 'preferredLocations must be a list'}), 400
        patch['preferredLocations'] = locs
    if 'targetRoles' in data:
        roles = _clean_str_list(data.get('targetRoles'))
        if roles is None:
            return jsonify({'error': 'targetRoles must be a list'}), 400
        patch['targetRoles'] = roles
    if 'industries' in data:
        inds = _clean_str_list(data.get('industries'))
        if inds is None:
            return jsonify({'error': 'industries must be a list'}), 400
        # /me reads industries via get_structured_target_industries(), which looks
        # at `targetIndustries` — NOT this legacy `industries` field. Writing only
        # `industries` meant the value never round-tripped: it saved, but the next
        # /me refetch read the untouched `targetIndustries` and reverted the UI
        # (the "industries won't save" bug — targetRoles worked because it reads
        # and writes the same key). Write the canonical field the reader uses; keep
        # `industries` too for any legacy reader and so the cache-bust below fires.
        patch['industries'] = inds
        patch['targetIndustries'] = inds
    if 'referralSource' in data:
        src = str(data.get('referralSource') or '').strip()[:120]
        if src:
            patch['referralSource'] = src
    if 'about' in data:
        # Write to personalNote — /me reads that FIRST, so the user's own words
        # always beat the LinkedIn-derived fallback. Allow clearing it ('' is a
        # legitimate value: "I don't want an About").
        patch['personalNote'] = str(data.get('about') or '').strip()[:600]
    if 'gradYear' in data:
        gy = str(data.get('gradYear') or '').strip()[:8]
        if gy:
            patch['gradYear'] = gy
    if 'linkedinUrl' in data:
        li = str(data.get('linkedinUrl') or '').strip()[:200]
        if li:
            patch['linkedinUrl'] = li
    # The two fields the People deck cannot run without. The app's gap survey
    # and Scout's school nudge have POSTed them here since 2026-08-16, but the
    # route never recognized them, so every save came back 400 ("Could not
    # save that. Try again later.") and the ranker stayed blind forever
    # (Rylan 2026-08-26, saving USC on the empty-deck nudge). Written to BOTH
    # spellings the readers use: person_ranker reads academics.school and
    # top-level school, goals.dreamCompanies and top-level dreamCompanies.
    if 'school' in data:
        school = str(data.get('school') or '').strip()[:120]
        if school:
            patch['school'] = school
            patch['academics'] = {'school': school}
    if 'dreamCompanies' in data:
        firms = _clean_str_list(data.get('dreamCompanies'))
        if firms is None:
            return jsonify({'error': 'dreamCompanies must be a list'}), 400
        patch['dreamCompanies'] = firms[:20]
    if 'onboardingComplete' in data:
        # Write-once in practice (the app only ever sends true, exactly once,
        # from completeOnboarding()) but accepted as a real bool so a bad
        # client payload can't wedge this into a truthy-but-wrong value.
        patch['onboardingComplete'] = bool(data.get('onboardingComplete'))

    if not patch:
        return jsonify({'error': 'no recognized preference fields'}), 400

    db.collection('users').document(uid).set(patch, merge=True)
    # Instant warm (2026-08-31): the moment a user saves target firms,
    # stock the warehouse shelf for THOSE firms in the background, so the
    # people deck is full by the time they reach it instead of waiting for
    # the 2am demand-ranked warm. Bounded (USER_WARM_MAX_COLLECTS, default
    # 36), coverage-probed (already-stocked firms cost nothing), and
    # tank-guarded like every other warm path.
    if patch.get('dreamCompanies'):
        try:
            from app.services.warehouse_warm import spawn_instant_user_warm
            spawn_instant_user_warm(patch['dreamCompanies'])
        except Exception:
            logger.exception('instant warm spawn failed for uid=%s', uid)
    # A feed-relevant edit (roles/industries/locations) must re-tune the feed.
    # The write alone doesn't touch jobFeedCache, so the ranked deck would keep
    # serving the stale slice for up to ~2h. Null the cache — same bust the
    # ?ungated escape hatch does (jobs.py) — so the next GET /api/jobs/feed
    # reranks against the new profile. referralSource doesn't affect ranking, so
    # a referral-only POST doesn't trigger a rerank.
    if any(k in patch for k in ('preferredLocations', 'targetRoles', 'industries')):
        db.collection('users').document(uid).update({'jobFeedCache': None})
    return jsonify({'ok': True, **patch}), 200


@mobile_bp.get('/feedback')
@require_firebase_auth
def list_feedback():
    """The user's own feedback thread, oldest first, so the chat screen can
    replay what they've sent. The founder-facing view reads the top-level
    `feedback` collection across all users."""
    db = get_db()
    uid = request.firebase_user['uid']
    try:
        # Read the per-user subcollection (ordered by createdAt only — no
        # composite index needed, unlike a where(uid)+order_by on the top-level
        # collection). The top-level `feedback` collection is the founder view.
        docs = (
            db.collection('users').document(uid).collection('feedback')
            .order_by('createdAt')
            .limit(200)
            .stream()
        )
        items = []
        for d in docs:
            x = d.to_dict() or {}
            items.append({
                'id': d.id,
                'message': x.get('message', ''),
                'createdAt': (x.get('createdAt').isoformat()
                              if hasattr(x.get('createdAt'), 'isoformat') else None),
            })
        return jsonify({'items': items}), 200
    except Exception:
        logger.exception('list_feedback failed for uid=%s', uid)
        return jsonify({'items': []}), 200


@mobile_bp.post('/feedback')
@require_firebase_auth
def submit_feedback():
    """Store one feedback message. Written to a TOP-LEVEL `feedback` collection
    (not under the user) so the founders can read everything in one query, with
    uid + email so we can follow up. This is the "goes directly to the founders"
    channel — no AI, no auto-reply; a human reads it."""
    db = get_db()
    uid = request.firebase_user['uid']
    data = request.get_json(silent=True) or {}
    message = (data.get('message') or '').strip()
    if not message:
        return jsonify({'error': 'message is required'}), 400
    if len(message) > 4000:
        message = message[:4000]

    email = request.firebase_user.get('email') or ''
    try:
        u = db.collection('users').document(uid).get().to_dict() or {}
        name = u.get('name') or ''
        tier = str(u.get('subscriptionTier') or u.get('tier') or 'free')
    except Exception:
        name, tier = '', 'free'

    doc = {
        'uid': uid,
        'email': email,
        'name': name,
        'tier': tier,
        'message': message,
        'platform': 'ios',
        'createdAt': datetime.now(timezone.utc),
        'read': False,   # founder-side triage flag
    }
    try:
        # Top-level = founders read everything in one query. Per-user
        # subcollection = the app replays this user's thread without a composite
        # index. Same id in both so they're easy to correlate.
        ref = db.collection('feedback').document()
        ref.set(doc)
        db.collection('users').document(uid).collection('feedback').document(ref.id).set(doc)

        # Ping the founders on their phones so nobody has to watch the Firestore
        # console. Best effort, off the request path — a push hiccup must not fail
        # the user's feedback. Each founder must be signed into the app with
        # notifications on for this to land; the feedback is stored regardless.
        def _alert():
            try:
                from app.services.push_service import send_push
                who = name or email or uid[:8]
                title = 'New feedback'
                body = f'{who} ({tier}): {message[:120]}'
                for fuid in _founder_alert_uids():
                    try:
                        send_push(fuid, title, body, data={'url': '/feedback', 'type': 'founder_feedback'})
                    except Exception:
                        logger.exception('feedback push to founder %s failed', fuid)
            except Exception:
                logger.exception('feedback founder alert failed')
        threading.Thread(target=_alert, daemon=True).start()

        return jsonify({
            'ok': True,
            'id': ref.id,
            'createdAt': doc['createdAt'].isoformat(),
        }), 200
    except Exception:
        logger.exception('submit_feedback failed for uid=%s', uid)
        return jsonify({'error': 'could not save feedback'}), 500


@mobile_bp.post('/notifications/read')
@require_firebase_auth
def mark_notifications_read():
    """Mark every bell item read and zero the unread counters (mirrors the
    web bell). Best-effort; returns ok even if the doc doesn't exist yet."""
    db = get_db()
    uid = request.firebase_user['uid']
    ref = db.collection('users').document(uid).collection('notifications').document('outbox')
    try:
        snap = ref.get()
        if snap.exists:
            data = snap.to_dict() or {}
            items = [{**it, 'read': True} for it in (data.get('items') or [])]
            ref.set(
                {'items': items, 'unreadReplyCount': 0, 'unreadLoopRunCount': 0},
                merge=True,
            )
    except Exception as exc:
        return jsonify({'error': str(exc) or 'Could not mark read'}), 500
    return jsonify({'ok': True}), 200


@mobile_bp.post('/outreach/<contact_id>/send')
@require_firebase_auth
def send_outreach(contact_id):
    """Send a cold-open draft in-app so the user never leaves for Gmail. Sends
    the existing Gmail draft by id when present (no duplicate); otherwise sends
    the stored subject/body fresh. Then advances the contact to sent."""
    db = get_db()
    uid = request.firebase_user['uid']
    ref = db.collection('users').document(uid).collection('contacts').document(contact_id)
    snap = ref.get()
    if not snap.exists:
        return jsonify({'error': 'Contact not found'}), 404
    c = snap.to_dict() or {}

    to = c.get('draftToEmail') or c.get('email')
    subject = c.get('emailSubject') or ''
    body = c.get('emailBody') or ''
    draft_id = c.get('gmailDraftId')
    if not draft_id and not (to and body):
        return jsonify({'error': 'No draft to send'}), 400

    try:
        from app.services.gmail_client import send_draft_for_user, send_email_for_user
        if draft_id:
            result = send_draft_for_user(uid, draft_id)
        else:
            result = send_email_for_user(uid, to, subject, (body or '').replace('\n', '<br>'))
    except ValueError:
        return jsonify({'error': 'Connect Gmail to send'}), 400
    except Exception as exc:
        return jsonify({'error': str(exc) or 'Gmail send failed'}), 500

    now = datetime.now().isoformat()
    ref.update({
        'pipelineStage': 'waiting_on_reply',
        'inOutbox': True,
        'emailSentAt': now,
        'lastActivityAt': now,
        'lastMessageFrom': 'me',
        'gmailThreadId': result.get('threadId') or c.get('gmailThreadId'),
        'gmailMessageId': result.get('id') or c.get('gmailMessageId'),
    })
    return jsonify({'sent': True, 'threadId': result.get('threadId', '')}), 200


def _company_slug(name: str) -> str:
    return re.sub(r'[^a-z0-9]+', '-', (name or '').strip().lower()).strip('-')


def _employee_label(value) -> str:
    """Normalize Firecrawl employee_count into a short size chip label."""
    if value in (None, '', 0):
        return ''
    s = str(value).strip()
    # Already a range or has 'employee'/'+' — use verbatim.
    if any(t in s.lower() for t in ('employee', '-', '–', '+', 'k')):
        return s if 'employee' in s.lower() else f'{s} employees'
    if s.replace(',', '').isdigit():
        return f'{s} employees'
    return s


def _first_industry(industries) -> str:
    if isinstance(industries, list):
        for i in industries:
            if isinstance(i, str) and i.strip():
                return i.strip()
        return ''
    return str(industries).strip() if industries else ''


def _map_company_profile(name: str, firecrawl: dict) -> dict:
    """Map a Firecrawl extract_company_profile dict onto the mobile CompanyInfo
    shape ({about, industry, size, hq, news}). News is omitted for v1."""
    fc = firecrawl or {}
    return {
        'name': name,
        'about': (fc.get('description') or '').strip(),
        'industry': _first_industry(fc.get('industries')),
        'size': _employee_label(fc.get('employee_count')),
        'hq': (fc.get('headquarters') or '').strip(),
        'news': [],  # v1: skip the second live news call
    }


# Cached company profiles are global (not per-user) and refreshed every 30 days —
# firm intel changes slowly and the live Perplexity+Firecrawl call is slow/costly.
_COMPANY_CACHE_TTL_DAYS = 30
# Async-fill guards (PLAN-instant-feel.md Phase 3): a cache MISS no longer
# blocks the request on the slow Perplexity+Firecrawl pipeline — it answers
# {pending: true} instantly and fills the cache in a background thread while
# the app polls. These bound the transient states:
_COMPANY_FETCHING_STALE_S = 150      # in-flight marker older than this = dead thread, respawn
_COMPANY_EMPTY_TTL_HOURS = 24        # "nothing found" is remembered for a day, then retried
_COMPANY_FAIL_COOLDOWN_S = 90        # provider error: don't re-hit a downed provider per poll


def _parse_iso_utc(value):
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _fill_company_profile_background(name: str, slug: str) -> None:
    """The slow half of the company endpoint, off the request thread. Writes
    exactly one of: a real profile, an empty marker, or a failure marker."""
    db = get_db()
    ref = db.collection('companyProfiles').document(slug)
    now_iso = datetime.now(timezone.utc).isoformat()
    try:
        from app.services.firm_details_extraction import _fetch_serp_results_only
        raw = _fetch_serp_results_only(name)
        firecrawl = (raw or {}).get('_firecrawl_data') or {}
        profile = _map_company_profile(name, firecrawl)
        if profile['about'] or profile['industry'] or profile['hq']:
            # Full overwrite (not merge) so stale fetch/empty markers clear.
            ref.set({'name': name, 'profile': profile, 'fetchedAt': now_iso})
        else:
            # Honest miss: remember for a day so polls stop, retry tomorrow.
            ref.set({'name': name, 'emptyAt': now_iso})
    except Exception as e:
        print(f"[MobileCompany] background fill failed for {name!r}: {e}")
        try:
            ref.set({'name': name, 'failedAt': now_iso})
        except Exception:
            pass


@mobile_bp.get('/company/<path:name>')
@require_firebase_auth
def company(name: str):
    """Lightweight single-company overview for the app's company page. Reuses the
    firm-intel pipeline (Perplexity website discovery → Firecrawl profile), cached
    in Firestore companyProfiles/{slug}. Cache hits return instantly; misses
    return {pending: true} and fill in the background (the app polls). Returns
    honest-empty fields when nothing is found rather than fabricating."""
    name = (name or '').strip()
    if not name:
        return jsonify({'error': 'name is required'}), 400

    db = get_db()
    slug = _company_slug(name)
    cache_ref = db.collection('companyProfiles').document(slug) if slug else None
    now = datetime.now(timezone.utc)

    if cache_ref is not None:
        snap = cache_ref.get()
        if snap.exists:
            data = snap.to_dict() or {}

            fetched_dt = _parse_iso_utc(data.get('fetchedAt'))
            if (
                fetched_dt is not None
                and (now - fetched_dt).days < _COMPANY_CACHE_TTL_DAYS
                and data.get('profile')
            ):
                return jsonify(data['profile']), 200

            empty_dt = _parse_iso_utc(data.get('emptyAt'))
            if empty_dt is not None and (now - empty_dt).total_seconds() < _COMPANY_EMPTY_TTL_HOURS * 3600:
                return jsonify(_map_company_profile(name, {})), 200

            failed_dt = _parse_iso_utc(data.get('failedAt'))
            if failed_dt is not None and (now - failed_dt).total_seconds() < _COMPANY_FAIL_COOLDOWN_S:
                return jsonify(_map_company_profile(name, {})), 200

            fetching_dt = _parse_iso_utc(data.get('fetchStartedAt'))
            if fetching_dt is not None and (now - fetching_dt).total_seconds() < _COMPANY_FETCHING_STALE_S:
                return jsonify({'pending': True}), 202

    if cache_ref is None:
        # No Firestore (shouldn't happen in prod) — degrade to the old
        # synchronous behavior rather than lying.
        try:
            from app.services.firm_details_extraction import _fetch_serp_results_only
            raw = _fetch_serp_results_only(name)
        except Exception:
            raw = None
        firecrawl = (raw or {}).get('_firecrawl_data') or {}
        return jsonify(_map_company_profile(name, firecrawl)), 200

    # Claim the fetch (merge keeps any stale profile visible to no one — the
    # freshness checks above already rejected it) and fill in the background.
    try:
        cache_ref.set({'name': name, 'fetchStartedAt': now.isoformat()}, merge=True)
    except Exception:
        pass
    import threading as _threading
    _threading.Thread(
        target=_fill_company_profile_background, args=(name, slug), daemon=True
    ).start()
    return jsonify({'pending': True}), 202


@mobile_bp.route('/meeting-prep/preview', methods=['POST'])
@require_firebase_auth
def meeting_prep_preview():
    """Free, read-only LinkedIn lookup so the app can confirm WHO a meeting prep
    is about before spending 15 credits on the full research job. Reuses the same
    `enrich_linkedin_profile` the prep itself runs (coffee_chat_prep.py:105) — no
    credit charge, no research, no PDF. Returns just enough for a confirm card."""
    data = request.get_json() or {}
    url = (data.get('linkedinUrl') or '').strip().rstrip('/')
    url = url.split('?')[0].split('#')[0]
    if 'linkedin.com/in/' not in url.lower():
        return jsonify({'ok': False, 'error': 'Enter a LinkedIn profile URL (linkedin.com/in/…)'}), 400

    try:
        # Engine-first (2026-08-30): the PDL enrich this reused 401s on the
        # retired key, so the confirm card ALWAYS said not-found. Coresignal
        # collects by shorthand; the warehouse answers free for people the
        # engine already met; the prep's full run uses the same door.
        from app.services.linkedin_enrich import enrich_linkedin_profile_engine
        c = enrich_linkedin_profile_engine(url, db=get_db()) or {}
    except Exception:
        c = {}

    if not c or not (c.get('fullName') or c.get('firstName')):
        # Not an error — the URL may just be too new/sparse for the provider.
        # The app falls back to letting the user prep anyway with a typed name.
        return jsonify({'ok': True, 'found': False}), 200

    edu = c.get('educationArray') or []
    school = ''
    if edu and isinstance(edu[0], dict):
        school = edu[0].get('school') or edu[0].get('name') or ''

    name = c.get('fullName') or f"{c.get('firstName', '')} {c.get('lastName', '')}".strip()
    return jsonify({
        'ok': True,
        'found': True,
        'name': name,
        'title': c.get('jobTitle') or '',
        'company': c.get('company') or '',
        'location': c.get('location') or '',
        'school': school,
    }), 200


# ---------------------------------------------------------------------------
# Draft jobs — the async swipe→draft flow (PLAN-instant-feel.md Phase 2).
# POST returns a job id in <1s; the pipeline runs on a bounded background
# pool streaming real stage updates into users/{uid}/draftJobs/{job_id};
# GET polls that doc. The web app keeps using the synchronous /api/prompt-
# search — these routes are mobile-only.
# ---------------------------------------------------------------------------

@mobile_bp.route('/draft-jobs', methods=['POST'])
@require_firebase_auth
def create_draft_job_route():
    from app.services.feature_flags import PDL_OUTAGE_ACTIVE
    if PDL_OUTAGE_ACTIVE:
        return jsonify({
            'error': 'service_unavailable',
            'message': 'Contact search temporarily unavailable.',
            'code': 'PDL_OUTAGE',
        }), 503

    db = get_db()
    if db is None:
        return jsonify({'error': 'Database unavailable'}), 503

    data = request.get_json(silent=True) or {}
    prompt = (data.get('prompt') or '').strip()
    if not prompt or len(prompt) < 3 or len(prompt) > 500:
        return jsonify({'error': 'Prompt must be 3-500 characters'}), 400

    uid = request.firebase_user['uid']

    # Anti-burst guard (2026-07-14). The client used to pace drafting with a
    # swipe-stamina batch + multi-hour cooldown — but that was React state, so a
    # force-quit handed you a fresh batch and it enforced nothing. It's gone;
    # credits are the only meter now. Credits bound the MONTH, though, not the
    # MINUTE: nothing stopped someone firing thirty cold emails in ninety seconds,
    # which is how a sending domain gets burned.
    #
    # So the pacing that actually mattered moves here, where it can't be
    # bypassed. This is a deliverability guard, not a paywall — it's deliberately
    # loose enough that a normal session never touches it.
    #
    # Exempt the App Store reviewer. A reviewer hammers a feature to test it, and
    # a 429 mid-demo reads as a broken/limited app — an easy rejection. The demo
    # account never sends real outreach that could burn a domain, so the
    # deliverability rationale doesn't apply to it.
    is_reviewer = (request.firebase_user.get('email') or '').strip().lower() in REVIEWER_EMAILS
    try:
        window_start = datetime.now(timezone.utc) - timedelta(seconds=DRAFT_BURST_WINDOW_SECONDS)
        recent = (
            db.collection('users').document(uid).collection('draftJobs')
            # createdAt, NOT created_at — draft_jobs.py writes it camelCase.
            # Querying the snake_case name matches zero docs, so the guard would
            # have looked fine and silently never fired.
            .where('createdAt', '>=', window_start)
            .limit(DRAFT_BURST_MAX + 1)
            .get()
        )
        if not is_reviewer and len(recent) >= DRAFT_BURST_MAX:
            mins = max(1, DRAFT_BURST_WINDOW_SECONDS // 60)
            return jsonify({
                'error': 'rate_limited',
                'code': 'DRAFT_BURST',
                'message': (
                    f'That\'s {DRAFT_BURST_MAX} drafts in {mins} minutes — give them a '
                    'moment to send. Outreach lands better spaced out anyway.'
                ),
            }), 429
    except Exception:
        # A guard that can't read must not block real work.
        logger.exception('draft burst check failed for uid=%s', uid)

    from app.services.draft_jobs import create_draft_job
    state = create_draft_job(
        db,
        user_id=request.firebase_user['uid'],
        user_email=request.firebase_user.get('email'),
        auth_display_name=(request.firebase_user or {}).get('name') or '',
        data=data,
    )
    return jsonify(state), 202


@mobile_bp.route('/draft-jobs/<job_id>', methods=['GET'])
@require_firebase_auth
def get_draft_job_route(job_id):
    db = get_db()
    if db is None:
        return jsonify({'error': 'Database unavailable'}), 503
    if not re.match(r'^[A-Za-z0-9_-]{8,160}$', job_id or ''):
        return jsonify({'error': 'Invalid job id'}), 400

    snap = (
        db.collection('users')
        .document(request.firebase_user['uid'])
        .collection('draftJobs')
        .document(job_id)
        .get()
    )
    if not snap.exists:
        return jsonify({'error': 'Job not found'}), 404

    from app.services.draft_jobs import public_job_state
    return jsonify(public_job_state(job_id, snap.to_dict() or {})), 200


@mobile_bp.get('/scout/progress')
@require_firebase_auth
def scout_progress():
    """Poll target for one in-flight action='ask' turn (Rylan 2026-09-16).

    The app fires this every ~1s while /scout/ask's blocking call is still
    in flight, and shows `label` instead of a static "Scout is thinking"
    bubble. _scout_ask_brain writes this doc via handle_chat's event_emitter
    hook and deletes it in a finally block once the turn ends, so 'idle'
    covers both "never started" and "already finished" - the app just stops
    polling once /scout/ask itself returns, whichever comes first.
    """
    db = get_db()
    if db is None:
        return jsonify({'status': 'idle'}), 200
    ask_id = (request.args.get('askId') or '').strip()[:64]
    if not ask_id:
        return jsonify({'error': 'askId required'}), 400
    uid = request.firebase_user['uid']
    try:
        snap = db.collection('users').document(uid).collection('scoutProgress').document(ask_id).get()
        if not snap.exists:
            return jsonify({'status': 'idle'}), 200
        d = snap.to_dict() or {}
        return jsonify({'status': d.get('status') or 'idle', 'label': d.get('label') or ''}), 200
    except Exception:
        return jsonify({'status': 'idle'}), 200


@mobile_bp.get('/scout/active')
@require_firebase_auth
def scout_active_jobs():
    """Re-hydration for the Scout tab (SCOUT-ACTION-CONTRACT.md): the app's
    in-flight action cards survive a full app kill by re-reading what's
    actually still running server-side. Thin aggregator over job state that
    already lives in Firestore — draft jobs + auto-applies in progress."""
    db = get_db()
    uid = request.firebase_user['uid']
    items = []
    # Shared zombie cutoff for BOTH kinds (hoisted out of the draft-jobs try
    # so the auto-apply filter never sees an undefined name if that block
    # fails early).
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=8)
    try:
        draft_jobs = (
            db.collection('users').document(uid).collection('draftJobs')
            .where('status', 'in', ['queued', 'running'])
            .limit(10).stream()
        )
        for d in draft_jobs:
            x = d.to_dict() or {}
            # Zombie filter: jobs that died mid-run keep status 'running'
            # forever. Only re-hydrate work that heartbeat'd recently —
            # anything older flashes as a card then instantly dies (the
            # glitch Rylan screenshotted 2026-07-08).
            ts = x.get('updatedAt') or x.get('createdAt')
            if not ts or ts < cutoff:
                continue
            items.append({
                'jobRef': {'kind': 'draft_job', 'id': d.id},
                'title': (x.get('prompt') or 'Drafting outreach')[:80],
                'stageLabel': x.get('stageLabel') or '',
                'startedAt': x.get('createdAt').isoformat() if x.get('createdAt') else None,
            })
    except Exception:
        pass
    try:
        aa = (
            db.collection('users').document(uid).collection('autoApplyJobs')
            .where('status', 'in', ['queued', 'running'])
            .limit(10).stream()
        )
        for d in aa:
            x = d.to_dict() or {}
            # Same zombie filter as draft jobs (it was missing here): a
            # browserless session that dies mid-fill leaves status 'running'
            # / stage 'filling_form' forever — Rylan's tab showed a wall of
            # weeks-old "Applying:" cards the day re-hydration learned to
            # render this kind (2026-07-09). Fields are snake_case and may
            # be ISO strings on this collection; coerce before comparing.
            ts = x.get('updated_at') or x.get('created_at')
            if isinstance(ts, str):
                try:
                    ts = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                except ValueError:
                    ts = None
            if ts is not None and ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if not ts or ts < cutoff:
                continue
            items.append({
                'jobRef': {'kind': 'auto_apply', 'id': d.id},
                'title': f"Applying: {x.get('job_title') or 'a job'} · {x.get('company') or ''}"[:80],
                'stageLabel': x.get('stage') or '',
                'startedAt': x.get('created_at'),
            })
    except Exception:
        pass
    try:
        # Meeting preps started from Scout chat (run_meeting_prep) — the last
        # jobRef kind the contract names. Preps finish in ~60s, so the same
        # 8-minute cutoff doubles as the zombie filter for docs whose worker
        # thread died mid-build. Timestamps on this collection are ISO strings.
        preps = (
            db.collection('users').document(uid).collection('coffee-chat-preps')
            .where('status', '==', 'processing')
            .limit(5).stream()
        )
        for d in preps:
            x = d.to_dict() or {}
            ts = x.get('updatedAt') or x.get('createdAt')
            if isinstance(ts, str):
                try:
                    ts = datetime.fromisoformat(ts.replace('Z', '+00:00'))
                except ValueError:
                    ts = None
            if ts is not None and ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if not ts or ts < cutoff:
                continue
            name = (x.get('contactName') or '').strip()
            items.append({
                'jobRef': {'kind': 'meeting_prep', 'id': d.id},
                'title': (f'Meeting prep: {name}' if name else 'Building your meeting prep')[:80],
                'stageLabel': x.get('stageLabel') or '',
                'startedAt': x.get('createdAt'),
            })
    except Exception:
        pass
    return jsonify({'items': items}), 200


@mobile_bp.route('/scout/ask', methods=['POST'])
@require_firebase_auth
def scout_ask():
    """SCOUT-ACTION-CONTRACT.md translator, v0 (app-team reference build,
    2026-07-08 — Nick's brain replaces/merges with this behind the same
    envelope). Handles the two action types the app can't fake client-side:

      find_contacts   — search WITHOUT drafting (web-Find parity): semantic
                        parse (same cached LLM parser the draft pipeline
                        uses) → PDL search → warmth-ordered preview. NO
                        emails in the payload and NO credits charged — the
                        reveal/charge happens only if the user drafts.
      draft_outreach  — creates a draft job (existing pipeline) and returns
                        its jobRef in the SAME response, per the contract.

    Everything else (clarify, receipts, apply, prep) stays app-side until
    the real brain lands. Say-text follows the honesty rule: no claimed
    work without the matching action attached.
    """
    db = get_db()
    if db is None:
        return jsonify({'error': 'Database unavailable'}), 503
    data = request.get_json(silent=True) or {}
    ask = (data.get('ask') or '').strip()
    ask_id = (data.get('askId') or '').strip()
    action = (data.get('action') or '').strip()
    params = data.get('params') or {}
    if not ask or len(ask) > 500 or action not in ('find_contacts', 'draft_outreach', 'classify', 'ask'):
        return jsonify({'error': 'Bad request'}), 400

    if action == 'ask':
        # Phase 1 (2026-07-09): the real brain behind the contract envelope.
        # handle_chat(surface="mobile") runs the full tool loop
        # (find_contacts, draft_outreach_emails, run_meeting_prep,
        # auto_apply_to_job, workflow reads...) and _scout_ask_contract
        # translates its envelope into SCOUT-ACTION-CONTRACT
        # {say, actions[], askId}. The three legacy RPC actions below stay
        # untouched: the app in the field still calls them.
        return _scout_ask_brain(db, ask, ask_id, data)

    if action == 'classify':
        # The intelligence layer for ANY ask: LLM classification with the
        # app's regex ladder as its instant fast-path / offline fallback.
        from app.services.scout_intent import classify_scout_ask
        result = classify_scout_ask(db, ask)
        return jsonify({'askId': ask_id, 'classification': result}), 200

    if action == 'draft_outreach':
        from app.services.feature_flags import PDL_OUTAGE_ACTIVE
        if PDL_OUTAGE_ACTIVE:
            return jsonify({
                'say': "Contact search is briefly down for maintenance — try again in a few minutes.",
                'actions': [], 'askId': ask_id,
                'error': {'code': 'pdl_outage', 'detail': ''},
            }), 200
        from app.services.draft_jobs import create_draft_job
        batch = max(1, min(int(params.get('count') or 1), 5))
        prompt = (params.get('prompt') or ask)[:500]
        state = create_draft_job(
            db,
            user_id=request.firebase_user['uid'],
            user_email=request.firebase_user.get('email'),
            auth_display_name=(request.firebase_user or {}).get('name') or '',
            data={'prompt': prompt, 'batchSize': batch, 'mode': 'draft', 'swipe_id': ask_id or None},
        )
        who = 'person' if batch == 1 else f'{batch} people'
        return jsonify({
            'say': f'On it — drafting {who} now. Watch the card; drafts land in your Inbox.',
            'actions': [{
                'type': 'draft_outreach',
                'params': {'prompt': prompt, 'count': batch},
                'needsConfirm': False,
                'jobRef': {'kind': 'draft_job', 'id': state.get('jobId')},
                'results': None,
            }],
            'askId': ask_id,
        }), 200

    # ---- find_contacts: search-only, web-Find parity ----
    from app.services.prompt_parser import parse_search_prompt_structured
    role = (params.get('role') or '').strip()
    company = (params.get('company') or '').strip()
    location = (params.get('location') or '').strip()
    if not (role and company):
        parsed = parse_search_prompt_structured(ask) or {}
        companies = parsed.get('companies') or []
        if not company and companies:
            company = (companies[0] or {}).get('name') or ''
        titles = parsed.get('title_variations') or []
        if not role:
            role = titles[0] if titles else ''
        locs = parsed.get('locations') or []
        if not location and locs:
            location = locs[0] or ''
    if not company:
        return jsonify({
            'say': 'Which company should I look at?',
            'actions': [], 'askId': ask_id,
            'error': {'code': 'needs_company', 'detail': ''},
        }), 200
    # Engine search (2026-08-27): Coresignal replaced PDL here. The PDL key is
    # retired and its calls came back 401 UNAUTHORIZED, which the old code read
    # as "nobody matched" and told the user to broaden the role. Search-only:
    # no email lookups, no user charge; the reveal happens at draft time.
    # No location means a nationwide search; no role means anyone at the firm.
    from app.services import coresignal_client
    parsed_for_engine = {
        'companies': [{'name': company}],
        'title_variations': ([role] if role else []),
        'locations': ([location] if location else []),
    }
    try:
        _fut = _scout_find_pool.submit(
            coresignal_client.search_contacts_from_prompt,
            parsed_for_engine, 5,
        )
        contacts, _rl, _already, _meta = _fut.result(timeout=_SCOUT_FIND_BUDGET_S)
        contacts = contacts or []
        # Free warehouse feed: every collected profile is a future instant hit.
        if contacts:
            try:
                from app.services.firm_cache.writer import cache_pdl_contacts
                cache_pdl_contacts(contacts, shape="app", async_write=True)
            except Exception:
                pass
    except _FuturesTimeout:
        # Orphan the slow search (it finishes into cache); don't pin the UI.
        return jsonify({
            'say': f"That search is taking longer than usual, nothing was charged. "
                   f"Try again in a moment, or narrow it (a role at {company}).",
            'actions': [], 'askId': ask_id,
            'error': {'code': 'search_timeout', 'detail': ''},
        }), 200
    except Exception:
        contacts = []
    items = []
    for c in contacts[:5]:
        name = f"{c.get('FirstName', '')} {c.get('LastName', '')}".strip() or c.get('Name') or ''
        if not name:
            continue
        items.append({
            'name': name,
            'title': c.get('Title') or c.get('JobTitle') or '',
            'company': c.get('Company') or company,
            'linkedinUrl': c.get('LinkedIn') or '',
            # deliberately NO email — reveal happens on draft, where it's charged
        })
    say = (
        f"Found {len(items)} at {company}" + (f" in {location}" if location else '') +
        ", tap Draft and I'll write to the best matches."
        if items else
        f"I couldn't find anyone at {company} just now. Try the full company name. Nothing was charged."
    )
    return jsonify({
        'say': say,
        'actions': ([{
            'type': 'find_contacts',
            'params': {'role': role, 'company': company, 'location': location},
            'needsConfirm': False,
            'jobRef': None,
            'results': {'kind': 'contacts', 'items': items},
        }] if items else []),
        'askId': ask_id,
    }), 200


# ---------------------------------------------------------------------------
# Phase 1 (2026-07-09): the real Scout brain behind /scout/ask action='ask'.
# One brain, two dialects - handle_chat serves web verbatim; this translator
# owns the mobile dialect (docs/SCOUT-ACTION-CONTRACT.md).
# ---------------------------------------------------------------------------

# Per-uid sliding-window throttle for brain turns (same shape and budget as
# the web /chat backstop: 20/min, 200/hr). Separate dict from transcription
# so a voice ask (transcribe + ask) doesn't double-bill one budget.
_ask_hits: dict = {}
_ASK_PER_MIN = 20
_ASK_PER_HOUR = 200


def _ask_rate_limited(uid: str) -> bool:
    import time as _t
    from collections import deque as _deque
    now = _t.time()
    dq = _ask_hits.get(uid)
    if dq is None:
        dq = _deque()
        _ask_hits[uid] = dq
    while dq and now - dq[0] > 3600:
        dq.popleft()
    if len(dq) >= _ASK_PER_HOUR:
        return True
    if sum(1 for t in dq if now - t <= 60) >= _ASK_PER_MIN:
        return True
    dq.append(now)
    if len(_ask_hits) > 5000:
        for k in [k for k, v in _ask_hits.items() if not v or now - v[-1] > 3600]:
            _ask_hits.pop(k, None)
    return False


# Brain error codes -> the contract's error enum (SCOUT-ACTION-CONTRACT.md
# Errors section). Codes with no user affordance on the app (CONSENT_REQUIRED,
# COUNT_REQUIRED, CONTACT_NOT_FOUND...) are deliberately unmapped: the brain
# already speaks them conversationally in `say`, which is the whole point of
# its never-dead-end error contract.
_ASK_ERROR_CODES = {
    'INSUFFICIENT_CREDITS': 'insufficient_credits',
    'GMAIL_NOT_CONNECTED': 'gmail_disconnected',
    'LIMIT_REACHED': 'cap_reached',
    'TIER_REQUIRED': 'cap_reached',
    # Auto-apply's own blockers. Each one has a real next move on the app, so
    # each one gets a code the Feed can turn into a button instead of leaving
    # the user with a sentence and nowhere to tap.
    'PROFILE_REQUIRED': 'application_profile_required',
    'WORK_AUTH_REQUIRED': 'application_profile_required',
    'INELIGIBLE': 'job_not_auto_appliable',
    'JOB_NOT_FOUND': 'job_not_auto_appliable',
    'AUTOAPPLY_UNAVAILABLE': 'auto_apply_unavailable',
}

# Zero-result receipts from these tools surface as error.no_results so the
# app can add its widen-the-search affordance.
_ASK_SEARCH_TOOLS = ('find_contacts', 'find_hiring_managers')


def _ask_contact_items(rows) -> list:
    """Brain contact/manager receipts -> contract card items (camelCase)."""
    items = []
    for c in rows or []:
        if not isinstance(c, dict):
            continue
        name = (c.get('name') or '').strip()
        if not name:
            continue
        item = {
            'name': name,
            'title': c.get('title') or '',
            'company': c.get('company') or '',
            'linkedinUrl': c.get('linkedin_url') or c.get('linkedinUrl') or '',
        }
        if c.get('email'):
            item['email'] = c['email']
        if c.get('contact_id'):
            item['contactId'] = c['contact_id']
        items.append(item)
    return items


def _ask_action(type_: str, params: dict, *, job_ref=None, results=None) -> dict:
    return {
        'type': type_,
        'params': params,
        'needsConfirm': False,
        'jobRef': job_ref,
        'results': results,
    }


def _scout_ask_contract(env: dict, ask_id: str) -> dict:
    """Translate the brain's web envelope into the contract envelope.

    Input: handle_chat's {tool, message, navigate, cta, chat_id,
    tool_results:[{name, result}]}. Output: {say, actions[], askId,
    conversationId, error?}. Execution receipts become typed actions with
    results/jobRef in the SAME response (the contract's reversible-actions
    rule); navigation and cta chips become an additive 'navigate' action
    (documented contract extension - the app drops unknown types today and
    learns to render this one in Phase 2).
    """
    say = (env.get('message') or '').strip()
    # The web path strips em dashes on labels and args; the mobile say text
    # was slipping through unstripped (Rylan's screenshot, 2026-08-30).
    try:
        from app.services.scout_assistant_service import _strip_em_dashes
        say = _strip_em_dashes(say)
    except Exception:
        pass
    actions: list = []
    error = None

    for tr in (env.get('tool_results') or []):
        if not isinstance(tr, dict):
            continue
        name = tr.get('name')
        res = tr.get('result')
        if not isinstance(res, dict):
            continue
        code = res.get('code')
        if code and error is None and code in _ASK_ERROR_CODES:
            error = {
                'code': _ASK_ERROR_CODES[code],
                'detail': str(res.get('error') or '')[:200],
            }
        if name in _ASK_SEARCH_TOOLS:
            rows = res.get('contacts') if name == 'find_contacts' else res.get('managers')
            items = _ask_contact_items(rows)
            if items:
                actions.append(_ask_action(
                    name,
                    {'company': res.get('company') or ''},
                    results={'kind': 'contacts', 'items': items},
                ))
            elif res.get('count') == 0 and not code and error is None:
                error = {'code': 'no_results', 'detail': ''}
        elif name == 'draft_outreach_emails':
            drafted = res.get('drafted') or []
            items = [
                {
                    'name': d.get('name') or '',
                    'title': '',
                    'company': d.get('company') or '',
                    'contactId': d.get('contact_id') or '',
                }
                for d in drafted
                if isinstance(d, dict) and d.get('name')
            ]
            if items:
                actions.append(_ask_action(
                    'draft_outreach',
                    {'count': len(items)},
                    results={'kind': 'contacts', 'items': items},
                ))
        elif name == 'run_meeting_prep':
            if res.get('started') and res.get('prep_id'):
                actions.append(_ask_action(
                    'meeting_prep',
                    {'contact_name': res.get('contact_name') or ''},
                    job_ref={'kind': 'meeting_prep', 'id': res['prep_id']},
                ))
        elif name == 'get_followups':
            # Scout offering "want me to send the follow-up?" with nothing to
            # tap is the old failure in a new place: the offer has to arrive
            # as an action. One chip for the person actually going cold; the
            # rest of the list stays in `say` where it reads better.
            rows = [
                f for f in (res.get('followups') or [])
                if isinstance(f, dict) and (f.get('contact_name') or '').strip()
            ]
            if rows:
                top = rows[0]
                actions.append(_ask_action(
                    'followup',
                    {
                        'contactName': top.get('contact_name') or '',
                        'company':     top.get('company') or '',
                        'draftReady':  bool(top.get('draft_ready')),
                        'waitingDays': top.get('waiting_days'),
                        'more':        max(0, len(rows) - 1),
                    },
                ))
        elif name == 'find_jobs':
            # Job results had no translation at all, so every "find me jobs"
            # and every find-then-apply turn lost its list on the way to the
            # app: the user saw prose about jobs they could not see or tap.
            items = []
            for j in (res.get('jobs') or []):
                if not isinstance(j, dict) or not j.get('job_id'):
                    continue
                items.append({
                    'jobId':      str(j.get('job_id')),
                    'title':      j.get('title') or '',
                    'company':    j.get('company') or '',
                    'location':   j.get('location') or '',
                    'autoApplyEligible': bool(j.get('auto_apply_eligible')),
                })
            if items:
                actions.append(_ask_action(
                    'find_jobs',
                    {'query': res.get('query') or ''},
                    results={'kind': 'jobs', 'items': items},
                ))
            elif res.get('count') == 0 and not code and error is None:
                error = {'code': 'no_results', 'detail': ''}
        elif name == 'auto_apply_to_job' and not code:
            # The tracking id is auto_apply_id: /auto-apply/<id>/status is
            # keyed on it, NOT on job_id. Sending job_id here (the old first
            # choice) made every status poll 404, so a submission the backend
            # really ran showed the user nothing.
            apply_id = res.get('auto_apply_id') or res.get('autoApplyId') or ''
            if apply_id:
                actions.append(_ask_action(
                    'auto_apply',
                    {
                        'jobId':   str(res.get('job_id') or ''),
                        'title':   res.get('job_title') or '',
                        'company': res.get('company') or '',
                        'status':  res.get('status') or 'queued',
                    },
                    job_ref={'kind': 'auto_apply', 'id': str(apply_id)},
                ))

    # ── Two receipts the model can legitimately produce that must not reach
    # the app as-is.
    #
    # 1. A turn can call auto_apply_to_job twice for the same job (observed:
    #    "apply me to a data analyst job" fired it once per candidate job and
    #    the server deduped both to one application). The submission is
    #    already deduped server-side, so a second card is a lie about how
    #    much happened AND a second status poller on one id.
    # 2. Offering "draft a follow-up to Sarah" in the same breath as the
    #    receipt saying a follow-up to Sarah was just drafted.
    deduped: list = []
    seen_apply: set = set()
    drafted = any(a['type'] == 'draft_outreach' for a in actions)
    for a in actions:
        if a['type'] == 'auto_apply':
            key = (a.get('jobRef') or {}).get('id')
            if key in seen_apply:
                continue
            seen_apply.add(key)
        if a['type'] == 'followup' and drafted:
            continue
        deduped.append(a)
    actions = deduped

    nav = env.get('navigate') if env.get('tool') == 'navigate' else None
    cta = env.get('cta')
    if nav and nav.get('route'):
        actions.append(_ask_action('navigate', {
            'route': nav['route'],
            'prefill': nav.get('prefill') or {},
            'label': (nav.get('reasoning') or '')[:80],
        }))
    elif isinstance(cta, dict) and cta.get('route'):
        actions.append(_ask_action('navigate', {
            'route': cta['route'],
            'prefill': cta.get('prefill') or {},
            'label': (cta.get('label') or '')[:80],
        }))

    payload = {
        'say': say,
        'actions': actions,
        'askId': ask_id,
        'conversationId': env.get('chat_id'),
    }
    if error:
        payload['error'] = error
    return payload


def _scout_ask_brain(db, ask: str, ask_id: str, data: dict):
    """action='ask': one full brain turn, contract envelope out.

    askId idempotency rides the swipe_idempotency store (the contract names
    it as the pattern to reuse): a replayed ask returns the stored response
    verbatim instead of re-running a credit-spending turn.
    """
    from app.services import swipe_idempotency as idem
    from app.utils.async_runner import run_async
    from app.services.scout_assistant_service import scout_assistant_service

    uid = request.firebase_user['uid']
    if _ask_rate_limited(uid):
        return jsonify({'error': 'Rate limit exceeded. Try again shortly.'}), 429

    idem_key = f'scoutask-{ask_id}' if ask_id and idem.valid_swipe_id(f'scoutask-{ask_id}') else None
    if idem_key:
        outcome, stored = idem.claim(db, uid, idem_key)
        if outcome == 'completed' and stored:
            payload, status = idem.replay_response(stored)
            return jsonify(payload), status
        if outcome != 'run':
            return jsonify({
                'say': 'Still working on that one - give me a second.',
                'actions': [], 'askId': ask_id,
            }), 200

    # Profile bits for the brain's live-context block. Best-effort: a missing
    # or malformed user doc degrades to defaults, never a 500.
    user_name, tier, credits, max_credits = 'there', 'free', 0, 300
    try:
        prof = (db.collection('users').document(uid).get().to_dict()) or {}
        if isinstance(prof, dict):
            user_name = str(prof.get('name') or prof.get('firstName')
                            or (request.firebase_user or {}).get('name') or 'there')
            tier = str(prof.get('subscriptionTier') or prof.get('tier') or 'free').lower()
            credits = int(prof.get('credits') or 0)
            max_credits = int(prof.get('maxCredits') or 300)
    except Exception:
        pass
    try:
        from app.routes.scout_assistant import _fetch_user_context
        user_context = _fetch_user_context(uid) or {}
    except Exception:
        user_context = {}

    conversation_id = (data.get('conversationId') or '').strip() or None

    # Live progress, mobile side (Rylan 2026-09-16): the app's request here is
    # a single blocking call with no streaming, so while it's in flight the
    # user saw one static "thinking" bubble regardless of what Scout was
    # actually doing. handle_chat/`_call_scout_tools` already emit tool_start
    # /tool_end with human labels for the web SSE path (_tool_label,
    # _tool_result_summary) - this reuses that exact same hook, just writing
    # to a Firestore doc instead of an SSE queue, so GET /scout/progress can
    # poll it. Best-effort: a write failure here must never break the turn.
    progress_ref = (
        db.collection('users').document(uid).collection('scoutProgress').document(ask_id)
        if ask_id else None
    )

    def _write_progress(event: str, event_data: dict):
        if progress_ref is None:
            return
        try:
            if event == 'tool_start':
                progress_ref.set({
                    'status': 'running',
                    'label': event_data.get('label') or 'Working on it',
                    'updated_at': datetime.utcnow(),
                }, merge=True)
            elif event == 'tool_end':
                progress_ref.set({
                    'status': 'running',
                    'label': event_data.get('summary') or 'Working on it',
                    'updated_at': datetime.utcnow(),
                }, merge=True)
        except Exception:
            pass

    try:
        env = run_async(scout_assistant_service.handle_chat(
            message=ask,
            conversation_history=[],
            current_page='/dashboard',
            user_name=user_name,
            tier=tier,
            credits=credits,
            max_credits=max_credits,
            user_context=user_context,
            uid=uid,
            chat_id=conversation_id,
            surface='mobile',
            event_emitter=_write_progress if progress_ref is not None else None,
        ))
        payload = _scout_ask_contract(env or {}, ask_id)
        if idem_key:
            idem.complete(db, uid, idem_key, payload, 200)
        return jsonify(payload), 200
    except Exception as exc:
        print(f'[ScoutAskBrain] turn failed: {type(exc).__name__}: {exc}')
        if idem_key:
            idem.fail(db, uid, idem_key)
        return jsonify({
            'say': "That one tripped me up - mind trying it again?",
            'actions': [], 'askId': ask_id,
            'error': {'code': 'internal', 'detail': ''},
        }), 200
    finally:
        # The main response always arrives before or with this cleanup, so a
        # poller either sees 'done' once and stops, or (network race) just
        # gets a 404-shaped empty read next time - never a stuck 'running'.
        if progress_ref is not None:
            try:
                progress_ref.delete()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Voice overhaul P2: server-side transcription (steady-forging-popcorn plan)
# ---------------------------------------------------------------------------

# Per-uid sliding-window throttle for audio transcription (same shape as
# resume.py's _parse_rate_limited, keyed by uid — this route is authed).
_transcribe_hits: dict = {}
_TRANSCRIBE_PER_MIN = 20
_TRANSCRIBE_PER_HOUR = 200


def _transcribe_rate_limited(uid: str) -> bool:
    import time as _t
    from collections import deque as _deque
    now = _t.time()
    dq = _transcribe_hits.get(uid)
    if dq is None:
        dq = _deque()
        _transcribe_hits[uid] = dq
    while dq and now - dq[0] > 3600:
        dq.popleft()
    if len(dq) >= _TRANSCRIBE_PER_HOUR:
        return True
    if sum(1 for t in dq if now - t <= 60) >= _TRANSCRIBE_PER_MIN:
        return True
    dq.append(now)
    if len(_transcribe_hits) > 5000:
        for k in [k for k, v in _transcribe_hits.items() if not v or now - v[-1] > 3600]:
            _transcribe_hits.pop(k, None)
    return False


def _wav_duration_seconds(data: bytes) -> float:
    """Duration from the WAV header — never assume a sample rate (iOS may
    ignore the 16kHz hint and emit 44.1k). Non-WAV (.caf) or unparsable
    headers return 0.0 and rely on the byte cap alone."""
    try:
        if len(data) < 44 or data[:4] != b'RIFF' or data[8:12] != b'WAVE':
            return 0.0
        import struct
        # Walk chunks to find fmt then data (don't assume canonical layout).
        pos = 12
        byte_rate = 0
        data_size = 0
        while pos + 8 <= len(data):
            cid = data[pos:pos + 4]
            size = struct.unpack('<I', data[pos + 4:pos + 8])[0]
            if cid == b'fmt ':
                byte_rate = struct.unpack('<I', data[pos + 16:pos + 20])[0]
            elif cid == b'data':
                data_size = size
                break
            pos += 8 + size + (size % 2)
        if byte_rate <= 0:
            return 0.0
        return (data_size or max(0, len(data) - pos - 8)) / float(byte_rate)
    except Exception:
        return 0.0


@mobile_bp.route('/scout/transcribe-ask', methods=['POST'])
@require_firebase_auth
def scout_transcribe_ask():
    """Voice overhaul P2 (steady-forging-popcorn plan): one round trip that
    turns the RAW audio of an ask into an understanding. Apple's on-device
    ASR mangles firm names unboundedly (Moelis arrived as Molly's / Molise /
    Mose in one field evening); here the audio is transcribed server-side
    with a firm-vocabulary biasing prompt, then classified by the same cached
    intent brain as action='classify'.

    Transcription and classification stay two separable calls on purpose —
    when the real brain lands behind SCOUT-ACTION-CONTRACT.md, this route
    becomes "transcribe, then call the brain" with the envelope unchanged.

    Degrades, never dies: any transcription failure falls back to the
    client-provided Apple transcript (transcript_source='apple') so the
    caller still gets a classification from one round trip.
    """
    import io
    import json as _json
    import time as _t

    from app.services.firm_vocabulary import transcription_prompt
    from app.services.metering import log_transcription_usage
    from app.services.openai_client import get_openai_client
    from app.services.scout_intent import classify_scout_ask

    db = get_db()
    if db is None:
        return jsonify({'error': 'Database unavailable'}), 503
    uid = request.firebase_user['uid']
    if _transcribe_rate_limited(uid):
        return jsonify({'error': 'Too many requests'}), 429

    ask_id = (request.form.get('askId') or '').strip()[:80]
    apple_transcript = (request.form.get('apple_transcript') or '').strip()[:500]
    try:
        hints = _json.loads(request.form.get('hint_companies') or '[]')
        hints = [str(h)[:60] for h in hints[:25] if h]
    except Exception:
        hints = []

    f = request.files.get('audio')
    filename = (f.filename or '') if f else ''
    if not f or not filename.lower().endswith(('.wav', '.caf')):
        return jsonify({'error': 'Bad or missing audio file'}), 400
    data = f.read()
    if not data or len(data) > 10 * 1024 * 1024:
        return jsonify({'error': 'Audio too large'}), 400
    duration = _wav_duration_seconds(data)
    if duration > 90:
        return jsonify({'error': 'Audio too long'}), 400

    transcript = apple_transcript
    source = 'apple'
    client = get_openai_client()
    model = 'gpt-4o-mini-transcribe'
    if client is not None:
        t0 = _t.time()
        try:
            resp = client.audio.transcriptions.create(
                model=model,
                file=('ask.wav', io.BytesIO(data)),
                language='en',
                prompt=transcription_prompt(hints),
                response_format='json',
                timeout=6,
            )
            text = (getattr(resp, 'text', '') or '').strip()
            if text:
                transcript = text[:500]
                source = 'audio'
            log_transcription_usage(
                model, getattr(resp, 'usage', None),
                latency_ms=int((_t.time() - t0) * 1000), status='ok',
            )
        except Exception as e:
            log_transcription_usage(
                model, None,
                latency_ms=int((_t.time() - t0) * 1000),
                status='error', error_msg=f'{type(e).__name__}',
            )

    if not transcript:
        return jsonify({'error': 'No transcript available'}), 400

    classification = classify_scout_ask(db, transcript)
    return jsonify({
        'askId': ask_id,
        'transcript': transcript,
        'transcript_source': source,
        'classification': classification,
    }), 200


# ---------------------------------------------------------------------------
# People deck: describe who you want to reach, swipe the results.
#
# Thin shims, deliberately. Everything about how a search runs, what it costs
# and when, lives in services/people_deck.py; these two only translate HTTP to
# that and back. See that module's docstring for why search is free and reveal
# is not.
# ---------------------------------------------------------------------------

@mobile_bp.route('/people-search', methods=['POST'])
@require_firebase_auth
def people_search_route():
    from app.services.feature_flags import PDL_OUTAGE_ACTIVE
    if PDL_OUTAGE_ACTIVE:
        return jsonify({
            'error': 'service_unavailable',
            'message': 'People search is temporarily unavailable.',
            'code': 'PDL_OUTAGE',
        }), 503

    from app.services.people_deck import BATCH_SIZE, search_people
    data = request.get_json(silent=True) or {}
    payload, status = search_people(
        user_id=request.firebase_user['uid'],
        user_email=request.firebase_user.get('email'),
        prompt=data.get('prompt') or '',
        limit=data.get('limit') or BATCH_SIZE,
        cursor=(data.get('cursor') or None),
    )
    return jsonify(payload), status


@mobile_bp.route('/people-search/reveal', methods=['POST'])
@require_firebase_auth
def people_search_reveal_route():
    from app.services.people_deck import reveal_person
    data = request.get_json(silent=True) or {}
    action = (data.get('action') or 'draft').strip().lower()
    if action not in ('draft', 'send'):
        action = 'draft'
    payload, status = reveal_person(
        user_id=request.firebase_user['uid'],
        user_email=request.firebase_user.get('email'),
        auth_display_name=(request.firebase_user or {}).get('name') or '',
        deck_id=(data.get('deck_id') or '').strip(),
        person_id=(data.get('person_id') or '').strip(),
        action=action,
    )
    return jsonify(payload), status


@mobile_bp.route('/resume/name', methods=['PATCH'])
@require_firebase_auth
def rename_resume_route():
    """Rename the resume on file (Rylan 2026-09-08).

    resumeFileName is the name recruiters see on the email attachment
    (emails.py reads it) and the name the app shows; the stored object keeps
    its path, so nothing else moves. The original extension is preserved and
    characters that break attachment headers are dropped.
    """
    import os as _os
    import re as _re
    data = request.get_json(silent=True) or {}
    name = str(data.get('name') or '').strip()[:80]
    if not name:
        return jsonify({'error': 'name required'}), 400
    db = get_db()
    if not db:
        return jsonify({'error': 'Database not initialized'}), 500
    uid = request.firebase_user['uid']
    ref = db.collection('users').document(uid)
    u = ref.get().to_dict() or {}
    if not u.get('resumeUrl'):
        return jsonify({'error': 'No resume on file'}), 404
    ext = _os.path.splitext(u.get('resumeFileName') or '')[1] or '.pdf'
    name = _re.sub(r'[\\/:*?"<>|\r\n]+', ' ', name).strip()
    if not name.lower().endswith(ext.lower()):
        name = f"{name}{ext}"
    ref.update({'resumeFileName': name})
    return jsonify({'ok': True, 'name': name}), 200
