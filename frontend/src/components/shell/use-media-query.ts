"use client";

import { useCallback, useSyncExternalStore } from "react";

function getServerSnapshot(): boolean {
  return false;
}

/**
 * Live `window.matchMedia(query).matches`. Always `false` on the server and
 * during hydration, so markup must look right from CSS alone at first paint;
 * this only refines behaviour (e.g. `hidden`, `inert`) once mounted.
 */
export function useMediaQuery(query: string): boolean {
  const subscribe = useCallback(
    (onChange: () => void) => {
      if (typeof window.matchMedia !== "function") return () => {};
      const mql = window.matchMedia(query);
      mql.addEventListener("change", onChange);
      return () => mql.removeEventListener("change", onChange);
    },
    [query],
  );
  const getSnapshot = useCallback(
    () =>
      typeof window.matchMedia === "function" &&
      window.matchMedia(query).matches,
    [query],
  );
  return useSyncExternalStore(subscribe, getSnapshot, getServerSnapshot);
}
