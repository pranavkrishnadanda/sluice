"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { useApi } from "@/components/api/ApiProvider";
import { errorDetail } from "@/lib/api/errors";
import { MAX_UPLOAD_BYTES, type TableName } from "@/lib/api/types";
import { Composer } from "./Composer";

/** The contract's 413 copy, shown before uploading an over-limit file. */
export const TOO_LARGE_ERROR = "File too large: the limit is 100 MB.";

/**
 * {@link Composer} wired to the API: creates the run, then opens its thread.
 * Stays busy until navigation replaces the page.
 */
export function NewRunComposer({ placeholder }: { placeholder?: string }) {
  const api = useApi();
  const router = useRouter();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const start = async (file: File, table: TableName) => {
    // Checked here, not left to the server's 413: the API rejects an
    // over-limit upload from Content-Length before reading the body, and
    // the browser then often reports a reset connection instead.
    if (file.size > MAX_UPLOAD_BYTES) {
      setError(TOO_LARGE_ERROR);
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const run = await api.createRun(file, table);
      router.push(`/runs/${encodeURIComponent(run.id)}`);
    } catch (err) {
      setBusy(false);
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
      onFileChange={() => setError(null)}
    />
  );
}
