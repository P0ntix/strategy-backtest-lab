#!/usr/bin/env python3
"""
intake.py -- Mandatory user-intake gate for the backtest lab.

DESIGN RULE: every condition of a strategy is imputed by the USER. The agent
must never invent a universe, a lookback, a threshold, a rebalance frequency,
a direction, or an economic rationale. This module makes that structural rather
than aspirational:

  1. `QUESTIONNAIRE` is the canonical list of things that must be asked.
  2. `validate_spec()` reports exactly which answers are still missing.
  3. `run_backtest.py` REFUSES to run a spec that has not been explicitly
     confirmed by the user, unless `--assume-defaults` is passed.

A spec is only "confirmed" when its `intake.confirmed` list names every key in
REQUIRED_KEYS. That forces an explicit, per-field act of confirmation, so a
half-assumed spec cannot quietly reach the backtest.

PROVENANCE: CONFIRMATION IS NOT SUPPLY
---------------------------------------
Confirmation records that a question was ASKED. It does not record that the
user ANSWERED it. `run_backtest.py` deep-merges DEFAULT_SPEC into the user's
spec before validating it, so a spec whose values are all still the engine's
defaults (the cross-sectional signal with long_rank 0.75 / short_rank 0.25,
momentum lookback 252 / skip 21, monthly rebalance, cost multiplier 1.0,
start date 2012-01-01, ...) is indistinguishable from a spec the user actually
chose -- unless the gate compares values against those defaults.

So `validate_spec()` also checks PROVENANCE. For every field the engine would
supply a default for, the value in the spec is compared with the
corresponding DEFAULT_SPEC value:

  * different  -> this value was supplied (by the user, deliberately);
  * identical  -> this is still the ENGINE'S value, not the user's.

A condition whose value is still the engine default counts as MISSING and the
run is refused, unless the user explicitly accepted that default. The
acceptance is recorded IN THE SPEC:

    "intake": {
      "confirmed": ["hypothesis", "universe", ...],
      "accepted_defaults": ["costs", "rebalance"]   # the user chose these
    }                                                # to be the engine default

Entries in `accepted_defaults` may be condition keys ("costs") or spec paths
("execution.cost_multiplier"). A path also covers the fields beneath it, so
accepting "signal" accepts the entry rule and the exit rule together.

The validation output is written to double as the interview script, so
printing the gaps literally tells the agent what to ask next.
"""

from __future__ import annotations

import json

try:
    # Flat import: running as a script, or with scripts/ on sys.path.
    from btengine import DEFAULT_SPEC
except ImportError:  # pragma: no cover - exercised by the installed `btlab` layout
    # Relative import: installed as a package (btlab.intake).
    from .btengine import DEFAULT_SPEC

