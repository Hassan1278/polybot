// API base URL — default uses the same-origin Next.js rewrite (/api/* → api:8000)
// which avoids CORS and uses a single port for browser↔server.
// Set NEXT_PUBLIC_API_URL to override (e.g. point at a remote production API).
export const API = process.env.NEXT_PUBLIC_API_URL || "/api";

/**
 * SWR fetcher — surfaces real errors instead of swallowing them.
 *
 * On network failure (CORS, refused connection, offline) the previous
 * implementation would throw a vague "api X 0" message that SWR rendered
 * as undefined data forever — the user just saw "—" placeholders with no
 * indication that anything was wrong. Now we throw an Error with the
 * actual cause so the page can show "API unreachable: <reason>".
 */
export async function fetcher<T>(path: string): Promise<T> {
  let r: Response;
  try {
    r = await fetch(`${API}${path}`, { cache: "no-store" });
  } catch (e) {
    // TypeError on cross-origin / refused / offline — most common in
    // dev when the api container isn't running or CORS isn't set up.
    const msg = e instanceof Error ? e.message : String(e);
    throw new Error(`network ${path}: ${msg} (is the api running on ${API}?)`);
  }
  if (!r.ok) {
    const text = await r.text().catch(() => "");
    throw new Error(`api ${path} ${r.status}: ${text.slice(0, 200)}`);
  }
  return r.json() as Promise<T>;
}

export async function postAdmin(path: string, token: string, body?: unknown): Promise<unknown> {
  const r = await fetch(`${API}${path}`, {
    method: "POST",
    headers: { "X-Admin-Token": token, "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) {
    const text = await r.text().catch(() => "");
    throw new Error(`api ${path} ${r.status}: ${text.slice(0, 200)}`);
  }
  return r.json();
}

export function openWs(onMsg: (m: { channel: string; data: any }) => void): WebSocket {
  // WS rewriting through Next.js is unreliable; always hit the api host
  // directly. If API is "/api" (same-origin rewrite mode), build ws://host:8000.
  let wsBase: string;
  if (API.startsWith("/")) {
    if (typeof window !== "undefined") {
      const host = window.location.hostname;
      wsBase = `ws://${host}:8000`;
    } else {
      wsBase = "ws://localhost:8000";
    }
  } else {
    wsBase = API.replace(/^http/, "ws");
  }
  const ws = new WebSocket(wsBase + "/ws");
  ws.onmessage = (e) => {
    try { onMsg(JSON.parse(e.data)); } catch {}
  };
  return ws;
}
