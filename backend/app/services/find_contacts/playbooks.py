"""Company size and per-function title playbooks for Find contacts."""
import re
from pathlib import Path

LARGE_EMPLOYEE_THRESHOLD = 200

_SUFFIXES = {"inc", "llc", "ltd", "corp", "corporation", "co", "company",
             "group", "holdings", "plc", "&"}


def _canonicalize(name):
    s = (name or "").lower().replace("&", " & ")
    s = re.sub(r"[.,'\"]", "", s)
    s = re.sub(r"[^a-z0-9& ]+", " ", s)
    tokens = s.split()
    while len(tokens) > 1 and tokens[-1] in _SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


_large_cache = None


def _large_employers():
    global _large_cache
    if _large_cache is None:
        path = Path(__file__).with_name("large_employers.txt")
        lines = path.read_text(encoding="utf-8").splitlines()
        _large_cache = {_canonicalize(l) for l in lines if l.strip()}
    return _large_cache


def company_size(facts, job):
    if _canonicalize(facts.get("company")) in _large_employers():
        return "large"
    employees = (job or {}).get("linkedin_org_employees")
    if isinstance(employees, (int, float)) and employees >= LARGE_EMPLOYEE_THRESHOLD:
        return "large"
    if facts.get("size_hint") == "large":
        return "large"
    return "startup"


_STOP_WORDS = {"of", "the", "and", "&", "for", "in", "at", "a", "an"}
_GENERIC_WORDS = {
    "office", "team", "group", "division", "department", "global", "americas",
    "emea", "apac", "north", "america", "manager", "director", "head", "lead",
    "senior", "associate", "analyst", "intern", "internship", "program",
    "summer", "vp", "vice", "president",
}

_C_SUITE = {
    "cto": "Chief Technology Officer",
    "cfo": "Chief Financial Officer",
    "coo": "Chief Operating Officer",
    "cmo": "Chief Marketing Officer",
    "cio": "Chief Information Officer",
    "cpo": "Chief Product Officer",
    "ciso": "Chief Information Security Officer",
    "chro": "Chief Human Resources Officer",
    "ceo": "Chief Executive Officer",
}

_FUNCTION_NAMES = {
    "ib": "Investment Banking",
    "sales_trading": "Sales & Trading",
    "asset_management": "Asset Management",
    "consulting": "Consulting",
    "software": "Engineering",
    "product": "Product",
    "data": "Data",
    "marketing": "Marketing",
    "strategy": "Strategy",
    "finance": "Finance",
    "operations": "Operations",
}

_FINANCE_FUNCTIONS = {"ib", "sales_trading", "asset_management"}
_TECH_FUNCTIONS = {"software", "product", "data"}


def _keywords(team):
    out = []
    for w in re.findall(r"[a-z0-9]+", (team or "").lower()):
        if w in _STOP_WORDS or w in _GENERIC_WORDS or w in out:
            continue
        out.append(w)
    return out


def _fill(templates, **values):
    """Substitute placeholders; drop titles whose placeholder is empty; dedupe."""
    out = []
    for t in templates:
        keys = re.findall(r"\{(\w+)\}", t)
        if any(not values.get(k) for k in keys):
            continue
        title = t.format(**values)
        if title not in out:
            out.append(title)
    return out


def _c_suite_titles(team):
    out = []
    for w in re.findall(r"[a-z]+", (team or "").lower()):
        title = _C_SUITE.get(w)
        if title and title not in out:
            out.append(title)
    return out


def slot_specs(facts, size):
    team = (facts.get("team") or "").strip()
    function = facts.get("function") or "other"
    keywords = _keywords(team)

    if size == "startup":
        name = _FUNCTION_NAMES.get(function, "")
        return {
            "leader": {"titles": _fill(
                ["Founder", "Co-Founder", "CEO", "Head of {function}"], function=name),
                "keywords": keywords},
            "process": {"titles": _fill(["{function} Lead"], function=name),
                        "keywords": keywords},
        }

    if function in _FINANCE_FUNCTIONS:
        leader = ["Managing Director", "Head of {team}"]
        process = ["Vice President", "Director", "Associate"]
        recruiter = ["Campus Recruiting", "Campus Recruiter", "Early Careers Recruiter"]
    elif function in _TECH_FUNCTIONS:
        leader = ["Director", "Head of {team}", "VP {team}"]
        process = ["Engineering Manager", "Product Manager", "Manager"]
        recruiter = ["University Recruiter", "Early Careers Recruiter"]
    elif function == "consulting":
        leader = ["Partner", "Principal"]
        process = ["Manager", "Senior Associate"]
        recruiter = ["Campus Recruiter"]
    else:
        leader = ["Vice President", "Head of {team}"]
        process = ["Director", "Senior Manager"]
        recruiter = ["Campus Recruiter", "Early Talent Recruiter"]

    leader_titles = _fill(leader, team=team)
    leader_titles += [t for t in _c_suite_titles(team) if t not in leader_titles]

    return {
        "leader": {"titles": leader_titles, "keywords": keywords},
        "process": {"titles": _fill(process), "keywords": keywords},
        "recruiter": {"titles": _fill(recruiter), "keywords": keywords},
    }
