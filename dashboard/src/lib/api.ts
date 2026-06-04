export const API = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";

export async function fetcher<T>(path: string): Promise<T> {
  const r = await fetch(`${API}${path}`, { cache: "no-store" });
  if (!r.ok) throw new Error(`api ${path} ${r.status}`);
  return r.json() as Promise<T>;
}

export async function postAdmin(path: string, token: string, body?: unknown): Promise<unknown> {
  const r = await fetch(`${API}${path}`, {
    method: "POST",
    headers: { "X-Admin-Token": token, "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!r.ok) throw new Error(`api ${path} ${r.status}`);
  return r.json();
}

export function openWs(onMsg: (m: { channel: string; data: any }) => void): WebSocket {
  const url = API.replace(/^http/, "ws") + "/ws";
  const ws = new WebSocket(url);
  ws.onmessage = (e) => {
    try { onMsg(JSON.parse(e.data)); } catch {}
  };
  return ws;
}
