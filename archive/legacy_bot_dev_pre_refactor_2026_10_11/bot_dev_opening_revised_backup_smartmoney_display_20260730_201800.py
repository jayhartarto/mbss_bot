from __future__ import annotations
import os
import re
import json
import copy
import time
import asyncio
import logging
import datetime
import sqlite3
import requests
import xml.etree.ElementTree as ET

# Load .env file — pakai python-dotenv kalau tersedia, fallback manual kalau tidak
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
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
    """Use this instead of yf.Ticker(...) directly everywhere in this script."""
    return yf.Ticker(symbol, session=_YF_SESSION)


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
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
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
_ihsg_cache = {"date": None, "return_today": None}


def get_ihsg_return_today():
    """
    Return % IHSG (Jakarta Composite Index, ^JKSE) HARI INI SAJA (close terakhir
    vs close sebelumnya) — DIGANTI dari versi 10-hari kumulatif atas permintaan
    user: untuk swing pendek, perbandingan 10 hari kurang responsif/relevan
    dibanding kondisi harian. Di-cache per hari (bukan per panggilan) karena
    dipanggil berulang kali untuk SETIAP saham dalam satu scan.
    """
    today_str = datetime.datetime.now(WIB).strftime("%Y-%m-%d")
    if _ihsg_cache["date"] == today_str and _ihsg_cache.get("return_today") is not None:
        return _ihsg_cache["return_today"]

    try:
        hist = get_yf_ticker("^JKSE").history(period="5d", timeout=15)
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
            hist = get_yf_ticker(symbol).history(period="5d", timeout=15)
            if len(hist) >= 2:
                pct_change = (hist["Close"].iloc[-1] - hist["Close"].iloc[-2]) / hist["Close"].iloc[-2] * 100
                context[label] = round(pct_change, 2)
        except Exception as e:
            print(f"⚠️ Failed to fetch macro ticker {symbol}: {e}")
    return context


def fetch_company_news(ticker, company_name, max_items=5):
    """
    Pulls recent real news scoped to a SPECIFIC company (not general market news) —
    this is what can actually surface corporate actions like buybacks, earnings
    releases, rights issues, or lawsuits, since the general market query is too
    broad to reliably catch single-company stories. Free via Google News RSS.
    """
    query = f"{company_name} OR {ticker} saham"
    url = f"https://news.google.com/rss/search?q={requests.utils.quote(query)}&hl=id&gl=ID&ceid=ID:id"
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
        print(f"⚠️ Failed to fetch company news for {ticker}: {e}")
        return []


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


def fetch_online_sharia_list(index_key: str = "ISSI"):
    """
    Fetches the live list of Sharia stocks dynamically from a public GitHub raw
    JSON Gist. Defaults to ISSI (70 most-liquid Sharia stocks) for broader
    coverage than plain JII (30 stocks), while still filtering for liquidity —
    unlike ISSI, which includes hundreds of thin/illiquid names.

    Retries on transient network errors before giving up — a single timeout here
    used to silently degrade the whole daily scan from 70 tickers down to a
    9-ticker hardcoded safety list, which is a big quality drop for what's often
    just one bad connection attempt.

    Falls back to JII if ISSI isn't present in the source, then to a small
    hardcoded safety list only if the source is unreachable after retries.
    """
    url = "https://gist.githubusercontent.com/SeptiyanAndika/2941e872798cea3bfb2e550106b8ad28/raw/index-saham.json"
    last_error = None
    for attempt in range(1, 4):  # up to 3 attempts
        try:
            response = requests.get(url, timeout=15)
            response.raise_for_status()
            data = response.json()

            sharia_list = set(data.get(index_key, []))
            if sharia_list:
                print(f"📡 Dynamically fetched {len(sharia_list)} {index_key} Sharia stocks from Gist.")
                return sharia_list

            # Requested index key not present in this source — fall back to JII.
            if index_key != "JII":
                print(f"⚠️ '{index_key}' not found in source, falling back to JII (30 stocks).")
                fallback_list = set(data.get("JII", []))
                if fallback_list:
                    print(f"📡 Dynamically fetched {len(fallback_list)} JII Sharia stocks from Gist.")
                    return fallback_list
            break  # got a real response, just no matching data — no point retrying
        except Exception as e:
            last_error = e
            print(f"⚠️ Gist fetch attempt {attempt}/3 failed: {e}")
            if attempt < 3:
                time.sleep(5 * attempt)  # 5s, 10s backoff

    print(f"⚠️ Failed to fetch live Sharia list after retries: {last_error}. Using fallback safety list.")

    return {"TLKM", "ADRO", "BRIS", "ANTM", "UNTR", "KLBF", "INDF", "ICBP", "ASII"}


# ==========================================
# 💼 PORTFOLIO STORAGE (simple local JSON file — private to this device)
# ==========================================
PORTFOLIO_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "portfolio.json")
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
WHITELIST_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ticker_whitelist.json")
ISSI_LIQUID_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "issi_liquid_whitelist.json")
DAILY_SCAN_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "daily_scan_cache.json")
DAYTRADE_PICKS_HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "daytrade_picks_history.json")
PENDING_ORDERS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pending_orders.json")
OHLCV_DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mbss_ohlcv.db")

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
BROKERSUM_LOOKBACK_DAYS = 7  # trading-day window for the aggregated multi-day view
BROKERSUM_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brokersum_cache.json")
BROKERSUM_HISTORY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "brokersum_history.json")
BROKERSUM_HISTORY_MAX_ENTRIES_PER_TICKER = 60  # ~3 bulan pemakaian rutin, cukup untuk analisis tren tanpa file membengkak

# In-memory state: which ticker was just /check'd, waiting for an optional Broker
# Sum screenshot. Ephemeral (lost on restart) is fine — this is a short-lived,
# per-session UI flow, not data worth persisting. {chat_id: {"ticker": str, "expires_at": datetime}}
PENDING_BROKERSUM_CHECKS = {}
PENDING_BROKERSUM_TIMEOUT_MINUTES = 5


def fetch_broker_summary_raw(ticker: str, from_date: str, to_date: str, investor: str = "all"):
    """
    Raw fetch from Index Alpha's broker-summary endpoint. Returns the list of
    per-broker records, or None on failure. investor: 'all', 'f' (foreign), 'd'
    (domestic), 'or'.

    Paces every call with a fixed delay AFTER it (not just relying on the 150/month
    budget) — the free tier also has a separate 10 requests/minute cap that was
    missed initially, causing every call in a multi-ticker /myportfolio brokersum
    run to fail with a rate-limit rejection (success:False, error:None) once more
    than ~10 requests fired in quick succession.
    """
    try:
        resp = requests.get(
            f"{INDEXALPHA_BASE_URL}/stocks/broker-summary",
            params={"ticker": ticker, "from": from_date, "to": to_date, "investor": investor},
            headers=INDEXALPHA_HEADERS,
            timeout=20,
        )
        data = resp.json()
        if not data.get("success"):
            print(f"⚠️ Index Alpha error for {ticker} ({investor}): {data.get('error')}")
            return None
        return data.get("data", [])
    except Exception as e:
        print(f"⚠️ Index Alpha fetch failed for {ticker} ({investor}): {e}")
        return None
    finally:
        # ~8.5 req/min, safely under the confirmed 10/min free-tier cap, applied
        # regardless of success/failure so pacing holds even after an error.
        time.sleep(7)


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

    if latest_in_db != today_marker:
        fresh = yfinance_get_kline(ticker, period="10d")
        if fresh is not None and not fresh.empty:
            upsert_ohlcv_daily(ticker, fresh)
        elif latest_in_db is None:
            full = yfinance_get_kline(ticker, period="2y")
            if full is not None and not full.empty:
                upsert_ohlcv_daily(ticker, full)

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


def lock_daily_daytrade_picks(top_candidates: list):
    """
    Kunci picks hari ini (IMMUTABLE begitu tersimpan) — dipakai untuk uji winrate
    nanti. Tiap pick disimpan dengan TP1/cut_loss ASLI dari rekomendasi hari itu
    (bukan angka tetap terpisah) — supaya benar-benar menguji akurasi rekomendasi
    sistem, bukan pertanyaan generik "naik X% dalam Y hari". Saham yang sama bisa
    muncul di beberapa tanggal berbeda — ini SENGAJA, karena tiap hari adalah
    sinyal/keputusan independen yang diuji terpisah, bukan "apakah saham X bagus".
    Tidak menambah entri duplikat untuk ticker+pick_date yang sama (idempotent).
    """
    history = load_daytrade_picks_history()
    pick_date = get_current_trading_day_close_marker()
    existing_keys = {(p["ticker"], p["pick_date"]) for p in history}

    added = 0
    for r in top_candidates:
        ticker = r["ticker"]
        if (ticker, pick_date) in existing_keys:
            continue  # sudah dikunci hari ini, jangan duplikat
        history.append({
            "ticker": ticker,
            "pick_date": pick_date,
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
        })
        added += 1

    if added > 0:
        save_daytrade_picks_history(history)
        print(f"🔒 {added} daytrade pick dikunci untuk {pick_date}")
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


def get_last_published_trading_day() -> str:
    """
    Returns YYYY-MM-DD of the most recent trading day whose broker summary data
    has ACTUALLY been published (Index Alpha updates once/day at 19:00 WIB).
    Used as the cache key instead of raw calendar date — so running brokersum
    Sunday night and again Monday morning (before Monday's own data publishes at
    19:00 WIB) correctly hits the same cache entry (both really do show Friday's
    data) instead of wasting a real API call on data that hasn't changed.

    Simplification: accounts for weekends only, not specific IDX public holidays —
    a holiday just causes one extra harmless cache miss (one wasted-but-not-wrong
    fetch), not stale or incorrect data being served.
    """
    now = datetime.datetime.now(WIB)
    reference_date = now.date() if now.hour >= 19 else now.date() - datetime.timedelta(days=1)
    while reference_date.weekday() >= 5:  # 5=Saturday, 6=Sunday
        reference_date -= datetime.timedelta(days=1)
    return reference_date.strftime("%Y-%m-%d")


def _load_brokersum_cache() -> dict:
    if not os.path.exists(BROKERSUM_CACHE_FILE):
        return {}
    try:
        with open(BROKERSUM_CACHE_FILE) as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ Failed to read brokersum cache: {e}")
        return {}


def _save_brokersum_cache(cache: dict):
    try:
        with open(BROKERSUM_CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)
    except Exception as e:
        print(f"⚠️ Failed to save brokersum cache: {e}")


def get_cached_brokersum(ticker: str):
    """
    Read-only cache lookup — used by /check to pick up ALREADY-FETCHED same-
    trading-day data for free, without spending any Index Alpha quota. Returns
    None if there's no cache entry for the current trading-day window (never
    fetches on its own).
    """
    cache = _load_brokersum_cache()
    entry = cache.get(ticker)
    if not entry:
        return None
    current_trading_day = get_last_published_trading_day()
    if entry.get("date") != current_trading_day:
        return None  # data has genuinely moved on (new trading day published), don't reuse
    return entry.get("data")


HARD_NEGATIVE_FLAGS_CHECK = ("lower_highs_bearish",)  # chart_pattern value checked separately


ACTION_PRIORITY_LABEL_ID = {
    "TAKE_PROFIT_CANDIDATE": "🟢 Take Profit Candidate",
    "HOLD": "🔵 Hold",
    "WATCH_CLOSELY": "🟡 Watch Closely",
    "EXIT_CANDIDATE": "🔴 Exit Candidate",
}


def classify_action_priority(scoring: dict, lifecycle_category: str = None) -> dict:
    """
    Label sederhana untuk scan cepat pagi hari — TUJUANNYA supaya user bisa
    lihat label ini SAJA tanpa baca seluruh analisis, langsung tahu posisi mana
    yang perlu diperhatikan lebih dulu. Murni PEMETAAN deterministik dari sinyal
    yang SUDAH dihitung (lifecycle, action decision, jarak ke TP/CL) — tidak ada
    perhitungan baru, tidak ada keputusan tambahan, cuma menyederhanakan yang
    sudah ada jadi satu label yang gampang di-scan.

    Prioritas (dicek berurutan, yang pertama cocok yang dipakai):
    1. Target sudah tercapai → Take Profit Candidate (paling actionable, uang
       di atas meja, jangan sampai terlewat)
    2. Momentum jelas lemah/rekomendasi hindari → Exit Candidate
    3. Sinyal ambigu, kategori hati-hati, atau sangat dekat level kritis (TP/CL
       dalam jarak <3%) → Watch Closely
    4. Selain itu → Hold (kondisi stabil, tidak ada urgensi)
    """
    price = scoring.get("price", 0)
    targets = scoring.get("targets", {})
    tp1 = targets.get("tp_1")
    cut_loss = targets.get("cut_loss")
    action_id = scoring.get("action_id")

    if lifecycle_category == "TARGET_TERCAPAI":
        return {"priority": "TAKE_PROFIT_CANDIDATE", "reason": "harga sudah capai/lewati TP1"}

    if lifecycle_category == "EVALUASI" or action_id == "AVOID_SELL":
        return {"priority": "EXIT_CANDIDATE", "reason": "momentum lemah, belum capai target, atau rekomendasi hindari/jual"}

    dist_to_cutloss_pct = None
    dist_to_tp_pct = None
    if price and tp1 and cut_loss:
        dist_to_cutloss_pct = abs((price - cut_loss) / price * 100)
        dist_to_tp_pct = abs((tp1 - price) / price * 100)
    is_near_critical_level = (
        (dist_to_cutloss_pct is not None and dist_to_cutloss_pct < 3)
        or (dist_to_tp_pct is not None and dist_to_tp_pct < 3)
    )

    if action_id == "MIXED_SIGNALS" or lifecycle_category == "HATI_HATI" or is_near_critical_level:
        reasons = []
        if action_id == "MIXED_SIGNALS":
            reasons.append("sinyal campuran")
        if lifecycle_category == "HATI_HATI":
            reasons.append("kategori hati-hati")
        if is_near_critical_level:
            reasons.append("mendekati level TP/CL (<3%)")
        return {"priority": "WATCH_CLOSELY", "reason": ", ".join(reasons)}

    return {"priority": "HOLD", "reason": "kondisi stabil, tidak ada urgensi khusus saat ini"}


def classify_risk_character(scoring: dict) -> dict:
    """
    Klasifikasi Base/Defensif vs Swing/Agresif — dimensi BERBEDA dari lifecycle
    (Baru/Produktif/dst). Lifecycle soal "sudah berapa lama & masih on track",
    ini soal "apa peran/karakter saham ini". Keduanya bisa berdampingan.

    BASE/DEFENSIF: value_score tinggi (fundamental kuat), volatilitas rendah
    (day_range_pct_10d kecil), tidak ada flag risiko aktif — cocok jadi
    penyeimbang portofolio, bukan sumber cuan cepat.

    SWING/AGRESIF: volatilitas tinggi dan/atau momentum kuat, MUNGKIN ada flag
    risiko tapi didukung sinyal lain (broker riil, MACD, dll) — selalu disertai
    saran manajemen risiko eksplisit karena karakternya memang lebih spekulatif.
    """
    value_score = scoring.get("scores", {}).get("value", 0)
    momentum_score = scoring.get("scores", {}).get("momentum", 0)
    day_range = scoring.get("day_range_pct_10d", 0) or 0
    has_risk_flag = (
        scoring.get("is_overbought_caution", False)
        or scoring.get("is_volume_spike_anomaly", False)
        or scoring.get("chart_pattern") == "lower_highs_bearish"
        or scoring.get("obv_divergence") == "bearish_divergence"
    )

    if value_score >= 6.5 and day_range < 8.0 and not has_risk_flag:
        return {
            "character": "BASE_DEFENSIF",
            "reason": "fundamental kuat, volatilitas rendah, tidak ada flag risiko aktif — cocok jadi penyeimbang portofolio",
        }

    if day_range >= 8.0 or momentum_score >= 7.0:
        risk_note = "ada flag risiko aktif, perlu manajemen risiko lebih ketat" if has_risk_flag else "volatil tapi tanpa flag risiko aktif saat ini"
        return {
            "character": "SWING_AGRESIF",
            "reason": f"volatilitas/momentum tinggi ({risk_note})",
        }

    return {
        "character": "NETRAL",
        "reason": "tidak cukup defensif untuk Base, tidak cukup volatil/bermomentum untuk Swing Agresif",
    }


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


def compute_high_conviction_score(ticker: str, scoring: dict, hist_daily: pd.DataFrame = None) -> dict:
    """
    8 kriteria High Conviction Breakout dari framework Minervini/IBD style
    (sumber: video yang ditinjau user), diadaptasi untuk IDX EOD + 4H Yahoo Finance.

    Kriteria yang diadaptasi (bukan dipakai verbatim karena konteks berbeda):
    - Candle body >5% (4H): pakai 4H native Yahoo Finance (terkonfirmasi selaras
      sesi IDX — 09:00 Sesi 1, 13:00 Sesi 2, tanpa perlu agregasi manual)
    - Market cap: digantikan MIN_STOCK_PRICE + volume filter (sudah di whitelist)
    - Timeframe: video pakai 4H untuk entry, kita adaptasi ke kombinasi EOD + 4H

    Return dict dengan setiap kriteria (True/False/None), jumlah yang terpenuhi,
    dan flag is_high_conviction (>=5 dari 7 kriteria yang bisa dicek).
    """
    result = {
        "consolidation_tight": None,      # Kriteria 1: konsolidasi <12%
        "breakout_close_confirmed": None, # Kriteria 2: close >2% di atas resistance
        "candle_body_strong_4h": None,    # Kriteria 3: body candle 4H >5%
        "relative_volume_ok": None,       # Kriteria 5: vol ratio >=1.5x
        "avg_volume_ok": None,            # Kriteria 6: avg vol harian memadai
        "near_high": None,                # Kriteria 7: dalam 10% dari high 20/50hr
        "above_ma20_and_ma50": None,      # Kriteria 8: di atas EMA21 dan SMA50
        "criteria_met": 0,
        "criteria_checkable": 0,
        "is_high_conviction": False,
        "summary": [],
    }

    price = scoring.get("price")
    if not price:
        return result

    # --- Kriteria 1: Konsolidasi ketat <12% ---
    # Dari video: "consolidation range under 12% measured from open/close of recent candles"
    # Adaptasi EOD: pakai day_range_pct_10d (high-low dalam 10 hari) — proxy yang
    # valid karena mengukur seberapa ketat pergerakan harga belakangan ini.
    day_range = scoring.get("day_range_pct_10d")
    if day_range is not None:
        result["criteria_checkable"] += 1
        tight = day_range < 12.0
        result["consolidation_tight"] = tight
        if tight:
            result["criteria_met"] += 1
            result["summary"].append(f"✅ Konsolidasi ketat: rentang 10hr {day_range}% (<12%)")
        else:
            result["summary"].append(f"❌ Konsolidasi terlalu lebar: {day_range}% (harus <12%)")

    # --- Kriteria 2: Konfirmasi penutupan breakout >2% di atas resistance ---
    # Dari video: "breakout candle must close at least 2% above the upper boundary"
    # Adaptasi: bandingkan close (harga saat ini) vs resistance_10d
    resistance = None
    if hist_daily is not None and not hist_daily.empty and len(hist_daily) >= 10:
        resistance = hist_daily["High"].tail(10).max()
    if resistance and price:
        result["criteria_checkable"] += 1
        pct_above = (price - resistance) / resistance * 100
        confirmed = pct_above >= 2.0
        result["breakout_close_confirmed"] = confirmed
        if confirmed:
            result["criteria_met"] += 1
            result["summary"].append(f"✅ Breakout terkonfirmasi: {pct_above:+.1f}% di atas resistance {resistance:.0f}")
        else:
            result["summary"].append(f"❌ Belum breakout: {pct_above:+.1f}% vs resistance {resistance:.0f} (butuh >+2%)")

    # --- Kriteria 3: Body candle 4H >5% ---
    # Dari video: "candle body (open to close distance) at least 5% on 4H timeframe"
    # Menggunakan 4H native Yahoo Finance — selaras sesi IDX tanpa agregasi manual
    try:
        hist_4h = get_ohlcv_4h(ticker, period="5d")
        if not hist_4h.empty and len(hist_4h) >= 2:
            result["criteria_checkable"] += 1
            last_bar = hist_4h.iloc[-1]
            body_pct = abs(last_bar["Close"] - last_bar["Open"]) / last_bar["Open"] * 100
            strong = body_pct >= 5.0
            result["candle_body_strong_4h"] = strong
            if strong:
                result["criteria_met"] += 1
                result["summary"].append(f"✅ Body candle 4H kuat: {body_pct:.1f}% (>=5%)")
            else:
                result["summary"].append(f"❌ Body candle 4H lemah: {body_pct:.1f}% (butuh >=5%)")
    except Exception:
        result["summary"].append("⚠️ Data 4H tidak tersedia")

    # --- Kriteria 5: Volume relatif >=1.5x ---
    vol_ratio = scoring.get("vol_ratio")
    if vol_ratio is not None:
        result["criteria_checkable"] += 1
        ok = vol_ratio >= 1.5
        result["relative_volume_ok"] = ok
        if ok:
            result["criteria_met"] += 1
            result["summary"].append(f"✅ Volume relatif tinggi: {vol_ratio}x (>=1.5x)")
        else:
            result["summary"].append(f"❌ Volume relatif rendah: {vol_ratio}x (butuh >=1.5x)")

    # --- Kriteria 6: Avg volume harian 500rb-1jt ---
    # Sudah difilter di ISSI whitelist (MIN_VOLUME_10D_AVG=500_000) — anggap pass
    # kalau saham sudah masuk whitelist. Untuk ticker di luar whitelist, cek dari
    # hist_daily kalau tersedia.
    if hist_daily is not None and not hist_daily.empty and len(hist_daily) >= 20:
        result["criteria_checkable"] += 1
        avg_vol = hist_daily["Volume"].tail(20).mean()
        ok = avg_vol >= 500_000
        result["avg_volume_ok"] = ok
        if ok:
            result["criteria_met"] += 1
            result["summary"].append(f"✅ Avg volume memadai: {int(avg_vol):,}/hari")
        else:
            result["summary"].append(f"❌ Avg volume terlalu rendah: {int(avg_vol):,}/hari (butuh >=500rb)")

    # --- Kriteria 7: Harga dalam 10% dari high 20 hari atau 50 hari ---
    # Dari video: "within 5-10% of 20-day, 50-day, or all-time high"
    if hist_daily is not None and not hist_daily.empty:
        result["criteria_checkable"] += 1
        high_20d = hist_daily["High"].tail(20).max() if len(hist_daily) >= 20 else None
        high_50d = hist_daily["High"].tail(50).max() if len(hist_daily) >= 50 else None
        near = False
        notes = []
        if high_20d:
            pct_from_20 = (high_20d - price) / high_20d * 100
            near = near or pct_from_20 <= 10.0
            notes.append(f"high20={high_20d:.0f} ({pct_from_20:.1f}% di bawah)")
        if high_50d:
            pct_from_50 = (high_50d - price) / high_50d * 100
            near = near or pct_from_50 <= 10.0
            notes.append(f"high50={high_50d:.0f} ({pct_from_50:.1f}% di bawah)")
        result["near_high"] = near
        if near:
            result["criteria_met"] += 1
            result["summary"].append(f"✅ Dekat high: {', '.join(notes)}")
        else:
            result["summary"].append(f"❌ Terlalu jauh dari high: {', '.join(notes)} (butuh <=10%)")

    # --- Kriteria 8: Di atas MA20 dan MA50 ---
    above_ema21 = not scoring.get("is_below_ema21", True)
    above_sma50 = not scoring.get("is_below_sma50", True)
    result["criteria_checkable"] += 1
    both_above = above_ema21 and above_sma50
    result["above_ma20_and_ma50"] = both_above
    if both_above:
        result["criteria_met"] += 1
        result["summary"].append("✅ Di atas EMA21 dan SMA50")
    elif above_ema21:
        result["summary"].append("⚠️ Di atas EMA21, tapi di bawah SMA50")
    elif above_sma50:
        result["summary"].append("⚠️ Di atas SMA50, tapi di bawah EMA21")
    else:
        result["summary"].append("❌ Di bawah EMA21 dan SMA50")

    # High conviction kalau >= 5 dari kriteria yang bisa dicek terpenuhi
    checkable = result["criteria_checkable"]
    met = result["criteria_met"]
    threshold = max(5, round(checkable * 0.7))  # 70% dari yang bisa dicek, min 5
    result["is_high_conviction"] = met >= threshold
    result["conviction_label"] = (
        "🔥 HIGH CONVICTION" if met >= threshold
        else f"⚪ Low conviction ({met}/{checkable} kriteria)"
    )
    return result


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
    - adx_component (15%): ADX TINGGI = kriteria POSITIF (beda dari swing
      scoring, di sana ADX rendah jadi pengali kepercayaan yang MENGURANGI
      skor) — day trade justru mencari saham dengan tren jelas.
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

    adx_component = min(10.0, (scoring.get("adx", 0) or 0) / 4.0)

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


