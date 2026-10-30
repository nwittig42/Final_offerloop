"""
Email generation and drafting routes
"""
import os
import base64
import html
import requests
from datetime import datetime
from flask import Blueprint, request, jsonify, Response
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
import mimetypes
import re

from app.config import GMAIL_SCOPES
from ..extensions import require_firebase_auth
from app.services.reply_generation import batch_generate_emails
from app.services.gmail_client import get_gmail_service_for_user, get_user_gmail_service_strict
from app.services.resume_parser import extract_text_from_pdf_bytes
from app.utils.url_validator import validate_fetch_url, UnsafeURLError
from app.utils.seniority import classify_seniority
from app.utils.warmth_scoring import score_contacts_for_email
from app.utils.users import get_outreach_email, merge_persona_fields
from ..extensions import get_db
from email_templates import get_template_instructions
from app.services.eml_builder import build_eml, eml_filename


def _persist_warmth_on_send(db, uid, contact_email, warmth_info, job_title):
    """Write warmthTier, warmthScore, and seniorityBucket on the contact doc.

    Called after email generation so the Phase 2 aggregation scanner can
    bucket contacts by warmth and seniority. Idempotent (merge update).
    Must never block the email-send flow — all exceptions are swallowed.
    """
    try:
        email_clean = (contact_email or "").strip().lower()
        if not email_clean:
            return
        contacts_ref = db.collection("users").document(uid).collection("contacts")
        matches = list(contacts_ref.where("email", "==", email_clean).limit(1).stream())
        if not matches:
            return
        update = {
            "seniorityBucket": classify_seniority(job_title),
        }
        if warmth_info:
            update["warmthTier"] = warmth_info.get("tier", "unknown")
            update["warmthScore"] = warmth_info.get("score", 0)
        matches[0].reference.update(update)
    except Exception as exc:
        import logging
        logging.getLogger("emails").debug(
            "warmth persist failed for %s: %s", contact_email, exc
        )

def _base_contact_fields(r, c, body):
    """Shared contact_data enrichment: personalization metadata, word count,
    lead-hook-usage detection, and contact identity fields.

    Used by both the Gmail draft branch and the no-Gmail fallback branch of
    generate_and_draft() so the Firestore contact doc gets the same
    personalization/analytics fields regardless of delivery mode.
    """
    fields = {}

    personalization = r.get("personalization")
    if personalization:
        fields["personalizationLabel"] = personalization.get("label", "")
        fields["personalizationType"] = personalization.get("commonality_type", "")
        fields["leadType"] = personalization.get("lead_type", "")
        fields["commonalityTypes"] = personalization.get("commonality_types", [])
        fields["warmthTierFinal"] = personalization.get("warmth_tier_final", "")

    # Word count of final email body
    fields["wordCountFinal"] = len(body.split())

    # Check if lead hook content appears in the final body.
    # Uses 3+ consecutive significant words (>=4 chars) from the hook
    # to detect incorporation without false positives from short words.
    lead_hook = (personalization or {}).get("lead_hook", "")
    if lead_hook and len(lead_hook) > 5:
        body_lower = body.lower()
        hook_words = [
            w for w in lead_hook.lower().split()
            if len(w) >= 4 and w not in {"this", "that", "they", "them", "their", "with", "from", "have", "been", "were", "also", "your", "about"}
        ]
        # Check for any 3-consecutive-word window match
        found = False
        for idx in range(len(hook_words) - 2):
            trigram = " ".join(hook_words[idx:idx + 3])
            if trigram in body_lower:
                found = True
                break
        # Fallback: if hook has fewer than 3 significant words,
        # check if any 2-word pair appears
        if not found and len(hook_words) >= 2 and len(hook_words) < 3:
            bigram = " ".join(hook_words[:2])
            found = bigram in body_lower
        fields["leadHookUsedInBody"] = found
    else:
        fields["leadHookUsedInBody"] = False

    # Add contact fields from the original contact data
    if c.get("FirstName"):
        fields["firstName"] = c["FirstName"]
    if c.get("LastName"):
        fields["lastName"] = c["LastName"]
    # Build full name for display
    full_name = (
        c.get("full_name")
        or c.get("name")
        or f"{c.get('FirstName', '')} {c.get('LastName', '')}".strip()
        or None
    )
    if full_name:
        fields["name"] = full_name
    if c.get("Company"):
        fields["company"] = c["Company"]
    if c.get("Title") or c.get("jobTitle"):
        fields["jobTitle"] = c.get("Title") or c.get("jobTitle")
    if c.get("LinkedIn") or c.get("linkedinUrl"):
        fields["linkedinUrl"] = c.get("LinkedIn") or c.get("linkedinUrl")
    if c.get("College") or c.get("college"):
        fields["college"] = c.get("College") or c.get("college")
    if c.get("location"):
        fields["location"] = c["location"]
    # pdlId for agentic queue dedup (new in Phase 1).
    if c.get("pdlId"):
        fields["pdlId"] = c.get("pdlId")

    return fields


emails_bp = Blueprint('emails', __name__, url_prefix='/api/emails')
def _infer_mime_type(filename_or_url: str, fallback=("application", "octet-stream")):
    # Try headers first if you already have them, else guess from extension
    guessed, _ = mimetypes.guess_type(filename_or_url)
    if guessed:
        main, sub = guessed.split("/", 1)
        return main, sub
    return fallback


def _normalize_drive_url(url: str) -> str:
    """
    Normalize Google Drive URLs to direct download format.
    Firebase Storage URLs are returned as-is.
    """
    if not url:
        return url
    
    # If it's a Firebase Storage URL, return as-is
    if 'firebasestorage.googleapis.com' in url:
        return url
    
    # Handle Google Drive URLs
    if 'drive.google.com' in url:
        # Extract file ID from various Drive URL formats
        
        # Format: https://drive.google.com/file/d/FILE_ID/view
        match = re.search(r'/file/d/([^/]+)', url)
        if match:
            file_id = match.group(1)
            return f'https://drive.google.com/uc?export=download&id={file_id}'
        
        # Format: https://drive.google.com/open?id=FILE_ID
        match = re.search(r'[?&]id=([^&]+)', url)
        if match:
            file_id = match.group(1)
            return f'https://drive.google.com/uc?export=download&id={file_id}'
    
    # Return original URL if no normalization needed
    return url


