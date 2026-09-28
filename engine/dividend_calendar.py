# -*- coding: utf-8 -*-
"""
Kalender dividen (cum/ex/recording/payment date) dari RapidAPI IDX
`/api/calendar/dividend` — 1 call market-wide mengembalikan ~1 tahun event
sampai cum date terjauh yang sudah diumumkan. Informational only, BUKAN
sinyal trading (dividen sudah pernah dites sbg sinyal harga di
engine/news_catalyst.py dan tidak ada edge).

Hemat kuota: refresh cuma kalau data tersimpan sudah >= REFRESH_EVERY_DAYS
hari (bukan tiap /eodscan). /check TIDAK PERNAH fetch, cuma baca file.

Event di-merge by dividend_id ke file lokal, jadi histori terakumulasi
melewati jendela 1 tahun API.

API tidak membedakan interim vs final (event_note kosong, fiscal_year
selalu 0). Yang ditampilkan cuma fakta: berapa kali ticker membagi dividen
dalam 12 bulan terakhir — >1x berarti ada pembagian interim.
"""
from __future__ import annotations

import datetime
import json
import os

import engine.legacy_core as core
import engine.broker as broker_engine

DIVIDEND_CALENDAR_FILE = os.path.join(core.PROJECT_ROOT, "dividend_calendar.json")
# Cum date kadang diumumkan <1 minggu sebelumnya (NICL 2026-10-02 sudah
# muncul 2026-09-25), jadi refresh mingguan bisa kelewatan. 3 hari ~ 10
# call/bulan dari budget 400.
REFRESH_EVERY_DAYS = 3
RECENT_EX_WINDOW_DAYS = 7
MARKET_CLOSE_WIB = datetime.time(16, 0)


