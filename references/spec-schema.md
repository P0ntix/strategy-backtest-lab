# Spec Schema — every field the engine understands

A spec is a JSON object. Only `name` is required; everything else has a
sensible default in `DEFAULT_SPEC` (btengine.py). Partial `portfolio`,
`execution`, `period`, `universe`, `screen` blocks are deep-merged with the
defaults.

```jsonc
{
  "name": "Human-readable strategy name (used for the report filename)",
  "hypothesis": "The economic claim, in the user's own words. Reported verbatim.",

  "universe": {
    "preset": "us_equity_sector",   // see UNIVERSE_PRESETS below
    "symbols": []                   // explicit list OVERRIDES preset
  },

  "period": {
    "start": "2010-01-01",          // ISO date; null/omitted = earliest available
    "end": null,                    // ISO date; null = latest
    "interval": "1d"                // 1d | 1wk | 1mo | 60m
  },

  "screen": {
    "min_adv_usd": 0                // drop symbols below this median dollar volume
  },

  "features": [                     // first entry is the default traded feature
    {
      "name": "momentum",           // must be in FEATURES
      "params": { "lookback": 252, "skip": 21 }
    }
  ],

  "signal": {
    "type": "cross_sectional",      // must be in SIGNALS
    "params": {
      "feature": "momentum",        // optional if only one feature is declared
      "long_rank": 0.75,
      "short_rank": 0.25
    }
  },

  "portfolio": {
    "gross_leverage": 1.0,          // sum of |weights| target
    "max_position": 0.2,            // per-name cap, as a fraction of equity (HARD cap, applied last)
    "vol_target": null,             // e.g. 0.15 -> inverse-vol size to 15% annual
    "vol_lookback": 60,
    "rebalance": "monthly",         // daily|weekly|monthly|quarterly|yearly
    "max_names": null,              // cap number of concurrent positions
    "net_target": null,             // force net exposure, e.g. 0.0 = neutral
    "long_only": false              // suppress the short leg
  },

  "execution": {
    "fill_at": "open",              // open | close (both causal; decision is at t-1)
    "cost_multiplier": 1.0,         // scale all costs; 2.0 = pessimistic
    "max_participation": 0.10,      // max 10% of a bar's dollar volume per order
    "mode": null,                   // null = daily path; "session" = intraday session path
    "window": "first",              // session mode: which bars of the session to keep
    "flat_at_close": false          // session mode: liquidate at the close of the entry bar
  },

  "capital": 100000.0,

  "intake": {                       // the gate's record of who said what
    "confirmed": [],                // the 12 questionnaire keys the user personally confirmed
    "variants_tried": null,         // int; drives the deflated-Sharpe penalty
    "notes": "",                    // free text; anything the user declined to answer
    "answers": {}                   // due-diligence answers, keyed "A.currency", "G.variants_tested"
  }
}
```

## Features

| name | key params | meaning |
|------|-----------|---------|
| `return` | `lag` | simple N-bar return |
| `momentum` | `lookback`, `skip` | time-series momentum over [i-skip-lb, i-skip]; `skip` avoids the reversal effect |
| `sma_gap` | `lookback` | (price / SMA) − 1 |
| `volatility` | `lookback`, `annualize` | rolling stdev of returns |
| `vol_ratio` | `short`, `long` | short vol / long vol — a volatility-regime measure |
| `zscore` | `lookback` | (price − SMA) / stdev |
| `rsi` | `lookback` | 0–100 |
| `max_drawdown` | `lookback` | drawdown from rolling peak (negative) |
| `skew` | `lookback` | rolling return skew |
| `volume_ratio` | `short`, `long` | short/long average volume |
| `bollinger_pct` | `lookback`, `nstd` | (price − SMA) / (nstd × stdev) |
| `atr_pct` | `lookback` | ATR / price |
| `ma_crossover` | `fast`, `slow` | binary 1.0 when fast SMA > slow SMA |
| `dist_to_high` | `lookback` | (price / rolling high) − 1 |

Every feature accepts `direction: -1` in `params` to invert it — use this for
contrarian readings rather than writing a second feature.

## Signals

