import { describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import { act, renderHook, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

vi.mock("@/lib/firebase", () => ({ db: {}, auth: { currentUser: null } }));

import { useFindWalkthrough } from "./useFindWalkthrough";
import { WALKTHROUGH_VERSION, type WalkthroughState } from "@/services/walkthrough";

const PENDING: WalkthroughState = { version: WALKTHROUGH_VERSION, status: "pending", step: "matches" };

function setup(saved: WalkthroughState | null, url = "/find") {
  const load = vi.fn(async (_uid: string): Promise<WalkthroughState | null> => saved);
  const save = vi.fn(async (_uid: string, _state: WalkthroughState) => {});
  const wrapper = ({ children }: { children: ReactNode }) => (
    <MemoryRouter initialEntries={[url]}>{children}</MemoryRouter>
  );
  const hook = renderHook(() => useFindWalkthrough("u1", { load, save }), { wrapper });
  return { ...hook, load, save };
}

describe("useFindWalkthrough", () => {
  it("opens automatically for a fresh pending walkthrough", async () => {
    const { result } = setup(PENDING);
    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(result.current.open).toBe(true);
    expect(result.current.state).toEqual(PENDING);
  });

  it("does not auto-open after closedAt or skippedAt", async () => {
    for (const saved of [{ ...PENDING, closedAt: "t0" }, { ...PENDING, skippedAt: "t0" }]) {
      const { result, unmount } = setup(saved);
      await waitFor(() => expect(result.current.loaded).toBe(true));
      expect(result.current.open).toBe(false);
      unmount();
    }
  });

  it("opens on /find?walkthrough=1 even after closedAt", async () => {
    const { result } = setup({ ...PENDING, closedAt: "t0" }, "/find?walkthrough=1");
    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(result.current.open).toBe(true);
  });

  it('exit("skip") saves skippedAt and closes', async () => {
    const { result, save } = setup(PENDING);
    await waitFor(() => expect(result.current.open).toBe(true));
    act(() => result.current.exit("skip"));
    expect(result.current.open).toBe(false);
    await waitFor(() =>
      expect(save).toHaveBeenCalledWith("u1", expect.objectContaining({ skippedAt: expect.any(String), status: "pending" })),
    );
    expect(save.mock.calls[0][1].closedAt).toBeUndefined();
  });

  it('exit("later") saves closedAt and closes', async () => {
    const { result, save } = setup(PENDING);
    await waitFor(() => expect(result.current.open).toBe(true));
    act(() => result.current.exit("later"));
    expect(result.current.open).toBe(false);
    await waitFor(() => expect(save).toHaveBeenCalledWith("u1", expect.objectContaining({ closedAt: expect.any(String) })));
    expect(save.mock.calls[0][1].skippedAt).toBeUndefined();
  });

  it('exit("later") keeps an existing closedAt', async () => {
    const { result, save } = setup({ ...PENDING, closedAt: "t0" }, "/find?walkthrough=1");
    await waitFor(() => expect(result.current.open).toBe(true));
    act(() => result.current.exit("later"));
    await waitFor(() => expect(save).toHaveBeenCalled());
    expect(save.mock.calls[0][1].closedAt).toBe("t0");
  });

  it("exit saves on top of the latest saved progress, not the state from when Find loaded", async () => {
    const { result, save, load } = setup(PENDING);
    await waitFor(() => expect(result.current.open).toBe(true));
    // The walkthrough moved on while the pop-up was open.
    load.mockResolvedValue({ ...PENDING, status: "drafted", step: "draft", selectedId: "p1", contactId: "ranker_p1" });
    act(() => result.current.exit("find"));
    await waitFor(() => expect(save).toHaveBeenCalled());
    expect(save.mock.calls[0][1]).toMatchObject({ status: "drafted", step: "draft", contactId: "ranker_p1", closedAt: expect.any(String) });
    expect(result.current.state).toMatchObject({ status: "drafted" });
  });

  it("holdPaidLookups is true until loaded and while open, false after closing", async () => {
    const { result } = setup(PENDING);
    expect(result.current.loaded).toBe(false);
    expect(result.current.holdPaidLookups).toBe(true);
    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(result.current.holdPaidLookups).toBe(true);
    act(() => result.current.exit("later"));
    expect(result.current.holdPaidLookups).toBe(false);
  });

  it("openNow opens it again", async () => {
    const { result } = setup({ ...PENDING, closedAt: "t0" });
    await waitFor(() => expect(result.current.loaded).toBe(true));
    act(() => result.current.openNow());
    expect(result.current.open).toBe(true);
    expect(result.current.holdPaidLookups).toBe(true);
  });

  it("no walkthrough on the user doc: loaded, closed, holdPaidLookups false", async () => {
    const { result } = setup(null, "/find?walkthrough=1");
    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(result.current.state).toBeNull();
    expect(result.current.open).toBe(false);
    expect(result.current.holdPaidLookups).toBe(false);
  });

  it("a failed load counts as no walkthrough", async () => {
    const load = vi.fn(async () => {
      throw new Error("offline");
    });
    const wrapper = ({ children }: { children: ReactNode }) => <MemoryRouter>{children}</MemoryRouter>;
    const { result } = renderHook(() => useFindWalkthrough("u1", { load, save: vi.fn() }), { wrapper });
    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(result.current.open).toBe(false);
    expect(result.current.holdPaidLookups).toBe(false);
  });

  it("a completed walkthrough doesn't reopen on /find?walkthrough=1", async () => {
    const { result } = setup({ ...PENDING, status: "completed" }, "/find?walkthrough=1");
    await waitFor(() => expect(result.current.loaded).toBe(true));
    expect(result.current.open).toBe(false);
  });
});