def compute_brokersum_priority(scoring: dict, total_stock_value: float = 0) -> float:
    """
    Ranks a stock's priority for spending scarce daily Index Alpha budget (5
    calls/day, 1 call/ticker = 5 tickers/day max). Combines:
    - urgency: how close price is to cut_loss or take_profit (closer = more
      time-sensitive, a wrong call here costs money NOW)
    - volatility: day_range_pct_10d — is this stock actively "in play"
    - ambiguity: MIXED_SIGNALS / active OBV divergence / overbought caution —
      cases where our own proxy is ALREADY uncertain, so real data resolves the
      most (a quiet stock with OBV divergence can be MORE worth checking than an
      obviously trending one — see the ANTM case: flat net masked real activity)
    - size: bigger positions deserve scrutiny regardless of how exciting the
      technicals look, since more capital is on the line if the call is wrong

    Watchlist entries (no position yet) get a flat modest size baseline.
    """
    price = scoring.get("price", 0)
    targets = scoring.get("targets", {})
    tp1 = targets.get("tp_1")
    cut_loss = targets.get("cut_loss")

    if price and tp1 and cut_loss:
        dist_to_cutloss_pct = abs((price - cut_loss) / price * 100)
        dist_to_tp_pct = abs((tp1 - price) / price * 100)
        closest_dist_pct = min(dist_to_cutloss_pct, dist_to_tp_pct)
        urgency = max(0.0, 10.0 - closest_dist_pct)
    else:
        urgency = 0.0

    volatility = min(10.0, scoring.get("day_range_pct_10d", 0) or 0)

    ambiguity = 0.0
    if scoring.get("action_id") == "MIXED_SIGNALS":
        ambiguity += 5.0
    if scoring.get("obv_divergence") in ("bearish_divergence", "bullish_divergence"):
        ambiguity += 3.0
    if scoring.get("is_overbought_caution"):
        ambiguity += 2.0
    ambiguity = min(10.0, ambiguity)

    market_value = scoring.get("market_value_idr")
    if market_value and total_stock_value > 0:
        size = min(10.0, (market_value / total_stock_value) * 10)
    else:
        size = 3.0  # flat baseline for watchlist (not owned yet, no position size)

    priority = urgency * 0.35 + volatility * 0.25 + ambiguity * 0.30 + size * 0.10
    return round(priority, 2)


def _apply_brokersum_adjustment_original(scoring: dict, brokersum: dict) -> dict:
    """
    Lets REAL broker flow data actually influence the score and decision, not
    just sit alongside it as narrative context. Only adjusts when
    broker_concentration_pct >= 10% — below that, the signal is too diffuse/
    noisy to trust (mirrors the ANTM lesson: concentration is what makes this
    data meaningful, not raw net flow alone).

    Adjustment is capped at ±3 points on sentiment_score, scaled by how
    concentrated the flow is (full weight at 40%+ concentration). At
    sentiment's 0.3 weight on final_score, that bounds the total swing to
    about ±0.9 on final_score — real influence, not something that alone
    flips a HOLD into a STRONG BUY by itself.

    Re-runs decide_action() with the adjusted scores so the actual decision
    (not just the displayed number) reflects real data when available.
    """
    concentration_pct = brokersum.get("broker_concentration_pct", 0)
    if concentration_pct < 10:
        scoring["brokersum_adjusted"] = False
        return scoring

    net_foreign_flow_pct = brokersum.get("net_foreign_flow_pct", 0)
    concentration_factor = min(1.0, concentration_pct / 40)
    adjustment = (net_foreign_flow_pct / 100) * 3.0 * concentration_factor

    old_sentiment = scoring["scores"]["sentiment"]
    new_sentiment = max(1.0, min(10.0, old_sentiment + adjustment))

    value_score = scoring["scores"]["value"]
    momentum_score = scoring["scores"]["momentum"]
    new_final = (value_score * 0.25) + (momentum_score * 0.45) + (new_sentiment * 0.30)

    decision = decide_action(
        final_score=new_final, value_score=value_score, momentum_score=momentum_score,
        sentiment_score=new_sentiment, is_financial_distress_flag=scoring.get("is_financial_distress_flag", False),
        chart_pattern=scoring.get("chart_pattern", "none"), is_overbought_caution=scoring.get("is_overbought_caution", False),
        obv_divergence=scoring.get("obv_divergence", "none"), is_volume_spike_anomaly=scoring.get("is_volume_spike_anomaly", False),
        is_near_price_floor=scoring.get("is_near_price_floor", False), is_unusually_low_pe=scoring.get("is_unusually_low_pe", False),
        macd_bearish_cross=scoring.get("macd_bearish_cross", False), is_below_sma50=scoring.get("is_below_sma50", False),
    )

    scoring["scores"]["sentiment"] = round(new_sentiment, 1)
    scoring["scores"]["final"] = round(new_final, 1)
    scoring["action_id"] = decision["action_id"]
    scoring["action_label_id"] = decision["action_label_id"]
    scoring["action_ceiling_applied"] = decision["ceiling_applied"]
    scoring["action_component_spread"] = decision["component_spread"]
    scoring["brokersum_adjusted"] = True
    scoring["brokersum_adjustment_applied"] = round(adjustment, 2)
    return scoring




def apply_brokersum_adjustment(scoring: dict, brokersum: dict) -> dict:
    """
    Wrapper broker-flow adjustment.

    Untuk source lama / Index Alpha / Zapi foreign flow:
    tetap pakai logic original.

    Untuk screenshot Broker Summary ALL 3D:
    gunakan smart_money_confirmation_score sebagai confirmation layer,
    bukan net foreign flow aggregate, karena ALL tab aggregate net normalnya 0.
    """
    if not brokersum:
        return scoring

    flow_scope = brokersum.get("flow_scope")

    if flow_scope != "ALL_BROKERS_3D":
        return _apply_brokersum_adjustment_original(scoring, brokersum)

    adjusted = dict(scoring)

    try:
        sm_score = int(float(brokersum.get("smart_money_confirmation_score", 0) or 0))
    except Exception:
        sm_score = 0

    sm_score = max(-15, min(15, sm_score))
    flow_label = brokersum.get("smart_money_flow_label", "NEUTRAL")
    reasons = brokersum.get("smart_money_reasons") or []

    old_sentiment = float(adjusted.get("sentiment_score", 0) or 0)
    old_final = float(adjusted.get("final_score", 0) or 0)

    # Konversi score -15..+15 ke adjustment konservatif.
    # Sentiment bergerak lebih besar, final lebih kecil agar tidak override teknikal.
    sentiment_delta = round(sm_score / 15.0 * 1.2, 2)

    if sm_score >= 0:
        final_delta = round(sm_score / 15.0 * 0.60, 2)
    else:
        final_delta = round(sm_score / 15.0 * 0.80, 2)

    new_sentiment = max(1.0, min(10.0, old_sentiment + sentiment_delta))
    new_final = max(1.0, min(10.0, old_final + final_delta))

    adjusted["sentiment_score"] = round(new_sentiment, 2)
    adjusted["final_score"] = round(new_final, 2)

    adjusted["brokersum_adjusted"] = True
    adjusted["brokersum_source"] = "screenshot_all_3d"
    adjusted["smart_money_confirmation_score"] = sm_score
    adjusted["smart_money_flow_label"] = flow_label
    adjusted["smart_money_reasons"] = reasons
    adjusted["smart_money_final_delta"] = final_delta
    adjusted["smart_money_sentiment_delta"] = sentiment_delta
    adjusted["broker_concentration_pct"] = brokersum.get("broker_concentration_pct")
    adjusted["top3_buy_concentration_pct"] = brokersum.get("top3_buy_concentration_pct")
    adjusted["top3_sell_concentration_pct"] = brokersum.get("top3_sell_concentration_pct")
    adjusted["dominant_buyer_avg"] = brokersum.get("dominant_buyer_avg")
    adjusted["top_net_buyers"] = brokersum.get("top_net_buyers", [])
    adjusted["top_net_sellers"] = brokersum.get("top_net_sellers", [])

    # Recompute deterministic action setelah final/sentiment berubah.
    try:
        action = decide_action(
            adjusted.get("final_score", 0),
            adjusted.get("value_score", 0),
            adjusted.get("momentum_score", 0),
            adjusted.get("sentiment_score", 0),
            adjusted.get("is_financial_distress_flag", False),
            adjusted.get("chart_pattern"),
            adjusted.get("is_overbought_caution", False),
            adjusted.get("obv_divergence", "none"),
            adjusted.get("is_volume_spike_anomaly", False),
            adjusted.get("is_near_price_floor", False),
            adjusted.get("is_unusually_low_pe", False),
            adjusted.get("macd_bearish_cross", False),
            adjusted.get("is_below_sma50", False),
        )

        if isinstance(action, dict):
            adjusted.update(action)
        elif isinstance(action, str):
            adjusted["action"] = action
            adjusted["action_label_id"] = ACTION_LABEL_ID.get(action, action)

    except Exception as e:
        print(f"⚠️ Gagal recompute action setelah Smart Money ALL 3D: {str(e)[:120]}")

    return adjusted


def extract_brokersum_from_screenshot(image_bytes: bytes, mime_type: str, expected_ticker: str) -> dict:
    """
    Uses Gemini's vision capability to read a Broker Sum screenshot from the
    user's own trading app — free, doesn't touch Index Alpha's quota at all.
    Deliberately conservative: if the image doesn't clearly show real Broker Sum
    data matching the expected ticker, returns success=False rather than
    guessing — a misread number here would silently feed into real score
    adjustment (apply_brokersum_adjustment), so this must fail loudly, not
    quietly produce a plausible-looking wrong number.

    NOTE: not live-tested against the real Gemini vision API from this
    environment (no network access to verify) — built from the documented
    google-genai SDK pattern. Verify on first real use.
    """
    extraction_prompt = f"""
You are extracting data from a screenshot of an Indonesian stock trading app's
"Broker Sum" (broker summary) screen for ticker {expected_ticker}.

Return ONLY valid JSON, no markdown, no commentary, in this exact shape:
{{
  "success": true or false,
  "ticker_visible": "the ticker shown in the image, or null",
  "reason_if_failed": "brief reason if success=false, else null",
  "foreign_buy_value_idr": number or null,
  "foreign_sell_value_idr": number or null,
  "foreign_net_value_idr": number or null,
  "top_brokers": [{{"code": "XX", "net_idr": number}}, ...] or []
}}

REQUIRED for success=true (this is the only thing that actually matters — the
AGGREGATE totals, usually under a "Total Value" or "Foreign" section header):
- foreign_buy_value_idr and foreign_sell_value_idr (or foreign_net_value_idr
  directly if that's what's shown). These are the ONLY fields required for a
  successful extraction.

OPTIONAL, NEVER a reason to fail the whole extraction:
- "top_brokers" (per-broker monetary net breakdown) is a NICE-TO-HAVE bonus,
  not a requirement. Many broker summary screens only show VOLUME (lot count)
  per broker, not a monetary value per broker — that is completely normal and
  expected. If you cannot determine a reliable Rupiah net_idr value per
  individual broker, simply return "top_brokers": [] (empty list) and still
  set success=true, AS LONG AS the required aggregate totals above are
  readable. Do NOT fail the entire extraction just because top_brokers can't
  be populated — that field existing or not has no bearing on success.

ASSUMPTION — the user sends screenshots from the "ALL" tab, 3 trading days,
Net mode active. This means the aggregate Buy/Sell/Net values are ALL-broker
values, NOT foreign-only values. For backward compatibility with existing bot
code, still return these aggregate ALL values in the legacy JSON keys named
foreign_buy_value_idr, foreign_sell_value_idr, and foreign_net_value_idr.
Interpret those keys as:
- foreign_buy_value_idr  = aggregate ALL buy value
- foreign_sell_value_idr = aggregate ALL sell value
- foreign_net_value_idr  = aggregate ALL net value

Broker breakdown is important for bandarmology:
- Read visible top BUY broker codes, their buy volume/lot, and average buy price.
- Read visible top SELL broker codes, their sell volume/lot, and average sell price.
- If monetary value per broker is not shown, estimate broker value as:
  volume_lot x 100 x average_price.
- For top_brokers, use positive net_idr for buyers and negative net_idr for sellers.
- Include broker avg price and volume_lot if readable.
- Do NOT fail just because some broker-level details are incomplete, as long as
  aggregate Buy/Sell/Net is readable.
- Do NOT classify a broker as "bandar" only from its code. Just extract the data.

NUMBER FORMAT — Indonesian trading apps commonly show values with suffixes.
These are FOUR DISTINCT magnitudes, do not confuse them:
- "K" = thousand = x1,000 (e.g. "1.35 K" = 1,350)
- "M" = million = x1,000,000 (e.g. "93.10 M" = 93,100,000)
- "B" = billion = x1,000,000,000 (e.g. "51.65 B" = 51,650,000,000)
- "T" = trillion = x1,000,000,000,000 (e.g. "2.49 T" = 2,490,000,000,000)
B and T are NOT the same magnitude — T is 1,000x larger than B. Always convert
to the FULL raw number in the JSON output using the correct multiplier above.

success=false ONLY if: the image isn't clearly a Broker Sum screen, the ticker
shown doesn't match "{expected_ticker}", OR the REQUIRED aggregate buy/sell/net
values above are genuinely illegible/not visible. Do not guess a number you
can't actually read — leave that specific field null and explain in
reason_if_failed. But do not fail success on top_brokers alone.
"""
    try:
        raw_text = _gemini_image_text(image_bytes, mime_type, extraction_prompt).strip()
        # Strip markdown code fences if the model added them despite instructions
        raw_text = re.sub(r"^```(?:json)?\s*|\s*```$", "", raw_text, flags=re.MULTILINE).strip()
        result = json.loads(raw_text)
        return result
    except json.JSONDecodeError as e:
        print(f"⚠️ Brokersum screenshot extraction returned non-JSON: {e}")
        return {"success": False, "reason_if_failed": "Gagal memproses respons AI — coba lagi atau kirim screenshot yang lebih jelas."}
    except Exception as e:
        print(f"⚠️ Brokersum screenshot extraction failed: {e}")
        return {"success": False, "reason_if_failed": f"Error teknis: {str(e)[:100]}"}


def compute_brokersum_from_screenshot_data(extracted: dict) -> dict:
    """
    Converts a successfully-extracted screenshot into the SAME shape
    compute_brokersum_metrics() produces, so it can feed into the identical
    apply_brokersum_adjustment() / decide_action() pipeline as Index Alpha data
    — no separate scoring logic needed for screenshot-sourced data.

    Returns None (NOT a fake 0% result) if the core buy/sell/net values are all
    genuinely missing — treating "couldn't read the number" the same as "the
    number is exactly zero" was a real bug (confirmed on a real RALS screenshot:
    silently produced 0% net flow when the actual data showed +Rp51.65M). A
    missing number must fail loudly here, not quietly become a wrong number
    that then gets used to adjust the real score.
    """
    foreign_buy_raw = extracted.get("foreign_buy_value_idr")
    foreign_sell_raw = extracted.get("foreign_sell_value_idr")
    net_foreign_raw = extracted.get("foreign_net_value_idr")

    # Fail loudly if there's genuinely nothing to work with — don't let "unknown"
    # silently become "zero", which is a completely different, misleading claim.
    if net_foreign_raw is None and foreign_buy_raw is None and foreign_sell_raw is None:
        return None

    foreign_buy = foreign_buy_raw or 0
    foreign_sell = foreign_sell_raw or 0
    net_foreign = net_foreign_raw if net_foreign_raw is not None else (foreign_buy - foreign_sell)
    foreign_gross = foreign_buy + foreign_sell

    net_foreign_flow_pct = (net_foreign / foreign_gross * 100) if foreign_gross > 0 else 0

    top_brokers = extracted.get("top_brokers") or []
    top_3_net_buy = sum(b.get("net_idr", 0) for b in sorted(top_brokers, key=lambda x: x.get("net_idr", 0), reverse=True)[:3] if b.get("net_idr", 0) > 0)
    broker_concentration_pct = (top_3_net_buy / foreign_gross * 100) if foreign_gross > 0 else 0

    return {
        "ticker": extracted.get("ticker_visible"),
        "source": "screenshot",  # distinguishes from Index Alpha's "api" source for transparency
        "flow_scope": "ALL_BROKERS_3D",
        "net_all_flow_idr": int(net_foreign),
        # Diasumsikan 7 hari (kalender) sesuai komitmen user untuk selalu mengambil
        # screenshot dengan window 5 hari bursa / 7 hari kalender — sama dengan
        # BROKERSUM_LOOKBACK_DAYS yang dipakai Index Alpha, supaya data dari kedua
        # sumber tetap sebanding/konsisten saat ditampilkan atau dibandingkan tren.
        "lookback_days": BROKERSUM_LOOKBACK_DAYS,
        "net_foreign_flow_idr": int(net_foreign),
        "net_foreign_flow_pct": round(net_foreign_flow_pct, 1),
        "broker_concentration_pct": round(broker_concentration_pct, 1),
        "proxy_agreement": "not_available",  # computed separately once merged with cmf/obv context
        "top_net_buyers": sorted([b for b in top_brokers if b.get("net_idr", 0) > 0], key=lambda x: x["net_idr"], reverse=True)[:3],
        "top_net_sellers": sorted([b for b in top_brokers if b.get("net_idr", 0) < 0], key=lambda x: x["net_idr"])[:3],
    }


def _load_brokersum_history() -> dict:
    if not os.path.exists(BROKERSUM_HISTORY_FILE):
        return {}
    try:
        with open(BROKERSUM_HISTORY_FILE) as f:
            return json.load(f)
    except Exception as e:
        print(f"⚠️ Gagal membaca brokersum history: {e}")
        return {}


def _save_brokersum_history(history: dict):
    try:
        with open(BROKERSUM_HISTORY_FILE, "w") as f:
            json.dump(history, f, indent=2)
    except Exception as e:
        print(f"⚠️ Gagal menyimpan brokersum history: {e}")


def append_brokersum_history(ticker: str, brokersum: dict):
    """
    Menyimpan HASIL BARU sebagai entri baru (bukan menimpa yang lama) — supaya
    riwayat menumpuk seiring waktu lewat pemakaian rutin, tanpa panggilan API
    tambahan sama sekali. Dipangkas ke BROKERSUM_HISTORY_MAX_ENTRIES_PER_TICKER
    entri terbaru per ticker agar file tidak membengkak tanpa batas.
    """
    history = _load_brokersum_history()
    entries = history.setdefault(ticker, [])
    entries.append({
        "date": get_last_published_trading_day(),
        "net_foreign_flow_idr": brokersum.get("net_foreign_flow_idr"),
        "net_foreign_flow_pct": brokersum.get("net_foreign_flow_pct"),
        "broker_concentration_pct": brokersum.get("broker_concentration_pct"),
    })
    # Dedup: kalau entri untuk tanggal yang sama sudah ada (dipanggil >1x di hari
    # bursa yang sama), timpa yang lama, jangan duplikat.
    seen_dates = {}
    for e in entries:
        seen_dates[e["date"]] = e
    deduped = sorted(seen_dates.values(), key=lambda e: e["date"])
    history[ticker] = deduped[-BROKERSUM_HISTORY_MAX_ENTRIES_PER_TICKER:]
    _save_brokersum_history(history)


def compute_brokersum_trend(ticker: str, current_net_flow_idr) -> dict:
    """
    Membandingkan hasil HARI INI vs entri log SEBELUMNYA untuk ticker yang sama —
    memberi sinyal ARAH TREN (menguat/melemah/stabil), BUKAN angka "net hari ini"
    yang presisi. Ini penting dijelaskan jujur: karena setiap panggilan adalah
    jumlah bergulir 7 hari, selisih dua panggilan berurutan = (kontribusi hari
    baru) - (kontribusi hari yang "jatuh" dari ujung window lama) — bukan murni
    net satu hari itu sendiri. Hanya diberi label "hari bursa sebelumnya" (bukan
    tren jangka panjang) kalau entri pembanding benar dari 1 hari bursa sebelum
    hari ini — kalau gap-nya lebih jauh, interpretasi "pergeseran 1 hari" tidak
    berlaku lagi, jadi ditandai sebagai perbandingan histori biasa saja.
    """
    history = _load_brokersum_history()
    entries = history.get(ticker, [])
    if not entries:
        return {"trend": "tidak_ada_histori", "delta_idr": None, "is_single_day_shift": False}

    today = get_last_published_trading_day()
    prior_entries = [e for e in entries if e["date"] < today]
    if not prior_entries:
        return {"trend": "tidak_ada_histori", "delta_idr": None, "is_single_day_shift": False}

    most_recent = prior_entries[-1]
    delta_idr = current_net_flow_idr - (most_recent.get("net_foreign_flow_idr") or 0)

    # Cek apakah entri pembanding ini benar dari 1 hari bursa sebelumnya (bukan
    # gap beberapa hari/minggu) — untuk menentukan apakah framing "pergeseran
    # window 1 hari" masih valid secara wajar.
    today_dt = datetime.datetime.strptime(today, "%Y-%m-%d").date()
    prior_dt = datetime.datetime.strptime(most_recent["date"], "%Y-%m-%d").date()
    gap_calendar_days = (today_dt - prior_dt).days
    is_single_day_shift = gap_calendar_days <= 3  # toleransi akhir pekan

    total_gross_approx = abs(current_net_flow_idr) + abs(most_recent.get("net_foreign_flow_idr") or 0)
    if total_gross_approx == 0:
        trend = "STABIL"
    else:
        delta_ratio = delta_idr / max(total_gross_approx, 1)
        if delta_ratio > 0.15:
            trend = "MENGUAT"
        elif delta_ratio < -0.15:
            trend = "MELEMAH"
        else:
            trend = "STABIL"

    return {
        "trend": trend, "delta_idr": int(delta_idr),
        "is_single_day_shift": is_single_day_shift,
        "compared_to_date": most_recent["date"],
    }


ZAPI_API_KEY  = os.environ.get("ZAPI_API_KEY", "")
ZAPI_HEADERS  = {"x-api-key": ZAPI_API_KEY}
ZAPI_BASE_URL = os.environ.get("ZAPI_BASE_URL", "https://api.zpi.web.id/v1")


def fetch_zapi_stock_summary(ticker: str, date_str: str = None) -> dict:
    """
    Sumber brokersum ALTERNATIF via Zapi (finance:idx/stock-summary) — TERPISAH
    dari Index Alpha, tidak menggantikan. Belum diverifikasi apakah update
    genuinely intraday atau harian (verifikasi live-update sedang berjalan) —
    diperlakukan sebagai data EOD sampai terbukti sebaliknya, jadi HANYA dipakai
    di /check dan /myportfolio (horizon swing), TIDAK di /screendaytrade — sama
    alasan strukturalnya dengan kenapa Index Alpha juga tidak dipakai di situ.

    CATATAN PENTING: ForeignBuy/ForeignSell dari Zapi adalah VOLUME LEMBAR,
    BUKAN nilai Rupiah (beda dari Index Alpha yang langsung kasih value) —
    net_foreign_flow_idr di sini adalah ESTIMASI (volume x harga penutupan),
    bukan angka pasti seperti dari Index Alpha.
    """
    try:
        params = {"length": "1", "start": "0", "code": ticker}
        if date_str:
            params["date"] = date_str
        resp = requests.get(
            f"{ZAPI_BASE_URL}/finance:idx/stock-summary",
            params=params, headers=ZAPI_HEADERS, timeout=20,
        )
        data = resp.json()
        rows = data.get("data", {}).get("data", [])
        if not rows:
            print(f"⚠️ Zapi stock-summary: tidak ada data untuk {ticker}")
            return None
        return rows[0]
    except Exception as e:
        print(f"⚠️ Zapi stock-summary fetch gagal untuk {ticker}: {e}")
        return None


def compute_brokersum_metrics_zapi(ticker: str, cmf=None, obv_divergence=None) -> dict:
    """
    Versi Zapi dari compute_brokersum_metrics() — struktur output SAMA persis
    supaya kompatibel dengan apply_brokersum_adjustment()/decide_action() yang
    sama. Tidak ada breakdown per-broker (top_net_buyers/sellers) dari sumber
    ini, karena stock-summary hanya kasih total foreign buy/sell per ticker,
    bukan per broker seperti Index Alpha.

    SUDAH DIKONFIRMASI (jangan dieksplorasi ulang): endpoint finance:idx/
    broker-summary Zapi TIDAK BISA difilter per-saham dalam kondisi apa pun —
    diuji langsung: code=BBCA vs code=TLKM menghasilkan recordsTotal dan angka
    Volume per broker yang IDENTIK PERSIS (data gabungan market-wide, bukan
    per-saham). Dicoba juga 4 nama parameter alternatif (stockCode, symbol,
    ticker, stock) — semua diterima tanpa error tapi diam-diam diabaikan,
    hasilnya tetap sama. Kesimpulan: endpoint ini secara desain untuk
    "broker paling aktif se-pasar hari ini", bukan "siapa yang menggerakkan
    saham X" — bukan keterbatasan yang bisa disiasati dengan parameter yang
    tepat. Breakdown broker per-saham HANYA tersedia dari Index Alpha.
    """
    row = fetch_zapi_stock_summary(ticker)
    if row is None:
        return None

    foreign_buy_shares = row.get("ForeignBuy", 0) or 0
    foreign_sell_shares = row.get("ForeignSell", 0) or 0
    close_price = row.get("Close", 0) or 0
    foreign_gross_shares = foreign_buy_shares + foreign_sell_shares

    net_foreign_shares = foreign_buy_shares - foreign_sell_shares
    net_foreign_flow_pct = (net_foreign_shares / foreign_gross_shares * 100) if foreign_gross_shares > 0 else 0
    net_foreign_flow_idr_estimate = int(net_foreign_shares * close_price)

    # Broker concentration tidak tersedia dari sumber ini (bukan breakdown per
    # broker) — default 0 supaya apply_brokersum_adjustment() otomatis skip
    # penyesuaian skor (threshold minimal 10%), tetap tampil sebagai info saja.
    broker_concentration_pct = 0

    proxy_agreement = "not_available"
    if cmf is not None and isinstance(cmf, (int, float)):
        proxy_bullish = cmf > 0
        real_bullish = net_foreign_flow_pct > 0
        if obv_divergence == "bearish_divergence" and net_foreign_flow_pct > 5:
            proxy_agreement = "CONTRADICTION: proxy showed bearish OBV divergence but real broker flow (Zapi) is net positive"
        elif obv_divergence == "bullish_divergence" and net_foreign_flow_pct < -5:
            proxy_agreement = "CONTRADICTION: proxy showed bullish OBV divergence but real broker flow (Zapi) is net negative"
        elif proxy_bullish == real_bullish:
            proxy_agreement = "confirms_proxy"
        else:
            proxy_agreement = "diverges_from_proxy"

    return {
        "ticker": ticker,
        "source": "zapi",
        "lookback_days": 1,  # stock-summary hanya kasih snapshot 1 hari, bukan agregat multi-hari seperti Index Alpha
        "net_foreign_flow_idr": net_foreign_flow_idr_estimate,
        "net_foreign_flow_idr_is_estimate": True,  # transparansi: ini estimasi (volume x close), bukan value asli
        "net_foreign_flow_pct": round(net_foreign_flow_pct, 1),
        "broker_concentration_pct": broker_concentration_pct,
        "proxy_agreement": proxy_agreement,
        "top_net_buyers": [],
        "top_net_sellers": [],
        # Field tambahan yang khas dari Zapi, tidak ada di Index Alpha:
        "bid": row.get("Bid"), "bid_volume": row.get("BidVolume"),
        "offer": row.get("Offer"), "offer_volume": row.get("OfferVolume"),
        "non_regular_volume": row.get("NonRegularVolume"),
        "non_regular_value": row.get("NonRegularValue"),
        "listed_shares": row.get("ListedShares"),
        "tradeble_shares": row.get("TradebleShares"),
    }


