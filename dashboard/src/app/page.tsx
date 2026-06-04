"use client";
import useSWR from "swr";
import { fetcher, postAdmin } from "@/lib/api";
import { ResponsiveLine } from "@nivo/line";
import { useState } from "react";

type Pnl = { ts: string; equity: number; realized: number; unrealized: number; open: number };
type Health = { ok: boolean; mode: string; can_sign: boolean; kill_switch: string | null };

export default function Home() {
  const { data: hh } = useSWR<Health>("/health", fetcher, { refreshInterval: 5000 });
  const { data: pnl } = useSWR<Pnl[]>("/pnl?mode=paper&limit=720", fetcher, { refreshInterval: 30000 });
  const [token, setToken] = useState("");
  const [busy, setBusy] = useState(false);

  const equityCurve = (pnl || []).map(p => ({ x: new Date(p.ts).getTime(), y: p.equity }));
  const last = pnl?.[pnl.length - 1];

  return (
    <div className="space-y-6">
      <header className="flex items-baseline gap-6">
        <h1 className="text-2xl font-bold">Overview</h1>
        <span className="k">{hh?.mode ?? "?"} mode</span>
        {hh?.kill_switch
          ? <span className="text-danger text-sm">KILLED: {hh.kill_switch}</span>
          : <span className="text-accent text-sm">live</span>}
      </header>

      <section className="grid grid-cols-4 gap-4">
        <Stat k="Equity"     v={last ? `$${last.equity.toFixed(2)}` : "—"} />
        <Stat k="Realized"   v={last ? `$${last.realized.toFixed(2)}` : "—"} />
        <Stat k="Unrealized" v={last ? `$${last.unrealized.toFixed(2)}` : "—"} />
        <Stat k="Open"       v={last ? String(last.open) : "—"} />
      </section>

      <section className="card">
        <h2 className="text-sm k mb-2">Equity curve — paper mode</h2>
        <div style={{ height: 320 }}>
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
        </div>
      </section>

      <section className="card">
        <h2 className="text-sm k mb-2">Kill switch</h2>
        <div className="flex gap-2 items-center">
          <input type="password" placeholder="admin token" value={token}
                 onChange={e => setToken(e.target.value)}
                 className="bg-black/40 border border-white/10 rounded px-3 py-2 text-sm w-72"/>
          <button disabled={busy || !token} className="bg-danger text-white px-3 py-2 rounded text-sm"
            onClick={async () => { setBusy(true); try { await postAdmin("/admin/kill?reason=dashboard", token); } finally { setBusy(false); } }}>
            KILL
          </button>
          <button disabled={busy || !token} className="bg-accent text-black px-3 py-2 rounded text-sm"
            onClick={async () => { setBusy(true); try { await postAdmin("/admin/kill/clear?by=dashboard", token); } finally { setBusy(false); } }}>
            Clear
          </button>
        </div>
      </section>
    </div>
  );
}

function Stat({ k, v }: { k: string; v: string }) {
  return <div className="card"><div className="k">{k}</div><div className="v">{v}</div></div>;
}
