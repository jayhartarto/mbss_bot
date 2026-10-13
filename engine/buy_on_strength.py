"""
engine/buy_on_strength.py — MBSS v2 (2026-10-13), new /swing lane: "BOS"
(Buy On Strength).

"Buy the stock that's already proven strength (ret_10d>+5%) and keeps
going, specifically during an IHSG bear-market RECOVERY phase (BOUNCE_OFF_
LOW) -- resilience against the crash, not a bottom-guess." Deliberate
pair/contrast to BOW ("buy the quiet dip in an uptrend") and OSB-v2 ("buy
the deep-oversold bounce during fresh capitulation" = CRASH_FAST, the
OPPOSITE sub-phase, see engine/ihsg_regime.py's docstring for why the two
lanes are complementary, not redundant). Full research trail, OOS
validation (2 bear episodes, 4-fold split), and every rejected variant
(cycling, staged entry, delayed confirmed-entry) live in memory
project_ret10d_crash_bounce_dna_filter_oos_validated_2026_10_13.md and its
follow-ups (project_ret10d_*_2026_10_13.md series) -- do not re-derive the
thresholds below without reading that trail first.

Gate (all conditions required, computed causally from bars up to and
including the trigger day):
  - IHSG regime: engine/ihsg_regime.py's `is_bounce_off_low()` --
    bear_episode_id>0 AND regime_phase=='BOUNCE_OFF_LOW'. Lane produces
    NOTHING outside this regime (same "deliberately frozen" convention as
    OSB-v2's dd_100 gate) -- confirmed in the regime-robustness check that
    this edge is roughly HALF as strong in NEUTRAL_BULLISH conditions, so
    this must stay regime-gated, never always-on.
  - ret_10d (close[i] vs close[i-10]) > +5%.
  - 6-feature "don't chase the most-extended mover" filter, all BELOW
    their fixed cutoff (averaged from the two OOS-trained cutoff sets,
    ep13-trained and ep14-trained, for a single stable production
    threshold -- see module-level constants): rs_vs_ihsg_10d,
    dist_sma20_pct, vol_ratio, ret_10d itself, pctb (Bollinger %B),
    dist_low60_pct.
  - Pre-entry 2-feature filter, BOTH above their fixed cutoff:
    max_day_contrib_pct (the move is concentrated in ONE big day, not a
    smooth multi-day grind -- counter-intuitively the stronger signal
    here, opposite this project's usual "quiet mover" bias) and
    atr_pct_20d (elevated volatility). NOTE: stacking a 3rd/4th feature
    here was tested and OVERFITS (improves one bear episode, hurts the
    other) -- 2 features is the validated ceiling, do not add more
    without fresh OOS validation.
  - Liquidity: deliberately NO floor -- tested 0.15B/0.5B/1.0B, all either
    neutral or actively hurt the backtest (the edge isn't concentrated in
    mega-liquid names); do not add one without re-validating first.

No IHSG day-return trigger (unlike OSB-v2/BOW) -- this lane's signal is
the stock's OWN 10-day momentum plus the DNA filters, not a same-day
market-relative trigger.

Exit mechanics:
  - TP = +8% (raised from the original research default of +5% after
    reviewing hold-duration economics -- the full-10-day-timeout bucket
    had NEGATIVE mean return/ret-per-day in BOTH bear episodes; TP10%+
    didn't help further).
  - SL = -15% (normal), auto-TIGHTENED to -7% the moment the FIRST day of
    holding (the day after entry_ref) closes down >=2% from entry_ref --
    this is a quick-cut-loss EARLY WARNING, not an immediate exit: swept
    -3% to -12%, no level actually lifts return above the no-tighten
    baseline (tightening always costs some return), but -7%/-8% minimizes
    that cost while still cutting the big-loser(<=-8%) rate hard. -5% (an
    earlier default) and anything looser than -8% are NOT optimal -- do
    not use those if ever re-tuning.
  - Max hold = 6 trading days is DISPLAY GUIDANCE ONLY (user's explicit
    call, 2026-10-13) -- a written note in the bot output once age_days
    reaches 6, NOT an auto-exit. The backtest found trades resolving fast
    (1-2 days) are far more capital-efficient (ret/day 2-3%+) than the
    ones dragging to the full window (negative ret/day) -- the note exists
    to nudge the user, the pick itself keeps tracking until it actually
    hits TP/SL or the safety-backstop ALERT_MAX_AGE_DAYS below.
  - ALERT_MAX_AGE_DAYS (safety backstop only, distinct from the 6-day
    guidance note): picks older than this are marked EXPIRED
    (mark-to-market) purely so the persisted history doesn't grow
    unbounded with ancient dead picks -- not itself a validated exit rule.

The day+2 cumulative-return>=2-3% "let it run toward 10%+" signal
(validated 80-82% MFE>=10% hit rate when stacked with the pre-entry
filter) is DELIBERATELY NOT wired here -- user's explicit call, stays a
manual watchlist observation, not automated logic.

CAVEAT carried over from the whole research thread: only 2 bear episodes
(2024-25 and 2026) exist to validate against; ~35% of raw signals are
same-ticker repeat-fires within a 7-day window (real but not actionably-
fixable correlation, see the wiring-prep memory); treat backtest numbers
as upper bounds within the regime they were validated in.
"""
from __future__ import annotations

