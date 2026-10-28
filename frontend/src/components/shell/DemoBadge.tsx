"use client";

import { useApi } from "@/components/api/ApiProvider";

export type DemoBadgeVariant = "header" | "compact";

const VARIANT: Record<DemoBadgeVariant, { box: string; dot: string }> = {
  // On glass, not the bare backdrop: the contrast guard covers --warn there.
  header: {
    box: "glass glass-pill h-9 gap-1.5 px-3.5 text-xs",
    dot: "size-1.5",
  },
  // A chip on a glass card or panel (chip-on-glass is in the contrast guard).
  compact: {
    box: "h-5 gap-1 rounded-full bg-chip px-2 text-[0.6875rem] leading-none",
    dot: "size-1",
  },
};

export interface DemoBadgeProps {
  /** `header` in the app bar; `compact` beside numbers on cards and panels. */
  variant?: DemoBadgeVariant;
  className?: string;
}

/**
 * Honesty marker: visible whenever the app is showing fixture data from the
 * mock client rather than a live backend. Renders nothing otherwise.
 */
export function DemoBadge({ variant = "header", className }: DemoBadgeProps) {
  const api = useApi();
  if (!api.isDemo) return null;
  return (
    <span
      title="Sample runs from built-in fixtures; no backend is connected."
      className={[
        "inline-flex shrink-0 select-none items-center font-medium text-warn",
        VARIANT[variant].box,
        className,
      ]
        .filter(Boolean)
        .join(" ")}
    >
      <span
        aria-hidden="true"
        className={`rounded-full bg-current ${VARIANT[variant].dot}`}
      />
      <span>Demo data</span>
      <span className="sr-only">
        : sample runs from built-in fixtures, no backend connected
      </span>
    </span>
  );
}
