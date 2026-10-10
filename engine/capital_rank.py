"""
engine/capital_rank.py — MBSS v2 (2026-10-07), /rank command.

"Which of today's ACTIVE picks, across BOW/OSB-v2/VCP/MACD-confirm, is the
most capital-efficient for the next 1 day of holding?" Combines each
candidate's VALIDATED historical bucket (lane x FF-booster-tag) stats --
return-per-day-of-capital-held AND Wilson-lower-bound win-rate -- into one
weighted composite score, per user request 2026-10-07 (memory
project_lane_capital_efficiency_2026_10_07.md + the same-day follow-up
correcting the unweighted ret/day-only ranking, which the user correctly
flagged as not apples-to-apples against lanes with much higher win-rates).

BSJP (both lanes) is DELIBERATELY EXCLUDED from this ranking -- user's own
call, which this module agrees with and documents: Lane1 has no hard
price-based stop-loss (only a discretionary "cut if red at D2" guideline),
no single canonical exit rule (TP1 vs TP2 vs occurrence-ladder-dependent
rung -- see the TP1-vs-TP2 comparison in memory project_lane_capital_
efficiency_2026_10_07.md that triggered this exact conversation), and an
overnight-gap entry mechanic structurally different from every other
lane's continuous intraday-to-multi-day hold. Lane2 is independently
closed/no-go. Mixing either into a "1-day capital efficiency" ranking
with the other four would not be a fair comparison.

LANE_STATS below are population-level backtest stats PER (lane, ff_priority)
BUCKET -- not per-ticker. Every candidate in the same bucket gets the same
underlying stats; this is intentionally transparent (we have NOT validated
finer-grained per-ticker differentiation beyond which bucket a candidate
falls into). Sources, each a `research/*.py` script + matching memory file
under C:\\Users\\asus\\.claude\\projects\\...\\memory\\:
  - BOW: research/lane_capital_efficiency_2026_10_07.py + a follow-up
    bucket split (ff_priority True/False, net_ratio_z60>=1.0 threshold)
  - OSB-v2: same script, bucket split on pos_days_5>=3
  - VCP: single bucket, no FF filter was ever confirmed for this lane
  - MACD-confirm: single bucket -- its FF signal is an EXCLUSION gate
    already applied inside engine/macd_confirm_pillar.py's evaluate_ticker,
    so every live candidate has ALREADY passed it; there is no "excluded"
    bucket to show here, only the post-exclusion remainder's stats.
"""
from __future__ import annotations

LANE_STATS = {
    # BOW numbers CORRECTED 2026-10-10: the original 55.4%/62.1% figures
    # (and the ret_per_day values below them) were computed with BOW's
    # SL/TP tracking wrongly hard-capped at ALERT_MAX_AGE_DAYS=5 -- most
    # trades never got the chance to reach their own validated median
    # time-to-TP (7-10 days per tier) before being force-marked EXPIRED.
    # Re-validated AT engine.buy_on_weakness.RESOLUTION_MAX_AGE_DAYS (16
    # trading days -- user's deliberate choice, Tier1's own P75
    # days-to-TP, shortened from the 30d near-plateau value 83.1%/87.6%).
    # These two numbers are NOT independent -- if RESOLUTION_MAX_AGE_DAYS
    # changes again, re-run research/bow_dynamic_conditional_stats-style
    # simulation AT THE NEW WINDOW and update both together.
    # Uniform across all 3 tiers by design -- see 2026-10-10 fairness
    # discussion: a precision-weighted per-tier score would bias ranking
    # toward whichever tier happens to have denser research data, so
    # /rank's SCORE always uses this single flat number regardless of
    # tier; any tier/condition-specific precision is DISPLAY-ONLY (see
    # buy_on_weakness.conditional_drift_note), never fed back here.
    ("BOW", True): {"win": 80.0, "wlb": 72.8, "ret_per_day": 0.565, "label": "BOW +FF priority"},
    ("BOW", False): {"win": 74.9, "wlb": 72.3, "ret_per_day": 0.429, "label": "BOW baseline"},
    ("OSB", True): {"win": 81.9, "wlb": 76.4, "ret_per_day": 0.859, "label": "OSB-v2 +FF priority"},
    ("OSB", False): {"win": 68.9, "wlb": 62.3, "ret_per_day": 0.208, "label": "OSB-v2 baseline"},
    ("VCP", None): {"win": 73.2, "wlb": 70.9, "ret_per_day": 0.009, "label": "VCP (no FF filter)"},
    ("MACD", None): {"win": 49.2, "wlb": 48.1, "ret_per_day": 0.396, "label": "MACD-confirm (post-exclusion)"},
}
# NOTE on OSB-v2: deliberately LEFT UNCHANGED 2026-10-10. A feasibility
# check (research/osb_dynamic_conditional_stats_2026_10_10.py) found
# OSB-v2's raw episode count overstates independence -- it only fires
# during IHSG dd_100<=-10% regimes, and 2+ years of data contains just 10
# distinct drawdown clusters, with many same-cluster trades firing on the
# same handful of dates. Wilson-LB computed on raw n is overconfident for
# this lane; don't casually "fix" OSB-v2's numbers the way BOW's were
# without a cluster-aware (block-bootstrap-by-drawdown-episode) method
# first -- this is a structural limitation, not a sample-size one that
# more data alone would resolve.

