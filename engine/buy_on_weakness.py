"""
engine/buy_on_weakness.py — MBSS v2 (2026-09-16, new swing-trade lane)

"Buy the quiet dip inside a confirmed long-term uptrend." Full research
trail (272-combo sweeps, causal-volume/relative-strength/ATR/CMF/OBV/MACD
booster search, chronological train/test split, IHSG-crash root-cause
diagnosis, entry-timing/exit-mechanism rejections) lives in memory
`project_buy_on_weak_pullback_sweep_2026_09_16.md` and the 29 research
scripts `research/buy_on_weak_*_2026_09_16.py` (start with
`buy_on_weak_final_full_combo_2026_09_16.py` for the reference backtest).
Do not re-derive the thresholds below without re-reading that trail first.

CRITICAL, carried over from research: a naive "TP = price touches its own
moving Bollinger upper band" is NOT the same as "profitable" — a stock in
a real breakdown drags its own band down with it, so a small bounce inside
a crash can "touch" an already-collapsed band while deeply underwater.
Every TP2 check in this file requires `high >= band AND band >= entry`.

Entry (all features computed causally, no lookahead, from bars up to and
including the trigger day):
  - close > SMA100
  - Bollinger %B <= 0.35 (Tier 3 ceiling; <=0.25 for Tier 1/2)
  - decline_formation_vr < 0.6 (mean vol/SMA20-vol from the most recent
    20d swing high through the trigger day — a "quiet" pullback)
  - rally_pct_pre40 >= 5% (price rose >=5% from the pre-peak 40d low into
    the swing high — some real prior strength, not just noise)
  - pullback_depth_pct in [8, 22)% (swing-high-to-trigger-day drop)
  - foreign flow NOT extreme-net-sell (10d FF net / 10d volume > -10%)
  - RS20d <= 10% (stock's own 20d return minus IHSG's 20d return — avoid
    stocks that already ran far ahead of the index)
  - ATR%-20d <= 6.0% (historical volatility ceiling; real cliff found
    exactly here in the research sweep)
  - CMF-20 <= 0.20 (excludes the toxic tail, not the whole positive zone)
  - NOT bearish OBV divergence (price up 20d while OBV down 20d)
  - MACD line (SMA12-SMA26, % of price) <= 1.0%

Tier (REVERSED vs the research file's numbering, per user request to match
the house BSJP fire-emoji convention where more fire = better):
  Tier 1 (best):     %B<=0.25 AND CMF-20 in [-0.10, 0.05)
  Tier 2:            %B<=0.25
  Tier 3 (broadest): %B<=0.35

Exit: TP2 = band-touch-with-validity-fix above. TP1 = entry x (1 + tier's
median backtest return), informational partial-profit marker only, does
NOT end an alert's lifecycle. SL = entry x (1 - 18%).

ALERT_MAX_AGE_DAYS (5) vs RESOLUTION_MAX_AGE_DAYS (30) — FIXED 2026-10-10,
these were wrongly conflated before. User clarified ALERT_MAX_AGE_DAYS was
only ever meant to gate "is this still a fresh breakout-conviction entry,"
not "stop tracking SL/TP for an already-open position." The original code
used the SAME 5-day counter for both, which forced every pick to EXPIRE
(mark-to-market, no further tracking) long before its own validated median
time-to-TP (7-10 days per tier, see TIER_SWING_RANGE below) — this alone
explained most of the gap between the backtested ~83-89% win-rate and the
~55-62% that was showing live/in `/rank`'s LANE_STATS. Confirmed by
re-running the exact gate+exit logic with the window extended: win-rate
recovers to 83.1% (baseline) / 87.6% (+FF booster) at a 30-day resolution
window (near-plateau; extending further to 90d only adds ~3pp more). Now:
ALERT_MAX_AGE_DAYS controls ONLY the "NEW"/"AGING" display label (is this
still worth a fresh entry), RESOLUTION_MAX_AGE_DAYS controls how long
`_resolve_active_pick` keeps checking SL/TP before giving up (EXPIRED).
See memory `project_wyckoff_methodology_exploration_2026_10_09.md` and the
same-day follow-up thread for the full validation trail.

PRICE-DRIFT ZONE + CONDITIONAL NOTES (2026-10-10, see same memory trail).
Once a pick has survived a few days, how far price has drifted from the
trigger-day close (NOT calendar delay alone) predicts forward outcome:
NETRAL (-1% to +1% drift) is safest; chasing SUDAH_NAIK_KUAT (>=+3% drift)
is the single worst combination found (lower win AND lower return AND
higher SL-rate than every other zone, worst in Tier 3). A small number of
(tier, obs_day, zone) cells have enough independent sample (n>=150) to
state a PRECISE win-rate for that exact condition — see
SOLID_CONDITIONAL_CELLS. Everywhere else, only a GENERIC rule-of-thumb
applies (quiet-zone survivors tend to resolve faster than the tier's own
median). CRITICAL: these notes are DISPLAY-ONLY — per user's explicit
fairness concern 2026-10-10, `/rank`'s actual SCORE must never mix
precision levels across tiers (that would bias ranking toward whichever
tier happens to have denser research data), so this information never
feeds into `engine/capital_rank.py`'s LANE_STATS lookup.

CAVEAT carried over from research (do not drop when reporting results):
a chronological train/test split found real regime degradation tied to the
Jan-Jun 2026 IHSG crash — this combo has NO market-wide regime gate yet,
it is pure stock-level technicals. Treat backtest hit-rates as upper
bounds in a favorable regime, not an unconditional forward guarantee.

RS-DISCRIMINATOR addition (2026-10-05, memory project_oversold_bounce_v2_
locked_design_2026_10_05.md "BOW positioning vs OSB-v2" section): BOW's own
mean return flips negative once IHSG dd_100<=-10% (checked via a live
15-day sample, not the full 2-year backtest). The cleanest discriminator
found was `rs20_vs_ihsg` (same RS20d metric already used as the RS20_MAX
exclusion below) — filtering to the top two RS quartiles flips the
degraded-regime mean positive (+0.9% vs -5.0% for the discarded half).
Only ACTIVE when dd_100<=-10% (outside that regime BOW runs unfiltered, as
before) — this is a derate/filter, not a hide: even the worst quartile
still wins ~20-24% of the time. CAVEAT: this threshold comes from one
continuous 15-day live sample (serially correlated, not independent
periods like the main backtest numbers above) — treat as directional, not
with the same confidence as the rest of this file's OOS-split findings.
"""
from __future__ import annotations

