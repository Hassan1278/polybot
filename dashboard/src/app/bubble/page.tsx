"use client";
import useSWR from "swr";
import { fetcher } from "@/lib/api";
import { ResponsiveScatterPlot } from "@nivo/scatterplot";

type Node = {
  id: string; address: string; category: string;
  win_rate: number | null; sharpe: number | null;
  trade_count: number; pnl: number; realized_pnl: number; roi: number;
  n_decisions: number; n_open_positions: number;
};
type B = { nodes: Node[] };

const COLOURS: Record<string, string> = {
  politics: "#ff5c6f", sports: "#22d39e", crypto: "#f5a623",
  macro: "#5cc8ff", entertainment: "#c084fc", other: "#7a7a85",
};

export default function BubbleMap() {
  const { data } = useSWR<B>("/correlation/bubble", fetcher, { refreshInterval: 30000 });
  const all = data?.nodes ?? [];

  // Only show wallets with both win_rate AND sharpe so the axes are honest.
  const plottable = all.filter(n => n.win_rate !== null && n.sharpe !== null);
  const hidden = all.length - plottable.length;

  const byCat: Record<string, any[]> = {};
  for (const n of plottable) {
    (byCat[n.category] ??= []).push({
      x: n.win_rate,
      y: n.sharpe,
      r: Math.max(2, Math.sqrt(n.trade_count || 1)),
      addr: n.address,
      n_decisions: n.n_decisions,
      n_open: n.n_open_positions,
      realized: n.realized_pnl,
      pnl: n.pnl,
      roi: n.roi,
    });
  }
  const series = Object.entries(byCat).map(([id, data]) => ({ id, data }));

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">Bubble Map</h1>
      <p className="text-muted text-sm">
        X: win-rate (realised) · Y: Sharpe · radius: √trade_count · colour: category
      </p>
      {hidden > 0 && (
        <p className="text-muted text-xs">
          {hidden} of {all.length} wallets hidden (no win-rate or Sharpe yet — need ≥ 5
          realised decisions / ≥ 5 trading days). They still appear in the Wallets tab.
        </p>
      )}
      <div className="card" style={{ height: 620 }}>
        {plottable.length === 0 ? (
          <div className="h-full flex items-center justify-center text-muted text-sm">
            no plottable wallets yet — wait for the next stats refresh
          </div>
        ) : (
          <ResponsiveScatterPlot
            data={series}
            margin={{ top: 16, right: 140, bottom: 60, left: 60 }}
            xScale={{ type: "linear", min: 0, max: 1 }}
            yScale={{ type: "linear", min: "auto", max: "auto" }}
            axisBottom={{ legend: "Win rate", legendOffset: 40 }}
            axisLeft={{ legend: "Sharpe", legendOffset: -50 }}
            nodeSize={(d: any) => Math.max(4, Math.min(48, d.data.r * 3))}
            colors={({ serieId }) => COLOURS[serieId as string] ?? "#7a7a85"}
            theme={{ background: "transparent",
                     text: { fill: "#7a7a85" },
                     grid: { line: { stroke: "#1c1c25" } } }}
            tooltip={({ node }: any) => (
              <div className="bg-panel border border-white/10 p-2 rounded text-xs">
                <div className="font-mono">{node.data.addr.slice(0, 10)}…</div>
                <div>wr {(node.data.x * 100).toFixed(1)}% · sharpe {node.data.y.toFixed(2)}</div>
                <div>realised ${Math.round(node.data.realized).toLocaleString()} · roi {(node.data.roi * 100).toFixed(1)}%</div>
                <div>decisions {node.data.n_decisions} · open {node.data.n_open}</div>
              </div>
            )}
            legends={[{ anchor: "top-right", direction: "column", translateX: 130,
                        itemWidth: 100, itemHeight: 18, symbolSize: 12 }]}
          />
        )}
      </div>
    </div>
  );
}
