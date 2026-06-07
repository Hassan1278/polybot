"use client";

import useSWR from "swr";
import Link from "next/link";
import { useMemo, useState } from "react";
import { ResponsiveBar } from "@nivo/bar";
import { fetcher } from "@/lib/api";
import { getAdminToken } from "@/lib/admin";

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
  cost_basis_usdc: number;
  win_rate_estimate: number | null;
  active_wallets: number;
  // Server may also include realized PnL on the row; treated as optional.
  realized_pnl_usdc?: number | null;
};

type MetricsPayload = {
  window: Window;
  mode: string;
  categories: CategoryRow[];
};

const WINDOWS: Window[] = ["1h", "24h", "7d", "30d"];

export default function MetricsPage() {
  const [win, setWin] = useState<Window>("24h");
  // Admin token isn't required for /metrics/categories (it's a read endpoint),
  // but we surface a note if it's missing because every other admin page needs
  // it — the rest of the dashboard expects users to set it on `/`.
  const hasToken = typeof window !== "undefined" ? !!getAdminToken() : false;

  const { data, error, isLoading } = useSWR<MetricsPayload>(
    `/metrics/categories?window=${win}`,
    fetcher,
    { refreshInterval: 15000 },
  );

  const cats = useMemo(() => {
    const list = data?.categories ?? [];
    return [...list].sort((a, b) => b.signals_window - a.signals_window);
  }, [data]);

  const totals = useMemo(() => {
    return cats.reduce(
      (acc, c) => {
        acc.signals += c.signals_window || 0;
        acc.fills += c.fills_paper_window || 0;
        acc.settles += c.settles_window || 0;
        acc.open += c.open_positions || 0;
        acc.realized += Number(c.realized_pnl_usdc ?? 0) || 0;
        acc.hasRealized = acc.hasRealized || c.realized_pnl_usdc != null;
        return acc;
      },
      { signals: 0, fills: 0, settles: 0, open: 0, realized: 0, hasRealized: false },
    );
  }, [cats]);

  const barData = useMemo(
    () =>
      cats
        .filter((c) => (c.fills_paper_window || 0) > 0)
        .slice(0, 20)
        .map((c) => ({ category: c.name, fills: c.fills_paper_window || 0 })),
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

      {!hasToken && (
        <div className="text-xs text-muted">
          Admin token not set —{" "}
          <Link href="/" className="underline">paste it on the home page</Link>{" "}
          to enable mutations. Read-only metrics are visible without it.
        </div>
      )}

      {error && (
        <div className="card border border-danger/40 text-danger text-sm">
          failed to load metrics: {error instanceof Error ? error.message : String(error)}
        </div>
      )}

      <section className="grid grid-cols-2 md:grid-cols-5 gap-4">
        <Stat k="Signals"      v={fmtInt(totals.signals)} />
        <Stat k="Fills"        v={fmtInt(totals.fills)} />
        <Stat k="Settles"      v={fmtInt(totals.settles)} />
        <Stat
          k="Realized PnL"
          v={totals.hasRealized ? `$${totals.realized.toFixed(2)}` : "—"}
          tone={totals.hasRealized ? toneFor(totals.realized) : ""}
        />
        <Stat k="Open positions" v={fmtInt(totals.open)} />
      </section>

      <section className="card">
        <div className="flex items-baseline justify-between mb-3">
          <h2 className="text-sm k">Per-category ({win})</h2>
          <span className="text-xs text-muted">
            {cats.length} {cats.length === 1 ? "category" : "categories"} · refresh 15s
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
                  <th className="text-right p-2">Open</th>
                  <th className="text-right p-2">Cost basis</th>
                  <th className="text-right p-2">Win rate est.</th>
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
                          href="/settings#categories"
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
                      <td className="p-2 text-right tabular-nums">
                        {fmtInt(c.signals_window)}
                      </td>
                      <td
                        className={`p-2 text-right tabular-nums ${
                          passRate != null && passRate > 0.5 ? "text-accent" : ""
                        }`}
                      >
                        {passRate != null ? `${(passRate * 100).toFixed(1)}%` : "—"}
                      </td>
                      <td className="p-2 text-right tabular-nums">
                        {fmtInt(c.fills_paper_window)}
                      </td>
                      <td className="p-2 text-right tabular-nums">
                        {fmtInt(c.settles_window)}
                      </td>
                      <td className="p-2 text-right tabular-nums">
                        {fmtInt(c.open_positions)}
                      </td>
                      <td className="p-2 text-right tabular-nums">
                        ${Number(c.cost_basis_usdc ?? 0).toFixed(2)}
                      </td>
                      <td
                        className={`p-2 text-right tabular-nums ${
                          c.win_rate_estimate != null && c.win_rate_estimate > 0.5
                            ? "text-accent"
                            : ""
                        }`}
                      >
                        {c.win_rate_estimate != null
                          ? `${(c.win_rate_estimate * 100).toFixed(1)}%`
                          : "n/a"}
                      </td>
                      <td className="p-2 text-right tabular-nums">
                        {fmtInt(c.active_wallets)}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <section className="card">
        <h2 className="text-sm k mb-2">Fills per category ({win})</h2>
        {barData.length === 0 ? (
          <div className="text-xs text-muted py-4">no fills in this window</div>
        ) : (
          <div style={{ height: 280 }}>
            <ResponsiveBar
              data={barData}
              keys={["fills"]}
              indexBy="category"
              margin={{ top: 8, right: 16, bottom: 64, left: 48 }}
              padding={0.3}
              colors={["#22d39e"]}
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

function toneFor(n: number): string {
  if (n > 0) return "text-accent";
  if (n < 0) return "text-danger";
  return "";
}
