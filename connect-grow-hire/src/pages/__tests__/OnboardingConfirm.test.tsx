import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, within } from "@testing-library/react";

vi.mock("@/data/universities", () => ({ UNIVERSITIES: ["University of Michigan", "UCLA"] }));

import { OnboardingConfirm } from "@/pages/OnboardingConfirm";
import type { ProfileDraft } from "@/services/onboardingProgress";

const base: ProfileDraft = {
  firstName: "Ana",
  lastName: "Lee",
  phone: "555",
  university: "University of Michigan",
  major: "Economics",
  graduationYear: "2027",
  roles: ["Investment Banking Analyst"],
  industries: ["Finance"],
  targetCompanies: ["Goldman Sachs", "Evercore"],
  interests: [],
  notInSchool: false,
};

const setup = (initial: Partial<ProfileDraft> = {}) => {
  const onConfirm = vi.fn();
  render(<OnboardingConfirm initial={{ ...base, ...initial }} onConfirm={onConfirm} />);
  return { onConfirm };
};

describe("OnboardingConfirm", () => {
  it("shows the inferred profile and confirms it unchanged in one click", () => {
    const { onConfirm } = setup();
    expect(screen.getByLabelText("School")).toHaveValue("University of Michigan");
    expect(screen.getByRole("list", { name: "Selected: Target companies" })).toHaveTextContent("Goldman Sachs");
    fireEvent.click(screen.getByRole("button", { name: "Looks good" }));
    expect(onConfirm).toHaveBeenCalledWith(base);
  });

  it("lets the user correct the school, major and grad year", () => {
    const { onConfirm } = setup();
    fireEvent.change(screen.getByLabelText("School"), { target: { value: "  UCLA " } });
    fireEvent.change(screen.getByLabelText("Major"), { target: { value: "Math" } });
    fireEvent.change(screen.getByLabelText("Grad year"), { target: { value: "2026" } });
    fireEvent.click(screen.getByRole("button", { name: "Looks good" }));
    expect(onConfirm).toHaveBeenCalledWith({
      ...base,
      university: "UCLA",
      major: "Math",
      graduationYear: "2026",
      // Marked as the user's own, so later inference never replaces them.
      edited: ["university", "major", "graduationYear"],
    });
  });

  it("adds and removes chips", () => {
    const { onConfirm } = setup();
    fireEvent.click(screen.getByRole("button", { name: "Remove Evercore" }));
    const input = screen.getByLabelText("Target companies");
    fireEvent.change(input, { target: { value: "Lazard" } });
    fireEvent.keyDown(input, { key: "Enter" });
    // Duplicates (any case) are ignored.
    fireEvent.change(input, { target: { value: "goldman sachs" } });
    fireEvent.click(screen.getByRole("button", { name: "Add to Target companies" }));
    expect(within(screen.getByRole("list", { name: "Selected: Target companies" })).getAllByRole("listitem")).toHaveLength(2);
    fireEvent.click(screen.getByRole("button", { name: "Looks good" }));
    expect(onConfirm.mock.calls[0][0].targetCompanies).toEqual(["Goldman Sachs", "Lazard"]);
  });

  it("requires a school unless the user is not in school", () => {
    const { onConfirm } = setup({ university: "" });
    fireEvent.click(screen.getByRole("button", { name: "Looks good" }));
    expect(onConfirm).not.toHaveBeenCalled();
    expect(screen.getByText(/Add your school/)).toBeInTheDocument();

    fireEvent.click(screen.getByLabelText(/I'm not in school/));
    fireEvent.click(screen.getByRole("button", { name: "Looks good" }));
    expect(onConfirm).toHaveBeenCalledWith(expect.objectContaining({ notInSchool: true, university: "" }));
  });

  it("rejects a grad year that isn't 4 digits", () => {
    const { onConfirm } = setup({ graduationYear: "27" });
    fireEvent.click(screen.getByRole("button", { name: "Looks good" }));
    expect(onConfirm).not.toHaveBeenCalled();
    expect(screen.getByText("Use a 4-digit year.")).toBeInTheDocument();
  });

  it("tells the user when the resume couldn't be read", () => {
    render(<OnboardingConfirm initial={{ ...base, university: "" }} onConfirm={vi.fn()} sparse />);
    expect(screen.getByRole("status")).toHaveTextContent(/couldn't read much/);
  });
});
