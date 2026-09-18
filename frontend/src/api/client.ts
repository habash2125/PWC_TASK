/**
 * Typed API client.
 *
 * The access token lives in memory only (never localStorage). The refresh token is an
 * HttpOnly cookie the browser sends to /api/v1/auth/* on its own; on a 401 we try one
 * refresh and replay the request, then give up and sign the user out.
 */
import type { Problem } from "@/types/api";

const BASE = "/api/v1";

let accessToken: string | null = null;
let onSignedOut: (() => void) | null = null;
let refreshing: Promise<boolean> | null = null;

export function setAccessToken(token: string | null) {
  accessToken = token;
}
export function getAccessToken() {
  return accessToken;
}
export function setSignedOutHandler(fn: () => void) {
  onSignedOut = fn;
}

export class ApiError extends Error {
  status: number;
  problem: Problem | null;
  constructor(status: number, problem: Problem | null, fallback: string) {
    super(problem?.detail ?? fallback);
    this.status = status;
    this.problem = problem;
  }
}

async function tryRefresh(): Promise<boolean> {
  if (!refreshing) {
    refreshing = (async () => {
      try {
        const r = await fetch(`${BASE}/auth/refresh`, { method: "POST", credentials: "include" });
        if (!r.ok) return false;
        const body = (await r.json()) as { access_token: string };
        accessToken = body.access_token;
        return true;
      } catch {
        return false;
      } finally {
        setTimeout(() => (refreshing = null), 0);
      }
    })();
  }
  return refreshing;
}

export interface RequestOptions {
  method?: string;
  body?: unknown;
  headers?: Record<string, string>;
  retry?: boolean;
}

export async function api<T>(path: string, opts: RequestOptions = {}): Promise<T> {
  const headers: Record<string, string> = { Accept: "application/json", ...(opts.headers ?? {}) };
  if (opts.body !== undefined) headers["Content-Type"] = "application/json";
  if (accessToken) headers.Authorization = `Bearer ${accessToken}`;
  const res = await fetch(`${BASE}${path}`, {
    method: opts.method ?? "GET",
    headers,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
    credentials: "include",
  });
  if (res.status === 401 && opts.retry !== false && !path.startsWith("/auth/login")) {
    if (await tryRefresh()) return api<T>(path, { ...opts, retry: false });
    accessToken = null;
    onSignedOut?.();
  }
  if (res.status === 204) return undefined as T;
  const text = await res.text();
  let parsed: unknown = null;
  try {
    parsed = text ? JSON.parse(text) : null;
  } catch {
    parsed = null;
  }
  if (!res.ok) {
    throw new ApiError(res.status, (parsed as Problem) ?? null, `${res.status} ${res.statusText}`);
  }
  return parsed as T;
}

export function idempotencyKey(): string {
  return crypto.randomUUID();
}
