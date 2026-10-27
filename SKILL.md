---
name: strategy-backtest-lab
description: "Validate a trading hypothesis by backtesting it."
version: 1.0.0
author: HP, Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [backtesting, trading, quant, validation, strategy, multi-asset]
    related_skills: [adversarial-code-audit]
---

# Strategy Backtest Lab

Takes a trading hypothesis in plain language, encodes it as a rules-based
strategy, runs it through a full eight-stage backtest pipeline against real
historical data, and returns a blunt verdict: **SUPPORTED / FRAGILE / REFUTED /
INCONCLUSIVE**. Works across equity, FX, futures, crypto, rates and mixed
multi-asset combinations.

Zero dependencies: pure Python stdlib, no numpy/pandas install, free data via
the Yahoo chart API. Designed for low-disk machines — installing nothing, and
the data cache costs ~20 MB even after testing ~50 symbols across every asset
class (vs ~200 MB for numpy+pandas+yfinance alone).

## When to Use

- The user states a trading idea and wants to know **whether it would actually
  have worked** before risking capital.
- A user asks to "backtest my strategy", "test this hypothesis", "validate this
  trading rule", or "is this idea real?".
- Comparing a hypothesis across asset classes ("does this work in FX too?").
- Auditing an existing system for look-ahead / overfitting before going live.
- A strategy that trades a specific intraday session window (e.g. the first
  hour of the US session) — supported via `execution.mode: "session"`.

**Don't use for:**
- Forward-testing or paper trading a *known* strategy (this is retrospective).
- Live broker execution, order routing, or position sizing for real accounts.
- Options Greeks (gamma exposure), order-book microstructure, or true tick
  replay — the engine works at bar resolution and models costs analytically,
  not from the book. A hypothesis that depends on those inputs must be
  decomposed (Procedure step 3) before it can be tested here.

## The Intake Rule (non-negotiable)

**Every condition of the strategy is imputed by the user. Never invent one.**

Do not choose the universe, the lookback, the entry/exit rule, the rebalance
frequency, the sizing method, the leverage, the costs, the date range, or the
economic rationale on the user's behalf. A strategy you designed yourself is
not the user's hypothesis — it is yours, and testing it answers a question
nobody asked.

This is enforced, not aspirational. `run_backtest.py` **refuses to execute**
(exit code 2) any spec the user has not explicitly confirmed, and prints the
outstanding questions. `--assume-defaults` exists only for a user who has
explicitly declined to answer; using it is itself a finding to report.

Ask for inputs **before** running anything. The full questionnaire:

```bash
python scripts/run_backtest.py --intake          # print all 12 core questions
python scripts/run_backtest.py --config X --intake-check   # what's still missing
```

The twelve core conditions:

| # | Condition | Question |
|---|-----------|----------|
| 1 | `hypothesis` | The claim in the user's words, and what **drives** it |
| 2 | `universe` | Which instruments, which asset class |
| 3 | `direction` | Long only / long-short / market-neutral |
| 4 | `entry` | The exact trigger — a rule, not an indicator name |
| 5 | `exit` | What closes the position |
| 6 | `timeframe` | Lookback / holding period |
| 7 | `rebalance` | How often it may trade |
| 8 | `sizing` | Equal weight / inverse-vol / fixed-fractional / max per name |
| 9 | `leverage` | Max gross exposure, max weight per instrument |
| 10 | `costs` | Actual commission/spread/slippage, or use class defaults |
| 11 | `period` | Date range to test |
| 12 | `variants_tried` | How many variations were tried while developing it |

Ask in batches of 3–4 (the `clarify` tool caps at 5 per call). Record answers
with `intake.mark_confirmed(spec, keys, variants_tried=N)`.

**"I don't know" is a valid answer — record it as the answer.** A user who
cannot state an exit rule or mechanism has told you something important;
report it rather than filling the gap.

## The A–I Due-Diligence Sheet

A full multi-asset review asks ~109 questions across nine sections (strategy
scope, data integrity, instrument modelling, look-ahead, costs & capacity,
portfolio construction, robustness, metrics, forward testing).

**Do not dump all 109 on the user.** Each question is pre-classified by who
can answer it:

| who | meaning |
|-----|---------|
| `engine` | The engine answers it with a **verified** fact about its own behaviour (data source, timestamp convention, survivorship treatment, execution model, what the gauntlet covers) |
| `user` | Only the user can answer it (rationale, base currency, hedging policy, discretionary overrides, forward-test criteria) |
| `both` | The user sets the parameter; the engine reports the value actually used |

