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

MBSS v2 (user request 2026-08-26, riset entry-timing FCM/PRE/CONTINUATION/
HC — backtest 2 tahun 576 ISSI raw OHLC, cakupan penuh di chat sesi ini):
3 mekanisme baru, masing-masing entry-timing berbeda krn karakter sinyalnya
beda (lihat konstanta terkait utk detail riset):
  - FCM: alert "beli di open" (jendela pendek FCM_OPEN_BUY_WINDOW_END),
    TAMBAHAN di atas pullback/confirmation-entry yg sudah ada (bukan
    pengganti) -- jangan kejar >2% dari open (FCM_OPEN_BUY_CHASE_CAP_PCT).
  - PRE-CROSS (SDT) & CONTINUATION (HC): alert "menjelang closing", scan
    HANYA mulai jam 14:00 (PRE_CONTINUATION_SCAN_START), begitu body candle
    hari berjalan hijau >=1% dari open (PRE_CONTINUATION_BODY_MIN_PCT).
    Volume informational saja, bukan gate.
  - HC Minervini: di-hide dari /hc langsung (win rate rendah), dipantau
    live SEKALI esok harinya utk gap-up >=3% (HC_GAP_WATCH_MIN_GAP_PCT) dari
    closing malam saat pertama diflag -- watchlist dari engine/nightly.py
    save_hc_gap_watch/load_hc_gap_watch_for_today.

Bisa dipanggil sbg one-shot CLI (`python bot.py --scanalert`, ikut pola
`--eodscan` yg sudah ada). TAPI di deployment produksi SEBENARNYA (GCP,
lihat engine/legacy_core.py build_app()) dipanggil lewat python-telegram-
bot's JobQueue IN-PROCESS (app.job_queue.run_repeating(run_scanalert_job,
interval=300, ...), run_scanalert_job cuma wrapper tipis ke
run_scan_alert_once() di sini) -- BUKAN cron eksternal Termux spt yg
DIASUMSIKAN sebelumnya di catatan ini (koreksi MBSS v2, user request
2026-08-27 -- ditemukan dari log produksi riil yg menunjukkan apscheduler
job "run_scanalert_job (trigger: interval[0:05:00])", bukan proses CLI
terpisah). Interval 300s (5 menit) itu masih HARDCODED di legacy_core.py,
BELUM diturunkan ke 3 menit spt riset audit timing merekomendasikan (lihat
komentar itu tetap valid sbg alasan/riset -- cuma lokasi eksekusinya yg
salah diasumsikan) -- PENTING sebelum diturunkan: log produksi yg sama jg
menunjukkan APScheduler kadang SUDAH skip run krn "maximum number of
running instances reached" di interval 5 menit (1x scan penuh kadang
mendekati/melebihi 5 menit, makin berat sejak FCM/PRE-CONTINUATION/HC-gap-
watch/BSJP-watch ditambahkan) -- turun ke 3 menit BISA memperparah overlap-
skip ini kalau runtime per-siklus tidak ikut dipangkas, jadi ukur dulu
runtime aktual sebelum mengubah interval, jangan cuma ganti angkanya.

Tiap scan FETCH ULANG seluruh bar 1m hari ini (bukan incremental) dan
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
import time

import pandas as pd
import yfinance as yf

from engine import legacy_core as core
from engine import broker as broker_engine
from engine import lane_confidence

STATE_FILE = os.path.join(core.PROJECT_ROOT, "scanalert_state.json")

PRICE_FLOOR = 60
PRICE_CEILING = 600
NO_ROOM_GAIN_PCT = 15.0
RET_3D_WARN_THRESHOLD = 10.0

# MBSS v2 (user request 2026-08-27 -- audit backtest 1m riil 27 hari bursa):
# Alert A (rolling 3-bar spike) & Alert B (pullback-rebound) TERBUKTI edge-nya
# nyaris 0 sbg sinyal actionable -- confirm-then-notify di berbagai delay/
# threshold TIDAK PERNAH menghasilkan median close positif konsisten (blip
# rate 63-85%, window TP realistis cuma ~1-2 menit, di bawah waktu reaksi
# manusia + cadence cron manapun). Digantikan Alert C (gain dari prev_close,
# _detect_open_buy dkk di atas) & REBOUND (gap-open+tier, di bawah) yang
# BEDA JAUH lebih baik di backtest yang sama. PUSH di-nonaktifkan (bukan
# dihapus -- _detect_alert_a/_detect_alert_b & pesan builder-nya TETAP ada,
# tinggal balik True kalau nanti ada desain baru yang mau dites ulang).
ALERT_A_B_PUSH_ENABLED = False

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

# MBSS v2 (user request 2026-08-27 -- riset backtest gap-open 4-10%, 1m riil
# 27 hari): "REBOUND" -- sinyal TERPISAH dari Alert A/B/gap-up di atas,
# dipanggil lewat CLI/cron SENDIRI (`python bot.py --scanalert-rebound`,
# state file sendiri jg -- HINDARI race condition baca-ubah-tulis bareng
# scanalert_state.json kalau kebetulan jalan bersamaan dgn --scanalert
# utama). Alasan terpisah: butuh cadence 1 MENIT (bukan 3 menit spt
# --scanalert utama) krn median waktu fire = menit ke-0 sejak open --
# TAPI hanya perlu jendela SEMPIT 09:00-09:10 (bukan 1 menit sepanjang
# hari, boros API call utk manfaat yg cuma relevan di pembukaan).
#
# Mekanisme (persis speks user): track running-low sejak OPEN (reset ke
# low TERBARU tiap kali ada low lebih dalam -- backtest "true MAE" pakai
# hindsight, live TIDAK bisa tahu titik terendah di muka, jadi dinamis).
# Begitu High rebound dari running-low SEKARANG >= tier (0.5/1.0/1.5/2.0%),
# fire alert utk tier itu (first-touch PER TIER, independen). Berhenti
# tracking ticker itu total begitu tier 2.0% tersentuh -- dites, ambang
# rebound LEBIH KECIL = kualitas forward LEBIH BAIK (0.5%: +15m close
# median +1.83%/68.9% positif -- TERBAIK dari seluruh eksplorasi sesi
# ini; 2.0%: +1.60%/59.8% -- masih oke tapi mulai menurun, cutoff wajar).
GAP_REBOUND_MIN_PCT = 4.0     # gap open dari prev_close, batas bawah (sweet spot 5-8% tapi 4-10% dites juga oke)
GAP_REBOUND_MAX_PCT = 10.0    # di atas ini masuk wilayah "instant pop lalu fade EOD" -- beda populasi/karakter, exclude dari REBOUND
GAP_REBOUND_TIERS = (0.5, 1.0, 1.5, 2.0)
GAP_REBOUND_DETECT_WINDOW_MIN = 10   # cari rebound HANYA 10 menit pertama sejak open, konsisten dgn riset
GAP_REBOUND_TP1_PCT = 4.0     # median MFE ~4-5% di horizon 5-10m dari entry rebound
GAP_REBOUND_TP2_PCT = 6.0     # median MFE ~6% di horizon 15-20m
GAP_REBOUND_SL_PCT = -2.5     # dekat P25 MAE dari entry rebound (-3.25%) -- kasih ruang dip median (-1.27%), potong sblm ekor buruk
GAP_REBOUND_MAX_HOLD_MIN = 20  # window realisasi TP1/TP2, sesuai riset "siku" di 15-20 menit
GAP_REBOUND_SCAN_WINDOW_START = datetime.time(9, 0)
GAP_REBOUND_SCAN_WINDOW_END = datetime.time(9, 10)
STATE_FILE_REBOUND = os.path.join(core.PROJECT_ROOT, "scanalert_rebound_state.json")

# MBSS v2 (user request 2026-08-27 -- "conviction sweep": pantau UNION
# watchlist harian [FCM, PRE-CROSS/CONTINUATION/VALIDATION, HC gap-watch,
# BSJP-watch] tiap 15 menit mulai 10:00, fire alert bertingkat begitu
# harga nunjukkin "momentum membangun" [2 checkpoint 15-menit naik
# beruntun -- reset kalau ada checkpoint yg flat/turun]). Reuse watchlist
# yg SUDAH dihitung run_scan_alert_once() (baca scanalert_state.json
# langsung) -- HINDARI hitung ulang compute_factor_scoring utk SELURUH
# universe dua kali sehari (mahal, network call per ticker). Jendela mulai
# 10:00 (bukan 09:00) krn universe FCM/PRE/CONTINUATION baru lengkap
# begitu run_scan_alert_once() sudah jalan minimal sekali pagi itu.
CONVICTION_SWEEP_WINDOW_START = datetime.time(10, 0)
CONVICTION_SWEEP_WINDOW_END = datetime.time(15, 55)
CONVICTION_SWEEP_CONSECUTIVE_UP_REQUIRED = 2   # 2 checkpoint 15-menit naik beruntun = 1 tier baru
CONVICTION_SWEEP_MAX_TIERS = 4                 # cap spam -- tier ke-4 butuh 8 checkpoint (~2 jam) naik terus
CONVICTION_SWEEP_PULLBACK_EXCEPTION_ENABLED = True

# MBSS v2 (user request 2026-08-27 -- TP1/TP2 individual per ticker,
# engine/lane_confidence.py): dict ini DIPANGKAS -- 5 entri lama (ABOVE_
# MOMENTUM/FCM/CONTINUATION/VALIDATION/MOMENTUM_EXTENDED, ceiling per-lane
# FLAT dari room_ladder_backtest.py) sudah TIDAK PERNAH tercapai lagi di
# run_conviction_sweep_once -- kelima lane itu sekarang lewat lane_
# confidence.compute_tp1_tp2 (per-TICKER, bukan per-lane), yang HASILNYA
# (real tp_info ATAU None+ticker di-drop dari universe) selalu sudah
# tersedia sebelum baris yg baca dict ini dieksekusi. Sisa 2 entri
# (FAST_RECOVERY/EARLY_RECOVERY) TETAP relevan -- lane_confidence SENGAJA
# tidak mendukungnya (angka resmi 62.35%/59.2% beda dari quick-recompute
# sandbox sesi ini, belum diinvestigasi) -- begitu juga DEFAULT_PCT
# (fallback HC_GAP_WATCH/BSJP_ARA/BSJP_SECOND_WAVE, lane di luar cakupan
# lane_confidence sama sekali).
CONVICTION_TP_CEILING_PCT = {
    "FAST_RECOVERY": 8.0,
    "EARLY_RECOVERY": 8.0,
}
CONVICTION_TP_CEILING_DEFAULT_PCT = 8.0  # HC_GAP_WATCH / BSJP_ARA / BSJP_SECOND_WAVE -- populasi gabungan ~49.4% di +8%, blm ada ceiling spesifik tervalidasi utk lane2 ini