# --------------------------------------------------------------------------
# The questionnaire. (key, question, why-it-matters)
# This ordering is the recommended order to ask the user.
# --------------------------------------------------------------------------
QUESTIONNAIRE = [
    ("hypothesis",
     "What is the trading hypothesis, in your own words? What do you think "
     "DRIVES it — behaviour, market structure, information, or risk premia — "
     "and in which market should it show up?",
     "A hypothesis with no mechanism is a curve fit. If you cannot articulate "
     "one, that is itself the finding and must be reported as such."),

    ("universe",
     "Which instruments exactly? Name them or pick an asset class "
     "(equities, FX, futures, crypto, rates/credit, commodities, or a mix).",
     "Survivorship and liquidity. Individual stocks carry survivorship bias the "
     "engine cannot remove; ETFs and proxies do not."),

    ("direction",
     "Long only, long/short, or market-neutral? Is the bet directional or a "
     "relative-value spread?",
     "Sets long_only / net_target, and changes what a positive Sharpe means."),

    ("entry",
     "What EXACTLY triggers an entry? Give the rule, not the indicator name "
     "— e.g. 'feature crosses above threshold X', not 'use momentum'.",
     "A vague rule cannot be coded, and coding it yourself invents a strategy "
     "the user never described."),

    ("exit",
     "What EXACTLY closes the position? Fixed horizon, signal reversal, "
     "stop loss, or end of rebalance period?",
     "Half the P&L of most strategies is in the exit. Leaving it to the agent "
     "is a fabricated assumption."),

    ("timeframe",
     "What lookback / holding period? Daily, weekly, monthly? Over how many "
     "bars should the signal be measured?",
     "Determines the feature lookback, the rebalance cadence, and whether the "
     "trade count can reach statistical significance."),

    ("rebalance",
     "How often may it trade? Daily, weekly, monthly, quarterly?",
     "Drives turnover and therefore costs — often the difference between a "
     "viable edge and a cost artifact."),

    ("sizing",
     "How should position size be set? Equal weight, inverse-volatility, "
     "fixed-fractional risk, or a maximum per name?",
     "Determines the return series that every metric is computed from."),

    ("leverage",
     "What leverage and concentration limits apply? Max gross exposure and "
     "max weight per instrument?",
     "Leverage is a risk decision, never an agent default."),

    ("costs",
     "What do you actually pay? Commission, spread, and slippage per side — "
     "or should the conservative per-asset-class defaults be used?",
     "Cost assumptions decide whether a small edge is tradeable at all."),

    ("period",
     "Which date range should be tested? Is there a period you want excluded "
     "or treated separately?",
     "Choosing the window after seeing results is a form of look-ahead on the "
     "sample itself."),

    ("variants_tried",
     "How many variations of this rule did you try before settling on it? "
     "An honest count, even if it is high.",
     "Drives the deflated-Sharpe penalty. Reporting the best of N trials is "
     "the single most common way a backtest lies."),
]

REQUIRED_KEYS = [k for k, _, _ in QUESTIONNAIRE]

# --------------------------------------------------------------------------
# PROVENANCE TABLE.
#
# For every field the engine would supply by itself: the condition it belongs
# to, the path it lives at in the spec, and the DEFAULT_SPEC value it must
# differ from. A spec field that still holds the engine's value has NOT been
# supplied by the user, no matter how many boxes the agent ticked in
# `intake.confirmed`.
#
# A condition may probe several fields. It counts as "at the engine default"
# only when EVERY field it probes is still at the default: a strategy that
# sets a position cap has a user-supplied sizing rule even though its
# vol_target is still None.
# --------------------------------------------------------------------------
def merge_with_defaults(user_spec):
    """
    Merge DEFAULT_SPEC under a user spec, one level deep per sub-dict.

    The merge is what makes the provenance check possible: it is the reason a
    spec can silently carry the engine's own values. It is kept here, next to
    the gate, so the CLI and `intake.py` validate EXACTLY the same object.
    """
    spec = json.loads(json.dumps(DEFAULT_SPEC))
    spec.update(user_spec or {})
    for k in ("portfolio", "execution", "period", "universe", "screen"):
        if k in (user_spec or {}):
            spec[k] = {**spec.get(k, {}), **user_spec[k]}
    return spec


def _default_at(path, fallback=None):
    """Read a dotted path out of DEFAULT_SPEC."""
    cur = DEFAULT_SPEC
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return fallback
        cur = cur[part]
    return cur


