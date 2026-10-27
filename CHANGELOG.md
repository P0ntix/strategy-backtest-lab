# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Purged and embargoed walk-forward** with a **disjointness assertion** that
  verifies and reports: no date appears in two OOS blocks, no OOS bar was in
  its own fold's training set, no training bar falls inside any test block
  under purge, and no training bar falls inside a preceding fold's embargo
  band. Purge and embargo sizes default to the maximum feature lookback in
  the spec and are overridable.
- **`portfolio.vol_target` is now a real volatility target.** The book is
  scaled so the portfolio's estimated annual volatility equals the target.
  Previously the level was discarded and `0.10` and `0.40` produced identical
  books. Portfolio vol uses a diagonal covariance (correlations assumed
  zero), so realised vol lands *below* the target on a diversified book —
  the target is an upper bound, not an exact setting.
- **`max_names` now selects the strongest names, per bar.** Membership is
  decided at each rebalance using only information available at that bar,
  ranked by the cross-sectional percentile rank of the traded feature.
- **The intake gate now enforces PROVENANCE, not just confirmation.** A
  condition whose fields all still hold the engine's default value is
  reported as unanswered, even when the user ticked "confirmed" — because
  confirmation proves the question was asked, not that the user answered it.
  A user who genuinely chose the engine default records that in
  `intake.accepted_defaults`.
- **A partial run now says so.** `--quick` names the stages it skipped in
  the verdict, in the report (`PARTIAL VALIDATION`) and in the scorecard
  (`skipped_stages`), and the verdict no longer claims to have survived
  "every robustness check" when it did not run them.
- Regression tests 17–24 in `scripts/selftest.py` cover rebalance timing,
  the net target, the position cap, configuration errors, CLI/library parity,
  `max_names` look-ahead and the `vol_target` level.

### Changed
- **`net_target` is applied as a shift, not a scale.** The same constant is
  added to every weight to reach the requested net exposure. A uniform scale
  could not reach a target of the opposite sign without inverting the
  strategy, could not neutralise a one-sided book, and was a no-op on an
  already-flat book.
- **`max_position` is applied last**, after the gross normalisation, the
  volatility target and the net-target shift — all of which rescale or move
  the book and would otherwise undo the cap.
- **The trade ledger is net of costs.** Every cost dollar is attributed to the
  position that caused it and netted out of that trade's P&L when the position
  closes. Profit factor, win rate and expectancy are now on the same basis as
  Sharpe; `sim["trade_pnl_basis"]` states the basis and the report labels its
  rows from it.
- **The library API and the CLI evaluate identical specifications.**
  `run_spec()` now applies `DEFAULT_SPEC` the same way `load_spec()` does, so
  the same strategy can no longer get two different verdicts depending on how
  it was invoked.
- **Walk-forward folds now reuse the base run's features instead of
  recomputing them on a cold slice.** A feature at bar `i` legitimately uses
  data from before bar `i` — that is history, not leakage — so a fold that
  recomputed features on the sliced panel was measuring a *different*
  strategy from the headline number, with its first `horizon` bars flat.
- The report labels the profit-factor, win-rate and expectancy rows with
  their basis, and the Monte Carlo is described as a bootstrap of the
  realised trade sequence rather than a "trade reshuffle". The walk-forward
  section now states the purge and embargo sizes, the disjointness result,
  the OOS retention ratio, and any note the planner had to raise.

### Fixed
- **The rebalance mask gates ORDER GENERATION, not order filling.** An order
  decided on a rebalance bar now fills on the very next bar. Previously the
  mask gated the fill, so the order sat pending until the *next* rebalance
  bar: every rebalance executed one full period late, and the final
  rebalance of the sample never executed. Since the default rebalance is
  `monthly`, this affected every default run.
- **`net_target` no longer divides by zero** when the book is already fully
  invested, and no longer **inverts** the book's sign when the target has
  the opposite sign.
- **The position cap is no longer blown through** by the net-target rescale.
- **The accounting identity is now exact**: `capital + Σ trade P&L` equals
  `final_equity` to the cent. The gap used to be the cost bill, which meant
  the trade statistics were gross while the equity curve was net.
- **An unknown `rebalance` string now raises** instead of silently producing
  a one-bar mask, a flat backtest and a verdict that read as "no edge".
- **A single-instrument universe with a cross-sectional signal now raises**
  with an actionable message, instead of producing a perfectly flat backtest
  with no explanation.
