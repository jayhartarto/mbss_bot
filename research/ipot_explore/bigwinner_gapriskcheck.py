"""How exposed are the picks to stops that cannot be executed (gap-through, limit-down locks)? Run: PYTHONUTF8=1 python -W ignore -m research.ipot_explore.bigwinner_gapriskcheck"""
import numpy as np, pandas as pd
from research.ipot_explore.bigwinner_risk import get_picks, manage, dedup
from research.ipot_explore.entry_scenarios import COST
px, D, T = get_picks()
S = T[T["p"] >= T.groupby("q")["p"].transform(lambda s: s.quantile(0.99))]
SD = dedup(S)
print(f"picks {len(SD)}; worst one-day move inside the 20-day window: share <= -10%: {100 * (SD.fmin <= -0.10).mean():.0f}% | <= -15%: {100 * (SD.fmin <= -0.15).mean():.0f}% | <= -20%: {100 * (SD.fmin <= -0.20).mean():.0f}%  (all test rows: <= -15% {100 * (T.fmin <= -0.15).mean():.0f}%)")
for v, st in (("S6", 0.06), ("S4", 0.04), ("BASE", 0.12)):
    g = np.array([manage(px, int(r), v) for r in SD["pxrow"].to_numpy()])
    stopped = g <= -st * 0.999 - 1e-9
    worse = g < -st - 0.005
    print(f"   {v}: trades stopped {int(stopped.sum())} ({100 * stopped.mean():.0f}%); of them beyond the stop distance by more than 0.5pp (gap-through): {int(worse.sum())} ({100 * worse.sum() / max(stopped.sum(), 1):.0f}% of stops), average loss of those {100 * g[worse].mean():+.1f}%, worst {100 * g.min():+.1f}%")
    srt = np.sort(g)[:5]
    print(f"        5 worst trades: " + " ".join(f"{100 * x:+.1f}%" for x in srt))
