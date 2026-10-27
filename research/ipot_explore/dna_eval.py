"""research/ipot_explore/dna_eval.py — DNA matching of short-swing winners (win5: ret5>=+5% from open t+1) PER IHSG REGIME, strict out-of-sample.
For each regime separately: train on the first 60% of that regime's dates and ticker half A, test on the later 40% (10-day embargo) and the UNSEEN ticker half B;
then the swapped fold (A<->B). Feature sets: T (technical, days -10..0), B (broker role nets/breadth), TB. Models: DNA (diagonal-LDA template = mean winner-vs-rest
vector in train z-space), LOGIT (L2), HGB (shallow gradient boosting). Metrics on the test top decile by score: precision & lift vs test base, ret5 minus same-date mean (excess), net of 0.4% cost, ret1.
Run: PYTHONUTF8=1 PANEL=<pkl> python research/ipot_explore/dna_eval.py"""
import os, warnings, numpy as np, pandas as pd
warnings.filterwarnings('ignore')
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import roc_auc_score
rng = np.random.default_rng(42)
COST = 0.4; TOPQ = 0.10
P = pd.read_pickle(os.environ['PANEL']).dropna(subset=['ret5', 'hi5', 'tr_tp5_sl5']).copy()
if os.environ.get('LABEL')=='vol':
    P['win5']=(P.hi5>=1.2*P.atr_0).astype(float)  # vol-normalised TP-touch winner: high within 5d >= open(t+1) x (1 + 1.2 x ATR14%)
if os.environ.get('LABEL')=='tpfirst':
    P['win5']=(P.tr_tp5_sl5>=4.99).astype(float)  # TP +5% reached BEFORE SL -5% within 5d
P['liq_l'] = np.log10(P.liq.clip(lower=1)); P['close_l'] = np.log10(P.close)
NON = {'ticker', 'date', 'ret5', 'ret1', 'hi5', 'gap0', 'win5', 'tr_tp5_sl5', 'tr_tp5_nosl', 'regime_phase', 'atr_cell', 'liq', 'close'}
FE = [c for c in P.columns if c not in NON]
BR = [c for c in FE if c.startswith('b_')]; TE = [c for c in FE if not c.startswith('b_')]
SETS = {'T': TE, 'B': BR, 'TB': FE}
P['ret5'] = P.tr_tp5_sl5   # realised trade: TP+5% / SL-5% / expire close t+5 (same-day both -> SL)
P['ex5'] = P.ret5 - P.groupby('date').ret5.transform('mean')
P['exn'] = P.tr_tp5_nosl - P.groupby('date').tr_tp5_nosl.transform('mean')   # literal exit: TP+5% touch else close t+5, no stop   # excess vs same-date cross-section mean
tk = np.array(sorted(P.ticker.unique())); rng.shuffle(tk); half = {t: i % 2 for i, t in enumerate(tk)}
P['half'] = P.ticker.map(half)

def prep(tr, te, cols):
    lo, hi = tr[cols].quantile(0.01), tr[cols].quantile(0.99); med = tr[cols].median()
    f = lambda D: D[cols].clip(lo, hi, axis=1).fillna(med)
    a, b = f(tr), f(te); mu, sd = a.mean(), a.std().replace(0, 1)
    return ((a - mu) / sd).values, ((b - mu) / sd).values

def dna_scores(Ztr, y, Zte):
    w = Ztr[y == 1].mean(0) - Ztr[y == 0].mean(0)       # standardised mean difference per feature = the "DNA" (d')
    return Zte @ w, w

def top_dedup(df, score, q):
    thr = np.quantile(score, 1 - q); s = df[score >= thr].sort_values(['ticker', 'date']); keep = []; pt = None; pdt = None
    for i, t, dd in zip(s.index, s.ticker, pd.to_datetime(s.date)):
        if t != pt or (dd - pdt).days > 7: keep.append(i); pt, pdt = t, dd
    return s.loc[keep]

