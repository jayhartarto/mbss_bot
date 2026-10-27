"""research/ipot_explore/dna_tp_grid.py — which filters capture "TP before SL within 5 days" for TP in {3,4,5,6,8,10}% x SL in {3,5}%, per IHSG regime, strict OOS.
Entry open(t+1); path = days t+1..t+5 (same-day TP&SL -> SL first; gap through a level fills at the open; else close t+5). Winner = TP reached before SL.
Per regime, fold (train first 60% dates x ticker half A; test later 40% after 10d embargo x half B; swapped) and combo:
  HGB_T / HGB_TB : shallow boosting on technical / technical+broker, top decile of test by score (ceiling, black box)
  RULE_TB        : depth-3 decision tree on TB features fit on train; leaves with n>=300, win-rate lift>=1.2x and train expectancy > base are the FILTER; applied to test (readable rules)
Metrics: win rate vs base, expectancy gross/net(0.4%), excess vs same-date mean of the same trade, ticker-cluster bootstrap t.
Run: PYTHONUTF8=1 PANEL=<dna_panel2.pkl> PYTHONPATH=. python research/ipot_explore/dna_tp_grid.py"""
import os, sys, warnings, numpy as np, pandas as pd
warnings.filterwarnings('ignore')
sys.path.insert(0, os.getcwd())
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.tree import DecisionTreeClassifier
import research.ipot_explore.accum_episode as A
rng = np.random.default_rng(42)
COST = 0.4; TPS = (3, 4, 5, 6, 8, 10); SLS = (3, 5)
P = pd.read_pickle(os.environ['PANEL']).dropna(subset=['hi5']).copy()
# ---- path simulation for the grid
_, o = A.load(); o = o.sort_values(['ticker', 'date']).reset_index(drop=True)
g = o.groupby('ticker', sort=False)
S = {k: {c: g[c].shift(-k).values for c in ('open', 'high', 'low', 'close')} for k in range(1, 6)}
e = S[1]['open']
sim = pd.DataFrame({'ticker': o.ticker.values, 'date': o.date.values})
for tp_ in TPS:
    for sl_ in SLS:
        res = np.full(len(e), np.nan); fl = np.zeros(len(e)); done = np.zeros(len(e), bool)
        tp = e * (1 + tp_ / 100); st = e * (1 - sl_ / 100)
        for k in range(1, 6):
            op, hi, lo, cl = S[k]['open'], S[k]['high'], S[k]['low'], S[k]['close']
            ok = ~done & ~np.isnan(hi) & ~np.isnan(e)
            gd = ok & (op <= st) & (k > 1); gu = ok & (op >= tp) & (k > 1)
            sh = ok & ~gd & ~gu & (lo <= st); th = ok & ~gd & ~gu & ~sh & (hi >= tp)
            for m, px, w in ((gd, op, 0), (gu, op, 1), (sh, st, 0), (th, tp, 1)):
                res[m] = (px[m] / e[m] - 1) * 100; fl[m] = w; done[m] = True
            if k == 5:
                m = ok & ~done; res[m] = (cl[m] / e[m] - 1) * 100; done[m] = True
        sim[f'r_{tp_}_{sl_}'] = res; sim[f'w_{tp_}_{sl_}'] = fl
P = P.merge(sim, on=['ticker', 'date'], how='left')
P['liq_l'] = np.log10(P.liq.clip(lower=1)); P['close_l'] = np.log10(P.close)
NON = {'ticker', 'date', 'ret5', 'ret1', 'hi5', 'gap0', 'win5', 'tr_tp5_sl5', 'tr_tp5_nosl', 'regime_phase', 'atr_cell', 'liq', 'close', 'half'}
NON |= {c for c in P.columns if c.startswith(('r_', 'w_')) and c[2:3].isdigit()}
FE = [c for c in P.columns if c not in NON]; BR = [c for c in FE if c.startswith('b_')]; TE = [c for c in FE if not c.startswith('b_')]
tk = np.array(sorted(P.ticker.unique())); rng.shuffle(tk); P['half'] = P.ticker.map({t: i % 2 for i, t in enumerate(tk)})
for tp_ in TPS:
    for sl_ in SLS: P[f'x_{tp_}_{sl_}'] = P[f'r_{tp_}_{sl_}'] - P.groupby('date')[f'r_{tp_}_{sl_}'].transform('mean')