Read the live split from `--disclose` rather than a number written here — the
engine-answerable set grows every time a capability mapping is added, so any
count in prose goes stale, and two documents quoting different counts is worse
than no count. The rule that matters: **the majority of the sheet is answerable
by the engine, and the user is never asked something the engine already
knows.** An engine-classified question that renders `[UNKNOWN]` is a broken
mapping, not a question for the user — `disclosure.unmapped_engine_questions()`
lists them.

So the workflow is:

```bash
python scripts/run_backtest.py --disclose            # full A-I sheet + what is still needed
python scripts/run_backtest.py --disclose-section D   # one section
```

`--disclose` prints the sheet with provenance markers and lists the questions
still needing the user. **Read that list and ask only those** — the user is
never asked something the engine already knows.

The sheet lands in the report under "Due-Diligence Disclosure" with a coverage
line (`N/109 answered`). Anything neither the user nor the engine answered is
printed as **UNKNOWN** rather than omitted — an unknown is a finding, and
hiding it would be the exact failure this sheet exists to prevent.

Record user answers with `intake.record_answer(spec, "A.currency", "USD, unhedged")`
(keys are like `A.currency`, `G.variants_tried`).

**Engine answer keys** (verbatim, from `disclosure.py`):

- `A` scope · `B` data integrity · `C` per-asset-class modelling
- `D` look-ahead & leakage · `E` costs, execution, financing, capacity
- `F` portfolio construction, cross-asset risk, correlation
- `G` robustness, overfitting, statistical validity
- `H` metrics to report · `I` forward testing

Honest limits to carry into every multi-asset answer: **no FX conversion or
hedging**, no point-in-time fundamentals or index membership, no covariance
matrix (so correlation is never audited), no options, no bond analytics, no
margin/financing costs, no futures roll cost, and **not** survivorship-free for
individual equities. Leave-one-asset-out, randomized rebalancing, top-trade
removal, and confidence intervals are **not implemented** — say so rather than
implying coverage.

## Prerequisites

Nothing to install. Confirm only:

```bash
python -c "import sys; print(sys.version)"
```

Requires Python 3.8+. Data comes from the Yahoo chart API — no API key, no
account. **First run needs internet; later runs hit a local cache** in `.cache/`
next to the scripts.

**Keep it dependency-free.** On a disk-constrained machine, do not add numpy,
pandas or yfinance to extend this engine — reimplement with `statistics` /
`urllib` / `random`. `references/extending-the-engine.md` has the stdlib
replacement table and the exact shape a new feature or signal must take.

## How to Run

All paths are relative to this skill's directory.

```bash
python scripts/run_backtest.py --list-presets     # universes / features / signals
python scripts/run_backtest.py --intake          # the 12 questions to ask the user
python scripts/run_backtest.py --example > strat.json   # template spec
python scripts/run_backtest.py --config strat.json --intake-check  # what's missing
python scripts/run_backtest.py --config strat.json --quick
python scripts/run_backtest.py --config strat.json            # full gauntlet
python scripts/selftest.py                                   # validate the lab
python scripts/selftest_intraday.py                         # validate the session path
python scripts/selftest_corporate_actions.py                # validate dividends + cache
python scripts/selftest_packaging.py                      # validate the install layout
python scripts/intraday_probe.py                            # session-window data probe
```

`selftest.py` must be run from the `scripts/` directory (it reads
`../templates/*.json` by relative path). `selftest_intraday.py`,
`selftest_corporate_actions.py` and `selftest_packaging.py` have no such
constraint — they run from anywhere, need no network, and take under a
second each.

Useful flags: `--intake` (print the questionnaire), `--intake-check` (report
outstanding conditions), `--quick` (skips Monte Carlo, parameter sweep,
replication), `--refresh` (bypass cache), `--n-variants N` (how many parameter
variants the user tried while developing — feeds the deflated-Sharpe penalty),
`--assume-defaults` (bypass the intake gate; only for a user who has explicitly
declined to answer), `--no-report` (stdout only).

**Exit codes:** `0` ran, `2` intake incomplete and refused to run.

## The Pipeline

