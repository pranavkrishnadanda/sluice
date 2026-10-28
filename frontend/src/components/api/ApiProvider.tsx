"use client";

import { createContext, type ReactNode, use, useState } from "react";
import { getApiClient } from "@/lib/api/client";
import type { ApiClient } from "@/lib/api/types";

const ApiContext = createContext<ApiClient | null>(null);

interface ApiProviderProps {
  children: ReactNode;
  /** Override the client (tests, stories). Defaults to {@link getApiClient}. */
  client?: ApiClient;
}

/**
 * Provides one {@link ApiClient} for the app's lifetime. The client is chosen
 * once on mount so the mock's created runs survive client-side navigation.
 */
export function ApiProvider({ children, client }: ApiProviderProps) {
  const [api] = useState<ApiClient>(() => client ?? getApiClient());
  return <ApiContext value={api}>{children}</ApiContext>;
}

/** The app's {@link ApiClient}. Must be rendered under {@link ApiProvider}. */
export function useApi(): ApiClient {
  const api = use(ApiContext);
  if (!api) throw new Error("useApi() must be used inside <ApiProvider>.");
  return api;
}
