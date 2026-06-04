"use client";
import useSWR from "swr";
import { fetcher } from "@/lib/api";

type W = {
  address: string; label: string | null; category: string | null; active: boolean;
  pnl_usdc: number | null; realized_pnl_usdc: number | null; roi: number | null;
  win_rate: number | null; sharpe: number | null;
  trade_count: number | null; avg_trade_size: number | null;
  n_decisions: number | null; n_open_positions: number | null;
  n_total_positions: number | null; n_trade_days: number | null;
};

const dash = (x: any) => (x === null || x === undefined || Number.isNaN(x) ? "—" : x);
const pct  = (x: number | null | undefined, digits = 1) =>
  x === null || x === undefined ? "—" : (x * 100).toFixed(digits) + "%";
const num  = (x: number | null | undefined, digits = 2) =>
  x === null || x === undefined ? "—" : x.toFixed(digits);
const usd  = (x: number | null | undefined) =>
  x === null || x === undefined ? "—" : `$${Math.round(x).toLocaleString()}`;

export default function Wallets() {
  const { data } = useSWR<W[]>("/wallets?limit=300", fetcher, { refreshInterval: 60000 });
  const rows = data ?? [];

  return (
    <div className="space-y-4">
      <header className="flex items-baseline gap-4">
        <h1 className="text-2xl font-bold">Tracked wallets</h1>
        <span className="text-muted text-sm">
          {rows.length} active · sorted by realised PnL
        </span>
      </header>

      <details className="text-muted text-xs max-w-3xl">
        <summary className="cursor-pointer text-text">
          Why some wallets show huge mark-to-market losses despite big realised gains
        </summary>
        <div className="mt-2 space-y-2">
          <p>
            <span className="text-text">Realised PnL</span> is the honest one — cash
            actually collected from closed bets. We sort by this.
          </p>
          <p>
            <span className="text-text">M-to-M</span> sums Polymarket's
            <code className="px-1">cashPnl</code> across <em>every</em> position the wallet
            still holds. It includes <strong>losing tickets that already resolved to $0
            but were never explicitly redeemed</strong> — Polymarket keeps them on the
            wallet as "open" positions with cashPnl = −initialValue. Sport-betting
            wallets often accumulate millions in such ghost losses while being
            net-profitable in realised cash.
          </p>
          <p>
            <span className="text-text">Win-rate</span> only counts positions with
            <code className="px-1">realizedPnl ≠ 0</code>; <span className="text-text">"—"</span>
            means fewer than 5. <span className="text-text">Sharpe</span> needs
            ≥ 5 trading days.
          </p>
        </div>
      </details>

      <div className="card overflow-x-auto">
        <table className="w-full text-sm">
          <thead className="text-muted text-xs uppercase">
            <tr>
              <th className="text-left p-2">Address</th>
              <th className="text-left p-2">Category</th>
              <th className="text-right p-2">Realised PnL</th>
              <th className="text-right p-2" title="cashPnl across all positions — inflated by un-redeemed losing tickets, see explainer above">M-to-M *</th>
              <th className="text-right p-2">ROI</th>
              <th className="text-right p-2">Win-Rate</th>
              <th className="text-right p-2" title="number of positions with realised PnL ≠ 0">Dec</th>
              <th className="text-right p-2">Sharpe</th>
              <th className="text-right p-2">Days</th>
              <th className="text-right p-2">Open</th>
              <th className="text-right p-2">Trades</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(r => (
              <tr key={r.address} className="border-t border-white/5">
                <td className="p-2 font-mono text-xs">
                  <a
                    href={`https://polymarketanalytics.com/address/${r.address}`}
                    target="_blank" rel="noreferrer"
                    className="hover:text-accent">
                    {r.address.slice(0, 10)}…
                  </a>
                </td>
                <td className="p-2">{dash(r.category)}</td>
                <td className={`p-2 text-right ${posneg(r.realized_pnl_usdc)}`}>
                  {usd(r.realized_pnl_usdc)}
                </td>
                <td className={`p-2 text-right ${posneg(r.pnl_usdc)}`}>
                  {usd(r.pnl_usdc)}
                </td>
                <td className="p-2 text-right">{pct(r.roi)}</td>
                <td className="p-2 text-right">{pct(r.win_rate)}</td>
                <td className="p-2 text-right">{dash(r.n_decisions)}</td>
                <td className="p-2 text-right">{num(r.sharpe)}</td>
                <td className="p-2 text-right">{dash(r.n_trade_days)}</td>
                <td className="p-2 text-right">{dash(r.n_open_positions)}</td>
                <td className="p-2 text-right">{dash(r.trade_count)}</td>
              </tr>
            ))}
            {!rows.length && (
              <tr><td colSpan={11} className="p-6 text-center text-muted">no wallets yet</td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function posneg(x: number | null | undefined): string {
  if (x === null || x === undefined) return "";
  if (x > 0) return "text-accent";
  if (x < 0) return "text-danger";
  return "";
}
