"""research/portfolio3_sim_2026_11_18.py -- simulasi portofolio maks 3 posisi (1/3 ekuitas per posisi, compounding), sinyal terfilter OSB-v2+BOW
(dd60<=-20 & RSI>=31), entry open T+1, biaya 0.4%, tanpa SL, H=90. Pemilihan saat sinyal > slot kosong: acak (300 seed) dgn/ tanpa preferensi sektor
berbeda (sektor IDX-IC idx_ic_sector_map.csv). Exit: TP10 | TP12 | 50%TP10+50%ride(arm10/trail10). MTM harian pakai close.
Hanya sinyal dgn window 90 bar lengkap. Run: PYTHONUTF8=1 python research/portfolio3_sim_2026_11_18.py"""
import sqlite3, numpy as np, pandas as pd, warnings
warnings.filterwarnings('ignore')
COST, H, NS, NSEED = 0.004, 90, 3, 300
px = pd.read_sql_query("SELECT ticker,date,open,high,low,close FROM ohlcv_daily ORDER BY ticker,date", sqlite3.connect('mbss_ohlcv.db'), parse_dates=['date'])
cal = np.sort(px.date.unique()); cidx = {d: i for i, d in enumerate(cal)}
sec = pd.read_csv('idx_ic_sector_map.csv').set_index('ticker').sector.to_dict()
EP = pd.read_pickle('research/bull5/lane_episodes_open.pkl')
sigs = pd.concat([EP['OSBv2'].assign(lane='OSB-v2'), EP['BOW'].assign(lane='BOW')])[['ticker', 'date', 'lane']].drop_duplicates()
sigs['date'] = pd.to_datetime(sigs.date)
A = {}
for t, g in px[px.ticker.isin(sigs.ticker.unique())].groupby('ticker'):
    c = g.close.astype(float); d = c.diff(); up = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean(); dn = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    A[t] = dict(d=g.date.values, o=g.open.to_numpy(float), h=g.high.to_numpy(float), l=g.low.to_numpy(float), c=c.to_numpy(float),
                rsi=(100 - 100 / (1 + up / dn.replace(0, np.nan))).to_numpy(), dd60=(c / c.rolling(60).max() - 1).to_numpy() * 100)

def tp_exit(a, e, ep, tp):
    lv = ep * (1 + tp / 100); hit = np.nonzero(a['h'][e:e + H] >= lv)[0]
    return (hit[0], max(a['o'][e + hit[0]], lv)) if len(hit) else (H - 1, a['c'][e + H - 1])
def ride_exit(a, e, ep, arm=10, trail=10):
    run = ep; armed = False
    for k in range(H):
        if armed:
            stop = run * (1 - trail / 100)
            if a['l'][e + k] <= stop: return k, min(a['o'][e + k], stop)
        run = max(run, a['h'][e + k])
        if not armed and run >= ep * (1 + arm / 100): armed = True
    return H - 1, a['c'][e + H - 1]

def build(mode):
    S = []
    for lane, g in sigs.sort_values(['date', 'ticker']).groupby('lane'):
        busy = {}
        for r in g.itertuples():
            a = A.get(r.ticker)
            if a is None or (r.ticker in busy and r.date <= busy[r.ticker]): continue
            i = int(np.searchsorted(a['d'], np.datetime64(r.date)))
            if i >= len(a['d']) or a['d'][i] != np.datetime64(r.date) or i + 1 + H > len(a['d']) or not (a['dd60'][i] <= -20 and a['rsi'][i] >= 31): continue
            e = i + 1; ep = a['o'][e]
            if not ep > 0: continue
            busy[r.ticker] = r.date + pd.Timedelta(days=int(H * 1.45))
            tr = [tp_exit(a, e, ep, 10)] if mode == 'TP10' else [tp_exit(a, e, ep, 12)] if mode == 'TP12' else [tp_exit(a, e, ep, 10), ride_exit(a, e, ep)]
            g0 = cidx[a['d'][e]]; gend = g0 + max(k for k, _ in tr)
            val = np.ones(gend - g0 + 1)  # nilai posisi relatif thd modal, per hari global g0..gend
            for j in range(len(val)):
                v = 0
                for k, p in tr: v += (p / ep) if j >= k else (a['c'][e + j] / ep)
                val[j] = v / len(tr)
            val[-1] *= (1 - COST)
            S.append(dict(ticker=r.ticker, sdate=cidx[r.date], g0=g0, gend=gend, val=val, sector=sec.get(r.ticker, 'NA'), lane=lane, mult=val[-1]))
    return sorted(S, key=lambda s: s['sdate'])

