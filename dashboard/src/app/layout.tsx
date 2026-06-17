import "./globals.css";
import Link from "next/link";
import type { ReactNode } from "react";
import KillTag from "@/components/KillTag";
import ConnectWallet from "@/components/ConnectWallet";
import LiveBanner from "@/components/LiveBanner";
import KillButton from "@/components/KillButton";

export const metadata = { title: "Polybot", description: "Polymarket smart-money mirror" };

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="en">
      <body>
        {/* Bright red banner across ALL pages when LIVE mode is active.
            Critical visual cue — "real USDC at risk" — that you can't miss
            no matter which sub-page you're on. Renders nothing in pure
            paper mode so the dashboard stays calm. */}
        <LiveBanner />
        <header className="border-b border-white/5">
          <nav className="max-w-7xl mx-auto flex items-center gap-6 px-6 h-14">
            <Link href="/" className="font-bold text-accent">polybot</Link>
            <Link href="/wallets"  className="text-muted hover:text-text">Wallets</Link>
            <Link href="/bubble"   className="text-muted hover:text-text">Bubble Map</Link>
            <Link href="/heatmap"  className="text-muted hover:text-text">Heatmap</Link>
            <Link href="/signals"  className="text-muted hover:text-text">Signals</Link>
            <Link href="/trades"   className="text-muted hover:text-text">Trades</Link>
            <Link href="/fills"    className="text-muted hover:text-text">Fills</Link>
            <Link href="/strategy" className="text-muted hover:text-text">Strategy</Link>
            <Link href="/pipeline" className="text-muted hover:text-text">Pipeline</Link>
            <Link href="/metrics"  className="text-muted hover:text-text">Metrics</Link>
            <Link href="/settings" className="text-muted hover:text-text">Settings</Link>
            <div className="ml-auto flex items-center gap-3">
              <KillTag />
              {/* Header-resident kill switch — always one click away no matter
                  which page you're on. Replaces the "buried in a Sign-in card
                  at the bottom of the home page" anti-pattern. */}
              <KillButton />
              <ConnectWallet />
            </div>
          </nav>
        </header>
        <main className="max-w-7xl mx-auto p-6">{children}</main>
      </body>
    </html>
  );
}
