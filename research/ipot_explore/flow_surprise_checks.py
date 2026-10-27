"""research/ipot_explore/flow_surprise_checks.py — robustness of the NEUTRAL_BULLISH flow-surprise survivors: (a) dates per confirmation period, (b) effect INSIDE ret5_past terciles x ATR terciles
(controls short-term reversal and volatility), (c) overlap between survivors (pairwise correlation of signal flags), (d) net-of-cost view, (e) 3 chronological thirds of the confirmation period."""
import os, warnings, numpy as np, pandas as pd
warnings.filterwarnings('ignore')
rng = np.random.default_rng(9)
X = pd.read_pickle(os.environ['PANEL']).replace([np.inf, -np.inf], np.nan)
for h in ('ret1', 'ret5'): X['x' + h[3:]] = X[h] - X.groupby(['date', 'atr_cell'])[h].transform('mean')
print('confirmation-period distinct dates per regime:')
SPLIT = {}
for rg, R in X.groupby('regime_phase'):
    dates = np.sort(R.date.unique()); cut = dates[int(len(dates) * 0.6)]; emb = dates[min(len(dates) - 1, int(len(dates) * 0.6) + 10)]
    SPLIT[rg] = (cut, emb); print(' ', rg, 'total', len(dates), 'confirm', int((dates > emb).sum()))

def bt(sub, col, blk=5, n=300):
    if len(sub) < 60: return np.nan, np.nan
    ds = np.sort(sub.date.unique()); bid = {d: i // blk for i, d in enumerate(ds)}
    g = sub.assign(b=sub.date.map(bid)).groupby('b')[col].agg(['sum', 'count']); sv, cv = g['sum'].values, g['count'].values
    if len(g) < 8: return sub[col].mean(), np.nan
    m = sv.sum() / cv.sum(); bs = [sv[k].sum() / cv[k].sum() for k in (rng.integers(0, len(g), len(g)) for _ in range(n))]
    return m, m / (np.std(bs) + 1e-9)

rg = 'NEUTRAL_BULLISH'; cut, emb = SPLIT[rg]; R = X[X.regime_phase == rg]; D, T = R[R.date <= cut], R[R.date > emb].copy()
SIG = {'new_buy_hi': ('new_buy', 'hi'), 'zmax_local_hi': ('zmax_local', 'hi'), 'nz2_hi': ('nz2', 'hi'), 'zmax_inst_lo': ('zmax_inst', 'lo'), 'zr_retail_hi(neg)': ('zr_retail_app', 'hi'),
       'zr_retail_lo': ('zr_retail_app', 'lo'), 'cb_gap_retail_lo': ('cb_gap_retail', 'lo'), 'cb_gap_inst_lo': ('cb_gap_inst', 'lo')}
for k, (f, tail) in SIG.items():
    q = D[f].quantile(0.9 if tail == 'hi' else 0.1); T[k] = (T[f] >= q) if tail == 'hi' else (T[f] <= q)
    if f == 'nz2': T[k] = T[f] >= 3
T['rt'] = pd.qcut(T.ret5_past, 3, labels=['past_fall', 'past_mid', 'past_rise']); T['at'] = pd.qcut(T.atr60, 3, labels=['lowvol', 'midvol', 'highvol'])
print('\n(b) excess ret5 (t) of each signal INSIDE ret5_past terciles / ATR60 terciles (NEUTRAL, confirmation); n in brackets')
for k in SIG:
    out = []
    for col in ('rt', 'at'):
        for lv in T[col].cat.categories:
            s = T[(T[k]) & (T[col] == lv)]; m, t = bt(s, 'x5'); out.append(f"{lv}:{m:+.2f}({t:+.1f})[{len(s)}]")
    print(f"{k:20s}", ' '.join(out))
print('\n(c) correlation between survivor flags (confirmation):'); print(T[list(SIG)].astype(float).corr().round(2).to_string())
print('\n(d) per-signal net view (confirmation): raw5, ex5, ex1, net5 = raw5-0.4')
for k in SIG:
    s = T[T[k]]; m5, t5 = bt(s, 'x5'); m1, t1 = bt(s, 'x1'); print(f"{k:20s} n={len(s):6d} raw5={s.ret5.mean():+.2f} ex5={m5:+.2f}(t{t5:+.1f}) ex1={m1:+.2f}(t{t1:+.1f}) net5={s.ret5.mean()-0.4:+.2f}  base_raw5={T.ret5.mean():+.2f}")
print('\n(e) ex5 by chronological third of the confirmation period:')
ds = np.sort(T.date.unique()); th = np.array_split(ds, 3)
for k in SIG:
    print(f"{k:20s}", ' | '.join(f"{bt(T[(T[k]) & T.date.isin(x)], 'x5')[0]:+.2f}" for x in th))
