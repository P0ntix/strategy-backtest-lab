#!/usr/bin/env python3
"""
selftest_intraday.py -- Regression suite for the INTRADAY / SESSION path.

`selftest.py` covers the DAILY path with ten checks. The session path --
60-minute bars collapsed to one opening-window bar per trading session,
executed flat-at-close -- had no coverage of its own, and it is the path
where a DST bug silently changes what a "day" means and a ledger bug
silently multiplies the trade count.

TEN checks, all offline, all synthetic, no network, no third-party
packages, well under a second:

  1. Session extraction is DST-safe. The 09:30 ET bar is 13:30 UTC in
     summer (EDT) and 14:30 UTC in winter (EST); the extraction must find
     it in both, on either side of the March transition. The daily path's
     collapser (records_to_daily) keeps the LAST bar of a date instead --
     the two must stay distinguishable.
  2. session_drift is causal. Recomputed on a panel truncated to the first
     half of the sessions, it must reproduce its first-half values EXACTLY
     (the technique of btvalidate.causality_audit).
  3. flat_at_close means NO overnight exposure. The return on a bar must
     equal that bar's open->close move, never the overnight gap.
  4. The accounting identity holds for a session-mode run:
     final_equity == capital + sum(trade pnl) - costs.
  5. One completed trade per session per instrument -- not the inflated
     count the old partial-adjustment ledger produced.
  6. The 60m fetch URL uses range=2y. Yahoo answers HTTP 422 to a
     full-range (period1=0&period2=9999999999) intraday request.
  7. An OLD date-keyed cache file is ignored and refetched, not served as
     if it were intraday bars.

Checks 8-10 assert that the three session-path MISCONFIGURATIONS are
rejected rather than silently mis-executed. They were originally written as
@unittest.expectedFailure against a version of the engine that silently
returned wrong results; the engine now raises ValueError for each, so they
have been converted into assertions of the fix. If a future change makes one
of these misconfigurations run silently again, these tests fail.

Run:  python scripts/selftest_intraday.py
Exit: 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import random
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import btengine
import intraday
from btengine import DEFAULT_SPEC, Panel, simulate
from intraday import extract_sessions, f_session_drift

RESULTS = []


# --------------------------------------------------------------------------
# Synthetic session data
#
# All timestamps are UTC epochs. The US Eastern offset is derived from the
# statutory rule (2nd Sunday of March -> 1st Sunday of November) so the
# fixtures are DST-correct WITHOUT touching a timezone database -- exactly
# the constraint the engine itself works under.
# --------------------------------------------------------------------------

def nth_weekday(year, month, weekday, n):
    """Date of the n-th given weekday (Mon=0) of a month."""
    first = dt.date(year, month, 1)
    delta = (weekday - first.weekday()) % 7
    return first + dt.timedelta(days=delta + 7 * (n - 1))


def et_offset_hours(d):
    """US Eastern offset in hours for a date (-4 EDT, -5 EST)."""
    dst_start = nth_weekday(d.year, 3, 6, 2)     # 2nd Sunday of March
    dst_end = nth_weekday(d.year, 11, 6, 1)      # 1st Sunday of November
    return -4 if dst_start <= d < dst_end else -5


def session_bar_times(d):
    """The seven RTH 60-minute bar start times of an ET session, in UTC."""
    off = et_offset_hours(d)
    out = []
    for k in range(7):                          # 09:30 .. 15:30 ET
        utc_minutes = (9 * 60 + 30 + 60 * k) - off * 60
        h, m = divmod(utc_minutes, 60)
        out.append(dt.datetime(d.year, d.month, d.day, h, m, tzinfo=dt.timezone.utc))
    return out


def business_days(start, count):
    """`count` consecutive Mon-Fri dates starting at `start`."""
    out = []
    d = start
    while len(out) < count:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out


def make_records(dates, seed=11, start_price=500.0, drift=0.0002, noise=0.002):
    """Synthetic 60m records: [epoch, o, h, l, c, v].

    `drift` is the per-bar directional move, `noise` the symmetric jitter
    around it. Both are needed to build a decisive WINNER and a decisive
    LOSER: the accounting identity is the sharper test on a losing run,
    because a sign error has somewhere to hide.
    """
    rnd = random.Random(seed)
    price = start_price
    recs = []
    for d in dates:
        for ts in session_bar_times(d):
            o = price
            c = o * (1.0 + drift + rnd.uniform(-noise, noise))
            h = max(o, c) * (1.0 + rnd.uniform(0.0, 0.001))
            l = min(o, c) * (1.0 - rnd.uniform(0.0, 0.001))
            v = rnd.uniform(500_000.0, 2_000_000.0)
            recs.append([int(ts.timestamp()), round(o, 4), round(h, 4),
                         round(l, 4), round(c, 4), round(v, 1)])
            price = c
    return recs


def make_panel(dates, symbols, seed=3, start_price=500.0):
    """A synthetic Panel on a session axis."""
    bars = {}
    for j, sym in enumerate(symbols):
        recs = make_records(dates, seed=seed + j, start_price=start_price)
        by_day = {}
        for rec in recs:
            day = dt.datetime.fromtimestamp(rec[0], dt.timezone.utc).strftime("%Y-%m-%d")
            by_day.setdefault(day, rec)
        bars[sym] = {d: by_day[d][1:6] for d in by_day}
    costs = {s: {"commission_bps": 0.5, "half_spread_bps": 1.0,
                 "slippage_bps": 1.0, "borrow_bps": 10.0} for s in symbols}
    return Panel([d.isoformat() for d in dates], sorted(symbols), bars, costs=costs)


def spec(name, hypothesis, preset, feature, signal, portfolio,
         period=None, capital=100000.0, execution=None):
    """Spec builder -- same shape as selftest.py's spec()."""
    s = json.loads(json.dumps(DEFAULT_SPEC))
    s.update({
        "name": name,
        "hypothesis": hypothesis,
        "universe": {"preset": preset, "symbols": []},
        "period": period or {"start": None, "end": None, "interval": "1d"},
        "features": feature,
        "signal": signal,
        "portfolio": portfolio,
        "execution": execution or {"fill_at": "open", "max_participation": 0.10,
                                   "cost_multiplier": 1.0},
        "capital": capital,
    })
    return s


