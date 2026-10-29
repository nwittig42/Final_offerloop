"""
LinkedIn import routes - Import contact from LinkedIn URL
"""
from flask import Blueprint, request, jsonify
import requests
import re
from datetime import datetime

from ..extensions import require_firebase_auth, get_db
from ..config import PDL_BASE_URL, PEOPLE_DATA_LABS_API_KEY
from ..services.reply_generation import batch_generate_emails, PURPOSES_INCLUDE_RESUME, email_body_mentions_resume, regenerate_with_feedback
from ..utils.warmth_scoring import score_contacts_for_email
from ..utils.users import get_outreach_email
from ..services.gmail_client import create_gmail_draft_for_user, download_resume_from_url
from ..services.hunter import get_verified_email, get_smart_company_domain
from ..services.pdl_client import _choose_best_email, _pdl_email_is_fresh
from ..services.resume_parser import extract_text_from_pdf_bytes
from ..utils.email_quality import check_email_quality
from ..services.email_request_builder import (
    resolve_email_template as _resolve_email_template,
    build_email_gen_request,
)

linkedin_import_bp = Blueprint('linkedin_import', __name__, url_prefix='/api/contacts')


def resolve_email_for_linkedin_import(pdl_contact: dict, person_data: dict) -> dict:
    """
    Resolve email using Hunter.io fallback pipeline (same as Coffee Chat Prep).
    
    Args:
        pdl_contact: Extracted contact dict with FirstName, LastName, Company fields
        person_data: Raw PDL person data dict for email extraction and context
    
    Returns:
        {
            "email": str | None,
            "email_source": "pdl" | "hunter.io" | None
        }
    """
    print(f"\n[LinkedInImport] ========== Email Resolution Started ==========")

    first_name = pdl_contact.get('FirstName', '')
    last_name = pdl_contact.get('LastName', '')
    company = pdl_contact.get('Company', '')

    print(f"[LinkedInImport] Contact Info: {first_name} {last_name} at {company}")

    # Use canonical PDL email picker + freshness gate from pdl_client.
    # Stale PDL emails (>90 days since job_last_updated) are dropped so Hunter
    # gets a chance to re-verify, matching the website's prompt-search behavior.
    emails = person_data.get('emails') if isinstance(person_data.get('emails'), list) else []
    recommended = person_data.get('recommended_personal_email')
    pdl_email = _choose_best_email(emails, recommended)
    if pdl_email and not _pdl_email_is_fresh(person_data, pdl_email):
        print(f"[LinkedInImport] PDL email failed freshness gate (>90d), preferring Hunter")
        pdl_email = None
    elif pdl_email:
        print(f"[LinkedInImport] PDL email passed freshness gate")
    else:
        print(f"[LinkedInImport] No PDL email — will rely on Hunter.io")
    
    # Resolve target company domain using existing domain helpers
    target_domain = None
    if company:
        print(f"[LinkedInImport] Resolving domain for company: {company}")
        target_domain = get_smart_company_domain(company)
        print(f"[LinkedInImport] Resolved domain: {target_domain or 'None'}")
    else:
        print(f"[LinkedInImport] No company provided - cannot resolve domain")
    
    # Call get_verified_email with proper parameters
    print(f"[LinkedInImport] Calling get_verified_email (domain: {target_domain or 'None'})")
    
    verified_result = get_verified_email(
        pdl_email=pdl_email,
        first_name=first_name,
        last_name=last_name,
        company=company,
        person_data=person_data,
        target_domain=target_domain
    )
    
    # Extract email and source
    email = verified_result.get('email')
    email_source = verified_result.get('email_source')
    email_verified = verified_result.get('email_verified', False)
    
    print(f"[LinkedInImport] Email resolution complete: source={email_source or 'None'}, verified={email_verified}")
    
    # Return structured output only (never "Not available", use None)
    return {
        "email": email if email and email != "Not available" else None,
        "email_source": email_source
    }


