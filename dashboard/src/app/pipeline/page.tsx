"use client";
import useSWR from "swr";
import type { ReactNode } from "react";
import { fetcher } from "@/lib/api";
import StatusPill from "@/components/StatusPill";

// Field names match the API response exactly (services/api/routes/pipeline.py).
// If the API ever renames these, the dashboard renders "—" silently — keep
// the two in sync.
type Health = {
  ws_subscribed_assets: number | null;
  trades_per_min_15m: number;
  signals_per_min_15m: number;
  signals_pass_rate_15m: number;
  fills_per_min_15m: number;
  last_trade_ts: string | null;
  last_signal_ts: string | null;
  last_fill_ts: string | null;
  lag_seconds: number | null;
  active_wallets: number;
  markets_total: number;
  markets_uncategorised: number;
  kill_switch_active: boolean;
  current_mode: string;
};

type GateBucket = { pass: number; fail: number };
type GatesSummary = {
  window_minutes: number;
  total_signals: number;
  total_pass: number;
  gates: Record<string, GateBucket>;
};

export default function PipelinePage() {
  const { data: health } = useSWR<Health>("/pipeline/health", fetcher, {
    refreshInterval: 5000,
  });
  const { data: gates } = useSWR<GatesSummary>(
    "/pipeline/gates/summary",
    fetcher,
    { refreshInterval: 15000 },
  );

  // `gates.gates` is an object {gate_name: {pass, fail}}; flatten into rows
  // sorted by total volume so the noisiest gate is on top.
  const rows = Object.entries(gates?.gates ?? {})
    .map(([gate, b]) => ({ gate, passed: b.pass, failed: b.fail }))
    .sort((a, b) => (b.passed + b.failed) - (a.passed + a.failed));

  const killed = !!health?.kill_switch_active;

  return (
    <div className="space-y-6">
      <header className="flex items-baseline gap-6">
        <h1 className="text-2xl font-bold">Pipeline health</h1>
        <span className="k">live polling · 5s / 15s</span>
        {killed ? (
          <span className="text-danger text-sm">KILLED</span>
        ) : (
          <span className="text-accent text-sm">running</span>
        )}
      </header>

      <p className="text-muted text-xs max-w-3xl">
        <span className="text-text">Wallet trades / min</span> = activity of the
        ~80 tracked Polymarket wallets (our intel source).{" "}
        <span className="text-text">Signals / min</span> = correlation clusters
        our engine produces.{" "}
        <span className="text-text">Pass rate</span> = % of clusters that survive
        the 8-gate chain.{" "}
        <span className="text-text">Fills / min</span> = orders WE actually
        placed (paper or live).
      </p>

      <section className="grid grid-cols-4 gap-4">
        <Stat
          k="Wallet trades / min"
          v={fmtNum(health?.trades_per_min_15m)}
          pill={
            <StatusPill
              value={health?.trades_per_min_15m}
              green={1}
              yellow={0.1}
              label={health?.trades_per_min_15m != null ? "tracked" : "—"}
            />
          }
        />
        <Stat
          k="Signals / min"
          v={fmtNum(health?.signals_per_min_15m)}
          pill={
            <StatusPill
              value={health?.signals_per_min_15m}
              green={0.5}
              yellow={0.05}
              label={health?.signals_per_min_15m != null ? "rate" : "—"}
            />
          }
        />
        <Stat
          k="Pass rate"
          v={fmtPct(health?.signals_pass_rate_15m)}
          pill={
            <StatusPill
              value={health?.signals_pass_rate_15m}
              green={0.5}
              yellow={0.2}
              label={fmtPct(health?.signals_pass_rate_15m)}
            />
          }
        />
        <Stat
          k="Fills / min"
          v={fmtNum(health?.fills_per_min_15m)}
          pill={
            <StatusPill
              value={health?.fills_per_min_15m}
              green={0.5}
              yellow={0.05}
              label={health?.fills_per_min_15m != null ? "rate" : "—"}
            />
          }
        />
        <Stat
          k="Lag (s)"
          v={fmtNum(health?.lag_seconds)}
          pill={
            <StatusPill
              value={health?.lag_seconds}
              green={2}
              yellow={10}
              invert
              label={
                health?.lag_seconds != null
                  ? `${health.lag_seconds.toFixed(0)}s`
                  : "—"
              }
            />
          }
        />
        <Stat
          k="WS assets"
          v={fmtInt(health?.ws_subscribed_assets)}
          pill={
            <StatusPill
              value={health?.ws_subscribed_assets}
              green={1}
              yellow={0}
              label={
                health?.ws_subscribed_assets == null
                  ? "—"
                  : String(health.ws_subscribed_assets)
              }
            />
          }
        />
        <Stat
          k="Active wallets"
          v={fmtInt(health?.active_wallets)}
          pill={
            <StatusPill
              value={health?.active_wallets}
              green={5}
              yellow={1}
              label={health?.active_wallets != null ? "count" : "—"}
            />
          }
        />
        <Stat
          k="Markets uncat."
          v={fmtInt(health?.markets_uncategorised)}
          pill={
            <StatusPill
              value={health?.markets_uncategorised}
              green={5}
              yellow={50}
              invert
              label={
                health?.markets_uncategorised != null
                  ? String(health.markets_uncategorised)
                  : "—"
              }
            />
          }
        />
      </section>

      <section className="grid grid-cols-4 gap-4">
        <Stat k="Mode"           v={health?.current_mode ?? "—"} />
        <Stat k="Markets total"  v={fmtInt(health?.markets_total)} />
        <Stat k="Last trade"     v={fmtRel(health?.last_trade_ts)} />
        <Stat k="Last fill"      v={fmtRel(health?.last_fill_ts)} />
      </section>

      <section className="card">
        <div className="flex items-baseline justify-between mb-3">
          <h2 className="text-sm k">Gate pass / fail (last 60 min)</h2>
          <span className="text-xs text-muted">
            {rows.length} gates · {gates?.total_signals ?? 0} signals · refresh 15s
          </span>
        </div>
        <div className="space-y-2">
          {rows.length === 0 && (
            <div className="text-xs text-muted">no gate data yet…</div>
          )}
          {rows.map((g) => {
            const total = g.passed + g.failed;
            const passPct = total > 0 ? (g.passed / total) * 100 : 0;
            const failPct = total > 0 ? (g.failed / total) * 100 : 0;
            return (
              <div key={g.gate} className="space-y-1">
                <div className="flex justify-between text-xs">
                  <span className="font-mono">{g.gate}</span>
                  <span className="text-muted tabular-nums">
                    <span className="text-accent">{g.passed}</span>
                    <span> / </span>
                    <span className="text-danger">{g.failed}</span>
                    <span className="text-muted"> ({total})</span>
                  </span>
                </div>
                <div className="flex h-3 w-full overflow-hidden rounded bg-white/5">
                  {total === 0 ? (
                    <div className="w-full bg-white/5" />
                  ) : (
                    <>
                      <div className="bg-accent" style={{ width: `${passPct}%` }} title={`pass ${g.passed}`} />
                      <div className="bg-danger" style={{ width: `${failPct}%` }} title={`fail ${g.failed}`} />
                    </>
                  )}
                </div>
              </div>
            );
          })}
        </div>
      </section>
    </div>
  );
}