Stages 1–7 live in `scripts/btengine.py`, stage 8 in `scripts/btvalidate.py`.
Full reference: `references/pipeline-stages.md`. Every spec field:
`references/spec-schema.md`. Engine self-checks and their reasoning:
`references/verification-invariants.md`. Reading a verdict:
`references/interpreting.md`.

| Stage | Code | What happens |
|-------|------|--------------|
| 1 Data | `fetch_batch`, `load_or_fetch`, `audit_panel` | Fetch OHLCV, cache to JSON, integrity audit |
| 2 Universe | `UNIVERSE_PRESETS`, `build_panel` | Align dates, forward-fill, liquidity screen |
| 3 Features | `FEATURES` registry | Causal transforms (momentum, zscore, RSI, vol…) |
| 4 Signal | `SIGNALS` registry | Rules → target weight per bar |
| 5 Portfolio | `construct_target_weights` | Capping, leverage, vol-targeting, rebalance |
| 6 Execution | `simulate` | Fill at **t+1** with spread, slippage, commission, borrow, ADV cap |
| 7 Accounting | `simulate` | Mark-to-market equity curve, daily returns, trade ledger |
| 8 Validation | `full_gauntlet` | Walk-forward, cost stress, gross-vs-net, param sweep, Monte Carlo, regimes, capacity, replication, causality → verdict |

**Intraday / session mode.** With `execution.mode: "session"` the data layer
switches to 60-minute bars collapsed to one **opening-window bar per session**
(09:30–10:00 ET), and `execution.flat_at_close: true` exits at that same bar's
close. The panel stays one bar per day, so every metric, the walk-forward
folds and the 252 annualisation work unchanged. Provider quirks, the DST-safe
session extraction and the spec fields are in
`references/intraday-session-mode.md`.

## Procedure

**Nothing runs before step 3 is complete.** Each step ends with a checkable
criterion.

1. **Run the intake gate first.** Call `--intake-check` (or start from the
   questionnaire) before writing any parameter yourself. Do not open a spec and
   start filling fields — that is exactly the failure mode this gate exists to
   stop.
   *Done when:* you have the 12 conditions below in the user's own words.

2. **Ask the user for what only they can answer.** First run
   `python scripts/run_backtest.py --disclose` and read the "STILL NEEDED FROM
   THE USER" list — the engine answers most of the 109 due-diligence questions
   with verified facts. Ask only what remains, using `clarify` in
   batches of 3–4. Do not answer your own questions. Do not offer a default and
   treat silence as consent. For anything the user cannot answer, record
   `unknown` and carry it into the report — an unspecified exit rule or
   mechanism is a finding about the hypothesis, not a gap for you to paper over.
   *Done when:* `--intake-check` reports **all 12 confirmed**, or the user has
   explicitly declined the ones they won't answer.

3. **Check the hypothesis is testable with the data this engine can actually
   obtain — and if it is not, decompose it and let the user choose.** Some
   inputs are simply unavailable: options Greeks (gamma exposure), order-book
   microstructure, tick data, exchange positioning. Probe the provider before
   promising a result, then present the **decompositions that ARE testable**
   and let the user pick one:
   - test the component you can measure (e.g. net drift) and state plainly
     which leg of the hypothesis was not tested;
   - test at a coarser resolution where the signal is observable;
   - ask the user to restate the rule in terms this engine can compute.
   Never silently substitute a proxy. A proxy test answers a different
   question, and the user must know which question got answered.
   *Done when:* the user has chosen a decomposition and knows which parts of
   their hypothesis the run will and will not cover.

4. **Encode the spec from their answers only.** Map their words to a feature +
   signal — do not substitute a strategy you think is better:
   - trend persistence → `momentum` + `cross_sectional`
   - oversold bounce → `zscore` + `time_series_zscore` (fades extremes)
   - breakout / vol expansion → `bollinger_pct` + `time_series_threshold`
   - regime filter → `volatility` or `sma_gap` + `time_series_threshold`
   - risk-parity tilt → `volatility` + `vol_target` in the portfolio block
   - session-window drift → `session_drift` + `cross_sectional` (intraday)
   Ask before substituting anything else. Prefer a preset the user recognised
   over one you picked.
   *Done when:* `intake.confirmed` lists all 12 keys and the spec matches what
   they said.

5. **Confirm the trial count.** Pass `--n-variants` with the real number from
   step 2. Reporting the best of N trials is the single most common way a
   backtest lies, and the engine cannot detect it.
   *Done when:* `--n-variants` reflects what they told you.

