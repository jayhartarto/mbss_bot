"""research/portfolio3_sim_2026_11_18.py -- simulasi portofolio maks 3 posisi (1/3 ekuitas per posisi, compounding), sinyal terfilter OSB-v2+BOW
(dd60<=-20 & RSI>=31), entry open T+1, biaya 0.4%, tanpa SL, H=90. Pemilihan saat sinyal > slot kosong: acak (300 seed) dgn/ tanpa preferensi sektor
berbeda (sektor IDX-IC idx_ic_sector_map.csv). Exit: TP10 | TP12 | 50%TP10+50%ride(arm10/trail10). MTM harian pakai close.
Hanya sinyal dgn window 90 bar lengkap. Run: PYTHONUTF8=1 python research/portfolio3_sim_2026_11_18.py"""
import sqlite3, numpy as np, pandas as pd, warnings
warnings.filterwarnings('ignore')
COST, H, NS, NSEED = 0.004, 90, 3, 300
px = pd.read_sql_query("SELECT ticker,date,open,high,low,close,volume FROM ohlcv_daily ORDER BY ticker,date", sqlite3.connect('mbss_ohlcv.db'), parse_dates=['date'])
cal = np.sort(px.date.unique()); cidx = {d: i for i, d in enumerate(cal)}
sec = pd.read_csv('idx_ic_sector_map.csv').set_index('ticker').sector.to_dict()
EP = pd.read_pickle('research/bull5/lane_episodes_open.pkl')
sigs = pd.concat([EP['OSBv2'].assign(lane='OSB-v2'), EP['BOW'].assign(lane='BOW')])[['ticker', 'date', 'lane']].drop_duplicates()
sigs['date'] = pd.to_datetime(sigs.date)
A = {}
for t, g in px[px.ticker.isin(sigs.ticker.unique())].groupby('ticker'):
    c = g.close.astype(float); d = c.diff(); up = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean(); dn = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    A[t] = dict(d=g.date.values, o=g.open.to_numpy(float), h=g.high.to_numpy(float), l=g.low.to_numpy(float), c=c.to_numpy(float),
                rsi=(100 - 100 / (1 + up / dn.replace(0, np.nan))).to_numpy(), dd60=(c / c.rolling(60).max() - 1).to_numpy() * 100, liq=((c * g.volume.astype(float)).rolling(20).mean() / 1e9).to_numpy())

def tp_exit(a, e, ep, tp, sl=None, T=H):
    lv = ep * (1 + tp / 100); st = ep * (1 - sl / 100) if sl else 0
    for k in range(T):
        if sl and a['l'][e + k] <= st: return k, min(a['o'][e + k], st)
        if a['h'][e + k] >= lv: return k, max(a['o'][e + k], lv)
    return T - 1, a['c'][e + T - 1]
def tp_exit_old(a, e, ep, tp):
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

def build(mode, sl=None, T=H):
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
            tr = [tp_exit(a, e, ep, 10 if mode == 'TP10' else 12, sl, T)]
            g0 = cidx[a['d'][e]]; gend = g0 + max(k for k, _ in tr)
            val = np.ones(gend - g0 + 1)  # nilai posisi relatif thd modal, per hari global g0..gend
            for j in range(len(val)):
                v = 0
                for k, p in tr: v += (p / ep) if j >= k else (a['c'][e + j] / ep)
                val[j] = v / len(tr)
            val[-1] *= (1 - COST)
            S.append(dict(ticker=r.ticker, sdate=cidx[r.date], g0=g0, gend=gend, val=val, sector=sec.get(r.ticker, 'NA'), lane=lane, mult=val[-1], liq=a['liq'][i]))
    return sorted(S, key=lambda s: s['sdate'])

def sim(S, rng, diverse, byliq=False):
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
                pick = max(pool, key=lambda c: c['liq']) if byliq else pool[rng.integers(len(pool))]; chosen.append(pick); cands = [s for s in cands if s is not pick]
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

def row(S, **kw):
    r = sim(S, np.random.default_rng(7), True, True)
    return r
print("Pemilihan: sektor beda dulu, lalu LIKUIDITAS (rata2 nilai 20h) tertinggi. Deterministik.")
for mode in ('TP10', 'TP12'):
    print("=" * 130 + f" {mode}: total / CAGR / maxDD / trade / win / mean per trade / slot terpakai")
    for T in (90, 60, 45, 30):
        for sl in (None, 8, 12, 15, 20, 25):
            S = build(mode, sl, T); r = row(S)
            print(f"  SL {str(sl) + '%' if sl else 'tanpa':>6} TS {T:2d}h: total {100*r['total']:+7.1f}% CAGR {100*r['cagr']:+6.1f}% DD {100*r['dd']:+6.1f}% | n={r['n']:3d} win {100*r['win']:4.0f}% mean {100*r['mean']:+6.2f}% | slot {100*r['util']:3.0f}%")
print("Pembanding TP10/H90/tanpa SL: acak vs sektor+likuid (300 seed utk acak)")
S = build('TP10'); rng = np.random.default_rng(7)
R = [sim(S, rng, True, False) for _ in range(200)]
print(f"  sektor beda acak: total median {100*np.median([r['total'] for r in R]):+.1f}% DD {100*np.median([r['dd'] for r in R]):+.1f}% | likuid: total {100*row(S)['total']:+.1f}% DD {100*row(S)['dd']:+.1f}%")
# robustness: paruh waktu
r = row(build('TP10', 15, 45)); c = r['curve']; h = len(c) // 2
print(f"  contoh SL15/TS45 kurva: paruh1 {100*(c[h]/c[0]-1):+.1f}% paruh2 {100*(c[-1]/c[h]-1):+.1f}%")
