"""
Resume parsing routes
"""
from flask import Blueprint, request, jsonify
from firebase_admin import auth as fb_auth
from firebase_admin import firestore
import urllib.parse

from app.services.resume_parser import extract_text_from_file
from app.services.resume_capabilities import (
    is_valid_resume_file,
    get_file_extension,
    build_resume_metadata
)
from app.services.resume_scoring import MIN_JOB_DESCRIPTION_CHARS, score_resume_structured
from ..extensions import require_firebase_auth
from app.utils.users import parse_resume_info, validate_parsed_resume
from ..extensions import get_db

resume_bp = Blueprint('resume', __name__, url_prefix='/api')


def upload_resume_to_firebase_storage(user_id, file):
    """Upload the resume PRIVATELY and return (url, thumb_url).

    2026-09-08: this used to call blob.make_public(), which left every resume
    readable by anyone with the path while the app promised "only you can
    see it". The token-secured Firebase URL keeps every existing reader
    (Gmail attachments, auto-apply, the website) working without a public
    ACL, and a page-one thumbnail is rendered once so previews never load
    the whole PDF. See app/services/resume_storage.py.
    """
    try:
        from app.services.resume_storage import store_resume
        data = file.read()
        out = store_resume(user_id, file.filename, data, getattr(file, 'mimetype', '') or '')
        return out.get('url'), out.get('thumb_url')
    except Exception as e:
        print(f"Storage upload failed: {e}")
        return None, None


def extract_storage_path_from_url(storage_url):
    """
    Extract the blob path from a Firebase Storage URL.
    
    Firebase Storage URLs have format:
    https://firebasestorage.googleapis.com/v0/b/{bucket}/o/{encoded_path}?alt=media&token=...
    
    Returns the decoded blob path, or None if URL format is invalid.
    """
    if not storage_url:
        return None
    
    try:
        # Parse the URL
        parsed = urllib.parse.urlparse(storage_url)
        
        # Check if it's a Firebase Storage URL
        if 'firebasestorage.googleapis.com' not in parsed.netloc:
            return None
        
        # Extract path from /v0/b/{bucket}/o/{encoded_path}
        path_parts = parsed.path.split('/o/')
        if len(path_parts) != 2:
            return None
        
        # Decode the path (URL-encoded)
        encoded_path = path_parts[1]
        decoded_path = urllib.parse.unquote(encoded_path)
        
        return decoded_path
    except Exception as e:
        print(f"[Resume] Error extracting storage path from URL: {e}")
        return None


def delete_file_from_storage(storage_url):
    """
    Delete a file from Firebase Storage using its public URL.
    
    Returns True if deletion succeeded, False otherwise.
    """
    if not storage_url:
        return False
    
    try:
        from firebase_admin import storage
        bucket = storage.bucket()
        
        # Extract blob path from URL
        blob_path = extract_storage_path_from_url(storage_url)
        if not blob_path:
            print(f"[Resume] Could not extract path from URL: {storage_url[:50]}...")
            return False
        
        # Get blob and delete
        blob = bucket.blob(blob_path)
        if blob.exists():
            blob.delete()
            print(f"[Resume] Deleted storage file: {blob_path}")
            return True
        else:
            print(f"[Resume] Storage file does not exist: {blob_path}")
            return False
    except Exception as e:
        print(f"[Resume] Error deleting file from storage: {e}")
        import traceback
        traceback.print_exc()
        return False


