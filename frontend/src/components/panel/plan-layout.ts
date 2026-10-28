/** Geometry for the Plan tab's DAG. Pure; no React. */

export interface PlanLayoutInput {
  id: string;
  dependsOn: readonly string[];
}

export interface PlacedNode {
  id: string;
  /** Dependency depth: 0 for roots, else 1 + the deepest dependency. */
  layer: number;
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface PlacedEdge {
  from: string;
  to: string;
  /** SVG path data: a vertical cubic from parent bottom to child top. */
  d: string;
}

export interface PlanLayout {
  width: number;
  height: number;
  nodes: PlacedNode[];
  edges: PlacedEdge[];
}

/** The panel's content width (380 px aside minus 16 px padding each side). */
export const PLAN_VIEW_WIDTH = 348;
/** Node height for a one-line title; callers pass a taller one for two. */
export const PLAN_NODE_HEIGHT = 62;
const PAD = 2;
const H_GAP = 12;
const V_GAP = 36;
const MAX_NODE_WIDTH = PLAN_VIEW_WIDTH - PAD * 2;
/** Below this a node title is unreadable; the view widens (and scrolls). */
const MIN_NODE_WIDTH = 120;

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
 * Places nodes in horizontal bands by depth, top to bottom. Within a band,
 * nodes are ordered by the mean position of their parents (one barycentre
 * pass) to keep edges short, and share the band's width evenly.
 */
export function layoutPlan(
  nodes: readonly PlanLayoutInput[],
  nodeHeight: number = PLAN_NODE_HEIGHT,
): PlanLayout {
  const depths = nodeDepths(nodes);
  const layers: PlanLayoutInput[][] = [];
  for (const n of nodes) {
    const d = depths.get(n.id) ?? 0;
    const layer = layers[d] ?? [];
    layer.push(n);
    layers[d] = layer;
  }

  const widest = Math.max(1, ...layers.map((l) => l?.length ?? 0));
  const width = Math.max(
    PLAN_VIEW_WIDTH,
    PAD * 2 + widest * MIN_NODE_WIDTH + (widest - 1) * H_GAP,
  );

  const placed = new Map<string, PlacedNode>();
  layers.forEach((layer = [], d) => {
    const centre = (n: PlanLayoutInput) => {
      const xs = n.dependsOn
        .map((p) => placed.get(p))
        .filter((p): p is PlacedNode => p !== undefined)
        .map((p) => p.x + p.width / 2);
      return xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : 0;
    };
    const ordered =
      d === 0
        ? layer
        : layer
            .map((n, i) => ({ n, i, c: centre(n) }))
            .sort((a, b) => a.c - b.c || a.i - b.i)
            .map((e) => e.n);
    const k = ordered.length;
    const nodeWidth = Math.min(
      MAX_NODE_WIDTH,
      (width - PAD * 2 - (k - 1) * H_GAP) / k,
    );
    const rowWidth = k * nodeWidth + (k - 1) * H_GAP;
    const left = (width - rowWidth) / 2;
    ordered.forEach((n, i) => {
      placed.set(n.id, {
        id: n.id,
        layer: d,
        x: left + i * (nodeWidth + H_GAP),
        y: PAD + d * (nodeHeight + V_GAP),
        width: nodeWidth,
        height: nodeHeight,
      });
    });
  });

  const edges: PlacedEdge[] = [];
  for (const n of nodes) {
    const to = placed.get(n.id);
    if (!to) continue;
    for (const dep of n.dependsOn) {
      const from = placed.get(dep);
      if (!from || from.layer >= to.layer) continue;
      const x1 = from.x + from.width / 2;
      const y1 = from.y + from.height;
      const x2 = to.x + to.width / 2;
      const y2 = to.y;
      const bend = (y2 - y1) / 2;
      edges.push({
        from: dep,
        to: n.id,
        d: `M ${x1} ${y1} C ${x1} ${y1 + bend} ${x2} ${y2 - bend} ${x2} ${y2}`,
      });
    }
  }

  const layerCount = layers.length;
  return {
    width,
    height:
      layerCount === 0
        ? 0
        : PAD * 2 + layerCount * nodeHeight + (layerCount - 1) * V_GAP,
    nodes: nodes
      .map((n) => placed.get(n.id))
      .filter((p): p is PlacedNode => p !== undefined),
    edges,
  };
}
