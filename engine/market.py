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
import engine.nightly as nightly_engine

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


def get_ihsg_volatility_percentile(lookback_days: int = 120, window: int = 20) -> float | None:
    """
    MBSS v2 (AB-RC1 backbone, user request): where does TODAY's IHSG
    volatility (stdev of daily returns over the trailing `window` days)
    rank against its OWN trailing history — same adaptive-percentile
    convention already used everywhere else in this codebase (RSI, volume
    ratio, ADX) instead of a fixed magic-number vol threshold, which would
    need re-tuning every time IDX's baseline volatility regime shifts.

    Returns 0-100 (higher = more volatile than usual), or None if not
    enough history. Not cached per-day like get_ihsg_return_today() since
    this is only called once per nightly regime classification, not once
    per ticker.
    """
    try:
        hist = core.get_yf_ticker("^JKSE").history(period=f"{lookback_days + window}d", timeout=15)
        if len(hist) < window + 20:
            return None
        daily_returns = hist["Close"].pct_change().dropna()
        rolling_vol = daily_returns.rolling(window).std().dropna()
        if len(rolling_vol) < 20:
            return None
        current_vol = rolling_vol.iloc[-1]
        pct_rank = core.percentile_rank(rolling_vol.iloc[:-1], current_vol)
        return round(pct_rank * 100, 1)
    except Exception as e:
        print(f"⚠️ Gagal hitung IHSG volatility percentile: {e}")
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
    ihsg_vol_percentile = get_ihsg_volatility_percentile()
    regime = classify_market_regime(breadth_pct_advancing, ihsg_return_today, ihsg_vol_percentile)

    return {
        "universe": "nightly scan universe (lihat NightlyEngine untuk cakupan persisnya)",
        "total_scored": total,
        "advancers": advancers,
        "decliners": decliners,
        "unchanged": unchanged,
        "breadth_pct_advancing": breadth_pct_advancing,
        "sector_avg_returns_pct": sector_avg_returns_pct,
        "ihsg_return_today_pct": round(ihsg_return_today, 2) if ihsg_return_today is not None else None,
        "ihsg_volatility_percentile": ihsg_vol_percentile,
        "regime": regime,
    }


def classify_market_regime(breadth_pct_advancing, ihsg_return_today_pct, ihsg_vol_percentile=None) -> str:
    """
    MBSS v2 (AB-RC1 backbone, user request): same-day breadth+return
    snapshot, now split into R1-R5 (matching the AB-RC1 research doc's
    regime framework) by adding IHSG volatility percentile as a second
    dimension — distinguishes R1 Bull Stable from R2 Bull High Vol, which
    a return+breadth-only read can't do (previous version conflated the two
    into one "STRONG_UPTREND"/"MILD_UPTREND" label).

    Still a SAME-DAY snapshot heuristic, not a proper multi-day trend model
    — no regime history/persistence/hysteresis yet (a stock market can flip
    R1<->R3 day to day on borderline breadth numbers). Thresholds are
    deliberately simple round numbers for R4/R5 (not backtested — mirrors
    the old function's honesty about this), EXCEPT the volatility split,
    which uses the same adaptive-percentile convention as the rest of the
    scoring engine (see get_ihsg_volatility_percentile) rather than a fixed
    magic number.

    R1 Bull Stable / R2 Bull High Vol / R3 Sideways / R4 Risk Off /
    R5 Stress / R0 Unknown (insufficient data) — per AB-RC1 doc section 4.
    """
    if breadth_pct_advancing is None or ihsg_return_today_pct is None:
        return "R0_UNKNOWN"

    # R5 Stress — most severe risk-off, checked first (takes priority over R4).
    if ihsg_return_today_pct <= -2.0 and breadth_pct_advancing < 30:
        return "R5_STRESS"
    # R4 Risk Off — same threshold the old RISK_OFF label used.
    if ihsg_return_today_pct <= -1.0 and breadth_pct_advancing < 35:
        return "R4_RISK_OFF"

    is_bull = ihsg_return_today_pct > 0 and breadth_pct_advancing > 55
    if is_bull:
        # Volatility percentile missing (network hiccup, insufficient history)
        # -> default to the MORE CAUTIOUS label (High Vol) rather than assume
        # calm conditions without evidence.
        vol_high = ihsg_vol_percentile is None or ihsg_vol_percentile >= 70
        return "R2_BULL_HIGH_VOL" if vol_high else "R1_BULL_STABLE"

    # Everything else (ranging, or a down day that doesn't clear R4's bar) —
    # R3 Sideways per AB-RC1's posture ("use the validated moderate gate").
    return "R3_SIDEWAYS"


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