import datetime
import json
import os
import sqlite3

import numpy as np
import pandas as pd

from engine import legacy_core as core
import engine.market as market_engine

# ---------------------------------------------------------------------------
# Constants (all validated in research/buy_on_weak_final_full_combo_2026_09_16.py)
# ---------------------------------------------------------------------------
FF_DB_PATH = os.path.join(core.PROJECT_ROOT, "research", "foreign_flow_2y.sqlite")
PICKS_FILE = os.path.join(core.PROJECT_ROOT, "buy_on_weakness_picks.json")

MIN_HISTORY = 220
SWING_LOOKBACK = 20
PRE_RALLY_LOOKBACK = 40

RALLY_MIN = 5.0
DEPTH_LO, DEPTH_HI = 8.0, 22.0
PCTB_CEIL_T3 = 0.35
PCTB_CEIL_T12 = 0.25
RS20_MAX = 10.0
ATR20_MAX = 6.0
CMF_MAX = 0.20
CMF_TOP_LO, CMF_TOP_HI = -0.10, 0.05
MACD_PCT_MAX = 1.0
SL_PCT = 18.0
FF_EXTREME_SELL = -10.0

TIER_MEDIAN_RET = {1: 5.85, 2: 6.19, 3: 5.28}
TIER_SWING_DAYS = {1: 10, 2: 8, 3: 7}  # kept for backward compat; display now uses TIER_SWING_RANGE
ALERT_MAX_AGE_DAYS = 5  # entry-freshness label ONLY -- see docstring 2026-10-10 fix

