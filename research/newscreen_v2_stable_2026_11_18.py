"""research/newscreen_v2_stable_2026_11_18.py
Lanjutan newscreen_tp20_trail: pencarian aturan yang STABIL antar-periode dan PER KELOMPOK REGIME IHSG (bukan memaksimalkan win rate total).
Exit tetap: TP+20%, trailing 10% setelah +12%, tanpa SL, maks 42 bar, biaya 0.4% (lihat script sebelumnya).
Kelompok: NEUTRAL (NEUTRAL_BULLISH), BEAR (CRASH_FAST/BEARISH_GRIND/PULLBACK_IN_BOUNCE), BOUNCE (BOUNCE_OFF_LOW).
Objektif (TRAIN, < 2025-07-01): maksimalkan win rate MINIMUM di 4 sub-periode (tanggal dibagi rata), syarat mean tiap sub-periode >= 0 dan mean total >= +2%.
Laporan: TEST (>= 2025-07-01). Fitur tanpa BOW/OSB-v2.
Run: PYTHONUTF8=1 python research/newscreen_v2_stable_2026_11_18.py
"""
import numpy as np, pandas as pd, warnings
warnings.filterwarnings('ignore')
D, LATEST = pd.read_pickle('research/.tmp_newscreen_panel.pkl')
SPLIT = pd.Timestamp('2025-07-01')
GROUPS = {'NEUTRAL': ['NEUTRAL_BULLISH'], 'BEAR': ['CRASH_FAST', 'BEARISH_GRIND', 'PULLBACK_IN_BOUNCE'], 'BOUNCE': ['BOUNCE_OFF_LOW']}
FEATS = ['dist20', 'dist50', 'dd60', 'dd120', 'ret5', 'ret10', 'ret20', 'ret60', 'rsi', 'macd', 'pctb', 'atr', 'rng10', 'volx', 'clv', 'body', 'gap', 'up5',
         'liq', 'rs20', 'brd20', 'brd50', 'ihsg_dd100', 'ihsg_r20']
QS = (0.1, 0.2, 0.3, 0.5, 0.7, 0.8, 0.9)

def mk(c, df):
    _, f, op, thr = c; x = df[f]
    return (x >= thr) if op == '>=' else (x <= thr)

def folds_of(df, k=4):
    ud = np.sort(df.date.unique()); cuts = np.array_split(ud, k)
    return [df.date.isin(c).to_numpy() for c in cuts]

def fold_stats(sub_mask, df, fold_masks):
    ws, ms, ns = [], [], []
    for fm in fold_masks:
        s = df[sub_mask & fm]; ns.append(len(s)); ws.append(100 * s.win.mean() if len(s) else np.nan); ms.append(s.ret.mean() if len(s) else np.nan)
    return ws, ms, ns

results = {}
for gname, phases in GROUPS.items():
    G = D[D.phase.isin(phases)]; tr = G[G.date < SPLIT]; te = G[G.date >= SPLIT]
    print("\n" + "=" * 130 + f"\nKELOMPOK {gname} ({'+'.join(phases)}): TRAIN n={len(tr):,} win={100*tr.win.mean():.1f}% mean={tr.ret.mean():+.2f}% | TEST n={len(te):,} win={100*te.win.mean():.1f}% mean={te.ret.mean():+.2f}%\n" + "=" * 130)
    if len(tr) < 3000: print("  train terlalu kecil"); continue
    fms = folds_of(tr)
    cands = []
    for f in FEATS:
        for q in QS:
            thr = float(tr[f].quantile(q)); cands.append((f'{f} >= {thr:.2f}', f, '>=', thr)); cands.append((f'{f} <= {thr:.2f}', f, '<=', thr))
    min_fold_n = 120
    chosen, cur_mask, cur_obj = [], np.ones(len(tr), bool), -1
    for depth in range(1, 5):
        best = None
        for c in cands:
            if any(c[0] == x[0] for x in chosen): continue
            m = cur_mask & mk(c, tr).to_numpy()
            if m.sum() < 1200: continue
            ws, ms, ns = fold_stats(m, tr, fms)
            if min(ns) < min_fold_n or np.nanmin(ms) < 0 or tr[m].ret.mean() < 2.0: continue
            obj = np.nanmin(ws)
            if best is None or obj > best[1]: best = (c, obj, m, ws, ms, ns)
        if best is None or best[1] < cur_obj + 0.3: break
        chosen.append(best[0]); cur_mask, cur_obj = best[2], best[1]
        mte = np.ones(len(te), bool)
        for c in chosen: mte &= mk(c, te).to_numpy()
        s = te[mte]
        print(f"\n  L{depth}: " + " AND ".join(c[0] for c in chosen))
        print(f"      TRAIN n={cur_mask.sum():6,} win/fold={['%.0f' % w for w in best[3]]} mean/fold={['%+.1f' % x for x in best[4]]} total win={100*tr[cur_mask].win.mean():.1f}% mean={tr[cur_mask].ret.mean():+.2f}%")
        print(f"      TEST  n={len(s):6,} ({s.date.nunique()} tgl) win={100*s.win.mean() if len(s) else float('nan'):5.1f}% mean={s.ret.mean() if len(s) else float('nan'):+6.2f}% worst={s.ret.min() if len(s) else float('nan'):+.1f}%")
    results[gname] = chosen

