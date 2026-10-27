#!/usr/bin/env python3
"""
intraday.py -- Intraday support for the backtest lab.

Adds three things the daily engine lacked:

  1. `build_session_panel()` — fetch 60-minute bars and collapse each trading
     session to ONE bar representing the opening window (09:30-10:00 ET).
  2. `f_session_drift()` — the net-drift feature: where the session closed
     within its range, signed and scaled by volume.
  3. `flat_at_close` execution — enter at the bar open, exit at that same
     bar's close. Flat by the close means no overnight exposure.

WHY GROUP BY UTC DATE
---------------------
Yahoo anchors intraday bars to EXCHANGE-LOCAL time, so the 09:30 ET bar is
13:30 UTC in summer (EDT) and 14:30 UTC in winter (EST). A fixed-UTC filter
would silently break at every DST transition. But both the first (09:30 ET)
and last (15:30 ET) bars of a session always fall on the SAME UTC calendar
date as the ET session, so grouping by UTC date and taking the first bar per
group recovers the opening window with no timezone database at all -- which
also means no dependency on the Windows tz database.

The result is a DAILY panel (one bar per session), so every metric, the
walk-forward folds, and the Sharpe annualisation (252) all work unchanged.
"""

from __future__ import annotations

import datetime as dt

try:
    # Flat import: running as a script, or with scripts/ on sys.path.
    from btengine import (
        DEFAULT_COSTS,
        Panel,
        audit_panel,
        infer_asset_class,
        load_or_fetch,
    )
except ImportError:  # pragma: no cover - exercised by the installed `btlab` layout
    # Relative import: installed as a package (btlab.intraday).
    from .btengine import (
        DEFAULT_COSTS,
        Panel,
        audit_panel,
        infer_asset_class,
        load_or_fetch,
    )

SESSION_INTERVAL = "60m"


# --------------------------------------------------------------------------
# Session extraction
# --------------------------------------------------------------------------

def extract_sessions(records, window="first"):
    """
    Collapse 60m records to one bar per session.

    window: "first" -> the 09:30-10:00 ET opening bar (the only supported
            value). Anything else raises, because the session panel is built
            as ONE bar per session: a multi-bar panel would need different
            date keying and a different annualisation, and returning bar
            opens in a bar-shaped slot produced a corrupt panel (high < low,
            volume read as a price) rather than an error.

    Returns {YYYY-MM-DD: [o,h,l,c,v]} keyed by the UTC date of the session.
    """
    if window != "first":
        raise ValueError(
            f"extract_sessions supports window='first' only, got {window!r}. "
            "A multi-bar session panel is not implemented.")
    by_day = {}
    for rec in records:
        ts = rec[0]
        o, h, l, c, v = rec[1], rec[2], rec[3], rec[4], rec[5]
        if c is None:
            continue
        day = dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%d")
        by_day.setdefault(day, []).append((ts, o, h, l, c, v))
    sessions = {}
    for day, bars in by_day.items():
        bars.sort(key=lambda r: r[0])
        b = bars[0]  # earliest bar of the session = the opening window
        sessions[day] = [b[1], b[2], b[3], b[4], b[5]]
    return sessions


_SESSION_CACHE = {}


