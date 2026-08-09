"""
engine/market.py — MarketContextEngine (MBSS v2 Sprint 1, Phase 3 + 3b)

Scope history (please read before extending this file)
-----------------------------------------------------
Phase 3 extracted what already existed in the codebase: today's IHSG
return, global macro backdrop (Wall St/Asia/USD-IDR/oil), and news
headlines. The breadth/sector-return/regime classification the Executive
Summary describes did NOT exist anywhere in the original code.

Phase 3b (this addition) builds that, but deliberately as a DIY, zero-new-
API computation: `run_nightly_full_scan()` in engine/nightly.py already
calls `compute_factor_scoring()` for every ticker in the ISSI-liquid
universe every night, and each result already carries `sector` (from
yfinance's `.info["sector"]`) and `ret_1d_pct` (today's % return) —
Phase 3b just aggregates data that's already being collected, instead of
adding a new paid API/key. See `compute_market_breadth()` below.

Caveats worth knowing:
  - Breadth/sector returns are computed over the ~212-ticker ISSI-liquid
    universe scanned each night, NOT the full ~800+ ticker IDX exchange.
    That's a real, narrower sample than "official" market breadth — but
    it's the same universe every other command already trusts, and it's
    free. A broader/official breadth figure would need a paid source
    (Sectors.app, Invezgo) or scraping IDX's own (undocumented) website —
    neither is done here.
  - `classify_market_regime()` is a same-day SNAPSHOT heuristic (today's
    breadth + today's IHSG return only). It is NOT a proper multi-day
    trend/volatility regime model — this refactor doesn't persist a
    breadth time series yet, so there's no history to detect "trending
    vs ranging" over time. Treat the regime label as a rough, same-day
    signal, not a rigorous classifier.
  - This is NEW logic (not extracted from the old codebase) — flagged
    explicitly since every other function in this file, and in
    engine/nightly.py, is a faithful move of pre-existing code.

Same circular-import rule as engine/nightly.py
-------------------------------------------------
Both `get_ihsg_return_today()` (needs `core.get_yf_ticker()`) and
`compute_market_breadth()` (needs `core.get_current_trading_day_close_marker()`)
call back into legacy_core.py, which in turn calls into this module — same
two-way dependency as engine/nightly.py. Both sides use MODULE imports
(`import engine.market as market_engine` / `import engine.legacy_core as
core`), never `from module import name` — see engine/nightly.py's
docstring for why the named form breaks depending on import order.
"""
from __future__ import annotations

import datetime
import xml.etree.ElementTree as ET

import requests

from engine import legacy_core as core
from engine.cache import cache_manager

# Cached per calendar day (WIB), not per call — get_ihsg_return_today() is
# invoked once per ticker inside compute_factor_scoring, so an uncached
# version would mean one extra Yahoo Finance call per ticker in every scan.
_ihsg_cache = {"date": None, "return_today": None}


def get_ihsg_return_today():
    """
    Return % IHSG (Jakarta Composite Index, ^JKSE) HARI INI SAJA (close terakhir
    vs close sebelumnya) — DIGANTI dari versi 10-hari kumulatif atas permintaan
    user: untuk swing pendek, perbandingan 10 hari kurang responsif/relevan
    dibanding kondisi harian. Di-cache per hari (bukan per panggilan) karena
    dipanggil berulang kali untuk SETIAP saham dalam satu scan.
    """
    today_str = datetime.datetime.now(core.WIB).strftime("%Y-%m-%d")
    if _ihsg_cache["date"] == today_str and _ihsg_cache.get("return_today") is not None:
        return _ihsg_cache["return_today"]

    try:
        hist = core.get_yf_ticker("^JKSE").history(period="5d", timeout=15)
        if len(hist) < 2:
            return None
        ihsg_return = ((hist["Close"].iloc[-1] - hist["Close"].iloc[-2]) / hist["Close"].iloc[-2]) * 100
        _ihsg_cache["date"] = today_str
        _ihsg_cache["return_today"] = ihsg_return
        return ihsg_return
    except Exception as e:
        print(f"⚠️ Gagal fetch IHSG untuk Relative Strength harian: {e}")
        return None


def fetch_macro_context():
    """
    Pulls broad market context known to meaningfully influence IDX day-to-day:
    the overnight Wall Street close (the single biggest external driver of how
    Asian markets open), regional Asian markets, USD/IDR (affects capital flows
    and export/commodity stocks differently), and crude oil (a major input for
    Indonesia's commodity-heavy index). All via yfinance — same free source as
    stock data, no new signup.
    """
    tickers = {
        "S&P 500 (Wall St overnight)": "^GSPC",
        "Nikkei 225 (Japan)": "^N225",
        "Hang Seng (Hong Kong)": "^HSI",
        "USD/IDR": "IDR=X",
        "Crude Oil WTI": "CL=F",
    }
    context = {}
    for label, symbol in tickers.items():
        try:
            hist = core.get_yf_ticker(symbol).history(period="5d", timeout=15)
            if len(hist) >= 2:
                pct_change = (hist["Close"].iloc[-1] - hist["Close"].iloc[-2]) / hist["Close"].iloc[-2] * 100
                context[label] = round(pct_change, 2)
        except Exception as e:
            print(f"⚠️ Failed to fetch macro ticker {symbol}: {e}")
    return context


