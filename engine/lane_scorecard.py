"""
engine/lane_scorecard.py — MBSS v2 (2026-10-22).

HINDARI / warning verdicts for lane picks and for single-stock lookups, from the 2026-10-22 research chain (memory:
project_swing_anti_thesis_backtest_2026_10_22, project_lane_gate_inversion_backtest_2026_10_22,
project_fundamental_avoid_test_2026_10_22). Everything is evaluated on the SIGNAL-DAY OHLCV bars + the IHSG regime phase of that day,
exactly how the backtests flagged trades, and is fail-open: missing data / regime fetch failure -> no verdict (never hides a pick
because something could not be computed).

HINDARI (pick is hidden from /swing, /rank, /bsjp; /check shows the label) — only where the out-of-sample expectation is <= 0 or the
rule was explicitly agreed:
  MACD-confirm : EXT  (far above SMA20 or near the 60d high)      | SPIKE_UP (1-day +12% on >=3.5x volume)
  VCP          : regime BEARISH_GRIND at signal day
  OSB-v2       : regime BEARISH_GRIND at signal day
  BSJP-L2      : SPIKE_UP at D0 (the pump lane's watchlist day)
  every lane except OSB-v2 : DROP_DN (down >=8% today and >=10% in 5 days) while the regime is NEUTRAL_BULLISH
  no lane (plain /check lookup) : regime CRASH_FAST while NOT oversold (macd_line_pct > -5%) or a red candle
Warnings only (⚠️, no hiding): EXT for BOW/BOS/VCP/OSB-v2, SPIKE_UP for lanes without a HINDARI on it.
BOW, BOS and BSJP-L1 have no HINDARI of their own (fundamentals were tested and are NOT used: no effect found).

EXT cutoffs are per-lane P75 of dist_sma20 and dd_high60 measured on the EARLY half of each lane's backtest population (derived
2026-10-22; re-derive with the live resolutions once enough exist).
"""
from __future__ import annotations

import datetime

import numpy as np
import pandas as pd

import engine.legacy_core as core

# lane -> (dist_sma20 P75 %, dd_high60 P75 %) from the early half of each lane's population
EXT_CUTOFFS = {
    "BOW": (-0.950, -9.502),
    "BOS": (5.486, -16.667),
    "VCP": (12.001, -1.149),
    "MACD-confirm": (7.156, -2.614),
    "OSB-v2": (-12.773, -36.842),
}
SPIKE_RET1D_MIN = 12.0
SPIKE_VOLRATIO_MIN = 3.5
DROP_RET1D_MAX = -8.0
DROP_RET5D_MAX = -10.0
CRASH_NOT_OVERSOLD_MACD_MIN = -5.0   # macd_line_pct (SMA12-SMA26 as % of price) above this = not oversold

LANE_OF_RANK_KEY = {"BOW": "BOW", "OSB": "OSB-v2", "VCP": "VCP", "MACD": "MACD-confirm", "BOS": "BOS"}

_cache: dict = {}
_reg: dict = {"day": None, "series": None}


def signal_features(ticker: str, date: str | None = None) -> dict | None:
    """Features at the last bar on/before `date` (None = latest bar). Same definitions as the backtest panel."""
    try:
        df = core.get_ohlcv_daily_from_db(ticker, 500)  # latest N bars: needs depth for old signal dates (live picks are <=20d old)
        if df is None or df.empty:
            return None
        if date:
            df = df[df.index <= pd.Timestamp(date)]
        if len(df) < 65:
            return None
        cl, hi, op, vo = df["Close"], df["High"], df["Open"], df["Volume"]
        sma12, sma20, sma26 = cl.rolling(12).mean(), cl.rolling(20).mean(), cl.rolling(26).mean()
        feats = {
            "date": df.index[-1].strftime("%Y-%m-%d"),
            "dist_sma20": float((cl.iloc[-1] / sma20.iloc[-1] - 1) * 100),
            "dd_high60": float((cl.iloc[-1] / hi.iloc[-60:].max() - 1) * 100),
            "ret_1d": float((cl.iloc[-1] / cl.iloc[-2] - 1) * 100),
            "ret_5d": float((cl.iloc[-1] / cl.iloc[-6] - 1) * 100),
            "vol_ratio": float(vo.iloc[-1] / vo.iloc[-20:].mean()) if vo.iloc[-20:].mean() > 0 else float("nan"),
            "macd_line_pct": float((sma12.iloc[-1] - sma26.iloc[-1]) / cl.iloc[-1] * 100),
            "green": bool(cl.iloc[-1] > op.iloc[-1]),
        }
        return None if any(isinstance(v, float) and np.isnan(v) for v in feats.values()) else feats
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ lane_scorecard: fitur {ticker} gagal (diabaikan): {e}")
        return None


