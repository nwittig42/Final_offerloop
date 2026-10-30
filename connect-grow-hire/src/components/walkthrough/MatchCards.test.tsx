import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";
import { MatchGallery } from "./MatchCards";
import type { WalkthroughMatch } from "@/services/walkthrough";

const match = (id: string, extra: Partial<WalkthroughMatch> = {}): WalkthroughMatch => ({
  id, name: `Pat ${id}`, firstName: "Pat", title: "Analyst", company: `Firm ${id}`, school: "", location: "",
  linkedinUrl: "", photoUrl: "", reasons: [], briefing: "", deckId: "d", rank: 0, ...extra,
});

function renderGallery(matches: WalkthroughMatch[], props: { selectedId?: string; unavailable?: string[] } = {}) {
  const onSelect = vi.fn();
  const { container } = render(
    <MatchGallery
      matches={matches}
      reasonFor={(m) => `Reason for ${m.name}`}
      selectedId={props.selectedId}
      unavailableIds={new Set(props.unavailable || [])}
      onSelect={onSelect}
    />,
  );
  return { onSelect, container };
}

describe("MatchGallery", () => {
  it("renders one profile card per match: name, role, school, reason", () => {
    renderGallery([match("1", { school: "University of Michigan" }), match("2"), match("3")]);
    const items = within(screen.getByRole("list", { name: "Your matches" })).getAllByRole("listitem");
    expect(items).toHaveLength(3);
    const first = within(items[0]);
    expect(first.getByText("Pat 1")).toBeInTheDocument();
    expect(first.getByText("Analyst at Firm 1")).toBeInTheDocument();
    expect(first.getByText("University of Michigan")).toBeInTheDocument();
    expect(first.getByText("Reason for Pat 1")).toBeInTheDocument();
    expect(first.getByText("No. 01")).toBeInTheDocument();
  });

  it("the whole card is one button that selects the person", () => {
    const m = match("1");
    const { onSelect } = renderGallery([m, match("2"), match("3")]);
    fireEvent.click(screen.getByRole("button", { name: "Pat 1, Analyst at Firm 1" }));
    expect(onSelect).toHaveBeenCalledWith(m);
  });

  it("marks the selected person", () => {
    renderGallery([match("1"), match("2"), match("3")], { selectedId: "2" });
    expect(screen.getByRole("button", { name: /Pat 2/ })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: /Pat 1/ })).toHaveAttribute("aria-pressed", "false");
    expect(within(screen.getByRole("button", { name: /Pat 2/ })).getByText("Selected")).toBeInTheDocument();
  });

  it("disables people without an email", () => {
    const { onSelect } = renderGallery([match("1"), match("2"), match("3")], { unavailable: ["3"] });
    const card = screen.getByRole("button", { name: /Pat 3/ });
    expect(card).toBeDisabled();
    expect(within(card).getByText("No email found")).toBeInTheDocument();
    fireEvent.click(card);
    expect(onSelect).not.toHaveBeenCalled();
  });

  it("shows the photo, and falls back to initials when it fails to load", () => {
    const { container } = renderGallery([match("1", { name: "Riley Sample", photoUrl: "https://example.com/p.jpg" }), match("2"), match("3")]);
    const img = container.querySelector("img")!;
    expect(img).toHaveAttribute("src", "https://example.com/p.jpg");
    fireEvent.error(img);
    expect(container.querySelector("img")).toBeNull();
    expect(screen.getByText("RS")).toBeInTheDocument();
  });

  it("lays out as many columns as there are cards, between 3 and 4", () => {
    const { container } = renderGallery([match("1"), match("2"), match("3"), match("4")]);
    expect((container.querySelector(".wt-gallery") as HTMLElement).style.getPropertyValue("--wt-cols")).toBe("4");
  });
});