# SL/TP resolution tracking window -- was wrongly hard-capped at
# ALERT_MAX_AGE_DAYS (5) before 2026-10-10. 30d was the near-plateau of
# the win-rate-vs-window curve (83.1%/87.6%), but user chose to shorten
# this to Tier 1's own P75 days-to-TP (16d, see TIER_SWING_RANGE below) --
# a deliberate, smaller trade-off (74.9%/80.0% at 16d, see LANE_STATS
# comment in engine/capital_rank.py for the exact re-validated numbers)
# in exchange for not tracking positions indefinitely past a reasonable
# swing horizon. If this gets tuned again, LANE_STATS must be
# re-validated AT THE SAME WINDOW -- the two are not independent.
RESOLUTION_MAX_AGE_DAYS = 16

RS_DISCRIMINATOR_DD100_GATE = -10.0  # only active once IHSG dd_100 is this deep or deeper
RS_DISCRIMINATOR_FLOOR = 3.0         # rs_20d must be >= this (top ~2 quartiles) in that regime

# Days-to-TP distribution (P25/median/P75), validated 2026-10-10 on n=126
# (Tier1) / 446 (Tier2) / 350 (Tier3) resolved TP trades at the 40d window
# -- replaces the single-point TIER_SWING_DAYS estimate for display.
TIER_SWING_RANGE = {1: (5, 10.5, 16), 2: (4, 8, 13), 3: (4, 7, 11)}

# Price-drift-since-trigger zone classification, validated 2026-10-10
# (observation days 3/4/5, pooled and per-tier). drift_pct = (current_close
# - entry_ref_price) / entry_ref_price * 100.
DRIFT_DISKON_MAX = -3.0
DRIFT_DISKON_RINGAN_MAX = -1.0
DRIFT_NAIK_MIN = 1.0
DRIFT_NAIK_KUAT_MIN = 3.0

# Cells with n>=150 independent trigger episodes in the (tier, obs_day,
# zone) grid. Re-validated 2026-10-10 AT RESOLUTION_MAX_AGE_DAYS=16 (the
# original research/bow_dynamic_conditional_stats_2026_10_10.py numbers
# were computed at a 30d window -- shortening the window shrinks the
# budget left to resolve, so these are NOT the same numbers; re-validate
# again if RESOLUTION_MAX_AGE_DAYS changes). obs_day = age_days - 1.
# DISPLAY-ONLY, see docstring -- never read by engine/capital_rank.py.
# NOTE the (2,10,NAIK_KUAT) cell flipped to a WARNING at this shorter
# window (win 51.0%, mean_ret -0.18%, n=194) -- chasing a pick that's
# already up a lot AND has survived 10 days with only ~5 days of budget
# left is now a coin-flip-or-worse, not a mild positive like it was at
# the 30d window. Kept in the table deliberately (not dropped) so the
# note still fires -- it's now informative as a caution, not a highlight.
SOLID_CONDITIONAL_CELLS = {
    (2, 2, "NETRAL"): {"win": 71.0, "wlb": 64.1},
    (2, 3, "NETRAL"): {"win": 67.3, "wlb": 59.5},
    (2, 4, "NAIK_KUAT"): {"win": 68.2, "wlb": 60.4},
    (2, 5, "NAIK_KUAT"): {"win": 66.4, "wlb": 58.6},
    (2, 7, "NAIK_KUAT"): {"win": 60.9, "wlb": 53.6},
    (2, 10, "NAIK_KUAT"): {"win": 51.0, "wlb": 44.0},
}
# Generic rule-of-thumb window (obs_day range) + zones it applies to --
# from the Part-2 finding (quiet survivors at day 5 resolve in median 4d
# vs day-1 entry's median 7d) -- used only where SOLID_CONDITIONAL_CELLS
# has no exact match for that (tier, obs_day, zone).
GENERIC_NOTE_OBS_DAY_RANGE = (3, 5)
GENERIC_NOTE_ZONES = {"NETRAL", "NAIK"}


