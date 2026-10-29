"""Shared hybrid IHSG gate (research/bull5, 2026-10-28; user-approved for BSJP Lane1,
MACD-confirm, BOW, VCP; OSB-v2 shows a risk label instead of being gated).

Gate OPEN when
    IHSG >= SMA50 - 1.1%                              (long gate)
 OR (IHSG >= 4% above its 10-day low AND IHSG >= SMA5) (rebound gate)

Caveat (Jul-Oct 2026): the gate showed NO benefit there (OSB-v2 dropped +5.9% vs kept
+2.0%; BOW kept -0.04% vs dropped +1.07%), so callers must LOG what they drop
(log_dropped) for a 4-6 week forward audit rather than trusting the backtest blindly.

Fail-OPEN: on any data error the gate reports open=True with reason "data_error".
"""
from __future__ import annotations

import datetime
import json
import os

import pandas as pd

from engine import legacy_core as core

LONG_D50_MIN = -1.1      # % vs SMA50
REBOUND_ABOVE_LOW = 4.0  # % above 10d low
DROP_LOG = "ihsg_gate_dropped.jsonl"

_cache: dict = {"date": None, "result": None}


def evaluate(close: pd.Series) -> dict:
    """Pure function: close = ascending IHSG closes. Returns state dict."""
    close = close.dropna()
    if len(close) < 50:
        raise ValueError("need >= 50 closes")
    last = float(close.iloc[-1])
    d50 = (last / close.rolling(50).mean().iloc[-1] - 1) * 100
    d5 = (last / close.rolling(5).mean().iloc[-1] - 1) * 100
    above_low = (last / close.rolling(10).min().iloc[-1] - 1) * 100
    long_ok = d50 >= LONG_D50_MIN
    rebound_ok = above_low >= REBOUND_ABOVE_LOW and d5 >= 0
    if long_ok:
        reason = f"IHSG {d50:+.1f}% vs SMA50"
    elif rebound_ok:
        reason = f"IHSG rebound +{above_low:.1f}% dari low 10 hari"
    else:
        reason = f"IHSG {d50:+.1f}% vs SMA50, rebound {above_low:+.1f}% dari low 10 hari"
    return {"open": bool(long_ok or rebound_ok), "reason": reason,
            "d50": float(d50), "d5": float(d5), "above10low": float(above_low)}


def get_gate_today() -> dict:
    """Cached per WIB calendar day. Never raises; fail-open on data error."""
    today = datetime.datetime.now(core.WIB).strftime("%Y-%m-%d")
    if _cache["date"] == today and _cache["result"] is not None:
        return _cache["result"]
    try:
        hist = core.get_yf_ticker("^JKSE").history(period="1y", timeout=30)
        res = evaluate(hist["Close"])
        res["date"] = hist.index[-1].strftime("%Y-%m-%d")
        _cache.update(date=today, result=res)
        return res
    except Exception as e:
        print(f"⚠️ ihsg_gate gagal, fail-open: {e}")
        return {"open": True, "reason": "data_error", "date": today}


def closed_notice(gate: dict | None = None) -> str | None:
    """One-line 'gate tertutup' notice for /swing and BSJP, None when open."""
    g = gate or get_gate_today()
    if g["open"]:
        return None
    return f"⛔ Gate IHSG tertutup ({g['reason']}) — pick lane ini disembunyikan."


def log_dropped(lane: str, picks: list[dict], gate: dict, path: str = DROP_LOG) -> None:
    """Append dropped picks for the forward audit (outcomes filled in later)."""
    try:
        with open(path, "a", encoding="utf-8") as f:
            for p in picks:
                f.write(json.dumps({"logged": datetime.date.today().isoformat(), "lane": lane,
                                    "ticker": p.get("ticker"), "entry_ref": p.get("entry_ref_price"),
                                    "gate_date": gate.get("date"), "reason": gate.get("reason")}) + "\n")
    except Exception as e:
        print(f"⚠️ ihsg_gate log_dropped gagal (diabaikan): {e}")


def filter_picks(lane: str, picks: list[dict]) -> tuple[list[dict], dict]:
    """Gate-open -> picks unchanged. Gate-closed -> ([], gate) and drops logged."""
    g = get_gate_today()
    if g["open"]:
        return picks, g
    log_dropped(lane, picks, g)
    return [], g


_logged: set = set()


def split_new(lane: str, picks: list[dict], max_new_age: int = 1) -> tuple[list[dict], dict, int]:
    """Display-time gate for NEW picks only (age_days <= max_new_age). The gate is evaluated on the
    signal date, so older (already tracked) picks are never hidden by today's gate.
    Returns (kept_picks, gate, n_dropped). Dropped picks are logged once per day per ticker."""
    g = get_gate_today()
    if g["open"]:
        return picks, g, 0
    new = [p for p in picks if p.get("age_days", 1) <= max_new_age]
    old = [p for p in picks if p.get("age_days", 1) > max_new_age]
    fresh = [p for p in new if (g.get("date"), lane, p.get("ticker")) not in _logged]
    if fresh:
        log_dropped(lane, fresh, g)
        _logged.update((g.get("date"), lane, p.get("ticker")) for p in fresh)
    return old, g, len(new)
