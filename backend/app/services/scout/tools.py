"""Scout tool schema: the three tools the model must pick exactly one of, every
turn.

Phase 2 of the Scout consolidation. This replaces the old "respond with valid
JSON in this exact format" prose contract. The model now answers by calling one
tool, and the tool's input schema enforces the structure, so the response can
never be missing a field or malformed.

The three tools map one to one onto what Scout can do on a turn:
  navigate  - propose taking the user somewhere (a plan, not an action).
  answer    - reply in chat, no navigation.
  clarify   - ask one short follow-up question.

Definitions are in Anthropic tool-use format. to_openai_tools() converts them
for the OpenAI fallback path (and for local testing without an Anthropic key).
"""
from __future__ import annotations

import asyncio
import re
from typing import Any, Dict, List, Optional

NAVIGATE_TOOL: Dict[str, Any] = {
    "name": "navigate",
    "description": (
        "Propose taking the user to a page, optionally with form fields "
        "pre-filled. Use when what the user wants is handled by a specific "
        "Offerloop page. This proposes a plan only: the user approves it "
        "before anything happens, and the user (never you) triggers the "
        "page's own action button. You never spend the user's credits."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "route": {
                "type": "string",
                "description": (
                    "Destination route. Must be exactly one of the routes "
                    "listed in the PAGES YOU CAN NAVIGATE TO section of the "
                    "system prompt."
                ),
            },
            "prefill": {
                "type": "object",
                "description": (
                    "Form fields to pre-fill on the destination page. Keys "
                    "must be field names from that route's 'Prefillable "
                    "fields' line. Use an empty object {} when there is "
                    "nothing to prefill."
                ),
                "additionalProperties": {"type": "string"},
            },
            "reasoning": {
                "type": "string",
                "description": (
                    "One short, human-readable sentence describing what this "
                    "does. Shown to the user verbatim on the approve card. "
                    "Example: 'Search for product managers at Google in New York.'"
                ),
            },
            "confidence": {
                "type": "number",
                "description": (
                    "How sure you are this route and prefill match what the "
                    "user wants, from 0.0 to 1.0. Use 0.9 or higher only when "
                    "the user was explicit. Use 0.6 to 0.9 when you inferred "
                    "the navigation from what they described. Use below 0.6 "
                    "when you are mostly guessing (prefer the clarify tool "
                    "instead in that case)."
                ),
            },
            "user_was_imperative": {
                "type": "boolean",
                "description": (
                    "True if the user gave a direct command to go somewhere "
                    "('take me to', 'go to', 'open', 'navigate to', 'show me "
                    "the X page'). False if you inferred the navigation from "
                    "what they described rather than an explicit command."
                ),
            },
            "auto_submit": {
                "type": "boolean",
                "description": (
                    "When true, the destination page populates the form AND "
                    "runs the search/action automatically - the user does not "
                    "have to click Search. Set true ONLY when the query is "
                    "complete: a clear target (company/role/people) AND a "
                    "specific count (or the count slider can default sanely). "
                    "Set false when the user might still want to tweak the "
                    "search before running it (broad query, ambiguous scope). "
                    "Currently honored by /find and /find?tab=companies."
                ),
            },
        },
        "required": [
            "route",
            "prefill",
            "reasoning",
            "confidence",
            "user_was_imperative",
        ],
    },
}

ANSWER_TOOL: Dict[str, Any] = {
    "name": "answer",
    "description": (
        "Reply to the user in chat without navigating anywhere. Use for "
        "questions, explanations, how-to help, and general conversation."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "text": {
                "type": "string",
                "description": (
                    "Your reply to the user, in Scout's voice. A planning, "
                    "strategy, how-to, or walkthrough reply MUST be broken "
                    "into multiple short paragraphs or numbered lines "
                    "separated by newline characters, never one dense "
                    "block. Short factual replies stay short."
                ),
            },
            "cta": {
                "type": "object",
                "description": (
                    "Optional. EXACTLY ONE end-of-message chip that bridges "
                    "the answer to a runnable Offerloop workflow. Omit "
                    "entirely when no relevant workflow exists. NEVER write "
                    "prose bridges like 'want me to...' or 'you might want "
                    "to...' instead of this chip - the chip IS the bridge. "
                    "Pick a route from PAGES YOU CAN NAVIGATE TO and prefill "
                    "fields the user named or that follow from the question."
                ),
                "properties": {
                    "label": {
                        "type": "string",
                        "description": (
                            "Short, concrete chip label, ideally under 10 "
                            "words. Example: 'Find 5 Bain alumni at USC'."
                        ),
                    },
                    "route": {
                        "type": "string",
                        "description": (
                            "Destination route. Must be exactly one of the "
                            "routes from PAGES YOU CAN NAVIGATE TO."
                        ),
                    },
                    "prefill": {
                        "type": "object",
                        "description": (
                            "Form fields to pre-fill on the destination "
                            "page. Same rules as navigate.prefill: only that "
                            "route's prefillable field names, only values "
                            "the user gave or that follow obviously."
                        ),
                        "additionalProperties": {"type": "string"},
                    },
                },
                "required": ["label", "route"],
            },
        },
        "required": ["text"],
    },
}

CLARIFY_TOOL: Dict[str, Any] = {
    "name": "clarify",
    "description": (
        "Ask the user one short follow-up question. Use when their intent is "
        "ambiguous, when a navigation could reasonably go to two different "
        "pages, or when a detail you need is missing."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "question": {
                "type": "string",
                "description": "One short, specific follow-up question.",
            },
        },
        "required": ["question"],
    },
}

# ---------------------------------------------------------------------------
# Helper tool. Not a reply to the user: the model may call this mid-turn to
# gather data, then still finishes the turn with exactly one terminal tool.
# ---------------------------------------------------------------------------

PARSE_JOB_URL_TOOL: Dict[str, Any] = {
    "name": "parse_job_url",
    "description": (
        "Helper tool, not a reply. Fetch a job-posting URL and extract its "
        "company, job title, and location. Call this when the user gives a "
        "link to a job posting and you need those details to fill in a "
        "navigate (for example, to the cover letter or interview prep page). "
        "After it returns, you still finish the turn with navigate, answer, "
        "or clarify."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "The job-posting URL to fetch and parse.",
            },
        },
        "required": ["url"],
    },
}

# Strategy memory helper tools (Phase 5, Stage 1). Like parse_job_url these
# are not a reply: the model calls one to write the user's multi-step plan to
# memory, then still finishes the turn with navigate, answer, or clarify.

