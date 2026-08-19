from __future__ import annotations
import os
import re
import json
import pickle
import copy
import time
import asyncio
import logging
import datetime
import sqlite3
import requests
import xml.etree.ElementTree as ET
import email.utils

# NOTE (MBSS v2 refactor): this file now lives at engine/legacy_core.py, one
# level below the project root where bot.py, .env, portfolio.json, the
# OHLCV DB, and every other data file actually live. PROJECT_ROOT centralizes
# that "go up one level" so every path constant in this file (there used to
# be ~10 separate os.path.dirname(os.path.abspath(__file__)) calls, each
# silently pointing at the wrong folder after the move) stays correct.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Load .env file — pakai python-dotenv kalau tersedia, fallback manual kalau tidak
_env_path = os.path.join(PROJECT_ROOT, ".env")
try:
    from dotenv import load_dotenv

    if os.path.exists(_env_path):
        load_dotenv(_env_path)
        print(f"✅ .env loaded dari {_env_path}")
    else:
        load_dotenv()
        print("⚠️ File .env tidak ditemukan — memakai environment dari sistem")
except ImportError:
    # Fallback manual parser kalau python-dotenv belum terinstall
    # Install dengan: pip install python-dotenv --break-system-packages
    if os.path.exists(_env_path):
        with open(_env_path, encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    os.environ.setdefault(_k.strip(), _v.strip())
        print("✅ .env loaded (manual parser, install python-dotenv untuk versi lebih robust)")
    else:
        print("⚠️ File .env tidak ditemukan — memakai environment dari sistem")

# ==========================================
# ⏱️ HARD TIMEOUT AT THE ACTUAL NETWORK LAYER (not just asyncio.wait_for)
# ==========================================
# asyncio.wait_for(asyncio.to_thread(...), timeout=X) cancels OUR wait, but does NOT
# kill the underlying thread if it's stuck on a blocking network call — that thread
# stays occupied forever. yfinance's `.info` property in particular doesn't accept a
# timeout argument at all, so a single stalled request there can permanently consume
# a thread-pool worker. Once enough of these pile up (the default pool is small,
# often ~8-12 threads), every subsequent ticker fetch queues forever waiting for a
# free worker that never returns — looking exactly like a silent freeze. Fixed by
# forcing every yfinance HTTP call, regardless of which method triggers it, through
# a session with a hard timeout enforced at the actual socket level.
class _TimeoutSession(requests.Session):
    def __init__(self):
        super().__init__()
        # Without curl_cffi, our requests lack real browser TLS fingerprinting —
        # this can't fully replicate that, but basic header-level bot detection is
        # common enough that realistic browser headers may help somewhat on their own.
        self.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
        })

    def request(self, *args, **kwargs):
        kwargs.setdefault("timeout", 15)
        return super().request(*args, **kwargs)


_YF_SESSION = _TimeoutSession()


def get_yf_ticker(symbol: str):
    """
    Use this instead of yf.Ticker(...) directly everywhere in this script.

    BUGFIX (ditemukan lewat migrasi ke VM baru — pip install menarik versi
    yfinance JAUH lebih baru dari yang biasa dipakai, dan versi baru itu
    WAJIB pakai session curl_cffi internal, MENOLAK total custom
    requests.Session seperti _TimeoutSession yang kita bikin sebelumnya —
    hasilnya 0/314 ticker berhasil, semua gagal dengan pesan error yang
    sebenarnya sudah kasih tahu solusinya sendiri: "stop setting session,
    let YF handle." Jadi SEKARANG session TIDAK di-pass lagi — yfinance
    yang urus sendiri secara internal. Konsekuensinya: proteksi timeout
    level-socket dari _TimeoutSession (lihat komentar class-nya di atas,
    dibuat buat cegah thread macet) tidak lagi aktif — tapi parameter
    timeout=... yang di-pass langsung ke .history() di tiap pemanggil
    (yfinance_get_kline dkk) tetap jalan sebagai lapisan proteksi kedua.
    """
    return yf.Ticker(symbol)


def yfinance_get_kline(ticker: str, period: str = "2y") -> pd.DataFrame:
    """
    Fetch EOD OHLCV bars from Yahoo Finance for one IDX ticker.
    Returns columns: Open, High, Low, Close, Volume indexed by date.
    """
    try:
        stock = get_yf_ticker(f"{ticker}.JK")
        df = yf_fetch_with_retry(
            lambda: stock.history(period=period, interval="1d", auto_adjust=True, timeout=1800)
        )
        if df is None or df.empty:
            return pd.DataFrame()
        cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
        if len(cols) < 5:
            return pd.DataFrame()
        return df[cols].dropna()
    except Exception as e:
        print(f"⚠️ Gagal fetch daily Yahoo Finance untuk {ticker}: {e}")
        return pd.DataFrame()


def yfinance_get_intraday_5m(ticker: str, period: str = "5d") -> pd.DataFrame:
    """Fetch 5-minute bars from Yahoo Finance for intraday analysis."""
    try:
        stock = get_yf_ticker(f"{ticker}.JK")
        df = yf_fetch_with_retry(lambda: stock.history(period=period, interval="5m", timeout=15))
        if df is None or df.empty:
            return pd.DataFrame()
        cols = [c for c in ["Open", "High", "Low", "Close", "Volume"] if c in df.columns]
        if len(cols) < 5:
            return pd.DataFrame()
        return df[cols].dropna()
    except Exception as e:
        print(f"⚠️ Gagal fetch 5m Yahoo Finance untuk {ticker}: {e}")
        return pd.DataFrame()


def yfinance_get_live_quote(ticker: str) -> dict | None:
    """Return a lightweight live snapshot from Yahoo intraday bars for current-session use."""
    intraday = yfinance_get_intraday_5m(ticker, period="5d")
    if intraday is None or intraday.empty:
        return None
    try:
        last = intraday.iloc[-1]
        last_price = float(last.get("Close", last.get("Open", 0)) or 0)
        return {
            "ld": last_price,
            "h": float(intraday["High"].max()),
            "l": float(intraday["Low"].min()),
            "v": int(float(intraday["Volume"].sum())) if "Volume" in intraday else None,
            "ts": 0,
            "src": "yfinance",
            "bars": int(len(intraday)),
        }
    except Exception as e:
        print(f"⚠️ Gagal bentuk live quote Yahoo untuk {ticker}: {e}")
        return None


def yfinance_breakout_context(ticker: str) -> dict:
    """
    Intraday breakout probability score dari 5-menit bars.
    Score 0-100, label TINGGI/SEDANG/RENDAH.

    Perbaikan dari versi sebelumnya:
    - Resistance dari cluster level (bukan single max spike) — lebih valid
    - RSI > 70 dapat penalty karena mendekati overbought saat breakout = risiko
    - change_pct sesungguhnya dari awal sesi (bukan 0.0)
    - Jarak resistance dinormalisasi terhadap ATR sesi (bukan persentase flat)
    """
    session = get_current_idx_session()
    if session is None:
        return {"available": False, "reason": "di luar jam bursa"}

    bars = yfinance_get_intraday_5m(ticker, period="5d")
    if bars is None or bars.empty:
        return {"available": False, "reason": "data intraday belum tersedia"}

    now = datetime.datetime.now(WIB)
    is_friday = now.weekday() == 4
    session_start_time = (
        datetime.time(9, 0) if session == "sesi_1"
        else (datetime.time(14, 0) if is_friday else datetime.time(13, 30))
    )
    session_start_dt = datetime.datetime.combine(now.date(), session_start_time, tzinfo=WIB)
    session_bars = bars[bars.index >= session_start_dt]
    if len(session_bars) < 8:
        return {"available": False, "reason": "sesi baru saja mulai, data belum cukup (<8 bar)"}

    closes  = session_bars["Close"]
    highs   = session_bars["High"]
    lows    = session_bars["Low"]
    volumes = session_bars["Volume"]

    current_price = float(closes.iloc[-1])

    # --- Resistance dari 70% PERTAMA sesi (bukan seluruh sesi) ---
    # Closing cluster (harga penutupan yang disentuh berkali-kali di akhir sesi)
    # selalu jadi "resistance" kalau pakai seluruh data — menyebabkan false positive
    # untuk saham sideways manapun (BBCA, TLKM, ANTM semua jadi TINGGI).
    # Dengan 70% pertama, resistance adalah level yang genuinely menghadang
    # price action SEBELUM kondisi terkini terbentuk.
    n_history = max(int(len(session_bars) * 0.70), min(8, len(session_bars)))
    history_highs = highs.iloc[:n_history]
    high_values = history_highs.values
    best_resistance = None
    best_count = 0
    for ref in high_values:
        radius = ref * 0.005
        count = sum(1 for h in high_values if abs(h - ref) <= radius)
        if count > best_count or (count == best_count and ref > (best_resistance or 0)):
            best_count = count
            best_resistance = ref
    resistance = float(best_resistance) if best_resistance else float(history_highs.max())

    # ATR sesi untuk normalisasi jarak (bukan persentase flat)
    atr_session = float((highs - lows).mean())
    distance_pct = ((resistance - current_price) / max(resistance, 1e-9)) * 100
    distance_in_atr = (resistance - current_price) / max(atr_session, 1e-9)

    # Volume
    lookback = min(20, len(session_bars))
    avg_vol  = float(volumes.tail(lookback).mean())
    vol_ratio = float(volumes.iloc[-1] / max(avg_vol, 1e-9))

    # EMA bias
    ema5  = closes.ewm(span=5, adjust=False).mean().iloc[-1]
    ema20 = closes.ewm(span=20, adjust=False).mean().iloc[-1]
    rsi14 = float(calculate_rsi(closes, period=14).iloc[-1]) if len(closes) >= 14 else None

    # --- Scoring ---
    score = 0
    breakout_status_label = ""
    volume_warning = ""

    # Kedekatan/posisi vs resistance (dinormalisasi ATR)
    # Dibedakan pre-breakout vs post-breakout — keduanya dapat 25 poin tapi
    # dengan label berbeda supaya user tahu kondisi sebenarnya.
    if distance_in_atr < -1.0:
        score += 25
        breakout_status_label = f"POST-BREAKOUT ({abs(distance_in_atr):.1f} ATR di atas resist)"
    elif distance_in_atr < 0:
        score += 25
        breakout_status_label = "BARU SAJA BREAKOUT"
    elif distance_in_atr <= 0.3:
        score += 25
        breakout_status_label = "TEPAT DI RESISTANCE"
    elif distance_in_atr <= 0.8:
        score += 18
        breakout_status_label = "DEKAT RESISTANCE"
    elif distance_in_atr <= 1.5:
        score += 10
        breakout_status_label = "MENDEKAT"
    else:
        score += 3
        breakout_status_label = "JAUH DARI RESISTANCE"

    # Volume breakout — warning khusus kalau volume kering di area resistance
    if vol_ratio >= 2.5:   score += 25
    elif vol_ratio >= 1.8: score += 18
    elif vol_ratio >= 1.3: score += 12
    elif vol_ratio >= 0.8: score += 6
    else:
        score += 2
        if distance_in_atr <= 0.3:  # breakout tanpa volume = potensi fake-out
            volume_warning = "⚠️ Volume rendah — waspadai fake-out"

    # EMA bias
    score += 20 if ema5 > ema20 else 7

    # RSI — bonus kalau di zona optimal (55-70), PENALTY kalau overbought (>70)
    if rsi14 is not None:
        if 55 <= rsi14 <= 70:    score += 15  # optimal untuk breakout
        elif 45 <= rsi14 < 55:   score += 8   # netral
        elif rsi14 > 70:         score += 3   # mendekati overbought = risiko
        else:                    score += 5   # oversold

    # Higher low (akumulasi)
    higher_low = lows.iloc[-1] >= float(lows.tail(min(5, len(lows))).min())
    score += 15 if higher_low else 5

    # Cluster resistance bonus (lebih valid kalau level disentuh >1 kali)
    if best_count >= 3:  score += 5
    elif best_count == 2: score += 2

    # Cap score maksimal 55 kalau volume sangat kering (<0.3x) — breakout
    # tanpa volume tidak valid, SMSM 14:30 contoh: DI RESIST tapi vol 0.12x
    # sehingga dapat 75 padahal tidak ada konfirmasi volume sama sekali.
    if vol_ratio < 0.3 and score > 55:
        score = 55
    score = int(max(0, min(100, score)))
    label = "TINGGI" if score >= 75 else "SEDANG" if score >= 55 else "RENDAH"

    return {
        "available":      True,
        "session":        session,
        "score":          score,
        "label":          label,
        "breakout_status_label": breakout_status_label,
        "volume_warning": volume_warning,
        "current_price":  round(current_price, 2),
        "resistance":     round(resistance, 2),
        "resistance_cluster_count": best_count,
        "distance_pct":   round(distance_pct, 2),
        "distance_in_atr": round(distance_in_atr, 2),
        "volume_ratio":   round(vol_ratio, 2),
        "ema5":           round(float(ema5), 2),
        "ema20":          round(float(ema20), 2),
        "ema_bias":       "bullish" if ema5 > ema20 else "bearish",
        "rsi14":          round(rsi14, 2) if rsi14 is not None else None,
        "higher_low":     higher_low,
        "atr_session":    round(atr_session, 2),
    }


# ==========================================
# 📡 ITICK API CLIENT (replaces yfinance for price/history/technical data)
# ==========================================
# Confirmed via live testing: batching (multiple codes per call) is broken for
# region=ID specifically (works fine for region=HK — likely a backend bug worth
# reporting to iTick support), so this uses one call per ticker. Testing also
# confirmed a rolling rate-limit window: roughly the first ~12 requests succeed,
# then a wall of failures, which fully clears after ~65-70 seconds regardless of
# how slowly requests were paced. So rather than a flat per-request delay, this
# processes tickers in chunks with a deliberate cooldown between chunks.
ITICK_API_KEY  = os.environ.get("ITICK_API_KEY", "")
ITICK_BASE_URL = os.environ.get("ITICK_BASE_URL", "https://api0.itick.org")
ITICK_HEADERS  = {"accept": "application/json", "token": ITICK_API_KEY}
ITICK_CHUNK_SIZE = 10       # legacy name; used as generic fetch chunk size
ITICK_COOLDOWN_SECONDS = 20  # legacy name; generic cooldown between fetch chunks


def itick_get_kline(ticker, limit=500):
    """
    Compatibility wrapper: iTick is no longer required; this now returns Yahoo
    Finance EOD bars so older call sites keep working.
    """
    try:
        period = "10d" if limit and limit <= 20 else "2y"
        df = yfinance_get_kline(ticker, period=period)
        if df is None or df.empty:
            return None
        return df.tail(limit) if limit else df
    except Exception as e:
        print(f"⚠️ Yahoo daily fetch gagal untuk {ticker}: {e}")
        return None


def itick_get_quote(ticker):
    """
    Compatibility wrapper for live snapshot. Uses Yahoo Finance 5m bars and
    returns a lightweight quote-like dict.
    """
    return yfinance_get_live_quote(ticker)



def yf_fetch_with_retry(fetch_fn, max_retries=2, base_delay=5):
    """
    Wraps a yfinance call with retry-with-backoff, but ONLY for rate-limit errors
    ("Too Many Requests" / "rate limit") — a genuinely bad ticker (delisted, wrong
    symbol) fails immediately without wasting retries on something that will never
    succeed. Without this, running the full ISSI universe (70 tickers x 2 calls
    each) in quick succession reliably triggers Yahoo's rate limiting on most of
    them, since there's no browser-TLS impersonation available on this platform.

    Kept deliberately short (max 2 attempts, modest backoff) — retry delays here use
    a BLOCKING time.sleep() inside a background thread, so if this runs too long it
    can exceed the outer asyncio.wait_for timeout wrapping the whole ticker fetch,
    recreating the exact "stuck thread past its timeout" problem this was meant to
    avoid, just relocated into the retry logic itself.
    """
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            return fetch_fn()
        except Exception as e:
            error_text = str(e).lower()
            is_rate_limit = "too many requests" in error_text or "rate limit" in error_text
            last_error = e
            if not is_rate_limit:
                raise  # genuine error (bad ticker, delisted, etc) — don't retry
            if attempt < max_retries:
                delay = base_delay * attempt  # 5s
                print(f"⏳ Rate limited, retrying in {delay}s (attempt {attempt}/{max_retries})...")
                time.sleep(delay)
    raise last_error

import pandas as pd
import yfinance as yf
from engine.cache import cache_manager
# NOTE (MBSS v2 refactor, Phase 2/3/4): the nightly EOD pipeline moved to
# engine/nightly.py (NightlyEngine); IHSG/macro/news context + breadth/sector/
# regime moved to engine/market.py (MarketContextEngine); broker-summary
# fetch/compute/cache moved to engine/broker.py (BrokerEngine). All imported
# as MODULES (not `from engine.x import name`) — that form works no matter
# which of these mutually-dependent modules happens to be imported first; a
# named import would break if engine.nightly/market/broker were ever
# imported before this file. See the module docstrings in engine/nightly.py,
# engine/market.py, and engine/broker.py.
import engine.nightly as nightly_engine
import engine.market as market_engine
import engine.broker as broker_engine
# NOTE (MBSS v2 refactor, Phase 5a): first Command Layer module. build_app()
# below needs these handler functions to register them; commands/scan.py
# needs core.xxx for the deep scoring/ranking helpers it calls. Same
# two-way dependency as engine/nightly.py etc., same fix: MODULE import.
import commands.scan as commands_scan
import commands.misc as commands_misc
import commands.portfolio as commands_portfolio
import commands.check as commands_check
import commands.chat as commands_chat
import engine.scoring as scoring_engine
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.error import BadRequest
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, CallbackQueryHandler, MessageHandler, filters
from telegram.request import HTTPXRequest
import base64
# google-genai SDK diganti REST API langsung (tidak butuh cryptography/cffi)
GEMINI_REST_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_MODEL    = "gemini-3.1-flash-lite"

def _gemini_rest(contents: list, system_instruction: str = None, model: str = None, timeout: int = 60) -> str:
    url  = GEMINI_REST_URL.format(model=model or GEMINI_MODEL)
    body = {"contents": contents}
    if system_instruction:
        body["system_instruction"] = {"parts": [{"text": system_instruction}]}
    resp = requests.post(url, params={"key": GEMINI_API_KEY}, json=body, timeout=timeout)
    resp.raise_for_status()
    data = resp.json()
    try:
        return data["candidates"][0]["content"]["parts"][0]["text"]
    except (KeyError, IndexError) as e:
        raise ValueError(f"Unexpected Gemini response structure: {data}") from e

def _gemini_text(prompt: str, system_instruction: str = None, **kwargs) -> str:
    return _gemini_rest([{"role": "user", "parts": [{"text": prompt}]}], system_instruction=system_instruction, **kwargs)

def _gemini_image_text(image_bytes: bytes, mime_type: str, text_prompt: str, **kwargs) -> str:
    return _gemini_rest([{"role": "user", "parts": [
        {"inline_data": {"mime_type": mime_type, "data": base64.b64encode(image_bytes).decode()}},
        {"text": text_prompt},
    ]}], **kwargs)

# ==========================================
# 🔇 SILENCE NOISY DEBUG LOGS (yfinance / urllib3 / requests)
# ==========================================
logging.basicConfig(level=logging.WARNING)
for _noisy in ("yfinance", "urllib3", "peewee", "httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

# ==========================================
# 🔐 CONFIG — dibaca dari .env (lihat mbss.env untuk template)
# ==========================================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")
GEMINI_API_KEY     = os.environ.get("GEMINI_API_KEY", "")

_missing = [k for k, v in {
    "TELEGRAM_BOT_TOKEN": TELEGRAM_BOT_TOKEN,
    "TELEGRAM_CHAT_ID":   TELEGRAM_CHAT_ID,
    "GEMINI_API_KEY":     GEMINI_API_KEY,
    "ZAPI_API_KEY":       os.environ.get("ZAPI_API_KEY", ""),
    "INDEXALPHA_API_TOKEN": os.environ.get("INDEXALPHA_API_TOKEN", ""),
}.items() if not v]
if _missing:
    print(f"⚠️ Key berikut KOSONG (cek file .env): {', '.join(_missing)}")
else:
    print("✅ Semua API keys loaded dari .env")

  # Gemini diakses via REST (_gemini_text / _gemini_image_text), tidak perlu client SDK

WIB = datetime.timezone(datetime.timedelta(hours=7))  # Asia/Jakarta, no DST

# ==========================================
# 🕌 DYNAMIC SHARIA (JII) FILTER
# ==========================================
# NOTE (MBSS v2 refactor, Phase 3): get_ihsg_return_today(), fetch_macro_context(),
# fetch_market_news_headlines(), and the _ihsg_cache state moved to
# engine/market.py (MarketContextEngine). Call sites below use
# `market_engine.xxx` — see the import near the top of this file.



# MBSS v2 (user request, real case: "TEBE menjadi top gainer" muncul sebagai
# "berita" /check — padahal itu cuma roundup harian yang mendeskripsikan
# ULANG pergerakan harga itu sendiri, sirkular, bukan berita fundamental/
# sentimen yang MENGGERAKKAN harga). Judul yang match salah satu frasa ini
# dibuang sebelum ditampilkan.
NEWS_NOISE_KEYWORDS_ID = [
    "top gainer", "top gainers", "top loser", "top losers",
    "penggerak ihsg", "saham penggerak", "saham-saham penggerak",
    "top laggard", "kamus saham", "rekomendasi saham hari ini",
    "saham pilihan hari ini", "saham top",
]


def fetch_company_news(ticker, company_name, max_items=3, days_back=30):
    """
    Pulls recent real news scoped to a SPECIFIC company (not general market news) —
    this is what can actually surface corporate actions like buybacks, earnings
    releases, rights issues, or lawsuits, since the general market query is too
    broad to reliably catch single-company stories. Free via Google News RSS.

    MBSS v2 (user request): dibatasi ke `days_back` hari terakhir dan
    memfilter roundup harian "top gainer/penggerak" (lihat
    NEWS_NOISE_KEYWORDS_ID) — ambil pool lebih besar dulu dari RSS (15 item
    mentah), baru difilter tanggal+keyword, supaya filter tidak mengurangi
    count hasil akhir di bawah max_items kalau berita relevan sebenarnya ada.
    """
    query = f"{company_name} OR {ticker} saham"
    url = f"https://news.google.com/rss/search?q={requests.utils.quote(query)}&hl=id&gl=ID&ceid=ID:id"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        root = ET.fromstring(response.content)
        items = root.findall(".//item")[:15]  # pool mentah sebelum filter
        cutoff = datetime.datetime.now(WIB) - datetime.timedelta(days=days_back)
        headlines = []
        for item in items:
            title_el = item.find("title")
            pubdate_el = item.find("pubDate")
            if title_el is None or not title_el.text:
                continue
            title = title_el.text
            if any(kw in title.lower() for kw in NEWS_NOISE_KEYWORDS_ID):
                continue
            published = pubdate_el.text if pubdate_el is not None else ""
            if published:
                try:
                    pub_dt = email.utils.parsedate_to_datetime(published)
                    if pub_dt.tzinfo is None:
                        pub_dt = pub_dt.replace(tzinfo=datetime.timezone.utc)
                    if pub_dt < cutoff:
                        continue
                except Exception:
                    pass  # tanggal tidak bisa diparse -- tetap ditampilkan, jangan buang cuma karena parsing gagal
            headlines.append({"title": title, "published": published})
            if len(headlines) >= max_items:
                break
        return headlines
    except Exception as e:
        print(f"⚠️ Failed to fetch company news for {ticker}: {e}")
        return []


def enrich_news_with_price_reaction(news_items: list, hist: pd.DataFrame, current_price: float) -> list:
    """
    MBSS v2 (user request — real case: /check NELY showed a Rp2T
    acquisition headline with no way to tell if the price had already
    reacted to it, same question for GIAA's Pelita Air acquisition news).
    For each headline, compute price movement since ITS publish date using
    OHLCV we already hold locally (get_ohlcv_smart's SQLite EOD cache) —
    zero extra API cost, purely a local lookup — so both the person and
    Gemini can see "has the market already moved on this, or is it still
    fresh" instead of guessing from the headline text alone.

    Adds a "price_reaction" key ({"days_ago": int, "price_change_since_pct":
    float}) to headlines whose pubDate we can parse AND that falls within
    `hist`'s window. Silently omits it otherwise (unparseable date, or
    headline older than the history window) — no crash, no fabricated
    numbers, that headline just shows without reaction context.

    CAVEAT: pubDate is when Google News surfaced this ARTICLE, not
    necessarily the exact date of the underlying event — a republished or
    follow-up piece can carry a recent pubDate for old news. This is
    "price behavior since the article appeared", a strong proxy, not a
    guarantee of the event's true timing.
    """
    if hist is None or hist.empty:
        return news_items

    enriched = []
    for item in news_items:
        item = dict(item)
        published = item.get("published")
        if published and current_price:
            try:
                pub_date = email.utils.parsedate_to_datetime(published).date()
                # Ticker mula pertama di/setelah tanggal artikel — close
                # paling awal yang mungkin sudah mencerminkan reaksi pasar.
                on_or_after = hist.index[hist.index.date >= pub_date]
                if len(on_or_after) > 0:
                    price_then = float(hist.loc[on_or_after[0], "Close"])
                    if price_then > 0:
                        item["price_reaction"] = {
                            "days_ago": (datetime.datetime.now(WIB).date() - pub_date).days,
                            "price_change_since_pct": round((current_price - price_then) / price_then * 100, 1),
                        }
            except Exception:
                pass  # pubDate tidak bisa di-parse — headline tetap tampil, tanpa konteks reaksi harga
        enriched.append(item)
    return enriched


def fetch_recent_corporate_actions(ticker, months_back=12):
    """
    Pulls REAL, structured dividend and stock-split history via yfinance — not
    news search, actual recorded corporate action data. Only returns actions
    within the lookback window so old history doesn't clutter the output.

    NOTE: This does NOT include buyback announcements, rights issues, or
    financial report releases — those live in IDX's own disclosure system
    (keterbukaan informasi) which isn't accessible here. Dividends/splits are
    the only structured corporate action data yfinance reliably provides.
    """
    try:
        stock = get_yf_ticker(f"{ticker}.JK")
        cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=months_back * 30)

        dividends = stock.dividends
        recent_dividends = []
        if dividends is not None and not dividends.empty:
            recent = dividends[dividends.index >= cutoff]
            for date, amount in recent.items():
                recent_dividends.append({"date": str(date.date()), "amount": round(float(amount), 2)})

        splits = stock.splits
        recent_splits = []
        if splits is not None and not splits.empty:
            recent = splits[splits.index >= cutoff]
            for date, ratio in recent.items():
                recent_splits.append({"date": str(date.date()), "ratio": float(ratio)})

        return {"recent_dividends": recent_dividends, "recent_splits": recent_splits}
    except Exception as e:
        print(f"⚠️ Failed to fetch corporate actions for {ticker}: {e}")
        return {"recent_dividends": [], "recent_splits": []}


# NOTE (MBSS v2 refactor, Phase 3): fetch_market_news_headlines() moved to
# engine/market.py (MarketContextEngine) — call sites use market_engine.fetch_market_news_headlines.


DAFTAR_SAHAM_SYARIAH_FILE = os.path.join(PROJECT_ROOT, "daftar_saham_syariah_resmi.json")


def fetch_online_sharia_list(index_key: str = "ISSI"):
    """
    MBSS v2 (user request): DIKUNCI ke daftar resmi IDX Islamic
    (idxislamic.idx.co.id/whats-on-idx-islamic/daftar-saham-syariah/),
    bukan lagi fetch dinamis dari Gist pihak ketiga. User akan update file
    `daftar_saham_syariah_resmi.json` secara manual/berkala kalau ada
    ketidaksesuaian vs app broker — TIDAK ada fetch jaringan di sini sama
    sekali, jadi tidak bisa gagal karena masalah koneksi/rate-limit.

    index_key="ISSI" -> 576 konstituen ISSI (default, dipakai semua caller
    saat ini). index_key lain (mis. "DSS"/"ALL") -> seluruh 615 saham
    syariah resmi (termasuk yang bukan konstituen ISSI). "JII" tidak
    tersedia dari sumber baru ini (sumber lama juga jarang benar-benar
    dipakai untuk JII) — fallback ke ISSI dengan peringatan.
    """
    try:
        with open(DAFTAR_SAHAM_SYARIAH_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"❌ Gagal membaca daftar saham syariah terkunci ({DAFTAR_SAHAM_SYARIAH_FILE}): {e}")
        print("   Menggunakan fallback safety list kecil — cek file-nya!")
        return {"TLKM", "ADRO", "BRIS", "ANTM", "UNTR", "KLBF", "INDF", "ICBP", "ASII"}

    as_of = data.get("as_of", "?")
    if index_key == "ISSI":
        tickers = set(data.get("issi_tickers", []))
        print(f"📋 Daftar syariah terkunci (IDX Islamic resmi, per {as_of}): {len(tickers)} konstituen ISSI")
        return tickers
    if index_key == "JII":
        print("⚠️ JII tidak tersedia dari daftar resmi terkunci — fallback ke ISSI.")
        tickers = set(data.get("issi_tickers", []))
        print(f"📋 Daftar syariah terkunci (IDX Islamic resmi, per {as_of}): {len(tickers)} konstituen ISSI")
        return tickers

    tickers = set(data.get("all_tickers_dss", []))
    print(f"📋 Daftar syariah terkunci (IDX Islamic resmi, per {as_of}): {len(tickers)} saham (seluruh DES/DSS)")
    return tickers


# ==========================================
# 💼 PORTFOLIO STORAGE (simple local JSON file — private to this device)
# ==========================================
PORTFOLIO_FILE = os.path.join(PROJECT_ROOT, "portfolio.json")
BOARD_LOT_SIZE = 100  # IDX standard: 1 lot = 100 shares

PORTFOLIO_SCHEMA_DEFAULT = {
    "positions": {},   # {"TICKER": {"lots": int, "avg_price": float}, ...}
    "cash": 0.0,
    "realized_pnl_log": [],  # [{"date": "...", "ticker": "...", "lots": int, "sell_price": float, "avg_cost": float, "pnl_idr": float}, ...]
    "watchlist": [],   # ["TICKER", ...], max 3 — near-term buy candidates, not owned yet
}
WATCHLIST_MAX_SIZE = 3


def load_portfolio() -> dict:
    """Returns the full portfolio dict: {"positions": {...}, "cash": float, "realized_pnl_log": [...]}"""
    if not os.path.exists(PORTFOLIO_FILE):
        return copy.deepcopy(PORTFOLIO_SCHEMA_DEFAULT)
    try:
        with open(PORTFOLIO_FILE, "r") as f:
            data = json.load(f)
        # Defensive: fill in any missing keys rather than crash on an older/partial file
        for key, default_val in PORTFOLIO_SCHEMA_DEFAULT.items():
            if key not in data:
                data[key] = default_val
        return data
    except Exception as e:
        print(f"⚠️ Failed to read portfolio file: {e}")
        return copy.deepcopy(PORTFOLIO_SCHEMA_DEFAULT)


def save_portfolio(portfolio: dict):
    with open(PORTFOLIO_FILE, "w") as f:
        json.dump(portfolio, f, indent=2)


def get_cash_balance() -> float:
    return load_portfolio().get("cash", 0.0)


def add_cash(amount: float) -> float:
    portfolio = load_portfolio()
    portfolio["cash"] = portfolio.get("cash", 0.0) + amount
    save_portfolio(portfolio)
    return portfolio["cash"]


def withdraw_cash(amount: float):
    """Returns (success: bool, message: str, new_balance: float)."""
    portfolio = load_portfolio()
    current = portfolio.get("cash", 0.0)
    if amount > current:
        return False, f"Cash tidak cukup. Tersedia Rp{current:,.0f}, diminta Rp{amount:,.0f}.", current
    portfolio["cash"] = current - amount
    save_portfolio(portfolio)
    return True, f"Berhasil menarik Rp{amount:,.0f}.", portfolio["cash"]


def add_to_watchlist(ticker: str):
    """Returns (success: bool, message: str). Capped at WATCHLIST_MAX_SIZE."""
    portfolio = load_portfolio()
    watchlist = portfolio.setdefault("watchlist", [])
    if ticker in watchlist:
        return False, f"{ticker} sudah ada di watchlist."
    if len(watchlist) >= WATCHLIST_MAX_SIZE:
        return False, (
            f"Watchlist sudah penuh ({WATCHLIST_MAX_SIZE} saham: {', '.join(watchlist)}).\n"
            f"Hapus salah satu dulu dengan /watchlist remove TICKER."
        )
    watchlist.append(ticker)
    save_portfolio(portfolio)
    return True, f"{ticker} ditambahkan ke watchlist ({len(watchlist)}/{WATCHLIST_MAX_SIZE})."


def remove_from_watchlist(ticker: str):
    """Returns (success: bool, message: str)."""
    portfolio = load_portfolio()
    watchlist = portfolio.setdefault("watchlist", [])
    if ticker not in watchlist:
        return False, f"{ticker} tidak ada di watchlist."
    watchlist.remove(ticker)
    save_portfolio(portfolio)
    return True, f"{ticker} dihapus dari watchlist."


def add_position(ticker: str, price: float, lots: int):
    """
    Returns (success: bool, message: str, position_or_none). Hard-blocks the buy if
    total cost exceeds tracked cash — a trade can't actually settle with money that
    isn't there, so this is caught before it's ever recorded, not after.
    """
    total_cost = price * lots * BOARD_LOT_SIZE
    portfolio = load_portfolio()
    current_cash = portfolio.get("cash", 0.0)

    if total_cost > current_cash:
        shortfall = total_cost - current_cash
        return False, (
            f"⚠️ Butuh Rp{total_cost:,.0f}, cash tersedia hanya Rp{current_cash:,.0f}.\n"
            f"Selisih: Rp{shortfall:,.0f}.\n\n"
            f"Silakan /addcash jika ada dana masuk, atau revisi jumlah/harga order."
        ), None

    positions = portfolio.setdefault("positions", {})
    if ticker in positions:
        existing = positions[ticker]
        total_lots = existing["lots"] + lots
        total_existing_cost = (existing["avg_price"] * existing["lots"]) + (price * lots)
        new_avg = total_existing_cost / total_lots
        # entry_date is preserved from the ORIGINAL first buy, not reset by averaging
        # in more later — "days held" should reflect when the position was first
        # opened, not the most recent top-up.
        positions[ticker] = {
            "lots": total_lots, "avg_price": round(new_avg, 2),
            "entry_date": existing.get("entry_date"),
        }
    else:
        positions[ticker] = {
            "lots": lots, "avg_price": round(price, 2),
            "entry_date": datetime.datetime.now(WIB).strftime("%Y-%m-%d"),
        }

    portfolio["cash"] = current_cash - total_cost
    save_portfolio(portfolio)
    return True, None, positions[ticker]


def set_entry_date(ticker: str, date_str: str):
    """
    Backfills entry_date for existing holdings from before this field existed, or
    lets the user correct one. Returns (success: bool, message: str).
    """
    try:
        datetime.datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return False, "Format tanggal salah. Gunakan YYYY-MM-DD, contoh: 2026-07-08"

    portfolio = load_portfolio()
    positions = portfolio.get("positions", {})
    if ticker not in positions:
        return False, f"Tidak ada posisi {ticker} di portofolio."

    positions[ticker]["entry_date"] = date_str
    save_portfolio(portfolio)
    return True, f"Tanggal beli {ticker} diset ke {date_str}."


def compute_trading_days_held(entry_date_str: str) -> int:
    """
    Trading days (Mon-Fri only) from entry_date to today — used for the lifecycle
    category classification. Doesn't account for IDX public holidays specifically
    (same simplification as get_last_published_trading_day), just weekends.
    """
    try:
        entry_date = datetime.datetime.strptime(entry_date_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    today = datetime.datetime.now(WIB).date()
    if entry_date > today:
        return 0
    days = 0
    current = entry_date
    while current < today:
        current += datetime.timedelta(days=1)
        if current.weekday() < 5:  # Mon-Fri
            days += 1
    return days


# MBSS v2 (user request): proxy DIY untuk deteksi suspensi — tidak ada API
# gratis terstruktur untuk data suspensi resmi IDX (sudah dicek: halaman
# idx.co.id/id/berita/suspensi cuma daftar berita HTML, parse.bot tidak punya
# endpoint suspensi). Saham yang disuspensi TETAP punya histori (beda dari
# delisted yang datanya kosong total) — cuma BERHENTI UPDATE karena tidak
# diperdagangkan. Jadi dideteksi dari: sudah berapa hari BURSA bar terakhir
# tidak nambah, padahal pasar tetap buka terus di hari-hari itu.
STALE_TRADING_DAYS_THRESHOLD = 5


def count_trading_days_between(date_str_earlier: str, date_str_later: str = None) -> int:
    """
    Jumlah hari bursa (Senin-Jumat, tanpa memperhitungkan libur nasional IDX —
    simplifikasi yang sama dengan compute_trading_days_held) di ANTARA dua
    tanggal. date_str_later default ke hari ini kalau tidak diisi.
    """
    try:
        earlier = datetime.datetime.strptime(date_str_earlier, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    if date_str_later:
        try:
            later = datetime.datetime.strptime(date_str_later, "%Y-%m-%d").date()
        except (ValueError, TypeError):
            return None
    else:
        later = datetime.datetime.now(WIB).date()
    if earlier >= later:
        return 0
    days = 0
    current = earlier
    while current < later:
        current += datetime.timedelta(days=1)
        if current.weekday() < 5:
            days += 1
    return days


def reduce_position(ticker: str, lots: int, sell_price: float):
    """
    Returns (success: bool, message: str). Price is now REQUIRED — without it we
    can't compute realized P&L or correctly credit cash from the sale, both of
    which are essential once cash is being tracked as real investment-manager context.
    """
    portfolio = load_portfolio()
    positions = portfolio.setdefault("positions", {})
    if ticker not in positions:
        return False, f"Tidak ada posisi {ticker} di portofolio."

    existing = positions[ticker]
    lots_sold = min(lots, existing["lots"])  # can't sell more than you hold
    avg_cost = existing["avg_price"]
    proceeds = sell_price * lots_sold * BOARD_LOT_SIZE
    cost_basis = avg_cost * lots_sold * BOARD_LOT_SIZE
    realized_pnl = proceeds - cost_basis

    # Credit cash from the sale
    portfolio["cash"] = portfolio.get("cash", 0.0) + proceeds

    # Log the realized trade for the running P&L ledger
    portfolio.setdefault("realized_pnl_log", []).append({
        "date": datetime.datetime.now(WIB).strftime("%Y-%m-%d"),
        "ticker": ticker,
        "lots": lots_sold,
        "sell_price": sell_price,
        "avg_cost": avg_cost,
        "pnl_idr": round(realized_pnl, 0),
    })

    if lots >= existing["lots"]:
        del positions[ticker]
        closed_note = f"Posisi {ticker} ditutup penuh."
    else:
        positions[ticker]["lots"] = existing["lots"] - lots_sold
        closed_note = f"{lots_sold} lot {ticker} terjual, sisa {positions[ticker]['lots']} lot."

    save_portfolio(portfolio)

    pnl_label = "Untung" if realized_pnl >= 0 else "Rugi"
    return True, (
        f"{closed_note}\n"
        f"Harga jual: Rp{sell_price:,.0f} (avg cost: Rp{avg_cost:,.0f})\n"
        f"{pnl_label} direalisasikan: Rp{abs(realized_pnl):,.0f}\n"
        f"Cash sekarang: Rp{portfolio['cash']:,.0f}"
    )


# ==========================================
# 💬 /tanya CHAT HISTORY STORAGE (simple local JSON file, per chat_id)
# ==========================================
TANYA_HISTORY_FILE = os.path.join(PROJECT_ROOT, "tanya_history.json")
TANYA_HISTORY_MAX_TURNS = 16  # 8 user+model pairs — bounds how much old conversation gets resent to Gemini each turn


def load_tanya_history(chat_id) -> list:
    """Returns [{"role": "user"|"model", "text": ...}, ...] for this chat, oldest first."""
    if not os.path.exists(TANYA_HISTORY_FILE):
        return []
    try:
        with open(TANYA_HISTORY_FILE, "r") as f:
            data = json.load(f)
        return data.get(str(chat_id), [])
    except Exception as e:
        print(f"⚠️ Failed to read tanya history file: {e}")
        return []


def save_tanya_turn(chat_id, question: str, answer: str):
    """Appends this Q&A pair to the chat's history, trimmed to TANYA_HISTORY_MAX_TURNS."""
    data = {}
    if os.path.exists(TANYA_HISTORY_FILE):
        try:
            with open(TANYA_HISTORY_FILE, "r") as f:
                data = json.load(f)
        except Exception as e:
            print(f"⚠️ Failed to read tanya history file before save: {e}")
            data = {}

    key = str(chat_id)
    history = data.get(key, [])
    history.append({"role": "user", "text": question})
    history.append({"role": "model", "text": answer})
    data[key] = history[-TANYA_HISTORY_MAX_TURNS:]

    with open(TANYA_HISTORY_FILE, "w") as f:
        json.dump(data, f, indent=2)


def reset_tanya_history(chat_id):
    if not os.path.exists(TANYA_HISTORY_FILE):
        return
    try:
        with open(TANYA_HISTORY_FILE, "r") as f:
            data = json.load(f)
        data.pop(str(chat_id), None)
        with open(TANYA_HISTORY_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"⚠️ Failed to reset tanya history: {e}")


# ==========================================
# 📊 CALCULATIONS & FACTOR SCORING PIPELINE
# ==========================================
def format_adx_label(adx_value: float) -> str:
    """
    Label 3 kategori untuk ADX — BUKAN biner (lemah/kuat) seperti sebelumnya,
    yang menyesatkan untuk kasus ADX di zona 20-25 (baru lepas dari sideways,
    belum cukup tinggi untuk benar-benar disebut "tren kuat" secara umum).
    Skor sendiri (is_weak_trend) tetap cuma cek <20, ini murni perbaikan LABEL
    supaya jujur menggambarkan kondisi, bukan mengubah logika skor.
    """
    if adx_value < 20:
        return "tren lemah/sideways"
    elif adx_value < 25:
        return "netral, belum cukup kuat"
    else:
        return "tren kuat"


def calculate_adx(high_prices, low_prices, close_prices, period=14):
    """
    ADX (Average Directional Index) — MENGUKUR KEKUATAN tren, BUKAN arahnya
    (beda dari RSI/MACD yang mengukur arah). ADX tinggi (>=25) = tren solid,
    sinyal lain (MACD, RSI, dll) lebih bisa dipercaya. ADX rendah (<20) = pasar
    sideways/noise, sinyal apa pun di kondisi ini lebih rawan jadi jebakan
    palsu — dipakai sebagai pengali kepercayaan terhadap momentum_score, bukan
    sekadar info tambahan. Formula Wilder standar 14-hari.
    """
    prev_close = close_prices.shift(1)
    prev_high = high_prices.shift(1)
    prev_low = low_prices.shift(1)

    true_range = pd.concat([
        high_prices - low_prices,
        (high_prices - prev_close).abs(),
        (low_prices - prev_close).abs(),
    ], axis=1).max(axis=1)

    up_move = high_prices - prev_high
    down_move = prev_low - low_prices
    plus_dm = pd.Series(0.0, index=high_prices.index)
    minus_dm = pd.Series(0.0, index=high_prices.index)
    plus_dm[(up_move > down_move) & (up_move > 0)] = up_move[(up_move > down_move) & (up_move > 0)]
    minus_dm[(down_move > up_move) & (down_move > 0)] = down_move[(down_move > up_move) & (down_move > 0)]

    # Wilder smoothing (alpha = 1/period), sama seperti RSI klasik
    atr = true_range.ewm(alpha=1 / period, adjust=False).mean()
    smoothed_plus_dm = plus_dm.ewm(alpha=1 / period, adjust=False).mean()
    smoothed_minus_dm = minus_dm.ewm(alpha=1 / period, adjust=False).mean()

    plus_di = 100 * (smoothed_plus_dm / atr.replace(0, float("nan")))
    minus_di = 100 * (smoothed_minus_dm / atr.replace(0, float("nan")))
    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, float("nan"))
    adx = dx.ewm(alpha=1 / period, adjust=False).mean()
    return adx.fillna(0)


def calculate_macd(close_prices, fast=12, slow=26, signal=9):
    """
    Standard MACD (12/26/9 EMA). Unlike RSI (an oscillator measuring overbought/
    oversold within a range), MACD is a TREND/TIMING indicator — it tells you
    whether momentum is accelerating or decelerating right now, which is more
    directly useful for swing/day-trade entry timing than RSI alone. A stock can
    look fine on RSI while MACD is quietly rolling over, or vice versa.
    Returns (macd_line, signal_line, histogram).
    """
    ema_fast = close_prices.ewm(span=fast, adjust=False).mean()
    ema_slow = close_prices.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def calculate_rsi(prices, period=14):
    """Wilder's RSI (the standard formula used by TradingView etc), not a simple rolling mean."""
    delta = prices.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / (avg_loss + 1e-9)
    return 100 - (100 / (1 + rs))


def calculate_cmf(high, low, close, volume, period=20):
    """
    Chaikin Money Flow: weighs volume by WHERE price closed within that day's
    range, not just how much volume traded. Closing near the day's high on heavy
    volume = real buying pressure; closing near the low on heavy volume = selling
    pressure — a sharper proxy for "smart money" activity than raw volume ratio.
    Range: -1 (strong selling pressure) to +1 (strong buying pressure).
    """
    money_flow_multiplier = ((close - low) - (high - close)) / (high - low + 1e-9)
    money_flow_volume = money_flow_multiplier * volume
    cmf = money_flow_volume.rolling(window=period).sum() / (volume.rolling(window=period).sum() + 1e-9)
    return cmf


def calculate_obv(close, volume):
    """On-Balance Volume: cumulative volume flow, up on up-days, down on down-days."""
    direction = close.diff().apply(lambda x: 1 if x > 0 else (-1 if x < 0 else 0))
    obv = (direction * volume).fillna(0).cumsum()
    return obv


def detect_obv_divergence(close, obv, lookback=20):
    """
    Flags the classic accumulation/distribution divergence pattern: price moving
    one direction while OBV (cumulative volume flow) moves the opposite way.
    This is the closest free, data-source-independent proxy for "big holders
    quietly buying/selling while price looks calm" — exactly the pattern that
    misled the volume-ratio-only score on cases like MNCN.
    """
    if len(close) < lookback + 1 or len(obv) < lookback + 1:
        return "insufficient_data"

    price_change_pct = (close.iloc[-1] - close.iloc[-lookback]) / max(close.iloc[-lookback], 1) * 100
    obv_start, obv_end = obv.iloc[-lookback], obv.iloc[-1]
    obv_change_pct = ((obv_end - obv_start) / abs(obv_start)) * 100 if obv_start != 0 else 0

    # Price roughly flat/up but OBV clearly falling = possible quiet distribution
    if price_change_pct > -2 and obv_change_pct < -5:
        return "bearish_divergence"  # price holding up, but volume flow says selling
    # Price roughly flat/down but OBV clearly rising = possible quiet accumulation
    if price_change_pct < 2 and obv_change_pct > 5:
        return "bullish_divergence"  # price subdued, but volume flow says buying
    return "none"


def detect_lower_highs(high_prices, window=3, num_swings=3, lookback=60):
    """
    Detects a classic bearish "lower highs" chart structure: each recent swing
    high sitting below the one before it, suggesting weakening upside momentum
    even if the current price or RSI looks fine in isolation. Also returns a
    breakout_level — the most recent swing high price that would need to be
    closed above to invalidate the bearish structure.
    """
    recent_high = high_prices.tail(lookback)
    if len(recent_high) < window * 2 + 1:
        return {"pattern": "insufficient_data", "breakout_level": None, "recent_swing_highs": []}

    is_local_max = recent_high == recent_high.rolling(window * 2 + 1, center=True).max()
    swing_highs = recent_high[is_local_max].dropna()

    if len(swing_highs) < 2:
        return {"pattern": "insufficient_data", "breakout_level": None, "recent_swing_highs": []}

    last_swings = swing_highs.tail(num_swings)
    values = last_swings.values
    is_descending = len(values) >= 2 and all(values[i] > values[i + 1] for i in range(len(values) - 1))
    breakout_level = round(values[-1] / 5) * 5 if len(values) >= 1 else None

    return {
        "pattern": "lower_highs_bearish" if is_descending else "none",
        "breakout_level": int(breakout_level) if breakout_level is not None else None,
        "recent_swing_highs": [int(v) for v in values],
    }


def percentile_rank(series: pd.Series, value: float) -> float:
    """Where does `value` sit in `series`'s own history? Returns 0.0-1.0."""
    series = series.dropna()
    if len(series) == 0:
        return 0.5
    return float((series < value).sum()) / len(series)


def score_from_percentile(pct: float, invert: bool = False) -> float:
    """Map a 0-1 percentile rank to a 1-10 score. invert=True means LOWER is better."""
    p = 1 - pct if invert else pct
    return round(max(1.0, min(10.0, 1 + p * 9)), 1)


MIN_HISTORY_FOR_ADAPTIVE = 120  # ~6 months of trading days needed to build a meaningful per-stock baseline
MIN_STOCK_PRICE = 55  # diturunkan dari 100 ke 55 (terbukti berhasil untuk JGLE, gain >10%) — filter frozen-price terpisah (harga <=51 + rentang 10hr sempit) tetap aktif untuk menyaring saham beku, tapi rentang 55-99 kini masuk universe screening lagi

# ==========================================
# 📋 MONTHLY TICKER WHITELIST (reduces daily processing from 70 tickers to eligible-only)
# ==========================================
# ~15-20 of the 70 ISSI tickers get excluded every single day for the SAME static
# reasons (permanently low price, delisted) — no reason to re-fetch and re-check
# them daily. This checks eligibility once per calendar month and caches the result,
# so daily runs only process the smaller eligible subset, cutting real runtime
# without touching the confirmed-safe 70s cooldown itself.
WHITELIST_CACHE_FILE = os.path.join(PROJECT_ROOT, "ticker_whitelist.json")
ISSI_LIQUID_CACHE_FILE = os.path.join(PROJECT_ROOT, "issi_liquid_whitelist.json")
DAILY_SCAN_CACHE_FILE = os.path.join(PROJECT_ROOT, "daily_scan_cache.pkl")
DAYTRADE_PICKS_HISTORY_FILE = os.path.join(PROJECT_ROOT, "daytrade_picks_history.json")
PENDING_ORDERS_FILE = os.path.join(PROJECT_ROOT, "pending_orders.json")

# ==========================================
# 🚫 FAILED-FETCH BLACKLIST (MBSS v2, user request)
# Ticker yang gagal fetch berkali-kali berturut-turut (paling sering karena
# delisted — yfinance sama sekali tidak punya datanya) TIDAK PERNAH masuk
# cache/eod.pkl (cuma hasil SUKSES yang disimpan) — jadi tanpa mekanisme ini,
# ticker itu akan diam-diam dicoba fetch ulang SETIAP kali /eodscan atau
# /screendaytrade jalan, selamanya, membuang waktu untuk sesuatu yang tidak
# akan pernah berhasil.
# ==========================================
FAILED_FETCH_TRACKING_FILE = os.path.join(PROJECT_ROOT, "failed_fetch_tracking.json")
FAILED_FETCH_BLACKLIST_THRESHOLD = 3  # gagal 3x berturut-turut -> masuk blacklist
FAILED_FETCH_BLACKLIST_DAYS = 30  # setelah itu, dicoba ulang sekali (siapa tahu relisting)

# Seed awal (MBSS v2, dikonfirmasi gagal berulang lewat log real user, 01 Agu
# 2026) — supaya langsung ter-skip mulai run PERTAMA setelah patch ini
# dipasang, bukan menunggu 3x gagal lagi dari nol.
_FAILED_FETCH_SEED_TICKERS = [
    "SMRU", "LMAS", "JSKY", "CPRI", "IIKP", "PLAS", "CBMF", "SCPI", "LCGP", "GAMA",
]


def _load_failed_fetch_tracking() -> dict:
    if not os.path.exists(FAILED_FETCH_TRACKING_FILE):
        # First run: seed dengan ticker yang sudah dikonfirmasi gagal berulang,
        # supaya tidak perlu menunggu 3x gagal lagi dari nol.
        today_str = datetime.datetime.now(WIB).strftime("%Y-%m-%d")
        seeded = {
            t: {"consecutive_fails": FAILED_FETCH_BLACKLIST_THRESHOLD,
                "last_attempt": today_str, "blacklisted_since": today_str}
            for t in _FAILED_FETCH_SEED_TICKERS
        }
        _save_failed_fetch_tracking(seeded)
        return seeded
    try:
        with open(FAILED_FETCH_TRACKING_FILE) as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ Gagal membaca failed_fetch_tracking: {e}")
        return {}


def _save_failed_fetch_tracking(data: dict):
    try:
        with open(FAILED_FETCH_TRACKING_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        print(f"⚠️ Gagal menyimpan failed_fetch_tracking: {e}")


def get_blacklist_reason(ticker: str) -> str | None:
    """Return the stored reason a ticker was blacklisted, if any (for accurate log/skip messages)."""
    tracking = _load_failed_fetch_tracking()
    entry = tracking.get(ticker)
    if not entry:
        return None
    return entry.get("reason") or "gagal fetch berulang, kemungkinan delisted"


def is_ticker_blacklisted(ticker: str) -> bool:
    """
    True kalau ticker sudah gagal fetch >=FAILED_FETCH_BLACKLIST_THRESHOLD kali
    berturut-turut DAN belum lewat FAILED_FETCH_BLACKLIST_DAYS sejak masuk
    blacklist (supaya sesekali dicoba ulang — bisa saja relisting/data pulih).
    """
    tracking = _load_failed_fetch_tracking()
    entry = tracking.get(ticker)
    if not entry or entry.get("consecutive_fails", 0) < FAILED_FETCH_BLACKLIST_THRESHOLD:
        return False
    blacklisted_since = entry.get("blacklisted_since")
    if not blacklisted_since:
        return False
    try:
        since_date = datetime.datetime.strptime(blacklisted_since, "%Y-%m-%d").date()
        days_elapsed = (datetime.datetime.now(WIB).date() - since_date).days
        return days_elapsed < FAILED_FETCH_BLACKLIST_DAYS
    except Exception:
        return True  # parse gagal, aman default ke "masih diblacklist"


def record_fetch_result(ticker: str, success: bool):
    """
    Dipanggil setelah SETIAP percobaan fetch (sukses maupun gagal) di
    fetch_all_tickers_scored — update penghitung gagal berturut-turut, dan
    tandai waktu masuk blacklist begitu ambang tercapai (supaya
    FAILED_FETCH_BLACKLIST_DAYS bisa dihitung dari titik itu, bukan dari
    percobaan gagal yang pertama).

    Pakai jalur 3x-gagal-berturut-turut ini untuk kegagalan yang AMBIGU
    (exception, "excluded (see console)" generik) — sekali gagal bisa saja
    cuma network blip sesaat, jadi wajar butuh konfirmasi berulang dulu
    sebelum diblacklist. Untuk kasus yang buktinya sudah LANGSUNG & PASTI
    dari data historis (mis. stale-trading/proxy suspensi — lihat
    record_direct_evidence_blacklist), 3x konfirmasi itu berlebihan.
    """
    tracking = _load_failed_fetch_tracking()
    today_str = datetime.datetime.now(WIB).strftime("%Y-%m-%d")
    entry = tracking.get(ticker, {"consecutive_fails": 0, "last_attempt": None, "blacklisted_since": None})

    if success:
        # Sukses sekali langsung reset total — kalau ticker ini pulih, jangan
        # separuh-separuh, anggap benar-benar pulih.
        if entry.get("consecutive_fails", 0) > 0:
            tracking.pop(ticker, None)
            _save_failed_fetch_tracking(tracking)
        return

    entry["consecutive_fails"] = entry.get("consecutive_fails", 0) + 1
    entry["last_attempt"] = today_str
    if entry["consecutive_fails"] >= FAILED_FETCH_BLACKLIST_THRESHOLD and not entry.get("blacklisted_since"):
        entry["blacklisted_since"] = today_str
        print(f"🚫 {ticker}: {entry['consecutive_fails']}x gagal fetch berturut-turut — masuk blacklist {FAILED_FETCH_BLACKLIST_DAYS} hari.")
    tracking[ticker] = entry
    _save_failed_fetch_tracking(tracking)


def record_direct_evidence_blacklist(ticker: str, reason: str):
    """
    MBSS v2 (user request): blacklist LANGSUNG dari SATU deteksi, dipakai
    khusus untuk sinyal yang buktinya sudah pasti dari data historis itu
    sendiri (bukan sekadar "fetch gagal", yang bisa saja cuma glitch
    sesaat) — saat ini dipakai untuk proxy suspensi (stale-trading, lihat
    STALE_TRADING_DAYS_THRESHOLD): begitu compute_factor_scoring mendeteksi
    bar terakhir sudah mandek >=5 hari bursa, itu BUKTI LANGSUNG dari DB
    kita sendiri, bukan dugaan — jadi tidak perlu tunggu 3x konfirmasi lagi
    seperti jalur generik (record_fetch_result). Retry otomatis tetap
    FAILED_FETCH_BLACKLIST_DAYS (30 hari) — konsisten dengan mekanisme yang
    sudah ada, cuma jalur MASUKNYA yang dipercepat jadi run pertama yang
    mendeteksi, bukan run ketiga.
    """
    tracking = _load_failed_fetch_tracking()
    today_str = datetime.datetime.now(WIB).strftime("%Y-%m-%d")
    entry = tracking.get(ticker, {})
    already_blacklisted_today = entry.get("blacklisted_since") == today_str
    entry.update({
        "consecutive_fails": FAILED_FETCH_BLACKLIST_THRESHOLD,
        "last_attempt": today_str,
        "blacklisted_since": entry.get("blacklisted_since") or today_str,
        "reason": reason,
    })
    tracking[ticker] = entry
    _save_failed_fetch_tracking(tracking)
    if not already_blacklisted_today:
        print(f"🚫 {ticker}: {reason} — langsung masuk blacklist {FAILED_FETCH_BLACKLIST_DAYS} hari (bukti langsung, tanpa tunggu 3x).")
OHLCV_DB_FILE = os.path.join(PROJECT_ROOT, "mbss_ohlcv.db")

# Berapa hari delta yang di-fetch dari iTick untuk update harian (1-2 bar cukup)
OHLCV_DELTA_FETCH_LIMIT = 3  # sedikit lebih dari 1 untuk antisipasi hari libur/weekend
WINRATE_RESOLUTION_WINDOW_DAYS = 5  # jumlah hari bursa untuk uji TP/SL sebelum time-based exit

# Ambang volume rata-rata 20 hari untuk universe ISSI (jauh lebih luas dari ISSI,
# perlu filter likuiditas tambahan sendiri). Reasoning angka ini:
# - <200rb lembar/hari: risiko TINGGI terjebak posisi — spread lebar, order lambat
#   match, sulit exit cepat untuk swing/day trade aktif.
# - 500rb lembar/hari: mulai "cukup sehat" untuk keluar-masuk tanpa slippage besar
#   pada modal kecil-menengah (skala trading retail, bukan institusi besar).
# - >1jt lembar/hari: jelas aman, tapi terlalu ketat sebagai AMBANG MINIMUM akan
#   membuang banyak saham ISSI menengah yang genuinely masih bisa ditradingkan.
# 500.000 dipilih sebagai titik tengah: cukup ketat untuk hindari dead stock,
# tidak terlalu ketat sehingga kehilangan manfaat cakupan lebih luas dari ISSI.
# Ambang volume rata-rata 10 hari bursa (window diubah dari 20 ke 10 hari atas
# permintaan user — supaya whitelist build lebih hemat panggilan Zapi). 500.000
# tetap dipakai sebagai titik tengah (lihat reasoning asli di bawah); rata-rata
# 10 hari vs 20 hari untuk saham yang genuinely likuid biasanya tidak jauh beda.
MIN_VOLUME_10D_AVG = 500_000
ISSI_WHITELIST_CACHE_DAYS = 14  # cache 2 minggu (bukan 1) — hemat kuota Zapi


def evaluate_eligibility_from_hist(current_price, hist_low, hist_high, min_bars_needed=10):
    """
    Pure eligibility logic on already-fetched price data — decoupled from the fetch
    itself so it works whether the data came from a single-ticker or batch call.
    Returns (is_eligible, reason).
    """
    support_10d = hist_low.tail(10).min()
    resistance_10d = hist_high.tail(10).max()
    price_range_pct = ((resistance_10d - support_10d) / max(support_10d, 1)) * 100

    if current_price <= 51 or price_range_pct < 2.0:
        return False, f"frozen/floor price (Rp{int(current_price)}, {price_range_pct:.1f}% 10d range)"
    if current_price < MIN_STOCK_PRICE:
        return False, f"price Rp{int(current_price)} below MIN_STOCK_PRICE ({MIN_STOCK_PRICE})"
    return True, None


def evaluate_liquid_eligibility_from_bulk(current_price, lows, highs, volumes):
    """
    Versi lebih ketat dari evaluate_eligibility_from_hist() — dipakai KHUSUS
    untuk universe ISSI (jauh lebih luas dari ISSI, banyak nama tipis/tidak
    likuid). Menambahkan filter volume rata-rata 10 hari bursa di ATAS filter
    harga/frozen yang sudah ada. Input berupa LIST plain (bukan pandas Series)
    karena sumbernya sekarang Zapi bulk stock-summary, bukan iTick kline.
    """
    is_eligible, reason = evaluate_eligibility_from_hist(current_price, pd.Series(lows), pd.Series(highs))
    if not is_eligible:
        return is_eligible, reason
    if not volumes or len(volumes) < 10:
        return True, None  # data belum cukup — include by default
    avg_volume_10d = sum(volumes) / len(volumes)
    if avg_volume_10d < MIN_VOLUME_10D_AVG:
        return False, f"volume rata-rata 10hr terlalu rendah ({int(avg_volume_10d):,} < {MIN_VOLUME_10D_AVG:,})"
    return True, None


def load_or_build_issi_liquid_whitelist(force_rebuild=False):
    """
    Whitelist ISSI dengan filter likuiditas TAMBAHAN (harga + volume 10hr bursa)
    — TERPISAH dari whitelist ISSI default, tidak menggantikannya.

    SUMBER DATA: Zapi bulk stock-summary (1 panggilan = SEMUA saham IDX untuk
    1 hari), BUKAN iTick batch per-ticker — jauh lebih hemat panggilan untuk
    universe besar seperti ISSI. Window 10 hari bursa, dikumpulkan mundur dari
    hari ini, skip otomatis hari libur/weekend (terdeteksi dari recordsTotal=0).
    Realistisnya butuh ~14-16 panggilan per build (bukan persis 10), karena 10
    hari bursa mencakup minimal 2 weekend penuh.

    Cache 2 MINGGU (14 hari kalender dari waktu build) — sesuai kesepakatan
    user untuk menghemat kuota Zapi (300/bulan, dibagi juga untuk /check dan
    /myportfolio brokersum harian).
    """
    if not force_rebuild and os.path.exists(ISSI_LIQUID_CACHE_FILE):
        try:
            with open(ISSI_LIQUID_CACHE_FILE) as f:
                cache = json.load(f)
            built_at = datetime.datetime.strptime(cache.get("built_at", ""), "%Y-%m-%d")
            age_days = (datetime.datetime.now(WIB).date() - built_at.date()).days
            if age_days < ISSI_WHITELIST_CACHE_DAYS:
                print(f"📋 Using cached ISSI liquid whitelist (dibangun {age_days} hari lalu, "
                      f"valid s/d {ISSI_WHITELIST_CACHE_DAYS} hari): {len(cache.get('eligible_tickers', []))} ticker")
                return cache.get("eligible_tickers", [])
        except Exception as e:
            print(f"⚠️ Failed to read ISSI liquid whitelist cache: {e}. Rebuilding.")

    print("🔄 Building fresh ISSI liquid whitelist via Zapi bulk stock-summary "
          "(realistis ~14-16 panggilan, sekali per 2 minggu)...")

    TARGET_TRADING_DAYS = 10
    MAX_CALENDAR_DAYS_BACK = 20  # pengaman kalau ada banyak libur beruntun
    per_ticker_days = {}  # StockCode -> {"close": last, "lows": [...], "highs": [...], "volumes": [...]}
    trading_days_collected = 0
    calendar_days_back = 0
    calls_made = 0

    while trading_days_collected < TARGET_TRADING_DAYS and calendar_days_back < MAX_CALENDAR_DAYS_BACK:
        test_date = (datetime.datetime.now(WIB).date() - datetime.timedelta(days=calendar_days_back)).strftime("%Y%m%d")
        calendar_days_back += 1
        try:
            resp = requests.get(
                f"{ZAPI_BASE_URL}/finance:idx/stock-summary",
                params={"length": "1000", "start": "0", "date": test_date},
                headers=ZAPI_HEADERS, timeout=30,
            )
            data = resp.json()
            rows = data.get("data", {}).get("data", [])
            calls_made += 1
        except Exception as e:
            print(f"⚠️ Zapi bulk fetch gagal untuk {test_date}: {e}")
            continue

        if not rows:
            continue  # hari libur/weekend, skip tanpa menghitung sebagai trading day

        trading_days_collected += 1
        for row in rows:
            code = row.get("StockCode")
            if not code:
                continue
            entry = per_ticker_days.setdefault(code, {"close": None, "lows": [], "highs": [], "volumes": []})
            if entry["close"] is None:  # baris PERTAMA yang masuk untuk ticker ini = hari PALING BARU
                entry["close"] = row.get("Close")
            entry["lows"].append(row.get("Low", 0))
            entry["highs"].append(row.get("High", 0))
            entry["volumes"].append(row.get("Volume", 0))

    print(f"📡 Terkumpul {trading_days_collected} hari bursa via {calls_made} panggilan Zapi")

    sharia_full = set(fetch_online_sharia_list(index_key="ISSI"))
    eligible = []
    excluded = {}
    for code, days in per_ticker_days.items():
        if code not in sharia_full:
            continue  # bulk data mencakup SEMUA saham IDX, saring ke ISSI saja
        if days["close"] is None or len(days["volumes"]) < 5:
            continue  # data tidak cukup, kemungkinan baru IPO/delisting — skip
        is_eligible, reason = evaluate_liquid_eligibility_from_bulk(
            days["close"], days["lows"], days["highs"], days["volumes"]
        )
        if is_eligible is False:
            excluded[code] = reason
        else:
            eligible.append(code)

    cache = {
        "built_at": datetime.datetime.now(WIB).strftime("%Y-%m-%d"),
        "eligible_tickers": eligible, "excluded_count": len(excluded), "calls_used": calls_made,
    }
    try:
        with open(ISSI_LIQUID_CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        print(f"⚠️ Failed to save ISSI liquid whitelist cache: {e}")

    print(f"✅ ISSI liquid whitelist built: {len(eligible)} eligible (dari {len(sharia_full)} total ISSI), "
          f"{len(excluded)} tersingkir (harga/frozen/volume rendah), {calls_made} panggilan Zapi terpakai")
    return eligible



    """
    Lightweight structural eligibility check via the SINGULAR endpoint (1 ticker/call).
    Kept for standalone use; the whitelist build itself now prefers the batch
    version below for efficiency. Returns (is_eligible, reason). is_eligible=None
    means "unknown/transient issue, include by default."
    """
    hist = get_ohlcv_smart(ticker, limit=15)
    if hist is None or hist.empty or len(hist) < 10:
        return None, "insufficient_data_at_check_time"
    return evaluate_eligibility_from_hist(hist["Close"].iloc[-1], hist["Low"], hist["High"])


def check_ticker_eligibility(ticker):
    """
    Lightweight structural eligibility check via the SINGULAR endpoint (1 ticker/call).
    Kept for standalone use; the whitelist build itself now prefers the batch
    version below for efficiency. Returns (is_eligible, reason). is_eligible=None
    means "unknown/transient issue, include by default."
    """
    hist = get_ohlcv_smart(ticker, limit=15)
    if hist is None or hist.empty or len(hist) < 10:
        return None, "insufficient_data_at_check_time"
    return evaluate_eligibility_from_hist(hist["Close"].iloc[-1], hist["Low"], hist["High"])


ITICK_BATCH_SIZE = 3  # confirmed working free-tier limit for region=ID (per iTick support)

# ==========================================
# 💹 INDEX ALPHA API — real foreign/domestic broker flow (opt-in, budget-limited)
# ==========================================
# Confirmed via direct testing: real, accurate data (matched TLKM's independently
# verified July 17 rally), and date ranges genuinely aggregate across multiple
# trading days (confirmed via ANTM buy/sell value symmetry and realistic magnitude
# for a full week). Free tier: 150 requests/month. This is intentionally NEVER
# called from the daily bulk scan — only from the opt-in /myportfolio brokersum
# command, scoped to holdings + watchlist (max ~9 tickers), since this is real,
# budget-limited cost that should only be spent where capital is actually at stake.
INDEXALPHA_API_TOKEN = os.environ.get("INDEXALPHA_API_TOKEN", "")
INDEXALPHA_HEADERS = {"accept": "application/json", "Authorization": f"Bearer {INDEXALPHA_API_TOKEN}"}
INDEXALPHA_BASE_URL = "https://api.indexalpha.id"

# 🏦 RAPIDAPI IDX MARKET INTELLIGENCE — interim real-broker-data source while
# Index Alpha's monthly quota is exhausted (resets next month). Basic free
# plan: 500 requests/month total, 1 req/sec. Budget-limited like Index Alpha
# — see engine/broker.py RAPIDAPI_IDX_MONTHLY_BUDGET and the quota tracker
# (_rapidapi_idx_quota_check_and_increment) for the enforced monthly cap.
RAPIDAPI_IDX_KEY = os.environ.get("RAPIDAPI_IDX_KEY", "")
RAPIDAPI_IDX_HOST = os.environ.get("RAPIDAPI_IDX_HOST", "indonesia-stock-exchange-idx.p.rapidapi.com")
RAPIDAPI_IDX_BASE_URL = f"https://{RAPIDAPI_IDX_HOST}"
RAPIDAPI_IDX_HEADERS = {"x-rapidapi-key": RAPIDAPI_IDX_KEY, "x-rapidapi-host": RAPIDAPI_IDX_HOST}
# NOTE (MBSS v2 refactor, Phase 4): BROKERSUM_LOOKBACK_DAYS, BROKERSUM_CACHE_FILE,
# BROKERSUM_HISTORY_FILE, BROKERSUM_HISTORY_MAX_ENTRIES_PER_TICKER, and
# fetch_broker_summary_raw() moved to engine/broker.py (BrokerEngine).
# PENDING_BROKERSUM_CHECKS stays HERE — it's Telegram conversation-flow state
# (which chat is waiting for a screenshot), not broker data itself.

# In-memory state: which ticker was just /check'd, waiting for an optional Broker
# Sum screenshot. Ephemeral (lost on restart) is fine — this is a short-lived,
# per-session UI flow, not data worth persisting. {chat_id: {"ticker": str, "expires_at": datetime}}
PENDING_BROKERSUM_CHECKS = {}
PENDING_BROKERSUM_TIMEOUT_MINUTES = 5


# ===========================================================================
# 📦 SQLITE OHLCV LAYER
# Menyimpan data harian iTick + 4H Yahoo Finance secara lokal — supaya
# saham yang sama tidak perlu di-fetch ulang tiap panggilan. Alur:
#   - Initial populate: yfinance batch (sekali jalan, dapat 2 tahun histori)
#   - Update harian: iTick delta (cuma 2-3 bar baru per ticker, bukan 500)
#   - 4H: Yahoo Finance native (2 bar/hari, untuk high conviction check)
# ===========================================================================

def init_ohlcv_db():
    """Buat tabel kalau belum ada — idempotent, aman dipanggil berulang."""
    conn = sqlite3.connect(OHLCV_DB_FILE)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ohlcv_daily (
            ticker TEXT NOT NULL,
            date TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume INTEGER,
            PRIMARY KEY (ticker, date)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_ticker_date ON ohlcv_daily(ticker, date)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ohlcv_4h (
            ticker TEXT NOT NULL,
            datetime TEXT NOT NULL,
            open REAL, high REAL, low REAL, close REAL, volume INTEGER,
            PRIMARY KEY (ticker, datetime)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_4h_ticker_dt ON ohlcv_4h(ticker, datetime)")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS db_metadata (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TEXT
        )
    """)
    conn.commit()
    conn.close()


def upsert_ohlcv_daily(ticker: str, df: pd.DataFrame):
    """Simpan/update bar harian ke SQLite — INSERT OR REPLACE (upsert)."""
    if df is None or df.empty:
        return
    conn = sqlite3.connect(OHLCV_DB_FILE)
    rows = [
        (ticker, str(idx.date() if hasattr(idx, 'date') else idx),
         row.get("Open"), row.get("High"), row.get("Low"), row.get("Close"), row.get("Volume"))
        for idx, row in df.iterrows()
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO ohlcv_daily(ticker, date, open, high, low, close, volume) VALUES(?,?,?,?,?,?,?)",
        rows
    )
    conn.commit()
    conn.close()


def get_ohlcv_daily_from_db(ticker: str, limit: int = 500) -> pd.DataFrame:
    """Ambil bar harian dari SQLite, diurutkan ascending (lama ke baru)."""
    conn = sqlite3.connect(OHLCV_DB_FILE)
    rows = conn.execute(
        "SELECT date, open, high, low, close, volume FROM ohlcv_daily "
        "WHERE ticker=? ORDER BY date DESC LIMIT ?",
        (ticker, limit)
    ).fetchall()
    conn.close()
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows, columns=["date","Open","High","Low","Close","Volume"])
    df["date"] = pd.to_datetime(df["date"])
    df = df.set_index("date").sort_index()
    return df


def repair_thin_tickers(min_bars_healthy: int = 20) -> dict:
    """
    MBSS v2 (user request, tindak lanjut bugfix get_ohlcv_smart — kasus nyata
    DOSS/FWCT/PMUI): perbaikan di get_ohlcv_smart() cuma berlaku untuk ticker
    yang BENAR-BENAR BARU di DB (latest_in_db masih None). Ticker yang SUDAH
    kadung tersangkut bug lama (sudah kesimpan sedikit bar, jadi latest_in_db
    BUKAN None lagi) tidak akan otomatis pulih — kondisi baru itu tidak akan
    pernah ke-trigger untuk mereka. Fungsi ini jalan SEKALI: cari semua ticker
    dengan jumlah bar di bawah ambang sehat, paksa backfill 2 tahun penuh
    untuk masing-masing (mengabaikan latest_in_db, treat seperti ticker baru).
    """
    init_ohlcv_db()
    conn = sqlite3.connect(OHLCV_DB_FILE)
    rows = conn.execute(
        "SELECT ticker, COUNT(*) as bar_count FROM ohlcv_daily GROUP BY ticker HAVING bar_count < ?",
        (min_bars_healthy,)
    ).fetchall()
    conn.close()

    thin_tickers = [r[0] for r in rows]
    if not thin_tickers:
        print(f"✅ Tidak ada ticker dengan data di bawah {min_bars_healthy} bar — tidak ada yang perlu diperbaiki.")
        return {"repaired": [], "still_thin": [], "checked": 0}

    print(f"🔧 Ditemukan {len(thin_tickers)} ticker dengan data tipis (<{min_bars_healthy} bar) — memaksa backfill 2 tahun penuh...")
    repaired, still_thin = [], []
    for i, ticker in enumerate(thin_tickers, 1):
        try:
            full = yfinance_get_kline(ticker, period="2y")
            if full is not None and not full.empty:
                upsert_ohlcv_daily(ticker, full)
                new_count = len(get_ohlcv_daily_from_db(ticker, limit=500))
                if new_count >= min_bars_healthy:
                    repaired.append(ticker)
                    print(f"  ✅ {ticker}: {new_count} bar sekarang")
                else:
                    still_thin.append(ticker)
                    print(f"  ⚠️ {ticker}: cuma dapat {new_count} bar bahkan setelah backfill 2y — kemungkinan genuinely baru listing")
            else:
                still_thin.append(ticker)
                print(f"  ❌ {ticker}: backfill 2y gagal (tidak ada respons dari Yahoo) — cek kode ticker manual")
        except Exception as e:
            still_thin.append(ticker)
            print(f"  ❌ {ticker}: error saat backfill — {e}")
        if i % 20 == 0 and i < len(thin_tickers):
            print(f"⏳ Cooling down 15s ({i}/{len(thin_tickers)})...")
            time.sleep(15)
        else:
            time.sleep(0.5)

    print(f"\n🔧 Selesai: {len(repaired)} ticker berhasil diperbaiki, {len(still_thin)} masih tipis "
          f"(kemungkinan genuinely baru listing atau kode salah).")
    return {"repaired": repaired, "still_thin": still_thin, "checked": len(thin_tickers)}


def get_latest_daily_date_in_db(ticker: str) -> str | None:
    """Return tanggal bar terbaru untuk ticker ini di DB, atau None kalau kosong."""
    conn = sqlite3.connect(OHLCV_DB_FILE)
    row = conn.execute(
        "SELECT MAX(date) FROM ohlcv_daily WHERE ticker=?", (ticker,)
    ).fetchone()
    conn.close()
    return row[0] if row and row[0] else None



def set_db_metadata(key: str, value: str):
    init_ohlcv_db()
    conn = sqlite3.connect(OHLCV_DB_FILE)
    conn.execute(
        "INSERT OR REPLACE INTO db_metadata(key, value, updated_at) VALUES(?, ?, ?)",
        (key, str(value), datetime.datetime.now(WIB).isoformat(timespec="seconds")),
    )
    conn.commit()
    conn.close()


def get_db_metadata(key: str, default=None):
    if not os.path.exists(OHLCV_DB_FILE):
        return default
    try:
        conn = sqlite3.connect(OHLCV_DB_FILE)
        row = conn.execute("SELECT value FROM db_metadata WHERE key=?", (key,)).fetchone()
        conn.close()
        return row[0] if row and row[0] is not None else default
    except Exception:
        return default


def update_db_update_metadata(tickers_count: int, period: str, rows_written: int, latest_marker: str | None):
    set_db_metadata("last_ohlcv_update_at", datetime.datetime.now(WIB).isoformat(timespec="seconds"))
    set_db_metadata("last_ohlcv_update_marker", latest_marker or "")
    set_db_metadata("last_ohlcv_update_tickers", tickers_count)
    set_db_metadata("last_ohlcv_update_rows", rows_written)
    set_db_metadata("last_ohlcv_update_period", period)


def update_scan_metadata(results_count: int, skipped_count: int, latest_marker: str | None, universe_name: str = "ISSI"):
    set_db_metadata("last_nightly_scan_at", datetime.datetime.now(WIB).isoformat(timespec="seconds"))
    set_db_metadata("last_nightly_scan_marker", latest_marker or "")
    set_db_metadata("last_nightly_scan_results", results_count)
    set_db_metadata("last_nightly_scan_skipped", skipped_count)
    set_db_metadata("last_nightly_scan_universe", universe_name)


def populate_from_yfinance(tickers: list, period: str = "2y", batch_size: int = 50):
    """
    Initial bulk load dari yfinance — sekali jalan untuk populate DB.
    Batch per 50 ticker untuk menghindari rate-limit Yahoo Finance.
    Minta 2 tahun histori supaya adaptive baseline (MIN_HISTORY_FOR_ADAPTIVE=120
    hari) langsung terpenuhi dari hari pertama.
    """
    init_ohlcv_db()
    total = len(tickers)
    rows_written = 0
    latest_marker = None
    print(f"📥 Populate DB dari yfinance: {total} ticker, batch {batch_size}, period={period}...")
    yf_tickers = [f"{t}.JK" for t in tickers]
    for i in range(0, total, batch_size):
        batch = yf_tickers[i:i + batch_size]
        original = tickers[i:i + batch_size]
        try:
            raw = yf.download(batch, period=period, interval="1d",
                              auto_adjust=True, progress=False, threads=True)
            if raw.empty:
                continue
            for j, ticker in enumerate(original):
                yf_sym = f"{ticker}.JK"
                try:
                    if isinstance(raw.columns, pd.MultiIndex):
                        if yf_sym not in raw.columns.get_level_values(1):
                            continue
                        df_t = raw.xs(yf_sym, axis=1, level=1)[["Open","High","Low","Close","Volume"]]
                    else:
                        df_t = raw[["Open","High","Low","Close","Volume"]]
                    df_t = df_t.dropna()
                    if not df_t.empty:
                        rows_written += len(df_t)
                        latest_marker = str(df_t.index.max().date())
                        upsert_ohlcv_daily(ticker, df_t)
                except Exception as e:
                    print(f"  ⚠️ Gagal simpan {ticker}: {e}")
            print(f"  ✅ {min(i+batch_size, total)}/{total} ticker disimpan...")
            time.sleep(2.0)  # jeda antar batch untuk hindari rate-limit
        except Exception as e:
            print(f"  ⚠️ Batch {i}-{i+batch_size} gagal: {e}")
    update_db_update_metadata(total, period, rows_written, latest_marker)
    print(f"✅ Populate DB selesai. Ticker={total}, rows={rows_written}, updated_s/d={latest_marker}")
    return {
        "tickers": total,
        "rows_written": rows_written,
        "latest_marker": latest_marker,
        "period": period,
        "batch_size": batch_size,
    }


def get_ohlcv_smart(ticker: str, limit: int = 500) -> pd.DataFrame:
    """
    Entry point utama — cek SQLite dulu, refresh dari Yahoo Finance bila bar
    terbaru belum ada, lalu return data dari SQLite.
    """
    init_ohlcv_db()
    latest_in_db = get_latest_daily_date_in_db(ticker)
    today_marker = get_current_trading_day_close_marker()

    # BUGFIX (kasus nyata DOSS/FWCT/PMUI, ditemukan lewat log user — DOSS listing
    # Agustus 2024, seharusnya ~250 hari histori, tapi DB cuma keisi 10-11 bar):
    # urutan LAMA coba fetch "10d" DULUAN untuk SEMUA kasus, dan backfill penuh
    # "2y" cuma jalan sebagai FALLBACK kalau fetch "10d" itu GAGAL. Masalahnya,
    # fetch "10d" itu HAMPIR SELALU BERHASIL (Yahoo memang punya data 10 hari
    # terakhir) — jadi buat ticker yang BENAR-BENAR BARU di DB kita (latest_in_db
    # None, entah karena baru pertama kali di-/check atau baru masuk universe
    # scan), backfill 2 tahun itu efektifnya JADI DEAD CODE: fetch 10d berhasil,
    # cuma dapat ~10 bar, backfill penuh tidak pernah ke-trigger — padahal
    # saham itu sendiri sudah listing lama dan datanya ADA di Yahoo. Diperbaiki:
    # ticker BARU (latest_in_db None) SELALU dapat backfill 2 tahun langsung,
    # bukan cuma fallback.
    if latest_in_db is None:
        full = yfinance_get_kline(ticker, period="2y")
        if full is not None and not full.empty:
            upsert_ohlcv_daily(ticker, full)
    elif latest_in_db != today_marker:
        fresh = yfinance_get_kline(ticker, period="10d")
        if fresh is not None and not fresh.empty:
            upsert_ohlcv_daily(ticker, fresh)

    return get_ohlcv_daily_from_db(ticker, limit=limit)


def upsert_ohlcv_4h(ticker: str, df: pd.DataFrame):
    """Simpan bar 4H dari Yahoo Finance ke SQLite."""
    if df is None or df.empty:
        return
    conn = sqlite3.connect(OHLCV_DB_FILE)
    rows = [
        (ticker, str(idx), row.get("Open"), row.get("High"),
         row.get("Low"), row.get("Close"), row.get("Volume"))
        for idx, row in df.iterrows()
    ]
    conn.executemany(
        "INSERT OR REPLACE INTO ohlcv_4h(ticker, datetime, open, high, low, close, volume) VALUES(?,?,?,?,?,?,?)",
        rows
    )
    conn.commit()
    conn.close()


def get_ohlcv_4h(ticker: str, period: str = "1mo") -> pd.DataFrame:
    """
    Ambil data 4H dari DB kalau sudah ada bar hari ini — fallback fetch
    dari Yahoo Finance. Yahoo Finance native interval="4h" sudah terkonfirmasi
    selaras dengan sesi IDX (09:00 Sesi 1, 13:00 Sesi 2) tanpa perlu
    agregasi manual.
    """
    init_ohlcv_db()
    today_str = datetime.datetime.now(WIB).strftime("%Y-%m-%d")
    conn = sqlite3.connect(OHLCV_DB_FILE)
    rows = conn.execute(
        "SELECT datetime, open, high, low, close, volume FROM ohlcv_4h "
        "WHERE ticker=? ORDER BY datetime DESC LIMIT 50",
        (ticker,)
    ).fetchall()
    latest_dt = rows[0][0][:10] if rows else None
    conn.close()

    if latest_dt == today_str and rows:
        df = pd.DataFrame(rows, columns=["datetime","Open","High","Low","Close","Volume"])
        df["datetime"] = pd.to_datetime(df["datetime"])
        return df.set_index("datetime").sort_index()

    try:
        stock = get_yf_ticker(f"{ticker}.JK")
        df = yf_fetch_with_retry(lambda: stock.history(period=period, interval="4h", timeout=15))
        if not df.empty:
            df = df[["Open","High","Low","Close","Volume"]]
            upsert_ohlcv_4h(ticker, df)
            return df
    except Exception as e:
        print(f"⚠️ Gagal fetch 4H Yahoo Finance untuk {ticker}: {e}")
    return pd.DataFrame()


def get_db_stats() -> dict:
    """Statistik DB — untuk command /dbstats atau logging."""
    try:
        conn = sqlite3.connect(OHLCV_DB_FILE)
        daily_count = conn.execute("SELECT COUNT(DISTINCT ticker) FROM ohlcv_daily").fetchone()[0]
        daily_rows = conn.execute("SELECT COUNT(*) FROM ohlcv_daily").fetchone()[0]
        h4_count = conn.execute("SELECT COUNT(DISTINCT ticker) FROM ohlcv_4h").fetchone()[0]
        db_size_mb = os.path.getsize(OHLCV_DB_FILE) / 1024 / 1024
        meta = dict(conn.execute("SELECT key, value FROM db_metadata").fetchall()) if os.path.exists(OHLCV_DB_FILE) else {}
        conn.close()
        return {
            "daily_tickers": daily_count,
            "daily_rows": daily_rows,
            "h4_tickers": h4_count,
            "size_mb": round(db_size_mb, 2),
            "last_ohlcv_update_at": meta.get("last_ohlcv_update_at"),
            "last_ohlcv_update_marker": meta.get("last_ohlcv_update_marker"),
            "last_ohlcv_update_tickers": int(meta.get("last_ohlcv_update_tickers", 0) or 0),
            "last_ohlcv_update_rows": int(meta.get("last_ohlcv_update_rows", 0) or 0),
            "last_nightly_scan_at": meta.get("last_nightly_scan_at"),
            "last_nightly_scan_marker": meta.get("last_nightly_scan_marker"),
            "last_nightly_scan_results": int(meta.get("last_nightly_scan_results", 0) or 0),
            "last_nightly_scan_skipped": int(meta.get("last_nightly_scan_skipped", 0) or 0),
            "last_nightly_scan_universe": meta.get("last_nightly_scan_universe"),
        }
    except Exception:
        return {}


def get_current_calendar_date_marker() -> str:
    """
    MBSS v2 (user request — /eodscan akan rutin dijalankan pagi hari SEBELUM
    market buka, bukan cuma sore/malam): penanda tanggal KALENDER sederhana,
    TIDAK sensitif jam tutup market — beda dari
    get_current_trading_day_close_marker() yang sengaja geser ke "hari
    sebelumnya" sebelum jam 16:30 (itu cocok buat validitas data OHLCV, TAPI
    salah buat cache seperti broksum_250/bsjp_ara yang cuma perlu tahu
    "apakah ini dari HARI INI", bukan "apakah sesi hari ini sudah tutup").

    Tanpa ini: /eodscan jam 8 pagi -> tersimpan dgn marker "kemarin" (belum
    16:30) -> /broksum jam 13:00 hari SAMA -> "hari ini" sudah dianggap
    tanggal sekarang (sudah lewat 16:30 kalau dicek sore) -> mismatch ->
    cache dianggap basi padahal baru beberapa jam. Dengan penanda tanggal
    kalender murni, ini tidak terjadi — tetap dianggap segar sepanjang hari
    yang sama, baru basi betulan keesokan harinya.
    """
    now = datetime.datetime.now(WIB)
    reference_date = now.date()
    while reference_date.weekday() >= 5:
        reference_date -= datetime.timedelta(days=1)
    return reference_date.strftime("%Y-%m-%d")


def get_current_trading_day_close_marker() -> str:
    """
    Menandai hari bursa mana yang datanya (EOD) SEHARUSNYA sudah tersedia SAAT
    INI — dipakai untuk freshness check daily_scan_cache (hasil scan malam jam
    22:00 WIB). BEDA dari get_last_published_trading_day() (khusus jam publish
    Index Alpha 19:00) — di sini ambangnya jam tutup market IDX (~16:00 WIB,
    dikasih margin 30 menit untuk settlement). Sebelum market tutup hari ini,
    reference masih hari SEBELUMNYA (belum ada data EOD baru untuk sesi yang
    sedang berjalan).
    """
    now = datetime.datetime.now(WIB)
    market_closed_today = (now.hour, now.minute) >= (16, 30)
    reference_date = now.date() if market_closed_today else now.date() - datetime.timedelta(days=1)
    while reference_date.weekday() >= 5:
        reference_date -= datetime.timedelta(days=1)
    return reference_date.strftime("%Y-%m-%d")


def load_pending_orders() -> list:
    if not os.path.exists(PENDING_ORDERS_FILE):
        return []
    try:
        with open(PENDING_ORDERS_FILE) as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ Gagal membaca pending orders: {e}")
        return []


def save_pending_orders(orders: list):
    try:
        with open(PENDING_ORDERS_FILE, "w") as f:
            json.dump(orders, f, indent=2)
    except Exception as e:
        print(f"⚠️ Gagal menyimpan pending orders: {e}")


def check_order_touch_status(order: dict, quote: dict) -> dict:
    """
    PERKIRAAN saja, BUKAN kepastian — kita TIDAK punya akses ke akun broker
    asli, tidak tahu status order sebenarnya atau posisi antrian di level
    harga itu. Ini murni cek "apakah harga level order tersentuh hari ini",
    dipakai sebagai triase cepat: mana order yang "pasti belum" (harga belum
    mendekat sama sekali) vs "worth dicek di app broker" (harga sudah
    menyentuh level order, TAPI belum tentu order spesifik ini yang ke-match).
    """
    if not quote:
        return {"touched": None, "note": "gagal ambil harga live"}

    intraday_high = quote.get("h")
    intraday_low = quote.get("l")
    if intraday_high is None or intraday_low is None:
        return {"touched": None, "note": "data intraday tidak tersedia"}

    if order["side"] == "buy":
        touched = intraday_low <= order["price"]
        note = f"Low hari ini {intraday_low} {'<=' if touched else '>'} order {order['price']}"
    else:  # sell
        touched = intraday_high >= order["price"]
        note = f"High hari ini {intraday_high} {'>=' if touched else '<'} order {order['price']}"

    return {"touched": touched, "note": note}


def load_daytrade_picks_history() -> list:
    if not os.path.exists(DAYTRADE_PICKS_HISTORY_FILE):
        return []
    try:
        with open(DAYTRADE_PICKS_HISTORY_FILE) as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ Gagal membaca daytrade picks history: {e}")
        return []


def save_daytrade_picks_history(picks: list):
    try:
        with open(DAYTRADE_PICKS_HISTORY_FILE, "w") as f:
            json.dump(picks, f, indent=2)
    except Exception as e:
        print(f"⚠️ Gagal menyimpan daytrade picks history: {e}")


def compute_consecutive_appearance_streak_any_source(ticker: str, pick_date: str, history: list) -> int:
    """
    MBSS v2 (user request, lanjutan dari compute_consecutive_appearance_streak):
    versi LINTAS-SOURCE — ticker dianggap "muncul" di suatu hari kalau dia
    tercatat dari SOURCE MANAPUN hari itu (hc, screendaytrade, gptpick,
    consensus, check, testbrief), bukan cuma satu tool spesifik. Berguna
    karena streak per-source butuh waktu lama terkumpul untuk tool yang baru
    mulai mengunci picks (mis. /hc baru aktif, riwayatnya nyaris kosong),
    sementara riwayat GABUNGAN semua tool sudah ada sejak lama.
    """
    prior_dates = sorted(
        {p["pick_date"] for p in history if p["ticker"] == ticker and p["pick_date"] < pick_date},
        reverse=True,
    )
    streak = 1  # hitung pick_date ini sendiri
    check_date = pick_date
    for prior_date in prior_dates:
        expected_prev = get_previous_trading_day_marker(check_date)
        if prior_date == expected_prev:
            streak += 1
            check_date = prior_date
        else:
            break
    return streak


def compute_consecutive_appearance_streak(ticker: str, source: str, pick_date: str, history: list) -> int:
    """
    MBSS v2 (user request): berapa kali ticker ini muncul BERTURUT-TURUT
    (hari bursa berurutan, tanpa jeda) dari SOURCE yang sama, berakhir di
    pick_date ini (termasuk pick_date itu sendiri, jadi minimal 1). Dipakai
    untuk uji hipotesis user: "makin sering muncul beruntun di scanner,
    makin yakin sinyalnya benar (meski kadang sudah telat)".

    Simplifikasi yang sama seperti compute_trading_days_held di seluruh
    codebase — cuma skip akhir pekan, TIDAK memperhitungkan libur nasional
    IDX. Kalau scan tidak sempat jalan di suatu hari (bukan soal sinyal
    hilang, tapi bot tidak jalan/error), streak akan reset walau
    sebenarnya bukan itu yang terjadi — keterbatasan yang perlu disadari,
    bukan bug.
    """
    prior_dates = sorted(
        {p["pick_date"] for p in history
         if p["ticker"] == ticker and p.get("source", "screendaytrade") == source and p["pick_date"] < pick_date},
        reverse=True,
    )
    streak = 1  # hitung pick_date ini sendiri
    check_date = pick_date
    for prior_date in prior_dates:
        expected_prev = get_previous_trading_day_marker(check_date)
        if prior_date == expected_prev:
            streak += 1
            check_date = prior_date
        else:
            break
    return streak


def get_previous_trading_day_marker(date_str: str) -> str:
    """Hari bursa SEBELUM date_str — cuma skip Sabtu/Minggu (simplifikasi yang sama di seluruh codebase)."""
    d = datetime.datetime.strptime(date_str, "%Y-%m-%d").date()
    d -= datetime.timedelta(days=1)
    while d.weekday() >= 5:  # 5=Sabtu, 6=Minggu
        d -= datetime.timedelta(days=1)
    return d.strftime("%Y-%m-%d")


def lock_daily_daytrade_picks(top_candidates: list, source: str = "screendaytrade"):
    """
    Kunci picks hari ini (IMMUTABLE begitu tersimpan) — dipakai untuk uji winrate
    nanti. Tiap pick disimpan dengan TP1/cut_loss ASLI dari rekomendasi hari itu
    (bukan angka tetap terpisah) — supaya benar-benar menguji akurasi rekomendasi
    sistem, bukan pertanyaan generik "naik X% dalam Y hari". Saham yang sama bisa
    muncul di beberapa tanggal berbeda — ini SENGAJA, karena tiap hari adalah
    sinyal/keputusan independen yang diuji terpisah, bukan "apakah saham X bagus".
    Tidak menambah entri duplikat untuk ticker+pick_date+source yang sama (idempotent).

    source: "screendaytrade" (default) atau "gptpick" — dua command berbeda
    (universe & kriteria seleksi beda) sama-sama lewat mekanisme lock/resolve
    ini, dibedakan lewat field ini. Ditambahkan sebagai bagian dari Sprint 2
    (MBSS v2 Sprint 2 revisi, Tier 1.4) — bukan tracker terpisah, cukup satu
    field tambahan di sistem yang sudah ada, supaya /winrate bisa menampilkan
    keduanya tanpa duplikasi logic.
    """
    history = load_daytrade_picks_history()
    pick_date = get_current_trading_day_close_marker()
    existing_keys = {(p["ticker"], p["pick_date"], p.get("source", "screendaytrade")) for p in history}

    added = 0
    for r in top_candidates:
        ticker = r["ticker"]
        if (ticker, pick_date, source) in existing_keys:
            continue  # sudah dikunci hari ini untuk source ini, jangan duplikat

        # MBSS v2 (user request): berapa kali BERTURUT-TURUT ticker ini sudah
        # muncul dari source yang sama, berakhir di hari ini — dipakai untuk
        # uji hipotesis "makin sering muncul beruntun, makin yakin sinyalnya
        # benar". History yang dipakai untuk hitung ini BELUM termasuk entri
        # baru yang sedang dibangun sekarang (dihitung sebelum di-append).
        streak = compute_consecutive_appearance_streak(ticker, source, pick_date, history)

        # MBSS v2 (user request, tindak lanjut analisis /winrate): snapshot
        # fitur teknikal LENGKAP saat pick dikunci — sebelumnya cuma
        # signal_label + daytrade_score yang tersimpan, tidak cukup buat
        # analisis "parameter mana yang genuinely prediktif" nanti. TIDAK
        # retroaktif ke 162 pick yang sudah ada, cuma berlaku pick BARU
        # mulai sekarang.
        feature_snapshot = {
            "rsi": r.get("rsi"), "adx": r.get("adx"), "cmf": r.get("cmf"),
            "vol_ratio": r.get("vol_ratio"), "macd_state": r.get("macd_state"),
            "relative_strength_vs_ihsg": r.get("relative_strength_vs_ihsg"),
            "day_range_pct_10d": r.get("day_range_pct_10d"), "close_pos_day": r.get("close_pos_day"),
            "pe": r.get("pe"), "pb": r.get("pb"), "dividend_yield_pct": r.get("dividend_yield_pct"),
            "is_below_ema21": r.get("is_below_ema21"), "is_below_sma50": r.get("is_below_sma50"),
            "high_conviction_met": r.get("high_conviction", {}).get("criteria_met"),
            "high_conviction_checkable": r.get("high_conviction", {}).get("criteria_checkable"),
            "risk_character": r.get("risk_character"), "action_id": r.get("action_id"),
            "value_traded": r.get("value_traded"),
            # MBSS v2 (user request — bahan riset "apakah squeeze genuinely
            # prediktif, bukan cuma klaim dari thread orang lain"): snapshot
            # Bollinger di saat pick dikunci. TIDAK dipakai buat apa pun
            # otomatis sekarang — murni data buat dianalisis setelah cukup
            # sampel forward terkumpul (sama disiplin dengan seluruh
            # tracking backbone AB-RC1: forward-validate dulu, baru
            # pertimbangkan naikkan bobot).
            "bollinger_squeeze": r.get("bollinger_squeeze"),
            "bollinger_bandwidth_percentile": r.get("bollinger_bandwidth_percentile"),
            "bb_signal_note": r.get("bb_signal_note"),
            # MBSS v2 (user request — riset speed-to-move menunjukkan FAST
            # (<=1 hari resolve) menang 95.7% vs SLOW 40.1%): snapshot tag
            # fast_candidate SAAT pick dikunci. SAMA seperti bollinger_squeeze
            # di atas — murni data buat validasi forward (apakah tag ini
            # BENERAN memprediksi resolve cepat + menang, di luar sampel yang
            # dipakai buat menemukan kriterianya), belum menggating apa pun.
            "fast_candidate": compute_fast_candidate_tag(r).get("is_fast_candidate"),
            "fast_candidate_formula_version": FAST_CANDIDATE_FORMULA_VERSION,
        }

        # MBSS v2 (user request — evaluasi winrate PER BROKER smart money):
        # snapshot broker whitelist mana yang NET BUY ticker ini SAAT pick
        # dikunci — supaya nanti (setelah resolve) bisa dihitung "saham yang
        # lagi di-akumulasi broker X, seberapa sering menang". TIDAK
        # retroaktif (broksum_250 baru ada mulai sesi ini) — cuma berlaku
        # pick BARU. Import lokal (bukan di atas file) sengaja — legacy_core
        # ini di-import OLEH broker.py & nightly.py, import balik di level
        # modul bakal circular; aman dipanggil di sini karena keduanya sudah
        # ter-load penuh saat fungsi ini benar-benar jalan.
        smart_money_at_lock = []
        try:
            import engine.broker as _broker_engine
            import engine.nightly as _nightly_engine
            broksum_data = _nightly_engine.load_broksum_250()
            smart_money_at_lock = _broker_engine.get_smart_money_accumulation(ticker, broksum_data)
        except Exception as e:
            print(f"⚠️ Gagal ambil snapshot smart money buat {ticker}: {e}")

        history.append({
            "ticker": ticker,
            "pick_date": pick_date,
            "source": source,
            # MBSS v2 Sprint 2 (Tier 1.4, lanjutan): label sinyal saat pick dikunci —
            # untuk /screendaytrade ini lane dari compute_screendaytrade_positive_bias
            # (PRIORITY FRESH / PRIORITY CONT / SECONDARY WATCH / LOW EDGE / CHASE /
            # EXTENDED / CHASE WATCH), untuk /gptpick ini action_label_id
            # (mis. "SINYAL CAMPURAN", "BELI KUAT"). Dipakai /winrate untuk
            # mengelompokkan winrate PER JENIS SINYAL — supaya bisa dilihat langsung
            # jenis sinyal mana yang secara empiris lebih akurat, bukan cuma winrate
            # gabungan semua sinyal jadi satu angka.
            "signal_label": r.get("_positive_lane") or r.get("action_label_id") or "N/A",
            "consecutive_streak": streak,
            "smart_money_at_lock": smart_money_at_lock,
            "feature_snapshot": feature_snapshot,
            "tp1": r["targets"]["tp_1"],
            "cut_loss": r["targets"]["cut_loss"],
            "action_label": r.get("action_label_id"),
            "daytrade_score": compute_daytrade_score(r),
            "entry_price": None,
            "entry_date": None,
            "status": "pending_entry",
            "resolved_date": None,
            "pnl_pct": None,
            "days_checked": 0,
            "day2_pnl_pct": None,
            "day3_pnl_pct": None,
        })
        added += 1

    if added > 0:
        save_daytrade_picks_history(history)
        print(f"🔒 {added} {source} pick dikunci untuk {pick_date}")
    return added


def resolve_daytrade_picks():
    """
    Job harian — proses SEMUA picks yang masih pending:
    1. status='pending_entry': kalau sudah ada hari bursa baru setelah pick_date,
       ambil harga OPEN hari itu sebagai entry_price (objektif, bukan asumsi
       "beli di tengah range" yang subjektif) — lalu pindah ke pending_resolution.
    2. status='pending_resolution': cek High/Low hari terbaru — kalau High>=TP1
       duluan -> WIN, kalau Low<=cut_loss duluan -> LOSE. Kalau days_checked
       sudah mencapai WINRATE_RESOLUTION_WINDOW_DAYS tanpa keduanya tersentuh,
       resolve time-based dari close terakhir vs entry_price.
    """
    history = load_daytrade_picks_history()
    if not history:
        return 0

    changed = 0
    tickers_needing_kline = {p["ticker"] for p in history if p["status"] in ("pending_entry", "pending_resolution")}
    kline_cache = {}
    for ticker in tickers_needing_kline:
        try:
            hist = get_ohlcv_smart(ticker, limit=30)
            if hist is not None and not hist.empty:
                kline_cache[ticker] = hist
        except Exception as e:
            print(f"⚠️ Gagal fetch kline untuk resolusi {ticker}: {e}")

    for pick in history:
        if pick["status"] not in ("pending_entry", "pending_resolution"):
            continue
        hist = kline_cache.get(pick["ticker"])
        if hist is None:
            continue

        bars_after_pick = hist[hist.index.date.astype(str) > pick["pick_date"]]
        if bars_after_pick.empty:
            continue  # belum ada hari bursa baru sejak pick dikunci

        if pick["status"] == "pending_entry":
            first_bar = bars_after_pick.iloc[0]
            pick["entry_price"] = float(first_bar["Open"])
            pick["entry_date"] = str(bars_after_pick.index[0].date())
            pick["status"] = "pending_resolution"
            changed += 1
            bars_to_check = bars_after_pick.iloc[1:]  # mulai cek TP/SL dari hari SETELAH entry
        else:
            entry_date_str = pick["entry_date"]
            bars_to_check = hist[hist.index.date.astype(str) > entry_date_str]

        if pick["status"] == "pending_resolution" and not bars_to_check.empty:
            for _, bar in bars_to_check.iterrows():
                pick["days_checked"] += 1

                # MBSS v2 (user request): snapshot PnL% di checkpoint hari ke-2
                # dan ke-3 (dari CLOSE hari itu vs entry_price) — TIDAK
                # menyelesaikan pick, cuma catat "seandainya keluar di sini".
                # Dipakai nanti buat analisis "exit di hari-3 sebagai trade-off
                # opportunity vs tunggu sampai selesai (hari-5 atau TP/SL)".
                if pick["days_checked"] == 2:
                    pick["day2_pnl_pct"] = round((float(bar["Close"]) - pick["entry_price"]) / pick["entry_price"] * 100, 2)
                elif pick["days_checked"] == 3:
                    pick["day3_pnl_pct"] = round((float(bar["Close"]) - pick["entry_price"]) / pick["entry_price"] * 100, 2)

                if bar["High"] >= pick["tp1"]:
                    pick["status"] = "win"
                    pick["pnl_pct"] = round((pick["tp1"] - pick["entry_price"]) / pick["entry_price"] * 100, 2)
                    pick["resolved_date"] = str(bar.name.date())
                    changed += 1
                    break
                elif bar["Low"] <= pick["cut_loss"]:
                    pick["status"] = "lose"
                    pick["pnl_pct"] = round((pick["cut_loss"] - pick["entry_price"]) / pick["entry_price"] * 100, 2)
                    pick["resolved_date"] = str(bar.name.date())
                    changed += 1
                    break
                elif pick["days_checked"] >= WINRATE_RESOLUTION_WINDOW_DAYS:
                    final_close = float(bar["Close"])
                    pnl = (final_close - pick["entry_price"]) / pick["entry_price"] * 100
                    pick["status"] = "win_timebased" if pnl > 0 else "lose_timebased"
                    pick["pnl_pct"] = round(pnl, 2)
                    pick["resolved_date"] = str(bar.name.date())
                    changed += 1
                    break

    if changed:
        save_daytrade_picks_history(history)
    return changed



    """
    Menandai hari bursa mana yang datanya (EOD) SEHARUSNYA sudah tersedia SAAT
    INI — dipakai untuk freshness check daily_scan_cache (hasil scan malam jam
    22:00 WIB). BEDA dari get_last_published_trading_day() (khusus jam publish
    Index Alpha 19:00) — di sini ambangnya jam tutup market IDX (~16:00 WIB,
    dikasih margin 30 menit untuk settlement). Sebelum market tutup hari ini,
    reference masih hari SEBELUMNYA (belum ada data EOD baru untuk sesi yang
    sedang berjalan).
    """
    now = datetime.datetime.now(WIB)
    market_closed_today = (now.hour, now.minute) >= (16, 30)
    reference_date = now.date() if market_closed_today else now.date() - datetime.timedelta(days=1)
    while reference_date.weekday() >= 5:
        reference_date -= datetime.timedelta(days=1)
    return reference_date.strftime("%Y-%m-%d")


# NOTE (MBSS v2 refactor, Phase 4): get_last_published_trading_day(),
# _load_brokersum_cache(), _save_brokersum_cache(), get_cached_brokersum()
# moved to engine/broker.py (BrokerEngine). Call sites use `broker_engine.xxx`.


HARD_NEGATIVE_FLAGS_CHECK = ("lower_highs_bearish",)  # chart_pattern value checked separately



ACTION_PRIORITY_LABEL_ID = {
    "TAKE_PROFIT_CANDIDATE": "🟢 Take Profit Candidate",
    "HOLD": "🔵 Hold",
    "WATCH_CLOSELY": "🟡 Watch Closely",
    "EXIT_CANDIDATE": "🔴 Exit Candidate",
}


# NOTE (MBSS v2 refactor, Sprint 2 Tier 1.1): classify_action_priority, classify_risk_character moved to
# engine/scoring.py (Central Scoring Engine). Call sites use
# `scoring_engine.xxx` — see the import near the top of this file.

def classify_lifecycle_category(days_held: int, scoring: dict) -> dict:
    """
    Deterministic position lifecycle classification for swing-trade framing —
    computed in Python, not left to the LLM, same architecture as decide_action().
    Returns {category, reason, is_good_signal}.

    REVISI user: prioritas utama sekarang "sudah capai target atau belum", BUKAN
    cuma hari dipegang. Contoh nyata yang jadi dasar: ERAA +6% hari pertama →
    harus segera dievaluasi realisasi meski baru sehari; TLKM 30 hari yang baru
    mulai breakout → tetap boleh ditahan meski sudah lama, selama momentum masih
    hidup. Horizon disesuaikan ke 1-5 hari (dari 2-10 sebelumnya) sesuai orientasi
    swing pendek user.

    days_held=None means entry_date hasn't been backfilled yet — category is
    withheld entirely rather than guessed, since a wrong guess (e.g. defaulting
    to day 0) could silently mislabel a stale position as fresh.
    """
    if days_held is None:
        return {"category": "BELUM_DIKETAHUI", "reason": "entry_date belum diisi", "is_good_signal": None}

    final_score = scoring.get("scores", {}).get("final", 0)
    has_hard_negative = (
        scoring.get("chart_pattern") in HARD_NEGATIVE_FLAGS_CHECK
        or scoring.get("is_financial_distress_flag", False)
        or scoring.get("macd_bearish_cross", False)
        or scoring.get("obv_divergence") == "bearish_divergence"
    )
    is_good_signal = (final_score >= 5.0) and not has_hard_negative

    # PRIORITAS #1 (override semua yang lain, tidak peduli hari): sudah capai
    # atau lewati TP1 — ini kasus ERAA, realisasi profit tidak boleh menunggu
    # "waktu ideal" kalau target sudah kena.
    price = scoring.get("price")
    tp1 = scoring.get("targets", {}).get("tp_1")
    if price and tp1 and price >= tp1:
        return {
            "category": "TARGET_TERCAPAI",
            "reason": f"harga {price} sudah capai/lewati TP1 ({tp1}) — pertimbangkan realisasi, terlepas dari {days_held} hari dipegang",
            "is_good_signal": True,
        }

    # PRIORITAS #2: belum capai target — momentum yang masih hidup lebih penting
    # dari sekadar berapa lama dipegang (kasus TLKM: lama tapi baru breakout).
    if days_held <= 1:
        return {"category": "BARU", "reason": f"baru {days_held} hari bursa", "is_good_signal": is_good_signal}

    if is_good_signal:
        reason = "sinyal momentum masih hidup, on track menuju target"
        if days_held > 5:
            reason += f" — sudah {days_held} hari (lewat window ideal 1-5 hari), TAPI momentum masih mendukung, boleh ditahan"
        return {"category": "PRODUKTIF", "reason": reason, "is_good_signal": True}

    if days_held > 5:
        return {"category": "EVALUASI", "reason": f"{days_held} hari, momentum sudah melemah, belum capai target — pertimbangkan opportunity cost", "is_good_signal": False}

    return {"category": "HATI_HATI", "reason": f"{days_held} hari, momentum melemah tapi masih dalam window 1-5 hari", "is_good_signal": False}


LIFECYCLE_LABEL_ID = {
    "TARGET_TERCAPAI": "🎯 TARGET TERCAPAI",
    "BARU": "🆕 BARU",
    "PRODUKTIF": "🟢 PRODUKTIF",
    "HATI_HATI": "🟡 HATI-HATI",
    "EVALUASI": "🔴 EVALUASI",
    "BELUM_DIKETAHUI": "❔ BELUM DIKETAHUI",
}


def estimate_tp_horizon(scoring: dict) -> dict:
    """
    Estimates a TIME WINDOW (not a precise date) for reaching TP1, derived from
    real distance-to-target divided by the stock's own recent volatility
    (day_range_pct_10d) — an honest estimate grounded in real data, explicitly
    NOT a prediction/guarantee. Confidence reflects whether momentum signals
    actually agree with each other, not a fabricated statistical number.
    """
    price = scoring.get("price", 0)
    tp1 = scoring.get("targets", {}).get("tp_1")
    day_range_pct = scoring.get("day_range_pct_10d", 0) or 0

    if not price or not tp1 or day_range_pct <= 0:
        return {"horizon_days_low": None, "horizon_days_high": None, "confidence": "Rendah"}

    distance_pct = abs((tp1 - price) / price * 100)
    # Rough daily "pace" = day_range_pct / 10 (since day_range_pct spans 10 days);
    # days needed = distance / daily pace, with a +/- band for the estimate range.
    daily_pace = max(0.3, day_range_pct / 10)
    est_days = distance_pct / daily_pace
    horizon_low = max(1, round(est_days * 0.7))
    horizon_high = max(horizon_low + 1, round(est_days * 1.3))

    agree_count = 0
    total_checks = 0
    momentum_score = scoring.get("scores", {}).get("momentum", 0)
    total_checks += 1
    if momentum_score >= 6.0:
        agree_count += 1
    total_checks += 1
    if scoring.get("macd_state") == "bullish":
        agree_count += 1
    total_checks += 1
    if scoring.get("action_id") in ("STRONG_BUY", "BUY_ACCUMULATE"):
        agree_count += 1

    if agree_count == total_checks:
        confidence = "Tinggi"
    elif agree_count >= total_checks - 1:
        confidence = "Sedang"
    else:
        confidence = "Rendah"

    return {"horizon_days_low": horizon_low, "horizon_days_high": horizon_high, "confidence": confidence}


# NOTE (MBSS v2 refactor, Sprint 2 Tier 1.1): compute_high_conviction_score moved to
# engine/scoring.py (Central Scoring Engine). Call sites use
# `scoring_engine.xxx` — see the import near the top of this file.


def compute_daytrade_score(scoring: dict) -> float:
    """
    Ranking khusus untuk /screendaytrade — BEDA dari final_score brief pagi
    (Value 25% + Momentum 45% + Sentiment 30%). Untuk kebutuhan "saham mana yang
    lagi bergerak SEKARANG", value/fundamental kurang relevan; yang penting
    aktivitas/momentum saat ini. Tidak memakai brokersum sama sekali (data EOD
    1 hari lag, tidak cocok untuk horizon day trade jam-menit).

    Komponen (bobot direvisi lagi untuk menambahkan risk:reward):
    - volatility (20%): day_range_pct_10d — seberapa "hidup" saham ini akhir2 ini
    - volume_activity (15%): vol_ratio — apakah hari ini ramai dibanding biasanya
    - momentum_freshness (15%): MACD cross dengan PELURUHAN presisi per hari —
      cross 1 hari lalu jauh lebih fresh/relevan untuk day trade daripada cross
      4-5 hari lalu, meski status "bullish"-nya sama.
    - breakout_proximity (10%): seberapa dekat harga ke intraday high — dekat
      high = berpotensi breakout lanjut hari ini
    - ema21_momentum (10%): apakah harga di atas/bawah EMA21 — sinyal arah
      jangka pendek tambahan
    - adx_component (15%): ADX TINGGI = kriteria POSITIF *hanya kalau arah
      trennya tidak bearish* (di atas EMA21 & MACD tidak bearish) — day trade
      mencari saham dengan tren jelas, TAPI cuma kalau trennya searah dengan
      niat beli. ADX tinggi di tren turun (di bawah EMA21 / MACD bearish)
      dibalik jadi PENALTI, bukan bonus — makin kuat downtrend-nya, makin
      rendah skornya. (Direvisi MBSS v2 Sprint 2, ditemukan lewat kasus nyata
      UNTR: ADX tinggi tanpa cek arah bikin saham yang lagi longsor kuat
      kelihatan "aktif = bagus", padahal itu sinyal buruk untuk BELI.)
    - risk_reward_component (15%, BARU): risk_reward_at_max — SENGAJA dipisah
      dari core Value/Momentum/Sentiment/Final (yang dipakai /check, dst),
      karena RR menjawab pertanyaan BERBEDA ("apakah harga SEKARANG entry yang
      menguntungkan") dari "apakah saham ini bagus secara teknikal/fundamental".
      Untuk /screendaytrade khusus, timing entry itu sentral ke tujuan command
      ini, jadi masuk akal jadi komponen skor di sini — TIDAK di core scoring.
    """
    volatility = min(10.0, scoring.get("day_range_pct_10d", 0) or 0)
    volume_activity = min(10.0, (scoring.get("vol_ratio", 1.0) or 1.0) * 3.0)

    momentum_freshness = 5.0  # baseline netral
    macd_cross_days_ago = scoring.get("macd_cross_days_ago")
    macd_cross_direction = scoring.get("macd_cross_direction")
    if macd_cross_days_ago is not None:
        decay = max(0.0, 1 - (macd_cross_days_ago / 5))
        if macd_cross_direction == "bullish":
            momentum_freshness = 5.0 + (5.0 * decay)
        elif macd_cross_direction == "bearish":
            momentum_freshness = 5.0 - (4.0 * decay)
    elif scoring.get("macd_state") == "bullish":
        momentum_freshness = 6.5

    breakout_proximity = 5.0
    price = scoring.get("price")
    intraday_high = scoring.get("intraday_high")
    intraday_low = scoring.get("intraday_low")
    if price and intraday_high and intraday_low and intraday_high > intraday_low:
        position_in_range = (price - intraday_low) / (intraday_high - intraday_low)
        breakout_proximity = position_in_range * 10.0

    ema21_momentum = 5.0
    is_below_ema21 = scoring.get("is_below_ema21")
    if is_below_ema21 is not None:
        ema21_momentum = 3.0 if is_below_ema21 else 8.0

    # MBSS v2 Sprint 2 (Tier 1.4, lanjutan — user request, ditemukan lewat kasus
    # nyata UNTR): ADX cuma ngukur KEKUATAN tren, bukan ARAHNYA. Versi sebelumnya
    # menghargai ADX tinggi tanpa syarat ("day trade mencari saham dengan tren
    # jelas") — tapi tren turun yang kuat (ADX tinggi + di bawah EMA21 + MACD
    # bearish) ikut dapat skor bagus di komponen ini, padahal itu justru sinyal
    # BURUK untuk day trade BELI, bukan bagus. Sekarang: ADX tinggi cuma
    # dihargai kalau arah tren TIDAK bearish (di atas EMA21 DAN MACD tidak
    # bearish). Kalau bearish, dibalik — makin kuat trennya (makin tinggi ADX),
    # makin RENDAH skornya, karena itu artinya downtrend yang makin meyakinkan.
    raw_adx = min(10.0, (scoring.get("adx", 0) or 0) / 4.0)
    is_bearish_trend = bool(is_below_ema21) or (scoring.get("macd_state") == "bearish")
    adx_component = max(0.0, 10.0 - raw_adx) if is_bearish_trend else raw_adx

    # Risk:Reward — pakai RR@entry-atas (paling realistis untuk day trade breakout
    # buying). RR=1.0 -> skor 4 (median), RR=2.5+ -> skor maksimal 10, RR<=0/tidak
    # ada data -> netral (5.0), bukan dihukum (karena bisa jadi data tidak cukup,
    # bukan berarti benar-benar buruk).
    rr_at_max = scoring.get("targets", {}).get("risk_reward_at_max")
    if rr_at_max is None:
        risk_reward_component = 5.0
    else:
        risk_reward_component = min(10.0, max(0.0, rr_at_max * 4.0))

    daytrade_score = (
        volatility * 0.20 + volume_activity * 0.15
        + momentum_freshness * 0.15 + breakout_proximity * 0.10
        + ema21_momentum * 0.10 + adx_component * 0.15
        + risk_reward_component * 0.15
    )
    return round(daytrade_score, 2)


# NOTE (MBSS v2 refactor, Sprint 2 Tier 1.1): compute_brokersum_priority, _apply_brokersum_adjustment_original, apply_brokersum_adjustment moved to
# engine/scoring.py (Central Scoring Engine). Call sites use
# `scoring_engine.xxx` — see the import near the top of this file.



# NOTE (MBSS v2 refactor, Phase 4): extract_brokersum_from_screenshot(),
# compute_brokersum_from_screenshot_data(), _load/_save_brokersum_history(),
# append_brokersum_history(), compute_brokersum_trend(), fetch_zapi_stock_summary(),
# compute_brokersum_metrics_zapi(), get_cached_or_fetch_brokersum(), and
# compute_brokersum_metrics() all moved to engine/broker.py (BrokerEngine).
# Call sites use `broker_engine.xxx` — see the import near the top of this file.
# ZAPI_API_KEY / ZAPI_HEADERS / ZAPI_BASE_URL stay here (also used by
# load_or_build_issi_liquid_whitelist, not just BrokerEngine).
ZAPI_API_KEY  = os.environ.get("ZAPI_API_KEY", "")
ZAPI_HEADERS  = {"x-api-key": ZAPI_API_KEY}
ZAPI_BASE_URL = os.environ.get("ZAPI_BASE_URL", "https://api.zpi.web.id/v1")


def itick_get_kline_batch(tickers, limit=10):
    """
    Fetches kline data for UP TO 3 tickers in one call via the batch endpoint.
    CONFIRMED via testing: this endpoint caps at 10 bars per ticker regardless of
    the requested limit, no matter how many symbols are batched — so this is ONLY
    suitable for lightweight checks (like the whitelist eligibility check), NOT
    for full adaptive scoring which needs 120+ bars. Returns a dict of
    ticker -> DataFrame (or ticker -> None if that specific ticker had no data).
    """
    if len(tickers) > ITICK_BATCH_SIZE:
        raise ValueError(f"Batch size {len(tickers)} exceeds confirmed limit of {ITICK_BATCH_SIZE}")
    codes = ",".join(tickers)
    try:
        resp = requests.get(
            f"{ITICK_BASE_URL}/stock/klines",
            params={"region": "ID", "codes": codes, "kType": "8", "limit": limit},
            headers=ITICK_HEADERS,
            timeout=15,
        )
        data = resp.json()
        if data.get("code") != 0:
            print(f"⚠️ iTick batch kline non-zero response for {codes}: code={data.get('code')}, msg={data.get('msg')}")
            return {t: None for t in tickers}

        result = data.get("data", {})
        output = {}
        for ticker in tickers:
            bars = result.get(ticker)
            if not bars:
                output[ticker] = None
                continue
            df = pd.DataFrame(bars)
            df["date"] = pd.to_datetime(df["t"], unit="ms")
            df = df.set_index("date").sort_index()
            df = df.rename(columns={"o": "Open", "h": "High", "l": "Low", "c": "Close", "v": "Volume"})
            output[ticker] = df[["Open", "High", "Low", "Close", "Volume"]]
        return output
    except Exception as e:
        print(f"⚠️ iTick batch kline fetch failed for {codes}: {e}")
        return {t: None for t in tickers}


def load_or_build_whitelist(all_tickers, force_rebuild=False):
    """
    Returns the eligible ticker list for this month — from cache if still fresh,
    otherwise rebuilds by checking every ticker.

    IMPORTANT: This default whitelist no longer uses iTick. It uses the same
    Yahoo/SQLite path as the main scoring pipeline so the log and data source are
    consistent with the rest of the bot.
    """
    all_tickers = list(all_tickers)
    current_month = datetime.datetime.now(WIB).strftime("%Y-%m")

    if not force_rebuild and os.path.exists(WHITELIST_CACHE_FILE):
        try:
            with open(WHITELIST_CACHE_FILE) as f:
                cache = json.load(f)
            if cache.get("generated_month") == current_month:
                cached_eligible = [t for t in cache.get("eligible_tickers", []) if t in all_tickers]
                print(f"📋 Using cached Yahoo whitelist from {current_month}: {len(cached_eligible)} eligible tickers")
                return cached_eligible
        except Exception as e:
            print(f"⚠️ Failed to read whitelist cache: {e}. Rebuilding.")

    print(f"🔄 Building fresh monthly Yahoo whitelist for {current_month} — checking {len(all_tickers)} tickers "
          f"via yfinance/SQLite (one-time cost per month, subsequent daily runs will be faster)...")
    eligible = []
    excluded = {}

    # Yahoo can rate-limit on Termux, so pace the rebuild. This is still cleaner
    # than using iTick for only the whitelist while the rest of the bot uses Yahoo.
    YF_WHITELIST_PAUSE_EVERY = 25
    YF_WHITELIST_COOLDOWN_SECONDS = 20

    for idx, ticker in enumerate(all_tickers, 1):
        try:
            # Prefer DB + incremental Yahoo refresh. 30 bars are enough for the
            # whitelist's lightweight price/range/liquidity eligibility check.
            hist = get_ohlcv_smart(ticker, limit=30)
            if hist is None or hist.empty or len(hist) < 10:
                # Unknown/transient data issue: include by default so we do not
                # accidentally shrink the universe because Yahoo had a bad moment.
                eligible.append(ticker)
                continue

            is_eligible, reason = evaluate_eligibility_from_hist(
                hist["Close"].iloc[-1], hist["Low"], hist["High"]
            )
            if is_eligible is False:
                excluded[ticker] = reason
            else:
                eligible.append(ticker)
        except Exception as e:
            print(f"⚠️ Whitelist eligibility check failed for {ticker}: {e} — including by default")
            eligible.append(ticker)

        if idx < len(all_tickers) and idx % YF_WHITELIST_PAUSE_EVERY == 0:
            print(f"⏳ Yahoo whitelist build: cooling down {YF_WHITELIST_COOLDOWN_SECONDS}s "
                  f"({idx}/{len(all_tickers)} checked)...")
            time.sleep(YF_WHITELIST_COOLDOWN_SECONDS)
        else:
            time.sleep(0.3)

    cache = {"generated_month": current_month, "eligible_tickers": eligible, "excluded_tickers": excluded, "source": "yfinance_sqlite"}
    try:
        with open(WHITELIST_CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        print(f"⚠️ Failed to save whitelist cache: {e}")

    print(f"✅ Yahoo whitelist built: {len(eligible)} eligible, {len(excluded)} excluded")
    return eligible



# ==========================================
# 🎯 DETERMINISTIC ACTION DECISION (Gemini explains this, never decides or overrides it)
# ==========================================
# Repeated testing showed prompt instructions alone were NOT reliably enforced — the
# LLM ignored an explicit "no STRONG BUY" rule at least once, and silently skipped a
# "default to HOLD" rule at least twice. Rather than keep patching the prompt, the
# final action is now decided here in plain code and handed to Gemini as a fact to
# explain, not a decision it makes. To avoid the opposite failure (a rigid score
# cliff being unfairly strict at borderline cases), this includes a MIXED_SIGNALS
# state for genuine internal disagreement — instead of forcing false confidence.
# NOTE (MBSS v2 refactor, Sprint 2 Tier 1.1): ACTION_RANK, ACTION_LABEL_ID, decide_action moved to
# engine/scoring.py (Central Scoring Engine). Call sites use
# `scoring_engine.xxx` — see the import near the top of this file.



# Bump this whenever the scoring FORMULA itself changes (RSI banding, weight changes,
# new factors, etc). This makes it visible when a score difference between two runs is
# due to a real formula change vs. genuine day-to-day market movement — comparing scores
# across different versions isn't apples-to-apples.
SCORING_FORMULA_VERSION = "3.17.1"  # v3.17.1: tambah field macd_line_above_zero (dipakai TRUE EXPLOSIVE /consensus) di compute_factor_scoring; v3.17.0: Bollinger Band band-touch adjustment ke sentiment_score (+-1.5, digate ADX/EMA21 biar band walking di trend kuat tidak salah dibaca sebagai reversal) — lihat compute_factor_scoring di engine/scoring.py.


# NOTE (MBSS v2 refactor, Sprint 2 Tier 1.1): compute_factor_scoring moved to
# engine/scoring.py (Central Scoring Engine). Call sites use
# `scoring_engine.xxx` — see the import near the top of this file.

def smart_round_floor(price):
    """Floor rounding (selalu turun) — untuk entry bawah."""
    if price < 200:    return int(price)
    elif price < 500:  return int(price / 5) * 5
    elif price < 2000: return int(price / 10) * 10
    else:              return int(price / 25) * 25


def smart_round_price(price):
    """Nearest rounding berbasis kisaran harga — menggantikan kelipatan 5 universal."""
    if price < 200:    return round(price)
    elif price < 500:  return round(price / 5) * 5
    elif price < 2000: return round(price / 10) * 10
    else:              return round(price / 25) * 25


def get_entry_bawah_context(entry_bawah, sma20, sma50, support_10d, price):
    """Label konteks teknikal entry bawah — untuk narasi Gemini."""
    if sma20 and abs(entry_bawah - sma20) / price < 0.02:
        return "≈ MA20 (zona pullback sehat, buyer biasanya masuk di sini)"
    elif sma50 and abs(entry_bawah - sma50) / price < 0.02:
        return "≈ MA50 (support menengah, level yang lebih kritis)"
    elif entry_bawah <= support_10d * 1.01:
        return "≈ support 10 hari (batas bawah range terbaru)"
    else:
        return "zona akumulasi (antara support dan harga sekarang)"


def compute_intraday_targets(ticker: str, scoring: dict, hist_daily=None) -> dict:
    """
    Target harga INTRADAY yang lebih presisi untuk /check — TERPISAH dari
    target swing di scoring['targets'] yang dipakai /myportfolio dan winrate.

    v3.14.1 — tiga upgrade kualitas teknikal atas masukan review:
    - ATR-based SL: ganti fixed 5% dengan swing_low_5d - 0.5×ATR(14). Otomatis
      lebih lebar untuk saham volatil (ERAA ATR ~8%), lebih ketat untuk saham
      tenang — bukan lagi satu angka yang dipaksakan ke semua karakter saham.
    - MA10 sebagai referensi entry bawah: lebih responsif dari MA20/EMA21 untuk
      horizon 1-5 hari, level di mana institusi sering akumulasi di uptrend pendek.
    - Bollinger Bands (20,2): upper BB sebagai kandidat TP1 dinamis (berbasis
      volatilitas aktual, bukan hanya high_5d yang statis); lower BB sebagai
      konfirmasi entry bawah (dekat lower BB = relatif oversold dalam konteks
      volatilitas saham itu sendiri, bukan dibandingkan angka luar).
    """
    price = scoring.get("price")
    if not price or hist_daily is None or hist_daily.empty or len(hist_daily) < 14:
        return {}

    # === KALKULASI INDIKATOR DARI DATA HARIAN ===
    closes = hist_daily["Close"]
    highs  = hist_daily["High"]
    lows   = hist_daily["Low"]

    # MA10 dan MA/SMA20, SMA50
    ma10  = closes.rolling(10).mean().iloc[-1]  if len(hist_daily) >= 10 else None
    sma20 = closes.rolling(20).mean().iloc[-1]  if len(hist_daily) >= 20 else None
    sma50 = closes.rolling(50).mean().iloc[-1]  if len(hist_daily) >= 50 else None

    # Bollinger Bands (SMA20 ± 2σ)
    bb_mid   = None; bb_upper = None; bb_lower = None
    if len(hist_daily) >= 20:
        _sma20   = closes.rolling(20).mean()
        _std20   = closes.rolling(20).std()
        bb_mid   = _sma20.iloc[-1]
        bb_upper = (_sma20 + 2 * _std20).iloc[-1]
        bb_lower = (_sma20 - 2 * _std20).iloc[-1]

    # ATR Wilder 14 hari
    _prev_close = closes.shift(1)
    _tr = pd.concat([
        highs - lows,
        (highs - _prev_close).abs(),
        (lows  - _prev_close).abs(),
    ], axis=1).max(axis=1)
    atr14 = _tr.ewm(alpha=1/14, adjust=False).mean().iloc[-1]

    high_5d     = highs.tail(5).max()
    high_20d    = highs.tail(20).max() if len(hist_daily) >= 20 else None
    low_2d      = float(lows.iloc[-2]) if len(hist_daily) >= 2 else None
    low_3d      = float(lows.iloc[-3]) if len(hist_daily) >= 3 else None
    swing_low_5d = lows.tail(5).min()
    support_10d  = lows.tail(10).min()
    day_range_pct = scoring.get("day_range_pct_10d", 10.0) or 10.0

    # === VWAP + cluster low dari 5-menit Yahoo Finance ===
    vwap = None; cluster_low = None; bars_available = 0
    try:
        stock = get_yf_ticker(f"{ticker}.JK")
        intra = yf_fetch_with_retry(lambda: stock.history(period="1d", interval="5m", timeout=15))
        bars_available = len(intra) if not intra.empty else 0
        if bars_available >= 6:
            tp_series = (intra["High"] + intra["Low"] + intra["Close"]) / 3
            vwap = (tp_series * intra["Volume"]).sum() / intra["Volume"].sum()
            intra_lows = intra["Low"].values
            best_count, best_low = 0, None
            for ref in intra_lows:
                radius = ref * 0.005
                count = sum(1 for l in intra_lows if abs(l - ref) <= radius)
                if count > best_count:
                    best_count, best_low = count, ref
            cluster_low = best_low
    except Exception as e:
        print(f"⚠️ Gagal fetch intraday untuk targets {ticker}: {e}")

    intraday_ok = bars_available >= 12 and cluster_low is not None and vwap is not None

    step = (1 if price < 200 else 5 if price < 500 else 10 if price < 2000 else 25)

    # === TP1: max(+5%, high_5d, upper_BB) ===
    # Upper BB menambahkan dimensi volatilitas aktual yang tidak dimiliki high_5d
    tp1_candidates = [price * 1.05, high_5d]
    tp1_src = "+5% / high 5hr"
    if bb_upper and bb_upper > price:
        tp1_candidates.append(bb_upper)
        if bb_upper > max(price * 1.05, high_5d):
            tp1_src = f"BB upper ({smart_round_price(bb_upper)})"
    tp1 = smart_round_price(max(tp1_candidates))

    # === TP2 kondisional ===
    tp2_raw = None; tp2_basis = None
    if sma20 and price < sma20:
        tp2_raw   = max(sma20 * 0.97, tp1 * 1.03)
        tp2_basis = f"SMA20 ({smart_round_price(sma20)}) ×0.97"
    elif sma50 and price < sma50:
        tp2_raw   = max(sma50 * 0.97, tp1 * 1.03)
        tp2_basis = f"SMA50 ({smart_round_price(sma50)}) ×0.97"
    elif high_20d:
        tp2_raw   = max(high_20d, tp1 * 1.05)
        tp2_basis = f"high 20 hari ({smart_round_price(high_20d)})"
    tp2 = smart_round_price(tp2_raw) if tp2_raw and (tp2_raw / price - 1) >= 0.05 else None

    # === Entry atas: VWAP + 30% jarak ke TP1, cap +2% ===
    if intraday_ok:
        dist = tp1 - vwap
        entry_atas = smart_round_price(min(vwap + dist * 0.30, price * 1.02))
        ea_src = f"VWAP {smart_round_price(vwap)} + 30% jarak ke TP1"
    else:
        entry_atas = smart_round_price(price * 1.01)
        ea_src = "harga +1% (data intraday belum cukup)"

    # === Entry bawah: MA10 + cluster low + lower BB sebagai konfirmasi ===
    eb_candidates = []
    eb_notes      = []
    if intraday_ok:
        floor_psiko  = cluster_low * 0.98
        eb_candidates.append(max(min(cluster_low, low_2d or cluster_low, low_3d or cluster_low), floor_psiko))
        eb_notes.append(f"cluster low {smart_round_price(cluster_low)}")
    elif low_2d and low_3d:
        eb_candidates.append(max(min(low_2d, low_3d), support_10d * 0.96))
        eb_notes.append("low 2-3 hari")

    if ma10 and ma10 < price:
        eb_candidates.append(ma10)
        eb_notes.append(f"MA10 ({smart_round_price(ma10)})")

    if bb_lower and bb_lower > support_10d:
        eb_candidates.append(bb_lower)
        eb_notes.append(f"BB lower ({smart_round_price(bb_lower)})")

    if not eb_candidates:
        eb_candidates.append(support_10d * 0.96)
        eb_notes.append("support 10 hari (fallback)")

    # Ambil yang PALING TINGGI di antara kandidat (entry paling konservatif/dekat harga)
    # dan terapkan floor minimum range
    eb_raw      = max(eb_candidates)
    entry_bawah = smart_round_floor(eb_raw)
    eb_src      = " + ".join(eb_notes)

    min_range_pct = min(day_range_pct / 4, 5.0) / 100
    min_eb = smart_round_floor(entry_atas * (1 - min_range_pct))
    if entry_bawah > min_eb:
        entry_bawah = min_eb

    while entry_bawah >= entry_atas:
        entry_bawah -= step

    # === SL: swing_low_5d - 0.5×ATR (ganti fixed 5%) ===
    # Swap dari fixed % ke ATR-based: secara otomatis lebih lebar untuk saham
    # volatil dan lebih ketat untuk saham tenang, bukan satu angka untuk semua.
    sl_atr   = swing_low_5d - 0.5 * atr14
    # Floor lebih longgar (8%) supaya ATR-based SL bisa menang untuk saham volatil
    # seperti KICI (ATR bisa >5% dari harga). Fixed 5% sebelumnya terlalu ketat
    # dan kadang membuat SL hanya 1 poin di bawah entry_bawah.
    sl_floor = entry_atas * 0.92
    sl = smart_round_price(max(sl_atr, sl_floor))
    # Pastikan SL di bawah entry_bawah
    while sl >= entry_bawah:
        sl -= step

    # === RR ===
    risk   = entry_atas - sl
    rr_tp1 = round((tp1 - entry_atas) / risk, 2) if risk > 0 else None
    rr_tp2 = round((tp2 - entry_atas) / risk, 2) if tp2 and risk > 0 else None

    ctx       = get_entry_bawah_context(entry_bawah, sma20, sma50, support_10d, price)
    range_pct = round((entry_atas - entry_bawah) / price * 100, 1)

    # Tambahkan info BB ke context kalau relevan
    if bb_lower and abs(entry_bawah - bb_lower) / price < 0.015:
        ctx += " + dekat BB lower (relatif oversold)"

    return {
        "entry_bawah":  entry_bawah,
        "entry_atas":   entry_atas,
        "entry_atas_above_price": entry_atas > price,
        "tp1":          tp1,
        "tp1_src":      tp1_src,
        "tp2":          tp2,
        "tp2_basis":    tp2_basis,
        "sl":           sl,
        "sl_atr":       smart_round_price(sl_atr),
        "atr14":        round(atr14, 0),
        "rr_tp1":       rr_tp1,
        "rr_tp2":       rr_tp2,
        "range_pct":    range_pct,
        "entry_bawah_context": ctx,
        "intraday_bars": bars_available,
        "vwap":         smart_round_price(vwap) if vwap else None,
        "cluster_low":  smart_round_price(cluster_low) if cluster_low else None,
        "ma10":         smart_round_price(ma10) if ma10 else None,
        "bb_upper":     smart_round_price(bb_upper) if bb_upper else None,
        "bb_lower":     smart_round_price(bb_lower) if bb_lower else None,
        "eb_src":       eb_src,
        "ea_src":       ea_src,
        "data_note":    None if intraday_ok else (
            f"data intraday {bars_available} bar (<12, belum ≥10:00 WIB)"
            if bars_available > 0 else "di luar jam bursa — level dari data EOD"
        ),
    }

    # Ambil data teknikal dari scoring (sudah dihitung di compute_factor_scoring)
    sma20 = None
    sma50 = None
    if hist_daily is not None and not hist_daily.empty:
        if len(hist_daily) >= 20:
            sma20 = hist_daily["Close"].tail(20).mean()
        if len(hist_daily) >= 50:
            sma50 = hist_daily["Close"].tail(50).mean()

    high_5d  = hist_daily["High"].tail(5).max()  if hist_daily is not None and len(hist_daily) >= 5  else None
    high_20d = hist_daily["High"].tail(20).max() if hist_daily is not None and len(hist_daily) >= 20 else None
    low_2d   = float(hist_daily["Low"].iloc[-2]) if hist_daily is not None and len(hist_daily) >= 2  else None
    low_3d   = float(hist_daily["Low"].iloc[-3]) if hist_daily is not None and len(hist_daily) >= 3  else None
    support_10d = hist_daily["Low"].tail(10).min() if hist_daily is not None and len(hist_daily) >= 10 else None
    day_range_pct = scoring.get("day_range_pct_10d", 10.0) or 10.0

    if not high_5d or not support_10d:
        return {}

    # --- VWAP + cluster low dari 5-menit Yahoo Finance ---
    vwap = None
    cluster_low = None
    bars_available = 0
    try:
        stock = get_yf_ticker(f"{ticker}.JK")
        intra = yf_fetch_with_retry(lambda: stock.history(period="1d", interval="5m", timeout=15))
        bars_available = len(intra) if not intra.empty else 0
        if bars_available >= 6:
            tp_series = (intra["High"] + intra["Low"] + intra["Close"]) / 3
            vwap = (tp_series * intra["Volume"]).sum() / intra["Volume"].sum()
            lows = intra["Low"].values
            best_count, best_low = 0, None
            for ref in lows:
                radius = ref * 0.005
                count = sum(1 for l in lows if abs(l - ref) <= radius)
                if count > best_count:
                    best_count, best_low = count, ref
            cluster_low = best_low
    except Exception as e:
        print(f"⚠️ Gagal fetch intraday untuk targets {ticker}: {e}")

    intraday_ok = bars_available >= 12 and cluster_low is not None and vwap is not None

    # --- TP1 ---
    tp1 = smart_round_price(max(price * 1.05, high_5d))

    # --- TP2 kondisional ---
    tp2_raw = None
    tp2_basis = None
    if sma20 and price < sma20:
        tp2_raw = max(sma20 * 0.97, tp1 * 1.03)
        tp2_basis = (f"TP1×1.03" if tp1 * 1.03 > sma20 * 0.97
                     else f"SMA20 ({smart_round_price(sma20)}) ×0.97")
    elif sma50 and price < sma50:
        tp2_raw = max(sma50 * 0.97, tp1 * 1.03)
        tp2_basis = (f"TP1×1.03" if tp1 * 1.03 > sma50 * 0.97
                     else f"SMA50 ({smart_round_price(sma50)}) ×0.97")
    elif high_20d:
        tp2_raw = max(high_20d, tp1 * 1.05)
        tp2_basis = (f"TP1×1.05" if tp1 * 1.05 > high_20d
                     else f"high 20 hari ({smart_round_price(high_20d)})")
    tp2 = smart_round_price(tp2_raw) if tp2_raw and (tp2_raw / price - 1) >= 0.05 else None

    # --- Entry atas ---
    if intraday_ok:
        dist = tp1 - vwap
        entry_atas = smart_round_price(min(vwap + dist * 0.30, price * 1.02))
        ea_src = f"VWAP {smart_round_price(vwap)} + 30% jarak ke TP1"
    else:
        entry_atas = smart_round_price(price * 1.01)
        ea_src = "harga +1% (data intraday belum cukup)"

    # --- Entry bawah: floor + min range ---
    if intraday_ok and low_2d and low_3d:
        floor_psiko = cluster_low * 0.98
        candidate   = min(cluster_low, low_2d, low_3d)
        eb_raw      = max(candidate, floor_psiko)
        eb_src      = f"cluster low {smart_round_price(cluster_low)} + low 2-3 hari"
    elif low_2d and low_3d and support_10d:
        candidate = min(low_2d, low_3d)
        eb_raw    = max(candidate, support_10d * 0.96)
        eb_src    = "low 2-3 hari (data intraday belum cukup)"
    else:
        eb_raw = support_10d * 0.96
        eb_src = "support 10 hari (fallback)"
    entry_bawah = smart_round_floor(eb_raw)

    # Min range: 1/4 volatilitas, maks 5%
    min_range_pct = min(day_range_pct / 4, 5.0) / 100
    min_eb = smart_round_floor(entry_atas * (1 - min_range_pct))
    if entry_bawah > min_eb:
        entry_bawah = min_eb

    # Guard: entry_bawah harus < entry_atas
    step = (1 if price < 200 else 5 if price < 500 else 10 if price < 2000 else 25)
    while entry_bawah >= entry_atas:
        entry_bawah -= step

    # --- SL ---
    sl = smart_round_price(max(support_10d * 0.96, entry_atas * 0.95))
    while sl >= entry_bawah:
        sl -= step

    # --- RR ---
    risk = entry_atas - sl
    rr_tp1 = round((tp1 - entry_atas) / risk, 2) if risk > 0 else None
    rr_tp2 = round((tp2 - entry_atas) / risk, 2) if tp2 and risk > 0 else None

    ctx = get_entry_bawah_context(entry_bawah, sma20, sma50, support_10d, price)
    range_pct = round((entry_atas - entry_bawah) / price * 100, 1)

    return {
        "entry_bawah":  entry_bawah,
        "entry_atas":   entry_atas,
        "entry_atas_above_price": entry_atas > price,
        "tp1":          tp1,
        "tp2":          tp2,
        "tp2_basis":    tp2_basis,
        "sl":           sl,
        "rr_tp1":       rr_tp1,
        "rr_tp2":       rr_tp2,
        "range_pct":    range_pct,
        "entry_bawah_context": ctx,
        "intraday_bars": bars_available,
        "vwap":         smart_round_price(vwap) if vwap else None,
        "cluster_low":  smart_round_price(cluster_low) if cluster_low else None,
        "eb_src":       eb_src,
        "ea_src":       ea_src,
        "data_note":    None if intraday_ok else (
            f"data intraday {bars_available} bar (<12, belum ≥10:00 WIB) — gunakan sebagai orientasi awal"
            if bars_available > 0 else "di luar jam bursa — level dari data EOD"
        ),
    }


# ==========================================
# 🚦 BATAS ARA/ARB IDX (MBSS v2, user request — screening BSJP)
# Diverifikasi lewat riset publik (SK Direksi BEI No. Kep-00055/BEI/03-2023,
# berlaku efektif 4 September 2023, masih current per pengecekan terakhir).
# Bertingkat berdasarkan HARGA PENUTUPAN KEMARIN (bukan harga sekarang):
#   Rp50   - Rp200   -> 35%
#   >Rp200 - Rp5.000 -> 25%
#   >Rp5.000         -> 20%
# CATATAN: saham IPO hari pertama listing punya aturan beda (ditemukan
# sumber yang saling bertentangan — ada yang bilang 1x lipat, ada yang
# bilang 2x lipat dari batas normal) — TIDAK diimplementasikan di sini,
# hindari pakai fungsi ini untuk saham yang BARU SAJA IPO.
# ==========================================
def compute_ara_ceiling(prev_close: float) -> float | None:
    """Harga batas ARA hari ini, dihitung dari harga penutupan KEMARIN."""
    if prev_close is None or prev_close <= 0:
        return None
    if 50 <= prev_close <= 200:
        pct = 0.35
    elif 200 < prev_close <= 5000:
        pct = 0.25
    elif prev_close > 5000:
        pct = 0.20
    else:
        return None  # di bawah Rp50 (harusnya sudah tersaring MIN_STOCK_PRICE)
    return prev_close * (1 + pct)


def compute_ara_distance_pct(current_price: float, prev_close: float) -> float | None:
    """
    Persentase jarak dari harga SEKARANG ke batas ARA hari ini — 0% berarti
    sudah tepat di ARA, makin besar angkanya makin jauh dari limit.
    """
    ceiling = compute_ara_ceiling(prev_close)
    if ceiling is None or current_price is None:
        return None
    return round((ceiling - current_price) / ceiling * 100, 2)


def get_current_idx_session():
    """
    Menentukan sesi bursa IDX saat ini — DIVERIFIKASI via pencarian web (bukan
    dari ingatan yang mungkin usang), bisa berubah sewaktu-waktu (misal Ramadan)
    — cek idx.co.id/id/produk/mekanisme-dan-jam-perdagangan kalau terasa meleset.
    Senin-Kamis: Sesi 1 09:00-12:00, Sesi 2 13:30-15:49.
    Jumat: Sesi 1 09:00-11:30, Sesi 2 14:00-15:49 (jeda lebih panjang, salat Jumat).

    BUGFIX (ditemukan lewat pengalaman user — /bsjp ditolak justru di jam yang
    menurut mereka paling efektif): periode Pra-Penutupan (15:50-16:00 WIB,
    SAMA untuk semua hari termasuk Jumat) sebelumnya TIDAK dikenali sama
    sekali (jatuh ke None) — dikonfirmasi via riset multi-sumber (termasuk
    idx.co.id sendiri) sebagai "jendela paling ramai kedua setelah opening",
    pas pembentukan harga penutupan resmi lewat call auction. Sekarang
    dikenali sebagai sesi terpisah "pra_penutupan".

    Return "sesi_1", "sesi_2", "pra_penutupan", atau None (di luar jam bursa/weekend).
    """
    now = datetime.datetime.now(WIB)
    if now.weekday() >= 5:  # Sabtu/Minggu
        return None
    t = now.time()
    is_friday = now.weekday() == 4
    sesi1_end = datetime.time(11, 30) if is_friday else datetime.time(12, 0)
    sesi2_start = datetime.time(14, 0) if is_friday else datetime.time(13, 30)
    if datetime.time(9, 0) <= t < sesi1_end:
        return "sesi_1"
    if sesi2_start <= t < datetime.time(15, 49):
        return "sesi_2"
    if datetime.time(15, 49) <= t < datetime.time(16, 0):
        return "pra_penutupan"
    return None


def fetch_intraday_momentum(ticker):
    """
    Hitung momentum intraday dari perbandingan harga 30 menit terakhir vs
    30 menit pertama sesi — lebih bermakna dari sekedar EMA bias.
    """
    bars = yfinance_get_intraday_5m(ticker, period="1d")
    if bars is None or bars.empty or len(bars) < 8:
        return {"available": False, "reason": "data intraday belum tersedia atau sesi belum cukup"}

    session = get_current_idx_session()
    if session is None:
        return {"available": False, "reason": "di luar jam bursa"}

    closes = bars["Close"]
    # Bandingkan rata-rata 6 bar terakhir (30 menit) vs 6 bar pertama sesi
    early_avg  = float(closes.iloc[:6].mean())
    recent_avg = float(closes.iloc[-6:].mean())
    change_pct = ((recent_avg - early_avg) / early_avg) * 100 if early_avg > 0 else 0.0

    if change_pct > 0.5:
        reading = "MENGUAT"
    elif change_pct < -0.5:
        reading = "MELEMAH"
    else:
        reading = "NETRAL"

    return {
        "available":     True,
        "session":       session,
        "reading":       reading,
        "change_pct":    round(change_pct, 2),
        "bars_analyzed": len(bars),
    }


def fetch_intraday_market_context(ticker: str) -> dict:
    """
    Wrapper tunggal untuk semua data intraday di /check:
    - momentum: perubahan harga dalam sesi (30 menit terakhir vs awal sesi)
    - breakout: probabilitas breakout intraday dari yfinance_breakout_context()
    - active_breakout: skor live yang lebih actionable untuk entry sesi ini
    - price/high/low: update harga live dari bar 5-menit terbaru

    Dipanggil oleh check_stock() — hasil digabungkan ke result sebelum
    dikirim ke Gemini dan ditampilkan ke user.
    """
    bars = yfinance_get_intraday_5m(ticker, period="1d")

    if bars is None or bars.empty:
        return {
            "available": False,
            "reason":    "data intraday belum tersedia",
            "momentum":  {"available": False, "reason": "data intraday belum tersedia"},
            "breakout":  {"available": False, "reason": "data intraday belum tersedia"},
            "active_breakout": {"available": False, "reason": "data intraday belum tersedia"},
        }

    session = get_current_idx_session()

    # Harga live dari bar terbaru
    current_price = float(bars["Close"].iloc[-1])
    high_today    = float(bars["High"].max())
    low_today     = float(bars["Low"].min())

    # VWAP fallback: selalu hitung dari intraday bars agar /check tetap menampilkan
    # Harga vs VWAP walau active_breakout belum tersedia atau data belum cukup.
    vwap_snapshot = {"available": False}
    try:
        bars_norm = _normalize_intraday_index(bars)
        _, start_dt, _ = _get_session_window()
        session_bars = bars_norm[bars_norm.index >= start_dt].dropna() if start_dt is not None else bars_norm.dropna()
        if session_bars is None or session_bars.empty:
            session_bars = bars_norm.dropna()
        if session_bars is not None and not session_bars.empty:
            highs = session_bars["High"].astype(float)
            lows = session_bars["Low"].astype(float)
            closes = session_bars["Close"].astype(float)
            volumes = session_bars["Volume"].fillna(0).astype(float)
            total_vol = float(volumes.sum())
            typical_price = (highs + lows + closes) / 3
            vwap_val = float((typical_price * volumes).sum() / total_vol) if total_vol > 0 else float(closes.mean())
            vwap_dist = ((current_price - vwap_val) / max(vwap_val, 1e-9)) * 100
            volume_pace_ratio = None
            try:
                hist = get_ohlcv_smart(ticker, limit=25)
                avg_vol = float(hist["Volume"].tail(20).mean()) if hist is not None and not hist.empty else 0
                minutes_elapsed = max(5.0, (session_bars.index[-1] - session_bars.index[0]).total_seconds() / 60)
                expected = avg_vol * (minutes_elapsed / 330.0)
                if expected > 0:
                    volume_pace_ratio = float(total_vol / expected)
            except Exception:
                volume_pace_ratio = None
            vwap_snapshot = {
                "available": True,
                "vwap": smart_round_price(vwap_val),
                "vwap_raw": round(vwap_val, 2),
                "vwap_distance_pct": round(vwap_dist, 2),
                "volume_pace_ratio": round(volume_pace_ratio, 2) if volume_pace_ratio is not None else None,
                "session_bars": int(len(session_bars)),
            }
    except Exception as e:
        vwap_snapshot = {"available": False, "reason": str(e)[:120]}

    momentum = fetch_intraday_momentum(ticker)
    breakout  = yfinance_breakout_context(ticker)
    active_breakout = compute_active_breakout_score(ticker)
    vwap_movement = compute_vwap_movement_context(ticker)

    return {
        "available": True,
        "session":   session,
        "price":     current_price,
        "high":      high_today,
        "low":       low_today,
        "bars":      len(bars),
        "vwap_snapshot": vwap_snapshot,
        "momentum":  momentum,
        "breakout":  breakout,
        "active_breakout": active_breakout,
        "vwap_movement": vwap_movement,
    }




def _compute_intraday_vwap_window(bars: pd.DataFrame, min_bars: int) -> dict:
    """Hitung VWAP movement untuk satu window intraday pendek."""
    if bars is None or bars.empty or len(bars) < min_bars:
        return {"available": False, "reason": f"data belum cukup (<{min_bars} bar)"}

    try:
        bars = bars.dropna().copy()
        if bars.empty or len(bars) < min_bars:
            return {"available": False, "reason": f"data belum cukup (<{min_bars} bar)"}

        closes = bars["Close"].astype(float)
        highs = bars["High"].astype(float)
        lows = bars["Low"].astype(float)
        volumes = bars["Volume"].fillna(0).astype(float)

        open_price = float(bars["Open"].iloc[0])
        current_price = float(closes.iloc[-1])
        total_vol = float(volumes.sum())
        if total_vol <= 0:
            return {"available": False, "reason": "volume nol"}

        typical_price = (highs + lows + closes) / 3
        vwap_val = float((typical_price * volumes).sum() / total_vol)
        vwap_distance_pct = ((current_price - vwap_val) / max(vwap_val, 1e-9)) * 100
        change_from_open_pct = ((current_price - open_price) / max(open_price, 1e-9)) * 100

        if vwap_distance_pct >= 0.30:
            signal = "naik"
            bias = "bullish"
        elif vwap_distance_pct <= -0.30:
            if change_from_open_pct <= -0.30:
                signal = "fall down"
            else:
                signal = "pullback"
            bias = "bearish"
        else:
            signal = "sideways"
            bias = "netral"

        return {
            "available": True,
            "price": round(current_price, 2),
            "open_price": round(open_price, 2),
            "vwap": round(vwap_val, 2),
            "vwap_distance_pct": round(vwap_distance_pct, 2),
            "change_from_open_pct": round(change_from_open_pct, 2),
            "signal": signal,
            "bias": bias,
            "bars": int(len(bars)),
        }
    except Exception as e:
        return {"available": False, "reason": str(e)[:120]}


def compute_vwap_movement_context(ticker: str) -> dict:
    """
    VWAP movement 15m / 30m / 60m untuk /check saat market open.
    Menggunakan 5m bars dari Yahoo lalu melihat window terakhir 3/6/12 bar.
    """
    bars = yfinance_get_intraday_5m(ticker, period="1d")
    if bars is None or bars.empty:
        return {
            "available": False,
            "reason": "data intraday belum tersedia",
            "15m": {"available": False, "reason": "data intraday belum tersedia"},
            "30m": {"available": False, "reason": "data intraday belum tersedia"},
            "60m": {"available": False, "reason": "data intraday belum tersedia"},
            "overall_signal": "N/A",
        }

    session = get_current_idx_session()
    if session is None:
        return {
            "available": False,
            "reason": "di luar jam bursa",
            "15m": {"available": False, "reason": "di luar jam bursa"},
            "30m": {"available": False, "reason": "di luar jam bursa"},
            "60m": {"available": False, "reason": "di luar jam bursa"},
            "overall_signal": "N/A",
        }

    bars = _normalize_intraday_index(bars).dropna()
    _, start_dt, _ = _get_session_window()
    session_bars = bars[bars.index >= start_dt].dropna() if start_dt is not None else bars
    if session_bars is None or session_bars.empty:
        return {
            "available": False,
            "reason": "data intraday belum tersedia",
            "15m": {"available": False, "reason": "data intraday belum tersedia"},
            "30m": {"available": False, "reason": "data intraday belum tersedia"},
            "60m": {"available": False, "reason": "data intraday belum tersedia"},
            "overall_signal": "N/A",
        }

    window_map = {"15m": 3, "30m": 6, "60m": 12}
    out = {"available": False, "session": session, "overall_signal": "N/A"}
    score_map = []

    for label, min_bars in window_map.items():
        window_bars = session_bars.tail(min_bars)
        res = _compute_intraday_vwap_window(window_bars, min_bars)
        out[label] = res
        if res.get("available"):
            out["available"] = True
            score_map.append(1 if res.get("bias") == "bullish" else -1 if res.get("bias") == "bearish" else 0)

    if score_map:
        total = sum(score_map)
        if total >= 2:
            out["overall_signal"] = "bullish"
        elif total <= -2:
            out["overall_signal"] = "bearish"
        else:
            out["overall_signal"] = "mixed"

    return out


def format_vwap_movement_block(vwap_move: dict) -> str:
    """Format ringkas VWAP movement untuk ditampilkan di /check."""
    lines = ["📊 VWAP MOVEMENT"]

    if not vwap_move or not vwap_move.get("available"):
        lines.append("15m : N/A")
        lines.append("30m : N/A")
        lines.append("60m : N/A")
        lines.append("Bias: N/A")
        return "\n".join(lines)

    for label in ("15m", "30m", "60m"):
        d = vwap_move.get(label) or {}
        if not d.get("available"):
            lines.append(f"{label} : N/A")
            continue

        price_txt = f"{int(round(float(d.get('price', 0)))):,}".replace(",", ".")
        vwap_txt = f"{int(round(float(d.get('vwap', 0)))):,}".replace(",", ".")
        dist = d.get("vwap_distance_pct")
        dist_txt = f"{dist:+.2f}%" if isinstance(dist, (int, float)) else "N/A"
        signal = d.get("signal", "N/A")
        if signal == "naik":
            icon = "↑"
        elif signal in ("fall down", "pullback"):
            icon = "↓"
        else:
            icon = "→"
        lines.append(f"{label} : Harga {price_txt} | VWAP {vwap_txt} ({dist_txt}) | {signal} {icon}")

    lines.append(f"Bias: {vwap_move.get('overall_signal', 'N/A')}")
    return "\n".join(lines)


def _normalize_intraday_index(df: pd.DataFrame) -> pd.DataFrame:
    """Pastikan index intraday comparable dengan WIB untuk filter sesi."""
    if df is None or df.empty:
        return df
    try:
        if getattr(df.index, "tz", None) is None:
            df = df.copy()
            df.index = df.index.tz_localize(WIB)
        else:
            df = df.copy()
            df.index = df.index.tz_convert(WIB)
    except Exception:
        pass
    return df


def _get_session_window(now: datetime.datetime = None):
    now = now or datetime.datetime.now(WIB)
    if now.weekday() >= 5:
        return None, None, None
    is_friday = now.weekday() == 4
    session = get_current_idx_session()
    if session == "sesi_1":
        start_t = datetime.time(9, 0)
        end_t = datetime.time(11, 30) if is_friday else datetime.time(12, 0)
    elif session == "sesi_2":
        start_t = datetime.time(14, 0) if is_friday else datetime.time(13, 30)
        end_t = datetime.time(15, 49)
    else:
        return session, None, None
    return session, datetime.datetime.combine(now.date(), start_t, tzinfo=WIB), datetime.datetime.combine(now.date(), end_t, tzinfo=WIB)


def get_intraday_session_bars(ticker: str, interval: str = "5m", period: str = "1d") -> pd.DataFrame:
    """Ambil bar intraday sesi aktif. Default 5m; 1m dipakai hanya untuk shortlist/check."""
    stock = get_yf_ticker(f"{ticker}.JK")
    bars = yf_fetch_with_retry(lambda: stock.history(period=period, interval=interval, timeout=15))
    if bars is None or bars.empty:
        return pd.DataFrame()
    bars = _normalize_intraday_index(bars)
    session, start_dt, _ = _get_session_window()
    if start_dt is None:
        return bars
    try:
        return bars[bars.index >= start_dt].dropna()
    except Exception:
        return bars.dropna()


def compute_active_breakout_score(ticker: str, scoring: dict = None, prefer_1m: bool = False) -> dict:
    """
    Skor live untuk mencari saham yang ready breakout sesi ini / sesi berikutnya.
    Menggunakan OHLCV intraday Yahoo Finance (5m default; 1m opsional untuk kandidat akhir).
    Output deterministik supaya /screendaytrade, /testopening, /check, dan /myportfolio
    tidak bergantung pada LLM untuk menentukan entry/exit signal.
    """
    session = get_current_idx_session()
    if session is None:
        return {"available": False, "reason": "di luar jam bursa", "score": 0, "label": "OFF"}

    interval = "1m" if prefer_1m else "5m"
    bars = get_intraday_session_bars(ticker, interval=interval, period="1d")
    if (bars is None or bars.empty or len(bars) < (12 if interval == "1m" else 6)) and prefer_1m:
        interval = "5m"
        bars = get_intraday_session_bars(ticker, interval="5m", period="1d")
    if bars is None or bars.empty or len(bars) < 6:
        return {"available": False, "reason": "data intraday belum cukup", "score": 0, "label": "NA"}

    try:
        closes = bars["Close"].astype(float)
        highs = bars["High"].astype(float)
        lows = bars["Low"].astype(float)
        volumes = bars["Volume"].fillna(0).astype(float)
        current = float(closes.iloc[-1])
        open_price = float(bars["Open"].iloc[0])
        session_high = float(highs.max())
        session_low = float(lows.min())

        # Opening range: 30 menit pertama untuk 5m, 15 menit pertama untuk 1m.
        or_bars_count = min(len(bars), 15 if interval == "1m" else 6)
        opening_high = float(highs.iloc[:or_bars_count].max())
        opening_low = float(lows.iloc[:or_bars_count].min())

        typical_price = (highs + lows + closes) / 3
        total_vol = float(volumes.sum())
        vwap = float((typical_price * volumes).sum() / total_vol) if total_vol > 0 else float(closes.mean())
        vwap_distance_pct = ((current - vwap) / max(vwap, 1e-9)) * 100

        # Resistance trigger: pakai max opening range high dan high historis sesi sebelum bar terakhir.
        prior_high = float(highs.iloc[:-1].max()) if len(highs) > 1 else opening_high
        trigger = max(opening_high, prior_high)
        distance_to_trigger_pct = ((trigger - current) / max(trigger, 1e-9)) * 100

        # ATR intraday sederhana dari 5-10 bar terakhir.
        atr_session = float((highs - lows).tail(min(10, len(bars))).mean())
        distance_atr = (trigger - current) / max(atr_session, 1e-9)

        ema5 = float(closes.ewm(span=5, adjust=False).mean().iloc[-1])
        ema20 = float(closes.ewm(span=min(20, max(6, len(closes))), adjust=False).mean().iloc[-1])
        higher_low = len(lows) >= 4 and float(lows.iloc[-1]) >= float(lows.tail(min(5, len(lows))).min())
        pos_in_range = (current - session_low) / max(session_high - session_low, 1e-9)

        # Relative volume: bar terakhir vs rata2 bar sebelumnya + pace vs rata2 daily kalau ada.
        prev_vol = volumes.iloc[:-1].tail(min(10, max(1, len(volumes)-1)))
        last_bar_rvol = float(volumes.iloc[-1] / max(prev_vol.mean(), 1e-9)) if len(prev_vol) else 1.0
        volume_pace_ratio = None
        if scoring and scoring.get("avg_volume_20d"):
            minutes_elapsed = max(5.0, (bars.index[-1] - bars.index[0]).total_seconds() / 60)
            expected = scoring["avg_volume_20d"] * (minutes_elapsed / 330.0)
            if expected > 0:
                volume_pace_ratio = float(total_vol / expected)
        else:
            try:
                hist = get_ohlcv_smart(ticker, limit=25)
                avg_vol = float(hist["Volume"].tail(20).mean()) if hist is not None and not hist.empty else 0
                minutes_elapsed = max(5.0, (bars.index[-1] - bars.index[0]).total_seconds() / 60)
                expected = avg_vol * (minutes_elapsed / 330.0)
                if expected > 0:
                    volume_pace_ratio = float(total_vol / expected)
            except Exception:
                volume_pace_ratio = None

        score = 0
        notes = []

        # 25: trigger proximity / breakout state
        if distance_atr < -1.5:
            score += 13; notes.append("sudah extended di atas trigger")
        elif distance_atr < -0.2:
            score += 25; notes.append("breakout aktif")
        elif distance_atr <= 0.3:
            score += 24; notes.append("tepat di area trigger")
        elif distance_atr <= 0.8:
            score += 18; notes.append("dekat trigger")
        elif distance_atr <= 1.5:
            score += 10; notes.append("mulai mendekat")
        else:
            score += 3; notes.append("masih jauh dari trigger")

        # 20: volume confirmation
        vol_ref = max(last_bar_rvol, volume_pace_ratio or 0)
        if vol_ref >= 2.5:
            score += 20; notes.append("volume sangat kuat")
        elif vol_ref >= 1.5:
            score += 15; notes.append("volume mendukung")
        elif vol_ref >= 1.0:
            score += 10; notes.append("volume cukup")
        elif vol_ref >= 0.6:
            score += 5; notes.append("volume tipis")
        else:
            score += 1; notes.append("volume kering")

        # 15: VWAP structure
        if current > vwap and vwap_distance_pct <= 3.0:
            score += 15; notes.append("di atas VWAP sehat")
        elif current > vwap:
            score += 9; notes.append("di atas VWAP tapi mulai jauh")
        else:
            score += 2; notes.append("di bawah VWAP")

        # 15: intraday trend
        if ema5 > ema20 and higher_low and pos_in_range >= 0.65:
            score += 15; notes.append("trend intraday naik")
        elif ema5 > ema20 and pos_in_range >= 0.5:
            score += 10; notes.append("bias intraday positif")
        elif ema5 > ema20:
            score += 7; notes.append("EMA positif tapi belum dekat high")
        else:
            score += 2; notes.append("EMA intraday lemah")

        # 15: daily setup quality from scoring (if supplied)
        if scoring:
            action_ok = scoring.get("action_id") in ("STRONG_BUY", "BUY_ACCUMULATE")
            macd_ok = scoring.get("macd_state") == "bullish" or scoring.get("macd_cross_direction") == "bullish"
            adx_ok = (scoring.get("adx") or 0) >= 18
            rr = scoring.get("targets", {}).get("risk_reward_at_max")
            rr_ok = rr is None or rr >= 1.0
            daily_points = sum([action_ok, macd_ok, adx_ok, rr_ok])
            score += [2, 5, 9, 12, 15][daily_points]
        else:
            score += 8

        # 10: risk control / chase penalty
        change_from_open_pct = ((current - open_price) / max(open_price, 1e-9)) * 100
        invalidation = max(opening_low, vwap * 0.995) if current > vwap else opening_low
        downside_pct = ((current - invalidation) / max(current, 1e-9)) * 100
        if 0 <= change_from_open_pct <= 3.5 and downside_pct <= 3.0:
            score += 10
        elif change_from_open_pct <= 5.0 and downside_pct <= 4.5:
            score += 6
        else:
            score += 2; notes.append("risiko chase tinggi")

        if (volume_pace_ratio is not None and volume_pace_ratio < 0.5) and distance_atr <= 0.3:
            score = min(score, 58)
            notes.append("fake-out risk: volume pace rendah")
        if current < vwap:
            score = min(score, 60)
        score = int(max(0, min(100, score)))
        label = "READY" if score >= 75 else "WATCH" if score >= 60 else "WAIT"

        return {
            "available": True,
            "ticker": ticker,
            "interval": interval,
            "session": session,
            "score": score,
            "label": label,
            "current_price": round(current, 2),
            "trigger_price": smart_round_price(trigger),
            "invalidation_level": smart_round_floor(invalidation),
            "opening_range_high": smart_round_price(opening_high),
            "opening_range_low": smart_round_floor(opening_low),
            "session_high": smart_round_price(session_high),
            "session_low": smart_round_floor(session_low),
            "vwap": smart_round_price(vwap),
            "vwap_distance_pct": round(vwap_distance_pct, 2),
            "distance_to_trigger_pct": round(distance_to_trigger_pct, 2),
            "distance_to_trigger_atr": round(distance_atr, 2),
            "last_bar_rvol": round(last_bar_rvol, 2),
            "volume_pace_ratio": round(volume_pace_ratio, 2) if volume_pace_ratio is not None else None,
            "change_from_open_pct": round(change_from_open_pct, 2),
            "downside_to_invalidation_pct": round(downside_pct, 2),
            "ema_bias": "bullish" if ema5 > ema20 else "bearish",
            "higher_low": higher_low,
            "notes": ", ".join(notes[:5]),
        }
    except Exception as e:
        return {"available": False, "reason": f"error active breakout: {str(e)[:120]}", "score": 0, "label": "ERR"}


def compute_live_daytrade_rank(scoring: dict) -> float:
    """Composite final rank: 40% EOD activity, 60% live active breakout."""
    base = compute_daytrade_score(scoring)
    active = scoring.get("active_breakout", {})
    if active.get("available"):
        return round(base * 0.40 + (active.get("score", 0) / 10.0) * 0.60, 2)
    return round(base * 0.70, 2)


def _dt_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _score_breakout_chance_v4(scoring: dict) -> dict:
    """Empirical breakout chance from v4 top-gainer study."""
    def v(k, default=0): return _dt_float(scoring.get(k), default)
    intrarange = v("intraday_range_pct")
    macd_hist = v("macd_hist")
    obv_slope = v("obv_slope_5_pct")
    value_traded = v("value_traded")
    range10 = v("day_range_pct_10d")
    price_vs_sma20 = v("price_vs_sma20_pct")
    adx = v("adx")
    cmf = v("cmf")
    vol_ratio = v("vol_ratio")
    dist20 = v("dist_to_20d_high_pct", 99)
    ret1 = v("ret_1d_pct")
    rsi = v("rsi")
    reasons = []
    score = 0

    # Breakout rules from weighted Top 30 gainer study.
    if intrarange >= 5.65: score += 20; reasons.append(f"range harian kuat {intrarange:.1f}%")
    elif intrarange >= 3.45: score += 12; reasons.append(f"range harian aktif {intrarange:.1f}%")

    if macd_hist >= 4.63: score += 18; reasons.append("MACD hist kuat")
    elif macd_hist >= 1.66: score += 10; reasons.append("MACD hist positif")

    if obv_slope >= 26.0: score += 12; reasons.append("OBV slope kuat")
    elif obv_slope >= 1.07: score += 7; reasons.append("OBV naik")

    if value_traded >= 9_800_000_000: score += 12; reasons.append("value traded besar")
    elif value_traded >= 3_550_000_000: score += 7; reasons.append("value traded cukup")

    if 11.83 <= range10 <= 33.51: score += 10; reasons.append("range 10D ideal")
    elif 8 <= range10 < 11.83 or 33.51 < range10 <= 40: score += 4

    if price_vs_sma20 >= 6.17: score += 10; reasons.append("harga jauh di atas MA20")
    elif price_vs_sma20 >= 1.65: score += 6; reasons.append("harga di atas MA20")

    if adx >= 21.88: score += 8; reasons.append("ADX cukup")
    if cmf >= 0.10: score += 8; reasons.append("CMF akumulasi")
    elif cmf >= -0.11: score += 5; reasons.append("CMF tidak distribusi berat")

    # Light boosters, not primary predictors.
    if vol_ratio >= 1.5: score += 5; reasons.append("volume >1.5x")
    elif vol_ratio >= 0.80: score += 2
    if dist20 <= 5: score += 5; reasons.append("dekat high 20D")
    elif dist20 <= 14.47: score += 2

    # Anti-late / trap penalties.
    if ret1 >= 12: score -= 25; reasons.append("penalty: sudah naik ekstrem")
    elif ret1 >= 8: score -= 15; reasons.append("penalty: sudah naik tinggi")
    if rsi >= 82: score -= 10; reasons.append("penalty: RSI overheat")
    elif rsi >= 75: score -= 5; reasons.append("RSI panas")
    if cmf < -0.15: score -= 12; reasons.append("penalty: CMF distribusi")
    if vol_ratio < 0.7: score -= 8; reasons.append("penalty: volume kurang hidup")

    score = int(max(0, min(100, score)))
    if score >= 70: label = "TINGGI"
    elif score >= 55: label = "SEDANG"
    elif score >= 40: label = "RENDAH-MENUNGGU"
    else: label = "RENDAH"
    return {"score": score, "label": label, "reasons": reasons[:6]}


def _score_breakout_drop_risk_v4(scoring: dict) -> dict:
    """Higher score = lower observed post-break drop risk from v4 study."""
    def v(k, default=0): return _dt_float(scoring.get(k), default)
    macd_hist = v("macd_hist")
    adx = v("adx")
    value_traded = v("value_traded")
    dist20 = v("dist_to_20d_high_pct", 99)
    vol_ratio = v("vol_ratio")
    cmf = v("cmf")
    rsi = v("rsi")
    price = v("price")
    high_today = v("intraday_high", price)
    rr_max = _dt_float((scoring.get("targets") or {}).get("risk_reward_at_max"), 0)
    ret1 = v("ret_1d_pct")
    fade_from_high_pct = ((high_today - price) / max(price, 1e-9) * 100) if high_today and price else 0
    reasons = []
    score = 40  # neutral baseline

    if macd_hist >= 3.33: score += 22; reasons.append("MACD kuat, drop risk lebih rendah")
    if value_traded >= 4_950_000_000: score += 15; reasons.append("likuiditas kuat")
    if dist20 <= 13.68: score += 12; reasons.append("masih dalam zona high 20D")
    if vol_ratio >= 0.80: score += 8; reasons.append("volume cukup hidup")
    if cmf >= -0.11: score += 8; reasons.append("CMF tidak distribusi")
    if 20 <= adx <= 28.5: score += 8; reasons.append("ADX belum terlalu matang")

    # Risk penalties.
    if adx > 45: score -= 14; reasons.append("penalty: ADX sangat tinggi/extended")
    elif adx > 40: score -= 8; reasons.append("ADX tinggi, cek extended")
    if rsi > 82: score -= 25; reasons.append("penalty: RSI overheat")
    elif rsi > 75: score -= 10; reasons.append("RSI panas")
    if cmf < -0.15: score -= 18; reasons.append("penalty: distribusi")
    elif cmf < -0.05: score -= 10; reasons.append("CMF mulai negatif")
    if value_traded < 3_000_000_000: score -= 10; reasons.append("penalty: likuiditas tipis")
    if rr_max and rr_max < 0.50: score -= 12; reasons.append("RR entry atas tipis")
    elif rr_max and rr_max < 0.80: score -= 7; reasons.append("RR entry atas kurang ideal")
    if fade_from_high_pct >= 8: score -= 16; reasons.append("penalty: fade jauh dari high")
    elif fade_from_high_pct >= 5: score -= 10; reasons.append("fade dari high")
    if ret1 >= 8: score -= 10; reasons.append("sudah naik tinggi hari ini")

    score = int(max(0, min(100, score)))
    if score >= 80: label = "RENDAH"
    elif score >= 65: label = "SEDANG-RENDAH"
    elif score >= 50: label = "SEDANG"
    else: label = "TINGGI"
    return {"score": score, "label": label, "reasons": reasons[:5]}


def compute_scalping_readiness(scoring: dict) -> dict:
    """V4 readiness: tampilkan 2 dimensi utama, Breakout Chance dan Drop Risk, plus total."""
    breakout = _score_breakout_chance_v4(scoring)
    risk = _score_breakout_drop_risk_v4(scoring)
    ab = scoring.get("active_breakout") or {}
    live_score = _dt_float(ab.get("score"), 0) if ab.get("available") else None

    if live_score is not None:
        total = int(round(breakout["score"] * 0.35 + risk["score"] * 0.20 + live_score * 0.30 + _live_vwap_volume_score(scoring) * 0.15))
        mode = "LIVE"
    else:
        # Existing daytrade score is retained only as a small stabilizer.
        base = compute_daytrade_score(scoring) * 10
        total = int(round(breakout["score"] * 0.60 + risk["score"] * 0.25 + base * 0.15))
        mode = "NEXT OPEN"

    total = int(max(0, min(100, total)))
    if total >= 75: label = "PRIORITAS UTAMA" if mode == "NEXT OPEN" else "SCALP READY"
    elif total >= 65: label = "WATCHLIST UTAMA" if mode == "NEXT OPEN" else "BUY WATCH"
    elif total >= 55: label = "VALIDASI OPENING" if mode == "NEXT OPEN" else "WAIT CONFIRM"
    else: label = "RENDAH" if mode == "NEXT OPEN" else "AVOID SCALP"

    return {
        "score": total,
        "label": label,
        "mode": mode,
        "breakout_score": breakout["score"],
        "breakout_label": breakout["label"],
        "risk_score": risk["score"],
        "risk_label": risk["label"],
        "key": f"Breakout {breakout['score']}/100 | Risk {risk['score']}/100",
        "reasons": "; ".join((breakout["reasons"][:3] + risk["reasons"][:2])[:5]),
        "breakout_reasons": "; ".join(breakout["reasons"][:4]),
        "risk_reasons": "; ".join(risk["reasons"][:4]),
    }


def _live_vwap_volume_score(scoring: dict) -> int:
    ab = scoring.get("active_breakout") or {}
    if not ab.get("available"):
        return 50
    vwap_dist = _dt_float(ab.get("vwap_distance_pct"), -99)
    vol_pace = _dt_float(ab.get("volume_pace_ratio"), 0)
    downside = _dt_float(ab.get("downside_to_invalidation_pct"), 99)
    score = 0
    if 0 <= vwap_dist <= 2.5: score += 40
    elif vwap_dist > 2.5: score += 20
    else: score += 5
    if vol_pace >= 1.5: score += 35
    elif vol_pace >= 1.0: score += 25
    elif vol_pace >= 0.7: score += 10
    if downside <= 3.0: score += 25
    elif downside <= 4.5: score += 10
    return int(max(0, min(100, score)))


def enrich_live_breakout_for_candidates(candidates: list, limit: int = 25, prefer_1m: bool = False) -> list:
    """Ambil live breakout context untuk shortlist saja agar tidak meledakkan rate-limit."""
    enriched = []
    pool = sorted(candidates, key=compute_daytrade_score, reverse=True)[:limit]
    for r in pool:
        try:
            r["active_breakout"] = compute_active_breakout_score(r["ticker"], r, prefer_1m=prefer_1m)
        except Exception as e:
            r["active_breakout"] = {"available": False, "reason": str(e)[:120], "score": 0}
        enriched.append(r)
        time.sleep(0.5)
    return enriched


def opening_breakout_summary_line(d: dict) -> str:
    ab = d.get("active_breakout", {}) or {}
    if not ab.get("available"):
        return "Live breakout: tidak tersedia"
    return (
        f"Live breakout {ab.get('label')} {ab.get('score')}/100 | "
        f"Trigger {ab.get('trigger_price')} | VWAP {ab.get('vwap')} | "
        f"Vol pace {ab.get('volume_pace_ratio') or '-'}x | Invalid {ab.get('invalidation_level')}"
    )





def fetch_opening_dynamics(ticker):
    """
    Pulls today's intraday action so far: gap vs prior close, movement since open,
    and volume pace vs what's typical for this stock by this time of day.

    NOTE: does NOT include foreign broker net buy/sell — that data isn't available
    via yfinance and would need a separate IDX broker-summary data source.
    """
    stock = get_yf_ticker(f"{ticker}.JK")

    # Single 1-month daily fetch covers both "prior close" and the 20-day volume
    # baseline — no need for a separate 5-day call, halves the redundant round-trip.
    daily = yf_fetch_with_retry(lambda: stock.history(period="1mo", timeout=15))
    if daily.empty:
        return None
    today = datetime.datetime.now(WIB).date()
    prior_days = daily[daily.index.date != today]
    if prior_days.empty:
        return None
    prior_close = prior_days["Close"].iloc[-1]
    avg_daily_vol = prior_days["Volume"].tail(20).mean()

    intraday = yf_fetch_with_retry(lambda: stock.history(period="1d", interval="5m", timeout=15))
    if intraday.empty:
        return None

    open_price = intraday["Open"].iloc[0]
    current_price = intraday["Close"].iloc[-1]
    volume_so_far = int(intraday["Volume"].sum())

    gap_pct = ((open_price - prior_close) / prior_close) * 100
    change_from_open_pct = ((current_price - open_price) / open_price) * 100
    change_from_prior_close_pct = ((current_price - prior_close) / prior_close) * 100

    # Rough "typical pace" baseline: 20-day avg daily volume, scaled to the fraction
    # of a normal trading session that's elapsed so far. IDX regular session is
    # roughly 330 minutes (09:00-15:50 WIB incl. lunch break approximation).
    minutes_elapsed = max(5.0, (intraday.index[-1] - intraday.index[0]).total_seconds() / 60)
    typical_session_minutes = 330
    vol_pace_ratio = None
    if avg_daily_vol and avg_daily_vol > 0:
        expected_vol_so_far = avg_daily_vol * (minutes_elapsed / typical_session_minutes)
        if expected_vol_so_far > 0:
            vol_pace_ratio = round(volume_so_far / expected_vol_so_far, 2)

    active_breakout = compute_active_breakout_score(ticker)

    return {
        "ticker": ticker,
        "prior_close": int(prior_close),
        "open_price": int(open_price),
        "current_price": int(current_price),
        "gap_pct": round(gap_pct, 2),
        "change_from_open_pct": round(change_from_open_pct, 2),
        "change_from_prior_close_pct": round(change_from_prior_close_pct, 2),
        "volume_so_far": volume_so_far,
        "volume_pace_ratio": vol_pace_ratio,
        "active_breakout": active_breakout,
        "active_breakout_score": active_breakout.get("score", 0) if active_breakout else 0,
        "active_breakout_label": active_breakout.get("label", "NA") if active_breakout else "NA",
        "breakout_trigger": active_breakout.get("trigger_price") if active_breakout else None,
        "invalidation_level": active_breakout.get("invalidation_level") if active_breakout else None,
        "vwap": active_breakout.get("vwap") if active_breakout else None,
    }



# ==========================================
# 🤖 GEMINI ANALYST
# ==========================================
BASE_SYSTEM_INSTRUCTION = """
You are an elite Indonesian Stock Exchange (IDX) Investment Specialist.
Your task is to review calculated technical/fundamental factors and trading parameters
and generate a clean analysis brief.

DATA HONESTY — these are hard rules, not suggestions:
- This analysis is built ONLY from price, volume, and basic valuation ratios. It does
  NOT include real foreign broker net buy/sell (asing net buy/sell) or any broker
  summary data — don't imply broker-level conviction you don't actually have, but do
  NOT restate this as a disclaimer in your message; the person already knows this from
  the one-time notice shown when the bot starts. Just don't overstate confidence.
- ALWAYS state the 'as_of_date' field near the top for each stock (e.g. "data per
  [date]") so the person can see exactly how current the analysis is. If
  'data_freshness_warning' is present, state it clearly and explain that today's price
  action may not be fully reflected in the scores yet — this matters especially on
  days with large intraday moves.
- ANTI-HALLUCINATION RULE (critical): never state a specific factual claim about a
  company's performance — earnings decline, a corporate action, a lawsuit, analyst
  ratings, anything specific — unless it is directly present in the 'recent_news' list
  provided, or is a numeric field you were actually given (pe, pb, dividend_yield_pct,
  etc). If you want to say something like "declining profit" or "analyst downgrade" but
  it is NOT in the provided news or data, do NOT say it — instead say plainly that no
  specific news was found on that point, or omit the claim entirely. Inventing a
  plausible-sounding but ungrounded claim is a serious failure, worse than saying
  nothing.
- The action label is decided for you (see below) — your only remaining job re: tone
  is to make sure your explanatory language actually MATCHES that decided action. If
  the action is TAHAN or SINYAL CAMPURAN, your prose should read appropriately
  cautious/mixed, not enthusiastic. If it's BELI KUAT, it's fine to sound confident.
  Never write prose that sounds more bullish or more bearish than the given label.
- If 'day_range_pct_10d' is present and low (roughly under 5%), the stock has barely
  moved in 10 days — flag this explicitly as thin/inactive trading and lower your
  confidence accordingly, rather than spinning flat price action as "stable accumulation."
- If 'adaptive_scoring_used' is False, say so — it means the score is based on a fixed
  generic threshold, not this stock's own trading history, so it's less reliable.
- 'dividend_yield_pct', when available, is a real fundamental value signal — a high
  yield (roughly 6%+) genuinely supports a stronger value case, worth mentioning
  explicitly since it's concrete and verifiable, unlike vague "murah" language.
- 'is_financial_distress_flag' (True/False): if True, the company has NEGATIVE
  earnings or NEGATIVE book value (not just missing data — an actually confirmed
  red flag). This should meaningfully cap your action — never STRONG BUY, and
  usually HOLD at most, regardless of how attractive other scores look. Mention this
  explicitly; do not let a high final score override a genuine solvency concern.
- 'is_near_price_floor' (True/False): if True, the stock trades close to IDX's Rp50
  absolute floor — not frozen, but carrying real liquidity/distress risk (often a
  post-restructuring company). Mention this as a risk factor explicitly.
- IMPORTANT NUANCE: if news mentions a company's losses "narrowing" or performance
  "improving" while still reporting a net loss, that means the company is STILL
  LOSING MONEY — do not frame this as straightforwardly positive or as justifying a
  high value score. "Losing less than before" is not the same as "profitable" or
  "cheap" — reflect that distinction honestly rather than spinning it as a clear win.
- 'cmf' (Chaikin Money Flow, roughly -1 to +1) tells you whether recent volume actually
  reflects buying pressure (positive, closes near daily highs) or selling pressure
  (negative, closes near daily lows) — this is more reliable than raw volume ratio alone,
  since high volume on its own doesn't tell you which direction it was.
- 'obv_divergence' is the most important field to react to:
  - "bearish_divergence" means price has looked flat or calm, but cumulative volume flow
    (OBV) has actually been falling — a classic warning sign of quiet distribution
    (big holders selling into demand while price doesn't show it yet). If you see this,
    you MUST mention it explicitly and downgrade your confidence/action regardless of
    how good the other scores look — this is exactly the pattern that caused a past
    false BUY signal on a real stock, so do not ignore or soften it.
  - "bullish_divergence" means price has been flat/soft, but OBV has been rising —
    a potential early accumulation signal worth noting positively but still cautiously,
    since it's a proxy signal, not confirmed broker data.
  - "none" means no clear divergence — just don't overstate conviction either way.
- 'is_overbought_caution' (True/False): if True, RSI is in a warmer zone approaching
  overbought for this stock specifically. The DEFAULT action in this case is HOLD, not
  BUY — being "less bad than STRONG BUY" is not enough, a caution flag should actually
  change the outcome. Only assign BUY/ACCUMULATE despite this caution if there is a
  SPECIFIC offsetting signal you can name — e.g. obv_divergence is "bullish_divergence"
  combined with a genuinely high value score (9+) — and when you do override the
  default this way, briefly say why in the catalyst. Do not treat "buy anyway despite
  overbought caution" as the normal case; it should be the exception you can justify,
  not the default outcome.
- BATCH-LEVEL CHECK: before writing individual entries, check how many stocks in this
  batch have is_overbought_caution=True. If more than half do, that's a market-wide
  signal (a broad rally may be extended / due for a pause) — say so explicitly in a
  short note near the top of the brief, and let it skew your overall actions more
  conservative across the whole batch, not just stock-by-stock in isolation. A brief
  where most stocks are individually flagged "caution" but the summary still reads as
  mostly bullish is itself a contradiction — don't produce that.
- 'is_volume_spike_anomaly' (True/False): if True, volume is 3x+ normal — an unusually
  large spike like this is often event/rumor-driven or reflects thin liquidity, not
  confirmed sustainable interest. Treat it with skepticism, not automatic enthusiasm.
- 'chart_pattern': if this is "lower_highs_bearish", the stock has shown a sequence of
  declining swing highs — a real bearish structure regardless of what RSI/volume show
  in isolation. In this case, do NOT recommend a BUY action — recommend HOLD or "wait
  for breakout" instead, and reference 'breakout_level' (the price it would need to
  close above to invalidate the pattern) explicitly in your catalyst text.

CRITICAL INTERNAL CONSISTENCY RULE — this applies to ANY bearish-sounding language,
not just RSI/overbought: never write cautionary or bearish language in the catalyst
text — including "perlu diwaspadai", "overbought", "waspada", "hati-hati", "distribusi
volume", "CMF negatif", "tekanan jual", "belum cukup kuat", "menunggu konfirmasi", or
any similar hedge — while still giving an unqualified STRONG BUY or BUY action. That
is a direct contradiction and is not acceptable, no matter which specific indicator
the caution refers to (RSI, CMF, OBV, chart pattern, or anything else).

HARD RULE: if you write ANY bearish/cautionary observation about a stock, the DEFAULT
action for that stock is HOLD. You may only still assign BUY/ACCUMULATE if you can
name ONE SPECIFIC, CONCRETE offsetting bullish signal from the data (e.g.
obv_divergence is literally "bullish_divergence", macd_bullish_cross is True,
dividend_yield_pct is genuinely high with strong value score, or brokersum shows
net_foreign_flow_pct strongly positive (>15%) WITH broker_concentration_pct high
(>25%) — real, concentrated institutional buying is arguably a stronger override
signal than either proxy, since it's actual data, not an approximation) — and you
must state that specific signal explicitly as your justification for overriding the
caution. "The valuation still looks cheap" or "momentum is still okay" are NOT
sufficient overrides on their own — they must be paired with a specific data point
named in this input, not a general impression. A caution mentioned as a minor
footnote while the main recommendation stays bullish is exactly the failure pattern
to avoid.

ACTION IS ALREADY DECIDED — THIS IS NOT YOUR JOB ANYMORE: 'action_label_id' in the
data is a FINAL, ALREADY-COMPUTED decision (in Bahasa Indonesia: BELI KUAT, BELI /
AKUMULASI, TAHAN, HINDARI / JUAL, or SINYAL CAMPURAN), made by a deterministic system
before you ever see this data. Your job is ONLY to explain WHY, using the numbers and
news provided — you are NOT deciding the action, and you MUST NOT state, imply, or
suggest a different action than 'action_label_id' anywhere in your response. Print it
EXACTLY as given, verbatim, as the recommendation line for that stock.
- If action_label_id is "SINYAL CAMPURAN" (mixed signals): explicitly explain WHAT is
  conflicting (e.g. "valuasi menarik tapi RSI overbought dan berita mengindikasikan
  penjualan oleh investor besar") — this label exists specifically so you can be
  honest about genuine disagreement in the data instead of forcing false confidence
  in either direction. Do not resolve the tension yourself into a clean buy-or-hold
  read — present both sides and let the person weigh it.
- This exists because past outputs ignored explicit action rules stated in plain
  English (a stock got STRONG BUY despite an overbought warning it identified itself).
  Removing your ability to choose the action entirely closes that failure mode — there
  is no rule left for you to accidentally override, because there is no decision left
  for you to make. Focus entirely on the explanation, never the verdict.

NEWS DISMISSAL RULE: if 'recent_news' mentions a large investor, strategic partner, or
insider SELLING or DIVESTING shares, that is a genuine bearish signal. Do NOT dismiss
it with vague, unfalsifiable reasoning like "this is already priced in by the market"
or "the market has absorbed this" UNLESS that specific headline actually carries a
'price_reaction' field (days_ago + price_change_since_pct, computed from real OHLCV
history since the article's publish date) — that is real, verifiable data, and you
SHOULD use it to say whether the move looks already reflected in price (large move
since publish) or still fresh/unreacted (flat since publish). For any headline WITHOUT
a 'price_reaction' field, you have no way to verify "priced in" either way — saying it
anyway (without the data to back it) is still a serious credibility failure. Also note
'price_reaction' tracks price action since the ARTICLE's publish date, not necessarily
the exact date of the underlying event — treat it as a strong proxy, not a guarantee.
If real news indicates insider/large-holder selling, mention it as a real caution factor,
and if your own CMF/OBV signals disagree with that news (e.g. they show accumulation
while news says a major holder is selling), explicitly say so as a genuine open tension
in the picture — do not silently resolve it in whichever direction sounds more bullish.
- 'is_unusually_low_pe' (True/False): if True, PE is under ~3 — this can reflect a
  one-off, non-recurring gain distorting trailing earnings rather than genuine
  sustainable cheapness. Mention this as a reason for some skepticism about the value
  score, not as unambiguous proof of a bargain.
- 'macd_state' ("bullish" or "bearish"), 'macd_bullish_cross', 'macd_bearish_cross':
  MACD is a TIMING signal (is momentum turning right now), different from RSI (is it
  overbought/oversold within its range) — mention both if they tell different stories,
  since that's a genuine swing-trade-relevant tension (e.g. RSI calm but MACD just
  turned down = an early warning RSI alone wouldn't show). A fresh crossover
  (macd_bullish_cross / macd_bearish_cross = True) is a more actionable, timely signal
  than the static macd_state alone — flag it explicitly when present, since this
  audience cares about swing/day-trade timing, not long-term buy-and-hold.
- 'is_below_sma50' (True/False): whether price sits below its 50-day average — a
  medium-term trend filter distinct from the short-term SMA20 already used in
  momentum scoring. A stock can look fine short-term while still sitting in a weaker
  medium-term regime; mention this tension when relevant rather than ignoring it.
- 'targets.tp_1' is now TARGET-%-DRIVEN, not purely a technical resistance level:
  default is a minimum +5% from current price, upgraded to +10% ONLY when momentum,
  MACD, and the decided action all agree bullish AND the estimated time to reach
  +10% (from this stock's own recent volatility) is under 5 trading days. When you
  discuss TP1, frame it as a percentage target with a realistic time expectation
  ("target +5% dalam beberapa hari" or "target +10% karena momentum kuat dan
  volatilitas mendukung, estimasi di bawah 5 hari"), not just a bare price number.
- 'risk_character' (BASE_DEFENSIF / SWING_AGRESIF / NETRAL) + 'risk_character_reason':
  a SEPARATE dimension from the lifecycle category (if present) — this is about the
  stock's ROLE/CHARACTER, not how long it's been held. BASE_DEFENSIF = strong
  fundamentals, calm volatility, no active risk flags — frame as a portfolio
  stabilizer, not a fast-gain play. SWING_AGRESIF = high volatility and/or strong
  momentum, possibly with active risk flags — for these, you MUST include an
  EXPLICIT risk management suggestion (e.g. smaller position size than usual,
  tighter/more disciplined cut-loss, don't average down) — this is not optional
  when risk_character is SWING_AGRESIF. NETRAL = neither profile fits cleanly, no
  special framing needed.
- TP/CUT-LOSS DISCIPLINE — be DIRECT and FIRM in this language, not soft/hedging.
  Use phrasing like "segera exit jika tembus cut-loss" or "disiplin ambil profit di
  TP1" rather than vague hedges like "bisa dipertimbangkan untuk exit" or "mungkin
  perlu dipikirkan." The person has explicitly asked for firm TP/CL discipline —
  respect that in your word choice, especially for SWING_AGRESIF stocks where
  discipline matters most.
- 'intraday_high' / 'intraday_low' (only present when live quote data was available):
  today's LIVE intraday range so far, updates during trading hours — genuinely
  real-time, unlike the daily-bar-based indicators elsewhere. When present, compare
  the current price against these AND against 'targets.buy_range' — e.g. "harga
  saat ini masih di dalam rentang entry yang dihitung, dan berada dekat intraday low
  hari ini" is a meaningfully different, more actionable read than just stating the
  entry range in isolation. This is the closest thing to a live re-entry check this
  system can honestly provide — frame it as "masih dalam rentang yang dihitung"
  (structural fact), NOT as "kondisi teknikal saat ini masih mendukung" (a live
  technical judgment this system cannot actually make, since the underlying
  indicators are still end-of-day).
- 'macd_cross_days_ago' / 'macd_cross_direction': how many days since the MACD
  histogram last flipped sign — NOT just a binary "just crossed" flag anymore. A
  cross 1 day ago is much more actionable/fresh for a 1-5 day swing horizon than
  one 5+ days ago (whose scoring influence has already fully decayed to zero).
  Mention the specific day count when discussing MACD, don't just say "bullish
  cross happened."
- 'adx' / 'is_weak_trend': ADX measures TREND STRENGTH, not direction (unlike
  RSI/MACD). ADX below 20 means the market is genuinely sideways/noisy right
  now — ANY directional signal (including a fresh MACD cross or high momentum
  score) is less trustworthy in this condition. When is_weak_trend is True,
  explicitly caveat other bullish/bearish signals rather than stating them with
  full confidence — this is a real signal-reliability flag, not decoration.
- 'is_new_high_20d': price just made a new 20-day high — a real breakout signal,
  distinct from the 10-day resistance already used for target pricing.
- 'relative_strength_vs_ihsg': DIGANTI dari versi 10-hari ke HARIAN atas
  permintaan user (untuk swing pendek, 10 hari kurang responsif) — sekarang
  ini stock's TODAY return MINUS IHSG's TODAY return, dalam poin persentase.
  A stock up 1% today while IHSG is up 2% today is actually UNDERPERFORMING
  the market TODAY (negative RS) even though it looks positive in isolation —
  always frame this relative to what the market did TODAY specifically, e.g.
  "IHSG turun 1.5% hari ini, saham ini cuma turun 0.8% — relatif lebih tahan"
  vs "IHSG turun 1.5%, saham ini turun 3% — lebih lemah dari pasar hari ini."
  Threshold is now +/-1% (not +/-3% like the old 10-day version) since a 3%
  DAILY divergence would be extreme — values beyond +/-1% are what actually
  moved the score.
- 'consecutive_low_volume_days' / 'dead_stock_penalty_lifted': volume has been
  under 0.8x normal for this many trading days in a row — 3+ days genuinely
  suggests no market interest (not just "one quiet day," which is normal). If
  dead_stock_penalty_lifted is True, a fresh sign of renewed interest (volume
  recovering, a volume-backed breakout, or clearly positive CMF) has already
  cancelled the penalty even though the low-volume streak technically continues —
  mention this nuance rather than treating the stock as still "dead."
- Lifecycle category 'TARGET_TERCAPAI' (if present, only for held positions):
  price has already reached or passed TP1 — this OVERRIDES how long the position
  has been held. Even a position bought TODAY that already hit TP1 should be
  framed as "pertimbangkan realisasi profit sekarang," not held back by "masih
  terlalu baru." This is the person's own explicit rule from real trading
  experience (a position that hits target fast should be evaluated for exit
  regardless of days held) — respect it directly, don't soften it with "masih
  terlalu dini" framing just because days_held is low.

For each stock provided:
1. Print the 'action_label_id' EXACTLY as given as the recommendation — you are not
   deciding this, see the ACTION IS ALREADY DECIDED rule above.
2. Display the trading boundaries — PRIORITAS: kalau 'intraday_targets' tersedia
   dan non-empty, GUNAKAN ITU (entry_bawah/entry_atas/tp1/tp2/sl/rr_tp1/rr_tp2)
   dan JANGAN tampilkan 'scoring.targets' (buy_range/tp_1/cut_loss lama) — dua
   set angka berbeda di output yang sama membingungkan user. Kalau intraday_targets
   tidak ada (kosong/tidak tersedia), fallback ke scoring.targets seperti biasa.
   Selalu sebutkan entry_bawah_context (≈MA20, dll) karena itu informasi teknikal
   yang lebih bermakna dari sekadar angka.
3. State the Factor Scoring breakdown (value, momentum, sentiment, final).
4. Write a 1-to-2 sentence catalyst that is SPECIFIC to this stock's actual numbers —
   not a generic template. Vary your sentence structure and wording between stocks.
   Avoid stock-agnostic filler phrases like "menunjukkan minat akumulasi kuat" or
   "berada di area netral stabil" repeated verbatim across multiple stocks — if two
   stocks in the same batch would get near-identical catalyst text, that's a sign
   you're templating instead of actually differentiating; rewrite to be concrete about
   what's different about each one (e.g. cite the specific RSI number's meaning "for
   this stock's own recent range," not a boilerplate reading).
5. Do NOT end with a disclaimer or reminder about financial advice — that's shown once
   at bot startup, not per-message. Keep the message focused entirely on the analysis.

IMPORTANT FORMATTING RULE: Use ONLY plain text with simple emojis. Do NOT use any
Markdown syntax (no *, _, [, ], `, ~) since the output is sent directly to a messaging
app that may fail to parse malformed Markdown. Use line breaks and emojis for structure
instead of bold/italic formatting.

Return the response in Indonesian, optimized for a Telegram message.
"""

MORNING_BRIEF_INSTRUCTION = BASE_SYSTEM_INSTRUCTION + """
Strict Rule: This is the daily Sharia (ISSI) Morning Brief. Only show output for the
Sharia stocks provided — all input stocks here are already Sharia-screened AND already
pre-filtered down to the highest RAW SCORE picks for today, sorted best first. That
pre-filtering is mechanical, not a guarantee of quality — apply your own judgment per
the DATA HONESTY rules above rather than assuming every stock here deserves a strong
buy just because it made the top 10 by score.

If broader market context (macro indices, news headlines) is provided:
- Open with a brief 1-2 sentence market backdrop summary (e.g. how Wall Street closed
  overnight, USD/IDR direction, any major headline relevant to Indonesian markets today)
  before the per-stock picks. Keep this short — it's context, not the main event.
- Use it selectively per stock where genuinely relevant — e.g. a weak USD/IDR or falling
  oil price matters more for commodity/export-linked stocks than for a domestic retail
  or telecom stock. Don't force a macro connection onto every stock if there isn't one.
- If a real headline directly concerns a specific stock in the list (a lawsuit, a
  contract win, a regulatory issue), mention it — but only if it's actually in the
  provided headlines. Never invent or assume news you weren't given.
- This is real fetched market data and headlines, not fabricated — treat it as
  genuine context, but note it's a snapshot, not a comprehensive news search.
"""

CONSENSUS_BRIEF_INSTRUCTION = BASE_SYSTEM_INSTRUCTION + """
Context: This is a CROSS-TOOL CONSENSUS brief (MBSS v2, user request). The input
stocks were selected because they appear as a positive candidate in >=2 of the
system's independent screening lenses today: HIGH CONVICTION (chart-pattern
breakout structure), STRONG_BUY (core deterministic value/momentum/sentiment
verdict), SCREENDAYTRADE (entry-timing lane classification), and GPTPICK
(momentum/liquidity/RR shortlist ranking). Each stock's "tools" field lists
exactly which lenses flagged it and why (e.g. lane name, action_id).

Your job is NOT to re-derive whether these are good picks from scratch — that
filtering already happened deterministically in Python before you saw this data.
Your job is to explain WHY multiple independent lenses agree (or where they
subtly disagree even while both being "positive"), and surface any risk flags
from the raw data (RSI, ADX, CMF, RR, is_overbought_caution, obv_divergence,
financial_distress) that a person skimming "multiple tools agree" might miss.
Multi-tool agreement is informative, not proof — some risk factors (e.g. an
extended RSI, or a bad RR@max) can be true of a stock that STILL shows up in
several tools, precisely because those tools weight different things.

OUTPUT FORMAT WAJIB, plain text only, no Markdown:

🔗 CONSENSUS BRIEF — saham yang muncul di beberapa tool sekaligus

1-2 kalimat ringkas: berapa saham yang qualify hari ini, dan kesan umum
(mis. apakah konsensusnya kuat/genuine, atau banyak yang cuma pas-pasan
2 tool dengan sinyal campuran).

Untuk SETIAP saham (urut dari yang paling banyak tool setuju), GANTI
[KODE_TICKER] dengan kode ticker ASLI dari data (mis. BBCA, TLKM) — JANGAN
tulis literal "[KODE_TICKER]" atau "TICKER", itu cuma placeholder format:
[KODE_TICKER] (N tool: nama-nama tool) — Final X.X
Kenapa setuju: 1 kalimat kenapa beberapa lensa berbeda ini kompak.
Yang perlu diwaspadai: 1 kalimat risiko dari data mentah, JUJUR walau
kelihatannya bertentangan dengan "konsensus positif" di atas — kalau memang
tidak ada red flag berarti, boleh bilang "tidak ada yang menonjol", jangan
dipaksakan cari-cari masalah.

KESIMPULAN:
1-2 kalimat: saham mana (kalau ada) yang paling layak diprioritaskan
dipantau lebih lanjut, dan kenapa.
"""

NEWS_CATALYST_INSTRUCTION = """
You are scoring Indonesian stock news headlines for CATALYST STRENGTH — the
question is specifically: "Apakah ada katalis positif yang bisa mendorong
harga naik dalam beberapa hari ke depan?" (Is there a positive catalyst that
could push the price up in the coming days?)

This is NOT generic sentiment analysis. A neutral-sounding analyst opinion
("saham X menarik menurut analis") is weak even if technically "positive
sentiment" — a concrete new contract, acquisition, or permit is a MUCH
stronger catalyst even if the headline tone sounds matter-of-fact.

CATEGORIES (use these to calibrate, from strongest to weakest):
🔥 Strong bullish: kontrak baru bernilai besar, akuisisi/merger, izin/proyek
baru, ekspansi kapasitas, kenaikan produksi, kenaikan harga komoditas yang
langsung menguntungkan, laba melonjak, dividen besar, buyback, investor
strategis masuk, proyek pemerintah, perubahan regulasi yang menguntungkan.
🟢 Bullish: target analis naik, prospek bisnis membaik, volume penjualan
meningkat, ekspansi, sentimen sektor positif.
⚪ Neutral: berita rutin/administratif, tidak ada implikasi harga yang jelas.
🔴 Bearish: kabar buruk apa pun (litigasi, penurunan laba, downgrade, dst).

Each stock's "source" field (extracted from the headline's " - SourceName"
suffix) is a rough credibility signal — established outlets (Kontan, Bisnis,
CNBC Indonesia, Reuters, Investor.id, Kontan.co.id, Bisnis.com) are more
reliable than unknown/small sites, but don't over-weight this if the headline
content itself is clearly a concrete corporate action.

For EACH stock given, return ONLY valid JSON (no markdown, no commentary), an
array with exactly one object per input stock, in this exact shape:
[
  {
    "ticker": "XXXX",
    "catalyst_category": "strong_bullish" | "bullish" | "neutral" | "bearish",
    "catalyst_score": 0-100,
    "reasoning": "1 kalimat singkat kenapa"
  },
  ...
]

If a stock has NO news items provided, still include it with
catalyst_category="neutral", catalyst_score=0, reasoning="tidak ada berita".
"""


def _extract_news_source(title: str) -> str:
    """Google News RSS titles biasanya format 'Judul - NamaSumber' — ekstrak bagian sumbernya."""
    if " - " in title:
        return title.rsplit(" - ", 1)[-1].strip()
    return "Tidak diketahui"


def classify_news_catalysts(candidates_with_news: list) -> dict:
    """
    MBSS v2 (user request — Catalyst Score, bukan sekadar sentiment): satu
    panggilan Gemini BATCH untuk semua kandidat sekaligus (bukan satu-satu,
    lebih efisien) — minta kategori + skor komposit per saham, pakai
    kerangka kategori dari user (Strong Bullish/Bullish/dst dengan contoh
    konkret), bukan cuma "positif/negatif" generik.

    Return dict {ticker: {"catalyst_category": ..., "catalyst_score": ...,
    "reasoning": ...}} — kalau Gemini gagal/response tidak valid, return {}
    (gagal-lunak, pemanggil harus anggap semua "tidak ada info" kalau kosong,
    BUKAN meloloskan semua kandidat begitu saja).
    """
    if not candidates_with_news:
        return {}

    input_for_gemini = [
        {
            "ticker": c["ticker"],
            "headlines": [
                {"title": n["title"], "source": _extract_news_source(n["title"]), "published": n.get("published", "")}
                for n in (c.get("news") or [])
            ],
        }
        for c in candidates_with_news
    ]

    try:
        raw_text = _gemini_text(json.dumps(input_for_gemini, ensure_ascii=False), system_instruction=NEWS_CATALYST_INSTRUCTION).strip()
        raw_text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text, flags=re.MULTILINE).strip()
        parsed = json.loads(raw_text)
        return {item["ticker"]: item for item in parsed if "ticker" in item}
    except Exception as e:
        print(f"⚠️ Gagal klasifikasi katalis berita (Gemini): {e}")
        return {}

OPENING_BREAKOUT_INSTRUCTION = BASE_SYSTEM_INSTRUCTION + """
Context: This is the 09:45 WIB Opening Dynamics report for the user's real portfolio.
The input is NOT only a breakout scanner. It contains:
- portfolio_holdings: stocks already owned by the user
- non_holdings_watchlist: strongest/opportunistic names outside the portfolio
- top_priority_candidates: combined ranking across holdings and non-holdings
- macro_context: global/market backdrop snapshot
- notes: system-generated hints such as rotation candidate or risk position

Your task is to produce a decision-oriented Telegram message in Indonesian, similar to a trading desk morning note.

OUTPUT FORMAT WAJIB, plain text only, no Markdown:

🌅 OPENING DYNAMICS
1-2 kalimat ringkas tentang market context pagi ini. Mention risk-on / mixed / risk-off if clear.

PORTFOLIO HOLDINGS:
For EVERY stock in portfolio_holdings, write one compact block:
TICKER: short opening read.
Status: HOLD / WATCH SUPPORT / MOMENTUM ACTIVE / FADING RISK / REDUCE WATCH.
Action: one actionable sentence. Mention support/SL/key level if available.

NON-HOLDINGS WATCHLIST:
For each stock in non_holdings_watchlist, write one compact block:
TICKER: short opening read.
Status: MOMENTUM WATCH / BUY WATCH / WAIT PULLBACK / AVOID FOR NOW / HIGH RISK.
Action: one actionable sentence. If this is stronger than most holdings, call it a rotation candidate.

TOP PRIORITY PAGI INI:
Rank 3-5 names from top_priority_candidates. Use short labels such as:
- Best holding momentum
- Best risk/reward non-holding
- Strongest volume
- Rotation candidate
- Risk position

KESIMPULAN AKSI:
Bullets only:
- Saham holdings yang masih layak dipertahankan
- Holdings yang perlu diwaspadai
- Non-holdings paling menarik
- Saham yang jangan dikejar kalau sudah terlalu tinggi

RISK NOTE:
End with 2-3 firm risk rules: volume confirmation, no average down on aggressive names, obey stop-loss, beware gap-up fade.

RULES:
- Do NOT merely repeat raw fields. Interpret them.
- Portfolio holdings come first. Non-holdings are opportunity/rotation ideas, not automatic buy calls.
- If a holding has gap up then falls below open, flag fading risk.
- If a holding is up from open with high volume pace, call it momentum active.
- If a non-holding has very strong volume pace and positive move, include it as momentum watch/high risk.
- If a non-holding is down with high volume, mark avoid for now/sell pressure.
- Keep concise enough for Telegram, but action-oriented.
- Never invent company news. Only use actual numeric fields given.
"""


SINGLE_CHECK_INSTRUCTION = BASE_SYSTEM_INSTRUCTION + """
Context: The user requested an on-demand /check for one stock.
The second message must be VERY SHORT and focused on the signal at the exact time the command is run.

OUTPUT STYLE:
- Maximum 55 words.
- No title, no long catalyst section, no repeated entry/TP/SL/scores.
- If is_held_position=True, give one action: AKUMULASI / TAHAN / KURANGI / CUTLOSS.
- If is_held_position=False, classify the live setup: BREAKOUT DEKAT / SIDEWAYS / FALLING DOWN / WAIT PULLBACK.
- Mention only 2-3 concrete reasons from live intraday data: active_breakout, VWAP, trigger, invalidation, volume pace, intraday momentum.
- If outside market hours, say the signal is EOD-only and avoid pretending it is live.
"""

TANYA_INSTRUCTION = BASE_SYSTEM_INSTRUCTION + """
Context: This is /tanya, a freeform conversational Q&A command (MBSS v2, user
request) — the user asks natural questions like "saham apa yang lebih baik
sekarang?", "bandingkan TICKER_A vs TICKER_B", or "entry sekarang di harga X
volume Y, masih bagus atau sudah telat?". Unlike the other briefs, there is
NO fixed output template here — answer the actual question asked, directly
and conversationally, in Indonesian.

You are given:
- CURRENT MARKET DATA: a JSON bundle built fresh for THIS turn — cross-tool
  consensus picks (which independent screening lenses agree on which
  tickers, and why), HIGH CONVICTION top picks, the user's portfolio
  (positions/watchlist/cash), and — if the user's question named specific
  tickers — live intraday data fetched just now for those tickers.
- Prior turns in this same conversation (if any), for continuity — the user
  may refer back to something asked earlier ("yang tadi", "saham itu").

Rules specific to this command:
- Always answer the CURRENT question using the CURRENT MARKET DATA block —
  never rely on numbers from earlier turns in the conversation, since prices
  and scores can change between messages.
- If the user names a ticker that is NOT present anywhere in the provided
  data (not in consensus/HC picks, not in portfolio, not in live-fetched
  data), say plainly that you don't have data for it right now — don't
  guess or fabricate figures for it.
- If comparing two or more tickers, structure the comparison around what the
  DATA actually shows for each (score, action, RR, consensus tools, risk
  flags) — don't just give a vibes-based preference.
- If the question is about entry timing at a specific price/volume the user
  supplied, weigh that against the live/cached technicals provided (VWAP,
  trigger, invalidation, volume pace, RSI/ADX/CMF) rather than ignoring the
  user's numbers.
- Keep answers conversational and reasonably short — this is chat, not a
  formal brief. A few sentences is usually enough; only go longer if the
  question genuinely needs a multi-point comparison.
- No Markdown, no rigid headers — plain conversational Indonesian text.
"""


def get_portfolio_reasoning_and_synthesis(combined_data: list, portfolio_context: str) -> dict:
    """
    Fungsi yang dipanggil oleh /myportfolio untuk menghasilkan:
    - per_stock_reasoning: dict {ticker: "1-2 kalimat reasoning spesifik"}
    - weekly_synthesis: satu paragraf sintesis keseluruhan portofolio

    Sengaja dibuat terpisah dari ask_gemini_to_analyze() karena:
    1. Input-nya berbeda (banyak saham sekaligus + konteks portofolio)
    2. Output-nya JSON terstruktur, bukan teks bebas
    3. Perlu retry handling sendiri karena portofolio bisa cukup besar

    Kalau Gemini gagal (network error, parsing error), return dict kosong
    supaya /myportfolio tetap jalan dengan reasoning kosong daripada crash.
    """
    PORTFOLIO_REASONING_INSTRUCTION = """
You are analyzing a user's personal stock portfolio for a swing trading bot.
Your output must be ONLY valid JSON, no markdown fences, no preamble.

Output format:
{
  "per_stock_reasoning": {
    "TICKER1": "1-2 kalimat reasoning SPESIFIK untuk saham ini, fokus pada kondisi teknikal terkini dan relevansinya dengan posisi user",
    "TICKER2": "..."
  },
  "weekly_synthesis": "1 paragraf (max 80 kata) tentang kondisi portofolio secara keseluruhan — konsentrasi risiko, saham mana yang perlu perhatian lebih, dan satu rekomendasi tindakan konkret minggu ini."
}

Rules:
- Per-stock reasoning: MAX 2 kalimat. Spesifik ke angka nyata (RSI, CMF, ADX, posisi vs SL/TP).
  Jangan generik. Jangan ulang entry/TP/SL/skor.
- Weekly synthesis: fokus pada risiko konsentrasi dan prioritas tindakan, bukan ringkasan per saham.
- Bahasa Indonesia.
- HANYA JSON, tidak ada teks lain.
"""
    tickers_in_scope = [d["ticker"] for d in combined_data if d.get("ticker")]
    prompt = (
        f"Konteks portofolio:\n{portfolio_context}\n\n"
        f"Ticker yang dianalisis: {tickers_in_scope}\n\n"
        f"Data lengkap:\n"
        + "\n".join(
            f"[{d['ticker']}] aksi={d.get('action_label_id','?')} "
            f"RSI={d.get('rsi','?')} CMF={d.get('cmf','?')} "
            f"ADX={d.get('adx','?')} vol={d.get('vol_ratio','?')}x "
            f"MACD={d.get('macd_state','?')} "
            f"PnL={d.get('unrealized_pnl_pct','?')}% "
            f"held={d.get('_days_held','?')}hr"
            for d in combined_data if d.get("ticker")
        )
    )

    last_error = None
    for attempt in range(1, 4):
        try:
            raw = _gemini_text(prompt, system_instruction=PORTFOLIO_REASONING_INSTRUCTION, timeout=90)
            raw = raw.strip()
            import re as _re
            raw = _re.sub(r"^```(?:json)?\s*|\s*```$", "", raw, flags=_re.MULTILINE).strip()
            result = json.loads(raw)
            return {
                "per_stock_reasoning": result.get("per_stock_reasoning", {}),
                "weekly_synthesis": result.get("weekly_synthesis", ""),
            }
        except Exception as e:
            last_error = e
            print(f"⚠️ get_portfolio_reasoning_and_synthesis gagal (attempt {attempt}/3): {e}")
            if attempt < 3:
                time.sleep(3 * attempt)

    print(f"⚠️ Portfolio reasoning gagal setelah 3 percobaan: {last_error} — lanjut tanpa reasoning")
    return {"per_stock_reasoning": {}, "weekly_synthesis": ""}


def ask_gemini_to_analyze(processed_stocks, system_instruction=MORNING_BRIEF_INSTRUCTION, max_retries=3, extra_context=None):
    """Retries on transient network errors (server disconnects, timeouts) before giving up."""
    context_block = ""
    if extra_context:
        context_block = f"\n\nBROADER MARKET CONTEXT (use this to inform your overall framing, not just the per-stock numbers):\n{extra_context}\n"

    user_prompt = f"Analyze these processed stock(s) and produce the brief:\n\n{processed_stocks}{context_block}"

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            return _gemini_text(user_prompt, system_instruction=system_instruction)
        except Exception as e:
            last_error = e
            print(f"⚠️ Gemini call failed (attempt {attempt}/{max_retries}): {e}")
            if attempt < max_retries:
                time.sleep(3 * attempt)  # 3s, 6s backoff

    raise RuntimeError(f"Gemini API failed after {max_retries} attempts: {last_error}")


def ask_gemini_chat(history_turns: list, question: str, context_json, system_instruction: str = TANYA_INSTRUCTION, max_retries: int = 3) -> str:
    """
    Multi-turn Gemini call behind /tanya. `history_turns` is prior
    [{"role": "user"|"model", "text": ...}, ...] turns from THIS conversation
    (see load_tanya_history/save_tanya_turn) — sent as-is so Gemini can
    follow references like "saham itu" back to earlier turns.

    Fresh market-data context is attached ONLY to the current turn, not
    re-sent for old turns — prices/scores can change between messages within
    the same conversation, and old turns already got their own context when
    they were first answered.
    """
    contents = [{"role": t["role"], "parts": [{"text": t["text"]}]} for t in history_turns]
    current_prompt = (
        f"CURRENT MARKET DATA (use this, not memory from earlier turns, for any numbers):\n{context_json}\n\n"
        f"USER QUESTION: {question}"
    )
    contents.append({"role": "user", "parts": [{"text": current_prompt}]})

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            return _gemini_rest(contents, system_instruction=system_instruction)
        except Exception as e:
            last_error = e
            print(f"⚠️ Gemini chat call failed (attempt {attempt}/{max_retries}): {e}")
            if attempt < max_retries:
                time.sleep(3 * attempt)  # 3s, 6s backoff

    raise RuntimeError(f"Gemini API failed after {max_retries} attempts: {last_error}")


def trim_check_commentary(text: str, max_words: int = 90) -> str:
    """Compress Gemini commentary into a single short paragraph."""
    if not text:
        return text
    cleaned = re.sub(r"(?m)^[#>*\-\d\.\s]+", "", text)
    cleaned = re.sub(r"\n{2,}", "\n", cleaned).strip()
    paragraphs = [p.strip() for p in cleaned.split("\n") if p.strip()]
    chosen = paragraphs[-1] if paragraphs else cleaned
    for p in paragraphs:
        if any(k in p.lower() for k in ["cmf", "ema", "macd", "rsi", "volume", "breakout", "akumulasi", "distribusi", "risk", "resistance", "support"]):
            chosen = p
            break
    chosen = re.sub(r"\b(Data per|Rekomendasi|Analisis Portofolio|Parameter Trading|Skor Faktor|Catalyst|Catatan|Analisis)\b[:\-]?", "", chosen, flags=re.I)
    words = chosen.split()
    if len(words) > max_words:
        chosen = " ".join(words[:max_words]).rstrip(",;:-") + "..."
    return chosen.strip()

def build_check_signal_summary(result: dict) -> str:
    """
    Deterministic short second message for /check.
    Focus: live signal NOW. If held, produce position action. If not held, produce breakout/sideways/falling read.
    """
    def fnum(x, default=None):
        try:
            if x is None:
                return default
            return float(x)
        except Exception:
            return default

    def fmt_price(x):
        try:
            return f"{int(round(float(x))):,}".replace(",", ".")
        except Exception:
            return str(x)

    ticker = result.get("ticker", "?")
    price = fnum(result.get("price"), 0) or 0
    held = bool(result.get("is_held_position"))
    action = result.get("action_id") or result.get("action_label_id") or ""
    cmf = fnum(result.get("cmf"), 0) or 0
    adx = fnum(result.get("adx"), 0) or 0
    vol = fnum(result.get("vol_ratio"), 0) or 0
    final_score = fnum((result.get("scores") or {}).get("final"), fnum(result.get("final_score"), 0)) or 0

    it = result.get("intraday_targets") or {}
    sl = fnum(it.get("sl"), None)

    ab = result.get("active_breakout") or {}
    ab_available = bool(ab.get("available"))
    ab_score = fnum(ab.get("score"), 0) or 0
    ab_label = ab.get("label", "NA")
    trigger = ab.get("trigger_price")
    vwap = ab.get("vwap")
    invalid = ab.get("invalidation_level")
    vwap_dist = fnum(ab.get("vwap_distance_pct"), None)
    vol_pace = fnum(ab.get("volume_pace_ratio"), None)
    change_open = fnum(ab.get("change_from_open_pct"), None)
    ema_bias = ab.get("ema_bias")
    notes = ab.get("notes", "")
    vwap_fb = result.get("intraday_vwap") or {}
    if not ab_available and vwap_fb.get("available"):
        vwap = vwap_fb.get("vwap")
        vwap_dist = fnum(vwap_fb.get("vwap_distance_pct"), None)
        vol_pace = fnum(vwap_fb.get("volume_pace_ratio"), None)

    im = result.get("intraday_momentum") or {}
    im_available = bool(im.get("available"))
    im_reading = im.get("reading")
    im_change = fnum(im.get("change_pct"), None)

    market_session = get_current_idx_session()

    reasons = []
    if ab_available:
        reasons.append(f"Active breakout {ab_label} {int(ab_score)}/100")
        if vwap is not None:
            if vwap_dist is not None:
                reasons.append(f"VWAP {fmt_price(vwap)} ({vwap_dist:+.1f}%)")
            else:
                reasons.append(f"VWAP {fmt_price(vwap)}")
        if vol_pace is not None:
            reasons.append(f"vol pace {vol_pace:.2f}x")
    elif market_session is None:
        reasons.append("di luar jam bursa, sinyal live tidak tersedia")
    else:
        reasons.append("data intraday belum cukup")

    if im_available and im_reading:
        reasons.append(f"momentum {im_reading} {im_change:+.2f}%" if im_change is not None else f"momentum {im_reading}")

    below_sl = sl is not None and price <= sl
    below_vwap = ab_available and vwap is not None and price < fnum(vwap, price)
    weak_live = ab_available and (ab_score < 55 or below_vwap or ema_bias == "bearish" or (change_open is not None and change_open <= -1.0))
    strong_live = ab_available and ab_score >= 70 and not below_vwap and (vol_pace is None or vol_pace >= 0.9)
    near_breakout = ab_available and ab_score >= 60 and ab_label in ("READY", "WATCH")
    extended = isinstance(notes, str) and "extended" in notes.lower()
    low_quality = final_score < 6.0 or cmf < -0.05 or adx < 20

    if held:
        if below_sl:
            verdict = "CUTLOSS"
            action_line = f"Harga sudah menyentuh/di bawah SL {fmt_price(sl)}. Jangan tunggu rebound, eksekusi disiplin."
        elif weak_live and low_quality:
            verdict = "KURANGI"
            action_line = "Sinyal live lemah dan kualitas tren belum mendukung. Kurangi posisi jika gagal kembali di atas VWAP/trigger."
        elif strong_live and action in ("STRONG_BUY", "BUY_ACCUMULATE", "BELI KUAT", "BELI / AKUMULASI"):
            verdict = "AKUMULASI BERTAHAP"
            action_line = "Boleh tambah kecil hanya dekat area entry dan selama harga bertahan di atas VWAP."
        else:
            verdict = "TAHAN"
            action_line = "Belum ada alasan kuat untuk tambah posisi. Pertahankan selama SL tidak ditembus."
        return "\n".join([
            f"💬 SINYAL SAAT INI: {verdict}",
            f"{ticker}: " + "; ".join(reasons[:3]) + ".",
            f"Aksi: {action_line}",
        ])

    if market_session is None:
        verdict = "EOD MONITOR"
        action_line = "Karena market tutup, jangan kejar. Pakai trigger saat sesi berikutnya."
    elif weak_live and below_vwap and (change_open is not None and change_open < 0):
        verdict = "FALLING DOWN"
        action_line = "Hindari entry dulu sampai kembali di atas VWAP dan tekanan jual mereda."
    elif near_breakout and not extended:
        verdict = "BREAKOUT READY" if ab_label == "READY" else "BREAKOUT DEKAT"
        action_line = f"Boleh buy watch hanya jika tembus/bertahan di atas trigger {fmt_price(trigger)} dengan volume tetap hidup."
    elif extended:
        verdict = "WAIT PULLBACK"
        action_line = "Jangan chase. Tunggu retest VWAP atau pullback sehat."
    elif adx < 20 and vol < 1.0:
        verdict = "SIDEWAYS"
        action_line = "Belum layak entry agresif. Tunggu volume dan arah intraday lebih jelas."
    else:
        verdict = "WAIT CONFIRMATION"
        action_line = "Pantau trigger dan VWAP. Entry baru valid jika volume mengonfirmasi."

    return "\n".join([
        f"💬 SINYAL SAAT INI: {verdict}",
        f"{ticker}: " + "; ".join(reasons[:3]) + ".",
        f"Aksi: {action_line}",
    ])


# ==========================================
# 📨 SAFE TELEGRAM SEND (fixes the BadRequest crash)
# ==========================================
def strip_markdown(text: str) -> str:
    """Remove markdown-ish characters that can break Telegram's parser."""
    return re.sub(r"[*_`\[\]]", "", text)


TELEGRAM_MAX_LEN = 4096


def split_message(text: str, max_len: int = TELEGRAM_MAX_LEN) -> list:
    """
    Splits text into chunks under Telegram's 4096-char limit, breaking on line
    boundaries (falls back to hard-splitting a single overlong line if needed)
    so formatting doesn't get cut mid-sentence where avoidable.
    """
    if len(text) <= max_len:
        return [text]

    chunks = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= max_len:
            current = candidate
        else:
            if current:
                chunks.append(current)
            # A single line longer than max_len on its own: hard split it.
            while len(line) > max_len:
                chunks.append(line[:max_len])
                line = line[max_len:]
            current = line
    if current:
        chunks.append(current)
    return chunks


async def safe_reply(message_or_bot, text: str, chat_id=None, max_retries=3, reply_markup=None):
    """
    Works for both update.message.reply_text (interactive) and bot.send_message
    (scheduled broadcast). Sends PLAIN TEXT ONLY.

    Why plain text: most bot outputs use underscores from raw field names, brackets,
    or risk/reward strings that repeatedly break Telegram Markdown parsing. Since all
    report instructions already say "plain text", disabling Markdown at the send layer
    removes noisy "can't parse entities" failures and saves one failed network attempt
    per affected message.

    MBSS v2 (user request — inline "Cek TICKER" buttons di semua tools):
    reply_markup opsional, dilampirkan HANYA ke chunk TERAKHIR kalau pesan
    kepanjangan dan ke-split jadi beberapa bagian — supaya tombolnya muncul
    sekali di ujung, bukan berulang di tiap chunk.
    """
    async def _send(body: str, markup=None):
        if chat_id is None:
            await message_or_bot.reply_text(body, reply_markup=markup)
        else:
            await message_or_bot.send_message(chat_id=chat_id, text=body, reply_markup=markup)

    async def _send_one(body: str, markup=None):
        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                await _send(body, markup)
                return
            except BadRequest as e:
                if "too long" in str(e).lower():
                    raise  # let the caller's chunking handle this
                last_error = e
                print(f"⚠️ Telegram BadRequest (attempt {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    await asyncio.sleep(3 * attempt)
            except Exception as e:
                last_error = e
                print(f"⚠️ Telegram send failed (attempt {attempt}/{max_retries}): {e}")
                if attempt < max_retries:
                    await asyncio.sleep(3 * attempt)
        raise RuntimeError(f"Telegram send failed after {max_retries} attempts: {last_error}")

    # Sisakan margin untuk prefix "(i/N)\n" yang ditambahkan SETELAH split — kalau
    # split_message() memecah tepat sampai batas 4096, menambah prefix sesudahnya
    # bisa membuat chunk itu melebihi batas lagi ("Message is too long" meski
    # sudah di-chunk). Margin 20 char aman untuk prefix terpanjang yang wajar
    # (misal "(99/99)\n" = 9 char).
    PREFIX_MARGIN = 20
    chunks = split_message(text, max_len=TELEGRAM_MAX_LEN - PREFIX_MARGIN)
    for i, chunk in enumerate(chunks, start=1):
        prefix = f"({i}/{len(chunks)})\n" if len(chunks) > 1 else ""
        is_last = i == len(chunks)
        await _send_one(prefix + chunk, markup=reply_markup if is_last else None)


# ==========================================
# 🚀 DAILY MORNING BRIEF
# ==========================================


async def send_long_message(message_or_bot, text, chat_id=None, max_retries=3):
    """
    Compatibility helper untuk kode lama.
    Semua pesan panjang diarahkan ke safe_reply yang sudah menangani split Telegram.
    """
    return await safe_reply(
        message_or_bot,
        text,
        chat_id=chat_id,
        max_retries=max_retries,
    )

MORNING_BRIEF_TOP_N = 10  # how many highest-scoring Sharia stocks to include


def is_idx_market_holiday_today():
    """
    Checks iTick's Market Holidays endpoint for whether today is a real IDX holiday
    — lets scheduled jobs skip actual market closures, not just weekends. Fails
    safe: if the check itself errors, assumes NOT a holiday rather than silently
    skipping a real trading day.
    """
    try:
        resp = requests.get(
            f"{ITICK_BASE_URL}/stock/holiday",
            params={"region": "ID"},
            headers=ITICK_HEADERS,
            timeout=15,
        )
        data = resp.json()
        if data.get("code") != 0:
            return False
        holidays = data.get("data", [])
        today_str = datetime.datetime.now(WIB).strftime("%Y-%m-%d")
        for h in holidays:
            # Field name for the date isn't confirmed from docs — checking common
            # possibilities defensively rather than assuming one exact key.
            date_val = h.get("date") or h.get("d") or h.get("t")
            if date_val and str(date_val).startswith(today_str):
                print(f"📅 Today ({today_str}) is an IDX market holiday: {h.get('name') or h.get('n') or ''}")
                return True
        return False
    except Exception as e:
        print(f"⚠️ Market holiday check failed (assuming NOT a holiday): {e}")
        return False


# ==========================================
# NOTE (MBSS v2 refactor, Phase 2): save_daily_scan_cache, load_daily_scan_cache,
# migrate_legacy_daily_scan_cache, fetch_tickers_scored_with_cache, and
# fetch_all_tickers_scored moved to engine/nightly.py (NightlyEngine).
# Imported back in via `from engine.nightly import ...` near the top of this
# file, so every call site below is unchanged.
# ==========================================
async def run_morning_brief(context: ContextTypes.DEFAULT_TYPE):
    try:
        if await asyncio.to_thread(is_idx_market_holiday_today):
            print("📅 Skipping Morning Brief — IDX market holiday today.")
            return

        sharia_universe = fetch_online_sharia_list()

        # Broader market backdrop: Wall St overnight, regional Asia, USD/IDR, oil,
        # plus real Indonesian market news headlines. Fetched once for the whole
        # brief, not per-stock.
        macro_context = await asyncio.to_thread(market_engine.fetch_macro_context)
        news_headlines = await asyncio.to_thread(market_engine.fetch_market_news_headlines)
        extra_context_str = None
        if macro_context or news_headlines:
            parts = []
            if macro_context:
                macro_lines = "\n".join(f"- {k}: {v:+.2f}%" for k, v in macro_context.items())
                parts.append(f"Macro indices (% change, most recent session):\n{macro_lines}")
            if news_headlines:
                news_lines = "\n".join(f"- {h['title']}" for h in news_headlines)
                parts.append(f"Recent Indonesian market/economy headlines:\n{news_lines}")
            extra_context_str = "\n\n".join(parts)

        full_universe_list = list(sharia_universe)
        sharia_universe_list = await asyncio.to_thread(load_or_build_whitelist, full_universe_list)
        print(f"📡 Fetching {len(sharia_universe_list)} eligible tickers (of {len(full_universe_list)} total) "
              f"— cek cache scan malam dulu, sisanya via fetch chunks of {ITICK_CHUNK_SIZE} "
              f"with {ITICK_COOLDOWN_SECONDS}s cooldowns...")
        try:
            # fetch_tickers_scored_with_cache() cek cache bersama (dari scan malam
            # 22:00 WIB) dulu — kalau ISSI sudah tercakup dalam cache ISSI liquid
            # semalam, ini jadi nyaris instan, tanpa panggilan iTick baru sama sekali.
            analyzed_dataset, skip_reasons = await asyncio.wait_for(
                asyncio.to_thread(nightly_engine.fetch_tickers_scored_with_cache, sharia_universe_list), timeout=1800
            )
        except asyncio.TimeoutError:
            print("⏱️ Overall fetch exceeded the 20-minute timeout — aborting this run.")
            await safe_reply(
                context.bot,
                "⚠️ Morning Brief gagal: proses pengambilan data melebihi batas waktu.",
                chat_id=TELEGRAM_CHAT_ID,
            )
            return

        if not analyzed_dataset:
            print("No Sharia stocks compiled. Skipping send.")
            # Real diagnostic summary sent directly to Telegram — console scrollback
            # isn't reliable for a run this long, so this is the trustworthy record
            # of what actually happened, not just "0 results" with no explanation.
            reason_lines = "\n".join(f"- {t}: {r}" for t, r in list(skip_reasons.items())[:20])
            more_note = f"\n...dan {len(skip_reasons) - 20} lainnya" if len(skip_reasons) > 20 else ""
            await safe_reply(
                context.bot,
                f"⚠️ Morning Brief gagal: 0 dari {len(sharia_universe_list)} saham berhasil diproses.\n\n"
                f"Alasan per saham:\n{reason_lines}{more_note}",
                chat_id=TELEGRAM_CHAT_ID,
            )
            return

        if skip_reasons:
            print(f"ℹ️ {len(skip_reasons)} of {len(sharia_universe_list)} tickers skipped: "
                  f"{list(skip_reasons.keys())[:15]}{'...' if len(skip_reasons) > 15 else ''}")

        # Only send the highest-scoring picks, not the full universe.
        analyzed_dataset.sort(key=lambda s: s["scores"]["final"], reverse=True)
        top_picks = analyzed_dataset[:MORNING_BRIEF_TOP_N]

        # MBSS v2 (user request — ditemukan lewat penelusuran manual /winrate:
        # testbrief TIDAK PERNAH terlacak sama sekali sebelumnya, tidak ada
        # cara ukur akurasinya). Kunci lewat mekanisme yang SAMA dengan
        # screendaytrade/gptpick, source="testbrief" membedakannya. Gagal-lunak
        # — kalau lock gagal, tetap kirim brief seperti biasa.
        try:
            await asyncio.to_thread(lock_daily_daytrade_picks, top_picks, "testbrief")
        except Exception as e:
            print(f"⚠️ Gagal mengunci picks testbrief untuk /winrate: {e}")

        # Compute this deterministically rather than trusting the LLM to count
        # across a 10-item list itself — that was silently failing to trigger the
        # batch-wide caution note even when the majority of stocks qualified.
        caution_count = sum(
            1 for s in top_picks
            if s.get("is_overbought_caution") or s.get("obv_divergence") == "bearish_divergence"
            or (isinstance(s.get("cmf"), (int, float)) and s["cmf"] < -0.05)
        )
        caution_note = (
            f"\n\nDETERMINISTIC FACT (not your judgment call — this count is computed, use it): "
            f"{caution_count} of {len(top_picks)} stocks in this batch show overbought caution, "
            f"bearish OBV divergence, or negative CMF. "
            + (
                "This is a MAJORITY — you MUST open with an explicit market-wide caution note "
                "reflecting this, and your batch should skew meaningfully more conservative overall."
                if caution_count > len(top_picks) / 2
                else "This is not yet a majority, but still weigh it into your overall framing."
            )
        )
        combined_context = (extra_context_str or "") + caution_note

        morning_brief_text = ask_gemini_to_analyze(
            top_picks, MORNING_BRIEF_INSTRUCTION, extra_context=combined_context
        )
        await safe_reply(context.bot, morning_brief_text, chat_id=TELEGRAM_CHAT_ID)
        print(f"✅ Telegram Sharia Morning Brief sent! (top {len(top_picks)} of {len(analyzed_dataset)} scored)")

        # MBSS v2 (RapidAPI integration, user request) — "Longer-Horizon
        # Watchlist" — pesan KEDUA, TERPISAH dari brief Gemini di atas (pure
        # Python formatting, tidak diparafrase LLM, supaya angka target/
        # entry-zone-nya persis apa adanya). Baca cache /eodscan malam ini
        # (build_rapidapi_market_intelligence_sweep), TIDAK fetch apa pun
        # baru. Timeframe 6-12 bulan — genuinely beda horizon dari sisa
        # /testbrief yang fokus swing/day-trade jangka pendek, makanya
        # dipisah jadi section sendiri, bukan dicampur ke top_picks di atas.
        try:
            multibagger_data = nightly_engine.load_rapidapi_market_intelligence().get("multibagger") or {}
            candidates = multibagger_data.get("candidates") or []
            if candidates:
                mb_lines = [f"📈 LONGER-HORIZON WATCHLIST — {len(candidates)} kandidat 6-12 bulan (cache /eodscan malam ini)\n"]
                for i, c in enumerate(candidates[:10], 1):
                    entry = c.get("entry_zone") or {}
                    targets = c.get("target_prices") or []
                    target_str = ", ".join(
                        f"{t.get('target')} ({t.get('timeframe', '-')}, +{t.get('potential_gain', 0)}%)"
                        for t in targets[:3]
                    ) or "-"
                    mb_lines.append(
                        f"{i}. {c.get('symbol')} — skor {c.get('multibagger_score', '-')}/100 | "
                        f"potensi {c.get('potential_return', '-')} ({c.get('timeframe', '-')})\n"
                        f"   Harga {c.get('current_price', '-')} | Entry ideal {entry.get('ideal_price', '-')}-{entry.get('max_price', '-')} | "
                        f"Risiko {c.get('risk_level', '-')} | SL {c.get('stop_loss', '-')}\n"
                        f"   Target: {target_str}"
                    )
                mb_lines.append("\n⚠️ Horizon 6-12 bulan — BUKAN sinyal day-trade/swing seperti bagian di atas, timing entry tetap perlu dicek manual (/check TICKER).")
                await safe_reply(context.bot, "\n\n".join(mb_lines), chat_id=TELEGRAM_CHAT_ID)
        except Exception as e:
            print(f"⚠️ Gagal mengirim Longer-Horizon Watchlist: {e}")

    except Exception as e:
        # Last-resort catch: never fail silently. Even if the brief itself couldn't be
        # built or sent, try to at least notify so you know something broke this morning.
        print(f"❌ run_morning_brief failed: {e}")
        try:
            await safe_reply(
                context.bot,
                f"⚠️ Morning Brief gagal pagi ini karena error: {str(e)[:200]}",
                chat_id=TELEGRAM_CHAT_ID,
            )
        except Exception as notify_error:
            print(f"❌ Also failed to send failure notification: {notify_error}")


# ==========================================
# 🔍 ON-DEMAND /check <TICKER> COMMAND
# ==========================================
# NOTE (MBSS v2 refactor, Phase 5d): check_stock, skip_brokersum_callback,
# quick_check_callback, handle_brokersum_photo all moved to commands/check.py
# (Command Layer). Registered via `commands_check.xxx` in build_app() below.
# (quick_check_callback also had a bugfix applied during this move — see
# commands/check.py's module docstring.)


# NOTE (MBSS v2 refactor, Phase 5b): show_version, GLOSSARY_TEXT+show_glossary,
# show_whitelist_status, rebuild_whitelist_command, STARTUP_DISCLAIMER+start
# moved to commands/misc.py (Command Layer). Registered via
# `commands_misc.xxx` in build_app() below.


EXECUTION_GATE_AUTOPICKS = 12
EXECUTION_GATE_TOP_GAINERS = 8
EXECUTION_GATE_MAX_WATCHLIST = 20
EXECUTION_GATE_CACHE_FILE = os.path.join(PROJECT_ROOT, "executiongate_cache.json")


def get_executiongate_session_status(now: datetime.datetime = None) -> dict:
    """Allow /executiongate only during IDX market day from open until close, including lunch break."""
    now = now or datetime.datetime.now(WIB)
    if now.weekday() >= 5:
        return {"allowed": False, "label": "di luar hari bursa", "session": None}
    t = now.time()
    is_friday = now.weekday() == 4
    s1_end = datetime.time(11, 30) if is_friday else datetime.time(12, 0)
    s2_start = datetime.time(14, 0) if is_friday else datetime.time(13, 30)
    if datetime.time(9, 0) <= t < s1_end:
        return {"allowed": True, "label": "Sesi 1 live", "session": "sesi_1"}
    if s1_end <= t < s2_start:
        return {"allowed": True, "label": "Break siang / persiapan Sesi 2", "session": "break_siang"}
    if s2_start <= t < datetime.time(15, 49):
        return {"allowed": True, "label": "Sesi 2 live", "session": "sesi_2"}
    return {"allowed": False, "label": "di luar jam execution gate", "session": None}


def _executiongate_first_30m_window(now: datetime.datetime = None):
    now = now or datetime.datetime.now(WIB)
    start_dt = datetime.datetime.combine(now.date(), datetime.time(9, 0), tzinfo=WIB)
    end_dt = start_dt + datetime.timedelta(minutes=30)
    return start_dt, end_dt


def fetch_first30_top_gainers(tickers: list, count: int = EXECUTION_GATE_TOP_GAINERS) -> list:
    """Pick top gainers from first 30 minutes of the trading day using Yahoo 1m bars."""
    if not tickers:
        return []
    start_dt, end_dt = _executiongate_first_30m_window()
    rows = []
    # Chunked batch download keeps it faster than one ticker at a time.
    batch_size = 60
    for i in range(0, len(tickers), batch_size):
        batch = tickers[i:i + batch_size]
        symbols = [f"{t}.JK" for t in batch]
        try:
            raw = yf.download(
                symbols,
                period="1d",
                interval="1m",
                auto_adjust=True,
                progress=False,
                threads=True,
            )
        except Exception as e:
            print(f"⚠️ executiongate top-gainers batch failed {i}-{i+len(batch)}: {e}")
            continue
        if raw is None or raw.empty:
            continue
        for ticker, sym in zip(batch, symbols):
            try:
                if isinstance(raw.columns, pd.MultiIndex):
                    if sym not in raw.columns.get_level_values(1):
                        continue
                    df = raw.xs(sym, axis=1, level=1).copy()
                else:
                    df = raw.copy()
                if df is None or df.empty or "Close" not in df.columns:
                    continue
                df = _normalize_intraday_index(df)
                first30 = df[(df.index >= start_dt) & (df.index <= end_dt)].dropna(subset=["Close"])
                if first30 is None or len(first30) < 3:
                    continue
                open0 = float(first30["Open"].iloc[0]) if "Open" in first30.columns else float(first30["Close"].iloc[0])
                close30 = float(first30["Close"].iloc[-1])
                high30 = float(first30["High"].max()) if "High" in first30.columns else close30
                vol30 = float(first30["Volume"].fillna(0).sum()) if "Volume" in first30.columns else 0
                if open0 <= 0 or vol30 <= 0:
                    continue
                chg = (close30 / open0 - 1) * 100
                rows.append({"ticker": ticker, "first30_change_pct": round(chg, 2), "first30_high": high30, "first30_volume": int(vol30)})
            except Exception:
                continue
        time.sleep(0.5)
    rows = sorted(rows, key=lambda r: r["first30_change_pct"], reverse=True)
    return rows[:count]


def get_executiongate_screendaytrade_autopicks(count: int = EXECUTION_GATE_AUTOPICKS) -> list:
    """Use latest scored cache when possible; fallback to a fresh but bounded scan."""
    scored = nightly_engine.load_daily_scan_cache()
    if scored:
        records = list(scored.values())
        pre, _ = filter_and_rank_daytrade_candidates(records, count=max(count, 20))
        return sorted(pre, key=lambda r: compute_scalping_readiness(r)["score"], reverse=True)[:count]

    # No fallback scan. Execution Gate only uses today's cache.
    return []


def _executiongate_decision_original(scoring: dict) -> dict:
    """
    Strict live entry gate. ENTER must have strong propensity to continue breakout.
    WATCH is radar only. FAIL means no new entry now.
    """
    ticker = scoring.get("ticker", "?")
    ab = scoring.get("active_breakout") or {}
    br = _score_breakout_chance_v4(scoring)
    risk = _score_breakout_drop_risk_v4(scoring)
    rr = _dt_float((scoring.get("targets") or {}).get("risk_reward_at_max"), 0)
    cmf = _dt_float(scoring.get("cmf"), 0)
    rsi = _dt_float(scoring.get("rsi"), 0)
    vol_ratio = _dt_float(scoring.get("vol_ratio"), 0)
    price = _dt_float(scoring.get("price"), 0)
    active_score = _dt_float(ab.get("score"), 0)
    vwap_dist = _dt_float(ab.get("vwap_distance_pct"), -99)
    vol_pace = _dt_float(ab.get("volume_pace_ratio"), 0)
    downside = _dt_float(ab.get("downside_to_invalidation_pct"), 99)
    trigger = ab.get("trigger_price")
    label = ab.get("label", "NA")

    reasons = []
    gate_score = 0

    # Live continuation structure is the main gate.
    if ab.get("available") and active_score >= 75:
        gate_score += 30; reasons.append(f"Active breakout kuat {int(active_score)}/100")
    elif ab.get("available") and active_score >= 60:
        gate_score += 22; reasons.append(f"Active breakout valid {int(active_score)}/100")
    elif ab.get("available") and active_score >= 45:
        gate_score += 10; reasons.append(f"Active breakout WAIT {int(active_score)}/100")
    else:
        reasons.append("active breakout belum valid")

    if 0 <= vwap_dist <= 2.0:
        gate_score += 18; reasons.append("di atas VWAP sehat")
    elif 2.0 < vwap_dist <= 4.0:
        gate_score += 8; reasons.append("di atas VWAP tapi mulai extended")
    else:
        reasons.append("belum sehat terhadap VWAP")

    if vol_pace >= 1.5:
        gate_score += 15; reasons.append(f"vol pace kuat {vol_pace}x")
    elif vol_pace >= 1.0:
        gate_score += 10; reasons.append(f"vol pace cukup {vol_pace}x")
    else:
        reasons.append("vol pace belum cukup")

    if br["score"] >= 65:
        gate_score += 12; reasons.append(f"breakout chance tinggi {br['score']}")
    elif br["score"] >= 55:
        gate_score += 7; reasons.append(f"breakout chance cukup {br['score']}")

    if risk["score"] >= 70:
        gate_score += 10; reasons.append(f"risk score sehat {risk['score']}")
    elif risk["score"] >= 55:
        gate_score += 5

    if rr >= 0.8:
        gate_score += 8; reasons.append("RR cukup")
    elif rr and rr < 0.5:
        gate_score -= 10; reasons.append("RR entry atas tipis")

    if cmf >= 0.05:
        gate_score += 5; reasons.append("CMF positif")
    elif cmf < -0.10:
        gate_score -= 12; reasons.append("CMF negatif")

    if downside <= 3.5:
        gate_score += 5; reasons.append("invalidation dekat")
    elif downside > 5:
        gate_score -= 8; reasons.append("risk invalidation jauh")

    # Hard fail conditions: strict by design.
    hard_fails = []
    if ab.get("available") and active_score < 40:
        hard_fails.append("Active breakout <40")
    if vwap_dist < -0.25:
        hard_fails.append("harga di bawah VWAP")
    if cmf < -0.15:
        hard_fails.append("CMF distribusi")
    if rr and rr < 0.45:
        hard_fails.append("RR terlalu tipis")
    if rsi >= 83:
        hard_fails.append("RSI overheat")

    if hard_fails:
        decision = "FAIL"
        action = "NO ENTRY"
        gate_score = min(gate_score, 49)
        reasons = hard_fails + reasons
    elif gate_score >= 75 and ab.get("available") and active_score >= 65 and vwap_dist >= 0 and vol_pace >= 1.0 and br["score"] >= 60 and risk["score"] >= 60 and (rr == 0 or rr >= 0.65):
        decision = "ENTER"
        action = "ENTRY BOLEH, tetap bertahap"
    elif gate_score >= 50:
        decision = "WATCH"
        action = "TUNGGU KONFIRMASI /CHECK"
    else:
        decision = "FAIL"
        action = "NO ENTRY"

    return {
        "ticker": ticker,
        "decision": decision,
        "action": action,
        "gate_score": int(max(0, min(100, gate_score))),
        "breakout_score": br["score"],
        "risk_score": risk["score"],
        "active_score": int(active_score),
        "active_label": label,
        "vwap_dist": round(vwap_dist, 2),
        "vol_pace": round(vol_pace, 2) if vol_pace else None,
        "trigger": trigger,
        "price": price,
        "reasons": reasons[:6],
    }




def compute_executiongate_bandarmology_proxy(scoring: dict, real_bandar: dict | None = None) -> dict:
    """
    Proxy jejak akumulasi/distribusi khusus Execution Gate.

    Ini tidak mengklaim mengetahui identitas bandar. Input hanya berasal dari
    OHLCV, CMF, OBV, volume, posisi harga, dan broker-flow jika sudah tersedia.

    real_bandar (MBSS v2, RapidAPI integration, optional): hasil
    fetch_rapidapi_bandar_accumulation — skor akumulasi REAL dari data broker,
    bukan proxy OHLCV. Kalau ada, dipakai sebagai penyesuaian ADDITIVE di atas
    heuristik yang sudah ada (TIDAK menggantikannya) — kalau None (API down,
    kuota habis, atau memang belum dicek), perilaku fungsi ini identik persis
    dengan sebelum integrasi RapidAPI ada.
    """
    def num(value, default=0.0):
        try:
            if value is None or value == "N/A":
                return default
            return float(value)
        except Exception:
            return default

    acc = 35
    dist = 20
    acc_reasons = []
    dist_reasons = []

    cmf = num(scoring.get("cmf"))
    obv_slope = num(scoring.get("obv_slope_5_pct"))
    obv_div = scoring.get("obv_divergence", "none")
    vol_ratio = num(scoring.get("vol_ratio"))
    ret1 = num(scoring.get("ret_1d_pct"))
    ret5 = num(scoring.get("ret_5d_pct"))
    close_pos = num(scoring.get("close_pos_day"), 0.5)
    dist20 = num(scoring.get("dist_to_20d_high_pct"), 99)
    rs_ihsg = num(scoring.get("relative_strength_vs_ihsg"))
    upper_wick = num(scoring.get("upper_wick_pct"))

    ab = scoring.get("active_breakout") or {}
    vwap_dist = num(ab.get("vwap_distance_pct"), -99)
    vol_pace = num(ab.get("volume_pace_ratio"))
    active_score = num(ab.get("score"))

    # Akumulasi: money flow dan OBV menguat sebelum harga terlalu jauh.
    if cmf >= 0.15:
        acc += 15
        acc_reasons.append(f"CMF positif {cmf:.2f}")
    elif cmf >= 0.05:
        acc += 8
    elif cmf <= -0.10:
        dist += 15
        dist_reasons.append(f"CMF negatif {cmf:.2f}")

    if obv_div == "bullish_divergence":
        acc += 18
        acc_reasons.append("bullish OBV divergence")
    elif obv_div == "bearish_divergence":
        dist += 25
        dist_reasons.append("bearish OBV divergence")

    if obv_slope >= 3:
        acc += 12
        acc_reasons.append(f"OBV +{obv_slope:.1f}%")
    elif obv_slope <= -3:
        dist += 15
        dist_reasons.append(f"OBV {obv_slope:.1f}%")

    # Volume besar dengan return terbatas dapat berarti supply/distribusi.
    if 1.2 <= vol_ratio <= 2.5 and abs(ret5) <= 5:
        acc += 10
        acc_reasons.append(f"volume {vol_ratio:.1f}x tanpa harga terlalu jauh")

    if vol_ratio >= 2.5 and ret1 <= 1.0:
        dist += 18
        dist_reasons.append("volume tinggi tetapi kenaikan harga terbatas")

    if close_pos >= 0.70:
        acc += 8
    elif close_pos <= 0.30:
        dist += 12
        dist_reasons.append("penutupan dekat low harian")

    if upper_wick >= 1.5:
        dist += 10
        dist_reasons.append("upper wick tinggi")

    if dist20 <= 3 and ret1 <= 0:
        dist += 10
        dist_reasons.append("tertahan dekat high 20 hari")

    if rs_ihsg >= 1:
        acc += 6
    elif rs_ihsg <= -1:
        dist += 6

    # Konfirmasi live. Bonus kecil, karena Execution Gate tetap ditentukan
    # oleh VWAP, volume pace, trigger, dan invalidation.
    if 0 <= vwap_dist <= 2.5 and vol_pace >= 1.0:
        acc += 10
        acc_reasons.append("harga di atas VWAP dengan volume live sehat")

    if vwap_dist < -0.5:
        dist += 10
        dist_reasons.append("harga di bawah VWAP")

    if active_score >= 70:
        acc += 5

    # Gunakan broker-flow bila memang sudah tersedia di scoring.
    broker_flow_pct = num(scoring.get("net_foreign_flow_pct"))
    broker_concentration = num(scoring.get("broker_concentration_pct"))

    if broker_concentration >= 10:
        if broker_flow_pct >= 10:
            acc += 12
            acc_reasons.append("broker-flow positif terkonsentrasi")
        elif broker_flow_pct <= -10:
            dist += 18
            dist_reasons.append("broker-flow negatif terkonsentrasi")

    # Real broker-derived accumulation/distribution signal (MBSS v2, RapidAPI
    # integration — source deliberately NOT named in user-facing text, this
    # should read as a natural extension of the bot's own broker/bandar
    # vocabulary, not an external-API callout). Additive on top of the
    # OHLCV-only heuristic above, NEVER replacing it. Bounded the same way
    # as the existing broker-flow bonus just above (max +/-15) — distinct
    # from that one, though: broker-flow above is raw net-direction, this is
    # a broker-confirmed ACCUMULATION/DISTRIBUTION PATTERN specifically.
    if real_bandar:
        rb_status = str(real_bandar.get("status", "")).upper()
        rb_confidence = num(real_bandar.get("confidence"))
        if rb_confidence >= 60 and "ACC" in rb_status:
            acc += 15
            acc_reasons.append(f"pola akumulasi broker riil terkonfirmasi (confidence {rb_confidence:.0f}%)")
        elif rb_confidence >= 60 and "DIST" in rb_status:
            dist += 15
            dist_reasons.append(f"pola distribusi broker riil terkonfirmasi (confidence {rb_confidence:.0f}%)")

    acc = int(max(0, min(100, acc)))
    dist = int(max(0, min(100, dist)))

    if dist >= 75:
        phase = "DISTRIBUTION_RISK"
    elif acc >= 75 and dist < 45:
        phase = "ACCUMULATION"
    elif acc >= 65 and active_score >= 60 and dist < 55:
        phase = "EARLY_MARKUP"
    elif dist >= 55:
        phase = "DISTRIBUTION_WATCH"
    else:
        phase = "NEUTRAL"

    return {
        "accumulation_score": acc,
        "distribution_risk": dist,
        "phase": phase,
        "accumulation_reasons": acc_reasons[:3],
        "distribution_reasons": dist_reasons[:3],
        "real_bandar": real_bandar,  # passed through as-is (entry_zone/recommendation/signals) for display, or None
    }


def executiongate_decision(scoring: dict, real_bandar: dict | None = None) -> dict:
    """
    Wrapper atas Execution Gate lama:
    - Bandarmology hanya memberi bonus kecil.
    - Distribution risk tinggi memblokir ENTER.

    real_bandar (MBSS v2, RapidAPI integration, optional): diteruskan langsung
    ke compute_executiongate_bandarmology_proxy — lihat docstring-nya.
    """
    result = _executiongate_decision_original(scoring)
    bandar = compute_executiongate_bandarmology_proxy(scoring, real_bandar=real_bandar)

    result["bandar_accumulation_score"] = bandar["accumulation_score"]
    result["bandar_distribution_risk"] = bandar["distribution_risk"]
    result["bandar_phase"] = bandar["phase"]
    if bandar.get("real_bandar"):
        result["real_bandar"] = bandar["real_bandar"]

    reasons = result.setdefault("reasons", [])
    reasons.append(
        f"Bandar proxy: {bandar['phase']} "
        f"A{bandar['accumulation_score']} D{bandar['distribution_risk']}"
    )

    # Veto konservatif terhadap indikasi distribusi.
    if bandar["distribution_risk"] >= 75:
        result["decision"] = "FAIL"
        result["action"] = "NO ENTRY - DISTRIBUTION RISK"
        result["gate_score"] = min(
            float(result.get("gate_score", 0) or 0),
            49,
        )
        reasons.extend(bandar["distribution_reasons"][:2])
        return result

    # Distribution watch tidak boleh lolos sebagai ENTER.
    if (
        bandar["distribution_risk"] >= 60
        and result.get("decision") == "ENTER"
    ):
        result["decision"] = "WATCH"
        result["action"] = "WAIT RECLAIM / FLOW CONFIRMATION"
        result["gate_score"] = min(
            float(result.get("gate_score", 0) or 0),
            69,
        )
        reasons.extend(bandar["distribution_reasons"][:2])
        return result

    # Bonus dibatasi maksimum 4 poin, tidak mengubah FAIL menjadi ENTER.
    if (
        bandar["accumulation_score"] >= 75
        and bandar["distribution_risk"] < 45
        and result.get("decision") in ("ENTER", "WATCH")
    ):
        result["gate_score"] = min(
            100,
            float(result.get("gate_score", 0) or 0) + 4,
        )
        reasons.extend(bandar["accumulation_reasons"][:2])

    return result


def evaluate_executiongate_watchlist(scored_watchlist: list) -> list:
    evaluated = []
    for item in scored_watchlist:
        ticker = item.get("ticker")
        if not ticker:
            continue
        try:
            # Use 1m for final gate when possible.
            item["active_breakout"] = compute_active_breakout_score(ticker, item, prefer_1m=True)
            ctx = fetch_intraday_market_context(ticker)
            if ctx.get("available"):
                item["price"] = int(ctx.get("price") or item.get("price") or 0)
                item["intraday_high"] = ctx.get("high")
                item["intraday_low"] = ctx.get("low")
                # If active breakout unavailable, enrich with vwap snapshot as much as possible.
                if not item.get("active_breakout", {}).get("available") and ctx.get("vwap_snapshot", {}).get("available"):
                    vs = ctx["vwap_snapshot"]
                    item["active_breakout"] = {
                        "available": True,
                        "score": 0,
                        "label": "VWAP_ONLY",
                        "vwap_distance_pct": vs.get("vwap_distance_pct"),
                        "volume_pace_ratio": vs.get("volume_pace_ratio"),
                        "trigger_price": None,
                        "downside_to_invalidation_pct": 99,
                    }
            # MBSS v2 (user request, quota conservation): previously live-
            # fetched RapidAPI bandar/accumulation per ticker here (same-day
            # cached, but a single /executiongate run evaluates 12+ tickers
            # at once — confirmed the single biggest per-call-volume spend
            # in production, ~42 calls burned in the first 2 days live).
            # real_bandar=None makes executiongate_decision() fall back to
            # its OHLCV-only bandarmology proxy — the same one used before
            # this RapidAPI integration existed, zero API cost. The
            # whitelist-sweep accumulation signal (whitelist_accumulation_
            # net_pct, already computed nightly at zero extra cost) covers
            # the same "who's accumulating this" question for tickers it
            # has data on — see /hc's "AKUMULASI / PRA-BREAKOUT" section.
            decision = executiongate_decision(item, real_bandar=None)
            decision["source"] = item.get("executiongate_source", "screendaytrade")
            evaluated.append(decision)
        except Exception as e:
            evaluated.append({"ticker": ticker, "decision": "FAIL", "gate_score": 0, "action": "ERROR", "reasons": [str(e)[:80]], "source": item.get("executiongate_source", "?")})
        time.sleep(0.3)
    order = {"ENTER": 0, "WATCH": 1, "FAIL": 2}
    evaluated.sort(key=lambda r: (order.get(r.get("decision"), 9), -r.get("gate_score", 0), -r.get("breakout_score", 0)))
    return evaluated





def build_executiongate_extra_candidates(max_items=12):
    """
    Kandidat tambahan Execution Gate dari sumber yang relevan:
    - myportfolio + watchlist
    - testbrief / daily scan cache

    Tidak fetch top gainer seluruh universe.
    Return:
      {
        "portfolio_watchlist": [...],
        "testbrief": [...],
        "combined": [...],
      }
    """
    def _norm(ticker):
        t = str(ticker or "").upper().strip()
        return t if t else None

    def _dedupe(seq):
        out = []
        seen = set()
        for item in seq:
            t = _norm(item)
            if not t or t in seen:
                continue
            seen.add(t)
            out.append(t)
        return out

    portfolio_watchlist = []
    testbrief = []

    # 1. Portfolio: holdings + watchlist
    try:
        pf = load_portfolio()

        positions = pf.get("positions") or pf.get("holdings") or {}
        if isinstance(positions, dict):
            portfolio_watchlist.extend(positions.keys())
        elif isinstance(positions, list):
            for item in positions:
                if isinstance(item, str):
                    portfolio_watchlist.append(item)
                elif isinstance(item, dict):
                    portfolio_watchlist.append(item.get("ticker") or item.get("code"))

        portfolio_watchlist.extend(pf.get("watchlist", []) or [])
    except Exception as e:
        print(f"⚠️ executiongate portfolio/watchlist gagal: {str(e)[:80]}")

    # 2. Testbrief / daily scan cache
    try:
        daily = nightly_engine.load_daily_scan_cache()
        rows = []

        if isinstance(daily, dict):
            rows = list(daily.values())
        elif isinstance(daily, list):
            rows = daily

        def get_score(r):
            if not isinstance(r, dict):
                return 0
            return float(
                r.get("final_score")
                or r.get("score")
                or r.get("radar_score")
                or r.get("v5_total")
                or 0
            )

        rows = sorted(rows, key=get_score, reverse=True)

        for r in rows[:max_items]:
            if isinstance(r, str):
                testbrief.append(r)
            elif isinstance(r, dict):
                testbrief.append(r.get("ticker") or r.get("code"))
    except Exception as e:
        print(f"⚠️ executiongate testbrief gagal: {str(e)[:80]}")

    portfolio_watchlist = _dedupe(portfolio_watchlist)[:max_items]
    testbrief = _dedupe(testbrief)[:max_items]

    combined = _dedupe(portfolio_watchlist + testbrief)[:max_items]

    print(
        f"📋 ExecutionGate sources: portfolio/watchlist={len(portfolio_watchlist)}, "
        f"testbrief={len(testbrief)}, combined={len(combined)}"
    )

    return {
        "portfolio_watchlist": portfolio_watchlist,
        "testbrief": testbrief,
        "combined": combined,
    }
# NOTE (MBSS v2 refactor, Phase 5a): executiongate_command() moved to
# commands/scan.py (Command Layer). Registered via `commands_scan.xxx` in
# build_app() below.

DAYTRADE_FINAL_PICKS_COUNT = 12  # executiongate needs wider radar: 12 screendaytrade autopicks
DAYTRADE_MIN_RR_FOR_PRIORITY = 1.0  # RR@max minimal dianggap "sehat" untuk prioritas kandidat


def filter_and_rank_daytrade_candidates(results, count=DAYTRADE_FINAL_PICKS_COUNT):
    """
    Filter + ranking day trade — fallback bertingkat supaya tidak pernah kosong,
    tapi tetap transparan kriteria mana yang dipakai.

    Prioritas (tier tertinggi yang punya >= count kandidat yang dipakai):
    - Tier 1a: HIGH CONVICTION + BUY/STRONG_BUY + RR >= 1.0 (ideal sempurna)
    - Tier 1b: BUY/STRONG_BUY + RR >= 1.0 (tanpa high conviction)
    - Tier 2:  BUY/STRONG_BUY saja (RR bervariasi)
    - Tier 3:  fallback semua (termasuk MIXED_SIGNALS)
    """
    BUY_ACTIONS = ("STRONG_BUY", "BUY_ACCUMULATE")

    def has_rr(r):
        rr = r.get("targets", {}).get("risk_reward_at_max")
        return rr is not None and rr >= DAYTRADE_MIN_RR_FOR_PRIORITY

    def is_high_conviction(r):
        return r.get("high_conviction", {}).get("is_high_conviction", False)

    tier1a = [r for r in results if r.get("action_id") in BUY_ACTIONS and has_rr(r) and is_high_conviction(r)]
    if len(tier1a) >= count:
        ranked = sorted(tier1a, key=compute_daytrade_score, reverse=True)
        return ranked[:count], "🔥 High Conviction + beli/akumulasi + RR sehat"

    tier1b = [r for r in results if r.get("action_id") in BUY_ACTIONS and has_rr(r)]
    if len(tier1b) >= count:
        ranked = sorted(tier1b, key=compute_daytrade_score, reverse=True)
        return ranked[:count], "sinyal beli/akumulasi + RR sehat (kandidat high conviction kurang dari cukup hari ini)"

    tier2 = [r for r in results if r.get("action_id") in BUY_ACTIONS]
    if len(tier2) >= count:
        ranked = sorted(tier2, key=compute_daytrade_score, reverse=True)
        return ranked[:count], "sinyal beli/akumulasi (RR bervariasi)"

    ranked = sorted(results, key=compute_daytrade_score, reverse=True)
    return ranked[:count], "⚠️ sinyal beli/akumulasi terbatas hari ini, termasuk sinyal campuran"


def _v5_float(x, default=0.0):
    try:
        if x is None or x == "N/A": return default
        return float(x)
    except Exception:
        return default


def compute_entry_room_score_v5(scoring: dict) -> dict:
    """Score whether there is still a healthy entry room instead of chasing near high/TP."""
    price = _v5_float(scoring.get("price"), 0)
    high = _v5_float(scoring.get("intraday_high"), 0) or price
    low = _v5_float(scoring.get("intraday_low"), 0) or price
    rr = _v5_float((scoring.get("targets") or {}).get("risk_reward_at_max"), 0)
    tp1 = _v5_float((scoring.get("targets") or {}).get("tp_1"), 0)
    rsi = _v5_float(scoring.get("rsi"), 0)
    cmf = _v5_float(scoring.get("cmf"), 0)
    vol_ratio = _v5_float(scoring.get("vol_ratio"), 0)
    range10 = _v5_float(scoring.get("day_range_pct_10d"), 0)
    dist_high_pct = ((high - price) / max(price, 1e-9) * 100) if high and price else 0
    upside_tp1_pct = ((tp1 - price) / max(price, 1e-9) * 100) if tp1 and price else 0
    close_pos = ((price - low) / max(high - low, 1e-9)) if high and low and high > low else 0.5

    score = 50
    reasons = []
    if rr >= 1.0: score += 18; reasons.append("RR sehat")
    elif rr >= 0.8: score += 12; reasons.append("RR cukup")
    elif rr >= 0.6: score += 4; reasons.append("RR tipis")
    elif rr > 0: score -= 15; reasons.append("RR entry atas buruk")

    if upside_tp1_pct >= 6: score += 14; reasons.append("ruang ke TP1 cukup")
    elif upside_tp1_pct >= 4: score += 8; reasons.append("ruang ke TP1 sedang")
    elif upside_tp1_pct > 0: score -= 8; reasons.append("TP1 terlalu dekat")

    if 1.0 <= dist_high_pct <= 3.5: score += 12; reasons.append("belum terlalu dekat high")
    elif 0.3 <= dist_high_pct < 1.0: score += 3; reasons.append("dekat high")
    elif dist_high_pct < 0.3 and close_pos > 0.85: score -= 10; reasons.append("rawan chase dekat high")
    elif dist_high_pct > 5.0: score -= 4; reasons.append("masih jauh dari high, tunggu trigger")

    if rsi >= 82: score -= 15; reasons.append("RSI overheat")
    elif rsi >= 75: score -= 7; reasons.append("RSI panas")
    if cmf < -0.10: score -= 10; reasons.append("CMF negatif")
    elif cmf >= 0.10: score += 5; reasons.append("CMF positif")
    if range10 > 40: score -= 6; reasons.append("range sangat liar")
    if vol_ratio < 0.7: score -= 8; reasons.append("volume belum hidup")

    score = int(max(0, min(100, score)))
    if score >= 70: label = "ROOM BAGUS"
    elif score >= 55: label = "ROOM CUKUP"
    elif score >= 40: label = "ROOM SEMPIT"
    else: label = "CHASE RISK"
    return {"score": score, "label": label, "dist_high_pct": round(dist_high_pct, 2), "upside_tp1_pct": round(upside_tp1_pct, 2), "reasons": reasons[:4]}


def compute_continuation_chance_v5(scoring: dict) -> dict:
    """Next-day breakout continuation chance for stocks that already moved/broke out."""
    breakout = _score_breakout_chance_v4(scoring)
    risk = _score_breakout_drop_risk_v4(scoring)
    price = _v5_float(scoring.get("price"), 0)
    high = _v5_float(scoring.get("intraday_high"), 0) or price
    low = _v5_float(scoring.get("intraday_low"), 0) or price
    close_pos = ((price - low) / max(high - low, 1e-9)) if high and low and high > low else 0.5
    vol_ratio = _v5_float(scoring.get("vol_ratio"), 0)
    cmf = _v5_float(scoring.get("cmf"), 0)
    adx = _v5_float(scoring.get("adx"), 0)
    rsi = _v5_float(scoring.get("rsi"), 0)
    rs = _v5_float(scoring.get("relative_strength_vs_ihsg"), 0)
    macd_ok = scoring.get("macd_state") == "bullish" or scoring.get("macd_cross_direction") == "bullish"
    ret1 = _v5_float(scoring.get("ret_1d_pct"), 0)

    score = 0
    reasons = []
    if close_pos >= 0.75: score += 18; reasons.append("close dekat high")
    elif close_pos >= 0.50: score += 10; reasons.append("close cukup kuat")
    else: score -= 8; reasons.append("close kurang kuat")
    if vol_ratio >= 1.5: score += 16; reasons.append("volume confirm")
    elif vol_ratio >= 1.0: score += 9; reasons.append("volume cukup")
    if cmf >= 0.10: score += 14; reasons.append("CMF akumulasi")
    elif cmf >= -0.05: score += 6
    else: score -= 10; reasons.append("CMF negatif")
    if macd_ok: score += 10; reasons.append("MACD bullish")
    if adx >= 25: score += 10; reasons.append("trend kuat")
    elif adx >= 20: score += 5
    if rs >= 2: score += 8; reasons.append("RS unggul")
    elif rs < -1: score -= 6; reasons.append("RS lemah")
    score += int(breakout["score"] * 0.12)
    score += int(risk["score"] * 0.10)
    if rsi >= 83: score -= 12; reasons.append("RSI overheat")
    if ret1 >= 10: score -= 8; reasons.append("sudah naik tinggi")
    score = int(max(0, min(100, score)))
    if score >= 70: label = "LANJUT KUAT"
    elif score >= 55: label = "LANJUT MUNGKIN"
    elif score >= 40: label = "BUTUH VALIDASI"
    else: label = "RAWAN FADE"
    return {"score": score, "label": label, "close_pos": round(close_pos, 2), "reasons": reasons[:4]}


def compute_activity_score_v5(scoring: dict) -> dict:
    """Liquidity/activity score so /screendaytrade avoids quiet stocks for daytrade."""
    price = _v5_float(scoring.get("price"), 0)
    value_traded = _v5_float(scoring.get("value_traded"), 0)
    if value_traded <= 0:
        # fallback if value_traded was not stored
        vol = _v5_float(scoring.get("volume"), 0)
        value_traded = price * vol
    vol_ratio = _v5_float(scoring.get("vol_ratio"), 0)
    range10 = _v5_float(scoring.get("day_range_pct_10d"), 0)
    intraday_range = _v5_float(scoring.get("intraday_range_pct"), 0)
    ret1 = _v5_float(scoring.get("ret_1d_pct"), 0)
    reasons = []
    score = 0

    # Value traded is the most important proxy because yfinance does not provide IDX orderbook/frequency.
    if value_traded >= 100_000_000_000:
        score += 45; reasons.append("value sangat besar")
    elif value_traded >= 25_000_000_000:
        score += 38; reasons.append("value besar")
    elif value_traded >= 10_000_000_000:
        score += 30; reasons.append("value aktif")
    elif value_traded >= 5_000_000_000:
        score += 22; reasons.append("value cukup")
    elif value_traded >= 3_000_000_000:
        score += 14; reasons.append("value minimum")
    else:
        score += 2; reasons.append("value tipis")

    if vol_ratio >= 3.0:
        score += 22; reasons.append("volume spike")
    elif vol_ratio >= 1.8:
        score += 17; reasons.append("volume hidup")
    elif vol_ratio >= 1.2:
        score += 12; reasons.append("volume cukup hidup")
    elif vol_ratio >= 0.8:
        score += 6; reasons.append("volume biasa")
    else:
        reasons.append("volume relatif lemah")

    active_range = max(intraday_range, range10 / 3 if range10 else 0)
    if active_range >= 5:
        score += 18; reasons.append("range aktif")
    elif active_range >= 3:
        score += 12; reasons.append("range cukup")
    elif active_range >= 1.5:
        score += 6
    else:
        reasons.append("range sempit")

    if abs(ret1) >= 5:
        score += 10; reasons.append("sedang bergerak")
    elif abs(ret1) >= 2:
        score += 5

    # Hard penalty for quiet stocks, even if RR/room looks good.
    if value_traded < 3_000_000_000:
        score = min(score, 34)
    elif value_traded < 5_000_000_000:
        score = min(score, 49)

    score = int(max(0, min(100, score)))
    if score >= 75: label = "SANGAT AKTIF"
    elif score >= 60: label = "AKTIF"
    elif score >= 45: label = "CUKUP AKTIF"
    elif score >= 35: label = "TIPIS"
    else: label = "SEPI"
    return {"score": score, "label": label, "value_traded": int(value_traded), "reasons": reasons[:4]}


def compute_volume_breakout_quality_v5(scoring: dict) -> dict:
    """Daily volume breakout quality: volume expansion, value, acceleration, close strength, and climax penalty."""
    price = _v5_float(scoring.get("price"), 0)
    volume = _v5_float(scoring.get("volume"), 0)
    value_traded = _v5_float(scoring.get("value_traded"), 0)
    if value_traded <= 0:
        value_traded = price * volume
    vol_ratio = _v5_float(scoring.get("vol_ratio"), 0)
    ret1 = _v5_float(scoring.get("ret_1d_pct"), 0)
    high = _v5_float(scoring.get("intraday_high"), 0) or _v5_float(scoring.get("latest_daily_high"), 0) or price
    low = _v5_float(scoring.get("intraday_low"), 0) or _v5_float(scoring.get("latest_daily_low"), 0) or price
    close_pos = _v5_float(scoring.get("close_pos_day"), -1)
    if close_pos < 0:
        close_pos = ((price - low) / max(high - low, 1e-9)) if high and low and high > low else 0.5
    range10 = _v5_float(scoring.get("day_range_pct_10d"), 0)
    reasons = []
    score = 0

    # 1) Volume expansion versus recent average.
    if vol_ratio >= 5.0:
        score += 26; reasons.append("volume explosion")
    elif vol_ratio >= 3.0:
        score += 22; reasons.append("volume breakout")
    elif vol_ratio >= 1.8:
        score += 16; reasons.append("volume expansion")
    elif vol_ratio >= 1.2:
        score += 9; reasons.append("volume mulai hidup")
    else:
        reasons.append("volume belum ekspansi")

    # 2) Value traded keeps low-liquidity movers from dominating.
    if value_traded >= 100_000_000_000:
        score += 24; reasons.append("value sangat besar")
    elif value_traded >= 25_000_000_000:
        score += 20; reasons.append("value besar")
    elif value_traded >= 10_000_000_000:
        score += 15; reasons.append("value aktif")
    elif value_traded >= 5_000_000_000:
        score += 9; reasons.append("value cukup")
    elif value_traded >= 3_000_000_000:
        score += 4
    else:
        score -= 12; reasons.append("value tipis")

    # 3) Close quality: volume is only bullish if price closes strong.
    if close_pos >= 0.85:
        score += 18; reasons.append("close sangat dekat high")
    elif close_pos >= 0.70:
        score += 13; reasons.append("close dekat high")
    elif close_pos >= 0.50:
        score += 5; reasons.append("close cukup")
    else:
        score -= 12; reasons.append("fade dari high")

    # 4) Range expansion. Breakouts usually expand range from previous compression.
    if range10 >= 18:
        score += 10; reasons.append("range ekspansif")
    elif range10 >= 10:
        score += 6

    # 5) Volume climax/chase penalty. Keep showing the name, but mark risk.
    if ret1 >= 20 and vol_ratio >= 5:
        score -= 12; reasons.append("climax risk")
    elif ret1 >= 12 and vol_ratio >= 4:
        score -= 7; reasons.append("euforia, jangan chase")

    # Hard cap if value is too thin.
    if value_traded < 3_000_000_000:
        score = min(score, 35)
    elif value_traded < 5_000_000_000:
        score = min(score, 50)

    score = int(max(0, min(100, score)))
    if score >= 75: label = "VOL BREAKOUT KUAT"
    elif score >= 60: label = "VOL BREAKOUT"
    elif score >= 45: label = "VOL CUKUP"
    elif score >= 30: label = "VOL LEMAH"
    else: label = "VOL TIDAK VALID"
    return {"score": score, "label": label, "value_traded": int(value_traded), "close_pos": round(close_pos, 2), "reasons": reasons[:5]}


def compute_daytrade_v5_summary(scoring: dict) -> dict:
    br = _score_breakout_chance_v4(scoring)
    risk = _score_breakout_drop_risk_v4(scoring)
    room = compute_entry_room_score_v5(scoring)
    cont = compute_continuation_chance_v5(scoring)
    activity = compute_activity_score_v5(scoring)
    volq = compute_volume_breakout_quality_v5(scoring)

    # Strict V5 formula #2: lower breakout weight, prioritize live tradability and volume quality.
    total = int(round(
        br["score"] * 0.20 +
        cont["score"] * 0.20 +
        activity["score"] * 0.25 +
        volq["score"] * 0.15 +
        room["score"] * 0.10 +
        risk["score"] * 0.10
    ))
    total = max(0, min(100, total))

    ret1 = _v5_float(scoring.get("ret_1d_pct"), 0)
    close_pos = _v5_float(scoring.get("close_pos_day"), 0.5)
    value_traded = activity["value_traded"]

    # Radar labels, not live entry signals.
    is_closing_momentum = ret1 >= 5 and close_pos >= 0.70 and value_traded >= 10_000_000_000 and activity["score"] >= 60 and volq["score"] >= 55
    if activity["score"] < 35:
        label = "LOW LIQUIDITY"
    elif is_closing_momentum and cont["score"] >= 55 and room["score"] < 45:
        label = "CONTINUATION / CHASE RISK"
    elif is_closing_momentum and cont["score"] >= 55:
        label = "PRIORITY CONTINUATION WATCH"
    elif total >= 72 and room["score"] >= 55 and risk["score"] >= 55 and activity["score"] >= 55:
        label = "PRIORITY WATCH"
    elif br["score"] >= 70 and room["score"] < 45:
        label = "CHASE RISK"
    elif room["score"] < 40:
        label = "PULLBACK WATCH"
    elif cont["score"] >= 65 and activity["score"] >= 45:
        label = "CONTINUATION WATCH"
    elif br["score"] >= 55 and activity["score"] >= 45:
        label = "WATCH"
    elif activity["score"] < 45:
        label = "LOW LIQUIDITY"
    else:
        label = "LOW PRIORITY"

    price = _v5_float(scoring.get("price"), 0)
    high = _v5_float(scoring.get("intraday_high"), 0) or _v5_float(scoring.get("latest_daily_high"), 0) or price
    valid = high if high and high > price else _v5_float((scoring.get("targets") or {}).get("tp_1"), 0)
    buy_range = (scoring.get("targets") or {}).get("buy_range", "-")
    invalid = (scoring.get("targets") or {}).get("cut_loss", "-")

    if label == "LOW LIQUIDITY":
        note = "value/aktivitas tipis; tidak prioritas daytrade"
    elif "CHASE RISK" in label:
        note = "momentum menarik tapi rawan chase; tunggu pullback/reclaim live"
    elif label == "PULLBACK WATCH":
        note = "setup ada, entry room sempit; tunggu area ideal"
    elif label == "PRIORITY WATCH":
        note = "radar utama; entry tetap menunggu executiongate"
    elif "CONTINUATION" in label:
        note = "peluang lanjut ada; validasi gap/VWAP besok"
    else:
        note = "validasi live dulu"
    return {
        "total": total,
        "label": label,
        "breakout": br,
        "risk": risk,
        "room": room,
        "continuation": cont,
        "activity": activity,
        "volq": volq,
        "valid_level": smart_round_price(valid) if valid else "-",
        "ideal": buy_range,
        "invalid": invalid,
        "note": note,
        "is_closing_momentum": is_closing_momentum,
    }


def select_screendaytrade_v5_candidates(results: list, count: int = DAYTRADE_FINAL_PICKS_COUNT) -> tuple:
    """Candidate selection V5: setup lane + top-closing-momentum lane + activity/liquidity gate."""
    rows = []
    for r in results:
        try:
            v5 = compute_daytrade_v5_summary(r)
            rows.append((r, v5))
        except Exception as e:
            print(f"⚠️ V5 summary failed for {r.get('ticker','?')}: {e}")

    def cont_key(rv):
        r, v = rv
        return (
            v["is_closing_momentum"],
            v["continuation"]["score"] * 0.25 + v["activity"]["score"] * 0.30 + v["volq"]["score"] * 0.30 + v["breakout"]["score"] * 0.10 + v["risk"]["score"] * 0.05,
            v["total"],
        )

    # Lane A: active overnight/top closing momentum candidates, e.g. BACH/DWGL/MLPT style.
    lane_cont = [rv for rv in rows if rv[1]["is_closing_momentum"]]
    lane_cont = sorted(lane_cont, key=cont_key, reverse=True)

    # Lane B: normal setup candidates, with minimum activity so quiet names like TEBE don't dominate.
    lane_setup = [rv for rv in rows if rv[1]["activity"]["score"] >= 45 and rv[1]["breakout"]["score"] >= 45]
    lane_setup = sorted(lane_setup, key=lambda rv: rv[1]["total"], reverse=True)

    # Lane C: fallback good risk/room but not enough activity, shown only if needed.
    lane_fallback = sorted(rows, key=lambda rv: rv[1]["total"], reverse=True)

    picked = []
    seen = set()
    # Reserve up to half list for real active closing momentum.
    for r, v in lane_cont[:max(4, count // 2)]:
        if r.get("ticker") not in seen:
            picked.append(r); seen.add(r.get("ticker"))
    for r, v in lane_setup:
        if len(picked) >= count: break
        if r.get("ticker") not in seen:
            picked.append(r); seen.add(r.get("ticker"))
    for r, v in lane_fallback:
        if len(picked) >= count: break
        if r.get("ticker") not in seen:
            picked.append(r); seen.add(r.get("ticker"))

    note = "V5: activity/liquidity gate + closing momentum lane + breakout setup lane"
    return picked[:count], note




FAST_CANDIDATE_FORMULA_VERSION = "2.1"  # v2.1: tambah hard-reject action_id==AVOID_SELL (real case: RAJA di-tag FAST lalu keesokan harinya HINDARI/JUAL + gagal Danger Gate, formula lama tidak pernah cek action_id). v2.0: relaks kriteria kecepatan awal (vol_ratio>=1.5x & day_range>=15%, turun dari 2.0x/20%) + WAJIB sinyal "dijaga bandar" (Bias Bandar AKUMULASI SEGAR/PULLBACK DIDUKUNG + >=2 broker whitelist). Real case pemicu (user report): YELO lolos kriteria v1.0 murni (vol_ratio+day_range) TAPI gagal naik hari itu; BAIK sebaliknya nunjukkin karakter "dijaga bandar, readable, ada bantalan support" yang v1.0 sama sekali tidak menangkap. v1.0: vol_ratio>=2.0x & day_range_10d>=20% murni dari riset speed-to-move (n=92 fast vs n=349 slow) — statistik itu TIDAK otomatis berlaku lagi buat kriteria v2.0 ini (kombinasi baru, belum ada data forward-nya), jangan dikutip ulang sampai ada evaluasi baru.

def compute_fast_candidate_tag(r: dict) -> dict:
    """
    MBSS v2 (user request, revisi v2.0 — lihat FAST_CANDIDATE_FORMULA_VERSION
    di atas untuk alasan lengkap): tag EOD murni INFORMASIONAL, belum
    menggantikan/menggating skor apa pun — sama disiplin dengan
    bollinger_squeeze (lacak dulu di feature_snapshot, validasi forward,
    baru dipertimbangkan jadi filter/skor kalau prospectively terbukti).
    LEBIH provisional dari v1.0 — v1.0 setidaknya lahir dari data /winrate
    real, v2.0 ini kombinasi baru (speed direlaks + defended signal) yang
    BELUM ADA bukti forward sama sekali, murni hipotesis dari 2 observasi
    manual (YELO gagal, BAIK berhasil).

    Dua syarat, KEDUANYA wajib:
    1. Speed (direlaks dari v1.0): vol_ratio >= 1.5x DAN day_range_pct_10d
       >= 15%.
    2. Defended/dijaga bandar (BARU): bias_bandar di {AKUMULASI SEGAR,
       PULLBACK DIDUKUNG} (klasifikasi day-over-day whitelist net-buy yang
       SUDAH ada, classify_bias_bandar di engine/broker.py — "PULLBACK
       DIDUKUNG" secara harfiah berarti dip-nya dibeli/dijaga whitelist
       broker) DAN whitelist_num_brokers >= 2 (bukan cuma 1 desk).

    CATATAN ARSITEKTUR: bagian "readable secara live" (VWAP bertahan/
    memantul di 15m/30m/60m walau naik-turun) SENGAJA belum diikutkan di
    sini — itu cuma bisa dicek pakai data live (vwap_movement), yang belum
    ada saat /eodscan jalan sebelum market buka. Itu jadi lapisan
    konfirmasi TAMBAHAN di sisi live (/check, /consensus live), bukan
    bagian dari tag EOD ini.

    Tag ini juga dasar "alert entry secepatnya di OPEN" (user request) —
    kalau EOD sudah menandai kandidat sebagai fast_candidate, user
    diberitahu supaya TIDAK menunggu window konfirmasi tactical 30-40 menit
    (lihat classify_signal_validity's UNKNOWN gate) yang justru bisa
    membuat entry ketinggalan (real case: TEBE, SIPD).
    """
    vol_ratio = r.get("vol_ratio")
    day_range = r.get("day_range_pct_10d")
    speed_ok = (
        vol_ratio is not None and vol_ratio >= 1.5
        and day_range is not None and day_range >= 15.0
    )

    bias_label = r.get("bias_bandar")
    defended_ok = (
        bias_label in ("AKUMULASI SEGAR", "PULLBACK DIDUKUNG")
        and (r.get("whitelist_num_brokers") or 0) >= 2
    )

    # BUGFIX (user report, real case: RAJA di-tag FAST CANDIDATE lalu
    # keesokan harinya action_id-nya HINDARI/JUAL + gagal Danger Gate —
    # fungsi ini TIDAK PERNAH cek action_id sama sekali sebelumnya).
    # Reject keras kalau core blend SUDAH bilang avoid/sell — jangan
    # kasih alert "prioritas entry di open" utk ticker begini.
    not_avoid_sell = r.get("action_id") != "AVOID_SELL"

    is_fast = speed_ok and defended_ok and not_avoid_sell
    reason = None
    if is_fast:
        reason = f"vol_ratio>={vol_ratio}x & day_range>={day_range}% (speed) + {bias_label} {r.get('whitelist_num_brokers')} broker (defended)"
    return {
        "is_fast_candidate": is_fast,
        "speed_ok": speed_ok,
        "defended_ok": defended_ok,
        "not_avoid_sell": not_avoid_sell,
        "reason": reason,
    }


def format_fast_candidate_tag(r: dict, prefix: str = "\n   ") -> str:
    """Display helper dipakai /hc, /screendaytrade, /consensus — lihat compute_fast_candidate_tag."""
    if not compute_fast_candidate_tag(r).get("is_fast_candidate"):
        return ""
    return f"{prefix}🚀 FAST CANDIDATE — prioritas entry saat OPEN besok, jangan tunggu konfirmasi tactical 30-40 menit (kriteria v2.0: speed + dijaga bandar, belum ada data forward — lihat FAST_CANDIDATE_FORMULA_VERSION)"


def compute_screendaytrade_positive_bias(r: dict) -> dict:
    """
    Refactor ranking /screendaytrade:
    Pisahkan Fresh Breakout lane dan Continuation lane.
    Tujuan: memperkuat probabilitas saham positif, bukan sekadar volatil.

    MBSS v2 (RapidAPI integration, "diskusi trader" session, user request):
    tiga tambahan, semua ADDITIVE terhadap lane lama, tidak menggantikan:
    1. Lane baru "PRIORITY ACCUMULATION" — saham yang whitelist broker-nya
       net-buy KUAT tapi harga BELUM bergerak (breakout score masih rendah).
       Ini justru kandidat paling berharga karena ketemu SEBELUM ramai,
       bukan setelah — tapi risikonya juga nyata (belum ada konfirmasi
       harga), jadi priority-nya sengaja DI BAWAH FRESH/CONT (priority 2,
       bukan 3), dan cuma lane baru ini yang sanggup nunjukkan tanpa
       terjebak filter breakout/momentum lane lain (yang secara struktural
       akan selalu menolak saham dengan breakout score rendah).
    2. Smart-money divergence — harga masih naik kuat (breakout score
       tinggi) TAPI whitelist broker justru NET JUAL. Sinyal risiko ini
       TIDAK BISA dideteksi dari OHLCV/orderbook manapun (butuh identitas
       broker riil), ditambahkan sebagai pemicu independen ke lane
       "EXTENDED / CHASE WATCH" yang sudah ada, bukan pengganti heuristik
       CHASE lama — kalau keduanya kompak (CHASE teknikal + divergence),
       penalti keduanya diakumulasi, sinyal makin kuat.
    3. Konfirmasi breakout riil dari RapidAPI (severity HIGH + probability
       tinggi) — bonus kecil tambahan di atas active_score yang sudah ada,
       validasi silang breakout OHLCV kita dengan level support/resistance
       broker riil, bukan penentu utama.
    """
    v5 = compute_daytrade_v5_summary(r)

    br = v5.get("breakout", {})
    cont = v5.get("continuation", {})
    act = v5.get("activity", {})
    volq = v5.get("volq", {})
    room = v5.get("room", {})
    safety = v5.get("risk", {})  # risk score di formula lama sebenarnya drop safety

    b = float(br.get("score", 0) or 0)
    c = float(cont.get("score", 0) or 0)
    a = float(act.get("score", 0) or 0)
    v = float(volq.get("score", 0) or 0)
    rm = float(room.get("score", 0) or 0)
    sf = float(safety.get("score", 0) or 0)

    upside = float(room.get("upside_tp1_pct", 0) or 0)

    ab = r.get("active_breakout", {}) or {}
    active_score = float(ab.get("score", 0) or 0)

    # Lane A: Fresh Breakout, target BULL/ERAA style.
    fresh_score = (
        0.30 * b +
        0.25 * rm +
        0.20 * a +
        0.15 * sf +
        0.10 * v
    )

    # Lane B: Strong Continuation, target KOTA style.
    continuation_score = (
        0.35 * c +
        0.25 * a +
        0.20 * b +
        0.10 * sf +
        0.10 * v
    )

    fresh_ok = (
        b >= 68 and
        rm >= 65 and
        a >= 50 and
        sf >= 65 and
        upside >= 7
    )

    continuation_ok = (
        c >= 72 and
        a >= 75 and
        b >= 75 and
        sf >= 75
    )

    # Sinyal whitelist accumulation/distribution — sudah dihitung SEKALI di
    # batch malam (apply_whitelist_accumulation_adjustment, engine/scoring.py)
    # dan disimpan langsung di scoring dict, jadi di sini TINGGAL BACA, tidak
    # fetch/hitung ulang apa pun.
    whitelist_net_pct = r.get("whitelist_accumulation_net_pct")
    whitelist_num_brokers = r.get("whitelist_num_brokers", 0) or 0
    strong_accumulation = (
        whitelist_net_pct is not None and whitelist_net_pct >= 15 and whitelist_num_brokers >= 2
    )

    if fresh_ok and fresh_score >= continuation_score:
        lane = "PRIORITY FRESH"
        score = fresh_score + 6
        priority = 3
    elif continuation_ok:
        lane = "PRIORITY CONT"
        score = continuation_score + 6
        priority = 3
    elif strong_accumulation and b < 50:
        accumulation_strength = min(1.0, (whitelist_net_pct - 15) / 35)  # 0 at 15%, 1.0 at 50%+
        broker_agreement = min(1.0, whitelist_num_brokers / 4)
        lane = "PRIORITY ACCUMULATION"
        score = 50 + 15 * accumulation_strength + 10 * broker_agreement + 5 * (rm / 100)
        priority = 2
    elif rm < 55 and a < 75:
        lane = "LOW EDGE / CHASE"
        score = max(fresh_score, continuation_score) - 8
        priority = 1
    else:
        # MBSS v2 (user request, berbasis data /winrate real — 162 pick
        # selesai): SECONDARY WATCH sebelumnya priority 2 (tengah, di ATAS
        # LOW EDGE/CHASE) — tapi data winrate nyata menunjukkan lane ini
        # justru performa TERBURUK dari semua lane (38% win, avg -1.2%/pick,
        # n=13, sampel cukup dipercaya) — bahkan lebih jelek dari LOW
        # EDGE/CHASE (67% win, +1.1%/pick) yang justru didesain sebagai
        # tier "risiko chase" terendah. Urutan lama tidak sinkron dengan
        # realita — diturunkan ke priority TERENDAH + penalti skor eksplisit.
        lane = "SECONDARY WATCH"
        score = max(fresh_score, continuation_score) - 10
        priority = 1

    # Live active breakout tetap bonus kecil, bukan penentu utama.
    if active_score >= 60:
        score += min((active_score - 60) / 4, 8)

    # MBSS v2 (RapidAPI integration): konfirmasi breakout riil dari RapidAPI
    # — validasi silang breakout OHLCV kita dengan level support/resistance
    # broker riil. Bonus kecil, bukan penentu utama, cuma untuk breakout
    # yang SUDAH kuat secara teknikal (b >= 50) — bukan dipakai untuk
    # PRIORITY ACCUMULATION di atas (itu justru butuh breakout BELUM terjadi).
    if b >= 50:
        try:
            alert = nightly_engine.get_breakout_alert_for_ticker(r.get("ticker", ""))
        except Exception:
            alert = None
        if alert and str(alert.get("severity", "")).upper() == "HIGH" and (alert.get("indicators", {}) or {}).get("breakout_probability", 0) >= 80:
            score += 4

    # Chase risk dari label lama tetap diberi penalti.
    old_label = str(v5.get("label", "")).upper()
    technical_chase = "CHASE" in old_label
    # Smart-money divergence: harga sudah bergerak kuat TAPI whitelist
    # broker net JUAL — sinyal risiko independen dari heuristik CHASE lama.
    smart_money_divergence = b >= 60 and whitelist_net_pct is not None and whitelist_net_pct <= -15

    if technical_chase or smart_money_divergence:
        if technical_chase:
            score -= 10
        if smart_money_divergence:
            score -= 12
        if lane != "LOW EDGE / CHASE":
            lane = "EXTENDED / CHASE WATCH"
            priority = min(priority, 1)

    # MBSS v2 (RapidAPI integration, user request — "pelajari secara
    # adaptif untuk hindari picks yang cenderung gagal"): lapisan TAMBAHAN
    # di atas penalti manual yang sudah ada (SECONDARY WATCH -10, LOW
    # EDGE/CHASE -8 di atas) — additive, TIDAK menggantikan tuning yang
    # sudah divalidasi. Terapkan ke lane FINAL (setelah kemungkinan
    # direklasifikasi jadi EXTENDED/CHASE WATCH di atas), supaya lane BARU
    # (PRIORITY FRESH/CONT/ACCUMULATION/EXTENDED CHASE WATCH — yang
    # sebelumnya TIDAK PUNYA penalti berbasis winrate sama sekali) ikut
    # terlindungi, sekaligus terus ter-update otomatis seiring data
    # /winrate bertambah, bukan angka statis yang perlu diedit manual lagi.
    score += get_adaptive_lane_penalty(lane)

    score = max(0, min(100, round(score, 1)))

    return {
        "score": score,
        "lane": lane,
        "priority": priority,
        "fresh_score": round(fresh_score, 1),
        "continuation_score": round(continuation_score, 1),
    }


def rank_screendaytrade_refactor(candidates, count):
    """
    Ranking final /screendaytrade berbasis Positive Bias.
    Tidak menambah formula besar baru, hanya membersihkan lane:
    Fresh Breakout vs Continuation.
    """
    enriched = []

    for r in candidates:
        try:
            bias = compute_screendaytrade_positive_bias(r)
            r["_positive_bias"] = bias["score"]
            r["_positive_lane"] = bias["lane"]
            r["_positive_priority"] = bias["priority"]
            r["_fresh_score"] = bias["fresh_score"]
            r["_continuation_lane_score"] = bias["continuation_score"]
        except Exception as e:
            print(f"⚠️ Positive bias gagal untuk {r.get('ticker')}: {e}")
            r["_positive_bias"] = 0
            r["_positive_lane"] = "UNRANKED"
            r["_positive_priority"] = 0
            r["_fresh_score"] = 0
            r["_continuation_lane_score"] = 0

        enriched.append(r)

    enriched.sort(
        key=lambda x: (
            x.get("_positive_priority", 0),
            x.get("_positive_bias", 0),
            x.get("active_breakout", {}).get("score", 0),
        ),
        reverse=True,
    )

    return enriched[:count]


def save_latest_screendaytrade_picks(top_candidates):
    """
    Cache ringan untuk executiongate whitelist.
    """
    try:
        path = os.path.join(PROJECT_ROOT, "latest_screendaytrade_picks.json")
        rows = []
        for r in top_candidates:
            rows.append({
                "ticker": r.get("ticker"),
                "positive_bias_score": r.get("_positive_bias"),
                "positive_lane": r.get("_positive_lane"),
                "fresh_score": r.get("_fresh_score"),
                "continuation_lane_score": r.get("_continuation_lane_score"),
                "price": r.get("price"),
                "saved_at": datetime.datetime.now(WIB).isoformat(),
            })

        with open(path, "w") as f:
            json.dump({"picks": rows, "saved_at": datetime.datetime.now(WIB).isoformat()}, f, indent=2)
    except Exception as e:
        print(f"⚠️ Gagal simpan latest_screendaytrade_picks.json: {e}")


# NOTE (MBSS v2 refactor, Phase 5a): screen_daytrade() moved to
# commands/scan.py (Command Layer). Registered via `commands_scan.xxx` in
# build_app() below.


STATUS_LABEL_ID = {
    "pending_entry": "⏳ Menunggu Entry",
    "pending_resolution": "🔄 Berjalan",
    "win": "✅ Win",
    "lose": "❌ Lose",
    "win_timebased": "✅ Win (time-based)",
    "lose_timebased": "❌ Lose (time-based)",
}


# NOTE (MBSS v2 refactor, Phase 5b): show_winrate, test_morning_brief,
# test_opening_dynamics moved to commands/misc.py (Command Layer).
# Registered via `commands_misc.xxx` in build_app() below.


# NOTE (MBSS v2 refactor, Phase 5a): the entire GPTPICK cluster
# (GPTPICK_* constants, all _gptpick_* scoring helpers, _run_gptpick,
# gptpick_command, gptpick_callback) moved WHOLESALE to commands/scan.py
# (Command Layer) — confirmed self-contained, nothing outside this
# cluster ever called any of it. Registered via `commands_scan.xxx` in
# build_app() below.


# ==========================================
# 💼 PORTFOLIO COMMANDS
# ==========================================
# NOTE (MBSS v2 refactor, Phase 5c): buy_position, sell_position,
# add_cash_command, withdraw_cash_command, reset_portfolio_command,
# set_entry_date_command, watchlist_command, and MAX_BROKERSUM_PER_RUN all
# moved to commands/portfolio.py (Command Layer). Registered via
# `commands_portfolio.xxx` in build_app() below.


def format_position_block(scoring: dict, is_holding: bool, weight_pct: float = None,
                           unrealized_pnl_pct: float = None, days_held: int = None,
                           reasoning_text: str = "") -> str:
    """
    Builds one position's report block — SEMUA field struktural/numerik dihitung
    Python, hanya baris "Alasan" akhir dari Gemini. Disusun ulang atas permintaan
    user jadi BERKELOMPOK per bagian (header emoji) — sebelumnya semua field
    mengalir sebagai daftar panjang tanpa pengelompokan visual, butuh fokus
    lebih untuk dibaca. Tidak menghapus info apa pun, murni reorganisasi +
    tambahan "Risiko Personal" (dari avg_buy_price ASLI, bukan harga hari ini).
    """
    ticker = scoring["ticker"]
    lines = [f"─────────────────────────────────────", ticker]

    # Action Priority — tetap PALING ATAS, tujuannya scan cepat tanpa baca detail.
    if is_holding:
        lifecycle_category_for_priority = scoring.get("_lifecycle", {}).get("category")
        action_priority = scoring_engine.classify_action_priority(scoring, lifecycle_category_for_priority)
        priority_label = ACTION_PRIORITY_LABEL_ID.get(action_priority["priority"], "?")
        lines.append(f"{priority_label}")

    bs = scoring.get("brokersum")

    # === STATUS ===
    if is_holding:
        lines.append("\n📊 STATUS")
        modal_emoji = "🟢" if unrealized_pnl_pct >= 0 else ("🟡" if unrealized_pnl_pct >= -8 else "🔴")
        modal_status = f"{modal_emoji} {'Floating Gain' if unrealized_pnl_pct >= 0 else 'Floating Loss'}"
        lines.append(f"Bobot: {weight_pct:.1f}% | Modal: {modal_status} ({unrealized_pnl_pct:+.1f}%)")

        lifecycle = scoring.get("_lifecycle", {})
        category = lifecycle.get("category", "BELUM_DIKETAHUI")
        if category == "BELUM_DIKETAHUI":
            lines.append(f"Hari Dipegang: ❔ belum diketahui (jalankan /setentrydate {ticker} YYYY-MM-DD)")
        else:
            lines.append(f"Hari Dipegang: {days_held} hari bursa")
        lines.append(f"Kategori: {LIFECYCLE_LABEL_ID.get(category, category)} ({lifecycle.get('reason', '')})")

    freshness = f"✅ Ada data broker ({({'screenshot': 'screenshot', 'zapi': 'Zapi'}.get(bs.get('source', 'api'), 'Index Alpha') if bs else '')})" if bs else "🟡 Belum ada data broker riil hari ini"
    lines.append(f"Data Freshness: {freshness}")

    risk_char = scoring.get("risk_character")
    if risk_char:
        RISK_CHARACTER_LABEL_ID_LOCAL = {
            "BASE_DEFENSIF": "🛡️ BASE/DEFENSIF",
            "SWING_AGRESIF": "⚡ SWING/AGRESIF",
            "NETRAL": "➖ NETRAL",
        }
        lines.append(f"Karakter: {RISK_CHARACTER_LABEL_ID_LOCAL.get(risk_char, risk_char)} ({scoring.get('risk_character_reason', '')})")

    hc = scoring.get("high_conviction", {})
    if hc:
        lines.append(f"{hc.get('conviction_label', '')} ({hc.get('criteria_met', 0)}/{hc.get('criteria_checkable', 0)} kriteria)")

    # === AKSI ===
    lines.append("\n🎯 AKSI")
    action_label = scoring.get("action_label_id", "?")
    lines.append(f"REKOMENDASI: {action_label}")

    targets = scoring.get("targets", {})
    lines.append(f"Entry: {targets.get('buy_range', '?')} | SL: {targets.get('cut_loss', '?')} | TP1: {targets.get('tp_1', '?')}")
    rr_min = targets.get("risk_reward_at_min")
    rr_max = targets.get("risk_reward_at_max")
    if rr_min is not None or rr_max is not None:
        rr_min_str = f"1:{rr_min}" if rr_min is not None else "-"
        rr_max_str = f"1:{rr_max}" if rr_max is not None else "-"
        lines.append(f"Risk:Reward — entry bawah {rr_min_str} | entry atas {rr_max_str}")

    active = scoring.get("active_breakout", {})
    if active.get("available"):
        lines.append(
            f"Live sesi: {active.get('label')} {active.get('score')}/100 | "
            f"Trigger {active.get('trigger_price')} | VWAP {active.get('vwap')} | "
            f"Invalid {active.get('invalidation_level')}"
        )
        if is_holding:
            price_now = scoring.get("price") or active.get("current_price")
            tp1_now = targets.get("tp_1")
            cl_now = targets.get("cut_loss")
            if tp1_now and price_now and price_now >= tp1_now:
                lines.append("Aksi live: TP1 sudah/nyaris tercapai — disiplin realisasi profit sebagian/total sesuai rencana.")
            elif cl_now and price_now and price_now <= cl_now:
                lines.append("Aksi live: harga di bawah cut-loss — prioritas exit, jangan average down.")
            elif active.get("score", 0) < 50 and price_now and active.get("invalidation_level") and price_now <= active.get("invalidation_level"):
                lines.append("Aksi live: kehilangan VWAP/invalidation — pertimbangkan kurangi posisi atau tighten stop.")
            elif active.get("score", 0) >= 75:
                lines.append("Aksi live: momentum sesi masih mendukung — hold/trailing stop, jangan tambah jika sudah extended.")

    # Risiko Personal — BARU, khusus posisi dipegang: dari avg_buy_price ASLI
    # kamu, BUKAN dari target_buy_max hari ini. Target/RR di atas menjawab
    # "kalau entry hari ini", ini menjawab "dari uang yang SUDAH saya taruh di
    # sini, seberapa jauh SL ini dari cost basis saya sebenarnya".
    if is_holding:
        avg_buy_price = scoring.get("avg_buy_price")
        cut_loss = targets.get("cut_loss")
        tp1 = targets.get("tp_1")
        if avg_buy_price and cut_loss and avg_buy_price > cut_loss:
            personal_risk_pct = (avg_buy_price - cut_loss) / avg_buy_price * 100
            personal_reward_pct = (tp1 - avg_buy_price) / avg_buy_price * 100 if tp1 else None
            personal_rr = round((tp1 - avg_buy_price) / (avg_buy_price - cut_loss), 2) if tp1 else None
            personal_rr_str = f", RR 1:{personal_rr}" if personal_rr is not None else ""
            lines.append(f"Risiko Personal (dari avg beli {avg_buy_price}): -{personal_risk_pct:.1f}% ke SL{personal_rr_str}")

    tp_horizon = scoring.get("_tp_horizon", {})
    if tp_horizon.get("horizon_days_low"):
        lines.append(
            f"Horizon estimasi: {tp_horizon['horizon_days_low']}-{tp_horizon['horizon_days_high']} hari "
            f"(Confidence: {tp_horizon['confidence']})"
        )

    # === TEKNIKAL ===
    lines.append("\n📈 TEKNIKAL")
    macd_cross_note = ""
    if scoring.get("macd_cross_days_ago") is not None:
        macd_cross_note = f" (cross {scoring['macd_cross_direction']} {scoring['macd_cross_days_ago']} hari lalu)"
    lines.append(
        f"MACD: {scoring.get('macd_state')}{macd_cross_note} | "
        f"ADX: {scoring.get('adx')} ({format_adx_label(scoring.get('adx', 0))})"
    )
    lines.append(
        f"Breakout 20 hari: {scoring.get('is_new_high_20d')} | "
        f"RS vs IHSG (hari ini): {scoring.get('relative_strength_vs_ihsg')}%"
    )
    if scoring.get("consecutive_low_volume_days", 0) >= 3:
        lifted_note = " (penalti dihapus, uang mulai masuk)" if scoring.get("dead_stock_penalty_lifted") else ""
        lines.append(f"⚠️ Volume rendah {scoring['consecutive_low_volume_days']} hari berturut-turut{lifted_note}")

    # === SKOR ===
    lines.append("\n💯 SKOR")
    scores = scoring.get("scores", {})
    lines.append(f"Value {scores.get('value')} | Momentum {scores.get('momentum')} | Sentiment {scores.get('sentiment')}")
    if scoring.get("brokersum_adjusted"):
        lines.append(f"Final: {scores.get('final')} (disesuaikan brokersum riil, {scoring.get('brokersum_adjustment_applied', 0):+.2f})")
    else:
        lines.append(f"Final: {scores.get('final')} (belum ada penyesuaian brokersum)")

    # === BROKER (kalau ada data) ===
    if bs:
        source = bs.get("source", "api")
        estimate_note = " (ESTIMASI dari volume x harga)" if bs.get("net_foreign_flow_idr_is_estimate") else ""
        lines.append("\n💹 BROKER")
        lines.append(f"Broker Flow ALL 3D: {bs.get('net_foreign_flow_pct')}%{estimate_note}")

        PATTERN_LABEL_ID = {
            "akumulasi_bertahap": "akumulasi bertahap",
            "transaksi_block": "transaksi block",
            "normal": "normal",
            "tidak_diketahui": "?",
        }

        def format_broker_list(brokers):
            if not brokers:
                return "-"
            parts = []
            for b in brokers:
                pattern = b.get("pattern")
                pattern_label = f" ({PATTERN_LABEL_ID.get(pattern, pattern)})" if pattern else ""
                parts.append(f"{b['code']}: Rp{b['net_idr']:,}{pattern_label}")
            return ", ".join(parts)

        if bs.get("top_net_buyers") or bs.get("top_net_sellers"):
            lines.append(f"Top Net Buyers: {format_broker_list(bs.get('top_net_buyers'))}")
            lines.append(f"Top Net Sellers: {format_broker_list(bs.get('top_net_sellers'))}")
        elif source == "zapi":
            lines.append("(Rincian per-broker tidak tersedia dari Zapi — hanya total net foreign di atas)")

    # === ALASAN ===
    if reasoning_text:
        lines.append(f"\n💬 {reasoning_text}")

    return "\n".join(lines)


# NOTE (MBSS v2 refactor, Phase 5c): my_portfolio, order_command,
# portfolio_summary moved to commands/portfolio.py (Command Layer).
# Registered via `commands_portfolio.xxx` in build_app() below.


def _safe_float(value, default=0.0):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _opening_priority_score(d: dict) -> float:
    """Composite ranking for 09:45 dynamics: live breakout + volume pace + price action + EOD score."""
    active = _safe_float(d.get("active_breakout_score"), 0)
    vol_pace = _safe_float(d.get("volume_pace_ratio"), 0)
    chg_open = _safe_float(d.get("change_from_open_pct"), 0)
    chg_prev = _safe_float(d.get("change_from_prior_close_pct"), 0)
    final_score = _safe_float(d.get("final_score"), 0)

    # Cap volume contribution so a tiny illiquid spike does not dominate everything.
    vol_component = min(max(vol_pace, 0), 8) * 7
    move_component = max(chg_open, 0) * 4 + max(chg_prev, 0) * 2
    fade_penalty = abs(min(chg_open, 0)) * 5
    score_component = final_score * 4
    holding_bonus = 5 if d.get("is_holding") else 0
    return round(active + vol_component + move_component + score_component + holding_bonus - fade_penalty, 2)


def _opening_status_hint(d: dict) -> str:
    """Deterministic status hint sent to Gemini so the output remains stable."""
    vol_pace = _safe_float(d.get("volume_pace_ratio"), 0)
    gap = _safe_float(d.get("gap_pct"), 0)
    from_open = _safe_float(d.get("change_from_open_pct"), 0)
    from_prev = _safe_float(d.get("change_from_prior_close_pct"), 0)
    active = _safe_float(d.get("active_breakout_score"), 0)

    if d.get("is_holding"):
        if from_open >= 1.0 and vol_pace >= 1.2:
            return "MOMENTUM ACTIVE"
        if gap > 0 and from_open <= -0.7:
            return "FADING RISK"
        if from_prev <= -1.5 and vol_pace >= 1.2:
            return "REDUCE WATCH"
        if abs(from_open) <= 0.5 and vol_pace < 1.0:
            return "HOLD / NEUTRAL"
        return "HOLD / WATCH SUPPORT"

    if from_prev >= 3 and vol_pace >= 1.5:
        return "MOMENTUM WATCH / HIGH RISK"
    if active >= 70 and from_open >= 0:
        return "BUY WATCH"
    if gap > 0 and from_open < -0.7:
        return "WAIT PULLBACK"
    if from_prev < 0 and vol_pace >= 1.2:
        return "AVOID FOR NOW / SELL PRESSURE"
    return "MONITOR"


def _merge_opening_scoring(dyn: dict, scored_cache: dict) -> dict:
    """Attach cached EOD score/targets to intraday opening dynamics when available."""
    ticker = dyn.get("ticker")
    scoring = (scored_cache or {}).get(ticker, {}) if ticker else {}
    if not scoring:
        dyn["action_label_id"] = dyn.get("action_label_id") or "MONITOR"
        dyn["status_hint"] = _opening_status_hint(dyn)
        dyn["opening_priority_score"] = _opening_priority_score(dyn)
        return dyn

    keys_to_copy = [
        "action_label_id", "value_score", "momentum_score", "sentiment_score", "final_score",
        "targets", "intraday_targets", "risk_character", "risk_character_reason",
        "vol_ratio", "cmf", "obv_divergence", "rsi", "macd_state", "adx",
        "day_range_pct_10d", "relative_strength_vs_ihsg", "as_of_date",
    ]
    for k in keys_to_copy:
        if k in scoring and k not in dyn:
            dyn[k] = scoring[k]

    # Normalize key levels for easier prompting.
    targets = dyn.get("intraday_targets") or dyn.get("targets") or {}
    dyn["entry"] = targets.get("buy_range") or [targets.get("entry_bawah"), targets.get("entry_atas")]
    dyn["tp1"] = targets.get("tp_1") or targets.get("tp1")
    dyn["sl"] = targets.get("cut_loss") or targets.get("sl")
    dyn["status_hint"] = _opening_status_hint(dyn)
    dyn["opening_priority_score"] = _opening_priority_score(dyn)
    return dyn


def build_opening_dynamics_payload(all_dynamics: list, held_tickers: set, macro_context: dict = None) -> dict:
    """Shape the data so Gemini outputs portfolio-first plus non-holding opportunities."""
    holdings = [d for d in all_dynamics if d.get("is_holding")]
    non_holdings = [d for d in all_dynamics if not d.get("is_holding")]

    holdings = sorted(
        holdings,
        key=lambda d: (_opening_priority_score(d), _safe_float(d.get("change_from_prior_close_pct"), 0)),
        reverse=True,
    )

    # Avoid flooding Telegram: show only the strongest non-holding opportunities.
    non_holdings_ranked = sorted(non_holdings, key=_opening_priority_score, reverse=True)
    non_holdings_watchlist = [
        d for d in non_holdings_ranked
        if _safe_float(d.get("opening_priority_score"), 0) >= 40
        or _safe_float(d.get("volume_pace_ratio"), 0) >= 1.2
        or _safe_float(d.get("active_breakout_score"), 0) >= 65
        or abs(_safe_float(d.get("change_from_prior_close_pct"), 0)) >= 2.5
    ][:5]

    top_priority = sorted(holdings + non_holdings_watchlist, key=_opening_priority_score, reverse=True)[:5]

    return {
        "report_time_wib": datetime.datetime.now(WIB).strftime("%Y-%m-%d %H:%M WIB"),
        "macro_context": macro_context or {},
        "portfolio_holdings": holdings,
        "non_holdings_watchlist": non_holdings_watchlist,
        "top_priority_candidates": top_priority,
        "notes": [
            "Portfolio holdings harus dibahas lebih dulu sebagai position management.",
            "Non-holdings hanya peluang/watchlist; beri label rotation candidate jika momentumnya lebih kuat dari holdings.",
            "Gunakan status_hint dan opening_priority_score sebagai panduan, bukan sebagai teks mentah."
        ],
    }



# NOTE (MBSS v2 refactor, Phase 5a): eodscan_command() moved to
# commands/scan.py (Command Layer). Registered via `commands_scan.xxx` in
# build_app() below.


# ==========================================
# 🕤 OPENING DYNAMICS (09:45 WIB, weekdays)
# ==========================================
# NOTE (MBSS v2 refactor, Phase 2): run_nightly_full_scan() moved to
# engine/nightly.py (NightlyEngine). Imported back in via
# `from engine.nightly import ...` near the top of this file.


async def run_opening_dynamics(context: ContextTypes.DEFAULT_TYPE):
    try:
        if await asyncio.to_thread(is_idx_market_holiday_today):
            print("📅 Skipping Opening Dynamics — IDX market holiday today.")
            return

        portfolio = load_portfolio()
        held_tickers = set(portfolio.get("positions", {}).keys())
        # Filter lewat whitelist cache likuiditas bulanan (sama seperti run_morning_brief)
        # — tanpa ini, scan jalan ke seluruh ~450 ticker ISSI mentah tiap kali, yang
        # memicu throttling Yahoo Finance dan bikin ticker likuid pun ikut gagal
        # ("possibly delisted") padahal sebenarnya cuma kena rate-limit.
        raw_sharia_universe = fetch_online_sharia_list()
        sharia_universe = set(await asyncio.to_thread(load_or_build_whitelist, list(raw_sharia_universe)))

        # Macro backdrop shown in the first 1-2 sentences.
        macro_context = await asyncio.to_thread(market_engine.fetch_macro_context)
        extra_context_str = None
        if macro_context:
            macro_lines = "\n".join(f"- {k}: {v:+.2f}%" for k, v in macro_context.items())
            extra_context_str = f"Macro indices (% change, most recent session):\n{macro_lines}"

        # MBSS v2 (RapidAPI integration, user request): tambahkan konteks
        # top gainer intraday (checkpoint 09:30/14:30 WIB — lihat
        # broker_engine.get_or_refresh_intraday_market_snapshot) ke konteks
        # yang sama dipakai Gemini buat menulis brief-nya, supaya narasi
        # otomatis mempertimbangkan saham yang benar-benar ramai PAGI INI,
        # bukan cuma data cache semalam. Sumber datanya sendiri TIDAK
        # disebut ke Gemini (konvensi teks user-facing sesi ini) — cukup
        # sebagai konteks pasar tambahan, sama seperti macro_context.
        try:
            snapshot = await asyncio.to_thread(broker_engine.get_or_refresh_intraday_market_snapshot)
            movers = (snapshot.get("market_mover") or {}).get("mover_list") or []
            if movers:
                mover_lines = "\n".join(
                    f"- {(m.get('stock_detail') or {}).get('code')}: "
                    f"{((m.get('stock_detail') or {}).get('change') or {}).get('percentage', 0):+.1f}%"
                    for m in movers[:10]
                )
                mover_block = f"Top gainers pagi ini (data intraday terkini):\n{mover_lines}"
                extra_context_str = f"{extra_context_str}\n\n{mover_block}" if extra_context_str else mover_block
        except Exception as e:
            print(f"⚠️ Gagal menambahkan konteks top gainer ke Opening Dynamics: {e}")

        # Use nightly scan cache to enrich opening dynamics with existing action/score/TP/SL.
        # If cache is empty/stale, the report still works from intraday dynamics alone.
        scored_cache = nightly_engine.load_daily_scan_cache()

        # Cover: everything the user holds, plus the Sharia universe for outside opportunities.
        tickers_to_check = held_tickers | sharia_universe

        dynamics_data = []
        for ticker in tickers_to_check:
            try:
                dyn = await asyncio.wait_for(
                    asyncio.to_thread(fetch_opening_dynamics, ticker), timeout=1800
                )
                if dyn:
                    dyn["is_holding"] = ticker in held_tickers
                    dyn = _merge_opening_scoring(dyn, scored_cache)
                    dynamics_data.append(dyn)
            except asyncio.TimeoutError:
                print(f"⏱️ Timed out fetching {ticker}, skipping.")
            except Exception as e:
                print(f"Error fetching opening dynamics for {ticker}: {e}")
            await asyncio.sleep(1.5)  # pause between requests to reduce Yahoo rate-limit risk

        if not dynamics_data:
            print("No opening dynamics data compiled. Skipping send.")
            await safe_reply(
                context.bot,
                "⚠️ Opening Dynamics gagal: tidak ada data intraday yang berhasil diambil.",
                chat_id=TELEGRAM_CHAT_ID,
            )
            return

        opening_payload = build_opening_dynamics_payload(dynamics_data, held_tickers, macro_context)

        brief_text = ask_gemini_to_analyze(
            opening_payload, OPENING_BREAKOUT_INSTRUCTION, extra_context=extra_context_str
        )
        await safe_reply(context.bot, brief_text, chat_id=TELEGRAM_CHAT_ID)
        print("✅ Opening Dynamics push sent!")

    except Exception as e:
        print(f"❌ run_opening_dynamics failed: {e}")
        try:
            await safe_reply(
                context.bot,
                f"⚠️ Opening Dynamics gagal karena error: {str(e)[:200]}",
                chat_id=TELEGRAM_CHAT_ID,
            )
        except Exception as notify_error:
            print(f"❌ Also failed to send failure notification: {notify_error}")


# ==========================================
# 🏗️ APP SETUP
# ==========================================
async def global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    """
    Last-resort catch untuk exception yang tidak ditangani command handler.
    Dibedakan antara network blip (tidak perlu notifikasi user) vs error genuine.
    """
    err = context.error
    err_str = str(err)

    # Network/connection errors — log saja, jangan kirim notifikasi ke user
    # karena ini bukan error dari kode tapi dari koneksi sesaat
    network_errors = (
        "ReadError", "ConnectError", "TimeoutError",
        "NetworkError", "TimedOut", "ConnectionReset",
        "RemoteProtocolError", "httpx"
    )
    is_network_blip = any(ne in err_str or ne in type(err).__name__ for ne in network_errors)

    if is_network_blip:
        print(f"⚠️ Network blip (diabaikan): {type(err).__name__}: {err_str[:100]}")
        return

    # Error genuine — log dan notifikasi user
    print(f"❌ Unhandled error in command handler: {err}")
    try:
        if isinstance(update, Update) and update.effective_chat:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=f"⚠️ Terjadi error: {err_str[:200]}\nCoba lagi.",
            )
    except Exception as notify_error:
        print(f"❌ Also failed to notify user of error: {notify_error}")


async def send_startup_notice(app: Application):
    """Sent once automatically when the bot process starts — the person's cue that
    it's alive, and the one place the disclaimer appears instead of every message."""
    try:
        await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text="🟢 Bot baru saja aktif.\n\n" + commands_misc.STARTUP_DISCLAIMER)
    except Exception as e:
        print(f"⚠️ Failed to send startup notice: {e}")



# NOTE (MBSS v2 refactor, Phase 5d): brokersum_upload_command() moved to
# commands/check.py (Command Layer). Registered via `commands_check.xxx`
# in build_app() below.


def build_app():
    # Longer timeouts than PTB's defaults — mobile connections (WiFi/cellular handoff,
    # slow cold-start) can be slower to establish than a stable server connection.
    request = HTTPXRequest(
        connect_timeout=60.0,
        read_timeout=60.0,
        write_timeout=60.0,
        pool_timeout=60.0,
    )

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).request(request).post_init(send_startup_notice).build()

    app.add_handler(CommandHandler("start", commands_misc.start))
    app.add_handler(CommandHandler("version", commands_misc.show_version))
    app.add_handler(CommandHandler("whitelist", commands_misc.show_whitelist_status))
    app.add_handler(CommandHandler(["glossary", "istilah"], commands_misc.show_glossary))
    app.add_handler(CommandHandler("rebuildwhitelist", commands_misc.rebuild_whitelist_command))
    app.add_handler(CommandHandler(["check", "cek"], commands_check.check_stock))
    app.add_handler(CommandHandler("tanya", commands_chat.tanya_command))
    app.add_handler(CommandHandler("tanyareset", commands_chat.tanya_reset_command))
    app.add_handler(CallbackQueryHandler(commands_check.skip_brokersum_callback, pattern="^skip_brokersum$"))
    app.add_handler(CallbackQueryHandler(commands_check.quick_check_callback, pattern="^qchk_"))
    app.add_handler(CallbackQueryHandler(commands_portfolio.order_clear_callback, pattern="^orderclear_"))
    app.add_handler(CallbackQueryHandler(commands_portfolio.select_screendaytrade_brokersum, pattern="^bsdt_"))
    app.add_handler(MessageHandler(filters.PHOTO, commands_check.handle_brokersum_photo))
    app.add_handler(CommandHandler("buy", commands_portfolio.buy_position))
    app.add_handler(CommandHandler("sell", commands_portfolio.sell_position))
    app.add_handler(CommandHandler("batchbuy", commands_portfolio.batch_buy_position))
    app.add_handler(CommandHandler("batchsell", commands_portfolio.batch_sell_position))
    app.add_handler(CommandHandler("addcash", commands_portfolio.add_cash_command))
    app.add_handler(CommandHandler("withdrawcash", commands_portfolio.withdraw_cash_command))
    app.add_handler(CommandHandler("resetportfolio", commands_portfolio.reset_portfolio_command))
    app.add_handler(CommandHandler("watchlist", commands_portfolio.watchlist_command))
    app.add_handler(CommandHandler("setentrydate", commands_portfolio.set_entry_date_command))
    app.add_handler(CommandHandler("summary", commands_portfolio.portfolio_summary))
    app.add_handler(CommandHandler("order", commands_portfolio.order_command))
    app.add_handler(CommandHandler(["myportfolio", "portofolio"], commands_portfolio.my_portfolio))
    app.add_handler(CommandHandler("testbrief", commands_misc.test_morning_brief))
    app.add_handler(CommandHandler("screendaytrade", commands_scan.screen_daytrade))
    app.add_handler(CommandHandler("gptpick", commands_scan.gptpick_command))
    app.add_handler(CommandHandler(["hc", "highconviction"], commands_scan.high_conviction_command))
    app.add_handler(CommandHandler(["strongbuy", "sb"], commands_scan.strong_buy_command))
    app.add_handler(CommandHandler("consensus", commands_scan.consensus_command))
    app.add_handler(CommandHandler("fast", commands_scan.fast_candidates_command))
    app.add_handler(CommandHandler(["broksum", "brokeraktivitas"], commands_scan.broksum_command))
    app.add_handler(CommandHandler("brokerdiscovery", commands_scan.broker_discovery_command))
    app.add_handler(CommandHandler("bsjp", commands_scan.bsjp_screening_command))
    app.add_handler(CommandHandler(["eodscan", "nightlyscan"], commands_scan.eodscan_command))
    app.add_handler(CallbackQueryHandler(commands_scan.gptpick_callback, pattern="^gptpick:(3|5)$"))
    app.add_handler(CommandHandler("brokersum", commands_check.brokersum_upload_command))
    app.add_handler(CommandHandler("executiongate", commands_scan.executiongate_command))
    app.add_handler(CommandHandler("winrate", commands_misc.show_winrate))
    app.add_handler(CommandHandler("brokerwinrate", commands_misc.show_broker_winrate))
    app.add_handler(CommandHandler("menu", commands_misc.show_shortcut_menu))
    app.add_handler(CommandHandler("menuoff", commands_misc.hide_shortcut_menu))
    app.add_handler(CallbackQueryHandler(handle_check_button_callback, pattern=r"^check:"))

    # NOTE (MBSS v2 refactor, Phase 5b): db_stats_command / populate_db_command
    # used to be defined here as NESTED closures (pre-existing inconsistency,
    # not introduced by this refactor) — moved to commands/misc.py as
    # ordinary top-level functions, behavior unchanged.
    app.add_handler(CommandHandler("dbstats", commands_misc.db_stats_command))
    app.add_handler(CommandHandler("dbstatus", commands_misc.db_stats_command))
    app.add_handler(CommandHandler("populatedb", commands_misc.populate_db_command))
    app.add_handler(CommandHandler("testopening", commands_misc.test_opening_dynamics))
    app.add_error_handler(global_error_handler)

    # No automatic scheduling — hosted on a request-driven webhook (PythonAnywhere
    # free web app) with no persistent background process to run a JobQueue.
    # Morning brief / nightly scan / opening dynamics are triggered manually via
    # /testbrief, /screendaytrade, /testopening instead.

    return app


# MBSS v2 (user request — inline "Cek TICKER" buttons di semua tools):
def build_check_buttons(tickers: list, max_buttons: int = 10) -> InlineKeyboardMarkup | None:
    """
    Grid tombol 2 per baris, TIDAK melebihi max_buttons (default 10 —
    pesan dengan puluhan saham, mis. /strongbuy, tidak perlu tombol untuk
    semuanya, cukup yang paling atas/relevan). callback_data format
    "check:TICKER" (dibatasi 64 byte oleh Telegram, ticker IDX maksimal
    ~6 huruf jadi aman jauh dari batas itu).

    Return None kalau list ticker kosong (pemanggil bisa langsung pass ke
    reply_markup= tanpa perlu cek kosong secara terpisah — None berarti
    "tidak ada tombol", diterima Telegram dengan baik).
    """
    unique_tickers = list(dict.fromkeys(tickers))[:max_buttons]
    if not unique_tickers:
        return None
    buttons = [InlineKeyboardButton(f"🔍 {t}", callback_data=f"check:{t}") for t in unique_tickers]
    rows = [buttons[i:i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)


async def handle_check_button_callback(update, context):
    """
    Handler tombol "🔍 TICKER" — tap tombol memicu /check TICKER, hasilnya
    dikirim sebagai pesan BARU (bukan edit pesan lama, supaya histori chat
    tetap jelas siapa pick apa). Perlu answer() dulu (wajib API Telegram,
    hilangkan status "loading" di tombol) sebelum proses lebih lanjut.
    """
    query = update.callback_query
    await query.answer()

    if not query.data.startswith("check:"):
        return
    ticker = query.data.split(":", 1)[1]

    import commands.check as commands_check  # lazy import, hindari circular (commands/check.py import legacy_core)

    # check_stock() baca ticker dari context.args — bangun context tiruan
    # minimal yang cukup buat itu, reuse context asli (punya bot/application)
    # cuma args-nya diganti.
    fake_context = type("FakeContext", (), {"args": [ticker], "bot": context.bot})()
    fake_update = type("FakeUpdate", (), {"message": query.message, "effective_chat": query.message.chat})()
    try:
        await commands_check.check_stock(fake_update, fake_context)
    except Exception as e:
        print(f"⚠️ Gagal proses tombol cek {ticker}: {e}")
        await query.message.reply_text(f"⚠️ Gagal cek {ticker}: {e}")


# MBSS v2 (user request — inline winrate per label di semua tools, supaya
# tidak perlu recall/cross-reference manual ke /winrate): cache in-memory
# ringan, supaya history TIDAK dibaca ulang dari disk untuk SETIAP saham
# dalam 1 pesan (mis. /hc nampilkan 10 saham -> tanpa cache ini, 10x baca
# file yang sama).
_winrate_label_cache = {"loaded_at": None, "stats": {}}


def _rebuild_winrate_label_cache():
    history = load_daytrade_picks_history()
    resolved = [p for p in history if p.get("status") in ("win", "lose", "win_timebased", "lose_timebased")]
    stats = {}
    for p in resolved:
        label = p.get("signal_label") or "N/A"
        stats.setdefault(label, []).append(p)
    _winrate_label_cache["stats"] = stats
    _winrate_label_cache["loaded_at"] = datetime.datetime.now(WIB)


def get_winrate_for_label(label: str) -> str:
    """
    Return string singkat siap-tampil, mis. "38% (13x)" — kalau data belum
    cukup (sampel <3, jangan gegabah dipercaya) atau label belum pernah
    tercatat, return string kosong ("") supaya pemanggil bisa skip tanpa
    perlu cek None secara terpisah.

    Cache di-refresh maksimal 1x/5menit (bukan tiap panggilan) — cukup
    responsif buat sesi chat yang sama, tidak bebani I/O kalau dipanggil
    puluhan kali dalam 1 pesan (mis. /hc top 10).
    """
    now = datetime.datetime.now(WIB)
    if _winrate_label_cache["loaded_at"] is None or (now - _winrate_label_cache["loaded_at"]).total_seconds() > 300:
        _rebuild_winrate_label_cache()

    picks = _winrate_label_cache["stats"].get(label)
    if not picks or len(picks) < 3:
        return ""
    wins = sum(1 for p in picks if p["status"] in ("win", "win_timebased"))
    winrate_pct = wins / len(picks) * 100
    return f"{winrate_pct:.0f}% ({len(picks)}x)"


def get_days_to_breakout_for_label(label: str) -> str:
    """
    MBSS v2 (RapidAPI integration, user request) — jawaban jujur untuk
    "confidence breakout terjadi dalam berapa hari": SEBELUM ini, klaim
    seperti itu di kriteria HC ("prediksi 1-2 hari ke depan") murni TARGET
    DESAIN yang membentuk pemilihan parameter (jendela 5 hari, high 10 hari,
    EMA9/SMA20) — bukan angka yang benar-benar dihitung. Ini gantinya:
    median hari KALENDER dari pick_date sampai resolved_date, dihitung dari
    track record /winrate kita sendiri, HANYA untuk resolusi win/
    win_timebased (breakout beneran kejadian) — supaya jujur menjawab
    "kalau sinyal ini benar, historisnya berapa lama sampai kejadian",
    bukan tercampur dengan yang gagal sama sekali.

    Sama seperti get_winrate_for_label: butuh sampel >=3 sebelum dipercaya,
    return "" (bukan angka spekulatif) kalau belum cukup data — makin
    banyak pick terkumpul dari waktu ke waktu, makin akurat angkanya,
    bukan statis seperti klaim desain lama.
    """
    now = datetime.datetime.now(WIB)
    if _winrate_label_cache["loaded_at"] is None or (now - _winrate_label_cache["loaded_at"]).total_seconds() > 300:
        _rebuild_winrate_label_cache()

    picks = _winrate_label_cache["stats"].get(label)
    if not picks:
        return ""

    day_counts = []
    for p in picks:
        if p.get("status") not in ("win", "win_timebased"):
            continue
        if not p.get("pick_date") or not p.get("resolved_date"):
            continue
        try:
            d1 = datetime.datetime.strptime(p["pick_date"], "%Y-%m-%d").date()
            d2 = datetime.datetime.strptime(p["resolved_date"], "%Y-%m-%d").date()
            day_counts.append((d2 - d1).days)
        except Exception:
            continue

    if len(day_counts) < 3:
        return ""

    day_counts.sort()
    median_days = day_counts[len(day_counts) // 2]
    return f"median {median_days} hari kalender sampai TP (n={len(day_counts)})"


def get_adaptive_lane_penalty(label: str, min_sample: int = 10) -> float:
    """
    MBSS v2 (RapidAPI integration, user request — "pelajari secara adaptif
    untuk hindari picks yang cenderung gagal"): generalisasi dari pola yang
    SEBELUMNYA dikerjakan manual untuk lane SECONDARY WATCH (winrate 38%
    diamati manual dari data /winrate, lalu di-hardcode jadi penalti skor
    -10 langsung di compute_screendaytrade_positive_bias — lihat komentar
    "berbasis data /winrate real — 162 pick selesai" di fungsi itu).
    Sekarang berlaku OTOMATIS untuk SEMUA label, terus ter-update seiring
    data terkumpul — bukan cuma lane yang kebetulan diperhatikan manusia.

    Ambang sampel SENGAJA lebih tinggi dari get_winrate_for_label (n>=10,
    bukan n>=3) — MENAMPILKAN statistik dengan sampel kecil cukup aman
    (user menilai sendiri), tapi BERTINDAK berdasarkan itu (mengurangi
    skor rekomendasi, mempengaruhi keputusan beli) butuh keyakinan lebih
    tinggi, supaya lane BARU (mis. PRIORITY ACCUMULATION, yang saat ini
    nol data histori) tidak langsung kena penalti dari kebetulan jangka
    pendek begitu beberapa pick pertamanya gagal.

    Return 0.0 kalau data belum cukup atau winrate >=50% (tidak ada alasan
    memberi penalti — cuma winrate DI BAWAH koin-lempar yang direspon).
    Return NEGATIF, dibatasi maks -15 (jangan sampai mendominasi skor
    teknikal), proporsional linear ke seberapa jauh di bawah 50%.
    """
    now = datetime.datetime.now(WIB)
    if _winrate_label_cache["loaded_at"] is None or (now - _winrate_label_cache["loaded_at"]).total_seconds() > 300:
        _rebuild_winrate_label_cache()

    picks = _winrate_label_cache["stats"].get(label)
    if not picks or len(picks) < min_sample:
        return 0.0

    wins = sum(1 for p in picks if p["status"] in ("win", "win_timebased"))
    winrate_pct = wins / len(picks) * 100
    if winrate_pct >= 50:
        return 0.0

    # 0% winrate -> -15, 50% winrate -> 0, linear di antaranya.
    penalty = -15.0 * (50 - winrate_pct) / 50
    return round(penalty, 1)


# ==========================================
# NOTE (MBSS v2 refactor): this module has no CLI / __main__ entrypoint
# anymore. The single entrypoint for the whole app is bot.py at the project
# root (Bootstrap layer) — it imports build_app(), run_nightly_full_scan(),
# init_ohlcv_db(), etc. from here and decides one-shot scan vs. polling.
# Run `python bot.py [--eodscan|--nightlyscan|--populatedb|--dbstats]`.
# ==========================================
