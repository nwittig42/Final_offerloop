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
vi.mock("@/lib/analytics", () => ({ trackFeatureActionCompleted: vi.fn() }));
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
    revealEmail: vi.fn(async () => ({ ok: true as const })),
    writeDraft: vi.fn(async (id: string) => {
      drafts.set(`ranker_${id}`, DRAFT);
      return { ok: true as const };
    }),
    loadDraft: vi.fn(async (_uid: string, contactId: string) => drafts.get(contactId) ?? null),
    saveDraft: vi.fn(async () => {}),
    firstOutreachFree: false,
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

  it("prepares an editable email on request, in the app only, and saves edits", async () => {
    const { s } = makeServices();
    renderPage(s);
    fireEvent.click(await screen.findByRole("button", { name: /Riley Sample/ }));
    fireEvent.click(await screen.findByRole("button", { name: "Prepare my email to Riley" }));
    const subject = await screen.findByLabelText("Subject");
    expect(subject).toHaveValue(DRAFT.subject);
    expect(s.revealEmail).toHaveBeenCalledWith("preview-0", "preview-deck");
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

  it("says the first email is free once the entitlement is on", async () => {
    const { s } = makeServices(null, { firstOutreachFree: true });
    renderPage(s);
    fireEvent.click(await screen.findByRole("button", { name: /Riley Sample/ }));
    expect(await screen.findByText("Your first email is on us. No credits needed.")).toBeInTheDocument();
  });
});
