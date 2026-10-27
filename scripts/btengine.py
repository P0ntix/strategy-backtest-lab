#!/usr/bin/env python3
"""
btengine.py -- Pure-stdlib backtest engine implementing Stages 1-7 of the
eight-stage backtesting pipeline.

  Stage 1  Data layer ............ fetch_*, Panel, audit
  Stage 2  Universe .............. UNIVERSE_PRESETS, build_panel
  Stage 3  Features .............. FEATURES registry
  Stage 4  Signal logic .......... SIGNALS registry
  Stage 5  Portfolio ............. construct_target_weights
  Stage 6  Execution ............ simulate
  Stage 7  Accounting ........... equity curve, ledger

Stage 8 (metrics, validation, verdict) lives in btvalidate.py.

NO third-party dependencies. No installs. Data cached to CSV so repeat runs
are offline and free.

Causality contract (the single most important property of this file):
    features[t] may only use market data at indices <= t.
    signal[t]   may only use features at indices <= t.
    FILLS OCCUR AT t+1 (never t) and never at a price derived from bar t
    after the bar has closed.
This is enforced structurally by simulate(), and independently verified by
btvalidate.causality_audit().
"""

from __future__ import annotations

import csv
import json
import math
import os
import random
import ssl
import statistics
import sys
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timezone

# --------------------------------------------------------------------------
# 0. Configuration
# --------------------------------------------------------------------------

ENGINE_VERSION = "1.0.0"

# Bumped to 2 when dividend adjustment was added, so stale unadjusted daily
# cache files are refetched instead of silently reused.
CACHE_SCHEMA = 2

PERIODS_PER_YEAR = {"1d": 252, "1wk": 52, "1mo": 12, "60m": 252 * 24}

# Cache lives next to the scripts so it is trivially inspectable and deletable.
CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".cache")
CACHE_DIR = os.path.normpath(CACHE_DIR)

UA = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
}


def _ssl_ctx():
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


# --------------------------------------------------------------------------
# 1. Math / stats helpers
# --------------------------------------------------------------------------


def mean(xs):
    xs = [x for x in xs if x is not None and not _is_nan(x)]
    return sum(xs) / len(xs) if xs else None


def stdev(xs, ddof=1):
    xs = [x for x in xs if x is not None and not _is_nan(x)]
    if len(xs) <= ddof:
        return None
    return statistics.stdev(xs) if len(xs) > 1 else 0.0


def _is_nan(x):
    return isinstance(x, float) and x != x


def safe_div(a, b, default=None):
    try:
        if b in (0, None) or _is_nan(b):
            return default
        return a / b
    except Exception:
        return default


def quantile(sorted_xs, q):
    """Linear-interpolated quantile. sorted_xs must already be sorted."""
    if not sorted_xs:
        return None
    if len(sorted_xs) == 1:
        return sorted_xs[0]
    pos = q * (len(sorted_xs) - 1)
    lo = int(math.floor(pos))
    hi = int(math.ceil(pos))
    if lo == hi:
        return sorted_xs[lo]
    return sorted_xs[lo] + (sorted_xs[hi] - sorted_xs[lo]) * (pos - lo)


def winsorize(xs, lo_q=0.01, hi_q=0.99):
    vals = sorted([x for x in xs if x is not None and not _is_nan(x)])
    if len(vals) < 5:
        return list(xs)
    lo, hi = quantile(vals, lo_q), quantile(vals, hi_q)
    return [None if x is None else (lo if x < lo else (hi if x > hi else x)) for x in xs]


def rolling(xs, lookback, fn, min_periods=None):
    """Rolling window application. Output[i] uses xs[i-lookback+1 .. i]."""
    if min_periods is None:
        min_periods = lookback
    out = [None] * len(xs)
    buf = []
    for i, x in enumerate(xs):
        buf.append(x)
        if len(buf) > lookback:
            buf.pop(0)
        if len(buf) >= min_periods:
            clean = [b for b in buf if b is not None and not _is_nan(b)]
            if len(clean) >= max(1, min_periods // 2):
                out[i] = fn(clean)
    return out


def rolling_std(xs, lookback):
    return rolling(xs, lookback, lambda w: stdev(w, ddof=1))


def pct_change(series, lag):
    out = [None] * len(series)
    for i in range(lag, len(series)):
        prev, cur = series[i - lag], series[i]
        if prev and cur is not None:
            out[i] = cur / prev - 1.0
    return out


def max_drawdown(equity):
    """Return (max_dd_fraction, peak_idx, trough_idx)."""
    peak, maxdd, pi, ti = -float("inf"), 0.0, 0, 0
    cur_peak_i = 0
    for i, v in enumerate(equity):
        if v is None:
            continue
        if v > peak:
            peak, cur_peak_i = v, i
        dd = 1.0 - (v / peak) if peak > 0 else 0.0
        if dd > maxdd:
            maxdd, pi, ti = dd, cur_peak_i, i
    return maxdd, pi, ti


def downside_dev(xs, mar=0.0):
    d = [min(0.0, x - mar) for x in xs if x is not None]
    if not d:
        return None
    return math.sqrt(sum(v * v for v in d) / len(d))


def pearson(xs, ys):
    pairs = [(a, b) for a, b in zip(xs, ys) if a is not None and b is not None
             and not _is_nan(a) and not _is_nan(b)]
    if len(pairs) < 3:
        return None
    ax = [p[0] for p in pairs]
    ay = [p[1] for p in pairs]
    mx, my = mean(ax), mean(ay)
    num = sum((a - mx) * (b - my) for a, b in pairs)
    dx = math.sqrt(sum((a - mx) ** 2 for a in ax))
    dy = math.sqrt(sum((b - my) ** 2 for b in ay))
    return safe_div(num, dx * dy)


# --------------------------------------------------------------------------
# 2. STAGE 1 -- Data layer
# --------------------------------------------------------------------------

def _http(url, timeout=30, retries=3):
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=timeout, context=_ssl_ctx()) as r:
                return r.read().decode("utf-8", "replace")
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.2 * (attempt + 1))
    raise RuntimeError(f"HTTP failed after {retries} tries: {url} -> {type(last).__name__}: {last}")


def yahoo_symbol(sym):
    return urllib.parse.quote(sym, safe="")


def fetch_yahoo_chart(symbol, interval="1d"):
    """Return dict: {records: [[epoch, o,h,l,c,v], ...], currency, name}.

    Records keep the raw epoch timestamp because intraday intervals have
    MULTIPLE bars per calendar date -- keying by date would collapse the
    whole session into one bar.
    """
    iv = {"1d": "1d", "1wk": "1wk", "1mo": "1mo", "60m": "60m"}[interval]
    base = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol(symbol)}"
    if interval == "1d":
        # Daily history goes back to the instrument's inception.
        url = (f"{base}?period1=0&period2=9999999999&interval={iv}"
               f"&includePrePost=false&events=div%2Csplit")
    else:
        # Yahoo REJECTS a full-range request for intraday intervals
        # (HTTP 422). Intraday must use an explicit `range`, and 60m is
        # capped by the provider at roughly 2 years of history.
        url = (f"{base}?range=2y&interval={iv}"
               f"&includePrePost=false&events=div%2Csplit")
    txt = _http(url)
    data = json.loads(txt)
    if data.get("chart", {}).get("error"):
        raise RuntimeError(f"Yahoo error for {symbol}: {data['chart']['error']}")
    res = data["chart"]["result"][0]
    ts = res.get("timestamp") or []
    q = res["indicators"]["quote"][0]
    meta = res.get("meta", {})
    # Dividend adjustment. Yahoo's `quote` closes are split-adjusted but NOT
    # dividend-adjusted; `adjclose` is the total-return series. Reading only
    # `quote` makes every ex-dividend date look like a real loss, which is a
    # genuine bias in any backtest. We back-adjust OHLC by the per-bar factor
    # adjclose/close, which reproduces Yahoo's adjusted series exactly (the
    # factor is a step function and adjclose is continuous), so returns
    # include dividends while the traded prices stay internally consistent.
    # Intraday intervals expose no adjclose, so they remain unadjusted - see
    # the `corporate_actions` capability text.
    adj = ((res.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose") or []
    records = []
    adjusted = False
    keys = ["open", "high", "low", "close", "volume"]
    for i, t in enumerate(ts):
        vals = []
        for k in keys:
            arr = q.get(k) or []
            vals.append(float(arr[i]) if i < len(arr) and arr[i] is not None else None)
        if vals[3] is None:  # need at least a close
            continue
        factor = 1.0
        if i < len(adj) and adj[i] is not None and vals[3]:
            f = float(adj[i]) / vals[3]
            # Guard against a malformed factor (0 or absurd) rather than
            # silently corrupting the whole series.
            if 0.05 < f < 20.0:
                factor = f
                adjusted = True
        if factor != 1.0:
            for k in range(4):  # open, high, low, close - NOT volume
                if vals[k] is not None:
                    vals[k] *= factor
        records.append([int(t)] + vals)
    return {"records": records, "currency": meta.get("currency"),
            "adjusted": adjusted,
            "name": meta.get("longName") or meta.get("shortName") or symbol}


def _cache_path(symbol, interval):
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in symbol)
    return os.path.join(CACHE_DIR, f"{safe}__{interval}.json")


def load_or_fetch(symbol, interval="1d", refresh=False, max_age_days=3):
    """Cached market data. Returns {records: [[epoch,o,h,l,c,v],...], currency, name}."""
    os.makedirs(CACHE_DIR, exist_ok=True)
    path = _cache_path(symbol, interval)
    if os.path.exists(path) and not refresh:
        age_days = (time.time() - os.path.getmtime(path)) / 86400.0
        # Intraday data is stale within a day; daily data within 3.
        max_age = 1.0 if interval != "1d" else max_age_days
        if age_days <= max_age:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    cached = json.load(f)
                # Older cache files stored date-keyed "rows"; those cannot
                # represent intraday bars (one key per day would collapse the
                # whole session) -- refetch instead.
                if "records" not in cached:
                    pass
                # Caches written before dividend adjustment was added hold
                # UNADJUSTED daily prices. They must be refetched, or the
                # backtest silently keeps the ex-dividend bias.
                elif interval == "1d" and cached.get("schema", 1) < CACHE_SCHEMA:
                    pass
                else:
                    return cached
            except Exception:
                pass
    got = fetch_yahoo_chart(symbol, interval)
    payload = {"symbol": symbol, "interval": interval, "fetched": int(time.time()),
               "schema": CACHE_SCHEMA,
               "currency": got.get("currency"), "name": got.get("name"),
               "adjusted": got.get("adjusted", False),
               "records": got["records"]}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f)
    os.replace(tmp, path)
    return payload


