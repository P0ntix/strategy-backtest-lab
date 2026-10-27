#!/usr/bin/env python3
"""
disclosure.py -- The multi-asset due-diligence answer sheet (sections A-I).

WHY THIS EXISTS
---------------
A multi-asset strategy review asks a lot of questions across nine sections.
Asking a user all of them is hostile and slow, and answering them all with
guesses is worse. But most of them are NOT the user's to answer: the data
source, the timestamp convention, the survivorship treatment, the execution
assumptions, the cost model -- those are properties of the ENGINE, and the
engine knows them and can be checked.

So every question is classified:

    who="engine"  the engine answers it, with a VERIFIED fact
    who="user"    only the user can answer it (rationale, currency, hedging
                  policy, discretionary overrides, forward-test criteria)
    who="both"    the user sets the parameter; the engine reports the value
                  actually used

`build_answers()` then assembles the full sheet: engine-answered where the
engine can answer, user-supplied where the user did supply, and explicitly
UNKNOWN where neither exists. Nothing is silently omitted -- an unanswered
question shows up in the report as UNKNOWN, which is itself a finding.

`unknowns()` lists what still needs the user, so the agent can ask for it
without re-interviewing everything.

THE MAP, AND WHAT IT MEANS
--------------------------
`CAP_BY_QUESTION` maps EVERY question key to a capability entry, so no
question can be silently unaccounted for. What the mapping MEANS depends on
the question's classification, and the two are kept consistent by the
import-time validation:

    who="engine" / who="both"
        the mapped capability IS the answer. The engine answers the question
        with a verified fact about its own behaviour, so it can never render
        [UNKNOWN].

    who="user"
        the mapped capability is the engine's STANCE on that question -- what
        the engine can and cannot see there. It is NOT an answer, so the
        question still renders [UNKNOWN] until the user answers it. This is
        why a user-only question must never be answered with engine prose: the
        user is never asked something the engine already knows.

`validate_capability_map()` enforces all of this at import time, so a mapping
that points at a missing capability, an empty capability, an unmapped
capability, a mapping for a question that does not exist, or a user-only
question wearing an engine answer, fails loudly on import instead of quietly
rendering a false claim or a spurious UNKNOWN.
"""

from __future__ import annotations

import json

# --------------------------------------------------------------------------
# Engine capability facts -- verified against the code, not asserted.
# --------------------------------------------------------------------------

