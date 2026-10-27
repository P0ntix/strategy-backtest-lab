# Contributing

Thanks for considering a contribution. This project is small on purpose, so
the rules below exist to keep it small.

## The one rule that is not negotiable

**Pure standard library. No numpy, no pandas, no yfinance, no dependencies of
any kind.**

The engine is deliberately dependency-free so it runs on a machine with very
little free disk (numpy + pandas + yfinance is roughly 200 MB of install;
the whole market-data cache here is ~30 MB). `dependencies = []` in
`pyproject.toml` is not aspirational — it is the product.

If you need a capability that a library would give you, reimplement it with
the standard library:

| Need | Use | Import |
|------|-----|--------|
| mean / stdev | `mean()`, `stdev(xs, ddof=1)` | `statistics` |
| linear-interpolated quantile | `quantile(sorted_xs, q)` | — |
| rolling window | `rolling(xs, lookback, fn)` | — |
| drawdown | `max_drawdown_pct(rets)` | — |
| correlation | `pearson(xs, ys)` | — |
| safe division | `safe_div(a, b, default)` | — |
| winsorize / clamp | `winsorize(xs, 0.01, 0.99)`, `max()`/`min()` | — |
| bootstrap / reshuffle | `random.Random(seed)` | `random` |
| JSON cache I/O | `json` + `os.replace` (atomic write) | `json`, `os` |
| HTTP fetch | `urllib.request` with a browser User-Agent | `urllib` |

`rolling` is the workhorse: most features are `rolling(series, n, agg)` plus
one line of arithmetic. Guard the warm-up window explicitly — the first `n`
bars have no value and every consumer must tolerate `None`.

**Null arithmetic is the recurring trap.** `safe_div(a, b) - 1.0` raises when
`safe_div` returns `None` during warm-up. Use the `_div_sub(a, b)` helper in
`btengine.py`, which checks both operands, rather than chaining.

## Setup

Nothing to install. Confirm the interpreter:

```bash
python -c "import sys; print(sys.version)"
```

Python 3.8+ is required. To work on the package metadata itself:

```bash
python -m pip install -e .
```

That installs the `btlab` console script (`btlab --list-presets`). The
primary usage does not need the install — every script runs directly from a
checkout:

```bash
python scripts/run_backtest.py --config my_strategy.json
```

## Running the test suites

**All four suites run from the `scripts/` directory.** The working directory
matters: `selftest.py` reads the spec templates via the relative path
`../templates/*.json`.

```bash
cd scripts
python selftest.py                   # behavioural controls, causality, invariants
python selftest_intraday.py         # session path (offline, synthetic)
python selftest_corporate_actions.py # dividend adjustment + cache schema (offline)
python selftest_packaging.py         # packaged-layout imports (offline, synthetic)
```

Expected results:

- `selftest.py` → `RESULT: ALL CHECKS PASSED` and
  `negative controls correctly not SUPPORTED: True`. It needs **network
  access on a cold cache** — it backtests real strategies against real
  fetched data. Later runs hit the local cache in `.cache/`.
- `selftest_intraday.py` → `RESULT: ALL CHECKS PASSED`. Offline and
  synthetic; takes under a second.
- `selftest_corporate_actions.py` → `OK` with 8 tests. Offline and synthetic.
- `selftest_packaging.py` → `OK` with 4 tests. Offline and synthetic.

**Re-run the suites after any change to `btengine.py`, `btvalidate.py`,
`intraday.py` or `disclosure.py`, before you quote a verdict.** The
accounting identity and the causality audit are cheap and catch exactly the
failures that look like findings.

## Adding a feature

Append to the `FEATURES` registry in `scripts/btengine.py`. The contract is
not optional:

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

The exact contract a new feature function must satisfy:

- **Signature:** `(panel, sym, **params)` — a pure function of the panel, the
  symbol and the parameters.
- **Return:** a list of the same length as `panel.dates`, aligned index for
  index with the panel's date axis. Use `None` for "no value yet" (warm-up).
- **Causality:** the value at index `i` may only use data at indices `<= i`.
  No centred windows, no full-sample normalisation, no whole-sample
  regression. These do not raise — they quietly print beautiful backtests.
- **Accept and ignore `direction`** — `compute_features` strips it before the
  call, but accept `**kw` so a stray parameter never breaks a run.
- **Do not mutate the panel** and do not use global state.

### New features must pass the causality-by-truncation audit

`selftest.py` TEST 5 recomputes every registered feature on a panel truncated
to half the dates and asserts the first-half values are bit-identical. A
centred window passes every metric and fails only there.

The sweep is automatic over `FEATURES`, using a default parameter set of
`{"lookback": 20}`. **If your feature uses differently-named parameters, add
a case for it in the TEST 5 sweep** so it is exercised with valid parameters
rather than silently defaulting. A feature that the sweep cannot exercise
correctly is not tested, and an untested feature is an unverified feature.

If your feature belongs to the session path, register it in
`scripts/intraday.py`'s `INTRADAY_FEATURES` instead, and add it to
`selftest_intraday.py`.

## Adding a signal

Append to the `SIGNALS` registry in `scripts/btengine.py`. A signal returns a
target weight per bar in `[-1, 1]` (`None` is treated as flat):

