"""research/osb_widen_oversold_2026_10_29.py -- can the OSB-v2 "oversold before the bounce" definition be widened beyond IHSG dd_100<=-10% & phase!=BEARISH_GRIND?
Stock-level OSB-v2 trigger WITHOUT any IHSG gate (same as engine.oversold_bounce_v2.evaluate_ticker minus the FF exclusion): RSI14 in [20,40), close >=10% below SMA20,
20d mean value >= Rp1bn, green candle, stock 1d return <= IHSG 1d return.  Extra: close>=100, not HSC.  Exit = production OSB-v2 (TP10, SL8, BE+5 -> entry+0.2%, 8d), buy at next open,
gap-aware, 0.4% cost, one open position per ticker, ret/day = mean net / mean days.  Window 2023-09-01..2026-10-13 (needs 100d of IHSG history for dd_100).
Candidate widenings use only SHORT IHSG state (dd10/dd20/r5) and/or stock-only depth.  Uncertainty = week-cluster bootstrap; also per-year ret/day and the share of picks in the busiest week.
Run: PYTHONUTF8=1 PYTHONPATH=. python research/osb_widen_oversold_2026_10_29.py"""
import os, sqlite3, sys, json, warnings
import numpy as np, pandas as pd
warnings.filterwarnings('ignore')
sys.path.insert(0, os.getcwd())
from engine import legacy_core as core
import engine.ihsg_regime as reg

COST, START, END = 0.4, '2023-09-01', '2026-10-13'
SL, TP, BE, H = 8.0, 10.0, 5.0, 8
rng = np.random.default_rng(11)

j = pd.read_csv('research/bull5/glob/JKSE.csv', index_col=0, parse_dates=True)
jc = j.Close.dropna()
I = pd.DataFrame({'ihsg': jc})
I['r1'] = jc.pct_change() * 100; I['r5'] = jc.pct_change(5) * 100
I['dd10'] = (jc / jc.rolling(10).max() - 1) * 100; I['dd20'] = (jc / jc.rolling(20).max() - 1) * 100
R = reg._build_regime(pd.DataFrame({'Close': jc.values}, index=jc.index)).set_index('date')
I['dd100'] = R.dd_100 * 100; I['phase'] = R.regime_phase
I.index = pd.to_datetime(I.index)
print('IHSG table', I.index.min().date(), I.index.max().date(), 'days in window', int(((I.index >= START) & (I.index <= END)).sum()))

px = pd.read_sql_query("SELECT ticker,date,open,high,low,close,volume FROM ohlcv_daily WHERE date<='2026-10-28' ORDER BY ticker,date", sqlite3.connect('mbss_ohlcv.db'), parse_dates=['date'])
HSC = set(json.load(open('hsc_list.json'))) if os.path.exists('hsc_list.json') else set()
if HSC and isinstance(next(iter(HSC)), dict): HSC = set()
A, rows = {}, []
for t, g in px.groupby('ticker'):
    if t in HSC or len(g) < 60: continue
    g = g.set_index('date'); c, o, h, l, v = (g[k].astype(float) for k in ('close', 'open', 'high', 'low', 'volume'))
    A[t] = (g.index.values, o.to_numpy(), h.to_numpy(), l.to_numpy(), c.to_numpy())
    f = pd.DataFrame({'ticker': t, 'date': g.index, 'close': c.values})
    f['rsi'] = core.calculate_rsi(c).values; f['dist20'] = ((c / c.rolling(20).mean() - 1) * 100).values
    f['liq'] = (c * v).rolling(20).mean().values; f['green'] = (c > o).values; f['ret1'] = (c.pct_change() * 100).values
    f['ret5'] = (c.pct_change(5) * 100).values; f['dd20s'] = ((c / c.rolling(20).max() - 1) * 100).values
    rows.append(f)
F = pd.concat(rows)
F = F.merge(I, left_on='date', right_index=True, how='inner')
F = F[(F.date >= START) & (F.date <= END)].reset_index(drop=True)   # merge keeps the left (per-ticker concat) index -> non-unique; dedup uses .loc
base = F[(F.rsi >= 20) & (F.rsi < 40) & (F.dist20 <= -10) & (F.liq >= 1e9) & F.green & (F.ret1 <= F.r1) & (F.close >= 100)].copy()
print('base stock-level signals (no IHSG gate):', len(base), '| signal days', base.date.nunique(), 'of', int(((I.index >= START) & (I.index <= END)).sum()))

def sim(t, d):
    dt, o, h, l, c = A[t]; i = int(np.searchsorted(dt, np.datetime64(d)))
    if i + 1 + H > len(c): return None
    e = i + 1; ep = o[e]
    if not ep > 0: return None
    stop = ep * (1 - SL / 100); armed = False
    for k in range(H):
        jx = e + k
        if armed: stop = max(stop, ep * 1.002)
        if l[jx] <= stop: return (min(o[jx], stop) / ep - 1) * 100, k + 1, e, jx
        tp = ep * (1 + TP / 100)
        if h[jx] >= tp: return (tp / ep - 1) * 100, k + 1, e, jx
        if h[jx] >= ep * (1 + BE / 100): armed = True
    jx = e + H - 1
    return (c[jx] / ep - 1) * 100, H, e, jx

