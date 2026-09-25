"""
MBSS v2 research (2026-09-25) -- does the ENTRY DAY (D1)'s own candle shape
(high/low far from close = big wick) predict different forward quality than
a "clean" close near the day's extreme? User's question: a stock whose D1
high ran far above its D1 close (rejected at highs, upper wick) vs one whose
D1 low dipped far below its D1 close (recovered off the lows, lower wick) --
does either indicate a systematically different outcome in the D2-D4 window?

Base population: research/.tmp_bsjp_stage1_revised_pick.parquet (n=4013,
Stage-1 REVISED union, already has close_stretch/mae_tp_aware/win_tp3
computed for the D2-D4 forward window). D1's own OHLC pulled fresh from
research/ohlcv_backtest_raw.csv (not in the pick parquet).

Output: research/.tmp_bsjp_d1_wick_quality.parquet + printed summary.
"""
import pandas as pd
import numpy as np

PICK = "research/.tmp_bsjp_stage1_revised_pick.parquet"
DAILY_CSV = "research/ohlcv_backtest_raw.csv"


def summarize(sub, label):
    n = len(sub)
    if n < 20:
        return None
    return {
        "label": label, "n": n,
        "win%": (sub["close_stretch"] > 0).mean() * 100,
        "mean_close_stretch": sub["close_stretch"].mean(),
        "median_close_stretch": sub["close_stretch"].median(),
        "mean_mae": sub["mae_tp_aware"].mean(),
        "p_mae<-8%": (sub["mae_tp_aware"] < -8).mean() * 100,
        "tp2_touch%": sub["tp2_hit"].mean() * 100 if "tp2_hit" in sub else np.nan,
        "win_tp3%": sub["win_tp3"].mean() * 100,
    }


