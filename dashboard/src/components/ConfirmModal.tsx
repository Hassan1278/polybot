"use client";
import { useEffect, useState } from "react";

/**
 * Confirmation modal for destructive / high-stakes actions.
 * The user must TYPE `confirmText` exactly before the action enables —
 * prevents accidental clicks (especially the paper→live switch).
 *
 * Usage:
 *   const [open, setOpen] = useState(false);
 *   <ConfirmModal
 *     open={open}
 *     title="Switch to LIVE mode?"
 *     body="Live mode places real USDC orders on Polymarket."
 *     confirmText="LIVE"
 *     onConfirm={async () => { ... }}
 *     onCancel={() => setOpen(false)}
 *   />
 */
export default function ConfirmModal({
  open, title, body, confirmText, onConfirm, onCancel, busy,
}: {
  open: boolean;
  title: string;
  body: string;
  confirmText: string;
  onConfirm: () => void | Promise<void>;
  onCancel: () => void;
  busy?: boolean;
}) {
  const [typed, setTyped] = useState("");
  // Reset typed each time the modal opens. Otherwise the previous confirm
  // text persists (e.g. after typing 'LIVE' to switch modes, the next
  // 'DELETE wallet' confirm starts pre-filled with 'LIVE' — and if the user
  // had successfully typed 'DELETE' to remove a wallet earlier, the next
  // delete confirm would be one-click-enabled, defeating the safety.
  useEffect(() => {
    if (open) setTyped("");
  }, [open]);
  if (!open) return null;
  const matches = typed.trim() === confirmText;
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <div className="card max-w-md w-full space-y-3">
        <h2 className="text-lg font-bold">{title}</h2>
        <p className="text-sm text-muted whitespace-pre-line">{body}</p>
        <label className="block text-xs k mt-2">
          Type <span className="font-mono text-text">{confirmText}</span> to confirm:
        </label>
        <input
          autoFocus
          className="w-full bg-bg2 border border-white/10 rounded px-2 py-1 font-mono"
          value={typed}
          onChange={(e) => setTyped(e.target.value)}
        />
        <div className="flex gap-2 justify-end mt-3">
          <button
            className="px-3 py-1 rounded border border-white/10 text-muted hover:text-text"
            onClick={() => { setTyped(""); onCancel(); }}
            disabled={busy}
          >cancel</button>
          <button
            className="px-3 py-1 rounded bg-danger text-white disabled:opacity-40"
            onClick={() => { onConfirm(); }}
            disabled={!matches || busy}
          >
            {busy ? "…" : "confirm"}
          </button>
        </div>
      </div>
    </div>
  );
}
