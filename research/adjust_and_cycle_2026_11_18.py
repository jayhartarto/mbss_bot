"""research/adjust_and_cycle_2026_11_18.py
A) Apakah parameter OSB-v2 bisa digeser utk target TP+20% horizon bebas (diukur hit-rate & return di 90 bar)? Sinyal OSB-v2 diregenerasi dari OHLCV (tanpa filter FF; gate IHSG dd100 dari engine/ihsg_regime).
   Pencarian di TRAIN (sinyal < 2026-01-01 = episode 2025), laporan di TEST (>= 2026-01-01 = episode 2026). BOW: hanya sinyal yg ada + pengetatan (parameternya tak bisa diregenerasi murah).
B) Strategi SIKLUS ("riding the wave"): beli di open T+1 (P0); jual saat high >= entry*(1+g) (g=3/5%); beli lagi kalau harga turun >= dip% (2) dari HARGA JUAL (limit; tidak di bar yg sama dgn penjualan);
   ulangi sampai harga menyentuh P0*(1+Tend) (Tend=10/20, atau tanpa target) atau horizon 90 bar habis (posisi terbuka di-mark-to-market). Biaya 0.4% per round-trip. Modal digabung (compounding).
   Pembanding pada episode yg SAMA: hold-sampai-TP20, hold-sampai-TP10 (tanpa SL, MTM di bar 90).
Run: PYTHONUTF8=1 python research/adjust_and_cycle_2026_11_18.py   (butuh research/.tmp_ihsg_regime_full.pkl)
"""
import sqlite3, itertools, numpy as np, pandas as pd, warnings
warnings.filterwarnings('ignore')
COST, TPP, HMAX, H1 = 0.4, 20.0, 250, 90
SPLIT = pd.Timestamp('2026-01-01')
R = pd.read_pickle('research/.tmp_ihsg_regime_full.pkl'); R.index = pd.to_datetime(R.index).normalize()
R['ret1'] = R.close.pct_change() * 100
px = pd.read_sql_query("SELECT ticker,date,open,high,low,close,volume FROM ohlcv_daily ORDER BY ticker,date", sqlite3.connect('mbss_ohlcv.db'), parse_dates=['date'])
A, frames = {}, []
for t, g in px.groupby('ticker'):
    n = len(g)
    if n < 150: continue
    o, h, l, c, v = (g[k].astype(float) for k in ('open', 'high', 'low', 'close', 'volume'))
    d = c.diff(); up = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean(); dn = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    F = pd.DataFrame(dict(ticker=t, date=g.date, rsi=100 - 100 / (1 + up / dn.replace(0, np.nan)), dist20=(c / c.rolling(20).mean() - 1) * 100,
                          dd60=(c / c.rolling(60).max() - 1) * 100, liq=(c * v).rolling(20).mean() / 1e9, ret1=c.pct_change() * 100, green=(c > o)))
    A[t] = dict(d=g.date.values, o=o.to_numpy(), h=h.to_numpy(), l=l.to_numpy(), c=c.to_numpy())
    pad = lambda x: np.concatenate([x.to_numpy(), np.full(HMAX + 2, np.nan)])
    O, Hh, L, C = pad(o), pad(h), pad(l), pad(c)
    N = n - 1; E = np.arange(1, N + 1)[:, None] + np.arange(HMAX)[None, :]
    O_, H_, C_ = O[E], Hh[E], C[E]
    ep = O_[:, 0]; tp = ep * (1 + TPP / 100); touch = H_ >= tp[:, None]; anyhit = touch.any(axis=1); first = np.where(anyhit, touch.argmax(axis=1), -1)
    avail = np.minimum(n - np.arange(1, N + 1), HMAX)
    fill = np.where(anyhit, np.maximum(O_[np.arange(N), np.maximum(first, 0)], tp), np.nan)
    hit90 = anyhit & (first + 1 <= H1)
    out90 = np.where(hit90, (fill / ep - 1) * 100, (C_[:, H1 - 1] / ep - 1) * 100) - COST
    F = F.iloc[:N].copy(); F['avail'] = avail; F['hit_day'] = np.where(anyhit, first + 1, np.nan)
    F['hit90'] = hit90.astype(float); F['out90'] = np.where(avail >= H1, out90, np.nan)
    frames.append(F)