def identity_gap(sim, capital):
    """
    (init + sum(pnl)) - final_equity. The trade ledger is NET of costs, so
    this must be ZERO -- every cost dollar is already netted out of the
    trade that caused it. It used to equal costs_paid, because the ledger
    held the gross price move while costs were debited from cash only.
    """
    return (capital + sum(t["pnl"] for t in sim["trades"])) - sim["final_equity"]


# --------------------------------------------------------------------------
# TEST 1 -- session extraction is DST-safe
# --------------------------------------------------------------------------

class Test01SessionExtraction(unittest.TestCase):
    """Session extraction picks the 09:30 ET bar across DST."""

    def test_1_dst_safe_session_extraction(self):
        # Three sessions: deep winter, either side of the March transition,
        # and deep summer.
        dates = [dt.date(2024, 1, 10),    # EST -> 09:30 ET is 14:30 UTC
                 dt.date(2024, 3, 8),     # EST (last Friday before the change)
                 dt.date(2024, 3, 11),    # EDT (first Monday after the change)
                 dt.date(2024, 7, 10)]    # EDT -> 09:30 ET is 13:30 UTC
        recs = []
        for d in dates:
            for k, ts in enumerate(session_bar_times(d)):
                base = 100.0 + k          # distinct price per bar -> the
                recs.append([int(ts.timestamp()), base, base + 0.5,   # chosen
                             base - 0.5, base + 0.25, 1_000_000.0])  # bar is
                                                                  # identifiable
        sessions = extract_sessions(recs, window="first")
        self.assertEqual(sorted(sessions), ["2024-01-10", "2024-03-08",
                                            "2024-03-11", "2024-07-10"])
        expected_hour = {"2024-01-10": 14, "2024-03-08": 14,
                         "2024-03-11": 13, "2024-07-10": 13}
        for day, bar in sessions.items():
            # The bar selected must be the EARLIEST record of that UTC day.
            day_recs = [r for r in recs
                        if dt.datetime.fromtimestamp(r[0], dt.timezone.utc)
                        .strftime("%Y-%m-%d") == day]
            first = min(day_recs, key=lambda r: r[0])
            self.assertEqual(bar, first[1:6], f"{day}: wrong bar selected")
            hour = dt.datetime.fromtimestamp(first[0], dt.timezone.utc).hour
            self.assertEqual(
                hour, expected_hour[day],
                f"{day}: first bar at {hour}:xx UTC, expected "
                f"{expected_hour[day]}:xx UTC (09:30 ET is 13:30 UTC in EDT "
                f"and 14:30 UTC in EST)")
            print(f"    {day}  first bar {hour}:30 UTC  "
                  f"({'EDT' if hour == 13 else 'EST'})  "
                  f"open={bar[0]:.2f} close={bar[3]:.2f}")

        # Input order must not matter: the extraction sorts within the day.
        shuffled = list(reversed(recs))
        self.assertEqual(extract_sessions(shuffled, window="first"), sessions)

        # The session key is the UTC date, which for RTH US equity is the
        # ET date -- no timezone database required to get there.
        for day in sessions:
            self.assertEqual(day[:4], "2024")

        # window="all" is NOT supported: it would require a multi-bar panel
        # with different keying and annualisation, and it used to return
        # bar opens in a bar-shaped slot (corrupt panel). It must raise.
        with self.assertRaises(ValueError):
            extract_sessions(recs, window="all")

        # CONTRAST with the DAILY path's collapser. records_to_daily keeps
        # the LAST record of each UTC date (it overwrites); extract_sessions
        # keeps the FIRST. On 1d data the two agree -- one bar per date --
        # but on 60m data they disagree by an entire session. If someone
        # ever "simplifies" extract_sessions into records_to_daily, the
        # session panel silently becomes the CLOSING bar instead of the
        # opening bar, and nothing else in the suite would notice.
        daily = btengine.records_to_daily(recs)
        self.assertEqual(sorted(daily), sorted(sessions))
        for day in sessions:
            self.assertNotEqual(daily[day], sessions[day],
                                f"{day}: the two collapser functions agree -- "
                                f"the opening-window selection is no longer "
                                f"distinguishable from the daily path")
            # records_to_daily keeps the last bar of the date.
            last = max((r for r in recs
                        if dt.datetime.fromtimestamp(r[0], dt.timezone.utc)
                        .strftime("%Y-%m-%d") == day), key=lambda r: r[0])
            self.assertEqual(daily[day], last[1:6])


