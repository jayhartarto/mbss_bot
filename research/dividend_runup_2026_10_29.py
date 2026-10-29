"""research/dividend_runup_2026_10_29.py -- is there a tradable run-up into the cum-dividend date?
Events: yfinance dividend history (ex-dates) of the live ISSI whitelist (ticker_whitelist.json) minus HSC.  cum day c = the last trading day BEFORE the ex-date (the ex-date price drop is never held).
Trade: buy at the OPEN of the session h sessions before the cum close (entry session = c-h+1), sell at the CLOSE of the cum day c.  h in (3,5,7,10,15) (h<=7 is the realistic range: cum dates are announced
only ~1-2 weeks ahead; larger h assumes foresight and is an UPPER bound).  Variants: plain hold, and SL -8% (gap-aware, intraday low).  Cost 0.4% round trip.
Excess = trade return minus the equal-weight mean open->close return of the liquid universe over the same sessions (market direction cancels).
Universe: close>=100, 20d mean value>=Rp1bn at entry.  Events with ex-date 2023-04-01..2026-10-13.  Week-cluster bootstrap.
Breakdowns: dividend yield, run-up already done (return over the 10 sessions before entry: 'belum naik' test), hybrid IHSG gate state, IHSG short-term state, year.
Run: PYTHONUTF8=1 PYTHONPATH=. python research/dividend_runup_2026_10_29.py"""
import os, sqlite3, sys, json, warnings, pickle
import numpy as np, pandas as pd
warnings.filterwarnings('ignore')
sys.path.insert(0, os.getcwd())
from concurrent.futures import ThreadPoolExecutor
import yfinance as yf

COST = 0.4
EV_START, EV_END, SPLIT = '2023-04-01', '2026-10-13', '2025-07-01'
HS = (3, 5, 7, 10, 15)
CACHE = os.environ.get('DIVCACHE', 'dividends_yf.pkl')
rng = np.random.default_rng(21)

WL = json.load(open('ticker_whitelist.json'))['eligible_tickers']
HSC = set(json.load(open('hsc_list.json'))) if os.path.exists('hsc_list.json') else set()
if HSC and isinstance(next(iter(HSC)), dict): HSC = set()
WL = [t for t in WL if t not in HSC]

if os.path.exists(CACHE):
    DIV = pickle.load(open(CACHE, 'rb'))
else:
    def get(t):
        try:
            d = yf.Ticker(t + '.JK').dividends
            return t, [(x.tz_localize(None).normalize() if x.tzinfo is None else x.tz_convert('Asia/Jakarta').tz_localize(None).normalize(), float(v)) for x, v in d.items()]
        except Exception as e:
            return t, None
    with ThreadPoolExecutor(5) as ex: DIV = dict(ex.map(get, WL))
    pickle.dump(DIV, open(CACHE, 'wb'))
print('tickers with dividend history', sum(1 for v in DIV.values() if v), 'of', len(WL), '| failed', sum(1 for v in DIV.values() if v is None))

px = pd.read_sql_query("SELECT ticker,date,open,high,low,close,volume FROM ohlcv_daily WHERE date<='2026-10-28' ORDER BY ticker,date", sqlite3.connect('mbss_ohlcv.db'), parse_dates=['date'])
A = {t: dict(d=g.date.values, o=g.open.to_numpy(float), h=g.high.to_numpy(float), l=g.low.to_numpy(float), c=g.close.to_numpy(float),
             liq=(g.close * g.volume).rolling(20).mean().to_numpy(float)) for t, g in px.groupby('ticker')}

# universe equal-weight benchmark per (entry date, h): mean of open[e] -> close[e+h-1] over liquid tickers
BM = {}
for h in HS:
    rec = []
    for t, a in A.items():
        n = len(a['c'])
        if n < h + 30: continue
        e = np.arange(0, n - h + 1); ok = (a['liq'][e] >= 1e9) & (a['c'][e] >= 100) & (a['o'][e] > 0)
        r = (a['c'][e + h - 1] / a['o'][e] - 1) * 100
        rec.append(pd.DataFrame({'date': a['d'][e][ok], 'r': r[ok]}))
    B = pd.concat(rec).groupby('date').r.mean(); BM[h] = B

J = pd.read_pickle('research/bull5/J2.pkl'); J.index = pd.to_datetime(J.index)
J['hyb'] = (J.d50 >= -1.1) | ((J.above10low >= 4) & (J.d5 >= 0))