SAVE_STRATEGY_TOOL: Dict[str, Any] = {
    "name": "save_strategy",
    "description": (
        "Helper tool, not a reply. Save the user's single active multi-step "
        "recruiting plan to memory, replacing any existing one. Call this when "
        "the user has a real goal that takes more than one step and more than "
        "one sitting (breaking into a field, landing interviews across a set "
        "of firms, a recruiting season), and they have agreed to the plan. Do "
        "not call it for a one-off task. After it returns you still finish the "
        "turn with answer."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "goal": {
                "type": "string",
                "description": (
                    "The user's goal in one plain sentence, concrete enough "
                    "to act on. Example: 'Land a 2027 summer analyst offer in "
                    "investment banking at a bulge bracket or elite boutique.'"
                ),
            },
            "steps": {
                "type": "array",
                "description": (
                    "The plan as an ordered list of 2 to 10 concrete steps, "
                    "sequenced by real recruiting timing."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "What to do, one short line.",
                        },
                        "detail": {
                            "type": "string",
                            "description": (
                                "Optional. One or two sentences of specifics: "
                                "which firms, which months, why this step."
                            ),
                        },
                        "route": {
                            "type": "string",
                            "description": (
                                "Optional. The Offerloop route this step maps "
                                "to, taken exactly from PAGES YOU CAN NAVIGATE "
                                "TO (for example /find). Omit it "
                                "when the step is not a single Offerloop page."
                            ),
                        },
                    },
                    "required": ["title"],
                },
            },
        },
        "required": ["goal", "steps"],
    },
}

UPDATE_STRATEGY_PROGRESS_TOOL: Dict[str, Any] = {
    "name": "update_strategy_progress",
    "description": (
        "Helper tool, not a reply. Update the user's active strategy: mark "
        "steps done, or close the whole plan out. Call it when the user "
        "reports finishing a step, or when the goal is reached or the user is "
        "walking away from it. After it returns you still finish the turn "
        "with answer."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "completed_steps": {
                "type": "array",
                "description": (
                    "Step numbers that are now done, 1-based, matching the "
                    "numbering in the ACTIVE STRATEGY block."
                ),
                "items": {"type": "integer"},
            },
            "close": {
                "type": "string",
                "enum": ["completed", "abandoned"],
                "description": (
                    "Set only to close the whole plan: 'completed' when the "
                    "goal is reached, 'abandoned' when the user is dropping "
                    "it. Leave unset for a normal progress update."
                ),
            },
        },
        "required": [],
    },
}

# Workflow state read tools (Phase 5, Stage 2). Read-only Firestore wrappers
# that let Scout pull state from across the product when grounding a reply or
# a strategy discussion. The returned summaries are JSON-safe and compact (one
# limit knob per tool, no raw documents).

GET_OUTBOX_STATUS_TOOL: Dict[str, Any] = {
    "name": "get_outbox_status",
    "description": (
        "Read-only. Returns the user's email outreach pipeline summary: "
        "total contacts in the outbox, how many are awaiting a reply, how "
        "many have replied, and the most recent ones with status and "
        "days-since-last-send. Call this whenever the answer depends on the "
        "user's actual outreach state (\"how many people did I email?\", "
        "\"did anyone reply?\", \"who has gone quiet?\"). Read only, no "
        "writes."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Max recent contacts to return (default 10).",
            },
        },
        "required": [],
    },
}

GET_RECENT_SEARCHES_TOOL: Dict[str, Any] = {
    "name": "get_recent_searches",
    "description": (
        "Read-only. Returns the user's recent natural-language contact "
        "searches (the prompt-search history on the Find page), newest "
        "first. Call this when the answer depends on what the user has been "
        "looking for lately."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Max recent searches to return (default 5).",
            },
        },
        "required": [],
    },
}

GET_RECENT_COVER_LETTERS_TOOL: Dict[str, Any] = {
    "name": "get_recent_cover_letters",
    "description": (
        "Read-only. Returns metadata for the user's recent cover letters "
        "(company, role, created_at, length), newest first. The letter body "
        "itself is not included; ask for it separately if needed."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Max recent cover letters to return (default 5).",
            },
        },
        "required": [],
    },
}

GET_MEETING_PREP_DRAFTS_TOOL: Dict[str, Any] = {
    "name": "get_meeting_prep_drafts",
    "description": (
        "Read-only. Returns the user's recent meeting prep drafts (coffee "
        "chats and informational meetings) with the contact name, meeting "
        "type, scheduled date, and created date, newest first."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Max recent meeting preps to return (default 5).",
            },
        },
        "required": [],
    },
}

GET_RECENT_FIRM_SEARCHES_TOOL: Dict[str, Any] = {
    "name": "get_recent_firm_searches",
    "description": (
        "Read-only. Returns the user's recent firm searches (structured "
        "company-discovery searches with filters and result counts), newest "
        "first. Distinct from get_recent_searches, which covers people."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Max recent firm searches to return (default 5).",
            },
        },
        "required": [],
    },
}

GET_APPLICATIONS_STATUS_TOOL: Dict[str, Any] = {
    "name": "get_applications_status",
    "description": (
        "Read-only. Returns the user's auto-apply applications: counts per "
        "queue (submitted, in flight, needs the user's answers, finish in "
        "browser, failed) plus the most recent applications with job title, "
        "company, and status. Use when the user asks about their "
        "applications, auto-apply progress, or what an application is "
        "waiting on. Cite specific jobs and companies, not just totals."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Max recent applications to return (default 8).",
            },
        },
        "required": [],
    },
}

GET_LOOPS_STATUS_TOOL: Dict[str, Any] = {
    "name": "get_loops_status",
    "description": (
        "Read-only. Returns the user's Loops (recurring outreach agents): "
        "per-loop name, status, cadence, last/next run, pending drafts, "
        "unread replies, and weekly credit spend, plus fleet totals. Use "
        "when the user asks about their Loops, the agent, or automated "
        "outreach that runs on its own. Cite specific loops by name."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "Max loops to return (default 6).",
            },
        },
        "required": [],
    },
}

FIND_JOBS_TOOL: Dict[str, Any] = {
    "name": "find_jobs",
    "description": (
        "Read-only. Searches the job catalog by keywords and returns up to "
        "`limit` recent matches with job_id, title, company, location, and "
        "whether each supports auto-apply. QUERY DISCIPLINE: use ONLY the "
        "role words the user gave, plus a company or city IF THE USER NAMED "
        "ONE (2-4 words total, e.g. 'data science intern'). NEVER pad the "
        "query with profile context - no school names, no 'student', no "
        "graduation year, no 'summer 2026': the catalog matches keywords "
        "literally and padded queries return nothing the user asked for. "
        "Use this to look up concrete jobs before discussing or applying to "
        "them. Present matches by title and company; never invent job ids."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Keywords, e.g. 'software engineer intern stripe'.",
            },
            "limit": {
                "type": "integer",
                "description": "Max jobs to return (default 5, max 10).",
            },
        },
        "required": ["query"],
    },
}