6. **Run `--quick`, read it with the user, then run the full gauntlet.** Do not
   present in-sample numbers as the answer — point at pooled **OOS** Sharpe
   and the fold table. If OOS is well below in-sample, that gap *is* the result.
   *Done when:* a report file exists and the verdict is read against
   `references/interpreting.md`.

7. **Report the verdict with its flags, not alone.** Every SUPPORTED that is
   really FRAGILE is a failure of honesty, not of the tool. List the flags
   verbatim. State plainly that even SUPPORTED means "warrants paper trading",
   never "trade it".
   *Done when:* verdict + flags + the single largest caveat are all stated.

8. **Stress the claim before stopping** when the verdict is SUPPORTED: re-run
   with the real `--n-variants`, and re-run on a different universe to check
   replication. A verdict that flips under those two changes is FRAGILE and must
   be reported that way.
   *Done when:* the verdict has been checked against at least one adverse
   assumption.

9. **Report anything the user left unspecified.** If `--assume-defaults` was
   used, or any condition is `unknown`, say so in the answer. An unstated exit
   rule or an unexamined variant count weakens the result regardless of the
   verdict.
   *Done when:* every assumption and every unknown is named in the response.

## Verdicts

| Verdict | Meaning | Next step |
|---------|---------|-----------|
| **SUPPORTED** | Pooled OOS Sharpe ≥ 0.5 and every robustness stage passed | Paper trade; do not size up on this evidence |
| **FRAGILE** | Positive OOS but a robustness stage failed, or OOS evidence only | Identify the failing stage; treat as unproven |
| **REFUTED** | OOS Sharpe < 0.5, or the edge dies under cost stress | Report what specifically killed it, and whether gross-of-costs was positive (real but untradeable) or negative (no edge at all) |
| **INCONCLUSIVE** | Too few trades / too short a sample to tell signal from noise | More data or a different design, not a better story |

A hard **REFUTED** is also returned when the causality audit fails — the
pipeline used future data and every number is void.

## Pitfalls

- **Never fill a strategy condition in yourself.** Not the universe, not the
  lookback, not the exit, not the rationale. The intake gate enforces this with
  exit code 2 — do not reach for `--assume-defaults` to get past it. It exists
  only for a user who explicitly declines to answer, and that must be reported.
- **Never quote in-sample Sharpe as the result.** It prints first; the pooled
  OOS figure is the decision-relevant one.
- **Yahoo returns current tickers only.** Stock-level universes silently drop
  delistings. Prefer ETFs, and say so whenever a stock list is used.
- **`XLC` and `XLRE` start late** (2018 / 2015 sector launches), which shortens
  the panel and costs lookback warm-up.
- **`--n-variants` defaults to 1.** If the user tuned anything, the deflated
  Sharpe is optimistic without it. Always ask.
- **Crypto trades 7 days; ETFs don't.** Cross-asset comparisons mix bar counts
  — check `n_bars` before comparing CAGRs.
- **The walk-forward is honest, not generous**: each fold recomputes features on
  a sliced panel, so long lookbacks lose warm-up at fold starts. That depresses
  OOS slightly. It is a conservatism, not a bug — don't "fix" it.
- **A report file is written into the CWD**, named after the strategy. Keep it;
  it is the evidence artifact.
- **The disclosure sheet and the reference docs are a CONTRACT — update them
  in the same pass as the code.** The A–I sheet states what the engine does
  and does not do; when a behaviour changes and the sheet still describes the
  old behaviour, the sheet hands the user a false claim. Grep the reference
  docs for the old semantics after every behaviour change ("level cancels",
  "applied after", "equals costs") and edit the sentence that misled rather
  than appending a correction note.
- **Cost defaults are per-asset-class and conservative** (`DEFAULT_COSTS`), not
  your broker's actual fees. If the user knows real costs, adjust
  `execution.cost_multiplier`.
- **Single-instrument strategies usually fail the trade-count bar.** A 50/200
  crossover on SPY over 22 years is only ~11 round-trips, so the engine returns
  FRAGILE with "only N trades" — correct, not a bug. Low-frequency rules on one
  instrument need either a longer sample or several instruments.
- **`rebalance` means exactly N times per year.** One bar per period is marked,
  so `monthly` is 12 rebalances, not "the first week of each month". Orders are
  never sent between rebalances.