def fetch_market_news_headlines(max_items=8):
    """
    Pulls recent real Indonesian market/economy headlines via Google News RSS —
    free, no API key required. Returns only actual fetched headline titles,
    nothing fabricated or paraphrased by the bot itself.
    """
    url = (
        "https://news.google.com/rss/search?"
        "q=IHSG+OR+%22bursa+efek%22+OR+ekonomi+Indonesia&hl=id&gl=ID&ceid=ID:id"
    )
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        root = ET.fromstring(response.content)
        items = root.findall(".//item")[:max_items]
        headlines = []
        for item in items:
            title_el = item.find("title")
            pubdate_el = item.find("pubDate")
            if title_el is not None and title_el.text:
                headlines.append({
                    "title": title_el.text,
                    "published": pubdate_el.text if pubdate_el is not None else "",
                })
        return headlines
    except Exception as e:
        print(f"⚠️ Failed to fetch news headlines: {e}")
        return []


# ---------------------------------------------------------------------
# Breadth + sector returns + regime (Phase 3b — NEW, see module docstring)
# ---------------------------------------------------------------------
def compute_market_breadth(results: list) -> dict:
    """
    Compute market breadth (advancers/decliners/unchanged) and average
    return per sector from the SAME `results` list NightlyEngine already
    produces every night — no new fetch, no new API. Each item already
    carries `sector` and `ret_1d_pct` from compute_factor_scoring().

    Called once, right after the nightly scan finishes, from
    run_nightly_full_scan() in engine/nightly.py.
    """
    advancers = decliners = unchanged = 0
    sector_returns: dict[str, list] = {}

    for r in results:
        if not r:
            continue
        ret = r.get("ret_1d_pct")
        if ret is None:
            continue  # can't classify this ticker's direction without a return figure
        if ret > 0:
            advancers += 1
        elif ret < 0:
            decliners += 1
        else:
            unchanged += 1
        sector = r.get("sector") or "N/A"
        sector_returns.setdefault(sector, []).append(ret)

    total = advancers + decliners + unchanged
    breadth_pct_advancing = round(advancers / total * 100, 1) if total else None

    sector_avg_returns_pct = {
        sector: round(sum(rets) / len(rets), 2)
        for sector, rets in sector_returns.items()
        if rets
    }
    # Strongest sector first — easiest to read in a Telegram message.
    sector_avg_returns_pct = dict(
        sorted(sector_avg_returns_pct.items(), key=lambda kv: kv[1], reverse=True)
    )

    ihsg_return_today = get_ihsg_return_today()
    regime = classify_market_regime(breadth_pct_advancing, ihsg_return_today)

    return {
        "universe": "nightly scan universe (lihat NightlyEngine untuk cakupan persisnya)",
        "total_scored": total,
        "advancers": advancers,
        "decliners": decliners,
        "unchanged": unchanged,
        "breadth_pct_advancing": breadth_pct_advancing,
        "sector_avg_returns_pct": sector_avg_returns_pct,
        "ihsg_return_today_pct": round(ihsg_return_today, 2) if ihsg_return_today is not None else None,
        "regime": regime,
    }


def classify_market_regime(breadth_pct_advancing, ihsg_return_today_pct) -> str:
    """
    Lightweight, SAME-DAY snapshot heuristic — combines today's breadth (%
    of the scanned universe that closed up) with today's IHSG return into
    one of five labels. This is NOT a proper multi-day trend/volatility
    regime model (no breadth history is persisted to detect that yet); it's
    a rough same-day read, cheap and transparent enough to sanity-check by
    eye. Thresholds are deliberately simple round numbers, not backtested.
    """
    if breadth_pct_advancing is None or ihsg_return_today_pct is None:
        return "UNKNOWN"
    if ihsg_return_today_pct <= -1.0 and breadth_pct_advancing < 35:
        return "RISK_OFF"
    if ihsg_return_today_pct >= 1.0 and breadth_pct_advancing > 65:
        return "STRONG_UPTREND"
    if breadth_pct_advancing > 55:
        return "MILD_UPTREND"
    if breadth_pct_advancing < 45:
        return "MILD_DOWNTREND"
    return "RANGING"


def save_market_context(breadth_data: dict):
    """
    Save the breadth/sector/regime snapshot to the shared cache, partition
    "market" (cache/market.pkl) — the partitioning the Executive Summary
    describes (eod.pkl / market.pkl / gpt.pkl).
    """
    meta = {"trading_day_marker": core.get_current_trading_day_close_marker()}
    ok = cache_manager.set("market", breadth_data, meta=meta)
    if ok:
        print(
            f"💾 Market context tersimpan (cache/market.pkl): "
            f"{breadth_data.get('advancers')} naik / {breadth_data.get('decliners')} turun "
            f"({breadth_data.get('breadth_pct_advancing')}% advancing), regime={breadth_data.get('regime')}"
        )
    else:
        print("⚠️ Gagal menyimpan market context — lihat log 'mbss.cache' untuk detail.")


def load_market_context() -> dict | None:
    """
    Return today's breadth/sector/regime snapshot, or None if it's missing
    or from a previous trading day (same staleness rule as
    engine/nightly.py's load_daily_scan_cache).
    """
    meta = cache_manager.get_meta("market")
    if not meta:
        return None
    current_marker = core.get_current_trading_day_close_marker()
    if meta.get("trading_day_marker") != current_marker:
        return None
    return cache_manager.get("market", default=None)
