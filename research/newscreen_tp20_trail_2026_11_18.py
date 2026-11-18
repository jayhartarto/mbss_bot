"""research/newscreen_tp20_trail_2026_11_18.py
Screen BARU (tidak memakai sinyal BOW/OSB-v2): cari aturan entry dari fitur OHLCV + regime IHSG + breadth pasar di SELURUH universe likuid,
dengan exit tetap: TP +20%, trailing 10% dari high setelah profit +12% (aktif), tanpa SL, maks 42 hari bursa (~60 hari kalender), biaya 0.4%.
Target: win rate (ret bersih > 0) setinggi mungkin dgn mean >= +3%. Aturan DICARI di TRAIN (< 2025-07-01) dan DILAPORKAN di TEST (>= 2025-07-01).
Entry: open T+1 setelah sinyal di close T. Hanya baris dgn jendela 42 bar lengkap. Intrabar konservatif (stop dicek sebelum TP, gap-aware).
Run: PYTHONUTF8=1 python research/newscreen_tp20_trail_2026_11_18.py
"""
import os, json, sqlite3, warnings
import numpy as np, pandas as pd
warnings.filterwarnings('ignore')

H, COST, TP, ARM, TRAIL = 42, 0.4, 20.0, 12.0, 10.0
SPLIT = pd.Timestamp('2025-07-01')
MIN_ROWS_TRAIN, MIN_DATES, MIN_MEAN = 2500, 120, 3.0

R = pd.read_pickle('research/.tmp_ihsg_regime_full.pkl'); R.index = pd.to_datetime(R.index).normalize()
HSC = set()
if os.path.exists('hsc_list.json'):
    j = json.load(open('hsc_list.json')); HSC = set(j if isinstance(j, list) and j and isinstance(j[0], str) else [])

px = pd.read_sql_query("SELECT ticker,date,open,high,low,close,volume FROM ohlcv_daily ORDER BY ticker,date", sqlite3.connect('mbss_ohlcv.db'), parse_dates=['date'])
frames = []
for t, g in px.groupby('ticker'):
    if t in HSC or len(g) < 150: continue
    g = g.reset_index(drop=True)
    o, h, l, c, v = (g[k].astype(float) for k in ('open', 'high', 'low', 'close', 'volume'))
    d = c.diff(); up = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean(); dn = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    ma20 = c.rolling(20).mean(); sd20 = c.rolling(20).std()
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    F = pd.DataFrame(dict(
        ticker=t, date=g.date, close=c,
        dist20=(c / ma20 - 1) * 100, dist50=(c / c.rolling(50).mean() - 1) * 100,
        dd60=(c / c.rolling(60).max() - 1) * 100, dd120=(c / c.rolling(120).max() - 1) * 100,
        ret5=c.pct_change(5) * 100, ret10=c.pct_change(10) * 100, ret20=c.pct_change(20) * 100, ret60=c.pct_change(60) * 100,
        rsi=100 - 100 / (1 + up / dn.replace(0, np.nan)),
        macd=(c.rolling(12).mean() - c.rolling(26).mean()) / c * 100,
        pctb=(c - (ma20 - 2 * sd20)) / (4 * sd20),
        atr=tr.rolling(14).mean() / c * 100,
        rng10=(h.rolling(10).max() - l.rolling(10).min()) / c * 100,
        volx=v / v.rolling(20).mean(),
        clv=(c - l) / (h - l).replace(0, np.nan),
        body=(c / o - 1) * 100, gap=(o / c.shift() - 1) * 100,
        up5=(d > 0).rolling(5).sum(),
        liq=(c * v).rolling(20).median() / 1e9))
    # outcome (vektor): entry open i+1, jendela H bar
    n = len(g); N = n - H - 1
    ret = np.full(n, np.nan); days = np.full(n, np.nan)
    if N > 0:
        E = np.arange(1, N + 1)[:, None] + np.arange(H)[None, :]      # entry di i+1 untuk i=0..N-1
        O_, H_, L_, C_ = o.to_numpy()[E], h.to_numpy()[E], l.to_numpy()[E], c.to_numpy()[E]
        ep = O_[:, 0]; tp = ep * (1 + TP / 100)
        runh = ep.copy(); done = np.zeros(N, bool); res = np.zeros(N); dd_ = np.zeros(N)
        for k in range(H):
            armed = (runh / ep - 1) * 100 >= ARM
            stop = np.where(armed, runh * (1 - TRAIL / 100), -np.inf)
            hs = ~done & armed & (L_[:, k] <= stop)
            res[hs] = (np.minimum(O_[hs, k], stop[hs]) / ep[hs] - 1) * 100; dd_[hs] = k + 1; done |= hs
            ht = ~done & (H_[:, k] >= tp)
            res[ht] = (np.maximum(O_[ht, k], tp[ht]) / ep[ht] - 1) * 100; dd_[ht] = k + 1; done |= ht
            runh = np.maximum(runh, H_[:, k])
        res[~done] = (C_[~done, H - 1] / ep[~done] - 1) * 100; dd_[~done] = H
        ret[:N] = res - COST; days[:N] = dd_
    F['ret'] = ret; F['days'] = days
    frames.append(F)
