import type { Metadata } from "next";
import Link from "next/link";
import { AppShell } from "@/components/shell/AppShell";

export const metadata: Metadata = {
  title: "Not found",
};

export default function NotFound() {
  return (
    <AppShell>
      <section
        aria-labelledby="not-found-title"
        className="flex flex-1 flex-col items-start justify-center gap-3 py-12"
      >
        <p className="font-medium font-mono text-muted text-sm">404</p>
        <h1
          id="not-found-title"
          className="font-semibold text-3xl tracking-tight"
        >
          Nothing here
        </h1>
        <p className="max-w-md text-muted">
          This page doesn&apos;t exist, or the run was removed.
        </p>
        <Link
          href="/runs"
          className="mt-2 inline-flex min-h-10 items-center rounded-full bg-raised px-4 font-medium text-sm transition-colors hover:bg-chip"
        >
          Start a new run
        </Link>
      </section>
    </AppShell>
  );
}