rows = []
for t, ev in DIV.items():
    if not ev or t not in A: continue
    a = A[t]; d = a['d']
    for ex, amt in ev:
        if not (pd.Timestamp(EV_START) <= ex <= pd.Timestamp(EV_END)) or amt <= 0: continue
        c = int(np.searchsorted(d, np.datetime64(ex))) - 1          # last session before the ex-date
        if c < 30 or c >= len(d) or (ex - pd.Timestamp(d[c])).days > 6: continue   # gap > 6 days = missing data / suspension
        for h in HS:
            e = c - h + 1
            if e < 12 or not (a['o'][e] > 0) or a['liq'][e] < 1e9 or a['c'][e] < 100: continue
            ep = a['o'][e]; stop = ep * 0.92; r_plain = (a['c'][c] / ep - 1) * 100; r_sl = r_plain
            for j in range(e, c + 1):
                if a['l'][j] <= stop: r_sl = (min(a['o'][j], stop) / ep - 1) * 100; break
            bm = BM[h].get(pd.Timestamp(d[e]), np.nan)
            rows.append(dict(ticker=t, h=h, cum=pd.Timestamp(d[c]), entry=pd.Timestamp(d[e]), amt=amt, yld=amt / a['c'][e - 1] * 100, pre10=(a['c'][e - 1] / a['c'][e - 11] - 1) * 100,
                             ret=r_plain, ret_sl=r_sl, bm=bm, exc=r_plain - bm, gate=bool(J.hyb.get(pd.Timestamp(d[e]), True)), idd=J.dd10.get(pd.Timestamp(d[e]), np.nan)))
E = pd.DataFrame(rows).dropna(subset=['bm'])
print('events (ticker x dividend) at h=7:', int((E.h == 7).sum()), '| h=3:', int((E.h == 3).sum()))
print('sanity: median dividend yield %', round(E[E.h == 7].yld.median(), 2))

def boot(x, h, col, B=1500):
    if len(x) < 15: return (np.nan, np.nan)
    wk = x.entry.dt.strftime('%G-%V'); T = pd.DataFrame({'n': x[col] - COST, 'c': 1, 'w': wk}).groupby('w').sum().to_numpy(); out = []
    for _ in range(B):
        s = T[rng.integers(0, len(T), len(T))].sum(0); out.append(s[0] / s[1] / h)
    return tuple(np.percentile(out, [5, 95]))

def S(x, h, col='ret'):
    if len(x) == 0: return dict(n=0)
    net = x[col] - COST; lo, hi = boot(x, h, col)
    return dict(n=len(x), win=round(100 * (net > 0).mean(), 1), mean_n=round(net.mean(), 2), exc=round(x.exc.mean(), 2) if col == 'ret' else round((x[col] - x.bm).mean(), 2),
                rd=round(net.mean() / h, 3), ci90=f'[{lo:.2f},{hi:.2f}]', worst=round(x[col].min(), 1))

pd.set_option('display.width', 250)
print('\n=== utama: beli open h sesi sebelum cum, jual close hari cum (ret/day = mean net / h)')
tab = []
for h in HS:
    x = E[E.h == h]
    tab.append({'h': h, 'var': 'tanpa SL'} | S(x, h)); tab.append({'h': h, 'var': 'SL-8%'} | S(x, h, 'ret_sl'))
    tab.append({'h': h, 'var': 'tanpa SL, test Jul25+'} | S(x[x.entry >= SPLIT], h))
print(pd.DataFrame(tab).to_string(index=False))
for h in (5, 7):
    x = E[E.h == h]
    print(f'\n=== breakdown h={h} (tanpa SL)')
    cuts = {'yield': pd.cut(x.yld, [0, 1, 2, 4, 100], labels=['<1%', '1-2%', '2-4%', '>4%']),
            'sudah naik 10 sesi sebelum entry': pd.cut(x.pre10, [-100, 0, 5, 12, 1000], labels=['turun', '0-5%', '5-12%', '>12%']),
            'gate IHSG': x.gate.map({True: 'terbuka', False: 'TERTUTUP'}),
            'IHSG dd10': pd.cut(x.idd, [-100, -4, -2, 0.01], labels=['<=-4', '-4..-2', '>-2']),
            'tahun': x.entry.dt.year}
    for nm, k in cuts.items():
        r = [{nm: str(g)} | S(x[k == g], h) for g in sorted(k.dropna().unique(), key=str)]
        print(pd.DataFrame(r).to_string(index=False)); print()
E.to_pickle(os.environ.get('OUT', 'dividend_events.pkl'))
