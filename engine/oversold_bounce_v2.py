"""
engine/oversold_bounce_v2.py — MBSS v2 (2026-10-06), new lane inside /bow.

"Buy the deep-oversold bounce during a confirmed IHSG bear regime." Full
design spec, backtest numbers (OOS across the 2024-25 and 2026 bear
episodes), and the decision trail that led here (RSI absolute band vs
cross-sectional percentile, mandatory green+not-outperform-IHSG trigger,
IHSG dd_100 gate shape, FF ranking booster) live in memory
project_oversold_bounce_v2_locked_design_2026_10_05.md — do not re-derive
the thresholds below without reading that trail first. REPLACES the old
`entry_pagi_oversold_bounce_gate_pass` lane that used to live inside
/allsetup and /pingpong (RSI<=p10 cross-sectional, no trigger, no IHSG
gate) — that lane is retired, this is its successor.

Gate (all conditions required, computed causally from bars up to and
including the trigger day):
  - IHSG macro regime: dd_100 (close vs trailing 100-trading-day high) <=
    -10% ("blindspot" test confirmed -10% to -15% is GOOD/OOS-consistent,
    not a blindspot -- the earlier -15% boundary was based on a coarser
    aggregate that hid this). Lane produces NOTHING outside this regime
    (deliberately FROZEN for -0% to -10%, not just "not deep enough yet" --
    that range contains a confirmed-bad sub-zone).
  - RSI14 (Wilder, ABSOLUTE value, NOT cross-sectional percentile): 20 <=
    RSI14 < 40.
  - price_vs_sma20_pct <= -10% (absolute threshold).
  - Liquidity: value_traded (20d avg) >= 1B IDR.
  - Trigger (MANDATORY, not a booster -- without it this RSI band is
    mediocre-to-losing): close > open (green candle) today AND stock's own
    close-to-close day return <= IHSG's own close-to-close day return that
    same day (rallying WITH the market-wide move, not an idiosyncratic
    spike).

Ranking booster (NOT a gate): foreign_net_ratio_5d > 0 preferred (OOS-
validated, win 79-84% both episodes vs 50-74% when negative) -- sort
candidates by this descending, never exclude on it.

Exit reference (informational display only -- NO auto-entry, same as the
old lane): TP1 (+5%, conservative, touch flag only, does NOT end the
alert's lifecycle) / TP2 (+10%, main resolution) / SL (-20%, do NOT tighten
below this -- tested at -15%, one of the two OOS episodes went negative).
Max hold 20 trading days, then EXPIRED (mark-to-market).

CAVEAT carried over from the locked design: this is a pure stock-level
technical gate with an IHSG regime pre-filter -- treat backtest hit-rates
as upper bounds in the bearish regime they were validated in, not an
unconditional forward guarantee.
"""
from __future__ import annotations

import json
import os

import pandas as pd

from engine import legacy_core as core
import engine.market as market_engine
import engine.buy_on_weakness as bow_engine  # reuse _load_ff_lookup, same FF archive

PICKS_FILE = os.path.join(core.PROJECT_ROOT, "oversold_bounce_v2_picks.json")

MIN_HISTORY = 30
RSI_LO, RSI_HI = 20.0, 40.0
DIST_SMA20_MAX = -10.0
LIQ_FLOOR = 1e9
IHSG_DD100_GATE = -10.0
FF_WINDOW = 5

TP1_PCT = 5.0   # conservative, informational touch-flag only
TP2_PCT = 10.0  # main resolution
SL_PCT = -20.0  # do NOT tighten below this, see docstring
ALERT_MAX_AGE_DAYS = 20  # trading days, matches the TP/SL touch-rate backtest horizon


def _compute_latest_features(df: pd.DataFrame) -> dict | None:
    """df: output of core.get_ohlcv_daily_from_db (index=date, columns
    Open/High/Low/Close/Volume, ascending)."""
    if df is None or len(df) < MIN_HISTORY:
        return None

    close = df["Close"]; open_ = df["Open"]; volume = df["Volume"]

    rsi_series = core.calculate_rsi(close)
    sma20 = close.rolling(20).mean()
    vs_sma20_pct = (close / sma20 - 1) * 100
    value_traded_20d = (close * volume).rolling(20).mean()
    ret_1d_pct = close.pct_change() * 100

    rsi_last = rsi_series.iloc[-1]
    vs_sma20_last = vs_sma20_pct.iloc[-1]
    value_traded_last = value_traded_20d.iloc[-1]
    ret_1d_last = ret_1d_pct.iloc[-1]

    if pd.isna(rsi_last) or pd.isna(vs_sma20_last) or pd.isna(value_traded_last) or pd.isna(ret_1d_last):
        return None

    return {
        "close": float(close.iloc[-1]),
        "rsi14": float(rsi_last),
        "vs_sma20_pct": float(vs_sma20_last),
        "value_traded_20d": float(value_traded_last),
        "ret_1d_pct": float(ret_1d_last),
        "is_green": bool(close.iloc[-1] > open_.iloc[-1]),
        "latest_date": df.index[-1].strftime("%Y-%m-%d"),
    }


