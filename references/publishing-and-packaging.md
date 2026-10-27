# Publishing and Packaging

How to ship this engine as a public, installable repo without shipping a
broken install or a README that lies.

---

## The one structural constraint

`scripts/` is a **flat layout**: modules import each other by top-level name
(`from btengine import ...`), and each entry-point script puts its own
directory on `sys.path`. That makes `python scripts/run_backtest.py` work from
a checkout with no installation.

It breaks the moment the project is **installed**. The same modules then live
inside the `btlab` package, where `btengine` is not a top-level module and a
flat import raises `ModuleNotFoundError`.

Every module that imports a sibling therefore needs a **dual import**:

```python
try:
    from btengine import (...)          # flat: script / checkout
except ImportError:
    from .btengine import (...)          # relative: installed package
```

Modules that import a sibling: `btvalidate.py` (→ `btengine`),
`intraday.py` (→ `btengine`). `btengine.run_spec` already does this for its
lazy `intraday` import.

**Why it is easy to miss:** the console script still works, because
`run_backtest.py` inserts its own directory into `sys.path` before importing
its siblings. The failure only appears for `import btlab.btvalidate` — the
exact call a user extending the library makes. Nothing else catches it.

`scripts/selftest_packaging.py` builds a throwaway `btlab/` package layout,
imports every module from it, and asserts the entry point is callable. Run it
after touching any import.

## `pyproject.toml`

Expose the flat directory as one importable package:

```toml
[project]
name = "strategy-backtest-lab"
version = "1.0.0"
dependencies = []          # ZERO runtime dependencies -- do not add numpy
requires-python = ">=3.8"

[project.scripts]
btlab = "btlab.run_backtest:main"

[tool.setuptools]
packages = ["btlab"]

[tool.setuptools.package-dir]
btlab = "scripts"
```

The entry-point path is **dotted and package-relative** (`btlab.run_backtest`,
not `run_backtest`) — the on-disk filename and the installed module path
differ.

Keep `dependencies = []`. The whole point of the engine is that it installs on
a constrained machine without pulling a scientific stack.

## `.gitignore` — the rules that actually matter

| Pattern | Why |
|---------|-----|
| `.cache/` | Fetched market data, tens of MB, fully regenerable. **This is the one that bites** — a repo that commits it is unusable. |
| `__pycache__/`, `*.py[cod]` | Bytecode |
| `*.egg-info/`, `build/`, `dist/` | Packaging artefacts from `pip install -e .` |
| `*_report.md` | Generated per-run evidence, not source |
| `.venv/`, `.pytest_cache/`, editor cruft | Standard |

Verify with `git check-ignore -v .cache scripts/__pycache__` and
`git add -An` — the second shows exactly what would be committed. A repo that
accidentally tracks a 31 MB cache is a bad first impression.

## CI matrix

Run on `ubuntu-latest`, `windows-latest`, `macos-latest` across the oldest
supported and a current Python.

**Windows is not optional here.** The session-window extraction detects trading
days by grouping on the UTC calendar date, because the provider anchors
intraday bars to exchange-local time. A naive implementation that filters on a
fixed UTC hour passes on a UTC runner and is wrong on a machine in a
non-UTC timezone. The matrix is what catches that class of bug.

Run **every** suite from the `scripts/` directory (`selftest.py` reads
`../templates/*.json` by relative path):

```bash
python selftest.py                    # needs network on a cold cache
python selftest_intraday.py           # offline
python selftest_corporate_actions.py  # offline
python selftest_packaging.py          # offline
```

`selftest.py` backtests real strategies, so it fetches market data. Either
allow network in CI or state the dependency explicitly in the workflow — do
not let it fail mysteriously.

## The capabilities matrix is the credibility section

A backtester that overstates its coverage is worse than no backtester. The
README carries two tables and both must stay true:

**Does support** — one row per capability, each stating the mechanism, not
just the name. "Daily bars are split- AND dividend-adjusted" beats "adjusted
prices": the reader can check the mechanism.

**Does not support** — state every gap. For this engine that list includes:
no FX conversion or hedging; no FX swap/rollover/carry; no point-in-time
fundamentals, index membership, credit ratings or sector classifications; no
covariance matrix (so correlation is never audited); no options; no bond
analytics; no alternatives/real-asset valuation; no margin, financing or
collateral-haircut costs; no futures roll cost; not survivorship-free for
individual equities; and the unimplemented validations — leave-one-asset-out,
randomised asset inclusion, randomised rebalancing dates, top-1–5 trade
removal, confidence intervals for expectancy/profit-factor/Sharpe/drawdown.
Plus the execution-realism gaps (no limit orders, stop fills, queue position,
latency, halts or market impact; costs do not widen with volatility) and the
per-asset-class caveat that intraday bars are not dividend-adjusted.

When a capability is added, move it out of the "not supported" table in the
same change — a stale limitations table is the most common way a research tool
starts lying.

## Publish-time verification

Do not publish on the strength of "the files exist". Execute the claims.

**1. Execute the README's own examples.** Extract the JSON block from the
worked example programmatically and run it — do not retype it, because
retyping re-introduces your own bugs and hides drift between the docs and
the code:

```python
sec = readme.split("## Worked example")[1].split("```json")[1].split("```")[0]
json.loads(sec)                      # must parse
write(sec, "my_strategy.json")
```

Then run the documented command and confirm the report contains every section
the README promises. Also verify the README's refusal claim by deleting one
`intake.confirmed` key and confirming exit code 2.

**2. Simulate the installed layout.** Build a throwaway package dir and import
every module from it — this is what catches the dual-import class of bug
before a user does:

```python
tmp = mkdtemp(); pkg = join(tmp, "btlab"); makedirs(pkg)
copy("scripts/"*.py, pkg)
# then, in a subprocess: import btlab.<module>
```

**3. Install for real, in a venv.** `pip install -e .` into a throwaway venv
(not the global interpreter), then run the console script from an unrelated
working directory, import `btlab.btvalidate`, and run one real backtest
through the installed entry point. A repo whose install is broken but whose
checkout works is a bug report waiting to happen.

**4. Check the repo is clean.** `git add -An` and read the file list. Confirm
no cache, no bytecode, no `egg-info`, no personal paths, no absolute
machine-specific paths in the docs.

**5. Re-read the limitations table against the code.** Every "not supported"
claim must still be true. Every "supported" claim must still be exercised by
a test.

## Documentation hygiene

- **No hardcoded counts** that can go stale — question counts, coverage
  percentages, per-asset-class cost tables. Point at the command that prints
  the live number (`--disclose`), and quote the mechanism instead.
- **LICENSE holder is a placeholder** until the owner replaces it. Say so in
  CONTRIBUTING.md so it is not missed.
- **`OWNER/OWNER` in `[project.urls]`** is a placeholder too — a broken URL in
  a published package is a visible loose end.
- If the repo ships a `SKILL.md`, keep it: it documents the agent-facing
  workflow, which is genuinely useful to a human reader and is not
  duplicated by the README.
