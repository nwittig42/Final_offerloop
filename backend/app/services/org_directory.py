"""
Org alumni directories (OFF-42). Turns the ingested org directories
(orgs / people / person_org_links, built by backend/scripts/org_directory_*)
into App-shape contacts for members of that org.

Membership lives on users/{uid}.orgMemberships as
  {orgId: {status: "active" | "pending", joinedAt, source}}
and is written only by the backend (routes/orgs.py). v1 joins are instant
("active"); the "pending" status is reserved for org-admin approval later
and is treated as not-a-member everywhere here.

The pilot directory is ~2K people, so members are joined once and cached
in-process instead of being queried per search.
"""
from __future__ import annotations

import re
import threading
import time

MEMBERSHIPS_FIELD = "orgMemberships"
ORGS_COLLECTION = "orgs"
# Join passwords live apart from orgs/ so no org read can ever return one.
# Server-only (default-deny in firestore.rules). Written by
# scripts/org_directory_set_password.py as a werkzeug hash, never plaintext.
ORG_SECRETS_COLLECTION = "org_secrets"
PEOPLE_COLLECTION = "people"
LINKS_COLLECTION = "person_org_links"

MEMBERS_TTL_SEC = 15 * 60
_members_cache: dict[str, tuple[float, list[dict]]] = {}
_cache_lock = threading.Lock()

# Words that only say "from this org" once the org name is matched.
_FILLER = r"(?:alumni|alumnus|alums?|brothers?|members?)"
_DANGLING = {"at", "in", "from", "who", "with", "of", "and", "or", "the", "a", "for", "work", "working", "are", "is"}


def _org_public(doc_id: str, d: dict) -> dict:
    return {
        "id": doc_id,
        "name": d.get("name") or doc_id,
        "shortName": d.get("shortName") or d.get("name") or doc_id,
        "school": d.get("school") or "",
        "type": d.get("type") or "",
        "memberCount": d.get("memberCount") or 0,
        "aliases": [a for a in (d.get("aliases") or []) if isinstance(a, str) and a.strip()],
        # The org's own color for its space in the app (header band, accents).
        "accentColor": d.get("accentColor") or "",
    }


def list_orgs(db) -> list[dict]:
    """Joinable orgs. An org only shows up once it's been seeded with joinable: true."""
    if db is None:
        return []
    out = []
    for snap in db.collection(ORGS_COLLECTION).stream():
        d = snap.to_dict() or {}
        if d.get("joinable") is True:
            out.append(_org_public(snap.id, d))
    out.sort(key=lambda o: o["name"].lower())
    return out


def check_org_password(db, org_id: str, password: str) -> bool | None:
    """True/False for the org's join password; None when it has none set
    (an org without a password can't be self-joined)."""
    from werkzeug.security import check_password_hash

    snap = db.collection(ORG_SECRETS_COLLECTION).document(org_id).get()
    pw_hash = (snap.to_dict() or {}).get("passwordHash") if snap.exists else None
    if not pw_hash:
        return None
    return bool(password) and check_password_hash(pw_hash, password)


def active_org_ids(user_data: dict | None) -> list[str]:
    memberships = (user_data or {}).get(MEMBERSHIPS_FIELD) or {}
    if not isinstance(memberships, dict):
        return []
    return sorted(
        org_id for org_id, m in memberships.items()
        if isinstance(m, dict) and m.get("status") == "active"
    )


def _org_terms(org: dict) -> list[str]:
    terms = {org.get("shortName") or "", org.get("name") or ""}
    terms.update(org.get("aliases") or [])
    # Longest first so "usc tke" wins over "tke" and gets stripped whole.
    return sorted((t.strip().lower() for t in terms if t and t.strip()), key=len, reverse=True)


def _tidy(text: str) -> str:
    words = text.split()
    while words and words[0].lower() in _DANGLING:
        words.pop(0)
    while words and words[-1].lower() in _DANGLING:
        words.pop()
    return " ".join(words)


