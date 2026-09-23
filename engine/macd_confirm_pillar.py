"""
engine/macd_confirm_pillar.py — MBSS v2 (2026-09-22, 3rd swing-trade lane)

"Buy the grinding trend, confirmed by its own follow-through." Third swing
pillar alongside engine/buy_on_weakness.py and engine/vcp_pillar.py. Full
research trail (10d MACD-centerline consistency baseline, ATR%/liquidity-
band/std_ret_10d Stage-1 derivation, 3-lane split, D1-D5 magnitude-
confirmation tiering, SL/TP correction, dozens of rejected candidate
filters) lives in memory `project_macd_n10_stage1_locked_final_2026_09_22.md`
and `project_macd_confirm_pillar_shipped_2026_09_22.md` (the sibling files
they link; grind/deep-drawdown threads are a DIFFERENT, PARKED pillar —
do not confuse). Do not re-derive the thresholds below without re-reading
that trail first.

IMPORTANT framing, user-corrected 2026-09-22: the D1-D5 tag is an ENTRY-
TIMING / watchlist signal, NOT a live-position exit-management tool. The
candidate has NOT been bought yet during D1-D5 -- the trader watches the
tag develop and only enters (at whatever the market price is when they
act) if/once conviction looks good; if it shows FADING, they simply don't
enter, they don't "exit" anything. `entry_ref_price` is the D0/D1 anchor
price the whole tag ladder and TP/SL math is scaled from, not a claim
that capital is already deployed.

Entry gate (Stage-1, all features causal, from bars up to and including
the trigger day):
  - macd_line (SMA12-SMA26, Brights convention) > 0 for >=90% of the
    trailing 10 trading days
  - macd_slope_5d (macd_line - macd_line 5d ago) >= 0 (not diving)
  - no 3-consecutive-down-day streak anywhere in the trailing 10d window
  - atr14_pct (Wilder) <= 5.78 -- tightened from an initial 90th-percentile
    guess after a direct head-to-head check showed the tighter cap
    dominates on every axis (win/mean/SL) OOS, not a tradeoff
  - value_20d_avg (liquidity) in [19,586,155 ; 1,259,645,238] IDR -- a
    HUMP-shaped band, not a ceiling: both illiquid (<19.6M) and mega-cap
    (>1.26B) names underperform. Do not simplify to a single-sided filter.
  - std_ret_10d (10d daily-return stdev) <= 4.29% -- picked over the
    equally-predictive but redundant bb_bandwidth_pct/cum_ret_10d (65-83%
    pairwise overlap, combining all three barely helps beyond picking one)

Lane assignment (evaluated once, at Stage-1 trigger time):
  - Quality(D2): value_20d_avg in [19,586,155 ; 59,722,504) -- the single
    BEST-performing liquidity decile within Stage-1, not just "low end"
  - HighRisk/Reward: rsi14>=81.01 OR that day's own return>=+4.17% OR
    that day's own gap>=+1.82% OR that day's own H-L range<=0.51% -- any
    ONE of these signal-day extremes flags it (union, not AND). This is a
    genuine high-risk/high-reward tier (SL-rate ~3x baseline OOS), not a
    safety exclusion -- surfaced as a tag, kept in the funnel.
  - Core/Neutral: everything else (the majority, ~72% of Stage-1 volume)

Stage-2 conviction tiering (checked DAILY from age_day 1 through 5, then
LOCKED -- no further checks past day 5, position just holds to the D12
horizon regardless of tag from then on): the single strongest confirmation
signal found in the whole research thread is simply the MAGNITUDE of the
candidate's own return so far vs its entry price (not a technical
snapshot -- combining with up_days/smoothness was tried and DILUTED the
signal, don't re-add them). Tertile cut of the historical confirmed-
positive (ret>0) distribution, thresholds re-derived per age_day (the
tertile edges widen as more days pass):
  FADING:      ret_so_far <= 0
  VALID:       0 < ret_so_far <= t1(age_day)
  STRONG:      t1(age_day) < ret_so_far <= t2(age_day)
  VERY STRONG: ret_so_far > t2(age_day)
Historical OOS win-rate by final (day-5) tag: FADING ~27%, VALID ~64%,
STRONG ~79%, VERY STRONG ~87% -- monotonic and clean in all 3 lanes.
CAVEAT (user-flagged 2026-09-23, confirmed by the rework below): this
70-90% figure is an ILLUSION for anyone entering TODAY -- it's anchored
to entry_ref (Day 1) and mostly reflects gains a Day-1 entrant already
banked by the time the tag reaches VERY STRONG, not the odds of a FRESH
entry from here. The number that actually matters for a fresh entrant is
WIN_RATE_TABLE below (21-61% depending on day/lane/tag, current-price
basis) -- always read that dynamically, never quote this paragraph's
70-90% as if it applies to someone buying now.

SL / TP1 / TP2 (revised 2026-09-22, then TP1/TP2+win-rate fully
reworked 2026-09-23 -- read this before touching any of the three):

  MISTAKE #1 (caught by user, verified with data, RETRACTED): SL was
  first set to entry_ref_price itself ("the FADING trigger level").
  Directly tested what happens if that's enforced as a REAL every-day
  stop (exit the moment low<=entry on ANY day 1-12): **100% of trades
  get stopped at exactly 0% return** -- virtually every position dips to
  its own entry price at some point during a 12-day window from normal
  volatility, so a breakeven stop ALWAYS fires. Also tested "exit at the
  Day-5 checkpoint if still fading" (matching the tag decomposition) --
  that ALSO makes the aggregate population WORSE than doing nothing
  (win 50.6%->33.8%, median flips negative), because ~19-29% of Day-5-
  fading trades still recover to a winner by D12 if left alone, and an
  early cut throws that away. **Conclusion: do not use any tag-derived
  price as a real stop-execution level. The tag is for the ENTRY
  decision (enter or don't), never for deciding to exit something
  already held.**

  FIX: SL = entry_ref_price x (1 - SL_PCT/100), SL_PCT=10.0 -- this is
  NOT a new number, it's exactly SL_SWING=-10% that every single win/
  mean/SL-rate statistic in the whole research thread was already
  computed with. Going back to it is undoing an unvalidated detour, not
  adding a new untested rule.

  MISTAKE #2 (corrected 2026-09-22): TP was framed as "price needed to
  reach VERY STRONG today" (a tier-upgrade milestone). Kept as a
  secondary `next_tier_price()` helper, but the PRIMARY TP1/TP2 became
  real historical HIGH-touch-rate targets during the D1-D12 hold, same
  informational style as vcp_pillar.py's TP1/TP2/TP3.

  2026-09-23 REWORK (user request: "sisa ceiling gain berapa untuk TP
  dinamis, dan dasar riset untuk jangan chasing"): the 2026-09-22 TP1/
  TP2 were a SINGLE FLAT number per tag, blended across all entry days
  1-5 and computed from the ORIGINAL entry_ref (not the price a fresh
  entrant would actually pay). Re-ran the backtest keyed by
  (age_day, lane, tag) with the REMAINING window only (from that day's
  own close forward to D12, not from entry_ref) --
  research/macd_confirm_d1d5_remaining_ceiling_2026_09_23.py then
  research/macd_confirm_d1d5_tp_formula_2026_09_23.py (full matrix, all
  36 (day x lane x tag) cells n>=19, no thin-cell fallback needed).
  TP1 = ~65%-touch quantile, TP2 = ~40%-touch quantile of the remaining
  max-high return; win-rate = P(remaining_close_ret>0) from that day's
  price, i.e. "if I buy fresh TODAY at this tag, not at the original
  signal day." `TP_TABLE_BY_DAY_LANE_TAG[age_day][lane][tag]` and
  `WIN_RATE_TABLE[age_day][lane][tag]` (age_day 1 still reuses day-2's
  numbers, day 1 has no real tag). VALIDATED actual exit remains
  CLOSE-ONLY at the D12 horizon -- TP1/TP2 stay reference-only, never
  wired as an actual sell trigger.

  KEY FINDING from the rework: in Quality(D2) lane specifically, VERY
  STRONG is the WORST forward-looking bucket (win_remain 21%/27%/36%/
  43% for day 2/3/4/5 -- below that lane's own VALID/STRONG), the
  opposite of Core/Neutral and HighRisk/Reward where VERY STRONG stays
  the BEST bucket every day (HighRisk/Reward VERY STRONG: win_remain
  53-61%, huge remaining ceiling e.g. Day2 TP2 +31%). This does NOT
  contradict the original "monotonic by day-5 tag" finding (that stat
  is anchored to entry_ref, this one is anchored to today's price) --
  it means a FRESH entry into an already-VERY-STRONG Quality(D2) name
  is chasing, while the same tag in the other two lanes is not. See
  `CHASE_CUTOFF_BY_DAY_QUALITY_D2` below -- `is_chasing_too_high()` is
  now lane+day-aware: gates ONLY Quality(D2) (day-specific ret_so_far
  cutoff, empirically the top quintile's lower bound where win clearly
  degrades), leaves Core/Neutral ungated, and leaves HighRisk/Reward
  ungated (running up there is a genuine continuation signal, not
  exhaustion -- flagging it would be a false warning).

Win-rate display: shown per candidate using the SMALLEST backtest bucket
that actually matches its current (lane, tag, age_day) combination --
NOT a single blended number. `WIN_RATE_TABLE[age_day][lane][tag]`, all
cells sourced from the 2026-09-23 rework (remaining-window, current-
price basis) at each of age_day 2/3/4/5 (age_day 1 reuses day-2's
numbers as the closest available granularity -- day 1 itself has no
real tag yet).

Horizon: EXPIRED after 12 trading days (matches the backtest's HOLD=12),
close-based resolution, no laddered TP exit.
"""
from __future__ import annotations

