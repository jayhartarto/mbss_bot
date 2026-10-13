"""
engine/ihsg_regime.py — MBSS v2 (2026-10-13).

Production port of research/ihsg_regime_phases_2026_10_04.py's 5-phase
IHSG regime classifier (CRASH_FAST / BEARISH_GRIND / BOUNCE_OFF_LOW /
PULLBACK_IN_BOUNCE / NEUTRAL_BULLISH) -- see memory
project_ihsg_bearish_regime_bottom_dna_2026_10_04.md for the original
research trail and project_ret10d_wiring_prep_osbv2_liquidity_independence_2026_10_13.md
for why this needed to become a SHARED production module: OSB-v2 (engine/
oversold_bounce_v2.py) and the new ret_10d BOUNCE_OFF_LOW lane both need
IHSG regime state, and until now there were two uncoordinated definitions
floating around (OSB-v2's flat `dd_100<=-10%` snapshot gate vs this
richer phase/episode classifier, research-only). This module is now the
SINGLE SOURCE OF TRUTH for dd_100/bear_episode_id/regime_phase -- each
lane still applies its OWN validated condition on top of it (OSB-v2 stays
on dd_100<=-10% via engine/market.py's existing get_ihsg_drawdown_100d(),
unchanged; the new ret_10d lane gates on bear_episode_id>0 AND
regime_phase=='BOUNCE_OFF_LOW' via this module) -- do NOT force both
lanes onto identical thresholds, their own backtests don't support that
(see the wiring-prep memory: OSB-v2's edge concentrates in CRASH_FAST,
is NEGATIVE in BEARISH_GRIND, and BOUNCE_OFF_LOW is thin for OSB-v2 —
the two lanes are complementary, not redundant).

Phases (mutually exclusive, computed causally day by day — no lookahead):
  CRASH_FAST     : >=15% drawdown off trailing-100d high AND still falling
                    hard (20d return <= -8%)
  BEARISH_GRIND   : >=15% drawdown, not crashing, not bouncing (20d return
                    in (-8%, +2%)) -- slow bleed
  BOUNCE_OFF_LOW  : >=15% drawdown, recovering with real momentum (20d
                    return > +2%) AND at/near its own post-trough running
                    high (retrace_from_runmax > -5%)
  PULLBACK_IN_BOUNCE : >=15% drawdown, was bouncing but has pulled back
                    >=5% off its own post-trough running high
  NEUTRAL_BULLISH : drawdown < 15% off trailing-100d high (not in a bear
                    regime at all, includes OSB-v2's own "gray zone"
                    between -10% and -15%)

A "bear episode" starts at the first date dd_100 crosses <=-15% within a
peak-to-next-ATH cycle whose trough drawdown vs that peak was <=-15% (not
at the peak date itself -- avoids misclassifying the pre-crash rally tail
as part of the bear episode). "post-trough running high" tracked from a
causal per-episode swing state machine (resets on every new low, grows
with every new high) -- this is what lets PULLBACK_IN_BOUNCE trigger only
once a real bounce peak exists to pull back FROM.

Needs LONG history (multi-year) to correctly identify ATH-to-ATH cycles
and bear-episode boundaries -- unlike engine/market.py's
get_ihsg_drawdown_100d() (only needs 1y for a rolling-100-day high), this
fetches `period="max"` from yfinance. Heavier call, but cached per
calendar day (WIB) like every other IHSG helper in engine/market.py.
"""
from __future__ import annotations

import datetime

import numpy as np
import pandas as pd

from engine import legacy_core as core

_regime_cache = {"date": None, "result": None}


