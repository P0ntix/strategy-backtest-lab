# Reading the Verdict

The verdict is a statement about **evidence**, not about money. Nothing here
predicts returns. Read this file before reporting a result to a user.

---

## The four verdicts

### SUPPORTED
Pooled walk-forward OOS Sharpe ≥ 0.5 **and** every robustness stage passed
with no flags.

What it means: the hypothesis survived the gauntlet on historical data. The
effect is not obviously an artifact of cost assumptions, parameter choice,
one lucky decade, or look-ahead.

What it does NOT mean: that it will make money. It means it is *worth
paper trading*. A Sharpe of 0.6 with a 30% drawdown is a research finding, not
a product.

### FRAGILE
Positive OOS but at least one robustness stage failed, or the only evidence is
in-sample.

Read the **flags list** — it names the exact stage that failed. A FRAGILE with
one flag ("parameter stability 40%") is a very different animal from one with
five. Never report FRAGILE without its flags.

### REFUTED
OOS Sharpe < 0.5, or the edge disappears at 2× costs, or look-ahead was
detected.

A REFUTED is a real result. Report what killed it — usually one of:

- **"edge does not survive 2x cost stress"** — check the gross-of-costs row
  first. If gross-of-costs Sharpe is positive, the signal is real but too
  small to trade at this size and frequency, and it becomes viable by
  trading less often or on a cheaper venue. If gross-of-costs is also
  negative, costs are not what killed it and the flag is misleading.
- **OOS Sharpe below bar** — it worked in-sample and not out. That gap is
  precisely the overfitting signature.
- **"LOOK-AHEAD DETECTED"** — the pipeline is broken, not the hypothesis. Fix
  the spec/engine; every number from that run is void.

### INCONCLUSIVE
Too few trades or too short a sample to distinguish signal from noise.

The correct response is more data or a different design — **not** a better
story. Never re-run until you get a different verdict; that is p-hacking.

---

## Gross vs net — why did it fail?

Two failures look identical from the verdict alone and call for opposite
responses. The `gross_vs_net` stage re-runs the strategy with every cost set
to zero, so the write-up can tell them apart:

| Gross-of-costs Sharpe | Net-of-costs Sharpe | What to say |
|-----------------------|---------------------|-------------|
| positive | negative | *A real edge, too small to trade at this size and frequency.* The fix is trading less often, not abandoning the hypothesis. |
| negative | negative | *No expectancy in this sample.* Costs are not the primary problem — saying "costs killed it" is false. |
| positive | positive | The edge survives costs; judge it on the net number. |

**Profit factor, win rate and expectancy are NET of costs.** Every cost
dollar is attributed to the position that caused it and netted out of that
trade's P&L when the position closes, so the trade statistics sit on the same
basis as the Sharpe and the CAGR. The report labels them accordingly
(`sim["trade_pnl_basis"]` states the basis). A profit factor quoted without
its basis is ambiguous: a gross profit factor of 2.0 with costs equal to the
entire edge is not the same finding as a net 2.0. When you quote one, say
which it is.

Never write "the edge disappeared under costs" without reading the gross row.
That sentence asserts a gross edge exists, and it is false whenever gross is
also negative — which sends the user off tuning a dead signal instead of
revisiting the hypothesis.

---

## Which number to quote

Always the **pooled OOS Sharpe** from walk-forward. In-sample Sharpe is
printed first because it comes first in the pipeline, and it is systematically
higher.

The fold table is the most informative artefact. Read it as:

| Pattern | Meaning |
|---------|---------|
| OOS ≈ IS in every fold | Stable relationship; genuinely generalising |
| OOS ≪ IS, decaying over folds | Classic overfit — the edge was fitted to noise |
| OOS > IS | Legitimate; often means the early sample was harder |
| One fold carrying everything | The edge lives in one period and is a regime bet |
| Alternating sign | No stable relationship; the metric is noise |

If `oos_profitable_folds` is low relative to `n_folds`, say so in plain
terms even when the pooled number clears the bar.

---

## Flags — what each one really means

| Flag | Diagnosis | Response |
|------|-----------|----------|
| `edge does not survive 2x cost stress` | Edge smaller than transaction costs | Report as "real but untradeable at this size" |
| `parameter stability < 60%` | Spike, not plateau — curve-fit to one parameter value | Report as fitted, not discovered |
| `Monte Carlo prob(profitable) < 85%` | Reshuffling the same trades often loses money | The historical path was lucky |
| `only N/M profitable years` | Concentrated in a few periods | Report which years carried it |
| `deflated Sharpe negative` | After penalising N trials, nothing survives | You found noise; say so |
| `replicated in only N% of alt markets` | Effect is local, not economic | Name the one market it worked in |
| `LOOK-AHEAD DETECTED` | Engine/spec bug | **Stop. Fix before reporting anything.** |
| `only N trades` | Statistical power too low | INCONCLUSIVE territory |

