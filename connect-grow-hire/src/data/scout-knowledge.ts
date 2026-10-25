/**
 * Scout Knowledge Base - Comprehensive product knowledge for Scout AI Assistant
 * 
 * This file contains all the information Scout needs to help users navigate
 * and understand Offerloop.
 */

// ============================================================================
// PAGE DIRECTORY - Every page in the app
// ============================================================================

export const PAGES = {
  contactSearch: {
    route: "/find",
    name: "Contact Search",
    description: "Find professionals at companies to network with. Enter job title, company, and location to discover contacts and automatically generate personalized outreach emails.",
    tabs: ["Contact Search", "Contact Library", "Import Contacts"],
    features: [
      "Search by job title, company, and location",
      "AI-generated personalized email drafts",
      "Gmail integration for sending emails",
      "Save contacts to your library",
      "Import contacts from CSV or manually",
      "Resume matching for better personalization (Pro+)"
    ],
    creditCost: "10 credits per contact",
    keywords: ["contact", "search", "find", "people", "professionals", "network", "networking", "email", "outreach", "connect"]
  },

  jobBoard: {
    route: "/job-board",
    name: "Job Board",
    description: "Browse job listings tailored to your profile. Optimize your resume for specific jobs, generate cover letters, and find recruiters.",
    tabs: ["Job Listings", "Saved Jobs"],
    features: [
      "AI-curated job listings",
      "Resume optimization for specific jobs",
      "Cover letter generation",
      "Find recruiters at target companies",
      "Save jobs for later",
      "Match score based on your resume"
    ],
    creditCost: "Varies by action - resume optimization and cover letters use credits",
    keywords: ["job", "jobs", "listings", "board", "openings", "positions", "career", "resume", "cover letter", "recruiter"]
  },

  contacts: {
    route: "/contacts",
    name: "Contacts",
    description: "Everyone you've saved, in one place. Pick a contact to see their details, your email conversation and its pipeline stage, draft or send a reply, and run a meeting prep before you talk. Switch to Spreadsheet view to see everyone in a table.",
    features: [
      "Search, sort, and group contacts by company",
      "See the email conversation and pipeline stage for each person",
      "Generate and send replies through Gmail",
      "Run a meeting prep from a contact's card (needs their LinkedIn URL)",
      "Past preps list with PDF downloads",
      "Add, edit, share, and export contacts to CSV"
    ],
    creditCost: "30 credits per meeting prep",
    keywords: ["contacts", "my network", "inbox", "outbox", "replies", "emails", "drafts", "coffee chat", "meeting prep", "prep", "follow up", "pipeline"]
  },

  pricing: {
    route: "/pricing",
    name: "Pricing",
    description: "View and manage your subscription. Compare Free, Pro, and Elite plans.",
    features: [
      "Compare plan features",
      "Upgrade or downgrade subscription",
      "Manage billing through Stripe",
      "View current plan and credits"
    ],
    keywords: ["pricing", "plans", "subscription", "upgrade", "pro", "elite", "free", "billing", "credits", "cost", "payment"]
  },

  accountSettings: {
    route: "/account-settings",
    name: "Account Settings",
    description: "Manage your profile, upload resume, connect Gmail, and update preferences.",
    features: [
      "Update profile information",
      "Upload and manage resume",
      "Connect Gmail for email drafts",
      "View and update preferences",
      "Sign out"
    ],
    keywords: ["settings", "account", "profile", "resume", "gmail", "connect", "preferences", "upload"]
  }
} as const;


// ============================================================================
// FEATURES DOCUMENTATION
// ============================================================================

