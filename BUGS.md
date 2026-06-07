# Known Bugs & Improvements — Polybot

Stand: nach Resilience-Push am 2026-06-07. Phase A + B + Security-Fixes alle implementiert.

## Dashboard Control Plane + Wallet Management + Per-Mode Settings (2026-06-07, IMPLEMENTED)

Full feature shipment from `~/.claude/plans/serene-seeking-puffin.md`. Five new
capabilities, all LIVE-tested before commit:

- **Encrypted wallet credentials** — new `packages/polybot/crypto.py` (AES-256-GCM
  + AAD binding + nonce-per-encryption + weak-key rejection) + `WalletCredential`
  model + migration `0006_wallet_credentials.py`. `ClobClient._signed_client()`
  prefers DB credential, falls back to .env. Encrypt/decrypt roundtrip + AAD-mismatch
  rejection live-verified.
- **Redis-override runtime config** — new `packages/polybot/runtime_config.py` with
  `merged_risk()`, `merged_categories()`, `merged_gates()`, `set_mode()`,
  `set_overrides()`. YAML stays the shipped baseline; Redis overrides are the
  dashboard's live patches. `services/executor/risk.py.preflight()` switched to
  `merged_risk()` so per-mode caps take effect on the next preflight without restart.
- **Per-mode YAML profiles** — `config/risk.yaml` restructured into
  `defaults` + `modes.{paper,live}` keys. `HotConfig.get(mode)` deep-merges
  defaults+per-mode. Live mode = tighter caps (max_open 30 vs 200, daily
  loss 100 vs 50, sizing.anchor 0.65 vs 0.5, etc.).
- **Admin endpoints** (services/api/routes/settings.py + metrics.py + main.py):
  - `GET /admin/settings/` — effective + overrides + baseline
  - `GET|POST /admin/settings/mode` — paper↔live (live requires `X-Live-Confirm` HMAC)
  - `PATCH|DELETE /admin/settings/risk` — risk-config overrides
  - `PATCH|POST|DELETE /admin/settings/categories[/name]` — categories CRUD
  - `PATCH /admin/settings/gates` — gate-param overrides
  - `GET|POST|DELETE /admin/settings/wallet[/id]` — wallet credentials
  - `GET /metrics/categories?window=24h` — per-category winrate/profit/signals
- **Dashboard `/settings` + `/metrics` pages** — 5 tabs (Mode, Risk, Categories,
  Gates, Wallet) + per-category metrics page with Nivo bar chart. CSP headers
  via `dashboard/src/middleware.ts`. `dashboard/src/lib/admin.ts` wraps
  authenticated PATCH/POST/DELETE with sessionStorage-only token storage.
- **Production security** — rate limit (60 req/min/IP via Redis bucket counter,
  `services/api/rate_limit.py`) wired on all `/admin/*` includes. CSP +
  X-Frame-Options DENY + X-Content-Type-Options nosniff + Permissions-Policy
  in middleware. Wallet `private_key` form: controlled input, no localStorage,
  no autoComplete, cleared on success AND on error.

LIVE-verified before commit:
  - Encrypt/decrypt roundtrip with AAD binding ✓
  - Mode switch paper→live changes all 6 risk caps (max_open 200→30, etc.) ✓
  - Live-confirm HMAC required (403 without it) ✓
  - PATCH /admin/settings/risk applies override, DELETE reverts ✓
  - /metrics/categories returns 8 categories with real per-cat data ✓
  - 65 rapid admin requests → 59 OK + 6 rate-limited (429) ✓
  - CSP header present on every dashboard response ✓
  - All 7 containers healthy, 0 unhandled exceptions ✓

Verdict: 3 parallel evaluator workflow (security/functional/code-quality) ran
post-implementation — see workflow results in this session's final notes.

---

## Resilience Hardening — Phase A + B (2026-06-07, FIXED)

Out of the original plan (`C:\Users\Hassa\.claude\plans\serene-seeking-puffin.md`):

- **A1** DB retry + pool_recycle in `packages/polybot/db.py` — tenacity AsyncRetrying on OperationalError, 3 attempts, exponential backoff. pool_recycle=1800, pool_timeout=10. LIVE-tested: `docker compose restart postgres` mid-traffic → executor recovered in <5 s, no unhandled exceptions.
- **A2** Fill UNIQUE constraint via `0004_fill_signal_unique.py` migration + executor dedup at top of `services/executor/main.py:handle()`. LIVE-tested: republishing signal_id 12713 → `executor_dedup_skip` fires, no duplicate row.
- **A3** `/health/deep` endpoint in `services/api/routes/health.py` queries DB + Redis, returns 503 if either fails. Docker healthchecks tightened: postgres+redis do query/write probes (not just port), api uses `/health/deep`, ingest/signals/executor use new `/health` on port 8081.
- **B1** Redis Streams + DLQ for `signal:new`. New `xpublish`/`xconsume`/`xack`/`xdlq`/`xautoclaim` in `packages/polybot/redis_bus.py`. Executor `signal_consumer` uses XREADGROUP with consumer group `executors`. On handle() exception: payload routed to `signal:new:dlq` + critical alert fired. Periodic `_autoclaim_loop` reclaims pending entries from crashed peer consumers every 60 s. LIVE-tested: test poison message correctly routed to DLQ with full error trace, alert sent.
- **B2** `/health` endpoints on ingest, signals, executor via `packages/polybot/health_server.py` (aiohttp on port 8081). `HealthBeacon` pinged from each main loop iteration. Docker healthchecks call them. All 3 services show `(healthy)` in `docker ps`.
- **B3** TimescaleDB hypertable for `trades` + 180-day retention policy via `0005_trades_retention.py`. Migration is online — converts the existing heap table with `migrate_data=>true`, adds composite PK (id, ts) so the partition column is in every unique constraint.
- **B4** GitHub-repo backup option in `scripts/backup.sh` + new `scripts/push_backup_to_github.sh`. Daily pg_dump optionally pushed to a private GitHub repo (configured via `GITHUB_BACKUP_TOKEN` + `GITHUB_BACKUP_REPO` in .env), 7-dump rotation. Skips silently if vars unset.

Plus new restoration tooling: `scripts/restore.sh` (manual, idempotent, prompts before destructive action).

---

## Production-Readiness Security Fixes (2026-06-07)

Discovered during the same audit pass:

- **B15** GET /admin/kill bypass: the GET handler lacked `Depends(require_admin)` while POST handlers had it. Anyone could read the kill-switch state. **Fixed**: added `dependencies=[Depends(require_admin)]` to the GET handler in `services/api/routes/admin.py:23`. Verified `curl http://localhost:8000/admin/kill` returns 401.
- **B16** trade_ingest watermark race condition: `_set_watermark` was called AFTER `session_scope` committed. If commit failed (transient OperationalError now caught by A1 retry, or hard failure), the watermark was bumped anyway → silent data loss. **Fixed**: moved the watermark write INSIDE the session_scope so it's atomic with the DB commit (`services/ingest/jobs/trade_ingest.py`).
- **CORS lockdown**: replaced `allow_origins=["*"]` + `allow_credentials=True` (OWASP CSRF) with an env-driven `CORS_ORIGINS` list (`services/api/main.py`). Default `http://localhost:3000`. Methods locked to GET/POST, headers to authorization+content-type.
- **ADMIN_TOKEN documentation** + dev-default kept but better commented in `packages/polybot/config.py`. The .env.example already had the right wording.

---

## B01 — Multi-Outcome Mark-Display falsch (FIXED in 0003)

**Wo:** `services/api/routes/positions.py` Zeile ~94-102, `services/executor/pnl_loop.py` Zeile ~63-78.

**Symptom:**
- Bot kauft "LYNN VISION" Token in Markt `[TYLOO, Lynn Vision]`
- Dashboard zeigt mark=0.9995 (=TYLOO's Preis), MTM +$53.91
- In Wahrheit: Lynn Vision Mark=0.0005, MTM ≈ -$3.10
- Display ist FAKE für jedes Outcome das nicht "YES" oder "NO" heißt UND wo der Bot das zweite Outcome gekauft hat

**Root cause:**
```python
tok = (row.yes_token_id if outcome == "YES"
       else row.no_token_id if outcome == "NO"
       else row.yes_token_id)   # ← always-yes fallback ist FALSCH
```
Bei einem Markt `outcomes: [TYLOO, Lynn Vision]` ist `yes_token_id` = TYLOO-Token.
Wenn Bot Lynn Vision kauft, fragen wir aber yes_token_id → bekommen TYLOO-Mark.

**Was NICHT betroffen ist:**
- Der actual fill (paper.py kauft den richtigen Token via market.yes/no_token_id der signal-Engine)
- Realized PnL beim Settle (settle_resolved_markets vergleicht string-basiert `position.outcome == market.outcome`)
- Cost basis (notional × avg_price — kein Token-Lookup)

**Was betroffen ist:**
- Live Mark-Display auf Dashboard
- Live Unrealized PnL im Snapshot
- Live Equity-Anzeige (überschätzt oder unterschätzt je nach welche Seite gekauft)

**Fix:**
1. ALTER markets ADD COLUMN outcomes JSONB
2. market_ingest + market_resolver populieren outcomes aus Gamma payload (`outcomes: '["TYLOO","Lynn Vision"]'`)
3. Lookup: `outcomes.index(position.outcome)` → 0 → yes_token_id, 1 → no_token_id
4. Backfill für die 48 aktuellen Positions (one-shot Gamma fetch)

---

## B02 — Pnl-loop und positions.py duplizieren Token-Lookup-Logik (FIXED)

**Symptom:** Selbe Bug-Logik existiert an 2 Stellen → wir mussten sie doppelt fixen, könnten in Zukunft asynchron werden.

**Fix:** Token-Lookup-Helper in `polybot/market_resolver.py` zentralisieren. Beide Caller importieren `resolve_token(market, outcome)`.

---

## B03 — Old paper fills tracken Fees nicht in realized_pnl (FIXED via backfill)

**Symptom:** Die 2 Fills vom 29. Mai (Hormuz $25, NIP $25) haben jeweils $0.50 Fee, aber `realized_pnl_usdc` = 0. Die 3+ neuen Fills tracken Fees korrekt als -$0.05 each.

**Root cause:** Alte `paper.py` Version (vor Workflow-Refactor) hat Fees nicht ins realized_pnl gebucht. Neue Version tut es.

**Effect:** $1.00 versteckter Verlust nicht in unserer realized-Anzeige. Equity ist um $1 zu hoch.

**Fix:** Backfill-Script das alle Fills bis YYYY-MM-DD durchgeht und fee_usdc zu Position.realized_pnl addiert. Oder ignorieren — $1 ist Rauschen.

---

## B04 — Latency-Lag der gesamten Pipeline ~10-30s (KNOWN LIMITATION)

**Symptom:** WS-Listener fängt keine direkte Wallet-Attribution → wir nutzen WS nur als Burst-Trigger für REST-Poll. Effective Lag: ~10-30 Sekunden zwischen Wallet-Trade und unserer DB-Sichtbarkeit.

**Root cause:** Polymarket-Public-WS exponiert keine maker/taker Adressen — nur anonyme Trade-Prints. Authentifizierte WS wäre per-User, nicht per-Market.

**Workaround in place:** Burst-Detection (≥3 Prints in 30s) → trigger early `/trades?market=` poll → attribuieren tracked wallets dort.

**Real fix:** Würde authenticated WS-Account erfordern (= Polymarket Trader-Account + on-chain wallet anlegen).

---

## B05 — sport_other resolved Märkte hängen in UMA-Dispute-Window (BY-DESIGN)

**Symptom:** Tennis-/CS:GO-Matches sind über, mark=0.001/0.999, aber `markets.closed=false` → kein auto-settle → Positions bleiben open mit fake-extreme-MTM.

**Root cause:** Polymarket UMA Oracle hat 2-72h Dispute-Window nach proposed Resolution. Bot wartet darauf, was korrekt ist.

**Status:** Nicht fixen — `aggressive_settle` Option diskutiert aber nicht implementiert. Wartet ist die ehrliche Strategie.

---

## B06 — risk_yaml.execution.max_orders_per_minute Default zu strikt (FIXED)

**War:** 6/min → bei 9-Signal-Burst wurden 3 verloren.
**Jetzt:** 20/min.

---

## B07 — alerts.risk_rejected_alert signal_id Argument-Mismatch (FIXED)

**War:** Caller übergab signal_id, Funktion akzeptierte nur reason → TypeError bei jedem Risk-Reject Alert.
**Jetzt:** Beide unterstützt.

---

## B08 — pnl_loop equity Sign-Bug auf cash_used (FIXED)

**War:** `1 if Fill.side == "BUY" else -1` als Python-Ternary auf SQLAlchemy Column → kollabierte zu konstant -1 → equity überschätzt um 2× Spent.
**Jetzt:** SQL `CASE WHEN` per-row.

---

## B09 — Initial Alembic-Migration kollidierte mit hand-applied schema (FIXED)

**War:** Workflow-Migration 0002 versuchte ADD COLUMN das schon manuell existierte.
**Jetzt:** `alembic stamp head` ge-flaggt + 0001/0002 bereinigt.

---

## B10 — leaderboard_scraper deactivated alte Wallets pauschal (FIXED)

**War:** Bei transient API-Fehler wurden alle nicht-gescorten Wallets auf is_active=false gesetzt → wir verloren halbes Roster.
**Jetzt:** Nur Wallets die diese Runde re-scored UND nicht im Top-Set deactiviert.

---

## B14 — Executor kauft FALSCHEN Token für alle non-YES/NO outcomes (CRITICAL, FIXED)

**Severity:** Catastrophic — alle Sport-Trades waren auf der GEGENSEITE platziert.

**Symptom (Beweis aus executor logs):**
- Signal: "BUY NIKOLAS SANCHEZ IZQUIERDO" auf Markt [Sanchez, Lautaro Midon]
- Executor queried CLOB book für token_id=69344... (= Lautaro's no_token)
- Recorded fill: outcome=NIKOLAS SANCHEZ IZQUIERDO, price=0.45, shares=8.83
- **Reality:** wir halten Lautaro Tokens (Sanchez gewann → unsere Tokens jetzt wertlos)

**Root cause:** 4 Stellen mit dem buggy Pattern:
```python
token_id = row[0] if outcome.upper() == "YES" else row[1]
```
- `services/executor/paper.py:127` (simulate_fill BUY)
- `services/executor/paper.py:194` (close_position SELL)
- `services/executor/live.py:144` (place_live)
- `services/signals/conditions/liquidity.py:34` (liquidity gate)

Für outcome="SANCHEZ" (non-YES/NO): `"SANCHEZ" != "YES"` → `row[1]` = no_token = Lautaro Token → wir kaufen die OPPOSITE side.

**Impact auf historische Trades:**
- Multi-outcome positions wo bot intended outcomes[0] (= erste Person/Team) → hat tatsächlich outcomes[1] gekauft
- Multi-outcome positions wo bot intended outcomes[1] → zufällig korrekt (no_token matched intent)
- Binary YES/NO: korrekt
- Aggregierter Display-Fehler: **+$300 fake unrealized profit** (real: $35, displayed: $382)

**Fix:**
1. Alle 4 Sites auf `token_for_outcome()` Helper umgestellt
2. One-shot `scripts/fix_b14_swap_outcomes.py`: swap outcome labels von outcomes[0] zu outcomes[1] für alle alten fills (sentinel idempotent)
3. One-shot `scripts/fix_b14_normalise_case.py`: outcome casing auf UPPER normalisiert + paper positions rebuild aus fills um Duplikate zu mergen

**Verifikation nach Fix:**
- Positions reduziert 66 → 50 (Duplikate gemergt)
- MTM korrigiert +$411 → +$70.80 (echter unrealised PnL)
- Equity korrigiert $10,382 → $10,035.67 (= +$35 echter Profit)
- Mary Stoiana (war FAKE +$56): jetzt korrekt -$8.95
- Lautaro Midon (war versteckt unter SANCHEZ): jetzt sichtbar als -$21.43

---

## B13 — Midpoint 404 + Retry-Sturm killt 60% der Mark-Lookups (FIXED)

**Symptom:** 39 von 66 offenen Positionen zeigten `mark=null` auf dem
Dashboard, total MTM = $285 obwohl die echte Summe ~$411 ist.

**Root cause (zwei verkettete Bugs):**

a) `ClobClient.midpoint()` ließ HTTP-Exceptions durch → `best_mark()` kam nie
   zum `/last-trade-price` Fallback. Für resolved-but-pending Markets returnt
   /midpoint 404 ("No orderbook"), und der ganze Pfad bricht ab.

b) `_http.py` retried 4× bei JEDEM HTTPError, inkl. 404 → 4 Calls × Backoff
   ~0.5+1+2+4 s = ~10s pro Position bevor sie aufgibt. Bei `_safe_midpoint`'s
   3-sek Timeout kam nichts mehr durch.

**Fix:**
- `midpoint()` + `price()` schlucken jede Exception → returnen 0.0.
  `best_mark()` sieht dann die 0 und fällt sauber auf `last_trade_price`.
- `_http._req` retried jetzt nur noch bei 5xx + 429 + Connection-Errors,
  nicht mehr bei 4xx (die sind permanent).

**Verifikation:** mark-Coverage von 27/66 (41%) → 52/66 (79%), MTM-Sum von
$285 → $411 (Differenz = die resolved positions deren last-trade-price
endlich abrufbar ist).

---

## B12 — Equity-Formel double-deducted cost basis offener Positions (FIXED)

**Symptom:** snapshot.equity ≈ $9,876 während die echte Equity (= starting + realized + unrealized) ≈ $10,258 ist. Konstanter Bias = sum of cost-basis-of-open-positions (~$350-400 für unsere ~60 offenen Trades).

**Root cause:** `pnl_loop._equity_paper` nutzte:
```python
equity = starting - cash_used + realized + unrealized
```
Aber `cash_used` enthält BUY-notionals der noch offenen Positionen. Die Position-Market-Values (cost + unrealized) wurden NIE als positive Komponente eingerechnet → Cost-Basis wird effektiv doppelt abgezogen.

**Beweis:** Start $100, BUY $30, mark steigt auf $0.40 (10$ MTM). Echte Equity = bank $70 + position-value $40 = $110. Alte Formel: $100 - $30 + $0 + $10 = $80 (off by $30 = cost). Korrekt: $100 + $0 + $10 = $110.

**Fix:** `equity = starting + realized + unrealized` — `cash_used` ist redundant weil `realized` schon alle closed-position-PnL inkl. fees enthält und `unrealized` die MTM-Veränderung der offenen.

---

## B11 — /midpoint returnt "no orderbook" für resolved Markets (FIXED)

**War:** Für jedes resolved-but-pending Market gab /midpoint einen Fehler
zurück → mark wurde auf 0.0 gesetzt → MTM-Beitrag = 0. Dadurch zeigte das
Dashboard für ~50% der offenen Positionen "—" oder 0, obwohl es klare
End-Preise (0.999/0.001) gab.

**Fix:** Neuer `ClobClient.best_mark()` Helper: midpoint zuerst, fallback
auf `/last-trade-price` der auch nach Orderbook-Close noch funktioniert.
Verwendet in `positions.py:_safe_midpoint` und `pnl_loop._equity_paper`.

---

## Offene Improvements (keine Bugs)

- **I01** — Outcomes Cache: für Backtest/Replay alle resolved Markets mit final outcome cachen
- **I02** — `outcome_pnl_usdc` Spalte in signals → später ML-Trainings-Set
- **I03** — Backtest-Engine via Replay der signals + outcomes Tabelle
- **I04** — Liquidity-Gate könnte spread% statt nur depth USDC checken
- **I05** — Wallet-Roster split nach time-decayed Performance (heutiger Top != letzte Woche Top)
