import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  // The Playwright build sets NEXT_DIST_DIR=.next-e2e so it never overwrites a
  // running `next dev`'s `.next` output.
  distDir: process.env.NEXT_DIST_DIR || ".next",
  reactStrictMode: true,
  poweredByHeader: false,
  // Do not auto-generate AGENTS.md when `next dev` detects a coding agent.
  agentRules: false,
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
};

export default nextConfig;