function Stat({ k, v, pill }: { k: string; v: string; pill?: ReactNode }) {
  return (
    <div className="card">
      <div className="flex items-center justify-between">
        <div className="k">{k}</div>
        {pill}
      </div>
      <div className="v mt-1">{v}</div>
    </div>
  );
}

function isNum(n: number | null | undefined): n is number {
  return typeof n === "number" && !Number.isNaN(n);
}
function fmtNum(n: number | null | undefined): string {
  if (!isNum(n)) return "—";
  return n.toFixed(2);
}
function fmtInt(n: number | null | undefined): string {
  if (!isNum(n)) return "—";
  return String(Math.round(n));
}
function fmtPct(n: number | null | undefined): string {
  if (!isNum(n)) return "—";
  return `${(n * 100).toFixed(1)}%`;
}
function fmtRel(iso: string | null | undefined): string {
  if (!iso) return "—";
  const ts = Date.parse(iso);
  if (Number.isNaN(ts)) return "—";
  const seconds = Math.max(0, Math.floor((Date.now() - ts) / 1000));
  if (seconds < 90)   return `${seconds}s ago`;
  if (seconds < 5400) return `${Math.round(seconds / 60)}m ago`;
  if (seconds < 172800) return `${Math.round(seconds / 3600)}h ago`;
  return `${Math.round(seconds / 86400)}d ago`;
}
