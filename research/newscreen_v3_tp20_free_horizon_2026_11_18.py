"""research/newscreen_v3_tp20_free_horizon_2026_11_18.py
Horizon BEBAS: TP +20% murni (tanpa SL, tanpa trailing). Ukur waktu sampai high menyentuh entry*1.20 pertama kali (maks 250 bar), hit-rate di horizon 20/40/60/90/120/180 bar bursa,
drawdown (MAE) sebelum TP, dan nasib yang belum kena TP (mark-to-market). Entry open T+1. Fitur = panel newscreen (tanpa BOW/OSB-v2) + regime IHSG.
Pencarian aturan di TRAIN (< 2025-01-01) -> objektif: hit-rate 90 bar MINIMUM di 4 sub-periode; lapor di TEST (>= 2025-01-01).
Hit dihitung dgn HIGH (gap-aware fill di open kalau open>=TP). Biaya 0.4% round trip. Baris hanya dipakai kalau bar ke depan >= horizon yg dievaluasi.
Run: PYTHONUTF8=1 python research/newscreen_v3_tp20_free_horizon_2026_11_18.py   (butuh research/.tmp_newscreen_panel.pkl dari newscreen_tp20_trail_2026_11_18.py PANEL_ONLY=1)
"""
import sqlite3, numpy as np, pandas as pd, warnings
warnings.filterwarnings('ignore')
TPP, COST, HMAX = 20.0, 0.4, 250
HZ = (20, 40, 60, 90, 120, 180)
HS = 90
SPLIT = pd.Timestamp('2025-01-01')

D, LATEST = pd.read_pickle('research/.tmp_newscreen_panel.pkl')
D = D.drop(columns=['ret', 'days', 'win'], errors='ignore')
px = pd.read_sql_query("SELECT ticker,date,open,high,low,close FROM ohlcv_daily ORDER BY ticker,date", sqlite3.connect('mbss_ohlcv.db'), parse_dates=['date'])
rows = []
for t, g in px.groupby('ticker'):
    if t not in set(D.ticker.unique()): continue
    n = len(g); o, h, l, c = (np.concatenate([g[k].to_numpy(float), np.full(HMAX + 2, np.nan)]) for k in ('open', 'high', 'low', 'close'))
    N = n - 1
    if N <= 0: continue
    E = np.arange(1, N + 1)[:, None] + np.arange(HMAX)[None, :]
    O_, H_, L_, C_ = o[E], h[E], l[E], c[E]
    ep = O_[:, 0]; tp = ep * (1 + TPP / 100)
    touch = H_ >= tp[:, None]
    anyhit = touch.any(axis=1); first = np.where(anyhit, touch.argmax(axis=1), -1)           # indeks 0-based bar
    avail = np.minimum(n - np.arange(1, N + 1), HMAX)
    Lmin = np.fmin.accumulate(L_, axis=1)
    out = dict(ticker=t, date=g.date.to_numpy()[:N], avail=avail, hit_day=np.where(anyhit, first + 1, np.nan))
    fill = np.where(anyhit, np.maximum(O_[np.arange(N), np.maximum(first, 0)], tp), np.nan)
    for hz in HZ:
        hit_h = anyhit & (first + 1 <= hz)
        endi = np.where(hit_h, first, hz - 1)
        ret_h = np.where(hit_h, (fill / ep - 1) * 100, (C_[:, hz - 1] / ep - 1) * 100) - COST
        out[f'out{hz}'] = np.where(avail >= hz, ret_h, np.nan)
        out[f'mae{hz}'] = np.where(avail >= hz, (Lmin[np.arange(N), endi] / ep - 1) * 100, np.nan)
    rows.append(pd.DataFrame(out))
Y = pd.concat(rows, ignore_index=True); Y['date'] = pd.to_datetime(Y.date)
D = D.merge(Y, on=['ticker', 'date'], how='inner')
D['hit90'] = ((D.hit_day <= HS)).astype(float)
D = D[D.avail >= HS].copy()
print(f"Panel: {len(D):,} baris (bar ke depan >= {HS}), {D.ticker.nunique()} ticker, {D.date.min().date()}..{D.date.max().date()}")

def horizon_table(df, label):
    out = []
    for hz in HZ:
        s = df[df.avail >= hz]
        if len(s) < 50: continue
        hit = (s.hit_day <= hz)
        nh = s[~hit]
        out.append(f"{hz}b:{100*hit.mean():.0f}%")
    hd = df.hit_day.dropna()
    print(f"  {label:34s} n={len(df):7,} | hit-rate " + " ".join(out) + f" | median hari ke-TP={hd.median():.0f} p75={hd.quantile(.75):.0f}")

print("\nBASE RATE semua baris:")
horizon_table(D, 'semua')
for ph, g in D.groupby('phase'): horizon_table(g, f'fase {ph}')
tr, te = D[D.date < SPLIT], D[D.date >= SPLIT]
print(f"\nTRAIN {len(tr):,} baris ({tr.date.min().date()}..{tr.date.max().date()}) | TEST {len(te):,} ({te.date.min().date()}..{te.date.max().date()})")