import datetime
import json
import os

import numpy as np
import pandas as pd

from engine import legacy_core as core
from engine import scanalert as scanalert_engine  # IDX tick-size rounding (_idx_round_tick*)

# ---------------------------------------------------------------------------
# Constants (all validated in the research trail referenced above)
# ---------------------------------------------------------------------------
PICKS_FILE = os.path.join(core.PROJECT_ROOT, "macd_confirm_pillar_picks.json")

MIN_HISTORY = 60
N_LB = 10
CONSISTENCY_THRESHOLD = 0.90

ATR_MAX = 5.78
LIQ_MIN = 19_586_155.0
LIQ_MAX = 1_259_645_238.0
STD_RET_10D_MAX = 4.29

QUALITY_LIQ_MIN = 19_586_155.0
QUALITY_LIQ_MAX = 59_722_504.0
HIGHRISK_RSI_MIN = 81.01
HIGHRISK_SIGNAL_RET_MIN = 4.17
HIGHRISK_SIGNAL_GAP_MIN = 1.82
HIGHRISK_SIGNAL_RANGE_MAX = 0.51

LANE_ICONS = {"Quality(D2)": "⭐⭐", "Core/Neutral": "⚪⭐", "HighRisk/Reward": "⚠️⭐"}

# tertile (t1, t2) thresholds per age_day, TRAIN-derived from the confirmed-
# positive (ret_so_far>0) return distribution -- see docstring, don't
# re-derive without reading the research trail first
TAG_THRESHOLDS = {
    1: (0.009, 0.022),
    2: (0.014, 0.036),
    3: (0.017, 0.045),
    4: (0.018, 0.052),
    5: (0.020, 0.057),
}
MAX_TAG_DAY = 5          # tag stops updating after this age
ALERT_MAX_AGE_DAYS = 12  # matches the backtest's HOLD -- expire at D12 regardless of tag