def match_orgs_in_prompt(prompt: str, orgs: list[dict]) -> tuple[list[str], str]:
    """Org ids named in the prompt, and the prompt with the org mention removed.

    "TKE alumni at Goldman Sachs" -> (["usc_tke"], "at Goldman Sachs")
    """
    matched: list[str] = []
    cleaned = prompt or ""
    for org in orgs:
        for term in _org_terms(org):
            pattern = re.compile(rf"(?<![\w]){re.escape(term)}(?:['’]s)?(?:\s+{_FILLER})?(?![\w])", re.IGNORECASE)
            if pattern.search(cleaned):
                cleaned = pattern.sub(" ", cleaned)
                if org["id"] not in matched:
                    matched.append(org["id"])
    if not matched:
        return [], prompt
    cleaned = re.sub(rf"(?<![\w]){_FILLER}(?![\w])", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"[,.;:!?]+", " ", cleaned)
    return matched, _tidy(cleaned)


def resolve_org_targets(prompt: str, filter_org_ids, user_data: dict | None, orgs: list[dict]) -> tuple[list[str], str]:
    """Which org directories this search should read, limited to the user's
    active memberships. Returns ([], prompt) when the search isn't an org search."""
    mine = set(active_org_ids(user_data))
    if not mine:
        return [], prompt
    known = {o["id"] for o in orgs}
    from_filter = [i for i in (filter_org_ids or []) if isinstance(i, str) and i in known and i in mine]
    named, cleaned = match_orgs_in_prompt(prompt, [o for o in orgs if o["id"] in mine])
    targets = list(dict.fromkeys(from_filter + named))
    if not targets:
        return [], prompt
    return targets, (cleaned if named else prompt)


def join_hint(prompt: str, user_data: dict | None, orgs: list[dict]) -> dict | None:
    """When a search names a joinable org the user isn't in ("TKE alumni"),
    the org to point them at so they can join. Never reveals any members."""
    mine = set(active_org_ids(user_data))
    named, _ = match_orgs_in_prompt(prompt, [o for o in orgs if o["id"] not in mine])
    if not named:
        return None
    org = next(o for o in orgs if o["id"] == named[0])
    return {"id": org["id"], "name": org["name"], "shortName": org["shortName"]}


def load_org_members(db, org_id: str) -> list[dict]:
    """Every link for the org merged with its person doc. Cached per process."""
    now = time.time()
    with _cache_lock:
        hit = _members_cache.get(org_id)
        if hit and now - hit[0] < MEMBERS_TTL_SEC:
            return hit[1]

    from google.cloud.firestore_v1.base_query import FieldFilter

    links = [
        s.to_dict() or {}
        for s in db.collection(LINKS_COLLECTION).where(filter=FieldFilter("orgId", "==", org_id)).stream()
    ]
    keys = [l["personKey"] for l in links if l.get("personKey")]
    people: dict[str, dict] = {}
    people_col = db.collection(PEOPLE_COLLECTION)
    for i in range(0, len(keys), 300):
        refs = [people_col.document(k) for k in keys[i:i + 300]]
        for snap in db.get_all(refs):
            if snap.exists:
                people[snap.id] = snap.to_dict() or {}

    members = []
    for link in links:
        person = people.get(link.get("personKey") or "")
        if person is None:
            continue
        members.append({**person, **{f"link_{k}": v for k, v in link.items()}, "personKey": link["personKey"]})

    with _cache_lock:
        _members_cache[org_id] = (now, members)
    return members


def org_badge(org: dict, member: dict) -> str:
    # No year here: the "'YY" on a directory name is the year they joined
    # the chapter, not graduation. The real class year rides separately as
    # OrgGradYear and only when the directory lists one.
    return f"{org.get('shortName') or org.get('name')} Alumni"


def grad_year(member: dict) -> int | None:
    """The directory's own "Class of YYYY", never estimated from the join year."""
    y = member.get("link_classOfYear")
    return y if isinstance(y, int) and 1900 < y < 2100 else None


# First words that start a job title, used to find where a directory's
# smashed "CompanyTitle" headline splits ("Houlihan LokeyAssociate").
_TITLE_WORDS = {
    "senior", "sr", "junior", "jr", "associate", "analyst", "director", "manager", "vice", "vp", "svp",
    "evp", "avp", "president", "partner", "principal", "founder", "co-founder", "cofounder", "ceo",
    "cfo", "coo", "cto", "cmo", "cio", "owner", "lead", "head", "chief", "consultant", "engineer",
    "software", "product", "sales", "account", "executive", "managing", "general", "attorney",
    "counsel", "intern", "specialist", "coordinator", "advisor", "adviser", "broker", "agent",
    "representative", "officer", "retired", "student", "teacher", "professor", "doctor", "physician",
    "dentist", "accountant", "cpa", "controller", "treasurer", "trader", "portfolio", "investment",
    "regional", "national", "global", "business", "marketing", "operations", "project", "program",
    "development", "research", "data", "design", "designer", "architect", "developer",
    "administrator", "assistant", "supervisor", "superintendent", "captain", "deputy", "member",
    "board", "chairman", "chair", "staff", "technician", "strategist", "editor", "writer", "producer",
    "recruiter", "md", "substitute", "real", "financial", "finance", "acquisitions", "logistics",
    "property", "commercial", "district", "area", "branch", "store", "office", "team", "creative",
    "art", "content", "communications", "relationship", "client", "customer", "field", "technical",
    "pilot", "realtor", "secretary",
}


def _first_word(s: str) -> str:
    return (re.match(r"[A-Za-z][A-Za-z-]*,?", s) or re.match(r"", "")).group(0).lower()


def split_headline(headline: str) -> tuple[str, str]:
    """("Company", "Title") from a directory headline that glued them
    together with no separator, e.g. "GoogleMarketing Strategy Lead" ->
    ("Google", "Marketing Strategy Lead"). A headline with no visible seam
    is a company on its own, unless it reads like a title ("Retired")."""
    h = (headline or "").strip()
    if not h or h.startswith("#") or len(h) < 2:
        return "", ""
    seams = []
    for i in range(1, len(h)):
        a, b = h[i - 1], h[i]
        if not b.isupper():
            continue
        nxt = h[i + 1] if i + 1 < len(h) else ""
        # "LokeyAssociate", "N.A.Senior", "(BCG)Talent": lower/punct then upper.
        # Not after "&": that's "M&A" / "M&T", not a seam.
        if a.islower() or a.isdigit() or a in ".),'":
            seams.append(i)
        # "MECADeputy": an acronym running into a capitalized word.
        elif a.isupper() and nxt.islower() and i >= 2 and h[i - 2].isupper():
            seams.append(i)
        # "ZHUCEO of ...": an acronym running into an acronym title.
        elif a.isupper() and re.match(r"(CEO|CFO|COO|CTO|CMO|CIO|SVP|EVP|AVP|VP|MD)(?![A-Za-z])", h[i:]):
            seams.append(i)
    titled = [i for i in seams if _first_word(h[i:]).rstrip(",") in _TITLE_WORDS]
    cut = titled[0] if titled else (seams[-1] if seams else None)
    if cut is None:
        return ("", h) if _first_word(h).rstrip(",") in _TITLE_WORDS else (h, "")
    return h[:cut].strip(" ,-"), h[cut:].strip(" ,-")


def _nice_title(t: str) -> str:
    """Warehouse titles arrive lowercased ("investment banking associate")."""
    if not t or t != t.lower():
        return t
    small = {"of", "and", "the", "at", "in", "for", "to", "&"}
    words = t.split()
    return " ".join(w if (i and w in small) else (w.upper() if w in {"vp", "md", "ceo", "cfo", "coo", "cto", "tmt", "ib"} else w[:1].upper() + w[1:])
                    for i, w in enumerate(words))


def warehouse_by_name(db, school: str) -> dict[tuple[str, str], dict]:
    """People already in our firm_employees warehouse from this school, keyed
    by (first, last) lowercased. Only names that appear once, so a match is
    never a guess between two "Michael Smith"s. Cached like the members."""
    key = f"wh::{school}"
    now = time.time()
    with _cache_lock:
        hit = _members_cache.get(key)
        if hit and now - hit[0] < MEMBERS_TTL_SEC:
            return hit[1]  # type: ignore[return-value]
    from google.cloud.firestore_v1.base_query import FieldFilter
    from app.services.firm_cache.schema import FIRM_EMPLOYEES_COLLECTION, slugify_school

    by_name: dict[tuple[str, str], dict] = {}
    dupes: set[tuple[str, str]] = set()
    slug = slugify_school(school)
    if slug:
        q = db.collection(FIRM_EMPLOYEES_COLLECTION).where(filter=FieldFilter("schools", "array_contains", slug))
        for snap in q.stream():
            d = snap.to_dict() or {}
            k = ((d.get("first_name") or "").strip().lower(), (d.get("last_name") or "").strip().lower())
            if not all(k):
                continue
            if k in by_name:
                dupes.add(k)
            by_name[k] = d
    for k in dupes:
        by_name.pop(k, None)
    with _cache_lock:
        _members_cache[key] = (now, by_name)  # type: ignore[assignment]
    return by_name


def member_contact(member: dict, org: dict, wh: dict | None = None) -> dict | None:
    """App-shape contact for one directory member, or None when they shouldn't
    be offered (deceased, still an undergrad, or no way to reach them).
    `wh` is this person's warehouse doc when one matches by name and school:
    a fresher job, a LinkedIn URL and a photo."""
    if member.get("link_membershipStatus") == "deceased":
        return None
    if member.get("link_membershipType") != "alumni":
        return None
    work_email = (member.get("workEmail") or "").strip()
    # A "lost" member's directory email is the address TKE itself flagged as stale.
    dir_email = "" if member.get("link_membershipStatus") == "lost" else (member.get("email") or "").strip()
    email = work_email or dir_email
    wh = wh or {}
    linkedin = (member.get("linkedinUrl") or "").strip() or (wh.get("linkedin_url") or "").strip()
    if not email and not linkedin:
        return None

    headline = (member.get("directoryHeadline") or member.get("link_headlineRaw") or "").strip()
    if headline.startswith("#"):  # spreadsheet junk like "#ERROR!"
        headline = ""
    # Job, best source first: Coresignal's current role, then our warehouse
    # (same person by name + school), then the directory's own headline.
    company = (member.get("coresignalCompany") or "").strip()
    title = (member.get("coresignalTitle") or "").strip()
    if not (company or title) and wh:
        company = (wh.get("company_display") or "").strip()
        title = _nice_title((wh.get("title") or wh.get("headline") or "").strip())
    if not (company or title) and headline:
        company, title = split_headline(headline)
    year = member.get("link_chapterYear") or member.get("link_classOfYear")
    return {
        "FirstName": member.get("firstName") or "",
        "LastName": member.get("lastName") or "",
        "LinkedIn": linkedin,
        "Email": email,
        "WorkEmail": work_email,
        "PersonalEmail": dir_email if dir_email != work_email else "",
        "Title": title,
        "Company": company,
        "City": "",
        "State": "",
        "College": org.get("school") or "",
        "EducationTop": org.get("school") or "Not available",
        "WorkSummary": " at ".join(p for p in (title, company) if p) or headline,
        # Directory phone numbers are never surfaced (Nick, 2026-10-18).
        "Phone": "",
        "PhotoUrl": member.get("photoUrl") or wh.get("photo_url") or "",
        "JoinedYear": None,
        "EmailSource": "fullenrich" if work_email else ("org_directory" if dir_email else ""),
        "EmailVerified": bool(work_email and member.get("workEmailStatus") == "DELIVERABLE"),
        "DataVersion": "org_directory",
        "source": "org_directory",
        "PersonKey": member.get("personKey") or "",
        "OrgId": org["id"],
        "OrgBadge": org_badge(org, member),
        "OrgHeadline": headline,
        # Join or class year, only for ordering by recency.
        "OrgYear": year if isinstance(year, int) else None,
        "OrgGradYear": grad_year(member),
    }


def _names(items) -> list[str]:
    out = []
    for it in items or []:
        v = it.get("name") if isinstance(it, dict) else it
        if isinstance(v, str) and v.strip():
            out.append(v.strip().lower())
    return out


_NON_ROLE = {"people", "person", "folks", "anyone", "someone", "everyone", "contacts", "guys"}


_ROLE_ABBREVIATIONS = {"vp", "svp", "evp", "avp", "md", "ed", "ceo", "cfo", "coo", "cto", "pm", "swe", "sde", "ib", "pe", "vc"}


def _role_requested(parsed: dict, prompt: str) -> bool:
    """The parser invents titles for any company ("People at Google" gets a
    full title list), so only filter by title when the user typed a role:
    a typed word that shows up in the parser's titles, or a role abbreviation.
    Company words like "goldman" or "gs" never count. When unsure this says
    no, which only means a broader list."""
    title_text = " ".join(
        _names(parsed.get("title_variations"))
        + [t for c in parsed.get("companies") or [] if isinstance(c, dict) for t in _names(c.get("matched_titles"))]
    )
    company_words = {w for n in _names(parsed.get("companies")) for w in re.findall(r"\w+", n)}
    for w in re.findall(r"\w+", (prompt or "").lower()):
        if w in company_words or w in _NON_ROLE or w in _DANGLING:
            continue
        stem = w[:-1] if w.endswith("s") and len(w) > 2 else w
        if stem in _ROLE_ABBREVIATIONS or (len(stem) > 2 and stem in title_text):
            return True
    return False


def filter_by_query(contacts: list[dict], parsed: dict | None, prompt: str | None = None) -> list[dict]:
    """Narrow by company and title. Locations, schools and industries are
    ignored: directories carry no such data, and every member shares the school.
    `prompt` is the user's remaining text; titles apply only if it names a role
    (None means always apply them)."""
    parsed = parsed or {}
    companies = _names(parsed.get("companies"))
    titles: list[str] = []
    if prompt is None or _role_requested(parsed, prompt):
        titles = _names(parsed.get("title_variations"))
        for c in parsed.get("companies") or []:
            if isinstance(c, dict):
                titles.extend(_names(c.get("matched_titles")))
        # "Investment Banking Analyst" also matches a bare "Analyst" headline.
        titles += [t.split()[-1] for t in titles if " " in t]

    def haystack(c: dict) -> str:
        return " ".join([c.get("Company") or "", c.get("Title") or "", c.get("OrgHeadline") or ""]).lower()

    out = []
    for c in contacts:
        h = haystack(c)
        if companies and not any(n in h for n in companies):
            continue
        if titles and not any(n in h for n in titles):
            continue
        out.append(c)
    return out


def rank(contacts: list[dict]) -> list[dict]:
    """Verified email first, then any email, then most recent alumni first."""
    return sorted(
        contacts,
        key=lambda c: (
            0 if c.get("EmailVerified") else 1 if c.get("Email") else 2,
            -(c.get("OrgYear") or 0),
        ),
    )


def directory_contacts(db, org: dict) -> list[dict]:
    """Every reachable alum in one org's directory, ranked."""
    seen: set[str] = set()
    out: list[dict] = []
    try:
        wh_index = warehouse_by_name(db, org.get("school") or "")
    except Exception as e:  # enrichment only; the directory works without it
        print(f"[OrgDirectory] warehouse match skipped: {e}")
        wh_index = {}
    for m in load_org_members(db, org["id"]):
        name_key = ((m.get("firstName") or "").strip().lower(), (m.get("lastName") or "").strip().lower())
        c = member_contact(m, org, wh_index.get(name_key))
        if c is None:
            continue
        key = (c["Email"] or c["LinkedIn"]).lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(c)
    return rank(out)


def directory_row(contact: dict) -> dict:
    """What the browsable directory shows before a member is unlocked: who
    they are and where they work, never their email or phone."""
    return {
        "personKey": contact["PersonKey"],
        "firstName": contact["FirstName"],
        "lastName": contact["LastName"],
        "title": contact["Title"],
        "company": contact["Company"],
        "headline": contact["OrgHeadline"],
        "year": contact["OrgYear"],
        "gradYear": contact["OrgGradYear"],
        "badge": contact["OrgBadge"],
        "linkedinUrl": contact["LinkedIn"],
        "photoUrl": contact["PhotoUrl"],
        "school": contact["College"],
        "hasEmail": bool(contact["Email"]),
    }


def search_org_directory(db, org_ids: list[str], parsed: dict | None, orgs: list[dict], prompt: str | None = None, cap: int = 200) -> list[dict]:
    by_id = {o["id"]: o for o in orgs}
    seen: set[str] = set()
    out: list[dict] = []
    for org_id in org_ids:
        if org_id not in by_id:
            continue
        for c in directory_contacts(db, by_id[org_id]):
            key = (c["Email"] or c["LinkedIn"]).lower()
            if key not in seen:
                seen.add(key)
                out.append(c)
    return rank(filter_by_query(out, parsed, prompt))[:cap]
