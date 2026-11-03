/**
 * What the UI shows or hints. The server decides for real (backend/app/policy.py, changes.py, closure.py);
 * these helpers only explain, so a refusal is never a surprise. Buttons are NOT hidden from people who may try:
 * the platform refuses, records the refusal, and the screen shows why.
 */
import type { Approval, CaseHead, Role, User } from "../api/types";
import { APPROVER_ROLES, roleLabel } from "./labels";

export function isApproverRole(role: Role): boolean {
  return APPROVER_ROLES.has(role);
}

/** Managers and regional heads only act inside their own region. Finance and admin are not region-bound. */
export function inRegion(user: User, region: string | null): boolean {
  if (user.role === "manager" || user.role === "regional_head") return !!user.region && user.region === region;
  return true;
}

/** Owner of the request, or an approver of that region. Mirrors changes.py. */
export function canChangeCase(user: User, c: CaseHead): boolean {
  if (c.requester_id === user.id) return true;
  return isApproverRole(user.role) && inRegion(user, c.region);
}

/** Approvers of the case's region run closure steps. Mirrors closure.py. */
export function canRunClosure(user: User, c: CaseHead): boolean {
  return isApproverRole(user.role) && inRegion(user, c.region);
}

/** Why this person's approve/reject click would be refused, or null if it should work. */
export function approvalHint(user: User, a: Approval, c: CaseHead, approvedBy: readonly string[]): string | null {
  if (user.role === "service") return "The AI service identity can never approve.";
  if (c.requester_id === user.id) return "You submitted this request, so you cannot approve it (separation of duties).";
  if (approvedBy.includes(user.id)) return "You already approved another tier. A different person must approve this one.";
  if (user.role === "admin") return "Admin runs the platform and holds no approval authority.";
  const ok = a.required_role === "manager" ? isApproverRole(user.role) : user.role === a.required_role;
  if (!ok) return `This tier needs a ${roleLabel(a.required_role).toLowerCase()}. You are a ${roleLabel(user.role).toLowerCase()}.`;
  if (!inRegion(user, c.region)) return `This case is in ${c.region ?? "another region"}, outside your region.`;
  return null;
}