AUTO_APPLY_TOOL: Dict[str, Any] = {
    "name": "auto_apply_to_job",
    "description": (
        "EXECUTE ACTION - queues a real auto-apply submission for one job "
        "and spends the user's credits. HARD RULES: (1) consent is "
        "intent-based. When the user's message is an explicit imperative "
        "to apply ('apply to three data science internships', 'auto-apply "
        "me to the Snap role'), you may find_jobs and apply to the "
        "best-matching eligible jobs in the same turn, up to the count "
        "they named. When the request is exploratory or ambiguous ('what "
        "internships are out there', 'can you apply to jobs for me?', no "
        "count and no named job), list the matches and ask which ones "
        "FIRST; apply only after they answer. When unsure, ask. "
        "(2) job_id must come from a find_jobs result in this conversation, "
        "and the job must be auto-apply eligible. (3) at most 3 submissions "
        "per user turn. Your answer MUST reflect the ACTUAL number of "
        "auto_apply_to_job calls this turn — never promise 3 when only 2 "
        "ran, name every job (title, company) whose submission actually "
        "returned this turn with its status, and point the user to "
        "/applications to track progress. If the result code is "
        "PROFILE_REQUIRED, tell the user to complete their application "
        "profile from any job page first; INSUFFICIENT_CREDITS means they "
        "need credits; INELIGIBLE means this job's ATS is unsupported."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "job_id": {
                "type": "string",
                "description": "The job_id from a find_jobs result.",
            },
        },
        "required": ["job_id"],
    },
}

DRAFT_OUTREACH_EMAILS_TOOL: Dict[str, Any] = {
    "name": "draft_outreach_emails",
    "description": (
        "EXECUTE ACTION - writes personalized outreach emails as Gmail "
        "drafts for contacts ALREADY SAVED in the user's network, and "
        "attaches them so the Inbox shows each conversation. Use this when "
        "the user asks to draft, write, or email contacts that were already "
        "found ('draft emails to each of them' right after a search means "
        "THOSE contacts). NEVER run a new contact search for such a "
        "request - searching again finds different people and spends "
        "credits. ALWAYS pass contact_names when the user refers to any "
        "specific person or people - by name, or by pronoun ('her', 'him', "
        "'them' after a search): resolve the pronoun to the names shown in "
        "this chat and pass those exact names. Omitting contact_names "
        "falls back to the most recently saved contacts, which can email "
        "the WRONG person - only omit for an explicit 'my most recent "
        "contacts' style request. Free (the contact-search credits already "
        "covered drafting). Requires Gmail connected "
        "(GMAIL_NOT_CONNECTED -> point them to /integrations). After "
        "calling, your answer MUST name each drafted email (contact, "
        "company, subject) with a [View in Gmail](gmail_draft_url) "
        "markdown link per draft when gmail_draft_url is present, plus any "
        "skips with reasons. The review page is called the Inbox (route "
        "/outbox) - never call it Outbox to the user. cta: one draft -> "
        "route '/outbox?contact=<contact_id>' labeled 'Open in your Inbox' "
        "(it opens that exact conversation); multiple drafts -> '/outbox' "
        "labeled 'Open your Inbox'."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "contact_names": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Names of the saved contacts to draft for, as shown in chat. Omit for the most recent.",
            },
            "limit": {
                "type": "integer",
                "description": "Max drafts when contact_names is omitted (default 4, max 5).",
            },
        },
        "required": [],
    },
}

FIND_CONTACTS_TOOL: Dict[str, Any] = {
    "name": "find_contacts",
    "description": (
        "EXECUTE ACTION - runs a live people search, saves the results to "
        "the user's My Network, and spends 5 credits per contact returned. "
        "Use this when the user asks for people at a NAMED company ('find "
        "me 3 software engineers at Spotify', 'get me USC alumni at Bain') "
        "OR for an industry-wide search with no company ('find investment "
        "banking analysts in Los Angeles that graduated from USC') - pass "
        "role/school/location and leave company empty. At least one of "
        "company, role, or school is required. Surface the results IN THE "
        "CHAT: name, title, company, and the alumni/warmth hook when "
        "present. COUNT IS REQUIRED: if the user gave no number, clarify "
        "once for it before calling (this search spends credits per "
        "contact). CHAIN WHEN ASKED: if the same message asks to find AND "
        "email people ('find 3 Spotify engineers and email them'), call "
        "find_contacts first, then draft_outreach_emails with the returned "
        "names, in the SAME turn. Navigate to /find only when the user "
        "wants to browse or refine filters themselves, not when they asked "
        "for people. Error codes: COUNT_REQUIRED -> the user never said "
        "how many, ask them for a count (once) and call again next turn; "
        "INSUFFICIENT_CREDITS -> say what the search costs vs their "
        "balance; a zero-count result means the search genuinely found "
        "nobody - say so and suggest widening (different title wording, "
        "drop the school filter), never pretend."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "company": {
                "type": "string",
                "description": (
                    "Target company name, exactly as the user gave it. Leave "
                    "empty for industry-wide searches with no named company."
                ),
            },
            "role": {
                "type": "string",
                "description": "Role or function, e.g. 'software engineer'. Omit if not given.",
            },
            "school": {
                "type": "string",
                "description": "School for the alumni filter, ONLY if the user asked for alumni.",
            },
            "location": {
                "type": "string",
                "description": "City or metro filter, e.g. 'Los Angeles'. Omit if not given.",
            },
            "count": {
                "type": "integer",
                "description": "How many contacts the user asked for (required by the count rule).",
            },
        },
        "required": ["count"],
    },
}

GET_COMPANY_INTEL_TOOL: Dict[str, Any] = {
    "name": "get_company_intel",
    "description": (
        "Read-only, free. Live research on one NAMED company: overview, "
        "recent news, recruiting signals, divisions, and how many alumni "
        "from the user's school work there (pass user_school when known "
        "from context). Use it when the user asks about a specific firm "
        "('tell me about Jane Street', 'is Databricks hiring new grads?') "
        "and answer IN THE CHAT with the specifics. For discovering "
        "MULTIPLE companies matching criteria, call discover_companies "
        "instead."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "company": {
                "type": "string",
                "description": "The company to research.",
            },
            "user_school": {
                "type": "string",
                "description": "The user's school, when known, for alumni density.",
            },
            "career_field": {
                "type": "string",
                "description": "Career field filter for the alumni count, e.g. 'investment banking'.",
            },
        },
        "required": ["company"],
    },
}

