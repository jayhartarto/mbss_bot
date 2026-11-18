"""research/hold60_filter_trail_2026_11_18.py
Tujuan: pick OSB-v2 + BOW, hold maks 60 hari kalender, TP 20%, trailing boleh sampai 10% -> cari filter (termasuk regime IHSG) dgn WIN RATE tertinggi,
plus exit strategy saat harga memudar dari puncak.
Entry open T+1. Win = return bersih (setelah biaya 0.4%) > 0. Hanya trade dgn jendela 60 hari lengkap. 1 posisi/ticker/lane.
Urutan intrabar KONSERVATIF: stop dicek dulu sebelum TP; stop gap-aware (open di bawah stop -> fill di open). Fade berbasis close -> keluar di OPEN hari berikutnya.
Pemilihan exit & filter dilakukan di TRAIN (60% trade pertama menurut tanggal sinyal), dilaporkan di TEST (40% terakhir) -> hindari overfit.
Run: PYTHONUTF8=1 python research/hold60_filter_trail_2026_11_18.py
"""
import sys, os, sqlite3, itertools, warnings
import numpy as np, pandas as pd
warnings.filterwarnings('ignore')
sys.path.insert(0, os.getcwd())

COST, LAST, TP = 0.4, pd.Timestamp('2026-11-16'), 20.0

# ---------- IHSG regime (kausal, engine yang sama dgn produksi)
cache = 'research/.tmp_ihsg_regime_full.pkl'
if os.path.exists(cache):
    R = pd.read_pickle(cache)
else:
    import yfinance as yf
    from engine.ihsg_regime import _build_regime
    h = yf.Ticker('^JKSE').history(period='max', timeout=30)
    h.index = h.index.tz_localize(None)
    R = _build_regime(h).set_index('date')[['close', 'dd_100', 'ret_20', 'regime_phase']]
    R.to_pickle(cache)
R.index = pd.to_datetime(R.index).normalize()
print("IHSG regime", R.index.min().date(), R.index.max().date(), R.regime_phase.value_counts().to_dict())

# ---------- OHLCV + fitur
px = pd.read_sql_query("SELECT ticker,date,open,high,low,close,volume FROM ohlcv_daily ORDER BY ticker,date",
                       sqlite3.connect('mbss_ohlcv.db'), parse_dates=['date'])
A = {}
for t, g in px.groupby('ticker'):
    c = g.close.astype(float); o = g.open.astype(float); h = g.high.astype(float); l = g.low.astype(float)
    d = c.diff(); up = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean(); dn = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    rsi = 100 - 100 / (1 + up / dn.replace(0, np.nan))
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    A[t] = dict(d=g.date.values, o=o.to_numpy(), h=h.to_numpy(), l=l.to_numpy(), c=c.to_numpy(), sma5=c.rolling(5).mean().to_numpy(),
                f=pd.DataFrame(dict(rsi=rsi, dist20=(c / c.rolling(20).mean() - 1) * 100, atr=tr.rolling(14).mean() / c * 100,
                                    ret5=c.pct_change(5) * 100, ret20=c.pct_change(20) * 100, dd60=(c / c.rolling(60).max() - 1) * 100,
                                    liq=(c * g.volume.astype(float)).rolling(20).median() / 1e9,
                                    green=(c > o).astype(float), body=(c / o - 1) * 100,
                                    volx=g.volume.astype(float) / g.volume.astype(float).rolling(20).mean())).to_numpy())
FCOL = ['rsi', 'dist20', 'atr', 'ret5', 'ret20', 'dd60', 'liq', 'green', 'body', 'volx']

