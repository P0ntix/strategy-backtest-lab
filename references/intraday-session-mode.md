# Intraday / Session-Window Mode

Backtesting a strategy that trades one specific intraday window (e.g. the
09:30–10:00 ET opening window) while keeping the whole daily pipeline intact.

---

## Provider quirks (Yahoo chart API)

| Quirk | Consequence |
|-------|-------------|
| A full-range request (`period1=0&period2=9999999999`) is **rejected (HTTP 422)** for intraday intervals | Intraday must use an explicit `range`; daily may use the full range |
| 60-minute history is capped at roughly **2 years** by the provider | ~500 sessions. Statistical power is the binding constraint, not signal quality |
| Finer intervals (30m, 15m, 5m) are **refused (422)** | 60m is the finest available here |
| Intraday bars are **RTH only** — 7 bars/day (09:30…15:30 ET) | No pre/post-market; an overnight session strategy cannot be tested |
| Bars are anchored to **exchange-local time**, so the 09:30 ET bar is 13:30 UTC in summer and 14:30 UTC in winter | A fixed-UTC session filter breaks at every DST transition |
| Some bars carry null OHLCV | Skip nulls; do not treat them as zero |
| The options chain endpoint requires auth (401) and exposes **current** greeks only | No historical gamma-exposure series is obtainable for free — decompose the hypothesis instead of proxying it silently |

## DST-safe session extraction

Group intraday records by **UTC calendar date** and take the first bar of each
group. This works without any timezone database:

- The first bar of a session (09:30 ET) and the last bar (15:30 ET) always fall
  on the **same UTC calendar date** as the exchange-local session.
- So "group by UTC date, take the first bar" recovers the opening window in
  both EDT and EST, with no `zoneinfo` and no dependency on the Windows tz
  database.

```python
by_day = {}
for ts, o, h, l, c, v in records:
    day = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
    by_day.setdefault(day, []).append((ts, o, h, l, c, v))
session_bar = sorted(by_day[day])[0]     # the 09:30-10:00 ET window
```

Verify the extraction by checking that the set of UTC hours appearing as a
day's first bar is a subset of `{13, 14}` (13 = EDT, 14 = EST). If a third
hour appears, the grouping is wrong. `scripts/intraday_probe.py` asserts this.

## Spec fields

```jsonc
"period":    { "interval": "60m", "start": null, "end": null },
"execution": {
  "mode": "session",        // switches the data layer to session panels
  "window": "first",        // "first" ONLY - see the enforced constraints below
  "fill_at": "open",
  "flat_at_close": true,    // exit at the close of the bar you entered
  "cost_multiplier": 1.0,
  "max_participation": 0.10
}
```

`flat_at_close: true` liquidates every position at the close of the bar it was
opened in, charging exit costs. The position is therefore opened and closed
within one bar, and no overnight exposure is ever taken. Positions still open
at the end of the sample are force-closed into the ledger so the accounting
identity still holds.

## The annualisation trap

A session panel holds **one bar per session**, so its return series is
**daily** and annualises at **252** — exactly like the daily pipeline. Nothing
in the metric code changes.

If a future change makes the panel hold all seven RTH bars, the return series
becomes intraday and must be annualised at bars-per-year. Getting this wrong
inflates Sharpe by the square root of the bar-count ratio (≈4.9× for 60m
versus daily). **Assert the annualisation factor whenever the panel shape
changes.**

## Session features

`scripts/intraday.py` registers:

| Feature | Meaning |
|---------|---------|
| `session_drift` | `(close_location_in_range − 0.5) × volume` — where the session closed within its range, signed and scaled by volume. `lookback > 1` averages prior sessions |
| `session_gap` | Session open versus the previous session's close |
| `session_return` | Intraday open-to-close return of the session window |

All obey the causality contract: the value at index `i` uses bars `<= i`. A
session's close is known at 10:00 ET, comfortably before the **next** session's
open, so a signal from session `t−1` can be traded in session `t`.

## What session mode cannot test

- **Options Greeks** (gamma exposure, dealer positioning) — no free historical
  series exists.
- **Order-book microstructure** — requires tick or book data.
- **Overnight or pre/post-market sessions** — the provider serves RTH only.
- **Sub-hour windows** — finer intervals are refused by the provider.

When a hypothesis depends on one of these, decompose it and let the user
choose which leg to test. Say in the report which parts of the hypothesis the
run does and does not cover.

## Before running an intraday backtest

```bash
python scripts/intraday_probe.py
```

It reports session count, the date range, the DST-hour check, the return-series
magnitude and whether RSI is degenerate — the failures that otherwise surface
as a confidently wrong verdict.

```bash
python scripts/selftest_intraday.py
```

A second, offline check of the session path itself: DST-safe extraction, the
causality of `session_drift`, no overnight exposure under `flat_at_close`,
the accounting identity on a winning and a losing session run, the round-trip
trade count, the 60m fetch URL, and the cache-format migration. It takes under
a second and needs no network.

### Enforced constraints in the session path

Three configurations used to fail **silently** — the run completed and
returned a plausible-looking backtest of the wrong thing. The engine now
rejects each with a `ValueError` naming the fix, and `selftest_intraday.py`
asserts the rejection, so the mistake cannot come back unnoticed:

1. **`window: "all"` is rejected.** A multi-bar session panel would need
   different date keying and a different annualisation, and the previous
   implementation returned only each bar's OPEN in a bar-shaped slot, so the
   panel had a high BELOW its low and a volume that was a price. Only
   `"window": "first"` is supported.

2. **`interval: "60m"` without `mode: "session"` is rejected.** `run_spec`
   branches on `execution.mode`; without it the spec goes to the daily path,
   whose collapser keeps the LAST record of each UTC date — the 15:30 ET bar,
   not the 09:30 ET opening window. Always set `execution.mode: "session"`.

3. **`fill_at: "close"` with `flat_at_close: true` is rejected.** The position
   would be entered at the close of a bar and closed at that same close: every
   session realises 0.00 of P&L while paying the full round-trip cost. Use
   `fill_at: "open"` for session mode.

A third offline suite covers corporate actions and cache invalidation:

```bash
python scripts/selftest_corporate_actions.py
```

It asserts that the ex-dividend drop is removed, that the adjusted series is
continuous across the ex-date, that volume is not scaled, that a missing or
malformed `adjclose` leaves prices alone, and that a pre-adjustment daily
cache file is refetched rather than served.

### Dividends in session mode

Daily bars are split- and dividend-adjusted (the engine back-adjusts OHLC by
`adjclose/close`). **Intraday 60m bars are not** — the provider publishes no
`adjclose` for that interval — so an ex-dividend date inside a 60m sample
leaves a small uncompensated drop, and a split inside a session is not
modelled at all.
