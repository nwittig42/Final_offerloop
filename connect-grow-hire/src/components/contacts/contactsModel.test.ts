import { describe, it, expect } from "vitest";
import { toRow, threadStage, stageToBackend, stageStyle, STAGES, STAGE_LABELS, STAGE_STYLES } from "./contactsModel";
import type { OutboxThread } from "@/services/api";

const thread = (over: Partial<OutboxThread>): OutboxThread => ({ id: "c1", ...over } as OutboxThread);

describe("contactsModel Opened stage", () => {
  it("lists opened between contacted and connected", () => {
    expect([...STAGES]).toEqual(["saved", "drafted", "contacted", "opened", "connected", "interviewing", "offer"]);
    expect(STAGE_LABELS.opened).toBe("Opened");
  });
  it("email_opened -> opened", () => {
    expect(threadStage(thread({ pipelineStage: "email_opened" }))).toBe("opened");
    expect(toRow({ id: "c1" }, thread({ pipelineStage: "email_opened" }), undefined).stageLabel).toBe("Opened");
  });
  it("waiting_on_reply with emailOpenedAt set stays contacted", () => {
    expect(threadStage(thread({ pipelineStage: "waiting_on_reply", emailOpenedAt: "2026-10-01T00:00:00Z" }))).toBe("contacted");
  });
  it("replied -> connected", () => {
    expect(threadStage(thread({ pipelineStage: "replied" }))).toBe("connected");
  });
  it("email_opened + hasUnreadReply keeps the unread flag on the row", () => {
    const t = thread({ pipelineStage: "email_opened", hasUnreadReply: true });
    expect(threadStage(t)).toBe("opened");
    const row = toRow({ id: "c1" }, t, undefined);
    expect(row.stage).toBe("opened");
    expect(row.stageLabel).toBe("Opened");
    expect(row.hasUnreadReply).toBe(true);
  });
  it("stageToBackend(opened) === email_opened", () => {
    expect(stageToBackend("opened")).toBe("email_opened");
  });
});

describe("contactsModel stage colors", () => {
  it("gives all seven stages distinct dot and pill colors", () => {
    expect(STAGES).toHaveLength(7);
    const dots = STAGES.map((s) => STAGE_STYLES[s].dot.toUpperCase());
    const pills = STAGES.map((s) => STAGE_STYLES[s].pill);
    expect(new Set(dots).size).toBe(7);
    expect(new Set(pills).size).toBe(7);
  });
  it("Opened is teal, matching the old tracker", () => {
    expect(stageStyle("opened")).toEqual({ dot: "#14B8A6", pill: "bg-teal-50 text-teal-700" });
  });
});
