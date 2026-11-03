import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { Confirm } from "./Confirm";

function setup(props: Partial<Parameters<typeof Confirm>[0]> = {}) {
  const onConfirm = vi.fn();
  render(<Confirm label="Approve" prompt="Approve this plan?" confirmLabel="Yes, approve" onConfirm={onConfirm} {...props} />);
  return { onConfirm, user: userEvent.setup() };
}

describe("Confirm", () => {
  it("starts as a single button and acts on nothing", () => {
    const { onConfirm } = setup();
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(screen.queryByText("Approve this plan?")).not.toBeInTheDocument();
    expect(onConfirm).not.toHaveBeenCalled();
  });

  it("the first press only asks; the second press acts once", async () => {
    const { onConfirm, user } = setup();
    await user.click(screen.getByRole("button", { name: "Approve" }));
    expect(onConfirm).not.toHaveBeenCalled();
    expect(screen.getByRole("group")).toHaveTextContent("Approve this plan?");
    await user.click(screen.getByRole("button", { name: "Yes, approve" }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
    expect(onConfirm).toHaveBeenCalledWith("");
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
  });

  it("Cancel closes without acting", async () => {
    const { onConfirm, user } = setup();
    await user.click(screen.getByRole("button", { name: "Approve" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(onConfirm).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
  });

  it("Escape closes without acting", async () => {
    const { onConfirm, user } = setup();
    await user.click(screen.getByRole("button", { name: "Approve" }));
    await user.keyboard("{Escape}");
    expect(onConfirm).not.toHaveBeenCalled();
    expect(screen.queryByRole("group")).not.toBeInTheDocument();
  });

  it("a required reason keeps the confirm button disabled until it is long enough", async () => {
    const { onConfirm, user } = setup({ reasonLabel: "Why reject?", minReason: 5, tone: "danger", confirmLabel: "Reject" });
    await user.click(screen.getByRole("button", { name: "Approve" }));
    const confirm = screen.getByRole("button", { name: "Reject" });
    expect(confirm).toBeDisabled();
    await user.type(screen.getByLabelText("Why reject?"), "no");
    expect(confirm).toBeDisabled();
    await user.type(screen.getByLabelText("Why reject?"), "pe, too costly");
    expect(confirm).toBeEnabled();
    await user.click(confirm);
    expect(onConfirm).toHaveBeenCalledWith("nope, too costly");
  });

  it("whitespace alone is not a reason, and the reason is trimmed", async () => {
    const { onConfirm, user } = setup({ reasonLabel: "Why?", minReason: 3, confirmLabel: "Go" });
    await user.click(screen.getByRole("button", { name: "Approve" }));
    await user.type(screen.getByLabelText("Why?"), "     ");
    expect(screen.getByRole("button", { name: "Go" })).toBeDisabled();
    await user.type(screen.getByLabelText("Why?"), "  ok then  ");
    await user.click(screen.getByRole("button", { name: "Go" }));
    expect(onConfirm).toHaveBeenCalledWith("ok then");
  });

  it("reopens with an empty reason after a cancel", async () => {
    const { user } = setup({ reasonLabel: "Why?" });
    await user.click(screen.getByRole("button", { name: "Approve" }));
    await user.type(screen.getByLabelText("Why?"), "typed");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await user.click(screen.getByRole("button", { name: "Approve" }));
    expect(screen.getByLabelText("Why?")).toHaveValue("");
  });

  it("disabled and pending both block opening", () => {
    setup({ disabled: true });
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
  });

  it("pending blocks opening too", () => {
    setup({ pending: true });
    expect(screen.getByRole("button", { name: "Approve" })).toBeDisabled();
  });

  it("danger tone styles the confirm button", async () => {
    const { user } = setup({ tone: "danger", confirmLabel: "Delete" });
    await user.click(screen.getByRole("button", { name: "Approve" }));
    expect(screen.getByRole("button", { name: "Delete" })).toHaveClass("btn--danger");
  });
});
