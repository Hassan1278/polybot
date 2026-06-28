# `services/statarb` — Intra-Polymarket statistical arbitrage

A **structural** edge, not a predictive one. We never forecast who wins; we
exploit a price relationship that *must* hold mechanically on a single venue:

> A basket of Polymarket tokens that is guaranteed to pay **$1** at resolution
> should never cost **less than $1**, net of fees. When it does, buying the
> basket locks the difference as risk-free profit.

This fits an execution-trader's instinct (a mechanical relationship to enforce,
like a cash-and-carry basis) rather than a bettor's (calling outcomes). It is
also *access-safe*: one venue, no cross-exchange latency race, no second KYC.

---

## The two relationships we trade

### 1. Binary YES/NO complementarity — `binary_complement_arb`
Every binary market has a YES token and a NO token. At resolution exactly one
pays $1. So **1 YES + 1 NO is a guaranteed $1**. If we can lift both asks for
`ask_yes + ask_no < $1` (net of fees), the gap is locked.
- Always structurally valid — no semantic assumptions, no event metadata.
- The bread-and-butter scan; runs off the local `Market` table.

### 2. Multi-outcome "buy the field" / negative-risk — `field_buy_arb`
A mutually-exclusive, collectively-exhaustive (**MECE**) event with K outcomes
resolves to exactly one winning YES. So **1 YES of every outcome is a guaranteed
$1**. If `Σ ask_yes[k] < $1`, buy the whole field and lock the gap.
- **Correctness precondition:** the outcomes must genuinely be MECE. On
  Polymarket that is exactly the set of events flagged `negRisk=true`. The
  scanner refuses to field-arb anything else — a non-exhaustive group would
  manufacture a phantom edge.

Both reduce to one routine: walk K ask ladders in lockstep, take a "basket" (one
share of each leg) while the marginal basket costs `< $1` net of the proportional
fee, stop when the next basket's edge hits zero or depth runs out. Walking deeper
raises each ask, so there is a natural optimal size. Output is depth-aware and
fee-net — the `net_usdc` is what actually lands, not a top-of-book mirage.

> **Roadmap v2 (not built):** temporal/logical nesting — "resolves YES by June"
> ⊆ "by July", so `P(June) ≤ P(July)` must hold. Needs semantic parsing of
> questions; deferred until the two mechanical scans are validated.

---

## Why this is safe to run

- **Paper-first.** `scanner.py` only *finds and logs* opportunities
  (`statarb_opportunity`). It places **no orders**. We validate that the logged
  edges are real and persistent against the live book before wiring execution.
- **Depth-aware + fee-net.** Edge is computed by walking real orderbook depth and
  subtracting fees/gas — never from a top-of-book quote that 10 shares would eat.
- **Honest fee model.** `fee_bps` defaults to live CLOB reality (**0%**); the
  paper simulator elsewhere models a conservative 2% (`FEE_BPS=200`). The knob is
  explicit so edge is never overstated. `gas_usdc` covers the on-chain
  redeem/merge to realize a complete set.
- **MECE-gated.** Field-arb only fires on `negRisk=true` events.

---

## Structure

```
services/statarb/
├── __init__.py        # public surface: the pure no-arb functions + result types
├── relations.py       # PURE no-arb core — ladders in, ArbOpportunity|None out.
│                      #   binary_complement_arb / field_buy_arb, depth-aware, fee-net.
│                      #   No network, no DB, no clock → trivially + exhaustively testable.
├── scanner.py         # PAPER-FIRST wiring: Market table + Gamma events + CLOB books
│                      #   → candidate baskets → relations.py → logged opportunities.
│                      #   Runnable standalone: `python -m services.statarb.scanner`.
└── README.md          # this file

tests/test_statarb_relations.py   # exhaustive unit tests of the pure core (18 cases)
```

The split is deliberate: **all the math lives in `relations.py` and is pure**, so
correctness is settled by fast unit tests with zero infrastructure. `scanner.py`
holds only the I/O — the part that benefits from polybot's existing clients.

---

## What it reuses from polybot (copied nothing — imports everything)

| Need | Reused from | Why it already fits |
|------|-------------|---------------------|
| Orderbook depth (batch) | `polybot.clients.ClobClient.books()` | One POST fetches every leg's book per pass |
| Single book / marks | `ClobClient.book()` / `best_mark()` | Same 404-swallowing read path the bot trusts |
| Market universe + token map | `polybot.models.Market` | `event_id` groups siblings; `outcomes[i] ↔ clobTokenIds[i]`; `yes/no_token_id` |
| Multi-outcome grouping + `negRisk` | `polybot.clients.GammaClient.events()` | Events carry the child markets and the MECE flag |
| DB session | `polybot.db.session_scope` | Same async session the rest of the services use |
| Structured logging | `polybot.logging.get_logger` | `statarb_opportunity` lines drop into the existing log pipeline |
| (next) Execution | `services/executor` `live.place_live` + `risk.preflight` | Venue-truth sizing, kill-switch, caps — reused, not reinvented |

No code was copied or forked; the package imports the shared libraries directly.
That keeps one source of truth now and makes the later extraction a clean cut.

---

## Roadmap

1. **✅ Pure no-arb core + tests** — `relations.py`, `tests/test_statarb_relations.py`.
2. **✅ Paper scanner** — `scanner.py`, binary + negRisk-field, observe-only.
3. **▶ Validate** — run the paper scan against the live book; confirm logged
   edges are real and survive the time it takes to lift both legs (the only real
   risk here is *execution* risk: one leg fills, the other moves).
4. **Execution** — atomic basket lift through `executor.live.place_live` under
   `risk.preflight`; partial-fill / one-leg-hung handling; complete-set
   redeem/merge to realize. Live-gated + kill-switch-aware like every other path.
5. **Extract** — once proven, lift `services/statarb/` into the standalone repo
   `Miyokuna/polymarket-statarb` via `git subtree split` (history preserved),
   keeping polybot's clients as a thin dependency or vendored shim.

---

## Run it (paper)

```bash
python -m services.statarb.scanner            # one binary + field pass, logs + summary
python -m services.statarb.scanner --binary   # binary only
python -m services.statarb.scanner --loop     # continuous (30s)
python -m pytest tests/test_statarb_relations.py -q
```
