import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

vi.mock("@/lib/firebase", () => ({ db: {}, auth: { currentUser: null } }));

import { FirstEmailReminder } from "./FirstEmailReminder";
import type { WalkthroughState } from "@/services/walkthrough";

const renderWith = (state: WalkthroughState | null, save = vi.fn(async () => {})) => {
  render(
    <MemoryRouter>
      <FirstEmailReminder uid="u1" load={async () => state} save={save} />
    </MemoryRouter>,
  );
  return save;
};

describe("FirstEmailReminder", () => {
  it("shows while the walkthrough is incomplete and links back to it", async () => {
    renderWith({ version: 1, status: "in_progress", step: "matches" });
    expect(await screen.findByText("Finish your first email")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Continue" })).toHaveAttribute("href", "/get-started");
  });

  it("mentions the saved draft", async () => {
    renderWith({
      version: 1, status: "drafted", step: "draft", selectedId: "p1",
      matches: [{ id: "p1", name: "Pat Kim", firstName: "Pat", title: "", company: "", school: "", location: "", linkedinUrl: "", photoUrl: "", reasons: [], briefing: "", deckId: "", rank: 0 }],
    });
    expect(await screen.findByText(/Your email to Pat is saved/)).toBeInTheDocument();
  });

  it("stays hidden for users without a walkthrough, finished ones, and after dismissing", async () => {
    for (const state of [
      null,
      { version: 1, status: "completed", step: "draft" },
      { version: 1, status: "in_progress", step: "matches", reminderDismissedAt: "t" },
    ] as Array<WalkthroughState | null>) {
      const { unmount } = render(
        <MemoryRouter>
          <FirstEmailReminder uid="u1" load={async () => state} save={vi.fn()} />
        </MemoryRouter>,
      );
      await new Promise((r) => setTimeout(r, 0));
      expect(screen.queryByText("Finish your first email")).not.toBeInTheDocument();
      unmount();
    }
  });

  it("dismissing hides it and remembers that", async () => {
    const save = renderWith({ version: 1, status: "in_progress", step: "matches" });
    fireEvent.click(await screen.findByRole("button", { name: "Dismiss reminder" }));
    expect(screen.queryByText("Finish your first email")).not.toBeInTheDocument();
    await waitFor(() => expect(save).toHaveBeenCalledWith("u1", expect.objectContaining({ reminderDismissedAt: expect.any(String), status: "in_progress" })));
  });
});