| type | params | behaviour |
|------|--------|-----------|
| `cross_sectional` | `long_rank`, `short_rank` | ranks the feature across the universe **each bar**; long the top `(1 − long_rank)`, short the bottom `short_rank`, flat between |
| `time_series_threshold` | `threshold` | +1 when feature > threshold, else flat |
| `time_series_zscore` | `entry`, `exit` | mean-reversion — fades \|z\| > entry, exits when \|z\| < exit |
| `crossover` | — | passes the binary `ma_crossover` feature straight through |
| `constant` | `weight` | fixed weight; the buy-and-hold baseline |

## Execution: daily vs session mode

The `execution` block chooses the path. Everything else in the spec is
identical across paths.

| field | values | meaning |
|-------|--------|---------|
| `mode` | `null` (default) | daily path — one bar per trading day |
| `mode` | `"session"` | intraday session path — 60-minute bars collapsed to one opening-window bar per session |
| `window` | `"first"` (default) | keep the earliest bar of each session (the 09:30–10:00 ET opening window) |
| `window` | `"all"` | accepted by the spec but **not usable** — see below |
| `flat_at_close` | `false` (default) | positions may be held across bars |
| `flat_at_close` | `true` | every position is liquidated at the close of the bar it was opened in |

### `mode: "session"`

The data layer switches to 60-minute bars and collapses each trading session
to **one bar** — the opening window (09:30–10:00 ET). The result is a
**daily-shaped panel**: one bar per session, one row per date. Consequences:

- The session path produces **DAILY returns**, so it annualises at **252**,
  not at a per-hour rate. Every metric, the walk-forward folds and the
  Sharpe annualisation are unchanged from the daily pipeline.
- `period.interval` is ignored on this path — 60-minute bars are fetched
  regardless of what the spec says.
- `screen.min_adv_usd` is **not applied** on this path; the liquidity screen
  is a daily-path step only.
- `flat_at_close: true` exits at the close of the same bar the position was
  entered in, so the position is opened and closed within one bar and
  **no overnight exposure is ever taken**.

`window: "all"` is documented in `references/intraday-session-mode.md` as
keeping every RTH bar. The implementation does not do that: the extraction
returns only the *opens* of the session's bars, so the assembled panel is not
a valid OHLCV bar and the run is meaningless. Use `"first"`. If a multi-bar
session window is ever needed, the extraction has to be extended to carry
`[o,h,l,c,v]` per bar and the panel rebuilt around that — see
`references/extending-the-engine.md`.

Provider quirks, the DST-safe session extraction and the probe script are in
`references/intraday-session-mode.md`.

## Session features

Registered by `scripts/intraday.py` into the shared `FEATURES` registry, and
therefore only available once `execution.mode` is `"session"` has run (the
registration happens inside the session branch of `run_spec`). A daily-mode
spec naming one of them raises `KeyError: Unknown feature`.

| name | key params | formula |
|------|-----------|---------|
| `session_drift` | `lookback` (default 1) | `close_location = (close − low) / (high − low)`, then `drift = (close_location − 0.5) × volume`. `lookback > 1` averages the raw drift over the trailing `lookback` sessions (all of which must be present, else `None`) |
| `session_gap` | none | `open[i] / close[i−1] − 1` — this session's open against the previous session's close. `None` at the first bar |
| `session_return` | none | `close[i] / open[i] − 1` — the opening window's open-to-close return |

`session_drift` is `None` on any bar with a zero range (`high == low`), a
missing high/low/close, or no volume. `volume` is the **opening-window**
bar's volume, not the full session's.

All three obey the causality contract: the value at index `i` uses bars
`<= i`. A session's close is known at 10:00 ET, so a signal from session
`i−1` can be traded in session `i`.

## The `intake` block

The gate's record of what the user personally supplied. It is written by the
helpers in `scripts/intake.py`, never by the agent inventing values.

```jsonc
"intake": {
  "confirmed": ["hypothesis", "universe", "..."],   // the 12 questionnaire keys
  "variants_tried": 1,                              // int; drives the deflated-Sharpe penalty
  "notes": "free text: what the user declined, what was assumed",
  "answers": {                                      // due-diligence answers
    "A.currency": "USD, unhedged",
    "G.variants_tested": "1 - not really tuned"
  }
}
```

