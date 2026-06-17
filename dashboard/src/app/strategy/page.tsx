"use client";

import useSWR from "swr";
import Link from "next/link";
import { fetcher } from "@/lib/api";
import { adminApi } from "@/lib/admin";
import { useAuthStatus } from "@/lib/auth-status";

type SettingsPayload = {
  mode: "paper" | "live";
  effective: {
    risk: Record<string, any>;
    categories: Record<string, any>;
    gates: { gates?: Array<{ name: string; type: string; enabled: boolean; params: Record<string, any> }> };
  };
};

type MetricsPayload = {
  totals?: {
    realized_lifetime_usdc: number;
    unrealized_mtm_usdc: number | null;
    wins_closed: number;
    losses_closed: number;
    win_rate_closed: number | null;
    signals_window: number;
    fills_paper_window: number;
    settles_window: number;
  };
};

/**
 * /strategy — explains the active signal-generation strategy in plain
 * English plus shows the actual parameters the bot is running RIGHT NOW
 * (so the docs can't drift from runtime config).
 *
 * The text below describes the default smart_money_mirror strategy.
 * When you swap strategies via SIGNAL_STRATEGY env, the headline + ID
 * update automatically; the body text remains accurate as long as the
 * strategy still works on tracked-wallet clusters. A future strategy
 * with a different mental model should ship its own /strategy/<name>
 * page or extend this one with a strategy switcher.
 */
