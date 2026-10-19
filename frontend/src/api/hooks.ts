import { useQuery } from "@tanstack/react-query";

import { useSession } from "../auth/session";
import { api } from "./client";
import type { Catalog } from "./types";

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