export const FEATURES = {
  contactSearch: {
    name: "Contact Search",
    whatItDoes: "Finds professionals at companies based on job title, company, and location. Automatically generates personalized outreach emails using AI.",
    howToUse: [
      "Go to Contact Search from the sidebar",
      "Enter the job title you're targeting (e.g., 'Investment Banking Analyst')",
      "Enter the company name (e.g., 'Goldman Sachs')",
      "Enter the location (e.g., 'New York, NY')",
      "Select how many contacts you want (1-15 depending on your plan)",
      "Click Search to find contacts",
      "Review generated emails and save to Gmail drafts"
    ],
    tips: [
      "Be specific with job titles for better matches",
      "Connect Gmail first to save email drafts automatically",
      "Pro users can upload a resume for better email personalization"
    ],
    creditCost: 10,
    creditUnit: "per contact"
  },

  firmSearch: {
    name: "Firm Search",
    whatItDoes: "Discovers companies matching your criteria using natural language search. Great for finding employers you might not know about.",
    howToUse: [
      "Go to Firm Search from the sidebar",
      "Type a natural language query (e.g., 'Investment banks in NYC focused on healthcare M&A')",
      "Select batch size (how many firms to return)",
      "Click Search to find matching companies",
      "Save interesting firms to your list"
    ],
    tips: [
      "Use descriptive queries for better results",
      "Include industry, location, and size preferences",
      "Save firms before they disappear from results"
    ],
    creditCost: 5,
    creditUnit: "per firm"
  },

  coffeeChatPrep: {
    name: "Coffee Chat Prep",
    whatItDoes: "Generates comprehensive preparation materials for networking conversations. Includes talking points, questions to ask, and research on the person and their company.",
    howToUse: [
      "Go to Coffee Chat Prep from the sidebar",
      "Paste the LinkedIn URL of the person you're meeting",
      "Click Generate Prep",
      "Wait for the AI to research and compile materials (takes 1-2 minutes)",
      "Review the prep and download as PDF"
    ],
    tips: [
      "Generate prep a day before your meeting",
      "Review the similarity summary to find common ground",
      "Use the suggested questions during your conversation"
    ],
    creditCost: 15,
    creditUnit: "per prep"
  },

  gmailIntegration: {
    name: "Gmail Integration",
    whatItDoes: "Connects your Gmail account to save email drafts automatically. Emails generated by Contact Search are saved directly to your Gmail drafts.",
    howToUse: [
      "Go to Account Settings",
      "Click 'Connect Gmail'",
      "Sign in with your Google account",
      "Grant the required permissions",
      "Now emails will be saved as drafts in Gmail"
    ],
    tips: [
      "You must connect Gmail before using Contact Search for full functionality",
      "Emails are saved as drafts - you review and send them manually",
      "You can disconnect Gmail anytime in Account Settings"
    ]
  },

  resumeUpload: {
    name: "Resume Upload",
    whatItDoes: "Upload your resume for better email personalization and job matching. Pro and Elite users get resume-matched emails.",
    howToUse: [
      "Go to Account Settings",
      "Click 'Upload Resume'",
      "Select your PDF resume file",
      "Wait for parsing to complete",
      "Your resume is now used for personalization"
    ],
    tips: [
      "Use a clean, well-formatted PDF for best parsing",
      "Update your resume when you have new experiences",
      "Resume is used for Coffee Chat Prep similarity analysis too"
    ]
  }
} as const;


// ============================================================================
// STEP-BY-STEP WORKFLOWS
// ============================================================================

