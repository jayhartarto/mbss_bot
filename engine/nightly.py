"""
engine/nightly.py — NightlyEngine (MBSS v2 Sprint 1, Phase 2)

Orchestrates the end-of-day pipeline per the Executive Summary:
    1. Load the ISSI-liquid universe (whitelist).
    2. Refresh OHLCV DB from Yahoo Finance.
    3. Compute technical indicators & base scores for every ticker
       (compute_factor_scoring — still lives in legacy_core for now, see
       note below).
    4. Save everything to the shared cache (partition "eod") via
       CacheManager, so every command reads from here instead of fetching
       live.
    5. Resolve any pending daytrade picks now that today's EOD data exists.

This is the SINGLE writer of the "eod" cache partition. Every command
(`/testbrief`, `/screendaytrade`, `/gptpick`, `/executiongate`, `/winrate`,
...) only ever reads it, via `fetch_tickers_scored_with_cache()` or
`load_daily_scan_cache()`.

Why this still imports `engine.legacy_core` at module scope
-------------------------------------------------------------
`compute_factor_scoring()` (the actual indicator/scoring math, ~600 lines)
is used by NightlyEngine here AND directly by several live/on-demand
commands (`/check`, `/executiongate`, portfolio valuation, ...) that are
NOT part of the nightly batch job. Moving it here would make those commands
import *this* module just to reach the scorer, which is backwards. It's a
genuinely shared "Scoring" concern that both NightlyEngine and the Command
Layer depend on — a candidate for its own `engine/scoring.py` in a later
phase. For now it stays in legacy_core.py and this module borrows it, same
as the whitelist builder, the OHLCV DB helpers, and the Telegram constants.

This creates a two-way dependency (legacy_core calls back into this module
for `fetch_tickers_scored_with_cache`, `load_daily_scan_cache`, and
`run_nightly_full_scan`), so `legacy_core` imports this module too. That's
safe ONLY because:
  - this module does `import engine.legacy_core as core` (a *module*
    import, not `from engine.legacy_core import name`), which works even
    while legacy_core is still mid-import (Python just hands back the
    partially-built module object from sys.modules); and
  - every `core.something` reference here happens INSIDE a function body,
    never at module load time — so by the time any of these functions are
    actually called, legacy_core has long finished loading.
Don't add module-level `core.X` lookups here, or the circular import breaks.

Same reasoning applies to `import engine.market as market_engine` below
(Phase 3b — `run_nightly_full_scan` calls `market_engine.compute_market_breadth`
after the scan finishes, to compute breadth/sector returns/regime from the
same results, no new fetch).
"""
from __future__ import annotations

import asyncio
import os
import pickle

from engine.cache import cache_manager
from engine import legacy_core as core
import engine.market as market_engine
import engine.scoring as scoring_engine
import engine.broker as broker_engine


# ---------------------------------------------------------------------
# Shared "eod" cache — save/load/migrate
# ---------------------------------------------------------------------
def save_daily_scan_cache(results: list):
    """
    Simpan hasil scan penuh (dari scheduled job jam 22:00 WIB / --eodscan) ke
    cache BERSAMA (engine/cache.py::CacheManager, partisi "eod") — dipakai
    oleh run_morning_brief(), screen_daytrade(), gptpick, dll supaya tidak
    perlu fetch ulang untuk ticker yang sudah di-scan malam sebelumnya.

    NightlyEngine adalah SATU-SATUNYA penulis partisi "eod"; semua command
    lain hanya membaca lewat load_daily_scan_cache().
    """
    scored_by_ticker = {r["ticker"]: r for r in results if r and r.get("ticker")}
    meta = {
        "trading_day_marker": core.get_current_trading_day_close_marker(),
        "formula_version": core.SCORING_FORMULA_VERSION,
    }
    ok = cache_manager.set("eod", {"scored": scored_by_ticker}, meta=meta)
    if ok:
        print(
            f"💾 Daily scan cache tersimpan (cache/eod.pkl): {len(scored_by_ticker)} ticker, "
            f"marker {meta['trading_day_marker']}, formula v{core.SCORING_FORMULA_VERSION}"
        )
    else:
        print("⚠️ Gagal menyimpan daily scan cache — lihat log 'mbss.cache' untuk detail.")


def load_daily_scan_cache() -> dict:
    """
    Return dict {ticker: scoring} dari cache scan malam TERAKHIR (partisi
    "eod" di cache bersama). Kalau marker trading day tidak cocok hari ini
    atau formula_version beda dari yang sedang jalan, return dict kosong
    (dianggap basi) — pemanggil lalu fetch fresh untuk ticker yang hilang.
    """
    meta = cache_manager.get_meta("eod")
    if not meta:
        return {}

    current_marker = core.get_current_trading_day_close_marker()
    if meta.get("trading_day_marker") != current_marker:
        print(f"📋 Daily scan cache basi (marker {meta.get('trading_day_marker')} != {current_marker}), diabaikan.")
        return {}
    if meta.get("formula_version") != core.SCORING_FORMULA_VERSION:
        print(
            f"📋 Daily scan cache dari formula versi lama "
            f"({meta.get('formula_version')} != {core.SCORING_FORMULA_VERSION}), diabaikan — fetch ulang dengan formula terbaru."
        )
        return {}

    payload = cache_manager.get("eod", default={})
    scored = payload.get("scored", {}) if isinstance(payload, dict) else {}
    if not isinstance(scored, dict):
        print("⚠️ Format daily scan cache tidak valid, diabaikan.")
        return {}
    return scored


