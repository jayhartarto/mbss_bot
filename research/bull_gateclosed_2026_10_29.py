"""research/bull_widen_2026_10_29.py -- can BULL fire more often without losing its edge?  Variants of liquidity floor, confirmation window and formula set, all on the SAME engine
(engine.bull features / raw_signals / simulate_exit), the hybrid IHSG gate (J2) evaluated on the signal date, next-open entry, one position per ticker, 0.4% cost.
Live universe = ticker_whitelist.json (ISSI eligible, 465) minus HSC; a second block shows the full research universe (all DB tickers) for reference.
Window 2023-09-01..2026-10-13; TEST = Jul 2025 on (bull5 formulas/exit were tuned before that).  Uncertainty = week-cluster bootstrap.
Run: PYTHONUTF8=1 PYTHONPATH=. python research/bull_widen_2026_10_29.py"""
import os, sqlite3, sys, json, warnings
import numpy as np, pandas as pd
warnings.filterwarnings('ignore')
sys.path.insert(0, os.getcwd())
import engine.bull as bull

COST, START, END, SPLIT = 0.4, '2023-09-01', '2026-10-13', '2025-07-01'
rng = np.random.default_rng(9)
WL = set(json.load(open('ticker_whitelist.json'))['eligible_tickers'])
HSC = set(json.load(open('hsc_list.json'))) if os.path.exists('hsc_list.json') else set()
if HSC and isinstance(next(iter(HSC)), dict): HSC = set()
J = pd.read_pickle('research/bull5/J2.pkl'); J.index = pd.to_datetime(J.index)
J['hyb'] = (J.d50 >= -1.1) | ((J.above10low >= 4) & (J.d5 >= 0))
px = pd.read_sql_query("SELECT ticker,date,open,high,low,close,volume FROM ohlcv_daily WHERE date<='2026-10-28' ORDER BY ticker,date", sqlite3.connect('mbss_ohlcv.db'), parse_dates=['date'])

rows, EX = [], {}
for t, g in px.groupby('ticker'):
    if t in HSC or len(g) < bull.MIN_HISTORY: continue
    d = g.set_index('date').rename(columns=str.capitalize)
    f = bull.features(d); s = bull.raw_signals(f)
    raw = s.raw.astype(int)
    pri = {w: (raw.shift(1).rolling(w, min_periods=1).sum().fillna(0) >= 1).to_numpy() for w in (1, 2, 3, 5, 8)}
    x = pd.DataFrame({'ticker': t, 'date': f.date.values, 'i': np.arange(len(f)), 'liq': f.liq.values, 'close': f.close.values, 'raw': s.raw.values,
                      'S1': s.S1.values, 'S2': s.S2.values, 'A': s.A.values, 'B': s.B.values, 'c1': pri[1], 'c2': pri[2], 'c3': pri[3], 'c5': pri[5], 'c8': pri[8]})
    x = x[x.raw & (x.date >= START) & (x.date <= END)]
    if len(x): rows.append(x)
    EX[t] = (d.Open.to_numpy(float), d.High.to_numpy(float), d.Low.to_numpy(float), d.Close.to_numpy(float))
S = pd.concat(rows, ignore_index=True)
S['hyb'] = S.date.map(lambda d: bool(J.hyb.get(pd.Timestamp(d), True)))
S = S[(~S.hyb) & (S.close >= bull.PRICE_MIN)].reset_index(drop=True)      # gate and price floor are part of the lane definition in every variant
res = []
for r in S.itertuples():
    o, h, l, c = EX[r.ticker]; q = bull.simulate_exit(o, h, l, c, r.i)
    res.append((np.nan, 0) if q is None or q['status'] == 'ALIVE' else (q['ret_pct'], q['bars']))
S['ret'] = [a for a, _ in res]; S['days'] = [b for _, b in res]; S = S.dropna(subset=['ret']).reset_index(drop=True)
print('gated raw alerts with outcome:', len(S))

