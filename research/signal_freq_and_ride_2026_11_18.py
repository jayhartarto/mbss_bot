"""research/signal_freq_and_ride_2026_11_18.py -- (1) frekuensi sinyal OSB-v2/BOW terfilter per bulan & regime (ada periode kosong?);
(2) hold TP10/12 vs ride trailing (arm +A%, trail Y% dari high berjalan, tanpa cap, tanpa SL, H=90) + 2-tahap (separuh TP10, separuh trailing).
Run: PYTHONUTF8=1 python research/signal_freq_and_ride_2026_11_18.py"""
import sqlite3, numpy as np, pandas as pd, warnings
warnings.filterwarnings('ignore')
COST, H = 0.4, 90
R = pd.read_pickle('research/.tmp_ihsg_regime_full.pkl'); R.index = pd.to_datetime(R.index).normalize()
print("kolom regime:", list(R.columns)[:12])
px = pd.read_sql_query("SELECT ticker,date,open,high,low,close FROM ohlcv_daily ORDER BY ticker,date", sqlite3.connect('mbss_ohlcv.db'), parse_dates=['date'])
EP = pd.read_pickle('research/bull5/lane_episodes_open.pkl')
sigs = pd.concat([EP['OSBv2'].assign(lane='OSB-v2'), EP['BOW'].assign(lane='BOW')])[['ticker', 'date', 'lane']].drop_duplicates()
sigs['date'] = pd.to_datetime(sigs.date)
A = {}
for t, g in px[px.ticker.isin(sigs.ticker.unique())].groupby('ticker'):
    c = g.close.astype(float); d = c.diff(); up = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean(); dn = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    A[t] = dict(d=g.date.values, o=g.open.to_numpy(float), h=g.high.to_numpy(float), l=g.low.to_numpy(float), c=c.to_numpy(),
                rsi=(100 - 100 / (1 + up / dn.replace(0, np.nan))).to_numpy(), dd60=(c / c.rolling(60).max() - 1).to_numpy() * 100)
phase_col = next((k for k in R.columns if 'phase' in k.lower() or 'regime' in k.lower()), None)

def ride(a, e, ep, arm, trail):
    hh, ll, oo = a['h'][e:e + H], a['l'][e:e + H], a['o'][e:e + H]
    run = ep; armed = False
    for k in range(H):
        if armed:
            stop = run * (1 - trail / 100)
            if ll[k] <= stop: return (min(oo[k], stop) / ep - 1) * 100 - COST, k + 1
        run = max(run, hh[k])
        if not armed and run >= ep * (1 + arm / 100): armed = True
    return (a['c'][e + H - 1] / ep - 1) * 100 - COST, H

rows = []
for lane, g in sigs.sort_values(['date', 'ticker']).groupby('lane'):
    busy = {}
    for r in g.itertuples():
        a = A.get(r.ticker)
        if a is None or (r.ticker in busy and r.date <= busy[r.ticker]): continue
        i = int(np.searchsorted(a['d'], np.datetime64(r.date)))
        if i >= len(a['d']) or a['d'][i] != np.datetime64(r.date): continue
        busy[r.ticker] = r.date + pd.Timedelta(days=int(H * 1.45))
        row = dict(lane=lane, ticker=r.ticker, date=r.date, filt=bool(a['dd60'][i] <= -20 and a['rsi'][i] >= 31),
                   phase=(R.loc[:r.date].iloc[-1][phase_col] if phase_col else 'NA'))
        if i + 1 + H <= len(a['d']) and a['o'][i + 1] > 0:
            e = i + 1; ep = a['o'][e]; hh, oo = a['h'][e:e + H], a['o'][e:e + H]
            row['complete'] = True
            for tp in (10, 12):
                lv = ep * (1 + tp / 100); hit = np.nonzero(hh >= lv)[0]
                row[f'tp{tp}'] = ((max(oo[hit[0]], lv) if len(hit) else a['c'][e + H - 1]) / ep - 1) * 100 - COST
            for arm in (10, 15):
                for tr in (5, 8, 10):
                    row[f'rd{arm}_{tr}'], row[f'rdd{arm}_{tr}'] = ride(a, e, ep, arm, tr)
        else: row['complete'] = False
        rows.append(row)