- **The rebalance mask gates ORDER GENERATION, not the fill.** An order
  decided on a rebalance bar fills on the next bar, whether or not that bar is
  itself marked. If the mask gates the fill instead, the order sits pending
  until the *next* rebalance bar — a full period late for a monthly strategy,
  and the final rebalance of the sample never executes. The bug is invisible
  in the output: the strategy still trades and the curve still looks
  plausible; only the timing is wrong.
- **Size a liquidity cap against LAGGED volume.** An order filled at bar i's
  open cannot know bar i's volume; using the same bar's volume is intra-bar
  look-ahead, and when that volume is zero the cap silently disappears and an
  order of any size passes. Use the trailing dollar volume strictly before
  the fill bar, and refuse the fill when there is none.
- **Never fill an order on a stale bar.** A forward-filled price is a price
  the market did not print. Mark the position with it, but do not fill on it,
  or a halted or delisted name trades indefinitely at a phantom price.
- **A misconfiguration must raise, not degrade.** An unknown `rebalance`
  string that falls through to a one-bar mask produces a flat backtest and a
  verdict that reads as "no edge" — a typo becomes a finding. A
  single-instrument universe with a cross-sectional signal does the same.
  Both must raise with an actionable message.
- **The library and the CLI must evaluate the same specification.** If one
  applies the defaults and the other does not, the same strategy gets two
  different verdicts depending on how it was invoked. Share one merge helper.
- **A price ratio is not a return.** If a container named `rets` holds
  `close/prev_close` instead of `close/prev_close − 1`, every consumer that
  assumes a return breaks *differently*: stdev and skew are location-invariant
  and silently stay correct, while RSI (every bar reads as a gain) pins at 100
  and threshold logic inverts. Assert both a plausible magnitude and a
  non-degenerate RSI — see `references/verification-invariants.md`.
- **One trade = one position lifecycle.** Emit a ledger record when a position
  returns to flat, not on each partial rebalance adjustment — otherwise the
  trade count inflates by orders of magnitude and every slice is measured
  against the original entry price, so the ledger can report a 74% win rate on
  a strategy that lost 28% a year. The accounting identity still passes,
  because the arithmetic is right and the unit of observation is wrong.
- **Walk-forward test blocks must not overlap.** Pooled OOS is only valid if
  the test windows form a partition; an overlapping final fold re-counts its
  strongest period and produces a headline Sharpe above every individual
  fold. Read the fold date ranges against each other, not just the Sharpes.
- **Apply concentration caps last.** A cap applied before the gross rescale is
  undone by it — and so is one applied before the volatility target or the
  net-target shift. Order: raw signal → inverse-vol → normalise to target
  gross → volatility target → net-target shift → clip. Accept that a binding
  cap means realised gross < target gross.
- **A net-exposure target is a SHIFT, not a scale.** Adding the same constant
  to every weight reaches any target; multiplying every weight by one factor
  cannot — it divides by zero on a fully invested book, inverts the book when
  the target has the opposite sign, and no-ops on an already-flat book. The
  scale version looks reasonable and is wrong in exactly the three cases a
  market-neutral mandate hits.
- **`vol_target` sets the book's size, and the level is honoured.** Weights are
  set proportional to `1/volatility` (risk-parity shape) and then scaled so
  the portfolio's estimated annual volatility equals the target. Portfolio vol
  uses a diagonal covariance (correlations assumed zero), so on a diversified
  book the estimate is too high and realised vol lands BELOW the target — read
  `vol_target` as an upper bound, not an exact setting.
- **Decompose gross-of-costs before writing the verdict.** A strategy can fail
  for two opposite reasons: no edge at all, or an edge too small to trade. Run
  it again with `execution.cost_multiplier: 0` and compare the two Sharpes. If
  gross is positive and net is negative, the report must say *real but
  untradeable at this size and frequency* — a frequency/sizing problem the
  user may solve by trading less often. If gross is also negative, the signal
  itself has no expectancy in the sample and costs are not the primary issue.
  Never write "the edge disappeared under costs" before checking gross: that
  sentence asserts a gross edge exists, and it is false when there is none.
- **A binding constraint makes other parameters inert — and a perfect sweep
  stability can be measuring exactly that.** When a per-name cap binds on every
  bar, every position takes the same weight, so sizing parameters (inverse-vol
  targets, weight caps) cannot change the result. A parameter sweep then
  reports 100% stability, which reads as a robustness plateau but is the
  parameter being unable to move anything. Confirm a sweep can move: perturb,
  check the Sharpe or the realised weights actually changed, and inspect the
  distinct realised weights (one repeated value everywhere means the cap
  dominates). Skip degenerate perturbations rather than reporting them as
  evidence of robustness.
