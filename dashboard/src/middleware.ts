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
    "script-src 'self' 'unsafe-inline' 'unsafe-eval'",
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
  return response;
}

export const config = {
  // skip static assets, favicon, _next/image
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
