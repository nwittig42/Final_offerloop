/**
 * WalkthroughFlow in modal mode (the first-email pop-up on /find). Page mode
 * is covered by GetStartedPage.test.tsx. Auth, analytics and every service
 * are fakes.
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

import { WalkthroughFlow, type WalkthroughExit } from "./WalkthroughFlow";
import { useFindWalkthrough } from "./useFindWalkthrough";
import { PREVIEW_PEOPLE, type WalkthroughServices } from "@/pages/walkthroughServices";
import { WALKTHROUGH_VERSION, pickMatches, type EmailDraft, type WalkthroughState } from "@/services/walkthrough";

const DRAFT: EmailDraft = { to: "riley@example.com", subject: "Hello from a USC student", body: "Hi Riley, ..." };
const MATCHES = pickMatches(PREVIEW_PEOPLE);
const DRAFT_STATE: WalkthroughState = {
  version: WALKTHROUGH_VERSION, status: "drafted", step: "draft", matches: MATCHES,
  selectedId: "preview-0", contactId: "ranker_preview-0",
};

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
      blocking: [] as string[], warnings: [] as string[], alreadySent: false,
    })),
    sendEmail: vi.fn(async () => ({ ok: true as const, alreadySent: false })) as WalkthroughServices["sendEmail"],
    ...overrides,
  } satisfies WalkthroughServices;
  return { s, drafts };
}
function atDraft(overrides: Partial<WalkthroughServices> = {}) {
  const made = makeServices(DRAFT_STATE, overrides);
  made.drafts.set("ranker_preview-0", DRAFT);
  return made;
}

const Where = () => {
  const loc = useLocation();
  return <div data-testid="where">{loc.pathname + loc.search}</div>;
};

function renderModal(services: WalkthroughServices, url = "/find?walkthrough=1") {
  const onExit = vi.fn<(why: WalkthroughExit) => void>();
  const utils = render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route
          path="/find"
          element={
            <>
              <Where />
              <WalkthroughFlow mode="modal" services={services} gmailReturnPath="/find?walkthrough=1" onExit={onExit} />
            </>
          }
        />
        <Route path="*" element={<div data-testid="left"><Where /></div>} />
      </Routes>
    </MemoryRouter>,
  );
  return { ...utils, onExit };
}

beforeEach(() => vi.clearAllMocks());

describe("WalkthroughFlow, modal mode", () => {
  it('modal mode: Skip calls onExit("skip") and does not navigate', async () => {
    const { s } = makeServices();
    const { onExit } = renderModal(s);
    await screen.findByRole("list", { name: "Your matches" });
    fireEvent.click(screen.getByRole("button", { name: "Skip for now and go to Find" }));
    expect(onExit).toHaveBeenCalledWith("skip");
    expect(screen.queryByTestId("left")).not.toBeInTheDocument();
    expect(screen.getByTestId("where")).toHaveTextContent("/find?walkthrough=1");
  });

  it('modal mode: Save and finish later calls onExit("later") after flushing the draft', async () => {
    const { s } = atDraft();
    const { onExit } = renderModal(s);
    fireEvent.change(await screen.findByLabelText("Email"), { target: { value: "Half done" } });
    fireEvent.click(screen.getAllByRole("button", { name: "Save and finish later" })[0]);
    expect(s.saveDraft).toHaveBeenCalledWith("u1", "ranker_preview-0", { subject: DRAFT.subject, body: "Half done" });
    expect(onExit).toHaveBeenCalledWith("later");
    expect(screen.queryByTestId("left")).not.toBeInTheDocument();
  });

  it('modal mode: "Search on Find" calls onExit("find")', async () => {
    const { s } = makeServices(null, { loadPeople: vi.fn(async () => ({ people: [], reason: "sparse_profile" })) });
    const { onExit } = renderModal(s);
    fireEvent.click(await screen.findByRole("button", { name: "Search on Find" }));
    expect(onExit).toHaveBeenCalledWith("find");
    expect(screen.queryByTestId("left")).not.toBeInTheDocument();
  });

  it('modal mode: a sent email shows "Sent to <first name>" with Open in Contacts and Done; Done calls onExit("find")', async () => {
    const { s } = atDraft({ gmailStatus: vi.fn(async () => ({ connected: true, canSend: true })) });
    const { onExit } = renderModal(s);
    fireEvent.click(await screen.findByRole("button", { name: "Send email to Riley" }));
    fireEvent.click(await screen.findByRole("button", { name: "Yes, send it" }));
    expect(await screen.findByRole("heading", { name: "Sent to Riley" })).toBeInTheDocument();
    expect(screen.queryByTestId("left")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Open in Contacts" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Done" }));
    expect(onExit).toHaveBeenCalledWith("find");
    expect(s.saveState).toHaveBeenLastCalledWith("u1", expect.objectContaining({ status: "completed" }));
  });

  it("modal mode: Open in Contacts opens the conversation", async () => {
    const { s } = atDraft({ gmailStatus: vi.fn(async () => ({ connected: true, canSend: true })) });
    renderModal(s);
    fireEvent.click(await screen.findByRole("button", { name: "Send email to Riley" }));
    fireEvent.click(await screen.findByRole("button", { name: "Yes, send it" }));
    fireEvent.click(await screen.findByRole("button", { name: "Open in Contacts" }));
    expect(within(await screen.findByTestId("left")).getByTestId("where")).toHaveTextContent("/contacts?contact=ranker_preview-0");
  });

  it('modal mode: connectGmail is called with gmailReturnPath "/find?walkthrough=1"', async () => {
    const { s } = atDraft();
    renderModal(s);
    fireEvent.click(await screen.findByRole("button", { name: "Connect Gmail" }));
    await waitFor(() => expect(s.connectGmail).toHaveBeenCalledWith("/find?walkthrough=1"));
  });

  it("modal mode: returning with ?walkthrough=1&connected=gmail on /find handles the connect and leaves /find with no walkthrough/connected params", async () => {
    const { s } = atDraft({ gmailStatus: vi.fn(async () => ({ connected: true, canSend: true })) });
    renderModal(s, "/find?tab=people&walkthrough=1&connected=gmail");
    expect(await screen.findByText("Gmail is connected. Your email is ready to send.")).toBeInTheDocument();
    expect(screen.getByTestId("where")).toHaveTextContent(/^\/find\?tab=people$/);
  });

  it("modal mode: a gmail_error return shows the error and cleans the /find URL", async () => {
    const { s } = atDraft();
    renderModal(s, "/find?walkthrough=1&gmail_error=scopes_declined");
    expect(await screen.findByRole("alert")).toHaveTextContent("leave all the boxes checked");
    expect(screen.getByTestId("where")).toHaveTextContent(/^\/find$/);
  });

  it('modal mode: "Add target companies in Settings" calls onExit("later") and opens Settings', async () => {
    const { s } = makeServices(null, { loadPeople: vi.fn(async () => ({ people: [], reason: "sparse_profile" })) });
    const { onExit } = renderModal(s);
    fireEvent.click(await screen.findByRole("button", { name: "Add target companies in Settings" }));
    expect(onExit).toHaveBeenCalledWith("later");
    expect(await screen.findByTestId("left")).toHaveTextContent("/account-settings");
  });

  it("modal mode: no full-page frame (no logo), but Save and finish later is still there", async () => {
    const { s } = makeServices();
    const { onExit, container } = renderModal(s);
    await screen.findByRole("list", { name: "Your matches" });
    expect(screen.queryByRole("img", { name: "Offerloop" })).not.toBeInTheDocument();
    expect(container.querySelector(".min-h-screen")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Save and finish later" }));
    expect(onExit).toHaveBeenCalledWith("later");
  });

  it('modal mode: a completed walkthrough calls onExit("find")', async () => {
    const { s } = makeServices({ version: WALKTHROUGH_VERSION, status: "completed", step: "draft" });
    const { onExit } = renderModal(s);
    await waitFor(() => expect(onExit).toHaveBeenCalledWith("find"));
    expect(screen.queryByTestId("left")).not.toBeInTheDocument();
  });

  it("closing the pop-up while matches are still loading keeps closedAt (the flow doesn't save after it's gone)", async () => {
    // One saved walkthrough shared by the pop-up's owner (the hook) and the flow, like users/{uid}.walkthrough.
    let stored: WalkthroughState | null = { version: WALKTHROUGH_VERSION, status: "pending", step: "matches" };
    const load = async () => stored;
    const save = async (_uid: string, next: WalkthroughState) => {
      stored = next;
    };
    let finishPeople: (v: { people: typeof PREVIEW_PEOPLE }) => void = () => {};
    const { s } = makeServices(null, {
      loadState: vi.fn(load),
      saveState: vi.fn(save),
      loadPeople: vi.fn(() => new Promise<{ people: typeof PREVIEW_PEOPLE }>((r) => (finishPeople = r))),
    });
    const Harness = () => {
      const w = useFindWalkthrough("u1", { load, save });
      return (
        <>
          <button type="button" onClick={() => w.exit("later")}>Close pop-up</button>
          {w.open && <WalkthroughFlow mode="modal" services={s} gmailReturnPath="/find?walkthrough=1" onExit={w.exit} />}
        </>
      );
    };
    render(
      <MemoryRouter initialEntries={["/find"]}>
        <Harness />
      </MemoryRouter>,
    );
    await waitFor(() => expect(s.loadPeople).toHaveBeenCalled());
    fireEvent.click(screen.getByRole("button", { name: "Close pop-up" }));
    await waitFor(() => expect(stored?.closedAt).toEqual(expect.any(String)));
    // The matches arrive after the pop-up is gone.
    finishPeople({ people: PREVIEW_PEOPLE });
    await new Promise((r) => setTimeout(r, 20));
    expect(stored?.closedAt).toEqual(expect.any(String));
  });
});
