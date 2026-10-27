"""Late trailing stop for the right-tail picks: initial stop s; the trail only activates once the high reached +T% above entry; then exit when price falls d% below the highest high (checked before the day's high updates the trail, gap exits at the open); day-20 time exit.
Run: PYTHONUTF8=1 python -W ignore -m research.ipot_explore.bigwinner_latetrail"""
import numpy as np, pandas as pd
from research.ipot_explore.bigwinner_risk import get_picks, dedup
from research.ipot_explore.entry_scenarios import COST
from research.ipot_explore.bigwinner_tp import stats, SPLIT

px, D, T = get_picks()
S = T[T["p"] >= T.groupby("q")["p"].transform(lambda s: s.quantile(0.99))]
SD = dedup(S); rows = SD["pxrow"].to_numpy()
O, Hh, L, C = (px[k].to_numpy(float) for k in ("open", "high", "low", "close"))
def run(s, Tact, d, H=20):
    out = np.zeros(len(rows))
    for j, r in enumerate(rows):
        E = O[r + 1]; stop = E * (1 - s); hh = E; res = None
        for k in range(1, H + 1):
            i = r + k
            if O[i] <= stop: res = O[i] / E - 1; break
            if L[i] <= stop: res = stop / E - 1; break
            hh = max(hh, Hh[i])
            if hh >= E * (1 + Tact): stop = max(stop, hh * (1 - d))
        out[j] = res if res is not None else C[r + H] / E - 1
    return out - COST
print(f"picks {len(SD)}")
rows_out = []
base = run(0.06, 9.99, 0.99)
for s in (0.06, 0.08):
    for Tact in (0.10, 0.15, 0.20, 0.30, 0.50):
        for d in (0.08, 0.12, 0.15, 0.20, 0.25):
            net = run(s, Tact, d); st = stats(net, SD, s)
            rows_out.append(dict(label=f"s{int(100*s)} activate+{int(100*Tact)} trail{int(100*d)}", **st))
R = pd.DataFrame(rows_out)
f = lambda d: d.assign(**{c: (d[c]*100).round(2) for c in ("mean","disc","val","win","avg_loss","ex5")}).round({"pf":2,"R":2})[["label","mean","disc","val","win","avg_loss","pf","ex5","qp","R"]]
nb = stats(base, SD, 0.06)
print(f"\nREFERENCE no TP, stop 6%: mean {100*nb['mean']:.2f}% disc {100*nb['disc']:.2f}% val {100*nb['val']:.2f}% win {100*nb['win']:.0f}% PF {nb['pf']:.2f} ex-best5 {100*nb['ex5']:.2f}%")
print("\nlate trailing, stop 6% (sorted by discovery mean):"); print(f(R[R.label.str.startswith('s6')].sort_values('disc', ascending=False).head(12)).to_string(index=False))
print("\nlate trailing, stop 8%:"); print(f(R[R.label.str.startswith('s8')].sort_values('disc', ascending=False).head(6)).to_string(index=False))
print("\nhow much of the no-TP mean does the best late trail keep?  disc/val vs reference:", f"{100*R.sort_values('disc',ascending=False).iloc[0]['disc']:.2f}/{100*R.sort_values('disc',ascending=False).iloc[0]['val']:.2f}", "vs", f"{100*nb['disc']:.2f}/{100*nb['val']:.2f}")
