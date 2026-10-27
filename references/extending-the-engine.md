# Extending the Engine Without Installing Anything

This engine is pure Python stdlib **on purpose**. The target machine has very
little free disk, so installing numpy + pandas + yfinance (~200 MB) for a
backtest is not an option. Keep it that way.

If a future session is asked to add a feature, a signal, or a validation stage,
extend it with stdlib rather than adding a dependency. Everything needed is
already in `btengine.py` — copy the shape.

---

## Stdlib replacements you will reach for

| Need | Use | Import |
|------|-----|--------|
| mean / stdev | `mean()`, `stdev(xs, ddof=1)` | `statistics` |
| linear-interp quantile | `quantile(sorted_xs, q)` | — |
| rolling window | `rolling(xs, lookback, fn)` | — |
| drawdown | `max_drawdown_pct(rets)` | — |
| correlation | `pearson(xs, ys)` | — |
| safe division | `safe_div(a, b, default)` | — |
| clamp / winsorize | `max()/min()`, `winsorize(xs, 0.01, 0.99)` | — |
| bootstrap / reshuffle | `random.Random(seed)` | `random` |
| JSON cache I/O | `json` + `os.replace` for atomic writes | `json`, `os` |
| HTTP fetch | `urllib.request` with a browser User-Agent | `urllib` |

`rolling` is the workhorse — most features are `rolling(series, n, agg)` plus
one line of arithmetic. Guard the warm-up window explicitly: the first `n` bars
have no value and every consumer must tolerate `None`.

**Null arithmetic is the recurring trap.** `safe_div(a, b) - 1.0` raises when
`safe_div` returns `None` during warm-up. Use a helper that checks both
operands (`_div_sub` in `btengine.py`) rather than chaining.

## Adding a feature

Append to the `FEATURES` registry. The causality contract is not optional:

> the value at index `i` may only use data at indices `<= i`

```python
def f_my_feature(panel, sym, lookback=30, **kw):
    s = _close_series(panel, sym)
    m = rolling(s, lookback, mean)
    sd = rolling(s, lookback, lambda w: stdev(w, ddof=1))
    return [_div_sub(c - mm, sdd) if (c is not None and mm is not None and sdd) else None
            for c, mm, sdd in zip(s, m, sd)]

FEATURES["my_feature"] = f_my_feature
```

Signature is `(panel, sym, **params)`, returns one list aligned to `panel.dates`.
Accept and ignore `direction` — `compute_features` strips it before the call.

## Adding a signal

Append to the `SIGNALS` registry. Return a target weight per bar in `[-1, 1]`
(`None` is treated as flat):

```python
def sig_my_rule(panel, features, feature=None, threshold=0.0, **kw):
    vals = features[feature]
    return {s: [(0.0 if v is None else (1.0 if v > threshold else 0.0))
                for v in vals[s]] for s in panel.symbols}

SIGNALS["my_rule"] = sig_my_rule
```

Cross-sectional signals receive the whole `{feature: {sym: [values]}}` map and
must re-rank **per date**, never against a full-sample distribution.

## Adding a validation stage

Write it in `btvalidate.py` as `fn(bundle, verbose=True) -> dict`, call it from
`full_gauntlet`, append to `flags` when it raises a concern, and surface it in
`render_report`. Keep the return shape uniform: `{"error": "..."}` on failure so
`--quick` can skip it without special-casing.

**Centralise any decision two code paths must make the same way.** The base run
and the walk-forward fold re-simulation both have to decide which feature the
signal trades on, and they drifted: the base run defaulted to the first
declared feature while the fold path demanded an explicit one and crashed.
Resolve it in one helper (`resolve_signal_spec(spec)`) that both call. The same
applies to the rebalance mask, the cost assumptions, and the execution mode —
a validation stage that re-simulates must reproduce the base run's decisions
exactly, or it measures a different strategy than the headline number.

## After any change

```bash
python scripts/selftest.py
```

Re-run it before quoting any verdict. `references/verification-invariants.md`
explains what each check catches. In particular a new feature must pass the
causality audit — a centred window passes every metric and fails only there.

## Performance

Pure stdlib is fine at these sizes (~50 symbols × ~7000 daily bars). Two habits
keep it fast and were load-bearing here:

- **Memoise the panel per process.** The gauntlet rebuilds the same panel for
  every cost-stress and parameter-sweep variant; caching cut a run from 20s to 5s.
- **Precompute feature arrays once** per run rather than recomputing per symbol
  inside the bar loop.

If a future change makes a run minutes long, profile before optimising — the
usual culprit is a panel rebuild, not the arithmetic.