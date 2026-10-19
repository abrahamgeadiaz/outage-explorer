import { render } from "@testing-library/react";
import { createMemoryRouter, RouterProvider } from "react-router";
import { vi } from "vitest";

import { makeQueryClient, Providers, routes } from "../App";
import type { User } from "../api/types";

export const USERS: Record<string, User> = {
  analyst: {
    username: "analyst",
    display_name: "Ana Analyst",
    role: "analyst",
    capabilities: ["catalog", "preview", "query"],
  },
  viewer: { username: "viewer", display_name: "Vic Viewer", role: "viewer", capabilities: ["catalog", "preview", "query"] },
  admin: {
    username: "admin",
    display_name: "Ada Admin",
    role: "admin",
    capabilities: ["catalog", "preview", "query", "refresh"],
  },
};

type Reply = [status: number, body: unknown];
type Handler = (request: { url: URL; method: string; body: unknown; headers: Record<string, string> }) => Reply;

/** Stub fetch with handlers keyed "METHOD /path" (query string excluded). Unknown routes are 404s. */
export function mockApi(handlers: Record<string, Handler | Reply>) {
  const calls: { method: string; url: URL; body: unknown; headers: Record<string, string> }[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input), "http://localhost");
    const method = init?.method ?? "GET";
    const headers = (init?.headers ?? {}) as Record<string, string>;
    const body = init?.body ? JSON.parse(String(init.body)) : undefined;
    calls.push({ method, url, body, headers });
    const handler = handlers[`${method} ${url.pathname}`];
    const [status, payload] =
      handler === undefined
        ? [404, { error: { code: "not_found", message: "Not Found" } }]
        : typeof handler === "function"
          ? handler({ url, method, body, headers })
          : handler;
    return new Response(JSON.stringify(payload), { status, headers: { "Content-Type": "application/json" } });
  });
  vi.stubGlobal("fetch", fetchMock);
  return { calls, fetchMock };
}

/** Handlers for a signed-in user: a stored token and /me answering with that user. */
export function signedInAs(username: keyof typeof USERS) {
  localStorage.setItem("outage-explorer.token", `token-for-${username}`);
  return { "GET /api/auth/me": [200, USERS[username]] as Reply };
}

export function renderAt(path: string) {
  const router = createMemoryRouter(routes, { initialEntries: [path] });
  const client = makeQueryClient();
  const utils = render(
    <Providers client={client}>
      <RouterProvider router={router} />
    </Providers>,
  );
  return { ...utils, router, client };
}

export const CATALOG = {
  build: { build_id: "20261019T000000Z-abc123", built_at: "2026-10-19T00:00:00+00:00" },
  datasets: [
    {
      name: "fact_national_daily",
      kind: "fact",
      description: "U.S. fleet totals per day.",
      grain: "one row per day",
      primary_key: ["period"],
      columns: [
        { name: "period", type: "DATE", description: "Day." },
        { name: "capacity_mw", type: "DECIMAL(12,3)", description: "Capacity." },
        { name: "outage_mw", type: "DECIMAL(12,3)", description: "Outage." },
      ],
      rows: 7229,
      filters: { date: ["period"], facility: false },
    },
    {
      name: "metric_fleet_offline_daily",
      kind: "metric",
      description: "US-08: share of total fleet capacity offline per day.",
      grain: "one row per day",
      primary_key: ["period"],
      columns: [
        { name: "period", type: "DATE", description: "Day." },
        { name: "offline_pct", type: "DOUBLE", description: "The US-08 metric." },
      ],
      rows: 7229,
      filters: { date: ["period"], facility: false },
    },
  ],
};