res = {}
for r in base.itertuples():
    s = sim(r.ticker, r.date)
    res[(r.ticker, r.date)] = s
base['s'] = [res[(t, d)] for t, d in zip(base.ticker, base.date)]
base = base[base.s.notna()].copy()
base[['ret', 'days', 'e', 'x']] = pd.DataFrame(base.s.tolist(), index=base.index)
base['date_s'] = base.date.dt.strftime('%Y-%m-%d')

def dedup(df):
    keep, busy = [], {}
    for r in df.sort_values(['date', 'ticker']).itertuples():
        if r.ticker in busy and r.e <= busy[r.ticker]: continue
        keep.append(r.Index); busy[r.ticker] = r.x
    return df.loc[keep]

def boot(df, B=1500):
    if len(df) < 15: return (np.nan, np.nan)
    wk = df.date.dt.strftime('%G-%V'); net = df.ret - COST
    S = pd.DataFrame({'n': net, 'd': df.days, 'c': 1, 'w': wk.values}).groupby('w').sum().to_numpy()
    out = []
    for _ in range(B):
        s = S[rng.integers(0, len(S), len(S))].sum(0); out.append((s[0] / s[2]) / (s[1] / s[2]))
    return tuple(np.percentile(out, [5, 95]))

NDAYS = int(((I.index >= START) & (I.index <= END)).sum())
def summ(name, m):
    d = dedup(base[m]); net = d.ret - COST
    if len(d) == 0: return dict(defn=name, n=0)
    lo, hi = boot(d); rd = net.mean() / d.days.mean()
    yr = {y: (lambda x: round((x.ret - COST).mean() / x.days.mean(), 2) if len(x) >= 10 else np.nan)(d[d.date.dt.year == y]) for y in (2023, 2024, 2025, 2026)}
    wk = d.date.dt.strftime('%G-%V').value_counts()
    return dict(defn=name, n=len(d), per_day=round(len(d) / NDAYS, 2), days_with=round(100 * d.date.nunique() / NDAYS), win=round(100 * (net > 0).mean(), 1), mean_n=round(net.mean(), 2),
                hold=round(d.days.mean(), 1), rd=round(rd, 3), ci90=f'[{lo:.2f},{hi:.2f}]', y23=yr[2023], y24=yr[2024], y25=yr[2025], y26=yr[2026], topwk=round(100 * wk.iloc[0] / len(d)), worst=round(d.ret.min(), 1))

b = base
P0 = (b.dd100 <= -10) & (b.phase != 'BEARISH_GRIND')
D = {
    'U0 tanpa syarat IHSG (stock-only)': b.ret.notna(),
    'P0 produksi (dd100<=-10 & bukan GRIND)': P0,
    'P0 sisanya (di luar produksi)': ~P0,
    'W1 IHSG dd10<=-4': b.dd10 <= -4,
    'W1b IHSG dd10<=-6': b.dd10 <= -6,
    'W2 IHSG dd20<=-6': b.dd20 <= -6,
    'W2b IHSG dd20<=-9': b.dd20 <= -9,
    'W3 IHSG r5<=-3': b.r5 <= -3,
    'W4 P0 atau dd10<=-4': P0 | (b.dd10 <= -4),
    'W5 P0 atau dd20<=-6': P0 | (b.dd20 <= -6),
    'IHSG tenang: dd10>-2': b.dd10 > -2,
    'IHSG tenang & saham dist20<=-15': (b.dd10 > -2) & (b.dist20 <= -15),
    'IHSG tenang & saham dist20<=-20': (b.dd10 > -2) & (b.dist20 <= -20),
    'IHSG tenang & dist20<=-15 & RSI<30': (b.dd10 > -2) & (b.dist20 <= -15) & (b.rsi < 30),
    'S1 stock-only dist20<=-15 (apa pun IHSG)': b.dist20 <= -15,
    'S2 stock-only dist20<=-20 (apa pun IHSG)': b.dist20 <= -20,
    'S3 stock-only dist20<=-15 & RSI<30': (b.dist20 <= -15) & (b.rsi < 30),
    'S4 P0 atau stock-only dist20<=-20': P0 | (b.dist20 <= -20),
    'S5 P0 atau (dist20<=-15 & RSI<30)': P0 | ((b.dist20 <= -15) & (b.rsi < 30)),
}
out = pd.DataFrame([summ(k, m) for k, m in D.items()])
pd.set_option('display.width', 300); pd.set_option('display.max_colwidth', 50)
print(out.to_string(index=False))
# gradient: base signal by IHSG dd10 bucket
bb = pd.cut(base.dd10, [-100, -8, -4, -2, 0.01], labels=['<=-8', '-8..-4', '-4..-2', '>-2'])
print('\nbase signal by IHSG dd10 bucket (deduped inside each bucket)')
print(pd.DataFrame([summ(f'dd10 {k}', (bb == k).to_numpy()) for k in ['<=-8', '-8..-4', '-4..-2', '>-2']]).to_string(index=False))
base.to_pickle(os.environ.get('OUT', 'osb_widen_base.pkl'))