def classify_drift_zone(drift_pct: float) -> tuple[str, str]:
    """Returns (zone_key, short_display_label). zone_key matches the keys
    used in SOLID_CONDITIONAL_CELLS/GENERIC_NOTE_ZONES."""
    if drift_pct <= DRIFT_DISKON_MAX:
        return "DISKON", "📉 Diskon"
    if drift_pct <= DRIFT_DISKON_RINGAN_MAX:
        return "DISKON_RINGAN", "🟡 Diskon ringan"
    if drift_pct < DRIFT_NAIK_MIN:
        return "NETRAL", "😌 Netral"
    if drift_pct < DRIFT_NAIK_KUAT_MIN:
        return "NAIK", "📈 Sudah naik"
    return "NAIK_KUAT", "🚀 Naik jauh"


def conditional_drift_note(tier: int, age_days: int, zone_key: str) -> str | None:
    """Short display-only annotation for the pick's current drift
    condition. Returns None when nothing applicable (most Tier1/3
    cells, or obs_day outside the validated ranges) -- callers should
    just omit the line in that case, not show a placeholder."""
    obs_day = age_days - 1
    cell = SOLID_CONDITIONAL_CELLS.get((tier, obs_day, zone_key))
    if cell is not None:
        zone_short = {"NETRAL": "Netral", "NAIK_KUAT": "Naik jauh"}.get(zone_key, zone_key)
        icon = "📊" if cell["win"] >= 60.0 else "⚠️"  # below 60% -> read as caution, not a neutral stat
        return f"{icon} {zone_short} H{obs_day}: win≈{cell['win']:.0f}% (data spesifik titik ini)"
    lo, hi = GENERIC_NOTE_OBS_DAY_RANGE
    if lo <= obs_day <= hi and zone_key in GENERIC_NOTE_ZONES:
        return f"⏩ H{obs_day} tenang: histori TP cenderung lebih cepat dari estimasi awal."
    return None


