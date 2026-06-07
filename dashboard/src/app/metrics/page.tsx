"use client";

import useSWR from "swr";
import Link from "next/link";
import { useMemo, useState } from "react";
import { ResponsiveBar } from "@nivo/bar";
import { fetcher } from "@/lib/api";

type Window = "1h" | "24h" | "7d" | "30d";

type CategoryRow = {
  name: string;
  enabled: boolean;
  config?: Record<string, unknown>;
  signals_window: number;
  passed_window: number;
  fills_paper_window: number;
  settles_window: number;
  open_positions: number;
  closed_positions: number;
  wins_closed: number;
  losses_closed: number;
  win_rate_closed: number | null;
  cost_basis_usdc: number;
  realized_lifetime_usdc: number;
  unrealized_mtm_usdc: number | null;
  net_pnl_usdc: number | null;
  mark_known_count: number | null;
  mark_unknown_count: number | null;
  active_wallets: number;
};

type Totals = {
  signals_window: number;
  passed_window: number;
  fills_paper_window: number;
  settles_window: number;
  open_positions: number;
  closed_positions: number;
  wins_closed: number;
  losses_closed: number;
  win_rate_closed: number | null;
  cost_basis_usdc: number;
  realized_lifetime_usdc: number;
  unrealized_mtm_usdc: number | null;
};

type MetricsPayload = {
  window: Window;
  mode: string;
  categories: CategoryRow[];
  totals?: Totals;
};

const WINDOWS: Window[] = ["1h", "24h", "7d", "30d"];