FEATS = ['dist20', 'dist50', 'dd60', 'dd120', 'ret5', 'ret10', 'ret20', 'ret60', 'rsi', 'macd', 'pctb', 'atr', 'rng10', 'volx', 'clv', 'body', 'gap', 'up5', 'liq', 'rs20', 'brd20', 'brd50', 'ihsg_dd100', 'ihsg_r20']
QS = (0.1, 0.2, 0.3, 0.5, 0.7, 0.8, 0.9)
cands = []
for f in FEATS:
    for q in QS:
        thr = float(tr[f].quantile(q)); cands.append((f'{f} >= {thr:.2f}', f, '>=', thr)); cands.append((f'{f} <= {thr:.2f}', f, '<=', thr))
for ph in D.phase.unique(): cands.append((f'fase != {ph}', 'phase', '!=', ph))

def mk(c, df):
    _, f, op, thr = c; x = df[f]
    return np.array((x >= thr) if op == '>=' else (x <= thr) if op == '<=' else (x != thr))

ud = np.sort(tr.date.unique()); fms = [np.array(tr.date.isin(cc)) for cc in np.array_split(ud, 4)]
hit_tr = np.array(tr.hit90); ret_tr = np.array(tr.out90)
chosen, cur, cur_obj = [], np.ones(len(tr), bool), -1
print("\n" + "=" * 130 + f"\nGREEDY (train): maksimalkan hit-rate {HS} bar MINIMUM antar 4 sub-periode; syarat n>=1200, tiap fold n>=120, mean ret90 >= +3%\n" + "=" * 130)
for depth in range(1, 6):
    best = None
    for c in cands:
        if any(c[0] == x[0] for x in chosen): continue
        m = cur & mk(c, tr)
        if m.sum() < 1200 or ret_tr[m].mean() < 3.0: continue
        ws = [hit_tr[m & fm].mean() * 100 if (m & fm).sum() >= 120 else np.nan for fm in fms]
        if np.isnan(ws).any(): continue
        obj = min(ws)
        if best is None or obj > best[1]: best = (c, obj, m, ws)
    if best is None or best[1] < cur_obj + 0.3: break
    chosen.append(best[0]); cur, cur_obj = best[2], best[1]
    mte = np.ones(len(te), bool)
    for c in chosen: mte &= mk(c, te)
    s = te[mte]
    print(f"\n  L{depth}: " + " AND ".join(c[0] for c in chosen))
    print(f"      TRAIN n={cur.sum():6,} hit{HS}/fold={['%.0f' % w for w in best[3]]} total={100*hit_tr[cur].mean():.1f}% ret{HS}={ret_tr[cur].mean():+.2f}%")
    print(f"      TEST  n={len(s):6,} ({s.date.nunique()} tgl) hit{HS}={100*s.hit90.mean() if len(s) else float('nan'):5.1f}% ret{HS}={s.out90.mean() if len(s) else float('nan'):+6.2f}%")

if not chosen: raise SystemExit("tidak ada aturan lolos")
sel = np.ones(len(D), bool)
for c in chosen: sel &= mk(c, D)
S = D[sel].sort_values(['date', 'ticker']); busy, keep = {}, []
for r in S.itertuples():
    if r.ticker in busy and r.date <= busy[r.ticker]: keep.append(False); continue
    busy[r.ticker] = r.date + pd.Timedelta(days=int((r.hit_day if r.hit_day == r.hit_day and r.hit_day <= HS else HS) * 1.45)); keep.append(True)
S = S[keep].copy()
print("\n" + "=" * 130 + "\nATURAN FINAL (dedup 1 posisi/ticker, slot terkunci sampai TP / %d bar)\n" % HS + "=" * 130)
for nm, g in (('SEMUA', S), ('TRAIN', S[S.date < SPLIT]), ('TEST', S[S.date >= SPLIT])):
    if len(g) == 0: continue
    horizon_table(g, nm)
    nh = g[g.hit_day.isna() | (g.hit_day > HS)]
    print(f"      TP tercapai dlm {HS} bar: {100*g.hit90.mean():.1f}% | MAE median s/d TP={g.mae90.median():+.1f}% p10={g.mae90.quantile(.1):+.1f}% | yg belum kena TP (n={len(nh)}): ret{HS} mean={nh.out90.mean():+.1f}% median={nh.out90.median():+.1f}% worst={nh.out90.min():+.1f}% | ret{HS} rata2 semua={g.out90.mean():+.2f}%")
S['q'] = S.date.dt.to_period('Q').astype(str)
print("  per kuartal: " + " | ".join(f"{q} n={len(g)} hit90={100*g.hit90.mean():.0f}% ret90={g.out90.mean():+.1f}" for q, g in S.groupby('q')))
print("  per fase: " + " | ".join(f"{p} n={len(g)} hit90={100*g.hit90.mean():.0f}%" for p, g in S.groupby('phase')))
S.drop(columns=['q']).to_csv('research/newscreen_v3_trades_2026_11_18.csv', index=False)
m = np.ones(len(LATEST), bool)
for c in chosen: m &= mk(c, LATEST)
cur_pick = LATEST[m].sort_values('liq', ascending=False)
print(f"\nBar terakhir {LATEST.date.max().date()} (fase IHSG {LATEST.phase.iloc[0]}): kandidat={len(cur_pick)}")
if len(cur_pick): print(cur_pick[['ticker', 'close', 'dist20', 'dd60', 'dd120', 'ret20', 'rsi', 'liq']].head(25).round(1).to_string(index=False))
