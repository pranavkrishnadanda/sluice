import { HttpApiClient } from "./http-client";
import { MockApiClient } from "./mock-client";
import type { ApiClient } from "./types";

export type { ApiClient } from "./types";

/**
 * Picks the API implementation from `NEXT_PUBLIC_SLUICE_API_URL`: unset,
 * blank or `"mock"` gives the fixture-backed {@link MockApiClient}; anything
 * else is the backend base URL for {@link HttpApiClient}.
 *
 * The variable is read with a literal `process.env.NEXT_PUBLIC_...` access so
 * Next.js can inline it into client bundles.
 */
export function getApiClient(): ApiClient {
  const base = process.env.NEXT_PUBLIC_SLUICE_API_URL?.trim();
  if (!base || base === "mock") return new MockApiClient();
  return new HttpApiClient(base);
}
