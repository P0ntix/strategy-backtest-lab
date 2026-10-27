# The Eight Stages — Implementation Reference

Each stage lists what it does, where it lives, and what it deliberately does NOT
do (i.e. what bias it cannot remove).

---

## Stage 1 — Data layer
**Code:** `fetch_yahoo_chart`, `load_or_fetch`, `fetch_batch`, `audit_panel`, `DEFAULT_COSTS`

- Source: Yahoo `v8/finance/chart` — free, no key, supports equities, ETFs,
  indices (`^VIX`), FX (`EURUSD=X`), futures (`ES=F`, `GC=F`), crypto
  (`BTC-USD`), yields (`^TNX`).
- **Dividend adjustment.** Yahoo's `indicators.quote` closes are
  split-adjusted but NOT dividend-adjusted, so every ex-dividend date looks
  like a real loss. The engine reads `indicators.adjclose` and back-adjusts
  OHLC by the per-bar factor `adjclose/close`, which reproduces Yahoo's
  adjusted series exactly (the factor is a step function; `adjclose` is
  continuous). Volume is not scaled. A malformed factor (0, or absurd) is
  rejected rather than applied. **Intraday (60m) has no `adjclose`, so
  intraday bars remain unadjusted** — an ex-dividend date inside a 60m
  sample leaves a small uncompensated drop.
- Cached to `.cache/<symbol>__<interval>.json`, refreshed after 3 days
  (daily) or 1 day (intraday). Cached first means repeat runs are offline and
  instant. The cache carries a schema marker, so a daily cache written
  before the dividend adjustment existed is refetched rather than silently
  reused.
- `audit_panel` flags: <120 bars, >12-day calendar gaps, non-positive prices,
  >30% zero-volume bars, ≥8 identical consecutive closes (stale feed),
  single-bar moves >55%.
- Costs are assigned per asset class from `DEFAULT_COSTS`:
  | class | commission | half-spread | slippage | borrow (annual) |
  |-------|-----------|-------------|----------|-----------------|
  | etf | 0.5 bp | 1.0 bp | 1.0 bp | 10 bp |
  | index/future | 0.2 bp | 0.5 bp | 0.5 bp | 5 bp |
  | fx | 0 bp | 0.5 bp | 0.5 bp | 15 bp |
  | crypto | 10 bp | 5.0 bp | 5.0 bp | 30 bp |

**Cannot remove:** point-in-time fundamentals (Yahoo gives adjusted prices only),
intraday microstructure, delisted securities.

### The session path (Stage 1, intraday)

When `execution.mode` is `"session"`, Stage 1 is served by
`load_or_fetch(symbol, "60m")` instead of the daily fetch. Three provider
facts shape it:

- **Yahoo caps 60-minute history at roughly 2 years.** The request is
  `?range=2y&interval=60m`, so the sample is bounded by the provider, not by
  the spec's `period.start`. Statistical power, not signal quality, is the
  binding constraint (~500 sessions).
- **A full-range intraday request is rejected with HTTP 422.** The daily
  path uses `period1=0&period2=9999999999`; the intraday path *must* use an
  explicit `range`. Do not "fix" the intraday URL to a full range.
- **Intraday bars are anchored to exchange-local time.** The 09:30 ET bar
  is 13:30 UTC in summer (EDT) and 14:30 UTC in winter (EST). A fixed-UTC
  session filter would silently break at every DST transition.

Session boundaries are found by **grouping the bars on their UTC calendar
date** and taking the first bar of each group. This is DST-robust and needs
no timezone database — and, on a Windows host, no dependency on the Windows
tz database either. The first (09:30 ET) and last (15:30 ET) bars of a
session always fall on the same UTC date as the exchange session, so the
grouping recovers the opening window in both EDT and EST.

The intraday cache is also treated differently: it goes stale after **1 day**
rather than 3, and older cache files that stored date-keyed rows are
refetched, because one key per day cannot represent multiple intraday bars.

Full detail, including the DST-hour assertion, is in
`references/intraday-session-mode.md`; run `scripts/intraday_probe.py`
before any intraday backtest.

## Stage 2 — Universe + panel
**Code:** `UNIVERSE_PRESETS`, `build_panel`, `Panel`, `infer_asset_class`