# --- SL: back to the validated SL_SWING=-10% (see docstring MISTAKE #1) ---
SL_PCT = 10.0

# --- TP1/TP2: real historical HIGH-touch-rate targets, informational
# only, keyed by (age_day, lane, tag) -- see docstring 2026-09-23 REWORK.
# TP1 = ~65%-touch quantile, TP2 = ~40%-touch quantile of the REMAINING
# max-high return computed from that day's own close forward to D12 (not
# from entry_ref -- a fresh entrant pays today's price, not the original
# signal-day price). Source:
# research/macd_confirm_d1d5_tp_formula_2026_09_23.py, all 36 cells
# n>=19 (no thin-cell fallback needed). No TP shown for FADING (no
# position exists yet -- nothing to target). ---
TP_TABLE_BY_DAY_LANE_TAG = {
    2: {
        "Quality(D2)": {"VALID": (2.0, 6.5), "STRONG": (3.5, 7.5), "VERY STRONG": (2.5, 9.0)},
        "Core/Neutral": {"VALID": (2.5, 7.0), "STRONG": (3.5, 9.0), "VERY STRONG": (5.0, 12.5)},
        "HighRisk/Reward": {"VALID": (3.5, 6.5), "STRONG": (5.0, 10.5), "VERY STRONG": (16.5, 31.0)},
    },
    3: {
        "Quality(D2)": {"VALID": (2.5, 6.0), "STRONG": (1.5, 4.5), "VERY STRONG": (3.5, 9.5)},
        "Core/Neutral": {"VALID": (2.5, 6.5), "STRONG": (3.5, 7.5), "VERY STRONG": (5.5, 13.5)},
        "HighRisk/Reward": {"VALID": (2.0, 5.5), "STRONG": (3.5, 8.0), "VERY STRONG": (10.0, 24.5)},
    },
    4: {
        "Quality(D2)": {"VALID": (1.5, 4.0), "STRONG": (3.5, 8.0), "VERY STRONG": (3.5, 15.5)},
        "Core/Neutral": {"VALID": (2.5, 6.0), "STRONG": (3.0, 7.0), "VERY STRONG": (5.5, 12.5)},
        "HighRisk/Reward": {"VALID": (3.0, 6.0), "STRONG": (4.0, 7.0), "VERY STRONG": (8.0, 19.5)},
    },
    5: {
        "Quality(D2)": {"VALID": (2.0, 4.5), "STRONG": (3.5, 6.0), "VERY STRONG": (5.0, 19.0)},
        "Core/Neutral": {"VALID": (2.5, 6.0), "STRONG": (3.0, 6.5), "VERY STRONG": (5.0, 10.0)},
        "HighRisk/Reward": {"VALID": (2.0, 5.5), "STRONG": (2.5, 7.0), "VERY STRONG": (9.0, 20.5)},
    },
}
TP_TABLE_BY_DAY_LANE_TAG[1] = TP_TABLE_BY_DAY_LANE_TAG[2]  # day 1 has no real tag yet, reuse day 2 as closest granularity