def main():
    pick = pd.read_parquet(PICK)
    daily = pd.read_csv(DAILY_CSV, parse_dates=["date"])
    d1_ohlc = daily.rename(columns={"date": "d1_date", "open": "d1_open", "high": "d1_high",
                                     "low": "d1_low", "close": "d1_close"})[
        ["ticker", "d1_date", "d1_open", "d1_high", "d1_low", "d1_close"]
    ]

    m = pick.merge(d1_ohlc, on=["ticker", "d1_date"], how="left")
    before = len(m)
    m = m.dropna(subset=["d1_high", "d1_low", "d1_close"])
    print(f"Merged {len(m)}/{before} rows with D1's own OHLC")

    # Also need TP1/TP2/TP3 hit flags -- reuse the tiered parquet (same base
    # population, same join keys) instead of recomputing.
    tiered = pd.read_parquet("research/.tmp_bsjp_stage2_v2_tiered.parquet")[
        ["ticker", "d1_date", "tp1_hit", "tp2_hit", "tp3_hit", "tier"]
    ]
    m = m.merge(tiered, on=["ticker", "d1_date"], how="left")

    m["upper_wick_pct"] = (m["d1_high"] - m["d1_close"]) / m["d1_close"] * 100.0
    m["lower_wick_pct"] = (m["d1_close"] - m["d1_low"]) / m["d1_close"] * 100.0
    m["day_range_pct"] = (m["d1_high"] - m["d1_low"]) / m["d1_close"] * 100.0
    m["close_position_in_range"] = np.where(
        m["day_range_pct"] > 0,
        (m["d1_close"] - m["d1_low"]) / (m["d1_high"] - m["d1_low"]),
        np.nan,
    )  # 0 = closed at day low, 1 = closed at day high

    m.to_parquet("research/.tmp_bsjp_d1_wick_quality.parquet", index=False)

    print(f"\n=== Distribution ===")
    print(f"upper_wick_pct: mean={m['upper_wick_pct'].mean():.2f} median={m['upper_wick_pct'].median():.2f} "
          f"P75={m['upper_wick_pct'].quantile(.75):.2f} P90={m['upper_wick_pct'].quantile(.90):.2f}")
    print(f"lower_wick_pct: mean={m['lower_wick_pct'].mean():.2f} median={m['lower_wick_pct'].median():.2f} "
          f"P75={m['lower_wick_pct'].quantile(.75):.2f} P90={m['lower_wick_pct'].quantile(.90):.2f}")
    print(f"close_position_in_range: mean={m['close_position_in_range'].mean():.2f} "
          f"median={m['close_position_in_range'].median():.2f}")

    print(f"\n=== BASELINE (n={len(m)}) ===")
    base = summarize(m, "ALL")
    print(base)

    # Quartile split by upper wick (big upper wick = rejected at highs, closed weak)
    print("\n=== By UPPER WICK size (D1 high far above D1 close) ===")
    q = m["upper_wick_pct"].quantile([0.25, 0.5, 0.75]).values
    for label, mask in [
        (f"Q1 upper_wick<{q[0]:.1f}%", m["upper_wick_pct"] < q[0]),
        (f"Q2 {q[0]:.1f}-{q[1]:.1f}%", (m["upper_wick_pct"] >= q[0]) & (m["upper_wick_pct"] < q[1])),
        (f"Q3 {q[1]:.1f}-{q[2]:.1f}%", (m["upper_wick_pct"] >= q[1]) & (m["upper_wick_pct"] < q[2])),
        (f"Q4 upper_wick>={q[2]:.1f}%", m["upper_wick_pct"] >= q[2]),
    ]:
        r = summarize(m[mask], label)
        if r:
            print(f"{r['label']}: n={r['n']} win%={r['win%']:.1f} mean_stretch={r['mean_close_stretch']:.2f}% "
                  f"median={r['median_close_stretch']:.2f}% mean_mae={r['mean_mae']:.2f}% "
                  f"p_mae<-8%={r['p_mae<-8%']:.1f}% tp2_touch%={r['tp2_touch%']:.1f} win_tp3%={r['win_tp3%']:.1f}")

    print("\n=== By LOWER WICK size (D1 recovered off intraday lows into the close) ===")
    q = m["lower_wick_pct"].quantile([0.25, 0.5, 0.75]).values
    for label, mask in [
        (f"Q1 lower_wick<{q[0]:.1f}%", m["lower_wick_pct"] < q[0]),
        (f"Q2 {q[0]:.1f}-{q[1]:.1f}%", (m["lower_wick_pct"] >= q[0]) & (m["lower_wick_pct"] < q[1])),
        (f"Q3 {q[1]:.1f}-{q[2]:.1f}%", (m["lower_wick_pct"] >= q[1]) & (m["lower_wick_pct"] < q[2])),
        (f"Q4 lower_wick>={q[2]:.1f}%", m["lower_wick_pct"] >= q[2]),
    ]:
        r = summarize(m[mask], label)
        if r:
            print(f"{r['label']}: n={r['n']} win%={r['win%']:.1f} mean_stretch={r['mean_close_stretch']:.2f}% "
                  f"median={r['median_close_stretch']:.2f}% mean_mae={r['mean_mae']:.2f}% "
                  f"p_mae<-8%={r['p_mae<-8%']:.1f}% tp2_touch%={r['tp2_touch%']:.1f} win_tp3%={r['win_tp3%']:.1f}")

    print("\n=== By CLOSE POSITION IN DAY RANGE (0=closed at low, 1=closed at high) ===")
    for label, mask in [
        ("closed bottom 25% of range", m["close_position_in_range"] < 0.25),
        ("closed mid (25-75%)", (m["close_position_in_range"] >= 0.25) & (m["close_position_in_range"] < 0.75)),
        ("closed top 25% of range", m["close_position_in_range"] >= 0.75),
    ]:
        r = summarize(m[mask], label)
        if r:
            print(f"{r['label']}: n={r['n']} win%={r['win%']:.1f} mean_stretch={r['mean_close_stretch']:.2f}% "
                  f"median={r['median_close_stretch']:.2f}% mean_mae={r['mean_mae']:.2f}% "
                  f"p_mae<-8%={r['p_mae<-8%']:.1f}% tp2_touch%={r['tp2_touch%']:.1f} win_tp3%={r['win_tp3%']:.1f}")

    # Combined extreme: big upper wick (rejected highs) AND closed in bottom
    # of range -- classic "spike then faded hard into the close" candle
    print("\n=== Extreme combo: big upper wick (>=P75) AND closed bottom half of range ===")
    q75_upper = m["upper_wick_pct"].quantile(0.75)
    combo_mask = (m["upper_wick_pct"] >= q75_upper) & (m["close_position_in_range"] < 0.5)
    r = summarize(m[combo_mask], "spike+fade combo")
    if r:
        print(f"n={r['n']} win%={r['win%']:.1f} mean_stretch={r['mean_close_stretch']:.2f}% "
              f"median={r['median_close_stretch']:.2f}% mean_mae={r['mean_mae']:.2f}% "
              f"p_mae<-8%={r['p_mae<-8%']:.1f}% tp2_touch%={r['tp2_touch%']:.1f} win_tp3%={r['win_tp3%']:.1f}")
    rest = summarize(m[~combo_mask], "rest")
    if rest:
        print(f"rest: n={rest['n']} win%={rest['win%']:.1f} mean_stretch={rest['mean_close_stretch']:.2f}% "
              f"median={rest['median_close_stretch']:.2f}% mean_mae={rest['mean_mae']:.2f}% "
              f"p_mae<-8%={rest['p_mae<-8%']:.1f}% tp2_touch%={rest['tp2_touch%']:.1f} win_tp3%={rest['win_tp3%']:.1f}")

    # Same splits but controlling for tier (does the wick effect survive
    # within TIER1-equivalent conviction, or is it just confounded by tier?)
    print("\n=== Controlling for tier: TIER1-eligible only (ret_2d>=4, pctb>=0.7, gap>=0) ===")
    tier1_mask = (m["ret_2d_cum"] >= 4) & (m["pct_b_prior"] >= 0.7) & (m["gap_pct"] >= 0)
    t1 = m[tier1_mask]
    print(f"n={len(t1)}")
    q = t1["upper_wick_pct"].quantile([0.5]).values
    for label, mask in [
        (f"upper_wick < median ({q[0]:.1f}%)", t1["upper_wick_pct"] < q[0]),
        (f"upper_wick >= median", t1["upper_wick_pct"] >= q[0]),
    ]:
        r = summarize(t1[mask], label)
        if r:
            print(f"{r['label']}: n={r['n']} win%={r['win%']:.1f} mean_stretch={r['mean_close_stretch']:.2f}% "
                  f"mean_mae={r['mean_mae']:.2f}% tp2_touch%={r['tp2_touch%']:.1f}")


if __name__ == "__main__":
    main()
