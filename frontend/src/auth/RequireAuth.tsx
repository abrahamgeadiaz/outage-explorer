import type { ReactNode } from "react";
import { Navigate, useLocation } from "react-router";

import type { Capability } from "../api/types";
import { Loading, NotAllowed } from "../components/States";
import { useSession } from "./session";

// Protected route. Hiding a screen is only for the user's convenience: the API checks every request
// itself, so a user who forces their way to a screen still gets a 403 and sees "not allowed".
export function RequireAuth({ capability, children }: { capability?: Capability; children: ReactNode }) {
  const session = useSession();
  const location = useLocation();

  if (session.status === "checking") return <Loading label="Checking your session…" />;
  if (session.status === "anonymous") return <Navigate to="/login" replace state={{ from: location.pathname + location.search }} />;
  if (capability && !session.can(capability)) return <NotAllowed>Your role cannot use this page.</NotAllowed>;
  return <>{children}</>;
}
