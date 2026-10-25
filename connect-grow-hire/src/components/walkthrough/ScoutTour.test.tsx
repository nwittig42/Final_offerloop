import { afterEach, describe, expect, it, vi } from "vitest";
import { act, fireEvent, render, screen } from "@testing-library/react";

import { ScoutTour, type ScoutTourProps } from "./ScoutTour";

// A real element on the page for the spotlight, with a fixed layout box
// (jsdom has no layout, so getBoundingClientRect is mocked).
const addTarget = (onClick = vi.fn()) => {
  const el = document.createElement("button");
  el.setAttribute("data-tour", "tour-target");
  el.textContent = "Real card";
  el.addEventListener("click", onClick);
  el.getBoundingClientRect = () =>
    ({ top: 100, left: 80, width: 300, height: 200, right: 380, bottom: 300, x: 80, y: 100, toJSON: () => ({}) }) as DOMRect;
  document.body.appendChild(el);
  return { el, onClick };
};

const renderTour = (props: Partial<ScoutTourProps> = {}) => {
  const handlers = { onSkip: vi.fn(), onClose: vi.fn() };
  render(
    <ScoutTour
      target='[data-tour="tour-target"]'
      title="Pick someone"
      body="Here are a few people worth emailing first."
      {...handlers}
      {...props}
    />,
  );
  return handlers;
};

afterEach(() => {
  document.querySelectorAll('[data-tour="tour-target"]').forEach((el) => el.remove());
  vi.useRealTimers();
});

describe("ScoutTour", () => {
  it("renders the title, body and the Scout yeti beside a spotlight", () => {
    addTarget();
    renderTour();
    const dialog = screen.getByRole("dialog", { name: "Pick someone" });
    expect(dialog).toHaveTextContent("Here are a few people worth emailing first.");
    expect(screen.getByRole("img", { name: "Scout" })).toBeInTheDocument();
    const ring = screen.getByTestId("scout-tour-ring");
    expect(ring.style.top).toBe("92px");
    expect(ring.style.left).toBe("72px");
    expect(ring.style.width).toBe("316px");
    expect(ring.style.height).toBe("216px");
  });

  it("action, secondary, Skip tour and close call their handlers", () => {
    addTarget();
    const action = vi.fn();
    const secondary = vi.fn();
    const { onSkip, onClose } = renderTour({
      action: { label: "Looks good", onClick: action },
      secondary: { label: "Not now", onClick: secondary },
    });
    fireEvent.click(screen.getByRole("button", { name: "Looks good" }));
    fireEvent.click(screen.getByRole("button", { name: "Not now" }));
    fireEvent.click(screen.getByRole("button", { name: "Skip tour" }));
    fireEvent.click(screen.getByRole("button", { name: "Close tour" }));
    expect(action).toHaveBeenCalledTimes(1);
    expect(secondary).toHaveBeenCalledTimes(1);
    expect(onSkip).toHaveBeenCalledTimes(1);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("a disabled action can't be clicked", () => {
    addTarget();
    const action = vi.fn();
    renderTour({ action: { label: "Connect Gmail", onClick: action, disabled: true } });
    const btn = screen.getByRole("button", { name: "Connect Gmail" });
    expect(btn).toBeDisabled();
    fireEvent.click(btn);
    expect(action).not.toHaveBeenCalled();
  });

  it("Escape closes", () => {
    addTarget();
    const { onClose } = renderTour();
    fireEvent.keyDown(document, { key: "Escape" });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("clicks on the spotlighted element reach the page", () => {
    const { el, onClick } = addTarget();
    renderTour();
    fireEvent.click(el);
    expect(onClick).toHaveBeenCalledTimes(1);
  });

  it("clicks on the dim layer are swallowed", () => {
    addTarget();
    const underneath = vi.fn();
    const parent = vi.fn();
    const below = document.createElement("button");
    below.addEventListener("click", underneath);
    document.body.appendChild(below);
    const docListener = vi.fn();
    document.addEventListener("click", docListener);
    render(
      <div onClick={parent}>
        <ScoutTour target='[data-tour="tour-target"]' title="Pick someone" body="b" onSkip={vi.fn()} onClose={vi.fn()} />
      </div>,
    );
    const dims = screen.getAllByTestId("scout-tour-dim");
    expect(dims).toHaveLength(4);
    dims.forEach((d) => fireEvent.click(d));
    expect(underneath).not.toHaveBeenCalled();
    expect(parent).not.toHaveBeenCalled();
    expect(docListener).not.toHaveBeenCalled();
    document.removeEventListener("click", docListener);
    below.remove();
  });

  it("with no target, centers the bubble with no cutout", () => {
    renderTour({ target: null });
    const dialog = screen.getByRole("dialog", { name: "Pick someone" });
    expect(dialog).toHaveAttribute("data-placement", "center");
    expect(screen.queryByTestId("scout-tour-ring")).not.toBeInTheDocument();
    expect(screen.getAllByTestId("scout-tour-dim")).toHaveLength(1);
  });

  it("shows nothing until a missing target appears", async () => {
    renderTour();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    await act(async () => {
      addTarget();
      await new Promise((r) => setTimeout(r, 0));
    });
    expect(await screen.findByRole("dialog", { name: "Pick someone" })).toBeInTheDocument();
    expect(screen.getByTestId("scout-tour-ring")).toBeInTheDocument();
  });

  it("falls back to a centered bubble if the target never appears", () => {
    vi.useFakeTimers();
    renderTour();
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(5000);
    });
    expect(screen.getByRole("dialog", { name: "Pick someone" })).toHaveAttribute("data-placement", "center");
  });

  it("sits at the bottom of the screen on narrow viewports", () => {
    const prev = window.innerWidth;
    Object.defineProperty(window, "innerWidth", { configurable: true, value: 390 });
    try {
      addTarget();
      renderTour();
      expect(screen.getByRole("dialog", { name: "Pick someone" })).toHaveAttribute("data-placement", "bottom");
    } finally {
      Object.defineProperty(window, "innerWidth", { configurable: true, value: prev });
    }
  });
});
