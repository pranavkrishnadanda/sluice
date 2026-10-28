import { describe, expect, test } from "vitest";
import { layoutPlan, nodeDepths } from "@/components/panel/plan-layout";

const n = (id: string, dependsOn: string[] = []) => ({ id, dependsOn });

describe("plan layout", () => {
  test("depth is the longest dependency chain", () => {
    const depths = nodeDepths([
      n("a"),
      n("b", ["a"]),
      n("c", ["a", "b"]),
      n("d"),
    ]);
    expect(Object.fromEntries(depths)).toEqual({ a: 0, b: 1, c: 2, d: 0 });
  });

  test("unknown dependencies and cycles do not hang or throw", () => {
    const depths = nodeDepths([n("a", ["zz"]), n("b", ["c"]), n("c", ["b"])]);
    expect(depths.get("a")).toBe(0);
    expect(Number.isFinite(depths.get("b"))).toBe(true);
    expect(Number.isFinite(depths.get("c"))).toBe(true);
  });

  test("layers stack top to bottom and edges join known nodes", () => {
    const layout = layoutPlan([
      n("a"),
      n("b", ["a"]),
      n("c", ["a"]),
      n("x", ["missing"]),
    ]);
    const byId = new Map(layout.nodes.map((p) => [p.id, p]));
    expect(byId.get("a")?.layer).toBe(0);
    expect(byId.get("b")?.layer).toBe(1);
    expect((byId.get("b")?.y ?? 0) > (byId.get("a")?.y ?? 0)).toBe(true);
    expect(layout.edges.map((e) => `${e.from}->${e.to}`).sort()).toEqual([
      "a->b",
      "a->c",
    ]);
    // Everything fits the view box.
    for (const p of layout.nodes) {
      expect(p.x).toBeGreaterThanOrEqual(0);
      expect(p.x + p.width).toBeLessThanOrEqual(layout.width);
    }
  });
});
