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
  const token = getAdminToken();
  if (!token) throw new Error("admin token not set — paste it in the kill-switch widget first");
  const r = await fetch(`${API}${path}`, {
    ...init,
    headers: {
      ...(init.headers || {}),
      "X-Admin-Token": token,
      "Content-Type": "application/json",
    },
  });
  if (r.status === 401) {
    clearAdminToken();
    throw new Error("admin token rejected — re-enter on home page");
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
