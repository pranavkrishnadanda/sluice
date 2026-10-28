// @vitest-environment node
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, test } from "vitest";
import { RUN_QUARANTINED_TRANSFORMS } from "@/lib/api/fixtures/run-quarantined";
import { RUN_REPAIRED_TRANSFORMS } from "@/lib/api/fixtures/run-repaired";
import { highlight, languageOf } from "@/lib/highlight";

const css = readFileSync(
  resolve(import.meta.dirname, "../../src/app/globals.css"),
  "utf8",
);
const solid = (selector: string) => {
  const start = css.indexOf(`${selector} {`);
  const hex = /--glass-solid:\s*(#[0-9a-f]{6})/i.exec(css.slice(start))?.[1];
  if (!hex) throw new Error(`no --glass-solid in ${selector}`);
  return hex;
};

const lum = (hex: string) => {
  const [r, g, b] = [1, 3, 5].map((i) => {
    const v = Number.parseInt(hex.slice(i, i + 2), 16) / 255;
    return v <= 0.04045 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4;
  }) as [number, number, number];
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
};
const contrast = (a: string, b: string) => {
  const [hi, lo] = [lum(a), lum(b)].sort((x, y) => y - x) as [number, number];
  return (hi + 0.05) / (lo + 0.05);
};

describe("highlight", () => {
  test("maps transform languages to grammars", () => {
    expect(languageOf("polars")).toBe("python");
    expect(languageOf("sql")).toBe("sql");
  });

  test("returns one token row per line with a light and a dark colour", async () => {
    const lines = await highlight("SELECT 1\nFROM t;", "sql");
    expect(lines).toHaveLength(2);
    const tokens = lines.flat();
    expect(tokens.map((t) => t.content).join("")).toBe("SELECT 1FROM t;");
    for (const t of tokens) {
      expect(t.light).toMatch(/^#[0-9a-f]{6}$/i);
      expect(t.dark).toMatch(/^#[0-9a-f]{6}$/i);
    }
  });

  test("every token colour in the fixtures is AA on the code surface", async () => {
    const light = solid(":root");
    const dark = solid('[data-theme="dark"]');
    const failures: string[] = [];
    for (const t of [
      ...RUN_REPAIRED_TRANSFORMS,
      ...RUN_QUARANTINED_TRANSFORMS,
    ]) {
      const lines = await highlight(t.code, languageOf(t.language));
      for (const token of lines.flat()) {
        if (!token.content.trim()) continue;
        if (contrast(token.light, light) < 4.5)
          failures.push(`light ${token.light}`);
        if (contrast(token.dark, dark) < 4.5)
          failures.push(`dark ${token.dark}`);
      }
    }
    expect([...new Set(failures)]).toEqual([]);
  });
});
