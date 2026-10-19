// The one place that talks HTTP. Every backend error has the shape {"error": {code, message}}; it
// becomes an ApiError, so screens branch on `code` / `status`, never on message text.

const TOKEN_KEY = "outage-explorer.token";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly details?: unknown;

  constructor(status: number, code: string, message: string, details?: unknown) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
    this.details = details;
  }

  get forbidden(): boolean {
    return this.status === 403;
  }
}

// The access token lives in localStorage so a reload or a new tab keeps the session (it expires on the
// server after 60 minutes). Storage can be unavailable (private mode, blocked site data): then the
// session just lasts as long as the page.
let memoryToken: string | null = null;

export const tokenStore = {
  get(): string | null {
    try {
      return localStorage.getItem(TOKEN_KEY) ?? memoryToken;
    } catch {
      return memoryToken;
    }
  },
  set(token: string): void {
    memoryToken = token;
    try {
      localStorage.setItem(TOKEN_KEY, token);
    } catch {
      /* memory only */
    }
  },
  clear(): void {
    memoryToken = null;
    try {
      localStorage.removeItem(TOKEN_KEY);
    } catch {
      /* nothing stored */
    }
  },
};

// Set by the session provider: a 401 on any call means the token expired or was revoked.
let onUnauthorized: (() => void) | null = null;
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  onUnauthorized = handler;
}

interface RequestOptions {
  method?: "GET" | "POST";
  body?: unknown;
  params?: Record<string, string | number | string[] | undefined | null>;
  signal?: AbortSignal;
}

export function buildUrl(path: string, params?: RequestOptions["params"]): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params ?? {})) {
    if (value === undefined || value === null || value === "") continue;
    for (const v of Array.isArray(value) ? value : [value]) search.append(key, String(v));
  }
  const qs = search.toString();
  return qs ? `${path}?${qs}` : path;
}

export async function api<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json" };
  if (options.body !== undefined) headers["Content-Type"] = "application/json";
  const token = tokenStore.get();
  if (token) headers.Authorization = `Bearer ${token}`;

  let response: Response;
  try {
    response = await fetch(buildUrl(path, options.params), {
      method: options.method ?? "GET",
      headers,
      body: options.body === undefined ? undefined : JSON.stringify(options.body),
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === "AbortError") throw err;
    throw new ApiError(0, "network_error", "Cannot reach the API. Is the backend running (uv run outage-api)?");
  }

  const data = await response.json().catch(() => null);
  if (!response.ok) {
    const error = data?.error;
    if (response.status === 401 && token && path !== "/api/auth/login") onUnauthorized?.();
    throw new ApiError(
      response.status,
      error?.code ?? "http_error",
      error?.message ?? `${response.status} ${response.statusText}`,
      error?.details,
    );
  }
  return data as T;
}