def extract_contact_from_pdl_person_enhanced(person_data):
    """
    Extract contact information from PDL person enrichment response.
    
    Args:
        person_data: Dictionary from PDL person/enrich API response
        
    Returns:
        Dictionary with standardized contact fields
    """
    contact = {}
    
    # Extract name
    first_name = person_data.get('first_name', '')
    last_name = person_data.get('last_name', '')
    contact['FirstName'] = first_name
    contact['LastName'] = last_name
    
    # Extract emails (prioritize work email)
    # Handle various formats: list, dict, boolean, string, or None
    emails_data = person_data.get('emails', [])
    work_email = None
    personal_email = None
    
    # Check if emails is actually a list/iterable
    if isinstance(emails_data, list) and len(emails_data) > 0:
        for email_obj in emails_data:
            email_addr = None
            email_type = None
            
            if isinstance(email_obj, dict):
                email_addr = email_obj.get('address', '') or email_obj.get('email', '')
                email_type = email_obj.get('type', '').lower()
            elif isinstance(email_obj, str):
                # Handle case where emails is a list of strings
                email_addr = email_obj
                email_type = 'work'  # Default to work
            
            if email_addr and '@' in email_addr:
                if email_type == 'work' or (email_type is None and not personal_email):
                    if not work_email:
                        work_email = email_addr
                    contact['Email'] = email_addr
                    contact['WorkEmail'] = email_addr
                elif email_type == 'personal':
                    if not personal_email:
                        personal_email = email_addr
                    contact['PersonalEmail'] = email_addr
    elif isinstance(emails_data, dict):
        # Handle case where emails is a single dict
        email_addr = emails_data.get('address', '') or emails_data.get('email', '')
        if email_addr and '@' in email_addr:
            contact['Email'] = email_addr
            contact['WorkEmail'] = email_addr
    elif isinstance(emails_data, str) and '@' in emails_data:
        # Handle case where emails is a single string
        contact['Email'] = emails_data
        contact['WorkEmail'] = emails_data
    
    # If no work email but we have personal email, use that
    if not contact.get('Email') and personal_email:
        contact['Email'] = personal_email
    
    # Extract job information
    job_titles = person_data.get('job_titles', [])
    if isinstance(job_titles, list) and len(job_titles) > 0:
        # Get the most recent job title
        latest_job = job_titles[0]
        if isinstance(latest_job, dict):
            contact['Title'] = latest_job.get('title', '') or latest_job.get('name', '')
        elif isinstance(latest_job, str):
            contact['Title'] = latest_job
    elif isinstance(job_titles, str):
        contact['Title'] = job_titles
    elif isinstance(job_titles, dict):
        contact['Title'] = job_titles.get('title', '') or job_titles.get('name', '')
    
    # Extract company information
    # PDL API can return either 'experience' (singular) or 'experiences' (plural)
    experiences = person_data.get('experiences') or person_data.get('experience') or []
    
    print(f"[LinkedInImport] [ExtractContact] Checking experiences: type={type(experiences)}, length={len(experiences) if isinstance(experiences, list) else 'N/A'}")
    
    if isinstance(experiences, list) and len(experiences) > 0:
        # Get the most recent experience
        latest_exp = experiences[0]
        print(f"[LinkedInImport] [ExtractContact] Latest experience: {latest_exp}")
        if isinstance(latest_exp, dict):
            company_obj = latest_exp.get('company', {})
            print(f"[LinkedInImport] [ExtractContact] Company object from experience: {company_obj} (type: {type(company_obj)})")
            if isinstance(company_obj, dict):
                contact['Company'] = company_obj.get('name', '')
                print(f"[LinkedInImport] [ExtractContact] Extracted company name from dict: {contact['Company']}")
            elif isinstance(company_obj, str):
                contact['Company'] = company_obj
                print(f"[LinkedInImport] [ExtractContact] Extracted company name from string: {contact['Company']}")
    elif isinstance(experiences, dict):
        # Handle case where experiences is a single dict
        company_obj = experiences.get('company', {})
        if isinstance(company_obj, dict):
            contact['Company'] = company_obj.get('name', '')
        elif isinstance(company_obj, str):
            contact['Company'] = company_obj
    
    # Also check top-level company field
    if not contact.get('Company'):
        company = person_data.get('company', '')
        print(f"[LinkedInImport] [ExtractContact] Checking top-level company field: {company} (type: {type(company)})")
        if isinstance(company, str):
            contact['Company'] = company
        elif isinstance(company, dict):
            contact['Company'] = company.get('name', '')
    
    print(f"[LinkedInImport] [ExtractContact] Final extracted company: '{contact.get('Company', '')}'")
    
    # Extract location
    location = person_data.get('location', {})
    if isinstance(location, dict):
        contact['City'] = location.get('city', '')
        contact['State'] = location.get('region', '')
        contact['Country'] = location.get('country', '')
    
    # Extract LinkedIn
    profiles = person_data.get('profiles', [])
    linkedin_url = None
    if isinstance(profiles, list):
        for profile in profiles:
            if isinstance(profile, dict):
                if profile.get('network', '').lower() == 'linkedin':
                    linkedin_url = profile.get('url', '')
                    break
    elif isinstance(profiles, dict):
        if profiles.get('network', '').lower() == 'linkedin':
            linkedin_url = profiles.get('url', '')
    
    # Also check top-level linkedin_url field
    if not linkedin_url:
        linkedin_url = person_data.get('linkedin_url') or person_data.get('linkedin')
    
    if linkedin_url:
        contact['LinkedIn'] = linkedin_url
    
    # Extract phone
    phone_numbers = person_data.get('phone_numbers', [])
    if isinstance(phone_numbers, list) and len(phone_numbers) > 0:
        phone_obj = phone_numbers[0]
        if isinstance(phone_obj, dict):
            contact['Phone'] = phone_obj.get('number', '') or phone_obj.get('phone', '')
        else:
            contact['Phone'] = str(phone_obj)
    elif isinstance(phone_numbers, str):
        contact['Phone'] = phone_numbers
    elif isinstance(phone_numbers, dict):
        contact['Phone'] = phone_numbers.get('number', '') or phone_numbers.get('phone', '')
    
    # Also check top-level phone field
    if not contact.get('Phone'):
        phone = person_data.get('phone', '') or person_data.get('phone_number', '')
        if phone:
            contact['Phone'] = str(phone)
    
    # Extract education
    educations = person_data.get('education', [])
    if isinstance(educations, list) and len(educations) > 0:
        latest_edu = educations[0]
        if isinstance(latest_edu, dict):
            school_obj = latest_edu.get('school', {})
            if isinstance(school_obj, dict):
                contact['College'] = school_obj.get('name', '')
            elif isinstance(school_obj, str):
                contact['College'] = school_obj
            contact['EducationTop'] = contact.get('College', '')
    elif isinstance(educations, dict):
        # Handle case where education is a single dict
        school_obj = educations.get('school', {})
        if isinstance(school_obj, dict):
            contact['College'] = school_obj.get('name', '')
        elif isinstance(school_obj, str):
            contact['College'] = school_obj
        contact['EducationTop'] = contact.get('College', '')
    
    # Also check top-level school/college fields
    if not contact.get('College'):
        school = person_data.get('school', '') or person_data.get('college', '')
        if isinstance(school, str):
            contact['College'] = school
        elif isinstance(school, dict):
            contact['College'] = school.get('name', '')
        contact['EducationTop'] = contact.get('College', '')
    
    # Don't set "Not available" - email will be resolved via Hunter
    # Leave Email field empty if no PDL email found
    
    return contact


