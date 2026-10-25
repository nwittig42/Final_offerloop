import { describe, expect, it, vi } from "vitest";
import { useState, type ReactNode } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("@/lib/firebase", () => ({ db: {}, auth: { currentUser: null } }));

import { useContactsTour, type ContactsTourDeps } from "./useContactsTour";
import { WALKTHROUGH_VERSION, type WalkthroughState } from "@/services/walkthrough";

const AT = (tourStep: WalkthroughState["tourStep"]): WalkthroughState => ({
  version: WALKTHROUGH_VERSION,
  status: "in_progress",
  step: "matches",
  tourStep,
  tourContactId: "ranker_7",
  tourPersonFirstName: "Pat",
});

const GOOD = { connected: true, scopes: ["https://www.googleapis.com/auth/gmail.compose"] };

function setup(saved: WalkthroughState, url: string, over: Partial<ContactsTourDeps> = {}) {
  let db: WalkthroughState | null = saved;
  const deps: ContactsTourDeps = {
    load: vi.fn(async () => db),
    save: vi.fn(async (_uid: string, s: WalkthroughState) => {
      db = s;
    }),
    gmailStatus: vi.fn(async () => GOOD),
    pushDraft: vi.fn(async () => ({ ok: true as const, gmailDraftUrl: "https://mail.google.com/d/1", created: true })),
    startOAuth: vi.fn(async () => "https://accounts.google.com/x"),
    redirect: vi.fn(),
    retryDelayMs: 0,
    allSetMs: 50,
    ...over,
  };
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const invalidate = vi.spyOn(client, "invalidateQueries");
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  const hook = renderHook(
    () => {
      const [params, setParams] = useState(() => new URLSearchParams(url));
      const updateParams = (patch: Record<string, string | null>) =>
        setParams((p) => {
          const next = new URLSearchParams(p);
          for (const [k, v] of Object.entries(patch)) {
            if (v === null) next.delete(k);
            else next.set(k, v);
          }
          return next;
        });
      return { params, tour: useContactsTour("u1", { searchParams: params, updateParams }, deps) };
    },
    { wrapper },
  );
  return { ...hook, deps, invalidate, saved: () => db };
}

