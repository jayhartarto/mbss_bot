"""
engine/vcp_pillar.py — MBSS v2 (2026-09-21, new swing-trade lane)

"Buy the breakout after a volatility squeeze." Second swing pillar
alongside engine/buy_on_weakness.py — confirmed 0% day-level signal
overlap with BOW (BOW needs %B<=0.35 near the lower band, VCP needs
near-high; structurally near-mutually-exclusive). Full research trail
(ATR/liquidity/RS20d/foreign-flow tuning, TP1/TP2/TP3 confidence-tier
derivation, horizon-vs-TP tradeoff analysis) lives in memory
`project_vcp_tp1_tp2_tp3_sl_2026_09_21.md` (read the "REVISI FINAL,
DIKUNCI" section — everything above it is superseded research history)
and the sibling files linked from `project_swing_unification_strategy_
2026_09_21.md`. Do not re-derive the thresholds below without re-reading
that trail first.

Entry gate (all features causal, no lookahead, from bars up to and
including the trigger day):
  - vcp_pass: 3-stage range contraction (15d>10d>5d, 5d<15d*0.7) + volume
    dry-up (5d avg < 15d avg) + near 20d high (within -10%)
  - RSI(Wilder) >= 65
  - ATR%-20d <= 10.0
  - value_traded_20d_avg in [0.15B, 30B) IDR
  - foreign flow NOT net-sell (10d FF net / 10d volume > 0)
  - RS20d (vs IHSG) <= 50 (VCP's own population runs much hotter than
    BOW's, so this ceiling is far looser than BOW's RS20_MAX=10)
  - MACD histogram (SMA12-SMA26/EMA9, % of price) in [-0.5, 2.75) —
    excludes both the deep-negative zone (weak, no real edge) and the
    thick-positive tail (extreme asymmetric bet, high SL-rate)

Exit — IMPORTANT, do not "simplify" this into a laddered sell without
re-reading the research: TP1/TP2/TP3 are THREE SEPARATE, INDEPENDENT
confidence-tier targets (BSJP-style display), NOT sequential partial
exits. Each was backtested as its own single-full-exit-at-first-touch
strategy:
  TP1 = entry x 1.025 (~72% historical touch rate within 12 trading days)
  TP2 = entry x 1.05  (~57% touch rate)
  TP3 = entry x 1.07  (~48% touch rate)
  SL  = entry x 0.90
Production pick-tracking resolves against SL and TP1 (TP1 = the
highest-confidence, fastest-resolving target — median ~2 trading days to
touch) as the PRIMARY lifecycle-ending events; TP2/TP3 touches are tracked
as informational upside flags only, same pattern as buy_on_weakness.py's
tp1_touched (there TP1 is informational and TP2 ends the alert; here it's
inverted because VCP's TP1 is the reliable one and TP2/TP3 are the
long-tail upside, not because the underlying mechanic is different).
Horizon: EXPIRED after 12 trading days if neither SL nor TP1 touched
(matches BOW's real median hold, NOT a 90-day "let it ride" cap — see
research file for why the shorter cap was chosen).

CAVEAT carried over from research: VCP's edge is structurally asymmetric
(right-tail-driven for TP2/TP3, unlike BOW's steady high-win-rate profile)
-- display touch-RATES per tier, never a single blended "expected return"
number, that number is deceptively small/misleading due to the TP-vs-SL
size asymmetry (see research file's worked example).
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
# Constants (all validated in the research trail referenced above)
# ---------------------------------------------------------------------------
FF_DB_PATH = os.path.join(core.PROJECT_ROOT, "research", "foreign_flow_2y.sqlite")
PICKS_FILE = os.path.join(core.PROJECT_ROOT, "vcp_pillar_picks.json")

MIN_HISTORY = 100

RSI_MIN = 65.0
ATR20_MAX = 10.0
LIQ_MIN = 0.15e9
LIQ_MAX = 30e9
RS20_MAX = 50.0
MACD_PCT_MIN = -0.5
MACD_PCT_MAX = 2.75

TP1_PCT, TP1_TOUCH_RATE = 2.5, 72.4
TP2_PCT, TP2_TOUCH_RATE = 5.0, 56.7
TP3_PCT, TP3_TOUCH_RATE = 7.0, 47.7
SL_PCT = 10.0
ALERT_MAX_AGE_DAYS = 12


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

    rsi = core.calculate_rsi(close)

    sma_fast = close.rolling(12).mean()
    sma_slow = close.rolling(26).mean()
    macd_line = sma_fast - sma_slow
    signal_line = macd_line.ewm(span=9, adjust=False).mean()
    macd_hist = macd_line - signal_line
    macd_hist_pct = macd_hist / close * 100

    prev_close = close.shift(1)
    tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
    atr_pct_20d = (tr / close * 100).rolling(20).mean()

    value_traded_20d_avg = (close * volume).rolling(20).mean()

    r1 = (high.rolling(15).max() - low.rolling(15).min()) / close
    r2 = (high.rolling(10).max() - low.rolling(10).min()) / close
    r3 = (high.rolling(5).max() - low.rolling(5).min()) / close
    vol5 = volume.rolling(5).mean()
    vol15 = volume.rolling(15).mean()
    high20 = high.rolling(20).max()

    r1_last, r2_last, r3_last = r1.iloc[-1], r2.iloc[-1], r3.iloc[-1]
    vcp_pass = False
    if pd.notna(r1_last) and r1_last > 0:
        tightening = r1_last > r2_last > r3_last and r3_last < r1_last * 0.7
        vol_dryup = bool(vol5.iloc[-1] < vol15.iloc[-1])
        h20 = high20.iloc[-1]
        near_high = bool(h20 and h20 > 0 and (close.iloc[-1] - h20) / h20 > -0.10)
        vcp_pass = bool(tightening and vol_dryup and near_high)

    ret20d = close.pct_change(20) * 100

    return {
        "close": float(close.iloc[-1]),
        "rsi": float(rsi.iloc[-1]) if pd.notna(rsi.iloc[-1]) else None,
        "macd_hist_pct": float(macd_hist_pct.iloc[-1]) if pd.notna(macd_hist_pct.iloc[-1]) else None,
        "atr_pct_20d": float(atr_pct_20d.iloc[-1]) if pd.notna(atr_pct_20d.iloc[-1]) else None,
        "value_traded_20d_avg": float(value_traded_20d_avg.iloc[-1]) if pd.notna(value_traded_20d_avg.iloc[-1]) else None,
        "vcp_pass": vcp_pass,
        "stock_ret_20d": float(ret20d.iloc[-1]) if pd.notna(ret20d.iloc[-1]) else None,
        "latest_date": df.index[-1].strftime("%Y-%m-%d"),
    }


def _load_ff_lookup() -> pd.DataFrame:
    """Same 2-year foreign-flow archive as buy_on_weakness.py -- REUSE,
    don't re-fetch."""
    if not os.path.exists(FF_DB_PATH):
        return pd.DataFrame(columns=["ticker", "date", "foreign_net"])
    conn = sqlite3.connect(FF_DB_PATH)
    try:
        ff = pd.read_sql("SELECT ticker, date, foreign_net FROM foreign_flow_daily", conn)
    finally:
        conn.close()
    ff["date"] = pd.to_datetime(ff["date"])
    return ff


