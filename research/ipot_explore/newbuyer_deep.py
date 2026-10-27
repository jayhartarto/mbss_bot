"""research/ipot_explore/newbuyer_deep.py — deep dive on the 'new-buyer surge' signal (NEUTRAL_BULLISH, the only regime with enough dates), 2026-10-27.
A  volume/day-0 control: is it just a volume spike?  excess re-based on week x ATR-tercile x volume-ratio-tercile x day-0-return-bucket cells.
B  definition sensitivity (prior-activity cut 0.1/0.25/0.5%), who the newcomers are (role), count of newcomers, dose-response by decile.
C  technical PHASE slices (pre-declared, interpretable) -> P(ret5>0), P(TP+5 before SL-5), raw/excess ret5, incremental vs the same phase without the signal; discovery (first 60% of dates) vs confirmation (later 40%, 10d embargo).
D  depth-3 tree on signal rows only (discovery) -> readable phases, applied to confirmation.
Run: PYTHONUTF8=1 FLOW=<flow_panel.pkl> DNA=<dna_panel2.pkl> PYTHONPATH=. python research/ipot_explore/newbuyer_deep.py"""
import os, sys, sqlite3, warnings, numpy as np, pandas as pd
warnings.filterwarnings('ignore')
sys.path.insert(0, os.getcwd())
from sklearn.tree import DecisionTreeClassifier
from engine.broker_roles import role_of
rng = np.random.default_rng(13)
ROOT = os.getcwd()
F = pd.read_pickle(os.environ['FLOW']).replace([np.inf, -np.inf], np.nan)
Dn = pd.read_pickle(os.environ['DNA'])
tech = ['rsi_0', 'd5_0', 'd20_0', 'd50_0', 'pctb_0', 'bw_0', 'bw_L10', 'volr_0', 'volr5', 'mh_0', 'h20_dist', 'l20_dist', 'sma20_sl', 'sma50_sl', 'green3', 'r1_0', 'r5_0', 'r10', 'cp_0', 'body_0', 'uw_0', 'tr_tp5_sl5', 'tr_tp5_nosl']
X = F.merge(Dn[['ticker', 'date'] + tech], on=['ticker', 'date'], how='inner')
X = X[X.regime_phase == 'NEUTRAL_BULLISH'].copy()
X['tpfirst'] = (X.tr_tp5_sl5 >= 4.99).astype(float); X['up5'] = (X.ret5 > 0).astype(float)
dates = np.sort(X.date.unique()); cut = dates[int(len(dates) * 0.6)]; emb = dates[min(len(dates) - 1, int(len(dates) * 0.6) + 10)]
X['period'] = np.where(X.date <= cut, 'disc', np.where(X.date > emb, 'conf', 'emb'))
X = X[X.period != 'emb'].copy()
X['wk'] = pd.to_datetime(X.date).dt.to_period('W').astype(str)
thr = X[X.period == 'disc'].new_buy.quantile(0.9); print('NEUTRAL rows', len(X), '| new_buy decile threshold (discovery) = %.4f' % thr, '| dates disc/conf:', X[X.period == 'disc'].date.nunique(), X[X.period == 'conf'].date.nunique())
X['sig'] = X.new_buy >= thr
X['x5'] = X.ret5 - X.groupby(['date', 'atr_cell']).ret5.transform('mean')