def load_daily_scan_cache_allow_stale() -> tuple[dict, str | None]:
    """
    Varian load_daily_scan_cache() (user request) — return apa pun yang ada
    di cache SEKALIPUN dari hari bursa sebelumnya atau formula versi lama,
    untuk command yang lebih baik "tampilkan data lama, tandai jelas"
    daripada menolak total. Return (scored_dict, staleness_note) —
    staleness_note None kalau cache genuinely masih current, kalau tidak
    string Indonesia siap-tampil yang menjelaskan seberapa basi.

    SENGAJA bukan perilaku default load_daily_scan_cache() sendiri — caller
    lain (mis. fallback fetch-langsung /testbrief untuk ticker yang hilang)
    mengandalkan staleness ketat untuk tahu kapan mereka butuh data segar;
    mengubah itu secara luas berisiko diam-diam menyajikan skor basi di
    tempat yang akurasinya lebih penting daripada ketersediaan.
    """
    meta = cache_manager.get_meta("eod")
    if not meta:
        return {}, None  # genuinely belum pernah ada cache — bukan "basi", memang kosong

    payload = cache_manager.get("eod", default={})
    scored = payload.get("scored", {}) if isinstance(payload, dict) else {}
    if not isinstance(scored, dict):
        return {}, None

    current_marker = core.get_current_trading_day_close_marker()
    staleness_note = None
    if meta.get("trading_day_marker") != current_marker:
        staleness_note = f"⚠️ Data dari scan {meta.get('trading_day_marker') or '?'}, BELUM update untuk hari ini — jalankan /eodscan untuk data terbaru."
    elif meta.get("formula_version") != core.SCORING_FORMULA_VERSION:
        staleness_note = f"⚠️ Data dari formula versi lama ({meta.get('formula_version')}), belum dihitung ulang dengan formula terbaru — jalankan /eodscan."

    return scored, staleness_note


def migrate_legacy_daily_scan_cache():
    """
    One-time migration: kalau cache/eod.pkl (format baru) belum ada TAPI file
    lama daily_scan_cache.pkl (format pra-refactor, dict polos tanpa envelope
    meta/data) masih ada di root proyek, baca lalu tulis ulang lewat
    CacheManager supaya cache malam terakhir tidak hilang gara-gara refactor.
    Aman dipanggil berkali-kali — no-op kalau cache/eod.pkl sudah ada, atau
    kalau file lama tidak ada.
    """
    if cache_manager.exists("eod"):
        return
    if not os.path.exists(core.DAILY_SCAN_CACHE_FILE):
        return
    try:
        with open(core.DAILY_SCAN_CACHE_FILE, "rb") as f:
            old_cache = pickle.load(f)
    except Exception as e:
        print(f"⚠️ Migrasi cache lama gagal dibaca ({core.DAILY_SCAN_CACHE_FILE}): {e}")
        return
    if not isinstance(old_cache, dict) or "scored" not in old_cache:
        return
    meta = {
        "trading_day_marker": old_cache.get("trading_day_marker"),
        "formula_version": old_cache.get("formula_version"),
    }
    ok = cache_manager.set("eod", {"scored": old_cache.get("scored", {})}, meta=meta)
    if ok:
        print(f"🔁 Migrasi cache lama ({core.DAILY_SCAN_CACHE_FILE}) → cache/eod.pkl berhasil.")


# ---------------------------------------------------------------------
# Bulk fetch/scoring across a ticker universe
# ---------------------------------------------------------------------
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
    for chunk_start in range(0, len(tickers), core.ITICK_CHUNK_SIZE):
        chunk = tickers[chunk_start:chunk_start + core.ITICK_CHUNK_SIZE]
        for ticker in chunk:
            # MBSS v2 (user request): skip ticker yang sudah dikonfirmasi gagal
            # fetch berkali-kali berturut-turut (biasanya delisted) — tidak ada
            # gunanya coba lagi setiap run, cuma buang waktu. Dicoba ulang
            # otomatis sesekali (lihat FAILED_FETCH_BLACKLIST_DAYS di
            # legacy_core.py) siapa tahu datanya pulih/relisting.
            if core.is_ticker_blacklisted(ticker):
                reason = core.get_blacklist_reason(ticker)
                skip_reasons[ticker] = f"blacklisted ({reason})" if reason else "blacklisted (persisten gagal fetch)"
                continue
            try:
                res = scoring_engine.compute_factor_scoring(ticker, include_quote_check=False)
                core.record_fetch_result(ticker, success=bool(res))
                if res:
                    results.append(res)
                else:
                    skip_reasons[ticker] = "excluded (see console for specific reason)"
            except Exception as e:
                print(f"Error processing {ticker}: {e}")
                core.record_fetch_result(ticker, success=False)
                skip_reasons[ticker] = f"exception: {str(e)[:100]}"
            core.time.sleep(1.0)  # light pacing within a chunk
        is_last_chunk = (chunk_start + core.ITICK_CHUNK_SIZE) >= len(tickers)
        if not is_last_chunk:
            print(f"⏳ Fetch/scoring: cooling down {core.ITICK_COOLDOWN_SECONDS}s before next chunk "
                  f"({chunk_start + len(chunk)}/{len(tickers)} tickers done)...")
            core.time.sleep(core.ITICK_COOLDOWN_SECONDS)
    return results, skip_reasons


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