describe("useContactsTour", () => {
  it("review: shows the draft step for the tour's contact only", async () => {
    const { result } = setup(AT("review"), "contact=ranker_7");
    await waitFor(() => expect(result.current.tour.view?.title).toBe("Your email to Pat"));

    const other = setup(AT("review"), "contact=someone_else");
    await waitFor(() => expect(other.result.current.tour.loaded).toBe(true));
    expect(other.result.current.tour.view).toBeNull();
  });

  it("selects the tour's contact when the URL has none", async () => {
    const { result } = setup(AT("review"), "");
    await waitFor(() => expect(result.current.params.get("contact")).toBe("ranker_7"));
  });

  it("already connected at review: skips to the push, then open", async () => {
    const { result, deps, invalidate, saved } = setup(AT("review"), "contact=ranker_7");
    await waitFor(() => expect(result.current.tour.view?.action).toBeTruthy());
    await act(async () => result.current.tour.view!.action!.onClick());
    await waitFor(() => expect(saved()?.tourStep).toBe("open"));
    expect(deps.pushDraft).toHaveBeenCalledWith("ranker_7");
    expect(deps.startOAuth).not.toHaveBeenCalled();
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["contacts"] });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["trackerContacts", "withArchived"] });
    await waitFor(() => expect(result.current.tour.view?.title).toBe("It's in your Gmail drafts"));
  });

  it("not connected at review: on to the Gmail step, no push", async () => {
    const { result, deps, saved } = setup(AT("review"), "contact=ranker_7", {
      gmailStatus: vi.fn(async () => ({ connected: false })),
    });
    await waitFor(() => expect(result.current.tour.view?.action).toBeTruthy());
    await act(async () => result.current.tour.view!.action!.onClick());
    await waitFor(() => expect(saved()?.tourStep).toBe("gmail"));
    expect(deps.pushDraft).not.toHaveBeenCalled();
    await waitFor(() => expect(result.current.tour.view?.title).toBe("Connect Gmail"));
  });

  it("Connect Gmail starts OAuth coming back to this contact", async () => {
    const { result, deps } = setup(AT("gmail"), "contact=ranker_7", { gmailStatus: vi.fn(async () => ({ connected: false })) });
    await waitFor(() => expect(deps.gmailStatus).toHaveBeenCalled());
    await waitFor(() => expect(result.current.tour.view?.action?.label).toBe("Connect Gmail"));
    await act(async () => result.current.tour.view!.action!.onClick());
    expect(deps.startOAuth).toHaveBeenCalledWith("/contacts?contact=ranker_7&tour=gmail");
    expect(deps.redirect).toHaveBeenCalledWith("https://accounts.google.com/x");
  });

  it("Not now closes the tour", async () => {
    const { result, saved } = setup(AT("gmail"), "contact=ranker_7");
    await waitFor(() => expect(result.current.tour.view?.secondary?.label).toBe("Not now"));
    await act(async () => result.current.tour.view!.secondary!.onClick());
    expect(saved()?.closedAt).toBeTruthy();
  });

  it("OAuth return with good scopes: pushes, moves to open, cleans the URL", async () => {
    const { result, deps, saved } = setup(AT("gmail"), "contact=ranker_7&tour=gmail&connected=gmail");
    await waitFor(() => expect(saved()?.tourStep).toBe("open"));
    expect(deps.gmailStatus).toHaveBeenCalled();
    expect(deps.pushDraft).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(result.current.params.toString()).toBe("contact=ranker_7"));
  });

  it("OAuth return with gmail_error: shows the permission copy, no push, cleans the URL", async () => {
    const { result, deps } = setup(AT("gmail"), "contact=ranker_7&tour=gmail&gmail_error=scopes_declined");
    await waitFor(() =>
      expect(result.current.tour.view?.body).toBe("Google didn't give us permission to create drafts. Connect again and tick every box."),
    );
    expect(result.current.tour.view?.action?.label).toBe("Try again");
    expect(deps.pushDraft).not.toHaveBeenCalled();
    await waitFor(() => expect(result.current.params.toString()).toBe("contact=ranker_7"));
  });

  it("OAuth return without send scopes: the permission copy, no push", async () => {
    const { result, deps } = setup(AT("gmail"), "contact=ranker_7&tour=gmail&connected=gmail", {
      gmailStatus: vi.fn(async () => ({ connected: true, scopes: ["https://www.googleapis.com/auth/gmail.readonly"] })),
    });
    await waitFor(() => expect(result.current.tour.view?.action?.label).toBe("Try again"));
    expect(deps.pushDraft).not.toHaveBeenCalled();
  });

  it("a push that finds the existing draft (created: false) still moves to open", async () => {
    const { saved } = setup(AT("gmail"), "contact=ranker_7&tour=gmail&connected=gmail", {
      pushDraft: vi.fn(async () => ({ ok: true as const, gmailDraftUrl: "https://mail.google.com/d/1", created: false })),
    });
    await waitFor(() => expect(saved()?.tourStep).toBe("open"));
  });

  it("push says Gmail isn't connected: back to the Gmail step with the permission copy", async () => {
    const { result, saved } = setup(AT("review"), "contact=ranker_7", {
      pushDraft: vi.fn(async () => ({ ok: false as const, reason: "gmail_scope_missing" as const })),
    });
    await waitFor(() => expect(result.current.tour.view?.action).toBeTruthy());
    await act(async () => result.current.tour.view!.action!.onClick());
    await waitFor(() => expect(saved()?.tourStep).toBe("gmail"));
    await waitFor(() => expect(result.current.tour.view?.action?.label).toBe("Try again"));
    expect(result.current.tour.view?.body).toMatch(/permission/);
  });

  it("draft_in_progress: waits and retries once", async () => {
    const pushDraft = vi
      .fn()
      .mockResolvedValueOnce({ ok: false, reason: "draft_in_progress" })
      .mockResolvedValueOnce({ ok: true, gmailDraftUrl: "u", created: true });
    const { saved } = setup(AT("gmail"), "contact=ranker_7&tour=gmail&connected=gmail", { pushDraft });
    await waitFor(() => expect(saved()?.tourStep).toBe("open"));
    expect(pushDraft).toHaveBeenCalledTimes(2);
  });

  it("push error: shows Try again, which pushes again", async () => {
    const pushDraft = vi
      .fn()
      .mockResolvedValueOnce({ ok: false, reason: "error" })
      .mockResolvedValueOnce({ ok: true, gmailDraftUrl: "u", created: true });
    const { result, saved, deps } = setup(AT("gmail"), "contact=ranker_7&tour=gmail&connected=gmail", { pushDraft });
    await waitFor(() => expect(result.current.tour.view?.body).toBe("Something went wrong putting it in your Gmail drafts."));
    await act(async () => result.current.tour.view!.action!.onClick());
    await waitFor(() => expect(saved()?.tourStep).toBe("open"));
    expect(deps.startOAuth).not.toHaveBeenCalled();
  });

  it("gmail step reloaded with Gmail already connected: Put it in Gmail pushes, no OAuth", async () => {
    const { result, deps, saved } = setup(AT("gmail"), "contact=ranker_7");
    await waitFor(() => expect(result.current.tour.view?.action?.label).toBe("Put it in Gmail"));
    expect(deps.pushDraft).not.toHaveBeenCalled();
    await act(async () => result.current.tour.view!.action!.onClick());
    await waitFor(() => expect(saved()?.tourStep).toBe("open"));
    expect(deps.pushDraft).toHaveBeenCalledWith("ranker_7");
    expect(deps.startOAuth).not.toHaveBeenCalled();
  });

  it("gmail step reloaded without Gmail: still Connect Gmail", async () => {
    const { result, deps } = setup(AT("gmail"), "contact=ranker_7", { gmailStatus: vi.fn(async () => ({ connected: false })) });
    await waitFor(() => expect(deps.gmailStatus).toHaveBeenCalled());
    await waitFor(() => expect(result.current.tour.view?.action?.label).toBe("Connect Gmail"));
  });

  it("a suppressed address: explains, no Try again, only Not now", async () => {
    const { result, deps } = setup(AT("gmail"), "contact=ranker_7&tour=gmail&connected=gmail", {
      pushDraft: vi.fn(async () => ({ ok: false as const, reason: "recipient_suppressed" as const })),
    });
    await waitFor(() => expect(result.current.tour.view?.body).toBe("Gmail won't take a draft to this address. It bounced before."));
    expect(result.current.tour.view?.action).toBeUndefined();
    expect(result.current.tour.view?.secondary?.label).toBe("Not now");
    expect(deps.pushDraft).toHaveBeenCalledTimes(1);
  });

  it("open: clicking the Gmail button completes the tour and shows You're all set briefly", async () => {
    const link = document.createElement("a");
    link.setAttribute("data-tour", "tour-gmail-button");
    link.href = "https://mail.google.com/d/1";
    link.target = "_blank";
    document.body.appendChild(link);
    let defaultPrevented: boolean | null = null;
    const watch = (e: Event) => {
      defaultPrevented = e.defaultPrevented;
      e.preventDefault(); // keep jsdom from navigating
    };
    try {
      const { result, saved } = setup(AT("open"), "contact=ranker_7", { allSetMs: 400 });
      await waitFor(() => expect(result.current.tour.view?.title).toBe("It's in your Gmail drafts"));
      window.addEventListener("click", watch);
      act(() => link.click());
      expect(defaultPrevented).toBe(false);
      expect(result.current.tour.view?.title).toBe("You're all set");
      expect(result.current.tour.view?.target).toBeNull();
      await waitFor(() => expect(saved()?.status).toBe("completed"));
      await waitFor(() => expect(result.current.tour.view).toBeNull());
    } finally {
      window.removeEventListener("click", watch);
      link.remove();
    }
  });
});

describe("useContactsTour holdSelection", () => {
  it("holds the page's auto-select while loading and while on the tour's contact", async () => {
    const { result } = setup(AT("review"), "contact=ranker_7");
    expect(result.current.tour.holdSelection).toBe(true);
    await waitFor(() => expect(result.current.tour.loaded).toBe(true));
    expect(result.current.tour.holdSelection).toBe(true);

    const done = setup({ ...AT("review"), status: "completed" }, "contact=ranker_7");
    await waitFor(() => expect(done.result.current.tour.loaded).toBe(true));
    expect(done.result.current.tour.holdSelection).toBe(false);
  });
});
