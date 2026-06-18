"use client";
import useSWR from "swr";
import { fetcher, postAdmin, API } from "@/lib/api";
import { ResponsiveLine } from "@nivo/line";
import { useState, useEffect } from "react";
import type React from "react";
import { ADMIN_TOKEN_KEY, getAdminToken, adminApi, setAdminToken, clearAdminToken } from "@/lib/admin";
import { getSessionToken } from "@/lib/wallet";
import { useAuthStatus } from "@/lib/auth-status";
import ConnectWallet from "@/components/ConnectWallet";
import ConfirmModal from "@/components/ConfirmModal";

type Pnl = { ts: string; equity: number; realized: number; unrealized: number; open: number };
type Health = { ok: boolean; mode: string; can_sign: boolean; kill_switch: string | null };
type OnchainBalances = {
  address: string;
  pol: number | null;
  usdc_e: number | null;
  usdc_native: number | null;
  error: string | null;
};
type OnchainWallet = {
  id: number;
  label: string;
  address: string;
  funder_address: string | null;
  // address the balances were actually read from (funder/collateral wallet
  // for proxy sig-types; equals `address` only for a pure EOA).
  balance_of?: string | null;
  is_active: boolean;
  balances: OnchainBalances;
};
type OnchainResp = { wallets: OnchainWallet[]; note?: string };

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

// Live (real-money) account state read from Polymarket itself — distinct from
// the paper ledger above. See services/api/routes/live.py.
type LivePosition = {
  asset: string;
  condition_id: string | null;
  title: string | null;
  slug: string | null;
  outcome: string | null;
  size: number;
  avg_price: number;
  cur_price: number;
  current_value: number;
  initial_value: number;
  cash_pnl: number;
  pct_change: number | null;
  redeemable: boolean;
};
type LiveAccount = {
  configured: boolean;
  note?: string;
  address?: string;
  pusd_balance?: number | null;
  positions_value?: number;
  unrealized_pnl?: number;
  equity?: number | null;
  n_positions?: number;
  positions?: LivePosition[];
  errors?: { balance: string | null; positions: string | null };
};