# --- Chase-risk cutoff: Quality(D2) ONLY (see docstring KEY FINDING --
# the other two lanes show no exhaustion, gating them would be a false
# warning). ret_so_far_pct >= this day's cutoff -> chasing. Empirically
# the top-quintile lower bound where win_remain clearly degrades, from
# the same research script. ---
CHASE_CUTOFF_BY_DAY_QUALITY_D2 = {1: 3.6, 2: 3.6, 3: 4.9, 4: 7.3, 5: 8.8}

# --- Win-rate lookup: WIN_RATE_TABLE[age_day][lane][tag] -> win% (float).
# 2026-09-23 REWORK: now P(remaining_close_ret>0) computed from that
# day's own price forward to D12 (current-price basis, matches the TP
# table above), NOT from entry_ref like the original 2026-09-22 table.
# Day 1 reuses day 2's numbers (day 1 itself has no real tag yet). ---
_WIN_RATE_DAY2 = {
    "Quality(D2)": {"FADING": 57.4, "VALID": 50.0, "STRONG": 45.5, "VERY STRONG": 21.1},
    "Core/Neutral": {"FADING": 51.8, "VALID": 50.2, "STRONG": 49.1, "VERY STRONG": 52.1},
    "HighRisk/Reward": {"FADING": 46.7, "VALID": 54.7, "STRONG": 57.6, "VERY STRONG": 60.9},
}
_WIN_RATE_DAY3 = {
    "Quality(D2)": {"FADING": 55.2, "VALID": 52.9, "STRONG": 44.8, "VERY STRONG": 26.9},
    "Core/Neutral": {"FADING": 51.9, "VALID": 49.8, "STRONG": 43.6, "VERY STRONG": 51.2},
    "HighRisk/Reward": {"FADING": 46.0, "VALID": 46.8, "STRONG": 41.0, "VERY STRONG": 55.8},
}
_WIN_RATE_DAY4 = {
    "Quality(D2)": {"FADING": 53.9, "VALID": 40.9, "STRONG": 57.6, "VERY STRONG": 36.4},
    "Core/Neutral": {"FADING": 51.4, "VALID": 51.4, "STRONG": 44.9, "VERY STRONG": 48.8},
    "HighRisk/Reward": {"FADING": 44.4, "VALID": 53.9, "STRONG": 40.0, "VERY STRONG": 52.4},
}
_WIN_RATE_DAY5 = {
    "Quality(D2)": {"FADING": 48.7, "VALID": 50.0, "STRONG": 61.1, "VERY STRONG": 43.2},
    "Core/Neutral": {"FADING": 48.7, "VALID": 53.4, "STRONG": 44.8, "VERY STRONG": 46.8},
    "HighRisk/Reward": {"FADING": 40.4, "VALID": 55.2, "STRONG": 38.2, "VERY STRONG": 53.2},
}
WIN_RATE_TABLE = {1: _WIN_RATE_DAY2, 2: _WIN_RATE_DAY2, 3: _WIN_RATE_DAY3, 4: _WIN_RATE_DAY4, 5: _WIN_RATE_DAY5}


