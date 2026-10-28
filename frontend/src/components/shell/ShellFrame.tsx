"use client";

import { usePathname } from "next/navigation";
import { type ReactNode, useEffect, useRef, useState } from "react";
import { ThemeToggle } from "@/components/theme/ThemeToggle";
import { MenuIcon, PanelIcon } from "./icons";
import { useMediaQuery } from "./use-media-query";

/* Keep in step with Tailwind's md (48rem) and lg (64rem) breakpoints. */
const NAV_DRAWER_QUERY = "(max-width: 767.98px)";
const PANEL_DRAWER_QUERY = "(max-width: 1023.98px)";

type Drawer = "nav" | "details";

export interface ShellFrameProps {
  brand: ReactNode;
  /** Header items before the theme toggle (e.g. the demo badge). */
  actions?: ReactNode;
  sidebar: ReactNode;
  sidePanel?: ReactNode;
  children: ReactNode;
}

const PILL_BUTTON =
  "glass glass-pill inline-flex h-11 min-w-11 items-center justify-center gap-2 px-3 font-medium text-fg text-sm transition-colors duration-150 hover:bg-raised";

/**
 * The interactive part of the app shell: drawer state for the sidebar
 * (< 768 px) and the side panel (< 1024 px). Everything it renders is passed
 * in, so the slots can stay Server Components.
 *
 * Above the breakpoints both are static columns. Below them they are
 * overlays: a toggle with `aria-expanded`, focus moved into the drawer on
 * open, Escape or the scrim to close (focus returns to the toggle), the main
 * column `inert` while covered, and the drawer closed on navigation.
 */
export function ShellFrame({
  brand,
  actions,
  sidebar,
  sidePanel,
  children,
}: ShellFrameProps) {
  const pathname = usePathname();
  const navIsDrawer = useMediaQuery(NAV_DRAWER_QUERY);
  const panelIsDrawer = useMediaQuery(PANEL_DRAWER_QUERY);

  // Remembering the path a drawer was opened on closes it on navigation
  // without an effect.
  const [open, setOpen] = useState<{ drawer: Drawer; path: string } | null>(
    null,
  );
  const current = open?.path === pathname ? open.drawer : null;
  const navOpen = navIsDrawer && current === "nav";
  const detailsOpen =
    sidePanel != null && panelIsDrawer && current === "details";
  const overlay = navOpen || detailsOpen;

  const menuButton = useRef<HTMLButtonElement>(null);
  const detailsButton = useRef<HTMLButtonElement>(null);
  const sidebarBox = useRef<HTMLDivElement>(null);
  const panelBox = useRef<HTMLElement>(null);

  const toggle = (drawer: Drawer) =>
    setOpen((prev) =>
      prev?.drawer === drawer && prev.path === pathname
        ? null
        : { drawer, path: pathname },
    );

  // Opening a drawer moves focus into it; Escape closes it and returns focus.
  useEffect(() => {
    if (!overlay) return;
    const box = navOpen ? sidebarBox.current : panelBox.current;
    const trigger = navOpen ? menuButton.current : detailsButton.current;
    box
      ?.querySelector<HTMLElement>(
        'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])',
      )
      ?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== "Escape") return;
      setOpen(null);
      trigger?.focus();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [overlay, navOpen]);

  return (
    <div className="flex h-dvh flex-col gap-3 p-3 sm:p-4">
      <a
        href="#main"
        className="glass glass-pill sr-only px-4 py-2 font-medium text-sm focus:not-sr-only focus:fixed focus:top-3 focus:left-3 focus:z-50"
      >
        Skip to content
      </a>

      {/* Above the scrim so the drawer toggles stay reachable. */}
      <header className="relative z-50 flex min-w-0 shrink-0 items-center gap-2">
        <button
          ref={menuButton}
          type="button"
          aria-label="Menu"
          aria-expanded={navOpen}
          aria-controls="app-sidebar"
          onClick={() => toggle("nav")}
          className={`${PILL_BUTTON} px-0 md:hidden`}
        >
          <MenuIcon />
        </button>
        {brand}
        <div className="ml-auto flex min-w-0 items-center gap-2">
          {actions}
          {sidePanel != null && (
            <button
              ref={detailsButton}
              type="button"
              aria-expanded={detailsOpen}
              aria-controls="app-details"
              onClick={() => toggle("details")}
              className={`${PILL_BUTTON} lg:hidden`}
            >
              <PanelIcon size={16} />
              <span className="max-sm:sr-only">Details</span>
            </button>
          )}
          <ThemeToggle />
        </div>
      </header>

      <div className="flex min-h-0 flex-1 gap-3">
        {overlay && (
          // Pointer convenience only; keyboard users close with Escape or
          // the toggle, so this layer is hidden from assistive tech.
          <div
            aria-hidden="true"
            onClick={() => setOpen(null)}
            className="fixed inset-0 z-30 bg-black/25"
          />
        )}

        <div
          ref={sidebarBox}
          id="app-sidebar"
          hidden={navIsDrawer && !navOpen}
          className={[
            "glass w-60 shrink-0 flex-col overflow-hidden",
            navOpen
              ? "max-md:fixed max-md:top-17 max-md:bottom-3 max-md:left-3 sm:max-md:top-18 sm:max-md:bottom-4 sm:max-md:left-4 max-md:z-40 max-md:flex max-md:w-[min(17rem,calc(100vw-1.5rem))] max-md:bg-glass-solid/90"
              : "max-md:hidden",
            "md:flex",
          ].join(" ")}
        >
          {sidebar}
        </div>

        <main
          id="main"
          tabIndex={-1}
          inert={overlay}
          className="glass flex min-w-0 flex-1 flex-col overflow-hidden focus-visible:outline-none"
        >
          <div className="min-h-0 flex-1 overflow-y-auto overscroll-contain">
            <div className="mx-auto flex min-h-full w-full max-w-[760px] flex-col px-4 py-6 sm:px-8 sm:py-10">
              {children}
            </div>
          </div>
        </main>

        {sidePanel != null && (
          <aside
            ref={panelBox}
            id="app-details"
            aria-label="Details"
            hidden={panelIsDrawer && !detailsOpen}
            className={[
              "glass shrink-0 flex-col overflow-hidden lg:flex lg:w-[380px]",
              detailsOpen
                ? "max-lg:fixed max-lg:top-17 max-lg:bottom-3 max-lg:right-3 sm:max-lg:top-18 sm:max-lg:bottom-4 sm:max-lg:right-4 max-lg:z-40 max-lg:flex max-lg:w-[min(380px,calc(100vw-1.5rem))] max-lg:bg-glass-solid/90"
                : "max-lg:hidden",
            ].join(" ")}
          >
            <div className="min-h-0 flex-1 overflow-y-auto p-4">
              {sidePanel}
            </div>
          </aside>
        )}
      </div>
    </div>
  );
}