def get_cached_or_fetch_brokersum(ticker: str, cmf=None, obv_divergence=None, lookback_days=BROKERSUM_LOOKBACK_DAYS):
    """
    Used by /myportfolio brokersum — checks the cache first, keyed by the last
    PUBLISHED trading day (not raw calendar date), so running this Sunday night
    and again Monday morning both correctly hit the same cache entry (both are
    really showing Friday's data until Monday's own data publishes at 19:00 WIB) —
    avoiding a wasted API call for data that hasn't actually changed. Fetches
    fresh only on a genuine cache miss, and writes the result back for /check to
    pick up later for free.
    """
    cached = get_cached_brokersum(ticker)
    if cached is not None:
        print(f"📋 Using cached brokersum for {ticker} (same trading day as last fetch)")
        return cached

    result = compute_brokersum_metrics(ticker, cmf, obv_divergence, lookback_days)
    if result is not None:
        result["trend"] = compute_brokersum_trend(ticker, result["net_foreign_flow_idr"])
        cache = _load_brokersum_cache()
        cache[ticker] = {"date": get_last_published_trading_day(), "data": result}
        _save_brokersum_cache(cache)
        append_brokersum_history(ticker, result)  # hanya saat fetch BARU, bukan cache hit
    return result


def compute_brokersum_metrics(ticker: str, cmf=None, obv_divergence=None, lookback_days=BROKERSUM_LOOKBACK_DAYS):
    """
    Computes real, derived broker-flow metrics for one ticker over a multi-day
    window — 1 API call (investor=f only). Reduced from the original 2-call
    (foreign+all) design after discovering Index Alpha's free tier has a HARD
    5 requests/day cap (confirmed via a real 403 rejection), not just the 150/
    month pool we originally designed around. At 1 call/ticker this supports
    5 tickers/day instead of 2 — real coverage is more valuable than the
    domestic-comparison depth we're giving up (flow_agreement, foreign
    participation %), given how scarce daily calls are.

    Every field here is a genuine computed ratio from real data, never a
    fabricated score/confidence. Returns None if data wasn't available.
    """
    to_date = datetime.datetime.now(WIB).date()
    from_date = to_date - datetime.timedelta(days=lookback_days + 4)  # pad for weekends
    from_str, to_str = from_date.strftime("%Y-%m-%d"), to_date.strftime("%Y-%m-%d")

    foreign_brokers = fetch_broker_summary_raw(ticker, from_str, to_str, investor="f")
    if not foreign_brokers:
        return None

    foreign_buy = sum(b.get("buy_value", 0) for b in foreign_brokers)
    foreign_sell = sum(b.get("sell_value", 0) for b in foreign_brokers)
    net_foreign = foreign_buy - foreign_sell
    foreign_gross = foreign_buy + foreign_sell

    net_foreign_flow_pct = (net_foreign / foreign_gross * 100) if foreign_gross > 0 else 0

    # Concentration within broker flow specifically (can't compare vs total market
    # without the second call anymore) — top-3 net foreign buyers vs total foreign
    # gross activity. Still catches "a few players moving decisively" vs "diffuse,
    # low-conviction foreign activity" even at this narrower scope.
    net_by_broker = sorted(
        [(b["code"], b.get("buy_value", 0) - b.get("sell_value", 0)) for b in foreign_brokers],
        key=lambda x: x[1], reverse=True,
    )
    top_3_net_buy = sum(v for _, v in net_by_broker[:3] if v > 0)
    broker_concentration_pct = (top_3_net_buy / foreign_gross * 100) if foreign_gross > 0 else 0

    # Transaction pattern: uses buy_freq/sell_freq (transaction COUNT), previously
    # fetched but discarded. Compares each top broker's average transaction size
    # against the OVERALL average for this stock (adaptive, not a fixed threshold —
    # same principle as the adaptive RSI/volume scoring elsewhere) to distinguish
    # genuine gradual accumulation (many small orders — the real bandarmology
    # pattern) from a one-off block trade (few large orders — weaker signal for
    # continuation, often just institutional rebalancing).
    total_foreign_freq = sum(b.get("buy_freq", 0) + b.get("sell_freq", 0) for b in foreign_brokers)
    overall_avg_size = (foreign_gross / total_foreign_freq) if total_foreign_freq > 0 else 0

    broker_lookup = {b["code"]: b for b in foreign_brokers}

    def classify_pattern(code):
        b = broker_lookup.get(code, {})
        freq = b.get("buy_freq", 0) + b.get("sell_freq", 0)
        value = b.get("buy_value", 0) + b.get("sell_value", 0)
        if freq == 0 or overall_avg_size == 0:
            return "tidak_diketahui"
        avg_size = value / freq
        ratio = avg_size / overall_avg_size
        if ratio < 0.7:
            return "akumulasi_bertahap"  # many small orders relative to the norm
        elif ratio > 1.5:
            return "transaksi_block"  # few large orders — likely one-off/rebalancing
        else:
            return "normal"

    top_net_buyers = [
        {"code": c, "net_idr": int(v), "pattern": classify_pattern(c)}
        for c, v in net_by_broker[:3] if v > 0
    ]
    top_net_sellers = [
        {"code": c, "net_idr": int(v), "pattern": classify_pattern(c)}
        for c, v in net_by_broker[-3:] if v < 0
    ]

    # Compare against our own existing technical proxies — never silently pick one
    # side when they disagree, same discipline as the news-dismissal rule elsewhere.
    proxy_agreement = "not_available"
    if cmf is not None and isinstance(cmf, (int, float)):
        proxy_bullish = cmf > 0
        real_bullish = net_foreign_flow_pct > 0
        if obv_divergence == "bearish_divergence" and net_foreign_flow_pct > 5:
            proxy_agreement = "CONTRADICTION: proxy showed bearish OBV divergence but real broker flow is net positive"
        elif obv_divergence == "bullish_divergence" and net_foreign_flow_pct < -5:
            proxy_agreement = "CONTRADICTION: proxy showed bullish OBV divergence but real broker flow is net negative"
        elif proxy_bullish == real_bullish:
            proxy_agreement = "confirms_proxy"
        else:
            proxy_agreement = "diverges_from_proxy"

    return {
        "ticker": ticker,
        "lookback_days": lookback_days,
        "net_foreign_flow_idr": int(net_foreign),
        "net_foreign_flow_pct": round(net_foreign_flow_pct, 1),
        "broker_concentration_pct": round(broker_concentration_pct, 1),
        "proxy_agreement": proxy_agreement,
        "top_net_buyers": top_net_buyers,
        "top_net_sellers": top_net_sellers,
    }


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
ACTION_RANK = {"AVOID_SELL": 0, "HOLD": 1, "BUY_ACCUMULATE": 2, "STRONG_BUY": 3}
ACTION_LABEL_ID = {
    "STRONG_BUY": "BELI KUAT",
    "BUY_ACCUMULATE": "BELI / AKUMULASI",
    "HOLD": "TAHAN",
    "AVOID_SELL": "HINDARI / JUAL",
    "MIXED_SIGNALS": "SINYAL CAMPURAN",
}


def decide_action(final_score, value_score, momentum_score, sentiment_score,
                   is_financial_distress_flag, chart_pattern, is_overbought_caution,
                   obv_divergence, is_volume_spike_anomaly, is_near_price_floor,
                   is_unusually_low_pe, macd_bearish_cross=False, is_below_sma50=False):
    # 1. Base tier from the blended score
    if final_score >= 7.5:
        base_tier = "STRONG_BUY"
    elif final_score >= 6.0:
        base_tier = "BUY_ACCUMULATE"
    elif final_score >= 4.5:
        base_tier = "HOLD"
    else:
        base_tier = "AVOID_SELL"

    # 2. Hard ceiling from serious flags — these override the score, not just adjust it
    if is_financial_distress_flag or chart_pattern == "lower_highs_bearish":
        ceiling = "HOLD"
    elif (is_overbought_caution or obv_divergence == "bearish_divergence"
          or is_volume_spike_anomaly or macd_bearish_cross):
        # MACD bearish crossover = momentum turning down RIGHT NOW — a fresh timing
        # signal, treated the same as our other "don't give this a STRONG BUY" flags.
        ceiling = "BUY_ACCUMULATE"
    else:
        ceiling = "STRONG_BUY"

    tier = base_tier if ACTION_RANK[base_tier] <= ACTION_RANK[ceiling] else ceiling

    # 3. Component disagreement: if value/momentum/sentiment strongly disagree with
    # each other, a single blended number is hiding real tension — surface it honestly
    # instead of picking a confident label that averages over a real conflict.
    component_spread = max(value_score, momentum_score, sentiment_score) - min(value_score, momentum_score, sentiment_score)
    strong_disagreement = component_spread >= 4.0

    # 4. Borderline buffer near the score cliffs — avoids false precision where 7.49
    # vs 7.51 would otherwise flip confidently between two very different labels.
    thresholds = [4.5, 6.0, 7.5]
    is_borderline = any(abs(final_score - t) < 0.3 for t in thresholds)
    # is_below_sma50 (still in a weaker medium-term trend) joins the other soft flags —
    # relevant for swing-trade framing since a good short-term setup inside a still-
    # weak medium-term regime is exactly the kind of tension worth surfacing.
    soft_flags_present = is_near_price_floor or is_unusually_low_pe or is_overbought_caution or is_below_sma50

    mixed_signals = strong_disagreement or (is_borderline and soft_flags_present)
    final_action = "MIXED_SIGNALS" if mixed_signals else tier

    return {
        "action_id": final_action,
        "action_label_id": ACTION_LABEL_ID[final_action],
        "base_tier_before_caps": base_tier,
        "ceiling_applied": tier != base_tier,
        "component_spread": round(component_spread, 1),
        "is_borderline": is_borderline,
    }


# Bump this whenever the scoring FORMULA itself changes (RSI banding, weight changes,
# new factors, etc). This makes it visible when a score difference between two runs is
# due to a real formula change vs. genuine day-to-day market movement — comparing scores
# across different versions isn't apples-to-apples.
SCORING_FORMULA_VERSION = "3.14.11"  # v3.14.3: migrasi konfigurasi ke .env — semua API keys/tokens (Telegram, Gemini, iTick, Index Alpha, Zapi) dipindahkan dari hardcoded di kode ke file .env terpisah. Load otomatis via python-dotenv kalau tersedia, fallback ke manual parser kalau belum diinstall. Startup validation: tampilkan warning eksplisit kalau ada key yang kosong, bukan hanya "True/False" seperti sebelumnya. Tidak ada satu pun secret yang tertinggal di kode Python (diverifikasi dengan grep sebelum commit). Untuk migrasi: isi file mbss.env dengan API keys kamu, rename jadi .env, taruh di folder yang sama dengan mbss.py.


