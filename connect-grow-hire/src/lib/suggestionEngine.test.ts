import { describe, it, expect } from "vitest";
import type { OutboxThread } from "@/services/api";
import { generateSuggestions } from "./suggestionEngine";

describe("suggestionEngine follow-ups", () => {
  it("email_opened thread 8 days after send with no follow-ups yields a follow_up card", () => {
    const sent = new Date(Date.now() - 8 * 24 * 60 * 60 * 1000).toISOString();
    const t = { id: "c1", company: "Acme", pipelineStage: "email_opened", followUpCount: 0, emailSentAt: sent } as unknown as OutboxThread;
    const out = generateSuggestions("email", { dreamCompanies: [] }, [t], new Set());
    expect(out.some((s) => s.type === "follow_up")).toBe(true);
  });
});
