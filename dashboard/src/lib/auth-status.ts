/**
 * Shared "is the user signed in?" check.
 *
 * Settings tabs need to know whether to render the form (signed in) or
 * the "paste your admin token / connect your wallet" gate. Previously
 * every tab called only `getAdminToken()` which rejected SIWE-signed-in
 * users — they connected MetaMask but the tabs still showed
 * "Admin token not set". The tabs use this helper now so either
 * auth path satisfies the gate.
 *
 * Re-render trigger: dashboard pages poll this on a 1s interval via
 * `useAuthStatus()` so a user who connects mid-page sees the tabs flip
 * on within ~1s without a full reload.
 */

import { useEffect, useState } from "react";
import { getAdminToken } from "./admin";
import { getSessionToken } from "./wallet";

export function isAuthed(): boolean {
  if (typeof window === "undefined") return false;
  return !!(getSessionToken() || getAdminToken());
}

export function useAuthStatus(): boolean {
  const [authed, setAuthed] = useState<boolean>(() => isAuthed());
  useEffect(() => {
    // Poll unconditionally — both sign-in AND sign-out need to be reflected
    // (the previous early-exit-when-authed bug left every other page stuck
    // thinking the user was still signed in after Disconnect).
    const id = setInterval(() => {
      const next = isAuthed();
      setAuthed((prev) => (prev === next ? prev : next));
    }, 1000);
    // 'storage' fires cross-tab; useful when a user signs in on another tab
    // of the same dashboard.
    const onStorage = () => setAuthed(isAuthed());
    window.addEventListener("storage", onStorage);
    return () => {
      clearInterval(id);
      window.removeEventListener("storage", onStorage);
    };
  }, []);
  return authed;
}