import json
import math
import os

import pandas as pd

from engine import legacy_core as core
import engine.ihsg_regime as ihsg_regime_engine

PICKS_FILE = os.path.join(core.PROJECT_ROOT, "buy_on_strength_picks.json")

MIN_HISTORY = 150
RET_10D_MIN = 5.0

# 6-feature filter cutoffs -- averaged from the two OOS-trained cutoff sets
# (ep13-trained: used to test ep14; ep14-trained: used to test ep13), for
# a single stable production threshold. See
# project_ret10d_crash_bounce_dna_filter_oos_validated_2026_10_13.md.
RS_VS_IHSG_10D_MAX = 9.0
DIST_SMA20_MAX = 7.5
VOL_RATIO_MAX = 0.95
RET_10D_MAX = 10.5
PCTB_MAX = 0.85
DIST_LOW60_MAX = 28.5

# Pre-entry 2-feature filter cutoffs (top-40% equivalent, averaged across
# the two OOS directions). See
# project_ret10d_preentry_filter_and_day2_compounding_2026_10_13.md.
MAX_DAY_CONTRIB_MIN = 0.85
ATR_PCT_20D_MIN = 5.5

TP_PCT = 8.0
SL_PCT_NORMAL = -15.0
SL_PCT_TIGHT = -7.0
DAY0_TIGHTEN_THRESH_PCT = -2.0
MAX_HOLD_GUIDANCE_DAYS = 6   # display note only, see module docstring
ALERT_MAX_AGE_DAYS = 20      # safety backstop, not a validated exit rule


def _compute_latest_features(df) -> dict | None:
    """df: output of core.get_ohlcv_daily_from_db (index=date, columns
    Open/High/Low/Close/Volume, ascending)."""
    if df is None or len(df) < MIN_HISTORY:
        return None

    close = df["Close"]; high = df["High"]; low = df["Low"]; volume = df["Volume"]

    sma20 = close.rolling(20).mean(); std20 = close.rolling(20).std()
    bb_upper = sma20 + 2 * std20; bb_lower = sma20 - 2 * std20
    bw = (bb_upper - bb_lower).replace(0, float("nan"))
    pctb = (close - bb_lower) / bw
    dist_sma20_pct = (close / sma20 - 1) * 100

    vol_sma20 = volume.rolling(20).mean()
    vol_ratio = volume / vol_sma20

    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr_pct_20d = (tr / close * 100).rolling(20).mean()

    ret_10d = close.pct_change(10) * 100

    low60 = close.rolling(60, min_periods=20).min()
    dist_low60_pct = (close / low60 - 1) * 100

    close_arr = close.to_numpy()
    n = len(close_arr)
    max_day_contrib = float("nan")
    if n > 10:
        total_move = close_arr[-1] - close_arr[-11]
        if abs(total_move) > 1e-9:
            day_gains = close_arr[-10:] - close_arr[-11:-1]
            max_day_contrib = float(max(abs(x) for x in day_gains) / abs(total_move))

    raw_vals = {
        "close": float(close.iloc[-1]),
        "ret_10d": float(ret_10d.iloc[-1]),
        "dist_sma20_pct": float(dist_sma20_pct.iloc[-1]),
        "vol_ratio": float(vol_ratio.iloc[-1]),
        "pctb": float(pctb.iloc[-1]),
        "dist_low60_pct": float(dist_low60_pct.iloc[-1]),
        "atr_pct_20d": float(atr_pct_20d.iloc[-1]),
        "max_day_contrib_pct": max_day_contrib,
    }
    if any(math.isnan(v) for v in raw_vals.values()):
        return None
    raw_vals["latest_date"] = df.index[-1].strftime("%Y-%m-%d")
    return raw_vals


