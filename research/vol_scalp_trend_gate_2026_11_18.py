"""research/vol_scalp_trend_gate_2026_11_18.py -- scalp 1-2 hari pada saham VOLATIL KONSISTEN + LIKUID, beli di area dip (limit), jual TP 3-5%, SL terukur,
dengan gate TREN naik jangka menengah / bounce dari dasar 50-100 hari / higher low.

Definisi (semua kausal; sinyal dihitung di close hari i, order limit berlaku di hari i+1 = bar f):
 volatil konsisten : median range harian 20h ((H-L)/Cprev) >= V  DAN  persentil-25 range 20h >= 0.6*V   (V in 4,5,6 %)
 likuid            : rata2 nilai transaksi 20h >= Rp L miliar (10 / 30)
 area beli         : limit = Cprev*(1 - D), D = kuantil q dari kedalaman dip historis 60h, dip_t=(Cprev-L_t)/Cprev (q=25/50/75 -> fill ~75/50/25% hari), D>=0
                      gap-aware: kalau open <= limit -> isi di open.
 exit              : TP = fill*(1+tp) tp in 3,4,5 ; SL = fill*(1-sl) sl in 2,3,4 ; tahan H=1 (close bar f) atau H=2 (close bar f+1)
 konvensi urutan intrabar (data harian tak tahu urutan): stop dicek dulu. TP pada bar fill: 'P' = TIDAK dihitung (pesimis, TP mungkin terjadi sebelum fill),
                      'O' = dihitung (optimis). Bar f+1: stop dulu, lalu TP (gap-aware).
 biaya             : 0.4% round trip.
Gate tren (di close i): lihat GATES di bawah.
Output: tabel gate x (V,L) pada exit baku, lalu grid exit utk gate terbaik, lalu pilih-di-train/uji-di-test.
Run: PYTHONUTF8=1 python research/vol_scalp_trend_gate_2026_11_18.py
"""
import sqlite3, itertools, numpy as np, pandas as pd, warnings
warnings.filterwarnings('ignore')
COST = 0.4
px = pd.read_sql_query("SELECT ticker,date,open,high,low,close,volume FROM ohlcv_daily ORDER BY ticker,date", sqlite3.connect('mbss_ohlcv.db'), parse_dates=['date'])
F = []  # per ticker frame
for t, g in px.groupby('ticker'):
    if len(g) < 220: continue
    g = g.reset_index(drop=True); o, h, l, c, v = (g[k].astype(float) for k in ('open', 'high', 'low', 'close', 'volume'))
    pc = c.shift(1); rng = (h - l) / pc * 100; dip = (pc - l) / pc * 100
    d = pd.DataFrame({'ticker': t, 'date': g.date, 'o': o, 'h': h, 'l': l, 'c': c})
    d['med20'] = rng.rolling(20).median(); d['p25r'] = rng.rolling(20).quantile(.25); d['liq'] = (c * v).rolling(20).mean() / 1e9
    for q in (25, 50, 75): d[f'D{q}'] = dip.rolling(60).quantile(q / 100).clip(lower=0)
    s20, s50, s100 = c.rolling(20).mean(), c.rolling(50).mean(), c.rolling(100).mean()
    d['t_a'] = (c > s50) & (s50 > s100)                                                # tren: harga>SMA50>SMA100
    d['t_b'] = (c > s20) & (s20 > s50) & (s50 > s100)                                  # tren kuat: SMA20>50>100
    d['t_c'] = (c > s50) & (s50 > s50.shift(10)) & (c / c.shift(60) - 1 >= .10)        # SMA50 naik + ret60>=10%
    d['t_d'] = (c > s50) & (c / c.shift(60) - 1 >= .20)                                 # ret60>=20%
    for n_, key in ((50, 'l50'), (100, 'l100')):
        lo = l.rolling(n_).min(); since = l.rolling(n_).apply(lambda x: len(x) - 1 - np.argmin(x), raw=True)
        for X in (10, 20):
            d[f'b{n_}_{X}'] = (c / lo - 1 >= X / 100) & (since >= 5) & (c > s20)       # bounce >=X% dari dasar n hari (dasar >=5 bar lalu) & di atas SMA20
    lw = lambda a, b: l.shift(a).rolling(b).min()
    for w in (5, 10):
        r0 = l.rolling(w).min(); r1 = lw(w, w); r2 = lw(2 * w, w)
        d[f'hl1_{w}'] = (r0 > r1) & (c > s20); d[f'hl2_{w}'] = (r0 > r1) & (r1 > r2) & (c > s20)
    d['hl2_10_t_a'] = d['hl2_10'] & d['t_a']; d['hl1_5_t_a'] = d['hl1_5'] & d['t_a']
    d['none'] = True
    F.append(d)