def sim(a, e, j, cfg):
    ep = a['o'][e]; tp = ep * (1 + TP / 100)
    runh = ep; pend = False
    for k in range(e, j + 1):
        if pend:                                   # fade terdeteksi di close kemarin -> keluar di open hari ini
            return (a['o'][k] / ep - 1) * 100 - COST, k - e + 1, 'fade'
        mfe = (runh / ep - 1) * 100
        stops = []
        if cfg.get('sl'): stops.append(ep * (1 - cfg['sl'] / 100))
        if cfg.get('trail') and mfe >= cfg['arm']: stops.append(runh * (1 - cfg['trail'] / 100))
        if cfg.get('give') and mfe >= cfg['arm']: stops.append(ep * (1 + mfe / 100 * (1 - cfg['give'] / 100)))
        if stops:
            st = max(stops)
            if a['l'][k] <= st:
                fill = min(a['o'][k], st)
                return (fill / ep - 1) * 100 - COST, k - e + 1, 'stop'
        if a['h'][k] >= tp:
            return (max(a['o'][k], tp) / ep - 1) * 100 - COST, k - e + 1, 'tp'
        runh = max(runh, a['h'][k])
        if cfg.get('fade') and (runh / ep - 1) * 100 >= cfg['arm'] and a['c'][k] < a['sma5'][k]:
            pend = True
    return (a['c'][j] / ep - 1) * 100 - COST, j - e + 1, 'time'

# ---------- sinyal
EP = pd.read_pickle('research/bull5/lane_episodes_open.pkl')
trades = []
for lane, key in (('OSB-v2', 'OSBv2'), ('BOW', 'BOW')):
    sig = EP[key][['ticker', 'date']].drop_duplicates().sort_values(['date', 'ticker'])
    busy = {}
    for r in sig.itertuples():
        a = A.get(r.ticker)
        if a is None: continue
        i = int(np.searchsorted(a['d'], np.datetime64(r.date)))
        if i >= len(a['d']) or a['d'][i] != np.datetime64(r.date) or i + 1 >= len(a['d']): continue
        e = i + 1
        if not a['o'][e] > 0: continue
        end = pd.Timestamp(a['d'][e]) + pd.Timedelta(days=60)
        if end > LAST: continue
        if r.ticker in busy and r.date <= busy[r.ticker]: continue
        j = int(np.searchsorted(a['d'], np.datetime64(end), side='right')) - 1
        busy[r.ticker] = pd.Timestamp(a['d'][j])   # konservatif: slot ticker terkunci sampai jendela habis
        rg = R.loc[:r.date].iloc[-1]
        trades.append(dict(lane=lane, ticker=r.ticker, sig=r.date, a=a, e=e, j=j, phase=rg.regime_phase, ihsg_dd100=rg.dd_100 * 100,
                           ihsg_r20=rg.ret_20, **dict(zip(FCOL, a['f'][i]))))
T = pd.DataFrame(trades).sort_values('sig').reset_index(drop=True)
cut = T.sig.iloc[int(len(T) * 0.6)]
T['set'] = np.where(T.sig < cut, 'train', 'test')
print(f"\nTrade total {len(T)} (OSB-v2 {sum(T.lane=='OSB-v2')}, BOW {sum(T.lane=='BOW')}); train < {cut.date()}: {sum(T.set=='train')}, test: {sum(T.set=='test')}")
print(T.groupby(['lane', 'phase']).size().unstack(fill_value=0).to_string())

def run(cfg, df=T):
    out = [sim(r.a, r.e, r.j, cfg) for r in df.itertuples()]
    return pd.DataFrame(out, columns=['ret', 'days', 'why'], index=df.index)

def stat(res):
    if len(res) == 0: return dict(n=0, win=np.nan, mean=np.nan, med=np.nan, days=np.nan, tp=np.nan, pf=np.nan, worst=np.nan)
    w, l = res.ret[res.ret > 0].sum(), -res.ret[res.ret <= 0].sum()
    return dict(n=len(res), win=100 * (res.ret > 0).mean(), mean=res.ret.mean(), med=res.ret.median(), days=res.days.mean(),
                tp=100 * (res.why == 'tp').mean(), pf=w / l if l > 0 else np.inf, worst=res.ret.min())