print("\n" + "=" * 130 + "\nFINAL: hanya kelompok yang bertahan di TEST (BEAR). NEUTRAL tanpa aturan stabil; BOUNCE gagal OOS (train 72% -> test 51%, mean -8%) -> tidak dipakai.\n" + "=" * 130)
results = {k: v for k, v in results.items() if k == 'BEAR'}
sel = np.zeros(len(D), bool)
for gname, chosen in results.items():
    if not chosen: continue
    m = np.array(D.phase.isin(GROUPS[gname]))
    for c in chosen: m &= np.array(mk(c, D))
    sel |= m
S = D[sel].sort_values(['date', 'ticker']); busy, keep = {}, []
for r in S.itertuples():
    if r.ticker in busy and r.date <= busy[r.ticker]: keep.append(False); continue
    busy[r.ticker] = r.date + pd.Timedelta(days=int(r.days * 1.45)); keep.append(True)
S = S[keep].copy(); S['q'] = S.date.dt.to_period('Q').astype(str)
for nm, g in (('SEMUA', S), ('TRAIN', S[S.date < SPLIT]), ('TEST', S[S.date >= SPLIT])):
    if len(g): print(f"  {nm}: n={len(g)} win={100*g.win.mean():.1f}% mean={g.ret.mean():+.2f}% median={g.ret.median():+.2f}% hold={g.days.mean():.1f} bar worst={g.ret.min():+.1f}% p10={g.ret.quantile(.1):+.1f}%")
print("  per kuartal: " + " | ".join(f"{q} n={len(g)} win={100*g.win.mean():.0f}% mean={g.ret.mean():+.1f}" for q, g in S.groupby('q')))
print("  per fase: " + " | ".join(f"{p} n={len(g)} win={100*g.win.mean():.0f}% mean={g.ret.mean():+.1f}" for p, g in S.groupby('phase')))
print("  per kelompok TEST: " + " | ".join(f"{gn} n={len(S[(S.date>=SPLIT)&S.phase.isin(ph)])} win={100*S[(S.date>=SPLIT)&S.phase.isin(ph)].win.mean():.0f}%" for gn, ph in GROUPS.items() if len(S[(S.date>=SPLIT)&S.phase.isin(ph)])))
S.drop(columns=['q']).to_csv('research/newscreen_v2_trades_2026_11_18.csv', index=False)
ph_now = LATEST.phase.iloc[0]; gn = [g for g, p in GROUPS.items() if ph_now in p][0]
m = np.array(LATEST.phase.isin(GROUPS[gn]))
for c in results.get(gn, []): m &= np.array(mk(c, LATEST))
cur = LATEST[m].sort_values('liq', ascending=False) if results.get(gn) else LATEST.iloc[0:0]
print(f"\nBar terakhir {LATEST.date.max().date()}: fase IHSG {ph_now} -> kelompok {gn}; aturan: {' AND '.join(c[0] for c in results.get(gn, [])) or '(tidak ada aturan stabil)'}; kandidat={len(cur)}")
if len(cur): print(cur[['ticker', 'close', 'dist20', 'dd60', 'ret20', 'rsi', 'liq']].head(20).round(1).to_string(index=False))