export default function StrategyPage() {
  // /admin/settings/ requires auth — fetch via adminApi (which sends
  // session OR admin token). Previously the page used the unauthenticated
  // `fetcher`, got a 401, and the loading state stuck forever showing all
  // "—" placeholders. The "/public fallback" comment described code that
  // never existed; dropped.
  const authed = useAuthStatus();
  const { data: settings } = useSWR<SettingsPayload>(
    authed ? "/admin/settings" : null,
    (p: string) => adminApi.get(p) as Promise<SettingsPayload>,
    { refreshInterval: 30_000 },
  );
  const { data: metrics } = useSWR<MetricsPayload>(
    "/metrics/categories?window=30d&include_marks=false",
    fetcher,
    { refreshInterval: 30_000 },
  );

  const gates = settings?.effective?.gates?.gates ?? [];
  const cats = settings?.effective?.categories ?? {};
  const enabledCats = Object.entries(cats).filter(([, c]: any) => c?.enabled);
  const sizing = settings?.effective?.risk?.sizing ?? {};
  const position = settings?.effective?.risk?.position ?? {};
  const correlation = gates.find((g) => g.name === "correlation_score");
  const walletQuality = gates.find((g) => g.name === "wallet_quality");
  const totals = metrics?.totals;

  return (
    <div className="space-y-6">
      <header className="flex items-baseline gap-4 flex-wrap">
        <h1 className="text-2xl font-bold">Strategy</h1>
        <span className="k">smart_money_mirror</span>
        {settings?.mode && <span className="k">{settings.mode} mode</span>}
      </header>

      <section className="card">
        <h2 className="text-sm k mb-2">What this bot actually does</h2>
        <p className="text-sm space-y-2 leading-relaxed">
          The Polybot strategy is called{" "}
          <span className="font-mono text-accent">smart_money_mirror</span>. The thesis: a handful
          of Polymarket wallets consistently make money. If <strong>several of them open the
          same bet within minutes of each other</strong>, that&apos;s a strong directional signal
          we want to mirror. Lone whales aren&apos;t enough; clusters are.
        </p>
      </section>

      <section className="card">
        <h2 className="text-sm k mb-2">The pipeline (step by step)</h2>
        <ol className="text-sm space-y-3 list-decimal pl-5 leading-relaxed">
          <li>
            <strong>Discovery.</strong> A scraper ranks active Polymarket wallets by realised PnL
            in each category, keeping the top {walletQuality?.params?.window ?? "30d"} performers
            (min win-rate{" "}
            <span className="font-mono">{walletQuality?.params?.min_avg_win_rate ?? "0.50"}</span>
            ). The roster refreshes every 30 min.
          </li>
          <li>
            <strong>Ingestion.</strong> Every 15 min we poll the Polymarket data API for fresh
            trades by every tracked wallet, dedup against a watermark, write them to the{" "}
            <code>trades</code> hypertable, and publish a <code>trade:new</code> event to Redis.
          </li>
          <li>
            <strong>Clustering.</strong> The signal engine wakes on each new trade and groups
            recent prints by <code>(market_id, outcome, side)</code> within a rolling{" "}
            {correlation?.params?.min_score
              ? ""
              : "30-min"}{" "}
            window. A cluster scores by{" "}
            <span className="text-muted">
              wallet-count × notional × time-decay (300 s half-life)
            </span>
            . Clusters with fewer than{" "}
            <span className="font-mono">
              {correlation?.params?.min_wallets ?? 3}
            </span>{" "}
            distinct wallets are dropped.
          </li>
          <li>
            <strong>Gate chain.</strong> Each surviving cluster passes through{" "}
            {gates.length} gates: category match, wallet quality, liquidity, risk-reward,
            timeframe, correlation score, cooldown, opposing smart money. Any HARD gate fail
            kills the signal. SOFT gates penalise the score.
          </li>
          <li>
            <strong>Sizing.</strong> Position size is a sigmoid of the cluster score —{" "}
            <span className="text-muted">
              base ${sizing.base_usdc ?? "—"} to max ${sizing.max_usdc ?? "—"} centred at{" "}
              {sizing.anchor ?? "—"} with steepness {sizing.steepness ?? "—"}
            </span>
            .
          </li>
          <li>
            <strong>Execution.</strong> Paper mode walks the live CLOB book + records a Fill.
            Live mode signs an EIP-712 order with the encrypted wallet and posts to Polymarket.
            Both paths apply the same per-market, per-category, daily-loss caps before placing.
          </li>
          <li>
            <strong>Resolution.</strong> Every 10 min a watcher flips markets whose Polymarket
            end_date has passed and re-checks resolution. Once flipped, the executor settles
            paper positions at $0 / $1 and updates realised PnL.
          </li>
        </ol>
      </section>

      <section className="card">
        <h2 className="text-sm k mb-2">Live parameters</h2>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          <Stat k="Min wallets/cluster" v={String(correlation?.params?.min_wallets ?? "—")} />
          <Stat k="Min correlation score" v={String(correlation?.params?.min_score ?? "—")} />
          <Stat k="Wallet min win-rate" v={String(walletQuality?.params?.min_avg_win_rate ?? "—")} />
          <Stat k="Position cap" v={`$${position.max_position_usdc ?? "—"}`} />
          <Stat k="Max open positions" v={String(position.max_open_positions ?? "—")} />
          <Stat k="Per-category cap" v={`$${position.max_per_category_usdc ?? "—"}`} />
          <Stat k="Sizing range" v={`$${sizing.base_usdc ?? "—"} → $${sizing.max_usdc ?? "—"}`} />
          <Stat k="Active categories" v={String(enabledCats.length)} />
        </div>
      </section>

      <section className="card">
        <h2 className="text-sm k mb-2">Lifetime performance (30 d)</h2>
        {!totals ? (
          <div className="text-xs text-muted">loading…</div>
        ) : (
          <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
            <Stat
              k="Realised PnL"
              v={fmtUsd(totals.realized_lifetime_usdc)}
              tone={tone(totals.realized_lifetime_usdc)}
            />
            <Stat
              k="Unrealised MTM"
              v={fmtUsd(totals.unrealized_mtm_usdc)}
              tone={tone(totals.unrealized_mtm_usdc)}
            />
            <Stat
              k="Win rate (closed)"
              v={
                totals.win_rate_closed != null
                  ? `${(totals.win_rate_closed * 100).toFixed(1)}%`
                  : "—"
              }
              tone={
                totals.win_rate_closed != null && totals.win_rate_closed > 0.5
                  ? "text-accent"
                  : totals.win_rate_closed != null && totals.win_rate_closed < 0.5
                  ? "text-danger"
                  : ""
              }
            />
            <Stat
              k="W / L (closed)"
              v={`${totals.wins_closed}W / ${totals.losses_closed}L`}
            />
            <Stat k="Settles 30d" v={String(totals.settles_window)} />
          </div>
        )}
        <p className="text-xs text-muted mt-3">
          See <Link href="/metrics" className="text-accent underline">/metrics</Link> for the
          per-category breakdown.
        </p>
      </section>

      <section className="card">
        <h2 className="text-sm k mb-2">Swap the strategy</h2>
        <p className="text-sm text-muted leading-relaxed">
          The strategy is pluggable. Set{" "}
          <code className="font-mono text-accent">SIGNAL_STRATEGY</code> in <code>.env</code>{" "}
          and restart the signals service. Available out of the box:
        </p>
        <ul className="text-sm mt-3 space-y-2">
          <li>
            <span className="font-mono text-accent">smart_money_mirror</span> (default) — the
            cluster-detection pipeline described above.
          </li>
          <li>
            <span className="font-mono text-accent">whale_follower</span> — single-address
            tracker. Requires <code className="font-mono">WHALE_FOLLOWER_ADDRESS</code>. Useful
            for mirroring one specific wallet without the cluster filter.
          </li>
        </ul>
        <p className="text-sm text-muted mt-3 leading-relaxed">
          To add a new strategy: drop a file in{" "}
          <code>services/signals/strategies/</code> that implements the{" "}
          <code className="font-mono">SignalStrategy</code> protocol, register it in the package{" "}
          <code>_REGISTRY</code>. The gate chain, executor, and dashboard adapt automatically.
        </p>
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

function fmtUsd(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "—";
  const sign = n < 0 ? "-" : "";
  return `${sign}$${Math.abs(n).toFixed(2)}`;
}

function tone(n: number | null | undefined): string {
  if (n == null || Number.isNaN(n)) return "";
  if (n > 0) return "text-accent";
  if (n < 0) return "text-danger";
  return "";
}