@emails_bp.post("/generate-and-draft")
@require_firebase_auth
def generate_and_draft():
    """Generate emails and create Gmail drafts"""
    db = get_db()
    uid = request.firebase_user["uid"]
    payload = request.get_json() or {}
    contacts = payload.get("contacts", [])
    resume_text = payload.get("resumeText", "")
    user_profile = payload.get("userProfile", {})
    career_interest = payload.get("careerInterests")
    fit_context = payload.get("fitContext")  # NEW: Job fit analysis context

    # If frontend didn't send resume text, backfill in priority order:
    # (1) Firestore `resumeText` — saved at upload time, no download cost.
    # (2) Download the PDF from `resumeUrl` and re-extract as last resort.
    print(f"[EmailGen] resume_text from payload: {repr(resume_text)[:100] if resume_text else 'None/empty'} "
          f"(len={len(resume_text) if resume_text else 0})")
    if not resume_text or len(resume_text.strip()) < 50:
        print("[EmailGen] resume_text missing or too short, checking Firestore cache...")
        _user_doc = db.collection("users").document(uid).get()
        _user_data = _user_doc.to_dict() or {}
        _cached_text = _user_data.get("resumeText")
        if _cached_text and len(_cached_text.strip()) >= 50:
            resume_text = _cached_text
            print(f"[EmailGen] Using cached resumeText from user doc ({len(resume_text)} chars)")

    if not resume_text or len(resume_text.strip()) < 50:
        print("[EmailGen] resume_text still missing after cache check, attempting backfill from URL...")
        _resume_url = payload.get("resumeUrl")
        _url_source = "payload" if _resume_url else None
        if not _resume_url:
            _resume_url = user_profile.get("resumeUrl")
            _url_source = "userProfile" if _resume_url else None
        if not _resume_url:
            _user_doc = db.collection("users").document(uid).get()
            _user_data = _user_doc.to_dict() or {}
            _resume_url = _user_data.get("resumeUrl")
            _url_source = "firestore" if _resume_url else None
        print(f"[EmailGen] Resume URL resolved: {_resume_url[:100] if _resume_url else 'None'} "
              f"(source={_url_source})")
        if _resume_url:
            _resume_url = _normalize_drive_url(_resume_url)
            try:
                _resume_url = validate_fetch_url(_resume_url)
                _res = requests.get(_resume_url, timeout=15, headers={"User-Agent": "Offerloop/1.0"})
                _res.raise_for_status()
                _pdf_bytes = _res.content
                print(f"[EmailGen] Resume download: {_res.status_code}, {len(_pdf_bytes)} bytes, "
                      f"content-type={_res.headers.get('content-type', 'unknown')}")
                if len(_pdf_bytes) >= 1024 and b"<html" not in _pdf_bytes[:2048].lower():
                    resume_text = extract_text_from_pdf_bytes(_pdf_bytes)
                    print(f"[EmailGen] Text extraction result: {len(resume_text)} chars, "
                          f"preview={repr(resume_text[:120])}")
                else:
                    print(f"[EmailGen] Resume download unusable: size={len(_pdf_bytes)}, "
                          f"looks_like_html={b'<html' in _pdf_bytes[:2048].lower()}")
            except Exception as e:
                print(f"[EmailGen] Resume download/extraction failed: {e}")
        else:
            print("[EmailGen] No resume URL found in payload, userProfile, or Firestore")

    # Per-user Gmail only. No shared-inbox fallback for user-facing drafts:
    # without an integration we return the generated emails in fallback mode
    # (downloadable .eml on the frontend) instead of erroring.
    gmail_service = get_user_gmail_service_strict(uid)
    fallback_mode = gmail_service is None
    if fallback_mode:
        print(f"[EmailGen] No Gmail integration for {uid}: fallback delivery mode")

    # ✅ FIX: Check if contacts already have emails to avoid duplicate generation
    # Filter out contacts that already have emailSubject and emailBody
    contacts_needing_emails = []
    contacts_with_emails = []
    for i, contact in enumerate(contacts):
        has_subject = contact.get('emailSubject') or contact.get('email_subject')
        has_body = contact.get('emailBody') or contact.get('email_body')
        if has_subject and has_body:
            contacts_with_emails.append((i, contact))
        else:
            contacts_needing_emails.append((i, contact))
    
    if contacts_with_emails:
        print(f"📧 Skipping email generation for {len(contacts_with_emails)} contacts that already have emails")
    
    # Load email template: prefer request body override, fall back to Firestore stored default
    user_doc = db.collection("users").document(uid).get()
    user_data = user_doc.to_dict() or {}
    # Prefer the user's .edu for the outreach identity (signature + mailto). This
    # flows into both this endpoint's signature builder and the LLM body via
    # batch_generate_emails(user_profile). Falls back to the primary email.
    _outreach_email = get_outreach_email(user_data)
    if _outreach_email:
        user_profile = {**(user_profile or {}), "email": _outreach_email}
    # Professional persona: userType + currentRole/currentCompany ride along
    # from the same user-doc fetch so batch_generate_emails can drop the
    # student framing for working professionals.
    user_profile = merge_persona_fields(dict(user_profile or {}), user_data)
    request_template = payload.get("emailTemplate") or {}
    stored_template = user_data.get("emailTemplate") or {}
    # Use request template if it has any meaningful values, otherwise use stored
    template = request_template if any(request_template.get(k) for k in ("purpose", "stylePreset", "customInstructions")) else stored_template
    purpose = template.get("purpose")
    style_preset = template.get("stylePreset")
    custom_instructions = (template.get("customInstructions") or "").strip()[:4000]
    template_instructions = get_template_instructions(purpose=purpose, style_preset=style_preset, custom_instructions=custom_instructions)
    signoff_phrase = (template.get("signoffPhrase") or stored_template.get("signoffPhrase") or "").strip() or "Best,"
    signature_block = (template.get("signatureBlock") or stored_template.get("signatureBlock") or "").strip()[:500]
    signoff_config = {"signoffPhrase": signoff_phrase, "signatureBlock": signature_block}
    draft_resume_filename = (
        payload.get("resumeFileName")
        or user_profile.get("resumeFileName")
        or user_data.get("resumeFileName")
        or "Resume.pdf"
    )

    # Pull resumeParsed from Firestore (includes LinkedIn enrichment + resume data)
    # This gives the email generator rich context: skills, career_interests,
    # extracurriculars, certifications, experience — even for LinkedIn-only users
    resume_parsed = user_data.get("resumeParsed")
    if resume_parsed and isinstance(resume_parsed, dict):
        print(f"[EmailGen] Using resumeParsed from Firestore: name={resume_parsed.get('name')!r}, "
              f"skills={bool(resume_parsed.get('skills'))}, career_interests={resume_parsed.get('career_interests', [])[:3]}, "
              f"experience={len(resume_parsed.get('experience', []))} entries")
    else:
        resume_parsed = None
        print("[EmailGen] No resumeParsed in Firestore")

    # Backfill career interests from resumeParsed if frontend didn't send them
    if not career_interest and resume_parsed and resume_parsed.get("career_interests"):
        career_interest = resume_parsed["career_interests"]
        print(f"[EmailGen] Backfilled career_interests from resumeParsed: {career_interest[:3]}")

    # Read personalNote and dreamCompanies for enhanced personalization
    personal_note = user_data.get("personalNote", "")
    dream_companies = user_data.get("dreamCompanies", [])

    # Perplexity hiring-signal enrichment (opt-in via payload flag). The HM
    # preview flow skips verify_hiring_managers_v2 for speed, which drops the
    # "Hiring now" badge. Re-run it here so the badge populates on drafted
    # rows before send — the review-before-send moment is when the signal
    # actually matters. Adds ~3-5s to draft, gated per-request.
    if payload.get("enrichHiringSignal") and contacts:
        _enrich_ctx = payload.get("enrichContext") or {}
        _enrich_company = _enrich_ctx.get("company") or (contacts[0].get("Company") or contacts[0].get("company") or "")
        _enrich_job_title = _enrich_ctx.get("jobTitle") or ""
        if _enrich_company:
            try:
                from app.services.perplexity_client import verify_hiring_managers_v2
                _verifications = verify_hiring_managers_v2(
                    hms=contacts,
                    company=_enrich_company,
                    job_title=_enrich_job_title,
                )
                for _c, _v in zip(contacts, _verifications):
                    _c["_actively_hiring"] = _v.get("actively_hiring", "unknown")
                    _signal = _v.get("recent_hiring_signal", "")
                    if _signal:
                        _c["_recent_hiring_signal"] = _signal
                _yes_count = sum(1 for _c in contacts if _c.get("_actively_hiring") == "yes")
                print(f"[EmailGen] Perplexity hiring-signal enrichment: {_yes_count}/{len(contacts)} actively hiring")
            except Exception as _perp_err:
                print(f"[EmailGen] Perplexity hiring-signal enrichment failed, continuing: {_perp_err}")

    # Only generate emails for contacts that don't have them
    results = {}
    if contacts_needing_emails:
        # Log if fit context is being used
        if fit_context:
            print(f"📧 Generating emails with fit context: {fit_context.get('job_title', 'Unknown')} at {fit_context.get('company', 'Unknown')}")

        # Extract just the contact dicts for generation
        contacts_to_generate = [contact for _, contact in contacts_needing_emails]
        
        # 1) Generate emails with fit context and user's template/signoff
        auth_display_name = (getattr(request, "firebase_user", None) or {}).get("name") or ""
        warmth_data = score_contacts_for_email(user_data, contacts_to_generate)
        print(f"[EmailGen] Calling batch_generate_emails: resume_text={'present (' + str(len(resume_text)) + ' chars)' if resume_text else 'None/empty'}, "
              f"contacts={len(contacts_to_generate)}")
        generated_results = batch_generate_emails(
            contacts_to_generate,
            resume_text,
            user_profile,
            career_interest,
            fit_context=fit_context,
            pre_parsed_user_info=resume_parsed,
            template_instructions=template_instructions,
            email_template_purpose=purpose,
            resume_filename=draft_resume_filename,
            signoff_config=signoff_config,
            auth_display_name=auth_display_name,
            personal_note=personal_note,
            dream_companies=dream_companies,
            warmth_data=warmth_data,
            uid=uid,
        )
        print(f"🧪 batch_generate_emails returned: type={type(generated_results)}, "
          f"len={len(generated_results) if hasattr(generated_results, '__len__') else 'n/a'}, "
          f"keys={list(generated_results.keys())[:5] if isinstance(generated_results, dict) else 'list'}")
        
        # Map results back to original indices
        for idx, (original_idx, _) in enumerate(contacts_needing_emails):
            result_key = idx
            if isinstance(generated_results, dict):
                result = generated_results.get(result_key) or generated_results.get(str(result_key))
                if result:
                    results[original_idx] = result
    else:
        print(f"📧 All {len(contacts)} contacts already have emails, skipping generation")


    # 2) Create drafts with resume and formatted body
    gmail = gmail_service
    created = []
    draft_ids = []

    # Log which mailbox we're drafting as
    if not fallback_mode:
        try:
            connected_email = gmail.users().getProfile(userId="me").execute().get("emailAddress")
            print(f"📧 Connected Gmail account: {connected_email}")
        except Exception as e:
            connected_email = None
            print(f"⚠️ Could not fetch connected Gmail profile: {e}")
    else:
        connected_email = None

    # user_data already loaded above for email template; use for resume too
    resume_url = (
        payload.get("resumeUrl") or
        user_profile.get("resumeUrl") or
        user_data.get("resumeUrl")
    )
    resume_filename = (
        payload.get("resumeFileName") or
        user_profile.get("resumeFileName") or
        user_data.get("resumeFileName") or
        "Resume.pdf"
    )

    if resume_url:
        resume_url = _normalize_drive_url(resume_url)

    # Download resume once before the loop (avoid N repeated downloads).
    # Skipped in fallback mode: the resume attachment is only used when
    # building the MIME message for a Gmail draft.
    _cached_resume_data = None
    _cached_resume_ctype = None
    if resume_url and not fallback_mode:
        try:
            resume_url = validate_fetch_url(resume_url)
            _resume_res = requests.get(
                resume_url, timeout=15,
                headers={"User-Agent": "Offerloop/1.0"}
            )
            _resume_res.raise_for_status()
            _cached_resume_data = _resume_res.content
            _cached_resume_ctype = _resume_res.headers.get("content-type", "")
            if isinstance(_cached_resume_ctype, bytes):
                _cached_resume_ctype = _cached_resume_ctype.decode("utf-8", errors="ignore")
            _cached_resume_ctype = _cached_resume_ctype.lower()
            # Guard: HTML sharing page instead of file bytes
            if len(_cached_resume_data) < 1024 and b"<html" in _cached_resume_data[:2048].lower():
                print("Resume URL returned HTML (likely a sharing page)")
                _cached_resume_data = None
            # Guard: skip very large attachments
            elif len(_cached_resume_data) > 8 * 1024 * 1024:
                print(f"Resume too large ({len(_cached_resume_data)} bytes) — skipping")
                _cached_resume_data = None
        except Exception as e:
            print(f"Could not download resume from {resume_url}: {e}")
            _cached_resume_data = None

    print(f"👥 Contacts received: {len(contacts)}")
    for i, c in enumerate(contacts):
        # ✅ FIX: Check if contact already has email, otherwise use newly generated email
        # First, check if contact already has emailSubject and emailBody
        existing_subject = c.get('emailSubject') or c.get('email_subject')
        existing_body = c.get('emailBody') or c.get('email_body')
        
        if existing_subject and existing_body:
            # Use existing email
            r = {
                'subject': existing_subject,
                'body': existing_body
            }
            print(f"✅ Using existing email for contact {i}: {c.get('FirstName', 'Unknown')}")
        else:
            # Try to get newly generated email
            r = None
            try:
                if isinstance(results, dict):
                    # handle both string and int keys
                    r = results.get(str(i)) or results.get(i)
                    # handle wrapped shape: {"results": [...]}
                    if r is None and "results" in results and isinstance(results["results"], list):
                        r = results["results"][i] if i < len(results["results"]) else None
                elif isinstance(results, list):
                    r = results[i] if i < len(results) else None
            except Exception as e:
                print(f"⚠️ Error reading generated email at index {i}: {e}")
                r = None

        if not r or not isinstance(r, dict) or not r.get("body") or not r.get("subject"):
            print(
                f"⚠️ Skipping index {i}: no email available (existing or generated) "
                f"for contact {c.get('FirstName', 'Unknown')}"
            )
            continue


        # Be tolerant to various keys
        to_addr = (
            c.get("Email") or c.get("email") or
            c.get("WorkEmail") or c.get("work_email") or
            c.get("PersonalEmail") or c.get("personal_email")
        )
        if not to_addr:
            print(f"⚠️ Skipping contact {i}: no email in {c}")
            continue

        # --- Format email content ---
        body = r["body"].strip()
        if "for context, i've attached my resume below" not in body.lower():
            body += "\n\nFor context, I've attached my resume below."

        # Check if body already ends with a signature (batch_generate_emails includes signature)
        # Look for common closings, user's signoffPhrase, user name, email, university, auth name, signatureBlock lines in last 200 chars
        body_lower = body.lower()
        has_signature = False
        phrase_lower = (signoff_config.get("signoffPhrase") or "").strip().lower()
        sig_block = (signoff_config.get("signatureBlock") or "").strip()
        auth_name = (getattr(request, "firebase_user", None) or {}).get("name", "").strip().lower()
        sig_block_lines = [line.strip().lower() for line in sig_block.split("\n") if line.strip()] if sig_block else []
        if user_profile or phrase_lower or sig_block:
            user_name = (user_profile or {}).get('name', '').lower()
            user_email = (user_profile or {}).get('email', '').lower()
            user_university = (user_profile or {}).get('university', '').lower()
            signature_indicators = [
                'best,', 'best regards', 'thank you', 'thanks,', 'sincerely', 'warm regards', 'cheers,',
                phrase_lower if phrase_lower else None,
                user_name if user_name else None,
                auth_name if auth_name else None,
                user_email if user_email else None,
                user_university if user_university else None,
            ]
            signature_indicators.extend(sig_block_lines)
            signature_indicators = [s for s in signature_indicators if s]
            body_end = body_lower[-200:] if len(body_lower) > 200 else body_lower
            has_signature = any(indicator in body_end for indicator in signature_indicators)
        
        # Build signature from signoff_config or user_profile (only if not already present)
        signature_html = ""
        signature_text = ""
        if not has_signature:
            phrase = (signoff_config.get("signoffPhrase") or "").strip() or "Best,"
            if signoff_config.get("signatureBlock") and sig_block:
                # Custom signature block (plain text and HTML)
                signature_text = "\n\n" + phrase + "\n" + sig_block
                signature_html = f"<br><p>{phrase.replace(chr(10), '<br>')}<br>{sig_block.replace(chr(10), '<br>')}</p>"
            elif user_profile:
                user_name = user_profile.get('name', '')
                user_email = user_profile.get('email', '')
                user_university = user_profile.get('university', '')
                user_year = user_profile.get('year', '') or user_profile.get('graduationYear', '')
                signature_parts_html = []
                if user_name:
                    signature_parts_html.append(f"<b>{user_name}</b>")
                if user_university:
                    if user_year:
                        signature_parts_html.append(f"{user_university} | Class of {user_year}")
                    else:
                        signature_parts_html.append(user_university)
                if user_email:
                    signature_parts_html.append(f'<a href="mailto:{user_email}">{user_email}</a>')
                if signature_parts_html:
                    signature_html = f"<br><p>{phrase}<br>{'<br>'.join(signature_parts_html)}</p>"
                else:
                    signature_html = f"<br><p>{phrase}</p>"
                signature_lines = [phrase]
                if user_name:
                    signature_lines.append(user_name)
                if user_university:
                    signature_lines.append(f"{user_university} | Class of {user_year}" if user_year else user_university)
                if user_email:
                    signature_lines.append(user_email)
                signature_text = "\n\n" + "\n".join(signature_lines)
            else:
                signature_html = f"<br><p>{phrase}</p>"
                signature_text = "\n\n" + phrase
        
        # Add signature to body before saving to Firestore (only if not already present)
        if signature_text:
            body += signature_text

        # Convert to simple HTML (paragraph spacing)
        html_body = "".join([
            f'<p style="margin:12px 0; line-height:1.6;">{p.strip()}</p>'
            for p in body.split("\n") if p.strip()
        ])
        # Add HTML signature
        html_body += signature_html

        # --- Fallback delivery: no Gmail integration, return content directly ---
        # Handled here, before MIME assembly, since fallback mode doesn't need
        # the multipart message or resume attachment built below.
        if fallback_mode:
            created.append({
                "index": i,
                "to": to_addr,
                "subject": r["subject"],
                "body": body,
                "deliveryMode": "fallback",
                # Perplexity hiring-signal fields — populated when the request
                # sets enrichHiringSignal=True (HM flow). Absent for regular
                # People-flow drafts.
                "activelyHiring": c.get("_actively_hiring"),
                "recentHiringSignal": c.get("_recent_hiring_signal"),
            })

            # Save the contact so My Network / surfaces show the drafted email.
            # No gmailDraftId/gmailDraftUrl and inOutbox stays False: reply
            # tracking requires Gmail.
            try:
                contacts_ref = db.collection("users").document(uid).collection("contacts")
                to_addr_clean = (to_addr or "").strip().lower()
                existing_contacts = list(contacts_ref.where("email", "==", to_addr_clean).limit(1).stream())

                contact_data = {
                    "emailSubject": r["subject"],
                    "emailBody": body,
                    "draftToEmail": to_addr_clean,
                    "emailGeneratedAt": datetime.utcnow().isoformat(),
                    "lastActivityAt": datetime.utcnow().isoformat(),
                    "updatedAt": datetime.utcnow().isoformat(),
                    "pipelineStage": "draft_created",
                    "inOutbox": False,
                }
                contact_data.update(_base_contact_fields(r, c, body))

                if existing_contacts:
                    existing_contacts[0].reference.update(contact_data)
                else:
                    contact_data["email"] = to_addr_clean
                    contact_data["createdAt"] = datetime.utcnow().isoformat()
                    contacts_ref.document().set(contact_data)

                # Persist warmth tier + seniority bucket for Phase 2 aggregation,
                # same as the Gmail draft branch below.
                _w_info = warmth_data.get(i) if warmth_data else None
                _persist_warmth_on_send(
                    db, uid, to_addr_clean, _w_info,
                    c.get("Title") or c.get("jobTitle") or "",
                )
            except Exception as e:
                print(f"[{i}] Failed to save fallback contact: {e}")
            continue

        # --- Build MIME message ---
        msg = MIMEMultipart("mixed")
        msg["to"] = to_addr
        msg["subject"] = r["subject"]

        from app.services import email_tracking
        tracking_token = email_tracking.start(uid)
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(body, "plain", "utf-8"))
        alt.attach(MIMEText(email_tracking.add_pixel(html_body, tracking_token), "html", "utf-8"))
        msg.attach(alt)

        # --- Attach resume if available (uses pre-downloaded bytes) ---
        if _cached_resume_data is not None:
            try:
                data = _cached_resume_data
                filename = resume_filename or "Resume.pdf"
                # If filename missing extension, try to pull one from the cached content-type
                if "." not in filename and _cached_resume_ctype and "/" in _cached_resume_ctype:
                    ext = mimetypes.guess_extension(_cached_resume_ctype.split(";")[0].strip()) or ".pdf"
                    filename += ext

                # Infer MIME type
                ctype_clean = (_cached_resume_ctype or "").split(";", 1)[0].strip()
                if "/" in ctype_clean:
                    main, sub = ctype_clean.split("/", 1)
                else:
                    main, sub = _infer_mime_type(filename)

                part = MIMEBase(main, sub)
                part.set_payload(data)
                encoders.encode_base64(part)
                part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
                msg.attach(part)
            except Exception as e:
                print(f"[{i}] Could not attach resume: {e}")

        # --- Create Gmail draft ---
        try:
            raw = base64.urlsafe_b64encode(msg.as_bytes()).decode("utf-8")
            draft = gmail.users().drafts().create(
                userId="me",
                body={"message": {"raw": raw}}
            ).execute()
            print(f"📤 [{i}] Draft created: {draft}")
            draft_id = draft.get("id")
            draft_ids.append(draft_id)
            email_tracking.register(
                tracking_token, uid, "outreach", recipient_email=to_addr,
                draft_id=draft_id,
                message_id=draft.get("message", {}).get("id"),
                thread_id=draft.get("message", {}).get("threadId"),
            )
            
            # Extract message ID and threadId from draft (Gmail creates a thread when draft is created)
            message_id = draft.get("message", {}).get("id")
            thread_id = draft.get("message", {}).get("threadId")
            
            if not message_id or not thread_id:
                # If message ID or threadId not in draft response, get it from the draft message
                try:
                    draft_message = gmail.users().drafts().get(userId="me", id=draft_id, format="full").execute()
                    if not message_id:
                        message_id = draft_message.get("message", {}).get("id")
                    if not thread_id:
                        thread_id = draft_message.get("message", {}).get("threadId")
                except Exception as e:
                    print(f"⚠️ [{i}] Could not get message/threadId from draft: {e}")

            # Use message ID format for more reliable draft URL (Option A from fix doc)
            # Format: https://mail.google.com/mail/u/0/#drafts?compose=<messageId>
            if message_id:
                gmail_url = (
                    f"https://mail.google.com/mail/?authuser={connected_email}#drafts?compose={message_id}"
                    if connected_email else f"https://mail.google.com/mail/u/0/#drafts?compose={message_id}"
                )
            else:
                # Fallback to draft ID format if message ID not available
                gmail_url = (
                    f"https://mail.google.com/mail/?authuser={connected_email}#draft/{draft_id}"
                    if connected_email else f"https://mail.google.com/mail/u/0/#draft/{draft_id}"
                )

            created.append({
                "index": i,
                "to": to_addr,
                "draftId": draft_id,
                "messageId": message_id,
                "threadId": thread_id,
                "gmailUrl": gmail_url,
                # Perplexity hiring-signal fields — populated when the request
                # sets enrichHiringSignal=True (HM flow). Absent for regular
                # People-flow drafts.
                "activelyHiring": c.get("_actively_hiring"),
                "recentHiringSignal": c.get("_recent_hiring_signal"),
            })
            
            # Save/update contact in Firestore with draft info (even if no threadId yet)
            # Drafts may not have threadId until they're sent or replied to
            try:
                contacts_ref = db.collection("users").document(uid).collection("contacts")
                # Find existing contact by email (normalized to lowercase)
                to_addr_clean = (to_addr or "").strip().lower()
                existing_contacts = list(contacts_ref.where("email", "==", to_addr_clean).limit(1).stream())
                
                contact_data = {
                    "gmailDraftId": draft_id,
                    "gmailMessageId": message_id,  # Save message ID for more reliable URL
                    "gmailDraftUrl": gmail_url,
                    "emailSubject": r["subject"],
                    "emailBody": body,
                    "draftToEmail": to_addr_clean,
                    "draftCreatedAt": datetime.utcnow().isoformat(),
                    "emailGeneratedAt": datetime.utcnow().isoformat(),
                    "lastActivityAt": datetime.utcnow().isoformat(),
                    "hasUnreadReply": False,
                    "draftStillExists": True,
                    "updatedAt": datetime.utcnow().isoformat(),
                    "pipelineStage": "draft_created",
                    "inOutbox": True,
                }

                # Store personalization metadata, word count, lead-hook usage,
                # and contact identity fields (shared with the fallback branch
                # below via _base_contact_fields).
                # New fields (leadType, commonalityTypes, warmthTierFinal, wordCountFinal,
                # leadHookUsedInBody) added 2026-04-28. Old contacts only have
                # personalizationLabel + personalizationType. No backfill — filter
                # analysis by emailGeneratedAt >= 2026-04-28 for clean P0 measurement.
                contact_data.update(_base_contact_fields(r, c, body))

                # Add threadId if we have it
                if thread_id:
                    contact_data["gmailThreadId"] = thread_id

                if existing_contacts:
                    # Update existing contact (same email = one contact doc)
                    contact_doc = existing_contacts[0]
                    contact_doc.reference.update(contact_data)
                    print(f"✅ [{i}] Updated contact {contact_doc.id} with draftId {draft['id']}" + (f" and threadId {thread_id}" if thread_id else ""))
                else:
                    # Create new contact only when no existing contact with this email
                    contact_data["email"] = to_addr_clean
                    contact_data["createdAt"] = datetime.utcnow().isoformat()
                    new_contact_ref = contacts_ref.document()
                    new_contact_ref.set(contact_data)
                    print(f"✅ [{i}] Created new contact {new_contact_ref.id} with draftId {draft['id']}" + (f" and threadId {thread_id}" if thread_id else ""))

                # Persist warmth tier + seniority bucket for Phase 2 aggregation.
                # warmth_data is keyed by index within contacts_needing_emails,
                # but we also need to handle contacts_with_emails (no warmth data).
                _w_info = warmth_data.get(i) if warmth_data else None
                _persist_warmth_on_send(
                    db, uid, to_addr_clean, _w_info,
                    c.get("Title") or c.get("jobTitle") or "",
                )
            except Exception as e:
                print(f"⚠️ [{i}] Failed to save contact to Firestore: {e}")
                import traceback
                traceback.print_exc()
        except Exception as e:
            print(f"❌ [{i}] Draft creation failed for {to_addr}: {e}")

    skipped_count = len(contacts) - len(created)
    return jsonify({
        "success": len(created) > 0 or len(contacts) == 0,
        "deliveryMode": "fallback" if fallback_mode else "gmail",
        "connected_email": connected_email,
        "draft_count": len(draft_ids),
        "draft_ids": draft_ids,
        "drafts": created,
        **({"skipped_count": skipped_count} if skipped_count > 0 else {}),
    }), 200


