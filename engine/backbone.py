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

import os
import json
import datetime

import pandas as pd

import engine.legacy_core as core

BACKBONE_FORMULA_VERSION = "AB-RC1.8"  # 1.8: tambah market_regime ke tiap all_scored[ticker] (supaya /winrate bisa disegmentasi per regime -- "snapshot granular utk walk-forward" user request). 1.7: tambah predicted_danger_vnext/danger_bucket_breakdown_vnext (SHADOW ONLY, tidak dipakai gate/rank) ke all_scored[ticker] -- lihat compute_danger_score_bucketed_vnext, dari mbss_formula_diagnosis_claude_agent.md's double-counting concern. ...(1.5 history above) + danger-adjusted rank_score (1.6, user request): Entry Rank ordering was pure probability_score, ignoring danger entirely once a survivor passed the gate -- two same-probability survivors with very different danger ranked identically. Added rank_score = probability_score - max(0, danger-30)*0.3 as the sort key (floor=30 deliberately loose, not strict -- backtest already shows the Danger Gate itself keeps dangerous-loss near 0%). Bump (and log the reason) on any threshold/weight/output-shape change; never silently re-tune.

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


def percentile_rank_list(values: list, value: float) -> float:
    """
    Thin wrapper over core.percentile_rank for plain Python lists —
    percentile_rank expects a pd.Series (calls .dropna() internally), and
    callers outside this module (e.g. commands/scan.py's Explosive Lane
    scoring) don't otherwise need a pandas import just for this. Returns
    0.0-1.0.
    """
    return core.percentile_rank(pd.Series(values), value) if values else 0.5


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

    # RR-at-current-price consistency fix (MBSS v2, user request — found
    # while writing the finance skill doc): the reused v4 base above still
    # penalizes RR internally using risk_reward_at_max (RR at the TOP of
    # the suggested entry range), the SAME stale measure probability_score
    # was fixed to stop using (compute_rr_at_current_price, RR at last
    # close) — meaning danger and probability were judging the SAME
    # ticker's RR by two different definitions. Add a direct penalty on
    # the CORRECT current-price RR so danger stays sensitive to real risk
    # regardless of what the embedded v4 measure assumed. Deliberately
    # additive (not a replacement of the v4 base) — some overlap with
    # probability_score's own rr_component is fine, they answer different
    # questions ("how dangerous" vs "how attractive").
    rr_now = compute_rr_at_current_price(scoring)
    if rr_now < 0.5:
        danger += 10
    elif rr_now < 1.0:
        danger += 5

    # Volatility position (day_range_pct_10d), cross-sectional percentile
    # within TONIGHT's universe — doc's "continuous volatility position,
    # including P90 context" as a backbone input, not a hard filter.
    if day_range_percentile is not None:
        if day_range_percentile >= 90:
            danger += 15
        elif day_range_percentile >= 75:
            danger += 7

    # Post-ARA chase risk (user real case: TEBE ranked #1 probability the
    # day after limit-up — continuation/CMF/volq all read very strong right
    # after an ARA, but that's precisely the classic gap-down/fade-next-day
    # setup, not genuine quality). RSI-overbought (already in the v4 base)
    # and RR (below) weren't enough alone to catch this — explicit penalty
    # on the single-day return itself. IDX auto-reject bands are price-tier
    # dependent (roughly 20-35%), so this is a deliberately blunt proxy
    # (same spirit as is_volume_spike_anomaly's vol_ratio>3.0 threshold
    # elsewhere in this codebase), not an exact ARA-band calculation.
    ret_1d = _f(scoring, "ret_1d_pct")
    if ret_1d >= 20:
        danger += 20
    elif ret_1d >= 12:
        danger += 10
    if scoring.get("is_volume_spike_anomaly"):
        danger += 6

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

    # Whitelist broker net-sell (MBSS v2, user request — "masukkan broksum
    # RapidAPI sebagai bonus/penalti"). whitelist_accumulation_net_pct sudah
    # dihitung GRATIS tiap /eodscan (whitelist sweep RapidAPI yang sudah
    # jalan, lihat apply_whitelist_accumulation_adjustment) — TIDAK ada call
    # RapidAPI tambahan di sini, cuma baca field yang sudah ada. Beda dari
    # endpoint RapidAPI lain yang baru online minggu ini, sinyal broker
    # whitelist ini SUDAH established (Bias Bandar sudah jadi penalti skor
    # inti sejak v3.16.0) — bukan sinyal belum-teruji, jadi aman masuk
    # Danger Gate langsung, tidak perlu fase forward-validation terpisah.
    # None kalau ticker di luar cakupan whitelist sweep -> 0 penalti (netral,
    # sesuai aturan "RapidAPI unavailable = netral" doc section 6.3).
    whitelist_net_pct = scoring.get("whitelist_accumulation_net_pct")
    whitelist_brokers = scoring.get("whitelist_num_brokers") or 0
    if whitelist_net_pct is not None and whitelist_net_pct <= -15 and whitelist_brokers >= 2:
        danger += 10  # smart-money net-sell kompak walau teknikal kelihatan oke -- pola distribusi

    # Regime routing — same danger inputs read differently depending on
    # whether the whole market is calm or stressed.
    danger += {"R4_RISK_OFF": 8, "R5_STRESS": 16, "R0_UNKNOWN": 8}.get(market_regime, 0)

    return round(max(0.0, min(100.0, danger)), 1)


DANGER_BUCKETED_VNEXT_VERSION = "shadow.1"  # SHADOW ONLY -- lihat compute_danger_score_bucketed_vnext, belum menggantikan compute_danger_score di mana pun

def compute_danger_score_bucketed_vnext(scoring: dict, market_regime: str, day_range_percentile: float | None) -> dict:
    """
    MBSS v2 (user request, dari mbss_formula_diagnosis_claude_agent.md
    "Risk 1: Double counting of related signals" — divalidasi manual
    terhadap compute_danger_score di atas: macd_bearish_cross (+8) dan
    rr_now (+10/+5) SAMA-SAMA sudah jadi input v4 base (100-v5['risk']
    ['score'], yang menurut docstring-nya sendiri mencakup MACD/ADX/
    liquidity/dist-to-high/vol_ratio/CMF/RSI/RR/fade/ret1) TAPI dapat
    penalti eksplisit KEDUA di atasnya juga -- real double-count, bukan
    kekhawatiran teoretis).

    SHADOW COMPUTATION SAJA — TIDAK menggantikan compute_danger_score di
    Danger Gate/rank_score/mana pun. daytrade_picks_history.json baru saja
    ke-reset ke nol (insiden numpy.bool_, lihat commit sebelumnya), jadi
    TIDAK ADA data buat validasi apakah versi bucketed ini genuinely lebih
    baik memisahkan pemenang dari yang kalah. Dipanggil PARALEL dengan
    compute_danger_score, hasilnya disimpan sebagai field TAMBAHAN di
    feature_snapshot (predicted_danger_vnext dst) supaya begitu histori
    cukup terkumpul lagi, kita punya data buat BANDINGKAN dua-duanya --
    baru pertimbangkan switch, sama disiplin dgn seluruh AB-RC1 (forward-
    validate dulu, jangan switch dari observasi/teori doang).

    STRUKTUR: v4 base (100-v5['risk']['score']) TETAP DIPAKAI APA ADANYA
    (REUSE, sudah teruji, bukan didekomposisi ulang -- itu perubahan jauh
    lebih besar dan berisiko tanpa data). Yang diubah HANYA lapisan
    penalti tambahan yang compute_danger_score susun ADDITIVE TANPA CAP --
    di sini dikelompokkan ke 5 kategori (TREND/FLOW/CHASE/LIQUIDITY/
    REGIME, persis kategori brief) + 1 kategori STRUCTURAL (distress flag,
    tidak cocok ke 5 kategori brief), MASING-MASING DIBATASI (capped)
    supaya 1 kategori (terutama CHASE -- di formula lama bisa sampai +48
    RAW dari room+rr+ret1d_spike+volume_spike sekaligus, jauh lebih besar
    dari kategori lain) tidak mendominasi total tanpa batas.

    Cap dipilih dari OBSERVASI actual max raw tiap kategori di formula
    lama (BUKAN angka brief yang dikarang tanpa data) -- placeholder,
    tetap perlu revalidasi setelah ada data forward.
    """
    v5 = core.compute_daytrade_v5_summary(scoring)
    base = 100.0 - v5["risk"]["score"]

    # TREND RISK (raw max lama: 18, cap dijaga sama)
    trend_risk = 0.0
    if scoring.get("macd_bearish_cross"):
        trend_risk += 8
    if scoring.get("is_below_sma50") and scoring.get("is_below_ema21"):
        trend_risk += 10
    trend_risk = min(trend_risk, 18)

    # FLOW RISK (raw max lama: 20, cap dijaga sama)
    flow_risk = 0.0
    if scoring.get("obv_divergence") == "bearish_divergence":
        flow_risk += 10
    whitelist_net_pct = scoring.get("whitelist_accumulation_net_pct")
    whitelist_brokers = scoring.get("whitelist_num_brokers") or 0
    if whitelist_net_pct is not None and whitelist_net_pct <= -15 and whitelist_brokers >= 2:
        flow_risk += 10
    flow_risk = min(flow_risk, 20)

    # CHASE RISK (raw max lama: 48 -- room 12 + rr 10 + ret1d 20 + volspike 6.
    # INI kategori yang paling butuh cap, dari lama-lama tidak dibatasi sama
    # sekali. Cap 25 dipilih supaya masih signifikan tapi tidak lagi bisa
    # mendominasi 2-3x kategori lain seperti sebelumnya -- placeholder,
    # perlu revalidasi.)
    room_score = v5["room"]["score"]
    chase_risk = 0.0
    if room_score < 40:
        chase_risk += 12
    elif room_score < 55:
        chase_risk += 6
    rr_now = compute_rr_at_current_price(scoring)
    if rr_now < 0.5:
        chase_risk += 10
    elif rr_now < 1.0:
        chase_risk += 5
    ret_1d = _f(scoring, "ret_1d_pct")
    if ret_1d >= 20:
        chase_risk += 20
    elif ret_1d >= 12:
        chase_risk += 10
    if scoring.get("is_volume_spike_anomaly"):
        chase_risk += 6
    chase_risk = min(chase_risk, 25)

    # LIQUIDITY/NOISE RISK (raw max lama: 27, cap 20 -- turun dari raw lama)
    liquidity_risk = 0.0
    if day_range_percentile is not None:
        if day_range_percentile >= 90:
            liquidity_risk += 15
        elif day_range_percentile >= 75:
            liquidity_risk += 7
    if scoring.get("is_near_price_floor"):
        liquidity_risk += 12
    liquidity_risk = min(liquidity_risk, 20)

    # REGIME RISK (sudah bounded by construction, raw max 16)
    regime_risk = {"R4_RISK_OFF": 8, "R5_STRESS": 16, "R0_UNKNOWN": 8}.get(market_regime, 0)
    regime_risk = min(regime_risk, 16)

    # STRUCTURAL RISK -- tidak cocok ke 5 kategori brief (distress bukan
    # trend/flow/chase/liquidity/regime, ini soal fundamental/kredibilitas
    # data), dikasih kategori sendiri, cap 15 (sama dgn raw lama, sudah
    # binary jadi tidak ada yang perlu dibatasi lebih jauh).
    structural_risk = 15.0 if scoring.get("is_financial_distress_flag") else 0.0

    total = base + trend_risk + flow_risk + chase_risk + liquidity_risk + regime_risk + structural_risk
    return {
        "predicted_danger_vnext": round(max(0.0, min(100.0, total)), 1),
        "danger_bucket_breakdown_vnext": {
            "base_v4": round(base, 1),
            "trend_risk": round(trend_risk, 1),
            "flow_risk": round(flow_risk, 1),
            "chase_risk": round(chase_risk, 1),
            "liquidity_risk": round(liquidity_risk, 1),
            "regime_risk": round(regime_risk, 1),
            "structural_risk": round(structural_risk, 1),
        },
        "formula_version": DANGER_BUCKETED_VNEXT_VERSION,
    }


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

    # Whitelist broker net-buy (MBSS v2, user request — see compute_danger_score
    # for why this is safe to use directly: established Bias Bandar signal,
    # zero extra RapidAPI cost, already reads None/neutral when unavailable).
    whitelist_net_pct = scoring.get("whitelist_accumulation_net_pct")
    whitelist_brokers = scoring.get("whitelist_num_brokers") or 0
    if whitelist_net_pct is not None and whitelist_net_pct >= 15 and whitelist_brokers >= 2:
        probability += 5.0  # bounded confirmation bonus — smart money net-buy, doesn't rescue a fundamentally poor setup on its own

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
            # MBSS v2 (user request — "snapshot seluruh detail sebisa mungkin
            # supaya walk-forward ke depan bisa lebih granular"): market_regime
            # itu sendiri GLOBAL per malam (bukan per-ticker), tapi disimpan DI
            # SINI (per-ticker entry) supaya lock_daily_daytrade_picks bisa
            # ambil lewat lookup yang SAMA (backbone_lookup) tanpa parameter
            # terpisah -- tanpa ini, hasil /winrate tidak bisa disegmentasi per
            # regime sama sekali (padahal DANGER_GATE_QUANTILE_BY_REGIME/
            # EXPLOSIVE_MIN_SCORE_BY_REGIME semuanya regime-adaptive dan
            # butuh data forward PER REGIME buat divalidasi).
            "market_regime": market_regime,
        }
        # SHADOW ONLY (user request, dari mbss_formula_diagnosis_claude_agent.md
        # -- lihat compute_danger_score_bucketed_vnext docstring): TIDAK
        # dipakai Danger Gate/rank_score, cuma dihitung + disimpan ke
        # `scored[ticker]` (BUKAN `r` -- `r` sudah TERSIMPAN ke
        # daily_scan_cache SEBELUM compute_backbone ini jalan, lihat
        # run_nightly_full_scan; mutasi ke `r` tidak akan pernah persist.
        # `scored` yang jadi backbone_result["all_scored"], itu yang
        # BENERAN disimpan lewat save_backbone_daily) — supaya caller bisa
        # ambil predicted_danger_vnext dari situ, sama seperti
        # entry_rank/probability_score yang sudah ada.
        try:
            danger_vnext = compute_danger_score_bucketed_vnext(r, market_regime, day_range_pct)
            scored[r["ticker"]]["predicted_danger_vnext"] = danger_vnext["predicted_danger_vnext"]
            scored[r["ticker"]]["danger_bucket_breakdown_vnext"] = danger_vnext["danger_bucket_breakdown_vnext"]
        except Exception as e:
            print(f"⚠️ Gagal hitung danger bucketed vnext (shadow) untuk {r.get('ticker')}: {e}")

    danger_values = [v["predicted_danger"] for v in scored.values()]
    gate_quantile = DANGER_GATE_QUANTILE_BY_REGIME.get(market_regime, DANGER_GATE_QUANTILE_BY_REGIME["R0_UNKNOWN"])
    danger_cutoff = _quantile(danger_values, gate_quantile) if danger_values else None

    for ticker, info in scored.items():
        info["passed_danger_gate"] = danger_cutoff is None or info["predicted_danger"] <= danger_cutoff

    survivors = [r for r in candidates if scored[r["ticker"]]["passed_danger_gate"]]

    # Entry Rank ranking key (MBSS v2, user request — ditemukan lewat
    # pertanyaan tajam: dua survivor probability SAMA tapi danger jauh
    # beda tetap rank SAMA sebelum ini, karena danger cuma dipakai sebagai
    # gate pass/fail, tidak pernah jadi pengurang rank DI ANTARA sesama
    # survivor). DANGER_RANK_PENALTY_FLOOR (30) = di bawah ini danger
    # dianggap genuinely rendah, TIDAK dihukum sama sekali (riset backtest
    # sudah tunjukkan Danger Gate sendiri sudah menekan dangerous-loss
    # <=-10% ke ~0%, jadi tidak perlu lapisan rank yang terlalu konservatif
    # lagi di atasnya — floor dipilih LEBIH LONGGAR, bukan lebih ketat,
    # atas alasan itu). Danger DI ATAS floor mengurangi rank_score linear
    # (bobot 0.3/poin). Ini parameter yang HARUS dievaluasi ulang dari
    # data forward (entry_rank/predicted_danger/probability_score sudah
    # otomatis ter-track) — bukan angka final.
    DANGER_RANK_PENALTY_FLOOR = 30.0
    DANGER_RANK_PENALTY_WEIGHT = 0.3
    for ticker, info in scored.items():
        danger_headroom = max(0.0, info["predicted_danger"] - DANGER_RANK_PENALTY_FLOOR)
        info["rank_score"] = round(info["probability_score"] - danger_headroom * DANGER_RANK_PENALTY_WEIGHT, 1)

    survivors.sort(key=lambda r: scored[r["ticker"]]["rank_score"], reverse=True)

    # Entry Rank (MBSS v2, user request): peringkat di antara SEMUA gate
    # survivor malam ini (bukan cuma di antara 8 nama Top-8) — angka
    # UNIVERSAL yang ditampilkan konsisten di /hc, /screendaytrade,
    # /consensus untuk ticker manapun yang lolos gate, bukan cuma yang
    # kebetulan masuk Top-8. #1 = rank_score tertinggi malam ini
    # (probability didiskon danger di atas floor, lihat catatan di atas).
    entry_rank_total = len(survivors)
    for rank, r in enumerate(survivors, 1):
        scored[r["ticker"]]["entry_rank"] = rank
        scored[r["ticker"]]["entry_rank_total"] = entry_rank_total

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