P = pd.concat(F, ignore_index=True)
P['tk'] = P.ticker.astype('category').cat.codes
# outcome arrays: bar f=i+1, f+1=i+2 (hanya jika ticker sama & ada)
for k in ('o', 'h', 'l', 'c'):
    for s in (1, 2): P[f'{k}{s}'] = P.groupby('tk')[k].shift(-s)
P = P.dropna(subset=['med20', 'p25r', 'liq', 'D50', 'o1', 'o2']).reset_index(drop=True)
print(f"panel {len(P)} baris, {P.ticker.nunique()} ticker, {P.date.min().date()}..{P.date.max().date()}")
GATES = ['none', 't_a', 't_b', 't_c', 't_d', 'b50_10', 'b50_20', 'b100_10', 'b100_20', 'hl1_5', 'hl2_5', 'hl1_10', 'hl2_10', 'hl1_5_t_a', 'hl2_10_t_a']

def outcome(S, q, tp, sl, H, conv):
    """vectorized; S = subframe. return (filled mask, net ret %)"""
    lim = S.c.to_numpy() * (1 - S[f'D{q}'].to_numpy() / 100)
    o1, h1, l1, c1, o2, h2, l2, c2 = (S[k].to_numpy() for k in ('o1', 'h1', 'l1', 'c1', 'o2', 'h2', 'l2', 'c2'))
    filled = l1 <= lim
    fp = np.where(o1 <= lim, o1, lim)
    tpv, slv = fp * (1 + tp / 100), fp * (1 - sl / 100)
    ex = np.full(len(S), np.nan); done = ~filled
    # bar f
    hit_sl = filled & (l1 <= slv)
    ex = np.where(hit_sl, slv, ex); done = done | hit_sl
    if conv == 'O':
        hit_tp = filled & ~done & (h1 >= tpv); ex = np.where(hit_tp, tpv, ex); done = done | hit_tp
    if H == 1:
        rest = filled & ~done; ex = np.where(rest, c1, ex)
    else:
        rest = filled & ~done
        s2 = rest & (l2 <= slv); ex = np.where(s2, np.minimum(o2, slv), ex)
        t2 = rest & ~s2 & (h2 >= tpv); ex = np.where(t2, np.maximum(o2, tpv), ex)
        e2 = rest & ~s2 & ~t2; ex = np.where(e2, c2, ex)
    ret = (ex / fp - 1) * 100 - COST
    return filled, ret

