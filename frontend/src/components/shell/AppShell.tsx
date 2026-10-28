import Link from "next/link";
import type { ReactNode } from "react";
import { DemoBadge } from "./DemoBadge";
import { SluiceMark } from "./icons";
import { ShellFrame } from "./ShellFrame";
import { Sidebar } from "./Sidebar";

export interface AppShellProps {
  children: ReactNode;
  /** Optional right-hand panel (e.g. run details); a drawer below 1024 px. */
  sidePanel?: ReactNode;
}

/**
 * The app frame: header (brand, demo badge, theme toggle), glass sidebar
 * (~240 px), centred main column (max 760 px) and an optional glass side
 * panel (~380 px), all over the layout's colour-blob backdrop.
 *
 * Server Component; the drawer behaviour lives in the client `ShellFrame`,
 * which receives these server-rendered slots.
 */
export function AppShell({ children, sidePanel }: AppShellProps) {
  return (
    <ShellFrame
      brand={
        <Link
          href="/runs"
          className="flex min-h-11 shrink-0 items-center gap-2 rounded-full pr-2 pl-1 font-semibold text-fg text-lg tracking-tight"
        >
          <SluiceMark />
          Sluice
        </Link>
      }
      actions={<DemoBadge />}
      sidebar={<Sidebar />}
      sidePanel={sidePanel}
    >
      {children}
    </ShellFrame>
  );
}
