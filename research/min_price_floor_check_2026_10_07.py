"""
2026-10-07: BEI (IDX) officially removed the Rp50 price floor
(Kep-00136/BEI/09-2026, 21-09-2026, effective 28-09-2026 -- see
AR Rules below) -- user reported genuinely liquid stocks now trading
below the project's old MIN_STOCK_PRICE=55 threshold (engine/legacy_core.py)
and asked for empirical verification + a new threshold recommendation.

New BEI auto-rejection structure (phase 1, 28-09-2026..31-12-2026):
  Rp1-10:    ARA/ARB = Rp1 fixed increment (not a percentage)
  >Rp10-200: ARA=35%, ARB=15% (asymmetric this phase)
  >Rp200-5000: ARA=25% (unchanged from before)
  >Rp5000:   ARA=20% (unchanged)
  From 2027: ARB becomes symmetric with ARA per tier (percentages unchanged)

This script: (1) confirms the old `current_price<=51` branch in
evaluate_eligibility_from_hist was wrongly excluding genuinely liquid
stocks now trading sub-55, (2) confirms the SEPARATE frozen-stock detector
(10d price range <2%) works correctly independent of price level, so
removing the hard floor doesn't let dead stocks back in, (3) quantifies
how much liquidity is "rescued" at a few candidate thresholds to justify
the final choice of 20.
"""
import pandas as pd


def main():
    df = pd.read_csv("research/ohlcv_backtest_raw.csv")
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values(["ticker", "date"])
    g = df.groupby("ticker")
    df["value_traded"] = df["close"] * df["volume"]
    df["value_20d_avg"] = g["value_traded"].transform(lambda s: s.rolling(20).mean())
    df["low_10d"] = g["low"].transform(lambda s: s.rolling(10).min())
    df["high_10d"] = g["high"].transform(lambda s: s.rolling(10).max())
    df["range10_pct"] = (df["high_10d"] - df["low_10d"]) / df["low_10d"] * 100

    latest_date = df["date"].max()
    latest = df[df["date"] == latest_date].copy()
    print(f"Latest date: {latest_date.date()}")

    under55 = latest[(latest["close"] < 55) & (latest["close"] > 0)].sort_values("value_20d_avg", ascending=False)
    print(f"\nTickers priced <55: {len(under55)}")
    print(under55[["ticker", "close", "value_20d_avg", "range10_pct", "volume"]].head(30).to_string(index=False))

    frozen = latest[latest["range10_pct"] < 2.0]
    print(f"\nGenuinely frozen (10d range<2%, ANY price) as of {latest_date.date()}: {len(frozen)}")
    print(frozen[["ticker", "close", "value_20d_avg", "range10_pct", "volume"]].to_string(index=False))

    print("\n--- Rescue-set size at candidate thresholds (price>=X, <55, NOT frozen) ---")
    for x in [15, 20, 25, 30]:
        rescued = latest[(latest["close"] >= x) & (latest["close"] < 55) & (latest["range10_pct"] >= 2.0)]
        print(f"  threshold={x}: n={len(rescued)}, combined 20d-avg value traded=Rp{rescued['value_20d_avg'].sum():,.0f}/day")


if __name__ == "__main__":
    main()
