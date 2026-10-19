import type { ReactNode } from "react";

import { ApiError } from "../api/client";

// Loading, empty, error and "not allowed" states, used the same way on every screen.

export function Loading({ label = "Loading…" }: { label?: string }) {
  return (
    <p className="state state-loading" role="status" aria-live="polite">
      <span className="spinner" aria-hidden="true" /> {label}
    </p>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <p className="state state-empty">{children}</p>;
}

export function NotAllowed({ children }: { children?: ReactNode }) {
  return (
    <div className="state state-forbidden" role="alert">
      <strong>Not allowed.</strong> {children ?? "Your role does not have access to this."}
    </div>
  );
}

export function ErrorBox({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  if (error instanceof ApiError && error.forbidden) return <NotAllowed>{error.message}</NotAllowed>;
  const message = error instanceof Error ? error.message : String(error);
  const code = error instanceof ApiError ? error.code : null;
  return (
    <div className="state state-error" role="alert">
      <strong>{code === "model_not_built" ? "No data yet." : "Something went wrong."}</strong> {message}
      {code && <code className="error-code">{code}</code>}
      {onRetry && (
        <button type="button" className="link-button" onClick={onRetry}>
          Retry
        </button>
      )}
    </div>
  );
}