def save_resume_to_firebase(user_id, resume_text, resume_url, parsed_info=None, resume_metadata=None):
    """Save resume text, URL, parsed info, and capabilities to Firestore with full structure"""
    try:
        from datetime import datetime
        import json
        
        db = get_db()
        if not db:
            print("[Resume] ERROR: No database connection")
            return False
        
        # DEBUG: Log the full parsed_info structure
        print(f"[Resume DEBUG] parsed_info type: {type(parsed_info)}")
        print(f"[Resume DEBUG] parsed_info keys: {list(parsed_info.keys()) if parsed_info else 'None'}")
        
        if parsed_info:
            # Log each major section
            print(f"[Resume DEBUG] experience: {len(parsed_info.get('experience', []))} entries")
            print(f"[Resume DEBUG] projects: {len(parsed_info.get('projects', []))} entries")
            print(f"[Resume DEBUG] education: {parsed_info.get('education', {})}")
            
            # Log first experience entry for verification
            if parsed_info.get('experience'):
                print(f"[Resume DEBUG] First experience: {json.dumps(parsed_info['experience'][0], indent=2)}")
            
            # Log first project entry for verification
            if parsed_info.get('projects'):
                print(f"[Resume DEBUG] First project: {json.dumps(parsed_info['projects'][0], indent=2)}")
        
        # Start with base update data
        update_data = {
            'resumeText': resume_text,
            'originalResumeText': resume_text,  # Backup of original text
            'resumeUrl': resume_url,
            'resumeUpdatedAt': datetime.now().isoformat()
        }
        
        # Add resume metadata with capabilities if provided
        if resume_metadata:
            update_data.update({
                'resumeFileName': resume_metadata.get('resumeFileName'),
                'resumeFileType': resume_metadata.get('resumeFileType'),
                'resumeUploadedAt': resume_metadata.get('resumeUploadedAt'),
                'resumeCapabilities': resume_metadata.get('resumeCapabilities', {})
            })
            # Page-one preview image (token URL), absent for DOCX uploads.
            if resume_metadata.get('resumeThumbUrl'):
                update_data['resumeThumbUrl'] = resume_metadata['resumeThumbUrl']
            print(f"[Resume] Saving capabilities: {resume_metadata.get('resumeCapabilities', {}).get('recommendedMode', 'unknown')}")
        
        # Save parsed resume data with full structure (v2 format)
        if parsed_info:
            # Store the complete parsed structure
            update_data['resumeParsed'] = parsed_info
            update_data['resumeParseVersion'] = 2  # Track schema version
            
            # DEBUG: Log what we're about to save
            print(f"[Resume DEBUG] update_data['resumeParsed'] keys: {list(update_data['resumeParsed'].keys())}")
            print(f"[Resume DEBUG] About to save to Firestore...")
            
            print(f"[Resume] Saved resume with version 2 structure")
            print(f"[Resume] Experience entries: {len(parsed_info.get('experience', []))}")
            print(f"[Resume] Project entries: {len(parsed_info.get('projects', []))}")
            if 'skills' in parsed_info:
                if isinstance(parsed_info['skills'], dict):
                    total_skills = sum(len(v) if isinstance(v, list) else 0 for v in parsed_info['skills'].values())
                    print(f"[Resume] Total skills: {total_skills}")
                else:
                    print(f"[Resume] Skills (legacy format): {len(parsed_info['skills']) if isinstance(parsed_info['skills'], list) else 'N/A'}")
        
        # Save to Firestore
        db.collection('users').document(user_id).update(update_data)
        
        # DEBUG: Read back and verify
        print(f"[Resume DEBUG] Verifying save...")
        saved_doc = db.collection('users').document(user_id).get()
        saved_data = saved_doc.to_dict()
        saved_parsed = saved_data.get('resumeParsed', {})
        print(f"[Resume DEBUG] Saved resumeParsed keys: {list(saved_parsed.keys())}")
        print(f"[Resume DEBUG] Saved experience count: {len(saved_parsed.get('experience', []))}")
        print(f"[Resume DEBUG] Saved projects count: {len(saved_parsed.get('projects', []))}")
        
        return True
    except Exception as e:
        print(f"Firestore save failed: {e}")
        import traceback
        traceback.print_exc()
        return False


