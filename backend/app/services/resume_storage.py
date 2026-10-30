"""
Resume files in Firebase Storage: private objects, token-secured links, and
a page-one thumbnail made once at write time.

Why this exists (2026-09-08): both upload paths (the app's /parse-resume and
the website's resume builder) called blob.make_public(), so every resume on
file was readable by anyone who had the storage.googleapis.com path, while
the app's Attachments screen promised "only you can see it". Storage rules
already restrict SDK reads to the owner; the public ACL was the only hole.

The fix keeps every existing reader working. A Firebase download token URL
(firebasestorage.googleapis.com/...?alt=media&token=<128-bit secret>) is
what the website's own SDK uploads already produce, it is not readable
without the secret, it can be revoked by rotating the token, and plain
requests.get() on it succeeds, so gmail_client, job_board, emails.py and the
auto-apply runner need no changes.

Thumbnails: page one rendered with PyMuPDF (already installed as a pdf2docx
dependency) to resumes/{uid}/thumb.png, ~40 KB, so the Profile card and the
Attachments screen never load a multi-megabyte PDF just to draw a preview.
"""
from __future__ import annotations

import logging
import urllib.parse
import os
import uuid

logger = logging.getLogger(__name__)

THUMB_FILENAME = "thumb.png"
THUMB_ZOOM = 0.8  # ~490px wide for a US Letter page


def _bucket():
    from firebase_admin import storage
    return storage.bucket()


def _with_download_token(blob) -> None:
    """Mint the download token as part of the upload (one write instead of
    upload + metadata patch; the Storage emulator can't patch metadata)."""
    blob.metadata = {**(blob.metadata or {}), "firebaseStorageDownloadTokens": uuid.uuid4().hex}


def token_url(blob) -> str:
    """The Firebase download URL for a blob, minting a download token if the
    object has none. Public ACL is never involved."""
    blob.reload()
    meta = dict(blob.metadata or {})
    token = (meta.get("firebaseStorageDownloadTokens") or "").split(",")[0].strip()
    if not token:
        token = uuid.uuid4().hex
        meta["firebaseStorageDownloadTokens"] = token
        blob.metadata = meta
        blob.patch()
    path = urllib.parse.quote(blob.name, safe="")
    return f"{_download_base()}/v0/b/{blob.bucket.name}/o/{path}?alt=media&token={token}"


def _download_base() -> str:
    """Where download links point. Local e2e testing runs the Firebase
    Storage emulator (STORAGE_EMULATOR_HOST, e.g. http://127.0.0.1:9199),
    which serves the same /v0/b/... download path; links must point there so
    nothing reaches the production Storage host."""
    emulator = (os.environ.get("STORAGE_EMULATOR_HOST") or "").strip().rstrip("/")
    if emulator:
        return emulator if "://" in emulator else f"http://{emulator}"
    return "https://firebasestorage.googleapis.com"


def revoke_public(blob) -> bool:
    """Drop anonymous read on an object that make_public() once opened up.
    Returns True when something changed."""
    try:
        blob.acl.reload()
        before = {(e.get("entity"), e.get("role")) for e in blob.acl}
        blob.acl.all().revoke_read()
        blob.acl.save()
        after = {(e.get("entity"), e.get("role")) for e in blob.acl}
        return before != after
    except Exception as exc:
        logger.warning("resume_storage: revoke_public failed for %s: %s", blob.name, exc)
        return False


def render_thumbnail(pdf_bytes: bytes) -> bytes | None:
    """PNG of page one, or None when the file is not a PDF PyMuPDF can open."""
    try:
        import fitz  # PyMuPDF
    except Exception:
        logger.warning("resume_storage: PyMuPDF unavailable, no thumbnail")
        return None
    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
        if doc.page_count < 1:
            return None
        pix = doc[0].get_pixmap(matrix=fitz.Matrix(THUMB_ZOOM, THUMB_ZOOM), alpha=False)
        return pix.tobytes("png")
    except Exception as exc:
        logger.info("resume_storage: thumbnail skipped: %s", exc)
        return None


def store_thumbnail(uid: str, pdf_bytes: bytes) -> str | None:
    png = render_thumbnail(pdf_bytes)
    if not png:
        return None
    try:
        blob = _bucket().blob(f"resumes/{uid}/{THUMB_FILENAME}")
        _with_download_token(blob)
        blob.upload_from_string(png, content_type="image/png")
        return token_url(blob)
    except Exception as exc:
        logger.warning("resume_storage: thumbnail upload failed uid=%s: %s", uid, exc)
        return None


def store_resume(uid: str, filename: str, data: bytes, content_type: str) -> dict:
    """Write the resume privately and return {"url": ..., "thumb_url": ...}.
    url is None when the upload itself failed (callers already handle that)."""
    try:
        blob = _bucket().blob(f"resumes/{uid}/{filename}")
        _with_download_token(blob)
        blob.upload_from_string(data, content_type=content_type or "application/octet-stream")
        url = token_url(blob)
    except Exception as exc:
        logger.error("resume_storage: upload failed uid=%s: %s", uid, exc)
        return {"url": None, "thumb_url": None}
    thumb = store_thumbnail(uid, data) if (content_type or "").lower() == "application/pdf" or filename.lower().endswith(".pdf") else None
    return {"url": url, "thumb_url": thumb}


def delete_thumbnail(uid: str) -> None:
    try:
        blob = _bucket().blob(f"resumes/{uid}/{THUMB_FILENAME}")
        if blob.exists():
            blob.delete()
    except Exception as exc:
        logger.info("resume_storage: thumbnail delete skipped uid=%s: %s", uid, exc)


def storage_path_from_url(url: str) -> str | None:
    """resumes/{uid}/{file} from either URL form we have ever stored:
    storage.googleapis.com/{bucket}/{path} (the old public form) or
    firebasestorage.googleapis.com/v0/b/{bucket}/o/{encoded}."""
    if not url:
        return None
    try:
        p = urllib.parse.urlparse(url)
        if "storage.googleapis.com" in p.netloc and "firebasestorage" not in p.netloc:
            parts = p.path.lstrip("/").split("/", 1)
            return urllib.parse.unquote(parts[1]) if len(parts) == 2 else None
        if "firebasestorage.googleapis.com" in p.netloc and "/o/" in p.path:
            return urllib.parse.unquote(p.path.split("/o/", 1)[1])
        emulator = urllib.parse.urlparse(_download_base())
        if emulator.netloc and p.netloc == emulator.netloc and "/o/" in p.path:
            return urllib.parse.unquote(p.path.split("/o/", 1)[1])
    except Exception:
        return None
    return None
