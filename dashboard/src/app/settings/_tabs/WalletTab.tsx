"use client";

/**
 * WalletTab — list / add / delete bot signing wallets.
 *
 * Backend:
 *   GET    /admin/settings/wallet              → Wallet[]
 *   POST   /admin/settings/wallet              → { label, address, funder_address,
 *                                                  signature_type, private_key }
 *   DELETE /admin/settings/wallet/{id}         → soft-disables
 *
 * Security note on the private_key field:
 *   - Stored only in component-local React state (no localStorage, no SWR cache).
 *   - Cleared from state ALWAYS after submit returns (success or error), so it
 *     never lingers in the DOM or React devtools.
 *   - The <textarea> has autoComplete="off", spellCheck={false}, no `name`
 *     attribute, and is a controlled component bound to state — no
 *     `defaultValue` retention, no browser autofill, no password-manager prompt.
 */

import type React from "react";
import { useEffect, useRef, useState } from "react";
import useSWR from "swr";
import { adminApi } from "@/lib/admin";
import { useAuthStatus } from "@/lib/auth-status";
import ConfirmModal from "@/components/ConfirmModal";

type Wallet = {
  id: number;
  label: string;
  address: string;
  funder_address: string | null;
  signature_type: number;
  is_active: boolean;
  created_at: string;
};

const SIG_TYPE_LABELS: Record<number, string> = {
  0: "EOA",
  1: "Email/Magic",
  2: "Browser",
};

const ADDR_RE = /^0x[a-fA-F0-9]{40}$/;
const ADDR_PLACEHOLDER = "0x" + "0".repeat(40);

function truncAddr(a: string): string {
  if (!a) return "—";
  if (a.length < 12) return a;
  return `${a.slice(0, 6)}...${a.slice(-4)}`;
}

function fmtDate(s: string): string {
  try {
    return new Date(s).toLocaleString();
  } catch {
    return s;
  }
}