- **A cross-sectional signal that scales by raw volume is mostly a volume
  ranking.** Multiplying a directional measure by raw volume is harmless within
  one instrument but, across a universe whose volumes differ by 10x, the rank
  becomes dominated by size rather than direction — the opposite of the rule
  the user described. Check what the rank is actually ordering: compare the
  feature's cross-sectional spread against the instruments' volume spread
  before trusting the signal, and say so if the user's rule cannot be encoded
  without changing its meaning.
- **Precompute rolling windows once per series, not inside a per-bar loop.**
  Calling a rolling-window feature from inside the bar loop makes the
  portfolio layer O(n²) in window work: a 500-bar session panel with
  inverse-vol sizing took over 400 seconds per run, against 1.1 seconds once
  the volatility series was computed once per symbol outside the loop. A
  validation gauntlet re-runs the strategy a dozen times, so a quadratic
  sizing layer turns a 30-second run into a timeout. Compute each
  per-symbol series once, then index it.
- **Intraday history is short and provider-capped.** 60-minute bars reach back
  roughly two years; finer intervals may be refused outright. Plan the sample
  size before promising statistical power.
- **Intraday bars are anchored to exchange-local time.** A fixed-UTC session
  filter breaks at every DST transition. Detect session boundaries per trading
  day instead — `references/intraday-session-mode.md`.
- **In session mode the panel is daily.** One bar per session, so returns
  annualise at 252. Annualising per intraday bar inflates Sharpe by the square
  root of the bar-count ratio.
- **A one-instrument universe needs a coverage floor of 1.** Any
  minimum-coverage filter that assumes at least two symbols discards every
  date and the panel comes back empty. Single-instrument strategies are a
  documented path, not an edge case.
- **When the user skips a free-text question twice, stop re-asking it
  open-ended.** Convert it to a set of concrete choices they can pick from, or
  let them select "I don't know" explicitly. Repeating an unanswered open
  question is noise; offering a choice list gets an answer in one round and
  keeps the skipped field visible as a recorded gap rather than a silent
  omission.

## Verification

Confirm the skill works — this runs negative controls that must NOT pass:

```bash
python scripts/selftest.py
```

Expect `RESULT: ALL CHECKS PASSED`, `negative controls correctly not
SUPPORTED: True`, and a count of how many negative controls were actually
checked — a guard that matched none is vacuous, so the count must be
non-zero. The selftest deliberately backtests strategies that should
fail (micro-reversion in crypto, a single 50/200 crossover) and confirms the
engine returns REFUTED/FRAGILE for them, plus a causality audit over every
built-in feature. **If a negative control comes back SUPPORTED, the engine is
broken — fix it before trusting any verdict.**

It also asserts the invariants that catch silent corruption — the full list,
with the failure each catches, is in
`references/verification-invariants.md`:

- **Accounting identity** — `final_equity == capital + Σ trade P&L`, exactly.
  The trade ledger is **net of costs**: every cost dollar is attributed to the
  position that caused it and netted out of that trade's P&L when the position
  closes. The ledger feeds profit factor, win rate, expectancy and the Monte
  Carlo; when it drifts from the equity curve those stats are fiction while
  Sharpe still looks fine. Profit factor, win rate and expectancy are
  therefore on the same basis as the Sharpe — the report labels them, and
  `sim["trade_pnl_basis"]` states which basis the ledger is on.
- **Causality by truncation** — every feature recomputed on half the data must
  reproduce its first-half values exactly. Catches centred windows and
  full-sample normalisation, which never raise.
- **Negative control** — a known-hopeless strategy must be REFUTED. A validator
  that passes everything validates nothing.
- **Units contract** — the return series holds returns (not price ratios), and
  no indicator is pinned (RSI spanning a range, not stuck at 0 or 100).
- **Constraint precedence** — no target weight exceeds its cap, and realised
  gross never exceeds the target gross.
- **Round-trip ledger** — one trade per position lifecycle, so a symbol's
  `[entry, exit]` intervals never overlap.
- **Partition integrity** — walk-forward test windows are disjoint, so pooled
  OOS never double-counts a period.