DEFAULT_W_RETURN = 0.5
DEFAULT_W_WIN = 0.5


def _gather_candidates():
    """Reuses the EXACT same active-pick loading/filtering as
    commands.scan.swing_command, so /rank never shows a candidate /swing
    itself wouldn't -- no second standard. Returns a list of dicts with
    lane_key ("BOW"/"OSB"/"VCP"/"MACD"), ff_priority (bool|None), and the
    raw pick dict for display fields (ticker, entry/SL/TP, age, etc.)."""
    import engine.buy_on_weakness as bow_engine
    import engine.oversold_bounce_v2 as osb_engine
    import engine.vcp_pillar as vcp_engine
    import engine.macd_confirm_pillar as macd_engine

    out = []

    for p in bow_engine.load_buy_on_weakness_picks():
        if p.get("status") != "ALIVE":
            continue
        out.append({"lane_key": "BOW", "ff_priority": bool(p.get("ff_priority")), "pick": p})

    for p in osb_engine.load_oversold_bounce_v2_picks():
        if p.get("status") != "ALIVE":
            continue
        out.append({"lane_key": "OSB", "ff_priority": bool(p.get("ff_priority")), "pick": p})

    for p in vcp_engine.load_vcp_picks():
        if p.get("status") != "ALIVE":
            continue
        out.append({"lane_key": "VCP", "ff_priority": None, "pick": p})

    # Same filter as swing_command: FADING hidden (not an entry today), and
    # Quality(D2) VERY STRONG hidden (confirmed-bad forward bucket, see
    # engine/macd_confirm_pillar.py module docstring).
    for p in macd_engine.load_macd_confirm_picks():
        if p.get("status") != "ALIVE" or p.get("tag") == "FADING":
            continue
        if p.get("lane") == "Quality(D2)" and p.get("tag") == "VERY STRONG":
            continue
        out.append({"lane_key": "MACD", "ff_priority": None, "pick": p})

    return out


def compute_rank(w_return: float = DEFAULT_W_RETURN, w_win: float = DEFAULT_W_WIN) -> list[dict]:
    """Returns candidates sorted by composite score descending. Each entry:
    {lane_key, ff_priority, pick, stats, score}. `stats` is the LANE_STATS
    bucket dict. ret_per_day is min-max normalized to 0-100 ACROSS the
    distinct buckets actually present today (not a fixed global scale) --
    self-consistent for whatever subset of lanes/buckets happens to be
    active, robust if this table's absolute numbers change later."""
    candidates = _gather_candidates()
    if not candidates:
        return []

    total_w = w_return + w_win
    if total_w <= 0:
        w_return, w_win = DEFAULT_W_RETURN, DEFAULT_W_WIN
        total_w = 1.0
    w_return, w_win = w_return / total_w, w_win / total_w

    for c in candidates:
        c["stats"] = LANE_STATS[(c["lane_key"], c["ff_priority"])]

    ret_values = [c["stats"]["ret_per_day"] for c in candidates]
    r_min, r_max = min(ret_values), max(ret_values)
    r_span = r_max - r_min

    for c in candidates:
        norm_ret = 100.0 if r_span == 0 else (c["stats"]["ret_per_day"] - r_min) / r_span * 100.0
        c["norm_ret"] = norm_ret
        c["score"] = w_return * norm_ret + w_win * c["stats"]["wlb"]

    candidates.sort(key=lambda c: (-c["score"], c["pick"].get("age_days", 99), c["pick"].get("ticker", "")))
    return candidates