# ---------------------------------------------------------------------------
# Feature computation — single ticker, TODAY's row only (production doesn't
# need the whole-history backtest loop research used).
# ---------------------------------------------------------------------------
def _compute_latest_features(df: pd.DataFrame) -> dict | None:
    """df: output of core.get_ohlcv_daily_from_db (index=date, columns
    Open/High/Low/Close/Volume, ascending). Returns None if not enough
    history for a reliable read (matches MIN_HISTORY used in research)."""
    if df is None or len(df) < MIN_HISTORY:
        return None

    close = df["Close"]; high = df["High"]; low = df["Low"]; volume = df["Volume"]

    sma100 = close.rolling(100).mean()
    sma20 = close.rolling(20).mean(); std20 = close.rolling(20).std()
    bb_upper = sma20 + 2 * std20
    bb_lower = sma20 - 2 * std20
    bw = (bb_upper - bb_lower).replace(0, np.nan)
    pctb = (close - bb_lower) / bw

    vol_sma20 = volume.rolling(20).mean()
    vol_ratio = volume / vol_sma20

    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr_pct_20d = (tr / close * 100).rolling(20).mean()

    hl_range = (high - low).replace(0, np.nan)
    mfm = ((close - low) - (high - close)) / hl_range
    mfv = mfm * volume
    cmf_20 = mfv.rolling(20).sum() / volume.rolling(20).sum()

    obv_dir = np.sign(close.diff()).fillna(0)
    obv = (obv_dir * volume).cumsum()
    obv_slope_20d_pct = obv.pct_change(20) * 100
    price_ret_20d = close.pct_change(20) * 100
    obv_bearish_div = (price_ret_20d > 0) & (obv_slope_20d_pct < 0)

    sma12 = close.rolling(12).mean(); sma26 = close.rolling(26).mean()
    macd_line = sma12 - sma26
    macd_line_pct = macd_line / close * 100

    if len(high) < SWING_LOOKBACK + 1:
        return None
    window = high.iloc[-(SWING_LOOKBACK + 1):-1]
    if window.isna().all():
        return None
    peak_date = window.idxmax()
    peak_price = float(window.max())
    pullback_depth_pct = (peak_price - float(close.iloc[-1])) / peak_price * 100

    peak_loc = df.index.get_loc(peak_date)
    pre_start = max(0, peak_loc - PRE_RALLY_LOOKBACK)
    pre_close_window = close.iloc[pre_start:peak_loc]
    rally_pct_pre40 = np.nan
    if len(pre_close_window) > 0:
        base_low = pre_close_window.min()
        if base_low and base_low > 0:
            rally_pct_pre40 = (peak_price - base_low) / base_low * 100

    decline_formation_vr = float(vol_ratio.loc[peak_date:].mean())

    return {
        "close": float(close.iloc[-1]),
        "sma100": float(sma100.iloc[-1]) if not pd.isna(sma100.iloc[-1]) else None,
        "pctb": float(pctb.iloc[-1]) if not pd.isna(pctb.iloc[-1]) else None,
        "bb_upper": float(bb_upper.iloc[-1]) if not pd.isna(bb_upper.iloc[-1]) else None,
        "atr_pct_20d": float(atr_pct_20d.iloc[-1]) if not pd.isna(atr_pct_20d.iloc[-1]) else None,
        "cmf_20": float(cmf_20.iloc[-1]) if not pd.isna(cmf_20.iloc[-1]) else None,
        "obv_bearish_div": bool(obv_bearish_div.iloc[-1]),
        "macd_line_pct": float(macd_line_pct.iloc[-1]) if not pd.isna(macd_line_pct.iloc[-1]) else None,
        "peak_price": peak_price,
        "pullback_depth_pct": pullback_depth_pct,
        "rally_pct_pre40": rally_pct_pre40,
        "decline_formation_vr": decline_formation_vr,
        "stock_ret_20d": float(price_ret_20d.iloc[-1]) if not pd.isna(price_ret_20d.iloc[-1]) else None,
        "latest_date": df.index[-1].strftime("%Y-%m-%d"),
        "vol_ratio_10d_sum_volume": float(volume.tail(10).sum()),
    }


def _load_ff_lookup() -> pd.DataFrame:
    """Whole-table load of the 2-year foreign-flow archive (research/
    foreign_flow_2y.sqlite — kept at this path deliberately, see
    project memory: production input despite the research/ prefix,
    same precedent as research/broksum_daily_history.sqlite). Pushed to
    the VPS from a residential IP (see push_foreign_flow_history.py) since
    IDX's endpoint 403s the VPS's datacenter IP."""
    if not os.path.exists(FF_DB_PATH):
        return pd.DataFrame(columns=["ticker", "date", "foreign_net"])
    conn = sqlite3.connect(FF_DB_PATH)
    try:
        ff = pd.read_sql("SELECT ticker, date, foreign_net FROM foreign_flow_daily", conn)
    finally:
        conn.close()
    ff["date"] = pd.to_datetime(ff["date"])
    return ff


def _ff_pctvol_10d(ticker: str, df: pd.DataFrame, ff_lookup: pd.DataFrame) -> float | None:
    last10_dates = df.index[-10:]
    ffg = ff_lookup[(ff_lookup.ticker == ticker) & (ff_lookup.date.isin(last10_dates))]
    if ffg.empty:
        return None
    net_sum = ffg["foreign_net"].sum()
    vol_sum = df["Volume"].tail(10).sum()
    if not vol_sum:
        return None
    return float(net_sum / vol_sum * 100)