CAPABILITIES = {
    "data_source": "Single source for EVERY asset class: the Yahoo Finance chart API v8 endpoint query1.finance.yahoo.com/v8/finance/chart/{symbol} - free, no API key, no account, no rate-limit contract. Yahoo is an AGGREGATOR, so the ultimate venue is Yahoo's chosen feed per listing and is not exposed; the returned volume is exchange-consolidated, not venue-level. VERSION: API v8; the engine identifies itself as btengine 1.0.0. EXTRACTION DATE: each symbol/interval is cached as JSON under .cache/{SYMBOL}__{interval}.json and the payload records a 'fetched' epoch timestamp; the cache is considered stale after 3 days (daily) or 1 day (intraday) and is re-fetched on the next run. There is NO dataset version, NO vintage, NO revision history and NO point-in-time snapshot - re-running after a corporate action returns a different history for the same symbol.",
    "timestamp_convention": "Bars are epoch UTC. Yahoo anchors intraday bars to "
                             "EXCHANGE-LOCAL time; daily bars are exchange session "
                             "dates.",
    "timezone_handling": "Session boundaries are detected per-day by grouping on "
                         "the UTC calendar date of each bar, which is DST-robust "
                         "and needs no tz database.",
    "asynchronous_closes": "NOT modelled as a synchronisation constraint. The "
                            "panel's date axis is the UNION of every symbol's "
                            "trading dates; a date is kept only if at least half "
                            "the universe (and at least one symbol) has a bar on "
                            "it, and then each symbol's series is FORWARD-FILLED "
                            "onto that axis. A market that did not trade on a "
                            "retained date therefore carries its LAST close "
                            "forward at a STALE price, and it IS still tradable "
                            "that day -- the stale price is carried into the "
                            "signal, the target weights and the fill.",
    "holidays": 'Handled implicitly: a closed market produces no bar for that symbol, that date is absent for that symbol, and the symbol is FORWARD-FILLED from its last traded bar - so it can still be traded at a stale price (see B.async_closes). The date itself survives on the axis if at least half the universe traded. HALF-DAYS are treated as a normal short session: the daily bar is a normal daily bar, and in session mode the 09:30-10:00 ET opening window is unaffected by a 13:00 close, so the session extraction still returns the same opening bar. There is NO holiday calendar, NO early-close calendar and NO per-class session model - the engine does not know which market was closed on which date.',
    "half_days": "Treated as a normal short session. The 09:30-10:00 ET window "
                 "is unaffected by a 13:00 close.",
    "survivorship": "NOT survivorship-free for individual equities - Yahoo serves only current listings, so any stock universe silently drops delistings (prefer ETFs). ETFs, FX, index series and crypto majors are survivorship-free WITH RESPECT TO THE INSTRUMENT LIST, but: the crypto preset is a FIXED list of 10 surviving majors, so failed or delisted crypto pairs are absent by construction; futures are represented by Yahoo's continuous series, so individual contract expiries are not modelled and the roll is undocumented and not point-in-time; there are NO individual bonds in any universe (rates/credit are ETF proxies and yield indices), so defaulted bonds are OUT OF SCOPE rather than survivorship-biased - a bond default reaches the backtest only through its ETF's price history; and OPTIONS ARE UNSUPPORTED entirely, so expired option contracts are out of scope. Membership of every universe is a static list, so there is no point-in-time index membership or delisting-driven reconstitution.",
    "corporate_actions": "Daily bars ARE split- AND dividend-adjusted: the "
                         "engine back-adjusts open/high/low/close by the "
                         "multiplicative per-bar factor adjclose/close, so "
                         "returns include dividends and the series is continuous "
                         "across splits (volume is NOT scaled). INTRADAY (60m) "
                         "bars are NOT adjusted -- no adjclose is published at "
                         "that interval -- so an ex-dividend date inside a 60m "
                         "sample leaves a small uncompensated drop, and "
                         "mergers, spin-offs, delisting returns and splits "
                         "inside a 60m session are not modelled.",
    "point_in_time": "NO point-in-time fundamentals, index membership, credit ratings, sector classifications or classification history are available in this engine - there is no such data source at all. Universe membership is a STATIC list: either a named preset (UNIVERSE_PRESETS) or an explicit symbols list, and a symbol's eligibility never changes over the sample. Two further filters apply and BOTH are not point-in-time: the optional liquidity screen (screen.min_adv_usd) keeps a symbol only if its MEDIAN dollar volume over the ENTIRE panel is at or above the threshold, so the screen is computed with FUTURE liquidity information and can admit or exclude a symbol on the strength of data that post-dates the bars being traded; and the date axis drops any date on which fewer than half the universe traded. max_names caps the number of simultaneously active names. Sector exposure exists only as separate sector ETFs in the universe, never as a classification applied to holdings.",
    "fx_conversion": "NOT modelled. All instruments are priced in their quote "
                     "currency and returns are computed in that currency. A "
                     "multi-currency book is NOT converted to a base currency.",
    "fx_costs": "No FX CONVERSION is performed, so there is no conversion cost "
                "to model - the book is never converted to a base currency. The "
                "FX class still carries a trading spread and slippage (0.5bps "
                "half-spread + 0.5bps slippage, 0bps commission) and a 15bps "
                "annualised BORROW on short notional; that borrow is the only "
                "carry-like charge and it is NOT a modelled swap or rollover "
                "rate. Hedging costs are not modelled because no hedging is "
                "performed - FX exposure is left unhedged in quote currency.",
    "risk_free_curves": 'NO curves of any kind are sourced, fitted or aligned: no risk-free curve, no swap curve, no credit-spread curve, no inflation curve. Consequently there is NO discounting, NO carry accrual and NO financing rate on cash. Critically, the risk-free rate used in Sharpe and Sortino is hard-coded to 0.0, so every reported Sharpe is a ZERO-HURDLE Sharpe, not an excess-return Sharpe - at prevailing risk-free rates this flatters the result. The only funding-like charge in the engine is the crypto borrow rate, used as a proxy for perp funding, and it is charged only on short notional.',
    "regime_coverage": 'The engine does NOT classify regimes - there is no regime classifier and no way to request one. It reports (a) a per-calendar-year return, Sharpe and bar-count table and (b) a high/low realised-volatility split of the portfolio return series (trailing 20-bar stdev, split at the median). That is the entirety of the regime facility: there is NO tagging of bull/bear, sideways, crisis, illiquidity, inflation-shock or rate-shock windows. Sample length is set by the user: daily presets typically yield 10-30 years of history (sector ETFs are shorter - XLC starts 2018, XLRE 2015), while the 60m intraday path is provider-capped at roughly 2 years, so an intraday sample can contain at most one full cycle and no meaningful regime coverage. Whether the window contains the regimes the question asks about is NOT verified by the engine and must be asserted by the user.',
    "execution_model": "Bar-resolution. Signal at bar t, fill at bar t+1 open "
                       "(or close). Costs: commission + half-spread + slippage "
                       "on traded notional, plus annualised borrow on short "
                       "notional. Orders capped at max_participation of the "
                       "TRAILING dollar volume (strictly before the fill bar); "
                       "if no trailing volume exists the order is not filled. "
                       "Dust orders below min_trade_frac are skipped. An order "
                       "is never filled on a STALE bar (a forward-filled price "
                       "the market did not print), though the position is "
                       "still marked at the last known price.",
    "gap_handling": "An order is filled at the bar's OPEN, never at a worse "
                    "intra-bar level. Gap risk is therefore NOT overstated in "
                    "favour of the strategy.",
    "limit_orders": "Not modelled. Every order is treated as a market order at "
                    "the next bar's open/close.",
    "partial_fills": "Partially modelled: the ADV participation cap can "
                     "truncate an order, but there is no queue position, no "
                     "rejection, and no latency.",
    "capacity_model": "Linear turnover scaling against a 5% ADV participation "
                      "ceiling on the most illiquid traded symbol. Does NOT "
                      "model market impact, so real capacity is LOWER.",
    "leverage_cost": 'NOT MODELLED - no margin interest, no collateral haircuts, and no financing cost on gross exposure. There is no margin, collateral, haircut or financing model of any kind: gross exposure above 1.0 is UNFUNDED, short proceeds earn 0%, and there is NO NETTING SET - every symbol is treated as margined independently and gross is computed on a per-symbol sum, so no offsetting credit between an equity short and a bond long is recognised.',
    "borrow_cost": 'An annualised borrow rate per asset class is charged DAILY ON SHORT NOTIONAL (etf 10bps, index_or_future 5bps, fx 15bps, crypto 30bps annualised). LOCATE FEES ARE NOT MODELLED, and BORROW AVAILABILITY, SHORT-SALE CONSTRAINTS, LOCATES AND RECALLS ARE NOT MODELLED at all - the engine assumes any short is fillable and never recalls it. The crypto rate doubles as the perp funding proxy, and because it is charged only on short notional a long crypto position pays no funding (see C.crypto).',
    "futures_roll": "Continuous futures are Yahoo's own continuous series, "
                    "whose roll methodology is undocumented and NOT "
                    "point-in-time. Roll cost is not modelled explicitly.",
    "options": "Split by instrument. OPTIONS: not supported at all - no pricing model, no Greeks, no implied-vol surface, no expiry/exercise/assignment, no settlement, no margin, no option bid-ask or liquidity data; an option hypothesis cannot be tested in this engine. FUTURES: supported, but only as Yahoo's continuous series - the specific contract traded is not recorded, the roll methodology is undocumented and not point-in-time, roll cost is NOT modelled, and there is no basis, term-structure, delivery-rule, contract-multiplier, tick-size or margin modelling; costs are charged as if the series were a continuous ETF-like instrument. Daily futures bars are dividend-adjusted, but the rolls are not costed.",
    "crypto_venue": "A SINGLE aggregated Yahoo series per symbol - no exchange fragmentation, no per-venue book, no venue outage handling, no custody or wallet risk, and NO stablecoin de-peg risk (a USDT-quoted pair would be treated as a normal spot pair with no basis or collateral model). FEES ARE CHARGED on the standard crypto schedule: commission 10bps + half-spread 5bps + slippage 5bps, with a borrow rate of 30bps annualised. FUNDING IS NOT MODELLED: perp funding is APPROXIMATED BY the crypto class borrow rate, and because borrow is charged only on SHORT notional, a LONG crypto position pays NO funding at all - the long leg's funding cost, which on a perpetual is paid for holding the position, is entirely absent. The engine trades 10 MAJORS ONLY (BTC, ETH, LTC, BCH, XRP, SOL, ADA, DOGE, AVAX, LINK): delisted pairs, failed tokens and exchange failures are ABSENT BY CONSTRUCTION. Crypto trades 7 DAYS A WEEK, so in a mixed book weekend dates enter the axis only where the coverage filter allows, and against STALE equity marks (see B.async_closes). Slippage is a constant 5bps with no venue-level liquidity or depth model.",
    "bond_analytics": "BOND-LEVEL ANALYTICS ARE NOT SUPPORTED: no clean/dirty price, no accrued interest, no coupon cash flow schedule, no duration or convexity, no credit-spread decomposition, no default or recovery modelling, no call/put feature handling. But rates and credit ARE testable at the ETF/index level: the rates_credit preset (TLT, IEF, SHY, LQD, HYG, EMB, TIP, BND, plus ^TNX/^TYX/^FVX/^IRX) provides total-return series, so duration exposure is embedded in the ETF price rather than modelled, and coupon income is captured only in the ETF's own adjusted price. There is no bond-level data: no liquidity metrics, no bid-ask or spread data, no issue-level identifiers, no point-in-time rating changes and no point-in-time index inclusion. Defaults are visible only as a price move inside an ETF's history. Inflation-linked exposure (TIP) is a price series with no CPI linkage modelled.",
    "alts_valuation": "ALTERNATIVES/REAL-ASSET VALUATION MECHANICS ARE NOT SUPPORTED: no appraisal smoothing, no valuation lag, no stale-price or staleness adjustment, no liquidity gates, no redemption/suspension modelling, no NAV-vs-market-price distinction. What IS tradeable is the listed proxy sleeve (VNQ, XLRE, REM, RWO, DBC, DBA, TIP), traded as an ordinary exchange-traded instrument with the standard ETF cost schedule and a daily close - so any appraisal lag embedded in the underlying is invisible, and the instrument's own redemption/liquidity risk is not modelled. Transaction costs are the standard ETF commission/half-spread/slippage plus borrow on shorts. LEVERAGE AND FINANCING ARE NOT MODELLED at all: no margin, no financing rate, no collateral haircut.",
    "walk_forward": "Purged + embargoed expanding-window walk-forward. The layout is [train_0][purge_0][test_0][embargo_0][train_1][purge_1][test_1] ... : each fold's in-sample set is its own train segment PLUS every earlier train segment, with the `purge` bars immediately before each test block excluded from EVERY training set (a training bar whose label horizon reaches into the test block would leak the test period) and the `embargo` bars immediately after each test block excluded from every later training set. Purge and embargo default to the maximum feature lookback in the spec and are overridable; the sizes actually used are printed. A DISJOINTNESS ASSERTION runs on every walk-forward and reports four checks: (a) no date appears in two out-of-sample blocks, (b) no out-of-sample bar was in its own fold's training set, (c) no training bar of fold f+1 falls inside fold f's test block or embargo band, (d) no training bar of any fold falls inside any test block. A violation is SURFACED, not swallowed. Features are computed on the FULL causal history and the panel is sliced afterwards, so a long lookback does not lose its warm-up at a fold boundary. If the sample cannot support the requested purge and embargo bands, the planner reduces the FOLD COUNT (never quietly halves the band size, which is the leak guarantee) and says so in a note that is printed with the results. NO PARAMETER IS RE-OPTIMISED PER FOLD - the same global parameters are used throughout - so the walk-forward tests whether a FIXED hypothesis generalises, not whether an optimiser can refit it, and therefore does NOT control for the user's own parameter selection (that is handled by the declared --n-variants penalty). The walk-forward is run on a SINGLE COMBINED universe, so per-asset-class holdout behaviour is not separately demonstrated.",
    "monte_carlo": "Bootstrap over the realised trade P&L sequence: each "
                   "resample rebuilds an equity curve from the sampled trades "
                   "and re-derives Sharpe and drawdown, annualised by the "
                   "observed trade frequency. Seeded (seed=7) for "
                   "reproducibility, and it needs at least 20 trades to run at "
                   "all.",
    "param_sweep": 'The sweep perturbs EACH NUMERIC PARAMETER OF EVERY DECLARED FEATURE, at -25% and +25%, and reports each perturbed IN-SAMPLE Sharpe, the retention ratio against the base in-sample Sharpe, and a STABILITY FRACTION defined as the share of perturbations that preserve the SIGN of the Sharpe - a sign-consistency measure, not a retention magnitude; a broad plateau is reported at stability >= 0.75. NOT perturbed: all signal thresholds, all portfolio/sizing parameters, the rebalance cadence, and all execution and cost parameters. ASSET-CLASS WEIGHTS ARE NEVER PERTURBED AT ALL - there is no allocation parameter to perturb, because the engine does not allocate across classes (see A.allocation), so the plateau across asset-class weights half of this question cannot be tested in this engine. The sweep is IN-SAMPLE; it does not re-run walk-forward at the perturbed settings. Degenerate (no-op) perturbations are skipped.',
    "deflated_sharpe": 'Implemented, but as a SIMPLIFIED PENALTY, not the Bailey/Lopez de Prado Deflated Sharpe Ratio: the observed Sharpe is reduced by an EXPECTED-MAX-UNDER-THE-NULL term 1/sqrt(n_obs) * ((1-gamma)*sqrt(2 ln n_variants) + gamma) with gamma = 0.5772. It applies NO non-normality (skew/kurtosis) term, NO trials-variance term and NO selection-bias correction, and it is applied to the IN-SAMPLE base Sharpe, NOT to the pooled OOS Sharpe that determines the verdict - so it is not a deflated OOS statistic. --n-variants DEFAULTS TO 1, in which case NO penalty is applied at all and the reported value equals the raw Sharpe; the correction is only active if the user supplies an honest count (intake.variants_tried must be confirmed). A negative deflated value raises a flag and downgrades an otherwise SUPPORTED verdict to FRAGILE.',
    "pbo": "A fold-level IS-vs-OOS RANK INVERSION rate, computed from the "
           "walk-forward's per-fold IN-SAMPLE Sharpe alongside its per-fold "
           "OUT-OF-SAMPLE Sharpe. For every pair of folds the engine compares "
           "two rankings of the same two folds - by IS Sharpe and by OOS "
           "Sharpe - and flags an INVERSION when the fold that looked better "
           "in-sample performed worse out-of-sample. The reported number is "
           "the fraction of comparable fold pairs that inverted (0% = the "
           "in-sample ranking carried over perfectly, 100% = it was pure "
           "noise); Kendall's tau between the two rankings is reported "
           "alongside as 1 - 2 * inversion_rate. It needs at least four folds "
           "to exist at all, so the statistic is coarse. It is the statistic "
           "the Bailey / Lopez de Prado overfitting analysis is built around "
           "- does the in-sample ordering survive out of sample - but it is "
           "NOT the full combinatorially-symmetric (CSCV) PBO: that version "
           "splits the sample into many sub-periods and re-ranks a POPULATION "
           "of strategy configurations in every split, which needs multiple "
           "candidate configurations per run and this engine does not produce "
           "them. Treat the number as a directional overfit signature, not a "
           "calibrated probability.",
    "leave_one_out": "NOT implemented as a dedicated stage - there is NO leave-one-asset-out test anywhere in the engine. Cross-market replication is a DIFFERENT experiment: it re-runs the same signal on ALTERNATIVE universes and reports each one's IN-SAMPLE Sharpe of the full sample. It indicates whether the signal is portable to other instruments; it does NOT measure what happens to a fixed multi-asset book when one sleeve is removed, because it never removes a sleeve from the original book and the alternative universes overlap the original. The required question - does performance collapse if one asset class is removed? - CANNOT be answered by this engine and must be produced by re-running the gauntlet on hand-built sub-universes, or not at all.",
    "random_rebalancing": "NOT implemented.",
    "top_trade_removal": "NOT implemented.",
    "confidence_intervals": "NOT reported for expectancy, profit factor, "
                            "Sharpe, or drawdown. Only Monte Carlo percentiles "
                            "for the resampled distribution.",
    "factor_exposure": "NOT decomposed. No Barra/Fama-French style factor "
                       "attribution.",
    "risk_contribution": 'NONE of the three bundled sub-topics is computed. RISK CONTRIBUTION BY ASSET CLASS: not computed - there is no risk model, no COVARIANCE matrix and no marginal/contribution-to-risk decomposition (the engine estimates no covariance or correlation at all). FACTOR EXPOSURE: not decomposed - no value/momentum/carry/trend/quality/liquidity/duration/credit attribution, and no benchmark-relative exposure. CONCENTRATION BY ASSET/SECTOR/COUNTRY/CURRENCY: only a per-name max_position cap, max_names, gross_leverage and net_target are enforced; there is NO reported concentration statistic by sector, country, currency or class, and no class/sector/country/currency/factor limits are enforced. The only exposure figures reported are portfolio-level gross exposure and time-in-market.',
    "correlation_matrix": "NOT estimated point-in-time. Cross-asset "
                           "correlation is therefore not audited for "
                           "look-ahead.",
    "null_hypothesis": "The engine tests the null of ZERO NET-OF-COST "
                       "EXPECTANCY: a strategy whose pooled out-of-sample "
                       "Sharpe is below 0.5, or whose edge disappears under "
                       "2x cost stress, is REFUTED. The 'no marginal "
                       "diversification benefit' half of that null is NOT "
                       "tested directly, because that requires a leave-one-out "
                       "comparison this engine does not perform.",
    # ---- capability entries for questions the engine answers ------------
    "constraints": "Enforced: per-name max_weight (hard cap, applied LAST - "
                   "after the gross normalisation, the volatility target and "
                   "the net-target shift, none of which may silently undo "
                   "it), gross_leverage, and net_target. net_target is applied "
                   "as a SHIFT (the same constant added to every weight), not "
                   "a uniform scale, so it can reach a target of the opposite "
                   "sign without inverting the strategy. NOT enforced: "
                   "per-class, per-sector, per-country, per-factor or "
                   "per-liquidity-bucket limits.",
    "commissions_model": 'Per asset class the engine charges EXACTLY FOUR components on traded notional, from DEFAULT_COSTS: COMMISSION, HALF-SPREAD (half the bid-ask), SLIPPAGE, and BORROW (annualised, charged daily on short notional). Rates (bps): etf 0.5 / 1.0 / 1.0 / 10.0; index_or_future 0.2 / 0.5 / 0.5 / 5.0; fx 0.0 / 0.5 / 0.5 / 15.0; crypto 10.0 / 5.0 / 5.0 / 30.0. EXCHANGE FEES, TRANSACTION TAXES, STAMP DUTIES and LEVIES ARE NOT MODELLED for any asset class - a strategy in a jurisdiction with stamp duty is being charged a cost stack that omits it. Note also that infer_asset_class classifies by symbol string, so a symbol it does not recognise falls back to the etf schedule. The only user lever is a SINGLE GLOBAL execution.cost_multiplier (the gauntlet re-runs at 1.0x/1.5x/2.0x/3.0x) - per-class rates CANNOT be overridden. Realised costs are reported as one scalar costs_paid; per-class attribution of realised costs is NOT available - the per-class table states the ASSUMED rates, not what each class actually paid.',
    "equities_modeling": "Daily bars ARE split- and dividend-adjusted (the "
                         "engine back-adjusts OHLC by the per-bar "
                         "adjclose/close factor), so daily returns include "
                         "dividends. INTRADAY (60m) bars are NOT adjusted, so "
                         "an ex-dividend date inside a 60m sample leaves a "
                         "small uncompensated drop. Mergers, spin-offs, "
                         "delisting returns, and splits inside a 60m session "
                         "are not modelled. Withholding taxes on dividends are "
                         "NOT modelled. Point-in-time index membership and "
                         "sector classification are NOT available. Borrow: "
                         "annualised borrow cost charged on short notional; "
                         "locate fees and availability not modelled. Auctions, "
                         "halts, tick sizes and market-on-close mechanics are "
                         "not modelled.",
    "fx_modeling": "Spot only. Forward points, carry, rollover, swap rates, "
                   "weekend gaps, settlement conventions and NDFs are not "
                   "modelled. Bid-ask and conversion costs are approximated by "
                   "the class spread. Hedging is not modelled - FX exposure "
                   "is left unhedged in quote currency.",
    "commodities_modeling": "Yahoo's CONTINUOUS futures series is used; the specific contract traded is not recorded, the ROLL METHODOLOGY IS UNDOCUMENTED AND NOT POINT-IN-TIME, and ROLL COST IS NOT MODELLED - there is no contango/backwardation pricing, no seasonal adjustment and no delivery or expiry handling. NO PHYSICAL CONSTRAINTS are modelled at all: NO STORAGE, NO physical inventory or deliverability, NO local basis, and NO physical/liquidity constraints - the series is treated as a continuously tradable financial instrument. No contract multiplier, tick size or exchange margin model exists. Note that infer_asset_class recognises only a fixed list of futures symbols, so commodity futures outside it (e.g. PL=F, PA=F, ZC=F) fall back to the ETF cost schedule.",
    "causality_contract": "features[i] use only data at indices <= i; a signal "
                           "decided at bar i fills at bar i+1. The engine's "
                           "own causality audit recomputes every feature on a "
                           "truncated panel and fails if any first-half value "
                           "changes - that is how centred windows and "
                           "full-sample normalisation get caught.",
    "bar_close_exec": "Bar-resolution and causal: the signal is computed at bar t and the order is filled at bar t+1's open by default (fill_at: 'open'), or at bar t+1's close (fill_at: 'close', still causal because the decision uses bar t). Orders are sent ONLY on rebalance-marked bars. Precisely: an ORDER IS GENERATED only on a rebalance-marked bar and fills on the NEXT bar, whether or not that next bar is itself marked; between marked bars no order exists and the previous target is simply held. Gating the FILL on the mask instead would leave every order pending until the next rebalance bar, so a monthly strategy would execute each rebalance a full month late. In session mode (execution.mode: 'session') the panel is DAILY - one 09:30-10:00 ET opening-window bar per session - and with flat_at_close: true the position opens at that bar's open and CLOSES AT THAT SAME BAR'S CLOSE, so there is NO OVERNIGHT EXPOSURE and the trade is a single-bar round trip.",
    "publication_lag": "The question does not arise, because every built-in feature is derived from the instrument's own PRICE and VOLUME series (return, momentum, sma_gap, volatility, vol_ratio, zscore, rsi, max_drawdown, skew, volume_ratio, bollinger_pct, atr_pct, ma_crossover, dist_to_high, plus the session-mode session_drift, session_gap, session_return). NO fundamentals, earnings, macro releases, ratings or news are ingested, so NO publication-time lag can leak - but equally, NO fundamental information is tested.",
    "cross_asset_alignment": "Features are computed PER SYMBOL from a shared date axis and each feature at bar i uses only data at indices <= i, so NO ASSET READS ANOTHER ASSET'S FUTURE CLOSE - the causality audit (recompute every feature on a truncated panel and require the first half to match) verifies this for all 14 built-in features. Two caveats the audit does NOT cover: (a) the axis is the UNION of all symbols' dates filtered to >=50% coverage, NOT an intersection; and (b) a symbol that did not trade on a retained date carries its PREVIOUS CLOSE FORWARD, so on that date a STALE price is presented to the cross-sectional rank as though it were a live observation - a stale-versus-live comparison, not a future-leak. No ORDER is filled on such a bar (the simulation drops it and counts it under stale_skips), but the stale value still participates in the cross-sectional RANK, so a halted name can occupy a top or bottom rank slot on a date it did not trade. The audit tests FEATURES ONLY: it says nothing about universe construction, and the liquidity screen uses full-sample ADV (see A.universe_rules).",
    "replication": "The strategy is re-run on ALTERNATIVE universes (usually "
                   "different asset classes) over the SAME historical window. "
                   "Each alternative run is IN-SAMPLE for that alternative "
                   "universe: same dates, same features, no walk-forward split, "
                   "no re-optimisation. It therefore tests whether the SIGNAL "
                   "is portable to another market, NOT whether it survives "
                   "out-of-sample -- it is not an out-of-sample breadth "
                   "result. The share of alternative universes with a positive "
                   "Sharpe is reported as the replication rate and is flagged "
                   "below 50%.",
    "subperiod_analysis": "Per-calendar-year return and Sharpe are computed and "
                           "printed, plus a high/low realised-volatility regime "
                           "split. Specific macro events (rate shocks, credit "
                           "crises) are not tagged automatically.",
    "bad_data": "Stage 1 audits every series and reports: fewer than 120 bars, "
                "calendar gaps over 12 days (possible halt or delisting), "
                "non-positive prices, more than 30% zero-volume bars, 8 or more "
                "identical consecutive closes (a stale feed), and single-bar "
                "moves above 55%. Findings are WARNINGS, not automatic "
                "corrections - the data is reported, not silently patched, "
                "because a silent fix hides the problem it was meant to reveal.",
    "granularity": "Bar resolution must match the execution assumption. Daily "
                   "bars cannot realistically model intraday futures stops, "
                   "option fills, or an opening-window entry and exit - a daily "
                   "backtest of an intraday rule is not a conservative "
                   "approximation, it is a different strategy. The session path "
                   "(60m bars collapsed to the 09:30-10:00 ET opening window) is "
                   "the supported way to test intraday rules, and the provider "
                   "caps it at roughly 2 years of history.",
    "randomized": "NOT implemented. Randomized asset inclusion and randomized "
                  "rebalancing dates are stronger robustness tests than the "
                  "parameter sweep for a multi-asset book, because they test "
                  "whether the result depends on the specific assets or calendar "
                  "chosen rather than on the signal. A strategy that survives "
                  "only on one asset set has not been shown to work.",
    "dividend_adjustment": "Daily bars are back-adjusted by a MULTIPLICATIVE "
                            "per-bar factor adjclose/close read from Yahoo's "
                            "chart API, applied to open/high/low/close (volume "
                            "is NOT scaled), so daily returns include dividends "
                            "and the series is continuous across splits. The "
                            "factor is sanity-bounded (0.05 < factor < 20) and a "
                            "malformed factor is ignored rather than allowed to "
                            "corrupt the series. The local cache carries a "
                            "schema marker (CACHE_SCHEMA = 2): a daily cache "
                            "written under the older schema holds UNADJUSTED "
                            "prices and is REFETCHED rather than silently "
                            "reused, so stale unadjusted data cannot leak into "
                            "a backtest. INTRADAY 60m bars have NO adjclose "
                            "published for that interval, so they are NOT "
                            "dividend-adjusted and an ex-dividend date inside a "
                            "60m sample leaves a small uncompensated drop.",
    "observations": "Reported per run: n_bars (panel bars with a non-null "
                    "return) and n_trades (closed round-trip position "
                    "lifecycles). The engine does NOT split those counts by "
                    "asset class or instrument - a run simulates ONE universe, "
                    "so there is no per-sleeve observation count to report.",
    "adv_participation": "Two DISTINCT participation rules exist and must not be conflated. (1) THE ORDER CAP THE SIMULATION ENFORCES: every order is truncated at max_participation - DEFAULT 10% - of the TRAILING dollar volume (the mean dollar volume of the bars STRICTLY BEFORE the fill bar, over a 20-bar window). Using the fill bar's own volume would be intra-bar look-ahead, since a bar's volume is not knowable at its open, and when that volume was zero the cap silently disappeared and let an order of any size through. If no trailing volume is available the order is NOT filled, and the skip is counted in the simulation result under the key liquidity_skips. THERE IS ONE EXCEPTION: an instrument with NO volume series at all (FX pairs and most OTC instruments have no consolidated screen volume) is not blocked - the participation cap simply does not apply to it, because there is no screen volume to participate in. Such instruments are listed in the simulation result under symbols_without_volume, and the order is still sized and costed normally. on daily bars that is up to 10% of one session's volume in a single order, so it is a PER-BAR, not per-day, limit. (2) THE CAPACITY CEILING THE CAPACITY STAGE ESTIMATES: the capital at which the binding symbol's DAILY traded notional would reach 5% of its median daily dollar volume, assuming turnover scales linearly with capital and NO market impact - so the true capacity is LOWER. The engine NEVER reports realised participation per asset class; per-symbol capacity limits are exposed by the capacity stage (per_symbol_limits), but a per-class participation statistic does not exist.",
    "core_metrics": "Computed and reported: CAGR, Sharpe, Sortino, Calmar, "
                    "profit factor, expectancy per trade, win rate, t-statistic, "
                    "skew, kurtosis, VaR(95%), CVaR(95%), max drawdown, total "
                    "return, final equity, turnover, time in market, average "
                    "gross exposure, costs paid, and a lag-1 autocorrelation "
                    "Sharpe adjustment. PROFIT FACTOR, EXPECTANCY AND WIN RATE "
                    "ARE COMPUTED FROM THE NET-OF-COSTS TRADE LEDGER, so they "
                    "are on the same basis as Sharpe and CAGR, and the report "
                    "labels them as such. NOT computed: annualised volatility and "
                    "drawdown duration - the engine produces neither, so no "
                    "figure for them can be quoted.",
    "drawdown_metrics": "Maximum drawdown (magnitude) is computed and "
                        "reported. Drawdown DURATION, time-to-recovery and worst "
                        "daily/weekly/monthly loss are NOT computed or "
                        "reported: the engine derives the drawdown depth from "
                        "the return series and never exposes a duration "
                        "metric.",
    "exposure_metrics": "Computed: turnover_total (cumulative traded notional over the run; the capacity stage separately derives turnover per year), time_in_market - DEFINED AS THE FRACTION OF BARS IN WHICH GROSS EXPOSURE EXCEEDED 5%, not calendar time invested - and avg_gross_exposure (mean gross exposure over equity). NOT computed: an average HOLDING PERIOD (per-trade bars_held is recorded in the ledger but never aggregated) and NET EXPOSURE / NET LEVERAGE (only gross exposure is tracked, so a dollar-neutral or net-target book's realised net drift is invisible). MARGIN USAGE IS NOT APPLICABLE - the engine models no margin, no collateral and no financing, so gross exposure above 1.0 is unfunded by construction.",
    "tail_risk": 'Skew, kurtosis, VaR(95%) and CVaR(95%) are computed, plus a high/low realised-volatility regime split. VaR(95%) is the 5th PERCENTILE return and CVaR(95%) is the MEAN OF THE TAIL (not the worst observation); both require at least 20 return observations and are None below that. Worst single gap and a tagged crisis slice are NOT computed.',
    "benchmark_corr": "NOT computed. The engine can run any universe as a "
                       "buy-and-hold comparator, but correlation to a "
                       "benchmark or to an existing portfolio is not reported.",
    "gross_net": 'The gauntlet runs the strategy a SECOND TIME WITH COSTS ZEROED, so a gross-of-costs run and the net-of-costs run are compared side by side (Sharpe and CAGR, plus costs paid and turnover) with a plain-English reading of the gap across the four QUADRANTS: an edge that is positive gross but negative net is real but untradeable at this size and frequency; an edge that is not positive gross is not an edge at all. PER-ASSET-CLASS ATTRIBUTION of realised costs is NOT auto-split - the class cost table holds ASSUMED rates, not realised per-class costs.',
    "spread_model": "A constant half-spread per asset class is charged on every "
                    "trade. Spread WIDENING IN STRESS OR ILLIQUIDITY is NOT "
                    "modelled - so a strategy that trades into a volatility "
                    "spike is flattered.",
    "slippage_model": "Constant slippage per asset class, plus an order cap at "
                      "max_participation (default 10%) of the TRAILING dollar "
                      "volume (strictly before the fill bar). Slippage does NOT "
                      "widen with volatility, and market impact is NOT "
                      "modelled.",
    "roll_carry": "Futures roll cost, FX swap/rollover, and option bid-ask spreads are NOT MODELLED. Crypto funding is APPROXIMATED BY the crypto class borrow rate (30bps annualised), but because borrow is charged only on SHORT NOTIONAL, a LONG crypto position pays NO FUNDING AT ALL - the long leg's funding cost, which on a perpetual is paid for holding the position, is entirely absent.",
    "size_decay": "NOT automated. The capacity stage reports the capital at "
                  "which the binding instrument would exceed 5% of ADV; "
                  "explicit 2x/5x/10x size runs are not built in.",
    "covariance": 'NOT computed. The engine estimates NO covariance matrix and NO correlation matrix at all - there is no risk model, no correlation estimate, and therefore no point-in-time correlation to leak. Nothing about correlations is look-ahead-free or otherwise, because correlations are simply absent from the output.',
    "stress_correlation": 'NOT modelled. There is no crisis-correlation regime, no correlation stress scenario and no conditional-correlation model - and no correlation matrix to stress in the first place (see F.covariance). A multi-asset book is therefore NOT tested for the correlation spikes that historically define a crisis.',
    "diversification_decay": "Not directly tested. The regime split shows "
                              "whether performance differs between high and low "
                              "volatility periods; it does not measure "
                              "correlation decay specifically.",
    "concentration": "Only per-name max_weight, gross_leverage and net_target "
                     "are enforced. Limits by class, sector, country, currency "
                     "and factor are NOT enforced.",
    "vol_target_impl": 'THE VOL-TARGET LEVEL IS NOW HONOURED: the book is scaled so the PORTFOLIO\'s estimated annual volatility equals vol_target, so vol_target 0.10 and vol_target 0.40 produce genuinely different books. The shape comes from RELATIVE inverse-volatility (1/sigma) scaling across names; the size comes from the volatility target. PORTFOLIO VOLATILITY IS ESTIMATED WITH A DIAGONAL COVARIANCE (pairwise correlations assumed zero), which OVERSTATES the volatility of a genuinely diversified multi-asset book - so REALISED VOLATILITY LANDS BELOW THE TARGET, and vol_target should be read as an UPPER BOUND on realised vol, not an exact setting. LIQUIDITY PLAYS NO PART in the sizing: the inverse-vol scaling uses trailing realised volatility only (default vol_lookback 60 bars, annualised at sqrt(252)) and is blind to ADV, spread and depth. A thin, high-vol instrument therefore receives a small weight but can still dominate realised risk.',
    "turnover_costs": "Turnover and total costs are tracked and reported per "
                       "run, and the cost-stress stage re-runs the strategy at "
                       "1.0x/1.5x/2.0x/3.0x the base costs.",
    "sleeve_sum": "NOT computed directly. The cross-market replication stage "
                  "reports the Sharpe of the same signal on alternative "
                  "universes (in-sample on those universes), which indicates "
                  "whether the edge is portable or specific to one sleeve.",
    "cross_asset_stability": "Indicated by the replication stage and the "
                             "per-year breakdown, but NOT by a dedicated "
                             "leave-one-out test, which is not implemented.",
    "concentration_of_result": "Partially. The per-year breakdown shows whether "
                               "one year carried the result, and replication "
                               "shows whether one asset class carried it. "
                               "There is no single-instrument attribution.",
    "trade_counts": 'A COMBINED trade count is reported and fewer than 30 trades raises a flag (THRESHOLDS.min_trades) and, with no OOS evidence, forces INCONCLUSIVE. THE THRESHOLD IS COMBINED, NOT PER SLEEVE: there is NO per-asset-class or per-instrument trade count, so a book with 120 trades concentrated in one sleeve passes the bar while a sleeve with 5 trades is statistically meaningless. One trade = one complete position lifecycle (partial rebalance adjustments are not counted).',
    "non_normality": "Skew, kurtosis and a lag-1 autocorrelation Sharpe adjustment are computed: sharpe_lag1 = S / sqrt(1 + (1 + 2*rho1)) - a SINGLE-LAG correction, NOT Lo (2002)'s estimator, which sums all autocorrelations up to the series length. The adjusted figure is returned in the scorecard but is NOT SHOWN in the rendered report metric table and does NOT enter the verdict. Fat tails and serial correlation are therefore visible but not acted on; there is no clustering test (runs/variance-ratio) and no bootstrap-based tail diagnostic.",
    "top_trades": "NOT implemented. Removing the top 1-5 trades is the standard "
                  "test for whether a result depends on a few outliers, and "
                  "this engine does not run it.",
    "drawdown_attribution": "NOT implemented per asset class or per period.",
    "currency_model": "Base and reporting currency are NOT configured. Returns "
                      "are computed in each instrument's own quote currency, "
                      "with NO FX conversion and NO hedging applied. A "
                      "multi-currency book is therefore not comparable to a "
                      "single-currency benchmark without an external FX step.",
    "benchmark": "Optional - the user must name one; the engine can run any "
                 "universe as a buy-and-hold comparator, but correlation to a "
                 "benchmark is NOT computed.",
    "rebalance_model": "One rebalance per period, on the period's first "
                       "available bar. 'daily' = every session; 'weekly', "
                       "'monthly', 'quarterly' and 'yearly' = the first bar of "
                       "each period. No orders are sent between rebalances.",
    "direction_model": "Direction is set by the spec, not inferred. long_only "
                       "suppresses the short leg and net_target forces a "
                       "specified net exposure. A time-series signal is "
                       "long/short by the SIGN of the feature against its "
                       "threshold; a cross-sectional signal is ranked across "
                       "the universe each bar, going long above long_rank and "
                       "short below short_rank.",
    "allocation_model": "The engine allocates WITHIN one universe - it sizes "
                        "positions and rescales to the target gross exposure. "
                        "The sizing models available are equal weight, "
                        "inverse-volatility (vol_target) and a hard per-name "
                        "cap; there is NO stop-loss-based fixed-fractional "
                        "sizing model, so 'risk per trade' sizing is not "
                        "directly expressible. It does NOT allocate across "
                        "asset classes: a multi-class book requires either a "
                        "single combined universe or an explicit external "
                        "allocation rule.",
    # ---- stances for questions only the USER can answer ------------------
    # These are NOT answers. They record what the engine can and cannot see
    # at that question, so the map covers every question without letting an
    # engine statement masquerade as a user's answer.
    "user_owned_question": "The engine has no verified view of this: it is a statement about the user's own reasoning, hypothesis or process, which no amount of market data can supply. The engine's role is to test the stated rule, not to vouch for the story behind it. The engine does apply its OWN fixed objective refutation criteria to every run, independently of the user's pre-registration: REFUTED if pooled OOS Sharpe < 0.5, or if the edge does not survive 2x cost stress, or if the causality audit fails (unconditional - the numbers are void); INCONCLUSIVE if fewer than 30 trades; and FRAGILE (SUPPORTED downgraded) if fewer than 50% of calendar years are profitable, fewer than 60% of walk-forward folds are profitable, parameter-sweep stability < 0.6, Monte Carlo P(profitable) < 0.85, OOS Sharpe retention < 40% of in-sample, or a negative deflated Sharpe.",
    "asset_class_scope": "The engine can only run the universes it has "
                          "presets for: US equity sector/style/broad, futures, "
                          "FX major/emerging, commodities, crypto, rates/credit, "
                          "international equity, real assets and multi-asset. "
                          "Which of those are in scope for THIS strategy, and "
                          "which are excluded and why, is the user's call.",
    "discretionary_not_modelled": "The engine has NO discretionary-override "
                                  "mechanism: every position comes from the "
                                  "spec's signal and portfolio rules. Any "
                                  "manual override would be invisible to the "
                                  "backtest, so it would have to be reported "
                                  "outside it.",
    "limits_not_modelled": "The engine enforces only per-name max_weight, "
                           "gross_leverage and net_target. Regulatory, "
                           "contract, borrow-availability and position limits "
                           "are outside it entirely and have to come from the "
                           "user's broker and risk policy.",
    "variants_user_declared": "The engine cannot see how many variants were "
                              "tried; it applies the deflated-Sharpe penalty "
                              "for the count the user DECLARES (--n-variants). "
                              "An understated count makes the penalty too "
                              "weak.",
    "forward_test": "The engine is retrospective only: it never paper trades, "
                    "routes orders or captures fills, and it keeps no live "
                    "log. Every forward-test fact therefore comes from the "
                    "user's own records; the engine's only contribution is the "
                    "backtest baseline those facts are compared against.",
}

