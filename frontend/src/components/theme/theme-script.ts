/**
 * Shared theme constants plus the pre-paint script inlined in `<head>` by the
 * root layout. Kept free of React so the server layout and the client store can
 * both import it.
 */

export const THEME_STORAGE_KEY = "sluice-theme";

export const THEME_MODES = ["system", "light", "dark"] as const;

export type ThemeMode = (typeof THEME_MODES)[number];

export const REDUCED_TRANSPARENCY_QUERY =
  "(prefers-reduced-transparency: reduce)";

export function isThemeMode(value: unknown): value is ThemeMode {
  return (
    typeof value === "string" &&
    (THEME_MODES as readonly string[]).includes(value)
  );
}

/**
 * Runs before first paint so the correct `data-theme` (and the reduced
 * transparency hook) is on `<html>` before any CSS is applied: no flash of the
 * wrong theme. Every storage / matchMedia access is guarded because either can
 * throw (privacy mode, blocked site data, old engines).
 */
export const themeInitScript = `(function(){var d=document.documentElement,m;try{m=localStorage.getItem(${JSON.stringify(
  THEME_STORAGE_KEY,
)})}catch(e){}if(m!=="light"&&m!=="dark")m="system";d.setAttribute("data-theme",m);try{if(window.matchMedia(${JSON.stringify(
  REDUCED_TRANSPARENCY_QUERY,
)}).matches)d.setAttribute("data-reduce-transparency","")}catch(e){}})();`;