def compute_factor_scoring(ticker, include_quote_check=True):
    """
    Returns a scored dict for one ticker, or None if data is unavailable/insufficient.

    Scoring is ADAPTIVE PER STOCK: RSI, momentum (price vs SMA20), and volume are each
    scored by where today's reading falls within THAT stock's own trailing history,
    rather than one fixed threshold applied to every stock. This means a "high" volume
    ratio for a thin, rarely-traded stock isn't scored the same as a "high" ratio for a
    heavily-traded one like TLKM.

    DATA SOURCE: price/volume history now comes from the local SQLite EOD cache,
    which is seeded and refreshed via Yahoo Finance. yfinance is also used for the
    lighter .info lookup (PE/PB/dividend). This keeps /screendaytrade and related
    EOD flows independent from the expired iTick key.

    include_quote_check: retained for compatibility. EOD screening now relies on
    Yahoo-backed OHLCV data, so the extra direct quote check is optional and only
    used where a separate live-status signal is still desired.
    """
    # Gunakan SQLite layer dulu (get_ohlcv_smart) — fetch delta dari iTick hanya
    # kalau bar hari ini belum ada di DB. Menghemat ~99% panggilan iTick untuk
    # ticker yang sudah pernah di-scan sebelumnya.
    hist = get_ohlcv_smart(ticker, limit=500)

    if hist is None or hist.empty or len(hist) < 20:
        return None

    # Direct halt/delisted check via iTick's trading status — more authoritative than
    # inferring "frozen" purely from price action (which is still kept as a backup
    # check further below regardless of this setting, in case this is skipped or fails).
    quote = None
    if include_quote_check:
        quote = itick_get_quote(ticker)
        if quote is not None:
            trading_status = quote.get("ts")
            if trading_status in (1, 2):  # 1=Halt, 2=Delisted
                status_label = "halted" if trading_status == 1 else "delisted"
                print(f"⚠️ {ticker}: excluded — iTick reports trading status = {status_label}")
                return None

    stock = get_yf_ticker(f"{ticker}.JK")
    # yfinance is now just an ENHANCEMENT (PE/PB/dividend) on top of iTick's core
    # price/technical data — not a hard dependency. If Yahoo is rate-limited or
    # down (which has been a recurring problem this session), we still have
    # perfectly good iTick data for this ticker and shouldn't drop it entirely
    # just because the secondary fundamentals lookup failed. Falls back to an
    # empty info dict, which the existing pe==0/pb==0 handling already treats as
    # neutral rather than crashing.
    try:
        info = yf_fetch_with_retry(lambda: stock.info)
    except Exception as e:
        print(f"⚠️ {ticker}: yfinance PE/PB/dividend lookup failed ({e}) — scoring with iTick data only, "
              f"value score will be neutral on the fundamentals it can't see.")
        info = {}
    close_prices = hist["Close"]
    volumes = hist["Volume"]
    low_prices = hist["Low"]
    high_prices = hist["High"]

    current_price = close_prices.iloc[-1]
    as_of_date = str(hist.index[-1].date())
    has_adaptive_baseline = len(hist) >= MIN_HISTORY_FOR_ADAPTIVE

    # DATA FRESHNESS CHECK: cross-check historical bar against iTick's own live quote
    # (rather than yfinance's, since iTick is now the primary price source).
    data_freshness_warning = None
    intraday_high = None
    intraday_low = None
    try:
        live_price = quote.get("ld") if quote else None
        if quote:
            intraday_high = quote.get("h")  # live intraday high, updates during trading hours
            intraday_low = quote.get("l")   # live intraday low, updates during trading hours
        if live_price and current_price:
            live_diff_pct = abs(live_price - current_price) / current_price * 100
            if live_diff_pct > 2.0:
                data_freshness_warning = (
                    f"Historical bar (Rp{int(current_price)}, as of {as_of_date}) differs "
                    f"from live quote (Rp{int(live_price)}) by {live_diff_pct:.1f}% — "
                    f"today's price action may not be fully reflected yet."
                )
    except Exception:
        pass

    # --- 0. DISTRESS / PRICE-FLOOR GUARD ---
    # IDX has an absolute minimum trade price of Rp50 ("gocap" among retail traders).
    # A stock stuck there — or one whose price hasn't genuinely moved in 10 days — isn't
    # "cheap", it's often frozen due to financial distress, suspension risk, or a
    # regulatory floor. Scoring "distance below own historical range" as a value signal
    # would wrongly rate this as maximally attractive. Exclude these outright rather
    # than let a flat price series generate a fake buy signal.
    support_10d_raw = low_prices.tail(10).min()
    resistance_10d_raw = high_prices.tail(10).max()
    price_range_pct = ((resistance_10d_raw - support_10d_raw) / max(support_10d_raw, 1)) * 100

    if current_price <= 51 or price_range_pct < 2.0:
        print(f"⚠️ {ticker}: excluded — frozen/floor price (price={current_price}, 10d range={price_range_pct:.1f}%)")
        return None

    # Broader risk-preference exclusion: very low nominal-price IDX stocks correlate
    # strongly with post-restructuring/distress situations in practice (both WEGE and
    # GIAA — the two confirmed anomalies found so far — sit under Rp100). This is a
    # blunt filter, not a claim that all sub-Rp100 stocks are bad, but given a capital
    # preservation-focused strategy it's a reasonable tradeoff: excludes some legitimate
    # low-priced stocks in exchange for meaningfully reducing exposure to the riskiest,
    # most anomaly-prone segment. Adjust MIN_STOCK_PRICE if this feels too aggressive.
    if current_price < MIN_STOCK_PRICE:
        print(f"⚠️ {ticker}: excluded — price {current_price} below MIN_STOCK_PRICE ({MIN_STOCK_PRICE})")
        return None

    # Doesn't fully exclude, but flags stocks trading close to the Rp50 floor (e.g.
    # post-restructuring stocks like GIAA) — these are genuinely tradeable, not
    # frozen, but carry real distress/liquidity risk that deserves explicit caution
    # rather than being scored the same as a healthy mid/large-cap stock.
    is_near_price_floor = current_price < 70

    # --- 1. SUPPORT / RESISTANCE BOUNDS (unchanged: still short-term, 10-day) ---
    support_10d = low_prices.tail(10).min()
    resistance_10d = high_prices.tail(10).max()

    # Volume ratio + histori, dan CMF — DIPINDAH ke sini (lebih awal dari biasanya)
    # karena sekarang dibutuhkan Momentum (reward/penalti volume, kondisi hapus
    # otomatis pakai CMF), bukan cuma Sentiment seperti sebelumnya. Sentiment di
    # bawah nanti REUSE variabel yang sama, tidak dihitung ulang.
    avg_vol_20d = volumes.rolling(window=20).mean().iloc[-1]
    current_vol = volumes.iloc[-1]
    vol_ratio = current_vol / (avg_vol_20d + 1e-9)
    vol_ratio_full_series = volumes / (volumes.rolling(window=20).mean() + 1e-9)

    cmf_series = calculate_cmf(high_prices, low_prices, close_prices, volumes)
    current_cmf = cmf_series.iloc[-1] if not cmf_series.empty else None

    # NOTE: target_buy_min/max, tp_1, cut_loss are computed FURTHER DOWN, after
    # momentum/MACD/decision are known — TP1 is now target-%-driven (min +5%,
    # up to +10% if confidence is high AND estimated horizon is under 5 days),
    # which needs those signals as input.

    # --- 2. VALUE SCORE ---
    # PREVIOUS VERSION BUG: this used to blend in "price percentile within its own
    # 1-2yr trading range" as a value signal. That's actually a MOMENTUM/mean-reversion
    # signal, not a value signal — a stock that just rallied hard on strong fundamentals
    # would get penalized here purely because its price is now higher in its own range,
    # even if its actual valuation (PBV, dividend yield) still looks cheap. Fixed by
    # removing that and using genuine fundamental factors instead: PE, PB, and dividend
    # yield — the same things real analysts and dividend-focused investors weigh.
    pe = info.get("trailingPE", 0) or 0
    pb = info.get("priceToBook", 0) or 0
    dividend_yield_raw = info.get("dividendYield", 0) or 0
    # yfinance sometimes returns this as a decimal (0.0886) and sometimes as a whole
    # percentage number (8.86) depending on ticker/version — normalize to a percentage.
    dividend_yield_pct = dividend_yield_raw * 100 if dividend_yield_raw < 1 else dividend_yield_raw

    # Smooth continuous scoring instead of int()-truncated ratios — the previous
    # int(25/pe) style formula had a harsh cliff that collapsed to score 1 for ANY
    # PE above ~12.5, treating completely normal blue-chip valuations (e.g. PE 15-20,
    # common for banks/telcos) the same as genuinely expensive stocks. This is what
    # was still dragging TLKM's value score down even after removing the price-
    # percentile contamination.
    # BUG FIX: pe<=0 or pb<=0 used to score a perfect 10 — meant to handle "missing
    # data" but this also silently rewarded genuinely NEGATIVE earnings (loss-making
    # company) or negative book value (accumulated losses wiping out equity — a real
    # distress signal) with the maximum possible value score. Fixed: missing/zero data
    # is now neutral (5), and a NEGATIVE ratio (confirmed loss/negative equity, not
    # just missing data) is scored low (2), since that's a genuine red flag.
    if pe is None or pe == 0:
        pe_score_fixed = 5.0  # unknown — don't reward or penalize blindly
    elif pe < 0:
        pe_score_fixed = 2.0  # confirmed negative earnings — real caution signal
    else:
        pe_score_fixed = max(1.0, min(10.0, 10 - (pe - 5) / 3))

    if pb is None or pb == 0:
        pb_score_fixed = 5.0
    elif pb < 0:
        pb_score_fixed = 2.0  # negative book value — equity wiped out by losses
    else:
        pb_score_fixed = max(1.0, min(10.0, 10 - (pb - 0.5) / 0.35))

    is_financial_distress_flag = (pe is not None and pe < 0) or (pb is not None and pb < 0)
    dividend_score = max(1.0, min(10.0, dividend_yield_pct))  # roughly 1 point per 1% yield, capped

    # Bobot direvisi user: PE 25 / PB 35 / Dividend 15 / Growth Catalyst 25 (dihapus,
    # susah dikuantifikasi) — dinormalisasi proporsional ke 100% dari 3 komponen
    # yang tersisa, mempertahankan rasio relatif PE<PB, Dividend paling kecil.
    value_score = (pe_score_fixed * 0.333) + (pb_score_fixed * 0.467) + (dividend_score * 0.20)

    # --- 3. MOMENTUM SCORE (RSI + price-vs-SMA20, both adaptive per stock) ---
    sma20_series = close_prices.rolling(window=20).mean()
    sma20 = sma20_series.iloc[-1]
    rsi_series = calculate_rsi(close_prices)
    current_rsi = rsi_series.iloc[-1]

    if pd.isna(current_rsi) or pd.isna(sma20):
        return None

    if has_adaptive_baseline:
        # Use this stock's OWN historical RSI distribution to define what "healthy middle
        # ground" vs "overbought/oversold" means for it, instead of fixed 45/65/75 bands.
        rsi_hist = rsi_series.iloc[:-1].dropna()
        p30, p45, p55, p65, p75 = (
            rsi_hist.quantile(0.30), rsi_hist.quantile(0.45), rsi_hist.quantile(0.55),
            rsi_hist.quantile(0.65), rsi_hist.quantile(0.75),
        ) if len(rsi_hist) >= MIN_HISTORY_FOR_ADAPTIVE else (30, 45, 55, 65, 75)
    else:
        p30, p45, p55, p65, p75 = 30, 45, 55, 65, 75

    # Split the old flat "45-65 = ideal" zone: RSI approaching the upper end (55-65) is
    # genuinely warmer / closer to overbought than the true middle (45-55), and should
    # score lower accordingly — not get the same max score as a stock sitting comfortably
    # in the middle. This prevents the scoring from silently rewarding a stock while the
    # generated text separately (and correctly) warns "watch out for overbought."
    is_overbought_caution = False
    if p45 <= current_rsi <= p55:
        rsi_score = 9
    elif p55 < current_rsi <= p65:
        rsi_score = 6.5
        is_overbought_caution = True
    elif p30 <= current_rsi < p45:
        rsi_score = 7
    elif p65 < current_rsi <= p75:
        rsi_score = 4.5
        is_overbought_caution = True
    elif current_rsi > p75:
        rsi_score = 3
        is_overbought_caution = True
    else:
        rsi_score = 5

    sma_dist_pct = ((current_price - sma20) / sma20) * 100
    if has_adaptive_baseline:
        dist_series = ((close_prices - sma20_series) / sma20_series * 100).iloc[:-1]
        dist_pct_rank = percentile_rank(dist_series, sma_dist_pct)
        sma_score = score_from_percentile(dist_pct_rank)
    else:
        sma_score = max(1, min(10, int(5 + (sma_dist_pct * 2))))

    # EMA21: sama pola adaptif dengan SMA20, tapi lebih reaktif terhadap harga
    # terbaru (weighted, bukan rata rata datar) — dipilih khusus karena user
    # berorientasi swing pendek 1-5 hari, butuh sinyal yang lebih cepat merespons
    # dibanding SMA20 yang cenderung lamban untuk horizon sependek itu.
    ema21_series = close_prices.ewm(span=21, adjust=False).mean()
    ema21 = ema21_series.iloc[-1]
    ema21_dist_pct = ((current_price - ema21) / ema21) * 100
    if has_adaptive_baseline:
        ema21_dist_series = ((close_prices - ema21_series) / ema21_series * 100).iloc[:-1]
        ema21_dist_pct_rank = percentile_rank(ema21_dist_series, ema21_dist_pct)
        ema21_score = score_from_percentile(ema21_dist_pct_rank)
    else:
        ema21_score = max(1, min(10, int(5 + (ema21_dist_pct * 2))))
    is_below_ema21 = bool(current_price < ema21)

    # Direvisi user: EMA21 jadi SATU-SATUNYA base Momentum (bukan lagi RSI+SMA20
    # weighted seperti sebelumnya) — RSI dipindah jadi FILTER/plafon di akhir
    # formula (lihat bawah, setelah semua lapisan tambahan), bukan komponen
    # linear berbobot lagi. SMA20 tidak lagi dipakai di base sama sekali — EMA21
    # lebih reaktif, lebih cocok horizon swing pendek 1-5 hari.
    momentum_score = ema21_score

    # --- MACD: timing/crossover signal, distinct from RSI's overbought/oversold read ---
    # Swing/day-trade oriented: we care about RECENT crossovers (momentum turning right
    # now), not just the static current state, since a stock can sit "MACD bearish" for
    # a long stable stretch without that being a fresh, actionable signal.
    macd_line, signal_line, macd_hist = calculate_macd(close_prices)
    current_macd_hist = macd_hist.iloc[-1]
    prev_macd_hist = macd_hist.iloc[-2] if len(macd_hist) > 1 else current_macd_hist
    macd_bullish_cross = bool(current_macd_hist > 0 and prev_macd_hist <= 0)
    macd_bearish_cross = bool(current_macd_hist < 0 and prev_macd_hist >= 0)
    macd_state = "bullish" if current_macd_hist > 0 else "bearish"

    # Berapa hari sejak cross terakhir (mundur cari kapan tanda histogram
    # terakhir berubah) — dipakai untuk formula PELURUHAN (decay) pengaruh
    # terhadap skor: cross 1 hari lalu = hampir penuh pengaruhnya, cross 5 hari
    # lalu = nyaris habis pengaruhnya, pas dengan horizon swing 1-5 hari user
    # (bukan lagi cuma True/False biner "baru saja" yang tidak presisi harinya).
    macd_cross_days_ago = None
    macd_cross_direction = None
    current_sign = current_macd_hist > 0
    for days_back in range(1, min(30, len(macd_hist))):
        past_sign = macd_hist.iloc[-1 - days_back] > 0
        if past_sign != current_sign:
            macd_cross_days_ago = days_back
            macd_cross_direction = "bullish" if current_sign else "bearish"
            break

    if macd_cross_days_ago is not None:
        decay = max(0.0, 1 - (macd_cross_days_ago / 5))
        if macd_cross_direction == "bearish":
            momentum_score = max(1.0, momentum_score - 1.5 * decay)
        elif macd_cross_direction == "bullish":
            momentum_score = min(10.0, momentum_score + 1.0 * decay)

    # --- SMA50: medium-term trend filter (swing-relevant horizon, not SMA200/long-term
    # investing horizon) — catches "looks fine short-term but still in a weaker medium-
    # term regime" cases that SMA20 alone (already used above) can miss.
    is_below_sma50 = False
    if len(close_prices) >= 50:
        sma50 = close_prices.rolling(window=50).mean().iloc[-1]
        is_below_sma50 = bool(current_price < sma50)

    # --- ADX: pengali KEPERCAYAAN terhadap momentum_score, bukan sekadar info.
    # ADX rendah (<20) = pasar sideways/noise — sinyal momentum apa pun di
    # kondisi ini kurang bisa dipercaya, jadi skor ditarik mendekati netral
    # (bukan dihapus sepenuhnya, cuma dikecilkan pengaruhnya).
    adx_series = calculate_adx(high_prices, low_prices, close_prices)
    current_adx = adx_series.iloc[-1]
    is_weak_trend = bool(current_adx < 20)
    if is_weak_trend:
        momentum_score = 5.0 + (momentum_score - 5.0) * 0.5

    # --- Breakout High 20 hari: harga tertinggi baru dalam 20 hari — sinyal
    # breakout klasik, beda dari resistance 10-hari yang sudah dipakai di
    # target harga (window lebih pendek, ini window lebih panjang khusus untuk
    # deteksi breakout).
    is_new_high_20d = False
    if len(high_prices) >= 20:
        high_20d = high_prices.tail(20).max()
        is_new_high_20d = bool(current_price >= high_20d)
        if is_new_high_20d:
            momentum_score = min(10.0, momentum_score + 1.0)

    # --- Relative Strength vs IHSG: DIGANTI dari versi 10-hari kumulatif ke
    # HARIAN atas permintaan user — untuk swing pendek, 10 hari kurang responsif.
    # Saham naik 1% hari ini itu biasa saja kalau IHSG naik 2% hari itu juga
    # (saham itu sebenarnya KALAH dari pasar HARI INI) — RS mengukur kekuatan
    # RELATIF harian, bukan cuma arah sendirian. Threshold diturunkan dari ±3%
    # (yang cocok untuk kumulatif 10 hari) ke ±1% (lebih masuk akal untuk
    # perbedaan satu hari — 3% beda dalam SATU hari itu sudah sangat ekstrem).
    relative_strength_vs_ihsg = None
    if len(close_prices) >= 2:
        stock_return_today = ((current_price - close_prices.iloc[-2]) / close_prices.iloc[-2]) * 100
        ihsg_return_today = get_ihsg_return_today()
        if ihsg_return_today is not None:
            relative_strength_vs_ihsg = round(stock_return_today - ihsg_return_today, 2)
            if relative_strength_vs_ihsg > 1:
                momentum_score = min(10.0, momentum_score + 0.5)
            elif relative_strength_vs_ihsg < -1:
                momentum_score = max(1.0, momentum_score - 0.5)

    # Chart structure check: a lower-highs pattern signals weakening upside even when
    # RSI/SMA look fine in isolation — this is what addresses "wait for breakout above
    # X" style feedback that pure indicator-based scoring misses entirely.
    swing_analysis = detect_lower_highs(high_prices)
    if swing_analysis["pattern"] == "lower_highs_bearish":
        momentum_score = max(1.0, momentum_score - 2.0)

    # --- Reward volume besar: breakout hampir selalu dimulai dari volume, jadi
    # dapat bonus langsung ke Momentum (bukan cuma memengaruhi Sentiment seperti
    # sebelumnya). Ambil TIER TERTINGGI saja, tidak kumulatif.
    if vol_ratio > 3.0:
        momentum_score = min(10.0, momentum_score + 1.5)
    elif vol_ratio > 2.0:
        momentum_score = min(10.0, momentum_score + 1.0)
    elif vol_ratio > 1.5:
        momentum_score = min(10.0, momentum_score + 0.5)

    # --- Penalti saham "mati": volume rendah (<0.8x) BERTURUT-TURUT, bukan cuma
    # 1 hari (wajar) — 3 hari mulai menunjukkan memang tidak ada minat pasar.
    # Hilang otomatis begitu ada tanda uang mulai masuk lagi (volume pulih,
    # breakout+volume, atau CMF jelas positif) — supaya tidak "menghukum"
    # saham yang baru saja mulai bergerak lagi.
    consecutive_low_volume_days = 0
    for i in range(1, min(10, len(vol_ratio_full_series)) + 1):
        if vol_ratio_full_series.iloc[-i] < 0.8:
            consecutive_low_volume_days += 1
        else:
            break

    dead_stock_penalty_lifted = (
        vol_ratio >= 1.2
        or (is_new_high_20d and vol_ratio > 1.0)  # breakout disertai volume
        or (current_cmf is not None and not pd.isna(current_cmf) and current_cmf > 0.15)  # CMF jelas positif
    )

    if consecutive_low_volume_days >= 3 and not dead_stock_penalty_lifted:
        if consecutive_low_volume_days >= 8:
            momentum_score = max(1.0, momentum_score - 2.0)
        elif consecutive_low_volume_days >= 5:
            momentum_score = max(1.0, momentum_score - 1.5)
        else:  # >= 3
            momentum_score = max(1.0, momentum_score - 1.0)

    # --- RSI sebagai FILTER, bukan komponen berbobot lagi: cuma membatasi SISI
    # ATAS (overbought ekstrem, ambang adaptif p75 yang sudah ada) — TIDAK
    # memaksa turun saat oversold, karena oversold pada swing justru sering jadi
    # peluang entry, bukan sinyal buruk. Plafon 7.0 adalah pilihan awal, bisa
    # disesuaikan lagi setelah dicoba di data nyata.
    RSI_OVERBOUGHT_MOMENTUM_CAP = 7.0
    if current_rsi > p75:
        momentum_score = min(momentum_score, RSI_OVERBOUGHT_MOMENTUM_CAP)

    # --- 4. SENTIMENT SCORE (volume, adaptive per stock, sharpened by CMF + OBV divergence) ---
    # vol_ratio, current_cmf sudah dihitung lebih awal (dibutuhkan Momentum duluan) —
    # reuse di sini, tidak dihitung ulang.
    if has_adaptive_baseline:
        vol_ratio_series = vol_ratio_full_series.iloc[:-1]
        vol_pct_rank = percentile_rank(vol_ratio_series, vol_ratio)
        sentiment_score = score_from_percentile(vol_pct_rank)
    else:
        if vol_ratio > 2.0:
            sentiment_score = 10
        elif vol_ratio > 1.5:
            sentiment_score = 8
        elif vol_ratio > 1.0:
            sentiment_score = 6
        elif vol_ratio < 0.5:
            sentiment_score = 2
        else:
            sentiment_score = 4

    # CMF: was volume actually buying pressure (closes near daily highs) or selling
    # pressure (closes near daily lows)? Pulls the raw volume-ratio score toward
    # what the money flow direction actually shows, instead of treating all high
    # volume as automatically bullish.
    if current_cmf is not None and not pd.isna(current_cmf):
        # current_cmf ranges roughly -1 to +1; nudge sentiment score toward it
        cmf_adjustment = current_cmf * 2.0  # e.g. CMF of -0.5 pulls score down ~1 point
        sentiment_score = max(1.0, min(10.0, sentiment_score + cmf_adjustment))
    else:
        current_cmf = None

    # OBV divergence: the key check for "price looks fine but volume flow disagrees"
    obv_series = calculate_obv(close_prices, volumes)
    obv_divergence = detect_obv_divergence(close_prices, obv_series)
    if obv_divergence == "bearish_divergence":
        sentiment_score = max(1.0, sentiment_score - 2.5)  # override: flow says distribution
    elif obv_divergence == "bullish_divergence":
        sentiment_score = min(10.0, sentiment_score + 1.0)

    # Extreme volume spikes (e.g. 5x+ normal) are anomalies, not automatically bullish —
    # they're often one-off news/rumor-driven or thin-liquidity events that can reverse
    # sharply. Flag rather than let a huge ratio blindly max out the sentiment score.
    is_volume_spike_anomaly = vol_ratio > 3.0

    # Direvisi user: dari Value 30/Momentum 40/Sentiment 30 jadi Value 25/Momentum
    # 45/Sentiment 30 — untuk swing pendek, Momentum harus mengalahkan Value
    # (mengejar uang beberapa hari, bukan mencari saham termurah).
    final_score = (value_score * 0.25) + (momentum_score * 0.45) + (sentiment_score * 0.30)

    company_name = info.get("longName") or info.get("shortName") or ticker
    sector = info.get("sector") or "N/A"

    # An unusually low PE (e.g. under ~3) can indicate a one-off/non-recurring gain
    # distorting trailing earnings, not genuine sustainable cheapness — a different
    # flavor of "too good to be true" value read than the negative-PE distress case.
    is_unusually_low_pe = (pe is not None and 0 < pe < 3)

    # FULL DETERMINISTIC DECISION — Gemini explains this, it does not choose or
    # override it. See decide_action() docstring/comments for why this moved out
    # of the prompt entirely.
    decision = decide_action(
        final_score=final_score, value_score=value_score, momentum_score=momentum_score,
        sentiment_score=sentiment_score, is_financial_distress_flag=is_financial_distress_flag,
        chart_pattern=swing_analysis["pattern"], is_overbought_caution=is_overbought_caution,
        obv_divergence=obv_divergence, is_volume_spike_anomaly=is_volume_spike_anomaly,
        is_near_price_floor=is_near_price_floor, is_unusually_low_pe=is_unusually_low_pe,
        macd_bearish_cross=macd_bearish_cross, is_below_sma50=is_below_sma50,
    )

    # --- TARGET HARGA BERBASIS % ---
    # TP1 sekarang diarahkan ke target persentase (bukan murni level resistance
    # teknikal seperti sebelumnya): default minimal +5%, naik ke +10% HANYA kalau
    # momentum/MACD/keputusan semuanya mendukung DAN estimasi hari untuk capai
    # +10% (berdasar volatilitas 10hr saham itu sendiri) di bawah 5 hari — supaya
    # target 10% tidak asal dipasang tanpa dasar realistis.
    target_buy_min = round(support_10d / 5) * 5
    target_buy_max = round((current_price * 1.01) / 5) * 5

    # CUT LOSS — dihitung LEBIH DULU sekarang (sebelum TP1), karena TP1 perlu
    # tahu jarak SL untuk menjamin risk:reward yang layak (lihat di bawah).
    MAX_CUTLOSS_DISTANCE_FROM_ENTRY_PCT = 5.0
    cut_loss_from_support = support_10d * 0.96
    cut_loss_max_distance = target_buy_max * (1 - MAX_CUTLOSS_DISTANCE_FROM_ENTRY_PCT / 100)
    cut_loss = max(cut_loss_from_support, cut_loss_max_distance)
    cut_loss = min(cut_loss, target_buy_min * 0.98)  # pengaman: SL selalu di bawah entry_min
    cut_loss = round(cut_loss / 5) * 5
    if cut_loss >= target_buy_min:
        # Artefak pembulatan ke kelipatan 5 — nilai sebelum dibulatkan sudah di
        # bawah entry_min, tapi pembulatan bisa membuat keduanya "bertemu" jadi
        # sama, membuat risk_at_min = 0. Turunkan 1 kelipatan pembulatan lagi
        # untuk memastikan strictly di bawah.
        cut_loss = target_buy_min - 5

    # --- TARGET HARGA BERBASIS % ---
    # TP1: default minimal +5%, naik ke +10% kalau momentum/MACD/keputusan semua
    # mendukung DAN horizon <5 hari, ATAU level resistance teknikal kalau lebih
    # tinggi dari itu — SEPENUHNYA dari sinyal teknikal asli, TIDAK disesuaikan
    # berdasarkan RR yang diinginkan.
    #
    # CATATAN KOREKSI (ditemukan lewat pertanyaan tajam user, "apa dasarnya,
    # atau dikarang biar RR-nya bagus?"): versi sebelumnya SEMPAT menambahkan
    # "tp1_for_min_rr" — memaksa TP1 naik sampai RR di entry_max mencapai
    # 1.5:1. Itu SALAH METODOLOGI: TP1 jadi direkayasa MUNDUR dari rasio yang
    # diinginkan, bukan dihitung dari sinyal teknikal genuine — persis
    # kekhawatiran user. DIBATALKAN. Kalau TP1 (dari resistance/confidence asli)
    # dan SL (dari support asli) menghasilkan RR jelek, itu BUKAN masalah yang
    # perlu "diperbaiki" dengan menggeser target harga — itu INFORMASI JUJUR
    # bahwa entry di harga sekarang memang kurang menguntungkan untuk saham ini.
    # RR tetap dihitung & ditampilkan apa adanya (lihat instruksi Gemini: kalau
    # RR@max di bawah 1:1, katakan terus terang, jangan disamarkan).
    tp1_pct_target = 0.05  # default minimal +5%
    daily_pace = max(0.3, price_range_pct / 10)  # pace harian dari volatilitas 10hr sendiri
    est_days_for_10pct = 10.0 / daily_pace
    horizon_confidence_high = (
        momentum_score >= 6.0
        and macd_state == "bullish"
        and decision["action_id"] in ("STRONG_BUY", "BUY_ACCUMULATE")
        and est_days_for_10pct < 5
    )
    if horizon_confidence_high:
        tp1_pct_target = 0.10

    tp1_from_pct = current_price * (1 + tp1_pct_target)
    tp_1 = max(tp1_from_pct, resistance_10d)  # murni % target atau resistance teknikal, TIDAK ADA lagi floor RR
    tp_1 = round(tp_1 / 5) * 5

    # Risk:Reward — dihitung di KEDUA ujung entry range dari TP1/SL yang SUDAH
    # final (genuine, bukan direkayasa), murni untuk INFORMASI — rasio yang
    # didapat SANGAT berbeda tergantung di mana benar-benar entry (dekat
    # entry_min = rasio jauh lebih baik daripada dekat entry_max/harga sekarang,
    # yang notabene entry paling umum untuk day trade breakout).
    risk_at_min = target_buy_min - cut_loss
    reward_at_min = tp_1 - target_buy_min
    rr_at_min = round(reward_at_min / risk_at_min, 2) if risk_at_min > 0 else None

    risk_at_max = target_buy_max - cut_loss
    reward_at_max = tp_1 - target_buy_max
    rr_at_max = round(reward_at_max / risk_at_max, 2) if risk_at_max > 0 else None

    # Defense in depth: if all price targets collapsed to the same number (can happen
    # from rounding on extremely low-priced/illiquid stocks even outside the floor-price
    # case above), the data has no genuine signal — exclude rather than show a fake range.
    if len({int(target_buy_min), int(target_buy_max), int(tp_1), int(cut_loss)}) == 1:
        print(f"⚠️ {ticker}: excluded — degenerate price targets all equal ({int(tp_1)}).")
        return None

    result = {
        "ticker": ticker,
        "name": company_name,
        "sector": sector,
        "price": int(current_price),
        "as_of_date": as_of_date,
        "data_freshness_warning": data_freshness_warning,
        "intraday_high": intraday_high,
        "intraday_low": intraday_low,
        "pe": round(pe, 1) if pe else "N/A",
        "pb": round(pb, 1) if pb else "N/A",
        "is_unusually_low_pe": is_unusually_low_pe,
        "dividend_yield_pct": round(dividend_yield_pct, 2) if dividend_yield_pct else "N/A",
        "is_financial_distress_flag": is_financial_distress_flag,
        "is_near_price_floor": is_near_price_floor,
        "macd_state": macd_state,
        "macd_hist": round(float(current_macd_hist), 4),
        "macd_bullish_cross": macd_bullish_cross,
        "macd_bearish_cross": macd_bearish_cross,
        "is_below_sma50": is_below_sma50,
        "is_below_ema21": is_below_ema21,
        "adx": round(current_adx, 1),
        "is_weak_trend": is_weak_trend,
        "macd_cross_days_ago": macd_cross_days_ago,
        "macd_cross_direction": macd_cross_direction,
        "is_new_high_20d": is_new_high_20d,
        "relative_strength_vs_ihsg": relative_strength_vs_ihsg,
        "consecutive_low_volume_days": consecutive_low_volume_days,
        "dead_stock_penalty_lifted": dead_stock_penalty_lifted,
        "high_conviction": compute_high_conviction_score(ticker, {
            "price": current_price,
            "day_range_pct_10d": price_range_pct,
            "vol_ratio": vol_ratio,
            "is_below_ema21": is_below_ema21,
            "is_below_sma50": is_below_sma50,
        }, hist_daily=hist),
        "action_id": decision["action_id"],
        "action_label_id": decision["action_label_id"],
        "action_ceiling_applied": decision["ceiling_applied"],
        "action_component_spread": decision["component_spread"],
        "rsi": round(current_rsi, 1),
        "ret_1d_pct": round(float(stock_return_today), 2) if 'stock_return_today' in locals() else None,
        "intraday_range_pct": round(float((high_prices.iloc[-1] - low_prices.iloc[-1]) / max(current_price, 1e-9) * 100), 2),
        "close_pos_day": round(float((current_price - low_prices.iloc[-1]) / max(high_prices.iloc[-1] - low_prices.iloc[-1], 1e-9)), 3),
        "value_traded": int(float(current_price * current_vol)),
        "price_vs_sma20_pct": round(float(sma_dist_pct), 2),
        "dist_to_20d_high_pct": round(float((high_20d - current_price) / max(current_price, 1e-9) * 100), 2) if len(high_prices) >= 20 else None,
        "obv_slope_5_pct": round(float((obv_series.iloc[-1] - obv_series.iloc[-6]) / max(abs(obv_series.iloc[-6]), 1e-9) * 100), 2) if len(obv_series) >= 6 else None,
        "vol_ratio": round(vol_ratio, 2),
        "cmf": round(current_cmf, 2) if current_cmf is not None else "N/A",
        "obv_divergence": obv_divergence,
        "is_overbought_caution": is_overbought_caution,
        "is_volume_spike_anomaly": is_volume_spike_anomaly,
        "chart_pattern": swing_analysis["pattern"],
        "scoring_formula_version": SCORING_FORMULA_VERSION,
        "breakout_level": swing_analysis["breakout_level"],
        "adaptive_scoring_used": has_adaptive_baseline,
        "day_range_pct_10d": round(price_range_pct, 1),  # transparency: how much this stock actually moved
        "targets": {
            "buy_range": f"{int(target_buy_min)} - {int(target_buy_max)}",
            "tp_1": int(tp_1),
            "cut_loss": int(cut_loss),
            "risk_reward_at_min": rr_at_min,
            "risk_reward_at_max": rr_at_max,
        },
        "scores": {
            "value": round(value_score, 1),
            "momentum": round(momentum_score, 1),
            "sentiment": round(sentiment_score, 1),
            "final": round(final_score, 1),
        },
    }

    risk_character = classify_risk_character(result)
    result["risk_character"] = risk_character["character"]
    result["risk_character_reason"] = risk_character["reason"]

    return result


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


def get_current_idx_session():
    """
    Menentukan sesi bursa IDX saat ini — DIVERIFIKASI via pencarian web (bukan
    dari ingatan yang mungkin usang), bisa berubah sewaktu-waktu (misal Ramadan)
    — cek idx.co.id/id/produk/mekanisme-dan-jam-perdagangan kalau terasa meleset.
    Senin-Kamis: Sesi 1 09:00-12:00, Sesi 2 13:30-15:49.
    Jumat: Sesi 1 09:00-11:30, Sesi 2 14:00-15:49 (jeda lebih panjang, salat Jumat).
    Return "sesi_1", "sesi_2", atau None (di luar jam bursa/weekend).
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
    }


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
or "the market has absorbed this" — that is not something you can actually verify from
the data you have, and stating it as if it were a fact is a serious credibility failure.
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


async def safe_reply(message_or_bot, text: str, chat_id=None, max_retries=3):
    """
    Works for both update.message.reply_text (interactive) and bot.send_message
    (scheduled broadcast). Sends PLAIN TEXT ONLY.

    Why plain text: most bot outputs use underscores from raw field names, brackets,
    or risk/reward strings that repeatedly break Telegram Markdown parsing. Since all
    report instructions already say "plain text", disabling Markdown at the send layer
    removes noisy "can't parse entities" failures and saves one failed network attempt
    per affected message.
    """
    async def _send(body: str):
        if chat_id is None:
            await message_or_bot.reply_text(body)
        else:
            await message_or_bot.send_message(chat_id=chat_id, text=body)

    async def _send_one(body: str):
        last_error = None
        for attempt in range(1, max_retries + 1):
            try:
                await _send(body)
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
        await _send_one(prefix + chunk)


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


def save_daily_scan_cache(results: list):
    """
    Simpan hasil scan penuh (dari scheduled job jam 22:00 WIB) ke cache bersama
    — dipakai oleh run_morning_brief() dan screen_daytrade() supaya tidak perlu
    fetch iTick ulang untuk ticker yang sudah di-scan malam sebelumnya. Ikut
    simpan SCORING_FORMULA_VERSION — supaya kalau formula di-update (misal
    perbaikan cut_loss/risk-reward), cache lama otomatis dianggap basi meski
    tanggalnya masih sama, bukan diam-diam tetap dipakai sampai kadaluarsa
    besok. Ditemukan lewat pertanyaan user: cache sebelumnya cuma cek tanggal,
    tidak cek versi formula — update kode tidak akan efektif sampai hari
    berikutnya kalau tidak diperbaiki.
    """
    scored_by_ticker = {r["ticker"]: r for r in results if r and r.get("ticker")}
    cache = {
        "trading_day_marker": get_current_trading_day_close_marker(),
        "formula_version": SCORING_FORMULA_VERSION,
        "scored": scored_by_ticker,
    }
    try:
        with open(DAILY_SCAN_CACHE_FILE, "w") as f:
            json.dump(cache, f, indent=2)
        print(f"💾 Daily scan cache tersimpan: {len(scored_by_ticker)} ticker, marker {cache['trading_day_marker']}, formula v{SCORING_FORMULA_VERSION}")
    except Exception as e:
        print(f"⚠️ Gagal menyimpan daily scan cache: {e}")


def load_daily_scan_cache() -> dict:
    """
    Return dict {ticker: scoring} dari cache scan malam TERAKHIR — HANYA kalau
    marker-nya masih cocok dengan hari bursa yang relevan SAAT INI (lihat
    get_current_trading_day_close_marker) DAN formula_version masih sama
    dengan SCORING_FORMULA_VERSION saat ini. Kalau salah satu tidak cocok
    (basi ATAU formula sudah di-update sejak cache dibuat), return dict kosong
    — pemanggil akan fallback ke fetch fresh untuk semua ticker.
    """
    if not os.path.exists(DAILY_SCAN_CACHE_FILE):
        return {}
    try:
        with open(DAILY_SCAN_CACHE_FILE) as f:
            cache = json.load(f)
        current_marker = get_current_trading_day_close_marker()
        if cache.get("trading_day_marker") != current_marker:
            print(f"📋 Daily scan cache basi (marker {cache.get('trading_day_marker')} != {current_marker}), diabaikan.")
            return {}
        if cache.get("formula_version") != SCORING_FORMULA_VERSION:
            print(f"📋 Daily scan cache dari formula versi lama "
                  f"({cache.get('formula_version')} != {SCORING_FORMULA_VERSION}), diabaikan — fetch ulang dengan formula terbaru.")
            return {}
        return cache.get("scored", {})
    except Exception as e:
        print(f"⚠️ Gagal membaca daily scan cache: {e}")
        return {}


def fetch_tickers_scored_with_cache(tickers):
    """
    Wrapper di atas fetch_all_tickers_scored() — cek cache bersama (dari scan
    malam jam 22:00 WIB) dulu untuk tiap ticker, HANYA fetch fresh untuk ticker
    yang TIDAK ada di cache (basi, belum pernah di-scan, atau cache belum pernah
    dibangun sama sekali — misal pemakaian pertama sebelum job malam pernah jalan).
    Format return SAMA persis dengan fetch_all_tickers_scored (results list,
    skip_reasons dict) — supaya pemanggil tidak perlu berubah.
    """
    cache = load_daily_scan_cache()
    cached_results = []
    tickers_needing_fetch = []
    for t in tickers:
        if t in cache:
            cached_results.append(cache[t])
        else:
            tickers_needing_fetch.append(t)

    if cached_results:
        print(f"📋 {len(cached_results)}/{len(tickers)} ticker dari cache malam ini "
              f"(hemat fetch), {len(tickers_needing_fetch)} perlu fetch baru")

    if not tickers_needing_fetch:
        return cached_results, {}

    fresh_results, skip_reasons = fetch_all_tickers_scored(tickers_needing_fetch)
    return cached_results + fresh_results, skip_reasons


def fetch_all_tickers_scored(tickers):
    """
    Runs the full EOD fetch + scoring for a list of tickers, synchronously start
    to finish. Meant to be called via a SINGLE asyncio.to_thread(...) wrapping
    the whole thing — this naturally takes several minutes for the full ISSI
    universe, which is fine for a background job.

    Returns (results, skip_reasons) — skip_reasons is a dict of ticker -> reason
    string for anything that DIDN'T make it into results. This exists because
    console scrollback in Pydroid isn't reliable for a run this long; having the
    bot report failures directly in Telegram gives real, persistent visibility.
    """
    results = []
    skip_reasons = {}
    for chunk_start in range(0, len(tickers), ITICK_CHUNK_SIZE):
        chunk = tickers[chunk_start:chunk_start + ITICK_CHUNK_SIZE]
        for ticker in chunk:
            try:
                res = compute_factor_scoring(ticker, include_quote_check=False)
                if res:
                    results.append(res)
                else:
                    skip_reasons[ticker] = "excluded (see console for specific reason)"
            except Exception as e:
                print(f"Error processing {ticker}: {e}")
                skip_reasons[ticker] = f"exception: {str(e)[:100]}"
            time.sleep(1.0)  # light pacing within a chunk
        is_last_chunk = (chunk_start + ITICK_CHUNK_SIZE) >= len(tickers)
        if not is_last_chunk:
            print(f"⏳ Fetch/scoring: cooling down {ITICK_COOLDOWN_SECONDS}s before next chunk "
                  f"({chunk_start + len(chunk)}/{len(tickers)} tickers done)...")
            time.sleep(ITICK_COOLDOWN_SECONDS)
    return results, skip_reasons


async def run_morning_brief(context: ContextTypes.DEFAULT_TYPE):
    try:
        if await asyncio.to_thread(is_idx_market_holiday_today):
            print("📅 Skipping Morning Brief — IDX market holiday today.")
            return

        sharia_universe = fetch_online_sharia_list()

        # Broader market backdrop: Wall St overnight, regional Asia, USD/IDR, oil,
        # plus real Indonesian market news headlines. Fetched once for the whole
        # brief, not per-stock.
        macro_context = await asyncio.to_thread(fetch_macro_context)
        news_headlines = await asyncio.to_thread(fetch_market_news_headlines)
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
                asyncio.to_thread(fetch_tickers_scored_with_cache, sharia_universe_list), timeout=1800
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
async def check_stock(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await safe_reply(update.message, 
            "Cara pakai: /check TICKER [zapi]\nContoh: /check BBCA\nContoh (+ data broker Zapi): /check BBCA zapi"
        )
        return

    ticker = context.args[0].upper().strip()
    use_zapi = len(context.args) > 1 and context.args[1].lower() == "zapi"
    await safe_reply(update.message, f"🔎 Menganalisa {ticker}, mohon tunggu...")

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(compute_factor_scoring, ticker), timeout=1800
        )
    except asyncio.TimeoutError:
        await safe_reply(update.message, f"⚠️ Timeout mengambil data untuk {ticker}. Coba lagi nanti.")
        return
    except Exception as e:
        await safe_reply(update.message, f"⚠️ Gagal mengambil data untuk {ticker}: {e}")
        return

    if not result:
        await safe_reply(update.message, 
            f"⚠️ Gagal mengambil data untuk {ticker}. Kemungkinan penyebab:\n"
            "- Kode saham salah atau belum listing cukup lama\n"
            "- iTick sedang rate-limited — ini SERING terjadi jika /check dijalankan "
            "bersamaan dengan atau segera setelah proses scan besar (morning brief/whitelist "
            "build), karena keduanya berbagi kuota API yang sama\n\n"
            "Coba lagi setelah beberapa menit, terutama jika ada proses scan besar yang "
            "baru saja/sedang berjalan."
        )
        return

    sharia_universe = fetch_online_sharia_list()
    result["is_sharia"] = ticker in sharia_universe

    # Intraday live context (5m bars) for /check only — used to update live price,
    # high/low, momentum, and breakout probability while market is open.
    try:
        intraday_ctx = await asyncio.to_thread(fetch_intraday_market_context, ticker)
        result["intraday_momentum"] = intraday_ctx.get("momentum", {"available": False, "reason": "error teknis"})
        result["intraday_breakout"] = intraday_ctx.get("breakout", {"available": False, "reason": "error teknis"})
        result["active_breakout"] = intraday_ctx.get("active_breakout", {"available": False, "reason": "error teknis"})
        if intraday_ctx.get("available"):
            if intraday_ctx.get("price") is not None:
                result["price"] = intraday_ctx["price"]
            if intraday_ctx.get("high") is not None:
                result["intraday_high"] = intraday_ctx["high"]
            if intraday_ctx.get("low") is not None:
                result["intraday_low"] = intraday_ctx["low"]
            if intraday_ctx.get("vwap_snapshot"):
                result["intraday_vwap"] = intraday_ctx.get("vwap_snapshot")
    except Exception as e:
        print(f"⚠️ Gagal fetch intraday context untuk {ticker}: {e}")
        result["intraday_momentum"] = {"available": False, "reason": "error teknis"}
        result["intraday_breakout"] = {"available": False, "reason": "error teknis"}
        result["active_breakout"] = {"available": False, "reason": "error teknis"}

    # Intraday targets — entry range/TP1/TP2/SL yang lebih presisi dari data live
    # + history SQLite, TERPISAH dari scoring['targets'] yang dipakai /myportfolio dan winrate
    try:
        hist = get_ohlcv_smart(ticker, limit=60)
        result["intraday_targets"] = await asyncio.to_thread(
            compute_intraday_targets, ticker, result, hist if not hist.empty else None
        )
    except Exception as e:
        print(f"⚠️ Gagal compute intraday targets untuk {ticker}: {e}")
        result["intraday_targets"] = {}

    # Position-awareness: if this ticker is a current holding, feed the actual
    # position (cost basis, lots, unrealized P&L) into the analysis so the response
    # is framed relative to what the person actually owns, not just generic signal.
    portfolio = load_portfolio()
    held_position = portfolio.get("positions", {}).get(ticker)
    if held_position:
        avg_price = held_position["avg_price"]
        lots = held_position["lots"]
        current_price = result["price"]
        unrealized_pnl_pct = ((current_price - avg_price) / avg_price) * 100
        unrealized_pnl_idr = (current_price - avg_price) * lots * BOARD_LOT_SIZE
        result["is_held_position"] = True
        result["held_avg_price"] = avg_price
        result["held_lots"] = lots
        result["held_unrealized_pnl_pct"] = round(unrealized_pnl_pct, 2)
        result["held_unrealized_pnl_idr"] = int(unrealized_pnl_idr)
    else:
        result["is_held_position"] = False

    # Free enrichment: if this ticker already has same-day real broker flow data
    # cached (from an earlier /myportfolio brokersum run), pick it up here at ZERO
    # extra API cost — /check never fetches this on its own, only reuses what's
    # already been paid for today. EXCEPT if "zapi" explicitly requested — that
    # always fetches fresh from Zapi (belum diverifikasi apakah update live atau
    # harian, jadi TIDAK dipakai default/otomatis, hanya kalau diminta eksplisit).
    if use_zapi:
        try:
            zapi_brokersum = await asyncio.to_thread(
                compute_brokersum_metrics_zapi, ticker, result.get("cmf"), result.get("obv_divergence")
            )
            if zapi_brokersum:
                result["brokersum"] = zapi_brokersum
                apply_brokersum_adjustment(result, zapi_brokersum)
                print(f"📋 /check {ticker}: enriched with Zapi brokersum (fresh fetch)")
            else:
                await safe_reply(update.message, f"⚠️ Gagal mengambil data Zapi untuk {ticker}, lanjut tanpa data broker.")
        except Exception as e:
            print(f"⚠️ Zapi brokersum gagal untuk {ticker}: {e}")
    else:
        cached_brokersum = get_cached_brokersum(ticker)
        if cached_brokersum:
            result["brokersum"] = cached_brokersum
            print(f"📋 /check {ticker}: enriched with cached same-day brokersum data")

    # Company-specific news + real dividend/split history — this is what can
    # actually surface corporate actions like buybacks or earnings releases,
    # unlike the general market-wide news used in the daily briefs.
    company_news = await asyncio.to_thread(fetch_company_news, ticker, result.get("name", ticker))
    corporate_actions = await asyncio.to_thread(fetch_recent_corporate_actions, ticker)
    result["recent_news"] = [h["title"] for h in company_news]
    result["recent_dividends"] = corporate_actions["recent_dividends"]
    result["recent_splits"] = corporate_actions["recent_splits"]

    # Second message: deterministic, short, and focused on the live signal at /check time.
    # This replaces the earlier long Gemini paragraph that often repeated dashboard fields.
    analysis_text = build_check_signal_summary(result)

    # Raw deterministic data — plain Python formatting, NOT AI-generated — so every
    # number Gemini referenced above can be independently cross-checked directly.
    freshness_line = f"⚠️ {result['data_freshness_warning']}\n" if result.get("data_freshness_warning") else ""
    brokersum_line = ""
    if result.get("brokersum"):
        bs = result["brokersum"]
        source = bs.get("source", "api")
        source_label = {"screenshot": "screenshot", "zapi": "Zapi"}.get(source, "Index Alpha")
        adjusted_note = " [SUDAH MENGUBAH SKOR]" if result.get("brokersum_adjusted") else " [info saja, konsentrasi <10%]"
        trend = bs.get("trend", {})
        trend_str = ""
        if trend.get("trend") and trend["trend"] != "tidak_ada_histori":
            shift_note = "vs hari bursa sebelumnya" if trend.get("is_single_day_shift") else f"vs {trend.get('compared_to_date')}"
            trend_str = f"\nTren: {trend['trend']} ({shift_note}, delta Rp{trend.get('delta_idr', 0):,})"
        estimate_note = " (ESTIMASI dari volume x harga, bukan value asli)" if bs.get("net_foreign_flow_idr_is_estimate") else ""
        zapi_extra = ""
        if source == "zapi":
            zapi_extra = (
                f"\nBid: {bs.get('bid')} ({bs.get('bid_volume')} lembar) | "
                f"Offer: {bs.get('offer')} ({bs.get('offer_volume')} lembar)\n"
                f"Non-Reguler: Vol {bs.get('non_regular_volume')} | Value Rp{bs.get('non_regular_value') or 0:,}"
            )
        broker_detail_lines = ""
        if bs.get("top_net_buyers") or bs.get("top_net_sellers"):
            broker_detail_lines = (
                f"\nTop Net Buyers: {bs.get('top_net_buyers')}\n"
                f"Top Net Sellers: {bs.get('top_net_sellers')}"
            )
        elif source == "zapi":
            broker_detail_lines = "\n(Rincian per-broker tidak tersedia dari Zapi — hanya total net foreign di atas)"

        brokersum_line = (
            f"\n💹 BROKER RIIL ({source_label}, {bs.get('lookback_days')} hari, cache):\n"
            f"Broker Flow ALL 3D: {bs.get('net_foreign_flow_pct')}%{estimate_note} | "
            f"Konsentrasi: {bs.get('broker_concentration_pct')}%{adjusted_note}"
            f"{trend_str}"
            f"{zapi_extra}\n"
            f"Proxy Agreement: {bs.get('proxy_agreement')}"
            f"{broker_detail_lines}"
        )

    intraday_line = (
        f"Intraday: High {result['intraday_high']} | Low {result['intraday_low']}\n"
        if result.get('intraday_high') else ""
    )

    # ── Icon helpers ──────────────────────────────────────────────
    def _icon_score(v):
        if v is None: return "➖"
        return "✅" if v >= 7.0 else "❗" if v < 5.5 else "➖"

    def _icon_rsi(v):
        if v is None: return "➖"
        if v > 70: return "⚠️"
        if v < 30: return "🔥"
        if v > 60: return "📈"
        if v < 40: return "📉"
        return "➖"

    def _icon_cmf(v):
        if v is None: return "➖"
        if v > 0.1:  return "✅"
        if v > 0.0:  return "📈"
        if v > -0.1: return "❗"
        return "🔴"

    def _icon_vol(v):
        if v is None: return "➖"
        if v >= 3.0: return "🔥"
        if v >= 1.5: return "✅"
        if v >= 1.2: return "📈"
        if v >= 0.8: return "➖"
        return "❗"

    def _icon_adx(v):
        if v is None: return "➖"
        if v >= 30: return "✅"
        if v >= 20: return "📈"
        return "➖"

    def _icon_macd(state):
        return "✅" if state == "bullish" else "❗" if state == "bearish" else "➖"

    def _icon_rs(v):
        if v is None: return "➖"
        if v > 1.0:  return "✅"
        if v < -1.0: return "❗"
        return "➖"

    def _fmt(n):
        """Format angka harga dengan titik ribuan."""
        try: return f"{int(n):,}".replace(",", ".")
        except: return str(n)

    # ── Data alias ─────────────────────────────────────────────────
    price  = result["price"]
    scores = result["scores"]
    it     = result.get("intraday_targets", {})
    im     = result.get("intraday_momentum", {})
    hc     = result.get("high_conviction", {})
    RISK_CHARACTER_LABEL_ID = {
        "BASE_DEFENSIF": "🛡️ BASE/DEFENSIF",
        "SWING_AGRESIF": "⚡ SWING/AGRESIF",
        "NETRAL": "➖ NETRAL",
    }
    risk_label = RISK_CHARACTER_LABEL_ID.get(result.get("risk_character"), "")

    # ── Skor baris ─────────────────────────────────────────────────
    sv, sm, ss, sf = scores["value"], scores["momentum"], scores["sentiment"], scores["final"]
    skor_line = (
        f"💯 SKOR FINAL: {sf}\n"
        f"{_icon_score(sv)} Nilai {sv}  |  "
        f"{_icon_score(sm)} Momentum {sm}  |  "
        f"{_icon_score(ss)} Sentimen {ss}"
    )

    # ── Target intraday ────────────────────────────────────────────
    if it:
        above = " ▲" if it.get("entry_atas_above_price") else ""
        target_lines = (
            f"📈 HARGA & TARGET\n"
            f"Harga  : {_fmt(price)}\n"
            f"Entry  : {_fmt(it['entry_bawah'])} — {_fmt(it['entry_atas'])}{above}\n"
            f"TP1    : {_fmt(it['tp1'])}  ({(it['tp1']/price-1)*100:+.1f}%)\n"
        )
        if it.get("tp2"):
            target_lines += f"TP2    : {_fmt(it['tp2'])}  ({(it['tp2']/price-1)*100:+.1f}%)\n"
        target_lines += (
            f"SL     : {_fmt(it['sl'])}  ({(it['sl']/price-1)*100:+.1f}%)\n"
            f"RR     : TP1=1:{it['rr_tp1']}"
            + (f"  |  TP2=1:{it['rr_tp2']}" if it.get("rr_tp2") else "") + "\n"
            f"↳ {it['entry_bawah_context']}"
        )
    else:
        tgt = result.get("targets", {})
        target_lines = (
            f"📈 HARGA & TARGET\n"
            f"Harga  : {_fmt(price)}\n"
            f"Entry  : {tgt.get('buy_range', '?')}\n"
            f"TP1    : {_fmt(tgt.get('tp_1', '?'))}\n"
            f"SL     : {_fmt(tgt.get('cut_loss', '?'))}"
        )

    # ── Posisi (kalau saham ini dipegang) ─────────────────────────
    posisi_lines = ""
    portfolio = load_portfolio()
    positions = portfolio.get("positions", {})
    ticker = result["ticker"]
    if ticker in positions:
        pos = positions[ticker]
        avg = pos.get("avg_price", 0)
        lots = pos.get("lots", 0)
        days = pos.get("days_held")
        pnl_pct = (price - avg) / avg * 100 if avg else 0
        pnl_rp  = (price - avg) * lots * 100
        pnl_icon = "✅" if pnl_pct >= 0 else ("⚠️" if pnl_pct >= -5 else "🔴")
        posisi_lines = (
            f"\n💼 POSISI SAYA\n"
            f"{pnl_icon} {lots} lot @avg {_fmt(avg)}  "
            f"({pnl_pct:+.1f}%)\n"
            f"P/L  : {'+' if pnl_rp >= 0 else '-'}Rp{abs(pnl_rp):,.0f}"
            + (f"  |  {days} hari" if days else "")
        )

    # ── Intraday status ────────────────────────────────────────────
    intraday_status = ""
    hi = result.get("intraday_high")
    lo = result.get("intraday_low")
    if hi and lo:
        intraday_status += f"\n⚡ INTRADAY\nHigh {_fmt(hi)}  |  Low {_fmt(lo)}\n"
    vwap_fb = result.get("intraday_vwap") or {}
    if vwap_fb.get("available"):
        vwap_ref = vwap_fb.get("vwap_raw", vwap_fb.get("vwap", price))
        vwap_sign = "di atas" if price >= vwap_ref else "di bawah"
        vp = vwap_fb.get("volume_pace_ratio")
        vp_txt = f" | Vol pace {vp}x" if vp is not None else ""
        intraday_status += f"VWAP {_fmt(vwap_fb.get('vwap'))} ({vwap_fb.get('vwap_distance_pct'):+.2f}%, {vwap_sign}){vp_txt}\n"
    if im.get("available"):
        sess = "Sesi 1" if im["session"] == "sesi_1" else "Sesi 2"
        intraday_status += f"Momentum {sess}: {im['reading']} ({im['change_pct']:+.2f}%)\n"
    elif hi and lo:
        intraday_status += "Momentum: di luar jam bursa\n"

    br = result.get("intraday_breakout", {})
    if br.get("available"):
        br_icon = "🔥" if br["label"] == "TINGGI" else "📈" if br["label"] == "SEDANG" else "⬇️"
        cluster_note = f" ({br['resistance_cluster_count']}x)" if br.get("resistance_cluster_count", 1) > 1 else ""
        status_note = f"  [{br['breakout_status_label']}]" if br.get("breakout_status_label") else ""
        intraday_status += (
            f"{br_icon} Breakout: {br['label']} ({br['score']}/100){status_note}"
            f"  Resist {_fmt(br['resistance'])}{cluster_note}"
            f"  Jarak {br['distance_pct']:+.1f}%"
        )
        if br.get("volume_warning"):
            intraday_status += f"\n{br['volume_warning']}"
    elif hi and lo:
        br_reason = br.get("reason") or "data belum cukup"
        intraday_status += f"Peluang Breakout: tidak tersedia ({br_reason})"

    ab = result.get("active_breakout", {})
    if ab.get("available"):
        intraday_status += (
            f"\n⚡ Active Breakout: {ab.get('label')} ({ab.get('score')}/100)"
            f"  Trigger {_fmt(ab.get('trigger_price'))}"
            f"  VWAP {_fmt(ab.get('vwap'))}"
            f"  Invalid {_fmt(ab.get('invalidation_level'))}"
        )
        if ab.get("volume_pace_ratio") is not None:
            intraday_status += f"\nVol pace {ab.get('volume_pace_ratio')}x | {ab.get('notes', '')}"

    # ── Conviction + karakter ──────────────────────────────────────
    meta_line = ""
    if hc: meta_line += f"{hc.get('conviction_label', '')}\n"
    if risk_label: meta_line += risk_label

    # ── Raw data dengan icon ───────────────────────────────────────
    rsi  = result.get("rsi")
    cmf  = result.get("cmf")
    vol  = result.get("vol_ratio")
    adx  = result.get("adx")
    macd = result.get("macd_state", "")
    rs   = result.get("relative_strength_vs_ihsg")
    macd_cross = ""
    if result.get("macd_cross_days_ago") is not None:
        macd_cross = f" (cross {result['macd_cross_direction']} {result['macd_cross_days_ago']}hr lalu)"

    raw_data_block = (
        f"─────────────────────\n"
        f"PE {result['pe']}  |  PB {result['pb']}  |  Div {result['dividend_yield_pct']}%\n"
        f"{_icon_rsi(rsi)} RSI {rsi}  |  "
        f"{_icon_cmf(cmf)} CMF {cmf}  |  "
        f"{_icon_vol(vol)} Vol {vol}x\n"
        f"{_icon_macd(macd)} MACD {macd}{macd_cross}  |  "
        f"{_icon_adx(adx)} ADX {adx} ({format_adx_label(adx)})\n"
        f"{_icon_rs(rs)} RS vs IHSG {rs}%  |  Range {result['day_range_pct_10d']}%"
    )

    # ── Tanggal & jam ──────────────────────────────────────────────
    now_wib = datetime.datetime.now(WIB)
    date_str = now_wib.strftime("%d %b %Y")
    session_info = get_current_idx_session()
    if session_info == "sesi_1":
        jam_str = "🟢 Sesi 1 berlangsung"
    elif session_info == "sesi_2":
        jam_str = "🟢 Sesi 2 berlangsung"
    else:
        jam_str = "🕐 Di luar jam bursa"

    # ── PESAN 1 — Dashboard ────────────────────────────────────────
    msg1 = "\n".join(filter(None, [
        f"{result.get('name', ticker)} ({ticker})",
        f"📅 {date_str}  |  {jam_str}",
        "",
        f"🎯 {result['action_label_id']}",
        meta_line,
        "",
        skor_line,
        "",
        target_lines,
        posisi_lines,
        intraday_status,
        "",
        raw_data_block,
    ]))
    await safe_reply(update.message, msg1)

    # Pesan 2 — sinyal ringkas pada waktu /check dijalankan
    await safe_reply(update.message, analysis_text)

    # Offer optional Broker Sum screenshot enrichment — SKIP entirely kalau sudah
    # ada data broker dari sumber lain (cache Index Alpha, atau baru saja fetch
    # Zapi via "/check TICKER zapi") — menawarkan lagi untuk data yang sama itu
    # redundant dan bisa membingungkan ("kenapa ditawari lagi, sudah ada datanya").
    if not result.get("brokersum"):
        PENDING_BROKERSUM_CHECKS[update.effective_chat.id] = {
            "ticker": ticker,
            "expires_at": datetime.datetime.now(WIB) + datetime.timedelta(minutes=PENDING_BROKERSUM_TIMEOUT_MINUTES),
        }
        keyboard = InlineKeyboardMarkup([[InlineKeyboardButton("Lewati (tidak ada screenshot)", callback_data="skip_brokersum")]])
        await update.message.reply_text(
            f"📸 Ada info Broker Sum untuk {ticker} hari ini dari app-mu? Kirim screenshot kalau ada, "
            f"atau lewati saja.",
            reply_markup=keyboard,
        )


def build_ticker_shortcut_keyboard(tickers: list, columns: int = 4) -> InlineKeyboardMarkup:
    """
    Grid tombol shortcut per ticker — dipakai di /summary untuk cek cepat tanpa
    ketik manual. callback_data dibatasi 64 byte oleh Telegram, format
    "qchk_TICKER" jauh di bawah itu jadi aman.
    """
    buttons = [InlineKeyboardButton(t, callback_data=f"qchk_{t}") for t in tickers]
    rows = [buttons[i:i + columns] for i in range(0, len(buttons), columns)]
    return InlineKeyboardMarkup(rows)


async def skip_brokersum_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    PENDING_BROKERSUM_CHECKS.pop(query.message.chat_id, None)
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text("👍 Dilewati.")


async def order_clear_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    ticker = query.data.replace("orderclear_", "")
    orders = load_pending_orders()
    orders = [o for o in orders if o["ticker"] != ticker]
    save_pending_orders(orders)
    await query.message.reply_text(f"✅ Order {ticker} dihapus dari pemantauan.")


async def quick_check_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Tombol shortcut ticker diklik — hasil RINGKAS (bukan replika penuh /check,
    yang punya opsi zapi/brokersum yang butuh interaksi tambahan) supaya cocok
    untuk use case "lihat cepat" dari tombol, bukan analisis mendalam. Kalau
    butuh detail lengkap (brokersum, dll), tetap arahkan ke /check manual.
    Sengaja TIDAK menyentuh/reuse check_stock() — dibangun terpisah dari fungsi
    inti yang sudah stabil (compute_factor_scoring, fetch_intraday_momentum)
    untuk menghindari risiko mengubah command /check yang sudah established.
    """
    query = update.callback_query
    await query.answer("Mengambil data...")
    ticker = query.data.replace("qchk_", "")

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(compute_factor_scoring, ticker), timeout=1800
        )
    except Exception as e:
        await query.message.reply_text(f"⚠️ Gagal mengambil data {ticker}: {e}")
        return
    if not result:
        await query.message.reply_text(f"⚠️ Gagal mengambil data {ticker}.")
        return

    try:
        momentum = await asyncio.to_thread(fetch_intraday_momentum, ticker)
    except Exception:
        momentum = {"available": False}

    momentum_line = ""
    if momentum.get("available"):
        session_label = "Sesi 1" if momentum["session"] == "sesi_1" else "Sesi 2"
        momentum_line = f"\nMomentum {session_label}: {momentum['reading']} ({momentum['change_pct']:+.2f}%)"

    targets = result.get("targets", {})
    rr_max = targets.get("risk_reward_at_max")
    rr_line = f" | RR: 1:{rr_max}" if rr_max is not None else ""

    text = (
        f"⚡ {ticker} — {result.get('name', '')}\n"
        f"Harga: {result['price']} | RSI: {result['rsi']} | ADX: {result['adx']}\n"
        f"{result['action_label_id']}{momentum_line}\n"
        f"Entry: {targets.get('buy_range', '?')} | SL: {targets.get('cut_loss', '?')} | "
        f"TP1: {targets.get('tp_1', '?')}{rr_line}\n\n"
        f"Detail lengkap: /check {ticker}"
    )
    await query.message.reply_text(text)



    query = update.callback_query
    await query.answer()
    PENDING_BROKERSUM_CHECKS.pop(query.message.chat_id, None)
    await query.edit_message_reply_markup(reply_markup=None)
    await query.message.reply_text("👍 Dilewati.")