DISCOVER_COMPANIES_TOOL: Dict[str, Any] = {
    "name": "discover_companies",
    "description": (
        "EXECUTE ACTION - runs a live company discovery search (the same "
        "engine as the Companies tab), saves it to the user's firm search "
        "history, and spends 2 credits per company returned. Use this when "
        "the user asks to FIND or LIST multiple companies matching criteria "
        "('find 10 smaller telecom startups on the west coast', 'list "
        "boutique healthcare banks in Chicago') - pass their full ask as "
        "query; the engine parses industry, size, and location from natural "
        "language. Surface the results IN THE CHAT: each company's name, "
        "industry, location, and size. COUNT IS REQUIRED: if the user gave "
        "no number, clarify once for it before calling (this search spends "
        "credits per company). Navigate to /find?tab=companies only when "
        "the user wants to browse or refine filters themselves, not when "
        "they asked for companies. For researching ONE named company, use "
        "get_company_intel instead. Error codes: COUNT_REQUIRED -> the user "
        "never said how many, ask them for a count (once) and call again "
        "next turn; INSUFFICIENT_CREDITS -> say what the search costs vs "
        "their balance; a zero-count result means nothing matched - say so "
        "and suggest broadening the criteria, never pretend."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": (
                    "The user's company ask in natural language, e.g. "
                    "'smaller telecom startups on the west coast'."
                ),
            },
            "count": {
                "type": "integer",
                "description": "How many companies the user asked for (required by the count rule).",
            },
        },
        "required": ["query", "count"],
    },
}

RUN_MEETING_PREP_TOOL: Dict[str, Any] = {
    "name": "run_meeting_prep",
    "description": (
        "EXECUTE ACTION - starts a real meeting prep (research packet + PDF) "
        "for a person and spends 30 of the user's credits. Use this when the "
        "user asks to be prepped for a meeting, call, or coffee chat with a "
        "NAMED person ('prep me for my call with Veronica Wittig'). An "
        "explicit ask like that IS consent: run it immediately, do not ask "
        "first and do not navigate to the meeting prep page instead. Pass "
        "contact_name; the person's LinkedIn URL is resolved automatically "
        "from the user's saved contacts. Pass linkedin_url ONLY when the "
        "user pasted one in chat. NEVER ask for a LinkedIn URL up front: "
        "ask only after this tool returns CONTACT_NOT_FOUND or NO_LINKEDIN. "
        "On started=true your answer MUST say the prep for <contact_name> "
        "is running, takes about a minute, and will appear right here in "
        "the chat with the PDF when done. NEVER say the prep is ready or "
        "describe its contents - it has not finished. Error codes: "
        "CONTACT_NOT_FOUND / NO_LINKEDIN -> ask for the person's LinkedIn "
        "URL (once); INSUFFICIENT_CREDITS -> say how many credits are "
        "needed vs available; LIMIT_REACHED -> their plan's meeting prep "
        "limit is used up, cta to /pricing; NEEDS_RESUME -> they must "
        "upload a resume in Account Settings first; PDL_OUTAGE -> the data "
        "provider is temporarily down, try later."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "contact_name": {
                "type": "string",
                "description": "The person's name exactly as the user said it.",
            },
            "linkedin_url": {
                "type": "string",
                "description": "Their LinkedIn URL, ONLY if the user pasted one.",
            },
        },
        "required": ["contact_name"],
    },
}

FIND_HIRING_MANAGERS_TOOL: Dict[str, Any] = {
    "name": "find_hiring_managers",
    "description": (
        "EXECUTE ACTION - finds hiring managers / recruiters for a role at "
        "a NAMED company and saves them to the user's Hiring Manager "
        "tracker. Costs 5 credits per manager found (default 3, max 5 - "
        "honor a count the user gives). Use for 'who's the hiring manager "
        "for X at Y', 'find recruiters at Stripe for the PM role'. "
        "MANDATORY output shape: your answer MUST list every returned "
        "manager as 'Name - Title - email' (one per line, in the order the "
        "tool returned them). 'Open your Hiring Managers' chip is IN "
        "ADDITION to the inline list, not a substitute. Saved managers are "
        "browsed at /my-network/managers; navigate to "
        "/find?tab=hiring-managers only to run a new search by hand. A zero "
        "result means none were found - say so honestly. "
        "INSUFFICIENT_CREDITS -> state cost vs balance."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "company": {"type": "string", "description": "Target company name."},
            "job_title": {"type": "string", "description": "The role they're recruiting for, when given."},
            "location": {"type": "string", "description": "Office/location, when given."},
            "count": {"type": "integer", "description": "How many managers (default 3, max 5)."},
        },
        "required": ["company"],
    },
}

GENERATE_COVER_LETTER_TOOL: Dict[str, Any] = {
    "name": "generate_cover_letter",
    "description": (
        "EXECUTE ACTION - writes a personalized cover letter for one "
        "specific job using the user's stored resume. Costs 5 credits. "
        "Job context, best first: job_id from a find_jobs result in this "
        "conversation; else job_url the user pasted; else job_title + "
        "company + job_description they provided. After calling, put the "
        "FULL letter text in your answer so the user can read and copy it. "
        "NEEDS_JOB_DESCRIPTION -> ask for the posting URL or description "
        "(once); NEEDS_RESUME -> they must upload a resume in Account "
        "Settings; INSUFFICIENT_CREDITS -> state cost vs balance. On "
        "generation failure credits auto-refund - say so."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "description": "job_id from a find_jobs result in this chat."},
            "job_url": {"type": "string", "description": "Posting URL the user pasted."},
            "job_title": {"type": "string", "description": "Role title, when given."},
            "company": {"type": "string", "description": "Company, when given."},
            "job_description": {"type": "string", "description": "Posting text the user pasted."},
        },
        "required": [],
    },
}

