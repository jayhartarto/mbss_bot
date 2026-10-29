"""
engine/newbuyer_tag.py — MBSS v2 (2026-10-27). "Pembeli baru" (new-buyer) booster tag for the VCP, BOS and MACD-confirm lanes.

Signal (research/ipot_explore, 2026-10-27; tag only, never a gate): in the 5 trading days up to the trigger date, brokers that were
ALMOST ABSENT in the stock over the prior 20 days (|net| < 0.25% of volume, window ending 5 days before) net-bought a large share of the
volume. Only newcomers from foreign_inst + local_other + foreign_retail_base count (gov_inst, retail_app and scalper newcomers do not help).
  nb_ref = sum over those newcomers of max(0, 5d net / 5d volume)        threshold NB_REF_MIN = 0.0570 (fixed from the NEUTRAL discovery period)
Evidence (production-lane replay 2023-2026, liquid >= Rp0.3bn/day, deduped): tagged n=183 vs untagged 1739 -> win 65.6 vs 60.8%, mean +1.15 vs +0.64%,
SL-hit 8.7 vs 8.2% (no risk reduction), t~2.6. Effect negative below ~Rp0.1bn/day, so the tag needs avg traded value >= LIQ_MIN. Not used on OSB-v2
(opposite sign) nor BOW (unclear). ~0.2 tagged picks/day overall (16% of days), so it is a booster/"Pilihan Kuat" marker, not a list filter.

Reads research/ipot_broker_daily.sqlite (fd=all, board=all; topped up by /ffupdate) and mbss_ohlcv.db. Net uses the conservative `net_lo`
(a broker missing from the buy side nets 0 buy; missing from the sell side is assumed to sell the day's smallest listed sell).
Returns None (unknown, no tag) whenever broker data for the trigger date or enough of the baseline window is missing.
"""
from __future__ import annotations

import os
import sqlite3

import numpy as np
import pandas as pd

from engine.broker_roles import role_of

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IPOT_DB = os.path.join(ROOT, "research", "ipot_broker_daily.sqlite")
OHLCV_DB = os.path.join(ROOT, "mbss_ohlcv.db")
NB_REF_MIN = 0.0570
LIQ_MIN = 3e8              # Rp per day, 20-day average of close*volume
NEW_BROKER_MAX_ACTIVITY = 0.0025
REF_ROLES = ("foreign_inst", "local_other", "foreign_retail_base")
TAG_LANES = ("VCP", "BOS", "MACD-confirm", "BULL")
MIN_BASELINE_DAYS = 15     # distinct days with broker rows among the 25 days before the trigger date
MARK = "🆕"
LABEL = "PEMBELI BARU"


