import { CheckIcon, CrossIcon, RetryIcon } from "@/components/shell/icons";
import type { RunViewState } from "@/lib/run-state";
import { ActivityLine } from "./ActivityLine";
import {
  buildSteps,
  STEP_STATUS_TEXT,
  type Step,
  type StepStatus,
} from "./steps";

const META_TONE: Record<StepStatus, string> = {
  done: "text-muted",
  active: "text-muted",
  retrying: "font-medium text-warn",
  failed: "font-medium text-error",
  pending: "text-muted",
};

function StatusIcon({ status }: { status: StepStatus }) {
  switch (status) {
    case "done":
      return <CheckIcon size={16} className="text-ok" />;
    case "retrying":
      return <RetryIcon size={16} className="text-warn" />;
    case "failed":
      return <CrossIcon size={16} className="text-error" />;
    case "active":
      // Pulses while in flight; static under reduced motion (globals.css).
      return (
        <span
          aria-hidden="true"
          className="relative grid size-4 place-items-center"
        >
          <span className="absolute size-3 animate-ping rounded-full bg-accent/30" />
          <span className="size-2 rounded-full bg-accent" />
        </span>
      );
    case "pending":
      return (
        <span
          aria-hidden="true"
          className="size-2.5 rounded-full border-[1.5px] border-muted/60"
        />
      );
  }
}

function StepRow({ step }: { step: Step }) {
  const quiet = step.status === "pending";
  return (
    <li className="rise-in flex items-start gap-3 py-2.5">
      <span className="sr-only">{STEP_STATUS_TEXT[step.status]}: </span>
      <span className="grid h-6 w-4 shrink-0 place-items-center">
        <StatusIcon status={step.status} />
      </span>
      <div className="min-w-0 flex-1">
        <p
          className={`text-pretty text-sm leading-6 ${quiet ? "text-muted" : ""}`}
        >
          <span className={quiet ? "" : "font-medium"}>{step.label}</span>
          {step.detail && (
            <span className={step.detailWarn ? "text-warn" : "text-muted"}>
              {" — "}
              {step.detail}
            </span>
          )}
        </p>
        {(step.meta || step.note) && (
          <p className="mt-0.5 flex flex-wrap gap-x-2 text-xs leading-5">
            {step.meta && (
              <span className={META_TONE[step.status]}>{step.meta}</span>
            )}
            {step.note && (
              <span className="min-w-0 break-words text-muted">
                {step.note}
              </span>
            )}
          </p>
        )}
        {step.activity && (
          // Keyed by phase so each new phase rises in and restarts its clock.
          <ActivityLine key={step.activity.seq} activity={step.activity} />
        )}
      </div>
      {step.chip && (
        <span className="mt-0.5 shrink-0 whitespace-nowrap rounded-full bg-chip px-2 py-0.5 font-mono text-[0.6875rem] text-fg leading-5">
          {step.chip}
        </span>
      )}
    </li>
  );
}

export interface StepListProps {
  state: RunViewState;
  /** Stream connected and unfinished: show the in-flight step. */
  live?: boolean;
  /** Precomputed rows (the thread also uses them for announcements). */
  steps?: Step[];
}

/**
 * The run's steps as an ordered list on a glass card. Each row says its
 * status in text (visually hidden prefix) as well as by icon and colour.
 * Rows animate in once (stable keys); a status change updates in place.
 */
export function StepList({ state, live = false, steps }: StepListProps) {
  const rows = steps ?? buildSteps(state, live);
  return (
    <ol
      aria-label="Pipeline steps"
      className="divide-y divide-fg/[0.06] rounded-2xl bg-raised/50 px-4 py-1 shadow-[inset_0_1px_0_rgb(255_255_255/0.35)]"
    >
      {rows.map((step) => (
        <StepRow key={step.id} step={step} />
      ))}
    </ol>
  );
}