```python
def sig_my_rule(panel, features, feature=None, threshold=0.0, **kw):
    vals = features[feature]
    return {s: [(0.0 if v is None else (1.0 if v > threshold else 0.0))
                for v in vals[s]] for s in panel.symbols}

SIGNALS["my_rule"] = sig_my_rule
```

Cross-sectional signals receive the whole `{feature: {sym: [values]}}` map and
must re-rank **per date**, never against a full-sample distribution.

## Adding a universe preset

Add an entry to `UNIVERSE_PRESETS` in `scripts/btengine.py`:

```python
UNIVERSE_PRESETS["my_universe"] = ["AAA", "BBB", "CCC"]
```

- Symbols use **Yahoo Finance chart-API tickers**: `SPY`, `EURUSD=X` for FX,
  `GC=F` for futures, `BTC-USD` for crypto, `^TNX` for yields.
- Prefer **ETFs and indices over individual stocks**. The provider serves
  current listings only, so a stock-level universe silently drops delistings
  and is *not* survivorship-free. Say so wherever a stock list is used.
- If the preset has a sensible sibling universe, add it to
  `ALT_UNIVERSES_BY_PRESET` in `scripts/run_backtest.py` so the gauntlet can
  attempt cross-market replication.
- Update the preset table in `references/spec-schema.md` and the universe
  list in `README.md`.

## Adding a validation stage

Write it in `scripts/btvalidate.py` as `fn(bundle, verbose=True) -> dict`,
call it from `full_gauntlet`, append to `flags` when it raises a concern, and
surface it in `render_report`. Keep the return shape uniform: `{"error": "..."}`
on failure, so `--quick` can skip it without special-casing.

**Centralise any decision two code paths must make the same way.** The base
run and the walk-forward fold re-simulation both decide which feature the
signal trades on, and they drifted once: the base run defaulted to the first
declared feature while the fold path demanded an explicit one and crashed.
That decision now lives in one helper (`resolve_signal_spec(spec)`) that both
call. The same applies to the rebalance mask, the cost assumptions and the
execution mode — a validation stage that re-simulates must reproduce the base
run's decisions exactly, or it measures a different strategy than the headline
number.

## Adding a due-diligence question

`scripts/disclosure.py` holds the A–I sheet. Add the question to the
relevant `SECTIONS` tuple with its `who` (`engine` / `user` / `both`) and its
`why`. If `who` is `engine`, map it to a `CAPABILITIES` entry via
`CAP_BY_QUESTION` — and assert `disclosure.unmapped_engine_questions()` is
still empty. An engine-classified question that renders `[UNKNOWN]` is a
broken mapping, not a question for the user.

## Repository layout

```
.
├── scripts/            the engine (flat layout, stdlib only)
│   ├── btengine.py     stages 1-7: data, universe, features, signal, portfolio, execution, accounting
│   ├── btvalidate.py   stage 8: metrics, the validation gauntlet, the verdict
│   ├── disclosure.py   the A-I due-diligence sheet
│   ├── intake.py       the intake gate
│   ├── intraday.py     session-window mode
│   ├── intraday_probe.py  a session-window data probe
│   ├── run_backtest.py the CLI
│   └── selftest*.py    the four test suites
├── templates/          example spec files (selftest.py reads these)
├── references/         implementation reference documents
├── .cache/             market-data cache — git-ignored, regenerable
└── pyproject.toml      packaging metadata
```

`scripts/` is a flat layout on purpose: the modules import each other by
top-level name (`from btengine import ...`) and each script puts its own
directory on `sys.path`. `pyproject.toml` maps that directory onto the
`btlab` package, so the console script is `btlab.run_backtest:main` — the
entry point must match the *installed* module path, not the filename.

A module that imports a sibling must therefore declare a **dual import**: the
flat name first, with a relative import as the fallback.

```python
try:
    from btengine import Panel, simulate
except ImportError:  # installed as a package (btlab.intraday)
    from .btengine import Panel, simulate
```

`selftest_packaging.py` enforces this: it builds a throwaway copy of
`scripts/` shaped as a `btlab/` package and imports every module from it, so
a missing fallback fails the suite rather than a user's `import
btlab.btvalidate`. Running the scripts directly from a checkout
(`python scripts/run_backtest.py`) is unaffected and remains the primary
usage.

## Style

- Pure stdlib, Python 3.8+ syntax. No type annotations are required; the
  codebase uses them sparingly.
- Comment the *why*, not the *what*. The comments in this codebase exist
  because the failure they describe is silent.
- Keep the causality contract visible in any function that transforms market
  data.

## Before you open a pull request

```bash
cd scripts
python selftest.py
python selftest_intraday.py
python selftest_corporate_actions.py
python selftest_packaging.py
```

All four must pass, and a green run must mean something: the suites include
**negative controls that must NOT pass** and a **causality-by-truncation
audit**. A validator that passes everything validates nothing. If a negative
control comes back SUPPORTED, the engine is broken — fix it before trusting
any verdict.

## License

MIT — see [LICENSE](LICENSE).

**The repository owner should replace the copyright holder in `LICENSE` and
the `authors` field in `pyproject.toml` with their own name before
publishing.** Both currently carry the neutral placeholder
`strategy-backtest-lab contributors`.
