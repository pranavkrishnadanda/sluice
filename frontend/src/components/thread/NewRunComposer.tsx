"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { useApi } from "@/components/api/ApiProvider";
import { ApiError, errorDetail } from "@/lib/api/errors";
import { MAX_UPLOAD_BYTES, type TableChoice } from "@/lib/api/types";
import { rememberAutoTable } from "@/lib/auto-table";
import { Composer } from "./Composer";

/** The contract's 413 copy, shown before uploading an over-limit file. */
export const TOO_LARGE_ERROR = "File too large: the limit is 100 MB.";
/** Fallback for a 422 `table_unknown` without a detail. */
export const TABLE_UNKNOWN_ERROR =
  "Couldn't tell which table this file is for — pick one.";

const isTableUnknown = (err: unknown) =>
  err instanceof ApiError && err.status === 422 && err.code === "table_unknown";

/**
 * {@link Composer} wired to the API: creates the run, then opens its thread.
 * Stays busy until navigation replaces the page.
 */
export function NewRunComposer({ placeholder }: { placeholder?: string }) {
  const api = useApi();
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tableError, setTableError] = useState<string | null>(null);

  const start = async (file: File, table: TableChoice) => {
    // Checked here, not left to the server's 413: the API rejects an
    // over-limit upload from Content-Length before reading the body, and
    // the browser then often reports a reset connection instead.
    if (file.size > MAX_UPLOAD_BYTES) {
      setError(TOO_LARGE_ERROR);
      return;
    }
    setBusy(true);
    setError(null);
    setTableError(null);
    try {
      const run = await api.createRun(file, table);
      if (table === "auto") rememberAutoTable(run.id);
      router.push(`/runs/${encodeURIComponent(run.id)}`);
    } catch (err) {
      setBusy(false);
      if (isTableUnknown(err)) {
        // Ask the user to pick: the picker shows this and takes focus.
        setTableError(errorDetail(err) ?? TABLE_UNKNOWN_ERROR);
        return;
      }
      // A 4xx detail (too large, unsupported format, unknown table) says
      // what to change; anything else is a connection or server problem.
      setError(
        errorDetail(err) ??
          "Couldn't start the run. Check the connection and try again.",
      );
    }
  };

  return (
    <Composer
      onSubmit={start}
      busy={busy}
      submitError={error}
      placeholder={placeholder}
      onFileChange={() => {
        setError(null);
        setTableError(null);
      }}
      tableError={tableError}
      onTableChange={() => setTableError(null)}
    />
  );
}
