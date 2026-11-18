"""research/dose_response_2026_11_18.py -- apakah BESAR gain berbeda menurut INTENSITAS parameter pilihan?
Episode = sinyal lane OSB-v2 & BOW (dedup 1 posisi/ticker, entry open T+1, horizon 90 bar). Intensitas diukur di bar sinyal (tanpa lookahead):
 dist20 (jarak ke SMA20), dd60 (turun dari high 60d), rsi, atr%, ret5, liq, volx, rel1 (ret hari-H dikurangi ret IHSG), i_dd100 (kedalaman drawdown IHSG).
Respons: MFE90 (high tertinggi 90 bar / entry), return bersih utk TP10/TP15/TP20 (tanpa SL, mark-to-market bar 90 kalau belum kena), hit%.
Uji: kuartil per fitur, Spearman rho (+ tanda di paruh awal/akhir), dan skor komposit 'kedalaman oversold' (a priori: -dist20, -dd60, -rsi) -> tercile.
Run: PYTHONUTF8=1 python research/dose_response_2026_11_18.py
"""
import sqlite3, numpy as np, pandas as pd, warnings
from scipy import stats
warnings.filterwarnings('ignore')
COST, H = 0.4, 90
R = pd.read_pickle('research/.tmp_ihsg_regime_full.pkl'); R.index = pd.to_datetime(R.index).normalize(); R['ret1'] = R.close.pct_change() * 100
px = pd.read_sql_query("SELECT ticker,date,open,high,low,close,volume FROM ohlcv_daily ORDER BY ticker,date", sqlite3.connect('mbss_ohlcv.db'), parse_dates=['date'])
EP = pd.read_pickle('research/bull5/lane_episodes_open.pkl')
sigs = pd.concat([EP['OSBv2'].assign(lane='OSB-v2'), EP['BOW'].assign(lane='BOW')])[['ticker', 'date', 'lane']].drop_duplicates()
sigs['date'] = pd.to_datetime(sigs.date)
A = {}
for t, g in px[px.ticker.isin(sigs.ticker.unique())].groupby('ticker'):
    o, h, l, c, v = (g[k].astype(float) for k in ('open', 'high', 'low', 'close', 'volume'))
    d = c.diff(); up = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean(); dn = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    A[t] = dict(d=g.date.values, o=o.to_numpy(), h=h.to_numpy(), c=c.to_numpy(),
                f=pd.DataFrame(dict(dist20=(c / c.rolling(20).mean() - 1) * 100, dd60=(c / c.rolling(60).max() - 1) * 100, rsi=100 - 100 / (1 + up / dn.replace(0, np.nan)),
                                    atr=tr.rolling(14).mean() / c * 100, ret5=c.pct_change(5) * 100, liq=(c * v).rolling(20).mean() / 1e9, volx=v / v.rolling(20).mean(),
                                    ret1=c.pct_change() * 100)).to_numpy())
FN = ['dist20', 'dd60', 'rsi', 'atr', 'ret5', 'liq', 'volx', 'ret1']
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
        hh, oo = a['h'][e:e + H], a['o'][e:e + H]
        row = dict(lane=lane, ticker=r.ticker, date=r.date, mfe=(hh.max() / ep - 1) * 100, **dict(zip(FN, a['f'][i])))
        rg = R.loc[:r.date].iloc[-1]; row['i_dd100'] = rg.dd_100 * 100; row['rel1'] = row['ret1'] - rg.ret1
        for tp in (10, 15, 20):
            lv = ep * (1 + tp / 100); hit = np.nonzero(hh >= lv)[0]
            row[f'r{tp}'] = ((max(oo[hit[0]], lv) if len(hit) else a['c'][e + H - 1]) / ep - 1) * 100 - COST; row[f'h{tp}'] = float(len(hit) > 0)
            row[f'd{tp}'] = (hit[0] + 1) if len(hit) else H
        rows.append(row)
E = pd.DataFrame(rows).dropna(subset=FN + ['i_dd100']).reset_index(drop=True)
E['osb'] = (E.lane == 'OSB-v2').astype(float)
cut = E.date.median()
print(f"Episode: {len(E)} (OSB-v2 {int(E.osb.sum())}, BOW {int((1-E.osb).sum())}), {E.date.min().date()}..{E.date.max().date()}; MFE90 median={E.mfe.median():.1f}% mean={E.mfe.mean():.1f}%")

