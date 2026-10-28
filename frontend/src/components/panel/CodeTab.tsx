"use client";

import { type CSSProperties, useEffect, useId, useMemo, useState } from "react";
import { CheckIcon, CrossIcon } from "@/components/shell/icons";
import type { Transform } from "@/lib/api/types";
import { type DiffLine, lineDiff } from "@/lib/diff";
import { type HighlightToken, highlight, languageOf } from "@/lib/highlight";

const WELL =
  "max-h-[26rem] overflow-auto overscroll-contain rounded-xl bg-glass-solid py-2.5 font-mono text-[0.75rem] leading-5 ring-1 ring-black/10 ring-inset dark:ring-white/10";

/** Highlighted tokens for `code`, or `null` until (or unless) Shiki loads. */
function useHighlighted(
  code: string,
  language: Transform["language"],
): HighlightToken[][] | null {
  const [result, setResult] = useState<{
    code: string;
    lines: HighlightToken[][];
  } | null>(null);
  useEffect(() => {
    let live = true;
    highlight(code, languageOf(language)).then(
      (lines) => {
        if (live) setResult({ code, lines });
      },
      () => {
        // Highlighting is progressive enhancement: plain text stays.
      },
    );
    return () => {
      live = false;
    };
  }, [code, language]);
  return result?.code === code ? result.lines : null;
}

function CodeBlock({ transform }: { transform: Transform }) {
  const tokens = useHighlighted(transform.code, transform.language);
  const plain = useMemo(
    () => transform.code.replace(/\n$/, "").split("\n"),
    [transform.code],
  );
  return (
    <figure
      /* oxlint-disable jsx-a11y/no-noninteractive-tabindex */
      // biome-ignore lint/a11y/noNoninteractiveTabindex: a scrollable region must be keyboard-focusable (WCAG 2.1.1).
      tabIndex={0}
      /* oxlint-enable jsx-a11y/no-noninteractive-tabindex */
      aria-label={`Attempt ${transform.attempt} code (${transform.language === "sql" ? "SQL" : "Polars"})`}
      className={WELL}
    >
      <pre>
        <code className="block w-max min-w-full">
          {plain.map((text, i) => {
            const lineTokens = tokens?.[i];
            return (
              // biome-ignore lint/suspicious/noArrayIndexKey: lines are positional.
              <span key={i} className="flex">
                <span
                  aria-hidden="true"
                  className="w-9 shrink-0 select-none pr-3 text-right text-muted"
                >
                  {i + 1}
                </span>
                <span className="pr-4 whitespace-pre">
                  {lineTokens
                    ? lineTokens.map((t, j) => (
                        <span
                          // biome-ignore lint/suspicious/noArrayIndexKey: tokens are positional.
                          key={j}
                          className="text-(--tok-light) dark:text-(--tok-dark)"
                          style={
                            {
                              "--tok-light": t.light,
                              "--tok-dark": t.dark,
                            } as CSSProperties
                          }
                        >
                          {t.content}
                        </span>
                      ))
                    : text || " "}
                </span>
              </span>
            );
          })}
        </code>
      </pre>
    </figure>
  );
}

const DIFF_ROW: Record<DiffLine["kind"], string> = {
  same: "",
  add: "bg-(--diff-add-bg)",
  del: "bg-(--diff-del-bg)",
};

function DiffView({ before, after }: { before: Transform; after: Transform }) {
  const lines = useMemo(
    () => lineDiff(before.code, after.code),
    [before.code, after.code],
  );
  const added = lines.filter((l) => l.kind === "add").length;
  const removed = lines.filter((l) => l.kind === "del").length;
  return (
    <div className="space-y-2">
      <p className="text-muted text-xs">
        Attempt {before.attempt} → {after.attempt}:{" "}
        <span className="font-medium text-ok">+{added}</span>{" "}
        <span className="font-medium text-error">−{removed}</span> lines
      </p>
      <figure
        /* oxlint-disable jsx-a11y/no-noninteractive-tabindex */
        // biome-ignore lint/a11y/noNoninteractiveTabindex: a scrollable region must be keyboard-focusable (WCAG 2.1.1).
        tabIndex={0}
        /* oxlint-enable jsx-a11y/no-noninteractive-tabindex */
        aria-label={`Changes from attempt ${before.attempt} to attempt ${after.attempt}`}
        className={WELL}
      >
        <pre>
          <code className="block w-max min-w-full">
            {lines.map((line, i) => {
              const marker =
                line.kind === "add" ? "+" : line.kind === "del" ? "−" : " ";
              const text = line.text || " ";
              return (
                <span
                  // biome-ignore lint/suspicious/noArrayIndexKey: lines are positional.
                  key={i}
                  data-kind={line.kind}
                  className={`flex ${DIFF_ROW[line.kind]}`}
                >
                  <span
                    aria-hidden="true"
                    className={`w-7 shrink-0 select-none text-center font-semibold ${
                      line.kind === "add"
                        ? "text-(--diff-add)"
                        : line.kind === "del"
                          ? "text-(--diff-del)"
                          : "text-muted"
                    }`}
                  >
                    {marker}
                  </span>
                  {line.kind === "add" ? (
                    <ins className="pr-4 whitespace-pre no-underline">
                      {text}
                    </ins>
                  ) : line.kind === "del" ? (
                    <del className="pr-4 whitespace-pre no-underline">
                      {text}
                    </del>
                  ) : (
                    <span className="pr-4 whitespace-pre">{text}</span>
                  )}
                </span>
              );
            })}
          </code>
        </pre>
      </figure>
    </div>
  );
}

