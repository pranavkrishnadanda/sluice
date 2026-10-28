"use client";

import { useApi } from "@/components/api/ApiProvider";

/**
 * Honesty marker: visible whenever the app is showing fixture data from the
 * mock client rather than a live backend. Renders nothing otherwise.
 */
export function DemoBadge({ className }: { className?: string }) {
  const api = useApi();
  if (!api.isDemo) return null;
  return (
    <span
      title="Sample runs from built-in fixtures; no backend is connected."
      className={[
        // On glass, not the bare backdrop: the contrast guard covers --warn there.
        "glass glass-pill inline-flex h-9 select-none items-center gap-1.5 px-3.5 font-medium text-warn text-xs",
        className,
      ]
        .filter(Boolean)
        .join(" ")}
    >
      <span aria-hidden="true" className="size-1.5 rounded-full bg-current" />
      <span>Demo data</span>
      <span className="sr-only">
        : sample runs from built-in fixtures, no backend connected
      </span>
    </span>
  );
}