FEATS = ['dist20', 'dd60', 'rsi', 'atr', 'ret5', 'liq', 'volx', 'rel1', 'i_dd100']
print("\n" + "=" * 150 + "\nSPEARMAN rho (intensitas fitur vs respons); tanda paruh awal/akhir (+ = searah)\n" + "=" * 150)
print(f"  {'fitur':8s} | {'MFE90':>16s} | {'ret TP10':>16s} | {'ret TP20':>16s} | arti (rho>0: nilai fitur LEBIH TINGGI -> gain lebih besar)")
for f in FEATS:
    out = []
    for y in ('mfe', 'r10', 'r20'):
        rho, p = stats.spearmanr(E[f], E[y])
        r1 = stats.spearmanr(E[f][E.date < cut], E[y][E.date < cut])[0]; r2 = stats.spearmanr(E[f][E.date >= cut], E[y][E.date >= cut])[0]
        out.append(f"{rho:+.2f} p={p:.3f} ({'+' if r1>0 else '-'}{'+' if r2>0 else '-'})")
    print(f"  {f:8s} | " + " | ".join(f"{x:>16s}" for x in out))

print("\n" + "=" * 150 + "\nKUARTIL per fitur (Q1=nilai terendah .. Q4=tertinggi): n, MFE90 median, hit TP20, ret@TP10, ret@TP20\n" + "=" * 150)
for f in ['dist20', 'dd60', 'rsi', 'atr', 'i_dd100', 'rel1']:
    E['q'] = pd.qcut(E[f], 4, labels=False, duplicates='drop')
    print(f"\n  {f} (batas kuartil: {', '.join(f'{x:.1f}' for x in E[f].quantile([0, .25, .5, .75, 1]))})")
    for q, g in E.groupby('q'):
        print(f"    Q{int(q)+1}: n={len(g):3d}  MFE med={g.mfe.median():5.1f}% mean={g.mfe.mean():5.1f}% | hit20={100*g.h20.mean():4.0f}% | ret@TP10={g.r10.mean():+5.2f}% ret@TP20={g.r20.mean():+5.2f}% | p10(TP20)={g.r20.quantile(.1):+6.1f}%")

# komposit a priori: kedalaman oversold
z = lambda s: (s - s.mean()) / s.std()
E['depth'] = z(-E.dist20) + z(-E.dd60) + z(-E.rsi)
E['t'] = pd.qcut(E.depth, 3, labels=['LONGGAR', 'SEDANG', 'DALAM'])
print("\n" + "=" * 150 + "\nSKOR KOMPOSIT 'kedalaman oversold' (= z(-dist20)+z(-dd60)+z(-rsi), ditentukan a priori) -> tercile\n" + "=" * 150)
for t, g in E.groupby('t'):
    first, second = g[g.date < cut], g[g.date >= cut]
    print(f"  {t:8s} n={len(g):3d} | MFE med={g.mfe.median():5.1f}% mean={g.mfe.mean():5.1f}% | hit10={100*g.h10.mean():4.0f}% hit15={100*g.h15.mean():4.0f}% hit20={100*g.h20.mean():4.0f}% | ret@TP10={g.r10.mean():+5.2f} @TP15={g.r15.mean():+5.2f} @TP20={g.r20.mean():+5.2f} | hari@TP10={g.d10.mean():4.1f} @TP20={g.d20.mean():4.1f} | p10 TP20={g.r20.quantile(.1):+6.1f} | ret@TP20 paruh1/2: {first.r20.mean():+5.2f}/{second.r20.mean():+5.2f} (n{len(first)}/{len(second)})")
rho = stats.spearmanr(E.depth, E.r20); rho2 = stats.spearmanr(E.depth, E.mfe)
print(f"  Spearman komposit vs ret@TP20: {rho[0]:+.2f} (p={rho[1]:.3f}); vs MFE90: {rho2[0]:+.2f} (p={rho2[1]:.3f})")
for ln, g in E.groupby('lane'):
    r = stats.spearmanr(g.depth, g.r20); m = stats.spearmanr(g.depth, g.mfe)
    print(f"  {ln}: komposit vs ret@TP20 {r[0]:+.2f} (p={r[1]:.3f}), vs MFE {m[0]:+.2f} (p={m[1]:.3f}), n={len(g)}")
# TP terbaik per tercile menurut ret/hari
print("\n  TP optimal per tercile (ret/hari = mean ret / mean hari) -> hanya TP10/15/20 yg dihitung:")
for t, g in E.groupby('t'):
    print(f"    {t:8s} " + " | ".join(f"TP{tp}: {g[f'r{tp}'].mean():+.2f}% / {g[f'd{tp}'].mean():.0f}hr = {g[f'r{tp}'].mean()/g[f'd{tp}'].mean():+.3f}/hr" for tp in (10, 15, 20)))