D = pd.concat(frames, ignore_index=True)
D = D[(D.liq >= 1.0) & (D.close >= 100)].copy()
D['date'] = pd.to_datetime(D.date)
# regime + breadth
rg = R.reindex(D.date.unique()).sort_index().ffill()
D['phase'] = D.date.map(rg.regime_phase); D['ihsg_dd100'] = D.date.map(rg.dd_100) * 100; D['ihsg_r20'] = D.date.map(rg.ret_20)
D['rs20'] = D.ret20 - D.ihsg_r20
D['brd20'] = D.groupby('date').dist20.transform(lambda s: (s > 0).mean() * 100)
D['brd50'] = D.groupby('date').dist50.transform(lambda s: (s > 0).mean() * 100)
D = D.dropna(subset=['dist50', 'dd120', 'ret60', 'rsi', 'atr', 'volx', 'phase'])
LATEST = D[D.date == D.date.max()].copy()
D = D.dropna(subset=['ret']).copy(); D['win'] = (D.ret > 0).astype(float)
pd.to_pickle((D, LATEST), 'research/.tmp_newscreen_panel.pkl')
if os.environ.get('PANEL_ONLY'): raise SystemExit("panel saved")
print(f"Panel: {len(D):,} baris, {D.ticker.nunique()} ticker, {D.date.min().date()}..{D.date.max().date()}")
tr, te = D[D.date < SPLIT], D[D.date >= SPLIT]
print(f"Baseline semua baris (exit TP20+trail): TRAIN n={len(tr):,} win={100*tr.win.mean():.1f}% mean={tr.ret.mean():+.2f}% | TEST n={len(te):,} win={100*te.win.mean():.1f}% mean={te.ret.mean():+.2f}%")
print("Baseline per fase (semua):"); print(D.groupby('phase').agg(n=('win', 'size'), win=('win', lambda s: 100 * s.mean()), mean=('ret', 'mean')).round(1).to_string())

FEATS = ['dist20', 'dist50', 'dd60', 'dd120', 'ret5', 'ret10', 'ret20', 'ret60', 'rsi', 'macd', 'pctb', 'atr', 'rng10', 'volx', 'clv', 'body', 'gap', 'up5',
         'liq', 'rs20', 'brd20', 'brd50', 'ihsg_dd100', 'ihsg_r20']
QS = (0.1, 0.2, 0.3, 0.5, 0.7, 0.8, 0.9)
cands = []
for f in FEATS:
    for q in QS:
        thr = float(tr[f].quantile(q))
        cands.append((f'{f} >= {thr:.2f}', f, '>=', thr)); cands.append((f'{f} <= {thr:.2f}', f, '<=', thr))
for ph in D.phase.unique(): cands.append((f'fase != {ph}', 'phase', '!=', ph))

def mk(c, df):
    _, f, op, thr = c; x = df[f]
    return (x >= thr) if op == '>=' else (x <= thr) if op == '<=' else (x != thr)

def evaluate(mask, df):
    s = df[mask]; return len(s), s.win.mean() * 100 if len(s) else np.nan, s.ret.mean() if len(s) else np.nan, s.date.nunique()