# --------------------------------------------------------------------------
# The questionnaire. (section_id, section_title, question_key, question, who, why)
# --------------------------------------------------------------------------

SECTIONS = [
    ("A", "Strategy definition and multi-asset scope", [
        ("A.asset_classes", "Which asset classes are in scope, and which are explicitly excluded? Why?",
         "user", "Scope determines the whole test. Exclusions need a reason."),
        ("A.rationale", "What is the economic rationale for combining these classes - diversification, carry, trend, relative value, or risk premia?",
         "user", "A rationale without a mechanism is a curve fit."),
        ("A.currency", "What is the base currency, reporting currency, and hedging policy?",
         "both", "FX conversion and hedging are not modelled; unhedged multi-currency returns are not comparable."),
        ("A.universe_rules", "What are the exact universe rules per asset class, and are they point-in-time?",
         "both", "The engine applies a static symbol list per preset."),
        ("A.rebalance_horizon", "What are the rebalancing frequency, holding period, and signal horizon per asset class?",
         "both", "Different sleeves need different cadences."),
        ("A.allocation", "How is capital allocated across asset classes - fixed weights, risk parity, vol targeting, optimisation, or a discretionary overlay?",
         "both", "The engine allocates within one universe; cross-class allocation needs a stated rule."),
        ("A.constraints", "What are the portfolio constraints: max weight per asset, class, sector, country, factor, liquidity bucket, and gross/net leverage?",
         "both", "The engine enforces per-name max_weight, gross_leverage, and net_target only."),
        ("A.benchmark", "What is the benchmark, and is it multi-asset and investable?",
         "both", "A non-investable benchmark cannot be outperformed in practice."),
        ("A.direction", "Is the strategy long-only, long/short, or market-neutral, and what sets the direction of each sleeve?",
         "both", "Direction changes what a positive Sharpe means; the user sets it, the engine reports what it applied."),
        ("A.null_hypothesis", "What is the null hypothesis - e.g. zero net-of-cost expectancy and no marginal diversification benefit?",
         "both", "The null must be stated before the test, not after."),
        ("A.rejection_criteria", "What would make you REJECT the strategy before running the test?",
         "user", "Pre-registered rejection criteria are what make a verdict falsifiable."),
        ("A.discretionary_overrides", "Are there discretionary overrides? If so, how are they modeled and logged?",
         "user", "Unmodelled discretion silently invalidates a backtest."),
    ]),
    ("B", "Data integrity and cross-asset alignment", [
        ("B.data_sources", "What are the exact data sources, vendor, extraction date, and version per asset class?",
         "engine", "Recorded in the engine and the local cache."),
        ("B.timestamps", "Are all asset classes timestamped consistently?",
         "engine", "UTC epoch internally; intraday anchored to exchange-local."),
        ("B.timezones", "How are time zones and daylight-saving changes handled across global markets?",
         "engine", "Session boundaries detected per-day; no tz database needed."),
        ("B.async_closes", "How are asynchronous market closes handled (US equities, FX 24/5, crypto 24/7, futures sessions)?",
         "engine", "Union axis plus a coverage filter plus per-symbol forward-fill; no cross-asset synchronisation model."),
        ("B.holidays", "How are holidays, half-days, and closures treated per asset class?",
         "engine", "Implicit - a closed market simply has no bar, and its last close is carried forward."),
        ("B.bad_data", "How are missing bars, stale prices, bad ticks, duplicate rows, zero-volume periods, and outliers detected and cleaned?",
         "engine", "Stage 1 audit flags short series, calendar gaps, non-positive prices, high zero-volume share, flat-close runs, and extreme moves."),
        ("B.survivorship", "Is the universe survivorship-free per asset class, including delisted equities, defaulted bonds, expired futures, and delisted crypto pairs?",
         "engine", "Free for ETFs/futures/FX/indices/crypto majors; NOT free for individual equities."),
        ("B.pit_classification", "Are index membership, credit ratings, sector classifications, and liquidity filters point-in-time?",
         "engine", "Not available in this engine; universe membership is static."),
        ("B.corporate_actions", "Are corporate actions handled correctly per asset class - splits, dividends, mergers, spin-offs, coupons, defaults, calls?",
         "engine", "Daily bars are adjusted; intraday bars are not; bonds/options are out of scope."),
        ("B.dividend_adjustment", "Are dividends reflected in the returns, and how is the adjustment applied?",
         "engine", "Dividend handling changes every long-horizon result."),
        ("B.fx_rates", "What FX conversion rates are used, from what source, at what timestamp?",
         "engine", "No conversion is performed - returns stay in quote currency."),
        ("B.curves", "How are risk-free curves, swap curves, credit spreads, inflation curves, and funding rates sourced and aligned?",
         "engine", "Not modelled."),
        ("B.regimes", "Does the sample cover multiple regimes - bull, bear, sideways, high/low vol, crisis, illiquidity, inflation and rate shocks?",
         "both", "Depends on the window; the engine reports a per-year breakdown."),
        ("B.observations", "How many independent observations and trades exist per asset class and for the combined portfolio?",
         "engine", "Reported as n_bars and n_trades."),
        ("B.granularity", "Is the data granularity sufficient for the execution assumptions per asset class?",
         "engine", "Daily bars cannot model intraday futures stops or option fills realistically."),
    ]),
    ("C", "Asset-class-specific instrument modeling", [
        ("C.equities", "Equities: splits, dividends, mergers, spin-offs, delistings, borrow, auctions, halts, tick sizes, market-on-close mechanics.",
         "engine", "Daily bars adjusted; borrow cost modelled; auctions/halts/tick sizes are not."),
        ("C.fixed_income", "Fixed income and credit: clean/dirty price, accrued interest, coupon cash flows, curve construction, duration, convexity, spread, default, recovery, calls.",
         "engine", "Not supported by this engine."),
        ("C.fx", "FX: spot, forward points, carry, rollover, swap rates, weekend gaps, settlement, NDFs, conversion costs, hedging.",
         "engine", "Spot only. Carry, rollover, and hedging costs are not modelled."),
        ("C.commodities", "Commodities: which contract is traded, continuous-series construction, roll dates and costs, contango/backwardation, storage, seasonality, multipliers, margin.",
         "engine", "Yahoo's undocumented continuous series; roll cost is not modelled explicitly."),
        ("C.crypto", "Crypto: 24/7 trading, exchange fragmentation, outages, stablecoin risk, fees, funding, borrow, custody, delisted pairs, venue liquidity and slippage.",
         "engine", "Single aggregated series; fragmentation, funding, custody, and stablecoin risk are not modelled."),
        ("C.derivatives", "Listed derivatives: pricing models, IV surfaces, expiry, assignment, exercise, settlement, margin, Greeks, basis, term structure, roll costs, option spreads.",
         "engine", "Not supported by this engine."),
        ("C.alts", "Alternatives and real assets: valuation lags, appraisal smoothing, liquidity gates, transaction costs, point-in-time valuation, leverage and redemption constraints.",
         "engine", "Not supported by this engine."),
    ]),
    ("D", "Look-ahead bias and information leakage", [
        ("D.signal_timing", "Is every signal computed using only information available before execution in that asset's market?",
         "engine", "Causality contract: feature[i] uses data <= i; signal[i] fills at i+1. Audited by truncation test."),
        ("D.bar_close_exec", "If a signal uses a bar close, is execution at that close, the next open, or later?",
         "both", "The engine fills at the next bar's open by default."),
        ("D.publication_lag", "Are fundamentals, earnings, macro releases, credit ratings, and news lagged by actual publication time?",
         "engine", "No fundamental or news data is used, so the question does not arise."),
        ("D.cross_asset_alignment", "Are cross-asset signals aligned so no asset uses another asset's future close?",
         "engine", "Yes for the single-engine feature path; the panel is shared."),
        ("D.fx_timing", "Are FX conversions using the rate available at the trade timestamp?",
         "engine", "No conversion performed."),
        ("D.holdout", "Was the out-of-sample/holdout period truly untouched across all asset classes?",
         "engine", "Walk-forward test blocks are non-overlapping and re-simulated with fresh capital."),
        ("D.multiple_testing", "Were multiple asset-class variants tested on the same data? How is multiple-testing bias corrected?",
         "both", "Deflated Sharpe applies the --n-variants penalty the user declares."),
        ("D.asset_selection", "Were asset classes added or removed after seeing results?",
         "user", "Only the user can attest to this - and it is the single most common way multi-asset studies lie."),
    ]),
    ("E", "Costs, execution, financing, and capacity", [
        ("E.commissions", "Are commissions, exchange fees, taxes, stamp duties, and levies included per asset class?",
         "both", "Per-class defaults in DEFAULT_COSTS; user may override the multiplier."),
        ("E.spread", "Is bid-ask spread modelled per asset class, including wider spreads in stress and illiquidity?",
         "engine", "Constant per class; stress-widening is NOT modelled."),
        ("E.slippage", "Is slippage a function of volatility, order size, liquidity, and order type?",
         "engine", "Constant per class plus an ADV participation cap; no volatility-dependent widening."),
        ("E.stop_fills", "Are stops assumed to fill at the stop price even when markets gap?",
         "engine", "No stop orders are modelled; market orders fill at the next bar's open."),
        ("E.limit_fills", "Are limit orders assumed filled on touch, and is queue position considered?",
         "engine", "Limit orders are not modelled at all."),
        ("E.partial_fills", "Are partial fills, rejected orders, and missed signals modelled?",
         "engine", "ADV truncation only; no rejections, no latency, no missed-signal modelling."),
        ("E.borrow", "Are short borrow costs, locate fees, and short-sale constraints included?",
         "engine", "Borrow cost yes; locate and availability no."),
        ("E.roll_carry_funding", "Are futures roll costs, FX swap costs, crypto funding, and options spreads included?",
         "engine", "Not modelled except crypto borrow as a proxy."),
        ("E.financing", "Are financing, margin interest, collateral haircuts, and leverage costs included?",
         "engine", "Not modelled."),
        ("E.fx_costs", "Are FX conversion costs and hedging costs included?",
         "engine", "No conversion, so no conversion cost."),
        ("E.adv_participation", "What percentage of average daily volume would your orders represent per asset class?",
         "engine", "The capacity stage solves for the capital at which the binding instrument hits 5% of ADV."),
        ("E.size_decay", "How does performance degrade at 2x, 5x, and 10x intended size?",
         "engine", "Capacity stage gives the ceiling; explicit 2x/5x/10x runs are NOT automated."),
        ("E.limits", "Are there position limits, borrow limits, contract limits, or regulatory constraints per asset class?",
         "user", "Regulatory constraints are outside the engine."),
    ]),
    ("F", "Portfolio construction, cross-asset risk, and correlation", [
        ("F.covariance", "Is the covariance/correlation matrix estimated point-in-time and free of look-ahead bias?",
         "engine", "No covariance matrix is estimated; correlation is not audited."),
        ("F.stress_correlation", "How do correlations behave in stress? Are crisis correlations modelled?",
         "engine", "Not modelled. The engine reports a high/low realised-vol regime split."),
        ("F.diversification_decay", "Does the strategy rely on diversification that disappears in drawdowns?",
         "engine", "Not directly tested; the regime split is the closest available proxy."),
        ("F.factor_exposure", "What are factor exposures across asset classes - value, momentum, carry, trend, quality, liquidity, duration, credit?",
         "engine", "Not decomposed."),
        ("F.risk_contribution", "Are risk contributions per asset class measured and within limits?",
         "engine", "Not computed. Inverse-vol sizing equalises contribution only under the stated assumption."),
        ("F.concentration", "Are concentration limits enforced by asset, class, country, sector, currency, and factor?",
         "both", "Only per-name max_weight, gross_leverage and net_target are enforced."),
        ("F.vol_target_impl", "How is volatility targeting or risk parity implemented across assets with different vol and liquidity?",
         "both", "Relative inverse-vol basis, normalised to target gross; the level cancels under gross normalisation."),
        ("F.turnover_costs", "Are rebalancing costs and turnover modelled at the portfolio level?",
         "engine", "Yes - turnover and total costs are tracked and reported."),
        ("F.margin_netting", "Are margin, collateral, and netting assumptions consistent across asset classes?",
         "engine", "Not modelled."),
        ("F.sleeve_sum", "Does the combined portfolio outperform the sum of sleeves after costs, or is diversification illusory?",
         "both", "Cross-market replication over alternative universes is the closest automated proxy."),
        ("F.leave_one_out", "What happens if one asset class is removed, and does performance collapse?",
         "engine", "NOT implemented as a dedicated stage."),
        ("F.cross_asset_stability", "Are cross-asset signals stable, or driven by one regime or one asset class?",
         "engine", "Replication stage plus per-year breakdown; no leave-one-out."),
    ]),
    ("G", "Robustness, overfitting, and statistical validity", [
        ("G.variants_tested", "How many parameter combinations, asset subsets, and allocation rules were tested?",
         "user", "Only the user knows this. It drives the deflated-Sharpe penalty."),
        ("G.plateau", "Does performance form a stable plateau across nearby parameters and asset-class weights?",
         "engine", "Parameter sweep perturbs every numeric feature parameter by +/-25% and reports the fraction of perturbations that keep the edge's sign."),
        ("G.walk_forward", "Does it survive walk-forward testing with rolling optimisation across all assets?",
         "engine", "Walk-forward runs, but WITHOUT per-fold re-optimisation."),
        ("G.oos_breadth", "Does it survive out-of-sample testing on different instruments, countries, and asset classes?",
         "engine", "Cross-market replication stage - but that run is IN-SAMPLE on the alternative universe."),
        ("G.subperiods", "Does it survive subperiod analysis - rate shocks, credit crises, commodity spikes, crypto drawdowns?",
         "engine", "Per-year return and Sharpe breakdown."),
        ("G.monte_carlo", "Does it survive Monte Carlo resampling of trades, returns, and trade order?",
         "engine", "Bootstrap over the trade sequence; trade ORDER is resampled too."),
        ("G.randomized", "Does it survive randomized asset inclusion or randomized rebalancing dates?",
         "engine", "NOT implemented."),
        ("G.concentration_of_result", "Are results driven by one asset class, one instrument, one year, or one event?",
         "engine", "Partially - per-year breakdown and replication."),
        ("G.trade_counts", "Are trade counts per asset class sufficient - at least 30-50 per sleeve?",
         "engine", "Reported; a shortfall is a flag."),
        ("G.confidence_intervals", "Are confidence intervals reported for expectancy, profit factor, Sharpe, and drawdown?",
         "engine", "Not for those metrics - only Monte Carlo percentiles."),
        ("G.deflated_sharpe", "Is the Sharpe ratio adjusted for multiple testing and selection bias?",
         "both", "Implemented; needs the user's honest variant count."),
        ("G.non_normality", "Are returns fat-tailed, autocorrelated, or clustered in ways that invalidate standard statistics?",
         "engine", "Skew, kurtosis, and a lag-1 autocorrelation Sharpe adjustment are reported."),
        ("G.top_trades", "What happens if you remove the top 1-5 trades per asset class and overall?",
         "engine", "NOT implemented."),
        ("G.drawdown_attribution", "Are drawdowns and tail losses attributed correctly by asset class?",
         "engine", "Not implemented per class."),
    ]),
    ("H", "Performance and risk metrics to report", [
        ("H.core_metrics", "CAGR, annualised volatility, Sharpe, Sortino, Calmar, profit factor, expectancy per trade.",
         "engine", "Everything the engine computes is listed in the answer; the two it does not compute are named."),
        ("H.drawdown_metrics", "Maximum drawdown, longest drawdown, time to recovery, worst daily/weekly/monthly loss.",
         "engine", "Max drawdown is computed; duration and time-to-recovery are NOT."),
        ("H.exposure_metrics", "Turnover, holding period, exposure, gross/net leverage, margin usage.",
         "engine", "Turnover, time in market, average gross exposure computed; margin usage N/A."),
        ("H.risk_attribution", "Risk contribution by asset class, factor exposure, concentration by asset/sector/country/currency.",
         "engine", "Not computed."),
        ("H.tail_risk", "Tail risk: skew, kurtosis, CVaR, worst gap, crisis performance.",
         "engine", "Skew, kurtosis, VaR/CVaR computed; worst gap and crisis slice N/A."),
        ("H.benchmark_corr", "Correlation to benchmarks and to an existing portfolio.",
         "engine", "NOT computed."),
        ("H.gross_net", "Performance gross and net of costs, with cost attribution by asset class.",
         "engine", "Gross-vs-net Sharpe/CAGR are reported side by side; per-class attribution is not auto-split."),
        ("H.capacity", "Capacity analysis by asset class and combined portfolio.",
         "engine", "Capacity stage reports the binding symbol and the ceiling."),
    ]),
    ("I", "Forward testing: multi-asset execution and behavior", [
        ("I.forward_test_type", "Is this paper trading or small live capital across all asset classes?",
         "user", "Outside this engine's scope entirely."),
        ("I.duration", "How long will the forward test run - minimum trades, weeks, and regime coverage per asset class?",
         "user", "The user sets the graduation criteria."),
        ("I.rule_parity", "Are rules identical to the backtest? If not, what changed and why?",
         "user", "The user must attest; rule drift is the usual reason forward results diverge."),
        ("I.success_criteria", "What are the predefined success/failure criteria before starting?",
         "user", "Must be set before, or it is not a test."),
        ("I.fill_parity", "Did signals, fills, and timestamps match across asset classes?",
         "user", "Observed during forward testing, not backtested."),
        ("I.slippage_actual", "What was actual slippage versus modelled slippage per asset class?",
         "user", "Requires a live log."),
        ("I.rejected_orders", "Were there rejected, delayed, partial, or missed orders?",
         "user", "Requires a live log."),
        ("I.feed_discrepancy", "Did data-feed discrepancies occur between signal source and broker across assets?",
         "user", "Requires a live log."),
        ("I.funding_behaviour", "Did FX conversion, funding, borrow, roll, or margin behave as modelled?",
         "user", "Requires a live log."),
        ("I.signals_vs_trades", "How many signals were generated versus taken per asset class?",
         "user", "Requires a live log."),
        ("I.skipped_trades", "For every skipped trade, what was the reason - fear, news, liquidity, sizing, rule ambiguity?",
         "user", "Requires a trade journal."),
        ("I.overrides", "Did you override the system? How often, why, and with what outcome?",
         "user", "Requires a trade journal."),
        ("I.sizing_adherence", "Were position sizing, stops, exposure limits, and loss limits honoured?",
         "user", "Requires a live log."),
        ("I.unintended_concentration", "Did correlated positions create unintended concentration across asset classes?",
         "user", "Requires a live log."),
        ("I.first_losing_streak", "How did you behave during the first multi-asset losing streak?",
         "user", "Behavioural, outside any engine."),
        ("I.regime_similarity", "Is the current regime similar to the backtest sample across asset classes?",
         "user", "Requires live data compared to the sample."),
        ("I.live_vs_backtest", "What are live expectancy, profit factor, win rate, drawdown, and slippage versus backtest per sleeve and combined?",
         "user", "Requires a live log."),
        ("I.degradation", "Is degradation within acceptable range - e.g. 5-10% lower win rate or higher costs?",
         "user", "Requires both logs."),
        ("I.statistical_maturity", "Are results statistically meaningful yet, or still too few trades?",
         "user", "The engine's trade-count flags are the guide."),
        ("I.operational_failures", "Did operational failures occur - data outage, broker API, reconciliation error, manual order error?",
         "user", "Requires a live log."),
        ("I.decision", "Does the strategy meet graduation criteria, need revision, or should it be rejected?",
         "user", "The user's decision, informed by the verdict."),
        ("I.scale_pause_evidence", "What evidence would justify scaling capital, and what evidence would force a pause?",
         "user", "Must be pre-registered."),
    ]),
]

