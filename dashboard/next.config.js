/** @type {import('next').NextConfig} */

// Internal Docker DNS name for the api container (used at runtime by the
// Next.js server-side proxy below). The browser never sees this — only the
// dashboard's standalone Node server resolves api:8000.
const API_INTERNAL = process.env.API_INTERNAL_URL || "http://api:8000";

const nextConfig = {
  output: "standalone",
  reactStrictMode: true,
  experimental: { typedRoutes: false },
  // trailingSlash stays at the Next.js default (false). The earlier
  // attempt to set it true broke the rewrite — Next.js leaked the
  // internal `http://api:8000/...` Location header to the browser on
  // FastAPI's trailing-slash redirect. The fix is route-side instead:
  // the FastAPI admin/settings routes are registered with empty path
  // (no slash) so the dashboard's `/admin/settings` fetch lands
  // directly, no redirect.

  // Same-origin API proxy. Without this, browser fetches go to
  //   http://localhost:8000/positions  (cross-origin → CORS preflight,
  //                                     two separate Docker Desktop port-forwards)
  // which is fragile (cache + CORS edge cases + flaky forwarding).
  // With this:
  //   http://localhost:3000/api/positions  → Next.js server proxies to api:8000/positions
  // Single port to the browser, no CORS, dashboard is fully self-contained.
  //
  // The dashboard's API client falls back to `${API}` if the rewrite isn't
  // hit (e.g. NEXT_PUBLIC_API_URL points at a remote host).
  async rewrites() {
    return [
      { source: "/api/:path*", destination: `${API_INTERNAL}/:path*` },
    ];
  },
};

module.exports = nextConfig;