def filter_to_gate_survivors(candidates: list, backbone_result: dict | None) -> list:
    """
    MBSS v2 (AB-RC1 doc section 5): SDT/HC "receive the backbone candidate
    pool" before applying their own tool-specific ranking — filters a list
    of scoring dicts down to tonight's Danger-Gate survivors (NOT just the
    narrow Top-8; the doc's Top-8 is used for Consensus Prime/Explosive
    Lane specifically, see doc section 6.1, while SDT/HC each need a wider
    pool to keep finding DIFFERENT candidates via their own existing lane
    logic rather than nearly duplicating each other's output).

    If `backbone_result` is missing (e.g. /eodscan hasn't run yet since
    this shipped), returns `candidates` UNCHANGED — same "degrade
    gracefully, never hard-block" convention as RapidAPI-missing/cache-stale
    handling elsewhere in this codebase.
    """
    if not backbone_result:
        return candidates
    all_scored = backbone_result.get("all_scored", {})
    return [r for r in candidates if all_scored.get(r.get("ticker"), {}).get("passed_danger_gate")]


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


# ==========================================
# Consensus Prime position-state + cooldown (AB-RC1 doc section 16-17,
# MBSS v2, user request). Simplified from the doc's literal NEW/ACTIVE/
# UPGRADED three-state model to NEW/ACTIVE — UPGRADED (a single-lane pick
# later confirmed by the other lane) needs tracking EVERY single-lane SDT-
# only/HC-only pick too, not just Prime intersections, which is a bigger
# state-tracking surface than this pass covers. Flagged here rather than
# silently doing less than the doc describes.
#
# The core risk-control pieces ARE implemented: no duplicate entry signal
# while a tracked position is still active, and a cooldown after a tracked
# position resolves via stop-loss (doc section 17: "2-3 trading days
# unless /check confirms a valid reclaim" — the reclaim override is NOT
# automated here, a human still decides that via /check).
# ==========================================
CONSENSUS_STATE_FILE = os.path.join(core.PROJECT_ROOT, "consensus_position_state.json")
COOLDOWN_TRADING_DAYS_AFTER_SL = 3
MAX_HOLD_TRADING_DAYS = 5  # doc section 11: default holding period for Consensus/Explosive short-horizon picks


