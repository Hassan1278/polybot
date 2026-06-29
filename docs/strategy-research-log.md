# Polymarket Strategy Search — Research Log

**Purpose.** A chronological, honest record of every mechanical-edge hypothesis we
tested on Polymarket (and, later, cross-venue vs Limitless): the hypothesis, how we
tested it, the actual result, and the verdict. Written so the investigation can be
picked up cold — especially the cross-venue thread.

**Scope.** We were looking for a *mechanical* (rule-based, non-discretionary)
positive-EV strategy runnable by a small retail operator with **no speed edge and no
private information**. Paper capital was set to **$300** (`packages/polybot/config.py:
paper_starting_usdc`). All work is observe-only / paper unless noted.

---

## TL;DR — the result the whole search converged on

> With **no information edge and no speed edge**, there is **no mechanical
> positive-EV strategy** on these venues — not taking liquidity, not making it.
>
> - **Taking** liquidity → every cross we found is arbed flat (threads 1–7).
> - **Making** liquidity → the spread you earn is *exact compensation* for adverse
>   selection; the reward subsidy (~10% APR best case) does not cover the gap risk.
> - **Predicting** with your own *public* external data → the market already aggregates
>   the same forecasts *better* than a single model can (thread 9: on weather it
>   out-predicts us 43% vs 32%). No accuracy edge ⇒ nothing to monetize.
>
> The only participants who profit have an edge the bot doesn't: **prediction**
> (we proved the crypto windows are a coin flip *and* that public weather models lose
> to the market), **speed** (HFT we don't have), or **private information** (the
> "informed minority"). Everyone else is a gambler.

This is not a failure of effort — it is the measured, repeated answer. It matches the
prior intuition that "the only profitable people on Polymarket are market makers, an
informed minority, or a gambler."

---

## Summary table

| # | Thread | How tested | Verdict |
|---|--------|-----------|---------|
| 1 | Intra-Polymarket statistical arb (binary + field) | `services/statarb/` no-arb scanner | **Efficient** — boundary ≈ 1.001, no gap after fees |
| 2 | Favorite–longshot / calibration bias | `scripts/calibration_backtest.py` | **Calibrated** — null at n≈3226 |
| 3 | Crypto $-strike fair value (lognormal N(d2)) | `scripts/crypto_fairvalue_backtest.py` | **Noise** — market ≈ model fair |
| 4 | Crypto "Up/Down" momentum fade | `scripts/crypto_momentum_backtest.py` | **Null** — already priced at 0.50 |
| 5 | 15-min BTC directional prediction | 13-agent research workflow | **Coin flip** net of costs |
| 6 | Orderbook-imbalance (OBI) signal | same research workflow | **HFT-only** — decays in seconds |
| 7 | Cross-venue arb (Polymarket ↔ Limitless) | `scripts/cross_venue_divergence.py` | **Dead** — noise + non-crossing books + different oracle |
| 8 | LP rewards / market-making | 2-agent program research | **Thin & conditional** — ~10% APR, wiped by adverse selection |
| 9 | Weather temp ladders — external-data / own-model | `scripts/weather_{truth,grade,market_full}.py` + Open-Meteo | **Market is the better forecaster** — Brier 0.104 vs 0.089, argmax 32% vs 43%, edge <0 even at 0¢ |

---

## Chronology

### 1 — Intra-Polymarket statistical arbitrage  → *efficient*
**Hypothesis.** A binary market's YES + NO should cost ≈ \$1; a multi-outcome event's
full field (negRisk "buy the field") should cost ≈ \$1. Any deviation below \$1 is a
risk-free lock.

**Method.** Built `services/statarb/`:
- `relations.py` — pure no-arb core: `binary_complement_arb`, `field_buy_arb`,
  `_field_walk`, `asks_from_book`, `best_ask`, `ArbOpportunity` (unit-tested in
  `tests/test_statarb_relations.py`).
