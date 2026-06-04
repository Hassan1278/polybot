# Hosting decision

## TL;DR

For a single-operator bot: **Contabo VPS L** (€8.49/mo) in EU. Docker Compose deploy. Caddy for TLS. Nightly pg_dump backup via cron.

## Why Contabo

| Criterion | Why it wins |
|---|---|
| RAM | 16 GB at this price tier is unmatched (Hetzner CCX13 = 8 GB, Linode 16 GB ≥ €60) |
| Disk | 400 GB NVMe — plenty for ~ 5 years of trade history |
| CPU | 6 vCPU (EPYC) handles correlation matrices over hundreds of wallets without breaking sweat |
| Egress | Unlimited, no bandwidth caps |
| Networking | Solid 80–120 ms RTT to Polymarket's US-East front-end — fine for non-arb |
| Reputation | Strong choice for backtesting and trading bots in independent reviews |
| Lock-in | Zero — it's a plain Linux box |

## When NOT Contabo

| Scenario | Pick |
|---|---|
| You need sub-30 ms to Polymarket's matching engine (true arbitrage) | **Vultr NJ** or **Linode NJ** — same DC region as Polymarket's CF front-end |
| You want auto-scaling / multi-region | **Fly.io** (~ $25/mo) — git-push deploy, but plan for a separate managed Postgres |
| Your team is large + cares about IAM, RDS, etc. | **AWS** (t3.medium + RDS Postgres + ElastiCache) — overkill for personal, fine for a small org |
| You already have a homelab and uptime is acceptable | self-host on the homelab + duckdns |

## Why NOT Railway

Railway is the easiest UX but priciest per resource and the resource model is opaque (memory bursts ≠ guaranteed). Fine for prototyping; expensive at 24/7 steady-state.

## Storage / backup strategy

- **Daily**: `pg_dump | gzip` → `/var/backups/polybot/pg-YYYY-MM-DD.sql.gz`, keep 14 days. (Installed by `contabo-setup.sh`.)
- **Weekly**: rsync the backup directory to a separate provider (Backblaze B2 or Hetzner Storage Box). The bot's signals + fills + audit log are the asset, not the model.
- **DR drill**: actually restore a backup into a sandbox container once a month. Untested backups don't exist.

## Sizing the box (rule of thumb)

| Workload | Memory | CPU |
|---|---|---|
| Postgres (Timescale, 30 d trade window, ~ 5 M rows) | 4–8 GB | 1 vCPU |
| Redis | 200 MB | 0.1 vCPU |
| 4 Python services (asyncio, modest pandas DataFrames) | 2 GB total | 1 vCPU |
| Next.js dashboard (standalone) | 250 MB | 0.1 vCPU |
| Caddy | 50 MB | 0.05 vCPU |

→ comfortably fits in 8 GB; 16 GB gives headroom for the wallet-stat re-computation spikes.
