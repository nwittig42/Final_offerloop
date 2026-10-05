"""
Lever form-filler.

Lever's apply form lives at `https://jobs.lever.co/{company}/{posting-id}/apply`
(or the bare job URL — Lever auto-routes to `/apply` when the page is loaded
without it). The DOM is much simpler than Greenhouse:

  Standard text         : input[name="name"], [name="email"], [name="phone"],
                          [name="org"] (current company)
  Links                 : input[name="urls[LinkedIn]"], [name="urls[GitHub]"],
                          [name="urls[Portfolio]"], [name="urls[Other]"]
  Resume upload         : input[type="file"][name="resume"]
  Cover letter          : input[type="file"][name="cover"] OR
                          textarea[name="comments"]
  Custom questions      : `<div class="application-question">` blocks; the
                          input/select/textarea inside has
                          `name="cards[CARD_ID][field0]"` and the label is
                          in a sibling `.application-label` (or wrapping
                          `<label>`).
  Multi-select checkbox : same wrapper, name ends in `[]`, one input per option
  EEO                   : optional, in a separate section. We skip unless
                          required.
  Submit                : button.template-btn-submit OR
                          button[data-qa="btn-submit"] OR
                          button[type="submit"]

Lever does NOT use reCAPTCHA aggressively (unlike Greenhouse), so the
graceful-failure surface is much smaller — most submissions go through on
the first try when the form is filled correctly. The aria-invalid retry
path is included as a safety net for the rare custom-question form that
slips past the initial classifier.

Dry-run mode fills everything but does not click Submit. Returns a full-
page screenshot so the user can verify visually in the modal.
"""
from __future__ import annotations

import base64
import logging
import os
from typing import Any, Callable, Dict, List, Optional

from app.services.auto_apply import _form_filler_common as common


logger = logging.getLogger(__name__)

# Build marker for runtime verification. Prints on import — if you Ctrl+C
# and restart the backend, this line should appear in the log.
_BUILD_TAG = "lever-v3-needs-verification-route"
print(f"[auto_apply] lever.py loaded — build={_BUILD_TAG}")


AnswerLookup = Callable[[str, str, Optional[List[str]]], Optional[Any]]


# ---------- public entry point ----------

