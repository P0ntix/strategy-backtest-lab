#!/usr/bin/env python3
"""
btvalidate.py -- Stage 8: performance metrics, the validation gauntlet, and
the verdict. Imports btengine. Pure stdlib.

The gauntlet (each is a potential refutation of the hypothesis):
  1.  base metrics + statistical power (t-stat of the return series)
  2.  walk-forward out-of-sample, PURGED and EMBARGOED (the gold standard)
  3.  cost stress (does the edge survive 1.5x / 2x / 3x costs?)
  4.  parameter sensitivity (plateau vs. spike)
  5.  Monte Carlo bootstrap over the realised trade sequence (resampled WITH
      REPLACEMENT; the Sharpe it reports is PER TRADE, not the per-bar
      Sharpe used everywhere else in this report)
  6.  subperiod / regime decomposition
  7.  capacity (position sizing vs. liquidity)
  8.  cross-market replication
  9.  data-snooping adjustment (deflated Sharpe, IS-vs-OOS rank inversion)
 10.  causality audit (no look-ahead)

Verdict vocabulary is deliberately blunt:
  SUPPORTED   survives every gauntlet stage with OOS evidence
  FRAGILE     positive OOS but fails robustness or power
  REFUTED     no OOS edge, or edge is entirely a cost artifact
  INCONCLUSIVE insufficient data / too few trades to judge
"""

from __future__ import annotations

import json
import math
import os
import random
import statistics
from datetime import datetime

try:
    # Flat import: running as a script, or with scripts/ on sys.path.
    from btengine import (
        PERIODS_PER_YEAR,
        FEATURES,
        SIGNALS,
        UNIVERSE_PRESETS,
        Panel,
        compute_features,
        compute_signal,
        construct_target_weights,
        downside_dev,
        f_volatility,
        max_drawdown,
        mean,
        quantile,
        rebalance_mask,
        resolve_signal_spec,
        run_spec,
        safe_div,
        simulate,
        stdev,
    )
except ImportError:  # pragma: no cover - exercised by the installed `btlab` layout
    # Relative import: installed as a package (btlab.btvalidate).
    from .btengine import (
        PERIODS_PER_YEAR,
        FEATURES,
        SIGNALS,
        UNIVERSE_PRESETS,
        Panel,
        compute_features,
        compute_signal,
        construct_target_weights,
        downside_dev,
        f_volatility,
        max_drawdown,
        mean,
        quantile,
        rebalance_mask,
        resolve_signal_spec,
        run_spec,
        safe_div,
        simulate,
        stdev,
    )

# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------


def _clean(xs):
    return [x for x in xs if x is not None and x == x]


def cagr_from_returns(rets, periods_per_year=252):
    rets = _clean(rets)
    if not rets:
        return None
    growth = 1.0
    for r in rets:
        growth *= (1.0 + r)
    years = len(rets) / periods_per_year
    if years <= 0 or growth <= 0:
        return -1.0
    return growth ** (1.0 / years) - 1.0


def sharpe(rets, rf_annual=0.0, periods_per_year=252):
    rets = _clean(rets)
    if len(rets) < 3:
        return None
    rf = rf_annual / periods_per_year
    ex = [r - rf for r in rets]
    sd = stdev(ex)
    if not sd:
        return None
    return (mean(ex) / sd) * math.sqrt(periods_per_year)


def sortino(rets, rf_annual=0.0, periods_per_year=252):
    rets = _clean(rets)
    if len(rets) < 3:
        return None
    rf = rf_annual / periods_per_year
    ex = [r - rf for r in rets]
    dd = downside_dev(ex, 0.0)
    if not dd:
        return None
    return (mean(ex) / dd) * math.sqrt(periods_per_year)


def calmar(rets, periods_per_year=252):
    """
    Calmar ratio = CAGR / maximum drawdown.

    The drawdown MUST be measured against the RUNNING PEAK of the growth
    curve, exactly as `max_drawdown_pct` does. An earlier version pinned the
    peak at 1.0 (the initial equity) and re-used the FINAL growth value for
    every bar, so it measured "how far the equity ended below where it
    started" instead of the real peak-to-trough drawdown. A strategy that
    rose 100% and then fell 50% was reported as having no drawdown at all,
    and every Calmar came back n/a.

    Sign convention: the numerator (CAGR) can be negative while the
    denominator (max drawdown) is a positive fraction, so a losing series
    yields a NEGATIVE Calmar. That is the honest answer -- it is only
    undefined (n/a) when there is no drawdown at all to divide by.
    """
    c = cagr_from_returns(rets, periods_per_year)
    if c is None:
        return None
    mdd = max_drawdown_pct(rets)
    return safe_calmar(c, mdd)


def safe_calmar(c, mdd):
    """CAGR over max drawdown. None when the drawdown is zero (undefined)."""
    if mdd is None or mdd <= 1e-9:
        return None
    return c / mdd


def profit_factor(trade_pnls):
    pnls = _clean(trade_pnls)
    if not pnls:
        return None
    wins = sum(p for p in pnls if p > 0)
    losses = -sum(p for p in pnls if p < 0)
    if losses <= 0:
        return None
    return wins / losses


def expectancy(trade_pnls):
    pnls = _clean(trade_pnls)
    if not pnls:
        return None
    return mean(pnls)


def max_drawdown_pct(rets):
    growth, peak, mdd, ti, pi = 1.0, 1.0, 0.0, 0, 0
    cur_peak_i = 0
    for i, r in enumerate(_clean(rets)):
        growth *= (1 + r)
        if growth > peak:
            peak, cur_peak_i = growth, i
        dd = 1 - growth / peak
        if dd > mdd:
            mdd, pi, ti = dd, cur_peak_i, i
    return mdd


def drawdown_series(rets):
    growth, peak = 1.0, 1.0
    out = []
    for r in _clean(rets):
        growth *= (1 + r)
        peak = max(peak, growth)
        out.append(growth / peak - 1.0)
    return out


def var_cvar(rets, q=0.05):
    rs = sorted(_clean(rets))
    if len(rs) < 20:
        return None, None
    idx = max(0, int(q * len(rs)) - 1)
    var = rs[idx]
    tail = rs[: idx + 1]
    cvar = mean(tail) if tail else None
    return var, cvar


def skew_kurt(rets):
    rs = _clean(rets)
    if len(rs) < 5:
        return None, None
    m, sd = mean(rs), stdev(rs)
    if not sd:
        return None, None
    sk = sum(((x - m) / sd) ** 3 for x in rs) / len(rs)
    ku = sum(((x - m) / sd) ** 4 for x in rs) / len(rs) - 3.0
    return sk, ku


def lo_sharpe_adjust(sharpe_value, n_obs, autocorr_1):
    """Lo (2002) adjustment for autocorrelation-induced Sharpe inflation."""
    if not sharpe_value or not n_obs:
        return None
    v = (1 + sharpe_value / 2) * sharpe_value / 2  # rough per-obs
    try:
        gamma = 1.0 + 2.0 * autocorr_1
        return sharpe_value / math.sqrt(max(1e-9, 1 + gamma))
    except Exception:
        return sharpe_value


def lag1_autocorr(xs):
    xs = _clean(xs)
    if len(xs) < 5:
        return 0.0
    m = mean(xs)
    num = sum((xs[i] - m) * (xs[i - 1] - m) for i in range(1, len(xs)))
    den = sum((x - m) ** 2 for x in xs)
    return num / den if den else 0.0


def deflated_sharpe(sharpe_value, n_obs, n_variants=1):
    """
    Lopez de Prado's Deflated Sharpe Ratio (approximate). Adjusts an observed
    Sharpe for (a) non-normality via skew/kurt and (b) the number of strategy
    variants tried -- the data-snooping penalty.
    """
    if not sharpe_value or n_obs < 10:
        return None
    if n_variants <= 1:
        return sharpe_value
    # Expected max Sharpe under the null across n_variants trials.
    euler = 0.5772
    sigma_n = 1.0 / math.sqrt(max(1, n_obs))
    # Approximate quantile of the max of n iid N(0, sigma_n) under H0
    z = math.sqrt(2 * math.log(max(2, n_variants)))
    expected_max = sigma_n * ((1 - euler) * z + euler)
    return sharpe_value - expected_max


def pbo_proxy(is_sharpes, oos_sharpes):
    """
    Probability-of-backtest-overfitting PROXY: the IS-vs-OOS RANK INVERSION
    rate across walk-forward folds.

    For every pair of folds we compare two rankings of the same two folds:
    the ranking by IN-SAMPLE Sharpe and the ranking by OUT-OF-SAMPLE Sharpe.
    The pair INVERTS when the fold that looked better in-sample performed
    worse out-of-sample. The reported number is the fraction of comparable
    fold pairs that inverted (0% = the in-sample ranking carried over
    perfectly, 100% = it was pure noise); Kendall's tau between the two
    rankings is reported alongside as 1 - 2 * inversion_rate.

    This is the statistic the Bailey / Lopez de Prado overfitting analysis
    is built around -- does the in-sample ordering of candidates survive
    out of sample -- computed across the folds of one walk-forward run. It
    is NOT the full combinatorially-symmetric (CSCV) PBO: that version
    splits the sample into many sub-periods, re-ranks a POPULATION of
    strategy configurations in every split and needs multiple candidate
    configurations per run, which this engine does not produce. Treat the
    number as a directional overfit signature, not a calibrated probability.

    NOTE the signature: this function REQUIRES the per-fold in-sample
    Sharpe alongside the per-fold out-of-sample Sharpe. An earlier version
    took the OOS Sharpe list alone and compared each OOS Sharpe against
    the ABSOLUTE value of its partner, so it measured the OOS Sharpe
    distribution's own sign disagreements and never looked at the in-sample
    evidence at all -- it could not detect overfitting because it never saw
    the in-sample result.
    """
    n = min(len(is_sharpes or []), len(oos_sharpes or []))
    if n < 4:
        return None
    inversions = 0
    comparable = 0
    for i in range(n):
        for j in range(i + 1, n):
            is_i, is_j = is_sharpes[i], is_sharpes[j]
            o_i, o_j = oos_sharpes[i], oos_sharpes[j]
            if None in (is_i, is_j, o_i, o_j):
                continue
            if is_i == is_j or o_i == o_j:
                # A tie carries no ordering information in either ranking.
                continue
            comparable += 1
            if (is_i > is_j) != (o_i > o_j):
                inversions += 1
    if not comparable:
        return None
    rate = inversions / comparable
    return {
        "inversion_rate": rate,
        "kendall_tau": 1.0 - 2.0 * rate,
        "n_pairs": comparable,
        "n_inversions": inversions,
        "label": "IS-vs-OOS rank inversion (PBO proxy)",
    }


