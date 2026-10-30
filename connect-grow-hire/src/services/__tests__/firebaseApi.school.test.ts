/** Find's school lookup (getUserOnboardingData) with Firestore mocked. */
import { describe, expect, it, vi } from "vitest";

const firestore = vi.hoisted(() => ({ getDoc: vi.fn() }));
vi.mock("firebase/firestore", async (orig) => ({
  ...(await orig<typeof import("firebase/firestore")>()),
  doc: vi.fn(() => ({})),
  getDoc: firestore.getDoc,
}));
vi.mock("@/lib/firebase", () => ({ db: {}, auth: { currentUser: null } }));

import { firebaseApi } from "@/services/firebaseApi";

const withDoc = (data: Record<string, unknown>) =>
  firestore.getDoc.mockResolvedValueOnce({ exists: () => true, data: () => data });

const resume = { resumeParsed: { education: { university: "University of Michigan" } } };
const confirmed = { onboarding: { profileConfirmedAt: "2026-10-20T00:00:00Z" } };

describe("getUserOnboardingData university", () => {
  it("uses the resume's school when nothing was confirmed", async () => {
    withDoc(resume);
    expect((await firebaseApi.getUserOnboardingData("u1")).university).toBe("University of Michigan");
  });

  it("respects a confirmed 'not in school' over the resume", async () => {
    withDoc({ ...resume, ...confirmed, universityOptOut: true, academics: { university: "" }, university: "" });
    const data = await firebaseApi.getUserOnboardingData("u1");
    expect(data.university).toBe("");
    expect(data.universityOptOut).toBe(true);
  });

  it("uses a school the user set after opting out", async () => {
    withDoc({ ...resume, ...confirmed, universityOptOut: true, university: "Santa Monica College" });
    expect((await firebaseApi.getUserOnboardingData("u1")).university).toBe("Santa Monica College");
  });

  it("prefers the confirmed school over the resume's", async () => {
    withDoc({ ...resume, ...confirmed, academics: { university: "UCLA" } });
    expect((await firebaseApi.getUserOnboardingData("u1")).university).toBe("UCLA");
  });

  it("leaves legacy opt-outs (no onboarding confirm) on the old chain", async () => {
    withDoc({ ...resume, universityOptOut: true });
    expect((await firebaseApi.getUserOnboardingData("u1")).university).toBe("University of Michigan");
  });
});
