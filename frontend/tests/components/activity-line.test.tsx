import { act, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { ActivityLine } from "@/components/thread/ActivityLine";
import type { ActivityView } from "@/lib/run-state";

const T0 = Date.parse("2026-10-28T09:00:00.000Z");

const view = (over: Partial<ActivityView> = {}): ActivityView => ({
  seq: 7,
  at: new Date(T0).toISOString(),
  phase: "verifying",
  message: "The verifier is checking the result",
  tier: "strong",
  attempt: 1,
  retryInS: null,
  ...over,
});

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(T0);
});
afterEach(() => {
  vi.useRealTimers();
});

const tick = (ms: number) =>
  act(() => {
    vi.advanceTimersByTime(ms);
  });

describe("ActivityLine", () => {
  test("shows the message with elapsed seconds that tick every second", () => {
    render(<ActivityLine activity={view()} />);
    const line = screen.getByTestId("activity-line");
    expect(line).toHaveTextContent("The verifier is checking the result");
    expect(line).toHaveTextContent("0 s");
    tick(1_000);
    expect(line).toHaveTextContent("1 s");
    tick(60_000);
    expect(line).toHaveTextContent("1 m 01 s");
  });

  test("counts a rate-limit back-off down to 'retrying now'", () => {
    render(
      <ActivityLine
        activity={view({
          phase: "waiting",
          message: "Rate limit reached on the small model — retrying in 3 s",
          retryInS: 3,
        })}
      />,
    );
    const line = screen.getByTestId("activity-line");
    expect(line).toHaveTextContent(
      "Rate limit reached on the small model — retrying in 3 s",
    );
    tick(1_000);
    expect(line).toHaveTextContent("retrying in 2 s");
    tick(5_000);
    expect(line).toHaveTextContent("retrying now");
    expect(line).not.toHaveTextContent(/retrying in \d/);
  });

  test("is not a live region: ticks are never announced", () => {
    render(<ActivityLine activity={view()} />);
    const line = screen.getByTestId("activity-line");
    expect(line).not.toHaveAttribute("aria-live");
    expect(line.closest("[aria-live]")).toBeNull();
  });

  test("the spinner is decorative and stops under reduced motion", () => {
    render(<ActivityLine activity={view()} />);
    const spinner = screen.getByTestId("activity-spinner");
    expect(spinner).toHaveAttribute("aria-hidden", "true");
    expect(spinner).toHaveClass("animate-spin", "motion-reduce:animate-none");
  });

  test("a new phase restarts the clock from its own start time", () => {
    const { rerender } = render(<ActivityLine activity={view()} />);
    tick(5_000);
    rerender(
      <ActivityLine
        activity={view({
          seq: 8,
          at: new Date(T0 + 5_000).toISOString(),
          phase: "loading",
          message: "Loading 15,000 rows into the warehouse",
        })}
      />,
    );
    const line = screen.getByTestId("activity-line");
    expect(line).toHaveTextContent("Loading 15,000 rows into the warehouse");
    expect(line).toHaveTextContent("0 s");
  });

  test("stops its timer on unmount", () => {
    const { unmount } = render(<ActivityLine activity={view()} />);
    unmount();
    expect(vi.getTimerCount()).toBe(0);
  });
});
