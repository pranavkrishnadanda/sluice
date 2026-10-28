import { FileIcon } from "@/components/shell/icons";
import { formatCount } from "@/lib/format";

export interface FileChipProps {
  name: string;
  /** Row count; omitted before the file has been profiled. */
  rows?: number | null;
  className?: string;
}

/** "orders_2026-10.csv · 48,210 rows" with a file glyph. */
export function FileChip({ name, rows, className }: FileChipProps) {
  const label = rows == null ? name : `${name} · ${formatCount(rows)} rows`;
  return (
    <span
      className={[
        "inline-flex min-w-0 max-w-full items-center gap-1.5 rounded-[10px] bg-chip px-2 py-1 text-fg text-xs",
        className,
      ]
        .filter(Boolean)
        .join(" ")}
    >
      <FileIcon size={14} className="shrink-0 text-muted" />
      <span className="min-w-0 truncate">{label}</span>
    </span>
  );
}