- **The negative-control guard is no longer vacuous.** It filtered on the
  uppercase string `NEGATIVE` while the test labels say "negative control",
  so the list was always empty and `all([])` was silently `True` — the guard
  could never fail.
- **`max_names` no longer looks ahead.** Universe membership was decided once
  for the whole sample, so a name that first signalled on the final bar was
  in the book from bar 0, and the survivors were the alphabetically first N
  rather than the strongest.
- **The liquidity cap now uses trailing volume.** It was computed from the
  *same* bar's volume to size an order that fills at that bar's open — the
  bar's volume is not knowable at its open. A thin bar tightened the cap, a
  fat bar loosened it, and a zero-volume bar made the cap disappear so an
  order of any size passed. The cap is now applied against the trailing
  dollar volume strictly before the fill bar, and an order with no trailing
  volume is not filled (counted in `sim["liquidity_skips"]`).
  An instrument with **no volume series at all** (FX pairs and most OTC
  instruments have no consolidated screen volume) is the separate case: the
  participation cap does not apply to it, so it still trades, and it is named
  in `sim["symbols_without_volume"]`.
- **Walk-forward folds are no longer cold-started.** They recomputed features
  on the sliced panel, so the first `horizon` bars of every fold carried no
  signal: a 252-bar lookback on a short fold produced an all-`None` signal,
  an empty fold and an artificial refutation. Folds now reuse the base run's
  full-panel features, so the fold measures the same strategy as the headline.
- **No order is filled on a stale bar.** The panel records which bars were
  forward-filled because the symbol printed no observation. Those prices are
  now used to *mark* positions but never to *fill* orders, so a halted or
  delisted name can no longer trade indefinitely at a phantom price.
  Dropped orders are counted in `sim["stale_skips"]`.
- **An unknown rebalance string now raises** instead of silently producing
  a one-bar mask, a flat backtest and a verdict that read as "no edge".
- **A single-instrument universe with a cross-sectional signal now raises**
  with an actionable message, instead of producing a perfectly flat backtest
  with no explanation.
- **The negative-control guard is no longer vacuous.** It filtered on the
  uppercase string `NEGATIVE` while the test labels say "negative control",
  so the list was always empty and `all([])` was silently `True` — the guard
  could never fail.
- **`max_names` no longer looks ahead.** Universe membership was decided once
  for the whole sample, so a name that first signalled on the final bar was
  in the book from bar 0, and the survivors were the alphabetically first N
  rather than the strongest.
- **The walk-forward partition, the PBO label, the Monte Carlo description and
  the `--quick` mode's robustness claim now match what the engine actually
  does.** The PBO statistic is now a genuine IS-vs-OOS rank-inversion rate
  (it receives the per-fold in-sample Sharpe, not just the OOS list).

## [1.0.0]

First public release. Pure Python standard library, **zero runtime
dependencies**, Python 3.8+.

### What this tool does

A dependency-free backtesting engine that takes a trading hypothesis, encodes
it as machine-readable rules, runs it through an eight-stage pipeline against
real historical market data, and returns a blunt verdict: **SUPPORTED**,
**FRAGILE**, **REFUTED** or **INCONCLUSIVE**.

- **Data** — fetches OHLCV from the free Yahoo Finance chart API (no API key,
  no account), caches it locally, and runs an integrity audit on every panel.
  Daily bars are **split- and dividend-adjusted**: the engine back-adjusts
  OHLC by the per-bar `adjclose / close` factor, which reproduces Yahoo's
  adjusted series exactly, so an ex-dividend date does not read as a real
  loss. The cache carries a schema marker, so daily cache files written
  before the adjustment existed are refetched rather than silently reused.
- **Universe** — aligns symbols on a shared date axis with forward-fill and a
  median-dollar-volume liquidity screen. Ships 14 built-in presets spanning
  US equity broad/sector/style, futures, FX (major and emerging), commodities,
  crypto, rates and credit, international equity, real assets and multi-asset
  combinations.
- **Features** — 14 causal transforms (momentum, z-score, RSI, realised
  volatility and ratio, Bollinger percent, ATR percent, MA crossover, skew,
  drawdown, volume ratio, distance to high, …), plus 3 session-window
  features for the intraday path.
- **Signal** — 5 rule types (cross-sectional rank, time-series threshold,
  time-series z-score mean reversion, crossover, constant) producing a target
  weight per symbol per bar.
- **Portfolio** — gross-leverage normalisation, inverse-volatility sizing, a
  hard per-name concentration cap, optional net-exposure target, long-only
  mode, and a rebalance calendar (daily / weekly / monthly / quarterly /
  yearly).
