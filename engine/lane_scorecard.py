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


# ======================================================================================================================
# Tag levels (BELI-A / BELI-B / TAHAN / KELUAR / NETRAL / HINDARI). Replaces STRONG_BUY / BUY_ACCUMULATE / MIXED_SIGNALS in the
# views that adopt it (/check first, then /swing markers). Decided from the lane x regime backtest table
# (engine/lane_scorecard_table.json, built by research/build_lane_scorecard.py) -- NOT from the final score.
# ======================================================================================================================
import json
import os

TABLE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lane_scorecard_table.json")
MIN_N = 30
A_WIN, A_MEAN = 70.0, 1.0            # BELI-A: win >= 70% AND mean >= +1.0% AND 95% CI lower bound of the mean > 0
B_WIN, B_MEAN = 60.0, 0.5            # BELI-B: win >= 60% AND mean >= +0.5% ...
B_MEAN_LO_MIN = -1.0                  # BELI-B also needs the 95% CI lower bound of the mean above this (not clearly <= 0)
B_ALT_WIN, B_ALT_MEAN = 55.0, 2.0    # ... OR win >= 55% with mean >= +2.0% and CI lower bound > 0 (fat-tail lanes such as BOS)
# Trading days after the trigger in which a NEW entry is still sensible. Adjustable assumption: after this the pick is TAHAN.
ENTRY_WINDOW_DAYS = {"BOW": 2, "OSB-v2": 3, "VCP": 2, "MACD-confirm": 1, "BOS": 2}
EXIT_RECENT_DAYS = 3                 # a resolved pick shows KELUAR for this many calendar days after resolved_date
# Lanes whose level is NOT lowered by the EXT / spike warnings (the warning line still shows). User decision 2026-10-22: OSB-v2 picks
# flagged EXT still meet the BELI-A bar on their own numbers (late half: win 73.8%, mean +3.64%, n=84).
NO_DOWNGRADE_LANES = {"OSB-v2"}
TAG_STYLE = {"A": ("🟢", "BELI-A"), "B": ("🟡", "BELI-B"), "TAHAN": ("🔵", "TAHAN"), "KELUAR": ("🔴", "KELUAR"),
             "NETRAL": ("⚪", "NETRAL"), "HINDARI": ("🚫", "HINDARI")}
TAG_MARK = {"A": "🟢A", "B": "🟡B", "TAHAN": "🔵", "KELUAR": "🔴", "NETRAL": "⚪", "HINDARI": "🚫"}

_table_cache: dict = {}


def _table() -> dict:
    if "t" not in _table_cache:
        try:
            with open(TABLE_PATH, encoding="utf-8") as f:
                _table_cache["t"] = json.load(f).get("lanes", {})
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ lane_scorecard: tabel scorecard tidak terbaca (tag jadi NETRAL): {e}")
            _table_cache["t"] = {}
    return _table_cache["t"]


def cell_level(lane: str, phase: str | None) -> tuple[str, dict | None]:
    """('A'|'B'|'NETRAL'|'NA', cell) for a lane in a regime phase. 'NA' = no cell or n < MIN_N (evidence too thin)."""
    c = _table().get(lane, {}).get(phase) if phase else None
    if not c or c["n"] < MIN_N:
        return "NA", c
    lo = c.get("mean_lo")
    if c["win"] >= A_WIN and c["mean"] >= A_MEAN and lo is not None and lo > 0:
        return "A", c
    if (c["win"] >= B_WIN and c["mean"] >= B_MEAN and lo is not None and lo > B_MEAN_LO_MIN) or             (c["win"] >= B_ALT_WIN and c["mean"] >= B_ALT_MEAN and lo is not None and lo > 0):
        return "B", c
    return "NETRAL", c


def _last_close(ticker: str) -> float | None:
    try:
        df = core.get_ohlcv_daily_from_db(ticker, 5)
        return float(df["Close"].iloc[-1]) if df is not None and not df.empty else None
    except Exception:  # noqa: BLE001
        return None


