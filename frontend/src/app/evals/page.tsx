import type { Metadata } from "next";
import { EvalsView } from "@/components/evals/EvalsView";
import { AppShell } from "@/components/shell/AppShell";

export const metadata: Metadata = {
  title: "Evals",
};

/**
 * Eval results. The summary comes from the API client (mock fixtures in the
 * demo, flagged by the header's Demo badge), so it loads on the client;
 * the shell and copy render on the server.
 */
export default function EvalsPage() {
  return (
    <AppShell>
      <section
        aria-labelledby="evals-title"
        className="flex flex-col gap-5 py-6 sm:py-10"
      >
        <header>
          <h1
            id="evals-title"
            className="text-balance font-semibold text-3xl tracking-tight sm:text-4xl"
          >
            Evals
          </h1>
          <p className="mt-3 max-w-xl text-pretty text-base text-muted">
            Every system scored on the same eval split, each metric with its 95%
            confidence interval. The chart plots quality against cost for the
            baselines and for each router setting.
          </p>
        </header>
        <EvalsView />
      </section>
    </AppShell>
  );
}
