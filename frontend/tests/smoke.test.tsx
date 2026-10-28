import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import Home from "@/app/page";
import RunsPage from "@/app/runs/page";
import { ApiProvider } from "@/components/api/ApiProvider";
import { MockApiClient } from "@/lib/api/mock-client";

const { redirect } = vi.hoisted(() => ({
  redirect: vi.fn<(url: string) => never>(),
}));
vi.mock("next/navigation", () => ({
  redirect,
  usePathname: () => "/runs",
  useRouter: () => ({ push: vi.fn<(href: string) => void>() }),
}));

describe("routes", () => {
  it("/ redirects to /runs", () => {
    Home();
    expect(redirect).toHaveBeenCalledWith("/runs");
  });

  it("/runs renders the Sluice shell and a page heading", () => {
    render(
      <ApiProvider client={new MockApiClient({ delayMs: 0 })}>
        <RunsPage />
      </ApiProvider>,
    );
    expect(screen.getByRole("link", { name: "Sluice" })).toBeInTheDocument();
    expect(
      screen.getByRole("heading", { level: 1, name: "New pipeline run" }),
    ).toBeInTheDocument();
  });
});