ALL_QUESTIONS = []
for _sid, _title, _qs in SECTIONS:
    for _k, _q, _who, _why in _qs:
        ALL_QUESTIONS.append((_sid, _title, _k, _q, _who, _why))

QUESTION_INDEX = {q[2]: q for q in ALL_QUESTIONS}


# --------------------------------------------------------------------------
# Explicit map: question key -> capability entry.
# Precise on purpose. Fuzzy matching silently mismatched keys and would have
# left verified facts unmapped (or mapped to the wrong question).
#
# Every question key appears here. For who="engine"/"both" questions the
# entry IS the answer; for who="user" questions it is the engine's stance and
# the question still renders UNKNOWN until the user answers.
# --------------------------------------------------------------------------

CAP_BY_QUESTION = {
    # A
    "A.asset_classes": "asset_class_scope",
    "A.rationale": "user_owned_question",
    "A.currency": "currency_model",
    "A.universe_rules": "point_in_time",
    "A.rebalance_horizon": "rebalance_model",
    "A.allocation": "allocation_model",
    "A.constraints": "constraints",
    "A.benchmark": "benchmark",
    "A.direction": "direction_model",
    "A.null_hypothesis": "null_hypothesis",
    "A.rejection_criteria": "user_owned_question",
    "A.discretionary_overrides": "discretionary_not_modelled",
    # B
    "B.data_sources": "data_source",
    "B.timestamps": "timestamp_convention",
    "B.timezones": "timezone_handling",
    "B.async_closes": "asynchronous_closes",
    "B.holidays": "holidays",
    "B.bad_data": "bad_data",
    "B.survivorship": "survivorship",
    "B.pit_classification": "point_in_time",
    "B.corporate_actions": "corporate_actions",
    "B.dividend_adjustment": "dividend_adjustment",
    "B.fx_rates": "fx_conversion",
    "B.curves": "risk_free_curves",
    "B.regimes": "regime_coverage",
    "B.observations": "observations",
    "B.granularity": "granularity",
    # C
    "C.equities": "equities_modeling",
    "C.fixed_income": "bond_analytics",
    "C.fx": "fx_modeling",
    "C.commodities": "commodities_modeling",
    "C.crypto": "crypto_venue",
    "C.derivatives": "options",
    "C.alts": "alts_valuation",
    # D
    "D.signal_timing": "causality_contract",
    "D.bar_close_exec": "bar_close_exec",
    "D.publication_lag": "publication_lag",
    "D.cross_asset_alignment": "cross_asset_alignment",
    "D.fx_timing": "fx_conversion",
    "D.holdout": "walk_forward",
    "D.multiple_testing": "deflated_sharpe",
    "D.asset_selection": "user_owned_question",
    # E
    "E.commissions": "commissions_model",
    "E.spread": "spread_model",
    "E.slippage": "slippage_model",
    "E.stop_fills": "gap_handling",
    "E.limit_fills": "limit_orders",
    "E.partial_fills": "partial_fills",
    "E.borrow": "borrow_cost",
    "E.roll_carry_funding": "roll_carry",
    "E.financing": "leverage_cost",
    "E.fx_costs": "fx_costs",
    "E.adv_participation": "adv_participation",
    "E.size_decay": "size_decay",
    "E.limits": "limits_not_modelled",
    # F
    "F.covariance": "covariance",
    "F.stress_correlation": "stress_correlation",
    "F.diversification_decay": "diversification_decay",
    "F.factor_exposure": "factor_exposure",
    "F.risk_contribution": "risk_contribution",
    "F.concentration": "concentration",
    "F.vol_target_impl": "vol_target_impl",
    "F.turnover_costs": "turnover_costs",
    "F.margin_netting": "leverage_cost",
    "F.sleeve_sum": "sleeve_sum",
    "F.leave_one_out": "leave_one_out",
    "F.cross_asset_stability": "cross_asset_stability",
    # G
    "G.variants_tested": "variants_user_declared",
    "G.plateau": "param_sweep",
    "G.walk_forward": "walk_forward",
    "G.oos_breadth": "replication",
    "G.subperiods": "subperiod_analysis",
    "G.monte_carlo": "monte_carlo",
    "G.randomized": "randomized",
    "G.concentration_of_result": "concentration_of_result",
    "G.trade_counts": "trade_counts",
    "G.confidence_intervals": "confidence_intervals",
    "G.deflated_sharpe": "deflated_sharpe",
    "G.non_normality": "non_normality",
    "G.top_trades": "top_trades",
    "G.drawdown_attribution": "drawdown_attribution",
    # H
    "H.core_metrics": "core_metrics",
    "H.drawdown_metrics": "drawdown_metrics",
    "H.exposure_metrics": "exposure_metrics",
    "H.risk_attribution": "risk_contribution",
    "H.tail_risk": "tail_risk",
    "H.benchmark_corr": "benchmark_corr",
    "H.gross_net": "gross_net",
    "H.capacity": "capacity_model",
    # I
    "I.forward_test_type": "forward_test",
    "I.duration": "forward_test",
    "I.rule_parity": "forward_test",
    "I.success_criteria": "forward_test",
    "I.fill_parity": "forward_test",
    "I.slippage_actual": "forward_test",
    "I.rejected_orders": "forward_test",
    "I.feed_discrepancy": "forward_test",
    "I.funding_behaviour": "forward_test",
    "I.signals_vs_trades": "forward_test",
    "I.skipped_trades": "forward_test",
    "I.overrides": "forward_test",
    "I.sizing_adherence": "forward_test",
    "I.unintended_concentration": "forward_test",
    "I.first_losing_streak": "forward_test",
    "I.regime_similarity": "forward_test",
    "I.live_vs_backtest": "forward_test",
    "I.degradation": "forward_test",
    "I.statistical_maturity": "forward_test",
    "I.operational_failures": "forward_test",
    "I.decision": "forward_test",
    "I.scale_pause_evidence": "forward_test",
}