def evaluate_ticker(ticker: str, ihsg_ret_10d_today: float | None) -> dict | None:
    """Returns a candidate dict if `ticker` qualifies TODAY, else None.
    Caller is responsible for the IHSG regime gate (checked once for the
    whole batch, not per-ticker -- see compute_buy_on_strength_candidates)."""
    if ihsg_ret_10d_today is None:
        return None  # rs_vs_ihsg_10d needs IHSG's own 10d return -- can't evaluate without it

    df = core.get_ohlcv_daily_from_db(ticker, limit=250)
    feats = _compute_latest_features(df)
    if feats is None:
        return None

    if feats["ret_10d"] <= RET_10D_MIN:
        return None
    rs_vs_ihsg_10d = feats["ret_10d"] - ihsg_ret_10d_today
    if rs_vs_ihsg_10d >= RS_VS_IHSG_10D_MAX:
        return None
    if feats["dist_sma20_pct"] >= DIST_SMA20_MAX:
        return None
    if feats["vol_ratio"] >= VOL_RATIO_MAX:
        return None
    if feats["ret_10d"] >= RET_10D_MAX:
        return None
    if feats["pctb"] >= PCTB_MAX:
        return None
    if feats["dist_low60_pct"] >= DIST_LOW60_MAX:
        return None
    if feats["max_day_contrib_pct"] < MAX_DAY_CONTRIB_MIN:
        return None
    if feats["atr_pct_20d"] < ATR_PCT_20D_MIN:
        return None

    entry_ref = feats["close"]
    return {
        "ticker": ticker,
        "trigger_date": feats["latest_date"],
        "status": "ALIVE",
        "age_days": 1,
        "entry_ref_price": entry_ref,
        "sl_pct": SL_PCT_NORMAL,
        "sl_tightened": False,
        "quick_sl_alert_pending": False,
        "ret_10d_at_trigger": round(feats["ret_10d"], 2),
        "rs_vs_ihsg_10d_at_trigger": round(rs_vs_ihsg_10d, 2),
        "resolved_date": None,
        "resolved_ret_pct": None,
    }


def compute_buy_on_strength_candidates(tickers: list[str]) -> list[dict]:
    """Nightly entry point. Does NOT touch the picks-history file -- call
    update_and_save_picks() with the result to persist/merge."""
    if not ihsg_regime_engine.is_bounce_off_low():
        return []  # regime gate: lane deliberately produces nothing outside BOUNCE_OFF_LOW

    regime = ihsg_regime_engine.get_ihsg_regime_today()
    ihsg_ret_10d_today = regime.get("ret_10") if regime else None

    candidates = []
    for t in tickers:
        try:
            c = evaluate_ticker(t, ihsg_ret_10d_today)
        except Exception as e:
            print(f"⚠️ Buy On Strength (BOS): gagal evaluasi {t}: {e}")
            continue
        if c is not None:
            candidates.append(c)
    return candidates


