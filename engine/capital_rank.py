"""
engine/capital_rank.py — MBSS v2 (2026-10-07, redesigned 2026-10-10/11).

"Which of today's ACTIVE picks, across BOW/OSB-v2/VCP/MACD-confirm, is the
most capital-efficient for the next 1 day of holding?" Combines each
candidate's VALIDATED stats -- return-per-day-of-capital-held, Wilson-
lower-bound win-rate, AND (new 2026-10-10) how much upside is left to the
TP target RIGHT NOW -- into one weighted composite score.

BSJP (both lanes) is DELIBERATELY EXCLUDED -- see memory
project_lane_capital_efficiency_2026_10_07.md: no hard SL, no single
canonical exit rule, overnight-gap entry mechanic structurally different
from the other four lanes' continuous hold.

2026-10-10/11 REDESIGN (memory project_bow_rank_scoring_redesign_2026_10_10.md
has the full validation trail -- read that before changing any number
here): the original flat per-(lane, ff_priority) LANE_STATS bucket was
found to hide two real, validated effects for BOW:
  1. Tier matters -- Tier1/2/3 have genuinely different win/ret profiles
     even at the SAME RESOLUTION_MAX_AGE_DAYS window. LANE_STATS[("BOW",
     tier, ff_priority)] now keys on tier too.
  2. Price-drift-since-entry matters, SAME RULE for every tier (not an
     asymmetric precision bonus -- a uniform adjustment everyone is
     subject to, which is why this is fair unlike the earlier rejected
     per-tier-only-where-convenient design): NAIK (1-5% drift) is a
     validated booster, NAIK_KUAT (>=5%) degrades hard for Tier2 by
     late obs_days and is UNIQUELY bad for Tier3 at nearly every obs_day
     -- Tier3+NAIK_KUAT is HIDDEN from this ranking entirely (user's
     explicit call), Tier1/2+NAIK_KUAT get a win-rate-table-driven
     penalty instead (see engine/buy_on_weakness.py's
     TIER2_NAIK_KUAT_WIN_BY_OBS_DAY / TIER1_NAIK_KUAT_FLAT_PENALTY_WIN /
     NAIK_ZONE_SCORE_BONUS / should_hide_from_rank).

MACD-confirm (2026-10-14 REDESIGN -- see engine/macd_confirm_pillar.py
module docstring): the old per-(age_day, lane, tag) D1-D5 tag system was
retired (proven non-predictive after a fresh-entry-anchored re-test).
MACD-confirm is now regime-gated (BOUNCE_OFF_LOW only) with a single
flat win-rate (`win_rate_of`, 62.2%) and a FIXED TP price set at entry
(`pick["tp_price"]`) -- TP-gain is computed directly from that field,
no more `tp_prices()`/`classify_fade_from_peak()` (both removed).

VCP is LEFT UNCHANGED for now (flat LANE_STATS bucket) -- unlike MACD it
has no existing tier/day-conditional structure to wire in, and unlike BOW
it hasn't been re-validated for its own drift-since-entry behavior yet.
Do not bolt BOW's exact thresholds onto VCP without first checking
whether VCP's dynamics even point the same direction (BOW's own
"chasing is bad" finding is the OPPOSITE of MACD-confirm's "already-run-
up is continuation, not exhaustion" finding -- lanes differ, always
re-validate before porting a rule).

TP-GAIN dimension (new 2026-10-10): `tp_gain_pct` = how far (%) from the
CURRENT price to the lane's own TP2-equivalent target, right now -- a
third, independent axis alongside ret/day and win-rate (a pick deep in
diskon with a wide-open band has more room left than one already
hugging its target). Candidates without this field computable (OSB/VCP
for now) get a neutral default so they aren't penalized for a
dimension this module can't yet measure for their lane.
"""
from __future__ import annotations