# Capability facts that are deliberately NOT the answer to any question.
# Each entry must say why it is kept -- an unmapped capability is either a
# summary used by the report prose, or a fact the engine states about its own
# scope rather than a claim about the run.
ALLOWED_ORPHANS = {
    "execution_model": "Summary of the fill and cost mechanics. No single A-I "
                        "question asks for the whole model; the parts are "
                        "answered by D.bar_close_exec, E.spread, E.slippage, "
                        "E.partial_fills and E.borrow.",
    "pbo": "Computed by the walk-forward stage and printed in the report's "
           "walk-forward block. It is the fold-level IS-vs-OOS RANK INVERSION "
           "rate: for every pair of folds the in-sample ranking and the "
           "out-of-sample ranking of the same two folds are compared, and a "
           "pair is inverted when the fold that looked better in-sample did "
           "worse out-of-sample. The reported number is the fraction of "
           "comparable pairs that inverted, with Kendall's tau reported "
           "alongside. It is a DIRECTIONAL overfit signature, not the full "
           "Bailey/Lopez de Prado combinatorially-symmetric PBO and not a "
           "calibrated probability. The G.walk_forward answer carries the same "
           "fact in prose, so this entry is the canonical definition of what "
           "the proxy does and does not measure.",
    "half_days": "Half-day handling is a sub-case of the holidays answer "
                 "(B.holidays), which states it in context.",
    "futures_roll": "Roll methodology is a sub-case of the commodities answer "
                    "(C.commodities), which states it in context.",
    "correlation_matrix": "The covariance answer (F.covariance) states that no "
                           "correlation matrix is estimated; this entry is the "
                           "canonical wording.",
    "random_rebalancing": "Covered by the randomised answer (G.randomized), "
                          "which names randomised rebalancing dates.",
    "top_trade_removal": "Covered by the top-trades answer (G.top_trades), "
                         "which names top-1-5 removal.",
}


