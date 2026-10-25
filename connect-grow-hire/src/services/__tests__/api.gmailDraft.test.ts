/**
 * pushContactGmailDraft maps the route's 200 / 409 / 400 to a result union.
 * fetch is faked; nothing leaves the process.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { apiService } from "@/services/api";

function reply(status: number, body: unknown) {
  vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(body), {
    status, headers: { "content-type": "application/json" },
  })));
}

beforeEach(() => {
  vi.spyOn(apiService as any, "getAuthHeaders").mockResolvedValue({ Authorization: "Bearer test" });
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("pushContactGmailDraft", () => {
  it("200 -> ok with the draft url and created flag", async () => {
    reply(200, { gmailDraftId: "d1", gmailDraftUrl: "https://mail.google.com/x", created: true });
    expect(await apiService.pushContactGmailDraft("c1")).toEqual({
      ok: true, gmailDraftUrl: "https://mail.google.com/x", created: true,
    });
    const [url, init] = (fetch as any).mock.calls[0];
    expect(url).toContain("/contacts/c1/gmail-draft");
    expect(init.method).toBe("POST");
  });

  it("409 -> the reason from the server", async () => {
    reply(409, { error: "gmail_not_connected" });
    expect(await apiService.pushContactGmailDraft("c1")).toEqual({ ok: false, reason: "gmail_not_connected" });
    reply(409, { error: "gmail_scope_missing" });
    expect(await apiService.pushContactGmailDraft("c1")).toEqual({ ok: false, reason: "gmail_scope_missing" });
  });

  it("400 -> no_saved_draft; anything else -> error", async () => {
    reply(400, { error: "no_saved_draft" });
    expect(await apiService.pushContactGmailDraft("c1")).toEqual({ ok: false, reason: "no_saved_draft" });
    reply(500, { error: "boom" });
    expect(await apiService.pushContactGmailDraft("c1")).toEqual({ ok: false, reason: "error" });
  });
});
