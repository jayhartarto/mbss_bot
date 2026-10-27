"""EXP-BIGWIN-3: same walk-forward right-tail signal with the executable minimum price Rp30 (not 50), tight-stop variants in R-multiples, and by price band (incl. the old 'gocap' zone Rp50-60) for the finalists.
Run (PYTHONUTF8=1): python -W ignore -m research.ipot_explore.bigwinner_risk2"""
import numpy as np, pandas as pd
from research.ipot_explore.bigwinner_risk import get_picks, manage, summary, dedup
from research.ipot_explore.entry_scenarios import COST

px, D, T = get_picks()
S = T[T["p"] >= T.groupby("q")["p"].transform(lambda s: s.quantile(0.99))]
SD = dedup(S)
base_big = T.BIG20.mean()
print(f"min price Rp30 | test rows {len(T)} (base BIG20 {100 * base_big:.1f}%, base net {100 * T.net.mean():+.2f}%) | picks (top 1%, dedup) {len(SD)} in {SD.ticker_s.nunique()} tickers | BIG20 {100 * SD.BIG20.mean():.1f}% | close<50: {100 * (SD.close < 50).mean():.0f}% of picks (all rows {100 * (T.close < 50).mean():.0f}%)")
stops = {"BASE": 0.12, "S8": 0.08, "S6": 0.06, "S4": 0.04, "COMBO": 0.08}
def table(P, vs, label):
    print(f"\n===== {label} (n={len(P)}) =====")
    print(f"{'variant':<7}{'n':>5}{'mean':>8}{'median':>8}{'win':>6}{'avgLoss':>9}{'avgWin':>8}{'P5':>8}{'<=-10%':>8}{'PF':>6}{'meanR':>7}{'ex-best5':>9}{'qtrs+':>7}")
    for v in vs:
        gross = np.array([manage(px, int(r), v) for r in P["pxrow"].to_numpy()])
        net = gross - COST
        s = summary(net)
        R = net / (stops.get(v, 0.12) + COST)
        pq = pd.Series(net, index=P.index).groupby(P["q"]).mean()
        print(f"{v:<7}{s['n']:>5}{100 * s['mean']:>7.2f}%{100 * s['med']:>7.1f}%{100 * s['win']:>5.0f}%{100 * s['avg_loss']:>8.1f}%{100 * s['avg_win']:>7.1f}%{100 * s['p5']:>7.1f}%{100 * s['big_loss']:>7.0f}%{s['pf']:>6.2f}{np.nanmean(R):>7.2f}{100 * s['ex5']:>8.2f}%{int((pq > 0).sum()):>4}/{len(pq)}")
table(SD, ["BASE", "S8", "S6", "S4", "COMBO"], "ALL PICKS, price >= Rp30")
bands = [("Rp30-49", SD.close < 50), ("Rp50-99", (SD.close >= 50) & (SD.close < 100)), ("Rp100-199", (SD.close >= 100) & (SD.close < 200)), ("Rp200+", SD.close >= 200), ("Rp50-60 (old gocap zone)", (SD.close >= 50) & (SD.close <= 60))]
for lab, m in bands:
    if m.sum() >= 25:
        table(SD[m], ["BASE", "S6", "S4"], f"price band {lab}")
    else:
        print(f"\n===== price band {lab}: n={int(m.sum())} (too few) =====")
print("\nby year, S6 and S4 (all picks):")
for v in ("S6", "S4"):
    gross = pd.Series([manage(px, int(r), v) for r in SD["pxrow"].to_numpy()], index=SD.index) - COST
    print(f"   {v}: " + " | ".join(f"{y}: n{len(g)} {100 * g.mean():+.2f}% (win {100 * (g > 0).mean():.0f}%)" for y, g in gross.groupby(SD.date.dt.year)))