# ---------------------------------------------------------------------------
# Feature computation — single ticker, TODAY's row only.
# ---------------------------------------------------------------------------
def _compute_latest_features(df: pd.DataFrame) -> dict | None:
    """df: output of core.get_ohlcv_daily_from_db (index=date, columns
    Open/High/Low/Close/Volume, ascending). Returns None if not enough
    history for a reliable read."""
    if df is None or len(df) < MIN_HISTORY:
        return None

    close = df["Close"]; high = df["High"]; low = df["Low"]; volume = df["Volume"]
    daily_ret = close.pct_change()

    macd_line = close.rolling(12).mean() - close.rolling(26).mean()
    pct_above_10d = macd_line.gt(0).rolling(N_LB).mean()
    macd_slope_5d = macd_line - macd_line.shift(5)

    down_day = (daily_ret < 0).astype(int)
    three_down = down_day & down_day.shift(1).fillna(0).astype(int) & down_day.shift(2).fillna(0).astype(int)
    no_3down_streak = three_down.rolling(N_LB).max().eq(0)

    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr14_pct = tr.ewm(alpha=1 / 14, adjust=False).mean() / close * 100

    value_20d_avg = (close * volume).rolling(20).mean()
    std_ret_10d = daily_ret.rolling(10).std() * 100
    rsi14 = core.calculate_rsi(close)

    signal_day_ret = daily_ret * 100
    signal_day_gap_pct = (df["Open"] - prev_close) / prev_close * 100
    signal_day_range_pct = (high - low) / close * 100

    def last(s):
        v = s.iloc[-1]
        return float(v) if pd.notna(v) else None

    return {
        "close": float(close.iloc[-1]),
        "pct_above_10d": last(pct_above_10d),
        "macd_slope_5d": last(macd_slope_5d),
        "no_3down_streak": bool(no_3down_streak.iloc[-1]) if pd.notna(no_3down_streak.iloc[-1]) else False,
        "atr14_pct": last(atr14_pct),
        "value_20d_avg": last(value_20d_avg),
        "std_ret_10d": last(std_ret_10d),
        "rsi14": last(rsi14),
        "signal_day_ret": last(signal_day_ret),
        "signal_day_gap_pct": last(signal_day_gap_pct),
        "signal_day_range_pct": last(signal_day_range_pct),
        "latest_date": df.index[-1].strftime("%Y-%m-%d"),
    }


def _passes_stage1(feats: dict) -> bool:
    if feats["pct_above_10d"] is None or feats["pct_above_10d"] < CONSISTENCY_THRESHOLD:
        return False
    if feats["macd_slope_5d"] is None or feats["macd_slope_5d"] < 0:
        return False
    if not feats["no_3down_streak"]:
        return False
    if feats["atr14_pct"] is None or feats["atr14_pct"] > ATR_MAX:
        return False
    if feats["value_20d_avg"] is None or not (LIQ_MIN <= feats["value_20d_avg"] <= LIQ_MAX):
        return False
    if feats["std_ret_10d"] is None or feats["std_ret_10d"] > STD_RET_10D_MAX:
        return False
    return True


