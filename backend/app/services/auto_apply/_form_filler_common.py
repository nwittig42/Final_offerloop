"""
Shared helpers for ATS form-fillers (Greenhouse / Lever / Ashby / future).

This module owns the universal pieces of the auto-apply browser-automation
flow that don't depend on a specific ATS's markup:

  - DOM inspection: detect_field_type, detect_options, detect_required,
    resolve_label_text (4-strategy label walker).
  - DOM mutation: fill_text_if_present, check_checkbox, fill_combobox.
  - React/Formik state sync: react_force_text, react_force_checkbox — the
    native-setter + bubbling-event pattern that updates React-controlled
    inputs' state when page.fill / page.check alone don't reach the
    controller (most visible on Formik-backed Greenhouse forms).
  - Aria-invalid orchestration: extract_invalid_field_questions reads
    rejected required fields after a failed submit; resolve_refill_and_resubmit
    pipes them through `auto_answer_form_questions` (the shared resolver in
    screening_answers), refills, and resubmits once before escalating to
    the Needs Attention drawer.
  - Wider classifier: collect_unclassified_required_ids picks up required
    fields that the ATS-native selector pass missed (Zscaler's custom EEO
    Sex field, GDPR consent checkboxes, multi-select checkbox groups).

Tenant-specific fillers (greenhouse.py, lever.py, ashby.py) call into these
helpers; they only own the standard-field selectors, the custom-question
classifier pass, and the success-URL patterns for their platform.

NOTE: greenhouse.py currently keeps its own duplicate copies of most of
these helpers — extracted here for new fillers (Lever, Ashby) without
touching greenhouse.py to avoid regression risk. A follow-up will migrate
greenhouse.py to use this module.
"""
from __future__ import annotations

import base64
import logging
from typing import Any, Callable, Dict, List, Optional


logger = logging.getLogger(__name__)


# ---------- selector helpers ----------

def id_selector(field_id: str) -> str:
    """Build a CSS selector that targets an element by id, tolerating ids
    that contain CSS-significant characters like `[`, `]`, `.`, `:`.

    Greenhouse uses array-style ids (`question_X[]`) for multi-select
    fields; `#question_X[]` is invalid CSS. The attribute-equals form
    `[id="..."]` avoids that whole class of escaping problems."""
    safe = field_id.replace("\\", "\\\\").replace('"', '\\"')
    return f'[id="{safe}"]'


# ---------- DOM inspection ----------

