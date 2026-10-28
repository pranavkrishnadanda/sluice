import type { Metadata } from "next";
import { AppShell } from "@/components/shell/AppShell";
import { NewRunComposer } from "@/components/thread/NewRunComposer";

export const metadata: Metadata = {
  title: "New run",
};

const STAGES = [
  ["Profile", "Find drifted columns, formats and units"],
  ["Repair", "Deterministic rules first, models only where needed"],
  ["Verify", "Every row checked against the table contract"],
  ["Load", "Clean rows in, the rest held in quarantine"],
] as const;

export default function RunsPage() {
  return (
    <AppShell>
      <section
        aria-labelledby="new-run-title"
        className="flex flex-1 flex-col justify-center py-6 sm:py-12"
      >
        <h1
          id="new-run-title"
          className="text-balance font-semibold text-3xl tracking-tight sm:text-4xl"
        >
          New pipeline run
        </h1>
        <p className="mt-3 max-w-xl text-pretty text-base text-muted sm:text-lg">
          Give Sluice a CSV, JSON or Parquet file and the table it belongs in.
          It plans the cleanup, shows every step as it runs, and only loads what
          passes the contract. Or open a recent run from the sidebar.
        </p>
        <div className="mt-8">
          <NewRunComposer />
        </div>
        <h2 className="mt-10 mb-3 font-medium text-muted text-xs uppercase tracking-[0.08em]">
          What happens next
        </h2>
        <ol className="grid gap-2 sm:grid-cols-2">
          {STAGES.map(([title, detail], i) => (
            <li
              key={title}
              className="flex gap-3 rounded-2xl bg-raised px-4 py-3 shadow-[inset_0_1px_0_rgb(255_255_255/0.5)]"
            >
              <span
                aria-hidden="true"
                className="mt-0.5 grid size-6 shrink-0 place-items-center rounded-full bg-chip font-medium font-mono text-muted text-xs"
              >
                {i + 1}
              </span>
              <span className="min-w-0">
                <span className="block font-medium text-sm">{title}</span>
                <span className="block text-muted text-sm">{detail}</span>
              </span>
            </li>
          ))}
        </ol>
      </section>
    </AppShell>
  );
}
