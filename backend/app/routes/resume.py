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
from ..extensions import require_firebase_auth
from app.utils.users import parse_resume_info, validate_parsed_resume
from ..extensions import get_db

resume_bp = Blueprint('resume', __name__, url_prefix='/api')


# --- Abuse backstop for /parse-resume -----------------------------------------
# /parse-resume is intentionally UNAUTHENTICATED (resumes are parsed during
# signup, before the account exists), and it calls OpenAI on every request. That
# makes it the one place a stranger can drive our OpenAI spend. Two guards:
#
#   1. A hard cap on the text length we ever send to the model, so each call's
#      cost is bounded no matter what file is uploaded.
#   2. A tight per-IP sliding window (a real user parses a resume once or twice
#      during onboarding; these caps only bite scripts). The global limiter
#      (500/hr) is far too loose for an unauthenticated paid endpoint.
#
# The window is in-memory + per-gunicorn-worker, so the effective ceiling is
# roughly (caps × worker count) — fine as a backstop, not exact accounting.
import time as _time
from collections import deque
from flask_limiter.util import get_remote_address

# A resume is a few pages; ~24k chars comfortably covers even a dense CV while
# capping the worst-case token bill of one parse.
MAX_RESUME_CHARS = 24_000

# Generous on purpose. The real cost protection is MAX_RESUME_CHARS (each parse
# is bounded to a fraction of a cent), so these caps only need to stop a script
# doing thousands — NOT a legit signup wave. College students on one campus
# network or at a live demo share a public IP, so we leave plenty of headroom
# (an 80-signup hour on a single IP is a huge real event, and still ~8 cents).
_parse_hits: dict[str, deque] = {}
_PARSE_PER_MIN = 20      # parses / 60s per IP
_PARSE_PER_HOUR = 80     # parses / 3600s per IP


def _parse_rate_limited(ip: str) -> bool:
    """Record a hit for ip and return True if it exceeds the per-minute or
    per-hour cap. Sliding window over request timestamps; prunes as it goes."""
    if not ip:
        return False
    now = _time.time()
    dq = _parse_hits.get(ip)
    if dq is None:
        dq = deque()
        _parse_hits[ip] = dq
    while dq and now - dq[0] > 3600:
        dq.popleft()
    if len(dq) >= _PARSE_PER_HOUR:
        return True
    last_min = sum(1 for t in dq if now - t <= 60)
    if last_min >= _PARSE_PER_MIN:
        return True
    dq.append(now)
    # Opportunistic cleanup so the dict doesn't grow unbounded across many IPs.
    if len(_parse_hits) > 5000:
        for k in [k for k, v in _parse_hits.items() if not v or now - v[-1] > 3600]:
            _parse_hits.pop(k, None)
    return False


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
        
        # ── Backfill the profile from what the resume just told us ───────
        # The parse was being stored under resumeParsed and nowhere else, so a
        # student who uploaded a resume was still asked for their school and
        # grad year on the profile page — facts their own resume had already
        # given us. Fill only BLANKS: anything the user typed themselves wins,
        # because they know their situation better than a PDF parse does.
        if parsed_info:
            try:
                existing = (db.collection('users').document(user_id).get().to_dict()) or {}
                education = parsed_info.get('education') or {}
                academics = existing.get('academics') or {}

                def _blank(*values) -> bool:
                    return not any(str(v or '').strip() for v in values)

                backfill = {}
                university = str(education.get('university') or '').strip()
                if university and _blank(existing.get('school'), academics.get('school')):
                    backfill['school'] = university
                    backfill['academics.school'] = university

                graduation = str(education.get('graduation') or '').strip()
                if graduation and _blank(existing.get('gradYear'), academics.get('gradYear')):
                    backfill['gradYear'] = graduation
                    backfill['academics.gradYear'] = graduation

                major = str(education.get('major') or '').strip()
                if major and _blank(existing.get('major'), academics.get('major')):
                    backfill['major'] = major
                    backfill['academics.major'] = major

                full_name = str(parsed_info.get('name') or '').strip()
                if full_name and full_name.lower() != 'unknown' and _blank(existing.get('name')):
                    backfill['name'] = full_name

                if backfill:
                    update_data.update(backfill)
                    print(f"[Resume] Backfilled profile fields from resume: {sorted(backfill)}")
            except Exception as backfill_err:
                # Never let a backfill problem cost the user their upload.
                print(f"[Resume] profile backfill skipped: {backfill_err}")

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

        # Abuse backstop: this endpoint is unauthenticated and calls OpenAI, so
        # throttle per-IP before doing any expensive work.
        if _parse_rate_limited(get_remote_address()):
            print("⛔ parse-resume rate limit hit")
            return jsonify({
                'error': 'Too many resume uploads. Please wait a bit and try again.'
            }), 429

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

        # Cap the text sent to the model so one upload can't run up an
        # unbounded OpenAI bill (a real resume is far under this).
        if len(resume_text) > MAX_RESUME_CHARS:
            print(f"✂️  Truncating resume text {len(resume_text)} -> {MAX_RESUME_CHARS} chars before parsing")
            resume_text = resume_text[:MAX_RESUME_CHARS]

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

