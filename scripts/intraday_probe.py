#!/usr/bin/env python3
"""
intraday_probe.py -- Probe session-window (60m) data BEFORE committing to an
intraday backtest.

For each symbol it reports and checks:

  1. 60m availability and how far back the provider reaches.
  2. Session count and date range (the sample-size limit).
  3. Bars per day (RTH expected = 7).
  4. The UTC hour of each day's FIRST bar. It must be 13 (EDT) or 14 (EST);
     a third hour means the session extraction is grouping wrongly.
  5. The magnitude of the return series. A price ratio leaked in as a return
     shows up here as a ~100% "move" on every bar.
  6. Whether RSI is degenerate (pinned at 0 or 100), which is the tell for
     ratios being stored where returns belong.

Run:  python scripts/intraday_probe.py [SYMBOL ...]
Exit: 0 when every check passed, 1 otherwise.

Run it as a FILE, not as a heredoc — a script file is re-runnable, and a
one-off heredoc probe is neither.
"""

import datetime as dt
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from btengine import UNIVERSE_PRESETS, f_rsi, load_or_fetch
from intraday import build_session_panel, extract_sessions

# The exchange-local opening hour maps to 13 UTC in summer (EDT, UTC-4) and
# 14 UTC in winter (EST, UTC-5). Anything else means the grouping is wrong.
EXPECTED_OPEN_UTC_HOURS = {13, 14}

# An hour-window close-to-close series for these ETFs should stay well under
# this. A ratio leak produces ~1.0 (i.e. a "100% move") on every bar.
MAX_PLAUSIBLE_BAR_RETURN = 0.35


def probe_raw(symbol):
    """Per-symbol fetch and structural checks. Returns (ok, rows)."""
    rows = []
    ok = True
    try:
        payload = load_or_fetch(symbol, "60m")
    except Exception as e:  # noqa: BLE001
        return False, [("60m fetch", f"FAILED {type(e).__name__}: {e}")]

    records = payload.get("records") or []
    if not records:
        return False, [("60m fetch", "no records returned")]

    sessions = extract_sessions(records)
    if not sessions:
        return False, [("60m fetch", "no sessions extracted")]

    days = sorted(sessions)
    rows.append(("60m bars", f"{len(records)}"))
    rows.append(("sessions", f"{len(days)}  {days[0]} -> {days[-1]}"))

    # First bar of each UTC day -> the UTC hour it lands on.
    first_ts = {}
    counts = {}
    for rec in records:
        stamp = dt.datetime.fromtimestamp(rec[0], dt.timezone.utc)
        day = stamp.strftime("%Y-%m-%d")
        counts[day] = counts.get(day, 0) + 1
        if day not in first_ts:
            first_ts[day] = stamp.hour
    hours = sorted(set(first_ts.values()))
    dst_ok = set(hours).issubset(EXPECTED_OPEN_UTC_HOURS)
    rows.append(("open-bar UTC hours",
                 f"{hours}  {'PASS' if dst_ok else 'FAIL (expected a subset of [13, 14])'}"))
    if not dst_ok:
        ok = False

    lo, hi = min(counts.values()), max(counts.values())
    rows.append(("bars per day", f"{lo}-{hi}  (RTH expected 7)"))
    return ok, rows


def probe_panel(panel):
    """Return-series and RSI checks on an assembled session panel."""
    rows = []
    ok = True
    for sym in panel.symbols:
        rets = [r for r in panel.rets[sym] if r is not None]
        if not rets:
            rows.append((sym, "no returns"))
            ok = False
            continue
        biggest = max(abs(r) for r in rets)
        mag_ok = biggest <= MAX_PLAUSIBLE_BAR_RETURN
        rsi_vals = [v for v in f_rsi(panel, sym, 14) if v is not None]
        if rsi_vals:
            span = max(rsi_vals) - min(rsi_vals)
            rsi_ok = span >= 1.0 and min(rsi_vals) >= 0.0 and max(rsi_vals) <= 100.0
            rsi_txt = f"min {min(rsi_vals):.0f} max {max(rsi_vals):.0f} span {span:.0f}"
        else:
            rsi_ok = False
            rsi_txt = "no values"
        rows.append((sym, f"max |ret| {biggest:.2%} {'PASS' if mag_ok else 'FAIL'}   "
                          f"RSI {rsi_txt} {'PASS' if rsi_ok else 'FAIL (pinned?)'}"))
        if not (mag_ok and rsi_ok):
            ok = False
    return ok, rows


def main(argv):
    symbols = argv[1:] or list(UNIVERSE_PRESETS["us_equity_broad"])
    print("=" * 66)
    print("SESSION-WINDOW DATA PROBE")
    print("=" * 66)

    all_ok = True
    for sym in symbols:
        print(f"\n--- {sym} ---")
        ok, rows = probe_raw(sym)
        for name, detail in rows:
            print(f"  {name:22s} {detail}")
        if not ok:
            print("  -> RAW CHECKS FAILED")
        all_ok = all_ok and ok

    print("\n" + "=" * 66)
    print("ASSEMBLED PANEL CHECKS")
    print("=" * 66)
    try:
        panel, failures, audit, info = build_session_panel(symbols, verbose=False)
    except Exception as e:  # noqa: BLE001
        print(f"  panel build FAILED: {type(e).__name__}: {e}")
        return 1

    if panel is None:
        print(f"  panel build FAILED: {failures}")
        return 1

    print(f"  sessions {info['n_sessions']}  {info['first_session']} -> {info['last_session']}")
    if failures:
        print(f"  symbol failures: {failures}")
        all_ok = False
    for a in audit:
        print(f"  audit: {a['symbol']} {a['issue']} ({a['severity']})")

    ok, rows = probe_panel(panel)
    for sym, detail in rows:
        print(f"  {sym:8s} {detail}")
    all_ok = all_ok and ok

    print("\n" + "=" * 66)
    print("PROBE RESULT:", "PASS" if all_ok else "FAIL")
    if not all_ok:
        print("Do not run an intraday backtest until the failures above are fixed —")
        print("they surface as a confidently wrong verdict, not as an error.")
    print("=" * 66)
    return 0 if all_ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
