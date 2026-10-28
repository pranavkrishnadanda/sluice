import { render, screen } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import { ApiProvider, useApi } from "@/components/api/ApiProvider";
import { MockApiClient } from "@/lib/api/mock-client";

function DemoFlag() {
  const api = useApi();
  return <p>{api.isDemo ? "demo" : "live"}</p>;
}

describe("ApiProvider", () => {
  test("useApi returns the provided client", () => {
    render(
      <ApiProvider client={new MockApiClient()}>
        <DemoFlag />
      </ApiProvider>,
    );
    expect(screen.getByText("demo")).toBeInTheDocument();
  });

  test("defaults to getApiClient()", () => {
    render(
      <ApiProvider>
        <DemoFlag />
      </ApiProvider>,
    );
    expect(screen.getByText("demo")).toBeInTheDocument();
  });

  test("useApi outside the provider throws", () => {
    expect(() => render(<DemoFlag />)).toThrow(/ApiProvider/);
  });
});
