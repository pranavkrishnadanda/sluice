import { Glass } from "@/components/glass/Glass";
import { ThemeToggle } from "@/components/theme/ThemeToggle";

export default function Home() {
  return (
    <main className="relative flex flex-1 flex-col items-center justify-center px-4 py-24">
      <ThemeToggle className="absolute top-4 right-4 sm:top-6 sm:right-6" />
      <Glass
        as="section"
        aria-labelledby="hero-title"
        className="w-full max-w-xl px-6 py-12 text-center sm:px-12 sm:py-16"
      >
        <h1
          id="hero-title"
          className="font-semibold text-5xl tracking-tight sm:text-6xl"
        >
          Sluice
        </h1>
        <p className="mx-auto mt-4 max-w-md text-balance text-base text-muted sm:text-lg">
          A self-planning agentic data pipeline for drifting data, measured like
          production.
        </p>
        <ul className="mt-8 flex flex-wrap justify-center gap-2 text-sm">
          <li className="rounded-full bg-chip px-3 py-1 text-ok">
            12/12 checks passed
          </li>
          <li className="rounded-full bg-chip px-3 py-1 text-warn">
            Repairing 2/3
          </li>
          <li className="rounded-full bg-chip px-3 py-1 text-accent">
            Loading rows
          </li>
        </ul>
      </Glass>
    </main>
  );
}
