import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { PersonCard } from "./PersonCard";
import type { Person } from "./peopleModel";

const person: Person = {
  id: "p1", source: "recommended", name: "Ada Lovelace", firstName: "Ada", lastName: "Lovelace",
  title: "Engineer", company: "Analytical Co", location: "London", school: "", linkedinUrl: "https://www.linkedin.com/in/ada",
  photoUrl: "", email: "", reasons: [], briefing: "", matchScore: null, rank: 1, deckId: "d",
};

function setup(over: Partial<React.ComponentProps<typeof PersonCard>> = {}, p: Person = person) {
  const fns = { onOpen: vi.fn(), onContact: vi.fn(), onDismiss: vi.fn(), onToggleBookmark: vi.fn() };
  render(
    <MemoryRouter>
      <PersonCard person={p} contactState="idle" {...fns} {...over} />
    </MemoryRouter>,
  );
  return fns;
}

describe("PersonCard open", () => {
  it("clicking the card body calls onOpen", () => {
    const f = setup();
    fireEvent.click(screen.getByRole("button", { name: "Open Ada Lovelace" }));
    expect(f.onOpen).toHaveBeenCalledTimes(1);
  });

  it("clicking Contact calls onContact and not onOpen", () => {
    const f = setup();
    fireEvent.click(screen.getByRole("button", { name: "Contact" }));
    expect(f.onContact).toHaveBeenCalledTimes(1);
    expect(f.onOpen).not.toHaveBeenCalled();
  });

  it("bookmark and dismiss do not open the card", () => {
    const f = setup();
    fireEvent.click(screen.getByRole("button", { name: /Save Ada Lovelace/ }));
    fireEvent.click(screen.getByRole("button", { name: "Hide Ada Lovelace" }));
    expect(f.onToggleBookmark).toHaveBeenCalledTimes(1);
    expect(f.onDismiss).toHaveBeenCalledTimes(1);
    expect(f.onOpen).not.toHaveBeenCalled();
  });

  it("the LinkedIn link does not open the card", () => {
    const f = setup();
    fireEvent.click(screen.getByRole("link", { name: /LinkedIn/ }));
    expect(f.onOpen).not.toHaveBeenCalled();
  });

  it("the directory email link does not open the card", () => {
    const f = setup({}, { ...person, source: "directory", email: "ada@example.com" });
    fireEvent.click(screen.getByRole("link", { name: /ada@example.com/ }));
    expect(f.onOpen).not.toHaveBeenCalled();
  });

  it("the drafted See email and View contact links do not open the card", () => {
    const f = setup({ contactState: "drafted", emailHref: null });
    fireEvent.click(screen.getByRole("link", { name: /See email/ }));
    fireEvent.click(screen.getByRole("link", { name: "View contact" }));
    expect(f.onOpen).not.toHaveBeenCalled();
  });

  it("the drafted See email link with an external href does not open the card", () => {
    const f = setup({ contactState: "drafted", emailHref: "https://mail.google.com/x" });
    fireEvent.click(screen.getByRole("link", { name: /See email/ }));
    expect(f.onOpen).not.toHaveBeenCalled();
  });

  it("the no-email note does not open the card", () => {
    const f = setup({ contactState: "no_email" });
    fireEvent.click(screen.getByText("No email found"));
    expect(f.onOpen).not.toHaveBeenCalled();
  });

  it("Enter on the focused card calls onOpen; Enter on an inner button does not", () => {
    const f = setup();
    fireEvent.keyDown(screen.getByRole("button", { name: "Open Ada Lovelace" }), { key: "Enter" });
    expect(f.onOpen).toHaveBeenCalledTimes(1);
    fireEvent.keyDown(screen.getByRole("button", { name: "Contact" }), { key: "Enter" });
    expect(f.onOpen).toHaveBeenCalledTimes(1);
  });

  it("sample cards do not open", () => {
    const f = setup({}, { ...person, source: "sample" });
    expect(screen.queryByRole("button", { name: "Open Ada Lovelace" })).toBeNull();
    fireEvent.click(screen.getByText("Ada Lovelace"));
    expect(f.onOpen).not.toHaveBeenCalled();
  });

  it("there is no Info button", () => {
    setup();
    expect(screen.queryByRole("button", { name: "Info" })).toBeNull();
  });

  it("an openable card shows a keyboard focus ring", () => {
    setup();
    const card = screen.getByRole("button", { name: "Open Ada Lovelace" });
    for (const c of ["focus-visible:outline-none", "focus-visible:ring-2", "focus-visible:ring-[#0071E3]", "focus-visible:ring-offset-2"]) {
      expect(card.className).toContain(c);
    }
  });
});