async def select_screendaytrade_brokersum(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Callback tombol Broker Summary dari /screendaytrade.
    Setelah user tap ticker, foto berikutnya akan dibaca sebagai Broker Summary ALL 3 hari.
    """
    query = update.callback_query
    await query.answer()

    data = query.data or ""
    if not data.startswith("bsdt_"):
        return

    ticker = data.replace("bsdt_", "").upper().strip()
    if not ticker:
        await query.edit_message_text("⚠️ Ticker tidak valid.")
        return

    PENDING_BROKERSUM_CHECKS[query.message.chat_id] = {
        "ticker": ticker,
        "expires_at": datetime.datetime.now(WIB) + datetime.timedelta(minutes=PENDING_BROKERSUM_TIMEOUT_MINUTES),
        "source": "screendaytrade",
        "mode": "all_3d_net",
    }

    await query.edit_message_text(
        f"📸 Kirim screenshot Broker Summary untuk {ticker}\\n\\n"
        f"Format yang diminta:\\n"
        f"• Tab: ALL\\n"
        f"• Periode: 3 hari bursa\\n"
        f"• Mode: Net aktif\\n"
        f"• Pastikan kode broker, buy/sell, lot/value, dan average price terlihat.\\n\\n"
        f"Bot akan membaca arah akumulasi/distribusi dan memberi Smart Money Confirmation."
    )


async def handle_brokersum_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    pending = PENDING_BROKERSUM_CHECKS.get(chat_id)

    if not pending:
        # No recent /check waiting for a photo — don't guess which ticker this is for.
        return
    if datetime.datetime.now(WIB) > pending["expires_at"]:
        PENDING_BROKERSUM_CHECKS.pop(chat_id, None)
        await safe_reply(update.message, "⏱️ Waktu untuk kirim screenshot sudah lewat. Jalankan /check lagi kalau masih ingin menambahkan.")
        return

    ticker = pending["ticker"]
    PENDING_BROKERSUM_CHECKS.pop(chat_id, None)  # consume the pending state either way

    await safe_reply(update.message, f"🔍 Membaca screenshot untuk {ticker}...")

    try:
        photo = update.message.photo[-1]  # highest resolution available
        photo_file = await context.bot.get_file(photo.file_id)
        image_bytes = await photo_file.download_as_bytearray()

        extracted = await asyncio.to_thread(
            extract_brokersum_from_screenshot, bytes(image_bytes), "image/jpeg", ticker
        )
    except Exception as e:
        await safe_reply(update.message, f"⚠️ Gagal memproses gambar: {e}\nHasil /check {ticker} sebelumnya tidak berubah.")
        return

    if not extracted.get("success"):
        reason = extracted.get("reason_if_failed", "tidak jelas")
        await safe_reply(
            update.message,
            f"⚠️ Tidak bisa membaca data Broker Sum dari gambar ini ({reason}).\n"
            f"Hasil /check {ticker} sebelumnya tidak berubah — silakan coba screenshot lain kalau mau."
        )
        return

    brokersum = compute_brokersum_from_screenshot_data(extracted)
    if brokersum is None:
        await safe_reply(
            update.message,
            f"⚠️ Gemini menandai ekstraksi berhasil, tapi angka inti (buy/sell/net asing) "
            f"ternyata tidak terbaca sama sekali.\n"
            f"Hasil /check {ticker} sebelumnya tidak berubah — silakan coba screenshot lain kalau mau."
        )
        return
    await safe_reply(
        update.message,
        f"✅ Berhasil dibaca dari screenshot:\n"
        f"Broker Flow ALL 3D: {brokersum['net_foreign_flow_pct']}% (Rp{brokersum['net_foreign_flow_idr']:,})\n"
        f"Konsentrasi: {brokersum['broker_concentration_pct']}%\n\n"
        f"Menerapkan konfirmasi smart money ke analisis {ticker}..."
    )

    try:
        scoring = await asyncio.wait_for(asyncio.to_thread(compute_factor_scoring, ticker), timeout=1800)
    except Exception as e:
        await safe_reply(update.message, f"⚠️ Gagal mengambil ulang data {ticker} untuk menerapkan brokersum: {e}")
        return
    if not scoring:
        await safe_reply(update.message, f"⚠️ Gagal mengambil ulang data {ticker}.")
        return

    brokersum["proxy_agreement"] = "not_available"
    cmf = scoring.get("cmf")
    obv_divergence = scoring.get("obv_divergence")
    if cmf is not None and isinstance(cmf, (int, float)):
        proxy_bullish = cmf > 0
        real_bullish = brokersum["net_foreign_flow_pct"] > 0
        if obv_divergence == "bearish_divergence" and brokersum["net_foreign_flow_pct"] > 5:
            brokersum["proxy_agreement"] = "CONTRADICTION: proxy showed bearish OBV divergence but real broker flow (screenshot) is net positive"
        elif obv_divergence == "bullish_divergence" and brokersum["net_foreign_flow_pct"] < -5:
            brokersum["proxy_agreement"] = "CONTRADICTION: proxy showed bullish OBV divergence but real broker flow (screenshot) is net negative"
        elif proxy_bullish == real_bullish:
            brokersum["proxy_agreement"] = "confirms_proxy"
        else:
            brokersum["proxy_agreement"] = "diverges_from_proxy"

    scoring["brokersum"] = brokersum
    apply_brokersum_adjustment(scoring, brokersum)

    # Cache under the same trading-day-aware key Index Alpha uses, so this doesn't
    # get needlessly re-fetched from Index Alpha for the same ticker/day, and vice versa.
    brokersum["trend"] = compute_brokersum_trend(ticker, brokersum["net_foreign_flow_idr"])
    cache = _load_brokersum_cache()
    cache[ticker] = {"date": get_last_published_trading_day(), "data": brokersum}
    _save_brokersum_cache(cache)
    append_brokersum_history(ticker, brokersum)

    analysis_text = ask_gemini_to_analyze([scoring], SINGLE_CHECK_INSTRUCTION)
    await safe_reply(update.message, analysis_text)


async def show_version(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply(
        update.message,
        f"📌 Scoring formula version: {SCORING_FORMULA_VERSION}\n\n"
        "Skor dari versi formula yang berbeda tidak bisa dibandingkan langsung — "
        "jika skor untuk saham yang sama terlihat berbeda antar hari, cek dulu apakah "
        "versi formulanya berubah sebelum menganggap itu ketidakkonsistenan data."
    )


GLOSSARY_TEXT = """📖 KAMUS ISTILAH BOT

━━━ SKOR FAKTOR ━━━
Nilai (Value): valuasi saham — PE, PB, yield dividen. Tinggi = murah secara fundamental.
Momentum: arah & kekuatan tren harga — RSI, MACD, SMA, pola chart.
Sentimen: tekanan beli/jual dari volume — CMF, OBV, rasio volume.
Final: gabungan tertimbang dari ketiganya (30% Nilai, 40% Momentum, 30% Sentimen).

━━━ INDIKATOR TEKNIKAL ━━━
RSI (Relative Strength Index): 0-100, mengukur jenuh beli/jual berdasarkan riwayat harga saham itu sendiri (adaptif). Sekitar 45-55 = netral sehat; mendekati 65-75+ = mulai jenuh beli.
MACD: indikator TIMING — beda dari RSI. RSI = "apakah harga sudah jenuh", MACD = "apakah momentum baru saja berbalik arah". Cross bullish/bearish = momentum baru berubah arah, sinyal timing yang lebih segar.
SMA50: rata-rata harga 50 hari — konteks tren menengah. "Di Bawah SMA50: True" = tren menengah masih lemah meski jangka pendek terlihat oke.
CMF (Chaikin Money Flow): -1 sampai +1. Apakah volume besar itu benar tekanan BELI (closing dekat harga tertinggi hari itu) atau tekanan JUAL (closing dekat terendah) — bukan cuma "volume tinggi = bagus".
OBV Divergence: membandingkan arah harga vs arah volume kumulatif. "bearish_divergence" = harga tenang/naik tapi volume sebenarnya menunjukkan distribusi diam-diam (jual). "bullish_divergence" = kebalikannya (akumulasi diam-diam).
Rentang 10 hari (day_range_pct_10d): seberapa jauh harga bergerak dalam 10 hari terakhir (tertinggi vs terendah). Rendah = harga "beku"/kurang aktif — TAPI ini bisa juga berarti ada akumulasi/distribusi diam-diam (lihat OBV Divergence), bukan otomatis berarti "tidak menarik".
Pola Chart (lower_highs_bearish): rangkaian puncak harga yang makin menurun — sinyal pelemahan meski indikator lain terlihat oke.

━━━ DATA FUNDAMENTAL ━━━
PE (Price-to-Earnings): harga saham dibanding laba per saham. Rendah = relatif murah, TAPI PE sangat rendah (<3) bisa juga tanda laba tidak wajar/sementara.
PB (Price-to-Book): harga saham dibanding nilai buku aset. <1 = secara teori diperdagangkan di bawah nilai aset bersihnya.
Yield Dividen: dividen tahunan dibagi harga saham, dalam %. Makin tinggi = makin menarik untuk strategi dividend capture.

━━━ DATA BROKER RIIL (Index Alpha, opt-in /myportfolio brokersum, maks 5 saham/hari) ━━━
Net Foreign Flow %: real net beli-jual ASING (bukan domestik — dibatasi jadi asing-saja karena limit keras 5x/hari), -100% sampai +100%. Ini DATA ASLI, bukan proksi seperti CMF/OBV.
Broker Concentration %: seberapa terkonsentrasi net-buy asing pada beberapa broker teratas. Tinggi (>25%) = sinyal lebih kuat (sedikit pemain besar bergerak) dibanding net-buy yang sama tapi tersebar di banyak broker.
Proxy Agreement: apakah data broker riil ini SESUAI atau BERTENTANGAN dengan CMF/OBV kita. "CONTRADICTION" = perlu perhatian ekstra, dua sumber data saling bertolak belakang.
Brokersum Adjusted: jika True, data riil ini SUDAH mengubah skor Sentimen & Final serta keputusan sistem (bukan cuma catatan tambahan) — hanya terjadi jika Broker Concentration cukup tinggi (≥10%) untuk dipercaya.

━━━ KEPUTUSAN SISTEM (dihitung otomatis, bukan pilihan AI) ━━━
BELI KUAT: skor tinggi, tidak ada flag risiko.
BELI / AKUMULASI: skor cukup baik, ATAU skor tinggi tapi ada 1 flag caution (jadi diturunkan dari BELI KUAT).
TAHAN: skor sedang, ATAU ada flag serius (distress finansial/pola lower-highs) yang membatasi ke level ini.
HINDARI / JUAL: skor rendah.
SINYAL CAMPURAN: skor komponen (Nilai/Momentum/Sentimen) saling bertentangan tajam, ATAU skor berada tepat di garis batas ditambah flag caution — sengaja TIDAK memberi kepastian palsu saat data memang tidak jelas arahnya.

━━━ FLAG LAINNYA ━━━
Overbought Caution: RSI mendekati area jenuh beli KHUSUS untuk saham ini (adaptif, bukan angka baku).
Distress: PE/PB negatif (laba/ekuitas negatif) — tanda masalah keuangan nyata.
Dekat Harga Dasar: harga di bawah ~Rp70, potensi saham "beku"/distressed (mis. WEGE, GIAA).
PE Tidak Wajar Rendah: PE di bawah ~3, mungkin laba tidak berulang, bukan murah sungguhan.
Lonjakan Volume: volume 3x+ normal — bisa jadi berita besar atau likuiditas tipis, bukan otomatis bullish.

Ketik /check, /myportfolio, atau /testbrief untuk melihat istilah-istilah ini dalam analisis nyata."""


async def show_glossary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update.message, GLOSSARY_TEXT)


async def show_whitelist_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not os.path.exists(WHITELIST_CACHE_FILE):
        await safe_reply(update.message, "📋 Belum ada whitelist tersimpan — akan dibuat otomatis saat brief berikutnya jalan.")
        return
    try:
        with open(WHITELIST_CACHE_FILE) as f:
            cache = json.load(f)
        eligible = cache.get("eligible_tickers", [])
        excluded = cache.get("excluded_tickers", {})
        excluded_lines = "\n".join(f"- {t}: {r}" for t, r in list(excluded.items())[:20])
        await safe_reply(
            update.message,
            f"📋 Whitelist bulan {cache.get('generated_month')}:\n"
            f"Eligible: {len(eligible)} saham\n"
            f"Excluded: {len(excluded)} saham\n\n{excluded_lines}"
        )
    except Exception as e:
        await safe_reply(update.message, f"⚠️ Gagal membaca whitelist: {e}")


async def rebuild_whitelist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update.message, "🔄 Membangun ulang whitelist bulanan, ini akan memakan waktu beberapa menit...")
    try:
        sharia_universe = await asyncio.to_thread(fetch_online_sharia_list)
        eligible = await asyncio.to_thread(load_or_build_whitelist, list(sharia_universe), True)
        await safe_reply(update.message, f"✅ Whitelist selesai dibangun ulang: {len(eligible)} saham eligible.")
    except Exception as e:
        await safe_reply(update.message, f"⚠️ Gagal membangun ulang whitelist: {e}")


STARTUP_DISCLAIMER = (
    "⚠️ CATATAN PENTING (ditampilkan sekali saat bot aktif):\n"
    "Semua analisis dari bot ini HANYA berdasarkan data harga, volume, dan rasio "
    "valuasi dasar. TIDAK termasuk data net buy/sell broker asing/lokal, dan TIDAK "
    "mencakup pengumuman resmi IDX secara menyeluruh (laporan keuangan formal, aksi "
    "korporasi) — hanya berita publik yang tersedia.\n\n"
    "Ini adalah alat bantu screening berbasis data, BUKAN saran keuangan profesional. "
    "Selalu lakukan verifikasi independen sebelum mengambil keputusan investasi.\n\n"
    "Catatan ini tidak akan diulang lagi di setiap pesan — cukup jadi pengingat bahwa "
    "bot sedang aktif."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update.message, STARTUP_DISCLAIMER)
    await safe_reply(update.message, 
        "🤖 Sharia Stock Bot aktif.\n\n"
        "Perintah analisa:\n"
        "/check TICKER — analisa saham apa saja on-demand (aware posisi jika sudah dimiliki)\n"
        "  Contoh: /check TLKM\n"
        "/check TICKER zapi — + data broker riil dari Zapi (sumber alternatif,\n"
        "  belum terverifikasi live vs harian — lihat catatan di hasil)\n\n"
        "Perintah portofolio:\n"
        "/buy TICKER HARGA LOT — catat posisi beli (butuh cash cukup)\n"
        "  Contoh: /buy TLKM 2850 10\n"
        "/sell TICKER LOT HARGA — jual & hitung realized P&L otomatis\n"
        "  Contoh: /sell TLKM 5 2900\n"
        "/addcash JUMLAH — tambah saldo cash\n"
        "  Contoh: /addcash 5000000\n"
        "/withdrawcash JUMLAH — kurangi saldo cash\n"
        "/resetportfolio — hapus SEMUA posisi & cash (perlu konfirmasi)\n"
        "/setentrydate TICKER YYYY-MM-DD — isi/perbaiki tanggal beli (untuk kategori siklus)\n"
        "/watchlist — lihat/kelola watchlist (maks 3 saham, belum dimiliki)\n"
        "  /watchlist add TICKER | /watchlist remove TICKER\n"
        "/summary — ringkasan cepat + tombol cek harga cepat + order tracking\n"
        "/order buy TICKER LOT HARGA — pantau order beli (PERKIRAAN status, bukan\n"
        "  kepastian — kita tidak punya akses ke app broker, cuma bandingkan harga)\n"
        "/order sell TICKER LOT HARGA — sama untuk order jual\n"
        "/order — lihat semua order dipantau | /order clear TICKER — hapus pantauan\n"
        "/myportfolio — analisis mendalam + kategori siklus (Baru/Produktif/Hati-hati/Evaluasi)\n"
        "/myportfolio brokersum — + data broker asing RIIL (Index Alpha), auto-pilih 5\n"
        "  saham prioritas tertinggi (maks 5/hari — batas keras dari Index Alpha)\n"
        "/myportfolio brokersum TICKER1 TICKER2 — pilih manual saham mana yang dicek\n"
        "/myportfolio brokersum zapi — sama, tapi via Zapi, SEMUA posisi+watchlist\n"
        "  tercover sekaligus (budget lebih longgar, ~10-12/hari aman dari 300/bulan)\n"
        "Setelah /check, bot akan tanya apakah kamu punya screenshot Broker Sum dari\n"
        "  app-mu hari ini — kirim untuk memperkaya analisis, atau tekan tombol Lewati.\n\n"
        "/glossary — kamus istilah (RSI, MACD, CMF, OBV, dll) yang dipakai di analisis\n\n"
        "/screendaytrade — screening khusus saham paling aktif/volatil HARI INI\n"
        "  (beda bobot dari brief pagi, fokus momentum bukan value; tanpa data broker\n"
        "  karena horizonnya jam-menit, bukan cocok untuk data EOD)\n"
        "/screendaytrade issi — sama, tapi universe ISSI (ratusan saham syariah,\n"
        "  bukan cuma ISSI yang 70) dengan filter likuiditas (harga + volume 10hr\n"
        "  bursa >=500rb lembar). Cache 2 minggu — pemakaian pertama tiap 2 minggu\n"
        "  butuh beberapa menit (via Zapi bulk), setelahnya instan sampai 2 minggu berikutnya\n"
        "/winrate — scorecard uji akurasi rekomendasi /screendaytrade (TP1/CL asli\n"
        "  dari rekomendasi hari itu, entry = harga open besoknya, dicek harian\n"
        "  s/d 5 hari bursa). Picks otomatis terkunci tiap kali /screendaytrade\n"
        "  dijalankan, diresolusi tiap malam setelah scan jam 22:00\n\n"
        "Testing (jalankan manual tanpa nunggu jadwal):\n"
        "/testbrief — jalankan Morning Brief sekarang\n"
        "/testopening — jalankan Opening Dynamics sekarang\n\n"
        "Otomatis terjadwal:\n"
        "🌙 04:00 WIB — Morning Brief Sharia (ISSI), sebelum market buka\n"
        "🕤 09:45 WIB — Opening Dynamics (gap, momentum, volume pagi)"
    )



EXECUTION_GATE_AUTOPICKS = 12
EXECUTION_GATE_TOP_GAINERS = 8
EXECUTION_GATE_MAX_WATCHLIST = 20
EXECUTION_GATE_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "executiongate_cache.json")


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
    scored = load_daily_scan_cache()
    if scored:
        records = list(scored.values())
        pre, _ = filter_and_rank_daytrade_candidates(records, count=max(count, 20))
        return sorted(pre, key=lambda r: compute_scalping_readiness(r)["score"], reverse=True)[:count]

    # Fallback: use whitelist and perform normal fetch. This can be slow, but /executiongate still works after restart.
    sharia_universe = fetch_online_sharia_list()
    tickers = load_or_build_whitelist(list(sharia_universe))
    results, _ = fetch_tickers_scored_with_cache(tickers)
    pre, _ = filter_and_rank_daytrade_candidates(results, count=max(count, 20))
    return sorted(pre, key=lambda r: compute_scalping_readiness(r)["score"], reverse=True)[:count]


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




def compute_executiongate_bandarmology_proxy(scoring: dict) -> dict:
    """
    Proxy jejak akumulasi/distribusi khusus Execution Gate.

    Ini tidak mengklaim mengetahui identitas bandar. Input hanya berasal dari
    OHLCV, CMF, OBV, volume, posisi harga, dan broker-flow jika sudah tersedia.
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
    }


def executiongate_decision(scoring: dict) -> dict:
    """
    Wrapper atas Execution Gate lama:
    - Bandarmology hanya memberi bonus kecil.
    - Distribution risk tinggi memblokir ENTER.
    """
    result = _executiongate_decision_original(scoring)
    bandar = compute_executiongate_bandarmology_proxy(scoring)

    result["bandar_accumulation_score"] = bandar["accumulation_score"]
    result["bandar_distribution_risk"] = bandar["distribution_risk"]
    result["bandar_phase"] = bandar["phase"]

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
            decision = executiongate_decision(item)
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
    - holdings / positions dari myportfolio
    - watchlist dari myportfolio
    - testbrief / daily scan cache

    Tidak fetch top gainer seluruh universe.
    """
    import os
    import json

    score = {}

    def add(ticker, weight):
        t = str(ticker or "").upper().strip()
        if not t:
            return
        score[t] = max(score.get(t, 0), weight)

    # 1. Portfolio: holdings + watchlist
    try:
        pf = load_portfolio()

        positions = pf.get("positions") or pf.get("holdings") or {}
        if isinstance(positions, dict):
            for t in positions.keys():
                add(t, 100)
        elif isinstance(positions, list):
            for item in positions:
                if isinstance(item, str):
                    add(item, 100)
                elif isinstance(item, dict):
                    add(item.get("ticker") or item.get("code"), 100)

        for t in pf.get("watchlist", []) or []:
            add(t, 90)

    except Exception as e:
        print(f"⚠️ executiongate portfolio whitelist gagal: {str(e)[:80]}")

    # 2. Testbrief / daily scan cache
    try:
        daily = load_daily_scan_cache()
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
                add(r, 70)
            elif isinstance(r, dict):
                add(r.get("ticker") or r.get("code"), 70 + min(get_score(r), 20))

    except Exception as e:
        print(f"⚠️ executiongate testbrief whitelist gagal: {str(e)[:80]}")

    # Return list ticker saja supaya kompatibel dengan flow topg lama
    return [
        t for t, _ in sorted(score.items(), key=lambda kv: kv[1], reverse=True)
    ][:max_items]


async def executiongate_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    status = get_executiongate_session_status()
    if not status["allowed"]:
        await safe_reply(update.message, f"⛔ /executiongate hanya aktif saat market live atau break siang. Status sekarang: {status['label']}.")
        return

    await safe_reply(update.message, f"🧭 Execution Gate berjalan ({status['label']}). Mengambil 12 autopick screendaytrade + 8 top gainer 30 menit pertama...")

    try:
        sharia_universe = list(fetch_online_sharia_list())
        wl = await asyncio.to_thread(load_or_build_whitelist, sharia_universe)
        autopicks = await asyncio.wait_for(asyncio.to_thread(get_executiongate_screendaytrade_autopicks, EXECUTION_GATE_AUTOPICKS), timeout=1800)
        for r in autopicks:
            r["executiongate_source"] = "screendaytrade"
        topg = await asyncio.to_thread(build_executiongate_extra_candidates, EXECUTION_GATE_TOP_GAINERS)  # portfolio/watchlist/testbrief whitelist

        scored_by_ticker = {r.get("ticker"): r for r in autopicks if r.get("ticker")}
        watch = list(scored_by_ticker.values())
        for tg in topg:
            t = tg.get("ticker")
            if not t or t in scored_by_ticker:
                continue
            base = compute_factor_scoring(t, include_quote_check=False)
            if not base:
                continue
            base["executiongate_source"] = f"top30m +{tg.get('first30_change_pct')}%"
            base["first30_change_pct"] = tg.get("first30_change_pct")
            watch.append(base)
            if len(watch) >= EXECUTION_GATE_MAX_WATCHLIST:
                break

        evaluated = await asyncio.wait_for(asyncio.to_thread(evaluate_executiongate_watchlist, watch[:EXECUTION_GATE_MAX_WATCHLIST]), timeout=1800)
    except asyncio.TimeoutError:
        await safe_reply(update.message, "⏱️ Execution Gate timeout. Coba ulang beberapa menit lagi atau pastikan cache screendaytrade sudah tersedia.")
        return
    except Exception as e:
        await safe_reply(update.message, f"⚠️ Execution Gate gagal: {str(e)[:200]}")
        return

    lines = [f"🧭 EXECUTION GATE — {status['label']}\n"]
    lines.append("ENTER sangat ketat: harga harus sehat vs VWAP, active breakout valid, vol pace hidup, risk/RR tidak buruk.\n")
    shown = 0
    for r in evaluated[:EXECUTION_GATE_MAX_WATCHLIST]:
        shown += 1
        icon = "🟢" if r["decision"] == "ENTER" else ("🟡" if r["decision"] == "WATCH" else "🔴")
        vp = r.get("vol_pace") if r.get("vol_pace") is not None else "-"
        lines.append(
            f"{shown}. {icon} {r['ticker']} — {r['decision']} ({r.get('gate_score',0)}/100) [{r.get('source','?')}]\n"
            f"   Breakout {r.get('breakout_score',0)}/100 | Risk {r.get('risk_score',0)}/100 | Active {r.get('active_score',0)}/100 {r.get('active_label','')}\n"
            f"   Harga {smart_round_price(r.get('price',0))} | VWAP dist {r.get('vwap_dist','-')}% | Vol pace {vp}x | Trigger {r.get('trigger') or '-'}\n"
            f"   Aksi: {r.get('action','-')} | Alasan: {', '.join(r.get('reasons',[])[:4])}\n"
        )
    lines.append("\nRule: ENTER boleh dipertimbangkan; WATCH tunggu /check membaik; FAIL no entry.")
    await send_long_message(update.message, "\n".join(lines))

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




def compute_screendaytrade_positive_bias(r: dict) -> dict:
    """
    Refactor ranking /screendaytrade:
    Pisahkan Fresh Breakout lane dan Continuation lane.
    Tujuan: memperkuat probabilitas saham positif, bukan sekadar volatil.
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

    if fresh_ok and fresh_score >= continuation_score:
        lane = "PRIORITY FRESH"
        score = fresh_score + 6
        priority = 3
    elif continuation_ok:
        lane = "PRIORITY CONT"
        score = continuation_score + 6
        priority = 3
    elif rm < 55 and a < 75:
        lane = "LOW EDGE / CHASE"
        score = max(fresh_score, continuation_score) - 8
        priority = 1
    else:
        lane = "SECONDARY WATCH"
        score = max(fresh_score, continuation_score)
        priority = 2

    # Live active breakout tetap bonus kecil, bukan penentu utama.
    if active_score >= 60:
        score += min((active_score - 60) / 4, 8)

    # Chase risk dari label lama tetap diberi penalti.
    old_label = str(v5.get("label", "")).upper()
    if "CHASE" in old_label:
        score -= 10
        if lane != "LOW EDGE / CHASE":
            lane = "EXTENDED / CHASE WATCH"
            priority = min(priority, 1)

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
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "latest_screendaytrade_picks.json")
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


