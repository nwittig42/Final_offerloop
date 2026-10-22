/**
 * OnboardingFlow v2 (Milestone 1), fully mocked: auth, orgs, the resume
 * source step and every service are fakes, so nothing touches Firebase or
 * the backend.
 */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";

const auth = vi.hoisted(() => ({
  completeOnboarding: vi.fn(async () => {}),
  refreshUser: vi.fn(async () => {}),
  signOut: vi.fn(async () => {}),
  user: { uid: "u1", email: "ana@umich.edu", name: "Ana Lee" } as Record<string, unknown> | null,
}));
const orgs = vi.hoisted(() => ({ value: { orgs: [] as unknown[], memberships: [] as string[], loading: false, isError: false } }));

vi.mock("@/contexts/FirebaseAuthContext", () => ({ useFirebaseAuth: () => auth }));
vi.mock("@/hooks/useOrgs", () => ({ useOrgs: () => orgs.value }));
const analytics = vi.hoisted(() => ({ trackFeatureActionCompleted: vi.fn(), trackFirstOutreachFunnel: vi.fn() }));
vi.mock("@/lib/analytics", () => analytics);
vi.mock("@/lib/firebase", () => ({ db: {}, auth: { currentUser: null } }));
vi.mock("@/services/api", () => ({ BACKEND_URL: "", extractDirection: vi.fn() }));
vi.mock("sonner", () => ({ toast: { error: vi.fn() } }));
vi.mock("@/data/universities", () => ({ UNIVERSITIES: ["University of Michigan"] }));
vi.mock("@/components/orgs/OrgMembershipEditor", () => ({
  OrgMembershipEditor: () => <div>org picker</div>,
}));
vi.mock("@/pages/OnboardingBuilder", () => ({ OnboardingBuilder: () => <div>builder</div> }));
// The real source step calls the parse/enrich APIs; this stub reports a parsed resume.
vi.mock("@/pages/OnboardingSource", () => ({
  OnboardingSource: ({ onNext }: { onNext: (d: unknown) => void }) => (
    <button
      type="button"
      onClick={() =>
        onNext({
          resumePrefill: null,
          linkedinPrefill: null,
          resolved: {
            name: "Ana Lee", firstName: "Ana", lastName: "Lee", email: "", phone: "",
            university: "", major: "", graduationYear: "",
          },
          resumeFile: null,
          linkedinUrl: "https://linkedin.com/in/ana",
          entryPath: "resume",
        })
      }
    >
      stub: resume parsed
    </button>
  ),
}));

import { OnboardingFlow } from "@/pages/OnboardingFlow";
import { ONBOARDING_PROGRESS_VERSION, type OnboardingProgress, type ProfileDraft } from "@/services/onboardingProgress";
import type { OnboardingServices } from "@/pages/onboardingServices";
import type { ParsedResume } from "@/utils/onboardingInference";

const parsedResume = {
  education: { university: "University of Michigan", major: "Economics", graduation: "May 2027" },
  experience: [{ title: "Summer Analyst", company: "Evercore" }],
  extracurriculars: [{ activity: "Finance club", organization: "Michigan Investment Group" }],
};

function makeServices(saved: OnboardingProgress | null = null, userDoc: Record<string, unknown> | null = null) {
  const services = {
    loadProgress: vi.fn(async () => saved),
    saveProgress: vi.fn(async () => {}),
    loadProfileSources: vi.fn(async () => ({ resumeParsed: parsedResume as ParsedResume | null, userDoc })),
    loadBuilderDraft: vi.fn(() => null),
    saveBuilderDraft: vi.fn(),
    extractDirection: vi.fn(async () => null),
    logEvent: vi.fn(),
  } satisfies OnboardingServices;
  return services;
}

const Where = () => {
  const loc = useLocation();
  return <div data-testid="where">{loc.pathname + loc.search}</div>;
};