STATE_FILE_CONVICTION = os.path.join(core.PROJECT_ROOT, "conviction_sweep_state.json")


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
    fetch_online_sharia_list() (baca lokal, murah, aman dipanggil tiap scan)
    sebagai lapis pertahanan kedua -- jangan bergantung pada file
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
    except Exception as e:
        print(f"⚠️ Gagal memuat daftar Sharia terkunci ({e}) -- pakai whitelist apa adanya, TIDAK di-intersect.")
        filtered = eligible
    return _exclude_erratic_volatility_profile(filtered)


# MBSS v2 (user request 2026-08-27 -- audit Alert A/B timing/drift, keputusan
# user "riwayat pola beku-meledak langsung exclude saja, termasuk yang
# extremely range candle tipis lebar"): HARD EXCLUDE dari universe (bukan
# tag informational spt _risk_tags -- dua pola ini beda kelas, user pilih
# tidak mau lihat sama sekali, bukan cuma diberi peringatan). Dua pola beda:
#   FROZEN_THEN_EXPLODE: median range harian 60 hari nyaris nol (nyaris
#     tidak bergerak MAYORITAS hari) TAPI max range ekstrem -- live case
#     YPAS (median=0.00%, max=38.10%, drift alert +15.32% di data 1m riil).
#     MEDIAN dipilih (bukan mean) justru krn mean gampang ke-drag oleh 1-2
#     hari ekstrem, padahal itu yang mau ditangkap -- exclude butuh baca
#     "mayoritas hari beku", median lebih representasikan itu.
#   CHRONICALLY_WIDE_RANGE: median range harian 60 hari sendiri sudah tinggi
#     terus-menerus (BUKAN sesekali) -- live case DOSS/DAYA/ARII (persentil
#     73-90 dari seluruh universe drift-audit).
FROZEN_MEDIAN_RANGE_MAX_PCT = 1.0
FROZEN_MAX_RANGE_MIN_PCT = 25.0
CHRONIC_WIDE_MEDIAN_RANGE_MIN_PCT = 5.5  # DOSS (real case, persentil 90 drift-audit) median_range 2thn ~5.95% -- 5.5 dipilih supaya kasus itu genuinely kena, bukan lolos tipis-tipis


def _exclude_erratic_volatility_profile(tickers: list[str]) -> list[str]:
    """None-safe (data historis kurang dari 60hr -> TIDAK dikecualikan, "missing=neutral")."""
    import engine.nightly as nightly_engine  # import lokal, hindari circular import di level modul
    try:
        scored = nightly_engine.load_daily_scan_cache()
    except Exception as e:
        print(f"⚠️ Gagal memuat daily_scan_cache utk cek pola volatilitas ({e}) -- exclude di-skip, universe apa adanya.")
        return tickers
    if not scored:
        return tickers

    kept = []
    excluded_frozen, excluded_wide = [], []
    for t in tickers:
        r = scored.get(t)
        med = r.get("hist_median_range_pct_60d") if r else None
        mx = r.get("hist_max_range_pct_60d") if r else None
        if med is None or mx is None:
            kept.append(t)
            continue
        if med <= FROZEN_MEDIAN_RANGE_MAX_PCT and mx >= FROZEN_MAX_RANGE_MIN_PCT:
            excluded_frozen.append(t)
            continue
        if med >= CHRONIC_WIDE_MEDIAN_RANGE_MIN_PCT:
            excluded_wide.append(t)
            continue
        kept.append(t)

    if excluded_frozen:
        print(f"🧊 Scan-alert: {len(excluded_frozen)} ticker di-exclude (pola beku-lalu-meledak): {', '.join(excluded_frozen[:15])}{' ...' if len(excluded_frozen) > 15 else ''}")
    if excluded_wide:
        print(f"📈 Scan-alert: {len(excluded_wide)} ticker di-exclude (chronically wide-range): {', '.join(excluded_wide[:15])}{' ...' if len(excluded_wide) > 15 else ''}")
    return kept


# ── REBOUND (gap-open 4-10%, tier 0.5/1.0/1.5/2.0%) ─────────────────────────

def _load_rebound_state() -> dict:
    if not os.path.exists(STATE_FILE_REBOUND):
        return {}
    try:
        with open(STATE_FILE_REBOUND) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_rebound_state(state: dict):
    with open(STATE_FILE_REBOUND, "w") as f:
        json.dump(state, f, indent=2)


def _ensure_rebound_daily_reset(state: dict) -> dict:
    today = _today_str()
    if state.get("trading_day_marker") != today:
        return {"trading_day_marker": today, "tickers": {}}
    return state


def _detect_gap_rebound_tiers(bars: pd.DataFrame, day_open: float, running_low: float, tiers_fired: list) -> tuple:
    """
    Update running_low (RESET ke titik terendah TERBARU tiap kali ada low
    lebih dalam -- live tidak bisa tahu titik terendah "sebenarnya" di
    muka spt backtest hindsight, jadi dinamis, bukan dikunci sekali).
    Return (running_low_baru, [(tier, fire_price), ...] utk tier yg BARU
    fire sejak panggilan ini -- first-touch PER TIER, independen).
    """
    if bars.empty:
        return running_low, []
    lows = bars["Low"].astype(float)
    highs = bars["High"].astype(float)
    new_low = min(running_low, float(lows.min())) if running_low is not None else float(lows.min())
    newly_fired = []
    for tier in GAP_REBOUND_TIERS:
        if tier in tiers_fired:
            continue
        target_price = new_low * (1 + tier / 100.0)
        if float(highs.max()) >= target_price:
            newly_fired.append((tier, target_price))
    return new_low, newly_fired


def _build_gap_rebound_message(ticker: str, tier: float, fire_price: float, running_low: float,
                                day_open: float, gap_pct: float, danger_tag: str | None = None) -> str:
    dip_pct = (running_low - day_open) / day_open * 100
    tp1 = round(fire_price * (1 + GAP_REBOUND_TP1_PCT / 100.0))
    tp2 = round(fire_price * (1 + GAP_REBOUND_TP2_PCT / 100.0))
    sl = round(fire_price * (1 + GAP_REBOUND_SL_PCT / 100.0))
    danger_line = f"\n{danger_tag}" if danger_tag else ""
    return (
        f"🔥 {ticker} REBOUND +{tier:.1f}% dari dip — entry {fire_price:,.0f}\n"
        f"Gap open +{gap_pct:.1f}% (open {day_open:,.0f}), sempat dip {dip_pct:+.1f}% ke {running_low:,.0f}\n"
        f"TP1 {tp1:,.0f} (+{GAP_REBOUND_TP1_PCT:.0f}%) | TP2 {tp2:,.0f} (+{GAP_REBOUND_TP2_PCT:.0f}%) — max {GAP_REBOUND_MAX_HOLD_MIN} menit\n"
        f"SL {sl:,.0f} ({GAP_REBOUND_SL_PCT:+.1f}%)\n"
        f"Historis tier {tier:.1f}%: closing median positif, hit-rate ~{'68.9%' if tier==0.5 else '64.4%' if tier==1.0 else '60.9%' if tier==1.5 else '59.8%'} (n=107-122, backtest 27hr bursa){danger_line}"
    )


async def run_gap_rebound_scan_once() -> dict:
    """
    One-shot CLI (`python bot.py --scanalert-rebound`), DIPISAH dari
    run_scan_alert_once() -- lihat catatan konstanta GAP_REBOUND_* di atas
    utk alasan (cadence 1 menit khusus jendela 09:00-09:10, state file
    sendiri hindari race condition baca-ubah-tulis bareng --scanalert utama).
    """
    summary = {"skipped_reason": None, "scanned": 0, "rebound_sent": 0}
    now_wib = datetime.datetime.now(core.WIB)
    if now_wib.weekday() >= 5:
        summary["skipped_reason"] = "weekend"
        return summary
    if not (GAP_REBOUND_SCAN_WINDOW_START <= now_wib.time() <= GAP_REBOUND_SCAN_WINDOW_END):
        summary["skipped_reason"] = "outside_rebound_window"
        return summary
    if await asyncio.to_thread(core.is_idx_market_holiday_today):
        summary["skipped_reason"] = "holiday"
        return summary
    if not is_scan_alert_enabled():
        summary["skipped_reason"] = "toggled_off"
        return summary

    state = _ensure_rebound_daily_reset(_load_rebound_state())
    universe = _get_alert_universe()
    if not universe:
        summary["skipped_reason"] = "no_universe"
        _save_rebound_state(state)
        return summary

    if state.get("daily_ref") is None:
        daily_ref = await asyncio.to_thread(_fetch_daily_ref, universe)
        state["daily_ref"] = daily_ref
    else:
        daily_ref = state["daily_ref"]

    if state.get("danger_lookup") is None:
        import engine.nightly as nightly_engine
        backbone_result, _ = await asyncio.to_thread(nightly_engine.load_backbone_daily_allow_stale)
        all_scored = (backbone_result or {}).get("all_scored", {}) or {}
        state["danger_lookup"] = {
            t: {"predicted_danger": info.get("predicted_danger"), "passed_danger_gate": info.get("passed_danger_gate")}
            for t, info in all_scored.items()
        }
    danger_lookup = state["danger_lookup"]

    ticker_list = list(daily_ref.keys())
    data = await asyncio.to_thread(_fetch_today_1m, ticker_list)

    tickers_state = state.setdefault("tickers", {})
    bot = None
    if core.TELEGRAM_BOT_TOKEN:
        import telegram
        bot = telegram.Bot(token=core.TELEGRAM_BOT_TOKEN)

    for t in ticker_list:
        ref = daily_ref.get(t)
        if ref is None:
            continue
        prev_close = ref["prev_close"]
        sym = t + ".JK"
        try:
            bars = data[sym].dropna(how="all").sort_index()
        except Exception:
            continue
        if bars.empty:
            continue
        summary["scanned"] += 1

        day_open = float(bars["Open"].astype(float).iloc[0])
        if day_open <= 0:
            continue
        gap_pct = (day_open - prev_close) / prev_close * 100
        if not (GAP_REBOUND_MIN_PCT <= gap_pct < GAP_REBOUND_MAX_PCT):
            continue

        t_state = tickers_state.setdefault(t, {"running_low": day_open, "tiers_fired": [], "done": False})
        if t_state.get("done"):
            continue

        detect_bars = bars.iloc[:GAP_REBOUND_DETECT_WINDOW_MIN + 1]
        new_low, newly_fired = _detect_gap_rebound_tiers(detect_bars, day_open, t_state["running_low"], t_state["tiers_fired"])
        t_state["running_low"] = new_low

        for tier, fire_price in newly_fired:
            t_state["tiers_fired"].append(tier)
            danger_tag = _danger_gate_tag(t, danger_lookup)
            msg = _build_gap_rebound_message(t, tier, fire_price, new_low, day_open, gap_pct, danger_tag)
            if bot is not None:
                await core.safe_reply(bot, msg, chat_id=core.TELEGRAM_CHAT_ID)
            else:
                print(f"[NO TELEGRAM TOKEN] {msg}")
            summary["rebound_sent"] += 1
            if tier >= max(GAP_REBOUND_TIERS):
                t_state["done"] = True

    _save_rebound_state(state)
    print(f"✅ Gap-rebound scan selesai: {summary['scanned']} ticker discan, {summary['rebound_sent']} alert REBOUND terkirim.")
    return summary