export default function MetricsPage() {
  const [win, setWin] = useState<Window>("24h");
  const { data, error, isLoading } = useSWR<MetricsPayload>(
    `/metrics/categories?window=${win}`,
    fetcher,
    { refreshInterval: 15000 },
  );

  const cats = useMemo(() => {
    const list = data?.categories ?? [];
    // Sort by net PnL desc so winners surface first; fall back to signals.
    return [...list].sort((a, b) => {
      const pa = a.net_pnl_usdc ?? a.realized_lifetime_usdc ?? 0;
      const pb = b.net_pnl_usdc ?? b.realized_lifetime_usdc ?? 0;
      if (pa !== pb) return pb - pa;
      return b.signals_window - a.signals_window;
    });
  }, [data]);

  const totals = data?.totals;

  const barData = useMemo(
    () =>
      cats
        .filter((c) => (c.realized_lifetime_usdc || 0) !== 0)
        .slice(0, 20)
        .map((c) => ({ category: c.name, pnl: Number(c.realized_lifetime_usdc) })),
    [cats],
  );

  return (
    <div className="space-y-6">
      <header className="flex items-baseline gap-4 flex-wrap">
        <h1 className="text-2xl font-bold">Metrics</h1>
        {data?.mode && <span className="k">{data.mode} mode</span>}
        <div className="ml-auto flex items-center gap-2">
          <label className="k">Window</label>
          <select
            value={win}
            onChange={(e) => setWin(e.target.value as Window)}
            className="bg-black/40 border border-white/10 rounded px-2 py-1 text-sm"
          >
            {WINDOWS.map((w) => (
              <option key={w} value={w}>{w}</option>
            ))}
          </select>
        </div>
      </header>

      {error && (
        <div className="card border border-danger/40 text-danger text-sm">
          failed to load metrics: {error instanceof Error ? error.message : String(error)}
        </div>
      )}

      <section className="grid grid-cols-2 md:grid-cols-6 gap-4">
        <Stat k={`Signals (${win})`}    v={fmtInt(totals?.signals_window)} />
        <Stat k={`Fills (${win})`}      v={fmtInt(totals?.fills_paper_window)} />
        <Stat k="Open positions"        v={fmtInt(totals?.open_positions)} />
        <Stat
          k="Realized (lifetime)"
          v={fmtUsd(totals?.realized_lifetime_usdc)}
          tone={toneFor(totals?.realized_lifetime_usdc)}
        />
        <Stat
          k="Unrealized (live)"
          v={fmtUsd(totals?.unrealized_mtm_usdc)}
          tone={toneFor(totals?.unrealized_mtm_usdc)}
        />
        <Stat
          k="Win rate (closed)"
          v={
            totals?.win_rate_closed != null
              ? `${(totals.win_rate_closed * 100).toFixed(1)}%`
              : "n/a"
          }
          tone={
            totals?.win_rate_closed != null && totals.win_rate_closed > 0.5
              ? "text-accent"
              : totals?.win_rate_closed != null && totals.win_rate_closed < 0.5
              ? "text-danger"
              : ""
          }
        />
      </section>

      <section className="card">
        <div className="flex items-baseline justify-between mb-3">
          <h2 className="text-sm k">Per-category — sorted by net PnL</h2>
          <span className="text-xs text-muted">
            {cats.length} categories · refresh 15s
          </span>
        </div>

        {isLoading && !data ? (
          <div className="text-xs text-muted py-4">loading…</div>
        ) : cats.length === 0 ? (
          <div className="text-sm text-muted py-4">
            no metrics yet — bot may be warming up
          </div>
        ) : (
          <div className="overflow-x-auto">
            <table className="text-sm w-full">
              <thead className="text-muted text-xs uppercase">
                <tr>
                  <th className="text-left p-2">Category</th>
                  <th className="text-right p-2">Signals</th>
                  <th className="text-right p-2">Pass%</th>
                  <th className="text-right p-2">Fills</th>
                  <th className="text-right p-2">Settles</th>
                  <th className="text-right p-2">W/L</th>
                  <th className="text-right p-2">Win rate</th>
                  <th className="text-right p-2">Open</th>
                  <th className="text-right p-2">Cost basis</th>
                  <th className="text-right p-2">Realized</th>
                  <th className="text-right p-2">Unreal MTM</th>
                  <th className="text-right p-2">Net PnL</th>
                  <th className="text-right p-2">Wallets</th>
                </tr>
              </thead>
              <tbody>
                {cats.map((c) => {
                  const passRate =
                    c.signals_window > 0 ? c.passed_window / c.signals_window : null;
                  return (
                    <tr key={c.name} className="border-t border-white/5">
                      <td className="p-2">
                        <Link
                          href="/settings"
                          className="underline-offset-2 hover:underline"
                        >
                          {c.name}
                        </Link>{" "}
                        <span
                          className={`ml-1 text-[10px] px-1.5 py-0.5 rounded ${
                            c.enabled
                              ? "bg-accent/15 text-accent"
                              : "bg-white/10 text-muted"
                          }`}
                        >
                          {c.enabled ? "enabled" : "disabled"}
                        </span>
                      </td>
                      <td className="p-2 text-right tabular-nums">{fmtInt(c.signals_window)}</td>
                      <td className={`p-2 text-right tabular-nums ${
                        passRate != null && passRate > 0.5 ? "text-accent" : ""
                      }`}>
                        {passRate != null ? `${(passRate * 100).toFixed(1)}%` : "—"}
                      </td>
                      <td className="p-2 text-right tabular-nums">{fmtInt(c.fills_paper_window)}</td>
                      <td className="p-2 text-right tabular-nums">{fmtInt(c.settles_window)}</td>
                      <td className="p-2 text-right tabular-nums text-xs text-muted">
                        {c.wins_closed}/{c.losses_closed}
                      </td>
                      <td className={`p-2 text-right tabular-nums ${
                        c.win_rate_closed != null && c.win_rate_closed > 0.5 ? "text-accent" : ""
                      }`}>
                        {c.win_rate_closed != null
                          ? `${(c.win_rate_closed * 100).toFixed(1)}%`
                          : "n/a"}
                      </td>
                      <td className="p-2 text-right tabular-nums">{fmtInt(c.open_positions)}</td>
                      <td className="p-2 text-right tabular-nums">{fmtUsd(c.cost_basis_usdc)}</td>
                      <td className={`p-2 text-right tabular-nums ${toneFor(c.realized_lifetime_usdc)}`}>
                        {fmtUsd(c.realized_lifetime_usdc)}
                      </td>
                      <td className={`p-2 text-right tabular-nums ${toneFor(c.unrealized_mtm_usdc)}`}>
                        {fmtUsd(c.unrealized_mtm_usdc)}
                      </td>
                      <td className={`p-2 text-right tabular-nums font-semibold ${toneFor(c.net_pnl_usdc)}`}>
                        {fmtUsd(c.net_pnl_usdc)}
                      </td>
                      <td className="p-2 text-right tabular-nums">{fmtInt(c.active_wallets)}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="card">
        <h2 className="text-sm k mb-2">Realized PnL per category (lifetime, $)</h2>
        {barData.length === 0 ? (
          <div className="text-xs text-muted py-4">no realized fills yet</div>
        ) : (
          <div style={{ height: 280 }}>
            <ResponsiveBar
              data={barData}
              keys={["pnl"]}
              indexBy="category"
              margin={{ top: 8, right: 16, bottom: 64, left: 56 }}
              padding={0.3}
              colors={({ data }) => ((data.pnl as number) >= 0 ? "#22d39e" : "#ff5470")}
              axisBottom={{ tickRotation: -35 }}
              axisLeft={{ format: ".0f" }}
              enableLabel={false}
              theme={{
                background: "transparent",
                text: { fill: "#7a7a85" },
                grid: { line: { stroke: "#1c1c25" } },
              }}
            />
          </div>
        )}
      </section>
    </div>
  );
}

function Stat({ k, v, tone }: { k: string; v: string; tone?: string }) {
  return (
    <div className="card">
      <div className="k">{k}</div>
      <div className={`v ${tone || ""}`}>{v}</div>
    </div>
  );
}

function fmtInt(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  return Math.round(n).toLocaleString();
}

function fmtUsd(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  const sign = n < 0 ? "-" : "";
  return `${sign}$${Math.abs(n).toFixed(2)}`;
}

function toneFor(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "";
  if (n > 0) return "text-accent";
  if (n < 0) return "text-danger";
  return "";
}
