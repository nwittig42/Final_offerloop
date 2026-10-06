"""
Greenhouse form-filler.

Hits the application form via Browserless-hosted Chromium. Verified against
the captured SpaceX fixture at backend/tests/fixtures/auto_apply/greenhouse_sample.html.

Field model (from the SpaceX fixture — Greenhouse forms are consistent):
  Standard text         : id="first_name" / "last_name" / "preferred_name"
                          id="email" / "phone" / "start-year--0" / "end-year--0"
  File uploads          : id="resume" / "cover_letter" (type=file, visually-hidden)
  Combobox (react-select): id="country" / "candidate-location" /
                            id="school--0" / "degree--0" / "discipline--0" /
                            id="gender" / "hispanic_ethnicity" /
                            id="veteran_status" / "disability_status"
  Custom questions      : id="question_XXXXXXXXX" with label[for=...]
                          label IDs end in "-label"

EEO option mapping is conservative — Greenhouse exposes "Decline to identify"
(or similar wording) as a stable option on the gender / race / veteran /
disability dropdowns. The Application Profile defaults sensitive fields to
"decline" which we translate to that option here.

Dry-run mode fills everything but does not click the Submit button. We always
return a full-page screenshot so the user can verify visually in the modal.
"""
from __future__ import annotations

import base64
import logging
import os
import re
from typing import Any, Callable, Dict, List, Optional

# NOTE: this module currently keeps its own copies of helpers that also
# live in `_form_filler_common.py` (id_selector, check_checkbox,
# fill_combobox, react_force_text, resolve_label_text, etc.). They are
# duplicated intentionally: greenhouse.py is the only filler shipping in
# production and we don't want to risk breaking it during the Lever/Ashby
# build-out. Once those land and stabilise, migrate greenhouse.py to use
# the shared module and delete the private duplicates here in a single
# clean refactor pass.


logger = logging.getLogger(__name__)

# Build marker so we can verify at runtime which revision of this module is
# loaded. Prints to stdout on import. When you Ctrl+C and restart the
# backend, this line should appear in the log — if it doesn't, the backend
# isn't actually reloading and any "test" is running against the old code.
_BUILD_TAG = "greenhouse-v19-needs-verification-route"
print(f"[auto_apply] greenhouse.py loaded — build={_BUILD_TAG}")


# Callback signature used by the runner to query the per-user answer library
# from inside the filler. The filler invokes it for any custom question its
# rule-based classifier couldn't resolve.
#   answer_lookup(label_text, field_type, options) -> Optional[Any]
AnswerLookup = Callable[[str, str, Optional[List[str]]], Optional[Any]]


# ---------- public entry point ----------

