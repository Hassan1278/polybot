"use client";

import { useState } from "react";
import useSWR from "swr";
import { fetcher } from "@/lib/api";
import { adminApi } from "@/lib/admin";
import { useAuthStatus } from "@/lib/auth-status";
import ConfirmModal from "@/components/ConfirmModal";

type Health = { kill_switch: string | null; mode: string };

/**
 * Persistent kill-switch button in the header.
 *
 * Previously the only way to kill the bot was a button buried inside the
 * "Sign in" card at the bottom of the home page. An audit flagged that
 * as a critical safety UX hole — in a real emergency the operator
 * doesn't want to navigate back to home and scroll. Now the kill action
 * is one click away on every page, with a typed confirmation modal.
 *
 * Hidden when not signed in (so we don't show a button that immediately
 * 401s). When kill is ALREADY active, the button morphs into "Clear" so
 * the operator can resume trading without leaving the page they're on.
 */
export default function KillButton() {
  const authed = useAuthStatus();
  const { data, mutate } = useSWR<Health>("/health", fetcher, { refreshInterval: 5000 });
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  if (!authed) return null;

  const killed = !!data?.kill_switch;

  const doKill = async () => {
    setBusy(true); setErr(null);
    try {
      await adminApi.post("/admin/kill?reason=dashboard-emergency", {});
      mutate();
      setConfirmOpen(false);
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e));
    } finally {
      setBusy(false);
    }
  };

  const doClear = async () => {
    setBusy(true); setErr(null);
    try {
      await adminApi.post("/admin/kill/clear?by=dashboard", {});
      mutate();
    } catch (e) {
      setErr(String(e instanceof Error ? e.message : e));
    } finally {
      setBusy(false);
    }
  };

  if (killed) {
    return (
      <button
        onClick={doClear}
        disabled={busy}
        className="bg-accent text-bg px-3 py-1.5 rounded text-xs font-bold disabled:opacity-40 tracking-wide"
        title={`Resume trading (kill reason: ${data?.kill_switch})`}
      >
        {busy ? "…" : "CLEAR KILL"}
      </button>
    );
  }

  return (
    <>
      <button
        onClick={() => { setErr(null); setConfirmOpen(true); }}
        disabled={busy}
        className="bg-danger text-white px-3 py-1.5 rounded text-xs font-bold disabled:opacity-40 tracking-wide hover:bg-danger/80"
        title="Stop the bot — no new fills until cleared"
      >
        🛑 KILL
      </button>
      <ConfirmModal
        open={confirmOpen}
        title="Activate kill switch?"
        body={
          "Stops every NEW trade across paper AND live modes immediately.\n\n" +
          "Existing open positions are NOT closed by the kill switch — use\n" +
          "the 'Close all' button on the home page for that.\n\n" +
          "Type KILL to confirm."
        }
        confirmText="KILL"
        busy={busy}
        onCancel={() => { setConfirmOpen(false); setErr(null); }}
        onConfirm={doKill}
      />
      {err && (
        <span className="text-xs text-danger max-w-[120px] truncate" title={err}>
          {err}
        </span>
      )}
    </>
  );
}
