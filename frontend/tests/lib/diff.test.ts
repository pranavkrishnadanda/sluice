import { describe, expect, test } from "vitest";
import { type DiffLine, LCS_MAX_CELLS, lineDiff } from "@/lib/diff";

const side = (lines: DiffLine[], keep: "add" | "del") =>
  lines
    .filter((l) => l.kind === "same" || l.kind === keep)
    .map((l) => l.text)
    .join("\n");

describe("lineDiff", () => {
  test("lineDiff basic", () => {
    expect(lineDiff("a\nb", "a\nc")).toEqual([
      { kind: "same", text: "a" },
      { kind: "del", text: "b" },
      { kind: "add", text: "c" },
    ]);
  });

  test("identical inputs are all `same`", () => {
    expect(lineDiff("x\ny", "x\ny")).toEqual([
      { kind: "same", text: "x" },
      { kind: "same", text: "y" },
    ]);
  });

  test("empty sides", () => {
    expect(lineDiff("", "")).toEqual([]);
    expect(lineDiff("", "a\nb")).toEqual([
      { kind: "add", text: "a" },
      { kind: "add", text: "b" },
    ]);
    expect(lineDiff("a", "")).toEqual([{ kind: "del", text: "a" }]);
  });

  test("a single trailing newline is not a line", () => {
    expect(lineDiff("a\n", "a\nb\n")).toEqual([
      { kind: "same", text: "a" },
      { kind: "add", text: "b" },
    ]);
  });

  test("keeps the longest common subsequence", () => {
    const out = lineDiff("a\nb\nc\nd\ne", "a\nc\nx\ne\nf");
    expect(out.filter((l) => l.kind === "same").map((l) => l.text)).toEqual([
      "a",
      "c",
      "e",
    ]);
    expect(side(out, "del")).toBe("a\nb\nc\nd\ne");
    expect(side(out, "add")).toBe("a\nc\nx\ne\nf");
  });

  test("huge inputs fall back to a bounded diff that still reconstructs both sides", () => {
    const n = Math.ceil(Math.sqrt(LCS_MAX_CELLS)) + 500;
    const a = Array.from({ length: n }, (_, i) => `a${i}`);
    const b = Array.from({ length: n }, (_, i) => `b${i}`);
    // Shared head and tail survive the fallback as `same`.
    const before = ["head", ...a, "tail"].join("\n");
    const after = ["head", ...b, "tail"].join("\n");
    const started = performance.now();
    const out = lineDiff(before, after);
    expect(performance.now() - started).toBeLessThan(1_000);
    expect(out[0]).toEqual({ kind: "same", text: "head" });
    expect(out.at(-1)).toEqual({ kind: "same", text: "tail" });
    expect(side(out, "del")).toBe(before);
    expect(side(out, "add")).toBe(after);
  });
});
