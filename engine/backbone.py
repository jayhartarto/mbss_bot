"""
engine/backbone.py — Shared EOD Backbone (MBSS v2, AB-RC1 research candidate,
user backtest — see backtest/MBSS_v317_AB_RC1_Final_Implementation_and_Research-1.md)

Computed ONCE per night on the full eligible Sharia universe, BEFORE any
primary scanner (/screendaytrade, /hc, /gptpick) does its own tool-specific
ranking. Two deterministic stages, per the research doc:

1. Danger Gate — reject candidates whose predicted_danger sits above a
   regime-specific quantile cutoff computed OVER TONIGHT'S OWN universe
   (cross-sectional, not a fixed absolute score) — "Q55" for R1, "Q35" for
   R3, per doc section 15.1. This is deliberately the auditable
   deterministic approximation the doc allows when no trained statistical
   model is shipped ("first implement an auditable deterministic
   approximation from the same factors").
2. Probability Rank — among gate survivors, rank by a blended probability
   score; take Top-8 (5-8 acceptable, NEVER force-filled to quota).

REUSE, not reinvent: danger_score's base is `100 - risk["score"]` from
compute_daytrade_v5_summary's existing `_score_breakout_drop_risk_v4`
sub-score (already covers MACD/ADX/liquidity/dist-to-high/vol_ratio/CMF/
RSI/RR/fade/ret1) — this module adds ONLY the danger inputs the doc lists
that v5's risk score does NOT already cover (day_range volatility,
Room, explicit bearish OBV divergence, explicit MACD bearish cross,
below-SMA50-AND-EMA21 compounding, near-price-floor, regime routing),
rather than re-deriving all of it from scratch.

Deterministic, unit-testable, versioned — every field this module computes
gets persisted (see engine/nightly.py's backbone_daily cache partition) so
a future MONTHLY walk-forward recalibration (see BACKBONE_FORMULA_VERSION)
has real forward data to work from. Per the doc: "Never train or modify
thresholds inside Telegram command execution" — any future retuning is a
manual, versioned, reviewed change, never an automatic in-request update.

Same MODULE-import rule as the rest of this refactor (see
engine/nightly.py's docstring) — `import engine.legacy_core as core` here,
never `from module import name`, never touch `core.xxx` at module level.
"""
from __future__ import annotations

import pandas as pd

import engine.legacy_core as core

BACKBONE_FORMULA_VERSION = "AB-RC1.1"  # AB-RC1 (doc section 24) + user fix: RR component now uses compute_rr_at_current_price (RR at last close) instead of risk_reward_at_max (RR at the top of the suggested entry range, which understates real risk once price has run past entry_max), weight 5%->15%. Bump (and log the reason) on any threshold/weight change; never silently re-tune.

# Regime-specific Danger Gate quantile cutoffs (doc section 15.1) — candidates
# with predicted_danger ABOVE this percentile of TONIGHT's own cross-sectional
# danger distribution are rejected. R2/R4/R5 not covered by the doc's forward
# validation window (all August sim dates were R1) — set conservatively
# (stricter than R1) rather than guessed loose, until forward-validated.
DANGER_GATE_QUANTILE_BY_REGIME = {
    "R1_BULL_STABLE": 0.55,
    "R2_BULL_HIGH_VOL": 0.40,   # doc: "highly selective until sufficient forward sample exists"
    "R3_SIDEWAYS": 0.35,
    "R4_RISK_OFF": 0.25,
    "R5_STRESS": 0.15,
    "R0_UNKNOWN": 0.25,          # no regime read -> treat cautiously, same tier as R4
}

BACKBONE_TOP_N = 8
BACKBONE_MIN_ACCEPTABLE = 5  # below this, doc says show fewer + state "market quality is limited" rather than force-fill


