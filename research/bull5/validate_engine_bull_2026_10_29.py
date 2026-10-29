"""Validate engine/bull.py against research/bull5: (1) raw/pick flags row-by-row vs ds10.pkl rules, (2) simulate_exit vs bull5 trade() on every confirm3 signal.
Run: PYTHONUTF8=1 PYTHONPATH=. python research/bull5/validate_engine_bull_2026_10_29.py"""
import sqlite3, sys
import numpy as np, pandas as pd
sys.path.insert(0, '.')
import engine.bull as bull

ds = pd.read_pickle('research/bull5/ds10.pkl')
ds = ds[(ds.liq >= 1) & (ds.close >= 100) & (~ds.hsc)].dropna(subset=['r_sl5', 'sma50', 'hi60', 'atr14', 'rsi', 'volr', 'pctb']).sort_values(['ticker', 'date']).reset_index(drop=True)
A = (ds.ret3 >= 5.7) & (ds.gap <= -0.93) & (ds.cpos <= 0.941); B = (ds.ret10 >= 12) & (ds.d5 >= -1.15) & (ds.d50 <= 4.58) & (ds.rng_pct <= 3.85)
T = (ds.slope20 >= 7.09) & (ds.atr14 <= 5.83); S1 = T & (ds.atrratio <= 0.971) & (ds.cpos >= 0.25); S2 = T & (ds.body >= 0.704)
ds['raw'] = S1 | S2 | A | B
ds['p3'] = ds.groupby('ticker')['raw'].transform(lambda s: s.astype(int).shift(1).rolling(3, min_periods=1).sum()).fillna(0)
ds['pick_r'] = ds.raw & (ds.p3 >= 1)

con = sqlite3.connect('mbss_ohlcv.db')
px = pd.read_sql("select ticker,date,open,high,low,close,volume from ohlcv_daily where date<='2026-10-20' order by ticker,date", con, parse_dates=['date'])
rows, ex = [], {}
for tk, g in px.groupby('ticker'):
    if len(g) < bull.MIN_HISTORY: continue
    d = g.set_index('date').rename(columns=str.capitalize)
    f = bull.features(d); s = bull.raw_signals(f)
    rows.append(pd.DataFrame({'ticker': tk, 'date': f.date.values, 'raw_e': s.raw.values, 'pick_e': s.pick.values, 'liq_e': f.liq.values, 'close_e': f.close.values}))
    ex[tk] = (d.index.values, d.Open.to_numpy(float), d.High.to_numpy(float), d.Low.to_numpy(float), d.Close.to_numpy(float))
E = pd.concat(rows)
M = ds[['ticker', 'date', 'raw', 'pick_r']].merge(E, on=['ticker', 'date'], how='left')
print('research rows', len(M), 'missing in engine', int(M.raw_e.isna().sum()))
M = M.dropna(subset=['raw_e'])
print('raw mismatch rows:', int((M.raw != M.raw_e).sum()), 'of', len(M), '| raw true research', int(M.raw.sum()), 'engine', int(M.raw_e.sum()))
print('pick mismatch rows:', int((M.pick_r != M.pick_e).sum()), '| pick true research', int(M.pick_r.sum()), 'engine', int(M.pick_e.sum()))

# (2) exit simulation vs bull5 trade() on every research pick
src = open('research/bull5/portfolio_final_2026_10_28.py').read().split('sig=ds[ds["any"]')[0]
g = {'__name__': 'v'}; exec(src, g)
trade = g['trade']; diff = 0; n = 0; mx = 0.0
for r in M[M.pick_r].itertuples():
    t = trade(r.ticker, r.date, .20, .08, .08, .06)
    if t is None or t['st'] != 'closed': continue
    dt, o, h, l, c = ex[r.ticker]; i = int(np.searchsorted(dt, np.datetime64(r.date)))
    s = bull.simulate_exit(o, h, l, c, i)
    if s is None or s['status'] == 'ALIVE': diff += 1; continue
    n += 1; d = abs(s['ret_pct'] - (t['x'] / t['e'] - 1) * 100); mx = max(mx, d); diff += d > 1e-6
print(f'exit sim: compared {n} closed picks, mismatches {diff}, max |ret diff| {mx:.8f}')