- **Execution** — fills at **t+1** (never t), with per-asset-class commission,
  half-spread and slippage, annualised borrow on short notional, a bar-volume
  participation cap, and dust-order suppression.
- **Accounting** — mark-to-market equity curve, per-bar returns, and a
  round-trip trade ledger where one trade is one complete position lifecycle.
- **Validation** — a gauntlet of walk-forward out-of-sample folds, gross-vs-net
  cost decomposition, cost stress (1.5x / 2x / 3x), parameter-sensitivity
  sweep, Monte Carlo trade reshuffling, subperiod and volatility-regime
  decomposition, capacity estimation, cross-market replication, a
  data-snooping (deflated Sharpe) penalty and a causality audit.

Additional capabilities:

- **An intake gate.** Every condition of a strategy must be imputed by the
  user. The engine refuses to run an unconfirmed spec and exits with code 2.
  `--assume-defaults` exists only for a user who has explicitly declined to
  answer, and using it is itself a finding to report.
- **An A–I due-diligence disclosure sheet.** A large multi-asset review is
  broken into sections and each question is pre-classified as answerable by
  the engine (with a verified fact), by the user, or by both. Answers carry
  `[user]` / `[engine]` / `[UNKNOWN]` provenance markers. **UNKNOWN is a
  finding, not an omission** — it is printed rather than hidden.
- **An intraday session mode.** With `execution.mode: "session"` the data
  layer switches to 60-minute bars collapsed to one 09:30–10:00 ET opening
  window bar per session. The panel stays daily-shaped, so every metric, the
  walk-forward folds and the 252 annualisation are unchanged. Session
  boundaries are detected per trading day, so the extraction is DST-safe and
  needs no timezone database.
- **Four self-test suites.** `scripts/selftest.py` (behavioural controls, a
  causality-by-truncation audit over every built-in feature, and seven
  invariants), `scripts/selftest_intraday.py` (offline checks of the session
  path), `scripts/selftest_corporate_actions.py` (offline checks of the
  dividend adjustment and cache-schema invalidation), and
  `scripts/selftest_packaging.py` (offline checks that every module imports
  both as a plain script and from the installed `btlab` package layout).

### What this tool explicitly does NOT support

Stated up front, because a backtester that overstates its coverage is worse
than no backtester at all. In particular it does **not** model:

- **FX conversion or hedging.** Returns stay in each instrument's quote
  currency; a multi-currency book is not converted to a base currency.
- **Point-in-time fundamentals, index membership, credit ratings or sector
  classifications.** Universe membership is static per spec.
- **A covariance matrix**, so cross-asset correlation is never audited.
- **Options.** No pricing, Greeks, implied volatility surface, expiry,
  assignment or margin.
- **Bond analytics.** No clean/dirty price, accrued interest, coupon cash
  flows, duration, convexity, default or recovery.
- **Alternatives and real-asset valuation.** No appraisal smoothing,
  valuation lag, liquidity gates or redemption constraints.
- **Margin, financing or collateral-haircut costs.** Gross exposure carries no
  cost of capital.
- **Futures roll cost.** Yahoo's continuous series is used as published; its
  roll methodology is undocumented and not point-in-time.
- **FX swap / rollover.** Spot only; carry is not modelled.
- **Survivorship-free individual equities.** The provider serves current
  listings only. Prefer ETFs, and say so whenever a stock list is used.
- **Leave-one-asset-out, randomised asset inclusion, randomised rebalancing
  dates, top-1-5 trade removal**, or **confidence intervals** for expectancy,
  profit factor, Sharpe or drawdown. These validations are not implemented and
  are not implied anywhere.

Additional honest limits: intraday bars are **not** dividend-adjusted (the
provider publishes no adjusted close at that interval) and splits inside a
60-minute session are not modelled; the walk-forward folds are deliberately
conservative because features are recomputed on a sliced panel, so long
lookbacks lose warm-up at each fold start; and market impact, order-book
microstructure and limit-order fill mechanics are not modelled.

### Notes

- The verdict vocabulary is deliberately blunt. **SUPPORTED** means "warrants
  paper trading" — never "trade it".
- Cost defaults are conservative per-asset-class figures, not any particular
  broker's actual fees.
- The data is fetched from Yahoo Finance and is subject to that provider's
  terms, availability and corrections.

[Unreleased]: https://github.com/OWNER/strategy-backtest-lab/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/OWNER/strategy-backtest-lab/releases/tag/v1.0.0