def _fetch_daily_ref(tickers: list[str]) -> dict:
    """
    Fetch closing kemarin (prev_close, dasar semua gain% + filter 60-600),
    ret_3d (3 hari bursa sebelum kemarin, utk tag "sudah lari kencang"), dan
    avg_value_traded_20d (MBSS v2, user request 2026-08-26 -- live case
    SAPX/WAPO: fire Alert A/B tapi rata-rata value traded 20 hari cuma
    Rp92-657 juta, JAUH di bawah floor likuiditas Rp1M yg dipakai Danger
    Gate/HC/SDT di seluruh sistem lain. Alert A/B/gap-up TIDAK PERNAH cek
    likuiditas -- cuma filter harga 60-600 -- jadi saham tipis lolos begitu
    saja. BUKAN exclude/filter di sini (scalping thin-liquid stock TETAP
    valid dipantau, cuma risikonya beda: slippage/spread lebih lebar,
    lebih rentan digerakkan modal kecil) -- ditandai sbg tag informational
    di pesan, lihat _risk_tags). period diperpanjang 10d->20d supaya
    rata-ratanya representatif, bukan cuma beberapa hari.
    """
    symbols = [t + ".JK" for t in tickers]
    data = yf.download(symbols, period="20d", interval="1d", group_by="ticker", threads=True, progress=False)
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
        volumes = d["Volume"].reindex(closes.index).fillna(0).astype(float)
        value_traded = (closes * volumes).tail(20)
        avg_value_traded_20d = float(value_traded.mean()) if len(value_traded) else None
        ref[t] = {"prev_close": prev_close, "ret_3d": ret_3d, "avg_value_traded_20d": avg_value_traded_20d}
    return ref


# MBSS v2 (user request 2026-08-27 -- riset BSJP buy-power backtest 2 tahun
# 576 ISSI: rasio volume hari ARA vs rata2 20 hari adalah prediktor
# terbaik, titik efektif >=10x [median high besok naik dari ~2-5% ke
# 6-9%, gap-positif 68-69% vs 62-64%]): monitor live BSJP-ARA (sleeper,
# engine/nightly.py load_bsjp_ara_candidates) + second-wave (load_second_
# wave_watch_for_today) utk rasio volume PACE-ADJUSTED (skala waktu
# sesi berjalan, BUKAN rasio end-of-day yg divalidasi -- ekstrapolasi
# beralasan, sama spirit dgn Alert B's ARA_TP -- info di message).
BUY_POWER_STRONG_VOL_RATIO = 10.0
BSJP_TYPICAL_SESSION_MINUTES = 330  # sesi IDX S1+S2 penuh, sama konvensi fetch_opening_dynamics


def _fetch_volume_ref(tickers: list[str]) -> dict:
    """
    Sama pola dgn _fetch_daily_ref, TAPI TANPA filter harga 60-600 -- BSJP-
    ARA/second-wave TIDAK dibatasi band harga scanalert biasa (BSJP-ARA
    sendiri pakai batas Rp1000, second-wave malah tanpa batas harga sama
    sekali). Cuma butuh avg_vol_20d (volume LEMBAR, bukan value rupiah --
    beda dari _fetch_daily_ref's avg_value_traded_20d) utk hitung pace-
    adjusted volume ratio.
    """
    symbols = [t + ".JK" for t in tickers]
    data = yf.download(symbols, period="20d", interval="1d", group_by="ticker", threads=True, progress=False)
    ref = {}
    for t in tickers:
        sym = t + ".JK"
        try:
            d = data[sym].dropna(how="all")
        except Exception:
            continue
        volumes = d["Volume"].dropna().astype(float)
        if len(volumes) < 2:
            continue
        ref[t] = {"avg_vol_20d": float(volumes.tail(20).mean())}
    return ref


def _detect_buy_power_surge(bars: pd.DataFrame, avg_vol_20d: float | None) -> dict | None:
    """
    First-touch: pace-adjusted volume ratio (volume hari ini SEJAUH INI vs
    ekspektasi pace normal) >= BUY_POWER_STRONG_VOL_RATIO. Pace pakai
    len(bars) sbg proksi menit sesi berjalan -- bar 1m yfinance sudah
    otomatis skip jeda istirahat 12:00-13:30 (dikonfirmasi dari data riil),
    jadi hitungan baris = menit sesi genuine, bukan wall-clock.
    """
    if bars.empty or not avg_vol_20d or avg_vol_20d <= 0:
        return None
    minutes_elapsed = max(5, len(bars))
    expected_vol_so_far = avg_vol_20d * (minutes_elapsed / BSJP_TYPICAL_SESSION_MINUTES)
    if expected_vol_so_far <= 0:
        return None
    volume_so_far = float(bars["Volume"].fillna(0).astype(float).sum())
    vol_pace_ratio = volume_so_far / expected_vol_so_far
    if vol_pace_ratio < BUY_POWER_STRONG_VOL_RATIO:
        return None
    current_price = float(bars["Close"].astype(float).iloc[-1])
    return {"vol_pace_ratio": vol_pace_ratio, "volume_so_far": volume_so_far, "current_price": current_price}


def _build_buy_power_surge_message(ticker: str, detection: dict, source: str, extra: dict) -> str:
    if source == "bsjp_ara":
        stats = "sleeper+katalis, historis gap-positif besok ~69.5%, median high +4.9% dari open (n=2027 episode ARA-like, 2 tahun)"
        source_label = f"BSJP-ARA sleeper (katalis: {extra.get('catalyst_category', '-')})"
    else:
        stats = "gelombang KEDUA (pernah ARA {:.0f}% dlm 10hr terakhir) -- historis LEBIH LEMAH dari sleeper murni: gap-positif besok ~57.9%, median high +4.1% dari open".format(extra.get("max_ret_1d_pct_10d", 0))
        source_label = "BSJP reaktivasi (second-wave)"
    return (
        f"🔥 {ticker} BUY POWER KUAT — {source_label}\n"
        f"Volume pace {detection['vol_pace_ratio']:.1f}x normal | harga {detection['current_price']:,.0f}\n"
        f"{stats}\n"
        f"Exit guidance: jual dekat open/awal sesi BESOK, JANGAN tahan sampai closing — "
        f"median closing besok justru negatif dari open (giveback), makin besar volume hari ini makin besar giveback-nya."
    )


# MBSS v2 (user request 2026-08-27 -- live case: "248 Failed downloads" SEMUA
# ticker sekaligus gagal dlm 1x panggilan, pesan yfinance "possibly delisted"
# menyesatkan -- itu bukan 248 saham genuinely delisted serentak, itu cara
# yfinance melaporkan Yahoo memblokir/rate-limit SATU BATCH REQUEST secara
# keseluruhan [pola umum di server cloud spt GCP, reputasi IP shared]. Beda
# dari kegagalan PER-TICKER wajar [beberapa delisted, encer di antara
# ratusan] -- kegagalan TOTAL serentak ini genuinely retriable. core.
# yf_fetch_with_retry TIDAK dipakai di sini krn filter kata kuncinya ["too
# many requests"/"rate limit"] tidak match pesan "possibly delisted" yg
# muncul di kasus ini -- retry KHUSUS di sini: kalau hasil download KOSONG
# TOTAL (bukan per-ticker), tunggu singkat lalu coba SEKALI lagi sebelum
# menyerah (match filosofi max_retries=2 yg sudah ada, bukan retry tanpa
# batas).
_FETCH_1M_RETRY_DELAY_SEC = 15


def _fetch_today_1m(tickers: list[str]):
    symbols = [t + ".JK" for t in tickers]
    data = yf.download(symbols, period="1d", interval="1m", group_by="ticker", threads=True, progress=False)
    if data.empty and tickers:
        print(f"⚠️ Scan-alert: fetch bar 1m KOSONG TOTAL ({len(tickers)} ticker) -- kemungkinan Yahoo blokir batch sesaat, retry sekali dalam {_FETCH_1M_RETRY_DELAY_SEC}s...")
        time.sleep(_FETCH_1M_RETRY_DELAY_SEC)
        data = yf.download(symbols, period="1d", interval="1m", group_by="ticker", threads=True, progress=False)
        if data.empty:
            print("⚠️ Scan-alert: retry jg kosong total -- genuinely gagal siklus ini, coba lagi siklus berikutnya.")
    return data


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