async def screen_daytrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Screening khusus day trade — TERPISAH dari brief pagi (bobot berbeda: fokus
    volatilitas/momentum/aktivitas sekarang, bukan value seimbang), dan TIDAK
    memakai Index Alpha sama sekali (data broker EOD, lag 1 hari, tidak cocok
    untuk horizon day trade jam-menit — kuota Index Alpha sebaiknya disimpan
    untuk /myportfolio yang horizonnya swing 2-10 hari, sesuai kesepakatan).

    Opsi "/screendaytrade issi" — universe JAUH lebih luas (ISSI, ratusan saham
    syariah, bukan cuma ISSI yang 70) dengan filter likuiditas tambahan
    (harga + volume rata-rata 10hr bursa >= 500rb lembar). Cache 2 MINGGU (14 hari kalender dari waktu build
    Sabtu) — build pertama di minggu itu MAHAL (bisa 30+ menit), builds
    berikutnya dalam minggu yang sama pakai cache, gratis/cepat.
    """
    use_issi = len(context.args) > 0 and context.args[0].lower() == "issi"

    if use_issi:
        await safe_reply(
            update.message,
            "🔎 Screening universe ISSI (lebih luas dari ISSI), mohon tunggu — "
            "kalau ini pemakaian pertama dalam 2 minggu terakhir, build whitelist likuid bisa "
            "makan waktu beberapa menit (via Zapi bulk, jauh lebih cepat dari sebelumnya). "
            "Setelah itu, pemakaian berikutnya dalam 2 minggu ini akan instan (pakai cache)."
        )
        tickers = await asyncio.to_thread(load_or_build_issi_liquid_whitelist)
    else:
        await safe_reply(update.message, "🔎 Screening kandidat day trade, mohon tunggu (~beberapa menit, sama seperti /testbrief)...")
        sharia_universe = fetch_online_sharia_list()
        tickers = await asyncio.to_thread(load_or_build_whitelist, list(sharia_universe))

    scan_timeout = 5400  # 90 menit: cukup untuk universe besar dan cache Yahoo
    try:
        # fetch_tickers_scored_with_cache() cek cache scan malam dulu — kalau
        # dipanggil setelah jam 22:00 di hari yang sama, mode "issi" (universe
        # SAMA dengan yang di-scan job malam) bisa jadi nyaris instan.
        results, skip_reasons = await asyncio.wait_for(
            asyncio.to_thread(fetch_tickers_scored_with_cache, tickers), timeout=scan_timeout
        )
    except asyncio.TimeoutError:
        await safe_reply(update.message, f"⏱️ Screening melebihi batas waktu {scan_timeout // 60} menit. Coba lagi nanti.")
        return

    if not results:
        await safe_reply(update.message, "⚠️ Tidak ada data yang berhasil diambil. Coba lagi nanti.")
        return

    # Stage 1 V5: setup lane + active closing momentum lane. Stage 2: live intraday breakout on shortlist only.
    pre_candidates, filter_tier_note = select_screendaytrade_v5_candidates(results, count=max(DAYTRADE_FINAL_PICKS_COUNT, 20))
    live_pool = await asyncio.to_thread(
        enrich_live_breakout_for_candidates, pre_candidates, 25, True
    )
    ready_pool = [r for r in live_pool if r.get("active_breakout", {}).get("available") and r["active_breakout"].get("score", 0) >= 60]

    # Refactor ranking:
    # Tidak lagi murni scalping_readiness snapshot.
    # Ranking final memisahkan Fresh Breakout dan Strong Continuation.
    top_candidates = rank_screendaytrade_refactor(live_pool, DAYTRADE_FINAL_PICKS_COUNT)

    if len(ready_pool) >= DAYTRADE_FINAL_PICKS_COUNT:
        filter_tier_note = filter_tier_note + " + Positive Bias lane refactor + live active breakout context"
    else:
        filter_tier_note = filter_tier_note + " + Positive Bias lane refactor (fallback karena kandidat READY terbatas)"

    # Kunci picks hari ini untuk uji winrate — idempotent (tidak duplikat kalau
    # /screendaytrade dipanggil berkali-kali di hari yang sama).
    await asyncio.to_thread(lock_daily_daytrade_picks, top_candidates)
    await asyncio.to_thread(save_latest_screendaytrade_picks, top_candidates)

    lines = ["⚡ SCREENING DAY TRADE - RADAR BREAKOUT V5 ACTIVITY\n"]
    lines.append(f"Kriteria: {filter_tier_note}\n")
    lines.append("Catatan: Ini RADAR, bukan entry final. Entry live wajib lewat /executiongate atau /check.\n")
    lines.append("Legend: B=Breakout, C=Continuation, Act=Activity/Liquidity, VolQ=Volume Breakout Quality, Room=Entry Room, Risk=risiko teknikal.\n")

    for i, r in enumerate(top_candidates, 1):
        v5 = compute_daytrade_v5_summary(r)
        ab = r.get("active_breakout", {})
        src_live = ""
        if ab.get("available"):
            src_live = f" | Active {ab.get('score')}/100 {ab.get('label')}"
        room = v5["room"]
        cont = v5["continuation"]
        risk = v5["risk"]
        br = v5["breakout"]
        volq = v5["volq"]
        lines.append(
            f"{i}. {r['ticker']} — {v5['label']}\n"
            f"   Total {v5['total']}/100 | Bias {r.get('_positive_bias', '-')}/100 | Lane {r.get('_positive_lane', '-')} | B {br['score']} | C {cont['score']} | Act {v5['activity']['score']} | VolQ {volq['score']} | Room {room['score']} | Safety {risk['score']}{src_live}\n"
            f"   Harga {r.get('price')} | Valid >{v5['valid_level']} | Ideal {v5['ideal']} | Invalid <{v5['invalid']}\n"
            f"   Room: {room['label']} ({room['dist_high_pct']}% ke high, upside TP1 {room['upside_tp1_pct']}%) | VolQ: {volq['label']} | Continuation: {cont['label']}\n"
            f"   Note: {v5['note']}"
        )

    await safe_reply(update.message, "\n\n".join(lines))

    # Tombol upload Broker Summary ALL 3 hari untuk 12 saham hasil radar.
    try:
        buttons = []
        row = []
        for r in top_candidates:
            t = str(r.get("ticker", "")).upper().strip()
            if not t:
                continue
            row.append(InlineKeyboardButton(t, callback_data=f"bsdt_{t}"))
            if len(row) == 4:
                buttons.append(row)
                row = []
        if row:
            buttons.append(row)

        if buttons:
            await context.bot.send_message(
                chat_id=update.effective_chat.id,
                text=(
                    "📊 Tambahkan Broker Summary untuk kandidat di atas.\n\n"
                    "Pilih ticker, lalu upload screenshot:\n"
                    "• Tab ALL\n"
                    "• Rentang 3 hari bursa\n"
                    "• Mode Net aktif\n\n"
                    "Prioritas upload: kandidat Fresh / Continuation terbaik."
                ),
                reply_markup=InlineKeyboardMarkup(buttons),
            )
    except Exception as e:
        print(f"⚠️ Gagal kirim tombol Broker Summary /screendaytrade: {e}")



STATUS_LABEL_ID = {
    "pending_entry": "⏳ Menunggu Entry",
    "pending_resolution": "🔄 Berjalan",
    "win": "✅ Win",
    "lose": "❌ Lose",
    "win_timebased": "✅ Win (time-based)",
    "lose_timebased": "❌ Lose (time-based)",
}


async def show_winrate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Scorecard uji winrate rekomendasi /screendaytrade — TP1/cut_loss ASLI dari
    rekomendasi hari itu (bukan angka tetap terpisah), entry = harga OPEN hari
    bursa setelah pick dikunci. Saham yang sama BISA muncul di beberapa tanggal
    — ini SENGAJA (tiap hari = sinyal/keputusan independen yang diuji terpisah,
    bukan "apakah saham X bagus secara umum").
    """
    history = load_daytrade_picks_history()
    if not history:
        await safe_reply(update.message, "📊 Belum ada data winrate — /screendaytrade perlu dijalankan dulu untuk mulai mengunci picks.")
        return

    history_sorted = sorted(history, key=lambda p: (p["pick_date"], p["ticker"]))

    today_str = get_current_trading_day_close_marker()
    lines = ["📊 WINRATE STOCKPICK — /screendaytrade\n", f"Today: {today_str}\n"]
    lines.append("Saham // Tanggal Pick // P/L // Status")
    for p in history_sorted:
        pnl_str = f"{p['pnl_pct']:+.1f}%" if p["pnl_pct"] is not None else "-"
        status_str = STATUS_LABEL_ID.get(p["status"], p["status"])
        lines.append(f"{p['ticker']} // {p['pick_date']} // {pnl_str} // {status_str}")

    lines.append("\nSummary Winrate")
    by_date = {}
    for p in history_sorted:
        by_date.setdefault(p["pick_date"], []).append(p)

    for pick_date, picks in sorted(by_date.items()):
        resolved = [p for p in picks if p["status"] in ("win", "lose", "win_timebased", "lose_timebased")]
        pending = [p for p in picks if p["status"] in ("pending_entry", "pending_resolution")]
        if not resolved and pending:
            lines.append(f"{pick_date}: {len(pending)} pick masih berjalan, belum ada hasil final")
            continue
        wins = [p for p in resolved if p["status"] in ("win", "win_timebased")]
        winrate_pct = (len(wins) / len(resolved) * 100) if resolved else 0
        total_gain = sum(p["pnl_pct"] for p in resolved if p["pnl_pct"] is not None)
        pending_note = f" ({len(pending)} masih berjalan)" if pending else ""
        lines.append(f"{pick_date}: {winrate_pct:.0f}% Win ({len(wins)}/{len(resolved)}); Total gain {total_gain:+.1f}%{pending_note}")

    # Ringkasan keseluruhan — bukti agregat apakah stockpick genuinely solid
    all_resolved = [p for p in history if p["status"] in ("win", "lose", "win_timebased", "lose_timebased")]
    if all_resolved:
        all_wins = [p for p in all_resolved if p["status"] in ("win", "win_timebased")]
        overall_winrate = len(all_wins) / len(all_resolved) * 100
        overall_gain = sum(p["pnl_pct"] for p in all_resolved if p["pnl_pct"] is not None)
        avg_gain = overall_gain / len(all_resolved)
        lines.append(f"\n📈 KESELURUHAN ({len(all_resolved)} pick selesai): {overall_winrate:.0f}% Win — "
                      f"rata-rata {avg_gain:+.2f}%/pick, total {overall_gain:+.1f}%")

    await safe_reply(update.message, "\n".join(lines))


async def test_morning_brief(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update.message, "🧪 Menjalankan Morning Brief manual, mohon tunggu...")
    await run_morning_brief(context)


async def test_opening_dynamics(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update.message, "🧪 Menjalankan Opening Dynamics manual, mohon tunggu...")
    await run_opening_dynamics(context)