# (condition, path in the spec, human label)
#
# Each condition probes the fields that CARRY that decision. A condition is
# "at the engine default" only when EVERY field it probes still holds the
# engine's value -- so one deliberate choice (say a position cap) is enough to
# show the user answered that condition, even if a neighbouring knob still
# sits at its default.
_DEFAULT_PROBE_PATHS = [
    ("hypothesis", "hypothesis", "Trading hypothesis"),
    ("universe", "universe.preset", "Universe preset"),
    ("universe", "universe.symbols", "Instrument list"),
    ("direction", "portfolio.long_only", "Long-only flag"),
    ("direction", "portfolio.net_target", "Net exposure target"),
    ("entry", "signal", "Signal (type and params)"),
    ("exit", "signal.params.entry", "Signal entry rule"),
    ("exit", "signal.params.exit", "Signal exit rule"),
    ("exit", "signal.params.threshold", "Signal threshold"),
    ("exit", "execution.mode", "Session execution mode"),
    ("exit", "execution.flat_at_close", "Flat-at-close rule"),
    ("timeframe", "features", "Feature list and lookbacks"),
    ("rebalance", "portfolio.rebalance", "Rebalance frequency"),
    ("sizing", "portfolio.max_position", "Max weight per name"),
    ("sizing", "portfolio.vol_target", "Volatility target"),
    ("sizing", "portfolio.max_names", "Max names held"),
    ("leverage", "portfolio.gross_leverage", "Gross exposure"),
    ("leverage", "portfolio.max_position", "Max weight per instrument"),
    ("costs", "execution.cost_multiplier", "Cost multiplier"),
    ("period", "period.start", "Start date"),
    ("variants_tried", "intake.variants_tried", "Variants tried"),
]

# Fields the engine would silently default if the user never answered. Each
# one must come from the user; that is the whole point of this gate.
# (condition, path in the spec, label, DEFAULT_SPEC value it must differ from)
DEFAULTS_ARE_FORBIDDEN = [
    (condition, path, label, _default_at(path))
    for (condition, path, label) in _DEFAULT_PROBE_PATHS
]

_MISSING = object()


def _get(d, path, default=None):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _is_blank(v):
    return v is None or v == "" or v == [] or v == {}


def _same(a, b):
    """Deep equality that treats 1 == 1.0 but never True == 1."""
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) < 1e-12
    if isinstance(a, dict) and isinstance(b, dict):
        return set(a) == set(b) and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b))
    return a == b


def _accepted_names(raw):
    """Normalise intake.accepted_defaults into a set of names."""
    if isinstance(raw, str):
        raw = [raw]
    out = set()
    for item in raw or []:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if name:
            out.add(name)
    return out


def _is_accepted(condition, probes, accepted):
    """
    True when the user explicitly accepted the engine default for this
    condition. A condition key ("costs") accepts that whole condition; a spec
    path ("execution.cost_multiplier") accepts that field, and any field
    beneath it ("signal" covers "signal.params").
    """
    if condition in accepted:
        return True
    for probe in probes:
        path = probe["path"]
        for name in accepted:
            if name == path or path.startswith(name + "."):
                return True
    return False


def _value_ok(key, spec):
    """The structural check: is the condition present and non-blank?"""
    if key == "hypothesis":
        return bool((spec.get("hypothesis") or "").strip())
    if key == "universe":
        uni = spec.get("universe") or {}
        syms = uni.get("symbols") or []
        preset = uni.get("preset") or ""
        return bool(syms) or bool(preset) and preset != "single"
    if key == "direction":
        return "long_only" in (spec.get("portfolio") or {})
    if key == "entry":
        feats = spec.get("features") or []
        sig = spec.get("signal") or {}
        return bool(feats) and bool(sig.get("type"))
    if key == "exit":
        sig = (spec.get("signal") or {}).get("params") or {}
        # An explicit exit condition: time-series signals carry entry/exit;
        # cross-sectional relies on the rebalance horizon.
        return bool(sig.get("entry") or sig.get("exit")
                    or sig.get("threshold") is not None
                    or bool((spec.get("portfolio") or {}).get("rebalance")))
    if key == "timeframe":
        # The horizon field is feature-specific: momentum/zscore use
        # `lookback`, `return` uses `lag`, crossovers use fast/slow.
        # Requiring `lookback` specifically would reject valid specs.
        TIMEFRAME_KEYS = ("lookback", "lag", "period", "horizon",
                          "fast", "short", "slow", "long", "n")
        return bool((spec.get("features") or [])) and all(
            any(k in (f.get("params") or {}) for k in TIMEFRAME_KEYS)
            for f in (spec.get("features") or []))
    if key == "rebalance":
        return _get(spec, "portfolio.rebalance") is not None
    if key == "sizing":
        p = spec.get("portfolio") or {}
        return any(k in p for k in
                   ("vol_target", "max_position", "max_names",
                    "gross_leverage"))
    if key == "leverage":
        p = spec.get("portfolio") or {}
        return ("gross_leverage" in p) and ("max_position" in p)
    if key == "costs":
        return _get(spec, "execution.cost_multiplier") is not None
    if key == "period":
        return _get(spec, "period.start") is not None
    if key == "variants_tried":
        intake = spec.get("intake") or {}
        return intake.get("variants_tried") is not None
    return True