# ---------------------------------------------------------------------
# The nightly orchestrator itself
# ---------------------------------------------------------------------
async def run_nightly_full_scan(context):
    """
    Scheduled job jam 22:00 WIB (atau `--eodscan` manual) — refresh DB dari
    Yahoo Finance, lalu scan penuh (compute_factor_scoring) untuk universe
    yang sama. Hasil disimpan ke daily_scan_cache dan dipakai bersama oleh
    run_morning_brief(), screen_daytrade(), gptpick, dll.

    NOTE (MBSS v2 refactor, Phase 3b — universe diperluas atas permintaan
    user): universe dulu "ISSI liquid" (212 ticker, dari
    load_or_build_issi_liquid_whitelist, filter liquiditas via Zapi). Sekarang
    disamakan dengan universe yang dipakai /testbrief, /screendaytrade,
    /gptpick — whitelist bulanan berbasis Yahoo (~389 ticker, dari
    fetch_online_sharia_list + load_or_build_whitelist) — supaya cache
    "eod" hasil scan malam ini mencakup HAMPIR SEMUA yang dibutuhkan command
    lain (cache-hit ~100%, bukan ~53% seperti sebelumnya). Trade-off:
    eodscan jadi lebih lama (~389 vs ~212 ticker, kira-kira +80% durasi).
    """
    try:
        if await asyncio.to_thread(core.is_idx_market_holiday_today):
            print("📅 Skipping nightly full scan — IDX market holiday today (tidak ada data EOD baru).")
            return

        sharia_universe = await asyncio.to_thread(core.fetch_online_sharia_list)
        full_universe_list = list(sharia_universe)
        universe_tickers = await asyncio.to_thread(core.load_or_build_whitelist, full_universe_list)
        universe_label = "ISSI eligible (Yahoo whitelist)"
        print(f"🌙 Nightly full scan dimulai: {len(universe_tickers)} ticker {universe_label}...")

        db_stats_payload = await asyncio.to_thread(core.populate_from_yfinance, universe_tickers, "10d", 50)
        db_stats = core.get_db_stats()
        latest_marker = db_stats.get("last_ohlcv_update_marker") or db_stats_payload.get("latest_marker") or "-"
        try:
            await context.bot.send_message(
                chat_id=core.TELEGRAM_CHAT_ID,
                text=(
                    f"✅ DB update sukses\n"
                    f"Universe: {universe_label}\n"
                    f"Ticker: {db_stats_payload.get('tickers', len(universe_tickers))}\n"
                    f"Updated s/d: {latest_marker}\n"
                    f"Rows written: {db_stats_payload.get('rows_written', 0):,}"
                ),
            )
        except Exception as notify_error:
            print(f"⚠️ Gagal kirim notifikasi DB update malam: {notify_error}")

        # Timeout dinaikkan dari 1800s (30 menit) ke 3000s (50 menit) — universe
        # 389 ticker (naik dari 212) butuh headroom lebih; 50 menit juga
        # sekarang cocok dengan pesan timeout di except block bawah (di kode
        # lama pesannya sudah bilang "50 menit" tapi timeout aslinya cuma 30).
        results, skip_reasons = await asyncio.wait_for(
            asyncio.to_thread(fetch_all_tickers_scored, universe_tickers), timeout=3000
        )
        save_daily_scan_cache(results)
        core.update_scan_metadata(len(results), len(skip_reasons), latest_marker, universe_name=universe_label)
        print(f"🌙 Nightly full scan selesai: {len(results)} berhasil, {len(skip_reasons)} gagal/dikecualikan.")

        # MBSS v2 (user request — BSJP-ARA "pola GIAA"): pre-filter + fetch
        # berita SEKALI di sini (murah relatif ke seluruh eodscan, dan
        # /bsjp siang/sore jadi tinggal baca cache tanpa fetch berita live).
        try:
            bsjp_ara_candidates = await asyncio.to_thread(build_bsjp_ara_candidates, results)
            save_bsjp_ara_candidates(bsjp_ara_candidates)
        except Exception as e:
            print(f"⚠️ Gagal membangun BSJP-ARA candidates: {e}")

        # MBSS v2 (user request — /broksum): fetch broker-summary batch buat
        # 250 ticker berskor tertinggi SEKALI di sini, pakai HABIS kuota
        # harian Index Alpha (5 panggilan batch x 50 = 250 ticker, persis
        # pas). /broksum siang/sore TINGGAL baca cache ini, TIDAK fetch
        # live sama sekali.
        trading_night_index = _get_and_increment_trading_night_index()

        broksum_250_data = {}
        try:
            broksum_250_data = await asyncio.to_thread(build_broksum_250, results)
        except Exception as e:
            print(f"⚠️ Gagal membangun BROKSUM 250 (Index Alpha): {e}")

        # RapidAPI IDX (MBSS v2, RapidAPI integration) — interim real-broker-
        # data source while Index Alpha's monthly quota is exhausted. Sweeps
        # the SAME SMART_MONEY_BROKER_WHITELIST used by Bias Bandar below,
        # one call per broker (13 total) covering that broker's activity
        # across EVERY ticker, not just the 250 highest-scored — reshaped
        # into Index Alpha's row format and merged in below, so every
        # existing broksum_250 consumer picks it up with zero changes. Every
        # 2nd trading night (10-day lookback window already overlaps
        # night-to-night, daily refresh is unnecessary — see budget in the
        # RapidAPI integration plan).
        rapidapi_whitelist_data = {}
        if trading_night_index % 2 == 0:
            try:
                rapidapi_whitelist_data = await asyncio.to_thread(build_rapidapi_broker_whitelist_sweep)
            except Exception as e:
                print(f"⚠️ Gagal membangun RapidAPI broker whitelist sweep: {e}")

        merged_broksum = _merge_broksum_sources(broksum_250_data, rapidapi_whitelist_data)
        save_broksum_250(merged_broksum)
        # MBSS v2 (user request — Bias Bandar): TAMBAHKAN snapshot hari
        # ini ke histori harian (bukan cuma cache sesaat) — supaya trend
        # akumulasi/distribusi bisa dilihat beberapa hari ke belakang.
        await asyncio.to_thread(append_broksum_daily_history, merged_broksum)

        # RapidAPI market-wide sweep (MBSS v2, RapidAPI integration) —
        # breakout alerts + multibagger scan + sector rotation, every
        # trading night (all inherently daily-fresh, unlike the whitelist
        # sweep/sentiment shortlist's every-2-nights cadence). Same
        # priority tier as the whitelist sweep above — front-loaded in
        # call order.
        try:
            rapidapi_market_intel = await asyncio.to_thread(build_rapidapi_market_intelligence_sweep)
            # Top brokers (MBSS v2, RapidAPI integration) — discovery/
            # cross-check only, never wired into an automated scoring path
            # (it's a turnover ranking, not a net-buying ranking — see
            # fetch_rapidapi_top_brokers's docstring). Every 10th trading
            # night only — near-free either way, but no reason to spend
            # nightly quota on a manual-review-only feature.
            if trading_night_index % 10 == 0:
                try:
                    rapidapi_market_intel["top_brokers"] = await asyncio.to_thread(broker_engine.fetch_rapidapi_top_brokers)
                except Exception as e:
                    print(f"⚠️ Gagal membangun RapidAPI top brokers: {e}")
            merged_market_intel = load_rapidapi_market_intelligence()
            merged_market_intel.update({k: v for k, v in rapidapi_market_intel.items() if v is not None})
            save_rapidapi_market_intelligence(merged_market_intel)
        except Exception as e:
            print(f"⚠️ Gagal membangun RapidAPI market intelligence sweep: {e}")

        # Whitelist accumulation/distribution adjustment (MBSS v2, RapidAPI
        # integration, "diskusi trader" session) — folds the whitelist
        # sweep's already-fetched broker rows into the CORE final score for
        # every ticker merged_broksum covers, at ZERO additional API cost.
        # Previously this kind of real-broker adjustment
        # (apply_brokersum_adjustment) only ever ran opt-in on a handful of
        # already-shortlisted tickers (gptpick top-10, myportfolio holdings)
        # because Index Alpha's 5-10/day quota made anything broader
        # infeasible — the whitelist sweep's flat cost removes that
        # constraint. Applied to `results` (in place) BEFORE re-saving
        # daily_scan_cache, so /screendaytrade, /hc, /testbrief, /consensus
        # all inherit the adjusted score through their normal cache read —
        # no per-tool wiring needed beyond this one nightly step.
        try:
            results_by_ticker = {r["ticker"]: r for r in results if r.get("ticker")}
            adjusted_count = 0
            for ticker, rows in merged_broksum.items():
                r = results_by_ticker.get(ticker)
                if not r or not r.get("scores"):
                    continue
                signal = broker_engine.compute_whitelist_accumulation_signal(ticker, rows)
                if not signal:
                    continue
                scoring_engine.apply_whitelist_accumulation_adjustment(r, signal)
                if r.get("whitelist_accumulation_adjusted"):
                    adjusted_count += 1
            if adjusted_count:
                save_daily_scan_cache(results)
                print(f"🎯 Whitelist accumulation/distribution: {adjusted_count} ticker skornya disesuaikan, cache di-update ulang.")
        except Exception as e:
            print(f"⚠️ Gagal menerapkan whitelist accumulation adjustment: {e}")

        # RapidAPI IDX sentiment shortlist (MBSS v2, RapidAPI integration) —
        # retail-vs-bandar divergence for a small top-scored shortlist, the
        # one signal in the whole RapidAPI integration with no OHLCV-
        # derivable proxy. Lowest priority of the RapidAPI nightly work —
        # first thing to silently shrink if the shared monthly quota runs
        # hot, since it's called last and every fetch call is gated by the
        # same budget counter regardless of call order.
        if trading_night_index % 2 == 0:
            try:
                rapidapi_sentiment_data = await asyncio.to_thread(build_rapidapi_sentiment_shortlist, results)
                save_rapidapi_sentiment_shortlist(rapidapi_sentiment_data)
            except Exception as e:
                print(f"⚠️ Gagal membangun RapidAPI sentiment shortlist: {e}")

        # Market breadth + sector returns + regime — dihitung dari data yang
        # SAMA yang baru saja dikumpulkan di atas (results), tanpa fetch baru.
        try:
            breadth = market_engine.compute_market_breadth(results)
            market_engine.save_market_context(breadth)
        except Exception as e:
            print(f"⚠️ Gagal menghitung market breadth: {e}")
            breadth = None

        # BUGFIX (ditemukan lewat pengamatan user — SOHO "Top" berturut-turut
        # dengan skor cuma 4.0, tidak istimewa): results TIDAK PERNAH di-sort
        # sebelum ini, jadi "Top" sebelumnya cuma ticker PERTAMA yang diproses
        # dalam urutan scan (alfabetis/urutan whitelist yang stabil tiap
        # malam) — bukan genuinely skor tertinggi. Sekarang benar-benar
        # sort dulu, DAN diperluas jadi top-3 (bukan cuma top-1) — supaya
        # pola klaster sektor (kalau beberapa saham sektor sama muncul
        # bareng di top-3) jadi kelihatan, bukan cuma 1 ticker terisolasi.
        results_sorted = sorted(results, key=lambda r: r.get("scores", {}).get("final", 0), reverse=True)
        top3 = results_sorted[:3]
        top_line = ", ".join(f"{r['ticker']} ({r.get('scores', {}).get('final', 0):.1f})" for r in top3) if top3 else "-"
        breadth_line = ""
        sector_line = ""
        if breadth:
            breadth_line = (
                f"Breadth: {breadth['advancers']} naik / {breadth['decliners']} turun "
                f"({breadth['breadth_pct_advancing']}%), regime={breadth['regime']}\n"
            )
            # MBSS v2 (user request — sentimen sektoral): sector_avg_returns_pct
            # SUDAH dihitung & disimpan tiap malam sejak lama (compute_market_breadth),
            # tapi TIDAK PERNAH ditampilkan ke user di manapun sampai sekarang.
            sectors = breadth.get("sector_avg_returns_pct", {})
            top_sectors = list(sectors.items())[:3]
            if top_sectors:
                sector_line = "Sektor terkuat: " + ", ".join(f"{s} ({r:+.1f}%)" for s, r in top_sectors) + "\n"
        try:
            await context.bot.send_message(
                chat_id=core.TELEGRAM_CHAT_ID,
                text=(
                    f"✅ Night scan selesai\n"
                    f"Universe: {universe_label}\n"
                    f"Scored: {len(results)}\n"
                    f"Skipped: {len(skip_reasons)}\n"
                    f"Top 3: {top_line}\n"
                    f"{breadth_line}"
                    f"{sector_line}"
                    f"Cache updated s/d: {latest_marker}"
                ),
            )
        except Exception as notify_error:
            print(f"⚠️ Gagal kirim notifikasi night scan: {notify_error}")

        # Resolusi picks winrate — dijalankan SETELAH scan malam, supaya data EOD
        # yang dipakai untuk cek TP/SL sudah mencakup hari ini.
        try:
            resolved_count = await asyncio.to_thread(core.resolve_daytrade_picks)
            print(f"🎯 Resolusi winrate: {resolved_count} pick diperbarui.")
        except Exception as e:
            print(f"⚠️ Resolusi daytrade picks gagal: {e}")
    except asyncio.TimeoutError:
        print("⏱️ Nightly full scan melebihi batas waktu 50 menit — cache TIDAK diperbarui malam ini.")
    except Exception as e:
        print(f"❌ Nightly full scan gagal: {e}")