def records_to_daily(records):
    """Collapse epoch records into {YYYY-MM-DD: [o,h,l,c,v]} (daily path)."""
    out = {}
    for rec in records:
        d = datetime.fromtimestamp(rec[0], tz=timezone.utc).strftime("%Y-%m-%d")
        out[d] = [rec[1], rec[2], rec[3], rec[4], rec[5]]
    return out


def fetch_batch(symbols, interval="1d", refresh=False, verbose=False):
    """Fetch all symbols, isolating per-symbol failures. Returns date-keyed bars."""
    out, failures = {}, {}
    for s in symbols:
        try:
            payload = load_or_fetch(s, interval, refresh=refresh)
            rows = records_to_daily(payload["records"])
            if not rows:
                failures[s] = "empty series"
            else:
                out[s] = rows
        except Exception as e:  # noqa: BLE001
            failures[s] = f"{type(e).__name__}: {e}"
        if verbose:
            n = len(out.get(s, {}))
            print(f"  [{s}] {n} bars" if n else f"  [{s}] FAILED: {failures.get(s)}", flush=True)
    return out, failures


# --------------------------------------------------------------------------
# 3. STAGE 2 -- Universe + Panel assembly
# --------------------------------------------------------------------------

UNIVERSE_PRESETS = {
    # --- Broad / factor ETFs (survivorship-safe instruments) ---
    "us_equity_broad": ["SPY", "VTI", "IVV", "RSP"],
    "us_equity_sector": ["XLB", "XLC", "XLE", "XLF", "XLI", "XLK", "XLP", "XLRE",
                         "XLU", "XLV", "XLY"],
    "us_equity_style": ["SPY", "IWM", "MDY", "RSP", "QQQ", "DIA"],
    # --- Cross-asset ---
    "futures_broad": ["ES=F", "NQ=F", "YM=F", "RTY=F", "ZN=F", "ZB=F", "ZF=F",
                      "ZT=F", "GC=F", "SI=F", "CL=F", "NG=F", "HG=F", "ZC=F", "ZS=F"],
    "fx_major": ["EURUSD=X", "GBPUSD=X", "USDJPY=X", "AUDUSD=X", "USDCHF=X",
                 "USDCAD=X", "NZDUSD=X", "USDSEK=X"],
    "fx_emerging": ["USDMXN=X", "USDZAR=X", "USDTRY=X", "USDBRL=X", "USDINR=X",
                    "USDSGD=X", "USDKRW=X", "USDPLN=X"],
    "commodities": ["GC=F", "SI=F", "CL=F", "NG=F", "HG=F", "PL=F", "PA=F", "ZC=F",
                    "ZS=F", "ZW=F", "KC=F", "SB=F", "CT=F", "LBS=F"],
    "crypto": ["BTC-USD", "ETH-USD", "LTC-USD", "BCH-USD", "XRP-USD", "SOL-USD",
               "ADA-USD", "DOGE-USD", "AVAX-USD", "LINK-USD"],
    "rates_credit": ["^TNX", "^TYX", "^FVX", "^IRX", "TLT", "IEF", "SHY", "LQD",
                     "HYG", "EMB", "TIP", "BND"],
    "intl_equity": ["EWJ", "EWZ", "EWY", "EWU", "EWA", "EWC", "EWI", "EWD", "EWG",
                    "EWI", "FXI", "INDA", "EWW", "EWQ", "EWN", "EWP", "EWA"],
    "real_assets": ["VNQ", "XLRE", "DBC", "DBA", "TIP", "DBC", "REM", "RWO"],
    # --- Multi-asset combination (cross-class diversification) ---
    "multi_asset": ["SPY", "TLT", "GLD", "DBC", "EFA", "EEM", "VNQ", "BTC-USD"],
    "risk_parity_ish": ["SPY", "TLT", "GLD", "VNQ", "EFA", "EEM"],
    "risk_on_risk_off": ["SPY", "QQQ", "IWM", "TLT", "GLD", "USO", "BTC-USD"],
    "single": [],
}


def infer_asset_class(symbol):
    s = symbol.upper()
    if s.endswith("=X") or "USD" in s and s[:3] in ("EUR", "GBP", "AUD", "NZD", "USD"):
        return "fx"
    if s.endswith("-USD") or s.endswith("-BTC"):
        return "crypto"
    if s.startswith("^") or s in ("ES=F", "NQ=F", "YM=F", "RTY=F", "GC=F", "SI=F",
                                  "CL=F", "NG=F", "HG=F", "ZN=F", "ZB=F", "ZF=F", "ZT=F"):
        return "index_or_future"
    if s.startswith("XL") or s in ("SPY", "VTI", "IVV", "IWM", "MDY", "RSP", "QQQ",
                                   "DIA", "TLT", "IEF", "SHY", "LQD", "HYG", "EMB",
                                   "VNQ", "GLD", "TIP", "BND"):
        return "etf"
    return "etf"


# Realistic cost assumptions per asset class (Stage 6 inputs). These are
# deliberately conservative; users should verify against their own broker.
DEFAULT_COSTS = {
    "etf": {"commission_bps": 0.5, "half_spread_bps": 1.0, "slippage_bps": 1.0, "borrow_bps": 10.0},
    "index_or_future": {"commission_bps": 0.2, "half_spread_bps": 0.5, "slippage_bps": 0.5, "borrow_bps": 5.0},
    "fx": {"commission_bps": 0.0, "half_spread_bps": 0.5, "slippage_bps": 0.5, "borrow_bps": 15.0},
    "crypto": {"commission_bps": 10.0, "half_spread_bps": 5.0, "slippage_bps": 5.0, "borrow_bps": 30.0},
}


