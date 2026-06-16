/**
 * Admin API helpers — PATCH / DELETE wrappers on top of api.ts.
 *
 * Auth model: the dashboard stores the admin token in `sessionStorage`
 * (cleared on tab close), accessed via `getAdminToken()`. The user
 * enters it once via the kill-switch widget on the home page.
 */

import { API } from "./api";

export const ADMIN_TOKEN_KEY = "polybot_admin_token";

export function getAdminToken(): string | null {
  if (typeof window === "undefined") return null;
  return sessionStorage.getItem(ADMIN_TOKEN_KEY);
}

export function setAdminToken(t: string): void {
  if (typeof window === "undefined") return;
  sessionStorage.setItem(ADMIN_TOKEN_KEY, t);
}

export function clearAdminToken(): void {
  if (typeof window === "undefined") return;
  sessionStorage.removeItem(ADMIN_TOKEN_KEY);
}

/** Compute a per-mode confirm token client-side. Mirrors the server's
 *  HMAC scheme (services/api/deps.py:make_admin_token). We can't ship
 *  the admin secret to the browser without defeating the auth model —
 *  but the kill-switch confirm uses a server-issued challenge instead.
 *  For the live-mode switch, the user just re-confirms the admin token
 *  and the server computes its own HMAC; we send a literal
 *  `${epoch}:${hmac_via_subtle_crypto}` if we ever go that route. For
 *  now, the server generates and returns the confirm via a dedicated
 *  /admin/settings/live-confirm endpoint (not implemented here — TODO).
 */

async function adminFetch(
  path: string,
  init: RequestInit,
): Promise<unknown> {
  // Prefer SIWE session, fall back to legacy admin token. The user may
  // have either configured.
  // Lazy import to avoid pulling ethers into pages that don't need it.
  const { getSessionToken } = await import("./wallet");
  const session = getSessionToken();
  const token = getAdminToken();
  if (!session && !token) {
    throw new Error("not signed in — click 'Connect Wallet' on the home page");
  }
  const authHeaders: Record<string, string> = {};
  if (session) authHeaders["X-Session-Token"] = session;
  if (token) authHeaders["X-Admin-Token"] = token;
  const r = await fetch(`${API}${path}`, {
    ...init,
    headers: {
      ...(init.headers || {}),
      ...authHeaders,
      "Content-Type": "application/json",
    },
  });
  if (r.status === 401) {
    // Session expired or token rejected — clear and ask the user to re-auth.
    clearAdminToken();
    const { clearSession } = await import("./wallet");
    clearSession();
    throw new Error("session expired — sign in again on the home page");
  }
  if (!r.ok) {
    const text = await r.text();
    throw new Error(`${path} ${r.status}: ${text.slice(0, 200)}`);
  }
  if (r.status === 204) return null;
  return r.json();
}

export const adminApi = {
  get:    (path: string)                  => adminFetch(path, { method: "GET" }),
  patch:  (path: string, body: unknown)   => adminFetch(path, { method: "PATCH", body: JSON.stringify(body) }),
  post:   (path: string, body: unknown)   => adminFetch(path, { method: "POST",  body: JSON.stringify(body) }),
  delete: (path: string)                  => adminFetch(path, { method: "DELETE" }),
};
