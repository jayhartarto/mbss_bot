"""
Run this ON THE VPS (needs cache/broksum_daily_history.pkl + mbss_ohlcv.db there).
Diffs consecutive 10-day-rolling-window whitelist-broker snapshots to approximate
daily net flow, joins with price data, tests forward 5d return vs net_pct_diff
and dist_ema21. Paste the printed output back to Claude.
"""
import pickle
import sqlite3
import pandas as pd

SMART_MONEY_BROKER_WHITELIST = {"ZP", "AK", "BK", "YU", "AI", "SQ", "BB", "KZ", "RX", "RF", "DX", "HP", "KI"}

with open("cache/broksum_daily_history.pkl", "rb") as f:
    envelope = pickle.load(f)
data = envelope.get("data", envelope) if isinstance(envelope, dict) and "data" in envelope else envelope

rows = []
for ticker, entries in data.items():
    entries = sorted(entries, key=lambda e: e["date"])
    for i in range(1, len(entries)):
        prev, cur = entries[i - 1], entries[i]
        prev_map = {b["code"]: b for b in prev["brokers"]}
        diffs = []
        for b in cur["brokers"]:
            code = b["code"]
            if code not in SMART_MONEY_BROKER_WHITELIST:
                continue
            pb = prev_map.get(code, {"buy_value": 0, "sell_value": 0})
            diff_buy = (b.get("buy_value") or 0) - (pb.get("buy_value") or 0)
            diff_sell = (b.get("sell_value") or 0) - (pb.get("sell_value") or 0)
            diffs.append((code, diff_buy, diff_sell))
        if not diffs:
            continue
        net_value = sum(db - ds for _, db, ds in diffs)
        gross_value = sum(abs(db) + abs(ds) for _, db, ds in diffs)
        if gross_value <= 0:
            continue
        net_pct = net_value / gross_value * 100
        num_brokers_active = sum(1 for _, db, ds in diffs if db != 0 or ds != 0)
        rows.append({
            "ticker": ticker, "date": cur["date"],
            "net_pct_diff": net_pct, "num_brokers_active": num_brokers_active,
            "net_value_diff": net_value, "gross_value_diff": gross_value,
        })

df = pd.DataFrame(rows)
print(f"=== DIFF SUMMARY ===")
print(f"Total diff rows: {len(df)}, unique tickers: {df['ticker'].nunique() if len(df) else 0}, "
      f"date range: {df['date'].min() if len(df) else 'n/a'} to {df['date'].max() if len(df) else 'n/a'}")
if len(df) == 0:
    raise SystemExit("No diff rows produced — check pickle structure matches expected shape.")
print(df["net_pct_diff"].describe())
print("\nnum_brokers_active distribution:")
print(df["num_brokers_active"].value_counts().sort_index())

con = sqlite3.connect("mbss_ohlcv.db")
ohlcv = pd.read_sql("SELECT ticker, date, close, high, low FROM ohlcv_daily", con)
ohlcv["date"] = pd.to_datetime(ohlcv["date"])
df["date"] = pd.to_datetime(df["date"])

feats = []
for t, g in ohlcv.groupby("ticker"):
    g = g.sort_values("date").reset_index(drop=True)
    g["ema21"] = g["close"].ewm(span=21, adjust=False).mean()
    g["dist_ema21"] = (g["close"] - g["ema21"]) / g["ema21"] * 100
    g["fwd_ret_5d"] = g["close"].shift(-5) / g["close"] - 1
    feats.append(g[["ticker", "date", "close", "dist_ema21", "fwd_ret_5d"]])
price_feat = pd.concat(feats, ignore_index=True)

merged = df.merge(price_feat, on=["ticker", "date"], how="inner")
merged_valid = merged.dropna(subset=["fwd_ret_5d"])
print(f"\n=== MERGED WITH PRICE DATA ===")
print(f"Rows with price+fwd_ret_5d available: {len(merged_valid)} (out of {len(merged)} price-matched)")

if len(merged_valid) >= 10:
    strong = merged_valid[(merged_valid["net_pct_diff"] >= 15) & (merged_valid["num_brokers_active"] >= 2)]
    rest = merged_valid[~((merged_valid["net_pct_diff"] >= 15) & (merged_valid["num_brokers_active"] >= 2))]
    print(f"\n=== STRONG accumulation-diff (net_pct_diff>=15, brokers_active>=2) ===")
    print(f"n={len(strong)}, mean fwd_ret_5d={strong['fwd_ret_5d'].mean()*100:.2f}%, "
          f"win_rate={(strong['fwd_ret_5d']>0).mean()*100:.1f}%" if len(strong) else "n=0")
    print(f"\n=== REST ===")
    print(f"n={len(rest)}, mean fwd_ret_5d={rest['fwd_ret_5d'].mean()*100:.2f}%, "
          f"win_rate={(rest['fwd_ret_5d']>0).mean()*100:.1f}%" if len(rest) else "n=0")

    if len(strong) >= 10:
        strong = strong.copy()
        strong["dist_ema21_tercile"] = pd.qcut(strong["dist_ema21"], 3, labels=["LO", "MID", "HI"], duplicates="drop")
        print(f"\n=== dist_ema21 tercile WITHIN strong-accumulation group ===")
        print(strong.groupby("dist_ema21_tercile", observed=True)["fwd_ret_5d"].agg(["count", "mean"]))
    else:
        print(f"\n(strong group n={len(strong)} too small for tercile split)")
else:
    print("Too few merged rows to analyze further — print merged_valid.head(30) manually to inspect.")
    print(merged_valid.head(30))
