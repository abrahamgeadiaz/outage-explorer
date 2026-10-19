import { useQueryClient } from "@tanstack/react-query";
import { createContext, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";

import { api, revokeToken, setUnauthorizedHandler, tokenStore } from "../api/client";
import type { Capability, TokenResponse, User } from "../api/types";

type Status = "checking" | "anonymous" | "authenticated";

interface Session {
  status: Status;
  user: User | null;
  notice: string | null; // why the user was signed out, shown on the login page
  login(username: string, password: string): Promise<void>;
  logout(notice?: string): void;
  can(capability: Capability): boolean;
}

const SessionContext = createContext<Session | null>(null);

export function SessionProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient();
  const [user, setUser] = useState<User | null>(null);
  const [status, setStatus] = useState<Status>(tokenStore.get() ? "checking" : "anonymous");
  const [notice, setNotice] = useState<string | null>(null);

  const logout = useCallback(
    (reason?: string) => {
      const token = tokenStore.get();
      if (token && reason === undefined) revokeToken(token); // signed out on purpose, not expired
      tokenStore.clear();
      queryClient.clear(); // nothing fetched for one user may be shown to the next
      setUser(null);
      setStatus("anonymous");
      setNotice(reason ?? null);
    },
    [queryClient],
  );

  // A stored token is only a hint: ask the API who it belongs to (and the role, which the server owns).
  useEffect(() => {
    if (status !== "checking") return;
    let cancelled = false;
    api<User>("/api/auth/me")
      .then((me) => {
        if (cancelled) return;
        setUser(me);
        setStatus("authenticated");
      })
      .catch(() => !cancelled && logout());
    return () => {
      cancelled = true;
    };
  }, [status, logout]);

  useEffect(() => {
    setUnauthorizedHandler(() => logout("Your session expired. Please sign in again."));
    return () => setUnauthorizedHandler(null);
  }, [logout]);

  const login = useCallback(
    async (username: string, password: string) => {
      const token = await api<TokenResponse>("/api/auth/login", { method: "POST", body: { username, password } });
      queryClient.clear();
      tokenStore.set(token.access_token);
      setUser(token.user);
      setNotice(null);
      setStatus("authenticated");
    },
    [queryClient],
  );

  const value = useMemo<Session>(
    () => ({
      status,
      user,
      notice,
      login,
      logout,
      can: (capability) => user?.capabilities.includes(capability) ?? false,
    }),
    [status, user, notice, login, logout],
  );
  return <SessionContext.Provider value={value}>{children}</SessionContext.Provider>;
}

export function useSession(): Session {
  const session = useContext(SessionContext);
  if (!session) throw new Error("useSession must be used inside <SessionProvider>");
  return session;
}
