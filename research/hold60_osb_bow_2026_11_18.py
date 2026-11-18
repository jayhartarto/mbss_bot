"""research/hold60_osb_bow_2026_11_18.py -- "trading santai": ambil pick OSB-v2 / BOW, tahan ~60 hari, tanpa TP/SL.
Sinyal: replay lane (research/bull5/lane_episodes_open.pkl; universe panel riset ~240 ticker, BOW 2025-01..2026-10, OSB-v2 2024-11..2026-10).
Entry: OPEN hari bursa setelah sinyal. Exit: CLOSE pada hari ke-60 kalender (dan varian 60 hari bursa). Biaya 0.4% round trip.
Satu posisi per ticker per lane (sinyal berikutnya diabaikan sampai posisi lama keluar). Hanya trade yang horizonnya sudah lengkap.
Baseline = rata-rata return 60d SEMUA ticker likuid (nilai 20d >= Rp1bn) dengan tanggal entry yang sama -> mengukur edge di atas pasar.
CI = bootstrap per-minggu entry (trade se-minggu tidak independen).
Run: PYTHONUTF8=1 python research/hold60_osb_bow_2026_11_18.py
"""
import sqlite3, numpy as np, pandas as pd

COST = 0.4
LAST = pd.Timestamp('2026-11-16')
rng = np.random.default_rng(11)

px = pd.read_sql_query("SELECT ticker,date,open,high,low,close,volume FROM ohlcv_daily ORDER BY ticker,date",
                       sqlite3.connect('mbss_ohlcv.db'), parse_dates=['date'])
A = {}
for t, g in px.groupby('ticker'):
    c = g.close.astype(float)
    A[t] = dict(d=g.date.values, o=g.open.to_numpy(float), l=g.low.to_numpy(float), c=c.to_numpy(),
                liq=(c * g.volume.astype(float)).rolling(20).median().to_numpy())

def hold(t, sig_date, mode):
    a = A.get(t)
    if a is None: return None
    i = int(np.searchsorted(a['d'], np.datetime64(sig_date)))
    if i >= len(a['d']) or a['d'][i] != np.datetime64(sig_date): return None
    e = i + 1
    if e >= len(a['d']): return None
    ep = a['o'][e]
    if not ep > 0: return None
    if mode == 'cal':
        target = np.datetime64(pd.Timestamp(a['d'][e]) + pd.Timedelta(days=60))
        if pd.Timestamp(target) > LAST: return None
        j = int(np.searchsorted(a['d'], target, side='right')) - 1
    else:
        j = e + 59
        if j >= len(a['d']): return None
    ret = (a['c'][j] / ep - 1) * 100 - COST
    mae = (a['l'][e:j + 1].min() / ep - 1) * 100
    return dict(ret=ret, mae=mae, entry=pd.Timestamp(a['d'][e]), exit=pd.Timestamp(a['d'][j]), days=j - e + 1, liq=a['liq'][i])

def baseline(entry_dates, mode):
    out = []
    for ed in sorted(set(entry_dates)):
        rs = []
        for t, a in A.items():
            e = int(np.searchsorted(a['d'], np.datetime64(ed)))
            if e >= len(a['d']) or a['d'][e] != np.datetime64(ed) or e < 1 or not a['liq'][e - 1] >= 1e9: continue
            ep = a['o'][e]
            if not ep > 0: continue
            if mode == 'cal':
                j = int(np.searchsorted(a['d'], np.datetime64(ed + pd.Timedelta(days=60)), side='right')) - 1
            else:
                j = e + 59
            if j >= len(a['d']) or pd.Timestamp(a['d'][j]) > LAST or j == e: continue
            rs.append((a['c'][j] / ep - 1) * 100 - COST)
        if rs: out.append((ed, np.mean(rs), np.median(rs)))
    return pd.DataFrame(out, columns=['entry', 'base_mean', 'base_med']).set_index('entry')

def boot_ci(df, col='ret'):
    wk = df.entry.dt.to_period('W').astype(str)
    groups = [g[col].to_numpy() for _, g in df.groupby(wk)]
    means = []
    for _ in range(2000):
        pick = rng.integers(0, len(groups), len(groups))
        means.append(np.concatenate([groups[k] for k in pick]).mean())
    return np.percentile(means, [5, 95])

def line(label, df):
    if len(df) == 0:
        print(f"  {label:34s} n=0"); return
    lo, hi = boot_ci(df)
    print(f"  {label:34s} n={len(df):4d} win={100*(df.ret>0).mean():5.1f}% mean={df.ret.mean():+6.2f}% med={df.ret.median():+6.2f}% "
          f"p10={df.ret.quantile(.1):+6.1f} p90={df.ret.quantile(.9):+6.1f} CI90=[{lo:+.1f},{hi:+.1f}] "
          f"excess={df.excess.mean():+6.2f}% | MAE med={df.mae.median():+5.1f}% <=-15:{100*(df.mae<=-15).mean():4.1f}% <=-25:{100*(df.mae<=-25).mean():4.1f}%")

EP = pd.read_pickle('research/bull5/lane_episodes_open.pkl')
for mode in ('cal', 'trd'):
    print("=" * 130)
    print("HOLD 60 HARI", "KALENDER (~42 hari bursa)" if mode == 'cal' else "BURSA (~85 hari kalender)", "| biaya 0.4% | tanpa SL/TP")
    for lane, key in (('OSB-v2', 'OSBv2'), ('BOW', 'BOW')):
        sig = EP[key][['ticker', 'date']].drop_duplicates().sort_values(['date', 'ticker'])
        rows, busy = [], {}
        for r in sig.itertuples():
            if r.ticker in busy and r.date <= busy[r.ticker]: continue
            h = hold(r.ticker, r.date, mode)
            if h is None: continue
            busy[r.ticker] = h['exit']
            rows.append(dict(lane=lane, ticker=r.ticker, sig=r.date, **h))
        df = pd.DataFrame(rows)
        if df.empty: print(lane, "no complete trades"); continue
        base = baseline(df.entry.unique(), mode)
        df = df.join(base, on='entry'); df['excess'] = df.ret - df.base_mean
        print(f"\n{lane}: sinyal {sig.date.min().date()}..{sig.date.max().date()}, trade lengkap {df.sig.min().date()}..{df.sig.max().date()}")
        line("SEMUA", df)
        print(f"  baseline pasar (tgl entry sama): mean={df.base_mean.mean():+.2f}% median={df.base_med.mean():+.2f}%")
        df['per'] = df.sig.dt.to_period('Q').astype(str)
        for p, g in df.groupby('per'): line(f"kuartal {p}", g)
        line("likuid >= Rp5bn/hari", df[df.liq >= 5e9])
        line("likuid < Rp5bn/hari", df[df.liq < 5e9])
        # portofolio sederhana: bobot sama per tanggal entry, lalu rata-rata antar-tanggal (hindari klaster)
        pd_ = df.groupby('entry').ret.mean()
        print(f"  rata-rata per-tanggal-entry (bobot sama): mean={pd_.mean():+.2f}% n_tgl={len(pd_)}  worst={pd_.min():+.1f}%  best={pd_.max():+.1f}%")
        df.to_csv(f"research/hold60_{lane}_{mode}_2026_11_18.csv", index=False)
