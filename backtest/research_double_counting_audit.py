"""
backtest/research_double_counting_audit.py — MBSS v2, user request (from
mbss_formula_diagnosis_claude_agent.md's "Risk 1: Double counting of
related signals"). Run on the server:

    python backtest/research_double_counting_audit.py

Question: do several of our "flow/distribution" and "trend risk" signals
actually just detect the SAME underlying event, each adding its own
penalty for one real thing? If CMF, close-position-in-range, and volume
ratio are all highly correlated with each other, a stock in real
distribution gets penalized 3x for 1 real signal — exactly the double-
counting risk the diagnosis brief raised.

IMPORTANT GAP (be upfront): feature_snapshot (lock_daily_daytrade_picks,
engine/legacy_core.py) does NOT currently store `obv_divergence` or
`whitelist_accumulation_net_pct` at pick-lock time — only
`smart_money_at_lock` (which broker net-bought, not a net-sell/distribution
figure) is captured. So this audit can only check the OVERLAP among:
    cmf, close_pos_day, vol_ratio, adx, rsi, day_range_pct_10d,
    relative_strength_vs_ihsg, is_below_ema21, is_below_sma50, value_traded
It CANNOT yet check whether OBV divergence or whitelist net-sell overlap
with these — if this audit shows the checkable signals ARE overlapping,
it's worth ALSO snapshotting obv_divergence/whitelist_accumulation_net_pct
going forward (same tag-and-track pattern as bollinger_squeeze/
fast_candidate) so a fuller audit is possible later.

Purely descriptive — no formula/weight change from this alone.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd

import engine.legacy_core as core

NUMERIC_FIELDS = [
    "cmf", "close_pos_day", "vol_ratio", "adx", "rsi",
    "day_range_pct_10d", "relative_strength_vs_ihsg", "value_traded",
]
BOOL_FIELDS = ["is_below_ema21", "is_below_sma50"]


def main():
    history = core.load_daytrade_picks_history()
    print(f"📋 Total picks di history: {len(history)}\n")

    rows = []
    for p in history:
        fs = p.get("feature_snapshot") or {}
        if not fs:
            continue
        row = {k: fs.get(k) for k in NUMERIC_FIELDS}
        for k in BOOL_FIELDS:
            v = fs.get(k)
            row[k] = 1 if v is True else (0 if v is False else None)
        row["pnl_pct"] = p.get("pnl_pct")
        row["is_win"] = 1 if p.get("status") in ("win", "win_timebased") else (0 if p.get("status") in ("lose", "lose_timebased") else None)
        rows.append(row)

    df = pd.DataFrame(rows)
    print(f"Picks dengan feature_snapshot terisi: {len(df)}\n")
    if len(df) < 30:
        print("⚠️ Sampel masih kecil (<30) — korelasi di bawah BARU indikasi awal.\n")

    feature_cols = NUMERIC_FIELDS + BOOL_FIELDS
    corr = df[feature_cols].apply(pd.to_numeric, errors="coerce").corr()

    print("=" * 78)
    print("KORELASI ANTAR FITUR (Pearson r) — |r| >= 0.4 ditandai sebagai overlap kandidat")
    print("=" * 78)
    flagged = []
    for i, a in enumerate(feature_cols):
        for b in feature_cols[i + 1:]:
            r = corr.loc[a, b]
            if pd.isna(r):
                continue
            marker = " <-- POSSIBLE DOUBLE-COUNT" if abs(r) >= 0.4 else ""
            if abs(r) >= 0.2:  # cuma tampilkan yang cukup berarti, jangan banjiri noise dekat-nol
                print(f"  {a:<28} vs {b:<28} r={r:+.2f}{marker}")
            if abs(r) >= 0.4:
                flagged.append((a, b, r))

    print("\n" + "=" * 78)
    print("KORELASI TIAP FITUR vs OUTCOME (pnl_pct, resolved picks saja)")
    print("=" * 78)
    resolved = df[df["is_win"].notna()]
    print(f"n resolved dengan feature_snapshot: {len(resolved)}\n")
    if len(resolved) >= 10:
        outcome_corr = resolved[feature_cols].apply(pd.to_numeric, errors="coerce").corrwith(resolved["pnl_pct"])
        for col in feature_cols:
            v = outcome_corr.get(col)
            if pd.isna(v):
                continue
            print(f"  {col:<28} r vs pnl_pct = {v:+.2f}")
    else:
        print("Sampel resolved terlalu kecil (<10) untuk korelasi outcome yang berarti.")

    print("\n" + "=" * 78)
    print("RINGKASAN")
    print("=" * 78)
    if flagged:
        print(f"{len(flagged)} pasangan fitur menunjukkan overlap kuat (|r|>=0.4):")
        for a, b, r in flagged:
            print(f"  - {a} <-> {b} (r={r:+.2f})")
        print(
            "\nKalau pasangan ini SECARA KONSEP sama-sama masuk kategori 'flow/distribution' atau "
            "'trend risk' di compute_danger_score, mereka kemungkinan menghukum 1 kondisi riil "
            "berkali-kali (persis kekhawatiran di brief diagnosis). Pertimbangkan: pilih SATU "
            "representatif per kelompok yang overlap, atau turunkan bobot yang lain."
        )
    else:
        print("Tidak ada pasangan dengan overlap kuat (|r|>=0.4) di antara fitur yang bisa dicek.")
        print("Double-counting mungkin lebih kecil dari yang dikhawatirkan brief -- TAPI ingat gap")
        print("di atas: obv_divergence dan whitelist_accumulation_net_pct belum bisa dicek sama sekali.")

    print(
        "\nCATATAN: is_below_ema21 vs is_below_sma50 HAMPIR PASTI berkorelasi tinggi by construction "
        "(dua-duanya threshold harga-vs-MA) -- itu bukan temuan baru, sudah diketahui sejak desain awal."
    )


if __name__ == "__main__":
    main()
