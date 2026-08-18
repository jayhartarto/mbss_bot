"""
backtest/research_value_traded_threshold.py — MBSS v2, user request ("apakah
akan meningkatkan winrate jika cap value traded dinaikkan?"). Run on the
server:

    python backtest/research_value_traded_threshold.py

Buckets RESOLVED /winrate picks by feature_snapshot.value_traded (liquidity
tier, same breakpoints compute_activity_score_v5 in engine/legacy_core.py
already uses: 3B/5B/10B/25B/100B) and reports winrate/avg gain per tier.
If winrate/avg gain clearly climbs at higher tiers, that's real evidence
for raising the current minimum thresholds (e.g. BSJP's 5B floor, Activity
score's 3B floor) -- if it's flat or noisy, current thresholds are probably
fine and raising them would just needlessly shrink the candidate pool.

Purely descriptive -- any actual threshold change should only happen after
this shows a real pattern, per this project's own "no hunch-based tuning,
only evidence" discipline, and only with a bumped formula version + logged
reason.
"""
import os
import statistics
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import engine.legacy_core as core

TIERS = [
    (0, 3_000_000_000, "<3B (di bawah floor Activity)"),
    (3_000_000_000, 5_000_000_000, "3B-5B"),
    (5_000_000_000, 10_000_000_000, "5B-10B (floor BSJP)"),
    (10_000_000_000, 25_000_000_000, "10B-25B"),
    (25_000_000_000, 100_000_000_000, "25B-100B"),
    (100_000_000_000, float("inf"), ">=100B"),
]


def main():
    history = core.load_daytrade_picks_history()
    resolved = [
        p for p in history
        if p.get("status") in ("win", "lose", "win_timebased", "lose_timebased")
        and (p.get("feature_snapshot") or {}).get("value_traded") is not None
    ]
    print(f"📋 Total picks di history: {len(history)}, resolved dengan value_traded tercatat: {len(resolved)}\n")
    if len(resolved) < 30:
        print("⚠️ Sampel masih kecil (<30) -- hasil di bawah BARU indikasi awal, jangan langsung dijadikan dasar naikkan threshold.\n")

    print("=" * 80)
    print(f"{'TIER':<32}{'n':<6}{'winrate':<10}{'avg_gain':<10}{'avg_days'}")
    print("=" * 80)
    rows = []
    for lo, hi, label in TIERS:
        group = [p for p in resolved if lo <= p["feature_snapshot"]["value_traded"] < hi]
        if not group:
            print(f"{label:<32}{0:<6}{'-':<10}{'-':<10}-")
            continue
        wins = [p for p in group if p["status"] in ("win", "win_timebased")]
        winrate = len(wins) / len(group) * 100
        avg_gain = statistics.mean(p["pnl_pct"] for p in group if p.get("pnl_pct") is not None)
        days = [p["days_checked"] for p in group if p.get("days_checked")]
        avg_days = statistics.mean(days) if days else None
        avg_days_s = f"{avg_days:.1f}" if avg_days is not None else "-"
        print(f"{label:<32}{len(group):<6}{winrate:5.1f}%   {avg_gain:+5.1f}%   {avg_days_s}")
        rows.append((label, len(group), winrate, avg_gain))

    print("\nBaca hasil ini begini:")
    print("- Kalau winrate/avg_gain NAIK KONSISTEN seiring tier makin tinggi (DAN n tiap tier cukup, >=15-20), itu bukti nyata utk naikkan floor liquiditas.")
    print("- Kalau NAIK lalu DATAR/turun di tier tertinggi, ada 'sweet spot' -- bukan 'makin tinggi makin baik' terus-menerus.")
    print("- Kalau FLAT/noise tanpa pola jelas, current threshold kemungkinan sudah cukup -- menaikkan cuma akan mengecilkan pool kandidat tanpa manfaat winrate.")


if __name__ == "__main__":
    main()
