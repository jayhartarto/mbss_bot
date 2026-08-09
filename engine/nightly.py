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

        # Market breadth + sector returns + regime — dihitung dari data yang
        # SAMA yang baru saja dikumpulkan di atas (results), tanpa fetch baru.
        try:
            breadth = market_engine.compute_market_breadth(results)
            market_engine.save_market_context(breadth)
        except Exception as e:
            print(f"⚠️ Gagal menghitung market breadth: {e}")
            breadth = None

        top_ticker = results[0]["ticker"] if results else "-"
        top_score = results[0].get("scores", {}).get("final") if results else None
        breadth_line = ""
        if breadth:
            breadth_line = (
                f"Breadth: {breadth['advancers']} naik / {breadth['decliners']} turun "
                f"({breadth['breadth_pct_advancing']}%), regime={breadth['regime']}\n"
            )
        try:
            await context.bot.send_message(
                chat_id=core.TELEGRAM_CHAT_ID,
                text=(
                    f"✅ Night scan selesai\n"
                    f"Universe: {universe_label}\n"
                    f"Scored: {len(results)}\n"
                    f"Skipped: {len(skip_reasons)}\n"
                    f"Top: {top_ticker} ({top_score})\n"
                    f"{breadth_line}"
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