function renderFlow(services: OnboardingServices, url = "/onboarding") {
  return render(
    <MemoryRouter initialEntries={[url]}>
      <Routes>
        <Route path="/onboarding" element={<OnboardingFlow onComplete={() => {}} services={services} />} />
        <Route path="*" element={<Where />} />
      </Routes>
    </MemoryRouter>,
  );
}

const saves = (s: ReturnType<typeof makeServices>) =>
  s.saveProgress.mock.calls.map((c) => (c as unknown as [string, OnboardingProgress])[1]);
const savedSteps = (s: ReturnType<typeof makeServices>) => saves(s).map((p) => p.step);
const completedWith = () => (auth.completeOnboarding.mock.calls[0] as unknown as [Record<string, any>])[0];

const baseDraft: ProfileDraft = {
  firstName: "Ana", lastName: "Lee", phone: "", university: "UCLA", major: "Math", graduationYear: "2026",
  roles: [], industries: [], targetCompanies: ["Google"], interests: [], notInSchool: false,
};

beforeEach(() => {
  vi.clearAllMocks();
  orgs.value = { orgs: [], memberships: [], loading: false, isError: false };
});

describe("OnboardingFlow", () => {
  it("starts a new user on the single intro and saves progress when they continue", async () => {
    const s = makeServices(null);
    renderFlow(s);
    expect(await screen.findByRole("button", { name: "Get started" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Get started" }));
    expect(await screen.findByText("stub: resume parsed")).toBeInTheDocument();
    expect(s.saveProgress).toHaveBeenCalledWith("u1", expect.objectContaining({ version: ONBOARDING_PROGRESS_VERSION, step: "source" }));
  });

  it("resumes at the confirm step with the saved draft after a refresh", async () => {
    renderFlow(makeServices({ version: ONBOARDING_PROGRESS_VERSION, step: "confirm", draft: baseDraft }));
    expect(await screen.findByLabelText("School")).toHaveValue("UCLA");
    expect(screen.queryByRole("button", { name: "Get started" })).not.toBeInTheDocument();
  });

  it("infers the profile from the saved parse, confirms it, completes and hands off to the walkthrough", async () => {
    const s = makeServices({ version: ONBOARDING_PROGRESS_VERSION, step: "source" });
    renderFlow(s, "/onboarding?returnTo=%2Fpricing");
    fireEvent.click(await screen.findByText("stub: resume parsed"));

    // Confirm is pre-filled from inference (school + grad year from the parse,
    // lane-derived roles and companies) with the name from the parse prefill.
    expect(await screen.findByLabelText("School")).toHaveValue("University of Michigan");
    expect(screen.getByLabelText("Grad year")).toHaveValue("2027");
    expect(screen.getByLabelText("First name")).toHaveValue("Ana");
    expect(screen.getByRole("list", { name: "Selected: Target companies" })).toHaveTextContent("Goldman Sachs");
    expect(savedSteps(s)).toContain("confirm");

    fireEvent.click(screen.getByRole("button", { name: "Looks good" }));
    await waitFor(() => expect(auth.completeOnboarding).toHaveBeenCalledTimes(1));

    const data = completedWith();
    expect(data.academics).toMatchObject({ university: "University of Michigan", major: "Economics", graduationYear: "2027" });
    expect(data.goals.dreamCompanies).toContain("Goldman Sachs");
    expect(data.goals.dreamCompaniesSource).toBe("onboarding_inferred");
    expect(data.dreamCompanies).toEqual(data.goals.dreamCompanies);
    // Nothing was edited, so every preference is recorded as accepted-inferred.
    expect(data.fieldProvenance).toMatchObject({ school: "onboarding_inferred", targetCompanies: "onboarding_inferred", careerTrack: "onboarding_inferred" });
    expect(data.targetRoles).toContain("Investment Banking Analyst");
    expect(data.careerTrack).toBe("Investment Banking");
    expect(data.isStudent).toBe(true);
    expect(data.profile.linkedinUrl).toBe("https://linkedin.com/in/ana");
    expect(data.profile.interests).toEqual(["Michigan Investment Group"]);
    expect(data.walkthrough).toMatchObject({ status: "pending" });
    expect(data.onboardingProgress).toBeNull();
    expect(data.onboarding.profileConfirmedAt).toEqual(expect.any(String));
    // Never written from the client: the rules reject credit/tier changes.
    expect(data).not.toHaveProperty("credits");
    expect(data).not.toHaveProperty("tier");

    expect(await screen.findByTestId("where")).toHaveTextContent("/get-started?returnTo=%2Fpricing");
    // Funnel start, without profile data.
    expect(analytics.trackFirstOutreachFunnel).toHaveBeenCalledWith("onboarding_completed", undefined, { entry_path: "resume", joined_org: false });
    expect(s.extractDirection).toHaveBeenCalledWith(expect.stringContaining("Economics"));
  });

  it("goes through the optional org step when orgs exist", async () => {
    orgs.value = { orgs: [{ id: "o1" }], memberships: [], loading: false, isError: false };
    const s = makeServices({ version: ONBOARDING_PROGRESS_VERSION, step: "source" });
    renderFlow(s);
    fireEvent.click(await screen.findByText("stub: resume parsed"));
    fireEvent.click(await screen.findByRole("button", { name: "Looks good" }));
    expect(await screen.findByText("org picker")).toBeInTheDocument();
    expect(savedSteps(s)).toContain("orgs");
    expect(auth.completeOnboarding).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Skip for now" }));
    await waitFor(() => expect(auth.completeOnboarding).toHaveBeenCalledTimes(1));
  });

  it("still reaches confirm when the parse read-back and LLM refinement both fail", async () => {
    const s = makeServices({ version: ONBOARDING_PROGRESS_VERSION, step: "source" });
    s.loadProfileSources.mockRejectedValueOnce(new Error("offline"));
    renderFlow(s);
    fireEvent.click(await screen.findByText("stub: resume parsed"));
    expect(await screen.findByLabelText("First name")).toHaveValue("Ana");
    expect(screen.getByLabelText("School")).toHaveValue("");
    expect(s.extractDirection).not.toHaveBeenCalled();
  });

  it("keeps going when a progress save fails", async () => {
    const s = makeServices(null);
    s.saveProgress.mockRejectedValue(new Error("permission-denied"));
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    renderFlow(s);
    fireEvent.click(await screen.findByRole("button", { name: "Get started" }));
    expect(await screen.findByText("stub: resume parsed")).toBeInTheDocument();
    warn.mockRestore();
  });

  it("keeps confirm-screen edits across a refresh", async () => {
    const s = makeServices({ version: ONBOARDING_PROGRESS_VERSION, step: "confirm", draft: baseDraft });
    renderFlow(s);
    fireEvent.change(await screen.findByLabelText("Major"), { target: { value: "Statistics" } });
    // Saved shortly after typing stops.
    await waitFor(() => expect(saves(s).some((p) => p.draft?.major === "Statistics")).toBe(true));
    const saved = saves(s).find((p) => p.draft?.major === "Statistics")!;
    expect(saved.step).toBe("confirm");
    expect(saved.draft?.edited).toContain("major");

    // "Refresh": a new mount restores the edit.
    renderFlow(makeServices(saved));
    expect((await screen.findAllByLabelText("Major")).at(-1)).toHaveValue("Statistics");
  });

  it("re-runs inference after a refresh mid-inference, keeping earlier edits", async () => {
    const prev: ProfileDraft = { ...baseDraft, university: "Michigan State", edited: ["university"] };
    const s = makeServices({ version: ONBOARDING_PROGRESS_VERSION, step: "confirm", entryPath: "resume", draft: prev, inferPending: true });
    renderFlow(s);
    // Inferred from the saved resume...
    expect(await screen.findByLabelText("Grad year")).toHaveValue("2027");
    expect(screen.getByRole("list", { name: "Selected: Target companies" })).toHaveTextContent("Goldman Sachs");
    // ...but the school the user typed wins over the resume's.
    expect(screen.getByLabelText("School")).toHaveValue("Michigan State");
    expect(s.loadProfileSources).toHaveBeenCalledTimes(1);
  });

  it("restores the org step after a refresh, and falls back to confirm when there are no orgs", async () => {
    orgs.value = { orgs: [{ id: "o1" }], memberships: ["o1"], loading: false, isError: false };
    renderFlow(makeServices({ version: ONBOARDING_PROGRESS_VERSION, step: "orgs", draft: baseDraft }));
    expect(await screen.findByText("org picker")).toBeInTheDocument();
    // Joined orgs are saved server-side, so the button reflects the membership.
    expect(screen.getByRole("button", { name: "Continue" })).toBeInTheDocument();
  });

  it("sends a restored org step with no orgs back to confirm", async () => {
    renderFlow(makeServices({ version: ONBOARDING_PROGRESS_VERSION, step: "orgs", draft: baseDraft }));
    expect(await screen.findByLabelText("School")).toHaveValue("UCLA");
    expect(screen.queryByText("org picker")).not.toBeInTheDocument();
  });

  it("never replaces preferences the user already saved with inferred ones", async () => {
    const userDoc = {
      goals: { dreamCompanies: ["Bain & Company"], careerTrack: "Management Consulting", recruitingSeason: "fall" },
      targetRoles: ["Strategy Analyst"],
    };
    const s = makeServices({ version: ONBOARDING_PROGRESS_VERSION, step: "source" }, userDoc);
    renderFlow(s);
    fireEvent.click(await screen.findByText("stub: resume parsed"));
    const companies = await screen.findByRole("list", { name: "Selected: Target companies" });
    expect(companies).toHaveTextContent("Bain & Company");
    expect(companies).not.toHaveTextContent("Goldman Sachs");

    fireEvent.click(screen.getByRole("button", { name: "Looks good" }));
    await waitFor(() => expect(auth.completeOnboarding).toHaveBeenCalledTimes(1));
    const data = completedWith();
    expect(data.goals.dreamCompanies).toEqual(["Bain & Company"]);
    expect(data.targetRoles).toEqual(["Strategy Analyst"]);
    // The resume says banking, but the user's own track stays.
    expect(data.careerTrack).toBe("Management Consulting");
    expect(data.goals.dreamCompaniesSource).toBe("explicit");
    expect(data.fieldProvenance).toMatchObject({ targetCompanies: "explicit", targetRoles: "explicit", careerTrack: "explicit", school: "onboarding_inferred" });
    // Only the fields onboarding sets; completeOnboarding merges goals field by field.
    expect(data.goals).not.toHaveProperty("recruitingSeason");
  });

  it("saves an explicit 'not in school' even when the resume names a school", async () => {
    const s = makeServices({ version: ONBOARDING_PROGRESS_VERSION, step: "source" });
    renderFlow(s);
    fireEvent.click(await screen.findByText("stub: resume parsed"));
    expect(await screen.findByLabelText("School")).toHaveValue("University of Michigan");
    fireEvent.click(screen.getByLabelText("I'm not in school, or mine isn't listed"));
    fireEvent.click(screen.getByRole("button", { name: "Looks good" }));
    await waitFor(() => expect(auth.completeOnboarding).toHaveBeenCalledTimes(1));
    const data = completedWith();
    expect(data.universityOptOut).toBe(true);
    expect(data.academics.university).toBe("");
    expect(data.university).toBe("");
    expect(data.fieldProvenance.school).toBe("explicit");
  });

  it("clears the builder draft once onboarding completes", async () => {
    const s = makeServices({ version: ONBOARDING_PROGRESS_VERSION, step: "confirm", draft: baseDraft });
    renderFlow(s);
    fireEvent.click(await screen.findByRole("button", { name: "Looks good" }));
    await waitFor(() => expect(s.saveBuilderDraft).toHaveBeenCalledWith("u1", null));
  });
});
