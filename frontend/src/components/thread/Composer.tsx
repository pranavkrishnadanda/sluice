"use client";

import {
  type ChangeEvent,
  type DragEvent,
  type FormEvent,
  useId,
  useRef,
  useState,
} from "react";
import {
  ArrowUpIcon,
  ChevronDownIcon,
  CrossIcon,
  PaperclipIcon,
} from "@/components/shell/icons";
import type { TableName } from "@/lib/api/types";
import { FileChip } from "./FileChip";

export const ACCEPTED_EXTENSIONS = [".csv", ".json", ".parquet"] as const;
const TABLES: readonly TableName[] = ["orders", "lineitem"];

const TYPE_ERROR = "Only .csv, .json or .parquet files can be loaded.";
const MISSING_ERROR = "Choose a file to load first.";

export function isAcceptedFile(file: File): boolean {
  const name = file.name.toLowerCase();
  return ACCEPTED_EXTENSIONS.some((ext) => name.endsWith(ext));
}

export interface ComposerProps {
  onSubmit: (file: File, table: TableName) => void | Promise<void>;
  /** A submit is in flight: the send button reports busy and ignores clicks. */
  busy?: boolean;
  /** Error from the last submit (e.g. the API refused the run). */
  submitError?: string | null;
  placeholder?: string;
}

/**
 * Glass input bar for a new run: attach (picker or drag-and-drop anywhere on
 * the bar), target table, send. Only .csv / .json / .parquet are accepted;
 * anything else shows an inline error and never submits.
 */
export function Composer({
  onSubmit,
  busy = false,
  submitError = null,
  placeholder = "Drop a CSV, JSON or Parquet file, or browse",
}: ComposerProps) {
  const [file, setFile] = useState<File | null>(null);
  const [table, setTable] = useState<TableName>("orders");
  const [error, setError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);
  const input = useRef<HTMLInputElement>(null);
  const errorId = useId();

  const shownError = error ?? submitError;

  const take = (picked: File | undefined) => {
    if (!picked) return;
    if (isAcceptedFile(picked)) {
      setFile(picked);
      setError(null);
    } else {
      setFile(null);
      setError(TYPE_ERROR);
    }
  };

  const onPick = (e: ChangeEvent<HTMLInputElement>) => {
    take(e.target.files?.[0]);
    // Allow picking the same file again after removing it.
    e.target.value = "";
  };

  const onDragOver = (e: DragEvent<HTMLFormElement>) => {
    if (!e.dataTransfer.types.includes("Files")) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = "copy";
    setDragging(true);
  };

  const onDragLeave = (e: DragEvent<HTMLFormElement>) => {
    const to = e.relatedTarget;
    if (to instanceof Node && e.currentTarget.contains(to)) return;
    setDragging(false);
  };

  const onDrop = (e: DragEvent<HTMLFormElement>) => {
    e.preventDefault();
    setDragging(false);
    take(e.dataTransfer.files[0]);
  };

  const submit = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    if (busy) return;
    if (!file) {
      setError(error ?? MISSING_ERROR);
      input.current?.focus();
      return;
    }
    void onSubmit(file, table);
  };

  const clear = () => {
    setFile(null);
    setError(null);
    input.current?.focus();
  };

  return (
    <div className="space-y-2">
      {/* Drag-and-drop is a pointer shortcut; the file input is the keyboard path. */}
      {/* oxlint-disable-next-line jsx-a11y/no-noninteractive-element-interactions */}
      <form
        aria-label="New pipeline run"
        onSubmit={submit}
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        onDrop={onDrop}
        className={[
          "glass flex min-w-0 items-center gap-1.5 rounded-[26px] p-1.5 transition-shadow duration-150",
          dragging
            ? "outline-2 outline-accent outline-dashed outline-offset-2"
            : "",
        ].join(" ")}
      >
        <label className="flex min-h-11 min-w-0 flex-1 cursor-pointer items-center gap-2 rounded-full pr-2 pl-2.5 transition-colors duration-150 hover:bg-raised has-[input:focus-visible]:outline-2 has-[input:focus-visible]:outline-(--focus-ring) has-[input:focus-visible]:outline-offset-2">
          <input
            ref={input}
            type="file"
            accept={ACCEPTED_EXTENSIONS.join(",")}
            aria-label="Attach a file"
            aria-invalid={shownError ? true : undefined}
            aria-describedby={shownError ? errorId : undefined}
            onChange={onPick}
            className="sr-only"
          />
          <PaperclipIcon className="shrink-0 text-muted" />
          {file ? (
            <FileChip name={file.name} />
          ) : (
            <span className="min-w-0 truncate text-muted text-sm">
              {dragging ? (
                "Drop to attach"
              ) : (
                <>
                  <span className="sm:hidden">Attach a file</span>
                  <span className="max-sm:hidden">{placeholder}</span>
                </>
              )}
            </span>
          )}
        </label>

        {file && (
          <button
            type="button"
            onClick={clear}
            aria-label={`Remove ${file.name}`}
            className="grid size-9 shrink-0 place-items-center rounded-full text-muted transition-colors duration-150 hover:bg-raised hover:text-fg"
          >
            <CrossIcon size={14} />
          </button>
        )}

        <span className="relative shrink-0">
          <select
            aria-label="Target table"
            value={table}
            onChange={(e) => setTable(e.target.value as TableName)}
            className="h-11 cursor-pointer appearance-none rounded-full bg-chip pr-8 pl-3.5 font-medium text-fg text-sm transition-colors duration-150 hover:bg-raised"
          >
            {TABLES.map((t) => (
              <option key={t} value={t}>
                {t}
              </option>
            ))}
          </select>
          <ChevronDownIcon
            size={14}
            className="pointer-events-none absolute top-1/2 right-3 -translate-y-1/2 text-muted"
          />
        </span>

        <button
          type="submit"
          aria-label="Start run"
          aria-disabled={busy || undefined}
          className={`accent-fill grid size-11 shrink-0 place-items-center rounded-full transition-[transform,filter] duration-150 hover:brightness-105 active:scale-95 ${busy ? "cursor-progress opacity-80" : ""}`}
        >
          <ArrowUpIcon size={18} className={busy ? "animate-pulse" : ""} />
        </button>
      </form>
      {shownError && (
        <p id={errorId} role="alert" className="px-4 text-error text-sm">
          {shownError}
        </p>
      )}
    </div>
  );
}
