import { describe, it, expect } from "vitest";
import type { OutboxThread } from "@/services/api";
import { outboxThreadToProto, protoStageToBackend, PROTO_STAGES, PROTO_STAGE_LABELS } from "./trackerAdapter";

function thread(over: Partial<OutboxThread>): OutboxThread {
  return { id: "t1", email: "a@b.com", name: "A B", ...over } as OutboxThread;
}

describe("trackerAdapter opened step", () => {
  it("orders opened between contacted and connected", () => {
    expect(PROTO_STAGES).toEqual(["saved", "drafted", "contacted", "opened", "connected", "interviewing", "offer"]);
    expect(PROTO_STAGE_LABELS.opened).toBe("Opened");
  });
  it("email_opened -> stage opened, status Opened", () => {
    const p = outboxThreadToProto(thread({ pipelineStage: "email_opened", emailOpenedAt: "2026-10-01T00:00:00Z" }));
    expect(p.stage).toBe("opened");
    expect(p.status).toBe("Opened");
  });
  it("waiting_on_reply with emailOpenedAt stays contacted (stage is source of truth)", () => {
    const p = outboxThreadToProto(thread({ pipelineStage: "waiting_on_reply", emailOpenedAt: "2026-10-01T00:00:00Z" }));
    expect(p.stage).toBe("contacted");
    expect(p.status).toBe("Sent");
  });
  it("replied -> connected", () => {
    expect(outboxThreadToProto(thread({ pipelineStage: "replied" })).stage).toBe("connected");
  });
  it("email_opened + unread reply -> status Reply", () => {
    const p = outboxThreadToProto(thread({ pipelineStage: "email_opened", hasUnreadReply: true }));
    expect(p.status).toBe("Reply");
  });
  it("protoStageToBackend(opened) is email_opened", () => {
    expect(protoStageToBackend("opened")).toBe("email_opened");
  });
});
