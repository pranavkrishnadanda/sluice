import type { HighlighterCore } from "shiki/core";
import type { Transform } from "@/lib/api/types";

export type HighlightLang = "python" | "sql";

export interface HighlightToken {
  content: string;
  /** Hex colour under the light theme. */
  light: string;
  /** Hex colour under the dark theme. */
  dark: string;
}

/** Polars transforms are Python; SQL transforms are SQL. */
export function languageOf(language: Transform["language"]): HighlightLang {
  return language === "sql" ? "sql" : "python";
}

/*
 * High-contrast GitHub themes: every token colour they give the fixtures'
 * Python and SQL clears 4.5:1 on the --glass-solid code well (the regular
 * GitHub themes drop to ~3.2:1). tests/lib/highlight.test.ts checks this.
 */
const LIGHT_THEME = "github-light-high-contrast";
const DARK_THEME = "github-dark-high-contrast";

let highlighter: Promise<HighlighterCore> | null = null;

/**
 * Lazily builds one fine-grained Shiki highlighter: core + the JavaScript
 * regex engine (no WASM), two grammars and two themes. Everything is behind
 * dynamic `import()`, so none of it is in the first-load bundle.
 */
function load(): Promise<HighlighterCore> {
  highlighter ??= (async () => {
    const [{ createHighlighterCore }, { createJavaScriptRegexEngine }] =
      await Promise.all([
        import("shiki/core"),
        import("shiki/engine/javascript"),
      ]);
    return createHighlighterCore({
      engine: createJavaScriptRegexEngine(),
      langs: [import("shiki/langs/python.mjs"), import("shiki/langs/sql.mjs")],
      themes: [
        import("shiki/themes/github-light-high-contrast.mjs"),
        import("shiki/themes/github-dark-high-contrast.mjs"),
      ],
    });
  })().catch((err: unknown) => {
    // Let a later call retry (e.g. a chunk failed to load offline).
    highlighter = null;
    throw err;
  });
  return highlighter;
}

/**
 * Tokenises `code` into lines of tokens, each carrying its light and dark
 * colour so the view can switch theme with CSS alone. Rejects if Shiki
 * cannot load; callers keep showing plain text.
 */
export async function highlight(
  code: string,
  lang: HighlightLang,
): Promise<HighlightToken[][]> {
  const shiki = await load();
  const lines = shiki.codeToTokensWithThemes(code.replace(/\n$/, ""), {
    lang,
    themes: { light: LIGHT_THEME, dark: DARK_THEME },
  });
  return lines.map((line) =>
    line.map((token) => ({
      content: token.content,
      light: token.variants.light?.color ?? "inherit",
      dark: token.variants.dark?.color ?? "inherit",
    })),
  );
}
