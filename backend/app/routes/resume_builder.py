"""Onboarding resume builder: free Harvard one-pager for users without a resume."""
from flask import Blueprint, request, jsonify, Response
from firebase_admin import firestore

from app.services.resume_builder_service import (
    ResumeBuilderError,
    canonical_to_parsed_info,
    canonical_to_text,
    generate_canonical_resume,
)
from app.services.resume_renderer import (
    CanonicalResume,
    from_resume_parsed,
    render_html,
    render_one_page,
)
from app.services.resume_capabilities import build_resume_metadata
from app.routes.resume import save_resume_to_firebase
from ..extensions import require_firebase_auth, get_db

resume_builder_bp = Blueprint('resume_builder', __name__, url_prefix='/api/resume-builder')

GENERATION_CAP = 10
EDITOR_DAILY_CAP = 30
RESUME_FILENAME = "Offerloop_Resume.pdf"


def _check_and_count_attempt(uid: str):
    """Enforce the lifetime cap; count the attempt up front (success or not).

    Returns an error response tuple if capped, else None.
    """
    db = get_db()
    ref = db.collection('users').document(uid)
    doc = ref.get()
    used = (doc.to_dict() or {}).get('resumeBuilderGenerations', 0) if doc.exists else 0
    if used >= GENERATION_CAP:
        return jsonify({
            'error': 'generation_limit_reached',
            'message': 'You have used all free resume generations.',
        }), 429
    ref.update({'resumeBuilderGenerations': firestore.Increment(1)})
    return None


def _check_and_count_editor_attempt(uid: str):
    """Daily cap for the in-app resume editor (free, separate from the
    lifetime onboarding cap). Counts the attempt up front, success or not.

    Returns an error response tuple if capped, else None.
    """
    from datetime import datetime, timezone

    db = get_db()
    ref = db.collection('users').document(uid)
    doc = ref.get()
    data = (doc.to_dict() or {}) if doc.exists else {}
    today = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    used = data.get('resumeEditorCount', 0) if data.get('resumeEditorDate') == today else 0
    if used >= EDITOR_DAILY_CAP:
        return jsonify({
            'error': 'editor_limit_reached',
            'message': 'You have hit the daily edit limit. It resets tomorrow.',
        }), 429
    ref.update({'resumeEditorDate': today, 'resumeEditorCount': used + 1})
    return None


def _thin_check(resume: CanonicalResume):
    """Quality bar (mobile contract, 2026-08-10): a resume with neither
    experience nor education entries is an empty one-pager, not a resume.
    Saving one silently was the padded-fiction failure mode, except emptier.
    Returns an error response tuple if too thin, else None."""
    missing = [
        section
        for section, entries in (('experience', resume.experience), ('education', resume.education))
        if not (entries or [])
    ]
    if len(missing) == 2:
        return jsonify({'error': 'profile_too_thin', 'missing': missing}), 422
    return None


def _upload_pdf(uid: str, pdf_bytes: bytes):
    """Upload the generated PDF privately to the same bucket/path uploaded
    resumes use. Returns (url, thumb_url); see app/services/resume_storage.py
    for why this no longer calls make_public (2026-09-08)."""
    try:
        from app.services.resume_storage import store_resume
        out = store_resume(uid, RESUME_FILENAME, pdf_bytes, 'application/pdf')
        return out.get('url'), out.get('thumb_url')
    except Exception as e:
        print(f"[ResumeBuilder] Storage upload failed: {e}")
        return None, None


def _render_save_respond(uid: str, resume: CanonicalResume):
    result = render_one_page(resume)
    resume_url, thumb_url = _upload_pdf(uid, result.pdf_bytes)
    parsed = canonical_to_parsed_info(resume)
    metadata = build_resume_metadata(url=resume_url or '', filename=RESUME_FILENAME, extension='pdf')
    if thumb_url:
        metadata['resumeThumbUrl'] = thumb_url
    save_resume_to_firebase(uid, canonical_to_text(resume), resume_url, parsed, metadata)
    return jsonify({'success': True, 'resumeUrl': resume_url, 'parsed': parsed})


@resume_builder_bp.route('/generate', methods=['POST'])
@require_firebase_auth
def generate():
    uid = request.firebase_user['uid']
    data = request.get_json() or {}
    prompt = (data.get('prompt') or '').strip()
    if not prompt:
        return jsonify({'error': 'prompt is required'}), 400
    # The in-app resume editor is free with its own daily cap; only the
    # onboarding builder burns the lifetime generation cap.
    if data.get('context') == 'editor':
        capped = _check_and_count_editor_attempt(uid)
    else:
        capped = _check_and_count_attempt(uid)
    if capped:
        return capped
    try:
        resume = generate_canonical_resume(prompt, data.get('previous'))
    except ResumeBuilderError as e:
        return jsonify({'error': str(e)}), 502
    return jsonify({'success': True, 'resume': resume.model_dump(), 'html': render_html(resume)})


@resume_builder_bp.route('/current', methods=['GET'])
@require_firebase_auth
def current():
    """Return the user's stored resume in builder (canonical) form + HTML
    preview, so the in-app editor can start a refine loop from it. Returns
    resume: null when the user has no stored resume yet."""
    uid = request.firebase_user['uid']
    db = get_db()
    doc = db.collection('users').document(uid).get()
    parsed = (doc.to_dict() or {}).get('resumeParsed') if doc.exists else None
    if not isinstance(parsed, dict) or not parsed:
        return jsonify({'success': True, 'resume': None, 'html': None})
    try:
        resume = from_resume_parsed(parsed)
        return jsonify({'success': True, 'resume': resume.model_dump(), 'html': render_html(resume)})
    except Exception as e:
        print(f"[ResumeBuilder] current conversion failed: {e}")
        return jsonify({'success': True, 'resume': None, 'html': None})


