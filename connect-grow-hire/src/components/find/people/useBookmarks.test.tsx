import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, renderHook, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";

const auth = vi.hoisted(() => ({ user: { uid: "u1" } }));
vi.mock("@/contexts/FirebaseAuthContext", () => ({ useFirebaseAuth: () => auth }));
vi.mock("@/lib/firebase", () => ({ db: {}, auth: { currentUser: null } }));
const fs = vi.hoisted(() => ({
  setDoc: vi.fn(async (..._a: any[]) => {}),
  deleteDoc: vi.fn(async (..._a: any[]) => {}),
  doc: vi.fn((_db: any, ...segs: string[]) => ({ path: segs.join("/") })),
}));
vi.mock("firebase/firestore", () => fs);
const api = vi.hoisted(() => ({
  getContacts: vi.fn(async (..._a: any[]): Promise<any[]> => []),
  createContact: vi.fn(async (..._a: any[]) => "new1"),
  deleteContact: vi.fn(async (..._a: any[]) => {}),
}));
vi.mock("@/services/firebaseApi", () => ({ firebaseApi: api }));
const toast = vi.hoisted(() => vi.fn());
vi.mock("@/hooks/use-toast", () => ({ toast }));

import { useBookmarks } from "./useBookmarks";
import type { Person } from "./peopleModel";

const PERSON: Person = {
  id: "c9", source: "recommended", name: "Riley Chen", firstName: "Riley", lastName: "Chen",
  title: "Analyst", company: "Goldman Sachs", location: "NYC", school: "USC",
  linkedinUrl: "https://www.linkedin.com/in/riley-chen", photoUrl: "", email: "",
  reasons: [], briefing: "", matchScore: null, rank: 1, deckId: "d1",
};

function setup() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={qc}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
  return renderHook(() => useBookmarks(), { wrapper });
}

beforeEach(() => {
  vi.clearAllMocks();
  api.getContacts.mockResolvedValue([]);
});

describe("useBookmarks.ensureSaved", () => {
  it("resolves true without writing when the person is already a contact", async () => {
    api.getContacts.mockResolvedValue([{ id: "x1", linkedinUrl: "linkedin.com/in/riley-chen", firstName: "Riley" }]);
    const { result } = setup();
    await waitFor(() => expect(result.current.contacts).toHaveLength(1));
    let ok = false;
    await act(async () => { ok = await result.current.ensureSaved(PERSON); });
    expect(ok).toBe(true);
    expect(fs.setDoc).not.toHaveBeenCalled();
    expect(api.createContact).not.toHaveBeenCalled();
  });

  it("saves a recommended person to ranker_<id> and resolves true", async () => {
    const { result } = setup();
    await waitFor(() => expect(api.getContacts).toHaveBeenCalled());
    let ok = false;
    await act(async () => { ok = await result.current.ensureSaved(PERSON); });
    expect(ok).toBe(true);
    expect(fs.setDoc).toHaveBeenCalledTimes(1);
    const [ref, data] = fs.setDoc.mock.calls[0] as [{ path: string }, Record<string, any>];
    expect(ref.path.endsWith("contacts/ranker_c9")).toBe(true);
    expect(data.status).toBe("Saved");
  });

  it("never removes: a second call leaves the person saved", async () => {
    const { result } = setup();
    await waitFor(() => expect(api.getContacts).toHaveBeenCalled());
    await act(async () => { await result.current.ensureSaved(PERSON); });
    let ok = false;
    await act(async () => { ok = await result.current.ensureSaved(PERSON); });
    expect(ok).toBe(true);
    expect(result.current.contactFor(PERSON)).toBeTruthy();
    expect(api.deleteContact).not.toHaveBeenCalled();
    expect(fs.deleteDoc).not.toHaveBeenCalled();
  });

  it("resolves false when the write fails, and always toasts the failure", async () => {
    fs.setDoc.mockRejectedValueOnce(new Error("nope"));
    const { result } = setup();
    await waitFor(() => expect(api.getContacts).toHaveBeenCalled());
    let ok = true;
    await act(async () => { ok = await result.current.ensureSaved(PERSON, { quiet: true }); });
    expect(ok).toBe(false);
    expect(result.current.contactFor(PERSON)).toBeUndefined();
    expect(toast).toHaveBeenCalledWith(expect.objectContaining({ title: "Couldn't save" }));
  });

  it("quiet skips the Saved toast", async () => {
    const { result } = setup();
    await waitFor(() => expect(api.getContacts).toHaveBeenCalled());
    await act(async () => { await result.current.ensureSaved(PERSON, { quiet: true }); });
    expect(toast).not.toHaveBeenCalled();
  });
});
