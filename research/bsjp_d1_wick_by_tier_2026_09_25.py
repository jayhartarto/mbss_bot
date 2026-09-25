"""
MBSS v2 research (2026-09-25) -- follow-up to bsjp_d1_wick_quality_2026_09_25.py.
User's question: within the SAME production conviction tier (STAGE1_BASE/
TIER1/TIER2/TIER3/TIER_EXTREME, engine/bsjp2._assign_conviction_tier), does
a big D1 upper wick (closed well below the entry day's own high) still
degrade forward quality -- or is that effect just a proxy for tier itself?

Reuses the EXACT production tier classifier (engine/bsjp2._assign_conviction_tier)
so tier labels here match what /bsjp actually assigns, not an approximation.
RSI14 (needed for TIER_EXTREME) is computed causally as-of D1's own close,
same Wilder formula as engine/legacy_core.calculate_rsi.

Output: printed summary, no new parquet (reuses .tmp_bsjp_d1_wick_quality.parquet).
"""
import pandas as pd
import numpy as np
import sys
sys.path.insert(0, ".")

from engine.bsjp2 import _assign_conviction_tier

DAILY_CSV = "research/ohlcv_backtest_raw.csv"
WICK = "research/.tmp_bsjp_d1_wick_quality.parquet"


def calculate_rsi(prices, period=14):
    delta = prices.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / (avg_loss + 1e-9)
    return 100 - (100 / (1 + rs))


def summarize(sub, label):
    n = len(sub)
    if n < 15:
        return None
    return {
        "label": label, "n": n,
        "win%": (sub["close_stretch"] > 0).mean() * 100,
        "mean": sub["close_stretch"].mean(),
        "median": sub["close_stretch"].median(),
        "mean_mae": sub["mae_tp_aware"].mean(),
        "tail<-8%": (sub["mae_tp_aware"] < -8).mean() * 100,
        "tp2_touch%": sub["tp2_hit"].mean() * 100,
    }


def fmt(r):
    return (f"n={r['n']:>4} win={r['win%']:.1f}% mean={r['mean']:+.2f}% median={r['median']:+.2f}% "
            f"mean_mae={r['mean_mae']:.2f}% tail<-8%={r['tail<-8%']:.1f}% tp2_touch={r['tp2_touch%']:.1f}%")


def main():
    m = pd.read_parquet(WICK)
    daily = pd.read_csv(DAILY_CSV, parse_dates=["date"]).sort_values(["ticker", "date"])

    # Causal RSI14 as-of D1's own close, per ticker, same formula as production.
    daily["rsi14"] = daily.groupby("ticker")["close"].transform(calculate_rsi)
    rsi_lookup = daily.rename(columns={"date": "d1_date", "rsi14": "rsi14_d1"})[["ticker", "d1_date", "rsi14_d1"]]
    m = m.merge(rsi_lookup, on=["ticker", "d1_date"], how="left")

    m["prod_tier"] = m.apply(
        lambda r: _assign_conviction_tier(
            r["ret_2d_cum"], r["pct_b_prior"], r["gap_pct"], r["atr_pct_prior"], r["rsi14_d1"],
        ),
        axis=1,
    )
    print(m["prod_tier"].value_counts())
    print()

    for tier in ["STAGE1_BASE", "TIER1", "TIER2", "TIER3", "TIER_EXTREME"]:
        sub = m[m["prod_tier"] == tier]
        if len(sub) < 30:
            print(f"{tier}: n={len(sub)} -- too small, skip")
            continue
        base = summarize(sub, f"{tier} ALL")
        print(f"=== {tier} (n={len(sub)}) baseline: {fmt(base)}")

        med = sub["upper_wick_pct"].median()
        lo = summarize(sub[sub["upper_wick_pct"] < med], f"{tier} upper_wick<median({med:.1f}%)")
        hi = summarize(sub[sub["upper_wick_pct"] >= med], f"{tier} upper_wick>=median")
        if lo:
            print(f"  small wick: {fmt(lo)}")
        if hi:
            print(f"  BIG wick:   {fmt(hi)}")

        # Same spike+fade combo as before, tier-local threshold
        q75 = sub["upper_wick_pct"].quantile(0.75)
        combo_mask = (sub["upper_wick_pct"] >= q75) & (sub["close_position_in_range"] < 0.5)
        combo = summarize(sub[combo_mask], f"{tier} spike+fade combo")
        rest = summarize(sub[~combo_mask], f"{tier} rest")
        if combo and rest:
            print(f"  spike+fade: {fmt(combo)}")
            print(f"  rest:       {fmt(rest)}")
        print()


if __name__ == "__main__":
    main()
