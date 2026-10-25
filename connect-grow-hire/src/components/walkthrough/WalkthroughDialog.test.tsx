import { describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";

// The flow itself is covered by WalkthroughFlow.test.tsx.
const flowProps = vi.hoisted(() => ({ last: null as Record<string, unknown> | null }));
vi.mock("./WalkthroughFlow", () => ({
  WalkthroughFlow: (props: Record<string, unknown>) => {
    flowProps.last = props;
    return <div data-testid="flow" />;
  },
}));

import { WalkthroughDialog } from "./WalkthroughDialog";

describe("WalkthroughDialog", () => {
  it("renders the walkthrough in modal mode, returning to /find?walkthrough=1 from Gmail", () => {
    const onExit = vi.fn();
    render(<WalkthroughDialog open onExit={onExit} />);
    expect(screen.getByTestId("flow")).toBeInTheDocument();
    expect(flowProps.last).toMatchObject({ mode: "modal", gmailReturnPath: "/find?walkthrough=1", onExit });
  });

  it('closing with the X calls onExit("later")', () => {
    const onExit = vi.fn();
    render(<WalkthroughDialog open onExit={onExit} />);
    fireEvent.click(screen.getByRole("button", { name: "Close" }));
    expect(onExit).toHaveBeenCalledWith("later");
  });

  it("renders nothing while closed", () => {
    render(<WalkthroughDialog open={false} onExit={vi.fn()} />);
    expect(screen.queryByTestId("flow")).not.toBeInTheDocument();
  });
});
