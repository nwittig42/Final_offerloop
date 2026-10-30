import { describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

vi.mock("@/lib/firebase", () => ({ db: {}, auth: { currentUser: null } }));

import { useFirstEmailTour } from "./useFirstEmailTour";
import { WALKTHROUGH_VERSION, type WalkthroughState } from "@/services/walkthrough";

const PENDING: WalkthroughState = { version: WALKTHROUGH_VERSION, status: "pending", step: "matches", tourStep: "pick" };

function setup(saved: WalkthroughState | null) {
  // The fake "database": load returns what save last wrote.
  let db = saved;
  const load = vi.fn(async (_uid: string): Promise<WalkthroughState | null> => db);
  const save = vi.fn(async (_uid: string, s: WalkthroughState) => {
    db = s;
  });
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  );
  const hook = renderHook(() => useFirstEmailTour("u1", { load, save }), { wrapper });
  return { ...hook, load, save, client };
}

describe("useFirstEmailTour", () => {
  it("is active for a fresh pending walkthrough, at the pick step", async () => {
    const { result } = setup(PENDING);
    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(result.current.active).toBe(true);
    expect(result.current.step).toBe("pick");
  });

  it("is inactive after skip or close", async () => {
    const { result, save } = setup(PENDING);
    await waitFor(() => expect(result.current.loaded).toBe(true));
    await act(() => result.current.skip());
    await waitFor(() => expect(result.current.active).toBe(false));
    expect(save.mock.calls[0][1].skippedAt).toBeTruthy();

    const b = setup(PENDING);
    await waitFor(() => expect(b.result.current.loaded).toBe(true));
    await act(() => b.result.current.close());
    await waitFor(() => expect(b.result.current.active).toBe(false));
    expect(b.save.mock.calls[0][1].closedAt).toBeTruthy();
  });

  it("resume makes it active again at the saved step", async () => {
    const { result } = setup({ ...PENDING, status: "in_progress", tourStep: "review", closedAt: "t0" });
    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(result.current.active).toBe(false);
    await act(() => result.current.resume());
    await waitFor(() => expect(result.current.active).toBe(true));
    expect(result.current.step).toBe("review");
    expect(result.current.state?.closedAt).toBeUndefined();
  });

  it("goTo saves the step and contact, and marks it in progress", async () => {
    const { result, save } = setup(PENDING);
    await waitFor(() => expect(result.current.loaded).toBe(true));
    await act(() => result.current.goTo("review", { tourContactId: "ranker_7" }));
    expect(save).toHaveBeenCalledTimes(1);
    expect(save.mock.calls[0][1]).toMatchObject({ status: "in_progress", tourStep: "review", tourContactId: "ranker_7" });
    await waitFor(() => expect(result.current.step).toBe("review"));
  });

  it("re-reads the latest state before writing, so other changes survive", async () => {
    const { result, load, save } = setup(PENDING);
    await waitFor(() => expect(result.current.loaded).toBe(true));
    load.mockResolvedValueOnce({ ...PENDING, reminderDismissedAt: "r0" });
    await act(() => result.current.goTo("draft"));
    expect(save.mock.calls[0][1]).toMatchObject({ tourStep: "draft", reminderDismissedAt: "r0" });
  });

  it("complete makes it inactive", async () => {
    const { result, save } = setup(PENDING);
    await waitFor(() => expect(result.current.loaded).toBe(true));
    await act(() => result.current.complete());
    expect(save.mock.calls[0][1].status).toBe("completed");
    await waitFor(() => expect(result.current.active).toBe(false));
    expect(result.current.step).toBeNull();
  });

  it("is inactive with no walkthrough doc, and writes nothing", async () => {
    const { result, save } = setup(null);
    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(result.current.active).toBe(false);
    expect(result.current.state).toBeNull();
    await act(() => result.current.goTo("draft"));
    expect(save).not.toHaveBeenCalled();
  });

  it("shares state through the query cache", async () => {
    const { result, client } = setup(PENDING);
    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(client.getQueryData(["walkthrough", "u1"])).toEqual(PENDING);
  });
});
