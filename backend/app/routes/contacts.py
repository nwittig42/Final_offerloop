"""
Contact management routes
"""
import json
from datetime import datetime
from flask import Blueprint, request, jsonify
from firebase_admin import firestore

from ..extensions import require_firebase_auth
from app.services.gmail_client import _load_user_gmail_creds, _gmail_service, check_for_replies
from ..extensions import get_db
from app.utils.exceptions import NotFoundError, ValidationError, OfferloopException
from app.utils.validation import ContactCreateRequest, ContactUpdateRequest, validate_request

contacts_bp = Blueprint('contacts', __name__, url_prefix='/api/contacts')


@contacts_bp.route('', methods=['GET'])
@require_firebase_auth
def get_contacts():
    """Get contacts for a user with pagination"""
    try:
        db = get_db()
        user_id = request.firebase_user['uid']
        
        if not db:
            raise OfferloopException("Database not initialized", error_code="DATABASE_ERROR")
        
        # Get pagination parameters
        per_page = min(request.args.get('per_page', 50, type=int), 100)  # Max 100 per page
        cursor = request.args.get('cursor')  # Document ID for cursor-based pagination
        # Legacy support: still accept page param for backwards compat
        page = request.args.get('page', 1, type=int)
        page = max(1, page)

        contacts_ref = db.collection('users').document(user_id).collection('contacts')

        # Query ordered by createdAt descending
        query = contacts_ref.order_by('createdAt', direction=firestore.Query.DESCENDING)

        # Cursor-based pagination (efficient — no offset scan)
        if cursor:
            try:
                cursor_doc = contacts_ref.document(cursor).get()
                if cursor_doc.exists:
                    query = query.start_after(cursor_doc)
            except Exception:
                pass  # If cursor is invalid, start from beginning
        elif page > 1:
            # Legacy offset fallback for old clients still sending page=
            offset = (page - 1) * per_page
            query = query.offset(offset)

        # Fetch one extra to check if there's a next page
        docs = list(query.limit(per_page + 1).stream())

        has_next = len(docs) > per_page
        items = []
        last_id = None

        for doc in docs[:per_page]:
            d = doc.to_dict()
            d['id'] = doc.id
            items.append(d)
            last_id = doc.id

        return jsonify({
            'contacts': items,
            'pagination': {
                'page': page,
                'per_page': per_page,
                'total_items': len(items),
                'has_next': has_next,
                'has_prev': page > 1 or cursor is not None,
                'next_cursor': last_id if has_next else None,
            }
        })
        
    except OfferloopException:
        raise
    except Exception as e:
        print(f"Error getting contacts: {str(e)}")
        raise OfferloopException(f"Failed to retrieve contacts: {str(e)}", error_code="CONTACTS_FETCH_ERROR")


@contacts_bp.route('', methods=['POST'])
@require_firebase_auth
def create_contact():
    """Create a new contact with validation"""
    try:
        db = get_db()
        data = request.get_json() or {}
        user_id = request.firebase_user['uid']
        
        if not db:
            raise OfferloopException("Database not initialized", error_code="DATABASE_ERROR")
        
        # Validate input
        try:
            validated_data = validate_request(ContactCreateRequest, data)
        except ValidationError as ve:
            return ve.to_response()
        
        today = datetime.now().strftime('%m/%d/%Y')
        contact = {
            'firstName': validated_data.get('firstName', ''),
            'lastName': validated_data.get('lastName', ''),
            'linkedinUrl': validated_data.get('linkedinUrl', ''),
            'email': validated_data.get('email', ''),
            'company': validated_data.get('company', ''),
            'jobTitle': validated_data.get('jobTitle', ''),
            'college': validated_data.get('college', ''),
            'location': validated_data.get('location', ''),
            'firstContactDate': today,
            'status': 'Not Contacted',
            'lastContactDate': today,
            'userId': user_id,
            'createdAt': datetime.utcnow().isoformat() + "Z",  # TODO: deprecated in Python 3.12
        }
        
        doc_ref = db.collection('users').document(user_id).collection('contacts').add(contact)
        contact['id'] = doc_ref[1].id
        
        return jsonify({'contact': contact}), 201
        
    except OfferloopException:
        raise
    except Exception as e:
        print(f"Error creating contact: {str(e)}")
        raise OfferloopException(f"Failed to create contact: {str(e)}", error_code="CONTACT_CREATE_ERROR")