# --------------------------------------------------------------------------
# TEST 2 -- session_drift is causal
# --------------------------------------------------------------------------

class Test02Causality(unittest.TestCase):
    """session_drift recomputed on half the sessions reproduces its first half."""

    def test_2_session_drift_is_causal(self):
        dates = business_days(dt.date(2024, 1, 2), 60)
        symbols = ["AAA", "BBB"]
        panel = make_panel(dates, symbols)
        half = panel.n // 2
        sub = Panel(panel.dates[:half], panel.symbols, panel.bars,
                    panel.costs, panel.meta)

        for lookback in (1, 3):
            for sym in panel.symbols:
                full = f_session_drift(panel, sym, lookback=lookback)
                part = f_session_drift(sub, sym, lookback=lookback)
                for i in range(half):
                    a, b = full[i], part[i]
                    self.assertEqual(a is None, b is None,
                                     f"lookback={lookback} {sym} bar {i}: "
                                     f"None pattern differs ({a} vs {b})")
                    if a is not None:
                        # EXACT reproduction: no tolerance band, because a
                        # look-ahead that is small is still a look-ahead.
                        self.assertEqual(
                            a, b,
                            f"lookback={lookback} {sym} bar {i}: "
                            f"full={a!r} truncated={b!r} -- look-ahead")

        # The same property must hold through the FEATURE REGISTRY, which is
        # how the pipeline actually consumes it.
        btengine.register_features(intraday.INTRADAY_FEATURES)
        feats_full = btengine.compute_features(
            panel, [{"name": "session_drift", "params": {"lookback": 3}}])
        feats_sub = btengine.compute_features(
            sub, [{"name": "session_drift", "params": {"lookback": 3}}])
        for sym in panel.symbols:
            for i in range(half):
                self.assertEqual(feats_full["session_drift"][sym][i],
                                 feats_sub["session_drift"][sym][i],
                                 f"registry session_drift {sym} bar {i}")
        n_vals = sum(1 for v in feats_full["session_drift"]["AAA"] if v is not None)
        print(f"    sessions={panel.n}  truncated={half}  "
              f"non-null drift values={n_vals}")


# --------------------------------------------------------------------------
# TEST 3 -- flat_at_close means no overnight exposure
# --------------------------------------------------------------------------