def _assign_lane(feats: dict) -> str:
    if QUALITY_LIQ_MIN <= feats["value_20d_avg"] < QUALITY_LIQ_MAX:
        return "Quality(D2)"
    is_highrisk = (
        (feats["rsi14"] is not None and feats["rsi14"] >= HIGHRISK_RSI_MIN)
        or (feats["signal_day_ret"] is not None and feats["signal_day_ret"] >= HIGHRISK_SIGNAL_RET_MIN)
        or (feats["signal_day_gap_pct"] is not None and feats["signal_day_gap_pct"] >= HIGHRISK_SIGNAL_GAP_MIN)
        or (feats["signal_day_range_pct"] is not None and feats["signal_day_range_pct"] <= HIGHRISK_SIGNAL_RANGE_MAX)
    )
    return "HighRisk/Reward" if is_highrisk else "Core/Neutral"


def _tag_of(ret_so_far: float | None, age_day: int) -> str:
    if ret_so_far is None or ret_so_far <= 0:
        return "FADING"
    day = min(age_day, MAX_TAG_DAY)
    t1, t2 = TAG_THRESHOLDS[day]
    if ret_so_far <= t1:
        return "VALID"
    if ret_so_far <= t2:
        return "STRONG"
    return "VERY STRONG"


def evaluate_ticker(ticker: str) -> dict | None:
    """Returns a candidate dict if `ticker` qualifies for Stage-1 TODAY,
    else None. entry_ref_price = today's close (same simplification as
    buy_on_weakness.py / vcp_pillar.py -- age_days/tag tracking starts
    from the NEXT nightly re-evaluation onward)."""
    df = core.get_ohlcv_daily_from_db(ticker, limit=150)
    feats = _compute_latest_features(df)
    if feats is None or not _passes_stage1(feats):
        return None

    lane = _assign_lane(feats)
    entry_ref = feats["close"]

    return {
        "ticker": ticker,
        "trigger_date": feats["latest_date"],
        "status": "ALIVE",
        "lane": lane,
        "age_days": 1,
        "entry_ref_price": entry_ref,
        "current_price": entry_ref,  # updated nightly in _resolve_active_pick
        "sl_price": scanalert_engine._idx_round_tick_floor(entry_ref * (1 - SL_PCT / 100)),
        "tag": "VALID" if 1 in TAG_THRESHOLDS else "FADING",  # placeholder, resolved tomorrow onward
        "ret_so_far_pct": None,
        "resolved_date": None,
        "resolved_ret_pct": None,
    }


def compute_macd_confirm_candidates(tickers: list[str]) -> list[dict]:
    """Nightly entry point: evaluate every ticker in `tickers`, return the
    list that qualifies today (does NOT touch the picks-history file --
    call update_and_save_picks() with the result to persist/merge)."""
    candidates = []
    for t in tickers:
        try:
            c = evaluate_ticker(t)
        except Exception as e:
            print(f"⚠️ MACD-confirm pillar: gagal evaluasi {t}: {e}")
            continue
        if c is not None:
            candidates.append(c)
    return candidates


# ---------------------------------------------------------------------------
# Pick-history persistence -- same 3-layer safety pattern as
# buy_on_weakness.py / vcp_pillar.py (REUSE core's existing generic helpers).
# ---------------------------------------------------------------------------
def load_macd_confirm_picks() -> list:
    if not os.path.exists(PICKS_FILE):
        return []
    try:
        with open(PICKS_FILE) as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ Gagal membaca macd_confirm_pillar_picks.json: {e}")
        return []


def save_macd_confirm_picks(picks: list):
    tmp_path = PICKS_FILE + ".tmp"
    try:
        core._backup_json_file_daily(PICKS_FILE)
        with open(tmp_path, "w") as f:
            json.dump(picks, f, indent=2, default=core._json_default_numpy_safe)
        os.replace(tmp_path, PICKS_FILE)
    except Exception as e:
        print(f"⚠️ Gagal menyimpan macd_confirm_pillar_picks.json: {e}")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