# ==========================================
# 💼 PORTFOLIO COMMANDS
# ==========================================
async def buy_position(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await safe_reply(update.message, 
            "Cara pakai: /buy TICKER HARGA LOT\nContoh: /buy TLKM 2850 10"
        )
        return
    ticker = context.args[0].upper().strip()
    try:
        price = float(context.args[1])
        lots = int(context.args[2])
        if price <= 0 or lots <= 0:
            raise ValueError
    except ValueError:
        await safe_reply(update.message, "⚠️ Harga dan lot harus angka positif.")
        return

    success, error_message, position = add_position(ticker, price, lots)
    if not success:
        await safe_reply(update.message, error_message)
        return
    await safe_reply(update.message, 
        f"✅ Tercatat: {ticker}\n"
        f"Total: {position['lots']} lot @ avg Rp{position['avg_price']:,.0f}\n"
        f"Cash tersisa: Rp{get_cash_balance():,.0f}"
    )


async def sell_position(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 3:
        await safe_reply(update.message, 
            "Cara pakai: /sell TICKER LOT HARGA\nContoh: /sell TLKM 5 2850"
        )
        return
    ticker = context.args[0].upper().strip()
    try:
        lots = int(context.args[1])
        sell_price = float(context.args[2])
        if lots <= 0 or sell_price <= 0:
            raise ValueError
    except ValueError:
        await safe_reply(update.message, "⚠️ Lot dan harga harus angka positif.")
        return

    success, message = reduce_position(ticker, lots, sell_price)
    await safe_reply(update.message, ("✅ " if success else "⚠️ ") + message)


async def add_cash_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await safe_reply(update.message, "Cara pakai: /addcash JUMLAH\nContoh: /addcash 5000000")
        return
    try:
        amount = float(context.args[0])
        if amount <= 0:
            raise ValueError
    except ValueError:
        await safe_reply(update.message, "⚠️ Jumlah harus angka positif.")
        return
    new_balance = add_cash(amount)
    await safe_reply(update.message, f"✅ Cash ditambahkan: Rp{amount:,.0f}\nCash sekarang: Rp{new_balance:,.0f}")


async def withdraw_cash_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 1:
        await safe_reply(update.message, "Cara pakai: /withdrawcash JUMLAH\nContoh: /withdrawcash 1000000")
        return
    try:
        amount = float(context.args[0])
        if amount <= 0:
            raise ValueError
    except ValueError:
        await safe_reply(update.message, "⚠️ Jumlah harus angka positif.")
        return
    success, message, _ = withdraw_cash(amount)
    await safe_reply(update.message, ("✅ " if success else "⚠️ ") + message)


async def reset_portfolio_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Wipes ALL positions, cash, and realized P&L history back to empty — a safety
    net for a botched manual entry. Requires explicit confirmation since this is
    destructive and not reversible (no undo, no backup kept).
    """
    if not context.args or context.args[0].lower() != "confirm":
        portfolio = load_portfolio()
        num_positions = len(portfolio.get("positions", {}))
        cash = portfolio.get("cash", 0.0)
        await safe_reply(
            update.message,
            f"⚠️ Ini akan MENGHAPUS SEMUA data portofolio:\n"
            f"- {num_positions} posisi saham\n"
            f"- Cash: Rp{cash:,.0f}\n"
            f"- Seluruh riwayat realized P&L\n\n"
            f"Tindakan ini TIDAK BISA DIBATALKAN.\n\n"
            f"Jika yakin, ketik: /resetportfolio confirm"
        )
        return

    save_portfolio(copy.deepcopy(PORTFOLIO_SCHEMA_DEFAULT))
    await safe_reply(update.message, "✅ Portofolio telah direset total — posisi, cash, dan riwayat P&L kosong kembali.")


async def set_entry_date_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if len(context.args) < 2:
        await safe_reply(update.message, 
            "Cara pakai: /setentrydate TICKER YYYY-MM-DD\nContoh: /setentrydate TLKM 2026-07-08"
        )
        return
    ticker = context.args[0].upper().strip()
    date_str = context.args[1].strip()
    success, message = set_entry_date(ticker, date_str)
    await safe_reply(update.message, ("✅ " if success else "⚠️ ") + message)


async def watchlist_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /watchlist — view current watchlist
    /watchlist add TICKER — add (max 3)
    /watchlist remove TICKER — remove
    """
    portfolio = load_portfolio()
    watchlist = portfolio.get("watchlist", [])

    if not context.args:
        if not watchlist:
            await safe_reply(update.message, f"📋 Watchlist kosong (maks {WATCHLIST_MAX_SIZE} saham).\nTambah dengan: /watchlist add TICKER")
        else:
            await safe_reply(update.message, f"📋 Watchlist ({len(watchlist)}/{WATCHLIST_MAX_SIZE}): {', '.join(watchlist)}")
        return

    action = context.args[0].lower()
    if action == "add" and len(context.args) >= 2:
        ticker = context.args[1].upper().strip()
        success, message = add_to_watchlist(ticker)
        await safe_reply(update.message, ("✅ " if success else "⚠️ ") + message)
    elif action == "remove" and len(context.args) >= 2:
        ticker = context.args[1].upper().strip()
        success, message = remove_from_watchlist(ticker)
        await safe_reply(update.message, ("✅ " if success else "⚠️ ") + message)
    else:
        await safe_reply(update.message, 
            "Cara pakai:\n/watchlist — lihat watchlist\n/watchlist add TICKER — tambah\n/watchlist remove TICKER — hapus"
        )


MAX_BROKERSUM_PER_RUN = 5  # confirmed hard daily cap on Index Alpha's free tier, 1 call/ticker


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
        action_priority = classify_action_priority(scoring, lifecycle_category_for_priority)
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


async def my_portfolio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    portfolio = load_portfolio()
    positions = portfolio.get("positions", {})
    cash = portfolio.get("cash", 0.0)
    watchlist = portfolio.get("watchlist", [])
    include_brokersum = len(context.args) > 0 and context.args[0].lower() == "brokersum"
    use_zapi_brokersum = (
        include_brokersum and len(context.args) > 1 and context.args[1].lower() == "zapi"
    )
    explicit_brokersum_tickers = (
        {t.upper() for t in context.args[1:]}
        if include_brokersum and len(context.args) > 1 and not use_zapi_brokersum
        else None
    )

    if not positions and not watchlist:
        await safe_reply(update.message, 
            "Portofolio masih kosong. Tambahkan posisi dengan:\n/buy TICKER HARGA LOT\n"
            "Atau tambahkan watchlist dengan:\n/watchlist add TICKER\n\n"
            f"Cash tersedia: Rp{cash:,.0f}"
        )
        return

    total_tickers = len(positions) + len(watchlist)
    await safe_reply(update.message, f"🔎 Menganalisa {total_tickers} posisi/watchlist, mohon tunggu...")

    positions_data = []
    watchlist_data = []
    sector_value = {}
    total_stock_value = 0.0
    missing_entry_date_tickers = []

    # PASS 1: full technical scoring for everyone. Chunked dengan cooldown —
    # compute_factor_scoring di sini pakai include_quote_check=True (default),
    # artinya 2 request iTick per ticker (kline+quote), BUKAN 1 seperti bulk
    # scan. Chunk size dibagi setengah dari ITICK_CHUNK_SIZE supaya total
    # request per chunk (ticker x 2) tetap di bawah ~12 rolling-window limit
    # yang sudah dikonfirmasi — sebelumnya loop ini TIDAK PUNYA pacing sama
    # sekali, menyebabkan semua ticker setelah ~6 gagal rate-limit begitu
    # portofolio+watchlist melebihi itu (ditemukan lewat log nyata: 13 ticker,
    # semua gagal "code=None, msg=None" setelah beberapa yang pertama berhasil).
    PORTFOLIO_SCORING_CHUNK_SIZE = max(1, ITICK_CHUNK_SIZE // 2)
    position_items = list(positions.items())

    for chunk_start in range(0, len(position_items), PORTFOLIO_SCORING_CHUNK_SIZE):
        chunk = position_items[chunk_start:chunk_start + PORTFOLIO_SCORING_CHUNK_SIZE]
        for ticker, pos in chunk:
            try:
                scoring = await asyncio.wait_for(asyncio.to_thread(compute_factor_scoring, ticker), timeout=1800)
            except asyncio.TimeoutError:
                print(f"⏱️ Timed out fetching {ticker} for portfolio, skipping.")
                continue
            except Exception as e:
                print(f"Error scoring {ticker} for portfolio: {e}")
                continue
            if not scoring:
                continue

            current_price = scoring["price"]
            avg_price = pos["avg_price"]
            lots = pos["lots"]
            shares = lots * BOARD_LOT_SIZE
            market_value = current_price * shares
            unrealized_pnl_pct = ((current_price - avg_price) / avg_price) * 100
            unrealized_pnl_idr = (current_price - avg_price) * shares

            total_stock_value += market_value
            sector = scoring.get("sector", "N/A")
            sector_value[sector] = sector_value.get(sector, 0) + market_value

            if not pos.get("entry_date"):
                missing_entry_date_tickers.append(ticker)

            positions_data.append({
                **scoring,
                "avg_buy_price": avg_price,
                "lots": lots,
                "market_value_idr": int(market_value),
                "unrealized_pnl_pct": round(unrealized_pnl_pct, 2),
                "unrealized_pnl_idr": int(unrealized_pnl_idr),
                "_entry_date": pos.get("entry_date"),
            })
        is_last_chunk = (chunk_start + PORTFOLIO_SCORING_CHUNK_SIZE) >= len(position_items)
        if not is_last_chunk:
            print(f"⏳ /myportfolio: cooling down {ITICK_COOLDOWN_SECONDS}s sebelum chunk berikutnya "
                  f"({chunk_start + len(chunk)}/{len(position_items)} posisi selesai)...")
            await asyncio.sleep(ITICK_COOLDOWN_SECONDS)

    # Cooldown sebelum mulai watchlist — window rate-limit mungkin masih "panas"
    # dari chunk TERAKHIR loop positions di atas (yang sengaja tidak diberi
    # cooldown karena dikira sudah selesai), jadi perlu jeda dulu sebelum
    # menyambung ke request baru untuk watchlist.
    if position_items:
        print(f"⏳ /myportfolio: cooling down {ITICK_COOLDOWN_SECONDS}s sebelum mulai watchlist...")
        await asyncio.sleep(ITICK_COOLDOWN_SECONDS)

    for ticker in watchlist:
        try:
            scoring = await asyncio.wait_for(asyncio.to_thread(compute_factor_scoring, ticker), timeout=1800)
        except asyncio.TimeoutError:
            print(f"⏱️ Timed out fetching watchlist {ticker}, skipping.")
            continue
        except Exception as e:
            print(f"Error scoring watchlist {ticker}: {e}")
            continue
        if not scoring:
            continue
        watchlist_data.append(scoring)

    if not positions_data and not watchlist_data:
        await safe_reply(update.message, "⚠️ Gagal mengambil data. Coba lagi nanti.")
        return

    # PASS 2: brokersum — before lifecycle classification, since it can adjust
    # final_score, which lifecycle classification depends on.
    if include_brokersum:
        combined = positions_data + watchlist_data

        if use_zapi_brokersum:
            # Mode Zapi: budget jauh lebih longgar (100/menit, ~10/hari rata2 aman
            # dari 300/bulan) — cakup SEMUA holdings+watchlist, bukan top-5 seperti
            # Index Alpha. Tetap ada batas pengaman kalau portofolio berkembang
            # jauh melebihi kebutuhan saat ini, supaya tidak habiskan kuota bulanan
            # dalam sehari tanpa sadar.
            MAX_ZAPI_BROKERSUM_SAFE = 15
            if len(combined) > MAX_ZAPI_BROKERSUM_SAFE:
                ranked = sorted(combined, key=lambda s: compute_brokersum_priority(s, total_stock_value), reverse=True)
                selected = ranked[:MAX_ZAPI_BROKERSUM_SAFE]
                selection_note = (
                    f"⚠️ {len(combined)} ticker melebihi batas aman {MAX_ZAPI_BROKERSUM_SAFE} — "
                    f"auto-pilih prioritas tertinggi: {', '.join(s['ticker'] for s in selected)}"
                )
            else:
                selected = combined
                selection_note = f"SEMUA {len(selected)} posisi/watchlist tercover: {', '.join(s['ticker'] for s in selected)}"

            await safe_reply(update.message, f"📡 Brokersum via Zapi ({selection_note})...")

            for scoring in selected:
                ticker = scoring["ticker"]
                try:
                    brokersum = await asyncio.to_thread(
                        compute_brokersum_metrics_zapi, ticker, scoring.get("cmf"), scoring.get("obv_divergence")
                    )
                    if brokersum:
                        scoring["brokersum"] = brokersum
                        apply_brokersum_adjustment(scoring, brokersum)
                        # Simpan ke cache harian yang sama — supaya /check TICKER
                        # bisa pakai ulang gratis. CATATAN: cache per-ticker, bukan
                        # per-sumber — kalau hari yang sama juga pernah pakai
                        # Index Alpha untuk ticker yang sama, yang terakhir dipanggil
                        # akan menimpa. Bukan masalah besar, tapi perlu disadari.
                        cache = _load_brokersum_cache()
                        cache[ticker] = {"date": get_last_published_trading_day(), "data": brokersum}
                        _save_brokersum_cache(cache)
                except Exception as e:
                    print(f"⚠️ Zapi brokersum fetch failed for {ticker}: {e}")
                await asyncio.sleep(0.5)  # jeda ringan, sopan santun — 100/menit sangat longgar

        elif explicit_brokersum_tickers:
            selected = [s for s in combined if s["ticker"] in explicit_brokersum_tickers]
            selection_note = f"dipilih manual: {', '.join(s['ticker'] for s in selected)}"

            await safe_reply(update.message, f"📡 Brokersum ({selection_note}) — pakai kuota Index Alpha terbatas...")

            for scoring in selected:
                ticker = scoring["ticker"]
                try:
                    brokersum = await asyncio.to_thread(
                        get_cached_or_fetch_brokersum, ticker, scoring.get("cmf"), scoring.get("obv_divergence")
                    )
                    if brokersum:
                        scoring["brokersum"] = brokersum
                        apply_brokersum_adjustment(scoring, brokersum)
                except Exception as e:
                    print(f"⚠️ Brokersum fetch failed for {ticker}: {e}")
        else:
            ranked = sorted(combined, key=lambda s: compute_brokersum_priority(s, total_stock_value), reverse=True)
            selected = ranked[:MAX_BROKERSUM_PER_RUN]
            selection_note = f"auto-pilih {len(selected)} prioritas tertinggi: {', '.join(s['ticker'] for s in selected)}"

            await safe_reply(update.message, f"📡 Brokersum ({selection_note}) — pakai kuota Index Alpha terbatas...")

            for scoring in selected:
                ticker = scoring["ticker"]
                try:
                    brokersum = await asyncio.to_thread(
                        get_cached_or_fetch_brokersum, ticker, scoring.get("cmf"), scoring.get("obv_divergence")
                    )
                    if brokersum:
                        scoring["brokersum"] = brokersum
                        apply_brokersum_adjustment(scoring, brokersum)
                except Exception as e:
                    print(f"⚠️ Brokersum fetch failed for {ticker}: {e}")

    # PASS 3: lifecycle category + TP horizon — computed AFTER brokersum, so they
    # reflect the real, possibly-adjusted final score, not the pre-adjustment one.
    for scoring in positions_data:
        days_held = compute_trading_days_held(scoring.get("_entry_date"))
        scoring["_days_held"] = days_held
        scoring["_lifecycle"] = classify_lifecycle_category(days_held, scoring)
        scoring["_tp_horizon"] = estimate_tp_horizon(scoring)

    for scoring in watchlist_data:
        scoring["_tp_horizon"] = estimate_tp_horizon(scoring)

    # PASS 3b: live active breakout for held positions/watchlist during market hours.
    # This makes /myportfolio actionable for session-2 decisions: take profit, hold,
    # wait for breakout, or tighten stop if price loses VWAP/invalidation.
    if get_current_idx_session() is not None:
        for scoring in positions_data + watchlist_data:
            try:
                scoring["active_breakout"] = await asyncio.to_thread(
                    compute_active_breakout_score, scoring["ticker"], scoring, True
                )
            except Exception as e:
                scoring["active_breakout"] = {"available": False, "reason": str(e)[:120]}
            await asyncio.sleep(0.3)

    # Sector concentration
    total_value = total_stock_value + cash
    sector_concentration = {
        sector: round((value / total_stock_value) * 100, 1)
        for sector, value in sector_value.items()
    } if total_stock_value > 0 else {}
    concentrated_sectors = {s: pct for s, pct in sector_concentration.items() if pct >= 40}

    realized_log = portfolio.get("realized_pnl_log", [])
    total_realized = sum(entry["pnl_idr"] for entry in realized_log)

    portfolio_context = (
        f"Cash tersedia: Rp{cash:,.0f}\n"
        f"Total nilai saham: Rp{total_stock_value:,.0f}\n"
        f"Total kekayaan (saham + cash): Rp{total_value:,.0f}\n"
        f"Total realized P&L (semua waktu): Rp{total_realized:,.0f}\n"
        f"Konsentrasi sektor (% dari nilai saham): {sector_concentration}\n"
        + (f"⚠️ SEKTOR TERKONSENTRASI (>=40% dari portofolio saham): {concentrated_sectors}\n"
           if concentrated_sectors else "Tidak ada sektor yang terlalu terkonsentrasi (semua di bawah 40%).\n")
        + (f"\nWATCHLIST ({len(watchlist_data)} saham, belum dimiliki — evaluasi untuk timing entry, "
           f"bukan add/trim/hold/sell):\n{[w['ticker'] for w in watchlist_data]}\n" if watchlist_data else "")
    )

    combined_data = positions_data + watchlist_data
    reasoning_result = await asyncio.to_thread(get_portfolio_reasoning_and_synthesis, combined_data, portfolio_context)
    per_stock_reasoning = reasoning_result.get("per_stock_reasoning", {})
    weekly_synthesis = reasoning_result.get("weekly_synthesis", "")

    # Assemble the final message deterministically in Python — every number is
    # exactly what was computed, Gemini only ever fills in the reasoning text.
    header = f"📊 RINGKASAN PORTOFOLIO\nTotal Kekayaan: Rp{total_value:,.0f} (Saham Rp{total_stock_value:,.0f} + Cash Rp{cash:,.0f})"
    if concentrated_sectors:
        header += f"\n⚠️ Konsentrasi Sektor: {', '.join(f'{s} {p}%' for s, p in concentrated_sectors.items())}"
    if missing_entry_date_tickers:
        header += (
            f"\n\n⚠️ {len(missing_entry_date_tickers)} posisi belum punya tanggal beli: {', '.join(missing_entry_date_tickers)}\n"
            f"Lengkapi dengan: /setentrydate TICKER YYYY-MM-DD\n"
            f"Kategori siklus untuk posisi ini disembunyikan sampai tanggal diisi."
        )

    blocks = [header]
    for scoring in positions_data:
        weight_pct = (scoring["market_value_idr"] / total_stock_value * 100) if total_stock_value > 0 else 0
        block = format_position_block(
            scoring, is_holding=True, weight_pct=weight_pct,
            unrealized_pnl_pct=scoring["unrealized_pnl_pct"], days_held=scoring.get("_days_held"),
            reasoning_text=per_stock_reasoning.get(scoring["ticker"], ""),
        )
        blocks.append(block)

    if watchlist_data:
        blocks.append("─────────────────────────────────────\nWATCHLIST")
        for scoring in watchlist_data:
            reasoning = per_stock_reasoning.get(scoring["ticker"], "")
            th = scoring.get("_tp_horizon", {})
            horizon_str = f" ({th['horizon_days_low']}-{th['horizon_days_high']} hari, Confidence: {th['confidence']})" if th.get("horizon_days_low") else ""
            blocks.append(f"{scoring['ticker']} — {scoring.get('action_label_id', '?')}{horizon_str}\n{reasoning}")

    blocks.append(f"─────────────────────────────────────\nSINTESIS MINGGUAN:\n{weekly_synthesis}")

    full_message = "\n\n".join(blocks)
    await safe_reply(update.message, full_message)


async def order_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /order buy TICKER LOT HARGA — catat order beli yang DIPANTAU (bukan posisi
    yang sudah terisi — untuk itu pakai /buy). /order sell TICKER LOT HARGA —
    sama untuk order jual. /order (tanpa argumen) — lihat semua order yang
    dipantau. /order clear TICKER — hapus dari pemantauan (setelah dicek
    manual di app broker bahwa order sudah match/dibatalkan).
    """
    if not context.args:
        orders = load_pending_orders()
        if not orders:
            await safe_reply(update.message, "📋 Tidak ada order yang dipantau.\nTambah dengan: /order buy TICKER LOT HARGA")
            return
        lines = ["📋 ORDER YANG DIPANTAU\n"]
        for o in orders:
            side_label = "BELI" if o["side"] == "buy" else "JUAL"
            lines.append(f"{o['ticker']} {side_label} {o['lot']} lot @ {o['price']} (ditambahkan {o['date_added']})")
        await safe_reply(update.message, "\n".join(lines))
        return

    subcommand = context.args[0].lower()

    if subcommand == "clear":
        if len(context.args) < 2:
            await safe_reply(update.message, "Cara pakai: /order clear TICKER")
            return
        ticker = context.args[1].upper()
        orders = load_pending_orders()
        before = len(orders)
        orders = [o for o in orders if o["ticker"] != ticker]
        save_pending_orders(orders)
        removed = before - len(orders)
        await safe_reply(update.message, f"✅ {removed} order {ticker} dihapus dari pemantauan." if removed else f"Tidak ada order {ticker} yang dipantau.")
        return

    if subcommand not in ("buy", "sell"):
        await safe_reply(update.message, "Cara pakai:\n/order buy TICKER LOT HARGA\n/order sell TICKER LOT HARGA\n/order clear TICKER\n/order (lihat semua)")
        return

    if len(context.args) < 4:
        await safe_reply(update.message, f"Cara pakai: /order {subcommand} TICKER LOT HARGA\nContoh: /order {subcommand} ANTM 10 3050")
        return

    ticker = context.args[1].upper()
    try:
        lot = int(context.args[2])
        price = float(context.args[3])
    except ValueError:
        await safe_reply(update.message, "Lot dan harga harus berupa angka.")
        return

    orders = load_pending_orders()
    orders.append({
        "ticker": ticker, "side": subcommand, "lot": lot, "price": price,
        "date_added": get_current_trading_day_close_marker(),
    })
    save_pending_orders(orders)
    side_label = "BELI" if subcommand == "buy" else "JUAL"
    await safe_reply(update.message, f"✅ Order {side_label} {ticker} {lot} lot @ {price} ditambahkan ke pemantauan.")


async def portfolio_summary(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Fast, lightweight summary — live price only (no full 500-bar scoring, no Gemini
    call), just current value + P&L. Meant for quick check-ins, not analysis.
    """
    portfolio = load_portfolio()
    positions = portfolio.get("positions", {})
    cash = portfolio.get("cash", 0.0)

    if not positions:
        await safe_reply(update.message, f"Portofolio masih kosong.\nCash tersedia: Rp{cash:,.0f}")
        return

    await safe_reply(update.message, "🔎 Mengambil harga terkini...")

    total_stock_value = 0.0
    total_cost_basis = 0.0
    lines = []

    for ticker, pos in positions.items():
        try:
            quote = await asyncio.to_thread(itick_get_quote, ticker)
        except Exception as e:
            print(f"Error fetching quote for {ticker} summary: {e}")
            quote = None

        avg_price = pos["avg_price"]
        lots = pos["lots"]
        shares = lots * BOARD_LOT_SIZE
        cost_basis = avg_price * shares
        total_cost_basis += cost_basis

        if quote and quote.get("ld"):
            current_price = quote["ld"]
            market_value = current_price * shares
            pnl_pct = ((current_price - avg_price) / avg_price) * 100
            total_stock_value += market_value
            lines.append(f"{ticker}: {lots} lot, Rp{market_value:,.0f} ({pnl_pct:+.1f}%)")
        else:
            # Fall back to cost basis if live quote fails, clearly marked as such
            total_stock_value += cost_basis
            lines.append(f"{ticker}: {lots} lot, Rp{cost_basis:,.0f} (harga live gagal, pakai avg cost)")

    total_unrealized_pnl = total_stock_value - total_cost_basis
    total_unrealized_pct = (total_unrealized_pnl / total_cost_basis * 100) if total_cost_basis > 0 else 0
    total_wealth = total_stock_value + cash

    realized_log = portfolio.get("realized_pnl_log", [])
    total_realized = sum(entry["pnl_idr"] for entry in realized_log)

    position_lines = "\n".join(lines)
    watchlist = portfolio.get("watchlist", [])
    all_tickers_for_buttons = list(positions.keys()) + [t for t in watchlist if t not in positions]

    keyboard = build_ticker_shortcut_keyboard(all_tickers_for_buttons) if all_tickers_for_buttons else None
    await safe_reply(
        update.message,
        f"📊 RINGKASAN PORTOFOLIO\n\n"
        f"{position_lines}\n\n"
        f"Total Saham: Rp{total_stock_value:,.0f}\n"
        f"Cash: Rp{cash:,.0f}\n"
        f"─────────────────\n"
        f"Total Kekayaan: Rp{total_wealth:,.0f}\n\n"
        f"Unrealized P&L: Rp{total_unrealized_pnl:,.0f} ({total_unrealized_pct:+.1f}%)\n"
        f"Realized P&L (semua waktu): Rp{total_realized:,.0f}"
    )
    if keyboard:
        await update.message.reply_text("🔍 Cek cepat:", reply_markup=keyboard)

    # Segmen order tracking — pelengkap kedua di /summary.
    pending_orders = load_pending_orders()
    if not pending_orders:
        await update.message.reply_text(
            "Ada order untuk saham Anda?\n"
            "Tambahkan dengan /order buy TICKER LOT HARGA atau /order sell TICKER LOT HARGA"
        )
    else:
        order_lines = ["📋 ORDER TRACKING (perkiraan, cek app broker untuk pastikan)\n"]
        order_buttons = []
        for o in pending_orders:
            try:
                quote = await asyncio.to_thread(itick_get_quote, o["ticker"])
            except Exception:
                quote = None
            status = check_order_touch_status(o, quote)
            side_label = "BELI" if o["side"] == "buy" else "JUAL"
            if status["touched"] is True:
                status_str = f"🟡 Kemungkinan sudah match — {status['note']}"
            elif status["touched"] is False:
                status_str = f"⚪ Belum tersentuh — {status['note']}"
            else:
                status_str = f"❔ {status['note']}"
            order_lines.append(f"{o['ticker']} {side_label} {o['lot']} lot @ {o['price']}\n{status_str}")
            order_buttons.append(InlineKeyboardButton(f"✅ Clear {o['ticker']}", callback_data=f"orderclear_{o['ticker']}"))

        order_keyboard = InlineKeyboardMarkup([order_buttons[i:i + 2] for i in range(0, len(order_buttons), 2)])
        await update.message.reply_text("\n\n".join(order_lines), reply_markup=order_keyboard)




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


# ==========================================
# 🕤 OPENING DYNAMICS (09:45 WIB, weekdays)
# ==========================================
async def run_nightly_full_scan(context: ContextTypes.DEFAULT_TYPE):
    """
    Scheduled job jam 22:00 WIB — refresh DB dari Yahoo Finance untuk universe
    ISSI liquid, lalu scan penuh (compute_factor_scoring) untuk universe yang
    sama. Hasil disimpan ke daily_scan_cache dan dipakai bersama oleh
    run_morning_brief() dan screen_daytrade().
    """
    try:
        if await asyncio.to_thread(is_idx_market_holiday_today):
            print("📅 Skipping nightly full scan — IDX market holiday today (tidak ada data EOD baru).")
            return

        issi_tickers = await asyncio.to_thread(load_or_build_issi_liquid_whitelist)
        print(f"🌙 Nightly full scan dimulai: {len(issi_tickers)} ticker ISSI liquid...")

        db_stats_payload = await asyncio.to_thread(populate_from_yfinance, issi_tickers, "10d", 50)
        db_stats = get_db_stats()
        latest_marker = db_stats.get("last_ohlcv_update_marker") or db_stats_payload.get("latest_marker") or "-"
        try:
            await context.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=(
                    f"✅ DB update sukses\n"
                    f"Universe: ISSI liquid\n"
                    f"Ticker: {db_stats_payload.get('tickers', len(issi_tickers))}\n"
                    f"Updated s/d: {latest_marker}\n"
                    f"Rows written: {db_stats_payload.get('rows_written', 0):,}"
                ),
            )
        except Exception as notify_error:
            print(f"⚠️ Gagal kirim notifikasi DB update malam: {notify_error}")

        results, skip_reasons = await asyncio.wait_for(
            asyncio.to_thread(fetch_all_tickers_scored, issi_tickers), timeout=1800
        )
        save_daily_scan_cache(results)
        update_scan_metadata(len(results), len(skip_reasons), latest_marker, universe_name="ISSI liquid")
        print(f"🌙 Nightly full scan selesai: {len(results)} berhasil, {len(skip_reasons)} gagal/dikecualikan.")

        top_ticker = results[0]["ticker"] if results else "-"
        top_score = results[0].get("scores", {}).get("final") if results else None
        try:
            await context.bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=(
                    f"✅ Night scan selesai\n"
                    f"Universe: ISSI liquid\n"
                    f"Scored: {len(results)}\n"
                    f"Skipped: {len(skip_reasons)}\n"
                    f"Top: {top_ticker} ({top_score})\n"
                    f"Cache updated s/d: {latest_marker}"
                ),
            )
        except Exception as notify_error:
            print(f"⚠️ Gagal kirim notifikasi night scan: {notify_error}")

        # Resolusi picks winrate — dijalankan SETELAH scan malam, supaya data EOD
        # yang dipakai untuk cek TP/SL sudah mencakup hari ini.
        try:
            resolved_count = await asyncio.to_thread(resolve_daytrade_picks)
            print(f"🎯 Resolusi winrate: {resolved_count} pick diperbarui.")
        except Exception as e:
            print(f"⚠️ Resolusi daytrade picks gagal: {e}")
    except asyncio.TimeoutError:
        print("⏱️ Nightly full scan melebihi batas waktu 50 menit — cache TIDAK diperbarui malam ini.")
    except Exception as e:
        print(f"❌ Nightly full scan gagal: {e}")


async def run_opening_dynamics(context: ContextTypes.DEFAULT_TYPE):
    try:
        if await asyncio.to_thread(is_idx_market_holiday_today):
            print("📅 Skipping Opening Dynamics — IDX market holiday today.")
            return

        portfolio = load_portfolio()
        held_tickers = set(portfolio.get("positions", {}).keys())
        sharia_universe = fetch_online_sharia_list()

        # Macro backdrop shown in the first 1-2 sentences.
        macro_context = await asyncio.to_thread(fetch_macro_context)
        extra_context_str = None
        if macro_context:
            macro_lines = "\n".join(f"- {k}: {v:+.2f}%" for k, v in macro_context.items())
            extra_context_str = f"Macro indices (% change, most recent session):\n{macro_lines}"

        # Use nightly scan cache to enrich opening dynamics with existing action/score/TP/SL.
        # If cache is empty/stale, the report still works from intraday dynamics alone.
        scored_cache = load_daily_scan_cache()

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
        await app.bot.send_message(chat_id=TELEGRAM_CHAT_ID, text="🟢 Bot baru saja aktif.\n\n" + STARTUP_DISCLAIMER)
    except Exception as e:
        print(f"⚠️ Failed to send startup notice: {e}")



async def brokersum_upload_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Manual Broker Summary upload:
    /brokersum TICKER
    Lalu kirim screenshot Broker Summary tab ALL, periode 3 hari, Net aktif.
    """
    if not context.args:
        await safe_reply(
            update.message,
            "Cara pakai: /brokersum TICKER\n"
            "Contoh: /brokersum BULL\n\n"
            "Lalu kirim screenshot Broker Summary:\n"
            "• Tab: ALL\n"
            "• Periode: 3 hari bursa\n"
            "• Mode: Net aktif\n"
            "• Pastikan kode broker, volume/lot, average, dan total terlihat."
        )
        return

    ticker = context.args[0].upper().strip()
    chat_id = update.effective_chat.id

    PENDING_BROKERSUM_CHECKS[chat_id] = {
        "ticker": ticker,
        "expires_at": datetime.datetime.now(WIB) + datetime.timedelta(minutes=PENDING_BROKERSUM_TIMEOUT_MINUTES),
        "source": "manual_all_3d",
    }

    await safe_reply(
        update.message,
        f"📸 Silakan kirim screenshot Broker Summary {ticker}.\n\n"
        "Format wajib:\n"
        "• Tab: ALL\n"
        "• Periode: 3 hari bursa\n"
        "• Mode: Net aktif\n"
        "• Kode broker Buy/Sell, volume/lot, average, dan total harus terlihat.\n\n"
        "Bot akan membaca ini sebagai konfirmasi bandarmology/smart money, bukan foreign-only."
    )


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

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("version", show_version))
    app.add_handler(CommandHandler("whitelist", show_whitelist_status))
    app.add_handler(CommandHandler(["glossary", "istilah"], show_glossary))
    app.add_handler(CommandHandler("rebuildwhitelist", rebuild_whitelist_command))
    app.add_handler(CommandHandler(["check", "cek"], check_stock))
    app.add_handler(CallbackQueryHandler(skip_brokersum_callback, pattern="^skip_brokersum$"))
    app.add_handler(CallbackQueryHandler(quick_check_callback, pattern="^qchk_"))
    app.add_handler(CallbackQueryHandler(order_clear_callback, pattern="^orderclear_"))
    app.add_handler(CallbackQueryHandler(select_screendaytrade_brokersum, pattern="^bsdt_"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_brokersum_photo))
    app.add_handler(CommandHandler("buy", buy_position))
    app.add_handler(CommandHandler("sell", sell_position))
    app.add_handler(CommandHandler("addcash", add_cash_command))
    app.add_handler(CommandHandler("withdrawcash", withdraw_cash_command))
    app.add_handler(CommandHandler("resetportfolio", reset_portfolio_command))
    app.add_handler(CommandHandler("watchlist", watchlist_command))
    app.add_handler(CommandHandler("setentrydate", set_entry_date_command))
    app.add_handler(CommandHandler("summary", portfolio_summary))
    app.add_handler(CommandHandler("order", order_command))
    app.add_handler(CommandHandler(["myportfolio", "portofolio"], my_portfolio))
    app.add_handler(CommandHandler("testbrief", test_morning_brief))
    app.add_handler(CommandHandler("screendaytrade", screen_daytrade))
    app.add_handler(CommandHandler("brokersum", brokersum_upload_command))
    app.add_handler(CommandHandler("executiongate", executiongate_command))
    app.add_handler(CommandHandler("winrate", show_winrate))

    async def db_stats_command(update, context):
        stats = get_db_stats()
        if not stats:
            await update.message.reply_text("📦 DB belum ada atau kosong. Jalankan /populatedb dulu.")
            return
        await update.message.reply_text(
            f"📦 OHLCV Database\n"
            f"Daily: {stats['daily_tickers']} ticker, {stats['daily_rows']:,} bar\n"
            f"4H: {stats['h4_tickers']} ticker\n"
            f"Ukuran: {stats['size_mb']} MB\n\n"
            f"Last update: {stats.get('last_ohlcv_update_at') or '-'}\n"
            f"Updated s/d: {stats.get('last_ohlcv_update_marker') or '-'}\n"
            f"Night scan: {stats.get('last_nightly_scan_at') or '-'}\n"
            f"Night marker: {stats.get('last_nightly_scan_marker') or '-'}"
        )

    async def populate_db_command(update, context):
        """
        Populate DB dari yfinance — ISSI saja, deduplicate.
        ISSI bisa ratusan ticker, proses bisa 10-20 menit — bot tetap bisa digunakan
        selama proses berlangsung karena dijalankan di background thread.
        """
        await update.message.reply_text(
            "📥 Memulai populate DB dari yfinance (ISSI, 2 tahun histori)...\n"
            "Proses bisa 10-20 menit. Bot tetap bisa digunakan selama ini."
        )
        try:
            issi = set(fetch_online_sharia_list(index_key="ISSI"))
            all_tickers = list(issi)
            await update.message.reply_text(
                f"📋 Total: {len(all_tickers)} ticker ISSI unik"
            )
            stats_payload = await asyncio.to_thread(populate_from_yfinance, all_tickers, "2y", 50)
            stats = get_db_stats()
            msg = (
                f"✅ DB berhasil dipopulate!\n"
                f"{stats.get('daily_tickers', 0)} ticker, {stats.get('daily_rows', 0):,} bar, "
                f"{stats.get('size_mb', 0)} MB\n"
                f"Updated s/d: {stats.get('last_ohlcv_update_marker') or stats_payload.get('latest_marker') or '-'}"
            )
            await update.message.reply_text(msg)
            try:
                await context.bot.send_message(
                    chat_id=TELEGRAM_CHAT_ID,
                    text=(
                        f"✅ DB update sukses\n"
                        f"Universe: ISSI\n"
                        f"Ticker: {stats_payload.get('tickers', len(all_tickers))}\n"
                        f"Updated s/d: {stats_payload.get('latest_marker') or stats.get('last_ohlcv_update_marker') or '-'}\n"
                        f"Rows written: {stats_payload.get('rows_written', 0):,}"
                    ),
                )
            except Exception as notify_error:
                print(f"⚠️ Gagal kirim notifikasi DB update: {notify_error}")
        except Exception as e:
            await update.message.reply_text(f"⚠️ Gagal populate DB: {e}")

    app.add_handler(CommandHandler("dbstats", db_stats_command))
    app.add_handler(CommandHandler("dbstatus", db_stats_command))
    app.add_handler(CommandHandler("populatedb", populate_db_command))
    app.add_handler(CommandHandler("testopening", test_opening_dynamics))
    app.add_handler(CommandHandler("testopening", test_opening_dynamics))
    app.add_error_handler(global_error_handler)

    # No automatic scheduling — hosted on a request-driven webhook (PythonAnywhere
    # free web app) with no persistent background process to run a JobQueue.
    # Morning brief / nightly scan / opening dynamics are triggered manually via
    # /testbrief, /screendaytrade, /testopening instead.

    return app


if __name__ == "__main__":
    import sys

    # ── CLI mode: python bot_db.py --populatedb ──────────────────
    if "--populatedb" in sys.argv:
        print("📥 Mode: populate DB dari command line (tanpa Telegram)")
        init_ohlcv_db()
        issi = set(fetch_online_sharia_list(index_key="ISSI"))
        tickers = list(issi)
        print(f"📋 {len(tickers)} ticker ISSI akan di-populate...")
        stats = populate_from_yfinance(tickers, period="2y", batch_size=50)
        db = get_db_stats()
        print(f"\n✅ Selesai!")
        print(f"   Ticker   : {db.get('daily_tickers', 0)}")
        print(f"   Total bar: {db.get('daily_rows', 0):,}")
        print(f"   Ukuran DB: {db.get('size_mb', 0)} MB")
        print(f"   Updated   : {stats.get('latest_marker', '-')}")
        sys.exit(0)

    # ── CLI mode: python bot_db.py --dbstats ─────────────────────
    if "--dbstats" in sys.argv:
        init_ohlcv_db()
        db = get_db_stats()
        print(f"📦 OHLCV Database")
        print(f"   Daily : {db.get('daily_tickers', 0)} ticker, {db.get('daily_rows', 0):,} bar")
        print(f"   4H    : {db.get('h4_tickers', 0)} ticker")
        print(f"   Size  : {db.get('size_mb', 0)} MB")
        sys.exit(0)

    # ── Normal mode: jalankan bot Telegram ───────────────────────
    print("📡 Bot starting: listening for commands + daily 04:00 WIB brief scheduled...")

    # Startup itself can fail if network isn't ready yet (common right after
    # launching the app on mobile, before WiFi/data has fully connected). Retry
    # the whole startup a few times with a short pause instead of aborting.
    STARTUP_RETRIES = 5
    for attempt in range(1, STARTUP_RETRIES + 1):
        try:
            application = build_app()
            application.run_polling(
                allowed_updates=Update.ALL_TYPES,
                bootstrap_retries=5,  # PTB's own internal retry for the connect step
            )
            break  # run_polling only returns on clean shutdown
        except Exception as e:
            print(f"⚠️ Bot startup failed (attempt {attempt}/{STARTUP_RETRIES}): {e}")
            if attempt < STARTUP_RETRIES:
                print("Retrying in 15 seconds — check your network connection...")
                time.sleep(15)
            else:
                print("❌ Giving up after repeated startup failures. Check your connection and restart manually.")
                raise