export const WORKFLOWS = {
  findingContacts: {
    name: "Finding Contacts to Network With",
    steps: [
      "Connect Gmail in Account Settings (if not done)",
      "Go to Contact Search",
      "Enter job title, company, and location",
      "Select batch size (1-15 contacts)",
      "Click Search",
      "Review the contacts and generated emails",
      "Emails are saved to Gmail drafts automatically",
      "Open Gmail, review, personalize if needed, and send"
    ]
  },

  connectingGmail: {
    name: "Connecting Gmail",
    steps: [
      "Click on Settings in the sidebar (or your profile)",
      "Go to Account Settings",
      "Find the Gmail section",
      "Click 'Connect Gmail'",
      "Sign in with your Google account",
      "Grant permissions for draft access",
      "You're connected! Emails will now save to drafts"
    ]
  },

  buyingCredits: {
    name: "Getting More Credits",
    steps: [
      "Go to Pricing from the sidebar",
      "Compare Free, Pro, and Elite plans",
      "Click 'Upgrade' on your chosen plan",
      "Complete payment through Stripe",
      "Credits are added immediately",
      "Your new plan limits are now active"
    ]
  },

  preparingForCoffeeChat: {
    name: "Preparing for a Coffee Chat",
    steps: [
      "Go to Coffee Chat Prep",
      "Find the LinkedIn URL of the person you're meeting",
      "Paste the URL and click Generate",
      "Wait 1-2 minutes for research to complete",
      "Review the prep materials",
      "Note the similarity points and suggested questions",
      "Download PDF to reference during the meeting"
    ]
  },

  optimizingResume: {
    name: "Optimizing Your Resume for a Job",
    steps: [
      "Go to Job Board and find a job you like",
      "Click 'Optimize Resume' on that job",
      "AI analyzes your resume against the job",
      "Review ATS score and suggestions",
      "Apply the recommended changes",
      "Download the optimized version"
    ]
  },

  findingRecruiters: {
    name: "Finding Recruiters for a Job",
    steps: [
      "Go to Job Board",
      "Find a job listing you're interested in",
      "Click 'Find Recruiter' button",
      "AI searches for recruiters at that company",
      "Review recruiter profiles and emails",
      "Emails are drafted and saved to Gmail"
    ]
  }
} as const;


// ============================================================================
// CREDIT SYSTEM
// ============================================================================

export const CREDIT_SYSTEM = {
  costs: {
    contactSearch: { amount: 10, unit: "per contact" },
    firmSearch: { amount: 10, unit: "per firm" },
    coffeeChatPrep: { amount: 30, unit: "per prep" },
    resumeOptimization: { amount: 40, unit: "per optimization" },
    coverLetter: { amount: 20, unit: "per letter" },
    recruiterSearch: { amount: 6, unit: "per search" }
  },

  tiers: {
    free: {
      name: "Free",
      price: "$0/month",
      credits: 300,
      maxCredits: 300,
      maxContactsPerSearch: 3,
      features: [
        "300 monthly credits",
        "Up to 3 contacts per search",
        "Basic email generation",
        "Coffee Chat Prep access",
        "Gmail integration"
      ],
      limitations: [
        "No resume-matched emails",
        "Basic personalization"
      ]
    },
    pro: {
      name: "Pro",
      price: "$9.99/month, or $4.99/month with a .edu email (50% student discount)",
      credits: 2000,
      maxCredits: 2000,
      maxContactsPerSearch: 8,
      features: [
        "2,000 monthly credits",
        "Up to 10 contacts per search",
        "Resume-matched personalized emails",
        "Priority support"
      ]
    },
    elite: {
      name: "Elite",
      price: "$34.99/month, or $17.49/month with a .edu email (50% student discount)",
      credits: 5000,
      maxCredits: 5000,
      maxContactsPerSearch: 15,
      features: [
        "3,000 monthly credits",
        "Up to 15 contacts per search",
        "All Pro features",
        "Unlimited Coffee Chat Prep",
        "Advanced analytics"
      ]
    }
  },

  howCreditsWork: [
    "Credits are your currency for using Offerloop features",
    "Each action costs a specific number of credits",
    "Credits reset monthly based on your subscription date",
    "Free tier: 300 credits/month",
    "Pro tier: 2,000 credits/month",
    "Elite tier: 5,000 credits/month",
    "Unused credits don't roll over",
    "You can see your remaining credits in the sidebar"
  ]
} as const;


// ============================================================================
// TROUBLESHOOTING
// ============================================================================