def _default_why(condition, detail):
    """The explanation shown next to a condition that is still a default."""
    paths = ", ".join(d["path"] for d in detail)
    return (
        f"This is still the ENGINE'S value, not the user's: {paths} matches "
        f"the default the lab would have used anyway. 'confirmed' only proves "
        f"the question was ASKED - it does not prove the user ANSWERED it. "
        f"Ask the user for their own value, or, if the user really did choose "
        f"the engine default, record that acceptance in "
        f"spec['intake']['accepted_defaults'] (e.g. ['{condition}'])."
    )


def validate_spec(spec):
    """
    Return a dict:
      missing     : list of (key, question, why) still unanswered
      at_default  : list of dicts describing fields still holding the
                    engine's default value (the provenance failure)
      defaults    : list of (path, label) fields left at an engine default
      accepted    : list of accepted_default names recorded by the user
      ok          : bool -- True when the spec is safe to backtest
      summary     : one-line human status
    """
    spec = spec or {}
    missing = []
    at_default = []
    defaults = []
    intake = spec.get("intake") or {}
    confirmed = set(intake.get("confirmed") or [])
    accepted = _accepted_names(intake.get("accepted_defaults"))

    # --- which fields still hold the engine's own value? ---------------
    # A field ABSENT from the spec counts as at-default: the merge would
    # inject the engine's value there anyway, so nothing distinguishes it
    # from a value the agent chose.
    flagged = set()
    for condition, path, label, default in DEFAULTS_ARE_FORBIDDEN:
        value = _get(spec, path, _MISSING)
        if value is _MISSING:
            value = default
        if _same(value, default):
            flagged.add(path)

    # --- group the probes by condition ----------------------------------
    probes_by_condition = {}
    for condition, path, label, default in DEFAULTS_ARE_FORBIDDEN:
        probes_by_condition.setdefault(condition, []).append(
            {"condition": condition, "path": path, "label": label,
             "default": default})

    at_default_conditions = {}
    for condition, probes in probes_by_condition.items():
        if _is_accepted(condition, probes, accepted):
            continue
        if all(p["path"] in flagged for p in probes):
            at_default_conditions[condition] = probes

    for key, question, why in QUESTIONNAIRE:
        value_ok = _value_ok(key, spec)

        # A field only counts as answered if the user also confirmed it.
        if not value_ok or key not in confirmed:
            missing.append((key, question, why))
            continue

        # ...and only if the value is not still the engine's own default.
        probes = at_default_conditions.get(key)
        if probes:
            detail = []
            for probe in probes:
                value = _get(spec, probe["path"])
                detail.append({"condition": key, "path": probe["path"],
                               "label": probe["label"],
                               "default": probe["default"], "value": value})
                defaults.append((probe["path"], probe["label"]))
            at_default.append(detail)
            missing.append((key, question, _default_why(key, detail)))

    ok = not missing
    if ok:
        summary = (f"All {len(REQUIRED_KEYS)} user-supplied conditions confirmed "
                   f"and none left at an engine default. Ready to backtest.")
    elif at_default:
        summary = (f"{len(missing)} condition(s) not yet supplied by the user "
                   f"({len(at_default)} of them are still at the engine's "
                   f"default) - ask before running.")
    else:
        summary = (f"{len(missing)} condition(s) not yet supplied by the user - "
                   f"ask before running.")

    return {"missing": missing, "at_default": at_default, "defaults": defaults,
            "accepted": sorted(accepted), "ok": ok, "summary": summary}