# ==========================================
# 🌆 BSJP-ARA — pre-filter "pola GIAA" (MBSS v2, user request)
# Saham yang KEMARIN diam total, berpotensi meledak HARI INI. Sengaja
# TERPISAH dari 6 kriteria /bsjp lama (source berbeda di /winrate: "bsjp"
# vs "bsjp_ara") — dua metode dijalankan BERDAMPINGAN, biar data yang
# putuskan mana lebih akurat, bukan ditebak sekarang.
#
# Pre-filter (murah, dari cache) + fetch berita (RSS, gratis) dikerjakan
# SEKALI di sini (bagian /eodscan malam) — supaya /bsjp siang/sore TINGGAL
# baca cache ini + cek live yang murah (harga sekarang vs open), TANPA
# fetch berita sama sekali saat live (alasan: takut lambat kalau fetch
# berita real-time — sudah didiskusikan & disepakati).
# ==========================================
BSJP_ARA_MAX_PRICE = 1000  # direvisi dari 500 (user request) — lebih banyak kandidat
BSJP_ARA_MAX_DAY_CHANGE_PCT = 5.0  # |day_change_pct| kemarin harus di bawah ini ("datar")
BSJP_ARA_MAX_VOL_RATIO = 1.5       # vol_ratio kemarin harus di bawah ini (belum ramai)
BSJP_ARA_NEWS_MAX_CANDIDATES = 30  # batas jumlah fetch berita per malam (RSS gratis tapi tetap network call)


