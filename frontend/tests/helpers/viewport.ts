import { vi } from "vitest";

/**
 * Emulates a viewport `width` px wide for `window.matchMedia`: `min-width` /
 * `max-width` px queries are evaluated against it; anything else is false.
 * Undo with `vi.unstubAllGlobals()`.
 */
export function setViewportWidth(width: number): void {
  const noop = () => {};
  vi.stubGlobal("innerWidth", width);
  vi.stubGlobal(
    "matchMedia",
    vi.fn<(query: string) => Partial<MediaQueryList>>((query) => {
      const min = /min-width:\s*([\d.]+)px/.exec(query)?.[1];
      const max = /max-width:\s*([\d.]+)px/.exec(query)?.[1];
      const sized = min !== undefined || max !== undefined;
      const matches =
        sized &&
        (min === undefined || width >= Number(min)) &&
        (max === undefined || width <= Number(max));
      return {
        matches,
        media: query,
        onchange: null,
        addEventListener: noop,
        removeEventListener: noop,
        dispatchEvent: () => false,
      };
    }),
  );
}