# --------------------------------------------------------------------------
# Aggregate metrics for a simulation bundle
# --------------------------------------------------------------------------


def compute_metrics(bundle, rf_annual=0.0, periods_per_year=252):
    sim = bundle["sim"]
    panel = bundle["panel"]
    rets = sim["returns"]
    dates = panel.dates
    # Align returns to dates
    dated_rets = [(dates[i], rets[i]) for i in range(min(len(dates), len(rets))) if rets[i] is not None]
    n_bars = len(dated_rets)
    trade_pnls = [t["pnl"] for t in sim["trades"]]
    rets_c = _clean(rets)

    cagr = cagr_from_returns(rets_c, periods_per_year)
    sh = sharpe(rets_c, rf_annual, periods_per_year)
    so = sortino(rets_c, rf_annual, periods_per_year)
    mdd = max_drawdown_pct(rets_c)
    cal = safe_calmar(cagr, mdd)
    var95, cvar95 = var_cvar(rets_c, 0.05)
    sk, ku = skew_kurt(rets_c)
    pf = profit_factor(trade_pnls)
    exp = expectancy(trade_pnls)
    win_rate = (sum(1 for p in trade_pnls if p > 0) / len(trade_pnls)) if trade_pnls else None
    gexp = _clean(sim.get("gross_exposure"))
    time_in_market = mean([1.0 if e > 0.05 else 0.0 for e in gexp])

    # Statistical power: t-stat of daily returns, and Sharpe standard error.
    t_stat = None
    if len(rets_c) > 2 and stdev(rets_c):
        t_stat = mean(rets_c) / (stdev(rets_c) / math.sqrt(len(rets_c)))

    return {
        "n_bars": n_bars,
        "n_trades": len(trade_pnls),
        "final_equity": sim["final_equity"],
        # The basis of the TRADE LEDGER's P&L. The engine reports it as
        # "net" (trade P&L already net of costs) or "gross" (costs are not
        # deducted from the ledger). It is surfaced here so the report can
        # LABEL profit factor / expectancy / win rate with their basis
        # instead of implying a net number. None means the engine did not
        # say, and the report falls back to a gross label plus a note.
        "trade_pnl_basis": sim.get("trade_pnl_basis"),
        "total_return": (sim["final_equity"] / bundle.get("spec", {}).get("capital", 100000.0) - 1.0)
        if bundle.get("spec", {}).get("capital") else None,
        "cagr": cagr,
        "sharpe": sh,
        "sortino": so,
        "max_drawdown": mdd,
        "calmar": cal,
        "profit_factor": pf,
        "expectancy": exp,
        "win_rate": win_rate,
        "var_95": var95,
        "cvar_95": cvar95,
        "skew": sk,
        "kurtosis": ku,
        "t_stat": t_stat,
        "sharpe_lag1": lo_sharpe_adjust(sh, n_bars, lag1_autocorr(rets_c)),
        "costs_paid": sim.get("costs_paid"),
        "turnover_total": sim.get("turnover_total"),
        "time_in_market": time_in_market,
        "avg_gross_exposure": mean(gexp),
    }


# --------------------------------------------------------------------------
# Gauntlet stage: walk-forward out-of-sample
# --------------------------------------------------------------------------


# --------------------------------------------------------------------------
# Gauntlet stage: walk-forward out-of-sample (purged + embargoed)
# --------------------------------------------------------------------------

# Parameter names that express a window length in a feature spec. The
# largest of them is the default purge size: a training bar whose label
# horizon reaches that far forward can overlap the start of a test block.
LOOKBACK_PARAM_KEYS = ("lookback", "lag", "fast", "short")
MIN_TEST_BARS = 60        # a fold's test window must be long enough to measure
MIN_TRAIN_BARS = 120      # a fold's in-sample segment must be usable
MIN_LIVE_BARS = 60        # a fold's test window must carry this much signal
                          # AFTER the feature warm-up is spent


def _spec_label_horizon(spec):
    """
    The largest label/lookback horizon among the spec's feature parameters.

    This is the default purge size and the default embargo size: a training
    bar whose label window reaches that far forward can overlap the start
    of a test block, so it must be excluded from the training set. The keys
    considered are the ones that express a window length: "lookback",
    "lag", "fast", "short".
    """
    horizon = 0
    for feat in spec.get("features") or []:
        params = (feat or {}).get("params") or {}
        for key in LOOKBACK_PARAM_KEYS:
            v = params.get(key)
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                continue
            if v > horizon:
                horizon = int(v)
    return horizon


def _plan_legacy_folds(n, n_folds):
    """
    The pre-purge layout, kept available so existing comparisons remain
    possible: contiguous, non-overlapping test blocks and one expanding
    training window per fold (which contains the earlier folds' test
    blocks -- which is exactly what the purge/embargo layout below fixes).
    """
    block = n // (n_folds + 1)
    folds = []
    for f in range(n_folds):
        te_start = block * (f + 1)
        te_end = min(n, block * (f + 2))
        # The legacy training set is the WHOLE expanding window
        # [0, te_start) -- one contiguous range, not a union of per-fold
        # ranges (which would double-count the earlier bars).
        folds.append({"fold": f, "train_segments": [(0, te_start)],
                      "purge_band": None, "embargo_band": None,
                      "test": (te_start, te_end)})
    return folds


def _plan_purged_folds(n, n_folds, purge, embargo, warmup=0,
                       min_test=MIN_TEST_BARS, min_train=MIN_TRAIN_BARS):
    """
    Purged + embargoed layout, one gap between consecutive test blocks:

        [train_0][purge_0][test_0][embargo_0][train_1][purge_1][test_1] ...

    * train_f    in-sample bars. A fold's training set is its own train
                 segment PLUS every earlier train segment: expanding, but
                 never containing a bar that was scored out of sample in
                 any fold, so the in-sample number and the out-of-sample
                 number are measured on disjoint data.
    * purge_f    `purge` bars immediately before test_f, excluded from
                 every training set: a training bar whose label horizon
                 reaches into the test block would leak the test period.
    * embargo_f  `embargo` bars immediately after test_f, excluded from
                 every later training set, so the bars right after a test
                 block can never be used as in-sample evidence.

    `warmup` is the feature warm-up length. A fold is re-simulated cold
    (a sliced panel, no history), so the first `warmup` bars of a test
    block carry no signal at all; a test window shorter than the warm-up
    would therefore measure nothing. Test windows are sized to clear the
    warm-up and still leave a measurable window.

    Sizing tiers, in order of preference:
      1. the requested bands at full size, with as many folds as the
         sample supports (the band size is the leak guarantee, so it is
         never quietly halved to keep a fold count);
      2. the bands shrunk, only if no fold count works at full size.
    The sizes and fold count actually used are returned so the report can
    state them. Returns (folds, purge_used, embargo_used, notes).
    """
    notes = []
    # A cold-started fold needs `warmup` bars before it carries any signal,
    # so only test - warmup bars of each window are actually measured.
    best = None
    for scale in (1.0, 0.75, 0.5, 0.25, 0.0):
        p = int(round(purge * scale))
        e = int(round(embargo * scale))
        for F in range(n_folds, 1, -1):        # 2 folds is the floor
            # Each fold consumes a test window and a purge band; the
            # embargo bands sit between consecutive folds. The base run's
            # own warm-up region is unusable, so it is excluded too.
            bands = F * p + (F - 1) * e + warmup
            avail = n - bands
            if avail <= 0:
                continue
            # In-sample segments take their minimum; the rest of the
            # sample becomes the test windows, which carry the verdict.
            test = (avail - F * min_train) // F
            if test - warmup < MIN_LIVE_BARS:
                continue
            train_total = avail - F * test
            if train_total < F * min_train:
                continue
            # PREFER THE LARGEST FOLD COUNT that fits, then the largest test
            # windows. Maximising total live BARS instead would systematically
            # prefer two wide folds over five narrow ones, which weakens the
            # fold-consistency check (the "N of M folds profitable" flag) and
            # leaves the rank-inversion statistic with too few folds to
            # exist at all. More independent out-of-sample periods is the
            # stronger evidence, so the requested fold count wins.
            cand = (F, test, p, e, train_total)
            if best is None or cand[0] > best[0]:
                best = cand
                break
        if best is not None:
            # The requested band size is the leak guarantee, so it is never
            # quietly shrunk to keep a fold count: if any fold count works
            # at this size, stop here.
            break
    if best is None:
        return None, 0, 0, notes
    F, test, p, e, train_total = best
    if (p, e) != (purge, embargo):
        notes.append(
            f"purge/embargo reduced from {purge}/{embargo} to {p}/{e} bars: "
            f"the sample cannot support the requested bands and still leave "
            f"a measurable test window and in-sample segment per fold")
    if F != n_folds:
        notes.append(f"fold count reduced from {n_folds} to {F} to fit the "
                     f"requested purge/embargo bands")

    folds = []
    # Start after the BASE run's warm-up: the base simulation is also cold,
    # so a training segment inside the warm-up region would measure
    # nothing (every return is flat). Those bars are left unused.
    pos = warmup
    segs = []          # the in-sample segment of each fold, in order
    spare = train_total - F * min_train
    for f in range(F):
        # Spare bars are spread evenly across the segments, so no fold's
        # in-sample window is starved.
        train_len = min_train + spare // F + (1 if f < spare % F else 0)
        tr_start, tr_end = pos, pos + train_len
        pos = tr_end
        segs.append((tr_start, tr_end))
        p_start, p_end = pos, pos + p
        pos = p_end
        t_start, t_end = pos, pos + test
        pos = t_end
        if f < F - 1:
            e_start, e_end = pos, pos + e
            pos = e_end
        else:
            e_start = e_end = pos
        # A fold's training set is its own segment PLUS every earlier
        # segment: expanding, and never containing a bar that was scored
        # out of sample in any fold.
        folds.append({"fold": f, "train_segments": list(segs),
                      "purge_band": (p_start, p_end),
                      "embargo_band": (e_start, e_end),
                      "test": (t_start, t_end)})
    return folds, p, e, notes


