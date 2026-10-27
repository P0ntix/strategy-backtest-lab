#!/usr/bin/env python3
"""
run_backtest.py -- CLI entry point for the strategy backtest lab.

Usage:
    python run_backtest.py --config my_strategy.json
    python run_backtest.py --config my_strategy.json --quick
    python run_backtest.py --list-presets
    python run_backtest.py --example > example_strategy.json

Options:
    --config PATH     Path to a JSON strategy spec (see --example).
    --quick           Skip the expensive stages (Monte Carlo / param sweep /
                     replication) for a fast read. The report and the verdict
                     say plainly which stages did NOT run.
    --refresh         Bypass the local data cache and re-fetch.
    --out PATH        Write the markdown report here (default: <name>_report.md).
    --n-variants N    How many parameter variants you tried while developing
                      this hypothesis (feeds the deflated-Sharpe penalty).
    --no-report       Don't write a file; just print to stdout.

Exit codes:
    0  the spec passed the intake gate (and, for a full run, the backtest ran)
    2  the intake gate refused: conditions are missing, or their values are
       still the engine's defaults. Nothing was backtested.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from btengine import UNIVERSE_PRESETS, FEATURES, SIGNALS, DEFAULT_SPEC, build_panel
from btvalidate import full_gauntlet, render_report, compute_metrics
from btengine import run_spec
from intake import (validate_spec, render_intake_prompt, REQUIRED_KEYS,
                    merge_with_defaults)

EXAMPLE = {
    "name": "12-1 Month Momentum (Cross-Sectional, Equity Sectors)",
    "hypothesis": "Sectors that outperformed over the past 12 months (skipping the last month) "
                  "continue to outperform over the next month, due to slow-moving institutional "
                  "flows and underreaction to persistent fundamental trends.",
    "universe": {"preset": "us_equity_sector", "symbols": []},
    "period": {"start": "2010-01-01", "end": None, "interval": "1d"},
    "screen": {"min_adv_usd": 0},
    "features": [{"name": "momentum", "params": {"lookback": 252, "skip": 21}}],
    "signal": {"type": "cross_sectional",
               "params": {"long_rank": 0.75, "short_rank": 0.25}},
    "portfolio": {"gross_leverage": 1.0, "rebalance": "monthly", "long_only": True,
                  "max_position": 0.2},
    "execution": {"fill_at": "open", "cost_multiplier": 1.0, "max_participation": 0.10},
    "capital": 100000.0,
    # Every condition below was supplied and confirmed BY THE USER, not assumed.
    # The values that happen to coincide with the engine's own defaults are
    # listed in accepted_defaults, which says "the user was asked and chose
    # this value on purpose". Without that record the gate refuses the spec,
    # because a value equal to the default cannot be told apart from one the
    # agent filled in.
    "intake": {
        "confirmed": ["hypothesis", "universe", "direction", "entry", "exit",
                      "timeframe", "rebalance", "sizing", "leverage", "costs",
                      "period", "variants_tried"],
        "accepted_defaults": ["costs", "entry", "exit", "rebalance", "timeframe"],
        "variants_tried": 1,
        "notes": "Illustrative example. A real spec must record the user's own answers."
    },
}

ALT_UNIVERSES_BY_PRESET = {
    "us_equity_sector": ["us_equity_style", "us_equity_broad"],
    "us_equity_broad": ["us_equity_sector", "us_equity_style"],
    "us_equity_style": ["us_equity_sector", "us_equity_broad"],
    "commodities": ["futures_broad"],
    "futures_broad": ["commodities"],
    "crypto": ["crypto"],
    "fx_major": ["fx_major", "fx_emerging"],
    "rates_credit": ["multi_asset"],
    "multi_asset": ["risk_parity_ish", "risk_on_risk_off"],
}

# The gauntlet stages, and the labels used when reporting that one did NOT run.
# Keys are the SCORECARD keys (what full_gauntlet stores); values are labels.
_STAGE_LABELS = {
    "monte_carlo": "Monte Carlo",
    "parameter_sensitivity": "parameter sweep",
    "walk_forward": "walk-forward",
    "cost_stress": "cost stress",
    "subperiod": "subperiod/regime",
    "capacity": "capacity",
    "replication": "cross-market replication",
    "causality": "causality audit",
}

# The `skip` tokens accepted by full_gauntlet -> the scorecard key they affect.
_SKIP_TOKEN_TO_STAGE = {
    "montecarlo": "monte_carlo",
    "paramsens": "parameter_sensitivity",
    "walkforward": "walk_forward",
    "coststress": "cost_stress",
    "subperiod": "subperiod",
    "capacity": "capacity",
    "causality": "causality",
    "replication": "replication",
}


def load_spec(path):
    """
    Load a user spec and merge DEFAULT_SPEC underneath it.

    The merge is deliberately the SAME one the intake gate validates against
    (intake.merge_with_defaults), so the gate sees exactly the spec that
    will be backtested. See intake.py for why the merge is the enemy here:
    it is what makes an engine default indistinguishable from a user choice.
    """
    with open(path, "r", encoding="utf-8") as f:
        user = json.load(f)
    return merge_with_defaults(user)


def _skipped_stage_labels(scorecard, skip):
    """
    Which validation stages did NOT run, and what to call them.

    Two sources, because the gauntlet signals a skipped stage two ways: it
    stores {"error": "skipped"} under the stage key, and it omits the key
    entirely when the stage never applied (replication with no alternative
    universe). Both mean the stage did not test anything.
    """
    labels = []
    for stage_key, label in _STAGE_LABELS.items():
        entry = (scorecard or {}).get(stage_key)
        if entry is None or (isinstance(entry, dict) and entry.get("error") == "skipped"):
            labels.append(label)
    return labels


def _quick_mode_note(labels):
    """The sentence that keeps a quick-mode verdict from overclaiming."""
    if not labels:
        return ""
    listed = ", ".join(labels[:-1]) + " and " + labels[-1] if len(labels) > 1 else labels[0]
    return (f"quick mode: {listed} were NOT run - this is a preliminary read, "
            f"not a full validation.")


def main():
    ap = argparse.ArgumentParser(description="Backtest a trading hypothesis.")
    ap.add_argument("--config")
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--out")
    ap.add_argument("--n-variants", type=int, default=1)
    ap.add_argument("--no-report", action="store_true")
    ap.add_argument("--example", action="store_true")
    ap.add_argument("--list-presets", action="store_true")
    ap.add_argument("--intake", action="store_true",
                    help="Print the intake questionnaire and exit.")
    ap.add_argument("--intake-check", action="store_true",
                    help="Report which user-supplied conditions are still missing.")
    ap.add_argument("--disclose", action="store_true",
                    help="Print the full A-I due-diligence answer sheet: what "
                         "the engine already answers, what the user must "
                         "supply, and what is still UNKNOWN.")
    ap.add_argument("--disclose-section", metavar="A",
                    help="Print one section of the due-diligence sheet.")
    ap.add_argument("--assume-defaults", action="store_true",
                    help="Bypass the intake gate. Only use when the user has "
                         "explicitly declined to answer — say so in the report.")
    args = ap.parse_args()

    if args.list_presets:
        print("Universe presets:")
        for k, v in UNIVERSE_PRESETS.items():
            print(f"  {k:20s} {v}")
        print("\nFeatures:", ", ".join(sorted(FEATURES)))
        print("Signals: ", ", ".join(sorted(SIGNALS)))
        return

    if args.example:
        print(json.dumps(EXAMPLE, indent=2))
        return

    if args.intake:
        print(render_intake_prompt())
        return

    if args.disclose or args.disclose_section:
        from disclosure import (SECTIONS, build_answers, render_sheet,
                                coverage_summary, unknowns, user_questions)
        spec_for_disclosure = {}
        if args.config:
            spec_for_disclosure = load_spec(args.config)
        sheet = build_answers(spec_for_disclosure)
        if args.disclose_section:
            sid = args.disclose_section.strip().upper()
            sec = sheet.get(sid)
            if not sec:
                print(f"Unknown section '{sid}'. Available: "
                      f"{', '.join(s[0] for s in SECTIONS)}")
                return
            print(f"### {sid}. {sec['title']}\n")
            for it in sec["items"]:
                mark = {"user": "user", "engine": "engine", "missing": "UNKNOWN"}[it["source"]]
                print(f"- {it['key']}: {it['q']}")
                print(f"    [{mark}] {it['answer']}")
                note = it.get("engine_note")
                if note:
                    print(f"    engine note: {note}")
        else:
            print(render_sheet(sheet))
            cov = coverage_summary(sheet)
            print("=" * 60)
            print(f"COVERAGE: {cov['total'] - cov['unknown']}/{cov['total']} answered "
                  f"({cov['coverage_pct']:.0f}%)  engine={cov['engine_answered']}  "
                  f"user={cov['user_answered']}  unknown={cov['unknown']}")
            print("=" * 60)
            pend = user_questions(sheet)
            if pend:
                print("\nSTILL NEEDED FROM THE USER:")
                for sid, key, q, why in pend:
                    print(f"  [{key}] {q}")
        return

    if not args.config:
        if args.intake_check:
            ap.error("--intake-check requires --config")
        ap.error("--config is required (use --example to see the format, "
                 "--intake to see what to ask the user)")

    spec = load_spec(args.config)

    # ---- INTAKE GATE -------------------------------------------------
    # Every strategy condition must be imputed by the user. The engine refuses
    # to run a spec that is not explicitly confirmed, so the agent cannot
    # quietly fill in parameters the user never specified.
    status = validate_spec(spec)
    if args.intake_check:
        print(status["summary"])
        if status["missing"]:
            print("\nMISSING (ask the user):")
            for key, question, why in status["missing"]:
                print(f"  - {key}: {question}")
        if status["defaults"]:
            print("\nAT ENGINE DEFAULT (must be answered by the user):")
            for path, label in status["defaults"]:
                print(f"  - {path} ({label})")
        return

    if not status["ok"]:
        print("=" * 68)
        print("INTAKE INCOMPLETE — refusing to backtest.")
        print("=" * 68)
        print(status["summary"])
        print()
        print("Every condition of the strategy must come from the user. Ask the")
        print("questions below and record the answers. Do NOT fill these in")
        print("yourself and do NOT fall back to engine defaults.")
        print()
        for key, question, why in status["missing"]:
            print(f"  [{key}]")
            print(f"    Q:    {question}")
            print(f"    why:  {why}")
            print()
        for path, label in status["defaults"]:
            print(f"  [{path}] ({label}) — left at the engine default.")
        print()
        print("Record answers with intake.mark_confirmed(spec, keys, variants_tried=N)")
        print("Full questionnaire: python scripts/intake.py")
        if args.assume_defaults:
            print("\n!! --assume-defaults: continuing with UNCONFIRMED values.")
        else:
            sys.exit(2)
    elif args.assume_defaults:
        print("note: --assume-defaults passed but intake was already complete.")

    preset = spec.get("universe", {}).get("preset", "custom")
    alt = ALT_UNIVERSES_BY_PRESET.get(preset, [])
    skip = ("montecarlo", "paramsens", "replication") if args.quick else ()

    print("=" * 66)
    print(f"BACKTESTING: {spec.get('name')}")
    if spec.get("hypothesis"):
        print(f"Hypothesis: {spec['hypothesis']}")
    print("=" * 66)

    g = full_gauntlet(spec, n_variants=args.n_variants,
                      alt_universes=alt, skip=skip)

    sc = g.get("scorecard", {})
    # The REAL instrument list of this run (post liquidity-screen), taken from
    # the scorecard. A preset NAME is not a count: len("us_equity_broad") would
    # report 15 instruments for a 4-instrument universe.
    symbols = list(sc.get("symbols") or [])
    ctx = {
        "metrics": sc.get("base", {}),
        "capacity": sc.get("capacity", {}),
        "walk_forward": sc.get("walk_forward", {}),
        "gross_vs_net": sc.get("gross_vs_net", {}) or {},
        "symbols": symbols,
        "n_symbols": len(symbols),
    }
    report = render_report(spec, g, ctx)
    print("\n" + "=" * 66)
    print(f"VERDICT: {g.get('verdict')} — {g.get('reasoning')}")
    # Say plainly which stages were skipped, so a partial run cannot be
    # mistaken for a complete validation.
    skipped = (g.get("scorecard") or {}).get("skipped_stages") or []
    if skipped:
        print("")
        print("  " + _quick_mode_note(skipped))
    print("=" * 66)

    if not args.no_report:
        out = args.out or f"{spec.get('name', 'strategy').replace(' ', '_').replace('/', '_')}_report.md"
        with open(out, "w", encoding="utf-8") as f:
            f.write(report)
        print(f"\nReport written to: {os.path.abspath(out)}")
    else:
        print(report)


if __name__ == "__main__":
    main()