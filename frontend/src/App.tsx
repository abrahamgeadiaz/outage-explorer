import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { createBrowserRouter, Navigate, RouterProvider, type RouteObject } from "react-router";

import { ApiError } from "./api/client";
import { RequireAuth } from "./auth/RequireAuth";
import { SessionProvider } from "./auth/session";
import { Layout } from "./components/Layout";
import { Empty } from "./components/States";
import { DatasetPage } from "./pages/DatasetPage";
import { DatasetsPage } from "./pages/DatasetsPage";
import { LoginPage } from "./pages/LoginPage";
import { RefreshPage } from "./pages/RefreshPage";
import { SqlPage } from "./pages/SqlPage";

export const routes: RouteObject[] = [
  { path: "/login", element: <LoginPage /> },
  {
    element: (
      <RequireAuth>
        <Layout />
      </RequireAuth>
    ),
    children: [
      { index: true, element: <Navigate to="/datasets" replace /> },
      { path: "/datasets", element: <DatasetsPage /> },
      { path: "/datasets/:name", element: <DatasetPage /> },
      {
        path: "/sql",
        element: (
          <RequireAuth capability="query">
            <SqlPage />
          </RequireAuth>
        ),
      },
      // Loaded on demand: the chart library is most of the bundle and only this page needs it.
      { path: "/fleet", lazy: async () => ({ Component: (await import("./pages/FleetPage")).FleetPage }) },
      {
        path: "/refresh",
        element: (
          <RequireAuth capability="refresh">
            <RefreshPage />
          </RequireAuth>
        ),
      },
      { path: "*", element: <Empty>Page not found.</Empty> },
    ],
  },
];

function isTransient(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false;
  return error.status === 0 || (error.status >= 500 && error.code !== "model_not_built");
}

export function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        // Retry only what can fix itself: network errors and transient 5xx. A 4xx stays a 4xx (403 is
        // still 403), and 503 model_not_built lasts until an Admin refreshes.
        retry: (failures, error) => failures < 2 && isTransient(error),
        refetchOnWindowFocus: false,
      },
    },
  });
}

export function Providers({ client, children }: { client: QueryClient; children: ReactNode }) {
  return (
    <QueryClientProvider client={client}>
      <SessionProvider>{children}</SessionProvider>
    </QueryClientProvider>
  );
}

export function App() {
  const [client] = useState(makeQueryClient);
  const [router] = useState(() => createBrowserRouter(routes));
  return (
    <Providers client={client}>
      <RouterProvider router={router} />
    </Providers>
  );
}