print("\n" + "=" * 130 + "\nGREEDY RULE SEARCH (train): maksimalkan win rate, support >= %d baris & >= %d tanggal, mean >= +%.0f%%\n" % (MIN_ROWS_TRAIN, MIN_DATES, MIN_MEAN) + "=" * 130)
chosen, mask_tr = [], np.ones(len(tr), bool)
cur = tr.win.mean() * 100
for depth in range(1, 6):
    best = None
    for c in cands:
        if any(c[0] == x[0] for x in chosen): continue
        m = mask_tr & mk(c, tr).to_numpy()
        n, w, mu, nd = evaluate(m, tr)
        if n < MIN_ROWS_TRAIN or nd < MIN_DATES or mu < MIN_MEAN: continue
        if best is None or w > best[1]: best = (c, w, mu, n, m)
    if best is None or best[1] < cur + 0.4: break
    chosen.append(best[0]); mask_tr = best[4]; cur = best[1]
    mte = np.ones(len(te), bool)
    for c in chosen: mte &= mk(c, te).to_numpy()
    n2, w2, mu2, nd2 = evaluate(mte, te)
    print(f"\n  L{depth}: " + " AND ".join(c[0] for c in chosen))
    print(f"      TRAIN n={best[3]:6,} win={best[1]:5.1f}% mean={best[2]:+6.2f}%   |   TEST n={n2:6,} win={w2:5.1f}% mean={mu2:+6.2f}% ({nd2} tgl)")

if not chosen: raise SystemExit("tidak ada aturan lolos")
allm = np.ones(len(D), bool)
for c in chosen: allm &= mk(c, D).to_numpy()
S = D[allm].sort_values(['date', 'ticker']).copy()
# tradable: 1 posisi/ticker (kunci sampai exit)
busy, keep = {}, []
for r in S.itertuples():
    if r.ticker in busy and r.date <= busy[r.ticker]: keep.append(False); continue
    busy[r.ticker] = r.date + pd.Timedelta(days=int(r.days * 1.45)); keep.append(True)
S = S[keep]
S['q'] = S.date.dt.to_period('Q').astype(str)
print("\n" + "=" * 130 + f"\nATURAN FINAL (dedup 1 posisi/ticker): n={len(S)}  win={100*S.win.mean():.1f}%  mean={S.ret.mean():+.2f}%  median={S.ret.median():+.2f}%  hold={S.days.mean():.1f} bar  worst={S.ret.min():+.1f}%  p10={S.ret.quantile(.1):+.1f}%  sinyal/bulan={len(S)/((S.date.max()-S.date.min()).days/30.4):.1f}\n" + "=" * 130)
for nm, g in (('TRAIN', S[S.date < SPLIT]), ('TEST', S[S.date >= SPLIT])):
    print(f"  {nm}: n={len(g)} win={100*g.win.mean():.1f}% mean={g.ret.mean():+.2f}% median={g.ret.median():+.2f}% worst={g.ret.min():+.1f}%")
print("  per kuartal: " + " | ".join(f"{q} n={len(g)} win={100*g.win.mean():.0f}% mean={g.ret.mean():+.1f}" for q, g in S.groupby('q')))
print("  per fase IHSG: " + " | ".join(f"{p} n={len(g)} win={100*g.win.mean():.0f}% mean={g.ret.mean():+.1f}" for p, g in S.groupby('phase')))
EP = pd.read_pickle('research/bull5/lane_episodes_open.pkl')
old = set(map(tuple, pd.concat([EP['BOW'][['ticker', 'date']], EP['OSBv2'][['ticker', 'date']]]).assign(date=lambda x: pd.to_datetime(x.date)).itertuples(index=False, name=None)))
ov = sum((r.ticker, r.date) in old for r in S.itertuples())
print(f"  overlap dgn sinyal BOW/OSB-v2 (ticker+tanggal sama): {ov}/{len(S)} = {100*ov/len(S):.0f}%")
S.drop(columns=['q']).to_csv('research/newscreen_trades_2026_11_18.csv', index=False)

m = np.ones(len(LATEST), bool)
for c in chosen: m &= mk(c, LATEST).to_numpy()
cur_pick = LATEST[m].sort_values('liq', ascending=False)
print(f"\nKandidat di bar terakhir {LATEST.date.max().date()} (fase IHSG {LATEST.phase.iloc[0]}): {len(cur_pick)}")
if len(cur_pick): print(cur_pick[['ticker', 'close', 'dist20', 'dd60', 'ret20', 'rsi', 'liq']].head(25).round(1).to_string(index=False))