def _resolve_active_pick(pick: dict) -> dict:
    """Re-check one still-ALIVE pick against fresh OHLCV: bump age, compute
    ret_so_far vs entry_ref_price, re-derive tag (locked after age_day 5),
    expire at ALERT_MAX_AGE_DAYS regardless of tag (matches the backtest's
    close-only-at-D12 exit -- no TP is ever wired as an actual trigger).

    Also refreshes `current_price` and `sl_price` off TODAY's close (user
    correction 2026-09-23: don't assume the trader already entered at
    entry_ref_price -- SL/TP shown to the user must be computed from the
    price they'd actually pay if they entered NOW, not the stale D0/D1
    anchor. `entry_ref_price` and `ret_so_far_pct`/`tag` stay anchored to
    the original signal day -- that's the research-validated basis for the
    conviction tag itself, only the execution levels (SL/TP1/TP2) move."""
    df = core.get_ohlcv_daily_from_db(pick["ticker"], limit=150)
    feats = _compute_latest_features(df)
    if feats is None:
        return pick  # can't evaluate today (e.g. delisted/no data) -- leave as-is

    today_date = feats["latest_date"]
    if today_date == pick.get("_last_checked_date"):
        return pick  # already resolved for today, avoid double-incrementing age

    pick["age_days"] = pick.get("age_days", 1) + 1
    entry_ref = pick["entry_ref_price"]
    ret_so_far = (feats["close"] - entry_ref) / entry_ref
    pick["ret_so_far_pct"] = round(ret_so_far * 100, 2)
    pick["tag"] = _tag_of(ret_so_far, pick["age_days"])

    pick["current_price"] = feats["close"]
    pick["sl_price"] = scanalert_engine._idx_round_tick_floor(feats["close"] * (1 - SL_PCT / 100))

    if pick["age_days"] > ALERT_MAX_AGE_DAYS:
        pick["status"] = "EXPIRED"
        pick["resolved_date"] = today_date
        pick["resolved_ret_pct"] = pick["ret_so_far_pct"]

    pick["_last_checked_date"] = today_date
    return pick


def update_and_save_picks(new_candidates: list[dict]) -> list:
    """Merge today's qualifying candidates into the persisted pick history:
    re-evaluate every still-active existing pick (age/tag/expiry), then
    append a fresh entry ONLY for tickers that don't already have an
    active pick. Call once per nightly cycle."""
    history = load_macd_confirm_picks()

    active_tickers = set()
    for pick in history:
        if pick.get("status") == "ALIVE":
            try:
                pick = _resolve_active_pick(pick)
            except Exception as e:
                print(f"⚠️ MACD-confirm pillar: gagal resolve pick {pick.get('ticker')}: {e}")
            active_tickers.add(pick["ticker"])

    for cand in new_candidates:
        if cand["ticker"] in active_tickers:
            continue
        history.append(cand)
        active_tickers.add(cand["ticker"])

    save_macd_confirm_picks(history)
    return history


# ---------------------------------------------------------------------------
# Display helpers
# ---------------------------------------------------------------------------
def format_age_label(pick: dict) -> str:
    """BOW/VCP-style age label: 'NEW (Day 1/5)' or 'AGING (Day N/5) — ALIVE'."""
    age = pick.get("age_days", 1)
    capped = min(age, MAX_TAG_DAY)
    return f"NEW (Day {age}/5)" if age <= 1 else f"AGING (Day {capped}/5) — ALIVE"


TAG_ICONS = {"FADING": "🔴", "VALID": "🟡", "STRONG": "🟠", "VERY STRONG": "🟢"}
_NEXT_TAG = {"FADING": "VALID", "VALID": "STRONG", "STRONG": "VERY STRONG"}  # VERY STRONG has no next


def next_tier_price(pick: dict) -> tuple[str, float] | None:
    """NOT a sell target -- see module docstring MISTAKE #2. Returns
    (next_tag_name, price) needed to upgrade to the NEXT tag TODAY (e.g.
    VALID->STRONG uses t1, STRONG->VERY STRONG uses t2), or None if
    already VERY STRONG (nothing further to show) or tag not yet
    resolved. Rounded UP to the nearest valid IDX tick (matches
    engine/bsjp2.py's TP rounding) so it doesn't undershoot."""
    tag = pick.get("tag")
    if tag is None or tag == "VERY STRONG":
        return None
    day = min(pick.get("age_days", 1), MAX_TAG_DAY)
    t1, t2 = TAG_THRESHOLDS[day]
    threshold = t1 if tag in ("FADING", "VALID") else t2
    raw = pick["entry_ref_price"] * (1 + threshold)
    return _NEXT_TAG[tag], scanalert_engine._idx_round_tick_ceil(raw)