E = pd.DataFrame(rows)
E.to_csv('research/signal_freq_and_ride_2026_11_18_episodes.csv', index=False)
F = E[E.filt].copy()

print("\n" + "=" * 120 + "\n(1) FREKUENSI SINYAL TERFILTER (dd60<=-20 & RSI>=31; dedup 1 posisi/ticker per lane)\n" + "=" * 120)
print(f"episode total {len(E)}, terfilter {len(F)}, rentang {F.date.min().date()}..{F.date.max().date()}")
F['ym'] = F.date.dt.to_period('M')
allm = pd.period_range(F.ym.min(), F.ym.max(), freq='M')
tab = F.groupby(['ym', 'lane']).size().unstack(fill_value=0).reindex(allm, fill_value=0)
print(tab.assign(total=tab.sum(axis=1)).to_string())
print(f"\nbulan tanpa sinyal terfilter: {int((tab.sum(axis=1)==0).sum())} dari {len(allm)}")
# hari kosong: gap antar tanggal sinyal
ds = np.sort(F.date.unique()); gaps = pd.Series(np.diff(ds).astype('timedelta64[D]').astype(int), index=pd.to_datetime(ds[1:]))
print("gap terpanjang antar tanggal sinyal (hari kalender):"); print(gaps.sort_values(ascending=False).head(6).to_string())
print(f"hari bursa unik dgn >=1 sinyal: {len(ds)}; median gap {gaps.median():.0f} hr")
print("\nPer fase regime IHSG (terfilter): n sinyal, share, jumlah hari-bursa fase itu, sinyal per 20 hari bursa")
Rr = R.copy(); Rr['phase'] = Rr[phase_col] if phase_col else 'NA'
Rr = Rr[(Rr.index >= F.date.min()) & (Rr.index <= F.date.max())]
for ph, g in F.groupby('phase'):
    nd = int((Rr.phase == ph).sum())
    print(f"  {str(ph):22s} n={len(g):4d} ({100*len(g)/len(F):4.1f}%) hari fase={nd:4d} -> {20*len(g)/max(nd,1):5.1f} sinyal/20hr")
print("\nPer lane x fase:"); print(F.groupby(['lane', 'phase']).size().unstack(fill_value=0).to_string())

print("\n" + "=" * 120 + "\n(2) RIDE TRAILING vs HOLD TP (terfilter, window H=90 lengkap, tanpa SL, biaya 0.4%)\n" + "=" * 120)
C = F[F.complete].copy(); cut = C.date.median()
print(f"n={len(C)}, paruh1={int((C.date<cut).sum())}, paruh2={int((C.date>=cut).sum())}")
def line(name, x, d=None):
    p1, p2 = x[C.date < cut].mean(), x[C.date >= cut].mean()
    print(f"  {name:28s} win={100*(x>0).mean():5.1f}% mean={x.mean():+6.2f} med={x.median():+6.2f} p10={x.quantile(.1):+6.1f} worst={x.min():+6.1f} | paruh1/2 {p1:+6.2f}/{p2:+6.2f}" + (f" | hari={d.mean():4.0f} ret/hr={x.mean()/d.mean():+.3f}" if d is not None else ''))
line('hold TP10', C.tp10); line('hold TP12', C.tp12)
for arm in (10, 15):
    for tr in (5, 8, 10):
        line(f'ride arm{arm}/trail{tr}', C[f'rd{arm}_{tr}'], C[f'rdd{arm}_{tr}'])
print("  -- 2 tahap: separuh TP10 + separuh ride --")
for arm in (10, 15):
    for tr in (5, 8, 10):
        line(f'50% TP10 + 50% ride{arm}/{tr}', (C.tp10 + C[f'rd{arm}_{tr}']) / 2)
print("\nPer lane (ride arm10/trail8 vs TP10):")
for ln, g in C.groupby('lane'):
    print(f"  {ln:7s} n={len(g)} TP10 mean {g.tp10.mean():+.2f} win {100*(g.tp10>0).mean():.0f}% | ride10/8 mean {g['rd10_8'].mean():+.2f} win {100*(g['rd10_8']>0).mean():.0f}%")