def _ff_net_ratio_z60(ticker: str, df: pd.DataFrame, ff_lookup: pd.DataFrame) -> float | None:
    """z-score of today's foreign net_ratio (foreign_net/volume) vs this
    ticker's own trailing 60-day baseline (EXCLUDING today) -- booster
    signal validated 2026-10-07 (memory project_bow_osb_absolute_gate_
    confirm_2026_10_07.md, within THIS lane's own gate-survivor
    population): z60>=1.0 lifts win 56.3%->62.1% (wlb 53.4->54.0) AND
    shrinks the worst-case tail loss (-18.0%->-13.3%). A LONGER baseline
    window than the generic net_ratio_z20 matters specifically for BOW --
    it's a slower-moving "quiet pullback in an uptrend" setup, so a 60d
    FF baseline is more apples-to-apples than a jumpy 10-20d one (see
    sweep memory project_ff_booster_light_sweep_2026_10_07.md for why
    shorter windows were weaker here). Clipped to [-10,10] -- an illiquid
    ticker's trailing std can collapse near zero and blow up an
    unclipped z-score otherwise. Returns None when there isn't enough FF
    history (needs 60 PRIOR days + today = 61) -- missing = neutral,
    NEVER a gate, this is a re-rank tag only (house convention)."""
    if len(df) < 61:
        return None
    last61_dates = df.index[-61:]
    ffg = ff_lookup[(ff_lookup.ticker == ticker) & (ff_lookup.date.isin(last61_dates))]
    if len(ffg) < 61:
        return None
    vol_by_date = df["Volume"].reindex(last61_dates)
    net_by_date = ffg.drop_duplicates("date").set_index("date")["foreign_net"].reindex(last61_dates)
    if vol_by_date.isna().any() or net_by_date.isna().any() or (vol_by_date <= 0).any():
        return None
    net_ratio = net_by_date / vol_by_date
    baseline, today = net_ratio.iloc[:-1], net_ratio.iloc[-1]
    std = baseline.std()
    if not std or pd.isna(std):
        return None
    return float(np.clip((today - baseline.mean()) / std, -10, 10))


def _assign_tier(pctb: float, cmf20: float) -> int:
    if pctb <= PCTB_CEIL_T12 and CMF_TOP_LO < cmf20 <= CMF_TOP_HI:
        return 1
    if pctb <= PCTB_CEIL_T12:
        return 2
    return 3