export interface TransformGroup {
  nodeId: string;
  title: string;
  /** Sorted by attempt, ascending. */
  attempts: Transform[];
}

/** Groups transforms by node, in plan order (unknown nodes last). */
export function groupTransforms(
  transforms: readonly Transform[],
  nodes: readonly { id: string; title: string }[],
): TransformGroup[] {
  const order = new Map(nodes.map((n, i) => [n.id, i]));
  const titles = new Map(nodes.map((n) => [n.id, n.title]));
  const groups = new Map<string, Transform[]>();
  for (const t of transforms) {
    const list = groups.get(t.nodeId) ?? [];
    list.push(t);
    groups.set(t.nodeId, list);
  }
  return [...groups.entries()]
    .sort(
      ([a], [b]) =>
        (order.get(a) ?? Number.MAX_SAFE_INTEGER) -
          (order.get(b) ?? Number.MAX_SAFE_INTEGER) || a.localeCompare(b),
    )
    .map(([nodeId, list]) => ({
      nodeId,
      title: titles.get(nodeId) ?? nodeId,
      attempts: [...list].sort((x, y) => x.attempt - y.attempt),
    }));
}

function NodeTransforms({ group }: { group: TransformGroup }) {
  const headingId = useId();
  const latest = group.attempts.at(-1);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [diff, setDiff] = useState(false);
  // A newly streamed attempt becomes the default until the user picks one.
  const selected =
    group.attempts.find((t) => t.id === selectedId) ?? latest ?? null;
  if (!selected) return null;
  const index = group.attempts.indexOf(selected);
  const previous = index > 0 ? group.attempts[index - 1] : undefined;
  const multi = group.attempts.length >= 2;

  return (
    <section aria-labelledby={headingId} className="space-y-2.5">
      <h2 id={headingId} className="text-pretty font-medium text-sm leading-5">
        <span className="mr-1.5 font-mono text-muted text-xs">
          {group.nodeId}
        </span>
        {group.title}
      </h2>

      <div className="flex flex-wrap items-center justify-between gap-2">
        <fieldset className="flex flex-wrap gap-1 rounded-[12px] bg-chip p-1">
          <legend className="sr-only">Attempt</legend>
          {group.attempts.map((t) => (
            <label
              key={t.id}
              className="inline-flex min-h-8 cursor-pointer items-center gap-1.5 rounded-[9px] px-2.5 font-medium text-muted text-xs transition-colors duration-150 has-checked:bg-raised has-checked:text-fg has-checked:shadow-sm has-focus-visible:outline-2 has-focus-visible:outline-(--focus-ring)"
            >
              <input
                type="radio"
                name={headingId}
                value={t.id}
                checked={t.id === selected.id}
                onChange={() => setSelectedId(t.id)}
                className="sr-only"
              />
              {t.passed ? (
                <CheckIcon size={14} className="text-ok" />
              ) : (
                <CrossIcon size={14} className="text-error" />
              )}
              Attempt {t.attempt}
              <span className="sr-only">
                , {t.passed ? "passed" : "failed"}
              </span>
            </label>
          ))}
        </fieldset>

        {multi && (
          <button
            type="button"
            role="switch"
            aria-checked={diff && previous !== undefined}
            disabled={previous === undefined}
            onClick={() => setDiff((v) => !v)}
            className="group inline-flex min-h-8 items-center gap-2 rounded-full px-1 font-medium text-fg text-xs disabled:cursor-not-allowed disabled:text-muted"
          >
            <span
              aria-hidden="true"
              className="relative h-4.5 w-8 rounded-full bg-chip shadow-[inset_0_0_0_1px_var(--text-muted)] transition-colors duration-150 group-aria-checked:bg-accent group-aria-checked:shadow-none"
            >
              <span className="absolute top-0.5 left-0.5 size-3.5 rounded-full bg-glass-solid shadow-sm transition-transform duration-150 group-aria-checked:translate-x-3.5" />
            </span>
            Diff vs previous attempt
          </button>
        )}
      </div>

      {diff && previous ? (
        <DiffView before={previous} after={selected} />
      ) : (
        <CodeBlock transform={selected} />
      )}
    </section>
  );
}

/**
 * Why a run may have no transforms, which picks the empty copy:
 * still running, rules handled everything (`noLlmWork`), the run failed,
 * or nothing more specific is known.
 */
export type NoCodeReason = "pending" | "rules" | "failed" | "none";

const NO_CODE: Record<NoCodeReason, string> = {
  pending: "No generated code yet — transforms appear as plan steps finish.",
  rules: "No generated code — deterministic rules handled this run.",
  failed:
    "No generated code — the run failed before any transform was written.",
  none: "No generated code for this run.",
};

export interface CodeTabProps {
  transforms: readonly Transform[];
  nodes: readonly { id: string; title: string }[];
  /** Picks the copy shown when there are no transforms. */
  emptyReason: NoCodeReason;
}

/**
 * Generated transforms, one section per plan node, each with an attempt
 * picker and (for repaired nodes) a line diff against the previous attempt.
 * Highlighting loads Shiki on demand; the plain code renders first.
 */
export function CodeTab({ transforms, nodes, emptyReason }: CodeTabProps) {
  const groups = useMemo(
    () => groupTransforms(transforms, nodes),
    [transforms, nodes],
  );
  if (groups.length === 0) {
    return (
      <p className="grid min-h-40 place-items-center rounded-xl bg-chip px-4 text-center text-muted text-sm">
        {NO_CODE[emptyReason]}
      </p>
    );
  }
  return (
    <div className="space-y-6">
      {groups.map((g) => (
        <NodeTransforms key={g.nodeId} group={g} />
      ))}
    </div>
  );
}