def build_bsjp_ara_candidates(results: list) -> list:
    """
    Jalankan pre-filter 3-tahap (harga, day_change_pct, vol_ratio) murni dari
    cache — TANPA fetch apa pun — lalu fetch berita RSS HANYA untuk yang
    lolos (dibatasi BSJP_ARA_NEWS_MAX_CANDIDATES, prioritas yang paling
    "datar" duluan, supaya kalau kepotong limit, yang paling representatif
    pola "sleeper" yang dapat prioritas).
    """
    prefiltered = []
    for r in results:
        if not r:
            continue
        price = r.get("price")
        day_change = r.get("day_change_pct")
        # BUGFIX (kasus nyata EKAD — lihat komentar day_change_pct/
        # vol_ratio_prior_day di compute_factor_scoring): pakai
        # vol_ratio_prior_day (genuinely "kemarin"), BUKAN vol_ratio (field
        # lama, sengaja "paling baru" termasuk hari ini kalau sudah masuk —
        # cocok buat skor momentum inti, TAPI salah buat pre-filter ini yang
        # butuh tahu apakah KEMARIN masih diam).
        vol_ratio_prior = r.get("vol_ratio_prior_day")
        if price is None or day_change is None or vol_ratio_prior is None:
            continue
        if price >= BSJP_ARA_MAX_PRICE:
            continue
        if abs(day_change) >= BSJP_ARA_MAX_DAY_CHANGE_PCT:
            continue
        if vol_ratio_prior >= BSJP_ARA_MAX_VOL_RATIO:
            continue
        prefiltered.append(r)

    print(f"🌆 BSJP-ARA pre-filter: {len(prefiltered)} kandidat lolos harga/day_change/volume (dari {len(results)})")

    # Prioritaskan yang PALING datar (day_change_pct paling dekat 0) untuk
    # fetch berita duluan, kalau ternyata lebih banyak dari batas limit.
    prefiltered.sort(key=lambda r: abs(r.get("day_change_pct", 0)))
    to_check_news = prefiltered[:BSJP_ARA_NEWS_MAX_CANDIDATES]

    candidates = []
    for r in to_check_news:
        ticker = r["ticker"]
        company_name = r.get("company_name") or ticker
        try:
            news = core.fetch_company_news(ticker, company_name, max_items=3)
        except Exception as e:
            print(f"⚠️ BSJP-ARA: gagal fetch berita {ticker}: {e}")
            news = []
        candidates.append({
            "ticker": ticker,
            "company_name": company_name,
            "prev_close": r.get("price"),
            "day_change_pct": r.get("day_change_pct"),
            "vol_ratio": r.get("vol_ratio_prior_day"),
            "sector": r.get("sector"),
            "news": news,
        })
        core.time.sleep(0.3)  # jaga-jaga rate limit Google News RSS, murah tapi tetap sopan

    print(f"🌆 BSJP-ARA: {len(candidates)} kandidat dengan berita terkumpul (dari {len(prefiltered)} lolos pre-filter harga/volume)")

    # MBSS v2 (user request — Catalyst Score, bukan sekadar "ada berita"):
    # klasifikasi 1x batch untuk semua kandidat, lalu HANYA saham dengan
    # katalis strong_bullish/bullish yang diteruskan — neutral/bearish/tanpa
    # berita sama sekali DIBUANG. Ini sesuai instruksi eksplisit: "News
    # catalist harus ambil positif saja".
    catalyst_map = core.classify_news_catalysts(candidates)
    if not catalyst_map:
        print("⚠️ BSJP-ARA: klasifikasi katalis gagal total — TIDAK ADA kandidat diloloskan (gagal-lunak, bukan meloloskan semua tanpa verifikasi).")
        return []

    final_candidates = []
    for c in candidates:
        cat = catalyst_map.get(c["ticker"])
        if not cat or cat.get("catalyst_category") not in ("strong_bullish", "bullish"):
            continue
        c["catalyst_category"] = cat.get("catalyst_category")
        c["catalyst_score"] = cat.get("catalyst_score")
        c["catalyst_reasoning"] = cat.get("reasoning")
        final_candidates.append(c)

    print(f"🌆 BSJP-ARA FINAL: {len(final_candidates)} kandidat dengan katalis positif (strong_bullish/bullish) dari {len(candidates)} yang dicek")
    return final_candidates


def save_bsjp_ara_candidates(candidates: list):
    meta = {"trading_day_marker": core.get_current_calendar_date_marker()}
    ok = cache_manager.set("bsjp_ara", {"candidates": candidates}, meta=meta)
    if ok:
        print(f"💾 BSJP-ARA candidates tersimpan (cache/bsjp_ara.pkl): {len(candidates)} kandidat")
    else:
        print("⚠️ Gagal menyimpan BSJP-ARA candidates cache.")