def render_intake_prompt(spec=None, only=None):
    """Render the interview script for the agent to put to the user."""
    L = []
    L.append("=" * 68)
    L.append("STRATEGY INTAKE — ask the user before running any backtest")
    L.append("=" * 68)
    L.append("")
    L.append("Do not invent any of these. Do not assume defaults. If the user")
    L.append("does not know, record that as the answer — an unknown is data.")
    L.append("")
    L.append("Marking a condition 'confirmed' is NOT the same as the user")
    L.append("supplying a value. A field that still holds the engine default")
    L.append("is the agent's choice until the user says otherwise. Record a")
    L.append("deliberate acceptance of a default in the spec itself:")
    L.append("    spec['intake']['accepted_defaults'] = ['costs', 'rebalance']")
    L.append("")
    status = validate_spec(spec) if spec else None
    for i, (key, question, why) in enumerate(QUESTIONNAIRE, 1):
        if only and key not in only:
            continue
        mark = ""
        if status and not any(m[0] == key for m in status["missing"]):
            mark = " [CONFIRMED]"
        L.append(f"{i}. [{key}]{mark}")
        L.append(f"   Q: {question}")
        L.append(f"   why: {why}")
        L.append("")
    if status and status["at_default"]:
        L.append("STILL AT THE ENGINE DEFAULT (not user-supplied):")
        for detail in status["at_default"]:
            for d in detail:
                L.append(f"   - [{d['condition']}] {d['path']} ({d['label']})")
                L.append(f"     engine default: {json.dumps(d['default'], default=str)}")
                L.append(f"     your spec:      {json.dumps(d['value'], default=str)}")
        L.append("")
    return "\n".join(L)


def mark_confirmed(spec, keys, variants_tried=None, notes=None):
    """Helper for the agent: record which fields the user personally supplied."""
    spec.setdefault("intake", {})
    cur = set(spec["intake"].get("confirmed") or [])
    cur.update(keys)
    spec["intake"]["confirmed"] = sorted(cur)
    if variants_tried is not None:
        spec["intake"]["variants_tried"] = variants_tried
    if notes:
        spec["intake"]["notes"] = notes
    return spec


def accept_defaults(spec, keys):
    """
    Helper for the agent: record that the user DELIBERATELY chose the engine
    default for these conditions (or for these spec paths).
    """
    spec.setdefault("intake", {})
    cur = set(spec["intake"].get("accepted_defaults") or [])
    cur.update(_accepted_names(keys))
    spec["intake"]["accepted_defaults"] = sorted(cur)
    return spec


def record_answer(spec, key, value):
    """
    Record one due-diligence answer from the user, keyed like "A.currency" or
    "G.variants_tested". These feed the A-I disclosure sheet in the report.
    """
    spec.setdefault("intake", {})
    spec["intake"].setdefault("answers", {})
    if value is None:
        spec["intake"]["answers"].pop(key, None)
    else:
        spec["intake"]["answers"][key] = str(value)
    return spec


def record_answers(spec, mapping):
    """Record several due-diligence answers at once."""
    for k, v in (mapping or {}).items():
        record_answer(spec, k, v)
    return spec


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else None
    if path:
        with open(path, "r", encoding="utf-8") as f:
            spec = json.load(f)
        st = validate_spec(spec)
        print(json.dumps({"missing": [m[0] for m in st["missing"]],
                          "at_default": st["at_default"],
                          "defaults": st["defaults"],
                          "accepted": st["accepted"],
                          "ok": st["ok"],
                          "summary": st["summary"]},
                         indent=2, default=str))
    else:
        print(render_intake_prompt())