"use client";
import useSWR from "swr";
import { fetcher, postAdmin, API } from "@/lib/api";
import { ResponsiveLine } from "@nivo/line";
import { useState, useEffect } from "react";
import type React from "react";
import { ADMIN_TOKEN_KEY, getAdminToken, setAdminToken, clearAdminToken } from "@/lib/admin";
import { getSessionToken } from "@/lib/wallet";
import ConnectWallet from "@/components/ConnectWallet";

type Pnl = { ts: string; equity: number; realized: number; unrealized: number; open: number };
type Health = { ok: boolean; mode: string; can_sign: boolean; kill_switch: string | null };
type Position = {
  market_id: string;
  slug: string | null;
  question: string | null;
  category: string | null;
  end_date: string | null;
  outcome: string;
  size_shares: number;
  avg_price: number;
  cost_usdc: number;
  mark_price: number | null;
  mark_to_market_usdc: number | null;
  pct_change: number | null;
  realized_pnl_usdc: number;
  resolved: boolean;
  updated_at: string | null;
};

export default function Home() {
  const { data: hh, error: hErr } = useSWR<Health>("/health", fetcher, { refreshInterval: 5000 });
  const { data: pnl, error: pErr } = useSWR<Pnl[]>("/pnl?mode=paper&limit=720", fetcher, { refreshInterval: 30000 });
  const { data: positions, error: posErr } = useSWR<Position[]>("/positions", fetcher, { refreshInterval: 15000 });

  // Admin token persists across pages via sessionStorage. We seed the input
  // from sessionStorage on mount so the user doesn't have to re-enter it
  // every time they navigate back here.
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  useEffect(() => {
    const t = getAdminToken();
    if (t) setToken(t);
  }, []);

  const equityCurve = (pnl || []).map(p => ({ x: new Date(p.ts).getTime(), y: p.equity }));
  const last = pnl?.[pnl.length - 1];
  const open = positions ?? [];

  const apiFailed = hErr || pErr || posErr;
  const firstErr = String(hErr ?? pErr ?? posErr ?? "");

  return (
    <div className="space-y-6">
      <header className="flex items-baseline gap-6">
        <h1 className="text-2xl font-bold">Overview</h1>
        <span className="k">{hh?.mode ?? "?"} mode</span>
        {hh?.kill_switch
          ? <span className="text-danger text-sm">KILLED: {hh.kill_switch}</span>
          : <span className="text-accent text-sm">running</span>}
      </header>

      {apiFailed && (
        <section className="card border-danger" style={{ borderColor: "#ff5470" }}>
          <h2 className="text-sm k text-danger mb-1">API unreachable</h2>
          <p className="text-xs text-muted whitespace-pre-line">
            {firstErr}
            {"\n\nDashboard is loaded but cannot reach the API at "}
            <code className="text-text">{API}</code>{". Common causes:"}
            {"\n  • the api container is not running (check `docker compose ps`)"}
            {"\n  • CORS blocks the call — hard-refresh this tab (Ctrl+Shift+R)"}
            {"\n  • Docker Desktop port-forward broke — try `wsl --shutdown` then restart Docker"}
          </p>
        </section>
      )}

      <section className="grid grid-cols-4 gap-4">
        <Stat k="Equity"     v={last ? `$${last.equity.toFixed(2)}` : "—"} />
        <Stat k="Realized"   v={last ? `$${last.realized.toFixed(2)}` : "—"} />
        <Stat k="Unrealized" v={last ? `$${last.unrealized.toFixed(2)}` : "—"} />
        <Stat k="Open"       v={last ? String(last.open) : "—"} />
      </section>

      <section className="card">
        <div className="flex items-baseline justify-between mb-2">
          <h2 className="text-sm k">Open positions</h2>
          <span className="text-xs text-muted">
            {open.length} open · live mark every 15s
          </span>
        </div>
        {open.length === 0 ? (
          <div className="text-xs text-muted py-2">
            {posErr ? `failed to load — ${String(posErr).slice(0, 120)}` : "no open positions"}
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-muted text-xs uppercase">
                <tr>
                  <th className="text-left p-2">Market</th>
                  <th className="text-left p-2">Cat</th>
                  <th className="text-left p-2">Outcome</th>
                  <th className="text-right p-2">Size</th>
                  <th className="text-right p-2">Avg</th>
                  <th className="text-right p-2">Mark</th>
                  <th className="text-right p-2">Cost</th>
                  <th className="text-right p-2">M-to-M</th>
                  <th className="text-right p-2">%</th>
                  <th className="text-left p-2">Resolves</th>
                </tr>
              </thead>
              <tbody>
                {open.map(p => (
                  <tr key={`${p.market_id}-${p.outcome}`} className="border-t border-white/5">
                    <td className="p-2 max-w-[280px] truncate" title={p.question ?? p.market_id}>
                      {p.question ?? <span className="font-mono text-xs">{p.market_id.slice(0, 14)}…</span>}
                    </td>
                    <td className="p-2 text-xs">{p.category ?? "—"}</td>
                    <td className="p-2">{p.outcome}</td>
                    <td className="p-2 text-right">{p.size_shares.toFixed(0)}</td>
                    <td className="p-2 text-right">{p.avg_price.toFixed(3)}</td>
                    <td className="p-2 text-right">{p.mark_price != null ? p.mark_price.toFixed(3) : "—"}</td>
                    <td className="p-2 text-right">${p.cost_usdc.toFixed(2)}</td>
                    <td className={`p-2 text-right ${pnlColor(p.mark_to_market_usdc)}`}>
                      {p.mark_to_market_usdc != null ? `$${p.mark_to_market_usdc.toFixed(2)}` : "—"}
                    </td>
                    <td className={`p-2 text-right ${pnlColor(p.pct_change)}`}>
                      {p.pct_change != null ? `${(p.pct_change * 100).toFixed(1)}%` : "—"}
                    </td>
                    <td className="p-2 text-xs whitespace-nowrap">{resolveStatus(p)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="card">
        <h2 className="text-sm k mb-2">Equity curve — paper mode</h2>
        <div style={{ height: 320 }}>
          {equityCurve.length === 0 ? (
            <div className="text-xs text-muted">
              {pErr ? `failed to load equity history — ${String(pErr).slice(0, 120)}` : "no snapshots yet"}
            </div>
          ) : (
            <ResponsiveLine
              data={[{ id: "equity", data: equityCurve }]}
              margin={{ top: 8, right: 16, bottom: 32, left: 56 }}
              xScale={{ type: "linear" }}
              yScale={{ type: "linear", min: "auto", max: "auto" }}
              curve="monotoneX"
              enableArea
              colors={["#22d39e"]}
              theme={{ background: "transparent", text: { fill: "#7a7a85" }, grid: { line: { stroke: "#1c1c25" } } }}
              axisBottom={{ format: (v) => new Date(v as number).toLocaleTimeString() }}
              axisLeft={{ format: ".0f" }}
              enablePoints={false}
            />
          )}
        </div>
      </section>

      <section className="card">
        <h2 className="text-sm k mb-2">Sign in</h2>
        <p className="text-xs text-muted mb-3">
          Connect your Ethereum wallet to unlock <code>/settings</code> (Wallet, Risk, Categories,
          Mode) and the KILL/Clear buttons. Sign-in is gasless — just an ECDSA signature, no
          on-chain tx. You stay signed in for 24 h or until you close the tab.
        </p>

        <ConnectWallet />

        <details className="mt-4">
          <summary className="text-xs text-muted cursor-pointer hover:text-text">
            Or paste an admin token (legacy)
          </summary>
          <div className="mt-3 flex gap-2 items-center flex-wrap">
            <input type="password" placeholder="admin token" value={token}
                   onChange={e => setToken(e.target.value)}
                   className="bg-black/40 border border-white/10 rounded px-3 py-2 text-sm w-72"/>
            <button
              className="bg-accent text-black px-3 py-2 rounded text-sm disabled:opacity-40"
              disabled={!token}
              onClick={() => {
                setAdminToken(token);
                setMsg({ kind: "ok", text: `token saved (${token.length} chars)` });
                setTimeout(() => setMsg(null), 4000);
              }}
            >Save token</button>
            <button
              className="text-xs text-muted underline"
              onClick={() => { clearAdminToken(); setToken(""); setMsg({ kind: "ok", text: "token cleared" }); setTimeout(() => setMsg(null), 3000); }}
            >clear</button>
            <span className="text-xs text-muted ml-2">
              current: {getAdminToken() ? `set (${getAdminToken()!.length} chars)` : "none"}
            </span>
          </div>
        </details>

        <div className="flex gap-2 items-center mt-4 flex-wrap">
          <span className="text-xs k">Kill switch:</span>
          <button disabled={busy || (!token && !getSessionToken())}
            className="bg-danger text-white px-3 py-2 rounded text-sm disabled:opacity-40"
            onClick={async () => {
              setBusy(true); setMsg(null);
              try {
                const session = getSessionToken();
                if (session) {
                  const r = await fetch(`${API}/admin/kill?reason=dashboard`, {
                    method: "POST",
                    headers: { "X-Session-Token": session },
                  });
                  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
                } else {
                  await postAdmin("/admin/kill?reason=dashboard", token);
                }
                setMsg({ kind: "ok", text: "kill switch activated — no new trades will fill" });
              } catch (e) {
                setMsg({ kind: "err", text: String(e instanceof Error ? e.message : e) });
              } finally { setBusy(false); }
            }}>
            KILL
          </button>
          <button disabled={busy || (!token && !getSessionToken())}
            className="bg-accent text-black px-3 py-2 rounded text-sm disabled:opacity-40"
            onClick={async () => {
              setBusy(true); setMsg(null);
              try {
                const session = getSessionToken();
                if (session) {
                  const r = await fetch(`${API}/admin/kill/clear?by=dashboard`, {
                    method: "POST",
                    headers: { "X-Session-Token": session },
                  });
                  if (!r.ok) throw new Error(`${r.status}: ${await r.text()}`);
                } else {
                  await postAdmin("/admin/kill/clear?by=dashboard", token);
                }
                setMsg({ kind: "ok", text: "kill switch cleared — trading resumed" });
              } catch (e) {
                setMsg({ kind: "err", text: String(e instanceof Error ? e.message : e) });
              } finally { setBusy(false); }
            }}>
            Clear
          </button>
        </div>

        {msg && (
          <div className={`mt-3 text-xs ${msg.kind === "ok" ? "text-accent" : "text-danger"}`}>
            {msg.text}
          </div>
        )}
      </section>
    </div>
  );
}

function Stat({ k, v }: { k: string; v: string }) {
  return <div className="card"><div className="k">{k}</div><div className="v">{v}</div></div>;
}

function pnlColor(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "";
  if (n > 0) return "text-accent";
  if (n < 0) return "text-danger";
  return "";
}

function resolveStatus(p: Position): React.ReactNode {
  if (p.resolved) {
    return <span className="text-accent">resolved · auto-settle pending</span>;
  }
  if (!p.end_date) {
    return <span className="text-muted">unknown</span>;
  }
  const ends = new Date(p.end_date);
  const ms = ends.getTime() - Date.now();
  if (ms <= 0) {
    return (
      <span className="text-danger" title={ends.toISOString()}>
        past end-date — awaiting Polymarket UMA
      </span>
    );
  }
  const hours = ms / 3_600_000;
  const days = ms / 86_400_000;
  const label =
    hours < 24
      ? `${hours.toFixed(0)}h`
      : days < 30
        ? `${days.toFixed(0)}d`
        : `${(days / 30).toFixed(1)}mo`;
  const tone = hours < 48 ? "text-text" : "text-muted";
  return (
    <span className={tone} title={ends.toISOString()}>
      in {label}
    </span>
  );
}
