/**
 * The ranker calls the walkthrough makes: no paid prefetch or dig on the
 * deck, no Gmail draft on outreach. Find's calls are unchanged. fetch is
 * faked; nothing leaves the process.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { apiService } from "@/services/api";

const calls: Array<{ url: string; body: any }> = [];

beforeEach(() => {
  calls.length = 0;
  vi.spyOn(apiService as any, "getAuthHeaders").mockResolvedValue({ Authorization: "Bearer test" });
  vi.stubGlobal("fetch", vi.fn(async (url: string, init?: RequestInit) => {
    calls.push({ url, body: init?.body ? JSON.parse(String(init.body)) : undefined });
    return new Response(JSON.stringify({ candidates: [], count: 0, drafted: true, sent: false, email: null }), {
      status: 200, headers: { "content-type": "application/json" },
    });
  }));
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("ranker API options", () => {
  it("walkthrough deck: its own surface, no prefetch, no dig", async () => {
    await apiService.getRankerCandidates({ limit: 20, surface: "web_walkthrough", allowPaidLookups: false });
    const q = new URL(calls[0].url, "http://x").searchParams;
    expect(q.get("surface")).toBe("web_walkthrough");
    expect(q.get("prefetch")).toBe("0");
    expect(q.get("dig")).toBe("0");
  });

  it("Find's deck call is unchanged", async () => {
    await apiService.getRankerCandidates({ limit: 12, company: "Evercore" });
    const q = new URL(calls[0].url, "http://x").searchParams;
    expect(q.get("surface")).toBe("web_find");
    expect(q.has("prefetch")).toBe(false);
    expect(q.has("dig")).toBe(false);
  });

  it("walkthrough outreach asks for no Gmail draft; the default request body is unchanged", async () => {
    await apiService.rankerOutreach("c1", "draft", "d1", { createGmailDraft: false, surface: "web_walkthrough" });
    await apiService.rankerOutreach("c1", "draft", "d1");
    expect(calls[0].body).toEqual({ action: "draft", deck_id: "d1", create_gmail_draft: false, surface: "web_walkthrough" });
    expect(calls[1].body).toEqual({ action: "draft", deck_id: "d1" });
  });

  it("reveal labels the surface, defaulting to Find", async () => {
    await apiService.revealRankerEmail("c1", "d1", "web_walkthrough");
    await apiService.revealRankerEmail("c1", "d1");
    expect(calls.map((c) => c.body.surface)).toEqual(["web_walkthrough", "web_find"]);
  });

  it("free first outreach is opt-in on the reveal; the default body is unchanged", async () => {
    await apiService.revealRankerEmail("c1", "d1", "web_walkthrough", { useFirstOutreach: true });
    await apiService.revealRankerEmail("c1", "d1", "web_walkthrough", { useFirstOutreach: false });
    expect(calls[0].body).toEqual({ deck_id: "d1", surface: "web_walkthrough", use_first_outreach: true });
    expect(calls[1].body).toEqual({ deck_id: "d1", surface: "web_walkthrough" });
  });

  it("send-first-email: dry run, then send with the confirmed hash", async () => {
    await apiService.sendFirstEmail("ranker_c1", { dryRun: true });
    await apiService.sendFirstEmail("ranker_c1", { confirmedHash: "abc", surface: "web_walkthrough" });
    await apiService.sendFirstEmail("ranker_c1", { confirmedHash: "abc", surface: "web_walkthrough", confirmNotSent: true });
    expect(calls.map((c) => c.url.replace(/^.*\/api/, ""))).toEqual(Array(3).fill("/contacts/ranker_c1/send-first-email"));
    expect(calls[0].body).toEqual({ dry_run: true });
    expect(calls[1].body).toEqual({ confirmed_hash: "abc", surface: "web_walkthrough" });
    expect(calls[2].body).toEqual({ confirmed_hash: "abc", surface: "web_walkthrough", confirm_not_sent: true });
  });
});
