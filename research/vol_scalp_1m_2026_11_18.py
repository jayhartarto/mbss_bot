"""research/vol_scalp_1m_2026_11_18.py -- uji scalp 1-2 hari saham volatil likuid dengan URUTAN INTRADAY EKSAK dari bar 1 menit (2026-08-10..2026-11-16, ~69 hari bursa).
Gate DILONGGARKAN dibanding vol_scalp_trend_gate: V (median range 20h) >= 3/4% & p25 range >= 0.5V, likuid >= Rp10/30bn; gate tren: none / c>SMA20 / c>SMA50 / higher-low 1x(5) / bounce>=10% dari low50.
Semua fitur harian dihitung di close hari t-1 (kausal). Dua gaya entry di hari t:
  E1 limit-dip   : limit = Cprev*(1-D_q), D_q = kuantil q (25/50/75) dari dip harian 60h; fill di menit pertama low<=limit (harga min(open menit, limit)).
  E2 konfirmasi  : setelah harga menyentuh zona (low<=Cprev*(1-D_q)), beli di OPEN menit berikut saat close menit >= low_berjalan*(1+r) (r=0.5/1.0%)  -> beli SETELAH pantulan.
Exit (menit demi menit, stop dicek dulu dlm menit yg sama, gap-aware): TP 2/3/4/5%, SL 1.5/2/3%, tahan H=1 (close hari t) atau H=2 (close hari t+1). Biaya 0.4%.
Pembanding: hasil bar harian utk kandidat yg sama (konvensi pesimis/optimis) -> seberapa jauh 1m mengubah kesimpulan.
Run: PYTHONUTF8=1 python research/vol_scalp_1m_2026_11_18.py
"""
import sqlite3, itertools, numpy as np, pandas as pd, warnings
warnings.filterwarnings('ignore')
COST = 0.4
px = pd.read_sql_query("SELECT ticker,date,open,high,low,close,volume FROM ohlcv_daily ORDER BY ticker,date", sqlite3.connect('mbss_ohlcv.db'), parse_dates=['date'])
F = []
for t, g in px.groupby('ticker'):
    if len(g) < 120: continue
    g = g.reset_index(drop=True); o, h, l, c, v = (g[k].astype(float) for k in ('open', 'high', 'low', 'close', 'volume'))
    pc = c.shift(1); rng = (h - l) / pc * 100; dip = (pc - l) / pc * 100
    d = pd.DataFrame({'ticker': t, 'date': g.date})
    # fitur di close t-1 -> geser 1 (dipakai pada hari t)
    f = pd.DataFrame({'pc': c, 'med20': rng.rolling(20).median(), 'p25r': rng.rolling(20).quantile(.25), 'liq': (c * v).rolling(20).mean() / 1e9})
    for q in (25, 50, 75): f[f'D{q}'] = dip.rolling(60).quantile(q / 100).clip(lower=0)
    s20, s50 = c.rolling(20).mean(), c.rolling(50).mean(); lo50 = l.rolling(50).min()
    f['g_s20'] = c > s20; f['g_s50'] = c > s50
    f['g_hl'] = (l.rolling(5).min() > l.shift(5).rolling(5).min()) & (c > s20)
    f['g_b50'] = (c / lo50 - 1 >= .10) & (c > s20); f['g_none'] = True
    d = pd.concat([d, f.shift(1).reset_index(drop=True)], axis=1)
    d['pdate'] = d.date.shift(1); d['nextdate'] = d.date.shift(-1)
    F.append(d)