def _build_gap_up_message(ticker: str, detection: dict, conviction: str = "", risk_tags: list[str] | None = None) -> str:
    hold_txt = "bertahan" if detection["holds"] else "belum jelas bertahan (sempat turun >3% dari open)"
    note = f"\n{detection['bucket_note']}" if detection["bucket_note"] else ""
    conviction_line = f"\n{conviction}" if conviction else ""
    risk_lines = "".join(f"\n{tag}" for tag in (risk_tags or []))
    return (
        f"🌅 {ticker} GAP-UP +{detection['gap_pct']:.1f}% di pembukaan ({hold_txt}) | open {detection['day_open']:,.0f}"
        f"{note}\nInformational — sample historis kecil, bukan sinyal beli.{conviction_line}{risk_lines}"
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


# MBSS v2 (user request 2026-08-26 — "defense mechanism chasing": live case
# beli di puncak Alert A/gap-up sebelum 09:30, turun dalam menjelang akhir
# sesi 1. Alert A/B/gap-up sifatnya MURNI teknikal intraday (spike/volume/
# pullback), TIDAK PERNAH dicek terhadap sinyal sistem yg py punya "dasar"
# (FCM/PRE-CROSS/CONTINUATION/HC, semua sudah divalidasi backtest 2 tahun
# 576 ISSI): kalau ticker yg sama JUGA lolos salah satu lane itu malam
# sebelumnya, ada alasan tambahan utk bertahan tunggu rebound; kalau tidak,
# itu genuinely spike telanjang tanpa dukungan apa pun -- user bisa lebih
# hati-hati/kurangi ukuran. Bukan filter/exclude (tidak mengubah kapan alert
# fire), murni informational tag ditempel di SEMUA alert teknikal.
def _conviction_tag(ticker: str, fcm_watchlist: dict, pre_continuation_watchlist: dict, hc_gap_watch_list: dict) -> str:
    if ticker in fcm_watchlist:
        w = fcm_watchlist[ticker]
        return f"✅ ADA SETUP: FRESH CROSS MOMENTUM ({w['cross_days_ago']}hr lalu, pre-cross +{w['ret10_pre_cross_pct']:.0f}%)"
    if ticker in pre_continuation_watchlist:
        w = pre_continuation_watchlist[ticker]
        lane_label = {"PRE": "PRE-CROSS (SDT)", "CONTINUATION": "CONTINUATION (HC)", "VALIDATION": "VALIDATION (HC)"}.get(w["lane"], w["lane"])
        return f"✅ ADA SETUP: {lane_label}, {w['detail']}"
    if ticker in hc_gap_watch_list:
        return "✅ ADA SETUP: HC Minervini (diflag malam sebelumnya)"
    return "⚠️ NO SETUP — spike teknikal murni, tidak ada dukungan sinyal sistem (FCM/PRE/CONTINUATION/HC)"


def _danger_gate_tag(ticker: str, danger_lookup: dict) -> str | None:
    """
    MBSS v2 (user request 2026-08-26, live case NZIA: fire breakout kuat
    TAPI Danger Gate malam sebelumnya SUDAH menolaknya, danger=78/100 --
    red flag independen yg ada SEBELUM rally terjadi, bukan analisis
    belakangan). danger_lookup = backbone_daily's all_scored (ticker ->
    predicted_danger/passed_danger_gate), dimuat SEKALI per hari (lihat
    run_scan_alert_once) -- None-safe kalau backbone belum ada/ticker di
    luar cakupan malam itu (missing = tidak ditandai, BUKAN dianggap aman
    ATAU bahaya -- konsisten "missing=neutral" convention codebase ini).
    """
    info = danger_lookup.get(ticker)
    if not info or info.get("passed_danger_gate") is not False:
        return None
    danger = info.get("predicted_danger")
    danger_str = f"{danger:.0f}/100" if danger is not None else "-"
    return f"🧱 DITOLAK DANGER GATE malam sebelumnya (danger {danger_str}) — sistem sudah menandai berisiko SEBELUM rally/breakout ini terjadi"


def _risk_tags(bars: pd.DataFrame, ref: dict | None, now_wib: datetime.datetime,
                ticker: str = "", danger_lookup: dict | None = None) -> list[str]:
    """Tag informational (lihat CHASE_WARN_GAIN_FROM_OPEN_PCT dkk) -- tidak pernah menekan/menunda alert, cuma ditempel."""
    tags = []
    if danger_lookup:
        danger_tag = _danger_gate_tag(ticker, danger_lookup)
        if danger_tag:
            tags.append(danger_tag)
    if not bars.empty:
        day_open = float(bars["Open"].astype(float).iloc[0])
        current_price = float(bars["Close"].astype(float).iloc[-1])
        if day_open > 0:
            gain_from_open = (current_price - day_open) / day_open * 100
            if gain_from_open >= CHASE_WARN_GAIN_FROM_OPEN_PCT:
                tags.append(
                    f"⚠️ CHASE RISK: sudah +{gain_from_open:.1f}% dari open — di atas +{CHASE_WARN_GAIN_FROM_OPEN_PCT:.0f}% "
                    f"drawdown lanjutan historis melompat (median -6% s/d -10% ke closing sesi)"
                )
    if now_wib.time() < RISKY_TIME_WINDOW_END:
        tags.append(
            f"⚠️ JAM BERISIKO: fire sebelum {RISKY_TIME_WINDOW_END.strftime('%H:%M')} — "
            f"historis 44% kasus turun >=3% & 22% turun >=5% dlm 60 menit (vs ~15%/5% di jam lain)"
        )
    if ref is not None:
        avg_vt = ref.get("avg_value_traded_20d")
        if avg_vt is not None and avg_vt < SCANALERT_LIQUIDITY_WARN_FLOOR_IDR:
            tags.append(
                f"⚠️ LIKUIDITAS TIPIS: avg value traded 20hr Rp{avg_vt/1e9:.2f}M — "
                f"di bawah floor Rp{SCANALERT_LIQUIDITY_WARN_FLOOR_IDR/1e9:.2f}M scalping, risiko slippage/spread lebih besar"
            )
    return tags


def _build_alert_a_message(ticker: str, detection: dict, ret_3d: float | None,
                            current_price: float | None, vwap: float | None,
                            conviction: str = "", risk_tags: list[str] | None = None) -> str:
    tag = " ⚠️lari kencang" if ret_3d is not None and ret_3d >= RET_3D_WARN_THRESHOLD else ""
    conviction_line = f"\n{conviction}" if conviction else ""
    risk_lines = "".join(f"\n{t}" for t in (risk_tags or []))
    return (
        f"⚡ {ticker} +{detection['spike_pct']:.1f}% | vol {detection['volume_ratio']:.1f}x | "
        f"{detection['time']}{tag}{_vwap_segment(current_price, vwap)} | amati{conviction_line}{risk_lines}"
    )


def _build_alert_b_messages(ticker: str, detection: dict, ret_3d: float | None,
                             orderbook_check: dict | None,
                             current_price: float | None, vwap: float | None,
                             macd_label: str | None = None, tp1: float | None = None,
                             tp2: float | None = None, ara_price: float | None = None,
                             buy_power: dict | None = None, conviction: str = "",
                             risk_tags: list[str] | None = None) -> list[str]:
    tag = " ⚠️lari kencang" if ret_3d is not None and ret_3d >= RET_3D_WARN_THRESHOLD else ""
    messages = [
        f"✅ {ticker} PULLBACK REBOUND dari +{detection['peak_tier']}% | "
        f"skrg {detection['gain_at_rebound_pct']:+.1f}% | {detection['rebound_time']}"
        f"{tag}{_vwap_segment(current_price, vwap)} | entry candidate"
    ]
    if conviction:
        messages.append(conviction)
    for t in (risk_tags or []):
        messages.append(t)
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

# MBSS v2 (user request 2026-08-26 — riset entry-timing FCM/PRE/CONTINUATION/
# HC, backtest 2 tahun 576 ISSI raw OHLC, lihat chat sesi ini): FCM ternyata
# entry TERBAIK justru di H+1 Open langsung (bukan tunggu konfirmasi/pullback
# spt sebelumnya) -- kecepatan menang krn momentumnya SUDAH terbukti (ret10
# pre-cross>15%). Chase-tolerance dites (0-7% dari open): hit6/hit10 FLAT di
# semua level (49-53%), TIDAK ADA penalti kualitas sampai +3%, tapi fill-rate
# anjlok cepat (100%->56.5% di +2%->42.5% di +3%) -- jadi 2% dipilih sbg CAP
# BUKAN krn kualitas jatuh di atasnya, tapi krn di atas situ opportunity-cost
# (kandidat sudah lari duluan) lebih besar drpd manfaatnya. Alert ini
# TAMBAHAN, bukan pengganti pullback/confirmation-entry yg sudah ada (user
# keputusan eksplisit) -- utk yg belum sempat entry di open, dua alert lama
# itu masih jalan sbg sinyal susulan sepanjang hari.
FCM_OPEN_BUY_CHASE_CAP_PCT = 2.0
FCM_OPEN_BUY_WINDOW_END = datetime.time(9, 15)  # jendela "beli di open" -- di luar ini bukan lagi representasi entry-di-open yg valid

# MBSS v2 (user request 2026-08-26, riset sesi sama): PRE-CROSS (lane SDT
# FAST_RECOVERY/EARLY_RECOVERY/ABOVE_MOMENTUM) dan CONTINUATION (HC, sudah
# cross bullish <=5hr & gain_since_cross 6-10%) SAMA-SAMA entry lebih baik
# TUNGGU MENJELANG CLOSING (H+1 Close menang tipis tapi konsisten dari H+1
# Open di kedua lane), BUKAN buru-buru di open spt FCM -- karena keduanya
# masih fase "membangun/baru konfirmasi", closing yg lemah sering nyaring
# false-start. Proxy live: body candle H+1 hijau dari open HARI ITU -- scan
# mulai jam 14:00 (bukan dari open) supaya "hijau" yg terdeteksi genuinely
# representasi menjelang closing, bukan noise pagi. Volume H+1 utk lane ini
# INFORMATIONAL SAJA (bukan gate) -- volume TINGGI justru correlate ke
# stagnant_neg lebih besar (45.9%/40.9% di bucket >=2x) drpd volume rendah,
# kebalikan dari FCM/HC Minervini yg justru diuntungkan volume tinggi --
# user pilih TIDAK menggate di volume karena arahnya beda antar-lane &
# belum cukup robust utk dijadikan hard filter tambahan, cukup ditampilkan
# sbg info.
#
# Ambang 2.0 (MBSS v2, user request 2026-08-27 -- sweep >0%/>1%/>2% thd
# dataset sama): DITURUNKAN ke 1.0 -- penurunan hit6 dari >2% cuma -3.0pp
# (PRE 56.4%->53.4%) / -3.5pp (CONTINUATION 57.6%->54.1%), TAPI fire-rate
# naik ~30-35% (PRE n=163->219, CONTINUATION n=59->74) -- trade-off
# menguntungkan. >0% DIPERTIMBANGKAN TAPI DITOLAK: penurunan lebih terasa,
# khususnya CONTINUATION turun ke 48.9% (di bawah 50%, hampir coin-flip).
PRE_CONTINUATION_BODY_MIN_PCT = 1.0
PRE_CONTINUATION_SCAN_START = datetime.time(14, 0)
MACD_CONTINUATION_MAX_CROSS_DAYS_AGO = 5    # PERSIS commands/scan.py high_conviction_command -- jangan drift

# MBSS v2 (user request 2026-08-26): HC Minervini-8-kriteria di-HIDE dari
# /hc langsung (win rate historis rendah, hit6=33.0% vs 50%+ lane lain, lihat
# commands/scan.py high_conviction_command) -- SEBAGAI GANTI, dipantau live
# esok harinya: gap-up dari closing malam ini (saat HC pertama diflag) >=3%
# di pembukaan -> hit6=62.7% (n=51), jauh di atas baseline 33%. Watchlist
# datang dari engine/nightly.py save_hc_gap_watch/load_hc_gap_watch_for_today
# (overwrite tiap malam, HANYA valid utk H+1 -- window >H+1 belum
# tervalidasi, sengaja TIDAK dipantau lebih lama, lihat load_hc_gap_watch_
# for_today docstring). Definisi SENGAJA tidak mensyaratkan ticker tetap HC
# di hari+1 (dites, subset itu n=14 malah lebih rendah hit6=50%).
HC_GAP_WATCH_MIN_GAP_PCT = 3.0

# MBSS v2 (user request 2026-08-26 — "defense mechanism chasing", live case
# beli di puncak NZIA/dkk sebelum 09:30): 3 tag informational (BUKAN filter/
# suppress -- Alert A/B tetap fire seperti biasa, cuma ditempeli peringatan)
# ditempel ke Alert A/B/gap-up, ditemukan lewat backtest 1m riil 27 hari
# terakhir, 174 ticker gap-candidate (lihat chat sesi ini):
#   1) CHASE: gain dari open sudah >=3% saat alert fire -- di atas ambang
#      ini risiko drawdown lanjutan melompat tajam (rally 60menit <3% ->
#      median dd -5.17% ke EOD; 3-6% -> -7.76%; makin besar makin dalam).
#   2) JAM RISIKO: fire sebelum jam 09:30 -- 44% kasus turun >=3% dlm 60
#      menit (vs cuma ~15% di jam2 lain), 22% turun >=5% (vs ~5%). TAPI
#      TIDAK di-suppress/tunda -- 72% Alert A & 53% Alert B genuinely fire
#      SETELAH 09:30 juga, jadi window ini bukan cuma noise pembukaan.
#   3) LIKUIDITAS TIPIS: avg value traded 20d di bawah SCANALERT_LIQUIDITY_
#      WARN_FLOOR_IDR (live case SAPX Rp92jt/hari, WAPO Rp657jt/hari,
#      KEDUANYA fire Alert A/B). Ditest floor Rp1M (LIQUIDITY_FLOOR_VALUE_
#      TRADED_IDR, dipakai Danger Gate/HC/SDT) SEBAGAI HARD FILTER dulu:
#      akan membuang 40.9% fire Alert A dan 50.8% Alert B -- TERLALU
#      AGRESIF, jadi (a) tag saja bukan exclude, DAN (b) user pilih floor
#      lebih longgar KHUSUS scalping (Rp500jt, lihat SCANALERT_LIQUIDITY_
#      WARN_FLOOR_IDR di bawah) -- exposure scalping jauh lebih singkat
#      drpd day-trade/swing multi-hari yg dilindungi floor Rp1M itu.
CHASE_WARN_GAIN_FROM_OPEN_PCT = 3.0
RISKY_TIME_WINDOW_END = datetime.time(9, 30)

# MBSS v2 (user request 2026-08-26): floor likuiditas KHUSUS tag Alert A/B/
# gap-up, SENGAJA lebih longgar dari core.LIQUIDITY_FLOOR_VALUE_TRADED_IDR
# (Rp1M, dipakai Danger Gate/HC/SDT) -- scalping intraday genuinely lebih
# toleran thd saham lebih tipis drpd swing/day-trade multi-hari (exposure
# lebih singkat), jadi TIDAK reuse konstanta yang sama. Tidak mengubah
# core.LIQUIDITY_FLOOR_VALUE_TRADED_IDR sama sekali -- floor Rp1M di
# Danger Gate/HC/SDT tetap seperti semula.
SCANALERT_LIQUIDITY_WARN_FLOOR_IDR = 500_000_000


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
                "pct_b": r.get("pct_b"),  # MBSS v2 2026-08-27 -- fitur utk engine/lane_confidence.py (TP1/TP2 individual)
            }
    return watchlist


