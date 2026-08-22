"""
bot.py — MBSS v2 Bootstrap / Entrypoint (Sprint 1)

Single entrypoint for the whole app, per Executive Summary:
    python bot.py                          -> starts Telegram polling bot
    python bot.py --eodscan / --nightlyscan -> one-shot EOD pipeline, then exits
    python bot.py --populatedb             -> one-shot OHLCV DB populate, then exits
    python bot.py --dbstats                -> print DB stats, then exits

This file only does: .env/config loading (via legacy_core's import-time
side effect), CLI parsing, logging setup, one-time cache migration, and
dispatch. All actual business logic (nightly scan, scoring, Telegram
handlers, DB access, ...) still lives in engine/legacy_core.py for this
phase of the refactor and gets imported wholesale — it will be split into
engine/nightly.py, engine/market.py, engine/broker.py, engine/gptscore.py
and commands/*.py in the next phases.
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

# ── Logging setup (Error Handling & Logging section of the Executive Summary):
# structured logging with timestamps, before anything else runs so import-time
# side effects in legacy_core (like .env loading) are covered too.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("mbss.bootstrap")

# Importing legacy_core triggers .env loading, key checks, and defines every
# command handler / engine function used below (this is the whole existing
# bot, unmodified except for the CacheManager wiring and the removed
# __main__ block — see the NOTE at the bottom of engine/legacy_core.py).
from engine import legacy_core as core  # noqa: E402
from engine.cache import cache_manager  # noqa: E402
import engine.nightly as nightly_engine  # noqa: E402
import engine.scanalert as scanalert_engine  # noqa: E402


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="bot.py",
        description="MBSS v2 — Telegram trading-assist bot for IDX/ISSI stocks.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--eodscan", "--nightlyscan", dest="eodscan", action="store_true",
        help="Run the one-shot End-of-Day pipeline (fetch, score, save cache), then exit.",
    )
    mode.add_argument(
        "--populatedb", dest="populatedb", action="store_true",
        help="Populate/refresh the OHLCV SQLite DB from Yahoo Finance for the ISSI universe, then exit.",
    )
    mode.add_argument(
        "--dbstats", dest="dbstats", action="store_true",
        help="Print OHLCV DB stats, then exit.",
    )
    mode.add_argument(
        "--repairthin", dest="repairthin", action="store_true",
        help="One-time repair: find tickers stuck with too-few OHLCV bars (pre-fix bug) and force a full 2y backfill, then exit.",
    )
    mode.add_argument(
        "--scanalert", dest="scanalert", action="store_true",
        help="One-shot intraday breaking-alert scan (Alert A/B), then exit. Meant to be invoked "
             "every 5 minutes by an external cron during trading hours (09:00-15:55 WIB).",
    )
    return parser.parse_args(argv)


def run_eodscan() -> None:
    """One-shot EOD pipeline (CLI). Mirrors the /eodscan Telegram command
    but prints admin messages to stdout instead of sending to Telegram."""
    logger.info("EOD scan starting (CLI mode) — refresh DB + compute scores + save shared cache")

    class _DummyBot:
        async def send_message(self, chat_id=None, text=""):
            print(f"\n[TG:{chat_id}] {text}\n")

    class _DummyContext:
        bot = _DummyBot()

    t0 = time.time()
    try:
        asyncio.run(nightly_engine.run_nightly_full_scan(_DummyContext()))
        logger.info("EOD scan finished in %.1fs", time.time() - t0)
    except Exception:
        logger.exception("EOD scan failed after %.1fs", time.time() - t0)
        sys.exit(1)


def run_populatedb() -> None:
    logger.info("DB populate starting (CLI mode) — ISSI universe, 2y history")
    t0 = time.time()
    try:
        core.init_ohlcv_db()
        issi = set(core.fetch_online_sharia_list(index_key="ISSI"))
        tickers = list(issi)
        logger.info("%d unique ISSI tickers to populate", len(tickers))
        stats = core.populate_from_yfinance(tickers, period="2y", batch_size=50)
        db = core.get_db_stats()
        logger.info(
            "DB populate done in %.1fs — %d tickers, %s bars, %s MB, updated to %s",
            time.time() - t0, db.get("daily_tickers", 0), f"{db.get('daily_rows', 0):,}",
            db.get("size_mb", 0), stats.get("latest_marker", "-"),
        )
    except Exception:
        logger.exception("DB populate failed after %.1fs", time.time() - t0)
        sys.exit(1)


def run_dbstats() -> None:
    core.init_ohlcv_db()
    db = core.get_db_stats()
    print("📦 OHLCV Database")
    print(f"   Daily : {db.get('daily_tickers', 0)} ticker, {db.get('daily_rows', 0):,} bar")
    print(f"   4H    : {db.get('h4_tickers', 0)} ticker")
    print(f"   Size  : {db.get('size_mb', 0)} MB")


def run_repairthin() -> None:
    """
    One-time repair (user request — kasus nyata DOSS/FWCT/PMUI): cari ticker
    yang tersangkut bug lama get_ohlcv_smart() (cuma kesimpan ~10 bar padahal
    listing sudah lama), paksa backfill 2 tahun penuh untuk masing-masing.
    """
    logger.info("Repair thin tickers starting (satu kali jalan)")
    t0 = time.time()
    try:
        result = core.repair_thin_tickers()
        logger.info(
            "Repair selesai dalam %.1fs — %d diperbaiki, %d masih tipis dari %d dicek",
            time.time() - t0, len(result["repaired"]), len(result["still_thin"]), result["checked"],
        )
    except Exception:
        logger.exception("Repair thin tickers gagal setelah %.1fs", time.time() - t0)
        sys.exit(1)


def run_scanalert() -> None:
    """One-shot intraday breaking-alert scan (CLI). Meant to run every 5
    minutes via external cron during trading hours — see engine/scanalert.py."""
    logger.info("Scan-alert starting (CLI mode) — Alert A/B intraday breaking scan")
    t0 = time.time()
    try:
        summary = asyncio.run(scanalert_engine.run_scan_alert_once())
        logger.info("Scan-alert finished in %.1fs: %s", time.time() - t0, summary)
    except Exception:
        logger.exception("Scan-alert failed after %.1fs", time.time() - t0)
        sys.exit(1)


def run_polling() -> None:
    """Start the Telegram bot in polling mode, with startup retries in case
    network isn't ready yet (common right after launching on mobile)."""
    logger.info("Bot starting: listening for commands (no JobQueue — request-driven only)")
    STARTUP_RETRIES = 5
    for attempt in range(1, STARTUP_RETRIES + 1):
        try:
            application = core.build_app()
            application.run_polling(
                allowed_updates=core.Update.ALL_TYPES,
                bootstrap_retries=5,  # PTB's own internal retry for the connect step
            )
            break  # run_polling only returns on clean shutdown
        except Exception as e:
            logger.warning("Bot startup failed (attempt %d/%d): %s", attempt, STARTUP_RETRIES, e)
            if attempt < STARTUP_RETRIES:
                logger.info("Retrying in 15 seconds — check your network connection...")
                time.sleep(15)
            else:
                logger.error("Giving up after repeated startup failures. Check your connection and restart manually.")
                raise


def main(argv: list[str] | None = None) -> None:
    args = parse_args(sys.argv[1:] if argv is None else argv)

    # One-time carry-forward of the pre-refactor cache file into the new
    # shared CacheManager, so last night's scan isn't lost by the refactor.
    nightly_engine.migrate_legacy_daily_scan_cache()
    logger.info("Shared cache dir: %s", cache_manager.cache_dir)

    if args.eodscan:
        run_eodscan()
    elif args.populatedb:
        run_populatedb()
    elif args.dbstats:
        run_dbstats()
    elif args.repairthin:
        run_repairthin()
    elif args.scanalert:
        run_scanalert()
    else:
        run_polling()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        logger.info("Stopped by user (Ctrl+C).")
    except Exception:
        # Top-level catch-all so the process never dies with a raw traceback
        # and no context, per the Executive Summary's error-handling policy.
        logger.exception("Fatal error — bot process terminating.")
        sys.exit(1)
