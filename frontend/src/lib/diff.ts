export type DiffKind = "same" | "add" | "del";

export interface DiffLine {
  kind: DiffKind;
  text: string;
}

/**
 * Largest LCS table (lines of `a` x lines of `b`, after trimming the shared
 * head and tail) worth computing: 4M cells is ~16 MB of `Uint32Array` and a
 * few tens of milliseconds. Bigger inputs get a bounded fallback instead of
 * hanging the tab.
 */
export const LCS_MAX_CELLS = 4_000_000;

/** One line per `\n`; a single trailing newline does not add an empty line. */
function toLines(text: string): string[] {
  if (text === "") return [];
  const lines = text.split("\n");
  if (lines.at(-1) === "") lines.pop();
  return lines;
}

const lines = (kind: DiffKind, texts: readonly string[]): DiffLine[] =>
  texts.map((text) => ({ kind, text }));

/** Classic LCS backtrack; at a change point deletions come before additions. */
function lcs(a: readonly string[], b: readonly string[]): DiffLine[] {
  const n = a.length;
  const m = b.length;
  const width = m + 1;
  // table[i * width + j] = LCS length of a[i..] and b[j..].
  const table = new Uint32Array((n + 1) * width);
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      table[i * width + j] =
        a[i] === b[j]
          ? (table[(i + 1) * width + j + 1] ?? 0) + 1
          : Math.max(
              table[(i + 1) * width + j] ?? 0,
              table[i * width + j + 1] ?? 0,
            );
    }
  }
  const out: DiffLine[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    const ai = a[i] ?? "";
    if (ai === b[j]) {
      out.push({ kind: "same", text: ai });
      i++;
      j++;
    } else if (
      (table[(i + 1) * width + j] ?? 0) >= (table[i * width + j + 1] ?? 0)
    ) {
      out.push({ kind: "del", text: ai });
      i++;
    } else {
      out.push({ kind: "add", text: b[j] ?? "" });
      j++;
    }
  }
  out.push(...lines("del", a.slice(i)), ...lines("add", b.slice(j)));
  return out;
}

/**
 * Unified line diff of `a` -> `b` via the longest common subsequence of
 * lines. Pure. The shared head and tail are trimmed first (cheap, and the
 * common case for repair attempts); if what remains would need more than
 * {@link LCS_MAX_CELLS} table cells, the middle is reported as a block
 * deletion followed by a block addition. Either way, the `same` + `del`
 * lines spell out `a` and the `same` + `add` lines spell out `b`.
 */
export function lineDiff(a: string, b: string): DiffLine[] {
  const left = toLines(a);
  const right = toLines(b);

  let head = 0;
  while (
    head < left.length &&
    head < right.length &&
    left[head] === right[head]
  ) {
    head++;
  }
  let tail = 0;
  while (
    tail < left.length - head &&
    tail < right.length - head &&
    left[left.length - 1 - tail] === right[right.length - 1 - tail]
  ) {
    tail++;
  }

  const midA = left.slice(head, left.length - tail);
  const midB = right.slice(head, right.length - tail);
  const middle =
    midA.length * midB.length > LCS_MAX_CELLS
      ? [...lines("del", midA), ...lines("add", midB)]
      : lcs(midA, midB);

  return [
    ...lines("same", left.slice(0, head)),
    ...middle,
    ...lines("same", left.slice(left.length - tail)),
  ];
}