P = pd.concat(frames, ignore_index=True); P['date'] = pd.to_datetime(P.date)
rg = R.reindex(P.date.unique()).sort_index().ffill()
P['i_ret1'] = P.date.map(rg.ret1); P['i_dd100'] = P.date.map(rg.dd_100) * 100; P['phase'] = P.date.map(rg.regime_phase)
P = P.dropna(subset=['rsi', 'dist20', 'i_dd100'])
P90 = P[P.avail >= H1]
print(f"Panel {len(P):,} baris; dgn horizon 90 bar: {len(P90):,} ({P90.date.min().date()}..{P90.date.max().date()})")

# ===================================================== A. parameter OSB-v2
print("\n" + "=" * 130 + "\nA. PARAMETER OSB-v2 (regenerasi) -> target TP+20%, horizon bebas diukur di 90 bar\n" + "=" * 130)
S0 = P90[(P90.i_dd100 <= -5) & P90.rsi.between(10, 55) & (P90.dist20 <= -4)].copy()
cols = {k: S0[k].to_numpy() for k in ('rsi', 'dist20', 'liq', 'i_dd100', 'green', 'ret1', 'i_ret1', 'hit90', 'out90', 'hit_day')}
grind = (S0.phase == 'BEARISH_GRIND').to_numpy(); istr = (S0.date < SPLIT).to_numpy(); dates = S0.date.to_numpy()

def metrics(m):
    out = {}
    for nm, sel in (('tr', m & istr), ('te', m & ~istr)):
        n = int(sel.sum())
        out[nm] = (n, len(np.unique(dates[sel])) if n else 0, 100 * cols['hit90'][sel].mean() if n else np.nan, cols['out90'][sel].mean() if n else np.nan)
    return out

def build(rlo, rhi, dist, liq, gate, trig, gx):
    m = (cols['rsi'] >= rlo) & (cols['rsi'] < rhi) & (cols['dist20'] <= dist) & (cols['liq'] >= liq) & (cols['i_dd100'] <= gate)
    if trig == 'strict': m &= cols['green'] & (cols['ret1'] <= cols['i_ret1'])
    elif trig == 'green': m &= cols['green']
    if gx: m &= ~grind
    return m

PROD = (20, 40, 10 * -1, 1.0, -10, 'strict', True)
def fmt(k, r):
    return f"rsi[{k[0]},{k[1]}) dist20<={k[2]} liq>={k[3]} gate<={k[4]} trig={k[5]} exGrind={k[6]} | TRAIN n={r['tr'][0]:5} tgl={r['tr'][1]:3} hit90={r['tr'][2]:5.1f}% ret90={r['tr'][3]:+6.2f}% | TEST n={r['te'][0]:5} tgl={r['te'][1]:3} hit90={r['te'][2]:5.1f}% ret90={r['te'][3]:+6.2f}%"
rp = metrics(build(*PROD)); print("PRODUKSI (regenerasi):\n  " + fmt(PROD, rp))
# sanity vs lane list
EP = pd.read_pickle('research/bull5/lane_episodes_open.pkl')
mm = build(*PROD); regen = set(zip(S0.ticker[mm], S0.date[mm])); lane = set(zip(EP['OSBv2'].ticker, pd.to_datetime(EP['OSBv2'].date)))
print(f"  regenerasi {len(regen)} sinyal-baris vs daftar lane OSB-v2 {len(lane)}: overlap {len(regen & lane)} (beda krn FF-exclusion/tier MACD/panel)")
grid = {'rlo': (15, 20, 25), 'rhi': (40, 45, 50), 'dist': (-6, -8, -10, -12, -15), 'liq': (0.5, 1.0, 3.0), 'gate': (-7, -10, -12, -15), 'trig': ('strict', 'green', 'none'), 'gx': (True, False)}
res = []
for k in itertools.product(*grid.values()):
    r = metrics(build(*k)); res.append((k, r))