def run_greenhouse_filler(
    apply_url: str,
    preview: Dict[str, Any],
    edited_answers: Dict[str, str],
    resume_path: Optional[str],
    dry_run: bool,
    job_id: str = "",
    answer_lookup: Optional[AnswerLookup] = None,
    *,
    uid: str = "",
    resume_summary: str = "",
    job_data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Drive the Greenhouse application form. Returns a result dict the
    runner persists to the autoApplyJobs doc."""
    print(f"\n[auto_apply] === START Greenhouse filler ===", flush=True)
    print(f"[auto_apply]   uid={uid!r} job_id={job_id!r} dry_run={dry_run}", flush=True)
    print(f"[auto_apply]   apply_url={apply_url!r}", flush=True)
    print(f"[auto_apply]   company={(job_data or {}).get('company')!r} title={(job_data or {}).get('title')!r}", flush=True)
    print(f"[auto_apply]   edited_answers keys: {list((edited_answers or {}).keys())}", flush=True)
    print(f"[auto_apply]   resume_path: {resume_path!r}", flush=True)

    candidate_urls = _candidate_apply_urls(apply_url, job_id)
    if not candidate_urls:
        print(f"[auto_apply] FAIL: no usable apply_url", flush=True)
        return _failure("no usable apply_url for greenhouse")
    print(f"[auto_apply]   candidate URLs to try: {candidate_urls}", flush=True)

    # Browserbase session: stealth defeats reCAPTCHA's behavioral signals,
    # solveCaptchas auto-handles reCAPTCHA / hCaptcha if it does score us low.
    # The leftover Greenhouse-tenant defense is the per-tenant email-code
    # gate (Temelio etc.) — handled below after submit by reading the
    # candidate's Gmail for the verification code.
    from app.services.auto_apply.browserbase_client import (
        BrowserbaseError, create_session, release_session,
    )
    print(f"[auto_apply] creating Browserbase session (stealth=True, solveCaptchas=True)...", flush=True)
    try:
        session_id, ws_url = create_session(stealth=True, solve_captchas=True)
        print(f"[auto_apply]   browserbase session_id={session_id}", flush=True)
    except BrowserbaseError as exc:
        print(f"[auto_apply] FAIL: browserbase session create: {exc}", flush=True)
        return _failure(str(exc))

    # Lazy import so the rest of the auto_apply package keeps loading even if
    # playwright isn't installed (e.g. during early dev / partial deploys).
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        release_session(session_id)
        return _failure("playwright not installed; pip install playwright")

    filled: Dict[str, str] = {}
    unmapped: List[Dict[str, str]] = []
    # Prepared answers — the set of {label, answer, source} records the
    # verification-required UX surfaces to the user when CAPTCHA blocks
    # submit. Populated lazily by _fill_custom_questions and the standard
    # field passes; empty for early-failure paths (no apply URL, etc).
    prepared_answers: List[Dict[str, Any]] = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(ws_url, timeout=60_000)
            try:
                # Browserbase pre-creates a context+page when the session
                # starts; reuse them instead of opening a second tab.
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                page = context.pages[0] if context.pages else context.new_page()

                # Some apply_urls point at the company's own careers page
                # (stripe.com/jobs/123, databricks.com/careers/...?gh_jid=X)
                # instead of Greenhouse — the Greenhouse Boards API returns
                # the company's custom URL when configured. We reconstruct
                # the direct Greenhouse URL from the job_id and try multiple
                # candidates. Whichever one actually renders #first_name wins.
                landed_on: Optional[str] = None
                attempt_log: List[Dict[str, str]] = []
                print(f"[auto_apply] === LOADING APPLY PAGE ===", flush=True)
                for candidate in candidate_urls:
                    print(f"[auto_apply]   trying URL: {candidate}", flush=True)
                    try:
                        page.goto(candidate, wait_until="domcontentloaded", timeout=20_000)
                        # Early-exit if the page redirected off Greenhouse —
                        # the form is never going to appear, and waiting 25s
                        # for #first_name eats our Browserless session budget.
                        final_url = page.url
                        if "greenhouse.io" not in final_url.lower():
                            print(f"[auto_apply]     redirected off greenhouse → {final_url}", flush=True)
                            attempt_log.append({
                                "url": candidate,
                                "result": "redirected off greenhouse",
                                "final_url": final_url,
                            })
                            continue
                        # Multi-selector wait: not every Greenhouse tenant uses
                        # `#first_name`. Custom form templates (seen on
                        # Cloudflare + MSF's `job-boards.greenhouse.io` runs
                        # 2026-07-14) render with `name="job_application[..]"`
                        # or `autocomplete="given-name"` styling instead. Wait
                        # for ANY of the known first-name shapes — Playwright
                        # comma-separated wait resolves on the first match.
                        _FIRST_NAME_MARKERS = ",".join([
                            "#first_name",
                            'input[name="job_application[first_name]"]',
                            'input[name="candidate[first_name]"]',
                            'input[autocomplete="given-name"]',
                            'input[aria-label*="first name" i]',
                        ])
                        page.wait_for_selector(_FIRST_NAME_MARKERS, timeout=12_000)
                        landed_on = candidate
                        attempt_log.append({"url": candidate, "result": "ok"})
                        print(f"[auto_apply]     SUCCESS: form field found, page is the Greenhouse form", flush=True)
                        print(f"[auto_apply]     page title: {page.title()!r}", flush=True)
                        break
                    except PWTimeout:
                        try:
                            final_url = page.url
                        except Exception:
                            final_url = candidate
                        # DOM peek on failure: capture the top form inputs on
                        # the page so the next audit reveals whether the form
                        # was there under a different selector, or genuinely
                        # missing. Cheap (~1 evaluate), only runs on failure.
                        dom_peek = None
                        try:
                            dom_peek = page.evaluate(
                                """() => {
                                    const inputs = Array.from(
                                        document.querySelectorAll('input, textarea, select')
                                    ).slice(0, 20).map(el => ({
                                        tag: el.tagName.toLowerCase(),
                                        id: el.id || null,
                                        name: el.getAttribute('name'),
                                        type: el.getAttribute('type'),
                                        aria: el.getAttribute('aria-label'),
                                    }));
                                    return {
                                        title: document.title,
                                        input_count: document.querySelectorAll('input').length,
                                        inputs,
                                    };
                                }"""
                            )
                        except Exception:
                            pass
                        entry = {
                            "url": candidate,
                            "result": "no first-name selector matched",
                            "final_url": final_url,
                        }
                        if dom_peek:
                            entry["dom_peek"] = dom_peek
                        attempt_log.append(entry)
                    except Exception as exc:
                        attempt_log.append({
                            "url": candidate,
                            "result": f"nav error: {exc}",
                        })
                if not landed_on:
                    # Capture failure screenshot so Sid can SEE where we ended
                    # up. This is the single most useful piece of diagnostic
                    # info for the next debugging round.
                    failure_b64 = None
                    try:
                        failure_b64 = base64.b64encode(
                            page.screenshot(full_page=True)
                        ).decode("ascii")
                    except Exception:
                        pass
                    return _failure(
                        "Greenhouse form did not render at any candidate URL",
                        attempted_urls=candidate_urls,
                        attempt_log=attempt_log,
                        screenshot_b64=failure_b64,
                    )

                print(f"[auto_apply] === FILLING STANDARD FIELDS (name, email, phone, country, location) ===", flush=True)
                _fill_standard_fields(page, preview, filled, unmapped)
                _record_standard_prepared(preview, prepared_answers)
                print(f"[auto_apply]   standard fields filled: {list(filled.keys())}", flush=True)
                print(f"[auto_apply]   standard fields unmapped: {[u.get('field_id') for u in unmapped]}", flush=True)

                print(f"[auto_apply] === FILLING EEO SECTION ===", flush=True)
                _fill_eeo_section(page, preview, filled, unmapped)

                print(f"[auto_apply] === UPLOADING RESUME ===", flush=True)
                _upload_resume(page, resume_path, filled, unmapped)
                print(f"[auto_apply]   resume status: {filled.get('resume', 'not attempted')}", flush=True)

                print(f"[auto_apply] === FILLING CUSTOM QUESTIONS (classify -> resolve -> fill) ===", flush=True)
                _fill_custom_questions(
                    page=page,
                    preview=preview,
                    edited_answers=edited_answers,
                    filled=filled,
                    unmapped=unmapped,
                    uid=uid,
                    resume_summary=resume_summary,
                    job=job_data or {},
                    prepared_answers=prepared_answers,
                )
                print(f"[auto_apply]   custom fill complete: {len(filled)} filled, {len(unmapped)} unmapped", flush=True)

                screenshot_bytes = page.screenshot(full_page=True)
                screenshot_b64 = base64.b64encode(screenshot_bytes).decode("ascii")

                # Aggressive-submit: click Submit even if the classifier thinks
                # some required fields are unmapped. The classifier over-flags
                # required (e.g., ATS marks a field optional at the server but
                # we saw the * asterisk in the DOM), and Greenhouse's own
                # aria-invalid response is the authoritative signal. If real
                # required fields are empty the post-submit refill-and-resubmit
                # path (below) will catch them and re-run the resolver, or
                # escalate to needs_attention on the SECOND miss. Halting here
                # was routing lots of otherwise-submittable jobs to the drawer
                # (baseline audit 2026-07-20: 22% of runs landed in
                # needs_attention pre-submit, most on "Location (City)",
                # "First Name", or "* Optional Personal Preferences" false
                # positives).
                pre_submit_pending_count = sum(1 for u in unmapped if u.get("required") is True)
                if pre_submit_pending_count and not dry_run:
                    print(f"[auto_apply]   classifier flagged {pre_submit_pending_count} required field(s) as unmapped — submitting anyway, aria-invalid will be authoritative", flush=True)

                # NOTE: The previous early-bail on detect_captcha_challenge was
                # removed in the Browserbase migration. Greenhouse's reCAPTCHA
                # widget is ALWAYS present in the DOM when the form renders,
                # so that check effectively returned "needs_verification" for
                # every submit attempt — bypassing the post-submit signals
                # entirely. With Browserbase's stealth + solveCaptchas the
                # widget can be present AND scored cleanly; the right verdict
                # comes from clicking submit and checking what actually
                # happened (success URL, email-code gate, aria-invalid).

                status = "dry_run_complete"
                failure_reason: Optional[str] = None
                if not dry_run:
                    print(f"[auto_apply] === CLICKING SUBMIT ===", flush=True)
                    try:
                        submit = page.query_selector('button[type="submit"]')
                        if not submit:
                            status = "submit_failed"
                            failure_reason = "no submit button found"
                            print(f"[auto_apply]   FAIL: no submit button on page", flush=True)
                        else:
                            # Stamp the submit click time so we can window the
                            # Gmail search for the verification code email — old
                            # codes shouldn't be picked up if Sid happens to
                            # have prior Greenhouse emails in the inbox.
                            import time as _time_now
                            submit_ts = int(_time_now.time())
                            print(f"[auto_apply]   clicking submit at ts={submit_ts}", flush=True)
                            submit.click()
                            page.wait_for_load_state("networkidle", timeout=30_000)
                            print(f"[auto_apply]   networkidle reached, URL now: {page.url!r}", flush=True)

                            # Per-tenant email-verification gate (Temelio,
                            # confirmed 2026-06-18): 8 new fields `#security-input-{0..7}`
                            # appear and Greenhouse emails an 8-char alphanumeric
                            # code to the candidate. The verification UI is
                            # rendered client-side AFTER the submit POST + email
                            # dispatch return. networkidle fires before that
                            # render completes, and we measured at least one
                            # case where the inputs took >5s to appear after
                            # networkidle — so poll for 30s.
                            print(f"[auto_apply]   polling for #security-input-0 (email-code gate detection, up to 30s)...", flush=True)
                            verification_visible = False
                            for _ in range(30):
                                if page.query_selector("#security-input-0"):
                                    verification_visible = True
                                    break
                                page.wait_for_timeout(1000)
                            print(f"[auto_apply]   email-verification UI visible: {verification_visible}", flush=True)
                            if verification_visible:
                                candidate_email = (preview.get("fields") or {}).get("email") or ""
                                print(f"[auto_apply] === EMAIL-CODE PATH triggered ===", flush=True)
                                _try_email_code_completion(
                                    page=page, uid=uid,
                                    candidate_email=candidate_email,
                                    submit_ts=submit_ts,
                                )
                                page.wait_for_load_state("networkidle", timeout=30_000)
                                print(f"[auto_apply]   post-email-code URL: {page.url!r}", flush=True)

                            screenshot_bytes = page.screenshot(full_page=True)
                            screenshot_b64 = base64.b64encode(screenshot_bytes).decode("ascii")

                            # Real success detection: Greenhouse re-renders
                            # the SAME page with aria-invalid="true" markers
                            # when required fields are empty. networkidle fires
                            # either way, so we can't trust it alone. Three
                            # signals we cross-check:
                            #   1. Are there any aria-invalid="true" fields?
                            #      → validation error
                            #   2. Is #first_name still on the page?
                            #      → form didn't unmount → likely still on form
                            #   3. Did the URL change to a /thank_you /
                            #      /confirmation / /apply/confirmation path?
                            #      → real success
                            invalid_count = 0
                            try:
                                invalid_count = page.evaluate(
                                    "() => document.querySelectorAll('[aria-invalid=\"true\"]').length"
                                )
                            except Exception:
                                pass
                            form_still_present = page.query_selector("#first_name") is not None
                            url_after = (page.url or "").lower()
                            success_url = any(
                                kw in url_after
                                for kw in ("thank", "confirmation", "/success", "complete")
                            )
                            print(f"[auto_apply] === POST-SUBMIT SIGNALS ===", flush=True)
                            print(f"[auto_apply]   final URL: {url_after}", flush=True)
                            print(f"[auto_apply]   success_url marker present: {success_url}", flush=True)
                            print(f"[auto_apply]   aria-invalid field count: {invalid_count}", flush=True)
                            print(f"[auto_apply]   #first_name still on page (form not unmounted): {form_still_present}", flush=True)

                            if success_url:
                                status = "submitted"
                                print(f"[auto_apply]   STATUS: submitted (success URL detected)", flush=True)
                            elif invalid_count > 0:
                                # Submit-and-learn: Greenhouse tells us which
                                # fields it needed via aria-invalid. Many of
                                # these are fields the pre-submit classifier
                                # missed because they don't use the
                                # question_X-label markup (Zscaler-style
                                # checkbox groups, custom-id Sex dropdowns,
                                # GDPR consent boxes). Run them back through
                                # the same brain that handles custom
                                # questions — slot match catches Sex,
                                # consent fast-path catches privacy /
                                # confidential / consent — and resubmit once
                                # before escalating to the drawer.
                                validation_pending = _extract_invalid_field_questions(page)
                                if validation_pending:
                                    status, validation_pending, screenshot_b64 = (
                                        _resolve_refill_and_resubmit(
                                            page=page,
                                            invalid_fields=validation_pending,
                                            preview=preview,
                                            uid=uid,
                                            resume_summary=resume_summary,
                                            job_data=job_data,
                                            filled=filled,
                                            unmapped=unmapped,
                                            current_screenshot_b64=screenshot_b64,
                                            edited_answers=edited_answers,
                                        )
                                    )
                                    if status == "submitted":
                                        # Retry succeeded — fall through to
                                        # the success return at the bottom.
                                        pass
                                    elif validation_pending:
                                        validation_pending = _dedupe_pending_by_label(validation_pending)
                                        print(f"[auto_apply]   post-submit pending questions (deduped): {len(validation_pending)}", flush=True)
                                        return {
                                            "status": "needs_attention",
                                            "filled": filled,
                                            "unmapped": unmapped,
                                            "pending_questions": validation_pending,
                                            "prepared_answers": prepared_answers,
                                            "screenshot_b64": screenshot_b64,
                                            "failure_reason": None,
                                        }
                                    else:
                                        # No pending and not submitted (e.g.
                                        # retry hit an exception). Fall
                                        # through to submit_failed.
                                        status = "submit_failed"
                                        failure_reason = (
                                            f"validation errors on {invalid_count} "
                                            "field(s); retry resolver did not "
                                            "complete cleanly"
                                        )
                                else:
                                    # Couldn't extract any field labels —
                                    # fall through to the original
                                    # submit_failed path.
                                    status = "submit_failed"
                                    failure_reason = (
                                        f"validation errors on {invalid_count} field(s) "
                                        "— most likely unmapped required questions. "
                                        "See the unmapped list."
                                    )
                            elif form_still_present:
                                status = "submit_failed"
                                failure_reason = (
                                    "form is still on the page after Submit click — "
                                    "submission was not accepted (likely silent validation)"
                                )
                                print(f"[auto_apply]   STATUS: submit_failed (form still present, no markers)", flush=True)
                            else:
                                # Form unmounted, no error markers, URL didn't
                                # change obviously — best-effort: assume success.
                                status = "submitted"
                                print(f"[auto_apply]   STATUS: submitted (form unmounted, best-effort)", flush=True)
                    except PWTimeout:
                        status = "submit_failed"
                        failure_reason = "submit network-idle timeout"
                        print(f"[auto_apply]   STATUS: submit_failed (network-idle timeout)", flush=True)
                    except Exception as exc:
                        status = "submit_failed"
                        failure_reason = f"submit click failed: {exc}"

                print(f"\n[auto_apply] === FINAL RESULT ===", flush=True)
                print(f"[auto_apply]   status: {status}", flush=True)
                print(f"[auto_apply]   failure_reason: {failure_reason}", flush=True)
                print(f"[auto_apply]   filled: {len(filled)} fields", flush=True)
                print(f"[auto_apply]   unmapped: {len(unmapped)} fields", flush=True)
                if unmapped:
                    for u in unmapped[:5]:
                        print(f"[auto_apply]     - unmapped: {u.get('field_id')!r} ({u.get('label', '')[:60]!r}) reason={u.get('reason')!r}", flush=True)
                print(f"[auto_apply] === END Greenhouse filler ===\n", flush=True)
                return {
                    "status": status,
                    "filled": filled,
                    "unmapped": unmapped,
                    "pending_questions": [],
                    "prepared_answers": prepared_answers,
                    "screenshot_b64": screenshot_b64,
                    "failure_reason": failure_reason,
                }
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
                # Stop the Browserbase per-second billing clock immediately;
                # leaving the session open until plan-side timeout would
                # waste unit budget on no real work.
                release_session(session_id)
                print(f"[auto_apply]   browserbase session {session_id} released", flush=True)
    except Exception as exc:
        logger.exception("greenhouse filler crashed")
        release_session(session_id)
        return _failure(f"{type(exc).__name__}: {exc}", filled=filled, unmapped=unmapped)


# ---------- checkbox-group collapsing ("select all that apply") ----------

def _group_checkbox_groups(page, classified_fields, meta_by_id):
    """Greenhouse renders 'select all that apply' multi-checkbox questions as
    N <input type="checkbox"> children of one <fieldset>. Each child shares
    the fieldset's legend text as its 'label' in our classifier, but has
    its OWN `<label for=checkbox_id>` carrying the option name (LinkedIn,
    Indeed, etc.). Without this collapse, the LLM sees 14 identical questions
    and tries to YES/NO each one — checking everything is wrong because
    Greenhouse rejects "you can't pick all of these for a question that asks
    one source."

    Collapse to a single virtual question: keep the first checkbox's
    field_id as the representative, populate `options` with the per-checkbox
    option texts, and store `group_members` so the fill step can locate the
    matching checkbox after the LLM picks an answer.
    """
    # Group field_ids by normalized (label, field_type=checkbox)
    groups: Dict[str, List[str]] = {}
    for f in classified_fields:
        if f.get("field_type") != "checkbox":
            continue
        label_key = (f.get("label") or "").strip().lower()
        for suf in ("(select all that apply)", "(select one)", "*"):
            label_key = label_key.replace(suf, "").strip()
        if not label_key:
            continue
        groups.setdefault(label_key, []).append(str(f["field_id"]))

    # Only collapse groups with ≥2 members
    collapse_groups = {k: ids for k, ids in groups.items() if len(ids) >= 2}
    if not collapse_groups:
        return classified_fields, meta_by_id

    # Fetch each checkbox's individual option text from the DOM
    print(
        f"[auto_apply.classify] collapsing {len(collapse_groups)} multi-checkbox group(s): "
        f"{ {k: len(v) for k, v in collapse_groups.items()} }",
        flush=True,
    )
    all_member_ids = [fid for ids in collapse_groups.values() for fid in ids]
    try:
        option_map = page.evaluate(
            """(ids) => {
                const out = {};
                for (const id of ids) {
                    let text = '';
                    try {
                        const esc = (window.CSS && CSS.escape) ? CSS.escape(id) : id;
                        const lbl = document.querySelector(`label[for="${esc}"]`);
                        if (lbl) text = (lbl.textContent || '').trim();
                    } catch (e) {}
                    if (!text) {
                        const el = document.getElementById(id);
                        if (el) {
                            text = el.value || el.getAttribute('aria-label') || '';
                            text = (text || '').trim();
                        }
                    }
                    out[id] = text;
                }
                return out;
            }""",
            all_member_ids,
        )
    except Exception as exc:
        print(f"[auto_apply.classify] checkbox-group option-text fetch failed: {exc}", flush=True)
        return classified_fields, meta_by_id

    # Build the collapsed structures: keep first member's field_id as the
    # representative; mark the rest for removal.
    drop_ids: set = set()
    representative_ids: Dict[str, str] = {}  # label_key -> representative field_id
    for label_key, ids in collapse_groups.items():
        rep_id = ids[0]
        representative_ids[label_key] = rep_id
        # Pull option text per member; skip empty (no <label for>) cases
        members = [
            {"field_id": fid, "option_text": (option_map.get(fid) or "").strip()}
            for fid in ids
        ]
        members = [m for m in members if m["option_text"]]
        if not members:
            continue
        options = [m["option_text"] for m in members]

        # Patch the representative's meta
        if rep_id in meta_by_id:
            meta_by_id[rep_id]["options"] = options
            meta_by_id[rep_id]["is_checkbox_group"] = True
            meta_by_id[rep_id]["group_members"] = members
            # Treat as a select-shaped question for the LLM so it picks ONE
            # option from the list rather than YES/NO-ing a checkbox.
            meta_by_id[rep_id]["field_type"] = "select"
            meta_by_id[rep_id]["is_combobox"] = False

        drop_ids.update(ids[1:])
        print(
            f"[auto_apply.classify]   group {label_key!r}: {len(members)} options, "
            f"representative={rep_id!r}, drop={len(ids)-1}",
            flush=True,
        )

    # Filter classified_fields + meta_by_id
    new_classified: List[Dict[str, Any]] = []
    for f in classified_fields:
        fid = str(f.get("field_id") or "")
        if fid in drop_ids:
            continue
        # Sync the representative's options + field_type into the
        # classified_fields entry (which is what the LLM batch reads).
        if fid in meta_by_id and meta_by_id[fid].get("is_checkbox_group"):
            f["options"] = meta_by_id[fid]["options"]
            f["field_type"] = "select"
        new_classified.append(f)
    for fid in drop_ids:
        meta_by_id.pop(fid, None)
    return new_classified, meta_by_id


# ---------- pending-question dedup ----------

def _dedupe_pending_by_label(pending: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Collapse duplicate-label entries so the drawer only asks once.

    Greenhouse's 'select all that apply' fields render as N individual
    checkboxes that share the parent fieldset's <legend> text — our
    classifier captures each as a separate required question with the
    SAME label ('How did you hear about this opportunity?' x14). Same
    pattern hit on Lever's Pronouns (6 checkboxes one label). Showing
    14 identical rows in the Needs Attention drawer is broken UX and
    the user has no way to tell them apart anyway.

    Keep the first entry per (normalized label, field_type) tuple,
    drop the rest. The first entry retains the canonical field_id so
    the resolve endpoint can route the user's answer back to its
    parent group.
    """
    seen: set = set()
    out: List[Dict[str, Any]] = []
    for p in pending:
        label = (p.get("label") or "").strip().lower()
        # Trim trailing `*` required markers and standard "(select all that apply)"
        # suffix so variants don't escape the dedup.
        for suffix in ("(select all that apply)", "(select one)", "*"):
            label = label.replace(suffix, "").strip()
        ftype = (p.get("field_type") or "").lower()
        key = (label, ftype)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


# ---------- email-code completion (Greenhouse per-tenant verification gate) ----------

def _try_email_code_completion(
    page, uid: str, candidate_email: str, submit_ts: int,
) -> bool:
    """If Greenhouse routed to the email-verification page (8x #security-input-*
    boxes for an 8-char alphanumeric code), poll the candidate's Gmail for
    the code, fill the boxes, and click submit again.

    Returns True if we filled the code and resubmitted (caller re-runs the
    success-signal check afterward); False if the user has no Gmail
    connected or the code never arrived within the poll window. On False
    the caller falls through to the existing needs_verification flow so
    the user can complete the code entry in their own browser.
    """
    from app.services.gmail_client import search_for_verification_code

    print(f"[auto_apply.emailcode] polling gmail for greenhouse verification code "
          f"(uid={uid}, since={submit_ts - 10})", flush=True)
    # Greenhouse actually sends verification emails from
    # no-reply@us.greenhouse-mail.io (or .eu/.ap regional variants), NOT
    # from greenhouse.io. Match on the -mail.io domain and also OR in the
    # distinctive body phrase as a belt-and-suspenders fallback for any
    # tenant deployment that uses a different sender domain.
    code = search_for_verification_code(
        uid,
        sender_pattern='from:greenhouse-mail.io OR "security code field"',
        # Anchor on the literal "application:" that immediately precedes
        # the code in Greenhouse's email body. Newline-anchored regexes
        # don't work because the HTML→text parser collapses \n into
        # spaces, but the word "application:" is stable across tenants.
        code_regex=r"application:\s+([A-Za-z0-9]{8})\b",
        since_epoch_seconds=submit_ts - 10,  # 10s buffer for clock skew
        max_wait_seconds=90,  # gmail search index can lag 30-60s
        poll_interval_seconds=5,
    )
    if not code:
        print(f"[auto_apply.emailcode] no code found in 60s — falling through to needs_verification",
              flush=True)
        return False

    print(f"[auto_apply.emailcode] CODE CAPTURED: {code} — filling 8 boxes", flush=True)
    try:
        for i, char in enumerate(code[:8]):
            sel = f"#security-input-{i}"
            if page.query_selector(sel):
                page.fill(sel, char)
                print(f"[auto_apply.emailcode] filled {sel} = {char!r}", flush=True)
            else:
                print(f"[auto_apply.emailcode] WARN: {sel} not found", flush=True)
        page.wait_for_timeout(500)
        # Greenhouse's 2FA-style UI sometimes auto-submits on the last
        # character; click Submit anyway in case it doesn't.
        submit = page.query_selector('button[type="submit"]')
        if submit:
            print("[auto_apply.emailcode] clicking submit again after code fill", flush=True)
            submit.click()
        else:
            print("[auto_apply.emailcode] no submit button found after code fill", flush=True)
        # Greenhouse validates the code via AJAX after click — give the page
        # time to update in-place (URL may not change on success).
        page.wait_for_timeout(3000)
        return True
    except Exception as exc:
        print(f"[auto_apply.emailcode] fill/submit failed: {exc}", flush=True)
        return False


# ---------- standard fields ----------

def _record_standard_prepared(
    preview: Dict[str, Any], prepared_answers: List[Dict[str, Any]]
) -> None:
    """Snapshot the standard-field values from `preview.fields` into the
    prepared_answers list so the verification UX can show them to the
    user. Cheap and idempotent — doesn't actually inspect the page."""
    from app.services.auto_apply import _form_filler_common as _c
    fields = preview.get("fields") or {}
    plan = [
        ("first_name", "First Name"),
        ("last_name", "Last Name"),
        ("full_name", "Full Name"),
        ("email", "Email"),
        ("phone", "Phone"),
        ("location", "Location"),
        ("linkedin_url", "LinkedIn URL"),
        ("github_url", "GitHub URL"),
        ("portfolio_url", "Portfolio / Website URL"),
    ]
    for key, label in plan:
        value = fields.get(key)
        if value:
            _c.record_prepared_answer(
                prepared_answers, field_id=key, label=label,
                answer=value, field_type="text", source="profile",
            )


def _fill_standard_fields(
    page, preview: Dict[str, Any], filled: Dict[str, str], unmapped: List
) -> None:
    fields = preview.get("fields") or {}
    # Greenhouse customizes which "preferred name" inputs a tenant exposes.
    # Some tenants use a single `#preferred_name`, others split into
    # `#preferred_first_name` + `#preferred_last_name` (Samsara, etc.). We
    # try all the variants; only the ones the page actually renders get
    # filled (_fill_text_if_present is a no-op when the selector misses).
    plan = [
        ("#first_name", fields.get("first_name")),
        ("#last_name", fields.get("last_name")),
        ("#preferred_name", fields.get("first_name")),
        ("#preferred_first_name", fields.get("first_name")),
        ("#preferred_last_name", fields.get("last_name")),
        ("#email", fields.get("email")),
        ("#phone", fields.get("phone")),
    ]
    for selector, value in plan:
        if not value:
            continue
        _fill_text_if_present(page, selector, value, filled)

    # Label-driven pass for fields whose IDs vary by tenant (Samsara uses
    # different ids than Stripe for "Preferred First Name", etc). We match
    # label TEXT against canonical fields so the fill is robust to id
    # naming. Runs after the id-based plan above so explicit id matches
    # win when both apply.
    _fill_by_label_text(page, fields, filled)

    # #country is a react-select combobox on most Greenhouse forms (separate
    # from #candidate-location which is the city). Default "United States" —
    # almost all Offerloop students are US-based. When we eventually carry
    # explicit country on the Application Profile, swap to that.
    if page.query_selector("#country"):
        _fill_combobox(page, "#country", "United States", filled, unmapped)

    location = fields.get("location")
    if location and page.query_selector("#candidate-location"):
        _fill_combobox(page, "#candidate-location", location, filled, unmapped)


def _fill_by_label_text(
    page, fields: Dict[str, Any], filled: Dict[str, str]
) -> None:
    """Walk every text-style input on the page, resolve its label via three
    strategies (label[for=id], wrapping <label>, aria-labelledby), match the
    label text against canonical fields, and fill. Robust to per-tenant id
    naming AND to whether the form uses for= / wrapping / aria-labelledby."""
    first_name = (fields.get("first_name") or "").strip()
    last_name = (fields.get("last_name") or "").strip()
    full_name = (fields.get("full_name") or f"{first_name} {last_name}").strip()
    email = (fields.get("email") or "").strip()
    phone = (fields.get("phone") or "").strip()

    # (substring patterns to match against lower-cased label text, value to fill)
    label_rules: List[tuple] = [
        (("preferred first name", "preferred first"), first_name),
        (("preferred last name", "preferred last"), last_name),
        (("preferred name", "nickname", "go by", "what should we call"), first_name),
        (("full name", "legal name"), full_name),
        (("email address",), email),
        (("phone number", "mobile number", "cell phone"), phone),
    ]

    try:
        pairs = page.evaluate(
            """() => {
                const out = [];
                const seen = new Set();
                const inputs = document.querySelectorAll(
                    'input[type="text"], input[type="email"], input[type="tel"], input:not([type]), textarea'
                );
                inputs.forEach((el) => {
                    const id = el.id || el.name || '';
                    if (!id || seen.has(id)) return;
                    seen.add(id);

                    let label = '';
                    // 1. label[for=id]
                    try {
                        const esc = (window.CSS && CSS.escape) ? CSS.escape(id) : id;
                        const direct = document.querySelector(`label[for="${esc}"]`);
                        if (direct) label = (direct.textContent || '').trim();
                    } catch (e) {}
                    // 2. wrapping <label>
                    if (!label) {
                        const parent = el.closest && el.closest('label');
                        if (parent) label = (parent.textContent || '').trim();
                    }
                    // 3. aria-labelledby
                    if (!label) {
                        const lb = el.getAttribute && el.getAttribute('aria-labelledby');
                        if (lb) {
                            const node = document.getElementById(lb);
                            if (node) label = (node.textContent || '').trim();
                        }
                    }
                    if (label) out.push({id: id, label: label.toLowerCase()});
                });
                return out;
            }"""
        )
    except Exception:
        return

    if not isinstance(pairs, list):
        return

    for pair in pairs:
        try:
            input_id = pair.get("id")
            text = pair.get("label") or ""
        except AttributeError:
            continue
        if not input_id or not text:
            continue
        selector = _id_selector(input_id)
        # Skip if the id-based plan already touched this selector
        if selector in filled:
            continue
        if not page.query_selector(selector):
            continue
        for patterns, value in label_rules:
            if not value:
                continue
            if any(p in text for p in patterns):
                _fill_text_if_present(page, selector, value, filled)
                break


# ---------- EEO ----------

# Map our internal Application Profile values to the wording Greenhouse uses
# on the dropdowns. "decline" → the literal "Decline to identify" option.
_EEO_OPTION_MAP = {
    "gender": {
        "male": "Male",
        "female": "Female",
        "non_binary": "Non-binary",
        "decline": "Decline to identify",
    },
    "hispanic_ethnicity": {
        "hispanic": "Yes",
        "not_hispanic": "No",
        "decline": "Decline to identify",
    },
    "veteran_status": {
        "not_veteran": "I am not a protected veteran",
        "veteran": "I am a veteran",
        "disabled_veteran": "I am a disabled veteran",
        "decline": "I don't wish to answer",
    },
    "disability_status": {
        "yes": "Yes, I have a disability",
        "no": "No, I don't have a disability",
        "decline": "I don't wish to answer",
    },
}


def _fill_eeo_section(
    page, preview: Dict[str, Any], filled: Dict[str, str], unmapped: List
) -> None:
    answers = preview.get("structured_answers") or {}
    pairs = [
        ("#gender", "gender", answers.get("gender")),
        ("#hispanic_ethnicity", "hispanic_ethnicity", answers.get("ethnicity")),
        ("#veteran_status", "veteran_status", answers.get("veteran_status")),
        ("#disability_status", "disability_status", answers.get("disability_status")),
    ]
    for selector, key, internal_value in pairs:
        if not page.query_selector(selector):
            continue
        option_text = _EEO_OPTION_MAP.get(key, {}).get(
            internal_value or "decline",
            _EEO_OPTION_MAP[key].get("decline", "Decline to identify"),
        )
        _fill_combobox(page, selector, option_text, filled, unmapped)


# ---------- resume upload ----------

def _upload_resume(
    page, resume_path: Optional[str], filled: Dict[str, str], unmapped: List
) -> None:
    if not resume_path or not os.path.exists(resume_path):
        unmapped.append({"field_id": "resume", "label": "Resume",
                          "reason": "no resume file available"})
        return
    target = page.query_selector("#resume")
    if not target:
        unmapped.append({"field_id": "resume", "label": "Resume",
                          "reason": "Greenhouse resume input not found"})
        return
    try:
        page.set_input_files("#resume", resume_path)
        filled["resume"] = "uploaded"
    except Exception as exc:
        unmapped.append({"field_id": "resume", "label": "Resume",
                          "reason": f"upload failed: {exc}"})


# ---------- custom questions ----------

def _fill_custom_questions(
    page,
    preview: Dict[str, Any],
    edited_answers: Dict[str, str],
    filled: Dict[str, str],
    unmapped: List,
    *,
    uid: str,
    resume_summary: str,
    job: Dict[str, Any],
    prepared_answers: Optional[List[Dict[str, Any]]] = None,
) -> None:
    """Greenhouse custom questions: <label id="question_X-label" for="question_X">{text}</label>.

    V2 brain: walk every custom question on the form, classify each input,
    then resolve the whole batch through `auto_answer_form_questions`. That
    function handles sensitive profile paths, preferences (NEEDS_USER),
    library hits, and the single batched LLM call with truthfulness rules.

    No per-tenant keyword logic in this file anymore — the resolver works
    across Greenhouse, Lever, and Ashby on the same contract."""
    from app.services.auto_apply.screening_answers import auto_answer_form_questions

    profile = preview.get("_application_profile") or {}
    classified_fields: List[Dict[str, Any]] = []
    meta_by_id: Dict[str, Dict[str, Any]] = {}

    # Pass 1: Greenhouse-native custom questions with the standard
    # `label[id="question_X-label"]` wrapper. These are the most reliable
    # to classify because the label markup is explicit.
    labels = page.query_selector_all('label[id^="question_"][id$="-label"]')
    for label in labels:
        try:
            label_text = (label.inner_text() or "").strip()
            field_id = label.get_attribute("for")
        except Exception:
            continue
        if not field_id:
            continue
        # Attribute selector tolerates array-style ids like "question_X[]".
        selector = _id_selector(field_id)
        if not page.query_selector(selector):
            unmapped.append(_unmapped_entry(
                page, selector, field_id, label_text,
                reason="field not interactable",
            ))
            continue

        field_type = _detect_field_type(page, selector)
        options = _detect_options(page, selector)
        required = _detect_required(page, selector)
        is_combobox = field_type in ("select", "radio")

        classified_fields.append({
            "field_id": field_id,
            "label": label_text,
            "field_type": field_type,
            "options": options,
            "required": required,
        })
        print(
            f"[auto_apply.classify] pre-submit: field_id={field_id!r} "
            f"label={label_text!r} field_type={field_type!r} required={required}",
            flush=True,
        )
        meta_by_id[field_id] = {
            "selector": selector,
            "label": label_text,
            "field_type": field_type,
            "options": options,
            "required": required,
            "is_combobox": is_combobox,
        }

    # Pass 2: required fields that aren't wrapped in the question_X-label
    # markup. Tenant-specific custom EEO (Zscaler's `Sex` with a numeric
    # id), GDPR demographic consent checkboxes, multi-option checkbox
    # groups rendered as `<input id="question_X[]_Y">` with the label on
    # the parent fieldset's legend, and the standard `id="phone"` when
    # the user's profile didn't pre-fill it. Filling these in the same
    # initial pass — rather than catching them after submit-validation
    # fails — avoids the "field stuck in error state" problem where
    # Greenhouse's per-field aria-invalid flag persists across resubmits.
    already_classified = {f["field_id"] for f in classified_fields}
    extra_ids = _collect_unclassified_required_ids(page, already_classified)
    for field_id in extra_ids:
        selector = _id_selector(field_id)
        if not page.query_selector(selector):
            continue
        label_text = _resolve_label_text(page, field_id) or field_id
        field_type = _detect_field_type(page, selector)
        options = _detect_options(page, selector)
        if field_type == "select" and not options:
            options = _harvest_combobox_options(page, selector)
        is_combobox = field_type in ("select", "radio")
        classified_fields.append({
            "field_id": field_id,
            "label": label_text,
            "field_type": field_type,
            "options": options,
            "required": True,
        })
        print(
            f"[auto_apply.classify] pre-submit (wider): field_id={field_id!r} "
            f"label={label_text!r} field_type={field_type!r}",
            flush=True,
        )
        meta_by_id[field_id] = {
            "selector": selector,
            "label": label_text,
            "field_type": field_type,
            "options": options,
            "required": True,
            "is_combobox": is_combobox,
        }

    if not classified_fields:
        return

    # Collapse "select all that apply" checkbox groups into a single virtual
    # question. Greenhouse renders these as N individual checkboxes that
    # share the parent fieldset's <legend> as their label — without grouping
    # we'd send the LLM 14 copies of "How did you hear about this
    # opportunity?", get a YES/NO style answer back, and try to check ALL 14
    # boxes. Per-checkbox option text comes from each input's own
    # `<label for="...">`, which we pull from the DOM here.
    classified_fields, meta_by_id = _group_checkbox_groups(
        page, classified_fields, meta_by_id,
    )

    # User's drawer answers win over profile/library/LLM. On a resume from
    # the Needs Attention drawer, edited_answers carries the values the
    # user just typed — without honoring them here the worker would re-run
    # the same resolver chain that produced the wrong answers in the first
    # place, fail the same combobox fill, escalate to drawer again, and
    # ask the user the same questions in a loop.
    edited = edited_answers or {}
    unanswered_fields = [
        f for f in classified_fields
        if str(f.get("field_id") or "") not in edited
    ]
    try:
        resolved = (
            auto_answer_form_questions(
                uid=uid,
                profile=profile,
                resume_summary=resume_summary,
                job=job,
                classified_fields=unanswered_fields,
            )
            if unanswered_fields
            else {}
        )
    except Exception as exc:
        logger.exception("auto_answer_form_questions crashed: %s", exc)
        # Degrade safely: everything becomes NEEDS_USER and escalates to
        # the Needs Attention drawer rather than submitting bad data.
        resolved = {
            f["field_id"]: {"answer": None, "source": "needs_user"}
            for f in unanswered_fields
        }

    for field_id, meta in meta_by_id.items():
        user_answer = edited.get(str(field_id))
        if user_answer:
            info = {"answer": user_answer, "source": "drawer"}
        else:
            info = resolved.get(field_id) or {"answer": None, "source": "needs_user"}
        answer = info.get("answer")
        selector = meta["selector"]

        if answer is None or answer == "" or answer == "NEEDS_USER":
            unmapped.append({
                "field_id": field_id,
                "label": meta["label"],
                "reason": f"no answer (source={info.get('source', 'unknown')})",
                "field_type": meta["field_type"],
                "options": meta["options"],
                "required": meta["required"],
            })
            continue

        if meta.get("is_checkbox_group"):
            # Multi-checkbox "select all that apply" group. The answer can
            # be either a single option string (legacy LLM/profile output)
            # or a list of option strings (drawer multi-select). Normalize
            # to a list, then check every member whose option_text matches
            # any element.
            members = meta.get("group_members") or []
            if isinstance(answer, (list, tuple, set)):
                requested = [str(a).strip() for a in answer if str(a).strip()]
            else:
                requested = [str(answer).strip()] if str(answer).strip() else []
            picked: List[Dict[str, Any]] = []
            for req in requested:
                req_lower = req.lower()
                match = None
                # 1. Exact match
                for m in members:
                    if (m["option_text"] or "").strip().lower() == req_lower:
                        match = m
                        break
                # 2. Substring either direction
                if not match:
                    for m in members:
                        opt = (m["option_text"] or "").strip().lower()
                        if opt and (opt in req_lower or req_lower in opt):
                            match = m
                            break
                if match and match not in picked:
                    picked.append(match)
            if picked:
                for m in picked:
                    sel = _id_selector(m["field_id"])
                    _check_checkbox(page, sel, True, filled, unmapped,
                                    m["field_id"], meta["label"])
                print(
                    f"[auto_apply.fill] checkbox-group: picked "
                    f"{[p['option_text'] for p in picked]!r} "
                    f"from {len(members)} group members for answer {answer!r}",
                    flush=True,
                )
            else:
                unmapped.append({
                    "field_id": field_id,
                    "label": meta["label"],
                    "reason": f"answer {answer!r} did not match any group option",
                    "field_type": "select",
                    "options": meta.get("options"),
                    "required": meta["required"],
                })
        elif meta["field_type"] == "checkbox":
            _check_checkbox(page, selector, answer, filled, unmapped, field_id, meta["label"])
        elif meta["is_combobox"]:
            _fill_combobox(page, selector, str(answer), filled, unmapped)
        else:
            _fill_text_if_present(page, selector, str(answer), filled)

        if prepared_answers is not None:
            from app.services.auto_apply import _form_filler_common as _c
            _c.record_prepared_answer(
                prepared_answers,
                field_id=field_id,
                label=meta["label"],
                answer=answer,
                field_type=meta["field_type"],
                source=info.get("source", "unknown"),
            )


def _unmapped_entry(
    page, selector: str, field_id: str, label_text: str, reason: str
) -> Dict[str, Any]:
    """Build an unmapped entry annotated with everything the Needs Attention
    drawer needs to render an input: field_type, options, required."""
    return {
        "field_id": field_id,
        "label": label_text,
        "reason": reason,
        "field_type": _detect_field_type(page, selector),
        "options": _detect_options(page, selector),
        "required": _detect_required(page, selector),
    }


def _detect_field_type(page, selector: str) -> str:
    """Best-effort classification of a Greenhouse form field."""
    try:
        result = page.evaluate(
            """(sel) => {
                const el = document.querySelector(sel);
                if (!el) return 'text';
                const tag = el.tagName?.toLowerCase() || '';
                if (tag === 'textarea') return 'textarea';
                if (tag === 'select') return 'select';
                // Greenhouse react-select renders a hidden input with role=combobox
                // wrapped in a div; if our selector matched that input, classify as select.
                if (el.getAttribute && el.getAttribute('role') === 'combobox') return 'select';
                const type = (el.getAttribute && el.getAttribute('type') || 'text').toLowerCase();
                if (type === 'number') return 'number';
                if (type === 'date') return 'date';
                if (type === 'radio') return 'radio';
                if (type === 'checkbox') return 'checkbox';
                return 'text';
            }""",
            selector,
        )
        return result or "text"
    except Exception:
        return "text"


def _detect_options(page, selector: str) -> Optional[List[str]]:
    """Extract select/radio option labels. Returns None for inputs without
    enumerated options."""
    try:
        result = page.evaluate(
            """(sel) => {
                const el = document.querySelector(sel);
                if (!el) return null;
                const tag = el.tagName?.toLowerCase();
                if (tag === 'select') {
                    return Array.from(el.options || [])
                        .map(o => (o.textContent || '').trim())
                        .filter(Boolean);
                }
                // Greenhouse react-select: the listbox is rendered on demand.
                // Best-effort: read aria-owns / aria-controls and grab options
                // if the dropdown happens to be open. Otherwise return null —
                // the Needs Attention drawer will fall back to a text input.
                const ownsId = el.getAttribute && (el.getAttribute('aria-owns') || el.getAttribute('aria-controls'));
                if (ownsId) {
                    const listbox = document.getElementById(ownsId);
                    if (listbox) {
                        return Array.from(listbox.querySelectorAll('[role="option"]'))
                            .map(o => (o.textContent || '').trim())
                            .filter(Boolean);
                    }
                }
                return null;
            }""",
            selector,
        )
        if isinstance(result, list) and result:
            return [str(x) for x in result]
        return None
    except Exception:
        return None


def _collect_unclassified_required_ids(page, already_classified: set) -> List[str]:
    """Find every required form input the question_X-label scan missed.

    Greenhouse forms vary by tenant — the standard custom-question wrapper
    `<label id="question_X-label" for="question_X">` is reliable when used,
    but tenants who customize their EEO section, GDPR consent flows, or
    multi-select checkbox groups often render fields outside that wrapper
    with their own bespoke ids. We scan the DOM directly:

      - `[aria-required="true"]` and `[required]` form inputs.
      - The standard Greenhouse `#phone` field — required on every form.

    Returns a deduplicated list of element ids (or names) that are NOT
    already in `already_classified` and don't belong to the standard fields
    already handled by `_fill_standard_fields` (first_name/last_name/email).
    """
    try:
        ids = page.evaluate(
            """() => {
                const out = [];
                const seen = new Set();
                // Standard-fields IDs handled directly by _fill_standard_fields.
                // We let those handlers run, then pick up #phone here only if
                // the standard handler didn't end up filling it.
                const handled = new Set([
                    'first_name', 'last_name', 'preferred_name',
                    'preferred_first_name', 'preferred_last_name',
                    'email', 'resume', 'cover_letter',
                    'country', 'candidate-location',
                ]);
                document.querySelectorAll(
                    'input[aria-required="true"], input[required], ' +
                    'select[aria-required="true"], select[required], ' +
                    'textarea[aria-required="true"], textarea[required], ' +
                    'input#phone'
                ).forEach((el) => {
                    const id = el.id || el.name || '';
                    if (!id) return;
                    if (handled.has(id)) return;
                    if (seen.has(id)) return;
                    seen.add(id);
                    out.push(id);
                });
                return out;
            }"""
        )
    except Exception as exc:
        logger.warning("_collect_unclassified_required_ids eval failed: %s", exc)
        return []
    if not isinstance(ids, list):
        return []
    return [str(x) for x in ids if str(x) not in already_classified]


def _resolve_refill_and_resubmit(
    page,
    invalid_fields: List[Dict[str, Any]],
    preview: Dict[str, Any],
    uid: str,
    resume_summary: str,
    job_data: Optional[Dict[str, Any]],
    filled: Dict[str, str],
    unmapped: List,
    current_screenshot_b64: str,
    edited_answers: Optional[Dict[str, str]] = None,
) -> tuple:
    """Second-chance pass over Greenhouse aria-invalid fields.

    The pre-submit classifier only walks `label[id^="question_"][id$="-label"]`
    custom-question markup. Greenhouse-rendered checkbox groups
    (`question_X[]_Y`), the standard EEO `Sex` dropdown when a tenant uses a
    custom numeric id, and GDPR demographic consent boxes don't match that
    selector, so they're never resolved. After submit they all show up as
    aria-invalid — at which point we already know the label and field_type,
    so we run them through `auto_answer_form_questions` (which gives us slot
    match for "Sex", consent fast-path for the agreement boxes, and the
    LLM for everything else), refill the page, and resubmit once.

    Returns `(status, remaining_pending, screenshot_b64)`:
      - `status`: "submitted" if resubmit reached a thank-you / success page,
        else "" (caller decides between needs_attention and submit_failed
        based on whether remaining_pending is non-empty).
      - `remaining_pending`: aria-invalid fields still unresolved after retry.
      - `screenshot_b64`: updated screenshot if we resubmitted, else the
        screenshot the caller passed in.
    """
    from app.services.auto_apply.screening_answers import auto_answer_form_questions

    profile = preview.get("_application_profile") or {}
    edited = edited_answers or {}

    # Only run the resolver on fields the user HASN'T already answered via
    # the drawer. Re-running profile+library+LLM on a field that the user
    # explicitly answered means we'd overwrite their input with the same
    # stale LLM answer and loop the drawer ("asked twice for location" bug
    # from Verkada dogfood).
    unanswered_fields = [
        f for f in invalid_fields
        if not edited.get(str(f.get("field_id") or ""))
    ]
    try:
        resolved = (
            auto_answer_form_questions(
                uid=uid,
                profile=profile,
                resume_summary=resume_summary,
                job=job_data or {},
                classified_fields=unanswered_fields,
            )
            if unanswered_fields
            else {}
        )
    except Exception as exc:
        logger.exception("retry resolver crashed: %s", exc)
        return ("", invalid_fields, current_screenshot_b64)

    filled_any = False
    for field in invalid_fields:
        fid = field.get("field_id")
        if not fid:
            continue
        field_type = field.get("field_type") or "text"
        label = field.get("label") or ""
        # User's drawer answer wins. Falls back to LLM-resolved value only
        # if drawer didn't speak for this field.
        user_answer = edited.get(str(fid))
        if user_answer:
            info = {"answer": user_answer, "source": "drawer"}
        else:
            info = resolved.get(fid) or {}
        answer = info.get("answer")
        if not answer or answer == "NEEDS_USER":
            continue
        sel = _id_selector(str(fid))
        try:
            if field_type == "checkbox":
                _check_checkbox(page, sel, answer, filled, unmapped, str(fid), label)
                # React/Formik fix: page.check() mutates DOM .checked but
                # Greenhouse's Formik validator reads from its own state,
                # which is only updated when React's onChange fires on a
                # synthetic event React itself dispatched. The native setter
                # trick bypasses React's wrapped setter to force a state
                # update on Formik-tracked checkboxes.
                _react_force_checkbox(page, sel, _truthy(answer))
            elif field_type in ("select", "radio"):
                _fill_combobox(page, sel, str(answer), filled, unmapped)
                # Diagnostic: if the combobox didn't take a value, dump the
                # parent chain so we can see what wrapper class the EEO
                # react-select uses.
                _diagnose_unfilled_combobox(page, sel, str(fid))
            else:
                _fill_text_if_present(page, sel, str(answer), filled)
                # Same React/Formik trick for text inputs. page.fill mutates
                # DOM .value and fires an input event, but on phone-mask /
                # formatted-input fields Greenhouse's Formik handler may
                # never see it. Re-fire via the native setter to be sure.
                _react_force_text(page, sel, str(answer))
            filled_any = True
            # Diagnostic: confirm the DOM actually mutated. If `value`/`checked`
            # is still empty/false after the fill, we know the helper silently
            # no-op'd (most often: react-select needs a real listbox-click,
            # checkbox inputs are visually proxied so check() throws, or React
            # isn't picking up the synthetic change event).
            try:
                dom_state = page.evaluate(
                    """(sel) => {
                        const el = document.querySelector(sel);
                        if (!el) return {present: false};
                        return {
                            present: true,
                            tag: (el.tagName || '').toLowerCase(),
                            type: (el.getAttribute && el.getAttribute('type')) || null,
                            value: el.value !== undefined ? String(el.value).slice(0, 60) : null,
                            checked: el.checked === true,
                            aria_invalid: el.getAttribute && el.getAttribute('aria-invalid'),
                            visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length),
                        };
                    }""",
                    sel,
                )
            except Exception:
                dom_state = {"error": "eval-failed"}
            print(
                f"[auto_apply.retry] refilled field_id={fid!r} "
                f"field_type={field_type!r} answer={str(answer)[:60]!r} "
                f"source={info.get('source')!r} dom={dom_state} "
                f"filled_entry={filled.get(sel)!r}",
                flush=True,
            )
        except Exception as exc:
            logger.warning("retry fill failed for %s: %s", fid, exc)

    if not filled_any:
        print(
            f"[auto_apply.retry] nothing to refill — "
            f"{len(invalid_fields)} field(s) escalating to drawer",
            flush=True,
        )
        return ("", invalid_fields, current_screenshot_b64)

    # Resubmit once.
    try:
        submit2 = page.query_selector('button[type="submit"]')
        if not submit2:
            print("[auto_apply.retry] no submit button on retry", flush=True)
            return ("", invalid_fields, current_screenshot_b64)
        submit2.click()
        page.wait_for_load_state("networkidle", timeout=30_000)
        new_screenshot_b64 = base64.b64encode(
            page.screenshot(full_page=True)
        ).decode("ascii")
        # Diagnostic: snapshot the same fields' DOM state after resubmit.
        # If values that we filled before submit are now empty/false, React
        # state was never updated and rendered them away. If values held
        # but aria-invalid is still true, Formik is validating against
        # internal state we never wrote to.
        for field in invalid_fields:
            fid = field.get("field_id")
            if not fid:
                continue
            sel2 = _id_selector(str(fid))
            try:
                dom_state2 = page.evaluate(
                    """(sel) => {
                        const el = document.querySelector(sel);
                        if (!el) return {present: false};
                        return {
                            present: true,
                            value: el.value !== undefined ? String(el.value).slice(0, 60) : null,
                            checked: el.checked === true,
                            aria_invalid: el.getAttribute && el.getAttribute('aria-invalid'),
                        };
                    }""",
                    sel2,
                )
            except Exception:
                dom_state2 = {"error": "eval-failed"}
            print(
                f"[auto_apply.retry] post-resubmit field_id={fid!r} dom={dom_state2}",
                flush=True,
            )
        invalid_count2 = 0
        try:
            invalid_count2 = page.evaluate(
                "() => document.querySelectorAll('[aria-invalid=\"true\"]').length"
            )
        except Exception:
            pass
        url_after2 = (page.url or "").lower()
        success_url2 = any(
            kw in url_after2
            for kw in ("thank", "confirmation", "/success", "complete")
        )
        if success_url2 or invalid_count2 == 0:
            print(
                f"[auto_apply.retry] resubmit succeeded "
                f"(invalid_count2={invalid_count2}, url={url_after2[:80]})",
                flush=True,
            )
            return ("submitted", [], new_screenshot_b64)
        # Some fields still invalid — re-extract and let caller escalate.
        remaining = _extract_invalid_field_questions(page)
        print(
            f"[auto_apply.retry] {invalid_count2} field(s) still invalid after "
            f"retry; {len(remaining)} resolved labels for drawer",
            flush=True,
        )
        return ("", remaining, new_screenshot_b64)
    except Exception as exc:
        logger.warning("retry submit failed: %s", exc)
        return ("", invalid_fields, current_screenshot_b64)


def _extract_invalid_field_questions(page) -> List[Dict[str, Any]]:
    """After Submit, Greenhouse marks rejected required fields with
    aria-invalid="true". For each one, resolve the label, classify the
    input type, and for combobox-style fields, click them open to harvest
    their actual options. Returns a pending_questions[] payload the runner
    persists for the Needs Attention drawer.

    Source of truth for "required" here is the form: if Greenhouse rejected
    it, it was required."""
    try:
        invalid_ids = page.evaluate(
            """() => {
                const seen = new Set();
                const ids = [];
                document.querySelectorAll('[aria-invalid="true"]').forEach((el) => {
                    const id = el.id || el.name || '';
                    if (id && !seen.has(id)) {
                        seen.add(id);
                        ids.push(id);
                    }
                });
                return ids;
            }"""
        )
    except Exception as exc:
        logger.warning("_extract_invalid_field_questions id-scan failed: %s", exc)
        return []

    if not isinstance(invalid_ids, list):
        return []

    results: List[Dict[str, Any]] = []
    for fid in invalid_ids:
        try:
            sel = _id_selector(str(fid))
            if not page.query_selector(sel):
                continue
            label = _resolve_label_text(page, str(fid))
            if not label:
                label = str(fid)
            field_type = _detect_field_type(page, sel)
            options = _detect_options(page, sel)
            # react-select widgets render their listbox on demand. If we saw
            # a select-style field with no options in the DOM, open the
            # widget to harvest them so the drawer can render a real
            # <select> instead of a guess-the-string text input.
            if field_type == "select" and not options:
                options = _harvest_combobox_options(page, sel)
            results.append({
                "field_id": str(fid),
                "label": label,
                "field_type": field_type,
                "options": options,
                "required": True,
            })
            print(
                f"[auto_apply.classify] post-submit aria-invalid: "
                f"field_id={str(fid)!r} label={label!r} field_type={field_type!r}",
                flush=True,
            )
        except Exception as exc:
            logger.warning("_extract_invalid_field_questions per-field failed: %s", exc)
            continue
    return results


def _resolve_label_text(page, field_id: str) -> str:
    """Find the visible question text for a given input id.

    Resolution priority:
      1. The input's `description` attribute (Greenhouse denormalizes the
         parent fieldset's legend onto each input — most reliable signal).
      2. The parent <fieldset>'s <legend>. Covers checkbox-group questions
         like "Zscaler Confidential Information" where the visible label
         is just "I Agree".
      3. <label for=id>. Only used when neither of the above is rich enough.
      4. aria-labelledby.
      5. Ancestor scan as last resort.

    If the resolved label is a generic acknowledgment phrase ("I Agree",
    "Yes", "I Acknowledge") AND a richer ancestor label exists, prefer the
    ancestor — the LLM cannot evaluate consent without the actual subject.
    Strips trailing `*` required markers from the final text."""
    try:
        text = page.evaluate(
            """(args) => {
                const fid = args.fid;
                const el = document.getElementById(fid);
                if (!el) return '';

                const generic = new Set([
                    'i agree', 'i acknowledge', 'i consent', 'i confirm',
                    'agree', 'acknowledge', 'consent', 'yes', 'no', 'ok',
                ]);
                const isGeneric = (s) => generic.has((s || '').toLowerCase().replace(/[*\\s]+/g, ' ').trim());
                const rich = (s) => (s || '').trim().length > 6 && !isGeneric(s);

                // 1. input.description — Greenhouse's denormalized fieldset legend
                const desc = el.getAttribute && el.getAttribute('description');
                if (rich(desc)) return desc.trim();

                // 2. Parent fieldset's legend (the actual question for I-Agree checkboxes)
                const fs = el.closest && el.closest('fieldset');
                if (fs) {
                    const legend = fs.querySelector('legend');
                    if (legend) {
                        const t = (legend.textContent || '').trim();
                        if (rich(t)) return t;
                    }
                }

                // 3. label[for=fid]
                const escaped = (window.CSS && CSS.escape) ? CSS.escape(fid) : fid;
                const direct = document.querySelector(`label[for="${escaped}"]`);
                let labelText = direct ? (direct.textContent || '').trim() : '';

                // 4. If label is generic, look harder for a richer ancestor label
                if (!rich(labelText)) {
                    const labelledBy = el.getAttribute && el.getAttribute('aria-labelledby');
                    if (labelledBy) {
                        const l = document.getElementById(labelledBy);
                        if (l) {
                            const t = (l.textContent || '').trim();
                            if (rich(t)) return t;
                        }
                    }
                    let parent = el.parentElement;
                    for (let i = 0; i < 6 && parent; i++) {
                        // Look for question-description block (Greenhouse pattern)
                        const qd = parent.querySelector && parent.querySelector('.question-description, [class*="description"]');
                        if (qd) {
                            const t = (qd.textContent || '').trim();
                            if (rich(t)) return t.slice(0, 400);
                        }
                        const lbl = parent.querySelector && parent.querySelector('label, .label, legend, [class*="label"]');
                        if (lbl) {
                            const t = (lbl.textContent || '').trim();
                            if (rich(t)) return t;
                        }
                        parent = parent.parentElement;
                    }
                }

                return labelText || '';
            }""",
            {"fid": field_id},
        )
        return (text or "").replace("*", "").strip()
    except Exception:
        return ""


def _harvest_combobox_options(page, selector: str) -> Optional[List[str]]:
    """Open a react-select widget, read its listbox options, close it.
    Returns None if no options surface.

    All open strategies live in `_open_combobox_menu` (shared with the
    fill path). Cost: ~400ms per combobox. Only called on the aria-invalid
    extraction path after a failed submit — never during the normal fill
    loop."""
    if not _open_combobox_menu(page, selector):
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        return None

    try:
        options = page.evaluate(
            """(sel) => {
                const el = document.querySelector(sel);
                if (!el) return null;
                const ownsId = el.getAttribute && (
                    el.getAttribute('aria-owns') || el.getAttribute('aria-controls')
                );
                let listbox = ownsId ? document.getElementById(ownsId) : null;
                if (!listbox) {
                    const all = document.querySelectorAll('[role="listbox"]');
                    if (all.length > 0) listbox = all[all.length - 1];
                }
                if (!listbox) return null;
                return Array.from(listbox.querySelectorAll('[role="option"]'))
                    .map(o => (o.textContent || '').trim())
                    .filter(Boolean);
            }""",
            selector,
        )
    except Exception:
        options = None

    try:
        page.keyboard.press("Escape")
    except Exception:
        pass
    page.wait_for_timeout(100)

    if isinstance(options, list) and options:
        return [str(o) for o in options]
    return None


def _listbox_visible(page, selector: str) -> bool:
    """Return True if a role=listbox is currently rendered on the page."""
    try:
        return bool(page.evaluate(
            """(sel) => {
                const el = document.querySelector(sel);
                if (!el) return false;
                const ownsId = el.getAttribute && (
                    el.getAttribute('aria-owns') || el.getAttribute('aria-controls')
                );
                if (ownsId && document.getElementById(ownsId)) return true;
                return document.querySelectorAll('[role="listbox"]').length > 0;
            }""",
            selector,
        ))
    except Exception:
        return False


def _open_combobox_menu(page, selector: str) -> bool:
    """Open a react-select widget's listbox and return True once visible.

    react-select's open trigger varies across Greenhouse tenants: the
    visible input usually opens on click, but EEO / work-auth selects with
    a hidden input need focus+ArrowDown, and a few tenants
    (Robinhood gender, work-auth Yes/No) only react to a real mousedown on
    the .select__control wrapper — react-select binds its open handler to
    mousedown, not click. Tries each strategy in turn; returns False if
    the listbox is still hidden after all four. Callers should treat the
    False case as "menu never opened" (distinct from "no option matched")
    so the retry/drawer path can surface the right reason."""
    el = page.query_selector(selector)
    if not el:
        return False

    # Strategy 1: click the element itself (works for inputs that ARE the
    # combobox trigger — most custom-question Greenhouse selects).
    try:
        el.click()
        page.wait_for_timeout(150)
        if _listbox_visible(page, selector):
            return True
    except Exception as exc:
        logger.debug("combobox open: el.click failed: %s", exc)

    # Strategy 2: walk up to the visible .select__control / value-container
    # and click that. Covers EEO-style hidden inputs whose parent is the
    # actual trigger.
    try:
        page.evaluate(
            """(sel) => {
                const input = document.querySelector(sel);
                if (!input) return false;
                let node = input.parentElement;
                for (let i = 0; i < 6 && node; i++) {
                    const cls = (node.className || '').toString();
                    const role = node.getAttribute && node.getAttribute('role');
                    if (
                        /control$/.test(cls) ||
                        /select__control/.test(cls) ||
                        /value-container/.test(cls) ||
                        role === 'combobox' ||
                        role === 'button'
                    ) {
                        node.click();
                        return true;
                    }
                    node = node.parentElement;
                }
                return false;
            }""",
            selector,
        )
        page.wait_for_timeout(200)
        if _listbox_visible(page, selector):
            return True
    except Exception as exc:
        logger.debug("combobox open: control-click fallback failed: %s", exc)

    # Strategy 3: focus + ArrowDown — react-select's documented keyboard
    # shortcut. Works on hidden-input selects when click doesn't.
    try:
        el.focus()
        page.keyboard.press("ArrowDown")
        page.wait_for_timeout(300)
        if _listbox_visible(page, selector):
            return True
    except Exception as exc:
        logger.debug("combobox open: focus+ArrowDown failed: %s", exc)

    # Strategy 4: dispatch a real mousedown event on the .select__control
    # ancestor. react-select binds its open handler to mousedown, NOT
    # click; some tenants only open via this path.
    try:
        page.evaluate(
            """(sel) => {
                const input = document.querySelector(sel);
                if (!input) return false;
                let node = input.parentElement;
                for (let i = 0; i < 6 && node; i++) {
                    const cls = (node.className || '').toString();
                    if (/control$/.test(cls) || /select__control/.test(cls)) {
                        const ev = new MouseEvent('mousedown', {
                            bubbles: true, cancelable: true, button: 0,
                        });
                        node.dispatchEvent(ev);
                        return true;
                    }
                    node = node.parentElement;
                }
                return false;
            }""",
            selector,
        )
        page.wait_for_timeout(300)
        if _listbox_visible(page, selector):
            return True
    except Exception as exc:
        logger.debug("combobox open: mousedown dispatch failed: %s", exc)

    return False


def _verify_combobox_commit(
    page, selector: str, expected_text: str
) -> bool:
    """After ArrowDown+Enter+Tab, verify the wrapper form actually accepted
    the commit. Two signals must agree:

      - `aria-invalid` on the input AND on the .select__control wrapper is
        not 'true' (and the wrapper doesn't carry --error).
      - The .select__value-container holds a .select__single-value (chip)
        whose text matches `expected_text` (case-insensitive, accepting
        substring match in either direction for "Los Angeles, CA" vs
        "Los Angeles, California, United States" cases).

    Figma's Greenhouse wrapper sets .select__control--error and
    aria-invalid='true' independently of react-select's internal state.
    react-select fires onChange and renders the chip, but the wrapper's
    validator keeps the field flagged invalid until blur. Without this
    verification we report `[idx=N]` as a successful fill and the runner
    re-queues the field as needs_attention; the drawer re-answer hits the
    same wrapper validator and loops forever (the Figma candidate-location
    bug from the 2026-06-22 v19 dogfood)."""
    try:
        return bool(page.evaluate(
            """(args) => {
                const sel = args.sel;
                const expected = (args.expected || '').toLowerCase().trim();
                const el = document.querySelector(sel);
                if (!el) return false;
                // The input's own aria-invalid is the first signal.
                if (el.getAttribute('aria-invalid') === 'true') return false;
                // Walk up to the .select__control wrapper. If it's still
                // --error or carries aria-invalid='true', the wrapper
                // validator rejected the commit.
                let control = el.parentElement;
                let controlFound = false;
                for (let i = 0; i < 6 && control; i++) {
                    const cls = (control.className || '').toString();
                    if (/select__control/.test(cls) || /control$/.test(cls)) {
                        controlFound = true;
                        if (/--error/.test(cls)) return false;
                        const cAria = control.getAttribute && control.getAttribute('aria-invalid');
                        if (cAria === 'true') return false;
                        break;
                    }
                    control = control.parentElement;
                }
                // The chip is the second signal — react-select renders it
                // inside .select__value-container as .select__single-value.
                let valueContainer = el.parentElement;
                for (let i = 0; i < 6 && valueContainer; i++) {
                    const cls = (valueContainer.className || '').toString();
                    if (/value-container/.test(cls)) {
                        const single = valueContainer.querySelector(
                            '.select__single-value, [class*="singleValue"]'
                        );
                        if (!single) return false;
                        const txt = (single.textContent || '').trim().toLowerCase();
                        if (!txt) return false;
                        if (!expected) return true;
                        return (
                            txt === expected ||
                            txt.includes(expected) ||
                            expected.includes(txt)
                        );
                    }
                    valueContainer = valueContainer.parentElement;
                }
                // No value-container reachable — fall back on aria-invalid
                // only (the input said it's valid; trust that).
                return true;
            }""",
            {"sel": selector, "expected": expected_text},
        ))
    except Exception:
        return False


def _detect_required(page, selector: str) -> bool:
    """Greenhouse marks required questions with aria-required='true' or a
    `required` attribute. Some custom questions also use a star-marked label
    that's only enforced on submit — we treat the explicit aria/required
    attributes as ground truth."""
    try:
        result = page.evaluate(
            """(sel) => {
                const el = document.querySelector(sel);
                if (!el) return false;
                if (el.required) return true;
                const aria = el.getAttribute && el.getAttribute('aria-required');
                if (aria === 'true') return true;
                // Greenhouse sometimes puts aria-required on a wrapping div for
                // react-select; check the nearest .field ancestor.
                let parent = el.parentElement;
                for (let i = 0; i < 4 && parent; i++) {
                    if (parent.getAttribute && parent.getAttribute('aria-required') === 'true') return true;
                    parent = parent.parentElement;
                }
                return false;
            }""",
            selector,
        )
        return bool(result)
    except Exception:
        return False


# ---------- low-level helpers ----------

def _id_selector(field_id: str) -> str:
    """Build a CSS selector that targets an element by id, tolerating ids
    that contain CSS-significant characters like `[`, `]`, `.`, `:`.

    Greenhouse uses array-style ids (`question_X[]`) for multi-select fields;
    `#question_X[]` is invalid CSS. The attribute-equals form `[id="..."]`
    avoids that whole class of escaping problems."""
    safe = field_id.replace("\\", "\\\\").replace('"', '\\"')
    return f'[id="{safe}"]'


def _fill_text_if_present(
    page, selector: str, value: str, filled: Dict[str, str]
) -> None:
    el = page.query_selector(selector)
    if not el or not value:
        return
    try:
        page.fill(selector, str(value))
        filled[selector] = "filled"
    except Exception as exc:
        filled[selector] = f"error: {exc}"


def _check_checkbox(
    page,
    selector: str,
    answer: Any,
    filled: Dict[str, str],
    unmapped: List,
    field_id: str,
    label: str,
) -> None:
    """Check or uncheck a checkbox based on the resolved answer.

    Truthy answers ("I Agree", "Yes", "True", true) → check the box.
    Falsy answers ("No", "False", false) → uncheck (or leave unchecked).

    Uses Playwright's `check()` / `uncheck()` rather than `click()` because
    they're idempotent: calling check() on an already-checked box is a no-op
    instead of toggling it off. Critical for cases where the worker re-runs
    after the user resolves drawer questions — we must not accidentally
    uncheck things we already checked on the prior pass."""
    should_check = _truthy(answer)
    el = page.query_selector(selector)
    if not el:
        return
    try:
        if should_check:
            page.check(selector, timeout=5_000)
            filled[selector] = f"checked:{answer}"
        else:
            page.uncheck(selector, timeout=5_000)
            filled[selector] = f"unchecked:{answer}"
    except Exception as exc:
        # Fall back to a raw click. Some Greenhouse checkbox renderings put
        # a custom icon over the real input so Playwright's check() can't
        # find a visible target — clicking the input by selector usually
        # still works via dispatch.
        try:
            el.click(timeout=3_000)
            filled[selector] = f"clicked:{answer}"
        except Exception as exc2:
            unmapped.append({
                "field_id": field_id,
                "label": label,
                "reason": f"checkbox toggle failed: {exc2}",
                "field_type": "checkbox",
                "options": None,
                "required": True,
            })


def _react_force_text(page, selector: str, value: str) -> None:
    """Force a React-controlled text input to update its parent component's
    state. Uses the native HTMLInputElement.value setter (bypassing React's
    wrapped setter, which would otherwise filter the change as "not from
    React") and dispatches a bubbling `input` event so React's onChange
    listener routes the update to setState / setFieldValue.

    This is the canonical pattern for poking React from outside (Playwright,
    Selenium, browser extensions). page.fill should do this, but on some
    Greenhouse tenants the Formik handler never sees the change — most
    visible on phone-mask inputs where a separate mask handler intercepts
    the value before Formik's onChange runs."""
    try:
        page.evaluate(
            """(args) => {
                const el = document.querySelector(args.sel);
                if (!el) return false;
                const proto = el.tagName === 'TEXTAREA'
                    ? HTMLTextAreaElement.prototype
                    : HTMLInputElement.prototype;
                const setter = Object.getOwnPropertyDescriptor(proto, 'value');
                if (setter && setter.set) {
                    setter.set.call(el, args.value);
                } else {
                    el.value = args.value;
                }
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.dispatchEvent(new Event('blur', { bubbles: true }));
                return true;
            }""",
            {"sel": selector, "value": value},
        )
    except Exception as exc:
        logger.debug("_react_force_text failed for %s: %s", selector, exc)


def _react_force_checkbox(page, selector: str, should_check: bool) -> None:
    """Force a React-controlled checkbox to update its parent component's
    state. Same pattern as _react_force_text but for the checked property.

    The dispatched `click` event is what React's checkbox onChange listens
    for; a `change` event alone is not enough in some React versions. We
    set .checked via the native setter first so that by the time React's
    handler reads el.checked it gets the right value."""
    try:
        page.evaluate(
            """(args) => {
                const el = document.querySelector(args.sel);
                if (!el) return false;
                const setter = Object.getOwnPropertyDescriptor(
                    HTMLInputElement.prototype, 'checked'
                );
                if (setter && setter.set) {
                    setter.set.call(el, args.checked);
                } else {
                    el.checked = args.checked;
                }
                el.dispatchEvent(new Event('click', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
            }""",
            {"sel": selector, "checked": bool(should_check)},
        )
    except Exception as exc:
        logger.debug("_react_force_checkbox failed for %s: %s", selector, exc)


def _diagnose_unfilled_combobox(page, selector: str, field_id: str) -> None:
    """If a combobox still has empty value after _fill_combobox ran, dump
    the input + 4 ancestors' outerHTML so we can see what wrapper class
    the form is using. Only logs when the value is empty — silent on
    success."""
    try:
        info = page.evaluate(
            """(sel) => {
                const el = document.querySelector(sel);
                if (!el) return null;
                if (el.value && el.value.length > 0) return null;
                const chain = [];
                let node = el;
                for (let i = 0; i < 5 && node; i++) {
                    chain.push({
                        i,
                        tag: (node.tagName || '').toLowerCase(),
                        id: node.id || null,
                        class: node.className || null,
                        role: node.getAttribute ? node.getAttribute('role') : null,
                    });
                    node = node.parentElement;
                }
                return {value: el.value, chain};
            }""",
            selector,
        )
        if info:
            print(
                f"[auto_apply.combodiag] field_id={field_id!r} "
                f"value={info.get('value')!r} chain={info.get('chain')}",
                flush=True,
            )
    except Exception:
        pass


def _truthy(value: Any) -> bool:
    """Coerce LLM / library answer to a checkbox state.

    Treats agreement phrasing ('i agree', 'yes', 'true', 'on', 'agreed') as
    True. Treats 'no'/'false'/empty as False. The LLM is instructed to
    return the explicit option label ("I Agree") for agreement checkboxes,
    but we accept booleans and yes/no too — the user's drawer might've
    saved any of these shapes."""
    if value is True:
        return True
    if value is False or value is None:
        return False
    s = str(value).strip().lower()
    if not s:
        return False
    return s in {
        "i agree", "agree", "agreed", "yes", "y", "true", "1", "on",
        "i acknowledge", "acknowledge", "i consent", "consent",
        "i confirm", "confirm", "ok", "checked",
    }


def _fill_combobox(
    page, selector: str, option_text: str, filled: Dict[str, str], unmapped: List
) -> None:
    """Greenhouse uses react-select. Open the listbox, type, navigate to
    the matching option with ArrowDown+Enter so react-select's event chain
    fires correctly.

    All open strategies live in `_open_combobox_menu` (shared with the
    options harvester). If that helper returns False the menu never
    opened — we emit `[menu-never-opened]` rather than typing into a
    closed dropdown, since the scorer would otherwise see zero options
    and emit `[no-match]`, conflating two different failure modes."""
    if not _open_combobox_menu(page, selector):
        unmapped.append({
            "field_id": selector, "label": option_text,
            "reason": "combobox menu did not open",
        })
        filled[selector] = f"combo:{option_text}[menu-never-opened]"
        return
    try:
        # Greenhouse autocomplete-backed selects (Verkada/Warp country,
        # candidate-location) filter the listbox in real time as we type.
        # Strategy: try progressively shorter prefixes of the answer until
        # the filtered list contains an option that genuinely matches.
        # Pick that one. Never click the "first option overall" because the
        # autocomplete-filtered first option is whatever happens to share
        # a stray character with our typed string (the Afghanistan bug
        # from 2026-06-18 dogfood).
        option_clicked = False
        # Tokens we'll try, in priority order:
        #   1. Full answer
        #   2. Answer up to first comma (e.g. "Los Angeles" from "Los Angeles, CA")
        #   3. Answer up to first " +" (strips phone prefix "+1" suffix)
        #   4. First word only
        cleaned = option_text.strip()
        prefix_tail_split = cleaned.split(" +", 1)[0].strip()
        comma_split = cleaned.split(",", 1)[0].strip()
        first_word = cleaned.split()[0] if cleaned.split() else cleaned
        # De-dupe while preserving order
        tries: List[str] = []
        for t in (cleaned, prefix_tail_split, comma_split, first_word):
            if t and t not in tries:
                tries.append(t)

        # Decline-flavored answers: if the answer looks like a refusal/decline
        # ("I don't wish to answer", "Prefer not to say", etc.), Greenhouse
        # tenants render the corresponding option with WILDLY varying exact
        # text — Anthropic uses one phrasing, Robinhood another, Verkada a
        # third. Typing only the literal answer often surfaces zero matching
        # options. Append a small set of canonical decline synonyms so we
        # have multiple shots at filtering the listbox down to the real
        # decline option, which `score` will then exact-match.
        _DECLINE_PROBES = (
            "decline", "prefer not", "don't wish", "do not wish",
            "rather not", "choose not",
        )
        cleaned_lower = cleaned.lower()
        if any(probe in cleaned_lower for probe in (
            "decline", "wish to answer", "wish to disclose", "prefer not",
            "don't want to answer", "rather not", "no answer",
        )):
            for syn in _DECLINE_PROBES:
                if syn not in tries:
                    tries.append(syn)

        for query in tries:
            # Clear the input before each retry (Ctrl-A + Backspace works on
            # react-select hidden inputs across platforms — react-select
            # listens for the synthetic input event).
            try:
                page.keyboard.press("Control+A")
                page.keyboard.press("Backspace")
            except Exception:
                pass
            page.keyboard.type(query, delay=30)
            # Poll up to 2s for matching options
            for _ in range(20):
                page.wait_for_timeout(100)
                try:
                    hit = page.evaluate(
                        """(args) => {
                            const wantedFull = (args.full || '').toLowerCase().trim();
                            const wantedQuery = (args.query || '').toLowerCase().trim();
                            const boxes = document.querySelectorAll('[role="listbox"]');
                            const box = boxes[boxes.length - 1];
                            if (!box) return null;
                            const opts = Array.from(box.querySelectorAll('[role="option"]'));
                            if (opts.length === 0) return null;

                            // Score every option, then pick the highest. Prevents
                            // the "United Arab Emirates" bug where the FIRST
                            // option starting with the prefix wins over the
                            // genuine intended match elsewhere in the list.
                            const wantedFullWords = new Set(
                                wantedFull.split(/[\\s,+]+/).filter(w => w.length > 1)
                            );
                            function score(optionText) {
                                const t = (optionText || '').trim().toLowerCase();
                                if (!t) return 0;
                                if (t === wantedFull) return 1000;
                                if (t === wantedQuery) return 900;
                                if (t.includes(wantedFull)) return 800;
                                // NEW: option contains the typed query (when
                                // the query is substantive, >= 4 chars). Catches
                                // the Robinhood candidate-location case where
                                // option = "Los Angeles, CA, USA" and our typed
                                // query is "Los Angeles" — clear winner that
                                // previously failed the overlap-ratio fallback
                                // (scored ~200, below the 400 threshold).
                                if (wantedQuery.length >= 4 && t.includes(wantedQuery)) return 750;
                                if (wantedFull.length > 3 && wantedFull.includes(t)) return 700;
                                // Word-overlap: how many of the FULL answer's
                                // distinct words appear in the option
                                const tWords = new Set(t.split(/[\\s,+]+/).filter(w => w.length > 1));
                                let overlap = 0;
                                for (const w of wantedFullWords) {
                                    if (tWords.has(w)) overlap++;
                                }
                                if (wantedFullWords.size === 0) return 0;
                                const overlapRatio = overlap / wantedFullWords.size;
                                // Only accept overlap ratio if at least one
                                // matched word is substantive (>= 4 chars). This
                                // blocks "united arab emirates" from winning on
                                // just "united" alone.
                                let hasSubstantiveMatch = false;
                                for (const w of wantedFullWords) {
                                    if (w.length >= 4 && tWords.has(w)) {
                                        hasSubstantiveMatch = true;
                                        break;
                                    }
                                }
                                if (!hasSubstantiveMatch) return 0;
                                return Math.round(500 * overlapRatio);
                            }

                            let bestIdx = -1;
                            let bestScore = 0;
                            for (let i = 0; i < opts.length; i++) {
                                const s = score(opts[i].textContent || '');
                                if (s > bestScore) {
                                    bestIdx = i;
                                    bestScore = s;
                                }
                            }
                            if (bestIdx >= 0 && bestScore >= 400) {
                                opts[bestIdx].scrollIntoView({block: 'nearest'});
                                return {
                                    index: bestIdx,
                                    text: (opts[bestIdx].textContent || '').trim().toLowerCase(),
                                    score: bestScore,
                                };
                            }
                            return null;
                        }""",
                        {"full": option_text, "query": query},
                    )
                except Exception:
                    hit = None
                if hit:
                    # Navigate to the matched option via keyboard so
                    # react-select's event chain fires correctly. Calling
                    # `.click()` from JS only flips the visual highlight —
                    # React state never gets the change and the form
                    # re-renders empty on validation. ArrowDown + Enter is
                    # the documented react-select keyboard pattern.
                    target_idx = int(hit.get("index") or 0)
                    for _ in range(target_idx + 1):
                        page.keyboard.press("ArrowDown")
                    page.wait_for_timeout(50)
                    page.keyboard.press("Enter")
                    # Force the wrapper form's blur handler so
                    # .select__control--error clears and the wrapper's
                    # validator re-runs. Figma's Greenhouse keeps
                    # aria-invalid='true' on the control until blur even
                    # though react-select already fired onChange and
                    # rendered the chip. Tab is the cleanest trigger
                    # because it doubles as natural forward-tab through
                    # the form; Escape would just close the listbox.
                    try:
                        page.keyboard.press("Tab")
                        page.wait_for_timeout(200)
                    except Exception:
                        pass
                    if _verify_combobox_commit(page, selector, hit.get("text") or ""):
                        option_clicked = True
                        filled[selector] = (
                            f"combo:{hit.get('text')}[idx={target_idx}|q={query!r}]"
                        )
                        break
                    # Chip rendered (react-select committed internally) but
                    # the wrapper's validator rejected the field. Distinct
                    # failure mode from [no-match] — re-typing the same
                    # answer (via drawer resubmit) bounces the same way, so
                    # surface a specific sentinel and DO NOT fall through
                    # to the [no-match] catch-all (which would lie about
                    # the failure mode and reset filled[]).
                    unmapped.append({
                        "field_id": selector, "label": option_text,
                        "reason": (
                            "combobox chip rendered but wrapper validator "
                            "still flagged aria-invalid after commit"
                        ),
                    })
                    filled[selector] = (
                        f"combo:{hit.get('text')}"
                        f"[chip-set-but-invalid|idx={target_idx}|q={query!r}]"
                    )
                    option_clicked = True
                    break
            if option_clicked:
                break

        # No prefix surfaced a real match. Leave the field empty — do NOT
        # click the first random option (that's the Afghanistan bug). The
        # validation pass will mark this field aria-invalid and route to
        # the drawer with the actual options exposed.
        if not option_clicked:
            try:
                page.keyboard.press("Escape")  # close the listbox
            except Exception:
                pass
            unmapped.append({
                "field_id": selector, "label": option_text,
                "reason": "no option matched any prefix of the answer",
            })
            filled[selector] = f"combo:{option_text}[no-match]"
    except Exception as exc:
        unmapped.append({"field_id": selector, "label": option_text,
                          "reason": f"combobox failure: {exc}"})


def _failure(reason: str, **extra: Any) -> Dict[str, Any]:
    out: Dict[str, Any] = {"status": "failed", "failure_reason": reason}
    out.update(extra)
    return out


def _candidate_apply_urls(apply_url: str, job_id: str) -> List[str]:
    """Return the URL(s) we should try to load the Greenhouse form from.

    The challenge: many companies (Stripe, Databricks, DoorDash, …)
    configure their Greenhouse boards so that visits to the canonical
    `boards.greenhouse.io/{slug}/jobs/{id}` URL get redirected to their own
    custom careers page (stripe.com/jobs/listing/...). The form is on a
    different URL.

    The two patterns that reliably reach the form regardless of custom-URL
    redirects:

      1. `https://boards.greenhouse.io/embed/job_app?for={slug}&token={ext_id}`
         This is the iframe-embed endpoint — returns ONLY the application
         form HTML. Companies have no way to redirect this; it's the
         endpoint they embed in their own page.

      2. `https://boards.greenhouse.io/{slug}/jobs/{ext_id}/applications/new`
         The explicit form path. Most companies don't redirect this either,
         since redirecting it would break their own custom careers page.

    We try those two first. The canonical `/jobs/{id}` URLs go LAST since
    they're the ones most commonly redirected away.
    """
    candidates: List[str] = []

    parts = (job_id or "").split("_", 2)
    if len(parts) == 3 and parts[0].lower() == "greenhouse":
        slug, ext_id = parts[1], parts[2]
        candidates.append(
            f"https://boards.greenhouse.io/embed/job_app?for={slug}&token={ext_id}"
        )
        candidates.append(
            f"https://boards.greenhouse.io/{slug}/jobs/{ext_id}/applications/new"
        )
        candidates.append(
            f"https://job-boards.greenhouse.io/{slug}/jobs/{ext_id}#app"
        )
        candidates.append(
            f"https://boards.greenhouse.io/{slug}/jobs/{ext_id}"
        )

    if apply_url:
        if "greenhouse.io" in apply_url.lower():
            candidates.insert(0, apply_url)
        else:
            candidates.append(apply_url)

    seen: set[str] = set()
    deduped: List[str] = []
    for url in candidates:
        if url and url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped
