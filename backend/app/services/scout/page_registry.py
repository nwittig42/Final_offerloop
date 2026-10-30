"""Scout PAGE_REGISTRY: the single source of truth for every page Scout can
navigate a user to.

The registry drives three things:
  1. The "pages you can navigate to" section of Scout's system prompt
     (build_pages_prompt_section), so the prompt and the navigable route set
     can never drift apart.
  2. navigate tool-call validation (valid_routes, get_page).
  3. The approve-flow rules (required_inputs, credit_cost, tier_required).

Route strings match the LIVE frontend route table (connect-grow-hire/src/
App.tsx). The Find tabs are distinct entries keyed by their full
query-string route ("/find?tab=companies") because they are different
products with different prefill fields; get_page resolves an exact match
before falling back to the bare path.

Fields per entry:
  route            Path Scout navigates to.
  purpose          What the page does. Terse and factual.
  inputs           Form field names Scout may prefill. These must match the
                   frontend form fields AND the scout_prefill sessionStorage
                   bridge. Empty when the page has no Scout-prefillable form.
  required_inputs  Subset of inputs that must be present to navigate. Empty for
                   every page today: landing on a page never hard-requires a
                   field, the page accepts partial prefill. Kept as the hook
                   the clarify rule reads.
  send_user_here_when  Natural-language trigger for the model.
  credit_cost      Credits the page's own action charges (per result/use), or
                   None when the page has no credit-spending action. Scout
                   never spends credits; this tells the approve flow whether a
                   credit-spending action is implied. Numbers mirror
                   backend/app/config.py CREDIT_COSTS.
  tier_required    Minimum tier to use the page ("pro"/"elite"), or None for
                   all tiers.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

# Each entry is a plain dict so the registry is trivially serializable and easy
# to diff. See module docstring for the field contract.
PAGE_REGISTRY: List[Dict[str, Any]] = [
    {
        "route": "/find",
        "purpose": "Search, People tab: find professionals at companies to network with, then generate personalized outreach emails. The search box accepts a full natural-language prompt, so school context, alumni framing, year, or other profile signals can be carried via the `prompt` field.",
        # `prompt` carries a full natural-language search ("McKinsey
        # consultants in LA from USC") that goes straight into the search bar.
        # Use it whenever the reasoning depends on context that does not fit
        # the three structured fields - school, year, alumni framing. The
        # structured fields are still accepted for simple cases.
        "inputs": ["job_title", "company", "location", "prompt"],
        "required_inputs": [],
        "send_user_here_when": "the user wants to find people, contacts, professionals, or alumni to network with or email. NOT for job listings or applying to jobs - that is the Job Board",
        "credit_cost": 10,
        "tier_required": None,
        # The page honors auto_submit on the Scout navigate payload: when set,
        # the prefill lands AND the search runs automatically.
        "auto_submit_supported": True,
    },
    {
        "route": "/find?tab=companies",
        "purpose": "Search, Companies tab: discover companies and firms matching your criteria. The search box accepts a full natural-language prompt, so size qualifiers, hiring posture, alumni density, or any other framing can be carried via the `prompt` field.",
        "inputs": ["industry", "location", "size", "prompt"],
        "required_inputs": [],
        "send_user_here_when": "the user wants to find or research companies, firms, or employers rather than individual people",
        "credit_cost": 10,
        "tier_required": "pro",
        "auto_submit_supported": True,
    },
    {
        "route": "/find?tab=hiring-managers",
        "purpose": "Search, Hiring Managers tab: find recruiters and hiring managers at target companies, or for a specific job posting URL.",
        "inputs": ["company", "job_title", "location", "job_url"],
        "required_inputs": [],
        "send_user_here_when": "the user wants to find recruiters or hiring managers specifically",
        "credit_cost": 10,
        "tier_required": None,
    },
    {
        "route": "/find/templates",
        "purpose": "Create and manage reusable outreach email templates; pick the template applied to drafted emails.",
        "inputs": [],
        "required_inputs": [],
        "send_user_here_when": "the user wants to create, edit, or choose their outreach email template",
        "credit_cost": None,
        "tier_required": None,
    },
    {
        "route": "/upload-list",
        "purpose": "Upload a contact list (CSV or pasted LinkedIn URLs). Offerloop finds emails, drafts outreach, and saves the contacts to My Network.",
        "inputs": [],
        "required_inputs": [],
        "send_user_here_when": "the user has their own list of contacts or LinkedIn URLs to import, enrich, or bulk-email",
        "credit_cost": 10,
        "tier_required": None,
    },
    {
        "route": "/my-network/people",
        "purpose": "The People tab of My Network: every contact the user has saved, with drafting and sending in place.",
        "inputs": [],
        "required_inputs": [],
        "send_user_here_when": "the user wants to see or manage the contacts they have already saved",
        "credit_cost": None,
        "tier_required": None,
    },
    {
        "route": "/my-network/companies",
        "purpose": "The Companies tab of My Network: the spreadsheet of every firm the user has saved from a Find Companies search.",
        "inputs": [],
        "required_inputs": [],
        "send_user_here_when": "the user wants to see the companies they have saved or are tracking",
        "credit_cost": None,
        "tier_required": None,
    },
    {
        "route": "/my-network/managers",
        "purpose": "The Hiring Managers tab of My Network: every hiring manager and recruiter the user has saved, with outreach in place.",
        "inputs": [],
        "required_inputs": [],
        "send_user_here_when": "the user wants to see or manage the hiring managers or recruiters they have already saved",
        "credit_cost": None,
        "tier_required": None,
    },
    {
        "route": "/job-board",
        "purpose": "Browse job listings: a personalized ranked feed (List view) and a browse-everything gallery. Save jobs, see match scores, find the hiring manager or team for a job, and auto-apply on supported postings.",
        "inputs": ["query"],
        "required_inputs": [],
        "send_user_here_when": "the user wants to browse or search open job listings, internships, or roles, or wants to auto-apply to jobs (auto-apply is triggered from a job listing)",
        "credit_cost": None,
        "tier_required": None,
    },
    {
        "route": "/applications",
        "purpose": "Auto-apply home: three queues - All applications (every auto-application with its status), Needs your answers (applications paused on a screening question), and Finish in browser (forms blocked by a CAPTCHA needing a final human step).",
        "inputs": [],
        "required_inputs": [],
        "send_user_here_when": "the user asks about their applications, auto-apply status, submitted applications, or anything an application is waiting on",
        "credit_cost": None,
        "tier_required": "pro",
    },
    {
        "route": "/agent",
        "purpose": "Loops: the fleet of recurring autonomous outreach agents. Each Loop runs a saved brief (target role, company, school) on a schedule - finding contacts, drafting, and (with approval) sending outreach.",
        "inputs": [],
        "required_inputs": [],
        "send_user_here_when": "the user asks about their Loops, the agent, automated or recurring outreach that runs on its own",
        "credit_cost": None,
        "tier_required": None,
    },
    {
        "route": "/agent/setup",
        "purpose": "Create a new Loop: describe who to reach and how often, review the proposed brief, and launch the recurring agent.",
        "inputs": [],
        "required_inputs": [],
        "send_user_here_when": "the user wants to set up, create, or start a new Loop / recurring outreach agent",
        "credit_cost": None,
        "tier_required": None,
    },
    {
        "route": "/outbox",
        "purpose": "Inbox: email threads with the contacts you have emailed. Tracks sent mail, detects replies from Gmail, and drafts and sends responses.",
        "inputs": ["query"],
        "required_inputs": [],
        "send_user_here_when": "the user wants to see their email threads, sent mail, replies, or respond to someone",
        "credit_cost": 20,
        "tier_required": None,
    },
    {
        "route": "/coffee-chat-prep",
        "purpose": "Meeting Prep: paste the LinkedIn URL of who you are meeting and get a research dossier - background, talking points, and questions - as a PDF.",
        "inputs": ["linkedin_url"],
        "required_inputs": ["linkedin_url"],
        "send_user_here_when": "the user has a networking call, coffee chat, or informational meeting coming up and wants to prepare",
        "credit_cost": 30,
        "tier_required": None,
    },
    {
        "route": "/coffee-chat-library",
        "purpose": "Library of the meeting prep documents you have already generated.",
        "inputs": [],
        "required_inputs": [],
        "send_user_here_when": "the user wants to revisit or reread a meeting prep they generated earlier",
        "credit_cost": None,
        "tier_required": None,
    },
    {
        "route": "/recruiting-timeline",
        "purpose": "Your personalized recruiting timeline with key dates and milestones by industry.",
        "inputs": [],
        "required_inputs": [],
        "send_user_here_when": "the user asks about their recruiting timeline, key dates, deadlines, or schedule",
        "credit_cost": 20,
        "tier_required": None,
    },
    {
        "route": "/integrations",
        "purpose": "Connect external accounts. Gmail is the key one: connecting it lets Offerloop write drafts into Gmail, send outreach, and sync replies into the Inbox.",
        "inputs": [],
        "required_inputs": [],
        "send_user_here_when": "the user wants to connect or disconnect Gmail, or asks why drafts/replies are not showing up",
        "credit_cost": None,
        "tier_required": None,
    },
    {
        "route": "/mcp-server",
        "purpose": "Set up Offerloop's MCP server inside Claude or ChatGPT, so those assistants can find contacts, get company intel, and draft outreach using the user's Offerloop account.",
        "inputs": [],
        "required_inputs": [],
        "send_user_here_when": "the user wants to use Offerloop from Claude or ChatGPT, or asks about the MCP server / connector",
        "credit_cost": None,
        "tier_required": None,
    },
    {
        "route": "/pricing",
        "purpose": "View and manage your subscription. Compare Free, Pro, and Elite plans.",
        "inputs": [],
        "required_inputs": [],
        "send_user_here_when": "the user asks about plans, pricing, upgrading, or managing their subscription",
        "credit_cost": None,
        "tier_required": None,
    },
    {
        "route": "/account-settings",
        "purpose": "Manage your profile and update preferences.",
        "inputs": [],
        "required_inputs": [],
        "send_user_here_when": "the user wants to update their profile or change preferences",
        "credit_cost": None,
        "tier_required": None,
    },
    {
        "route": "/documentation",
        "purpose": "Help docs and guides for using Offerloop.",
        "inputs": [],
        "required_inputs": [],
        "send_user_here_when": "the user wants the help docs or written guides for the platform",
        "credit_cost": None,
        "tier_required": None,
    },
    {
        "route": "/onboarding",
        "purpose": "First-time setup: profile, academics, location, and goals.",
        "inputs": [],
        "required_inputs": [],
        "send_user_here_when": "the user wants to redo or finish first-time setup of their profile",
        "credit_cost": None,
        "tier_required": None,
    },
]


# ---------------------------------------------------------------------------
# Registry version - bump on ANY change to PAGE_REGISTRY or ROUTE_ALIASES.
# ---------------------------------------------------------------------------
# The Scout embedding caches (Phase 4 Tier B) stamp every entry with the
# version it was built against. A lookup ignores entries from an older
# version, and stale entries are evicted on the next write cycle. Bumping this
# is how a registry change invalidates the caches without a manual flush.
# v4: discover_companies executes multi-company discovery in chat; cached
# navigate answers for those phrasings must not serve.
# v5: /my-network/managers added; hiring-manager tracker aliases moved off
# the search tab.
REGISTRY_VERSION = 5


# ---------------------------------------------------------------------------
# Route aliases - hand-curated phrasings the pre-LLM router (Tier A) maps to a
# route. Kept next to PAGE_REGISTRY so the two never drift. Keys are lowercase
# phrases. The router only treats an alias as a hit behind an explicit
# navigation verb ("open ...", "go to ...", "take me to ..."), so a passing
# mention of a word like "jobs" inside a sentence does not trigger navigation.
# Every value must be a route present in PAGE_REGISTRY (asserted at import).
# ---------------------------------------------------------------------------
ROUTE_ALIASES: Dict[str, str] = {
    "search": "/find",
    "contact search": "/find",
    "people search": "/find",
    "find people": "/find",
    "find contacts": "/find",
    "firm search": "/find?tab=companies",
    "company search": "/find?tab=companies",
    "find companies": "/find?tab=companies",
    "recruiter spreadsheet": "/find?tab=hiring-managers",
    "hiring managers": "/my-network/managers",
    "hiring manager search": "/find?tab=hiring-managers",
    "find recruiters": "/find?tab=hiring-managers",
    "email templates": "/find/templates",
    "upload list": "/upload-list",
    "upload contacts": "/upload-list",
    "import contacts": "/upload-list",
    "import a list": "/upload-list",
    "meeting prep": "/coffee-chat-prep",
    "coffee chat prep": "/coffee-chat-prep",
    "meeting library": "/coffee-chat-library",
    "coffee chat library": "/coffee-chat-library",
    "inbox": "/outbox",
    "outbox": "/outbox",
    "tracker": "/outbox",
    "network tracker": "/outbox",
    "email tracker": "/outbox",
    "replies": "/outbox",
    "applications": "/applications",
    "auto apply": "/applications",
    "auto-apply": "/applications",
    "my applications": "/applications",
    "loops": "/agent",
    "loop": "/agent",
    "agent": "/agent",
    "new loop": "/agent/setup",
    "create a loop": "/agent/setup",
    "calendar": "/recruiting-timeline",
    "recruiting timeline": "/recruiting-timeline",
    "recruiting calendar": "/recruiting-timeline",
    "contact directory": "/my-network/people",
    "saved contacts": "/my-network/people",
    "my contacts": "/my-network/people",
    "my network": "/my-network/people",
    "hiring manager tracker": "/my-network/managers",
    "my hiring managers": "/my-network/managers",
    "saved hiring managers": "/my-network/managers",
    "saved recruiters": "/my-network/managers",
    "company tracker": "/my-network/companies",
    "companies tab": "/my-network/companies",
    "saved companies": "/my-network/companies",
    "job board": "/job-board",
    "jobs": "/job-board",
    "job listings": "/job-board",
    "integrations": "/integrations",
    "connect gmail": "/integrations",
    "gmail": "/integrations",
    "mcp server": "/mcp-server",
    "mcp": "/mcp-server",
    "connector": "/mcp-server",
    "pricing": "/pricing",
    "plans": "/pricing",
    "upgrade": "/pricing",
    "subscription": "/pricing",
    "account settings": "/account-settings",
    "settings": "/account-settings",
    "documentation": "/documentation",
    "docs": "/documentation",
    "help docs": "/documentation",
    "onboarding": "/onboarding",
}


# ---------------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------------

_BY_ROUTE: Dict[str, Dict[str, Any]] = {p["route"]: p for p in PAGE_REGISTRY}


def get_page(route: str) -> Optional[Dict[str, Any]]:
    """Return the registry entry for a route, or None if not a known route.

    Exact match (including query string) wins, so "/find?tab=companies"
    resolves to the Companies entry; then the bare path is tried, so
    "/find?anything" still resolves to "/find".
    """
    if not route:
        return None
    exact = _BY_ROUTE.get(route)
    if exact:
        return exact
    base = route.split("?")[0].rstrip("/") or "/"
    return _BY_ROUTE.get(base)


def page_identity(route: str) -> tuple:
    """Page identity for same-page comparisons: (path, find_tab).

    The three Find tabs share the /find pathname but are different products,
    so "/find" and "/find?tab=companies" are NOT the same page. For /find,
    no tab means the People tab. Query params other than tab are ignored.
    """
    raw = route or ""
    base = raw.split("?")[0].rstrip("/") or "/"
    tab = None
    if "?" in raw:
        try:
            from urllib.parse import parse_qs
            tab = (parse_qs(raw.split("?", 1)[1]).get("tab") or [None])[0]
        except Exception:
            tab = None
    if base == "/find" and not tab:
        tab = "people"
    return (base, tab)


def valid_routes() -> List[str]:
    """All routes Scout is allowed to navigate to."""
    return [p["route"] for p in PAGE_REGISTRY]


def is_valid_route(route: str) -> bool:
    """True if route (exact or ignoring any query string) is in the registry."""
    return get_page(route) is not None


def build_pages_prompt_section() -> str:
    """Render the registry into the "pages you can navigate to" prompt section.

    This is the single source of truth for that section: the prompt is
    generated from PAGE_REGISTRY, never hand-written prose.
    """
    lines: List[str] = [
        "## PAGES YOU CAN NAVIGATE TO",
        "",
        "Each page below is somewhere you can send the user with the navigate "
        "tool. Only ever navigate to a route in this list.",
        "",
    ]
    for page in PAGE_REGISTRY:
        purpose = page["purpose"]
        if page.get("tier_required"):
            purpose = f"{purpose} (requires {str(page['tier_required']).capitalize()})"
        lines.append(f"{page['route']} - {purpose}")
        lines.append(f"  Send here when: {page['send_user_here_when']}.")
        if page.get("inputs"):
            lines.append(f"  Prefillable fields: {', '.join(page['inputs'])}")
        if page.get("required_inputs"):
            lines.append(f"  Required to navigate: {', '.join(page['required_inputs'])}")
        if page.get("credit_cost") is not None:
            lines.append(
                f"  Action cost: about {page['credit_cost']} credits per result "
                f"(the user spends this, not you)."
            )
        lines.append("")
    return "\n".join(lines).rstrip()


# Fail fast at import if an alias points at a route not in the registry.
assert all(r in _BY_ROUTE for r in ROUTE_ALIASES.values()), (
    "ROUTE_ALIASES has a target not in PAGE_REGISTRY: "
    + ", ".join(sorted(set(ROUTE_ALIASES.values()) - set(_BY_ROUTE)))
)
