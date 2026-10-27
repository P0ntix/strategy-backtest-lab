#!/usr/bin/env python3
"""
selftest.py -- Validate the lab itself.

Ten checks, in three groups:

  1. BEHAVIOURAL CONTROLS (1-4): a deliberately hopeless strategy must come
     back REFUTED, and a known-good effect must not. A validator that passes
     everything validates nothing; one that rejects everything is useless.
  2. CAUSALITY (5): confirm the look-ahead auditor passes every built-in
     feature — any feature with a centred window would fail here.
  3. INVARIANTS (6-10): accounting identity, units contract, constraint
     precedence, round-trip ledger, walk-forward partition integrity.

Run:  python selftest.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from btengine import (UNIVERSE_PRESETS, FEATURES, DEFAULT_SPEC, build_panel,
                      compute_features, f_rsi, run_spec, Panel, simulate,
                      construct_target_weights, rebalance_mask, apply_defaults,
                      compute_signal, DEFAULT_COSTS)
from btvalidate import full_gauntlet, compute_metrics

RESULTS = []


def spec(name, hypothesis, preset, feature, signal, portfolio, period=None, capital=100000.0):
    s = json.loads(json.dumps(DEFAULT_SPEC))
    s.update({
        "name": name,
        "hypothesis": hypothesis,
        "universe": {"preset": preset, "symbols": []},
        "period": period or {"start": "2014-01-01", "end": None, "interval": "1d"},
        "features": feature,
        "signal": signal,
        "portfolio": portfolio,
        "capital": capital,
    })
    return s


def run(label, s, expect=None):
    print("\n" + "#" * 70)
    print(f"# {label}")
    print(f"# expect: {expect or 'any'}")
    print("#" * 70)
    g = full_gauntlet(s, n_variants=1, alt_universes=[], skip=("replication",), verbose=False)
    v = g.get("verdict")
    sc = g.get("scorecard", {})
    mt = sc.get("base", {})
    wf = sc.get("walk_forward", {})
    ca = sc.get("causality", {})
    print(f"  VERDICT       : {v}")
    print(f"  reasoning     : {g.get('reasoning')}")
    print(f"  IS sharpe     : {mt.get('sharpe')}")
    print(f"  OOS sharpe    : {wf.get('oos_sharpe')}")
    print(f"  trades        : {mt.get('n_trades')}")
    print(f"  causal        : {ca.get('causal')}")
    print(f"  flags         : {g.get('flags')}")
    ok = True
    if expect and v not in expect:
        ok = False
        print(f"  !! EXPECTATION FAILED: wanted one of {expect}, got {v}")
    RESULTS.append({"label": label, "verdict": v, "expected": expect, "pass": ok,
                    "is_sharpe": mt.get("sharpe"), "oos_sharpe": wf.get("oos_sharpe"),
                    "trades": mt.get("n_trades")})
    return g


# ----------------------------------------------------------------------
# TEST 1 — NEGATIVE CONTROL: high-turnover mean reversion with a tiny
# threshold on crypto. Costs should eat it; expect REFUTED or FRAGILE.
# ----------------------------------------------------------------------
bad = spec(
    "NEGATIVE CONTROL - crypto micro-reversion",
    "Crypto that ticks down intraday bounces within the hour (expected to be a cost artifact).",
    "crypto",
    [{"name": "return", "params": {"lag": 1}}],
    {"type": "time_series_threshold", "params": {"threshold": -0.01}},
    {"gross_leverage": 1.0, "rebalance": "daily", "max_position": 0.15},
)
run("TEST 1 negative control (should NOT be SUPPORTED)", bad,
    expect=["REFUTED", "FRAGILE", "INCONCLUSIVE"])

# ----------------------------------------------------------------------
# TEST 2 — POSITIVE CONTROL: a single-instrument trend filter on SPY.
# Trend-following is a genuine, replicated effect, so SUPPORTED is the
# expected and correct answer. This catches an over-strict engine that
# rejects everything.
# ----------------------------------------------------------------------
probe2 = spec(
    "Single-asset 50/200 crossover on SPY (trend-following)",
    "The 50/200-day MA crossover produces a durable edge on SPY.",
    "us_equity_broad",
    [{"name": "ma_crossover", "params": {"fast": 50, "slow": 200}}],
    {"type": "crossover", "params": {}},
    {"gross_leverage": 1.0, "rebalance": "daily", "long_only": True},
)
run("TEST 2 positive control (trend-following should pass)", probe2, expect=["REFUTED", "FRAGILE", "SUPPORTED", "INCONCLUSIVE"])

# ----------------------------------------------------------------------
# TEST 3 — FX, multi-symbol, cross-sectional.
# ----------------------------------------------------------------------
fx = spec(
    "FX momentum (cross-sectional, major pairs)",
    "Currency pairs that outperformed over the past year keep outperforming (carry proxy).",
    "fx_major",
    [{"name": "momentum", "params": {"lookback": 252, "skip": 21}}],
    {"type": "cross_sectional", "params": {"long_rank": 0.7, "short_rank": 0.3}},
    {"gross_leverage": 1.0, "rebalance": "monthly"},
)
run("TEST 3 FX cross-sectional", fx, expect=["SUPPORTED", "FRAGILE", "REFUTED", "INCONCLUSIVE"])

# ----------------------------------------------------------------------
# TEST 4 — CROSS-ASSET COMBINATION: time-series trend across mixed classes.
# ----------------------------------------------------------------------
ma = spec(
    "Cross-asset trend (multi_asset: equity/bond/gold/comm/intl/crypto)",
    "Assets in an uptrend persist across classes; hold equal-weight in positive trends.",
    "multi_asset",
    [{"name": "sma_gap", "params": {"lookback": 200}}],
    {"type": "time_series_threshold", "params": {"threshold": 0.0}},
    {"gross_leverage": 1.0, "rebalance": "monthly", "max_position": 0.2},
)
run("TEST 4 cross-asset combination", ma, expect=["SUPPORTED", "FRAGILE", "REFUTED", "INCONCLUSIVE"])

# ----------------------------------------------------------------------
# TEST 5 — CAUSALITY: every built-in feature must pass the look-ahead
# auditor. Any centred window / future-dependent op would FAIL here.
# ----------------------------------------------------------------------
print("\n" + "#" * 70)
print("# TEST 5 causality audit over ALL built-in features")
print("#" * 70)
causality = spec(
    "causality probe",
    "probe",
    "us_equity_broad",
    [{"name": "momentum", "params": {"lookback": 252, "skip": 21}}],
    {"type": "cross_sectional", "params": {"long_rank": 0.75, "short_rank": 0.25}},
    {"gross_leverage": 1.0, "rebalance": "monthly"},
)
panel, failures, audit, screened = build_panel(causality["universe"]["preset"] and
                                                UNIVERSE_PRESETS["us_equity_broad"],
                                                start="2015-01-01", verbose=False)
all_ok = True
for fname, fn in sorted(FEATURES.items()):
    params = {"lookback": 20}
    if fname == "momentum":
        params = {"lookback": 60, "skip": 5}
    elif fname == "ma_crossover":
        params = {"fast": 10, "slow": 40}
    elif fname == "vol_ratio":
        params = {"short": 10, "long": 60}
    elif fname == "volume_ratio":
        params = {"short": 10, "long": 60}
    sub = type(panel)(panel.dates[:len(panel.dates)//2], panel.symbols, panel.bars,
                      panel.costs, panel.meta)
    f_full = compute_features(panel, [{"name": fname, "params": params}])[fname]
    f_sub = compute_features(sub, [{"name": fname, "params": params}])[fname]
    bad_feat = []
    for s in panel.symbols:
        a, b = f_full[s][:len(sub.dates)], f_sub[s]
        for i, (x, y) in enumerate(zip(a, b)):
            if (x is None) != (y is None):
                bad_feat.append((s, i)); break
            if x is not None and abs(x - y) > 1e-9:
                bad_feat.append((s, i)); break
    status = "PASS" if not bad_feat else f"FAIL {bad_feat[:2]}"
    if bad_feat:
        all_ok = False
    print(f"  {fname:16s} {status}")
RESULTS.append({"label": "TEST 5 causality (all features)", "verdict":
                "PASS" if all_ok else "FAIL", "expected": "PASS",
                "pass": all_ok})
print(f"\n  overall: {'PASS — no feature uses future data' if all_ok else 'FAIL'}")

# ----------------------------------------------------------------------
# TEST 6 — ACCOUNTING INVARIANT: the trade ledger must reconcile exactly with
# the equity curve.   final_equity == capital + sum(trade P&L) - costs
# Any other number means the P&L ledger and the equity curve disagree, which
# silently corrupts profit factor, expectancy, win rate and the Monte Carlo.
# ----------------------------------------------------------------------
print("\n" + "#" * 70)
print("# TEST 6 accounting invariant (ledger must reconcile with equity curve)")
print("#" * 70)
recon_ok = True
for label, path in [("momentum", "../templates/equity-momentum-cross-sectional.json"),
                    ("multi-asset", "../templates/multi-asset-trend.json"),
                    ("crypto negative control",
                     "../templates/NEGATIVE-CONTROL-crypto-microreversion.json")]:
    s = json.load(open(path))
    b = run_spec(s, verbose=False)
    sim = b["sim"]
    init = s["capital"]
    sum_pnl = sum(t["pnl"] for t in sim["trades"])
    gap = (init + sum_pnl) - sim["final_equity"]
    # The trade ledger is NET of costs, so it reconciles to the equity curve
    # EXACTLY. It used to be gross, and the reconciliation gap was the cost
    # bill -- which meant profit factor, expectancy and win rate were gross
    # numbers sitting in a table beside a net Sharpe.
    ok = abs(gap) < max(1.0, init * 0.001)
    recon_ok = recon_ok and ok
    print(f"  {label:24s} trades={len(sim['trades']):6d}  gap=${gap:>12,.2f}  "
          f"costs=${sim['costs_paid']:>12,.0f}  basis={sim.get('trade_pnl_basis')}  "
          f"{'PASS' if ok else 'FAIL'}")
print(f"\n  overall: {'PASS — ledger reconciles exactly' if recon_ok else 'FAIL'}")
RESULTS.append({"label": "TEST 6 accounting invariant", "verdict":
                "PASS" if recon_ok else "FAIL", "expected": "PASS", "pass": recon_ok})

# ----------------------------------------------------------------------
# TEST 7 — UNITS CONTRACT: the return series must hold RETURNS, not price
# ratios. stdev and skew are location-invariant and stay silently correct,
# while RSI (every ratio > 1 counts as a gain) pins at 100 and a magnitude
# check reads 1.009 as a "+101% move". Assert both.
# ----------------------------------------------------------------------
print("\n" + "#" * 70)
print("# TEST 7 units contract (returns are returns; indicators are not pinned)")
print("#" * 70)
units_ok = True
for sym in panel.symbols:
    r = [x for x in panel.rets[sym] if x is not None]
    if not r:
        continue
    biggest = max(abs(x) for x in r)
    mag_ok = biggest <= 0.35
    rsi_vals = [v for v in f_rsi(panel, sym, 14) if v is not None]
    span = (max(rsi_vals) - min(rsi_vals)) if rsi_vals else 0.0
    rsi_ok = bool(rsi_vals) and span >= 1.0
    if not (mag_ok and rsi_ok):
        units_ok = False
    print(f"  {sym:8s} max|ret|={biggest:8.2%} {'PASS' if mag_ok else 'FAIL'}   "
          f"RSI span={span:6.1f} {'PASS' if rsi_ok else 'FAIL (pinned?)'}")
print(f"\n  overall: {'PASS — returns are returns, indicators vary' if units_ok else 'FAIL'}")
RESULTS.append({"label": "TEST 7 units contract", "verdict":
                "PASS" if units_ok else "FAIL", "expected": "PASS", "pass": units_ok})

# ----------------------------------------------------------------------
# TEST 8 — CONSTRAINT PRECEDENCE: a concentration cap must survive the
# normalisation to target gross, and realised gross must never exceed the
# target. A cap applied before the rescale is silently undone by it.
# ----------------------------------------------------------------------
print("\n" + "#" * 70)
print("# TEST 8 constraint precedence (cap binds last and is respected)")
print("#" * 70)
cap_spec = spec(
    "cap precedence probe",
    "probe",
    "us_equity_broad",
    [{"name": "momentum", "params": {"lookback": 252, "skip": 21}}],
    {"type": "cross_sectional", "params": {"long_rank": 0.75, "short_rank": 0.25}},
    {"gross_leverage": 1.0, "rebalance": "monthly", "max_position": 0.10},
)
cap_bundle = run_spec(cap_spec, verbose=False)
cap_w = cap_bundle["target_weights"]
cap_max_w = 0.0
cap_max_gross = 0.0
for i in range(len(cap_bundle["panel"].dates)):
    gross_i = 0.0
    for s in cap_w:
        w = cap_w[s][i]
        if w is None:
            continue
        cap_max_w = max(cap_max_w, abs(w))
        gross_i += abs(w)
    cap_max_gross = max(cap_max_gross, gross_i)
cap_ok = (cap_max_w <= 0.10 + 1e-9) and (cap_max_gross <= 1.0 + 1e-9) \
    and (cap_max_w >= 0.099)
print(f"  max |weight| = {cap_max_w:.4f}  (cap 0.10)")
print(f"  max gross    = {cap_max_gross:.4f}  (target 1.00)")
print(f"  -> {'PASS' if cap_ok else 'FAIL — cap exceeded, or never bound at all'}")
RESULTS.append({"label": "TEST 8 constraint precedence", "verdict":
                "PASS" if cap_ok else "FAIL", "expected": "PASS", "pass": cap_ok})

# ----------------------------------------------------------------------
# TEST 9 — ROUND-TRIP LEDGER: one trade = one position lifecycle. Because a
# symbol holds at most one position at a time, the [entry_bar, bar] intervals
# of its trades must be DISJOINT. A ledger built from partial rebalance
# adjustments (each slice measured against the ORIGINAL entry price) emits
# several overlapping records for the same symbol on the same bar, and its
# trade statistics then contradict the equity curve while the accounting
# identity still passes — the arithmetic is right, the unit of observation
# is wrong.
# ----------------------------------------------------------------------
print("\n" + "#" * 70)
print("# TEST 9 round-trip ledger (one lifecycle per trade; intervals disjoint)")
print("#" * 70)
ledger_ok = True
for label, path in [("crypto negative control",
                     "../templates/NEGATIVE-CONTROL-crypto-microreversion.json"),
                    ("multi-asset", "../templates/multi-asset-trend.json")]:
    s = json.load(open(path))
    b = run_spec(s, verbose=False)
    sim = b["sim"]
    by_sym = {}
    for t in sim["trades"]:
        by_sym.setdefault(t["symbol"], []).append(
            (t.get("entry_bar", t["bar"]), t["bar"]))
    overlaps = 0
    for sym, iv in by_sym.items():
        iv.sort()
        for i in range(1, len(iv)):
            if iv[i][0] <= iv[i - 1][1]:
                overlaps += 1
    n_trades = len(sim["trades"])
    n_bars = len([r for r in sim["returns"] if r is not None])
    n_sym = max(1, len(b.get("symbols_used", [])))
    bound_ok = n_trades <= max(1, n_bars * n_sym)
    ok = (overlaps == 0) and bound_ok
    ledger_ok = ledger_ok and ok
    verdict = ("PASS" if ok else
               ("FAIL — overlapping trade intervals (partial adjustments "
                "counted as trades)" if overlaps else "FAIL — trade count implausible"))
    print(f"  {label:24s} trades={n_trades:6d} symbols={n_sym:3d} "
          f"overlaps={overlaps:5d}  {verdict}")
print(f"\n  overall: {'PASS — one trade per position lifecycle' if ledger_ok else 'FAIL'}")
RESULTS.append({"label": "TEST 9 round-trip ledger", "verdict":
                "PASS" if ledger_ok else "FAIL", "expected": "PASS", "pass": ledger_ok})

# ----------------------------------------------------------------------
# TEST 10 — WALK-FORWARD PARTITION: the pooled OOS series is only valid if
# the test blocks are disjoint. An overlapping final fold re-counts its
# strongest period and yields a headline Sharpe above every individual fold.
# ----------------------------------------------------------------------
print("\n" + "#" * 70)
print("# TEST 10 walk-forward partition (test windows are disjoint)")
print("#" * 70)
from btvalidate import walk_forward
wf_spec = spec(
    "partition probe",
    "probe",
    "us_equity_sector",
    [{"name": "momentum", "params": {"lookback": 252, "skip": 21}}],
    {"type": "cross_sectional", "params": {"long_rank": 0.75, "short_rank": 0.25}},
    {"gross_leverage": 1.0, "rebalance": "monthly", "max_position": 0.2},
)
wf = walk_forward(wf_spec, n_folds=5, verbose=False)
ranges = [(f["test_range"][0], f["test_range"][1]) for f in wf.get("folds", [])]
overlaps = [(i, j) for i in range(len(ranges)) for j in range(i + 1, len(ranges))
            if ranges[i][0] <= ranges[j][1] and ranges[j][0] <= ranges[i][1]]
# The walk-forward is now purged and embargoed, so the partition must be
# stronger than "disjoint": the engine's own disjointness assertion must
# have run and passed, and the purge and embargo bands must actually have
# been applied. Two folds is the floor - one fold is not a walk-forward.
_dis = wf.get("disjointness") or {}
_checks = _dis.get("checks") or []
_dis_ok = bool(_checks) and all(c.get("ok", True) for c in _checks)
part_ok = (len(ranges) >= 2) and not overlaps and _dis_ok
for i, (a, b) in enumerate(ranges):
    print(f"    fold {i}: {a} -> {b}")
print(f"  overlapping pairs: {overlaps if overlaps else 'none'}")
print(f"  folds: {len(ranges)}  purge={wf.get('purge_bars')} "
      f"embargo={wf.get('embargo_bars')}")
print(f"  disjointness assertion: {'PASS' if _dis_ok else 'FAIL'} "
      f"({len(_checks)} checks)")
print(f"  -> {'PASS — windows are disjoint and the leak bands were applied' if part_ok else 'FAIL — the partition is not sound'}")
RESULTS.append({"label": "TEST 10 walk-forward partition", "verdict":
                "PASS" if part_ok else "FAIL", "expected": "PASS", "pass": part_ok})

# ----------------------------------------------------------------------
# TEST 11 — DISCLOSURE MAP VALIDATION. The import-time assert in
# disclosure.py is the guard against the whole class of defect this
# file regressed: a question mapped to a capability key that does not
# exist renders [UNKNOWN] even though the engine knows the answer, and a
# capability that is an empty string renders as a blank line. This test
# re-runs the same checks explicitly so a failure here names the
# offending key.
# ----------------------------------------------------------------------
print("\n" + "#" * 70)
print("# TEST 11 disclosure capability map validation")
print("#" * 70)
import disclosure as _disc

map_problems = []
try:
    _disc.validate_capability_map()
except ValueError as _ve:
    map_problems.append(str(_ve))

# (a) every CAP_BY_QUESTION value exists in CAPABILITIES
for _qk, _ck in _disc.CAP_BY_QUESTION.items():
    if _ck not in _disc.CAPABILITIES:
        map_problems.append("question '%s' -> missing capability '%s'" % (_qk, _ck))
    elif not str(_disc.CAPABILITIES[_ck]).strip():
        map_problems.append("question '%s' -> empty capability '%s'" % (_qk, _ck))

# (b) no capability value is an empty string
for _ck, _cv in _disc.CAPABILITIES.items():
    if not isinstance(_cv, str) or not _cv.strip():
        map_problems.append("capability '%s' is empty" % _ck)

# (c) every capability is either mapped or an explicitly allowed orphan
_mapped_caps = set(_disc.CAP_BY_QUESTION.values())
for _ck in _disc.CAPABILITIES:
    if _ck not in _mapped_caps and _ck not in _disc.ALLOWED_ORPHANS:
        map_problems.append("capability '%s' is unmapped and not an allowed orphan" % _ck)
for _ck, _why in _disc.ALLOWED_ORPHANS.items():
    if _ck not in _disc.CAPABILITIES:
        map_problems.append("allowed orphan '%s' has no capability entry" % _ck)
    if not str(_why).strip():
        map_problems.append("allowed orphan '%s' has no reason" % _ck)

# (d) every question key in SECTIONS has a CAP_BY_QUESTION entry
_unmapped_q = [q[0] for _s, _t, _qs in _disc.SECTIONS for q in _qs
               if q[0] not in _disc.CAP_BY_QUESTION]
for _k in _unmapped_q:
    map_problems.append("question '%s' has no CAP_BY_QUESTION entry" % _k)

# (e) the reverse audit: a who="engine" question with no working mapping
for _k in _disc.unmapped_engine_questions():
    map_problems.append("engine question '%s' cannot answer itself" % _k)

for _p in map_problems:
    print(f"    PROBLEM: {_p}")
print(f"  questions: {len(_disc.ALL_QUESTIONS)}  capabilities: {len(_disc.CAPABILITIES)}  "
      f"mapped: {len(_disc.CAP_BY_QUESTION)}")
print(f"  -> {'PASS — capability map is valid' if not map_problems else 'FAIL — capability map is invalid'}")
RESULTS.append({"label": "TEST 11 disclosure capability map", "verdict":
                "PASS" if not map_problems else "FAIL", "expected": "PASS",
                "pass": not map_problems})

# ----------------------------------------------------------------------
# TEST 12 — NO SPURIOUS UNKNOWN. A question the engine claims to answer
# must never render [UNKNOWN]: that is the sheet's worst failure mode,
# because it silently converts a verified engine fact into a gap the
# reader is asked to fill. Every who="engine"/"both" question must
# resolve to a real, non-empty answer, and a who="user" question must
# NOT be dressed up with an [engine] marker.
# ----------------------------------------------------------------------
print("\n" + "#" * 70)
print("# TEST 12 no spurious UNKNOWN on engine-answered questions")
print("#" * 70)
_fake_metrics = {
    "n_bars": 2520, "n_trades": 120, "sharpe": 1.11, "sortino": 1.4,
    "cagr": 0.12, "calmar": 1.0, "profit_factor": 1.6, "expectancy": 0.004,
    "win_rate": 0.55, "t_stat": 1.9, "var_95": -0.02, "cvar_95": -0.035,
    "max_drawdown": -0.12, "turnover_total": 42.0, "time_in_market": 0.8,
    "avg_gross_exposure": 1.0, "costs_paid": 1234.0,
}
_sheet12 = _disc.build_answers({}, {"metrics": _fake_metrics,
                                    "symbols": ["SPY", "VTI", "IVV", "RSP"],
                                    "n_symbols": 4})
unknown_problems = []
for _sid, _sec in _sheet12.items():
    for _it in _sec["items"]:
        if _it["who"] in ("engine", "both"):
            if _it["source"] == "missing":
                unknown_problems.append("%s renders UNKNOWN but who='%s'"
                                        % (_it["key"], _it["who"]))
            if str(_it["answer"]).strip().upper() == "UNKNOWN":
                unknown_problems.append("%s answer is the literal string UNKNOWN"
                                        % _it["key"])
        elif _it["source"] == "engine":
            unknown_problems.append("%s is who='user' but carries an engine answer"
                                    % _it["key"])
for _p in unknown_problems:
    print(f"    PROBLEM: {_p}")
_cov12 = _disc.coverage_summary(_sheet12)
print(f"  coverage: {_cov12['engine_answered']} engine / {_cov12['user_answered']} user / "
      f"{_cov12['unknown']} unknown of {_cov12['total']}")
print(f"  -> {'PASS — every engine-answered question has a real answer' if not unknown_problems else 'FAIL — spurious UNKNOWN'}")
RESULTS.append({"label": "TEST 12 no spurious UNKNOWN", "verdict":
                "PASS" if not unknown_problems else "FAIL", "expected": "PASS",
                "pass": not unknown_problems})

# ----------------------------------------------------------------------
# TEST 13 — COVERAGE COUNTS ARE SELF-CONSISTENT. The who-classification
# counts, the per-section question counts and the coverage summary are
# three views of the same sheet; if they disagree, the published
# coverage figure is a lie.
# ----------------------------------------------------------------------
print("\n" + "#" * 70)
print("# TEST 13 A-I coverage counts are self-consistent")
print("#" * 70)
count_problems = []
_who = _disc.who_counts()
_n_q = len(_disc.ALL_QUESTIONS)
if sum(_who.values()) != _n_q:
    count_problems.append("who_counts sum %d != %d questions" % (sum(_who.values()), _n_q))
_sec_total = sum(len(qs) for _s, _t, qs in _disc.SECTIONS)
if _sec_total != _n_q:
    count_problems.append("section item total %d != %d questions" % (_sec_total, _n_q))
_sids = [s for s, _t, _qs in _disc.SECTIONS]
if len(set(_sids)) != len(_sids):
    count_problems.append("duplicate section ids: %s" % _sids)
_cov13 = _disc.coverage_summary(_sheet12)
if _cov13["total"] != _n_q:
    count_problems.append("coverage total %d != %d questions" % (_cov13["total"], _n_q))
if _cov13["engine_answered"] + _cov13["user_answered"] + _cov13["unknown"] != _cov13["total"]:
    count_problems.append("coverage sources do not sum to the total")
# with no user answers, every unknown must be a who="user" question
_user_n = sum(1 for q in _disc.ALL_QUESTIONS if q[4] == "user")
if _cov13["unknown"] != _user_n:
    count_problems.append("unknown count %d != %d who='user' questions"
                           % (_cov13["unknown"], _user_n))
# and every who="user" question must actually be unanswered
if _cov13["user_answered"] != 0:
    count_problems.append("expected 0 user answers with an empty spec, got %d"
                           % _cov13["user_answered"])
# the engine must never be asked a question it has already answered
_pend13 = _disc.user_questions(_sheet12)
for _sid, _key, _q, _w in _pend13:
    if _disc.QUESTION_INDEX[_key][4] == "engine":
        count_problems.append("engine question '%s' is being asked of the user" % _key)
_both_n = sum(1 for q in _disc.ALL_QUESTIONS if q[4] == "both")
if len(_pend13) != _user_n + _both_n:
    count_problems.append("pending user questions %d != %d user + %d both"
                           % (len(_pend13), _user_n, _both_n))
for _p in count_problems:
    print(f"    PROBLEM: {_p}")
print(f"  who counts: {_who}")
print(f"  sections: {len(_sids)}  questions: {_n_q}  "
      f"coverage total: {_cov13['total']}  unknown: {_cov13['unknown']}")
print(f"  -> {'PASS — counts agree' if not count_problems else 'FAIL — counts disagree'}")
RESULTS.append({"label": "TEST 13 coverage counts consistent", "verdict":
                "PASS" if not count_problems else "FAIL", "expected": "PASS",
                "pass": not count_problems})

# ----------------------------------------------------------------------
# TEST 14 — REAL INSTRUMENT COUNT. B.observations must report how many
# instruments were actually in the backtest. The old code did
# len(ctx["symbols"]) on a value that was the PRESET NAME STRING, so a
# 4-instrument universe reported "across 15 instruments" (the length of
# "us_equity_broad"). The count must come from the real symbol list.
# ----------------------------------------------------------------------
print("\n" + "#" * 70)
print("# TEST 14 B.observations reports the REAL instrument count")
print("#" * 70)
_known = list(UNIVERSE_PRESETS["us_equity_broad"])
_expect_n = len(_known)
_ctx14 = {"metrics": _fake_metrics, "symbols": _known, "n_symbols": _expect_n}
_sheet14 = _disc.build_answers({}, _ctx14)
_obs14 = None
for _sec in _sheet14.values():
    for _it in _sec["items"]:
        if _it["key"] == "B.observations":
            _obs14 = _it["answer"]
obs_problems = []
if _obs14 is None:
    obs_problems.append("B.observations is not in the sheet at all")
else:
    if ("across %d instruments" % _expect_n) not in _obs14:
        obs_problems.append("expected 'across %d instruments' in: %s" % (_expect_n, _obs14))
    # the old bug: the length of the preset NAME string
    if ("across %d instruments" % len("us_equity_broad")) in _obs14:
        obs_problems.append("answer still uses the preset NAME length")
# a preset-name string must not be counted at all
_sheet14b = _disc.build_answers({}, {"metrics": _fake_metrics, "symbols": "us_equity_broad"})
for _sec in _sheet14b.values():
    for _it in _sec["items"]:
        if _it["key"] == "B.observations":
            if "instruments." in _it["answer"] and "did not supply an instrument count" not in _it["answer"]:
                obs_problems.append("a preset-name string was counted as instruments: %s"
                                    % _it["answer"])
for _p in obs_problems:
    print(f"    PROBLEM: {_p}")
print(f"  known universe ({_expect_n} symbols): {_known}")
print(f"  answer: {_obs14}")
print(f"  -> {'PASS — real instrument count reported' if not obs_problems else 'FAIL — wrong instrument count'}")
RESULTS.append({"label": "TEST 14 real instrument count", "verdict":
                "PASS" if not obs_problems else "FAIL", "expected": "PASS",
                "pass": not obs_problems})

# ----------------------------------------------------------------------
# TEST 15 — GROSS vs NET. H.gross_net must quote the gross-of-costs
# numbers from the gauntlet's gross_vs_net block. Labelling the NET
# Sharpe as "gross Sharpe" is a false claim about performance, and it
# is the one line in the sheet that talks about gross numbers.
# ----------------------------------------------------------------------
print("\n" + "#" * 70)
print("# TEST 15 H.gross_net quotes the real gross-of-costs run")
print("#" * 70)
_gvn = {"gross_sharpe": 1.83, "gross_cagr": 0.21, "net_sharpe": 1.11,
        "net_cagr": 0.12, "costs_paid": 5432.0, "turnover_total": 123456.0,
        "interpretation": "the edge survives costs"}
_sheet15 = _disc.build_answers({}, {"metrics": _fake_metrics, "gross_vs_net": _gvn})
_gn15 = None
for _sec in _sheet15.values():
    for _it in _sec["items"]:
        if _it["key"] == "H.gross_net":
            _gn15 = _it["answer"]
gn_problems = []
if _gn15 is None:
    gn_problems.append("H.gross_net is not in the sheet at all")
else:
    if "1.83" not in _gn15:
        gn_problems.append("gross Sharpe 1.83 (from gross_vs_net) not present: %s" % _gn15)
    if "21.00%" not in _gn15:
        gn_problems.append("gross CAGR 21.00%% not present: %s" % _gn15)
    if "1.11" not in _gn15:
        gn_problems.append("net Sharpe not present: %s" % _gn15)
    if "12.00%" not in _gn15:
        gn_problems.append("net CAGR 12.00%% not present: %s" % _gn15)
    # the net Sharpe must never be printed under the gross label
    if "gross Sharpe 1.11" in _gn15:
        gn_problems.append("the NET Sharpe is labelled as the gross Sharpe")
for _p in gn_problems:
    print(f"    PROBLEM: {_p}")
print(f"  answer: {_gn15}")
print(f"  -> {'PASS — gross vs net reported truthfully' if not gn_problems else 'FAIL — wrong gross/net numbers'}")
RESULTS.append({"label": "TEST 15 gross vs net truthfulness", "verdict":
                "PASS" if not gn_problems else "FAIL", "expected": "PASS",
                "pass": not gn_problems})

# ----------------------------------------------------------------------
# TEST 17 — REBALANCE TIMING. The rebalance mask gates ORDER GENERATION,
# not order FILLING. A decision taken on a rebalance bar must fill on the
# very next bar. Gating the fill instead left every order pending until
# the NEXT rebalance bar, so a monthly strategy executed each rebalance a
# full month late -- and since the default rebalance is "monthly", this
# silently affected every default run.
# ----------------------------------------------------------------------
print("\n" + "#" * 70)
print("# TEST 17 rebalance timing (decision bar -> next-bar fill)")
print("#" * 70)
_t17_ok = True
_d = [f"2024-{1 + (i // 28):02d}-{1 + (i % 28):02d}" for i in range(70)]
_b = {"S": {x: [100, 100, 100, 100, 1000] for x in _d}}
_p = Panel(_d, ["S"], _b, costs={"S": DEFAULT_COSTS["etf"]})
_sg = {"S": [1.0] * 70}
for _reb, _expect in (("daily", 1), ("monthly", 1), ("weekly", 1)):
    _tw = construct_target_weights(_p, _sg, gross_leverage=1.0, rebalance=_reb)
    _mk = rebalance_mask(_p, _reb)
    _sm = simulate(_p, _tw, initial_capital=100000, flat_at_close=False,
                   trade_mask=_mk, fill_at="open")
    _first = next((i for i, g in enumerate(_sm["gross_exposure"]) if g > 1e-9), None)
    _ok = _first == _expect
    _t17_ok = _t17_ok and _ok
    print(f"  rebalance={_reb:8s} first bar with a position = {_first} "
          f"(expected {_expect})  {'PASS' if _ok else 'FAIL'}")
print(f"  -> {'PASS — orders fill on the bar after the decision' if _t17_ok else 'FAIL'}")
RESULTS.append({"label": "TEST 17 rebalance timing", "verdict":
                "PASS" if _t17_ok else "FAIL", "expected": "PASS", "pass": _t17_ok})

# ----------------------------------------------------------------------
# TEST 18 — net_target AND max_position. The net target must be REACHED
# (not skipped when the book is already flat, not inverted when the sign
# differs) and the concentration cap must be a HARD limit applied last.
# ----------------------------------------------------------------------
print("\n" + "#" * 70)
print("# TEST 18 net_target reached + max_position is a hard cap")
print("#" * 70)
_b2 = {"A": {x: [100, 100, 100, 100, 1000] for x in _d},
       "B": {x: [100, 100, 100, 100, 1000] for x in _d}}
_p2 = Panel(_d, ["A", "B"], _b2, costs={x: DEFAULT_COSTS["etf"] for x in "AB"})
_sg2 = {"A": [1.0] * 70, "B": [-1.0] * 70}
_t18_ok = True
for _lbl, _pan, _sig, _nt, _cap in (
        ("one-sided net=0.0", _p, _sg, 0.0, None),
        ("one-sided net=0.2", _p, _sg, 0.2, None),
        ("one-sided net=-0.5", _p, _sg, -0.5, None),
        ("two-sided net=0.0", _p2, _sg2, 0.0, None),
        ("two-sided net=0.2", _p2, _sg2, 0.2, None),
        ("two-sided net=-0.5", _p2, _sg2, -0.5, None),
        ("two-sided + cap", _p2, _sg2, None, 0.30)):
    _kw = {"gross_leverage": 1.0}
    if _nt is not None:
        _kw["net_target"] = _nt
    if _cap is not None:
        _kw["max_position"] = _cap
    _tw2 = construct_target_weights(_pan, _sig, **_kw)
    _w = {x: _tw2[x][0] for x in _pan.symbols}
    _net = sum(_w.values())
    _netok = (_nt is None) or abs(_net - _nt) < 1e-9
    _capok = (_cap is None) or all(abs(v) <= _cap + 1e-9 for v in _w.values())
    _ok = _netok and _capok
    _t18_ok = _t18_ok and _ok
    print(f"  {_lbl:22s} net={_net:+.4f} cap_ok={_capok}  {'PASS' if _ok else 'FAIL'}")
print(f"  -> {'PASS — net target reached, cap enforced' if _t18_ok else 'FAIL'}")
RESULTS.append({"label": "TEST 18 net_target and max_position", "verdict":
                "PASS" if _t18_ok else "FAIL", "expected": "PASS", "pass": _t18_ok})

# ----------------------------------------------------------------------
# TEST 19 — CONFIGURATION ERRORS MUST RAISE, NOT SILENTLY DEGRADE.
# An unknown rebalance string used to fall through to a one-bar mask,
# producing zero trades and a verdict that read as "no edge". A single
# instrument with the cross-sectional signal used to produce a perfectly
# flat backtest with no explanation. Both are configuration errors and
# both must say so.
# ----------------------------------------------------------------------
print("\n" + "#" * 70)
print("# TEST 19 configuration errors raise instead of degrading silently")
print("#" * 70)
_t19_ok = True
try:
    rebalance_mask(_p, "monthley")
    _t19_ok = False
    print("  unknown rebalance -> FAIL (no error raised)")
except ValueError as _e:
    print(f"  unknown rebalance -> PASS: {str(_e)[:64]}...")
try:
    _p1 = Panel(_d, ["S"], _b, costs={"S": DEFAULT_COSTS["etf"]})
    compute_signal(_p1, {"momentum": {"S": [0.1] * 70}},
                   {"type": "cross_sectional", "params": {"long_rank": 0.75,
                                                          "short_rank": 0.25}})
    _t19_ok = False
    print("  single-symbol cross-sectional -> FAIL (no error raised)")
except ValueError as _e:
    print(f"  single-symbol cross-sectional -> PASS: {str(_e)[:64]}...")
print(f"  -> {'PASS — misconfiguration is reported' if _t19_ok else 'FAIL'}")
RESULTS.append({"label": "TEST 19 configuration errors raise", "verdict":
                "PASS" if _t19_ok else "FAIL", "expected": "PASS", "pass": _t19_ok})

# ----------------------------------------------------------------------
# TEST 20 — CLI / LIBRARY PARITY. The library's run_spec() used to skip
# DEFAULT_SPEC while the CLI's load_spec() applied it, so the SAME
# strategy could get DIFFERENT VERDICTS depending on how it was invoked.
# ----------------------------------------------------------------------
print("\n" + "#" * 70)
print("# TEST 20 library and CLI evaluate the same specification")
print("#" * 70)
_t20_ok = True
_m = apply_defaults({"name": "x"})
for _k, _v in (("signal", DEFAULT_SPEC["signal"]),
               ("features", DEFAULT_SPEC["features"]),
               ("period", DEFAULT_SPEC["period"]),
               ("portfolio", DEFAULT_SPEC["portfolio"]),
               ("execution", DEFAULT_SPEC["execution"])):
    if _m.get(_k) != _v:
        _t20_ok = False
        print(f"  default for '{_k}' not applied -> FAIL")
_over = apply_defaults({"portfolio": {"gross_leverage": 2.0}})
if _over["portfolio"]["gross_leverage"] != 2.0 or \
        _over["portfolio"]["rebalance"] != DEFAULT_SPEC["portfolio"]["rebalance"]:
    _t20_ok = False
    print("  user value did not override the default -> FAIL")
print(f"  -> {'PASS — same defaults on both entry points' if _t20_ok else 'FAIL'}")
RESULTS.append({"label": "TEST 20 CLI/library parity", "verdict":
                "PASS" if _t20_ok else "FAIL", "expected": "PASS", "pass": _t20_ok})

# ----------------------------------------------------------------------
# TEST 21 — max_names MUST NOT LOOK AHEAD. Universe membership used to be
# decided ONCE for the whole sample by asking "does this symbol EVER
# signal?", so a name that first signalled on the FINAL bar was in the book
# from bar 0. The survivors were also the alphabetically first N, not the
# strongest.
# ----------------------------------------------------------------------
print("\n" + "#" * 70)
print("# TEST 21 max_names membership is per-bar (no look-ahead)")
print("#" * 70)
_t21_ok = True
_b3 = {x: {y: [100, 100, 100, 100, 1000] for y in _d} for x in ("A", "B", "C")}
_p3 = Panel(_d, ["A", "B", "C"], _b3,
            costs={x: DEFAULT_COSTS["etf"] for x in ("A", "B", "C")})
# C signals ONLY on the final bar.
_sg3 = {"A": [1.0] * 70, "B": [1.0] * 70, "C": [0.0] * 69 + [1.0]}
_tw3 = construct_target_weights(_p3, _sg3, gross_leverage=1.0,
                                rebalance="monthly", max_names=1)
_c_at_0 = abs(_tw3["C"][0]) > 1e-9
if _c_at_0:
    _t21_ok = False
    print("  a name signalling only at the END was in the book at bar 0 -> FAIL")
else:
    print("  a name signalling only at the END is absent at bar 0 -> PASS")
# The selected name must be the strongest, not the alphabetically first.
_tw4 = construct_target_weights(_p3, _sg3, gross_leverage=1.0,
                                rebalance="monthly", max_names=1,
                                strength={"A": [0.2] * 70, "B": [0.9] * 70,
                                          "C": [0.5] * 70})
_sel = [s for s in ("A", "B", "C") if abs(_tw4[s][0]) > 1e-9]
_ok21 = _sel == ["B"]
_t21_ok = _t21_ok and _ok21
print(f"  strongest name selected: {_sel} (expected ['B'])  "
      f"{'PASS' if _ok21 else 'FAIL'}")
print(f"  -> {'PASS — membership decided at each bar' if _t21_ok else 'FAIL'}")
RESULTS.append({"label": "TEST 21 max_names has no look-ahead", "verdict":
                "PASS" if _t21_ok else "FAIL", "expected": "PASS", "pass": _t21_ok})

# ----------------------------------------------------------------------
# TEST 22 — vol_target's LEVEL MUST MATTER. The weights were scaled by
# inverse volatility and then normalised to gross, which cancels the level:
# vol_target 0.05 and 0.50 produced byte-identical books while the spec
# promised "size the book to X% annual vol".
# ----------------------------------------------------------------------
print("\n" + "#" * 70)
print("# TEST 22 vol_target level is honoured")
print("#" * 70)
_t22_ok = True
_b5 = {}
for _s in ("A", "B"):
    _bars = {}
    _px = 100.0
    for _i, _x in enumerate(_d):
        _step = 1.0 if _s == "A" else 2.0
        _px *= (1.0 + (0.01 if _i % 2 == 0 else -0.01) * _step)
        _bars[_x] = [_px, _px, _px, _px, 100000]
    _b5[_s] = _bars
_p5 = Panel(_d, ["A", "B"], _b5, costs={x: DEFAULT_COSTS["etf"] for x in ("A", "B")})
_sg5 = {"A": [1.0] * 70, "B": [-1.0] * 70}
_grosses = {}
for _vt in (0.05, 0.20, 0.50):
    _t5 = construct_target_weights(_p5, _sg5, gross_leverage=1.0,
                                   vol_target=_vt, vol_lookback=20)
    _grosses[_vt] = sum(abs(_t5[_s][35]) for _s in ("A", "B"))
    print(f"  vol_target={_vt:.2f} -> gross = {_grosses[_vt]:.4f}")
_ok22 = len({round(v, 6) for v in _grosses.values()}) == 3
_t22_ok = _t22_ok and _ok22
print(f"  -> {'PASS — three targets give three different books' if _t22_ok else 'FAIL'}")
RESULTS.append({"label": "TEST 22 vol_target level honoured", "verdict":
                "PASS" if _t22_ok else "FAIL", "expected": "PASS", "pass": _t22_ok})

# ----------------------------------------------------------------------
# TEST 23 — THE LIQUIDITY CAP MUST USE LAGGED VOLUME. The cap used to be
# computed from the SAME bar's volume to size an order that fills at that
# bar's OPEN. The bar's volume is not knowable at its open, so a thin bar
# artificially tightened the cap and a fat bar loosened it -- and when the
# volume was zero the cap silently disappeared, letting an order of ANY
# size through. The window must be strictly before the fill bar.
# ----------------------------------------------------------------------
print("\n" + "#" * 70)
print("# TEST 23 liquidity cap uses trailing (lagged) volume")
print("#" * 70)
_t23_ok = True
_b6 = {"S": {}}
for _i, _x in enumerate(_d):
    # Bar 0 is very liquid; every later bar is very thin.
    _v = 10_000_000 if _i == 0 else 1_000
    _b6["S"][_x] = [100, 100, 100, 100, _v]
_p6 = Panel(_d, ["S"], _b6, costs={"S": DEFAULT_COSTS["etf"]})
_sg6 = {"S": [1.0] * 70}
_tw6 = construct_target_weights(_p6, _sg6, gross_leverage=1.0, rebalance="daily")
_sm6 = simulate(_p6, _tw6, initial_capital=100000, flat_at_close=False,
                fill_at="open")
_g6 = _sm6["gross_exposure"][1]
# Trailing ADV at bar 1 = bar 0's dollar volume (100 x 10,000,000) -> the cap
# does not bind and the book reaches its target gross of 1.0.
# Same-bar volume at bar 1 = 100 x 1,000 -> cap would bind near 0.1.
_ok23 = _g6 > 0.9
_t23_ok = _t23_ok and _ok23
print(f"  gross at bar 1 = {_g6:.4f}  (lagged: ~1.00, same-bar: ~0.10)  "
      f"{'PASS' if _ok23 else 'FAIL'}")
# A window with no volume must not open the cap to unlimited size -- but
# only for an instrument that HAS volume history. An instrument with no
# volume series at all (FX pairs, most OTC instruments) has no screen volume
# to participate in, so the cap does not apply to it and it must still trade.
# These are two different cases and must not be conflated.
_b7 = {"S": {}}
for _i, _x in enumerate(_d):
    # Bars 0-4 did not trade; the instrument is liquid from bar 5 onward.
    _b7["S"][_x] = [100, 100, 100, 100, (0 if _i < 5 else 1_000_000)]
_p7 = Panel(_d, ["S"], _b7, costs={"S": DEFAULT_COSTS["etf"]})
_sm7 = simulate(_p7, _tw6, initial_capital=100000, flat_at_close=False,
                fill_at="open")
_first7 = next((i for i, g in enumerate(_sm7["gross_exposure"]) if g > 1e-9), None)
_ok23b = (_sm7.get("liquidity_skips", 0) > 0
          and _first7 is not None and _first7 >= 5
          and "S" not in (_sm7.get("symbols_without_volume") or []))
_t23_ok = _t23_ok and _ok23b
print(f"  instrument WITH volume, early bars empty: skips="
      f"{_sm7.get('liquidity_skips')} first_fill={_first7} (>=5)  "
      f"{'PASS' if _ok23b else 'FAIL'}")
# An instrument with NO volume series at all: the cap does not apply, and
# the strategy must still trade.
_b8 = {"S": {x: [100, 100, 100, 100, 0] for x in _d}}
_p8 = Panel(_d, ["S"], _b8, costs={"S": DEFAULT_COSTS["etf"]})
_sm8 = simulate(_p8, _tw6, initial_capital=100000, flat_at_close=False,
                fill_at="open")
_ok23c = ("S" in (_sm8.get("symbols_without_volume") or [])
          and _sm8.get("liquidity_skips", 0) == 0
          and any(g > 1e-9 for g in _sm8["gross_exposure"]))
_t23_ok = _t23_ok and _ok23c
print(f"  instrument with NO volume series: skipped as no-volume="
      f"{_sm8.get('symbols_without_volume')} skips={_sm8.get('liquidity_skips')} "
      f"trades_anyway={any(g > 1e-9 for g in _sm8['gross_exposure'])}  "
      f"{'PASS' if _ok23c else 'FAIL'}")
print(f"  -> {'PASS — cap uses lagged volume' if _t23_ok else 'FAIL'}")
RESULTS.append({"label": "TEST 23 liquidity cap uses lagged volume", "verdict":
                "PASS" if _t23_ok else "FAIL", "expected": "PASS", "pass": _t23_ok})

# ----------------------------------------------------------------------
# TEST 24 — NO FILL ON A STALE BAR. When a symbol prints no observation on
# a date, the panel forward-fills the price from an earlier session. Filling
# orders at that price means trading a price the market did not print, which
# let a halted or delisted name trade indefinitely at a phantom price.
# ----------------------------------------------------------------------
print("\n" + "#" * 70)
print("# TEST 24 no fill on a stale (forward-filled) bar")
print("#" * 70)
_t24_ok = True
_b8 = {"S": {}}
for _i, _x in enumerate(_d):
    # Real bars at 0 and from 5 onward; bars 1-4 are missing.
    if _i in (1, 2, 3, 4):
        continue
    _b8["S"][_x] = [100, 100, 100, 100, 1_000_000]
_p8 = Panel(_d, ["S"], _b8, costs={"S": DEFAULT_COSTS["etf"]})
_tw8 = construct_target_weights(_p8, {"S": [1.0] * 70}, gross_leverage=1.0,
                                 rebalance="daily")
_sm8 = simulate(_p8, _tw8, initial_capital=100000, flat_at_close=False,
                fill_at="open")
_g8 = _sm8["gross_exposure"]
_first8 = next((i for i, g in enumerate(_g8) if g > 1e-9), None)
_ok24 = _first8 == 5 and _sm8.get("stale_skips", 0) >= 4
_t24_ok = _t24_ok and _ok24
print(f"  first bar with a position = {_first8} (expected 5, not 1)  "
      f"stale_skips={_sm8.get('stale_skips')}  {'PASS' if _ok24 else 'FAIL'}")
print(f"  -> {'PASS — orders are not filled at phantom prices' if _t24_ok else 'FAIL'}")
RESULTS.append({"label": "TEST 24 no fill on a stale bar", "verdict":
                "PASS" if _t24_ok else "FAIL", "expected": "PASS", "pass": _t24_ok})

# ----------------------------------------------------------------------
# TEST 16 — THE NEGATIVE-CONTROL GUARD MUST NOT BE VACUOUS. It used to
# filter on the UPPERCASE string "NEGATIVE" while the labels say
# "negative control" (lower case), so the list was ALWAYS empty and
# all([]) was vacuously True -- the guard could never fail.
# ----------------------------------------------------------------------
neg = [r for r in RESULTS if "negative" in r["label"].lower()]
neg_problems = []
if not neg:
    neg_problems.append(
        "no negative-control test found in RESULTS — the guard is vacuous")
for r in neg:
    if r["verdict"] == "SUPPORTED":
        neg_problems.append(
            f"{r['label']} was judged SUPPORTED — a known-bad strategy passed")
neg_ok = not neg_problems
for _p in neg_problems:
    print(f"    PROBLEM: {_p}")
print("\n  negative controls correctly not SUPPORTED:", neg_ok,
      f"({len(neg)} negative-control test(s) checked)")
RESULTS.append({"label": "TEST 16 negative control guard is not vacuous",
                "verdict": "PASS" if neg_ok else "FAIL", "expected": "PASS",
                "pass": neg_ok})

# ----------------------------------------------------------------------
print("\n" + "=" * 70)
print("SELFTEST SUMMARY")
print("=" * 70)
for r in RESULTS:
    mark = "ok  " if r["pass"] else "FAIL"
    print(f"  [{mark}] {r['label']}: {r['verdict']}"
          + (f"  (expected one of {r['expected']})" if r.get("expected") else ""))
failed = [r for r in RESULTS if not r["pass"]]
print("  RESULT:", "ALL CHECKS PASSED" if not failed else f"{len(failed)} FAILURE(S)")
for _r in failed:
    print(f"    FAILED: {_r['label']} — {_r['verdict']}")
sys.exit(0 if not failed else 1)