export default function Home() {
  // Auth status declared FIRST — the positions SWR below depends on it
  // (signed-out users skip the call, signed-in users use adminApi). An
  // earlier ordering broke the prod build with "Block-scoped variable
  // 'authed' used before its declaration" because TypeScript's TDZ rule
  // fires on let/const before the hook ran.
  // Subscribes to storage events so the KILL/Clear buttons re-enable as
  // soon as the user connects MetaMask from the header.
  const authed = useAuthStatus();

  const { data: hh, error: hErr } = useSWR<Health>("/health", fetcher, { refreshInterval: 5000 });
  const { data: pnl, error: pErr } = useSWR<Pnl[]>("/pnl?mode=paper&limit=720", fetcher, { refreshInterval: 30000 });
  // /positions requires admin now (audit hardening — was an info leak).
  // Use adminApi when signed in; gate on authed so the SWR doesn't fire
  // a 401-storm before login completes.
  const { data: positions, error: posErr, mutate: mutatePositions } = useSWR<Position[]>(
    authed ? "/positions" : null,
    (p: string) => adminApi.get(p) as Promise<Position[]>,
    { refreshInterval: 15000 },
  );
  // /wallets/onchain — live POL + USDC.e from Polygon. Admin-only because
  // it reveals the bot wallet addresses. Polls every 30s (RPC isn't free
  // and balances don't change that fast).
  const { data: chain } = useSWR<OnchainResp>(
    authed ? "/wallets/onchain" : null,
    (p: string) => adminApi.get(p) as Promise<OnchainResp>,
    { refreshInterval: 30000 },
  );
  // /live/account — REAL Polymarket state for the deposit wallet: pUSD cash
  // (via clob-rs) + open positions/mark-to-market (via the data API). Admin-
  // only; polls every 30s. This is the "real cockpit" the paper cards aren't.
  const { data: live } = useSWR<LiveAccount>(
    authed ? "/live/account" : null,
    (p: string) => adminApi.get(p) as Promise<LiveAccount>,
    { refreshInterval: 30000 },
  );

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

      {/* These four always read the PAPER ledger (/pnl?mode=paper), so label
          them as such — even when the bot is in live mode. Real money lives
          in the "Live account" panel below. */}
      <section className="grid grid-cols-4 gap-4">
        <Stat k="Equity (paper)"
              v={last ? `$${last.equity.toFixed(2)}` : "—"} />
        <Stat k="Realized (paper)"   v={last ? `$${last.realized.toFixed(2)}` : "—"} />
        <Stat k="Unrealized (paper)" v={last ? `$${last.unrealized.toFixed(2)}` : "—"} />
        <Stat k="Open (paper)"       v={last ? String(last.open) : "—"} />
      </section>

      {authed && chain && chain.wallets.length > 0 && (
        <section className="card">
          <div className="flex items-baseline justify-between mb-2">
            <h2 className="text-sm k">Bot wallet — on-chain (Polygon, live)</h2>
            <span className="text-xs text-muted">refresh 30s</span>
          </div>
          {chain.wallets.map(w => (
            <div key={w.id} className="grid grid-cols-4 gap-4 py-2 border-t border-[#1c1c25] first:border-t-0">
              <div>
                <div className="text-xs text-muted">{w.label}</div>
                <div className="text-xs font-mono" title={w.balance_of ?? w.address}>
                  {(w.balance_of ?? w.address).slice(0, 6)}…{(w.balance_of ?? w.address).slice(-4)}
                </div>
                <div className="text-[10px] text-muted">
                  {w.balance_of && w.balance_of !== w.address ? "funder / collateral" : "signer (EOA)"}
                </div>
              </div>
              <Stat k="USDC.e"
                    v={w.balances.usdc_e == null ? "—" : `$${w.balances.usdc_e.toFixed(2)}`} />
              <Stat k="USDC (native)"
                    v={w.balances.usdc_native == null ? "—" :
                       w.balances.usdc_native < 0.01 ? "—" : `$${w.balances.usdc_native.toFixed(2)}`} />
              <Stat k="POL (gas)"
                    v={w.balances.pol == null ? "—" : `${w.balances.pol.toFixed(4)}`} />
              {w.balances.error && (
                <div className="col-span-4 text-xs text-danger">
                  RPC error: {w.balances.error}
                </div>
              )}
            </div>
          ))}
        </section>
      )}

      {authed && live && live.configured && (
        <section className="card" style={{ borderColor: "#22d39e55" }}>
          <div className="flex items-baseline justify-between mb-2">
            <h2 className="text-sm k text-accent">Live account — Polymarket (real money)</h2>
            <span className="text-xs text-muted font-mono" title={live.address}>
              {live.address ? `${live.address.slice(0, 6)}…${live.address.slice(-4)}` : ""} · 30s
            </span>
          </div>
          <div className="grid grid-cols-4 gap-4 mb-3">
            <Stat k="Equity (live)"
                  v={live.equity != null ? `$${live.equity.toFixed(2)}` : "—"} />
            <Stat k="pUSD (cash)"
                  v={live.pusd_balance != null ? `$${live.pusd_balance.toFixed(2)}` : "—"} />
            <Stat k="In positions"
                  v={`$${(live.positions_value ?? 0).toFixed(2)}`} />
            <Stat k="Unrealized"
                  v={`$${(live.unrealized_pnl ?? 0).toFixed(2)}`} />
          </div>

          {(live.errors?.balance || live.errors?.positions) && (
            <div className="text-[11px] mb-2 space-y-0.5">
              {live.errors?.balance && (
                <div className="text-danger">cash balance unavailable — {live.errors.balance}</div>
              )}
              {live.errors?.positions && (
                <div className="text-danger">positions unavailable — {live.errors.positions}</div>
              )}
            </div>
          )}

          {(live.positions?.length ?? 0) === 0 ? (
            <div className="text-xs text-muted py-1">
              {live.errors?.positions ? "could not load live positions" : "no open positions on Polymarket"}
            </div>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-muted text-xs uppercase">
                  <tr>
                    <th className="text-left p-2">Market</th>
                    <th className="text-left p-2">Outcome</th>
                    <th className="text-right p-2">Size</th>
                    <th className="text-right p-2">Avg</th>
                    <th className="text-right p-2">Cur</th>
                    <th className="text-right p-2">Value</th>
                    <th className="text-right p-2">PnL</th>
                    <th className="text-right p-2">%</th>
                  </tr>
                </thead>
                <tbody>
                  {live.positions!.map(p => (
                    <tr key={p.asset} className="border-t border-white/5">
                      <td className="p-2 max-w-[280px] truncate" title={p.title ?? p.slug ?? p.asset}>
                        {p.title ?? <span className="font-mono text-xs">{p.asset.slice(0, 10)}…</span>}
                        {p.redeemable && <span className="ml-2 text-[10px] text-accent">redeemable</span>}
                      </td>
                      <td className="p-2">{p.outcome ?? "—"}</td>
                      <td className="p-2 text-right">{p.size.toFixed(2)}</td>
                      <td className="p-2 text-right">{p.avg_price.toFixed(3)}</td>
                      <td className="p-2 text-right">{p.cur_price.toFixed(3)}</td>
                      <td className="p-2 text-right">${p.current_value.toFixed(2)}</td>
                      <td className={`p-2 text-right ${pnlColor(p.cash_pnl)}`}>
                        ${p.cash_pnl.toFixed(2)}
                      </td>
                      <td className={`p-2 text-right ${pnlColor(p.pct_change)}`}>
                        {p.pct_change != null ? `${(p.pct_change * 100).toFixed(1)}%` : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <p className="text-[10px] text-muted mt-2">
            Real on-venue money (Polymarket data API + clob-rs collateral) — distinct from the
            paper ledger below. Equity = pUSD cash + marked value of open positions.
          </p>
        </section>
      )}

      <section className="card">
        <div className="flex items-baseline justify-between mb-2">
          <h2 className="text-sm k">Open positions — paper ledger</h2>
          <div className="flex items-center gap-3">
            <span className="text-xs text-muted">
              {open.length} open · live mark every 15s
            </span>
            {authed && open.length > 0 && (
              <CloseAllButton onDone={() => mutatePositions()} />
            )}
          </div>
        </div>
        {open.length === 0 ? (
          <div className="text-xs text-muted py-2">
            {!authed
              ? "Sign in (Connect Wallet, header) to view open positions."
              : posErr
                ? `failed to load — ${String(posErr).slice(0, 120)}`
                : "no open positions"}
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
                  <th className="text-right p-2"></th>
                </tr>
              </thead>
              <tbody>
                {open.map(p => (
                  <PositionRow
                    key={`${p.market_id}-${p.outcome}`}
                    p={p}
                    authed={authed}
                    onClosed={() => mutatePositions()}
                  />
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
          <button disabled={busy || !authed}
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
          <button disabled={busy || !authed}
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

function PositionRow({
  p, authed, onClosed,
}: {
  p: Position;
  authed: boolean;
  onClosed: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<"ok" | "err" | null>(null);
  const [errMsg, setErrMsg] = useState<string | null>(null);

  // Map raw reject reasons to operator-friendly messages. The previous
  // tooltip just showed the raw "no_bids" / "insufficient_depth" which
  // doesn't tell the user whether to retry or wait.
  const friendlyError = (raw: string | undefined): string => {
    if (!raw) return "rejected";
    if (raw === "no_bids") return "no bids + no fallback price — wait for resolution";
    if (raw === "insufficient_depth") return "thin book — try again or wait";
    if (raw === "no_position") return "already closed";
    if (raw === "market_unknown") return "market not in DB — restart needed";
    if (raw === "no_token_id") return "market metadata missing";
    return raw;
  };

  const onClose = async () => {
    if (!confirm(`Close ${p.outcome} on ${p.question?.slice(0, 60) ?? p.market_id.slice(0, 14)}? Sells at best bid (or best mark if book empty).`)) return;
    setBusy(true); setErrMsg(null);
    try {
      const r = await adminApi.post("/positions/close", {
        market_id: p.market_id, outcome: p.outcome, fraction: 1.0,
      }) as { status?: string; reason?: string; error?: string };
      if (r?.status === "filled" || r?.status === "submitted" || r?.status === "partial") {
        setDone("ok");
        // Trigger immediate SWR refresh so the row disappears from
        // the live table instead of waiting up to 15 s for the auto-poll.
        setTimeout(onClosed, 500);
      } else {
        setDone("err");
        setErrMsg(friendlyError(r?.reason ?? r?.error ?? r?.status));
      }
    } catch (e) {
      setDone("err");
      setErrMsg(String(e instanceof Error ? e.message : e).slice(0, 100));
    } finally {
      setBusy(false);
    }
  };

  return (
    <tr className="border-t border-white/5">
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
      <td className="p-2 text-right">
        {authed && (
          done === "ok" ? (
            <span className="text-accent text-xs">✓ closed</span>
          ) : (
            <div className="flex items-center gap-1 justify-end">
              <button
                onClick={onClose}
                disabled={busy}
                className="text-xs px-2 py-1 rounded border border-danger/40 text-danger hover:bg-danger/10 disabled:opacity-40"
                title={errMsg ?? "Sell at best bid (paper mode)"}
              >
                {busy ? "…" : done === "err" ? "retry" : "close"}
              </button>
              {errMsg && done === "err" && (
                <span className="text-[10px] text-muted max-w-[120px] truncate" title={errMsg}>
                  {errMsg}
                </span>
              )}
            </div>
          )
        )}
      </td>
    </tr>
  );
}

function CloseAllButton({ onDone }: { onDone: () => void }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<{ closed_n?: number; rejected_n?: number; total?: number; summary?: string } | null>(null);

  const onConfirm = async () => {
    setBusy(true);
    try {
      const r = await adminApi.post("/positions/close-all", {}) as any;
      setResult(r);
      onDone();
    } catch (e) {
      setResult({ summary: `error: ${String(e instanceof Error ? e.message : e)}` });
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <button
        onClick={() => { setResult(null); setOpen(true); }}
        className="text-xs px-3 py-1 rounded border border-danger/50 text-danger hover:bg-danger/10"
        title="Emergency: sell every open paper position at best bid"
      >
        Close all
      </button>
      <ConfirmModal
        open={open}
        title="EMERGENCY — Close all open positions"
        body={
          "Sells EVERY open paper position at best bid right now.\n\n" +
          "Partial closes are NOT rolled back if one position fails. The\n" +
          "kill switch is NOT toggled — new signals can still arrive and\n" +
          "open new positions until you also activate it.\n\n" +
          "Type CLOSE-ALL to confirm."
        }
        confirmText="CLOSE-ALL"
        busy={busy}
        onCancel={() => setOpen(false)}
        onConfirm={async () => {
          await onConfirm();
          setOpen(false);
        }}
      />
      {result && (
        <span className="text-xs text-muted">
          {result.summary ?? `${result.closed_n ?? 0}/${result.total ?? 0} closed${result.rejected_n ? `, ${result.rejected_n} failed` : ""}`}
        </span>
      )}
    </>
  );
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