def _ff_ratio_5d(ticker: str, df: pd.DataFrame, ff_lookup: pd.DataFrame) -> float | None:
    last_dates = df.index[-FF_WINDOW:]
    ffg = ff_lookup[(ff_lookup.ticker == ticker) & (ff_lookup.date.isin(last_dates))]
    if ffg.empty:
        return None
    net_sum = ffg["foreign_net"].sum()
    vol_sum = df["Volume"].tail(FF_WINDOW).sum()
    if not vol_sum:
        return None
    return float(net_sum / vol_sum * 100)


def _ff_pos_days_5(ticker: str, df: pd.DataFrame, ff_lookup: pd.DataFrame) -> int | None:
    """Count of the last 5 trading days with positive net foreign buy
    (foreign_net/volume > 0) -- booster/exclusion signal validated
    2026-10-07 (memory project_ff_booster_light_sweep_2026_10_07.md +
    project_bow_osb_absolute_gate_confirm_2026_10_07.md, within THIS
    lane's own gate-survivor population): >=3/5 positive days lifts win
    75.2%->80.7% (wlb 70.9->75.1%); <=1/5 positive days degrades it to
    68.3% (wlb->58.8%), confirmed as a real exclusion (not just noise).
    Deliberately a pure day-count, not a magnitude/z-score -- this was
    the feature shape that actually worked best for this specific lane
    (see the sweep memory for why other shapes were weaker here).
    Returns None (not 0) when there's no FF data for any of the last 5
    days -- missing = neutral, caller must never exclude on None."""
    last5_dates = df.index[-FF_WINDOW:]
    ffg = ff_lookup[(ff_lookup.ticker == ticker) & (ff_lookup.date.isin(last5_dates))]
    if ffg.empty:
        return None
    vol_by_date = df["Volume"].reindex(last5_dates)
    net_by_date = ffg.set_index("date")["foreign_net"].reindex(last5_dates)
    valid = vol_by_date.notna() & net_by_date.notna() & (vol_by_date > 0)
    if not valid.any():
        return None
    net_ratio = net_by_date[valid] / vol_by_date[valid]
    return int((net_ratio > 0).sum())


def evaluate_ticker(ticker: str, ff_lookup: pd.DataFrame, ihsg_ret_1d_today: float | None) -> dict | None:
    """Returns a candidate dict if `ticker` qualifies TODAY, else None.
    Caller is responsible for the IHSG dd_100 regime gate (checked once for
    the whole batch, not per-ticker -- see compute_oversold_bounce_v2_candidates)."""
    if ihsg_ret_1d_today is None:
        return None  # trigger is mandatory and needs IHSG's own day return -- can't evaluate without it

    df = core.get_ohlcv_daily_from_db(ticker, limit=250)
    feats = _compute_latest_features(df)
    if feats is None:
        return None

    if not (RSI_LO <= feats["rsi14"] < RSI_HI):
        return None
    if feats["vs_sma20_pct"] > DIST_SMA20_MAX:
        return None
    if feats["value_traded_20d"] < LIQ_FLOOR:
        return None
    if not (feats["is_green"] and feats["ret_1d_pct"] <= ihsg_ret_1d_today):
        return None  # mandatory trigger, see docstring

    ff_ratio5 = _ff_ratio_5d(ticker, df, ff_lookup)  # missing = neutral, never penalize (house convention)

    # EXCLUSION (2026-10-07, validated -- see _ff_pos_days_5 docstring):
    # <=1/5 recent positive-FF days is a real, confirmed quality drop
    # within this lane's own gate survivors. Missing (None) NEVER
    # excludes -- only a confirmed low count does.
    ff_pos_days5 = _ff_pos_days_5(ticker, df, ff_lookup)
    if ff_pos_days5 is not None and ff_pos_days5 <= 1:
        return None

    entry_ref = feats["close"]
    sl_price = round(entry_ref * (1 + SL_PCT / 100))
    tp1_price = round(entry_ref * (1 + TP1_PCT / 100))
    tp2_price = round(entry_ref * (1 + TP2_PCT / 100))

    return {
        "ticker": ticker,
        "trigger_date": feats["latest_date"],
        "status": "ALIVE",
        "tp1_touched": False,
        "age_days": 1,
        "entry_ref_price": entry_ref,
        "sl_price": sl_price,
        "tp1_price": tp1_price,
        "tp2_price": tp2_price,
        "ff_ratio_5d": ff_ratio5,
        "ff_pos_days_5": ff_pos_days5,
        # BOOSTER (2026-10-07, validated): >=3/5 recent positive-FF days,
        # a re-rank tag only -- never excludes/gates, house convention
        # (booster, not hard gate). See _ff_pos_days_5 docstring.
        "ff_priority": bool(ff_pos_days5 is not None and ff_pos_days5 >= 3),
        "rsi14_at_trigger": round(feats["rsi14"], 1),
        "resolved_date": None,
        "resolved_ret_pct": None,
    }


