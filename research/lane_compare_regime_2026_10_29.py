"""research/lane_compare_regime_2026_10_29.py -- apples-to-apples ret/day of every lane + split by SHORT IHSG regime.
Same rules for all lanes: same signal window, same universe (close>=100, 20d median value>=Rp1bn, not HSC; native universe shown as a second table), signal after close of day i,
buy at OPEN of i+1, current production exit with gap-aware stops, mark-to-market at the horizon, one open position per ticker per lane, 0.4% round-trip cost.
ret/day = mean net trade return / mean days held.  Uncertainty = week-cluster bootstrap (trades fired in the same week are not independent).
Signals: BOW/OSB-v2/BOS/MACD-confirm/VCP = replay of the production engines (PICKS pickle from lane_newbuyer.py); BULL = engine/bull.py on the same OHLCV.
BSJP Lane1 is NOT included: it needs the D1/D2 engine and its horizon is close->next-day, so it cannot be put on this basis honestly.
Short regime = IHSG 10-day return at the signal date (tertiles of 2023-26), no long-window (SMA200/drawdown100) state.
Run: PYTHONUTF8=1 PICKS=<lane_newbuyer_picks_wide.pkl> PYTHONPATH=. python research/lane_compare_regime_2026_10_29.py"""
import os, sqlite3, sys, json, warnings
import numpy as np, pandas as pd
warnings.filterwarnings('ignore')
sys.path.insert(0, os.getcwd())
import engine.bull as bull

COST = 0.4
END = '2026-10-13'
WIN = ('2025-04-24', END)      # earliest date where every lane has coverage
TEST = ('2025-07-01', END)     # bull5 formulas/exits were tuned on data before Jul-2025
SPLIT = '2026-01-01'
rng = np.random.default_rng(7)

px = pd.read_sql_query("SELECT ticker,date,open,high,low,close,volume FROM ohlcv_daily WHERE date<='2026-10-28' ORDER BY ticker,date", sqlite3.connect('mbss_ohlcv.db'), parse_dates=['date'])
A = {}
for t, g in px.groupby('ticker'):
    c = g.close.astype(float); ma = c.rolling(20).mean(); sd = c.rolling(20).std()
    A[t] = dict(d=g.date.values, o=g.open.to_numpy(float), h=g.high.to_numpy(float), l=g.low.to_numpy(float), c=c.to_numpy(), bb=(ma + 2 * sd).to_numpy(),
                liq=(c * g.volume.astype(float)).rolling(20).median().to_numpy())
HSC = set(json.load(open('hsc_list.json'))) if os.path.exists('hsc_list.json') else set()
if isinstance(next(iter(HSC), None), dict): HSC = set()

J = pd.read_pickle('research/bull5/J2.pkl')
J.index = pd.to_datetime(J.index)
J['hyb'] = (J.d50 >= -1.1) | ((J.above10low >= 4) & (J.d5 >= 0))
q1, q2 = J.r10.quantile([1 / 3, 2 / 3])
J['reg'] = np.where(J.r10 <= q1, 'IHSG turun (r10<=%.1f%%)' % q1, np.where(J.r10 >= q2, 'IHSG naik (r10>=%.1f%%)' % q2, 'IHSG datar'))

def sim(t, d, sl, H, tp=None, band=False, be=None):
    a = A[t]; i = int(np.searchsorted(a['d'], np.datetime64(d)))
    if i >= len(a['d']) or a['d'][i] != np.datetime64(d) or i + 1 + H > len(a['c']): return None
    e = i + 1; ep = a['o'][e]
    if not ep > 0: return None
    stop = ep * (1 - sl / 100); armed = False
    for k in range(H):
        j = e + k
        if armed: stop = max(stop, ep * 1.002)
        if a['l'][j] <= stop: return (min(a['o'][j], stop) / ep - 1) * 100, k + 1, e, j
        tpp = a['bb'][j] if band else ep * (1 + tp / 100)
        if tpp == tpp and tpp >= ep and a['h'][j] >= tpp: return (tpp / ep - 1) * 100, k + 1, e, j
        if be and a['h'][j] >= ep * (1 + be / 100): armed = True
    j = e + H - 1
    return (a['c'][j] / ep - 1) * 100, H, e, j

def bull_rows():
    rows = []
    for t, a in A.items():
        if len(a['c']) < bull.MIN_HISTORY or t in HSC: continue
        d = pd.DataFrame({'Open': a['o'], 'High': a['h'], 'Low': a['l'], 'Close': a['c'], 'Volume': 0.0}, index=pd.DatetimeIndex(a['d']))
        g = px[px.ticker == t]; d['Volume'] = g.volume.to_numpy(float)
        f = bull.features(d); s = bull.raw_signals(f)
        for i in np.where(s.pick.to_numpy())[0]:
            ds = str(a['d'][i])[:10]
            if not (WIN[0] <= ds <= END): continue
            r = bull.simulate_exit(a['o'], a['h'], a['l'], a['c'], i)
            if r is None or r['status'] == 'ALIVE': continue
            rows.append(dict(ticker=t, date=ds, lane='BULL', ret=r['ret_pct'], days=r['bars'], e=i + 1, x=i + r['bars'], close=a['c'][i], liq=a['liq'][i]))
    return rows

