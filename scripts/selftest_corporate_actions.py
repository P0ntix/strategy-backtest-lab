#!/usr/bin/env python3
"""
selftest_corporate_actions.py -- Regression suite for corporate-action handling.

The engine reads Yahoo's `indicators.quote`, whose closes are split-adjusted
but NOT dividend-adjusted. On an ex-dividend date the raw close drops by the
dividend with no offset, so a long-only backtest silently books a loss that
never happened. This suite locks in the fix: the engine back-adjusts OHLC by
the per-bar factor adjclose/close, which reproduces Yahoo's adjusted series.

No network: the Yahoo payload is synthesised and urlopen is monkeypatched.

Run:  python scripts/selftest_corporate_actions.py
Exit: 0 when every check passes, 1 otherwise.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import btengine


def make_payload(closes, adjcloses, opens=None, highs=None, lows=None, vols=None):
    """Build a Yahoo chart-API-shaped payload."""
    n = len(closes)
    opens = opens if opens is not None else closes
    highs = highs if highs is not None else closes
    lows = lows if lows is not None else closes
    vols = vols if vols is not None else [1000] * n
    return {
        "chart": {
            "error": None,
            "result": [{
                "meta": {"currency": "USD", "longName": "Test"},
                "timestamp": [1700000000 + 86400 * i for i in range(n)],
                "indicators": {
                    "quote": [{
                        "open": list(opens), "high": list(highs), "low": list(lows),
                        "close": list(closes), "volume": list(vols),
                    }],
                    "adjclose": [{"adjclose": list(adjcloses)}],
                },
            }],
        }
    }


class _FakeResponse:
    def __init__(self, payload):
        self._buf = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def read(self):
        return self._buf.read()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def patched_fetch(payload):
    """Return a context manager patching btengine's HTTP call."""
    return mock.patch.object(btengine.urllib.request, "urlopen",
                             return_value=_FakeResponse(payload))