def stats(S, filled, ret, split):
    n_o = len(S); r = ret[filled]; n = len(r)
    if n < 30: return None
    dts = S.date.to_numpy()[filled]; m = np.datetime64(S.date.sort_values().iloc[len(S) // 2]) if split is None else split
    a, b = r[dts < m], r[dts >= m]
    ntd = pd.Series(dts).nunique()
    return dict(orders=n_o, n=n, fill=100 * n / n_o, win=100 * (r > 0).mean(), mean=r.mean(), med=np.median(r), p10=np.percentile(r, 10), h1=a.mean() if len(a) > 20 else np.nan, h2=b.mean() if len(b) > 20 else np.nan,
                per_day=n / max(ntd, 1), ndays=ntd)
BASE = dict(q=50, tp=4, sl=3, H=2)
print("\n" + "=" * 170 + f"\n1) GATE x (V,L) pada exit baku q={BASE['q']} TP{BASE['tp']} SL{BASE['sl']} H{BASE['H']} ; konvensi P (pesimis) / O (optimis)\n" + "=" * 170)
print(f"{'V%':>3} {'Lbn':>4} {'gate':11s} | {'orders':>6} {'fill%':>5} {'n':>6} | P: {'win':>5} {'mean':>6} {'p10':>6} {'h1':>6} {'h2':>6} | O: {'win':>5} {'mean':>6} | hari {'ndays':>5} trade/hari")
RES = []
for V, L in itertools.product((4, 5, 6), (10, 30)):
    Bm = P[(P.med20 >= V) & (P.p25r >= .6 * V) & (P.liq >= L)]
    for gname in GATES:
        S = Bm[Bm[gname].astype(bool)]
        if len(S) < 60: continue
        fp_, rp = outcome(S, BASE['q'], BASE['tp'], BASE['sl'], BASE['H'], 'P'); fo_, ro = outcome(S, BASE['q'], BASE['tp'], BASE['sl'], BASE['H'], 'O')
        sp, so = stats(S, fp_, rp, None), stats(S, fo_, ro, None)
        if sp is None: continue
        RES.append((V, L, gname, sp['n'], sp['mean'], so['mean']))
        print(f"{V:3d} {L:4d} {gname:11s} | {sp['orders']:6d} {sp['fill']:5.0f} {sp['n']:6d} | P: {sp['win']:5.1f} {sp['mean']:+6.2f} {sp['p10']:+6.1f} {sp['h1']:+6.2f} {sp['h2']:+6.2f} | O: {so['win']:5.1f} {so['mean']:+6.2f} | {sp['ndays']:5d} {sp['per_day']:5.1f}")

print("\n" + "=" * 170 + "\n2) GRID EXIT (q,TP,SL,H) pada gate x (V,L) dgn n>=150 : 15 terbaik menurut mean konvensi P (pesimis), dgn mean kedua paruh waktu\n" + "=" * 170)
rows = []
for V, L in itertools.product((4, 5, 6), (10, 30)):
    Bm = P[(P.med20 >= V) & (P.p25r >= .6 * V) & (P.liq >= L)]
    for gname in GATES:
        S = Bm[Bm[gname].astype(bool)]
        if len(S) < 200: continue
        for q, tp, sl, H in itertools.product((25, 50, 75), (3, 4, 5), (2, 3, 4), (1, 2)):
            f_, r_ = outcome(S, q, tp, sl, H, 'P'); st = stats(S, f_, r_, None)
            if st is None or st['n'] < 150: continue
            rows.append(dict(V=V, L=L, gate=gname, q=q, tp=tp, sl=sl, H=H, **{k: st[k] for k in ('n', 'fill', 'win', 'mean', 'p10', 'h1', 'h2')}))
G = pd.DataFrame(rows)
pd.set_option('display.width', 220)
print(G.sort_values('mean', ascending=False).head(15).round(2).to_string(index=False))
print(f"\nkonfigurasi diuji: {len(G)}; yang mean>0 di kedua paruh: {int(((G.h1>0)&(G.h2>0)).sum())}; mean>+0.5 di kedua paruh: {int(((G.h1>.5)&(G.h2>.5)).sum())}")
print("\nrata2 mean per gate (semua exit), konvensi P:"); print(G.groupby('gate')[['mean', 'h1', 'h2']].mean().round(2).sort_values('mean', ascending=False).to_string())
print("\nrata2 mean per q / tp / sl / H:")
for k in ('q', 'tp', 'sl', 'H'): print(f"  {k}: " + ", ".join(f"{a}={m:+.2f}" for a, m in G.groupby(k)['mean'].mean().items()))
print("\nrata2 mean per (V,L):"); print(G.groupby(['V', 'L'])['mean'].agg(['mean', 'count']).round(2).to_string())

print("\n" + "=" * 170 + "\n3) PILIH-DI-TRAIN / UJI-DI-TEST: split di 60% tanggal. Top 10 konfigurasi menurut mean TRAIN (konvensi P, n_train>=100) -> mean TEST\n" + "=" * 170)
cut = P.date.sort_values().iloc[int(len(P) * .6)]
tr = []
for V, L in itertools.product((4, 5, 6), (10, 30)):
    Bm = P[(P.med20 >= V) & (P.p25r >= .6 * V) & (P.liq >= L)]
    for gname in GATES:
        S = Bm[Bm[gname].astype(bool)]; A, Bt = S[S.date < cut], S[S.date >= cut]
        if len(A) < 150 or len(Bt) < 60: continue
        for q, tp, sl, H in itertools.product((25, 50, 75), (3, 4, 5), (2, 3, 4), (1, 2)):
            fa, ra = outcome(A, q, tp, sl, H, 'P'); fb, rb = outcome(Bt, q, tp, sl, H, 'P')
            if fa.sum() < 100 or fb.sum() < 40: continue
            tr.append(dict(V=V, L=L, gate=gname, q=q, tp=tp, sl=sl, H=H, n_tr=int(fa.sum()), mean_tr=ra[fa].mean(), n_te=int(fb.sum()), mean_te=rb[fb].mean(), win_te=100 * (rb[fb] > 0).mean()))
T = pd.DataFrame(tr)
print(f"cut {cut.date()}, konfigurasi {len(T)}")
print(T.sort_values('mean_tr', ascending=False).head(10).round(2).to_string(index=False))
print(f"\nkorelasi peringkat mean_tr vs mean_te: {T.mean_tr.corr(T.mean_te, method='spearman'):+.2f}; rata2 mean TEST semua konfigurasi {T.mean_te.mean():+.2f}; top-50 train -> rata2 TEST {T.sort_values('mean_tr', ascending=False).head(50).mean_te.mean():+.2f}; % konfigurasi TEST>0: {100*(T.mean_te>0).mean():.0f}%")
G.to_csv('research/vol_scalp_grid_2026_11_18.csv', index=False)