- Master date axis = union of all symbols' dates, filtered to those with ≥50%
  coverage, then restricted to `period.start`/`period.end`.
- Non-trading days are forward-filled; volume carries forward. Missing bars are
  never interpolated forward into a price.
- **A forward-filled price is marked, never traded.** The panel records which
  bars are stale (the symbol printed no observation on that date). The
  position is *valued* at the last known price — that is how you mark a
  position you cannot trade — but **no order is filled on a stale bar**.
  Without this, a halted or delisted name traded indefinitely at a phantom
  price. Dropped orders are counted in `sim["stale_skips"]`, never silently.
- `screen.min_adv_usd` removes symbols below a median dollar-volume floor.
- Presets span: `us_equity_broad`, `us_equity_sector`, `us_equity_style`,
  `futures_broad`, `fx_major`, `fx_emerging`, `commodities`, `crypto`,
  `rates_credit`, `intl_equity`, `real_assets`, `multi_asset`,
  `risk_parity_ish`, `risk_on_risk_off`.

**Cannot remove:** survivorship bias on individual equities (Yahoo serves
current listings only). Use ETFs.

### The session path (Stage 2)

`build_session_panel` (`scripts/intraday.py`) replaces the daily panel
builder:

- Alignment is over **sessions**, not dates. A session is kept only when at
  least half the universe is present (`max(1, int(0.5 × N))`), the same
  coverage floor as the daily path, so one short-lived symbol cannot shorten
  the axis for everyone else.
- Fewer than 60 aligned sessions raises `RuntimeError` — below that the
  sample cannot support the gauntlet.
- The result is a **daily-shaped panel**: one row per session date. This is
  why the session path produces DAILY returns and annualises at **252**, not
  at a per-hour rate. Every metric, the walk-forward fold layout and the
  Sharpe annualisation are unchanged from the daily pipeline.
- The liquidity screen is **not** applied on this path; `screened_out` is
  reported as empty for session runs.
- `period.interval` is ignored — 60-minute bars are fetched regardless.


## Stage 3 — Features
**Code:** `FEATURES` registry — `return`, `momentum`, `sma_gap`, `volatility`,
`vol_ratio`, `zscore`, `rsi`, `max_drawdown`, `skew`, `volume_ratio`,
`bollinger_pct`, `atr_pct`, `ma_crossover`, `dist_to_high`

Every feature obeys the causality contract: **value at index `i` uses only data
at indices ≤ `i`**. Each is a pure function of `(panel, symbol, **params)`.

`direction: -1` in params inverts the feature (e.g. a contrarian read of
momentum) without a second feature definition.

Cross-sectional normalisation is handled by the signal, not the feature, so a
feature's meaning stays stable.

### The session path (Stage 3)

`scripts/intraday.py` registers three extra features into the same
`FEATURES` registry when `execution.mode` is `"session"`:

| feature | formula | notes |
|---------|---------|-------|
| `session_drift` | `((close − low) / (high − low) − 0.5) × volume` | where the session closed within its range, centred on zero, scaled by volume. `lookback > 1` averages the raw drift over the trailing `lookback` sessions |
| `session_gap` | `open[i] / close[i−1] − 1` | this session's open against the previous session's close |
| `session_return` | `close[i] / open[i] − 1` | the opening window's open-to-close return |

The registration happens inside the session branch of `run_spec`, so these
names exist only once a session run has happened in the process. A daily-mode
spec naming one of them raises `KeyError: Unknown feature`.

The same causality contract holds: the value at index `i` uses bars `<= i`.
A session's close is known at 10:00 ET, comfortably before the next
session's open, so a signal from session `i−1` can be traded in session `i`.

## Stage 4 — Signal logic
**Code:** `SIGNALS` registry

| type | params | output |
|------|--------|--------|
| `cross_sectional` | `long_rank`, `short_rank` | long top `(1-long_rank)`, short bottom `short_rank` |
| `time_series_threshold` | `threshold` | +1 when feature > threshold, else flat |
| `time_series_zscore` | `entry`, `exit` | mean-reversion: fade \|z\|>entry, exit at \|z\|<exit |
| `crossover` | — | uses the binary `ma_crossover` feature directly |
| `constant` | `weight` | buy-and-hold baseline |