# ---------------------------------------------------------------------------
# Pick-history persistence -- same 3-layer safety pattern as
# oversold_bounce_v2.py / buy_on_weakness.py.
# ---------------------------------------------------------------------------
def load_buy_on_strength_picks() -> list:
    if not os.path.exists(PICKS_FILE):
        return []
    try:
        with open(PICKS_FILE) as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ Gagal membaca buy_on_strength_picks.json: {e}")
        return []


def save_buy_on_strength_picks(picks: list):
    tmp_path = PICKS_FILE + ".tmp"
    try:
        core._backup_json_file_daily(PICKS_FILE)
        with open(tmp_path, "w") as f:
            json.dump(picks, f, indent=2, default=core._json_default_numpy_safe)
        os.replace(tmp_path, PICKS_FILE)
    except Exception as e:
        print(f"⚠️ Gagal menyimpan buy_on_strength_picks.json: {e}")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


def _resolve_active_pick(pick: dict) -> dict:
    """Re-check one still-ALIVE pick. Order matters, matches the backtest
    exactly: (1) on the FIRST day of holding only, check whether to
    tighten the SL (day0 close <=-2% from entry_ref); (2) THEN check SL
    (using whatever sl_pct is now active) before TP, same conservative
    same-day-ambiguity convention as every other lane; (3) else bump age
    and append the 6-day guidance note (display only)."""
    df = core.get_ohlcv_daily_from_db(pick["ticker"], limit=5)
    if df is None or df.empty:
        return pick

    entry_ref = pick["entry_ref_price"]
    today_low = float(df["Low"].iloc[-1])
    today_high = float(df["High"].iloc[-1])
    today_close = float(df["Close"].iloc[-1])
    today_date = df.index[-1].strftime("%Y-%m-%d")

    if today_date == pick.get("_last_checked_date"):
        return pick  # already resolved for today, avoid double-incrementing age

    # (1) day0 quick-SL tighten check -- ONLY on the first holding day
    if pick.get("age_days", 1) == 1 and not pick.get("sl_tightened"):
        day0_ret_pct = (today_close - entry_ref) / entry_ref * 100
        if day0_ret_pct <= DAY0_TIGHTEN_THRESH_PCT:
            pick["sl_pct"] = SL_PCT_TIGHT
            pick["sl_tightened"] = True
            pick["quick_sl_alert_pending"] = True  # cleared once displayed once, see /swing formatting

    sl_abs = entry_ref * (1 + pick["sl_pct"] / 100)
    tp_abs = entry_ref * (1 + TP_PCT / 100)

    # (2) SL before TP, same convention as every other lane
    if today_low <= sl_abs:
        pick["status"] = "SL_HIT"
        pick["resolved_date"] = today_date
        pick["resolved_ret_pct"] = round((sl_abs - entry_ref) / entry_ref * 100, 2)
    elif today_high >= tp_abs:
        pick["status"] = "TP_HIT"
        pick["resolved_date"] = today_date
        pick["resolved_ret_pct"] = round((tp_abs - entry_ref) / entry_ref * 100, 2)
    else:
        pick["age_days"] = pick.get("age_days", 1) + 1
        if pick["age_days"] > ALERT_MAX_AGE_DAYS:
            pick["status"] = "EXPIRED"
            pick["resolved_date"] = today_date
            pick["resolved_ret_pct"] = round((today_close - entry_ref) / entry_ref * 100, 2)

    pick["_last_checked_date"] = today_date
    return pick


def update_and_save_picks(new_candidates: list[dict]) -> list:
    """Merge today's qualifying candidates into the persisted pick history.
    Call once per nightly cycle, same pattern as every other /swing lane."""
    history = load_buy_on_strength_picks()

    active_tickers = set()
    for pick in history:
        if pick.get("status") == "ALIVE":
            try:
                pick = _resolve_active_pick(pick)
            except Exception as e:
                print(f"⚠️ Buy On Strength (BOS): gagal resolve pick {pick.get('ticker')}: {e}")
            active_tickers.add(pick["ticker"])

    for cand in new_candidates:
        if cand["ticker"] in active_tickers:
            continue
        history.append(cand)
        active_tickers.add(cand["ticker"])

    save_buy_on_strength_picks(history)
    return history