def compute_oversold_bounce_v2_candidates(tickers: list[str]) -> list[dict]:
    """Nightly entry point. Does NOT touch the picks-history file -- call
    update_and_save_picks() with the result to persist/merge."""
    ihsg_dd100 = market_engine.get_ihsg_drawdown_100d()
    if ihsg_dd100 is None or ihsg_dd100 > IHSG_DD100_GATE:
        return []  # regime frozen (not deep enough) or unknown -- lane deliberately produces nothing

    ihsg_ret_1d_today = market_engine.get_ihsg_return_today()
    ff_lookup = bow_engine._load_ff_lookup()
    candidates = []
    for t in tickers:
        try:
            c = evaluate_ticker(t, ff_lookup, ihsg_ret_1d_today)
        except Exception as e:
            print(f"⚠️ Oversold Bounce v2: gagal evaluasi {t}: {e}")
            continue
        if c is not None:
            candidates.append(c)
    return candidates


# ---------------------------------------------------------------------------
# Pick-history persistence -- same 3-layer safety pattern as buy_on_weakness.py
# (and daytrade_picks_history.json before it) -- reuse core's existing
# generic helpers rather than reimplementing them.
# ---------------------------------------------------------------------------
def load_oversold_bounce_v2_picks() -> list:
    if not os.path.exists(PICKS_FILE):
        return []
    try:
        with open(PICKS_FILE) as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ Gagal membaca oversold_bounce_v2_picks.json: {e}")
        return []


def save_oversold_bounce_v2_picks(picks: list):
    tmp_path = PICKS_FILE + ".tmp"
    try:
        core._backup_json_file_daily(PICKS_FILE)
        with open(tmp_path, "w") as f:
            json.dump(picks, f, indent=2, default=core._json_default_numpy_safe)
        os.replace(tmp_path, PICKS_FILE)
    except Exception as e:
        print(f"⚠️ Gagal menyimpan oversold_bounce_v2_picks.json: {e}")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


def _resolve_active_pick(pick: dict) -> dict:
    """Re-check one still-ALIVE pick: SL first (conservative, same-day-
    ambiguity convention used throughout this project), then TP2 (main
    resolution), else bump age and check TP1 (informational touch flag
    only, does not end the alert). Matches the exact mechanic validated in
    research/oversold_bounce_tp_sl_sweep_2026_10_05.py's touch-rate sweep."""
    df = core.get_ohlcv_daily_from_db(pick["ticker"], limit=5)
    if df is None or df.empty:
        return pick  # can't evaluate today (e.g. delisted/no data) -- leave as-is

    entry_ref = pick["entry_ref_price"]
    sl_abs = entry_ref * (1 + SL_PCT / 100)
    tp1_abs = entry_ref * (1 + TP1_PCT / 100)
    tp2_abs = entry_ref * (1 + TP2_PCT / 100)
    today_low = float(df["Low"].iloc[-1])
    today_high = float(df["High"].iloc[-1])
    today_close = float(df["Close"].iloc[-1])
    today_date = df.index[-1].strftime("%Y-%m-%d")

    if today_date == pick.get("_last_checked_date"):
        return pick  # already resolved for today, avoid double-incrementing age

    if today_low <= sl_abs:
        pick["status"] = "SL_HIT"
        pick["resolved_date"] = today_date
        pick["resolved_ret_pct"] = round((sl_abs - entry_ref) / entry_ref * 100, 2)
    elif today_high >= tp2_abs:
        pick["status"] = "TP2_HIT"
        pick["resolved_date"] = today_date
        pick["resolved_ret_pct"] = round((tp2_abs - entry_ref) / entry_ref * 100, 2)
    else:
        if today_high >= tp1_abs:
            pick["tp1_touched"] = True
        pick["age_days"] = pick.get("age_days", 1) + 1
        if pick["age_days"] > ALERT_MAX_AGE_DAYS:
            pick["status"] = "EXPIRED"
            pick["resolved_date"] = today_date
            pick["resolved_ret_pct"] = round((today_close - entry_ref) / entry_ref * 100, 2)

    pick["_last_checked_date"] = today_date
    return pick


def update_and_save_picks(new_candidates: list[dict]) -> list:
    """Merge today's qualifying candidates into the persisted pick history:
    re-evaluate every still-active existing pick (SL/TP2/age), then append
    a fresh entry ONLY for tickers that don't already have an active pick
    (one continuous alert per setup, not a daily re-fire). Call once per
    nightly cycle."""
    history = load_oversold_bounce_v2_picks()

    active_tickers = set()
    for pick in history:
        if pick.get("status") == "ALIVE":
            try:
                pick = _resolve_active_pick(pick)
            except Exception as e:
                print(f"⚠️ Oversold Bounce v2: gagal resolve pick {pick.get('ticker')}: {e}")
            active_tickers.add(pick["ticker"])

    for cand in new_candidates:
        if cand["ticker"] in active_tickers:
            continue
        history.append(cand)
        active_tickers.add(cand["ticker"])

    save_oversold_bounce_v2_picks(history)
    return history