| field | written by | consumed by |
|-------|-----------|-------------|
| `confirmed` | `intake.mark_confirmed(spec, keys)` | `intake.validate_spec` — the gate refuses to run until all 12 keys are present |
| `variants_tried` | `intake.mark_confirmed(spec, keys, variants_tried=N)` | the deflated-Sharpe penalty, and `G.variants_tested` on the disclosure sheet |
| `notes` | `intake.mark_confirmed(spec, keys, notes=...)` | the report; free text, no validation |
| `answers` | `intake.record_answer(spec, key, value)` / `intake.record_answers(spec, mapping)` | the A–I due-diligence sheet (`references/interpreting.md`) |

`answers` keys are the question keys from the sheet: `"A.currency"`,
`"G.variants_tested"`, `"A.rejection_criteria"`, and so on. Values are
stored as strings; passing `None` removes the key.

A spec passes the gate only when **both** hold: all 12 questionnaire keys are
in `confirmed`, **and** the corresponding spec field carries a real value
(not blank). Four fields additionally may not be left at an engine default:
`period.start`, `portfolio.rebalance`, `portfolio.gross_leverage`,
`execution.cost_multiplier`. Anything else is a finding to report, not a gap
to paper over.

## Portfolio sizing: how the caps interact

The `portfolio` block is applied in this order, and the order is the whole
point:

1. raw signal per symbol, restricted to the names eligible **at this bar**
2. optional inverse-volatility scaling (`vol_target` + `vol_lookback`)
3. normalise to the target gross (`gross_leverage`)
4. optional volatility targeting — scale the book to `vol_target`
5. optional `net_target` shift
6. apply `max_position` as a **HARD cap** — last

### `max_position` is a hard cap applied last

`max_position` is applied **after** every other sizing step, so none of them
can silently undo it: not the gross normalisation, not the volatility target,
not the net-target shift. The consequence must be read honestly: **if the cap
binds, realised gross is lower than `gross_leverage`.** A cap and a target
gross are simultaneously satisfiable only when `N × max_position >=
gross_leverage` for a universe of `N` instruments.

**Worked example.** 4 instruments, a top/bottom-quartile cross-sectional
split (so 1 long + 1 short), `gross_leverage: 1.0`, `max_position: 0.10`:

| step | long | short | gross |
|------|------|-------|-------|
| raw signal | +1.0 | −1.0 | 2.0 |
| normalised to `gross_leverage` 1.0 | +0.50 | −0.50 | 1.00 |
| `max_position` 0.10 applied | +0.10 | −0.10 | **0.20** |

The maximum achievable gross is `2 × 0.10 = 0.20`, not `1.0` — because only
two of the four names are ever live, and each is clipped to 10%. The spec
asked for 1.0x gross and the run delivers 0.2x. That is the honest reading
of the user's stated constraints, not an engine defect; state it in the
report. `scripts/selftest.py` TEST 8 asserts both halves of the invariant
(no weight above the cap, realised gross never above the target).

### `net_target` is a shift, not a scale

`net_target` moves the book's net exposure to the requested value by adding
the **same constant to every weight**:

```
offset = (net_target - Σw) / N
w_s   += offset
```

A uniform *scale* — the intuitive implementation — cannot reach the target
in three common cases: it divides by zero when the book is already fully
invested (`net == 1.0`), it **inverts** the book when the target has the
opposite sign, and it is a no-op when the book is already net-flat but the
target is not zero. The shift handles all three, preserves the book's
relative structure, and leaves gross unchanged for a balanced book.

### `vol_target` sets the size of the book

Weights are set proportional to `1 / volatility` — that is the risk-parity
*shape* — and then the whole book is scaled so the portfolio's estimated
annual volatility equals `vol_target` — that is the *level*. `vol_target:
0.10` and `vol_target: 0.40` produce genuinely different books.

Portfolio volatility is estimated with a **diagonal covariance** (pairwise
correlations assumed zero), which *overstates* the volatility of a genuinely
diversified multi-asset book. Realised volatility therefore lands **below**
the target: read `vol_target` as an upper bound on realised vol, not an exact
setting.

### `max_names` is decided per bar

