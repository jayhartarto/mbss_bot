"""
backtest/research_speed_to_move.py — MBSS v2, user request ("probability
atau prioritas untuk entry di EOD sedini mungkin"). Run on the server:

    python backtest/research_speed_to_move.py

Question: among RESOLVED /winrate picks (win/lose/win_timebased/
lose_timebased), which EOD features (already snapshotted at pick-lock time
in feature_snapshot — see lock_daily_daytrade_picks in engine/legacy_core.py)
correlate with FAST resolution (few days_checked) vs SLOW multi-day drift?
If a clear pattern exists, that becomes the basis for an "urgency"/priority
score to flag which tonight's picks are likelier to move first thing
tomorrow — instead of treating all picks as equally time-sensitive.

NOT a formula change by itself — purely descriptive statistics. Any new
formula from this should only happen after a real pattern shows up here,
per this project's own "no hunch-based tuning" discipline.
"""
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.legacy_core as core


def _resolved(history):
    return [p for p in history if p.get("status") in ("win", "lose", "win_timebased", "lose_timebased") and p.get("days_checked")]


def _bucket_stats(label, group):
    if not group:
        return
    wins = [p for p in group if p["status"] in ("win", "win_timebased")]
    winrate = len(wins) / len(group) * 100
    avg_days = statistics.mean(p["days_checked"] for p in group)
    avg_gain = statistics.mean(p["pnl_pct"] for p in group if p.get("pnl_pct") is not None)
    print(f"  {label:<28} n={len(group):<4} winrate={winrate:5.1f}%  avg_days={avg_days:4.1f}  avg_gain={avg_gain:+5.1f}%")


def numeric_bucket_analysis(resolved, feature_key, label, bins):
    print(f"\n📊 {label} (feature_snapshot.{feature_key})")
    for lo, hi, name in bins:
        group = [
            p for p in resolved
            if (v := (p.get("feature_snapshot") or {}).get(feature_key)) is not None
            and isinstance(v, (int, float)) and lo <= v < hi
        ]
        _bucket_stats(name, group)


def bool_bucket_analysis(resolved, feature_key, label):
    print(f"\n📊 {label} (feature_snapshot.{feature_key})")
    for val, name in [(True, "True"), (False, "False/None")]:
        group = [p for p in resolved if bool((p.get("feature_snapshot") or {}).get(feature_key)) == val]
        _bucket_stats(name, group)


def main():
    history = core.load_daytrade_picks_history()
    resolved = _resolved(history)
    print(f"📋 Total picks di history: {len(history)}, resolved (ada days_checked): {len(resolved)}\n")
    if len(resolved) < 30:
        print("⚠️ Sampel masih sangat kecil (<30) — hasil di bawah ini BARU indikasi awal, jangan dijadikan dasar formula baru dulu.")

    print("=" * 70)
    print("FAST (<=1 hari) vs SLOW (>=3 hari) — winrate & avg gain")
    print("=" * 70)
    fast = [p for p in resolved if p["days_checked"] <= 1]
    mid = [p for p in resolved if 2 <= p["days_checked"] <= 2]
    slow = [p for p in resolved if p["days_checked"] >= 3]
    _bucket_stats("FAST (<=1 hari)", fast)
    _bucket_stats("MID (2 hari)", mid)
    _bucket_stats("SLOW (>=3 hari)", slow)

    numeric_bucket_analysis(resolved, "adx", "ADX (trend strength) vs kecepatan", [
        (0, 20, "ADX <20 (lemah)"), (20, 25, "ADX 20-25 (netral)"), (25, 999, "ADX >=25 (kuat)"),
    ])
    numeric_bucket_analysis(resolved, "cmf", "CMF (money flow) vs kecepatan", [
        (-999, -0.1, "CMF <-0.1 (negatif)"), (-0.1, 0.1, "CMF netral"), (0.1, 999, "CMF >0.1 (positif)"),
    ])
    numeric_bucket_analysis(resolved, "vol_ratio", "Volume ratio vs kecepatan", [
        (0, 1.0, "Vol <1.0x"), (1.0, 2.0, "Vol 1.0-2.0x"), (2.0, 999, "Vol >=2.0x"),
    ])
    numeric_bucket_analysis(resolved, "value_traded", "Value traded (Rp) vs kecepatan", [
        (0, 3_000_000_000, "<3B"), (3_000_000_000, 10_000_000_000, "3B-10B"),
        (10_000_000_000, 25_000_000_000, "10B-25B"), (25_000_000_000, float("inf"), ">=25B"),
    ])
    numeric_bucket_analysis(resolved, "day_range_pct_10d", "Day range 10D (volatilitas) vs kecepatan", [
        (0, 10, "<10%"), (10, 20, "10-20%"), (20, 999, ">=20%"),
    ])

    bool_bucket_analysis(resolved, "bollinger_squeeze", "Bollinger squeeze vs kecepatan")
    bool_bucket_analysis(resolved, "is_below_ema21", "Below EMA21 vs kecepatan")

    print("\n📊 HC criteria met/checkable ratio vs kecepatan")
    for lo, hi, name in [(0.0, 0.5, "<50% kriteria HC"), (0.5, 0.85, "50-85% kriteria HC"), (0.85, 1.01, ">=85% kriteria HC")]:
        group = []
        for p in resolved:
            fs = p.get("feature_snapshot") or {}
            met, checkable = fs.get("high_conviction_met"), fs.get("high_conviction_checkable")
            if met is None or not checkable:
                continue
            ratio = met / checkable
            if lo <= ratio < hi:
                group.append(p)
        _bucket_stats(name, group)

    print("\n📊 Consecutive streak (lintas-tool) vs kecepatan")
    for n in (1, 2, 3, 4):
        label = f"Streak {n}x" if n < 4 else "Streak >=4x"
        group = [p for p in resolved if (p.get("consecutive_streak") == n if n < 4 else (p.get("consecutive_streak") or 0) >= 4)]
        _bucket_stats(label, group)

    print("\nDone. Kalau ada bucket yang winrate/avg_days-nya jauh beda dari yang lain DAN n cukup besar (>=15-20), itu kandidat sinyal 'urgency' yang worth dibangun jadi skor eksplisit.")


if __name__ == "__main__":
    main()