def sim(S, rng, diverse):
    d0, d1 = S[0]['g0'], max(s['gend'] for s in S)
    slots = []  # posisi terbuka: (s, size)
    eq = 1.0; cash_eq = 1.0  # cash_eq = ekuitas yg belum terpasang; posisi dinilai terpisah
    curve = np.ones(d1 - d0 + 2); used = np.zeros(d1 - d0 + 2); done = []; k = 0
    bysd = {}
    for s in S: bysd.setdefault(s['sdate'], []).append(s)
    cash = 1.0; open_ = []
    for day in range(d0, d1 + 1):
        # tutup posisi yg selesai hari ini
        for p in [p for p in open_ if p['s']['gend'] <= day]:
            cash += p['size'] * p['s']['mult']; open_.remove(p); done.append(p['s']['mult'] - 1)
        # sinyal hari ini -> entry open besok (g0 = sdate+1)
        cands = [s for s in bysd.get(day, []) if s['g0'] > day]
        free = NS - len(open_)
        if free > 0 and cands:
            held = {p['s']['ticker'] for p in open_}; cands = [s for s in cands if s['ticker'] not in held]
            chosen = []
            while free > len(chosen) and cands:
                hs = {p['s']['sector'] for p in open_} | {c['sector'] for c in chosen}
                pool = [s for s in cands if s['sector'] not in hs] if diverse else []
                pool = pool or cands
                pick = pool[rng.integers(len(pool))]; chosen.append(pick); cands = [s for s in cands if s is not pick]
            tot = cash + sum(p['size'] for p in open_)
            for s in chosen:
                size = tot / NS
                cash -= size; open_.append(dict(s=s, size=size, start=s['g0']))
        # tandai nilai
        inv = 0.0
        for p in open_:
            s = p['s']; j = day - s['g0']
            inv += p['size'] * (s['val'][min(max(j, 0), len(s['val']) - 1)] if j >= 0 else 1.0)
        curve[day - d0] = cash + inv; used[day - d0] = len(open_)
    curve = curve[:d1 - d0 + 1]; used = used[:d1 - d0 + 1]
    dd = (curve / np.maximum.accumulate(curve) - 1).min()
    yrs = (d1 - d0) / 245
    return dict(total=curve[-1] - 1, cagr=(curve[-1] ** (1 / yrs) - 1), dd=dd, n=len(done), win=np.mean(np.array(done) > 0), mean=np.mean(done), util=used.mean() / NS, idle0=(used == 0).mean(), yrs=yrs, curve=curve, d0=d0)

print(f"Ekuitas: {NS} posisi sama besar; sektor IDX-IC; {NSEED} seed acak")
for mode in ('TP10', 'TP12', 'TP10+ride'):
    S = build(mode)
    print("\n" + "=" * 118 + f"\nMODE {mode}: {len(S)} sinyal terfilter lengkap {pd.Timestamp(cal[S[0]['sdate']]).date()}..{pd.Timestamp(cal[max(s['gend'] for s in S)]).date()}, sektor unik {len({s['sector'] for s in S})}\n" + "=" * 118)
    for diverse in (False, True):
        rng = np.random.default_rng(7); R = [sim(S, rng, diverse) for _ in range(NSEED)]
        q = lambda k, p: np.percentile([r[k] for r in R], p)
        print(f"  {'sektor beda' if diverse else 'acak biasa  '}: total median {100*q('total',50):+6.1f}% (p10 {100*q('total',10):+6.1f}, p90 {100*q('total',90):+6.1f}) | CAGR med {100*q('cagr',50):+6.1f}% | maxDD med {100*q('dd',50):+6.1f}% (p10 {100*q('dd',10):+6.1f}) | trade {q('n',50):.0f} win {100*q('win',50):.0f}% mean {100*q('mean',50):+.2f}% | slot terpakai {100*q('util',50):.0f}% | hari 0 posisi {100*q('idle0',50):.0f}% | durasi {R[0]['yrs']:.2f} th | P(total<0) {100*np.mean([r['total']<0 for r in R]):.0f}%")
    if mode == 'TP10':
        rng = np.random.default_rng(7); R = [sim(S, rng, True) for _ in range(NSEED)]
        M = np.median(np.array([[r['curve'][min(i, len(r['curve']) - 1)] for i in range(len(R[0]['curve']))] for r in R]), axis=0)
        days = pd.to_datetime(cal[R[0]['d0']:R[0]['d0'] + len(M)]); s = pd.Series(M, index=days); ms = s.resample('ME').last(); mr = ms.pct_change().fillna(ms.iloc[0] - 1) * 100
        print("  kurva median bulanan (sektor beda): " + ", ".join(f"{d.strftime('%y-%m')}:{v:+.1f}" for d, v in mr.items()))
        print(f"  bulan negatif {int((mr<0).sum())}/{len(mr)}; bulan flat (|r|<0.3%) {int((mr.abs()<0.3).sum())}")
S = build('TP10')
print("\nSEKTOR sinyal (TP10, semua sinyal lengkap):"); print(pd.Series([s['sector'] for s in S]).value_counts().to_string())