def _ff_net10d_pct(ticker: str, df: pd.DataFrame, ff_lookup: pd.DataFrame) -> float | None:
    last10_dates = df.index[-10:]
    ffg = ff_lookup[(ff_lookup.ticker == ticker) & (ff_lookup.date.isin(last10_dates))]
    if ffg.empty:
        return None
    net_sum = ffg["foreign_net"].sum()
    vol_sum = df["Volume"].tail(10).sum()
    if not vol_sum:
        return None
    return float(net_sum / vol_sum * 100)


def evaluate_ticker(ticker: str, ff_lookup: pd.DataFrame, rs20_ihsg: float | None) -> dict | None:
    """Returns a candidate dict if `ticker` qualifies TODAY, else None."""
    df = core.get_ohlcv_daily_from_db(ticker, limit=150)
    feats = _compute_latest_features(df)
    if feats is None:
        return None
    if not feats["vcp_pass"]:
        return None
    if feats["rsi"] is None or feats["rsi"] < RSI_MIN:
        return None
    if feats["atr_pct_20d"] is None or feats["atr_pct_20d"] > ATR20_MAX:
        return None
    if feats["value_traded_20d_avg"] is None or not (LIQ_MIN <= feats["value_traded_20d_avg"] < LIQ_MAX):
        return None
    if feats["macd_hist_pct"] is None or not (MACD_PCT_MIN <= feats["macd_hist_pct"] < MACD_PCT_MAX):
        return None

    ff_net10d_pct = _ff_net10d_pct(ticker, df, ff_lookup)
    if ff_net10d_pct is not None and ff_net10d_pct <= 0:
        return None
    # missing FF = neutral, never penalize (house convention) -> pass through

    if rs20_ihsg is not None and feats["stock_ret_20d"] is not None:
        rs_20d = feats["stock_ret_20d"] - rs20_ihsg
        if rs_20d > RS20_MAX:
            return None

    entry_ref = feats["close"]
    sl_price = round(entry_ref * (1 - SL_PCT / 100))
    tp1_price = round(entry_ref * (1 + TP1_PCT / 100))
    tp2_price = round(entry_ref * (1 + TP2_PCT / 100))
    tp3_price = round(entry_ref * (1 + TP3_PCT / 100))

    return {
        "ticker": ticker,
        "trigger_date": feats["latest_date"],
        "status": "ALIVE",
        "tp2_touched": False,
        "tp3_touched": False,
        "age_days": 1,
        "entry_ref_price": entry_ref,
        "sl_price": sl_price,
        "tp1_price": tp1_price, "tp1_touch_rate": TP1_TOUCH_RATE,
        "tp2_price": tp2_price, "tp2_touch_rate": TP2_TOUCH_RATE,
        "tp3_price": tp3_price, "tp3_touch_rate": TP3_TOUCH_RATE,
        "resolved_date": None,
        "resolved_ret_pct": None,
    }


