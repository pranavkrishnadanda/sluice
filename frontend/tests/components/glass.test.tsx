import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Glass } from "@/components/glass/Glass";

describe("Glass", () => {
  it("renders children inside a .glass element", () => {
    render(<Glass>x</Glass>);
    expect(screen.getByText("x").closest(".glass")).not.toBeNull();
  });

  it("renders as the requested element and merges className", () => {
    render(
      <Glass as="section" className="p-4" aria-label="Panel">
        y
      </Glass>,
    );
    const panel = screen.getByRole("region", { name: "Panel" });
    expect(panel.tagName).toBe("SECTION");
    expect(panel).toHaveClass("glass", "p-4");
  });
});
