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

SL / TP1 / TP2 (revised 2026-09-22 after two mistakes were caught and
fixed -- read this before touching any of the three):

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

  MISTAKE #2 (also corrected): TP was framed as "price needed to reach
  VERY STRONG today" (a tier-upgrade milestone). Kept as a secondary
  `next_tier_price()` helper (renamed from the old `tp_price()` /
  `very_strong_milestone_price()`), but the PRIMARY TP1/TP2 shown to the
  user are now real historical HIGH-touch-rate targets during the D1-D12
  hold, same informational style as vcp_pillar.py's TP1/TP2/TP3 (touch-
  rate, NOT a claim that this is the recommended sell point):
    VALID:       TP1 +5% (~47-58% touch), TP2 +8%  (~29-41% touch)
    STRONG:      TP1 +8% (~49-91% touch), TP2 +10% (~37-51% touch)
    VERY STRONG: TP1 +10% (~90-97% touch), TP2 +15% (~67-90% touch)
  (ranges reflect real lane-to-lane spread; one flat number per tag is
  used for simplicity, matching the user's requested message layout).
  The VALIDATED actual exit remains CLOSE-ONLY at the D12 horizon --
  every TP-ladder variant tested in research traded away mean for a
  higher win-rate illusion. TP1/TP2 are reference-only, never wired as
  an actual sell trigger.

Win-rate display: shown per candidate using the SMALLEST backtest bucket
that actually matches its current (lane, tag, age_day) combination --
NOT a single blended number. `WIN_RATE_TABLE[age_day][lane][tag]`, all
cells sourced from the OOS TEST lane x tag matrices computed same session
(see memory) at each of age_day 2/3/4/5 (age_day 1 reuses day-2's numbers
as the closest available granularity -- day 1 itself wasn't matrix-tested).

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

# --- TP1/TP2: real historical HIGH-touch-rate targets, informational only,
# see docstring MISTAKE #2. One flat pct per tag (lane-to-lane spread
# noted in the docstring, simplified here). No TP shown for FADING
# (no position exists yet -- nothing to target). ---
TP1_PCT_BY_TAG = {"VALID": 5.0, "STRONG": 8.0, "VERY STRONG": 10.0}
TP2_PCT_BY_TAG = {"VALID": 8.0, "STRONG": 10.0, "VERY STRONG": 15.0}

# --- Win-rate lookup: WIN_RATE_TABLE[age_day][lane][tag] -> win% (float),
# sourced from the OOS TEST lane x tag matrices (same research session,
# see memory project_macd_confirm_pillar_shipped_2026_09_22.md). Day 1
# reuses day 2's numbers (day 1 itself has no matrix, too little signal
# by definition -- entry day only). ---
_WIN_RATE_DAY2 = {
    "Quality(D2)": {"FADING": 42.0, "VALID": 62.0, "STRONG": 56.0, "VERY STRONG": 70.0},
    "Core/Neutral": {"FADING": 38.0, "VALID": 57.0, "STRONG": 67.0, "VERY STRONG": 83.0},
    "HighRisk/Reward": {"FADING": 28.0, "VALID": 53.0, "STRONG": 61.0, "VERY STRONG": 71.0},
}
_WIN_RATE_DAY3 = {
    "Quality(D2)": {"FADING": 36.0, "VALID": 60.0, "STRONG": 70.0, "VERY STRONG": 81.0},
    "Core/Neutral": {"FADING": 34.0, "VALID": 65.0, "STRONG": 68.0, "VERY STRONG": 89.0},
    "HighRisk/Reward": {"FADING": 25.0, "VALID": 62.0, "STRONG": 60.0, "VERY STRONG": 79.0},
}
_WIN_RATE_DAY4 = {
    "Quality(D2)": {"FADING": 34.0, "VALID": 52.0, "STRONG": 83.0, "VERY STRONG": 82.0},
    "Core/Neutral": {"FADING": 31.0, "VALID": 65.0, "STRONG": 74.0, "VERY STRONG": 88.0},
    "HighRisk/Reward": {"FADING": 21.0, "VALID": 63.0, "STRONG": 65.0, "VERY STRONG": 79.0},
}
_WIN_RATE_DAY5 = {
    "Quality(D2)": {"FADING": 26.0, "VALID": 71.0, "STRONG": 78.0, "VERY STRONG": 83.0},
    "Core/Neutral": {"FADING": 29.0, "VALID": 64.0, "STRONG": 81.0, "VERY STRONG": 88.0},
    "HighRisk/Reward": {"FADING": 19.0, "VALID": 62.0, "STRONG": 72.0, "VERY STRONG": 84.0},
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
    """TP1/TP2 = real historical HIGH-touch-rate targets for this tag,
    computed from CURRENT price (user correction 2026-09-23 -- not
    entry_ref_price; nothing has been bought yet, so the % must be
    reachable from where the trader would actually enter today).
    Informational reference, NOT an exit trigger -- see module docstring
    MISTAKE #2; validated exit is close-only at D12. None for FADING (no
    position exists yet)."""
    tag = pick.get("tag")
    if tag is None or tag == "FADING" or tag not in TP1_PCT_BY_TAG:
        return None
    current = pick.get("current_price", pick["entry_ref_price"])
    tp1 = scanalert_engine._idx_round_tick_ceil(current * (1 + TP1_PCT_BY_TAG[tag] / 100))
    tp2 = scanalert_engine._idx_round_tick_ceil(current * (1 + TP2_PCT_BY_TAG[tag] / 100))
    return tp1, tp2


def is_chasing_too_high(pick: dict) -> bool:
    """No-chasing guardrail (user request 2026-09-23): if price has
    already run further from entry_ref_price than this tag's OWN TP2
    touch-rate target (see TP2_PCT_BY_TAG -- reusing the already-
    validated ceiling rather than inventing a new number), a fresh entry
    TODAY is chasing a move that's statistically already past its typical
    upside for this tag, not catching it early. FADING/None tag -> False
    (irrelevant, already hidden from display elsewhere)."""
    tag = pick.get("tag")
    if tag is None or tag == "FADING" or tag not in TP2_PCT_BY_TAG:
        return False
    ret_so_far = pick.get("ret_so_far_pct")
    if ret_so_far is None:
        return False
    return ret_so_far > TP2_PCT_BY_TAG[tag]


def win_rate_of(pick: dict) -> float | None:
    """Smallest matching backtest bucket for this candidate's CURRENT
    (age_day, lane, tag) -- not a blended average. See WIN_RATE_TABLE."""
    tag = pick.get("tag")
    if tag is None:
        return None
    day = min(pick.get("age_days", 1), MAX_TAG_DAY)
    return WIN_RATE_TABLE.get(day, {}).get(pick["lane"], {}).get(tag)
