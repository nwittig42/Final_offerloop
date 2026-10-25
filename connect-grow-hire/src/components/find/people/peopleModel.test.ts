import { describe, expect, it } from "vitest";
import { personFromRanker } from "./peopleModel";

const base = { id: "a", firstName: "M", lastName: "O" };

describe("personFromRanker schools", () => {
  it("carries every school and the joined year", () => {
    const p = personFromRanker(
      { person: { ...base, college: "Michigan", schools: ["Michigan", "HBS"], joinedYear: 2021 }, reasons: [], briefing: "" },
      0,
      "d",
    );
    expect(p?.schools).toEqual(["Michigan", "HBS"]);
    expect(p?.joinedYear).toBe(2021);
  });

  it("falls back to the single college for legacy payloads", () => {
    const p = personFromRanker({ person: { ...base, college: "Michigan" }, reasons: [], briefing: "" }, 0, "d");
    expect(p?.schools).toEqual(["Michigan"]);
  });

  it("is empty with no school data", () => {
    const p = personFromRanker({ person: base, reasons: [], briefing: "" }, 0, "d");
    expect(p?.schools).toEqual([]);
  });
});