rows = []; dna_rows = []
for rg, R in P.groupby('regime_phase'):
    dates = np.sort(R.date.unique()); cut = dates[int(len(dates) * 0.6)]; emb = dates[min(len(dates) - 1, int(len(dates) * 0.6) + 10)]
    for fold in (0, 1):
        tr = R[(R.date <= cut) & (R.half == fold)]; te = R[(R.date > emb) & (R.half != fold)]
        if len(tr) < 2000 or len(te) < 1000 or tr.win5.sum() < 100: continue
        y = tr.win5.values.astype(int)
        for sn, cols in SETS.items():
            Ztr, Zte = prep(tr, te, cols)
            sc = {}
            sc['DNA'], w = dna_scores(Ztr, y, Zte)
            sc['LOGIT'] = LogisticRegression(C=0.02, max_iter=300).fit(Ztr, y).decision_function(Zte)
            sc['HGB'] = HistGradientBoostingClassifier(max_depth=3, max_iter=150, learning_rate=0.05, min_samples_leaf=200, l2_regularization=5.0).fit(Ztr, y).predict_proba(Zte)[:, 1]
            if sn != 'TB' and fold == 0:
                for j in np.argsort(-np.abs(w))[:6]: dna_rows.append((rg, sn, cols[j], round(float(w[j]), 3)))
            for mn, s in sc.items():
                try: auc = roc_auc_score(te.win5, s)
                except Exception: auc = np.nan
                pk = top_dedup(te.assign(_s=s), s, TOPQ) if False else top_dedup(te, pd.Series(s, index=te.index), TOPQ)
                rows.append(dict(regime=rg, fold=fold, set=sn, model=mn, n_test=len(te), base_win=te.win5.mean() * 100, auc=auc, n_pick=len(pk),
                                 prec=pk.win5.mean() * 100, ex5=pk.ex5.mean(), exn=pk.exn.mean(), nosl=pk.tr_tp5_nosl.mean(), ret5=pk.ret5.mean(), base_ret5=te.ret5.mean(), ret1=pk.ret1.mean(),
                                 _tick=pk.ticker.values, _ex=pk.ex5.values))
R_ = pd.DataFrame(rows)
def boot(g):
    tick = np.concatenate(g._tick.values); ex = np.concatenate(g._ex.values)
    s = pd.Series(ex).groupby(tick).agg(['sum', 'count']); sv, cv = s['sum'].values, s['count'].values
    b = [sv[k].sum() / cv[k].sum() for k in (rng.integers(0, len(s), len(s)) for _ in range(150))]
    return ex.mean() / (np.std(b) + 1e-9)
agg = R_.groupby(['regime', 'set', 'model']).apply(lambda g: pd.Series(dict(
    folds=len(g), base_win=g.base_win.mean(), auc=g.auc.mean(), prec=np.average(g.prec, weights=g.n_pick), lift=np.average(g.prec, weights=g.n_pick) / g.base_win.mean(),
    n_pick=g.n_pick.sum(), ex5=np.average(g.ex5, weights=g.n_pick), net=np.average(g.ret5, weights=g.n_pick) - COST, exn=np.average(g.exn, weights=g.n_pick), net_nosl=np.average(g.nosl, weights=g.n_pick) - COST, ret1=np.average(g.ret1, weights=g.n_pick), t_ex=boot(g)))).reset_index()
pd.set_option('display.width', 250)
print(agg.round(2).to_string(index=False))
print('\nDNA (top |d\'| features, fold0, train winners-vs-rest, standardised):')
D = pd.DataFrame(dna_rows, columns=['regime', 'set', 'feature', 'd']);
for (rg, sn), g in D.groupby(['regime', 'set']): print(rg, sn, ', '.join(f"{a}:{b:+.2f}" for a, b in zip(g.feature, g.d)))
agg.to_csv(os.path.join(os.path.dirname(os.environ['PANEL']), 'dna_eval_'+os.environ.get('LABEL','abs')+'.csv'), index=False)
