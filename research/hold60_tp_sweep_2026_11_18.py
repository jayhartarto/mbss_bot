"""research/hold60_tp_sweep_2026_11_18.py -- pick OSB-v2 / BOW, tahan maks 60 hari, keluar saat TP tersentuh (10/20/30%), tanpa SL.
Entry open T+1. TP dicek pakai HIGH tiap hari (gap-aware: kalau open >= TP, fill di open). Kalau TP tak tersentuh dalam 60 hari -> close hari ke-60 (timeout).
Biaya 0.4% round-trip. 1 posisi/ticker/lane. Hanya trade yang horizon 60 harinya sudah lengkap (jadi TP-vs-timeout tidak bias).
Run: PYTHONUTF8=1 python research/hold60_tp_sweep_2026_11_18.py
"""
import sqlite3, numpy as np, pandas as pd

COST = 0.4
LAST = pd.Timestamp('2026-11-16')
TPS = (10, 20, 30)
rng = np.random.default_rng(11)

px = pd.read_sql_query("SELECT ticker,date,open,high,low,close,volume FROM ohlcv_daily ORDER BY ticker,date",
                       sqlite3.connect('mbss_ohlcv.db'), parse_dates=['date'])
A = {}
for t, g in px.groupby('ticker'):
    A[t] = dict(d=g.date.values, o=g.open.to_numpy(float), h=g.high.to_numpy(float), l=g.low.to_numpy(float), c=g.close.to_numpy(float))

def window(t, sig_date, mode):
    a = A.get(t)
    if a is None: return None
    i = int(np.searchsorted(a['d'], np.datetime64(sig_date)))
    if i >= len(a['d']) or a['d'][i] != np.datetime64(sig_date): return None
    e = i + 1
    if e >= len(a['d']) or not a['o'][e] > 0: return None
    if mode == 'cal':
        end = pd.Timestamp(a['d'][e]) + pd.Timedelta(days=60)
        if end > LAST: return None
        j = int(np.searchsorted(a['d'], np.datetime64(end), side='right')) - 1
    else:
        j = e + 59
        if j >= len(a['d']): return None
    return a, e, j

def sim_tp(a, e, j, tp):
    ep = a['o'][e]; tpp = ep * (1 + tp / 100)
    for k in range(e, j + 1):
        if a['h'][k] >= tpp:
            fill = max(a['o'][k], tpp)       # gap-up fill at open
            return (fill / ep - 1) * 100 - COST, k - e + 1, True
    return (a['c'][j] / ep - 1) * 100 - COST, j - e + 1, False

def run(mode):
    print("=" * 120)
    print("MAKS 60 HARI", "KALENDER (~42 hari bursa)" if mode == 'cal' else "BURSA (~85 hari kalender)", "| TP touch (gap-aware) | tanpa SL | biaya 0.4%")
    EP = pd.read_pickle('research/bull5/lane_episodes_open.pkl')
    for lane, key in (('OSB-v2', 'OSBv2'), ('BOW', 'BOW')):
        sig = EP[key][['ticker', 'date']].drop_duplicates().sort_values(['date', 'ticker'])
        for tp in TPS:
            rows, busy = [], {}
            for r in sig.itertuples():
                if r.ticker in busy and r.date <= busy[r.ticker]: continue
                w = window(r.ticker, r.date, mode)
                if w is None: continue
                a, e, j = w
                ret, days, hit = sim_tp(a, e, j, tp)
                busy[r.ticker] = pd.Timestamp(a['d'][min(e + days - 1, len(a['d']) - 1)])
                rows.append(dict(sig=r.date, entry=pd.Timestamp(a['d'][e]), ret=ret, days=days, hit=hit,
                                 mae=(a['l'][e:e + days].min() / a['o'][e] - 1) * 100))
            df = pd.DataFrame(rows)
            if df.empty: continue
            wk = df.entry.dt.to_period('W').astype(str)
            groups = [g.ret.to_numpy() for _, g in df.groupby(wk)]
            bm = [np.concatenate([groups[k] for k in rng.integers(0, len(groups), len(groups))]).mean() for _ in range(2000)]
            lo, hi = np.percentile(bm, [5, 95])
            h, to = df[df.hit], df[~df.hit]
            print(f"\n{lane} TP{tp}%: n={len(df)} ({df.sig.min().date()}..{df.sig.max().date()})")
            print(f"  TP tercapai: {100*df.hit.mean():5.1f}% | hari ke-TP median={h.days.median() if len(h) else float('nan'):.0f} p75={h.days.quantile(.75) if len(h) else float('nan'):.0f}")
            print(f"  semua trade: mean={df.ret.mean():+6.2f}% median={df.ret.median():+6.2f}% CI90=[{lo:+.1f},{hi:+.1f}] win(>0)={100*(df.ret>0).mean():5.1f}% mean hold={df.days.mean():.1f}d  ret/hari={df.ret.mean()/df.days.mean():+.3f}%")
            print(f"  yang timeout (n={len(to)}): mean={to.ret.mean() if len(to) else float('nan'):+6.2f}% median={to.ret.median() if len(to) else float('nan'):+6.2f}% p10={to.ret.quantile(.1) if len(to) else float('nan'):+6.1f} | MAE median semua={df.mae.median():+5.1f}% <=-15:{100*(df.mae<=-15).mean():4.1f}% <=-25:{100*(df.mae<=-25).mean():4.1f}%")
            df['per'] = df.sig.dt.to_period('Q').astype(str)
            print("  per kuartal (n / TP hit% / mean%): " + " | ".join(f"{p} {len(g)}/{100*g.hit.mean():.0f}%/{g.ret.mean():+.1f}" for p, g in df.groupby('per')))

for m in ('cal', 'trd'):
    run(m)