CFG = {'TP20 tanpa SL': dict()}
for s in (8, 10, 12, 15): CFG[f'TP20 SL{s}'] = dict(sl=s)
for arm, tr in itertools.product((8, 10, 12), (5, 7, 10)): CFG[f'TP20 SL12 + trail{tr} (arm+{arm})'] = dict(sl=12, arm=arm, trail=tr)
for arm, g in itertools.product((8, 10, 12), (30, 40, 50)): CFG[f'TP20 SL12 + giveback{g}% (arm+{arm})'] = dict(sl=12, arm=arm, give=g)
for arm in (8, 10, 12): CFG[f'TP20 SL12 + fade<SMA5 (arm+{arm})'] = dict(sl=12, arm=arm, fade=True)
# varian tanpa SL (trailing/fade hanya aktif setelah profit tercapai) + SL darurat -25%
for arm, tr in itertools.product((8, 10, 12, 15), (5, 7, 10)): CFG[f'TP20 noSL + trail{tr} (arm+{arm})'] = dict(arm=arm, trail=tr)
for arm, g in itertools.product((10, 12, 15), (30, 40, 50)): CFG[f'TP20 noSL + giveback{g}% (arm+{arm})'] = dict(arm=arm, give=g)
for arm in (8, 10, 12, 15): CFG[f'TP20 noSL + fade<SMA5 (arm+{arm})'] = dict(arm=arm, fade=True)
CFG['TP20 SL25'] = dict(sl=25)
for arm, tr in ((10, 7), (10, 10), (12, 10)): CFG[f'TP20 SL25 + trail{tr} (arm+{arm})'] = dict(sl=25, arm=arm, trail=tr)
for arm, g in ((10, 40), (12, 40)): CFG[f'TP20 SL25 + giveback{g}% (arm+{arm})'] = dict(sl=25, arm=arm, give=g)

print("\n" + "=" * 140 + "\nSTAGE 1 - EXIT, semua trade tanpa filter (urut win rate TRAIN)\n" + "=" * 140)
rows = []
for name, cfg in CFG.items():
    res = run(cfg)
    s_tr, s_te = stat(res[T.set == 'train']), stat(res[T.set == 'test'])
    rows.append(dict(exit=name, tr_win=s_tr['win'], tr_mean=s_tr['mean'], tr_days=s_tr['days'], te_win=s_te['win'], te_mean=s_te['mean'], te_pf=s_te['pf'], te_worst=s_te['worst'], te_tp=s_te['tp']))
S1 = pd.DataFrame(rows).sort_values('tr_win', ascending=False)
print(S1.head(30).round(1).to_string(index=False))
print("... (dibatasi 30 teratas dari %d konfigurasi)" % len(S1))

# exit terpilih: win rate TRAIN tertinggi dgn mean TRAIN >= +3% (supaya bukan trik 'ambil untung kecil')
cand = S1[S1.tr_mean >= 3.0]
best_name = cand.iloc[0]['exit'] if len(cand) else S1.iloc[0]['exit']
best = CFG[best_name]
print(f"\n>> exit terpilih (train): {best_name}")
T[['ret', 'days', 'why']] = run(best)

print("\n" + "=" * 140 + "\nSTAGE 2 - REGIME x LANE dengan exit terpilih (semua periode)\n" + "=" * 140)
for (lane, ph), g in T.groupby(['lane', 'phase']):
    s = stat(g); print(f"  {lane:7s} {ph:20s} n={s['n']:3d} win={s['win']:5.1f}% mean={s['mean']:+6.2f}% med={s['med']:+6.2f}% hold={s['days']:4.1f}d TP={s['tp']:4.1f}% worst={s['worst']:+6.1f}%")
print("\n  per fase (gabungan):")
for ph, g in T.groupby('phase'):
    s = stat(g); print(f"  {ph:20s} n={s['n']:3d} win={s['win']:5.1f}% mean={s['mean']:+6.2f}%")