def tp_prices(pick: dict) -> tuple[float, float] | None:
    """TP1/TP2 = real historical HIGH-touch-rate targets, keyed by
    (age_day, lane, tag) and computed from CURRENT price (see module
    docstring 2026-09-23 REWORK -- the REMAINING window from today's
    price forward to D12, not entry_ref_price; nothing has been bought
    yet, so both the % AND the day/lane context must match where the
    trader would actually enter today). Informational reference, NOT an
    exit trigger; validated exit is close-only at D12. None for FADING
    (no position exists yet)."""
    tag = pick.get("tag")
    if tag is None or tag == "FADING":
        return None
    day = min(pick.get("age_days", 1), MAX_TAG_DAY)
    cell = TP_TABLE_BY_DAY_LANE_TAG.get(day, {}).get(pick["lane"], {}).get(tag)
    if cell is None:
        return None
    tp1_pct, tp2_pct = cell
    current = pick.get("current_price", pick["entry_ref_price"])
    tp1 = scanalert_engine._idx_round_tick_ceil(current * (1 + tp1_pct / 100))
    tp2 = scanalert_engine._idx_round_tick_ceil(current * (1 + tp2_pct / 100))
    return tp1, tp2


def is_chasing_too_high(pick: dict) -> bool:
    """No-chasing guardrail, made lane+day-aware 2026-09-23 (see module
    docstring KEY FINDING): gates ONLY Quality(D2) lane -- that's the
    ONE lane where a fresh entry into an already-run-up name showed
    real forward degradation (win_remain drops sharply past the day-
    specific cutoff in CHASE_CUTOFF_BY_DAY_QUALITY_D2). Core/Neutral and
    HighRisk/Reward showed NO exhaustion pattern (HighRisk/Reward's
    biggest-run-up bucket is actually its BEST bucket) -- gating those
    would be a false warning, so they always return False. FADING/None
    tag -> False (irrelevant, already hidden from display elsewhere)."""
    tag = pick.get("tag")
    if tag is None or tag == "FADING":
        return False
    if pick.get("lane") != "Quality(D2)":
        return False
    ret_so_far = pick.get("ret_so_far_pct")
    if ret_so_far is None:
        return False
    day = min(pick.get("age_days", 1), MAX_TAG_DAY)
    cutoff = CHASE_CUTOFF_BY_DAY_QUALITY_D2.get(day)
    if cutoff is None:
        return False
    return ret_so_far >= cutoff


def needs_hard_sl_warning(pick: dict) -> bool:
    """Hard-SL emphasis flag (user request 2026-09-23), HighRisk/Reward
    VERY STRONG ONLY. Rationale: research/macd_confirm_clean_population_
    sl_mae_loss_2026_09_23.py found this exact (lane, tag) cell has by far
    the fattest loss tail in the whole clean population -- mean loss
    among D12-close losers is -16.9% (close-only, no real stop), vs -3%
    to -6% for every other lane/tag cell. Directly tested applying
    SL_PCT=-10% as a REAL intraday exit for this cell specifically
    (research/macd_confirm_d1d5_sl_tp_touch_winrate_2026_09_23.py +
    follow-up): mean loss among losers contracts to -9.2% (close to the
    SL floor itself) at the cost of win-rate dropping ~9pt (55.5%->46.4%)
    -- a real, data-backed trade-off, unlike VALID/STRONG in the same
    lane where SL barely moves the numbers (SL rarely touched there).
    Does NOT change the module's close-only default exit for the pillar
    as a whole -- this is a candidate-level warning only, telling the
    trader that THIS specific cell's downside is uniquely severe if left
    unmanaged, not a change to the validated backtest exit rule."""
    return pick.get("lane") == "HighRisk/Reward" and pick.get("tag") == "VERY STRONG"


def win_rate_of(pick: dict) -> float | None:
    """Smallest matching backtest bucket for this candidate's CURRENT
    (age_day, lane, tag) -- not a blended average. See WIN_RATE_TABLE."""
    tag = pick.get("tag")
    if tag is None:
        return None
    day = min(pick.get("age_days", 1), MAX_TAG_DAY)
    return WIN_RATE_TABLE.get(day, {}).get(pick["lane"], {}).get(tag)
