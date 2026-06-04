"use client";
import { openWs } from "@/lib/api";
import { useEffect, useRef, useState } from "react";

type T = { wallet: string; market_id: string; side: string; size: number; price: number; ts: number };

export default function Trades() {
  const [trades, setTrades] = useState<T[]>([]);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const ws = openWs((m) => {
      if (m.channel !== "trade:new") return;
      setTrades(prev => [m.data as T, ...prev].slice(0, 200));
    });
    return () => ws.close();
  }, []);

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">Live trades (tracked wallets)</h1>
      <div ref={ref} className="card overflow-y-auto" style={{ maxHeight: 700 }}>
        <table className="w-full text-sm">
          <thead className="text-muted text-xs sticky top-0 bg-panel">
            <tr>
              <th className="text-left p-2">Time</th>
              <th className="text-left p-2">Wallet</th>
              <th className="text-left p-2">Market</th>
              <th className="text-left p-2">Side</th>
              <th className="text-right p-2">Size</th>
              <th className="text-right p-2">Price</th>
            </tr>
          </thead>
          <tbody>
            {trades.map((t, i) => (
              <tr key={i} className="border-t border-white/5">
                <td className="p-2 text-xs">{new Date(t.ts * 1000).toLocaleTimeString()}</td>
                <td className="p-2 font-mono text-xs">{t.wallet.slice(0, 10)}…</td>
                <td className="p-2 font-mono text-xs">{t.market_id?.slice(0, 14)}…</td>
                <td className={`p-2 ${t.side === "BUY" ? "text-accent" : "text-danger"}`}>{t.side}</td>
                <td className="p-2 text-right">{t.size?.toFixed(2)}</td>
                <td className="p-2 text-right">{t.price?.toFixed(3)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