export const TROUBLESHOOTING = {
  gmailNotConnected: {
    issue: "Gmail not connected",
    symptoms: ["Emails not saving to drafts", "Connect Gmail button showing"],
    solution: [
      "Go to Account Settings",
      "Click 'Connect Gmail'",
      "Sign in with Google and grant permissions",
      "Try your search again"
    ]
  },

  outOfCredits: {
    issue: "Out of credits",
    symptoms: ["Searches failing", "'Insufficient credits' message", "Actions blocked"],
    solution: [
      "Check your credits in the sidebar",
      "Go to Pricing to upgrade your plan",
      "Credits reset monthly - check your reset date",
      "Pro ($9.99, or $4.99 with a .edu email) gives 2,000 credits, Elite ($34.99, or $17.49 with .edu) gives 5,000"
    ]
  },

  contactNotFound: {
    issue: "No contacts found",
    symptoms: ["Empty search results", "Few or no matches"],
    solution: [
      "Try broader job titles (e.g., 'Analyst' instead of 'Investment Banking Analyst')",
      "Check spelling of company name",
      "Try different locations or remove location filter",
      "Some smaller companies may have limited data"
    ]
  },

  emailNotPersonalized: {
    issue: "Emails seem generic",
    symptoms: ["Basic email templates", "No resume references"],
    solution: [
      "Make sure you've uploaded your resume in Account Settings",
      "Pro/Elite users get resume-matched personalization",
      "Complete your profile with career interests",
      "The more info you provide, the better personalization"
    ]
  },

  prepTakingTooLong: {
    issue: "Coffee Chat Prep taking too long",
    symptoms: ["Loading for more than 5 minutes", "Stuck on processing"],
    solution: [
      "Coffee Chat Prep usually takes 1-2 minutes",
      "If stuck longer, refresh and try again",
      "Check if the LinkedIn URL is accessible"
    ]
  },

  subscriptionIssues: {
    issue: "Subscription or billing problems",
    symptoms: ["Payment failed", "Plan not updating", "Credits not showing"],
    solution: [
      "Go to Pricing and click 'Manage Subscription'",
      "This opens Stripe where you can update payment",
      "If plan upgraded but credits not showing, refresh the page",
      "Contact support if issues persist"
    ]
  }
} as const;


// ============================================================================
// ROUTE MAPPING - For auto-navigation
// ============================================================================

export const ROUTE_MAPPING = {
  // Direct page routes
  pages: {
    "/find": ["contact search", "find contacts", "search contacts", "network", "networking", "outreach", "email"],
    "/job-board": ["job board", "jobs", "job listings", "openings", "positions", "resume", "cover letter", "recruiter"],
    "/pricing": ["pricing", "plans", "upgrade", "subscription", "pro", "elite", "credits", "billing", "payment"],
    "/account-settings": ["settings", "account", "profile", "gmail", "resume upload", "preferences"],
    "/contacts": ["contacts", "my network", "contact directory", "saved contacts", "my contacts", "people", "outbox", "inbox", "emails", "drafts", "sent", "replies", "messages", "coffee chat", "coffee prep", "meeting prep", "past preps"]
  },

  // Intent to route mapping
  intents: {
    findContacts: "/find",
    browseJobs: "/job-board",
    prepareCoffeeChat: "/contacts",
    viewPricing: "/pricing",
    manageAccount: "/account-settings",
    viewSavedContacts: "/contacts",
    viewEmails: "/contacts",
    connectGmail: "/account-settings",
    uploadResume: "/account-settings",
    checkCredits: "/pricing"
  }
} as const;


// ============================================================================
// SUGGESTED QUESTIONS
// ============================================================================

export const SUGGESTED_QUESTIONS = [
  "I want to find people to reach out to",
  "What do I get on each plan?",
  "Help me set up my Gmail",
  "What's the deal with credits?"
] as const;

/**
 * Context-aware suggestion chips by route. Fallback to SUGGESTED_QUESTIONS
 * for unmapped pages.
 *
 * Keys are pathnames. The panel looks up a tab-qualified key
 * ("pathname?tab=<tab>") first, then the bare pathname (see ScoutSidePanel).
 */