def compute_vcp_candidates(tickers: list[str]) -> list[dict]:
    """Nightly entry point: evaluate every ticker in `tickers`, return the
    list that qualifies today (does NOT touch the picks-history file --
    call update_and_save_picks() with the result to persist/merge)."""
    ff_lookup = _load_ff_lookup()
    rs20_ihsg = market_engine.get_ihsg_return_nd(20)
    candidates = []
    for t in tickers:
        try:
            c = evaluate_ticker(t, ff_lookup, rs20_ihsg)
        except Exception as e:
            print(f"⚠️ VCP pillar: gagal evaluasi {t}: {e}")
            continue
        if c is not None:
            candidates.append(c)
    return candidates


# ---------------------------------------------------------------------------
# Pick-history persistence -- same 3-layer safety pattern as
# buy_on_weakness.py (REUSE core's existing generic helpers).
# ---------------------------------------------------------------------------
def load_vcp_picks() -> list:
    if not os.path.exists(PICKS_FILE):
        return []
    try:
        with open(PICKS_FILE) as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ Gagal membaca vcp_pillar_picks.json: {e}")
        return []


def save_vcp_picks(picks: list):
    tmp_path = PICKS_FILE + ".tmp"
    try:
        core._backup_json_file_daily(PICKS_FILE)
        with open(tmp_path, "w") as f:
            json.dump(picks, f, indent=2, default=core._json_default_numpy_safe)
        os.replace(tmp_path, PICKS_FILE)
    except Exception as e:
        print(f"⚠️ Gagal menyimpan vcp_pillar_picks.json: {e}")
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass


def _resolve_active_pick(pick: dict) -> dict:
    """Re-check one still-ALIVE pick against fresh OHLCV: SL first
    (conservative, same-day-ambiguity convention used throughout this
    project's research), then TP1 (the reliable, fast-resolving target --
    ENDS the alert, unlike TP2/TP3 which are tracked as informational
    upside flags only), else bump age and check TP2/TP3 touch flags."""
    df = core.get_ohlcv_daily_from_db(pick["ticker"], limit=150)
    feats = _compute_latest_features(df)
    if feats is None:
        return pick  # can't evaluate today (e.g. delisted/no data) -- leave as-is

    entry_ref = pick["entry_ref_price"]
    today_low = float(df["Low"].iloc[-1])
    today_high = float(df["High"].iloc[-1])
    today_date = feats["latest_date"]

    if today_date == pick.get("_last_checked_date"):
        return pick  # already resolved for today, avoid double-incrementing age

    if today_low <= pick["sl_price"]:
        pick["status"] = "SL_HIT"
        pick["resolved_date"] = today_date
        pick["resolved_ret_pct"] = round((pick["sl_price"] - entry_ref) / entry_ref * 100, 2)
    elif today_high >= pick["tp1_price"]:
        pick["status"] = "TP1_HIT"
        pick["resolved_date"] = today_date
        pick["resolved_ret_pct"] = round((pick["tp1_price"] - entry_ref) / entry_ref * 100, 2)
        # informational only, doesn't change status further
        if today_high >= pick["tp2_price"]:
            pick["tp2_touched"] = True
        if today_high >= pick["tp3_price"]:
            pick["tp3_touched"] = True
    else:
        if today_high >= pick["tp2_price"]:
            pick["tp2_touched"] = True
        if today_high >= pick["tp3_price"]:
            pick["tp3_touched"] = True
        pick["age_days"] = pick.get("age_days", 1) + 1
        if pick["age_days"] > ALERT_MAX_AGE_DAYS:
            pick["status"] = "EXPIRED"
            pick["resolved_date"] = today_date
            today_close = float(df["Close"].iloc[-1])
            pick["resolved_ret_pct"] = round((today_close - entry_ref) / entry_ref * 100, 2)

    pick["_last_checked_date"] = today_date
    return pick


def update_and_save_picks(new_candidates: list[dict]) -> list:
    """Merge today's qualifying candidates into the persisted pick history:
    re-evaluate every still-active existing pick (SL/TP1/age), then append
    a fresh entry ONLY for tickers that don't already have an active pick.
    Call once per nightly cycle."""
    history = load_vcp_picks()

    active_tickers = set()
    for pick in history:
        if pick.get("status") == "ALIVE":
            try:
                pick = _resolve_active_pick(pick)
            except Exception as e:
                print(f"⚠️ VCP pillar: gagal resolve pick {pick.get('ticker')}: {e}")
            active_tickers.add(pick["ticker"])

    for cand in new_candidates:
        if cand["ticker"] in active_tickers:
            continue
        history.append(cand)
        active_tickers.add(cand["ticker"])

    save_vcp_picks(history)
    return history