def _build_regime(hist: pd.DataFrame) -> pd.DataFrame:
    """hist: yfinance history DataFrame (DatetimeIndex, ascending, has
    'Close'). Same causal logic as research/ihsg_regime_phases_2026_10_04.py's
    build_ihsg_regime() -- kept in lockstep with that file, do not drift
    the two implementations apart; if the research script's logic changes,
    port the change here too."""
    df = pd.DataFrame({"close": hist["Close"].to_numpy()}, index=hist.index).reset_index()
    df.columns = ["date", "close"]
    df["ath"] = df["close"].cummax()
    df["high_100"] = df["close"].rolling(100, min_periods=20).max()
    df["dd_100"] = df["close"] / df["high_100"] - 1
    df["ret_20"] = df["close"].pct_change(20) * 100
    df["ret_10"] = df["close"].pct_change(10) * 100  # for BOS's rs_vs_ihsg_10d (engine/buy_on_strength.py)

    peaks = df[df["close"] == df["ath"]].copy()
    peak_dates = peaks["date"].tolist()
    peak_closes = peaks["close"].tolist()

    df["bear_episode_id"] = 0
    ep_id = 0
    for i in range(len(peak_dates)):
        p_date, p_close = peak_dates[i], peak_closes[i]
        end_date = peak_dates[i + 1] if i + 1 < len(peak_dates) else df["date"].iloc[-1] + pd.Timedelta(days=1)
        window = df[(df["date"] > p_date) & (df["date"] < end_date)]
        if window.empty:
            continue
        trough_dd = window["close"].min() / p_close - 1
        if trough_dd <= -0.15:
            real_start = window[window["dd_100"] <= -0.15]
            if real_start.empty:
                continue
            real_window = window.loc[real_start.index[0]:]
            ep_id += 1
            df.loc[real_window.index, "bear_episode_id"] = ep_id

    df["bearish_gate"] = df["bear_episode_id"] > 0

    df["retrace_from_runmax"] = np.nan
    for _, g in df[df["bear_episode_id"] > 0].groupby("bear_episode_id"):
        idx = g.index
        closes = df.loc[idx, "close"].to_numpy()
        retrace = np.empty(len(closes))
        cur_low = closes[0]
        cur_high_since_low = closes[0]
        for i, c in enumerate(closes):
            if c < cur_low:
                cur_low = c
                cur_high_since_low = c
            else:
                cur_high_since_low = max(cur_high_since_low, c)
            retrace[i] = (c / cur_high_since_low - 1) * 100
        df.loc[idx, "retrace_from_runmax"] = retrace

    def classify(row):
        if not row["bearish_gate"]:
            return "NEUTRAL_BULLISH"
        if pd.notna(row["retrace_from_runmax"]) and row["retrace_from_runmax"] <= -5 and row["ret_20"] > -8:
            return "PULLBACK_IN_BOUNCE"
        if row["ret_20"] <= -8:
            return "CRASH_FAST"
        if row["ret_20"] > 2 and (pd.isna(row["retrace_from_runmax"]) or row["retrace_from_runmax"] > -5):
            return "BOUNCE_OFF_LOW"
        return "BEARISH_GRIND"

    df["regime_phase"] = df.apply(classify, axis=1)
    return df


def get_ihsg_regime_today() -> dict | None:
    """Returns {date, dd_100 (fraction, e.g. -0.12), bear_episode_id (int,
    0 = not in a declared bear episode), regime_phase (str), ret_20} for
    the latest trading day, or None on fetch/compute failure. Cached per
    calendar day (WIB) -- same convention as every other IHSG helper in
    engine/market.py."""
    today_str = datetime.datetime.now(core.WIB).strftime("%Y-%m-%d")
    if _regime_cache["date"] == today_str and _regime_cache.get("result") is not None:
        return _regime_cache["result"]

    try:
        hist = core.get_yf_ticker("^JKSE").history(period="max", timeout=30)
        if len(hist) < 150:
            return None
        df = _build_regime(hist)
        last = df.iloc[-1]
        result = {
            "date": last["date"].strftime("%Y-%m-%d"),
            "dd_100": float(last["dd_100"]) if pd.notna(last["dd_100"]) else None,
            "bear_episode_id": int(last["bear_episode_id"]),
            "regime_phase": str(last["regime_phase"]),
            "ret_20": float(last["ret_20"]) if pd.notna(last["ret_20"]) else None,
            "ret_10": float(last["ret_10"]) if pd.notna(last["ret_10"]) else None,
        }
        _regime_cache["date"] = today_str
        _regime_cache["result"] = result
        return result
    except Exception as e:
        print(f"⚠️ Gagal hitung IHSG regime phase (engine/ihsg_regime.py): {e}")
        return None


def is_bounce_off_low() -> bool:
    """Convenience gate for the ret_10d crash-bounce lane: bear_episode_id>0
    AND regime_phase=='BOUNCE_OFF_LOW'. Returns False (never True) on any
    fetch/compute failure -- missing regime data means the lane produces
    nothing, same fail-safe convention as OSB-v2's dd_100 gate."""
    r = get_ihsg_regime_today()
    if r is None:
        return False
    return r["bear_episode_id"] > 0 and r["regime_phase"] == "BOUNCE_OFF_LOW"