def _f(scoring: dict, key: str, default: float = 0.0) -> float:
    try:
        v = scoring.get(key)
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def compute_danger_score(scoring: dict, market_regime: str, day_range_percentile: float | None) -> float:
    """
    0-100, HIGHER = MORE DANGEROUS. Base = inverted V5 drop-risk sub-score
    (see module docstring for why this reuses rather than reinvents), then
    additive penalties for the doc's inputs that base doesn't cover.
    """
    v5 = core.compute_daytrade_v5_summary(scoring)
    danger = 100.0 - v5["risk"]["score"]  # base: MACD/ADX/liquidity/dist-to-high/vol_ratio/CMF/RSI/RR/fade/ret1

    # Room — doc lists this explicitly; a breakout with nowhere left to run
    # is dangerous even if v4's drop-risk factors look fine in isolation.
    room_score = v5["room"]["score"]
    if room_score < 40:
        danger += 12
    elif room_score < 55:
        danger += 6

    # Volatility position (day_range_pct_10d), cross-sectional percentile
    # within TONIGHT's universe — doc's "continuous volatility position,
    # including P90 context" as a backbone input, not a hard filter.
    if day_range_percentile is not None:
        if day_range_percentile >= 90:
            danger += 15
        elif day_range_percentile >= 75:
            danger += 7

    # Explicit flags the v4 risk score doesn't check directly.
    if scoring.get("obv_divergence") == "bearish_divergence":
        danger += 10
    if scoring.get("macd_bearish_cross"):
        danger += 8
    if scoring.get("is_below_sma50") and scoring.get("is_below_ema21"):
        danger += 10  # compounding weak-trend context, not just one MA
    if scoring.get("is_near_price_floor"):
        danger += 12
    if scoring.get("is_financial_distress_flag"):
        danger += 15

    # Regime routing — same danger inputs read differently depending on
    # whether the whole market is calm or stressed.
    danger += {"R4_RISK_OFF": 8, "R5_STRESS": 16, "R0_UNKNOWN": 8}.get(market_regime, 0)

    return round(max(0.0, min(100.0, danger)), 1)


def compute_rr_at_current_price(scoring: dict) -> float:
    """
    RR di HARGA TERAKHIR (close), bukan risk_reward_at_max (RR di batas ATAS
    range entry yang disarankan) — koreksi user (real case: range entry
    biasanya area pullback DI BAWAH harga sekarang, jadi kalau harga sudah
    lewat entry_max, risk_reward_at_max under-estimate risiko beli di harga
    sekarang; RR asli kalau masuk SEKARANG bisa lebih jelek dari yang
    ditampilkan). Dihitung lokal di backbone.py, TIDAK mengubah
    risk_reward_at_max di scoring['targets'] (field itu dipakai fitur lain
    di luar backbone — /check, /myportfolio, dll — scoped di sini saja).

    Returns 0 kalau harga sudah >= TP1 (tidak ada ruang naik lagi) atau
    harga sudah <= cut_loss (SL sudah kebobol secara definisi) — dua-duanya
    genuinely RR=0/negatif, bukan data hilang.
    """
    price = _f(scoring, "price")
    targets = scoring.get("targets") or {}
    tp_1 = _f(targets, "tp_1")
    cut_loss = _f(targets, "cut_loss")
    if not price or not tp_1 or not cut_loss or price <= cut_loss or price >= tp_1:
        return 0.0
    risk = price - cut_loss
    reward = tp_1 - price
    return round(reward / risk, 2) if risk > 0 else 0.0


def compute_probability_score(scoring: dict, market_regime: str) -> float:
    """
    0-100, HIGHER = BETTER. Blend of continuation quality, money-flow
    direction, trend reliability, relative strength, MACD state, Bollinger
    context, safety/VolQ/Room (reused from V5), and RR — per doc section
    3.2. Regime compatibility folded in as a small additive/subtractive
    adjustment (bull regimes reward relative strength more; sideways
    rewards safety more), not a hard multiplier, matching the doc's
    instruction that regime is a routing/posture signal, not a gate here.
    """
    v5 = core.compute_daytrade_v5_summary(scoring)
    cmf = _f(scoring, "cmf")
    adx = _f(scoring, "adx")
    rs_vs_ihsg = _f(scoring, "relative_strength_vs_ihsg")
    rr_now = compute_rr_at_current_price(scoring)

    # ADX reliability: a trend signal is only as trustworthy as the trend
    # strength behind it (see calculate_adx's own docstring/convention —
    # <20 weak/sideways, >=25 strong — reused here, not re-derived).
    adx_reliability = 100.0 if adx >= 25 else (60.0 if adx >= 20 else 30.0)

    # CMF as a 0-100 read (CMF ranges roughly -1..+1, same scaling
    # convention already used for cmf_adjustment in compute_factor_scoring).
    cmf_component = max(0.0, min(100.0, 50.0 + cmf * 100.0))

    # Relative strength vs IHSG, capped +-15% mapped to 0-100.
    rs_component = max(0.0, min(100.0, 50.0 + (max(-15.0, min(15.0, rs_vs_ihsg)) / 15.0) * 50.0))

    rr_component = max(0.0, min(100.0, rr_now * 40.0))  # RR 2.5:1 -> 100, dihitung di harga sekarang (lihat compute_rr_at_current_price)

    bollinger_component = 50.0
    if scoring.get("bollinger_squeeze"):
        bollinger_component += 10.0  # pre-breakout coil — informational tilt only, per earlier squeeze discussion, never a hard gate
    bb_note = scoring.get("bb_signal_note")
    if bb_note == "near_lower_band_bounce_candidate":
        bollinger_component += 8.0
    elif bb_note == "near_upper_band_caution":
        bollinger_component -= 8.0
    bollinger_component = max(0.0, min(100.0, bollinger_component))

    # rr_component dinaikkan dari 5% -> 15% (user request, real observation:
    # Top-8 sanity-check pertama SEMUANYA punya RR<1 tanpa ini menekan
    # urutan) -- ditrim dari continuation (20->15) dan CMF (15->10) supaya
    # bobot total tetap ~1.0.
    probability = (
        v5["continuation"]["score"] * 0.15
        + cmf_component * 0.10
        + adx_reliability * 0.10
        + rs_component * 0.15
        + bollinger_component * 0.05
        + v5["risk"]["score"] * 0.15   # Safety
        + v5["volq"]["score"] * 0.10
        + v5["room"]["score"] * 0.05
        + rr_component * 0.15
    )

    # Regime compatibility — small posture nudge, per doc section 4/15.
    if market_regime in ("R1_BULL_STABLE", "R2_BULL_HIGH_VOL"):
        probability += (rs_component - 50.0) * 0.05  # bull regime rewards genuine outperformers a bit more
    elif market_regime == "R3_SIDEWAYS":
        probability += (v5["risk"]["score"] - 50.0) * 0.05  # sideways rewards safety a bit more
    elif market_regime in ("R4_RISK_OFF", "R5_STRESS", "R0_UNKNOWN"):
        probability -= 5.0  # doc: "lower confidence and reduced output" in risk-off/stress

    return round(max(0.0, min(100.0, probability)), 1)


