"use client";

/**
 * ModeTab — paper/live PARALLEL mode toggles.
 *
 * The bot supports both modes running concurrently: every gate-passing
 * signal produces one Fill row per active mode, each with its own risk
 * preflight + caps. This lets the operator keep a paper shadow running
 * while live experiments with real money.
 *
 * Enabling live still goes through ConfirmModal + a server-issued
 * X-Live-Confirm HMAC + live-readiness pre-checks (kill switch off,
 * wallet credential present, encryption key configured). Disabling
 * live is unrestricted (it always makes the bot safer).
 */

import { useState } from "react";
import useSWR from "swr";
import { API } from "@/lib/api";
import { adminApi, getAdminToken } from "@/lib/admin";
import { useAuthStatus } from "@/lib/auth-status";
import { getSessionToken } from "@/lib/wallet";
import ConfirmModal from "@/components/ConfirmModal";

type Mode = "paper" | "live";

type ModeResp = { mode: Mode; enabled_modes?: Mode[] };

type RiskShape = {
  max_position_usdc?: number;
  max_open_positions?: number;
  max_per_category_usdc?: number;
  drawdown?: { max_daily_loss_usdc?: number };
};

type SettingsResp = {
  mode: Mode;
  effective: {
    risk: RiskShape;
    categories?: unknown;
    gates?: unknown;
  };
  overrides?: unknown;
  baseline?: { risk?: RiskShape };
};

function fmtUsdc(v: number | undefined): string {
  if (v === undefined || v === null || Number.isNaN(v)) return "—";
  return `$${Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
}

function fmtInt(v: number | undefined): string {
  if (v === undefined || v === null || Number.isNaN(v)) return "—";
  return Number(v).toLocaleString();
}

export default function ModeTab() {
  const authed = useAuthStatus();

  const modeSwr = useSWR<ModeResp>(
    authed ? "/admin/settings/mode" : null,
    (path: string) => adminApi.get(path) as Promise<ModeResp>,
    { refreshInterval: 5000 },
  );
  const settingsSwr = useSWR<SettingsResp>(
    authed ? "/admin/settings" : null,
    (path: string) => adminApi.get(path) as Promise<SettingsResp>,
    { refreshInterval: 15000 },
  );

  const [confirmOpen, setConfirmOpen] = useState(false);
  const [confirmToken, setConfirmToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  if (!authed) {
    return (
      <div className="card space-y-2">
        <h2 className="text-lg font-bold">Mode</h2>
        <p className="text-sm text-muted">
          Not signed in. Click <span className="text-accent">Connect Wallet</span>{" "}
          in the header, or paste an admin token in the kill-switch widget on
          the <a href="/" className="text-accent underline">home page</a>.
        </p>
      </div>
    );
  }

  const currentMode: Mode | null = modeSwr.data?.mode ?? null;
  const enabledModes = new Set<Mode>(modeSwr.data?.enabled_modes ?? (currentMode ? [currentMode] : []));
  const paperOn = enabledModes.has("paper");
  const liveOn = enabledModes.has("live");

  const patchModes = async (patch: { paper?: boolean; live?: boolean }, liveConfirm?: string) => {
    setErr(null);
    const sessionTok = getSessionToken();
    const adminTok = getAdminToken();
    if (!sessionTok && !adminTok) {
      setErr("Not signed in — connect wallet or paste admin token first.");
      return false;
    }
    setBusy(true);
    try {
      const headers: Record<string, string> = { "Content-Type": "application/json" };
      if (sessionTok) headers["X-Session-Token"] = sessionTok;
      if (adminTok) headers["X-Admin-Token"] = adminTok;
      if (liveConfirm) headers["X-Live-Confirm"] = liveConfirm;
      const r = await fetch(`${API}/admin/settings/mode/enabled`, {
        method: "PATCH",
        headers,
        body: JSON.stringify(patch),
      });
      if (r.status === 403) {
        setConfirmToken("");
        setErr("live-confirm expired or rejected — try the live toggle again.");
        return false;
      }
      if (!r.ok) {
        const text = await r.text();
        throw new Error(`PATCH /mode/enabled ${r.status}: ${text.slice(0, 200)}`);
      }
      await modeSwr.mutate();
      await settingsSwr.mutate();
      return true;
    } catch (e: any) {
      setErr(String(e?.message ?? e));
      return false;
    } finally {
      setBusy(false);
    }
  };

  const togglePaper = async () => {
    if (paperOn && !liveOn) {
      setErr("Can't disable paper while live is also off — use the kill switch to pause everything.");
      return;
    }
    await patchModes({ paper: !paperOn });
  };

  const toggleLive = async () => {
    if (liveOn) {
      // Disabling live is unrestricted (it makes the bot SAFER).
      if (!paperOn) {
        setErr("Re-enable paper before disabling live — the bot needs at least one mode.");
        return;
      }
      await patchModes({ live: false });
      return;
    }
    // Enabling live → confirm modal + live-challenge token.
    setErr(null);
    setConfirmOpen(true);
  };

  const confirmEnableLive = async () => {
    setErr(null);
    setBusy(true);
    try {
      let tok = confirmToken.trim();
      if (!tok) {
        try {
          const challenge = await adminApi.get("/admin/settings/mode/live-challenge") as { confirm_token: string };
          tok = challenge?.confirm_token ?? "";
        } catch (e) {
          setErr(`failed to fetch live challenge: ${e instanceof Error ? e.message : String(e)}`);
          return;
        }
      }
      if (!tok) {
        setErr("could not obtain live-confirm token");
        return;
      }
      const ok = await patchModes({ live: true }, tok);
      if (ok) {
        setConfirmOpen(false);
        setConfirmToken("");
      }
    } finally {
      setBusy(false);
    }
  };

  const eff = settingsSwr.data?.effective?.risk ?? {};
  const base = settingsSwr.data?.baseline?.risk ?? {};

  return (
    <div className="space-y-4">
      <div className="card space-y-3">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-bold">Active modes</h2>
          {modeSwr.error ? (
            <span className="text-xs text-danger">
              {String(modeSwr.error.message ?? modeSwr.error)}
            </span>
          ) : null}
        </div>

        <p className="text-xs text-muted">
          Each enabled mode runs in PARALLEL. Every gate-passing signal produces
          one Fill row per active mode, each with its own risk preflight. Run
          paper alongside live to keep a shadow control group while the bot
          handles real USDC.
        </p>

        <div className="grid grid-cols-2 gap-3">
          <ModeChip
            label="PAPER"
            on={paperOn}
            busy={busy}
            onToggle={togglePaper}
            tone="accent"
            description="Simulated fills against the live orderbook. No USDC at risk."
          />
          <ModeChip
            label="LIVE"
            on={liveOn}
            busy={busy}
            onToggle={toggleLive}
            tone="danger"
            description="EIP-712 signed orders posted to Polymarket. Real USDC."
          />
        </div>

        {err ? (
          <div className="text-sm text-danger border border-danger/40 rounded px-2 py-1 mt-2">
            {err}
          </div>
        ) : null}
      </div>

      <div className="card space-y-3">
        <div className="flex items-center justify-between">
          <h3 className="font-semibold">Effective risk</h3>
          {settingsSwr.error ? (
            <span className="text-xs text-danger">
              {String(settingsSwr.error.message ?? settingsSwr.error)}
            </span>
          ) : null}
        </div>
        <div className="grid grid-cols-4 gap-3">
          <RiskStat
            label="max position"
            current={fmtUsdc(eff.max_position_usdc)}
            baseline={fmtUsdc(base.max_position_usdc)}
          />
          <RiskStat
            label="max open positions"
            current={fmtInt(eff.max_open_positions)}
            baseline={fmtInt(base.max_open_positions)}
          />
          <RiskStat
            label="max per category"
            current={fmtUsdc(eff.max_per_category_usdc)}
            baseline={fmtUsdc(base.max_per_category_usdc)}
          />
          <RiskStat
            label="max daily loss"
            current={fmtUsdc(eff.drawdown?.max_daily_loss_usdc)}
            baseline={fmtUsdc(base.drawdown?.max_daily_loss_usdc)}
          />
        </div>
        <p className="text-xs text-muted">
          "current" = effective values for the active{" "}
          <span className="font-mono">{currentMode ?? "?"}</span> mode.
          "baseline" = the unmodified mode default (overrides cleared).
        </p>
      </div>

      <ConfirmModal
        open={confirmOpen}
        title="Enable LIVE — real USDC at risk"
        body={
          "Live mode places REAL orders on Polymarket with REAL USDC.\n\n" +
          "The bot already verified your wallet credential, kill-switch state,\n" +
          "and encryption key. Confirming below will fetch a fresh challenge\n" +
          "token from the server (no copy-paste of docker commands needed).\n\n" +
          "Paper mode stays enabled in parallel — you keep a shadow control\n" +
          "group running while live experiments with real money."
        }
        confirmText="LIVE"
        busy={busy}
        onCancel={() => { setConfirmOpen(false); setConfirmToken(""); setErr(null); }}
        onConfirm={confirmEnableLive}
      />
    </div>
  );
}

function ModeChip({
  label, on, busy, onToggle, tone, description,
}: {
  label: string;
  on: boolean;
  busy: boolean;
  onToggle: () => void;
  tone: "accent" | "danger";
  description: string;
}) {
  const palette = tone === "accent"
    ? { onClasses: "bg-accent/20 border-accent text-accent",
        offClasses: "bg-bg2/40 border-white/10 text-muted",
        buttonOn: "border-accent/60 text-accent hover:bg-accent/10",
        buttonOff: "border-accent/30 text-accent hover:bg-accent/10" }
    : { onClasses: "bg-danger/20 border-danger text-danger",
        offClasses: "bg-bg2/40 border-white/10 text-muted",
        buttonOn: "border-danger/60 text-danger hover:bg-danger/10",
        buttonOff: "border-danger/30 text-danger hover:bg-danger/10" };
  return (
    <div className={`rounded-lg border p-4 space-y-2 ${on ? palette.onClasses : palette.offClasses}`}>
      <div className="flex items-center justify-between">
        <span className="font-bold text-lg tracking-widest">{label}</span>
        <span className={`text-xs font-mono ${on ? "" : "text-muted"}`}>
          {on ? "ON" : "OFF"}
        </span>
      </div>
      <p className="text-xs leading-relaxed">{description}</p>
      <button
        className={`w-full px-3 py-1.5 rounded border text-sm disabled:opacity-40 disabled:cursor-not-allowed ${on ? palette.buttonOn : palette.buttonOff}`}
        onClick={onToggle}
        disabled={busy}
      >
        {busy ? "…" : on ? `Disable ${label}` : `Enable ${label}`}
      </button>
    </div>
  );
}

function RiskStat({
  label, current, baseline,
}: {
  label: string;
  current: string;
  baseline: string;
}) {
  const same = current === baseline;
  return (
    <div className="bg-bg2/40 border border-white/5 rounded p-2">
      <div className="k">{label}</div>
      <div className="v">{current}</div>
      <div className="text-[10px] text-muted mt-1">
        baseline:{" "}
        <span className={same ? "" : "text-text"}>{baseline}</span>
      </div>
    </div>
  );
}
