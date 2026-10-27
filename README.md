# Strategy Backtest Lab

**A dependency-free backtesting engine that tells you whether your trading hypothesis is real — and if it isn't, tells you exactly why.**

You state a trading idea. The lab encodes it as machine-readable rules, runs it through an eight-stage pipeline against real historical market data, and returns a blunt verdict: **SUPPORTED**, **FRAGILE**, **REFUTED**, or **INCONCLUSIVE**. It supports equities, ETFs, FX, futures, crypto, rates and credit, and mixed multi-asset universes, plus an intraday "session" mode that trades the 09:30–10:00 ET opening window.

**Zero dependencies. Pure Python standard library. Python 3.8+.** There is nothing to `pip install` — not numpy, not pandas, not yfinance. Data comes from the free Yahoo Finance chart API (no API key, no account), and the whole market-data cache is about 30 MB even after covering dozens of symbols across every asset class.

The engine is deliberately blunt about its own limits. It ships an A–I due-diligence disclosure sheet where every question is answered by the engine with a verified fact, by you, or marked **UNKNOWN** — and an UNKNOWN is printed, not hidden. It also tells you, in plain English, the thing most backtesters blur: whether a failing strategy had *no edge at all*, or *a real edge too small to survive costs*. Those are different findings and they call for opposite responses.

---

## Contents

