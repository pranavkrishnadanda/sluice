import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, test, vi } from "vitest";
import { ApiProvider } from "@/components/api/ApiProvider";
import { Composer, type ComposerProps } from "@/components/thread/Composer";
import { FileChip } from "@/components/thread/FileChip";
import { NewRunComposer } from "@/components/thread/NewRunComposer";
import { RunThread } from "@/components/thread/RunThread";
import { RUN_REPAIRED_EVENTS } from "@/lib/api/fixtures/run-repaired";
import { MockApiClient } from "@/lib/api/mock-client";
import type { ApiClient, RunEvent } from "@/lib/api/types";

const nav = vi.hoisted(() => ({ push: vi.fn<(href: string) => void>() }));
vi.mock("next/navigation", () => ({
  usePathname: () => "/runs",
  useRouter: () => ({ push: nav.push }),
}));

beforeEach(() => {
  nav.push.mockReset();
});

interface StreamCall {
  runId: string;
  onEvent: (e: RunEvent) => void;
  resolve: () => void;
  reject: (err: Error) => void;
  signal: AbortSignal;
  /** Whether every earlier subscription was already aborted when this began. */
  previousAborted: boolean;
}

/**
 * A client whose stream is driven by the test. Each `streamEvents` call is
 * recorded; `emitThrough(seq)` delivers run-repaired's events up to `seq` on
 * the latest call (or on `call`), `end()` resolves it. Every call replays
 * from the first event (a reconnect without Last-Event-ID), so the reducer's
 * seq de-dup is exercised. Like a misbehaving backend, it ignores `signal`:
 * it keeps emitting after abort unless the component guards against it.
 */
function controlledClient(events: readonly RunEvent[] = RUN_REPAIRED_EVENTS) {
  const calls: StreamCall[] = [];
  const base = new MockApiClient({ delayMs: 0 });
  const streamEvents = vi.fn<ApiClient["streamEvents"]>(
    (runId, onEvent, signal) =>
      new Promise<void>((resolve, reject) => {
        calls.push({
          runId,
          onEvent,
          resolve,
          reject,
          signal,
          previousAborted: calls.every((c) => c.signal.aborted),
        });
      }),
  );
  const client: ApiClient = {
    isDemo: true,
    listRuns: () => base.listRuns(),
    createRun: (f, t) => base.createRun(f, t),
    streamEvents,
    getTransforms: (id) => base.getTransforms(id),
    getPreview: (id, n) => base.getPreview(id, n),
    getEvalSummary: () => base.getEvalSummary(),
  };
  const pick = (call?: number) =>
    call === undefined ? calls.at(-1) : calls[call];
  return {
    client,
    streamEvents,
    calls,
    signal: () => calls.at(-1)?.signal ?? null,
    emitThrough(seq: number, call?: number) {
      const target = pick(call);
      act(() => {
        for (const e of events) {
          if (e.seq <= seq) target?.onEvent(structuredClone(e));
        }
      });
    },
    async end(call?: number) {
      await act(async () => {
        pick(call)?.resolve();
      });
    },
    async fail(call?: number) {
      await act(async () => {
        pick(call)?.reject(new Error("stream dropped"));
      });
    },
  };
}

function renderThread(client: ApiClient, runId = "run-repaired") {
  return render(
    <ApiProvider client={client}>
      <RunThread runId={runId} />
    </ApiProvider>,
  );
}

function steps() {
  return within(screen.getByRole("list", { name: "Pipeline steps" }));
}

