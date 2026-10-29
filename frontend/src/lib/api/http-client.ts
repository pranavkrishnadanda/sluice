import { ApiError, parseErrorBody } from "./errors";
import type {
  ApiClient,
  DataPreview,
  EvalSummary,
  PreviewPage,
  RawInput,
  RunEvent,
  RunSummary,
  TableChoice,
  Transform,
} from "./types";

export { ApiError } from "./errors";

export interface HttpApiClientOptions {
  /** Base delay before an SSE reconnect; doubles per consecutive failure. */
  reconnectDelayMs?: number;
  /** Consecutive failed connections tolerated before `streamEvents` rejects. */
  maxReconnects?: number;
}

interface SseMessage {
  id: string | null;
  data: string;
}

/**
 * Minimal `text/event-stream` parser (WHATWG SSE): handles `id:`, multi-line
 * `data:`, comment lines and LF / CRLF / CR line endings. Other fields
 * (`event:`, `retry:`) are ignored: the contract uses unnamed messages only.
 */
async function* readSse(
  body: ReadableStream<Uint8Array>,
): AsyncGenerator<SseMessage> {
  const reader = body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let id: string | null = null;
  let data: string[] = [];
  let finished = false;
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) {
        finished = true;
        return;
      }
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split(/\r\n|\r|\n/);
      // The last piece may be an incomplete line (or a lone "\r" of "\r\n").
      buffer = lines.pop() ?? "";
      for (const line of lines) {
        if (line === "") {
          if (data.length > 0) yield { id, data: data.join("\n") };
          id = null;
          data = [];
          continue;
        }
        if (line.startsWith(":")) continue;
        const colon = line.indexOf(":");
        const field = colon === -1 ? line : line.slice(0, colon);
        let fieldValue = colon === -1 ? "" : line.slice(colon + 1);
        if (fieldValue.startsWith(" ")) fieldValue = fieldValue.slice(1);
        if (field === "data") data.push(fieldValue);
        else if (field === "id") id = fieldValue;
      }
    }
  } finally {
    // The consumer stopped early (e.g. after `run_finished`) or the read
    // failed: cancel the body so the connection is closed, not leaked.
    if (!finished) await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
}

function parseJson(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

function isRunEvent(value: unknown): value is RunEvent {
  return (
    typeof value === "object" &&
    value !== null &&
    "seq" in value &&
    typeof value.seq === "number" &&
    "type" in value &&
    typeof value.type === "string"
  );
}

const pageQuery = (page: PreviewPage) =>
  new URLSearchParams({
    offset: String(page.offset),
    limit: String(page.limit),
  });

function wait(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted || ms <= 0) return resolve();
    const done = () => {
      clearTimeout(timer);
      signal.removeEventListener("abort", done);
      resolve();
    };
    const timer = setTimeout(done, ms);
    signal.addEventListener("abort", done, { once: true });
  });
}

/**
 * {@link ApiClient} over the FastAPI backend. See `docs/api/contract.md` for
 * the route mapping. All JSON is camelCase and passed through as-is.
 */
export class HttpApiClient implements ApiClient {
  readonly isDemo = false;
  readonly #base: string;
  readonly #reconnectDelayMs: number;
  readonly #maxReconnects: number;

  constructor(baseUrl: string, opts: HttpApiClientOptions = {}) {
    this.#base = baseUrl.replace(/\/+$/, "");
    this.#reconnectDelayMs = opts.reconnectDelayMs ?? 1_000;
    this.#maxReconnects = opts.maxReconnects ?? 5;
  }

  listRuns(): Promise<RunSummary[]> {
    return this.#json("/runs");
  }

  createRun(file: File, table: TableChoice): Promise<RunSummary> {
    const body = new FormData();
    body.append("file", file);
    body.append("table", table);
    return this.#json("/runs", { method: "POST", body });
  }

  getTransforms(runId: string): Promise<Transform[]> {
    return this.#json(`/runs/${encodeURIComponent(runId)}/transforms`);
  }

  getPreview(runId: string, page: PreviewPage): Promise<DataPreview> {
    return this.#json(
      `/runs/${encodeURIComponent(runId)}/preview?${pageQuery(page)}`,
    );
  }

  getInput(
    runId: string,
    page: PreviewPage,
    signal?: AbortSignal,
  ): Promise<RawInput> {
    return this.#json(
      `/runs/${encodeURIComponent(runId)}/input?${pageQuery(page)}`,
      signal ? { signal } : {},
    );
  }

  getEvalSummary(): Promise<EvalSummary> {
    return this.#json("/evals/summary");
  }

  /**
   * Streams `GET /runs/{id}/events` until `run_finished` or abort. A dropped
   * connection is retried with `Last-Event-ID` and exponential backoff;
   * events with `seq` at or below the last delivered one are skipped, so
   * `onEvent` sees each event exactly once and in order.
   */
  async streamEvents(
    runId: string,
    onEvent: (e: RunEvent) => void,
    signal: AbortSignal,
  ): Promise<void> {
    const url = `${this.#base}/runs/${encodeURIComponent(runId)}/events`;
    let lastSeq = 0;
    let failures = 0;

    while (!signal.aborted) {
      let progressed = false;
      let lastError: unknown = null;
      try {
        const headers = new Headers({ Accept: "text/event-stream" });
        if (lastSeq > 0) headers.set("Last-Event-ID", String(lastSeq));
        const res = await fetch(url, { headers, signal, cache: "no-store" });
        if (!res.ok) throw await this.#error(res, url);
        if (!res.body) throw new Error(`Empty event stream from ${url}`);

        for await (const message of readSse(res.body)) {
          const parsed = parseJson(message.data);
          // Malformed or unrecognised messages are skipped (the contract
          // says clients ignore what they do not understand).
          if (!isRunEvent(parsed) || parsed.seq <= lastSeq) continue;
          lastSeq = parsed.seq;
          progressed = true;
          onEvent(parsed);
          if (parsed.type === "run_finished") return;
        }
      } catch (err) {
        if (signal.aborted) return;
        // Client errors (unknown run, auth) will not fix themselves.
        if (err instanceof ApiError && err.status >= 400 && err.status < 500) {
          throw err;
        }
        lastError = err;
      }
      // The stream ended or dropped before `run_finished`: reconnect.
      failures = progressed ? 0 : failures + 1;
      if (failures > this.#maxReconnects) {
        throw (
          lastError ??
          new Error(`Event stream for ${runId} closed without finishing`)
        );
      }
      const backoff = this.#reconnectDelayMs * 2 ** Math.max(0, failures - 1);
      await wait(backoff, signal);
    }
  }

  async #json<T>(path: string, init: RequestInit = {}): Promise<T> {
    const url = `${this.#base}${path}`;
    const headers = new Headers(init.headers);
    headers.set("Accept", "application/json");
    const res = await fetch(url, { ...init, headers });
    if (!res.ok) throw await this.#error(res, url);
    // Shape is trusted per the contract (no runtime schema library).
    return (await res.json()) as T;
  }

  async #error(res: Response, url: string): Promise<ApiError> {
    let text = "";
    try {
      text = await res.text();
    } catch {
      // Body unreadable: keep the status text.
    }
    const body = parseErrorBody(text);
    const message = `${res.status} ${body.detail ?? (text || res.statusText)}`;
    return new ApiError(res.status, url, message.trim(), body);
  }
}