def evaluate_ticker(ticker: str, ff_lookup: pd.DataFrame, rs20_ihsg: float | None,
                     ihsg_dd100: float | None = None) -> dict | None:
    """Returns a candidate dict if `ticker` qualifies TODAY, else None."""
    df = core.get_ohlcv_daily_from_db(ticker, limit=250)
    feats = _compute_latest_features(df)
    if feats is None:
        return None
    if feats["sma100"] is None or feats["close"] <= feats["sma100"]:
        return None
    if feats["pctb"] is None or feats["pctb"] > PCTB_CEIL_T3:
        return None
    if feats["decline_formation_vr"] is None or feats["decline_formation_vr"] >= 0.6:
        return None
    if feats["rally_pct_pre40"] is None or feats["rally_pct_pre40"] < RALLY_MIN:
        return None
    if not (DEPTH_LO <= feats["pullback_depth_pct"] < DEPTH_HI):
        return None
    if feats["atr_pct_20d"] is None or feats["atr_pct_20d"] > ATR20_MAX:
        return None
    if feats["cmf_20"] is None or feats["cmf_20"] > CMF_MAX:
        return None
    if feats["obv_bearish_div"]:
        return None
    if feats["macd_line_pct"] is None or feats["macd_line_pct"] > MACD_PCT_MAX:
        return None

    ff_pctvol10 = _ff_pctvol_10d(ticker, df, ff_lookup)
    if ff_pctvol10 is not None and ff_pctvol10 <= FF_EXTREME_SELL:
        return None
    # missing FF = neutral, never penalize (house convention) -> pass through

    if rs20_ihsg is not None and feats["stock_ret_20d"] is not None:
        rs_20d = feats["stock_ret_20d"] - rs20_ihsg
        if rs_20d > RS20_MAX:
            return None
        if (ihsg_dd100 is not None and ihsg_dd100 <= RS_DISCRIMINATOR_DD100_GATE
                and rs_20d < RS_DISCRIMINATOR_FLOOR):
            return None

    tier = _assign_tier(feats["pctb"], feats["cmf_20"])
    # BOOSTER (2026-10-07, validated, see _ff_net_ratio_z60 docstring) --
    # re-rank tag only, never excludes/gates.
    ff_net_ratio_z60 = _ff_net_ratio_z60(ticker, df, ff_lookup)
    ff_priority = bool(ff_net_ratio_z60 is not None and ff_net_ratio_z60 >= 1.0)
    entry_ref = feats["close"]
    entry_high = round(feats["peak_price"] * (1 - DEPTH_LO / 100))
    entry_low = round(feats["peak_price"] * (1 - DEPTH_HI / 100))
    sl_price = round(entry_ref * (1 - SL_PCT / 100))
    tp1_pct = TIER_MEDIAN_RET[tier]
    tp1_price = round(entry_ref * (1 + tp1_pct / 100))
    tp2_price = round(feats["bb_upper"]) if feats["bb_upper"] else None

    return {
        "ticker": ticker,
        "trigger_date": feats["latest_date"],
        "tier": tier,
        "status": "ALIVE",
        "tp1_touched": False,
        "age_days": 1,
        "entry_low": entry_low,
        "entry_high": entry_high,
        "entry_ref_price": entry_ref,
        "sl_price": sl_price,
        "tp1_price": tp1_price,
        "tp1_pct": tp1_pct,
        "tp2_price_latest": tp2_price,
        "swing_length_days_typical": TIER_SWING_DAYS[tier],
        "swing_horizon_p25": TIER_SWING_RANGE[tier][0],
        "swing_horizon_median": TIER_SWING_RANGE[tier][1],
        "swing_horizon_p75": TIER_SWING_RANGE[tier][2],
        "price_drift_pct": 0.0,
        "drift_zone": "NETRAL",
        "drift_label": "😌 Baru alert",
        "ff_net_ratio_z60": ff_net_ratio_z60,
        "ff_priority": ff_priority,
        "resolved_date": None,
        "resolved_ret_pct": None,
    }


def compute_buy_on_weakness_candidates(tickers: list[str]) -> list[dict]:
    """Nightly entry point: evaluate every ticker in `tickers`, return the
    list that qualifies today (does NOT touch the picks-history file —
    call update_and_save_picks() with the result to persist/merge)."""
    ff_lookup = _load_ff_lookup()
    rs20_ihsg = market_engine.get_ihsg_return_nd(20)
    ihsg_dd100 = market_engine.get_ihsg_drawdown_100d()
    candidates = []
    for t in tickers:
        try:
            c = evaluate_ticker(t, ff_lookup, rs20_ihsg, ihsg_dd100)
        except Exception as e:
            print(f"⚠️ Buy on Weakness: gagal evaluasi {t}: {e}")
            continue
        if c is not None:
            candidates.append(c)
    return candidates


# ---------------------------------------------------------------------------
# Pick-history persistence — REUSES the exact 3-layer safety pattern already
# proven for daytrade_picks_history.json (real production incident: a plain
# open(path,"w") + numpy.bool_ leaking into the dict crashed json.dump
# mid-write and permanently truncated hundreds of history entries). Reuse
# core's existing generic helpers rather than reimplementing them.
# ---------------------------------------------------------------------------
def load_buy_on_weakness_picks() -> list:
    if not os.path.exists(PICKS_FILE):
        return []
    try:
        with open(PICKS_FILE) as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ Gagal membaca buy_on_weakness_picks.json: {e}")
        return []