---

## Deflated Sharpe and `--n-variants`

This is the flag users most often skip, and it is the difference between an
honest test and self-deception.

If you tried 50 parameter combinations and are reporting the best one, the
observed Sharpe is upward-biased — you selected a maximum from noise. The
deflated Sharpe subtracts the expected maximum of N trials under the null.

**Always ask the user how many variants they tried.** If they genuinely did not
tune anything, `--n-variants 1` is correct. If they tuned even a little, use
their real number. A verdict that is SUPPORTED at N=1 and REFUTED at N=50 is
the most useful single result this lab can produce — and hiding it is the whole
failure mode.

---

## Multi-asset interpretation

When a hypothesis is tested across asset classes, the *pattern* matters more
than any single verdict:

- **Works in 4+ unrelated classes** → likely a genuine behavioural or
  structural effect. Strongest possible evidence.
- **Works only in the class it was designed on** → probably a class-specific
  microstructure artifact (bid-ask bounce, stale pricing, fee structure).
- **Works in equities and FX but not bonds** → a risk-premium effect; plausible.
- **Works only in one instrument** → likely overfit to that instrument's
  history.

Cross-class replication is implemented automatically for related presets
(`ALT_UNIVERSES_BY_PRESET` in run_backtest.py). Note that the replication step
shares the same feature/signal code, so it tests the *effect's breadth*, not
the code's correctness.

---

## Capacity

`est_capacity_usd` is the capital at which the most illiquid traded symbol would
exceed 5% of its ADV. It is a liquidity ceiling, not a P&L projection:

- It assumes costs scale linearly. Real capacity is **lower** once market
  impact is modelled.
- An edge with $200k capacity is not a business, however good the Sharpe.
- Capacity is invariant to the starting capital in the spec (it is a ratio) —
  if it isn't, the engine has a bug.

## Reading the Due-Diligence Disclosure

Every report ends with a **Due-Diligence Disclosure** sheet: 109 questions
across nine sections (A strategy scope, B data integrity, C per-asset-class
modelling, D look-ahead, E costs and capacity, F portfolio construction,
G robustness, H metrics, I forward testing), each with a provenance marker.

### The provenance markers

| marker | meaning | how to treat it |
|--------|---------|-----------------|
| `[user]` | the user supplied this answer | the strongest evidence on the sheet — it is the user's own statement |
| `[engine]` | the engine answers it with a verified fact about its own behaviour | trustworthy about the engine; it says nothing about the user's hypothesis |
| `[UNKNOWN]` | neither the user nor the engine answered it | **a finding. Report it — never omit it.** |

The sheet lands in the report under "Due-Diligence Coverage" with a
coverage line:

```
- Questions answered: **N/109** (P%)
- Answered by the engine (verified): N
- Answered by the user: N
- Still UNKNOWN: **N**
```

**The user is never asked something the engine already knows.** Most of the
sheet carries an engine answer — a static, verified capability fact about the
engine's own behaviour (data source, timestamp convention, survivorship
treatment, execution model, what the gauntlet covers), plus two more
(`B.observations`, `E.adv_participation`) filled in from the run's own metrics
and capacity estimate. Each question is separately classified by *who* can
answer it: `engine`, `user`, or `both`.

Read the live numbers from the coverage line rather than from prose — the
engine-answerable set grows whenever a capability mapping is added, so a
count written in a document goes stale.

The remaining `[UNKNOWN]`s are mostly questions only the user can answer, and
`--disclose` prints the user-answerable subset under "STILL NEEDED FROM THE
USER" — ask only that list.

One caveat on the sheet itself: an engine-classified question can still render
`[UNKNOWN]` if its capability mapping points at an entry absent from the
registry. That is an engine defect, not a question for the user, and it is
checkable: `disclosure.unmapped_engine_questions()` must return an empty list.
An engine-classified question showing up on the "STILL NEEDED FROM THE USER"
list is a symptom of a broken mapping.

### UNKNOWN is a finding, not a gap in the paperwork

An `[UNKNOWN]` means a load-bearing condition of the test was never
established. It is printed rather than omitted precisely because hiding it
would be the failure mode the sheet exists to prevent. A report with many
UNKNOWNs is not a broken report — it is an accurate one, and the UNKNOWNs
have to be carried into the answer.

Two specific cases worth naming in a write-up:

