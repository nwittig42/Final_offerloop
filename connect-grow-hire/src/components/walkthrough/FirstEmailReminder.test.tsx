import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";

vi.mock("@/lib/firebase", () => ({ db: {}, auth: { currentUser: null } }));

import { FirstEmailReminder } from "./FirstEmailReminder";
import type { WalkthroughState } from "@/services/walkthrough";

const renderWith = (state: WalkthroughState | null, save = vi.fn(async () => {}), onContinue = vi.fn()) => {
  render(
    <MemoryRouter>
      <FirstEmailReminder uid="u1" state={state} onContinue={onContinue} save={save} />
    </MemoryRouter>,
  );
  return save;
};

describe("FirstEmailReminder", () => {
  it("shows while the walkthrough is incomplete; Continue resumes the tour", async () => {
    const onContinue = vi.fn();
    renderWith({ version: 1, status: "in_progress", step: "matches" }, vi.fn(async () => {}), onContinue);
    expect(await screen.findByText("Finish your first email")).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Continue" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Continue" }));
    expect(onContinue).toHaveBeenCalledTimes(1);
  });

  it("mentions the saved draft", async () => {
    renderWith({
      version: 1, status: "drafted", step: "draft", selectedId: "p1",
      matches: [{ id: "p1", name: "Pat Kim", firstName: "Pat", title: "", company: "", school: "", location: "", linkedinUrl: "", photoUrl: "", reasons: [], briefing: "", deckId: "", rank: 0 }],
    });
    expect(await screen.findByText(/Your email to Pat is saved/)).toBeInTheDocument();
  });

  it("mentions the tour's saved draft", async () => {
    renderWith({ version: 1, status: "in_progress", step: "matches", tourStep: "gmail", tourContactId: "ranker_p1", tourPersonFirstName: "Sam" });
    expect(await screen.findByText(/Your email to Sam is saved/)).toBeInTheDocument();
  });

  it("no saved-draft line while the tour is still picking (a picked person had no email)", async () => {
    renderWith({ version: 1, status: "in_progress", step: "matches", tourStep: "pick", tourContactId: "ranker_p1", tourPersonFirstName: "Sam" });
    expect(await screen.findByText(/We picked a few people/)).toBeInTheDocument();
  });

  it("stays hidden for users without a walkthrough, finished ones, and after dismissing", async () => {
    for (const state of [
      null,
      { version: 1, status: "completed", step: "draft" },
      { version: 1, status: "in_progress", step: "matches", reminderDismissedAt: "t" },
    ] as Array<WalkthroughState | null>) {
      const { unmount } = render(
        <MemoryRouter>
          <FirstEmailReminder uid="u1" state={state} onContinue={vi.fn()} save={vi.fn()} />
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
