"use client";

import useSWR from "swr";
import Link from "next/link";
import { useState } from "react";
import { adminApi } from "@/lib/admin";
import { useAuthStatus } from "@/lib/auth-status";
import ModeTab from "./_tabs/ModeTab";
import RiskTab from "./_tabs/RiskTab";
import CategoriesTab from "./_tabs/CategoriesTab";
import GatesTab from "./_tabs/GatesTab";
import WalletTab from "./_tabs/WalletTab";

type ModeResp = { mode: "paper" | "live" };

const TABS = ["Mode", "Risk", "Categories", "Gates", "Wallet"] as const;
type TabName = (typeof TABS)[number];

export default function SettingsPage() {
  const [active, setActive] = useState<TabName>("Mode");
  const authed = useAuthStatus();

  // Only fetch mode badge if we have any auth (avoids constant 401s)
  const { data: modeData, error: modeError } = useSWR<ModeResp>(
    authed ? "/admin/settings/mode" : null,
    (path: string) => adminApi.get(path) as Promise<ModeResp>,
    { refreshInterval: 5000 },
  );

  if (!authed) {
    return (
      <div className="space-y-6">
        <header className="flex items-baseline gap-6">
          <h1 className="text-2xl font-bold">Settings</h1>
        </header>
        <div className="card">
          <h2 className="text-sm k mb-2">Not signed in</h2>
          <p className="text-sm text-muted">
            Click <span className="text-accent">Connect Wallet</span> in the
            header to sign in with MetaMask, or paste an admin token into the
            kill-switch widget on the{" "}
            <Link href="/" className="text-accent underline">
              home page
            </Link>.
          </p>
        </div>
      </div>
    );
  }

  const mode = modeData?.mode;
  const modeBadge =
    mode === "live" ? (
      <span className="text-danger text-sm">LIVE mode</span>
    ) : mode === "paper" ? (
      <span className="text-accent text-sm">paper mode</span>
    ) : modeError ? (
      <span className="text-danger text-sm">mode: error</span>
    ) : (
      <span className="text-muted text-sm">mode: …</span>
    );

  return (
    <div className="space-y-6">
      <header className="flex items-baseline gap-6">
        <h1 className="text-2xl font-bold">Settings</h1>
        {modeBadge}
        {modeError ? (
          <span className="text-xs text-danger" title={String(modeError)}>
            {String((modeError as Error).message || modeError)}
          </span>
        ) : null}
      </header>

      <nav className="flex gap-1 border-b border-white/10">
        {TABS.map((t) => {
          const isActive = t === active;
          return (
            <button
              key={t}
              onClick={() => setActive(t)}
              className={[
                "px-4 py-2 text-sm border-b-2 -mb-px transition-colors",
                isActive
                  ? "border-accent text-text"
                  : "border-transparent text-muted hover:text-text",
              ].join(" ")}
            >
              {t}
            </button>
          );
        })}
      </nav>

      <section>
        {active === "Mode" && <ModeTab />}
        {active === "Risk" && <RiskTab />}
        {active === "Categories" && <CategoriesTab />}
        {active === "Gates" && <GatesTab />}
        {active === "Wallet" && <WalletTab />}
      </section>
    </div>
  );
}
