import { describe, expect, it } from "vitest";
import { approval, caseHead, user } from "../test/factories";
import { approvalHint, canChangeCase, canRunClosure, inRegion, isApproverRole } from "./permissions";

const westCase = caseHead({ region: "West", requester_id: "u-req" });
const manager = user({ id: "u-m", role: "manager", region: "West" });
const head = user({ id: "u-h", role: "regional_head", region: "West" });
const finance = user({ id: "u-f", role: "finance_director", region: null });
const admin = user({ id: "u-a", role: "admin", region: null });

describe("isApproverRole", () => {
  it("is true for the three approver roles only", () => {
    expect(isApproverRole("manager")).toBe(true);
    expect(isApproverRole("regional_head")).toBe(true);
    expect(isApproverRole("finance_director")).toBe(true);
    expect(isApproverRole("requester")).toBe(false);
    expect(isApproverRole("admin")).toBe(false);
    expect(isApproverRole("service")).toBe(false);
  });
});

describe("inRegion", () => {
  it("binds managers and regional heads to their own region", () => {
    expect(inRegion(manager, "West")).toBe(true);
    expect(inRegion(manager, "South")).toBe(false);
    expect(inRegion(head, "South")).toBe(false);
  });
  it("a regional role with no region has no reach", () => {
    expect(inRegion(user({ role: "manager", region: null }), "West")).toBe(false);
  });
  it("does not bind finance or admin", () => {
    expect(inRegion(finance, "South")).toBe(true);
    expect(inRegion(admin, "West")).toBe(true);
  });
});

describe("canChangeCase and canRunClosure", () => {
  it("the requester may change their own case but not run closure", () => {
    const me = user({ id: "u-req", role: "requester", region: "West" });
    expect(canChangeCase(me, westCase)).toBe(true);
    expect(canRunClosure(me, westCase)).toBe(false);
  });
  it("another requester may do neither", () => {
    const other = user({ id: "u-other", role: "requester" });
    expect(canChangeCase(other, westCase)).toBe(false);
    expect(canRunClosure(other, westCase)).toBe(false);
  });
  it("an approver of the region may do both", () => {
    expect(canChangeCase(manager, westCase)).toBe(true);
    expect(canRunClosure(manager, westCase)).toBe(true);
  });
  it("an approver of another region may do neither", () => {
    const south = user({ id: "u-s", role: "manager", region: "South" });
    expect(canChangeCase(south, westCase)).toBe(false);
    expect(canRunClosure(south, westCase)).toBe(false);
  });
  it("admin holds no case authority", () => {
    expect(canChangeCase(admin, westCase)).toBe(false);
    expect(canRunClosure(admin, westCase)).toBe(false);
  });
});

describe("approvalHint", () => {
  const tier = approval({ required_role: "regional_head" });

  it("is null when the click should work", () => {
    expect(approvalHint(head, tier, westCase, [])).toBeNull();
  });
  it("blocks the AI service identity first", () => {
    expect(approvalHint(user({ role: "service" }), tier, westCase, [])).toMatch(/never approve/i);
  });
  it("blocks approving your own request", () => {
    const self = user({ id: "u-req", role: "regional_head", region: "West" });
    expect(approvalHint(self, tier, westCase, [])).toMatch(/separation of duties/i);
  });
  it("blocks a second tier by the same person", () => {
    expect(approvalHint(finance, approval({ required_role: "finance_director" }), westCase, ["u-f"])).toMatch(/different person/i);
  });
  it("blocks admin", () => {
    expect(approvalHint(admin, tier, westCase, [])).toMatch(/admin/i);
  });
  it("blocks the wrong role and names both roles", () => {
    const msg = approvalHint(manager, tier, westCase, []);
    expect(msg).toMatch(/regional head/i);
    expect(msg).toMatch(/manager/i);
  });
  it("a manager tier accepts any approver role in region", () => {
    expect(approvalHint(head, approval({ required_role: "manager" }), westCase, [])).toBeNull();
    expect(approvalHint(manager, approval({ required_role: "manager" }), westCase, [])).toBeNull();
  });
  it("blocks the right role in the wrong region", () => {
    const southHead = user({ id: "u-sh", role: "regional_head", region: "South" });
    expect(approvalHint(southHead, tier, westCase, [])).toMatch(/outside your region/i);
  });
  it("finance director is not region-bound", () => {
    const southCase = caseHead({ region: "South", requester_id: "u-req" });
    expect(approvalHint(finance, approval({ required_role: "finance_director" }), southCase, [])).toBeNull();
  });
  it("says another region when the case has none", () => {
    const noRegion = caseHead({ region: null, requester_id: "u-req" });
    expect(approvalHint(head, tier, noRegion, [])).toMatch(/another region/i);
  });
});