@contacts_bp.route('/<contact_id>', methods=['PUT'])
@require_firebase_auth
def update_contact(contact_id):
    """Update an existing contact with validation"""
    try:
        db = get_db()
        data = request.get_json() or {}
        user_id = request.firebase_user['uid']
        
        if not db:
            raise OfferloopException("Database not initialized", error_code="DATABASE_ERROR")
        
        # Validate input
        try:
            validated_data = validate_request(ContactUpdateRequest, data)
        except ValidationError as ve:
            return ve.to_response()
        
        ref = db.collection('users').document(user_id).collection('contacts').document(contact_id)
        doc = ref.get()
        
        if not doc.exists:
            raise NotFoundError("Contact")
        
        # Build update dict from validated data
        update = {}
        allowed_fields = ['firstName', 'lastName', 'linkedinUrl', 'email', 'company', 'jobTitle', 'college', 'location', 'status']
        for field in allowed_fields:
            if field in validated_data:
                update[field] = validated_data[field]
        
        # Handle status change - update lastContactDate
        if 'status' in update:
            current = doc.to_dict()
            if current.get('status') != update['status']:
                update['lastContactDate'] = datetime.now().strftime('%m/%d/%Y')
        
        if update:
            ref.update(update)
        
        out = ref.get().to_dict()
        out['id'] = contact_id
        
        return jsonify({'contact': out})
        
    except (OfferloopException, NotFoundError):
        raise
    except Exception as e:
        print(f"Error updating contact: {str(e)}")
        raise OfferloopException(f"Failed to update contact: {str(e)}", error_code="CONTACT_UPDATE_ERROR")


@contacts_bp.route('/<contact_id>', methods=['DELETE'])
@require_firebase_auth
def delete_contact(contact_id):
    """Delete a contact"""
    try:
        db = get_db()
        user_id = request.firebase_user['uid']
        
        if not db:
            raise OfferloopException("Database not initialized", error_code="DATABASE_ERROR")
        
        ref = db.collection('users').document(user_id).collection('contacts').document(contact_id)
        
        if not ref.get().exists:
            raise NotFoundError("Contact")

        ref.delete()

        return jsonify({'message': 'Contact deleted successfully'})
        
    except (OfferloopException, NotFoundError):
        raise
    except Exception as e:
        print(f"Error deleting contact: {str(e)}")
        raise OfferloopException(f"Failed to delete contact: {str(e)}", error_code="CONTACT_DELETE_ERROR")


@contacts_bp.route('/<contact_id>/check-replies', methods=['GET'])
@require_firebase_auth
def check_contact_replies(contact_id):
    """Check if a contact has replied to our email"""
    try:
        db = get_db()
        user_id = request.firebase_user['uid']
        
        # Get contact from Firestore
        contact_ref = db.collection('users').document(user_id).collection('contacts').document(contact_id)
        contact_doc = contact_ref.get()
        
        if not contact_doc.exists:
            return jsonify({'error': 'Contact not found'}), 404
        
        contact_data = contact_doc.to_dict()
        thread_id = contact_data.get('gmailThreadId')
        email = contact_data.get('email')
        
        if not thread_id or not email:
            return jsonify({'hasReply': False, 'isUnread': False})
        
        # Get Gmail service
        creds = _load_user_gmail_creds(user_id)
        if not creds:
            return jsonify({'error': 'Gmail not connected'}), 401
        
        gmail_service = _gmail_service(creds)
        
        # Check for replies
        reply_status = check_for_replies(gmail_service, thread_id, email)
        
        # Update contact with reply status
        contact_ref.update({
            'hasUnreadReply': reply_status['isUnread'],
            'lastChecked': datetime.now().isoformat()
        })
        
        return jsonify(reply_status)
        
    except Exception as e:
        print(f"Error checking replies: {e}")
        return jsonify({'error': str(e)}), 500


@contacts_bp.route('/<contact_id>/mute-notifications', methods=['POST'])
@require_firebase_auth
def mute_contact_notifications(contact_id):
    """Mute/unmute notifications for a contact"""
    try:
        db = get_db()
        user_id = request.firebase_user['uid']
        data = request.get_json() or {}
        muted = data.get('muted', True)
        
        contact_ref = db.collection('users').document(user_id).collection('contacts').document(contact_id)
        
        if not contact_ref.get().exists:
            return jsonify({'error': 'Contact not found'}), 404
        
        contact_ref.update({
            'notificationsMuted': muted,
            'mutedAt': datetime.now().isoformat() if muted else None
        })
        
        return jsonify({'success': True, 'muted': muted})
        
    except Exception as e:
        print(f"Error muting notifications: {e}")
        return jsonify({'error': str(e)}), 500


@contacts_bp.route('/batch-check-replies', methods=['POST', 'OPTIONS'])
@require_firebase_auth
def batch_check_replies():
    """Check replies for multiple contacts at once"""
    # Handle CORS preflight
    if request.method == 'OPTIONS':
        return '', 200
    
    try:
        db = get_db()
        user_id = request.firebase_user['uid']
        data = request.get_json() or {}
        contact_ids = data.get('contactIds', [])
        
        if not contact_ids:
            return jsonify({'results': {}})
        
        # Get Gmail service
        creds = _load_user_gmail_creds(user_id)
        if not creds:
            return jsonify({'error': 'Gmail not connected'}), 401
        
        gmail_service = _gmail_service(creds)
        results = {}
        
        for contact_id in contact_ids[:20]:  # Limit to 20 at a time
            try:
                contact_ref = db.collection('users').document(user_id).collection('contacts').document(contact_id)
                contact_doc = contact_ref.get()
                
                if not contact_doc.exists:
                    continue
                
                contact_data = contact_doc.to_dict()
                
                # Skip if notifications are muted
                if contact_data.get('notificationsMuted'):
                    results[contact_id] = {'hasReply': False, 'isUnread': False, 'muted': True}
                    continue
                
                thread_id = contact_data.get('gmailThreadId')
                email = contact_data.get('email')
                
                if thread_id and email:
                    reply_status = check_for_replies(gmail_service, thread_id, email)
                    results[contact_id] = reply_status
                    
                    # Update in Firestore
                    contact_ref.update({
                        'hasUnreadReply': reply_status['isUnread'],
                        'lastChecked': datetime.now().isoformat()
                    })
            except Exception as e:
                print(f"Error checking contact {contact_id}: {e}")
                continue
        
        return jsonify({'results': results})
        
    except Exception as e:
        print(f"Error batch checking replies: {e}")
        return jsonify({'error': str(e)}), 500