Ranks are recomputed **per date** across the panel, so a signal never sees the
future composition of the universe.

## Stage 5 — Portfolio construction
**Code:** `construct_target_weights`

The order of operations is load-bearing. It is:

1. **Raw signal** per symbol (shorts zeroed if `long_only`).
2. **Optional inverse-volatility scaling** — multiply by `1 / vol`, where
   `vol` is the annualised rolling volatility over `vol_lookback`.
3. **Normalise to target gross** — scale so `Σ|w| = gross_leverage`.
4. **Optional volatility targeting** — scale the book so the portfolio's
   estimated annual volatility equals `vol_target`.
5. **Optional `net_target` shift** — move the book's net exposure to the
   requested value.
6. **Apply `max_position` as a HARD cap** — clip each weight to
   `±max_position`.

- `gross_leverage`: target sum of \|weights\|. `1.0` = fully invested.
- `max_position`: per-name cap, applied **last**, after every other sizing step.
- `vol_target` + `vol_lookback`: target portfolio volatility and the lookback
  used to estimate each asset's volatility.
- `rebalance`: `daily` / `weekly` / `monthly` / `quarterly` / `yearly` — between
  rebalances the previous target is held.
- `net_target`: force a specific net exposure (e.g. `0` for market neutral).
- `long_only`: suppress short legs.

**Why the cap must come last.** A cap applied *before* the gross
normalisation is silently undone by it: the rescale multiplies every weight
back up by the same factor, and the portfolio ends up exactly as
concentrated as it always was. The same is true of the net-target shift and
the volatility target, both of which rescale or shift the book. Clipping last
is the only ordering in which the cap actually binds.

**The honest consequence.** When the cap binds, realised gross is
necessarily **below** `gross_leverage`. The two constraints are only
simultaneously satisfiable when `N × max_position >= gross_leverage`. For
example, 4 instruments with a top/bottom-quartile split (1 long + 1 short)
and `max_position: 0.10` can reach a maximum gross of `2 × 0.10 = 0.20`,
not the 1.0 the spec asks for. That is not an engine defect — it is the
arithmetic of the user's stated constraints, and it belongs in the report.
See `references/verification-invariants.md` §7 and `scripts/selftest.py`
TEST 8.

**`net_target` is a SHIFT, not a scale.** The book is moved to the requested
net exposure by adding the same constant to every weight:

```
offset = (net_target - Σw) / N
w_s   += offset
```

A uniform *scale* — the obvious implementation, and the one this engine used
to use — cannot reach the target in three common cases: it divides by zero
when the book is already fully invested (`net == 1.0`), it **inverts** the
book when the target has the opposite sign (`net == 2.0` toward a target of
`-0.5` flips a long book short), and it is a no-op when the book is already
net-flat but the target is not zero. The shift handles all of them, preserves
the book's relative structure, and leaves gross unchanged for a balanced
book.

**`vol_target` sets the level, and the level is honoured.** The weights are
scaled so the portfolio's estimated annual volatility equals the target:
`vol_target: 0.10` and `vol_target: 0.40` now produce genuinely different
books. Portfolio volatility is estimated with a **diagonal covariance**
(correlations assumed zero), which *overstates* the volatility of a
genuinely diversified multi-asset book — so realised volatility lands
**below** the target. Treat `vol_target` as an upper bound on realised vol,
not an exact setting.