def format_rank_message(ranked: list[dict], w_return: float = DEFAULT_W_RETURN, w_win: float = DEFAULT_W_WIN, top_n: int = 20) -> str:
    total_w = w_return + w_win
    if total_w <= 0:
        w_return, w_win = DEFAULT_W_RETURN, DEFAULT_W_WIN
        total_w = 1.0
    w_return_pct = round(w_return / total_w * 100)
    w_win_pct = 100 - w_return_pct

    if not ranked:
        return (
            "📊 /RANK — tidak ada pick aktif saat ini di BOW/OSB-v2/VCP/MACD-confirm.\n\n"
            "⚠️ BSJP (Lane1 & Lane2) sengaja TIDAK disertakan -- mekanisme beda "
            "(tanpa SL keras, exit ladder-dependent, overnight-gap), lihat /bsjp sendiri."
        )

    lines = [
        f"📊 /RANK — Peringkat ekonomi modal per 1 hari tahan ({len(ranked)} pick aktif)",
        f"Bobot: {w_return_pct}% return/hari + {w_win_pct}% win-rate (Wilson LB 95%)",
        "⚠️ BSJP TIDAK disertakan (tanpa SL keras, exit ladder-dependent, mekanisme overnight-gap beda) -- cek /bsjp terpisah.\n",
    ]
    for i, c in enumerate(ranked[:top_n], 1):
        p = c["pick"]; s = c["stats"]
        ticker = p.get("ticker", "?")
        age = p.get("age_days", "?")
        entry = p.get("entry_ref_price") or p.get("entry_low")
        sl = p.get("sl_price")
        priority_tag = " ⭐FF" if c["ff_priority"] else ""
        block = (
            f"{i}. {ticker} [{s['label']}{priority_tag}] — Score {c['score']:.1f}\n"
            f"   ret/hari≈{s['ret_per_day']:+.2f}% (ann. simple {s['ret_per_day']*252:+.0f}%) | "
            f"win {s['win']:.1f}% (wlb {s['wlb']:.1f}%) | Day {age}"
        )
        if entry and sl:
            block += f"\n   Entry~{entry:,.0f} | SL {sl:,.0f}"
        # Swing horizon + price-drift -- only present on picks that carry
        # these fields (currently BOW only, added 2026-10-10). Display-only,
        # never affects `score` above -- see LANE_STATS fairness note.
        p25, p75, median_days = p.get("swing_horizon_p25"), p.get("swing_horizon_p75"), p.get("swing_horizon_median")
        if p25 is not None and p75 is not None:
            block += f"\n   Swing horizon: ~{p25:g}-{p75:g} hari (median {median_days:g})"
        drift = p.get("price_drift_pct")
        if drift is not None:
            block += f"\n   Pergerakan sejak entry: {drift:+.1f}% — {p.get('drift_label', '')}"
        lines.append(block)
    if len(ranked) > top_n:
        lines.append(f"… +{len(ranked) - top_n} pick lain tidak ditampilkan (gunakan /swing untuk daftar lengkap).")
    lines.append(
        "ℹ️ Score = gabungan return/hari (dinormalisasi 0-100 ANTAR bucket yang aktif hari ini) "
        "+ win-rate (Wilson LB). Semua pick dalam bucket yang sama (lane+tag FF) pakai angka backtest "
        "yang SAMA -- ini peringkat tingkat lane/bucket, bukan skor per-ticker individual."
    )
    return "\n\n".join(lines)
