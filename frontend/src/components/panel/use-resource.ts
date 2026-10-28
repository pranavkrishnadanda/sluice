"use client";

import { useCallback, useEffect, useEffectEvent, useState } from "react";

export type Resource<T> =
  | { status: "idle" }
  | { status: "loading" }
  | {
      status: "ready";
      data: T;
      /** Last good data, shown while a newer key loads. */
      stale: boolean;
    }
  | { status: "error"; retry: () => void };

interface Settled<T> {
  key: string;
  attempt: number;
  result: { ok: true; data: T } | { ok: false };
}

/**
 * Loads `load()` whenever `key` changes (`null` = not wanted yet). While a
 * new key loads, the last good data stays on screen marked `stale` instead
 * of flashing a skeleton. A result that arrives after the key moved on, or
 * after unmount, is dropped. `load` is an effect event (always the latest,
 * never a dependency), so it need not be memoised; change `key` to refetch.
 */
export function useResource<T>(
  key: string | null,
  load: () => Promise<T>,
): Resource<T> {
  const [attempt, setAttempt] = useState(0);
  const [settled, setSettled] = useState<Settled<T> | null>(null);
  const [lastGood, setLastGood] = useState<T | null>(null);

  const run = useEffectEvent(load);

  useEffect(() => {
    if (key === null) return;
    let live = true;
    run().then(
      (data) => {
        if (!live) return;
        setSettled({ key, attempt, result: { ok: true, data } });
        setLastGood(data);
      },
      () => {
        if (live) setSettled({ key, attempt, result: { ok: false } });
      },
    );
    return () => {
      live = false;
    };
  }, [key, attempt]);

  const retry = useCallback(() => setAttempt((n) => n + 1), []);

  if (key === null) return { status: "idle" };
  const current =
    settled?.key === key && settled.attempt === attempt ? settled : null;
  if (current?.result.ok) {
    return { status: "ready", data: current.result.data, stale: false };
  }
  if (current && !current.result.ok) return { status: "error", retry };
  return lastGood === null
    ? { status: "loading" }
    : { status: "ready", data: lastGood, stale: true };
}