- [Quickstart](#quickstart)
- [Worked example](#worked-example-end-to-end)
- [The eight-stage pipeline](#the-eight-stage-pipeline)
- [The intake gate](#the-intake-gate)
- [The A–I due-diligence sheet](#the-a%E2%80%93i-due-diligence-sheet)
- [Capabilities matrix](#capabilities-matrix)
- [Session / intraday mode](#session--intraday-mode)
- [Verification](#verification)
- [Repository layout](#repository-layout)
- [Contributing, license, attribution](#contributing-license-and-attribution)

---

## Quickstart

Confirm your interpreter — nothing else is needed:

```bash
python -c "import sys; print(sys.version)"
```

Print the universes, features and signals the engine knows about:

```bash
python scripts/run_backtest.py --list-presets
```

Print the intake questionnaire (the questions you must answer before a
backtest is allowed to run):

```bash
python scripts/run_backtest.py --intake
```

Write a template spec, then check what is still missing from it:

```bash
python scripts/run_backtest.py --example > my_strategy.json
python scripts/run_backtest.py --config my_strategy.json --intake-check
```

Run it — fast first, then the full gauntlet:

```bash
python scripts/run_backtest.py --config my_strategy.json --quick
python scripts/run_backtest.py --config my_strategy.json
```

Read the due-diligence sheet and its live coverage numbers:

```bash
python scripts/run_backtest.py --disclose
python scripts/run_backtest.py --disclose-section D
```

### CLI reference

| Flag | Effect |
|------|--------|
| `--config PATH` | Run the strategy described by the JSON spec at `PATH`. |
| `--quick` | Skip the expensive stages (Monte Carlo, parameter sweep, replication) for a fast read. |
| `--n-variants N` | How many parameter variants **you** tried while developing this hypothesis. Feeds the deflated-Sharpe penalty. Defaults to 1. |
| `--assume-defaults` | Bypass the intake gate. Only for a user who has explicitly declined to answer — and using it is itself a finding you must report. |
| `--intake` | Print the intake questionnaire and exit. |
| `--intake-check` | Report which user-supplied conditions are still missing (requires `--config`). |
| `--disclose` | Print the full A–I due-diligence sheet, its coverage line, and the questions still needing you. |
| `--disclose-section X` | Print one section (`A`–`I`) of the sheet. |
| `--example` | Print a complete example spec as JSON and exit. |
| `--list-presets` | Print the universe presets, features and signals, and exit. |
| `--refresh` | Bypass the local data cache and re-fetch. |
| `--out PATH` | Write the markdown report to `PATH` (default: `<name>_report.md`). |
| `--no-report` | Print to stdout only; do not write a report file. |

### Exit codes

| Code | Meaning |
|------|---------|
| `0` | The backtest ran and a verdict was produced. |
| `2` | The intake was incomplete and the engine **refused to run**. Nothing was backtested. |

---

## Worked example (end to end)

A spec is a JSON file. Here is a complete, runnable one — cross-sectional
momentum over US sector ETFs, in which each condition was supplied and
confirmed by the user rather than invented by the engine:

```json
{
  "name": "12-1 Month Momentum (Cross-Sectional, Equity Sectors)",
  "hypothesis": "Sectors that outperformed over the past 12 months (skipping the last month) continue to outperform over the next month, due to slow-moving institutional flows and underreaction to persistent fundamental trends.",
  "universe": { "preset": "us_equity_sector", "symbols": [] },
  "period": { "start": "2010-01-01", "end": null, "interval": "1d" },
  "screen": { "min_adv_usd": 0 },
  "features": [
    { "name": "momentum", "params": { "lookback": 252, "skip": 21 } }
  ],
  "signal": {
    "type": "cross_sectional",
    "params": { "long_rank": 0.75, "short_rank": 0.25 }
  },
  "portfolio": {
    "gross_leverage": 1.0,
    "rebalance": "monthly",
    "long_only": true,
    "max_position": 0.2
  },
  "execution": {
    "fill_at": "open",
    "cost_multiplier": 1.0,
    "max_participation": 0.10
  },
  "capital": 100000.0,
  "intake": {
    "confirmed": [
      "hypothesis", "universe", "direction", "entry", "exit",
      "timeframe", "rebalance", "sizing", "leverage", "costs",
      "period", "variants_tried"
    ],
    "variants_tried": 1,
    "notes": "Illustrative example. A real spec must record the user's own answers."
  }
}
```

Save it as `my_strategy.json` and run:

```bash
python scripts/run_backtest.py --config my_strategy.json --n-variants 12
```

You get a verdict line and a markdown report in the current working directory,
named after the spec's `name` field (so this one is
`12-1_Month_Momentum_(Cross-Sectional,_Equity_Sectors)_report.md`; use
`--out PATH` to choose a different name). The report contains the in-sample
metrics, the walk-forward fold table, the by-year breakdown, the parameter
sweep, the gross-vs-net decomposition, the cost stress table, the Monte Carlo
distribution, the causality audit, and the full due-diligence sheet.

The `intake.confirmed` list is what makes the run legal. Drop one key and
the engine refuses:

```
$ python scripts/run_backtest.py --config my_strategy.json
====================================================================
INTAKE INCOMPLETE — refusing to backtest.
====================================================================
...
```

More worked examples — risk parity across asset classes, vol-regime
breakout, and an intraday session-drift strategy — are in
[`templates/`](templates/). Every spec field is documented in
[`references/spec-schema.md`](references/spec-schema.md).

---

## The eight-stage pipeline

| # | Stage | Code | What it does |
|---|-------|------|--------------|
| 1 | **Data** | `fetch_batch`, `load_or_fetch`, `audit_panel` | Fetch OHLCV from the Yahoo chart API, cache it locally, and audit the result for short series, calendar gaps, non-positive prices, excessive zero-volume bars, stale flat closes and extreme single-bar moves. |
| 2 | **Universe** | `UNIVERSE_PRESETS`, `build_panel` | Align symbols on a shared date axis, forward-fill, apply the median-dollar-volume liquidity screen. |
| 3 | **Features** | `FEATURES` registry | Compute causal transforms (momentum, z-score, RSI, realised volatility, Bollinger percent, ATR percent, MA crossover, skew, drawdown, …). |
| 4 | **Signal** | `SIGNALS` registry | Turn rules into a target weight per symbol per bar (cross-sectional rank, time-series threshold, z-score mean reversion, crossover, constant). |
| 5 | **Portfolio** | `construct_target_weights` | Normalise to target gross, apply optional inverse-vol sizing, apply the hard per-name cap, apply the optional net target, and mark the bars on which orders may be sent. |
| 6 | **Execution** | `simulate` | Fill at **t+1** (never t) with commission, half-spread, slippage, borrow on shorts, and a bar-volume participation cap. |
| 7 | **Accounting** | `simulate` | Mark-to-market equity curve, per-bar returns, and a round-trip trade ledger where one trade is one complete position lifecycle. |
| 8 | **Validation** | `full_gauntlet` | Walk-forward out-of-sample folds, gross-vs-net decomposition, cost stress, parameter sweep, Monte Carlo, subperiod/regime decomposition, capacity, replication, causality audit, deflated Sharpe → the verdict. |

Stage 8 is the point of the whole thing. A backtest that reports only
in-sample metrics has told you nothing, because the in-sample number is
exactly what a curve fit looks like.

### What the gauntlet actually runs

| Stage | Function | Refutes the hypothesis when… |
|-------|----------|-------------------------------|
| Base metrics | `compute_metrics` | Sharpe / CAGR / drawdown / trade count are implausible or absent |
| Walk-forward OOS | `walk_forward` | Pooled out-of-sample Sharpe < 0.5, or OOS is far below in-sample |
| Gross vs net | `gross_vs_net` | (informational, but it changes the verdict wording) Separates *no edge at all* from *a real edge too small to trade* |
| Cost stress | `cost_stress` | Sharpe goes ≤ 0 at 2× costs |
| Parameter sweep | `parameter_sensitivity` | Fewer than 60% of ±25% perturbations keep the edge — a spike, not a plateau |
| Monte Carlo | `monte_carlo` | P(profitable) < 85% over 3,000 trade reshuffles |
| Subperiods | `subperiod_and_regime` | Fewer than 50% of calendar years are positive |
| Capacity | `capacity_estimate` | (informational) the binding instrument's ADV caps the account size |
| Replication | `cross_market_replication` | The edge fails in more than half of the alternative universes |
| Causality | `causality_audit` | Any feature's value changes when future data is removed → **hard REFUTED** |
| Data snooping | `deflated_sharpe` | The adjusted Sharpe goes negative given `--n-variants N` |

### Gross vs net: the distinction most backtesters blur

A strategy can fail for two opposite reasons. The gauntlet re-runs the whole
strategy with `cost_multiplier: 0.0` and reports both sides:

| Gross-of-costs | Net-of-costs | Reading |
|----------------|---------------|---------|
| positive | negative | A real edge, too small to trade. The fix is trading less often or at a different size — **not** abandoning the hypothesis. |
| negative | negative | No expectancy in the sample. Costs are *not* the primary problem, and "the edge disappeared under costs" would be a false explanation. |
| positive | positive | The edge survives; judge it on the net number. |

The verdict reasoning uses this decomposition, so a REFUTED verdict tells you
*which* failure you have. Full detail in
[`references/verification-invariants.md`](references/verification-invariants.md).

---

## The intake gate

**Every condition of the strategy must come from you. The engine refuses to
run a spec you have not confirmed.**

This is enforced, not aspirational. `run_backtest.py` exits with code **2**
on any spec whose twelve core conditions are not confirmed, and prints the
outstanding questions. `--assume-defaults` exists only for someone who has
explicitly declined to answer — and using it is itself a finding you must
report.

A spec passes only when **both** hold: all twelve keys appear in
`intake.confirmed`, **and** the corresponding field carries a real value.
Four fields additionally may not be left at an engine default:
`period.start`, `portfolio.rebalance`, `portfolio.gross_leverage`,
`execution.cost_multiplier`.

| # | Condition | The question |
|---|-----------|--------------|
| 1 | `hypothesis` | The claim in your own words, and what **drives** it — behaviour, market structure, information, or a risk premium? |
| 2 | `universe` | Which instruments exactly? Which asset class? |
| 3 | `direction` | Long only, long/short, or market-neutral? |
| 4 | `entry` | The exact trigger — a rule, not an indicator name. |
| 5 | `exit` | What exactly closes the position? |
| 6 | `timeframe` | What lookback / holding period, measured over how many bars? |
| 7 | `rebalance` | How often may it trade? |
| 8 | `sizing` | Equal weight, inverse-volatility, fixed-fractional, or a maximum per name? |
| 9 | `leverage` | Maximum gross exposure, maximum weight per instrument? |
| 10 | `costs` | Actual commission, spread and slippage — or the conservative per-asset-class defaults? |
| 11 | `period` | Which date range should be tested? |
| 12 | `variants_tried` | How many variations did you try before settling on this one? |

**"I don't know" is a valid answer — record it as the answer.** A user who
cannot state an exit rule or a mechanism has told you something important;
report it rather than filling the gap.

### `--n-variants` and the deflated Sharpe

Reporting the best of N trials is the single most common way a backtest
lies, and no engine can detect it — only you know how many you tried. Pass
the honest number:

```bash
python scripts/run_backtest.py --config my_strategy.json --n-variants 37
```

The deflated Sharpe penalises the observed Sharpe for the number of variants
tried, on top of the non-normality correction. If the adjusted Sharpe goes
negative, the engine raises a flag: the edge is probably a data-snooping
artifact. **`--n-variants` defaults to 1, which is optimistic** — always ask
the user.

---

## The A–I due-diligence sheet

A full multi-asset review asks a large number of questions across nine
sections: **A** strategy scope, **B** data integrity, **C** per-asset-class
modelling, **D** look-ahead and leakage, **E** costs, execution, financing and
capacity, **F** portfolio construction and cross-asset risk, **G** robustness
and statistical validity, **H** metrics, **I** forward testing.

Each question is pre-classified by who can answer it:

| Marker | Meaning |
|--------|---------|
| `[engine]` | The engine answers it with a **verified fact** about its own behaviour — the data source, the timestamp convention, the survivorship treatment, the execution model, what the gauntlet covers. |
| `[user]` | Only you can answer it — rationale, base currency, hedging policy, discretionary overrides, forward-test criteria. |
| `[UNKNOWN]` | Neither answered it. **An unknown is a finding.** It is printed rather than omitted, because hiding it would be the exact failure this sheet exists to prevent. |

Print the sheet and its live numbers:

```bash
python scripts/run_backtest.py --disclose
```

The output ends with a coverage line (answered / total, and the split between
engine-answered, user-answered and still unknown) followed by a
**"STILL NEEDED FROM THE USER"** list — ask only those. You are never asked
something the engine already knows. To read one section:

```bash
python scripts/run_backtest.py --disclose-section D
```

The sheet also lands inside every generated report, under
"Due-Diligence Disclosure", with its coverage line.

> **The counts are deliberately not written down here.** The engine-answerable
> set grows every time a capability mapping is added, so any number baked into
> prose goes stale — and two documents quoting different counts is worse than
> no count at all. Run `--disclose` and read the live numbers.

An engine-classified question that renders `[UNKNOWN]` is a **broken mapping,
not a question for you** — `disclosure.unmapped_engine_questions()` lists any
such defect.

---

## Capabilities matrix

### What the engine DOES support

| Area | Support |
|------|---------|
| Asset classes | US equities and sector/style ETFs, indices, futures, FX (major and emerging), crypto, rates and credit, commodities, international equity, real assets, and mixed multi-asset combinations. |
| Universe presets | 14 built-in presets covering broad/sector/style equities, `futures_broad`, `fx_major`, `fx_emerging`, `commodities`, `crypto`, `rates_credit`, `intl_equity`, `real_assets`, `multi_asset`, `risk_parity_ish`, `risk_on_risk_off`. Any preset can be copied into `universe.symbols` and edited. |
| Corporate actions | **Daily bars are split- AND dividend-adjusted.** The engine reads Yahoo's `adjclose` and back-adjusts OHLC by the per-bar `adjclose / close` factor, which reproduces Yahoo's adjusted series exactly — so an ex-dividend date does not read as a real loss, and returns include dividends. Volume is never scaled, and the adjusted OHLC stays internally consistent (`high ≥ low`, body within the bar). A malformed factor is rejected rather than applied. |
| Cache schema | Cached files carry a schema marker (`CACHE_SCHEMA`, currently 2). Daily cache files written before dividend adjustment existed are **refetched**, not silently reused — otherwise the backtest would keep the ex-dividend bias. |
| Features | 14 causal transforms: `return`, `momentum`, `sma_gap`, `volatility`, `vol_ratio`, `zscore`, `rsi`, `max_drawdown`, `skew`, `volume_ratio`, `bollinger_pct`, `atr_pct`, `ma_crossover`, `dist_to_high`. Plus `session_drift`, `session_gap`, `session_return` in session mode. Every feature accepts `direction: -1` to invert it. |
| Signals | `cross_sectional`, `time_series_threshold`, `time_series_zscore` (mean reversion with an explicit entry and exit), `crossover`, `constant`. Cross-sectional ranks are recomputed **per date**. |
| Portfolio construction | Target gross leverage, optional inverse-volatility sizing, a **hard** per-name `max_position` cap, optional net-exposure target, long-only mode, maximum concurrent names, and a rebalance calendar (daily / weekly / monthly / quarterly / yearly). Orders are only sent on the marked bars. |
| Execution | Fill at **t+1** at the open (or close), never at t. Costs: per-asset-class commission + half-spread + slippage on traded notional, plus annualised borrow on short notional. Bar-volume participation cap, dust-order suppression, optional flat-at-close (no overnight exposure). |
| Accounting | Mark-to-market equity curve, per-bar returns, turnover, total costs paid, gross-exposure history, and a round-trip trade ledger where **one trade is one complete position lifecycle** — not one per partial rebalance adjustment. |
| Validation | Walk-forward out-of-sample with non-overlapping test blocks, gross-vs-net cost decomposition, cost stress (1.5×/2×/3×), ±25% parameter sweep, Monte Carlo trade reshuffling, by-year and volatility-regime decomposition, capacity estimation, cross-market replication, causality-by-truncation audit, deflated Sharpe and a PBO proxy. |
| Reporting | A markdown report with the verdict, in-sample metrics, fold table, by-year table, parameter sweep, gross-vs-net decomposition, cost stress table, Monte Carlo distribution, causality result, every raised flag, and the full due-diligence sheet. |
| Intraday | A session mode that trades the 09:30–10:00 ET opening window, with DST-safe session extraction that needs no timezone database. |

### Two sizing rules worth knowing before you read a result

**The position cap is a HARD limit, applied last.** A cap applied before the
gross rescale, the net-target shift or the volatility target is silently
undone by them. The honest consequence: **if the cap binds, realised gross is
lower than `gross_leverage`.** Worked example — 4 instruments, a
top/bottom-quartile cross-sectional split (1 long + 1 short),
`max_position: 0.10`:

| step | long | short | gross |
|------|------|-------|-------|
| raw signal | +1.0 | −1.0 | 2.0 |
| normalised to `gross_leverage` 1.0 | +0.50 | −0.50 | 1.00 |
| `max_position` 0.10 applied | +0.10 | −0.10 | **0.20** |

The maximum achievable gross is `0.20`, not `1.0`. That is the arithmetic of
the stated constraints, not an engine defect — and it belongs in the report.

**`vol_target` sets the size of the book.** The weights are set proportional
to `1 / volatility` (risk-parity *shape*), and then the whole book is scaled
so the portfolio's estimated annual volatility equals the target (its
*level*). `vol_target: 0.10` and `vol_target: 0.40` now produce genuinely
different books.

Portfolio volatility is estimated with a **diagonal covariance** — pairwise
correlations are assumed to be zero — so for a genuinely diversified
multi-asset book the estimate is *too high* and the realised volatility lands
**below** the target. Read `vol_target` as an upper bound on realised vol,
not an exact setting. Realised volatility is reported separately in the
report so the gap is visible.

**`net_target` moves the book, it does not scale it.** The engine adds the
same constant to every weight to reach the requested net exposure. A uniform
scale (the intuitive implementation) cannot reach a target of the opposite
sign without inverting the strategy, cannot neutralise a one-sided book, and
is a no-op when the book is already net-flat. The shift handles all three.

### What the engine does NOT support

These are stated up front on purpose. A backtester that overstates its
coverage is worse than no backtester at all.

| Area | Not supported |
|------|---------------|
| FX | **No FX conversion and no hedging.** Every instrument is priced in its quote currency and returns are computed in that currency. A multi-currency book is **not** converted to a base currency, so cross-currency comparisons are not apples-to-apples. |
| FX carry | **No FX swap / rollover / carry.** Spot only. Forward points, swap rates, rollover costs and hedging costs are not modelled. |
| Point-in-time data | **No point-in-time fundamentals, index membership, credit ratings, or sector classifications.** Universe membership is static per spec. |
| Correlation | **No covariance matrix**, so cross-asset correlation is never audited — including for look-ahead. |
| Options | **No options.** No pricing, Greeks, implied-volatility surface, expiry, assignment, or margin modelling. |
| Bonds | **No bond analytics.** No clean/dirty price, accrued interest, coupon cash flows, duration, convexity, default or recovery. |
| Alternatives | **No alternatives or real-asset valuation modelling.** No appraisal smoothing, valuation lag, liquidity gates, or redemption constraints. |
| Leverage costs | **No margin, financing, or collateral-haircut costs.** Gross exposure carries no cost of capital. |
| Futures | **No futures roll cost.** Yahoo's continuous series is used as published; its roll methodology is undocumented and not point-in-time. |
| Survivorship | **Not survivorship-free for individual equities.** The provider serves current listings only, so a stock-level universe silently drops delistings. Prefer ETFs, and say so whenever a stock list is used. |
| Robustness | **Leave-one-asset-out** is not implemented as a dedicated stage (cross-market replication over alternative universes serves a similar purpose). |
| Robustness | **Randomised asset inclusion** and **randomised rebalancing dates** are not implemented. |
| Robustness | **Top-1–5 trade removal** is not implemented. |
| Statistics | **No confidence intervals** are reported for expectancy, profit factor, Sharpe, or drawdown. Only Monte Carlo percentiles of the resampled distribution are given. |
| Execution realism | No limit orders, stop-order fills, queue position, latency, exchange halts, or true market impact. Costs do not widen with volatility. |
| Attribution | No Barra/Fama-French-style factor decomposition and no per-asset-class risk contribution. |
| Forward testing | **None.** The engine is retrospective only. Forward testing is a separate, manual process. |
| Intraday | 60-minute bars are **not dividend-adjusted** (the provider publishes no adjusted close at that interval), and splits inside a 60-minute session are not modelled. |

---

## Session / intraday mode

With `execution.mode: "session"` the data layer switches to 60-minute bars
collapsed to **one bar per session**: the 09:30–10:00 ET opening window. The
panel stays daily-shaped, so every metric, the walk-forward fold layout and
the 252 annualisation work unchanged — a session run returns *daily* returns.

```jsonc
"period":    { "interval": "60m", "start": null, "end": null },
"execution": {
  "mode": "session",        // switches the data layer to session panels
  "window": "first",        // the 09:30-10:00 ET opening bar
  "fill_at": "open",
  "flat_at_close": true,    // exit at the close of the bar you entered
  "cost_multiplier": 1.0,
  "max_participation": 0.10
}
```

Session boundaries are found by grouping the bars on their **UTC calendar
date** and taking the first bar of each group. The first (09:30 ET) and last
(15:30 ET) bars of a session always fall on the same UTC date as the
exchange-local session, so this recovers the opening window in both EDT and
EST — with no timezone database and no dependency on the Windows tz database.

### Enforced constraints

The session path has a few configurations that cannot produce a valid result.
Rather than let them fail quietly, **the engine raises a `ValueError` with an
actionable message for each of them:**

| Configuration | Why it is rejected |
|---------------|--------------------|
| `period.interval: "60m"` **without** `execution.mode: "session"` | The daily data path would collapse the intraday bars onto the **last** bar of each day — the 15:30 ET close — silently returning a plausible-looking daily backtest of the **wrong window**. |
| `execution.window: "all"` | The session panel is built as one bar per session. A multi-bar session panel would need different date keying and a different annualisation, and is **not implemented** — only `"first"` is meaningful. |
| `execution.fill_at: "close"` **with** `execution.flat_at_close: true` | The position would be entered and exited at the same price, realising exactly 0.00 per session while still paying the full round-trip cost. Use `fill_at: "open"`. |

The rule underneath all three: **a configuration that cannot produce a valid
result must fail loudly rather than return a confidently wrong number.**

### Other session-path limits

- **The provider caps 60-minute history at roughly two years.** Statistical
  power, not signal quality, is the binding constraint.
- **Finer intervals are refused by the provider**, and intraday bars are
  RTH only — no pre/post-market, so an overnight session cannot be tested.
- **A full-range intraday request is rejected (HTTP 422)**, so the intraday
  fetch must use an explicit `range`.
- The liquidity screen is not applied on this path.

See [`references/intraday-session-mode.md`](references/intraday-session-mode.md).

---

## Verification

Four suites, all run from the `scripts/` directory:

```bash
cd scripts
python selftest.py                   # behavioural controls, causality, invariants
python selftest_intraday.py         # session path (offline, synthetic)
python selftest_corporate_actions.py # dividend adjustment + cache schema (offline, synthetic)
python selftest_packaging.py         # packaged-layout imports (offline, synthetic)
```

**A green run is meaningful, and that is the point.** The suites are not
smoke tests:

- **Negative controls that MUST NOT pass.** `selftest.py` deliberately
  backtests strategies that should fail — high-turnover crypto
  micro-reversion, a single-instrument 50/200 crossover — and confirms the
  engine returns REFUTED or FRAGILE for them, not SUPPORTED. It also runs a
  positive control so the engine is not merely over-strict. **If a negative
  control comes back SUPPORTED, the engine is broken.** A validator that
  passes everything validates nothing.
- **A causality-by-truncation audit.** Every built-in feature is recomputed
  on a panel truncated to half the dates, and the first-half values must be
  bit-identical. This is what catches centred windows and full-sample
  normalisation — failures that never raise and only ever print beautiful
  backtests.
- **Seven invariants** that catch silent corruption: the accounting identity
  (`final_equity == capital + Σ trade P&L − costs`), the units contract (a
  price ratio is not a return), constraint precedence, the round-trip ledger,
  walk-forward partition integrity, and more. The reasoning behind each is in
  [`references/verification-invariants.md`](references/verification-invariants.md).

`selftest.py` needs **network access on a cold cache** — it backtests real
strategies against real fetched data. The other three suites are fully offline
and synthetic and take under a second.

After changing anything in `btengine.py`, `btvalidate.py`, `intraday.py` or
`disclosure.py`, re-run the suites before quoting a verdict. The accounting
identity and the causality audit are cheap and catch exactly the failures
that look like findings.

---

## Repository layout

```
.
├── scripts/            the engine — flat layout, standard library only
│   ├── btengine.py     stages 1-7: data, universe, features, signal, portfolio, execution, accounting
│   ├── btvalidate.py   stage 8: metrics, the validation gauntlet, the verdict
│   ├── disclosure.py   the A-I due-diligence sheet
│   ├── intake.py       the intake gate
│   ├── intraday.py     session-window mode
│   ├── intraday_probe.py  a session-window data probe
│   ├── run_backtest.py the CLI
│   └── selftest*.py    the four test suites
├── templates/          example spec files
├── references/         implementation reference documents
├── .cache/             market-data cache — git-ignored, regenerable
└── pyproject.toml      packaging metadata
```

The reference documents are the deep reference, and each is worth reading
before you change anything:

| Document | What it covers |
|----------|----------------|
| [`references/spec-schema.md`](references/spec-schema.md) | Every field the engine understands |
| [`references/pipeline-stages.md`](references/pipeline-stages.md) | What each stage does — and what bias it cannot remove |
| [`references/verification-invariants.md`](references/verification-invariants.md) | Each invariant and the failure it catches |
| [`references/interpreting.md`](references/interpreting.md) | How to read a verdict |
| [`references/intraday-session-mode.md`](references/intraday-session-mode.md) | Provider quirks and the DST-safe extraction |
| [`references/extending-the-engine.md`](references/extending-the-engine.md) | The stdlib replacement table and the exact shape a new feature must take |

### Installing it as a package

Optional — the scripts run fine straight from a checkout:

```bash
python -m pip install -e .
btlab --list-presets
```

---

## Contributing, license and attribution

Contributions are welcome. Please read
[`CONTRIBUTING.md`](CONTRIBUTING.md) first — in particular, the project is
**pure standard library by design**, and new features must pass the
causality-by-truncation audit and be added to the selftest feature sweep.

**License:** MIT. See [`LICENSE`](LICENSE). The repository owner should
replace the placeholder copyright holder and the `authors` field in
`pyproject.toml` with their own name before publishing.

**Data source:** market data comes from the **Yahoo Finance chart API**
(`query1.finance.yahoo.com/v8/finance/chart/…`), used free of charge and
without an API key. Yahoo's data is subject to Yahoo's terms of service, may
be corrected or delayed, and is redistributed here only as a fetched cache
for local analysis. The `.cache/` directory is git-ignored and is never
committed.

**Disclaimer:** this is a research and educational tool. A backtest is a
hypothesis test, not a forecast. Even a SUPPORTED verdict warrants paper
trading before any live capital — never "trade it".
