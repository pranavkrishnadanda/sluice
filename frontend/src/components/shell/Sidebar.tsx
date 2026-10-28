"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { useMemo } from "react";
import { errorDetail } from "@/lib/api/errors";
import type { RunStatus, RunSummary } from "@/lib/api/types";
import { groupRunsByDay } from "./group-runs";
import { ChartIcon, PlusIcon } from "./icons";
import { useRuns } from "./use-runs";

const STATUS: Record<RunStatus, { label: string; dot: string }> = {
  queued: { label: "Queued", dot: "bg-muted" },
  running: { label: "Running", dot: "bg-accent animate-pulse" },
  succeeded: { label: "Succeeded", dot: "bg-ok" },
  quarantined: { label: "Quarantined", dot: "bg-warn" },
  failed: { label: "Failed", dot: "bg-error" },
};

const ITEM =
  "flex min-h-10 items-center gap-2.5 rounded-[10px] px-2.5 text-sm transition-colors duration-150";
const ITEM_IDLE = "text-fg/85 hover:bg-raised hover:text-fg";
const ITEM_ACTIVE =
  "bg-raised font-medium text-fg shadow-[0_1px_4px_rgb(0_0_0/0.06)]";

function SectionLabel({ id, children }: { id: string; children: string }) {
  return (
    <h2
      id={id}
      className="mt-4 mb-1 px-2.5 font-medium text-[0.6875rem] text-muted uppercase tracking-[0.08em]"
    >
      {children}
    </h2>
  );
}

function RunLink({ run, active }: { run: RunSummary; active: boolean }) {
  const status = STATUS[run.status];
  return (
    <Link
      href={`/runs/${encodeURIComponent(run.id)}`}
      aria-current={active ? "page" : undefined}
      title={run.fileName}
      className={`${ITEM} ${active ? ITEM_ACTIVE : ITEM_IDLE}`}
    >
      <span
        aria-hidden="true"
        className={`size-2 shrink-0 rounded-full ${status.dot}`}
      />
      <span className="min-w-0 truncate">{run.fileName}</span>
      <span className="sr-only">, {status.label}</span>
    </Link>
  );
}

function RunList({ pathname }: { pathname: string }) {
  const { state, retry } = useRuns();
  // Grouped against the time the list was fetched, not render time, so
  // re-renders are pure and server and client markup never disagree.
  const groups = useMemo(
    () =>
      state.status === "ready"
        ? groupRunsByDay(state.runs, new Date(state.fetchedAt))
        : [],
    [state],
  );

  if (state.status === "loading") {
    return (
      <div className="mt-4 px-0.5">
        <output className="sr-only">Loading runs…</output>
        <ul aria-hidden="true" className="space-y-1.5">
          {["w-4/5", "w-3/5", "w-2/3"].map((w) => (
            <li key={w} className="flex h-10 items-center gap-2.5 px-2.5">
              <span className="size-2 rounded-full bg-chip" />
              <span className={`h-3 animate-pulse rounded-full bg-chip ${w}`} />
            </li>
          ))}
        </ul>
      </div>
    );
  }

  if (state.status === "error") {
    return (
      <div className="mt-4 space-y-2 px-2.5 text-sm">
        <p className="text-muted">
          Couldn&apos;t load runs.
          {errorDetail(state.error) && <> {errorDetail(state.error)}</>}
        </p>
        <button
          type="button"
          onClick={retry}
          className="min-h-10 rounded-[10px] bg-raised px-3 font-medium text-fg transition-colors hover:bg-chip"
        >
          Retry
        </button>
      </div>
    );
  }

  if (groups.length === 0) {
    return <p className="mt-4 px-2.5 text-muted text-sm">No runs yet</p>;
  }

  return groups.map((group) => {
    const id = `runs-${group.label.toLowerCase()}`;
    return (
      <section key={group.label} aria-labelledby={id}>
        <SectionLabel id={id}>{group.label}</SectionLabel>
        <ul className="space-y-0.5">
          {group.runs.map((run) => (
            <li key={run.id}>
              <RunLink
                run={run}
                active={pathname === `/runs/${encodeURIComponent(run.id)}`}
              />
            </li>
          ))}
        </ul>
      </section>
    );
  });
}

/**
 * Primary navigation: start a run, recent runs grouped by day, and evals.
 * Lives inside the shell's sidebar (or mobile drawer) container.
 */
export function Sidebar() {
  const pathname = usePathname();
  const onNewRun = pathname === "/runs";
  const onEvals = pathname === "/evals" || pathname.startsWith("/evals/");

  return (
    <nav aria-label="Primary" className="flex h-full min-h-0 flex-col p-3">
      <Link
        href="/runs"
        aria-current={onNewRun ? "page" : undefined}
        className={`${ITEM} bg-raised font-semibold text-fg shadow-[0_1px_4px_rgb(0_0_0/0.06)] hover:shadow-[0_2px_10px_rgb(0_0_0/0.1)]`}
      >
        <PlusIcon size={16} />
        New pipeline run
      </Link>

      <div className="-mx-1 mt-1 min-h-0 flex-1 overflow-y-auto px-1 pb-2">
        <RunList pathname={pathname} />
      </div>

      <div className="border-fg/10 border-t pt-2">
        <Link
          href="/evals"
          aria-current={onEvals ? "page" : undefined}
          className={`${ITEM} ${onEvals ? ITEM_ACTIVE : ITEM_IDLE}`}
        >
          <ChartIcon size={16} />
          Evals
        </Link>
      </div>
    </nav>
  );
}
