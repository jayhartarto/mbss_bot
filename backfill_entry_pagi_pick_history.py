#!/usr/bin/env python3
"""
backfill_entry_pagi_pick_history.py

MBSS v2 (user request 2026-10-03): entry_pagi_pick_history.json baru
dibuat (streak-ordered tagging, lihat engine/scanalert.py get_entry_pagi_
streak/record_entry_pagi_picks, memory project_entrypagi_streak_backtest_
2026_10_01) -- TIDAK ADA histori sebelum file ini ada, jadi tag streak
akan mulai dari 1x semua dan baru dapat tag "berturut-turut" setelah
beberapa hari bursa lewat kalau dibiarkan kosong. Script ini BACKFILL
beberapa hari bursa terakhir supaya streak langsung ada konteksnya begitu
/entrypagi pertama jalan, pola PERSIS backfill_lane_pick_history.py --
REUSE fungsi produksi asli (compute_factor_scoring, entry_pagi_dna_gate_
pass, entry_pagi_oversold_bounce_gate_pass, compute_atr_pct14_p75,
compute_oversold_bounce_thresholds, record_entry_pagi_picks), BUKAN
reimplementasi formula:

  1. Untuk tiap hari bursa dalam window backfill, monkey-patch
     core.get_ohlcv_smart supaya HANYA mengembalikan data SAMPAI hari itu
     (no-lookahead genuine) -- lalu panggil scoring.compute_factor_scoring
     ASLI utk SELURUH ticker_whitelist.json, dapat pct_b/atr_pct14/
     ret_5d_pct/rsi/price_vs_sma20_pct/value_traded persis spt kalau
     /entrypagi benar-benar jalan hari itu.
  2. Hitung cross-sectional atr_p75 & oversold thresholds dari populasi
     hari itu (compute_atr_pct14_p75/compute_oversold_bounce_thresholds --
     fungsi produksi yang SAMA dipakai live), evaluasi entry_pagi_dna_
     gate_pass/entry_pagi_oversold_bounce_gate_pass per ticker, kumpulkan
     yang lolos.
  3. record_entry_pagi_picks() utk tiap source (entry_pagi_momentum/
     entry_pagi_bounce), pick_date HISTORIS (bukan hari ini). Urutan
     insert tidak masalah -- get_entry_pagi_streak selalu sort ulang
     tanggal dari history, bukan bergantung urutan append.
  4. Restore get_ohlcv_smart asli setelah selesai.

Jalankan SEKALI di server (python backfill_entry_pagi_pick_history.py),
bukan di dev clone -- entry_pagi_pick_history.json gitignored,
per-deployment. Aman dijalankan ulang (idempotent, record_entry_pagi_picks
dedup by ticker+pick_date+source).
"""
from __future__ import annotations

import json
import sys

import pandas as pd

sys.path.insert(0, ".")
from engine import legacy_core as core  # noqa: E402
from engine import scoring  # noqa: E402
from engine import scanalert as scanalert_engine  # noqa: E402

BACKFILL_TRADING_DAYS = 5  # "5 hari ke belakang" -- jumlah hari bursa pick_date yg dibackfill


def _make_truncated_fetcher(real_fetch, as_of_date):
    # **kwargs (bukan cuma `limit`) -- compute_factor_scoring memanggil
    # get_ohlcv_smart dgn skip_live_refresh=True juga; tanpa **kwargs di
    # sini, panggilan itu TypeError ("unexpected keyword argument") krn
    # wrapper ini yg menggantikan core.get_ohlcv_smart selama truncated-
    # fetch phase. Ditemukan sewaktu smoke-test lokal script ini 2026-10-03
    # -- backfill_lane_pick_history.py punya wrapper serupa TANPA **kwargs,
    # kemungkinan sudah punya bug laten yang sama (belum dicek/diperbaiki
    # di sini, luar scope).
    def _fetcher(ticker, limit=500, **kwargs):
        df = real_fetch(ticker, limit=limit + 40, **kwargs)  # buffer ekstra sblm dipotong
        if df is None or df.empty:
            return df
        idx_dates = pd.to_datetime(df.index).date
        mask = idx_dates <= as_of_date
        return df[mask].tail(limit)
    return _fetcher