def _fold_train_index(plan_folds, fold_no):
    """
    Bar indices used as in-sample evidence for a fold: its own train
    segment plus every earlier train segment. Test blocks and the
    purge/embargo bands are never included.
    """
    idx = []
    for (a, b) in plan_folds[fold_no]["train_segments"]:
        idx.extend(range(a, b))
    return idx


def _check_disjointness(used_folds, train_index, purge_applied, embargo_applied):
    """
    Verify the structural invariants of the fold layout and REPORT every
    violation -- a violation is surfaced, never swallowed.

    (a) no date appears in more than one out-of-sample block
    (b) no out-of-sample bar was in the training set of its own fold
    (c) with embargo > 0, no training bar of fold f+1 falls inside fold
        f's test block or inside fold f's embargo band
    (d) with purge applied, no training bar of ANY fold falls inside ANY
        test block
    """
    violations = []
    checks = []
    test_sets = {pf["fold"]: set(range(pf["test"][0], pf["test"][1]))
                  for pf in used_folds}
    train_sets = {pf["fold"]: set(train_index.get(pf["fold"], ()))
                  for pf in used_folds}
    keys = sorted(test_sets)

    # (a) OOS blocks pairwise disjoint
    clashes = []
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            shared = test_sets[keys[i]] & test_sets[keys[j]]
            if shared:
                clashes.append({"folds": [keys[i], keys[j]],
                                "n_bars": len(shared)})
    checks.append({"id": "a_oos_blocks_disjoint", "applicable": True,
                   "ok": not clashes, "detail": clashes})
    if clashes:
        violations.append({"check": "a_oos_blocks_disjoint",
                           "detail": f"out-of-sample blocks overlap: {clashes}"})

    # (b) own-fold training set contains none of its own OOS bars
    own = []
    for f in keys:
        shared = train_sets[f] & test_sets[f]
        if shared:
            own.append({"fold": f, "n_bars": len(shared)})
    checks.append({"id": "b_no_oos_bar_in_own_training", "applicable": True,
                   "ok": not own, "detail": own})
    if own:
        violations.append({"check": "b_no_oos_bar_in_own_training",
                           "detail": f"fold training set contains its own "
                                     f"out-of-sample bars: {own}"})

    # (c) embargo band after fold f must not be in fold f+1's training set
    emb = []
    if embargo_applied:
        by_fold = {pf["fold"]: pf for pf in used_folds}
        for f in keys:
            nxt = f + 1
            if nxt not in train_sets:
                continue
            band = set()
            pb = by_fold[f].get("embargo_band")
            if pb:
                band = set(range(pb[0], pb[1]))
            shared = train_sets[nxt] & (test_sets[f] | band)
            if shared:
                emb.append({"fold": nxt, "after_fold": f, "n_bars": len(shared)})
    checks.append({"id": "c_embargo_band_not_in_next_training",
                   "applicable": bool(embargo_applied), "ok": not emb,
                   "detail": emb})
    if emb:
        violations.append({"check": "c_embargo_band_not_in_next_training",
                           "detail": f"embargo band leaked into the next "
                                     f"fold's training set: {emb}"})

    # (d) no training bar of any fold inside any test block
    purge = []
    if purge_applied:
        for g in keys:
            for f in keys:
                shared = train_sets[g] & test_sets[f]
                if shared:
                    purge.append({"train_fold": g, "test_fold": f,
                                  "n_bars": len(shared)})
    checks.append({"id": "d_no_training_bar_inside_any_test_block",
                   "applicable": bool(purge_applied), "ok": not purge,
                   "detail": purge})
    if purge:
        violations.append({"check": "d_no_training_bar_inside_any_test_block",
                           "detail": f"training bars fall inside a test "
                                     f"block: {purge}"})

    return {"ok": not violations, "violations": violations, "checks": checks}


def walk_forward(spec, n_folds=5, train_frac=0.6, n_variants=1, verbose=True,
                 purge_bars=None, embargo_bars=None):
    """
    Chronological walk-forward with a PURGE and an EMBARGO, plus an
    explicit disjointness assertion over the fold layout.

    purge_bars   bars immediately before each test block that are removed
                 from every training set (their label horizon could reach
                 into the test block). Default: the largest lookback among
                 the spec's feature parameters.
    embargo_bars bars immediately after each test block that are removed
                 from every later training set. Default: the same value.

    Passing purge_bars=0 AND embargo_bars=0 explicitly selects the legacy
    layout -- contiguous test blocks and an expanding training window -- so
    existing comparisons remain possible.

    Signals here use GLOBAL parameters (not re-optimised per fold) unless
    the spec says otherwise -- this tests whether the HYPOTHESIS holds out
    of sample, not whether a parameter optimiser can re-fit it.
    """
    base = run_spec(spec, verbose=False)
    if "error" in base:
        return {"error": base["error"]}
    panel = base["panel"]
    dates = panel.dates
    n = len(dates)
    if n < (n_folds + 1) * 60:
        _err = f"not enough bars for {n_folds} folds ({n} bars)"
        if verbose:
            print(f"  walk-forward COULD NOT BE RUN: {_err}")
        return {"error": _err}

    horizon = _spec_label_horizon(spec)
    explicit = (purge_bars is not None) or (embargo_bars is not None)
    legacy = bool(explicit and purge_bars == 0 and embargo_bars == 0)
    purge_req = horizon if purge_bars is None else max(0, int(purge_bars))
    embargo_req = horizon if embargo_bars is None else max(0, int(embargo_bars))

    notes = []
    if legacy:
        plan = _plan_legacy_folds(n, n_folds)
        purge_used = embargo_used = 0
    else:
        plan, purge_used, embargo_used, notes = _plan_purged_folds(
            n, n_folds, purge_req, embargo_req, warmup=horizon)
        if plan is None:
            _err = (f"not enough bars for a purged/embargoed walk-forward "
                    f"({n} bars)")
            if verbose:
                print(f"  walk-forward COULD NOT BE RUN: {_err}")
            return {"error": _err}

    base_rets = base["sim"]["returns"]
    oos_rets = [None] * n
    fold_info = []
    is_sharpes, oos_sharpes = [], []
    train_index = {}
    used_folds = []
    oos_live_rets = []

    for pf in plan:
        te_start, te_end = pf["test"]
        if te_start >= n or te_end - te_start < MIN_TEST_BARS:
            continue
        tr_idx = _fold_train_index(plan, pf["fold"])
        train_index[pf["fold"]] = tr_idx
        train_rets = _clean([base_rets[i] for i in tr_idx if i < len(base_rets)])
        is_sh = sharpe(train_rets, periods_per_year=252)

        # OOS: re-simulate the test window from fresh capital. Features are
        # taken from the FULL panel and sliced, so the fold is not COLD: a
        # feature at bar i legitimately uses data from before bar i, which is
        # history rather than leakage. The base run computes its features the
        # same way, so the fold measures the SAME strategy as the headline.
        # (The earlier implementation recomputed features on the sliced
        # panel, which left the first `horizon` bars of every fold without a
        # signal - a 252-bar lookback on a short fold produced an all-None
        # signal and an artificial refutation.)
        fold_bundle = _resimulate_window(spec, panel, te_start, te_end,
                                          full_features=base.get("features"))
        if "error" in fold_bundle:
            continue
        oos_rets_seg = _clean(fold_bundle["returns"])
        oos_sh = sharpe(oos_rets_seg, periods_per_year=252)
        # There is no cold prefix any more, so the live window IS the test
        # window. The two OOS measures coincide; the live-basis series is
        # kept because it is what the retention check and the rank-inversion
        # statistic consume, and both are like-for-like comparisons.
        warm = 0
        live_seg = oos_rets_seg
        oos_live_sh = oos_sh
        # The IS and OOS lists are appended together so the two lists stay
        # index-aligned for the rank-inversion statistic below.
        is_sharpes.append(is_sh)
        oos_sharpes.append(oos_live_sh)
        oos_live_rets.extend(live_seg)
        for j, r in enumerate(fold_bundle["returns"]):
            idx = te_start + j
            if idx < n:
                oos_rets[idx] = r
        used_folds.append(pf)
        fold_info.append({
            "fold": pf["fold"],
            "train_bars": len(tr_idx),
            "train_range": [dates[tr_idx[0]], dates[tr_idx[-1]]] if tr_idx else None,
            "test_bars": te_end - te_start,
            "warmup_bars": warm,
            "live_bars": len(live_seg),
            "test_range": [dates[te_start], dates[min(te_end, n) - 1]],
            "purge_bars": purge_used,
            "embargo_bars": embargo_used,
            "is_sharpe": is_sh,
            "oos_sharpe": oos_sh,
            "oos_sharpe_live": oos_live_sh,
            "oos_cagr": cagr_from_returns(oos_rets_seg),
            "oos_trades": len(fold_bundle.get("trades", [])),
        })
        if verbose:
            print(f"  fold {pf['fold']}: IS sh={_f(is_sh)} (n={len(tr_idx)})  "
                  f"OOS sh={_f(oos_sh)}  "
                  f"({dates[te_start]}..{dates[min(te_end,n)-1]})  "
                  f"purge={purge_used} embargo={embargo_used}")

    pooled = _clean(oos_rets)
    pooled_sh = sharpe(pooled, periods_per_year=252)
    pooled_live_sh = sharpe(oos_live_rets, periods_per_year=252)
    final_is = is_sharpes[-1] if is_sharpes else None
    # OOS retention: how much of the final in-sample Sharpe survived out of
    # sample. The out-of-sample side is measured on live bars only, so the
    # comparison is like-for-like; the ratio is the documented "OOS << IS"
    # refutation criterion.
    retention = None
    if pooled_live_sh is not None and final_is is not None and final_is > 0:
        retention = pooled_live_sh / final_is
    pbo = pbo_proxy(is_sharpes, oos_sharpes)
    disjoint = _check_disjointness(used_folds, train_index,
                                   purge_applied=purge_used > 0,
                                   embargo_applied=embargo_used > 0)
    if verbose:
        # Anything the planner had to give up MUST be visible. A fold count
        # quietly reduced from 5 to 3, or a purge band quietly halved, is a
        # weaker leak guarantee than the report implies.
        for _nt in notes:
            print(f"  NOTE: {_nt}")
        _chk = disjoint.get("checks") or []
        _bad = [c for c in _chk if not c.get("ok", True)]
        print(f"  disjointness: {'PASS' if not _bad else 'FAIL'} "
              f"({len(_chk)} checks: "
              + ", ".join(c.get("id", "?") for c in _chk) + ")")
        for _c in _bad:
            print(f"    VIOLATION: {_c.get('id')}: {_c.get('detail')}")
        for _v in (disjoint.get("violations") or []):
            print(f"    VIOLATION: {_v}")
    return {
        "n_folds": len(fold_info),
        "folds": fold_info,
        "mode": "legacy" if legacy else "purged_embargoed",
        "label_horizon": horizon,
        "purge_bars": purge_used,
        "embargo_bars": embargo_used,
        "purge_bars_requested": purge_req,
        "embargo_bars_requested": embargo_req,
        "notes": notes,
        "disjointness": disjoint,
        "oos_rets": pooled,
        "oos_sharpe": pooled_sh,
        "oos_sharpe_live": pooled_live_sh,
        "oos_cagr": cagr_from_returns(pooled),
        "oos_max_drawdown": max_drawdown_pct(pooled),
        "oos_trades_total": sum(f.get("oos_trades", 0) for f in fold_info),
        "oos_profitable_folds": sum(1 for f in fold_info if (f["oos_sharpe"] or -9) > 0),
        "is_sharpes": is_sharpes,
        "oos_sharpes": oos_sharpes,
        "final_is_sharpe": final_is,
        "oos_retention": retention,
        "pbo": pbo["inversion_rate"] if pbo else None,
        "pbo_detail": pbo,
    }