def detect_field_type(page, selector: str) -> str:
    """Best-effort classification of a form field by querying the live DOM."""
    try:
        result = page.evaluate(
            """(sel) => {
                const el = document.querySelector(sel);
                if (!el) return 'text';
                const tag = el.tagName?.toLowerCase() || '';
                if (tag === 'textarea') return 'textarea';
                if (tag === 'select') return 'select';
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


def detect_options(page, selector: str) -> Optional[List[str]]:
    """Extract select / radio option labels. Returns None for inputs without
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


def detect_required(page, selector: str) -> bool:
    """Required-field detection. Treats explicit `required` and
    `aria-required="true"` (on the input or any ancestor up to 4 levels) as
    ground truth — star-marked labels alone are not enough."""
    try:
        result = page.evaluate(
            """(sel) => {
                const el = document.querySelector(sel);
                if (!el) return false;
                if (el.required) return true;
                const aria = el.getAttribute && el.getAttribute('aria-required');
                if (aria === 'true') return true;
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


def listbox_visible(page, selector: str) -> bool:
    """True if a role=listbox is currently rendered (i.e. the combobox is open)."""
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


def resolve_label_text(page, field_id: str) -> str:
    """Find the visible question text for a given input id.

    Resolution priority:
      1. The input's `description` attribute (Greenhouse denormalizes the
         parent fieldset's legend onto each input — most reliable signal).
      2. The parent <fieldset>'s <legend>. Covers checkbox-group questions
         like "Zscaler Confidential Information" where the visible label
         on the input itself is just "I Agree".
      3. <label for=id>.
      4. aria-labelledby.
      5. Ancestor scan for `.question-description` / `[class*="label"]`.

    Generic acknowledgment phrases ("I Agree", "Yes") are rejected in favor
    of a richer ancestor label — the LLM cannot evaluate consent without
    the actual subject. Trailing `*` required markers are stripped."""
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

                const desc = el.getAttribute && el.getAttribute('description');
                if (rich(desc)) return desc.trim();

                const fs = el.closest && el.closest('fieldset');
                if (fs) {
                    const legend = fs.querySelector('legend');
                    if (legend) {
                        const t = (legend.textContent || '').trim();
                        if (rich(t)) return t;
                    }
                }

                const escaped = (window.CSS && CSS.escape) ? CSS.escape(fid) : fid;
                const direct = document.querySelector(`label[for="${escaped}"]`);
                let labelText = direct ? (direct.textContent || '').trim() : '';

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


def harvest_combobox_options(page, selector: str) -> Optional[List[str]]:
    """Open a react-select widget, read its listbox options, close it.

    The reliable way to open is focus + ArrowDown (react-select's documented
    keyboard shortcut). Falls back to clicking the visible `.select__control`
    ancestor. Returns None if no options surface. ~400ms per call — only use
    on the aria-invalid path, never during the hot fill loop."""
    el = page.query_selector(selector)
    if not el:
        return None

    opened = False
    try:
        el.focus()
        page.keyboard.press("ArrowDown")
        page.wait_for_timeout(300)
        opened = listbox_visible(page, selector)
    except Exception as exc:
        logger.debug("focus+ArrowDown failed: %s", exc)

    if not opened:
        try:
            page.evaluate(
                """(sel) => {
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    let node = el.parentElement;
                    for (let i = 0; i < 6 && node; i++) {
                        const cls = (node.className || '').toString();
                        const role = node.getAttribute && node.getAttribute('role');
                        if (
                            /control$/.test(cls) ||
                            /select__control/.test(cls) ||
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
            page.wait_for_timeout(300)
            opened = listbox_visible(page, selector)
        except Exception as exc:
            logger.debug("control-click fallback failed: %s", exc)

    if not opened:
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


# ---------- DOM mutation primitives ----------

def fill_text_if_present(
    page, selector: str, value: str, filled: Dict[str, str]
) -> None:
    """Type into a text/textarea input if it exists. No-op when value is
    empty or the input isn't on the page. Records the outcome in `filled`."""
    el = page.query_selector(selector)
    if not el or not value:
        return
    try:
        page.fill(selector, str(value))
        filled[selector] = "filled"
    except Exception as exc:
        filled[selector] = f"error: {exc}"


def check_checkbox(
    page,
    selector: str,
    answer: Any,
    filled: Dict[str, str],
    unmapped: List,
    field_id: str,
    label: str,
) -> None:
    """Idempotently check or uncheck based on the resolved answer.

    Uses Playwright's `check()` / `uncheck()` (no-ops when already in the
    target state) so re-runs after drawer resolution don't accidentally
    toggle previously-set boxes. Falls back to a raw click when the
    checkbox is visually proxied and Playwright refuses to interact."""
    should_check = truthy(answer)
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
    except Exception:
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


def truthy(value: Any) -> bool:
    """Coerce LLM/library answer to a checkbox boolean. Treats agreement
    phrasing ("I Agree", "Yes", "True") as True; "No"/"False"/empty as
    False."""
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


def fill_radio_group(
    page,
    group_name: str,
    option_text: str,
    filled: Dict[str, str],
    unmapped: List,
) -> None:
    """Click the native `<input type="radio">` in a group whose label or
    value matches `option_text`.

    Use this for native HTML radio groups (Lever uses these for
    multiple-choice questions). Do NOT use for react-select widgets —
    those go through `fill_combobox`.

    Matching priority: exact label-text match → exact value match →
    case-insensitive substring match on the label. Falls back to clicking
    the first radio when nothing matches, on the theory that an unanswered
    radio is worse than the form's first option (which often happens to be
    the safe default — "No" / "Decline" / etc).
    """
    if not group_name or not option_text:
        return
    safe_name = group_name.replace("\\", "\\\\").replace('"', '\\"')
    selector = f'input[type="radio"][name="{safe_name}"]'
    try:
        target_id = page.evaluate(
            """(args) => {
                const radios = Array.from(document.querySelectorAll(args.sel));
                if (radios.length === 0) return null;
                const wanted = (args.text || '').toString().trim().toLowerCase();
                if (!wanted) return null;

                // Helper: read the label text associated with a radio.
                const labelOf = (r) => {
                    // 1. <label for=id>
                    if (r.id) {
                        const escaped = (window.CSS && CSS.escape)
                            ? CSS.escape(r.id) : r.id;
                        const lbl = document.querySelector(`label[for="${escaped}"]`);
                        if (lbl) return (lbl.textContent || '').trim();
                    }
                    // 2. closest <label> wrapping the input
                    const wrap = r.closest && r.closest('label');
                    if (wrap) return (wrap.textContent || '').trim();
                    // 3. immediate next/previous sibling text
                    const sib = r.nextElementSibling || r.previousElementSibling;
                    if (sib) return (sib.textContent || '').trim();
                    return '';
                };

                // Pass 1: exact label match (case-insensitive)
                for (const r of radios) {
                    if (labelOf(r).toLowerCase() === wanted) return r.id || r.value || '';
                }
                // Pass 2: exact value match
                for (const r of radios) {
                    if ((r.value || '').toString().toLowerCase() === wanted) {
                        return r.id || r.value || '';
                    }
                }
                // Pass 3: substring match on label
                for (const r of radios) {
                    if (labelOf(r).toLowerCase().includes(wanted)) {
                        return r.id || r.value || '';
                    }
                }
                // Pass 4: substring match on value
                for (const r of radios) {
                    if ((r.value || '').toString().toLowerCase().includes(wanted)) {
                        return r.id || r.value || '';
                    }
                }
                return null;
            }""",
            {"sel": selector, "text": option_text},
        )
    except Exception as exc:
        unmapped.append({
            "field_id": group_name,
            "label": option_text,
            "reason": f"radio group eval failed: {exc}",
        })
        return

    if not target_id:
        unmapped.append({
            "field_id": group_name,
            "label": option_text,
            "reason": f"no radio matching {option_text!r} in group {group_name!r}",
        })
        return

    # Click via JS to get bubbling click event React's onChange listens for.
    # Playwright's page.click on a hidden radio (Lever often visually
    # styles them as buttons with the actual <input> hidden) will refuse;
    # JS click works regardless of visibility.
    try:
        page.evaluate(
            """(args) => {
                let el = document.getElementById(args.id);
                if (!el) {
                    // Fall back to a value-based lookup.
                    const radios = Array.from(document.querySelectorAll(
                        `input[type="radio"][name="${args.name}"]`
                    ));
                    el = radios.find(r => r.value === args.id);
                }
                if (!el) return false;
                el.click();
                el.dispatchEvent(new Event('change', { bubbles: true }));
                return true;
            }""",
            {"id": str(target_id), "name": group_name},
        )
        filled[selector] = f"radio:{option_text}"
    except Exception as exc:
        unmapped.append({
            "field_id": group_name,
            "label": option_text,
            "reason": f"radio click failed: {exc}",
        })


def harvest_radio_options(page, name: str) -> Optional[List[str]]:
    """Read the visible label text for every radio sharing a `name` attribute.

    Used at classify time so the LLM resolver sees the actual choice
    labels (e.g. "NYC metro area" / "Toronto metro area" / "Other") and
    can return one of them by name. Returns None when no labels resolve.

    Lever uses native radios for multi-choice questions; Ashby uses them
    for `ValueSelect` field types whose UI renders as radio buttons.
    Both flow through this helper."""
    if not name:
        return None
    safe_name = name.replace("\\", "\\\\").replace('"', '\\"')
    try:
        result = page.evaluate(
            """(sel) => {
                const radios = Array.from(document.querySelectorAll(sel));
                const out = [];
                for (const r of radios) {
                    let text = '';
                    if (r.id) {
                        const escaped = (window.CSS && CSS.escape)
                            ? CSS.escape(r.id) : r.id;
                        const lbl = document.querySelector(`label[for="${escaped}"]`);
                        if (lbl) text = (lbl.textContent || '').trim();
                    }
                    if (!text) {
                        const wrap = r.closest && r.closest('label');
                        if (wrap) text = (wrap.textContent || '').trim();
                    }
                    if (!text) {
                        // Ashby wraps each radio in `<span><input/></span><label/>`
                        // — the label is the next sibling of the span.
                        const span = r.closest && r.closest('span');
                        const next = span && span.nextElementSibling;
                        if (next && (next.tagName || '').toLowerCase() === 'label') {
                            text = (next.textContent || '').trim();
                        }
                    }
                    if (!text) text = (r.value || '').toString();
                    if (text) out.push(text);
                }
                return out;
            }""",
            f'input[type="radio"][name="{safe_name}"]',
        )
        if isinstance(result, list) and result:
            return [str(x) for x in result]
        return None
    except Exception:
        return None


def fill_combobox(
    page, selector: str, option_text: str, filled: Dict[str, str], unmapped: List
) -> None:
    """Open a combobox (react-select), type the option, Enter to select.

    Clicks the input first; if no listbox opens (the hidden input often
    doesn't trigger the open handler), walks up to the visible
    `.select__control` wrapper and clicks that. Then types and presses
    Enter to commit. Works for standard react-select and most react-select
    forks (`.react-select__control`, generated `css-*-control` class
    names)."""
    el = page.query_selector(selector)
    if not el:
        return
    try:
        el.click()
        page.wait_for_timeout(150)
        if not listbox_visible(page, selector):
            try:
                page.evaluate(
                    """(sel) => {
                        const input = document.querySelector(sel);
                        if (!input) return false;
                        let node = input.parentElement;
                        for (let i = 0; i < 6 && node; i++) {
                            const cls = (node.className || '').toString();
                            if (
                                /select__control/.test(cls) ||
                                /react-select__control/.test(cls) ||
                                /select__value-container/.test(cls) ||
                                /-control$/.test(cls)
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
            except Exception:
                pass
        page.keyboard.type(option_text, delay=20)
        page.wait_for_timeout(200)
        page.keyboard.press("Enter")
        filled[selector] = f"combo:{option_text}"
    except Exception as exc:
        unmapped.append({"field_id": selector, "label": option_text,
                          "reason": f"combobox failure: {exc}"})


# ---------- React/Formik state sync ----------

def react_force_text(page, selector: str, value: str) -> None:
    """Force a React-controlled text input to update its parent component's
    state. Uses the native HTMLInputElement.value setter (bypassing React's
    wrapped setter) and dispatches bubbling input/change/blur events so
    React's onChange listener routes the update to setState/setFieldValue.

    Canonical pattern for poking React from outside (Playwright, browser
    extensions). page.fill should do this on its own, but on phone-mask
    inputs and Formik-tracked fields the controller sometimes doesn't see
    the change unless we manually fire."""
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
        logger.debug("react_force_text failed for %s: %s", selector, exc)


def react_force_checkbox(page, selector: str, should_check: bool) -> None:
    """Force a React-controlled checkbox to update parent state via the
    native HTMLInputElement.checked setter + bubbling click/change events.

    The click event matters — React's checkbox onChange listens for it;
    a change event alone isn't enough in some React versions."""
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
        logger.debug("react_force_checkbox failed for %s: %s", selector, exc)


# ---------- CAPTCHA detection ----------

def detect_captcha_challenge(page) -> Optional[Dict[str, Any]]:
    """Return a description of the bot-defense widget present on the page,
    or None when no widget is detected.

    Used after a failed submit attempt to distinguish "form had bad data"
    (aria-invalid path → drawer) from "form rejected because of CAPTCHA
    score" (this path → needs_verification → user finishes in browser).
    All three of the ATSes Offerloop ships against deploy CAPTCHA on the
    candidate-facing apply form:

      - Greenhouse: invisible reCAPTCHA + per-tenant email-verification gate
      - Lever: hCaptcha (visible challenge on score failure)
      - Ashby: reCAPTCHA v3 invisible

    Return shape:
        {"vendor": "recaptcha" | "hcaptcha", "sitekey": "...",
         "marker": "what we matched in the DOM"}

    We detect by widget presence — not by "the challenge actually fired
    in this submit attempt". On Greenhouse/Lever/Ashby the widget is
    ALWAYS on the page when the form is rendered, so finding it means
    "any submit attempt risks score-based blocking; route to verification
    flow rather than burning a Browserless session retrying"."""
    try:
        result = page.evaluate(
            """() => {
                // hCaptcha — Lever's vendor
                const hcap = document.querySelector(
                    '.h-captcha, [data-sitekey][data-hcaptcha-widget-id], ' +
                    'iframe[src*="hcaptcha.com"], iframe[src*="newassets.hcaptcha"]'
                );
                if (hcap) {
                    const sitekey = hcap.getAttribute && (
                        hcap.getAttribute('data-sitekey') ||
                        hcap.querySelector && hcap.querySelector('[data-sitekey]')
                    );
                    return {vendor: 'hcaptcha', sitekey: sitekey || null,
                            marker: 'h-captcha widget'};
                }
                // reCAPTCHA — Greenhouse + Ashby's vendor
                const recap = document.querySelector(
                    '.g-recaptcha, .grecaptcha-badge, ' +
                    'iframe[src*="recaptcha"], iframe[src*="recaptcha.net"], ' +
                    'script[src*="recaptcha/api.js"]'
                );
                if (recap) {
                    let sitekey = recap.getAttribute && recap.getAttribute('data-sitekey');
                    if (!sitekey) {
                        // Sitekey often only appears in the script src as a
                        // query param `render=<sitekey>` (v3 invisible).
                        const scripts = document.querySelectorAll(
                            'script[src*="recaptcha/api.js"]'
                        );
                        for (const s of scripts) {
                            const m = (s.src || '').match(/[?&]render=([^&]+)/);
                            if (m) { sitekey = m[1]; break; }
                        }
                    }
                    return {vendor: 'recaptcha', sitekey: sitekey || null,
                            marker: 'g-recaptcha widget'};
                }
                // Cloudflare Turnstile — not seen on Greenhouse/Lever/Ashby
                // today but trivially cheap to also catch.
                const turnstile = document.querySelector(
                    '.cf-turnstile, iframe[src*="challenges.cloudflare.com"]'
                );
                if (turnstile) {
                    return {vendor: 'turnstile',
                            sitekey: turnstile.getAttribute && turnstile.getAttribute('data-sitekey'),
                            marker: 'cf-turnstile widget'};
                }
                return null;
            }"""
        )
    except Exception as exc:
        logger.warning("detect_captcha_challenge eval failed: %s", exc)
        return None
    if isinstance(result, dict) and result.get("vendor"):
        return result
    return None


# ---------- post-submit aria-invalid orchestration ----------

def collect_unclassified_required_ids(page, already_classified: set) -> List[str]:
    """Find every required form input the ATS-native selector pass missed.

    Used as Pass 2 of the pre-submit classifier: scans `aria-required="true"`
    and `[required]` inputs/selects/textareas plus any other ATS-specific
    standard fields the caller wants always included. Filters out ids
    already classified or already handled by `fill_standard_fields`."""
    try:
        ids = page.evaluate(
            """() => {
                const out = [];
                const seen = new Set();
                const handled = new Set([
                    'first_name', 'last_name', 'preferred_name',
                    'preferred_first_name', 'preferred_last_name',
                    'name', 'full_name',
                    'email', 'resume', 'cover_letter', 'cover',
                    'country', 'candidate-location', 'location',
                ]);
                document.querySelectorAll(
                    'input[aria-required="true"], input[required], ' +
                    'select[aria-required="true"], select[required], ' +
                    'textarea[aria-required="true"], textarea[required]'
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
        logger.warning("collect_unclassified_required_ids eval failed: %s", exc)
        return []
    if not isinstance(ids, list):
        return []
    return [str(x) for x in ids if str(x) not in already_classified]


def extract_invalid_field_questions(page) -> List[Dict[str, Any]]:
    """After a failed submit, every `[aria-invalid="true"]` input is a
    required field the ATS rejected. For each: resolve label via
    resolve_label_text, classify type, harvest combobox options if needed.
    Returns the pending_questions payload the runner persists for the
    Needs Attention drawer."""
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
        logger.warning("extract_invalid_field_questions id-scan failed: %s", exc)
        return []

    if not isinstance(invalid_ids, list):
        return []

    results: List[Dict[str, Any]] = []
    for fid in invalid_ids:
        try:
            sel = id_selector(str(fid))
            if not page.query_selector(sel):
                continue
            label = resolve_label_text(page, str(fid))
            if not label:
                label = str(fid)
            field_type = detect_field_type(page, sel)
            options = detect_options(page, sel)
            if field_type == "select" and not options:
                options = harvest_combobox_options(page, sel)
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
            logger.warning("extract_invalid_field_questions per-field failed: %s", exc)
            continue
    return results


def resolve_refill_and_resubmit(
    page,
    invalid_fields: List[Dict[str, Any]],
    preview: Dict[str, Any],
    uid: str,
    resume_summary: str,
    job_data: Optional[Dict[str, Any]],
    filled: Dict[str, str],
    unmapped: List,
    current_screenshot_b64: str,
    submit_selector: str = 'button[type="submit"]',
    success_url_keywords: tuple = ("thank", "confirmation", "/success", "complete"),
) -> tuple:
    """Second-chance pass over aria-invalid fields after a failed submit.

    Pipes the rejected field list through `auto_answer_form_questions` (slot
    match, consent fast-path, library, LLM), refills resolved fields with
    React-friendly dispatch, and resubmits once. Returns
    `(status, remaining_pending, screenshot_b64)`:

      - status: "submitted" if the resubmit reached a thank-you / success
        page, else "" (caller decides between needs_attention and
        submit_failed based on whether remaining_pending is non-empty).
      - remaining_pending: aria-invalid fields still unresolved after retry.
      - screenshot_b64: new screenshot if we resubmitted, else the screenshot
        the caller passed in.

    `submit_selector` and `success_url_keywords` are ATS-specific knobs."""
    from app.services.auto_apply.screening_answers import auto_answer_form_questions

    profile = preview.get("_application_profile") or {}
    try:
        resolved = auto_answer_form_questions(
            uid=uid,
            profile=profile,
            resume_summary=resume_summary,
            job=job_data or {},
            classified_fields=invalid_fields,
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
        info = resolved.get(fid) or {}
        answer = info.get("answer")
        if not answer or answer == "NEEDS_USER":
            continue
        sel = id_selector(str(fid))
        try:
            if field_type == "checkbox":
                check_checkbox(page, sel, answer, filled, unmapped, str(fid), label)
                react_force_checkbox(page, sel, truthy(answer))
            elif field_type in ("select", "radio"):
                fill_combobox(page, sel, str(answer), filled, unmapped)
            else:
                fill_text_if_present(page, sel, str(answer), filled)
                react_force_text(page, sel, str(answer))
            filled_any = True
            print(
                f"[auto_apply.retry] refilled field_id={fid!r} "
                f"field_type={field_type!r} answer={str(answer)[:60]!r} "
                f"source={info.get('source')!r}",
                flush=True,
            )
        except Exception as exc:
            logger.warning("retry fill failed for %s: %s", fid, exc)

    if not filled_any:
        return ("", invalid_fields, current_screenshot_b64)

    try:
        submit2 = page.query_selector(submit_selector)
        if not submit2:
            return ("", invalid_fields, current_screenshot_b64)
        submit2.click()
        page.wait_for_load_state("networkidle", timeout=30_000)
        new_screenshot_b64 = base64.b64encode(
            page.screenshot(full_page=True)
        ).decode("ascii")
        invalid_count2 = 0
        try:
            invalid_count2 = page.evaluate(
                "() => document.querySelectorAll('[aria-invalid=\"true\"]').length"
            )
        except Exception:
            pass
        url_after2 = (page.url or "").lower()
        success_url2 = any(kw in url_after2 for kw in success_url_keywords)
        if success_url2 or invalid_count2 == 0:
            print(
                f"[auto_apply.retry] resubmit succeeded "
                f"(invalid_count2={invalid_count2}, url={url_after2[:80]})",
                flush=True,
            )
            return ("submitted", [], new_screenshot_b64)
        remaining = extract_invalid_field_questions(page)
        print(
            f"[auto_apply.retry] {invalid_count2} field(s) still invalid after "
            f"retry; {len(remaining)} resolved labels for drawer",
            flush=True,
        )
        return ("", remaining, new_screenshot_b64)
    except Exception as exc:
        logger.warning("retry submit failed: %s", exc)
        return ("", invalid_fields, current_screenshot_b64)


# ---------- classifier dispatch ----------

ResolveFn = Callable[[str, Dict[str, str], List, str, str], None]


def record_prepared_answer(
    prepared_answers: List[Dict[str, Any]],
    field_id: str,
    label: str,
    answer: Any,
    field_type: str,
    source: str,
) -> None:
    """Append one resolved answer to the prepared_answers list — the data
    the frontend shows when surfacing a job to "finish in browser" so the
    user knows what we'd fill and can copy-paste.

    Skip when there's no answer (NEEDS_USER / None / empty)."""
    if not answer or answer == "NEEDS_USER":
        return
    prepared_answers.append({
        "field_id": field_id,
        "label": label or field_id,
        "answer": str(answer)[:2000],
        "field_type": field_type,
        "source": source,
    })


def dispatch_fill(
    page,
    field_id: str,
    field_type: str,
    answer: Any,
    label: str,
    filled: Dict[str, str],
    unmapped: List,
) -> None:
    """Dispatch a resolved answer to the appropriate fill primitive by
    field_type, and follow up with the React/Formik state-sync poke so
    Formik-backed forms (Greenhouse, sometimes Lever) actually pick up
    the change. Used by the pre-submit fill loop in tenant-specific
    fillers."""
    sel = id_selector(field_id)
    try:
        if field_type == "checkbox":
            check_checkbox(page, sel, answer, filled, unmapped, field_id, label)
            react_force_checkbox(page, sel, truthy(answer))
        elif field_type in ("select", "radio"):
            fill_combobox(page, sel, str(answer), filled, unmapped)
        else:
            fill_text_if_present(page, sel, str(answer), filled)
            react_force_text(page, sel, str(answer))
    except Exception as exc:
        unmapped.append({
            "field_id": field_id,
            "label": label,
            "reason": f"dispatch fill failed: {exc}",
            "field_type": field_type,
            "options": None,
            "required": True,
        })


def failure(reason: str, **extra: Any) -> Dict[str, Any]:
    """Shorthand for a uniform `status=failed` return shape."""
    out: Dict[str, Any] = {"status": "failed", "failure_reason": reason}
    out.update(extra)
    return out


# A posting that has been taken down is NOT a filler bug — but we were reporting
# it as "<ATS> form did not render at any candidate URL", which reads like our
# code is broken and tells the user nothing actionable. In practice a large share
# of auto-apply "failures" are simply dead jobs: the scraped pool goes stale, and
# the pool's own expires_at is not trustworthy (2026-07-13: only 3 of 7,980 jobs
# were flagged expired, yet several we tried 404'd). Say so plainly instead.
_GONE_MARKERS = (
    "no longer accepting",
    "no longer available",
    "no longer accepting applications",
    "position is closed",
    "job is closed",
    "posting is closed",
    "this job is no longer",
    "not found",
    "page not found",
    "404",
    "job posting has expired",
    "has been filled",
)


def posting_looks_gone(page) -> bool:
    """True when the loaded page says the job is taken down / doesn't exist.

    Deliberately conservative: only consulted AFTER every candidate URL failed
    to render a form, so a false positive can't cost us a real application — the
    application was already not happening. The value is an honest reason.
    """
    # URL evidence FIRST. An ATS that bounces you off a posting has already told
    # you it's dead — and it does so in the URL, before any body text would.
    #
    # Greenhouse redirects a dead posting to the board root with ?error=true
    # (e.g. job-boards.greenhouse.io/msfcareers?error=true). The page you land on
    # is a perfectly normal job LIST, so not one of the text markers below fires,
    # and we reported "Greenhouse form did not render at any candidate URL" —
    # blaming ourselves for a job that simply no longer exists. That reads as a
    # broken product, and it sent the user to a listing that isn't there.
    try:
        url = (page.url or "").lower()
    except Exception:
        url = ""
    if "error=true" in url:
        return True

    try:
        text = (page.evaluate("() => (document.body.innerText || '')") or "").lower()
    except Exception:
        return False
    if not text:
        return False
    head = text[:4000]
    return any(m in head for m in _GONE_MARKERS)


JOB_GONE_REASON = (
    "This job is no longer accepting applications — the posting was taken down. "
    "Nothing was submitted."
)


# ---------- post-submit evidence ----------

EVIDENCE_CONFIRMATION_URL = "confirmation_url"
EVIDENCE_CONFIRMATION_TEXT = "confirmation_text"
EVIDENCE_UNCONFIRMED = "unconfirmed"

_SUCCESS_TEXT_MARKERS = (
    "thanks for applying",
    "thank you for applying",
    "thank you for your application",
    "application received",
    "we received your application",
    "we've received your application",
    "your application has been submitted",
    "your application was submitted",
    "application submitted",
)


def classify_submit_evidence(page, success_url_keywords) -> Dict[str, Any]:
    """What the ATS showed us after a run we're about to call `submitted`.

    The fillers mark a run submitted on three different grounds: the page
    landed on a thank-you / confirmation URL, the page text says the
    application was received, or the form simply went away with no error
    markers (the "best-effort" branch, and a resubmit that left zero invalid
    fields). Only the first two are positive proof the ATS accepted it, so
    record which one we had and let the audit and the confirmation email
    tell them apart. Never raises.
    """
    final_url = ""
    try:
        final_url = page.url or ""
    except Exception:
        pass
    lowered = final_url.lower()
    if any(kw in lowered for kw in success_url_keywords):
        return {"submit_evidence": EVIDENCE_CONFIRMATION_URL, "submit_final_url": final_url}
    try:
        text = (page.evaluate("() => document.body.innerText || ''") or "").lower()
    except Exception:
        text = ""
    if any(marker in text for marker in _SUCCESS_TEXT_MARKERS):
        return {"submit_evidence": EVIDENCE_CONFIRMATION_TEXT, "submit_final_url": final_url}
    return {"submit_evidence": EVIDENCE_UNCONFIRMED, "submit_final_url": final_url}