class Panel:
    """Aligned OHLCV matrix across symbols on a shared date axis."""

    def __init__(self, dates, symbols, bars, costs=None, meta=None):
        self.dates = dates
        self.symbols = symbols
        self.bars = bars              # {sym: {date: [o,h,l,c,v]}}
        self.index = {d: i for i, d in enumerate(dates)}
        self.n = len(dates)
        self.costs = costs or {}
        self.meta = meta or {}
        # Forward-filled close series, aligned to the master date axis.
        self.close = {}
        self.open = {}
        self.high = {}
        self.low = {}
        self.vol = {}
        # Staleness: True where a bar was FORWARD-FILLED because the symbol
        # had no observation on that date. Trading a forward-filled price means
        # trading a price the market did not print -- a halted or delisted
        # name would trade indefinitely at a phantom price.
        self.stale = {}
        for s in symbols:
            src = bars[s]
            c, o, h, l, v = [], [], [], [], []
            st = []
            pc = po = ph = pl = None
            pv = 0.0
            for d in dates:
                b = src.get(d)
                st.append(b is None)
                if b is not None:
                    po, ph, pl, pc = b[0] if b[0] is not None else pc, b[1] if b[1] is not None else pc, \
                                     b[2] if b[2] is not None else pc, b[3]
                    pv = b[4] if b[4] is not None else 0.0
                c.append(pc); o.append(po); h.append(ph); l.append(pl); v.append(pv)
            self.close[s] = c
            self.open[s] = o
            self.high[s] = h
            self.low[s] = l
            self.vol[s] = v
            self.stale[s] = st
        # Period-over-period RETURNS per symbol. These must be true returns
        # (close/prev_close - 1), NOT price ratios. Storing ratios here silently
        # breaks every consumer that assumes a return: the data audit reads a
        # ratio of 1.009 as a "+101% move", and RSI treats every bar as a gain
        # (all ratios > 1) so it pins at 100.
        self.rets = {s: [None] + [safe_div(self.close[s][i], self.close[s][i - 1])
                                 for i in range(1, self.n)] for s in symbols}
        for s in symbols:
            self.rets[s] = [None if r is None else r - 1.0 for r in self.rets[s]]

    def slice(self, start, end):
        """Return a new Panel over dates in [start, end] inclusive."""
        ds = [d for d in self.dates if start <= d <= end]
        return Panel(ds, self.symbols, self.bars, self.costs, self.meta)

    def adv(self, symbol, lookback=63):
        """Average daily dollar volume."""
        return rolling([(self.close[symbol][i] or 0) * (self.vol[symbol][i] or 0)
                        for i in range(self.n)], lookback, mean)


def audit_panel(panel):
    """Stage 1 integrity audit. Returns a list of {symbol, issue, severity}."""
    issues = []
    for s in panel.symbols:
        rows = panel.bars[s]
        if len(rows) < 120:
            issues.append({"symbol": s, "issue": f"only {len(rows)} bars — too short", "severity": "fatal"})
        dates_sorted = sorted(rows)
        # calendar gaps
        big_gaps = 0
        for a, b in zip(dates_sorted, dates_sorted[1:]):
            try:
                da = datetime.strptime(a, "%Y-%m-%d").date()
                db = datetime.strptime(b, "%Y-%m-%d").date()
                if (db - da).days > 12:
                    big_gaps += 1
            except Exception:
                pass
        if big_gaps:
            issues.append({"symbol": s, "issue": f"{big_gaps} calendar gap(s) > 12d (possible halt/delisting)",
                           "severity": "warn"})
        # non-positive prices
        bad = sum(1 for b in rows.values() if b[3] is not None and b[3] <= 0)
        if bad:
            issues.append({"symbol": s, "issue": f"{bad} non-positive close bars", "severity": "fatal"})
        # zero volume fraction
        zv = sum(1 for b in rows.values() if not b[4])
        if rows and zv / len(rows) > 0.30:
            issues.append({"symbol": s, "issue": f"{zv/len(rows):.0%} zero-volume bars (illiquid/halted?)",
                           "severity": "warn"})
        # stale prices (flat close runs)
        cs = [b[3] for _, b in sorted(rows.items()) if b[3] is not None]
        run = best = 1
        for i in range(1, len(cs)):
            run = run + 1 if cs[i] == cs[i - 1] else 1
            best = max(best, run)
        if best >= 8:
            issues.append({"symbol": s, "issue": f"flat close for {best} consecutive bars (stale feed?)",
                           "severity": "warn"})
        # extreme single-day move
        rets = panel.rets[s]
        moves = [abs(r) for r in rets if r is not None]
        if moves and max(moves) > 0.55:
            issues.append({"symbol": s, "issue": f"extreme single-bar move {max(moves):.0%} (data check)",
                           "severity": "warn"})
    return issues


_PANEL_CACHE = {}


def build_panel(symbols, interval="1d", start=None, end=None, refresh=False,
                min_adv_usd=0.0, adv_lookback=63, verbose=False):
    """
    Stage 2: fetch + align + optionally apply liquidity screen.
    start/end are ISO date strings. Returns (panel, failures, audit, screened_out).

    Panels are memoised per process — the validation gauntlet rebuilds the same
    panel for every cost-stress and parameter-sweep variant, and refetching
    dominates runtime otherwise. Pass refresh=True to force a refetch.
    """
    key = (tuple(sorted(symbols)), interval, start, end, round(min_adv_usd, 2), refresh)
    if not refresh and key in _PANEL_CACHE:
        return _PANEL_CACHE[key]

    rows, failures = fetch_batch(symbols, interval, refresh=refresh, verbose=verbose)
    if not rows:
        return None, failures, [], {}
    # Master date axis = union of all dates, then intersect to the window.
    alld = sorted({d for s in rows for d in rows[s]})
    if start:
        alld = [d for d in alld if d >= start]
    if end:
        alld = [d for d in alld if d <= end]
    # Drop dates with too little coverage so a single short-lived symbol cannot
    # shrink the whole axis. For a 1-symbol universe the floor must be 1, not 2 —
    # otherwise every date is discarded and the panel comes back empty.
    min_coverage = min(len(symbols), max(1, int(0.5 * len(symbols))))
    kept = []
    for d in alld:
        cover = sum(1 for s in rows if d in rows[s])
        if cover >= min_coverage:
            kept.append(d)
    if len(kept) < 120:
        raise RuntimeError(f"Panel too short after alignment: {len(kept)} dates")
    bars = {}
    for s in rows:
        # forward-fill onto master axis
        sd = {}
        last = None
        for d in kept:
            b = rows[s].get(d)
            if b is not None:
                last = b
            if last is not None:
                sd[d] = last
        bars[s] = sd
    panel = Panel(kept, sorted(bars), bars,
                  costs={s: DEFAULT_COSTS.get(infer_asset_class(s), DEFAULT_COSTS["etf"]) for s in bars})
    audit = audit_panel(panel)

    # Liquidity screen
    screened_out = {}
    if min_adv_usd > 0:
        for s in list(panel.symbols):
            adv_series = panel.adv(s, adv_lookback)
            adv_series = [a for a in adv_series if a is not None]
            med = quantile(sorted(adv_series), 0.5) if adv_series else 0
            if (med or 0) < min_adv_usd:
                screened_out[s] = med or 0
    result = (panel, failures, audit, screened_out)
    if not refresh:
        _PANEL_CACHE[key] = result
    return result


# --------------------------------------------------------------------------
# 4. STAGE 3 -- Feature registry
#
# Every feature takes (panel, **params) and returns a list of per-bar values
# aligned to panel.dates. Contract: value at index i uses data at indices <= i.
# --------------------------------------------------------------------------

def _close_series(panel, sym):
    return panel.close[sym]


def f_return(panel, sym, lag=1, **kw):
    return pct_change(_close_series(panel, sym), lag)


def f_momentum(panel, sym, lookback=252, skip=0, **kw):
    """Time-series momentum over [i-skip-lookback, i-skip]."""
    s = _close_series(panel, sym)
    out = [None] * len(s)
    total = lookback + skip
    for i in range(total, len(s)):
        base = s[i - total]
        if not base:
            continue
        end = s[i - skip] if skip else s[i]
        if end is None:
            continue
        out[i] = end / base - 1.0
    return out


def _div_sub(a, b, sub=0.0):
    """(a / b) - sub, returning None if either input is missing. Guard against
    the warm-up window where a rolling stat is still None."""
    if a is None or b is None or _is_nan(a) or _is_nan(b) or b == 0:
        return None
    return a / b - sub


def f_sma_gap(panel, sym, lookback=50, **kw):
    s = _close_series(panel, sym)
    sma = rolling(s, lookback, mean)
    return [_div_sub(c, m, 1.0) for c, m in zip(s, sma)]


def f_volatility(panel, sym, lookback=20, annualize=True, **kw):
    rets = panel.rets[sym]
    vol = rolling(rets, lookback, lambda w: stdev(w, ddof=1))
    if annualize:
        sc = math.sqrt(252)
        vol = [None if v is None else v * sc for v in vol]
    return vol


def f_realized_vol_ratio(panel, sym, short=10, long=60, **kw):
    vs = f_volatility(panel, sym, short, annualize=False)
    vl = f_volatility(panel, sym, long, annualize=False)
    return [safe_div(s, l) for s, l in zip(vs, vl)]


def f_zscore(panel, sym, lookback=60, **kw):
    s = _close_series(panel, sym)
    m = rolling(s, lookback, mean)
    sd = rolling(s, lookback, lambda w: stdev(w, ddof=1))
    return [safe_div(c - mm, sdd) if (c is not None and mm is not None and sdd) else None
            for c, mm, sdd in zip(s, m, sd)]