def _resimulate_window(spec, panel, start_idx, end_idx, full_features=None):
    """
    Re-run the strategy on panel dates [start_idx, end_idx) only.

    FEATURES ARE COMPUTED ON THE FULL PANEL AND THEN SLICED.

    Computing them on the sliced panel instead makes the fold COLD: the first
    `horizon` bars carry no signal at all, so a 200-bar lookback yields 200
    flat bars and a 252-bar lookback on a short fold yields an all-None
    signal - an empty fold, and therefore an artificial refutation. That is
    not conservatism, it is a bug. A feature at bar i legitimately uses data
    from before bar i; that is history, not leakage, and the base run
    computes features on the full panel. A fold that does not is measuring a
    DIFFERENT strategy from the headline number.

    `full_features` is passed in from the base run so the fold reuses the
    EXACT values the base run used, not a recomputation that might differ.
    """
    sub_dates = panel.dates[start_idx:end_idx]
    if len(sub_dates) < 30:
        return {"error": "window too short"}
    if full_features is None:
        full_features = compute_features(panel, spec["features"])
    features = {name: {s: (list(vals[start_idx:end_idx]) if vals else [])
                       for s, vals in by_sym.items()}
                for name, by_sym in full_features.items()}
    sub = Panel(sub_dates, panel.symbols, panel.bars, panel.costs, panel.meta)
    # Resolve the traded feature the SAME way the base run does, or the folds
    # would trade a different feature than the headline result.
    signal = compute_signal(sub, features, resolve_signal_spec(spec))
    target = construct_target_weights(sub, signal, **spec.get("portfolio", {}))
    res = simulate(sub, target,
                   initial_capital=spec.get("capital", 100000.0),
                   fill_at=spec.get("execution", {}).get("fill_at", "open"),
                   cost_multiplier=spec.get("execution", {}).get("cost_multiplier", 1.0),
                   max_participation=spec.get("execution", {}).get("max_participation", 0.10),
                   flat_at_close=spec.get("execution", {}).get("flat_at_close", False),
                   trade_mask=rebalance_mask(sub, spec.get("portfolio", {}).get("rebalance", "daily")))
    return res


def _f(x, fmt="{:.2f}"):
    return "n/a" if x is None else fmt.format(x)


# --------------------------------------------------------------------------
# Gauntlet stage: cost stress
# --------------------------------------------------------------------------


def cost_stress(spec, multipliers=(1.0, 1.5, 2.0, 3.0), verbose=True):
    """Does the edge survive punitive costs? An edge that dies at 2x is fragile."""
    out = []
    base = run_spec(spec, verbose=False)
    if "error" in base:
        return {"error": base["error"]}
    for m in multipliers:
        s2 = _deep_copy_spec(spec)
        s2.setdefault("execution", {})["cost_multiplier"] = m
        b = run_spec(s2, verbose=False)
        mt = compute_metrics(b)
        out.append({"cost_multiplier": m, "sharpe": mt["sharpe"], "cagr": mt["cagr"],
                    "n_trades": mt["n_trades"]})
        if verbose:
            print(f"  cost x{m}: sharpe={_f(mt['sharpe'])} cagr={_f(mt['cagr'],'{:.1%}')}")
    return {"stress": out}


def _deep_copy_spec(spec):
    return json.loads(json.dumps(spec))


# --------------------------------------------------------------------------
# Gauntlet stage: parameter sensitivity (plateau vs spike)
# --------------------------------------------------------------------------


def parameter_sensitivity(spec, verbose=True):
    """
    Perturb the numeric parameters of EVERY declared feature by +-25% and see
    how the in-sample Sharpe responds. A real edge is a broad plateau; a spike
    is curve-fitting.

    Sweeping only the first feature (the earlier behaviour) misses the
    parameters that actually matter and can silently perturb a degenerate
    value -- e.g. a lookback of 1 becomes 1 again under a -25% change, so the
    sweep reported the same Sharpe twice and tested nothing.
    """
    base = run_spec(spec, verbose=False)
    if "error" in base:
        return {"error": base["error"]}
    base_sh = sharpe(_clean(base["sim"]["returns"]), periods_per_year=252)
    results = []
    for fi, feat in enumerate(spec.get("features") or []):
        params = dict(feat.get("params", {}))
        numeric_keys = [k for k in params if isinstance(params[k], (int, float))
                        and "direction" not in k]
        for k in numeric_keys:
            base_val = params[k]
            for delta in (-0.25, 0.25):
                new_val = max(1, int(round(base_val * (1 + delta))))
                if new_val == base_val:
                    # Degenerate perturbation (e.g. lookback 1 -> 1). Skipping
                    # it is honest: a no-op is not evidence of a plateau.
                    continue
                s2 = _deep_copy_spec(spec)
                s2["features"][fi]["params"][k] = new_val
                b = run_spec(s2, verbose=False)
                if "error" in b:
                    continue
                sh = sharpe(_clean(b["sim"]["returns"]), periods_per_year=252)
                results.append({"feature": feat.get("name"), "param": k,
                                "value": new_val, "base_value": base_val,
                                "sharpe": sh,
                                "sharpe_ratio_to_base": safe_div(sh, base_sh)
                                if (sh and base_sh) else None})
                if verbose:
                    print(f"  {feat.get('name')}.{k}={new_val}: sharpe={_f(sh)} "
                          f"({_f(safe_div(sh, base_sh),'{:.0%}')} of base)")
    # Stability = fraction of perturbations that keep the sign of the edge
    same_sign = sum(1 for r in results
                    if (r["sharpe"] is not None and base_sh is not None
                        and (r["sharpe"] > 0) == (base_sh > 0)))
    stability = same_sign / len(results) if results else None
    if not results:
        return {"base_sharpe": base_sh, "perturbations": [],
                "stability": None,
                "interpretation": "no perturbable parameter found - the sweep "
                                  "tested nothing and is NOT evidence of robustness"}
    return {"base_sharpe": base_sh, "perturbations": results,
            "stability": stability,
            "interpretation": ("broad plateau - robust" if (stability or 0) >= 0.75
                               else "narrow/unstable - possible curve-fit")}