**`max_names` is decided per bar.** Universe membership is re-evaluated at
each rebalance using only information available at that bar, and the
surviving names are the *strongest*, not the alphabetically first. Ranking
uses the cross-sectional percentile rank of the traded feature (how extreme
the name's rank is), with the signal magnitude as a fallback and the symbol
name as a deterministic final tie-break. Membership must not be a
whole-sample decision: a name that first produces a signal on the final bar
of the sample is not in the book before that bar, and a name that stops
signalling leaves the book on the next rebalance.

## Stage 6 — Execution simulation
**Code:** `simulate`

The causality-critical section. At bar `i`:

1. Mark positions at `open[i]`.
2. Execute orders **decided at bar `i-1`**, filled at `open[i]` (or `close[i]`
   if `fill_at: "close"` — still causal, since the decision used bar `i-1`).
3. Charge costs: commission + half-spread + slippage on traded notional, plus
   annualised borrow on any short notional held that bar.
4. Mark at `close[i]` → daily equity and return.

### The rebalance mask gates ORDERS, not fills

The rebalance mask marks the bars on which an **order may be generated**.
An order generated at bar `i` fills at bar `i+1` — and bar `i+1` is *not*
itself a rebalance bar.

This distinction is load-bearing. If the mask gates the *fill* instead, the
order decided at bar `i` finds `can_trade == False` on bar `i+1` (that bar is
not marked), so it sits pending until the **next** marked bar — which, for a
monthly strategy, is a full month later. Every rebalance then executes one
period late, and the final rebalance of the sample never executes at all.

The correct sequence is: mark bar `i` → generate an order → fill it on bar
`i+1` regardless of whether `i+1` is marked → between marked bars, no order
exists and the previous target is simply held.

An unknown rebalance string **raises**. It used to fall through to a
one-bar mask, which produced a completely flat backtest and a verdict that
read as "no edge" when the real cause was a typo.

Also modelled:

- **`max_participation`** (0.10 default) — an order may not exceed 10% of the
  **trailing** dollar volume in one bar. The window is strictly *before* the
  fill bar: an order filled at bar `i`'s open cannot know bar `i`'s volume,
  only what came before. Using the same bar's volume was intra-bar
  look-ahead, and when that volume was zero the cap silently disappeared,
  letting an order of any size through. If no trailing volume is available
  the order is **not filled** and the skip is counted in
  `sim["liquidity_skips"]`.
- **`min_trade_frac`** (0.0005) — dust orders are skipped so the trade count
  and expectancy stats stay meaningful.

Bankruptcy: if marked equity ≤ 0 the simulation halts and zeroes the curve.

### `flat_at_close` (session mode)

`execution.flat_at_close: true` liquidates every open position at the
**close of the same bar it was opened in**, charging exit costs on the way
out. The position is therefore opened and closed within one bar, and the
strategy never carries overnight exposure.

The trade-ledger consequences follow directly from that:

- **One trade = one position lifecycle.** A session-mode trade is opened
  and closed inside the same bar, so it is a single ledger record with
  `bars_held == 0` and `entry_bar == bar`. It is *not* a partial rebalance
  adjustment, and it must not be counted twice.
- The recorded P&L is the **gross** open-to-close price move. Entry and exit
  costs are charged to cash and reported separately in `costs_paid`, so the
  accounting identity (`final_equity == capital + Σ trade P&L − costs`)
  still holds.
- Positions still open at the end of the sample are force-closed into the
  ledger, so the accounting identity still holds.
- Because every position round-trips within one bar, the trade count is
  roughly the number of active (symbol, bar) pairs. A session run
  therefore reports a *high* trade count over a *short* sample — which is
  the opposite of the usual "too few trades" flag, and the reason the
  ~2-year provider cap is the real constraint.

In the normal case the `flat_at_close` records are the only trades in the
ledger — there are no multi-bar positions to confuse the round-trip
invariant. (The one exception is a bar with no usable close price, where
the liquidation is skipped and the position carries to the next bar.)

**Cannot remove:** partial fills, queue priority, latency, exchange halts,
true market impact. Mitigate by running `cost_multiplier` at 2–3×.

## Stage 7 — Accounting
**Code:** `simulate` return value

Produces the equity curve, per-bar returns, a trade ledger (closed portions with
realised P&L), turnover, total costs paid, and the gross-exposure time series.

The ledger emits a record when a position returns to **flat**, not on each
partial rebalance adjustment — one trade per position lifecycle. In session
mode with `flat_at_close: true` each lifecycle is one bar long. See
`references/verification-invariants.md` §9.

## Stage 8 — Validation gauntlet
**Code:** `full_gauntlet` in `btvalidate.py`

| Stage | Function | Refutes the hypothesis when… |
|-------|----------|-------------------------------|
| Base metrics | `compute_metrics` | Sharpe/CAGR/drawdown/trade count are implausible or absent |
| Walk-forward | `walk_forward` | Pooled OOS Sharpe < 0.5, or OOS ≪ IS |
| Gross vs net | `gross_vs_net` | (informational) re-runs the strategy with `cost_multiplier: 0` so the report can separate *no edge at all* from *a real edge too small to trade* |
| Cost stress | `cost_stress` | Sharpe goes ≤0 at 2× costs |
| Parameter sweep | `parameter_sensitivity` | <60% of ±25% perturbations keep the edge (spike, not plateau) |
| Monte Carlo | `monte_carlo` | P(profitable) < 85% over 3,000 reshuffles |
| Subperiods | `subperiod_and_regime` | <50% of calendar years positive |
| Capacity | `capacity_estimate` | (informational) binding symbol's ADV caps size |
| Replication | `cross_market_replication` | Edge fails in <50% of alternative universes |
| Causality | `causality_audit` | Any feature's value changes when future data is removed → **hard REFUTED** |
| Snooping | `deflated_sharpe` | Adjusted Sharpe < 0 given `--n-variants N` |

Thresholds live in `THRESHOLDS` (btvalidate.py) — edit them there if the user
has a different risk appetite.

### The Due-Diligence Disclosure sheet

`render_report` appends a **Due-Diligence Disclosure** section to every
report, built by `scripts/disclosure.py`. It is not a separate artefact —
it is part of the evidence file, and it is the record of *what was actually
established* about the run.

The sheet asks **109 questions** across nine sections (A strategy scope,
B data integrity, C per-asset-class modelling, D look-ahead, E costs and
capacity, F portfolio construction, G robustness, H metrics, I forward
testing). Each question is pre-classified by who can answer it:

| who | meaning |
|-----|---------|
| `engine` | the engine answers it with a verified fact about its own behaviour |
| `user` | only the user can answer it (rationale, base currency, hedging policy, discretionary overrides, forward-test criteria) |
| `both` | the user sets the parameter; the engine reports the value actually used |

**Do not hardcode the split.** The engine-answerable set grows every time a
capability mapping is added, so any count written in prose goes stale — and two
documents quoting different counts is worse than no count at all. Read the live
numbers from `--disclose`, which prints the coverage line directly.

Most of the sheet carries a static verified engine answer, and two more
(`B.observations`, `E.adv_participation`) are answered from the run's own
metrics and capacity estimate. The user is never asked something the engine
already knows; what remains are the questions that genuinely need a user
answer.

An engine-classified question can still render `[UNKNOWN]` if its capability
mapping points at an entry that does not exist — a missing mapping is an engine
defect to fix, not a question for the user. Every `who: "engine"` key in
`CAP_BY_QUESTION` must resolve to a real `CAPABILITIES` entry; assert it with
`disclosure.unmapped_engine_questions() == []`.

A **coverage line** sits above the sheet:

```
- Questions answered: **N/109** (P%)
- Answered by the engine (verified): N
- Answered by the user: N
- Still UNKNOWN: **N**
```

Each answer carries a provenance marker — `[user]`, `[engine]` or
`[UNKNOWN]`. `UNKNOWN` means neither the user nor the engine answered it.
It is printed rather than omitted, because **an unknown is a finding**:
hiding it would be the exact failure the sheet exists to prevent. How to
read the sheet, and what to do about an `UNKNOWN` in a load-bearing
question, is in `references/interpreting.md`.

You can print the sheet without running a backtest:

```bash
python scripts/run_backtest.py --disclose              # full sheet + what is still needed
python scripts/run_backtest.py --disclose-section D   # one section
```

`--disclose` ends with a "STILL NEEDED FROM THE USER" list — ask only those.

### The causality audit

The one check the engine runs on itself, independent of the hypothesis.
It computes features on the full panel, then recomputes them on a panel
truncated to the first half of dates. If any first-half value differs, that
feature used future data. A centred rolling window (e.g. pandas
`rolling(center=True)`) fails this immediately. `selftest.py` runs it across
every built-in feature.

### Walk-forward design

Expanding-window with **non-overlapping** test blocks:

```
[initial train][test0][test1][test2][test3][test4]
```

Each fold re-runs the whole strategy on its test slice with fresh capital, so
no position or equity carries across a boundary. Long-lookback features lose
their warm-up at each fold start — this depresses OOS and is intentional.

Parameters are **not** re-optimised per fold. This tests whether the
*hypothesis* generalises, not whether an optimiser can re-fit it.