@emails_bp.route("/eml", methods=["POST"])
@require_firebase_auth
def download_eml():
    """Build a downloadable .eml draft for users without a connected inbox.

    The frontend posts back the subject/body it received from
    generate-and-draft in fallback mode; the user's resume (from their own
    Firestore doc, never from the request) is attached when available.
    """
    uid = request.firebase_user["uid"]
    payload = request.get_json(silent=True) or {}
    to_addr = (payload.get("to") or "").strip()
    subject = (payload.get("subject") or "").strip()
    body = payload.get("body") or ""
    if not to_addr or not subject or not body:
        return jsonify({"error": "to, subject and body are required"}), 400

    db = get_db()
    user_data = (db.collection("users").document(uid).get().to_dict() or {})

    resume_bytes = None
    resume_ctype = None
    resume_filename = user_data.get("resumeFileName") or "Resume.pdf"
    resume_url = user_data.get("resumeUrl")
    if resume_url:
        try:
            resume_url = validate_fetch_url(_normalize_drive_url(resume_url))
            res = requests.get(resume_url, timeout=15, headers={"User-Agent": "Offerloop/1.0"})
            res.raise_for_status()
            content = res.content
            # Mirror the guard in generate_and_draft() above: only reject
            # when the payload is both small AND looks like an HTML sharing
            # page (a real PDF/DOCX can legitimately be small).
            looks_like_html_sharing_page = len(content) < 1024 and b"<html" in content[:2048].lower()
            too_large = len(content) > 8 * 1024 * 1024
            if looks_like_html_sharing_page:
                print("[EML] resume URL returned HTML (likely a sharing page)")
            elif too_large:
                print(f"[EML] resume too large ({len(content)} bytes) — skipping")
            else:
                resume_bytes = content
                resume_ctype = res.headers.get("content-type", "application/pdf")
        except Exception as e:
            print(f"[EML] resume download failed, sending without attachment: {e}")

    html_body = "".join(
        f'<p style="margin:12px 0; line-height:1.6;">{html.escape(p.strip())}</p>'
        for p in body.split("\n") if p.strip()
    )
    raw = build_eml(to_addr, subject, body, body_html=html_body,
                    resume_bytes=resume_bytes, resume_filename=resume_filename,
                    resume_ctype=resume_ctype)
    filename = eml_filename(payload.get("firstName"), payload.get("company"))
    return Response(
        raw,
        mimetype="message/rfc822",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _send_one_draft(gmail_service_or_creds, uid, draft_id):
    """Send a single Gmail draft by id. Returns a result dict, never raises.

    Accepts either a pre-built gmail_service (safe when called from a
    single-threaded context) OR a google.oauth2 Credentials object (safe
    from concurrent threads — a fresh service is built here so each thread
    has its own httplib2.Http socket). Sharing a single service across
    threads corrupts the underlying HTTP transport and produces
    CannotSendHeader / HTTP 400 with garbled body — see send_drafts_batch
    for the load-once-build-per-thread pattern.

    Result shape:
      {"draftId": ..., "success": True,  "messageId": ..., "threadId": ...}
      {"draftId": ..., "success": False, "error": "draft_not_found"}   # 404
      {"draftId": ..., "success": False, "error": "gmail_token_expired"}
      {"draftId": ..., "success": False, "error": "send_failed", "message": ...}

    Email quality is preserved verbatim — Gmail sends the exact bytes of
    the existing draft, no re-composition or re-attachment happens here.
    """
    from googleapiclient.errors import HttpError

    # If we were handed credentials (concurrent path), build a per-thread
    # service. If we were handed a service directly (serial path), use it.
    # Detect by presence of `users` attribute — Resource objects have it,
    # Credentials objects don't.
    if hasattr(gmail_service_or_creds, "users"):
        gmail_service = gmail_service_or_creds
    else:
        from app.services.gmail_client import _gmail_service
        gmail_service = _gmail_service(gmail_service_or_creds)

    # Look up the recipient email for this draft (best-effort — used only
    # for log diagnostics). Firestore lookup adds ~50ms per send but the
    # signal is priceless when a send returns 200 from Gmail but the
    # message never appears in the Sent folder or gets silently dropped.
    _recipient_hint = ""
    try:
        db = get_db()
        _matches = list(
            db.collection("users").document(uid).collection("contacts")
              .where("gmailDraftId", "==", draft_id).limit(1).stream()
        )
        if _matches:
            _data = _matches[0].to_dict() or {}
            _recipient_hint = _data.get("email") or _data.get("draftToEmail") or ""
    except Exception:
        pass

    try:
        sent = gmail_service.users().drafts().send(
            userId="me", body={"id": draft_id}
        ).execute()
    except HttpError as e:
        status = getattr(getattr(e, "resp", None), "status", None)
        # Pull the Gmail error body — usually has {"error": {"message": "..."}}
        # with the real reason (rate limit, invalid recipient, etc.).
        try:
            err_body = e.content.decode("utf-8", errors="ignore") if hasattr(e, "content") else ""
        except Exception:
            err_body = ""
        if status == 404:
            print(f"[SendDraft] draft={draft_id[:16]}... to={_recipient_hint or 'unknown'} status=404 draft_not_found (already sent or deleted)")
            _mark_contact_sent_by_draft(uid, draft_id, message_id=None, thread_id=None)
            return {"draftId": draft_id, "success": False, "error": "draft_not_found"}
        if status in (401, 403):
            print(f"[SendDraft] draft={draft_id[:16]}... to={_recipient_hint or 'unknown'} status={status} gmail_token_expired body={err_body[:300]}")
            return {"draftId": draft_id, "success": False, "error": "gmail_token_expired"}
        print(f"[SendDraft] draft={draft_id[:16]}... to={_recipient_hint or 'unknown'} status={status} send_failed body={err_body[:500]}")
        return {"draftId": draft_id, "success": False, "error": "send_failed", "message": str(e)}
    except Exception as e:
        err_str = str(e).lower()
        if "invalid_grant" in err_str or "token has been expired or revoked" in err_str:
            print(f"[SendDraft] draft={draft_id[:16]}... to={_recipient_hint or 'unknown'} gmail_token_expired (auth exception) err={str(e)[:300]}")
            return {"draftId": draft_id, "success": False, "error": "gmail_token_expired"}
        print(f"[SendDraft] draft={draft_id[:16]}... to={_recipient_hint or 'unknown'} send_failed (non-HttpError) type={type(e).__name__} err={str(e)[:500]}")
        return {"draftId": draft_id, "success": False, "error": "send_failed", "message": str(e)}

    message_id = sent.get("id") or sent.get("message", {}).get("id")
    thread_id = sent.get("threadId") or sent.get("message", {}).get("threadId")
    print(f"[SendDraft] draft={draft_id[:16]}... to={_recipient_hint or 'unknown'} ✅ sent messageId={(message_id or 'none')[:20]}...")
    _mark_contact_sent_by_draft(uid, draft_id, message_id, thread_id)
    return {
        "draftId": draft_id,
        "success": True,
        "messageId": message_id,
        "threadId": thread_id,
    }


@emails_bp.post("/send-draft/<draft_id>")
@require_firebase_auth
def send_draft(draft_id):
    """Send an existing Gmail draft by draftId.

    Powers the Find page's per-row "Send" button.
    Response shape matches the frontend contract in api.ts sendDraft():
      - success:      {"success": True, "messageId": ..., "threadId": ...}
      - already gone: HTTP 200 {"success": False, "error": "draft_not_found"}
                      (frontend treats this as sent — draft was consumed elsewhere)
      - auth broken:  HTTP 401 {"error": "gmail_token_expired", ...}
      - other:        HTTP 502 {"success": False, "error": "send_failed", ...}
    """
    if not draft_id or not draft_id.strip():
        return jsonify({"success": False, "error": "missing_draft_id"}), 400

    uid = request.firebase_user["uid"]
    user_email = request.firebase_user.get("email")
    gmail_service = get_gmail_service_for_user(user_email, user_id=uid)
    if not gmail_service:
        return jsonify({
            "error": "gmail_not_connected",
            "message": "Please connect your Gmail account to send emails.",
        }), 401

    result = _send_one_draft(gmail_service, uid, draft_id)

    if result["success"]:
        return jsonify({
            "success": True,
            "messageId": result.get("messageId"),
            "threadId": result.get("threadId"),
        }), 200
    if result["error"] == "draft_not_found":
        return jsonify({"success": False, "error": "draft_not_found"}), 200
    if result["error"] == "gmail_token_expired":
        return jsonify({
            "error": "gmail_token_expired",
            "message": "Your Gmail connection has expired. Please reconnect Gmail.",
        }), 401
    return jsonify({
        "success": False,
        "error": "send_failed",
        "message": result.get("message", ""),
    }), 502


@emails_bp.post("/send-drafts-batch")
@require_firebase_auth
def send_drafts_batch():
    """Send N existing Gmail drafts in parallel.

    Request:  {"draftIds": ["r-123", "r-456", ...]}
    Response: {
        "success": True,                    # true if ≥1 send succeeded
        "sent_count": 12,                   # includes draft_not_found (already sent)
        "failed_count": 3,
        "results": [                        # aligned with input order
            {"draftId": "r-123", "success": True,  "messageId": "m1", "threadId": "t1"},
            {"draftId": "r-456", "success": False, "error": "draft_not_found"},
            {"draftId": "r-789", "success": False, "error": "send_failed", "message": "..."},
        ],
    }

    Special: if Gmail auth is dead, returns 401 {"error": "gmail_token_expired"}
    without attempting any sends (all N would fail identically). If the entire
    batch is auth-blocked mid-flight, still returns 200 with per-item errors
    so the frontend can render partial progress.

    Parallelism capped at 5 workers to stay within Gmail's per-user quota
    (250 units/sec; messages.send costs 100 units → 2.5 sends/sec sustained).
    Email content is not re-generated; the exact draft bytes are sent as-is.
    """
    uid = request.firebase_user["uid"]
    user_email = request.firebase_user.get("email")

    payload = request.get_json(silent=True) or {}
    draft_ids = payload.get("draftIds") or []
    if not isinstance(draft_ids, list):
        return jsonify({"error": "invalid_payload", "message": "draftIds must be a list"}), 400
    # De-dupe and strip while preserving order
    seen = set()
    cleaned = []
    for did in draft_ids:
        if not isinstance(did, str):
            continue
        d = did.strip()
        if d and d not in seen:
            seen.add(d)
            cleaned.append(d)
    if not cleaned:
        return jsonify({
            "success": False,
            "sent_count": 0,
            "failed_count": 0,
            "results": [],
        }), 200

    # Cap batch size to prevent runaway requests (Elite tier max is 15 contacts)
    MAX_BATCH = 50
    if len(cleaned) > MAX_BATCH:
        return jsonify({
            "error": "batch_too_large",
            "message": f"Maximum {MAX_BATCH} drafts per batch send.",
        }), 400

    # Thread-safety fix: load credentials once, hand them to each worker so
    # each thread builds its own gmail_service (fresh httplib2.Http socket).
    # Sharing a single service across threads corrupted the transport under
    # concurrent .execute() calls — produced CannotSendHeader + HTTP 400 with
    # HTML garbage body → users saw "1 of 3 sent" with no explanation.
    # If per-user OAuth creds aren't available (rare — user never connected
    # Gmail), fall back to the shared token.pickle service under serial send.
    from app.services.gmail_client import _load_user_gmail_creds
    from concurrent.futures import ThreadPoolExecutor

    creds = _load_user_gmail_creds(uid)
    if creds:
        service_or_creds = creds
        max_workers = min(5, len(cleaned))
    else:
        # Shared account fallback — thread-unsafe, so serialize.
        shared = get_gmail_service_for_user(user_email, user_id=uid)
        if not shared:
            return jsonify({
                "error": "gmail_not_connected",
                "message": "Please connect your Gmail account to send emails.",
            }), 401
        service_or_creds = shared
        max_workers = 1

    results_by_id = {}
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {pool.submit(_send_one_draft, service_or_creds, uid, did): did for did in cleaned}
        for fut in futures:
            did = futures[fut]
            try:
                results_by_id[did] = fut.result()
            except Exception as e:
                results_by_id[did] = {
                    "draftId": did,
                    "success": False,
                    "error": "send_failed",
                    "message": str(e),
                }

    # Preserve input order in response
    ordered = [results_by_id[d] for d in cleaned]
    # draft_not_found counts as sent — the draft is gone (either sent elsewhere
    # or user deleted it in Gmail). Frontend flips row to "Sent" in both cases.
    sent_count = sum(1 for r in ordered if r["success"] or r.get("error") == "draft_not_found")
    failed_count = len(ordered) - sent_count

    return jsonify({
        "success": sent_count > 0,
        "sent_count": sent_count,
        "failed_count": failed_count,
        "results": ordered,
    }), 200


def _mark_contact_sent_by_draft(uid, draft_id, message_id, thread_id):
    """Stamp the contact doc owning this draft as sent.

    Idempotent: safe to call on the 404 "already gone" path (message_id may
    be None; we only refresh timestamps and pipeline stage in that case).
    Never raises — send success must not be lost to a Firestore hiccup.
    """
    try:
        db = get_db()
        contacts_ref = db.collection("users").document(uid).collection("contacts")
        matches = list(contacts_ref.where("gmailDraftId", "==", draft_id).limit(1).stream())
        if not matches:
            return
        now_iso = datetime.utcnow().isoformat()
        update = {
            "pipelineStage": "email_sent",
            "emailSentAt": now_iso,
            "lastActivityAt": now_iso,
            "updatedAt": now_iso,
            "draftStillExists": False,
            "inOutbox": True,
        }
        if message_id:
            update["gmailMessageId"] = message_id
        if thread_id:
            update["gmailThreadId"] = thread_id
        matches[0].reference.update(update)
    except Exception as exc:
        import logging
        logging.getLogger("emails").warning(
            "send_draft contact update failed draft=%s: %s", draft_id, exc
        )