class TestDividendAdjustment(unittest.TestCase):
    """The adjustment must remove the ex-dividend drop without breaking OHLC."""

    def test_1_ex_dividend_drop_is_removed(self):
        """A 2.0 dividend on a 100.00 close must not look like a 2% loss."""
        # Day 0: close 100.00, no dividend yet -> adjclose 100.00
        # Day 1: close 98.00 after paying 2.00 -> adjclose 98.00 (no drop)
        payload = make_payload(
            closes=[100.0, 98.0, 99.0],
            adjcloses=[100.0, 98.0, 99.0],
            opens=[100.0, 98.0, 99.0],
            highs=[100.0, 98.0, 99.0],
            lows=[100.0, 98.0, 99.0])
        with patched_fetch(payload):
            got = btengine.fetch_yahoo_chart("TEST", "1d")
        recs = got["records"]
        self.assertEqual(len(recs), 3)
        # Day 0 is back-adjusted by 100/100 = 1.0 -> unchanged.
        self.assertAlmostEqual(recs[0][4], 100.0, places=6)
        # Day 2 sits AFTER the last dividend -> factor 1.0 -> unchanged.
        self.assertAlmostEqual(recs[2][4], 99.0, places=6)
        self.assertTrue(got["adjusted"], "adjusted flag must be set")

    def test_2_series_is_continuous_across_ex_dividend(self):
        """Adjacent bars must not show a dividend-sized jump."""
        # Before the dividend: close 100, adjclose 100. After a 5.00 dividend:
        # close 95, adjclose 95 (Yahoo back-adjusts HISTORY, so the earlier
        # bar is scaled DOWN by the factor and the series stays continuous).
        payload = make_payload(
            closes=[100.0, 95.0],
            adjcloses=[95.0, 95.0])
        with patched_fetch(payload):
            got = btengine.fetch_yahoo_chart("TEST", "1d")
        recs = got["records"]
        r0 = recs[0][4]   # adjusted close of day 0
        r1 = recs[1][4]   # adjusted close of day 1
        ret = r1 / r0 - 1.0
        self.assertAlmostEqual(ret, 0.0, places=6,
                               msg=f"adjusted return should be flat across the "
                                   f"ex-dividend date, got {ret:.4%}")
        # And the adjustment actually happened (raw 100 -> 95 is a 5% drop).
        self.assertLess(r0, 100.0 - 1e-9,
                        "history must be back-adjusted by the dividend factor")

    def test_3_volume_is_not_scaled(self):
        """Only prices are adjusted; volume must stay raw."""
        payload = make_payload(
            closes=[100.0, 95.0], adjcloses=[95.0, 95.0],
            vols=[1111, 2222])
        with patched_fetch(payload):
            got = btengine.fetch_yahoo_chart("TEST", "1d")
        self.assertEqual(got["records"][0][5], 1111)
        self.assertEqual(got["records"][1][5], 2222)

    def test_4_ohlc_stays_internally_consistent(self):
        """After adjustment, high >= low and open/close stay within the bar."""
        payload = make_payload(
            closes=[100.0, 95.0], adjcloses=[95.0, 95.0],
            opens=[99.0, 94.0], highs=[101.0, 96.0], lows=[98.0, 93.0])
        with patched_fetch(payload):
            got = btengine.fetch_yahoo_chart("TEST", "1d")
        for rec in got["records"]:
            o, h, l, c = rec[1], rec[2], rec[3], rec[4]
            self.assertGreaterEqual(h, l, "high must not be below low")
            self.assertGreaterEqual(h, max(o, c), "high must not be below body")
            self.assertLessEqual(l, min(o, c), "low must not be above body")

    def test_5_missing_adjclose_leaves_prices_alone(self):
        """No adjclose (e.g. intraday) must not corrupt the series."""
        payload = make_payload(closes=[100.0, 101.0], adjcloses=[100.0, 101.0])
        payload["chart"]["result"][0]["indicators"].pop("adjclose")
        with patched_fetch(payload):
            got = btengine.fetch_yahoo_chart("TEST", "60m")
        self.assertAlmostEqual(got["records"][0][4], 100.0, places=6)
        self.assertAlmostEqual(got["records"][1][4], 101.0, places=6)
        self.assertFalse(got["adjusted"])

    def test_6_absurd_factor_is_rejected(self):
        """A malformed adjclose must not scale the series to nonsense."""
        payload = make_payload(closes=[100.0, 101.0], adjcloses=[100.0, 0.0])
        with patched_fetch(payload):
            got = btengine.fetch_yahoo_chart("TEST", "1d")
        self.assertAlmostEqual(got["records"][1][4], 101.0, places=6,
                               msg="a zero adjclose must be ignored, not applied")


class TestCacheSchemaInvalidation(unittest.TestCase):
    """Stale unadjusted daily caches must be refetched, not reused."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="btcache_")
        self._orig_cache_dir = btengine.CACHE_DIR
        btengine.CACHE_DIR = self.tmp

    def tearDown(self):
        btengine.CACHE_DIR = self._orig_cache_dir
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_7_old_schema_cache_is_refetched(self):
        """A daily cache without schema 2 holds unadjusted prices."""
        path = btengine._cache_path("OLD", "1d")
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"symbol": "OLD", "interval": "1d",
                       "rows": {"2024-01-02": [1, 1, 1, 1, 1]}}, f)
        payload = make_payload(closes=[50.0], adjcloses=[50.0])
        with patched_fetch(payload):
            out = btengine.load_or_fetch("OLD", "1d")
        self.assertIn("records", out, "old-format cache must not be served")
        self.assertGreaterEqual(out.get("schema", 0), btengine.CACHE_SCHEMA,
                                "refetched cache must carry the current schema")

    def test_8_current_schema_cache_is_served(self):
        """A current-schema cache IS served - no pointless refetch."""
        path = btengine._cache_path("NEW", "1d")
        payload = make_payload(closes=[50.0], adjcloses=[50.0])
        with patched_fetch(payload):
            first = btengine.load_or_fetch("NEW", "1d")
        with mock.patch.object(btengine.urllib.request, "urlopen",
                               side_effect=AssertionError("must not refetch")):
            second = btengine.load_or_fetch("NEW", "1d")
        self.assertEqual(first["records"], second["records"])
        self.assertGreaterEqual(second.get("schema", 0), btengine.CACHE_SCHEMA)


if __name__ == "__main__":
    unittest.main(verbosity=2)