def regime_phase_on(date: str) -> str | None:
    """IHSG regime phase on/before `date` (one yfinance fetch per calendar day per process). None on failure."""
    try:
        today = datetime.date.today().isoformat()
        if _reg["day"] != today or _reg["series"] is None:
            import engine.ihsg_regime as ihsg_regime
            hist = core.get_yf_ticker("^JKSE").history(period="max", timeout=30)
            reg = ihsg_regime._build_regime(hist)
            idx = pd.to_datetime(reg["date"]).dt.tz_localize(None).dt.normalize()
            _reg["series"] = pd.Series(reg["regime_phase"].values, index=idx).sort_index()
            _reg["day"] = today
        s = _reg["series"]
        s = s[s.index <= pd.Timestamp(date)]
        return str(s.iloc[-1]) if len(s) else None
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ lane_scorecard: regime gagal (diabaikan): {e}")
        return None


def evaluate(lane: str | None, ticker: str, date: str | None = None) -> dict:
    """{"hindari": [reasons], "warn": [reasons]} for `ticker` as a pick of `lane` (None = plain lookup) at signal day `date`."""
    key = (lane, ticker, date, datetime.date.today().isoformat())
    if key in _cache:
        return _cache[key]
    out = {"hindari": [], "warn": []}
    f = signal_features(ticker, date)
    if f is None:
        _cache[key] = out
        return out
    phase = regime_phase_on(f["date"])
    spike = f["ret_1d"] >= SPIKE_RET1D_MIN and f["vol_ratio"] >= SPIKE_VOLRATIO_MIN
    drop = f["ret_1d"] <= DROP_RET1D_MAX and f["ret_5d"] <= DROP_RET5D_MAX
    cut = EXT_CUTOFFS.get(lane)
    ext = bool(cut) and (f["dist_sma20"] >= cut[0] or f["dd_high60"] >= cut[1])
    R_EXT = "sudah jauh dari MA20 / dekat high 60 hari"
    R_SPIKE = f"lonjakan 1 hari {f['ret_1d']:+.0f}% dengan volume {f['vol_ratio']:.1f}x"
    R_DROP = f"jatuh {f['ret_1d']:+.0f}% hari ini dan {f['ret_5d']:+.0f}% dalam 5 hari di pasar normal"
    R_REG = f"regime {phase}"

    if lane == "MACD-confirm":
        if ext: out["hindari"].append(R_EXT)
        if spike: out["hindari"].append(R_SPIKE)
    elif lane in ("VCP", "OSB-v2"):
        if phase == "BEARISH_GRIND": out["hindari"].append(R_REG)
        if ext: out["warn"].append(R_EXT)
    elif lane in ("BOW", "BOS"):
        if ext: out["warn"].append(R_EXT)
    elif lane == "BSJP-L2":
        if spike: out["hindari"].append(R_SPIKE)
    if spike and lane not in ("MACD-confirm", "BSJP-L2"):
        out["warn"].append(R_SPIKE + " (rawan kena SL ketat)")
    if drop and phase == "NEUTRAL_BULLISH" and lane != "OSB-v2":
        out["hindari"].append(R_DROP)
    if lane is None and phase == "CRASH_FAST" and (f["macd_line_pct"] > CRASH_NOT_OVERSOLD_MACD_MIN or not f["green"]):
        out["hindari"].append("CRASH_FAST tapi tidak oversold / candle merah")
    _cache[key] = out
    return out


def is_hindari(lane: str | None, ticker: str, date: str | None = None) -> bool:
    return bool(evaluate(lane, ticker, date)["hindari"])


def partition(lane: str, picks: list, date_key: str = "trigger_date") -> tuple[list, list]:
    """(kept, hidden) where hidden = [(lane, ticker, reasons)]. Fail-open per pick."""
    kept, hidden = [], []
    for p in picks:
        try:
            ev = evaluate(lane, p["ticker"], p.get(date_key))
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ lane_scorecard: {lane} {p.get('ticker')} gagal (pick tetap tampil): {e}")
            ev = {"hindari": []}
        if ev["hindari"]:
            hidden.append((lane, p["ticker"], ev["hindari"]))
        else:
            kept.append(p)
    return kept, hidden


def partition_candidates(candidates: list) -> tuple[list, list]:
    """For engine.capital_rank candidates ({lane_key, pick, ...})."""
    kept, hidden = [], []
    for c in candidates:
        lane = LANE_OF_RANK_KEY.get(c.get("lane_key"))
        p = c["pick"]
        try:
            ev = evaluate(lane, p["ticker"], p.get("trigger_date")) if lane else {"hindari": []}
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ lane_scorecard: rank {p.get('ticker')} gagal (pick tetap tampil): {e}")
            ev = {"hindari": []}
        if ev["hindari"]:
            hidden.append((lane, p["ticker"], ev["hindari"]))
        else:
            kept.append(c)
    return kept, hidden


def hidden_footnote(hidden: list, max_items: int = 8) -> str:
    """One closing line for the pick-list commands; '' when nothing was hidden."""
    if not hidden:
        return ""
    items = "; ".join(f"{t} ({lane}: {', '.join(r)})" for lane, t, r in hidden[:max_items])
    more = f" +{len(hidden) - max_items} lainnya" if len(hidden) > max_items else ""
    return f"🚫 {len(hidden)} pick disembunyikan (HINDARI): {items}{more}."