P = pd.concat(F, ignore_index=True).dropna(subset=['med20', 'p25r', 'liq', 'D50', 'pc'])
P = P[(P.date >= '2026-08-10') & (P.date <= '2026-11-16') & (P.med20 >= 3) & (P.p25r >= 1.5) & (P.liq >= 10)].reset_index(drop=True)
print(f"kandidat ticker-hari: {len(P)} (ticker {P.ticker.nunique()}, hari {P.date.nunique()}), V>=3 & likuid>=Rp10bn")
tk = tuple(P.ticker.unique())
c1 = sqlite3.connect('research/mbss_1m_20260905.sqlite')
M = pd.read_sql_query(f"SELECT ticker, ts_utc, open, high, low, close, volume FROM ohlcv_1m WHERE ticker IN ({','.join('?'*len(tk))}) AND volume>0 ORDER BY ticker, ts_utc", c1, params=tk)
M['date'] = pd.to_datetime(M.ts_utc.str[:10])
BARS = {k: tuple(g[x].to_numpy(float) for x in ('open', 'high', 'low', 'close')) for k, g in M.groupby(['ticker', 'date'])}
print(f"menit dimuat {len(M)}, ticker-hari bar {len(BARS)}")
P = P[[ (r.ticker, r.date) in BARS for r in P.itertuples()]].reset_index(drop=True)
P['next_ok'] = [(r.ticker, r.nextdate) in BARS for r in P.itertuples()]
P = P[P.next_ok].reset_index(drop=True)   # sampel sama utk H=1 dan H=2
print(f"kandidat dengan bar 1m hari t dan t+1: {len(P)}")

def exit_path(O, H, L, C, e, b1, fp, tp, sl, Hd, first_tp_ok):
    """e: indeks bar entry (gabungan hari t + t+1); b1: panjang bar hari t. return harga exit."""
    tpv, slv = fp * (1 + tp / 100), fp * (1 - sl / 100)
    end = (b1 - 1) if Hd == 1 else (len(C) - 1)
    k0 = e if first_tp_ok else e + 1
    # bar e: hanya SL (kalau tak boleh TP)
    if not first_tp_ok and L[e] <= slv: return slv, e
    if k0 > end: return C[end], end
    hs, ls = H[k0:end + 1], L[k0:end + 1]
    ms, mt = ls <= slv, hs >= tpv
    i_s = ms.argmax() if ms.any() else 10**9; i_t = mt.argmax() if mt.any() else 10**9
    if i_s == 10**9 and i_t == 10**9: return C[end], end
    if i_s <= i_t: k = k0 + i_s; return min(O[k], slv), k
    k = k0 + i_t; return max(O[k], tpv), k

def run(r, q, style, rr, tp, sl, Hd):
    o1, h1, l1, c1_ = BARS[(r.ticker, r.date)]; o2, h2, l2, c2 = BARS[(r.ticker, r.nextdate)]
    O = np.concatenate([o1, o2]); H = np.concatenate([h1, h2]); L = np.concatenate([l1, l2]); C = np.concatenate([c1_, c2]); b1 = len(o1)
    zone = r.pc * (1 - getattr(r, f'D{q}') / 100)
    if style == 'E1':
        hit = np.nonzero(L[:b1] <= zone)[0]
        if not len(hit): return None
        e = hit[0]; fp = min(O[e], zone); ftp = False
    else:
        rl = np.minimum.accumulate(L[:b1]); touched = np.nonzero(L[:b1] <= zone)[0]
        if not len(touched): return None
        t0 = touched[0]
        cond = np.nonzero(C[t0:b1 - 1] >= rl[t0:b1 - 1] * (1 + rr / 100))[0]
        if not len(cond): return None
        e = t0 + cond[0] + 1; fp = O[e]; ftp = True
    px_, k = exit_path(O, H, L, C, e, b1, fp, tp, sl, Hd, ftp)
    return (px_ / fp - 1) * 100 - COST, k >= b1