def lane_rows(P):
    spec = {'OSBv2': dict(sl=8, H=8, tp=10, be=5), 'MACDc': dict(sl=6, H=5, tp=8), 'BOS': dict(sl=15, H=6, tp=8), 'VCP': dict(sl=10, H=12, tp=2.5), 'BOW': dict(sl=12, H=16, band=True)}
    rows = []
    for r in P.itertuples():
        if r.lane not in spec or not (WIN[0] <= r.date <= END): continue
        s = sim(r.ticker, r.date, **spec[r.lane])
        if s: rows.append(dict(ticker=r.ticker, date=r.date, lane=r.lane, ret=s[0], days=s[1], e=s[2], x=s[3], close=r.close, liq=A[r.ticker]['liq'][s[2] - 1]))
    return rows

def dedup(df):
    keep, busy = [], {}
    for r in df.sort_values(['date', 'ticker']).itertuples():
        k = (r.lane, r.ticker)
        if k in busy and r.e <= busy[k]: continue
        keep.append(r.Index); busy[k] = r.x
    return df.loc[keep]

def stat(df):
    if len(df) == 0: return dict(n=0)
    net = df.ret - COST; md = df.days.mean()
    return dict(n=len(df), win=100 * (net > 0).mean(), mean_n=net.mean(), days=md, rd=net.mean() / md, worst=df.ret.min())

def boot(df, B=1500):
    if len(df) < 8: return (np.nan, np.nan)
    wk = pd.to_datetime(df.date).dt.strftime('%G-%V'); grp = {k: g for k, g in df.assign(net=df.ret - COST).groupby(wk)}
    keys = list(grp); out = []
    sums = np.array([[g.net.sum(), g.days.sum(), len(g)] for g in grp.values()])
    for _ in range(B):
        s = sums[rng.integers(0, len(keys), len(keys))].sum(0); out.append((s[0] / s[2]) / (s[1] / s[2]))
    return tuple(np.percentile(out, [5, 95]))

def table(T, title, by=None):
    print('\n=== ' + title)
    out = []
    for key, g in (T.groupby(['lane'] + ([by] if by else []))):
        s = stat(g); lo, hi = boot(g); k = key if isinstance(key, tuple) else (key,)
        out.append(dict(zip(['lane'] + ([by] if by else []), k)) | s | dict(ci90=f'[{lo:.2f},{hi:.2f}]'))
    R = pd.DataFrame(out)
    R = R.sort_values(([by] if by else []) + ['rd'], ascending=[True] * bool(by) + [False])
    pd.set_option('display.width', 250)
    print(R.round(3).to_string(index=False))
    return R

def main():
    P = pd.read_pickle(os.environ['PICKS']).drop_duplicates(['lane', 'ticker', 'date'])
    rows = lane_rows(P) + bull_rows()
    T = pd.DataFrame(rows)
    T['hyb'] = T.date.map(lambda d: bool(J.hyb.get(pd.Timestamp(d), True)))
    T['reg'] = T.date.map(lambda d: J.reg.get(pd.Timestamp(d), 'n/a'))
    common = T[(T.close >= 100) & (T.liq >= 1e9) & (~T.ticker.isin(HSC))]
    # production configuration: BULL and BOW are behind the hybrid gate, the rest are not
    prod = lambda D: D[~(D.lane.isin(['BULL', 'BOW']) & ~D.hyb)]
    for name, D in (('native universe (as the lane fires)', T), ('COMMON universe (close>=100, value>=Rp1bn, not HSC)', common)):
        for wn, (a, b) in (('FULL ' + '..'.join(WIN), WIN), ('TEST ' + '..'.join(TEST), TEST)):
            W = D[(D.date >= a) & (D.date <= b)]
            table(dedup(W), f'{name} | {wn} | UNGATED all lanes')
            table(dedup(prod(W)), f'{name} | {wn} | PRODUCTION (BULL+BOW behind hybrid gate)')
    C = dedup(prod(common[(common.date >= WIN[0]) & (common.date <= WIN[1])]))
    R = table(C, 'COMMON universe, FULL window, production config, by SHORT IHSG regime (r10 tertile)', by='reg')
    C.to_pickle(os.environ.get('OUT', 'lane_compare_trades.pkl'))
    # dynamic test: pick best lane per regime on train, apply on test
    tr, te = C[C.date < SPLIT], C[C.date >= SPLIT]
    best = {}
    for reg, g in tr.groupby('reg'):
        cand = {l: stat(x) for l, x in g.groupby('lane') if len(x) >= 15}
        if cand: best[reg] = max(cand, key=lambda l: cand[l]['rd'])
    print('\n=== dynamic switch test: best lane per regime chosen on <', SPLIT, '->', best)
    sw = pd.concat([te[(te.reg == r) & (te.lane == l)] for r, l in best.items()]) if best else te.iloc[:0]
    print('switched  :', {k: round(v, 3) for k, v in stat(sw).items()})
    for l, g in te.groupby('lane'): print(f'static {l:6s}:', {k: round(v, 3) for k, v in stat(g).items()})
    print('\nalerts/day by lane and regime (common, production):')
    ndays = J.loc[WIN[0]:WIN[1]].groupby('reg').size()
    print((C.groupby(['lane', 'reg']).size().unstack().div(ndays, axis=1)).round(2).to_string())
    print('\nIHSG days per regime:', ndays.to_dict(), '| thresholds', round(q1, 2), round(q2, 2))

if __name__ == '__main__':
    main()