class Test03Execution(unittest.TestCase):
    """A position opened at the open and closed at the close earns no gap."""

    def test_3_flat_at_close_has_no_overnight_exposure(self):
        dates = ["2024-01-10", "2024-01-11", "2024-01-12",
                 "2024-01-16", "2024-01-17"]
        bars = {}
        for i in range(len(dates)):
            o = 100.0 * (1.05 ** i)          # +5% overnight gap every night
            c = o * 1.01                       # +1% inside the session
            bars[dates[i]] = [o, max(o, c) * 1.002, min(o, c) * 0.998, c,
                              5_000_000.0]
        panel = Panel(dates, ["TEST"], {"TEST": bars},
                      costs={"TEST": {"commission_bps": 0.0, "half_spread_bps": 0.0,
                                      "slippage_bps": 0.0, "borrow_bps": 0.0}})
        target = {"TEST": [1.0] * len(dates)}
        sim = simulate(panel, target, initial_capital=100000.0, fill_at="open",
                       cost_multiplier=1.0, max_participation=0.10,
                       flat_at_close=True, trade_mask=None)

        rets = sim["returns"]
        # Bar 0 cannot trade: orders decided at bar t fill at t+1.
        self.assertAlmostEqual(rets[0], 0.0, places=12)
        for i in range(1, len(dates)):
            o = bars[dates[i]][0]
            c = bars[dates[i]][3]
            intrabar = c / o - 1.0
            gap = o / bars[dates[i - 1]][3] - 1.0
            with_gap = c / bars[dates[i - 1]][3] - 1.0
            self.assertAlmostEqual(rets[i], intrabar, places=12,
                                   msg=f"bar {i}: return is not the "
                                       f"open->close move")
            # And it must NOT be the overnight-inclusive return.
            self.assertGreater(
                abs(rets[i] - with_gap), 0.01,
                f"bar {i}: return contains the overnight gap "
                f"(gap={gap:+.4f})")
        # Flat at the close of every single bar -> no exposure carried over.
        for i, g in enumerate(sim["gross_exposure"]):
            self.assertAlmostEqual(g, 0.0, places=12,
                                   msg=f"bar {i}: still exposed at the close")
        # Equity compounds the intraday move only.
        expected_final = 100000.0 * (1.01 ** (len(dates) - 1))
        self.assertAlmostEqual(sim["final_equity"], expected_final, places=6)
        print(f"    bars={len(dates)}  final_equity={sim['final_equity']:,.2f}  "
              f"(gap-bearing path would have reached "
              f"{100000.0 * (1.05 * 1.01) ** (len(dates) - 1):,.2f})")
        print(f"    gross exposure after every bar: "
              f"{set(round(g, 12) for g in sim['gross_exposure'])}")
        # One completed round trip per session that held a position.
        self.assertEqual(len(sim["trades"]), len(dates) - 1)
        for t in sim["trades"]:
            self.assertEqual(t["entry_bar"], t["bar"])
            self.assertEqual(t["bars_held"], 0)
            self.assertTrue(t.get("flat_at_close"))


# --------------------------------------------------------------------------
# TEST 4 + 5 -- session-mode run: accounting identity and trade count
# --------------------------------------------------------------------------

SESSION_SYMBOLS = ["SYN-A", "SYN-B"]
# 130 sessions -- enough to clear the daily path's 120-bar floor, so the
# checks cannot pass merely because the panel was too short to build.
SESSION_DATES = business_days(dt.date(2024, 1, 2), 130)


def session_payloads(dates, drift):
    """Payloads shaped like load_or_fetch() output, for a given drift."""
    out = {}
    for j, sym in enumerate(SESSION_SYMBOLS):
        out[sym] = {"symbol": sym, "interval": "60m", "fetched": 0,
                    "currency": "USD", "name": sym,
                    "records": make_records(dates, seed=100 + j,
                                            start_price=500.0 + 50 * j,
                                            drift=drift)}
    return out


def fake_load_or_fetch(payloads):
    def _fake(symbol, interval="1d", refresh=False, max_age_days=3):
        return payloads[symbol]
    return _fake


def session_spec():
    return spec(
        "SESSION MODE REGRESSION PROBE",
        "probe",
        "single",
        [{"name": "session_drift", "params": {"lookback": 1}}],
        {"type": "constant", "params": {"weight": 1.0}},
        {"gross_leverage": 1.0, "rebalance": "daily", "long_only": False},
        period={"start": None, "end": None, "interval": "60m"},
        execution={"mode": "session", "window": "first", "fill_at": "open",
                   "flat_at_close": True, "cost_multiplier": 1.0,
                   "max_participation": 0.10},
    )