# --------------------------------------------------------------------------
# Gauntlet stage: Monte Carlo trade reshuffle
# --------------------------------------------------------------------------


def monte_carlo(bundle, n_runs=5000, rf_annual=0.0, seed=7, verbose=True):
    """
    Bootstrap over the realised TRADE SEQUENCE, resampled WITH REPLACEMENT.

    What it actually does: it draws `n` trades at random WITH REPLACEMENT
    from the realised trade ledger (rng.randrange), so a trade can appear
    several times and another not at all. Each resample is replayed as an
    equity curve (starting from the initial capital, each sampled trade's
    P&L added in sequence) and the resulting per-trade return series is
    measured. This is a bootstrap of the trade sequence -- NOT a reshuffle
    of the observed order (no ordering of the realised trades is preserved
    or permuted) and NOT a resampling of the bar-level returns.

    BASIS WARNING: the Sharpe this stage reports is a PER-TRADE Sharpe.
    The return series is one observation per sampled trade and it is
    annualised by the observed trade frequency (trades per year), whereas
    the headline Sharpe everywhere else in this report is a PER-BAR Sharpe
    annualised at 252. The two are not comparable and must not be read as
    the same number. The percentile labels say so explicitly.
    """
    sim = bundle["sim"]
    trades = [t["pnl"] for t in sim["trades"] if t["pnl"] is not None]
    n = len(trades)
    if n < 20:
        return {"error": f"only {n} trades — Monte Carlo needs >=20"}
    initial = bundle.get("spec", {}).get("capital", 100000.0)
    # Annualisation factor: how many trades the strategy completes per year.
    n_bars = sum(1 for r in sim["returns"] if r is not None)
    years = max(n_bars / 252.0, 1e-6)
    periods_per_year = max(n / years, 1.0)

    rng = random.Random(seed)
    sharpes, finals, prob_loss_dd = [], [], []
    for _ in range(n_runs):
        equity = initial
        rets = []
        for _ in range(n):
            pnl = trades[rng.randrange(n)]
            rets.append(pnl / equity if equity > 0 else 0.0)
            equity += pnl
        sd = stdev(rets)
        if sd and sd > 0:
            sharpes.append((mean(rets) / sd) * math.sqrt(periods_per_year))
        finals.append(equity)
        prob_loss_dd.append(max_drawdown_pct(rets))

    ss = sorted(sharpes)
    fs = sorted(finals)
    prob_positive = sum(1 for f in fs if f > initial) / len(fs) if fs else None
    p5_dd = quantile(sorted(prob_loss_dd), 0.95) if prob_loss_dd else None
    if verbose:
        print(f"  MC runs={n_runs} trades={n} ({periods_per_year:.0f}/yr) "
              f"P(profitable)={_f(prob_positive,'{:.1%}')} "
              f"per-trade Sharpe p5/p50/p95={_f(quantile(ss,0.05))}/"
              f"{_f(quantile(ss,0.5))}/{_f(quantile(ss,0.95))}  "
              f"median-DD={_f(quantile(sorted(prob_loss_dd),0.5),'{:.1%}')}")
    return {"n_runs": n_runs, "n_trades": n, "trades_per_year": periods_per_year,
            "prob_profitable": prob_positive,
            "sharpe_basis": "per-trade (annualised at the observed trade "
                            "frequency) — NOT comparable to the per-bar "
                            "Sharpe elsewhere in this report",
            "resampling": "trade sequence resampled WITH REPLACEMENT "
                          "(bootstrap); the realised trade ORDER is not "
                          "preserved or permuted",
            "sharpe_p5": quantile(ss, 0.05), "sharpe_p50": quantile(ss, 0.5),
            "sharpe_p95": quantile(ss, 0.95),
            "prob_sharpe_below_zero": (sum(1 for x in sharpes if x < 0) / len(sharpes)
                                       if sharpes else None),
            "median_max_drawdown": quantile(sorted(prob_loss_dd), 0.5),
            "worst5pct_max_drawdown": p5_dd,
            "final_p5": quantile(fs, 0.05), "final_p50": quantile(fs, 0.50),
            "interpretation": "Distribution of outcomes over bootstrap "
                              "resamples of the realised trade sequence "
                              "(sampled WITH REPLACEMENT), each resample "
                              "replayed as an equity curve. p5 below 0 means "
                              "a fifth of resamples lose money."}


# --------------------------------------------------------------------------
# Gauntlet stage: subperiod / regime decomposition
# --------------------------------------------------------------------------


def subperiod_and_regime(bundle, verbose=True):
    """
    Decompose performance by calendar year and by volatility regime, to test
    whether the edge concentrates in one lucky period.
    """
    panel = bundle["panel"]
    rets = bundle["sim"]["returns"]
    # By calendar year. Non-overlapping by construction (bars map 1:1 to dates).
    by_year = {}
    for i, d in enumerate(panel.dates[:len(rets)]):
        if rets[i] is None:
            continue
        by_year.setdefault(d[:4], []).append(rets[i])
    years = [{"year": y,
              "return": math.prod(1 + r for r in by_year[y]) - 1,
              "sharpe": sharpe(by_year[y], periods_per_year=252),
              "bars": len(by_year[y])} for y in sorted(by_year)]

    # By realised-vol regime (use market-wide avg vol across symbols)
    n = panel.n
    port_ret = _clean(rets)
    # compute a trailing vol series for the portfolio
    vol_series = []
    for i in range(len(rets)):
        if rets[i] is None:
            vol_series.append(None)
        else:
            w = [r for r in rets[max(0, i - 20):i + 1] if r is not None]
            vol_series.append(stdev(w) if len(w) > 5 else None)
    vols = _clean(vol_series)
    if vols:
        med_vol = quantile(sorted(vols), 0.5)
        hi_vol_rets = [rets[i] for i in range(len(rets))
                       if rets[i] is not None and vol_series[i] is not None and vol_series[i] >= med_vol]
        lo_vol_rets = [rets[i] for i in range(len(rets))
                       if rets[i] is not None and vol_series[i] is not None and vol_series[i] < med_vol]
        regimes = {
            "high_vol": {"sharpe": sharpe(hi_vol_rets), "bars": len(hi_vol_rets)},
            "low_vol": {"sharpe": sharpe(lo_vol_rets), "bars": len(lo_vol_rets)},
        }
    else:
        regimes = {}
    pos_years = sum(1 for y in years if (y["return"] or 0) > 0)
    if verbose:
        print(f"  years positive: {pos_years}/{len(years)}")
        for y in years:
            print(f"    {y['year']}: ret={_f(y['return'],'{:.1%}')} sh={_f(y['sharpe'])}")
    return {"by_year": years, "positive_years": pos_years,
            "total_years": len(years), "by_regime": regimes,
            "edge_concentration": (max((y["return"] or 0) for y in years) >
                                   (3 * (cagr_from_returns(port_ret) or 0))
                                   if years else None)}


# --------------------------------------------------------------------------
# Gauntlet stage: capacity
# --------------------------------------------------------------------------


def capacity_estimate(bundle, verbose=True):
    """
    Rough capacity: the capital at which per-symbol traded notional would
    exceed `p` (default 5%) of that instrument's median daily dollar volume.

    Turnover is linear in capital, so capacity scales inversely with the
    current trading rate. The binding constraint is the MOST ILLIQUID symbol
    actually traded, not the sum across all symbols.
    """
    panel = bundle["panel"]
    mt = compute_metrics(bundle)
    turnover = mt.get("turnover_total") or 0.0
    n_bars = mt["n_bars"] or 1
    years = n_bars / 252.0
    # Only symbols that were actually traded constrain capacity.
    capital0 = bundle.get("spec", {}).get("capital", 100000.0)
    traded = [s for s in panel.symbols
              if any(abs(w) > 0 for w in bundle["target_weights"].get(s, []))]
    if not traded or turnover <= 0:
        return {"est_capacity_usd": None, "reason": "no traded symbols or zero turnover",
                "turnover_per_year": turnover}

    per_symbol_turnover_per_year = turnover / years / len(traded)
    limits = []
    for s in traded:
        dollar_vol = [((panel.close[s][i] or 0) * (panel.vol[s][i] or 0)) for i in range(panel.n)]
        adv = quantile(sorted([d for d in dollar_vol if d > 0]), 0.5) if any(d > 0 for d in dollar_vol) else 0
        if adv > 0 and per_symbol_turnover_per_year > 0:
            # Turnover scales linearly with capital. Solve for the capital C at
            # which this symbol's DAILY traded notional hits 5% of its ADV:
            #   (T * C / capital0) / 252 = 0.05 * ADV
            #   => C = 0.05 * ADV * 252 * capital0 / T
            cap_c = (0.05 * adv * 252.0) * capital0 / per_symbol_turnover_per_year
            limits.append((s, cap_c))
    if not limits:
        return {"est_capacity_usd": None, "reason": "no ADV data"}
    binding_sym, cap = min(limits, key=lambda kv: kv[1])
    if verbose:
        print(f"  est. capacity ~${cap:,.0f} (binding symbol: {binding_sym}, "
              f"5% of ADV participation)")
    return {"est_capacity_usd": cap, "binding_symbol": binding_sym,
            "per_symbol_limits": {s: v for s, v in limits},
            "turnover_per_year": turnover,
            "n_traded_symbols": len(traded),
            "note": "Assumes costs scale linearly; real capacity is lower once "
                    "market impact is modelled."}


# --------------------------------------------------------------------------
# Gauntlet stage: causality audit (no look-ahead)
# --------------------------------------------------------------------------


