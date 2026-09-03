"""
MBSS v2 (user request) — push alert intraday "breaking" utk scalping, 2 tahap:

  *** Alert A & Alert B DI BAWAH INI SUDAH DEAD (ALERT_A_B_PUSH_ENABLED=
  False sejak 2026-08-27 -- backtest 1m riil 27 hari TERBUKTI edge nyaris
  0, lihat catatan di ALERT_A_B_PUSH_ENABLED). Kode deteksi/pesan TETAP
  ADA tapi TIDAK PERNAH push, deskripsi di bawah BUKAN cerminan behavior
  produksi saat ini. Gap-up/GAP-REBOUND (setelahnya di bawah) sudah
  di-MERGE 2026-08-30 -- lihat catatan GAP_REBOUND_MIN_PCT/GAP_HOLD_MIN_
  VOL_RATIO_PARTIAL utk state final. ***

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
  - HC Minervini gap-watch: RETIRED 2026-08-30 -- lihat engine/nightly.py
    utk riwayat lengkap (backtest ulang dgn populasi WR model baru
    membuktikan sinyal ini tidak bertahan, mayoritas cuma dari gap-nya
    sendiri, give-back close-based parah).

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
import pickle
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

# MBSS v2 (bugfix 2026-08-31, incident live: run_scanalert_job macet >5 menit
# TANPA exception sama sekali -- job_queue (max_instances=1) skip terus tiap
# siklus 3 menit berikutnya, operator terpaksa Ctrl+Z bot.py berulang kali
# krn kelihatan "hang", numpuk proses zombie (Ctrl+Z = SIGTSTP, BUKAN kill --
# proses lama TETAP hidup di background) sampai 4x ~16% RAM tiap proses,
# genuinely inilah akar slowdown/SSH-hang sebelumnya, bukan cuma network
# blip sesaat). yf.download versi terpasang (1.6.0) SUDAH py default
# timeout=10 per-request, TAPI kombinasi threads=True + batch besar (200+
# ticker) terbukti bisa tetap macet total jauh lebih lama tanpa exception
# (kemungkinan deadlock/starvation thread-pool internal yfinance sendiri,
# di LUAR jangkauan timeout per-HTTP-request-nya) -- safety net di LEVEL
# JOB ini independen dari timeout internal yfinance: kalau fetch universe
# besar tidak selesai dalam JOB_FETCH_TIMEOUT_SEC, batalkan tunggu (job
# tetap SELESAI dgn hasil kosong/None utk siklus ini, TIDAK memblokir siklus
# berikutnya) drpd menggantung tanpa batas. CATATAN: asyncio.wait_for HANYA
# membatalkan MENUNGGUnya di sisi event loop -- thread background yg sudah
# terlanjur jalan (asyncio.to_thread) tidak bisa dipaksa berhenti (keterbatasan
# Python thread, bukan bug di sini), tapi itu cukup utk mencegah job_queue
# macet berantai spt insiden ini.
JOB_FETCH_TIMEOUT_SEC = 90

# Sentinel dipakai di caller yg cache hasil SEKALI/HARI (fresh_cross_momentum_
# watchlist, pre_continuation_watchlist, daily_ref, dll) -- HARUS bisa bedakan
# "timeout, coba lagi siklus berikutnya" dari "genuinely dihitung & hasilnya
# kosong" ({} kosong itu SAH, mis. 0 kandidat FCM hari itu) -- kalau caller
# pakai default={} biasa, timeout jg ke-cache PERMANEN sbg "0 kandidat"
# sepanjang hari (bug yg sama persis dgn conviction-sweep sebelum diperbaiki).
_FETCH_TIMED_OUT = object()


async def _fetch_with_timeout(fn, *args, timeout: float = JOB_FETCH_TIMEOUT_SEC, default=None):
    try:
        return await asyncio.wait_for(asyncio.to_thread(fn, *args), timeout=timeout)
    except asyncio.TimeoutError:
        arg_hint = f"{len(args[0])} item" if args and hasattr(args[0], "__len__") else ""
        print(f"⚠️ Timeout {timeout:.0f}s: {fn.__name__}({arg_hint}) -- skip siklus ini, coba lagi siklus berikutnya.")
        return default


# MBSS v2 (bugfix 2026-09-03, live case: "Scan-alert job gagal: can't start
# new thread" -- root cause DITEMUKAN saat investigasi bareng user: 5 job
# berbeda [run_gap_rebound_scan_once 60s, run_scan_alert_once 180s,
# run_bsjp_shortlist_scan_auto 900s, run_bsjp_recheck_once 300s SEJAK
# BSJP_RECHECK_INTERVAL_SEC dipercepat 2026-09-02, run_conviction_sweep_once
# 900s] SEMUA bikin telegram.Bot(token=...) BARU tiap siklus, TIDAK PERNAH
# di-shutdown/close -- python-telegram-bot v20+ bikin httpx.AsyncClient baru
# di dalamnya tiap instantiate, jadi resource (thread/koneksi) numpuk pelan2
# selama proses jalan lama, akhirnya OS menolak bikin thread baru. User
# eksplisit TOLAK opsi "perlambat interval job" (itu cuma menunda, BUKAN
# memperbaiki, DAN py biaya nyata -- run_gap_rebound_scan_once perlu cek
# SERING dlm jendela sempit 09:00-09:10 utk nangkap rebound cepat) -- minta
# perbaikan akar: SATU Bot instance di-cache & DIPAKAI ULANG lintas SEMUA
# job, bukan dibikin baru tiap siklus. TIDAK pakai context.bot (JobQueue
# punya itu) krn fungsi2 ini SENGAJA bisa dipanggil manual DI LUAR job
# context juga (mis. /bsjp) -- caching module-level ini TETAP self-contained
# tanpa perlu context, cuma menghindari re-instantiate.
_shared_telegram_bot = None


def _get_shared_bot():
    """Satu telegram.Bot instance di-cache utk seumur proses (bukan re-create
    tiap job cycle) -- lihat catatan panjang di atas soal insiden 2026-09-03."""
    global _shared_telegram_bot
    if _shared_telegram_bot is None and core.TELEGRAM_BOT_TOKEN:
        import telegram
        _shared_telegram_bot = telegram.Bot(token=core.TELEGRAM_BOT_TOKEN)
    return _shared_telegram_bot


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
#
# MBSS v2 (user request 2026-08-30, MERGE -- "explore poin 1-3 apakah bisa
# di merge dan simplify tapi WR tetap bagus"): fresh 1m backtest (30 hari
# retensi Yahoo, 89 event gap [4,12)% pd 18 hari bursa Agustus 2026, n=130
# observasi -- lihat riwayat chat sesi ini utk detail lengkap) membandingkan
# REBOUND vs GAP_UP lama (_detect_gap_up, "beli langsung di open") pd
# horizon SAMA (5/10/15/30/60m + EOD, bukan cuma horizon masing2 spt
# validasi asli): REBOUND positif di SEMUA horizon (+0.24% s/d +1.20%
# close-based, 51-65% positif), GAP_UP flat-negatif di semua horizon (0%
# s/d -2.51% EOD, 32-48% positif) -- give-back klasik. Overlap populasi
# 92% (46/50 event GAP_UP JUGA fire REBOUND, tier 0.5% cukup longgar utk
# menangkap hampir semua "holds" case juga). GAP_UP & Alert A/B (sudah
# mati sejak ALERT_A_B_PUSH_ENABLED=False) DIHAPUS TOTAL, GAP_REBOUND jadi
# SATU-SATUNYA sinyal gap live, range diperlebar 4-10% -> 4-12% (warisi
# upper range GAP_UP, tidak ada bukti 10-12% lebih lemah). Range bawah HOLD
# check (GAP_UP_HOLD_CHECK_BARS/GAP_UP_HOLD_MAX_DROP_PCT di bawah, dipakai
# _detect_gap_hold BARU) DIPERTAHANKAN sbg mekanisme KEDUA -- lihat
# GAP_HOLD_MIN_VOL_RATIO_PARTIAL utk alasan.
GAP_REBOUND_MIN_PCT = 4.0     # gap open dari prev_close, batas bawah (sweet spot 5-8% tapi 4-10% dites juga oke)
GAP_REBOUND_MAX_PCT = 12.0    # diperlebar dari 10.0 (merge 2026-08-30) -- warisi upper range GAP_UP lama, tidak ada bukti 10-12% lebih lemah
GAP_REBOUND_TIERS = (0.5, 1.0, 1.5, 2.0)
GAP_REBOUND_DETECT_WINDOW_MIN = 10   # cari rebound HANYA 10 menit pertama sejak open, konsisten dgn riset
GAP_REBOUND_TP1_PCT = 4.0     # median MFE ~4-5% di horizon 5-10m dari entry rebound
GAP_REBOUND_TP2_PCT = 6.0     # median MFE ~6% di horizon 15-20m
GAP_REBOUND_SL_PCT = -2.5     # dekat P25 MAE dari entry rebound (-3.25%) -- kasih ruang dip median (-1.27%), potong sblm ekor buruk
GAP_REBOUND_MAX_HOLD_MIN = 20  # window realisasi TP1/TP2, sesuai riset "siku" di 15-20 menit
GAP_REBOUND_SCAN_WINDOW_START = datetime.time(9, 0)
GAP_REBOUND_SCAN_WINDOW_END = datetime.time(9, 10)
STATE_FILE_REBOUND = os.path.join(core.PROJECT_ROOT, "scanalert_rebound_state.json")

# MBSS v2 (user request 2026-08-30, "worth checking kombinasi gap up dan
# vol" -- mekanisme KEDUA merge, PENGGANTI _detect_gap_up lama): dari n=50
# event GAP_UP lama, volume 5 menit pertama (dinormalisasi ke laju volume
# 20-hari sendiri, vol_ratio_partial = vol_window/(avg_vol_20d/330menit*5))
# corr POSITIF ke return (+0.38 di 15m, +0.37 di 30m) -- tercile: LOW
# (n=17) EOD median -3.81%/5.9% positif (JELEK), MID (n=16) +2.28%/56.2%
# positif (BAGUS), HIGH (n=17) +short-term oke tapi fade EOD -1.54%/35.3%
# positif. Floor diambil dari batas tercile LOW/MID empiris (~11.7,
# dibulatkan 12.0) -- HANYA exclude sepertiga TERBURUK, MID+HIGH tetap
# lolos (exit disiplin cepat sudah jadi warning wajib, jadi fade EOD di
# HIGH tercile less relevant selama TP diambil cepat). CATATAN: floor ini
# TIDAK diterapkan ke REBOUND (vol_ratio_partial REBOUND kena artifact
# data -- >=60/80 observasi volume bar pertama tercatat 0 dari Yahoo,
# BUKAN genuinely nol, corr null-nya TIDAK bisa dipercaya) -- REBOUND
# sudah cukup baik tanpa filter tambahan, jangan tambah filter belum teruji.
# n=50 (18 hari bursa) -- directional/pilot, bukan setara confidence
# backtest 2 tahun daily lain di sesi ini, sample masih kecil.
GAP_HOLD_MIN_VOL_RATIO_PARTIAL = 12.0

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
# sandbox sesi ini, belum diinvestigasi) -- BSJP_ARA/BSJP_SECOND_WAVE
# DIHAPUS dari daftar ini, MBSS v2 user request 2026-08-29: BSJP bukan lagi
# lane conviction-sweep. HC_GAP_WATCH (dulu justifikasi utama DEFAULT_PCT
# di bawah) RETIRED 2026-08-30, lihat engine/nightly.py -- DEFAULT_PCT
# dipertahankan sbg fallback generik utk tag lain di luar dict ini.
CONVICTION_TP_CEILING_PCT = {
    "FAST_RECOVERY": 8.0,
    "EARLY_RECOVERY": 8.0,
}
CONVICTION_TP_CEILING_DEFAULT_PCT = 8.0

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
        f"DAY TRADE\n"
        f"🔥 {ticker} REBOUND +{tier:.1f}% dari dip — entry {fire_price:,.0f}\n"
        f"Gap open +{gap_pct:.1f}% (open {day_open:,.0f}), sempat dip {dip_pct:+.1f}% ke {running_low:,.0f}\n"
        f"TP1 {tp1:,.0f} (+{GAP_REBOUND_TP1_PCT:.0f}%) | TP2 {tp2:,.0f} (+{GAP_REBOUND_TP2_PCT:.0f}%) — max {GAP_REBOUND_MAX_HOLD_MIN} menit\n"
        f"SL {sl:,.0f} ({GAP_REBOUND_SL_PCT:+.1f}%){danger_line}"
    )


async def run_gap_rebound_scan_once() -> dict:
    """
    One-shot CLI (`python bot.py --scanalert-rebound`), DIPISAH dari
    run_scan_alert_once() -- lihat catatan konstanta GAP_REBOUND_* di atas
    utk alasan (cadence 1 menit khusus jendela 09:00-09:10, state file
    sendiri hindari race condition baca-ubah-tulis bareng --scanalert utama).

    MBSS v2 (user request 2026-08-30, MERGE): SEKARANG mengirim DUA
    mekanisme independen per ticker (nama fungsi/job/state file TETAP
    "rebound" -- historical, bukan lagi literal cakupannya, lihat catatan
    GAP_REBOUND_MIN_PCT/GAP_HOLD_MIN_VOL_RATIO_PARTIAL utk alasan merge):
    1. REBOUND (tier 0.5/1.0/1.5/2.0% dari dip) -- TIDAK berubah.
    2. HOLD (gap holds >=5 menit + volume gate) -- PENGGANTI _detect_gap_up/
       Alert A/B lama (retired total, terbukti give-back parah di backtest
       ulang 2026-08-30).
    Keduanya independen (satu ticker bisa fire salah satu, keduanya, atau
    tidak sama sekali), sama pola dgn FCM open-buy vs pullback-entry.
    """
    summary = {"skipped_reason": None, "scanned": 0, "rebound_sent": 0, "hold_sent": 0}
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

    if not state.get("daily_ref"):
        # BUGFIX (live incident 2026-09-02): SEBELUMNYA `state["daily_ref"] =
        # daily_ref` di-assign TANPA SYARAT walau timeout (daily_ref={}) --
        # kalau berhasil sempat ke-save ke disk, ini akan MEMBEKUKAN {}
        # selamanya (state.get("daily_ref") is None jadi False permanen,
        # persis bug conviction-sweep/swing-lane yg SUDAH diperbaiki di
        # tempat lain -- kelewat di sini). Ditambah: ticker_list=[] dari
        # daily_ref kosong lalu diteruskan ke _fetch_today_1m([]) ->
        # yf.download(symbols=[]) -> ValueError "No objects to concatenate"
        # (exception ASLI, BUKAN timeout, jadi TIDAK ketangkep _fetch_with_
        # timeout) -- crash SETIAP siklus, coincidentally MENCEGAH state ke-
        # save (fungsi crash sebelum sempat _save_rebound_state), jadi
        # retry tiap menit (bukan macet selamanya) TAPI selalu crash.
        # Fix: HANYA cache kalau genuinely terisi; kalau kosong (timeout
        # ATAU genuinely tak ada data), skip bersih siklus ini -- TIDAK
        # crash, TIDAK dibekukan, coba lagi siklus berikutnya.
        daily_ref = await _fetch_with_timeout(_fetch_daily_ref, universe, default={})
        if not daily_ref:
            summary["skipped_reason"] = "daily_ref_unavailable"
            _save_rebound_state(state)
            return summary
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
    data = await _fetch_with_timeout(_fetch_today_1m, ticker_list, default=pd.DataFrame())

    tickers_state = state.setdefault("tickers", {})
    bot = _get_shared_bot()

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

        t_state = tickers_state.setdefault(t, {"running_low": day_open, "tiers_fired": [], "done": False, "hold_sent": False})
        t_state.setdefault("hold_sent", False)  # ticker lama di state file blm punya field ini

        if not t_state.get("done"):
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

        # MBSS v2 (user request 2026-08-30, MERGE): mekanisme KEDUA, independen
        # dari REBOUND di atas -- lihat docstring fungsi ini/_detect_gap_hold.
        if not t_state["hold_sent"]:
            det_hold = _detect_gap_hold(bars, prev_close, ref.get("avg_vol_20d"))
            if det_hold:
                danger_tag = _danger_gate_tag(t, danger_lookup)
                risk_tags = [danger_tag] if danger_tag else []
                msg = _build_gap_hold_message(t, det_hold, risk_tags=risk_tags)
                if bot is not None:
                    await core.safe_reply(bot, msg, chat_id=core.TELEGRAM_CHAT_ID)
                else:
                    print(f"[NO TELEGRAM TOKEN] {msg}")
                t_state["hold_sent"] = True
                summary["hold_sent"] += 1

    _save_rebound_state(state)
    print(f"✅ Gap scan selesai: {summary['scanned']} ticker discan, {summary['rebound_sent']} alert REBOUND + {summary['hold_sent']} alert HOLD terkirim.")
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
        # MBSS v2 (user request 2026-08-30, merge gap-up/gap-rebound): avg
        # volume MENTAH (bukan value_traded) -- basis vol_ratio_partial di
        # _detect_gap_hold, sama window/tail(20) dgn avg_value_traded_20d
        # di atas (konsisten dgn toleransi "20d" yg sudah dipakai field itu).
        vol_tail20 = volumes.tail(20)
        avg_vol_20d = float(vol_tail20.mean()) if len(vol_tail20) else None
        ref[t] = {
            "prev_close": prev_close, "ret_3d": ret_3d,
            "avg_value_traded_20d": avg_value_traded_20d, "avg_vol_20d": avg_vol_20d,
        }
    return ref


# MBSS v2 (user request 2026-08-29, REVISI TOTAL -- unified BSJP, blend
# ARA/second-wave jadi SATU sinyal, "Beli Sore Jual Pagi"): full parameter
# sweep (ret_1d band, volume multiplier, price-near-high tightness, volume
# vs MA200 -- 162 kombinasi, validasi CLOSE-based/exit-efficiency bukan
# touch-rate, daily_2y_issi_raw.pkl chronological discovery/validation
# split) menemukan gap-reaction TERBAIK justru saat SEMUA 4 kriteria
# berikut terpenuhi bersamaan:
#   1. ret_1d > 15% (lihat revisi 2026-08-31 di bawah)
#   2. Volume > 1.5x volume kemarin
#   3. High < 1.01x harga sekarang ("clean close" -- proksi close_pos_
#      today, korelasi TERKUAT ke gap besok yg ditemukan sesi ini, 0.327
#      Spearman, jauh di atas fitur lain yg diuji)
#   4. Volume > 1.0x rata-rata volume 200 hari
# Median gap reaction (Open besok vs harga saat SEMUA 4 lolos) = 3.1%,
# n=196-208 validation, out-of-sample (angka ASLI di threshold 18%, lihat
# revisi di bawah utk angka di 15%). TIDAK ada individual WR yg justified
# (semua korelasi fitur lanjutan <0.25 setelah gate ini) -- TP1 pakai
# angka grup, bukan model per-ticker.
#
# ARSITEKTUR 2-FASE (user request, "kalau EOD-only, belum tentu open besok
# up dari close, jadi ketinggalan kereta"): entry sebenarnya SORE INI
# (SEBELUM gap terjadi), BUKAN besok pagi (setelah gap, sudah kemahalan) --
# deteksi HARUS live intraday:
#   Fase 1 (/bsjp manual, ATAU run_bsjp_shortlist_scan_auto JobQueue tiap
#     15 menit 09:00-15:50 -- lihat revisi di bawah): scan SELURUH
#     universe, simpan yg lolos 4 kriteria sbg shortlist.
#   Fase 2 (run_bsjp_recheck_once, JobQueue tiap 15 menit 09:30-15:50):
#     re-cek HANYA shortlist (murah), kirim alert final ke ticker yg MASIH
#     lolos SEMUA 4 kriteria (proyeksi makin akurat makin sore) & belum
#     pernah dialert hari ini.
# Alert = sinyal "beli SEKARANG", bukan sekadar konfirmasi pasif. TP1 =
# harga saat alert x (1+3.1%), jual besok pagi begitu tersentuh (atau,
# kalau belum sempat entry sore, besok pagi masih ada room ke TP1 -> boleh
# masuk/averaging up; sudah dekat/lewat TP1 -> jangan kejar). JANGAN tahan
# ke closing besok -- gap fade cepat (D1 CLOSE median -5.93% pd threshold
# ini, jauh lebih dalam dari gap reaction-nya sendiri).
#
# MBSS v2 (user request 2026-08-31, live case BALI/KICI lolos shortlist tapi
# sudah +25%/+24.4% -- practically sudah ARA, tidak bisa dibeli lagi): DUA
# perbaikan sekaligus, saling melengkapi (backtest ulang 64 hari bursa
# validasi, 3 kriteria lain TETAP produksi):
#   1. BSJP_RET1D_MIN_PCT diturunkan 18%->15% -- avg jarak ke ARA saat
#      kandidat tertangkap naik dari 0.82% ke 1.12% (proksi ARA band kasar
#      35/25/20% by price tier), harga kualitas gap_med turun 3.06%->2.70%
#      (~12% relatif) & touch-rate 64.5%->62.3% (~2pp) -- tradeoff nyata
#      tapi moderat, n/hari nyaris sama (2.86 vs 2.98). 12% dites jg tapi
#      dibuang (avg_dist 1.79% lumayan lebih besar, TAPI gap_med anjlok ke
#      2.29%, ~25% relatif -- terlalu agresif).
#   2. run_bsjp_shortlist_scan_auto (JobQueue baru, 09:00-15:50 WIB tiap 15
#      menit) -- SEBELUMNYA Fase 1 CUMA manual-trigger (/bsjp), jadi kalau
#      user baru cek siang/sore, kandidat yg nembus threshold pagi2 sudah
#      lanjut lari ke ARA sebelum sempat ketahuan. Backtest EOD di atas
#      TIDAK bisa mengukur efek checking lebih SERING (cuma 1x/hari by
#      construction) -- perbaikan #2 ini independen dari #1, keduanya
#      saling menambah room, bukan salah satu saja.
BSJP_RET1D_MIN_PCT = 15.0
BSJP_VOL_VS_PREV_DAY_MULT = 1.5
BSJP_CLEAN_CLOSE_MULT = 1.01
BSJP_VOL_VS_MA200_MULT = 1.0
# MBSS v2 (user request 2026-09-01 -- redesign besar Fase 1/Fase 2 setelah
# riset live case "nailunn" BSJP list + KKES + kalibrasi clean_close per
# checkpoint waktu, lihat riwayat chat sesi 2026-09-01 utk detail lengkap):
#
# FASE 1 (jaring kandidat AWAL, longgar SENGAJA -- toh Fase 2 validasi ulang
# ketat): ret_1d>=1% (BUKAN lagi toleransi 75% dari 15%=11.25% -- >=1% hampir
# tanpa syarat return, krn EOD-based 15% terbukti membuang byk kandidat
# genuine spt POWR/SOCI/SMLE/BEST/ICON/IATA/YELO/EPAC -- 10/14 nailunn list
# gagal MURNI krn ret_1d, padahal 100% touch positif Day+1). vol_vs_prev/
# vol_vs_ma200 KEMBALI ke ambang PENUH (1.5x/1.0x, TANPA toleransi lagi --
# toleransi 75% utk 2 kriteria ini terbukti nyaris tak menambah kandidat di
# sweep 1m sebelumnya). clean_close<=1.05 (naik dari 1.03 -- dites ulang
# live case KKES: 1.05 vs 1.08/1.10 SAMA PERSIS waktu capture-nya [vol_vs_
# prev jadi penghambat baru begitu clean_close dilonggarkan >=1.05], jadi
# 1.05 sudah titik jenuh, tak perlu lebih longgar lagi).
BSJP_SHORTLIST_RET1D_MIN_PCT = 1.0
BSJP_SHORTLIST_CLEAN_CLOSE_MULT = 1.05
#
# FASE 2 (keputusan AKHIR sebelum alert "BELI SORE INI" beneran dikirim):
# ret_1d awalnya DIHAPUS SAMA SEKALI sbg gate (user: "ret_1d kan dari EOD
# sebelumnya, intraday sudah pasti bergerak lebih jauh, jadi basi sbg gate
# di titik keputusan akhir"). clean_close TETAP KETAT (default 1.01x,
# TIDAK dilonggarkan) -- kalibrasi per-checkpoint-waktu (1m riil, 7 hari,
# populasi kandidat ret_1d>5%&vol_vs_prev>1x di checkpoint) nunjuk clean_
# close KETAT justru MENANG di kedua metrik (P(EOD masih clean) DAN
# touch-rate Day+1 sungguhan) di SETIAP checkpoint yg dites -- melonggarkan
# clean_close utk Fase 2 JUSTRU mengencerkan kualitas, bukan menambah
# kandidat baik. vol_vs_prev/vol_vs_ma200 TETAP penuh (1.5x/1.0x, tak
# pernah berubah). Window dimajukan drastis 14:00->09:30 (user: ARA
# beberapa kali sudah terjadi SEJAK SESI 1) DAN interval dipercepat
# 1800s->900s, DISINKRONKAN dgn interval Fase 1 otomatis.
#
# UPDATE 2026-09-02 (live case MNCN lolos Fase 2 padahal sideways beberapa
# hari): ret_1d intraday (bukan EOD kemarin -- field ret_1d_pct yg SAMA,
# current_price vs prev_close, live SAAT dicek) DIKEMBALIKAN sbg gate
# MINIMAL utk Fase 2, TAPI angka kecil (bukan 15% lama). Backtest EOD-
# proxy (2thn/576 ISSI, chronological 70/30 validasi, populasi vol+clean_
# close SAJA tanpa syarat return): pita 0-1% TERBUKTI terlemah (touch3=
# 12.7%/touch6=5.2%, hampir sama dgn MNCN-style sideways) vs >=1%
# (touch3=43.2%/touch6=30.0%) & >=3% (51.0%/37.0%) -- >=3% direkomendasikan
# tapi user minta DILONGGARKAN spy kandidat tetap kelihatan (bukan terlalu
# sedikit), jadi >=1% dipilih (n=1256 vs n=972 di validasi, tetap
# menyingkirkan kasus sideways MNCN-style [+1.0% persis di batas]).
# Cross-check checkpoint sesi 1 (1m riil, ~12:00 WIB, n kecil) nunjuk pola
# LEBIH RUMIT -- ret_1d intraday TIDAK monoton memprediksi P(EOD masih
# clean) sendirian (malah pita 0-1% py P(EOD_clean) TERTINGGI 45.4%,
# turun ke 20-24% di pita 3-10% -- big mover pagi py lebih byk waktu utk
# konsolidasi siang), TAPI ret_1d intraday KONSISTEN memprediksi MAGNITUDE
# return EOD (med -1.4%->+13.8% monoton bersih di 7 pita). clean_close
# SAAT checkpoint yg sama jauh lebih prediktif utk "akan tetap clean":
# di antara ret1d_s1>=3%, yg SUDAH clean_close_s1<1.02 py P(EOD_clean)=
# 34.3% vs yg SUDAH pullback (cc_s1>=1.05) cuma 5.2% -- clean_close SAAT
# ITU (bukan return) yg jadi sinyal utama utk "akan tetap clean", ret_1d
# lebih sbg sinyal MAGNITUDE. clean_close gate Fase 2 (1.01x ketat,
# TETAP tidak berubah) SUDAH menangkap bagian ini scr langsung tiap kali
# dicek -- floor ret_1d>=1% di sini melengkapi dgn menyaring kasus
# sideways murni (spt MNCN) yg toh KEBETULAN clean pada momen dicek.
BSJP_RECHECK_RET1D_MIN_PCT = 1.0
BSJP_TP1_MEDIAN_GAP_PCT = 3.1
BSJP_RECHECK_WINDOW_START = datetime.time(9, 30)
BSJP_RECHECK_WINDOW_END = datetime.time(15, 50)
# MBSS v2 (user request 2026-09-02): 900s->300s -- alasan LANGSUNG terkait
# temuan presisi sesi ini: entry ASLI (harga saat checkpoint pertama lolos)
# rata-rata +0.85% s.d +1.81% DI ATAS Close(T) [snap["current_price"] pd
# saat alert fire vs closing hari itu] -- ticker biasanya SUDAH mulai fade
# dari titik lolos gate sebelum sempat dialert, cek lebih SERING mengurangi
# lag deteksi (bukan lag antar TICK, tapi lag SEJAK ticker genuinely lolos
# s.d TERDETEKSI) jadi entry lebih dekat ke harga saat genuinely lolos, BUKAN
# beberapa menit setelahnya. SENGAJA TIDAK disamakan dgn Fase 1 (TETAP 900s
# di bawah) -- Fase 1 cuma jaring kandidat awal (longgar, tidak time-
# sensitive), Fase 2 yg genuinely butuh presisi timing krn itu yg jadi
# harga alert beneran. first=250 (job registration, legacy_core.py)
# TIDAK bentrok dgn Fase 1 (first=460/900s) atau conviction sweep
# (first=100/900s) di interval BARU ini -- gcd(300,900)=300, (250-460) &
# (250-100) SAMA SEKALI TIDAK habis dibagi 300, jadi TIDAK PERNAH align.
BSJP_RECHECK_INTERVAL_SEC = 300  # 5 menit
BSJP_SHORTLIST_SCAN_WINDOW_START = datetime.time(9, 0)  # Fase 1 otomatis -- mulai buka, BUKAN nunggu akhir sesi 1
BSJP_SHORTLIST_SCAN_INTERVAL_SEC = 900  # 15 menit -- Fase 1 TETAP, lihat catatan BSJP_RECHECK_INTERVAL_SEC knp Fase 2 dipercepat sendiri
BSJP_MIN_HISTORY_DAYS = 260  # >200 hari (MA200) + buffer hari libur/data hilang

# MBSS v2 (BUGFIX 2026-09-02, DIKOREKSI setelah versi pertama shipped):
# angka DI BAWAH INI (Tier 2 + /bsjp tp) awalnya dihitung dari backtest yg
# TERCEMAR -- daily_2y_issi_extended.pkl punya baris hantu 2026-08-25
# (libur, TIDAK ada transaksi) dgn Close=NaN utk 548/576 ticker, dan
# prev_close SEBELUMNYA diambil TANPA dropna() dulu -- utk T=2026-08-26
# prev_close jadi NaN, ret_1d jadi NaN, dan gate `ret_1d<=RET1D_MIN` diam-
# diam TIDAK PERNAH menolak (NaN<=x selalu False) -- 41 dari 134 kandidat
# Fase 2 "presisi" awal ternyata SPURIOUS (lolos gate ret_1d yg seharusnya
# gagal). Di-fix (.dropna() sebelum .iloc[-1]) & backtest diulang bersih --
# n Fase 2 valid jadi 93 (bukan 134), n Fase1-reject jadi 79 (bukan 122).
#
# Tier 2 "BSJP WATCH" (kandidat lolos Fase 1 [clean_close<=1.05] tapi
# GAGAL Fase 2 ketat [clean_close<1.01]): reject group (n=79, CORRECTED)
# touch3=38.0%/touch10=8.9% (vs ACCEPT 44.1%/20.4%). above_sma50 TERNYATA
# TIDAK banyak menyelamatkan lagi setelah dikoreksi (reject&above_sma50
# n=70 touch3=38.6% -- HAMPIR SAMA dgn reject keseluruhan 38.0%, beda dari
# klaim awal yg [tercemar] nunjuk gap besar) -- above_sma50 dipertahankan
# sbg filter ringan (matches 70/79 = 89% populasi reject secara alami,
# tidak merugikan), TAPI JANGAN klaim ini "rescue" kuat lagi, cuma
# menyaring tipis. Reject group SENDIRI (bahkan tanpa trend filter) masih
# py edge nyata di atas baseline populasi umum (~31% dari studi EOD 2yr
# sebelumnya) -- itu alasan Tier 2 tetap dipertahankan, BUKAN krn
# above_sma50-nya.
BSJP_WATCH_TP_GAP_PCT = 2.0
BSJP_WATCH_TOUCH3_HISTORICAL_PCT = 38.6

# /bsjp tp -- panduan jual pre-open esok pagi. ANCHOR = CLOSING harga hari
# alert (2026-09-02, user request, live case KKES -- avg cost 111, closing
# ternyata 94-95, TP1/TP2 lama yg dihitung dari entry ALERT FIRE [109]
# terbukti TIDAK REALISTIS: backtest utk grup "faded>=5%" spt KKES nunjuk
# 0% (BUKAN rendah, NOL) ticker yg PERNAH balik ke harga entry lagi di Day+1,
# apalagi ke TP1/TP2 dari situ. User: "sesuai disiplin BSJP yg memang
# seharusnya beli di sore hari (>15:30)" -- closing HARIAN (yg sudah
# mencerminkan harga sore/akhir sesi) adalah anchor yg BENAR secara disiplin
# MAUPUN backtest, bukan harga alert-fire yg bisa jauh dari closing.
#
# Backtest presisi CORRECTED (Fase 2 ACCEPT, n=93, 5 hari bursa) dgn ANCHOR
# CLOSE (bukan entry): overall touch>=1%=74.2%/>=3%=48.4%/>=5%=34.4% (semua
# LEBIH TINGGI drpd anchor entry yg sebelumnya touch>=1%=57.0%/>=3%=40.9%/
# >=5%=33.3% -- closing adalah anchor yg lebih baik, bukan cuma lebih benar
# disiplin). Tier ret_1d_pct (segmen SAMA, floor-walk SAMA, TAPI touch
# dihitung dari CLOSE):
#   ret_1d 1-5%   (n=42): TP1=1.5% (52.4%)  TP2=3%  (31.0%)
#   ret_1d 5-10%  (n=17): TP1=3%   (58.8%)  TP2=7%  (29.4%)
#   ret_1d 10-20% (n=18): TP1=4%   (50.0%)  TP2=10% (27.8%)
#   ret_1d >=20%  (n=16): TP1=10%  (50.0%)  TP2=20% (25.0%)
#
# FADE CAP (kritis, live case KKES): drift entry->close BUKAN noise --
# ticker yg closing-nya SUDAH >=5% di bawah harga alert-fire (spt KKES,
# -12.8%) py profil BEDA SAMA SEKALI: n=11, TP1 floor-walk(50%) cuma 2.5%
# (54.5%), dan NOL kejadian tembus >=8% dari closing (bukan rendah, betul2
# nol di sampel ini). Kandidat spt ini TIDAK BOLEH dikasih TP2 stretch dari
# tier ret_1d biasa (misal tier >=20% yg nawarin TP2=20% -- itu klaim palsu
# utk ticker yg sudah crash separuh hari). Override: kalau drift<=-5%, TP1
# diturunkan ke BSJP_FADE_TP1_GAP_PCT & TP2 DIHILANGKAN SAMA SEKALI (bukan
# dikecilkan -- backtest literally 0% di atas 7-8%, menampilkan angka apa
# pun di situ menyesatkan).
BSJP_TP_TIERS = [
    # (ret1d_lo, ret1d_hi, tp1_gap_pct, tp1_hit_pct, tp2_gap_pct, tp2_hit_pct)
    (1.0, 5.0, 1.5, 52.4, 3.0, 31.0),
    (5.0, 10.0, 3.0, 58.8, 7.0, 29.4),
    (10.0, 20.0, 4.0, 50.0, 10.0, 27.8),
    (20.0, float("inf"), 10.0, 50.0, 20.0, 25.0),
]
BSJP_TP1_GAP_PCT = 2.0  # fallback (pick lama tanpa ret_1d_pct tersimpan)
BSJP_TP1_HISTORICAL_HIT_PCT = 46.2
BSJP_TP2_GAP_PCT = 6.0  # fallback (pick lama tanpa ret_1d_pct tersimpan)
BSJP_TP2_HISTORICAL_HIT_PCT = 26.9

BSJP_FADE_DRIFT_THRESHOLD_PCT = -5.0  # closing >=5% di bawah harga alert-fire
BSJP_FADE_TP1_GAP_PCT = 2.5
BSJP_FADE_TP1_HISTORICAL_HIT_PCT = 54.5


def _bsjp_tp_for_ret1d(ret1d_pct: float | None) -> tuple[float, float, float, float]:
    """Return (tp1_gap_pct, tp1_hit_pct, tp2_gap_pct, tp2_hit_pct) utk ret_1d_pct
    tertentu -- reuse BSJP_TP_TIERS, fallback ke flat BSJP_TP1_GAP_PCT/BSJP_TP2_
    GAP_PCT kalau ret1d_pct None atau di luar seluruh tier (mis. persis 1.0%).
    TIDAK menghandle fade-cap -- itu override TERPISAH di caller (butuh drift,
    bukan cuma ret1d_pct), lihat build_bsjp_tp_plan_message."""
    if ret1d_pct is not None:
        for lo, hi, tp1, tp1_hit, tp2, tp2_hit in BSJP_TP_TIERS:
            if lo <= ret1d_pct < hi:
                return tp1, tp1_hit, tp2, tp2_hit
    return BSJP_TP1_GAP_PCT, BSJP_TP1_HISTORICAL_HIT_PCT, BSJP_TP2_GAP_PCT, BSJP_TP2_HISTORICAL_HIT_PCT

STATE_FILE_BSJP = os.path.join(core.PROJECT_ROOT, "bsjp_shortlist_state.json")


def _load_bsjp_state() -> dict:
    if not os.path.exists(STATE_FILE_BSJP):
        return {}
    try:
        with open(STATE_FILE_BSJP) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_bsjp_state(state: dict):
    with open(STATE_FILE_BSJP, "w") as f:
        json.dump(state, f, indent=2)


STATE_FILE_BSJP_BASE = os.path.join(core.PROJECT_ROOT, "bsjp_historical_base.json")
_BSJP_FETCH_BATCH = 100  # chunk bulk yf.download -- hindari 1 request raksasa utk seluruh universe sekaligus


def _load_bsjp_base_cache() -> dict:
    if not os.path.exists(STATE_FILE_BSJP_BASE):
        return {}
    try:
        with open(STATE_FILE_BSJP_BASE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_bsjp_base_cache(cache: dict):
    with open(STATE_FILE_BSJP_BASE, "w") as f:
        json.dump(cache, f)


def _fetch_bsjp_historical_base(tickers: list[str]) -> dict:
    """
    Bagian STATIS (prev_close/prev_volume/vol_ma200 -- SEMUA dari data
    SEBELUM hari ini, tidak pernah berubah sepanjang hari bursa berjalan)
    -- 260 hari, MAHAL, tapi dipanggil CUMA 1x/hari (di-cache, lihat
    _fetch_bsjp_universe_snapshot). Filter tanggal EKSPLISIT (bukan
    position-based iloc[:-1]) -- benar apapun waktu fetch ini terjadi
    (bisa saja base pertama kali dibangun SAAT market sudah buka, jadi
    baris terakhir yg di-download BISA jadi hari ini sendiri, bukan
    kemarin -- kalau pakai iloc[:-1] robotic itu keliru diasumsikan
    "prior" padahal itu hari ini).
    """
    today_date = datetime.datetime.now(core.WIB).date()
    base = {}
    for i in range(0, len(tickers), _BSJP_FETCH_BATCH):
        batch = tickers[i:i + _BSJP_FETCH_BATCH]
        symbols = [t + ".JK" for t in batch]
        data = yf.download(symbols, period=f"{BSJP_MIN_HISTORY_DAYS}d", interval="1d", group_by="ticker", threads=True, progress=False)
        for t in batch:
            sym = t + ".JK"
            try:
                d = data[sym].dropna(how="all")
            except Exception:
                continue
            prior = d[d.index.date < today_date]
            if len(prior) < 200:
                continue
            prev_close = float(prior["Close"].iloc[-1])
            prev_volume = float(prior["Volume"].iloc[-1])
            vol_ma200 = float(prior["Volume"].tail(200).mean())
            # MBSS v2 (user request 2026-09-02, Tier 2 "BSJP WATCH" --
            # above_sma50 causal, dari 50 Close SEBELUM hari ini SAJA, TIDAK
            # ikut close hari berjalan yg belum final): dipakai sbg trend
            # filter, lihat catatan lengkap di atas BSJP_WATCH_TP_GAP_PCT.
            sma50 = float(prior["Close"].tail(50).mean()) if len(prior) >= 50 else None
            if prev_close <= 0:
                continue
            base[t] = {"prev_close": prev_close, "prev_volume": prev_volume, "vol_ma200": vol_ma200, "sma50": sma50}
        if i + _BSJP_FETCH_BATCH < len(tickers):
            time.sleep(0.5)
    return base


def _fetch_bsjp_live_bar(tickers: list[str]) -> dict:
    """
    Bagian LIVE (HARI INI saja) -- period KECIL (5 hari, bukan 260),
    dipanggil TIAP kali _fetch_bsjp_universe_snapshot jalan (beda dari
    historical base yg di-cache 1x/hari). Inilah fetch yg genuinely perlu
    fresh tiap siklus -- Fase 1 tiap 15 menit, Fase 2 tiap 5 menit (2026-
    09-02, lihat BSJP_RECHECK_INTERVAL_SEC) -- payload-nya jauh lebih kecil
    drpd base.
    """
    today_date = datetime.datetime.now(core.WIB).date()
    live = {}
    for i in range(0, len(tickers), _BSJP_FETCH_BATCH):
        batch = tickers[i:i + _BSJP_FETCH_BATCH]
        symbols = [t + ".JK" for t in batch]
        data = yf.download(symbols, period="5d", interval="1d", group_by="ticker", threads=True, progress=False)
        for t in batch:
            sym = t + ".JK"
            try:
                d = data[sym].dropna(how="all")
            except Exception:
                continue
            today_rows = d[d.index.date == today_date]
            if today_rows.empty:
                continue
            today = today_rows.iloc[-1]
            live[t] = {
                "current_price": float(today["Close"]),
                "high_so_far": float(today["High"]),
                "volume_so_far": float(today["Volume"]) if not pd.isna(today["Volume"]) else 0.0,
            }
        if i + _BSJP_FETCH_BATCH < len(tickers):
            time.sleep(0.5)
    return live


def _fetch_bsjp_closing_prices(picks: list[dict]) -> dict:
    """
    /bsjp tp -- closing HARIAN (bukan harga live) utk HARI ALERT ASLI tiap
    pick, dipakai sbg anchor TP1/TP2 (lihat catatan panjang di atas BSJP_
    TP_TIERS knp closing, bukan harga alert-fire).

    BUGFIX 2026-09-02 (live case: closing yg tampil PERSIS SAMA dgn cut_loss
    utk ICON/JARR/KKES -- ketahuan krn user bandingkan langsung): versi
    PERTAMA pakai pick_date sbg tanggal target fetch -- SALAH, pick_date
    SELALU 1 hari SEBELUM hari alert beneran (get_current_trading_day_close_
    marker() dipanggil SAAT lock, SEBELUM 16:30 WIB, jadi masih mundur 1
    hari -- lihat catatan panjang di build_bsjp_tp_plan_message). Fetch
    "closing pick_date" scr harfiah = fetch closing HARI SEBELUM alert =
    PERSIS prev_close/cut_loss yg SUDAH tersimpan -- itu sebabnya closing
    yg tampil identik dgn cut_loss.

    Fix: JANGAN coba rekonstruksi tanggal alert dari pick_date (rapuh
    lewat weekend/libur -- +1 hari kalender BISA salah kalau alert hari
    Senin [pick_date jadi Jumat, bukan Minggu]). Sebagai gantinya, cocokkan
    NILAI cut_loss (prev_close) yg SUDAH tersimpan tiap pick thd baris
    daily bars -- begitu ketemu baris yg cocok, ambil baris SETELAHNYA
    (posisi berikutnya di data yfinance, yg SUDAH otomatis skip weekend/
    libur dgn sendirinya) sbg closing hari alert yg SEBENARNYA. Robust
    thd off-by-one apa pun penyebabnya, tidak perlu tau exact tanggal.
    """
    tickers = sorted({p["ticker"] for p in picks})
    cut_loss_by_ticker = {}
    for p in picks:
        cl = p.get("cut_loss")
        if cl:
            cut_loss_by_ticker[p["ticker"]] = cl  # sama utk semua pick ticker yg sama di hari yg sama

    closes = {}
    for i in range(0, len(tickers), _BSJP_FETCH_BATCH):
        batch = tickers[i:i + _BSJP_FETCH_BATCH]
        symbols = [t + ".JK" for t in batch]
        data = yf.download(symbols, period="10d", interval="1d", group_by="ticker", threads=True, progress=False)
        for t in batch:
            sym = t + ".JK"
            prev_close = cut_loss_by_ticker.get(t)
            if not prev_close:
                continue
            try:
                d = data[sym].dropna(how="all")
            except Exception:
                continue
            if d.empty:
                continue
            close_series = d["Close"]
            match_idx = None
            for idx in range(len(close_series) - 1, -1, -1):  # cari dari PALING BARU mundur -- ambil match terdekat
                val = close_series.iloc[idx]
                if not pd.isna(val) and abs(val - prev_close) < 0.5:
                    match_idx = idx
                    break
            if match_idx is None or match_idx + 1 >= len(close_series):
                continue  # prev_close tidak ketemu di window 10 hari, ATAU match adalah baris TERAKHIR (belum ada hari sesudahnya di data)
            next_close = close_series.iloc[match_idx + 1]
            if pd.isna(next_close):
                continue
            closes[t] = float(next_close)
        if i + _BSJP_FETCH_BATCH < len(tickers):
            time.sleep(0.5)
    return closes


def _fetch_bsjp_universe_snapshot(tickers: list[str]) -> dict:
    """
    MBSS v2 (user request 2026-08-31, live case: panggilan KEDUA /bsjp di
    hari yg sama masih sangat lambat, SSH ikut lag): root cause -- SEBELUM
    ini, tiap panggilan re-fetch 260 hari PENUH utk SELURUH universe,
    padahal 259 dari 260 hari itu STATIS (tidak berubah selama hari bursa
    berjalan). Sekarang dipecah dua: bagian statis (_fetch_bsjp_historical_
    base) di-cache 1x/hari (bsjp_historical_base.json), bagian live
    (_fetch_bsjp_live_bar, period=5 hari SAJA) fetch fresh tiap panggilan.
    Panggilan PERTAMA hari itu masih mahal (base belum ada), tapi panggilan
    KEDUA dst (termasuk auto-scan tiap 15 menit) jauh lebih murah -- cuma
    fetch bagian live yg kecil, base-nya reuse dari cache.
    """
    today = _today_str()
    base_cache = _load_bsjp_base_cache()
    if base_cache.get("trading_day_marker") != today or not base_cache.get("base"):
        base = _fetch_bsjp_historical_base(tickers)
        base_cache = {"trading_day_marker": today, "base": base}
        _save_bsjp_base_cache(base_cache)
    else:
        base = base_cache["base"]
        missing = [t for t in tickers if t not in base]
        if missing:
            extra_base = _fetch_bsjp_historical_base(missing)
            base.update(extra_base)
            base_cache["base"] = base
            _save_bsjp_base_cache(base_cache)

    live = _fetch_bsjp_live_bar(tickers)

    snap = {}
    for t in tickers:
        b = base.get(t)
        l = live.get(t)
        if not b or not l:
            continue
        sma50 = b.get("sma50")
        snap[t] = {
            "current_price": l["current_price"], "high_so_far": l["high_so_far"], "volume_so_far": l["volume_so_far"],
            "prev_close": b["prev_close"], "prev_volume": b["prev_volume"], "vol_ma200": b["vol_ma200"],
            "ret_1d_pct": (l["current_price"] / b["prev_close"] - 1) * 100,
            "above_sma50": (l["current_price"] > sma50) if sma50 else None,
        }
    return snap


def _check_bsjp_criteria(
    snap: dict,
    require_ret1d: bool = True,
    ret1d_min: float | None = None,
    clean_close_mult: float | None = None,
) -> bool:
    """
    4 kriteria (vol_vs_prev/vol_vs_ma200 SELALU wajib penuh, TIDAK PERNAH
    ditoleransi -- lihat catatan MBSS v2 2026-09-01 di atas BSJP_RET1D_
    MIN_PCT utk alasan lengkap redesign Fase 1/Fase 2 & riset pendukung).

    require_ret1d=False: skip gate ret_1d sama sekali (tak dipakai lagi
    produksi sejak live case MNCN -- lihat UPDATE 2026-09-02 di atas
    BSJP_RECHECK_RET1D_MIN_PCT, TAPI param ini TETAP ada utk fleksibilitas
    riset). ret1d_min: override BSJP_RET1D_MIN_PCT -- FASE 1 pakai
    BSJP_SHORTLIST_RET1D_MIN_PCT=1.0 (hampir tanpa syarat), FASE 2 pakai
    BSJP_RECHECK_RET1D_MIN_PCT=1.0 (floor kecil, skrg BUKAN lagi ret_1d
    EOD basi tapi ret_1d_pct LIVE/intraday yg SAMA field-nya, dicek ULANG
    tiap siklus -- cukup utk menyaring sideways murni spt MNCN, TIDAK
    seketat 15% lama).
    clean_close_mult: override BSJP_CLEAN_CLOSE_MULT (FASE 1 -> BSJP_
    SHORTLIST_CLEAN_CLOSE_MULT=1.05; FASE 2 SENGAJA TETAP default/None ->
    1.01 ketat -- kalibrasi per-checkpoint nunjuk clean_close ketat MENANG
    di P(EOD masih clean) & touch-rate Day+1 sungguhan, melonggarkannya
    utk Fase 2 justru mengencerkan kualitas).
    """
    ret_1d = snap.get("ret_1d_pct")
    prev_volume = snap.get("prev_volume")
    vol_ma200 = snap.get("vol_ma200")
    volume_so_far = snap.get("volume_so_far")
    current_price = snap.get("current_price")
    high_so_far = snap.get("high_so_far")
    if None in (ret_1d, prev_volume, vol_ma200, volume_so_far, current_price, high_so_far):
        return False
    if require_ret1d:
        effective_ret1d_min = ret1d_min if ret1d_min is not None else BSJP_RET1D_MIN_PCT
        if ret_1d <= effective_ret1d_min:
            return False
    if not prev_volume or volume_so_far <= BSJP_VOL_VS_PREV_DAY_MULT * prev_volume:
        return False
    if not vol_ma200 or volume_so_far <= BSJP_VOL_VS_MA200_MULT * vol_ma200:
        return False
    effective_clean_close_mult = clean_close_mult if clean_close_mult is not None else BSJP_CLEAN_CLOSE_MULT
    if not current_price or high_so_far >= effective_clean_close_mult * current_price:
        return False
    return True


def _build_bsjp_message(ticker: str, snap: dict) -> str:
    current_price = snap["current_price"]
    tp1_price = current_price * (1 + BSJP_TP1_MEDIAN_GAP_PCT / 100.0)
    vol_vs_prev = snap["volume_so_far"] / max(snap["prev_volume"], 1.0)
    return (
        f"BSJP\n"
        f"🔥 {ticker} BUY POWER KUAT — vol {vol_vs_prev:.1f}x kemarin, harga dekat high hari ini\n"
        f"Harga sekarang: {current_price:,.0f}\n"
        f"TP1 (estimasi): {tp1_price:,.0f}\n"
        f"BELI SORE INI. Jual besok pagi begitu TP1 tersentuh."
    )


def _build_bsjp_watch_message(ticker: str, snap: dict) -> str:
    """
    Tier 2 (MBSS v2, user request 2026-09-02) -- lolos Fase 1 (clean_close
    <=1.05) & trend naik (above_sma50), TAPI GAGAL Fase 2 ketat (clean_close
    <1.01). Confidence lebih rendah -- lihat catatan lengkap di atas
    BSJP_WATCH_TP_GAP_PCT utk backtest & angka.
    """
    current_price = snap["current_price"]
    tp_price = current_price * (1 + BSJP_WATCH_TP_GAP_PCT / 100.0)
    vol_vs_prev = snap["volume_so_far"] / max(snap["prev_volume"], 1.0)
    pullback_pct = (snap["high_so_far"] / current_price - 1) * 100
    return (
        f"BSJP WATCH (Tier 2 — confidence lebih rendah)\n"
        f"🔶 {ticker} — vol {vol_vs_prev:.1f}x kemarin, trend naik, TAPI sudah {pullback_pct:.1f}% dari high hari ini (bukan clean close ketat)\n"
        f"Harga sekarang: {current_price:,.0f}\n"
        f"TP (estimasi, historis ~{BSJP_WATCH_TOUCH3_HISTORICAL_PCT:.0f}% touch Day+1): {tp_price:,.0f}\n"
        f"⚠️ Bukan alert utama -- backtest: peluang touch TP kecil (~2-3%) mendekati alert utama, TAPI peluang winner besar jauh lebih rendah. Cocok utk TP cepat, bukan incar gain besar."
    )


async def run_bsjp_shortlist_scan(tickers: list[str]) -> list[dict]:
    """
    FASE 1 -- scan SELURUH universe thd 4 kriteria, simpan yg lolos sbg
    shortlist utk di-recheck Fase 2. Return list kandidat (dict lengkap
    termasuk snapshot). Dipanggil DUA cara (MBSS v2, user request
    2026-08-31): manual (/bsjp) DAN otomatis (run_bsjp_shortlist_scan_auto,
    JobQueue tiap 15 menit 09:00-15:50) -- lihat catatan BSJP_SHORTLIST_
    SCAN_INTERVAL_SEC di atas utk alasan kenapa manual-only tidak cukup.
    """
    snapshot = await _fetch_with_timeout(_fetch_bsjp_universe_snapshot, tickers, timeout=150, default={})
    passed = [
        {"ticker": t, **snap} for t, snap in snapshot.items()
        if _check_bsjp_criteria(snap, ret1d_min=BSJP_SHORTLIST_RET1D_MIN_PCT, clean_close_mult=BSJP_SHORTLIST_CLEAN_CLOSE_MULT)
    ]

    state = _load_bsjp_state()
    today = _today_str()
    if state.get("trading_day_marker") != today:
        # MBSS v2 (user request 2026-09-01, live case "nailunn" BSJP list --
        # 14/14 checkable ticker SEMUA sudah lolos 4 kriteria PENUH KEMARIN
        # sebelum direkomendasikan lagi hari ini): backtest 2thn/576 ISSI
        # (chronological 70/30, validasi, n=347): ticker lolos BSJP PENUH
        # hari T py touch>=3/5/6/10% hari T+1 = 69.5/65.4/63.4/58.2% (vs
        # baseline 29.5/17.3/13.6/6.0%) -- edge KUAT, berdiri sendiri TANPA
        # syarat tambahan hari T+1. User EKSPLISIT: BUKAN continuation/tahan
        # posisi lebih lama (nama "Beli Sore Jual Pagi" py 1-malam hold yg
        # tidak boleh diubah) -- cukup pastikan ticker kemarin TIDAK
        # TERLEWAT dari kandidat pool hari ini. Tidak ada threshold/alert/
        # exit BARU -- ticker ini masuk shortlist SAMA PERSIS spt kandidat
        # lain, tetap lewat Fase 2 recheck STRICT & alert final standar yg
        # SAMA SEKALI tidak berubah ("BELI SORE INI, jual besok pagi").
        yesterday_alerted = state.get("alerted", []) if state.get("trading_day_marker") else []
        state = {
            "trading_day_marker": today,
            "shortlist": sorted(set(yesterday_alerted)),
            "alerted": [],
            "watch_alerted": [],  # Tier 2 (2026-09-02) -- dedup terpisah dari alert utama
        }
    state["shortlist"] = sorted(set(state.get("shortlist", [])) | {c["ticker"] for c in passed})
    state.setdefault("alerted", [])
    state.setdefault("watch_alerted", [])
    _save_bsjp_state(state)
    return passed


def _build_bsjp_shortlist_new_message(ticker: str, snap: dict) -> str:
    vol_vs_prev = snap["volume_so_far"] / max(snap["prev_volume"], 1.0)
    vol_vs_ma200 = snap["volume_so_far"] / max(snap["vol_ma200"], 1.0)
    return (
        f"BSJP SHORTLIST (Fase 1 otomatis)\n"
        f"🌆 {ticker} baru masuk shortlist — {snap['current_price']:,.0f} ({snap['ret_1d_pct']:+.1f}%)\n"
        f"Vol {vol_vs_prev:.1f}x kemarin | {vol_vs_ma200:.1f}x MA200\n"
        f"⚠️ BUKAN alert entry -- akan di-recheck live 09:30-15:50 WIB, alert final (dgn TP1) baru dikirim kalau MASIH lolos semua kriteria."
    )


async def run_bsjp_shortlist_scan_auto() -> dict:
    """
    MBSS v2 (user request 2026-08-31, live case BALI/KICI lolos shortlist
    tapi sudah +25%/+24.4% -- practically ARA, tidak bisa dibeli lagi):
    Fase 1 OTOMATIS, JobQueue tiap BSJP_SHORTLIST_SCAN_INTERVAL_SEC
    (no-op murah di luar jendela 09:00-15:50 WIB). SEBELUMNYA Fase 1 CUMA
    manual-trigger (/bsjp) -- kalau user baru cek siang/sore, kandidat yg
    nembus threshold pagi2 sudah lanjut lari ke ARA sebelum sempat
    ketahuan (lihat riset room-to-ARA di atas). Checking lebih SERING &
    lebih PAGI menambah room riil, INDEPENDEN dari BSJP_RET1D_MIN_PCT
    itu sendiri -- dua perbaikan ini saling melengkapi, bukan alternatif.

    Kirim notifikasi HANYA utk ticker BARU masuk shortlist (dedup thd
    shortlist SEBELUM scan ini dimulai) -- bukan re-broadcast seluruh
    shortlist tiap 15 menit (spam).
    """
    summary = {"skipped_reason": None, "scanned": 0, "new_shortlist": 0}
    now_wib = datetime.datetime.now(core.WIB)
    if now_wib.weekday() >= 5:
        summary["skipped_reason"] = "weekend"
        return summary
    if not (BSJP_SHORTLIST_SCAN_WINDOW_START <= now_wib.time() <= BSJP_RECHECK_WINDOW_END):
        summary["skipped_reason"] = "outside_window"
        return summary
    if await asyncio.to_thread(core.is_idx_market_holiday_today):
        summary["skipped_reason"] = "holiday"
        return summary
    if not is_scan_alert_enabled():
        summary["skipped_reason"] = "toggled_off"
        return summary

    import engine.nightly as nightly_engine  # import lokal -- hindari circular import di level modul
    scored = nightly_engine.load_daily_scan_cache()
    if not scored:
        summary["skipped_reason"] = "no_cache"
        return summary
    universe = sorted(scored.keys())

    state_before = _load_bsjp_state()
    today = _today_str()
    shortlist_before = set(state_before.get("shortlist", [])) if state_before.get("trading_day_marker") == today else set()

    passed = await run_bsjp_shortlist_scan(universe)
    summary["scanned"] = len(universe)

    new_tickers = sorted({c["ticker"] for c in passed} - shortlist_before)
    if new_tickers:
        bot = _get_shared_bot()
        passed_by_ticker = {c["ticker"]: c for c in passed}
        for t in new_tickers:
            msg = _build_bsjp_shortlist_new_message(t, passed_by_ticker[t])
            if bot is not None:
                await core.safe_reply(bot, msg, chat_id=core.TELEGRAM_CHAT_ID)
            else:
                print(f"[NO TELEGRAM TOKEN] {msg}")
            summary["new_shortlist"] += 1

    print(f"✅ BSJP shortlist scan (auto): {summary['scanned']} ticker discan, {summary['new_shortlist']} baru masuk shortlist.")
    return summary


async def run_bsjp_recheck_once() -> dict:
    """
    FASE 2 (JobQueue, tiap BSJP_RECHECK_INTERVAL_SEC, no-op murah di luar
    jendela BSJP_RECHECK_WINDOW_START-END): re-cek HANYA shortlist Fase 1,
    kirim alert final ke ticker yg MASIH lolos SEMUA 4 kriteria & belum
    pernah dialert hari ini (dedup via state["alerted"]).
    """
    now_wib = datetime.datetime.now(core.WIB)
    if not (BSJP_RECHECK_WINDOW_START <= now_wib.time() <= BSJP_RECHECK_WINDOW_END):
        return {"checked": 0, "alerted": 0}

    state = _load_bsjp_state()
    today = _today_str()
    if state.get("trading_day_marker") != today:
        return {"checked": 0, "alerted": 0}  # belum /bsjp hari ini -- tidak ada shortlist utk di-recheck

    alerted_already = set(state.get("alerted", []))
    shortlist = [t for t in state.get("shortlist", []) if t not in alerted_already]
    if not shortlist:
        return {"checked": 0, "alerted": 0}

    snapshot = await _fetch_with_timeout(_fetch_bsjp_universe_snapshot, shortlist, default={})
    bot = _get_shared_bot()

    n_alerted = 0
    n_watch_alerted = 0
    alerted_list = state.setdefault("alerted", [])
    watch_alerted_already = set(state.get("watch_alerted", []))
    watch_alerted_list = state.setdefault("watch_alerted", [])
    fired = []
    fired_watch = []
    for t in shortlist:
        snap = snapshot.get(t)
        if not snap:
            continue
        if _check_bsjp_criteria(snap, ret1d_min=BSJP_RECHECK_RET1D_MIN_PCT):
            msg = _build_bsjp_message(t, snap)
            if bot is not None:
                await core.safe_reply(bot, msg, chat_id=core.TELEGRAM_CHAT_ID)
            else:
                print(f"[NO TELEGRAM TOKEN] {msg}")
            alerted_list.append(t)
            n_alerted += 1
            # MBSS v2 (user request -- lock HANYA saat alert BENERAN fire, bukan
            # di shortlist Fase 1 yg belum tentu konfirmasi): cut_loss pakai
            # prev_close (thesis "buy power" gagal kalau balik ke bawah closing
            # kemarin) -- proksi wajar, bukan angka backtest terpisah.
            fired.append({
                "ticker": t, "current_price": snap["current_price"],
                # ret_1d_pct (2026-09-02) -- mengalir otomatis ke feature_
                # snapshot lock_daily_daytrade_picks (sudah ada field ini di
                # sana), dipakai build_bsjp_tp_plan_message utk pilih tier
                # TP1/TP2 individualized -- lihat BSJP_TP_TIERS.
                "ret_1d_pct": snap["ret_1d_pct"],
                "targets": {"tp_1": snap["current_price"] * (1 + BSJP_TP1_MEDIAN_GAP_PCT / 100.0), "cut_loss": snap["prev_close"]},
            })
        elif (
            t not in watch_alerted_already
            and snap.get("above_sma50")
            and _check_bsjp_criteria(snap, ret1d_min=BSJP_RECHECK_RET1D_MIN_PCT, clean_close_mult=BSJP_SHORTLIST_CLEAN_CLOSE_MULT)
        ):
            # Tier 2 "BSJP WATCH" (2026-09-02) -- gagal Fase 2 ketat, TAPI
            # masih lolos wide net Fase 1 (clean_close<=1.05) & trend naik.
            # Lihat catatan lengkap di atas BSJP_WATCH_TP_GAP_PCT.
            msg = _build_bsjp_watch_message(t, snap)
            if bot is not None:
                await core.safe_reply(bot, msg, chat_id=core.TELEGRAM_CHAT_ID)
            else:
                print(f"[NO TELEGRAM TOKEN] {msg}")
            watch_alerted_list.append(t)
            n_watch_alerted += 1
            # MBSS v2 (user request 2026-09-02 -- "BSJP WATCH tetap masuk
            # /bsjp tp"): lock TERPISAH (source="bsjp_watch", BUKAN "bsjp")
            # -- confidence beda, TP beda (moderat saja, BSJP_WATCH_TP_GAP_
            # PCT), jangan campur dgn alert utama supaya /bsjp tp bisa
            # tampilkan keduanya dgn label yg jelas berbeda.
            fired_watch.append({
                "ticker": t, "current_price": snap["current_price"],
                "ret_1d_pct": snap["ret_1d_pct"],
                "targets": {"tp_1": snap["current_price"] * (1 + BSJP_WATCH_TP_GAP_PCT / 100.0), "cut_loss": snap["prev_close"]},
            })

    _save_bsjp_state(state)
    if fired:
        try:
            await asyncio.to_thread(core.lock_daily_daytrade_picks, fired, "bsjp")
        except Exception as e:
            print(f"⚠️ Gagal mengunci picks BSJP untuk /winrate: {e}")
    if fired_watch:
        try:
            await asyncio.to_thread(core.lock_daily_daytrade_picks, fired_watch, "bsjp_watch")
        except Exception as e:
            print(f"⚠️ Gagal mengunci picks BSJP WATCH untuk /winrate: {e}")
    return {"checked": len(shortlist), "alerted": n_alerted, "watch_alerted": n_watch_alerted}


def build_bsjp_tp_plan_message() -> str:
    """
    /bsjp tp (MBSS v2, user request 2026-09-02, REVISI setelah live case
    KKES) -- panduan jual pre-open esok pagi utk SEMUA ticker yang lolos
    Fase 2 (source="bsjp") ATAU Tier 2 WATCH (source="bsjp_watch") hari
    bursa terakhir. ANCHOR = closing HARI ALERT (fetch live, BUKAN harga
    alert-fire) -- user: "sesuai disiplin BSJP yg memang seharusnya beli di
    sore hari (>15:30)", DAN backtest presisi CORRECTED konfirmasi closing
    anchor genuinely lebih baik (bukan cuma lebih benar disiplin) -- lihat
    catatan panjang di atas BSJP_TP_TIERS. Entry ALERT-FIRE (dari "tp1"
    tersimpan, direkonstruksi) TETAP dihitung & ditampilkan sbg REFERENSI
    (drift context), TAPI bukan lagi anchor TP.

    FADE CAP: kalau closing sudah >=5% di bawah harga alert-fire (drift),
    TP2 stretch DIHILANGKAN & TP1 diturunkan ke BSJP_FADE_TP1_GAP_PCT --
    backtest nunjuk grup ini literally 0% pernah tembus >=8% dari closing.
    Live case: KKES (avg cost user 111, closing 94-95, drift ~-13%) --
    dgn desain lama TP1/TP2 dari entry (109) nunjuk 111/116 yg TIDAK PERNAH
    kejadian di backtest (0/11). Dgn fade cap: TP1 = closing*1.025 (~97,
    ~55% historis), TIDAK ada TP2.

    PENTING soal tanggal (bug yg ketemu & DIPERBAIKI 2026-09-02, sebelum
    sempat dipakai produksi): jangan panggil ulang get_current_trading_day_
    close_marker() DI SINI utk cari pick_date target -- alert Fase 2 SELALU
    dikunci SAAT market masih buka (sebelum 16:30 WIB), jadi marker SAAT
    lock masih mundur 1 hari. Fix: cari pick_date TERBARU yang genuinely
    ada di history (gabungan kedua source) -- robust terlepas kapan /bsjp
    tp dipanggil.
    """
    history = core.load_daytrade_picks_history()
    bsjp_picks = [p for p in history if p.get("source") in ("bsjp", "bsjp_watch") and p.get("pick_date")]
    if not bsjp_picks:
        return "📋 Belum pernah ada BSJP (Fase 2 maupun WATCH) -- tidak ada panduan TP."

    target_date = max(p["pick_date"] for p in bsjp_picks)
    picks = [p for p in bsjp_picks if p["pick_date"] == target_date]
    main_picks = [p for p in picks if p["source"] == "bsjp"]
    watch_picks = [p for p in picks if p["source"] == "bsjp_watch"]

    staleness_note = ""
    try:
        target_dt = datetime.datetime.strptime(target_date, "%Y-%m-%d").date()
        days_old = (datetime.datetime.now(core.WIB).date() - target_dt).days
        if days_old > 2:
            staleness_note = f"\n⚠️ Pick TERBARU dari {target_date} ({days_old} hari lalu) -- kemungkinan TIDAK ada alert baru-baru ini, ini BUKAN rencana malam ini.\n"
    except Exception:
        pass

    closes = _fetch_bsjp_closing_prices(picks)

    lines = [f"🌅 BSJP TP PLAN — {target_date} ({len(main_picks)} alert utama, {len(watch_picks)} WATCH){staleness_note}\n"]

    def _entry_and_close(p: dict, tp1_multiplier_pct: float) -> tuple[float, float | None] | None:
        # MBSS v2 (bug ketemu & diperbaiki 2026-09-02, live case: /bsjp tp
        # tampil kosong -- lock_daily_daytrade_picks TIDAK PERNAH simpan
        # current_price mentah, cuma "tp1"/"cut_loss" flat di top level.
        # tp1 tersimpan SELALU = entry * (1+tp1_multiplier_pct/100) --
        # deterministik, entry ASLI direkonstruksi persis dari situ.
        tp1_stored = p.get("tp1")
        if not tp1_stored:
            return None
        entry = tp1_stored / (1 + tp1_multiplier_pct / 100.0)
        close_px = closes.get(p["ticker"])
        return entry, close_px

    if main_picks:
        lines.append("🔥 ALERT UTAMA (BELI SORE INI)")
        for p in sorted(main_picks, key=lambda x: x["ticker"]):
            res = _entry_and_close(p, BSJP_TP1_MEDIAN_GAP_PCT)
            if res is None:
                continue
            entry, close_px = res
            cut_loss = p.get("cut_loss")

            if close_px is None:
                # Closing belum berhasil di-fetch -- fallback entry-anchor
                # LAMA drpd tidak nampilkan apa-apa, TAPI beri label jelas.
                ret1d_at_entry = (p.get("feature_snapshot") or {}).get("ret_1d_pct")
                tp1_gap, tp1_hit, tp2_gap, tp2_hit = _bsjp_tp_for_ret1d(ret1d_at_entry)
                line = (
                    f"{p['ticker']} — entry alert-fire {entry:,.0f} (closing GAGAL di-fetch, pakai entry sbg fallback)\n"
                    f"  TP1: {entry * (1 + tp1_gap / 100.0):,.0f} (+{tp1_gap:.1f}%)   TP2: {entry * (1 + tp2_gap / 100.0):,.0f} (+{tp2_gap:.1f}%)"
                )
            else:
                drift = (close_px / entry - 1) * 100
                drift_label = f"{drift:+.1f}%"
                if drift <= BSJP_FADE_DRIFT_THRESHOLD_PCT:
                    tp1 = close_px * (1 + BSJP_FADE_TP1_GAP_PCT / 100.0)
                    line = (
                        f"{p['ticker']} — closing {close_px:,.0f} (entry alert-fire {entry:,.0f}, drift {drift_label} — FADE, "
                        f"TP2 DIHILANGKAN, backtest 0% tembus >=8% dari closing utk grup ini)\n"
                        f"  TP1 (moderat, ~{BSJP_FADE_TP1_HISTORICAL_HIT_PCT:.0f}% historis touch Day+1): {tp1:,.0f} (+{BSJP_FADE_TP1_GAP_PCT:.1f}%)"
                    )
                else:
                    ret1d_at_entry = (p.get("feature_snapshot") or {}).get("ret_1d_pct")
                    tp1_gap, tp1_hit, tp2_gap, tp2_hit = _bsjp_tp_for_ret1d(ret1d_at_entry)
                    tp1 = close_px * (1 + tp1_gap / 100.0)
                    tp2 = close_px * (1 + tp2_gap / 100.0)
                    line = (
                        f"{p['ticker']} — closing {close_px:,.0f} (entry alert-fire {entry:,.0f}, drift {drift_label})\n"
                        f"  TP1 (moderat, ~{tp1_hit:.0f}% historis touch Day+1): {tp1:,.0f} (+{tp1_gap:.1f}%)\n"
                        f"  TP2 (stretch, ~{tp2_hit:.0f}% historis touch Day+1): {tp2:,.0f} (+{tp2_gap:.1f}%)"
                    )
            if cut_loss:
                line += f"\n  Cut loss: {cut_loss:,.0f}"
            lines.append(line)

    if watch_picks:
        lines.append("🔶 WATCH (Tier 2 — confidence lebih rendah, TP moderat saja)")
        for p in sorted(watch_picks, key=lambda x: x["ticker"]):
            res = _entry_and_close(p, BSJP_WATCH_TP_GAP_PCT)
            if res is None:
                continue
            entry, close_px = res
            cut_loss = p.get("cut_loss")
            anchor_px = close_px if close_px is not None else entry
            tp1 = anchor_px * (1 + BSJP_WATCH_TP_GAP_PCT / 100.0)
            close_label = f"closing {close_px:,.0f}" if close_px is not None else f"entry alert-fire {entry:,.0f} (closing gagal di-fetch)"
            line = (
                f"{p['ticker']} — {close_label}\n"
                f"  TP (moderat, ~{BSJP_WATCH_TOUCH3_HISTORICAL_PCT:.0f}% historis touch Day+1): {tp1:,.0f} (+{BSJP_WATCH_TP_GAP_PCT:.1f}%)"
            )
            if cut_loss:
                line += f"\n  Cut loss: {cut_loss:,.0f}"
            lines.append(line)

    lines.append(
        "\n⚠️ TP1/TP2 dihitung dari CLOSING hari alert (sesuai disiplin BSJP beli sore >15:30), BUKAN dari harga "
        "alert-fire lagi -- backtest presisi (1m riil, 5 hari bursa) konfirmasi closing anchor lebih akurat, "
        "terutama utk ticker yg fade jauh dari harga alert-fire. FADE (drift closing vs alert-fire <=-5%) hanya "
        "dapat TP1 moderat, TIDAK ada TP2 -- backtest 0% tembus di atas 7-8% dari closing utk grup itu. "
        "Sampel per tier masih kecil (11-42), treat sbg directional."
    )
    return "\n\n".join(lines)


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
# tidak pernah terlihat). Awalnya informational-only tag (dites thd 344
# ticker/19 hari, n=4003 gap-day event, further-gain median +16.5%/66.7%
# EOD positif di bucket 5-12% "holds" tapi n=15 kecil) -- lihat riwayat lama
# utk detail. RETIRED sbg mekanisme SENDIRI 2026-08-30, DIGABUNG ke
# GAP_REBOUND_MIN_PCT/MAX_PCT di atas (satu range gap, bukan dua yg
# overlap) -- "holds" checknya (GAP_UP_HOLD_CHECK_BARS/MAX_DROP_PCT di
# bawah) tetap dipertahankan, dipakai _detect_gap_hold (mekanisme KEDUA,
# lihat GAP_HOLD_MIN_VOL_RATIO_PARTIAL).
GAP_UP_HOLD_CHECK_BARS = 5
GAP_UP_HOLD_MAX_DROP_PCT = -3.0
GAP_HOLD_SESSION_MINUTES = 330  # aproksimasi menit sesi IDX (09:00-16:00 dikurangi istirahat ~90 menit) -- basis normalisasi vol_ratio_partial

# MBSS v2 (user request 2026-08-29 -- "DAY TRADE" TP1/NO CHASE): backtest 1m
# riil (174 ticker gap-candidate, 27 hari bursa, n=97 event gap 5-12% dgn
# data 1m lengkap). TP1=+4% dari OPEN: tersentuh 51.5% event (>50%, level
# TERJAUH yg masih layak -- +5% turun ke 45.4%). NO_CHASE=+2% dari open:
# level PERTAMA yg dites, degradasi entry-ke-closing SUDAH kelihatan di sini
# (median return dari entry ke closing -1.12%, cuma 38.2% positif) --
# TIDAK monoton membaik di level lebih rendah (belum dites <2%), tapi +2%
# adalah titik test pertama yg sudah loss-making, jadi dipakai sbg cutoff
# konservatif. (HC_GAP_WATCH dulu me-reuse angka yg sama utk populasinya
# sendiri -- RETIRED 2026-08-30, lihat engine/nightly.py.) Sekarang dipakai
# _build_gap_hold_message (PENGGANTI _build_gap_up_message lama).
DAY_TRADE_GAP_TP1_PCT = 4.0
DAY_TRADE_NO_CHASE_PCT = 2.0


def _detect_gap_hold(bars: pd.DataFrame, prev_close: float, avg_vol_20d: float | None) -> dict | None:
    """
    Mekanisme KEDUA gap signal (MERGE 2026-08-30, PENGGANTI _detect_gap_up
    lama -- lihat catatan GAP_HOLD_MIN_VOL_RATIO_PARTIAL utk riset
    lengkap). Beda dari versi lama: (1) TIDAK fire kalau holds=False sama
    sekali (dulu tetap fire dgn catatan "belum jelas bertahan" -- versi
    lama TERBUKTI give-back parah, jadi sekarang genuinely gate, bukan
    cuma informational), (2) WAJIB lolos floor volume vol_ratio_partial
    (missing avg_vol_20d = TIDAK fire, bukan neutral -- filter volume
    justru INTI perbaikan mekanisme ini, sama disiplin dgn daytrade_hc_
    confidence.compute_tp1 "fitur tak lengkap -> None").
    """
    if bars.empty or not prev_close or prev_close <= 0:
        return None
    day_open = float(bars["Open"].astype(float).iloc[0])
    if day_open <= 0:
        return None
    gap_pct = (day_open - prev_close) / prev_close * 100
    if gap_pct < GAP_REBOUND_MIN_PCT or gap_pct >= GAP_REBOUND_MAX_PCT:
        return None
    check_bars = bars.iloc[:GAP_UP_HOLD_CHECK_BARS]
    if len(check_bars) < GAP_UP_HOLD_CHECK_BARS:
        return None  # tunggu cukup bar dulu sebelum menilai "holds" -- first-touch tetap terjaga via state gap_hold_sent
    low_so_far = float(check_bars["Low"].astype(float).min())
    holds = (low_so_far - day_open) / day_open * 100 >= GAP_UP_HOLD_MAX_DROP_PCT
    if not holds:
        return None
    if not avg_vol_20d:
        return None
    vol_window = float(check_bars["Volume"].fillna(0).astype(float).sum())
    vol_ratio_partial = vol_window / (avg_vol_20d / GAP_HOLD_SESSION_MINUTES * GAP_UP_HOLD_CHECK_BARS)
    if vol_ratio_partial < GAP_HOLD_MIN_VOL_RATIO_PARTIAL:
        return None
    current_price = float(bars["Close"].astype(float).iloc[-1])
    return {"gap_pct": gap_pct, "day_open": day_open, "current_price": current_price, "vol_ratio_partial": vol_ratio_partial}


def _build_gap_hold_message(ticker: str, detection: dict, conviction: str = "", risk_tags: list[str] | None = None) -> str:
    day_open = detection["day_open"]
    tp1 = day_open * (1 + DAY_TRADE_GAP_TP1_PCT / 100.0)
    no_chase = day_open * (1 + DAY_TRADE_NO_CHASE_PCT / 100.0)
    conviction_line = f"\n{conviction}" if conviction else ""
    risk_lines = "".join(f"\n{tag}" for tag in (risk_tags or []))
    return (
        f"DAY TRADE\n"
        f"🌅 {ticker} GAP-UP +{detection['gap_pct']:.1f}% HOLDS (vol {detection['vol_ratio_partial']:.1f}x laju normal)\n"
        f"open : {day_open:,.0f}\n"
        f"Now  : {detection['current_price']:,.0f}\n"
        f"TP 1 : {tp1:,.0f}\n"
        f"NO CHASE > {no_chase:,.0f}"
        f"{conviction_line}{risk_lines}"
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
def _conviction_tag(ticker: str, fcm_watchlist: dict, pre_continuation_watchlist: dict) -> str:
    if ticker in fcm_watchlist:
        w = fcm_watchlist[ticker]
        return f"✅ ADA SETUP: FRESH CROSS MOMENTUM ({w['cross_days_ago']}hr lalu, pre-cross +{w['ret10_pre_cross_pct']:.0f}%)"
    if ticker in pre_continuation_watchlist:
        w = pre_continuation_watchlist[ticker]
        lane_label = {
            "PRE": "PRE-CROSS (SDT)", "CONTINUATION": "CONTINUATION (HC)", "VALIDATION": "VALIDATION (HC)",
            "MOMENTUM_EXTENDED": "MOMENTUM EXTENDED (Swing)", "EARLY_VALIDATION": "EARLY VALIDATION (pra-VALIDATION)", "LATE_VALIDATION": "LATE VALIDATION (pasca-window)",
        }.get(w["lane"], w["lane"])
        return f"✅ ADA SETUP: {lane_label}, {w['detail']}"
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
    return f"🧱 DITOLAK DANGER GATE malam sebelumnya (danger {danger_str})"


def _chase_risk_tag(current_price: float, day_open: float) -> str | None:
    """
    Extracted dari _risk_tags -- reusable tanpa perlu bars/ref/danger_lookup
    penuh (dipakai conviction sweep juga, bukan cuma Alert A/B/gap-up).
    """
    if not day_open or day_open <= 0:
        return None
    gain_from_open = (current_price - day_open) / day_open * 100
    if gain_from_open >= CHASE_WARN_GAIN_FROM_OPEN_PCT:
        return f"⚠️ CHASE RISK: sudah +{gain_from_open:.1f}% dari open"
    return None


def _fail_signal_tag(current_price: float, day_open: float, day_high_so_far: float | None, lane_tag: str) -> str | None:
    """
    MBSS v2 (user request 2026-08-29, live case PDES/TMPO) -- lihat catatan
    FAIL_SIGNAL_EXTENDED_LANES di atas utk backtest lengkap. Proxy LIVE:
    harga SEMPAT naik dari open (High so far > open) tapi SEKARANG sudah
    balik ke/di bawah open -- deteksi dini, bukan nunggu closing -10% confirm.
    """
    if lane_tag not in FAIL_SIGNAL_EXTENDED_LANES:
        return None
    if not day_open or day_open <= 0 or day_high_so_far is None:
        return None
    if day_high_so_far <= day_open or current_price > day_open:
        return None
    return f"🔻 FAIL SIGNAL: sempat naik ke {day_high_so_far:,.0f} tapi sekarang sudah balik ke/di bawah open ({day_open:,.0f})"


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
        chase_tag = _chase_risk_tag(current_price, day_open)
        if chase_tag:
            tags.append(chase_tag)
    if now_wib.time() < RISKY_TIME_WINDOW_END:
        tags.append(f"⚠️ JAM BERISIKO: fire sebelum {RISKY_TIME_WINDOW_END.strftime('%H:%M')}")
    if ref is not None:
        avg_vt = ref.get("avg_value_traded_20d")
        if avg_vt is not None and avg_vt < SCANALERT_LIQUIDITY_WARN_FLOOR_IDR:
            tags.append(f"⚠️ LIKUIDITAS TIPIS: avg value traded 20hr Rp{avg_vt/1e9:.2f}M")
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
# MBSS v2 (user request 2026-08-31): 15% -> 5% -- sweep granular (cross_
# days_ago<=2, chronological 70/30 validasi) nunjuk 5% titik terbaik DALAM
# rentang 5-10% yg diminta user (fwd_ret_d3 mean +0.21%/pos 41.0%, terbaik
# di rentang itu -- 7-8% justru DIP ke negatif sebelum pulih lagi di 9-12%,
# BUKAN monoton bersih). touch3%(3 hari)=59.6% di 5% vs 67.5% di 15% lama
# (trade-off nyata, bukan longgar tanpa biaya), tapi ~4x kandidat (1402 vs
# 354) & masih jauh di atas baseline pasar (49.5%). Model lane_confidence
# FCM DIRETRAIN thd populasi >5% baru (lihat lane_confidence_constants.json,
# AUC 0.568-0.605, Brier konsisten lebih baik dari baseline) -- model lama
# dilatih khusus populasi >15%, ekstrapolasi ke 5-15% kurang presisi.
MACD_FRESH_CROSS_MOMENTUM_RET10_PRE_MIN = 5.0

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

# MBSS v2 (user request 2026-08-31, live case SPTO/WIFI/MEDC/HRUM/BANK --
# fresh cross genuine tapi tak masuk lane manapun): backtest 2thn/576 ISSI
# (chronological 70/30, validasi) nunjuk populasi gain_since_cross 2-3%
# (cross_days_ago 1-5, TANPA gate dist_to_sma20 -- beda dari CONTINUATION/
# VALIDATION) py peluang TRANSISI ke zona gain_since_cross>=4% (zona
# tervalidasi py edge) dlm 1-2 hari bursa berikutnya = 33.2-33.4%, vs
# baseline pasar acak cuma 13.4% (>2x lipat). Pita 0-2% DITOLAK (10.7%/
# 18.8%, TIDAK terselamatkan filter sekunder vol_ratio maupun RSI -- sudah
# dites, tetap lemah) -- HANYA 2-3% yg lolos. Filosofi user: "kita punya
# mekanisme sweep candidate all setup, maka lebih banyak kandidat
# sebenarnya lebih baik" -- lane ini SENGAJA definisi longgar (TANPA gate
# dist_to_sma20, beda dari CONTINUATION/VALIDATION) krn perannya "jaring
# kandidat AWAL", Conviction Sweep-lah yg konfirmasi belakangan seiring
# harga (mungkin) lanjut naik ke zona VALIDATION. TIDAK didukung engine/
# lane_confidence.py (belum ada model terlatih utk lane baru ini) --
# caller fallback ke CONVICTION_TP_CEILING_PCT statis, SAMA spt FAST_
# RECOVERY/EARLY_RECOVERY.
MACD_EARLY_VALIDATION_GAIN_MIN_PCT = 2.0
MACD_EARLY_VALIDATION_GAIN_MAX_PCT = 3.0

# MBSS v2 (user request 2026-08-31 -- lanjutan riset dead zone "since cross
# 3-6d, kondisi tertentu, target d1/d2/d3 minimal 3%"): dead-zone map penuh
# (cross_days_ago 1-6, SEMUA populasi yg belum tercover lane manapun)
# nunjuk gain_since_cross>=10% di hari 3-6 GENUINELY tak tercover lane apa
# pun (CONTINUATION mentok <10%, MOMENTUM_EXTENDED baru mulai hari 6+
# dgn syarat lain) -- touch>=3% dlm d3 = 68.1% (n=943), TERBAIK dari
# seluruh studi sesi ini, bahkan lebih tinggi dari FCM sendiri. Gain 3-6%
# (fails gate dist_to_sma20>=12%) JUGA py edge (touch d3=55.7%, mean fwd
# +0.54%). User usul gabung jadi SATU rule sederhana >=3% (open-ended,
# skip pita 6-10% yg sedikit lebih lemah tapi masih di atas baseline) --
# gain>=3% MERGED: touch d3=61.3% (n=2346) vs baseline 49.5%, tetap solid
# meski pita 6-10% mengencerkan sedikit. TANPA gate dist_to_sma20 (SAMA
# filosofi EARLY_VALIDATION). Model lane_confidence DILATIH (BEDA dari
# EARLY_VALIDATION) -- gain_since_cross py varians besar di pita open-
# ended ini, jadi fitur eksplisit, AUC 0.558-0.572 & Brier konsisten lebih
# baik dari baseline (lihat lane_confidence_constants.json "LATE_VALIDATION").
MACD_LATE_VALIDATION_GAIN_MIN_PCT = 3.0
MACD_LATE_VALIDATION_MIN_CROSS_DAYS_AGO = 3
MACD_LATE_VALIDATION_MAX_CROSS_DAYS_AGO = 6

# MBSS v2 (user request 2026-08-30 -- "sinyal intraday lainnya sudah selaras
# dengan swingtrade, masukkan MOMENTUM_EXTENDED juga"): lane ke-6 /go SWING
# TRADE ini SEBELUMNYA absen dari watchlist live (_get_pre_continuation_
# watchlist di bawah) -- gap murni, bukan disengaja. Konstanta PERSIS
# commands/scan.py go_command/screen_daytrade -- jangan drift. Menambahkan
# ini SEKALIGUS mengaktifkan _fail_signal_tag/FAIL_SIGNAL_EXTENDED_LANES
# (sudah ditulis 2026-08-29, tapi selama ini TIDAK PERNAH match apa pun
# krn tag "MOMENTUM_EXTENDED" belum pernah diproduksi di sini).
MACD_EXTENDED_MIN_CROSS_DAYS_AGO = 6
MACD_EXTENDED_MAX_CROSS_DAYS_AGO = 40
MACD_GAP_SLOPE_Q4_THRESHOLD = 0.3106
MACD_MOMENTUM_RET1D_MIN = 2.5

# MBSS v2 (user request 2026-08-30): HC_GAP_WATCH_MIN_GAP_PCT + _detect_hc_
# gap_watch/_build_hc_gap_watch_message DIHAPUS -- lihat engine/nightly.py
# utk riwayat lengkap kenapa (backtest ulang dgn populasi WR model baru
# membuktikan sinyal ini tidak bertahan sbg compound signal terpisah).

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

# MBSS v2 (user request 2026-08-29 -- live case PDES 7 Agst: gap-up +16% dari
# prev close, TAPI closing -23.3% dari open hari itu, follow-through beberapa
# hari lemah; user juga cite TMPO 12->21 Agst pola serupa): backtest 576
# ISSI/2thn, populasi "sudah extended" (ret5d_trailing>=20% SEBELUM hari ini)
# x "candle hari ini reversal dalam" (close<=-10% dari open) -- n=925, mean
# fwd5d=-2.41% (vs baseline "extended tapi candle normal" n=6332, mean
# fwd5d=+3.78%), P(fwd5d<=-5%) 46.3% vs 34.9%, median worst Low 3hr -9.65%
# vs -5.26%. RSI/volume-decline/consecutive-up-days TIDAK prediktif (dites
# terpisah) -- shape candle hari itu sendiri yang justru sinyal paling kuat
# ditemukan di seluruh eksplorasi risiko sesi ini.
#
# FAIL_SIGNAL di bawah = proxy LIVE dari temuan EOD di atas: deteksi dini
# saat harga baru MULAI kembali ke/di bawah open (bukan nunggu closing -10%
# confirm) -- trade-off sengaja: lebih banyak false positive (bisa rebound
# sebelum closing), krn tujuan tag ini WARNING dini, bukan sinyal exit
# otomatis. Digate ke lane yg SUDAH "extended" by construction (MOMENTUM_
# EXTENDED) -- lane FCM/ABOVE_MOMENTUM/CONTINUATION/VALIDATION BELUM tentu
# "sudah lari jauh" jadi TIDAK match populasi backtest di atas, sengaja
# tidak ikut digate. BSJP_ARA/BSJP_SECOND_WAVE DIHAPUS dari sini (MBSS v2,
# user request 2026-08-29) -- BSJP bukan lagi lane conviction-sweep, sudah
# punya disiplin exit sendiri ("beli sore jual pagi", lihat run_bsjp_
# recheck_once), tidak lewat jalur fail-signal ini lagi.
FAIL_SIGNAL_EXTENDED_LANES = ("MOMENTUM_EXTENDED",)

# MBSS v2 (user request 2026-08-26): floor likuiditas KHUSUS tag Alert A/B/
# gap-up, SENGAJA lebih longgar dari core.LIQUIDITY_FLOOR_VALUE_TRADED_IDR
# (Rp1M, dipakai Danger Gate/HC/SDT) -- scalping intraday genuinely lebih
# toleran thd saham lebih tipis drpd swing/day-trade multi-hari (exposure
# lebih singkat), jadi TIDAK reuse konstanta yang sama. Tidak mengubah
# core.LIQUIDITY_FLOOR_VALUE_TRADED_IDR sama sekali -- floor Rp1M di
# Danger Gate/HC/SDT tetap seperti semula.
SCANALERT_LIQUIDITY_WARN_FLOOR_IDR = 500_000_000


def _get_swing_lane_universe() -> list[str]:
    """
    MBSS v2 (user request 2026-08-31 -- "conviction sweep bukannya
    harusnya cek all tickers di allsetup?"): universe KHUSUS FCM/
    PRE-CONTINUATION/Conviction Sweep -- BEDA dari _get_alert_universe()
    (ticker_whitelist.json, whitelist bulanan TERPISAH, dipakai Alert A/B/
    gap-rebound/gap-hold yg genuinely butuh populasi scalping-friendly
    tervalidasi sendiri, TIDAK disentuh oleh perubahan ini).

    SEBELUMNYA lane SWING TRADE (FCM/CONTINUATION/VALIDATION/MOMENTUM_
    EXTENDED/PRE tier) live-tracking ikut pakai _get_alert_universe(),
    padahal /allsetup & /go (versi EOD, SUMBER KEBENARAN definisi lane2
    ini) pakai universe /eodscan PENUH -- gap nyata, confirmed dari log
    produksi 2026-08-31: 146 ticker ke-exclude "chronically wide-range"
    (proteksi KHUSUS scalping Alert A/B, lihat FROZEN_MEDIAN_RANGE_MAX_PCT
    dkk) TIDAK PERNAH bisa muncul di FCM/PRE-CONTINUATION/Conviction Sweep
    sama sekali, walau /allsetup menampilkannya dgn normal.

    UPDATE (user request 2026-08-31, live case KOTA/IATA/JGLE/YELO/ASLI/
    KICI ke-exclude total, user curiga median wide-range genuinely bias
    POSITIF bukan noise -- backtest sblm ubah): exclusion chronically-wide-
    range DIHAPUS dari swing-lane (TETAP dipertahankan di _get_alert_
    universe() utk gap-rebound/gap-hold/Alert A/B, lihat line ~375 -- itu
    proteksi ASLI yg divalidasi utk scalping horizon, TIDAK disentuh).
    Backtest same-day-exit (60hr, 15m bars, validation split n=1467):
    chronic-wide-range (median_range_60d>=5.5%) justru py UPSIDE same-day
    lebih besar stlh momentum trigger (ret_to_best_after median +2.25% vs
    +1.18% normal, hit-rate 83.4% vs 72.3%, corr +0.256) -- hold-to-close
    naif tetap buruk utk KEDUA grup (~27% hit-rate), TAPI FCM/PRE-
    CONTINUATION/Conviction Sweep tidak pernah hold-to-close naif (selalu
    py TP1/TP2 exit), jadi upside intraday itu genuinely bisa ditangkap.
    Swing-horizon backtest (d1/d3/d5 close-to-close) TETAP nunjuk sedikit
    bias negatif utk grup ekstrem (median_range>=10%, n kecil) -- kalau ke
    depan mau proteksi lagi, arahnya threshold TINGGI (~10%) bukan 5.5%.
    """
    import engine.nightly as nightly_engine  # import lokal -- hindari circular import di level modul
    scored = nightly_engine.load_daily_scan_cache()
    if not scored:
        return []
    return sorted(scored.keys())


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
            r = scoring.compute_factor_scoring(t, include_quote_check=False, skip_live_fundamentals=True, skip_live_refresh=True)
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
    scoring.py), CONTINUATION/VALIDATION (macd_cross_direction bullish,
    cross<=5hr, gain_since_cross 6-10%/3-6%, commands/scan.py high_
    conviction_command), dan MOMENTUM_EXTENDED (cross 6-40hr, gap_slope_3d>=
    Q4, ret_1d>2.5%, commands/scan.py go_command -- ditambah 2026-08-30
    supaya SEMUA 6 lane /go SWING TRADE tercakup live, bukan cuma 5) --
    dihitung ulang di sini via compute_factor_
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
            r = scoring.compute_factor_scoring(t, include_quote_check=False, skip_live_fundamentals=True, skip_live_refresh=True)
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
        # MOMENTUM_EXTENDED (lane ke-6 /go SWING TRADE, ditambah 2026-08-30)
        # -- gate SENDIRI, TIDAK pakai MACD_LANE_DIST_SMA20_MIN_PCT (beda dari
        # CONTINUATION/VALIDATION di bawah, PERSIS commands/scan.py go_command
        # swing_candidates loop -- jangan tambah gate yg tidak ada di sana).
        if (
            r.get("macd_regime") == "ABOVE_CENTERLINE" and r.get("macd_episode_had_volume_breakout") is True
            and r.get("macd_cross_days_ago") is not None
            and MACD_EXTENDED_MIN_CROSS_DAYS_AGO <= r["macd_cross_days_ago"] <= MACD_EXTENDED_MAX_CROSS_DAYS_AGO
            and r.get("macd_gap_slope_3d") is not None and r["macd_gap_slope_3d"] >= MACD_GAP_SLOPE_Q4_THRESHOLD
            and r.get("ret_1d_pct") is not None and r["ret_1d_pct"] > MACD_MOMENTUM_RET1D_MIN
        ):
            watchlist[t] = {
                "lane": "MOMENTUM_EXTENDED", "detail": f"cross {r['macd_cross_days_ago']}hr lalu, ret1d +{r['ret_1d_pct']:.1f}%",
                "dist_to_sma20": r.get("price_vs_sma20_pct"), "pct_b": r.get("pct_b"),
                "gap_slope_3d": r.get("macd_gap_slope_3d"),
            }
            continue
        # EARLY_VALIDATION (lane ke-8, ditambah 2026-08-31): TANPA gate
        # dist_to_sma20 (SENGAJA, lihat catatan MACD_EARLY_VALIDATION_GAIN_
        # MIN_PCT di atas) -- dicek SEBELUM gate sma_dist di bawah supaya
        # tidak ikut ke-block olehnya.
        early_gain = r.get("macd_gain_since_cross_pct")
        if (
            r.get("macd_cross_direction") == "bullish"
            and r.get("macd_cross_days_ago") is not None
            and r["macd_cross_days_ago"] <= MACD_CONTINUATION_MAX_CROSS_DAYS_AGO
            and early_gain is not None
            and MACD_EARLY_VALIDATION_GAIN_MIN_PCT <= early_gain < MACD_EARLY_VALIDATION_GAIN_MAX_PCT
        ):
            watchlist[t] = {
                "lane": "EARLY_VALIDATION", "detail": f"+{early_gain:.1f}% sejak cross",
                "dist_to_sma20": r.get("price_vs_sma20_pct"), "pct_b": r.get("pct_b"),
            }
            continue
        sma_dist = r.get("price_vs_sma20_pct")
        gain = r.get("macd_gain_since_cross_pct")
        cross_days_ago = r.get("macd_cross_days_ago")
        is_bullish = r.get("macd_cross_direction") == "bullish"

        if (
            is_bullish and cross_days_ago is not None and cross_days_ago <= MACD_CONTINUATION_MAX_CROSS_DAYS_AGO
            and gain is not None and sma_dist is not None and sma_dist >= MACD_LANE_DIST_SMA20_MIN_PCT
        ):
            if 6.0 <= gain < 10.0:
                watchlist[t] = {
                    "lane": "CONTINUATION", "detail": f"+{gain:.1f}% sejak cross",
                    "dist_to_sma20": sma_dist, "pct_b": r.get("pct_b"),
                }
                continue
            elif 3.0 <= gain < 6.0:
                watchlist[t] = {
                    "lane": "VALIDATION", "detail": f"+{gain:.1f}% sejak cross",
                    "dist_to_sma20": sma_dist, "pct_b": r.get("pct_b"),
                }
                continue

        # LATE_VALIDATION (lane ke-9, 2026-08-31): TANPA gate dist_to_sma20,
        # hari 3-6 (BEDA dari EARLY_VALIDATION 1-5, & dari CONTINUATION/
        # VALIDATION yg cuma s.d. hari 5) -- dicek SETELAH CONTINUATION/
        # VALIDATION supaya keduanya (kalau genuinely match, dist>=12%)
        # tetap prioritas -- lihat catatan MACD_LATE_VALIDATION_GAIN_MIN_PCT
        # di atas.
        if (
            is_bullish and cross_days_ago is not None
            and MACD_LATE_VALIDATION_MIN_CROSS_DAYS_AGO <= cross_days_ago <= MACD_LATE_VALIDATION_MAX_CROSS_DAYS_AGO
            and gain is not None and gain >= MACD_LATE_VALIDATION_GAIN_MIN_PCT
        ):
            watchlist[t] = {
                "lane": "LATE_VALIDATION", "detail": f"+{gain:.1f}% sejak cross",
                "dist_to_sma20": sma_dist, "pct_b": r.get("pct_b"), "gain_since_cross": gain,
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
    lane_label = {
        "PRE": "PRE-CROSS (SDT)", "CONTINUATION": "CONTINUATION (HC)", "VALIDATION": "VALIDATION (HC)",
        "MOMENTUM_EXTENDED": "MOMENTUM EXTENDED (Swing)", "EARLY_VALIDATION": "EARLY VALIDATION (pra-VALIDATION)", "LATE_VALIDATION": "LATE VALIDATION (pasca-window)",
    }.get(lane, lane)
    tp_suffix = _tp_lines_suffix(watchlist_entry, detection["current_price"])
    if tp_suffix:
        hist_line = tp_suffix.lstrip("\n")
    else:
        hist_label = {
            "PRE": "hit6~50-56%", "CONTINUATION": "hit6~60.2%", "VALIDATION": "hit6~57.6%",
            "MOMENTUM_EXTENDED": "hit6~69.4-70.2%",
            # MBSS v2 2026-08-31: bukan hit6 (belum ada model lane_confidence
            # utk lane ini) -- metrik beda: peluang lanjut ke gain_since_
            # cross>=4% (zona VALIDATION) dlm 1-2 hari bursa, lihat catatan
            # MACD_EARLY_VALIDATION_GAIN_MIN_PCT.
            "EARLY_VALIDATION": "~33% lanjut ke VALIDATION dlm 1-2h",
            # MBSS v2 2026-08-31: fallback statis kalau fitur gain_since_cross/
            # dist_to_sma20/pct_b tak lengkap (model py, tapi None -> tp_suffix
            # kosong) -- lihat MACD_LATE_VALIDATION_GAIN_MIN_PCT.
            "LATE_VALIDATION": "touch>=3%(d3)~61.3%",
        }.get(lane, "-")
        hist_line = f"Historis: {hist_label}"
    return (
        f"SWING TRADE\n"
        f"📈 {ticker} MENJELANG CLOSING — {lane_label}, {detail}\n"
        f"Body hijau +{detection['gain_pct']:.1f}% dari open ({detection['day_open']:,.0f}) — skrg {detection['current_price']:,.0f}\n"
        f"{hist_line}"
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
        f"SWING TRADE\n"
        f"🔥 {ticker} PULLBACK ENTRY — FRESH CROSS MOMENTUM ({watchlist_entry['cross_days_ago']} hari lalu, "
        f"momentum pre-cross +{watchlist_entry['ret10_pre_cross_pct']:.1f}%)\n"
        f"Pullback {detection['pullback_pct']:.1f}% dari open ({detection['day_open']:,.0f}) — skrg {detection['current_price']:,.0f} | {zone_label}"
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
        f"SWING TRADE\n"
        f"🚀 {ticker} CONFIRMATION ENTRY — FRESH CROSS MOMENTUM ({watchlist_entry['cross_days_ago']} hari lalu, "
        f"momentum pre-cross +{watchlist_entry['ret10_pre_cross_pct']:.1f}%)\n"
        f"Lanjut naik +{detection['gain_pct']:.1f}% dari open ({detection['day_open']:,.0f}) — skrg {detection['current_price']:,.0f}"
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
        f"SWING TRADE\n"
        f"🔔 {ticker} BELI DI OPEN — FRESH CROSS MOMENTUM ({watchlist_entry['cross_days_ago']} hari lalu, "
        f"momentum pre-cross +{watchlist_entry['ret10_pre_cross_pct']:.1f}%)\n"
        f"Open {detection['day_open']:,.0f} — skrg {detection['current_price']:,.0f} ({detection['gain_from_open']:+.1f}% dari open)\n"
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
        "pullback_entry_sent": 0, "open_buy_sent": 0, "pre_continuation_sent": 0,
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
        # BUGFIX (2026-08-31): SEBELUMNYA panggilan sinkron langsung (bukan
        # asyncio.to_thread) -- yf.download di dalamnya BLOCKING seluruh event
        # loop (bukan cuma job scan-alert ini) selama fetch berjalan.
        daily_ref = await _fetch_with_timeout(_fetch_daily_ref, universe, default=_FETCH_TIMED_OUT)
        if daily_ref is _FETCH_TIMED_OUT:
            daily_ref = {}  # JANGAN cache -- state["daily_ref"] tetap None, retry siklus berikutnya
        else:
            state["daily_ref"] = daily_ref
            print(f"✅ Universe 60-600 hari ini: {len(daily_ref)} ticker (dari {len(universe)} whitelist)")
    else:
        daily_ref = state["daily_ref"]

    # MBSS v2 (user request 2026-08-31): universe FCM/PRE-CONTINUATION BEDA
    # dari `universe` (_get_alert_universe(), whitelist bulanan Alert A/B/
    # gap) -- lihat docstring _get_swing_lane_universe() utk alasan (harus
    # SAMA populasi dgn /allsetup, bukan whitelist scalping terpisah).
    # BUGFIX (2026-08-31): pakai sentinel _FETCH_TIMED_OUT, BUKAN default={}/
    # [] biasa -- kalau timeout ke-cache sbg "0 kandidat" PERMANEN sepanjang
    # hari, persis bug conviction-sweep yg baru diperbaiki (lihat catatan
    # run_conviction_sweep_once).
    need_fcm = state.get("fresh_cross_momentum_watchlist") is None
    need_pre_cont = state.get("pre_continuation_watchlist") is None
    swing_lane_universe = None
    if need_fcm or need_pre_cont:
        swing_lane_universe = await _fetch_with_timeout(_get_swing_lane_universe, timeout=180, default=_FETCH_TIMED_OUT)
        if swing_lane_universe is _FETCH_TIMED_OUT:
            swing_lane_universe = []

    if need_fcm:
        if not swing_lane_universe:
            fcm_watchlist = {}
        else:
            print(f"📡 Scan-alert: hitung watchlist FRESH CROSS MOMENTUM (cross<=2hr, ret10_pre>15%) utk {len(swing_lane_universe)} ticker...")
            fcm_watchlist = await _fetch_with_timeout(_get_fresh_cross_momentum_watchlist, swing_lane_universe, timeout=180, default=_FETCH_TIMED_OUT)
            if fcm_watchlist is _FETCH_TIMED_OUT:
                fcm_watchlist = {}
            else:
                state["fresh_cross_momentum_watchlist"] = fcm_watchlist
                print(f"✅ FRESH CROSS MOMENTUM watchlist hari ini: {len(fcm_watchlist)} ticker.")
    else:
        fcm_watchlist = state["fresh_cross_momentum_watchlist"]

    if need_pre_cont:
        if not swing_lane_universe:
            pre_continuation_watchlist = {}
        else:
            print(f"📡 Scan-alert: hitung watchlist PRE-CROSS/CONTINUATION utk {len(swing_lane_universe)} ticker...")
            pre_continuation_watchlist = await _fetch_with_timeout(_get_pre_continuation_watchlist, swing_lane_universe, timeout=180, default=_FETCH_TIMED_OUT)
            if pre_continuation_watchlist is _FETCH_TIMED_OUT:
                pre_continuation_watchlist = {}
            else:
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
        extra_ref = await _fetch_with_timeout(_fetch_conviction_ref, lane_tickers_needing_ref, default={}) if lane_tickers_needing_ref else {}

        def _lane_ref_price(t):
            if t in daily_ref:
                return daily_ref[t]["prev_close"]
            return extra_ref.get(t)

        # BUGFIX (user report 2026-08-27, live case VERN -- lihat catatan
        # panjang di lane_confidence.py's should_suppress): should_suppress
        # (BUKAN `compute_tp1_tp2(...) is None`) -- fitur tak lengkap TIDAK
        # BOLEH dianggap sama dgn "gagal ambang WR", atau SEMUA ticker bisa
        # ke-suppress begitu ada 1 fitur baru yg belum terisi di sumber data.
        n_suppressed = 0
        for t, entry in list(fcm_watchlist.items()):
            ref_price = _lane_ref_price(t)
            features = {"ret10_pre_cross_pct": entry.get("ret10_pre_cross_pct"), "pct_b": entry.get("pct_b")}
            if lane_confidence.should_suppress("FCM", features, ref_price):
                del fcm_watchlist[t]
                n_suppressed += 1
            elif ref_price:
                entry["tp_info"] = lane_confidence.compute_tp1_tp2("FCM", features, ref_price)

        for t, entry in list(pre_continuation_watchlist.items()):
            lane_tag = entry["detail"] if entry["lane"] == "PRE" else entry["lane"]
            if lane_tag not in lane_confidence.SUPPORTED_LANES:
                continue  # FAST_RECOVERY/EARLY_RECOVERY -- tetap tanpa tp_info, fallback statis
            ref_price = _lane_ref_price(t)
            features = {
                "dist_to_sma20": entry.get("dist_to_sma20"), "pct_b": entry.get("pct_b"),
                "gain_since_cross": entry.get("gain_since_cross"),  # LATE_VALIDATION saja butuh ini, lane lain abaikan
            }
            if lane_confidence.should_suppress(lane_tag, features, ref_price):
                del pre_continuation_watchlist[t]
                n_suppressed += 1
            elif ref_price:
                entry["tp_info"] = lane_confidence.compute_tp1_tp2(lane_tag, features, ref_price)

        state["fresh_cross_momentum_watchlist"] = fcm_watchlist
        state["pre_continuation_watchlist"] = pre_continuation_watchlist
        state["lane_tp_computed"] = True
        print(f"✅ Confidence individual (TP1/TP2) dihitung -- {n_suppressed} ticker di-suppress (WR<50% di level terdekat).")

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

    # MBSS v2 (user request 2026-08-29, REVISI): BSJP watch-lookup DIHAPUS
    # dari state loop bersama ini -- lihat run_bsjp_shortlist_scan/
    # run_bsjp_recheck_once utk mekanisme BSJP yang baru (2-fase, state
    # & cadence sendiri).

    # Union -- FCM/PRE-CONTINUATION BISA di luar band harga 60-600 (tidak
    # ada floor harga di definisi masing-masing), jadi tidak selalu subset
    # alert_universe.
    alert_universe = list(daily_ref.keys())
    full_ticker_set = sorted(
        set(alert_universe) | set(fcm_watchlist.keys()) | set(pre_continuation_watchlist.keys())
    )
    print(
        f"🔍 Scan-alert: {len(full_ticker_set)} ticker ({len(alert_universe)} alert + {len(fcm_watchlist)} FCM + "
        f"{len(pre_continuation_watchlist)} PRE/CONTINUATION), fetch bar 1m..."
    )
    # BUGFIX (live incident 2026-09-02, sama akar dgn run_gap_rebound_scan_
    # once): kalau daily_ref/fcm_watchlist/pre_continuation_watchlist SEMUA
    # kosong siklus ini (mis. semua lagi timeout bareng), full_ticker_set=[]
    # -> _fetch_today_1m([]) -> yf.download(symbols=[]) -> ValueError "No
    # objects to concatenate" (exception ASLI, lolos dari _fetch_with_
    # timeout) -- crash job ini. Skip bersih drpd crash.
    if not full_ticker_set:
        summary["skipped_reason"] = "empty_ticker_set"
        _save_state(state)
        return summary
    data = await _fetch_with_timeout(_fetch_today_1m, full_ticker_set, default=pd.DataFrame())

    tickers_state = state.setdefault("tickers", {})
    bot = _get_shared_bot()

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
            t, {"excluded_no_room": False, "alert_a_sent": False, "alert_b_sent": False, "watchlist_entry_sent": False}
        )
        t_state.setdefault("watchlist_entry_sent", False)
        t_state.setdefault("open_buy_sent", False)
        t_state.setdefault("pre_continuation_sent", False)

        # MBSS v2 (user request 2026-08-29, REVISI): BSJP DIKELUARKAN dari
        # loop scan-alert bersama ini -- sekarang sinyal 2-fase sendiri
        # (run_bsjp_shortlist_scan via /bsjp akhir sesi 1, run_bsjp_
        # recheck_once tiap 5 menit 09:30-15:50, dipercepat dari 15 menit
        # 2026-09-02), state & cadence sendiri, tidak lagi numpang fetch
        # 1m bar loop 3-menitan ini.

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

        # MBSS v2 (user request 2026-08-30): HC Minervini gap-watch DIHAPUS --
        # lihat engine/nightly.py utk riwayat lengkap.

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
            continue  # ticker ini HANYA di fcm_watchlist/pre_continuation_watchlist (di luar band 60-600) -- Alert A/B di bawah butuh prev_close, sisanya di-skip

        conviction = _conviction_tag(t, fcm_watchlist, pre_continuation_watchlist)
        risk_tags = _risk_tags(bars, ref, now_wib, ticker=t, danger_lookup=danger_lookup)

        # MBSS v2 (user request 2026-08-30, MERGE): gap-up/gap-hold DIPINDAH
        # total ke run_gap_rebound_scan_once (job 1-menit, window 09:00-09:10)
        # -- lihat catatan GAP_HOLD_MIN_VOL_RATIO_PARTIAL/_detect_gap_hold.
        # Tidak lagi di sini (job 3-menitan ini, sepanjang hari) krn deteksi
        # "holds" SELALU cuma butuh 5 menit pertama, window sempit REBOUND
        # job sudah cukup & lebih presisi (cadence 1 menit vs 3 menit).

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
    universe conviction sweep (FCM/PRE/CONTINUATION/VALIDATION/HC-gap-watch)
    tidak dibatasi band harga scanalert biasa.
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
    lane yg tak didukung engine/lane_confidence.py (BSJP_ARA/BSJP_SECOND_
    WAVE/FAST_RECOVERY/EARLY_RECOVERY) -- caller fallback ke
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
        # MBSS v2 (user request 2026-08-31): SAMA alasan dgn run_scan_alert_
        # once -- lihat docstring _get_swing_lane_universe().
        swing_lane_universe = _get_swing_lane_universe()
        if fcm is None:
            fcm = _get_fresh_cross_momentum_watchlist(swing_lane_universe)
        if pre_cont is None:
            pre_cont = _get_pre_continuation_watchlist(swing_lane_universe)

    for t, w in (fcm or {}).items():
        tags[t] = {"tag": "FCM", "features": {"ret10_pre_cross_pct": w.get("ret10_pre_cross_pct"), "pct_b": w.get("pct_b")}}
    for t, w in (pre_cont or {}).items():
        if t in tags:
            continue
        lane_tag = w["detail"] if w["lane"] == "PRE" else w["lane"]  # PRE -> FAST_RECOVERY/EARLY_RECOVERY/ABOVE_MOMENTUM
        tags[t] = {
            "tag": lane_tag,
            "features": {
                "dist_to_sma20": w.get("dist_to_sma20"), "pct_b": w.get("pct_b"),
                "gain_since_cross": w.get("gain_since_cross"),  # LATE_VALIDATION saja butuh ini
            },
        }

    # MBSS v2 (user request 2026-08-30): HC_GAP_WATCH union DIHAPUS -- lihat
    # engine/nightly.py utk riwayat lengkap.

    # MBSS v2 (user request 2026-08-29, REVISI): BSJP TIDAK LAGI bagian
    # union watchlist Conviction Sweep -- BSJP sekarang sinyal 2-fase
    # sendiri (run_bsjp_shortlist_scan/run_bsjp_recheck_once), shortlist +
    # state terpisah, TIDAK dicampur ke scanalert_state.json/tags lane
    # MACD lain.
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


# MBSS v2 (user request 2026-08-29 -- kategori DAY TRADE/SWING TRADE/BSJP di
# baris pertama tiap pesan): conviction sweep SELALU pakai ceiling 5-hari
# bursa (SWING horizon) utk lane MACD -- BSJP_ARA/BSJP_SECOND_WAVE DIHAPUS
# dari conviction sweep (MBSS v2, user request 2026-08-29, lihat run_bsjp_
# recheck_once), jadi tag di sini tidak pernah lagi berupa BSJP apa pun.
def _conviction_sweep_category(tag: str) -> str:
    return "SWING TRADE"


def _build_conviction_sweep_message(ticker: str, tag: str, tier: int, current_price: float,
                                     ref_price: float, ceiling_price: float, ceiling_pct: float,
                                     tp_info: dict | None = None, extra_tags: list[str] | None = None) -> str:
    gain_from_ref = (current_price - ref_price) / ref_price * 100
    if tp_info:
        tp_line = "\n".join(lane_confidence.format_tp_lines(tp_info, current_price=current_price))
    else:
        tp_line = f"Estimasi max TP {ceiling_price:,.0f}"
    warn_lines = "".join(f"\n{t}" for t in (extra_tags or []))
    return (
        f"{_conviction_sweep_category(tag)}\n"
        f"📈 {ticker} 🔺 RISING (tier {tier}/{CONVICTION_SWEEP_MAX_TIERS}) — {tag}\n"
        f"Harga {current_price:,.0f} ({gain_from_ref:+.1f}% dari closing kemarin {ref_price:,.0f})\n"
        f"{tp_line}\n"
        f"Radar: 2x checkpoint 15-menit naik beruntun{warn_lines}"
    )


def _build_conviction_pullback_exception_message(ticker: str, tag: str, current_price: float,
                                                   ref_price: float, ceiling_price: float,
                                                   tp_info: dict | None = None, extra_tags: list[str] | None = None) -> str:
    gain_from_ref = (current_price - ref_price) / ref_price * 100
    label = f"{tp_info['tp2_price']:,.0f}" if tp_info and "tp2_price" in tp_info else f"{ceiling_price:,.0f}"
    warn_lines = "".join(f"\n{t}" for t in (extra_tags or []))
    return (
        f"{_conviction_sweep_category(tag)}\n"
        f"🔁 {ticker} 🔺 STILL RISING setelah pullback (sudah lewat estimasi ceiling {label}) — {tag}\n"
        f"Harga {current_price:,.0f} ({gain_from_ref:+.1f}% dari closing kemarin {ref_price:,.0f}), sempat pullback dari puncak hari ini lalu naik lagi{warn_lines}"
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

    if not state.get("universe"):
        # MBSS v2 (bugfix 2026-08-31, user report "conviction sweep masih 0
        # ticker" setelah fix universe FCM/PRE-CONTINUATION di run_scan_alert_
        # once): SEBELUMNYA `state["universe"] = {}` langsung DIKUNCI di sini
        # begitu build pertama hari itu kosong (race umum: conviction-sweep &
        # scan-alert utama SAMA-SAMA first=10 di build_app(), jadi build
        # pertama bisa kepentok sebelum scan-alert utama sempat isi fresh_
        # cross_momentum_watchlist/pre_continuation_watchlist di scanalert_
        # state.json, ATAU fetch ref_price yg flaky) -- akibatnya "0 ticker
        # dicek" macet SEPANJANG HARI walau scan-alert utama belakangan punya
        # kandidat asli. Sekarang: HANYA commit ke state["universe"] kalau
        # BENAR-BENAR terisi; kalau kosong, JANGAN dikunci -- coba build ulang
        # siklus 15 menit berikutnya (murah, aman -- window conviction sweep
        # tetap terbatas jam bursa).
        main_state = _load_state()
        universe_tags = await _fetch_with_timeout(_build_conviction_universe_tags, main_state, timeout=180, default={})
        universe = {}
        n_suppressed = 0
        if universe_tags:
            ref_prices = await _fetch_with_timeout(_fetch_conviction_ref, list(universe_tags.keys()), default={})
            for t, info in universe_tags.items():
                ref_price = ref_prices.get(t)
                if not ref_price:
                    continue
                tag = info["tag"]
                tp_info = None
                # BUGFIX (user report 2026-08-27, live case VERN): should_suppress,
                # BUKAN `compute_tp1_tp2(...) is None` -- lihat catatan lane_confidence.py
                if lane_confidence.should_suppress(tag, info["features"], ref_price):
                    n_suppressed += 1
                    continue  # confidence individual <50% di level terdekat -- tidak diproduksi jadi sinyal
                if tag in lane_confidence.SUPPORTED_LANES:
                    tp_info = lane_confidence.compute_tp1_tp2(tag, info["features"], ref_price)
                universe[t] = {"tag": tag, "ref_price": ref_price, "tp_info": tp_info}
        if not universe:
            _save_conviction_state(state)
            summary["skipped_reason"] = "no_universe"
            return summary
        state["universe"] = universe
        print(f"✅ Conviction-sweep universe hari ini: {len(universe)} ticker ({n_suppressed} di-suppress, WR<50%).")
    else:
        universe = state["universe"]

    if not universe:
        summary["skipped_reason"] = "no_universe"
        _save_conviction_state(state)
        return summary

    data = await _fetch_with_timeout(_fetch_today_1m, list(universe.keys()), default=pd.DataFrame())
    tickers_state = state.setdefault("tickers", {})
    bot = _get_shared_bot()

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
        if events:
            # MBSS v2 (user request 2026-08-29 -- wire CHASE_RISK & FAIL_SIGNAL
            # ke conviction sweep, sebelumnya cuma dipakai Alert A/B/gap-up)
            day_open = float(bars["Open"].astype(float).iloc[0])
            day_high_so_far = float(bars["High"].astype(float).max())
            extra_tags = []
            chase_tag = _chase_risk_tag(current_price, day_open)
            if chase_tag:
                extra_tags.append(chase_tag)
            fail_tag = _fail_signal_tag(current_price, day_open, day_high_so_far, tag)
            if fail_tag:
                extra_tags.append(fail_tag)
        for event in events:
            kind = event[0]
            if kind == "tier":
                tier = event[1]
                msg = _build_conviction_sweep_message(t, tag, tier, current_price, ref_price, ceiling_price, ceiling_pct, tp_info=tp_info, extra_tags=extra_tags)
                summary["tier_alerts_sent"] += 1
            else:
                msg = _build_conviction_pullback_exception_message(t, tag, current_price, ref_price, ceiling_price, tp_info=tp_info, extra_tags=extra_tags)
                summary["pullback_exception_sent"] += 1
            if bot is not None:
                await core.safe_reply(bot, msg, chat_id=core.TELEGRAM_CHAT_ID)
            else:
                print(f"[NO TELEGRAM TOKEN] {msg}")

    _save_conviction_state(state)
    print(f"✅ Conviction-sweep selesai: {summary['scanned']} ticker dicek, "
          f"{summary['tier_alerts_sent']} tier alert, {summary['pullback_exception_sent']} pullback-exception.")
    return summary