export const SCOUT_CHIPS_BY_PAGE: Record<string, readonly string[]> = {
  "/find": [
    "Help me narrow my search",
    "Find alumni from my school at my target firms",
    "I'm not getting good results",
    "How do credits work with searches?",
  ],
  "/job-board": [
    "Find contacts at these companies",
    "Which of these jobs fit my resume best?",
    "How does auto-apply work?",
    "Help me find similar jobs",
  ],
  "/applications": [
    "What's the status of my applications?",
    "What does 'needs your answers' mean?",
    "Why is an application stuck in the browser step?",
    "How does auto-apply work?",
  ],
  "/contacts": [
    "Who hasn't replied yet?",
    "Help me write a follow-up",
    "How do I reply to this thread?",
    "When should I follow up after no response?",
  ],
  "/agent": [
    "What does a Loop actually do?",
    "Set up a Loop for my target companies",
    "How many credits does a Loop use?",
    "Should I approve auto-sending?",
  ],
  "/upload-list": [
    "How should I format my CSV?",
    "What happens after I upload a list?",
    "Can I import LinkedIn URLs?",
    "Where do imported contacts go?",
  ],
  "/integrations": [
    "Why should I connect Gmail?",
    "Is it safe to connect my Gmail?",
    "My drafts aren't showing up in Gmail",
    "How do replies get tracked?",
  ],
  "/mcp-server": [
    "What can I do from Claude or ChatGPT?",
    "How do I set up the connector?",
    "Does MCP use my credits?",
    "What's the difference from using the website?",
  ],
  "/recruiting-timeline": [
    "What should I be doing right now?",
    "When does recruiting start for my industry?",
    "Am I behind for this cycle?",
    "Build me a plan for the next month",
  ],
  "/pricing": [
    "What do I get on each plan?",
    "How far do 2,000 credits go?",
    "What happens to unused credits?",
    "Which plan fits how I'm recruiting?",
  ],
};

// ============================================================================
// HELPER FUNCTIONS
// ============================================================================

/**
 * Find the best route for a user intent/query
 */
export function findRouteForIntent(query: string): string | null {
  const lowerQuery = query.toLowerCase();
  
  // Check direct page matches first
  for (const [route, keywords] of Object.entries(ROUTE_MAPPING.pages)) {
    for (const keyword of keywords) {
      if (lowerQuery.includes(keyword)) {
        return route;
      }
    }
  }
  
  return null;
}

/**
 * Get page info by route
 */
export function getPageByRoute(route: string): typeof PAGES[keyof typeof PAGES] | null {
  for (const [key, page] of Object.entries(PAGES)) {
    if (page.route === route || page.route.startsWith(route)) {
      return page;
    }
  }
  return null;
}

/**
 * Format knowledge base for system prompt
 */
export function formatKnowledgeForPrompt(): string {
  const sections: string[] = [];
  
  // Pages
  sections.push("PAGES AND ROUTES:");
  for (const [key, page] of Object.entries(PAGES)) {
    sections.push(`- ${page.name} (${page.route}): ${page.description}`);
  }
  
  // Credit costs
  sections.push("\nCREDIT COSTS:");
  for (const [feature, cost] of Object.entries(CREDIT_SYSTEM.costs)) {
    sections.push(`- ${feature}: ${cost.amount} credits ${cost.unit}`);
  }
  
  // Tiers
  sections.push("\nSUBSCRIPTION TIERS:");
  for (const [tier, info] of Object.entries(CREDIT_SYSTEM.tiers)) {
    sections.push(`- ${info.name} (${info.price}): ${info.credits} credits/month, ${info.maxContactsPerSearch} contacts/search`);
  }
  
  // Troubleshooting
  sections.push("\nCOMMON ISSUES:");
  for (const [key, issue] of Object.entries(TROUBLESHOOTING)) {
    sections.push(`- ${issue.issue}: ${issue.solution[0]}`);
  }
  
  return sections.join("\n");
}

export default {
  PAGES,
  FEATURES,
  WORKFLOWS,
  CREDIT_SYSTEM,
  TROUBLESHOOTING,
  ROUTE_MAPPING,
  SUGGESTED_QUESTIONS,
  SCOUT_CHIPS_BY_PAGE,
  findRouteForIntent,
  getPageByRoute,
  formatKnowledgeForPrompt
};

