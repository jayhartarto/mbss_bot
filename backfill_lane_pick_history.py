#!/usr/bin/env python3
"""
backfill_lane_pick_history.py

MBSS v2 (user request — "simulasikan pick hari ini punya pick history juga,
build history dari 5d lalu"): VALIDATION section /screendaytrade butuh
entry_price/day1_pnl_pct/day2_pnl_pct DARI PICK HISTORY RIIL (lock_daily_
daytrade_picks + resolve_daytrade_picks, jalan otomatis tiap malam via
nightly.py) -- tapi source "screendaytrade_macd_lane" baru mulai dikunci
sejak redesign SDT hari ini, jadi VALIDATION kosong sampai beberapa siklus
/eodscan lewat. Script ini BACKFILL beberapa hari bursa terakhir supaya
VALIDATION langsung ada datanya, TANPA menunggu.

Cara kerja -- REUSE fungsi produksi asli (compute_factor_scoring,
resolve_daytrade_picks), BUKAN reimplementasi formula:
  1. Untuk tiap hari bursa dalam window backfill, monkey-patch
     core.get_ohlcv_smart supaya HANYA mengembalikan data SAMPAI hari itu
     (no-lookahead genuine) -- lalu panggil scoring.compute_factor_scoring
     ASLI, dapat macd_approach_tier + targets persis seperti kalau
     /screendaytrade benar-benar jalan hari itu.
  2. Kandidat lane (FAST_RECOVERY/EARLY_RECOVERY/ABOVE_MOMENTUM) dikunci ke
     daytrade_picks_history.json dengan pick_date HISTORIS (bukan hari ini)
     via struktur field PERSIS sama dengan lock_daily_daytrade_picks.
  3. Restore get_ohlcv_smart asli, lalu panggil resolve_daytrade_picks()
     ASLI (bukan simulasi manual) -- pakai data REAL sampai hari ini utk
     entry_price/day1-3_pnl_pct/status, sama seperti proses malam biasa.

Jalankan SEKALI di server (python backfill_lane_pick_history.py), bukan di
dev clone -- daytrade_picks_history.json gitignored, per-deployment.
"""
from __future__ import annotations

import json
import sys

import pandas as pd

sys.path.insert(0, ".")
from engine import legacy_core as core  # noqa: E402
from engine import scoring  # noqa: E402

BACKFILL_TRADING_DAYS = 5  # "5d lalu" -- jumlah hari bursa pick_date yg dibackfill
LANES = ("FAST_RECOVERY", "EARLY_RECOVERY", "ABOVE_MOMENTUM")


def _make_truncated_fetcher(real_fetch, as_of_date):
    def _fetcher(ticker, limit=500):
        df = real_fetch(ticker, limit=limit + 40)  # buffer ekstra sblm dipotong
        if df is None or df.empty:
            return df
        idx_dates = pd.to_datetime(df.index).date
        mask = idx_dates <= as_of_date
        return df[mask].tail(limit)
    return _fetcher


def _noop_blacklist_write(*args, **kwargs):
    pass  # lihat catatan _run_backfill_pass -- WAJIB no-op selama truncated-fetch phase


def main():
    with open(core.WHITELIST_CACHE_FILE) as f:
        universe = json.load(f).get("eligible_tickers", [])
    print(f"Universe: {len(universe)} ticker (ticker_whitelist.json)")

    # BAHAYA NYATA ditemukan saat testing: get_ohlcv_smart/compute_factor_
    # scoring membandingkan bar TERAKHIR yg dikembalikan vs TANGGAL ASLI HARI
    # INI (wall-clock) -- begitu kita truncate data ke tanggal historis, bar
    # terakhir jadi "tampak" mandek berhari-hari, kepicu proxy-suspensi
    # (record_direct_evidence_blacklist) yang MENULIS PERMANEN ke
    # failed_fetch_tracking.json (blacklist 30 hari, dipakai SEMUA scan
    # produksi -- /eodscan, /screendaytrade, /hc). No-op-kan DULU sebelum
    # truncated-fetch phase mulai, restore SETELAH selesai -- backfill ini
    # tidak boleh meninggalkan efek samping permanen ke state produksi lain.
    real_record_direct_evidence_blacklist = core.record_direct_evidence_blacklist
    real_record_fetch_result = core.record_fetch_result
    core.record_direct_evidence_blacklist = _noop_blacklist_write
    core.record_fetch_result = _noop_blacklist_write

    real_get_ohlcv_smart = core.get_ohlcv_smart
    sample_hist = real_get_ohlcv_smart("BBCA", limit=30)
    if sample_hist is None or sample_hist.empty:
        raise SystemExit("Gagal ambil histori sample (BBCA) -- cek koneksi/DB.")
    trading_dates = sorted(set(pd.to_datetime(sample_hist.index).date))
    # Exclude hari terakhir (hari ini/paling baru, belum ada data 'besok' utk entry_price).
    pick_dates = trading_dates[-(BACKFILL_TRADING_DAYS + 1):-1]
    print(f"Trading dates tersedia (sample): {trading_dates[-8:]}")
    print(f"Pick dates yg akan dibackfill: {pick_dates}")

    history = core.load_daytrade_picks_history()
    existing_keys = {(p["ticker"], p["pick_date"], p.get("source")) for p in history}
    added = 0

    for pick_date in pick_dates:
        core.get_ohlcv_smart = _make_truncated_fetcher(real_get_ohlcv_smart, pick_date)
        found_this_day = 0
        for ticker in universe:
            key = (ticker, str(pick_date), "screendaytrade_macd_lane")
            if key in existing_keys:
                continue
            try:
                r = scoring.compute_factor_scoring(ticker, include_quote_check=False)
            except Exception:
                continue
            if not r or r.get("macd_approach_tier") not in LANES:
                continue
            targets = r.get("targets") or {}
            if targets.get("tp_1") is None or targets.get("cut_loss") is None:
                continue
            history.append({
                "ticker": ticker, "pick_date": str(pick_date), "source": "screendaytrade_macd_lane",
                "signal_label": r.get("macd_approach_tier"), "consecutive_streak": 1,
                "smart_money_at_lock": [], "feature_snapshot": {"backfilled": True},
                "tp1": targets["tp_1"], "cut_loss": targets["cut_loss"],
                "action_label": r.get("action_id"), "daytrade_score": None,
                "entry_price": None, "entry_date": None, "status": "pending_entry",
                "resolved_date": None, "pnl_pct": None, "days_checked": 0,
                "day1_pnl_pct": None, "day2_pnl_pct": None, "day3_pnl_pct": None,
            })
            existing_keys.add(key)
            added += 1
            found_this_day += 1
        print(f"  {pick_date}: {found_this_day} kandidat lane baru dikunci")

    core.get_ohlcv_smart = real_get_ohlcv_smart  # WAJIB restore sebelum resolve (data asli, no-lookahead selesai)
    core.record_direct_evidence_blacklist = real_record_direct_evidence_blacklist
    core.record_fetch_result = real_record_fetch_result
    core.save_daytrade_picks_history(history)
    print(f"\n✅ {added} pick historis ditambahkan ke {core.DAYTRADE_PICKS_HISTORY_FILE}")

    print("\n🎯 Menjalankan resolve_daytrade_picks() ASLI (entry_price/day1-3_pnl_pct dari data riil)...")
    resolved = core.resolve_daytrade_picks()
    print(f"✅ {resolved} pick diperbarui (entry_price/day1-3_pnl_pct/status).")


if __name__ == "__main__":
    main()