- `scanner.py` — paper scanner: `_fetch_book_index` (chunked `/books`, singular
  fallback), `scan_binaries` + near-miss reporting, `scan_field` grouped by `event_id`
  with an `_event_meta` completeness gate (`field_min_coverage` 0.98 so an incomplete
  field can't fake an arb).
- `persistence.py` — `PersistenceTracker` (clock-injected, tested).

**Result.** The cheapest achievable field/binary sums sit at a boundary of ≈ **1.001**
— i.e. just *above* \$1 once you walk real asks. No exploitable gap survives fees.

**Verdict.** Efficient. No risk-free arb inside Polymarket.

---

### 2 — Favorite–longshot bias / calibration  → *calibrated*
**Hypothesis.** Prediction crowds overprice longshots and underprice favorites, so
price ≠ realized frequency in a systematic, fadeable way.

**Method.** `scripts/calibration_backtest.py` — `resolved_yes`,
`outcome_from_history` (terminal price → outcome), `sample_at_fraction`,
`calibration_table`. Bucket markets by price, compare bucket price to realized
YES-rate over a large resolved sample.

**Result.** Realized YES-rate tracks price across buckets (well-calibrated). A faint
mid-range hint existed but **washed out within noise at n ≈ 3226**.

**Verdict.** Calibrated. Null.

---

### 3 — Crypto $-strike fair value  → *noise*
**Hypothesis.** "Will BTC be above \$X at time T" markets misprice vs a proper
lognormal fair value derived from spot and realized vol.

**Method.** `scripts/crypto_fairvalue_backtest.py` — `bs_prob_above(spot,strike,
t_years,sigma)` = N(d2) = P(S_T > K); `fair_value`; `realized_vol`;
`follow_model_edge` (P&L of betting toward the model); `divergence_table` (binned
market-vs-model error). Reuses the asset/strike parsing in
`packages/polybot/asset_direction.py`.

**Result.** Market price ≈ model fair value; the divergence is noise and following the
model yields no edge.

**Verdict.** Efficient. Noise.

---

### 4 — Crypto "Up or Down" momentum fade  → *null*
**Hypothesis.** Short-window "X Up or Down" markets are martingales (fair ≈ 0.50), and
crowds *chase* momentum — so after a run, continuation is overpriced and you fade
toward 0.50.

**Method.** `scripts/crypto_momentum_backtest.py` — sample the "Up" price, read the
outcome from the terminal price, pair against fair = 0.50, and measure the realized
P&L of fading the lean (reuses thread-3 machinery).

**Result.** These markets are **already priced at 0.50** — the crowd doesn't lean, so
there's nothing to fade.

**Verdict.** Null (priced at fair).

---

### 5 — 15-minute BTC directional prediction  → *coin flip*
**Hypothesis (operator's strong prior).** BTC direction over 15-minute windows is
predictable "much more reliably."

**Method.** A multi-agent research workflow (≈13 agents: fan-out literature/data
research → adversarial verification → synthesis) on (a) 15-min BTC up/down
predictability and (b) crypto orderbook-imbalance alpha.

**Result.** Net of transaction costs, **15-min direction is a coin flip.** Any raw
signal is below the cost to trade it.

**Verdict.** Coin flip net of costs.

---

### 6 — Orderbook-imbalance (OBI) signal  → *HFT-only*
**Hypothesis.** Orderbook imbalance predicts the next short-term move.

**Method.** Part of the same research workflow.

**Result.** OBI alpha is real but **decays in seconds** — it belongs to
latency-advantaged HFT (colocation, sub-ms requote). Inaccessible to us.

**Verdict.** HFT-only.

---

### 7 — Cross-venue arbitrage: Polymarket ↔ Limitless  → *dead* (3 nails)
**Constraint.** The second venue had to be **non-KYC** (Kalshi is out as a non-US
operator). Chose **Limitless Exchange** — a CLOB prediction market on **Base** (chain
8453, USDC) running the same short-dated crypto Up/Down products.

**Hypothesis.** The *same* crypto Up/Down window priced on two venues should diverge —
either a risk-free lock (buy Up cheap on one, Down on the other) or a systematic
statistical edge.

**Method & evolution** (`scripts/cross_venue_divergence.py`, branch
`claude/happy-ramanujan-5z3lfo`):

1. **Relative-divergence framing.** Measured divergence in **log-odds (logit) space**,
   not absolute cents (a 0.025-vs-0.006 split is a ~4× divergence; 0.685-vs-0.655 is
   agreement). Decision metric: is the divergence **systematic** (|mean| > 2·se over
   distinct pairs → one venue consistently richer, fadeable) or **symmetric noise**?
2. **Matching.** Pair markets by **asset + duration + resolution second**. Added
   `_duration_seconds` so a 15-min and an hourly window that merely share an end aren't
   matched. Observed **skew = 0** on every pair — both venues use *identical*
   clock-boundary UTC resolution timestamps. The windows are genuinely the same bet.
3. **First result was an ARTIFACT.** The naive read used Limitless's inline
   `prices` field and showed a big "SYSTEMATIC" lean. **`prices` is a seed
   placeholder, not a quote:** a market advertising `prices=[0.49, 0.51]` had a real
   order book of **bid 0.25 / ask 0.48, midpoint 0.365, `lastTradePrice: null`**. The
   "edge" was the 0.49 placeholder vs Polymarket's actually-traded price. Rebuilt to
   read **real order books on both venues** (`best_bid_ask`, `_limitless_book`,
   Polymarket CLOB `book`).
4. **Executable-cross metric.** Since the same window's Up pays \$1 on both venues iff
   the asset is up, buying Up cheap on one venue + Down on the other locks \$1. With
   each venue's binary tight, this reduces to a **book cross**:
   `edge = max(poly_bid_up − lim_ask_up, lim_bid_up − poly_ask_up)`. `>0` (net of
   fees/gas) = a real lock.

**Result (real books, sample of matched pairs):**
- **Mid divergence = symmetric NOISE.** mean log-odds Δ **−0.036 ±2se 0.131**, median
  |Δ| 0.068, LIM-higher 60%. When both venues are liquid they *agree*
  (XRP 0.298/0.295, SOL 0.477/0.485, ETH 0.135/0.135).
- **Books don't cross.** EDGE negative on 9 of 10 pairs. The single "+0.010" was one
  tick on a deep-OTM market — an artifact of fetching the two books ~100 ms apart
  (non-atomic snapshot) plus tick granularity; it does not grow with more samples.
- **Spread structure explains why.** Limitless spreads ran **2–92¢** (often
  token-wide / untraded) vs Polymarket ≈ **1¢**. Where Limitless is tight it agrees
  with Polymarket (no edge); where it's wide you can't execute.

**The oracle nail (the linchpin).** Both venues resolve on "Chainlink BTC/USD" — but
**different Chainlink streams**:
- Limitless → `data.chain.link/streams/btc-usd-cexprice-streams` (CEX-price stream),
  comparing the price at window-end vs window-start.
- Polymarket → `data.chain.link/streams/btc-usd` (standard aggregator).

Because these windows are near coin-flips, when the true move is smaller than the
*basis between the two feeds*, the feeds can resolve **opposite**. The "lock"
(Up\@Limitless + Down\@Polymarket) then pays **\$2 (lucky) or \$0 (lose the whole
stake)** instead of a guaranteed \$1. Different oracles convert a "risk-free arb" into
a **positive-EV-but-fat-tailed basis trade** — not worth it for a few cents of gross
edge.

**Verdict.** Dead, three independent nails: (1) mid divergence is noise, (2) the books
never cross, (3) different oracles = basis risk even if they did.

---

### 8 — LP rewards / market-making (the "makers win" pivot)  → *thin & conditional*
**Hypothesis.** The one inefficiency that kept showing up is the **spread itself** —
which is the **maker's income**, not a taker's edge. Capture it via market-making,
subsidized by venue liquidity-reward programs.

**Method.** Two parallel research agents on the Polymarket and Limitless reward
programs (existence, formula, magnitude, eligibility, adverse-selection reality).

**Polymarket Liquidity Rewards.**
- Live and expanded in 2026 (CLOB v2 launch, \$1M one-off pool).
- Formula: quadratic proximity-to-mid score `S(v,s) = ((v−s)/v)²` (v = max qualifying
  spread, ≈ 3¢), two-sided-weighted (`Q_min` with a single-sided penalty divisor
  c ≈ 3), order book **sampled once per minute**, paid **pro-rata** from a per-market
  daily pool (≈ \$500–\$5k/day), settled daily in **pUSD** (\$1/day minimum).
- Eligibility: **non-US / non-KYC wallets qualify**; the only gate is **IP-geoblock**
  (location-based). (Moot here — we already trade Polymarket.)
- Yield: early farmers saw 700%+ annualized; competed away. **Realistic standing yield
  today ≈ 10% APR**, and only on *calm, long-dated* markets. Big reliable pools sit in
  sports/politics — **not** short-dated crypto (thin pools, worst adverse selection).
- The catch: the formula **forces you to the midpoint**, where you're maximally picked
  off. Prices gap 40–50 points in seconds on news; **one event can erase weeks** of
  rewards. No credible source shows a *no-alpha* mechanical quoter net-profiting in
  active markets. Practitioner consensus: *"a bonus, not a strategy."* (~84% of wallets
  are unprofitable; the top 1% capture ~76% of profits.)

**Limitless incentives.**
- LP rewards exist (a small daily USDC pool, ~\$1k/day and possibly *platform-wide*,
  unconfirmed) **plus** an airdrop points program (LMTS already down ~80%+ from ATH).
- **No maker rebate** — makers just trade fee-free; takers pay 0–3%.
- Non-KYC fine; **US is geo-blocked by ToS** (non-US OK).
- Two killers: (1) the venue runs its **own team-controlled market maker** as the
  dominant quoter → external fills near mid are uncertain (crowd-out); (2) **severe
  adverse selection** on 15-min books — the **23–92¢ spreads we measured are
  themselves the signal** that tight quoting gets picked off faster than the subsidy
  pays. Quote tight enough to earn the multiplier and you go **net-negative**.

**Verdict.** No-edge mechanical making is break-even-to-negative; the subsidy is too
thin to cover adverse selection. At best, Limitless is a low-conviction *airdrop
points-farm*, and Polymarket rewards are a *yield overlay* that only works on top of a
real pricing or latency edge.

---

### 9 — Weather temperature markets: external-data / own-model edge  → *market is the better forecaster*
**The pivot.** Threads 1–8 hunted for mispricing the market itself reveals. This thread
tested the operator's own framing: *don't out-trade the market, out-predict it* — "bet
on the event most likely to win **regardless of price**" (beta, not alpha) — using
**external data the crowd supposedly underuses**. Polymarket runs daily per-city
temperature ladders (1°C / 2°F buckets; the day's high lands in exactly one bucket — a
partition) that resolve on **Weather Underground** at a specific airport station
(e.g. NYC=LaGuardia, Paris=Le Bourget, London=London City). Operator's thesis: find the
**margin of failure** of the forecasts, and whether the WU resolution source diverges
from reality enough to exploit — *not* to build a better model from scratch.

**Method** (all DB-free — the bot's own `fills` table was corrupt, see note):
1. **Ground truth** (`scripts/weather_truth.py`). Enumerate resolved weather markets
   from the gamma catalog, batch-resolve via gamma `condition_ids` (a **repeated** query
   param, 20/call — ~20× fewer requests, kills the 429 storm), reassemble each city-day
   ladder, take the single YES bucket = where the high landed. **636 city-day ladders
   over 14 days, median 7 buckets/ladder, winner present in 628/636 (only 1.3% missing)**
   → the clean subset is unbiased. Physically validated: Paris 41°C / London 36°C on
   Jun 24–26 = the real late-June 2025 European heatwave; Wellington/Buenos Aires/São
   Paulo show Southern-Hemisphere winter highs.
2. **Forecast grading** (`scripts/weather_grade.py`). Open-Meteo **Previous Runs API**
   gives the forecast *as issued 24h/48h ahead* (hourly `temperature_2m_previous_day1/2`,
   daily max derived — no daily aggregate exists on that endpoint). **ICON is the best
   public model (~1.1°C MAE @ 24h)**; ERA5 reanalysis = "actual." **WU runs ~0.53°C
   hotter than ERA5** — a real, idiosyncratic station bias (confirming the thesis).
3. **Money test** (`scripts/weather_market_full.py`). Turn ICON's 24h forecast (+0.4°C
   WU-hot correction) into a Gaussian (σ=1.4°C from the measured error) → a forecast
   probability for **every bucket**. Price **every bucket** from CLOB price history at
   24h before resolution (throttled 8/s + disk-cached so the ~3.5k-request run is
   rate-limit-free and resumable). Compare forecast vs market three ways over **3,555
   buckets / 465 ladders**.

**Result — the market wins on every axis:**
- **Calibration (Brier, lower = better):** forecast **0.1037** vs market **0.0894**
  → the **market is the better per-bucket predictor** (~16% lower error).
- **Argmax accuracy (the "most likely to win" test):** the forecast's top bucket won
  **32.3%**; the market's top bucket won **43.4%** (n=465 ladders) → **the market picks
  the winner far more often.** This kills the *beta* premise directly: our public-data
  model is a *worse* forecaster than the crowd, before price even enters.
- **Realized edge (bet buckets the forecast thinks are underpriced by >5pts):**
  **negative even at zero cost** — all −0.013 ±0.016, low-volume **−0.024 ±0.021**
  (worse), uniformly NEGATIVE once any spread/fee haircut applies. Hit rate 4–6% on cheap
  (~6¢) longshots: the model systematically *overrates* far-from-mode buckets.

**Why.** (1) The market already ingests the same public forecasts — and better ones:
multi-model ensembles, and *nowcasts* closer to resolution that we, with no speed edge,
can't race. (2) The WU station bias is **already priced** — we baked +0.4°C into the
forecast and still lost. (3) There's a **physical ceiling**: 24h forecast error
(~1.1°C) ⊕ WU idiosyncratic noise (~0.8°C) ≈ 1.36°C combined → the central 1°C bucket
captures the high only ~30% of the time, matching our 32%. The market's 43% means it is
using information beyond a single 24h public model. A retail operator cannot beat that
with free model data.

**The illiquid-tail hope, refuted.** The one place an edge could plausibly hide — thin,
low-volume buckets — is where the forecast does **worst** (−0.024 vs −0.003 high-volume,
at zero haircut). Those are far-tail longshots the model overrates, and the spread is
widest exactly there.

**Verdict.** Efficient — and more pointedly, **the market is a strictly better forecaster
than our external-data model**, so the approach fails at the *accuracy* (beta) level
before cost even enters. "Bring your own *public* data" does not beat a liquid prediction
market that already aggregates it.

**Data-integrity note.** The bot's `fills`/DB record for weather was **corrupt**
(Data-API truth: 48 trades / \$15 BUY vs DB 447 / \$1158 BUY, and 29 phantom DB-only
markets), so this whole thread was rebuilt from independent gamma/CLOB/Data-API +
Open-Meteo. Treat the live `fills` table as unreliable for weather analysis.

---

## Synthesis

Seven taking-side threads are each efficient; the making side pays a spread that
exactly compensates adverse selection; and the "bring your own data" prediction side
(thread 9) loses to the market's own aggregate forecast. The unifying theorem (see
TL;DR): **no information edge + no speed edge ⇒ no mechanical +EV strategy — taking,
making, or predicting.**

Thread 9 sharpened the theorem in an important way. The operator's most promising-sounding
idea was *beta, not alpha*: skip the market's price and just predict the outcome better,
using external data the crowd "underuses." But a liquid prediction market **is** an
aggregate forecast, and on weather it beat a calibrated single-model forecast outright
(43% vs 32% at picking the winner). The lesson: **public** external data is already in
the price. An accuracy edge requires data the market *doesn't* have.

The **one** mechanical +EV thing still standing is a **reward-overlay quoter** on
*calm, long-dated* Polymarket markets with **cancel-on-move** to dodge pick-offs — a
~10% APR, capital-hungry, competitive yield-farm. Real, measurable, scalable; but a
yield-farm, **not alpha**.

Anything with a higher ceiling requires an **edge the bot doesn't have**: prediction
(disproven for crypto windows *and* for public-data weather forecasting), speed (HFT),
or **private information** on specific markets — the last being the only one a non-speed
retail operator can realistically supply, and only by stopping the hunt for a black-box
and instead *amplifying their own topical knowledge* with breadth + calibration +
disciplined sizing.

---

## Appendix — reusable facts (for continued cross-venue research)

**Limitless API** (`https://api.limitless.exchange`)
- `GET /markets/active?page={n}&limit={≤25}` — **must** pass `page`; `limit` capped at
  25 (50 returns empty). Inline `prices:[up,down]` is a **SEED placeholder**, *not* a
  tradeable quote — ignore it.
- `GET /markets/{slug}/orderbook` → `{bids:[{price,size,side}], asks:[…], midpoint,
  adjustedMidpoint, maxSpread, minSize, lastTradePrice, tokenId}`. **Use this for real
  prices.** `lastTradePrice: null` ⇒ untraded window.
- Base (8453), USDC (`0x8335…2913`). Maker fee 0; taker 0–3%. Resolves on Chainlink
  **`btc-usd-cexprice-streams`** (window-end vs window-start).

**Polymarket Gamma** (`https://gamma-api.polymarket.com`)
- `GET /markets?active=true&closed=false&end_date_min=…&end_date_max=…&order=endDate
  &ascending=true&limit=500` — date window is required (the `active` flag alone returns
  stale never-closed markets).
- `outcomePrices` and `clobTokenIds` and `outcomes` come back as **JSON strings**;
  `outcomes` for these is `["Up","Down"]`, so index 0 = Up. Live book via the CLOB
  (`/book`, `/midpoint`, `/price`) — **not** the gamma `outcomePrices` snapshot.
- Resolves crypto Up/Down on Chainlink **`btc-usd`** (standard stream).

**Window alignment.** Both venues quote 5-min / 15-min / hourly / daily Up/Down on
**clock-aligned UTC boundaries** — matched pairs showed **skew = 0**. The grids are
*not* fully 1:1 (e.g. Polymarket runs 5-min windows where Limitless runs 15-min), so
match on **asset + duration + resolution-second**.

**Divergence metric.** Compare probabilities in **log-odds**:
`logit(p) = ln(p/(1−p))`. A sample is *systematic* if `|mean(Δlogit)| > 2·se`, else
symmetric noise. Code: `relative_divergence`, `divergence_summary` in the scanner.

**What would have to change for cross-venue arb to live:**
1. **Same oracle** — both venues resolving on the *identical* Chainlink stream and
   timestamps (today they differ → basis risk). This is the dominant blocker.
2. **Crossing books** — one venue's best bid for Up above the other's best ask, net of
   fees + gas, in executable size (today Limitless quotes far too wide).
3. **Cross-chain leg risk** — Polymarket settles on Polygon, Limitless on Base; the two
   legs cannot be executed atomically, so there is fill/timing risk between them.

**Scanner.** `scripts/cross_venue_divergence.py` (+ `tests/test_cross_venue_
divergence.py`) computes both the executable-cross EDGE and the mid divergence from
real books; run on the VPS:
`docker compose exec -T executor python -m scripts.cross_venue_divergence`
(append `--loop --interval 30` for a rolling sample). Pure core is unit-tested; the
venue I/O is integration-only (the dev sandbox blocks `api.limitless.exchange` at the
egress proxy, so it must run where the bot runs).

---

## Appendix — reusable facts (weather thread 9)

**Polymarket weather markets.** Per-city, per-day **temperature ladders**: 1°C (or 2°F)
buckets, plus open-ended `"X°C or below"` / `"X°C or higher"` ends; the daily high lands
in exactly one bucket (a partition, so YES-probabilities across a full ladder sum to 1).
Two kinds per city-day: `highest` (daily high) and `lowest` (daily low) — we tested
`highest`. Resolution source is mostly **Weather Underground** at a named airport station
(NYC=LaGuardia, Paris=Le Bourget, London=London City; minority on Hong Kong Observatory /
NOAA). Each bucket is its own gamma market with a `conditionId` and `clobTokenIds`.

**Gamma batch resolution.** `/markets` takes `condition_ids` as a **repeated** query param
(`condition_ids=a&condition_ids=b&…`), *not* a CSV — pass a Python **list** so httpx emits
repeated keys (a `",".join(...)` is read as one malformed id → 0 matches). ~20 ids/call
keeps the URL safe and cuts requests ~20×. Add `closed=true` to surface settled markets
(gamma's default `closed=false` hides them). `outcomePrices`/`clobTokenIds` come back as
JSON strings; YES = index 0; a market is resolved when `closed=true` and `outcomePrices[0]
> 0.5` ⇒ YES won.

**Open-Meteo — forecast *as it was issued*.** **Previous Runs API**
(`https://previous-runs-api.open-meteo.com/v1/forecast`): hourly
`temperature_2m_previous_day1` / `_previous_day2` = the run issued 24h / 48h ahead. **No
daily aggregate exists on that endpoint** — request hourly and derive the daily max
yourself. Models: `icon_seamless` (best, ~1.1°C MAE @ 24h), `gfs_seamless`,
`ecmwf_ifs025` (note `ecmwf_ifs04` returns no data). **Archive/ERA5** "actual"
(`https://archive-api.open-meteo.com/v1/archive`, `daily=temperature_2m_max`) lags ~5
days. **Multi-location batching:** comma-separated `latitude`/`longitude` returns a list
(one object per location) — collapses N calls into a few; mind the per-call rate limit.

**CLOB price history.** `clob.price_history(token, interval="max", fidelity=60)` returns
`{"history":[{"t":unix,"p":price}]}` — a **dict**, not a bare list; unwrap
`raw["history"]`. To price a bucket at "24h before resolution," sample the last point at
or before `end_ts − 24*3600`.

**Measured constants (Jun 2025, ~250–636 city-days).** ICON 24h MAE ≈ **1.1°C**; WU
station reads ≈ **+0.53°C hotter than ERA5** (idiosyncratic, real); decompose WU =
reality + ~0.53° systematic + ~0.8° noise ⇒ single 1°C-bucket precision is capped at
~30% even with a perfect-mean forecast. Gaussian(μ=ICON+0.4, σ=1.4) is well-calibrated to
this.

**Scripts** (all branch `claude/happy-ramanujan-5z3lfo`, pure cores unit-tested; all I/O
runs on the VPS because the dev sandbox blocks external APIs at the egress proxy):
`weather_truth.py` (ground truth + ladder completeness), `weather_grade.py` (model MAE /
bias / WU-vs-ERA5 fidelity), `weather_market_full.py` (the decisive money test — Brier
calibration + argmax accuracy + cost-swept edge over every bucket). Run:
`docker compose exec -T executor python -m scripts.weather_market_full --days 14 --rate 8`
(first run ~5 min, cached + resumable).