Universe membership is re-evaluated at each rebalance using only information
available at that bar, and the surviving names are the **strongest**, not the
alphabetically first. Ranking uses the cross-sectional percentile rank of the
traded feature (how extreme the name's rank is), with the signal magnitude
as a fallback and the symbol name as a deterministic final tie-break. A name
that first produces a signal on the final bar of the sample is not in the
book before that bar, and a name that stops signalling leaves the book on
the next rebalance.

## Universe presets

| preset | contents |
|--------|----------|
| `us_equity_broad` | SPY, VTI, IVV, RSP |
| `us_equity_sector` | 11 SPDR sector ETFs |
| `us_equity_style` | SPY, IWM, MDY, RSP, QQQ, DIA |
| `futures_broad` | ES/NQ/YM/RTY/ZN/ZB/ZF/ZT/GC/SI/CL/NG/HG/ZC/ZS |
| `fx_major` | 8 G10 pairs |
| `fx_emerging` | 8 EM pairs |
| `commodities` | metals, energy, ags |
| `crypto` | 10 majors |
| `rates_credit` | ^TNX/^TYX/^FVX/^IRX + TLT/IEF/SHY/LQD/HYG/EMB/TIP/BND |
| `intl_equity` | 14 country ETFs |
| `real_assets` | VNQ, XLRE, DBC, DBA, TIP, REM, RWO |
| `multi_asset` | SPY, TLT, GLD, DBC, EFA, EEM, VNQ, BTC-USD |
| `risk_parity_ish` | SPY, TLT, GLD, VNQ, EFA, EEM |
| `risk_on_risk_off` | SPY, QQQ, IWM, TLT, GLD, USO, BTC-USD |

A preset is just a symbol list — copy any into `universe.symbols` and edit.

## Worked examples

**Cross-sectional momentum (needs ≥2 symbols)**
```json
{"features": [{"name": "momentum", "params": {"lookback": 252, "skip": 21}}],
 "signal": {"type": "cross_sectional", "params": {"long_rank": 0.75, "short_rank": 0.25}},
 "portfolio": {"rebalance": "monthly", "long_only": true, "max_position": 0.2}}
```

**Single-asset mean reversion**
```json
{"universe": {"symbols": ["SPY"]},
 "features": [{"name": "zscore", "params": {"lookback": 10}}],
 "signal": {"type": "time_series_zscore", "params": {"entry": 2.0, "exit": 0.5}},
 "portfolio": {"rebalance": "daily", "gross_leverage": 1.0}}
```

**Risk-parity across asset classes**
```json
{"universe": {"preset": "risk_parity_ish"},
 "features": [{"name": "volatility", "params": {"lookback": 60, "annualize": true}}],
 "signal": {"type": "time_series_threshold", "params": {"threshold": 0.0}},
 "portfolio": {"rebalance": "monthly", "vol_target": 0.10, "vol_lookback": 60}}
```

**Vol-regime breakout**
```json
{"universe": {"preset": "commodities"},
 "features": [{"name": "bollinger_pct", "params": {"lookback": 20, "nstd": 2.0}}],
 "signal": {"type": "time_series_threshold", "params": {"threshold": 0.0}},
 "portfolio": {"rebalance": "weekly", "gross_leverage": 1.0, "max_position": 0.15}}
```

**Session drift (intraday opening window)**
```json
{"universe": {"preset": "us_equity_broad"},
 "period": {"start": "2024-10-24", "end": null, "interval": "60m"},
 "features": [{"name": "session_drift", "params": {"lookback": 1}}],
 "signal": {"type": "cross_sectional", "params": {"long_rank": 0.75, "short_rank": 0.25}},
 "portfolio": {"rebalance": "daily", "gross_leverage": 1.0, "max_position": 0.10},
 "execution": {"mode": "session", "window": "first", "fill_at": "open",
               "flat_at_close": true, "cost_multiplier": 1.0, "max_participation": 0.10}}
```
The panel is one bar per session, so this run returns DAILY returns and
annualises at 252. `flat_at_close: true` closes every position at the close
of the bar it was opened in — no overnight exposure. A full worked spec is
in `templates/session-drift-intraday.json`.

## Cost realism

Costs come from `DEFAULT_COSTS` per inferred asset class. Override by
multiplying: `execution.cost_multiplier = 2.0` models a worse-than-typical
venue or a small account paying spreads. Crypto is already set ~10× the ETF
cost, which is why high-turnover crypto strategies usually die at 2× stress.