def load_bsjp_ara_candidates() -> list:
    meta = cache_manager.get_meta("bsjp_ara")
    if not meta:
        return []
    current_marker = core.get_current_calendar_date_marker()
    if meta.get("trading_day_marker") != current_marker:
        return []
    payload = cache_manager.get("bsjp_ara", default={})
    return payload.get("candidates", []) if isinstance(payload, dict) else []


# ==========================================
# 🏦 RAPIDAPI IDX NIGHTLY (MBSS v2, RapidAPI integration) — interim real-
# broker-data source while Index Alpha's monthly quota is exhausted. Two
# pieces: a broker-whitelist sweep that merges straight into BROKSUM 250
# below (same shape, zero changes needed in any consumer), and a small
# ticker-scoped sentiment shortlist for the one signal (retail-vs-bandar
# divergence) nothing else in this file provides.
# ==========================================


def _get_and_increment_trading_night_index() -> int:
    """
    Persisted counter, incremented once per run_nightly_full_scan call
    (which itself already skips IDX holidays) — used to gate every-N-nights
    cadences (whitelist sweep, sentiment shortlist) without needing
    real-calendar-date math around weekends/holidays.
    """
    current = cache_manager.get("rapidapi_night_index", default=0)
    if not isinstance(current, int):
        current = 0
    new_index = current + 1
    cache_manager.set("rapidapi_night_index", new_index)
    return new_index


def build_rapidapi_broker_whitelist_sweep() -> dict:
    """
    Sweep engine.broker.SMART_MONEY_BROKER_WHITELIST (13 codes) via
    fetch_rapidapi_broker_activity, one call per broker over a rolling
    RAPIDAPI_WHITELIST_SWEEP_LOOKBACK_DAYS window, reshape each response with
    rapidapi_broker_activity_to_broksum_rows, and merge into one
    {ticker: [rows, ...]} dict covering every ticker any whitelisted broker
    touched — broader than BROKSUM 250's top-N-by-score scope, since it's
    keyed by broker activity, not by which tickers happened to score well.
    Unlike Index Alpha's 250-ticker cap, this has NO ticker cap at all.
    """
    to_date = core.datetime.datetime.now(core.WIB).date()
    from_date = to_date - core.datetime.timedelta(days=broker_engine.RAPIDAPI_WHITELIST_SWEEP_LOOKBACK_DAYS)
    from_str, to_str = from_date.isoformat(), to_date.isoformat()

    merged: dict = {}
    for code in broker_engine.SMART_MONEY_BROKER_WHITELIST:
        activity = broker_engine.fetch_rapidapi_broker_activity(code, from_str, to_str)
        if not activity:
            continue
        for ticker, rows in broker_engine.rapidapi_broker_activity_to_broksum_rows(activity).items():
            merged.setdefault(ticker, []).extend(rows)
    print(f"🏦 RapidAPI whitelist sweep: {len(merged)} ticker tercakup dari {len(broker_engine.SMART_MONEY_BROKER_WHITELIST)} broker whitelist.")
    return merged


def _merge_broksum_sources(index_alpha_data: dict, rapidapi_data: dict) -> dict:
    """
    Union by ticker; within a ticker, dedupe by broker code — Index Alpha's
    row wins if both sources have the same broker+ticker (richer gross-
    figures source when it's actually working), RapidAPI only fills gaps.
    Currently Index Alpha is exhausted, so in practice this is close to a
    pass-through of the RapidAPI data until next month's reset.
    """
    if not rapidapi_data:
        return index_alpha_data
    if not index_alpha_data:
        return rapidapi_data

    merged: dict = {}
    for ticker in set(index_alpha_data) | set(rapidapi_data):
        by_code: dict = {}
        for row in rapidapi_data.get(ticker, []):
            code = row.get("code")
            if code:
                by_code[code] = row
        for row in index_alpha_data.get(ticker, []):  # Index Alpha rows overwrite RapidAPI's for the same code
            code = row.get("code")
            if code:
                by_code[code] = row
        merged[ticker] = list(by_code.values())
    return merged


def build_rapidapi_sentiment_shortlist(results: list) -> dict:
    """
    Top RAPIDAPI_SENTIMENT_SHORTLIST_SIZE tickers by (final score,
    active_breakout score) from tonight's full scan — same selection logic
    used to answer "pick the top candidates from all tools, higher scoring
    or nearest breakout readiness," since final score already reflects the
    shared scoring engine /screendaytrade, /hc, /gptpick all read from.
    Fetches RapidAPI sentiment (retail-vs-bandar divergence) for each.
    """
    scored = [r for r in results if r.get("scores", {}).get("final") is not None]
    ranked = sorted(
        scored,
        key=lambda r: (r["scores"]["final"], r.get("active_breakout", {}).get("score", 0)),
        reverse=True,
    )[:broker_engine.RAPIDAPI_SENTIMENT_SHORTLIST_SIZE]

    data = {}
    for r in ranked:
        result = broker_engine.fetch_rapidapi_sentiment(r["ticker"])
        if result:
            data[r["ticker"]] = result
    print(f"🎭 RapidAPI sentiment shortlist: {len(data)}/{len(ranked)} ticker berhasil.")
    return data


def save_rapidapi_sentiment_shortlist(data: dict):
    meta = {"trading_day_marker": core.get_current_calendar_date_marker()}
    ok = cache_manager.set("rapidapi_sentiment_shortlist", {"data": data}, meta=meta)
    if ok:
        print(f"💾 RapidAPI sentiment shortlist tersimpan: {len(data)} ticker")
    else:
        print("⚠️ Gagal menyimpan RapidAPI sentiment shortlist cache.")


def load_rapidapi_sentiment_shortlist() -> dict:
    payload = cache_manager.get("rapidapi_sentiment_shortlist", default={})
    return payload.get("data", {}) if isinstance(payload, dict) else {}