def load_consensus_position_state() -> dict:
    if not os.path.exists(CONSENSUS_STATE_FILE):
        return {}
    try:
        with open(CONSENSUS_STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ Gagal baca consensus_position_state.json: {e}")
        return {}


def save_consensus_position_state(state: dict):
    try:
        with open(CONSENSUS_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
    except Exception as e:
        print(f"⚠️ Gagal simpan consensus_position_state.json: {e}")


def _add_trading_days(date_str: str, n: int) -> str:
    d = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
    added = 0
    while added < n:
        d += datetime.timedelta(days=1)
        if d.weekday() < 5:
            added += 1
    return str(d)


def resolve_consensus_positions(state: dict, scored: dict) -> dict:
    """
    Called EVERY /consensus run, BEFORE classifying today's fresh picks —
    checks every tracked ACTIVE position against tonight's close: hit TP,
    hit SL (starts cooldown), or timed out (MAX_HOLD_TRADING_DAYS, doc
    section 11). Ticker missing from tonight's scored cache (delisted,
    suspended, dropped out of the eligible universe) is left untouched
    rather than guessed at. Mutates and returns `state`.
    """
    today = core.get_current_trading_day_close_marker()
    for ticker, info in state.items():
        if info.get("position_status") != "ACTIVE":
            continue
        r = scored.get(ticker)
        price = r.get("price") if r else None
        if not price:
            continue
        tp, sl = info.get("planned_tp"), info.get("planned_sl")
        days_held = core.compute_trading_days_held(info.get("entry_date")) or 0
        if tp and price >= tp:
            info["position_status"] = "RESOLVED_TP"
            info["resolved_date"] = today
        elif sl and price <= sl:
            info["position_status"] = "RESOLVED_SL"
            info["resolved_date"] = today
            info["cooldown_until"] = _add_trading_days(today, COOLDOWN_TRADING_DAYS_AFTER_SL)
        elif days_held >= MAX_HOLD_TRADING_DAYS:
            info["position_status"] = "RESOLVED_TIME"
            info["resolved_date"] = today
    return state


def classify_and_update_consensus_entry(ticker: str, r: dict, state: dict) -> str:
    """
    Call ONLY for tickers that qualify as Consensus Prime TODAY (Backbone
    Top-8 ∩ SDT ∩ HC). Returns one of:
      "NEW"              — fresh signal, state entry created (entry_date=today).
      "ACTIVE"           — already an open tracked position; last_confirmation_date
                            bumped, entry_date/planned_tp/planned_sl UNCHANGED
                            (doc section 16: no new entry, just reconfirmed).
      "COOLDOWN_BLOCKED" — resolved via SL recently, still inside cooldown;
                            caller should NOT display this as an entry signal.
    Mutates `state` in place (caller is responsible for saving it).
    """
    today = core.get_current_trading_day_close_marker()
    info = state.get(ticker)

    if info and info.get("position_status") == "ACTIVE":
        info["last_confirmation_date"] = today
        return "ACTIVE"

    if info and info.get("cooldown_until") and today < info["cooldown_until"]:
        return "COOLDOWN_BLOCKED"

    targets = r.get("targets") or {}
    state[ticker] = {
        "position_status": "ACTIVE",
        "first_signal_date": today,
        "last_confirmation_date": today,
        "entry_date": today,
        "planned_tp": targets.get("tp_1"),
        "planned_sl": targets.get("cut_loss"),
        "cooldown_until": None,
    }
    return "NEW"


# ==========================================
# AB-RC3 Tactical Live Rank (MBSS v2, user request — /check jadi lebih
# tactical: konfirmasi cepat apakah sinyal EOD masih valid di harga live
# sekarang, plus keputusan taktis kalau ticker itu dipegang di portofolio).
# Source of truth: backtest/Tactical_Check_Decision_Engine_Claude.md
# (AB-RC3), TAPI scope dipangkas signifikan dari dokumen aslinya setelah
# review Section 53 bersama user — lihat catatan per fungsi kenapa.
#
# LIVE dari awal (bukan shadow-mode) — MBSS v2, keputusan user: logic ini
# disusun dari data yang SUDAH tampil di /check (VWAP, active_breakout,
# momentum), cuma bobot agregasinya yang baru — sama seperti backbone
# (Danger/Probability) yang juga langsung tampil lalu diperbaiki dari
# observasi nyata, bukan didiamkan dulu di belakang layar. Tetap disimpan
# ke tactical_shadow_log.json (save_tactical_shadow_snapshot) sebagai
# bahan riset/evaluasi ke depan.
#
# Simplifikasi disengaja vs dokumen AB-RC3 asli:
# - TIDAK reuse "PortfolioRankService" (belum pernah dibangun/AB-RC2 belum
#   ada) — Base Opportunity di sini langsung reuse probability_score/
#   predicted_danger dari compute_backbone (AB-RC1) yang SUDAH ada.
# - Setup classifier dipangkas dari 7 tipe (Pullback/VWAP Reclaim/
#   Breakout/Squeeze/Momentum/Mean Reversion/Failed) jadi 3 STATE
#   (VALID/RETEST/INVALID) — pullback-to-support dan retest-breakout
#   sengaja DIGABUNG ke RETEST (makna praktisnya sama: "mundur ke level
#   kunci tanpa breakdown, belum gagal belum konfirmasi").
# - Smart-Money Support TIDAK dapat skor numerik dari data same-session
#   (kita tidak fetch broker live per desain kuota) — reuse
#   classify_bias_bandar (SUDAH ADA, trend net-buy hari-ke-hari dari
#   whitelist sweep semalam) sebagai gantinya, dilabeli jelas "posisi
#   kemarin malam".
# - TIDAK ada Rotation Engine, TIDAK ada Live Opportunity Cache — itu
#   scope AB-RC3 Phase 2+, ditunda per kesepakatan.
# ==========================================

TACTICAL_FORMULA_VERSION = "AB-RC3-shadow.4"  # .4: tambah winrate_tag (WR xx% dari /winrate real) di output classify_extended_beyond_plan buat ditampilkan di /check; .3: classify_extended_beyond_plan diselaraskan dgn lane SDT "EXTENDED / CHASE WATCH" (74% win real, n=23) / "LOW EDGE / CHASE" (28% win real, n=29) sbg sinyal utama, kriteria ala-BSJP jadi sekunder


def compute_tactical_support_zone(scoring: dict) -> dict | None:
    """
    Referensi support BARU buat state EXTENDED (harga sudah lewat rencana
    EOD) — VWAP sesi sebagai Tactical Cut, MENGGANTIKAN cut_loss EOD yang
    sudah tidak relevan (terlalu jauh di bawah harga sekarang). MBSS v2
    (user request, real case: TMPO high 190 close 158) — VWAP dipilih
    karena itu level "harga rata-rata tertimbang volume" sesi ini, jauh
    lebih relevan buat entry yang terjadi setelah harga sudah berlari,
    daripada level EOD kemarin yang sudah jauh tertinggal.

    Returns None kalau VWAP tidak tersedia (di luar jam bursa/data gagal)
    -- caller HARUS tampilkan ini sebagai "tidak bisa dihitung", bukan
    diam-diam pakai angka lain.
    """
    ab = scoring.get("active_breakout") or {}
    vwap = ab.get("vwap")
    if not ab.get("available") or not vwap:
        return None
    return {
        "support": round(float(vwap)),
        "tactical_cut": round(float(vwap) * 0.98),  # ~2% buffer di bawah VWAP -- longgar sengaja, VWAP sendiri sudah jadi garis batas utama
    }


def classify_extended_beyond_plan(scoring: dict) -> dict:
    """
    Harga sudah >= TP1 EOD — cek kriteria ala-BSJP-ARA (reuse KRITERIA-nya
    dari commands/scan.py's BSJP thresholds, BUKAN mesin live-fetch
    beratnya — /check sudah punya data live yang setara) buat bedain
    momentum genuinely kuat dari sekadar pop sesaat.

    MBSS v2 (user request, berbasis data /winrate real — 442 pick): SELARASKAN
    dengan lane SDT "EXTENDED / CHASE WATCH" yang sudah PUNYA track record
    nyata (74% win, n=23, avg+3.7%/pick) — dipakai sebagai sinyal UTAMA,
    bukan cuma kriteria ala-BSJP di atas yang belum ada bukti empiris.
    Kebalikannya, lane "LOW EDGE / CHASE" tercatat jelek (28% win, n=29,
    avg-2.2%/pick) jadi dipakai buat menolak chase juga. compute_screendaytrade_positive_bias
    dipanggil langsung dari `scoring` yang sudah ada (active_breakout,
    whitelist_accumulation_net_pct, dst sudah ke-enrich di /check) — REUSE,
    bukan fetch/hitung ulang apa pun.
    """
    ret_1d = _f(scoring, "ret_1d_pct")
    rsi = scoring.get("rsi")
    ab = scoring.get("active_breakout") or {}
    vol_pace = ab.get("volume_pace_ratio")
    vwap_movement = scoring.get("vwap_movement") or {}
    vwap_bullish = vwap_movement.get("overall_signal") == "bullish"

    prev_close = scoring.get("price") / (1 + ret_1d / 100) if ret_1d and ret_1d > -100 else None
    ara_distance = core.compute_ara_distance_pct(scoring.get("price"), prev_close) if prev_close else None
    ara_has_room = ara_distance is None or ara_distance >= 3.0  # masih ada ruang sebelum limit, bukan sudah mepet

    checks = {
        "gain_5pct": ret_1d is not None and ret_1d >= 5.0,
        "volume_2x": vol_pace is not None and vol_pace >= 2.0,
        "rsi_60_85": rsi is not None and 60 <= rsi <= 85,
        "vwap_bullish": vwap_bullish,
        "ara_room": ara_has_room,
    }
    met = sum(1 for v in checks.values() if v)

    try:
        sdt_lane = core.compute_screendaytrade_positive_bias(scoring).get("lane")
    except Exception:
        sdt_lane = None

    support = compute_tactical_support_zone(scoring)
    support_note = f" Tactical Cut (VWAP): {support['tactical_cut']}." if support else " VWAP tidak tersedia, tactical cut tidak bisa dihitung."

    # winrate_tag: HANYA diisi kalau state ini ditentukan oleh lane SDT yang
    # PUNYA data /winrate real di baliknya (bukan fallback kriteria ala-BSJP,
    # yang belum ada bukti empiris) — supaya angka WR yang ditampilkan ke
    # user selalu bisa dipertanggungjawabkan sumbernya.
    if sdt_lane == "LOW EDGE / CHASE":
        return {"state": "EXTENDED_NO_CHASE", "winrate_tag": "WR 28% (n=29)", "reasons": [
            f"Harga sudah lewat TP1 EOD, dan lane SDT membaca ini 'LOW EDGE / CHASE'"
            f" (track record nyata cuma 28% win, n=29) — {met}/5 kriteria ala-BSJP tidak cukup mengimbangi." + support_note
        ]}

    if sdt_lane == "EXTENDED / CHASE WATCH" or met >= 4:
        reason_bits = []
        if sdt_lane == "EXTENDED / CHASE WATCH":
            reason_bits.append("lane SDT 'EXTENDED / CHASE WATCH' (track record nyata 74% win, avg+3.7%/pick)")
        if met >= 4:
            reason_bits.append(f"{met}/5 kriteria ala-BSJP terpenuhi")
        reason = " dan ".join(reason_bits)
        return {
            "state": "EXTENDED_CHASE",
            "winrate_tag": "WR 74% (n=23)" if sdt_lane == "EXTENDED / CHASE WATCH" else None,
            "reasons": [
                f"Harga sudah lewat TP1 EOD, TAPI momentum tervalidasi ({reason})."
                f" Rencana EOD lama SUDAH SELESAI — ini kejar momentum risiko tinggi disadari, bukan entry sesuai plan semalam." + support_note
            ],
        }

    return {"state": "EXTENDED_NO_CHASE", "reasons": [
        f"Harga sudah lewat TP1 EOD TANPA dukungan kuat (cuma {met}/5 kriteria ala-BSJP, lane SDT: {sdt_lane or 'n/a'})."
        f" Jangan kejar di sini — tunggu /eodscan berikutnya buat plan baru, atau retrace ke VWAP dulu." + support_note
    ]}


def classify_signal_validity(scoring: dict) -> dict:
    """
    3-state simplifikasi dari setup classifier AB-RC3 (lihat catatan modul
    di atas): apakah struktur LIVE hari ini masih mendukung premis sinyal
    EOD semalam. Butuh `active_breakout`/`intraday_momentum` sudah
    ter-enrich di `scoring` (persis field yang sudah dipakai /check,
    /screendaytrade live).

    Returns {"state": "VALID"|"RETEST"|"INVALID"|"UNKNOWN", "reasons": [...]}
    """
    ab = scoring.get("active_breakout") or {}
    im = scoring.get("intraday_momentum") or {}
    price = _f(scoring, "price")
    cut_loss = _f((scoring.get("targets") or {}), "cut_loss")

    # Data live tidak tersedia sama sekali (di luar jam bursa / gagal fetch)
    # -- jangan menebak, transparan "belum bisa dinilai".
    if not ab.get("available") and not im.get("available"):
        return {"state": "UNKNOWN", "reasons": ["Data live tidak tersedia (di luar jam bursa atau gagal fetch)."]}

    # EXTENDED (rezim BEDA, dicek DULUAN): harga sudah lewat TP1 EOD --
    # rencana semalam sudah "selesai"/terlampaui, cut_loss lama sudah
    # tidak relevan (terlalu jauh di bawah). MBSS v2 (user request, real
    # case: TMPO high 190 close 158 -- risiko fade besar kalau chase tanpa
    # referensi baru) -- BUKAN auto-INVALID, tapi juga tidak auto-hijau;
    # cek kriteria ala-BSJP-ARA (fitur SUDAH ADA, /bsjp) dari data yang
    # sudah tersedia di /check, buat bedain "momentum kuat tervalidasi"
    # dari "cuma pop sesaat".
    tp1 = _f((scoring.get("targets") or {}), "tp_1")
    if tp1 and price and price >= tp1:
        return classify_extended_beyond_plan(scoring)

    # INVALID paling kuat: harga sudah tembus cut_loss EOD -- premis
    # sinyal semalam sudah terbantahkan telak, ini beda dari Hard SL
    # portofolio (itu dicek terpisah di compute_tactical_decision).
    if cut_loss and price and price <= cut_loss:
        return {"state": "INVALID", "reasons": [f"Harga ({price:.0f}) sudah di bawah cut_loss EOD ({cut_loss:.0f})."]}

    vwap_dist = ab.get("vwap_distance_pct")
    vol_pace = ab.get("volume_pace_ratio")
    momentum_change = im.get("change_pct") if im.get("available") else None
    momentum_bearish = momentum_change is not None and momentum_change < -1.0
    weak_volume = vol_pace is None or vol_pace < 0.8

    below_vwap_material = vwap_dist is not None and vwap_dist < -1.0

    if below_vwap_material and weak_volume and momentum_bearish:
        return {"state": "INVALID", "reasons": [
            f"Breakdown VWAP ({vwap_dist:+.1f}%) dengan volume lemah (pace {vol_pace}) DAN momentum sesi negatif ({momentum_change:+.1f}%)."
        ]}

    if below_vwap_material or (vwap_dist is not None and -1.0 <= vwap_dist < 0):
        return {"state": "RETEST", "reasons": [f"Harga di bawah/dekat VWAP ({vwap_dist:+.1f}%), belum breakdown tegas."]}

    return {"state": "VALID", "reasons": ["Struktur live (VWAP/momentum) masih mendukung premis EOD."]}


def compute_tactical_live_rank(scoring: dict, backbone_info: dict | None, daily_broksum_history: dict) -> dict:
    """
    Base Opportunity (EOD, reuse langsung dari compute_backbone — TIDAK
    reimplementasi) + lapisan penyesuaian live (VWAP, volume pace,
    momentum sesi, trend broker whitelist hari-ke-hari via
    classify_bias_bandar, RR di harga sekarang). Bounded 0-100.

    `backbone_info` = backbone_result["all_scored"].get(ticker) dari
    caller — None kalau ticker di luar cakupan /eodscan malam ini (base
    default netral 50, bukan 0, supaya tidak menghukum ticker yang cuma
    kebetulan di luar radar EOD).
    """
    base = backbone_info.get("probability_score", 50.0) if backbone_info else 50.0
    adjustment = 0.0
    components = {}

    ab = scoring.get("active_breakout") or {}
    im = scoring.get("intraday_momentum") or {}

    if ab.get("available"):
        vwap_dist = ab.get("vwap_distance_pct")
        if vwap_dist is not None:
            if vwap_dist > 0:
                adjustment += 8
                components["vwap"] = f"+8 (di atas VWAP, {vwap_dist:+.1f}%)"
            elif vwap_dist < -1.0:
                adjustment -= 10
                components["vwap"] = f"-10 (di bawah VWAP, {vwap_dist:+.1f}%)"

        vol_pace = ab.get("volume_pace_ratio")
        if vol_pace is not None:
            if vol_pace >= 1.5:
                adjustment += 5
                components["volume_pace"] = f"+5 (pace {vol_pace}x)"
            elif vol_pace < 0.7:
                adjustment -= 5
                components["volume_pace"] = f"-5 (pace {vol_pace}x, lemah)"

    if im.get("available") and im.get("change_pct") is not None:
        change = im["change_pct"]
        if change >= 1.0:
            adjustment += 5
            components["momentum"] = f"+5 (momentum sesi {change:+.1f}%)"
        elif change <= -1.0:
            adjustment -= 5
            components["momentum"] = f"-5 (momentum sesi {change:+.1f}%)"

    bias = broker_engine_classify_bias_bandar_safe(scoring.get("ticker"), daily_broksum_history)
    if bias:
        label = bias.get("label")
        if label in ("AKUMULASI SEGAR", "PULLBACK DIDUKUNG"):
            adjustment += 5
            components["bias_bandar"] = f"+5 ({label}, data semalam)"
        elif label == "DISTRIBUSI":
            adjustment -= 8
            components["bias_bandar"] = f"-8 ({label}, data semalam)"
        elif label == "AKUMULASI BASI":
            adjustment -= 3
            components["bias_bandar"] = f"-3 ({label}, data semalam)"

    rr_now = compute_rr_at_current_price(scoring)
    if rr_now < 0.5:
        adjustment -= 8
        components["live_rr"] = f"-8 (RR@sekarang {rr_now}, tipis)"

    live_rank = round(max(0.0, min(100.0, base + adjustment)), 1)
    eod_rank = backbone_info.get("entry_rank") if backbone_info else None
    eod_rank_total = backbone_info.get("entry_rank_total") if backbone_info else None

    # RR EOD vs LIVE (MBSS v2, user request — poin 1: "RR live bisa diberi
    # perubahan +/- juga dibanding EOD"). rr_eod = risk_reward_at_max, angka
    # yang SAMA sudah ditampilkan di /hc, /screendaytrade, /consensus --
    # dipakai sebagai baseline biar user bandingkan dengan apa yang sudah
    # familiar, walau secara teknis dihitung di entry_max bukan close EOD.
    rr_eod = _f((scoring.get("targets") or {}), "risk_reward_at_max", None)

    return {
        "eod_probability": base,
        "live_rank": live_rank,
        "delta": round(live_rank - base, 1),
        "eod_entry_rank": eod_rank,
        "eod_entry_rank_total": eod_rank_total,
        "rr_eod": rr_eod,
        "rr_now": rr_now,
        "rr_delta": round(rr_now - rr_eod, 2) if rr_eod is not None else None,
        "components": components,
        "bias_bandar": bias,
        "formula_version": TACTICAL_FORMULA_VERSION,
    }


def broker_engine_classify_bias_bandar_safe(ticker: str | None, daily_broksum_history: dict):
    """Local import (see lock_daily_daytrade_picks in legacy_core.py for why: avoids a module-level circular import with engine/broker.py)."""
    if not ticker:
        return None
    try:
        import engine.broker as broker_engine
        return broker_engine.classify_bias_bandar(ticker, daily_broksum_history)
    except Exception as e:
        print(f"⚠️ Gagal classify_bias_bandar buat {ticker}: {e}")
        return None


def compute_tactical_decision(scoring: dict, validity: dict, is_held: bool) -> dict:
    """
    Terjemahkan validity state (+ konteks portofolio) jadi 1 keputusan
    taktis. TIDAK OTOMATIS EKSEKUSI apa pun — ini murni label buat
    dibaca manusia, disimpan buat shadow-mode review.
    """
    state = validity["state"]
    price = _f(scoring, "price")
    tp1 = _f((scoring.get("targets") or {}), "tp_1")

    if state == "UNKNOWN":
        return {"decision": "DATA_UNAVAILABLE", "note": "Data live tidak cukup untuk keputusan taktis."}

    if is_held:
        if state == "EXTENDED_CHASE":
            return {"decision": "TRAIL_PROFIT", "note": "Sudah lewat TP1 dengan momentum tervalidasi — pertimbangkan trailing (jual sebagian, biarkan sisanya lanjut), bukan full-exit paksa."}
        if state == "EXTENDED_NO_CHASE":
            return {"decision": "FULL_TP", "note": "Sudah lewat TP1 TANPA dukungan momentum kuat — ambil profit penuh sekarang, jangan serakah tunggu lebih tinggi."}
        if state == "INVALID":
            return {"decision": "EARLY_EXIT_WATCH", "note": "Premis sinyal sudah terbantahkan live — pertimbangkan keluar lebih awal, ini BUKAN Hard SL struktural."}
        if tp1 and price and price >= tp1:
            return {"decision": "PREPARE_TP", "note": "Harga sudah di area TP1 — siap-siap ambil profit."}
        if state == "RETEST":
            return {"decision": "HOLD_MONITOR", "note": "Mundur ke level kunci tanpa breakdown — tahan, pantau ketat."}
        return {"decision": "HOLD", "note": "Struktur live masih mendukung, tidak ada aksi mendesak."}

    # Ticker belum dipegang (kandidat entry baru / live discovery manual)
    if state == "EXTENDED_CHASE":
        return {"decision": "CHASE_OPPORTUNITY", "note": "Rencana EOD terlampaui, TAPI momentum tervalidasi — peluang kejar risiko tinggi disadari, pakai Tactical Cut (VWAP) di atas, BUKAN cut_loss EOD lama."}
    if state == "EXTENDED_NO_CHASE":
        return {"decision": "SKIP_EXTENDED", "note": "Rencana EOD terlampaui tanpa dukungan momentum — jangan kejar, tunggu /eodscan berikutnya atau retrace ke VWAP."}
    if state == "INVALID":
        return {"decision": "SKIP", "note": "Sinyal EOD sudah tidak didukung struktur live."}
    if state == "RETEST":
        return {"decision": "WAIT_RETEST", "note": "Tunggu konfirmasi — jangan entry di tengah retest."}
    rr_now = compute_rr_at_current_price(scoring)
    if rr_now < 0.5:
        return {"decision": "NO_CHASE", "note": f"RR di harga sekarang cuma {rr_now} — reward tersisa terlalu tipis."}
    return {"decision": "WATCH_VALID", "note": "Sinyal EOD masih valid live — bukan auto 'beli sekarang', tetap perlu penilaian entry sendiri."}


TACTICAL_SHADOW_LOG_FILE = os.path.join(core.PROJECT_ROOT, "tactical_shadow_log.json")
TACTICAL_SHADOW_LOG_MAX_ENTRIES = 3000  # cap pertumbuhan file -- ini log observasi Phase 1, bukan arsip permanen


def save_tactical_shadow_snapshot(ticker: str, validity: dict, tactical_rank: dict, decision: dict, is_held: bool):
    """
    Phase 1 shadow-mode logging (AB-RC3) — append-only, dipanggil dari
    /check TIAP kali (tidak idempotent per hari seperti lock_daily_
    daytrade_picks, sengaja: tactical state bisa berubah beberapa kali
    sehari, kita justru mau lihat perubahannya). Dibaca manual/ad-hoc
    nanti buat evaluasi apakah state VALID/RETEST/INVALID dan keputusan
    yang dihasilkan masuk akal dibanding apa yang BENERAN terjadi —
    belum ada command/script pembaca otomatis di Phase 1 ini.
    """
    try:
        log = []
        if os.path.exists(TACTICAL_SHADOW_LOG_FILE):
            with open(TACTICAL_SHADOW_LOG_FILE, encoding="utf-8") as f:
                log = json.load(f)
        log.append({
            "ticker": ticker,
            "timestamp": datetime.datetime.now(core.WIB).isoformat(),
            "is_held": is_held,
            "validity_state": validity.get("state"),
            "validity_reasons": validity.get("reasons"),
            "winrate_tag": validity.get("winrate_tag"),
            "live_rank": tactical_rank.get("live_rank"),
            "eod_probability": tactical_rank.get("eod_probability"),
            "delta": tactical_rank.get("delta"),
            "eod_entry_rank": tactical_rank.get("eod_entry_rank"),
            "eod_entry_rank_total": tactical_rank.get("eod_entry_rank_total"),
            "components": tactical_rank.get("components"),
            "decision": decision.get("decision"),
            "decision_note": decision.get("note"),
            "formula_version": TACTICAL_FORMULA_VERSION,
        })
        log = log[-TACTICAL_SHADOW_LOG_MAX_ENTRIES:]
        # BUGFIX (defense-in-depth setelah insiden nyata di
        # daytrade_picks_history.json — numpy.bool_ dari perhitungan
        # pandas bocor ke data yang di-json.dump, TIDAK serializable,
        # dan open(path,"w") langsung truncate SEBELUM dump gagal, jadi
        # file lama ikut rusak). Tulis ke temp file dulu, baru
        # os.replace() atomic -- kalau serialisasi gagal, log lama utuh.
        tmp_path = TACTICAL_SHADOW_LOG_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(log, f, indent=2, default=core._json_default_numpy_safe)
        os.replace(tmp_path, TACTICAL_SHADOW_LOG_FILE)
    except Exception as e:
        print(f"⚠️ Gagal simpan tactical shadow snapshot buat {ticker}: {e}")


OFFRADAR_MAX_NAMES = 3  # sama pola dengan EXPLOSIVE_MAX_NAMES -- cap keras, terlepas berapa banyak yang lolos filter di bawah


def load_todays_notable_offradar_checks(exclude_tickers: set, live_rank_threshold: float = 60.0, max_names: int = OFFRADAR_MAX_NAMES) -> list:
    """
    MBSS v2 (user request — "/consensus live" section 2, "nampung temuan di
    luar tools"): scan tactical_shadow_log.json (log yang SUDAH ditulis
    tiap /check dari Phase 1 shadow-mode) buat ticker yang di-/check MANUAL
    hari ini TAPI TIDAK ada di Consensus Prime/Explosive Lane malam ini —
    genuinely "di luar radar" tool otomatis.

    Filter SENGAJA KETAT (user confirmed, hindari jadi daftar panjang tidak
    fokus): cuma EXTENDED_CHASE dengan winrate_tag REAL (dari lane SDT yang
    punya bukti /winrate, lihat classify_extended_beyond_plan), ATAU
    live_rank >= live_rank_threshold (placeholder 60, belum ada forward
    data — sama posisi "conservative until proven" dengan konstanta AB-RC1
    lain, sesuaikan setelah cukup observasi). Ceiling live_rank buat ticker
    yang TIDAK di-scoring /eodscan sama sekali (base default 50) cuma 73
    (base 50 + maks +23 stacking bonus live) — jadi 60 sengaja diturunkan
    dari usulan awal 70 (user request) supaya masih ada margin realistis
    buat kombinasi live yang KUAT tapi tidak sempurna (mis. cuma 3 dari 4
    bonus live kena, atau kena penalti RR tipis -8), bukan cuma yang
    stacking sempurna.

    Selain filter di atas, DIBATASI keras ke `max_names` (default 3, sama
    pola dengan EXPLOSIVE_MAX_NAMES) — kalau lebih dari itu yang lolos
    filter dalam satu hari, cuma live_rank TERTINGGI yang tampil. Ini
    jaring pengaman kedua di luar threshold, buat hari-hari volatil dimana
    banyak /check manual sekaligus lolos filter ketat di atas.

    Ambil entri TERAKHIR per ticker hari ini saja (satu ticker bisa
    ke-/check berkali-kali, state bisa berubah — kita mau yang terbaru).
    """
    if not os.path.exists(TACTICAL_SHADOW_LOG_FILE):
        return []
    try:
        with open(TACTICAL_SHADOW_LOG_FILE, encoding="utf-8") as f:
            log = json.load(f)
    except Exception:
        return []

    today = datetime.datetime.now(core.WIB).date()
    latest_by_ticker = {}
    for entry in log:
        ticker = entry.get("ticker")
        if not ticker or ticker in exclude_tickers:
            continue
        try:
            ts = datetime.datetime.fromisoformat(entry["timestamp"])
        except Exception:
            continue
        if ts.astimezone(core.WIB).date() != today:
            continue
        latest_by_ticker[ticker] = entry  # log sudah urut kronologis (append-only) -- yang terakhir menang

    notable = []
    for entry in latest_by_ticker.values():
        is_chase_confirmed = entry.get("validity_state") == "EXTENDED_CHASE" and entry.get("winrate_tag")
        live_rank = entry.get("live_rank")
        is_high_rank = live_rank is not None and live_rank >= live_rank_threshold
        if is_chase_confirmed or is_high_rank:
            notable.append(entry)

    notable.sort(key=lambda e: e.get("live_rank") or 0, reverse=True)
    return notable[:max_names]
