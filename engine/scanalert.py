"""
MBSS v2 (user request) — push alert intraday "breaking" utk scalping, 2 tahap:

  Alert A ("worth watching", gratis): price_spike>=4% (3 bar) DAN
    volume_ratio>=5x baseline (3 bar) — first-touch hari itu, informational,
    BUKAN sinyal beli. Threshold divalidasi backtest 1m riil (19 hari bursa,
    344 ticker riset, konsisten di universe produksi 60-600).

  Alert B ("entry signal", MENGGANTIKAN peran konfirmasi Alert A): dari
    threshold TERTINGGI yg pernah tersentuh hari itu (>3%, jadi basisnya
    +4/+5/+6%), begitu harga pullback >=4% dari peak lalu REBOUND balik ke
    >=-1% dari peak dlm 15 menit -- itu yg jadi sinyal entry sebenarnya.
    Backtest: conv->+10% naik monoton dari pullback dangkal ke dalam (37%
    di -1.5% sampai 49% di -6%), depth -4% dipilih user sbg titik kerja
    (bukan yg mentah-mentah tertinggi, supaya event tidak terlalu jarang).
    Zapi Stage 2 (orderbook, `broker.check_orderbook_solid_buy_zapi`) HANYA
    dicek kalau Alert B fire -- bukan di Alert A.

  Exclude-live: ticker yg gain-nya SUDAH >=15% DARI OPEN HARI INI (BUKAN dari
    prev_close) di cek PERTAMA hari itu di-skip total hari itu ("no room to
    entry"). MBSS v2 (user correction 2026-08-24, live case FIRE: gap-up
    +13.4% di PEMBUKAAN lalu cuma chop ±8% intraday -- excluded to
    prev_close-based, TAPI intraday room-nya sendiri belum genuinely
    exhausted): basis diganti ke gain-from-OPEN supaya gap pra-market/auction
    (08:58-09:00) tidak ikut kehitung sbg "exhaust" -- itu bukan tekanan beli
    INTRADAY yg genuinely sudah habis. Divalidasi ulang thd basis baru ini
    (1m riil, 344 ticker, 19 hari bursa): time-to-(-5%-drawdown) within 2
    menit turun dari 56.4% (basis prev_close) ke 45.8% (basis open) --
    sinyal lebih bersih, bukan cuma teori. Median further-gain tetap modest
    (median further gain saat touch 15%: +2.8%) dan risiko downside tetap
    dominan (86% kasus akhirnya kena -5% drawdown) -- exclude tetap masuk
    akal, cuma basisnya yg diperbaiki.

  Tag ret_3d>=10% ("sudah lari kencang"): relative-risk EOD-negatif 2.2-3.3x
    lebih tinggi, konsisten di semua threshold — ditempel sbg warning label
    di pesan, bukan exclude (variance-nya dua arah, bukan cuma risiko).

Dipanggil sbg one-shot CLI (`python bot.py --scanalert`), ikut pola
`--eodscan` yg sudah ada — BUKAN in-process scheduler. Dimaksudkan di-invoke
tiap 5 menit oleh cron eksternal (Termux crontab) 09:00-15:55 WIB hari
bursa. Tiap scan FETCH ULANG seluruh bar 1m hari ini (bukan incremental) dan
re-derive semua deteksi dari nol — state file HANYA menyimpan flag
"sudah dikirim/di-exclude", bukan progress harga inkremental. Ini sengaja:
lebih sederhana & self-correcting (tidak ada state drift antar-proses)
dibanding melacak breach/rebound secara bertahap lintas invocation terpisah.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import os

import pandas as pd
import yfinance as yf

from engine import legacy_core as core
from engine import broker as broker_engine

STATE_FILE = os.path.join(core.PROJECT_ROOT, "scanalert_state.json")

PRICE_FLOOR = 60
PRICE_CEILING = 600
NO_ROOM_GAIN_PCT = 15.0
RET_3D_WARN_THRESHOLD = 10.0

ALERT_A_SPIKE_PCT = 4.0
ALERT_A_VOLUME_RATIO = 5.0
ALERT_A_LOOKBACK_BARS = 3
ALERT_A_BASELINE_BARS = 3

ALERT_B_MIN_PEAK_THRESHOLD = 4       # basis Alert B: threshold >3% (mulai dari +4%)
ALERT_B_THRESHOLD_TIERS = (4, 5, 6)
ALERT_B_PULLBACK_DEPTH_PCT = -4.0    # dari peak (harga tertinggi hari itu SETELAH tier ini tersentuh)
ALERT_B_REBOUND_TARGET_PCT = -1.0    # rebound ke >=-1% dari peak dianggap "solid"
ALERT_B_REBOUND_WINDOW_MINUTES = 15

MIN_VWAP_BARS = 3  # dibawah ini VWAP nyaris = typical price bar itu sendiri, belum representatif (temuan riset session ini)
SESSION2_START_TIME = datetime.time(12, 0)  # cutoff sederhana S1/S2 -- VWAP HARUS reset tiap sesi, tidak nyambung lewat jeda istirahat

SCAN_WINDOW_START = datetime.time(9, 0)
SCAN_WINDOW_END = datetime.time(15, 55)


# ── State & toggle persistence ────────────────────────────────────────────

def _load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(state: dict):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def _today_str() -> str:
    return datetime.datetime.now(core.WIB).strftime("%Y-%m-%d")


def is_scan_alert_enabled() -> bool:
    return core.get_db_metadata("scanalert_enabled", "1") == "1"


def set_scan_alert_enabled(enabled: bool):
    core.set_db_metadata("scanalert_enabled", "1" if enabled else "0")


def _ensure_daily_reset(state: dict) -> dict:
    """
    Reset penuh begitu ganti hari bursa (ticker progress DAN toggle balik ON)
    -- match konvensi trading_day_marker yg sudah dipakai di seluruh
    codebase (mis. RapidAPI same-day dedup): jangan pernah anggap flag hari
    kemarin masih berlaku hari ini.
    """
    today = _today_str()
    if state.get("trading_day_marker") != today:
        print(f"🔄 Scan-alert: hari bursa baru ({today}) -- reset state ticker & toggle balik ON.")
        set_scan_alert_enabled(True)
        return {"trading_day_marker": today, "tickers": {}, "daily_ref": None}
    return state


# ── Universe & data fetch ──────────────────────────────────────────────────

def _get_alert_universe() -> list[str]:
    """
    Universe = whitelist bulanan yg SUDAH ADA (ticker_whitelist.json, dipakai
    /eodscan & command lain lewat core.load_or_build_whitelist) dipotong ke
    harga 60-600 -- BUKAN daftar riset terpisah. Filter harga pakai
    daily_ref (closing kemarin) yg sudah di-cache per hari, bukan fetch live
    terpisah lagi.

    MBSS v2 (user request 2026-08-24 — live case: saham non-Sharia muncul
    di ALERT): ticker_whitelist.json BISA mengandung kontaminasi lama (ticker
    di luar ISSI) -- ditemukan 72/386 saham di file dev ini bukan konstituen
    ISSI (mis. ASII, BRPT, LPKR, ADHI, PWON, FREN), karena load_or_build_
    whitelist() dulu HANYA memfilter ulang IN-MEMORY tiap dipanggil, tidak
    pernah menulis ulang FILE-nya (sudah diperbaiki, self-heal di sana) --
    tapi fungsi ini baca FILE LANGSUNG (bukan lewat load_or_build_whitelist),
    jadi tidak boleh menganggap file selalu bersih. Intersect eksplisit thd
    fetch_online_sharia_list() (baca lokal, murah, aman dipanggil tiap scan
    5 menit) sebagai lapis pertahanan kedua -- jangan bergantung pada file
    sudah ter-self-heal duluan.
    """
    if not os.path.exists(core.WHITELIST_CACHE_FILE):
        print("⚠️ ticker_whitelist.json belum ada -- jalankan /eodscan dulu sebelum scan-alert bisa jalan.")
        return []
    with open(core.WHITELIST_CACHE_FILE) as f:
        wl = json.load(f)
    current_month = datetime.datetime.now(core.WIB).strftime("%Y-%m")
    generated_month = wl.get("generated_month")
    if generated_month != current_month:
        # Tampilkan TETAP (bukan tolak total) -- konsisten dgn konvensi
        # "show stale data with a note" di seluruh codebase -- tapi user
        # perlu tahu ini bukan whitelist bulan ini.
        print(f"⚠️ ticker_whitelist.json basi (dari {generated_month}, sekarang {current_month}) -- "
              f"jalankan /eodscan untuk refresh. Tetap dipakai apa adanya utk scan ini.")
    eligible = wl.get("eligible_tickers", [])
    try:
        sharia_universe = set(core.fetch_online_sharia_list())
        filtered = [t for t in eligible if t in sharia_universe]
        dropped = len(eligible) - len(filtered)
        if dropped > 0:
            print(f"🕌 Scan-alert: {dropped} ticker non-Sharia dibuang dari universe (di luar ISSI terkunci).")
        return filtered
    except Exception as e:
        print(f"⚠️ Gagal memuat daftar Sharia terkunci ({e}) -- pakai whitelist apa adanya, TIDAK di-intersect.")
        return eligible


def _fetch_daily_ref(tickers: list[str]) -> dict:
    """
    Fetch closing kemarin (prev_close, dasar semua gain% + filter 60-600)
    dan ret_3d (3 hari bursa sebelum kemarin, utk tag "sudah lari kencang").
    Dipanggil SEKALI per hari (di-cache ke state), bukan tiap scan 5 menit.
    """
    symbols = [t + ".JK" for t in tickers]
    data = yf.download(symbols, period="10d", interval="1d", group_by="ticker", threads=True, progress=False)
    ref = {}
    for t in tickers:
        sym = t + ".JK"
        try:
            d = data[sym].dropna(how="all")
        except Exception:
            continue
        closes = d["Close"].astype(float).dropna()
        if len(closes) < 2:
            continue
        prev_close = float(closes.iloc[-1])
        if not (PRICE_FLOOR <= prev_close <= PRICE_CEILING):
            continue
        ret_3d = None
        if len(closes) >= 4:
            ret_3d = float((closes.iloc[-1] - closes.iloc[-4]) / closes.iloc[-4] * 100)
        ref[t] = {"prev_close": prev_close, "ret_3d": ret_3d}
    return ref


def _fetch_today_1m(tickers: list[str]):
    symbols = [t + ".JK" for t in tickers]
    return yf.download(symbols, period="1d", interval="1m", group_by="ticker", threads=True, progress=False)


def _compute_current_session_vwap(bars: pd.DataFrame) -> float | None:
    """
    VWAP LIVE (as of bar TERAKHIR yg tersedia) -- dipakai user utk menilai
    "harga sekarang aman entry atau tidak" begitu alert diterima, BUKAN vwap
    di titik historis deteksi terjadi. Reset per sesi (S1 vs S2, cutoff
    12:00) -- VWAP tidak boleh nyambung lewat jeda istirahat. Return None
    kalau bar sesi-berjalan masih <MIN_VWAP_BARS (di bar-bar pertama sesi,
    VWAP nyaris = typical price bar itu sendiri, bukan rata-rata yg berarti
    -- match temuan user sendiri soal ini di riset backtest sebelumnya).
    """
    if bars.empty:
        return None
    now_ts = bars.index[-1]
    is_s2 = now_ts.time() >= SESSION2_START_TIME
    session_mask = (bars.index.time >= SESSION2_START_TIME) if is_s2 else (bars.index.time < SESSION2_START_TIME)
    session_bars = bars[session_mask]
    if len(session_bars) < MIN_VWAP_BARS:
        return None
    highs = session_bars["High"].astype(float)
    lows = session_bars["Low"].astype(float)
    closes = session_bars["Close"].astype(float)
    vols = session_bars["Volume"].fillna(0).astype(float)
    total_vol = vols.sum()
    if total_vol <= 0:
        return None
    typical = (highs + lows + closes) / 3.0
    return float((typical * vols).sum() / total_vol)


# MBSS v2 (user request 2026-08-24, live case FIRE: gap-up +13.4% di
# pembukaan, TIDAK KETANGKAP Alert A/B karena keduanya cuma bandingkan bar
# SESAMA hari itu -- gap yg terjadi SEBELUM bar pertama secara struktural
# tidak pernah terlihat). Informational-only tag, BUKAN alert baru dgn
# klaim prediktif -- dites thd data 1m riil (344 ticker, 19 hari bursa,
# n=4003 gap-day event) TAPI per-bucket n KECIL (n=15 holds di sweet spot),
# jauh di bawah confidence temuan lain sesi ini (puluhan ribu sampel).
# HANYA bucket 5-12% (gabungan 5-8%+8-12%) yg ditampilkan -- further-gain
# median +16.5%, 66.7% EOD positif thd open. Di LUAR range ini SENGAJA
# tidak fire sama sekali (user request -- "kalau >12% masih bahaya spt
# >15%, drop; <5% no meaningful gain, drop juga"): gap<5% historis lemah
# (n=85, cuma 29.3% EOD positif bahkan yg holds), gap>=12% (persis kasus
# FIRE sendiri) JUSTRU paling lemah (+6.3%, cuma 20% EOD positif, n=5) --
# FIRE profitable hari itu TIDAK berarti gap besar reliably bagus, dan
# gap>=12% juga cepat mendekati wilayah NO_ROOM_GAIN_PCT (exhaust) yg
# sudah terbukti berisiko tinggi (lihat catatan no-room di atas). "Holds"
# = harga tidak jatuh >3% dari open dlm 5 bar pertama yg tersedia (sama
# persis metodologi backtest-nya).
GAP_UP_MIN_PCT = 5.0
GAP_UP_MAX_PCT = 12.0
GAP_UP_HOLD_CHECK_BARS = 5
GAP_UP_HOLD_MAX_DROP_PCT = -3.0
GAP_UP_SWEET_SPOT_NOTE = "🥇 sweet spot historis (further-gain median +16.5%, 66.7% EOD positif — n=15 holds, sample kecil)"


def _detect_gap_up(bars: pd.DataFrame, prev_close: float) -> dict | None:
    if bars.empty or not prev_close or prev_close <= 0:
        return None
    day_open = float(bars["Open"].astype(float).iloc[0])
    if day_open <= 0:
        return None
    gap_pct = (day_open - prev_close) / prev_close * 100
    if gap_pct < GAP_UP_MIN_PCT or gap_pct >= GAP_UP_MAX_PCT:
        return None  # di luar sweet spot 5-12% -- sengaja tidak fire, bukan cuma diberi catatan lemah
    check_bars = bars.iloc[:GAP_UP_HOLD_CHECK_BARS]
    if len(check_bars) < GAP_UP_HOLD_CHECK_BARS:
        return None  # tunggu cukup bar dulu sebelum menilai "holds" -- first-touch tetap terjaga via state gap_checked
    low_so_far = float(check_bars["Low"].astype(float).min())
    holds = (low_so_far - day_open) / day_open * 100 >= GAP_UP_HOLD_MAX_DROP_PCT
    return {"gap_pct": gap_pct, "day_open": day_open, "holds": holds, "bucket_note": GAP_UP_SWEET_SPOT_NOTE}


def _build_gap_up_message(ticker: str, detection: dict) -> str:
    hold_txt = "bertahan" if detection["holds"] else "belum jelas bertahan (sempat turun >3% dari open)"
    note = f"\n{detection['bucket_note']}" if detection["bucket_note"] else ""
    return (
        f"🌅 {ticker} GAP-UP +{detection['gap_pct']:.1f}% di pembukaan ({hold_txt}) | open {detection['day_open']:,.0f}"
        f"{note}\nInformational — sample historis kecil, bukan sinyal beli."
    )


# ── Deteksi (reuse persis logika riset — first-touch, bukan snapshot terakhir) ──

def _detect_alert_a(bars: pd.DataFrame, prev_close: float) -> dict | None:
    """
    First-touch spike>=4% (3 bar) + volume_ratio>=5x (3 bar vs baseline 3 bar
    sebelumnya) di SELURUH bar hari ini sejauh ini -- persis metodologi
    backtest, supaya tidak ada kejadian yg terlewat gara-gara timing scan.
    """
    closes = bars["Close"].astype(float)
    highs = bars["High"].astype(float)
    vols = bars["Volume"].fillna(0).astype(float)
    n = len(bars)
    min_bars = ALERT_A_LOOKBACK_BARS + ALERT_A_BASELINE_BARS
    if n < min_bars:
        return None
    for i in range(min_bars - 1, n):
        price_now = closes.iloc[i]
        price_before = closes.iloc[i - ALERT_A_LOOKBACK_BARS]
        if price_before <= 0:
            continue
        spike_pct = (price_now - price_before) / price_before * 100
        if spike_pct < ALERT_A_SPIKE_PCT:
            continue
        recent_vol = vols.iloc[i - ALERT_A_LOOKBACK_BARS + 1:i + 1].sum()
        baseline_window = vols.iloc[:i - ALERT_A_LOOKBACK_BARS + 1].tail(ALERT_A_BASELINE_BARS)
        if baseline_window.empty or baseline_window.mean() <= 0:
            continue
        baseline_vol = baseline_window.mean() * ALERT_A_LOOKBACK_BARS
        volume_ratio = recent_vol / baseline_vol
        if volume_ratio < ALERT_A_VOLUME_RATIO:
            continue
        return {
            "spike_pct": float(spike_pct), "volume_ratio": float(volume_ratio),
            "price": float(price_now), "time": bars.index[i].strftime("%H:%M"),
        }
    return None


def _detect_alert_b(bars: pd.DataFrame, prev_close: float) -> dict | None:
    """
    1) Cari threshold tertinggi (dari 4/5/6%) yg pernah tersentuh hari ini.
    2) Dari titik itu, tandai peak_price = harga tertinggi SEJAK tier itu
       tersentuh (bukan cuma harga di titik sentuh -- bisa lanjut naik dulu).
    3) Cari breach pertama: Low turun >=4% dari peak_price, SETELAH peak_price
       ditetapkan (peak reference berhenti update begitu breach terjadi).
    4) Cek rebound: dlm 15 menit setelah breach, High balik ke >=-1% dari
       peak_price -- kalau ya, itu Alert B.
    Persis metodologi backtest (pullback_depth_sweep_multi.py).
    """
    closes = bars["Close"].astype(float)
    highs = bars["High"].astype(float)
    lows = bars["Low"].astype(float)
    n = len(bars)

    gain_high = (highs - prev_close) / prev_close * 100
    peak_tier = None
    for tier in sorted(ALERT_B_THRESHOLD_TIERS, reverse=True):
        if (gain_high >= tier).any():
            peak_tier = tier
            break
    if peak_tier is None:
        return None

    tier_touch_idx = int((gain_high >= peak_tier).values.argmax())
    peak_price = float(highs.iloc[tier_touch_idx])
    peak_idx = tier_touch_idx
    for i in range(tier_touch_idx, n):
        if highs.iloc[i] > peak_price:
            peak_price = float(highs.iloc[i])
            peak_idx = i

    breach_idx = None
    for i in range(peak_idx, n):
        drawdown_pct = (lows.iloc[i] - peak_price) / peak_price * 100
        if drawdown_pct <= ALERT_B_PULLBACK_DEPTH_PCT:
            breach_idx = i
            break
    if breach_idx is None:
        return None

    rebound_target_price = peak_price * (1 + ALERT_B_REBOUND_TARGET_PCT / 100.0)
    window_end_time = bars.index[breach_idx] + pd.Timedelta(minutes=ALERT_B_REBOUND_WINDOW_MINUTES)
    for i in range(breach_idx, n):
        if bars.index[i] > window_end_time:
            break
        if highs.iloc[i] >= rebound_target_price:
            return {
                "peak_tier": peak_tier, "peak_price": peak_price,
                "breach_time": bars.index[breach_idx].strftime("%H:%M"),
                "rebound_time": bars.index[i].strftime("%H:%M"),
                "rebound_price": float(closes.iloc[i]),
                "gain_at_rebound_pct": float((closes.iloc[i] - prev_close) / prev_close * 100),
            }
    return None


# MBSS v2 (user request — "prediksi TP sehat dari speed harga, resiko
# fading kalau speed melandai"): pure price-speed decay (rasio kecepatan
# window 5m/5m atau 10m/10m) DITES DULU thd data 1m riil (344 ticker, 19
# hari bursa) -- korelasi cuma 0.04-0.06, TERLALU LEMAH utk dipakai (lihat
# riset sesi ini). Yang genuinely bermakna: harga MASIH naik TAPI volume 10
# menit terakhir menyusut <0.5x dari 10 menit sebelumnya -- median further-
# gain 30 menit ke depan turun ~35-40% (0.56% vs baseline 0.90%, n=5669) --
# klasik pola "buyer kering". Dipanggil HANYA saat Alert B fire (bukan
# bulk-scan), pakai bars 1m yg SUDAH di-fetch scan ini, TIDAK ada fetch
# tambahan.
VOLUME_PRICE_SIGNAL_WINDOW_MINUTES = 10
VOLUME_PRICE_FADING_RATIO_MAX = 0.5
VOLUME_PRICE_SOLID_RATIO_MIN = 1.5


def _compute_volume_price_signal(bars: pd.DataFrame, window_minutes: int = VOLUME_PRICE_SIGNAL_WINDOW_MINUTES) -> dict | None:
    closes = bars["Close"].astype(float)
    vols = bars["Volume"].fillna(0).astype(float)
    n = len(closes)
    if n < 2 * window_minutes + 1:
        return None
    c_now = float(closes.iloc[-1])
    c_w = float(closes.iloc[-1 - window_minutes])
    if c_w <= 0:
        return None
    price_change_pct = (c_now - c_w) / c_w * 100
    vol_recent = float(vols.iloc[-window_minutes:].sum())
    vol_prior = float(vols.iloc[-2 * window_minutes:-window_minutes].sum())
    if vol_prior <= 0:
        return None
    vol_ratio = vol_recent / vol_prior
    if price_change_pct > 0 and vol_ratio < VOLUME_PRICE_FADING_RATIO_MAX:
        signal = "fading"
    elif price_change_pct > 0 and vol_ratio > VOLUME_PRICE_SOLID_RATIO_MIN:
        signal = "solid"
    else:
        signal = "neutral"
    return {"signal": signal, "price_change_pct": price_change_pct, "vol_ratio": vol_ratio}


# ── Pesan (ringkas, sengaja tanpa banyak keterangan — user baca cepat & amati live) ──

def _vwap_segment(current_price: float | None, vwap: float | None) -> str:
    """" | VWAP 1.180 (+1.8%)" -- kosong total kalau VWAP belum tersedia (bukan dipaksa tampil placeholder)."""
    if vwap is None or current_price is None or vwap <= 0:
        return ""
    dist_pct = (current_price - vwap) / vwap * 100
    return f" | VWAP {vwap:,.0f} ({dist_pct:+.1f}%)"


# MBSS v2 (user request — "bantu analisa entry candidate: macd position,
# arah harga, TP1/TP2 selain VWAP, prediksi buy power"): dipanggil HANYA
# saat Alert B fire (bukan bulk-scan), pakai daily OHLC yg SUDAH ada di
# SQLite lokal (core.get_ohlcv_smart, DB-first, TIDAK nambah kuota Zapi) --
# reuse calculate_macd Brights-compatible yg sama dgn seluruh scoring
# harian, supaya "posisi MACD" di alert konsisten dgn makna yg sama di
# /check, /hc, /screendaytrade, BUKAN definisi terpisah.
def _compute_macd_position_label(ticker: str) -> str | None:
    try:
        hist_daily = core.get_ohlcv_smart(ticker, limit=40)
    except Exception as e:
        print(f"⚠️ Gagal fetch daily OHLC utk MACD position {ticker}: {e}")
        return None
    if hist_daily is None or hist_daily.empty or len(hist_daily) < 30:
        return None
    closes = hist_daily["Close"].astype(float)
    macd_line, signal_line, macd_hist = core.calculate_macd(closes)
    macd_now, signal_now = float(macd_line.iloc[-1]), float(signal_line.iloc[-1])
    hist_now = float(macd_hist.iloc[-1])
    hist_prev = float(macd_hist.iloc[-3]) if len(macd_hist) >= 3 else hist_now
    if macd_now > 0 and signal_now > 0:
        regime = "atas centerline"
    elif macd_now < 0 and signal_now < 0:
        regime = "bawah centerline"
    else:
        regime = "sekitar centerline"
    direction = "bullish" if macd_now > signal_now else "bearish"
    momentum = "menguat" if hist_now > hist_prev else "melemah"
    return f"MACD {direction} ({regime}), histogram {momentum}"


# TP1 = +10% dari prev_close -- KONSISTEN dgn milestone +10% yg sama yg
# dipakai lane/CONTINUATION/VALIDATION seharian ini (bukan definisi TP
# terpisah), bukan target baru yg diciptakan khusus alert. TP2 = titik 75%
# dari jarak prev_close->ARA -- waypoint SEBELUM plafon keras ARA (biar beda
# dari ARA itu sendiri, sesuai permintaan user "TP2 selain ARA").
ALERT_TP1_PCT = 10.0
ALERT_TP2_ARA_FRACTION = 0.75


def _compute_tp_targets(prev_close: float, ara_price: float | None) -> tuple[float | None, float | None]:
    if not prev_close or prev_close <= 0:
        return None, None
    tp1 = round(prev_close * (1 + ALERT_TP1_PCT / 100.0))
    tp2 = None
    if ara_price and ara_price > tp1:
        tp2_candidate = round(prev_close + (ara_price - prev_close) * ALERT_TP2_ARA_FRACTION)
        if tp2_candidate > tp1:
            tp2 = tp2_candidate
    return tp1, tp2


def _build_alert_a_message(ticker: str, detection: dict, ret_3d: float | None,
                            current_price: float | None, vwap: float | None) -> str:
    tag = " ⚠️lari kencang" if ret_3d is not None and ret_3d >= RET_3D_WARN_THRESHOLD else ""
    return (
        f"⚡ {ticker} +{detection['spike_pct']:.1f}% | vol {detection['volume_ratio']:.1f}x | "
        f"{detection['time']}{tag}{_vwap_segment(current_price, vwap)} | amati"
    )


def _build_alert_b_messages(ticker: str, detection: dict, ret_3d: float | None,
                             orderbook_check: dict | None,
                             current_price: float | None, vwap: float | None,
                             macd_label: str | None = None, tp1: float | None = None,
                             tp2: float | None = None, ara_price: float | None = None,
                             buy_power: dict | None = None) -> list[str]:
    tag = " ⚠️lari kencang" if ret_3d is not None and ret_3d >= RET_3D_WARN_THRESHOLD else ""
    messages = [
        f"✅ {ticker} PULLBACK REBOUND dari +{detection['peak_tier']}% | "
        f"skrg {detection['gain_at_rebound_pct']:+.1f}% | {detection['rebound_time']}"
        f"{tag}{_vwap_segment(current_price, vwap)} | entry candidate"
    ]
    if macd_label:
        messages.append(f"📊 {ticker} {macd_label}")

    tp_parts = []
    if tp1:
        tp_parts.append(f"TP1 {tp1:,.0f}")
    if tp2:
        tp_parts.append(f"TP2 {tp2:,.0f}")
    if ara_price:
        tp_parts.append(f"ARA {ara_price:,.0f}")
    if tp_parts:
        messages.append(f"🎯 {ticker} target: " + " | ".join(tp_parts))

    if orderbook_check and orderbook_check.get("confirmed"):
        bid_pct = orderbook_check.get("bid_percent")
        ask_pct = orderbook_check.get("ask_percent")
        messages.append(f"✅ {ticker} ORDERBOOK SOLID BUY | bid {bid_pct}% vs ask {ask_pct}%")

    if buy_power and buy_power.get("available") and buy_power.get("label"):
        reason_str = ", ".join(buy_power.get("reasons") or [])
        messages.append(f"{buy_power['label']}" + (f" ({reason_str})" if reason_str else ""))

    return messages


# ── Orkestrasi 1x scan ──────────────────────────────────────────────────────

# MBSS v2 (user request 2026-08-24 — "harus seamlessly berjalan beriringan
# dengan scanalert"): watch kandidat FRESH CROSS MOMENTUM (SDT, commands/
# scan.py -- cross_days_ago<=2, ret10_pre_cross_pct>15%) SECARA INTRADAY --
# begitu candidate ini pullback ke zona toleransi yg SUDAH divalidasi
# (median MAE trade yg EVENTUALLY hit +6% = -4.01%, p25 -9.01%, p10
# -14.29%, dari 576 ISSI/2thn n=635), push "BUY NOW"-style alert -- bukan
# nunggu user buka /screendaytrade. Watchlist DIHITUNG ULANG dari scratch
# tiap hari (bukan baca daytrade_picks_history.json) supaya jalan
# independen dari apakah user sudah jalankan /eodscan atau /screendaytrade
# hari itu -- genuinely "seamless", tanpa langkah manual.
PULLBACK_ENTRY_MIN_PCT = -2.0            # minimal pullback berarti dari open (bukan noise harian)
PULLBACK_ENTRY_HEALTHY_MAX_PCT = -9.0    # dlm p25 -- masih sangat umum utk trade yg eventually menang
PULLBACK_ENTRY_CAUTION_MAX_PCT = -14.0   # dlm p10 -- lebih dalam, tapi masih dlm rentang tervalidasi
MACD_FRESH_CROSS_MOMENTUM_MAX_DAYS_AGO = 2   # PERSIS commands/scan.py -- jangan drift dari definisi SDT
MACD_FRESH_CROSS_MOMENTUM_RET10_PRE_MIN = 15.0


def _get_fresh_cross_momentum_watchlist(universe: list[str]) -> dict:
    """
    Reuse PERSIS kriteria FRESH CROSS MOMENTUM (commands/scan.py, jangan
    drift) -- dihitung ulang di sini via compute_factor_scoring langsung,
    BUKAN baca daily_scan_cache/pick-history, supaya tidak bergantung pada
    command lain sudah dijalankan user hari itu.
    """
    from engine import scoring  # import lokal -- pola sama dgn broker_engine's compute_orderflow_snapshot_zapi, hindari import-time circular
    watchlist = {}
    for t in universe:
        try:
            r = scoring.compute_factor_scoring(t, include_quote_check=False)
        except Exception:
            continue
        if not r:
            continue
        if (
            r.get("macd_cross_direction") == "bullish"
            and r.get("macd_cross_days_ago") is not None
            and r["macd_cross_days_ago"] <= MACD_FRESH_CROSS_MOMENTUM_MAX_DAYS_AGO
            and r.get("macd_ret10_pre_cross_pct") is not None
            and r["macd_ret10_pre_cross_pct"] > MACD_FRESH_CROSS_MOMENTUM_RET10_PRE_MIN
        ):
            watchlist[t] = {
                "ret10_pre_cross_pct": r["macd_ret10_pre_cross_pct"],
                "cross_days_ago": r["macd_cross_days_ago"],
            }
    return watchlist


def _detect_pullback_entry(bars: pd.DataFrame) -> dict | None:
    """
    Pullback dari OPEN hari ini -- BUKAN dari peak spt Alert B. FRESH CROSS
    MOMENTUM entry-nya "ref: open sesi berikutnya" (lihat commands/scan.py),
    jadi open hari ini ADALAH titik referensi entry, bukan peak intraday.
    First-touch begitu pullback TERDALAM sejauh ini masuk zona [-9%,-2%]
    (sehat) atau (-14%,-9%] (hati-hati) -- di luar -14% TIDAK di-alert
    (di luar rentang tervalidasi utk trade yg eventually menang).
    """
    if bars.empty:
        return None
    day_open = float(bars["Open"].astype(float).iloc[0])
    if day_open <= 0:
        return None
    current_price = float(bars["Close"].astype(float).iloc[-1])
    lowest_so_far = float(bars["Low"].astype(float).min())
    deepest_pullback_pct = (lowest_so_far - day_open) / day_open * 100
    if deepest_pullback_pct > PULLBACK_ENTRY_MIN_PCT:
        return None
    if deepest_pullback_pct < PULLBACK_ENTRY_CAUTION_MAX_PCT:
        return None  # di luar toleransi tervalidasi -- sengaja tidak alert, bukan lupa
    zone = "healthy" if deepest_pullback_pct >= PULLBACK_ENTRY_HEALTHY_MAX_PCT else "caution"
    return {"pullback_pct": deepest_pullback_pct, "current_price": current_price, "day_open": day_open, "zone": zone}


def _build_pullback_entry_message(ticker: str, detection: dict, watchlist_entry: dict) -> str:
    zone_label = "🎯 SEHAT (dlm p25)" if detection["zone"] == "healthy" else "⚠️ HATI-HATI (mendekati batas p10)"
    return (
        f"🔥 {ticker} PULLBACK ENTRY — FRESH CROSS MOMENTUM ({watchlist_entry['cross_days_ago']} hari lalu, "
        f"momentum pre-cross +{watchlist_entry['ret10_pre_cross_pct']:.1f}%)\n"
        f"Pullback {detection['pullback_pct']:.1f}% dari open ({detection['day_open']:,.0f}) — skrg {detection['current_price']:,.0f} | {zone_label}\n"
        f"Tervalidasi: median MAE trade menang -4.0%, p25 -9.0%, p10 -14.3% (n=635) — bukan sinyal beli otomatis, verifikasi live."
    )


# MBSS v2 (user correction 2026-08-24 -- "tidak hanya healthy pullback,
# termasuk kalau masuk ke zona validation yang menandakan sinyal naik
# semakin tinggi"): sisi SEBALIKNYA dari pullback-entry -- kandidat FCM yg
# HARI INI lanjut naik kuat (bukan dip) juga sinyal "BUY NOW" yg valid,
# konsisten dgn temuan terkuat sesi ini (gap_slope>=Q4 + ret_1d_today>3% ->
# hit6=68.79%, jauh lebih baik dari pullback-only). Threshold >3% REUSE
# dari riset MOMENTUM EXTENDED itu (extended-episode 6-10hr), BUKAN
# independen divalidasi khusus utk konteks FCM (0-2hr post-cross) -- extra-
# polasi yg beralasan, bukan angka baru dari nol. Beri tahu user kalau mau
# divalidasi ulang khusus populasi FCM.
CONFIRMATION_ENTRY_MIN_GAIN_PCT = 3.0


def _detect_confirmation_entry(bars: pd.DataFrame) -> dict | None:
    """First-touch begitu gain dari open hari ini >= ambang -- momentum lanjut naik, bukan pullback."""
    if bars.empty:
        return None
    day_open = float(bars["Open"].astype(float).iloc[0])
    if day_open <= 0:
        return None
    current_price = float(bars["Close"].astype(float).iloc[-1])
    highest_so_far = float(bars["High"].astype(float).max())
    gain_pct = (highest_so_far - day_open) / day_open * 100
    if gain_pct < CONFIRMATION_ENTRY_MIN_GAIN_PCT:
        return None
    return {"gain_pct": gain_pct, "current_price": current_price, "day_open": day_open}


def _build_confirmation_entry_message(ticker: str, detection: dict, watchlist_entry: dict) -> str:
    return (
        f"🚀 {ticker} CONFIRMATION ENTRY — FRESH CROSS MOMENTUM ({watchlist_entry['cross_days_ago']} hari lalu, "
        f"momentum pre-cross +{watchlist_entry['ret10_pre_cross_pct']:.1f}%)\n"
        f"Lanjut naik +{detection['gain_pct']:.1f}% dari open ({detection['day_open']:,.0f}) — skrg {detection['current_price']:,.0f}\n"
        f"Sinyal konfirmasi (bukan pullback) — historis kombinasi momentum+konfirmasi hari sama hit6~69% (konteks episode extended, ekstrapolasi ke FCM) — verifikasi live."
    )


async def run_scan_alert_once() -> dict:
    """
    Satu kali scan penuh: fetch universe + data, deteksi Alert A/B per
    ticker, kirim ke Telegram (kalau ada & belum dikirim hari ini), simpan
    state. Return summary dict (utk logging CLI).
    """
    summary = {"skipped_reason": None, "alert_a_sent": 0, "alert_b_sent": 0, "scanned": 0, "excluded_no_room": 0, "gap_up_sent": 0, "pullback_entry_sent": 0}

    now_wib = datetime.datetime.now(core.WIB)
    if now_wib.weekday() >= 5:  # Sabtu/Minggu -- no-op murah, cek ini SEBELUM network call apapun
        summary["skipped_reason"] = "weekend"
        return summary
    if not (SCAN_WINDOW_START <= now_wib.time() <= SCAN_WINDOW_END):
        summary["skipped_reason"] = "outside_market_hours"
        return summary

    if await asyncio.to_thread(core.is_idx_market_holiday_today):
        summary["skipped_reason"] = "holiday"
        print("📅 Scan-alert: libur bursa hari ini, skip.")
        return summary

    state = _ensure_daily_reset(_load_state())

    if not is_scan_alert_enabled():
        summary["skipped_reason"] = "toggled_off"
        print("⏸️ Scan-alert: OFF (manual toggle) -- skip scan ini.")
        _save_state(state)
        return summary

    universe = _get_alert_universe()
    if not universe:
        summary["skipped_reason"] = "no_universe"
        return summary

    if state.get("daily_ref") is None:
        print(f"📡 Scan-alert: fetch daily reference (prev_close + ret_3d) utk {len(universe)} ticker whitelist...")
        daily_ref = _fetch_daily_ref(universe)
        state["daily_ref"] = daily_ref
        print(f"✅ Universe 60-600 hari ini: {len(daily_ref)} ticker (dari {len(universe)} whitelist)")
    else:
        daily_ref = state["daily_ref"]

    if state.get("fresh_cross_momentum_watchlist") is None:
        print(f"📡 Scan-alert: hitung watchlist FRESH CROSS MOMENTUM (cross<=2hr, ret10_pre>15%) utk {len(universe)} ticker...")
        fcm_watchlist = await asyncio.to_thread(_get_fresh_cross_momentum_watchlist, universe)
        state["fresh_cross_momentum_watchlist"] = fcm_watchlist
        print(f"✅ FRESH CROSS MOMENTUM watchlist hari ini: {len(fcm_watchlist)} ticker.")
    else:
        fcm_watchlist = state["fresh_cross_momentum_watchlist"]

    # Union -- FCM watchlist BISA di luar band harga 60-600 (tidak ada floor
    # harga di definisi SDT-nya), jadi tidak selalu subset alert_universe.
    alert_universe = list(daily_ref.keys())
    full_ticker_set = sorted(set(alert_universe) | set(fcm_watchlist.keys()))
    print(f"🔍 Scan-alert: {len(full_ticker_set)} ticker ({len(alert_universe)} alert + {len(fcm_watchlist)} FCM watchlist), fetch bar 1m...")
    data = await asyncio.to_thread(_fetch_today_1m, full_ticker_set)

    tickers_state = state.setdefault("tickers", {})
    bot = None
    if core.TELEGRAM_BOT_TOKEN:
        import telegram
        bot = telegram.Bot(token=core.TELEGRAM_BOT_TOKEN)

    for t in full_ticker_set:
        ref = daily_ref.get(t)
        prev_close = ref["prev_close"] if ref else None
        ret_3d = ref.get("ret_3d") if ref else None
        sym = t + ".JK"
        try:
            bars = data[sym].dropna(how="all").sort_index()
        except Exception:
            continue
        if bars.empty:
            continue
        summary["scanned"] += 1

        current_price = float(bars["Close"].astype(float).iloc[-1])
        vwap_now = _compute_current_session_vwap(bars)

        t_state = tickers_state.setdefault(
            t, {"excluded_no_room": False, "alert_a_sent": False, "alert_b_sent": False, "gap_up_sent": False, "watchlist_entry_sent": False}
        )
        t_state.setdefault("gap_up_sent", False)  # ticker lama di state file blm punya field ini
        t_state.setdefault("watchlist_entry_sent", False)

        # FRESH CROSS MOMENTUM watchlist entry -- DUA sisi (user correction:
        # bukan cuma pullback), independen dari prev_close/Alert A-B
        # machinery di bawah (ticker ini bisa HANYA ada krn masuk
        # fcm_watchlist, di luar band harga 60-600 scanalert biasa). Satu
        # flag dibagi utk keduanya -- begitu salah satu fire, cukup 1x/hari,
        # tidak dobel-alert ticker yg sama.
        if t in fcm_watchlist and not t_state["watchlist_entry_sent"]:
            det_confirm = _detect_confirmation_entry(bars)
            if det_confirm:
                msg = _build_confirmation_entry_message(t, det_confirm, fcm_watchlist[t])
                if bot is not None:
                    await core.safe_reply(bot, msg, chat_id=core.TELEGRAM_CHAT_ID)
                else:
                    print(f"[NO TELEGRAM TOKEN] {msg}")
                t_state["watchlist_entry_sent"] = True
                summary["pullback_entry_sent"] += 1
            else:
                det_pullback = _detect_pullback_entry(bars)
                if det_pullback:
                    msg = _build_pullback_entry_message(t, det_pullback, fcm_watchlist[t])
                    if bot is not None:
                        await core.safe_reply(bot, msg, chat_id=core.TELEGRAM_CHAT_ID)
                    else:
                        print(f"[NO TELEGRAM TOKEN] {msg}")
                    t_state["watchlist_entry_sent"] = True
                    summary["pullback_entry_sent"] += 1

        if ref is None:
            continue  # ticker ini HANYA di fcm_watchlist (di luar band 60-600) -- Alert A/B/gap-up di bawah butuh prev_close, sisanya di-skip

        if not t_state["gap_up_sent"]:
            det_gap = _detect_gap_up(bars, prev_close)
            if det_gap:
                msg = _build_gap_up_message(t, det_gap)
                if bot is not None:
                    await core.safe_reply(bot, msg, chat_id=core.TELEGRAM_CHAT_ID)
                else:
                    print(f"[NO TELEGRAM TOKEN] {msg}")
                t_state["gap_up_sent"] = True
                summary["gap_up_sent"] = summary.get("gap_up_sent", 0) + 1

        if not t_state["excluded_no_room"] and not t_state["alert_a_sent"] and not t_state["alert_b_sent"]:
            day_open = float(bars["Open"].astype(float).iloc[0])
            if day_open > 0:
                gain_from_open = float((bars["High"].astype(float).max() - day_open) / day_open * 100)
                if gain_from_open >= NO_ROOM_GAIN_PCT:
                    t_state["excluded_no_room"] = True
                    summary["excluded_no_room"] += 1
        if t_state["excluded_no_room"]:
            continue

        if not t_state["alert_a_sent"]:
            det_a = _detect_alert_a(bars, prev_close)
            if det_a:
                msg = _build_alert_a_message(t, det_a, ret_3d, current_price, vwap_now)
                if bot is not None:
                    await core.safe_reply(bot, msg, chat_id=core.TELEGRAM_CHAT_ID)
                else:
                    print(f"[NO TELEGRAM TOKEN] {msg}")
                t_state["alert_a_sent"] = True
                summary["alert_a_sent"] += 1

        if not t_state["alert_b_sent"]:
            det_b = _detect_alert_b(bars, prev_close)
            if det_b:
                # MBSS v2 (user request — enrich Alert B: MACD position, TP1/
                # TP2/ARA, prediksi buy power). Semua fallback aman ke None
                # kalau fetch gagal (DB lokal utk MACD, Zapi utk orderbook) --
                # _build_alert_b_messages sudah skip section yg None/kosong,
                # jadi Alert B TETAP terkirim (pesan inti) walau enrichment gagal.
                orderbook_check = await asyncio.to_thread(
                    broker_engine.check_orderbook_solid_buy_zapi, t
                )
                macd_label = await asyncio.to_thread(_compute_macd_position_label, t)
                ara_price = broker_engine.compute_ara_price(prev_close)
                tp1, tp2 = _compute_tp_targets(prev_close, ara_price)
                volume_price_signal = _compute_volume_price_signal(bars)
                buy_power = broker_engine.predict_buy_power_trajectory(
                    prev_close, current_price, ara_price, orderbook_check, volume_price_signal
                )
                for msg in _build_alert_b_messages(
                    t, det_b, ret_3d, orderbook_check, current_price, vwap_now,
                    macd_label=macd_label, tp1=tp1, tp2=tp2, ara_price=ara_price, buy_power=buy_power,
                ):
                    if bot is not None:
                        await core.safe_reply(bot, msg, chat_id=core.TELEGRAM_CHAT_ID)
                    else:
                        print(f"[NO TELEGRAM TOKEN] {msg}")
                t_state["alert_b_sent"] = True
                summary["alert_b_sent"] += 1

    _save_state(state)
    print(f"✅ Scan-alert selesai: {summary['scanned']} ticker discan, "
          f"{summary['alert_a_sent']} Alert A, {summary['alert_b_sent']} Alert B, "
          f"{summary['pullback_entry_sent']} FCM watchlist entry, "
          f"{summary['excluded_no_room']} di-exclude (no room).")
    return summary
