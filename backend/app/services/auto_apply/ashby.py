"""
Ashby form-filler.

Ashby's apply form lives at `https://jobs.ashbyhq.com/{company}/{job-id}`
(the application form is embedded on the job-detail page itself — there
is no separate `/apply` URL). The DOM uses Ashby's own React framework
with stable `_systemfield_*` ids for standard fields:

  Standard text         : input#_systemfield_name (full name)
                          input#_systemfield_email
                          input#_systemfield_phone
                          input#_systemfield_location_search (combobox)
  Resume upload         : input#_systemfield_resume (type=file, hidden,
                          activated via the visible "Attach resume" button)
  Custom questions      : <div class="ashby-application-form-field"> blocks
                          (or `[data-testid="application-form-field"]` on
                          newer renders), each with a question UUID id like
                          `b8c1e2a4-...` and a label resolvable via
                          `<label for=...>`.
  EEO                   : separate section, fields prefixed with `_eeo_`.
                          Usually optional.
  Submit                : button matching text "Submit Application" — Ashby
                          doesn't expose a stable data-testid for the
                          submit button. Detect by text + form attribute.

Ashby does not use reCAPTCHA at all on the candidate side (as of late
2025) — they rely on rate-limiting on their own backend. So the
graceful-failure surface is essentially zero for valid submissions.

Dry-run mode fills everything but does not click Submit.
"""
from __future__ import annotations

import base64
import re
import logging
import os
from typing import Any, Callable, Dict, List, Optional

from app.services.auto_apply import _form_filler_common as common


logger = logging.getLogger(__name__)

_BUILD_TAG = "ashby-v3-needs-verification-route"
print(f"[auto_apply] ashby.py loaded — build={_BUILD_TAG}")


AnswerLookup = Callable[[str, str, Optional[List[str]]], Optional[Any]]


# ---------- public entry point ----------

