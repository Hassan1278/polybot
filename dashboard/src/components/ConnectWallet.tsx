"use client";

import { useEffect, useState } from "react";
import { clearSession, connectAndSignIn, getSessionAddress, hasInjectedWallet, logout } from "@/lib/wallet";

/**
 * "Connect Wallet" button — Web3 sign-in for the dashboard.
 *
 * Three states: not-signed-in, signed-in (showing truncated address),
 * busy (showing spinner). Mirrors the UX of pump.fun / Uniswap etc.
 *
 * Coexists with the admin-token input: either auth path authorises
 * admin requests, so the user can pick whichever they prefer.
 */
export default function ConnectWallet({
  onChange,
}: {
  onChange?: (address: string | null) => void;
}) {
  const [addr, setAddr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    setAddr(getSessionAddress());
  }, []);

  const doConnect = async () => {
    setErr(null);
    setBusy(true);
    try {
      const { address } = await connectAndSignIn();
      setAddr(address);
      onChange?.(address);
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      setErr(msg);
      onChange?.(null);
    } finally {
      setBusy(false);
    }
  };

  const doDisconnect = async () => {
    setBusy(true);
    try {
      await logout();
      setAddr(null);
      onChange?.(null);
    } finally {
      setBusy(false);
    }
  };

  if (addr) {
    return (
      <div className="flex items-center gap-2">
        <span className="text-xs k">connected:</span>
        <span className="text-xs font-mono text-accent" title={addr}>
          {addr.slice(0, 6)}…{addr.slice(-4)}
        </span>
        <button
          onClick={doDisconnect}
          disabled={busy}
          className="text-xs text-muted hover:text-danger underline disabled:opacity-40"
        >
          {busy ? "…" : "disconnect"}
        </button>
      </div>
    );
  }

  if (!hasInjectedWallet()) {
    return (
      <div className="text-xs text-muted">
        no Ethereum wallet detected — install{" "}
        <a
          href="https://metamask.io/download/"
          target="_blank"
          rel="noopener noreferrer"
          className="text-accent underline"
        >
          MetaMask
        </a>
        , or paste an admin token below
      </div>
    );
  }

  return (
    <div className="space-y-2">
      <button
        onClick={doConnect}
        disabled={busy}
        className="bg-accent text-black px-3 py-2 rounded text-sm font-semibold disabled:opacity-40"
      >
        {busy ? "connecting…" : "Connect Wallet"}
      </button>
      {err && (
        <div className="text-xs text-danger break-all">
          {err}{" "}
          <button
            onClick={() => { clearSession(); setErr(null); }}
            className="ml-2 underline text-muted hover:text-text"
          >
            dismiss
          </button>
        </div>
      )}
    </div>
  );
}
