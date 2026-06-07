"use client";

/**
 * ModeTab — paper/live mode switcher.
 *
 * Paper->live transitions place real USDC at risk so they go through
 * ConfirmModal AND require an X-Live-Confirm HMAC token. The browser
 * cannot compute that token without the admin secret, so this UI
 * shows a copy-paste command for the operator to run on the API host
 * and a textarea to paste the resulting `{epoch}:{hmac}` string.
 *
 * NOTE: this is a workaround. The proper fix is a server-issued
 * challenge endpoint (GET /admin/settings/mode/live-challenge) that
 * returns a fresh token the browser can echo back. Not implemented
 * server-side yet — tracked as a follow-up.
 */

import { useState } from "react";
import useSWR from "swr";
import { API } from "@/lib/api";
import { adminApi, getAdminToken } from "@/lib/admin";
import ConfirmModal from "@/components/ConfirmModal";

type Mode = "paper" | "live";

type ModeResp = { mode: Mode };

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

const LIVE_CONFIRM_CMD =
  `docker compose exec api python -c "import hashlib,hmac,time,os; secret=open('.env').read().split('ADMIN_TOKEN=')[1].split(chr(10))[0].encode(); ts=str(int(time.time())); print(f'{ts}:{hmac.new(secret, b\\"switch-to-live:\\"+ts.encode(), hashlib.sha256).hexdigest()}')"`;

function fmtUsdc(v: number | undefined): string {
  if (v === undefined || v === null || Number.isNaN(v)) return "—";
  return `$${Number(v).toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
}

function fmtInt(v: number | undefined): string {
  if (v === undefined || v === null || Number.isNaN(v)) return "—";
  return Number(v).toLocaleString();
}

export default function ModeTab() {
  const token = typeof window !== "undefined" ? getAdminToken() : null;

  const modeSwr = useSWR<ModeResp>(
    token ? "/admin/settings/mode" : null,
    (path: string) => adminApi.get(path) as Promise<ModeResp>,
    { refreshInterval: 5000 },
  );
  const settingsSwr = useSWR<SettingsResp>(
    token ? "/admin/settings/" : null,
    (path: string) => adminApi.get(path) as Promise<SettingsResp>,
    { refreshInterval: 15000 },
  );

  const [confirmOpen, setConfirmOpen] = useState(false);
  const [confirmToken, setConfirmToken] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  if (!token) {
    return (
      <div className="card space-y-2">
        <h2 className="text-lg font-bold">Mode</h2>
        <p className="text-sm text-muted">
          Admin token not set. Open the{" "}
          <a href="/" className="text-accent underline">home page</a> and paste
          your admin token into the kill-switch widget first.
        </p>
      </div>
    );
  }

  const currentMode: Mode | null = modeSwr.data?.mode ?? null;
  const isPaper = currentMode === "paper";
  const isLive = currentMode === "live";

  const switchToPaper = async () => {
    setErr(null);
    setBusy(true);
    try {
      await adminApi.post("/admin/settings/mode", { mode: "paper" });
      await modeSwr.mutate();
      await settingsSwr.mutate();
    } catch (e: any) {
      setErr(String(e?.message ?? e));
    } finally {
      setBusy(false);
    }
  };

  const switchToLive = async () => {
    setErr(null);
    const tok = confirmToken.trim();
    if (!tok) {
      setErr("Paste a live-confirm token first.");
      return;
    }
    const adminTok = getAdminToken();
    if (!adminTok) {
      setErr("Admin token missing.");
      return;
    }
    setBusy(true);
    try {
      // adminApi can't attach a per-call custom header, so go direct to fetch
      // for this one endpoint to pass X-Live-Confirm.
      const r = await fetch(`${API}/admin/settings/mode`, {
        method: "POST",
        headers: {
          "X-Admin-Token": adminTok,
          "X-Live-Confirm": tok,
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ mode: "live" }),
      });
      if (r.status === 403) {
        // Keep modal open so the operator can paste a fresh token.
        alert("live-confirm expired/invalid, regenerate");
        return;
      }
      if (!r.ok) {
        const text = await r.text();
        throw new Error(`mode switch ${r.status}: ${text.slice(0, 200)}`);
      }
      setConfirmOpen(false);
      setConfirmToken("");
      await modeSwr.mutate();
      await settingsSwr.mutate();
    } catch (e: any) {
      setErr(String(e?.message ?? e));
    } finally {
      setBusy(false);
    }
  };

  const copyCmd = async () => {
    try {
      await navigator.clipboard.writeText(LIVE_CONFIRM_CMD);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch {
      /* clipboard blocked — operator can select manually */
    }
  };

  const eff = settingsSwr.data?.effective?.risk ?? {};
  const base = settingsSwr.data?.baseline?.risk ?? {};

  return (
    <div className="space-y-4">
      <div className="card space-y-3">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-bold">Mode</h2>
          {modeSwr.error ? (
            <span className="text-xs text-danger">
              {String(modeSwr.error.message ?? modeSwr.error)}
            </span>
          ) : null}
        </div>

        <div className="flex items-center gap-4">
          <span className="k">current</span>
          {currentMode === null ? (
            <span className="px-4 py-2 rounded-lg bg-bg2 text-muted font-mono">…</span>
          ) : isPaper ? (
            <span className="px-4 py-2 rounded-lg bg-accent/20 border border-accent text-accent font-bold text-xl tracking-widest">
              PAPER
            </span>
          ) : (
            <span className="px-4 py-2 rounded-lg bg-danger/20 border border-danger text-danger font-bold text-xl tracking-widest">
              LIVE
            </span>
          )}
        </div>

        <div className="flex gap-2 pt-2">
          <button
            className="px-3 py-2 rounded border border-accent/60 text-accent hover:bg-accent/10 disabled:opacity-40 disabled:cursor-not-allowed"
            onClick={switchToPaper}
            disabled={busy || isPaper || currentMode === null}
          >
            Switch to Paper
          </button>
          <button
            className="px-3 py-2 rounded border border-danger/60 text-danger hover:bg-danger/10 disabled:opacity-40 disabled:cursor-not-allowed"
            onClick={() => { setErr(null); setConfirmOpen(true); }}
            disabled={busy || isLive || currentMode === null}
          >
            Switch to Live
          </button>
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
        title="Switch to LIVE — real USDC at risk"
        body={
          "Live mode places REAL orders on Polymarket with REAL USDC.\n\n" +
          "Risk caps tighten automatically (max position, daily loss, per-category exposure all reduce).\n\n" +
          "You also need a live-confirm token — copy the command below, run it on the API host, and paste the result into the box."
        }
        confirmText="LIVE"
        busy={busy}
        onCancel={() => { setConfirmOpen(false); setConfirmToken(""); setErr(null); }}
        onConfirm={switchToLive}
      />

      {/*
        Helper UI for the live-confirm token. ConfirmModal is a black-box
        component (we can't push extra children inside it), so we render the
        instructions as a sibling overlay anchored above the modal's confirm
        button while the modal is open.
      */}
      {confirmOpen ? (
        <div className="fixed inset-0 z-[60] flex items-end justify-center p-4 pointer-events-none">
          <div className="card max-w-md w-full space-y-2 pointer-events-auto bg-panel mb-2 border border-white/10">
            <h4 className="text-sm font-semibold">Live-confirm token</h4>
            <p className="text-xs text-muted">
              1. Copy this command and run it on the API host:
            </p>
            <div className="relative">
              <pre className="text-[10px] bg-bg2 border border-white/10 rounded p-2 overflow-x-auto whitespace-pre-wrap break-all font-mono">
                {LIVE_CONFIRM_CMD}
              </pre>
              <button
                className="absolute top-1 right-1 text-[10px] px-2 py-0.5 rounded border border-white/10 text-muted hover:text-text bg-panel"
                onClick={copyCmd}
                type="button"
              >
                {copied ? "copied" : "copy"}
              </button>
            </div>
            <label className="block text-xs k mt-2">
              2. Paste live-confirm token (epoch:hmac):
            </label>
            <textarea
              className="w-full h-16 bg-bg2 border border-white/10 rounded px-2 py-1 font-mono text-xs"
              value={confirmToken}
              onChange={(e) => setConfirmToken(e.target.value)}
              placeholder="1717689600:a1b2c3..."
              spellCheck={false}
            />
            <p className="text-[10px] text-muted">
              Workaround: a server-issued challenge endpoint
              (/admin/settings/mode/live-challenge) is the proper fix and is
              not yet implemented.
            </p>
          </div>
        </div>
      ) : null}
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