def _linkedin_resume_or_error(uid: str):
    """Shared by the save and preview LinkedIn routes: load the enriched
    profile, map it deterministically, thin-guard it. Returns
    (resume, None) or (None, error_response_tuple)."""
    db = get_db()
    doc = db.collection('users').document(uid).get()
    linkedin_parsed = (doc.to_dict() or {}).get('linkedinResumeParsed') if doc.exists else None
    if not linkedin_parsed or not linkedin_parsed.get('name'):
        return None, (jsonify({'error': 'No LinkedIn profile data on file. Enrich first.'}), 400)
    try:
        resume = from_resume_parsed(linkedin_parsed)
    except Exception as e:
        print(f"[ResumeBuilder] from-linkedin mapping failed: {e}")
        return None, (jsonify({'error': 'Could not build a resume from your LinkedIn profile.'}), 502)
    thin = _thin_check(resume)
    if thin:
        return None, thin
    return resume, None


@resume_builder_bp.route('/from-linkedin', methods=['POST'])
@require_firebase_auth
def from_linkedin():
    """Build + save a resume from the linkedinResumeParsed the enrichment
    route already stored. Frontend calls /api/enrich-linkedin-onboarding first.

    No generation-cap charge (changed 2026-08-10, agreed with mobile): the
    mapping is deterministic with no LLM call, and the cap exists to bound
    LLM spend, which lives on /generate and the editor path only."""
    uid = request.firebase_user['uid']
    resume, err = _linkedin_resume_or_error(uid)
    if err:
        return err
    try:
        return _render_save_respond(uid, resume)
    except Exception as e:
        print(f"[ResumeBuilder] from-linkedin failed: {e}")
        return jsonify({'error': 'Could not build a resume from your LinkedIn profile.'}), 502


# The polish pass runs the deterministic LinkedIn mapping through the same
# Harvard-rubric generator the website's builder uses, as a refinement of the
# mapped draft. Instructions only rewrite wording; the FACTUAL DISCIPLINE
# rules in the system prompt forbid inventing employers, dates, or numbers.
_POLISH_INSTRUCTIONS = (
    "Polish this resume draft, which was mapped mechanically from the user's "
    "LinkedIn profile. Rewrite every bullet to meet the rubric: strong varied "
    "action verbs, accomplishment over duty, consistent tense, 8 to 18 words. "
    "Tighten section content to one page. Keep every employer, school, title, "
    "date, location, and number EXACTLY as given; add nothing that is not in "
    "the draft."
)


@resume_builder_bp.route('/from-linkedin/preview', methods=['POST'])
@require_firebase_auth
def from_linkedin_preview():
    """Render-only variant (mobile contract, 2026-08-10): same deterministic
    mapping as /from-linkedin but saves NOTHING. The user approves in the app
    and /finalize performs the only save.

    polish=true (2026-08-10): after mapping, run the draft through the same
    Harvard-rubric LLM pass the website's builder uses, so the mobile build
    matches web quality. The LLM call burns the lifetime generation cap; if
    capped or the LLM fails, fall back to the unpolished mapping (polished:
    false) rather than failing the preview."""
    uid = request.firebase_user['uid']
    resume, err = _linkedin_resume_or_error(uid)
    if err:
        return err
    polished = False
    if (request.get_json(silent=True) or {}).get('polish'):
        capped = _check_and_count_attempt(uid)
        if not capped:
            try:
                resume = generate_canonical_resume(_POLISH_INSTRUCTIONS, resume.model_dump())
                polished = True
            except ResumeBuilderError as e:
                print(f"[ResumeBuilder] preview polish failed, serving unpolished: {e}")
    return jsonify({
        'success': True,
        'resume': resume.model_dump(),
        'html': render_html(resume),
        'polished': polished,
    })


@resume_builder_bp.route('/download', methods=['POST'])
@require_firebase_auth
def download():
    """Stream the current draft as a PDF download. Pure render: no storage
    write, no generation-cap charge, no onboarding side effects — users can
    download, keep refining, and still hit finalize."""
    data = request.get_json() or {}
    try:
        resume = CanonicalResume.model_validate(data.get('resume') or {})
    except Exception:
        return jsonify({'error': 'Invalid resume payload'}), 400
    try:
        result = render_one_page(resume)
    except Exception as e:
        print(f"[ResumeBuilder] download render failed: {e}")
        return jsonify({'error': 'Could not render your resume. Try again.'}), 502
    return Response(
        result.pdf_bytes,
        mimetype='application/pdf',
        headers={'Content-Disposition': f'attachment; filename="{RESUME_FILENAME}"'},
    )


@resume_builder_bp.route('/finalize', methods=['POST'])
@require_firebase_auth
def finalize():
    uid = request.firebase_user['uid']
    data = request.get_json() or {}
    try:
        resume = CanonicalResume.model_validate(data.get('resume') or {})
    except Exception:
        return jsonify({'error': 'Invalid resume payload'}), 400
    try:
        return _render_save_respond(uid, resume)
    except Exception as e:
        print(f"[ResumeBuilder] finalize failed: {e}")
        return jsonify({'error': 'Could not save your resume. Try again.'}), 502
