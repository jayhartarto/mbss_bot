"""research/oversold_outside_regime_2026_10_29.py -- is there an oversold-bounce rule that works OUTSIDE the OSB-v2 production regime (IHSG dd_100<=-10% & phase!=BEARISH_GRIND)?
Only days where the production regime is OFF are used.  Universe: close>=100, 20d mean value>=Rp1bn, not HSC.  Entry = open of next day.
Search target (train only): mean EXCESS 5-day return (stock close(e+4)/open(e) minus the same-date universe mean, so market direction cancels), cluster-t by date.
Greedy beam over simple threshold conditions on stock-only features (no IHSG feature in the rule); depth<=3.  The top rules are then scored ONCE on the held-out test with the
OSB-v2 exit (TP10/SL8/BE5/8d), 0.4% cost, one position per ticker, week-cluster bootstrap, and split by IHSG short state.  Train 2023-09-01..2025-06-30, test 2025-07-01..2026-10-13.
Run: PYTHONUTF8=1 PYTHONPATH=. python research/oversold_outside_regime_2026_10_29.py"""
import os, sqlite3, sys, json, warnings
import numpy as np, pandas as pd
warnings.filterwarnings('ignore')
sys.path.insert(0, os.getcwd())
from engine import legacy_core as core
import engine.ihsg_regime as reg

COST, START, END, SPLIT = 0.4, '2023-09-01', '2026-10-13', '2025-07-01'
SL, TP, BE, H = 8.0, 10.0, 5.0, 8
rng = np.random.default_rng(3)

jc = pd.read_csv('research/bull5/glob/JKSE.csv', index_col=0, parse_dates=True).Close.dropna()
I = pd.DataFrame({'i_r1': jc.pct_change() * 100, 'i_r5': jc.pct_change(5) * 100, 'i_dd10': (jc / jc.rolling(10).max() - 1) * 100})
Rg = reg._build_regime(pd.DataFrame({'Close': jc.values}, index=jc.index)).set_index('date')
I['p0'] = ((Rg.dd_100 * 100 <= -10) & (Rg.regime_phase != 'BEARISH_GRIND')).values
I.index = pd.to_datetime(I.index)

HSC = set(json.load(open('hsc_list.json'))) if os.path.exists('hsc_list.json') else set()
if HSC and isinstance(next(iter(HSC)), dict): HSC = set()
px = pd.read_sql_query("SELECT ticker,date,open,high,low,close,volume FROM ohlcv_daily WHERE date<='2026-10-28' ORDER BY ticker,date", sqlite3.connect('mbss_ohlcv.db'), parse_dates=['date'])

def sim_all(o, h, l, c):
    """OSB-v2 exit for an entry at the open of every next bar, vectorised over signal bars."""
    n = len(c); ret = np.full(n, np.nan); days = np.zeros(n); fwd5 = np.full(n, np.nan)
    i = np.arange(0, n - H); e = i + 1; ep = o[e]; good = ep > 0
    stop = ep * (1 - SL / 100); armed = np.zeros(len(i), bool); done = np.zeros(len(i), bool); rr = np.zeros(len(i)); dd = np.zeros(len(i))
    for k in range(H):
        j = e + k
        stop = np.where(armed, np.maximum(stop, ep * 1.002), stop)
        hit = ~done & (l[j] <= stop); rr = np.where(hit, (np.minimum(o[j], stop) / ep - 1) * 100, rr); dd = np.where(hit, k + 1, dd); done |= hit
        tp = ~done & (h[j] >= ep * (1 + TP / 100)); rr = np.where(tp, TP, rr); dd = np.where(tp, k + 1, dd); done |= tp
        armed |= ~done & (h[j] >= ep * (1 + BE / 100))
    last = e + H - 1
    rr = np.where(done, rr, (c[last] / ep - 1) * 100); dd = np.where(done, dd, H)
    ret[i] = np.where(good, rr, np.nan); days[i] = dd; fwd5[i] = np.where(good, (c[e + 4] / ep - 1) * 100, np.nan)
    return ret, days, fwd5

