import { describe, it, expect } from "vitest";
import { openedSummary } from "./openedLabel";
import type { OutboxThread } from "@/services/api";

const now = Date.parse("2026-10-18T12:00:00Z");
const t = (o: Partial<OutboxThread>) => ({ id: "c" , ...o }) as OutboxThread;

describe("openedSummary", () => {
  it("single open", () => {
    expect(openedSummary(t({ emailOpenedAt: "2026-10-18T10:00:00Z", emailOpenCount: 1 }), now)).toBe("Opened 2h ago");
  });
  it("multiple opens use last open time", () => {
    expect(openedSummary(t({ emailOpenedAt: "2026-10-15T10:00:00Z", emailLastOpenedAt: "2026-10-18T10:00:00Z", emailOpenCount: 3 }), now)).toBe("Opened 3 times, last 2h ago");
  });
  it("no data", () => {
    expect(openedSummary(t({}), now)).toBe("");
  });
});
