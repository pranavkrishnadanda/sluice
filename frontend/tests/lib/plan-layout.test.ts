import { describe, expect, test } from "vitest";
import { nodeDepths, planWaves } from "@/components/panel/plan-layout";

const n = (id: string, dependsOn: string[] = []) => ({ id, dependsOn });

describe("plan depths", () => {
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
});

describe("planWaves", () => {
  test("a diamond is three waves: root, the parallel pair, the join", () => {
    const waves = planWaves([
      n("a"),
      n("b", ["a"]),
      n("c", ["a"]),
      n("d", ["b", "c"]),
    ]);
    expect(waves).toEqual([["a"], ["b", "c"], ["d"]]);
  });

  test("a chain is one step per wave", () => {
    expect(planWaves([n("a"), n("b", ["a"]), n("c", ["b"])])).toEqual([
      ["a"],
      ["b"],
      ["c"],
    ]);
  });

  test("independent nodes all run in the first wave, in plan order", () => {
    expect(planWaves([n("z"), n("y"), n("x")])).toEqual([["z", "y", "x"]]);
  });

  test("a node waits for its deepest dependency; unknown deps are ignored", () => {
    expect(
      planWaves([n("a"), n("b", ["a"]), n("c", ["a", "b"]), n("d", ["nope"])]),
    ).toEqual([["a", "d"], ["b"], ["c"]]);
  });

  test("no nodes, no waves", () => {
    expect(planWaves([])).toEqual([]);
  });
});
