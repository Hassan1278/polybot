"use client";
import useSWR from "swr";
import { fetcher } from "@/lib/api";
import { ResponsiveHeatMap } from "@nivo/heatmap";

type H = { labels: string[]; addresses: string[]; matrix: number[][] };

export default function Heatmap() {
  const { data } = useSWR<H>("/correlation/heatmap?days=7", fetcher, { refreshInterval: 60000 });
  const labels = data?.labels ?? [];
  const matrix = data?.matrix ?? [];

  const series = labels.map((l, i) => ({
    id: l,
    data: labels.map((c, j) => ({ x: c, y: matrix[i]?.[j] ?? 0 })),
  }));

  return (
    <div className="space-y-4">
      <h1 className="text-2xl font-bold">Wallet correlation (Jaccard, 7 d)</h1>
      <p className="text-muted text-sm">
        Two wallets correlate when they trade the same markets. High cells = candidates for
        the "wallets-do-the-same-thing" gate.
      </p>
      <div className="card" style={{ height: 640 }}>
        <ResponsiveHeatMap
          data={series as any}
          margin={{ top: 60, right: 40, bottom: 40, left: 80 }}
          valueFormat=">-.2f"
          axisTop={{ tickRotation: -60 }}
          colors={{ type: "sequential", scheme: "viridis", minValue: 0, maxValue: 1 }}
          theme={{ background: "transparent", text: { fill: "#7a7a85" } }}
        />
      </div>
    </div>
  );
}