def _load() -> dict:
    if not os.path.exists(DIVIDEND_CALENDAR_FILE):
        return {"fetched_at": None, "events": {}}
    try:
        with open(DIVIDEND_CALENDAR_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ Gagal membaca kalender dividen: {e}")
        return {"fetched_at": None, "events": {}}


def _save(store: dict):
    with open(DIVIDEND_CALENDAR_FILE, "w", encoding="utf-8") as f:
        json.dump(store, f, indent=1)


def _today() -> datetime.date:
    return datetime.datetime.now(core.WIB).date()


def _parse_float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def is_stale(store: dict | None = None) -> bool:
    store = store or _load()
    fetched_at = store.get("fetched_at")
    if not fetched_at:
        return True
    try:
        age = datetime.datetime.now(core.WIB) - datetime.datetime.fromisoformat(fetched_at)
    except ValueError:
        return True
    return age >= datetime.timedelta(days=REFRESH_EVERY_DAYS)


def refresh_if_stale() -> bool:
    """Fetch + merge kalau data sudah basi. Return True kalau fetch berhasil."""
    store = _load()
    if not is_stale(store):
        print(f"📋 Kalender dividen: masih segar (fetched {store.get('fetched_at')}), skip (hemat kuota).")
        return False
    rows = broker_engine.fetch_rapidapi_dividend_calendar()
    if not rows:
        return False
    events = store.get("events", {})
    for r in rows:
        div_id = str(r.get("dividend_id") or "")
        ticker = r.get("company_symbol")
        if not div_id or not ticker:
            continue
        events[div_id] = {
            "ticker": ticker,
            "cum_date": r.get("dividend_cumdate") or None,
            "ex_date": r.get("dividend_exdate") or None,
            "rec_date": r.get("dividend_recdate") or None,
            "pay_date": r.get("dividend_paydate") or None,
            "value": _parse_float(r.get("dividend_value")),
            "currency": r.get("dividend_currency"),
            "lastprice": _parse_float(r.get("lastprice")),
        }
    store = {"fetched_at": datetime.datetime.now(core.WIB).isoformat(timespec="seconds"), "events": events}
    _save(store)
    print(f"💰 Kalender dividen: {len(rows)} event dari API, total tersimpan {len(events)}.")
    return True


def _payouts_last_12m(events: list, ticker: str, ref_cum: str) -> int:
    """Jumlah pembagian ticker dgn cum date dalam 365 hari s/d ref_cum (inklusif)."""
    ref = datetime.date.fromisoformat(ref_cum)
    start = ref - datetime.timedelta(days=365)
    n = 0
    for e in events:
        if e["ticker"] != ticker or not e.get("cum_date"):
            continue
        d = datetime.date.fromisoformat(e["cum_date"])
        if start < d <= ref:
            n += 1
    return n


def _enrich(e: dict, all_events: list, today: datetime.date, price: float | None = None) -> dict:
    e = dict(e)
    cum = datetime.date.fromisoformat(e["cum_date"])
    e["days_to_cum"] = (cum - today).days
    ref_price = price or e.get("lastprice")
    e["yield_pct"] = round(e["value"] / ref_price * 100, 2) if e.get("value") and ref_price else None
    e["payouts_12m"] = _payouts_last_12m(all_events, e["ticker"], e["cum_date"])
    return e


def get_calendar_view(today: datetime.date | None = None) -> dict:
    """{"upcoming": [...cum >= today], "recent_ex": [...ex dlm 7 hari terakhir], "fetched_at": str|None}"""
    today = today or _today()
    store = _load()
    events = [e for e in store.get("events", {}).values() if e.get("cum_date")]
    upcoming, recent_ex = [], []
    for e in events:
        cum = datetime.date.fromisoformat(e["cum_date"])
        ex = datetime.date.fromisoformat(e["ex_date"]) if e.get("ex_date") else None
        if cum >= today:
            upcoming.append(_enrich(e, events, today))
        elif ex and today - datetime.timedelta(days=RECENT_EX_WINDOW_DAYS) <= ex <= today:
            recent_ex.append(_enrich(e, events, today))
    upcoming.sort(key=lambda e: e["cum_date"])
    recent_ex.sort(key=lambda e: e["ex_date"], reverse=True)
    return {"upcoming": upcoming, "recent_ex": recent_ex, "fetched_at": store.get("fetched_at")}


def get_ticker_dividend(ticker: str, price: float | None = None, today: datetime.date | None = None) -> dict | None:
    """Event dividen ticker yg relevan sekarang: cum date akan datang, atau sudah ex tapi belum dibayar."""
    today = today or _today()
    events = [e for e in _load().get("events", {}).values() if e.get("cum_date")]
    relevant = []
    for e in events:
        if e["ticker"] != ticker:
            continue
        cum = datetime.date.fromisoformat(e["cum_date"])
        pay = datetime.date.fromisoformat(e["pay_date"]) if e.get("pay_date") else None
        if cum >= today or (pay and pay >= today):
            relevant.append(e)
    if not relevant:
        return None
    relevant.sort(key=lambda e: e["cum_date"])
    return _enrich(relevant[0], events, today, price)


def _fmt_date(iso: str | None) -> str:
    if not iso:
        return "-"
    return datetime.date.fromisoformat(iso).strftime("%d %b")


def _fmt_value(value: float | None) -> str:
    if value is None:
        return "-"
    return f"Rp{value:,.2f}".rstrip("0").rstrip(".")


def _cum_session_closed(e: dict) -> bool:
    return e["days_to_cum"] == 0 and datetime.datetime.now(core.WIB).time() >= MARKET_CLOSE_WIB


def format_event_line(e: dict, session_note: bool = True) -> str:
    parts = [f"{_fmt_value(e.get('value'))}/lbr"]
    if session_note and _cum_session_closed(e):
        parts.insert(0, "cum HARI INI, sesi sudah tutup")
    if e.get("yield_pct") is not None:
        parts.append(f"yield ~{e['yield_pct']:.1f}%")
    if e.get("payouts_12m", 0) > 1:
        parts.append(f"pembagian ke-{e['payouts_12m']} dlm 12 bln")
    dates = f"cum {_fmt_date(e.get('cum_date'))} · ex {_fmt_date(e.get('ex_date'))} · bayar {_fmt_date(e.get('pay_date'))}"
    return f"{' | '.join(parts)}\n   {dates}"


def format_check_line(e: dict | None) -> str:
    """Satu blok ringkas utk /check, kosong kalau tidak ada event relevan."""
    if not e:
        return ""
    days = e["days_to_cum"]
    if days > 0:
        status = f"cum date {days} hari lagi (beli s/d {_fmt_date(e['cum_date'])} utk dapat hak)"
    elif days == 0 and _cum_session_closed(e):
        status = "cum date hari ini, sesi sudah tutup — beli sekarang sudah TIDAK dapat hak"
    elif days == 0:
        status = "HARI INI cum date (hari terakhir beli utk dapat hak)"
    else:
        status = f"sudah ex-date, dibayar {_fmt_date(e.get('pay_date'))}"
    return f"\n💰 Dividen: {status}\n   {format_event_line(e, session_note=False)}"
