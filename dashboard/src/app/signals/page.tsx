"use client";
import useSWR from "swr";
import { fetcher, openWs } from "@/lib/api";
import { useEffect, useState } from "react";

type S = {
  id: number; ts: string; market_id: string; side: string; outcome: string;
  wallet_count: number; wallets: string[]; avg_win_rate: number;
  correlation_score: number; target_price: number; target_size_usdc: number;
  gate_results: Record<string, { pass: boolean; reason: string; type: string }>;
  gate_pass: boolean; executed: boolean;
};

export default function Signals() {
  const { data, mutate } = useSWR<S[]>("/signals?limit=50", fetcher, { refreshInterval: 10000 });
  const [live, setLive] = useState<number>(0);
  useEffect(() => {
    const ws = openWs((m) => { if (m.channel === "signal:new") { setLive(x => x + 1); mutate(); } });
    return () => ws.close();
  }, [mutate]);

  const rows = data ?? [];

  return (
    <div className="space-y-4">
      <header className="flex items-baseline gap-3">
        <h1 className="text-2xl font-bold">Signals</h1>
        <span className="text-xs text-muted">live events: {live}</span>
      </header>

      <div className="space-y-2">
        {rows.map(s => (
          <div key={s.id} className={`card flex flex-col gap-2 border-l-4 ${s.gate_pass ? "border-l-accent" : "border-l-danger"}`}>
            <div className="flex justify-between items-center">
              <div className="flex gap-3 items-center">
                <span className="font-mono text-xs text-muted">#{s.id}</span>
                <span className={`text-sm font-semibold ${s.side === "BUY" ? "text-accent" : "text-danger"}`}>{s.side} {s.outcome}</span>
                <span className="text-xs text-muted">{s.wallet_count} wallets · score {s.correlation_score.toFixed(2)}</span>
                <span className="text-xs text-muted">target {(s.target_price * 100).toFixed(1)}%</span>
              </div>
              <span className="text-xs text-muted">{new Date(s.ts).toLocaleString()}</span>
            </div>
            <div className="font-mono text-xs break-all text-muted">{s.market_id}</div>
            <div className="flex flex-wrap gap-1">
              {Object.entries(s.gate_results || {}).map(([k, v]) => (
                <span key={k}
                      className={`text-[10px] px-1.5 py-0.5 rounded ${v.pass ? "bg-accent/15 text-accent" : "bg-danger/15 text-danger"}`}>
                  {k}: {v.reason}
                </span>
              ))}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
