"use client";

import { Glass } from "@/components/glass/Glass";

/**
 * Last-resort boundary for a route that throws while rendering. Kept free of
 * the shell (its own data fetching may be what failed).
 */
export default function RouteError({ reset }: { reset: () => void }) {
  return (
    <main className="grid min-h-dvh place-items-center p-4">
      <Glass
        as="section"
        role="alert"
        className="w-full max-w-md space-y-3 px-6 py-8 text-center"
      >
        <h1 className="font-semibold text-2xl tracking-tight">
          Something went wrong
        </h1>
        <p className="text-muted">
          This screen hit an unexpected error. Try again, or reload the page.
        </p>
        <button
          type="button"
          onClick={reset}
          className="inline-flex min-h-10 items-center rounded-full bg-raised px-4 font-medium text-sm transition-colors hover:bg-chip"
        >
          Try again
        </button>
      </Glass>
    </main>
  );
}