def causality_audit(spec, verbose=True):
    """
    Independent verification that the pipeline is causal:
      - Build the panel, compute features on the FULL panel.
      - Truncate the panel to the first half of dates, recompute features.
      - Features in the first half must be IDENTICAL. Any difference means a
        feature used future data (e.g. centered rolling windows, full-sample
        normalisation).
    """
    base = run_spec(spec, verbose=False)
    if "error" in base:
        return {"error": base["error"], "causal": None}
    panel = base["panel"]
    half = panel.n // 2
    full_feats = base["features"]
    sub = Panel(panel.dates[:half], panel.symbols, panel.bars, panel.costs, panel.meta)
    sub_feats = compute_features(sub, spec["features"])
    violations = []
    for fname in full_feats:
        for s in panel.symbols:
            a = full_feats[fname][s][:half]
            b = sub_feats[fname][s]
            for i, (x, y) in enumerate(zip(a, b)):
                if x is None and y is None:
                    continue
                if (x is None) != (y is None):
                    violations.append({"feature": fname, "symbol": s, "bar": i})
                    break
                if x is not None and abs(x - y) > 1e-9:
                    violations.append({"feature": fname, "symbol": s, "bar": i,
                                       "full": x, "truncated": y})
                    break
    causal = len(violations) == 0
    if verbose:
        print(f"  causality audit: {'PASS (no look-ahead)' if causal else 'FAIL'}"
              + (f" — {len(violations)} violation(s)" if violations else ""))
    return {"causal": causal, "violations": violations[:20]}


# --------------------------------------------------------------------------
# Gauntlet stage: cross-market replication
# --------------------------------------------------------------------------


def cross_market_replication(spec, alt_universe, verbose=True):
    """
    If the edge is economic, it should appear (perhaps weaker) in a different
    market. Port the signal to an alternative universe and measure OOS-ish
    in-sample Sharpe there.
    """
    results = []
    for uni in alt_universe:
        s2 = _deep_copy_spec(spec)
        s2["universe"] = {"preset": uni, "symbols": []}
        b = run_spec(s2, verbose=False)
        if "error" in b:
            results.append({"universe": uni, "error": b["error"]})
            continue
        mt = compute_metrics(b)
        results.append({"universe": uni, "sharpe": mt["sharpe"],
                        "cagr": mt["cagr"], "n_trades": mt["n_trades"]})
        if verbose:
            print(f"  {uni}: sharpe={_f(mt['sharpe'])} cagr={_f(mt['cagr'],'{:.1%}')}")
    ok = [r for r in results if r.get("sharpe") is not None and r["sharpe"] > 0]
    return {"results": results, "n_replicated": len(ok), "n_tested": len(results),
            "replication_rate": (len(ok) / len(results)) if results else None}


# --------------------------------------------------------------------------
# Full gauntlet + verdict
# --------------------------------------------------------------------------

THRESHOLDS = {
    "min_trades": 30,
    "min_oos_sharpe": 0.5,       # the bar for "there is an edge"
    "min_in_sharpe": 0.7,
    "oos_retention_min": 0.4,    # OOS sharpe should retain >=40% of IS
    "oos_sharpe_strong": 1.0,     # an OOS sharpe this strong survives a
                                  # cost-stress failure as FRAGILE, not a
                                  # refutation
    "cost_stress_min_x": 2.0,    # edge must survive 2x costs
    "mc_prob_min": 0.85,
    "param_stability_min": 0.6,
    "min_positive_year_frac": 0.5,
    "min_profitable_fold_frac": 0.6,   # at least 60% of folds must be profitable
    "t_stat_min": 2.0,
}


def _gross_interpretation(gross_sh, net_sh):
    """
    Plain-English reading of the gross-vs-net gap, covering all four
    quadrants. A strategy can fail because it has no edge, or because its
    edge is real but too small to trade -- those are different findings and
    the report must not blur them.
    """
    g = gross_sh if gross_sh is not None else 0.0
    n = net_sh if net_sh is not None else 0.0
    if g <= 0 and n <= 0:
        return ("no positive edge even before costs - the signal itself has "
                "no exploitable expectancy in this sample, so costs are not "
                "the primary problem")
    if g > 0 and n <= 0:
        return ("there IS a positive gross-of-costs edge, but it is too small "
                "to survive trading costs - the strategy may be real but is "
                "untradeable at this size and frequency")
    if g > 0 and n > 0:
        drag = (g - n) / g if g else 0.0
        if drag < 0.15:
            return (f"the edge survives costs with little erosion "
                    f"({drag:.0%} of the gross Sharpe lost to costs) - "
                    f"costs are not the constraint")
        return (f"the edge survives costs but is materially eroded "
                f"({drag:.0%} of the gross Sharpe lost to costs) - a "
                f"lower-turnover or larger-size version of this strategy "
                f"would keep more of it")
    return "cost decomposition did not complete"


def full_gauntlet(spec, n_variants=1, walk_forward_folds=5,
                  alt_universes=None, skip=(), verbose=True):
    """
    Run every gauntlet stage, collect a scorecard, and return a verdict.
    `skip` lets you omit expensive stages. `n_variants` = how many parameter
    variants were tried while developing (for the deflated-Sharpe penalty).
    """
    if verbose:
        print("\n" + "=" * 66)
        print("STAGE 8 — VALIDATION GAUNTLET")
        print("=" * 66)

    # --- base run
    base = run_spec(spec, verbose=verbose)
    if "error" in base:
        return {"verdict": "INCONCLUSIVE", "error": base["error"]}
    mt = compute_metrics(base)
    if verbose:
        print("\n[1] BASE METRICS")
        print(f"    bars={mt['n_bars']}  trades={mt['n_trades']}  "
              f"sharpe={_f(mt['sharpe'])}  cagr={_f(mt['cagr'],'{:.1%}')}  "
              f"maxDD={_f(mt['max_drawdown'],'{:.1%}')}")

    scorecard = {"base": mt}  # type: dict
    # The instruments ACTUALLY in the panel, after the liquidity screen. The
    # disclosure sheet reports "N instruments" from this list -- a preset NAME
    # is not a count, and len("us_equity_broad") would report a fictional
    # 15-instrument universe for a 4-instrument run.
    scorecard["symbols"] = list(base.get("symbols_used") or [])
    flags = []

    # --- statistical power
    if mt["n_trades"] < THRESHOLDS["min_trades"]:
        flags.append(f"only {mt['n_trades']} trades (need >={THRESHOLDS['min_trades']})")

    # --- t-stat of the daily return series (the base metric's own power check)
    if mt.get("t_stat") is not None and mt["t_stat"] < THRESHOLDS["t_stat_min"]:
        flags.append(f"t-stat {mt['t_stat']:.2f} below {THRESHOLDS['t_stat_min']} "
                     f"- the return series is not distinguishable from noise")

    # --- walk-forward
    if "walkforward" not in skip and verbose:
        print("\n[2] WALK-FORWARD OUT-OF-SAMPLE")
    wf = walk_forward(spec, n_folds=walk_forward_folds, verbose=verbose) \
        if "walkforward" not in skip else {"error": "skipped"}
    scorecard["walk_forward"] = wf
    if "error" in wf:
        flags.append(f"walk-forward: {wf['error']}")
    else:
        # Fold consistency: a pooled number can clear the bar while individual
        # folds are negative, which means the result is carried by a few
        # periods. That is a regime bet, not a stable edge.
        nf = wf.get("n_folds") or 0
        pf = wf.get("oos_profitable_folds") or 0
        if nf and (pf / nf) < THRESHOLDS["min_profitable_fold_frac"]:
            if pf == 0:
                flags.append(f"0/{nf} walk-forward folds profitable - the edge "
                             "is absent in every out-of-sample period, not "
                             "merely concentrated in a few")
            else:
                flags.append(f"only {pf}/{nf} walk-forward folds profitable "
                             "(edge concentrated in a few periods)")
        # OOS retention: the documented "OOS << IS" refutation criterion.
        # The out-of-sample Sharpe is compared with the FINAL in-sample
        # Sharpe; if the edge barely survives out of sample, the hypothesis
        # is not supported even when the pooled number clears the bar.
        retention = wf.get("oos_retention")
        if retention is not None and retention < THRESHOLDS["oos_retention_min"]:
            flags.append(f"OOS Sharpe retains only {retention:.0%} of the "
                         f"in-sample Sharpe (need "
                         f"{THRESHOLDS['oos_retention_min']:.0%}) - the edge "
                         f"degrades out of sample")
        # A disjointness violation means the out-of-sample measurement is
        # contaminated. It is surfaced, never swallowed.
        disjoint = wf.get("disjointness") or {}
        if disjoint and not disjoint.get("ok", True):
            for v in disjoint.get("violations", []):
                flags.append(f"walk-forward disjointness VIOLATED "
                             f"({v.get('check')}): {v.get('detail')}")

    # --- gross vs net decomposition
    # A strategy can fail for two very different reasons: it has no edge at
    # all, or it has an edge too small to survive costs. The verdict reads
    # the same either way, so the report must state which one it is.
    gross_spec = _deep_copy_spec(spec)
    gross_spec.setdefault("execution", {})["cost_multiplier"] = 0.0
    gb = run_spec(gross_spec, verbose=False)
    gross = None
    if "error" not in gb:
        gm = compute_metrics(gb)
        net_mt = scorecard.get("base", {})
        gross = {
            "gross_sharpe": gm["sharpe"], "gross_cagr": gm["cagr"],
            "net_sharpe": net_mt.get("sharpe"), "net_cagr": net_mt.get("cagr"),
            "costs_paid": net_mt.get("costs_paid"),
            "turnover_total": net_mt.get("turnover_total"),
            "interpretation": _gross_interpretation(gm["sharpe"], net_mt.get("sharpe")),
        }
    scorecard["gross_vs_net"] = gross
    if verbose and gross:
        print(f"  gross-of-costs sharpe={_f(gross['gross_sharpe'])} "
              f"vs net {_f(gross['net_sharpe'])}  ({gross['interpretation']})")

    # --- cost stress
    cs = cost_stress(spec, verbose=verbose) if "coststress" not in skip else {"error": "skipped"}
    scorecard["cost_stress"] = cs
    if "error" not in cs:
        # Does any tested multiplier >= min still show positive sharpe?
        survivors = [r for r in cs["stress"]
                     if r["cost_multiplier"] >= THRESHOLDS["cost_stress_min_x"]
                     and (r["sharpe"] or 0) > 0]
        if not survivors:
            flags.append("edge does not survive 2x cost stress")

    # --- parameter sensitivity
    ps = parameter_sensitivity(spec, verbose=verbose) if "paramsens" not in skip else {"error": "skipped"}
    scorecard["parameter_sensitivity"] = ps
    if "error" not in ps and (ps.get("stability") is not None):
        if ps["stability"] < THRESHOLDS["param_stability_min"]:
            flags.append(f"parameter stability {ps['stability']:.0%} (< {THRESHOLDS['param_stability_min']:.0%})")

    # --- Monte Carlo
    mc = monte_carlo(base, n_runs=3000, verbose=verbose) if "montecarlo" not in skip else {"error": "skipped"}
    scorecard["monte_carlo"] = mc
    if "error" not in mc and (mc.get("prob_profitable") is not None):
        if mc["prob_profitable"] < THRESHOLDS["mc_prob_min"]:
            flags.append(f"Monte Carlo prob(profitable) {mc['prob_profitable']:.0%}")

    # --- subperiod
    sp = subperiod_and_regime(base, verbose=verbose) if "subperiod" not in skip else {"error": "skipped"}
    scorecard["subperiod"] = sp
    if "error" not in sp and sp["total_years"] >= 2:
        frac = sp["positive_years"] / sp["total_years"]
        if frac < THRESHOLDS["min_positive_year_frac"]:
            flags.append(f"only {sp['positive_years']}/{sp['total_years']} profitable years")

    # --- capacity
    cap = capacity_estimate(base, verbose=verbose) if "capacity" not in skip else {"error": "skipped"}
    scorecard["capacity"] = cap

    # --- replication
    if alt_universes and "replication" not in skip:
        if verbose:
            print("\n[8] CROSS-MARKET REPLICATION")
        cr = cross_market_replication(spec, alt_universes, verbose=verbose)
        scorecard["replication"] = cr
        if cr["replication_rate"] is not None and cr["replication_rate"] < 0.5:
            flags.append(f"replicated in only {cr['replication_rate']:.0%} of alt markets")

    # --- causality
    if verbose:
        print("\n[9] CAUSALITY AUDIT (look-ahead check)")
    ca = causality_audit(spec, verbose=verbose) if "causality" not in skip else {"causal": None}
    scorecard["causality"] = ca
    if ca.get("causal") is False:
        flags.append("LOOK-AHEAD DETECTED — feature uses future data")

    # --- deflated Sharpe (data-snooping penalty)
    deflated = deflated_sharpe(mt["sharpe"], mt["n_bars"], n_variants=n_variants)
    scorecard["deflated_sharpe"] = deflated
    if n_variants > 1 and deflated is not None and deflated < 0:
        flags.append(f"deflated Sharpe negative ({deflated:.2f}) — edge likely snooping artifact")

    verdict, reasoning = _verdict(mt, wf, flags, n_variants, gross=gross,
                                  skipped=skip)
    # Record what was NOT run, so nothing downstream can imply a partial
    # validation was a complete one.
    scorecard["skipped_stages"] = _skipped_stage_labels(skip)
    return {"verdict": verdict, "reasoning": reasoning, "flags": flags,
            "scorecard": scorecard, "thresholds": THRESHOLDS}