@contacts_bp.route('/<contact_id>/generate-reply', methods=['POST'])
@require_firebase_auth
def generate_reply_draft(contact_id):
    """Generate a reply draft for a contact's message"""
    try:
        import base64
        from email.mime.text import MIMEText
        db = get_db()
        user_id = request.firebase_user['uid']
        
        # Get contact
        contact_ref = db.collection('users').document(user_id).collection('contacts').document(contact_id)
        contact_doc = contact_ref.get()
        
        if not contact_doc.exists:
            return jsonify({'error': 'Contact not found'}), 404
        
        contact_data = contact_doc.to_dict()
        thread_id = contact_data.get('gmailThreadId')
        
        if not thread_id:
            return jsonify({'error': 'No Gmail thread found'}), 400
        
        # Get Gmail service
        creds = _load_user_gmail_creds(user_id)
        if not creds:
            return jsonify({'error': 'Gmail not connected'}), 401
        
        gmail_service = _gmail_service(creds)
        
        # Get the latest message in the thread
        thread = gmail_service.users().threads().get(
            userId='me',
            id=thread_id,
            format='full'
        ).execute()
        
        messages = thread.get('messages', [])
        if not messages:
            return jsonify({'error': 'No messages in thread'}), 400
        
        latest_message = messages[-1]
        
        # Extract message body (simplified)
        payload = latest_message.get('payload', {})
        body = ''
        
        if 'parts' in payload:
            for part in payload['parts']:
                if part['mimeType'] == 'text/plain':
                    body = base64.urlsafe_b64decode(part['body']['data']).decode('utf-8')
                    break
        elif 'body' in payload and 'data' in payload['body']:
            body = base64.urlsafe_b64decode(payload['body']['data']).decode('utf-8')
        
        # Generate reply using AI
        contact_name = (contact_data.get('firstName') or '').strip()
        company = (contact_data.get('company') or '').strip()
        email_subject = contact_data.get('emailSubject') or 'Our conversation'
        snippet = contact_data.get('lastMessageSnippet') or ''

        try:
            from app.services.openai_client import get_openai_client
            oai = get_openai_client()
            prompt = (
                f"Write a short, professional reply email (3-5 sentences) to {contact_name}"
                f"{(' at ' + company) if company else ''}.\n"
                f"Subject: {email_subject}\n"
                f"Their latest message: {body[:500] if body else snippet}\n\n"
                "Be warm but concise. Do not include a subject line. "
                "End with a professional sign-off like 'Best regards'."
            )
            completion = oai.chat.completions.create(
                model="gpt-4o-mini",
                messages=[
                    {"role": "system", "content": "You are a professional networking assistant. Write concise, natural reply emails."},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=300,
                temperature=0.7,
            )
            reply_text = (completion.choices[0].message.content or "").strip()
        except Exception as ai_err:
            print(f"AI reply generation failed, using empty draft: {ai_err}")
            reply_text = ""
        
        # Create draft reply in Gmail
        from app.services import email_tracking
        tracking_token = email_tracking.start(user_id)
        message = email_tracking.tracked_text_mime(reply_text, tracking_token)
        message['to'] = contact_data.get('email')
        message['subject'] = f"Re: {contact_data.get('emailSubject', 'Our conversation')}"
        
        raw = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')
        
        draft_body = {
            'message': {
                'raw': raw,
                'threadId': thread_id
            }
        }
        
        draft = gmail_service.users().drafts().create(userId='me', body=draft_body).execute()
        email_tracking.register(
            tracking_token, user_id, 'reply', recipient_email=contact_data.get('email'),
            contact_id=contact_id, draft_id=draft.get('id'),
            message_id=draft.get('message', {}).get('id'), thread_id=thread_id,
        )
        
        # Mark as read
        gmail_service.users().threads().modify(
            userId='me',
            id=thread_id,
            body={'removeLabelIds': ['UNREAD']}
        ).execute()
        
        # Update contact
        contact_ref.update({
            'hasUnreadReply': False,
            'lastReplyDraftId': draft['id']
        })
        
        return jsonify({
            'success': True,
            'draftId': draft['id'],
            'threadId': thread_id,
            'gmailUrl': f"https://mail.google.com/mail/u/0/#draft/{draft['id']}"
        })
        
    except Exception as e:
        print(f"Error generating reply: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@contacts_bp.route('/bulk', methods=['POST'])
@require_firebase_auth
def bulk_create_contacts():
    """Bulk create contacts with validation and deduplication"""
    try:
        db = get_db()
        data = request.get_json() or {}
        user_id = request.firebase_user['uid']
        raw_contacts = data.get('contacts') or []
        if not db:
            return jsonify({'error': 'Firebase not initialized'}), 500

        MAX_BULK_CONTACTS = 500
        if len(raw_contacts) > MAX_BULK_CONTACTS:
            return jsonify({'error': f'Too many contacts. Maximum {MAX_BULK_CONTACTS} per request.'}), 400
        
        contacts_ref = db.collection('users').document(user_id).collection('contacts')
        created = 0
        skipped = 0
        created_contacts = []
        today = datetime.now().strftime('%m/%d/%Y')
        
        for idx, rc in enumerate(raw_contacts):
            first_name = (rc.get('FirstName') or rc.get('firstName') or '').strip()
            last_name = (rc.get('LastName') or rc.get('lastName') or '').strip()
            email = (rc.get('Email') or rc.get('WorkEmail') or rc.get('PersonalEmail') or rc.get('email') or '').strip()
            linkedin = (rc.get('LinkedIn') or rc.get('linkedinUrl') or '').strip()
            company = (rc.get('Company') or rc.get('company') or '').strip()
            job_title = (rc.get('Title') or rc.get('jobTitle') or '').strip()
            college = (rc.get('College') or rc.get('college') or '').strip()
            city = (rc.get('City') or '').strip()
            state = (rc.get('State') or '').strip()
            location = (rc.get('location') or ', '.join([v for v in [city, state] if v]) or '').strip()
            pdl_id = (rc.get('pdlId') or rc.get('pdl_id') or '').strip()
            
            # Skip if missing critical fields
            if not (first_name and last_name):
                skipped += 1
                continue
            
            # Check for duplicates - check both email and LinkedIn
            # This matches the same logic used in exclusion
            is_duplicate = False
            
            # Check by email if available
            if email:
                email_query = contacts_ref.where('email', '==', email).limit(1)
                email_docs = list(email_query.stream())
                if email_docs:
                    is_duplicate = True
            
            # Check by LinkedIn if available and not already found as duplicate
            if not is_duplicate and linkedin:
                linkedin_query = contacts_ref.where('linkedinUrl', '==', linkedin).limit(1)
                linkedin_docs = list(linkedin_query.stream())
                if linkedin_docs:
                    is_duplicate = True
            
            # Also check by name + company combination (for cases where email/LinkedIn might differ slightly)
            if not is_duplicate and first_name and last_name and company:
                name_company_query = contacts_ref.where('firstName', '==', first_name).where('lastName', '==', last_name).where('company', '==', company).limit(1)
                name_company_docs = list(name_company_query.stream())
                if name_company_docs:
                    is_duplicate = True
            
            if is_duplicate:
                # DON'T update email fields for duplicates - preserve the existing draft relationship
                # The user already has a draft for this contact, so we keep the original emailBody
                # and gmailDraftUrl to maintain consistency between Firestore and Gmail draft
                
                # Find the existing contact document to check if it needs any non-email updates
                existing_doc = None
                if email:
                    email_query = contacts_ref.where('email', '==', email).limit(1)
                    email_docs = list(email_query.stream())
                    if email_docs:
                        existing_doc = email_docs[0]
                elif linkedin:
                    linkedin_query = contacts_ref.where('linkedinUrl', '==', linkedin).limit(1)
                    linkedin_docs = list(linkedin_query.stream())
                    if linkedin_docs:
                        existing_doc = linkedin_docs[0]
                elif first_name and last_name and company:
                    name_company_query = contacts_ref.where('firstName', '==', first_name).where('lastName', '==', last_name).where('company', '==', company).limit(1)
                    name_company_docs = list(name_company_query.stream())
                    if name_company_docs:
                        existing_doc = name_company_docs[0]
                
                # Only update non-email fields if needed (e.g., lastContactDate, status, etc.)
                # Do NOT update: emailSubject, emailBody, gmailDraftId, gmailDraftUrl
                # This preserves the relationship between Firestore emailBody and Gmail draft
                if existing_doc:
                    update_data = {
                        'updatedAt': datetime.now().isoformat(),
                    }
                    # Optionally update other non-email fields here if needed
                    # For example, you might want to update lastContactDate if the contact was searched again
                    existing_doc.reference.update(update_data)
                    print(f"✅ Updated existing contact {first_name} {last_name} (preserved email content and draft URL)")
                
                skipped += 1
                print(f"🚫 Skipping duplicate contact: {first_name} {last_name} ({email or linkedin or 'no email/linkedin'}) - preserving existing email content and draft")
                continue
            
            # Get email subject and body if available (from generated emails)
            email_subject = (rc.get('emailSubject') or rc.get('email_subject') or '').strip()
            email_body = (rc.get('emailBody') or rc.get('email_body') or '').strip()
            gmail_draft_id = (rc.get('gmailDraftId') or rc.get('gmail_draft_id') or '').strip()
            gmail_draft_url = (rc.get('gmailDraftUrl') or rc.get('gmail_draft_url') or '').strip()
            
            contact = {
                'firstName': first_name,
                'lastName': last_name,
                'email': email,
                'linkedinUrl': linkedin,
                'company': company,
                'jobTitle': job_title,
                'college': college,
                'location': location,
                'city': city,
                'state': state,
                'firstContactDate': today,
                'status': 'Not Contacted',
                'lastContactDate': today,
                'userId': user_id,
                'createdAt': datetime.utcnow().isoformat() + "Z",  # TODO: deprecated in Python 3.12
                'lastActivityAt': datetime.utcnow().isoformat() + "Z",  # TODO: deprecated in Python 3.12
                # pdlId for agentic queue dedup (new in Phase 1). PDL search routes
                # (runs_hunter.py et al.) return pdlId on each contact; frontend
                # passes it through to this bulk-add endpoint.
                'pdlId': pdl_id,
            }

            # Add email subject and body if available (from generated personalized emails)
            if email_subject:
                contact['emailSubject'] = email_subject
            if email_body:
                contact['emailBody'] = email_body
            # Add Gmail draft URL if available (draft has resume attached)
            if gmail_draft_id:
                contact['gmailDraftId'] = gmail_draft_id
                contact['pipelineStage'] = 'draft_created'
                contact['draftCreatedAt'] = datetime.utcnow().isoformat() + "Z"  # TODO: deprecated in Python 3.12
                contact['draftStillExists'] = True
            else:
                contact['draftStillExists'] = False
            if gmail_draft_url:
                contact['gmailDraftUrl'] = gmail_draft_url

            # Outbox fields required for tracker visibility
            contact['inOutbox'] = True
            contact['draftToEmail'] = (rc.get('draftToEmail') or '').strip().lower() or email
            contact['hasUnreadReply'] = False
            contact['gmailMessageId'] = (rc.get('gmailMessageId') or rc.get('gmail_message_id') or '').strip() or None

            # Set pipelineStage to "new" for contacts without drafts (from prompt search)
            if 'pipelineStage' not in contact:
                # Accept pipelineStage from request if provided and valid
                req_stage = (rc.get('pipelineStage') or '').strip()
                if req_stage in ('new', 'draft_created'):
                    contact['pipelineStage'] = req_stage
                else:
                    contact['pipelineStage'] = 'new'
            
            doc_ref = contacts_ref.add(contact)
            contact['id'] = doc_ref[1].id
            created_contacts.append(contact)
            created += 1
        
        return jsonify({
            'created': created,
            'skipped': skipped,
            'contacts': created_contacts
        })
        
    except OfferloopException:
        raise
    except Exception as e:
        print(f"Error bulk creating contacts: {str(e)}")
        import traceback
        traceback.print_exc()
        raise OfferloopException(f"Failed to bulk create contacts: {str(e)}", error_code="BULK_CREATE_ERROR")


@contacts_bp.route('/<contact_id>', methods=['GET'])
@require_firebase_auth
def get_contact(contact_id):
    """Get a single contact by ID"""
    try:
        db = get_db()
        user_id = request.firebase_user['uid']
        
        if not db:
            raise OfferloopException("Database not initialized", error_code="DATABASE_ERROR")
        
        contact_ref = db.collection('users').document(user_id).collection('contacts').document(contact_id)
        contact_doc = contact_ref.get()
        
        if not contact_doc.exists:
            raise NotFoundError("Contact")
        
        contact = contact_doc.to_dict()
        contact['id'] = contact_id
        
        return jsonify({'contact': contact})
        
    except (OfferloopException, NotFoundError):
        raise
    except Exception as e:
        print(f"Error getting contact: {str(e)}")
        raise OfferloopException(f"Failed to retrieve contact: {str(e)}", error_code="CONTACT_FETCH_ERROR")


@contacts_bp.route('/bulk-delete', methods=['POST'])
@require_firebase_auth
def bulk_delete_contacts():
    """Bulk delete contacts by IDs"""
    try:
        db = get_db()
        user_id = request.firebase_user['uid']
        data = request.get_json() or {}
        contact_ids = data.get('contactIds', [])
        
        if not db:
            raise OfferloopException("Database not initialized", error_code="DATABASE_ERROR")
        
        if not contact_ids or not isinstance(contact_ids, list):
            raise ValidationError("contactIds must be a non-empty array", field="contactIds")
        
        if len(contact_ids) > 100:
            raise ValidationError("Cannot delete more than 100 contacts at once", field="contactIds")
        
        deleted_count = 0
        not_found = []
        
        contacts_ref = db.collection('users').document(user_id).collection('contacts')
        
        for contact_id in contact_ids:
            contact_ref = contacts_ref.document(contact_id)
            if contact_ref.get().exists:
                contact_ref.delete()
                deleted_count += 1
            else:
                not_found.append(contact_id)

        return jsonify({
            'deleted': deleted_count,
            'not_found': not_found,
            'message': f'Successfully deleted {deleted_count} contact(s)'
        })
        
    except (OfferloopException, ValidationError):
        raise
    except Exception as e:
        print(f"Error bulk deleting contacts: {str(e)}")
        raise OfferloopException(f"Failed to bulk delete contacts: {str(e)}", error_code="BULK_DELETE_ERROR")


@contacts_bp.route('/refresh-warmth', methods=['POST'])
@require_firebase_auth
def refresh_warmth_scores():
    """Re-score all saved contacts against the user's profile and update Firestore."""
    try:
        from app.utils.warmth_scoring import compute_warmth_score, _build_user_comparison_data

        db = get_db()
        uid = request.firebase_user['uid']

        # Load user profile
        user_doc = db.collection('users').document(uid).get()
        if not user_doc.exists:
            return jsonify({'error': 'User not found'}), 404
        user_data = user_doc.to_dict()
        comparison = _build_user_comparison_data(user_data)

        # Load all contacts
        contacts_ref = db.collection('users').document(uid).collection('contacts')
        docs = list(contacts_ref.stream())

        updated = 0
        batch = db.batch()
        batch_count = 0

        for doc in docs:
            contact = doc.to_dict()
            result = compute_warmth_score(comparison, contact)

            batch.update(doc.reference, {
                'warmthScore': result['score'],
                'warmthTier': result['tier'],
                'warmthSignals': [s.get('signal', '') for s in result['signals']],
            })
            batch_count += 1
            updated += 1

            # Firestore batch limit is 500
            if batch_count >= 450:
                batch.commit()
                batch = db.batch()
                batch_count = 0

        if batch_count > 0:
            batch.commit()

        return jsonify({'updated': updated, 'message': f'Scored {updated} contacts'})

    except Exception as e:
        print(f"[RefreshWarmth] Error: {e}")
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# Reply Coach endpoints
# ---------------------------------------------------------------------------

@contacts_bp.route('/<contact_id>/reply-draft', methods=['GET'])
@require_firebase_auth
def get_reply_draft(contact_id):
    """Get or generate a reply draft for a contact.

    ?refresh=1 forces a fresh generation, bypassing both the ready-draft cache
    and the in-flight pending doc. The inbox Generate button always passes
    this so cached webhook-path drafts (latest-snippet-only, pre-thread-aware)
    don't surface as stale on the first click for replied contacts.
    """
    try:
        uid = request.firebase_user['uid']
        refresh = request.args.get('refresh', '').lower() in ('1', 'true', 'yes')
        from app.services.reply_coach import get_reply_draft as _get_draft
        draft = _get_draft(uid, contact_id, refresh=refresh)
        if draft is None:
            return jsonify({"error": "No reply context available"}), 404
        return jsonify(draft)
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@contacts_bp.route('/<contact_id>/reply-draft/send', methods=['POST'])
@require_firebase_auth
def send_reply_draft(contact_id):
    """Create a Gmail draft from a reply coach draft."""
    try:
        uid = request.firebase_user['uid']
        db = get_db()
        data = request.get_json() or {}
        body = data.get("body", "").strip()
        if not body:
            return jsonify({"error": "Reply body is required"}), 400

        # Get contact to find thread
        contact_ref = db.collection('users').document(uid).collection('contacts').document(contact_id)
        contact_doc = contact_ref.get()
        if not contact_doc.exists:
            return jsonify({"error": "Contact not found"}), 404

        contact_data = contact_doc.to_dict() or {}
        thread_id = contact_data.get("gmailThreadId")
        to_email = contact_data.get("email") or contact_data.get("draftToEmail", "")
        subject = contact_data.get("emailSubject") or contact_data.get("draftSubject", "")

        if not to_email:
            return jsonify({"error": "No email address for contact"}), 400

        # Create Gmail draft
        from app.services.gmail_client import _load_user_gmail_creds, _gmail_service
        creds = _load_user_gmail_creds(uid)
        if not creds:
            return jsonify({"error": "Gmail not connected"}), 400

        service = _gmail_service(creds)
        import base64
        from email.mime.text import MIMEText
        from app.services import email_tracking
        tracking_token = email_tracking.start(uid)
        msg = email_tracking.tracked_text_mime(body, tracking_token)
        msg["to"] = to_email
        msg["subject"] = f"Re: {subject}" if subject and not subject.lower().startswith("re:") else subject
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()

        draft_body = {"message": {"raw": raw}}
        if thread_id:
            draft_body["message"]["threadId"] = thread_id

        draft = service.users().drafts().create(userId="me", body=draft_body).execute()
        email_tracking.register(
            tracking_token, uid, "reply", recipient_email=to_email,
            contact_id=contact_id, draft_id=draft.get("id"),
            message_id=draft.get("message", {}).get("id"), thread_id=thread_id,
        )

        # Log reply_response_sent metric
        from app.utils.metrics_events import log_event
        log_event(uid, "reply_response_sent", {
            "contact_id": contact_id,
            "used_auto_draft": True,
            "edited_before_send": data.get("edited", False),
        })

        return jsonify({"draftId": draft.get("id"), "status": "draft_created"})

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------------------------------------------------------------------
# Auto-Prep endpoint (on-demand fallback)
# ---------------------------------------------------------------------------

@contacts_bp.route('/<contact_id>/auto-prep', methods=['GET'])
@require_firebase_auth
def get_auto_prep(contact_id):
    """Get auto-prep status for a contact (meeting_scheduled stage)."""
    try:
        uid = request.firebase_user['uid']
        db = get_db()

        # Check for existing coffee chat prep
        preps_ref = db.collection('users').document(uid).collection('coffee-chat-preps')
        preps = list(preps_ref.where("contactId", "==", contact_id).order_by("createdAt", direction="DESCENDING").limit(1).stream())
        if preps:
            prep_data = preps[0].to_dict()
            return jsonify({"status": prep_data.get("status", "unknown"), "prepId": preps[0].id})

        # Check pending doc
        pending_ref = db.collection('users').document(uid).collection('pending_auto_preps').document(contact_id)
        pending_doc = pending_ref.get()
        if pending_doc.exists:
            pending_data = pending_doc.to_dict() or {}
            status = pending_data.get("status")
            if status == "pending":
                created_str = pending_data.get("createdAt", "")
                if created_str:
                    try:
                        from datetime import datetime as dt, timezone as tz
                        created_dt = dt.fromisoformat(created_str.replace("Z", "+00:00"))
                        age_minutes = (dt.now(tz.utc) - created_dt).total_seconds() / 60
                        if age_minutes < 10:
                            return jsonify({"status": "generating"})
                    except Exception:
                        pass
            # Stale or failed — fall through to trigger

        # No prep exists — trigger on-demand if stage is meeting_scheduled
        contact_ref = db.collection('users').document(uid).collection('contacts').document(contact_id)
        contact_doc = contact_ref.get()
        if not contact_doc.exists:
            return jsonify({"error": "Contact not found"}), 404

        contact_data = contact_doc.to_dict() or {}
        if contact_data.get("pipelineStage") != "meeting_scheduled":
            return jsonify({"status": "not_applicable"})

        # Trigger auto-prep
        from app.services.outbox_service import trigger_auto_prep
        result = trigger_auto_prep(uid, contact_id, contact_data)
        return jsonify(result)

    except Exception as e:
        return jsonify({"error": str(e)}), 500


@contacts_bp.route('/clear-mcp-unseen', methods=['POST'])
@require_firebase_auth
def clear_mcp_unseen():
    """Clear `mcpUnseen=true` on every contact for the current user.

    The frontend calls this once after rendering My Network — the MCP
    persistence layer (app/mcp_server/persist.py) writes new contacts
    with mcpUnseen=true so the UI can show a one-time orange highlight,
    and this endpoint flips them all to false so the highlight doesn't
    persist across reloads.
    """
    try:
        uid = request.firebase_user['uid']
        db = get_db()
        if not db:
            return jsonify({"error": "Database not initialized"}), 500
        from app.mcp_server.persist import clear_mcp_unseen_for_user
        cleared = clear_mcp_unseen_for_user(uid, db)
        return jsonify({"cleared": cleared})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@contacts_bp.route('/<contact_id>/gmail-draft', methods=['POST'])
@require_firebase_auth
def push_contact_gmail_draft(contact_id):
    """POST /api/contacts/<contact_id>/gmail-draft

    Copy the draft saved on this contact (written in-app by ranker outreach
    with create_gmail_draft=false) into the user's Gmail drafts. Free; makes
    no new draft when the contact already has one.

    200 {gmailDraftId, gmailDraftUrl, created}
    400 no_saved_draft
    404 contact_not_found
    409 gmail_not_connected | gmail_scope_missing
    502 gmail_draft_failed
    """
    import logging
    from app.services import gmail_client
    from app.services.first_email_send import SEND_SCOPES

    uid = request.firebase_user['uid']
    db = get_db()
    if not db:
        return jsonify({"error": "Database not initialized"}), 500
    contact_id = (contact_id or "").strip()
    if not contact_id:
        return jsonify({"error": "contact_not_found"}), 404

    user_ref = db.collection('users').document(uid)
    contact_ref = user_ref.collection('contacts').document(contact_id)
    snap = contact_ref.get()
    if not snap.exists:
        return jsonify({"error": "contact_not_found"}), 404
    contact = snap.to_dict() or {}

    if contact.get("gmailDraftId"):
        return jsonify({
            "gmailDraftId": contact["gmailDraftId"],
            "gmailDraftUrl": contact.get("gmailDraftUrl") or "",
            "created": False,
        })

    subject = (contact.get("emailSubject") or "").strip()
    body = (contact.get("emailBody") or "").strip()
    to_email = (contact.get("draftToEmail") or "").strip()
    if not body or not to_email:
        return jsonify({"error": "no_saved_draft"}), 400

    try:
        creds = _load_user_gmail_creds(uid)
    except Exception:
        creds = None
    if not creds:
        return jsonify({"error": "gmail_not_connected"}), 409
    scopes = set(getattr(creds, "scopes", None) or [])
    if scopes and not (scopes & (SEND_SCOPES | {"https://www.googleapis.com/auth/gmail.modify"})):
        return jsonify({"error": "gmail_scope_missing"}), 409

    user_data = {}
    try:
        user_snap = user_ref.get()
        if user_snap.exists:
            user_data = user_snap.to_dict() or {}
    except Exception:
        pass
    user_email = request.firebase_user.get("email") or user_data.get("email") or ""
    user_tier = user_data.get("subscriptionTier", user_data.get("tier", "free")) or "free"
    user_info = {"name": user_data.get("name", ""), "email": user_email, "phone": "", "linkedin": ""}

    # Same inputs as ranker outreach: the resume is downloaded once and attached.
    resume_bytes, resume_filename = None, user_data.get("resumeFileName")
    resume_url = user_data.get("resumeUrl") or user_data.get("resumeURL")
    if resume_url:
        try:
            resume_bytes, downloaded_name = gmail_client.download_resume_from_url(resume_url)
            if resume_bytes and not resume_filename:
                resume_filename = downloaded_name
        except Exception as exc:
            logging.getLogger(__name__).warning("gmail-draft: resume download failed uid=%s: %s", uid, exc)

    gmail_contact = {
        "FirstName": contact.get("firstName") or "",
        "LastName": contact.get("lastName") or "",
        "Email": to_email,
        "Company": contact.get("company") or "",
        "Title": contact.get("jobTitle") or "",
        "LinkedIn": contact.get("linkedinUrl") or "",
        "College": contact.get("college") or "",
    }
    try:
        result = gmail_client.create_gmail_draft_for_user(
            gmail_contact, subject, body, tier=user_tier, user_email=user_email,
            resume_content=resume_bytes, resume_filename=resume_filename,
            user_info=user_info, user_id=uid,
        )
    except Exception:
        logging.getLogger(__name__).exception("gmail-draft failed uid=%s contact=%s", uid, contact_id)
        return jsonify({"error": "gmail_draft_failed"}), 502

    draft_id = result.get("draft_id", "") if isinstance(result, dict) else ""
    if not draft_id or str(draft_id).startswith("mock_"):
        return jsonify({"error": "gmail_draft_failed"}), 502
    draft_url = result.get("draft_url") or ""

    contact_ref.set({
        "gmailDraftId": draft_id,
        "gmailDraftUrl": draft_url,
        "pipelineStage": "draft_created",
        "draftStillExists": True,
    }, merge=True)
    return jsonify({"gmailDraftId": draft_id, "gmailDraftUrl": draft_url, "created": True})


@contacts_bp.route('/<contact_id>/send-first-email', methods=['POST'])
@require_firebase_auth
def send_first_email_route(contact_id):
    """POST /api/contacts/<contact_id>/send-first-email

    Send this contact's first email: exactly the saved draft the user
    confirmed, at most once (app.services.first_email_send).

    Body: {"confirmed_hash": str,      # draft_hash of the subject/body shown
           "surface": str,             # "web_walkthrough" completes the walkthrough
           "dry_run": bool,            # review only: recipient, blocking, warnings
           "confirm_not_sent": bool}   # after a "check your Sent folder" prompt

    200 {sent, alreadySent, contactId, messageId, threadId}
    200 dry_run {recipient, subject, hash, blocking, warnings, alreadySent}
    401 gmail_not_connected | gmail_token_expired
    403 gmail_scope_missing
    404 contact_not_found
    409 draft_changed | send_in_progress | send_unconfirmed
    422 blocked {blocking: [...]}
    429 daily_cap_reached
    502 send_failed
    """
    from app.services import first_email_send as fes

    uid = request.firebase_user['uid']
    db = get_db()
    if not db:
        return jsonify({"error": "Database not initialized"}), 500
    data = request.get_json(silent=True) or {}
    contact_id = (contact_id or "").strip()
    if not contact_id:
        return jsonify({"error": "contact_not_found"}), 404

    if data.get("dry_run") is True:
        info = fes.review(db, uid, contact_id)
        if info is None:
            return jsonify({"error": "contact_not_found"}), 404
        return jsonify({
            "recipient": info.recipient,
            "subject": info.subject,
            "hash": info.hash,
            "blocking": info.blocking,
            "warnings": info.warnings,
            "alreadySent": bool(info.contact.get("emailSent")),
        })

    try:
        result = fes.send_first_email(
            db, uid, contact_id, str(data.get("confirmed_hash") or ""),
            surface=str(data.get("surface") or "")[:40],
            confirm_not_sent=data.get("confirm_not_sent") is True,
        )
        return jsonify(result), 200
    except fes.SendError as err:
        return jsonify({"error": err.code, **err.extra}), err.status
    except Exception:
        import logging
        logging.getLogger(__name__).exception("send-first-email failed uid=%s contact=%s", uid, contact_id)
        return jsonify({"error": "send_failed"}), 502