def prep(tr, te, cols):
    lo, hi = tr[cols].quantile(0.01), tr[cols].quantile(0.99); med = tr[cols].median()
    f = lambda D: D[cols].clip(lo, hi, axis=1).fillna(med)
    return f(tr), f(te)

def dedup(df, mask):
    s = df[mask].sort_values(['ticker', 'date']); keep = []; pt = None; pdt = None
    for i, t, dd in zip(s.index, s.ticker, pd.to_datetime(s.date)):
        if t != pt or (dd - pdt).days > 7: keep.append(i); pt, pdt = t, dd
    return s.loc[keep]

def leaf_rules(tree, cols, X, y, ret, base_ret):
    t = tree.tree_; leaf = tree.apply(X.values); out = []
    def path(node, conds):
        if t.children_left[node] == -1: return [(node, conds)]
        f, th = cols[t.feature[node]], t.threshold[node]
        return path(t.children_left[node], conds + [(f, '<=', th)]) + path(t.children_right[node], conds + [(f, '>', th)])
    base_w = y.mean()
    for node, conds in path(0, []):
        m = leaf == node
        if m.sum() >= 300 and y[m].mean() >= 1.2 * base_w and ret[m].mean() > base_ret: out.append((node, conds, m.sum(), y[m].mean(), ret[m].mean()))
    return out

rows = []; rule_txt = {}
for rg, R in P.groupby('regime_phase'):
    if rg == 'PULLBACK_IN_BOUNCE': continue
    dates = np.sort(R.date.unique()); cut = dates[int(len(dates) * 0.6)]; emb = dates[min(len(dates) - 1, int(len(dates) * 0.6) + 10)]
    for fold in (0, 1):
        tr = R[(R.date <= cut) & (R.half == fold)]; te = R[(R.date > emb) & (R.half != fold)]
        tr = tr.dropna(subset=[f'r_{TPS[0]}_{SLS[0]}']); te = te.dropna(subset=[f'r_{TPS[0]}_{SLS[0]}'])
        if len(tr) < 2000 or len(te) < 1000: continue
        XT_tr, XT_te = prep(tr, te, TE); XA_tr, XA_te = prep(tr, te, FE)
        for tp_ in TPS:
            for sl_ in SLS:
                y = tr[f'w_{tp_}_{sl_}'].values.astype(int); rt = tr[f'r_{tp_}_{sl_}'].values
                if y.sum() < 100: continue
                picks = {}
                for nm, (a, b) in (('HGB_T', (XT_tr, XT_te)), ('HGB_TB', (XA_tr, XA_te))):
                    sc = HistGradientBoostingClassifier(max_depth=3, max_iter=120, learning_rate=0.05, min_samples_leaf=200, l2_regularization=5.0).fit(a, y).predict_proba(b)[:, 1]
                    picks[nm] = (sc >= np.quantile(sc, 0.9))
                dt = DecisionTreeClassifier(max_depth=3, min_samples_leaf=300, random_state=0).fit(XA_tr, y)
                lr = leaf_rules(dt, FE, XA_tr, y, rt, rt.mean())
                if lr:
                    leaf_te = dt.apply(XA_te.values); keep = np.isin(leaf_te, [x[0] for x in lr]); picks['RULE_TB'] = keep
                    if fold == 0: rule_txt[(rg, tp_, sl_)] = [(' & '.join(f"{f}{o}{th:.3g}" for f, o, th in x[1]), x[2], round(x[3] * 100, 1), round(x[4], 2)) for x in lr]
                for nm, mk in picks.items():
                    if mk.sum() < 40: continue
                    pk = dedup(te, mk)
                    if len(pk) < 30: continue
                    rows.append(dict(regime=rg, fold=fold, tp=tp_, sl=sl_, model=nm, n_test=len(te), base_w=te[f'w_{tp_}_{sl_}'].mean() * 100, base_r=te[f'r_{tp_}_{sl_}'].mean(),
                                     n=len(pk), w=pk[f'w_{tp_}_{sl_}'].mean() * 100, r=pk[f'r_{tp_}_{sl_}'].mean(), x=pk[f'x_{tp_}_{sl_}'].mean(),
                                     _tick=pk.ticker.values, _x=pk[f'x_{tp_}_{sl_}'].values))