# Human-readable names for the stages that can be skipped, so a partial run
# can SAY what it did not do instead of implying it did everything.
_SKIP_LABELS = {
    "walkforward": "walk-forward out-of-sample validation",
    "coststress": "cost stress",
    "paramsens": "parameter sensitivity sweep",
    "montecarlo": "Monte Carlo",
    "subperiod": "subperiod and regime analysis",
    "capacity": "capacity estimate",
    "replication": "cross-market replication",
    "causality": "causality audit",
}


def _skipped_stage_labels(skipped):
    """Turn a skip tuple into readable names, in a stable order."""
    if not skipped:
        return []
    order = [k for k in _SKIP_LABELS if k in skipped]
    order += [k for k in skipped if k not in _SKIP_LABELS]
    return [_SKIP_LABELS.get(k, k) for k in order]


def _verdict(mt, wf, flags, n_variants, gross=None, skipped=()):
    sh = mt["sharpe"]
    oos_sh = wf.get("oos_sharpe") if isinstance(wf, dict) else None
    # A walk-forward that ERRORED is not the same as one that was skipped.
    # It must be stated, or a verdict that fell back to in-sample evidence
    # reads as though the out-of-sample test had passed.
    wf_error = wf.get("error") if isinstance(wf, dict) else None
    skipped_labels = _skipped_stage_labels(skipped)
    # A partial run must not claim it passed checks it never ran.
    scope = ""
    if skipped_labels:
        listed = (", ".join(skipped_labels[:-1]) + " and " + skipped_labels[-1]
                  if len(skipped_labels) > 1 else skipped_labels[0])
        scope = (f" NOTE: {listed} "
                 f"{'were' if len(skipped_labels) > 1 else 'was'} NOT run, so "
                 "this is a preliminary read, not a full validation.")
    if wf_error:
        scope = (f" NOTE: the walk-forward out-of-sample validation could NOT "
                 f"be run ({wf_error}), so this verdict rests on IN-SAMPLE "
                 f"evidence only. It is not an out-of-sample result."
                 + scope)

    hard_fail = [f for f in flags if "LOOK-AHEAD" in f]
    if hard_fail:
        return "REFUTED", "Pipeline is not causal; results are unusable."

    if mt["n_trades"] < THRESHOLDS["min_trades"] and not oos_sh:
        return "INCONCLUSIVE", (f"Too few trades ({mt['n_trades']}) to "
                                 f"distinguish signal from noise.{scope}")

    # Primary evidence = OOS Sharpe.
    if oos_sh is not None:
        if oos_sh >= THRESHOLDS["min_oos_sharpe"]:
            if not flags:
                if skipped_labels:
                    return "SUPPORTED", (f"Pooled OOS Sharpe {oos_sh:.2f} clears the "
                                         f"{THRESHOLDS['min_oos_sharpe']} bar and every "
                                         f"robustness check that WAS RUN passed."
                                         f"{scope}")
                return "SUPPORTED", (f"Pooled OOS Sharpe {oos_sh:.2f} clears the "
                                     f"{THRESHOLDS['min_oos_sharpe']} bar and the strategy "
                                     f"survived every robustness check.")
            return "FRAGILE", (f"OOS Sharpe {oos_sh:.2f} is positive but concerns remain: "
                               + "; ".join(flags) + scope)
        # Below the bar. The gross-vs-net decomposition says WHY, which is a
        # materially different finding: "no edge at all" versus "a real edge
        # that costs more to harvest than it yields".
        g_sh = (gross or {}).get("gross_sharpe")
        if g_sh is not None:
            if g_sh <= 0:
                return "REFUTED", (f"Pooled OOS Sharpe {oos_sh:.2f} is below "
                                   f"{THRESHOLDS['min_oos_sharpe']}, and the gross-of-costs "
                                   f"Sharpe is {g_sh:.2f} - there is NO positive edge even "
                                   f"before costs. The signal has no exploitable expectancy "
                                   f"in this sample; this is not a cost problem that better "
                                   f"execution could solve." + scope)
            return "REFUTED", (f"Pooled OOS Sharpe {oos_sh:.2f} is below "
                               f"{THRESHOLDS['min_oos_sharpe']}, while the gross-of-costs "
                               f"Sharpe is {g_sh:.2f} - a real gross edge exists but it is "
                               f"too small to survive costs at this size and frequency."
                               + scope)
        if flags and any("cost" in f for f in flags):
            return "REFUTED", (f"OOS Sharpe {oos_sh:.2f} fails; edge disappears under cost "
                               f"stress - likely a cost artifact, not a real edge." + scope)
        return ("REFUTED", f"OOS Sharpe {oos_sh:.2f} is below "
                f"{THRESHOLDS['min_oos_sharpe']} - no out-of-sample edge." + scope)

    # No OOS available: fall back to IS but penalise heavily.
    if sh is None or sh < THRESHOLDS["min_in_sharpe"]:
        return ("REFUTED",
                f"In-sample Sharpe {sh} does not even clear the in-sample bar."
                + scope)
    return ("FRAGILE", ("Only in-sample evidence available (no walk-forward); "
                        "treat as unproven until OOS tested." + scope))


# --------------------------------------------------------------------------
# Report rendering
# --------------------------------------------------------------------------


