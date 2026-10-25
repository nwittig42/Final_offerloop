import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { PersonProfileDialog } from "./PersonProfileDialog";
import { contactButtonTitle } from "./PersonCard";
import type { Person } from "./peopleModel";

const api = vi.hoisted(() => ({ createCoffeeChatPrep: vi.fn() }));
const credits = vi.hoisted(() => ({ balance: 100 }));
const toastFn = vi.hoisted(() => vi.fn());

vi.mock("@/services/api", () => ({ apiService: api }));
vi.mock("@/hooks/useCreditsView", () => ({ useCreditsView: () => credits }));
vi.mock("@/hooks/use-toast", () => ({ toast: toastFn }));

const person: Person = {
  id: "p1", source: "recommended", name: "Ada Lovelace", firstName: "Ada", lastName: "Lovelace",
  title: "Engineer", company: "Analytical Co", location: "London", school: "Cambridge",
  schools: ["Cambridge", "Oxford"], joinedYear: 2019,
  linkedinUrl: "https://www.linkedin.com/in/ada", photoUrl: "", email: "",
  reasons: ["Went to your school"], briefing: "Builds engines.", matchScore: 8.5, rank: 1, deckId: "d",
};

const bare: Person = {
  ...person, school: "", schools: [], joinedYear: null, linkedinUrl: "", reasons: [], briefing: "", matchScore: null,
};

