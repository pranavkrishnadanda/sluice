import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ThemeProvider } from "@/components/theme/ThemeProvider";
import { ThemeToggle } from "@/components/theme/ThemeToggle";

const html = document.documentElement;

function mockMatchMedia(matches: (query: string) => boolean) {
  const noop = () => {};
  vi.stubGlobal(
    "matchMedia",
    vi.fn<(query: string) => Partial<MediaQueryList>>((query) => ({
      matches: matches(query),
      media: query,
      onchange: null,
      addEventListener: noop,
      removeEventListener: noop,
      dispatchEvent: () => false,
    })),
  );
}

function renderToggle() {
  return render(
    <ThemeProvider>
      <ThemeToggle />
    </ThemeProvider>,
  );
}

beforeEach(() => {
  localStorage.clear();
  delete html.dataset.theme;
  delete html.dataset.reduceTransparency;
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ThemeToggle", () => {
  it("cycles system -> light -> dark", async () => {
    const user = userEvent.setup();
    renderToggle();
    const button = screen.getByRole("button", { name: "Theme: system" });
    expect(html.dataset.theme).toBe("system");

    await user.click(button);
    expect(button).toHaveAccessibleName("Theme: light");
    expect(html.dataset.theme).toBe("light");

    await user.click(button);
    expect(button).toHaveAccessibleName("Theme: dark");
    expect(html.dataset.theme).toBe("dark");

    await user.click(button);
    expect(button).toHaveAccessibleName("Theme: system");
    expect(html.dataset.theme).toBe("system");
  });

  it("persists the theme to localStorage", async () => {
    const user = userEvent.setup();
    renderToggle();
    const button = screen.getByRole("button", { name: "Theme: system" });
    await user.click(button);
    await user.click(button);
    expect(localStorage.getItem("sluice-theme")).toBe("dark");
  });

  it("restores a stored theme on mount", () => {
    localStorage.setItem("sluice-theme", "dark");
    renderToggle();
    expect(
      screen.getByRole("button", { name: "Theme: dark" }),
    ).toBeInTheDocument();
    expect(html.dataset.theme).toBe("dark");
  });

  it("survives localStorage throwing", async () => {
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(() => {
      throw new Error("SecurityError");
    });
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new Error("QuotaExceededError");
    });
    const user = userEvent.setup();
    renderToggle();
    const button = screen.getByRole("button", { name: "Theme: system" });
    await user.click(button);
    expect(button).toHaveAccessibleName("Theme: light");
    expect(html.dataset.theme).toBe("light");
  });
});

describe("ThemeProvider", () => {
  it("sets data-reduce-transparency when the user prefers reduced transparency", () => {
    mockMatchMedia(
      (query) => query === "(prefers-reduced-transparency: reduce)",
    );
    renderToggle();
    expect(html).toHaveAttribute("data-reduce-transparency");
  });

  it("leaves data-reduce-transparency unset by default", () => {
    mockMatchMedia(() => false);
    renderToggle();
    expect(html).not.toHaveAttribute("data-reduce-transparency");
  });
});
