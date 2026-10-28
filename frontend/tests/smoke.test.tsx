import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import Home from "@/app/page";

describe("home page", () => {
  it("renders the Sluice heading", () => {
    render(<Home />);
    expect(
      screen.getByRole("heading", { level: 1, name: "Sluice" }),
    ).toBeInTheDocument();
  });
});
