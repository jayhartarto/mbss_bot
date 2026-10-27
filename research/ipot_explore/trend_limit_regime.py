"""Year x IHSG-regime table for the finalist (TREND_ST, limit 2%/3%, exit SL12/20d), and a causal regime gate. Run: PYTHONUTF8=1 python -W ignore -m research.ipot_explore.trend_limit_regime"""
import numpy as np, pandas as pd
from research.ipot_explore.entry_scenarios import load, regimes, simulate, clus_t, COST
from research.ipot_explore.entry_scenarios2 import cooldown

px = load(); px["ticker"] = px["ticker"].astype("category")
px = px.merge(regimes(), on="date", how="left"); px["regime_phase"] = px["regime_phase"].fillna("NA")
px = px.sort_values(["ticker", "date"]).reset_index(drop=True)
g = px.groupby("ticker", observed=True); c = px["close"]
elig = (px.close >= 50) & (px.val20 >= 0.1e9) & (px.nobs >= 130) & (px.date >= "2023-01-01")
st = (c > px.ma20) & (px.ma20 > px.ma50) & (px.ma20 > g["ma20"].shift(5)) & (px.rsi >= 50) & (px.rsi <= 75) & (c <= 1.10 * px.ma20)
rows = cooldown(st & ~st.groupby(px["ticker"], observed=True).shift(1, fill_value=False) & elig, px)
tk = px["ticker"].cat.codes.to_numpy()[rows]; yr = pd.DatetimeIndex(px["date"].to_numpy()[rows]).year.to_numpy(); rg = px["regime_phase"].to_numpy()[rows]
REGS = ["BEARISH_GRIND", "BOUNCE_OFF_LOW", "CRASH_FAST", "NEUTRAL_BULLISH", "PULLBACK_IN_BOUNCE"]
print("signals per year x regime:")
print(pd.crosstab(yr, rg).reindex(columns=REGS).to_string())
for d in (0.02, 0.03):
    f_, r_ = simulate(px, rows, d, 9.99, 0.12, 20, hmax=20); net = r_ - COST
    print(f"\nlimit {100 * d:.0f}%, SL12/20d: net mean % (n) by year x regime")
    out = {}
    for y in (2023, 2024, 2025, 2026):
        out[y] = [f"{100 * net[f_ & (yr == y) & (rg == r)].mean():+.1f} ({int((f_ & (yr == y) & (rg == r)).sum())})" if (f_ & (yr == y) & (rg == r)).sum() >= 15 else "-" for r in REGS]
    print(pd.DataFrame(out, index=REGS).T.to_string())
    for lab, gate in (("gate BOUNCE_OFF_LOW + NEUTRAL_BULLISH", np.isin(rg, ["BOUNCE_OFF_LOW", "NEUTRAL_BULLISH"])), ("gate BOUNCE_OFF_LOW only", rg == "BOUNCE_OFF_LOW"), ("avoid BEARISH_GRIND/CRASH_FAST", ~np.isin(rg, ["BEARISH_GRIND", "CRASH_FAST"]))):
        line = " | ".join(f"{y}: {100 * net[f_ & gate & (yr == y)].mean():+.2f}% (n{int((f_ & gate & (yr == y)).sum())})" for y in (2023, 2024, 2025, 2026))
        print(f"   {lab}: {line} | all years {100 * net[f_ & gate].mean():+.2f}% t {clus_t(net[f_ & gate], tk[f_ & gate]):+.1f}")