def _get_pre_continuation_watchlist(universe: list[str]) -> dict:
    """
    Reuse PERSIS kriteria PRE-CROSS lane (macd_approach_tier, engine/
    scoring.py) dan CONTINUATION/VALIDATION (macd_cross_direction bullish,
    cross<=5hr, gain_since_cross 6-10%/3-6%, commands/scan.py high_
    conviction_command) -- dihitung ulang di sini via compute_factor_
    scoring, pola SAMA dgn _get_fresh_cross_momentum_watchlist di atas
    (independen dari command lain sudah jalan atau belum hari ini).

    MBSS v2 (user request 2026-08-27, susun conviction sweep): CONTINUATION
    & VALIDATION digate dist_to_sma20>=MACD_LANE_DIST_SMA20_MIN_PCT, PERSIS
    commands/scan.py high_conviction_command -- sebelumnya CONTINUATION di
    fungsi ini TIDAK pakai gate ini (drift dari /hc), dan VALIDATION sama
    sekali tidak ditandai di sini (celah, /hc sudah pakai gate dist_to_
    sma20 sejak SCORING_FORMULA_VERSION 3.17.21 tapi watchlist live intraday
    ini belum ikut) -- disamakan sekarang supaya universe conviction sweep
    match persis kriteria /hc malam sebelumnya.
    """
    from engine import scoring  # import lokal, sama alasan spt FCM watchlist di atas
    MACD_LANE_DIST_SMA20_MIN_PCT = 12.0  # PERSIS commands/scan.py & engine/scoring.py -- jangan drift
    watchlist = {}
    for t in universe:
        try:
            r = scoring.compute_factor_scoring(t, include_quote_check=False)
        except Exception:
            continue
        if not r:
            continue
        tier = r.get("macd_approach_tier")
        if tier in ("FAST_RECOVERY", "EARLY_RECOVERY", "ABOVE_MOMENTUM"):
            watchlist[t] = {
                "lane": "PRE", "detail": tier,
                "dist_to_sma20": r.get("price_vs_sma20_pct"), "pct_b": r.get("pct_b"),
            }
            continue
        sma_dist = r.get("price_vs_sma20_pct")
        if sma_dist is None or sma_dist < MACD_LANE_DIST_SMA20_MIN_PCT:
            continue
        gain = r.get("macd_gain_since_cross_pct")
        if (
            r.get("macd_cross_direction") != "bullish"
            or r.get("macd_cross_days_ago") is None
            or r["macd_cross_days_ago"] > MACD_CONTINUATION_MAX_CROSS_DAYS_AGO
            or gain is None
        ):
            continue
        if 6.0 <= gain < 10.0:
            watchlist[t] = {
                "lane": "CONTINUATION", "detail": f"+{gain:.1f}% sejak cross",
                "dist_to_sma20": sma_dist, "pct_b": r.get("pct_b"),
            }
        elif 3.0 <= gain < 6.0:
            watchlist[t] = {
                "lane": "VALIDATION", "detail": f"+{gain:.1f}% sejak cross",
                "dist_to_sma20": sma_dist, "pct_b": r.get("pct_b"),
            }
    return watchlist


def _detect_h1_strong_body(bars: pd.DataFrame) -> dict | None:
    """
    Body candle hari-berjalan hijau kuat >=2% dari open -- proxy live utk
    "H+1 body kuat" yg tervalidasi (lihat PRE_CONTINUATION_BODY_MIN_PCT).
    Dipanggil HANYA sesudah jam 14:00 (dicek oleh caller) supaya representasi
    menjelang closing, bukan noise pagi. First-touch (bukan snapshot
    terakhir) -- begitu gain dari open PERNAH tembus ambang hari ini.
    """
    if bars.empty:
        return None
    day_open = float(bars["Open"].astype(float).iloc[0])
    if day_open <= 0:
        return None
    current_price = float(bars["Close"].astype(float).iloc[-1])
    highest_so_far = float(bars["High"].astype(float).max())
    gain_pct = (highest_so_far - day_open) / day_open * 100
    if gain_pct < PRE_CONTINUATION_BODY_MIN_PCT:
        return None
    vol_so_far = float(bars["Volume"].fillna(0).astype(float).sum())
    return {"gain_pct": gain_pct, "current_price": current_price, "day_open": day_open, "vol_so_far": vol_so_far}


# MBSS v2 (user request 2026-08-27 -- TP1/TP2 individual per ticker,
# gantikan WR rata-rata grup statis, dipakai di SEMUA alert scan-alert yg
# bertag lane MACD): watchlist_entry["tp_info"] (kalau ada, diisi sekali/
# hari di run_scan_alert_once/run_conviction_sweep_once via engine/
# lane_confidence.py) -> baris "TP1 xxx (WR yy%)"/"(Tercapai!)". None kalau
# lane tak didukung (FAST_RECOVERY/EARLY_RECOVERY) atau ref_price tak ada
# -- caller HARUS fallback ke teks statis lama di kasus itu.
def _tp_lines_suffix(watchlist_entry: dict, current_price: float | None) -> str:
    tp_info = watchlist_entry.get("tp_info")
    if not tp_info:
        return ""
    return "\n" + "\n".join(lane_confidence.format_tp_lines(tp_info, current_price=current_price))


def _build_h1_strong_body_message(ticker: str, detection: dict, watchlist_entry: dict) -> str:
    lane, detail = watchlist_entry["lane"], watchlist_entry["detail"]
    lane_label = {"PRE": "PRE-CROSS (SDT)", "CONTINUATION": "CONTINUATION (HC)", "VALIDATION": "VALIDATION (HC)"}.get(lane, lane)
    tp_suffix = _tp_lines_suffix(watchlist_entry, detection["current_price"])
    if tp_suffix:
        hist_line = f"Confidence individual (dist_to_sma20/pct_b ticker ini, bukan rata-rata grup):{tp_suffix}"
    else:
        hist_label = {"PRE": "PRE hit6~50-56%", "CONTINUATION": "CONTINUATION hit6~60.2%", "VALIDATION": "VALIDATION hit6~57.6%"}.get(lane, "-")
        hist_line = f"Historis: {hist_label} (+dist_to_sma20>=12%) — verifikasi live sebelum entry."
    return (
        f"📈 {ticker} MENJELANG CLOSING — {lane_label}, {detail}\n"
        f"Body hijau +{detection['gain_pct']:.1f}% dari open ({detection['day_open']:,.0f}) — skrg {detection['current_price']:,.0f}\n"
        f"Volume hari ini: {detection['vol_so_far']:,.0f} lembar (informational, bukan gate)\n"
        f"{hist_line}"
    )


def _detect_hc_gap_watch(bars: pd.DataFrame, prev_close: float) -> dict | None:
    """Gap-up di pembukaan dari closing malam saat pertama diflag HC (lihat HC_GAP_WATCH_MIN_GAP_PCT)."""
    if bars.empty or not prev_close or prev_close <= 0:
        return None
    day_open = float(bars["Open"].astype(float).iloc[0])
    if day_open <= 0:
        return None
    gap_pct = (day_open - prev_close) / prev_close * 100
    if gap_pct < HC_GAP_WATCH_MIN_GAP_PCT:
        return None
    return {"gap_pct": gap_pct, "day_open": day_open, "prev_close": prev_close}