# ==========================================
# 📡 RAPIDAPI MARKET INTELLIGENCE SWEEP (MBSS v2, RapidAPI integration) —
# breakout alerts + multibagger scan, each a single market-wide call
# covering the WHOLE exchange (~30 alerts / N candidates per call) — every
# trading night, since both are inherently daily-fresh signals (breakout
# alerts are about TODAY's volume/price action; multibagger candidates feed
# the next morning's /testbrief). No sector filter — confirmed live it
# doesn't reliably filter anyway, and full-market coverage is what the
# nightly sweep needs.
# ==========================================


def build_rapidapi_market_intelligence_sweep() -> dict:
    """
    One call each to breakout/alerts, multibagger/scan, and sector-rotation
    — each independently try/excepted internally (a fetch failure returns
    None for that key, never raises) so one failing doesn't block the
    others. Returns {"breakout_alerts": {...}|None, "multibagger": {...}|None,
    "sector_rotation": {...}|None}.
    """
    breakout = broker_engine.fetch_rapidapi_breakout_alerts()
    multibagger = broker_engine.fetch_rapidapi_multibagger_scan()
    sector_rotation = broker_engine.fetch_rapidapi_sector_rotation()
    return {"breakout_alerts": breakout, "multibagger": multibagger, "sector_rotation": sector_rotation}


def save_rapidapi_market_intelligence(data: dict):
    meta = {"trading_day_marker": core.get_current_calendar_date_marker()}
    ok = cache_manager.set("rapidapi_market_intel", {"data": data}, meta=meta)
    if ok:
        n_breakout = len((data.get("breakout_alerts") or {}).get("alerts") or [])
        n_multibagger = len((data.get("multibagger") or {}).get("candidates") or [])
        print(f"💾 RapidAPI market intelligence tersimpan: {n_breakout} breakout alert, {n_multibagger} multibagger candidate")
    else:
        print("⚠️ Gagal menyimpan RapidAPI market intelligence cache.")


def load_rapidapi_market_intelligence() -> dict:
    payload = cache_manager.get("rapidapi_market_intel", default={})
    return payload.get("data", {}) if isinstance(payload, dict) else {}


def get_breakout_alert_for_ticker(ticker: str) -> dict | None:
    """Cheap indexed lookup, built fresh from load_rapidapi_market_intelligence() each call."""
    alerts = (load_rapidapi_market_intelligence().get("breakout_alerts") or {}).get("alerts") or []
    return next((a for a in alerts if a.get("symbol") == ticker), None)


def get_multibagger_candidate_for_ticker(ticker: str) -> dict | None:
    """Cheap indexed lookup, built fresh from load_rapidapi_market_intelligence() each call."""
    candidates = (load_rapidapi_market_intelligence().get("multibagger") or {}).get("candidates") or []
    return next((c for c in candidates if c.get("symbol") == ticker), None)


# ==========================================
# 💹 BROKSUM 250 (MBSS v2, user request) — broker summary batch untuk 250
# saham berskor tertinggi dari cache /eodscan, di-fetch SEKALI tiap malam
# (5 panggilan batch x 50 ticker = 250 ticker, PERSIS pas kuota harian
# Index Alpha 5x/hari). /broksum siang/sore TINGGAL baca cache ini,
# TIDAK fetch live sama sekali — supaya tidak rebutan kuota antara nightly
# job dan pemakaian interaktif sepanjang hari.
# ==========================================
BROKSUM_250_BATCH_SIZE = 50
BROKSUM_250_TOTAL_TICKERS = 100  # direvisi dari 250 (user request — kuota bulanan Index Alpha hampir habis): 100 ticker = pas 2 panggilan batch (50+50), bukan 5. Efek sampingnya BAGUS ke depan: 2 kredit/hari vs 5/hari — 150/bulan jadi tahan ~75 hari, bukan ~30, jauh lebih toleran kalau /eodscan sempat dijalankan berulang dalam sehari.
BROKSUM_250_LOOKBACK_DAYS = 10  # direvisi dari 7 (user request)


def build_broksum_250(results: list) -> dict:
    """
    Ambil BROKSUM_250_TOTAL_TICKERS ticker berskor Final tertinggi dari hasil
    /eodscan malam ini, fetch broker-summary batch via endpoint resmi Index
    Alpha. Return dict {ticker: [baris broker, ...]}.
    """
    scored_with_final = [r for r in results if r and r.get("scores", {}).get("final") is not None]
    top250 = sorted(scored_with_final, key=lambda r: r["scores"]["final"], reverse=True)[:BROKSUM_250_TOTAL_TICKERS]
    tickers = [r["ticker"] for r in top250]

    if not tickers:
        print("⚠️ BROKSUM 250: tidak ada ticker berskor untuk di-fetch.")
        return {}

    to_date = core.datetime.datetime.now(core.WIB).date()
    from_date = to_date - core.datetime.timedelta(days=BROKSUM_250_LOOKBACK_DAYS)
    from_str, to_str = from_date.strftime("%Y-%m-%d"), to_date.strftime("%Y-%m-%d")

    all_data = {}
    chunks = [tickers[i:i + BROKSUM_250_BATCH_SIZE] for i in range(0, len(tickers), BROKSUM_250_BATCH_SIZE)]
    print(f"💹 BROKSUM 250: fetch {len(tickers)} ticker berskor tertinggi lewat {len(chunks)} panggilan batch...")
    for i, chunk in enumerate(chunks, 1):
        result = broker_engine.fetch_broker_summary_batch_raw(chunk, from_str, to_str, investor="all")
        if result:
            all_data.update(result)
        print(f"💹 BROKSUM 250: batch {i}/{len(chunks)} — {len(result) if result else 0} ticker berhasil")

    print(f"💹 BROKSUM 250: total {len(all_data)}/{len(tickers)} ticker berhasil di-fetch.")
    return all_data