def f_rsi(panel, sym, lookback=14, **kw):
    rets = panel.rets[sym]

    def _rsi(window):
        gains = [max(0.0, r) for r in window]
        losses = [max(0.0, -r) for r in window]
        ag, al = mean(gains), mean(losses)
        if ag is None or al is None:
            return None
        if al == 0:
            return 100.0 if ag > 0 else 50.0
        rs = ag / al
        return 100.0 - (100.0 / (1.0 + rs))

    return rolling(rets, lookback + 1, _rsi, min_periods=lookback + 1)


def f_max_drawdown(panel, sym, lookback=60, **kw):
    s = _close_series(panel, sym)
    out = [None] * len(s)
    for i in range(lookback, len(s)):
        w = [x for x in s[i - lookback + 1:i + 1] if x]
        if len(w) >= lookback // 2:
            peak = -float("inf")
            dd = 0.0
            for v in w:
                peak = max(peak, v)
                if peak > 0:
                    dd = max(dd, 1 - v / peak)
            out[i] = -dd
    return out


def f_skew(panel, sym, lookback=60, **kw):
    def _sk(w):
        m = mean(w)
        sd = stdev(w)
        if not m or not sd:
            return None
        return sum(((x - m) / sd) ** 3 for x in w) / len(w)
    return rolling(panel.rets[sym], lookback, _sk)


def f_volume_ratio(panel, sym, short=10, long=60, **kw):
    vs = rolling(panel.vol[sym], short, mean)
    vl = rolling(panel.vol[sym], long, mean)
    return [safe_div(s, l) for s, l in zip(vs, vl)]


def f_bollinger_pct(panel, sym, lookback=20, nstd=2.0, **kw):
    s = _close_series(panel, sym)
    m = rolling(s, lookback, mean)
    sd = rolling(s, lookback, lambda w: stdev(w, ddof=1))
    out = [None] * len(s)
    for i in range(len(s)):
        if s[i] is not None and m[i] is not None and sd[i]:
            out[i] = (s[i] - m[i]) / (nstd * sd[i])
    return out


def f_atr_pct(panel, sym, lookback=14, **kw):
    n = panel.n
    tr = [None] * n
    for i in range(1, n):
        h, l, pc = panel.high[sym][i], panel.low[sym][i], panel.close[sym][i - 1]
        if None in (h, l, pc):
            continue
        tr[i] = max(h - l, abs(h - pc), abs(l - pc))
    atr = rolling(tr, lookback, mean)
    return [safe_div(a, c) if a is not None else None for a, c in zip(atr, panel.close[sym])]


def f_ma_crossover(panel, sym, fast=20, slow=100, **kw):
    s = _close_series(panel, sym)
    f_ = rolling(s, fast, mean)
    sl = rolling(s, slow, mean)
    return [1.0 if (a is not None and b is not None and a > b) else 0.0 for a, b in zip(f_, sl)]


def f_distance_to_high(panel, sym, lookback=252, **kw):
    s = _close_series(panel, sym)
    out = [None] * len(s)
    for i in range(lookback, len(s)):
        w = [x for x in s[i - lookback + 1:i + 1] if x]
        if w:
            hi = max(w)
            out[i] = _div_sub(s[i], hi, 1.0)
    return out


FEATURES = {
    "return": f_return,
    "momentum": f_momentum,
    "sma_gap": f_sma_gap,
    "volatility": f_volatility,
    "vol_ratio": f_realized_vol_ratio,
    "zscore": f_zscore,
    "rsi": f_rsi,
    "max_drawdown": f_max_drawdown,
    "skew": f_skew,
    "volume_ratio": f_volume_ratio,
    "bollinger_pct": f_bollinger_pct,
    "atr_pct": f_atr_pct,
    "ma_crossover": f_ma_crossover,
    "dist_to_high": f_distance_to_high,
}


def register_features(mapping):
    """Extend the feature registry (used by the intraday module)."""
    FEATURES.update(mapping)
    return sorted(FEATURES)


def compute_features(panel, feature_specs, winsorize_cs=True):
    """
    feature_specs: list of {"name": <str>, "params": {...}}.
    Returns {name: {symbol: [values]}}. If the feature is cross-sectional
    (cross_sectional=True), values are winsorized per date.
    """
    out = {}
    for spec in feature_specs:
        fname = spec["name"]
        if fname not in FEATURES:
            raise KeyError(f"Unknown feature '{fname}'. Available: {sorted(FEATURES)}")
        params = {k: v for k, v in spec.get("params", {}).items()
                  if k not in ("cross_sectional", "direction")}
        direction = spec.get("params", {}).get("direction", 1)
        by_sym = {}
        for s in panel.symbols:
            vals = FEATURES[fname](panel, s, **params)
            if direction not in (1, -1):
                raise ValueError("direction must be 1 or -1")
            by_sym[s] = [None if v is None else v * direction for v in vals]
        out[fname] = by_sym
    return out


# --------------------------------------------------------------------------
# 5. STAGE 4 -- Signal logic
#
# A signal takes features and produces a TARGET WEIGHT per symbol per date.
# -1 = short one unit of notional, 0 = flat, +1 = long one unit, or fractional
# (e.g. 0.5 = half unit). Values may be any float in [-1, 1] pre-normalisation.
# --------------------------------------------------------------------------

def _cross_sectional_rank_by_date(values_by_symbol, n_dates):
    """values_by_symbol: {sym: [vals]}. Returns {sym: [pct_rank in 0..1]}."""
    ranks = {s: [None] * n_dates for s in values_by_symbol}
    for d in range(n_dates):
        vals = [(s, values_by_symbol[s][d]) for s in values_by_symbol
                if values_by_symbol[s][d] is not None]
        if len(vals) < 2:
            continue
        vals.sort(key=lambda kv: kv[1])
        for idx, (s, _) in enumerate(vals):
            ranks[s][d] = idx / (len(vals) - 1)
    return ranks


def _cross_sectional_strength(panel, feature_values):
    """
    Conviction score per symbol per bar for a cross-sectional signal: how
    EXTREME the name's cross-sectional percentile rank is. 1.0 = the most
    extreme name on that date, 0.5 = the median. The raw cross-sectional
    signal is +/-1 for every long and short, so it cannot rank conviction on
    its own; without this, `max_names` kept the alphabetically first names.
    """
    ranks = _cross_sectional_rank_by_date(feature_values, panel.n)
    return {s: [(max(r, 1.0 - r) if r is not None else None) for r in ranks[s]]
            for s in panel.symbols}


def sig_cross_sectional(panel, features, long_rank=0.8, short_rank=0.2, **kw):
    """
    Classic cross-sectional: long the top (1-long_rank) by the primary feature,
    short the bottom short_rank. Requires multiple symbols.
    """
    fname = kw["feature"]
    ranks = _cross_sectional_rank_by_date(features[fname], panel.n)
    sig = {}
    for s in panel.symbols:
        r = ranks[s]
        row = []
        for x in r:
            if x is None:
                row.append(0.0)
            elif x >= long_rank:
                row.append(1.0)
            elif x <= short_rank:
                row.append(-1.0)
            else:
                row.append(0.0)
        sig[s] = row
    return sig


def sig_time_series_threshold(panel, features, feature=None, threshold=0.0,
                              long_when_gt=True, exit_only=False, **kw):
    """Long (or short) when the feature crosses a threshold, flat otherwise."""
    vals = features[feature]
    sig = {}
    for s in panel.symbols:
        v = vals[s]
        row = []
        for x in v:
            if x is None:
                row.append(0.0)
            elif long_when_gt:
                row.append(1.0 if x > threshold else 0.0)
            else:
                row.append(-1.0 if x > threshold else 0.0)
        sig[s] = row
    return sig


def sig_time_series_zscore(panel, features, feature=None, entry=1.5, exit=0.5,
                           direction=1, **kw):
    """Mean-reversion: enter when |z| > entry, hold until |z| < exit."""
    vals = features[feature]
    sig = {}
    for s in panel.symbols:
        v = vals[s]
        row, pos = [], 0.0
        for x in v:
            if x is None:
                row.append(0.0); continue
            z = x * direction
            if pos == 0.0:
                if z > entry:
                    pos = -1.0  # reversion: fade a high z with a short
                elif z < -entry:
                    pos = 1.0
            else:
                if (pos > 0 and z > -exit) or (pos < 0 and z < exit):
                    pos = 0.0
            row.append(pos)
        sig[s] = row
    return sig