def _build_hc_gap_watch_message(ticker: str, detection: dict) -> str:
    return (
        f"🔥 {ticker} HC GAP-UP +{detection['gap_pct']:.1f}% dari closing malam HC diflag ({detection['prev_close']:,.0f}) — buka {detection['day_open']:,.0f}\n"
        f"Historis: gap>=3% pasca-HC-Minervini hit6~62.7% (n=51, backtest 2 tahun) — jauh di atas baseline HC 33% tanpa filter ini.\n"
        f"Cek 1x, tidak dipantau lagi hari berikutnya kalau tidak fire hari ini."
    )


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
    tp_suffix = _tp_lines_suffix(watchlist_entry, detection["current_price"])
    return (
        f"🔥 {ticker} PULLBACK ENTRY — FRESH CROSS MOMENTUM ({watchlist_entry['cross_days_ago']} hari lalu, "
        f"momentum pre-cross +{watchlist_entry['ret10_pre_cross_pct']:.1f}%)\n"
        f"Pullback {detection['pullback_pct']:.1f}% dari open ({detection['day_open']:,.0f}) — skrg {detection['current_price']:,.0f} | {zone_label}\n"
        f"Tervalidasi: median MAE trade menang -4.0%, p25 -9.0%, p10 -14.3% (n=635) — bukan sinyal beli otomatis, verifikasi live."
        f"{tp_suffix}"
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
    tp_suffix = _tp_lines_suffix(watchlist_entry, detection["current_price"])
    return (
        f"🚀 {ticker} CONFIRMATION ENTRY — FRESH CROSS MOMENTUM ({watchlist_entry['cross_days_ago']} hari lalu, "
        f"momentum pre-cross +{watchlist_entry['ret10_pre_cross_pct']:.1f}%)\n"
        f"Lanjut naik +{detection['gain_pct']:.1f}% dari open ({detection['day_open']:,.0f}) — skrg {detection['current_price']:,.0f}\n"
        f"Sinyal konfirmasi (bukan pullback) — historis kombinasi momentum+konfirmasi hari sama hit6~69% (konteks episode extended, ekstrapolasi ke FCM) — verifikasi live."
        f"{tp_suffix}"
    )


def _detect_open_buy(bars: pd.DataFrame) -> dict | None:
    """
    FCM: entry di H+1 Open langsung (lihat FCM_OPEN_BUY_CHASE_CAP_PCT
    docstring). None kalau gain dari open SUDAH >=cap -- jangan kejar,
    momen sudah lewat (caller jg membatasi jendela waktu via
    FCM_OPEN_BUY_WINDOW_END, dua lapis proteksi supaya tidak fire "beli di
    open" yg sebenarnya sudah siang hari).
    """
    if bars.empty:
        return None
    day_open = float(bars["Open"].astype(float).iloc[0])
    if day_open <= 0:
        return None
    current_price = float(bars["Close"].astype(float).iloc[-1])
    gain_from_open = (current_price - day_open) / day_open * 100
    if gain_from_open >= FCM_OPEN_BUY_CHASE_CAP_PCT:
        return None
    return {"day_open": day_open, "current_price": current_price, "gain_from_open": gain_from_open}


def _build_open_buy_message(ticker: str, detection: dict, watchlist_entry: dict) -> str:
    tp_suffix = _tp_lines_suffix(watchlist_entry, detection["current_price"])
    return (
        f"🔔 {ticker} BELI DI OPEN — FRESH CROSS MOMENTUM ({watchlist_entry['cross_days_ago']} hari lalu, "
        f"momentum pre-cross +{watchlist_entry['ret10_pre_cross_pct']:.1f}%)\n"
        f"Open {detection['day_open']:,.0f} — skrg {detection['current_price']:,.0f} ({detection['gain_from_open']:+.1f}% dari open)\n"
        f"Riset: entry di open lebih baik dari nunggu konfirmasi/pullback (hit rate flat s/d +3% chase, tapi fill-rate anjlok di atas +2%) — "
        f"JANGAN KEJAR kalau sudah naik >{FCM_OPEN_BUY_CHASE_CAP_PCT:.0f}% dari open ini."
        f"{tp_suffix}"
    )


async def run_scan_alert_once() -> dict:
    """
    Satu kali scan penuh: fetch universe + data, deteksi Alert A/B per
    ticker, kirim ke Telegram (kalau ada & belum dikirim hari ini), simpan
    state. Return summary dict (utk logging CLI).
    """
    summary = {
        "skipped_reason": None, "alert_a_sent": 0, "alert_b_sent": 0, "scanned": 0, "excluded_no_room": 0,
        "gap_up_sent": 0, "pullback_entry_sent": 0, "open_buy_sent": 0, "pre_continuation_sent": 0, "hc_gap_sent": 0,
        "buy_power_sent": 0,
    }

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

    if state.get("pre_continuation_watchlist") is None:
        print(f"📡 Scan-alert: hitung watchlist PRE-CROSS/CONTINUATION utk {len(universe)} ticker...")
        pre_continuation_watchlist = await asyncio.to_thread(_get_pre_continuation_watchlist, universe)
        state["pre_continuation_watchlist"] = pre_continuation_watchlist
        print(f"✅ PRE-CROSS/CONTINUATION watchlist hari ini: {len(pre_continuation_watchlist)} ticker.")
    else:
        pre_continuation_watchlist = state["pre_continuation_watchlist"]

    # MBSS v2 (user request 2026-08-27 -- TP1/TP2 individual per ticker):
    # hitung SEKALI/hari (bukan tiap siklus 3 menit), simpan tp_info di tiap
    # entry watchlist -- lane FAST_RECOVERY/EARLY_RECOVERY (belum didukung
    # lane_confidence) & ticker tanpa ref_price dibiarkan tanpa tp_info,
    # caller fallback ke teks statis lama. Suppression: ticker FCM/ABOVE_
    # MOMENTUM/CONTINUATION/VALIDATION dgn confidence individual <50% di
    # level terdekat DIBUANG dari watchlist (tidak diproduksi jadi sinyal
    # sama sekali) -- bukan cuma disembunyikan angkanya.
    if state.get("lane_tp_computed") is not True:
        lane_tickers_needing_ref = [
            t for t in (set(fcm_watchlist) | set(pre_continuation_watchlist)) if t not in daily_ref
        ]
        extra_ref = await asyncio.to_thread(_fetch_conviction_ref, lane_tickers_needing_ref) if lane_tickers_needing_ref else {}

        def _lane_ref_price(t):
            if t in daily_ref:
                return daily_ref[t]["prev_close"]
            return extra_ref.get(t)

        n_suppressed = 0
        for t, entry in list(fcm_watchlist.items()):
            ref_price = _lane_ref_price(t)
            tp_info = lane_confidence.compute_tp1_tp2(
                "FCM", {"ret10_pre_cross_pct": entry.get("ret10_pre_cross_pct"), "pct_b": entry.get("pct_b")}, ref_price
            ) if ref_price else None
            if tp_info is None:
                del fcm_watchlist[t]
                n_suppressed += 1
            else:
                entry["tp_info"] = tp_info

        for t, entry in list(pre_continuation_watchlist.items()):
            lane_tag = entry["detail"] if entry["lane"] == "PRE" else entry["lane"]
            if lane_tag not in lane_confidence.SUPPORTED_LANES:
                continue  # FAST_RECOVERY/EARLY_RECOVERY -- tetap tanpa tp_info, fallback statis
            ref_price = _lane_ref_price(t)
            tp_info = lane_confidence.compute_tp1_tp2(
                lane_tag, {"dist_to_sma20": entry.get("dist_to_sma20"), "pct_b": entry.get("pct_b")}, ref_price
            ) if ref_price else None
            if tp_info is None:
                del pre_continuation_watchlist[t]
                n_suppressed += 1
            else:
                entry["tp_info"] = tp_info

        state["fresh_cross_momentum_watchlist"] = fcm_watchlist
        state["pre_continuation_watchlist"] = pre_continuation_watchlist
        state["lane_tp_computed"] = True
        print(f"✅ Confidence individual (TP1/TP2) dihitung -- {n_suppressed} ticker di-suppress (WR<50% di level terdekat).")

    if state.get("hc_gap_watch_list") is None:
        import engine.nightly as nightly_engine  # import lokal -- hindari circular import di level modul
        hc_watch_rows = await asyncio.to_thread(nightly_engine.load_hc_gap_watch_for_today)
        hc_gap_watch_list = {row["ticker"]: row["prev_close"] for row in hc_watch_rows if row.get("prev_close")}
        state["hc_gap_watch_list"] = hc_gap_watch_list
        print(f"✅ HC gap-watch hari ini: {len(hc_gap_watch_list)} ticker (dari HC Minervini malam kemarin).")
    else:
        hc_gap_watch_list = state["hc_gap_watch_list"]

    # MBSS v2 (user request 2026-08-26, live case NZIA: Alert B fire kuat tapi
    # Danger Gate malam sebelumnya SUDAH menolaknya) -- baca predicted_danger/
    # passed_danger_gate dari backbone_daily malam terakhir (bukan hitung
    # ulang), disederhanakan ke {ticker: {predicted_danger, passed_danger_gate}}
    # SAJA (bukan simpan all_scored utuh) supaya aman di-JSON-kan ke state
    # file & tidak membengkakkan ukurannya.
    if state.get("danger_lookup") is None:
        import engine.nightly as nightly_engine
        backbone_result, _ = await asyncio.to_thread(nightly_engine.load_backbone_daily_allow_stale)
        all_scored = (backbone_result or {}).get("all_scored", {}) or {}
        danger_lookup = {
            t: {"predicted_danger": info.get("predicted_danger"), "passed_danger_gate": info.get("passed_danger_gate")}
            for t, info in all_scored.items()
        }
        state["danger_lookup"] = danger_lookup
        n_rejected = sum(1 for v in danger_lookup.values() if v.get("passed_danger_gate") is False)
        print(f"✅ Danger Gate lookup hari ini: {len(danger_lookup)} ticker ({n_rejected} ditolak malam kemarin).")
    else:
        danger_lookup = state["danger_lookup"]

    # MBSS v2 (user request 2026-08-27 -- BSJP buy-power monitor): BSJP-ARA
    # (sleeper+katalis, sudah dihitung nightly) + second-wave (reaktivasi,
    # sudah dihitung nightly) digabung jadi satu lookup {ticker: source_info}
    # -- keduanya dipantau pace volume live yg SAMA (_detect_buy_power_surge),
    # bedanya cuma pesan/statistik yg ditampilkan.
    if state.get("bsjp_watch_lookup") is None:
        import engine.nightly as nightly_engine
        bsjp_ara_rows = await asyncio.to_thread(nightly_engine.load_bsjp_ara_candidates)
        second_wave_rows = await asyncio.to_thread(nightly_engine.load_second_wave_watch_for_today)
        bsjp_watch_lookup = {}
        for row in bsjp_ara_rows:
            if row.get("ticker"):
                bsjp_watch_lookup[row["ticker"]] = {"source": "bsjp_ara", "catalyst_category": row.get("catalyst_category")}
        for row in second_wave_rows:
            if row.get("ticker") and row["ticker"] not in bsjp_watch_lookup:  # BSJP-ARA prioritas kalau ticker sama kebetulan masuk dua-duanya
                bsjp_watch_lookup[row["ticker"]] = {"source": "second_wave", "max_ret_1d_pct_10d": row.get("max_ret_1d_pct_10d")}
        state["bsjp_watch_lookup"] = bsjp_watch_lookup
        if bsjp_watch_lookup:
            vol_ref = await asyncio.to_thread(_fetch_volume_ref, list(bsjp_watch_lookup.keys()))
            state["bsjp_vol_ref"] = vol_ref
        else:
            state["bsjp_vol_ref"] = {}
        print(f"✅ BSJP watch hari ini: {len(bsjp_ara_rows)} BSJP-ARA + {len(second_wave_rows)} second-wave = {len(bsjp_watch_lookup)} ticker dipantau buy-power.")
    else:
        bsjp_watch_lookup = state["bsjp_watch_lookup"]
    bsjp_vol_ref = state.get("bsjp_vol_ref", {})

    # Union -- FCM/PRE-CONTINUATION/HC-gap-watch/BSJP-watch BISA di luar band
    # harga 60-600 (tidak ada floor harga di definisi masing-masing), jadi
    # tidak selalu subset alert_universe.
    alert_universe = list(daily_ref.keys())
    full_ticker_set = sorted(
        set(alert_universe) | set(fcm_watchlist.keys()) | set(pre_continuation_watchlist.keys())
        | set(hc_gap_watch_list.keys()) | set(bsjp_watch_lookup.keys())
    )
    print(
        f"🔍 Scan-alert: {len(full_ticker_set)} ticker ({len(alert_universe)} alert + {len(fcm_watchlist)} FCM + "
        f"{len(pre_continuation_watchlist)} PRE/CONTINUATION + {len(hc_gap_watch_list)} HC gap-watch + "
        f"{len(bsjp_watch_lookup)} BSJP-watch), fetch bar 1m..."
    )
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
        t_state.setdefault("open_buy_sent", False)
        t_state.setdefault("pre_continuation_sent", False)
        t_state.setdefault("hc_gap_sent", False)
        t_state.setdefault("buy_power_sent", False)

        # BSJP-ARA (sleeper) / second-wave (reaktivasi): pace volume live
        # >=BUY_POWER_STRONG_VOL_RATIO -- jalan SEPANJANG hari (bukan cuma
        # jendela waktu tertentu spt FCM/PRE-CONTINUATION), krn buy-power
        # bisa muncul kapan saja & justru itu yg mau ditangkap SEDINI mungkin
        # (persis kasus EKAD, entry mid-day bukan di jam spesifik).
        if t in bsjp_watch_lookup and not t_state["buy_power_sent"]:
            watch_info = bsjp_watch_lookup[t]
            avg_vol_20d = (bsjp_vol_ref.get(t) or {}).get("avg_vol_20d")
            det_power = _detect_buy_power_surge(bars, avg_vol_20d)
            if det_power:
                msg = _build_buy_power_surge_message(t, det_power, watch_info["source"], watch_info)
                if bot is not None:
                    await core.safe_reply(bot, msg, chat_id=core.TELEGRAM_CHAT_ID)
                else:
                    print(f"[NO TELEGRAM TOKEN] {msg}")
                t_state["buy_power_sent"] = True
                summary["buy_power_sent"] = summary.get("buy_power_sent", 0) + 1

        # FCM: beli di open, jendela pendek (FCM_OPEN_BUY_WINDOW_END) --
        # dicek SEBELUM confirmation/pullback (independen, bisa dua-duanya
        # fire di hari yg sama: open-buy pagi ini, pullback/confirmation
        # susulan siang kalau belum sempat entry pagi).
        if t in fcm_watchlist and not t_state["open_buy_sent"] and now_wib.time() <= FCM_OPEN_BUY_WINDOW_END:
            det_open = _detect_open_buy(bars)
            if det_open:
                msg = _build_open_buy_message(t, det_open, fcm_watchlist[t])
                if bot is not None:
                    await core.safe_reply(bot, msg, chat_id=core.TELEGRAM_CHAT_ID)
                else:
                    print(f"[NO TELEGRAM TOKEN] {msg}")
                t_state["open_buy_sent"] = True
                summary["open_buy_sent"] += 1

        # PRE-CROSS / CONTINUATION: body hijau kuat >=2%, HANYA dicek mulai
        # jam 14:00 (PRE_CONTINUATION_SCAN_START) -- lihat konstanta di atas.
        if (
            t in pre_continuation_watchlist and not t_state["pre_continuation_sent"]
            and now_wib.time() >= PRE_CONTINUATION_SCAN_START
        ):
            det_body = _detect_h1_strong_body(bars)
            if det_body:
                msg = _build_h1_strong_body_message(t, det_body, pre_continuation_watchlist[t])
                if bot is not None:
                    await core.safe_reply(bot, msg, chat_id=core.TELEGRAM_CHAT_ID)
                else:
                    print(f"[NO TELEGRAM TOKEN] {msg}")
                t_state["pre_continuation_sent"] = True
                summary["pre_continuation_sent"] += 1

        # HC Minervini gap-watch -- ticker ini di-hide dari /hc malam
        # kemarin, dipantau SEKALI utk gap>=3% pagi ini (lihat
        # HC_GAP_WATCH_MIN_GAP_PCT).
        if t in hc_gap_watch_list and not t_state["hc_gap_sent"]:
            det_hc_gap = _detect_hc_gap_watch(bars, hc_gap_watch_list[t])
            if det_hc_gap:
                msg = _build_hc_gap_watch_message(t, det_hc_gap)
                if bot is not None:
                    await core.safe_reply(bot, msg, chat_id=core.TELEGRAM_CHAT_ID)
                else:
                    print(f"[NO TELEGRAM TOKEN] {msg}")
                t_state["hc_gap_sent"] = True
                summary["hc_gap_sent"] += 1

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
            continue  # ticker ini HANYA di fcm_watchlist/pre_continuation_watchlist/hc_gap_watch_list (di luar band 60-600) -- Alert A/B/gap-up di bawah butuh prev_close, sisanya di-skip

        conviction = _conviction_tag(t, fcm_watchlist, pre_continuation_watchlist, hc_gap_watch_list)
        risk_tags = _risk_tags(bars, ref, now_wib, ticker=t, danger_lookup=danger_lookup)

        if not t_state["gap_up_sent"]:
            det_gap = _detect_gap_up(bars, prev_close)
            if det_gap:
                msg = _build_gap_up_message(t, det_gap, conviction=conviction, risk_tags=risk_tags)
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

        if ALERT_A_B_PUSH_ENABLED and not t_state["alert_a_sent"]:
            det_a = _detect_alert_a(bars, prev_close)
            if det_a:
                msg = _build_alert_a_message(t, det_a, ret_3d, current_price, vwap_now, conviction=conviction, risk_tags=risk_tags)
                if bot is not None:
                    await core.safe_reply(bot, msg, chat_id=core.TELEGRAM_CHAT_ID)
                else:
                    print(f"[NO TELEGRAM TOKEN] {msg}")
                t_state["alert_a_sent"] = True
                summary["alert_a_sent"] += 1

        if ALERT_A_B_PUSH_ENABLED and not t_state["alert_b_sent"]:
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
                    conviction=conviction, risk_tags=risk_tags,
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
          f"{summary['open_buy_sent']} FCM open-buy, "
          f"{summary['pullback_entry_sent']} FCM pullback/confirmation, "
          f"{summary['pre_continuation_sent']} PRE/CONTINUATION menjelang closing, "
          f"{summary['hc_gap_sent']} HC gap-watch, "
          f"{summary['buy_power_sent']} BSJP buy-power surge, "
          f"{summary['excluded_no_room']} di-exclude (no room).")
    return summary