print("\nSENSITIVITAS marginal (parameter lain = produksi):")
for pname, vals in grid.items():
    pi = list(grid).index(pname); row = []
    for vv in vals:
        k = list(PROD); k[pi] = vv; r = metrics(build(*k)); row.append(f"{vv}: tr {r['tr'][2]:.0f}%/{r['tr'][3]:+.1f} (n{r['tr'][0]}) te {r['te'][2]:.0f}%/{r['te'][3]:+.1f} (n{r['te'][0]})")
    print(f"  {pname:5s} " + " | ".join(row))
ok = [(k, r) for k, r in res if r['tr'][0] >= 150 and r['tr'][1] >= 15 and r['te'][0] >= 30]
print("\nTOP 6 menurut ret90 TRAIN (n_train>=150, >=15 tgl) -> hasil TEST:")
for k, r in sorted(ok, key=lambda x: -x[1]['tr'][3])[:6]: print("  " + fmt(k, r))
print("TOP 6 menurut hit90 TRAIN:")
for k, r in sorted(ok, key=lambda x: -x[1]['tr'][2])[:6]: print("  " + fmt(k, r))
tr_rank = pd.Series({i: r['tr'][3] for i, (k, r) in enumerate(ok)}).rank(ascending=False); te_rank = pd.Series({i: r['te'][3] for i, (k, r) in enumerate(ok)}).rank(ascending=False)
print(f"korelasi peringkat ret90 train vs test seluruh grid ({len(ok)} kombinasi): {tr_rank.corr(te_rank):+.2f}  (0 = tidak ada konsistensi)")

# BOW: sinyal yg ada + pengetatan
print("\nBOW (sinyal lane yg ada, 2025-01..2026-10) free horizon 90 bar:")
B = EP['BOW'][['ticker', 'date']].drop_duplicates().assign(date=lambda x: pd.to_datetime(x.date)).merge(P90, on=['ticker', 'date'], how='inner')
cut = B.date.median()
for nm, m in (('semua', np.ones(len(B), bool)), ('dd60<=-20 & rsi>=31', ((B.dd60 <= -20) & (B.rsi >= 31)).to_numpy()), ('dd60<=-30', (B.dd60 <= -30).to_numpy()), ('rsi<35', (B.rsi < 35).to_numpy())):
    a, b = B[m & (B.date < cut).to_numpy()], B[m & (B.date >= cut).to_numpy()]
    print(f"  {nm:22s} paruh awal n={len(a):3} hit90={100*a.hit90.mean() if len(a) else np.nan:5.1f}% ret90={a.out90.mean() if len(a) else np.nan:+6.2f}% | paruh akhir n={len(b):3} hit90={100*b.hit90.mean() if len(b) else np.nan:5.1f}% ret90={b.out90.mean() if len(b) else np.nan:+6.2f}%")

# ===================================================== B. strategi siklus
print("\n" + "=" * 130 + "\nB. STRATEGI SIKLUS (ambil untung g%, masuk lagi turun >=2% dari harga jual, sampai +Tend% dari entry awal), horizon 90 bar\n" + "=" * 130)
def hold_tp(a, e, tp_pct):
    ep = a['o'][e]; tp = ep * (1 + tp_pct / 100)
    for k in range(H1):
        j = e + k
        if a['h'][j] >= tp: return (max(a['o'][j], tp) / ep - 1) * 100 - COST, 1
    return (a['c'][e + H1 - 1] / ep - 1) * 100 - COST, 0