function setup(over: Partial<React.ComponentProps<typeof PersonProfileDialog>> = {}) {
  const fns = {
    onContact: vi.fn(),
    onToggleBookmark: vi.fn(),
    onClose: vi.fn(),
    ensureSaved: vi.fn(async () => true),
  };
  const qc = new QueryClient();
  const invalidate = vi.spyOn(qc, "invalidateQueries");
  render(
    <QueryClientProvider client={qc}>
      <MemoryRouter>
        <PersonProfileDialog person={person} contactState="idle" isBookmarked={false} {...fns} {...over} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  return { ...fns, ...over, invalidate };
}

const openPrep = () => fireEvent.click(screen.getByRole("button", { name: "Meeting prep" }));

beforeEach(() => {
  api.createCoffeeChatPrep.mockReset();
  api.createCoffeeChatPrep.mockResolvedValue({ prepId: "prep1" });
  toastFn.mockReset();
  credits.balance = 100;
});

describe("PersonProfileDialog", () => {
  it("shows name, title at company, all schools and location", () => {
    setup();
    expect(screen.getByText("Ada Lovelace")).toBeInTheDocument();
    expect(screen.getByText("Engineer at Analytical Co")).toBeInTheDocument();
    expect(screen.getByText("Cambridge · Oxford · London")).toBeInTheDocument();
    expect(screen.getByText("Joined 2019")).toBeInTheDocument();
    expect(screen.getByText("Strong fit")).toBeInTheDocument();
    expect(screen.getByText("Why them")).toBeInTheDocument();
    expect(screen.getByText("Builds engines.")).toBeInTheDocument();
  });

  it("hides empty sections", () => {
    setup({ person: bare });
    expect(screen.queryByText("Why them")).toBeNull();
    expect(screen.queryByText(/Cambridge/)).toBeNull();
    expect(screen.queryByText(/Joined/)).toBeNull();
    expect(screen.queryByRole("button", { name: "Meeting prep" })).toBeNull();
    expect(screen.queryByRole("link", { name: /LinkedIn/ })).toBeNull();
    // Location alone still shows.
    expect(screen.getByText("London")).toBeInTheDocument();
  });

  it("falls back to school when schools is empty", () => {
    setup({ person: { ...bare, school: "Cambridge" } });
    expect(screen.getByText("Cambridge · London")).toBeInTheDocument();
  });

  it("primary button calls onContact when idle; shows Open in Contacts when drafted; disabled No email found when no_email", () => {
    const f = setup();
    fireEvent.click(screen.getByRole("button", { name: "See email & draft" }));
    expect(f.onContact).toHaveBeenCalledTimes(1);
  });

  it("drafted shows Open in Contacts linking to contactHref", () => {
    setup({ contactState: "drafted", contactHref: "/contacts?contact=abc" });
    expect(screen.getByRole("link", { name: "Open in Contacts" })).toHaveAttribute("href", "/contacts?contact=abc");
  });

  it("onOpenContact replaces the Open in Contacts navigation (the tour)", () => {
    const onOpenContact = vi.fn();
    setup({ contactState: "drafted", contactHref: "/contacts?contact=abc", onOpenContact });
    const link = screen.getByRole("link", { name: "Open in Contacts" });
    expect(link).toHaveAttribute("data-tour", "tour-profile-primary");
    expect(fireEvent.click(link)).toBe(false);
    expect(onOpenContact).toHaveBeenCalledTimes(1);
  });

  it("no_email shows a disabled No email found", () => {
    setup({ contactState: "no_email" });
    expect(screen.getByRole("button", { name: "No email found" })).toBeDisabled();
  });

  it("search people get Draft email; busy states are disabled", () => {
    setup({ person: { ...person, source: "search" }, contactState: "working" });
    expect(screen.getByRole("button", { name: "Writing…" })).toBeDisabled();
  });

  it("Save calls onToggleBookmark", () => {
    const f = setup();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(f.onToggleBookmark).toHaveBeenCalledTimes(1);
  });

  it("Meeting prep: cancel saves nothing and starts nothing", async () => {
    const f = setup();
    openPrep();
    expect(await screen.findByText("Prep for a meeting with Ada Lovelace?")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByText("Prep for a meeting with Ada Lovelace?")).toBeNull());
    expect(f.ensureSaved).not.toHaveBeenCalled();
    expect(api.createCoffeeChatPrep).not.toHaveBeenCalled();
  });

  it("Meeting prep: not enough credits saves nothing and starts nothing", async () => {
    credits.balance = 5;
    const f = setup();
    openPrep();
    fireEvent.click(await screen.findByRole("button", { name: "Run prep" }));
    await waitFor(() => expect(toastFn).toHaveBeenCalledWith(expect.objectContaining({ title: "Not enough credits" })));
    expect(f.ensureSaved).not.toHaveBeenCalled();
    expect(api.createCoffeeChatPrep).not.toHaveBeenCalled();
  });

  it("Meeting prep: confirm saves first, then starts the prep with the LinkedIn URL", async () => {
    const f = setup();
    openPrep();
    fireEvent.click(await screen.findByRole("button", { name: "Run prep" }));
    await waitFor(() => expect(api.createCoffeeChatPrep).toHaveBeenCalledWith({ linkedinUrl: person.linkedinUrl }));
    expect(f.ensureSaved).toHaveBeenCalledWith(person, { quiet: true });
    expect(vi.mocked(f.ensureSaved).mock.invocationCallOrder[0]).toBeLessThan(api.createCoffeeChatPrep.mock.invocationCallOrder[0]);
    await waitFor(() => expect(toastFn).toHaveBeenCalledWith(expect.objectContaining({ title: "Meeting prep started" })));
    expect(f.invalidate).toHaveBeenCalledWith({ queryKey: ["coffeeChatPreps"] });
  });

  it("Meeting prep: if saving fails, the prep doesn't start", async () => {
    const f = setup({ ensureSaved: vi.fn(async () => false) });
    openPrep();
    fireEvent.click(await screen.findByRole("button", { name: "Run prep" }));
    await waitFor(() => expect(f.ensureSaved).toHaveBeenCalled());
    expect(api.createCoffeeChatPrep).not.toHaveBeenCalled();
  });

  it("Meeting prep: an API error shows the failure toast", async () => {
    api.createCoffeeChatPrep.mockResolvedValue({ error: "Boom" });
    setup();
    openPrep();
    fireEvent.click(await screen.findByRole("button", { name: "Run prep" }));
    await waitFor(() => expect(toastFn).toHaveBeenCalledWith(expect.objectContaining({ title: "Meeting prep failed", description: "Boom" })));
  });

  it("shows the email as a mailto link when we have one", () => {
    setup({ email: "ada@analytical.co" });
    const link = screen.getByRole("link", { name: "ada@analytical.co" });
    expect(link).toHaveAttribute("href", "mailto:ada@analytical.co");
  });

  it("shows no email line without one", () => {
    setup({ email: "" });
    expect(screen.queryByRole("link", { name: /@/ })).toBeNull();
    expect(document.querySelector('a[href^="mailto:"]')).toBeNull();
  });

  it("no title: the dialog has no description and Radix doesn't warn", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    setup({ person: { ...bare, title: "" } });
    expect(screen.getByRole("dialog")).not.toHaveAttribute("aria-describedby");
    expect(warn.mock.calls.some((c) => String(c[0]).includes("Description"))).toBe(false);
    warn.mockRestore();
  });

  it("the primary contact button carries the card's cost title", () => {
    setup();
    expect(screen.getByRole("button", { name: "See email & draft" })).toHaveAttribute("title", contactButtonTitle(person));
    expect(contactButtonTitle(person)).toMatch(/8 credits/);
  });
});