def _noop_blacklist_write(*args, **kwargs):
    pass  # lihat catatan main() -- WAJIB no-op selama truncated-fetch phase


def main():
    with open(core.WHITELIST_CACHE_FILE) as f:
        universe = json.load(f).get("eligible_tickers", [])
    print(f"Universe: {len(universe)} ticker (ticker_whitelist.json)")

    # BAHAYA NYATA (sama seperti backfill_lane_pick_history.py) --
    # get_ohlcv_smart/compute_factor_scoring membandingkan bar TERAKHIR yg
    # dikembalikan vs TANGGAL ASLI HARI INI (wall-clock) -- begitu data
    # ditruncate ke tanggal historis, bar terakhir jadi "tampak" mandek
    # berhari-hari, kepicu proxy-suspensi (record_direct_evidence_
    # blacklist) yang MENULIS PERMANEN ke failed_fetch_tracking.json
    # (blacklist 30 hari, dipakai SEMUA scan produksi). No-op-kan DULU.
    real_record_direct_evidence_blacklist = core.record_direct_evidence_blacklist
    real_record_fetch_result = core.record_fetch_result
    core.record_direct_evidence_blacklist = _noop_blacklist_write
    core.record_fetch_result = _noop_blacklist_write

    real_get_ohlcv_smart = core.get_ohlcv_smart
    sample_hist = real_get_ohlcv_smart("BBCA", limit=30)
    if sample_hist is None or sample_hist.empty:
        raise SystemExit("Gagal ambil histori sample (BBCA) -- cek koneksi/DB.")
    trading_dates = sorted(set(pd.to_datetime(sample_hist.index).date))
    # Exclude hari terakhir (hari ini/paling baru -- belum tentu closed/final).
    pick_dates = trading_dates[-(BACKFILL_TRADING_DAYS + 1):-1]
    print(f"Trading dates tersedia (sample): {trading_dates[-8:]}")
    print(f"Pick dates yg akan dibackfill: {pick_dates}")

    for pick_date in pick_dates:
        core.get_ohlcv_smart = _make_truncated_fetcher(real_get_ohlcv_smart, pick_date)
        pick_date_str = str(pick_date)

        scored = {}
        for ticker in universe:
            try:
                r = scoring.compute_factor_scoring(ticker, include_quote_check=False)
            except Exception:
                continue
            if r:
                scored[ticker] = r

        atr_p75 = scanalert_engine.compute_atr_pct14_p75(scored)
        oversold_thresholds = scanalert_engine.compute_oversold_bounce_thresholds(scored)

        momentum_pass = [
            t for t, info in scored.items()
            if scanalert_engine.entry_pagi_dna_gate_pass(info, atr_p75)
        ]
        bounce_pass = [
            t for t, info in scored.items()
            if scanalert_engine.entry_pagi_oversold_bounce_gate_pass(info, oversold_thresholds)
        ]

        scanalert_engine.record_entry_pagi_picks(
            momentum_pass, scanalert_engine.SOURCE_ENTRY_PAGI_MOMENTUM, pick_date_str
        )
        scanalert_engine.record_entry_pagi_picks(
            bounce_pass, scanalert_engine.SOURCE_ENTRY_PAGI_BOUNCE, pick_date_str
        )
        print(f"  {pick_date_str}: {len(scored)} ticker ter-score, "
              f"{len(momentum_pass)} momentum gate-pass, {len(bounce_pass)} bounce gate-pass")

    core.get_ohlcv_smart = real_get_ohlcv_smart  # WAJIB restore sebelum script selesai
    core.record_direct_evidence_blacklist = real_record_direct_evidence_blacklist
    core.record_fetch_result = real_record_fetch_result

    print(f"\n✅ Backfill selesai -- cek {scanalert_engine.ENTRY_PAGI_PICK_HISTORY_FILE}")


if __name__ == "__main__":
    main()
