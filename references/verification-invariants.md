# Verification Invariants for Simulation Harnesses

Rules that hold for **any** backtester, simulator, or P&L engine — not just
this one. Each invariant is a cheap assertion that catches a whole class of
silent corruption. Assert them in a selftest and run that selftest after every
change to the engine.

The governing idea: **a metric that disagrees with another metric computed from
the same run is a bug in the engine, not a finding about the strategy.** The
trade ledger cannot disagree with the equity curve. Costs cannot be $1 after
four thousand trades. A capacity estimate cannot depend on the starting capital.

---

## 1. Accounting identity (the strongest check)

```
final_equity == initial_capital + sum(trade_pnl) - total_costs
```

Every dollar of profit must be attributable to a recorded trade, and every
dollar of cost must actually be deducted. The trade ledger is **net of
costs**: every cost dollar is attributed to the position that caused it and
netted out of that trade's P&L when the position closes. Assert:

```python
gap = (init + sum(t["pnl"] for t in sim["trades"])) - sim["final_equity"]
assert abs(gap) < init * 1e-3
```

**Why it matters:** the trade ledger feeds profit factor, win rate, expectancy
and any trade-resampling bootstrap. When those drift from the equity curve, the
strategy-level statistics are fiction while the Sharpe still looks fine — the
two are computed by different code paths and will not notice each other.

**The ledger must be on the same basis as the equity curve.** When costs were
debited from cash only and the trade ledger held the gross price move, the
reconciliation gap was exactly the cost bill — the two statements "balanced",
and the report printed a **gross** profit factor and win rate next to a
**net** Sharpe with no basis label. The identity to assert is now `gap == 0`,
not `gap == costs`. `sim["trade_pnl_basis"]` states which basis the ledger is
on ("net"), and the report labels its profit-factor and expectancy rows from
that key.

Run it on a **winning and a losing** strategy. A losing strategy is the
sharper test: it forces sign errors to surface.

## 2. Capital-invariance for scale-dependent quantities

Liquidity-based capacity is a *ratio*. It must be **identical** at every
starting capital:

```python
for capital in (10_000, 100_000, 1_000_000, 10_000_000):
    ... assert capacity == capacity_at_previous   # while turnover scales linearly
```

**Why it matters:** a capacity figure is solved as `C = k · ADV / turnover`. Drop
the `· capital` term and you return turnover, not capacity — an error that
lands within two orders of magnitude of a plausible number and so survives a
sanity glance. Scaling capital is the cheapest way to expose it.

Rule of thumb: *if a quantity should not depend on the size of the portfolio,
run it at two sizes and diff.*

## 3. Causality by truncation

To prove a feature never reads the future:

1. Compute features on the full panel.
2. Recompute them on the panel truncated to the first half of dates.
3. Assert the first-half values are **bit-identical**.

**Why it matters:** centred rolling windows, full-sample normalisation, and
whole-sample regression are the three usual look-ahead sources, and none of
them raise. They just quietly print beautiful backtests. Run this over **every**
built-in feature, not just the one under test — one centred helper contaminates
every strategy that touches it.

## 4. Negative controls before trusting anything

A validator that passes everything validates nothing. Before reporting any
result, confirm the harness **rejects** at least:

- a deliberately hopeless strategy (high turnover, fee-dominated, known not to
  work) — must come back REFUTED/NEGATIVE
- a known-good effect — must come back POSITIVE, or you have an over-strict
  harness that refuses real findings

**Why it matters:** the asymmetry is invisible from inside a single run. A
harness that always says "profitable" produces confident, worthless output. Only
a case you *expect* to fail can tell you the harness works.

## 5. Sanity-bounds on the numbers themselves

Cheap post-hoc assertions on headline stats:

| Quantity | Plausible bound | Violation means |
|----------|-----------------|-----------------|
| Trades | rebalances × positions × ~2 | counting partial adjustments as trades |
| Turnover/capital/year | 2–50× for a rebalancing strategy | a period predicate fires far too often |
| Costs paid | non-trivial once trades > 100 | an accumulator is scoped inside the wrong loop |
| Sharpe | usually < 3 | unit error or annualisation applied twice |

Turnover per year is the single most informative one: a "monthly" strategy
running at hundreds of times capital per year means the rebalance predicate is
matching a *window* (e.g. every bar in the first week of the month) instead of
exactly one bar per period.

## 6. Units contract: a price ratio is not a return

A series container named `rets` must hold **returns** (`close/prev_close − 1`),
not price ratios (`close/prev_close`). The failure is silent and **uneven**,
which is what makes it dangerous:

