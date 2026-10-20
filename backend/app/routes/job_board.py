"""
Job Board API endpoints - job listings, resume optimization, and cover letter generation.
Uses SerpAPI Google Jobs for real job listings with Firestore caching.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Set, Tuple
from urllib.parse import urlparse, parse_qs

from flask import Blueprint, jsonify, request, current_app
import requests
from bs4 import BeautifulSoup

from app.extensions import require_firebase_auth, require_tier, get_db, get_limiter, get_rate_limit_key
from app.services.auth import deduct_credits_atomic, refund_credits_atomic, check_and_reset_credits
from app.services.openai_client import get_async_openai_client, get_openai_client
from app.services.ats_scorer import calculate_ats_score
from app.services.recruiter_finder import find_recruiters, determine_job_type, find_hiring_manager
from app.utils.users import get_outreach_email
from app.services.resume_optimizer_v2 import optimize_resume_v2 as run_resume_optimization
from app.services.career_stage import (
    derive_career_stage,
    classify_job_level,
    job_level_allowed,
    ALLOWED_JOB_LEVELS,
)
from app.services.resume_capabilities import get_capabilities
from app.services.pdf_builder import generate_cover_letter_pdf
from firebase_admin import firestore

logger = logging.getLogger(__name__)

job_board_bp = Blueprint("job_board", __name__, url_prefix="/api/job-board")


# =============================================================================
# CONSTANTS
# =============================================================================

OPTIMIZATION_CREDIT_COST = 20
COVER_LETTER_CREDIT_COST = 5
RECRUITER_CREDIT_COST = 5  # per recruiter or hiring manager
CACHE_DURATION_HOURS = 6  # How long to cache job results

# Error Messages for Resume Optimization
ERROR_MESSAGES = {
    'url_parse_failed': 'Could not extract job details from that URL. Try pasting the job description directly.',
    'url_not_supported': 'That job board is not supported yet. Please paste the job description instead.',
    'resume_not_found': 'No resume found. Please upload your resume in Account Settings first.',
    'resume_incomplete': 'Your resume is missing required sections. Please update your profile.',
    'ai_timeout': 'The optimization is taking longer than expected. Your credits have been refunded. Please try again.',
    'ai_rate_limit': 'Our AI service is busy. Your credits have been refunded. Please try again in a few minutes.',
    'ai_error': 'Something went wrong with the optimization. Your credits have been refunded.',
    'invalid_job_description': 'The job description is too short or invalid. Please provide more details.',
    'credits_insufficient': 'Not enough credits. You need 20 credits to optimize your resume.',
    'database_error': 'Database error occurred. Please try again.',
    'json_parse_error': 'Error processing the optimization results. Please try again.',
}

def get_status_code(error_code: str) -> int:
    """Get HTTP status code for error code."""
    if error_code == 'credits_insufficient':
        return 402
    if error_code in ['resume_not_found', 'resume_incomplete', 'url_parse_failed', 'url_not_supported', 'invalid_job_description']:
        return 400
    if error_code in ['ai_timeout', 'ai_rate_limit']:
        return 503
    return 500

# Job Quality Configuration
MAX_JOB_AGE_DAYS = int(os.getenv('MAX_JOB_AGE_DAYS', 30))  # Filter out jobs older than this
MIN_QUALITY_SCORE = int(os.getenv('MIN_QUALITY_SCORE', 15))  # Minimum quality score threshold

# SerpAPI Configuration
SERPAPI_KEY = os.getenv("SERPAPI_KEY") or os.getenv("SERP_API_KEY")


# =============================================================================
# USER PROFILE CACHING (5-minute TTL)
# =============================================================================

from cachetools import TTLCache
_user_profile_cache = TTLCache(maxsize=500, ttl=300)  # 500 users max, 5-minute TTL

def _get_cached_user_profile(uid: str) -> Optional[dict]:
    """Get cached user profile if not expired."""
    profile = _user_profile_cache.get(uid)
    if profile is not None:
        logger.info(f"[JobBoard]  Using cached user profile for {uid[:8]}...")
        return profile
    return None

def _set_cached_user_profile(uid: str, profile: dict):
    """Cache user profile with current timestamp."""
    _user_profile_cache[uid] = profile
    logger.info(f"[JobBoard]  Cached user profile for {uid[:8]}... (TTL: 300s, cache_size={len(_user_profile_cache)}/{_user_profile_cache.maxsize})")

def _clear_user_profile_cache(uid: Optional[str] = None):
    """Clear user profile cache. If uid is None, clear all."""
    if uid:
        if uid in _user_profile_cache:
            del _user_profile_cache[uid]
            logger.info(f"[JobBoard]  Cleared cache for {uid[:8]}...")
    else:
        _user_profile_cache.clear()
        logger.info(f"[JobBoard]  Cleared all user profile cache")


# =============================================================================
# FIRESTORE CACHING
# =============================================================================

def get_cache_key(query: str, location: str, job_type: Optional[str] = None, page_token: Optional[str] = None) -> str:
    """
    Generate a unique cache key for a job search query.
    Includes page_token for pagination support.
    """
    cache_string = f"{query.lower().strip()}|{location.lower().strip()}|{job_type or 'all'}|{page_token or 'first'}"
    return hashlib.md5(cache_string.encode()).hexdigest()


def get_cached_jobs(cache_key: str, user_id: Optional[str] = None) -> Optional[List[Dict[str, Any]]]:
    """
    PHASE 5: Retrieve cached job results from Firestore if not expired.
    Now checks for cache invalidation markers for the user.
    """
    try:
        db = get_db()
        if not db:
            return None
        
        # PHASE 5: Check for cache invalidation for this user
        if user_id:
            invalidation_ref = db.collection("job_cache_invalidations").document(user_id)
            invalidation_doc = invalidation_ref.get()
            if invalidation_doc.exists:
                invalidation_data = invalidation_doc.to_dict()
                invalidated_at = invalidation_data.get("invalidated_at")
                
                if invalidated_at:
                    # Check if cache was created before invalidation
                    cache_ref = db.collection("job_cache").document(cache_key)
                    cache_doc = cache_ref.get()
                    
                    if cache_doc.exists:
                        cache_data = cache_doc.to_dict()
                        cached_at = cache_data.get("cached_at")
                        
                        if cached_at:
                            # Convert timestamps for comparison
                            if hasattr(invalidated_at, 'timestamp'):
                                invalidation_time = datetime.fromtimestamp(invalidated_at.timestamp(), tz=timezone.utc).replace(tzinfo=None)
                            else:
                                invalidation_time = invalidated_at
                            
                            if hasattr(cached_at, 'timestamp'):
                                cache_time = datetime.fromtimestamp(cached_at.timestamp(), tz=timezone.utc).replace(tzinfo=None)
                            else:
                                cache_time = cached_at
                            
                            # If cache is older than invalidation, reject it
                            if cache_time < invalidation_time:
                                logger.info(f"[JobBoard][INVALIDATE] user={user_id[:8] if user_id else 'unknown'}... cache_key={cache_key[:8]}... invalidated (cache_time={cache_time}, invalidation_time={invalidation_time})")
                                return None
            
        cache_ref = db.collection("job_cache").document(cache_key)
        cache_doc = cache_ref.get()
        
        if not cache_doc.exists:
            logger.info(f"[JobBoard Cache] Miss - no cache for key {cache_key[:8]}...")
            return None
        
        cache_data = cache_doc.to_dict()
        cached_at = cache_data.get("cached_at")
        
        if not cached_at:
            return None
        
        # Convert Firestore timestamp to datetime, handling timezone
        if hasattr(cached_at, 'timestamp'):
            # Firestore Timestamp object
            cache_time = datetime.fromtimestamp(cached_at.timestamp(), tz=timezone.utc).replace(tzinfo=None)
        elif hasattr(cached_at, 'tzinfo') and cached_at.tzinfo is not None:
            # Timezone-aware datetime
            cache_time = cached_at.replace(tzinfo=None)
        else:
            # Already naive datetime
            cache_time = cached_at
        
        expiry_time = cache_time + timedelta(hours=CACHE_DURATION_HOURS)
        now = datetime.utcnow()
        
        if now > expiry_time:
            logger.info(f"[JobBoard Cache] Expired - key {cache_key[:8]}...")
            return None
        
        jobs = cache_data.get("jobs", [])
        logger.info(f"[JobBoard Cache] Hit - returning {len(jobs)} cached jobs")
        return jobs
        
    except Exception as e:
        logger.error(f"[JobBoard Cache] Error reading cache: {e}")
        return None


def get_cached_jobs_with_stale(cache_key: str, user_id: Optional[str] = None) -> tuple[Optional[List[Dict[str, Any]]], bool]:
    """
    Like get_cached_jobs, but returns stale data if cache is expired.
    Returns (jobs, is_stale) — is_stale=True means data is expired but usable.
    """
    try:
        db = get_db()
        if not db:
            return None, False

        # Check for cache invalidation for this user (invalidated = fully stale, don't return)
        if user_id:
            invalidation_ref = db.collection("job_cache_invalidations").document(user_id)
            invalidation_doc = invalidation_ref.get()
            if invalidation_doc.exists:
                invalidation_data = invalidation_doc.to_dict()
                invalidated_at = invalidation_data.get("invalidated_at")
                if invalidated_at:
                    cache_ref = db.collection("job_cache").document(cache_key)
                    cache_doc = cache_ref.get()
                    if cache_doc.exists:
                        cache_data = cache_doc.to_dict()
                        cached_at = cache_data.get("cached_at")
                        if cached_at:
                            if hasattr(invalidated_at, 'timestamp'):
                                invalidation_time = datetime.fromtimestamp(invalidated_at.timestamp(), tz=timezone.utc).replace(tzinfo=None)
                            else:
                                invalidation_time = invalidated_at
                            if hasattr(cached_at, 'timestamp'):
                                cache_time = datetime.fromtimestamp(cached_at.timestamp(), tz=timezone.utc).replace(tzinfo=None)
                            else:
                                cache_time = cached_at
                            if cache_time < invalidation_time:
                                # User preferences changed — stale data won't be relevant
                                logger.info(f"[JobBoard][INVALIDATE] Cache invalidated by preference change, not returning stale data")
                                return None, False

        cache_ref = db.collection("job_cache").document(cache_key)
        cache_doc = cache_ref.get()

        if not cache_doc.exists:
            return None, False

        cache_data = cache_doc.to_dict()
        cached_at = cache_data.get("cached_at")
        if not cached_at:
            return None, False

        if hasattr(cached_at, 'timestamp'):
            cache_time = datetime.fromtimestamp(cached_at.timestamp(), tz=timezone.utc).replace(tzinfo=None)
        elif hasattr(cached_at, 'tzinfo') and cached_at.tzinfo is not None:
            cache_time = cached_at.replace(tzinfo=None)
        else:
            cache_time = cached_at

        expiry_time = cache_time + timedelta(hours=CACHE_DURATION_HOURS)
        now = datetime.utcnow()

        jobs = cache_data.get("jobs", [])
        is_stale = now > expiry_time

        if is_stale:
            logger.info(f"[JobBoard Cache] Stale hit - returning {len(jobs)} stale cached jobs (expired {(now - expiry_time).total_seconds():.0f}s ago)")
        else:
            logger.info(f"[JobBoard Cache] Fresh hit - returning {len(jobs)} cached jobs")

        return jobs, is_stale

    except Exception as e:
        logger.error(f"[JobBoard Cache] Error reading cache: {e}")
        return None, False


def set_cached_jobs(
    cache_key: str, 
    jobs_data: Any,  # Can be List[Dict] or tuple of (List[Dict], next_token)
    query: str, 
    location: str,
    job_type: Optional[str] = None
) -> None:
    """
    Store job results in Firestore cache.
    Accepts either a list of jobs (legacy) or tuple of (jobs, next_token).
    """
    try:
        db = get_db()
        if not db:
            return
        
        # Handle both tuple format (new) and list format (legacy)
        if isinstance(jobs_data, tuple) and len(jobs_data) == 2:
            jobs, next_token = jobs_data
        else:
            jobs = jobs_data if isinstance(jobs_data, list) else []
            next_token = None
            
        cache_ref = db.collection("job_cache").document(cache_key)
        cache_data = {
            "jobs": jobs,
            "query": query,
            "location": location,
            "job_type": job_type,
            "cached_at": datetime.utcnow(),
            "expires_at": datetime.utcnow() + timedelta(hours=CACHE_DURATION_HOURS),
            "result_count": len(jobs),
        }
        
        if next_token:
            cache_data["next_page_token"] = next_token
            
        cache_ref.set(cache_data)
        logger.info(f"[JobBoard Cache] Stored {len(jobs)} jobs for key {cache_key[:8]}...")
        
    except Exception as e:
        logger.error(f"[JobBoard Cache] Error writing cache: {e}")


def clear_expired_cache() -> int:
    """
    Clean up expired cache entries. Call periodically or via cron.
    Returns number of deleted entries.
    """
    try:
        db = get_db()
        if not db:
            return 0
            
        expired_docs = db.collection("job_cache").where(
            "expires_at", "<", datetime.utcnow()
        ).limit(100).get()
        
        deleted = 0
        for doc in expired_docs:
            doc.reference.delete()
            deleted += 1
        
        if deleted > 0:
            logger.info(f"[JobBoard Cache] Cleaned up {deleted} expired entries")
        
        return deleted
        
    except Exception as e:
        logger.error(f"[JobBoard Cache] Error clearing expired cache: {e}")
        return 0


# =============================================================================
# BACKGROUND CACHE REFRESH
# =============================================================================

_refresh_in_progress: Dict[str, bool] = {}
_refresh_lock = threading.Lock()
_refresh_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="jb-refresh")


def _trigger_background_refresh(cache_key: str, query: str, location: str, job_type: Optional[str], user_id: Optional[str]):
    """Trigger a background refresh for a stale cache entry."""
    with _refresh_lock:
        if _refresh_in_progress.get(cache_key):
            logger.info(f"[JobBoard Cache] Background refresh already in progress for {cache_key[:8]}...")
            return
        _refresh_in_progress[cache_key] = True

    def _do_refresh():
        try:
            logger.info(f"[JobBoard Cache] Background refresh started for {cache_key[:8]}...")
            # Fetch fresh data (bypass cache)
            jobs, next_token = fetch_jobs_from_serpapi(
                query=query,
                location=location,
                job_type=job_type,
                num_results=10,
                use_cache=False,
                page_token=None,
                user_id=user_id,
            )
            if jobs:
                set_cached_jobs(cache_key, (jobs, next_token), query, location, job_type)
                logger.info(f"[JobBoard Cache] Background refresh complete: {len(jobs)} fresh jobs cached for {cache_key[:8]}...")
            else:
                logger.warning(f"[JobBoard Cache] Background refresh returned no jobs for {cache_key[:8]}...")
        except Exception as e:
            logger.error(f"[JobBoard Cache] Background refresh failed for {cache_key[:8]}...: {e}")
        finally:
            with _refresh_lock:
                _refresh_in_progress.pop(cache_key, None)

    _refresh_pool.submit(_do_refresh)


# =============================================================================
# SERPAPI GOOGLE JOBS INTEGRATION
# =============================================================================

def get_best_job_link(job: Dict[str, Any]) -> str:
    """
    Extract the best direct job link from SerpAPI job data.
    
    Priority order:
    1. LinkedIn link (if available) - look for "linkedin.com" in the URL
    2. First apply option (company's direct job posting link)
    3. Fallback to related_links or share_link
    
    Args:
        job: Raw job data from SerpAPI
        
    Returns:
        Best available job link URL
    """
    apply_options = job.get("apply_options", [])
    
    # First priority: Look for LinkedIn link in apply_options
    if apply_options:
        for option in apply_options:
            link = option.get("link", "")
            if link and "linkedin.com" in link.lower():
                logger.info(f"[JobBoard] Using LinkedIn link: {link[:80]}...")
                return link
    
    # Second priority: Use first apply option (company's direct job posting)
    if apply_options and len(apply_options) > 0:
        direct_link = apply_options[0].get("link", "")
        if direct_link:
            logger.info(f"[JobBoard] Using direct company link: {direct_link[:80]}...")
            return direct_link
    
    # Fallback: Try related_links
    related_links = job.get("related_links", [])
    if related_links:
        fallback_link = related_links[0].get("link", "")
        if fallback_link:
            logger.info(f"[JobBoard] Using related link: {fallback_link[:80]}...")
            return fallback_link
    
    # Last resort: share_link or empty
    share_link = job.get("share_link", "")
    if share_link:
        logger.info(f"[JobBoard] Using share link: {share_link[:80]}...")
        return share_link
    
    logger.warning(f"[JobBoard] WARNING: No valid job link found for job: {job.get('title', 'Unknown')}")
    return ""


def fetch_jobs(query, location, num_results=10, user_id=None):
    """Job search — Perplexity primary, SerpAPI fallback during transition."""
    # PRIMARY: Perplexity
    try:
        from app.services.perplexity_client import search_jobs_live
        from app.services.enrichment_cache import get_cached, set_cached

        cache_key = [query, location, str(num_results)]
        cached = get_cached("job_search", cache_key)
        if cached:
            return cached if isinstance(cached, list) else cached.get("jobs", []), cached.get("next_token")

        jobs = search_jobs_live(query, location, limit=num_results,
                                domain_filter=["linkedin.com", "greenhouse.io",
                                               "lever.co", "workday.com", "indeed.com"])
        if jobs:
            set_cached("job_search", cache_key, {"jobs": jobs})
            return jobs, None
    except Exception:
        logger.warning("Perplexity job search failed, falling back to SerpAPI", exc_info=True)

    # FALLBACK: SerpAPI (DEPRECATED: remove in Phase 8)
    return fetch_jobs_from_serpapi(query, location, num_results, user_id)


def fetch_jobs_from_serpapi(
    query: str,
    location: str = "United States",
    job_type: Optional[str] = None,
    num_results: int = 20,
    use_cache: bool = True,
    page_token: Optional[str] = None,  # Can be next_page_token string or "start=10" format
    user_id: Optional[str] = None,  # PHASE 5: For cache invalidation checking
) -> tuple[List[Dict[str, Any]], Optional[str]]:
    """
    DEPRECATED: remove in Phase 8.
    Fetch job listings from SerpAPI Google Jobs with Firestore caching.
    Uses next_page_token for pagination (start parameter is deprecated).
    
    Args:
        query: Job search query (e.g., "Software Engineer Intern")
        location: Location to search (e.g., "San Francisco, CA")
        job_type: Optional filter - "Internship", "Full-Time", "Part-Time", "Contract"
        num_results: Number of results to fetch (max 10 per request for Google Jobs)
        use_cache: Whether to use Firestore cache
        page_token: Next page token from previous response (None for first page)
    
    Returns:
        Tuple of (list of job dictionaries, next_page_token for pagination)
    """
    # Check cache first (but only for single-page requests to avoid stale pagination)
    # Uses stale-while-revalidate: return stale data immediately, refresh in background
    if use_cache and page_token is None:  # Only cache first page to avoid pagination issues
        cache_key = get_cache_key(query, location, job_type, page_token)
        # PHASE 5: Pass user_id for cache invalidation checking
        cached_data, is_stale = get_cached_jobs_with_stale(cache_key, user_id=user_id)
        if cached_data is not None:
            # Cache stores tuple of (jobs, next_token) or legacy list format
            if isinstance(cached_data, tuple) and len(cached_data) == 2:
                cached_jobs, cached_token = cached_data
                logger.info(f"[JobBoard Cache] Hit - returning {len(cached_jobs)} cached jobs (stale={is_stale}), next_token={'Yes' if cached_token else 'No'}")
                if is_stale:
                    _trigger_background_refresh(cache_key, query, location, job_type, user_id)
                return cached_jobs, cached_token
            # Legacy cache format (just jobs list) - if we only have 10 jobs and no next_token,
            # it might be incomplete, so bypass cache to get fresh data with pagination tokens
            elif isinstance(cached_data, list):
                if len(cached_data) <= 10 and not is_stale:
                    # Legacy cache with 10 or fewer jobs likely doesn't have next_token info
                    # Bypass cache to get fresh data with proper pagination
                    logger.info(f"[JobBoard Cache] Legacy cache with {len(cached_data)} jobs (likely incomplete), bypassing cache to fetch fresh data")
                    use_cache = False  # Fall through to fetch fresh data
                else:
                    # If we have more than 10 jobs in cache, or data is stale (return what we have)
                    logger.info(f"[JobBoard Cache] Hit - returning {len(cached_data)} cached jobs (stale={is_stale}, legacy format)")
                    if is_stale:
                        _trigger_background_refresh(cache_key, query, location, job_type, user_id)
                    return cached_data, None
    
    if not SERPAPI_KEY:
        logger.warning("[JobBoard] WARNING: SERPAPI_KEY not found, using mock data")
        return [], None
    
    try:
        url = "https://serpapi.com/search"
        
        # Build query with job type included (chips parameter causes 400 errors)
        search_query = query
        if job_type:
            if job_type == "Internship" and "internship" not in query.lower():
                search_query = f"{query} internship"
            elif job_type == "Full-Time" and "full time" not in query.lower() and "full-time" not in query.lower():
                search_query = f"{query} full time"
            elif job_type == "Part-Time" and "part time" not in query.lower() and "part-time" not in query.lower():
                search_query = f"{query} part time"
            elif job_type == "Contract" and "contract" not in query.lower():
                search_query = f"{query} contract"
        
        params = {
            "engine": "google_jobs",
            "q": search_query,
            "location": location,
            "api_key": SERPAPI_KEY,
            "hl": "en",
            "num": min(num_results, 10),  # SerpAPI Google Jobs typically returns 10 results per page
        }
        
        # Use next_page_token for pagination if available
        # Fallback to 'start' parameter if next_page_token is not available
        if page_token:
            if isinstance(page_token, str) and page_token.startswith("start="):
                # Using start parameter for pagination (fallback method)
                start_offset = int(page_token.replace("start=", ""))
                params["start"] = start_offset
                logger.info(f"[JobBoard] Using start parameter for pagination: start={start_offset}")
            else:
                # Using next_page_token (preferred method)
                params["next_page_token"] = page_token
        
        logger.info(f"[JobBoard] Fetching jobs from SerpAPI: {search_query} in {location}")
        logger.info(f"[JobBoard] Request params (masked): engine={params['engine']}, q={params['q']}, location={params['location']}, num={params['num']}, page_token={'***' if page_token else 'None'}")
        
        response = requests.get(url, params=params, timeout=30)
        
        # Check for HTTP errors
        if response.status_code != 200:
            logger.error(f"[JobBoard] SerpAPI HTTP error {response.status_code}: {response.text[:500]}")
            return [], None
        
        try:
            data = response.json()
        except ValueError as e:
            logger.error(f"[JobBoard] SerpAPI JSON parse error: {e}, response: {response.text[:500]}")
            return [], None
        
        if "error" in data:
            error_msg = data.get('error', 'Unknown error')
            # Check if this is a "no more results" error (expected when pagination exhausted)
            if isinstance(error_msg, str):
                if "hasn't returned any results" in error_msg.lower() or "no results" in error_msg.lower():
                    # This is expected when there are no more results for pagination
                    logger.error(f"[JobBoard] No more results available for pagination (expected): {error_msg}")
                    return [], None  # Return empty to stop pagination gracefully
                elif "invalid" in error_msg.lower() and "token" in error_msg.lower():
                    # Invalid token - might be expired or malformed
                    logger.error(f"[JobBoard] Invalid pagination token (may be expired): {error_msg}")
                    return [], None
            # For other errors, log as actual errors
            logger.error(f"[JobBoard] SerpAPI API error: {error_msg}")
            if isinstance(data["error"], dict):
                logger.error(f"[JobBoard] Error details: {data['error']}")
            return [], None
        
        jobs_results = data.get("jobs_results", [])
        
        # Debug: Log response structure to understand pagination
        logger.info(f"[JobBoard] Response keys: {list(data.keys())}")
        logger.info(f"[JobBoard] Found {len(jobs_results)} jobs from SerpAPI")
        
        # Extract next_page_token from pagination object
        pagination = data.get("pagination", {})
        if pagination:
            logger.info(f"[JobBoard] Pagination object keys: {list(pagination.keys())}")
        next_page_token = pagination.get("next_page_token") if pagination else None
        
        # Also check for pagination at root level
        if not next_page_token:
            next_page_token = data.get("next_page_token")
        
        # Check for serpapi_pagination (alternative format - this contains the full URL or token)
        serpapi_pagination = data.get("serpapi_pagination", {})
        if serpapi_pagination and not next_page_token:
            next_page_value = serpapi_pagination.get("next")
            logger.info(f"[JobBoard] Found next value in serpapi_pagination: {next_page_value[:100] if next_page_value else 'None'}...")
            if next_page_value:
                # The value might be a full URL or just a token
                # If it's a URL, extract the token from the query string
                if isinstance(next_page_value, str) and "next_page_token=" in next_page_value:
                    # Extract token from URL
                    try:
                        parsed = urlparse(next_page_value)
                        params = parse_qs(parsed.query)
                        if "next_page_token" in params:
                            next_page_token = params["next_page_token"][0]
                            logger.info(f"[JobBoard] Extracted token from URL: {next_page_token[:50]}...")
                    except Exception as e:
                        logger.error(f"[JobBoard] Error extracting token from URL: {e}")
                        # Fallback: try to extract manually
                        if "next_page_token=" in next_page_value:
                            parts = next_page_value.split("next_page_token=")
                            if len(parts) > 1:
                                token_part = parts[1].split("&")[0]
                                next_page_token = token_part
                                logger.info(f"[JobBoard] Extracted token manually: {next_page_token[:50]}...")
                elif isinstance(next_page_value, str):
                    # It's already a token
                    next_page_token = next_page_value
                    logger.info(f"[JobBoard] Using next_page_value as token directly")
        
        if next_page_token:
            logger.info(f"[JobBoard] Next page token available for pagination: {next_page_token[:50]}...")
        else:
            logger.info(f"[JobBoard] No next_page_token found in response. Checking if we can use 'start' parameter for pagination...")
        
        # Transform SerpAPI format to our format
        jobs = []
        for idx, job in enumerate(jobs_results):
            # Extract salary if available
            salary = None
            if job.get("detected_extensions", {}).get("salary"):
                salary = job["detected_extensions"]["salary"]
            elif job.get("extensions"):
                for ext in job["extensions"]:
                    if "$" in ext or "hour" in ext.lower() or "year" in ext.lower():
                        salary = ext
                        break
            
            # Determine job type
            detected_type = "Full-Time"
            extensions = job.get("extensions", [])
            extensions_str = " ".join(extensions).lower()
            if "intern" in extensions_str or "internship" in job.get("title", "").lower():
                detected_type = "Internship"
            elif "part-time" in extensions_str or "part time" in extensions_str:
                detected_type = "Part-Time"
            elif "contract" in extensions_str or "contractor" in extensions_str:
                detected_type = "Contract"
            
            # Check if remote
            is_remote = "remote" in extensions_str or "work from home" in extensions_str
            
            # Get posted time
            posted = "Recently"
            detected_extensions = job.get("detected_extensions", {})
            if detected_extensions.get("posted_at"):
                posted = detected_extensions["posted_at"]
            elif extensions:
                for ext in extensions:
                    if "ago" in ext.lower() or "day" in ext.lower() or "hour" in ext.lower():
                        posted = ext
                        break
            
            # Extract requirements from description highlights
            requirements = []
            highlights = job.get("job_highlights", [])
            for highlight in highlights:
                if highlight.get("title", "").lower() in ["qualifications", "requirements", "what you need"]:
                    requirements = highlight.get("items", [])[:5]
                    break
            
            # Get best apply link using priority: LinkedIn > Direct company link > Fallback
            apply_link = get_best_job_link(job)
            
            # Build job object
            transformed_job = {
                "id": job.get("job_id", f"serp_{idx}_{hash(job.get('title', ''))}"),
                "title": job.get("title", "Unknown Title"),
                "company": job.get("company_name", "Unknown Company"),
                "location": job.get("location", location),
                "salary": salary,
                "type": detected_type,
                "posted": posted,
                "description": job.get("description", "")[:2000],  # Limit description size for cache
                "requirements": requirements,
                "url": apply_link,
                "logo": job.get("thumbnail"),
                "remote": is_remote,
                "experienceLevel": classify_job_level(job.get("title", ""), job.get("description", "")),
                "via": job.get("via", ""),
            }
            
            jobs.append(transformed_job)
        
        # Cache the results with next_page_token
        if use_cache and jobs:
            cache_key = get_cache_key(query, location, job_type, page_token)
            # Store tuple of (jobs, next_token) in cache
            set_cached_jobs(cache_key, (jobs, next_page_token), query, location, job_type)
        
        return jobs, next_page_token
        
    except requests.exceptions.RequestException as e:
        logger.error(f"[JobBoard] SerpAPI request error: {e}")
        return [], None
    except Exception as e:
        logger.error(f"[JobBoard] Error fetching jobs: {e}")
        return [], None


# =============================================================================
# ENHANCED RESUME DATA EXTRACTION & MAPPINGS (Parts 1-3)
# =============================================================================

def extract_user_profile_from_resume(user_data: dict) -> dict:
    """
    Extract user profile data from resumeParsed, handling both old and new formats.
    
    Old format (flat):
        resumeParsed.major, resumeParsed.skills (array), resumeParsed.university
    
    New format (nested):
        resumeParsed.education.major, resumeParsed.skills.programming_languages, etc.
    
    Returns a normalized profile dict.
    """
    resume_parsed = user_data.get('resumeParsed', {})
    professional_info = user_data.get('professionalInfo', {})
    
    # === EDUCATION ===
    # Try new format first (nested under education)
    education = resume_parsed.get('education', {}) if resume_parsed else {}
    major = (
        education.get('major') or 
        (resume_parsed.get('major', '') if resume_parsed else '') or
        professional_info.get('fieldOfStudy') or
        professional_info.get('major') or
        ''
    )
    university = (
        education.get('university') or 
        (resume_parsed.get('university', '') if resume_parsed else '') or
        professional_info.get('university') or
        ''
    )
    graduation = (
        education.get('graduation') or 
        (resume_parsed.get('year', '') if resume_parsed else '') or
        professional_info.get('graduationYear') or
        ''
    )
    coursework = education.get('coursework', [])
    
    # === SKILLS ===
    # Try new format first (nested object with categories)
    skills_data = resume_parsed.get('skills', {}) if resume_parsed else {}
    
    if isinstance(skills_data, dict):
        # New format: skills is an object with categories
        programming_languages = skills_data.get('programming_languages', [])
        tools_frameworks = skills_data.get('tools_frameworks', [])
        core_skills = skills_data.get('core_skills', [])
        databases = skills_data.get('databases', [])
        cloud_devops = skills_data.get('cloud_devops', [])
        soft_skills = skills_data.get('soft_skills', [])
        
        # Combine all skills into a flat list for matching
        all_skills = (
            programming_languages + 
            tools_frameworks + 
            core_skills + 
            databases + 
            cloud_devops
        )
    elif isinstance(skills_data, list):
        # Old format: skills is a flat array
        all_skills = [s for s in skills_data if isinstance(s, str)]
        programming_languages = all_skills
        tools_frameworks = []
        core_skills = []
    else:
        all_skills = []
        programming_languages = []
        tools_frameworks = []
        core_skills = []
    
    # === EXPERIENCE ===
    # Try new format first (array of objects with company, title, bullets)
    experience = resume_parsed.get('experience', []) if resume_parsed else []
    
    if not experience:
        # Fall back to old format
        key_experiences = resume_parsed.get('key_experiences', []) if resume_parsed else []
        experience = [
            {
                'title': exp if isinstance(exp, str) else exp.get('title', ''),
                'company': '' if isinstance(exp, str) else exp.get('company', ''),
                'bullets': [exp] if isinstance(exp, str) else exp.get('bullets', []),
                'keywords': [] if isinstance(exp, str) else exp.get('keywords', [])
            }
            for exp in key_experiences
        ]
    
    # Extract experience details for matching
    experience_titles = []
    experience_companies = []
    experience_bullets = []
    
    for exp in experience:
        if isinstance(exp, dict):
            if exp.get('title'):
                experience_titles.append(exp['title'])
            if exp.get('company'):
                experience_companies.append(exp['company'])
            if exp.get('bullets'):
                if isinstance(exp['bullets'], list):
                    experience_bullets.extend(exp['bullets'])
                else:
                    experience_bullets.append(str(exp['bullets']))
        elif isinstance(exp, str):
            # Old format: just strings
            experience_bullets.append(exp)
    
    # === PROJECTS ===
    projects = resume_parsed.get('projects', []) if resume_parsed else []
    project_names = [p.get('name', '') for p in projects if isinstance(p, dict)]
    project_descriptions = [p.get('description', '') for p in projects if isinstance(p, dict)]
    project_technologies = []
    for p in projects:
        if isinstance(p, dict) and p.get('technologies'):
            if isinstance(p['technologies'], list):
                project_technologies.extend(p['technologies'])
    
    # === EXTRACURRICULARS ===
    extracurriculars = resume_parsed.get('extracurriculars', []) if resume_parsed else []
    if not extracurriculars:
        # Try old format
        extracurriculars = resume_parsed.get('interests', []) if resume_parsed else []
    
    # Normalize extracurriculars to dict format
    normalized_extracurriculars = []
    for ec in extracurriculars:
        if isinstance(ec, dict):
            normalized_extracurriculars.append({
                'name': ec.get('name', ''),
                'role': ec.get('role', ''),
                'description': ec.get('description', '')
            })
        elif isinstance(ec, str):
            normalized_extracurriculars.append({
                'name': ec,
                'role': '',
                'description': ''
            })
    
    # === CONTACT ===
    contact = resume_parsed.get('contact', {}) if resume_parsed else {}
    location = contact.get('location') or resume_parsed.get('location', '') if resume_parsed else ''
    
    return {
        # Education
        'major': major,
        'university': university,
        'graduation': graduation,
        'coursework': coursework,
        
        # Skills (categorized)
        'all_skills': all_skills,
        'programming_languages': programming_languages,
        'tools_frameworks': tools_frameworks,
        'core_skills': core_skills,
        
        # Experience
        'experience': experience,
        'experience_titles': experience_titles,
        'experience_companies': experience_companies,
        'experience_bullets': experience_bullets,
        'experience_count': len(experience),
        
        # Projects
        'projects': projects,
        'project_names': project_names,
        'project_descriptions': project_descriptions,
        'project_technologies': project_technologies,
        'project_count': len(projects),
        
        # Other
        'extracurriculars': normalized_extracurriculars,
        'location': location,
        
        # For logging
        'has_major': bool(major),
        'skills_count': len(all_skills),
        'extracurriculars_count': len(normalized_extracurriculars),
    }


def get_user_career_profile(uid: str) -> dict:
    """
    Extract comprehensive career profile from user's Firestore data.
    Uses 5-minute cache to avoid repeated Firestore lookups.
    
    Returns:
        {
            "major": str,
            "minor": str | None,
            "skills": List[str],
            "extracurriculars": List[dict],  # {name, role, description, keywords}
            "experiences": List[dict],        # {title, company, keywords}
            "interests": List[str],
            "graduation_year": int | None,
            "gpa": float | None,
            "target_industries": List[str],
            "job_types": List[str]
        }
    """
    # Check cache first
    cached_profile = _get_cached_user_profile(uid)
    if cached_profile is not None:
        return cached_profile
    
    db = get_db()
    if not db:
        return {}
    
    try:
        user_ref = db.collection("users").document(uid)
        user_doc = user_ref.get()
        
        if not user_doc.exists:
            return {}
        
        user_data = user_doc.to_dict()
        
        # Sanitize data to handle DocumentReferences
        resume_parsed = user_data.get("resumeParsed", {})
        if resume_parsed:
            resume_parsed = sanitize_firestore_data(resume_parsed, depth=0, max_depth=10)
            user_data["resumeParsed"] = resume_parsed
        
        # Use helper function to extract profile from both old and new formats
        profile = extract_user_profile_from_resume(user_data)
        professional_info = user_data.get("professionalInfo", {})
        academics_data = user_data.get("academics", {})
        
        # Extract minor (only from professionalInfo, not in resumeParsed)
        minor = professional_info.get("minor") or None
        
        # Extract graduation year - PHASE 1 FIX: Handle "May 2026" format
        graduation_year = None
        year_str = profile.get('graduation') or professional_info.get("graduationYear") or academics_data.get("graduationYear")
        if year_str:
            try:
                year_str_clean = str(year_str).strip()
                # Try to extract 4-digit year (handles "2026", "May 2026", "2026-05", etc.)
                year_match = re.search(r'\b(19|20)\d{2}\b', year_str_clean)
                if year_match:
                    graduation_year = int(year_match.group())
                else:
                    # Fallback: try first 4 digits
                    graduation_year = int(year_str_clean[:4])
            except (ValueError, TypeError, AttributeError):
                pass
        
        # Extract GPA
        gpa = professional_info.get("gpa")
        if gpa:
            try:
                gpa = float(gpa)
            except (ValueError, TypeError):
                gpa = None
        
        # Extract target industries
        target_industries = professional_info.get("targetIndustries", [])
        if not target_industries:
            target_industries = user_data.get("targetIndustries", [])
        if not isinstance(target_industries, list):
            target_industries = []
        
        # Extract job types - PHASE 1 FIX: Read from location.jobTypes (where onboarding saves it)
        # Fallback to top-level jobTypes for backwards compatibility
        location_data = user_data.get("location", {})
        job_types = location_data.get("jobTypes", [])
        if not job_types:
            job_types = user_data.get("jobTypes", [])
        if not isinstance(job_types, list):
            job_types = []
        
        # Extract interests/careerInterests - PHASE 1 FIX: Read from location.interests or location.careerInterests
        # Fallback to professionalInfo.interests or top-level interests for backwards compatibility
        interests = location_data.get("interests", [])
        if not interests:
            interests = location_data.get("careerInterests", [])
        if not interests:
            interests = professional_info.get("interests", [])
        if not interests:
            interests = user_data.get("interests", [])
        if not interests:
            interests = user_data.get("careerInterests", [])
        if not isinstance(interests, list):
            interests = []
        
        # Extract preferredLocation - PHASE 1 FIX: Read from location.preferredLocation (currently never read)
        # PHASE 5: Also check top-level preferredLocation/preferredLocations for backwards compatibility
        preferred_location = location_data.get("preferredLocation", [])
        if not preferred_location:
            # Fallback to top-level fields (frontend may save here)
            preferred_location = user_data.get("preferredLocation", [])
        if not preferred_location:
            preferred_location = user_data.get("preferredLocations", [])
        if not isinstance(preferred_location, list):
            preferred_location = []
        
        # Extract graduationMonth - PHASE 1 FIX: Read from academics.graduationMonth (currently never read)
        graduation_month = academics_data.get("graduationMonth") or professional_info.get("graduationMonth")
        
        # Extract degree - PHASE 1 FIX: Read from academics.degree (currently never read)
        degree = academics_data.get("degree") or professional_info.get("degree")
        
        # Extract university - PHASE 1 FIX: Ensure we read from academics.university
        university = academics_data.get("university") or professional_info.get("university") or profile.get("university", "")
        
        # Check if resume is present - PHASE 1 FIX: Boolean flag for resume presence
        resume_present = bool(user_data.get("resumeParsed")) or bool(user_data.get("resumeUrl"))
        
        # PHASE 1: Log raw intent extraction for observability
        logger.info(f"[Intent] Raw profile extracted for user {uid[:8]}...: "
              f"preferredLocation={len(preferred_location) if preferred_location else 0} locations, "
              f"careerInterests={len(interests)} interests, "
              f"jobTypes={len(job_types)} types, "
              f"graduationYear={graduation_year}, "
              f"graduationMonth={graduation_month}, "
              f"degree={degree}, "
              f"university={university[:30] if university else 'None'}, "
              f"resume_present={resume_present}")
        
        # Log warnings for missing critical fields
        if not preferred_location:
            logger.warning(f"[Intent][WARN] Missing preferredLocation for user {uid[:8]}..., using default behavior")
        if not interests:
            logger.warning(f"[Intent][WARN] Missing careerInterests for user {uid[:8]}..., will fallback to major-based inference")
        if not job_types:
            logger.warning(f"[Intent][WARN] Missing jobTypes for user {uid[:8]}..., will use default based on graduation year")
        if not graduation_year:
            logger.warning(f"[Intent][WARN] Missing graduationYear for user {uid[:8]}..., will assume current year + 1")
        
        # Convert experience list to expected format. Dates are kept so
        # career-stage derivation can sum real years of experience.
        experiences = []
        for exp in profile.get('experience', [])[:10]:
            if isinstance(exp, dict):
                experiences.append({
                    "title": exp.get("title", ""),
                    "company": exp.get("company", ""),
                    "dates": exp.get("dates", ""),
                    "keywords": exp.get("keywords", [])
                })

        # Professional identity (multi-audience support): userType is set at
        # onboarding ('student' | 'professional'); yearsExperience is an
        # optional user-stated number that overrides resume-derived years.
        user_type = user_data.get("userType") or professional_info.get("userType") or ""
        years_experience_stated = professional_info.get("yearsExperience")
        if years_experience_stated is not None:
            try:
                years_experience_stated = float(years_experience_stated)
            except (ValueError, TypeError):
                years_experience_stated = None
        current_role = professional_info.get("currentRole") or ""
        current_company = professional_info.get("currentCompany") or ""
        
        result = {
            "major": profile.get("major", ""),
            "minor": minor,
            "skills": profile.get("all_skills", [])[:20],  # Limit to top 20 skills
            "extracurriculars": profile.get("extracurriculars", [])[:15],  # Limit to top 15
            "experiences": experiences,  # Already limited to top 10
            "interests": interests[:10],  # Limit to top 10
            "graduation_year": graduation_year,
            "graduation_month": graduation_month,  # PHASE 1: Added
            "degree": degree,  # PHASE 1: Added
            "university": university,  # PHASE 1: Added (was in profile but not in result)
            "gpa": gpa,
            "target_industries": target_industries,
            "job_types": job_types,
            "preferred_location": preferred_location,  # PHASE 1: Added (critical - was never read)
            "resume_present": resume_present,  # PHASE 1: Added
            "user_type": user_type,
            "years_experience_stated": years_experience_stated,
            "current_role": current_role,
            "current_company": current_company,
        }
        
        # Cache the result
        _set_cached_user_profile(uid, result)
        
        return result
        
    except Exception as e:
        logger.error(f"[JobBoard] Error extracting user career profile: {e}")
        import traceback
        traceback.print_exc()
        return {}


def normalize_intent(user_profile: dict) -> dict:
    """
    PHASE 1: Convert raw user profile data into a clean intent contract.
    
    Normalizes and returns:
    - career_domains: Canonical list derived from careerInterests
    - preferred_locations: Normalized city variants
    - job_types: Normalized casing and synonyms
    - graduation_timing: Computed graduation year, months until graduation, career phase
    - education_context: Degree, university
    - resume_present: Boolean flag
    
    Args:
        user_profile: Raw user profile from get_user_career_profile()
        
    Returns:
        Normalized intent contract dict with all fields safely defaulted
    """
    from datetime import datetime
    
    # Extract raw fields with safe defaults
    career_interests = user_profile.get("interests", []) or []
    preferred_location = user_profile.get("preferred_location", []) or []
    job_types = user_profile.get("job_types", []) or []
    graduation_year = user_profile.get("graduation_year")
    graduation_month = user_profile.get("graduation_month")
    degree = user_profile.get("degree")
    university = user_profile.get("university", "")
    resume_present = user_profile.get("resume_present", False)
    
    # === NORMALIZE CAREER DOMAINS ===
    # Map career interests to canonical domain categories
    career_domains = []
    domain_mapping = {
        # Finance domain
        "investment banking": "finance_banking",
        "private equity": "finance_banking",
        "hedge funds": "finance_banking",
        "asset management": "finance_banking",
        "wealth management": "finance_banking",
        "finance": "finance_banking",
        "banking": "finance_banking",
        "trading": "finance_banking",
        "equity research": "finance_banking",
        "fintech": "finance_banking",  # Adjacent domain
        
        # Technology domain
        "software engineering": "technology",
        "software development": "technology",
        "software engineer": "technology",
        "data science": "technology",
        "data scientist": "technology",
        "machine learning": "technology",
        "artificial intelligence": "technology",
        "cybersecurity": "technology",
        "product management": "technology",
        "technology": "technology",
        "tech": "technology",
        
        # Consulting domain
        "management consulting": "consulting",
        "strategy consulting": "consulting",
        "consulting": "consulting",
        "advisory": "consulting",
        
        # Other domains (add as needed)
        "marketing": "marketing",
        "sales": "sales",
        "operations": "operations",
        "healthcare": "healthcare",
        "education": "education",
    }
    
    for interest in career_interests:
        if not interest:
            continue
        interest_lower = str(interest).lower().strip()
        # Direct match
        if interest_lower in domain_mapping:
            domain = domain_mapping[interest_lower]
            if domain not in career_domains:
                career_domains.append(domain)
        else:
            # Partial match (e.g., "Investment Banking Analyst" contains "investment banking")
            for key, domain in domain_mapping.items():
                if key in interest_lower:
                    if domain not in career_domains:
                        career_domains.append(domain)
                    break
    
    # If no career interests, infer from major
    if not career_domains:
        major = user_profile.get("major", "").lower()
        if any(kw in major for kw in ["finance", "economics", "accounting", "business"]):
            career_domains = ["finance_banking"]
        elif any(kw in major for kw in ["computer", "software", "data", "information", "cs"]):
            career_domains = ["technology"]
        elif any(kw in major for kw in ["consulting", "strategy", "management"]):
            career_domains = ["consulting"]
        else:
            career_domains = []  # Unknown domain
    
    # === NORMALIZE PREFERRED LOCATIONS ===
    # Normalize common city variants to "City, State" format
    normalized_locations = []
    location_normalizations = {
        "new york": "New York, NY",
        "nyc": "New York, NY",
        "manhattan": "New York, NY",
        "brooklyn": "New York, NY",
        "queens": "New York, NY",
        "san francisco": "San Francisco, CA",
        "sf": "San Francisco, CA",
        "los angeles": "Los Angeles, CA",
        "la": "Los Angeles, CA",
        "chicago": "Chicago, IL",
        "boston": "Boston, MA",
        "seattle": "Seattle, WA",
        "austin": "Austin, TX",
        "washington": "Washington, DC",
        "dc": "Washington, DC",
        "remote": "Remote",  # Special case - keep as-is
    }
    
    for loc in preferred_location:
        if not loc:
            continue
        loc_str = str(loc).strip()
        loc_lower = loc_str.lower()
        
        # Check if already in normalized format (contains comma and state)
        if "," in loc_str and len(loc_str.split(",")) == 2:
            normalized_locations.append(loc_str)
        elif loc_lower in location_normalizations:
            normalized_locations.append(location_normalizations[loc_lower])
        else:
            # Try partial match
            normalized = None
            for key, normalized_loc in location_normalizations.items():
                if key in loc_lower or loc_lower in key:
                    normalized = normalized_loc
                    break
            if normalized:
                normalized_locations.append(normalized)
            else:
                # Keep original if can't normalize (user may have entered custom location)
                normalized_locations.append(loc_str)
    
    # Deduplicate
    normalized_locations = list(dict.fromkeys(normalized_locations))
    
    # === NORMALIZE JOB TYPES ===
    # Normalize casing and synonyms
    normalized_job_types = []
    job_type_mapping = {
        "internship": "internship",
        "intern": "internship",
        "summer analyst": "internship",
        "co-op": "internship",
        "coop": "internship",
        "full-time": "full-time",
        "fulltime": "full-time",
        "full time": "full-time",
        "entry level": "full-time",
        "new grad": "full-time",
        "part-time": "part-time",
        "parttime": "part-time",
        "part time": "part-time",
    }
    
    for jt in job_types:
        if not jt:
            continue
        jt_lower = str(jt).lower().strip()
        normalized = job_type_mapping.get(jt_lower, jt_lower)
        if normalized not in normalized_job_types:
            normalized_job_types.append(normalized)
    
    # === COMPUTE GRADUATION TIMING ===
    current_year = datetime.now().year
    current_month = datetime.now().month
    
    graduation_year_int = None
    if graduation_year:
        try:
            graduation_year_int = int(str(graduation_year).strip()[:4])
        except (ValueError, TypeError):
            pass
    
    # Compute months until graduation
    months_until_graduation = None
    if graduation_year_int and graduation_month:
        # Map month name to number
        month_map = {
            "january": 1, "february": 2, "march": 3, "april": 4,
            "may": 5, "june": 6, "july": 7, "august": 8,
            "september": 9, "october": 10, "november": 11, "december": 12
        }
        month_str = str(graduation_month).lower().strip()
        graduation_month_num = month_map.get(month_str, 5)  # Default to May
        
        # Calculate months until graduation
        graduation_date = datetime(graduation_year_int, graduation_month_num, 1)
        current_date = datetime(current_year, current_month, 1)
        if graduation_date > current_date:
            months_until_graduation = (graduation_year_int - current_year) * 12 + (graduation_month_num - current_month)
        else:
            months_until_graduation = 0  # Already graduated
    elif graduation_year_int:
        # No month specified, assume May (standard graduation)
        graduation_date = datetime(graduation_year_int, 5, 1)
        current_date = datetime(current_year, current_month, 1)
        if graduation_date > current_date:
            months_until_graduation = (graduation_year_int - current_year) * 12 + (5 - current_month)
        else:
            months_until_graduation = 0
    
    # Determine career phase from every available signal: dated resume
    # experience, stated years of experience, graduation timing, and the
    # onboarding userType. Students keep internship/new_grad; professionals
    # land in early_career/mid_level/senior/executive. See career_stage.py.
    stage_info = derive_career_stage(
        graduation_year=graduation_year_int,
        months_until_graduation=months_until_graduation,
        experiences=user_profile.get("experiences", []),
        user_type=user_profile.get("user_type"),
        years_experience_override=user_profile.get("years_experience_stated"),
    )
    career_phase = stage_info["stage"]
    
    # === BUILD INTENT CONTRACT ===
    intent_contract = {
        "career_domains": career_domains,
        "preferred_locations": normalized_locations,
        "job_types": normalized_job_types,
        "graduation_timing": {
            "graduation_year": graduation_year_int,
            "graduation_month": graduation_month,
            "months_until_graduation": months_until_graduation,
            "career_phase": career_phase,
            "years_experience": stage_info.get("years_experience"),
            "is_student": stage_info.get("is_student", True),
            "stage_source": stage_info.get("source"),
        },
        "education_context": {
            "degree": degree,
            "university": university,
        },
        "resume_present": resume_present,
    }
    
    # PHASE 1: Log normalized intent
    logger.info(f"[Intent] Normalized intent for user: "
          f"career_domains={career_domains}, "
          f"preferred_locations={normalized_locations}, "
          f"job_types={normalized_job_types}, "
          f"career_phase={career_phase}, "
          f"graduation_year={graduation_year_int}, "
          f"months_until_grad={months_until_graduation}, "
          f"resume_present={resume_present}")
    
    return intent_contract


# Extracurricular to career signal mapping
EC_CAREER_SIGNALS = {
    # Technical/Engineering
    "robotics": ["hardware engineer", "mechanical engineer", "automation", "embedded systems"],
    "coding club": ["software engineer", "developer", "programmer"],
    "hackathon": ["software engineer", "startup", "product", "innovation"],
    "data science": ["data analyst", "data scientist", "machine learning", "analytics"],
    "cybersecurity": ["security engineer", "security analyst", "InfoSec"],
    "ai club": ["machine learning engineer", "AI researcher", "data scientist"],
    "engineering": ["engineer", "technical", "R&D"],
    
    # Business/Finance
    "finance club": ["investment banking", "private equity", "financial analyst", "trader"],
    "consulting club": ["consultant", "strategy", "management consulting", "advisory"],
    "entrepreneurship": ["startup", "founder", "business development", "venture"],
    "investment club": ["asset management", "portfolio", "equity research"],
    "business": ["analyst", "operations", "strategy", "business development"],
    "economics": ["economist", "policy analyst", "research analyst"],
    
    # Leadership/Management
    "student government": ["operations", "program manager", "policy", "administration"],
    "president": ["leadership", "management", "executive"],
    "captain": ["team lead", "management", "leadership"],
    "founder": ["entrepreneur", "startup", "founder"],
    "board member": ["governance", "strategy", "leadership"],
    
    # Communications/Creative
    "debate": ["law", "consulting", "policy", "communications", "public affairs"],
    "journalism": ["content", "communications", "media", "editor", "writer"],
    "marketing club": ["marketing", "brand", "digital marketing", "social media"],
    "public speaking": ["communications", "sales", "client-facing"],
    "theater": ["communications", "creative", "public relations"],
    "film": ["video production", "content creator", "media"],
    
    # Research/Academic
    "research": ["research assistant", "R&D", "scientist", "lab", "academia"],
    "lab": ["research", "scientist", "lab technician", "R&D"],
    "honors society": ["research", "academic", "graduate school"],
    "teaching assistant": ["education", "training", "curriculum"],
    "tutoring": ["education", "teaching", "mentoring"],
    
    # Social Impact
    "volunteer": ["nonprofit", "social impact", "community", "NGO"],
    "community service": ["nonprofit", "community outreach", "social work"],
    "sustainability": ["environmental", "ESG", "green energy", "climate"],
    "social entrepreneurship": ["social impact", "nonprofit", "mission-driven"],
    
    # Healthcare/Science
    "pre-med": ["healthcare", "medical", "clinical", "hospital"],
    "biology club": ["biotech", "pharmaceutical", "life sciences", "research"],
    "chemistry": ["pharmaceutical", "chemical engineer", "lab", "R&D"],
    "health": ["healthcare", "wellness", "public health", "clinical"],
    
    # Sports/Athletics
    "athlete": ["teamwork", "discipline", "competitive", "performance"],
    "sports": ["athletics", "coaching", "sports management", "fitness"],
    "intramural": ["teamwork", "recreation", "wellness"],
    
    # Arts/Design
    "design": ["UI/UX", "graphic design", "product design", "creative"],
    "art": ["creative", "design", "visual", "artistic"],
    "music": ["creative", "entertainment", "production"],
    "photography": ["visual", "content", "creative", "media"],
}

LEADERSHIP_ROLES = [
    "president", "vice president", "vp", "captain", "lead", "leader",
    "founder", "co-founder", "director", "head", "chief", "chair",
    "chairman", "coordinator", "manager", "officer", "executive"
]


def extract_career_signals(extracurriculars: List[dict]) -> List[str]:
    """
    Extract career-relevant signals from extracurricular activities.
    
    Args:
        extracurriculars: List of {name, role, description} dicts
        
    Returns:
        List of career keywords/job titles that align with activities
    """
    signals = []
    has_leadership = False
    
    for ec in extracurriculars:
        ec_name = (ec.get("name") or "").lower()
        ec_role = (ec.get("role") or "").lower()
        ec_desc = (ec.get("description") or "").lower()
        combined_text = f"{ec_name} {ec_role} {ec_desc}"
        
        # Check for leadership roles
        if any(role in ec_role for role in LEADERSHIP_ROLES):
            has_leadership = True
            signals.extend(["leadership", "management"])
        
        # Match against EC career signals
        for keyword, careers in EC_CAREER_SIGNALS.items():
            if keyword in combined_text:
                signals.extend(careers[:3])  # Take top 3 career matches
    
    # Add general leadership signal if detected
    if has_leadership and "leadership" not in signals:
        signals.append("leadership roles")
    
    # Deduplicate and limit
    seen = set()
    unique_signals = []
    for s in signals:
        if s.lower() not in seen:
            seen.add(s.lower())
            unique_signals.append(s)
    
    return unique_signals[:10]  # Return top 10 signals


# Major to job title mapping
MAJOR_TO_JOBS = {
    # Computer Science & Engineering
    "computer science": ["software engineer", "developer", "SWE", "data scientist", "ML engineer", "backend engineer", "frontend engineer", "full stack developer"],
    "computer engineering": ["software engineer", "hardware engineer", "embedded systems", "firmware engineer", "systems engineer"],
    "software engineering": ["software engineer", "developer", "SWE", "DevOps", "platform engineer"],
    "data science": ["data scientist", "data analyst", "ML engineer", "analytics", "business intelligence"],
    "information technology": ["IT analyst", "systems administrator", "technical support", "network engineer"],
    "cybersecurity": ["security engineer", "security analyst", "penetration tester", "InfoSec"],
    "artificial intelligence": ["ML engineer", "AI researcher", "data scientist", "research scientist"],
    
    # Engineering
    "electrical engineering": ["electrical engineer", "hardware engineer", "embedded systems", "power systems"],
    "mechanical engineering": ["mechanical engineer", "design engineer", "manufacturing", "product engineer"],
    "civil engineering": ["civil engineer", "structural engineer", "construction", "infrastructure"],
    "chemical engineering": ["chemical engineer", "process engineer", "manufacturing", "R&D"],
    "biomedical engineering": ["biomedical engineer", "medical devices", "healthcare technology", "R&D"],
    "aerospace engineering": ["aerospace engineer", "systems engineer", "defense", "aviation"],
    "industrial engineering": ["industrial engineer", "operations", "supply chain", "process improvement"],
    "environmental engineering": ["environmental engineer", "sustainability", "ESG", "consulting"],
    
    # Business & Economics
    "business": ["analyst", "consultant", "operations", "business development", "strategy"],
    "business administration": ["business analyst", "operations", "management", "strategy", "consulting"],
    "finance": ["financial analyst", "investment banking", "private equity", "asset management", "trader"],
    "accounting": ["accountant", "auditor", "tax", "financial analyst", "controller"],
    "economics": ["economist", "analyst", "policy", "research", "consulting"],
    "marketing": ["marketing", "brand manager", "digital marketing", "product marketing", "growth"],
    "management": ["management", "operations", "strategy", "consulting", "program manager"],
    "entrepreneurship": ["startup", "founder", "business development", "venture capital"],
    "supply chain": ["supply chain", "logistics", "operations", "procurement"],
    "real estate": ["real estate analyst", "property management", "development", "investments"],
    
    # Sciences
    "biology": ["research", "biotech", "pharmaceutical", "lab", "life sciences"],
    "chemistry": ["chemist", "pharmaceutical", "R&D", "lab", "materials science"],
    "physics": ["physicist", "research", "data scientist", "quantitative", "engineering"],
    "mathematics": ["quantitative analyst", "data scientist", "actuary", "research", "analyst"],
    "statistics": ["statistician", "data analyst", "data scientist", "quantitative", "research"],
    "neuroscience": ["research", "neuroscience", "healthcare", "pharmaceutical", "biotech"],
    "environmental science": ["environmental", "sustainability", "conservation", "research", "ESG"],
    
    # Healthcare
    "nursing": ["nurse", "healthcare", "clinical", "patient care", "medical"],
    "public health": ["public health", "healthcare", "epidemiology", "policy", "research"],
    "pre-med": ["healthcare", "clinical", "research", "medical", "hospital"],
    "health administration": ["healthcare administration", "hospital management", "health policy"],
    "pharmacy": ["pharmacist", "pharmaceutical", "clinical", "research"],
    
    # Social Sciences
    "psychology": ["research", "HR", "UX research", "counseling", "behavioral"],
    "sociology": ["research", "policy", "social work", "community", "nonprofit"],
    "political science": ["policy", "government", "law", "political", "public affairs"],
    "international relations": ["international", "policy", "diplomacy", "NGO", "global"],
    "communications": ["communications", "PR", "marketing", "media", "content"],
    "journalism": ["journalist", "editor", "content", "media", "writer"],
    "public relations": ["PR", "communications", "media relations", "corporate communications"],
    
    # Arts & Humanities
    "english": ["writer", "editor", "content", "communications", "publishing"],
    "history": ["research", "education", "policy", "museum", "archives"],
    "philosophy": ["law", "consulting", "ethics", "policy", "research"],
    "art": ["designer", "creative", "art director", "visual", "UX"],
    "graphic design": ["graphic designer", "UI designer", "creative", "visual design", "brand"],
    "film": ["video production", "film", "media", "content", "entertainment"],
    "music": ["music", "entertainment", "production", "creative", "education"],
    
    # Other
    "education": ["teacher", "education", "curriculum", "training", "instructional design"],
    "law": ["legal", "paralegal", "compliance", "policy", "law clerk"],
    "architecture": ["architect", "design", "urban planning", "construction"],
    "urban planning": ["urban planner", "city planning", "policy", "development"],
}


def get_job_keywords_for_major(major: str) -> List[str]:
    """
    Get relevant job title keywords for a given major.
    
    Args:
        major: User's academic major (e.g., "Data Science and Economics")
        
    Returns:
        List of relevant job title keywords
    """
    if not major:
        return ["entry level", "associate", "analyst"]
    
    major_lower = major.lower().strip()
    
    # Combined majors (like "Data Science and Economics")
    if 'data' in major_lower and 'economics' in major_lower:
        return ['Data Scientist', 'Data Analyst', 'Quantitative Analyst', 'Financial Analyst', 'Business Intelligence']
    if 'data' in major_lower and 'finance' in major_lower:
        return ['Data Scientist', 'Data Analyst', 'Quantitative Analyst', 'Financial Analyst', 'Business Intelligence']
    
    # Data Science / Analytics
    if 'data science' in major_lower or 'data analytics' in major_lower:
        return ['Data Scientist', 'Data Analyst', 'Machine Learning', 'ML Engineer', 'Data Engineer', 'Analytics']
    
    # Computer Science / Software Engineering
    if 'computer science' in major_lower or 'software' in major_lower:
        return ['Software Engineer', 'Developer', 'SWE', 'Backend', 'Frontend', 'Full Stack']
    
    # Economics / Finance
    if 'economics' in major_lower or 'finance' in major_lower:
        return ['Financial Analyst', 'Investment Banking', 'Quantitative', 'Economics', 'Strategy']
    
    # Business / Management
    if 'business' in major_lower or 'management' in major_lower:
        return ['Business Analyst', 'Product Manager', 'Strategy', 'Consultant', 'Operations']
    
    # Direct match
    if major_lower in MAJOR_TO_JOBS:
        return MAJOR_TO_JOBS[major_lower]
    
    # Partial match
    for key, jobs in MAJOR_TO_JOBS.items():
        if key in major_lower or major_lower in key:
            return jobs
    
    # Keyword matching
    for key, jobs in MAJOR_TO_JOBS.items():
        key_words = key.split()
        if any(word in major_lower for word in key_words):
            return jobs
    
    # Default fallback
    return ["entry level", "associate", "analyst", "coordinator"]


# Industry to keywords mapping (for query building)
INDUSTRY_TO_KEYWORDS = {
    "technology": ["software", "tech", "IT", "developer", "engineer"],
    "finance": ["financial", "banking", "investment", "analyst"],
    "consulting": ["consultant", "advisory", "strategy"],
    "healthcare": ["health", "medical", "clinical", "hospital"],
    "marketing": ["marketing", "advertising", "brand", "digital"],
    "media": ["media", "entertainment", "content", "journalism"],
    "retail": ["retail", "e-commerce", "merchandising"],
    "manufacturing": ["manufacturing", "operations", "supply chain"],
    "education": ["education", "teaching", "academic", "learning"],
    "government": ["government", "public sector", "policy", "federal"],
    "nonprofit": ["nonprofit", "NGO", "social impact", "foundation"],
    "real estate": ["real estate", "property", "development"],
    "energy": ["energy", "oil", "gas", "renewable", "utilities"],
    "automotive": ["automotive", "auto", "vehicle", "transportation"],
    "aerospace": ["aerospace", "aviation", "defense", "space"],
}

# ============================================
# COMPANY TIER LISTS (for quality filtering)
# ============================================

TOP_TECH_COMPANIES = [
    # FAANG / MAANG
    "google", "meta", "amazon", "apple", "netflix", "microsoft",
    # Top Tech
    "nvidia", "salesforce", "adobe", "oracle", "ibm", "intel", "cisco",
    # Unicorns & High-Growth
    "stripe", "databricks", "figma", "canva", "notion", "airtable", "plaid",
    "coinbase", "robinhood", "instacart", "doordash", "uber", "lyft", "airbnb",
    "snowflake", "datadog", "mongodb", "twilio", "okta", "crowdstrike",
    "palantir", "splunk", "servicenow", "workday", "zoom", "slack", "dropbox",
    "spotify", "pinterest", "snap", "twitter", "linkedin", "tiktok", "bytedance",
    # Defense/Gov Tech
    "anduril", "scale ai", "openai", "anthropic",
]

TOP_FINANCE_COMPANIES = [
    # Investment Banks (Bulge Bracket)
    "goldman sachs", "jp morgan", "jpmorgan", "morgan stanley", "bank of america",
    "citigroup", "citi", "barclays", "deutsche bank", "ubs", "credit suisse",
    # Elite Boutiques
    "evercore", "lazard", "moelis", "centerview", "perella weinberg", "pwp",
    "pjt partners", "greenhill", "rothschild",
    # Private Equity
    "blackstone", "kkr", "carlyle", "apollo", "tpg", "warburg pincus",
    "advent international", "bain capital", "silver lake", "thoma bravo",
    # Hedge Funds
    "citadel", "bridgewater", "two sigma", "de shaw", "jane street", "point72",
    "millennium", "aqr", "renaissance", "man group",
    # Asset Management
    "blackrock", "vanguard", "fidelity", "t. rowe price", "state street",
    "pimco", "wellington", "capital group",
    # Venture Capital
    "sequoia", "andreessen horowitz", "a16z", "kleiner perkins", "accel",
    "benchmark", "greylock", "lightspeed", "general catalyst",
]

TOP_CONSULTING_COMPANIES = [
    # MBB
    "mckinsey", "bain", "boston consulting", "bcg",
    # Big 4
    "deloitte", "pwc", "pricewaterhousecoopers", "ey", "ernst young", "kpmg",
    # Tier 2 Consulting
    "accenture", "booz allen", "oliver wyman", "roland berger", "strategy&",
    "l.e.k.", "lek", "simon-kucher", "kearney", "atkearney",
    # Boutique/Specialty
    "parthenon", "ey-parthenon", "zs associates", "cornerstone research",
]

FORTUNE_500_KEYWORDS = [
    "fortune 500", "fortune500", "f500",
    "walmart", "exxon", "chevron", "berkshire", "unitedhealth",
    "johnson & johnson", "j&j", "procter & gamble", "p&g",
    "jpmorgan", "visa", "mastercard", "home depot", "pfizer",
    "coca-cola", "pepsi", "nike", "starbucks", "disney", "boeing",
    "3m", "caterpillar", "general electric", "ge", "honeywell",
    "lockheed martin", "raytheon", "northrop grumman",
]

# Generic/Low-Quality company name patterns to filter out
LOW_QUALITY_COMPANY_PATTERNS = [
    "recruiting", "staffing", "placement", "agency", "temp ",
    "personnel", "workforce", "employment services", "hiring",
    "talent acquisition", "job seekers", "career services",
    "remote jobs", "work from home", "wfh jobs",
]

# Spam keywords to detect in descriptions
SPAM_KEYWORDS = [
    "make money fast", "work from home!!!", "earn $", "$$",
    "no experience needed", "hiring immediately!!!", "urgent!!!",
    "apply now!!!", "!!!",
]

# High-quality job sources (prioritize these)
HIGH_QUALITY_SOURCES = [
    "linkedin", "company website", "glassdoor", "indeed", 
    "lever", "greenhouse", "workday", "careers page",
]

# Industry to top companies mapping
INDUSTRY_TOP_COMPANIES = {
    "technology": TOP_TECH_COMPANIES,
    "tech": TOP_TECH_COMPANIES,
    "software": TOP_TECH_COMPANIES,
    "finance": TOP_FINANCE_COMPANIES,
    "banking": TOP_FINANCE_COMPANIES,
    "investment": TOP_FINANCE_COMPANIES,
    "consulting": TOP_CONSULTING_COMPANIES,
    "strategy": TOP_CONSULTING_COMPANIES,
}

# =============================================================================
# SKILL SYNONYMS FOR SEMANTIC MATCHING
# =============================================================================

SKILL_SYNONYMS = {
    # Programming Languages
    "python": ["python3", "python programming", "python development", "py"],
    "javascript": ["js", "node.js", "nodejs", "node", "es6", "typescript", "ts"],
    "java": ["java programming", "j2ee", "spring", "spring boot"],
    "c++": ["cpp", "c plus plus", "cplusplus"],
    "c#": ["csharp", "c sharp", ".net", "dotnet"],
    "sql": ["mysql", "postgresql", "postgres", "sqlite", "database", "queries"],
    "r": ["r programming", "r language", "rstudio"],
    
    # Data Science / ML
    "machine learning": ["ml", "deep learning", "neural networks", "ai", "artificial intelligence"],
    "data science": ["data analytics", "data analysis", "statistical analysis", "analytics"],
    "data analysis": ["data analytics", "analytics", "business intelligence", "bi"],
    "tensorflow": ["tf", "keras", "pytorch", "deep learning framework"],
    "pandas": ["dataframes", "data manipulation", "python data"],
    
    # Web Development
    "react": ["reactjs", "react.js", "react native"],
    "angular": ["angularjs", "angular.js"],
    "vue": ["vuejs", "vue.js"],
    "html": ["html5", "html/css", "web development"],
    "css": ["css3", "scss", "sass", "styling"],
    "flask": ["python flask", "flask framework"],
    "django": ["python django", "django framework"],
    
    # Cloud & DevOps
    "aws": ["amazon web services", "cloud computing", "ec2", "s3", "lambda"],
    "azure": ["microsoft azure", "cloud"],
    "gcp": ["google cloud", "google cloud platform"],
    "docker": ["containers", "containerization", "kubernetes", "k8s"],
    "kubernetes": ["k8s", "container orchestration", "docker"],
    "ci/cd": ["continuous integration", "continuous deployment", "devops", "jenkins", "github actions"],
    
    # Business / Finance
    "financial modeling": ["financial analysis", "dcf", "valuation", "excel modeling"],
    "excel": ["spreadsheets", "microsoft excel", "google sheets", "vlookup", "pivot tables"],
    "powerpoint": ["presentations", "slides", "ppt", "google slides"],
    "accounting": ["bookkeeping", "financial reporting", "gaap"],
    "investment banking": ["ib", "m&a", "mergers and acquisitions", "capital markets"],
    
    # Soft Skills
    "leadership": ["team lead", "management", "leading teams", "team leadership"],
    "communication": ["written communication", "verbal communication", "presentation skills"],
    "project management": ["pm", "agile", "scrum", "project planning", "pmp"],
    "teamwork": ["collaboration", "team player", "cross-functional"],
    "problem solving": ["analytical", "critical thinking", "troubleshooting"],
}

# Build reverse mapping for quick lookup
SKILL_TO_SYNONYMS: Dict[str, Set[str]] = {}
for primary, synonyms in SKILL_SYNONYMS.items():
    all_terms = {primary.lower()} | {s.lower() for s in synonyms}
    for term in all_terms:
        if term not in SKILL_TO_SYNONYMS:
            SKILL_TO_SYNONYMS[term] = set()
        SKILL_TO_SYNONYMS[term].update(all_terms)


# =============================================================================
# PHASE 3: Domain-Owned Query Templates
# =============================================================================

# Domain-specific query template registry
DOMAIN_QUERY_TEMPLATES = {
    "finance_banking": {
        "allowed_roles": [
            "Investment Banking",
            "Summer Analyst",
            "Private Equity",
            "Equity Research",
            "Asset Management",
            "Corporate Finance",
            "Wealth Management",
            "Trading",
            "Sales & Trading",
            "Investment Banking Analyst",
        ],
        "allowed_programs": [
            "Summer Analyst Program",
            "Investment Banking Internship Program",
            "Finance Internship Program",
            "Analyst Program",
        ],
        "top_firms": [
            "Goldman Sachs", "Morgan Stanley", "JP Morgan", "JPMorgan",
            "Bank of America", "Citi", "Citigroup", "Credit Suisse",
            "Barclays", "Deutsche Bank", "UBS", "Wells Fargo",
            "Evercore", "Lazard", "Moelis", "Centerview",
            "PJT Partners", "Rothschild", "Greenhill", "Houlihan Lokey",
        ],
        "forbidden_keywords": [
            "python", "java", "sql", "software engineer", "SWE", "SDE",
            "data scientist", "product manager", "product",
            "analyst intern",  # Too generic without finance qualifier
            "business intern",  # Too generic
        ],
        "requires_finance_keyword": True,  # Every query must include finance-specific role
    },
    "technology": {
        "allowed_roles": [
            "Software Engineer",
            "Backend Engineer",
            "Frontend Engineer",
            "Full Stack Engineer",
            "Data Engineer",
            "Machine Learning Engineer",
            "Product Manager",
            "SWE",
            "SDE",
        ],
        "allowed_skills": [
            "Python", "JavaScript", "Java", "React", "C++", "TypeScript",
            "Node.js", "AWS", "Docker", "Kubernetes",
        ],
        "top_firms": TOP_TECH_COMPANIES[:15],
        "forbidden_keywords": [
            "investment banking", "private equity", "consulting",
            "financial analyst", "equity research",
        ],
        "requires_finance_keyword": False,
    },
    "consulting": {
        "allowed_roles": [
            "Consultant",
            "Strategy Consultant",
            "Management Consultant",
            "Business Analyst",
            "Associate Consultant",
        ],
        "top_firms": TOP_CONSULTING_COMPANIES[:10],
        "forbidden_keywords": [
            "software engineer", "developer", "programmer",
            "investment banking", "private equity",
        ],
        "requires_finance_keyword": False,
    },
}


def build_personalized_queries(user_profile: dict, job_types: List[str]) -> List[dict]:
    """
    PHASE 3: Build intent-aligned queries using domain-specific templates.
    
    Uses user_profile["_intent_contract"] to generate domain-appropriate queries
    with location-first narrowing.
    
    Args:
        user_profile: Comprehensive user career profile (must have _intent_contract)
        job_types: List of job types (e.g., ["Internship", "Full-Time"])
        
    Returns:
        List of query dicts: [{query: str, priority: int, source: str, weight: float}]
    """
    queries = []
    intent_contract = user_profile.get("_intent_contract", {})
    
    # PHASE 3: If no intent contract, fall back to old logic (backwards compatibility)
    if not intent_contract:
        logger.warning("[QueryGen][WARN] No intent_contract found, using legacy query generation")
        return _build_legacy_queries(user_profile, job_types)
    
    # Extract normalized intent
    career_domains = intent_contract.get("career_domains", [])
    preferred_locations = intent_contract.get("preferred_locations", [])
    normalized_job_types = intent_contract.get("job_types", [])
    
    # Normalize job type for search
    job_type_str = normalized_job_types[0].lower() if normalized_job_types and len(normalized_job_types) > 0 else (job_types[0].lower() if job_types and len(job_types) > 0 else "internship")
    if job_type_str == "full-time":
        job_type_str = "entry level"
    elif job_type_str == "internship":
        job_type_str = "internship"
    
    # Determine primary domain (use first domain)
    primary_domain = career_domains[0] if career_domains else None
    
    if not primary_domain:
        logger.warning("[QueryGen][WARN] No career_domains in intent_contract, using legacy query generation")
        return _build_legacy_queries(user_profile, job_types)
    
    # Get domain template
    domain_template = DOMAIN_QUERY_TEMPLATES.get(primary_domain)
    if not domain_template:
        logger.warning(f"[QueryGen][WARN] No template for domain={primary_domain}, using legacy query generation")
        return _build_legacy_queries(user_profile, job_types)
    
    # PHASE 3: Location-first query narrowing
    # Build queries with location embedded, not as fallback
    location_variants = _build_location_variants(preferred_locations)
    
    # Query Priority Order (as specified):
    # 1. Exact role + location
    # 2. Program-based + location
    # 3. Firm-based + location
    # 4. Remote-only (explicit)
    
    priority = 1
    
    # 1. EXACT ROLE + LOCATION queries (location embedded in query)
    # PHASE 5 FIX: Generate queries for EACH preferred location, not just first 3 variants
    if domain_template.get("allowed_roles"):
        for role in domain_template["allowed_roles"][:5]:  # Top 5 roles
            # Iterate through each preferred location directly
            for pref_location in preferred_locations[:3]:  # Top 3 preferred locations
                if "remote" in pref_location.lower():
                    query = f"{role} {job_type_str} Remote"
                    location_param = "Remote"
                else:
                    # Use full location format: "City, State"
                    query = f"{role} {job_type_str} {pref_location}"
                    location_param = pref_location
                    
                    # PHASE 7B: Remove city-only variant (deduplication now handles this)
                    # City-only variants create duplicate queries that return same jobs
                    # Since deduplication exists, prefer full location format only
                
                queries.append({
                    "query": query,
                    "priority": priority,
                    "source": f"role_{role.replace(' ', '_').lower()}",
                    "weight": 1.3,  # High weight for exact role matches
                    "location": location_param  # Store location for SerpAPI param
                })
                priority += 1
    
    # 2. PROGRAM-BASED + LOCATION queries
    # PHASE 5 FIX: Generate for each preferred location
    if domain_template.get("allowed_programs"):
        for program in domain_template["allowed_programs"][:3]:  # Top 3 programs
            for pref_location in preferred_locations[:2]:  # Top 2 preferred locations
                if "remote" in pref_location.lower():
                    query = f"{program} Remote"
                    location_param = "Remote"
                else:
                    query = f"{program} {pref_location}"
                    location_param = pref_location
                queries.append({
                    "query": query,
                    "priority": priority,
                    "source": f"program_{program.replace(' ', '_').lower()}",
                    "weight": 1.25,  # High weight for program matches
                    "location": location_param
                })
                priority += 1
    
    # 3. FIRM-BASED + LOCATION queries
    # PHASE 5 FIX: Generate for each preferred location
    if domain_template.get("top_firms"):
        firms = domain_template["top_firms"][:5]  # Top 5 firms
        for firm in firms:
            for pref_location in preferred_locations[:2]:  # Top 2 preferred locations
                if "remote" in pref_location.lower():
                    query = f"{firm} {job_type_str} Remote"
                    location_param = "Remote"
                else:
                    query = f"{firm} {job_type_str} {pref_location}"
                    location_param = pref_location
                queries.append({
                    "query": query,
                    "priority": priority,
                    "source": f"firm_{firm.replace(' ', '_').lower()}",
                    "weight": 1.2,  # High weight for top firms
                    "location": location_param
                })
                priority += 1
    
    # 4. REMOTE-ONLY queries (explicit)
    if domain_template.get("allowed_roles"):
        for role in domain_template["allowed_roles"][:3]:  # Top 3 roles
            query = f"Remote {role} {job_type_str}"
            queries.append({
                "query": query,
                "priority": priority,
                "source": "remote_explicit",
                "weight": 1.15,
                "location": "United States"  # Remote jobs use US as location param
            })
            priority += 1
    
    # PHASE 3: Technology domain can use skill-based queries (ONLY for tech domain)
    if primary_domain == "technology" and domain_template.get("allowed_skills"):
        skills = user_profile.get("skills", [])
        if skills:
            # Use top 3 skills that match allowed_skills
            matching_skills = [s for s in skills[:5] if any(allowed.lower() in s.lower() or s.lower() in allowed.lower() 
                                                           for allowed in domain_template["allowed_skills"])]
            for skill in matching_skills[:3]:  # Top 3 matching skills
                for location_variant in location_variants[:2]:
                    if location_variant.lower() == "remote":
                        query = f"{skill} {job_type_str} Remote"
                    else:
                        query = f"{skill} {job_type_str} {location_variant}"
                    queries.append({
                        "query": query,
                        "priority": priority,
                        "source": f"skill_{skill.lower().replace(' ', '_')}",
                        "weight": 1.1,
                        "location": location_variant
                    })
                    priority += 1
    
    # PHASE 3: Kill generic skill queries for non-technical domains
    # This is handled by domain templates - only technology domain has allowed_skills
    
    # 5. SOURCE-SPECIFIC queries (Glassdoor, ZipRecruiter, Wellfound, Workday)
    # Google Jobs aggregates from these platforms; appending the platform name
    # biases results toward listings sourced from that platform.
    source_platforms = ["Glassdoor", "ZipRecruiter", "Wellfound", "Workday"]
    if domain_template.get("allowed_roles"):
        top_role = domain_template["allowed_roles"][0]
        primary_location = preferred_locations[0] if preferred_locations else "United States"
        for platform in source_platforms:
            queries.append({
                "query": f"{top_role} {job_type_str} {platform}",
                "priority": priority,
                "source": f"platform_{platform.lower()}",
                "weight": 1.0,
                "location": primary_location,
            })
            priority += 1

    # PHASE 7B: Deduplicate queries (remove exact duplicates and near-duplicates)
    seen_queries_normalized = set()
    deduplicated_queries = []
    removed_query_count = 0
    
    for q in queries:
        # Normalize query for comparison (lowercase, strip, remove extra spaces)
        query_normalized = " ".join(q["query"].lower().strip().split())
        if query_normalized not in seen_queries_normalized:
            seen_queries_normalized.add(query_normalized)
            deduplicated_queries.append(q)
        else:
            removed_query_count += 1
            logger.info(f"[QueryGen][REDUCE] removed_duplicate_query=\"{q['query']}\"")
    
    queries = deduplicated_queries
    
    # Limit total queries (stop once we have enough)
    queries = queries[:12]  # Max 12 queries to avoid API overload
    
    if removed_query_count > 0:
        logger.info(f"[QueryGen][REDUCE] removed {removed_query_count} duplicate queries, {len(queries)} remaining")
    
    # PHASE 3: Safety check - if no queries generated, try fallback expansion
    if not queries:
        logger.warning(f"[QueryGen][WARN] No queries generated for domain={primary_domain}, attempting fallback expansion")
        
        # Expand firms (safety fallback)
        if domain_template.get("top_firms") and len(domain_template["top_firms"]) > 5:
            firms = domain_template["top_firms"][5:10]  # Next 5 firms
            primary_location = location_variants[0] if location_variants else "United States"
            for firm in firms[:3]:
                query = f"{firm} {job_type_str} {primary_location}"
                queries.append({
                    "query": query,
                    "priority": 200,
                    "source": "fallback_expand_firms",
                    "weight": 0.9,
                    "location": primary_location
                })
        
        # Expand program keywords (safety fallback)
        if not queries and domain_template.get("allowed_programs") and len(domain_template["allowed_programs"]) > 3:
            programs = domain_template["allowed_programs"][3:6]
            primary_location = location_variants[0] if location_variants else "United States"
            for program in programs[:2]:
                query = f"{program} {primary_location}"
                queries.append({
                    "query": query,
                    "priority": 250,
                    "source": "fallback_expand_programs",
                    "weight": 0.85,
                    "location": primary_location
                })
        
        # Expand geography one level (safety fallback)
        if not queries and location_variants:
            # Try state-level if we were using city-level
            expanded_locations = [loc for loc in location_variants if "," not in loc or loc.split(",")[1].strip()][:2]
            allowed_roles = domain_template.get("allowed_roles", [])
            if expanded_locations and allowed_roles and len(allowed_roles) > 0:
                role = allowed_roles[0]
                for loc in expanded_locations[:2]:
                    query = f"{role} {job_type_str} {loc}"
                    queries.append({
                        "query": query,
                        "priority": 300,
                        "source": "fallback_expand_geography",
                        "weight": 0.8,
                        "location": loc
                    })
        
        # Minimal fallback (last resort - but NEVER generic "internship")
        if not queries:
            logger.error(f"[QueryGen][WARN] All fallback attempts failed for domain={primary_domain}, using minimal specific fallback")
            allowed_roles = domain_template.get("allowed_roles", [])
            fallback_role = allowed_roles[0] if allowed_roles and len(allowed_roles) > 0 else None
            if not fallback_role:
                # Even in fallback, use domain-specific role, not generic
                if primary_domain == "finance_banking":
                    fallback_role = "Investment Banking"
                elif primary_domain == "technology":
                    fallback_role = "Software Engineer"
                elif primary_domain == "consulting":
                    fallback_role = "Consultant"
                else:
                    fallback_role = job_type_str
            
            primary_location = location_variants[0] if location_variants else "United States"
            query = f"{fallback_role} {job_type_str} {primary_location}"
            queries.append({
                "query": query,
                "priority": 400,
                "source": "fallback_minimal_specific",
                "weight": 0.7,
                "location": primary_location
            })
    
    # PHASE 3: Validate queries don't contain forbidden keywords (safety check)
    if domain_template.get("forbidden_keywords"):
        forbidden = domain_template["forbidden_keywords"]
        validated_queries = []
        for q in queries:
            query_lower = q["query"].lower()
            # Check if query contains any forbidden keywords
            contains_forbidden = any(forbidden_kw in query_lower for forbidden_kw in forbidden)
            if contains_forbidden:
                logger.warning(f"[QueryGen][WARN] Query contains forbidden keyword for domain={primary_domain}: {q['query']}, skipping")
                continue
            validated_queries.append(q)
        
        if len(validated_queries) < len(queries):
            logger.warning(f"[QueryGen][WARN] Filtered out {len(queries) - len(validated_queries)} queries with forbidden keywords")
            queries = validated_queries
        
        # If all queries were filtered, use minimal fallback
        if not queries:
            logger.error(f"[QueryGen][ERROR] All queries filtered due to forbidden keywords, using minimal fallback")
            allowed_roles = domain_template.get("allowed_roles", [])
            fallback_role = allowed_roles[0] if allowed_roles and len(allowed_roles) > 0 else job_type_str
            primary_location = location_variants[0] if location_variants else "United States"
            queries.append({
                "query": f"{fallback_role} {job_type_str} {primary_location}",
                "priority": 500,
                "source": "fallback_after_validation",
                "weight": 0.6,
                "location": primary_location
            })
    
    # Log query generation
    query_strings = [q["query"] for q in queries]
    logger.info(f"[QueryGen] domain={primary_domain} generated_queries={query_strings[:5]}... (total={len(queries)})")
    
    return queries


def _build_location_variants(preferred_locations: List[str]) -> List[str]:
    """
    PHASE 3: Build location variants for location-first query narrowing.
    
    Priority order:
    1. Preferred city (exact)
    2. Metro area
    3. State
    4. Remote-only (if no locations)
    
    NEVER starts at "United States" unless no preferences.
    """
    if not preferred_locations:
        return ["Remote"]  # Explicit remote, not "United States"
    
    variants = []
    
    for location in preferred_locations[:3]:  # Top 3 preferred locations
        # Handle "Remote" specially
        if "remote" in location.lower():
            variants.append("Remote")
            continue
        
        # Extract components: "City, State" format
        if "," in location:
            parts = [p.strip() for p in location.split(",")]
            city = parts[0]
            state = parts[1] if len(parts) > 1 else ""
            
            # Variant 1: Full "City, State"
            variants.append(location)
            
            # Variant 2: City only (metro area)
            if city:
                variants.append(city)
            
            # Variant 3: State only (broader geographic)
            if state:
                variants.append(state)
        else:
            # Just city or state name
            variants.append(location)
    
    # Deduplicate while preserving order
    seen = set()
    unique_variants = []
    for variant in variants:
        variant_lower = variant.lower()
        if variant_lower not in seen:
            seen.add(variant_lower)
            unique_variants.append(variant)
    
    return unique_variants[:5]  # Max 5 variants


def _build_legacy_queries(user_profile: dict, job_types: List[str]) -> List[dict]:
    """
    Legacy query generation (backwards compatibility when intent_contract missing).
    This is the old implementation kept for fallback.
    """
    queries = []
    job_type_str = job_types[0].lower() if job_types else "internship"
    
    if job_type_str == "full-time":
        job_type_str = "entry level"
    
    major = user_profile.get("major", "")
    skills = user_profile.get("skills", [])
    interests = user_profile.get("interests", [])
    target_industries = user_profile.get("target_industries", [])
    
    if major:
        major_jobs = get_job_keywords_for_major(major)[:4]
        if major_jobs:
            major_query = f"{job_type_str} ({' OR '.join(major_jobs[:3])})"
            queries.append({
                "query": major_query,
                "priority": 1,
                "source": "major",
                "weight": 1.2
            })
    
    if skills and len(skills) >= 2:
        top_skills = skills[:4]
        skills_query = f"{job_type_str} ({' OR '.join(top_skills[:3])})"
        queries.append({
            "query": skills_query,
            "priority": 2,
            "source": "skills",
            "weight": 1.1
        })
    
    if target_industries:
        industry = target_industries[0]
        industry_keywords = INDUSTRY_TO_KEYWORDS.get(industry.lower(), [industry])
        industry_query = f"{job_type_str} {' OR '.join(industry_keywords[:2])}"
        queries.append({
            "query": industry_query,
            "priority": 4,
            "source": "industry",
            "weight": 1.0
        })
    
    if not queries:
        queries.append({
            "query": f"{job_type_str} jobs",
            "priority": 10,
            "source": "fallback",
            "weight": 0.8
        })
    
    return queries


def build_top_companies_query(user_profile: dict, job_type_str: str) -> str:
    """
    Build a query targeting top/prestigious companies based on user's profile.
    
    Args:
        user_profile: User's career profile
        job_type_str: Job type (internship, entry level, etc.)
        
    Returns:
        Query string targeting top companies, or empty string if no match
    """
    major = (user_profile.get("major") or "").lower()
    target_industries = user_profile.get("target_industries", [])
    
    # Determine which company tier list to use
    companies_to_target = []
    
    # Check major for industry signals
    if any(kw in major for kw in ["computer", "software", "data", "information", "electrical"]):
        companies_to_target = TOP_TECH_COMPANIES[:8]
    elif any(kw in major for kw in ["finance", "economics", "accounting", "business"]):
        # Mix of finance and consulting
        companies_to_target = TOP_FINANCE_COMPANIES[:5] + TOP_CONSULTING_COMPANIES[:3]
    elif any(kw in major for kw in ["math", "statistics", "physics"]):
        # Quant-friendly companies
        companies_to_target = ["jane street", "two sigma", "citadel", "de shaw"] + TOP_TECH_COMPANIES[:4]
    
    # Override with target industries if specified
    if target_industries:
        industry = target_industries[0].lower()
        if industry in INDUSTRY_TOP_COMPANIES:
            companies_to_target = INDUSTRY_TOP_COMPANIES[industry][:8]
    
    if not companies_to_target:
        # Default to a mix of top companies
        companies_to_target = TOP_TECH_COMPANIES[:3] + TOP_FINANCE_COMPANIES[:2] + TOP_CONSULTING_COMPANIES[:2]
    
    # Build query with company names
    # Take top 5 to keep query reasonable
    top_5 = companies_to_target[:5]
    company_or_clause = " OR ".join([f'"{c}"' for c in top_5])
    
    return f"{job_type_str} ({company_or_clause})"


def build_search_query(
    job_types: List[str],
    industries: List[str],
    user_major: Optional[str] = None,
    user_skills: Optional[List[str]] = None,
    user_key_experiences: Optional[List[str]] = None,
) -> str:
    """
    Build an optimized search query based on user preferences and resume data.
    """
    query_parts = []
    
    # Add job type
    if "Internship" in job_types:
        query_parts.append("internship")
    elif "Full-Time" in job_types:
        query_parts.append("entry level")
    
    # Add resume-based skills (top 2-3 most relevant)
    if user_skills and len(user_skills) > 0:
        # Take top skills and add them to query
        top_skills = user_skills[:3]
        skills_query = " OR ".join(top_skills)
        query_parts.append(f"({skills_query})")
    
    # Add industries/interests
    if industries:
        top_industries = industries[:2]  # Focus on top 2
        
        industry_mapping = {
            "Technology": "software engineer OR product manager",
            "Finance": "financial analyst OR investment banking",
            "Consulting": "consultant OR strategy",
            "Healthcare": "healthcare OR medical",
            "Marketing": "marketing OR brand manager",
            "Engineering": "engineer",
            "Data Science": "data scientist OR machine learning",
            "Design": "UX designer OR product designer",
            "Sales": "sales OR business development",
            "Operations": "operations OR supply chain",
            "Accounting": "accountant OR audit",
            "Legal": "legal OR paralegal",
            "Human Resources": "HR OR recruiting",
            "Real Estate": "real estate OR property",
        }
        
        for industry in top_industries:
            if industry in industry_mapping:
                query_parts.append(industry_mapping[industry])
            else:
                query_parts.append(industry.lower())
    
    # Add major if no industries
    if not industries and user_major:
        query_parts.append(user_major.lower())
    
    # Default fallback
    if not query_parts:
        query_parts.append("entry level jobs")
    
    return " ".join(query_parts)


def build_location_query(locations: List[str]) -> str:
    """
    PHASE 3: Build location string for SerpAPI (location-first approach).
    
    Uses first preferred location, never defaults to "United States" unless
    no locations specified or only remote.
    """
    if not locations:
        return "United States"  # Fallback only if no preferences
    
    primary_location = locations[0]
    
    # Handle remote specially
    if "remote" in primary_location.lower():
        return "United States"  # Remote jobs can be anywhere in US
    
    # PHASE 3: Use preferred location directly (location-first)
    return primary_location


# =============================================================================
# JOB MATCHING HELPER FUNCTIONS
# =============================================================================

def expand_skill_terms(skill: str) -> Set[str]:
    """Expand a skill into all its synonyms and variations."""
    skill_lower = skill.lower().strip()
    expanded = SKILL_TO_SYNONYMS.get(skill_lower, {skill_lower})
    
    for primary, synonyms in SKILL_SYNONYMS.items():
        all_terms = [primary.lower()] + [s.lower() for s in synonyms]
        if skill_lower in all_terms:
            expanded.update(all_terms)
    
    return expanded


_skill_pattern_cache: Dict[str, re.Pattern] = {}

def semantic_skill_match(skill: str, job_text: str) -> Tuple[bool, float]:
    """
    Check if skill matches job text, considering synonyms.
    Returns (matched, confidence_score).
    """
    expanded_skills = expand_skill_terms(skill)
    job_text_lower = job_text.lower()

    for term in expanded_skills:
        if len(term) <= 4:
            # Use cached compiled regex for short terms (word boundary match)
            if term not in _skill_pattern_cache:
                _skill_pattern_cache[term] = re.compile(r'\b' + re.escape(term) + r'\b')
            if _skill_pattern_cache[term].search(job_text_lower):
                confidence = 1.0 if term == skill.lower() else 0.85
                return True, confidence
        else:
            if term in job_text_lower:
                confidence = 1.0 if term == skill.lower() else 0.85
                return True, confidence

    return False, 0.0


def calculate_field_affinity(user_major: str, job_title: str, job_desc: str) -> float:
    """
    Calculate how related the user's field is to the job.
    Returns 0.0 - 1.0 affinity score.
    """
    if not user_major:
        return 0.3
    
    major_lower = user_major.lower()
    job_text = f"{job_title} {job_desc}".lower()
    job_title_lower = job_title.lower()
    
    # Field clusters
    tech_fields = ["computer science", "software", "data science", "information", "computer engineering", "cs"]
    finance_fields = ["finance", "accounting", "economics", "business", "mba"]
    engineering_fields = ["mechanical", "electrical", "civil", "chemical", "aerospace", "biomedical"]
    science_fields = ["biology", "chemistry", "physics", "mathematics", "statistics"]
    
    # Job type indicators
    pure_tech_jobs = ["software engineer", "developer", "programmer", "swe", "frontend", "backend", "full stack"]
    data_tech_jobs = ["data scientist", "data science", "data engineer", "data analyst", "machine learning", "ml engineer", "ai engineer", "analytics"]
    pure_finance_jobs = ["investment banking", "investment analyst", "financial analyst", "trader", "equity research"]
    quant_jobs = ["quantitative", "quant analyst", "quant developer", "algorithmic"]
    
    if major_lower in job_text:
        return 1.0
    
    job_is_pure_tech = any(j in job_title_lower for j in pure_tech_jobs)
    job_is_data_tech = any(j in job_title_lower for j in data_tech_jobs)
    job_is_pure_finance = any(j in job_title_lower for j in pure_finance_jobs)
    job_is_quant = any(j in job_title_lower for j in quant_jobs)
    
    user_is_tech = any(f in major_lower for f in tech_fields)
    user_is_finance = any(f in major_lower for f in finance_fields)
    user_is_engineering = any(f in major_lower for f in engineering_fields)
    user_is_science = any(f in major_lower for f in science_fields)
    
    # Combined majors (e.g., "Data Science and Economics")
    # These should match both data tech and finance jobs well
    user_is_data_and_finance = ('data' in major_lower and ('economics' in major_lower or 'finance' in major_lower))
    
    # Strong matches
    if user_is_data_and_finance and (job_is_data_tech or job_is_quant or job_is_pure_finance):
        return 0.9  # Very good match for combined majors
    if user_is_tech and (job_is_pure_tech or job_is_data_tech):
        return 0.95
    if user_is_finance and job_is_pure_finance:
        return 0.95
    if user_is_science and job_is_quant:
        return 0.85
    if user_is_tech and job_is_quant:
        return 0.75
    
    # Partial matches
    if user_is_data_and_finance and job_is_pure_tech:
        return 0.5  # Moderate match for SWE roles
    if user_is_engineering and job_is_pure_tech:
        return 0.6
    if user_is_science and job_is_data_tech:
        return 0.7
    if user_is_finance and job_is_data_tech:
        return 0.4
    
    # Weak matches
    if user_is_tech and job_is_pure_finance:
        return 0.15
    if user_is_finance and job_is_pure_tech:
        return 0.1
    if user_is_science and job_is_pure_finance:
        return 0.3
    
    return 0.2


# =============================================================================
# PHASE 2: HARD GATES (Intent-Based Filtering)
# =============================================================================

# =============================================================================
# PHASE 4A: Domain-Aware Seniority Rules
# =============================================================================

# PHASE 7B.1: Hard block keywords that prevent entry-level overrides
SENIORITY_HARD_BLOCK_KEYWORDS = [
    "level 4", "level 5", "level iv", "level v",
    "l4", "l5",
    "staff", "principal", "lead"
]

DOMAIN_SENIORITY_RULES = {
    "finance_banking": {
        # Entry-level roles (ALLOW for internship/new grad seekers)
        # PHASE 7B.2: Finance-specific entry-level allowlist (analyst keywords are entry-level in finance)
        "entry_level_allow": [
            "analyst",
            "summer analyst",
            "investment banking analyst",
            "capital markets analyst",
            "equity research analyst",
            "corporate banking analyst",
            "transaction advisory analyst",
            "associate",  # If internship context
            "early careers",
            "early career",
            "capital markets",  # If intern context
            "cib",  # Corporate & Investment Banking
            "markets",  # If intern context
            "investment banking intern",
            "corporate banking intern",
            "equity research intern",
            "sales and trading intern",
            "asset management intern",
            "wealth management intern",
        ],
        # Senior roles (REJECT for internship/new grad seekers)
        "senior_reject": [
            "vice president",
            "vp ",
            "director",
            "managing director",
            "md ",
            "principal",
            "head of",
            "lead ",  # Only if NOT intern context - but "lead analyst" is handled by finance allowlist
        ],
        # Use allow-list logic (prefer allowing entry-level titles)
        "prefer_allow_list": True,
    },
    "technology": {
        "entry_level_allow": [
            "intern",
            "internship",
            "entry level",
            "entry-level",
            "junior",
            "new grad",
            "recent grad",
            "associate",
            "0-2 years",
        ],
        "senior_reject": [
            "senior",
            "lead",
            "principal",
            "director",
            "manager",
            "head of",
            "staff",
            "architect",
        ],
        "prefer_allow_list": False,  # Tech uses reject-list logic
    },
    "consulting": {
        "entry_level_allow": [
            "analyst",
            "associate",
            "intern",
            "internship",
            "entry level",
            "business analyst",
        ],
        "senior_reject": [
            "manager",
            "director",
            "principal",
            "partner",
            "vp ",
            "vice president",
        ],
        "prefer_allow_list": True,
    },
}

# =============================================================================
# PHASE 4A: Metro Area Mapping
# =============================================================================

METRO_AREAS = {
    "Los Angeles, CA": {
        "Los Angeles",
        "Santa Monica",
        "Beverly Hills",
        "Culver City",
        "Westwood",
        "Pasadena",
        "El Segundo",
        "Venice",
        "Manhattan Beach",
        "Irvine",
        "Glendale",
        "Burbank",
        "Torrance",
        "Long Beach",
        "Redondo Beach",
        "Hermosa Beach",
    },
    "New York, NY": {
        "New York",
        "Manhattan",
        "Brooklyn",
        "Queens",
        "Bronx",
        "Staten Island",
        "Jersey City",
        "Hoboken",
        "Newark",
        "White Plains",
        "Stamford",
    },
    "San Francisco, CA": {
        "San Francisco",
        "Oakland",
        "Berkeley",
        "Palo Alto",
        "Mountain View",
        "Sunnyvale",
        "Redwood City",
        "San Mateo",
        "Foster City",
        "Burlingame",
        "Millbrae",
        "San Jose",  # Major tech hub
        "Cupertino",  # Apple
        "Santa Clara",  # Major tech hub
        "Fremont",  # East Bay tech
        "Milpitas",  # South Bay
        "Menlo Park",  # Facebook/Meta
        "San Rafael",  # North Bay
        "Sausalito",  # North Bay
        "Daly City",  # South SF
        "Redwood Shores",  # Oracle
    },
    "Chicago, IL": {
        "Chicago",
        "Evanston",
        "Oak Park",
        "Naperville",
        "Schaumburg",
        "Arlington Heights",
        "Des Plaines",
    },
    "Boston, MA": {
        "Boston",
        "Cambridge",
        "Somerville",
        "Newton",
        "Waltham",
        "Burlington",
        "Lexington",
    },
    "Seattle, WA": {
        "Seattle",
        "Bellevue",
        "Redmond",
        "Kirkland",
        "Renton",
        "Tacoma",
    },
    "Washington, DC": {
        "Washington",
        "Arlington",
        "Alexandria",
        "Bethesda",
        "Silver Spring",
        "Rockville",
    },
    "Austin, TX": {
        "Austin",
        "Round Rock",
        "Cedar Park",
        "Pflugerville",
    },
}


def _infer_job_domain(job: dict, intent_contract: dict) -> Optional[str]:
    """
    PHASE 4A: Infer the job's domain from title/description.
    Returns the primary domain if job matches user's career domains.
    """
    career_domains = intent_contract.get("career_domains", [])
    if not career_domains:
        return None
    
    job_title = (job.get("title") or "").lower()
    job_desc = (job.get("description") or "").lower()
    job_text = f"{job_title} {job_desc}".lower()
    
    # Use same domain keywords as career domain gate
    job_domain_keywords = {
        "finance_banking": [
            "investment banking", "private equity", "hedge fund", "asset management",
            "wealth management", "financial analyst", "trader", "equity research",
            "capital markets", "m&a", "mergers and acquisitions", "banking",
            "fintech", "financial services", "investment management"
        ],
        "technology": [
            "software engineer", "software developer", "developer", "programmer",
            "data scientist", "data engineer", "machine learning", "ml engineer",
            "ai engineer", "cybersecurity", "security engineer", "product manager",
            "full stack", "backend", "frontend", "swe", "sde"
        ],
        "consulting": [
            "consultant", "consulting", "advisory", "strategy consultant",
            "management consultant", "business consultant", "advisory services"
        ],
    }
    
    # Check which domain the job matches
    for domain in career_domains:
        keywords = job_domain_keywords.get(domain, [])
        for keyword in keywords:
            if keyword in job_text:
                return domain
    
    return None


def apply_hard_gate_career_domain(job: dict, intent_contract: dict) -> Tuple[bool, str]:
    """
    PHASE 2: Hard gate for career domain alignment.
    
    Rejects jobs that are in wrong career domains (e.g., Finance major seeing SWE jobs).
    Allows adjacent domains (e.g., Finance ↔ FinTech).
    
    Args:
        job: Job dict with title, description, company
        intent_contract: Normalized intent contract from normalize_intent()
        
    Returns:
        Tuple of (passes_gate: bool, reason: str)
        - passes_gate=True: Job matches career domain (allow)
        - passes_gate=False: Job in wrong domain (reject)
        - reason: Human-readable explanation
    """
    career_domains = intent_contract.get("career_domains", [])
    
    # If no career domains specified, allow all (fallback behavior)
    if not career_domains:
        return True, "no_domain_constraint"
    
    job_title = (job.get("title") or "").lower()
    job_desc = (job.get("description") or "").lower()
    job_company = (job.get("company") or "").lower()
    job_text = f"{job_title} {job_desc} {job_company}".lower()
    
    # Infer job's domain from title/description
    job_domain_keywords = {
        "finance_banking": [
            "investment banking", "private equity", "hedge fund", "asset management",
            "wealth management", "financial analyst", "trader", "equity research",
            "capital markets", "m&a", "mergers and acquisitions", "banking",
            "fintech", "financial services", "investment management"
        ],
        "technology": [
            "software engineer", "software developer", "developer", "programmer",
            "data scientist", "data engineer", "machine learning", "ml engineer",
            "ai engineer", "cybersecurity", "security engineer", "product manager",
            "full stack", "backend", "frontend", "swe", "sde"
        ],
        "consulting": [
            "consultant", "consulting", "advisory", "strategy consultant",
            "management consultant", "business consultant", "advisory services"
        ],
        "marketing": [
            "marketing", "brand manager", "digital marketing", "product marketing",
            "growth", "marketing analyst", "marketing coordinator"
        ],
        "sales": [
            "sales", "account executive", "business development", "sales representative",
            "sales manager", "account manager"
        ],
        "operations": [
            "operations", "operations analyst", "supply chain", "logistics",
            "operations manager", "process improvement"
        ],
        "healthcare": [
            "healthcare", "medical", "clinical", "hospital", "health services",
            "nursing", "physician", "healthcare administration"
        ],
        "education": [
            "education", "teaching", "teacher", "educator", "curriculum",
            "instructional design", "education coordinator"
        ],
    }
    
    # Check if job matches any of user's career domains
    job_domain_matches = []
    for domain in career_domains:
        keywords = job_domain_keywords.get(domain, [])
        for keyword in keywords:
            if keyword in job_text:
                job_domain_matches.append(domain)
                break
    
    # If job matches at least one career domain, pass
    if job_domain_matches:
        return True, f"matches_domain:{','.join(job_domain_matches)}"
    
    # Check for adjacent domains (e.g., Finance ↔ FinTech)
    adjacent_domains = {
        "finance_banking": ["technology"],  # FinTech is adjacent
        "technology": ["finance_banking"],  # FinTech is adjacent
    }
    
    for domain in career_domains:
        adjacent = adjacent_domains.get(domain, [])
        for adj_domain in adjacent:
            keywords = job_domain_keywords.get(adj_domain, [])
            # Check for cross-domain keywords (e.g., "fintech" in tech job for finance user)
            for keyword in keywords:
                if keyword in job_text and any(cross_term in job_text for cross_term in ["fintech", "financial", "banking", "investment"]):
                    return True, f"adjacent_domain:{adj_domain}"
    
    # Job doesn't match any career domain - REJECT
    return False, f"domain_mismatch:job_domain=unknown,user_domains={','.join(career_domains)}"


def apply_hard_gate_job_type(job: dict, intent_contract: dict) -> Tuple[bool, str]:
    """
    PHASE 2: Hard gate for job type alignment.
    
    Rejects jobs that don't match user's requested job types.
    Internship seekers should NEVER see full-time roles.
    
    Args:
        job: Job dict with type field
        intent_contract: Normalized intent contract
        
    Returns:
        Tuple of (passes_gate: bool, reason: str)
    """
    job_types = intent_contract.get("job_types", [])
    
    # If no job types specified, allow all (fallback behavior)
    if not job_types:
        return True, "no_type_constraint"
    
    job_type_raw = (job.get("type") or "").lower()
    job_title = (job.get("title") or "").lower()
    job_text = f"{job_type_raw} {job_title}".lower()
    
    # Normalize job type from job posting
    job_type_normalized = None
    if "intern" in job_text or "internship" in job_text or "summer analyst" in job_text or "co-op" in job_text or "coop" in job_text:
        job_type_normalized = "internship"
    elif "full-time" in job_text or "fulltime" in job_text or "full time" in job_text:
        job_type_normalized = "full-time"
    elif "entry level" in job_text or "new grad" in job_text or "entry-level" in job_text:
        job_type_normalized = "full-time"  # Entry level is full-time
    elif "part-time" in job_text or "parttime" in job_text or "part time" in job_text:
        job_type_normalized = "part-time"
    
    # If couldn't infer job type, be lenient (allow through, let scoring handle it)
    if not job_type_normalized:
        return True, "type_ambiguous"
    
    # Check if job type matches user's requested types
    if job_type_normalized in job_types:
        return True, f"type_match:{job_type_normalized}"
    
    # Hard rejection: job type doesn't match
    return False, f"type_mismatch:job_type={job_type_normalized},user_types={','.join(job_types)}"


def apply_hard_gate_location(job: dict, intent_contract: dict) -> Tuple[bool, str]:
    """
    PHASE 4A: Hard gate for location alignment with metro-aware matching.
    
    Rejects jobs that are not in user's preferred locations (unless remote).
    Now handles metro areas (e.g., Santa Monica matches Los Angeles, CA).
    
    Args:
        job: Job dict with location field
        intent_contract: Normalized intent contract
        
    Returns:
        Tuple of (passes_gate: bool, reason: str)
    """
    preferred_locations = intent_contract.get("preferred_locations", [])
    
    # If no preferred locations specified, allow all (no geographic constraint)
    if not preferred_locations:
        return True, "no_location_constraint"
    
    job_location_raw = (job.get("location") or "").strip()
    job_remote = job.get("remote", False)
    
    # Remote jobs always pass (location-agnostic)
    if job_remote or "remote" in job_location_raw.lower():
        return True, "remote_job"
    
    # PHASE 4A: Extract job city from location string
    job_city = None
    job_loc_lower = job_location_raw.lower()
    
    # Handle "City, State" format
    if "," in job_location_raw:
        job_city = job_location_raw.split(",")[0].strip()
    else:
        # Try to extract city name (first word or two)
        words = job_location_raw.split()
        if len(words) >= 1:
            job_city = words[0]  # Take first word as city
    
    # Check if job location matches any preferred location (exact or partial)
    for pref_loc in preferred_locations:
        pref_loc_lower = pref_loc.lower()
        
        # Exact match (case-insensitive)
        if job_loc_lower == pref_loc_lower:
            return True, f"location_exact_match:{pref_loc}"
        
        # PHASE 4A: Metro area matching
        if pref_loc in METRO_AREAS:
            metro_cities = METRO_AREAS[pref_loc]
            # Check if job city is in metro area
            if job_city and job_city in metro_cities:
                logger.info(f"[Location] job_city={job_city} matched metro={pref_loc}")
                return True, f"location_metro_match:job_city={job_city},metro={pref_loc}"
            
            # Check if job location mentions "Greater [Metro] Area"
            metro_name = pref_loc.split(",")[0]  # Extract city name from "City, State"
            if f"greater {metro_name.lower()} area" in job_loc_lower:
                logger.info(f"[Location] job_location={job_location_raw} matched metro={pref_loc} (Greater Area)")
                return True, f"location_metro_greater_area:metro={pref_loc}"
        
        # Partial match: "New York, NY" matches "New York" or "NYC"
        # Extract city name from "City, State" format
        if "," in pref_loc:
            pref_city = pref_loc.split(",")[0].strip().lower()
            if pref_city in job_loc_lower or job_loc_lower in pref_city:
                return True, f"location_partial_match:{pref_loc}"
        else:
            # Preferred location is just city name
            if pref_loc_lower in job_loc_lower or job_loc_lower in pref_loc_lower:
                return True, f"location_partial_match:{pref_loc}"
        
        # Handle common city abbreviations
        city_abbrevs = {
            "nyc": "new york",
            "sf": "san francisco",
            "la": "los angeles",
        }
        for abbrev, full_name in city_abbrevs.items():
            if abbrev in pref_loc_lower and full_name in job_loc_lower:
                return True, f"location_abbrev_match:{pref_loc}"
            if abbrev in job_loc_lower and full_name in pref_loc_lower:
                return True, f"location_abbrev_match:{pref_loc}"
    
    # Job location doesn't match any preferred location - REJECT
    if job_city:
        logger.warning(f"[Location][REJECT] job_city={job_city} not in metro/preferred={','.join(preferred_locations)}")
    return False, f"location_mismatch:job_location={job_location_raw},preferred={','.join(preferred_locations)}"


def apply_hard_gate_seniority(job: dict, intent_contract: dict) -> Tuple[bool, str]:
    """
    PHASE 4A: Hard gate for seniority/graduation timing alignment with domain-aware interpretation.
    
    Rejects jobs that require seniority beyond user's career phase.
    Now uses domain-specific seniority rules (e.g., "Analyst" is entry-level in finance).
    
    Args:
        job: Job dict with title field
        intent_contract: Normalized intent contract with graduation_timing
        
    Returns:
        Tuple of (passes_gate: bool, reason: str)
    """
    graduation_timing = intent_contract.get("graduation_timing", {})
    career_phase = graduation_timing.get("career_phase", "unknown")
    months_until_grad = graduation_timing.get("months_until_graduation")

    # If career phase is unknown, be lenient (allow through). Professionals
    # legitimately have no graduation timing, so months_until_grad being None
    # is NOT a reason to skip the gate anymore.
    if career_phase == "unknown" or career_phase not in ALLOWED_JOB_LEVELS:
        return True, "seniority_unknown"
    
    job_title = (job.get("title") or "").lower()
    job_desc = (job.get("description") or "").lower()
    job_text = f"{job_title} {job_desc}".lower()
    
    # PHASE 4A: Infer job domain for domain-aware seniority interpretation
    job_domain = _infer_job_domain(job, intent_contract)
    domain_rules = DOMAIN_SENIORITY_RULES.get(job_domain, {}) if job_domain else {}
    
    # PHASE 4A: Domain-aware seniority interpretation
    job_seniority = "unknown"
    interpreted_level = "unknown"
    
    # PHASE 7B.1: Check for hard block keywords FIRST (prevent false overrides)
    has_hard_block = False
    matched_hard_block_keyword = None
    # Normalize job text for matching (lowercase, strip punctuation)
    job_text_normalized = re.sub(r'[^\w\s]', ' ', job_text).lower()
    for block_keyword in SENIORITY_HARD_BLOCK_KEYWORDS:
        if block_keyword.lower() in job_text_normalized:
            has_hard_block = True
            matched_hard_block_keyword = block_keyword
            break
    
    # PHASE 7B.2: Internship/Student Override (applies to all domains, but critical for finance)
    # Force entry-level if internship/student keywords are present (unless hard block)
    has_student_override = False
    matched_student_keyword = None
    if not has_hard_block:
        student_intern_keywords = [
            "intern", "internship", "summer", "spring", "off-cycle",
            "class of 2025", "class of 2026", "class of 2027", "class of 2028", "class of 2029",
            "graduating 2025", "graduating 2026", "graduating 2027", "graduating 2028", "graduating 2029",
            "current student", "undergraduate", "masters student", "mba candidate"
        ]
        for keyword in student_intern_keywords:
            if keyword.lower() in job_text:
                has_student_override = True
                matched_student_keyword = keyword
                break
    
    # PHASE 7B.2: Finance-specific entry-level allowlist (analyst keywords are entry-level in finance)
    finance_override_applied = False
    matched_finance_keyword = None
    if job_domain == "finance_banking" and not has_hard_block and not has_student_override:
        finance_entry_keywords = [
            "analyst",
            "summer analyst",
            "investment banking analyst",
            "capital markets analyst",
            "equity research analyst",
            "corporate banking analyst",
            "transaction advisory analyst"
        ]
        for keyword in finance_entry_keywords:
            if keyword.lower() in job_text:
                finance_override_applied = True
                matched_finance_keyword = keyword
                break
    
    if domain_rules and domain_rules.get("prefer_allow_list", False):
        # Use allow-list logic (e.g., finance: prefer allowing entry-level titles)
        entry_level_allow = domain_rules.get("entry_level_allow", [])
        senior_reject = domain_rules.get("senior_reject", [])
        
        # PHASE 7B: Expanded entry-level keywords for allow-list domains too
        entry_level_keywords_expanded = [
            "entry level", "entry-level", "entrylevel",
            "new grad", "new graduate", "newgrad", "newgraduate",
            "early career", "earlycareer",
            "college graduate", "collegegraduate",
            "recent graduate", "recentgraduate",
            "class of 2025", "class of 2026", "class of 2027", "class of 2028", "class of 2029",
            "graduate program", "graduateprogram",
            "junior",  # Only when not paired with 3+ years
            "engineer i", "engineer 1", "engineer i ", "engineer 1 ",
            "analyst i", "analyst 1", "analyst i ", "analyst 1 ",
            "developer i", "developer 1", "developer i ", "developer 1 ",
            "associate",
            "0-2 years", "0-1 years", "0-3 years",
            "recent grad",
            "new hire",
            "rotational program", "rotationalprogram",
            "campus hire", "campushire"
        ]
        all_entry_level_allow = list(set(entry_level_allow + entry_level_keywords_expanded))
        
        # PHASE 7B: Check entry-level signals (override senior terms, but not hard blocks or overrides)
        is_entry_level = False
        matched_entry_keyword = None
        if not has_hard_block and not has_student_override and not finance_override_applied:
            for entry_term in all_entry_level_allow:
                if entry_term.lower() in job_text:
                    # Special handling: "junior" only if not paired with 3+ years
                    if entry_term.lower() == "junior":
                        has_experience_req = any(exp_kw in job_text for exp_kw in ["3+", "4+", "5+", "6+", "7+", "8+", "9+", "10+", "years"])
                        if has_experience_req:
                            continue
                    is_entry_level = True
                    matched_entry_keyword = entry_term
                    interpreted_level = "entry_level"
                    break
        
        # Check if title matches senior reject list (only if no entry-level signal)
        is_senior = False
        if not is_entry_level and not has_student_override and not finance_override_applied:
            for senior_term in senior_reject:
                if senior_term.lower() in job_text:
                    # Special handling: "lead" in finance might be "lead analyst" (entry) vs "lead" (senior)
                    if senior_term == "lead " and "intern" in job_text:
                        continue  # "Lead Intern" is entry-level
                    is_senior = True
                    interpreted_level = "senior"
                    break
        
        if has_hard_block:
            # PHASE 7B.1: Hard block keywords force senior classification
            job_seniority = "senior"
            interpreted_level = "senior"
            logger.warning(f"[Seniority][BLOCK] title=\"{job.get('title', '')[:50]}\" reason=explicit_senior_marker keyword=\"{matched_hard_block_keyword}\"")
        elif has_student_override:
            # PHASE 7B.2: Student/intern override forces entry-level
            job_seniority = "entry"
            interpreted_level = "entry_level"
            logger.info(f"[Seniority][STUDENT_OVERRIDE] title=\"{job.get('title', '')[:50]}\" keyword=\"{matched_student_keyword}\"")
        elif finance_override_applied:
            # PHASE 7B.2: Finance override forces entry-level
            job_seniority = "entry"
            interpreted_level = "entry_level"
            logger.info(f"[Seniority][FINANCE_OVERRIDE] title=\"{job.get('title', '')[:50]}\" keyword=\"{matched_finance_keyword}\"")
        elif is_entry_level:
            job_seniority = "entry"
            logger.info(f"[Seniority][OVERRIDE] title=\"{job.get('title', '')[:50]}\" reason=entry_level_keyword keyword=\"{matched_entry_keyword}\"")
        elif is_senior:
            job_seniority = "senior"
        else:
            # Ambiguous - check for experienced keywords
            experienced_keywords = ["experienced", "3+ years", "5+ years", "7+ years", "10+ years", "years of experience"]
            has_experienced = any(keyword in job_text for keyword in experienced_keywords)
            if has_experienced:
                job_seniority = "experienced"
                interpreted_level = "experienced"
            else:
                # Ambiguous - be lenient for allow-list domains
                interpreted_level = "ambiguous"
                return True, f"seniority_ambiguous:domain={job_domain},title={job_title[:50]}"
    else:
        # Use reject-list logic (e.g., technology: reject senior keywords)
        # PHASE 7B: Expanded entry-level keywords that override senior terms
        entry_level_keywords_expanded = [
            "entry level", "entry-level", "entrylevel",
            "new grad", "new graduate", "newgrad", "newgraduate",
            "early career", "earlycareer",
            "college graduate", "collegegraduate",
            "recent graduate", "recentgraduate",
            "class of 2025", "class of 2026", "class of 2027", "class of 2028", "class of 2029",
            "graduate program", "graduateprogram",
            "junior",  # Only when not paired with 3+ years
            "engineer i", "engineer 1", "engineer i ", "engineer 1 ",
            "analyst i", "analyst 1", "analyst i ", "analyst 1 ",
            "developer i", "developer 1", "developer i ", "developer 1 ",
            "associate",
            "0-2 years", "0-1 years", "0-3 years",
            "recent grad",
            "new hire",
            "rotational program", "rotationalprogram",
            "campus hire", "campushire"
        ]
        
        senior_keywords = domain_rules.get("senior_reject", ["senior", "lead", "principal", "director", "manager", "head of", "vp ", "vice president", "staff", "level 4", "level 5", "5+ years"])
        experienced_keywords = ["experienced", "3+ years", "5+ years", "7+ years", "10+ years", "years of experience"]
        entry_keywords = domain_rules.get("entry_level_allow", ["entry level", "entry-level", "junior", "associate", "new grad", "recent grad", "0-2 years"])
        
        # PHASE 7B: Merge expanded entry keywords with domain-specific ones
        all_entry_keywords = list(set(entry_keywords + entry_level_keywords_expanded))
        
        # PHASE 7B.2: Internship/Student Override (applies to all domains)
        # Force entry-level if internship/student keywords are present (unless hard block)
        has_student_override_tech = False
        matched_student_keyword_tech = None
        if not has_hard_block:
            student_intern_keywords = [
                "intern", "internship", "summer", "spring", "off-cycle",
                "class of 2025", "class of 2026", "class of 2027", "class of 2028", "class of 2029",
                "graduating 2025", "graduating 2026", "graduating 2027", "graduating 2028", "graduating 2029",
                "current student", "undergraduate", "masters student", "mba candidate"
            ]
            for keyword in student_intern_keywords:
                if keyword.lower() in job_text:
                    has_student_override_tech = True
                    matched_student_keyword_tech = keyword
                    break
        
        # PHASE 7B: Check for entry-level signals (override senior terms, but not hard blocks or overrides)
        has_entry_keywords = False
        matched_entry_keyword = None
        if not has_hard_block and not has_student_override_tech:
            for keyword in all_entry_keywords:
                # Use word boundaries for better matching (e.g., "junior" not in "senior")
                keyword_lower = keyword.lower()
                if keyword_lower in job_text:
                    # Special handling: "junior" only if not paired with 3+ years
                    if keyword_lower == "junior":
                        # Check if job has experience requirements
                        has_experience_req = any(exp_kw in job_text for exp_kw in ["3+", "4+", "5+", "6+", "7+", "8+", "9+", "10+", "years"])
                        if has_experience_req:
                            continue  # Skip this match - "junior" with experience = not entry-level
                    has_entry_keywords = True
                    matched_entry_keyword = keyword
                    break
        
        # Check for seniority indicators in job
        has_senior_keywords = any(keyword in job_text for keyword in senior_keywords)
        has_experienced_keywords = any(keyword in job_text for keyword in experienced_keywords)
        
        # PHASE 7B: Entry-level signals override senior terms (but hard blocks override everything)
        if has_hard_block:
            # PHASE 7B.1: Hard block keywords force senior classification
            job_seniority = "senior"
            interpreted_level = "senior"
            logger.warning(f"[Seniority][BLOCK] title=\"{job.get('title', '')[:50]}\" reason=explicit_senior_marker keyword=\"{matched_hard_block_keyword}\"")
        elif has_student_override_tech:
            # PHASE 7B.2: Student/intern override forces entry-level
            job_seniority = "entry"
            interpreted_level = "entry_level"
            logger.info(f"[Seniority][STUDENT_OVERRIDE] title=\"{job.get('title', '')[:50]}\" keyword=\"{matched_student_keyword_tech}\"")
        elif has_entry_keywords:
            job_seniority = "entry"
            interpreted_level = "entry_level"
            logger.info(f"[Seniority][OVERRIDE] title=\"{job.get('title', '')[:50]}\" reason=entry_level_keyword keyword=\"{matched_entry_keyword}\"")
        elif has_senior_keywords:
            # Only classify as senior if explicit senior signals exist AND no entry-level signals
            job_seniority = "senior"
            interpreted_level = "senior"
        elif has_experienced_keywords:
            job_seniority = "experienced"
            interpreted_level = "experienced"
        else:
            # Ambiguous - be lenient
            interpreted_level = "ambiguous"
            return True, f"seniority_ambiguous:domain={job_domain or 'unknown'},title={job_title[:50]}"
    
    # PHASE 4A: Log interpretation decision
    logger.info(f"[Seniority] domain={job_domain or 'unknown'} title=\"{job.get('title', '')[:50]}\" "
          f"→ interpreted_level={interpreted_level} job_seniority={job_seniority}")

    # Map the classification onto the canonical job levels used by
    # career_stage.ALLOWED_JOB_LEVELS, then gate BIDIRECTIONALLY: a job can
    # be rejected for being too senior (student seeing a VP role) or too
    # junior (a 15-year professional seeing a summer internship).
    intern_flavored = bool(has_student_override) or bool(
        re.search(r"\b(intern(ship)?|co[-\s]?op|summer\s+analyst)\b", job_title)
    )

    if job_seniority == "entry":
        job_level = "intern" if intern_flavored else "entry"
    elif job_seniority == "experienced":
        job_level = "mid"
    elif job_seniority == "senior":
        # Distinguish true executive roles (VP, MD, C-suite, Head of) from
        # senior IC/lead roles so senior users still see staff/principal jobs
        # while only executives see the VP tier.
        job_level = classify_job_level(job.get("title") or "")
        if job_level not in ("senior", "executive"):
            job_level = "senior"
    else:
        job_level = "unknown"

    if job_level_allowed(career_phase, job_level):
        return True, f"seniority_acceptable:career_phase={career_phase},job_level={job_level},domain={job_domain}"

    from app.services.career_stage import JOB_LEVELS as _JOB_LEVEL_ORDER
    allowed_indices = [
        _JOB_LEVEL_ORDER.index(a)
        for a in ALLOWED_JOB_LEVELS.get(career_phase, set())
        if a in _JOB_LEVEL_ORDER
    ]
    job_index = _JOB_LEVEL_ORDER.index(job_level) if job_level in _JOB_LEVEL_ORDER else None
    if job_index is not None and allowed_indices and job_index < min(allowed_indices):
        direction = "seniority_too_low"
    else:
        direction = "seniority_too_high"
    return False, f"{direction}:career_phase={career_phase},job_level={job_level},domain={job_domain},months_until_grad={months_until_grad}"


def apply_all_hard_gates(jobs: List[dict], intent_contract: dict, user_id: str = "") -> Tuple[List[dict], dict]:
    """
    PHASE 2: Apply all hard gates to a list of jobs.
    
    Filters jobs through all hard gates in order:
    1. Career domain gate
    2. Job type gate
    3. Location gate
    4. Seniority gate
    
    Args:
        jobs: List of job dicts
        intent_contract: Normalized intent contract
        user_id: User ID for logging (optional)
        
    Returns:
        Tuple of (filtered_jobs: List[dict], rejection_stats: dict)
    """
    filtered_jobs = []
    rejection_stats = {
        "career_domain": 0,
        "job_type": 0,
        "location": 0,
        "seniority": 0,
        "total_rejected": 0,
        "total_kept": 0,
        # PHASE 7B: Seniority telemetry
        "seniority_entry_keywords_seen": 0,
        "seniority_entry_keywords_misclassified": 0,
        "seniority_entry_keyword_breakdown": {},
        # PHASE 7B.1: Hard block stats
        "seniority_overrides_blocked_by_level": 0,
        # PHASE 7B.2: Finance and student override stats
        "finance_overrides_applied": 0,
        "student_overrides_applied": 0
    }
    
    for job in jobs:
        job_id = job.get("id", "unknown")
        rejected = False
        rejection_reason = None
        rejection_gate = None
        
        # Gate 1: Career Domain
        passes, reason = apply_hard_gate_career_domain(job, intent_contract)
        if not passes:
            rejected = True
            rejection_reason = reason
            rejection_gate = "career_domain"
            rejection_stats["career_domain"] += 1
            logger.warning(f"[HardGate][REJECT] user={user_id[:8] if user_id else 'unknown'}... job={job_id} gate=career_domain reason={reason} job_title=\"{job.get('title', '')[:50]}\"")
        
        # Gate 2: Job Type (only check if passed domain gate)
        if not rejected:
            passes, reason = apply_hard_gate_job_type(job, intent_contract)
            if not passes:
                rejected = True
                rejection_reason = reason
                rejection_gate = "job_type"
                rejection_stats["job_type"] += 1
                logger.warning(f"[HardGate][REJECT] user={user_id[:8] if user_id else 'unknown'}... job={job_id} gate=job_type reason={reason} job_title=\"{job.get('title', '')[:50]}\"")
        
        # Gate 3: Location (only check if passed previous gates)
        if not rejected:
            passes, reason = apply_hard_gate_location(job, intent_contract)
            if not passes:
                rejected = True
                rejection_reason = reason
                rejection_gate = "location"
                rejection_stats["location"] += 1
                logger.warning(f"[HardGate][REJECT] user={user_id[:8] if user_id else 'unknown'}... job={job_id} gate=location reason={reason} job_location=\"{job.get('location', '')[:50]}\"")
        
        # Gate 4: Seniority (only check if passed previous gates)
        # PHASE 7B: Track entry-level keywords for telemetry
        job_title_gate = (job.get("title") or "").lower()
        job_desc_gate = (job.get("description") or "").lower()
        job_text_gate = f"{job_title_gate} {job_desc_gate}".lower()
        entry_level_keywords_for_telemetry = [
            "entry level", "entry-level", "new grad", "new graduate",
            "early career", "college graduate", "recent graduate",
            "class of 2025", "class of 2026", "class of 2027", "class of 2028", "class of 2029",
            "graduate program", "junior", "engineer i", "engineer 1",
            "analyst i", "analyst 1", "developer i", "developer 1",
            "associate", "0-2 years", "recent grad", "new hire",
            "rotational program", "campus hire"
        ]
        has_entry_keyword_for_telemetry = any(keyword in job_text_gate for keyword in entry_level_keywords_for_telemetry)
        matched_entry_keyword_for_telemetry = None
        if has_entry_keyword_for_telemetry:
            for keyword in entry_level_keywords_for_telemetry:
                if keyword in job_text_gate:
                    matched_entry_keyword_for_telemetry = keyword
                    break
        
        # PHASE 7B.1: Check for hard block keywords before calling seniority gate (for stats)
        job_text_gate_normalized = re.sub(r'[^\w\s]', ' ', job_text_gate).lower()
        has_hard_block_for_stats = any(block_keyword.lower() in job_text_gate_normalized for block_keyword in SENIORITY_HARD_BLOCK_KEYWORDS)
        
        # PHASE 7B.2: Check for student/intern override keywords (for stats)
        student_intern_keywords_for_stats = [
            "intern", "internship", "summer", "spring", "off-cycle",
            "class of 2025", "class of 2026", "class of 2027", "class of 2028", "class of 2029",
            "graduating 2025", "graduating 2026", "graduating 2027", "graduating 2028", "graduating 2029",
            "current student", "undergraduate", "masters student", "mba candidate"
        ]
        has_student_override_for_stats = not has_hard_block_for_stats and any(keyword.lower() in job_text_gate for keyword in student_intern_keywords_for_stats)
        
        # PHASE 7B.2: Check for finance override keywords (for stats)
        # Need to infer job domain first
        job_domain_for_stats = None
        if intent_contract:
            job_domain_for_stats = _infer_job_domain(job, intent_contract)
        
        finance_entry_keywords_for_stats = [
            "analyst",
            "summer analyst",
            "investment banking analyst",
            "capital markets analyst",
            "equity research analyst",
            "corporate banking analyst",
            "transaction advisory analyst"
        ]
        has_finance_override_for_stats = (
            job_domain_for_stats == "finance_banking" and
            not has_hard_block_for_stats and
            not has_student_override_for_stats and
            any(keyword.lower() in job_text_gate for keyword in finance_entry_keywords_for_stats)
        )
        
        if not rejected:
            passes, reason = apply_hard_gate_seniority(job, intent_contract)
            
            # PHASE 7B.1: Track if entry-level override was blocked by hard block keywords
            if has_entry_keyword_for_telemetry and has_hard_block_for_stats:
                rejection_stats["seniority_overrides_blocked_by_level"] += 1
            
            # PHASE 7B.2: Track finance and student overrides applied
            if has_finance_override_for_stats:
                rejection_stats["finance_overrides_applied"] += 1
            if has_student_override_for_stats:
                rejection_stats["student_overrides_applied"] += 1
            if not passes:
                rejected = True
                rejection_reason = reason
                rejection_gate = "seniority"
                rejection_stats["seniority"] += 1
                
                # PHASE 7B: Track entry-level keyword misclassifications
                if has_entry_keyword_for_telemetry:
                    rejection_stats["seniority_entry_keywords_misclassified"] += 1
                    keyword_key = matched_entry_keyword_for_telemetry or "unknown"
                    rejection_stats["seniority_entry_keyword_breakdown"][keyword_key] = \
                        rejection_stats["seniority_entry_keyword_breakdown"].get(keyword_key, 0) + 1
                
                logger.warning(f"[HardGate][REJECT] user={user_id[:8] if user_id else 'unknown'}... job={job_id} gate=seniority reason={reason} job_title=\"{job.get('title', '')[:50]}\"")
        
        # PHASE 7B: Track entry-level keywords seen (regardless of rejection)
        if has_entry_keyword_for_telemetry:
            rejection_stats["seniority_entry_keywords_seen"] += 1
        
        # Keep job if passed all gates
        if not rejected:
            filtered_jobs.append(job)
            rejection_stats["total_kept"] += 1
        else:
            rejection_stats["total_rejected"] += 1
    
    # STEP 2: Gate Outcome Summary
    total_fetched = len(jobs)
    rejected_domain = rejection_stats['career_domain']
    rejected_location = rejection_stats['location']
    rejected_seniority = rejection_stats['seniority']
    rejected_type = rejection_stats['job_type']
    passed = rejection_stats['total_kept']
    
    logger.info(f"[GateSummary]")
    logger.info(f"[GateSummary] total_fetched={total_fetched}")
    logger.warning(f"[GateSummary] rejected_domain={rejected_domain}")
    logger.warning(f"[GateSummary] rejected_location={rejected_location}")
    logger.warning(f"[GateSummary] rejected_seniority={rejected_seniority}")
    logger.warning(f"[GateSummary] rejected_type={rejected_type}")
    logger.info(f"[GateSummary] passed={passed}")
    
    # PHASE 7B: Log seniority telemetry (preserved for detailed analysis)
    if rejection_stats["seniority_entry_keywords_seen"] > 0:
        logger.warning(f"[Seniority][STATS] entry_keywords_seen={rejection_stats['seniority_entry_keywords_seen']} "
              f"misclassified={rejection_stats['seniority_entry_keywords_misclassified']} "
              f"breakdown={rejection_stats['seniority_entry_keyword_breakdown']}")
    
    # PHASE 7B.1: Log hard block stats
    if rejection_stats["seniority_overrides_blocked_by_level"] > 0:
        logger.warning(f"[Seniority][STATS] overrides_blocked_by_level={rejection_stats['seniority_overrides_blocked_by_level']}")
    
    # PHASE 7B.2: Log finance and student override stats
    if rejection_stats["finance_overrides_applied"] > 0:
        logger.warning(f"[Seniority][STATS] finance_overrides_applied={rejection_stats['finance_overrides_applied']}")
    if rejection_stats["student_overrides_applied"] > 0:
        logger.warning(f"[Seniority][STATS] student_overrides_applied={rejection_stats['student_overrides_applied']}")
    
    return filtered_jobs, rejection_stats


# PHASE 6: Job Deduplication with Source Priority
# =============================================================================

# Source priority table (higher = better)
SOURCE_PRIORITY = {
    "company_site": 100,
    "greenhouse": 95,
    "lever": 95,
    "ashby": 95,
    "workday": 90,
    "linkedin": 85,
    "wellfound": 82,
    "builtin": 80,
    "indeed": 75,
    "ziprecruiter": 70,
    "simplify": 65,
    "wayup": 65,
    "glassdoor": 60,
    "other": 50
}


def get_job_source_score(job: dict) -> int:
    """
    PHASE 6: Infer job source from URL or 'via' field and return priority score.
    
    Determines source by checking:
    1. job["via"] field (from SerpAPI)
    2. job["url"] field (parsing domain)
    
    Returns:
        Integer priority score from SOURCE_PRIORITY (defaults to "other" = 50)
    """
    # Try to get source from "via" field first
    via_field = job.get("via", "").strip().lower()
    if via_field:
        # Check common patterns in via field
        if "linkedin" in via_field:
            return SOURCE_PRIORITY["linkedin"]
        elif "greenhouse" in via_field:
            return SOURCE_PRIORITY["greenhouse"]
        elif "lever" in via_field:
            return SOURCE_PRIORITY["lever"]
        elif "ashby" in via_field:
            return SOURCE_PRIORITY["ashby"]
        elif "indeed" in via_field:
            return SOURCE_PRIORITY["indeed"]
        elif "ziprecruiter" in via_field:
            return SOURCE_PRIORITY["ziprecruiter"]
        elif "glassdoor" in via_field:
            return SOURCE_PRIORITY["glassdoor"]
        elif "builtin" in via_field:
            return SOURCE_PRIORITY["builtin"]
        elif "simplify" in via_field:
            return SOURCE_PRIORITY["simplify"]
        elif "wayup" in via_field:
            return SOURCE_PRIORITY["wayup"]
        elif "wellfound" in via_field or "angel" in via_field:
            return SOURCE_PRIORITY["wellfound"]
        elif "workday" in via_field:
            return SOURCE_PRIORITY["workday"]

    # Try to infer from URL
    url = job.get("url", "").strip().lower()
    if url:
        # Parse URL to extract domain
        try:
            parsed_url = urlparse(url)
            domain = parsed_url.netloc.lower()
            
            # Remove www. prefix
            if domain.startswith("www."):
                domain = domain[4:]
            
            # Check for known sources in domain
            if "linkedin.com" in domain:
                return SOURCE_PRIORITY["linkedin"]
            elif "greenhouse.io" in domain or "boards.greenhouse.io" in domain:
                return SOURCE_PRIORITY["greenhouse"]
            elif "lever.co" in domain or "jobs.lever.co" in domain:
                return SOURCE_PRIORITY["lever"]
            elif "ashbyhq.com" in domain or "jobs.ashbyhq.com" in domain:
                return SOURCE_PRIORITY["ashby"]
            elif "myworkdayjobs.com" in domain or "wd5.myworkday.com" in domain:
                return SOURCE_PRIORITY["workday"]
            elif "indeed.com" in domain:
                return SOURCE_PRIORITY["indeed"]
            elif "ziprecruiter.com" in domain:
                return SOURCE_PRIORITY["ziprecruiter"]
            elif "glassdoor.com" in domain:
                return SOURCE_PRIORITY["glassdoor"]
            elif "builtin.com" in domain:
                return SOURCE_PRIORITY["builtin"]
            elif "simplify.jobs" in domain:
                return SOURCE_PRIORITY["simplify"]
            elif "wayup.com" in domain:
                return SOURCE_PRIORITY["wayup"]
            elif "wellfound.com" in domain or "angel.co" in domain:
                return SOURCE_PRIORITY["wellfound"]
            else:
                # Check if it looks like a company site (common patterns)
                # If URL doesn't match known aggregators, assume company site
                aggregator_domains = ["indeed", "linkedin", "glassdoor", "ziprecruiter",
                                    "monster", "careerbuilder", "dice", "simplyhired",
                                    "wellfound", "angel.co", "wayup", "builtin"]
                if not any(agg in domain for agg in aggregator_domains):
                    return SOURCE_PRIORITY["company_site"]
        except Exception:
            # Fail-safe: if URL parsing fails, continue to fallback
            pass
    
    # Default fallback
    return SOURCE_PRIORITY["other"]


def get_job_source_name(job: dict) -> str:
    """
    PHASE 6: Get human-readable source name from job.
    
    Returns the source name (key from SOURCE_PRIORITY) for logging.
    Determines source directly from job fields (similar to get_job_source_score).
    """
    # Try to get source from "via" field first
    via_field = job.get("via", "").strip().lower()
    if via_field:
        # Check common patterns in via field
        if "linkedin" in via_field:
            return "linkedin"
        elif "greenhouse" in via_field:
            return "greenhouse"
        elif "lever" in via_field:
            return "lever"
        elif "ashby" in via_field:
            return "ashby"
        elif "indeed" in via_field:
            return "indeed"
        elif "ziprecruiter" in via_field:
            return "ziprecruiter"
        elif "glassdoor" in via_field:
            return "glassdoor"
        elif "builtin" in via_field:
            return "builtin"
        elif "simplify" in via_field:
            return "simplify"
        elif "wayup" in via_field:
            return "wayup"
    
    # Try to infer from URL
    url = job.get("url", "").strip().lower()
    if url:
        try:
            parsed_url = urlparse(url)
            domain = parsed_url.netloc.lower()
            
            # Remove www. prefix
            if domain.startswith("www."):
                domain = domain[4:]
            
            # Check for known sources in domain
            if "linkedin.com" in domain:
                return "linkedin"
            elif "greenhouse.io" in domain or "boards.greenhouse.io" in domain:
                return "greenhouse"
            elif "lever.co" in domain or "jobs.lever.co" in domain:
                return "lever"
            elif "ashbyhq.com" in domain or "jobs.ashbyhq.com" in domain:
                return "ashby"
            elif "myworkdayjobs.com" in domain or "wd5.myworkday.com" in domain:
                return "workday"
            elif "indeed.com" in domain:
                return "indeed"
            elif "ziprecruiter.com" in domain:
                return "ziprecruiter"
            elif "glassdoor.com" in domain:
                return "glassdoor"
            elif "builtin.com" in domain:
                return "builtin"
            elif "simplify.jobs" in domain:
                return "simplify"
            elif "wayup.com" in domain:
                return "wayup"
            else:
                # Check if it looks like a company site
                aggregator_domains = ["indeed", "linkedin", "glassdoor", "ziprecruiter",
                                    "monster", "careerbuilder", "dice", "simplyhired",
                                    "wellfound", "angel.co", "wayup", "builtin"]
                if not any(agg in domain for agg in aggregator_domains):
                    return "company_site"
        except Exception:
            pass
    
    # Default fallback
    return "other"


def _parse_posted_date(job: dict) -> Optional[datetime]:
    """
    PHASE 6: Parse posted date from job for tie-breaking.
    
    Attempts to extract a datetime from the "posted" field.
    Returns None if parsing fails or field is missing.
    """
    posted_str = job.get("posted", "").strip()
    if not posted_str:
        return None
    
    posted_lower = posted_str.lower()
    
    # Try to parse common formats like "2 days ago", "1 week ago", etc.
    # For tie-breaking, we want more recent = better
    # Return a datetime that represents "how old" the posting is (older = earlier datetime)
    try:
        if "hour" in posted_lower or "just" in posted_lower:
            # Very recent
            return datetime.now(timezone.utc)
        elif "day" in posted_lower:
            # Extract number of days
            days_match = re.search(r'(\d+)', posted_str)
            if days_match:
                days = int(days_match.group(1))
                return datetime.now(timezone.utc) - timedelta(days=days)
        elif "week" in posted_lower:
            days_match = re.search(r'(\d+)', posted_str)
            if days_match:
                weeks = int(days_match.group(1))
                return datetime.now(timezone.utc) - timedelta(weeks=weeks)
        elif "month" in posted_lower:
            days_match = re.search(r'(\d+)', posted_str)
            if days_match:
                months = int(days_match.group(1))
                return datetime.now(timezone.utc) - timedelta(days=months * 30)
    except Exception:
        pass
    
    return None


def compute_job_fingerprint(job: dict) -> Optional[str]:
    """
    PHASE 6: Compute a deterministic fingerprint for a job.
    
    Creates a stable, source-independent fingerprint using:
    - company_name (normalized)
    - job_title (normalized)
    - address_city (normalized metro if available)
    
    Normalization:
    - Lowercase
    - Strip whitespace
    - Remove punctuation
    
    Returns:
        SHA-256 hash string of "company|title|city", or None if required fields are missing
    """
    # Extract required fields
    company_name = job.get("company", "").strip()
    job_title = job.get("title", "").strip()
    location = job.get("location", "").strip()
    
    # Fail-safe: return None if required fields are missing
    if not company_name or not job_title:
        return None
    
    # Extract city from location string
    city = None
    if location:
        # Handle "City, State" format
        if "," in location:
            city = location.split(",")[0].strip()
        else:
            # Take first word as city
            words = location.split()
            if words:
                city = words[0].strip()
    
    # Normalize city to metro if available
    normalized_city = None
    if city:
        # Check if city is in any metro area and normalize to metro name
        for metro_name, metro_cities in METRO_AREAS.items():
            if city in metro_cities:
                # Extract city name from metro (e.g., "San Francisco, CA" -> "San Francisco")
                metro_city = metro_name.split(",")[0].strip()
                normalized_city = metro_city
                break
        
        # If not in a metro area, use the city as-is
        if not normalized_city:
            normalized_city = city
    
    # Normalize inputs: lowercase, strip whitespace, remove punctuation
    def normalize_string(s: str) -> str:
        if not s:
            return ""
        # Lowercase and strip
        s = s.lower().strip()
        # Remove punctuation (keep alphanumeric and spaces)
        s = re.sub(r'[^\w\s]', '', s)
        # Normalize whitespace (multiple spaces to single space)
        s = re.sub(r'\s+', ' ', s)
        return s.strip()
    
    normalized_company = normalize_string(company_name)
    normalized_title = normalize_string(job_title)
    normalized_city_str = normalize_string(normalized_city) if normalized_city else ""
    
    # Create fingerprint: "company|title|city"
    fingerprint_string = f"{normalized_company}|{normalized_title}|{normalized_city_str}"
    
    # Generate SHA-256 hash
    fingerprint_hash = hashlib.sha256(fingerprint_string.encode('utf-8')).hexdigest()
    
    return fingerprint_hash


def deduplicate_jobs(jobs: List[dict]) -> Tuple[List[dict], dict]:
    """
    PHASE 6: Remove duplicate jobs using fingerprinting with source priority.
    
    Uses compute_job_fingerprint to identify duplicates.
    Groups jobs by fingerprint and selects the job with the best source.
    
    Selection priority:
    1. Higher source score (from SOURCE_PRIORITY)
    2. More recent posting date (if available)
    3. First seen (stable fallback)
    
    Args:
        jobs: List of job dicts (should have passed hard gates)
        
    Returns:
        Tuple of (deduplicated_jobs: List[dict], stats: dict)
        stats contains: before, after, removed, source_promotions
    """
    # Group jobs by fingerprint, tracking original order
    fingerprint_groups: Dict[str, List[Tuple[dict, int]]] = {}  # (job, original_index)
    jobs_without_fingerprint: List[dict] = []
    
    for idx, job in enumerate(jobs):
        fingerprint = compute_job_fingerprint(job)
        
        # Skip if fingerprint computation failed (missing required fields)
        if not fingerprint:
            # Fail-safe: include job if fingerprint can't be computed
            jobs_without_fingerprint.append(job)
            continue
        
        if fingerprint not in fingerprint_groups:
            fingerprint_groups[fingerprint] = []
        fingerprint_groups[fingerprint].append((job, idx))
    
    deduplicated_jobs: List[dict] = []
    removed_count = 0
    source_promotions = 0
    
    stats = {
        "before": len(jobs),
        "after": 0,
        "removed": 0,
        "source_promotions": 0
    }
    
    # Process each fingerprint group
    for fingerprint, group_jobs_with_indices in fingerprint_groups.items():
        if len(group_jobs_with_indices) == 1:
            # No duplicates for this fingerprint
            deduplicated_jobs.append(group_jobs_with_indices[0][0])
            continue
        
        # Multiple jobs with same fingerprint - select the best one
        # Sort by: source score (desc), posting date (desc, more recent = better), original order (asc, first seen = better)
        def job_sort_key(job_with_idx: Tuple[dict, int]) -> Tuple[int, float, int]:
            job, original_idx = job_with_idx
            source_score = get_job_source_score(job)
            posted_date = _parse_posted_date(job)
            # Use negative source_score for descending order
            # Use negative timestamp so more recent (larger datetime) comes first
            # Use original order for tie-breaking (ascending = first seen wins)
            if posted_date:
                date_key = -posted_date.timestamp()  # More recent = higher timestamp = better
            else:
                date_key = float('inf')  # No date = sort last
            return (-source_score, date_key, original_idx)
        
        # Sort to find the best job
        sorted_jobs_with_indices = sorted(group_jobs_with_indices, key=job_sort_key)
        winner, winner_idx = sorted_jobs_with_indices[0]
        losers_with_indices = sorted_jobs_with_indices[1:]
        
        # Get source names for logging
        winner_source = get_job_source_name(winner)
        winner_score = get_job_source_score(winner)
        
        # Check if there was a source promotion (winner has higher score than at least one loser)
        # Find the highest-scoring loser for logging
        promoted = False
        best_loser_source = None
        best_loser_score = -1
        
        for loser, loser_idx in losers_with_indices:
            loser_source = get_job_source_name(loser)
            loser_score = get_job_source_score(loser)
            
            if winner_score > loser_score:
                promoted = True
                # Track the best loser (highest score) for logging
                if loser_score > best_loser_score:
                    best_loser_score = loser_score
                    best_loser_source = loser_source
        
        # Log once per duplicate group if there was a promotion
        if promoted and best_loser_source:
            job_title = winner.get("title", "Unknown")
            logger.info(f"[Dedup][SOURCE] fingerprint={fingerprint[:8]} winner={winner_source} loser={best_loser_source} title=\"{job_title}\"")
            source_promotions += 1
        
        # Add winner
        deduplicated_jobs.append(winner)
        removed_count += len(losers_with_indices)
    
    # Add jobs without fingerprints (fail-safe)
    deduplicated_jobs.extend(jobs_without_fingerprint)
    
    stats["after"] = len(deduplicated_jobs)
    stats["removed"] = removed_count
    stats["source_promotions"] = source_promotions
    
    # Log summary statistics
    logger.info(f"[Dedup][STATS] before={stats['before']} after={stats['after']} removed={stats['removed']} source_promotions={stats['source_promotions']}")
    
    return deduplicated_jobs, stats


def score_job_for_user(job: dict, user_profile: dict, query_weight: float = 1.0) -> int:
    """
    PHASE 2: Calculate a match score for a job based on user's profile.
    
    IMPORTANT: This function assumes all jobs have already passed hard intent gates
    (career domain, job type, location, seniority). Scoring is for RANKING only,
    not for filtering. If a job reaches this function, it is already acceptable.
    
    Scoring breakdown (100 points max):
    - Base relevance: 20 points (having a profile) [PHASE 2: Removed job type check - now 20 points]
    - Field/Major affinity: 20 points (semantic matching) [Note: Domain validated by hard gate, this is for ranking]
    - Skills match: 30 points (with synonym expansion)
    - Experience relevance: 15 points
    - Additional signals: 15 points (extracurriculars, interests, timing)
    
    Args:
        job: Job dict with title, description, requirements (must have passed hard gates)
        user_profile: User's career profile
        query_weight: Multiplier based on query source (1.0-1.2)
        
    Returns:
        Match score 0-100
    """
    score = 0.0
    
    job_title = (job.get("title") or "").lower()
    job_desc = (job.get("description") or "").lower()
    job_reqs = " ".join(job.get("requirements") or []).lower()
    job_company = (job.get("company") or "").lower()
    job_text = f"{job_title} {job_desc} {job_reqs} {job_company}"
    
    # 1. BASE RELEVANCE (20 points max)
    # PHASE 2: Removed job type check (now handled by hard gate)
    has_profile = bool(user_profile.get("major") or user_profile.get("skills"))
    if has_profile:
        score += 20.0  # PHASE 2: Full 20 points since job type already validated by hard gate
    
    # 2. FIELD/MAJOR AFFINITY (20 points)
    # PHASE 2: Note - career domain is now validated by hard gate, this is for ranking within acceptable domain
    major = user_profile.get("major") or ""
    field_affinity = calculate_field_affinity(major, job_title, job_desc)
    score += field_affinity * 20
    
    # 3. SKILLS MATCH (30 points)
    skills = user_profile.get("skills", [])
    if skills:
        skill_points = 0.0
        generic_terms = {"strong", "excellent", "good", "experience", "skills", "ability", 
                        "abilities", "working", "team", "work", "looking", "candidates"}
        
        title_matches = 0
        desc_matches = 0
        
        for skill in skills[:15]:
            if not skill:
                continue
            skill_lower = skill.lower().strip()
            if len(skill_lower) < 3 or skill_lower in generic_terms:
                continue
            
            matched_title, conf_title = semantic_skill_match(skill, job_title)
            if matched_title:
                title_matches += 1
                skill_points += 6.0 * conf_title
                continue
            
            matched_desc, conf_desc = semantic_skill_match(skill, job_text)
            if matched_desc and conf_desc >= 0.8:
                desc_matches += 1
                skill_points += 3.0 * conf_desc
        
        total_matches = title_matches + desc_matches
        if total_matches > 0:
            skill_points *= (1 + 0.03 * min(total_matches, 5))
        
        score += min(skill_points, 30.0)
    
    # 4. EXPERIENCE RELEVANCE (15 points)
    experiences = user_profile.get("experiences", [])
    if experiences:
        exp_score = 0.0
        for exp in experiences[:5]:
            exp_title = (exp.get("title") or "").lower()
            exp_keywords = exp.get("keywords", [])
            
            title_words = [w for w in exp_title.split() if len(w) > 3]
            for word in title_words:
                if word in job_title:
                    exp_score += 4.0
                    break
            
            for kw in exp_keywords[:5]:
                if not kw:
                    continue
                matched, conf = semantic_skill_match(kw, job_text)
                if matched:
                    exp_score += 1.5 * conf
        
        score += min(15.0, exp_score)
    
    # 5. ADDITIONAL SIGNALS (15 points)
    additional_score = 0.0
    
    # Extracurriculars (6 points max)
    extracurriculars = user_profile.get("extracurriculars", [])
    if extracurriculars:
        ec_matches = 0
        for ec in extracurriculars[:5]:
            ec_name = (ec.get('name') or "")
            ec_role = (ec.get('role') or "")
            ec_desc = (ec.get('description') or "")
            ec_text = f"{ec_name} {ec_role} {ec_desc}".lower()
            ec_words = [w for w in ec_text.split() if len(w) > 3]
            for word in ec_words[:10]:
                if word in job_text:
                    ec_matches += 1
                    break
        additional_score += min(6, ec_matches * 2)
    
    # Interests (4 points max)
    interests = user_profile.get("interests", [])
    interest_score = sum(1.5 for interest in interests[:5] if interest and (interest.lower() in job_text))
    additional_score += min(4, interest_score)
    
    # Industry match (3 points)
    target_industries = user_profile.get("target_industries", [])
    industry_keywords = {
        "technology": ["software", "tech", "developer", "engineering"],
        "finance": ["financial", "banking", "investment", "analyst"],
        "consulting": ["consultant", "advisory", "strategy"],
        "healthcare": ["health", "medical", "clinical"],
    }
    for industry in target_industries[:3]:
        industry_lower = industry.lower()
        if industry_lower in job_text:
            additional_score += 3
            break
        if industry_lower in industry_keywords and any(kw in job_text for kw in industry_keywords[industry_lower]):
            additional_score += 2
            break
    
    # Graduation timing (2 points)
    # PHASE 2: Keep timing scoring but note that seniority is now validated by hard gate
    # This is for ranking within acceptable jobs (e.g., internship seeker sees multiple internships, this helps prioritize)
    grad_year = user_profile.get("graduation_year")
    if grad_year:
        current_year = datetime.now().year
        years_to_grad = grad_year - current_year
        job_type = (job.get("type") or "").lower()
        
        if years_to_grad <= 0 and ("new grad" in job_text or "entry level" in job_text or job_type == "full-time"):
            additional_score += 2
        elif years_to_grad == 1 and ("internship" in job_text or "new grad" in job_text):
            additional_score += 2
        elif years_to_grad > 1 and ("internship" in job_text or job_type == "internship"):
            additional_score += 2
    
    score += min(15.0, additional_score)
    
    # Apply query weight and return
    score = score * query_weight
    return max(0, min(100, int(round(score))))


def calculate_quality_score(job: dict) -> int:
    """
    Calculate a quality score for a job based on various signals.
    
    Quality factors (0-50 points):
    - Description quality: 0-15 points
    - Source quality: 0-10 points
    - Company quality: 0-15 points
    - Recency: 0-10 points
    
    Args:
        job: Job dict
        
    Returns:
        Quality score 0-50
    """
    score = 0
    
    # 1. DESCRIPTION QUALITY (0-15 points)
    description = job.get("description", "")
    
    # Length bonus
    if len(description) > 500:
        score += 8
    elif len(description) > 300:
        score += 5
    elif len(description) < 100:
        score -= 5  # Penalty for too short
    
    # Structure bonus - has clear sections
    if any(section in description.lower() for section in ["requirements", "qualifications", "responsibilities", "benefits"]):
        score += 4
    
    # Has salary information
    if job.get("salary") or "$" in description or "salary" in description.lower():
        score += 3
    
    # Spam detection (penalty)
    desc_lower = description.lower()
    if any(spam in desc_lower for spam in SPAM_KEYWORDS):
        score -= 10
    
    # 2. SOURCE QUALITY (0-10 points)
    via = job.get("via", "").lower()
    
    if any(source in via for source in HIGH_QUALITY_SOURCES):
        score += 10
    elif any(pattern in via for pattern in ["recruiter", "staffing", "agency"]):
        score -= 5  # Penalty for generic recruiters
    else:
        score += 3  # Neutral source
    
    # 3. COMPANY QUALITY (0-15 points)
    company = job.get("company", "").lower()
    
    # Check against top company lists
    if any(top_co in company for top_co in TOP_TECH_COMPANIES):
        score += 15
    elif any(top_co in company for top_co in TOP_FINANCE_COMPANIES):
        score += 15
    elif any(top_co in company for top_co in TOP_CONSULTING_COMPANIES):
        score += 15
    elif any(f500 in company for f500 in FORTUNE_500_KEYWORDS):
        score += 12
    elif any(pattern in company for pattern in LOW_QUALITY_COMPANY_PATTERNS):
        score -= 10  # Penalty for generic/staffing companies
    else:
        score += 5  # Neutral company
    
    # 4. RECENCY (0-10 points)
    posted = job.get("posted", "").lower()
    
    try:
        if "hour" in posted or "just" in posted or "today" in posted:
            score += 10  # Very fresh
        elif "day" in posted:
            days = int(''.join(filter(str.isdigit, posted.split("day")[0])) or "0")
            if days <= 3:
                score += 10
            elif days <= 7:
                score += 8
            elif days <= 14:
                score += 5
            else:
                score += 2
        elif "week" in posted:
            weeks = int(''.join(filter(str.isdigit, posted.split("week")[0])) or "0")
            if weeks <= 2:
                score += 5
            else:
                score += 1
        elif "month" in posted:
            score -= 3  # Old posting penalty
    except (ValueError, IndexError):
        score += 3  # Unknown recency, neutral
    
    return max(0, min(50, score))


def _parse_job_age_days(job: dict) -> Optional[int]:
    """
    Parse the job's posted date and return age in days.
    
    Args:
        job: Job dict with 'posted' field
        
    Returns:
        Age in days, or None if cannot be parsed
    """
    posted = job.get("posted", "").lower()
    if not posted:
        return None
    
    try:
        if "hour" in posted or "just" in posted or "today" in posted:
            return 0
        elif "day" in posted:
            days_str = ''.join(filter(str.isdigit, posted.split("day")[0]))
            days = int(days_str) if days_str else 0
            return days
        elif "week" in posted:
            weeks_str = ''.join(filter(str.isdigit, posted.split("week")[0]))
            weeks = int(weeks_str) if weeks_str else 0
            return weeks * 7
        elif "month" in posted:
            months_str = ''.join(filter(str.isdigit, posted.split("month")[0]))
            months = int(months_str) if months_str else 0
            return months * 30
        else:
            return None
    except (ValueError, IndexError):
        return None


def is_job_quality_acceptable(job: dict, min_quality_score: int = 0) -> bool:
    """
    Check if a job meets minimum quality standards.
    
    Args:
        job: Job dict
        min_quality_score: Minimum quality score threshold
        
    Returns:
        True if job is acceptable, False if it should be filtered out
    """
    company = job.get("company", "").lower()
    description = job.get("description", "")
    
    # Hard filters - always exclude these
    
    # 1. Generic/placeholder company names
    if company in ["company", "employer", "organization", "confidential", ""]:
        return False
    
    # 2. Low-quality company patterns
    if any(pattern in company for pattern in LOW_QUALITY_COMPANY_PATTERNS):
        # Allow if description is high quality (might be legit staffing for good company)
        if len(description) < 300:
            return False
    
    # 3. Too short descriptions
    if len(description) < 50:
        return False
    
    # 4. Spam keywords
    desc_lower = description.lower()
    if any(spam in desc_lower for spam in SPAM_KEYWORDS):
        return False
    
    # 5. Recency filter - remove jobs older than MAX_JOB_AGE_DAYS
    job_age_days = _parse_job_age_days(job)
    if job_age_days is not None and job_age_days > MAX_JOB_AGE_DAYS:
        return False
    
    # 6. Quality score threshold
    if min_quality_score > 0:
        quality_score = calculate_quality_score(job)
        if quality_score < min_quality_score:
            return False
    
    return True


def filter_jobs_by_quality(jobs: List[dict], min_quality_score: int = MIN_QUALITY_SCORE) -> List[dict]:
    """
    Filter out low-quality jobs.
    
    Args:
        jobs: List of job dicts
        min_quality_score: Minimum quality score to keep (default 10)
        
    Returns:
        Filtered list of jobs
    """
    filtered = []
    removed_count = 0
    removed_for_recency = 0
    
    for job in jobs:
        # Check recency separately for logging
        job_age_days = _parse_job_age_days(job)
        if job_age_days is not None and job_age_days > MAX_JOB_AGE_DAYS:
            removed_count += 1
            removed_for_recency += 1
            continue
        
        if is_job_quality_acceptable(job, min_quality_score):
            filtered.append(job)
        else:
            removed_count += 1
    
    if removed_count > 0:
        logger.info(f"[JobBoard] Filtered out {removed_count} low-quality jobs (recency: {removed_for_recency}, quality: {removed_count - removed_for_recency})")
    
    return filtered


def score_jobs_by_resume_match(
    jobs: List[dict],
    user_profile: dict,
    query_weights: dict = None,
    user_id: str = "",
) -> List[dict]:
    """
    Score and rank all jobs based on user profile match AND quality.

    Combined scoring:
    - Resume Match Score: 0-100 (how well job matches user's profile)
    - Quality Score: 0-50 (job posting quality signals)
    - Phase 2 boost: dream/target/alumni/saved-affinity (folded into combined_score)
    - Final Score: Weighted combination with match prioritized, plus signal boost

    Args:
        jobs: List of job dicts
        user_profile: User's career profile
        query_weights: Optional dict mapping job_id to query weight
        user_id: Firebase uid — required to load per-user signals (Phase 2).
                 If omitted, signal boosts are skipped (legacy callers).

    Returns:
        List of jobs with matchScore, qualityScore, combinedScore, and
        matchSignals added, sorted by combined score descending.
    """
    # Phase 2: load per-user signals once and reuse for every job in the
    # batch. UserSignals.boost() returns (score_delta, [reason_codes]).
    signals = None
    if user_id:
        try:
            from app.services.job_ranker_signals import load_user_signals
            signals = load_user_signals(user_id)
        except Exception:
            logger.exception("[JobBoard] Failed to load user signals; scoring without boosts")

    # Check if profile is empty or has no meaningful data
    has_profile_data = (
        user_profile and
        (
            (user_profile.get("major") and user_profile.get("major").strip()) or
            (user_profile.get("skills") and len(user_profile.get("skills", [])) > 0) or
            (user_profile.get("extracurriculars") and len(user_profile.get("extracurriculars", [])) > 0) or
            (user_profile.get("experiences") and len(user_profile.get("experiences", [])) > 0) or
            (user_profile.get("interests") and len(user_profile.get("interests", [])) > 0) or
            (user_profile.get("target_industries") and len(user_profile.get("target_industries", [])) > 0)
        )
    )

    if not has_profile_data:
        # No profile or empty profile, assign neutral match scores but still
        # calculate quality + signal boost. A user with no resume but a dream
        # companies list should still see those companies ranked first.
        logger.info("[JobBoard] No user profile data found, using neutral scores")
        for job in jobs:
            boost, reasons = (signals.boost(job) if signals else (0, []))
            quality_score = calculate_quality_score(job)
            job["matchScore"] = 50
            job["qualityScore"] = quality_score
            job["combinedScore"] = round(50 + (quality_score * 0.5) + boost, 1)
            job["matchSignals"] = reasons
        jobs.sort(key=lambda x: x.get("combinedScore", 0), reverse=True)
        return jobs

    query_weights = query_weights or {}

    for job in jobs:
        weight = query_weights.get(job.get("id"), 1.0)

        # Calculate base scores (unchanged — kept inside 0-100 / 0-50 ranges
        # for the UI display).
        match_score = score_job_for_user(job, user_profile, weight)
        quality_score = calculate_quality_score(job)

        # Phase 2 signal boost applied at the combined_score level so the
        # matchScore display stays in its familiar 0-100 range, but a
        # dream-company job reliably outranks a strong-keyword non-dream job.
        boost, reasons = (signals.boost(job) if signals else (0, []))

        combined_score = (match_score * 0.7) + (quality_score * 0.6) + boost

        job["matchScore"] = match_score
        job["qualityScore"] = quality_score
        job["combinedScore"] = round(combined_score, 1)
        job["matchSignals"] = reasons

    jobs.sort(key=lambda x: x.get("combinedScore", 0), reverse=True)

    return jobs


# =============================================================================
# DATA SANITIZATION HELPERS
# =============================================================================

def is_document_reference(obj):
    """Check if an object is a Firestore DocumentReference."""
    if obj is None:
        return False
    # Check by type name
    obj_type = str(type(obj))
    if 'DocumentReference' in obj_type or 'Reference' in obj_type:
        return True
    # Check by attributes
    if hasattr(obj, 'path') and hasattr(obj, 'id'):
        # DocumentReference has both path and id
        return True
    # Check by module
    if hasattr(obj, '__class__') and hasattr(obj.__class__, '__module__'):
        module = obj.__class__.__module__
        if module and 'firestore' in module.lower() and 'reference' in obj_type.lower():
            return True
    return False

def sanitize_firestore_data(obj, depth=0, max_depth=10):
    """
    Recursively convert Firestore-specific types to JSON-serializable types.
    Optimized: Removed verbose logging to improve performance.
    """
    # Prevent infinite recursion
    if depth > max_depth:
        return str(obj) if obj is not None else None
    
    if obj is None:
        return None
    elif isinstance(obj, str):
        return obj
    elif isinstance(obj, (int, float, bool)):
        return obj
    
    # Check for DocumentReference FIRST - before other checks
    # This is critical because DocumentReference can appear anywhere
    if is_document_reference(obj):
        try:
            if hasattr(obj, 'path'):
                return str(obj.path)
            elif hasattr(obj, 'id'):
                return str(obj.id)
            else:
                return str(obj)
        except Exception as e:
            # Only log actual errors, not normal conversions
            logger.error(f"[JobBoard] Error converting DocumentReference: {e}")
            return None
    
    elif isinstance(obj, dict):
        return {str(k): sanitize_firestore_data(v, depth+1, max_depth) for k, v in obj.items()}
    elif isinstance(obj, (list, tuple)):
        return [sanitize_firestore_data(item, depth+1, max_depth) for item in obj]
    elif hasattr(obj, 'isoformat'):  # datetime
        try:
            return obj.isoformat()
        except:
            return str(obj)
    elif hasattr(obj, 'latitude'):  # GeoPoint
        try:
            return {"lat": obj.latitude, "lng": obj.longitude}
        except:
            return str(obj)
    elif hasattr(obj, '_pb'):  # Firestore protobuf types
        return str(obj)
    elif hasattr(obj, '__class__'):
        class_name = str(type(obj))
        if 'firestore' in class_name.lower() or 'DocumentReference' in class_name:
            # Catch any Firestore types
            try:
                if hasattr(obj, 'path'):
                    return str(obj.path)
                return str(obj)
            except:
                return None
    # Last resort: try to convert to string
    try:
        return str(obj)
    except:
        return None

# =============================================================================
# HELPER FUNCTIONS
# =============================================================================

def extract_relevant_resume_data_for_cover_letter(resume: Dict[str, Any], max_length_per_field: int = 500) -> Dict[str, Any]:
    """
    Extract only relevant resume sections for cover letter generation.
    This significantly reduces prompt size and improves generation speed.
    
    Args:
        resume: Full resume dictionary
        max_length_per_field: Maximum characters for text fields
        
    Returns:
        Optimized resume dictionary with only relevant sections
    """
    if not isinstance(resume, dict):
        return {}
    
    relevant = {}
    
    # Always include name
    if resume.get("name"):
        relevant["name"] = str(resume["name"])
    # Raw resume text (2026-09-08): present when the user has no structured
    # resumeParsed. Carried into the prompt so the letter still speaks from
    # the real resume instead of an empty experience list.
    if resume.get("raw_text") and not (resume.get("experience") or resume.get("education")):
        relevant["raw_text"] = str(resume["raw_text"])[:6000]
    
    # Extract top education entries (max 2)
    if resume.get("education"):
        education = resume["education"]
        if isinstance(education, list):
            relevant["education"] = education[:2]
        elif isinstance(education, dict):
            # Handle single education dict
            edu_copy = education.copy()
            if "coursework" in edu_copy and isinstance(edu_copy["coursework"], list):
                edu_copy["coursework"] = edu_copy["coursework"][:10]  # Limit coursework
            relevant["education"] = [edu_copy]
    
    # Extract top experiences (max 5)
    if resume.get("experience"):
        experience = resume.get("experience", [])
        if isinstance(experience, list):
            cleaned_experience = []
            for exp in experience[:5]:
                if isinstance(exp, dict):
                    exp_copy = exp.copy()
                    # Truncate long descriptions
                    if "description" in exp_copy:
                        desc = str(exp_copy["description"])
                        if len(desc) > max_length_per_field:
                            exp_copy["description"] = desc[:max_length_per_field] + "..."
                    if "bullets" in exp_copy and isinstance(exp_copy["bullets"], list):
                        # Limit bullets per experience
                        exp_copy["bullets"] = exp_copy["bullets"][:8]
                    cleaned_experience.append(exp_copy)
            relevant["experience"] = cleaned_experience
    
    # Extract top projects (max 3)
    if resume.get("projects"):
        projects = resume.get("projects", [])
        if isinstance(projects, list):
            cleaned_projects = []
            for proj in projects[:3]:
                if isinstance(proj, dict):
                    proj_copy = proj.copy()
                    if "description" in proj_copy:
                        desc = str(proj_copy["description"])
                        if len(desc) > max_length_per_field:
                            proj_copy["description"] = desc[:max_length_per_field] + "..."
                    cleaned_projects.append(proj_copy)
            relevant["projects"] = cleaned_projects
    
    # Extract skills (limit to top 20-25 skills total)
    if resume.get("skills"):
        skills = resume.get("skills", {})
        if isinstance(skills, dict):
            cleaned_skills = {}
            for skill_category, skill_list in skills.items():
                if isinstance(skill_list, list):
                    cleaned_skills[skill_category] = skill_list[:15]  # Limit per category
            relevant["skills"] = cleaned_skills
        elif isinstance(skills, list):
            relevant["skills"] = {"all": skills[:25]}
    
    # Extract summary/objective (truncate)
    if resume.get("summary"):
        summary = str(resume["summary"])
        relevant["summary"] = summary[:max_length_per_field] + "..." if len(summary) > max_length_per_field else summary
    
    if resume.get("objective"):
        objective = str(resume["objective"])
        relevant["objective"] = objective[:max_length_per_field] + "..." if len(objective) > max_length_per_field else objective
    
    # Extract contact info (minimal)
    if resume.get("contact"):
        contact = resume.get("contact", {})
        if isinstance(contact, dict):
            relevant["contact"] = {
                "email": contact.get("email"),
                "location": contact.get("location")
            }
    
    return {k: v for k, v in relevant.items() if v}  # Remove empty values


def get_or_cache_sanitized_resume(user_id: str, raw_resume: Dict[str, Any], db) -> Optional[str]:
    """
    Get sanitized resume from cache or generate and cache it.
    This avoids repeated expensive sanitization operations.
    
    Args:
        user_id: User ID
        raw_resume: Raw resume dictionary from Firestore
        db: Firestore database instance
        
    Returns:
        JSON string of sanitized resume, or None if caching disabled
    """
    try:
        user_ref = db.collection("users").document(user_id)
        user_doc = user_ref.get()
        
        if not user_doc.exists:
            return None
        
        user_data = user_doc.to_dict()
        
        # Generate hash of raw resume for cache validation
        resume_json_str = json.dumps(raw_resume, sort_keys=True, default=str)
        resume_hash = hashlib.md5(resume_json_str.encode()).hexdigest()
        
        # Check if we have cached sanitized resume with matching hash
        cached = user_data.get("resumeParsedSanitized")
        cached_hash = user_data.get("resumeParsedHash")
        
        if cached and cached_hash == resume_hash and isinstance(cached, str):
            logger.info(f"[JobBoard]  Using cached sanitized resume (hash: {resume_hash[:8]}...)")
            return cached
        
        # Generate and cache sanitized resume
        logger.info(f"[JobBoard] Generating new sanitized resume (hash: {resume_hash[:8]}...)")
        sanitized = sanitize_firestore_data(raw_resume, depth=0, max_depth=20)
        
        # Custom JSON encoder that handles DocumentReferences
        def json_default(obj):
            """Custom default handler for json.dumps that catches DocumentReferences."""
            if is_document_reference(obj):
                try:
                    if hasattr(obj, 'path'):
                        return str(obj.path)
                    elif hasattr(obj, 'id'):
                        return str(obj.id)
                    return str(obj)
                except:
                    return None
            try:
                return str(obj)
            except:
                return None
        
        resume_json = json.dumps(sanitized, default=json_default, ensure_ascii=False)
        
        # Cache the result
        try:
            user_ref.update({
                "resumeParsedSanitized": resume_json,
                "resumeParsedHash": resume_hash
            })
            logger.info(f"[JobBoard]  Cached sanitized resume")
        except Exception as cache_error:
            logger.error(f"[JobBoard]  Failed to cache sanitized resume: {cache_error}")
            # Continue anyway, just return the JSON
        
        return resume_json
        
    except Exception as e:
        logger.error(f"[JobBoard] Error in get_or_cache_sanitized_resume: {e}")
        return None


def parse_job_url(url: str) -> Optional[Dict[str, Any]]:
    """
    Scrape job details from a URL.
    """
    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Upgrade-Insecure-Requests": "1"
        }
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        
        soup = BeautifulSoup(response.text, "html.parser")
        
        job_data = {
            "title": None,
            "company": None,
            "location": None,
            "description": None,
            "requirements": [],
        }
        
        # Try to extract metadata from structured data (JSON-LD, microdata, meta tags)
        # Many job sites use structured data for SEO - this works even if HTML parsing fails
        try:
            json_ld_scripts = soup.find_all('script', type='application/ld+json')
            for script in json_ld_scripts:
                try:
                    data = json.loads(script.string)
                    if isinstance(data, dict):
                        # Look for job posting schema
                        if data.get('@type') == 'JobPosting' or 'JobPosting' in str(data.get('@type', '')):
                            if 'hiringOrganization' in data:
                                org = data['hiringOrganization']
                                if isinstance(org, dict) and not job_data.get("company"):
                                    job_data["company"] = org.get('name', '')
                            if 'title' in data and not job_data.get("title"):
                                job_data["title"] = data['title']
                            if 'jobLocation' in data:
                                loc = data['jobLocation']
                                if isinstance(loc, dict):
                                    address = loc.get('address', {})
                                    if isinstance(address, dict) and not job_data.get("location"):
                                        locality = address.get('addressLocality', '')
                                        region = address.get('addressRegion', '')
                                        if locality and region:
                                            job_data["location"] = f"{locality}, {region}"
                                        elif locality:
                                            job_data["location"] = locality
                            if 'description' in data and not job_data.get("description"):
                                job_data["description"] = data['description'][:5000]
                except (json.JSONDecodeError, AttributeError):
                    continue
        except Exception as e:
            logger.error(f"[JobBoard] Error extracting JSON-LD data: {e}")
        
        # Try meta tags (og:title, og:description, etc.) as fallback
        if not job_data.get("title"):
            meta_title = soup.find('meta', property='og:title')
            if meta_title and meta_title.get('content'):
                job_data["title"] = meta_title.get('content')
        
        if not job_data.get("description"):
            meta_desc = soup.find('meta', property='og:description')
            if meta_desc and meta_desc.get('content'):
                job_data["description"] = meta_desc.get('content')[:5000]
        
        # Greenhouse parsing (boards.greenhouse.io or jobs.greenhouse.io)
        if "greenhouse.io" in url:
            logger.info(f"[JobBoard] Parsing Greenhouse URL: {url}")
            # Greenhouse has a JSON API: append .json to the job URL
            # URL pattern: https://boards.greenhouse.io/company/jobs/12345
            try:
                gh_match = re.search(r'greenhouse\.io/([^/]+)/jobs/(\d+)', url)
                if gh_match:
                    gh_company_slug = gh_match.group(1)
                    gh_job_id = gh_match.group(2)
                    json_url = f"https://boards-api.greenhouse.io/v1/boards/{gh_company_slug}/jobs/{gh_job_id}"
                    logger.info(f"[JobBoard] Fetching Greenhouse JSON API: {json_url}")
                    gh_resp = requests.get(json_url, headers={"Accept": "application/json"}, timeout=10)
                    if gh_resp.status_code == 200:
                        gh_data = gh_resp.json()
                        if gh_data.get('title'):
                            job_data["title"] = gh_data['title']
                        if gh_data.get('location', {}).get('name'):
                            job_data["location"] = gh_data['location']['name']
                        # Extract company from departments or metadata
                        if not job_data.get("company"):
                            # Use the slug as company name (capitalize)
                            job_data["company"] = gh_company_slug.replace("-", " ").title()
                        # Description is HTML in the 'content' field
                        if gh_data.get('content'):
                            desc_soup = BeautifulSoup(gh_data['content'], 'html.parser')
                            job_data["description"] = desc_soup.get_text(strip=True)[:5000]
                        logger.info(f"[JobBoard] Greenhouse API success: title={job_data.get('title')}, company={job_data.get('company')}")
            except Exception as gh_err:
                logger.warning(f"[JobBoard] Greenhouse API fallback failed: {gh_err}")
            # If API didn't work, try HTML selectors
            if not job_data.get("title"):
                title_elem = soup.select_one("h1.app-title, h1[class*='title'], .job__title h1, h1")
                if title_elem:
                    job_data["title"] = title_elem.get_text(strip=True)
            if not job_data.get("description"):
                desc_elem = soup.select_one("#content, .job__description, [class*='description'], .content")
                if desc_elem:
                    desc_text = desc_elem.get_text(strip=True)
                    if desc_text and len(desc_text) > 50:
                        job_data["description"] = desc_text[:5000]

        # Lever parsing (jobs.lever.co)
        elif "lever.co" in url:
            logger.info(f"[JobBoard] Parsing Lever URL: {url}")
            # Lever serves server-rendered HTML with predictable selectors
            title_elem = soup.select_one("h2.posting-headline .posting-title, h2[class*='posting-headline'], .posting-headline h2, h2")
            if title_elem:
                job_data["title"] = title_elem.get_text(strip=True)

            # Company name from the page header
            company_elem = soup.select_one(".main-header-logo img, a.main-header-logo, .posting-categories .posting-category:first-child, .main-header .company-name")
            if company_elem:
                # Try alt text from logo image first
                if company_elem.name == 'img' and company_elem.get('alt'):
                    job_data["company"] = company_elem['alt'].replace(' Logo', '').replace(' logo', '').strip()
                else:
                    job_data["company"] = company_elem.get_text(strip=True)

            # If no company from selectors, extract from URL (jobs.lever.co/company-name/...)
            if not job_data.get("company"):
                lever_match = re.search(r'lever\.co/([^/]+)', url)
                if lever_match:
                    job_data["company"] = lever_match.group(1).replace("-", " ").title()

            # Location from posting categories
            location_elem = soup.select_one(".posting-categories .location, .sort-by-commitment, [class*='location']")
            if location_elem:
                job_data["location"] = location_elem.get_text(strip=True)

            # Description from posting sections
            desc_elems = soup.select(".section-wrapper .section .content, .posting-page .content, [class*='description']")
            if desc_elems:
                desc_text = "\n".join(elem.get_text(strip=True) for elem in desc_elems)
                if desc_text and len(desc_text) > 50:
                    job_data["description"] = desc_text[:5000]

        # Ashby parsing (jobs.ashbyhq.com)
        elif "ashbyhq.com" in url:
            logger.info(f"[JobBoard] Parsing Ashby URL: {url}")
            # Ashby is mostly client-rendered, but has some data in script tags
            # Try to find __NEXT_DATA__ or similar embedded JSON
            for script in soup.find_all('script'):
                script_text = script.string or ''
                if '__NEXT_DATA__' in script_text or 'jobPosting' in script_text or '"title"' in script_text:
                    try:
                        # Try to extract JSON from script content
                        json_match = re.search(r'\{.*"title".*\}', script_text, re.DOTALL)
                        if json_match:
                            script_data = json.loads(json_match.group(0))
                            # Navigate nested structure
                            props = script_data.get('props', {}).get('pageProps', {})
                            job_info = props.get('job', props.get('jobPosting', props))
                            if job_info.get('title'):
                                job_data["title"] = job_info['title']
                            if job_info.get('location'):
                                loc = job_info['location']
                                job_data["location"] = loc if isinstance(loc, str) else loc.get('name', '')
                            if job_info.get('descriptionHtml') or job_info.get('description'):
                                desc_html = job_info.get('descriptionHtml', job_info.get('description', ''))
                                job_data["description"] = BeautifulSoup(desc_html, 'html.parser').get_text(strip=True)[:5000]
                            if job_info.get('organizationName') or job_info.get('companyName'):
                                job_data["company"] = job_info.get('organizationName', job_info.get('companyName', ''))
                    except (json.JSONDecodeError, KeyError, AttributeError):
                        continue

            # Fallback: extract company from URL
            if not job_data.get("company"):
                ashby_match = re.search(r'ashbyhq\.com/([^/]+)', url)
                if ashby_match:
                    job_data["company"] = ashby_match.group(1).replace("-", " ").title()

            # HTML fallback selectors
            if not job_data.get("title"):
                title_elem = soup.select_one("h1, [class*='title']")
                if title_elem:
                    job_data["title"] = title_elem.get_text(strip=True)
            if not job_data.get("description"):
                desc_elem = soup.select_one("[class*='description'], [class*='content'], article")
                if desc_elem:
                    desc_text = desc_elem.get_text(strip=True)
                    if desc_text and len(desc_text) > 50:
                        job_data["description"] = desc_text[:5000]

        # Workday parsing (myworkdayjobs.com / wd5.myworkday.com)
        elif "workday" in url or "myworkdayjobs.com" in url:
            logger.info(f"[JobBoard] Parsing Workday URL: {url}")
            # Workday is heavily client-rendered but sometimes embeds data in scripts
            for script in soup.find_all('script'):
                script_text = script.string or ''
                if '"jobPostingInfo"' in script_text or '"title"' in script_text:
                    try:
                        json_match = re.search(r'\{.*"title".*\}', script_text, re.DOTALL)
                        if json_match:
                            wd_data = json.loads(json_match.group(0))
                            posting = wd_data.get('jobPostingInfo', wd_data)
                            if posting.get('title'):
                                job_data["title"] = posting['title']
                            if posting.get('location'):
                                job_data["location"] = posting['location']
                            if posting.get('jobDescription'):
                                job_data["description"] = BeautifulSoup(posting['jobDescription'], 'html.parser').get_text(strip=True)[:5000]
                    except (json.JSONDecodeError, KeyError):
                        continue

            # Try to extract company from URL
            if not job_data.get("company"):
                wd_match = re.search(r'myworkdayjobs\.com/(?:en-US/)?([^/]+)', url)
                if not wd_match:
                    wd_match = re.search(r'myworkday\.com/([^/]+)', url)
                if wd_match:
                    job_data["company"] = wd_match.group(1).replace("-", " ").title()

            # HTML fallback
            if not job_data.get("title"):
                title_elem = soup.select_one("h1, h2[data-automation-id='jobPostingHeader'], [data-automation-id='jobPostingTitle']")
                if title_elem:
                    job_data["title"] = title_elem.get_text(strip=True)
            if not job_data.get("description"):
                desc_elem = soup.select_one("[data-automation-id='jobPostingDescription'], [class*='description']")
                if desc_elem:
                    desc_text = desc_elem.get_text(strip=True)
                    if desc_text and len(desc_text) > 50:
                        job_data["description"] = desc_text[:5000]

        # Glassdoor parsing
        elif "glassdoor.com" in url:
            logger.info(f"[JobBoard] Parsing Glassdoor URL: {url}")
            # Try JSON-LD first (already handled above), then selectors
            if not job_data.get("title"):
                title_elem = soup.select_one("[class*='JobTitle'], h1, [data-test='job-title']")
                if title_elem:
                    job_data["title"] = title_elem.get_text(strip=True)
            if not job_data.get("company"):
                company_elem = soup.select_one("[class*='EmployerName'], [data-test='employer-name']")
                if company_elem:
                    job_data["company"] = company_elem.get_text(strip=True)
            if not job_data.get("description"):
                desc_elem = soup.select_one("[class*='JobDescription'], [class*='jobDescription'], #JobDescriptionContainer")
                if desc_elem:
                    desc_text = desc_elem.get_text(strip=True)
                    if desc_text and len(desc_text) > 50:
                        job_data["description"] = desc_text[:5000]

        # LinkedIn parsing
        elif "linkedin.com" in url:
            # Try updated LinkedIn selectors first (current structure)
            title_selectors = [
                "h1.job-details-jobs-unified-top-card__job-title",
                ".jobs-unified-top-card__job-title",
                "h1.topcard__title",
                "h1[class*='job-title']",
                ".jobs-details h1",
                "h1.top-card-layout__title"  # Fallback to old selector
            ]
            for selector in title_selectors:
                title_elem = soup.select_one(selector)
                if title_elem:
                    job_data["title"] = title_elem.get_text(strip=True)
                    break
            
            # Try updated company selectors
            company_selectors = [
                ".job-details-jobs-unified-top-card__company-name a",
                ".jobs-unified-top-card__company-name a",
                ".topcard__org-name-link",
                ".job-details-jobs-unified-top-card__primary-description-container a",
                ".jobs-details-top-card__company-url",
                "a[class*='org-name-link']"
            ]
            for selector in company_selectors:
                company_elem = soup.select_one(selector)
                if company_elem:
                    job_data["company"] = company_elem.get_text(strip=True)
                    break
            
            # Try updated location selectors
            location_selectors = [
                ".job-details-jobs-unified-top-card__bullet",
                ".jobs-unified-top-card__bullet",
                ".topcard__flavor--bullet",
                ".jobs-details-top-card__bullet",
                "span[class*='bullet']"
            ]
            for selector in location_selectors:
                location_elem = soup.select_one(selector)
                if location_elem:
                    job_data["location"] = location_elem.get_text(strip=True)
                    break
            
            # Try updated description selectors
            description_selectors = [
                ".jobs-description__content",
                ".jobs-box__html-content",
                "#job-details",
                ".jobs-description-content__text",
                "div.description__text"  # Fallback to old selector
            ]
            for selector in description_selectors:
                description_elem = soup.select_one(selector)
                if description_elem:
                    desc_text = description_elem.get_text(strip=True)
                    if desc_text and len(desc_text) > 50:
                        job_data["description"] = desc_text[:5000]
                        break
            
        # Indeed parsing
        elif "indeed.com" in url:
            title_elem = soup.find("h1", class_="jobsearch-JobInfoHeader-title")
            company_elem = soup.find("div", {"data-company-name": True})
            description_elem = soup.find("div", id="jobDescriptionText")
            
            job_data["title"] = title_elem.get_text(strip=True) if title_elem else None
            job_data["company"] = company_elem.get_text(strip=True) if company_elem else None
            job_data["description"] = description_elem.get_text(strip=True) if description_elem else None
        
        # beBee parsing
        elif "bebee.com" in url:
            logger.info(f"[JobBoard] Parsing beBee URL: {url}")
            # beBee typically has the title in h1
            title_elem = soup.find("h1")
            if title_elem:
                title_text = title_elem.get_text(strip=True)
                # Remove location from title if present (format: "Title - Location")
                if " - " in title_text:
                    title_text = title_text.split(" - ")[0]
                job_data["title"] = title_text
                logger.info(f"[JobBoard] Found beBee title: {job_data['title']}")
            
            # Try to find company - might be in various places
            company_selectors = [
                "span[class*='company']",
                "div[class*='company']",
                "a[class*='company']",
                "[class*='employer']",
                "[class*='organization']"
            ]
            for selector in company_selectors:
                elem = soup.select_one(selector)
                if elem:
                    company_text = elem.get_text(strip=True)
                    if company_text and len(company_text) > 2:
                        job_data["company"] = company_text[:100]
                        logger.info(f"[JobBoard] Found beBee company: {job_data['company']}")
                        break
            
            # Try to find job description
            desc_selectors = [
                "div[class*='description']",
                "div[class*='job-description']",
                "div[class*='content']",
                "article",
                "div[id*='description']",
                "section[class*='description']"
            ]
            for selector in desc_selectors:
                elem = soup.select_one(selector)
                if elem:
                    desc_text = elem.get_text(strip=True)
                    # Skip if it's just Lorem ipsum placeholder text
                    if desc_text and "lorem ipsum" not in desc_text.lower()[:100]:
                        job_data["description"] = desc_text[:5000]
                        logger.info(f"[JobBoard] Found beBee description: {len(job_data['description'])} chars")
                        break
            
            # If no description found, try to get summary
            if not job_data["description"]:
                summary_elem = soup.find("div", class_=lambda x: x and "summary" in x.lower())
                if summary_elem:
                    job_data["description"] = summary_elem.get_text(strip=True)[:5000]
                    logger.info(f"[JobBoard] Found beBee summary: {len(job_data['description'])} chars")
            
            # Extract location from title if present
            if title_elem:
                title_full = title_elem.get_text(strip=True)
                if " - " in title_full:
                    location_part = title_full.split(" - ")[-1]
                    if "," in location_part or any(word in location_part.lower() for word in ["ca", "ny", "tx", "fl", "il"]):
                        job_data["location"] = location_part
                        logger.info(f"[JobBoard] Found beBee location: {job_data['location']}")
            
        # Apple jobs parsing
        elif "jobs.apple.com" in url:
            logger.info(f"[JobBoard] Parsing Apple jobs URL: {url}")
            # Apple typically has the title in h1 or title tag
            title_elem = soup.find("h1") or soup.find("title")
            if title_elem:
                title_text = title_elem.get_text(strip=True)
                # Clean up title (remove " - Jobs - Careers at Apple" suffix)
                if " - " in title_text:
                    title_text = title_text.split(" - ")[0]
                job_data["title"] = title_text[:200]
                logger.info(f"[JobBoard] Found Apple title: {job_data['title']}")
            
            # Company is always Apple for jobs.apple.com
            job_data["company"] = "Apple"
            logger.info(f"[JobBoard] Set company to Apple")
            
            # Try to find description
            desc_selectors = [
                "[class*='description']",
                "[class*='job-description']",
                "[class*='content']",
                "article",
                "[id*='description']",
                "section[class*='description']",
                "div[class*='details']"
            ]
            for selector in desc_selectors:
                elem = soup.select_one(selector)
                if elem:
                    desc_text = elem.get_text(strip=True)
                    if desc_text and len(desc_text) > 50:
                        if "lorem ipsum" not in desc_text.lower()[:200]:
                            job_data["description"] = desc_text[:5000]
                            logger.info(f"[JobBoard] Found Apple description: {len(job_data['description'])} chars")
                            break
        
        # Generic fallback
        else:
            logger.info(f"[JobBoard] Using generic fallback parsing for URL: {url}")
            # Try multiple title selectors
            title_selectors = [
                "h1",
                "h2[class*='title']",
                "[class*='job-title']",
                "[class*='position-title']",
                "title"  # HTML title tag as last resort
            ]
            for selector in title_selectors:
                elem = soup.select_one(selector)
                if elem:
                    title_text = elem.get_text(strip=True)
                    # Remove location from title if present (format: "Title - Location")
                    if " - " in title_text and len(title_text.split(" - ")) == 2:
                        title_text = title_text.split(" - ")[0]
                    if title_text and len(title_text) > 3:
                        job_data["title"] = title_text[:200]
                        logger.info(f"[JobBoard] Found title: {job_data['title']}")
                        break
                    
            # Try multiple company selectors
            company_selectors = [
                "[class*='company']",
                "[class*='employer']",
                "[class*='organization']",
                "[class*='recruiter']",
                "a[href*='company']"
            ]
            for selector in company_selectors:
                elem = soup.select_one(selector)
                if elem:
                    company_text = elem.get_text(strip=True)
                    if company_text and len(company_text) > 2 and len(company_text) < 100:
                        job_data["company"] = company_text[:100]
                        logger.info(f"[JobBoard] Found company: {job_data['company']}")
                        break
            
            # If company not found via selectors, try extracting from title
            if not job_data["company"] and job_data["title"]:
                title = job_data["title"]
                # Pattern 1: "Careers at [Company]" or "Jobs at [Company]"
                match = re.search(r'(?:Careers|Jobs)\s+at\s+([A-Za-z0-9\s&\.]+?)(?:\s*[-–—]|\s*\||\s*$)', title, re.IGNORECASE)
                if match:
                    company = match.group(1).strip()
                    # Clean up common suffixes
                    company = re.sub(r'\s*(Jobs|Careers|Internships).*$', '', company, flags=re.IGNORECASE)
                    if len(company) > 1 and len(company) < 100:
                        job_data["company"] = company
                        logger.info(f"[JobBoard] Extracted company from title (pattern 1): {job_data['company']}")
                
                # Pattern 2: "[Company] Careers" or "[Company] Jobs"
                if not job_data["company"]:
                    match = re.search(r'^([A-Za-z0-9\s&\.]+?)\s+(?:Careers|Jobs|Internships)', title, re.IGNORECASE)
                    if match:
                        company = match.group(1).strip()
                        if len(company) > 1 and len(company) < 100:
                            job_data["company"] = company
                            logger.info(f"[JobBoard] Extracted company from title (pattern 2): {job_data['company']}")
                
                # Pattern 3: "Title - Company" (if title ends with company name)
                if not job_data["company"] and " - " in title:
                    parts = title.split(" - ")
                    if len(parts) >= 2:
                        # Check if last part looks like a company name (not a location)
                        last_part = parts[-1].strip()
                        # If it doesn't look like a location (no commas, not too long), treat as company
                        if "," not in last_part and len(last_part) < 50 and not any(word in last_part.lower() for word in ["jobs", "careers", "internships", "view", "apply"]):
                            job_data["company"] = last_part
                            logger.info(f"[JobBoard] Extracted company from title suffix: {job_data['company']}")
            
            # If still no company, try extracting from URL domain
            # Only do this for direct company career pages, not job boards
            if not job_data["company"]:
                try:
                    parsed_url = urlparse(url)
                    domain = parsed_url.netloc.lower()
                    
                    # Skip job board sites - they're not company career pages
                    job_board_domains = [
                        "linkedin.com", "indeed.com", "glassdoor.com", "monster.com",
                        "ziprecruiter.com", "simplyhired.com", "careerbuilder.com",
                        "bebee.com", "dice.com", "stackoverflow.com", "github.com"
                    ]
                    is_job_board = any(jb in domain for jb in job_board_domains)
                    
                    if not is_job_board and domain:
                        # Remove common prefixes
                        domain = domain.replace("www.", "").replace("jobs.", "").replace("careers.", "")
                        # Get the main domain part (before first dot)
                        domain_parts = domain.split(".")
                        if domain_parts:
                            main_domain = domain_parts[0]
                            # Capitalize first letter
                            if main_domain and len(main_domain) > 2:
                                company = main_domain.capitalize()
                                # Handle multi-word domains (e.g., "apple-com" -> "Apple Com" -> "Apple Com")
                                company = company.replace("-", " ").title()
                                # Handle common patterns
                                company = company.replace(" Com", "").replace(" Inc", "").replace(" Corp", "")
                                job_data["company"] = company
                                logger.info(f"[JobBoard] Extracted company from URL domain: {job_data['company']}")
                except Exception as e:
                    logger.error(f"[JobBoard] Error extracting company from URL: {e}")
                    
            # Try multiple description selectors
            desc_selectors = [
                "[class*='description']",
                "[class*='job-description']",
                "[class*='content']",
                "article",
                "[id*='description']",
                "section[class*='description']",
                "div[class*='details']"
            ]
            for selector in desc_selectors:
                elem = soup.select_one(selector)
                if elem:
                    desc_text = elem.get_text(strip=True)
                    # Skip if it's just placeholder text
                    if desc_text and len(desc_text) > 50:
                        # Check for Lorem ipsum
                        if "lorem ipsum" not in desc_text.lower()[:200]:
                            job_data["description"] = desc_text[:5000]
                            logger.info(f"[JobBoard] Found description: {len(job_data['description'])} chars")
                            break
        
        # Return job_data if we have at least title or company (essential fields)
        # Don't require description since some sites may not have it easily accessible
        has_essential_data = job_data.get("title") or job_data.get("company")
        if has_essential_data:
            logger.info(f"[JobBoard] Successfully parsed job URL: title={bool(job_data.get('title'))}, company={bool(job_data.get('company'))}, location={bool(job_data.get('location'))}, description={bool(job_data.get('description'))}")
            return job_data
        else:
            logger.error(f"[JobBoard] Failed to parse essential fields from URL: {url}")
            return None
        
    except Exception as e:
        logger.error(f"[JobBoard] Error parsing URL {url}: {e}")
        return None


def extract_keywords_from_job(description: str) -> List[str]:
    """
    Extract important keywords from a job description for ATS optimization.
    """
    skill_patterns = [
        r'\b(Python|Java|JavaScript|TypeScript|C\+\+|Go|Rust|Ruby|PHP|Swift|Kotlin|Scala)\b',
        r'\b(React|Angular|Vue|Node\.js|Django|Flask|Spring|Rails|Next\.js|Express)\b',
        r'\b(AWS|Azure|GCP|Docker|Kubernetes|CI/CD|DevOps|Terraform|Jenkins)\b',
        r'\b(SQL|PostgreSQL|MySQL|MongoDB|Redis|GraphQL|NoSQL|Elasticsearch)\b',
        r'\b(Machine Learning|AI|Deep Learning|NLP|Computer Vision|LLM|PyTorch|TensorFlow)\b',
        r'\b(Agile|Scrum|JIRA|Git|GitHub|GitLab|Confluence|Asana)\b',
        r'\b(Excel|PowerPoint|Tableau|Power BI|Salesforce|SAP|Oracle)\b',
        r'\b(Leadership|Communication|Problem.solving|Analytical|Teamwork|Collaboration)\b',
        r'\b(Financial Modeling|Valuation|DCF|LBO|M&A|Due Diligence|Bloomberg)\b',
        r'\b(Product Management|Roadmap|Stakeholder|Cross-functional|PRD|OKR)\b',
        r'\b(Figma|Sketch|Adobe|InDesign|Photoshop|Illustrator|UI|UX)\b',
    ]
    
    keywords = set()
    for pattern in skill_patterns:
        matches = re.findall(pattern, description, re.IGNORECASE)
        keywords.update([m.strip() for m in matches])
    
    return list(keywords)[:20]


async def optimize_resume_with_ai(
    user_resume: Dict[str, Any],
    job_description: str,
    job_title: str = "",
    company: str = "",
) -> Dict[str, Any]:
    """
    Use AI to optimize a resume for a specific job posting.
    """
    logger.info(f"[JobBoard optimize_resume_with_ai] Starting. Resume type: {type(user_resume)}")
    # Sanitize user_resume one more time to be absolutely sure
    user_resume = sanitize_firestore_data(user_resume, depth=0, max_depth=20)
    
    # Aggressive cleanup: recursively remove any DocumentReferences
    def deep_clean_refs(obj, path="root"):
        """Recursively remove all DocumentReferences."""
        if is_document_reference(obj):
            logger.info(f"[JobBoard] deep_clean_refs: Removing DocumentReference at {path}")
            return None
        elif isinstance(obj, dict):
            cleaned = {}
            for k, v in obj.items():
                if not is_document_reference(v):
                    cleaned[k] = deep_clean_refs(v, f"{path}.{k}")
            return cleaned
        elif isinstance(obj, list):
            cleaned = []
            for i, item in enumerate(obj):
                if not is_document_reference(item):
                    cleaned_item = deep_clean_refs(item, f"{path}[{i}]")
                    if cleaned_item is not None:
                        cleaned.append(cleaned_item)
            return cleaned
        return obj
    
    logger.info(f"[JobBoard optimize_resume_with_ai] Running deep_clean_refs...")
    user_resume = deep_clean_refs(user_resume)
    logger.info(f"[JobBoard optimize_resume_with_ai] Deep clean complete")
    
    keywords = extract_keywords_from_job(job_description)
    # Ensure all keywords are strings and filter out any None/empty values
    keywords = [str(k) for k in keywords if k and k is not None]
    
    # Custom JSON encoder that aggressively handles DocumentReferences
    def json_default(obj):
        """Custom default handler for json.dumps that catches DocumentReferences."""
        if is_document_reference(obj):
            try:
                if hasattr(obj, 'path'):
                    return str(obj.path)
                elif hasattr(obj, 'id'):
                    return str(obj.id)
                return str(obj)
            except:
                return None
        # Fallback to string conversion
        try:
            return str(obj)
        except:
            return None
    
    # Safely serialize resume for prompt - with multiple passes to catch all DocumentReferences
    logger.info(f"[JobBoard] Starting JSON serialization. Resume keys: {list(user_resume.keys()) if isinstance(user_resume, dict) else 'N/A'}")
    resume_json = None
    for attempt in range(5):  # Try up to 5 times with increasingly aggressive sanitization
        try:
            # Deep sanitize before each attempt
            logger.info(f"[JobBoard] Serialization attempt {attempt + 1}: sanitizing...")
            user_resume = sanitize_firestore_data(user_resume, depth=0, max_depth=20)
            
            # Final check for any remaining DocumentReferences before serialization
            def check_and_remove_refs(obj, path=""):
                """Recursively check and remove DocumentReferences."""
                if is_document_reference(obj):
                    logger.info(f"[JobBoard] Found DocumentReference at {path}, removing...")
                    return None
                elif isinstance(obj, dict):
                    cleaned = {}
                    for k, v in obj.items():
                        if not is_document_reference(v):
                            cleaned_v = check_and_remove_refs(v, f"{path}.{k}")
                            if cleaned_v is not None:
                                cleaned[k] = cleaned_v
                    return cleaned
                elif isinstance(obj, list):
                    cleaned = []
                    for i, item in enumerate(obj):
                        if not is_document_reference(item):
                            cleaned_item = check_and_remove_refs(item, f"{path}[{i}]")
                            if cleaned_item is not None:
                                cleaned.append(cleaned_item)
                    return cleaned
                return obj
            
            user_resume = check_and_remove_refs(user_resume, "root")
            
            # Convert all remaining non-serializable objects to strings
            def force_stringify(obj):
                """Force convert any remaining problematic objects to strings."""
                if is_document_reference(obj):
                    return str(obj.path) if hasattr(obj, 'path') else str(obj)
                elif isinstance(obj, dict):
                    return {k: force_stringify(v) for k, v in obj.items()}
                elif isinstance(obj, list):
                    return [force_stringify(item) for item in obj]
                elif not isinstance(obj, (str, int, float, bool, type(None))):
                    # Convert any other non-basic types to string
                    try:
                        return str(obj)
                    except:
                        return None
                return obj
            
            user_resume = force_stringify(user_resume)
            
            # Try to serialize with custom default handler
            logger.info(f"[JobBoard] Attempting json.dumps...")
            resume_json = json.dumps(user_resume, indent=2, default=json_default, ensure_ascii=False)
            
            # Verify the JSON doesn't contain any problematic objects by parsing it back
            logger.info(f"[JobBoard] Verifying JSON by parsing back...")
            test_parse = json.loads(resume_json)
            logger.info(f"[JobBoard]  JSON serialization successful!")
            break  # Success
        except (TypeError, ValueError) as e:
            logger.error(f"[JobBoard] Serialization attempt {attempt + 1} failed: {e}")
            import traceback
            traceback.print_exc()
            if attempt == 4:  # Last attempt
                # Final fallback: convert everything to basic types, remove any remaining problematic fields
                logger.info(f"[JobBoard] Final fallback: aggressive cleanup...")
                user_resume = sanitize_firestore_data(user_resume, depth=0, max_depth=20)
                # Remove any fields that might still have issues
                if isinstance(user_resume, dict):
                    user_resume = {k: v for k, v in user_resume.items() if not is_document_reference(v)}
                resume_json = json.dumps(user_resume, indent=2, default=json_default, ensure_ascii=False)
    
    # Ensure keywords are all strings - double check for any DocumentReferences
    logger.info(f"[JobBoard] Processing keywords. Count: {len(keywords)}")
    safe_keywords = []
    for idx, kw in enumerate(keywords):
        if is_document_reference(kw):
            logger.warning(f"[JobBoard] WARNING: DocumentReference found in keywords[{idx}], skipping...")
            continue
        safe_keywords.append(str(kw))
    keywords_str = ', '.join(safe_keywords) if safe_keywords else 'None specified'
    logger.info(f"[JobBoard] Keywords string created. Length: {len(keywords_str)}")
    
    # Ensure all string values are safe
    job_title_safe = str(job_title or 'Not specified')
    company_safe = str(company or 'Not specified')
    job_desc_safe = str(job_description[:3000])
    
    # Ensure resume_json is actually a string and doesn't contain any issues
    if not isinstance(resume_json, str):
        logger.error(f"[JobBoard] ERROR: resume_json is not a string! Type: {type(resume_json)}")
        raise ValueError(f"resume_json must be a string, got {type(resume_json)}")
    
    logger.info(f"[JobBoard] Building prompt. resume_json length: {len(resume_json)}")
    
    # Verify resume_json is actually a string and doesn't contain DocumentReferences
    if not isinstance(resume_json, str):
        raise ValueError("resume_json is not a string")
    
    # Test that we can safely use it in the prompt
    try:
        # Try to parse it back to ensure it's valid JSON
        json.loads(resume_json)
        logger.info(f"[JobBoard]  JSON validation passed")
    except json.JSONDecodeError as e:
        logger.info(f"[JobBoard] Invalid JSON in resume_json: {e}")
        raise
    
    # Build prompt with strict rules to prevent fabrication
    try:
        RESUME_OPTIMIZATION_PROMPT = """You are an expert resume optimizer. Your task is to enhance a resume for a specific job while following STRICT rules.

## ABSOLUTE RULES (NEVER VIOLATE)

### Rule 1: NEVER FABRICATE
- NEVER change degree types (Bachelor's stays Bachelor's, not Master's)
- NEVER change or invent dates
- NEVER change company names or job titles
- NEVER add skills, certifications, or experiences the candidate doesn't have
- NEVER guess or fill in missing information
- If something is unclear, keep it exactly as-is

### Rule 2: PRESERVE ALL CONTENT
- Keep ALL sections from the original resume (Education, Experience, Projects, Skills, etc.)
- Keep ALL bullet points — you may reword them but never delete them
- Keep ALL projects listed — these demonstrate technical ability
- Keep ALL skills listed — you may reorder by relevance but never remove
- Keep coursework if present — it shows relevant knowledge

### Rule 3: PRESERVE ALL FACTS
These must remain EXACTLY as in the original:
- Degree type and major (e.g., "Bachelor of Science in Data Science and Economics")
- University name
- Graduation date/expected graduation
- Company names (e.g., "Offerloop.ai" not "AI-powered platform")
- Job titles (e.g., "Student IT Assistant" not "Technical Support Specialist")
- Employment dates (e.g., "March 2024 – May 2025" not "2019-2020")
- Locations
- Quantified achievements (e.g., "100 students" stays "100 students")

### Rule 4: WHAT YOU CAN CHANGE
- Reword bullet points for stronger impact (stronger verbs, clearer outcomes)
- Reorder bullet points to prioritize most relevant ones first
- Reorder skills to put job-relevant skills first
- Add job-relevant keywords INTO existing bullet points where they fit naturally
- Improve action verbs (e.g., "helped with" → "supported", "did" → "executed")
- Make quantified impacts more prominent
- Tighten verbose language

### Rule 5: KEYWORD INTEGRATION
- Review the job keywords provided
- Insert keywords ONLY into existing content where they make sense
- DO NOT add keywords as standalone items if the candidate doesn't have that experience
- Example GOOD: Original "Built data pipelines" → Enhanced "Built ETL data pipelines using Python"
  (if candidate knows Python and job mentions ETL)
- Example BAD: Adding "Kubernetes" when candidate has no container experience

---

## JOB DETAILS

**Target Position:** {job_title}
**Company:** {company}
**Job Description:**
{job_description}

**Key Keywords to Consider (only use where naturally applicable):**
{keywords_list}

---

## ORIGINAL RESUME DATA

{resume_json}

---

## YOUR TASK

1. Read the original resume carefully
2. Identify which experiences and skills are most relevant to the target job
3. Enhance bullet points with stronger language and relevant keywords
4. Reorder content to prioritize relevance (most relevant first)
5. Return the optimized resume in the exact JSON format specified below

## OUTPUT FORMAT

Return ONLY valid JSON in this exact structure:

{{
  "optimized_content": "Full optimized resume text with clear sections (Summary, Experience, Education, Skills). ALL original content must be preserved - only enhanced wording.",
  "relevance_score": 0-100,
  "keywords_added": ["list of keywords you successfully integrated into existing content"],
  "important_keywords_missing": ["list of job keywords that didn't fit candidate's actual experience"],
  "sections_optimized": ["list of sections you improved"],
  "suggestions": ["specific, actionable suggestions for the candidate to genuinely improve their resume"],
  "warnings": ["any concerns, e.g., 'Candidate lacks X skill mentioned in job requirements'"]
}}

Note: "relevance_score" should reflect how well the candidate's actual experience aligns with the role requirements. Consider: relevant job titles, industry experience, years of experience match, responsibility level match. This is a nuanced judgment about how well their background fits the role (not just keyword matching - that's handled separately).

## FINAL CHECKLIST (Verify before responding)

- [ ] All dates match the original exactly
- [ ] All company names match the original exactly  
- [ ] All job titles match the original exactly
- [ ] Degree type matches the original exactly (Bachelor's/Master's/etc.)
- [ ] All projects from original are included
- [ ] All skills from original are included (none removed)
- [ ] All bullet points from original are included (none deleted)
- [ ] No skills or experiences were invented
- [ ] Keywords were only added where they fit existing experience"""

        prompt = RESUME_OPTIMIZATION_PROMPT.format(
            job_title=job_title_safe,
            company=company_safe,
            job_description=job_desc_safe,
            keywords_list=keywords_str,
            resume_json=resume_json
        )
    except Exception as prompt_error:
        logger.error(f"[JobBoard] ERROR building prompt f-string: {prompt_error}")
        import traceback
        traceback.print_exc()
        raise

    try:
        logger.info("[JobBoard] Getting OpenAI client...")
        openai_client = get_async_openai_client()
        logger.info(f"[JobBoard] Got OpenAI client: {openai_client}")
        if not openai_client:
            raise Exception("OpenAI client not available")
        
        logger.info("[JobBoard] About to call OpenAI API...")
        logger.info(f"[JobBoard] Prompt length: {len(prompt)}")
        logger.info(f"[JobBoard] Prompt type: {type(prompt)}")
        logger.info(f"[JobBoard] Prompt preview: {prompt[:200]}...")
        
        # Ensure prompt is a plain string (not containing any DocumentReferences)
        if not isinstance(prompt, str):
            logger.error(f"[JobBoard] ERROR: Prompt is not a string! Type: {type(prompt)}")
            prompt = str(prompt)
        
        # Ensure all message content is strings
        system_content = """You are a resume optimization expert. You MUST follow all rules exactly.
Your primary directive is to ENHANCE the resume without CHANGING any facts.
Never fabricate, never delete content, never change dates/titles/companies/degrees.
If you're unsure about something, keep it exactly as-is.
Return ONLY valid JSON. Do not include explanations or markdown."""
        user_content = str(prompt)  # Force to string
        
        logger.info(f"[JobBoard] System content type: {type(system_content)}")
        logger.info(f"[JobBoard] User content type: {type(user_content)}")
        
        messages = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_content}
        ]
        
        logger.info(f"[JobBoard] Messages list created. About to call API...")
        logger.info(f"[JobBoard] Model: gpt-4o, Max tokens: 3500")
        
        # Retry configuration - increased timeouts for large prompts
        max_retries = 2
        base_timeout = 180.0  # 3 minutes per attempt (increased from 120s)
        
        # For long-running requests, create a fresh client to avoid connection pool issues
        from app.services.openai_client import create_async_openai_client
        openai_client = create_async_openai_client()
        if not openai_client:
            raise Exception("OpenAI client not available")
        
        last_error = None
        for retry_attempt in range(max_retries):
            timeout = base_timeout + (retry_attempt * 60.0)  # Increase by 60s for retries (was 30s)
            logger.info(f"[JobBoard] Attempt {retry_attempt + 1}/{max_retries} with {timeout}s timeout...")
            
            try:
                # Create the API call with increased timeout
                # Note: The timeout parameter controls the HTTP client timeout
                api_call = openai_client.chat.completions.create(
                    model="gpt-4o",
                    messages=messages,
                    temperature=0.7,
                    max_tokens=3500,
                    timeout=timeout,  # HTTP client timeout
                )
                
                # Wait for response with additional buffer for connection pool
                # Add extra buffer for connection acquisition and processing
                response = await asyncio.wait_for(api_call, timeout=timeout + 60.0)  # Increased buffer to 60s for connection pool
                logger.info(f"[JobBoard]  OpenAI API call completed successfully (attempt {retry_attempt + 1})")
                last_error = None
                break  # Success, exit retry loop
                
            except asyncio.TimeoutError:
                last_error = TimeoutError(f"OpenAI API call timed out after {timeout} seconds (attempt {retry_attempt + 1}/{max_retries})")
                logger.error(f"[JobBoard]  {last_error}")
                if retry_attempt == max_retries - 1:
                    # Last attempt failed
                    import traceback
                    traceback.print_exc()
                    raise Exception("Resume optimization timed out after multiple attempts. Please try again or contact support if the issue persists.")
                # Wait before retry
                await asyncio.sleep(2.0)
                
            except Exception as api_error:
                last_error = api_error
                logger.error(f"[JobBoard]  API call failed (attempt {retry_attempt + 1}/{max_retries}): {api_error}")
                logger.error(f"[JobBoard] Error type: {type(api_error)}")
                if retry_attempt == max_retries - 1:
                    # Last attempt failed
                    import traceback
                    traceback.print_exc()
                    raise
                # Wait before retry
                await asyncio.sleep(2.0)
        
        # If we exited the loop without success
        if last_error:
            raise last_error
        logger.info(f"[JobBoard] Response type: {type(response)}")
        logger.info(f"[JobBoard] Number of choices: {len(response.choices) if hasattr(response, 'choices') else 'N/A'}")
        
        content = response.choices[0].message.content.strip()
        logger.info(f"[JobBoard] Content length: {len(content)}")
        logger.info(f"[JobBoard] Content preview: {content[:200]}...")
        
        if content.startswith("```"):
            content = re.sub(r"```json?\n?", "", content)
            content = re.sub(r"\n?```", "", content)
        
        logger.info("[JobBoard] About to parse JSON from content...")
        result = json.loads(content)
        logger.info("[JobBoard] JSON parsed successfully")
        
        logger.info("[JobBoard] Building return dictionary...")
        
        # Get AI relevance score (now the only score from AI)
        ai_relevance_score = int(result.get("relevance_score", 75))
        logger.info(f"[JobBoard] AI relevance score: {ai_relevance_score}")
        
        # Get the optimized resume content as text
        optimized_content = str(result.get("optimized_content", ""))
        
        # Calculate programmatic ATS scores
        logger.info("[JobBoard] Calculating programmatic ATS scores...")
        try:
            ats_result = calculate_ats_score(
                resume_text=optimized_content,
                job_description=job_desc_safe,
                ai_relevance_score=ai_relevance_score
            )
            logger.info(f"[JobBoard]  ATS scores calculated: overall={ats_result['overall']}, keywords={ats_result['keywords']}, formatting={ats_result['formatting']}, relevance={ats_result['relevance']}")
        except Exception as ats_error:
            logger.error(f"[JobBoard] ERROR calculating ATS scores: {ats_error}")
            import traceback
            traceback.print_exc()
            # Fallback to defaults if calculation fails
            ats_result = {
                "overall": 75,
                "keywords": 70,
                "formatting": 85,
                "relevance": ai_relevance_score,
                "details": {
                    "matched_keywords": [],
                    "missing_keywords": [],
                    "formatting_checks": {},
                    "formatting_issues": [],
                    "suggestions": ["Could not calculate detailed scores"]
                }
            }
        
        # Safely convert all list items to strings, filtering out any DocumentReferences
        def safe_string_list(items):
            """Convert list items to strings, filtering out DocumentReferences."""
            safe_items = []
            for item in items:
                if is_document_reference(item):
                    logger.warning(f"[JobBoard] WARNING: Found DocumentReference in list, skipping...")
                    continue
                try:
                    safe_items.append(str(item))
                except Exception as e:
                    logger.warning(f"[JobBoard] WARNING: Could not convert item to string: {e}")
                    continue
            return safe_items
        
        suggestions = safe_string_list(result.get("suggestions", []))
        keywords_added = safe_string_list(result.get("keywords_added", []))
        important_keywords_missing = safe_string_list(result.get("important_keywords_missing", []))
        warnings = safe_string_list(result.get("warnings", []))
        
        # Handle sections_optimized - can be array of strings or array of objects
        sections_optimized_raw = result.get("sections_optimized", [])
        sections_optimized = []
        for item in sections_optimized_raw:
            if is_document_reference(item):
                logger.warning(f"[JobBoard] WARNING: Found DocumentReference in sections_optimized, skipping...")
                continue
            if isinstance(item, dict):
                # New format: extract section name from object
                section_name = item.get("section", "")
                if section_name:
                    sections_optimized.append(str(section_name))
            else:
                # Old format: already a string
                try:
                    sections_optimized.append(str(item))
                except Exception as e:
                    logger.warning(f"[JobBoard] WARNING: Could not convert section to string: {e}")
                    continue
        
        confidence_level = result.get("confidence_level", "medium")
        if confidence_level not in ["high", "medium", "low"]:
            confidence_level = "medium"
        
        logger.info(f"[JobBoard] Suggestions count: {len(suggestions)}")
        logger.info(f"[JobBoard] Keywords added count: {len(keywords_added)}")
        logger.info(f"[JobBoard] Important keywords missing count: {len(important_keywords_missing)}")
        logger.info(f"[JobBoard] Sections optimized count: {len(sections_optimized)}")
        logger.warning(f"[JobBoard] Warnings count: {len(warnings)}")
        logger.info(f"[JobBoard] Confidence level: {confidence_level}")
        
        # Include structured resume data for PDF generation
        # The optimized resume should maintain the same structure as the original
        structured_resume = None
        if isinstance(user_resume, dict):
            # Create a copy of the structured resume data
            # This will be used by the frontend PDF generator
            structured_resume = {
                "name": user_resume.get("name", ""),
                "contact": user_resume.get("contact") or user_resume.get("Contact") or {},
                "Summary": user_resume.get("summary") or user_resume.get("Summary") or user_resume.get("objective") or user_resume.get("Objective") or "",
                "Experience": user_resume.get("experience") or user_resume.get("Experience") or [],
                "Education": user_resume.get("education") or user_resume.get("Education") or None,
                "Skills": user_resume.get("skills") or user_resume.get("Skills") or None,
                "Projects": user_resume.get("projects") or user_resume.get("Projects") or [],
                "Extracurriculars": user_resume.get("extracurriculars") or user_resume.get("Extracurriculars") or [],
            }
            # Clean up empty contact object
            if structured_resume["contact"] and not any(structured_resume["contact"].values()):
                structured_resume["contact"] = {}
        
        # Combine AI suggestions with ATS scorer suggestions
        all_suggestions = suggestions.copy()
        if ats_result.get("details", {}).get("suggestions"):
            # Add ATS scorer suggestions, avoiding duplicates
            for ats_suggestion in ats_result["details"]["suggestions"]:
                if ats_suggestion not in all_suggestions:
                    all_suggestions.append(ats_suggestion)
        
        # Use missing keywords from ATS scorer (more accurate)
        missing_keywords_from_scorer = ats_result.get("details", {}).get("missing_keywords", [])
        # Combine with AI's important_keywords_missing, avoiding duplicates
        all_missing_keywords = list(set(important_keywords_missing + missing_keywords_from_scorer))
        
        return_dict = {
            "content": optimized_content,
            "structured": structured_resume,  # Add structured data for PDF generation
            "atsScore": {
                "overall": ats_result["overall"],
                "keywords": ats_result["keywords"],
                "formatting": ats_result["formatting"],
                "relevance": ats_result["relevance"],
                "suggestions": all_suggestions[:15],  # Limit to 15 suggestions total
                "jdQualityWarning": ats_result["details"].get("jd_quality_warning"),  # JD quality warning
                "technicalKeywordsInJd": ats_result["details"].get("technical_keywords_in_jd", 0),  # Count of technical keywords
            },
            "keywordsAdded": keywords_added,
            "importantKeywordsMissing": all_missing_keywords[:15],  # Limit to 15 missing keywords
            "sectionsOptimized": sections_optimized,
            "warnings": warnings,
            "confidenceLevel": confidence_level,
        }
        
        # Final check - ensure return_dict is JSON serializable
        logger.info("[JobBoard] Verifying return dictionary is JSON serializable...")
        try:
            test_json = json.dumps(return_dict, default=str)
            logger.info("[JobBoard]  Return dictionary is JSON serializable")
        except Exception as json_error:
            logger.error(f"[JobBoard] ERROR: Return dictionary is not JSON serializable: {json_error}")
            import traceback
            traceback.print_exc()
            raise
        
        logger.info("[JobBoard] Return dictionary built successfully")
        return return_dict
        
    except Exception as e:
        logger.error(f"[JobBoard] AI optimization error: {e}")
        logger.error(f"[JobBoard] Error type: {type(e)}")
        logger.error(f"[JobBoard] Error args: {e.args}")
        import traceback
        logger.info("[JobBoard] Full traceback:")
        traceback.print_exc()
        raise


async def generate_cover_letter_with_ai(
    user_resume: Dict[str, Any],
    job_description: str,
    job_title: str = "",
    company: str = "",
) -> Dict[str, Any]:
    """
    Generate a personalized cover letter using AI.
    Optimized for performance: reduced retries, smaller prompts, faster generation.
    Uses gpt-4o for faster responses with optimized retry logic.
    """
    # Extract only relevant resume sections to reduce prompt size
    relevant_resume = extract_relevant_resume_data_for_cover_letter(user_resume)
    
    # Extract name for closing signature
    user_name = str(relevant_resume.get("name", "") or "")
    
    # Determine seniority level from resume
    def determine_seniority(resume: Dict[str, Any]) -> str:
        """Determine candidate seniority level from resume."""
        experience = resume.get("experience", [])
        education = resume.get("education", [])
        
        # Check if currently in school or recent grad
        education_list = education if isinstance(education, list) else [education] if education else []
        current_year = datetime.now().year
        
        # Check graduation dates
        for edu in education_list:
            if isinstance(edu, dict):
                grad_date = edu.get("graduationDate") or edu.get("endDate") or edu.get("end_date")
                if grad_date:
                    grad_year = None
                    if isinstance(grad_date, dict):
                        grad_year = grad_date.get("year")
                    elif isinstance(grad_date, str):
                        # Try to extract year
                        import re
                        year_match = re.search(r'\d{4}', grad_date)
                        if year_match:
                            grad_year = int(year_match.group())
                    
                    if grad_year and grad_year >= current_year - 2:
                        # Recent grad or still in school
                        return "new grad" if grad_year <= current_year else "intern"
        
        # Check experience years
        if experience:
            exp_list = experience if isinstance(experience, list) else [experience]
            total_years = 0
            for exp in exp_list:
                if isinstance(exp, dict):
                    start_date = exp.get("startDate") or exp.get("start_date")
                    end_date = exp.get("endDate") or exp.get("end_date") or {"year": current_year}
                    if isinstance(start_date, dict) and isinstance(end_date, dict):
                        start_year = start_date.get("year")
                        end_year = end_date.get("year") or current_year
                        if start_year:
                            total_years += (end_year - start_year)
            
            if total_years <= 1:
                return "new grad"
            elif total_years <= 3:
                return "early career"
        
        # Default based on experience presence
        if experience and len(experience if isinstance(experience, list) else [experience]) > 0:
            return "early career"
        
        return "new grad"  # Default assumption
    
    seniority_level = determine_seniority(relevant_resume)
    
    # Custom JSON encoder that handles DocumentReferences and other non-serializable objects
    def json_default(obj):
        """Custom default handler for json.dumps that catches DocumentReferences."""
        if is_document_reference(obj):
            try:
                if hasattr(obj, 'path'):
                    return str(obj.path)
                elif hasattr(obj, 'id'):
                    return str(obj.id)
                return str(obj)
            except:
                return None
        try:
            return str(obj)
        except:
            return None
    
    # Serialize optimized resume (no indentation for smaller size)
    try:
        resume_json = json.dumps(relevant_resume, default=json_default, ensure_ascii=False)
        # Verify it's valid JSON
        json.loads(resume_json)
        logger.info(f"[JobBoard]  Resume serialized successfully (length: {len(resume_json)} chars)")
    except Exception as e:
        logger.error(f"[JobBoard] Error serializing resume: {e}")
        raise ValueError(f"Failed to serialize resume data: {e}")
    
    # Truncate job description to prevent overly long prompts
    job_desc_truncated = job_description[:3000] if len(job_description) > 3000 else job_description
    
    prompt = f"""SYSTEM PROMPT — End-to-End Elite Cover Letter Generator

You are an expert career writer whose goal is to maximize interview conversion by producing cover letters that are:

clearly human-written

role-specific and company-specific

grounded in how work is actually done

free of generic or AI-sounding language

You do not write before reasoning.
You do not rely on marketing language or mission statements.

INPUTS

You will receive:

Company name: {company or 'Not specified'}
Job title: {job_title or 'Not specified'}
Job description: {job_desc_truncated}
Candidate resume (JSON or text): {resume_json}
Candidate seniority: {seniority_level}

INTERNAL WORKFLOW (MANDATORY)

You must complete Stages 1–3 internally before producing the final output.
Only the final cover letter text is returned.

🔍 STAGE 1 — ROLE & COMPANY REALITY EXTRACTION

From the job description and general public knowledge, infer the work realities of this role.

Extract:

A. Operating Model

How work is executed day to day

Ownership level and autonomy

Pace and pressure

Team or client interaction model

Degree of ambiguity

B. Success Criteria

What strong performance looks like in the first 3–6 months

What outputs actually matter

C. Risk Factors

Common mistakes

What underperformance looks like

What the role must avoid

Rules

Do not copy job description language

Do not use marketing or mission statements

Focus on execution reality

🧠 STAGE 2 — OPERATING MODEL COMPRESSION

Compress Stage 1 into 3–5 operating traits.

Each trait must describe how work is actually done, not values.

Examples of acceptable traits:

high ownership with limited direction

execution under tight deadlines

hypothesis-driven problem solving

reliability-first engineering

frequent client or user interaction

These traits will act as anchors for the letter.

✍️ STAGE 3 — COVER LETTER GENERATION

Write a 3–4 paragraph cover letter using the operating traits as constraints.

Paragraph 1 — Intentional, Company-Specific Hook

OPENING CONSTRAINT

Do not begin the cover letter with phrases such as:

"I am writing to apply"

"I am writing to express my interest"

"I am excited to apply"

The opening sentence must instead:

Reference how work is done in this role or company

Explain why this environment is a strong fit for how the candidate operates

The opening should read as motivation-driven, not application-driven.

Explain why this role at this company

Reference how the organization operates, not just what it does

This paragraph must fail if the company name is swapped with a competitor

Paragraph 2 — Ownership + Evidence

Highlight 2–3 concrete experiences

Use ownership verbs (built, led, owned, designed, shipped)

Include at least one real constraint (ambiguity, scale, deadlines, accuracy)

Frame as: action → result → impact

Paragraph 3 — Role Alignment via Operating Traits

Align the candidate's behavior and experience to the operating traits

ANALYTICAL SPIKE FRAMING

When including a technical or analytical example, frame it around:

how options were evaluated

tradeoffs considered

decisions made under constraints

Do not focus solely on performance improvements or technical elegance.

Include one concrete analytical or technical spike:

a specific task or analysis

the method used

the decision or outcome it supported

Avoid abstract skill claims.

OPERATING MECHANICS RULE

At least one sentence in the letter must reference a concrete work mechanic, such as:

team structure

feedback or learning loop

ownership model

iteration cadence

decision-making process

Values alone (impact, excellence, innovation) do not satisfy this requirement.

Paragraph 4 — Confident Close

CLOSING CONSTRAINT

The closing paragraph must:

Signal readiness to contribute

Invite a conversation about impact or fit

Avoid phrases that only express gratitude or excitement

The tone should be confident and forward-looking, not deferential.

Forward-looking

Invites conversation

No over-gratitude or insecurity

✂️ STYLE CONSTRAINTS (ANTI-AI, STRICT)

Do NOT use:

em dashes or hyphens

parentheses

bullet points

overly balanced or mirrored sentences

Prefer:

commas and periods

natural sentence variation

clear, direct language

mild repetition if it improves clarity

The writing should resemble a strong, thoughtful student or early-career professional, not optimized AI prose.

✅ FINAL QUALITY CHECK (MANDATORY)

Before returning the letter, verify:

At least two company-specific operating references

At least one concrete analytical or technical detail

At least two ownership verbs

Language matches how work is done in this role

HARD SWAP TEST

Replace the company name with a direct competitor and reread the letter.

If the letter still reads naturally without revision, it fails.

Revise until at least one paragraph becomes inaccurate or awkward when swapped.

No AI punctuation or stylistic tells

INEVITABILITY CHECK

After writing, ask:
"Does this letter make it clear why this candidate belongs in this environment specifically?"

If the answer is no, revise to strengthen operating-model alignment.

If any condition fails, revise.

FINAL OUTPUT

Return only the finalized cover letter text.
No explanations. No internal reasoning. No formatting notes.
Include a proper greeting (Dear [Hiring Manager/Team]) and closing with signature (Sincerely, {user_name})."""

    # Use fresh OpenAI client per request to avoid connection pool issues
    from app.services.openai_client import create_async_openai_client
    openai_client = create_async_openai_client()
    if not openai_client:
        raise Exception("OpenAI client not available")
    
    # Use gpt-4o for faster responses
    model = "gpt-4o"
    max_retries = 2  # Reduced from 3
    base_timeout = 45.0  # Reduced from 60s
    
    logger.info(f"[JobBoard] Calling OpenAI API for cover letter generation...")
    logger.info(f"[JobBoard] Model: {model}, Prompt length: {len(prompt)} chars, max_tokens: 1200")
    
    last_error = None
    for retry_attempt in range(max_retries):
        try:
            # Exponential backoff: 45s, 60s
            timeout = base_timeout + (retry_attempt * 15.0)
            
            if retry_attempt > 0:
                wait_time = 2 ** retry_attempt  # 2s, 4s
                logger.info(f"[JobBoard] Retry attempt {retry_attempt + 1}/{max_retries} after {wait_time}s wait...")
                await asyncio.sleep(wait_time)
            
            api_call = openai_client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": "You are an expert cover letter writer whose goal is to maximize interview conversion. Return only the finalized cover letter text, with no explanations or metadata."},
                    {"role": "user", "content": prompt}
                ],
                temperature=0.7,  # Reduced from 0.8 for slightly faster, more deterministic responses
                max_tokens=1200,  # Reduced from 2000 (cover letters are typically 400-600 words)
                timeout=timeout,
            )
            
            # Wait for response with slightly longer timeout
            response = await asyncio.wait_for(api_call, timeout=timeout + 10.0)
            logger.info(f"[JobBoard]  OpenAI API call completed for cover letter (attempt {retry_attempt + 1})")
            
            content = response.choices[0].message.content.strip()
            
            # Remove markdown code blocks if present
            if content.startswith("```"):
                content = re.sub(r"```[a-z]*\n?", "", content)
                content = re.sub(r"\n?```", "", content)
                content = content.strip()
            
            # Ensure proper signature is included if name is available
            if user_name and not content.lower().endswith(user_name.lower()):
                # Check if there's already a "Sincerely" or similar closing
                if "sincerely" not in content.lower() and "best regards" not in content.lower() and "regards" not in content.lower():
                    # Add signature if not present
                    content = content.rstrip() + f"\n\nSincerely,\n{user_name}"
            
            # Return in the expected format (maintaining backward compatibility)
            return {
                "content": content,
                "highlights": [],  # No longer extracting highlights
                "tone": "Professional",  # Default tone
            }
            
        except asyncio.TimeoutError:
            last_error = TimeoutError(f"OpenAI API call timed out after {timeout} seconds (attempt {retry_attempt + 1}/{max_retries})")
            logger.error(f"[JobBoard]  {last_error}")
            if retry_attempt == max_retries - 1:
                raise last_error
        except Exception as api_err:
            last_error = api_err
            logger.error(f"[JobBoard]  OpenAI API call error (attempt {retry_attempt + 1}/{max_retries}): {api_err}")
            if retry_attempt == max_retries - 1:
                raise
    
    # Should never reach here, but just in case
    if last_error:
        raise last_error
    raise Exception("Cover letter generation failed after all retries")


# =============================================================================
# API ROUTES
# =============================================================================

def _fetch_jobs_for_query(
    query_info: dict,
    location: str,
    primary_job_type: Optional[str],
    jobs_per_query: int,
    refresh: bool
) -> tuple[List[dict], dict]:
    """
    PHASE 3: Helper function to fetch jobs for a single query.
    Uses location from query_info if available (location-first), otherwise uses fallback location.
    
    Used for parallel execution.
    
    Returns:
        Tuple of (jobs list, query metadata dict)
    """
    query = query_info["query"]
    weight = query_info.get("weight", 1.0)
    source = query_info.get("source", "unknown")
    
    # PHASE 3: Use location from query_info if specified (location-first), otherwise use fallback
    query_location = query_info.get("location", location)
    # If query_location is "Remote", use "United States" for SerpAPI (remote jobs can be anywhere in US)
    if query_location and query_location.lower() == "remote":
        query_location = "United States"
    
    query_jobs = []
    current_token = None
    
    try:
        # PHASE 3: Fetch jobs for this query (queries now have location embedded)
        # We'll fetch 2 pages (20 jobs) per query
        for page_num in range(2):  # 2 pages = 20 jobs max
            jobs_batch, next_token = fetch_jobs_from_serpapi(
                query=query,
                location=query_location,  # PHASE 3: Use query-specific location
                job_type=primary_job_type,
                num_results=10,  # Google Jobs returns 10 per page
                use_cache=not refresh and page_num == 0,  # Only cache first page
                page_token=current_token,
            )
            
            if not jobs_batch:
                if page_num == 0:
                    logger.info(f"[JobBoard] No jobs found for query '{query}' on first page")
                else:
                    logger.info(f"[JobBoard] Pagination stopped for query '{query}' at page {page_num + 1} (no more results)")
                break
            
            # Early exit if no new jobs were added (avoid wasting API calls)
            jobs_before = len(query_jobs)
            
            # Deduplicate within this query's results (local deduplication)
            seen_in_query = set()
            for job in jobs_batch:
                job_id = job.get("id")
                if job_id and job_id not in seen_in_query:
                    seen_in_query.add(job_id)
                    job["_query_weight"] = weight  # Store weight in job for later aggregation
                    job["_query_source"] = source  # Track source for debugging
                    query_jobs.append(job)
            
            # Early exit if no new jobs were added
            jobs_after = len(query_jobs)
            if jobs_after == jobs_before:
                logger.info(f"[JobBoard] No new jobs added on page {page_num + 1} for query '{query}', stopping pagination")
                break
            
            if len(query_jobs) >= jobs_per_query:
                logger.info(f"[JobBoard] Reached target jobs per query ({jobs_per_query}) for '{query}'")
                break
            
            if not next_token:
                logger.info(f"[JobBoard] No more pages available for query '{query}' (no next_token)")
                break
            
            current_token = next_token
        
        query_metadata = {
            "query": query,
            "source": source,
            "jobs_found": len(query_jobs),
            "weight": weight
        }
        
        return query_jobs, query_metadata
        
    except Exception as e:
        logger.error(f"[JobBoard] Error fetching for query '{query}': {e}")
        import traceback
        traceback.print_exc()
        return [], {
            "query": query,
            "source": source,
            "jobs_found": 0,
            "weight": weight,
            "error": str(e)
        }


def fetch_personalized_jobs(
    user_profile: dict,
    job_types: List[str],
    locations: List[str],
    max_jobs: int = 50,
    refresh: bool = False,
    user_id: str = ""
) -> tuple[List[dict], dict]:
    """Serve the user-facing job feed from the Fantastic.jobs curated Firestore pool.

    The pool is populated daily by the ingest pipeline (backend/pipeline/main.py)
    with student-shaped recipes — internships, new-grad, MBB/IB/quant whitelists,
    visa-sponsoring roles — and pre-filtered by quality_gate.py (no staffing
    agencies, no scams, no senior-only roles, nothing >60 days old). This
    function applies the per-user hard gates, dedup, and ranking on top.

    Plan: docs/JOB_BOARD_ELEVATION_PLAN.md Phase 1.
    """
    from app.services.job_serving import (
        fetch_jobs_from_firestore,
        fetch_jobs_from_firestore_personalized,
    )
    from app.services.job_ranker_signals import load_user_signals

    # Phase 2: personalized vector-retrieved pool when the user has a
    # preference vector; otherwise falls back to recency-ordered pool.
    # RANKER_MODE=rules env var forces the recency path (kill switch).
    intent_contract_for_pool = user_profile.get("_intent_contract") or {}
    career_domain_for_pool = intent_contract_for_pool.get("career_domain") if isinstance(intent_contract_for_pool, dict) else None
    pool_jobs, pool_meta = fetch_jobs_from_firestore_personalized(
        uid=user_id,
        user_profile=user_profile,
        career_domain=career_domain_for_pool,
    )

    # Phase 2: drop jobs the user has explicitly dismissed before any other
    # processing. Cheap negative signal — they told us they don't want this
    # role, we shouldn't waste a slot scoring it.
    signals_preload = load_user_signals(user_id) if user_id else None
    dismissed_count = 0
    if signals_preload and signals_preload.dismissed_job_ids:
        before = len(pool_jobs)
        pool_jobs = [j for j in pool_jobs if j.get("id") not in signals_preload.dismissed_job_ids]
        dismissed_count = before - len(pool_jobs)

    quality_filtered = filter_jobs_by_quality(pool_jobs, min_quality_score=MIN_QUALITY_SCORE)

    intent_contract = user_profile.get("_intent_contract", {})
    if intent_contract:
        gated_jobs, gate_stats = apply_all_hard_gates(quality_filtered, intent_contract, user_id)
    else:
        logger.warning("[JobBoard] No intent_contract; skipping hard gates")
        gated_jobs = quality_filtered
        gate_stats = {
            "total_rejected": 0,
            "total_kept": len(quality_filtered),
            "career_domain": 0,
            "job_type": 0,
            "location": 0,
            "seniority": 0,
        }

    deduped, dedup_stats = deduplicate_jobs(gated_jobs)
    weights = {job.get("id"): 1.0 for job in deduped if job.get("id")}
    scored = score_jobs_by_resume_match(deduped, user_profile, weights, user_id=user_id)

    # Phase 2 telemetry: how many top-N results carry signal badges. Lets us
    # tell from logs whether ranking is actually surfacing user-specific
    # matches or whether the badges are all blank (= the boosts aren't firing
    # because nobody filled in dreamCompanies, etc.).
    top_10 = scored[:10]
    badge_counts = {
        "dream_company": sum(1 for j in top_10 if "dream_company" in (j.get("matchSignals") or [])),
        "target_company": sum(1 for j in top_10 if "target_company" in (j.get("matchSignals") or [])),
        "alumni_at_company": sum(1 for j in top_10 if "alumni_at_company" in (j.get("matchSignals") or [])),
        "saved_company_affinity": sum(1 for j in top_10 if "saved_company_affinity" in (j.get("matchSignals") or [])),
    }

    metadata = {
        "serving_source": "firestore",
        "queries_used": [{
            "query": "firestore_curated_pool",
            "source": "fantasticjobs",
            "jobs_found": len(pool_jobs),
            "weight": 1.0,
        }],
        "total_fetched": pool_meta.get("pool_size", len(pool_jobs)),
        "total_after_quality_filter": len(quality_filtered),
        "total_after_intent_gates": len(gated_jobs),
        "total_after_deduplication": len(deduped),
        "total_after_filter": len(deduped),
        "filtered_out": len(pool_jobs) - len(deduped),
        "dismissed_filtered": dismissed_count,
        "badge_counts_top10": badge_counts,
        "location": build_location_query(locations or user_profile.get("preferred_locations") or []),
        "gate_stats": gate_stats,
        "dedup_stats": dedup_stats,
        "firestore_pool": pool_meta,
    }
    logger.info(
        "[JobBoard] pool=%d quality=%d gated=%d deduped=%d scored=%d dismissed=%d badges_top10=%s",
        len(pool_jobs), len(quality_filtered), len(gated_jobs), len(deduped), len(scored),
        dismissed_count, badge_counts,
    )
    return scored[:max_jobs], metadata


def _fetch_personalized_jobs_legacy_serpapi(
    user_profile: dict,
    job_types: List[str],
    locations: List[str],
    max_jobs: int = 50,
    refresh: bool = False,
    user_id: str = ""
) -> tuple[List[dict], dict]:
    """Legacy SerpAPI-fanout job feed. No longer wired to the user-facing
    endpoint — kept only because /api/job-board/search and agent_actions.py
    still call fetch_jobs_from_serpapi directly. Slated for removal once
    those call sites migrate (Phase 7 cleanup in the elevation plan).
    """
    # PHASE 3: Build intent-aligned queries (queries now include location)
    queries = build_personalized_queries(user_profile, job_types)
    
    # PHASE 3: Extract location from intent contract for logging/stats
    intent_contract = user_profile.get("_intent_contract", {})
    preferred_locations = intent_contract.get("preferred_locations", []) if intent_contract else locations
    career_domains = intent_contract.get("career_domains", []) if intent_contract else []
    primary_domain = career_domains[0] if career_domains and len(career_domains) > 0 else None
    
    # Build location query for SerpAPI (queries themselves have location embedded, but SerpAPI needs separate param)
    location = build_location_query(locations)
    
    all_jobs = []
    seen_ids = set()
    query_weights = {}  # Track which query each job came from
    queries_used = []
    
    # QUICK WIN: Limit API calls - fetch from top 4 queries (reduced from 6)
    # This reduces API calls by ~33% while keeping the most relevant queries
    max_queries = min(4, len(queries))
    jobs_per_query = 20  # QUICK WIN: Reduced from 50 to 20 (2 pages × 10 jobs/page) per query
    
    primary_job_type = job_types[0] if job_types else None
    
    # Execute queries in parallel
    logger.info(f"[JobBoard] Executing {max_queries} queries in parallel (QUICK WIN: reduced from 6)...")
    start_time = datetime.now()
    
    with ThreadPoolExecutor(max_workers=4) as executor:  # Reduced from 6 to match max_queries
        # Submit all queries
        future_to_query = {
            executor.submit(
                _fetch_jobs_for_query,
                query_info,
                location,
                primary_job_type,
                jobs_per_query,
                refresh
            ): query_info
            for query_info in queries[:max_queries]
        }
        
        # Collect results as they complete
        for future in as_completed(future_to_query):
            query_info = future_to_query[future]
            try:
                query_jobs, query_metadata = future.result()
                
                # Deduplicate across all queries
                for job in query_jobs:
                    job_id = job.get("id")
                    if job_id and job_id not in seen_ids:
                        seen_ids.add(job_id)
                        weight = job.pop("_query_weight", query_info.get("weight", 1.0))
                        query_weights[job_id] = weight
                        all_jobs.append(job)
                
                queries_used.append(query_metadata)
                
                # Stop if we have enough jobs
                if len(all_jobs) >= max_jobs:
                    logger.info(f"[JobBoard] Reached max_jobs ({max_jobs}), stopping query execution")
                    # Cancel remaining futures (they'll complete but we won't process results)
                    break
                    
            except Exception as e:
                logger.error(f"[JobBoard] Query '{query_info.get('query', 'unknown')}' failed: {e}")
                queries_used.append({
                    "query": query_info.get("query", "unknown"),
                    "source": query_info.get("source", "unknown"),
                    "jobs_found": 0,
                    "error": str(e)
                })
    
    elapsed_time = (datetime.now() - start_time).total_seconds()
    logger.info(f"[JobBoard] Parallel query execution completed in {elapsed_time:.2f} seconds")
    
    # Filter out low-quality jobs BEFORE intent gates
    quality_filtered_jobs = filter_jobs_by_quality(all_jobs, min_quality_score=MIN_QUALITY_SCORE)
    
    # PHASE 2: Apply hard intent gates (after quality filter, before scoring)
    intent_contract = user_profile.get("_intent_contract", {})
    if not intent_contract:
        logger.warning(f"[HardGate][WARN] No intent_contract found for user, skipping hard gates (Phase 1 may not have run)")
        filtered_jobs = quality_filtered_jobs
        gate_stats = {"total_rejected": 0, "total_kept": len(quality_filtered_jobs), "career_domain": 0, "job_type": 0, "location": 0, "seniority": 0}
    else:
        filtered_jobs, gate_stats = apply_all_hard_gates(quality_filtered_jobs, intent_contract, user_id)
        
        # PHASE 3: Log gate effectiveness (target: rejection rate < 25%)
        total_after_quality = len(quality_filtered_jobs)
        total_rejected = gate_stats["total_rejected"]
        rejection_rate = (total_rejected / total_after_quality * 100) if total_after_quality > 0 else 0
        primary_domain = intent_contract.get("career_domains", [None])[0] if intent_contract else "unknown"
        
        logger.info(f"[QueryGen][STATS] domain={primary_domain} "
              f"fetched={len(all_jobs)} "
              f"after_quality={total_after_quality} "
              f"rejected_by_gates={total_rejected} "
              f"kept={gate_stats['total_kept']} "
              f"rejection_rate={rejection_rate:.1f}%")
        
        # Warn if rejection rate is too high (indicates query generation needs improvement)
        if rejection_rate > 25.0 and total_after_quality > 0:
            logger.warning(f"[QueryGen][WARN] High rejection rate {rejection_rate:.1f}% for domain={primary_domain}, queries may be too broad")
    
    # PHASE 6: Deduplicate jobs (after hard gates, before scoring)
    deduplicated_jobs, dedup_stats = deduplicate_jobs(filtered_jobs)
    
    # Score remaining jobs (match + quality scoring) - only jobs that passed all gates and deduplication
    scored_jobs = score_jobs_by_resume_match(deduplicated_jobs, user_profile, query_weights)
    
    # STEP 3: Ranking Transparency (No Changes)
    top_jobs = scored_jobs[:5]
    top_titles = [job.get("title", "N/A") for job in top_jobs]
    top_companies = [job.get("company", "N/A") for job in top_jobs]
    
    if scored_jobs:
        score_values = [job.get("combinedScore", 0) for job in scored_jobs]
        min_score = min(score_values) if score_values else 0
        max_score = max(score_values) if score_values else 0
        score_range = f"{min_score}-{max_score}"
    else:
        score_range = "N/A"
    
    logger.info(f"[RankingSummary]")
    logger.info(f"[RankingSummary] top_titles={top_titles}")
    logger.info(f"[RankingSummary] top_companies={top_companies}")
    logger.info(f"[RankingSummary] score_range={score_range}")
    
    # Return top jobs
    metadata = {
        "queries_used": queries_used,
        "total_fetched": len(all_jobs),
        "total_after_quality_filter": len(quality_filtered_jobs),
        "total_after_intent_gates": len(filtered_jobs),  # PHASE 2: Track jobs after intent gates
        "total_after_deduplication": len(deduplicated_jobs),  # PHASE 6: Track jobs after deduplication
        "total_after_filter": len(deduplicated_jobs),  # Keep for backwards compatibility (updated to post-dedup)
        "filtered_out": len(all_jobs) - len(deduplicated_jobs),
        "location": location,
        "gate_stats": gate_stats,  # PHASE 2: Add gate rejection stats to metadata
        "dedup_stats": dedup_stats  # PHASE 6: Add deduplication stats to metadata
    }
    
    return scored_jobs[:max_jobs], metadata


@job_board_bp.route("/jobs", methods=["POST"])
@require_firebase_auth
def get_job_listings():
    """
    Get job listings based on user preferences using SerpAPI Google Jobs.
    Uses Firestore caching to reduce API calls.
    
    Request body:
    {
        "jobTypes": ["Internship", "Full-Time"],
        "industries": ["Technology", "Finance"],
        "locations": ["San Francisco, CA"],
        "refresh": false,
        "searchQuery": "optional direct search query"
    }
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
        user_id = request.firebase_user.get('uid')

        job_types = data.get("jobTypes", ["Internship"])
        industries = data.get("industries", [])
        locations = data.get("locations", [])
        search_query = data.get("searchQuery", "")
        refresh = data.get("refresh", False)  # Force refresh bypasses cache

        # Phase 1 (Job Board Elevation Plan): cap the refresh path so a runaway
        # client can't burn external API quota by spamming refresh=true. Normal
        # cache-hit requests are not counted — only forced refreshes that
        # bypass the cache. 50/day per user is well above any human pattern.
        if refresh and not _check_user_rate_limit(user_id, "job-feed-refresh-daily", "50 per day"):
            return jsonify({
                "error": "Daily refresh limit reached",
                "message": "You've refreshed the job feed 50 times today. The next refresh will be available tomorrow."
            }), 429
        
        # Get pagination parameters
        page = data.get("page", 1)
        per_page = data.get("perPage", 20)
        start = (page - 1) * per_page
        
        # Cache invalidation is handled inside get_cached_jobs_with_stale() — no extra read needed here

        # Get comprehensive user profile
        user_profile = get_user_career_profile(user_id)
        
        # PHASE 1: Normalize intent contract (foundation for future phases)
        intent_contract = normalize_intent(user_profile)
        # Attach normalized intent to user_profile for downstream use (future phases)
        user_profile["_intent_contract"] = intent_contract
        
        # STEP 1: Persona Logging Guardrail
        education_context = intent_contract.get("education_context", {})
        graduation_timing = intent_contract.get("graduation_timing", {})
        degree = education_context.get("degree", "")
        career_phase = graduation_timing.get("career_phase", "unknown")
        domains = intent_contract.get("career_domains", [])
        preferred_locations = intent_contract.get("preferred_locations", [])
        months_until_graduation = graduation_timing.get("months_until_graduation")
        
        logger.info(f"[Persona]")
        logger.info(f"[Persona] degree={degree}")
        logger.info(f"[Persona] career_phase={career_phase}")
        logger.info(f"[Persona] job_types={job_types}")
        logger.info(f"[Persona] domains={domains}")
        logger.info(f"[Persona] preferred_locations={preferred_locations}")
        logger.info(f"[Persona] months_until_graduation={months_until_graduation}")
        
        # Debug: Log profile data (without sensitive info)
        major_value = user_profile.get('major', '')
        logger.info(f"[JobBoard] User profile summary: major={bool(major_value)} ({major_value}), "
              f"skills={len(user_profile.get('skills', []))}, "
              f"extracurriculars={len(user_profile.get('extracurriculars', []))}, "
              f"experiences={len(user_profile.get('experiences', []))}")
        
        # Add request params to profile
        user_profile["target_industries"] = industries or user_profile.get("target_industries", [])
        user_profile["job_types"] = job_types
        
        # QUICK WIN: Fetch personalized jobs using multi-query approach
        # Reduced from 150 to 50 jobs for faster initial load (enough for 2-3 pages)
        if page == 1:
            jobs, metadata = fetch_personalized_jobs(
                user_profile=user_profile,
                job_types=job_types,
                locations=locations,
                max_jobs=50,  # QUICK WIN: Reduced from 150 to 50 jobs for faster loading
                refresh=refresh,
                user_id=user_id  # PHASE 2: Pass user_id for hard gate logging
            )
        else:
            # For subsequent pages, use simpler single-query approach with pagination
            # (For now, we'll return empty for page > 1 - can be enhanced later)
            # TODO: Implement pagination for multi-query results
            jobs = []
            metadata = {"queries_used": [], "total_fetched": 0, "location": build_location_query(locations)}
        
        # Paginate results
        start_idx = (page - 1) * per_page
        end_idx = start_idx + per_page
        paginated_jobs = jobs[start_idx:end_idx]
        
        # Fallback to mock data if needed
        if not paginated_jobs:
            logger.info("[JobBoard] Falling back to mock data")
            paginated_jobs = get_mock_jobs(job_types, industries, locations)
            source = "demo"
        else:
            source = "serpapi"
        
        # Determine if there are more jobs available
        has_more = end_idx < len(jobs)
        
        # Estimate total
        estimated_total = len(jobs)
        
        # Get primary query for display
        primary_query = ""
        if metadata.get("queries_used"):
            primary_query = metadata["queries_used"][0].get("query", "")
        elif search_query:
            primary_query = search_query
        
        location = metadata.get("location", build_location_query(locations))
        
        # Quality filter toggle (default to True)
        quality_filter = data.get("qualityFilter", True)
        
        # STEP 4: Location UX Micro-Clarification
        location_label = None
        if len(preferred_locations) > 1:
            location_label = f"Showing jobs in: {' · '.join(preferred_locations)}"
        elif len(preferred_locations) == 1:
            location_label = f"Showing jobs in: {preferred_locations[0]}"
        
        logger.info(f"[JobBoard] Returning {len(paginated_jobs)} jobs, hasMore={has_more}, estimatedTotal={estimated_total}")
        
        return jsonify({
            "jobs": paginated_jobs,
            "total": len(jobs),
            "estimatedTotal": estimated_total,
            "page": page,
            "perPage": per_page,
            "hasMore": has_more,
            "source": source,
            "query": primary_query,
            "location": location,
            "locationLabel": location_label,
            "cached": source == "serpapi" and not refresh,
            "quality": {
                "filtered_out": metadata.get("filtered_out", 0),
                "filter_enabled": quality_filter
            },
            "personalization": {
                "major": user_profile.get("major"),
                "skills_used": len(user_profile.get("skills", [])),
                "ec_signals": len(extract_career_signals(user_profile.get("extracurriculars", []))),
                "queries_used": len(metadata.get("queries_used", []))
            }
        }), 200
        
    except Exception as e:
        logger.error(f"[JobBoard] Error fetching jobs: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@job_board_bp.route("/search", methods=["POST"])
@require_firebase_auth
def search_jobs():
    """
    Direct job search with custom query.
    
    Request body:
    {
        "query": "software engineer intern",
        "location": "San Francisco, CA",
        "jobType": "Internship"
    }
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
        
        query = data.get("query", "entry level jobs")
        location = data.get("location", "United States")
        job_type = data.get("jobType")
        
        jobs, _ = fetch_jobs_from_serpapi(
            query=query,
            location=location,
            job_type=job_type,
            num_results=10,
            page_token=None,
            user_id=None,  # PHASE 5: user_id not available in this context
        )
        
        return jsonify({
            "jobs": jobs,
            "total": len(jobs),
            "query": query,
            "location": location,
        }), 200
        
    except Exception as e:
        logger.error(f"[JobBoard] Search error: {e}")
        return jsonify({"error": str(e)}), 500


@job_board_bp.route("/optimize-resume", methods=["POST"])
@require_firebase_auth
def optimize_resume():
    """
    Optimize user's resume for a specific job.
    Cost: 20 credits (refunded on failure)
    """
    stages = {}
    start_time = time.time()
    
    try:
        data = request.get_json(force=True, silent=True) or {}
        user_id = request.firebase_user.get('uid')
        
        job_url = data.get("jobUrl")
        job_description = data.get("jobDescription", "")
        job_title = data.get("jobTitle", "")
        company = data.get("company", "")
        
        # Stage 1: Validate inputs and check database
        stages['validate'] = {'start': time.time()}
        db = get_db()
        if not db:
            return jsonify({
                "error": True,
                "error_code": "database_error",
                "message": ERROR_MESSAGES.get("database_error", "Database not available"),
                "credits_refunded": False
            }), 500
            
        user_ref = db.collection("users").document(user_id)
        user_doc = user_ref.get()
        
        if not user_doc.exists:
            return jsonify({
                "error": True,
                "error_code": "user_not_found",
                "message": "User not found",
                "credits_refunded": False
            }), 404
        
        # Validate job description input
        if not job_url and not job_description:
            stages['validate']['end'] = time.time()
            return jsonify({
                "error": True,
                "error_code": "invalid_job_description",
                "message": ERROR_MESSAGES.get("invalid_job_description"),
                "credits_refunded": False
            }), get_status_code("invalid_job_description")
        
        if job_description and len(job_description.strip()) < 50:
            stages['validate']['end'] = time.time()
            return jsonify({
                "error": True,
                "error_code": "invalid_job_description",
                "message": ERROR_MESSAGES.get("invalid_job_description"),
                "credits_refunded": False
            }), get_status_code("invalid_job_description")
        
        user_data = user_doc.to_dict()
        # Sanitize user_data before using it - it might contain DocumentReferences
        logger.info(f"[JobBoard] Sanitizing user_data before credit check...")
        user_data = sanitize_firestore_data(user_data, depth=0, max_depth=20)
        current_credits = check_and_reset_credits(user_ref, user_data)
        stages['validate']['end'] = time.time()
        
        # Check credits first
        if current_credits < OPTIMIZATION_CREDIT_COST:
            return jsonify({
                "error": True,
                "error_code": "credits_insufficient",
                "message": ERROR_MESSAGES.get("credits_insufficient"),
                "required": OPTIMIZATION_CREDIT_COST,
                "current": current_credits,
                "credits_refunded": False
            }), get_status_code("credits_insufficient")
        
        # Deduct credits BEFORE doing the expensive optimization
        # This prevents negative balances and ensures we have credits before proceeding
        logger.info(f"[JobBoard] Deducting credits before optimization...")
        logger.info(f"[JobBoard] Current credits: {current_credits}, Required: {OPTIMIZATION_CREDIT_COST}")
        success, new_credits = deduct_credits_atomic(user_id, OPTIMIZATION_CREDIT_COST, "resume_optimization")
        
        if not success:
            # Deduction failed (likely insufficient credits due to race condition)
            return jsonify({
                "error": True,
                "error_code": "credits_insufficient",
                "message": ERROR_MESSAGES.get("credits_insufficient"),
                "current": new_credits,
                "required": OPTIMIZATION_CREDIT_COST,
                "credits_refunded": False
            }), get_status_code("credits_insufficient")
        
        logger.info(f"[JobBoard] Credits deducted successfully. New balance: {new_credits}")
        
        # Stage 2: Parse job URL (if provided)
        stages['parse_job'] = {'start': time.time()}
        if job_url:
            logger.info(f"[JobBoard] Parsing job URL: {job_url}")
            try:
                parsed_job = parse_job_url(job_url)
                if parsed_job:
                    # Parsing succeeded - use title/company from URL if available
                    if parsed_job.get("title"):
                        job_title = parsed_job.get("title")
                        logger.info(f"[JobBoard] Parsed title from URL: {job_title}")
                    if parsed_job.get("company"):
                        company = parsed_job.get("company")
                        logger.info(f"[JobBoard] Parsed company from URL: {company}")
                    # Use description from URL if available and valid, otherwise keep manually pasted one
                    if parsed_job.get("description") and len(parsed_job.get("description", "").strip()) >= 50:
                        job_description = parsed_job.get("description")
                        logger.info(f"[JobBoard] Using description from URL ({len(job_description)} chars)")
                    else:
                        logger.info(f"[JobBoard] URL parsing didn't provide a valid description, will use manually pasted description if available")
                else:
                    logger.info(f"[JobBoard] URL parsing returned None, will use manually pasted description if available")
            except Exception as url_error:
                logger.error(f"[JobBoard] Error parsing URL (non-fatal): {url_error}")
                # Don't fail here - continue to use manually pasted description if available
                import traceback
                traceback.print_exc()
        
        stages['parse_job']['end'] = time.time()
        
        # Validate job description - must have either from URL or manually pasted
        if not job_description or len(job_description.strip()) < 50:
            refund_success, _ = refund_credits_atomic(user_id, OPTIMIZATION_CREDIT_COST, "resume_optimization_refund")
            logger.info(f"[JobBoard] Refunded credits due to invalid/missing job description: {refund_success}")
            return jsonify({
                "error": True,
                "error_code": "invalid_job_description",
                "message": ERROR_MESSAGES.get("invalid_job_description"),
                "credits_refunded": refund_success
            }), get_status_code("invalid_job_description")
        
        # Stage 3: Retrieve resume
        stages['retrieve_resume'] = {'start': time.time()}
        raw_resume = user_data.get("resumeParsed", {})
        if not raw_resume:
            stages['retrieve_resume']['end'] = time.time()
            refund_success, _ = refund_credits_atomic(user_id, OPTIMIZATION_CREDIT_COST, "resume_optimization_refund")
            logger.info(f"[JobBoard] Refunded credits due to missing resume: {refund_success}")
            return jsonify({
                "error": True,
                "error_code": "resume_not_found",
                "message": ERROR_MESSAGES.get("resume_not_found"),
                "credits_refunded": refund_success
            }), get_status_code("resume_not_found")
        
        # Deep sanitization - run multiple times to catch nested references
        logger.info(f"[JobBoard] Starting sanitization of resume data. Type: {type(raw_resume)}")
        user_resume = raw_resume
        for i in range(3):  # Multiple passes
            logger.info(f"[JobBoard] Sanitization pass {i+1}")
            user_resume = sanitize_firestore_data(user_resume, depth=0, max_depth=20)
        
        # Additional check: manually inspect and clean ALL list fields (handle both old and new formats)
        list_fields = ['experience', 'key_experiences', 'achievements', 'interests', 'skills', 'projects', 'extracurriculars', 'certifications', 'publications', 'awards', 'volunteer']
        for field_name in list_fields:
            if isinstance(user_resume, dict) and field_name in user_resume:
                field_value = user_resume.get(field_name, [])
                if isinstance(field_value, list):
                    cleaned_list = []
                    for idx, item in enumerate(field_value):
                        if item is not None:
                            cleaned_item = sanitize_firestore_data(item, depth=0, max_depth=20)
                            if isinstance(cleaned_item, dict):
                                cleaned_item = {k: sanitize_firestore_data(v, depth=0, max_depth=20) for k, v in cleaned_item.items()}
                            if cleaned_item is not None and not is_document_reference(cleaned_item):
                                cleaned_list.append(cleaned_item)
                    user_resume[field_name] = cleaned_list
        
        def recursive_check_for_refs(obj, path="root", max_depth=5, current_depth=0):
            """Recursively check for DocumentReferences and remove them."""
            if current_depth > max_depth:
                return obj
            if is_document_reference(obj):
                return None
            elif isinstance(obj, dict):
                cleaned = {}
                for k, v in obj.items():
                    cleaned_v = recursive_check_for_refs(v, f"{path}.{k}", max_depth, current_depth+1)
                    if cleaned_v is not None and not is_document_reference(cleaned_v):
                        cleaned[k] = cleaned_v
                return cleaned
            elif isinstance(obj, list):
                cleaned = []
                for i, item in enumerate(obj):
                    cleaned_item = recursive_check_for_refs(item, f"{path}[{i}]", max_depth, current_depth+1)
                    if cleaned_item is not None and not is_document_reference(cleaned_item):
                        cleaned.append(cleaned_item)
                return cleaned
            return obj
        
        user_resume = recursive_check_for_refs(user_resume, "root")
        
        def final_clean(obj):
            """Final aggressive clean - convert everything to basic types."""
            if is_document_reference(obj):
                return str(obj.path) if hasattr(obj, 'path') else str(obj)
            elif isinstance(obj, dict):
                return {k: final_clean(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [final_clean(item) for item in obj]
            elif not isinstance(obj, (str, int, float, bool, type(None))):
                return str(obj)
            return obj
        
        user_resume = final_clean(user_resume)
        stages['retrieve_resume']['end'] = time.time()
        
        if not user_resume:
            refund_success, _ = refund_credits_atomic(user_id, OPTIMIZATION_CREDIT_COST, "resume_optimization_refund")
            logger.info(f"[JobBoard] Refunded credits due to empty resume after sanitization: {refund_success}")
            return jsonify({
                "error": True,
                "error_code": "resume_incomplete",
                "message": ERROR_MESSAGES.get("resume_incomplete"),
                "credits_refunded": refund_success
            }), get_status_code("resume_incomplete")
        
        # Stage 4: Extract keywords
        stages['extract_keywords'] = {'start': time.time()}
        # Keywords will be extracted in optimize_resume_with_ai
        stages['extract_keywords']['end'] = time.time()
        
        # Stage 5: AI optimization (longest stage)
        stages['ai_optimization'] = {'start': time.time()}
        logger.info(f"[JobBoard] Calling optimize_resume_with_ai...")
        try:
            async def optimize_with_timeout():
                return await optimize_resume_with_ai(user_resume, job_description, job_title, company)
                
            # Increased outer timeout to match increased API timeouts
            # Base: 180s, Retry: 240s, Buffer: 30s, so max is ~270s, use 300s for safety
            logger.info(f"[JobBoard] Starting async optimization with 300 second timeout...")
            optimized = asyncio.run(
                asyncio.wait_for(optimize_with_timeout(), timeout=300.0)  # Increased from 190s to 300s
            )
            logger.info(f"[JobBoard]  AI function returned successfully")
            stages['ai_optimization']['end'] = time.time()
                
            
            # Final sanitization of the optimized result before returning
            logger.info(f"[JobBoard] Sanitizing optimized result before jsonify...")
            optimized = sanitize_firestore_data(optimized, depth=0, max_depth=20)
                
            
            # Verify it's JSON serializable
            try:
                test_json = json.dumps(optimized, default=str)
                logger.info(f"[JobBoard]  Optimized result is JSON serializable")
            except Exception as json_error:
                logger.error(f"[JobBoard] ERROR: Optimized result is not JSON serializable: {json_error}")
                import traceback
                traceback.print_exc()
                # Refund credits on JSON serialization error
                refund_success, _ = refund_credits_atomic(user_id, OPTIMIZATION_CREDIT_COST, "resume_optimization_refund")
                logger.error(f"[JobBoard] Refunded credits due to JSON serialization error: {refund_success}")
                return jsonify({
                    "error": True,
                    "error_code": "json_parse_error",
                    "message": ERROR_MESSAGES.get("json_parse_error"),
                    "credits_refunded": refund_success
                }), get_status_code("json_parse_error")
            
        except asyncio.TimeoutError:
            logger.info(f"[JobBoard]  Resume optimization timed out after 130 seconds")
            import traceback
            traceback.print_exc()
            stages['ai_optimization']['end'] = time.time()
            refund_success, _ = refund_credits_atomic(user_id, OPTIMIZATION_CREDIT_COST, "resume_optimization_refund")
            logger.info(f"[JobBoard] Refunded credits due to timeout: {refund_success}")
            return jsonify({
                "error": True,
                "error_code": "ai_timeout",
                "message": ERROR_MESSAGES.get("ai_timeout"),
                "credits_refunded": refund_success
            }), get_status_code("ai_timeout")
        except Exception as optimization_error:
            logger.error(f"[JobBoard]  Error in optimization: {optimization_error}")
            logger.error(f"[JobBoard] Error type: {type(optimization_error)}")
            import traceback
            traceback.print_exc()
            stages['ai_optimization']['end'] = time.time()
            
            # Check for rate limit errors
            error_str = str(optimization_error).lower()
            if 'rate limit' in error_str or '429' in error_str:
                refund_success, _ = refund_credits_atomic(user_id, OPTIMIZATION_CREDIT_COST, "resume_optimization_refund")
                logger.info(f"[JobBoard] Refunded credits due to rate limit: {refund_success}")
                return jsonify({
                    "error": True,
                    "error_code": "ai_rate_limit",
                    "message": ERROR_MESSAGES.get("ai_rate_limit"),
                    "credits_refunded": refund_success
                }), get_status_code("ai_rate_limit")
            else:
                # Generic AI error
                refund_success, _ = refund_credits_atomic(user_id, OPTIMIZATION_CREDIT_COST, "resume_optimization_refund")
                logger.error(f"[JobBoard] Refunded credits due to AI error: {refund_success}")
                return jsonify({
                    "error": True,
                    "error_code": "ai_error",
                    "message": ERROR_MESSAGES.get("ai_error"),
                    "credits_refunded": refund_success
                }), get_status_code("ai_error")
        
        # Credits were already deducted before the optimization
        # Use the new_credits from the earlier deduction
        logger.info(f"[JobBoard] Using credits from pre-deduction. Balance: {new_credits}")
        
        # Calculate processing time
        processing_time_ms = int((time.time() - start_time) * 1000)
        
        # Build response - ensure all values are basic types
        logger.info(f"[JobBoard] Building final response...")
        response_data = {
            "optimizedResume": optimized,
            "creditsUsed": int(OPTIMIZATION_CREDIT_COST),
            "creditsRemaining": int(new_credits) if new_credits is not None else 0,
            "processingTimeMs": processing_time_ms,
        }
        
        # Sanitize the entire response one more time
        logger.info(f"[JobBoard] Sanitizing final response...")
        response_data = sanitize_firestore_data(response_data, depth=0, max_depth=20)
        
        # Final check before jsonify
        logger.info(f"[JobBoard] Testing final response JSON serialization...")
        try:
            test_response_json = json.dumps(response_data, default=str)
            logger.info(f"[JobBoard]  Final response is JSON serializable")
        except Exception as json_error:
            logger.error(f"[JobBoard] ERROR: Final response is not JSON serializable: {json_error}")
            logger.info(f"[JobBoard] Response data: {response_data}")
            import traceback
            traceback.print_exc()
            # Refund credits if response building fails
            refund_success, _ = refund_credits_atomic(user_id, OPTIMIZATION_CREDIT_COST, "resume_optimization_refund")
            logger.error(f"[JobBoard] Refunded credits due to response serialization error: {refund_success}")
            return jsonify({
                "error": True,
                "error_code": "json_parse_error",
                "message": ERROR_MESSAGES.get("json_parse_error"),
                "credits_refunded": refund_success
            }), get_status_code("json_parse_error")
        
        logger.info(f"[JobBoard] Calling jsonify...")
        try:
            result = jsonify(response_data)
            logger.info(f"[JobBoard]  jsonify successful, returning response...")
            logger.info(f"[JobBoard] Processing time: {processing_time_ms}ms")
            return result, 200
        except Exception as jsonify_error:
            logger.error(f"[JobBoard] ERROR in jsonify: {jsonify_error}")
            import traceback
            traceback.print_exc()
            # Refund credits if jsonify fails
            refund_success, _ = refund_credits_atomic(user_id, OPTIMIZATION_CREDIT_COST, "resume_optimization_refund")
            logger.error(f"[JobBoard] Refunded credits due to jsonify error: {refund_success}")
            return jsonify({
                "error": True,
                "error_code": "json_parse_error",
                "message": ERROR_MESSAGES.get("json_parse_error"),
                "credits_refunded": refund_success
            }), get_status_code("json_parse_error")
        
    except Exception as e:
        logger.error(f"[JobBoard] Unexpected error in resume optimization: {e}")
        import traceback
        traceback.print_exc()
        # Try to refund credits on unexpected error
        refund_success = False
        try:
            refund_success, _ = refund_credits_atomic(user_id, OPTIMIZATION_CREDIT_COST, "resume_optimization_refund")
            logger.error(f"[JobBoard] Refunded credits due to unexpected error: {refund_success}")
        except Exception as refund_error:
            logger.error(f"[JobBoard] Failed to refund credits: {refund_error}")
        return jsonify({
            "error": True,
            "error_code": "ai_error",
            "message": ERROR_MESSAGES.get("ai_error"),
            "credits_refunded": refund_success
        }), get_status_code("ai_error")


@job_board_bp.route("/resume-capabilities", methods=["GET"])
@require_firebase_auth
def get_resume_capabilities_endpoint():
    """
    Get the current user's resume capabilities for frontend UX decisions.
    
    Returns:
        - hasResume: bool
        - resumeFileName: str
        - resumeFileType: str (pdf, docx, doc)
        - resumeCapabilities: dict with available modes
    """
    try:
        uid = request.firebase_user.get('uid')
        
        db = get_db()
        user_doc = db.collection('users').document(uid).get()
        
        if not user_doc.exists:
            return jsonify({"error": "User not found"}), 404
        
        user_data = user_doc.to_dict()
        
        # Get file type, defaulting to pdf for old uploads
        file_type = user_data.get('resumeFileType', 'pdf')
        
        # Get capabilities - either stored or computed
        capabilities = user_data.get('resumeCapabilities')
        if not capabilities:
            # Compute capabilities for old uploads that don't have them stored
            capabilities = get_capabilities(file_type)
            # Add available modes (inline logic to avoid importing private function)
            modes = []
            if file_type == 'docx':
                modes.append({
                    'id': 'direct_edit',
                    'name': 'Format-Preserving Optimization',
                    'description': 'Optimize content while keeping your exact formatting, fonts, and layout.',
                    'recommended': True,
                    'preservesFormatting': True,
                })
            if file_type in ['pdf', 'doc']:
                modes.append({
                    'id': 'suggestions',
                    'name': 'Suggestions Mode',
                    'description': 'Get specific ATS improvements to apply yourself. Your original formatting stays intact.',
                    'recommended': file_type == 'pdf',
                    'preservesFormatting': True,
                })
            modes.append({
                'id': 'template_rebuild',
                'name': 'Template Rebuild',
                'description': 'Rebuild your resume in a clean, ATS-optimized template with fully optimized content.',
                'recommended': False,
                'preservesFormatting': False,
            })
            capabilities['availableModes'] = modes
        
        return jsonify({
            "hasResume": bool(user_data.get('resumeUrl')),
            "resumeFileName": user_data.get('resumeFileName', 'resume.pdf'),
            "resumeFileType": file_type,
            "resumeCapabilities": capabilities,
            "resumeUploadedAt": user_data.get('resumeUploadedAt')
        })
        
    except Exception as e:
        logger.error(f"[ResumeCapabilities] Error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@job_board_bp.route("/optimize-resume-v2", methods=["POST"])
@require_firebase_auth
def optimize_resume_v2():
    """
    Optimize resume with explicit mode selection.
    
    Request body:
        - jobDescription: str (required, min 50 chars)
        - mode: str (required) - 'direct_edit', 'suggestions', or 'template_rebuild'
        - jobTitle: str (optional)
        - company: str (optional)
        - jobUrl: str (optional) - will be parsed if provided
    
    Returns:
        - For direct_edit: PDF file download
        - For suggestions: JSON with suggestions
        - For template_rebuild: JSON with structured content
    """
    import tempfile
    from flask import Response
    
    user_id = None
    file_path = None
    
    try:
        # Get request data
        data = request.get_json(force=True, silent=True) or {}
        user_id = request.firebase_user.get('uid')
        
        job_url = data.get("jobUrl")
        job_description = data.get("jobDescription", "")
        job_title = data.get("jobTitle", "")
        company = data.get("company", "")
        mode = data.get("mode")
        
        logger.info(f"\n[JobBoardV2] ========================================")
        logger.info(f"[JobBoardV2] Resume Optimization V2 Request")
        logger.info(f"[JobBoardV2] User: {user_id}")
        logger.info(f"[JobBoardV2] Mode: {mode}")
        logger.info(f"[JobBoardV2] Job: {job_title} at {company}")
        logger.info(f"[JobBoardV2] ========================================\n")
        
        # Parse job URL if provided
        if job_url:
            try:
                parsed_job = parse_job_url(job_url)
                if parsed_job:
                    if parsed_job.get("title"):
                        job_title = parsed_job.get("title")
                    if parsed_job.get("company"):
                        company = parsed_job.get("company")
                    if parsed_job.get("description") and len(parsed_job.get("description", "").strip()) >= 50:
                        job_description = parsed_job.get("description")
            except Exception as url_error:
                logger.error(f"[JobBoardV2] Error parsing URL (non-fatal): {url_error}")
        
        # Validate inputs
        if not job_description or len(job_description.strip()) < 50:
            return jsonify({
                "error": "Job description is required (minimum 50 characters)"
            }), 400
        
        if not mode:
            return jsonify({
                "error": "Optimization mode is required. Choose: direct_edit, suggestions, or template_rebuild"
            }), 400
        
        if mode not in ['direct_edit', 'suggestions', 'template_rebuild']:
            return jsonify({
                "error": f"Invalid mode: {mode}. Choose: direct_edit, suggestions, or template_rebuild"
            }), 400
        
        # Get user data
        db = get_db()
        if not db:
            return jsonify({"error": "Database not available"}), 500
        
        user_ref = db.collection("users").document(user_id)
        user_doc = user_ref.get()
        
        if not user_doc.exists:
            return jsonify({"error": "User not found"}), 404
        
        user_data = user_doc.to_dict()
        
        # Check credits
        current_credits = check_and_reset_credits(user_ref, user_data)
        
        if current_credits < OPTIMIZATION_CREDIT_COST:
            return jsonify({
                "error": "Insufficient credits",
                "required": OPTIMIZATION_CREDIT_COST,
                "available": current_credits
            }), 402
        
        # Get resume info
        resume_url = user_data.get("resumeUrl")
        resume_file_type = user_data.get("resumeFileType", "pdf")
        
        if not resume_url:
            return jsonify({
                "error": "No resume found. Please upload a resume first."
            }), 400
        
        # Validate mode for file type
        if mode == "direct_edit" and resume_file_type != "docx":
            return jsonify({
                "error": f"Direct edit mode requires a DOCX file. Your current resume is a {resume_file_type.upper()}. Please upload a DOCX resume or choose 'suggestions' mode.",
                "currentFileType": resume_file_type,
                "recommendedMode": "suggestions"
            }), 400
        
        # Deduct credits BEFORE optimization
        logger.info(f"[JobBoardV2] Deducting {OPTIMIZATION_CREDIT_COST} credits...")
        success, new_credits = deduct_credits_atomic(user_id, OPTIMIZATION_CREDIT_COST, "resume_optimization_v2")
        
        if not success:
            return jsonify({
                "error": "Insufficient credits",
                "required": OPTIMIZATION_CREDIT_COST,
                "available": new_credits
            }), 402
        
        logger.info(f"[JobBoardV2] Credits: {current_credits} → {new_credits}")
        
        # Download resume to temp file
        logger.info(f"[JobBoardV2] Downloading resume from {resume_url[:80]}...")
        
        resume_response = requests.get(resume_url, timeout=30, headers={"User-Agent": "Offerloop/1.0"})
        resume_response.raise_for_status()
        
        # Save to temp file with correct extension
        suffix = f".{resume_file_type}"
        with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as f:
            f.write(resume_response.content)
            file_path = f.name
        
        logger.info(f"[JobBoardV2] Resume saved to {file_path}")
        
        try:
            # Get OpenAI client
            openai_client = get_openai_client()
            if not openai_client:
                raise Exception("OpenAI client not available")
            
            # Run optimization
            logger.info(f"[JobBoardV2] Running optimization (mode: {mode})...")
            
            pdf_bytes, metadata = run_resume_optimization(
                file_path=file_path,
                file_type=resume_file_type,
                job_description=job_description,
                openai_client=openai_client,
                mode=mode,
                job_title=job_title,
                company=company
            )
            
            logger.info(f"[JobBoardV2]  Optimization complete!")
            
            # Clean up temp file
            try:
                if file_path and os.path.exists(file_path):
                    os.unlink(file_path)
            except Exception as e:
                logger.warning(f"[JobBoardV2] Warning: Could not delete temp file: {e}")
            
            # Return based on mode
            if pdf_bytes:
                # Direct edit mode - return PDF file
                logger.info(f"[JobBoardV2] Returning PDF ({len(pdf_bytes)} bytes)")
                
                return Response(
                    pdf_bytes,
                    mimetype="application/pdf",
                    headers={
                        "Content-Disposition": f'attachment; filename=optimized_resume_{company or "ats"}.pdf',
                        "X-Optimization-Mode": mode,
                        "X-Credits-Used": str(OPTIMIZATION_CREDIT_COST),
                        "X-Credits-Remaining": str(new_credits),
                        "X-Replacements-Made": str(metadata.get("replacements_made", 0))
                    }
                )
            else:
                # Suggestions or template rebuild - return JSON
                logger.info(f"[JobBoardV2] Returning JSON response")
                
                return jsonify({
                    **metadata,
                    "creditsUsed": OPTIMIZATION_CREDIT_COST,
                    "creditsRemaining": new_credits
                })
                
        except Exception as e:
            # Refund credits on failure
            logger.error(f"[JobBoardV2]  Optimization error: {e}")
            logger.info(f"[JobBoardV2] Refunding {OPTIMIZATION_CREDIT_COST} credits...")
            refund_success, _ = refund_credits_atomic(user_id, OPTIMIZATION_CREDIT_COST, "resume_optimization_v2_refund")
            logger.info(f"[JobBoardV2] Refunded: {refund_success}")
            
            import traceback
            traceback.print_exc()
            
            return jsonify({
                "error": f"Optimization failed: {str(e)}",
                "creditsRefunded": True
            }), 500
            
    except Exception as e:
        logger.error(f"[JobBoardV2] Error: {e}")
        import traceback
        traceback.print_exc()
        
        # Try to refund credits
        if user_id:
            try:
                refund_success, _ = refund_credits_atomic(user_id, OPTIMIZATION_CREDIT_COST, "resume_optimization_v2_refund")
                logger.info(f"[JobBoardV2] Refunded credits: {refund_success}")
            except Exception as refund_error:
                logger.error(f"[JobBoardV2] Failed to refund: {refund_error}")
        
        return jsonify({"error": str(e)}), 500
        
    finally:
        # Cleanup temp file
        if file_path and os.path.exists(file_path):
            try:
                os.unlink(file_path)
            except Exception as cleanup_error:
                logger.error(f"[JobBoardV2] Cleanup error: {cleanup_error}")


def normalize_company_name(company: str) -> str:
    """
    Normalize company names to prefer recognizable brand names over parent company names.
    Maps parent companies to their well-known brand names.
    """
    if not company:
        return company
    
    company_lower = company.lower().strip()
    
    # Map parent companies to their recognizable brand names
    company_mappings = {
        'sie': 'Playstation',  # Sony Interactive Entertainment -> Playstation
        'sony interactive entertainment': 'Playstation',
        'meta': 'Meta',  # Keep as is
        'facebook': 'Meta',
        'alphabet': 'Google',
        'google llc': 'Google',
        'amazon.com services llc': 'Amazon',
        'amazon web services': 'AWS',
        'microsoft corporation': 'Microsoft',
        'apple inc': 'Apple',
        'apple computer': 'Apple',
    }
    
    # Check for exact matches first
    if company_lower in company_mappings:
        return company_mappings[company_lower]
    
    # Check for partial matches (e.g., "SIE" in "SIE America")
    for parent, brand in company_mappings.items():
        if parent in company_lower:
            return brand
    
    return company


def extract_job_details_with_openai(job_description: str) -> Optional[Dict[str, str]]:
    """
    Use OpenAI to extract company name, job title, and location from job description.
    This is the primary extraction method for job details.
    """
    try:
        client = get_openai_client()
        if not client:
            logger.info("[FindRecruiter] OpenAI client not available")
            return None
        
        # Truncate description if too long (keep first 4000 chars for better context)
        truncated_desc = job_description[:4000] if len(job_description) > 4000 else job_description
        
        prompt = f"""Extract the following information from this job posting. Be precise and accurate.

Job Description:
{truncated_desc}

Extract these three pieces of information:
1. company: The primary company name hiring for this position (e.g., "Amazon", "Google", "Microsoft", "Netflix", "Playstation"). 
   - Look for the company name in the header, title, or first few lines
   - PREFER recognizable brand names over parent company names:
     * If you see "Sony Interactive Entertainment" or "SIE", return "Playstation"
     * If you see "Alphabet", return "Google"
     * If you see "Amazon.com Services LLC" or "Amazon", return "Amazon"
     * If you see "Facebook", return "Meta"
   - Ignore generic words like "Employer", "Company", "Organization"
   - Return null ONLY if absolutely no company name can be found

2. job_title: The exact job title/position name (e.g., "Software Development Engineer Intern", "Data Scientist Intern", "Software Engineer")
   - Return null if not found

3. location: The job location in "City, State" format (e.g., "Seattle, WA", "San Francisco, CA", "Los Angeles, CA")
   - Look for location in the header, job details section, or requirements
   - If multiple locations are listed, use the primary one
   - Return null if not found

Return ONLY a valid JSON object in this exact format with no markdown, no code blocks, no explanation:
{{"company": "Company Name or null", "job_title": "Job Title or null", "location": "City, State or null"}}"""

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You are a JSON extraction assistant. Extract job details from job postings. Return only valid JSON with no explanation or markdown."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=200,
            temperature=0.1
        )
        
        result_text = response.choices[0].message.content.strip()
        
        # Remove markdown code blocks if present
        if '```' in result_text:
            result_text = result_text.split('```')[1]
            if result_text.startswith('json'):
                result_text = result_text[4:]
            result_text = result_text.strip()
        
        # Try to extract JSON from response
        import json
        json_match = re.search(r'\{[^{}]*\}', result_text)
        if json_match:
            result = json.loads(json_match.group())
        else:
            result = json.loads(result_text)
        
        # Validate and clean the result
        extracted = {}
        if result.get('company') and result['company'].lower() not in ['null', 'none', 'n/a', '']:
            company_name = result['company'].strip()
            # Normalize company name to prefer brand names over parent companies
            extracted['company'] = normalize_company_name(company_name)
        if result.get('job_title') and result['job_title'].lower() not in ['null', 'none', 'n/a', '']:
            extracted['job_title'] = result['job_title'].strip()
        if result.get('location') and result['location'].lower() not in ['null', 'none', 'n/a', '']:
            extracted['location'] = result['location'].strip()
        
        return extracted if extracted else None
        
    except json.JSONDecodeError as e:
        logger.error(f"[FindRecruiter] Failed to parse OpenAI JSON response: {e}")
        logger.info(f"[FindRecruiter] Response was: {result_text[:200]}")
        return None
    except Exception as e:
        logger.error(f"[FindRecruiter] OpenAI extraction error: {e}")
        return None


def _check_find_humans_hourly_cap(user_id: str) -> bool:
    """
    Per-user hourly cap for the Find the Humans flow (20/hour).

    Uses the existing firestore-backed Flask-Limiter storage so the counter
    is shared across gunicorn workers. Fail-open on storage errors so a
    Firestore outage doesn't block users.
    """
    try:
        lim = get_limiter()
        if not lim or not getattr(lim, "_storage", None):
            return True
        from limits import parse
        from limits.strategies import FixedWindowRateLimiter
        item = parse("20 per hour")
        strategy = FixedWindowRateLimiter(lim._storage)
        return strategy.hit(item, "find-humans", user_id or "anon")
    except Exception as e:
        logger.error(f"[FindHumans] rate-limit check failed: {e}")
        return True


def _check_user_rate_limit(user_id: str, scope: str, limit_str: str) -> bool:
    """Generic per-user rate-limit check backed by the Flask-Limiter storage.

    Added as part of Phase 1 of the Job Board Elevation Plan to cap abuse on
    expensive endpoints (recruiter/hiring-manager discovery, job-feed refresh).
    Returns True if the request is allowed, False if it should be rejected
    with HTTP 429. Fail-open on storage errors so a Firestore outage doesn't
    block users.
    """
    try:
        lim = get_limiter()
        if not lim or not getattr(lim, "_storage", None):
            return True
        from limits import parse
        from limits.strategies import FixedWindowRateLimiter
        item = parse(limit_str)
        strategy = FixedWindowRateLimiter(lim._storage)
        return strategy.hit(item, scope, user_id or "anon")
    except Exception as e:
        logger.error(f"[RateLimit] {scope} check failed: {e}")
        return True


def _feed_warehouse(people: list, tag: str) -> None:
    """Every recruiter / hiring manager / employee a search turns up becomes
    a permanent warehouse row, same as People search (flag-gated by
    ENABLE_FIRM_CACHE_WRITE, async). Never breaks the search."""
    try:
        from app.services.firm_cache.writer import cache_pdl_contacts
        cache_pdl_contacts(people or [], shape="app", async_write=True)
    except Exception:
        logger.warning(f"[{tag}] warehouse feed failed")


@job_board_bp.route("/find-recruiter", methods=["POST"])
@require_firebase_auth
@require_tier(['pro', 'elite'])
def find_recruiter_endpoint():
    """
    Find recruiters at a company for a specific job.

    Request:
        {
            "company": "Google",
            "jobTitle": "Software Engineer",
            "jobDescription": "...",  # Optional
            "jobType": "engineering",  # Optional, will be auto-detected
            "location": "San Francisco, CA",  # Optional
            "no_parse": true,  # Optional: skip parse_job_url + OpenAI extraction
            "source": "find_humans"  # Optional: marks request as Find the Humans flow
        }

    Response:
        {
            "recruiters": [...],
            "jobTypeDetected": "engineering",
            "companyCleaned": "Google LLC",
            "totalFound": 12,
            "creditsCharged": 75,
            "creditsRemaining": 135
        }
    """
    try:
        user_id = request.firebase_user.get('uid')
        data = request.get_json(force=True, silent=True) or {}

        # ------------------------------------------------------------------
        # Find the Humans: per-user hourly cap.
        # Applies when the request is tagged with source='find_humans'.
        # ------------------------------------------------------------------
        is_find_humans = data.get('source') == 'find_humans'
        if is_find_humans:
            if not _check_find_humans_hourly_cap(user_id):
                return jsonify({
                    "error": "Hourly limit reached",
                    "message": "You've used Find the Humans 20 times this hour. Try again later."
                }), 429

        # Phase 1 (Job Board Elevation Plan): daily cap on recruiter discovery.
        # Each call fans out to PDL + Hunter (5 app credits but ~$10-15 in
        # external API spend pre-fixes). 100/day is comfortably above any
        # legitimate user pattern but stops runaway clients.
        if not _check_user_rate_limit(user_id, "find-recruiter-daily", "100 per day"):
            return jsonify({
                "error": "Daily limit reached",
                "message": "You've used Find Recruiter 100 times today. Try again tomorrow."
            }), 429

        no_parse = bool(data.get('no_parse'))

        # Get job information from various sources
        company = data.get('company')
        job_title = data.get('jobTitle', '')
        job_description = data.get('jobDescription', '')
        job_type = data.get('jobType')  # Optional
        location = data.get('location')  # Optional
        job_url = data.get('jobUrl')  # Optional - for external job links

        # Log initial values for debugging
        logger.info(f"[FindRecruiter] Initial values - company: '{company}', job_title: '{job_title}', location: '{location}', has_description: {bool(job_description)}, has_url: {bool(job_url)}, no_parse: {no_parse}")

        # Reject obviously invalid company names from request
        invalid_company_names = {'job type', 'job details', 'job description', 'employer', 'company', 'organization', 'n/a', 'null', 'none', ''}
        if company and company.lower().strip() in invalid_company_names:
            logger.warning(f"[FindRecruiter] Rejecting invalid company name from request: '{company}'")
            company = None
        elif company:
            # Normalize company name from user input
            company = normalize_company_name(company)

        # Parse job URL to fill in missing fields (company, title, location, description)
        # Skipped entirely when no_parse=true (Find the Humans path).
        if job_url and not no_parse:
            try:
                parsed_job = parse_job_url(job_url)
                if parsed_job:
                    if parsed_job.get('company') and not company:
                        company = normalize_company_name(parsed_job.get('company'))
                    if parsed_job.get('title') and not job_title:
                        job_title = parsed_job.get('title')
                    if parsed_job.get('location') and not location:
                        location = parsed_job.get('location')
                    if parsed_job.get('description') and not job_description:
                        job_description = parsed_job.get('description')
                    logger.info(f"[FindRecruiter] URL parsed: company={bool(parsed_job.get('company'))}, title={bool(parsed_job.get('title'))}, desc={bool(parsed_job.get('description'))}")
                else:
                    logger.warning(f"[FindRecruiter] URL parsing returned no data for: {job_url}")
            except Exception as e:
                logger.error(f"[FindRecruiter] Error parsing job URL: {e}")

        # Use OpenAI to extract missing information from job description.
        # Skipped entirely when no_parse=true (Find the Humans path) — Job
        # Board cards already carry structured fields and the call is the
        # main parser-brittleness drop-off point we are bypassing.
        if job_description and not no_parse:
            logger.info(f"[FindRecruiter] Using OpenAI to extract job details from description (length: {len(job_description)})...")
            try:
                extracted = extract_job_details_with_openai(job_description)
                if extracted:
                    logger.info(f"[FindRecruiter] OpenAI extraction result: {extracted}")
                    # Always use OpenAI extracted values (they're more reliable than URL parsing or user input)
                    # Override existing values even if they exist, because OpenAI is more accurate
                    if extracted.get('company'):
                        company = extracted['company']  # Already normalized in extract_job_details_with_openai
                        logger.info(f"[FindRecruiter]  OpenAI extracted company: '{company}'")
                    if extracted.get('job_title'):
                        job_title = extracted['job_title']
                        logger.info(f"[FindRecruiter]  OpenAI extracted job_title: '{job_title}'")
                    if extracted.get('location'):
                        location = extracted['location']
                        logger.info(f"[FindRecruiter]  OpenAI extracted location: '{location}'")
                else:
                    logger.info(f"[FindRecruiter]  OpenAI extraction returned None or empty result")
            except Exception as e:
                logger.error(f"[FindRecruiter]  OpenAI extraction failed: {e}")
                import traceback
                traceback.print_exc()
        elif no_parse:
            logger.info(f"[FindRecruiter] no_parse=true, skipping OpenAI extraction")
        else:
            logger.info(f"[FindRecruiter]  No job description provided, skipping OpenAI extraction")
        
        # Final validation - reject invalid company names (common false positives)
        invalid_company_names = {
            'job type', 'job details', 'job description', 'job title', 'job location',
            'employer', 'company', 'organization', 'corporation', 
            'n/a', 'null', 'none', '', 'full-time', 'part-time', 'contract',
            'remote', 'hybrid', 'on-site', 'location', 'details', 'description'
        }
        if company:
            company_lower = company.lower().strip()
            if company_lower in invalid_company_names or len(company_lower) < 2:
                logger.warning(f"[FindRecruiter]  Final validation: Rejecting invalid company name: '{company}'")
                company = None
        
        logger.info(f"[FindRecruiter] Final values before validation - company: '{company}', job_title: '{job_title}', location: '{location}'")
        
        # Validate required fields
        if not company:
            return jsonify({
                "error": "Company name is required",
                "suggestion": "Please provide a company name, paste a job URL, or include the company name in the job description."
            }), 400
        
        # Check user credits (minimum RECRUITER_CREDIT_COST for 1 contact)
        db = get_db()
        if not db:
            return jsonify({"error": "Database not available"}), 500
            
        user_ref = db.collection('users').document(user_id)
        user_doc = user_ref.get()
        
        if not user_doc.exists:
            return jsonify({"error": "User not found"}), 404
        
        user_data = user_doc.to_dict()
        user_data = sanitize_firestore_data(user_data, depth=0, max_depth=20)
        current_credits = check_and_reset_credits(user_ref, user_data)
        
        if current_credits < RECRUITER_CREDIT_COST:
            return jsonify({
                "error": "Insufficient credits",
                "creditsRequired": RECRUITER_CREDIT_COST,
                "creditsAvailable": current_credits
            }), 402
        
        # Get user's requested max_results. Cap lowered 10 -> 4 (2026-06)
        # because the unified referral-draft workflow proved that beyond 3-4
        # recruiters per company, click-through drops sharply AND each extra
        # PDL record is another billed credit. Default 3 to match the SPA's
        # FindHumansModal usage.
        try:
            max_results_requested = int(data.get('maxResults', 3))
        except (TypeError, ValueError):
            max_results_requested = 3
        max_results_requested = min(max(max_results_requested, 1), 4)
        
        # Calculate how many recruiters user can afford (RECRUITER_CREDIT_COST per recruiter)
        max_affordable = current_credits // RECRUITER_CREDIT_COST
        # Use the minimum of what user requested and what they can afford
        max_results_to_fetch = min(max_results_requested, max_affordable)
        
        # Get user's resume and contact info for email generation
        user_resume = user_data.get('resumeParsed', {})
        resume_text = user_data.get('resumeText', '')
        resume_phone = user_resume.get('contact', {}).get('phone', '') if isinstance(user_resume.get('contact'), dict) else ''
        resume_linkedin = user_resume.get('contact', {}).get('linkedin', '') if isinstance(user_resume.get('contact'), dict) else ''
        user_contact = {
            "name": user_resume.get('name', user_data.get('displayName', '')),
            "email": get_outreach_email(user_data),
            "phone": resume_phone or user_data.get('phone', ''),
            "linkedin": resume_linkedin or user_data.get('linkedin', '')
        }

        # Check if user wants to generate emails (default to True)
        generate_emails = data.get('generateEmails', True)
        create_drafts = data.get('createDrafts', True)

        # Find recruiters - use the requested amount (limited by affordability)
        result = find_recruiters(
            company_name=company,
            job_type=job_type,
            job_title=job_title,
            job_description=job_description,
            location=location,
            max_results=max_results_to_fetch,  # Use requested amount (limited by credits)
            generate_emails=generate_emails,
            user_resume=user_resume,
            user_contact=user_contact,
            resume_text=resume_text
        )
        
        # Check if we got results
        if result.get("error"):
            return jsonify({
                "error": result["error"],
                "recruiters": [],
                "requestedCount": max_results_requested,
                "foundCount": 0,
                "creditsCharged": 0
            }), 500
        
        # Get results (already limited by max_results_to_fetch)
        all_recruiters = result.get("recruiters", [])
        all_emails = result.get("emails", [])
        total_found = result.get("total_found", 0)
        _feed_warehouse(all_recruiters, "FindRecruiter")
        # Results are already limited to max_results_to_fetch, but we still need to respect credits
        affordable_recruiters = all_recruiters[:max_affordable]
        
        # Limit emails to match affordable recruiters
        # Match by email address (could be Email or WorkEmail field)
        affordable_emails = []
        if all_emails:
            # Build set of all possible emails for affordable recruiters
            affordable_recruiter_emails = set()
            for r in affordable_recruiters:
                if r.get("Email") and r.get("Email") != "Not available":
                    affordable_recruiter_emails.add(r.get("Email"))
                if r.get("WorkEmail") and r.get("WorkEmail") != "Not available":
                    affordable_recruiter_emails.add(r.get("WorkEmail"))
            
            # Filter emails to only those matching affordable recruiters
            affordable_emails = [e for e in all_emails if e.get("to_email") in affordable_recruiter_emails]
        
        # Check if there are more available but user ran out of credits
        has_more = len(all_recruiters) > max_affordable or total_found > max_affordable
        credits_needed_for_more = (len(all_recruiters) - max_affordable) * RECRUITER_CREDIT_COST if has_more else 0

        # Deduct credits atomically BEFORE creating Gmail drafts (prevents TOCTOU)
        credits_charged = RECRUITER_CREDIT_COST * len(affordable_recruiters)
        if credits_charged > 0:
            success, new_balance = deduct_credits_atomic(user_id, credits_charged, "find_recruiter")
            if not success:
                return jsonify({
                    "error": "Insufficient credits",
                    "creditsRequired": credits_charged,
                    "creditsAvailable": 0
                }), 402
        else:
            new_balance = current_credits

        # Create Gmail drafts if requested (after credits are deducted)
        drafts_created = []
        if create_drafts and affordable_emails:
            from app.services.gmail_client import _load_user_gmail_creds, get_gmail_service_for_user
            from app.services.gmail_client import download_resume_from_url
            import base64
            from email.mime.multipart import MIMEMultipart
            from email.mime.text import MIMEText
            from email.mime.base import MIMEBase
            from email import encoders

            # Check Gmail connection - credentials are stored in integrations/gmail subcollection
            gmail_creds = _load_user_gmail_creds(user_id)
            resume_url = user_data.get('resumeURL') or user_data.get('resumeUrl')

            if gmail_creds:
                # Download resume once for all drafts
                resume_content = None
                resume_filename = None
                if resume_url:
                    try:
                        resume_content, resume_filename = download_resume_from_url(resume_url)
                        stored_filename = user_data.get('resumeFileName')
                        if stored_filename:
                            resume_filename = stored_filename
                        if resume_content:
                            logger.info(f"[FindRecruiter] Downloaded resume ({len(resume_content)} bytes, filename={resume_filename}) for {len(affordable_emails)} drafts")
                    except Exception as e:
                        logger.error(f"[FindRecruiter] Failed to download resume: {e}")

                # Get Gmail service
                gmail_service = get_gmail_service_for_user(user_data.get('email'), user_id=user_id)

                if gmail_service:
                    import re as _re
                    _EMAIL_RE = _re.compile(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$')

                    for email_data in affordable_emails:
                        try:
                            recruiter = email_data.get("recruiter", {})
                            to_email = email_data.get("to_email")
                            if not to_email:
                                continue
                            # Validate email format before creating draft
                            if not _EMAIL_RE.match(to_email):
                                logger.info(f"[FindRecruiter] Skipping invalid email: {to_email[:50]}")
                                continue
                            to_name = email_data.get("to_name", "")
                            subject = email_data.get("subject", "")
                            body_html = email_data.get("body", "")
                            body_plain = email_data.get("plain_body", "")

                            # Sanitize to_name to prevent header injection
                            to_name = to_name.replace('"', '').replace('\n', '').replace('\r', '')

                            # Create MIME message (mixed for attachment support)
                            message = MIMEMultipart('mixed')
                            message['to'] = f'"{to_name}" <{to_email}>' if to_name else to_email
                            message['subject'] = subject

                            # Add text/html alternative part
                            alt = MIMEMultipart('alternative')
                            alt.attach(MIMEText(body_plain, 'plain'))
                            from app.services import email_tracking
                            tracking_token = email_tracking.start(user_id)
                            alt.attach(MIMEText(email_tracking.add_pixel(body_html, tracking_token), 'html'))
                            message.attach(alt)

                            # Add resume attachment if available
                            if resume_content and resume_filename:
                                part = MIMEBase('application', 'pdf')
                                part.set_payload(resume_content)
                                encoders.encode_base64(part)
                                part.add_header('Content-Disposition', f'attachment; filename="{resume_filename}"')
                                message.attach(part)

                            # Encode message
                            raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')

                            # Create draft
                            draft_body = {
                                'message': {
                                    'raw': raw_message
                                }
                            }

                            draft = gmail_service.users().drafts().create(userId='me', body=draft_body).execute()
                            email_tracking.register(tracking_token, user_id, 'hiring_manager', recipient_email=to_email, draft_id=draft.get('id'), message_id=draft.get('message', {}).get('id'), thread_id=draft.get('message', {}).get('threadId'))
                            draft_id = draft['id']

                            # Get Gmail account email for URL
                            try:
                                profile = gmail_service.users().getProfile(userId='me').execute()
                                connected_email = profile.get('emailAddress', '')
                            except:
                                connected_email = user_data.get('email', '')

                            draft_url = f"https://mail.google.com/mail/?authuser={connected_email}#draft/{draft_id}" if connected_email else f"https://mail.google.com/mail/u/0/#draft/{draft_id}"

                            drafts_created.append({
                                "recruiter_email": to_email,
                                "draft_id": draft_id,
                                "draft_url": draft_url
                            })

                            logger.info(f"[FindRecruiter] Created Gmail draft for {to_email}")

                        except Exception as e:
                            logger.error(f"[FindRecruiter] Failed to create draft for {email_data.get('to_email')}: {e}")
                else:
                    logger.info(f"[FindRecruiter] Gmail service not available")
            else:
                logger.info(f"[FindRecruiter] Gmail not connected (no credentials found in integrations/gmail), skipping draft creation")
                logger.info(f"[FindRecruiter] User ID: {user_id}")

        # Phase 6 (June 2026): unified referral-draft workflow.
        # Cache the validated recruiter list so the new
        # /referral-draft/from-find-recruiter endpoint can read it back
        # (server-side trust boundary — clients never send contact fields).
        # 60-min TTL matches discovery_cache; cache_doc keyed per (uid,
        # company, jobTitle, hour-bucket) so the same search within the
        # cache window collides cleanly.
        from app.services.alumni_discovery import (
            _generate_search_id as _gen_recruiter_search_id,
            write_recruiter_cache,
        )
        recruiter_search_id = _gen_recruiter_search_id(
            user_id, company, job_title or "",
        )
        try:
            write_recruiter_cache(user_id, recruiter_search_id, {
                "search_id": recruiter_search_id,
                "company": result.get("company_cleaned") or company,
                "job_title": job_title or "",
                "recruiters": affordable_recruiters,
            })
        except Exception as _cache_err:
            # Cache write is best-effort — never fail the search over it.
            logger.warning(
                "[FindRecruiter] recruiter_cache write failed uid=%s: %s",
                user_id, _cache_err,
            )

        response = {
            "recruiters": affordable_recruiters,
            "emails": affordable_emails,
            "draftsCreated": drafts_created,
            "jobTypeDetected": result["job_type_detected"],
            "companyCleaned": result["company_cleaned"],
            "searchTitles": result["search_titles"],
            "totalFound": total_found,
            "requestedCount": max_results_requested,
            "foundCount": len(affordable_recruiters),
            "creditsCharged": credits_charged,
            "creditsRemaining": new_balance,
            "message": result.get("message"),
            "searchId": recruiter_search_id,
        }
        
        # Add message if there are more available
        if has_more and len(affordable_recruiters) > 0:
            response["hasMore"] = True
            response["moreAvailable"] = len(all_recruiters) - max_affordable
            response["creditsNeededForMore"] = credits_needed_for_more
            response["message"] = f"Found {len(affordable_recruiters)} recruiter(s). {len(all_recruiters) - max_affordable} more available but you need {credits_needed_for_more} more credits."
        elif has_more and len(affordable_recruiters) == 0:
            response["hasMore"] = True
            response["moreAvailable"] = total_found
            response["creditsNeededForMore"] = total_found * RECRUITER_CREDIT_COST
            response["message"] = f"Found {total_found} recruiter(s) but you need {total_found * RECRUITER_CREDIT_COST} credits to view them. You currently have {current_credits} credits."
        
        return jsonify(response)
        
    except Exception as e:
        logger.error(f"[FindRecruiter] Error: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


def derive_employee_titles(job_title: str, company: str, job_description: str = "") -> List[str]:
    """
    Use OpenAI to derive 3 to 5 peer / teammate job titles worth a coffee chat on
    the team behind a posting (the Find People flow). Excludes recruiter / HR and
    people-manager titles (those have their own buttons) and the posting's own
    junior / intern level. Returns a bounded, de-duplicated list, or [] when the
    model is unavailable or returns nothing usable (the caller handles fallback).

    Mirrors the OpenAI usage pattern in extract_job_details_with_openai.
    """
    try:
        client = get_openai_client()
        if not client:
            logger.info("[FindEmployee] OpenAI client not available for title derivation")
            return []

        truncated_desc = (job_description or "")[:4000]
        prompt = f"""A candidate wants to network with the right people on the team behind this job posting before applying. Identify the individual contributors and adjacent teammates they should reach out to for a coffee chat.

Company: {company}
Posting title: {job_title}
Job description:
{truncated_desc or "(no description provided)"}

Return 3 to 5 specific job titles of likely teammates and adjacent individual contributors worth a coffee chat. Rules:
- EXCLUDE recruiter, talent acquisition, HR, sourcing, and "people" titles.
- EXCLUDE people-manager titles (anything with Manager, Director, Head, VP, Chief, or President).
- EXCLUDE the posting's own intern / junior level so you do not just return more people at the level being hired. Example: for a "Data Science Intern", return "Data Scientist", "Senior Data Scientist", "Data Engineer", "Machine Learning Engineer", NOT "Data Science Intern".
- Prefer current individual-contributor titles in the same and directly adjacent functions.

Return ONLY a valid JSON object, no markdown, no code blocks, in this exact format:
{{"titles": ["Title One", "Title Two", "Title Three"]}}"""

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "You map a job posting to the individual-contributor teammates a candidate should network with. Return only valid JSON with no explanation or markdown."},
                {"role": "user", "content": prompt}
            ],
            max_tokens=200,
            temperature=0.2
        )

        result_text = response.choices[0].message.content.strip()
        if '```' in result_text:
            result_text = result_text.split('```')[1]
            if result_text.startswith('json'):
                result_text = result_text[4:]
            result_text = result_text.strip()

        import json
        json_match = re.search(r'\{.*\}', result_text, re.DOTALL)
        parsed = json.loads(json_match.group() if json_match else result_text)

        raw_titles = parsed.get("titles", []) if isinstance(parsed, dict) else []
        cleaned: List[str] = []
        seen = set()
        for t in raw_titles:
            if not isinstance(t, str):
                continue
            t = t.strip()
            key = t.lower()
            if t and key not in seen and key not in ('null', 'none', 'n/a'):
                seen.add(key)
                cleaned.append(t)
        return cleaned[:5]
    except Exception as e:
        logger.error(f"[FindEmployee] Title derivation failed: {e}")
        return []


def _fallback_employee_titles(job_title: str) -> List[str]:
    """
    Heuristic fallback when LLM title derivation is unavailable: strip the
    junior / intern qualifiers from the posting title to get a broader base role.
    Returns a single base title, or [] if nothing usable remains.
    """
    if not job_title:
        return []
    base = job_title
    for token in ["Intern", "Internship", "Co-op", "Coop", "Junior", "Jr.", "Jr",
                  "Entry Level", "Entry-Level", "New Grad", "New Graduate", "Trainee"]:
        base = re.sub(rf"\b{re.escape(token)}\b", "", base, flags=re.IGNORECASE)
    base = re.sub(r"\s+", " ", base).strip(" -,/")
    return [base] if len(base) >= 2 else []


def _create_gmail_drafts_for_emails(user_id: str, user_data: Dict, emails_to_draft: List[Dict]) -> List[Dict]:
    """
    Create Gmail drafts (with resume attachment) for a list of generated emails
    and return [{recruiter_email, draft_id, draft_url}]. Factored out of the inline
    /find-recruiter draft logic for reuse by /find-employee. Best-effort: a per
    email failure never aborts the batch, and a missing Gmail connection is a
    no-op rather than an error.
    """
    drafts_created: List[Dict] = []
    if not emails_to_draft:
        return drafts_created

    from app.services.gmail_client import _load_user_gmail_creds, get_gmail_service_for_user
    from app.services.gmail_client import download_resume_from_url
    import base64
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email.mime.base import MIMEBase
    from email import encoders

    gmail_creds = _load_user_gmail_creds(user_id)
    if not gmail_creds:
        logger.info(f"[FindEmployee] Gmail not connected for user {user_id}, skipping drafts")
        return drafts_created

    resume_url = user_data.get('resumeURL') or user_data.get('resumeUrl')
    resume_content = None
    resume_filename = None
    if resume_url:
        try:
            resume_content, resume_filename = download_resume_from_url(resume_url)
            stored_filename = user_data.get('resumeFileName')
            if stored_filename:
                resume_filename = stored_filename
        except Exception as e:
            logger.error(f"[FindEmployee] Failed to download resume: {e}")

    gmail_service = get_gmail_service_for_user(user_data.get('email'), user_id=user_id)
    if not gmail_service:
        logger.info("[FindEmployee] Gmail service not available")
        return drafts_created

    email_re = re.compile(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$')
    for email_data in emails_to_draft:
        try:
            to_email = email_data.get("to_email")
            if not to_email or not email_re.match(to_email):
                continue
            to_name = (email_data.get("to_name", "") or "").replace('"', '').replace('\n', '').replace('\r', '')
            subject = email_data.get("subject", "")
            body_html = email_data.get("body", "")
            body_plain = email_data.get("plain_body", "")

            message = MIMEMultipart('mixed')
            message['to'] = f'"{to_name}" <{to_email}>' if to_name else to_email
            message['subject'] = subject

            alt = MIMEMultipart('alternative')
            alt.attach(MIMEText(body_plain, 'plain'))
            from app.services import email_tracking
            tracking_token = email_tracking.start(user_id)
            alt.attach(MIMEText(email_tracking.add_pixel(body_html, tracking_token), 'html'))
            message.attach(alt)

            if resume_content and resume_filename:
                part = MIMEBase('application', 'pdf')
                part.set_payload(resume_content)
                encoders.encode_base64(part)
                part.add_header('Content-Disposition', f'attachment; filename="{resume_filename}"')
                message.attach(part)

            raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')
            draft = gmail_service.users().drafts().create(userId='me', body={'message': {'raw': raw_message}}).execute()
            email_tracking.register(tracking_token, user_id, 'hiring_manager', recipient_email=to_email, draft_id=draft.get('id'), message_id=draft.get('message', {}).get('id'), thread_id=draft.get('message', {}).get('threadId'))
            draft_id = draft['id']

            try:
                profile = gmail_service.users().getProfile(userId='me').execute()
                connected_email = profile.get('emailAddress', '')
            except Exception:
                connected_email = user_data.get('email', '')

            draft_url = (
                f"https://mail.google.com/mail/?authuser={connected_email}#draft/{draft_id}"
                if connected_email else f"https://mail.google.com/mail/u/0/#draft/{draft_id}"
            )
            drafts_created.append({"recruiter_email": to_email, "draft_id": draft_id, "draft_url": draft_url})
        except Exception as e:
            logger.error(f"[FindEmployee] Failed to create draft for {email_data.get('to_email')}: {e}")

    return drafts_created


@job_board_bp.route("/find-employee", methods=["POST"])
@require_firebase_auth
@require_tier(['pro', 'elite'])
def find_employee_endpoint():
    """
    Find peers / teammates (individual contributors) on the team behind a job
    posting, for coffee-chat networking. Distinct from /find-recruiter (recruiters)
    and /find-hiring-manager (the manager).

    Target titles are derived once per job by an LLM and cached on the shared job
    doc (target_titles field); later clicks on the same job skip the LLM and go
    straight to PDL. The titles feed the shared find_recruiters pipeline via
    titles_override so email / draft / receipt logic is reused. Charges
    RECRUITER_CREDIT_COST (5) per person returned.

    Request: { jobId?, company?, jobTitle?, jobDescription?, location?, maxResults? }
    Response mirrors /find-recruiter (people in the `recruiters` field).
    """
    try:
        user_id = request.firebase_user.get('uid')
        data = request.get_json(force=True, silent=True) or {}

        # Daily cap mirrors the other discovery endpoints (PDL + Hunter spend).
        if not _check_user_rate_limit(user_id, "find-employee-daily", "100 per day"):
            return jsonify({
                "error": "Daily limit reached",
                "message": "You've used Find People 100 times today. Try again tomorrow."
            }), 429

        job_id = data.get('jobId')
        company = data.get('company')
        job_title = data.get('jobTitle', '')
        job_description = data.get('jobDescription', '')
        job_type = data.get('jobType')
        location = data.get('location')

        db = get_db()
        if not db:
            return jsonify({"error": "Database not available"}), 500

        # Pull description / title / company and any cached target titles from the
        # shared job doc (server-trusted). The feed strips description_raw, so the
        # doc is the source of truth for the posting prose.
        cached_titles = None
        job_doc_ref = None
        if job_id:
            job_doc_ref = db.collection("jobs").document(job_id)
            job_doc = job_doc_ref.get()
            if job_doc.exists:
                jd = job_doc.to_dict() or {}
                if not job_description:
                    job_description = (jd.get("description_raw") or "").strip()
                if not job_title:
                    job_title = jd.get("title") or job_title
                if not company:
                    company = jd.get("company") or company
                ct = jd.get("target_titles")
                if isinstance(ct, list):
                    valid = [t.strip() for t in ct if isinstance(t, str) and t.strip()]
                    if valid:
                        cached_titles = valid

        # Normalize / validate company (mirrors /find-recruiter).
        invalid_company_names = {
            'job type', 'job details', 'job description', 'job title', 'job location',
            'employer', 'company', 'organization', 'corporation',
            'n/a', 'null', 'none', '', 'full-time', 'part-time', 'contract',
            'remote', 'hybrid', 'on-site', 'location', 'details', 'description'
        }
        if company and company.lower().strip() in invalid_company_names:
            company = None
        elif company:
            company = normalize_company_name(company)

        if not company:
            return jsonify({
                "error": "Company name is required",
                "suggestion": "Open this job from the board so we can read its company."
            }), 400

        # Resolve target titles: cached -> LLM (then cache) -> heuristic fallback.
        titles = cached_titles
        if not titles:
            titles = derive_employee_titles(job_title, company, job_description)
            if titles and job_doc_ref is not None:
                try:
                    job_doc_ref.update({
                        "target_titles": titles,
                        "target_titles_at": datetime.now(timezone.utc),
                    })
                except Exception as cache_err:
                    logger.warning(f"[FindEmployee] target_titles cache write failed job={job_id}: {cache_err}")
        if not titles:
            titles = _fallback_employee_titles(job_title)
        if not titles:
            return jsonify({
                "error": "Couldn't infer who to reach for this role. Try again."
            }), 422

        # Credits.
        user_ref = db.collection('users').document(user_id)
        user_doc = user_ref.get()
        if not user_doc.exists:
            return jsonify({"error": "User not found"}), 404

        user_data = user_doc.to_dict()
        user_data = sanitize_firestore_data(user_data, depth=0, max_depth=20)
        current_credits = check_and_reset_credits(user_ref, user_data)

        if current_credits < RECRUITER_CREDIT_COST:
            return jsonify({
                "error": "Insufficient credits",
                "creditsRequired": RECRUITER_CREDIT_COST,
                "creditsAvailable": current_credits
            }), 402

        # Requested count: 1..5, capped by affordability.
        try:
            max_results_requested = int(data.get('maxResults', 3))
        except (TypeError, ValueError):
            max_results_requested = 3
        max_results_requested = min(max(max_results_requested, 1), 5)
        max_affordable = current_credits // RECRUITER_CREDIT_COST
        max_results_to_fetch = min(max_results_requested, max_affordable)

        # Resume / contact for email generation.
        user_resume = user_data.get('resumeParsed', {})
        resume_text = user_data.get('resumeText', '')
        resume_phone = user_resume.get('contact', {}).get('phone', '') if isinstance(user_resume.get('contact'), dict) else ''
        resume_linkedin = user_resume.get('contact', {}).get('linkedin', '') if isinstance(user_resume.get('contact'), dict) else ''
        user_contact = {
            "name": user_resume.get('name', user_data.get('displayName', '')),
            "email": get_outreach_email(user_data),
            "phone": resume_phone or user_data.get('phone', ''),
            "linkedin": resume_linkedin or user_data.get('linkedin', '')
        }
        generate_emails = data.get('generateEmails', True)
        create_drafts = data.get('createDrafts', True)

        # Search via the shared pipeline with our derived titles.
        result = find_recruiters(
            company_name=company,
            job_type=job_type,
            job_title=job_title,
            job_description=job_description,
            location=location,
            max_results=max_results_to_fetch,
            generate_emails=generate_emails,
            user_resume=user_resume,
            user_contact=user_contact,
            resume_text=resume_text,
            titles_override=titles,
            role_type="employee",
        )

        if result.get("error"):
            return jsonify({
                "error": result["error"],
                "recruiters": [],
                "requestedCount": max_results_requested,
                "foundCount": 0,
                "creditsCharged": 0
            }), 500

        all_people = result.get("recruiters", [])
        all_emails = result.get("emails", [])
        total_found = result.get("total_found", 0)
        _feed_warehouse(all_people, "FindEmployee")
        affordable_people = all_people[:max_affordable]

        affordable_emails = []
        if all_emails:
            affordable_email_set = set()
            for r in affordable_people:
                if r.get("Email") and r.get("Email") != "Not available":
                    affordable_email_set.add(r.get("Email"))
                if r.get("WorkEmail") and r.get("WorkEmail") != "Not available":
                    affordable_email_set.add(r.get("WorkEmail"))
            affordable_emails = [e for e in all_emails if e.get("to_email") in affordable_email_set]

        has_more = len(all_people) > max_affordable or total_found > max_affordable
        credits_needed_for_more = (len(all_people) - max_affordable) * RECRUITER_CREDIT_COST if has_more else 0

        # Deduct credits atomically BEFORE creating Gmail drafts (prevents TOCTOU).
        credits_charged = RECRUITER_CREDIT_COST * len(affordable_people)
        if credits_charged > 0:
            success, new_balance = deduct_credits_atomic(user_id, credits_charged, "find_employee")
            if not success:
                return jsonify({
                    "error": "Insufficient credits",
                    "creditsRequired": credits_charged,
                    "creditsAvailable": 0
                }), 402
        else:
            new_balance = current_credits

        drafts_created = []
        if create_drafts and affordable_emails:
            drafts_created = _create_gmail_drafts_for_emails(user_id, user_data, affordable_emails)

        response = {
            "recruiters": affordable_people,
            "emails": affordable_emails,
            "draftsCreated": drafts_created,
            "jobTypeDetected": result.get("job_type_detected", job_type or "general"),
            "companyCleaned": result.get("company_cleaned", company),
            "searchTitles": titles[:5],
            "targetTitles": titles[:5],
            "totalFound": total_found,
            "requestedCount": max_results_requested,
            "foundCount": len(affordable_people),
            "creditsCharged": credits_charged,
            "creditsRemaining": new_balance,
            "message": result.get("message"),
        }
        if has_more and len(affordable_people) > 0:
            response["hasMore"] = True
            response["moreAvailable"] = len(all_people) - max_affordable
            response["creditsNeededForMore"] = credits_needed_for_more

        return jsonify(response)

    except Exception as e:
        logger.error(f"[FindEmployee] Error: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@job_board_bp.route("/referral-draft", methods=["POST"])
@require_firebase_auth
def referral_draft_endpoint():
    """Phase 5 quality lift: generate a referral outreach draft for a saved
    contact at a job's company.

    Distinct from /find-recruiter (which cold-searches for new contacts at the
    company). This endpoint runs when the user already has a contact saved —
    it pulls richer per-contact context (coffee-chat prep, recent activity,
    JD/resume overlap) and produces a two-step "ask for chat" email, then
    creates a Gmail draft and returns the URL for the SPA to open.

    Request:
      {
        "contact_id": "<doc id under users/{uid}/contacts>",
        "job": {
          "job_id": "...",      // used for cache key
          "title": "...",
          "company": "...",
          "location": "...",
          "description": "...", // optional; falls back to structured
          "structured": {...},  // optional; Firecrawl-extracted bullets
          "apply_url": "..."    // optional
        }
      }

    Response (200 on success):
      {
        "ok": true,
        "gmailUrl": "https://mail.google.com/...",
        "draftId": "...",
        "subject": "...",
        "body": "...",
        "cached": false,
        "context_used": {
          "has_coffee_chat_prep": bool,
          "has_recent_activity": bool,
          "overlap_count": int,
          "two_step_framing": true
        }
      }
    Falls back to ok:true with gmailUrl:null when Gmail isn't connected —
    the SPA can still show subject/body for copy-paste in that case.
    """
    try:
        user_id = request.firebase_user.get("uid")
        user_email = request.firebase_user.get("email") or ""
        data = request.get_json(force=True, silent=True) or {}

        contact_id = (data.get("contact_id") or "").strip()
        job = data.get("job") or {}
        if not contact_id:
            return jsonify({"error": "contact_id required"}), 400
        if not isinstance(job, dict) or not job.get("company"):
            return jsonify({"error": "job with company required"}), 400

        # Rate limit: gpt-4o per click is ~$0.05; 30/day is well above any
        # legitimate single-session use and stops accidental spam from
        # double-clicks or stuck UI states.
        if not _check_user_rate_limit(user_id, "referral-draft-daily", "30 per day"):
            return jsonify({
                "error": "Daily limit reached",
                "message": "You've drafted 30 referral emails today. Try again tomorrow.",
            }), 429

        from app.services.referral_email import build_referral_draft
        result = build_referral_draft(
            uid=user_id,
            user_email=user_email,
            contact_id=contact_id,
            job=job,
            # Phase 5 step 1: generate text only. The SPA opens a preview/
            # edit modal; the user then commits via /referral-draft/commit
            # which creates the actual Gmail draft from their edited text.
            commit=False,
        )
        if not result.get("ok"):
            err = result.get("error", "unknown")
            logger.warning("[ReferralDraft] generation failed uid=%s err=%s", user_id, err)
            status = 404 if err == "contact_not_found" else 500
            return jsonify({"error": err}), status
        return jsonify(result), 200
    except Exception as e:
        logger.exception("[ReferralDraft] endpoint failed: %s", e)
        return jsonify({"error": "internal_error"}), 500


@job_board_bp.route("/referral-draft/commit", methods=["POST"])
@require_firebase_auth
def referral_draft_commit_endpoint():
    """Phase 5 step 2: create the Gmail draft from user-edited text.

    Called after the student reviews the LLM-generated draft in the SPA
    preview modal. Trusts whatever subject/body the user submits — no
    regeneration. Returns the Gmail URL.

    Request:
      {
        "contact_id": "...",
        "subject": "...",
        "body": "..."
      }
    """
    try:
        user_id = request.firebase_user.get("uid")
        user_email = request.firebase_user.get("email") or ""
        data = request.get_json(force=True, silent=True) or {}

        contact_id = (data.get("contact_id") or "").strip()
        subject = (data.get("subject") or "").strip()
        body = (data.get("body") or "").strip()
        if not contact_id:
            return jsonify({"error": "contact_id required"}), 400
        if not subject or not body:
            return jsonify({"error": "subject and body required"}), 400

        # Cheaper than generation — no LLM call. Still rate-limited so a
        # stuck UI can't spam Gmail drafts.
        if not _check_user_rate_limit(user_id, "referral-commit-daily", "60 per day"):
            return jsonify({
                "error": "Daily limit reached",
                "message": "You've created 60 referral drafts today. Try again tomorrow.",
            }), 429

        from app.services.referral_email import commit_referral_draft
        result = commit_referral_draft(
            uid=user_id,
            user_email=user_email,
            contact_id=contact_id,
            subject=subject,
            body=body,
        )
        if not result.get("ok"):
            err = result.get("error", "unknown")
            logger.warning("[ReferralDraftCommit] failed uid=%s err=%s", user_id, err)
            status = 404 if err == "contact_not_found" else (
                400 if err in ("empty_text", "text_too_long") else 500
            )
            return jsonify(result), status
        return jsonify(result), 200
    except Exception as e:
        logger.exception("[ReferralDraftCommit] endpoint failed: %s", e)
        return jsonify({"error": "internal_error"}), 500


@job_board_bp.route("/parse-hiring-prompt", methods=["POST"])
@require_firebase_auth
def parse_hiring_prompt():
    """
    Parse a natural language prompt into structured hiring manager search fields.

    Request: { "prompt": "recruiter at Bain for consulting internships" }
    Response: { "company": "Bain & Company", "jobTitle": "Consulting Intern", "location": "", "jobDescription": "" }
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
        prompt = (data.get("prompt") or "").strip()
        if not prompt:
            return jsonify({"error": "Prompt is required"}), 400

        from app.services.openai_client import get_openai_client
        client = get_openai_client()

        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": """Extract structured fields from a natural language query about finding hiring managers or recruiters.
Return ONLY valid JSON with these fields:
{
    "company": "company name (cleaned, full name)",
    "jobTitle": "the role/position being recruited for",
    "location": "city/region if mentioned, else empty string",
    "jobDescription": "brief summary of the role if mentioned, else empty string"
}
Examples:
- "recruiter at Bain for consulting internships" → {"company": "Bain & Company", "jobTitle": "Consulting Intern", "location": "", "jobDescription": "consulting internship"}
- "hiring manager at Google for software engineers in NYC" → {"company": "Google", "jobTitle": "Software Engineer", "location": "New York, NY", "jobDescription": ""}
- "who hires analysts at Goldman Sachs" → {"company": "Goldman Sachs", "jobTitle": "Analyst", "location": "", "jobDescription": ""}"""},
                {"role": "user", "content": prompt}
            ],
            temperature=0.1,
            max_tokens=200
        )

        import json as _json
        result_text = response.choices[0].message.content.strip()
        if result_text.startswith("```"):
            result_text = result_text.split("```")[1]
            if result_text.startswith("json"):
                result_text = result_text[4:]
        result_text = result_text.strip()

        parsed = _json.loads(result_text)
        return jsonify({
            "company": parsed.get("company", ""),
            "jobTitle": parsed.get("jobTitle", ""),
            "location": parsed.get("location", ""),
            "jobDescription": parsed.get("jobDescription", ""),
        })
    except Exception as e:
        logger.error(f"[ParseHiringPrompt] Error: {e}")
        return jsonify({"error": "Failed to parse prompt. Please try entering fields manually."}), 500


@job_board_bp.route("/find-hiring-manager", methods=["POST"])
@require_firebase_auth
def find_hiring_manager_endpoint():
    """
    Find hiring managers at a company for a specific job.
    
    Request:
        {
            "company": "Google",
            "jobTitle": "Software Engineer",
            "jobDescription": "...",  # Optional
            "jobType": "engineering",  # Optional, will be auto-detected
            "location": "San Francisco, CA",  # Optional
            "jobUrl": "https://...",  # Optional
            "maxResults": 3,  # Default: 3
            "generateEmails": true,  # Default: true
            "createDrafts": true  # Default: true
        }
    
    Response:
        {
            "hiringManagers": [...],
            "emails": [...],
            "draftsCreated": [...],
            "jobTypeDetected": "engineering",
            "companyCleaned": "Google LLC",
            "totalFound": 5,
            "creditsCharged": 45,
            "creditsRemaining": 155
        }
    """
    try:
        user_id = request.firebase_user.get('uid')
        data = request.get_json(force=True, silent=True) or {}

        # Phase 1 (Job Board Elevation Plan): daily cap on hiring-manager
        # discovery. Each call fans out to a multi-tier PDL search + Hunter
        # verification — by far the most expensive endpoint per request.
        # 100/day caps abuse while leaving room for legitimate Pro/Elite use.
        if not _check_user_rate_limit(user_id, "find-hiring-manager-daily", "100 per day"):
            return jsonify({
                "error": "Daily limit reached",
                "message": "You've used Find Hiring Manager 100 times today. Try again tomorrow."
            }), 429

        # Get job information from various sources
        company = data.get('company')
        job_title = data.get('jobTitle', '')
        job_description = data.get('jobDescription', '')
        job_type = data.get('jobType')  # Optional
        location = data.get('location')  # Optional
        job_url = data.get('jobUrl')  # Optional - for external job links

        # Log initial values for debugging
        logger.info(f"[FindHiringManager] Initial values - company: '{company}', job_title: '{job_title}', location: '{location}', has_description: {bool(job_description)}, has_url: {bool(job_url)}")
        
        # Reject obviously invalid company names from request
        invalid_company_names = {'job type', 'job details', 'job description', 'employer', 'company', 'organization', 'n/a', 'null', 'none', ''}
        if company and company.lower().strip() in invalid_company_names:
            logger.warning(f"[FindHiringManager] Rejecting invalid company name from request: '{company}'")
            company = None
        elif company:
            # Normalize company name from user input
            company = normalize_company_name(company)
        
        # Parse job URL to fill in missing fields (company, title, location, description)
        if job_url:
            try:
                parsed_job = parse_job_url(job_url)
                if parsed_job:
                    if parsed_job.get('company') and not company:
                        company = normalize_company_name(parsed_job.get('company'))
                    if parsed_job.get('title') and not job_title:
                        job_title = parsed_job.get('title')
                    if parsed_job.get('location') and not location:
                        location = parsed_job.get('location')
                    if parsed_job.get('description') and not job_description:
                        job_description = parsed_job.get('description')
                    logger.info(f"[FindHiringManager] URL parsed: company={bool(parsed_job.get('company'))}, title={bool(parsed_job.get('title'))}, desc={bool(parsed_job.get('description'))}")
                else:
                    logger.warning(f"[FindHiringManager] URL parsing returned no data for: {job_url}")
            except Exception as e:
                logger.error(f"[FindHiringManager] Error parsing job URL: {e}")
        # NOTE: Firecrawl extract_job_posting() can scrape `hiring_manager` from
        # ATS postings but takes ~42s end-to-end (Firecrawl stealth + LLM
        # extraction). Not wired into this route until we make the call async
        # / background. Helpers live in firecrawl_client + recruiter_finder
        # (_seed_from_firecrawl_name) as future work.

        # Use OpenAI to extract missing information from job description (primary extraction method)
        if job_description:
            logger.info(f"[FindHiringManager] Using OpenAI to extract job details from description (length: {len(job_description)})...")
            try:
                extracted = extract_job_details_with_openai(job_description)
                if extracted:
                    logger.info(f"[FindHiringManager] OpenAI extraction result: {extracted}")
                    if extracted.get('company'):
                        company = extracted['company']  # Already normalized
                        logger.info(f"[FindHiringManager]  OpenAI extracted company: '{company}'")
                    if extracted.get('job_title'):
                        job_title = extracted['job_title']
                        logger.info(f"[FindHiringManager]  OpenAI extracted job_title: '{job_title}'")
                    if extracted.get('location'):
                        location = extracted['location']
                        logger.info(f"[FindHiringManager]  OpenAI extracted location: '{location}'")
            except Exception as e:
                logger.error(f"[FindHiringManager]  OpenAI extraction failed: {e}")
        
        # Final validation - reject invalid company names
        invalid_company_names = {
            'job type', 'job details', 'job description', 'job title', 'job location',
            'employer', 'company', 'organization', 'corporation', 
            'n/a', 'null', 'none', '', 'full-time', 'part-time', 'contract',
            'remote', 'hybrid', 'on-site', 'location', 'details', 'description'
        }
        if company:
            company_lower = company.lower().strip()
            if company_lower in invalid_company_names or len(company_lower) < 2:
                logger.warning(f"[FindHiringManager]  Final validation: Rejecting invalid company name: '{company}'")
                company = None
        
        logger.info(f"[FindHiringManager] Final values before validation - company: '{company}', job_title: '{job_title}', location: '{location}'")
        
        # Validate required fields
        if not company:
            return jsonify({
                "error": "Company name is required",
                "suggestion": "Please provide a company name, paste a job URL, or include the company name in the job description."
            }), 400
        
        # Check user credits (minimum RECRUITER_CREDIT_COST for 1 contact)
        db = get_db()
        if not db:
            return jsonify({"error": "Database not available"}), 500
            
        user_ref = db.collection('users').document(user_id)
        user_doc = user_ref.get()
        
        if not user_doc.exists:
            return jsonify({"error": "User not found"}), 404
        
        user_data = user_doc.to_dict()
        user_data = sanitize_firestore_data(user_data, depth=0, max_depth=20)
        current_credits = check_and_reset_credits(user_ref, user_data)
        
        if current_credits < RECRUITER_CREDIT_COST:
            return jsonify({
                "error": "Insufficient credits",
                "creditsRequired": RECRUITER_CREDIT_COST,
                "creditsAvailable": current_credits
            }), 402
        
        # Get user's requested max_results (default: 3, max: 10)
        try:
            max_results_requested = int(data.get('maxResults', 3))
        except (TypeError, ValueError):
            max_results_requested = 3
        max_results_requested = min(max(max_results_requested, 1), 10)
        
        # Calculate how many hiring managers user can afford (RECRUITER_CREDIT_COST per manager)
        max_affordable = current_credits // RECRUITER_CREDIT_COST
        # Use the minimum of what user requested and what they can afford
        max_results_to_fetch = min(max_results_requested, max_affordable)
        
        # Get user's resume and contact info for email generation
        user_resume = user_data.get('resumeParsed', {})
        resume_text = user_data.get('resumeText', '')
        resume_phone = user_resume.get('contact', {}).get('phone', '') if isinstance(user_resume.get('contact'), dict) else ''
        resume_linkedin = user_resume.get('contact', {}).get('linkedin', '') if isinstance(user_resume.get('contact'), dict) else ''
        user_contact = {
            "name": user_resume.get('name', user_data.get('displayName', '')),
            "email": get_outreach_email(user_data),
            "phone": resume_phone or user_data.get('phone', ''),
            "linkedin": resume_linkedin or user_data.get('linkedin', '')
        }

        # Check if user wants to generate emails (default to True)
        generate_emails = data.get('generateEmails', True)
        create_drafts = data.get('createDrafts', True)

        # Preview mode — frontend already sends mode='preview' from the initial
        # search; the backend was ignoring it. When preview, force off the
        # expensive Perplexity verify + email gen + draft creation and return
        # PDL-ranked contacts fast. Drafts are created by /emails/generate-and-draft
        # when the user clicks Draft, same contract as the People flow.
        mode = (data.get('mode') or 'draft').lower()
        if mode == 'preview':
            generate_emails = False
            create_drafts = False

        # Find hiring managers
        result = find_hiring_manager(
            company_name=company,
            job_type=job_type,
            job_title=job_title,
            job_description=job_description,
            location=location,
            max_results=max_results_to_fetch,
            generate_emails=generate_emails,
            user_resume=user_resume,
            user_contact=user_contact,
            resume_text=resume_text,
            role_type="hiring_manager",
            uid=user_id,
            mode=mode,
        )
        
        # Check if we got results
        if result.get("error"):
            return jsonify({
                "error": result["error"],
                "hiringManagers": [],
                "creditsCharged": 0
            }), 500
        
        # Get results
        all_hiring_managers = result.get("hiringManagers", [])
        all_emails = result.get("emails", [])
        total_found = result.get("total_found", 0)
        _feed_warehouse(all_hiring_managers, "FindHiringManager")
        
        # Results are already limited to max_results_to_fetch, but we still need to respect credits
        affordable_managers = all_hiring_managers[:max_affordable]
        
        # Limit emails to match affordable managers
        affordable_emails = []
        if all_emails:
            affordable_manager_emails = set()
            for m in affordable_managers:
                if m.get("Email") and m.get("Email") != "Not available":
                    affordable_manager_emails.add(m.get("Email"))
                if m.get("WorkEmail") and m.get("WorkEmail") != "Not available":
                    affordable_manager_emails.add(m.get("WorkEmail"))
            
            affordable_emails = [e for e in all_emails if e.get("to_email") in affordable_manager_emails]
        
        # Deduct credits atomically BEFORE creating Gmail drafts (prevents TOCTOU)
        credits_charged = RECRUITER_CREDIT_COST * len(affordable_managers)
        if credits_charged > 0:
            success, new_balance = deduct_credits_atomic(user_id, credits_charged, "find_hiring_manager")
            if not success:
                return jsonify({
                    "error": "Insufficient credits",
                    "creditsRequired": credits_charged,
                    "creditsAvailable": 0
                }), 402
        else:
            new_balance = current_credits

        # Create Gmail drafts if requested (after credits are deducted)
        drafts_created = []
        if create_drafts and affordable_emails:
            from app.services.gmail_client import _load_user_gmail_creds, get_gmail_service_for_user
            from app.services.gmail_client import download_resume_from_url
            import base64
            from email.mime.multipart import MIMEMultipart
            from email.mime.text import MIMEText
            from email.mime.base import MIMEBase
            from email import encoders

            # Check Gmail connection
            gmail_creds = _load_user_gmail_creds(user_id)
            resume_url = user_data.get('resumeURL') or user_data.get('resumeUrl')

            if gmail_creds:
                # Download resume once for all drafts
                resume_content = None
                resume_filename = None
                if resume_url:
                    try:
                        resume_content, resume_filename = download_resume_from_url(resume_url)
                        stored_filename = user_data.get('resumeFileName')
                        if stored_filename:
                            resume_filename = stored_filename
                        if resume_content:
                            logger.info(f"[FindHiringManager] Downloaded resume ({len(resume_content)} bytes, filename={resume_filename}) for {len(affordable_emails)} drafts")
                    except Exception as e:
                        logger.error(f"[FindHiringManager] Failed to download resume: {e}")

                # Get Gmail service
                gmail_service = get_gmail_service_for_user(user_data.get('email'), user_id=user_id)

                if gmail_service:
                    import re as _re2
                    _EMAIL_RE2 = _re2.compile(r'^[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}$')

                    for email_data in affordable_emails:
                        try:
                            manager = email_data.get("recruiter", {})
                            to_email = email_data.get("to_email")
                            if not to_email:
                                continue
                            # Validate email format before creating draft
                            if not _EMAIL_RE2.match(to_email):
                                logger.info(f"[FindHiringManager] Skipping invalid email: {to_email[:50]}")
                                continue
                            to_name = email_data.get("to_name", "")
                            subject = email_data.get("subject", "")
                            body_html = email_data.get("body", "")
                            body_plain = email_data.get("plain_body", "")

                            # Sanitize to_name to prevent header injection
                            to_name = to_name.replace('"', '').replace('\n', '').replace('\r', '')

                            # Create MIME message (mixed for attachment support)
                            message = MIMEMultipart('mixed')
                            message['to'] = f'"{to_name}" <{to_email}>' if to_name else to_email
                            message['subject'] = subject

                            # Add text/html alternative part
                            alt = MIMEMultipart('alternative')
                            alt.attach(MIMEText(body_plain, 'plain'))
                            from app.services import email_tracking
                            tracking_token = email_tracking.start(user_id)
                            alt.attach(MIMEText(email_tracking.add_pixel(body_html, tracking_token), 'html'))
                            message.attach(alt)

                            # Add resume attachment if available
                            if resume_content and resume_filename:
                                part = MIMEBase('application', 'pdf')
                                part.set_payload(resume_content)
                                encoders.encode_base64(part)
                                part.add_header('Content-Disposition', f'attachment; filename="{resume_filename}"')
                                message.attach(part)

                            # Encode message
                            raw_message = base64.urlsafe_b64encode(message.as_bytes()).decode('utf-8')

                            # Create draft
                            draft_body = {
                                'message': {
                                    'raw': raw_message
                                }
                            }

                            draft = gmail_service.users().drafts().create(userId='me', body=draft_body).execute()
                            email_tracking.register(tracking_token, user_id, 'hiring_manager', recipient_email=to_email, draft_id=draft.get('id'), message_id=draft.get('message', {}).get('id'), thread_id=draft.get('message', {}).get('threadId'))
                            draft_id = draft['id']

                            # Extract message ID from draft (more reliable for opening drafts)
                            message_id = draft.get('message', {}).get('id')
                            if not message_id:
                                try:
                                    draft_message = gmail_service.users().drafts().get(userId='me', id=draft_id, format='full').execute()
                                    message_id = draft_message.get('message', {}).get('id')
                                except Exception as e:
                                    logger.info(f"[FindHiringManager] Could not get message ID from draft: {e}")

                            # Get Gmail account email for URL
                            try:
                                profile = gmail_service.users().getProfile(userId='me').execute()
                                connected_email = profile.get('emailAddress', '')
                            except:
                                connected_email = user_data.get('email', '')

                            if message_id:
                                draft_url = (
                                    f"https://mail.google.com/mail/?authuser={connected_email}#drafts?compose={message_id}"
                                    if connected_email else f"https://mail.google.com/mail/u/0/#drafts?compose={message_id}"
                                )
                            else:
                                draft_url = f"https://mail.google.com/mail/?authuser={connected_email}#draft/{draft_id}" if connected_email else f"https://mail.google.com/mail/u/0/#draft/{draft_id}"

                            drafts_created.append({
                                "recruiter_email": to_email,
                                "draft_id": draft_id,
                                "message_id": message_id,
                                "draft_url": draft_url
                            })

                            logger.info(f"[FindHiringManager] Created Gmail draft for {to_email}")

                            # Log this manually drafted hiring manager into the
                            # outbox tracker at DRAFT time, matching Find People,
                            # so they appear in /outbox. Routed through the shared
                            # builder via upsert_hm_outbox_contact so this path and
                            # the agent / Loop HM path write the identical shape
                            # (inOutbox + isHiringManager, dedup on lowercased
                            # email, conservative merge on re-draft). The
                            # recruiters/* save in save-recruiters is unchanged;
                            # this write is purely additive. Own try/except so a
                            # Firestore hiccup never gets logged as a
                            # draft-creation failure.
                            try:
                                from app.services.outbox_service import upsert_hm_outbox_contact
                                hm_first = (manager.get("FirstName") or manager.get("firstName") or manager.get("first_name") or "").strip()
                                hm_last = (manager.get("LastName") or manager.get("lastName") or manager.get("last_name") or "").strip()
                                if not hm_first and not hm_last and to_name:
                                    name_parts = to_name.split()
                                    hm_first = name_parts[0]
                                    hm_last = " ".join(name_parts[1:])
                                now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                                today = datetime.now().strftime("%m/%d/%Y")
                                upsert_hm_outbox_contact(
                                    db,
                                    user_id,
                                    first_name=hm_first,
                                    last_name=hm_last,
                                    email=to_email,
                                    company=(manager.get("Company") or manager.get("company") or company or "").strip(),
                                    job_title=(manager.get("Title") or manager.get("title") or manager.get("jobTitle") or manager.get("job_title") or "").strip(),
                                    linkedin_url=(manager.get("LinkedIn") or manager.get("linkedin") or manager.get("linkedinUrl") or manager.get("linkedin_url") or "").strip(),
                                    email_subject=subject,
                                    # Store the plain-text body in Firestore. The HTML
                                    # version (body_html) is only for the Gmail MIME part
                                    # above; the tracker renders emailBody as text.
                                    email_body=body_plain,
                                    gmail_draft_id=draft_id,
                                    gmail_draft_url=draft_url,
                                    now_iso=now_iso,
                                    today=today,
                                )
                                logger.info(f"[FindHiringManager] Logged outbox contact for {to_email}")
                            except Exception as outbox_err:
                                logger.error(f"[FindHiringManager] Outbox contact upsert failed for {to_email}: {outbox_err}")

                        except Exception as e:
                            logger.error(f"[FindHiringManager] Failed to create draft for {email_data.get('to_email')}: {e}")
                else:
                    logger.info(f"[FindHiringManager] Gmail service not available")
            else:
                logger.info(f"[FindHiringManager] Gmail not connected, skipping draft creation")

        response = {
            "hiringManagers": affordable_managers,
            "emails": affordable_emails,
            "draftsCreated": drafts_created,
            "jobTypeDetected": result["job_type_detected"],
            "companyCleaned": result["company_cleaned"],
            "searchTitles": result.get("search_titles", []),
            "totalFound": total_found,
            "creditsCharged": credits_charged,
            "creditsRemaining": new_balance
        }
        
        return jsonify(response)
        
    except Exception as e:
        logger.error(f"[FindHiringManager] Error: {str(e)}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@job_board_bp.route("/save-recruiters", methods=["POST"])
@require_firebase_auth
def save_recruiters_to_tracker():
    """
    Save recruiters (from find-recruiter or find-hiring-manager) to the user's
    Hiring Manager Tracker (Firestore users/{uid}/recruiters).
    Used by the Chrome extension so results are saved like on the website.
    """
    try:
        user_id = request.firebase_user.get("uid")
        data = request.get_json(force=True, silent=True) or {}
        recruiters = data.get("recruiters") or []
        drafts_created = data.get("draftsCreated") or []
        company_cleaned = data.get("companyCleaned") or data.get("company") or ""
        associated_job_title = data.get("associatedJobTitle") or data.get("jobTitle") or ""
        associated_job_url = data.get("associatedJobUrl") or data.get("jobUrl") or ""

        if not recruiters:
            return jsonify({"saved": 0, "skipped": 0, "message": "No recruiters to save"}), 200

        db = get_db()
        if not db:
            return jsonify({"error": "Database not available"}), 500

        # Build draft map: email -> { draft_id, draft_url, message_id }
        draft_map = {}
        for d in drafts_created:
            email = (d.get("recruiter_email") or d.get("recruiterEmail") or "").strip().lower()
            if email:
                draft_map[email] = {
                    "draft_id": d.get("draft_id"),
                    "draft_url": d.get("draft_url"),
                    "message_id": d.get("message_id"),
                }

        # Convert API recruiter shape to Firestore document shape (match frontend firebaseApi)
        def to_firestore_recruiter(r):
            email = (r.get("Email") or r.get("email") or r.get("WorkEmail") or r.get("work_email") or "").strip()
            city = (r.get("City") or r.get("city") or "").strip()
            state = (r.get("State") or r.get("state") or "").strip()
            location = ", ".join(filter(None, [city, state])).strip()
            doc_data = {
                "firstName": (r.get("FirstName") or r.get("firstName") or r.get("first_name") or "").strip(),
                "lastName": (r.get("LastName") or r.get("lastName") or r.get("last_name") or "").strip(),
                "linkedinUrl": (r.get("LinkedIn") or r.get("linkedin") or r.get("linkedinUrl") or r.get("linkedin_url") or "").strip(),
                "email": email,
                "company": (r.get("Company") or r.get("company") or company_cleaned or "").strip(),
                "jobTitle": (r.get("Title") or r.get("title") or r.get("jobTitle") or r.get("job_title") or associated_job_title or "").strip(),
                "location": location,
                "dateAdded": datetime.utcnow().isoformat() + "Z",
                "status": "Not Contacted",
                "createdAt": datetime.utcnow().isoformat() + "Z",
                "updatedAt": datetime.utcnow().isoformat() + "Z",
            }
            if r.get("Phone") or r.get("phone"):
                doc_data["phone"] = r.get("Phone") or r.get("phone")
            if r.get("WorkEmail") or r.get("work_email"):
                doc_data["workEmail"] = r.get("WorkEmail") or r.get("work_email")
            if r.get("PersonalEmail") or r.get("personal_email"):
                doc_data["personalEmail"] = r.get("PersonalEmail") or r.get("personal_email")
            if associated_job_title:
                doc_data["associatedJobTitle"] = associated_job_title
            if associated_job_url:
                doc_data["associatedJobUrl"] = associated_job_url
            draft_info = draft_map.get(email.lower()) if email else None
            if draft_info:
                if draft_info.get("draft_id"):
                    doc_data["gmailDraftId"] = draft_info["draft_id"]
                if draft_info.get("draft_url"):
                    doc_data["gmailDraftUrl"] = draft_info["draft_url"]
                if draft_info.get("message_id"):
                    doc_data["gmailMessageId"] = draft_info["message_id"]
            return doc_data

        # Get existing recruiters for deduplication
        recruiters_ref = db.collection("users").document(user_id).collection("recruiters")
        existing = recruiters_ref.stream()
        existing_emails = set()
        existing_linkedins = set()
        for doc_snap in existing:
            d = doc_snap.to_dict()
            if d.get("email"):
                existing_emails.add((d.get("email") or "").strip().lower())
            if d.get("linkedinUrl"):
                existing_linkedins.add((d.get("linkedinUrl") or "").strip())

        to_save = []
        for r in recruiters:
            doc_data = to_firestore_recruiter(r)
            email = (doc_data.get("email") or "").strip().lower()
            linkedin = (doc_data.get("linkedinUrl") or "").strip()
            if email and email in existing_emails:
                continue
            if linkedin and linkedin in existing_linkedins:
                continue
            to_save.append(doc_data)
            if email:
                existing_emails.add(email)
            if linkedin:
                existing_linkedins.add(linkedin)

        if not to_save:
            return jsonify({"saved": 0, "skipped": len(recruiters), "message": "All recruiters already in tracker"}), 200

        # Batch write (Firestore limit 500; we have at most a few dozen)
        batch = db.batch()
        for doc_data in to_save:
            new_ref = recruiters_ref.document()
            batch.set(new_ref, doc_data)
        batch.commit()

        logger.info(f"[JobBoard] Saved {len(to_save)} recruiter(s) to Hiring Manager Tracker for user {user_id}")
        return jsonify({
            "saved": len(to_save),
            "skipped": len(recruiters) - len(to_save),
            "message": f"Saved {len(to_save)} to Hiring Manager Tracker",
        }), 200

    except Exception as e:
        logger.error(f"[JobBoard] save-recruiters error: {e}")
        import traceback
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


@job_board_bp.route("/generate-cover-letter", methods=["POST"])
@require_firebase_auth
def generate_cover_letter():
    """
    Generate a personalized cover letter for a job.
    Cost: 5 credits
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
        user_id = request.firebase_user.get('uid')
        
        job_url = data.get("jobUrl")
        job_description = data.get("jobDescription", "")
        job_title = data.get("jobTitle", "")
        company = data.get("company", "")
        
        # Check credits
        db = get_db()
        if not db:
            return jsonify({"error": "Database not available"}), 500
            
        user_ref = db.collection("users").document(user_id)
        user_doc = user_ref.get()
        
        if not user_doc.exists:
            return jsonify({"error": "User not found"}), 404
        
        user_data = user_doc.to_dict()
        current_credits = check_and_reset_credits(user_ref, user_data)
        
        if current_credits < COVER_LETTER_CREDIT_COST:
            return jsonify({
                "error": "Insufficient credits",
                "required": COVER_LETTER_CREDIT_COST,
                "current": current_credits,
            }), 402
        
        # Get job details from URL if provided
        # Always parse URL when provided to ensure we get the latest job info
        if job_url:
            logger.info(f"[JobBoard] Parsing job URL for cover letter: {job_url}")
            parsed_job = parse_job_url(job_url)
            if parsed_job:
                logger.info(f"[JobBoard] Successfully parsed job from URL")
                logger.info(f"[JobBoard] Parsed title: {parsed_job.get('title')}")
                logger.info(f"[JobBoard] Parsed company: {parsed_job.get('company')}")
                logger.info(f"[JobBoard] Parsed description length: {len(parsed_job.get('description', '') or '')}")
                # URL data takes precedence over existing fields
                if parsed_job.get("title"):
                    job_title = parsed_job.get("title")
                if parsed_job.get("company"):
                    company = parsed_job.get("company")
                if parsed_job.get("description"):
                    job_description = parsed_job.get("description")
            else:
                logger.error(f"[JobBoard] Failed to parse job from URL, using provided fields")
        
        if not job_description:
            return jsonify({"error": "Job description is required. Please paste the job description or provide a valid URL."}), 400
        
        # Get user's resume - use cached sanitized version if available
        raw_resume = user_data.get("resumeParsed", {})
        if not raw_resume:
            # Fallback (2026-09-08): users who onboarded through the app have
            # resumeText (or a resumeUrl) but never the structured
            # resumeParsed, and this route sent them to "upload your resume"
            # although the email drafts already used that same resume. Hand
            # the generator the raw text; it works from that when the
            # structured fields are missing.
            _text = (user_data.get("resumeText") or "").strip()
            if not _text and user_data.get("resumeUrl"):
                try:
                    from app.services.gmail_client import download_resume_from_url as _dl
                    from app.services.resume_parser import extract_text_from_pdf_bytes as _ex
                    _bytes, _fname = _dl(user_data.get("resumeUrl"))
                    if _bytes:
                        _text = (_ex(_bytes) or "").strip()
                except Exception as _e:
                    logger.warning(f"[JobBoard] resume fallback download failed: {_e}")
            if _text:
                raw_resume = {
                    "name": user_data.get("displayName") or user_data.get("name") or "",
                    "raw_text": _text[:8000],
                }
                logger.info(f"[JobBoard] Cover letter using raw resume text ({len(_text)} chars), no resumeParsed on file")
        if not raw_resume:
            return jsonify({
                "error": "No resume found. Please upload your resume in Account Settings first."
            }), 400
        
        # Sanitize resume once (removed redundant multiple passes for performance)
        # The generate_cover_letter_with_ai function will further optimize by extracting only relevant sections
        user_resume = sanitize_firestore_data(raw_resume, depth=0, max_depth=20)
        
        if not user_resume:
            return jsonify({
                "error": "No resume found. Please upload your resume in Account Settings first."
            }), 400
        
        # Deduct credits BEFORE doing the expensive generation
        # This prevents negative balances and ensures we have credits before proceeding
        logger.info(f"[JobBoard] Deducting credits before cover letter generation...")
        logger.info(f"[JobBoard] Current credits: {current_credits}, Required: {COVER_LETTER_CREDIT_COST}")
        success, new_credits = deduct_credits_atomic(user_id, COVER_LETTER_CREDIT_COST, "cover_letter_generation")
        
        if not success:
            # Deduction failed (likely insufficient credits due to race condition)
            return jsonify({
                "error": "Failed to deduct credits. Insufficient credits or system error.",
                "current": new_credits,
                "required": COVER_LETTER_CREDIT_COST,
            }), 402
        
        logger.info(f"[JobBoard] Credits deducted successfully. New balance: {new_credits}")
        
        # Generate cover letter
        logger.info(f"[JobBoard] Starting cover letter generation...")
        # Total timeout: 2 retries * (45s + 15s buffer) + retry delays (2s + 4s) + buffer = ~120s
        total_timeout = 120.0  # Reduced from 200s
        try:
            # Wrap in timeout to prevent indefinite hanging
            # Use a longer timeout to account for retries (3 attempts with exponential backoff)
            async def generate_with_timeout():
                return await generate_cover_letter_with_ai(user_resume, job_description, job_title, company)
            
            logger.info(f"[JobBoard] Starting async cover letter generation with {total_timeout} second timeout...")
            cover_letter = asyncio.run(
                asyncio.wait_for(generate_with_timeout(), timeout=total_timeout)
            )
            logger.info(f"[JobBoard]  Cover letter generation completed successfully")
        except asyncio.TimeoutError:
            logger.info(f"[JobBoard]  Cover letter generation timed out after {total_timeout} seconds")
            # Refund credits on timeout
            logger.info(f"[JobBoard] Refunding {COVER_LETTER_CREDIT_COST} credits due to timeout...")
            refund_success, refunded_credits = refund_credits_atomic(user_id, COVER_LETTER_CREDIT_COST, "cover_letter_timeout_refund")
            if refund_success:
                logger.info(f"[JobBoard]  Credits refunded successfully. New balance: {refunded_credits}")
            else:
                logger.error(f"[JobBoard]  Failed to refund credits (user may need manual refund)")
            import traceback
            traceback.print_exc()
            return jsonify({
                "error": "Cover letter generation timed out. Your credits have been refunded. Please try again.",
                "creditsRefunded": refund_success,
            }), 504
        except Exception as gen_error:
            logger.error(f"[JobBoard]  Error during cover letter generation: {gen_error}")
            logger.error(f"[JobBoard] Error type: {type(gen_error)}")
            # Refund credits on error
            logger.error(f"[JobBoard] Refunding {COVER_LETTER_CREDIT_COST} credits due to error...")
            refund_success, refunded_credits = refund_credits_atomic(user_id, COVER_LETTER_CREDIT_COST, "cover_letter_error_refund")
            if refund_success:
                logger.info(f"[JobBoard]  Credits refunded successfully. New balance: {refunded_credits}")
            else:
                logger.error(f"[JobBoard]  Failed to refund credits (user may need manual refund)")
            import traceback
            traceback.print_exc()
            return jsonify({
                "error": f"Cover letter generation failed: {str(gen_error)}. Your credits have been refunded.",
                "creditsRefunded": refund_success,
            }), 500
        
        return jsonify({
            "coverLetter": cover_letter,
            "creditsUsed": COVER_LETTER_CREDIT_COST,
            "creditsRemaining": new_credits,
            # Backfill for the client so the download filename and any
            # follow-up UI reflect what we actually resolved from the URL.
            "company": company,
            "jobTitle": job_title,
        }), 200

    except Exception as e:
        logger.error(f"[JobBoard] Cover letter generation error: {e}")
        return jsonify({"error": str(e)}), 500


@job_board_bp.route("/cover-letter-pdf", methods=["POST"])
@require_firebase_auth
def generate_cover_letter_pdf_endpoint():
    """
    Generate a PDF from cover letter text content.
    No credit cost - just PDF generation.
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
        content = data.get("content", "")
        company = data.get("company", "")

        if not content:
            return jsonify({"error": "Cover letter content is required"}), 400

        # Pull the applicant's contact info from the stored resume + user
        # profile so the PDF header can carry name / phone / email / LinkedIn.
        # Best-effort: any missing fields are just skipped in the header.
        applicant: Dict[str, str] = {}
        try:
            user_id = request.firebase_user.get("uid")
            db = get_db()
            if db and user_id:
                user_doc = db.collection("users").document(user_id).get()
                if user_doc.exists:
                    user_data = user_doc.to_dict() or {}
                    raw_resume = user_data.get("resumeParsed") or {}
                    user_resume = sanitize_firestore_data(raw_resume, depth=0, max_depth=20) or {}
                    contact = user_resume.get("contact") if isinstance(user_resume.get("contact"), dict) else {}
                    applicant = {
                        "name": (
                            user_resume.get("name")
                            or user_data.get("displayName")
                            or user_data.get("name")
                            or ""
                        ),
                        "email": (
                            contact.get("email")
                            or user_data.get("email")
                            or request.firebase_user.get("email")
                            or ""
                        ),
                        "phone": contact.get("phone") or user_data.get("phone") or "",
                        "linkedin": contact.get("linkedin") or user_data.get("linkedin") or "",
                    }
        except Exception:
            logger.warning("[JobBoard] Failed to load applicant info for PDF header", exc_info=True)

        # Generate PDF
        pdf_buffer = generate_cover_letter_pdf(
            content,
            company=company or None,
            applicant=applicant or None,
        )

        # Create filename: companyname_cover_letter.pdf
        import re
        if company:
            # Sanitize company name for filename: lowercase, replace spaces/special chars with underscores
            sanitized_company = re.sub(r'[^a-z0-9]+', '_', company.lower().strip())
            sanitized_company = sanitized_company.strip('_')
            filename = f"{sanitized_company}_cover_letter.pdf"
        else:
            filename = "cover_letter.pdf"
        
        # Return PDF as response
        from flask import Response
        return Response(
            pdf_buffer.getvalue(),
            mimetype='application/pdf',
            headers={
                'Content-Disposition': f'attachment; filename={filename}'
            }
        )
        
    except Exception as e:
        logger.error(f"[JobBoard] Cover letter PDF generation error: {e}")
        return jsonify({"error": str(e)}), 500


@job_board_bp.route("/parse-job-url", methods=["POST"])
@require_firebase_auth
def parse_job_url_endpoint():
    """
    Parse a job URL to extract job details.
    No credit cost - just URL parsing.
    """
    try:
        data = request.get_json(force=True, silent=True) or {}
        url = data.get("url", "")
        
        if not url:
            return jsonify({"error": "URL is required"}), 400
        
        try:
            parsed = urlparse(url)
            if not parsed.scheme or not parsed.netloc:
                return jsonify({"error": "Invalid URL format"}), 400
        except Exception:
            return jsonify({"error": "Invalid URL"}), 400
        
        job_data = parse_job_url(url)
        
        if not job_data:
            return jsonify({
                "error": "Could not parse job details from URL. Please paste the job description instead.",
                "job": None
            }), 422
        
        return jsonify({"job": job_data}), 200
        
    except Exception as e:
        logger.error(f"[JobBoard] URL parsing error: {e}")
        return jsonify({"error": str(e)}), 500


@job_board_bp.route("/clear-cache", methods=["POST"])
@require_firebase_auth
def clear_cache():
    """
    Admin endpoint to clear expired cache entries.
    """
    try:
        deleted = clear_expired_cache()
        return jsonify({
            "message": f"Cleared {deleted} expired cache entries",
            "deleted": deleted,
        }), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# =============================================================================
# MOCK DATA FALLBACK
# =============================================================================

def get_mock_jobs(
    job_types: List[str],
    industries: List[str],
    locations: List[str],
) -> List[Dict[str, Any]]:
    """
    Fallback mock job listings when SerpAPI is unavailable.
    """
    all_jobs = [
        {
            "id": "mock_1",
            "title": "Software Engineering Intern",
            "company": "Google",
            "location": "Mountain View, CA",
            "salary": "$8,000/mo",
            "type": "Internship",
            "posted": "2 days ago",
            "description": "Join Google's engineering team for a summer internship focused on building scalable systems and innovative products that impact billions of users worldwide. You'll work with experienced engineers on real projects.",
            "requirements": ["Currently pursuing BS/MS in Computer Science", "Experience with Python, Java, or C++", "Strong problem-solving skills"],
            "url": "https://careers.google.com/jobs/",
            "remote": False,
            "experienceLevel": "entry",
        },
        {
            "id": "mock_2",
            "title": "Product Management Intern",
            "company": "Meta",
            "location": "Menlo Park, CA",
            "salary": "$9,000/mo",
            "type": "Internship",
            "posted": "1 week ago",
            "description": "Work alongside experienced PMs to ship products used by billions. You'll define strategy, work cross-functionally, and drive impact on Facebook, Instagram, or WhatsApp.",
            "requirements": ["MBA or BS/MS in relevant field", "Strong analytical skills", "Excellent communication"],
            "url": "https://www.metacareers.com/",
            "remote": True,
            "experienceLevel": "entry",
        },
        {
            "id": "mock_3",
            "title": "Investment Banking Analyst",
            "company": "Goldman Sachs",
            "location": "New York, NY",
            "salary": "$110,000/yr",
            "type": "Full-Time",
            "posted": "3 days ago",
            "description": "Join our Investment Banking Division to work on high-profile M&A, capital markets, and restructuring transactions for Fortune 500 clients.",
            "requirements": ["Bachelor's degree in Finance, Economics, or related", "Strong financial modeling skills", "Excellent attention to detail"],
            "url": "https://www.goldmansachs.com/careers/",
            "remote": False,
            "experienceLevel": "entry",
        },
        {
            "id": "mock_4",
            "title": "Data Science Intern",
            "company": "Netflix",
            "location": "Los Gatos, CA",
            "salary": "$8,500/mo",
            "type": "Internship",
            "posted": "4 days ago",
            "description": "Apply machine learning and statistical methods to personalization, content recommendations, and business analytics that shape what 200M+ members watch.",
            "requirements": ["Pursuing MS/PhD in Statistics, CS, or related", "Proficiency in Python and SQL", "Experience with ML frameworks"],
            "url": "https://jobs.netflix.com/",
            "remote": True,
            "experienceLevel": "entry",
        },
        {
            "id": "mock_5",
            "title": "Consulting Analyst",
            "company": "McKinsey & Company",
            "location": "Chicago, IL",
            "salary": "$95,000/yr",
            "type": "Full-Time",
            "posted": "2 weeks ago",
            "description": "Solve complex business problems for Fortune 500 clients across industries. Develop strategy, drive implementation, and create lasting impact.",
            "requirements": ["Outstanding academic record", "Strong analytical and problem-solving skills", "Leadership experience"],
            "url": "https://www.mckinsey.com/careers",
            "remote": False,
            "experienceLevel": "entry",
        },
        # Additional internship jobs
        {
            "id": "mock_6",
            "title": "Software Engineering Intern",
            "company": "Apple",
            "location": "Cupertino, CA",
            "salary": "$8,200/mo",
            "type": "Internship",
            "posted": "5 days ago",
            "description": "Work on cutting-edge iOS, macOS, or cloud infrastructure projects. Collaborate with world-class engineers to build products used by billions.",
            "requirements": ["Pursuing CS degree", "Experience with Swift, Objective-C, or C++", "Passion for technology"],
            "url": "https://www.apple.com/careers/",
            "remote": False,
            "experienceLevel": "entry",
        },
        {
            "id": "mock_7",
            "title": "Investment Banking Summer Analyst",
            "company": "JPMorgan Chase",
            "location": "New York, NY",
            "salary": "$10,000/mo",
            "type": "Internship",
            "posted": "1 week ago",
            "description": "Summer internship in Investment Banking. Work on live deals, financial modeling, and client presentations.",
            "requirements": ["Strong academic performance", "Interest in finance", "Analytical mindset"],
            "url": "https://careers.jpmorgan.com/",
            "remote": False,
            "experienceLevel": "entry",
        },
        {
            "id": "mock_8",
            "title": "Product Design Intern",
            "company": "Airbnb",
            "location": "San Francisco, CA",
            "salary": "$7,500/mo",
            "type": "Internship",
            "posted": "3 days ago",
            "description": "Design experiences that make travel more accessible and meaningful. Work on user research, prototyping, and visual design.",
            "requirements": ["Portfolio demonstrating design skills", "Experience with Figma or Sketch", "User-centered design thinking"],
            "url": "https://careers.airbnb.com/",
            "remote": True,
            "experienceLevel": "entry",
        },
        {
            "id": "mock_9",
            "title": "Software Engineering Intern",
            "company": "Amazon",
            "location": "Seattle, WA",
            "salary": "$8,400/mo",
            "type": "Internship",
            "posted": "6 days ago",
            "description": "Build scalable systems that power Amazon's retail, AWS, and Alexa services. Work on distributed systems, machine learning, or frontend applications.",
            "requirements": ["CS or related degree", "Programming experience", "Problem-solving skills"],
            "url": "https://www.amazon.jobs/",
            "remote": False,
            "experienceLevel": "entry",
        },
        {
            "id": "mock_10",
            "title": "Business Development Intern",
            "company": "Salesforce",
            "location": "San Francisco, CA",
            "salary": "$7,800/mo",
            "type": "Internship",
            "posted": "4 days ago",
            "description": "Help drive growth by identifying new business opportunities, building partnerships, and supporting strategic initiatives.",
            "requirements": ["Business or related major", "Strong communication skills", "Entrepreneurial mindset"],
            "url": "https://www.salesforce.com/careers/",
            "remote": True,
            "experienceLevel": "entry",
        },
        {
            "id": "mock_11",
            "title": "Research Intern",
            "company": "Microsoft",
            "location": "Redmond, WA",
            "salary": "$8,600/mo",
            "type": "Internship",
            "posted": "2 days ago",
            "description": "Work on cutting-edge research in AI, cloud computing, or human-computer interaction. Collaborate with world-renowned researchers.",
            "requirements": ["Research experience", "Strong technical background", "Publication record preferred"],
            "url": "https://careers.microsoft.com/",
            "remote": False,
            "experienceLevel": "entry",
        },
        {
            "id": "mock_12",
            "title": "Marketing Intern",
            "company": "Nike",
            "location": "Beaverton, OR",
            "salary": "$7,200/mo",
            "type": "Internship",
            "posted": "1 week ago",
            "description": "Support marketing campaigns, social media strategy, and brand initiatives for one of the world's most recognized brands.",
            "requirements": ["Marketing or communications major", "Creative thinking", "Social media experience"],
            "url": "https://jobs.nike.com/",
            "remote": False,
            "experienceLevel": "entry",
        },
    ]
    
    # Filter by job type if specified
    if job_types and "all" not in [t.lower() for t in job_types]:
        all_jobs = [j for j in all_jobs if j["type"] in job_types]
    
    # Return more jobs (up to 50) to simulate real API behavior
    return all_jobs * 3  # Repeat the list 3 times to get ~36 jobs for internships


# =============================================================================
# SAVED JOBS — student bookmarks
# =============================================================================
# Stored at users/{uid}/savedJobs/{job_id}. The frontend POSTs here when a
# student clicks "Save" on a job card; the read at jobs.py:312 powers the
# "saved company" affinity badge in the feed.

@job_board_bp.route("/saved-jobs", methods=["GET"])
@require_firebase_auth
def list_saved_jobs():
    uid = request.firebase_user["uid"]
    db = get_db()
    snap = db.collection("users").document(uid).collection("savedJobs").stream()
    saved = []
    for d in snap:
        data = d.to_dict() or {}
        data["job_id"] = data.get("job_id") or d.id
        saved_at = data.get("saved_at")
        if hasattr(saved_at, "isoformat"):
            data["saved_at"] = saved_at.isoformat()
        saved.append(data)
    return jsonify({"saved": saved, "count": len(saved)})


@job_board_bp.route("/saved-jobs", methods=["POST"])
@require_firebase_auth
def save_job():
    uid = request.firebase_user["uid"]
    payload = request.get_json(silent=True) or {}
    job_id = payload.get("job_id")
    if not job_id:
        return jsonify({"error": "job_id required"}), 400
    db = get_db()
    doc_ref = db.collection("users").document(uid).collection("savedJobs").document(str(job_id))
    doc_ref.set({
        "job_id": str(job_id),
        "title": payload.get("title"),
        "company": payload.get("company"),
        "location": payload.get("location"),
        "apply_url": payload.get("apply_url"),
        "match_score": payload.get("match_score"),
        "saved_at": firestore.SERVER_TIMESTAMP,
    })
    # A saved job's company becomes a People deck target (OFF-41).
    try:
        from app.services.people_deck_cache import mark_stale
        mark_stale(uid, db, reason="saved_job")
    except Exception:
        pass
    return jsonify({"success": True, "job_id": str(job_id)})


@job_board_bp.route("/saved-jobs/<job_id>", methods=["DELETE"])
@require_firebase_auth
def unsave_job(job_id: str):
    uid = request.firebase_user["uid"]
    db = get_db()
    db.collection("users").document(uid).collection("savedJobs").document(str(job_id)).delete()
    return jsonify({"success": True, "job_id": str(job_id)})

