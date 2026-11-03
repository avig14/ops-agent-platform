import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { ErrorBoundary } from "./ErrorBoundary";

afterEach(() => vi.restoreAllMocks());

let broken = true;
function Bomb() {
  if (broken) throw new Error("kaboom");
  return <p>all fine</p>;
}

describe("ErrorBoundary", () => {
  it("passes children through when nothing throws", () => {
    broken = false;
    render(
      <ErrorBoundary>
        <Bomb />
      </ErrorBoundary>,
    );
    expect(screen.getByText("all fine")).toBeInTheDocument();
  });

  it("catches a render crash, shows the message and logs it", () => {
    const log = vi.spyOn(console, "error").mockImplementation(() => {});
    broken = true;
    render(
      <ErrorBoundary>
        <Bomb />
      </ErrorBoundary>,
    );
    const alert = screen.getByRole("alert");
    expect(alert).toHaveTextContent("This screen hit a problem");
    expect(alert).toHaveTextContent("kaboom");
    expect(log).toHaveBeenCalled();
  });

  it("Reload this screen recovers once the cause is gone", async () => {
    vi.spyOn(console, "error").mockImplementation(() => {});
    broken = true;
    render(
      <ErrorBoundary>
        <Bomb />
      </ErrorBoundary>,
    );
    broken = false;
    await userEvent.click(screen.getByRole("button", { name: "Reload this screen" }));
    expect(screen.getByText("all fine")).toBeInTheDocument();
  });
});
