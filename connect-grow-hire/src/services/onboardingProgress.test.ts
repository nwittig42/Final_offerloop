import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const firestore = vi.hoisted(() => ({
  doc: vi.fn((_db: unknown, ...path: string[]) => ({ path: path.join("/") })),
  getDoc: vi.fn(),
  updateDoc: vi.fn(async () => {}),
}));
vi.mock("firebase/firestore", () => firestore);
vi.mock("@/lib/firebase", () => ({ db: {}, auth: { currentUser: null } }));

import {
  BUILDER_DRAFT_VERSION,
  ONBOARDING_PROGRESS_VERSION,
  loadBuilderDraft,
  loadOnboardingProgress,
  saveBuilderDraft,
  resumeStep,
  saveOnboardingProgress,
  type ProfileDraft,
} from "./onboardingProgress";

const draft: ProfileDraft = {
  firstName: "Ana",
  lastName: "Lee",
  phone: "",
  university: "UCLA",
  major: "Economics",
  graduationYear: "2027",
  roles: [],
  industries: [],
  targetCompanies: [],
  interests: [],
  notInSchool: false,
};

describe("resumeStep", () => {
  const v = ONBOARDING_PROGRESS_VERSION;

  it("starts new users at the intro", () => {
    expect(resumeStep(null)).toBe("intro");
    expect(resumeStep(undefined)).toBe("intro");
  });

  it("resumes at the saved step", () => {
    expect(resumeStep({ version: v, step: "source" })).toBe("source");
    expect(resumeStep({ version: v, step: "builder" })).toBe("builder");
    expect(resumeStep({ version: v, step: "confirm", draft })).toBe("confirm");
    expect(resumeStep({ version: v, step: "orgs", draft })).toBe("orgs");
  });

  it("falls back to the source step when confirm/orgs lost their draft and no resume step was recorded", () => {
    expect(resumeStep({ version: v, step: "confirm" })).toBe("source");
    expect(resumeStep({ version: v, step: "orgs" })).toBe("source");
  });

  it("returns to confirm (to re-run inference) when the resume was already submitted", () => {
    expect(resumeStep({ version: v, step: "confirm", entryPath: "resume", inferPending: true })).toBe("confirm");
    expect(resumeStep({ version: v, step: "orgs", entryPath: "linkedin" })).toBe("confirm");
  });

  it("restarts on an older progress version or an unknown step", () => {
    expect(resumeStep({ version: v - 1, step: "confirm", draft })).toBe("intro");
    expect(resumeStep({ version: v, step: "slides" as never })).toBe("intro");
  });
});

describe("persistence", () => {
  beforeEach(() => vi.clearAllMocks());

  it("writes only the onboardingProgress field on the user's own doc, without undefined values", async () => {
    await saveOnboardingProgress("u1", { version: ONBOARDING_PROGRESS_VERSION, step: "source", linkedinUrl: undefined });
    expect(firestore.doc).toHaveBeenCalledWith({}, "users", "u1");
    const [, payload] = firestore.updateDoc.mock.calls[0] as unknown as [unknown, Record<string, any>];
    expect(Object.keys(payload)).toEqual(["onboardingProgress"]);
    expect(payload.onboardingProgress).toMatchObject({ version: ONBOARDING_PROGRESS_VERSION, step: "source" });
    expect("linkedinUrl" in payload.onboardingProgress).toBe(false);
    expect(typeof payload.onboardingProgress.updatedAt).toBe("string");
  });

  it("loads progress and the saved resume parse from the user doc", async () => {
    firestore.getDoc.mockResolvedValueOnce({
      exists: () => true,
      data: () => ({
        onboardingProgress: { version: ONBOARDING_PROGRESS_VERSION, step: "confirm", draft },
        resumeParsed: { name: "Ana Lee" },
      }),
    });
    const out = await loadOnboardingProgress("u1");
    expect(out.progress?.step).toBe("confirm");
    expect(out.resumeParsed).toEqual({ name: "Ana Lee" });
  });

  it("returns nulls for a missing user doc", async () => {
    firestore.getDoc.mockResolvedValueOnce({ exists: () => false, data: () => undefined });
    expect(await loadOnboardingProgress("u1")).toEqual({ progress: null, resumeParsed: null });
  });
});

// In-memory stand-in: Node 26's own (disabled) localStorage global hides
// jsdom's in this test environment.
function memoryStorage(): Storage {
  const m = new Map<string, string>();
  return {
    get length() { return m.size; },
    clear: () => m.clear(),
    getItem: (k) => m.get(k) ?? null,
    key: (i) => Array.from(m.keys())[i] ?? null,
    removeItem: (k) => void m.delete(k),
    setItem: (k, v) => void m.set(k, String(v)),
  };
}

describe("builder draft (localStorage)", () => {
  beforeEach(() => vi.stubGlobal("localStorage", memoryStorage()));
  afterEach(() => vi.unstubAllGlobals());
  const draft = {
    version: BUILDER_DRAFT_VERSION,
    sections: { education: "USC, Econ, 2027" },
    prompt: "",
    history: [],
    resume: null,
    html: "",
  };

  it("round-trips per user and clears with null", () => {
    saveBuilderDraft("u1", draft);
    expect(loadBuilderDraft("u1")).toEqual(draft);
    expect(loadBuilderDraft("u2")).toBeNull();
    saveBuilderDraft("u1", null);
    expect(loadBuilderDraft("u1")).toBeNull();
  });

  it("ignores corrupt or old-version drafts", () => {
    localStorage.setItem("offerloop:onboarding-builder:u1", "{not json");
    expect(loadBuilderDraft("u1")).toBeNull();
    localStorage.setItem("offerloop:onboarding-builder:u1", JSON.stringify({ ...draft, version: 0 }));
    expect(loadBuilderDraft("u1")).toBeNull();
  });

  it("never throws when storage is unavailable", () => {
    vi.stubGlobal("localStorage", {
      ...memoryStorage(),
      setItem: () => {
        throw new Error("QuotaExceededError");
      },
    });
    expect(() => saveBuilderDraft("u1", draft)).not.toThrow();
    vi.stubGlobal("localStorage", undefined);
    expect(() => saveBuilderDraft("u1", draft)).not.toThrow();
    expect(loadBuilderDraft("u1")).toBeNull();
  });
});