| Consumer | Sensitive to a constant offset? | What breaks |
|----------|-------------------------------|-------------|
| `stdev`, `skew`, correlation | **No** — location-invariant | nothing; the numbers stay correct |
| `rsi` | **Yes** | every ratio > 1 counts as a gain, so RSI pins at 100 forever |
| threshold / sign logic | **Yes** | a positive ratio reads as a positive *and* large move |
| magnitude sanity checks | **Yes** | a ratio of 1.009 reads as a "+101% move" |

The location-invariant consumers give false confidence that the pipeline is
fine, while every sign-dependent feature is inverted. Assert two things:

```python
assert max(abs(r) for r in rets) < 0.35        # a plausible per-bar move
span = max(rsi_vals) - min(rsi_vals)
assert span >= 1.0                              # an indicator that actually moves
```

**Why it matters:** a degenerate RSI is the cheapest detector of the wrong
units in the series, because no real price path can produce it.

## 7. Constraint precedence: caps bind last

Apply a concentration cap **after every other sizing step**, never before: not
before the gross normalisation, not before the volatility target, not before
the net-target shift. A cap applied first is silently undone by the rescale
that follows it, and the portfolio ends up exactly as concentrated as it
always was.

```python
assert max(abs(w) for w in all_target_weights) <= cap + 1e-9
assert max(gross_of_each_bar) <= target_gross + 1e-9
```

**Why it matters:** the two constraints contradict each other when the universe
is small — *N* instruments, a per-name cap of *c*, and a target gross of *G*
are simultaneously satisfiable only if *N × c ≥ G*. When the cap binds,
realised gross is necessarily **below** the target. That is not a bug to paper
over; it is the honest reading of the user's stated constraints and belongs in
the report. Sharpe is leverage-invariant, so it does not change the verdict —
but CAGR and drawdown scale with it.

**A target expressed as a net exposure must be REACHABLE.** If the target
mechanism is a uniform scale, it cannot reach a target of the opposite sign
without inverting the strategy, cannot neutralise a one-sided book, and is a
no-op on an already-flat book. An offset (the same constant added to every
weight) handles all three. Assert the target is actually hit:

```python
for target_net in (0.0, 0.2, -0.5):
    w = construct(..., net_target=target_net)
    assert abs(sum(w.values()) - target_net) < 1e-9
```

## 8. Force-close open positions at the end of the sample

Any position still open on the last bar holds unrealised P&L that appears in
the equity curve but in no trade record — silently inflating profit factor and
expectancy.

Close them out into the ledger at the final price, and reconcile against
Invariant 1.

## 9. Round-trip ledger: one trade = one position lifecycle

A "trade" must be a **complete position lifecycle** — opened, adjusted, closed —
not each individual rebalance adjustment.

- Track a weighted-average cost basis per position.
- Accumulate realised P&L while the position is open.
- Emit one ledger record when the position returns to flat.
- Force-close anything still open on the last bar (§8).

**Why it matters:** counting each partial adjustment as a trade inflates the
trade count by orders of magnitude and, worse, measures every slice against the
**original** entry price. The trade statistics then invert the equity curve: a
strategy can bleed 28% a year with a 99% drawdown while its ledger reports
12,000 trades, a 74% win rate, and a Monte Carlo that never loses money.

The accounting identity (§1) still passes, because the per-trade P&L sums
correctly — the corruption is in the *unit of observation*, not the arithmetic.
This is why §1 alone is not sufficient.

The checkable assertion exploits the fact that a symbol holds at most one
position at a time, so its lifecycles cannot interleave:

```python
iv = sorted((t["entry_bar"], t["bar"]) for t in trades if t["symbol"] == sym)
assert all(iv[i][0] > iv[i-1][1] for i in range(1, len(iv)))   # intervals disjoint
```

Overlapping intervals mean the ledger is counting slices of one position as
separate trades.

## 10. Walk-forward partition integrity

Pooled out-of-sample results are only valid if the test blocks form a
**partition** — no date may appear in two folds.

```python
assert len(set(all_test_dates)) == len(all_test_dates)
assert sum(len(f["test_dates"]) for f in folds) == len(set(all_test_dates))
```

**Why it matters:** an overlapping final fold re-uses dates already counted in
an earlier fold, so the pooled OOS series double-counts its strongest period
and the headline out-of-sample Sharpe comes out **higher than any individual
fold produced**. It is invisible in the per-fold table unless you read the
date ranges against each other.

Derive the block layout instead of hand-computing it: reserve an initial
training block, then give each fold its own contiguous test block, so the test
windows are disjoint by construction.

## 11. Monte Carlo must rebuild the curve, not shortcut a per-trade Sharpe

Resample the trade **sequence**, replay it through an equity curve, and measure
Sharpe and drawdown from that curve. A per-trade Sharpe proxy multiplied by a
`sqrt(term)` factor is a different quantity and can land two orders of
magnitude off.