# ── CONVICTION SWEEP (mulai 10:00, tiap 15 menit, tiered) ───────────────────
# Lihat catatan konstanta CONVICTION_SWEEP_*/CONVICTION_TP_CEILING_PCT di atas.

def _load_conviction_state() -> dict:
    if not os.path.exists(STATE_FILE_CONVICTION):
        return {}
    try:
        with open(STATE_FILE_CONVICTION) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_conviction_state(state: dict):
    with open(STATE_FILE_CONVICTION, "w") as f:
        json.dump(state, f, indent=2)


def _ensure_conviction_daily_reset(state: dict) -> dict:
    today = _today_str()
    if state.get("trading_day_marker") != today:
        return {"trading_day_marker": today, "universe": None, "tickers": {}}
    return state


def _fetch_conviction_ref(tickers: list[str]) -> dict:
    """
    Closing kemarin (basis "estimasi max TP"), TANPA filter harga 60-600 --
    sama alasan dgn _fetch_volume_ref: universe conviction sweep (FCM/PRE/
    CONTINUATION/VALIDATION/HC-gap-watch/BSJP-watch) tidak dibatasi band
    harga scanalert biasa.
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
        if len(closes) < 1:
            continue
        ref[t] = float(closes.iloc[-1])
    return ref


def _build_conviction_universe_tags(main_state: dict) -> dict:
    """
    ticker -> {"tag": lane, "features": {...}}. features kosong ({}) utk
    lane yg tak didukung engine/lane_confidence.py (HC_GAP_WATCH/BSJP_ARA/
    BSJP_SECOND_WAVE/FAST_RECOVERY/EARLY_RECOVERY) -- caller fallback ke
    CONVICTION_TP_CEILING_PCT utk lane2 itu. Baca watchlist yg SUDAH di-
    cache run_scan_alert_once() di scanalert_state.json (lihat catatan
    CONVICTION_SWEEP_WINDOW_START) -- fallback hitung sendiri kalau state
    utama belum ada (mis. proses baru mulai persis jam 10:00, sebelum
    --scanalert utama sempat jalan).
    """
    tags = {}
    fcm = main_state.get("fresh_cross_momentum_watchlist")
    pre_cont = main_state.get("pre_continuation_watchlist")
    if fcm is None or pre_cont is None:
        alert_universe = _get_alert_universe()
        if fcm is None:
            fcm = _get_fresh_cross_momentum_watchlist(alert_universe)
        if pre_cont is None:
            pre_cont = _get_pre_continuation_watchlist(alert_universe)

    for t, w in (fcm or {}).items():
        tags[t] = {"tag": "FCM", "features": {"ret10_pre_cross_pct": w.get("ret10_pre_cross_pct"), "pct_b": w.get("pct_b")}}
    for t, w in (pre_cont or {}).items():
        if t in tags:
            continue
        lane_tag = w["detail"] if w["lane"] == "PRE" else w["lane"]  # PRE -> FAST_RECOVERY/EARLY_RECOVERY/ABOVE_MOMENTUM
        tags[t] = {"tag": lane_tag, "features": {"dist_to_sma20": w.get("dist_to_sma20"), "pct_b": w.get("pct_b")}}

    import engine.nightly as nightly_engine
    hc_gap_watch_list = main_state.get("hc_gap_watch_list")
    if hc_gap_watch_list is None:
        hc_watch_rows = nightly_engine.load_hc_gap_watch_for_today()
        hc_gap_watch_list = {row["ticker"]: row["prev_close"] for row in hc_watch_rows if row.get("prev_close")}
    for t in hc_gap_watch_list:
        if t not in tags:
            tags[t] = {"tag": "HC_GAP_WATCH", "features": {}}

    bsjp_watch_lookup = main_state.get("bsjp_watch_lookup")
    if bsjp_watch_lookup is None:
        bsjp_ara_rows = nightly_engine.load_bsjp_ara_candidates()
        second_wave_rows = nightly_engine.load_second_wave_watch_for_today()
        bsjp_watch_lookup = {}
        for row in bsjp_ara_rows:
            if row.get("ticker"):
                bsjp_watch_lookup[row["ticker"]] = {"source": "bsjp_ara"}
        for row in second_wave_rows:
            if row.get("ticker") and row["ticker"] not in bsjp_watch_lookup:
                bsjp_watch_lookup[row["ticker"]] = {"source": "second_wave"}
    for t, w in bsjp_watch_lookup.items():
        if t not in tags:
            tags[t] = {"tag": "BSJP_ARA" if w.get("source") == "bsjp_ara" else "BSJP_SECOND_WAVE", "features": {}}

    return tags


def _process_conviction_ticker(t_state: dict, current_price: float, ref_price: float, ceiling_price: float) -> list[tuple]:
    """
    Update t_state IN-PLACE, return list of event tuple ("tier", n) atau
    ("pullback_exception",) yg BARU terjadi sejak checkpoint sebelumnya.

    Logic: bandingkan current_price vs checkpoint 15-menit SEBELUMNYA
    (bukan vs harga pertama hari itu) -- "naik" = beruntun genuine staircase,
    bukan cuma "masih di atas pagi ini". consecutive_up reset ke 0 begitu
    ada checkpoint yg TIDAK naik (flat/turun) -- sesuai speks "kalau side,
    fading, falldown, tidak perlu fire".

    Suppression: begitu current_price >= ceiling_price, tier BARU dihentikan
    (room sudah exhausted relatif ceiling backtest) -- KECUALI pola pullback-
    lalu-swing-naik-lagi terdeteksi (current < session_high [pernah pullback
    dari titik tertinggi hari itu] DAN current > ref_price [masih di atas
    closing kemarin] DAN sedang on a fresh up-streak) -- fire SEKALI sbg
    "pullback re-entry", echo filosofi REBOUND.
    """
    events = []
    t_state["session_high"] = max(t_state.get("session_high", current_price), current_price)
    last_checkpoint = t_state.get("last_checkpoint_price")

    if last_checkpoint is None:
        t_state["last_checkpoint_price"] = current_price
        t_state["consecutive_up"] = 0
        return events

    if current_price > last_checkpoint:
        t_state["consecutive_up"] = t_state.get("consecutive_up", 0) + 1
    else:
        t_state["consecutive_up"] = 0
    t_state["last_checkpoint_price"] = current_price

    beyond_ceiling = current_price >= ceiling_price
    consecutive_up = t_state["consecutive_up"]

    if not beyond_ceiling:
        tiers_fired = t_state.get("tiers_fired", 0)
        potential_tier = consecutive_up // CONVICTION_SWEEP_CONSECUTIVE_UP_REQUIRED
        if potential_tier > tiers_fired and potential_tier <= CONVICTION_SWEEP_MAX_TIERS:
            t_state["tiers_fired"] = potential_tier
            events.append(("tier", potential_tier))
    elif (
        CONVICTION_SWEEP_PULLBACK_EXCEPTION_ENABLED
        and not t_state.get("pullback_exception_fired")
        and consecutive_up >= CONVICTION_SWEEP_CONSECUTIVE_UP_REQUIRED
        and t_state["session_high"] > current_price
        and current_price > ref_price
    ):
        t_state["pullback_exception_fired"] = True
        events.append(("pullback_exception",))

    return events


def _build_conviction_sweep_message(ticker: str, tag: str, tier: int, current_price: float,
                                     ref_price: float, ceiling_price: float, ceiling_pct: float,
                                     tp_info: dict | None = None) -> str:
    gain_from_ref = (current_price - ref_price) / ref_price * 100
    if tp_info:
        tp_line = "\n".join(lane_confidence.format_tp_lines(tp_info, current_price=current_price))
    else:
        tp_line = (f"Estimasi max TP {ceiling_price:,.0f} (+{ceiling_pct:.0f}%, ceiling backtest hit-rate>=50% dlm 5 hari bursa "
                   f"sejak kualifikasi lane -- BUKAN janji hari ini juga)")
    return (
        f"📈 {ticker} MOMENTUM MEMBANGUN (tier {tier}) — {tag}\n"
        f"Harga {current_price:,.0f} ({gain_from_ref:+.1f}% dari closing kemarin {ref_price:,.0f})\n"
        f"{tp_line}\n"
        f"Radar: 2x checkpoint 15-menit naik beruntun -- pola live, verifikasi sebelum entry."
    )


def _build_conviction_pullback_exception_message(ticker: str, tag: str, current_price: float,
                                                   ref_price: float, ceiling_price: float,
                                                   tp_info: dict | None = None) -> str:
    gain_from_ref = (current_price - ref_price) / ref_price * 100
    label = f"{tp_info['tp2_price']:,.0f}" if tp_info and "tp2_price" in tp_info else f"{ceiling_price:,.0f}"
    return (
        f"🔁 {ticker} PULLBACK RE-ENTRY (sudah lewat estimasi ceiling {label}) — {tag}\n"
        f"Harga {current_price:,.0f} ({gain_from_ref:+.1f}% dari closing kemarin {ref_price:,.0f}), sempat pullback dari puncak hari ini lalu naik lagi\n"
        f"⚠️ Room ke target awal sudah tipis/habis — ini radar reaktivasi, bukan target baru, verifikasi live."
    )


async def run_conviction_sweep_once() -> dict:
    """
    One-shot CLI (`python bot.py --conviction-sweep`), dipanggil via
    JobQueue tiap 15 menit (lihat build_app() engine/legacy_core.py).
    State terpisah dari scanalert_state.json (hindari race condition
    baca-ubah-tulis bareng --scanalert utama yg jalan tiap 3 menit).
    """
    summary = {"skipped_reason": None, "scanned": 0, "tier_alerts_sent": 0, "pullback_exception_sent": 0}
    now_wib = datetime.datetime.now(core.WIB)
    if now_wib.weekday() >= 5:
        summary["skipped_reason"] = "weekend"
        return summary
    if not (CONVICTION_SWEEP_WINDOW_START <= now_wib.time() <= CONVICTION_SWEEP_WINDOW_END):
        summary["skipped_reason"] = "outside_window"
        return summary
    if await asyncio.to_thread(core.is_idx_market_holiday_today):
        summary["skipped_reason"] = "holiday"
        return summary
    if not is_scan_alert_enabled():
        summary["skipped_reason"] = "toggled_off"
        return summary

    state = _ensure_conviction_daily_reset(_load_conviction_state())

    if state.get("universe") is None:
        main_state = _load_state()
        universe_tags = await asyncio.to_thread(_build_conviction_universe_tags, main_state)
        if not universe_tags:
            state["universe"] = {}
            _save_conviction_state(state)
            summary["skipped_reason"] = "no_universe"
            return summary
        ref_prices = await asyncio.to_thread(_fetch_conviction_ref, list(universe_tags.keys()))
        universe = {}
        n_suppressed = 0
        for t, info in universe_tags.items():
            ref_price = ref_prices.get(t)
            if not ref_price:
                continue
            tag = info["tag"]
            tp_info = None
            if tag in lane_confidence.SUPPORTED_LANES:
                tp_info = lane_confidence.compute_tp1_tp2(tag, info["features"], ref_price)
                if tp_info is None:
                    n_suppressed += 1
                    continue  # confidence individual <50% di level terdekat -- tidak diproduksi jadi sinyal
            universe[t] = {"tag": tag, "ref_price": ref_price, "tp_info": tp_info}
        state["universe"] = universe
        print(f"✅ Conviction-sweep universe hari ini: {len(universe)} ticker ({n_suppressed} di-suppress, WR<50%).")
    else:
        universe = state["universe"]

    if not universe:
        summary["skipped_reason"] = "no_universe"
        _save_conviction_state(state)
        return summary

    data = await asyncio.to_thread(_fetch_today_1m, list(universe.keys()))
    tickers_state = state.setdefault("tickers", {})
    bot = None
    if core.TELEGRAM_BOT_TOKEN:
        import telegram
        bot = telegram.Bot(token=core.TELEGRAM_BOT_TOKEN)

    for t, info in universe.items():
        tag = info["tag"]
        ref_price = info["ref_price"]
        tp_info = info.get("tp_info")
        if not ref_price:
            continue
        sym = t + ".JK"
        try:
            bars = data[sym].dropna(how="all").sort_index()
        except Exception:
            continue
        if bars.empty:
            continue
        summary["scanned"] += 1
        current_price = float(bars["Close"].astype(float).iloc[-1])

        t_state = tickers_state.setdefault(t, {})
        if tp_info:
            ceiling_pct = tp_info.get("tp2_level", tp_info["tp1_level"])
            ceiling_price = tp_info.get("tp2_price", tp_info["tp1_price"])
        else:
            ceiling_pct = CONVICTION_TP_CEILING_PCT.get(tag, CONVICTION_TP_CEILING_DEFAULT_PCT)
            ceiling_price = ref_price * (1 + ceiling_pct / 100.0)

        events = _process_conviction_ticker(t_state, current_price, ref_price, ceiling_price)
        for event in events:
            kind = event[0]
            if kind == "tier":
                tier = event[1]
                msg = _build_conviction_sweep_message(t, tag, tier, current_price, ref_price, ceiling_price, ceiling_pct, tp_info=tp_info)
                summary["tier_alerts_sent"] += 1
            else:
                msg = _build_conviction_pullback_exception_message(t, tag, current_price, ref_price, ceiling_price, tp_info=tp_info)
                summary["pullback_exception_sent"] += 1
            if bot is not None:
                await core.safe_reply(bot, msg, chat_id=core.TELEGRAM_CHAT_ID)
            else:
                print(f"[NO TELEGRAM TOKEN] {msg}")

    _save_conviction_state(state)
    print(f"✅ Conviction-sweep selesai: {summary['scanned']} ticker dicek, "
          f"{summary['tier_alerts_sent']} tier alert, {summary['pullback_exception_sent']} pullback-exception.")
    return summary