def render_report(spec, gauntlet, ctx=None):
    """Human-readable verdict report as markdown."""
    L = []
    L.append(f"# Backtest Report — {spec.get('name', 'untitled')}")
    L.append("")
    hyp = spec.get("hypothesis", "(none provided)")
    L.append(f"**Hypothesis:** {hyp}")
    L.append("")
    L.append(f"## VERDICT: **{gauntlet.get('verdict')}**")
    L.append("")
    L.append(f"{gauntlet.get('reasoning', '')}")
    L.append("")

    sc = gauntlet.get("scorecard", {})
    skipped = sc.get("skipped_stages") or []
    if skipped:
        listed = (", ".join(skipped[:-1]) + " and " + skipped[-1]
                  if len(skipped) > 1 else skipped[0])
        L.append(f"> **PARTIAL VALIDATION.** {listed} "
                 f"{'were' if len(skipped) > 1 else 'was'} NOT run. Every statement "
                 f"below is limited to the checks that were actually executed. "
                 f"This is a preliminary read, not a full validation.")
        L.append("")

    mt = sc.get("base", {})
    if mt:
        L.append("## In-Sample Metrics")
        L.append("")
        # Profit factor, win rate and expectancy come from the trade ledger.
        # The ledger's basis must be stated next to them, or they read as
        # though they were on the same footing as the Sharpe.
        _basis = mt.get("trade_pnl_basis") or "gross"
        _basis_label = "net of costs" if _basis == "net" else "gross of costs"
        L.append("| Metric | Value |")
        L.append("|--------|-------|")
        for k, label, fmt in [
            ("n_trades", "Trades", "{}"), ("n_bars", "Bars", "{}"),
            ("sharpe", "Sharpe", "{:.2f}"), ("sortino", "Sortino", "{:.2f}"),
            ("cagr", "CAGR", "{:.2%}"), ("max_drawdown", "Max Drawdown", "{:.2%}"),
            ("calmar", "Calmar", "{:.2f}"),
            ("profit_factor", f"Profit Factor ({_basis_label})", "{:.2f}"),
            ("win_rate", f"Win Rate ({_basis_label})", "{:.1%}"),
            ("t_stat", "t-stat", "{:.2f}"),
            ("time_in_market", "Time in Market", "{:.0%}"),
            ("costs_paid", "Costs Paid", "${:,.0f}"),
        ]:
            v = mt.get(k)
            L.append(f"| {label} | {fmt.format(v) if v is not None else 'n/a'} |")
        L.append("")

    wf = sc.get("walk_forward", {})
    if wf.get("error"):
        # An errored walk-forward must be STATED. Omitting the section
        # entirely makes a report that fell back to in-sample evidence look
        # exactly like one that passed its out-of-sample test.
        L.append("## Walk-Forward (Out-of-Sample)")
        L.append("")
        L.append(f"> **COULD NOT BE RUN.** {wf['error']}. The verdict below "
                 f"therefore rests on IN-SAMPLE evidence only. It is not an "
                 f"out-of-sample result, and no out-of-sample claim may be "
                 f"made from this run.")
        L.append("")
    elif wf:
        L.append("## Walk-Forward (Out-of-Sample)")
        L.append("")
        L.append(f"- Pooled OOS Sharpe: **{_f(wf.get('oos_sharpe'))}**")
        L.append(f"- Pooled OOS CAGR: {_f(wf.get('oos_cagr'),'{:.2%}')}")
        L.append(f"- Profitable folds: {wf.get('oos_profitable_folds')}/{wf.get('n_folds')}")
        if wf.get("purge_bars") is not None:
            L.append(f"- Purge: {wf.get('purge_bars')} bars · "
                     f"Embargo: {wf.get('embargo_bars')} bars "
                     f"(label horizon {wf.get('label_horizon')})")
        _dis = wf.get("disjointness") or {}
        _dchecks = _dis.get("checks") or []
        _dbad = [c for c in _dchecks if not c.get("ok", True)]
        if _dchecks:
            L.append(f"- Disjointness: **{'PASS' if not _dbad else 'FAIL'}** "
                     f"({len(_dchecks)} checks: "
                     + ", ".join(c.get("id", "?") for c in _dchecks) + ")")
            for _c in _dbad:
                L.append(f"  - VIOLATION: {_c.get('id')}: {_c.get('detail')}")
        for _nt in (wf.get("notes") or []):
            L.append(f"- NOTE: {_nt}")
        if wf.get("oos_retention") is not None:
            L.append(f"- OOS Sharpe retention vs final in-sample: "
                     f"{wf['oos_retention']:.0%}")
        if wf.get("pbo") is not None:
            L.append(f"- PBO proxy (overfit signature): {wf['pbo']:.0%}")
        L.append("")
        L.append("| Fold | Test Window | IS Sharpe | OOS Sharpe | OOS Trades |")
        L.append("|------|-------------|-----------|------------|------------|")
        for f in wf.get("folds", []):
            L.append(f"| {f['fold']} | {f['test_range'][0]} → {f['test_range'][1]} | "
                     f"{_f(f['is_sharpe'])} | {_f(f['oos_sharpe'])} | {f['oos_trades']} |")
        L.append("")

    sp = sc.get("subperiod", {})
    if sp and "error" not in sp and sp.get("by_year"):
        L.append("## By Year")
        L.append("")
        L.append("| Year | Return | Sharpe |")
        L.append("|------|--------|--------|")
        for y in sp["by_year"]:
            L.append(f"| {y['year']} | {_f(y['return'],'{:.1%}')} | {_f(y['sharpe'])} |")
        L.append("")

    ps = sc.get("parameter_sensitivity", {})
    if ps and "error" not in ps and ps.get("perturbations"):
        L.append("## Parameter Sensitivity")
        L.append("")
        L.append(f"- Stability: **{_f(ps.get('stability'),'{:.0%}')}** — {ps.get('interpretation','')}")
        L.append("")
        L.append("| Perturbation | Sharpe |")
        L.append("|--------------|--------|")
        for p in ps["perturbations"]:
            L.append(f"| {p['param']}={p['value']} | {_f(p['sharpe'])} |")
        L.append("")

    gv = sc.get("gross_vs_net", {})
    if gv:
        L.append("## Gross vs Net (cost decomposition)")
        L.append("")
        L.append("| | Sharpe | CAGR |")
        L.append("|----|--------|------|")
        L.append(f"| Gross of costs | {_f(gv.get('gross_sharpe'))} | "
                 f"{_f(gv.get('gross_cagr'),'{:.2%}')} |")
        L.append(f"| Net of costs | {_f(gv.get('net_sharpe'))} | "
                 f"{_f(gv.get('net_cagr'),'{:.2%}')} |")
        L.append("")
        L.append(f"Costs paid: ${gv.get('costs_paid') or 0:,.0f} on "
                 f"${gv.get('turnover_total') or 0:,.0f} of turnover.")
        L.append("")
        L.append(f"> {gv.get('interpretation')}.")
        L.append("")

    cs = sc.get("cost_stress", {})
    if cs and "error" not in cs:
        L.append("## Cost Stress")
        L.append("")
        L.append("| Cost Multiple | Sharpe | CAGR |")
        L.append("|---------------|--------|------|")
        for r in cs["stress"]:
            L.append(f"| {r['cost_multiplier']}x | {_f(r['sharpe'])} | {_f(r['cagr'],'{:.1%}')} |")
        L.append("")

    mc = sc.get("monte_carlo", {})
    if mc and "error" not in mc:
        L.append("## Monte Carlo (trade resampling)")
        L.append("")
        L.append(f"- {mc['n_runs']} resamples over {mc['n_trades']} trades "
                 f"({mc.get('trades_per_year', 0):.0f} trades/year)")
        L.append(f"- P(profitable): **{_f(mc.get('prob_profitable'),'{:.1%}')}**")
        L.append(f"- P(Sharpe < 0): {_f(mc.get('prob_sharpe_below_zero'),'{:.1%}')}")
        L.append(f"- Sharpe p5 / p50 / p95: {_f(mc.get('sharpe_p5'))} / "
                 f"{_f(mc.get('sharpe_p50'))} / {_f(mc.get('sharpe_p95'))}")
        L.append(f"- Median reshuffled max drawdown: {_f(mc.get('median_max_drawdown'),'{:.1%}')}")
        L.append("")
        L.append("> A p5 Sharpe below 0 means a fifth of orderings of these same "
                 "trades lose money — the edge is thinner than the sample implies.")
        L.append("")

    ca = sc.get("causality", {})
    if ca:
        L.append("## Causality Audit")
        L.append("")
        L.append("**PASS** — no look-ahead detected." if ca.get("causal")
                 else "**FAIL** — look-ahead detected; results invalid.")
        L.append("")

    if gauntlet.get("flags"):
        L.append("## Concerns Raised")
        L.append("")
        for f in gauntlet["flags"]:
            L.append(f"- ⚠ {f}")
        L.append("")

    # --- Multi-asset due-diligence disclosure (A-I) -------------------
    try:
        from disclosure import build_answers, render_sheet, coverage_summary
        sheet = build_answers(spec, ctx)
        cov = coverage_summary(sheet)
        L.append("---")
        L.append("")
        L.append(f"## Due-Diligence Coverage")
        L.append("")
        L.append(f"- Questions answered: **{cov['total'] - cov['unknown']}/{cov['total']}** "
                 f"({cov['coverage_pct']:.0f}%)")
        L.append(f"- Answered by the engine (verified): {cov['engine_answered']}")
        L.append(f"- Answered by the user: {cov['user_answered']}")
        L.append(f"- Still UNKNOWN: **{cov['unknown']}**")
        L.append("")
        L.append(render_sheet(sheet))
    except Exception as e:  # noqa: BLE001
        L.append(f"## Due-Diligence Disclosure")
        L.append("")
        L.append(f"_Disclosure sheet unavailable: {type(e).__name__}: {e}_")
        L.append("")

    L.append("## Interpretation Notes")
    L.append("")
    L.append("- In-sample metrics are for comparison only; the walk-forward/OOS "
             "result is the decision-relevant number.")
    L.append("- This report is a hypothesis test, not a forecast. Even a SUPPORTED "
             "verdict warrants paper trading before live capital.")
    L.append("")
    return "\n".join(L)


if __name__ == "__main__":
    print("Use btvalidate.py via run_backtest.py or import full_gauntlet()")