- **A load-bearing UNKNOWN.** If an UNKNOWN sits on a question the result
  actually depends on — `A.rejection_criteria` (what would have rejected
  the strategy), `A.benchmark` (what it is measured against),
  `A.rationale` (why the effect should exist), `A.currency` (base currency
  and hedging policy), `G.variants_tested` (how many variants were tried) —
  then the verdict must be **downgraded** in the write-up. A SUPPORTED
  verdict resting on an UNKNOWN criterion is not SUPPORTED; it is
  unproven. Report it as FRAGILE, name the UNKNOWN, and say exactly what
  the verdict would need to become SUPPORTED.
- **A benign UNKNOWN.** Some UNKNOWNs are genuinely outside the engine's
  reach (the whole of section I, forward testing). Saying so is honest;
  asking the user to answer them is not useful. Distinguish the two
  rather than treating every UNKNOWN as fatal.

The rule of thumb: **an UNKNOWN in a load-bearing question downgrades the
verdict; an UNKNOWN outside the engine's reach is a stated limit.**

### Capabilities the engine does not have — always state them

The sheet records these as verified engine facts, and they belong in every
multi-asset answer whether or not the run touched them:

- **No FX conversion or hedging.** Returns stay in each instrument's quote
  currency; a multi-currency book is not converted to a base currency.
- **No point-in-time fundamentals or index membership.** Universe membership
  is static per spec.
- **No covariance matrix.** Cross-asset correlation is therefore **never
  audited** — a strategy resting on diversification may be resting on a
  correlation regime estimated with hindsight.
- **No options.** No pricing, Greeks, IV surface, expiry, assignment or margin.
- **No bond analytics.** No clean/dirty price, accrued interest, coupon
  cash flows, duration, convexity or recovery.
- **No margin or financing costs** on gross exposure.
- **No futures roll cost.** Yahoo's continuous series has an undocumented,
  non-point-in-time roll methodology.
- **Not survivorship-free for individual equities.** Yahoo serves current
  listings only. ETFs, futures, FX, indices and crypto majors are free.

### Validations that are not implemented — never imply coverage

These four are **not implemented**. Say so rather than implying the engine
covered them:

| not implemented | what exists instead |
|-----------------|---------------------|
| leave-one-asset-out | cross-market replication over alternative universes — a proxy for a different question |
| randomized rebalancing / randomized asset inclusion | nothing |
| top-trade removal (top 1–5 trades) | nothing; the per-year breakdown and replication are the closest available proxy |
| confidence intervals on expectancy, profit factor, Sharpe, drawdown | Monte Carlo percentiles over the resampled trade sequence only |

A strategy whose result depends on any of these has not been tested on that
dimension, and the write-up must say so.

---

## Standing caveats to state out loud

Whenever you report a verdict, these belong in the answer:

1. **This is retrospective.** It says nothing about the future.
2. **Survivorship bias remains on individual stocks.** The engine uses ETFs and
   proxies precisely because Yahoo only returns current listings.
3. **Costs are modelled, not measured.** They are conservative per class but
   they are not the user's broker.
4. **No partial fills, latency, halts, or market impact** are simulated.
5. **SUPPORTED means "paper trade this", not "trade this".**
6. **The backtest's own failure modes are only partly checkable.** The causality
   audit catches look-ahead in the feature code. It cannot catch a bias baked
   into the hypothesis itself, survivorship in the instrument list, or the fact
   that you picked this strategy because it looked good once.
7. **The engine's missing capabilities travel with the verdict.** No FX
   conversion or hedging, no point-in-time fundamentals or index membership, no
   covariance matrix (so correlation is never audited), no options, no bond
   analytics, no margin/financing costs, no futures roll cost, and not
   survivorship-free for individual equities. Leave-one-asset-out, randomized
   rebalancing, top-trade removal and confidence intervals are **not
   implemented** — say so rather than implying coverage. The full list is in
   the Due-Diligence Disclosure section above.
8. **Every `[UNKNOWN]` on the disclosure sheet is part of the result.** An
   unanswered load-bearing question downgrades the verdict; an unanswered
   question outside the engine's reach is a stated limit. Neither may be
   omitted from the write-up.
9. **A binding `max_position` cap means realised gross is below the
   requested `gross_leverage`.** Report the realised gross, not the target —
   e.g. 4 instruments, a quartile split and a 10% cap give at most 0.2x gross,
   not 1.0x.
10. **In session mode the panel is daily.** One bar per session, so returns
    annualise at 252. The sample is also short — roughly two years of 60-minute
    history is all the provider allows — so statistical power, not signal
    quality, is usually the binding constraint.
11. **Always state whether gross-of-costs was positive.** “The edge died in
    costs” and “the signal had no expectancy” are different findings with
    different responses, and the verdict cannot tell them apart. Quote the
    gross-of-costs Sharpe next to the net one whenever a cost flag fires.