class Test04SessionMode(unittest.TestCase):
    """Accounting identity and trade count in session mode."""

    @classmethod
    def setUpClass(cls):
        cls.dates = SESSION_DATES

    def _run_session(self, drift, symbols=None):
        # build_session_panel memoises per process, keyed on the SYMBOL SET
        # (not on the data behind it). Two fixtures that share a universe but
        # carry different prices would otherwise collide in that cache and
        # the second run would silently reuse the first run's panel. Clear it
        # and pass refresh=True so each fixture gets its own data.
        if hasattr(intraday, "_SESSION_CACHE"):
            intraday._SESSION_CACHE.clear()
        payloads = session_payloads(self.dates, drift)
        s = session_spec()
        syms = list(symbols or SESSION_SYMBOLS)
        s["universe"]["symbols"] = syms
        payload_map = {sym: payloads[syms[0]] for sym in syms}
        with mock.patch.object(intraday, "load_or_fetch",
                               fake_load_or_fetch(payload_map)):
            bundle = btengine.run_spec(s, refresh=True, verbose=False)
        self.assertNotIn("error", bundle, bundle.get("error"))
        return bundle

    def test_4_accounting_identity_holds_in_session_mode(self):
        # A winner AND a loser: a losing run is the sharper test because it
        # forces sign errors to surface. The drift must clear the round-trip
        # cost (2.5 bps a side on gross 1.0) or the "winner" is a loser.
        for label, drift in (("winning", 0.0020), ("losing", -0.0020)):
            bundle = self._run_session(drift)
            sim = bundle["sim"]
            capital = session_spec()["capital"]
            gap = identity_gap(sim, capital)
            expected = sim["costs_paid"]
            tol = max(1e-6, capital * 1e-8)
            # The ledger is net of costs, so the reconciliation gap is ZERO.
            # It used to equal costs_paid, which meant the trade statistics
            # were gross while the equity curve was net.
            self.assertLess(
                abs(gap), tol,
                f"{label}: (capital + sum(pnl)) - final_equity = {gap:.6f}, "
                f"must be 0 (the ledger is net of costs)")
            self.assertGreater(expected, 0.0, f"{label}: no costs were paid")
            ret = sim["final_equity"] / capital - 1.0
            # The fixtures must actually straddle zero, or this check only
            # ever runs on one sign and cannot catch a sign error.
            if label == "winning":
                self.assertGreater(ret, 0.0, "the 'winning' fixture lost money")
            else:
                self.assertLess(ret, 0.0, "the 'losing' fixture made money")
            print(f"    {label:8s} trades={len(sim['trades']):5d} "
                  f"gap={gap:.8f}  costs={expected:,.2f}  "
                  f"final_equity={sim['final_equity']:,.2f}  "
                  f"return={ret:+.2%}")

    def test_5_one_trade_per_session_per_instrument(self):
        bundle = self._run_session(0.0004)
        sim = bundle["sim"]
        panel = bundle["panel"]
        n_sessions = panel.n
        n_sym = len(panel.symbols)

        # (a) flat_at_close: one completed round trip per session per
        #     instrument that held a position. The first session cannot
        #     trade (orders fill at t+1), hence n_sessions - 1.
        trades = sim["trades"]
        expected = (n_sessions - 1) * n_sym
        self.assertEqual(len(trades), expected,
                         f"expected {expected} round trips "
                         f"({n_sessions} sessions x {n_sym} instruments), "
                         f"got {len(trades)}")
        for t in trades:
            self.assertEqual(t["entry_bar"], t["bar"],
                             "a session-mode trade must open and close on "
                             "the same bar")
            self.assertEqual(t["bars_held"], 0)
            self.assertTrue(t.get("flat_at_close"))
        # One trade per (bar, symbol) -- never two, which is what counting
        # every rebalance slice produces.
        seen = set()
        for t in trades:
            key = (t["bar"], t["symbol"])
            self.assertNotIn(key, seen, f"duplicate trade for {key}")
            seen.add(key)

        # (b) The discriminating case: with positions HELD across sessions
        #     (flat_at_close off) and target weights that genuinely swing,
        #     the engine fills many times. A correct ledger counts ONE
        #     lifecycle per instrument, not one per fill. The old buggy
        #     ledger counted the slices and produced ~n_sessions trades per
        #     symbol. Quantify both sides so the check cannot pass
        #     vacuously: the turnover proves many fills actually happened.
        swing = [0.3 + 0.4 * ((i // 3) % 2) for i in range(n_sessions)]
        target = {s: list(swing) for s in panel.symbols}
        held = simulate(panel, target, initial_capital=100000.0, fill_at="open",
                        cost_multiplier=1.0, max_participation=0.10,
                        flat_at_close=False, trade_mask=None)
        n_held = len(held["trades"])
        initial_notional = 100000.0 * 0.5 * n_sym      # ~half the book, per side
        round_trips = held["turnover_total"] / (2.0 * initial_notional)
        self.assertGreater(
            round_trips, 5.0,
            f"only {round_trips:.1f} round trips of turnover -- the fixture "
            f"is not producing enough fills for this check to discriminate")
        self.assertLessEqual(
            n_held, n_sym + 2,
            f"{n_held} ledger records for {n_sym} held positions despite "
            f"{round_trips:.1f} round trips of trading -- partial rebalance "
            f"adjustments are being counted as trades")
        # And the lifecycles of one symbol never interleave.
        for sym in panel.symbols:
            iv = sorted((t.get("entry_bar", t["bar"]), t["bar"])
                        for t in held["trades"] if t["symbol"] == sym)
            for i in range(1, len(iv)):
                self.assertGreater(iv[i][0], iv[i - 1][1],
                                   f"{sym}: overlapping trade intervals {iv}")
        print(f"    sessions={n_sessions}  instruments={n_sym}  "
              f"flat_at_close trades={len(trades)} (expected {expected})")
        print(f"    held-position: {round_trips:.1f} round trips of trading "
              f"-> {n_held} ledger records (expected <= {n_sym + 2})")


# --------------------------------------------------------------------------
# TEST 6 -- the 60m fetch URL
# --------------------------------------------------------------------------

class FakeResponse:
    """Minimal stand-in for the object urlopen() returns."""

    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


CHART_PAYLOAD = {
    "chart": {
        "error": None,
        "result": [{
            "meta": {"currency": "USD", "shortName": "TEST"},
            "timestamp": [1700000000, 1700003600],
            "indicators": {"quote": [{
                "open": [100.0, 101.0], "high": [102.0, 103.0],
                "low": [99.0, 100.0], "close": [101.0, 102.0],
                "volume": [1000.0, 1100.0],
            }]},
        }],
    }
}


class Test06DataLayer(unittest.TestCase):
    """The fetch URL and the cache format."""

    def test_6_intraday_fetch_url_uses_range_2y(self):
        captured = []

        def fake_urlopen(req, timeout=None, context=None):
            captured.append(req.full_url)
            return FakeResponse(CHART_PAYLOAD)

        with mock.patch("urllib.request.urlopen", fake_urlopen):
            btengine.fetch_yahoo_chart("SPY", "60m")
            btengine.fetch_yahoo_chart("SPY", "1d")

        self.assertEqual(len(captured), 2)
        intraday_url, daily_url = captured

        # Intraday MUST use an explicit range. Yahoo answers HTTP 422 to
        # period1=0&period2=9999999999 for any intraday interval.
        self.assertIn("range=2y", intraday_url,
                      f"intraday URL must use an explicit range: {intraday_url}")
        self.assertIn("interval=60m", intraday_url)
        self.assertNotIn("period1=", intraday_url,
                         f"intraday URL must not use period1: {intraday_url}")
        self.assertNotIn("9999999999", intraday_url)

        # Daily may use the full range.
        self.assertIn("period1=0", daily_url, daily_url)
        self.assertIn("period2=9999999999", daily_url, daily_url)
        self.assertNotIn("range=", daily_url)
        print(f"    60m : {intraday_url}")
        print(f"    1d  : {daily_url}")

    def test_7_old_date_keyed_cache_is_ignored(self):
        tmp = tempfile.mkdtemp(prefix="btcache_")
        old_dir = btengine.CACHE_DIR
        try:
            btengine.CACHE_DIR = tmp
            path = btengine._cache_path("TESTCACHE", "60m")
            # The OLD format: date-keyed "rows". One key per day cannot
            # represent intraday bars -- it would collapse the whole session
            # into a single bar.
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"symbol": "TESTCACHE", "interval": "60m",
                           "fetched": 0,
                           "rows": {"2024-01-10": [100.0, 101.0, 99.0,
                                                   100.5, 1_000_000.0]}}, f)

            fresh = {"records": [[1700000000, 100.0, 101.0, 99.0, 100.5,
                                  1_000_000.0]], "currency": "USD",
                     "name": "TESTCACHE"}
            calls = []

            def fake_fetch(symbol, interval="1d"):
                calls.append((symbol, interval))
                return fresh

            with mock.patch.object(btengine, "fetch_yahoo_chart", fake_fetch):
                got = btengine.load_or_fetch("TESTCACHE", "60m")
            self.assertEqual(calls, [("TESTCACHE", "60m")],
                             "the stale-format cache must trigger a refetch")
            self.assertIn("records", got)
            self.assertNotIn("rows", got)
            self.assertEqual(got["records"], fresh["records"])

            # And the cache file itself must be rewritten in the new format.
            with open(path, "r", encoding="utf-8") as f:
                rewritten = json.load(f)
            self.assertIn("records", rewritten)
            self.assertNotIn("rows", rewritten)

            # Positive control: a NEW-format cache IS served without a fetch.
            calls2 = []

            def fake_fetch2(symbol, interval="1d"):
                calls2.append(symbol)
                raise AssertionError("a valid cache must not be refetched")

            with mock.patch.object(btengine, "fetch_yahoo_chart", fake_fetch2):
                again = btengine.load_or_fetch("TESTCACHE", "60m")
            self.assertEqual(calls2, [])
            self.assertEqual(again["records"], fresh["records"])
            print(f"    old cache ignored and refetched; new cache served "
                  f"without a fetch")
        finally:
            btengine.CACHE_DIR = old_dir
            shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------------
# FIXED DEFECTS -- now asserted, not tracked.
#
# These were real defects in the session path. They were originally tracked
# as expected failures so they stayed visible; the engine now rejects each
# misconfiguration loudly, so they are asserted directly.
# --------------------------------------------------------------------------

class Test08RejectsMisconfiguration(unittest.TestCase):
    """
    Regression tests for three session-path defects that have since been FIXED.

    These were originally written as @unittest.expectedFailure so the suite
    stayed green while the bugs stayed visible. The engine now rejects each
    misconfiguration loudly instead of silently returning a plausible but
    wrong result, so they are converted into assertions of the FIX. If a
    future change makes one of these misconfigurations run silently again,
    this test fails - which is the whole point.
    """

    def test_8_window_all_is_rejected_not_corrupted(self):
        """FIXED: window='all' now raises instead of corrupting the panel.

        Previously extract_sessions returned only the OPEN of each bar, and
        build_session_panel fed a list of seven prices into Panel, which read
        the first five as (open, high, low, close, volume) - producing a panel
        with a high BELOW its low and a volume that was a price. The data
        audit did not flag it. Now it raises, so the misuse cannot happen
        silently.
        """
        recs = make_records([dt.date(2024, 1, 10)])
        with self.assertRaises(ValueError):
            extract_sessions(recs, window="all")
        # And the supported path still produces a well-formed bar.
        ok = extract_sessions(recs, window="first")
        for day, row in ok.items():
            self.assertEqual(len(row), 5, f"bar must be full OHLCV, got {row!r}")
            self.assertGreaterEqual(row[1], row[2], "high must not be below low")

    def test_9_interval_60m_requires_session_mode(self):
        """FIXED: interval='60m' without mode='session' is now rejected.

        Previously run_spec branched only on execution.mode, so a spec with
        period.interval='60m' and no mode went to build_panel, whose
        records_to_daily keeps the LAST record of each date - the 15:30 ET
        closing bar. The run completed and reported a plausible daily
        backtest of the WRONG window. It now raises.
        """
        payloads = session_payloads(SESSION_DATES, 0.0020)
        s = spec("interval probe", "probe", "single",
                 [{"name": "momentum", "params": {"lookback": 60}}],
                 {"type": "constant", "params": {"weight": 1.0}},
                 {"gross_leverage": 1.0, "rebalance": "daily"},
                 period={"start": None, "end": None, "interval": "60m"})
        s["universe"]["symbols"] = list(SESSION_SYMBOLS)
        raised = None
        with mock.patch.object(btengine, "load_or_fetch",
                               fake_load_or_fetch(payloads)):
            try:
                btengine.run_spec(s, refresh=True, verbose=False)
            except Exception as e:       # noqa: BLE001
                raised = e
        self.assertIsNotNone(
            raised,
            "interval='60m' without execution.mode='session' must be "
            "rejected, not silently run as a daily backtest on the closing bar")
        self.assertIsInstance(raised, ValueError)
        self.assertIn("session", str(raised).lower())

    def test_10_fill_at_close_with_flat_at_close_is_rejected(self):
        """FIXED: fill_at='close' + flat_at_close=True is now rejected.

        flat_at_close liquidates at panel.close[i]. With fill_at='close' the
        position is ENTERED at that same close, so the round trip realises
        0.00 of P&L while paying full round-trip cost every session - a cost
        pump wearing the costume of a strategy. run_spec now raises.
        """
        s = spec("fill probe", "probe", "single",
                 [{"name": "momentum", "params": {"lookback": 60}}],
                 {"type": "constant", "params": {"weight": 1.0}},
                 {"gross_leverage": 1.0, "rebalance": "daily"},
                 period={"start": None, "end": None, "interval": "60m"})
        s["universe"]["symbols"] = list(SESSION_SYMBOLS)
        s["execution"] = {"mode": "session", "window": "first",
                          "fill_at": "close", "flat_at_close": True}
        raised = None
        payloads = session_payloads(SESSION_DATES, 0.0020)
        if hasattr(intraday, "_SESSION_CACHE"):
            intraday._SESSION_CACHE.clear()
        with mock.patch.object(intraday, "load_or_fetch",
                               fake_load_or_fetch(payloads)):
            try:
                btengine.run_spec(s, refresh=True, verbose=False)
            except Exception as e:       # noqa: BLE001
                raised = e
        self.assertIsNotNone(
            raised,
            "fill_at='close' with flat_at_close=True must be rejected: it "
            "enters and exits at the same price and earns exactly 0.00 while "
            "paying full round-trip cost")
        self.assertIsInstance(raised, ValueError)


# --------------------------------------------------------------------------
# Runner -- selftest.py-style output
# --------------------------------------------------------------------------

class StyleResult(unittest.TextTestResult):
    """Prints a per-check PASS/FAIL block in the style of selftest.py."""

    def startTest(self, test):
        super().startTest(test)
        self._label = (test.shortDescription() or test.id())
        print("\n" + "#" * 70)
        print(f"# {test.id().split('.')[-1]}")
        print(f"# {self._label}")
        print("#" * 70)
        sys.stdout.flush()

    def addSuccess(self, test):
        super().addSuccess(test)
        RESULTS.append((test.id().split(".")[-1], "PASS", ""))
        print(f"  [ok  ] {test.shortDescription() or test.id()}")
        sys.stdout.flush()

    def addFailure(self, test, err):
        super().addFailure(test, err)
        detail = self._exc_info_to_string(err, test)
        RESULTS.append((test.id().split(".")[-1], "FAIL", detail))
        print(f"  [FAIL] {test.shortDescription() or test.id()}")
        print(detail)
        sys.stdout.flush()

    def addError(self, test, err):
        super().addError(test, err)
        detail = self._exc_info_to_string(err, test)
        RESULTS.append((test.id().split(".")[-1], "ERROR", detail))
        print(f"  [ERR ] {test.shortDescription() or test.id()}")
        print(detail)
        sys.stdout.flush()

    def addExpectedFailure(self, test, err):
        super().addExpectedFailure(test, err)
        RESULTS.append((test.id().split(".")[-1], "KNOWN-ISSUE", ""))
        print(f"  [BUG ] {test.shortDescription() or test.id()}")
        print("         (known defect in the session path -- reported here, "
              "not fixed by this suite)")
        sys.stdout.flush()

    def addUnexpectedSuccess(self, test):
        super().addUnexpectedSuccess(test)
        RESULTS.append((test.id().split(".")[-1], "FIXED", ""))
        print(f"  [FIXD] {test.shortDescription() or test.id()}")
        print("         -> this known issue now PASSES. The engine has been "
              "fixed; drop the @expectedFailure marker.")
        sys.stdout.flush()

    def printErrors(self):
        """selftest.py prints its own failure detail; suppress the default."""
        return


def main():
    loader = unittest.TestLoader()
    suite = loader.loadTestsFromModule(sys.modules[__name__])
    # Run the suite directly against the styled result object: the stock
    # TextTestRunner appends its own "Ran N tests / OK" block, which would
    # duplicate the summary below.
    result = StyleResult(sys.stdout, True, 0)
    suite.run(result)

    print("\n" + "=" * 70)
    print("INTRADAY / SESSION SELFTEST SUMMARY")
    print("=" * 70)
    for name, verdict, _ in RESULTS:
        mark = {"PASS": "ok  ", "KNOWN-ISSUE": "BUG ", "FIXED": "FIXD"}.get(
            verdict, "FAIL")
        print(f"  [{mark}] {name}: {verdict}")
    failed = [r for r in RESULTS if r[1] in ("FAIL", "ERROR")]
    bugs = [r for r in RESULTS if r[1] == "KNOWN-ISSUE"]
    fixed = [r for r in RESULTS if r[1] == "FIXED"]
    verdict = ("ALL CHECKS PASSED" if not failed else f"{len(failed)} FAILURE(S)")
    print(f"\n  RESULT: {verdict}  ({len(bugs)} known issue(s) tracked)")
    if fixed:
        print("  NOTE: a known issue now passes -- update its marker.")
    print("=" * 70)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
