/**
 * First-use walkthrough (Milestone 2), fully mocked: auth, analytics and
 * every service are fakes, so nothing touches Firebase, the ranker, the
 * email lookup or the LLM.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

const auth = vi.hoisted(() => ({ user: { uid: "u1", name: "Ana Lee", email: "ana@umich.edu" } }));
vi.mock("@/contexts/FirebaseAuthContext", () => ({ useFirebaseAuth: () => auth }));
const analytics = vi.hoisted(() => ({ trackFeatureActionCompleted: vi.fn(), trackFirstOutreachFunnel: vi.fn() }));
vi.mock("@/lib/analytics", () => analytics);
vi.mock("@/lib/firebase", () => ({ db: {}, auth: { currentUser: null } }));
vi.mock("@/services/api", () => ({ apiService: {}, BACKEND_URL: "" }));
vi.mock("@/services/firebaseApi", () => ({ firebaseApi: {} }));

import GetStartedPage from "@/pages/GetStartedPage";
import { PREVIEW_PEOPLE, type WalkthroughServices } from "@/pages/walkthroughServices";
import { WALKTHROUGH_VERSION, pickMatches, type EmailDraft, type WalkthroughState } from "@/services/walkthrough";

const DRAFT: EmailDraft = { to: "riley@example.com", subject: "Hello from a USC student", body: "Hi Riley, ..." };

function makeServices(saved: WalkthroughState | null = null, overrides: Partial<WalkthroughServices> = {}) {
  const drafts = new Map<string, EmailDraft>();
  const s = {
    loadState: vi.fn(async () => saved),
    saveState: vi.fn(async () => {}),
    loadProfile: vi.fn(async () => ({ university: "University of Southern California", targetCompanies: ["Goldman Sachs"], roles: [] })),
    loadPeople: vi.fn(async () => ({ people: PREVIEW_PEOPLE })),
    loadFirstOutreach: vi.fn(async () => false),
    revealEmail: vi.fn(async (_id: string, _deck: string, useFree: boolean) => ({ ok: true as const, free: useFree })) as WalkthroughServices["revealEmail"],
    writeDraft: vi.fn(async (id: string) => {
      drafts.set(`ranker_${id}`, DRAFT);
      return { ok: true as const };
    }),
    loadDraft: vi.fn(async (_uid: string, contactId: string) => drafts.get(contactId) ?? null),
    saveDraft: vi.fn(async () => {}),
    gmailStatus: vi.fn(async () => ({ connected: false, canSend: false })),
    connectGmail: vi.fn(async () => "redirecting" as const) as WalkthroughServices["connectGmail"],
    reviewSend: vi.fn(async (contactId: string) => ({
      recipient: "riley@example.com", subject: drafts.get(contactId)?.subject ?? "", hash: "h1",
      blocking: [] as string[], warnings: ["too_short"], alreadySent: false,
    })),
    sendEmail: vi.fn(async () => ({ ok: true as const, alreadySent: false })) as WalkthroughServices["sendEmail"],
    ...overrides,
  } satisfies WalkthroughServices;
  return { s, drafts };
}

const Where = () => {
  const loc = useLocation();
  return <div data-testid="where">{loc.pathname + loc.search}</div>;
};

function renderPage(services: WalkthroughServices, url = "/get-started") {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/get-started" element={<GetStartedPage services={services} />} />
        <Route path="*" element={<Where />} />
      </Routes>
    </MemoryRouter>,
  );
}

const lastSaved = (s: ReturnType<typeof makeServices>["s"]) =>
  (vi.mocked(s.saveState).mock.calls.at(-1) as unknown as [string, WalkthroughState])[1];
const MATCHES = pickMatches(PREVIEW_PEOPLE);

beforeEach(() => vi.clearAllMocks());

describe("GetStartedPage walkthrough", () => {
  it("shows 3-4 matches from different companies with a reason for each, and saves them", async () => {
    const { s } = makeServices({ version: WALKTHROUGH_VERSION, status: "pending", step: "matches" });
    renderPage(s);
    const list = await screen.findByRole("list", { name: "Your matches" });
    const items = within(list).getAllByRole("listitem");
    expect(items.length).toBeGreaterThanOrEqual(3);
    expect(items.length).toBeLessThanOrEqual(4);
    const companies = MATCHES.map((m) => m.company);
    expect(new Set(companies).size).toBe(companies.length);
    expect(within(items[0]).getByText("Went to University of Southern California, like you")).toBeInTheDocument();
    // Never starts paid lookups on its own.
    expect(s.revealEmail).not.toHaveBeenCalled();
    expect(lastSaved(s)).toMatchObject({ status: "in_progress", step: "matches" });
    expect(lastSaved(s).matches?.map((m) => m.id)).toEqual(MATCHES.map((m) => m.id));
  });

  it("selecting a person shows their profile and why they fit, without spending anything", async () => {
    const { s } = makeServices();
    renderPage(s);
    fireEvent.click(await screen.findByRole("button", { name: /Riley Sample/ }));
    expect(await screen.findByRole("heading", { name: "Riley Sample" })).toBeInTheDocument();
    expect(screen.getByText("Works at Goldman Sachs, one of your target companies")).toBeInTheDocument();
    expect(screen.getByText(/uses 8 credits. If we can't find one, you're not charged/)).toBeInTheDocument();
    expect(s.revealEmail).not.toHaveBeenCalled();
    expect(s.writeDraft).not.toHaveBeenCalled();
    expect(lastSaved(s)).toMatchObject({ step: "person", selectedId: "preview-0" });
  });

  it("going back to the matches keeps the person marked as selected", async () => {
    const { s } = makeServices();
    renderPage(s);
    fireEvent.click(await screen.findByRole("button", { name: /Jamie Demo/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Back to your matches" }));
    expect(await screen.findByRole("button", { name: /Jamie Demo/ })).toHaveAttribute("aria-pressed", "true");
    expect(lastSaved(s)).toMatchObject({ step: "matches", selectedId: "preview-3" });
  });

  it("prepares an editable email on request, in the app only, and saves edits", async () => {
    const { s } = makeServices();
    renderPage(s);
    fireEvent.click(await screen.findByRole("button", { name: /Riley Sample/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Prepare my email to Riley" }));
    const subject = await screen.findByLabelText("Subject");
    expect(subject).toHaveValue(DRAFT.subject);
    expect(s.revealEmail).toHaveBeenCalledWith("preview-0", "preview-deck", false);
    expect(s.writeDraft).toHaveBeenCalledWith("preview-0", "preview-deck");
    expect(lastSaved(s)).toMatchObject({ status: "drafted", step: "draft", selectedId: "preview-0", contactId: "ranker_preview-0" });

    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "Hi Riley, edited" } });
    await waitFor(() =>
      expect(s.saveDraft).toHaveBeenCalledWith("u1", "ranker_preview-0", { subject: DRAFT.subject, body: "Hi Riley, edited" }),
    );
    expect(await screen.findByText("Saved")).toBeInTheDocument();
  });

  it("restores the saved draft (with edits) after a refresh, without finding the email again", async () => {
    const { s, drafts } = makeServices({
      version: WALKTHROUGH_VERSION, status: "drafted", step: "draft", matches: MATCHES, selectedId: "preview-0", contactId: "ranker_preview-0",
    });
    drafts.set("ranker_preview-0", { ...DRAFT, body: "My edited version" });
    renderPage(s);
    expect(await screen.findByLabelText("Email")).toHaveValue("My edited version");
    expect(s.revealEmail).not.toHaveBeenCalled();
    expect(s.writeDraft).not.toHaveBeenCalled();
    expect(s.loadPeople).not.toHaveBeenCalled();
  });

  it("finishes preparing a draft that was interrupted by a refresh", async () => {
    const { s } = makeServices({
      version: WALKTHROUGH_VERSION, status: "in_progress", step: "draft", matches: MATCHES, selectedId: "preview-0", contactId: "ranker_preview-0",
    });
    renderPage(s);
    expect(await screen.findByLabelText("Subject")).toHaveValue(DRAFT.subject);
    expect(s.writeDraft).toHaveBeenCalledTimes(1);
  });

  it("handles a person with no email: nothing charged, back to the matches, pick someone else", async () => {
    const { s } = makeServices(null, { revealEmail: vi.fn(async () => ({ ok: false as const, reason: "no_email" as const })) });
    renderPage(s);
    fireEvent.click(await screen.findByRole("button", { name: /Riley Sample/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Prepare my email to Riley" }));
    expect(await screen.findByText(/couldn't find a verified email for Riley, so nothing was charged/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Riley Sample/ })).toBeDisabled();
    expect(screen.getByText("No email found")).toBeInTheDocument();
    expect(s.writeDraft).not.toHaveBeenCalled();
    expect(lastSaved(s)).toMatchObject({ step: "matches", noEmailIds: ["preview-0"] });
  });

  it("explains a lack of credits and stays on the person", async () => {
    const { s } = makeServices(null, { revealEmail: vi.fn(async () => ({ ok: false as const, reason: "insufficient_credits" as const })) });
    renderPage(s);
    fireEvent.click(await screen.findByRole("button", { name: /Riley Sample/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Prepare my email to Riley" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("You need 8 credits");
    expect(screen.getByRole("button", { name: "Prepare my email to Riley" })).toBeInTheDocument();
  });

  it("recovers from a failed draft: try again, or pick someone else", async () => {
    const { s, drafts } = makeServices();
    vi.mocked(s.writeDraft).mockResolvedValueOnce({ ok: false, reason: "generation_failed" });
    renderPage(s);
    fireEvent.click(await screen.findByRole("button", { name: /Riley Sample/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Prepare my email to Riley" }));
    expect(await screen.findByRole("heading", { name: "We couldn't write that email" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Pick someone else" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByLabelText("Subject")).toHaveValue(DRAFT.subject);
    expect(s.writeDraft).toHaveBeenCalledTimes(2);
    expect(drafts.size).toBe(1);
  });

  it("shows a way out when there are no recommendations", async () => {
    const { s } = makeServices(null, { loadPeople: vi.fn(async () => ({ people: [], reason: "sparse_profile" })) });
    renderPage(s, "/get-started?returnTo=%2Fpricing");
    expect(await screen.findByRole("heading", { name: "We don't have matches for you yet" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Search on Find" }));
    expect(await screen.findByTestId("where")).toHaveTextContent("/pricing");
  });

  it("shows a retry when recommendations fail to load", async () => {
    const loadPeople = vi.fn().mockRejectedValueOnce(new Error("offline")).mockResolvedValue({ people: PREVIEW_PEOPLE });
    const { s } = makeServices(null, { loadPeople });
    renderPage(s);
    expect(await screen.findByRole("heading", { name: "We couldn't load your matches" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByRole("list", { name: "Your matches" })).toBeInTheDocument();
  });

  it("can always save and exit to Find", async () => {
    const { s } = makeServices();
    renderPage(s);
    await screen.findByRole("list", { name: "Your matches" });
    fireEvent.click(screen.getByRole("button", { name: "Save and finish later" }));
    expect(await screen.findByTestId("where")).toHaveTextContent("/find");
  });

  it("sends someone who already finished straight to Find", async () => {
    const { s } = makeServices({ version: WALKTHROUGH_VERSION, status: "completed", step: "draft" });
    renderPage(s);
    expect(await screen.findByTestId("where")).toHaveTextContent("/find");
  });

});

// ── Milestone 3: free first outreach, Gmail, sending ───────────────────────

const DRAFT_STATE: WalkthroughState = {
  version: WALKTHROUGH_VERSION, status: "drafted", step: "draft", matches: MATCHES,
  selectedId: "preview-0", contactId: "ranker_preview-0",
};
function atDraft(overrides: Partial<WalkthroughServices> = {}) {
  const made = makeServices(DRAFT_STATE, overrides);
  made.drafts.set("ranker_preview-0", DRAFT);
  return made;
}
const funnelEvents = () => analytics.trackFirstOutreachFunnel.mock.calls.map((c) => c[0]);

describe("free first outreach", () => {
  it("uses the free first outreach when the backend says it's available", async () => {
    const { s } = makeServices(null, { loadFirstOutreach: vi.fn(async () => true) });
    renderPage(s);
    fireEvent.click(await screen.findByRole("button", { name: /Riley Sample/ }));
    expect(await screen.findByText("Your first email is on us. No credits needed.")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Prepare my email to Riley" }));
    await screen.findByLabelText("Subject");
    expect(s.revealEmail).toHaveBeenCalledWith("preview-0", "preview-deck", true);
    expect(analytics.trackFirstOutreachFunnel).toHaveBeenCalledWith(
      "walkthrough_draft_generated", undefined, expect.objectContaining({ free_outreach: true }),
    );
  });

  it("never charges silently when the free outreach can't be used: shows the price and waits", async () => {
    const revealEmail = vi.fn()
      .mockResolvedValueOnce({ ok: false, reason: "free_unavailable" })
      .mockResolvedValueOnce({ ok: true, free: false });
    const { s } = makeServices(null, { loadFirstOutreach: vi.fn(async () => true), revealEmail });
    renderPage(s);
    fireEvent.click(await screen.findByRole("button", { name: /Riley Sample/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Prepare my email to Riley" }));
    expect(await screen.findByText(/free first email isn't available on this account/)).toBeInTheDocument();
    expect(screen.getByText("Finding their email uses 8 credits. If we can't find one, you're not charged.")).toBeInTheDocument();
    expect(revealEmail).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole("button", { name: "Prepare my email to Riley" }));
    await screen.findByLabelText("Subject");
    expect(revealEmail).toHaveBeenLastCalledWith("preview-0", "preview-deck", false);
  });

  it("resuming an interrupted draft still uses the free outreach", async () => {
    const { s } = makeServices(
      { ...DRAFT_STATE, status: "in_progress" },
      { loadFirstOutreach: vi.fn(() => new Promise<boolean>((r) => setTimeout(() => r(true), 20))) },
    );
    renderPage(s);
    await screen.findByLabelText("Subject");
    expect(s.revealEmail).toHaveBeenCalledWith("preview-0", "preview-deck", true);
  });
});

describe("Gmail connection", () => {
  it("explains why Gmail is needed and starts OAuth back to this draft, after saving edits", async () => {
    const { s } = atDraft();
    renderPage(s, "/get-started?returnTo=%2Fpricing");
    expect(await screen.findByText(/goes out from your own Gmail address/)).toBeInTheDocument();
    expect(screen.getByText(/Nothing is sent without your OK/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "Edited before connecting" } });
    fireEvent.click(screen.getByRole("button", { name: "Connect Gmail" }));
    await waitFor(() => expect(s.connectGmail).toHaveBeenCalledWith("/get-started?returnTo=%2Fpricing"));
    // The edit was saved before leaving for Google.
    expect(s.saveDraft).toHaveBeenCalledWith("u1", "ranker_preview-0", { subject: DRAFT.subject, body: "Edited before connecting" });
    expect(vi.mocked(s.saveDraft).mock.invocationCallOrder[0]).toBeLessThan(vi.mocked(s.connectGmail).mock.invocationCallOrder[0]);
  });

  it("skips the connect step when Gmail can already send", async () => {
    const { s } = atDraft({ gmailStatus: vi.fn(async () => ({ connected: true, canSend: true })) });
    renderPage(s);
    expect(await screen.findByRole("button", { name: "Send email to Riley" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Connect Gmail" })).not.toBeInTheDocument();
  });

  it("comes back from OAuth to the same edited draft, re-checking permissions", async () => {
    const { s, drafts } = atDraft({ gmailStatus: vi.fn(async () => ({ connected: true, canSend: true })) });
    drafts.set("ranker_preview-0", { ...DRAFT, body: "My edited version" });
    renderPage(s, "/get-started?connected=gmail");
    expect(await screen.findByText("Gmail is connected. Your email is ready to send.")).toBeInTheDocument();
    expect(screen.getByLabelText("Email")).toHaveValue("My edited version");
    expect(screen.getByRole("button", { name: "Send email to Riley" })).toBeInTheDocument();
    expect(s.gmailStatus).toHaveBeenCalled();
    expect(funnelEvents()).toContain("walkthrough_gmail_connected");
  });

  it("doesn't trust ?connected=gmail when the granted permissions can't send", async () => {
    const { s } = atDraft({ gmailStatus: vi.fn(async () => ({ connected: true, canSend: false })) });
    renderPage(s, "/get-started?connected=gmail");
    expect(await screen.findByRole("alert")).toHaveTextContent("without permission to send");
    expect(screen.getByRole("button", { name: "Reconnect Gmail" })).toBeInTheDocument();
    expect(funnelEvents()).not.toContain("walkthrough_gmail_connected");
  });

  it("explains a declined permission and keeps the draft", async () => {
    const { s } = atDraft();
    renderPage(s, "/get-started?gmail_error=scopes_declined");
    expect(await screen.findByRole("alert")).toHaveTextContent("leave all the boxes checked");
    expect(screen.getByLabelText("Subject")).toHaveValue(DRAFT.subject);
    expect(screen.getByRole("button", { name: "Connect Gmail" })).toBeInTheDocument();
  });
});

describe("sending", () => {
  const connected = { gmailStatus: vi.fn(async () => ({ connected: true, canSend: true })) };

  it("sends only after an explicit confirm, then completes and opens the conversation", async () => {
    const { s } = atDraft({ ...connected });
    renderPage(s);
    fireEvent.click(await screen.findByRole("button", { name: "Send email to Riley" }));
    const dialog = await screen.findByRole("dialog", { name: "Confirm sending" });
    expect(within(dialog).getByText("riley@example.com")).toBeInTheDocument();
    expect(within(dialog).getByText(DRAFT.subject)).toBeInTheDocument();
    // Style issues are optional suggestions, not blockers.
    expect(within(dialog).getByRole("list", { name: "Optional suggestions" })).toBeInTheDocument();
    expect(s.sendEmail).not.toHaveBeenCalled();

    fireEvent.click(within(dialog).getByRole("button", { name: "Yes, send it" }));
    expect(await screen.findByTestId("where")).toHaveTextContent("/contacts?contact=ranker_preview-0");
    expect(s.sendEmail).toHaveBeenCalledWith("ranker_preview-0", "h1", { confirmNotSent: false });
    expect(lastSaved(s)).toMatchObject({ status: "completed" });

    const sentEvent = analytics.trackFirstOutreachFunnel.mock.calls.find((c) => c[0] === "first_email_sent");
    expect(sentEvent).toBeTruthy();
    // No email text, recipient or names in analytics.
    const flat = JSON.stringify(analytics.trackFirstOutreachFunnel.mock.calls);
    for (const secret of [DRAFT.subject, DRAFT.body, "riley@example.com", "Riley"]) expect(flat).not.toContain(secret);
  });

  it("an edit after review closes the confirm, so only reviewed text can be sent", async () => {
    const { s } = atDraft({ ...connected });
    renderPage(s);
    fireEvent.click(await screen.findByRole("button", { name: "Send email to Riley" }));
    await screen.findByRole("dialog", { name: "Confirm sending" });
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "Changed my mind" } });
    expect(screen.queryByRole("dialog", { name: "Confirm sending" })).not.toBeInTheDocument();
    expect(s.sendEmail).not.toHaveBeenCalled();
  });

  it("blocks serious problems before the confirm", async () => {
    const { s } = atDraft({
      ...connected,
      reviewSend: vi.fn(async () => ({ recipient: "r@x.com", subject: "s", hash: "h", blocking: ["placeholder"], warnings: [], alreadySent: false })),
    });
    renderPage(s);
    fireEvent.click(await screen.findByRole("button", { name: "Send email to Riley" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Replace placeholder text like [Name]");
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
  });

  it("asks to reconnect when Gmail access expired, keeping the draft", async () => {
    const { s } = atDraft({ ...connected, sendEmail: vi.fn(async () => ({ ok: false as const, reason: "gmail_token_expired" as const })) });
    renderPage(s);
    fireEvent.click(await screen.findByRole("button", { name: "Send email to Riley" }));
    fireEvent.click(await screen.findByRole("button", { name: "Yes, send it" }));
    expect(await screen.findByRole("button", { name: "Reconnect Gmail" })).toBeInTheDocument();
    expect(screen.getByLabelText("Subject")).toHaveValue(DRAFT.subject);
    expect(s.saveState).not.toHaveBeenCalledWith("u1", expect.objectContaining({ status: "completed" }));
  });

  it("reloads the draft when it changed since review", async () => {
    const { s, drafts } = atDraft({ ...connected });
    vi.mocked(s.sendEmail).mockImplementationOnce(async () => {
      drafts.set("ranker_preview-0", { ...DRAFT, body: "Edited in another tab" });
      return { ok: false, reason: "draft_changed" };
    });
    renderPage(s);
    fireEvent.click(await screen.findByRole("button", { name: "Send email to Riley" }));
    fireEvent.click(await screen.findByRole("button", { name: "Yes, send it" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("changed since you reviewed it");
    expect(screen.getByLabelText("Email")).toHaveValue("Edited in another tab");
  });

  it("when an earlier try may have sent, asks to check Sent before sending again", async () => {
    const sendEmail = vi.fn()
      .mockResolvedValueOnce({ ok: false, reason: "send_unconfirmed" })
      .mockResolvedValueOnce({ ok: true, alreadySent: false });
    const { s } = atDraft({ ...connected, sendEmail });
    renderPage(s);
    fireEvent.click(await screen.findByRole("button", { name: "Send email to Riley" }));
    fireEvent.click(await screen.findByRole("button", { name: "Yes, send it" }));
    expect(await screen.findByText("Check your Sent folder first")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /Open Gmail Sent/ })).toHaveAttribute("href", "https://mail.google.com/mail/u/0/#sent");
    fireEvent.click(screen.getByRole("button", { name: "It's not there. Send it now" }));
    expect(await screen.findByTestId("where")).toHaveTextContent("/contacts?contact=ranker_preview-0");
    expect(sendEmail).toHaveBeenLastCalledWith("ranker_preview-0", "h1", { confirmNotSent: true });
  });

  it("a failed send keeps the draft and can be retried", async () => {
    const sendEmail = vi.fn()
      .mockResolvedValueOnce({ ok: false, reason: "send_failed" })
      .mockResolvedValueOnce({ ok: true, alreadySent: false });
    const { s } = atDraft({ ...connected, sendEmail });
    renderPage(s);
    fireEvent.click(await screen.findByRole("button", { name: "Send email to Riley" }));
    fireEvent.click(await screen.findByRole("button", { name: "Yes, send it" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("won't send it twice");
    fireEvent.click(screen.getByRole("button", { name: "Send email to Riley" }));
    fireEvent.click(await screen.findByRole("button", { name: "Yes, send it" }));
    expect(await screen.findByTestId("where")).toHaveTextContent("/contacts?contact=ranker_preview-0");
  });

  it("an email that already went out just opens the conversation", async () => {
    const { s } = atDraft({
      ...connected,
      reviewSend: vi.fn(async () => ({ recipient: "r@x.com", subject: "s", hash: "h", blocking: [], warnings: [], alreadySent: true })),
    });
    renderPage(s);
    fireEvent.click(await screen.findByRole("button", { name: "Send email to Riley" }));
    expect(await screen.findByTestId("where")).toHaveTextContent("/contacts?contact=ranker_preview-0");
    expect(s.sendEmail).not.toHaveBeenCalled();
    expect(funnelEvents()).not.toContain("first_email_sent");
  });
});

describe("analytics", () => {
  it("records match selection with timestamps-only props", async () => {
    const { s } = makeServices();
    renderPage(s);
    fireEvent.click(await screen.findByRole("button", { name: /Riley Sample/ }));
    expect(analytics.trackFirstOutreachFunnel).toHaveBeenCalledWith("walkthrough_match_selected", undefined, { rank: 0, position: 0 });
  });
});