# --------------------------------------------------------------------------
# Validation -- runs at import time so this class of defect cannot recur.
# --------------------------------------------------------------------------

def validate_capability_map():
    """
    Check the question->capability map for the defects that make the sheet
    lie. Raises ValueError naming the offending key; returns True if clean.
    """
    problems = []
    mapped_caps = set(CAP_BY_QUESTION.values())

    for qkey, ckey in sorted(CAP_BY_QUESTION.items()):
        if ckey not in CAPABILITIES:
            problems.append("question '%s' maps to capability '%s', which does "
                            "not exist in CAPABILITIES" % (qkey, ckey))
        if qkey not in QUESTION_INDEX:
            problems.append("CAP_BY_QUESTION key '%s' is not a question in "
                            "SECTIONS" % qkey)

    for ckey, cval in sorted(CAPABILITIES.items()):
        if not isinstance(cval, str) or not cval.strip():
            problems.append("capability '%s' has an empty value" % ckey)
        elif ckey not in mapped_caps and ckey not in ALLOWED_ORPHANS:
            problems.append("capability '%s' is not mapped to any question and "
                            "is not listed in ALLOWED_ORPHANS" % ckey)

    for ckey, reason in sorted(ALLOWED_ORPHANS.items()):
        if ckey not in CAPABILITIES:
            problems.append("ALLOWED_ORPHANS entry '%s' has no matching "
                            "CAPABILITIES entry" % ckey)
        if not str(reason).strip():
            problems.append("ALLOWED_ORPHANS entry '%s' has no reason" % ckey)

    for qkey, q in sorted(QUESTION_INDEX.items()):
        who = q[4]
        if who not in ("engine", "user", "both"):
            problems.append("question '%s' has an unknown who='%s'"
                            % (qkey, who))
        if qkey not in CAP_BY_QUESTION:
            problems.append("question '%s' has no CAP_BY_QUESTION entry" % qkey)
        elif who in ("engine", "both"):
            ckey = CAP_BY_QUESTION[qkey]
            if ckey not in CAPABILITIES or not CAPABILITIES[ckey].strip():
                problems.append("question '%s' is classified '%s' but its "
                                "capability '%s' cannot answer it"
                                % (qkey, who, ckey))

    if problems:
        raise ValueError("disclosure.py capability map is invalid:\n  - "
                         + "\n  - ".join(problems))
    return True


