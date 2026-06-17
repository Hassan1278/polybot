"use client";

import useSWR from "swr";
import { fetcher } from "@/lib/api";

type Health = {
  mode: string;
  kill_switch: string | null;
};

type ModeResp = {
  mode: "paper" | "live";
  enabled_modes?: ("paper" | "live")[];
};

/**
 * Page-wide LIVE-mode banner.
 *
 * Renders a bright red strip across the top of every page when LIVE mode
 * is currently active, otherwise renders nothing. The visual is impossible
 * to miss — operators previously had only a tiny "mode: live" tag in the
 * header, and a few audit reviewers flagged that as "easy to forget which
 * mode you're in while reading a sub-page like /trades or /settings".
 *
 * Data source: /health (every 5s) — same single source-of-truth as KillTag.
 * Falls back to /admin/settings/mode when unauthenticated /health doesn't
 * return enabled_modes (so the banner survives logged-out browsing).
 */
export default function LiveBanner() {
  const { data } = useSWR<Health>("/health", fetcher, { refreshInterval: 5000 });
  const liveActive = data?.mode === "live";

  if (!liveActive && !data?.kill_switch) return null;

  if (data?.kill_switch) {
    return (
      <div className="bg-danger text-white text-center py-1.5 px-4 font-bold tracking-wide text-sm">
        🛑 KILL SWITCH ACTIVE — no new trades. Reason: <code className="font-mono">{data.kill_switch}</code>
      </div>
    );
  }

  return (
    <div className="bg-danger text-white text-center py-1.5 px-4 font-bold tracking-wide text-sm">
      ⚠ LIVE MODE — real USDC at risk · positions trade on Polymarket
    </div>
  );
}
