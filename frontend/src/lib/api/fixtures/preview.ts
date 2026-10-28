import {
  type DataPreview,
  PREVIEW_MAX_LIMIT,
  type PreviewCell,
  type PreviewPage,
  type PreviewRow,
} from "../types";

/**
 * mulberry32 seeded per row, so row `i` depends only on `(seed, i)`: output
 * is deterministic and any prefix of a larger preview equals a smaller one.
 */
function rowRandom(seed: number, index: number): () => number {
  let a = (seed ^ Math.imul(index + 1, 0x9e3779b1)) >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4_294_967_296;
  };
}

const same = (value: string): PreviewCell => ({
  before: value,
  after: value,
  flagged: false,
});

const pad = (n: number, width: number) => String(n).padStart(width, "0");

function isoDate(dayOffset: number): string {
  // 2026-10-01 plus `dayOffset` days, UTC.
  return new Date(Date.UTC(2026, 9, 1 + dayOffset)).toISOString().slice(0, 10);
}

function dmy(iso: string): string {
  const [y, m, d] = iso.split("-");
  return `${d}/${m}/${y}`;
}

/** "124500.50" -> "1,24,500.50" (en-IN digit grouping). */
function indianGrouping(amount: string): string {
  const [intPart = "", frac] = amount.split(".");
  const last3 = intPart.slice(-3);
  const rest = intPart.slice(0, -3).replace(/\B(?=(\d{2})+(?!\d))/g, ",");
  const grouped = rest ? `${rest},${last3}` : last3;
  return frac === undefined ? grouped : `${grouped}.${frac}`;
}

interface PreviewSpec {
  seed: number;
  columns: string[];
  row(index: number, rand: () => number): PreviewRow;
}

const SPECS: Record<string, PreviewSpec> = {
  "run-repaired": {
    seed: 0x5eed_0a1d,
    columns: ["o_custkey", "o_orderdate", "o_totalprice"],
    row(i, rand) {
      const orderKey = 1 + i * 4;
      const custKey = 1 + Math.floor(rand() * 1_500);
      const date = isoDate(Math.floor(rand() * 31));
      const rupees = (850 + Math.floor(rand() * 5_000_000) / 10).toFixed(2);
      const shape = rand();
      let price: PreviewCell;
      if (shape < 0.003) {
        // Sentinel the rules refuse to guess: nulled and flagged.
        price = { before: "N/A", after: null, flagged: true };
      } else if (shape < 0.02) {
        price = {
          before: rupees.replace(".", ""),
          after: rupees,
          flagged: false,
        };
      } else if (shape < 0.6) {
        price = {
          before: indianGrouping(rupees),
          after: rupees,
          flagged: false,
        };
      } else {
        price = same(rupees);
      }
      return {
        pk: String(orderKey),
        quarantined: false,
        reason: null,
        cells: {
          o_custkey: same(String(custKey)),
          o_orderdate: same(date),
          o_totalprice: price,
        },
      };
    },
  },
  "run-clean": {
    seed: 0x5eed_c1ea,
    columns: ["o_clerk", "o_orderdate"],
    row(i, rand) {
      const clerk = `Clerk#${pad(1 + Math.floor(rand() * 1_000), 9)}`;
      const padded = rand() < 0.08;
      const date = isoDate(Math.floor(rand() * 30) - 30);
      return {
        pk: String(1 + i * 4),
        quarantined: false,
        reason: null,
        cells: {
          o_clerk: padded
            ? { before: `  ${clerk} `, after: clerk, flagged: false }
            : same(clerk),
          o_orderdate: { before: dmy(date), after: date, flagged: false },
        },
      };
    },
  },
  "run-quarantined": {
    seed: 0x5eed_9a7e,
    columns: ["l_orderkey", "l_discount", "l_shipmode"],
    row(i, rand) {
      const orderKey = 1 + Math.floor(i / 4) * 4;
      const lineNumber = 1 + (i % 4);
      const orphan = rand() < 212 / 60_175;
      const pct = Math.floor(rand() * 11);
      const modes = ["AIR", "MAIL", "RAIL", "SHIP", "TRUCK"] as const;
      const mode = modes[Math.floor(rand() * modes.length)] ?? "AIR";
      const lower = rand() < 0.1;
      return {
        // Composite key: l_orderkey|l_linenumber.
        pk: `${orderKey}|${lineNumber}`,
        quarantined: orphan,
        reason: orphan ? `l_orderkey ${orderKey} is not in orders` : null,
        cells: {
          l_orderkey: orphan
            ? { before: String(orderKey), after: null, flagged: true }
            : same(String(orderKey)),
          l_discount: {
            before: String(pct),
            after: (pct / 100).toFixed(2),
            flagged: false,
          },
          l_shipmode: lower
            ? { before: mode.toLowerCase(), after: mode, flagged: false }
            : same(mode),
        },
      };
    },
  },
};

export function hasPreview(runId: string): boolean {
  return runId in SPECS;
}

/**
 * Generates one page of the preview on demand; nothing is precomputed at
 * import time. Like the server, `limit` is capped at
 * {@link PREVIEW_MAX_LIMIT} and `nextOffset` is `null` after the last page.
 */
export function generatePreview(
  runId: string,
  totalRows: number,
  { offset, limit }: PreviewPage,
): DataPreview {
  const spec = SPECS[runId];
  if (!spec) throw new Error(`No preview fixture for ${runId}`);
  const start = Math.max(0, Math.min(Math.floor(offset), totalRows));
  const count = Math.max(
    0,
    Math.min(Math.floor(limit), PREVIEW_MAX_LIMIT, totalRows - start),
  );
  const rows = Array.from({ length: count }, (_, i) =>
    spec.row(start + i, rowRandom(spec.seed, start + i)),
  );
  const end = start + count;
  return {
    columns: [...spec.columns],
    rows,
    totalRows,
    nextOffset: end < totalRows ? end : null,
  };
}