def cycle(a, e, g, dip, tend):
    cap = 1.0; pos = True; ent = a['o'][e]; P0 = ent; level = P0 * (1 + tend / 100) if tend else np.inf
    last_sale = None; sold_bar = -1; cyc = 0; inmkt = 0; ended = False
    for k in range(H1):
        j = e + k; o, h, l = a['o'][j], a['h'][j], a['l'][j]
        if pos:
            inmkt += 1; tp = ent * (1 + g / 100)
            if o >= tp or h >= tp:
                sell = max(o, tp) if o >= tp else tp
                cap *= (sell / ent) * (1 - COST / 100); cyc += 1; pos = False; last_sale = sell; sold_bar = k
                if sell >= level: ended = True; break
        else:
            if h >= level: ended = True; break
            if k > sold_bar:
                rl = last_sale * (1 - dip / 100)
                if o <= rl: pos = True; ent = o
                elif l <= rl: pos = True; ent = rl
    holding = pos and not ended
    if holding: cap *= (a['c'][e + H1 - 1] / ent) * (1 - COST / 100)
    return (cap - 1) * 100, cyc, inmkt, ended, holding

sigs = pd.concat([EP['OSBv2'].assign(lane='OSB-v2'), EP['BOW'].assign(lane='BOW')])[['ticker', 'date', 'lane']].drop_duplicates()
sigs['date'] = pd.to_datetime(sigs.date)
sigs = sigs.merge(P90[['ticker', 'date', 'dd60', 'rsi']], on=['ticker', 'date'], how='inner').sort_values(['date', 'ticker'])
eps = []
for lane, g in sigs.groupby('lane'):
    busy = {}
    for r in g.itertuples():
        if r.ticker in busy and r.date <= busy[r.ticker]: continue
        a = A[r.ticker]; i = int(np.searchsorted(a['d'], np.datetime64(r.date)))
        if i + 1 + H1 > len(a['d']): continue
        busy[r.ticker] = r.date + pd.Timedelta(days=int(H1 * 1.45))
        eps.append(dict(lane=lane, ticker=r.ticker, date=r.date, e=i + 1, filt=bool(r.dd60 <= -20 and r.rsi >= 31)))
E = pd.DataFrame(eps)
SETS = {'OSB-v2 semua': E[E.lane == 'OSB-v2'], 'BOW semua': E[E.lane == 'BOW'], 'OSB+BOW terfilter (dd60<=-20 & RSI>=31)': E[E.filt]}
print(f"episode: OSB-v2 {sum(E.lane=='OSB-v2')}, BOW {sum(E.lane=='BOW')}, terfilter {int(E.filt.sum())} (hanya sinyal dgn >=90 bar ke depan)")
for sname, S in SETS.items():
    print(f"\n--- {sname} (n={len(S)}) ---")
    print(f"  {'strategi':40s} {'mean%':>7} {'med%':>7} {'win%':>6} {'selesai%':>8} {'siklus':>6} {'hari_pasar':>10} {'msh_pegang%':>11} {'MTM_pegang':>10} {'worst':>7}")
    rows = []
    for tp_pct in (10, 20):
        rr = [hold_tp(A[r.ticker], r.e, tp_pct) for r in S.itertuples()]
        x = np.array([q[0] for q in rr]); hit = np.array([q[1] for q in rr])
        rows.append((f'hold sampai TP{tp_pct} (tanpa SL)', x, 100 * hit.mean(), np.nan, np.nan, 100 * (1 - hit.mean()), x[hit == 0].mean() if (hit == 0).any() else np.nan))
    for g, tend in itertools.product((3, 5), (10, 20, None)):
        rr = [cycle(A[r.ticker], r.e, g, 2.0, tend) for r in S.itertuples()]
        x = np.array([q[0] for q in rr]); cy = np.array([q[1] for q in rr]); im = np.array([q[2] for q in rr]); en = np.array([q[3] for q in rr]); ho = np.array([q[4] for q in rr])
        rows.append((f'siklus g={g}% dip=2% sampai +{tend if tend else "-"}%', x, 100 * en.mean(), cy.mean(), im.mean(), 100 * ho.mean(), x[ho].mean() if ho.any() else np.nan))
    for nm, x, sel, cy, im, ho, mtm in rows:
        print(f"  {nm:40s} {x.mean():+7.2f} {np.median(x):+7.2f} {100*(x>0).mean():6.1f} {sel:8.1f} {cy if cy==cy else float('nan'):6.1f} {im if im==im else float('nan'):10.1f} {ho:11.1f} {mtm if mtm==mtm else float('nan'):+10.1f} {x.min():+7.1f}")
