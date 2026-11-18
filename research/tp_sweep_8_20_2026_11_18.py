"""research/tp_sweep_8_20_2026_11_18.py -- skenario TP terbaik di range 8-20% (langkah 1pp), tanpa SL.
Episode: sinyal lane OSB-v2 & BOW (research/bull5/lane_episodes_open.pkl), 1 posisi/ticker (slot terkunci H*1.45 hari), entry open T+1.
Exit: high >= entry*(1+TP) -> fill max(open, TP) (gap-aware); kalau belum kena dlm H bar -> close bar H (mark-to-market). Biaya 0.4%.
Metrik: win%, mean, median, hari tahan rata2, ret/hari (= mean / hari tahan), p10, worst, mean paruh awal vs akhir (konsistensi).
Tahap ganda (separuh di TP_a, separuh di TP_b) = rata-rata dua return TP murni pada entry yg sama (tanpa SL -> tidak ada interaksi).
Run: PYTHONUTF8=1 python research/tp_sweep_8_20_2026_11_18.py
"""
import sqlite3, itertools, numpy as np, pandas as pd, warnings
warnings.filterwarnings('ignore')
COST = 0.4
TPS = list(range(8, 21))
px = pd.read_sql_query("SELECT ticker,date,open,high,low,close FROM ohlcv_daily ORDER BY ticker,date", sqlite3.connect('mbss_ohlcv.db'), parse_dates=['date'])
EP = pd.read_pickle('research/bull5/lane_episodes_open.pkl')
sigs = pd.concat([EP['OSBv2'].assign(lane='OSB-v2'), EP['BOW'].assign(lane='BOW')])[['ticker', 'date', 'lane']].drop_duplicates()
sigs['date'] = pd.to_datetime(sigs.date)
A = {}
for t, g in px[px.ticker.isin(sigs.ticker.unique())].groupby('ticker'):
    c = g.close.astype(float); d = c.diff(); up = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean(); dn = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    A[t] = dict(d=g.date.values, o=g.open.to_numpy(float), h=g.high.to_numpy(float), c=c.to_numpy(), rsi=(100 - 100 / (1 + up / dn.replace(0, np.nan))).to_numpy(),
                dd60=(c / c.rolling(60).max() - 1).to_numpy() * 100)

def run(H):
    rows = []
    for lane, g in sigs.sort_values(['date', 'ticker']).groupby('lane'):
        busy = {}
        for r in g.itertuples():
            a = A.get(r.ticker)
            if a is None or (r.ticker in busy and r.date <= busy[r.ticker]): continue
            i = int(np.searchsorted(a['d'], np.datetime64(r.date)))
            if i >= len(a['d']) or a['d'][i] != np.datetime64(r.date) or i + 1 + H > len(a['d']): continue
            e = i + 1; ep = a['o'][e]
            if not ep > 0: continue
            busy[r.ticker] = r.date + pd.Timedelta(days=int(H * 1.45))
            hh = a['h'][e:e + H]; oo = a['o'][e:e + H]
            row = dict(lane=lane, ticker=r.ticker, date=r.date, filt=bool(a['dd60'][i] <= -20 and a['rsi'][i] >= 31))
            for tp in TPS:
                lv = ep * (1 + tp / 100); hit = np.nonzero(hh >= lv)[0]
                if len(hit): k = hit[0]; row[f'r{tp}'] = (max(oo[k], lv) / ep - 1) * 100 - COST; row[f'd{tp}'] = k + 1
                else: row[f'r{tp}'] = (a['c'][e + H - 1] / ep - 1) * 100 - COST; row[f'd{tp}'] = H
                row[f'h{tp}'] = float(len(hit) > 0)
            rows.append(row)
    return pd.DataFrame(rows)

def table(E, H, label):
    cut = E.date.median()
    print(f"\n--- {label} | H={H} bar | n={len(E)} ({E.date.min().date()}..{E.date.max().date()}) ---")
    print(f"  {'TP':>3} {'hit%':>5} {'win%':>5} {'mean':>6} {'med':>6} {'hari':>5} {'ret/hr':>7} {'p10':>6} {'worst':>6} {'paruh1':>7} {'paruh2':>7}")
    best = {}
    for tp in TPS:
        x = E[f'r{tp}']; dys = E[f'd{tp}']
        p1, p2 = x[E.date < cut].mean(), x[E.date >= cut].mean()
        rpd = x.mean() / dys.mean()
        print(f"  {tp:3d} {100*E[f'h{tp}'].mean():5.1f} {100*(x>0).mean():5.1f} {x.mean():+6.2f} {x.median():+6.2f} {dys.mean():5.1f} {rpd:+7.3f} {x.quantile(.1):+6.1f} {x.min():+6.1f} {p1:+7.2f} {p2:+7.2f}")
        best[tp] = dict(mean=x.mean(), rpd=rpd, win=100 * (x > 0).mean(), p1=p1, p2=p2, sharpe=x.mean() / x.std())
    B = pd.DataFrame(best).T
    print(f"  >> mean tertinggi: TP{int(B['mean'].idxmax())} ({B['mean'].max():+.2f}%) | ret/hari tertinggi: TP{int(B.rpd.idxmax())} ({B.rpd.max():+.3f}%/bar) | win tertinggi: TP{int(B.win.idxmax())} ({B.win.max():.1f}%) | mean/std tertinggi: TP{int(B.sharpe.idxmax())} | TP dgn mean>0 di KEDUA paruh: {[int(k) for k in B.index if B.loc[k,'p1']>0 and B.loc[k,'p2']>0][:13]}")
    # tahap ganda
    combos = []
    for a_, b_ in itertools.combinations(TPS, 2):
        if b_ - a_ < 3: continue
        x = (E[f'r{a_}'] + E[f'r{b_}']) / 2; dd = (E[f'd{a_}'] + E[f'd{b_}']) / 2
        combos.append((a_, b_, x.mean(), x.mean() / dd.mean(), 100 * (x > 0).mean(), x.quantile(.1)))
    C = pd.DataFrame(combos, columns=['a', 'b', 'mean', 'rpd', 'win', 'p10'])
    print("  tahap ganda 50/50 (a,b) terbaik menurut mean: " + "; ".join(f"{int(r.a)}/{int(r.b)}: mean {r['mean']:+.2f}% win {r.win:.0f}% rpd {r.rpd:+.3f}" for _, r in C.sort_values('mean', ascending=False).head(3).iterrows()))
    print("  tahap ganda terbaik menurut ret/hari: " + "; ".join(f"{int(r.a)}/{int(r.b)}: mean {r['mean']:+.2f}% win {r.win:.0f}% rpd {r.rpd:+.3f}" for _, r in C.sort_values('rpd', ascending=False).head(3).iterrows()))

for H in (90, 60):
    E = run(H)
    print("\n" + "=" * 140 + f"\nHORIZON {H} BAR BURSA\n" + "=" * 140)
    table(E[E.filt], H, 'OSB-v2+BOW TERFILTER (dd60<=-20 & RSI>=31)')
    table(E, H, 'OSB-v2+BOW SEMUA')
    table(E[E.lane == 'OSB-v2'], H, 'OSB-v2 saja')
    table(E[E.lane == 'BOW'], H, 'BOW saja')