def run_ashby_filler(
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
    progress_cb: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Drive the Ashby application form. Same return shape as the
    Greenhouse / Lever fillers so the runner dispatch is uniform."""
    candidate_urls = _candidate_apply_urls(apply_url, job_id)
    if not candidate_urls:
        return common.failure("no usable apply_url for ashby")

    from app.services.auto_apply.browserbase_client import (
        BrowserbaseError, create_session, release_session,
    )
    try:
        session_id, ws_url = create_session(stealth=True, solve_captchas=True)
    except BrowserbaseError as exc:
        return common.failure(str(exc))

    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        release_session(session_id)
        return common.failure("playwright not installed; pip install playwright")

    filled: Dict[str, str] = {}
    unmapped: List[Dict[str, str]] = []
    prepared_answers: List[Dict[str, Any]] = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(ws_url, timeout=60_000)
            try:
                context = browser.contexts[0] if browser.contexts else browser.new_context()
                page = context.pages[0] if context.pages else context.new_page()

                landed_on: Optional[str] = None
                attempt_log: List[Dict[str, str]] = []
                for candidate in candidate_urls:
                    try:
                        page.goto(candidate, wait_until="domcontentloaded", timeout=20_000)
                        final_url = page.url
                        if "ashbyhq.com" not in final_url.lower():
                            attempt_log.append({
                                "url": candidate,
                                "result": "redirected off ashby",
                                "final_url": final_url,
                            })
                            continue
                        # Wait for any of the standard Ashby field markers.
                        # The Apply button may need a click first on some
                        # tenants to reveal the form.
                        if not _ensure_form_visible(page):
                            attempt_log.append({
                                "url": candidate,
                                "result": "form not visible after Apply click",
                                "final_url": final_url,
                            })
                            continue
                        landed_on = candidate
                        attempt_log.append({"url": candidate, "result": "ok"})
                        break
                    except PWTimeout:
                        try:
                            final_url = page.url
                        except Exception:
                            final_url = candidate
                        attempt_log.append({
                            "url": candidate,
                            "result": "no Ashby form on page",
                            "final_url": final_url,
                        })
                    except Exception as exc:
                        attempt_log.append({
                            "url": candidate,
                            "result": f"nav error: {exc}",
                        })
                if not landed_on:
                    failure_b64 = None
                    try:
                        failure_b64 = base64.b64encode(
                            page.screenshot(full_page=True)
                        ).decode("ascii")
                    except Exception:
                        pass
                    # A taken-down posting is not a render bug — say which it is.
                    gone = common.posting_looks_gone(page)
                    return common.failure(
                        common.JOB_GONE_REASON if gone
                        else "Ashby form did not render at any candidate URL",
                        attempted_urls=candidate_urls,
                        attempt_log=attempt_log,
                        screenshot_b64=failure_b64,
                        job_gone=gone,
                    )

                _fill_standard_fields(
                    page, preview, filled, unmapped, prepared_answers,
                )
                _upload_resume(page, resume_path, filled, unmapped)
                _fill_custom_questions(
                    page=page,
                    preview=preview,
                    edited_answers=edited_answers,
                    filled=filled,
                    unmapped=unmapped,
                    prepared_answers=prepared_answers,
                    uid=uid,
                    resume_summary=resume_summary,
                    job=job_data or {},
                )

                screenshot_bytes = page.screenshot(full_page=True)
                screenshot_b64 = base64.b64encode(screenshot_bytes).decode("ascii")

                pending = [u for u in unmapped if u.get("required") is True]
                if pending and not dry_run:
                    return {
                        "status": "needs_attention",
                        "filled": filled,
                        "unmapped": unmapped,
                        "pending_questions": pending,
                        "prepared_answers": prepared_answers,
                        "screenshot_b64": screenshot_b64,
                        "failure_reason": None,
                    }

                # Needs-verification escalation: Ashby ships reCAPTCHA v3
                # invisible on every tenant. Even with a perfect fill, the
                # submit attempt risks a score-based rejection (and on a
                # rejected attempt the headless session is now flagged
                # with their backend). Route to verification UX so the
                # user submits from their real browser instead.
                captcha = common.detect_captcha_challenge(page)
                if captcha and not dry_run:
                    return {
                        "status": "needs_verification",
                        "filled": filled,
                        "unmapped": unmapped,
                        "pending_questions": [],
                        "prepared_answers": prepared_answers,
                        "captcha": captcha,
                        "apply_url": page.url or apply_url,
                        "screenshot_b64": screenshot_b64,
                        "failure_reason": None,
                    }

                status = "dry_run_complete"
                failure_reason: Optional[str] = None
                if not dry_run:
                    try:
                        submit = _find_submit_button(page)
                        if not submit:
                            status = "submit_failed"
                            failure_reason = "no submit button found"
                        else:
                            # Stamp the attempt BEFORE the click. RQ requeues a job whose
                            # worker died, so the task re-runs from the top — and without
                            # this the second run happily submits the application AGAIN.
                            # (Discord got two runs on 2026-07-14 when a deploy restarted
                            # the worker mid-apply.) After the click would leave the fatal
                            # window — died between clicking and recording — uncovered.
                            if progress_cb:
                                try:
                                    progress_cb("awaiting_verification")
                                except Exception:
                                    pass
                            submit.click()
                            page.wait_for_load_state("networkidle", timeout=30_000)
                            screenshot_bytes = page.screenshot(full_page=True)
                            screenshot_b64 = base64.b64encode(screenshot_bytes).decode("ascii")

                            invalid_count = 0
                            try:
                                invalid_count = page.evaluate(
                                    "() => document.querySelectorAll('[aria-invalid=\"true\"]').length"
                                )
                            except Exception:
                                pass
                            url_after = (page.url or "").lower()
                            success_url = any(
                                kw in url_after
                                for kw in (
                                    "thank", "/success", "submitted",
                                    "/confirmation", "/application/success",
                                )
                            )
                            success_marker = _detect_success_marker(page)
                            # Ashby keeps the user on the same URL and
                            # replaces the form with a thank-you panel.
                            # Detect by the marker more than the URL.

                            if success_marker or success_url:
                                status = "submitted"
                            elif invalid_count > 0:
                                validation_pending = common.extract_invalid_field_questions(page)
                                if validation_pending:
                                    retry_status, validation_pending, screenshot_b64 = (
                                        common.resolve_refill_and_resubmit(
                                            page=page,
                                            invalid_fields=validation_pending,
                                            preview=preview,
                                            uid=uid,
                                            resume_summary=resume_summary,
                                            job_data=job_data,
                                            filled=filled,
                                            unmapped=unmapped,
                                            current_screenshot_b64=screenshot_b64,
                                            submit_selector=_SUBMIT_SELECTOR_CSS,
                                            success_url_keywords=(
                                                "thank", "/success", "submitted",
                                                "/confirmation",
                                            ),
                                        )
                                    )
                                    if retry_status == "submitted":
                                        status = "submitted"
                                    elif validation_pending:
                                        return {
                                            "status": "needs_attention",
                                            "filled": filled,
                                            "unmapped": unmapped,
                                            "pending_questions": validation_pending,
                                            "screenshot_b64": screenshot_b64,
                                            "failure_reason": None,
                                        }
                                    else:
                                        status = "submit_failed"
                                        failure_reason = (
                                            f"validation errors on {invalid_count} "
                                            "field(s); retry resolver did not "
                                            "complete cleanly"
                                        )
                                else:
                                    status = "submit_failed"
                                    failure_reason = (
                                        f"validation errors on {invalid_count} field(s) "
                                        "— most likely unmapped required questions."
                                    )
                            else:
                                # No invalid markers, no success URL. If the
                                # form is gone, assume success; otherwise
                                # report submit_failed.
                                form_still_present = (
                                    page.query_selector('input#_systemfield_name, input#_systemfield_email')
                                    is not None
                                )
                                if form_still_present:
                                    status = "submit_failed"
                                    failure_reason = (
                                        "form is still on the page after Submit — "
                                        "submission was not accepted"
                                    )
                                else:
                                    status = "submitted"
                    except PWTimeout:
                        status = "submit_failed"
                        failure_reason = "submit network-idle timeout"
                    except Exception as exc:
                        status = "submit_failed"
                        failure_reason = f"submit click failed: {exc}"

                evidence = (
                    common.classify_submit_evidence(page, ("thank", "/success", "submitted", "/confirmation"))
                    if status == "submitted" else {}
                )
                return {
                    "status": status,
                    "filled": filled,
                    "unmapped": unmapped,
                    "pending_questions": [],
                    "prepared_answers": prepared_answers,
                    "screenshot_b64": screenshot_b64,
                    "failure_reason": failure_reason,
                    **evidence,
                }
            finally:
                try:
                    browser.close()
                except Exception:
                    pass
                release_session(session_id)
    except Exception as exc:
        logger.exception("ashby filler crashed")
        release_session(session_id)
        return common.failure(f"{type(exc).__name__}: {exc}", filled=filled, unmapped=unmapped)


# ---------- form gating ----------

def _ensure_form_visible(page) -> bool:
    """Ashby pages sometimes hide the application form behind an Apply
    button. Click it if present, then wait for the form to render.
    Returns True if the form is visible after the (possible) click."""
    # Ashby renders the form CLIENT-SIDE, so right after domcontentloaded the
    # `_systemfield_*` inputs usually aren't in the DOM yet. The old code did a
    # single synchronous query_selector here, saw an un-hydrated page, found no
    # Apply button on the /application URL (the form is inline there, no button),
    # and returned False — so every candidate URL reported "form did not render"
    # even though it was about to. WAIT for the form to hydrate before giving up.
    try:
        page.wait_for_selector(
            'input#_systemfield_name, input#_systemfield_email',
            timeout=12_000,
        )
        return True
    except Exception:
        pass
    # Try clicking an Apply button — could be a `<a>` or `<button>` with
    # the text "Apply" / "Apply for this job".
    apply_button = None
    try:
        apply_button = page.evaluate_handle(
            """() => {
                const candidates = Array.from(document.querySelectorAll(
                    'a, button, [role="button"]'
                ));
                const target = candidates.find((el) => {
                    const t = (el.innerText || el.textContent || '').trim().toLowerCase();
                    return t === 'apply' || t === 'apply for this job' ||
                           t === 'apply now' || t === 'apply for this position';
                });
                return target || null;
            }"""
        )
    except Exception:
        apply_button = None
    if apply_button:
        try:
            element = apply_button.as_element()
            if element:
                element.click()
                page.wait_for_selector(
                    'input#_systemfield_name, input#_systemfield_email',
                    timeout=8_000,
                )
                return True
        except Exception:
            return False
    return False


# ---------- standard fields ----------

def _fill_standard_fields(
    page,
    preview: Dict[str, Any],
    filled: Dict[str, str],
    unmapped: List,
    prepared_answers: List[Dict[str, Any]],
) -> None:
    """Fill Ashby's standard `_systemfield_*` inputs. Like Lever, Ashby
    uses one combined `name` field, not first/last split."""
    fields = preview.get("fields") or {}
    first_name = (fields.get("first_name") or "").strip()
    last_name = (fields.get("last_name") or "").strip()
    full_name = (fields.get("full_name") or f"{first_name} {last_name}").strip()

    plan = [
        ("#_systemfield_name", full_name),
        ("#_systemfield_email", fields.get("email")),
        ("#_systemfield_phone", fields.get("phone")),
        ("#_systemfield_linkedin", fields.get("linkedin_url")),
        ("#_systemfield_github", fields.get("github_url")),
        ("#_systemfield_website", fields.get("portfolio_url")),
    ]
    label_by_selector = {
        "#_systemfield_name": "Full Name",
        "#_systemfield_email": "Email",
        "#_systemfield_phone": "Phone",
        "#_systemfield_linkedin": "LinkedIn URL",
        "#_systemfield_github": "GitHub URL",
        "#_systemfield_website": "Website / Portfolio URL",
    }
    for selector, value in plan:
        if not value:
            continue
        common.fill_text_if_present(page, selector, str(value), filled)
        common.react_force_text(page, selector, str(value))
        common.record_prepared_answer(
            prepared_answers,
            field_id=selector,
            label=label_by_selector.get(selector, selector),
            answer=value,
            field_type="text",
            source="profile",
        )

    # Location is a combobox on Ashby — the input is `_systemfield_location_search`
    # and selection happens via the listbox.
    location = fields.get("location")
    if location and page.query_selector("#_systemfield_location_search"):
        common.fill_combobox(
            page, "#_systemfield_location_search", str(location),
            filled, unmapped,
        )


def _upload_resume(
    page, resume_path: Optional[str], filled: Dict[str, str], unmapped: List
) -> None:
    """Attach the resume to Ashby's hidden file input."""
    if not resume_path:
        return
    if not os.path.exists(resume_path):
        unmapped.append({
            "field_id": "_systemfield_resume",
            "label": "Resume",
            "reason": f"resume file not found at {resume_path}",
            "field_type": "file",
            "required": True,
        })
        return
    try:
        resume_input = (
            page.query_selector("#_systemfield_resume")
            or page.query_selector('input[type="file"][name*="resume"]')
            or page.query_selector('input[type="file"]')
        )
        if resume_input:
            resume_input.set_input_files(resume_path)
            filled["#_systemfield_resume"] = "uploaded"
        else:
            unmapped.append({
                "field_id": "_systemfield_resume",
                "label": "Resume",
                "reason": "no file input found on form",
                "field_type": "file",
                "required": True,
            })
    except Exception as exc:
        logger.warning("Ashby resume upload failed: %s", exc)
        unmapped.append({
            "field_id": "_systemfield_resume",
            "label": "Resume",
            "reason": f"upload error: {exc}",
            "field_type": "file",
            "required": True,
        })


# ---------- custom questions ----------

def _fill_custom_questions(
    page,
    preview: Dict[str, Any],
    edited_answers: Dict[str, str],
    filled: Dict[str, str],
    unmapped: List,
    prepared_answers: List[Dict[str, Any]],
    *,
    uid: str,
    resume_summary: str,
    job: Dict[str, Any],
) -> None:
    """Ashby custom questions live inside `.ashby-application-form-field`
    wrappers. Each wrapper has one logical question and one input/select.
    Field ids are UUIDs assigned by Ashby's backend.

    Pass 1 walks the wrappers, Pass 2 (shared wider classifier) catches
    anything outside the standard wrapper — custom EEO, GDPR consent,
    etc."""
    from app.services.auto_apply.screening_answers import auto_answer_form_questions

    profile = preview.get("_application_profile") or {}
    classified_fields: List[Dict[str, Any]] = []
    meta_by_id: Dict[str, Dict[str, Any]] = {}

    # Pass 1: walk every Ashby form-field wrapper. The semantic anchor
    # confirmed against jobs.ashbyhq.com/futurefitai/.../application
    # (June 2026) is the `[data-field-path]` attribute — it's on every
    # field wrapper including the radio-group fieldset that DOESN'T carry
    # the `_fieldEntry_*` / `ashby-application-form-field-entry` class.
    # We also accept the class as a fallback for tenants on older
    # Ashby renders.
    field_blocks = page.query_selector_all(
        '[data-field-path], '
        '.ashby-application-form-field-entry, '
        '[data-testid="application-form-field"]'
    )
    # Skip wrappers for system fields we already filled — match by the
    # `data-field-path` attribute which carries `_systemfield_name` etc.
    SYSTEM_PREFIX = "_systemfield_"
    for block in field_blocks:
        # Skip system-field wrappers — those are handled by
        # `_fill_standard_fields`. We check the wrapper's data-field-path
        # rather than the first input's id because radio-group wrappers
        # carry the canonical field path while their inputs have synthetic
        # ids like `ENTRY-UUID_FIELD-UUID-labeled-radio-0`.
        try:
            field_path = block.get_attribute("data-field-path") or ""
        except Exception:
            field_path = ""
        if field_path.startswith(SYSTEM_PREFIX):
            continue
        try:
            inputs = block.query_selector_all(
                'input:not([type="hidden"]):not([type="file"]), select, textarea'
            )
        except Exception:
            continue
        if not inputs:
            continue
        label_text = _resolve_block_label(block)
        try:
            first_type = (inputs[0].get_attribute("type") or "").lower()
        except Exception:
            first_type = ""
        if first_type == "checkbox" and len(inputs) > 1:
            for inp in inputs:
                _classify_input(
                    page, inp, label_text,
                    classified_fields, meta_by_id, unmapped,
                )
        else:
            _classify_input(
                page, inputs[0], label_text,
                classified_fields, meta_by_id, unmapped,
            )

    # Pass 2: wider scan.
    already_classified = {f["field_id"] for f in classified_fields}
    extra_ids = common.collect_unclassified_required_ids(page, already_classified)
    for field_id in extra_ids:
        # Skip system fields — those are standard and handled separately.
        if field_id.startswith(SYSTEM_PREFIX):
            continue
        selector = common.id_selector(field_id)
        if not page.query_selector(selector):
            continue
        label_text = common.resolve_label_text(page, field_id) or field_id
        field_type = common.detect_field_type(page, selector)
        options = common.detect_options(page, selector)
        if field_type == "select" and not options:
            options = common.harvest_combobox_options(page, selector)
        is_combobox = field_type in ("select", "radio")
        classified_fields.append({
            "field_id": field_id,
            "label": label_text,
            "field_type": field_type,
            "options": options,
            "required": True,
        })
        print(
            f"[auto_apply.classify] ashby pre-submit (wider): "
            f"field_id={field_id!r} label={label_text!r} "
            f"field_type={field_type!r}",
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

    try:
        resolved = auto_answer_form_questions(
            uid=uid,
            profile=profile,
            resume_summary=resume_summary,
            job=job,
            classified_fields=classified_fields,
        )
    except Exception as exc:
        logger.exception("auto_answer_form_questions crashed: %s", exc)
        resolved = {
            f["field_id"]: {"answer": None, "source": "needs_user"}
            for f in classified_fields
        }

    for field_id, meta in meta_by_id.items():
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

        if meta["field_type"] == "checkbox":
            common.check_checkbox(
                page, selector, answer, filled, unmapped, field_id, meta["label"],
            )
            common.react_force_checkbox(page, selector, common.truthy(answer))
        elif meta["field_type"] == "radio":
            group_name = meta.get("group_name") or ""
            if group_name:
                common.fill_radio_group(
                    page, group_name, str(answer), filled, unmapped,
                )
            else:
                # Ashby usually uses react-select for choice questions, but
                # some custom forms ship native radios — fall through to
                # combobox dispatch as a best-effort.
                common.fill_combobox(page, selector, str(answer), filled, unmapped)
        elif meta["is_combobox"]:
            common.fill_combobox(page, selector, str(answer), filled, unmapped)
        else:
            common.fill_text_if_present(page, selector, str(answer), filled)
            common.react_force_text(page, selector, str(answer))

        common.record_prepared_answer(
            prepared_answers,
            field_id=field_id,
            label=meta["label"],
            answer=answer,
            field_type=meta["field_type"],
            source=info.get("source", "unknown"),
        )


def _classify_input(
    page,
    input_el,
    label_text: str,
    classified_fields: List[Dict[str, Any]],
    meta_by_id: Dict[str, Dict[str, Any]],
    unmapped: List,
) -> None:
    """Classify one Ashby input.

    Most Ashby fields have a stable UUID `id` on the input itself
    (`<input id="3597e936-...">`). Radio-group inputs are the exception:
    each radio carries a synthetic id like
    `ENTRY-UUID_FIELD-UUID-labeled-radio-N` and all radios in a group
    share the same `name`. For radios we use the group's name as the
    field_id so the resolver/dispatch treats the group as one logical
    question, not one per option."""
    try:
        el_id = input_el.get_attribute("id") or ""
        name = input_el.get_attribute("name") or ""
        el_type = (input_el.get_attribute("type") or "").lower()
    except Exception:
        return

    if el_type == "radio" and name:
        # Use the group name as the field_id; selecting any one radio by
        # name returns the group's first member (good enough for type/
        # required detection and is the same shape Lever uses).
        field_id = f"name:{name}"
        safe_name = name.replace("\\", "\\\\").replace('"', '\\"')
        selector = f'[name="{safe_name}"]'
    elif el_id:
        field_id = el_id
        selector = common.id_selector(field_id)
    elif name:
        field_id = f"name:{name}"
        safe_name = name.replace("\\", "\\\\").replace('"', '\\"')
        selector = f'[name="{safe_name}"]'
    else:
        return

    if not page.query_selector(selector):
        return
    field_type = common.detect_field_type(page, selector)
    options = common.detect_options(page, selector)
    required = common.detect_required(page, selector)
    is_combobox = field_type in ("select", "radio")

    # For radio groups, harvest the option labels by walking the group's
    # siblings — `detect_options` only catches react-select-style options.
    if field_type == "radio" and not options and name:
        options = common.harvest_radio_options(page, name)

    classified_fields.append({
        "field_id": field_id,
        "label": label_text,
        "field_type": field_type,
        "options": options,
        "required": required,
    })
    print(
        f"[auto_apply.classify] ashby pre-submit: field_id={field_id!r} "
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
        # For radio dispatch — empty for non-radio fields.
        "group_name": name if field_type == "radio" else "",
    }


def _resolve_block_label(block) -> str:
    """Read the question text from an Ashby form-field wrapper.

    Ashby's current markup uses a child `<label>` or a div with
    `data-testid="field-label"` for the visible question text.
    Falls back to wrapping `<legend>` or the block's first text line."""
    try:
        lbl = block.query_selector(
            '[data-testid="field-label"], label, legend'
        )
        if lbl:
            text = (lbl.inner_text() or "").strip()
            if text:
                return text.replace("*", "").strip()
    except Exception:
        pass
    try:
        text = (block.inner_text() or "").strip().splitlines()[0]
        return text.replace("*", "").strip()
    except Exception:
        return ""


# ---------- submit detection ----------

_SUBMIT_SELECTOR_CSS = (
    '.ashby-application-form-submit-button, '
    '[data-testid="submit-button"], '
    'button[type="submit"]'
)


def _find_submit_button(page):
    """Find Ashby's submit button.

    Ashby tenants don't expose a stable id or class for the submit button —
    they rely on the React form `onSubmit`. We try a data-testid first
    (some newer tenants set it), then `button[type="submit"]`, then fall
    back to a text-match for "Submit Application" / "Submit"."""
    btn = page.query_selector(_SUBMIT_SELECTOR_CSS)
    if btn:
        return btn
    try:
        return page.evaluate_handle(
            """() => {
                const buttons = Array.from(document.querySelectorAll('button'));
                return buttons.find((b) => {
                    const t = (b.innerText || '').trim().toLowerCase();
                    return t === 'submit application' || t === 'submit';
                }) || null;
            }"""
        ).as_element()
    except Exception:
        return None


def _detect_success_marker(page) -> bool:
    """Ashby replaces the form with a thank-you panel inline (no URL
    change). Detect via text-content scan."""
    try:
        return bool(page.evaluate(
            """() => {
                const text = (document.body.innerText || '').toLowerCase();
                return text.includes('application received') ||
                       text.includes('thanks for applying') ||
                       text.includes('thank you for your application') ||
                       text.includes("we've received your application") ||
                       text.includes('your application has been submitted');
            }"""
        ))
    except Exception:
        return False


# ---------- url candidates ----------

def _candidate_apply_urls(apply_url: str, job_id: str) -> List[str]:
    """Return URLs to try for the Ashby application form.

    Ashby's canonical URL is `https://jobs.ashbyhq.com/{company}/{job-id}`
    — the application form is on the same page as the job detail, no
    `/apply` suffix. Some tenants embed the form on their own careers
    page via Ashby's iframe widget; we don't follow those (the embedded
    iframe has its own URL pattern we'd need to extract)."""
    candidates: List[str] = []

    # Resolve (slug, ashby_job_id). Same trap Greenhouse had: job ids in our pool
    # are namespaced by SOURCE (simplify_…, fantasticjobs_…), not by ATS, so the
    # ashby_{slug}_{id} shape below often doesn't match — and then the
    # /application candidate was never built at all. The apply_url carries both
    # values, so read them from there when the id doesn't cooperate.
    slug = ashby_job_id = None
    parts = (job_id or "").split("_", 2)
    if len(parts) == 3 and parts[0].lower() == "ashby":
        slug, ashby_job_id = parts[1], parts[2]
    elif apply_url:
        m = re.search(
            r"jobs\.ashbyhq\.com/([^/?#]+)/([0-9a-fA-F][0-9a-fA-F-]{7,})",
            apply_url,
        )
        if m:
            slug, ashby_job_id = m.group(1), m.group(2)

    # /application is where the form actually IS. Verified 2026-07-12: the bare
    # job page returns ~7KB with no form node, while /application returns ~106KB
    # containing input#_systemfield_name — the very selector the filler waits on.
    # The old code did insert(0, apply_url), which put that formless bare page
    # FIRST, so the filler burned its 12s wait on a page that can never match.
    if slug and ashby_job_id:
        candidates.append(f"https://jobs.ashbyhq.com/{slug}/{ashby_job_id}/application")
        candidates.append(f"https://jobs.ashbyhq.com/{slug}/{ashby_job_id}")

    if apply_url:
        candidates.append(apply_url)

    seen: set[str] = set()
    deduped: List[str] = []
    for url in candidates:
        if url and url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped
