"""Parse the USC TKE directory CSV export into normalized records.

Source format is NOT real tabular CSV: it's a vertical scrape where every
field of every member sits on its own CSV row, and each member's block ends
with a literal "Save Contact" row. Confirmed against the full file
(11,708 rows -> 2,127 "Save Contact" markers == the "2127 Members" header,
2026-10-16 export).

Per-member block, in order, after [type, name]:
    [status]?            "Lost" or "Deceased", sometimes glued onto the
                          "Class of YYYY" row with no separator
    [class_of_year]?      "Class of YYYY"
    [headline]?           free-text company+title smashed together, no
                          reliable delimiter -> kept raw, never split
    [email]?              contains "@"
    [phone]?              "(ddd) ddd-dddd"

Classification is content-based (regex per line), not positional, since
any of the four optional fields can be independently absent.

THIS PARSER IS TKE-SPECIFIC. The next org directory will almost certainly
export in a different shape — see org_directory_README.md for what's
reusable (the orgs/people/person_org_links schema, the enrichment scripts)
versus what needs a new parser per source (this file).
"""
from __future__ import annotations

import csv
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

PHONE_RE = re.compile(r"^\(\d{3}\) \d{3}-\d{4}$")
CLASS_OF_RE = re.compile(r"^Class of (\d{4})(Lost|Deceased)?$")
NAME_SUFFIX_RE = re.compile(r"^(.+) '(\d{2})$")

ORG_ID = "usc_tke"
ORG_NAME = "USC Tau Kappa Epsilon"
SOURCE_LABEL = "usc_tke_directory_csv_2026-10-16"


@dataclass
class ParsedMember:
    raw_lines: list
    org_type: str  # "Alumni" | "Undergrad" (as the source literally says)
    name: str
    first_name: str
    last_name: str
    membership_status: str  # "active" | "lost" | "deceased"
    membership_type: str  # "alumni" | "undergrad"
    chapter_year: int | None  # from the "'YY" suffix on the name
    class_of_year: int | None  # from a separate "Class of YYYY" line
    headline_raw: str | None  # unsplit company+title text
    email: str | None
    phone: str | None

    def person_key(self) -> str:
        if self.email:
            return "email:" + self.email
        basis = "|".join(self.raw_lines)
        digest = hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]
        return f"synth:{digest}"


def _split_name(raw_name: str) -> tuple[str, str, int | None]:
    m = NAME_SUFFIX_RE.match(raw_name)
    if m:
        base, yy = m.group(1), int(m.group(2))
        year = 2000 + yy if yy <= 26 else 1900 + yy
    else:
        base, year = raw_name, None
    parts = base.split(" ", 1)
    first = parts[0]
    last = parts[1] if len(parts) > 1 else ""
    return first, last, year


def _classify_block(lines: list) -> ParsedMember:
    org_type = lines[0]
    raw_name = lines[1]
    first, last, chapter_year = _split_name(raw_name)

    status = "active"
    class_of_year = None
    headline = None
    email = None
    phone = None

    for line in lines[2:]:
        if line == "Lost":
            status = "lost"
            continue
        if line == "Deceased":
            status = "deceased"
            continue
        m = CLASS_OF_RE.match(line)
        if m:
            class_of_year = int(m.group(1))
            if m.group(2) == "Lost":
                status = "lost"
            elif m.group(2) == "Deceased":
                status = "deceased"
            continue
        if "@" in line:
            email = line.strip().lower()
            continue
        if PHONE_RE.match(line):
            phone = line
            continue
        headline = line  # leftover free text: the smashed company+title

    return ParsedMember(
        raw_lines=lines,
        org_type=org_type,
        name=raw_name,
        first_name=first,
        last_name=last,
        membership_status=status,
        membership_type="undergrad" if org_type == "Undergrad" else "alumni",
        chapter_year=chapter_year,
        class_of_year=class_of_year,
        headline_raw=headline,
        email=email,
        phone=phone,
    )


def parse_csv(path: str) -> list[ParsedMember]:
    with open(path, encoding="utf-8", errors="replace", newline="") as f:
        rows = list(csv.reader(f))
    lines = [(r[0] if r else "") for r in rows]

    save_idxs = [i for i, l in enumerate(lines) if l.strip() == "Save Contact"]
    if not save_idxs:
        raise ValueError("no 'Save Contact' markers found, file shape changed")

    first_header_idx = next(i for i, l in enumerate(lines) if l.strip() == "Last Name")
    start = first_header_idx + 1
    starts = [start] + [i + 1 for i in save_idxs[:-1]]

    members = []
    for s, e in zip(starts, save_idxs):
        block = [l for l in lines[s:e] if l.strip() != ""]
        if not block:
            continue
        members.append(_classify_block(block))
    return members


if __name__ == "__main__":
    import json
    import sys

    if len(sys.argv) < 2:
        print("usage: python org_directory_parse_tke.py <path to the TKE directory CSV>")
        sys.exit(1)
    path = sys.argv[1]
    if not Path(path).exists():
        print(f"no such file: {path}")
        sys.exit(1)

    members = parse_csv(path)
    print(f"parsed {len(members)} members")

    by_status: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for m in members:
        by_status[m.membership_status] = by_status.get(m.membership_status, 0) + 1
        by_type[m.membership_type] = by_type.get(m.membership_type, 0) + 1
    print("by status:", by_status)
    print("by type:", by_type)

    keys = [m.person_key() for m in members]
    print("unique person keys:", len(set(keys)), "of", len(keys))

    print("\nsample records:")
    for m in members[:3] + [x for x in members if x.membership_status == "lost"][:2] + [
        x for x in members if x.membership_type == "undergrad"
    ]:
        print(json.dumps(m.__dict__, indent=2, default=str))