def compute_backbone(results: list, market_regime: str) -> dict:
    """
    Main entry point — called once per night from run_nightly_full_scan()
    AFTER scoring finishes, on the FULL eligible Sharia universe (`results`
    from fetch_tickers_scored_with_cache). Returns:

        {
            "top8": [ {...scoring, **backbone fields...}, ... ],   # 0-8 items, gate survivors ranked by probability, best first
            "all_scored": { ticker: {...backbone fields...}, ... }, # EVERY candidate's danger/probability/gate result, for tracking/backfill even if not in top8
            "market_regime": "...",
            "danger_gate_quantile": 0.xx,
            "formula_version": BACKBONE_FORMULA_VERSION,
        }
    """
    candidates = [r for r in results if r and r.get("ticker") and r.get("price")]

    day_range_values = [r.get("day_range_pct_10d") for r in candidates if r.get("day_range_pct_10d") is not None]

    scored = {}
    for r in candidates:
        drp = r.get("day_range_pct_10d")
        # Cross-sectional percentile vs the WHOLE night's universe (not this
        # ticker's own trailing history, unlike the RSI/volume adaptive
        # scores elsewhere) — self-inclusion bias is negligible at this
        # population size (~300+ tickers).
        day_range_pct = (
            round(core.percentile_rank(pd.Series(day_range_values), drp) * 100, 1)
            if drp is not None and day_range_values else None
        )
        danger = compute_danger_score(r, market_regime, day_range_pct)
        probability = compute_probability_score(r, market_regime)
        scored[r["ticker"]] = {
            "predicted_danger": danger,
            "probability_score": probability,
            "day_range_percentile": day_range_pct,
        }

    danger_values = [v["predicted_danger"] for v in scored.values()]
    gate_quantile = DANGER_GATE_QUANTILE_BY_REGIME.get(market_regime, DANGER_GATE_QUANTILE_BY_REGIME["R0_UNKNOWN"])
    danger_cutoff = _quantile(danger_values, gate_quantile) if danger_values else None

    for ticker, info in scored.items():
        info["passed_danger_gate"] = danger_cutoff is None or info["predicted_danger"] <= danger_cutoff

    survivors = [r for r in candidates if scored[r["ticker"]]["passed_danger_gate"]]
    survivors.sort(key=lambda r: scored[r["ticker"]]["probability_score"], reverse=True)

    top8 = []
    for rank, r in enumerate(survivors[:BACKBONE_TOP_N], 1):
        entry = {
            **r,
            **scored[r["ticker"]],
            "backbone_rank": rank,
            "backbone_score": scored[r["ticker"]]["probability_score"],
            "market_regime": market_regime,
            "formula_version": BACKBONE_FORMULA_VERSION,
        }
        top8.append(entry)

    return {
        "top8": top8,
        "all_scored": scored,
        "market_regime": market_regime,
        "danger_gate_quantile": gate_quantile,
        "danger_cutoff_value": danger_cutoff,
        "formula_version": BACKBONE_FORMULA_VERSION,
    }


def _quantile(values: list, q: float) -> float | None:
    """Simple linear-interpolation quantile, no numpy dependency needed for this list size."""
    if not values:
        return None
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    pos = q * (len(s) - 1)
    lo, hi = int(pos), min(int(pos) + 1, len(s) - 1)
    frac = pos - lo
    return s[lo] + (s[hi] - s[lo]) * frac