def bt(sub, col, blk=5, n=250):
    if len(sub) < 50: return np.nan, np.nan
    ds = np.sort(sub.date.unique()); bid = {d: i // blk for i, d in enumerate(ds)}
    g = sub.assign(b=sub.date.map(bid)).groupby('b')[col].agg(['sum', 'count']); sv, cv = g['sum'].values, g['count'].values
    if len(g) < 8: return sub[col].mean(), np.nan
    m = sv.sum() / cv.sum(); bs = [sv[k].sum() / cv[k].sum() for k in (rng.integers(0, len(g), len(g)) for _ in range(n))]
    return m, m / (np.std(bs) + 1e-9)

C = X[X.period == 'conf']; Dd = X[X.period == 'disc']
print('\ncorr(new_buy, volr_0)=%.2f corr(new_buy, |r1_0|)=%.2f' % (X.new_buy.corr(X.volr_0), X.new_buy.corr(X.r1_0.abs())))
# ---------------- A
X['vt'] = X.groupby('date').volr_0.transform(lambda s: pd.qcut(s.rank(method='first'), 3, labels=False, duplicates='drop'))
X['rb'] = pd.cut(X.r1_0, [-100, -2, 2, 100], labels=[0, 1, 2])
X['x5c'] = X.ret5 - X.groupby(['wk', 'atr_cell', 'vt', 'rb']).ret5.transform('mean')
C = X[X.period == 'conf']; Dd = X[X.period == 'disc']
print('\n=== A. volume / day-0 control (excess re-based on week x ATR x volume-tercile x day0-return cells)')
for nm, S in (('disc', Dd), ('conf', C)):
    s = S[S.sig]; print(f'{nm}: n={len(s)} ex5_ATRcell={bt(s,"x5")[0]:+.2f}(t{bt(s,"x5")[1]:+.1f})  ex5_fullcontrol={bt(s,"x5c")[0]:+.2f}(t{bt(s,"x5c")[1]:+.1f})')
print('conf, within volume tercile (full-control excess, t) [n]:', ' | '.join(f"vol{int(v)}: {bt(C[C.sig&(C.vt==v)],'x5c')[0]:+.2f}({bt(C[C.sig&(C.vt==v)],'x5c')[1]:+.1f})[{int((C.sig&(C.vt==v)).sum())}]" for v in (0, 1, 2)))
print('conf, within day-0 return bucket:', ' | '.join(f"r1[{['<-2','-2..2','>2'][int(v)]}]: {bt(C[C.sig&(C.rb==v)],'x5c')[0]:+.2f}({bt(C[C.sig&(C.rb==v)],'x5c')[1]:+.1f})[{int((C.sig&(C.rb==v)).sum())}]" for v in (0, 1, 2)))
# ---------------- B variants (recompute from raw broker data)
c = sqlite3.connect(os.path.join(ROOT, 'research', 'ipot_merged.sqlite'))
d = pd.read_sql_query("SELECT ticker, trade_date date, broker_code broker, buy_lot, sell_lot FROM ipot_broker_daily WHERE fd='all' AND board='all'", c); c.close()
c = sqlite3.connect(os.path.join(ROOT, 'mbss_ohlcv.db')); o = pd.read_sql_query("SELECT ticker, date, volume FROM ohlcv_daily WHERE date>='2022-10-01' ORDER BY ticker,date", c); c.close()
ms = d.groupby(['ticker', 'date']).sell_lot.transform('min').fillna(0); sl_ = d.sell_lot.fillna(0); bl_ = d.buy_lot.fillna(0)
d['net_lo'] = np.where(d.buy_lot.isna(), -sl_, bl_ - np.where(d.sell_lot.isna(), ms, sl_))
keep = set(X.ticker.unique()); dg = {t: x for t, x in d[d.ticker.isin(keep)].groupby('ticker')}; rows = []
for t, g in o[o.ticker.isin(keep)].groupby('ticker'):
    if t not in dg: continue
    cal = g.date.values; vol = g.volume.values / 100.0; vs5 = pd.Series(vol).rolling(5).sum().values; vs20 = pd.Series(vol).rolling(20).sum().values
    pv = dg[t].pivot_table(index='date', columns='broker', values='net_lo', aggfunc='sum').reindex(cal).fillna(0.0)
    roll5 = pv.rolling(5).sum().div(vs5, axis=0); pa = pv.abs().rolling(20).sum().shift(5).div(vs20, axis=0)
    rl = pd.Series({b: role_of(b) for b in pv.columns}); f = {'ticker': [t] * len(cal), 'date': cal}
    for cutv, nm in ((0.001, '01'), (0.0025, '025'), (0.005, '05')):
        f['nb_' + nm] = roll5.clip(lower=0).where(pa < cutv, 0).sum(axis=1).values
    newm = pa < 0.0025; pos = roll5.clip(lower=0).where(newm, 0)
    for r_ in ('foreign_inst', 'gov_inst', 'retail_app', 'scalper', 'local_other', 'foreign_retail_base'):
        cc = rl.index[rl == r_]; f['nbr_' + r_] = pos[cc].sum(axis=1).values if len(cc) else 0.0
    f['nb_cnt'] = (pos >= 0.005).sum(axis=1).values; f['nb_max'] = pos.max(axis=1).values
    rows.append(pd.DataFrame(f))
V = pd.concat(rows, ignore_index=True).replace([np.inf, -np.inf], np.nan)
X = X.merge(V, on=['ticker', 'date'], how='left'); C = X[X.period == 'conf']; Dd = X[X.period == 'disc']
print('\n=== B. definition sensitivity & composition (conf, top decile of each variant by discovery threshold; full-control excess ret5)')
for v in ('nb_01', 'nb_025', 'nb_05', 'nbr_foreign_inst', 'nbr_gov_inst', 'nbr_retail_app', 'nbr_scalper', 'nbr_local_other', 'nbr_foreign_retail_base', 'nb_max'):
    q = Dd[v].quantile(0.9)
    if q <= 0: q = Dd[v][Dd[v] > 0].quantile(0.5)
    s = C[C[v] >= q]; m, t = bt(s, 'x5c'); print(f'{v:26s} thr={q:.4f} n={len(s):6d} ex5c={m:+.2f}(t{t:+.1f}) raw5={s.ret5.mean():+.2f} up5={s.up5.mean()*100:.0f}%')
for k in (1, 2, 3):
    s = C[(C.nb_cnt == k) if k < 3 else (C.nb_cnt >= 3)]; m, t = bt(s, 'x5c'); print(f'nb_cnt{"=" if k<3 else ">="}{k}: n={len(s)} ex5c={m:+.2f}(t{t:+.1f})')
print('dose-response (conf, deciles of new_buy by discovery edges), ex5c:')
edges = np.unique(np.quantile(Dd.new_buy.dropna(), np.linspace(0, 1, 11))); edges[0] = -1e9; edges[-1] = 1e9; C2 = C.assign(dec=pd.cut(C.new_buy, edges, include_lowest=True, labels=False))
print(' '.join(f"d{int(k)}:{v:+.2f}" for k, v in C2.groupby('dec').x5c.mean().items()))
# ---------------- C phases
P = {'uptrend (c>sma20,sma50, slope+)': lambda S: (S.d20_0 > 0) & (S.d50_0 > 0) & (S.sma20_sl > 0),
     'pullback in uptrend (d50>0, d20 -6..0, rsi<50)': lambda S: (S.d50_0 > 0) & S.d20_0.between(-6, 0) & (S.rsi_0 < 50),
     'squeeze (bw<10 & narrowing)': lambda S: (S.bw_0 < 10) & (S.bw_0 < S.bw_L10),
     'breakout (within 2% of 20d high & volr>1.5)': lambda S: (S.h20_dist >= -2) & (S.volr_0 > 1.5),
     'near-high consolidation (-8..-2% of high, atr<4)': lambda S: S.h20_dist.between(-8, -2) & (S.atr_0 < 4),
     'oversold bounce (rsi<35 & green)': lambda S: (S.rsi_0 < 35) & (S.body_0 > 0),
     'downtrend (d20<-5 & d50<0)': lambda S: (S.d20_0 < -5) & (S.d50_0 < 0),
     'extended (d20>10 or r10>15)': lambda S: (S.d20_0 > 10) | (S.r10 > 15),
     'volume dry-up before (volr5<0.8)': lambda S: S.volr5 < 0.8,
     'macd hist>0 & rising': lambda S: (S.mh_0 > 0) & (S.mh_0 > S.mh_0.shift(0) * 0),
     'rsi 45-65': lambda S: S.rsi_0.between(45, 65), 'high vol (atr>6)': lambda S: S.atr_0 > 6, 'low vol (atr<3)': lambda S: S.atr_0 < 3,
     'green candle close in top 30% of range': lambda S: (S.cp_0 > 0.7) & (S.body_0 > 0), 'price<=Rp200': lambda S: S.close <= 200}
print('\n=== C. technical phases: signal rows inside phase (conf)  |  P(up5) P(TP5-before-SL5) raw5 ex5 | incremental vs same phase WITHOUT signal (ex5 diff) | discovery ex5')
res = []
for nm, fn in P.items():
    for per, S in (('disc', Dd), ('conf', C)):
        m = fn(S).fillna(False); a = S[m & S.sig]; b = S[m & ~S.sig]
        if per == 'disc': dex = a.x5.mean(); continue
        if len(a) < 100: continue
        e, te = bt(a, 'x5'); eb = b.x5.mean()
        res.append((nm, len(a), a.up5.mean() * 100, a.tpfirst.mean() * 100, b.tpfirst.mean() * 100, a.ret5.mean(), e, te, e - eb, dex, b.up5.mean() * 100))
R = pd.DataFrame(res, columns=['phase', 'n', 'P_up5', 'P_tpfirst', 'P_tpfirst_nosig', 'raw5', 'ex5', 't', 'incr_vs_nosig', 'disc_ex5', 'P_up5_nosig']).sort_values('P_tpfirst', ascending=False)
base = C[C.sig]; print('ALL signal rows conf: n=%d P_up5=%.1f%% P_tpfirst=%.1f%% raw5=%+.2f | all non-signal: P_up5=%.1f%% P_tpfirst=%.1f%%' % (len(base), base.up5.mean() * 100, base.tpfirst.mean() * 100, base.ret5.mean(), C[~C.sig].up5.mean() * 100, C[~C.sig].tpfirst.mean() * 100))
pd.set_option('display.width', 250); pd.set_option('display.max_colwidth', 60); print(R.round(2).to_string(index=False))
# ---------------- D tree
ft = [c for c in ['rsi_0', 'd5_0', 'd20_0', 'd50_0', 'pctb_0', 'bw_0', 'volr_0', 'volr5', 'mh_0', 'h20_dist', 'l20_dist', 'sma20_sl', 'sma50_sl', 'green3', 'r1_0', 'r5_0', 'r10', 'cp_0', 'atr_0'] if c in X]
sd = Dd[Dd.sig].dropna(subset=ft + ['tpfirst']); sc = C[C.sig].dropna(subset=ft + ['tpfirst'])
for tgt in ('tpfirst', 'up5'):
    tr = DecisionTreeClassifier(max_depth=3, min_samples_leaf=250, random_state=0).fit(sd[ft], sd[tgt]); t_ = tr.tree_; ld, lc = tr.apply(sd[ft].values), tr.apply(sc[ft].values)
    def paths(n, cs):
        if t_.children_left[n] == -1: return [(n, cs)]
        f, th = ft[t_.feature[n]], t_.threshold[n]
        return paths(t_.children_left[n], cs + [f'{f}<={th:.3g}']) + paths(t_.children_right[n], cs + [f'{f}>{th:.3g}'])
    print(f'\n=== D. tree on signal rows, target={tgt} (disc base {sd[tgt].mean()*100:.1f}%, conf base {sc[tgt].mean()*100:.1f}%)')
    for n, cs in sorted(paths(0, []), key=lambda x: -sd[tgt][ld == x[0]].mean()):
        a = sd[ld == n]; b = sc[lc == n]
        if len(a) < 250 or len(b) < 50: continue
        print(f'  disc n={len(a):5d} {tgt}={a[tgt].mean()*100:5.1f}% | conf n={len(b):5d} {tgt}={b[tgt].mean()*100:5.1f}% up5={b.up5.mean()*100:4.1f}% raw5={b.ret5.mean():+.2f} ex5={b.x5.mean():+.2f} | ' + ' & '.join(cs))
