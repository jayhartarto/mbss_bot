"""research/ipot_explore/flow_surprise_eval.py — steps 1+2 (flow surprise, flow-vs-price absorption), each IHSG regime its own segment.
Signal = feature in the top (or bottom) decile of the regime's DISCOVERY period (first 60% of its dates); confirm on the later 40% (10-day embargo) with the SAME thresholds.
Outcome = ret (open t+1 -> close t+1 / t+5) minus the mean of the same date x ATR-tercile cell (excess). t from a bootstrap over 5-date BLOCKS (market-wide days not independent).
Pass rule fixed in advance: discovery |t|>=2 AND confirmation t>=2 with the same sign AND BH-FDR (over all confirmation tests) < 0.2.
Step 2 absorption = 2x2 groups of inst5 high (top decile) x ret5_past (<=0 vs >0).  Step 3 = interaction with type descriptors for survivors only.
Run: PYTHONUTF8=1 PANEL=<flow_panel.pkl> python research/ipot_explore/flow_surprise_eval.py"""
import os, warnings, numpy as np, pandas as pd
from math import erf, sqrt
warnings.filterwarnings('ignore')
rng = np.random.default_rng(5)
X = pd.read_pickle(os.environ['PANEL']).replace([np.inf, -np.inf], np.nan)
TYPE = ['atr60', 'liq60', 'gorengan120', 'foreign_share60', 'retail_share60', 'top3conc60']
NON = {'ticker', 'date', 'regime_phase', 'ret1', 'ret5', 'hi5', 'gap0', 'atr_cell', 'atr_0', 'liq', 'close'} | set(TYPE)
FEATS = [c for c in X.columns if c not in NON]
for h in ('ret1', 'ret5'): X['x' + h[3:]] = X[h] - X.groupby(['date', 'atr_cell'])[h].transform('mean')

def blocks_t(sub, col, blk=5, n=200):
    if len(sub) < 40: return np.nan, np.nan
    ds = np.sort(sub.date.unique()); bid = {d: i // blk for i, d in enumerate(ds)}
    g = sub.assign(b=sub.date.map(bid)).groupby('b')[col].agg(['sum', 'count']); sv, cv = g['sum'].values, g['count'].values
    m = sv.sum() / cv.sum(); bs = [sv[k].sum() / cv[k].sum() for k in (rng.integers(0, len(g), len(g)) for _ in range(n))]
    return m, m / (np.std(bs) + 1e-9)

def pval(t): return 2 * (1 - 0.5 * (1 + erf(abs(t) / sqrt(2)))) if pd.notna(t) else np.nan

rows = []
for rg, R in X.groupby('regime_phase'):
    if rg == 'PULLBACK_IN_BOUNCE': continue
    dates = np.sort(R.date.unique()); cut = dates[int(len(dates) * 0.6)]; emb = dates[min(len(dates) - 1, int(len(dates) * 0.6) + 10)]
    D = R[R.date <= cut]; T = R[R.date > emb]
    for f in FEATS:
        v = D[f].dropna()
        if len(v) < 3000 or v.nunique() < 5: continue
        for tail, thr, op in (('hi', v.quantile(0.9), np.greater_equal), ('lo', v.quantile(0.1), np.less_equal)):
            if tail == 'lo' and v.quantile(0.1) == v.quantile(0.5): continue
            sd = D[op(D[f], thr)]; st = T[op(T[f], thr)]
            if len(sd) < 300 or len(st) < 150: continue
            md, td = blocks_t(sd, 'x5'); mt, tt = blocks_t(st, 'x5'); m1, t1 = blocks_t(st, 'x1')
            rows.append(dict(regime=rg, feat=f, tail=tail, thr=round(float(thr), 3), n_d=len(sd), ex5_d=md, t_d=td, n_t=len(st), ex5_t=mt, t_t=tt, ex1_t=m1, t1_t=t1,
                             raw5_t=st.ret5.mean()))
R_ = pd.DataFrame(rows); R_['p'] = R_.t_t.apply(pval)
R_ = R_.sort_values('p'); R_['bh'] = (R_.p * len(R_) / (np.arange(len(R_)) + 1)).clip(upper=1)
R_['pass'] = (R_.t_d.abs() >= 2) & (R_.t_t.abs() >= 2) & (np.sign(R_.ex5_d) == np.sign(R_.ex5_t)) & (R_.bh < 0.2)
pd.set_option('display.width', 250); pd.set_option('display.max_rows', 300)
print('tests', len(R_), '| |t_t|>=2:', int((R_.t_t.abs() >= 2).sum()), '(chance ~', round(0.05 * len(R_), 1), ') | discovery&confirm same sign |t|>=2:', int(((R_.t_d.abs() >= 2) & (R_.t_t.abs() >= 2) & (np.sign(R_.ex5_d) == np.sign(R_.ex5_t))).sum()), '| PASS(BH<.2):', int(R_['pass'].sum()))
print('\nTop 25 by confirmation p:'); print(R_.head(25).round(3).drop(columns=['p']).to_string(index=False))
print('\nPASS:'); print(R_[R_['pass']].round(3).to_string(index=False))
R_.to_csv(os.path.join(os.path.dirname(os.environ['PANEL']), 'flow_surprise_eval.csv'), index=False)

print('\n=== Step 2: absorption 2x2 (inst5 top decile of regime discovery) x ret5_past; excess ret5 on CONFIRMATION period')
for rg, R in X.groupby('regime_phase'):
    if rg == 'PULLBACK_IN_BOUNCE': continue
    dates = np.sort(R.date.unique()); cut = dates[int(len(dates) * 0.6)]; emb = dates[min(len(dates) - 1, int(len(dates) * 0.6) + 10)]
    thr = R[R.date <= cut].inst5.quantile(0.9); T = R[(R.date > emb) & (R.inst5 >= thr)]
    out = []
    for nm, m in (('flow hi & price <=0 (absorbed)', T.ret5_past <= 0), ('flow hi & price up', T.ret5_past > 0)):
        s = T[m]; mm, tt = blocks_t(s, 'x5'); m1, t1 = blocks_t(s, 'x1'); out.append(f"{nm}: n={len(s)} ex5={mm:+.2f} (t {tt:+.1f}) ex1={m1:+.2f}")
    print(rg, '| thr %.3f | ' % thr + ' || '.join(out))
print('\n=== Step 2b: cost-basis gap (cb_gap_inst: close vs inst buyers 5d VWAP) buckets, confirmation, excess ret5')
for rg, R in X.groupby('regime_phase'):
    if rg == 'PULLBACK_IN_BOUNCE': continue
    dates = np.sort(R.date.unique()); cut = dates[int(len(dates) * 0.6)]; emb = dates[min(len(dates) - 1, int(len(dates) * 0.6) + 10)]
    T = R[R.date > emb].dropna(subset=['cb_gap_inst']); T = T[T.inst5 > 0.01]   # only when institutions net bought
    out = []
    for nm, m in (('underwater<-3%', T.cb_gap_inst < -3), ('near cost -3..+3', T.cb_gap_inst.between(-3, 3)), ('in profit>+3%', T.cb_gap_inst > 3)):
        s = T[m]; mm, tt = blocks_t(s, 'x5'); out.append(f"{nm}: n={len(s)} ex5={mm:+.2f} (t {tt:+.1f})")
    print(rg, '| ' + ' || '.join(out))