```python
assert 0.3 < median(resampled_sharpe) < 3.0 * observed_sharpe
```

**Why it matters:** a shortcut proxy reported a median Sharpe of 89.7 — not a
number anyone would mistake for a finding, but it was inside the report and
would have been read as "the edge is overwhelmingly robust". Assert the
resampled distribution lands in the same range as the run's own Sharpe.

## 12. Gross-of-costs decomposition

Run the strategy a second time with every cost set to zero and compare:

```python
gross = run_with(cost_multiplier=0.0)
assert gross["sharpe"] and net["sharpe"]
```

| Gross-of-costs | Net-of-costs | Reading |
|-----------------|---------------|---------|
| positive | negative | A real edge, too small to trade. Report it as *untradeable at this size/frequency* — the fix is trading less often, not abandoning the hypothesis. |
| negative | negative | No expectancy in the sample. Costs are not the primary problem and "costs killed it" is a false explanation. |
| positive | positive | Edge survives; judge on the net number. |

**Why it matters:** the two failure modes look identical from the verdict alone
and call for opposite responses — one is a trading-frequency problem worth
solving, the other kills the hypothesis. A verdict that says "the edge
disappeared under costs" asserts a gross edge exists; when gross is also
negative that sentence is false and sends the user off tuning a dead signal.
Make the decomposition a standard stage, not an afterthought.

## 13. Constraint inertness: a sweep that cannot move proves nothing

When a per-name cap binds on every bar, all positions take the same weight, so
sizing parameters cannot affect the result. A parameter sweep over such a
strategy reports perfect stability — which reads as a robustness plateau but is
the parameter being unable to move anything.

```python
w = [x for bar in target_weights for x in bar if x]
assert len(set(round(x, 6) for x in w)) > 1     # more than one realised weight
# and for each swept parameter:
assert abs(sharpe_perturbed - sharpe_base) > 0  # it can actually move something
```

**Why it matters:** "stable across ±25% of every parameter" is one of the
strongest-looking robustness results available, and it is free when the
constraint binds. A sweep that reports the identical Sharpe for every
perturbation is telling you the sweep was inert, not that the strategy is
robust. Assert that a perturbation changes something; skip degenerate
perturbations (a lookback of 1 perturbed by −25% is still 1) rather than
recording them as evidence.

## 14. Build the selftest before you trust the engine

Order matters: **write the invariants while building the engine, not after a
result looks good.** Every invariant here exists because the engine produced a
confident wrong number first. Retrofitting assertions onto an engine you
already believe produces assertions you unconsciously write to pass.

---

## 13. The session path needs its own suite

The invariants above are written for a DAILY panel. Session mode (60-minute
bars collapsed to one opening-window bar per session, executed flat-at-close)
breaks differently, and the daily checks do not reach it:

- A DST bug changes what a "day" means. Yahoo anchors intraday bars to
  exchange-local time, so the 09:30 ET bar is 13:30 UTC in summer (EDT) and
  14:30 UTC in winter (EST). Group by UTC calendar date and take the first
  bar of each group; assert the set of first-bar UTC hours is a subset of
  `{13, 14}` and that the two regimes actually appear in the sample.
- The daily path's collapser keeps the LAST bar of a date. If the session
  path reuses it, the "opening window" silently becomes the closing window.
  Assert the two collapser functions return different bars for intraday data.
- `flat_at_close` must mean no overnight exposure. On a synthetic price path
  with a deliberate overnight gap, each bar's return must equal its own
  open→close move, and gross exposure must be zero at every close. A path that
  leaks the gap returns close-to-close instead.
- The accounting identity (§1) must hold in session mode too, on a winning
  AND a losing run. A losing run is the sharper test: it forces sign errors
  to surface.
- One trade per session per instrument. With `flat_at_close` each session
  produces exactly one completed round trip; the old partial-adjustment
  ledger produced one per rebalance slice. Prove the check discriminates by
  also running a held-position variant with swinging weights and confirming
  that many round trips of turnover still yield only one ledger record per
  instrument.

Run `scripts/selftest_intraday.py` after changing anything in
`scripts/intraday.py` or in the session branch of `scripts/btengine.py`.

---

## §13 — Corporate actions and cache schema

Yahoo's `indicators.quote` closes are split-adjusted but NOT
dividend-adjusted. Reading only `quote` makes every ex-dividend date look
like a real loss — worth roughly 0.3–0.7% per ex-date on SPY, and enough to
invert a marginal long-only result. The engine therefore reads
`indicators.adjclose` and back-adjusts OHLC by the per-bar factor
`adjclose/close`.

