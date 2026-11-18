"""research/compare_existing_vs_new_2026_11_18.py -- strategi baru (OSB-v2+BOW terfilter, TP10, tanpa SL / SL20, H90) vs aturan EXISTING produksi.
Existing OSB-v2: SL-8%, TP2 +10%, BE +0.2% aktif (mulai bar berikut) setelah high>=+5%, max 8 bar lalu tutup di close.
Existing BOW  : SL-12%, TP = Bollinger upper (SMA20+2sd, band close bar sebelumnya), window resolusi 16 bar lalu tutup di close.
Sinyal: lane_episodes_open.pkl dedup 1 posisi/ticker, entry open T+1, biaya 0.4%, hanya window 90 bar lengkap (sampel sama utk semua aturan).
Metrik: win, mean, hari tahan rata2, ret/hari (= mean / hari tahan), + portofolio 3 slot (sektor beda, acak 200 seed).
Run: PYTHONUTF8=1 python research/compare_existing_vs_new_2026_11_18.py"""
import sqlite3, numpy as np, pandas as pd, warnings
warnings.filterwarnings('ignore')
COST, H, NS, NSEED = 0.004, 90, 3, 200
px = pd.read_sql_query("SELECT ticker,date,open,high,low,close FROM ohlcv_daily ORDER BY ticker,date", sqlite3.connect('mbss_ohlcv.db'), parse_dates=['date'])
cal = np.sort(px.date.unique()); cidx = {d: i for i, d in enumerate(cal)}
sec = pd.read_csv('idx_ic_sector_map.csv').set_index('ticker').sector.to_dict()
EP = pd.read_pickle('research/bull5/lane_episodes_open.pkl')
sigs = pd.concat([EP['OSBv2'].assign(lane='OSB-v2'), EP['BOW'].assign(lane='BOW')])[['ticker', 'date', 'lane']].drop_duplicates()
sigs['date'] = pd.to_datetime(sigs.date)
A = {}
for t, g in px[px.ticker.isin(sigs.ticker.unique())].groupby('ticker'):
    c = g.close.astype(float); d = c.diff(); up = d.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean(); dn = (-d.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    m = c.rolling(20).mean(); sd = c.rolling(20).std()
    A[t] = dict(d=g.date.values, o=g.open.to_numpy(float), h=g.high.to_numpy(float), l=g.low.to_numpy(float), c=c.to_numpy(float),
                bb=(m + 2 * sd).to_numpy(), rsi=(100 - 100 / (1 + up / dn.replace(0, np.nan))).to_numpy(), dd60=(c / c.rolling(60).max() - 1).to_numpy() * 100)

def ex_osb(a, e, i, ep, N=None):
    sl, tp, be_on, be_lv, armed = ep * .92, ep * 1.10, ep * 1.05, ep * 1.002, False
    for k in range(min(8, N)):
        st = max(sl, be_lv) if armed else sl
        if a['l'][e + k] <= st: return k, min(a['o'][e + k], st)
        if a['h'][e + k] >= tp: return k, max(a['o'][e + k], tp)
        if a['h'][e + k] >= be_on: armed = True
    return (7, a['c'][e + 7]) if N >= 8 else (N - 1, a['c'][e + N - 1])
def ex_bow(a, e, i, ep, N=None):
    sl = ep * .88
    for k in range(min(16, N)):
        tp = a['bb'][e + k - 1]
        if a['l'][e + k] <= sl: return k, min(a['o'][e + k], sl)
        if tp == tp and a['h'][e + k] >= tp: return k, max(a['o'][e + k], tp)
    return (15, a['c'][e + 15]) if N >= 16 else (N - 1, a['c'][e + N - 1])
def mk_tp(tp, slp=None, T=H):
    def f(a, e, i, ep, N=None):
        lv = ep * (1 + tp / 100); st = ep * (1 - slp / 100) if slp else 0
        for k in range(min(T, N)):
            if slp and a['l'][e + k] <= st: return k, min(a['o'][e + k], st)
            if a['h'][e + k] >= lv: return k, max(a['o'][e + k], lv)
        return (T - 1, a['c'][e + T - 1]) if N >= T else (N - 1, a['c'][e + N - 1])
    return f
RULES = {'OSB-v2 existing': ex_osb, 'BOW existing': ex_bow, 'NEW TP10 tanpa SL': mk_tp(10), 'NEW TP10 SL20': mk_tp(10, 20), 'NEW TP12 SL20': mk_tp(12, 20),
         'NEW TP10 SL20 TS30': mk_tp(10, 20, 30)}

# sinyal dasar (dedup, window lengkap), tandai filt
base = []
for lane, g in sigs.sort_values(['date', 'ticker']).groupby('lane'):
    busy = {}
    for r in g.itertuples():
        a = A.get(r.ticker)
        if a is None or (r.ticker in busy and r.date <= busy[r.ticker]): continue
        i = int(np.searchsorted(a['d'], np.datetime64(r.date)))
        if i >= len(a['d']) or a['d'][i] != np.datetime64(r.date) or i + 1 >= len(a['d']) : continue
        e = i + 1; ep = a['o'][e]
        if not ep > 0: continue
        busy[r.ticker] = r.date + pd.Timedelta(days=int(H * 1.45))
        base.append(dict(lane=lane, ticker=r.ticker, date=r.date, i=i, e=e, ep=ep, filt=bool(a['dd60'][i] <= -20 and a['rsi'][i] >= 31)))
B = pd.DataFrame(base); print(f"sinyal dasar: {len(B)} (OSB-v2 {int((B.lane=='OSB-v2').sum())}, BOW {int((B.lane=='BOW').sum())}, terfilter {int(B.filt.sum())}), {B.date.min().date()}..{B.date.max().date()}")

def trades(rule, sub):
    out = []
    for r in sub.itertuples():
        a = A[r.ticker]; N = len(a['c']) - r.e; k, p = RULES[rule](a, r.e, r.i, r.ep, N)
        out.append(dict(open=bool(k == N - 1 and ((rule.startswith('OSB') and N < 8) or (rule.startswith('BOW') and N < 16) or (rule.startswith('NEW') and N < 90 and p == a['c'][-1]))), ticker=r.ticker, lane=r.lane, date=r.date, ret=(p / r.ep - 1) * 100 - COST * 100, days=k + 1, k=k, p=p, e=r.e, ep=r.ep, sdate=cidx[r.date]))
    return pd.DataFrame(out)
def line(name, T):
    x, d = T.ret, T.days
    print(f"  {name:44s} n={len(T):3d} win={100*(x>0).mean():5.1f}% mean={x.mean():+6.2f}% med={x.median():+6.2f}% hari={d.mean():5.1f} ret/hari={x.mean()/d.mean():+.3f}% p10={x.quantile(.1):+6.1f} worst={x.min():+6.1f}")



NS, NSEED = 3, 200
def build(T):
    S = []
    for r in T.itertuples():
        a = A[r.ticker]; g0 = cidx[a['d'][r.e]]
        val = np.array([(r.p / r.ep) if j >= r.k else a['c'][r.e + j] / r.ep for j in range(r.k + 1)]); val[-1] = val[-1] * (1 - COST)
        S.append(dict(ticker=r.ticker, sdate=r.sdate, g0=g0, gend=g0 + r.k, val=val, sector=sec.get(r.ticker, 'NA'), mult=val[-1], prio=r.prio))
    return sorted(S, key=lambda s: s['sdate'])
def sim(S, rng, use_prio):
    d0, d1 = S[0]['g0'], max(s['gend'] for s in S); bysd = {}
    for s in S: bysd.setdefault(s['sdate'], []).append(s)
    cash, open_, done = 1.0, [], []; curve = np.ones(d1 - d0 + 1); used = np.zeros(d1 - d0 + 1)
    for day in range(d0, d1 + 1):
        for p in [p for p in open_ if p['s']['gend'] <= day]:
            cash += p['size'] * p['s']['mult']; open_.remove(p); done.append(p['s']['mult'] - 1)
        cands = [s for s in bysd.get(day, []) if s['g0'] > day]; free = NS - len(open_)
        if free > 0 and cands:
            held = {p['s']['ticker'] for p in open_}; cands = [s for s in cands if s['ticker'] not in held]; chosen = []
            while free > len(chosen) and cands:
                hs = {p['s']['sector'] for p in open_} | {c['sector'] for c in chosen}
                pool = cands
                if use_prio and any(c['prio'] for c in pool): pool = [c for c in pool if c['prio']]
                pool = [c for c in pool if c['sector'] not in hs] or pool
                pick = pool[rng.integers(len(pool))]; chosen.append(pick); cands = [c for c in cands if c['ticker'] != pick['ticker']]
            tot = cash + sum(p['size'] for p in open_)
            for s in chosen: cash -= tot / NS; open_.append(dict(s=s, size=tot / NS))
        inv = sum(p['size'] * (p['s']['val'][min(max(day - p['s']['g0'], 0), len(p['s']['val']) - 1)] if day >= p['s']['g0'] else 1.0) for p in open_)
        curve[day - d0] = cash + inv; used[day - d0] = len(open_)
    return dict(total=curve[-1] - 1, dd=(curve / np.maximum.accumulate(curve) - 1).min(), n=len(done), util=used.mean() / NS, mean=np.mean(done) if done else 0, win=np.mean(np.array(done) > 0) if done else 0)

OSBa, BOWa = B[B.lane == 'OSB-v2'], B[B.lane == 'BOW']; Fl = B[B.filt]
def tag(T, prio): T = T.copy(); T['prio'] = prio; return T
def dedupe(T): return T.drop_duplicates(['ticker', 'sdate']).reset_index(drop=True)
NEW = 'NEW TP12 SL20'
H1 = dedupe(pd.concat([tag(trades(NEW, Fl), True), tag(trades('OSB-v2 existing', OSBa[~OSBa.filt]), False)]))
SC = {
 'OSB-v2 existing (semua sinyal OSB)': (dedupe(tag(trades('OSB-v2 existing', OSBa), False)), False),
 'EXISTING gabungan (OSB + BOW)': (dedupe(pd.concat([tag(trades('OSB-v2 existing', OSBa), False), tag(trades('BOW existing', BOWa), False)])), False),
 'NEW saja (terfilter, TP12 SL20)': (dedupe(tag(trades(NEW, Fl), True)), False),
 'HYBRID-1: terfilter=NEW, sisa OSB=existing': (H1, False),
 'HYBRID-1 + prioritas NEW': (H1, True),
 'HYBRID-2: +BOW sisa = BOW existing': (dedupe(pd.concat([tag(trades(NEW, Fl), True), tag(trades('OSB-v2 existing', OSBa[~OSBa.filt]), False), tag(trades('BOW existing', BOWa[~BOWa.filt]), False)])), False),
}
PER = {'Penuh Nov24-Okt26': ('2024-11-01', '2026-11-01'), 'Nov24-Mar26 (sebelum crash 2026)': ('2024-11-01', '2026-04-01'), 'Apr-Agu26 (crash->rebound)': ('2026-04-01', '2026-09-01'), 'Sep-Okt26 (crash lanjut)': ('2026-09-01', '2026-11-01')}
for pn, (a0, a1) in PER.items():
    print("=" * 30 + f" PERIODE {pn} " + "=" * 30)
    for sn, (T, prio) in SC.items():
        Tp = T[(T.date >= a0) & (T.date < a1)]
        if len(Tp) < 3: print(f"  {sn:46s} sinyal terlalu sedikit"); continue
        S = build(Tp); rng = np.random.default_rng(7); R = [sim(S, rng, prio) for _ in range(NSEED)]
        q = lambda k, p: np.percentile([r[k] for r in R], p)
        print(f"  {sn:46s} sinyal {len(Tp):3d} | total med {100*q('total',50):+7.1f}% (p10 {100*q('total',10):+6.1f}, p90 {100*q('total',90):+6.1f}) DD {100*q('dd',50):+6.1f}% | trade {q('n',50):4.0f} win {100*q('win',50):3.0f}% mean {100*q('mean',50):+5.2f}% | slot {100*q('util',50):3.0f}%")
