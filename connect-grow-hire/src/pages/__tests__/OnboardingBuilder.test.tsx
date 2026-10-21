/** Resume builder draft persistence; the builder APIs are mocked. */
import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

vi.mock("@/services/api", () => ({
  generateResumeBuilder: vi.fn(),
  finalizeResumeBuilder: vi.fn(),
  downloadResumeBuilderPdf: vi.fn(),
  downloadPdfBlob: vi.fn(),
}));

import { OnboardingBuilder } from "@/pages/OnboardingBuilder";
import { BUILDER_DRAFT_VERSION, type BuilderDraft } from "@/services/onboardingProgress";

describe("OnboardingBuilder draft", () => {
  it("reports typed sections so they can be saved", () => {
    const onDraftChange = vi.fn();
    render(<OnboardingBuilder onComplete={() => {}} submitting={false} onDraftChange={onDraftChange} />);
    fireEvent.change(screen.getByLabelText("Education"), { target: { value: "USC, Econ, 2027" } });
    const last = onDraftChange.mock.calls.at(-1)?.[0] as BuilderDraft;
    expect(last.sections.education).toBe("USC, Econ, 2027");
    expect(last.version).toBe(BUILDER_DRAFT_VERSION);
  });

  it("restores saved sections after a refresh", () => {
    const saved: BuilderDraft = {
      version: BUILDER_DRAFT_VERSION,
      sections: { education: "USC, Econ, 2027", experience: "Intern at Evercore" },
      prompt: "",
      history: [],
      resume: null,
      html: "",
    };
    render(<OnboardingBuilder onComplete={() => {}} submitting={false} loadDraft={() => saved} />);
    expect(screen.getByLabelText("Education")).toHaveValue("USC, Econ, 2027");
    expect(screen.getByLabelText("Work experience")).toHaveValue("Intern at Evercore");
  });

  it("reports null when there's nothing worth keeping", () => {
    const onDraftChange = vi.fn();
    render(<OnboardingBuilder onComplete={() => {}} submitting={false} onDraftChange={onDraftChange} />);
    expect(onDraftChange).toHaveBeenLastCalledWith(null);
  });
});
