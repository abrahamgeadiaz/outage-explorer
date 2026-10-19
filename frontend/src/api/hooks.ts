import { keepPreviousData, useQuery } from "@tanstack/react-query";

import { useSession } from "../auth/session";
import { api } from "./client";
import type { Catalog, Dataset, RowsPage } from "./types";

// Query keys start with the username: the cache is cleared on login/logout anyway, and this makes it
// impossible for one user's data to be read back under another user's key.

export function useCatalog() {
  const { user } = useSession();
  return useQuery({
    queryKey: [user?.username, "catalog"],
    queryFn: ({ signal }) => api<Catalog>("/api/datasets", { signal }),
    staleTime: 60_000,
  });
}

export function useDataset(name: string) {
  const { user } = useSession();
  return useQuery({
    queryKey: [user?.username, "dataset", name],
    queryFn: ({ signal }) => api<Dataset>(`/api/datasets/${encodeURIComponent(name)}`, { signal }),
    staleTime: 60_000,
  });
}

export interface RowsParams {
  start?: string;
  end?: string;
  facility_id?: string[];
  limit: number;
  offset: number;
}

export function useRows(name: string, params: RowsParams, enabled = true) {
  const { user } = useSession();
  return useQuery({
    queryKey: [user?.username, "rows", name, params],
    queryFn: ({ signal }) =>
      api<RowsPage>(`/api/datasets/${encodeURIComponent(name)}/rows`, { params: { ...params }, signal }),
    placeholderData: keepPreviousData, // keep the current page on screen while the next one loads
    enabled,
  });
}

export interface Facility {
  id: string;
  name: string;
}

/** Plant names for the facility filter, when the role may read dim_facility (otherwise: none). */
export function useFacilities(enabled: boolean) {
  const { user } = useSession();
  return useQuery({
    queryKey: [user?.username, "facilities"],
    queryFn: async ({ signal }) => {
      const page = await api<RowsPage>("/api/datasets/dim_facility/rows", { params: { limit: 1000 }, signal });
      const id = page.columns.findIndex((c) => c.name === "facility_id");
      const name = page.columns.findIndex((c) => c.name === "facility_name");
      return page.rows
        .map((r) => ({ id: String(r[id]), name: String(r[name]) }))
        .sort((a, b) => a.name.localeCompare(b.name));
    },
    enabled,
    staleTime: 10 * 60_000,
  });
}