validate_capability_map()


# --------------------------------------------------------------------------
# Assembly
# --------------------------------------------------------------------------

def _get(d, path, default=None):
    cur = d
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return default
        cur = cur[part]
    return cur


def _fmt(v, fmt=None):
    """Format a metric for prose without ever printing 'None'."""
    if v is None:
        return "n/a"
    if fmt:
        try:
            return fmt.format(v)
        except (ValueError, TypeError):
            return str(v)
    if isinstance(v, float):
        return f"{v:.4g}"
    return str(v)


def _instrument_count(ctx):
    """
    The REAL number of instruments in the backtest.

    Prefers the integer count the runner supplies. Falls back to the length of
    the symbol list ONLY when it really is a list -- a preset NAME is a
    string, and len("us_equity_broad") would report a fictional 15-instrument
    universe for a 4-instrument run.
    """
    n = ctx.get("n_symbols")
    if isinstance(n, int) and not isinstance(n, bool) and n >= 0:
        return n
    syms = ctx.get("symbols")
    if isinstance(syms, (list, tuple)):
        return len(syms)
    return None


def _runtime_facts(ctx):
    """
    The runtime facts this run contributes, keyed by question.

    These are injected INTO the capability text; they never replace it, and
    there is no second, independently written answer set. Every fact here
    comes from the scorecard the gauntlet produced.
    """
    facts = {}
    m = ctx.get("metrics") or {}
    if m:
        facts["H.core_metrics"] = (
            "This run: Sharpe {sh}, Sortino {so}, CAGR {cg}, Calmar {ca}, "
            "profit factor {pf}, expectancy {ex}, win rate {wr}, t-stat {ts}, "
            "VaR(95%) {var}, CVaR(95%) {cvar}, max drawdown {dd}, {nt} trades "
            "over {nb} bars.").format(
                sh=_fmt(m.get("sharpe")), so=_fmt(m.get("sortino")),
                cg=_fmt(m.get("cagr"), "{:.2%}"), ca=_fmt(m.get("calmar")),
                pf=_fmt(m.get("profit_factor")), ex=_fmt(m.get("expectancy")),
                wr=_fmt(m.get("win_rate"), "{:.1%}"),
                ts=_fmt(m.get("t_stat")), var=_fmt(m.get("var_95"), "{:.2%}"),
                cvar=_fmt(m.get("cvar_95"), "{:.2%}"),
                dd=_fmt(m.get("max_drawdown"), "{:.2%}"),
                nt=_fmt(m.get("n_trades")), nb=_fmt(m.get("n_bars")))
        facts["H.drawdown_metrics"] = (
            "This run: max drawdown {dd} (duration and time-to-recovery are "
            "not reported).").format(dd=_fmt(m.get("max_drawdown"), "{:.2%}"))
        facts["H.exposure_metrics"] = (
            "This run: turnover {to}, time in market {tm}, average gross "
            "exposure {ge}.").format(
                to=_fmt(m.get("turnover_total")),
                tm=_fmt(m.get("time_in_market"), "{:.0%}"),
                ge=_fmt(m.get("avg_gross_exposure")))
        obs = "This run: {nb} bars, {nt} trades".format(
            nb=_fmt(m.get("n_bars")), nt=_fmt(m.get("n_trades")))
        n_inst = _instrument_count(ctx)
        if n_inst is not None:
            obs += " across %d instruments." % n_inst
        else:
            obs += " (the runner did not supply an instrument count)."
        facts["B.observations"] = obs

    gvn = ctx.get("gross_vs_net") or {}
    if gvn:
        facts["H.gross_net"] = (
            "This run: gross Sharpe {gs} (gross CAGR {gc}) against net Sharpe "
            "{ns} (net CAGR {nc}); ${cp} of costs paid on ${tv} of turnover. "
            "{interp}.").format(
                gs=_fmt(gvn.get("gross_sharpe")),
                gc=_fmt(gvn.get("gross_cagr"), "{:.2%}"),
                ns=_fmt(gvn.get("net_sharpe")),
                nc=_fmt(gvn.get("net_cagr"), "{:.2%}"),
                cp=_fmt(gvn.get("costs_paid"), "{:,.0f}"),
                tv=_fmt(gvn.get("turnover_total"), "{:,.0f}"),
                interp=(gvn.get("interpretation") or "").strip())
    elif m:
        # No gross run was supplied: report the net numbers and label them as
        # net. Never dress a net Sharpe up as a gross one.
        facts["H.gross_net"] = (
            "This run (net of costs only - no gross-of-costs run was "
            "supplied): net Sharpe {ns}, costs paid ${cp}.").format(
                ns=_fmt(m.get("sharpe")), cp=_fmt(m.get("costs_paid"), "{:,.0f}"))

    cap = ctx.get("capacity") or {}
    if cap.get("est_capacity_usd"):
        facts["E.adv_participation"] = (
            "This run: binding instrument {bs}, ceiling ~${cap} at 5% of its "
            "ADV ({nt} symbols traded).").format(
                bs=cap.get("binding_symbol"),
                cap=_fmt(cap.get("est_capacity_usd"), "{:,.0f}"),
                nt=_fmt(cap.get("n_traded_symbols")))
    return facts