def get_sector_rank_info(sector: str) -> dict | None:
    """
    MBSS v2 (user request — sentimen sektoral sebagai penguat sinyal): kalau
    sebuah saham dari sektor yang MALAM INI rata-ratanya kuat, itu konteks
    tambahan yang berguna ("saham ini bukan cuma bagus sendirian, sektornya
    juga lagi ramai"). Baca dari cache market context yang SUDAH dihitung
    tiap malam (compute_market_breadth), tidak fetch apa pun baru.
    Return None kalau data sektor tidak ada / sektor "N/A".
    """
    if not sector or sector == "N/A":
        return None
    breadth = load_market_context()
    if not breadth:
        return None
    sectors = breadth.get("sector_avg_returns_pct", {})
    if sector not in sectors:
        return None
    ranked = list(sectors.items())  # sudah terurut kuat->lemah dari compute_market_breadth
    rank = next((i for i, (s, _) in enumerate(ranked, 1) if s == sector), None)
    info = {"sector": sector, "avg_return_pct": sectors[sector], "rank": rank, "total_sectors": len(ranked)}

    # MBSS v2 (RapidAPI integration) — perkaya rank same-day avg-return di
    # atas (sudah ada bertahun-tahun) dengan momentum/status/rekomendasi/
    # foreign-flow riil dari RapidAPI, KALAU ada. Byte-identical fallback
    # kalau data RapidAPI tidak tersedia (kuota habis, endpoint down,
    # sektor tidak match nama) — caller lama tidak perlu berubah sama
    # sekali, cuma dapat field TAMBAHAN kalau kebetulan ada.
    try:
        rapidapi_sectors = (nightly_engine.load_rapidapi_market_intelligence().get("sector_rotation") or {}).get("all_sectors") or []
        match = next((s for s in rapidapi_sectors if s.get("sector_name") == sector), None)
        if match:
            info["momentum_score"] = match.get("momentum_score")
            info["status"] = match.get("status")
            info["recommendation"] = match.get("recommendation")
            info["foreign_flow"] = match.get("foreign_flow")
    except Exception:
        pass  # graceful fallback — info tetap valid tanpa field tambahan ini

    return info


def format_sector_tag(sector: str, prefix: str = "\n   ") -> str:
    """
    MBSS v2 (user request — sentimen sektoral di SEMUA tools, bukan cuma
    /hc & /consensus): formatter satu pintu supaya tampilannya konsisten
    di mana pun dipakai. Return string kosong kalau data sektor tidak ada
    (None-safe, aman dipakai tanpa cek terpisah di tiap command).

    MBSS v2 (RapidAPI integration): tambahan status/rekomendasi momentum
    sektor di akhir baris KALAU ada di get_sector_rank_info — tidak ada
    perubahan kalau datanya tidak tersedia. Sumber data TIDAK disebut di
    teks (konvensi teks user-facing sesi ini) — tampil sebagai perluasan
    natural dari label sektor yang sudah ada, bukan callout API eksternal.
    """
    info = get_sector_rank_info(sector)
    if not info:
        return ""
    base = f"{prefix}🏭 Sektor {info['sector']}: #{info['rank']}/{info['total_sectors']} terkuat ({info['avg_return_pct']:+.1f}% avg)"
    if info.get("status") and info.get("recommendation"):
        base += f" | Momentum: {info['status']} → {info['recommendation']}"
    return base