def run_lever_filler(
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
    """Drive the Lever application form. Returns a result dict the
    runner persists to the autoApplyJobs doc.

    Mirrors `run_greenhouse_filler`'s signature so the runner dispatch can
    treat all ATSes uniformly."""
    candidate_urls = _candidate_apply_urls(apply_url, job_id)
    if not candidate_urls:
        return common.failure("no usable apply_url for lever")

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

                # Try each candidate URL until one renders the application
                # form. `input[name="name"]` is Lever's universal first
                # standard field — if it's not on the page, we're not on
                # an apply form.
                landed_on: Optional[str] = None
                attempt_log: List[Dict[str, str]] = []
                for candidate in candidate_urls:
                    try:
                        page.goto(candidate, wait_until="domcontentloaded", timeout=20_000)
                        final_url = page.url
                        if "lever.co" not in final_url.lower():
                            attempt_log.append({
                                "url": candidate,
                                "result": "redirected off lever",
                                "final_url": final_url,
                            })
                            continue
                        page.wait_for_selector('input[name="name"]', timeout=12_000)
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
                            "result": "no input[name=name]",
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
                    # The Multiply Labs posting that "failed" on 2026-07-13 was a
                    # plain 404 — the job was gone. Report that, don't blame the form.
                    gone = common.posting_looks_gone(page)
                    return common.failure(
                        common.JOB_GONE_REASON if gone
                        else "Lever form did not render at any candidate URL",
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

                # Needs-attention escalation: any REQUIRED field still
                # unanswered after profile + library + LLM. Don't submit.
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

                # Needs-verification escalation: Lever's apply form ships
                # hCaptcha on every tenant. Even when our fill is perfect
                # and there are no unanswered required fields, the submit
                # click triggers an interactive hCaptcha challenge that
                # would reject a headless Browserless session. Route to
                # the verification UX so the user finishes the application
                # in their own browser (real device, real IP — hCaptcha
                # scores them as human).
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

                            # Success detection: Lever redirects to /apply/thanks
                            # (or shows an inline "Thanks for applying" panel)
                            # when the form goes through. Validation failures
                            # mark fields with aria-invalid="true" and keep us
                            # on the same URL.
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
                                    "thank", "/thanks", "/confirmation",
                                    "/success", "complete",
                                )
                            )
                            success_marker = _detect_success_marker(page)
                            form_still_present = (
                                page.query_selector('input[name="name"]') is not None
                            )

                            if success_url or success_marker:
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
                                            submit_selector=_SUBMIT_SELECTOR,
                                            success_url_keywords=(
                                                "thank", "/thanks", "/confirmation",
                                                "/success", "complete",
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
                            elif form_still_present:
                                status = "submit_failed"
                                failure_reason = (
                                    "form is still on the page after Submit — "
                                    "submission was not accepted"
                                )
                            else:
                                # Form unmounted, no error markers, URL didn't
                                # change obviously — best-effort: assume success.
                                status = "submitted"
                    except PWTimeout:
                        status = "submit_failed"
                        failure_reason = "submit network-idle timeout"
                    except Exception as exc:
                        status = "submit_failed"
                        failure_reason = f"submit click failed: {exc}"

                evidence = (
                    common.classify_submit_evidence(page, ("thank", "/thanks", "/confirmation", "/success", "complete"))
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
        logger.exception("lever filler crashed")
        release_session(session_id)
        return common.failure(f"{type(exc).__name__}: {exc}", filled=filled, unmapped=unmapped)


# ---------- standard fields ----------

def _fill_standard_fields(
    page,
    preview: Dict[str, Any],
    filled: Dict[str, str],
    unmapped: List,
    prepared_answers: List[Dict[str, Any]],
) -> None:
    """Fill Lever's standard fields. Unlike Greenhouse, Lever uses a single
    `name` input for the full name (not split first/last). URL fields use
    bracket-notation names that must be matched via attribute selector."""
    fields = preview.get("fields") or {}
    first_name = (fields.get("first_name") or "").strip()
    last_name = (fields.get("last_name") or "").strip()
    full_name = (fields.get("full_name") or f"{first_name} {last_name}").strip()

    # Field names confirmed against backend/tests/fixtures/auto_apply/New_LEVER.html.
    # Lever's URL slots are tenant-configurable but the four common ones are
    # LinkedIn / GitHub / Personal Site / Twitter. We also include the older
    # Portfolio / Other variants as fallbacks — fill_text_if_present is a
    # no-op when the selector doesn't match, so trying both is harmless.
    portfolio = fields.get("portfolio_url") or fields.get("website_url")
    twitter = fields.get("twitter_url") or fields.get("other_url")
    plan = [
        ('input[name="name"]', full_name),
        ('input[name="email"]', fields.get("email")),
        ('input[name="phone"]', fields.get("phone")),
        ('input[name="org"]', fields.get("current_company")),
        ('input[name="location"]', fields.get("location")),
        ('input[name="urls[LinkedIn]"]', fields.get("linkedin_url")),
        ('input[name="urls[GitHub]"]', fields.get("github_url")),
        ('input[name="urls[Personal Site]"]', portfolio),
        ('input[name="urls[Twitter]"]', twitter),
        # Fallbacks for tenants on older Lever templates.
        ('input[name="urls[Portfolio]"]', portfolio),
        ('input[name="urls[Other]"]', twitter),
    ]
    # Map selector → human-readable label for the prepared_answers record.
    label_by_selector = {
        'input[name="name"]': "Full Name",
        'input[name="email"]': "Email",
        'input[name="phone"]': "Phone",
        'input[name="org"]': "Current Company",
        'input[name="location"]': "Location",
        'input[name="urls[LinkedIn]"]': "LinkedIn URL",
        'input[name="urls[GitHub]"]': "GitHub URL",
        'input[name="urls[Personal Site]"]': "Personal Site URL",
        'input[name="urls[Twitter]"]': "Twitter URL",
        'input[name="urls[Portfolio]"]': "Portfolio URL",
        'input[name="urls[Other]"]': "Other URL",
    }
    for selector, value in plan:
        if not value:
            continue
        common.fill_text_if_present(page, selector, str(value), filled)
        # React/Formik state-sync follow-up — Lever's form is React-based
        # and the standard page.fill sometimes doesn't propagate to state.
        common.react_force_text(page, selector, str(value))
        common.record_prepared_answer(
            prepared_answers,
            field_id=selector,
            label=label_by_selector.get(selector, selector),
            answer=value,
            field_type="text",
            source="profile",
        )


def _upload_resume(
    page, resume_path: Optional[str], filled: Dict[str, str], unmapped: List
) -> None:
    """Attach the user's resume to Lever's file input. No-op if the file
    doesn't exist locally — degrades to needs_attention via the required
    field check rather than crashing."""
    if not resume_path:
        return
    if not os.path.exists(resume_path):
        unmapped.append({
            "field_id": "resume",
            "label": "Resume",
            "reason": f"resume file not found at {resume_path}",
            "field_type": "file",
            "required": True,
        })
        return
    try:
        resume_input = page.query_selector('input[type="file"][name="resume"]')
        if not resume_input:
            resume_input = page.query_selector('input[type="file"]')
        if resume_input:
            resume_input.set_input_files(resume_path)
            filled['input[name="resume"]'] = "uploaded"
        else:
            unmapped.append({
                "field_id": "resume",
                "label": "Resume",
                "reason": "no file input found on form",
                "field_type": "file",
                "required": True,
            })
    except Exception as exc:
        logger.warning("Lever resume upload failed: %s", exc)
        unmapped.append({
            "field_id": "resume",
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
    """Lever custom questions: walked via two passes.

    Pass 1: explicit `.application-question` blocks. Each block contains a
    label (`<label>` or `.application-label`) plus one or more inputs whose
    name follows the `cards[CARD_ID][fieldN]` pattern. We resolve the label
    per block, then classify the input.

    Pass 2: the shared wider classifier picks up anything Pass 1 missed —
    standard `required` / `aria-required` form inputs not inside a
    `.application-question` block (custom EEO, GDPR consent, etc.).

    Both passes feed into `auto_answer_form_questions` for resolution
    (sensitive profile slots, consent fast-path, library, LLM)."""
    from app.services.auto_apply.screening_answers import auto_answer_form_questions

    profile = preview.get("_application_profile") or {}
    classified_fields: List[Dict[str, Any]] = []
    meta_by_id: Dict[str, Dict[str, Any]] = {}

    # Pass 1: walk .application-question blocks. Each block has one logical
    # question, but may contain multiple inputs (radio group, multi-select).
    question_blocks = page.query_selector_all('.application-question, .application-additional-question')
    for block in question_blocks:
        try:
            label_text = _resolve_block_label(block)
            inputs = block.query_selector_all(
                'input:not([type="hidden"]):not([type="file"]), select, textarea'
            )
        except Exception:
            continue
        if not inputs:
            continue
        # For radio / single-input questions, use the first interactive input
        # as the field_id. For checkbox groups (multi-select), enumerate.
        first = inputs[0]
        try:
            first_type = (first.get_attribute("type") or "").lower()
        except Exception:
            first_type = ""
        if first_type == "checkbox" and len(inputs) > 1:
            # Multi-select: one field per option, all share the question label.
            for inp in inputs:
                _classify_input(
                    page, inp, label_text,
                    classified_fields, meta_by_id, unmapped,
                )
        else:
            _classify_input(
                page, first, label_text,
                classified_fields, meta_by_id, unmapped,
            )

    # Pass 2: wider scan for any required input not already classified.
    # Catches custom EEO sections, GDPR consent boxes, and any tenant-
    # specific markup that didn't render with the .application-question
    # wrapper.
    already_classified = {f["field_id"] for f in classified_fields}
    extra_ids = common.collect_unclassified_required_ids(page, already_classified)
    for field_id in extra_ids:
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
            f"[auto_apply.classify] lever pre-submit (wider): "
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
            # Lever multiple-choice questions use native <input type="radio">
            # — find the radio whose label/value matches the answer and click
            # it. The field_id is one radio's id or `name:<group>`; the
            # group name we need for the query lives on every radio in the
            # group.
            group_name = meta.get("group_name") or field_id.replace("name:", "")
            common.fill_radio_group(
                page, group_name, str(answer), filled, unmapped,
            )
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
    """Classify one input element. Lever inputs without `id` are addressable
    by `name`; we synthesize a stable field_id of the form
    `name:<the-name>` and select via attribute. The orchestration treats
    that as opaque, so the resolver works the same way."""
    try:
        el_id = input_el.get_attribute("id") or ""
        name = input_el.get_attribute("name") or ""
    except Exception:
        return
    if el_id:
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
        f"[auto_apply.classify] lever pre-submit: field_id={field_id!r} "
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
        # Captured for the radio fill dispatch: we need the group name
        # (shared `name` attribute) to find sibling radios.
        "group_name": name,
    }


# `_harvest_radio_options` has been promoted to `common.harvest_radio_options`
# so the Ashby filler can share it. See `_form_filler_common.py`.


def _resolve_block_label(block) -> str:
    """Read the question text from a Lever `.application-question` block.

    Priority: explicit `.application-label` div (most reliable on Lever's
    current markup), then wrapping `<label>`, then `<legend>` for fieldset
    groups, then the block's own text content as last resort."""
    try:
        lbl = block.query_selector(
            '.application-label, .application-question-label, legend, label'
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

_SUBMIT_SELECTOR = (
    'button.template-btn-submit, '
    'button[data-qa="btn-submit"], '
    'button[type="submit"]'
)


def _find_submit_button(page):
    """Find Lever's submit button. Lever tenants vary the class but
    universally use `type="submit"` on the apply form."""
    return page.query_selector(_SUBMIT_SELECTOR)


def _detect_success_marker(page) -> bool:
    """Some Lever tenants show an inline 'Thanks for applying' panel
    instead of redirecting to `/apply/thanks`. Detect that case via
    text content scan."""
    try:
        return bool(page.evaluate(
            """() => {
                const text = (document.body.innerText || '').toLowerCase();
                return text.includes('thanks for applying') ||
                       text.includes('application received') ||
                       text.includes('thank you for applying') ||
                       text.includes('we received your application');
            }"""
        ))
    except Exception:
        return False


# ---------- url candidates ----------

def _candidate_apply_urls(apply_url: str, job_id: str) -> List[str]:
    """Return URLs to try when navigating to the Lever apply form.

    Lever's canonical URLs:
      - `https://jobs.lever.co/{company}/{posting-id}` (job detail)
      - `https://jobs.lever.co/{company}/{posting-id}/apply` (form)

    Most companies don't redirect either. We try `/apply` first since it
    skips the job-detail page; the job-detail URL is the fallback when the
    bare apply URL isn't accepted (some companies use a custom careers
    page that redirects to `/apply`)."""
    candidates: List[str] = []

    # Pull slug + posting-id from the job_id when the runner prefixed it
    # (format: `lever_{slug}_{posting-id}`).
    parts = (job_id or "").split("_", 2)
    if len(parts) == 3 and parts[0].lower() == "lever":
        slug, posting_id = parts[1], parts[2]
        candidates.append(f"https://jobs.lever.co/{slug}/{posting_id}/apply")
        candidates.append(f"https://jobs.lever.co/{slug}/{posting_id}")

    if apply_url:
        if "lever.co" in apply_url.lower():
            # /apply is where the FORM is; the bare URL is the job-detail page.
            # The old code did insert(0, with_apply) and THEN insert(0, apply_url),
            # which put the formless detail page first — so we burned the 12s
            # form wait on a page that can never match before even trying /apply.
            # Same ordering bug Ashby and Greenhouse had. Form first.
            base = apply_url.rstrip("/")
            if base.endswith("/apply"):
                candidates.insert(0, base)
            else:
                candidates.insert(0, base)          # detail page: keep as fallback
                candidates.insert(0, base + "/apply")  # ...but try the form first
        else:
            candidates.append(apply_url)

    seen: set[str] = set()
    deduped: List[str] = []
    for url in candidates:
        if url and url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped
