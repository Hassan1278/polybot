# Known Bugs & Improvements — Polybot

Stand: nach Manual-Audit am 2026-06-05. Reine Bestandsaufnahme — Reihenfolge ist
nach Priorität, alle bisher noch UNFIXED.

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