def save_broksum_250(data: dict):
    """
    MBSS v2 BUGFIX (user request — "tarikan kemarin jadi hilang"): SEBELUMNYA
    fungsi ini menimpa cache lama TANPA SYARAT, termasuk kalau hasil fetch
    hari ini KOSONG (mis. kuota Index Alpha habis, semua 5 panggilan batch
    gagal) — jadi data kemarin yang masih bagus & masih berguna (meski
    sedikit lagging) ikut TERHAPUS oleh kegagalan hari ini. SEKARANG: kalau
    hasil baru kosong atau jauh lebih kecil dari yang sudah ada (<50%,
    indikasi gagal parsial), JANGAN timpa — pertahankan data lama, catat
    tanggal fetch SUKSES terakhir supaya /broksum bisa transparan soal
    seberapa lama data itu ("data dari 3 hari lalu", bukan diam-diam basi).
    """
    existing = load_broksum_250()
    if not data:
        print(f"⚠️ BROKSUM 250: hasil fetch hari ini KOSONG — cache lama ({len(existing)} ticker) DIPERTAHANKAN, tidak ditimpa.")
        return
    if existing and len(data) < len(existing) * 0.5:
        print(f"⚠️ BROKSUM 250: hasil fetch baru cuma {len(data)} ticker (vs {len(existing)} sebelumnya) — kemungkinan gagal parsial, cache lama DIPERTAHANKAN.")
        return

    meta = {
        "trading_day_marker": core.get_current_calendar_date_marker(),
        "last_successful_fetch_date": core.get_current_calendar_date_marker(),
    }
    ok = cache_manager.set("broksum_250", {"data": data}, meta=meta)
    if ok:
        print(f"💾 BROKSUM 250 tersimpan (cache/broksum_250.pkl): {len(data)} ticker")
    else:
        print("⚠️ Gagal menyimpan BROKSUM 250 cache.")


def get_broksum_250_age_info() -> dict | None:
    """
    MBSS v2 (user request — transparansi umur data): baca meta cache buat
    tahu tanggal fetch SUKSES terakhir, berapa hari lagging dari hari ini.
    Dipakai /broksum supaya user tahu jelas kalau datanya bukan dari
    hari ini (mis. kuota Index Alpha habis beberapa hari), bukan diam-diam.
    """
    meta = cache_manager.get_meta("broksum_250")
    if not meta or not meta.get("last_successful_fetch_date"):
        return None
    last_date_str = meta["last_successful_fetch_date"]
    try:
        last_date = core.datetime.datetime.strptime(last_date_str, "%Y-%m-%d").date()
        today = core.datetime.datetime.now(core.WIB).date()
        days_lagging = (today - last_date).days
        return {"last_fetch_date": last_date_str, "days_lagging": days_lagging}
    except Exception:
        return None


def load_broksum_250() -> dict:
    """
    MBSS v2 (user request — "jalankan saja sesuai cache yang tersedia"):
    TIDAK LAGI cek basi/tanggal — return apa pun yang tersimpan, berapa pun
    umurnya. Beda dari daily_scan_cache (yang genuinely butuh data harga
    HARI YANG BENAR karena representasi closing tertentu) — broksum itu
    pola akumulasi broker yang tetap informatif walau beberapa hari lalu,
    dan /eodscan yang dijalankan sore/malam (setelah 16:30 tapi sebelum
    tengah malam) sempat tertahan "basi" cuma karena selisih menit dari
    penanda kalender — user eksplisit tidak mau pengecekan seketat itu.
    """
    meta = cache_manager.get_meta("broksum_250")
    if not meta:
        return {}
    payload = cache_manager.get("broksum_250", default={})
    return payload.get("data", {}) if isinstance(payload, dict) else {}


# ==========================================
# 🏦 BIAS BANDAR — histori harian broksum + klasifikasi trend (MBSS v2,
# user request, setelah studi kasus TMPO/MDIA/JGLE/DOOH/ICON manual). Ini
# MULAI DARI NOL — broksum_250 sebelumnya cuma snapshot "sekarang", ditimpa
# tiap malam. Sekarang SETIAP malam, snapshot itu DITAMBAHKAN ke histori
# (bukan cuma disimpan sesaat) — supaya beberapa hari ke depan bisa dilihat
# TREND-nya (naik/mendatar/berbalik), bukan cuma angka statis.
# ==========================================
BROKSUM_DAILY_HISTORY_MAX_DAYS = 15  # cukup buat lihat trend 10 hari + buffer, tidak menumpuk tanpa batas


def append_broksum_daily_history(broksum_250_data: dict):
    """
    Simpan snapshot HARI INI (per ticker, per broker whitelist, net_value &
    avg_buy_price) sebagai 1 ENTRI BARU dalam deret waktu — bukan menimpa.
    Cuma broker WHITELIST yang disimpan (bukan semua broker) — hemat
    ukuran file, dan cuma itu yang relevan buat klasifikasi Bias Bandar.
    """
    import engine.broker as broker_engine

    history = cache_manager.get("broksum_daily_history", default={})
    if not isinstance(history, dict):
        history = {}
    today_marker = core.get_current_calendar_date_marker()

    for ticker, rows in broksum_250_data.items():
        whitelist_rows = [r for r in rows if r.get("code") in broker_engine.SMART_MONEY_BROKER_WHITELIST]
        if not whitelist_rows:
            continue
        entries = history.setdefault(ticker, [])
        # Dedup tanggal yang sama (kalau /eodscan sempat dijalankan >1x sehari)
        entries = [e for e in entries if e.get("date") != today_marker]
        entries.append({
            "date": today_marker,
            "brokers": [
                {"code": r.get("code"), "buy_value": r.get("buy_value"), "sell_value": r.get("sell_value"),
                 "buy_avg": r.get("buy_avg"), "sell_avg": r.get("sell_avg")}
                for r in whitelist_rows
            ],
        })
        history[ticker] = sorted(entries, key=lambda e: e["date"])[-BROKSUM_DAILY_HISTORY_MAX_DAYS:]

    ok = cache_manager.set("broksum_daily_history", history)
    if ok:
        print(f"💾 Bias Bandar: histori harian tersimpan untuk {len(history)} ticker")
    else:
        print("⚠️ Gagal menyimpan histori harian broksum.")


def load_broksum_daily_history() -> dict:
    history = cache_manager.get("broksum_daily_history", default={})
    return history if isinstance(history, dict) else {}
