"use client";
import useSWR from "swr";
import Link from "next/link";
import { fetcher } from "@/lib/api";

type Fill = {
  id: number;
  signal_id: number | null;
  ts: string;
  mode: string;
  market_id: string;
  outcome: string;
  side: string;
  size_shares: number;
  price: number;
  notional_usdc: number;
  fee_usdc: number;
  status: string;
  venue_order_id: string | null;
  error: string | null;
};

/**
 * /fills — historical trade ledger.
 *
 * Shows every fill the bot has ever placed (paper and live mixed in one
 * table, filterable). The home page only shows OPEN positions; this page
 * shows the full trade history, including settled and rejected fills.
 *
 * Backed by GET /fills?limit=200 (no auth required — observational only).
 */
export default function FillsPage() {
  const { data, error, isLoading } = useSWR<Fill[]>("/fills?limit=200", fetcher, {
    refreshInterval: 15000,
  });

  const fills = data ?? [];
  const totalNotional = fills
    .filter(f => f.status === "filled" && f.side === "BUY")
    .reduce((s, f) => s + f.notional_usdc, 0);
  const totalFees = fills.reduce((s, f) => s + (f.fee_usdc || 0), 0);
  const byStatus = fills.reduce((acc, f) => {
    acc[f.status] = (acc[f.status] || 0) + 1;
    return acc;
  }, {} as Record<string, number>);

  return (
    <div className="space-y-6">
      <header className="flex items-baseline gap-6">
        <h1 className="text-2xl font-bold">Trade history</h1>
        <span className="k">{fills.length} recent fills · live polling 15s</span>
      </header>

      {error && (
        <section className="card" style={{ borderColor: "#ff5470" }}>
          <h2 className="text-sm k text-danger">Failed to load fills</h2>
          <p className="text-xs text-muted mt-1">{String(error)}</p>
        </section>
      )}

      <section className="grid grid-cols-4 gap-4">
        <div className="card"><div className="k">Total fills (shown)</div><div className="v">{fills.length}</div></div>
        <div className="card"><div className="k">Total BUY notional</div><div className="v">${totalNotional.toFixed(2)}</div></div>
        <div className="card"><div className="k">Total fees</div><div className="v">${totalFees.toFixed(2)}</div></div>
        <div className="card">
          <div className="k">By status</div>
          <div className="text-xs text-muted">
            {Object.entries(byStatus).map(([k, v]) => `${k}: ${v}`).join(" · ") || "—"}
          </div>
        </div>
      </section>

      <section className="card">
        <h2 className="text-sm k mb-2">All fills (newest first, capped 200)</h2>
        {isLoading && fills.length === 0 ? (
          <div className="text-xs text-muted py-2">loading…</div>
        ) : fills.length === 0 ? (
          <div className="text-xs text-muted py-2">no fills yet</div>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-sm tabular-nums">
              <thead className="text-muted text-xs uppercase">
                <tr>
                  <th className="text-left p-2">When</th>
                  <th className="text-left p-2">Mode</th>
                  <th className="text-left p-2">Status</th>
                  <th className="text-left p-2">Side</th>
                  <th className="text-left p-2">Outcome</th>
                  <th className="text-right p-2">Size</th>
                  <th className="text-right p-2">Price</th>
                  <th className="text-right p-2">Notional</th>
                  <th className="text-right p-2">Fee</th>
                  <th className="text-left p-2">Market</th>
                  <th className="text-right p-2">Signal</th>
                </tr>
              </thead>
              <tbody>
                {fills.map(f => (
                  <tr key={f.id} className="border-t border-white/5">
                    <td className="p-2 whitespace-nowrap text-xs">
                      {new Date(f.ts).toISOString().replace("T", " ").slice(0, 16)}Z
                    </td>
                    <td className="p-2 text-xs">{f.mode}</td>
                    <td className={`p-2 text-xs ${statusColor(f.status)}`}>{f.status}</td>
                    <td className={`p-2 text-xs ${sideColor(f.side)}`}>{f.side}</td>
                    <td className="p-2 max-w-[160px] truncate" title={f.outcome}>{f.outcome}</td>
                    <td className="p-2 text-right">{f.size_shares.toFixed(2)}</td>
                    <td className="p-2 text-right">{f.price.toFixed(3)}</td>
                    <td className="p-2 text-right">${f.notional_usdc.toFixed(2)}</td>
                    <td className="p-2 text-right text-muted">${(f.fee_usdc || 0).toFixed(3)}</td>
                    <td className="p-2 text-xs font-mono text-muted" title={f.market_id}>
                      {f.market_id.slice(0, 12)}…
                    </td>
                    <td className="p-2 text-right text-xs">
                      {f.signal_id ? (
                        <span className="font-mono text-muted">#{f.signal_id}</span>
                      ) : (
                        <span className="text-muted">—</span>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </section>

      <p className="text-xs text-muted">
        Tip: the home page (<Link href="/" className="text-accent underline">/</Link>) shows OPEN positions.
        This page shows EVERY fill the bot has ever made (paper+live, all statuses).
      </p>
    </div>
  );
}

function sideColor(side: string): string {
  if (side === "BUY") return "text-accent";
  if (side === "SELL") return "text-danger";
  return "text-muted";
}

function statusColor(s: string): string {
  if (s === "filled") return "text-accent";
  if (s === "settled") return "text-muted";
  if (s === "rejected") return "text-danger";
  if (s === "partial") return "text-text";
  return "text-muted";
}