export default function WalletTab() {
  const authed = useAuthStatus();

  const { data, error, isLoading, mutate } = useSWR<Wallet[]>(
    authed ? "/admin/settings/wallet" : null,
    (path: string) => adminApi.get(path) as Promise<Wallet[]>,
    { refreshInterval: 15000 },
  );

  // ── form state (each field controlled — `private_key` is the sensitive one)
  const [label, setLabel] = useState("");
  const [address, setAddress] = useState("");
  const [funder, setFunder] = useState("");
  const [sigType, setSigType] = useState<number>(0);
  const [privateKey, setPrivateKey] = useState(""); // CLEARED after every submit
  const [submitting, setSubmitting] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const [successMsg, setSuccessMsg] = useState<string | null>(null);
  const topRef = useRef<HTMLDivElement | null>(null);

  // ── delete-confirm modal state
  const [deleteTarget, setDeleteTarget] = useState<Wallet | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  // Defense-in-depth for the private-key field:
  //   1. Warn before navigation (close-tab / route change) while a key sits
  //      in the textarea, so an accidental click doesn't drop the key.
  //   2. Zero the state on unmount so React releases the reference.
  useEffect(() => {
    if (!privateKey) return;
    const handler = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [privateKey]);
  useEffect(() => () => setPrivateKey(""), []);

  if (!authed) {
    return (
      <div className="card">
        <h2 className="text-lg font-bold mb-2">Wallets</h2>
        <p className="text-sm text-muted">
          Not signed in. Click <span className="text-accent">Connect Wallet</span>{" "}
          in the header, or paste an admin token in the kill-switch widget on
          the <a href="/" className="text-accent underline">home page</a>.
        </p>
      </div>
    );
  }

  const wallets = data ?? [];

  async function onSubmit(e: React.FormEvent) {
    e.preventDefault();
    setFormError(null);
    setSuccessMsg(null);

    // Client-side validation
    if (!label.trim()) { setFormError("label is required"); return; }
    if (!ADDR_RE.test(address.trim())) {
      setFormError("address must be a 42-char 0x-prefixed hex string");
      return;
    }
    if (!ADDR_RE.test(funder.trim())) {
      setFormError("funder_address must be a 42-char 0x-prefixed hex string");
      return;
    }
    if (!privateKey.trim()) {
      setFormError("private_key is required");
      return;
    }

    setSubmitting(true);
    try {
      await adminApi.post("/admin/settings/wallet", {
        label: label.trim(),
        address: address.trim(),
        funder_address: funder.trim(),
        signature_type: sigType,
        private_key: privateKey.trim(),
      });

      // SUCCESS — clear the entire form (especially private_key) and refresh.
      const savedLabel = label.trim();
      setLabel("");
      setAddress("");
      setFunder("");
      setSigType(0);
      setPrivateKey("");
      setSuccessMsg(`wallet "${savedLabel}" added · old active wallet deactivated`);
      await mutate();
      topRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (err: any) {
      // ERROR — keep the rest of the form filled so the user can fix it, but
      // ALWAYS clear the private_key (security: never leave the key sitting in
      // the DOM after a failed request — they should re-paste it intentionally).
      setPrivateKey("");
      setFormError(err?.message ?? String(err));
      topRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    } finally {
      setSubmitting(false);
    }
  }

  async function onConfirmDelete() {
    if (!deleteTarget) return;
    setDeleteBusy(true);
    setDeleteError(null);
    try {
      await adminApi.delete(`/admin/settings/wallet/${deleteTarget.id}`);
      setDeleteTarget(null);
      await mutate();
    } catch (err: any) {
      setDeleteError(err?.message ?? String(err));
    } finally {
      setDeleteBusy(false);
    }
  }

  return (
    <div className="space-y-4" ref={topRef}>
      <header className="flex items-baseline gap-3">
        <h2 className="text-xl font-bold">Signing wallets</h2>
        <span className="text-muted text-sm">
          {wallets.length} configured · refreshes every 15s
        </span>
      </header>

      {successMsg && (
        <div className="card border border-accent/40 text-sm text-accent">
          {successMsg}
        </div>
      )}
      {error && (
        <div className="card border border-danger/40 text-sm text-danger">
          failed to load wallets: {String((error as Error).message ?? error)}
        </div>
      )}

      {/* ───────────────────────── EXISTING WALLETS TABLE ─────────────────────── */}
      <div className="card overflow-x-auto">
        <table className="text-sm w-full">
          <thead className="text-muted text-xs uppercase">
            <tr>
              <th className="text-left p-2">ID</th>
              <th className="text-left p-2">Label</th>
              <th className="text-left p-2">Address</th>
              <th className="text-left p-2">Funder</th>
              <th className="text-left p-2">Sig Type</th>
              <th className="text-left p-2">Active</th>
              <th className="text-left p-2">Created</th>
              <th className="text-right p-2">Actions</th>
            </tr>
          </thead>
          <tbody>
            {isLoading && (
              <tr><td colSpan={8} className="p-4 text-center text-muted">loading…</td></tr>
            )}
            {!isLoading && wallets.length === 0 && (
              <tr><td colSpan={8} className="p-6 text-center text-muted">no wallets configured</td></tr>
            )}
            {wallets.map((w) => {
              const active = w.is_active;
              return (
                <tr
                  key={w.id}
                  className={`border-t border-white/5 ${active ? "font-semibold" : ""}`}
                >
                  <td className="p-2 tabular-nums">{w.id}</td>
                  <td className="p-2">{w.label}</td>
                  <td className="p-2 font-mono text-xs" title={w.address}>
                    {truncAddr(w.address)}
                  </td>
                  <td className="p-2 font-mono text-xs" title={w.funder_address ?? ""}>
                    {w.funder_address ? truncAddr(w.funder_address) : "—"}
                  </td>
                  <td className="p-2">
                    {SIG_TYPE_LABELS[w.signature_type] ?? `type ${w.signature_type}`}
                  </td>
                  <td className="p-2">
                    {active ? (
                      <span className="inline-flex items-center gap-1.5">
                        <span
                          className="inline-block w-2 h-2 rounded-full bg-accent"
                          aria-label="active"
                        />
                        <span className="text-accent">active</span>
                      </span>
                    ) : (
                      <span className="text-muted">—</span>
                    )}
                  </td>
                  <td className="p-2 text-xs text-muted">{fmtDate(w.created_at)}</td>
                  <td className="p-2 text-right">
                    <button
                      className="px-2 py-1 rounded border border-danger/40 text-danger text-xs hover:bg-danger/10"
                      onClick={() => { setDeleteError(null); setDeleteTarget(w); }}
                    >
                      delete
                    </button>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
        {deleteError && (
          <div className="text-sm text-danger mt-2">delete failed: {deleteError}</div>
        )}
      </div>

      {/* ─────────────────────────── ADD WALLET FORM ──────────────────────────── */}
      <div className="card space-y-3">
        <h3 className="text-lg font-bold">Add wallet</h3>

        <div className="rounded border border-danger/50 bg-danger/10 p-3 text-sm space-y-1">
          <div className="font-semibold text-danger">
            ⚠ Adding a wallet stores the encrypted private key in the database.
          </div>
          <div className="text-text">
            Lose <code className="font-mono text-xs">WALLET_ENCRYPTION_KEY</code>{" "}
            → the wallet is unrecoverable. Backup <code className="font-mono text-xs">.env</code>.
          </div>
          <div className="text-text">
            Old active wallet is automatically deactivated.
          </div>
        </div>

        {formError && (
          <div className="rounded border border-danger/40 bg-danger/10 p-2 text-sm text-danger">
            {formError}
          </div>
        )}

        <form className="space-y-3" onSubmit={onSubmit} autoComplete="off">
          <div className="grid grid-cols-2 gap-3">
            <Field label="Label">
              <input
                type="text"
                className="w-full bg-black/40 border border-white/10 rounded px-2 py-1"
                value={label}
                onChange={(e) => setLabel(e.target.value)}
                placeholder="e.g. polybot-prod-1"
                autoComplete="off"
                disabled={submitting}
              />
            </Field>

            <Field label="Signature type">
              <select
                className="w-full bg-black/40 border border-white/10 rounded px-2 py-1"
                value={sigType}
                onChange={(e) => setSigType(Number(e.target.value))}
                disabled={submitting}
              >
                <option value={0}>0 — EOA</option>
                <option value={1}>1 — Email/Magic</option>
                <option value={2}>2 — Browser</option>
              </select>
            </Field>

            <Field label="Address (0x...)">
              <input
                type="text"
                className="w-full bg-black/40 border border-white/10 rounded px-2 py-1 font-mono text-xs"
                value={address}
                onChange={(e) => setAddress(e.target.value)}
                placeholder={ADDR_PLACEHOLDER}
                maxLength={42}
                autoComplete="off"
                spellCheck={false}
                disabled={submitting}
              />
            </Field>

            <Field label="Funder address (0x...)">
              <input
                type="text"
                className="w-full bg-black/40 border border-white/10 rounded px-2 py-1 font-mono text-xs"
                value={funder}
                onChange={(e) => setFunder(e.target.value)}
                placeholder={ADDR_PLACEHOLDER}
                maxLength={42}
                autoComplete="off"
                spellCheck={false}
                disabled={submitting}
              />
            </Field>
          </div>

          <Field label="Private key (never persisted in browser)">
            <textarea
              className="w-full bg-black/40 border border-white/10 rounded px-2 py-1 font-mono text-xs h-20"
              value={privateKey}
              onChange={(e) => setPrivateKey(e.target.value)}
              placeholder="0x..."
              autoComplete="off"
              spellCheck={false}
              // controlled component — no defaultValue, no `name` attribute, so
              // browser password managers won't try to save it
              disabled={submitting}
            />
            <div className="text-xs text-muted mt-1">
              Cleared from this field immediately after the request returns
              (success or error). No localStorage, no autofill.
            </div>
          </Field>

          <div className="flex justify-end">
            <button
              type="submit"
              className="px-4 py-2 rounded bg-accent text-black text-sm font-semibold disabled:opacity-40"
              disabled={submitting}
            >
              {submitting ? "adding…" : "add wallet"}
            </button>
          </div>
        </form>
      </div>

      {/* ───────────────────────── DELETE CONFIRM MODAL ───────────────────────── */}
      <ConfirmModal
        open={!!deleteTarget}
        title={`Delete wallet #${deleteTarget?.id ?? ""}?`}
        body={
          deleteTarget
            ? `Soft-disables wallet "${deleteTarget.label}" (${truncAddr(deleteTarget.address)}).\n` +
              `The encrypted private key remains in the database but the wallet ` +
              `will no longer be used for signing.`
            : ""
        }
        confirmText="DELETE"
        busy={deleteBusy}
        onConfirm={onConfirmDelete}
        onCancel={() => { setDeleteTarget(null); setDeleteError(null); }}
      />
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label className="block">
      <div className="k mb-1">{label}</div>
      {children}
    </label>
  );
}