describe("RunThread", () => {
  test("run-repaired shows retry then success", async () => {
    const stream = controlledClient();
    renderThread(stream.client);

    // seq 7 = node_started n1 attempt 2, after attempt 1 failed.
    stream.emitThrough(7);
    const node = steps()
      .getByText("Map amt to o_totalprice and normalise amounts")
      .closest("li");
    expect(node).not.toBeNull();
    const row = within(node as HTMLElement);
    expect(row.getByText("attempt 2/3")).toBeInTheDocument();
    expect(row.getByText(/Retrying/)).toBeInTheDocument();
    expect(
      row.getByText("contract check failed: 14 rows in paise"),
    ).toBeInTheDocument();

    stream.emitThrough(13);
    await stream.end();
    expect(steps().getByText("Loaded 48,210 rows")).toBeInTheDocument();
    expect(row.getByText("$0.02 · 2.1 s")).toBeInTheDocument();
    expect(row.getByText("strong")).toBeInTheDocument();
    expect(steps().getByText(/4 issues found/)).toBeInTheDocument();
    expect(steps().getByText(/3 fixes/)).toBeInTheDocument();
    expect(steps().getByText(/12\/12 contract checks/)).toBeInTheDocument();
    expect(screen.getByText(/Renamed amt to o_totalprice/)).toBeInTheDocument();
    expect(screen.queryByText("Connection lost")).not.toBeInTheDocument();
  });

  test("user bubble shows the file chip and the request", async () => {
    renderThread(new MockApiClient({ delayMs: 0 }));
    expect(
      await screen.findByText("orders_2026-10.csv · 48,210 rows"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Load this into the orders table."),
    ).toBeInTheDocument();
  });

  test("run-clean shows no LLM work", async () => {
    renderThread(new MockApiClient({ delayMs: 0 }), "run-clean");
    expect(await steps().findByText("No LLM work needed")).toBeInTheDocument();
    expect(steps().getAllByText(/\$0\b/).length).toBeGreaterThan(0);
    await screen.findByText(/no model calls were needed/);
    expect(screen.getByText(/Total cost \$0\b/)).toBeInTheDocument();
  });

  test("run-quarantined shows quarantined count", async () => {
    renderThread(new MockApiClient({ delayMs: 0 }), "run-quarantined");
    expect(await steps().findByText(/212 quarantined/)).toBeInTheDocument();
    expect(steps().getByText("Loaded 59,963 rows")).toBeInTheDocument();
  });

  test("stream drop shows Connection lost and Retry resumes", async () => {
    const stream = controlledClient();
    renderThread(stream.client);

    stream.emitThrough(3);
    await stream.end();
    expect(await screen.findByText("Connection lost")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(stream.streamEvents).toHaveBeenCalledTimes(2);
    expect(screen.queryByText("Connection lost")).not.toBeInTheDocument();

    stream.emitThrough(13);
    await stream.end();
    expect(steps().getByText("Loaded 48,210 rows")).toBeInTheDocument();
    // Replayed events were de-duplicated: one row per milestone.
    expect(steps().getAllByText(/issues found/)).toHaveLength(1);
    expect(screen.queryByText("Connection lost")).not.toBeInTheDocument();
  });

  test("steps are an ordered list whose status is conveyed in text", () => {
    const stream = controlledClient();
    renderThread(stream.client);
    stream.emitThrough(12); // loaded seen, run_finished not yet
    expect(screen.getByRole("list", { name: "Pipeline steps" }).tagName).toBe(
      "OL",
    );
    const items = steps().getAllByRole("listitem");
    // Profiled, Rules, n1, n2, Verified, Loaded.
    expect(items).toHaveLength(6);
    for (const item of items) expect(item).toHaveTextContent(/^Done/);
  });

  test("shows an in-progress step while waiting for the next event", () => {
    const stream = controlledClient();
    renderThread(stream.client);
    stream.emitThrough(10); // n2 finished; verification pending
    const last = steps().getAllByRole("listitem").at(-1);
    expect(last).toHaveTextContent(/In progress/);
    expect(last).toHaveTextContent(/Verifying/);
  });

  test("announces the latest step politely without making the list live", () => {
    const stream = controlledClient();
    renderThread(stream.client);
    stream.emitThrough(2);
    const live = screen.getByRole("status");
    expect(live).toHaveAttribute("aria-live", "polite");
    expect(live).toHaveTextContent(/Profiled/);
    expect(
      screen.getByRole("list", { name: "Pipeline steps" }),
    ).not.toHaveAttribute("aria-live");
  });

  test("aborts the stream on unmount", () => {
    const stream = controlledClient();
    const view = renderThread(stream.client);
    view.unmount();
    expect(stream.signal()?.aborted).toBe(true);
  });

  test("events and rejections after unmount are ignored without warnings", async () => {
    const errors = vi.spyOn(console, "error");
    const stream = controlledClient();
    const view = renderThread(stream.client);
    stream.emitThrough(2);
    view.unmount();

    stream.emitThrough(13, 0);
    await stream.fail(0);
    expect(errors).not.toHaveBeenCalled();

    // A fresh mount starts from an empty state, not the old run's.
    renderThread(stream.client);
    expect(screen.queryByText(/issues found/)).not.toBeInTheDocument();
    expect(stream.calls).toHaveLength(2);
  });

  test("an aborted subscription cannot update the live thread", async () => {
    const stream = controlledClient();
    renderThread(stream.client);
    stream.emitThrough(3);
    await stream.end();
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(stream.calls[0]?.signal.aborted).toBe(true);

    // The dropped stream wakes up late: none of it may reach the reducer.
    stream.emitThrough(13, 0);
    expect(screen.queryByText("Loaded 48,210 rows")).not.toBeInTheDocument();
    expect(screen.queryByText(/Renamed amt/)).not.toBeInTheDocument();

    // ...nor may its rejection flip the new subscription to an error.
    await stream.fail(0);
    expect(
      screen.queryByText("Couldn't load this run"),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("Connection lost")).not.toBeInTheDocument();

    stream.emitThrough(13, 1);
    await stream.end(1);
    expect(steps().getByText("Loaded 48,210 rows")).toBeInTheDocument();
  });

  test("switching runId aborts the previous subscription before the next", () => {
    const stream = controlledClient();
    const view = renderThread(stream.client, "run-repaired");
    stream.emitThrough(5);
    view.rerender(
      <ApiProvider client={stream.client}>
        <RunThread runId="run-clean" />
      </ApiProvider>,
    );
    expect(stream.calls.map((c) => c.runId)).toEqual([
      "run-repaired",
      "run-clean",
    ]);
    expect(stream.calls[0]?.signal.aborted).toBe(true);
    expect(stream.calls[1]?.previousAborted).toBe(true);
    expect(stream.calls[1]?.signal.aborted).toBe(false);
    // The new run starts fresh: run-repaired's rows are gone.
    expect(screen.queryByText(/issues found/)).not.toBeInTheDocument();
    stream.emitThrough(13, 0);
    expect(screen.queryByText(/issues found/)).not.toBeInTheDocument();
  });

  test("a stream that rejects offers Retry", async () => {
    const client = new MockApiClient({ delayMs: 0 });
    renderThread(client, "run-missing");
    expect(
      await screen.findByText("Couldn't load this run"),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });
});

describe("FileChip", () => {
  test("shows name and Indian-grouped row count", () => {
    render(<FileChip name="big.csv" rows={124_500} />);
    expect(screen.getByText("big.csv · 1,24,500 rows")).toBeInTheDocument();
  });
});

describe("Composer", () => {
  test("composer rejects .xlsx", async () => {
    const client = new MockApiClient({ delayMs: 0 });
    const createRun = vi.spyOn(client, "createRun");
    render(
      <ApiProvider client={client}>
        <NewRunComposer />
      </ApiProvider>,
    );
    const user = userEvent.setup({ applyAccept: false });
    await user.upload(
      screen.getByLabelText("Attach a file"),
      new File(["a,b"], "orders.xlsx"),
    );
    expect(screen.getByRole("alert")).toHaveTextContent(
      /\.csv, \.json or \.parquet/,
    );
    await user.click(screen.getByRole("button", { name: "Start run" }));
    expect(createRun).not.toHaveBeenCalled();
    expect(nav.push).not.toHaveBeenCalled();
  });

  test("composer submits and navigates", async () => {
    const client = new MockApiClient({ delayMs: 0 });
    const createRun = vi.spyOn(client, "createRun");
    render(
      <ApiProvider client={client}>
        <NewRunComposer />
      </ApiProvider>,
    );
    const user = userEvent.setup();
    const file = new File(["a,b"], "orders_2026-11.csv", { type: "text/csv" });
    await user.upload(screen.getByLabelText("Attach a file"), file);
    expect(screen.getByText("orders_2026-11.csv")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Start run" }));
    expect(createRun).toHaveBeenCalledWith(file, "orders");
    await waitFor(() => expect(nav.push).toHaveBeenCalledWith("/runs/run-1"));
  });

  test("a rejected pick keeps the previous valid file", async () => {
    const onSubmit = vi.fn<ComposerProps["onSubmit"]>();
    render(<Composer onSubmit={onSubmit} />);
    const user = userEvent.setup({ applyAccept: false });
    const good = new File(["a,b"], "orders.csv", { type: "text/csv" });
    await user.upload(screen.getByLabelText("Attach a file"), good);
    await user.upload(
      screen.getByLabelText("Attach a file"),
      new File(["x"], "orders.xlsx"),
    );
    expect(screen.getByRole("alert")).toHaveTextContent(/\.csv, \.json/);
    expect(screen.getByText("orders.csv")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Start run" }));
    expect(onSubmit).toHaveBeenCalledWith(good, "orders");
  });

  test("choosing a new file clears a failed submit's error", async () => {
    const client = new MockApiClient({ delayMs: 0 });
    vi.spyOn(client, "createRun").mockRejectedValueOnce(new Error("503"));
    render(
      <ApiProvider client={client}>
        <NewRunComposer />
      </ApiProvider>,
    );
    const user = userEvent.setup();
    const input = screen.getByLabelText("Attach a file");
    await user.upload(input, new File(["a"], "a.csv", { type: "text/csv" }));
    await user.click(screen.getByRole("button", { name: "Start run" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /Couldn't start the run/,
    );
    await user.upload(input, new File(["b"], "b.csv", { type: "text/csv" }));
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  test("submitting without a file explains what is missing", async () => {
    const onSubmit = vi.fn<ComposerProps["onSubmit"]>();
    render(<Composer onSubmit={onSubmit} />);
    await userEvent.click(screen.getByRole("button", { name: "Start run" }));
    expect(screen.getByRole("alert")).toHaveTextContent(/Choose a file/);
    expect(onSubmit).not.toHaveBeenCalled();
  });

  test("accepts a dropped file and the chosen table", async () => {
    const onSubmit = vi.fn<ComposerProps["onSubmit"]>();
    render(<Composer onSubmit={onSubmit} />);
    const file = new File(["{}"], "lineitem.json", {
      type: "application/json",
    });
    const zone = screen.getByRole("form", { name: "New pipeline run" });
    act(() => {
      zone.dispatchEvent(dragEvent("dragover", file));
      zone.dispatchEvent(dragEvent("drop", file));
    });
    expect(screen.getByText("lineitem.json")).toBeInTheDocument();
    await userEvent.selectOptions(
      screen.getByLabelText("Target table"),
      "lineitem",
    );
    await userEvent.click(screen.getByRole("button", { name: "Start run" }));
    expect(onSubmit).toHaveBeenCalledWith(file, "lineitem");
  });
});

function dragEvent(type: string, file: File): Event {
  const event = new Event(type, { bubbles: true, cancelable: true });
  Object.defineProperty(event, "dataTransfer", {
    value: { files: [file], types: ["Files"], dropEffect: "none" },
  });
  return event;
}