TAILOR_RESUME_TOOL: Dict[str, Any] = {
    "name": "tailor_resume_to_job",
    "description": (
        "Free. Scores the user's stored resume against one specific job "
        "and returns concrete edit suggestions. Use for 'how does my "
        "resume stack up against this role', 'tailor my resume to the "
        "Stripe PM job'. Job context resolution is the same as "
        "generate_cover_letter (job_id from this chat's find_jobs results, "
        "else job_url, else pasted description). When multiple postings "
        "match the company (e.g. two Databricks internships), pick the "
        "first eligible match and run — DO NOT ask the user to choose "
        "between roles after already calling this tool. MANDATORY output "
        "shape: your answer MUST inline the fit score / verdict, the top "
        "strengths, the top gaps, and every suggested edit as 'what it "
        "says now -> what to write instead'. Never end with 'which role "
        "did you mean?' after the tool has already returned data. "
        "NEEDS_JOB_DESCRIPTION -> ask once for the posting URL or text; "
        "NEEDS_RESUME -> point to Account Settings."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "description": "job_id from a find_jobs result in this chat."},
            "job_url": {"type": "string", "description": "Posting URL the user pasted."},
            "job_title": {"type": "string", "description": "Role title, when given."},
            "company": {"type": "string", "description": "Company, when given."},
            "job_description": {"type": "string", "description": "Posting text the user pasted."},
        },
        "required": [],
    },
}

# Terminal tools end a turn (exactly one per turn). Helper tools gather data
# or write memory mid-turn and the model keeps going. parallel_tool_calls=False
# caps each step at one tool; the caller offers only terminal tools on the
# final step so a turn can never end without one.
TERMINAL_TOOLS: List[Dict[str, Any]] = [NAVIGATE_TOOL, ANSWER_TOOL, CLARIFY_TOOL]
HELPER_TOOLS: List[Dict[str, Any]] = [
    PARSE_JOB_URL_TOOL,
    SAVE_STRATEGY_TOOL,
    UPDATE_STRATEGY_PROGRESS_TOOL,
    GET_OUTBOX_STATUS_TOOL,
    GET_RECENT_SEARCHES_TOOL,
    GET_RECENT_COVER_LETTERS_TOOL,
    GET_MEETING_PREP_DRAFTS_TOOL,
    GET_RECENT_FIRM_SEARCHES_TOOL,
    GET_APPLICATIONS_STATUS_TOOL,
    GET_LOOPS_STATUS_TOOL,
    FIND_JOBS_TOOL,
    AUTO_APPLY_TOOL,
    DRAFT_OUTREACH_EMAILS_TOOL,
    RUN_MEETING_PREP_TOOL,
    FIND_CONTACTS_TOOL,
    GET_COMPANY_INTEL_TOOL,
    DISCOVER_COMPANIES_TOOL,
    FIND_HIRING_MANAGERS_TOOL,
    GENERATE_COVER_LETTER_TOOL,
    TAILOR_RESUME_TOOL,
]
SCOUT_TOOLS: List[Dict[str, Any]] = TERMINAL_TOOLS + HELPER_TOOLS

TERMINAL_TOOL_NAMES = {t["name"] for t in TERMINAL_TOOLS}
HELPER_TOOL_NAMES = {t["name"] for t in HELPER_TOOLS}
TOOL_NAMES = {t["name"] for t in SCOUT_TOOLS}


def to_openai_tools(terminal_only: bool = False) -> List[Dict[str, Any]]:
    """Tool set in OpenAI function-tool format.

    terminal_only=True returns just navigate/answer/clarify; the caller uses it
    on the final step to force the turn to end on a terminal tool.
    """
    tools = TERMINAL_TOOLS if terminal_only else SCOUT_TOOLS
    return [
        {
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t["description"],
                "parameters": t["input_schema"],
            },
        }
        for t in tools
    ]


# ---------------------------------------------------------------------------
# Helper tool implementations
# ---------------------------------------------------------------------------


async def parse_job_url(url: str) -> Dict[str, Any]:
    """Fetch a job posting and return {company, job_title, location}.

    Thin wrapper over firecrawl_client.extract_job_posting. Returns empty
    strings on any failure (no API key, fetch error, unparseable page) so the
    model can still navigate without prefill or ask the user.
    """
    cleaned = (url or "").strip()
    if not cleaned:
        return {"company": "", "job_title": "", "location": "", "error": "no url provided"}
    try:
        from app.services.firecrawl_client import extract_job_posting
        # extract_job_posting is synchronous; keep it off the event loop.
        data = await asyncio.to_thread(extract_job_posting, cleaned)
    except Exception as e:  # any failure degrades to empty fields
        return {"company": "", "job_title": "", "location": "", "error": str(e)}
    data = data if isinstance(data, dict) else {}
    return {
        "company": str(data.get("company") or "").strip(),
        "job_title": str(data.get("title") or "").strip(),
        "location": str(data.get("location") or "").strip(),
    }


async def _run_save_strategy(
    args: Dict[str, Any], context: Dict[str, Any]
) -> Dict[str, Any]:
    """Helper-tool wrapper around strategy.save_strategy.

    Marks context["strategy_touched"] = True on success so handle_chat knows
    this turn wrote the strategy and can skip caching the answer.
    """
    from app.services.scout import strategy as strategy_mod
    uid = context.get("uid")
    tier = context.get("tier")
    if not uid:
        return {"ok": False, "error": "not_signed_in"}
    result = await asyncio.to_thread(
        strategy_mod.save_strategy,
        uid, tier, args.get("goal"), args.get("steps"),
    )
    if result.get("ok"):
        context["strategy_touched"] = True
    return result


async def _run_update_strategy_progress(
    args: Dict[str, Any], context: Dict[str, Any]
) -> Dict[str, Any]:
    """Helper-tool wrapper around strategy.update_strategy_progress."""
    from app.services.scout import strategy as strategy_mod
    uid = context.get("uid")
    tier = context.get("tier")
    if not uid:
        return {"ok": False, "error": "not_signed_in"}
    result = await asyncio.to_thread(
        strategy_mod.update_strategy_progress,
        uid, tier, args.get("completed_steps"), args.get("close"),
    )
    if result.get("ok"):
        context["strategy_touched"] = True
    return result


async def _run_workflow_read(
    fn_name: str,
    args: Dict[str, Any],
    context: Dict[str, Any],
    default_limit: int,
    empty_envelope: Dict[str, Any],
) -> Dict[str, Any]:
    """Shared body for the six workflow-state read tools.

    Resolves uid, normalizes the limit, dispatches to the named function in
    workflow_state, and marks context["workflow_state_touched"] so handle_chat
    refuses to cache an answer colored by user-specific workflow state.
    """
    from app.services.scout import workflow_state
    uid = context.get("uid")
    if not uid:
        return dict(empty_envelope)
    try:
        limit = int(args.get("limit") or default_limit)
    except (TypeError, ValueError):
        limit = default_limit
    fn = getattr(workflow_state, fn_name, None)
    if fn is None:
        return dict(empty_envelope)
    result = await asyncio.to_thread(fn, uid, limit)
    context["workflow_state_touched"] = True
    return result