The invariant is **continuity across the ex-date**: the return between two
adjacent bars straddling a dividend must not contain the dividend. The
adjusted series reproduces Yahoo's `adjclose` exactly, so the check is that
the engine's close equals `adjclose` (up to the factor) and that the
adjusted return across the ex-date is materially smaller than the raw one.

Supporting checks:

- **Volume is not scaled** by the adjustment factor.
- **A missing `adjclose`** (intraday intervals publish none) leaves the
  prices untouched rather than corrupting them.
- **A malformed factor** (0, or absurd) is rejected rather than applied —
  a silent scale of the whole series is worse than no adjustment.
- **The cache carries a schema marker.** A daily cache written before the
  adjustment existed holds unadjusted prices and must be refetched, not
  served. Without the marker, the fix is defeated by a stale cache and the
  bias returns invisibly.

Run `scripts/selftest_corporate_actions.py` after changing anything in the
fetch or cache path. It is offline: the Yahoo payload is synthesised and
`urlopen` is monkeypatched.

---

## §14 — Execution timing: orders, fills and the bar they happen on

Three separate rules govern *when* an order exists and *when* it fills. They
are easy to conflate and each one has a distinct failure mode.

### The rebalance mask gates ORDER GENERATION, not the fill

An order generated on a rebalance bar fills on the **next** bar, whether or
not that next bar is itself a rebalance bar.

```python
# A decision taken on bar 0 must fill on bar 1, not on the next rebalance bar.
mask = rebalance_mask(panel, "monthly")
sim = simulate(panel, weights, trade_mask=mask, fill_at="open")
first = next(i for i, g in enumerate(sim["gross_exposure"]) if g > 1e-9)
assert first == 1
```

**Why it matters:** if the mask gates the *fill* instead, the order decided at
bar 0 finds the next bar unmarked, so it sits pending until the following
rebalance bar — a full month later, for a monthly strategy. Every rebalance
executes one period late and the final rebalance of the sample never executes
at all. The bug is invisible in the output: the strategy still trades, the
equity curve still looks plausible, and only the *timing* is wrong.

### The liquidity cap must use LAGGED volume

An order filled at bar `i`'s open cannot know bar `i`'s volume.

```python
# A fill at bar 1 must be sized by bar 0's volume, not bar 1's.
```

**Why it matters:** a thin bar artificially tightens the cap and a fat bar
loosens it, so the cap becomes a function of the very quantity it is meant to
constrain. Worse, when that volume is zero the cap silently *disappears* and
an order of any size passes. Use the trailing dollar volume strictly before
the fill bar, and refuse the fill when no trailing volume exists — a trade
you cannot size against volume is not a trade. Count the refusals.

### No fill on a stale bar

When a symbol prints no observation on a date, the panel forward-fills the
price. That price may be used to **mark** a position (it is how you value
something you cannot trade) but it must never **fill** an order.

```python
# With bars 1-4 missing, the position must not open before bar 5.
assert first_position_bar == 5
assert sim["stale_skips"] >= 4
```

**Why it matters:** without this, a halted or delisted name trades
indefinitely at a phantom price, and the strategy appears to hold a position
in an instrument that stopped existing.

### A test that cannot fail is not a test

A guard written as `all(r["verdict"] != "SUPPORTED" for r in [])` is
vacuously `True` — an empty list satisfies it, and it will never catch
anything. This happens when the filter string does not match the labels it
searches (an uppercase `"NEGATIVE"` against a label reading "negative
control"). Assert that the guard found something to check:

```python
neg = [r for r in results if "negative" in r["label"].lower()]
assert neg, "no negative control was checked — the guard is vacuous"
```

---

## Checklist for adding an invariant

- Is it checkable from outputs alone, with no reference to intent?
- Does it hold for a losing strategy as well as a winning one?
- Would it have caught the last bug you fixed?
- Does it run fast enough to include in every regression pass?
- **Can it fail?** A guard over an empty collection is vacuously true. Assert
  that the guard matched something.

If any answer is no, it is a nice-to-have. The four that always earn their
place: **accounting identity, capital-invariance, causality-by-truncation,
negative control.** Add the units contract (§6) whenever the series type
changes, constraint precedence (§7) whenever the sizing layer changes, the
round-trip ledger (§9) whenever positions are adjusted while open, the
partition check (§10) whenever folds are pooled into one OOS series, the
gross-of-costs decomposition (§12) whenever the verdict can turn on costs, the
corporate-actions continuity check (§13) whenever the fetch or cache path
changes, and the execution-timing checks (§14) whenever the order/fill path
changes.

**A metric that disagrees with another metric from the same run is an engine
bug, not a strategy finding.** When two numbers conflict, assume the engine is
wrong first — the strategy cannot make costs negative or Sharpe exceed the
square root of the sample size.