def sig_crossover(panel, features, feature=None, **kw):
    vals = features[feature]
    sig = {s: [(0.0 if x is None else float(x)) for x in vals[s]] for s in panel.symbols}
    return sig


def sig_constant(panel, features, weight=1.0, **kw):
    return {s: [weight] * panel.n for s in panel.symbols}


SIGNALS = {
    "cross_sectional": sig_cross_sectional,
    "time_series_threshold": sig_time_series_threshold,
    "time_series_zscore": sig_time_series_zscore,
    "crossover": sig_crossover,
    "constant": sig_constant,
}


def compute_signal(panel, features, signal_spec):
    stype = signal_spec["type"]
    if stype not in SIGNALS:
        raise KeyError(f"Unknown signal type '{stype}'. Available: {sorted(SIGNALS)}")
    params = dict(signal_spec.get("params", {}))
    feature = params.pop("feature", None)
    if stype != "constant" and not feature:
        # Default to the only feature available; explicit `feature` is required
        # only when the spec declares several.
        if len(features) == 1:
            feature = next(iter(features))
        else:
            raise ValueError(
                f"Signal '{stype}' needs params.feature — the spec declares "
                f"{sorted(features)}")
    fn = SIGNALS[stype]
    if stype == "cross_sectional":
        # A cross-sectional signal ranks instruments AGAINST EACH OTHER, so it
        # needs at least two with a usable value. With one instrument it
        # silently produced a completely flat backtest - zero trades, zero
        # risk, and a verdict that read as "no edge" rather than "this
        # configuration cannot be run".
        usable = [s for s in panel.symbols
                  if any(v is not None for v in features[feature].get(s, []))]
        if len(usable) < 2:
            raise ValueError(
                f"The cross_sectional signal needs at least TWO instruments to "
                f"rank against each other, but only {len(usable)} has a usable "
                f"value for feature '{feature}' (universe: "
                f"{list(panel.symbols)}). A single-instrument cross-sectional "
                f"strategy is always flat. Use a time-series signal "
                f"(time_series_threshold / time_series_zscore / crossover) for "
                f"a one-instrument universe, or widen the universe.")
    sig = fn(panel, features, feature=feature, **params)
    return sig


# --------------------------------------------------------------------------
# 6. STAGE 5 -- Portfolio construction
# --------------------------------------------------------------------------

def construct_target_weights(panel, signal, gross_leverage=1.0, max_position=None,
                            vol_target=None, vol_lookback=60, rebalance="daily",
                            max_names=None, net_target=None, long_only=False,
                            strength=None):
    """
    Convert per-symbol signals to target weights. Applies caps and optional
    inverse-vol scaling. Rebalance controls how often weights are refreshed.

    `strength` is an optional {symbol: [value]} series used ONLY to rank
    which names survive `max_names`. Without it, ranking falls back to the
    signal's own magnitude.

    MEMBERSHIP IS DECIDED PER BAR. It used to be decided ONCE for the whole
    sample, by asking "does this symbol EVER signal?" - which is look-ahead:
    a symbol that only produced a signal on the FINAL bar of the sample
    entered the universe from bar 0, and the surviving names were simply
    the first `max_names` in alphabetical order, not the strongest.
    """
    n = panel.n
    rebal_idx = set()
    if rebalance == "daily":
        rebal_idx = set(range(n))
    else:
        # Same single-bar-per-period rule as rebalance_mask, so the weights are
        # refreshed on exactly the bars that are allowed to trade.
        for i, bi in enumerate(rebalance_mask(panel, rebalance)):
            if bi:
                rebal_idx.add(i)

    target = {s: [0.0] * n for s in panel.symbols}
    cur = {s: 0.0 for s in panel.symbols}
    # Precompute the volatility series ONCE per symbol. Recomputing it inside
    # the per-bar loop made construct_target_weights O(n^2) in rolling-window
    # work, which dominated the runtime of every validation variant.
    vol_series_cache = {}
    if vol_target:
        for s in panel.symbols:
            vol_series_cache[s] = f_volatility(panel, s, vol_lookback, annualize=True)
    for i in range(n):
        if i in rebal_idx:
            # 0) Decide WHICH names are in the book, using ONLY information
            #    available at this bar. A name that is not signalling at this
            #    bar is not a candidate, and a name that stops signalling
            #    leaves the book on the next rebalance.
            cand = []
            for s in panel.symbols:
                v = signal[s][i]
                if v is None or v == 0:
                    continue
                if long_only and v < 0:
                    continue
                st = None
                if strength is not None and s in strength:
                    sv = strength[s][i]
                    st = abs(sv) if sv is not None else None
                if st is None:
                    st = abs(v)
                cand.append((-st, s))
            if max_names and len(cand) > max_names:
                # Strongest first. Ties break on symbol name so the result is
                # deterministic and reproducible, never on dictionary order.
                cand.sort()
                cand = cand[:max_names]
            active = {s for _, s in cand}
            # 1) raw signal, optionally scaled by inverse volatility.
            raw = {}
            for s in active:
                sig = signal[s][i]
                if long_only and sig < 0:
                    sig = 0.0
                w = sig
                if vol_target:
                    v = vol_series_cache[s][i]
                    if v and v > 1e-6:
                        # Relative inverse-vol basis. After normalisation to the
                        # target gross below, the LEVEL of vol_target cancels --
                        # only the relative inverse-vol scaling survives.
                        w = w * (1.0 / v)
                raw[s] = w
            # 2) normalise to target gross.
            gross = sum(abs(v) for v in raw.values())
            if gross > 0:
                scale = gross_leverage / gross
                for s in panel.symbols:
                    cur[s] = raw.get(s, 0.0) * scale
            else:
                for s in panel.symbols:
                    cur[s] = 0.0
            # 2b) VOLATILITY TARGETING. The LEVEL of vol_target used to be
            #     discarded: the weights were scaled by inverse volatility and
            #     then normalised to gross, which cancels the level entirely,
            #     so vol_target=0.05 and vol_target=0.50 produced identical
            #     books despite the spec promising "size to X% annual vol".
            #     Here the book is scaled so the PORTFOLIO's estimated annual
            #     volatility equals the target.
            #
            #     Portfolio vol is estimated with a DIAGONAL covariance
            #     (correlations assumed zero). For a genuinely diversified
            #     multi-asset book that OVERSTATES portfolio volatility, so
            #     the realised vol lands BELOW the target. Treat the target
            #     as an upper bound on realised vol, not an exact setting.
            if vol_target and vol_target > 0:
                pvol = 0.0
                for s in panel.symbols:
                    v = vol_series_cache.get(s, [None] * n)[i]
                    if v is None or v <= 1e-9:
                        continue
                    pvol += (cur[s] * v) ** 2
                pvol = math.sqrt(pvol)
                if pvol > 1e-9:
                    k = vol_target / pvol
                    for s in panel.symbols:
                        cur[s] *= k
            # 3) apply net_target: shift the book so its NET exposure equals
            #    the target.
            #
            #    This is done with an OFFSET (add the same constant to every
            #    weight), not a uniform SCALE. Scaling multiplies every weight
            #    by one factor, so it cannot reach a target of the opposite
            #    sign without INVERTING the strategy, it cannot neutralise a
            #    one-sided book, and it is a no-op when the book is already
            #    net-flat. The old formula did all three wrong: it divided by
            #    zero at net == 1.0, flipped a long book short at net == 2.0,
            #    and silently skipped a book whose net was already 0.
            #
            #    The offset preserves the book's relative structure, so a
            #    long/short pair stays market-neutral in its spread, and it
            #    leaves gross untouched for a balanced book.
            if net_target is not None and panel.symbols:
                net = sum(cur.values())
                offset = (net_target - net) / len(panel.symbols)
                for s in panel.symbols:
                    cur[s] += offset
            # 4) apply the concentration cap LAST, as a HARD limit. A cap
            #    applied before the gross rescale or the net_target adjustment
            #    is silently undone by it. If the cap binds, realised gross is
            #    necessarily BELOW gross_leverage - report it rather than
            #    pretending the target was met.
            if max_position is not None:
                cap = abs(max_position)
                for s in panel.symbols:
                    cur[s] = max(-cap, min(cap, cur[s]))
        for s in panel.symbols:
            target[s][i] = cur[s]
    return target


# --------------------------------------------------------------------------
# 7. STAGE 6+7 -- Execution simulation + accounting
#
# The causality-critical section. At bar i we:
#   1. mark positions at open[i]
#   2. execute trades decided at bar i-1, filled at open[i] (or close[i],
#      configurable). If the open gapped through a stop, fill at the open.
#   3. mark positions at close[i] -> daily equity
# --------------------------------------------------------------------------