def normalize_linkedin_url(url):
    """Normalize LinkedIn URL to standard format"""
    url = url.strip().rstrip('/')
    url = url.split('?')[0]
    
    pattern = r'^https?://(www\.)?linkedin\.com/in/([\w-]+)$'
    match = re.match(pattern, url)
    
    if match:
        username = match.group(2)
        return f'https://www.linkedin.com/in/{username}'
    return None


@linkedin_import_bp.route('/import-linkedin', methods=['POST', 'OPTIONS'])
@require_firebase_auth
def import_from_linkedin():
    """
    Import a contact from LinkedIn URL:
    1. Parse LinkedIn URL to extract username
    2. Query PDL to find contact by LinkedIn URL
    3. Generate personalized email using OpenAI
    4. Create Gmail draft
    5. Save contact to Firestore
    """
    # Handle OPTIONS preflight requests
    if request.method == 'OPTIONS':
        # CORS middleware will handle headers, just return empty response
        return '', 200
    
    try:
        print(f"\n{'='*80}")
        print(f"[LinkedInImport] ========== NEW IMPORT REQUEST ==========")
        print(f"{'='*80}")
        
        data = request.get_json() or {}
        linkedin_url = data.get('linkedin_url', '').strip()
        user_id = request.firebase_user['uid']
        user_resume = data.get('user_resume', '')
        user_email = request.firebase_user.get('email', 'Unknown')
        # Preview vs draft. The website search-box paste sends create_draft=False so
        # this behaves like a normal Find People search: find the contact + email,
        # save it, and let the user choose to draft/send afterward. The chrome
        # extension (and any caller that omits the flag) keeps the legacy
        # auto-draft behavior. Skipping the draft also skips the slow enrichment +
        # email generation, so preview returns fast.
        create_draft = bool(data.get('create_draft', True))
        # email_mode=fast (the extension, 2026-09-07): answer in seconds from
        # the shelf or Hunter; if only FullEnrich's waterfall is left, start it
        # in the background and let /contacts/<id>/email-status finish it.
        email_mode = (str(data.get('email_mode') or '')).strip().lower()
        
        print(f"[LinkedInImport] Request received: resume_provided={bool(user_resume)}")
        
        if not linkedin_url:
            print(f"[LinkedInImport] ❌ ERROR: Missing LinkedIn URL")
            return jsonify({'status': 'error', 'message': 'Missing LinkedIn URL'}), 400
        
        # Step 1: Validate and normalize LinkedIn URL
        print(f"[LinkedInImport] Step 1: Validating and normalizing LinkedIn URL...")
        linkedin_url = normalize_linkedin_url(linkedin_url)
        if not linkedin_url:
            print(f"[LinkedInImport] ❌ ERROR: Invalid LinkedIn URL format")
            return jsonify({'status': 'error', 'message': 'Invalid LinkedIn URL format'}), 400
        print(f"[LinkedInImport] ✅ Normalized URL: {linkedin_url}")
        
        # Step 2: Enrich via Apify (2026-10-28). Warehouse for people a
        # previous student met; Apify (HarvestAPI's linkedin-profile-scraper)
        # for the rest. Apify is the project's designated single LinkedIn
        # vendor (CLAUDE.md). This path previously used Coresignal collect
        # by LinkedIn shorthand; that worked but added a vendor dependency
        # for data this route only needs the thin slice of (name/title/
        # company/city/state/one school/photo — see pdl_contact below).
        # Meeting prep still uses the richer Coresignal collect for its
        # own needs (enrich_linkedin_profile_engine), unaffected by this.
        print(f"[LinkedInImport] Step 2: Enriching via Apify (warehouse-first)...")
        from app.services.linkedin_enrich import enrich_linkedin_profile_via_apify, warehouse_only_profile
        engine_data = warehouse_only_profile(linkedin_url, db=get_db())
        if engine_data:
            print(f"[LinkedInImport] ✅ Served from warehouse, no vendor call")
        else:
            engine_data = enrich_linkedin_profile_via_apify(linkedin_url, db=get_db())
        if not engine_data or not (engine_data.get('firstName') or engine_data.get('lastName')):
            print(f"[LinkedInImport] ❌ ERROR: engine returned no data")
            return jsonify({
                'status': 'error',
                'message': 'Could not find contact information for this LinkedIn profile',
                'error_code': 'CONTACT_NOT_FOUND'
            }), 404

        # Shape into the App-contact dict the rest of this route consumes.
        edu0 = (engine_data.get('educationArray') or [{}])[0] if engine_data.get('educationArray') else {}
        pdl_contact = {
            'FirstName': engine_data.get('firstName', ''),
            'LastName': engine_data.get('lastName', ''),
            'Title': engine_data.get('jobTitle', ''),
            'Company': engine_data.get('company', ''),
            'City': engine_data.get('city', ''),
            'State': engine_data.get('state', ''),
            'College': (edu0.get('school') or '') if isinstance(edu0, dict) else '',
            'LinkedIn': linkedin_url,
            'PhotoUrl': engine_data.get('photoUrl', ''),
            'Email': '',
        }

        print(f"[LinkedInImport] ✅ Contact Extracted:")
        print(f"[LinkedInImport]   - Name: {pdl_contact.get('FirstName', '')} {pdl_contact.get('LastName', '')}")
        print(f"[LinkedInImport]   - Company: {pdl_contact.get('Company', 'None')}")
        print(f"[LinkedInImport]   - Title: {pdl_contact.get('Title', 'None')}")
        print(f"[LinkedInImport]   - Location: {pdl_contact.get('City', '')}, {pdl_contact.get('State', '')}")

        # Step 4 (2026-09-07): fastest source first. The shelf (instant: any
        # student found them in the last 90 days), then Hunter's single
        # database (2 to 3 seconds, accepted at confidence 90+), then
        # FullEnrich's waterfall. The waterfall is 15 to 90 seconds and
        # slowest on a miss, so with email_mode=fast it runs in the
        # background: this response returns now, the address lands on the
        # contact and the shelf when the job finishes, and email-status hands
        # it to the panel. Other callers keep the synchronous waterfall.
        # See app.services.email_fast, the same ladder the deck reveal and
        # the website Find run.
        print(f"[LinkedInImport] Step 4: Resolving email (shelf, Hunter, FullEnrich)...")
        contact_email = None
        email_source = None
        email_pending = None
        try:
            from app.services import email_fast
            _fast = email_fast.fast_email(
                first_name=pdl_contact.get('FirstName', ''),
                last_name=pdl_contact.get('LastName', ''),
                company=pdl_contact.get('Company', ''),
                linkedin_url=linkedin_url,
                db=get_db(),
            )
            if _fast:
                contact_email = _fast['email']
                email_source = _fast['source']
                print(f"[LinkedInImport] Fast rung hit: source={email_source}")
            elif email_mode == 'fast':
                email_pending = email_fast.start_pending(
                    pdl_contact.get('FirstName', ''), pdl_contact.get('LastName', ''),
                    pdl_contact.get('Company', ''), linkedin_url, get_db(),
                )
                print(f"[LinkedInImport] FullEnrich started in the background: {bool(email_pending)}")
            if not contact_email and email_pending is None:
                from app.services import fullenrich_client
                _fe_hit = fullenrich_client.find_work_email(
                    first_name=pdl_contact.get('FirstName', ''),
                    last_name=pdl_contact.get('LastName', ''),
                    company=pdl_contact.get('Company', ''),
                    linkedin_url=linkedin_url,
                    db=get_db(),
                )
                if _fe_hit and _fe_hit.get('email'):
                    contact_email = _fe_hit['email']
                    email_source = f"fullenrich_{(_fe_hit.get('status') or 'found').lower()}"
        except Exception as fe_err:
            print(f"[LinkedInImport] email lookup failed: {fe_err}")
        if contact_email and email_source and email_source != 'warehouse':
            try:
                from app.services import email_fast as _ef
                _ef.write_email_to_shelf(linkedin_url, contact_email, email_source, get_db())
            except Exception:
                pass
        has_email = contact_email is not None

        print(f"[LinkedInImport] Email resolution: found={has_email}, source={email_source or 'None'}")

        contact_for_email = pdl_contact.copy()
        contact_for_email['LinkedIn'] = linkedin_url
        if contact_email:
            contact_for_email['Email'] = contact_email

        # Step 4: Load user data (template, resume, tier all need it)
        print(f"[LinkedInImport] Step 5: Loading user data...")
        db = get_db()
        user_ref = db.collection('users').document(user_id)
        user_doc = user_ref.get()
        user_data = user_doc.to_dict() if user_doc.exists else {}
        user_profile = {
            'name': user_data.get('name', ''),
            'university': user_data.get('university', ''),
            'major': user_data.get('major', ''),
            'year': user_data.get('year', ''),
            # Prefer the user's .edu as the outreach identity (body signature).
            'email': get_outreach_email(user_data) or request.firebase_user.get('email', ''),
        }
        for key in ("resumeParsed", "academics", "goals", "careerTrack",
                    "dreamCompanies", "hometown", "location", "pastCompanies"):
            if key in user_data and key not in user_profile:
                user_profile[key] = user_data[key]

        user_tier = (user_data.get('subscriptionTier') or user_data.get('tier') or 'free').lower()
        print(f"[LinkedInImport] User tier: {user_tier}")

        # Step 4c: Resume text + PDF for attachment.
        # Prefer the request-body resume (website's ContactSearchPage sends it pre-extracted).
        # Fall back to Firestore resumeText, and finally to downloading the PDF and extracting.
        resume_text_for_email = (user_resume or '').strip() or (user_data.get('resumeText') or '')
        resume_content = None
        resume_filename = user_data.get('resumeFileName') if user_data else None
        resume_url = (user_data.get('resumeUrl') or user_data.get('resumeURL')) if user_data else None
        if resume_url:
            try:
                print(f"[LinkedInImport] Downloading resume from {resume_url[:80]}...")
                _content, _fname = download_resume_from_url(resume_url)
                if _content:
                    resume_content = _content
                    if not resume_filename:
                        resume_filename = _fname or 'resume.pdf'
                    if not resume_text_for_email or len(resume_text_for_email) < 50:
                        try:
                            extracted = extract_text_from_pdf_bytes(_content)
                            if extracted and len(extracted.strip()) > 50:
                                resume_text_for_email = extracted
                                print(f"[LinkedInImport] Extracted {len(extracted)} chars from resume PDF")
                        except Exception as _ee:
                            print(f"[LinkedInImport] PDF text extraction failed: {_ee}")
            except Exception as e:
                print(f"[LinkedInImport] Resume download failed: {e}")
        if resume_text_for_email:
            print(f"[LinkedInImport] Resume text ready ({len(resume_text_for_email)} chars)")
        else:
            print(f"[LinkedInImport] No resume text available — email will skip resume context")

        email_subject = None
        email_body = None
        email_personalization = None
        quality_regenerated = False
        draft_result = None
        enrichment_data = {}
        warmth_data = {}  # populated whenever we have an email (cheap), so the
                          # result card shows the warmth/fit pill in both modes.

        # Warmth scoring is cheap and drives the result-card fit pill — run it
        # whenever an email exists, in preview and draft modes alike.
        if has_email:
            warmth_data = score_contacts_for_email(user_data or {}, [contact_for_email])

        # Preview mode (create_draft=False) stops after finding the contact + email
        # and scoring warmth. The slow enrichment + email generation + Gmail draft
        # only run when create_draft is true (legacy/extension path), so the
        # website paste returns fast and the user drafts/sends on demand.
        if has_email and create_draft:
            # Step 4d: Perplexity + Apify enrichment (same pipeline as website prompt-search)
            contacts_for_enrich = [contact_for_email]
            try:
                from app.services.perplexity_client import batch_enrich_contacts
                enrichment_data = batch_enrich_contacts(contacts_for_enrich) or {}
                enrich = enrichment_data.get(0, {})
                if enrich.get("talking_points"):
                    contact_for_email["enrichment_talking_points"] = enrich["talking_points"]
                if enrich.get("recent_activity"):
                    contact_for_email["enrichment_recent_activity"] = enrich["recent_activity"]
                if enrich.get("media_appearances"):
                    contact_for_email["perplexity_media_appearances"] = enrich["media_appearances"]
                if enrich.get("published_writing"):
                    contact_for_email["perplexity_published_writing"] = enrich["published_writing"]
                if enrich.get("news_mentions"):
                    contact_for_email["perplexity_news_mentions"] = enrich["news_mentions"]
            except Exception as _pe:
                print(f"[LinkedInImport] Perplexity contact enrichment failed (non-blocking): {_pe}")

            try:
                from app.services.apify_client import batch_enrich_linkedin_posts_via_apify
                apify_results = batch_enrich_linkedin_posts_via_apify(contacts_for_enrich) or {}
                payload = apify_results.get(0, {})
                if payload.get("linkedin_recent_posts"):
                    contact_for_email["linkedin_recent_posts"] = payload["linkedin_recent_posts"]
            except Exception as _ae:
                print(f"[LinkedInImport] Apify enrichment failed (non-blocking): {_ae}")

            try:
                from app.services.perplexity_client import batch_enrich_company_news
                company_enrichment = batch_enrich_company_news(contacts_for_enrich) or {}
                co = company_enrichment.get(0, {})
                if co.get("company_recent_news"):
                    contact_for_email["company_recent_news"] = co["company_recent_news"]
                if co.get("company_description"):
                    contact_for_email["company_description"] = co["company_description"]
            except Exception as _ce:
                print(f"[LinkedInImport] Perplexity company enrichment failed (non-blocking): {_ce}")

            print(f"[LinkedInImport] Step 6: Generating personalized email...")
            auth_display_name = (getattr(request, "firebase_user", None) or {}).get("name") or ""

            email_request = build_email_gen_request(
                contacts=[contact_for_email],
                user_id=user_id,
                user_profile=user_profile,
                user_data=user_data,
                auth_display_name=auth_display_name,
                fit_context=None,
                template_override=None,
                resume_text=resume_text_for_email or None,
                resume_filename=resume_filename,
                warmth_data=warmth_data,
                enrichment_data=enrichment_data,
                db=db,
            )
            email_results = batch_generate_emails(**email_request)

            print(f"[LinkedInImport] Email generation result: {bool(email_results)}")

            email_data = None
            if email_results:
                email_data = email_results.get(0) or email_results.get('0')
                if not email_data and isinstance(email_results, dict) and email_results:
                    email_data = next(iter(email_results.values()))
                elif not email_data and isinstance(email_results, list) and email_results:
                    email_data = email_results[0]

            if email_data:
                email_subject = email_data.get('subject', f"Connecting with you - {contact_for_email['FirstName']}")
                email_body = email_data.get('plain_body') or email_data.get('body', '')
                email_personalization = email_data.get('personalization')
                print(f"[LinkedInImport]   - Email Subject: {email_subject}")
                print(f"[LinkedInImport]   - Email Body Length: {len(email_body) if email_body else 0} chars")

                # Step 6b: Email Quality Gate — one regen pass on failure
                if email_subject and email_body:
                    try:
                        qg_university = user_profile.get('university', '')
                        qr = check_email_quality(email_subject, email_body, contact_for_email, qg_university)
                        if not qr['passed']:
                            print(f"[LinkedInImport] Quality gate failed ({qr['failures']}), regenerating...")
                            _regen_sender = (auth_display_name or (user_profile or {}).get("name") or "").strip()
                            improved = regenerate_with_feedback(
                                contact_for_email,
                                user_profile,
                                {"subject": email_subject, "body": email_body},
                                qr['failures'],
                                sender_name=_regen_sender or None,
                                signoff_config=email_request.get("signoff_config") if isinstance(email_request, dict) else None,
                            )
                            improved_qr = check_email_quality(improved['subject'], improved['body'], contact_for_email, qg_university)
                            if improved_qr['passed'] or len(improved_qr['failures']) < len(qr['failures']):
                                email_subject = improved['subject']
                                email_body = improved['body']
                                quality_regenerated = True
                                print(f"[LinkedInImport] Quality gate: regenerated email accepted")
                            else:
                                print(f"[LinkedInImport] Quality gate: regen did not improve, keeping original")

                        # Log to email_quality_logs
                        try:
                            final_qr = check_email_quality(email_subject, email_body, contact_for_email, qg_university)
                            db.collection("email_quality_logs").add({
                                "userId": user_id,
                                "contactId": contact_for_email.get("pdlId", ""),
                                "passed": final_qr["passed"],
                                "failures": final_qr["failures"],
                                "regenerated": quality_regenerated,
                                "source": "linkedin_import",
                                "timestamp": datetime.utcnow().isoformat() + "Z",
                            })
                        except Exception as _ql:
                            print(f"[LinkedInImport] Quality log write failed: {_ql}")
                    except Exception as qg_err:
                        print(f"[LinkedInImport] Quality gate error (non-blocking): {qg_err}")

                if email_body:
                    print(f"[LinkedInImport] Step 7: Creating Gmail draft...")
                    user_email = request.firebase_user.get('email')
                    user_info = {
                        'name': user_profile.get('name', '') or user_data.get('name', ''),
                        # Prefer .edu for the draft signature identity.
                        'email': get_outreach_email(user_data) or user_email or '',
                        'phone': user_data.get('phone', '') if user_data else '',
                        'linkedin': user_data.get('linkedin', '') if user_data else '',
                    }
                    try:
                        draft_result = create_gmail_draft_for_user(
                            contact=contact_for_email,
                            email_subject=email_subject,
                            email_body=email_body,
                            tier=user_tier,
                            user_email=user_email,
                            user_id=user_id,
                            user_info=user_info,
                            resume_content=resume_content,
                            resume_filename=resume_filename,
                        )
                        if draft_result:
                            print(f"[LinkedInImport]   - ✅ Gmail draft created")
                            if isinstance(draft_result, dict):
                                print(f"[LinkedInImport]   - Draft ID: {draft_result.get('draft_id', 'Unknown')}")
                                print(f"[LinkedInImport]   - Draft URL: {draft_result.get('draft_url', 'Unknown')}")
                        else:
                            print(f"[LinkedInImport]   - ⚠️ Gmail draft returned None")
                    except Exception as draft_error:
                        print(f"[LinkedInImport]   - ❌ Gmail draft error: {draft_error}")
                        import traceback
                        traceback.print_exc()
            else:
                print(f"[LinkedInImport]   - ⚠️ No email generated")
        else:
            print(f"[LinkedInImport]   - ⚠️ Email generation returned no results")

        # Step 6: Save to Firestore
        print(f"[LinkedInImport] Step 8: Saving contact to Firestore...")
        now_iso = datetime.utcnow().isoformat() + "Z"
        contact_data = {
            'firstName': pdl_contact.get('FirstName', ''),
            'lastName': pdl_contact.get('LastName', ''),
            'email': contact_email,
            'emailSource': email_source,
            'emailVerified': bool(contact_for_email.get('EmailVerified')),
            'emailConfidenceScore': int(contact_for_email.get('EmailConfidenceScore') or 0),
            'linkedinUrl': linkedin_url,
            'jobTitle': pdl_contact.get('Title', ''),
            'company': pdl_contact.get('Company', ''),
            'college': pdl_contact.get('College', '') or pdl_contact.get('college', '') or '',
            'location': f"{pdl_contact.get('City', '')}, {pdl_contact.get('State', '')}".strip(', '),
            'city': pdl_contact.get('City', ''),
            'state': pdl_contact.get('State', ''),
            'source': 'linkedin_import',
            'status': 'drafted' if has_email and draft_result else 'not_contacted',
            'createdAt': datetime.utcnow(),
            'updatedAt': datetime.utcnow(),
            'pdlId': pdl_contact.get('pdlId', '') or '',
        }

        if email_subject:
            contact_data['emailSubject'] = email_subject
        if email_body:
            contact_data['emailBody'] = email_body

        if draft_result:
            if isinstance(draft_result, dict):
                contact_data['gmailDraftId'] = draft_result.get('draft_id')
                message_id = draft_result.get('message_id')
                if message_id:
                    contact_data['gmailMessageId'] = message_id
                contact_data['gmailDraftUrl'] = draft_result.get('draft_url')
                contact_data['gmailThreadId'] = draft_result.get('thread_id')
                draft_recipient = draft_result.get('recipient_email')
            else:
                contact_data['gmailDraftId'] = str(draft_result)
                draft_recipient = None
            contact_data['pipelineStage'] = 'draft_created'
            contact_data['emailGeneratedAt'] = now_iso
            contact_data['draftCreatedAt'] = now_iso
            contact_data['draftStillExists'] = True
            contact_data['draftToEmail'] = draft_recipient or contact_email
            contact_data['inOutbox'] = True
            contact_data['lastActivityAt'] = now_iso
            contact_data['hasUnreadReply'] = False

        # Personalization metadata (matches runs.py:818 contract for analytics)
        if email_personalization:
            contact_data['personalizationLabel'] = email_personalization.get('label', '')
            contact_data['personalizationType'] = email_personalization.get('commonality_type', '')
            if email_personalization.get('lead_type'):
                contact_data['leadType'] = email_personalization['lead_type']
            if email_personalization.get('commonality_types'):
                contact_data['commonalityTypes'] = email_personalization['commonality_types']
            if email_personalization.get('warmth_tier_final'):
                contact_data['warmthTierFinal'] = email_personalization['warmth_tier_final']
            if email_personalization.get('word_count_final') is not None:
                contact_data['wordCountFinal'] = email_personalization['word_count_final']
            if email_personalization.get('lead_hook_used_in_body') is not None:
                contact_data['leadHookUsedInBody'] = email_personalization['lead_hook_used_in_body']
        if quality_regenerated:
            contact_data['qualityRegenerated'] = True

        # Warmth metadata
        if contact_for_email.get('warmth_score') is not None:
            contact_data['warmthScore'] = contact_for_email['warmth_score']
            contact_data['warmthTier'] = contact_for_email.get('warmth_tier', '')
            contact_data['warmthLabel'] = contact_for_email.get('warmth_label', '')
            contact_data['warmthSignals'] = contact_for_email.get('warmth_signals', [])

        # Enrichment metadata (Perplexity + Apify) — bounded to mirror runs.py
        if contact_for_email.get('enrichment_talking_points'):
            contact_data['enrichmentTalkingPoints'] = contact_for_email['enrichment_talking_points'][:5]
        if contact_for_email.get('enrichment_recent_activity'):
            contact_data['enrichmentRecentActivity'] = contact_for_email['enrichment_recent_activity']
        if contact_for_email.get('perplexity_media_appearances'):
            contact_data['perplexityMediaAppearances'] = contact_for_email['perplexity_media_appearances'][:5]
        if contact_for_email.get('perplexity_published_writing'):
            contact_data['perplexityPublishedWriting'] = contact_for_email['perplexity_published_writing'][:5]
        if contact_for_email.get('perplexity_news_mentions'):
            contact_data['perplexityNewsMentions'] = contact_for_email['perplexity_news_mentions'][:5]
        if contact_for_email.get('linkedin_recent_posts'):
            contact_data['linkedinRecentPosts'] = contact_for_email['linkedin_recent_posts'][:5]
        if contact_for_email.get('company_recent_news'):
            contact_data['companyRecentNews'] = contact_for_email['company_recent_news'][:5]
        if contact_for_email.get('company_description'):
            contact_data['companyDescription'] = contact_for_email['company_description'][:1000]

        if email_pending:
            # The ticket email-status polls. The charge is recorded so a miss
            # can give it back.
            from ..config import CREDIT_COSTS as _CCp
            contact_data['emailPending'] = {**email_pending, 'charged': int(_CCp.get('reveal_email', 8))}
        print(f"[LinkedInImport]   - Contact Data Prepared:")
        print(f"[LinkedInImport]     - Name: {contact_data['firstName']} {contact_data['lastName']}")
        print(f"[LinkedInImport]     - Email: {contact_data['email'] or 'None'}")
        print(f"[LinkedInImport]     - Email Source: {contact_data['emailSource'] or 'None'}")
        print(f"[LinkedInImport]     - Status: {contact_data['status']}")
        
        contacts_ref = db.collection('users').document(user_id).collection('contacts')
        doc_ref = contacts_ref.add(contact_data)
        contact_id = doc_ref[1].id
        print(f"[LinkedInImport]   - ✅ Contact saved with ID: {contact_id}")
        
        # Step 7: Deduct credit
        print(f"[LinkedInImport] Step 9: Deducting credit...")
        # 8 since 2026-09-07 (was 5): one hand-picked person costs the same
        # on every surface, matching the app deck's reveal (CREDIT_COSTS
        # reveal_email).
        from ..config import CREDIT_COSTS as _CC
        credits_to_deduct = int(_CC.get('reveal_email', 8))
        user_doc = user_ref.get()
        current_credits = 0
        if user_doc.exists:
            current_credits = user_doc.to_dict().get('credits', 0)
        
        print(f"[LinkedInImport]   - Current Credits: {current_credits}")
        print(f"[LinkedInImport]   - Credits to Deduct: {credits_to_deduct}")
        
        new_credits = max(0, current_credits - credits_to_deduct)
        user_ref.update({
            'credits': new_credits,
            'lastCreditUsage': datetime.utcnow().isoformat()
        })
        print(f"[LinkedInImport]   - ✅ New Credits: {new_credits}")
        
        # Build response message
        print(f"[LinkedInImport] Step 10: Preparing response...")
        full_name = f"{pdl_contact.get('FirstName', '')} {pdl_contact.get('LastName', '')}".strip()
        
        # HTTP Semantics: 200 for success (email optional), 404 only for contact not found, 500 for errors
        # Always return 200 if contact was successfully imported, even without email
        if has_email and draft_result:
            message = f'Successfully imported {full_name}! Gmail draft created.'
        elif has_email and not create_draft:
            # Preview mode: contact + email found, draft is the user's next step.
            message = f'Found {full_name}. Draft or send when you’re ready.'
        elif has_email:
            message = f'Successfully imported {full_name}, but email draft could not be created.'
        else:
            # No email found after Hunter fallback, but contact was imported successfully
            message = f'Successfully imported {full_name}, but no email address was found. You can add an email manually later.'
        
        status_code = 200  # Always 200 for successful contact import
        
        print(f"[LinkedInImport] Final Summary:")
        print(f"[LinkedInImport]   - Contact: {full_name}")
        print(f"[LinkedInImport]   - Email Found: {has_email}")
        print(f"[LinkedInImport]   - Email Source: {email_source or 'None'}")
        print(f"[LinkedInImport]   - Draft Created: {draft_result is not None}")
        print(f"[LinkedInImport]   - Credits Remaining: {new_credits}")
        print(f"[LinkedInImport]   - Status Code: {status_code}")
        print(f"{'='*80}")
        print(f"[LinkedInImport] ========== IMPORT COMPLETE ==========")
        print(f"{'='*80}\n")
        
        warmth_entry = warmth_data.get(0) if isinstance(warmth_data, dict) else None
        response_payload = {
            'status': 'ok',
            # Card-ready shape so the frontend can render this through the same
            # result-card + action-button path as the Find People search.
            'contact': {
                'full_name': full_name,
                'firstName': pdl_contact.get('FirstName', ''),
                'lastName': pdl_contact.get('LastName', ''),
                'email': contact_email,  # None if not found
                'email_source': email_source,  # "pdl" or "hunter.io" or None
                'linkedin_url': linkedin_url,
                'linkedinUrl': linkedin_url,
                'jobTitle': pdl_contact.get('Title', ''),
                'company': pdl_contact.get('Company', ''),
                'location': f"{pdl_contact.get('City', '')}, {pdl_contact.get('State', '')}".strip(', '),
                'emailSubject': email_subject,
                'emailBody': email_body,
                'gmailDraftUrl': draft_result.get('draft_url') if isinstance(draft_result, dict) else None,
                'warmth_label': (warmth_entry or {}).get('label'),
                'warmth_tier': (warmth_entry or {}).get('tier'),
                'warmth_signals': (warmth_entry or {}).get('signals'),
            },
            'contact_id': contact_id,
            'draft_created': draft_result is not None,
            'email_found': has_email,
            'email_pending': email_pending is not None,
            'message': message,
            'credits_remaining': new_credits
        }
        if draft_result and isinstance(draft_result, dict) and draft_result.get('draft_url'):
            response_payload['gmail_draft_url'] = draft_result.get('draft_url')
        return jsonify(response_payload), status_code
        
    except Exception as e:
        print(f"\n{'='*80}")
        print(f"[LinkedInImport] ❌❌❌ ERROR OCCURRED ❌❌❌")
        print(f"{'='*80}")
        print(f"[LinkedInImport] Error: {str(e)}")
        print(f"[LinkedInImport] Error Type: {type(e).__name__}")
        import traceback
        print(f"[LinkedInImport] Traceback:")
        traceback.print_exc()
        print(f"{'='*80}\n")
        return jsonify({'status': 'error', 'message': str(e)}), 500




