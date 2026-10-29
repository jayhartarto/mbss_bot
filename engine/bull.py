"""
engine/bull.py -- MBSS v2 (2026-10-29): lane BULL ("bull5", research/bull5/SESSION_SUMMARY_2026_10_28.md).

Buy a stock that was ALERTED AGAIN within 3 trading days of a first alert (confirmation), only while the hybrid IHSG gate (engine/ihsg_gate.py) is open.

Raw alert on day t (features at the close of t, all from the stock's own OHLCV):
  T  = slope20 >= 7.09 and atr14 <= 5.83                              (trend + low volatility)
  S1 = T and atrratio <= 0.971 and cpos >= 0.25                       (momentum, closing in the upper part of the range)
  S2 = T and body >= 0.704                                            (momentum, green candle body)
  A  = ret3 >= 5.7 and gap <= -0.93 and cpos <= 0.941                (pullback after a 3-day pop, gap-down open)
  B  = ret10 >= 12 and d5 >= -1.15 and d50 <= 4.58 and rng_pct <= 3.85   (pullback inside a strong 10-day move)
  raw = S1 or S2 or A or B.   PICK = raw today AND a raw alert on at least one of the previous 3 bars (p3 >= 1).
Universe: 20-day median traded value >= Rp1bn, close >= 100, not HSC.  One open position per ticker.

Execution / exit (backtest-identical): buy at the OPEN of the bar after the alert; TP +20%, SL -8%, trailing stop that arms once the high is +8% above entry and then
trails 6% below the running high (armed from the NEXT bar), SL checked before TP inside a bar, gap-aware stop fill, max 9 bars then close.
Evidence (net of 0.4% cost, train Jan23-Jun25 / test Jul25-Oct26): confirm3 test EV +2.0% win 59%; 8-slot portfolio with the hybrid gate +244% / MDD -5.6%.
Caveats: exit and gate thresholds were tuned on the same data; Aug-Oct 2026 the lane was ~-1.8% (flat through the 10-16 crash because the gate was closed).
"""
from __future__ import annotations

import json
import os

import numpy as np
import pandas as pd

from engine import legacy_core as core

PICKS_FILE = os.path.join(core.PROJECT_ROOT, "bull_picks.json")

MIN_HISTORY = 120
LIQ_MIN_BN = 1.0     # 20-day median of close*volume, Rp bn
PRICE_MIN = 100.0

SLOPE20_MIN, ATR14_MAX = 7.09, 5.83
S1_ATRRATIO_MAX, S1_CPOS_MIN = 0.971, 0.25
S2_BODY_MIN = 0.704
A_RET3_MIN, A_GAP_MAX, A_CPOS_MAX = 5.7, -0.93, 0.941
B_RET10_MIN, B_D5_MIN, B_D50_MAX, B_RNG_MAX = 12.0, -1.15, 4.58, 3.85
CONFIRM_WINDOW = 3

TP_PCT = 20.0
SL_PCT = 8.0
TRAIL_ARM_PCT = 8.0
TRAIL_WIDTH_PCT = 6.0
MAX_HOLD_DAYS = 9
ENTRY_WINDOW_DAYS = 1   # only Day 1 (buy at the open right after the alert) is an entry; later days are tracking (lane_scorecard.ENTRY_WINDOW_DAYS["BULL"])


# ------------------------------------------------------------------ features / signals
def features(df: pd.DataFrame) -> pd.DataFrame:
    """Per-bar features (same formulas as research/bull5/build_2026_10_28.py). df columns Open/High/Low/Close/Volume, ascending."""
    o, h, l, c, v = (df[x].astype(float).reset_index(drop=True) for x in ("Open", "High", "Low", "Close", "Volume"))
    f = pd.DataFrame({"date": df.index})
    for k in (3, 10):
        f[f"ret{k}"] = c.pct_change(k) * 100
    sma5, sma20, sma50 = c.rolling(5).mean(), c.rolling(20).mean(), c.rolling(50).mean()
    f["d5"] = (c / sma5 - 1) * 100
    f["d50"] = (c / sma50 - 1) * 100
    f["slope20"] = (sma20 / sma20.shift(5) - 1) * 100
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    f["atr14"] = tr.rolling(14).mean() / c * 100
    f["atrratio"] = (tr.rolling(5).mean() / c * 100) / f["atr14"]
    rng = (h - l).where(h > l)
    f["cpos"] = (c - l) / rng
    f["body"] = (c - o) / o * 100
    f["gap"] = (o / pc - 1) * 100
    f["rng_pct"] = (h - l) / c * 100
    f["liq"] = (c * v).rolling(20).median() / 1e9
    f["close"] = c
    f["sma50"] = sma50
    return f