VALID_REBALANCE = ("daily", "weekly", "monthly", "quarterly", "yearly")


def rebalance_mask(panel, rebalance="daily"):
    """
    Which bars may GENERATE ORDERS. Between rebalances no order exists at all
    and the previous target is simply held.

    Exactly ONE bar per period is marked (the first available bar), so
    "monthly" means 12 rebalances/year, not "the first week of each month".

    CRITICAL: this mask gates ORDER GENERATION, not order FILLING. An order
    generated at bar i fills at bar i+1 even though bar i+1 is not itself a
    rebalance bar. Gating the fill instead makes every rebalance execute one
    full period late, because the order sits pending until the next marked
    bar.

    An unknown rebalance string raises. It used to fall through to a
    one-bar mask, which produced a flat backtest and a false "no edge"
    verdict from nothing but a typo.
    """
    if rebalance not in VALID_REBALANCE:
        raise ValueError(
            f"Unknown rebalance frequency {rebalance!r}. "
            f"Expected one of {VALID_REBALANCE}.")
    mask = [False] * panel.n
    if rebalance == "daily":
        return [True] * panel.n

    def period_key(d):
        y, m, day = d[0:4], d[5:7], d[8:10]
        try:
            dt = datetime.strptime(d, "%Y-%m-%d").date()
        except Exception:
            return None
        if rebalance == "weekly":
            # ISO week: the first trading bar of each week
            iso = dt.isocalendar()
            return ("w", iso[0], iso[1])
        if rebalance == "monthly":
            return ("m", y, m)
        if rebalance == "quarterly":
            return ("q", y, (int(m) - 1) // 3)
        if rebalance == "yearly":
            return ("y", y)
        return None

    seen = set()
    for i, d in enumerate(panel.dates):
        k = period_key(d)
        if k is not None and k not in seen:
            seen.add(k)
            mask[i] = True
    if not any(mask):
        mask[0] = True
    return mask


def simulate(panel, target_weights, initial_capital=100000.0,
             fill_at="open", cost_multiplier=1.0, max_participation=0.10,
             min_trade_frac=0.0005, trade_mask=None, flat_at_close=False,
             verbose=False):
    """
    Returns a results dict with the equity curve, returns, ledger, and trades.
    fill_at: "open" (default, causal) or "close" (also causal: decided at i-1).
    min_trade_frac: orders smaller than this fraction of equity are skipped, so
        rebalance dust doesn't inflate the trade count / expectancy stats.
    trade_mask: bars allowed to send orders (see rebalance_mask). None = daily.
    flat_at_close: close any position at the CLOSE of the bar it was opened
        in. Models an intraday strategy that is flat by the close, so there is
        no overnight exposure. The position is therefore opened and closed
        within the same bar, and its full open->close P&L is realised there.
    """
    n = panel.n
    symbols = panel.symbols
    cash = initial_capital
    shares = {s: 0.0 for s in symbols}
    # Round-trip trade bookkeeping (weighted-average cost basis per position)
    avg_cost = {s: None for s in symbols}
    open_pnl = {s: 0.0 for s in symbols}
    open_bar = {s: None for s in symbols}
    # Cost dollars currently attributed to the open position, so they can be
    # netted out of that trade's P&L when it closes.
    pos_entry_cost = {s: 0.0 for s in symbols}
    # Accumulate across the WHOLE run, not per bar.
    turnover_total = 0.0
    cost_total = 0.0
    # Orders dropped because no trailing volume was available to size them.
    liquidity_skips = 0
    # Orders dropped because the bar was stale (forward-filled price).
    stale_skips = 0
    # TRAILING average dollar volume, computed from bars STRICTLY BEFORE each
    # bar. Used to size the liquidity cap without look-ahead: an order filled
    # at bar i's open cannot know bar i's volume, only what came before.
    ADV_WINDOW = 20
    adv_series = {}
    # An instrument with NO volume history at all (FX pairs and most OTC
    # instruments have no consolidated screen volume) is a DIFFERENT case
    # from an instrument that has volume but did not trade on this bar. The
    # participation cap simply does not apply to the former - there is no
    # screen volume to participate in - while for the latter a missing
    # volume means the order genuinely cannot be sized and must not be sent.
    no_volume_data = set()
    for s in symbols:
        _dv = [((panel.close[s][j] or 0) * (panel.vol[s][j] or 0))
               for j in range(n)]
        if not any(v > 0 for v in _dv):
            no_volume_data.add(s)
        _a = [0.0] * n
        for j in range(n):
            _lo = max(0, j - ADV_WINDOW)
            _w = _dv[_lo:j]                 # strictly before j
            _a[j] = (sum(_w) / len(_w)) if _w else 0.0
        adv_series[s] = _a

    equity_curve = [None] * n
    daily_rets = [None] * n
    gross_exposure_hist = [0.0] * n
    trades = []
    pending = {s: 0.0 for s in symbols}  # target weight decided at bar i-1, executed at i
    order_pending = False               # True while an order is awaiting its fill

    for i in range(n):
        # --- Mark at open
        mark_open = cash + sum(shares[s] * (panel.open[s][i] or panel.close[s][i] or 0)
                               for s in symbols)
        if mark_open <= 0:
            # Bankruptcy: stop trading, mark dead.
            for k in range(i, n):
                equity_curve[k] = 0.0
                daily_rets[k] = -1.0 if k == i else 0.0
            break

        # --- Execute pending orders (decided at bar i-1) at THIS bar.
        # The rebalance mask gates ORDER GENERATION (see below), NOT this
        # fill. An order generated at a rebalance bar fills on the very next
        # bar even though that bar is not itself a rebalance bar. Gating the
        # fill here instead left every order pending until the NEXT rebalance
        # bar, so a monthly strategy executed each rebalance a full month
        # late.
        order_live = order_pending
        px_basis = panel.open if fill_at == "open" else panel.close
        for s in symbols:
            if not order_live:
                break
            tgt_w = pending.get(s, 0.0)
            px = px_basis[s][i] or panel.close[s][i]
            if px is None or px <= 0:
                continue
            # NO FILL ON A STALE BAR. If the symbol printed no observation on
            # this date the price is forward-filled from an earlier session --
            # a price the market did not print. Trading it lets a halted or
            # delisted name trade indefinitely at a phantom price. The mark
            # still uses the last known price (that is how you value a
            # position you cannot trade), but no order is filled.
            if panel.stale.get(s, [False] * n)[i]:
                stale_skips += 1
                continue
            tgt_notional = tgt_w * mark_open
            cur_notional = shares[s] * px
            delta_notional = tgt_notional - cur_notional
            # Liquidity cap: cannot trade more than max_participation of the
            # TRAILING dollar volume in one bar.
            #
            # The window must be strictly BEFORE this bar. An order decided at
            # bar i-1 fills at bar i's OPEN, so bar i's own volume is not
            # knowable at that moment -- using it to size the order is
            # intra-bar look-ahead, and a thin bar would artificially tighten
            # the cap while a fat bar would loosen it.
            #
            # Because the first fill can only happen at bar 1 (an order is
            # generated at bar i-1), there is always at least one prior bar to
            # measure against.
            adv = adv_series.get(s, [0.0] * n)[i]
            if adv <= 0 and s not in no_volume_data:
                # The instrument HAS volume history but there is none to size
                # this order against on this bar -- it did not trade, or the
                # trailing window is empty. A trade you cannot size against
                # volume is not a trade. Counted and surfaced, never silent.
                liquidity_skips += 1
                continue
            # For an instrument with no screen volume at all (FX, most OTC
            # instruments) the participation cap does not apply: there is no
            # consolidated volume to participate in. The order is still sized
            # and costed normally.
            cap = adv * max_participation if adv > 0 else 0.0
            if cap > 0 and abs(delta_notional) > cap:
                delta_notional = cap if delta_notional > 0 else -cap
            if abs(delta_notional) < 1e-9:
                continue
            # Skip rebalance dust: no broker sends a $1 order, and counting
            # these would inflate trade count and wreck expectancy stats.
            if abs(delta_notional) < mark_open * min_trade_frac:
                continue
            units = delta_notional / px
            # Cost
            c = panel.costs.get(s, DEFAULT_COSTS["etf"])
            cost_rate = (c["commission_bps"] + c["half_spread_bps"] + c["slippage_bps"]) / 1e4
            if delta_notional < 0:
                # borrow cost charged on short notional over the holding period (charged daily below)
                pass
            cost = abs(delta_notional) * cost_rate * cost_multiplier
            prev_shares = shares[s]
            shares[s] += units
            cash -= delta_notional + cost
            turnover_total += abs(delta_notional)
            cost_total += cost

            # --- Round-trip trade accounting (weighted-average cost basis).
            # Only a position that returns to FLAT emits a trade. Counting each
            # partial rebalance adjustment as a "trade" would inflate the trade
            # count by orders of magnitude and measure every slice against the
            # original entry price, producing trade stats that contradict the
            # equity curve. One trade = one complete position lifecycle.
            #
            # The ledger is NET of costs. Every cost dollar is attributed to the
            # position that caused it and netted out when that position closes,
            # so profit factor, expectancy and win rate are on the same basis
            # as Sharpe and CAGR. Previously costs were debited from cash only,
            # which left the trade ledger GROSS while the equity curve was net
            # - the report printed a gross profit factor beside a net Sharpe.
            units_traded = abs(units)
            prev_abs = abs(prev_shares)
            closed_qty = 0.0
            if prev_abs > 1e-9 and (prev_shares > 0) != (units > 0):
                closed_qty = min(units_traded, prev_abs)
            opened_qty = units_traded - closed_qty

            # Split this fill's cost between the part that CLOSES an existing
            # position and the part that OPENS new exposure.
            if units_traded > 1e-12:
                cost_to_closed = cost * (closed_qty / units_traded)
                cost_to_opened = cost - cost_to_closed
            else:
                cost_to_closed = cost_to_opened = 0.0

            realized = 0.0
            if prev_shares > 1e-9 and units < -1e-9:        # reduce a long
                realized = (px - avg_cost[s]) * closed_qty
            elif prev_shares < -1e-9 and units > 1e-9:      # cover a short
                realized = (avg_cost[s] - px) * closed_qty
            if closed_qty > 0 and prev_abs > 1e-9:
                # Net out the entry cost carried by the units being closed and
                # the exit cost of this fill.
                entry_cost_closed = pos_entry_cost[s] * (closed_qty / prev_abs)
                pos_entry_cost[s] -= entry_cost_closed
                open_pnl[s] += realized - entry_cost_closed - cost_to_closed
            if opened_qty > 0:
                pos_entry_cost[s] += cost_to_opened
            if open_bar[s] is None and abs(shares[s]) > 1e-9:
                open_bar[s] = i
            # Update weighted-average cost for increases.
            if abs(shares[s]) > 1e-9:
                if prev_shares * units > 0 and prev_shares != 0 and avg_cost[s] is not None:
                    # same direction: blend weighted-average cost
                    tot = abs(prev_shares) + abs(units)
                    avg_cost[s] = (avg_cost[s] * abs(prev_shares) + px * abs(units)) / tot
                elif avg_cost[s] is None or prev_shares == 0:
                    avg_cost[s] = px
                elif (prev_shares > 0) != (shares[s] > 0):
                    avg_cost[s] = px          # flipped through flat
            else:
                avg_cost[s] = None
            # Emit the trade when the position closes out.
            if abs(shares[s]) <= 1e-9 and open_bar[s] is not None:
                trades.append({"bar": i, "date": panel.dates[i], "symbol": s,
                               "pnl": open_pnl[s],
                               "bars_held": i - open_bar[s],
                               "entry_bar": open_bar[s]})
                open_pnl[s] = 0.0
                open_bar[s] = None
                avg_cost[s] = None
                pos_entry_cost[s] = 0.0

        # --- Daily borrow cost on shorts (annualized bps on notional)
        for s in symbols:
            if shares[s] < 0:
                c = panel.costs.get(s, DEFAULT_COSTS["etf"])
                borrow_daily = c["borrow_bps"] / 1e4 / 252.0 * cost_multiplier
                cst = abs(shares[s]) * (px_basis[s][i] or panel.close[s][i] or 0) * borrow_daily
                cash -= cst
                cost_total += cst
                # Attribute the borrow to the open position so it nets out of
                # that trade's P&L rather than silently inflating it.
                pos_entry_cost[s] += cst

        # --- Mark at close
        eq_close = cash + sum(shares[s] * (panel.close[s][i] or 0) for s in symbols)

        # --- Flat by the close: liquidate everything at this bar's close.
        # Models an intraday strategy that never holds overnight. Exit costs
        # are charged, because getting out costs money too.
        if flat_at_close:
            for s in symbols:
                if abs(shares[s]) > 1e-9:
                    px = panel.close[s][i]
                    if px is None or px <= 0:
                        continue
                    notional = shares[s] * px
                    c = panel.costs.get(s, DEFAULT_COSTS["etf"])
                    cost_rate = (c["commission_bps"] + c["half_spread_bps"]
                                  + c["slippage_bps"]) / 1e4
                    exit_cost = abs(notional) * cost_rate * cost_multiplier
                    realized = 0.0
                    if shares[s] > 0:
                        realized = (px - (avg_cost[s] or px)) * shares[s]
                    else:
                        realized = ((avg_cost[s] or px) - px) * (-shares[s])
                    # Net out the entry costs and borrow carried by this
                    # position, AND this exit's own cost, so the trade's P&L
                    # is on the same basis as the equity curve. The exit cost
                    # is charged to cash immediately below; it is not part of
                    # the normal fill path, so it has to be netted here
                    # explicitly or the trade overstates its own profit.
                    realized -= pos_entry_cost[s] + exit_cost
                    cash += notional - exit_cost
                    cost_total += exit_cost
                    turnover_total += abs(notional)
                    open_pnl[s] += realized
                    if open_bar[s] is not None:
                        trades.append({"bar": i, "date": panel.dates[i], "symbol": s,
                                       "pnl": open_pnl[s],
                                       "bars_held": i - open_bar[s],
                                       "entry_bar": open_bar[s],
                                       "flat_at_close": True})
                    open_pnl[s] = 0.0
                    open_bar[s] = None
                    avg_cost[s] = None
                    pos_entry_cost[s] = 0.0
                    shares[s] = 0.0
            eq_close = cash

        equity_curve[i] = eq_close
        prev_eq = equity_curve[i - 1] if i > 0 and equity_curve[i - 1] else initial_capital
        daily_rets[i] = safe_div(eq_close - prev_eq, prev_eq, 0.0)
        gross_exposure_hist[i] = safe_div(sum(abs(shares[s] * (panel.close[s][i] or 0))
                                               for s in symbols), eq_close, 0.0)

        # --- Decide the target for the NEXT bar (causal: fills at i+1).
        # An ORDER IS GENERATED only on a rebalance bar. Between rebalances no
        # order exists, so the previous target is simply held.
        if trade_mask is None or trade_mask[i]:
            for s in symbols:
                pending[s] = target_weights[s][i]
            order_pending = True
        else:
            order_pending = False

        # Force-close any position still open at the end of the sample so the
        # final equity and the trade ledger agree. Without this, an open
        # position's unrealised P&L sits in equity but never in any trade
        # record, which silently inflates profit factor and expectancy.
        if i == n - 1:
            for s in symbols:
                if abs(shares[s]) > 1e-9 and open_bar[s] is not None:
                    last_px = panel.close[s][i] or 0
                    if shares[s] > 0:
                        realized = (last_px - (avg_cost[s] or last_px)) * shares[s]
                    else:
                        realized = ((avg_cost[s] or last_px) - last_px) * (-shares[s])
                    # Net out any entry costs and borrow still carried.
                    realized -= pos_entry_cost[s]
                    trades.append({"bar": i, "date": panel.dates[i], "symbol": s,
                                   "pnl": open_pnl[s] + realized,
                                   "bars_held": i - open_bar[s],
                                   "entry_bar": open_bar[s],
                                   "forced_close": True})
                    open_pnl[s] = 0.0
                    open_bar[s] = None
                    pos_entry_cost[s] = 0.0

    return {
        "equity": equity_curve,
        "returns": daily_rets,
        "trades": trades,
        "trade_pnl_basis": "net",
        "liquidity_skips": liquidity_skips,
        "symbols_without_volume": sorted(no_volume_data),
        "stale_skips": stale_skips,
        "turnover_total": turnover_total,
        "costs_paid": cost_total,
        "gross_exposure": gross_exposure_hist,
        "final_equity": equity_curve[-1] if equity_curve and equity_curve[-1] is not None else 0.0,
    }


# --------------------------------------------------------------------------
# 8. Convenience: run a full strategy spec end to end
# --------------------------------------------------------------------------

DEFAULT_SPEC = {
    "name": "untitled",
    "hypothesis": "",
    "universe": {"preset": "us_equity_broad", "symbols": []},
    "period": {"start": "2012-01-01", "end": None, "interval": "1d"},
    "screen": {"min_adv_usd": 0},
    "features": [{"name": "momentum", "params": {"lookback": 252, "skip": 21}}],
    "signal": {"type": "cross_sectional", "params": {"long_rank": 0.75, "short_rank": 0.25}},
    "portfolio": {"gross_leverage": 1.0, "max_position": None, "vol_target": None,
                  "vol_lookback": 60, "rebalance": "monthly", "max_names": None,
                  "net_target": None, "long_only": False},
    "execution": {"fill_at": "open", "max_participation": 0.10, "cost_multiplier": 1.0},
    "capital": 100000.0,
}


def apply_defaults(spec):
    """
    Merge DEFAULT_SPEC under a user spec, one level deep per sub-dict.

    The CLI's load_spec() used to do this while the library's run_spec()
    did not, so the same strategy could get DIFFERENT VERDICTS depending on
    whether it was run from the command line or from Python. Both entry
    points now go through this function, so they cannot disagree.
    """
    merged = json.loads(json.dumps(DEFAULT_SPEC))
    for k, v in (spec or {}).items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k] = {**merged[k], **v}
        else:
            merged[k] = v
    return merged