LANE_STATS = {
    # BOW split by TIER 2026-10-10/11 (was a single flat bucket before --
    # see module docstring). Re-validated AT RESOLUTION_MAX_AGE_DAYS=16.
    # Tier1's own +FF booster bucket is thin (n=20) -- kept but flagged;
    # Tier3's +FF booster (n=46) is borderline, also flagged. Neither is
    # as trustworthy as Tier2's (n=79).
    ("BOW", 1, True): {"win": 70.0, "wlb": 48.1, "ret_per_day": 0.408, "label": "BOW Tier1 +FF (n=20, thin)"},
    ("BOW", 1, False): {"win": 76.2, "wlb": 69.1, "ret_per_day": 0.460, "label": "BOW Tier1"},
    ("BOW", 2, True): {"win": 81.0, "wlb": 71.0, "ret_per_day": 0.717, "label": "BOW Tier2 +FF"},
    ("BOW", 2, False): {"win": 72.5, "wlb": 68.6, "ret_per_day": 0.457, "label": "BOW Tier2"},
    ("BOW", 3, True): {"win": 82.6, "wlb": 69.3, "ret_per_day": 0.366, "label": "BOW Tier3 +FF (n=46, border)"},
    ("BOW", 3, False): {"win": 77.7, "wlb": 73.5, "ret_per_day": 0.374, "label": "BOW Tier3"},
    ("OSB", True): {"win": 81.9, "wlb": 76.4, "ret_per_day": 0.859, "label": "OSB-v2 +FF priority"},
    ("OSB", False): {"win": 68.9, "wlb": 62.3, "ret_per_day": 0.208, "label": "OSB-v2 baseline"},
    ("VCP", None): {"win": 73.2, "wlb": 70.9, "ret_per_day": 0.009, "label": "VCP (no FF filter)"},
    # MACD's flat bucket is now only a FALLBACK for when win_rate_of()
    # returns None (pick missing) -- see _macd_stats_and_tp_gain().
    # 2026-10-14: updated to match the regime-gated fixed-exit redesign
    # (BOUNCE_OFF_LOW, SL-6%/TP8%/maxhold5, backtested win=62.2%).
    ("MACD", None): {"win": 62.2, "wlb": 56.2, "ret_per_day": 0.296, "label": "MACD-confirm (BOUNCE_OFF_LOW, fixed exit)"},
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

DEFAULT_W_RETURN = 0.40
DEFAULT_W_WIN = 0.45
DEFAULT_W_TPGAIN = 0.15  # user request 2026-10-11: TP gain is a less certain factor
                         # (target band itself moves, no hard trigger like win/SL) --
                         # de-emphasized vs win-rate/return, was 0.30
NEUTRAL_TPGAIN_SCORE = 50.0  # assigned when tp_gain_pct can't be computed for a lane yet


def _bow_stats_and_adjustment(pick: dict) -> tuple[dict, float, bool]:
    """Returns (stats, score_bonus, hide). `stats` always has
    win/wlb/ret_per_day/label (a copy, safe to mutate). `score_bonus` is
    an additive delta applied AFTER normalization (NAIK zone booster).
    `hide` True means drop this candidate entirely (Tier3+NAIK_KUAT)."""
    import engine.buy_on_weakness as bow_engine

    tier = pick.get("tier", 2)
    ff = bool(pick.get("ff_priority"))
    base = LANE_STATS.get(("BOW", tier, ff)) or LANE_STATS[("BOW", tier, False)]
    zone = pick.get("drift_zone", "NETRAL")

    if bow_engine.should_hide_from_rank(tier, zone):
        return dict(base), 0.0, True

    stats = dict(base)
    bonus = 0.0

    # Path-dependence override (2026-10-13) takes PRIORITY over the
    # plain zone-level overrides below -- it's based on a more specific
    # validated population (peak/trough drift since entry, not just
    # today's point-in-time zone). See engine/buy_on_weakness.py's
    # classify_path_condition docstring for the three conditions.
    path_cond = bow_engine.classify_path_condition(pick)
    if path_cond is not None:
        stats["win"], stats["wlb"] = path_cond["win"], path_cond["wlb"]
        stats["label"] = path_cond["label"]  # audit fix 2026-10-11: label must match the win/wlb actually shown
        return stats, bonus, False

    if zone == "NAIK_KUAT":
        obs_day = pick.get("age_days", 1) - 1
        if tier == 2:
            win = bow_engine.TIER2_NAIK_KUAT_WIN_BY_OBS_DAY.get(obs_day)
            if win is not None:
                # wlb approximated as win minus a typical gap at this n
                # (65-138) -- these cells were never precise enough to
                # have an exact Wilson-LB computed, see memory.
                stats["win"], stats["wlb"] = win, max(0.0, win - 8.0)
        elif tier == 1:
            stats["win"] = bow_engine.TIER1_NAIK_KUAT_FLAT_PENALTY_WIN
            stats["wlb"] = max(0.0, stats["win"] - 12.0)  # wider margin -- thinner data
    elif zone == "NAIK":
        bonus = bow_engine.NAIK_ZONE_SCORE_BONUS
    return stats, bonus, False


def _bow_tp_gain_pct(pick: dict) -> float | None:
    drift = pick.get("price_drift_pct")
    entry_ref = pick.get("entry_ref_price")
    tp2 = pick.get("tp2_price_latest")
    if drift is None or not entry_ref or not tp2:
        return None
    current_price = entry_ref * (1 + drift / 100)
    if current_price <= 0:
        return None
    return (tp2 - current_price) / current_price * 100


def _vcp_stats_and_adjustment(pick: dict) -> dict:
    """Day-indexed win override for VCP's two validated zones (2026-10-11,
    research/vcp_drift_since_entry_2026_10_11.py) -- NAIK (price continued
    up since entry, confirms the breakout) and DISKON (price fell,
    breakout failing, degrades the longer it persists) BOTH have usable
    per-obs_day tables, so no separate flat bonus is layered on top of
    the override (unlike BOW's NAIK, which only had a flat bonus because
    no day-indexed table existed for it) -- would double-count the effect.
    No hide needed -- see engine/vcp_pillar.py docstring, nothing here is
    thin/catastrophic enough to warrant dropping, unlike BOW's Tier3."""
    import engine.vcp_pillar as vcp_engine

    stats = dict(LANE_STATS[("VCP", None)])
    zone = pick.get("drift_zone", "NETRAL")
    obs_day = pick.get("age_days", 1) - 1
    if zone == "NAIK":
        win = vcp_engine.VCP_NAIK_TP1_RATE_BY_OBS_DAY.get(obs_day)
        if win is not None:
            stats["win"], stats["wlb"] = win, max(0.0, win - 8.0)
    elif zone == "DISKON":
        win = vcp_engine.VCP_DISKON_TP1_RATE_BY_OBS_DAY.get(obs_day)
        if win is not None:
            stats["win"], stats["wlb"] = win, max(0.0, win - 10.0)
    return stats


def _vcp_tp_gain_pct(pick: dict) -> float | None:
    """NAIK-zone picks target VCP_NAIK_TP_TARGET_PCT (5.5%, the validated
    economic optimum for that subpopulation -- see engine/vcp_pillar.py)
    instead of the flat TP1(+2.5%) everyone else uses as the reference
    target for this dimension."""
    import engine.vcp_pillar as vcp_engine

    drift = pick.get("price_drift_pct")
    entry_ref = pick.get("entry_ref_price")
    if drift is None or not entry_ref:
        return None
    current_price = entry_ref * (1 + drift / 100)
    if current_price <= 0:
        return None
    zone = pick.get("drift_zone", "NETRAL")
    if zone == "NAIK":
        target = entry_ref * (1 + vcp_engine.VCP_NAIK_TP_TARGET_PCT / 100)
    else:
        target = pick.get("tp1_price") or entry_ref * (1 + vcp_engine.TP1_PCT / 100)
    return (target - current_price) / current_price * 100


def _macd_stats_and_tp_gain(pick: dict) -> tuple[dict, float | None]:
    """Flat win-rate (2026-10-14 regime-gated fixed-exit redesign -- see
    engine/macd_confirm_pillar.py module docstring) + TP-gain computed
    from the pick's own FIXED tp_price set at entry."""
    import engine.macd_confirm_pillar as macd_engine

    fallback = LANE_STATS[("MACD", None)]
    win = macd_engine.win_rate_of(pick)
    stats = dict(fallback)
    if win is not None:
        stats["win"] = win
        stats["wlb"] = max(0.0, win - 6.0)
        stats["label"] = f"MACD-confirm D{pick.get('age_days', 1)}"

    tp_gain = None
    tp_price = pick.get("tp_price")
    current = pick.get("current_price")
    if tp_price and current:
        tp_gain = (tp_price - current) / current * 100
    return stats, tp_gain


def _gather_candidates():
    """Reuses the EXACT same active-pick loading/filtering as
    commands.scan.swing_command, so /rank never shows a candidate /swing
    itself wouldn't -- no second standard. Returns a list of dicts with
    lane_key, ff_priority, pick, stats, score_bonus, tp_gain_pct."""
    import engine.buy_on_weakness as bow_engine
    import engine.oversold_bounce_v2 as osb_engine
    import engine.vcp_pillar as vcp_engine
    import engine.macd_confirm_pillar as macd_engine

    out = []

    for p in bow_engine.load_buy_on_weakness_picks():
        if p.get("status") != "ALIVE":
            continue
        stats, bonus, hide = _bow_stats_and_adjustment(p)
        if hide:
            continue
        out.append({"lane_key": "BOW", "ff_priority": bool(p.get("ff_priority")), "pick": p,
                     "stats": stats, "score_bonus": bonus, "tp_gain_pct": _bow_tp_gain_pct(p)})

    for p in osb_engine.load_oversold_bounce_v2_picks():
        if p.get("status") != "ALIVE":
            continue
        stats = LANE_STATS[("OSB", bool(p.get("ff_priority")))]
        out.append({"lane_key": "OSB", "ff_priority": bool(p.get("ff_priority")), "pick": p,
                     "stats": dict(stats), "score_bonus": 0.0, "tp_gain_pct": None})

    for p in vcp_engine.load_vcp_picks():
        if p.get("status") != "ALIVE":
            continue
        out.append({"lane_key": "VCP", "ff_priority": None, "pick": p,
                     "stats": _vcp_stats_and_adjustment(p), "score_bonus": 0.0,
                     "tp_gain_pct": _vcp_tp_gain_pct(p)})

    # Same filter as swing_command: show-once rule (age_days<=1), see
    # engine/macd_confirm_pillar.py module docstring (2026-10-14 redesign).
    for p in macd_engine.load_macd_confirm_picks():
        if p.get("status") != "ALIVE" or p.get("age_days", 1) > 1:
            continue
        stats, tp_gain = _macd_stats_and_tp_gain(p)
        out.append({"lane_key": "MACD", "ff_priority": None, "pick": p,
                     "stats": stats, "score_bonus": 0.0, "tp_gain_pct": tp_gain})

    return out


def compute_rank(w_return: float = DEFAULT_W_RETURN, w_win: float = DEFAULT_W_WIN,
                  w_tpgain: float = DEFAULT_W_TPGAIN) -> list[dict]:
    """Returns candidates sorted by composite score descending. Each entry:
    {lane_key, ff_priority, pick, stats, score_bonus, tp_gain_pct, score}.
    ret_per_day and tp_gain_pct are both min-max normalized to 0-100
    ACROSS the distinct values actually present today -- self-consistent
    for whatever subset of lanes/buckets happens to be active."""
    candidates = _gather_candidates()
    if not candidates:
        return []

    total_w = w_return + w_win + w_tpgain
    if total_w <= 0:
        w_return, w_win, w_tpgain = DEFAULT_W_RETURN, DEFAULT_W_WIN, DEFAULT_W_TPGAIN
        total_w = DEFAULT_W_RETURN + DEFAULT_W_WIN + DEFAULT_W_TPGAIN
    w_return, w_win, w_tpgain = w_return / total_w, w_win / total_w, w_tpgain / total_w

    ret_values = [c["stats"]["ret_per_day"] for c in candidates]
    r_min, r_max = min(ret_values), max(ret_values)
    r_span = r_max - r_min

    tp_values = [c["tp_gain_pct"] for c in candidates if c["tp_gain_pct"] is not None]
    t_min, t_max = (min(tp_values), max(tp_values)) if tp_values else (0.0, 0.0)
    t_span = t_max - t_min

    for c in candidates:
        norm_ret = 100.0 if r_span == 0 else (c["stats"]["ret_per_day"] - r_min) / r_span * 100.0
        if c["tp_gain_pct"] is None:
            norm_tpgain = NEUTRAL_TPGAIN_SCORE
        else:
            norm_tpgain = 100.0 if t_span == 0 else (c["tp_gain_pct"] - t_min) / t_span * 100.0
        c["norm_ret"] = norm_ret
        c["norm_tpgain"] = norm_tpgain
        c["score"] = (w_return * norm_ret + w_win * c["stats"]["wlb"]
                      + w_tpgain * norm_tpgain + c["score_bonus"])

    candidates.sort(key=lambda c: (-c["score"], c["pick"].get("age_days", 99), c["pick"].get("ticker", "")))
    return candidates


def format_rank_message(ranked: list[dict], w_return: float = DEFAULT_W_RETURN, w_win: float = DEFAULT_W_WIN,
                         w_tpgain: float = DEFAULT_W_TPGAIN, top_n: int = 20, broker_flags: dict | None = None) -> str:
    total_w = w_return + w_win + w_tpgain
    if total_w <= 0:
        w_return, w_win, w_tpgain = DEFAULT_W_RETURN, DEFAULT_W_WIN, DEFAULT_W_TPGAIN
        total_w = DEFAULT_W_RETURN + DEFAULT_W_WIN + DEFAULT_W_TPGAIN
    w_return_pct = round(w_return / total_w * 100)
    w_win_pct = round(w_win / total_w * 100)
    w_tpgain_pct = 100 - w_return_pct - w_win_pct

    if not ranked:
        return (
            "📊 /RANK — tidak ada pick aktif saat ini di BOW/OSB-v2/VCP/MACD-confirm.\n\n"
            "⚠️ BSJP (Lane1 & Lane2) sengaja TIDAK disertakan -- mekanisme beda "
            "(tanpa SL keras, exit ladder-dependent, overnight-gap), lihat /bsjp sendiri."
        )

    lines = [
        f"📊 /RANK — Peringkat ekonomi modal per 1 hari tahan ({len(ranked)} pick aktif)",
        f"Bobot: {w_return_pct}% return/hari + {w_win_pct}% win-rate (Wilson LB 95%) + {w_tpgain_pct}% sisa jarak ke TP",
        "⚠️ BSJP TIDAK disertakan (tanpa SL keras, exit ladder-dependent, mekanisme overnight-gap beda) -- cek /bsjp terpisah.\n",
    ]
    for i, c in enumerate(ranked[:top_n], 1):
        p = c["pick"]; s = c["stats"]
        ticker = p.get("ticker", "?")
        age = p.get("age_days", "?")
        entry = p.get("entry_ref_price") or p.get("entry_low")
        sl = p.get("sl_price")
        priority_tag = " ⭐FF" if c["ff_priority"] else ""
        _bf = (broker_flags or {}).get(ticker)
        if _bf:
            import engine.broker_flags as _broker_flags
            priority_tag += _broker_flags.short_tag(_bf)
        block = (
            f"{i}. {ticker} [{s['label']}{priority_tag}] — Score {c['score']:.1f}\n"
            f"   ret/hari≈{s['ret_per_day']:+.2f}% (ann. simple {s['ret_per_day']*252:+.0f}%) | "
            f"win {s['win']:.1f}% (wlb {s['wlb']:.1f}%) | Day {age}"
        )
        if c["tp_gain_pct"] is not None:
            block += f" | sisa ke TP≈{c['tp_gain_pct']:+.1f}%"
        if entry and sl:
            block += f"\n   Entry~{entry:,.0f} | SL {sl:,.0f}"
        # Swing horizon + price-drift -- only present on picks that carry
        # these fields (currently BOW only, added 2026-10-10). Display-only,
        # never affects `score` above except via the zone bonus/penalty
        # already folded into `stats`/`score_bonus` upstream.
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
        "ℹ️ Score = return/hari + win-rate (Wilson LB) + sisa jarak ke TP (ketiganya dinormalisasi 0-100 "
        "ANTAR pick yang aktif hari ini), SUDAH memperhitungkan pergerakan sejak entry (lihat baris "
        "Pergerakan) -- arahnya KEBALIKAN per lane: BOW 'naik jauh' (≥5%) = chasing, dipenalti (Tier3 "
        "malah disembunyikan total); VCP 'naik' (1-5%) = breakout terkonfirmasi, dibooster, SL tetap sama."
    )
    return "\n\n".join(lines)
