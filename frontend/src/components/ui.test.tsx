import type { UseQueryResult } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import { ActionError, ActorBadge, Boundary, ErrorBox, errorParts, StatusChip } from "./ui";

/** Just the fields Boundary reads. */
function q<T>(over: Partial<UseQueryResult<T>>): UseQueryResult<T> {
  return { isPending: false, isError: false, data: undefined, error: null, refetch: vi.fn(), ...over } as unknown as UseQueryResult<T>;
}

describe("Boundary", () => {
  it("shows a skeleton while loading", () => {
    render(<Boundary query={q<number[]>({ isPending: true })} what="cases">{() => <p>data</p>}</Boundary>);
    expect(screen.getByRole("status")).toHaveTextContent("Loading cases");
  });

  it("shows a custom skeleton when given one", () => {
    render(
      <Boundary query={q<number[]>({ isPending: true })} what="cases" skeleton={<p>custom wait</p>}>
        {() => <p>data</p>}
      </Boundary>,
    );
    expect(screen.getByText("custom wait")).toBeInTheDocument();
  });

  it("shows the error, request id and a working retry", async () => {
    const refetch = vi.fn();
    render(
      <Boundary query={q<number[]>({ isError: true, error: new ApiError(500, "Boom", "req-3"), refetch })} what="cases">
        {() => <p>data</p>}
      </Boundary>,
    );
    expect(screen.getByRole("alert")).toHaveTextContent("Could not load cases");
    expect(screen.getByText("req-3")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /try again/i }));
    expect(refetch).toHaveBeenCalledTimes(1);
  });

  it("keeps showing old data when a background refresh fails", () => {
    render(
      <Boundary query={q<string>({ isError: true, error: new Error("blip"), data: "still here" })} what="things">
        {(d) => <p>{d}</p>}
      </Boundary>,
    );
    expect(screen.getByText("still here")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows the empty state when the data says it is empty", () => {
    render(
      <Boundary query={q<number[]>({ data: [] })} what="cases" isEmpty={(d) => d.length === 0} empty={<p>nothing here</p>}>
        {() => <p>data</p>}
      </Boundary>,
    );
    expect(screen.getByText("nothing here")).toBeInTheDocument();
    expect(screen.queryByText("data")).not.toBeInTheDocument();
  });

  it("renders children with the data when there is some", () => {
    render(
      <Boundary query={q<number[]>({ data: [1, 2, 3] })} what="cases" isEmpty={(d) => d.length === 0} empty={<p>nothing here</p>}>
        {(d) => <p>count {d.length}</p>}
      </Boundary>,
    );
    expect(screen.getByText("count 3")).toBeInTheDocument();
  });

  it("treats falsy data (0) as data, not as loading", () => {
    render(<Boundary query={q<number>({ data: 0 })} what="n">{(d) => <p>value {d}</p>}</Boundary>);
    expect(screen.getByText("value 0")).toBeInTheDocument();
  });
});

describe("errors", () => {
  it("errorParts handles ApiError, Error and junk", () => {
    expect(errorParts(new ApiError(403, "Forbidden", "r1"))).toMatchObject({ message: "Forbidden", requestId: "r1" });
    expect(errorParts(new Error("plain"))).toEqual({ message: "plain", hint: "Try again.", requestId: null });
    expect(errorParts("weird").message).toBe("Something went wrong");
  });

  it("ActionError is silent without an error", () => {
    const { container } = render(<ActionError error={null} what="approve" />);
    expect(container).toBeEmptyDOMElement();
  });

  it("ActionError says what failed, why and what to do", () => {
    render(<ActionError error={new ApiError(403, "Only a regional head may approve", "req-5")} what="approve" />);
    const el = screen.getByRole("alert");
    expect(el).toHaveTextContent("Could not approve: Only a regional head may approve.");
    expect(el).toHaveTextContent(/your role cannot do this/i);
    expect(el).toHaveTextContent("req-5");
  });

  it("ErrorBox has no retry button unless asked", () => {
    render(<ErrorBox error={new Error("x")} what="load" />);
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
  });
});

describe("badges", () => {
  it("StatusChip uses the plain-language label, not the enum", () => {
    render(<StatusChip status="AWAITING_APPROVAL" />);
    expect(screen.getByText("Awaiting approval")).toHaveClass("chip--person");
  });

  it("StatusChip shows an unknown status as is", () => {
    render(<StatusChip status="WEIRD" />);
    expect(screen.getByText("WEIRD")).toBeInTheDocument();
  });

  it("ActorBadge tells person, AI and system apart in words, not colour alone", () => {
    render(
      <>
        <ActorBadge type="human" />
        <ActorBadge type="agent" />
        <ActorBadge type="system" />
      </>,
    );
    expect(screen.getByText("Person")).toBeInTheDocument();
    expect(screen.getByText("AI")).toBeInTheDocument();
    expect(screen.getByText("System")).toBeInTheDocument();
  });
});