@resume_bp.route('/parse-resume', methods=['POST'])
def parse_resume():
    """Parse uploaded resume (PDF, DOCX, or DOC), upload to storage, and extract user information"""
    try:
        from datetime import datetime
        
        print("=" * 60)
        print("📋 RESUME UPLOAD & PARSING")
        print("=" * 60)
        
        # Validate file exists
        if 'resume' not in request.files:
            print("❌ No resume file in request")
            return jsonify({'error': 'No resume file provided'}), 400
        
        file = request.files['resume']
        print(f"📄 File: {file.filename}")
        
        if not file.filename:
            print("❌ Empty filename")
            return jsonify({'error': 'No file selected'}), 400
        
        # Validate file type using capabilities service
        if not is_valid_resume_file(file.filename, file.mimetype):
            print(f"❌ Invalid file type: {file.filename}")
            return jsonify({
                'error': 'Invalid file type. Please upload a PDF, DOCX, or DOC file.'
            }), 400
        
        # Get file extension
        extension = get_file_extension(file.filename, file.mimetype)
        print(f"📄 File type: {extension.upper()}")
        
        # Extract text from file
        print(f"📖 Extracting text from {extension.upper()}...")
        resume_text = extract_text_from_file(file, extension)
        
        if not resume_text:
            print(f"❌ Could not extract text from {extension.upper()}")
            return jsonify({
                'error': f'Could not extract text from {extension.upper()} file'
            }), 400
        
        print(f"✅ Extracted {len(resume_text)} characters")
        
        # Parse user info
        print("🔍 Parsing resume info...")
        parsed_info = parse_resume_info(resume_text)
        
        # Validate parsed resume
        if parsed_info:
            is_valid, errors = validate_parsed_resume(parsed_info)
            if errors:
                print(f"⚠️  Validation {'warnings' if is_valid else 'errors'}: {', '.join(errors)}")
            if not is_valid:
                print(f"❌ Resume parsing validation failed, but continuing with partial data")
        
        name = parsed_info.get('name') if parsed_info else 'Unknown'
        if not name and parsed_info and 'education' in parsed_info:
            # Try to get name from education if available
            name = 'Unknown'
        print(f"✅ Parsed: {name}")
        
        # Get user ID from auth token
        user_id = None
        resume_url = None
        resume_metadata = None
        
        try:
            db = get_db()
            auth_header = request.headers.get('Authorization', '')
            
            if auth_header.startswith('Bearer '):
                id_token = auth_header.split(' ', 1)[1].strip()
                
                try:
                    decoded = fb_auth.verify_id_token(id_token, clock_skew_seconds=5)
                    user_id = decoded.get('uid')
                    print(f"[Resume] Processing authenticated upload")
                    
                    if user_id and db:
                        # STEP 4A: Upload file to Firebase Storage
                        print("\n📤 Uploading to Firebase Storage...")
                        file.seek(0)  # Reset file pointer for re-reading
                        resume_url, resume_thumb_url = upload_resume_to_firebase_storage(user_id, file)
                        
                        if not resume_url:
                            print("⚠️  File upload failed, continuing without URL")
                        
                        # STEP 4B: Build resume metadata with capabilities
                        print("\n💾 Building resume metadata...")
                        resume_metadata = build_resume_metadata(
                            url=resume_url or '',
                            filename=file.filename,
                            extension=extension
                        )
                        if resume_thumb_url:
                            resume_metadata['resumeThumbUrl'] = resume_thumb_url
                        
                        # STEP 4C: Save both text, URL, parsed data, and capabilities to Firebase
                        print("\n💾 Saving to Firestore...")
                        file.seek(0)  # Reset again for text extraction
                        save_result = save_resume_to_firebase(
                            user_id, 
                            resume_text,
                            resume_url,
                            parsed_info,  # Include parsed data
                            resume_metadata  # Include capabilities metadata
                        )
                        
                        if save_result:
                            print("✅ All data saved successfully")
                            print(f"✅ Capabilities: {resume_metadata['resumeCapabilities']['recommendedMode']}")
                        else:
                            print("⚠️  Save returned False")
                    
                except Exception as e:
                    print(f"❌ Token verification failed: {e}")
                    import traceback
                    traceback.print_exc()
                    
        except Exception as e:
            print(f"❌ Auth check failed: {e}")
            import traceback
            traceback.print_exc()
        
        print("=" * 60)
        print("✅ RESUME PROCESSING COMPLETE")
        print("=" * 60)
        
        # Build response with capabilities
        response_data = {
            'success': True,
            'data': parsed_info,
            'savedToFirebase': bool(user_id),
            'resumeUrl': resume_url,
            'resumeFileName': file.filename,
            'resumeFileType': extension,
            'message': _get_upload_message(extension)
        }
        
        if resume_metadata:
            response_data['resumeCapabilities'] = resume_metadata['resumeCapabilities']
        
        return jsonify(response_data)
        
    except Exception as e:
        print(f"💥 FATAL ERROR: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': 'Failed to parse resume'}), 500


def _get_upload_message(extension: str) -> str:
    """Get user-friendly message based on file type."""
    if extension == 'docx':
        return "Resume uploaded successfully! Your formatting will be perfectly preserved during optimization."
    elif extension == 'doc':
        return "Resume uploaded successfully! For best results, consider saving as DOCX format."
    else:  # pdf
        return "Resume uploaded successfully! For best formatting preservation during optimization, consider uploading a DOCX file."


@resume_bp.route('/resume', methods=['DELETE'])
@require_firebase_auth
def delete_resume():
    """Completely delete all resume data for a user."""
    try:
        user_id = request.firebase_user.get('uid')
        if not user_id:
            return jsonify({'error': 'User ID not found'}), 401
        
        db = get_db()
        if not db:
            return jsonify({'error': 'Database not available'}), 500
        
        user_ref = db.collection('users').document(user_id)
        user_doc = user_ref.get()
        
        if not user_doc.exists:
            return jsonify({'error': 'User not found'}), 404
        
        user_data = user_doc.to_dict()
        
        print(f"[Resume] Starting deletion")
        
        # 1. Delete files from Firebase Storage
        storage_urls = [
            user_data.get('resumeUrl'),
            user_data.get('originalResumeUrl'),
        ]
        
        for url in storage_urls:
            if url:
                try:
                    delete_file_from_storage(url)
                except Exception as e:
                    print(f"[Resume] Warning: Failed to delete storage file {url[:50] if url else 'None'}...: {e}")
                    # Continue even if storage deletion fails
        try:
            from app.services.resume_storage import delete_thumbnail
            delete_thumbnail(user_id)
        except Exception as e:
            print(f"[Resume] Warning: thumbnail delete skipped: {e}")
        
        # 2. Delete ALL resume fields from Firestore using DELETE_FIELD
        # This completely removes the fields instead of setting them to null
        update_data = {
            # Current resume data
            'resumeParsed': firestore.DELETE_FIELD,
            'resumeText': firestore.DELETE_FIELD,
            'resumeUrl': firestore.DELETE_FIELD,
            'resumeThumbUrl': firestore.DELETE_FIELD,
            'resumeFileName': firestore.DELETE_FIELD,
            'resumeUpdatedAt': firestore.DELETE_FIELD,
            'resumeParseVersion': firestore.DELETE_FIELD,
            'resumeSource': firestore.DELETE_FIELD,
            'resumeNeedsOCR': firestore.DELETE_FIELD,
            
            # Original resume data (backup copies)
            'originalResumeParsed': firestore.DELETE_FIELD,
            'originalResumeText': firestore.DELETE_FIELD,
            'originalResumeUrl': firestore.DELETE_FIELD,
            'originalResumeFileName': firestore.DELETE_FIELD,
            
            # Metadata
            'resumeBackfilledAt': firestore.DELETE_FIELD,
        }
        
        user_ref.update(update_data)
        
        print(f"[Resume] Successfully deleted all resume data")
        
        return jsonify({
            'success': True,
            'message': 'Resume deleted successfully'
        }), 200
        
    except Exception as e:
        print(f"[Resume] Error deleting resume: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': 'Failed to delete resume'}), 500


def _upload_tailored_pdf(user_id: str, job_hash: str, pdf_bytes: bytes) -> str | None:
    """Upload a tailored resume PDF and return the public URL."""
    try:
        from firebase_admin import storage
        bucket = storage.bucket()
        blob = bucket.blob(f'resumes/{user_id}/tailored/{job_hash}.pdf')
        blob.upload_from_string(pdf_bytes, content_type='application/pdf')
        blob.make_public()
        return blob.public_url
    except Exception as exc:
        print(f"[Resume] Tailored PDF upload failed: {exc}")
        import traceback
        traceback.print_exc()
        return None


def _job_hash(job_title: str, company: str, description: str) -> str:
    """Deterministic short hash so re-tailoring the same job overwrites."""
    import hashlib
    key = f"{(job_title or '').strip().lower()}|{(company or '').strip().lower()}|{(description or '').strip()[:500]}"
    return hashlib.sha1(key.encode('utf-8')).hexdigest()[:12]


@resume_bp.route('/resume/tailor', methods=['POST'])
@require_firebase_auth
def tailor_resume_route():
    """Tailor the user's resume to a specific job description via Claude.

    Body: { jobUrl?, jobDescription?, jobTitle?, company? }

    Flow: read users/{uid}.resumeParsed → normalize to CanonicalResume →
    resolve JD (URL via Firecrawl, or pasted text) → Claude Opus 4.7 tailors
    bullets/skills/projects → render canonical PDF → upload to
    resumes/{uid}/tailored/{jobHash}.pdf → persist to
    users/{uid}/tailoredResumes/{jobHash} → return { pdfUrl, ... }.
    """
    from datetime import datetime

    from app.services.resume_jd_resolver import JDResolutionError, resolve_jd
    from app.services.resume_renderer import (
        ResumeRenderError,
        from_resume_parsed,
        render_one_page,
    )
    from app.services.resume_tailor import ResumeTailorError, tailor_resume

    try:
        user_id = request.firebase_user.get('uid')
        if not user_id:
            return jsonify({'error': 'User ID not found'}), 401

        db = get_db()
        if not db:
            return jsonify({'error': 'Database not available'}), 500

        data = request.get_json(force=True, silent=True) or {}
        job_url = (data.get('jobUrl') or '').strip()
        job_description = (data.get('jobDescription') or '').strip()
        job_title = (data.get('jobTitle') or '').strip()
        company = (data.get('company') or '').strip()

        if not job_url and not job_description:
            return jsonify({'error': 'Provide a jobUrl or jobDescription.'}), 400

        user_ref = db.collection('users').document(user_id)
        user_doc = user_ref.get()
        if not user_doc.exists:
            return jsonify({'error': 'User not found'}), 404

        user_data = user_doc.to_dict() or {}
        resume_parsed = user_data.get('resumeParsed')
        if not resume_parsed or not isinstance(resume_parsed, dict):
            return jsonify({
                'error': 'No parsed resume found. Upload your resume in Account Settings first.'
            }), 400

        try:
            jd = resolve_jd(
                job_url=job_url or None,
                job_description=job_description or None,
                job_title=job_title or None,
                company=company or None,
            )
        except JDResolutionError as exc:
            return jsonify({'error': str(exc)}), 400

        canonical = from_resume_parsed(resume_parsed)

        try:
            tailored = tailor_resume(canonical, jd)
        except ResumeTailorError as exc:
            print(f"[Resume/tailor] Claude call failed: {exc}")
            return jsonify({'error': f'Tailor failed: {exc}'}), 500

        try:
            result = render_one_page(tailored)
        except ResumeRenderError as exc:
            print(f"[Resume/tailor] Render failed: {exc}")
            return jsonify({'error': f'Render failed: {exc}'}), 500

        job_hash = _job_hash(jd.title, jd.company, jd.description)
        pdf_url = _upload_tailored_pdf(user_id, job_hash, result.pdf_bytes)
        if not pdf_url:
            return jsonify({'error': 'Failed to upload tailored PDF to storage'}), 500

        updated_at = datetime.now().isoformat()
        tailored_ref = user_ref.collection('tailoredResumes').document(job_hash)
        tailored_ref.set({
            'jobTitle': jd.title or None,
            'company': jd.company or None,
            'jobUrl': job_url or None,
            'pdfUrl': pdf_url,
            'pageCount': result.page_count,
            'reductionsApplied': result.reductions_applied,
            'updatedAt': updated_at,
            'model': 'claude-opus-4-7',
        }, merge=True)

        return jsonify({
            'pdfUrl': pdf_url,
            'jobTitle': jd.title or None,
            'company': jd.company or None,
            'tailoredResumeId': job_hash,
            'pageCount': result.page_count,
            'reductionsApplied': result.reductions_applied,
            'updatedAt': updated_at,
        }), 200

    except Exception as exc:
        print(f"[Resume/tailor] error: {exc}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': 'Failed to tailor resume'}), 500


@resume_bp.route('/resume/tailored', methods=['GET'])
@require_firebase_auth
def list_tailored_resumes():
    """List the caller's past tailored resumes, newest first."""
    try:
        uid = request.firebase_user['uid']
        db = get_db()
        if not db:
            return jsonify({'error': 'Database not available'}), 500

        col = db.collection('users').document(uid).collection('tailoredResumes')
        items = []
        for snap in col.stream():
            doc = snap.to_dict() or {}
            items.append({
                'id': snap.id,
                'jobTitle': doc.get('jobTitle'),
                'company': doc.get('company'),
                'jobUrl': doc.get('jobUrl'),
                'pdfUrl': doc.get('pdfUrl'),
                'pageCount': doc.get('pageCount'),
                'updatedAt': doc.get('updatedAt'),
                'model': doc.get('model'),
            })
        items.sort(key=lambda x: x.get('updatedAt') or '', reverse=True)
        return jsonify({'items': items}), 200
    except Exception as e:
        print(f"[Resume/tailored list] error: {e}")
        return jsonify({'error': 'Failed to list tailored resumes'}), 500


@resume_bp.route('/resume/score', methods=['POST'])
@require_firebase_auth
def score_resume():
    """
    Score the caller's resume against the Harvard rubric and return
    path-targeted, mechanically-applicable recommendations.

    Body: {"resumeParsed": {...}} (optional — falls back to the user doc's
    stored resumeParsed if omitted or empty). Optional job-fit mode: pass
    "jobDescription" (plus optional "jobTitle", "company") to score fit
    against that specific posting instead of general quality. Costs no
    credits.
    """
    try:
        uid = request.firebase_user['uid']
        db = get_db()

        payload = request.get_json(silent=True) or {}
        parsed = payload.get('resumeParsed')

        # Optional job-fit mode: a meaningful jobDescription switches the
        # scorer to grade fit against that posting. Empty/whitespace-only is
        # treated as absent (general mode) so clients that always send the
        # field don't break; present-but-too-short is a hard 400.
        job_context = None
        job_description = payload.get('jobDescription')
        job_description = job_description.strip() if isinstance(job_description, str) else ''
        if job_description:
            if len(job_description) < MIN_JOB_DESCRIPTION_CHARS:
                return jsonify({
                    'error': 'Job description is too short to score against '
                             f'(minimum {MIN_JOB_DESCRIPTION_CHARS} characters). '
                             'Paste the full posting.'
                }), 400
            job_title = payload.get('jobTitle')
            company = payload.get('company')
            job_context = {
                'job_description': job_description,
                'job_title': job_title.strip() if isinstance(job_title, str) else '',
                'company': company.strip() if isinstance(company, str) else '',
            }

        if not parsed:
            user_data = {}
            if db:
                user_doc = db.collection('users').document(uid).get()
                user_data = user_doc.to_dict() or {}
            parsed = user_data.get('resumeParsed')

        if not parsed:
            return jsonify({'error': 'No resume data to score. Upload a resume first.'}), 400

        try:
            result = score_resume_structured(parsed, job_context=job_context)
        except ValueError as e:
            return jsonify({'error': str(e)}), 400

        return jsonify(result), 200

    except Exception as e:
        print(f"[Resume] Scoring failed: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': 'Failed to score resume'}), 502