def build_session_panel(symbols, start=None, end=None, refresh=False, verbose=False,
                        window="first"):
    """
    Stage 1+2 for intraday: fetch 60m bars, extract the opening window per
    session, align symbols on a shared session axis.

    Returns (panel, failures, audit, info)

    Panels are memoised per process: the validation gauntlet rebuilds the same
    panel for every cost-stress and parameter-sweep variant, and rebuilding
    costs minutes without this.
    """
    key = (tuple(sorted(symbols)), start, end, window, refresh)
    if not refresh and key in _SESSION_CACHE:
        return _SESSION_CACHE[key]

    per_symbol, failures = {}, {}
    for s in symbols:
        try:
            payload = load_or_fetch(s, SESSION_INTERVAL, refresh=refresh)
            sess = extract_sessions(payload["records"], window=window)
            if not sess:
                failures[s] = "no sessions"
            else:
                per_symbol[s] = sess
        except Exception as e:  # noqa: BLE001
            failures[s] = f"{type(e).__name__}: {e}"
        if verbose:
            n = len(per_symbol.get(s, {}))
            print(f"  [{s}] {n} sessions" if n else f"  [{s}] FAILED: {failures.get(s)}",
                  flush=True)
    if not per_symbol:
        return None, failures, [], {}

    all_days = sorted({d for s in per_symbol for d in per_symbol[s]})
    if start:
        all_days = [d for d in all_days if d >= start]
    if end:
        all_days = [d for d in all_days if d <= end]

    # Require at least half the universe present so one short-lived symbol
    # cannot shorten the axis for everyone else.
    min_cov = min(len(per_symbol), max(1, int(0.5 * len(per_symbol))))
    days = [d for d in all_days
            if sum(1 for s in per_symbol if d in per_symbol[s]) >= min_cov]
    if len(days) < 60:
        raise RuntimeError(f"too few sessions after alignment: {len(days)}")

    bars = {}
    for s in per_symbol:
        sd = {}
        last = None
        for d in days:
            if d in per_symbol[s]:
                last = per_symbol[s][d]
            if last is not None:
                sd[d] = last
        bars[s] = sd

    costs = {s: DEFAULT_COSTS.get(infer_asset_class(s), DEFAULT_COSTS["etf"])
             for s in bars}
    panel = Panel(days, sorted(bars), bars, costs=costs)
    audit = audit_panel(panel)
    info = {
        "n_sessions": len(days),
        "first_session": days[0],
        "last_session": days[-1],
        "window": window,
        "interval": SESSION_INTERVAL,
        "note": "One bar per session = the 09:30-10:00 ET opening window. "
                "Returns are therefore DAILY and annualise at 252.",
    }
    result = (panel, failures, audit, info)
    if not refresh:
        _SESSION_CACHE[key] = result
    return result


# --------------------------------------------------------------------------
# The net-drift feature
# --------------------------------------------------------------------------

def f_session_drift(panel, sym, lookback=1, **kw):
    """
    Net drift of the session window: where it closed within its range,
    centred on zero, scaled by volume.

        close_location = (close - low) / (high - low)      in [0, 1]
        drift          = (close_location - 0.5) * volume

    Positive = closed near the high of the opening window on heavy volume
    (net buying pressure). Negative = the mirror image.

    With lookback > 1 the drift is averaged over that many prior sessions,
    which the user's intake answer set to 1 (previous session only).

    Causal: the value at bar i uses bars <= i, and the session's close is
    known at 10:00 ET -- well before the NEXT session's open.
    """
    n = panel.n
    s = panel.symbols
    o, h, l, c, v = panel.open[sym], panel.high[sym], panel.low[sym], \
        panel.close[sym], panel.vol[sym]
    raw = []
    for i in range(n):
        hi, lo, cl, vol = h[i], l[i], c[i], v[i]
        if None in (hi, lo, cl):
            raw.append(None)
            continue
        rng = hi - lo
        if rng <= 0 or not vol:
            raw.append(None)
            continue
        loc = (cl - lo) / rng
        raw.append((loc - 0.5) * vol)
    if lookback <= 1:
        return raw
    out = [None] * n
    for i in range(n):
        if i < lookback - 1:
            continue
        w = [raw[j] for j in range(i - lookback + 1, i + 1) if raw[j] is not None]
        if len(w) == lookback:
            out[i] = sum(w) / lookback
    return out


def f_session_gap(panel, sym, **kw):
    """
    Overnight gap expressed in the session window: the session's open versus
    the previous session's close, as a fraction of the previous close.
    Used only when a hypothesis is explicitly about gaps.
    """
    n = panel.n
    o, c = panel.open[sym], panel.close[sym]
    out = [None] * n
    for i in range(1, n):
        pc, po = c[i - 1], o[i]
        if pc and po is not None and pc > 0:
            out[i] = po / pc - 1.0
    return out


def f_session_return(panel, sym, **kw):
    """Intraday return of the session window: open to close."""
    n = panel.n
    o, c = panel.open[sym], panel.close[sym]
    out = [None] * n
    for i in range(n):
        if o[i] and c[i] is not None and o[i] > 0:
            out[i] = c[i] / o[i] - 1.0
    return out


INTRADAY_FEATURES = {
    "session_drift": f_session_drift,
    "session_gap": f_session_gap,
    "session_return": f_session_return,
}
