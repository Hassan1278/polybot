/**
 * SIWE wallet sign-in for dashboard auth.
 *
 * UX: user clicks "Connect Wallet" → MetaMask pops up → they pick the
 * account → we ask the server for a fresh nonce + message → they sign
 * (gasless, ECDSA) → we POST the signature to /auth/verify → server
 * returns a session token → we put it in sessionStorage as
 * polybot_session_token → all admin requests now send X-Session-Token.
 *
 * Backward compat: the existing admin-token input still works, so
 * scripts (kill_switch.py, dashboard pre-Web3 users) continue to
 * authenticate the old way.
 */

import { ethers } from "ethers";
import { API } from "./api";

export const SESSION_KEY = "polybot_session_token";
export const SESSION_ADDR_KEY = "polybot_session_address";

declare global {
  interface Window {
    ethereum?: ethers.Eip1193Provider;
  }
}

export function hasInjectedWallet(): boolean {
  return typeof window !== "undefined" && !!window.ethereum;
}

export function getSessionToken(): string | null {
  if (typeof window === "undefined") return null;
  return sessionStorage.getItem(SESSION_KEY);
}

export function getSessionAddress(): string | null {
  if (typeof window === "undefined") return null;
  return sessionStorage.getItem(SESSION_ADDR_KEY);
}

export function clearSession(): void {
  if (typeof window === "undefined") return;
  sessionStorage.removeItem(SESSION_KEY);
  sessionStorage.removeItem(SESSION_ADDR_KEY);
}

/**
 * Full sign-in flow. Returns the session token + address on success or
 * throws an Error with a human-readable message that the UI can display.
 */
export async function connectAndSignIn(): Promise<{ address: string; token: string }> {
  if (!hasInjectedWallet()) {
    throw new Error(
      "no Ethereum wallet detected — install MetaMask (or any EIP-1193 wallet) to connect",
    );
  }
  const provider = new ethers.BrowserProvider(window.ethereum!);

  // 1. Get the user's address (triggers MetaMask popup if not yet connected).
  let signer: ethers.JsonRpcSigner;
  try {
    signer = await provider.getSigner();
  } catch (e) {
    throw new Error(
      `wallet connection rejected: ${e instanceof Error ? e.message : String(e)}`,
    );
  }
  const address = await signer.getAddress();

  // 2. Ask the server for a fresh nonce + the message to sign.
  const nonceUrl = `${API}/auth/nonce?address=${encodeURIComponent(address)}`;
  const nonceResp = await fetch(nonceUrl);
  if (!nonceResp.ok) {
    throw new Error(`server refused nonce: ${nonceResp.status} ${await nonceResp.text()}`);
  }
  const { message } = (await nonceResp.json()) as { message: string };

  // 3. Sign the message (gasless — just an ECDSA signature).
  let signature: string;
  try {
    signature = await signer.signMessage(message);
  } catch (e) {
    throw new Error(
      `user declined to sign: ${e instanceof Error ? e.message : String(e)}`,
    );
  }

  // 4. Verify with the server.
  const verifyResp = await fetch(`${API}/auth/verify`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ address, message, signature }),
  });
  if (!verifyResp.ok) {
    throw new Error(
      `server rejected signature: ${verifyResp.status} ${await verifyResp.text()}`,
    );
  }
  const { session_token } = (await verifyResp.json()) as { session_token: string };

  // 5. Persist + return.
  sessionStorage.setItem(SESSION_KEY, session_token);
  sessionStorage.setItem(SESSION_ADDR_KEY, address.toLowerCase());
  return { address: address.toLowerCase(), token: session_token };
}

/**
 * Server-side logout: invalidates the Redis session and clears the
 * browser-side storage.
 */
export async function logout(): Promise<void> {
  const token = getSessionToken();
  if (token) {
    await fetch(`${API}/auth/logout`, {
      method: "POST",
      headers: { "X-Session-Token": token },
    }).catch(() => undefined);
  }
  clearSession();
}