- **Rebalance timing** — a decision taken on a rebalance bar fills on the
  next bar, not on the next rebalance bar.
- **Net target reached, cap enforced** — `net_target` is hit exactly, across
  signs and on one-sided and two-sided books, and `max_position` binds as a
  hard limit.
- **Configuration errors raise** — an unknown `rebalance`, and a
  single-instrument universe with a cross-sectional signal, both raise
  instead of returning a flat and meaningless backtest.
- **CLI/library parity** — the same spec resolves to the same specification
  through either entry point.
- **No look-ahead in selection** — `max_names` membership is decided per
  bar, so a name that first signals on the final bar is not in the book
  before it.
- **Execution timing** — the liquidity cap is sized on lagged volume, and
  no order fills on a stale bar.
- **A guard that cannot fail is not a test** — the negative-control check
  asserts it actually matched a negative control, so a filter that matches
  nothing fails loudly instead of passing vacuously.

**After changing anything in `btengine.py` or `btvalidate.py`, re-run
`scripts/selftest.py` before quoting a verdict.** The accounting identity and
the causality audit are cheap and catch the failures that look like findings.

### The session path has its own suite

The daily invariants above do not cover session mode, which has its own
failure modes (a DST bug changes what a "day" means; a ledger bug multiplies
the trade count). Run it alongside:

```bash
python scripts/selftest_intraday.py
```

Expect `RESULT: ALL CHECKS PASSED`. It is offline, synthetic, and takes under
a second. Ten checks cover the session path specifically:

- **DST-safe extraction** — the 09:30 ET bar is 13:30 UTC in summer and
  14:30 UTC in winter; the extraction must find it on either side of the
  March transition, and must stay distinguishable from the daily path's
  collapser (which keeps the *last* bar of a date).
- **Causality of `session_drift`** — recomputed on half the sessions, it must
  reproduce its first-half values exactly.
- **No overnight exposure** — under `flat_at_close`, a bar's return must be
  its open→close move and must not contain the overnight gap.
- **Accounting identity in session mode** — on a winning *and* a losing run.
- **Round-trip trade count** — one completed trade per session per instrument,
  never one per rebalance slice.
- **The 60m fetch URL** — `range=2y`, because Yahoo answers 422 to a
  full-range intraday request.
- **Cache format migration** — an old date-keyed cache file is refetched, not
  served as if it were intraday bars.
- **Misconfigurations are rejected, not silently wrong** — `window: "all"`,
  `interval: "60m"` without `execution.mode: "session"`, and
  `fill_at: "close"` with `flat_at_close: true` each raise `ValueError`
  instead of returning a plausible-but-wrong result.

A third suite covers corporate actions and the cache:

```bash
python scripts/selftest_corporate_actions.py
```

Daily bars are **split- and dividend-adjusted** — the engine reads
`indicators.adjclose` and back-adjusts OHLC by `adjclose/close`, because
Yahoo's `quote` closes are split-adjusted only and every ex-dividend date
otherwise reads as a real loss (0.3–0.7% per ex-date on SPY). **Intraday 60m
bars are not** — the provider publishes no `adjclose` there, so an
ex-dividend date inside a 60m sample leaves a small uncompensated drop. The
cache carries a schema marker, so a daily cache written before the adjustment
is refetched rather than silently reused. Details in
`references/verification-invariants.md` §13.

### The installed layout has its own suite

```bash
python scripts/selftest_packaging.py
```

This repo is also published as an installable package (`btlab`). The `scripts/`
directory is a **flat layout** — modules import each other by top-level name
so they run as plain scripts — but a flat import raises
`ModuleNotFoundError` once the modules are installed inside the `btlab`
package. The console script still works, so the failure only appears for
`import btlab.btvalidate`. **Any module that imports a sibling needs a dual
import** (`try: from btengine import ... / except ImportError: from .btengine
import ...`); `btvalidate.py` and `intraday.py` are the ones that do today.
Run this suite after touching any import.

`references/publishing-and-packaging.md` has the full publish path: the
package layout, the `.gitignore` rules that matter, the CI matrix, the
capabilities matrix the README must carry, and the publish-time checks
(execute the README's own example, simulate the installed layout, install
into a throwaway venv) before anything is published.

Single-strategy sanity check:

```bash
python scripts/run_backtest.py --config strat.json --quick
```

Expect a verdict line, a non-`error` walk-forward block, and
`causality audit: PASS`.