def raw_signals(f: pd.DataFrame) -> pd.DataFrame:
    T = (f.slope20 >= SLOPE20_MIN) & (f.atr14 <= ATR14_MAX)
    S1 = T & (f.atrratio <= S1_ATRRATIO_MAX) & (f.cpos >= S1_CPOS_MIN)
    S2 = T & (f.body >= S2_BODY_MIN)
    A = (f.ret3 >= A_RET3_MIN) & (f.gap <= A_GAP_MAX) & (f.cpos <= A_CPOS_MAX)
    B = (f.ret10 >= B_RET10_MIN) & (f.d5 >= B_D5_MIN) & (f.d50 <= B_D50_MAX) & (f.rng_pct <= B_RNG_MAX)
    out = pd.DataFrame({"S1": S1.fillna(False), "S2": S2.fillna(False), "A": A.fillna(False), "B": B.fillna(False)})
    out["raw"] = out[["S1", "S2", "A", "B"]].any(axis=1)
    prior = out["raw"].astype(int).shift(1).rolling(CONFIRM_WINDOW, min_periods=1).sum().fillna(0)
    out["p3"] = prior
    out["pick"] = out["raw"] & (prior >= 1)
    return out


def simulate_exit(o, h, l, c, i: int) -> dict | None:
    """Pure exit replay for an alert on bar i (entry = open of i+1). Arrays are numpy float. Returns
    {status, ret_pct, bars, entry_open, stop, max_high, trail_armed}; status ALIVE while fewer than MAX_HOLD_DAYS bars exist and nothing triggered."""
    if i + 1 >= len(o) or not o[i + 1] > 0:
        return None
    e = float(o[i + 1])
    sl = e * (1 - SL_PCT / 100)
    stop, tp, maxh, armed = sl, e * (1 + TP_PCT / 100), e, False
    last = min(i + MAX_HOLD_DAYS, len(o) - 1)
    for j in range(i + 1, last + 1):
        if l[j] <= stop:
            return dict(status="TRAIL_HIT" if armed else "SL_HIT", ret_pct=(min(o[j], stop) / e - 1) * 100, bars=j - i, entry_open=e, stop=stop, max_high=maxh, trail_armed=armed)
        if h[j] >= tp:
            return dict(status="TP_HIT", ret_pct=TP_PCT, bars=j - i, entry_open=e, stop=stop, max_high=maxh, trail_armed=armed)
        maxh = max(maxh, float(h[j]))
        if maxh >= e * (1 + TRAIL_ARM_PCT / 100):
            armed = True
            stop = max(stop, maxh * (1 - TRAIL_WIDTH_PCT / 100))
    if i + MAX_HOLD_DAYS <= len(o) - 1:
        return dict(status="EXPIRED", ret_pct=(c[last] / e - 1) * 100, bars=last - i, entry_open=e, stop=stop, max_high=maxh, trail_armed=armed)
    return dict(status="ALIVE", ret_pct=(c[last] / e - 1) * 100, bars=last - i, entry_open=e, stop=stop, max_high=maxh, trail_armed=armed)


# ------------------------------------------------------------------ nightly candidates
def evaluate_ticker(ticker: str) -> dict | None:
    """Candidate dict if `ticker` is a confirmed BULL alert on its latest bar, else None."""
    df = core.get_ohlcv_daily_from_db(ticker, limit=250)
    if df is None or len(df) < MIN_HISTORY:
        return None
    f = features(df)
    s = raw_signals(f)
    last = f.iloc[-1]
    if not bool(s["pick"].iloc[-1]):
        return None
    if not (last.liq >= LIQ_MIN_BN and last.close >= PRICE_MIN) or pd.isna(last.sma50):
        return None
    if core.hsc_tag(ticker):
        return None
    forms = [k for k in ("S1", "S2", "A", "B") if bool(s[k].iloc[-1])]
    kind = "PULLBACK" if ("A" in forms or "B" in forms) else "MOMENTUM"
    ref = float(last.close)
    return {
        "ticker": ticker,
        "trigger_date": df.index[-1].strftime("%Y-%m-%d"),
        "status": "ALIVE",
        "age_days": 1,
        "formulas": forms,
        "kind": kind,
        "entry_ref_price": ref,
        "entry_open": None,
        "sl_price": round(ref * (1 - SL_PCT / 100)),
        "tp_price": round(ref * (1 + TP_PCT / 100)),
        "trail_armed": False,
        "max_high": None,
        "price_drift_pct": 0.0,
        "resolved_date": None,
        "resolved_ret_pct": None,
    }