# Per-tool defaults and empty envelopes. Kept here (not imported from
# workflow_state) so the dispatch table stays self-contained.
_OUTBOX_EMPTY = {"total_contacts": 0, "awaiting_reply": 0, "replied": 0, "recent": []}
_LIST_EMPTY = {"count": 0, "recent": []}
_APPLICATIONS_EMPTY = {
    "total": 0, "submitted": 0, "in_flight": 0, "needs_answers": 0,
    "finish_in_browser": 0, "failed": 0, "recent": [],
}
_LOOPS_EMPTY = {
    "count": 0, "running": 0, "paused": 0,
    "pending_drafts": 0, "unread_replies": 0, "loops": [],
}


async def run_helper_tool(
    name: str,
    args: Dict[str, Any],
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Execute a helper (non-terminal) tool by name and return its result.

    The optional context carries per-turn state the helper may need (uid,
    tier, and the strategy_touched / workflow_state_touched flags the
    write-side and read-side helpers set). Older helpers (parse_job_url)
    ignore context, so it stays optional.
    """
    args = args if isinstance(args, dict) else {}
    ctx = context if context is not None else {}
    if name == "parse_job_url":
        return await parse_job_url(str(args.get("url") or ""))
    if name == "save_strategy":
        return await _run_save_strategy(args, ctx)
    if name == "update_strategy_progress":
        return await _run_update_strategy_progress(args, ctx)
    if name == "get_outbox_status":
        return await _run_workflow_read(
            "get_outbox_status", args, ctx, 10, _OUTBOX_EMPTY)
    if name == "get_recent_searches":
        return await _run_workflow_read(
            "get_recent_searches", args, ctx, 5, _LIST_EMPTY)
    if name == "get_recent_cover_letters":
        return await _run_workflow_read(
            "get_recent_cover_letters", args, ctx, 5, _LIST_EMPTY)
    if name == "get_meeting_prep_drafts":
        return await _run_workflow_read(
            "get_meeting_prep_drafts", args, ctx, 5, _LIST_EMPTY)
    if name == "get_recent_firm_searches":
        return await _run_workflow_read(
            "get_recent_firm_searches", args, ctx, 5, _LIST_EMPTY)
    if name == "get_applications_status":
        return await _run_workflow_read(
            "get_applications_status", args, ctx, 8, _APPLICATIONS_EMPTY)
    if name == "get_loops_status":
        return await _run_workflow_read(
            "get_loops_status", args, ctx, 6, _LOOPS_EMPTY)
    if name == "find_jobs":
        return await asyncio.to_thread(
            _find_jobs, str(args.get("query") or ""), args.get("limit"))
    if name == "auto_apply_to_job":
        return await _run_auto_apply(args, ctx)
    if name == "draft_outreach_emails":
        return await _run_draft_outreach(args, ctx)
    if name == "run_meeting_prep":
        return await _run_meeting_prep(args, ctx)
    if name == "find_contacts":
        return await _run_find_contacts(args, ctx)
    if name == "get_company_intel":
        return await _run_company_intel(args, ctx)
    if name == "discover_companies":
        return await _run_discover_companies(args, ctx)
    if name == "find_hiring_managers":
        return await _run_find_hiring_managers(args, ctx)
    if name == "generate_cover_letter":
        return await _run_generate_cover_letter(args, ctx)
    if name == "tailor_resume_to_job":
        return await _run_tailor_resume(args, ctx)
    return {"error": f"unknown helper tool: {name}"}


def _job_context_args(args: Dict[str, Any]) -> Dict[str, str]:
    """Shared kwargs for the job-context tools (cover letter, tailoring)."""
    return {
        "job_id": str(args.get("job_id") or ""),
        "job_url": str(args.get("job_url") or ""),
        "job_title": str(args.get("job_title") or ""),
        "company": str(args.get("company") or ""),
        "job_description": str(args.get("job_description") or ""),
    }


async def _run_find_hiring_managers(args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """Hiring manager discovery saved to the tracker. Marks
    workflow_state_touched so the turn is never cached or served cross-user."""
    uid = context.get("uid")
    if not uid:
        return {"count": 0, "managers": [],
                "error": "sign in required", "code": "AUTH_REQUIRED"}
    if not _user_authorized(context, _HM_KEYWORDS):
        return {"count": 0, "managers": [],
                **_consent_refusal("a hiring manager search")}
    try:
        from app.services.scout.job_actions import find_hiring_managers_for_chat
        result = await asyncio.to_thread(
            find_hiring_managers_for_chat,
            uid,
            str(args.get("company") or ""),
            str(args.get("job_title") or ""),
            str(args.get("location") or ""),
            args.get("count") or 3,
        )
        context["workflow_state_touched"] = True
        return result
    except Exception as e:
        print(f"[ScoutTools] find_hiring_managers failed: {e}")
        return {"count": 0, "managers": [],
                "error": "hiring manager search failed", "code": "INTERNAL"}


async def _run_generate_cover_letter(args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    uid = context.get("uid")
    if not uid:
        return {"error": "sign in required", "code": "AUTH_REQUIRED"}
    if not _user_authorized(context, _COVER_LETTER_KEYWORDS):
        return _consent_refusal("a cover letter")
    try:
        from app.services.scout.job_actions import cover_letter_for_chat
        result = await cover_letter_for_chat(uid, **_job_context_args(args))
        context["workflow_state_touched"] = True
        return result
    except Exception as e:
        print(f"[ScoutTools] generate_cover_letter failed: {e}")
        return {"error": "cover letter generation failed", "code": "INTERNAL"}


async def _run_tailor_resume(args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    uid = context.get("uid")
    if not uid:
        return {"error": "sign in required", "code": "AUTH_REQUIRED"}
    try:
        from app.services.scout.job_actions import tailor_resume_for_chat
        result = await asyncio.to_thread(
            tailor_resume_for_chat, uid, **_job_context_args(args))
        context["workflow_state_touched"] = True
        return result
    except Exception as e:
        print(f"[ScoutTools] tailor_resume_to_job failed: {e}")
        return {"error": "resume analysis failed", "code": "INTERNAL"}


# ---------------------------------------------------------------------------
# Consent gates. Execute tools spend credits or write to the user's Gmail /
# trackers, so the ASK must come from the user, not the model: gpt-5-mini
# once answered a bare count reply by running the pending search AND an
# unrequested draft AND an unrequested 30-credit meeting prep. Each execute
# tool requires one of its trigger words in the user's recent messages, or
# an affirmation directly answering an assistant message that offered it
# ("Want me to draft emails?" -> "yes").
# ---------------------------------------------------------------------------

_AFFIRMATION_RE = re.compile(
    r"^(yes|yeah|yep|yup|sure|ok|okay|please do|do it|go ahead|go for it"
    r"|sounds good|yes please|do that|please)\b",
    re.I,
)


def _user_authorized(context: Dict[str, Any], keywords: tuple) -> bool:
    """True when the user's own words ask for this workflow.

    Empty recent_user_text (direct/test callers without chat context) passes:
    the gate protects the chat loop, not the API surface.
    """
    recent_user = str(context.get("recent_user_text") or "")
    if not recent_user.strip():
        return True
    lowered = recent_user.lower()
    if any(k in lowered for k in keywords):
        return True
    last_assistant = str(context.get("last_assistant_text") or "").lower()
    current = str(context.get("user_message") or "").strip()
    if any(k in last_assistant for k in keywords) and _AFFIRMATION_RE.match(current):
        return True
    return False


def _consent_refusal(workflow: str) -> Dict[str, Any]:
    return {
        "error": (
            f"the user has not asked for {workflow} in this conversation; "
            "do not run it - if it would help, offer it and wait for them "
            "to say yes"
        ),
        "code": "CONSENT_REQUIRED",
    }


_DRAFT_KEYWORDS = ("draft", "email", "e-mail", "outreach", "write to",
                   "reach out", "message them", "send", "follow up")
_PREP_KEYWORDS = ("prep", "meeting", "call", "coffee", "interview", "meet")
_APPLY_KEYWORDS = ("apply", "application")
_HM_KEYWORDS = ("hiring manager", "recruiter", "hiring", "manager")
_COVER_LETTER_KEYWORDS = ("cover letter", "coverletter", "letter")


# A people search spends credits per contact, so the count must come from
# the user, not the model. Digits (but not years), small number words, and
# "a couple / a few" all count as the user naming a quantity.
_COUNT_TOKEN_RE = re.compile(
    r"\b(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten"
    r"|a couple|couple|a few|few|all|every|each)\b",
    re.I,
)


async def _run_find_contacts(args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """Live people search saved to My Network. Marks workflow_state_touched
    so the turn is never cached or served cross-user."""
    uid = context.get("uid")
    if not uid:
        return {"count": 0, "contacts": [],
                "error": "sign in required", "code": "AUTH_REQUIRED"}
    # Harness-enforced count rule: gpt-5-mini invents a count when the user
    # gave none, silently spending credits. If the triggering message names
    # no quantity, refuse and make the model ask (one clarify, ever).
    user_message = str(context.get("user_message") or "")
    if user_message and not _COUNT_TOKEN_RE.search(user_message):
        return {
            "count": 0, "contacts": [],
            "error": ("the user has not said how many contacts to pull; "
                      "this search costs 5 credits per contact, so ask them "
                      "for a count before searching"),
            "code": "COUNT_REQUIRED",
        }
    try:
        from app.services.scout.contact_actions import find_contacts_for_chat
        result = await asyncio.to_thread(
            find_contacts_for_chat,
            uid,
            context.get("tier"),
            str(args.get("company") or ""),
            str(args.get("role") or ""),
            str(args.get("school") or ""),
            str(args.get("location") or ""),
            args.get("count") or 5,
        )
        context["workflow_state_touched"] = True
        return result
    except Exception as e:
        print(f"[ScoutTools] find_contacts failed: {e}")
        return {"count": 0, "contacts": [],
                "error": "contact search failed", "code": "INTERNAL"}


async def _run_discover_companies(args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """Multi-company discovery saved to firm search history. Marks
    workflow_state_touched so the turn is never cached or served cross-user.

    Same harness-enforced count rule as find_contacts: the search spends
    credits per company, so the quantity must come from the user."""
    uid = context.get("uid")
    if not uid:
        return {"count": 0, "companies": [],
                "error": "sign in required", "code": "AUTH_REQUIRED"}
    user_message = str(context.get("user_message") or "")
    if user_message and not _COUNT_TOKEN_RE.search(user_message):
        return {
            "count": 0, "companies": [],
            "error": ("the user has not said how many companies to find; "
                      "this search costs 2 credits per company, so ask them "
                      "for a count before searching"),
            "code": "COUNT_REQUIRED",
        }
    try:
        from app.services.scout.company_actions import discover_companies_for_chat
        result = await asyncio.to_thread(
            discover_companies_for_chat,
            uid,
            context.get("tier"),
            str(args.get("query") or ""),
            args.get("count") or 10,
        )
        context["workflow_state_touched"] = True
        return result
    except Exception as e:
        print(f"[ScoutTools] discover_companies failed: {e}")
        return {"count": 0, "companies": [],
                "error": "company discovery failed", "code": "INTERNAL"}


async def _run_company_intel(args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """Company research in chat. Marks workflow_state_touched: the alumni
    density is grounded in the asking user's school."""
    try:
        from app.services.scout.contact_actions import company_intel_for_chat
        result = await asyncio.to_thread(
            company_intel_for_chat,
            context.get("uid") or "",
            context.get("tier"),
            str(args.get("company") or ""),
            str(args.get("user_school") or ""),
            str(args.get("career_field") or ""),
        )
        context["workflow_state_touched"] = True
        return result
    except Exception as e:
        print(f"[ScoutTools] get_company_intel failed: {e}")
        return {"error": "company research failed", "code": "INTERNAL"}


async def _run_meeting_prep(args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """Start a meeting prep job for a saved contact. Marks
    workflow_state_touched so the turn is never cached or served cross-user."""
    uid = context.get("uid")
    if not uid:
        return {"started": False, "error": "sign in required", "code": "AUTH_REQUIRED"}
    if not _user_authorized(context, _PREP_KEYWORDS):
        return {"started": False, **_consent_refusal("a meeting prep")}
    try:
        from app.services.scout.prep_actions import start_meeting_prep
        result = await asyncio.to_thread(
            start_meeting_prep,
            uid,
            str(args.get("contact_name") or ""),
            str(args.get("linkedin_url") or ""),
        )
        context["workflow_state_touched"] = True
        return result
    except Exception as e:
        print(f"[ScoutTools] run_meeting_prep failed: {e}")
        return {"started": False, "error": "prep failed to start", "code": "INTERNAL"}


async def _run_draft_outreach(args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """Draft Gmail outreach for saved contacts. Marks workflow_state_touched
    so the turn is never cached or served cross-user."""
    uid = context.get("uid")
    if not uid:
        return {"drafted": [], "skipped": [], "count": 0,
                "error": "sign in required", "code": "AUTH_REQUIRED"}
    if not _user_authorized(context, _DRAFT_KEYWORDS):
        return {"drafted": [], "skipped": [], "count": 0,
                **_consent_refusal("email drafts")}
    names = args.get("contact_names")
    if not isinstance(names, list):
        names = None
    try:
        from app.services.scout.outreach_actions import draft_emails_to_contacts
        result = await asyncio.to_thread(
            draft_emails_to_contacts, uid, names, args.get("limit") or 4,
        )
        context["workflow_state_touched"] = True
        return result
    except Exception as e:
        print(f"[ScoutTools] draft_outreach_emails failed: {e}")
        return {"drafted": [], "skipped": [], "count": 0,
                "error": "drafting failed", "code": "INTERNAL"}


def job_rank_key(job: Dict[str, Any]):
    """Sort key for Scout job results — quality first, applyability last.

    Ascending sort. Order of precedence:
      1. `_score`  — how many query tokens matched (more is better)
      2. `_tier`   — relevance_tier, 1 (early-career) beats 3
      3. auto-apply eligibility — a pure tiebreaker between otherwise
         equally good jobs.

    Eligibility must never remove a job from the results. Scout previously
    hard-filtered to auto-apply-eligible jobs, which hid every Workday,
    Oracle, Oleeo and Goldman posting at a target employer unless there
    happened to be no eligible match at all.
    """
    return (
        -(job.get("_score") or 0),
        int(job.get("_tier") or 3),
        not job.get("auto_apply_eligible"),
    )


def _job_text(value: Any) -> str:
    """Coerce a job-doc field to display text. Some sources store location or
    company as dicts; slicing those raised KeyError(slice(...)) and killed
    the whole search."""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("name", "display", "label", "city", "text"):
            v = value.get(key)
            if isinstance(v, str) and v.strip():
                return v
        return ""
    return str(value) if value is not None else ""


def _find_jobs(query: str, limit: Any = None) -> Dict[str, Any]:
    """Lean catalog search for Scout: token filter + recency, best-effort.

    Mirrors the /api/jobs/search primary-token strategy without the cursor /
    post-filter machinery: one array_contains on the rarest token, ordered by
    posted_at desc, annotated with auto-apply eligibility.
    """
    try:
        limit = max(1, min(int(limit or 5), 10))
    except (TypeError, ValueError):
        limit = 5
    query = (query or "").strip()
    if not query:
        return {"count": 0, "jobs": [], "error": "query required"}
    try:
        from app.extensions import get_db
        from app.services.auto_apply.ats_detector import is_eligible
        from backend.pipeline.normalizer import build_search_terms
        from google.cloud.firestore_v1.base_query import FieldFilter

        db = get_db()
        tokens = build_search_terms(query, None, None) or []
        # Try up to the two longest tokens as the Firestore filter: a stuffed
        # or unlucky primary ("angeles") should not zero out the whole search.
        primaries = sorted(set(tokens), key=len, reverse=True)[:2] or [None]
        candidates: Dict[str, Dict[str, Any]] = {}
        for primary in primaries:
            q = db.collection("jobs").order_by("posted_at", direction="DESCENDING")
            if primary:
                q = q.where(filter=FieldFilter("search_terms", "array_contains", primary))
            for snap in q.limit(150).stream():
                if snap.id in candidates:
                    continue
                d = snap.to_dict() or {}
                title = _job_text(d.get("title"))
                company = _job_text(d.get("company"))
                loc = _job_text(d.get("location"))
                haystack = f"{title} {company} {loc}".lower()
                # Rank by how many query tokens land; never hard-require all
                # of them (an extra word like a city must not zero the search).
                score = sum(1 for t in tokens if t in haystack)
                candidates[snap.id] = {
                    "job_id": snap.id,
                    "title": title[:160],
                    "company": company[:120],
                    "location": loc[:120],
                    "auto_apply_eligible": bool(is_eligible(d)),
                    "apply_url": _job_text(d.get("apply_url") or d.get("url") or "")[:2000],
                    "_score": score,
                    "_tier": int(d.get("relevance_tier") or 3),
                }
            if candidates:
                break
        # Rank on quality, not on which ATS the job happens to sit on:
        # query-token match first, then how early-career the posting is
        # (relevance_tier 1 beats 3), and only as a final tiebreaker prefer
        # jobs Scout can submit itself. This used to hard-filter to eligible
        # jobs, which made every Workday / Oracle / Oleeo / Goldman posting
        # at a target employer invisible in Scout unless there was no
        # eligible match at all — we were ingesting that inventory and then
        # hiding it. Auto-apply is a tiebreaker, never a filter.
        shown = sorted(candidates.values(), key=job_rank_key)[:limit]
        _ineligible = sum(1 for j in shown if not j["auto_apply_eligible"])
        if _ineligible:
            print(f"[ScoutTools] find_jobs: {_ineligible}/{len(shown)} results "
                  f"are not auto-apply-eligible (previously hidden)")
        jobs = [{k: v for k, v in j.items() if not k.startswith("_")} for j in shown]
        # query rides along so the harness can prefill the Job Board chip.
        return {"count": len(jobs), "jobs": jobs, "query": query}
    except Exception as e:
        print(f"[ScoutTools] find_jobs failed: {e}")
        return {"count": 0, "jobs": [], "error": "job search unavailable"}


async def _run_auto_apply(args: Dict[str, Any], context: Dict[str, Any]) -> Dict[str, Any]:
    """Execute one auto-apply submission on the user's behalf.

    Tier-gated to Pro/Elite (mirrors the HTTP route's @require_tier). Marks
    workflow_state_touched so the turn is never served from or promoted to
    the shared answer cache.
    """
    uid = context.get("uid")
    if not uid:
        return {"error": "sign in required", "code": "AUTH_REQUIRED"}
    tier = str(context.get("tier") or "free").lower()
    if tier not in ("pro", "elite"):
        return {
            "error": "auto-apply requires Pro or Elite",
            "code": "TIER_REQUIRED",
        }
    if not _user_authorized(context, _APPLY_KEYWORDS):
        return _consent_refusal("an auto-apply submission")
    job_id = str(args.get("job_id") or "").strip()
    if not job_id:
        return {"error": "job_id required", "code": "BAD_REQUEST"}
    try:
        from app.services.auto_apply.submit_service import submit_auto_apply_for_user
        payload, _status = await asyncio.to_thread(
            submit_auto_apply_for_user, uid, job_id, dry_run=False,
        )
        context["workflow_state_touched"] = True
        return payload
    except Exception as e:
        print(f"[ScoutTools] auto_apply_to_job failed: {e}")
        return {"error": "auto-apply failed to start", "code": "INTERNAL"}