print("\n" + "=" * 140 + "\nSTAGE 3 - FILTER: pilih di TRAIN (win rate tertinggi, n>=35% trade train), laporkan di TEST\n" + "=" * 140)
tr = T[T.set == 'train']; te = T[T.set == 'test']
cands = []
for ph in T.phase.unique(): cands.append((f'exclude {ph}', lambda d, ph=ph: d.phase != ph))
cands.append(('hanya BOUNCE/NEUTRAL (bukan CRASH/GRIND/PULLBACK)', lambda d: d.phase.isin(['NEUTRAL_BULLISH', 'BOUNCE_OFF_LOW'])))
cands.append(('lane OSB-v2', lambda d: d.lane == 'OSB-v2')); cands.append(('lane BOW', lambda d: d.lane == 'BOW'))
for f in ['rsi', 'dist20', 'atr', 'ret5', 'ret20', 'dd60', 'liq', 'body', 'volx', 'ihsg_dd100', 'ihsg_r20']:
    for q in (0.2, 0.35, 0.5, 0.65, 0.8):
        thr = T[f].quantile(q)
        cands.append((f'{f} >= {thr:.1f} (q{int(q*100)})', lambda d, f=f, thr=thr: d[f] >= thr))
        cands.append((f'{f} <= {thr:.1f} (q{int(q*100)})', lambda d, f=f, thr=thr: d[f] <= thr))

min_n = int(0.35 * len(tr))

def apply(fns, d):
    m = np.ones(len(d), bool)
    for fn in fns: m &= fn(d).to_numpy()
    return d[m]

print(f"  baseline TRAIN: {stat(tr)['n']} trade win={stat(tr)['win']:.1f}% mean={stat(tr)['mean']:+.2f}% | TEST: {stat(te)['n']} win={stat(te)['win']:.1f}% mean={stat(te)['mean']:+.2f}%")
chosen = []
for level in (1, 2, 3):
    best_sc, best_c = None, None
    for name, fn in cands:
        if any(name == c[0] for c in chosen): continue
        sub = apply([c[1] for c in chosen] + [fn], tr)
        if len(sub) < min_n: continue
        s = stat(sub)
        if s['mean'] < 3.0: continue
        sc = (s['win'], s['mean'])
        if best_sc is None or sc > best_sc: best_sc, best_c = sc, (name, fn)
    if best_c is None: break
    chosen.append(best_c)
    fns = [c[1] for c in chosen]
    a_tr, a_te = stat(apply(fns, tr)), stat(apply(fns, te))
    print(f"\n  level {level}: " + " AND ".join(c[0] for c in chosen))
    print(f"    TRAIN n={a_tr['n']:3d} win={a_tr['win']:5.1f}% mean={a_tr['mean']:+6.2f}% pf={a_tr['pf']:.2f}")
    print(f"    TEST  n={a_te['n']:3d} win={a_te['win']:5.1f}% mean={a_te['mean']:+6.2f}% pf={a_te['pf']:.2f} worst={a_te['worst']:+.1f}% hold={a_te['days']:.1f}d TP={a_te['tp']:.1f}%")

final = apply([c[1] for c in chosen], T) if chosen else T
final['q'] = final.sig.dt.to_period('Q').astype(str)
print("\n  filter final per kuartal (semua periode): " + " | ".join(f"{q} n={len(g)} win={100*(g.ret>0).mean():.0f}% mean={g.ret.mean():+.1f}" for q, g in final.groupby('q')))
print("  filter final per lane: " + " | ".join(f"{l} n={len(g)} win={100*(g.ret>0).mean():.0f}% mean={g.ret.mean():+.1f}" for l, g in final.groupby('lane')))

print("\n" + "=" * 140 + "\nSTAGE 4 - EXIT pada subset terfilter (TRAIN/TEST), urut win rate TRAIN\n" + "=" * 140)
rows = []
ftr, fte = apply([c[1] for c in chosen], tr), apply([c[1] for c in chosen], te)
for name, cfg in CFG.items():
    s1 = stat(run(cfg, ftr)); s2 = stat(run(cfg, fte))
    rows.append(dict(exit=name, tr_n=s1['n'], tr_win=s1['win'], tr_mean=s1['mean'], te_n=s2['n'], te_win=s2['win'], te_mean=s2['mean'], te_pf=s2['pf'], te_worst=s2['worst'], te_days=s2['days']))
print(pd.DataFrame(rows).sort_values('tr_win', ascending=False).head(14).round(1).to_string(index=False))
final.drop(columns=['a']).to_csv('research/hold60_filter_final_2026_11_18.csv', index=False)