def dedup(df):
    keep, busy = [], {}
    for r in df.sort_values(['date', 'ticker']).itertuples():
        e = r.i + 1; x = r.i + int(r.days)
        if r.ticker in busy and e <= busy[r.ticker]: continue
        keep.append(r.Index); busy[r.ticker] = x
    return df.loc[keep]

def boot(d, B=1200):
    if len(d) < 15: return (np.nan, np.nan)
    wk = d.date.dt.strftime('%G-%V').to_numpy(); net = (d.ret - COST).to_numpy()
    T = pd.DataFrame({'n': net, 'd': d.days.to_numpy(), 'w': wk}).groupby('w').sum().to_numpy(); out = []
    for _ in range(B):
        s = T[rng.integers(0, len(T), len(T))].sum(0); out.append(s[0] / s[1])
    return tuple(np.percentile(out, [5, 95]))

cl = J[~J.hyb]; NDAYS = int(((cl.index >= START) & (cl.index <= END)).sum()); NTEST = int(((cl.index >= SPLIT) & (cl.index <= END)).sum()); print('hari gate tertutup', NDAYS, 'test', NTEST)
def line(name, m, uni):
    d = dedup(S[m & uni]);
    if not len(d): return dict(variant=name, n=0)
    net = d.ret - COST; lo, hi = boot(d); te = d[d.date >= SPLIT]; tr = d[d.date < SPLIT]
    f = lambda x: round((x.ret - COST).mean() / x.days.mean(), 2) if len(x) >= 10 else np.nan
    return dict(variant=name, n=len(d), per_day=round(len(d) / NDAYS, 2), test_per_day=round(len(te) / NTEST, 2), win=round(100 * (net > 0).mean(), 1), mean_n=round(net.mean(), 2),
                hold=round(d.days.mean(), 1), rd=round(net.mean() / d.days.mean(), 3), ci90=f'[{lo:.2f},{hi:.2f}]', rd_train=f(tr), rd_test=f(te), worst=round(d.ret.min(), 1))

lq = lambda x: S.liq >= x
V = {
    'V0 produksi: liq>=1, konfirmasi 3': lq(1) & S.c3,
    'liq>=0.5, konfirmasi 3': lq(0.5) & S.c3,
    'liq>=0.3, konfirmasi 3': lq(0.3) & S.c3,
    'liq>=1, konfirmasi 2': lq(1) & S.c2,
    'liq>=1, konfirmasi 5': lq(1) & S.c5,
    'liq>=1, konfirmasi 8': lq(1) & S.c8,
    'liq>=1, tanpa konfirmasi (alert mentah)': lq(1) & S.raw,
    'liq>=0.5, konfirmasi 5': lq(0.5) & S.c5,
    'liq>=1, konfirmasi 3 hanya S1/S2 (momentum)': lq(1) & S.c3 & (S.S1 | S.S2),
    'liq>=1, konfirmasi 3 hanya A/B (pullback)': lq(1) & S.c3 & (S.A | S.B),
    'liq>=1, mentah hanya S1/S2': lq(1) & (S.S1 | S.S2),
    'liq>=1, mentah hanya A/B': lq(1) & (S.A | S.B),
    'MARGINAL: alert pertama (tak terkonfirmasi), liq>=1': lq(1) & S.raw & ~S.c3,
    'MARGINAL: tak terkonfirmasi hanya S1/S2': lq(1) & ~S.c3 & (S.S1 | S.S2),
    'MARGINAL: tak terkonfirmasi hanya A/B': lq(1) & ~S.c3 & (S.A | S.B),
    'MARGINAL: konfirmasi 3 tapi liq 0.3-1': (S.liq >= 0.3) & (S.liq < 1) & S.c3,
}
pd.set_option('display.width', 300); pd.set_option('display.max_colwidth', 50)
for nm, uni in (('LIVE universe (whitelist ISSI 465 - HSC)', S.ticker.isin(WL)), ('RESEARCH universe (semua ticker DB - HSC)', S.ticker.notna())):
    print('\n=== ' + nm + ' | gate hybrid TERTUTUP | ' + START + '..' + END)
    print(pd.DataFrame([line(k, m, uni) for k, m in V.items()]).to_string(index=False))
