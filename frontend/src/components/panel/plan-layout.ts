/** Dependency structure for the Plan tab. Pure; no React. */

export interface PlanLayoutInput {
  id: string;
  dependsOn: readonly string[];
}

/**
 * Longest-path depth of every node. Unknown dependency ids are ignored and a
 * dependency cycle is broken where it is detected, so malformed plans still
 * lay out instead of recursing forever.
 */
export function nodeDepths(
  nodes: readonly PlanLayoutInput[],
): Map<string, number> {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const depth = new Map<string, number>();
  const visiting = new Set<string>();

  const visit = (id: string): number => {
    const known = depth.get(id);
    if (known !== undefined) return known;
    if (visiting.has(id)) return -1; // back edge: ignore this dependency
    visiting.add(id);
    let d = 0;
    for (const dep of byId.get(id)?.dependsOn ?? []) {
      if (byId.has(dep)) d = Math.max(d, visit(dep) + 1);
    }
    visiting.delete(id);
    depth.set(id, d);
    return d;
  };

  for (const n of nodes) visit(n.id);
  return depth;
}

/**
 * Groups nodes into dependency waves, top to bottom: wave 0 has no (known)
 * dependencies, wave k only depends on earlier waves. Nodes in one wave can
 * run in parallel; within a wave they keep plan order.
 */
export function planWaves(nodes: readonly PlanLayoutInput[]): string[][] {
  const depths = nodeDepths(nodes);
  const waves: string[][] = [];
  for (const n of nodes) {
    const d = depths.get(n.id) ?? 0;
    const wave = waves[d] ?? [];
    wave.push(n.id);
    waves[d] = wave;
  }
  // Depths are contiguous (each is 1 + a smaller one), but stay defensive.
  return waves.filter((w) => w !== undefined);
}
