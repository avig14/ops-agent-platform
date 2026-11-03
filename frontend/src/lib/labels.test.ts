import { describe, expect, it } from "vitest";
import { GATE_EVENT, gateView } from "./labels";

describe("gateView", () => {
  it("marks a GATE_ code as a rejection and keeps the code", () => {
    const v = gateView("GATE_COMMS_NUMBER");
    expect(v.kind).toBe("blocked");
    expect(v.code).toBe("GATE_COMMS_NUMBER");
    expect(v.text).toBe(GATE_EVENT.GATE_COMMS_NUMBER);
  });
  it("marks an outage as a fallback, not a rejection", () => {
    expect(gateView("llm_unavailable").kind).toBe("fallback");
  });
  it("has plain words for every code the backend can emit", () => {
    for (const code of ["GATE_UNGROUNDED", "GATE_NUMERIC_MISMATCH", "GATE_UNKNOWN_ENTITY", "GATE_BAD_ACTION", "GATE_COMMS_NUMBER", "GATE_COMMS_LEAK", "llm_unavailable"]) {
      expect(GATE_EVENT[code], code).toBeTruthy();
    }
  });
  it("never shows a blank line for a code it does not know", () => {
    const v = gateView("GATE_SOMETHING_NEW");
    expect(v.kind).toBe("blocked");
    expect(v.text.length).toBeGreaterThan(10);
  });
});
