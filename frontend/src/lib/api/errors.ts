/**
 * API error handling shared by the HTTP and mock clients. The contract's
 * error body is `{"detail": string, "code"?: string}` for every 4xx / 5xx.
 */

export interface ErrorBody {
  /** Human-readable reason; `null` when the body did not carry a string. */
  detail: string | null;
  /** Optional machine-readable code, e.g. `"too_large"`. */
  code: string | null;
}

/** A non-2xx response from the Sluice API. */
export class ApiError extends Error {
  readonly status: number;
  readonly url: string;
  readonly detail: string | null;
  readonly code: string | null;

  constructor(
    status: number,
    url: string,
    message: string,
    body: Partial<ErrorBody> = {},
  ) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.url = url;
    this.detail = body.detail ?? null;
    this.code = body.code ?? null;
  }
}

/** Parses an error body; anything off-contract yields no detail or code. */
export function parseErrorBody(text: string): ErrorBody {
  try {
    const value: unknown = JSON.parse(text);
    if (typeof value !== "object" || value === null) {
      return { detail: null, code: null };
    }
    const detail =
      "detail" in value && typeof value.detail === "string"
        ? value.detail
        : null;
    const code =
      "code" in value && typeof value.code === "string" ? value.code : null;
    return { detail, code };
  } catch {
    return { detail: null, code: null };
  }
}

/**
 * The server's own explanation, for client errors only (4xx with a string
 * `detail`, e.g. a 413 "file too large"). Network failures and 5xx return
 * `null`: their bodies are not meant for users, so callers keep their
 * connection copy.
 */
export function errorDetail(err: unknown): string | null {
  if (!(err instanceof ApiError)) return null;
  if (err.status < 400 || err.status >= 500) return null;
  const detail = err.detail?.trim();
  return detail ? detail : null;
}

/** `true` for a 404 from the API (e.g. an unknown run id). */
export function isNotFound(err: unknown): boolean {
  return err instanceof ApiError && err.status === 404;
}

/**
 * `true` for the preview's `409 not_ready`: the run has loaded but its
 * preview is still being written, and the contract says to retry shortly.
 */
export function isNotReady(err: unknown): boolean {
  return (
    err instanceof ApiError && err.status === 409 && err.code === "not_ready"
  );
}

/** Waits between retries of a `not_ready` answer: about 16 s in all. */
export const NOT_READY_RETRY_MS: readonly number[] = [
  250, 500, 1_000, 2_000, 4_000, 8_000,
];

/**
 * Calls `load`, retrying while it rejects with `409 not_ready` (see
 * {@link isNotReady}), once after each delay in `delays`. Any other error,
 * or `not_ready` after the last retry, rejects as usual. Aborting `signal`
 * clears the pending wait and rejects with the signal's reason (an
 * `AbortError`), so a closed panel or a changed run stops retrying.
 */
export async function retryWhileNotReady<T>(
  load: () => Promise<T>,
  delays: readonly number[] = NOT_READY_RETRY_MS,
  signal?: AbortSignal,
): Promise<T> {
  for (const delay of delays) {
    signal?.throwIfAborted();
    try {
      return await load();
    } catch (err) {
      if (!isNotReady(err)) throw err;
    }
    await wait(delay, signal);
  }
  signal?.throwIfAborted();
  return load();
}

/** Resolves after `ms`, or rejects (timer cleared) once `signal` aborts. */
function wait(ms: number, signal?: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal?.aborted) {
      reject(signal.reason);
      return;
    }
    const onAbort = () => {
      clearTimeout(timer);
      reject(signal?.reason);
    };
    const timer = setTimeout(() => {
      signal?.removeEventListener("abort", onAbort);
      resolve();
    }, ms);
    signal?.addEventListener("abort", onAbort, { once: true });
  });
}