def _user_answers(spec):
    """
    The user's own answers, keyed by question key.

    `intake.record_answer` stores them under spec["intake"]["answers"]. The
    declared variant count lives beside them at intake.variants_tried and is
    the user's statement too, so it is folded in here rather than being
    re-derived at each use site.
    """
    answers = dict(_get(spec, "intake.answers", {}) or {})
    vt = _get(spec, "intake.variants_tried")
    if vt is not None and not str(answers.get("G.variants_tested", "")).strip():
        answers["G.variants_tested"] = "%s variants declared by the user" % vt
    # The intake helper documents this key with the shorter spelling too.
    if not str(answers.get("G.variants_tested", "")).strip():
        alt = answers.get("G.variants_tried")
        if alt is not None and str(alt).strip():
            answers["G.variants_tested"] = str(alt)
    return answers


def build_answers(spec, ctx=None):
    """
    Assemble the full answer sheet.

    spec: the strategy spec (user answers live under spec["intake"]["answers"])
    ctx : optional runtime context dict, e.g. {"metrics": ..., "capacity": ...,
          "walk_forward": ..., "gross_vs_net": ..., "symbols": [...],
          "n_symbols": int}

    Returns {section_id: {"title":..., "items":[{key,q,who,why,answer,source}]}}

    ONE authoritative path per question: the user's answer if supplied, else
    the engine capability (with this run's facts injected), else UNKNOWN.

    The engine's own position on a question is reported separately as
    `engine_note`. For a "both" question the user has answered, that note is
    the engine's report of the value it ACTUALLY used -- which is the whole
    point of the "both" classification. For a "user"-only question the note
    says what the engine can and cannot see there, and the answer itself
    stays UNKNOWN until the user supplies it.
    """
    ctx = ctx or {}
    answers = _user_answers(spec)
    facts = _runtime_facts(ctx)
    out = {}
    for sid, title, qs in SECTIONS:
        items = []
        for key, q, who, why in qs:
            user_val = answers.get(key)
            cap_key = CAP_BY_QUESTION.get(key)
            cap_val = CAPABILITIES.get(cap_key) if cap_key else None
            fact = facts.get(key)

            if user_val is not None and str(user_val).strip():
                answer, source = str(user_val), "user"
                # The engine's report of what it ACTUALLY used. For a "both"
                # question that is the whole point of the classification; for
                # a "user" question it is the scope caveat, which stays useful
                # once the user has answered.
                note = cap_val
                user_supplied = True
            elif who in ("engine", "both") and cap_val:
                answer = cap_val
                if fact:
                    answer = "%s %s" % (answer, fact)
                source = "engine"
                note = None
                user_supplied = False
            else:
                answer, source = "UNKNOWN", "missing"
                note = cap_val
                user_supplied = False
            items.append({"key": key, "q": q, "who": who, "why": why,
                          "answer": answer, "source": source,
                          "engine_note": note,
                          "user_supplied": user_supplied})
        out[sid] = {"title": title, "items": items}
    return out


def unmapped_engine_questions():
    """
    Questions the engine PROMISES to answer but cannot.

    A `who="engine"`/`who="both"` question whose capability mapping is
    missing renders as [UNKNOWN], which is an engine defect, not a question
    for the user. Assert this list is empty whenever a capability mapping is
    added -- `validate_capability_map()` enforces it at import time too.
    """
    return sorted(k for k, v in QUESTION_INDEX.items()
                  if v[4] in ("engine", "both")
                  and (k not in CAP_BY_QUESTION
                       or CAP_BY_QUESTION[k] not in CAPABILITIES
                       or not CAPABILITIES[CAP_BY_QUESTION[k]].strip()))


def who_counts():
    """How the questions are classified (engine / user / both)."""
    counts = {"engine": 0, "user": 0, "both": 0}
    for q in ALL_QUESTIONS:
        counts[q[4]] = counts.get(q[4], 0) + 1
    return counts


def unknowns(sheet):
    """Questions still UNKNOWN, grouped by who must answer them."""
    out = {}
    for sid, sec in sheet.items():
        for it in sec["items"]:
            if it["source"] == "missing":
                out.setdefault(it["who"], []).append((sid, it["key"], it["q"]))
    return out


def user_questions(sheet, only_missing=True):
    """
    Questions the USER still needs to answer (or that they already answered).

    A who="both" question is asked of the user whenever the user has not
    supplied their side of it: the engine reports the value it actually
    used, but the parameter itself is the user's to set. A who="user"
    question is asked until the user answers it. A who="engine" question
    is never asked -- the engine has already answered it.
    """
    out = []
    for sid, sec in sheet.items():
        for it in sec["items"]:
            if it["who"] not in ("user", "both"):
                continue
            if only_missing and it.get("user_supplied"):
                continue
            out.append((sid, it["key"], it["q"], it["why"]))
    return out


def render_sheet(sheet):
    """Render the A-I answer sheet as markdown."""
    L = []
    L.append("## Multi-Asset Due-Diligence Disclosure")
    L.append("")
    L.append("Source markers: **[user]** supplied by the user · "
             "**[engine]** verified property of the engine · "
             "**[UNKNOWN]** not answered by either — treat as a gap.")
    L.append("")
    for sid, sec in sheet.items():
        L.append(f"### {sid}. {sec['title']}")
        L.append("")
        for it in sec["items"]:
            mark = {"user": "user", "engine": "engine", "missing": "UNKNOWN"}[it["source"]]
            ans = it["answer"]
            if isinstance(ans, float):
                ans = f"{ans:.4g}"
            L.append(f"- **{it['key']}** — {it['q']}")
            L.append(f"  - *[{mark}]* {ans}")
            # The engine's own position on a question it does NOT answer. It
            # is printed as a note, never as the answer, so a user-only
            # question never carries an [engine] provenance marker.
            note = it.get("engine_note")
            if note:
                L.append(f"  - _engine note:_ {note}")
        L.append("")
    return "\n".join(L)


def coverage_summary(sheet):
    """Counts by provenance, for the headline of the report."""
    counts = {"user": 0, "engine": 0, "missing": 0}
    for sec in sheet.values():
        for it in sec["items"]:
            counts[it["source"]] += 1
    total = sum(counts.values())
    return {
        "total": total,
        "engine_answered": counts["engine"],
        "user_answered": counts["user"],
        "unknown": counts["missing"],
        "coverage_pct": (100.0 * (total - counts["missing"]) / total) if total else 0.0,
    }


if __name__ == "__main__":
    import sys
    path = sys.argv[1] if len(sys.argv) > 1 else None
    if path:
        with open(path, "r", encoding="utf-8") as f:
            spec = json.load(f)
        sheet = build_answers(spec)
        print(render_sheet(sheet))
        print(json.dumps(coverage_summary(sheet), indent=2))
    else:
        for sid, title, qs in SECTIONS:
            print(f"{sid}. {title}")
            for key, q, who, why in qs:
                print(f"   [{who:6s}] {key}: {q}")
