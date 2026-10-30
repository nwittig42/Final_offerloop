"""
Posting reader: step 1 of Find contacts.

One JSON-mode LLM call turns a job doc into the facts later steps search on:
the team, the function, the office city, named people and company size. The
result is a pure function of the job dict plus the LLM call; the service
caches it as `posting_facts` on the job doc, this module never writes.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.services.openai_client import get_openai_client
from app.utils.locations import office_city_or_none

logger = logging.getLogger(__name__)

READER_MODEL = "gpt-4o-mini"
MAX_DESCRIPTION_CHARS = 5000
STARTUP_MAX_EMPLOYEES = 200  # startup = under 200 employees

FUNCTIONS = (
    "ib", "sales_trading", "asset_management", "consulting", "software",
    "product", "data", "marketing", "strategy", "finance", "operations", "other",
)
LEVELS = ("intern", "analyst", "associate", "other")
TEAM_SOURCES = ("job title", "department field", "description")

SYSTEM_PROMPT = """You read a job posting and extract facts used to find the people behind it.

Return STRICT JSON with exactly these keys:
- team: the specific team or group the role sits in, as a short name ("Investment Banking", "Office of the CTO", "Payments Risk"). Empty string if the posting does not say. Do not invent one and do not just repeat the company name. A department that only names the hiring program or HR ("University Recruiting", "Early Careers", "Internships") is not a team.
- team_source: where you found the team: "job title", "department field", or "description". Empty string if team is empty.
- function: one of ib, sales_trading, asset_management, consulting, software, product, data, marketing, strategy, finance, operations, other
- office_city: the city the role is based in, or null if remote, hybrid, multiple locations, or unclear
- level: one of intern, analyst, associate, other
- named_people: people the posting names (hiring manager, team lead, recruiter contact) as [{"name": "...", "role": "..."}]. Only people actually named in the text. Empty list if none.

No prose."""


def _clean(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _size_hint(job: dict) -> str:
    employees = job.get("linkedin_org_employees")
    if isinstance(employees, bool) or not isinstance(employees, (int, float)) or employees <= 0:
        return "unknown"
    return "startup" if employees < STARTUP_MAX_EMPLOYEES else "large"


def _is_internship(job: dict, level: str) -> bool:
    if level == "intern":
        return True
    haystack = f"{_clean(job.get('type'))} {_clean(job.get('title'))}".lower()
    return "intern" in haystack


# Department values that describe the hiring program or HR, not a team.
_NOT_A_TEAM = re.compile(
    r"recruit|campus|early.?career|early talent|university|intern|student|graduate|"
    r"\bprograms?\b|talent|human resources|\bhr\b|people ops|^general$|^other$|^n/?a$", re.I)


def _names_a_team(value: str, company: str) -> bool:
    if not value or _NOT_A_TEAM.search(value):
        return False
    if sum(ch.isdigit() for ch in value) >= 3:  # "R0123456", "1040 - Corp"
        return False
    return value.lower() != (company or "").strip().lower()


def _structured_team(job: dict) -> str:
    """department or team field, only when it names a real team."""
    company = _clean(job.get("company"))
    for field in ("department", "team"):
        value = _clean(job.get(field))
        if _names_a_team(value, company):
            return value
    return ""


def _fallback(job: dict) -> dict:
    team = _structured_team(job)
    return _assemble(job, team=team, team_source="department field" if team else "",
                     function="other", office_city=None, level="other", named_people=[])


def _assemble(job: dict, *, team: str, team_source: str, function: str,
              office_city: str | None, level: str, named_people: list) -> dict:
    return {
        "company": _clean(job.get("company")),
        "team": team,
        "team_source": team_source,
        "function": function,
        "office_city": office_city if office_city else office_city_or_none(job.get("location")),
        "level": level,
        "is_internship": _is_internship(job, level),
        "named_people": named_people,
        "size_hint": _size_hint(job),
    }


def _named_people(raw: Any) -> list[dict]:
    if not isinstance(raw, list):
        return []
    out, seen = [], set()
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = _clean(item.get("name"))
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        out.append({"name": name, "role": _clean(item.get("role"))})
    return out


def read_posting(job: dict) -> dict:
    """Extract team, function, office city, level and named people from a job doc."""
    job = job or {}
    client = get_openai_client()
    if not client:
        logger.warning("Posting reader: OpenAI client unavailable")
        return _fallback(job)

    user_text = "\n".join([
        f"Title: {_clean(job.get('title'))}",
        f"Company: {_clean(job.get('company'))}",
        f"Location: {_clean(job.get('location'))}",
        f"Department: {_clean(job.get('department'))}",
        f"Team: {_clean(job.get('team'))}",
        f"Description:\n{_clean(job.get('description_raw'))[:MAX_DESCRIPTION_CHARS]}",
    ])

    raw = "{}"
    try:
        response = client.chat.completions.create(
            model=READER_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_text},
            ],
            response_format={"type": "json_object"},
            temperature=0,
            max_tokens=500,
        )
        raw = response.choices[0].message.content or "{}"
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("LLM returned non-object JSON")
    except Exception:
        logger.exception("Posting reader: LLM call failed, raw=%s", raw[:200])
        return _fallback(job)

    # A team named in the job title is the strongest clue, then a department
    # field that names a team, then whatever the description says.
    llm_team = _clean(parsed.get("team"))
    llm_source = _clean(parsed.get("team_source"))
    if not llm_team or llm_source not in TEAM_SOURCES:
        llm_team, llm_source = "", ""
    structured = _structured_team(job)
    if llm_source == "job title":
        team, team_source = llm_team, llm_source
    elif structured:
        team, team_source = structured, "department field"
    else:
        team, team_source = llm_team, llm_source

    function = _clean(parsed.get("function")).lower()
    if function not in FUNCTIONS:
        function = "other"
    level = _clean(parsed.get("level")).lower()
    if level not in LEVELS:
        level = "other"

    return _assemble(
        job, team=team, team_source=team_source, function=function,
        office_city=office_city_or_none(_clean(parsed.get("office_city"))),
        level=level, named_people=_named_people(parsed.get("named_people")),
    )
