"""
Result checker for prompt search (OFF-58).

The providers match on fields; the student typed a sentence. One cheap LLM
call reads every candidate against that sentence and says which ones
actually fit ("USC alumni at tech startups in SF" should not keep a Walmart
director), so the search only returns, and only charges for, real matches.

Fails open: if the model is unavailable or errors, everyone is kept and the
search behaves as it did before.
"""
import json
from typing import Any, Dict, List

from app.services.openai_client import get_openai_client

JUDGE_TIMEOUT = 12.0
JUDGE_MODEL = "gpt-4.1-mini"

_SYSTEM = """You check people-search results against what a college student asked for.
For each candidate, decide whether they fit the request as the student meant it.

Rules:
- Judge the CURRENT role and company only.
- Use what you know about the company too: a household-name retailer, bank or big tech company is not a "startup" even if the data is thin.
- A "startup" is a young, private company of up to a few hundred people. Being founded very recently makes a company MORE of a startup, never less. Well-known venture-backed companies (Otter.ai, Ramp, Retool) are startups until they are large or public. Any private company under about 500 employees counts; never drop one for being "too big" below that.
- School and location must match when the request names them. A city includes its metro area: San Francisco means the whole Bay Area (Oakland, San Jose, Palo Alto, Mountain View, Menlo Park and so on); New York includes Brooklyn and Jersey City.
- Be strict on the kind of company and the kind of role the student asked for; be lenient on exact title wording.
- When the data is missing and nothing contradicts the request, keep the candidate.
- Only drop for a concrete mismatch you can name.

Return ONLY JSON: {"results": [{"i": <index>, "keep": true|false, "why": "<5-10 words>"}]}, one entry per candidate."""


def _describe(i: int, c: Dict[str, Any]) -> Dict[str, Any]:
    company = {
        "name": c.get("Company") or "",
        "industry": c.get("CompanyIndustry") or "",
        "employees": c.get("CompanyEmployees"),
        "type": c.get("CompanyType") or "",
        "founded": c.get("CompanyFounded"),
        "last_funding": c.get("CompanyLastFunding") or "",
        "hq": c.get("CompanyHQ") or "",
    }
    return {
        "i": i,
        "title": c.get("Title") or c.get("JobTitle") or "",
        "company": {k: v for k, v in company.items() if v not in ("", None)},
        "location": c.get("location") or ", ".join(x for x in (c.get("City"), c.get("State")) if x),
        "schools": c.get("EducationTop") or c.get("College") or "",
    }


def judge_contacts(prompt: str, contacts: List[Dict[str, Any]]) -> List[bool]:
    """keep/drop per contact, in order. All True when the check can't run."""
    if not contacts:
        return []
    client = get_openai_client()
    if not client:
        return [True] * len(contacts)
    payload = {"request": prompt, "candidates": [_describe(i, c) for i, c in enumerate(contacts)]}
    try:
        resp = client.chat.completions.create(
            model=JUDGE_MODEL,
            messages=[
                {"role": "system", "content": _SYSTEM},
                {"role": "user", "content": json.dumps(payload)},
            ],
            max_tokens=80 + 40 * len(contacts),
            temperature=0,
            response_format={"type": "json_object"},
            timeout=JUDGE_TIMEOUT,
        )
        data = json.loads(resp.choices[0].message.content or "{}")
        verdicts = {int(r["i"]): (bool(r.get("keep", True)), str(r.get("why") or ""))
                    for r in data.get("results", []) if isinstance(r, dict) and "i" in r}
    except Exception as e:
        print(f"[SearchJudge] check failed, keeping everyone: {e}")
        return [True] * len(contacts)

    keep = []
    for i, c in enumerate(contacts):
        ok, why = verdicts.get(i, (True, "no verdict"))
        if not ok:
            print(f"[SearchJudge] drop {c.get('FirstName', '')} {c.get('LastName', '')} @ {c.get('Company', '')}: {why}")
        keep.append(ok)
    print(f"[SearchJudge] kept {sum(keep)}/{len(contacts)}")
    return keep