def resolve_signal_spec(spec):
    """
    Resolve which feature the signal trades on.

    A spec may omit `signal.params.feature` when it declares exactly one
    feature; that feature is then the traded one. Both the main run and the
    walk-forward fold re-simulation MUST resolve this identically, or the
    folds trade a different feature than the base run. Centralised here so
    the two paths cannot drift apart.
    """
    sig = json.loads(json.dumps(spec.get("signal") or {"type": "constant"}))
    sig.setdefault("params", {})
    feats = spec.get("features") or []
    if sig.get("type") != "constant" and not sig["params"].get("feature"):
        if len(feats) == 1:
            sig["params"]["feature"] = feats[0]["name"]
        elif feats:
            # Multiple features: the first is the default traded one unless the
            # spec says otherwise.
            sig["params"]["feature"] = feats[0]["name"]
    return sig


def run_spec(spec, refresh=False, verbose=True):
    """Build panel, features, signal, weights, simulate. Returns a results bundle."""
    # Apply the engine defaults here too, so the library API and the CLI
    # evaluate an identical specification. Previously the CLI merged
    # DEFAULT_SPEC and the library did not, and the two produced different
    # verdicts for the same strategy.
    spec = apply_defaults(spec)
    uni = spec.get("universe", {})
    symbols = list(uni.get("symbols") or [])
    if not symbols:
        preset = uni.get("preset", "us_equity_broad")
        symbols = UNIVERSE_PRESETS.get(preset, [])
        if not symbols:
            raise ValueError(f"Unknown/empty universe preset '{preset}'")
    per = spec.get("period", {})
    interval = per.get("interval", "1d")
    exec_spec = spec.get("execution", {})
    session_mode = exec_spec.get("mode") == "session"

    # --- Fail loudly on configurations that would silently produce a WRONG
    # result rather than an obviously broken one.
    if interval != "1d" and not session_mode:
        raise ValueError(
            f"period.interval='{interval}' requires execution.mode='session'. "
            "Without it the daily data path runs and collapses the intraday "
            "bars onto the LAST bar of each day (the 15:30 ET close), which "
            "silently returns a plausible-looking daily backtest of the "
            "WRONG window. Set execution.mode='session' (and "
            "execution.window='first' for the 09:30-10:00 ET opening window), "
            "or use period.interval='1d' for a daily strategy.")
    if session_mode and exec_spec.get("window", "first") != "first":
        raise ValueError(
            f"execution.window={exec_spec.get('window')!r} is not supported. "
            "The session panel is built as ONE bar per session (the opening "
            "window), so only window='first' is meaningful. A multi-bar "
            "session panel would need a different date keying and a "
            "different annualisation, and is not implemented.")
    if session_mode and exec_spec.get("fill_at", "open") == "close" and \
            exec_spec.get("flat_at_close", False):
        raise ValueError(
            "execution.fill_at='close' with execution.flat_at_close=true "
            "enters and exits at the SAME price, so every session earns "
            "exactly 0.00 while still paying full round-trip cost. Use "
            "fill_at='open' with flat_at_close=true (enter at the opening "
            "window's open, exit at its close), or drop flat_at_close.")

    if session_mode:
        # Intraday path: 60m bars collapsed to one opening-window bar per
        # session. Registers the session_* features first.
        try:
            from intraday import INTRADAY_FEATURES, build_session_panel
        except ImportError:
            from .intraday import INTRADAY_FEATURES, build_session_panel
        register_features(INTRADAY_FEATURES)
        if verbose:
            print(f"[stage 1-2] fetching {len(symbols)} symbols at 60m "
                  f"(session opening window)...")
        panel, failures, audit, info = build_session_panel(
            symbols, start=per.get("start"), end=per.get("end"),
            refresh=refresh, verbose=verbose,
            window=exec_spec.get("window", "first"))
        if panel is None:
            return {"error": "session data fetch failed for all symbols",
                    "failures": failures}
    else:
        info = {}
        screened = {}
        if verbose:
            print(f"[stage 1-2] fetching {len(symbols)} symbols for "
                  f"{uni.get('preset', 'custom')}...")
        panel, failures, audit, screened = build_panel(
            symbols, interval=interval, start=per.get("start"), end=per.get("end"),
            refresh=refresh, min_adv_usd=spec.get("screen", {}).get("min_adv_usd", 0),
            verbose=verbose)
        if panel is None:
            return {"error": "data fetch failed for all symbols", "failures": failures}
        if screened:
            if verbose:
                print(f"  liquidity screen removed: {list(screened)}")
            keep = [s for s in panel.symbols if s not in screened]
            if len(keep) >= 2:
                panel = Panel(panel.dates, keep, panel.bars, panel.costs, panel.meta)
    fatal = [a for a in audit if a["severity"] == "fatal"]
    if fatal and verbose:
        print("  FATAL data issues:", fatal)

    if verbose:
        print(f"[stage 3] computing {len(spec['features'])} feature(s)...")
    features = compute_features(panel, spec["features"])
    if verbose:
        print(f"[stage 4] signal: {spec['signal']['type']}")
    # Default the signal's traded feature to the first declared feature, so a
    # spec only needs `feature` explicitly when it trades on a later one.
    sig_spec = json.loads(json.dumps(spec["signal"]))
    sig_spec.setdefault("params", {})
    if sig_spec["type"] != "constant" and not sig_spec["params"].get("feature"):
        sig_spec["params"]["feature"] = spec["features"][0]["name"]
    signal = compute_signal(panel, features, sig_spec)
    if verbose:
        print("[stage 5] constructing target weights...")
    # For a cross-sectional signal the signal values are +/-1, so they cannot
    # rank conviction. Pass the cross-sectional RANK as the strength series so
    # `max_names` keeps the most extreme names rather than the alphabetically
    # first ones.
    _strength = None
    if sig_spec["type"] == "cross_sectional":
        _fname = sig_spec["params"].get("feature")
        if _fname and _fname in features:
            _strength = _cross_sectional_strength(panel, features[_fname])
    target = construct_target_weights(panel, signal,
                                      strength=_strength,
                                      **spec.get("portfolio", {}))
    if verbose:
        print("[stage 6-7] simulating with costs...")
    res = simulate(panel, target,
                   initial_capital=spec.get("capital", 100000.0),
                   fill_at=exec_spec.get("fill_at", "open"),
                   cost_multiplier=exec_spec.get("cost_multiplier", 1.0),
                   max_participation=exec_spec.get("max_participation", 0.10),
                   flat_at_close=exec_spec.get("flat_at_close", False),
                   trade_mask=rebalance_mask(panel, spec.get("portfolio", {}).get("rebalance", "daily")))

    return {
        "spec": spec,
        "panel": panel,
        "features": features,
        "signal": signal,
        "target_weights": target,
        "sim": res,
        "audit": audit,
        "failures": failures,
        "screened_out": {} if session_mode else screened,
        "session_info": info,
        "symbols_used": panel.symbols,
    }


def panel_to_csv(panel, path):
    """Persist the assembled panel for reproducibility."""
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        cols = []
        for s in panel.symbols:
            cols += [f"{s}_open", f"{s}_high", f"{s}_low", f"{s}_close", f"{s}_volume"]
        w.writerow(["date"] + cols)
        for i, d in enumerate(panel.dates):
            row = [d]
            for s in panel.symbols:
                row += [panel.open[s][i], panel.high[s][i], panel.low[s][i],
                        panel.close[s][i], panel.vol[s][i]]
            w.writerow(row)
    return path


if __name__ == "__main__":
    print(f"btengine {ENGINE_VERSION} -- import this module; run backtests via btvalidate.py")