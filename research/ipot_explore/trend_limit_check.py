"""Robustness of the finalist from entry_scenarios2: TREND_ST + L2 (limit 2% under the close) with exits X2 (SL10/20d), X3 (SL12/20d), X4 (TP20/SL10/20d).
By year, cost 1.0%, ticker concentration, discount depth (L1/L2/L3/L4), and the same trades vs buying at the next open (M0). Run: PYTHONUTF8=1 python -W ignore -m research.ipot_explore.trend_limit_check"""
import numpy as np, pandas as pd
from research.ipot_explore.entry_scenarios import load, regimes, simulate, clus_t, COST
from research.ipot_explore.entry_scenarios2 import cooldown

px = load(); px["ticker"] = px["ticker"].astype("category")
px = px.merge(regimes(), on="date", how="left"); px["regime_phase"] = px["regime_phase"].fillna("NA")
px = px.sort_values(["ticker", "date"]).reset_index(drop=True)
g = px.groupby("ticker", observed=True); c = px["close"]
elig = (px.close >= 50) & (px.val20 >= 0.1e9) & (px.nobs >= 130) & (px.date >= "2023-01-01")
st = (c > px.ma20) & (px.ma20 > px.ma50) & (px.ma20 > g["ma20"].shift(5)) & (px.rsi >= 50) & (px.rsi <= 75) & (c <= 1.10 * px.ma20)
sig = st & ~st.groupby(px["ticker"], observed=True).shift(1, fill_value=False) & elig
rows = cooldown(sig, px)
tk = px["ticker"].cat.codes.to_numpy()[rows]; dts = px["date"].to_numpy()[rows]; yr = pd.DatetimeIndex(dts).year.to_numpy()
liq = px["val20"].to_numpy()[rows]; price = px["close"].to_numpy()[rows]
print(f"TREND_ST signals {len(rows)}, tickers {len(set(tk))}")
EX = {"X2 SL10/20d": (9.99, 0.10, 20), "X3 SL12/20d": (9.99, 0.12, 20), "X4 TP20/SL10/20d": (0.20, 0.10, 20)}
print("\nA. by year (net 0.4%): limit 2% vs market buy, same exit")
for en, (tp, sl, H) in EX.items():
    for mname, d in (("L2", 0.02), ("M0", None)):
        f_, r_ = simulate(px, rows, d, tp, sl, H, hmax=20); net = r_ - COST
        line = [f"{y}: {100 * net[f_ & (yr == y)].mean():+.2f}% (n{int((f_ & (yr == y)).sum())}, t {clus_t(net[f_ & (yr == y)], tk[f_ & (yr == y)]):+.1f})" for y in (2023, 2024, 2025, 2026)]
        print(f"   {en:<18} {mname}: " + " | ".join(line) + f" | fill {100 * f_.mean():.0f}%")
print("\nB. cost sensitivity and concentration (L2, X3 SL12/20d):")
f_, r_ = simulate(px, rows, 0.02, 9.99, 0.12, 20, hmax=20)
for cst in (0.004, 0.008, 0.012):
    net = r_ - cst
    print(f"   cost {100 * cst:.1f}%: mean {100 * net[f_].mean():+.2f}% win {100 * (net[f_] > 0).mean():.0f}% median {100 * np.median(net[f_]):+.2f}% t {clus_t(net[f_], tk[f_]):+.1f}")
net = r_ - COST
df = pd.DataFrame({"tk": tk[f_], "net": net[f_]})
by = df.groupby("tk")["net"].agg(["sum", "count"]).sort_values("sum", ascending=False)
tot = by["sum"].sum()
print(f"   trades {len(df)}, tickers {len(by)}; top 10 tickers contribute {100 * by['sum'].head(10).sum() / tot:.0f}% of total P&L; without the top 10 tickers mean = {100 * (tot - by['sum'].head(10).sum()) / (len(df) - by['count'].head(10).sum()):+.2f}%")
q = np.percentile(df.net, [5, 25, 50, 75, 95]); print("   net return percentiles 5/25/50/75/95: " + " ".join(f"{100 * x:+.1f}%" for x in q))
print("\nC. discount depth (X3), validation split only (signal date > 2025-06-30) | discovery:")
disc = dts <= np.datetime64("2025-06-30")
for d in (0.01, 0.02, 0.03, 0.04, 0.05):
    f2, r2 = simulate(px, rows, d, 9.99, 0.12, 20, hmax=20); n2 = r2 - COST
    print(f"   limit {100 * d:.0f}%: fill {100 * f2.mean():.0f}% | disc {100 * n2[f2 & disc].mean():+.2f}% (n{int((f2 & disc).sum())}) | val {100 * n2[f2 & ~disc].mean():+.2f}% (n{int((f2 & ~disc).sum())})")
print("\nD. liquidity and price terciles (L2, X3):")
for lab, arr in (("liquidity", liq), ("price", price)):
    cuts = np.quantile(arr, [1 / 3, 2 / 3]); grp = np.digitize(arr, cuts)
    print("   " + lab + ": " + " | ".join(f"t{k} {100 * net[f_ & (grp == k)].mean():+.2f}% (n{int((f_ & (grp == k)).sum())})" for k in range(3)))