def compute(ticker: str, date: str, ipot_conn=None, ohlcv_conn=None) -> dict | None:
    """-> {nb_ref, liq, tagged} as of trading day `date`, or None when the data cannot support a decision."""
    own_i, own_o = ipot_conn is None, ohlcv_conn is None
    ic = sqlite3.connect(f"file:{IPOT_DB}?mode=ro", uri=True, timeout=60) if own_i else ipot_conn
    oc = sqlite3.connect(OHLCV_DB, timeout=60) if own_o else ohlcv_conn
    try:
        o = pd.read_sql_query("SELECT date, close, volume FROM ohlcv_daily WHERE ticker IN (?, ?) AND date<=? ORDER BY date DESC LIMIT 45",
                              oc, params=(ticker, ticker + ".JK", date)).iloc[::-1].reset_index(drop=True)
        if len(o) < 30 or o.date.iloc[-1] != date:
            return None
        b = pd.read_sql_query("SELECT trade_date date, broker_code broker, buy_lot, sell_lot FROM ipot_broker_daily "
                              "WHERE ticker=? AND fd='all' AND board='all' AND trade_date>=? AND trade_date<=?",
                              ic, params=(ticker, o.date.iloc[0], date))
        if b.empty or date not in set(b.date):
            return None
        if b.date.nunique() < MIN_BASELINE_DAYS:
            return None
        ms = b.groupby("date").sell_lot.transform("min").fillna(0)
        sl, bl = b.sell_lot.fillna(0), b.buy_lot.fillna(0)
        b["net"] = np.where(b.buy_lot.isna(), -sl, bl - np.where(b.sell_lot.isna(), ms, sl))
        pv = b.pivot_table(index="date", columns="broker", values="net", aggfunc="sum").reindex(o.date).fillna(0.0)
        vol = pd.Series(o.volume.values / 100.0, index=pv.index)          # shares -> lots
        roll5 = pv.rolling(5).sum().div(vol.rolling(5).sum(), axis=0)
        pa = pv.abs().rolling(20).sum().shift(5).div(vol.rolling(20).sum(), axis=0)
        ref = [c for c in pv.columns if role_of(c) in REF_ROLES]
        if not ref:
            return None
        newm = pa[ref].iloc[-1] < NEW_BROKER_MAX_ACTIVITY
        nb = float(roll5[ref].iloc[-1].clip(lower=0)[newm].sum())
        liq = float((o.close * o.volume).iloc[-20:].mean())
        return {"nb_ref": round(nb, 4), "liq": liq, "tagged": bool(nb >= NB_REF_MIN and liq >= LIQ_MIN)}
    except Exception as e:  # noqa: BLE001 — a tag problem must never break a lane
        print(f"⚠️ newbuyer_tag {ticker} {date}: {e}")
        return None
    finally:
        if own_i:
            ic.close()
        if own_o:
            oc.close()


def annotate(picks: list[dict], lane: str) -> int:
    """Set `new_buyer` (True/False) + `new_buyer_nb` on ALIVE picks that have no decision yet, using the pick's own trigger_date.
    Picks whose broker data was not yet available stay None and are retried on the next call (e.g. after a later /ffupdate).
    Returns the number of picks newly decided. Never raises."""
    if lane not in TAG_LANES:
        return 0
    todo = [p for p in picks if p.get("status") == "ALIVE" and p.get("new_buyer") is None and p.get("trigger_date")]
    if not todo:
        return 0
    n = 0
    try:
        ic = sqlite3.connect(f"file:{IPOT_DB}?mode=ro", uri=True, timeout=60)
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ newbuyer_tag: IPOT DB tidak bisa dibuka ({e}); tag dilewati")
        return 0
    oc = sqlite3.connect(OHLCV_DB, timeout=60)
    try:
        for p in todo:
            r = compute(p["ticker"].replace(".JK", ""), str(p["trigger_date"])[:10], ic, oc)
            if r is None:
                continue
            p["new_buyer"], p["new_buyer_nb"] = r["tagged"], r["nb_ref"]
            n += 1
    finally:
        ic.close()
        oc.close()
    return n


def is_tagged(pick: dict) -> bool:
    return bool(pick.get("new_buyer"))


def live_report() -> str:
    """Live validation from the pick files: resolved picks, tagged vs untagged, per lane (win = resolved_ret_pct > 0)."""
    import json
    files = {"VCP": "vcp_pillar_picks.json", "BOS": "buy_on_strength_picks.json", "MACD-confirm": "macd_confirm_pillar_picks.json", "BULL": "bull_picks.json"}
    rows = []
    for lane, f in files.items():
        try:
            with open(os.path.join(ROOT, f), encoding="utf-8") as fh:
                for p in json.load(fh):
                    if p.get("resolved_date") and p.get("new_buyer") is not None and p.get("resolved_ret_pct") is not None:
                        rows.append((lane, bool(p["new_buyer"]), float(p["resolved_ret_pct"])))
        except Exception:  # noqa: BLE001
            pass
    if not rows:
        return "Belum ada pick ber-tag yang resolve."
    d = pd.DataFrame(rows, columns=["lane", "tag", "ret"])
    out = ["Validasi live tag PEMBELI BARU (pick resolved):"]
    for tag, g in d.groupby("tag"):
        out.append(f"  {'bertag' if tag else 'tanpa tag'}: n={len(g)} win={(g.ret > 0).mean() * 100:.0f}% mean={g.ret.mean():+.2f}%")
    return "\n".join(out)
