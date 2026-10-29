"""Crowd-avoid flag (research/bull5 broker_booster, user-approved 2026-10-28): HIDE a NEW pick when, over the 5 sessions up to its trigger
date, scalpers (MG, CP) net-bought >= 3% of traded lots or retail-app brokers (XL, XC, PD, GI, RO) net-bought >= 5%.
Same net convention as engine/newbuyer_tag (broker missing on the buy side -> buys 0; missing on the sell side -> sells the day's
smallest listed sell). Fail-OPEN: no IPOT data / any error -> the pick stays visible. Display-time only, nothing is persisted."""
from __future__ import annotations

import sqlite3

import numpy as np
import pandas as pd

from engine.broker_roles import role_of
from engine.newbuyer_tag import IPOT_DB, OHLCV_DB

SCALPER_NET5_MIN = 0.03
RETAIL_APP_NET5_MIN = 0.05
HIDE_LANES = ("BULL",)  # only where the evidence exists (research/bull5 broker_booster); BOW/BOS/OSB-v2 untested -> not hidden


def compute(ticker: str, date: str, ipot_conn=None, ohlcv_conn=None) -> dict | None:
    """-> {scalper, retail_app, hide} (net5 as a fraction of 5-day lots) or None when data cannot support a decision."""
    own_i, own_o = ipot_conn is None, ohlcv_conn is None
    ic = sqlite3.connect(f"file:{IPOT_DB}?mode=ro", uri=True, timeout=60) if own_i else ipot_conn
    oc = sqlite3.connect(OHLCV_DB, timeout=60) if own_o else ohlcv_conn
    try:
        o = pd.read_sql_query("SELECT date, volume FROM ohlcv_daily WHERE ticker IN (?, ?) AND date<=? ORDER BY date DESC LIMIT 5",
                              oc, params=(ticker, ticker + ".JK", date)).iloc[::-1].reset_index(drop=True)
        if len(o) < 5 or o.date.iloc[-1] != date:
            return None
        b = pd.read_sql_query("SELECT trade_date date, broker_code broker, buy_lot, sell_lot FROM ipot_broker_daily "
                              "WHERE ticker=? AND fd='all' AND board='all' AND trade_date>=? AND trade_date<=?",
                              ic, params=(ticker, o.date.iloc[0], date))
        if b.empty or date not in set(b.date) or b.date.nunique() < 5:
            return None
        ms = b.groupby("date").sell_lot.transform("min").fillna(0)
        sl, bl = b.sell_lot.fillna(0), b.buy_lot.fillna(0)
        b["net"] = np.where(b.buy_lot.isna(), -sl, bl - np.where(b.sell_lot.isna(), ms, sl))
        b["role"] = b.broker.map(role_of)
        tot = float(o.volume.sum()) / 100.0   # shares -> lots
        if tot <= 0:
            return None
        sc = float(b[b.role == "scalper"].net.sum()) / tot
        ra = float(b[b.role == "retail_app"].net.sum()) / tot
        return {"scalper": round(sc, 4), "retail_app": round(ra, 4),
                "hide": bool(sc >= SCALPER_NET5_MIN or ra >= RETAIL_APP_NET5_MIN)}
    except Exception as e:  # noqa: BLE001 -- a flag problem must never break a lane
        print(f"⚠️ crowd_flag {ticker} {date}: {e}")
        return None
    finally:
        if own_i:
            ic.close()
        if own_o:
            oc.close()


def partition(lane: str, picks: list[dict], max_new_age: int = 1) -> tuple[list[dict], list[dict]]:
    """-> (kept, hidden). Only NEW picks (age_days <= max_new_age) of HIDE_LANES are evaluated; the rest pass through."""
    if lane not in HIDE_LANES or not picks:
        return picks, []
    kept, hidden = [], []
    try:
        ic = sqlite3.connect(f"file:{IPOT_DB}?mode=ro", uri=True, timeout=60)
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ crowd_flag: IPOT DB tidak bisa dibuka ({e}); flag dilewati")
        return picks, []
    oc = sqlite3.connect(OHLCV_DB, timeout=60)
    try:
        for p in picks:
            r = None
            if p.get("age_days", 1) <= max_new_age and p.get("trigger_date"):
                r = compute(p["ticker"].replace(".JK", ""), str(p["trigger_date"])[:10], ic, oc)
            if r and r["hide"]:
                p["crowd_flag"] = r
                hidden.append(p)
            else:
                kept.append(p)
    finally:
        ic.close()
        oc.close()
    return kept, hidden


def footnote(hidden: list[dict]) -> str | None:
    if not hidden:
        return None
    return f"🙈 {len(hidden)} pick disembunyikan (crowd): " + ", ".join(
        f"{p['ticker']} ({'scalper' if p['crowd_flag']['scalper'] >= SCALPER_NET5_MIN else 'ritel-app'} "
        f"{max(p['crowd_flag']['scalper'], p['crowd_flag']['retail_app']) * 100:+.0f}%)" for p in hidden)
