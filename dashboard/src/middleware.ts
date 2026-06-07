import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

/**
 * Security headers — sent on every dashboard response.
 *
 * CSP rationale:
 *  - default 'self' blocks every external script/style/iframe by default.
 *  - connect-src allows fetch to the API (NEXT_PUBLIC_API_URL) + the
 *    websocket endpoint (ws:// or wss://).
 *  - script-src + style-src 'unsafe-inline' is necessary because Tailwind
 *    injects styles inline AND Next.js inlines a hydration snippet.
 *    When we migrate to nonce-based CSP we can drop these — for now the
 *    real attack surface is XSS from API data and that's mitigated by
 *    React's auto-escaping.
 *  - frame-ancestors 'none' = no clickjacking (also covered by X-Frame-Options).
 */
export function middleware(_request: NextRequest) {
  const apiUrl = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8000";
  const wsUrl = apiUrl.replace(/^http/, "ws");
  const response = NextResponse.next();

  const csp = [
    "default-src 'self'",
    `connect-src 'self' ${apiUrl} ${wsUrl}`,
    "img-src 'self' data:",
    // 'unsafe-eval' removed (Triple-verify HIGH-3) — Next.js 14 prod build
    // doesn't need it; only dev mode does. 'unsafe-inline' stays for
    // Tailwind + Next's hydration snippet (mitigated by React auto-escape).
    "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline'",
    "font-src 'self' data:",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
  ].join("; ");

  response.headers.set("Content-Security-Policy", csp);
  response.headers.set("X-Frame-Options", "DENY");
  response.headers.set("X-Content-Type-Options", "nosniff");
  response.headers.set("Referrer-Policy", "strict-origin-when-cross-origin");
  response.headers.set("Permissions-Policy", "geolocation=(), camera=(), microphone=()");
  // Next.js standalone defaults HTML responses to `Cache-Control:
  // s-maxage=31536000, stale-while-revalidate` (1-YEAR cache!), which means
  // a user who visited an old broken build keeps seeing it until the
  // browser revalidates — for a YEAR. For a single-instance dashboard
  // that's user-hostile, so override every HTML response to no-store.
  // Static assets under /_next/static still have their hash-based long
  // cache (immutable) because the middleware matcher skips them.
  response.headers.set("Cache-Control", "no-store, no-cache, must-revalidate");
  // Prevent any browser/proxy ETag/stale-revalidate loop:
  response.headers.set("Pragma", "no-cache");
  return response;
}

export const config = {
  // skip static assets, favicon, _next/image
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