GATES = ['g_none', 'g_s20', 'g_s50', 'g_hl', 'g_b50']
cut = P.date.sort_values().iloc[len(P) // 2]
res = []
for (V, L_), gname in itertools.product(((3, 10), (3, 30), (4, 10), (4, 30)), GATES):
    sub = P[(P.med20 >= V) & (P.p25r >= .5 * V) & (P.liq >= L_) & (P[gname].astype(bool))]
    if len(sub) < 80: continue
    for style, q, rr in [('E1', q, 0) for q in (25, 50, 75)] + [('E2', q, rr) for q in (25, 50, 75) for rr in (0.5, 1.0)]:
        for tp, sl, Hd in itertools.product((2, 3, 4, 5), (1.5, 2, 3), (1, 2)):
            rets, dts = [], []
            for r in sub.itertuples():
                x = run(r, q, style, rr, tp, sl, Hd)
                if x is not None: rets.append(x[0]); dts.append(r.date)
            if len(rets) < 40: continue
            rets = np.array(rets); dts = np.array(dts); a, b = rets[dts < np.datetime64(cut)], rets[dts >= np.datetime64(cut)]
            res.append(dict(V=V, L=L_, gate=gname, style=style, q=q, r=rr, tp=tp, sl=sl, H=Hd, orders=len(sub), n=len(rets), fill=100 * len(rets) / len(sub), win=100 * (rets > 0).mean(),
                            mean=rets.mean(), p10=np.percentile(rets, 10), h1=a.mean() if len(a) > 15 else np.nan, h2=b.mean() if len(b) > 15 else np.nan))
R = pd.DataFrame(res); R.to_csv('research/vol_scalp_1m_grid_2026_11_18.csv', index=False)
pd.set_option('display.width', 220)
print(f"\nkonfigurasi: {len(R)}; mean>0: {int((R['mean']>0).sum())}; mean>0 di kedua paruh: {int(((R.h1>0)&(R.h2>0)).sum())}; mean>+0.3 kedua paruh: {int(((R.h1>.3)&(R.h2>.3)).sum())}")
print("\nTOP 20 menurut mean (n>=60):"); print(R[R.n >= 60].sort_values('mean', ascending=False).head(20).round(2).to_string(index=False))
print("\nrata2 mean per style/q/r:"); print(R.groupby(['style', 'q', 'r'])['mean'].agg(['mean', 'max', 'count']).round(2).to_string())
print("\nrata2 mean per gate:"); print(R.groupby('gate')['mean'].agg(['mean', 'max']).round(2).to_string())
for k in ('tp', 'sl', 'H', 'V', 'L'): print(f"  rata2 mean per {k}: " + ", ".join(f"{a}={m:+.2f}" for a, m in R.groupby(k)['mean'].mean().items()))
print("\nPembanding bar harian pada kandidat yg sama (E1 q50 TP4 SL3 H2, gate none, V3 L10): dihitung ulang dengan bar harian")
sub = P[(P.med20 >= 3) & (P.liq >= 10)]
rows = []
for r in sub.itertuples():
    d = px[(px.ticker == r.ticker) & (px.date.isin([r.date, r.nextdate]))]
    if len(d) < 2: continue
    d1, d2 = d.iloc[0], d.iloc[1]; zone = r.pc * (1 - r.D50 / 100)
    if d1.low > zone: continue
    fp = min(d1.open, zone); tpv, slv = fp * 1.04, fp * .97
    for conv in ('P', 'O'):
        if d1.low <= slv: ex = slv
        elif conv == 'O' and d1.high >= tpv: ex = tpv
        elif d2.low <= slv: ex = min(d2.open, slv)
        elif d2.high >= tpv: ex = max(d2.open, tpv)
        else: ex = d2.close
        rows.append((conv, (ex / fp - 1) * 100 - COST))
D = pd.DataFrame(rows, columns=['conv', 'ret']); print(D.groupby('conv').ret.agg(['count', 'mean']).round(2).to_string())
x = [run(r, 50, 'E1', 0, 4, 3, 2) for r in sub.itertuples()]; x = np.array([a[0] for a in x if a is not None]); print(f"  1m EKSAK pada kandidat sama: n={len(x)} mean={x.mean():+.2f}%")
