import { describe, expect, test, vi } from "vitest";
import {
  ApiError,
  errorDetail,
  isNotFound,
  parseErrorBody,
} from "@/lib/api/errors";
import { HttpApiClient } from "@/lib/api/http-client";

describe("parseErrorBody", () => {
  test("reads the contract shape", () => {
    expect(
      parseErrorBody('{"detail":"File too large","code":"too_large"}'),
    ).toEqual({ detail: "File too large", code: "too_large" });
  });

  test("a non-string detail (FastAPI 422 array) or plain text is no detail", () => {
    expect(parseErrorBody('{"detail":[{"loc":["body"]}]}')).toEqual({
      detail: null,
      code: null,
    });
    expect(parseErrorBody("Bad Gateway")).toEqual({ detail: null, code: null });
    expect(parseErrorBody("")).toEqual({ detail: null, code: null });
  });
});

describe("errorDetail", () => {
  const err = (status: number, detail: string | null) =>
    new ApiError(status, "u", `${status}`, { detail });

  test("4xx with a string detail shows it", () => {
    expect(errorDetail(err(413, "Too large"))).toBe("Too large");
    expect(errorDetail(err(422, "Unknown table"))).toBe("Unknown table");
  });

  test("5xx, missing detail and network errors have none", () => {
    expect(errorDetail(err(500, "Traceback"))).toBeNull();
    expect(errorDetail(err(400, null))).toBeNull();
    expect(errorDetail(new TypeError("Failed to fetch"))).toBeNull();
    expect(errorDetail("nope")).toBeNull();
  });

  test("isNotFound is a 404 ApiError only", () => {
    expect(isNotFound(err(404, null))).toBe(true);
    expect(isNotFound(err(410, null))).toBe(false);
    expect(isNotFound(new Error("Unknown run"))).toBe(false);
  });
});

describe("HttpApiClient errors", () => {
  test("carry status, detail and code from the JSON body", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn<typeof fetch>(
        async () =>
          new Response(
            JSON.stringify({ detail: "Unsupported format", code: "format" }),
            { status: 415 },
          ),
      ),
    );
    const error = await new HttpApiClient("http://api.test")
      .createRun(new File(["x"], "a.xlsx"), "orders")
      .catch((e: unknown) => e);
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      status: 415,
      detail: "Unsupported format",
      code: "format",
    });
    vi.unstubAllGlobals();
  });
});