# ---------------------------------------------------------------------------
# Email status (2026-09-07): the second half of email_mode=fast.
#
# import-linkedin returns at once when only FullEnrich's waterfall is left,
# with the job ticket stored on the contact as emailPending. The panel polls
# this route; when the job lands, the address is written to the contact and
# (verified tiers only) to the shared shelf, and a miss refunds the charge.
# ---------------------------------------------------------------------------


@linkedin_import_bp.route('/<contact_id>/email-status', methods=['GET'])
@require_firebase_auth
def linkedin_import_email_status(contact_id):
    """GET /api/contacts/<contact_id>/email-status

    Response 200: {"status": "found"|"miss"|"pending", "email": str|null,
                   "source": str|null, "refunded": bool}
    Response 404: {"error": "contact_not_found"}
    """
    uid = request.firebase_user['uid']
    contact_id = (contact_id or '').strip()
    if not contact_id:
        return jsonify({'error': 'contact_id required'}), 400
    db = get_db()
    if db is None:
        return jsonify({'error': 'Database not initialized'}), 500

    ref = db.collection('users').document(uid).collection('contacts').document(contact_id)
    try:
        snap = ref.get()
    except Exception:
        snap = None
    if snap is None or not getattr(snap, 'exists', False):
        return jsonify({'error': 'contact_not_found'}), 404
    doc = snap.to_dict() or {}

    email = (doc.get('email') or '').strip()
    if email and email.lower() != 'not available':
        return jsonify({'status': 'found', 'email': email,
                        'source': doc.get('emailSource'), 'refunded': False}), 200

    pending = doc.get('emailPending') or {}
    if not pending or pending.get('resolved'):
        return jsonify({'status': 'miss', 'email': None, 'source': None,
                        'refunded': bool(pending.get('refunded'))}), 200

    from app.services import email_fast
    state, hit = email_fast.poll_pending(pending, db)
    if state == 'pending':
        return jsonify({'status': 'pending', 'email': None, 'source': None, 'refunded': False}), 200

    from google.cloud import firestore as _fs
    if state == 'found' and hit:
        try:
            ref.update({
                'email': hit['email'],
                'emailSource': hit['source'],
                'emailVerified': True,
                'emailConfidenceScore': 95,
                'emailPending': {**pending, 'resolved': 'found', 'resolvedAt': email_fast._now_iso()},
                'updatedAt': _fs.SERVER_TIMESTAMP,
            })
        except Exception as e:
            print(f"[EmailStatus] contact update failed for {contact_id}: {e}")
        try:
            email_fast.write_email_to_shelf(doc.get('linkedinUrl') or '', hit['email'], hit['source'], db)
        except Exception:
            pass
        return jsonify({'status': 'found', 'email': hit['email'],
                        'source': hit['source'], 'refunded': False}), 200

    # A miss: the charge was for the address, so it goes back.
    refunded = False
    charged = int(pending.get('charged') or 0)
    if charged > 0 and not pending.get('refunded'):
        try:
            from app.services.auth import refund_credits_atomic
            ok, _remaining = refund_credits_atomic(uid, charged, 'email_pending_refund')
            refunded = bool(ok)
        except Exception as e:
            print(f"[EmailStatus] refund failed for {contact_id}: {e}")
    try:
        ref.update({
            'emailPending': {**pending, 'resolved': 'miss', 'refunded': refunded,
                             'resolvedAt': email_fast._now_iso()},
            'updatedAt': _fs.SERVER_TIMESTAMP,
        })
    except Exception as e:
        print(f"[EmailStatus] contact update failed for {contact_id}: {e}")
    return jsonify({'status': 'miss', 'email': None, 'source': None, 'refunded': refunded}), 200


