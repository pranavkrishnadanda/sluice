"use client";

import type { ReactNode } from "react";
import { useApi } from "@/components/api/ApiProvider";
import { Glass } from "@/components/glass/Glass";
import { useResource } from "@/components/panel/use-resource";
import { DemoBadge } from "@/components/shell/DemoBadge";
import { errorDetail } from "@/lib/api/errors";
import type { EvalSummary } from "@/lib/api/types";
import { ParetoChart } from "./ParetoChart";
import { ResultsTable } from "./ResultsTable";

const GENERATED = new Intl.DateTimeFormat("en-GB", {
  dateStyle: "medium",
  timeStyle: "short",
  timeZone: "UTC",
});

function Card({
  id,
  title,
  meta,
  children,
}: {
  id: string;
  title: string;
  meta?: ReactNode;
  children: ReactNode;
}) {
  return (
    <Glass as="section" aria-labelledby={id} className="min-w-0 p-4 sm:p-5">
      <div className="mb-4 flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
        <h2 id={id} className="font-semibold text-base tracking-tight">
          {title}
        </h2>
        {meta}
      </div>
      {children}
    </Glass>
  );
}

function Skeleton({ what, height }: { what: string; height: string }) {
  return (
    <output aria-label={`Loading ${what}`} className="block space-y-2.5">
      <span className="block h-3 w-28 animate-pulse rounded-full bg-chip" />
      <span className={`block ${height} animate-pulse rounded-xl bg-chip`} />
    </output>
  );
}

function Unavailable({ error, retry }: { error: unknown; retry: () => void }) {
  const detail = errorDetail(error);
  return (
    <Glass className="p-4 sm:p-5">
      <div
        role="alert"
        className="grid min-h-40 place-items-center gap-3 rounded-xl bg-chip px-4 py-6 text-center"
      >
        <p className="text-sm">
          <span className="font-medium text-error">
            Couldn’t load the eval results.
          </span>{" "}
          <span className="text-muted">
            {detail ?? "Check the connection and try again."}
          </span>
        </p>
        <button
          type="button"
          onClick={retry}
          className="glass glass-pill inline-flex h-10 items-center px-4 font-medium text-fg text-sm transition-colors duration-150 hover:bg-raised"
        >
          Retry
        </button>
      </div>
    </Glass>
  );
}

function Ready({ summary }: { summary: EvalSummary }) {
  if (summary.systems.length === 0 && summary.pareto.length === 0) {
    return (
      <Glass className="p-4 sm:p-5">
        <ResultsTable summary={summary} />
      </Glass>
    );
  }
  const generated = new Date(summary.generatedAt);
  return (
    <>
      <Card
        id="evals-results"
        title="Results"
        meta={
          <div className="flex flex-wrap items-center gap-2">
            {Number.isNaN(generated.getTime()) ? null : (
              <p className="text-muted text-xs">
                Generated{" "}
                <time dateTime={summary.generatedAt}>
                  {GENERATED.format(generated)} UTC
                </time>
              </p>
            )}
            <DemoBadge variant="compact" />
          </div>
        }
      >
        <ResultsTable summary={summary} />
      </Card>
      <Card
        id="evals-pareto"
        title="Cost vs quality"
        meta={<DemoBadge variant="compact" />}
      >
        <ParetoChart summary={summary} />
      </Card>
    </>
  );
}

/**
 * The evals page body: fetches the summary once and renders the results
 * table and the cost–quality chart, with loading, error (retry) and empty
 * states.
 */
export function EvalsView() {
  const api = useApi();
  const summary = useResource("eval-summary", () => api.getEvalSummary());

  switch (summary.status) {
    case "idle":
    case "loading":
      return (
        <>
          <Card id="evals-results" title="Results">
            <Skeleton what="results" height="h-40" />
          </Card>
          <Card id="evals-pareto" title="Cost vs quality">
            <Skeleton what="chart" height="h-72 sm:h-80" />
          </Card>
        </>
      );
    case "error":
      return <Unavailable error={summary.error} retry={summary.retry} />;
    case "ready":
      return <Ready summary={summary.data} />;
  }
}