rows = []
for t, g in px.groupby('ticker'):
    if t in HSC or len(g) < 120: continue
    g = g.reset_index(drop=True); o, h, l, c, v = (g[k].astype(float) for k in ('open', 'high', 'low', 'close', 'volume'))
    f = pd.DataFrame({'ticker': t, 'date': g.date, 'close': c})
    f['rsi'] = core.calculate_rsi(c).values; f['dist20'] = (c / c.rolling(20).mean() - 1) * 100; f['dist50'] = (c / c.rolling(50).mean() - 1) * 100
    f['dd20s'] = (c / c.rolling(20).max() - 1) * 100; f['dd60s'] = (c / c.rolling(60).max() - 1) * 100
    for k in (1, 3, 5, 10): f[f'ret{k}'] = c.pct_change(k) * 100
    rg = (h - l).replace(0, np.nan)
    f['body'] = (c / o - 1) * 100; f['cpos'] = (c - l) / rg; f['lowwick'] = (np.minimum(o, c) - l) / rg
    f['volr'] = v / v.rolling(20).mean(); f['gap'] = (o / c.shift() - 1) * 100
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    f['atrp'] = tr.rolling(14).mean() / c * 100
    ma, sd = c.rolling(20).mean(), c.rolling(20).std(); f['pctb'] = (c - (ma - 2 * sd)) / (4 * sd)
    f['down5'] = (c < o).astype(int).rolling(5).sum()
    f['liq'] = (c * v).rolling(20).mean()
    ret, days, fwd5 = sim_all(o.to_numpy(), h.to_numpy(), l.to_numpy(), c.to_numpy())
    f['ret'], f['days'], f['fwd5'] = ret, days, fwd5; f['i'] = np.arange(len(g))
    rows.append(f)
F = pd.concat(rows, ignore_index=True)
F = F.merge(I, left_on='date', right_index=True, how='inner').reset_index(drop=True)
F['rel1'] = F.ret1 - F.i_r1; F['rel5'] = F.ret5 - F.i_r5
F = F[(F.date >= START) & (F.date <= END) & (F.close >= 100) & (F.liq >= 1e9) & (~F.p0)].dropna(subset=['fwd5', 'ret', 'rsi', 'dist50', 'dd60s', 'atrp', 'volr', 'pctb', 'cpos']).reset_index(drop=True)
F['exc'] = F.fwd5 - F.groupby('date').fwd5.transform('mean')
print('rows', len(F), 'dates', F.date.nunique(), '| train rows', int((F.date < SPLIT).sum()), 'test rows', int((F.date >= SPLIT).sum()))

FEATS = ['rsi', 'dist20', 'dist50', 'dd20s', 'dd60s', 'ret1', 'ret3', 'ret5', 'ret10', 'body', 'cpos', 'lowwick', 'volr', 'gap', 'atrp', 'pctb', 'down5', 'rel1', 'rel5']
OS = (F.rsi < 45) & (F.dist20 <= -7)      # oversold base: the search may only ADD conditions inside oversold stocks (excess is still vs ALL stocks that day)
print('oversold base rows', int(OS.sum()), '| mean excess5', round(F[OS].exc.mean(), 2), '| mean fwd5', round(F[OS].fwd5.mean(), 2))
tr = F[OS & (F.date < SPLIT)].reset_index(drop=True); te = F[OS & (F.date >= SPLIT)].reset_index(drop=True)
dcode, dlab = pd.factorize(tr.date); nd = len(dlab)
exc = tr.exc.to_numpy()
conds = []
for f in FEATS:
    qs = np.unique(np.nanpercentile(tr[f], [5, 10, 20, 30, 70, 80, 90, 95]))
    for q in qs:
        conds.append((f, '<=', q, (tr[f] <= q).to_numpy())); conds.append((f, '>=', q, (tr[f] >= q).to_numpy()))

def score(mask):
    n = int(mask.sum())
    if n < 250: return -9, n, 0
    S = np.bincount(dcode[mask], weights=exc[mask], minlength=nd); N = np.bincount(dcode[mask], minlength=nd); m = S.sum() / n
    if (N > 0).sum() < 60: return -9, n, m
    se = np.sqrt(((S - m * N) ** 2).sum()) / n
    return (m / se if se > 0 else -9), n, m