# ── Profile glance (2026-09-08) ────────────────────────────────────────
# The extension's profile page asks the page for the person's company to
# list their colleagues. LinkedIn's 2026 layouts hide that behind scrambled
# class names, so the panel also asks the shelf: anyone a student found
# before carries company, title and photo. Free, no vendor call, no charge.
@linkedin_import_bp.route('/profile-glance', methods=['GET'])
@require_firebase_auth
def profile_glance():
    try:
        from ..services.email_fast import linkedin_slug
        from ..services.firm_cache.schema import FIRM_EMPLOYEES_COLLECTION
        slug = linkedin_slug(request.args.get('url') or '')
        db = get_db()
        if not slug or db is None:
            return jsonify({"found": False}), 200
        snap = db.collection(FIRM_EMPLOYEES_COLLECTION).document(slug).get()
        if not getattr(snap, "exists", False):
            return jsonify({"found": False}), 200
        d = snap.to_dict() or {}
        return jsonify({
            "found": True,
            "name": d.get("name") or "",
            "company": d.get("company_display") or "",
            "title": d.get("headline") or "",
            "photo_url": d.get("photo_url") or "",
            "logo_url": "",
        }), 200
    except Exception as e:
        print(f"[LinkedInImport] profile-glance failed: {e}")
        return jsonify({"found": False}), 200