D = pd.DataFrame(rows)
def boot(gp):
    tick = np.concatenate(gp._tick.values); x = np.concatenate(gp._x.values)
    s = pd.Series(x).groupby(tick).agg(['sum', 'count']); sv, cv = s['sum'].values, s['count'].values
    b = [sv[k].sum() / cv[k].sum() for k in (rng.integers(0, len(s), len(s)) for _ in range(150))]
    return x.mean() / (np.std(b) + 1e-9)
def agg(gp):
    w = gp.n.values
    return pd.Series(dict(folds=len(gp), n=int(gp.n.sum()), base_w=gp.base_w.mean(), win=np.average(gp.w, weights=w), exp=np.average(gp.r, weights=w), base_r=gp.base_r.mean(),
                          net=np.average(gp.r, weights=w) - COST, excess=np.average(gp.x, weights=w), t=boot(gp)))
A_ = D.groupby(['regime', 'model', 'tp', 'sl']).apply(agg).reset_index()
pd.set_option('display.width', 250); pd.set_option('display.max_rows', 500)
A_.to_csv(os.path.join(os.path.dirname(os.environ['PANEL']), 'dna_tp_grid.csv'), index=False)
print('combos x regimes x models:', len(A_))
for m in ('HGB_TB', 'HGB_T', 'RULE_TB'):
    print(f'\n=== {m}: net expectancy per trade (%) by regime x TP/SL  (t of excess in brackets only where |t|>=2)')
    X = A_[A_.model == m].copy(); X['cell'] = X.apply(lambda r: f"{r.net:+.2f}" + (f"[{r.t:+.1f}]" if abs(r.t) >= 2 else ''), axis=1)
    X['k'] = X.tp.astype(str) + '/' + X.sl.astype(str)
    print(X.pivot(index='regime', columns='k', values='cell').fillna('-').to_string())
print('\n=== top 15 by t of excess (any model), net>0 only')
print(A_[(A_.net > 0) & (A_.n >= 150)].sort_values('t', ascending=False).head(15).round(2).to_string(index=False))
print('\n=== broker contribution: HGB_TB minus HGB_T (excess, pp) by regime, mean over combos')
pv = A_[A_.model.isin(['HGB_T', 'HGB_TB'])].pivot_table(index=['regime', 'tp', 'sl'], columns='model', values='excess')
pv['d'] = pv.HGB_TB - pv.HGB_T; print(pv.groupby('regime').d.agg(['mean', lambda s: (s > 0).mean()]).round(2).rename(columns={'<lambda_0>': 'share_positive'}).to_string())
print('\n=== readable RULE filters (fold0), best combo per regime by train leaf expectancy')
for rg in sorted({k[0] for k in rule_txt}):
    ks = [k for k in rule_txt if k[0] == rg]
    best = max(ks, key=lambda k: max(x[3] for x in rule_txt[k]))
    print(rg, f'TP{best[1]}/SL{best[2]}'); [print('   n=%d win=%.1f%% train_exp=%+.2f  IF %s' % (x[1], x[2], x[3], x[0])) for x in sorted(rule_txt[best], key=lambda x: -x[3])[:4]]
