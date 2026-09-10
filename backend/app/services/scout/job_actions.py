"""Scout execute actions on jobs: hiring managers, cover letters, resume fit.

Completes the in-chat workflow set:
- find_hiring_managers_for_chat: same recruiter_finder pipeline the Find
  page's Hiring Managers tab uses (RECRUITER_CREDIT_COST per manager),
  results saved to users/{uid}/recruiters so the tab shows them.
- cover_letter_for_chat: same generator + credit cost as the job board's
  cover letter endpoint; the letter text returns straight into the chat.
- tailor_resume_for_chat: resume-vs-job fit analysis with concrete edit
  suggestions. Free (one utility-model call). Built fresh: the website's
  old edited-resume endpoint was removed in the 2026-05-26 cleanup.
- improve_resume_for_target: the same edit-suggestion shape with no job
  posting required, for "tailor my resume to investment banking" style asks.
  Falls back to the user's own stated goal (targetRoles / dreamCompanies)
  when the chat gave no target (2026-09-09, Rylan: general asks answered in
  prose with no way to apply anything).

All three resolve job context the same way: an explicit description beats a
job_id from the catalog (description_raw) beats scraping the job_url.
Failures return a structured `code`; nothing raises.
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

logger = logging.getLogger(__name__)

_UTILITY_MODEL = os.getenv("SCOUT_UTILITY_MODEL", "gpt-4.1-mini")
_MAX_MANAGERS = 5


def _db():
    try:
        from app.extensions import get_db
        return get_db()
    except Exception:
        return None


def _text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("name", "display", "label", "city", "text"):
            v = value.get(key)
            if isinstance(v, str) and v.strip():
                return v
        return ""
    return "" if value is None else str(value)


def _resolve_job_context(
    db,
    job_id: str = "",
    job_url: str = "",
    job_title: str = "",
    company: str = "",
    job_description: str = "",
) -> Tuple[str, str, str, str]:
    """Fill (job_title, company, job_description, job_url) from the best
    available source. Explicit description wins; then the catalog doc; then
    a live scrape of the URL (slowest, last resort)."""
    job_id = (job_id or "").strip()
    job_url = (job_url or "").strip()
    if job_id and db is not None:
        try:
            snap = db.collection("jobs").document(job_id).get()
            if snap.exists:
                d = snap.to_dict() or {}
                job_title = job_title or _text(d.get("title"))
                company = company or _text(d.get("company"))
                job_description = job_description or _text(d.get("description_raw"))
                job_url = job_url or _text(d.get("apply_url"))
        except Exception as e:
            logger.warning("[ScoutJobs] job doc read failed: %s", e)
    if not job_description and job_url:
        try:
            from app.services.firecrawl_client import extract_job_posting
            parsed = extract_job_posting(job_url) or {}
            job_title = job_title or _text(parsed.get("title"))
            company = company or _text(parsed.get("company"))
            job_description = job_description or _text(parsed.get("description"))
        except Exception as e:
            logger.warning("[ScoutJobs] job url parse failed: %s", e)
    return job_title.strip(), company.strip(), job_description.strip(), job_url


# ---------------------------------------------------------------------------
# Hiring managers
# ---------------------------------------------------------------------------


def find_hiring_managers_for_chat(
    uid: str,
    company: str,
    job_title: str = "",
    location: str = "",
    count: int = 3,
) -> Dict[str, Any]:
    """Find hiring managers / recruiters for a role and save them to the
    Hiring Manager tracker. Costs RECRUITER_CREDIT_COST per manager found."""
    empty = {"count": 0, "managers": []}
    if not uid:
        return {**empty, "error": "sign in required", "code": "AUTH_REQUIRED"}
    company = (company or "").strip()
    if not company:
        return {**empty, "error": "company required", "code": "BAD_REQUEST"}
    from app.services.industry_terms import is_industry_not_company, industry_rejection_message
    if is_industry_not_company(company):
        # An industry is a filter, never an employer (2026-07-09: PDL with
        # company="investment banking" returned shells and dead profiles).
        # The model relays the message and asks for a firm - never spends.
        return {**empty, "error": industry_rejection_message(company),
                "code": "INDUSTRY_NOT_COMPANY"}
    db = _db()
    if db is None:
        return {**empty, "error": "database unavailable", "code": "UNAVAILABLE"}

    from app.routes.job_board import RECRUITER_CREDIT_COST
    from app.services.auth import check_and_reset_credits, deduct_credits_atomic
    from app.services.recruiter_finder import find_hiring_manager

    try:
        count = max(1, min(int(count or 3), _MAX_MANAGERS))
    except (TypeError, ValueError):
        count = 3

    user_ref = db.collection("users").document(uid)
    user_doc = user_ref.get()
    if not user_doc.exists:
        return {**empty, "error": "account not found", "code": "AUTH_REQUIRED"}
    user_data = user_doc.to_dict() or {}
    credits_available = check_and_reset_credits(user_ref, user_data)
    if credits_available < RECRUITER_CREDIT_COST:
        return {
            **empty,
            "error": (
                f"needs at least {RECRUITER_CREDIT_COST} credits, "
                f"only {credits_available} available"
            ),
            "code": "INSUFFICIENT_CREDITS",
        }
    count = min(count, credits_available // RECRUITER_CREDIT_COST)

    user_resume = user_data.get("resumeParsed") or {}
    try:
        result = find_hiring_manager(
            company_name=company,
            job_title=(job_title or "").strip(),
            location=(location or "").strip() or None,
            max_results=count,
            generate_emails=False,
            user_resume=user_resume,
            resume_text=user_data.get("resumeText") or "",
            role_type="hiring_manager",
            uid=uid,
        ) or {}
    except Exception as e:
        logger.warning("[ScoutJobs] find_hiring_manager failed: %s", e)
        return {**empty, "error": "hiring manager search failed", "code": "INTERNAL"}

    if result.get("error"):
        return {**empty, "error": str(result["error"]), "code": "INTERNAL"}

    raw_managers = result.get("hiringManagers") or []
    if not raw_managers:
        return {**empty, "company": company}

    charged = RECRUITER_CREDIT_COST * len(raw_managers)
    success, _bal = deduct_credits_atomic(uid, charged, "find_hiring_manager")
    if not success:
        return {**empty, "error": "insufficient credits", "code": "INSUFFICIENT_CREDITS"}

    saved = _save_managers_to_tracker(db, uid, raw_managers, company, job_title)

    managers = []
    for m in raw_managers:
        email = _text(m.get("Email") or m.get("WorkEmail")).strip()
        managers.append({
            "name": f"{_text(m.get('FirstName')).strip()} {_text(m.get('LastName')).strip()}".strip(),
            "title": _text(m.get("Title")).strip(),
            "company": _text(m.get("Company")).strip() or company,
            "email": "" if email.lower() == "not available" else email,
            "linkedin_url": _text(m.get("LinkedIn")).strip(),
        })
    return {
        "count": len(managers),
        "managers": managers,
        "company": company,
        "saved_to_tracker": saved,
        "credits_charged": charged,
    }


def _save_managers_to_tracker(db, uid, raw_managers, company, job_title) -> int:
    """Persist found managers into users/{uid}/recruiters (the Hiring
    Managers tab's collection), deduped by email / LinkedIn. Mirrors the
    /save-recruiters route's document shape."""
    try:
        ref = db.collection("users").document(uid).collection("recruiters")
        existing_emails, existing_linkedins = set(), set()
        for snap in ref.stream():
            d = snap.to_dict() or {}
            if d.get("email"):
                existing_emails.add(str(d["email"]).strip().lower())
            if d.get("linkedinUrl"):
                existing_linkedins.add(str(d["linkedinUrl"]).strip())
        now = datetime.now(timezone.utc).isoformat()
        saved = 0
        for m in raw_managers:
            email = _text(m.get("Email") or m.get("WorkEmail")).strip()
            if email.lower() == "not available":
                email = ""
            linkedin = _text(m.get("LinkedIn")).strip()
            if (email and email.lower() in existing_emails) or (
                linkedin and linkedin in existing_linkedins
            ):
                continue
            doc = {
                "firstName": _text(m.get("FirstName")).strip(),
                "lastName": _text(m.get("LastName")).strip(),
                "linkedinUrl": linkedin,
                "email": email,
                "company": _text(m.get("Company")).strip() or company,
                "jobTitle": _text(m.get("Title")).strip(),
                "location": _text(m.get("City")).strip(),
                "dateAdded": now,
                "status": "Not Contacted",
                "createdAt": now,
                "updatedAt": now,
                "source": "scout_chat",
            }
            if job_title:
                doc["associatedJobTitle"] = job_title
            ref.document().set(doc)
            saved += 1
            if email:
                existing_emails.add(email.lower())
            if linkedin:
                existing_linkedins.add(linkedin)
        return saved
    except Exception as e:
        logger.warning("[ScoutJobs] tracker save failed: %s", e)
        return 0


# ---------------------------------------------------------------------------
# Cover letter
# ---------------------------------------------------------------------------


async def cover_letter_for_chat(
    uid: str,
    job_id: str = "",
    job_url: str = "",
    job_title: str = "",
    company: str = "",
    job_description: str = "",
) -> Dict[str, Any]:
    """Generate a cover letter for a specific job, same engine + credit cost
    as the job board endpoint. Returns the letter text for the chat."""
    if not uid:
        return {"error": "sign in required", "code": "AUTH_REQUIRED"}
    db = _db()
    if db is None:
        return {"error": "database unavailable", "code": "UNAVAILABLE"}

    import asyncio

    from app.routes.job_board import (
        COVER_LETTER_CREDIT_COST,
        generate_cover_letter_with_ai,
    )
    from app.services.auth import (
        check_and_reset_credits,
        deduct_credits_atomic,
        refund_credits_atomic,
    )

    job_title, company, job_description, job_url = await asyncio.to_thread(
        _resolve_job_context, db, job_id, job_url, job_title, company, job_description,
    )
    if not job_description:
        return {
            "error": "no job description available for this job; ask the user to paste the posting URL or description",
            "code": "NEEDS_JOB_DESCRIPTION",
        }

    user_ref = db.collection("users").document(uid)
    user_doc = await asyncio.to_thread(user_ref.get)
    if not user_doc.exists:
        return {"error": "account not found", "code": "AUTH_REQUIRED"}
    user_data = user_doc.to_dict() or {}
    user_resume = user_data.get("resumeParsed") or {}
    if not user_resume:
        return {
            "error": "resume required: upload one in Account Settings first",
            "code": "NEEDS_RESUME",
        }
    credits_available = await asyncio.to_thread(
        check_and_reset_credits, user_ref, user_data)
    if credits_available < COVER_LETTER_CREDIT_COST:
        return {
            "error": f"needs {COVER_LETTER_CREDIT_COST} credits, only {credits_available} available",
            "code": "INSUFFICIENT_CREDITS",
        }
    success, _bal = await asyncio.to_thread(
        deduct_credits_atomic, uid, COVER_LETTER_CREDIT_COST, "cover_letter")
    if not success:
        return {"error": "insufficient credits", "code": "INSUFFICIENT_CREDITS"}

    try:
        result = await generate_cover_letter_with_ai(
            user_resume=user_resume,
            job_description=job_description[:12000],
            job_title=job_title,
            company=company,
        )
    except Exception as e:
        logger.warning("[ScoutJobs] cover letter generation failed: %s", e)
        await asyncio.to_thread(
            refund_credits_atomic, uid, COVER_LETTER_CREDIT_COST, "cover_letter_failed")
        return {"error": "cover letter generation failed; credits refunded", "code": "INTERNAL"}

    # The generator returns {"content", "highlights", "tone"}.
    letter = ""
    if isinstance(result, dict):
        letter = _text(result.get("content") or result.get("coverLetter") or result.get("cover_letter"))
    elif isinstance(result, str):
        letter = result
    if not letter.strip():
        await asyncio.to_thread(
            refund_credits_atomic, uid, COVER_LETTER_CREDIT_COST, "cover_letter_failed")
        return {"error": "cover letter generation failed; credits refunded", "code": "INTERNAL"}

    return {
        "cover_letter": letter.strip(),
        "job_title": job_title,
        "company": company,
        "job_url": job_url,
        "credits_charged": COVER_LETTER_CREDIT_COST,
    }


# ---------------------------------------------------------------------------
# Resume fit / tailoring
# ---------------------------------------------------------------------------

_TAILOR_PROMPT = """You are a resume coach for candidates applying to a specific job.
Compare the resume against the job posting and return ONLY valid JSON:
{
  "fit_score": <0-100 integer, honest not flattering>,
  "verdict": "<one sentence: overall fit and the single biggest lever>",
  "strengths": ["<up to 3 specific matches between resume and posting>"],
  "gaps": ["<up to 3 specific requirements the resume does not show>"],
  "edits": [
    {"section": "<resume section>", "current": "<what it says now, short>",
     "suggested": "<rewritten line targeting this job, concrete>"}
  ]
}
At most 4 edits. Every suggestion must be grounded in something already on
the resume (reframed, quantified, reordered); never invent experience."""


def tailor_resume_for_chat(
    uid: str,
    job_id: str = "",
    job_url: str = "",
    job_title: str = "",
    company: str = "",
    job_description: str = "",
) -> Dict[str, Any]:
    """Score the user's resume against a specific job and suggest concrete
    edits. Free: one utility-model call."""
    if not uid:
        return {"error": "sign in required", "code": "AUTH_REQUIRED"}
    db = _db()
    if db is None:
        return {"error": "database unavailable", "code": "UNAVAILABLE"}

    job_title, company, job_description, job_url = _resolve_job_context(
        db, job_id, job_url, job_title, company, job_description)
    if not job_description:
        return {
            "error": "no job description available; ask the user to paste the posting URL or description",
            "code": "NEEDS_JOB_DESCRIPTION",
        }

    user_doc = db.collection("users").document(uid).get()
    if not user_doc.exists:
        return {"error": "account not found", "code": "AUTH_REQUIRED"}
    user_data = user_doc.to_dict() or {}
    resume_text = user_data.get("resumeText") or ""
    if not resume_text.strip():
        parsed = user_data.get("resumeParsed")
        resume_text = json.dumps(parsed)[:8000] if parsed else ""
    if not resume_text.strip():
        return {
            "error": "resume required: upload one in Account Settings first",
            "code": "NEEDS_RESUME",
        }

    try:
        from app.services.openai_client import get_openai_client
        client = get_openai_client()
        header = f"JOB: {job_title} at {company}\n" if (job_title or company) else ""
        response = client.chat.completions.create(
            model=_UTILITY_MODEL,
            messages=[
                {"role": "system", "content": _TAILOR_PROMPT},
                {"role": "user", "content": (
                    f"{header}JOB POSTING:\n{job_description[:8000]}\n\n"
                    f"RESUME:\n{resume_text[:8000]}"
                )},
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
        )
        analysis = json.loads(response.choices[0].message.content or "{}")
    except Exception as e:
        logger.warning("[ScoutJobs] resume tailoring failed: %s", e)
        return {"error": "resume analysis failed", "code": "INTERNAL"}

    return {
        "fit_score": analysis.get("fit_score"),
        "verdict": _text(analysis.get("verdict")),
        "strengths": analysis.get("strengths") or [],
        "gaps": analysis.get("gaps") or [],
        "edits": (analysis.get("edits") or [])[:4],
        "job_title": job_title,
        "company": company,
        "job_url": job_url,
    }


_IMPROVE_PROMPT = """You are a resume coach. Given a resume and a target
(an industry, role type, or set of companies the candidate is aiming for),
suggest concrete edits that make the resume read stronger for that target.
Return ONLY valid JSON:
{
  "verdict": "<one sentence: current fit for this target and the single biggest lever>",
  "strengths": ["<up to 3 things already on the resume that fit this target>"],
  "gaps": ["<up to 3 things this target usually looks for that the resume does not show>"],
  "edits": [
    {"section": "<resume section>", "current": "<what it says now, short>",
     "suggested": "<rewritten line targeting this target, concrete>"}
  ]
}
At most 5 edits. Every suggestion must be grounded in something already on
the resume (reframed, quantified, reordered); never invent experience,
employers, dates, or numbers."""


def _resolve_target_focus(user_data: Dict[str, Any], focus: str) -> str:
    """What to tailor toward: the chat's own words first, else the
    onboarding goal (targetRoles / dreamCompanies). Empty when neither
    exists, so the caller asks instead of guessing a target."""
    focus = (focus or "").strip()
    if focus:
        return focus
    parts = []
    roles = user_data.get("targetRoles")
    if isinstance(roles, list) and roles:
        parts.append(" / ".join(str(r) for r in roles[:3] if r))
    firms = user_data.get("dreamCompanies")
    if isinstance(firms, list) and firms:
        parts.append("aiming for " + ", ".join(str(f) for f in firms[:3] if f))
    return "; ".join(p for p in parts if p)


def improve_resume_for_target(uid: str, focus: str = "") -> Dict[str, Any]:
    """General resume coaching, no job posting required. Free: one
    utility-model call. Same edit shape as tailor_resume_for_chat so the
    app renders both the same way."""
    if not uid:
        return {"error": "sign in required", "code": "AUTH_REQUIRED"}
    db = _db()
    if db is None:
        return {"error": "database unavailable", "code": "UNAVAILABLE"}

    user_doc = db.collection("users").document(uid).get()
    if not user_doc.exists:
        return {"error": "account not found", "code": "AUTH_REQUIRED"}
    user_data = user_doc.to_dict() or {}
    resume_text = user_data.get("resumeText") or ""
    if not resume_text.strip():
        parsed = user_data.get("resumeParsed")
        resume_text = json.dumps(parsed)[:8000] if parsed else ""
    if not resume_text.strip():
        return {
            "error": "resume required: upload one in Account Settings first",
            "code": "NEEDS_RESUME",
        }

    target = _resolve_target_focus(user_data, focus)
    if not target:
        return {
            "error": "no target given; ask what role, industry, or companies to tailor toward",
            "code": "NEEDS_FOCUS",
        }

    try:
        from app.services.openai_client import get_openai_client
        client = get_openai_client()
        response = client.chat.completions.create(
            model=_UTILITY_MODEL,
            messages=[
                {"role": "system", "content": _IMPROVE_PROMPT},
                {"role": "user", "content": (
                    f"TARGET: {target}\n\nRESUME:\n{resume_text[:8000]}"
                )},
            ],
            response_format={"type": "json_object"},
            temperature=0.3,
        )
        analysis = json.loads(response.choices[0].message.content or "{}")
    except Exception as e:
        logger.warning("[ScoutJobs] resume improvement failed: %s", e)
        return {"error": "resume analysis failed", "code": "INTERNAL"}

    return {
        "target": target,
        "verdict": _text(analysis.get("verdict")),
        "strengths": analysis.get("strengths") or [],
        "gaps": analysis.get("gaps") or [],
        "edits": (analysis.get("edits") or [])[:5],
    }