def compute_bull_candidates(tickers: list[str]) -> list[dict]:
    """Nightly entry point. Applies the hybrid IHSG gate on the signal date: gate closed -> no new picks (the dropped ones are logged for the audit)."""
    import engine.ihsg_gate as ihsg_gate
    cands = []
    for t in tickers:
        try:
            c = evaluate_ticker(t)
        except Exception as e:  # noqa: BLE001
            print(f"⚠️ BULL: gagal evaluasi {t}: {e}")
            continue
        if c is not None:
            cands.append(c)
    gate = ihsg_gate.get_gate_today()
    if not gate["open"] and cands:
        ihsg_gate.log_dropped("BULL", cands, gate)
        print(f"⛔ BULL: gate IHSG tertutup ({gate['reason']}), {len(cands)} kandidat tidak dibuat.")
        return []
    return cands


# ------------------------------------------------------------------ persistence / resolution
def load_bull_picks() -> list:
    if not os.path.exists(PICKS_FILE):
        return []
    try:
        with open(PICKS_FILE) as fh:
            return json.load(fh)
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ Gagal membaca bull_picks.json: {e}")
        return []


def save_bull_picks(picks: list):
    tmp = PICKS_FILE + ".tmp"
    try:
        core._backup_json_file_daily(PICKS_FILE)
        with open(tmp, "w") as fh:
            json.dump(picks, fh, indent=2, default=core._json_default_numpy_safe)
        os.replace(tmp, PICKS_FILE)
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ Gagal menyimpan bull_picks.json: {e}")
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except Exception:  # noqa: BLE001
            pass


def _resolve_active_pick(pick: dict) -> dict:
    """Stateless replay of the exit from the pick's trigger bar through the latest bar in the DB (idempotent, safe to call twice a day)."""
    df = core.get_ohlcv_daily_from_db(pick["ticker"], limit=60)
    if df is None or df.empty:
        return pick
    dates = df.index.strftime("%Y-%m-%d")
    idx = np.flatnonzero(dates == pick["trigger_date"])
    if len(idx) == 0:
        return pick
    i = int(idx[0])
    if i + 1 >= len(df):
        return pick          # alert bar is still the latest bar -> not entered yet
    arr = {k: df[k.capitalize()].to_numpy(float) for k in ("open", "high", "low", "close")}
    r = simulate_exit(arr["open"], arr["high"], arr["low"], arr["close"], i)
    if r is None:
        return pick
    pick["entry_open"] = round(r["entry_open"], 2)
    pick["sl_price"] = round(r["entry_open"] * (1 - SL_PCT / 100))
    pick["tp_price"] = round(r["entry_open"] * (1 + TP_PCT / 100))
    pick["trail_armed"] = bool(r["trail_armed"])
    pick["trail_stop"] = round(r["stop"]) if r["trail_armed"] else None
    pick["max_high"] = round(r["max_high"], 2)
    pick["age_days"] = r["bars"] + 1
    pick["price_drift_pct"] = round((float(arr["close"][-1]) / r["entry_open"] - 1) * 100, 2)
    pick["_last_checked_date"] = dates[-1]
    if r["status"] != "ALIVE":
        pick["status"] = r["status"]
        pick["resolved_date"] = dates[min(i + r["bars"], len(df) - 1)]
        pick["resolved_ret_pct"] = round(r["ret_pct"], 2)
    return pick


def update_and_save_picks(new_candidates: list[dict]) -> list:
    """Resolve every ALIVE pick, append today's candidates for tickers without an ALIVE pick, tag, save. Call once per nightly cycle."""
    history = load_bull_picks()
    active = set()
    for pick in history:
        if pick.get("status") == "ALIVE":
            try:
                _resolve_active_pick(pick)
            except Exception as e:  # noqa: BLE001
                print(f"⚠️ BULL: gagal resolve pick {pick.get('ticker')}: {e}")
            if pick.get("status") == "ALIVE":
                active.add(pick["ticker"])
    for cand in new_candidates:
        if cand["ticker"] in active:
            continue
        history.append(cand)
        active.add(cand["ticker"])
    from engine import newbuyer_tag  # booster tag, display-only; fail-safe (never raises)
    newbuyer_tag.annotate(history, "BULL")
    save_bull_picks(history)
    return history