beam = [((), np.ones(len(tr), bool), -9, 0, 0.0)]; best = {}; tried = 0
for depth in range(3):
    cand = []
    for desc, mask, _, _, _ in beam:
        used = {d[0] for d in desc}
        for f, op, q, cm in conds:
            if f in used: continue
            m2 = mask & cm; t_, n_, mean_ = score(m2); tried += 1
            if t_ > -9 and mean_ > 0.5: cand.append((desc + ((f, op, q),), m2, t_, n_, mean_))
    cand.sort(key=lambda x: -x[2]); beam = cand[:25]
    for d in beam: best[d[0]] = d[2:]
print('conditions', len(conds), '| rule evaluations', tried)
top = sorted(best.items(), key=lambda kv: -kv[1][0])[:12]

def dedup(df):
    keep, busy = [], {}
    for r in df.sort_values(['date', 'ticker']).itertuples():
        e = r.i + 1; x = r.i + int(r.days)
        if r.ticker in busy and e <= busy[r.ticker]: continue
        keep.append(r.Index); busy[r.ticker] = x
    return df.loc[keep]

def boot(d, B=1500):
    if len(d) < 15: return (np.nan, np.nan)
    wk = d.date.dt.strftime('%G-%V').to_numpy(); net = (d.ret - COST).to_numpy(); dy = d.days.to_numpy()
    S = pd.DataFrame({'n': net, 'd': dy, 'c': 1, 'w': wk}).groupby('w').sum().to_numpy(); out = []
    for _ in range(B):
        s = S[rng.integers(0, len(S), len(S))].sum(0); out.append(s[0] / s[1])
    return tuple(np.percentile(out, [5, 95]))

def apply(df, desc):
    m = np.ones(len(df), bool)
    for f, op, q in desc: m &= (df[f] <= q).to_numpy() if op == '<=' else (df[f] >= q).to_numpy()
    return df[m]

ND = {'train': tr.date.nunique(), 'test': te.date.nunique()}
def stats(name, d, nd):
    d = dedup(d)
    if len(d) == 0: return dict(rule=name, n=0)
    net = d.ret - COST; lo, hi = boot(d)
    return dict(rule=name, n=len(d), per_day=round(len(d) / nd, 2), days_with=round(100 * d.date.nunique() / nd), win=round(100 * (net > 0).mean(), 1), mean_n=round(net.mean(), 2),
                hold=round(d.days.mean(), 1), rd=round(net.mean() / d.days.mean(), 3), ci90=f'[{lo:.2f},{hi:.2f}]', exc5=round(d.exc.mean(), 2), worst=round(d.ret.min(), 1))

pd.set_option('display.width', 300); pd.set_option('display.max_colwidth', 90)
fmt = lambda desc: ' & '.join(f'{f}{op}{q:.2f}' for f, op, q in desc)
tab = []
for desc, (t_, n_, m_) in top:
    a = stats(fmt(desc), apply(tr, desc), ND['train']); a = {'set': 'TRAIN', 'trainT': round(t_, 1)} | a
    b = stats(fmt(desc), apply(te, desc), ND['test']); b = {'set': 'TEST', 'trainT': round(t_, 1)} | b
    tab += [a, b]
print('\nTOP train rules (rule evaluated on train, then once on held-out test)')
print(pd.DataFrame(tab).to_string(index=False))
# reference: current OSB-v2 trigger applied outside the regime
ref = lambda d: d[(d.rsi >= 20) & (d.rsi < 40) & (d.dist20 <= -10) & (d.body > 0) & (d.ret1 <= d.i_r1)]
print('\nreference: OSB-v2 stock trigger outside regime'); print(pd.DataFrame([stats('OSB trigger train', ref(tr), ND['train']), stats('OSB trigger test', ref(te), ND['test'])]).to_string(index=False))
# best test-validated rule: split by IHSG short state
if top:
    d0 = top[0][0]; T0 = apply(te, d0)
    bk = pd.cut(T0.i_dd10, [-100, -4, -2, 0.01], labels=['dd10<=-4', '-4..-2', 'tenang >-2'])
    print('\nTOP rule on test by IHSG dd10 state:', fmt(d0))
    print(pd.DataFrame([stats(str(k), T0[bk == k], ND['test']) for k in bk.cat.categories]).to_string(index=False))