def save_buy_on_weakness_picks(picks: list):
    tmp_path = PICKS_FILE + ".tmp"
    try:
        core._backup_json_file_daily(PICKS_FILE)
        with open(tmp_path, "w") as f:
            json.dump(picks, f, indent=2, default=core._json_default_numpy_safe)
        os.replace(tmp_path, PICKS_FILE)
    except Exception as e:
        print(f"⚠️ Gagal menyimpan buy_on_weakness_picks.json: {e}")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


def _resolve_active_pick(pick: dict) -> dict:
    """Re-check one still-ALIVE pick against fresh OHLCV: SL first
    (conservative, same-day-ambiguity convention used throughout this
    project's research), then TP2 (band>=entry validity fix), else bump
    age and re-price TP2 to today's (moving) band. TP1 touch is tracked
    as an informational flag only -- it does not end the alert."""
    df = core.get_ohlcv_daily_from_db(pick["ticker"], limit=250)
    feats = _compute_latest_features(df)
    if feats is None:
        return pick  # can't evaluate today (e.g. delisted/no data) -- leave as-is

    entry_ref = pick["entry_ref_price"]
    sl_abs = entry_ref * (1 - SL_PCT / 100)
    today_low = float(df["Low"].iloc[-1])
    today_high = float(df["High"].iloc[-1])
    today_close = float(df["Close"].iloc[-1])
    today_band = feats["bb_upper"]
    today_date = feats["latest_date"]

    if today_date == pick.get("_last_checked_date"):
        return pick  # already resolved for today, avoid double-incrementing age

    if today_low <= sl_abs:
        pick["status"] = "SL_HIT"
        pick["resolved_date"] = today_date
        pick["resolved_ret_pct"] = round((sl_abs - entry_ref) / entry_ref * 100, 2)
    elif today_band and today_high >= today_band and today_band >= entry_ref:
        pick["status"] = "TP2_HIT"
        pick["resolved_date"] = today_date
        pick["resolved_ret_pct"] = round((today_band - entry_ref) / entry_ref * 100, 2)
        pick["tp2_price_latest"] = round(today_band)
    else:
        if today_close >= pick["tp1_price"]:
            pick["tp1_touched"] = True
        pick["age_days"] = pick.get("age_days", 1) + 1
        pick["tp2_price_latest"] = round(today_band) if today_band else pick.get("tp2_price_latest")
        drift_pct = (today_close - entry_ref) / entry_ref * 100
        zone_key, zone_label = classify_drift_zone(drift_pct)
        pick["price_drift_pct"] = round(drift_pct, 2)
        pick["drift_zone"] = zone_key
        pick["drift_label"] = zone_label
        # RESOLUTION_MAX_AGE_DAYS (not ALERT_MAX_AGE_DAYS) gates SL/TP
        # tracking -- ALERT_MAX_AGE_DAYS only controls the NEW/AGING
        # display label (see docstring 2026-10-10 fix).
        if pick["age_days"] > RESOLUTION_MAX_AGE_DAYS:
            pick["status"] = "EXPIRED"

    pick["_last_checked_date"] = today_date
    return pick


def update_and_save_picks(new_candidates: list[dict]) -> list:
    """Merge today's qualifying candidates into the persisted pick history:
    re-evaluate every still-active existing pick (SL/TP2/age), then append
    a fresh entry ONLY for tickers that don't already have an active pick
    (avoids re-alerting on the same setup every day it stays in the
    pullback zone -- one continuous alert per setup, not a daily re-fire).
    Call once per nightly cycle (or once per backfilled day)."""
    history = load_buy_on_weakness_picks()

    active_tickers = set()
    for pick in history:
        if pick.get("status") == "ALIVE":
            try:
                pick = _resolve_active_pick(pick)
            except Exception as e:
                print(f"⚠️ Buy on Weakness: gagal resolve pick {pick.get('ticker')}: {e}")
            active_tickers.add(pick["ticker"])

    for cand in new_candidates:
        if cand["ticker"] in active_tickers:
            continue
        history.append(cand)
        active_tickers.add(cand["ticker"])

    save_buy_on_weakness_picks(history)
    return history