def _exit_reason(p: dict, last_close: float | None) -> str | None:
    """KELUAR reason from the pick's own state: resolved recently, or price already through its SL / main TP."""
    st = p.get("status")
    if st and st not in ("ALIVE", "LEGACY_CLOSED"):
        rd = p.get("resolved_date")
        try:
            recent = rd is None or (datetime.date.today() - datetime.date.fromisoformat(str(rd)[:10])).days <= EXIT_RECENT_DAYS
        except Exception:  # noqa: BLE001
            recent = True
        if recent:
            what = "TP tercapai" if "TP" in str(st).upper() else "SL kena" if "SL" in str(st).upper() else "habis horizon" if "EXPIRED" in str(st).upper() else str(st)
            ret = p.get("resolved_ret_pct")
            return f"{what}{f' ({ret:+.1f}%)' if ret is not None else ''}"
        return None
    if last_close is None:
        return None
    sl = p.get("sl_price")
    if sl and last_close <= sl:
        return f"harga {last_close:,.0f} sudah di/bawah SL {sl:,.0f}"
    tp = p.get("tp_price") or p.get("tp2_price") or p.get("tp1_price")
    if tp and last_close >= tp:
        return f"harga {last_close:,.0f} sudah mencapai TP {tp:,.0f}: ambil untung"
    return None


def classify(lane: str, p: dict, last_close: float | None = None) -> dict:
    """{'tag': A|B|TAHAN|KELUAR|NETRAL|HINDARI, 'reason': str, 'cell': dict|None} for one lane pick (priority: KELUAR > HINDARI >
    TAHAN > BELI-A/B > NETRAL). EXT / spike warnings downgrade BELI-A -> BELI-B and BELI-B -> NETRAL."""
    t = p["ticker"]
    trig = p.get("trigger_date")
    if last_close is None:
        last_close = _last_close(t)
    exit_r = _exit_reason(p, last_close)
    if exit_r:
        return {"tag": "KELUAR", "reason": exit_r, "cell": None}
    if p.get("status") not in (None, "ALIVE"):
        return {"tag": "NETRAL", "reason": "pick lama, sudah selesai", "cell": None}
    ev = evaluate(lane, t, trig)
    if ev["hindari"]:
        return {"tag": "HINDARI", "reason": "; ".join(ev["hindari"]), "cell": None}
    age = int(p.get("age_days", 1) or 1)
    if age > ENTRY_WINDOW_DAYS.get(lane, 2):
        note = ", TP1 sudah tersentuh" if p.get("tp1_touched") else ""
        return {"tag": "TAHAN", "reason": f"hari ke-{age}, lewat jendela entry baru{note}", "cell": None}
    phase = regime_phase_on(trig) if trig else None
    lvl, c = cell_level(lane, phase)
    if lvl == "NA":
        return {"tag": "NETRAL", "reason": f"bukti {lane} saat {phase or 'regime tak diketahui'} kurang (n<{MIN_N})", "cell": c}
    stats = f"{lane} saat {phase}: win {c['win']:.0f}% | rata-rata {c['mean']:+.1f}% (n={c['n']})"
    if lvl in ("A", "B") and ev["warn"] and lane not in NO_DOWNGRADE_LANES:
        lvl2 = "B" if lvl == "A" else "NETRAL"
        return {"tag": lvl2, "reason": f"{stats} — turun satu tingkat: {ev['warn'][0]}", "cell": c}
    return {"tag": lvl, "reason": stats, "cell": c}


def classify_lookup(ticker: str) -> dict:
    """Tag for a ticker that is not an active pick (plain /check lookup): HINDARI only on the documented generic rules."""
    ev = evaluate(None, ticker.upper(), None)
    if ev["hindari"]:
        return {"tag": "HINDARI", "reason": "; ".join(ev["hindari"]), "cell": None}
    return {"tag": "NETRAL", "reason": "bukan pick aktif di lane mana pun", "cell": None}


def tag_text(d: dict) -> str:
    icon, name = TAG_STYLE[d["tag"]]
    return f"{icon} {name} — {d['reason']}"
