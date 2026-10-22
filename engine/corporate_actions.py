"""
engine/corporate_actions.py — MBSS v2 (2026-10-22).

Upcoming corporate-action windows (cash dividend, rights issue, stock split / reverse split, bonus shares) from IPOT's free
"Corporate Action & RUPS" fragment (no key; verified 2026-10-22):
  https://www.indopremier.com/module/saham/include/corporateaction.php?code=T&start=MM/DD/YYYY
Answers every event from `start` on (upcoming ones included) as an HTML table: type, amount, R1, R2, cum date, status, and hidden cells with
the ex date (data-ca=5) etc. Same module / spurious-404 quirk as the broker-summary endpoint (retry with doubling pauses).

These are MECHANICAL warnings, not trading signals: the price drops on the ex date (dividend) or is adjusted (rights / split), which can
trip a stop-loss that has nothing to do with the thesis. Display-only: never changes a tag level. Dividends as a PRICE signal were tested
earlier and have no edge (engine/dividend_calendar.py docstring).

Cache: corporate_actions_cache.json in the project root (12h TTL per ticker). The nightly job prefetches active-pick tickers so /swing can
read cache-only; /check may do one live fetch for its single ticker.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import time
import urllib.error
import urllib.request

import engine.legacy_core as core

URL = "https://www.indopremier.com/module/saham/include/corporateaction.php?code={t}&start={d}"
UA = "Mozilla/5.0"
CACHE_FILE = os.path.join(core.PROJECT_ROOT, "corporate_actions_cache.json")
TTL_HOURS = 12
LOOKBACK_DAYS = 10          # fetch from this many days back so an ex date a few days after its cum date is still caught
DEFAULT_HORIZON_DAYS = 25   # calendar days ahead that count as "inside the holding window"

_ROW = re.compile(r"<tr>(.*?)</tr>", re.S)
_CELL = re.compile(r"<td([^>]*)>(.*?)</td>", re.S)
_TAG = re.compile(r"<[^>]+>")


def _num(s: str) -> float | None:
    try:
        return float(s.replace(",", "").strip())
    except Exception:  # noqa: BLE001
        return None


def _kind(type_text: str) -> str:
    t = type_text.lower()
    if "right" in t or "hmetd" in t:
        return "ri"
    if "split" in t:
        return "split"
    if "bonus" in t or ("saham" in t and "deviden" in t) or "stock div" in t:
        return "bonus"
    if "deviden" in t or "dividen" in t or "dividend" in t:
        return "div"
    return "other"


def parse_events(html: str) -> list[dict]:
    """Rows of the first table (Corporate Actions), before the RUPS table."""
    if "<tbody>" not in html:
        return []
    body = html.split("<tbody>", 1)[1].split("</tbody>", 1)[0]
    events = []
    for tr in _ROW.findall(body):
        cells = _CELL.findall(tr)
        if len(cells) < 8:
            continue
        txt = [" ".join(_TAG.sub(" ", b).split()) for _, b in cells]
        ca = {}
        for (attrs, _), t in zip(cells, txt):
            m = re.search(r'data-ca="(\d+)"', attrs)
            if m:
                ca[int(m.group(1))] = t
        events.append({
            "type": txt[2], "kind": _kind(txt[2]), "amount": _num(txt[3]), "r1": _num(txt[4]), "r2": _num(txt[5]),
            "cum": ca.get(3), "status": ca.get(4), "ex": ca.get(5),
        })
    return events


def _fetch(ticker: str) -> list[dict] | None:
    since = (datetime.date.today() - datetime.timedelta(days=LOOKBACK_DAYS)).strftime("%m/%d/%Y")
    url = URL.format(t=ticker, d=since)
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "text/html"})
            with urllib.request.urlopen(req, timeout=30) as r:
                html = r.read().decode("utf-8", "replace")
            if "Invalid stock code" in html:
                return []
            return parse_events(html)
        except urllib.error.HTTPError as e:
            time.sleep(0.5 * (2 ** attempt) if e.code == 404 else 5)
        except Exception:  # noqa: BLE001
            time.sleep(0.5 * (2 ** attempt))
    return None


def _load_cache() -> dict:
    try:
        with open(CACHE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def _save_cache(cache: dict) -> None:
    try:
        tmp = CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cache, f, ensure_ascii=False)
        os.replace(tmp, CACHE_FILE)
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ corporate_actions: cache tidak tersimpan (diabaikan): {e}")


def _fresh(entry: dict | None) -> bool:
    try:
        age = datetime.datetime.now() - datetime.datetime.fromisoformat(entry["fetched_at"])
        return age < datetime.timedelta(hours=TTL_HOURS)
    except Exception:  # noqa: BLE001
        return False


def get_events(ticker: str, allow_fetch: bool = True) -> list[dict]:
    """Cached events for `ticker` (12h TTL). With allow_fetch=False only the cache is read (stale cache still returned)."""
    t = ticker.upper()
    cache = _load_cache()
    entry = cache.get(t)
    if _fresh(entry) or not allow_fetch:
        return (entry or {}).get("events", [])
    ev = _fetch(t)
    if ev is None:
        return (entry or {}).get("events", [])      # fetch failed: fall back to whatever we had
    cache[t] = {"fetched_at": datetime.datetime.now().isoformat(timespec="seconds"), "events": ev}
    _save_cache(cache)
    return ev


def prefetch(tickers: list[str]) -> dict:
    """Nightly: refresh the cache for the given tickers (skips fresh entries). Never raises."""
    cache = _load_cache()
    done = failed = 0
    for t in sorted({x.upper() for x in tickers if x}):
        if _fresh(cache.get(t)):
            continue
        ev = _fetch(t)
        if ev is None:
            failed += 1
            continue
        cache[t] = {"fetched_at": datetime.datetime.now().isoformat(timespec="seconds"), "events": ev}
        done += 1
    if done:
        _save_cache(cache)
    return {"fetched": done, "failed": failed}


def _event_date(ev: dict) -> datetime.date | None:
    for k in ("ex", "cum"):
        v = ev.get(k)
        if v and re.fullmatch(r"\d{4}-\d{2}-\d{2}", v):
            return datetime.date.fromisoformat(v)
    return None


def upcoming(ticker: str, horizon_days: int = DEFAULT_HORIZON_DAYS, allow_fetch: bool = True) -> list[dict]:
    """Events whose ex date (else cum date) is within [today-2, today+horizon]. Sorted by date."""
    today = datetime.date.today()
    out = []
    for ev in get_events(ticker, allow_fetch=allow_fetch):
        if ev["kind"] == "other":
            continue
        d = _event_date(ev)
        if d is None or not (today - datetime.timedelta(days=2) <= d <= today + datetime.timedelta(days=horizon_days)):
            continue
        out.append({**ev, "date": d})
    return sorted(out, key=lambda e: e["date"])


def format_event(ev: dict, price: float | None = None) -> str:
    d = ev["date"]
    when = f"{d.day} {['Jan','Feb','Mar','Apr','Mei','Jun','Jul','Agu','Sep','Okt','Nov','Des'][d.month - 1]}"
    delta = (d - datetime.date.today()).days
    rel = "hari ini" if delta == 0 else f"{delta} hari lagi" if delta > 0 else f"{-delta} hari lalu"
    k = ev["kind"]
    if k == "div":
        amt = ev.get("amount")
        pct = f" ≈ {amt / price * 100:.1f}% harga" if amt and price else ""
        return f"📅 Ex-dividen {when} ({rel}): Rp{amt:,.1f}{pct}, harga turun mekanis saat ex" if amt else \
               f"📅 Ex-dividen {when} ({rel}), harga turun mekanis saat ex"
    if k == "ri":
        ratio = f"{ev['r1']:g}:{ev['r2']:g} " if ev.get("r1") and ev.get("r2") else ""
        px = f"@Rp{ev['amount']:,.0f} " if ev.get("amount") else ""
        return f"📅 Right Issue {ratio}{px}ex {when} ({rel}): dilusi, harga disesuaikan, SL bisa kena palsu"
    if k == "split":
        ratio = f" {ev['r1']:g}:{ev['r2']:g}" if ev.get("r1") and ev.get("r2") else ""
        return f"📅 {ev['type']}{ratio} ex {when} ({rel}): harga disesuaikan, SL/TP lama jadi tidak valid"
    ratio = f" {ev['r1']:g}:{ev['r2']:g}" if ev.get("r1") and ev.get("r2") else ""
    return f"📅 {ev['type']}{ratio} ex {when} ({rel}): harga disesuaikan, cek SL/TP"


def window_lines(ticker: str, horizon_days: int = DEFAULT_HORIZON_DAYS, allow_fetch: bool = True, max_lines: int = 3) -> list[str]:
    try:
        evs = upcoming(ticker, horizon_days, allow_fetch)
        if not evs:
            return []
        price = None
        try:
            df = core.get_ohlcv_daily_from_db(ticker.upper(), 5)
            price = float(df["Close"].iloc[-1]) if df is not None and not df.empty else None
        except Exception:  # noqa: BLE001
            pass
        return [format_event(e, price) for e in evs[:max_lines]]
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ corporate_actions: {ticker} gagal (diabaikan): {e}")
        return []
