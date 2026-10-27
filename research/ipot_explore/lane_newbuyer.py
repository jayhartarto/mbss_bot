"""research/ipot_explore/lane_newbuyer.py — does the refined NEW-BUYER signal add to each EXISTING production lane?  (2026-10-27)
Lane signals are produced by REPLAYING the production engine code historically: engine.<lane>.evaluate_ticker on the OHLCV slice ending at each date
(core.get_ohlcv_daily_from_db monkeypatched), with the real FF lookup and an IHSG table from yfinance ^JKSE + engine.ihsg_regime._build_regime.
Regime gates mirror each compute_*_candidates: BOS & MACD-confirm only in BOUNCE_OFF_LOW (bear episode), OSB-v2 only if IHSG dd_100<=-10% and phase!=BEARISH_GRIND (is_crash_fast flag),
BOW/VCP ungated at the evaluate level.  Rows limited to the research universe (close>=50, 20d value>=Rp0.3bn, open gap<=7%), so illiquid MACD-confirm/VCP picks are not covered.
Signals (thresholds FIXED from the NEUTRAL discovery period): sig = new_buy top decile (all newcomer roles); sigref = foreign_inst + local_other + foreign_retail_base newcomers top decile.
Outcome: lane-native exit (TP/SL/horizon from the lane constants, same-day both -> SL first) and raw ret5/ret10 from open(t+1); picks de-duplicated to 1 per ticker per 7 calendar days.
Run: PYTHONUTF8=1 FLOW=<flow_panel.pkl> DNA=<dna_panel2.pkl> JKSE=<csv> PYTHONPATH=. python research/ipot_explore/lane_newbuyer.py"""
import os, sys, sqlite3, warnings, numpy as np, pandas as pd
from concurrent.futures import ProcessPoolExecutor
warnings.filterwarnings('ignore')
sys.path.insert(0, os.getcwd())
ROOT = os.getcwd()
LANES = {'BOS': (8, 15, 10), 'OSBv2': (10, 20, 20), 'BOW': (6, 18, 10), 'VCP': (2.5, 10, 5), 'MACDc': (8, 6, 5)}   # native TP%, SL%, horizon (trading days)

# ------------------------------------------------------------------ worker side
_D = {}; _CUR = {}
def _init(tickers):
    import engine.legacy_core as core
    import engine.buy_on_strength as bos, engine.oversold_bounce_v2 as osb, engine.buy_on_weakness as bow, engine.vcp_pillar as vcp, engine.macd_confirm_pillar as macd
    c = sqlite3.connect(os.path.join(ROOT, 'mbss_ohlcv.db'))
    q = "SELECT ticker, date, open, high, low, close, volume FROM ohlcv_daily WHERE ticker IN (%s) ORDER BY ticker, date" % ','.join('?' * len(tickers))
    raw = pd.read_sql_query(q, c, params=list(tickers)); c.close()
    for t, g in raw.groupby('ticker'):
        df = g.rename(columns={'open': 'Open', 'high': 'High', 'low': 'Low', 'close': 'Close', 'volume': 'Volume'}).drop(columns='ticker')
        df['date'] = pd.to_datetime(df['date']); _D[t] = df.set_index('date')
    def fake(ticker, limit=500):
        df = _D[ticker]; i = _CUR['i']; return df.iloc[max(0, i - limit + 1):i + 1]
    core.get_ohlcv_daily_from_db = fake
    _CUR['mods'] = dict(bos=bos, osb=osb, bow=bow, vcp=vcp, macd=macd); _CUR['ff'] = bow._load_ff_lookup()

def _work(args):
    jobs, ih = args   # jobs: list of (ticker, date_str, [lanes]);  ih: dict date -> dict(ret1, ret10, ret20, dd100, phase)
    m = _CUR['mods']; ff = _CUR['ff']; out = []
    for t, ds, lanes in jobs:
        df = _D.get(t)
        if df is None: continue
        i = df.index.searchsorted(pd.Timestamp(ds))
        if i >= len(df) or df.index[i] != pd.Timestamp(ds): continue
        _CUR['i'] = i; h = ih.get(ds)
        if h is None: continue
        for L in lanes:
            try:
                if L == 'BOS': r = m['bos'].evaluate_ticker(t, h['ret10'])
                elif L == 'OSBv2': r = m['osb'].evaluate_ticker(t, ff, h['ret1'], is_crash_fast=(h['phase'] == 'CRASH_FAST'))
                elif L == 'BOW': r = m['bow'].evaluate_ticker(t, ff, h['ret20'], h['dd100'])
                elif L == 'VCP': r = m['vcp'].evaluate_ticker(t, ff, h['ret20'])
                else: r = m['macd'].evaluate_ticker(t, ff)
            except Exception:
                r = None
            if r is not None: out.append((t, ds, L))
    return out

# ------------------------------------------------------------------ main
def newcomer_ref():
    """foreign_inst + local_other + foreign_retail_base newcomer share (all dates) and all-role new_buy re-derived consistently with flow_surprise_build."""
    from engine.broker_roles import role_of
    c = sqlite3.connect(os.path.join(ROOT, 'research', 'ipot_merged.sqlite'))
    d = pd.read_sql_query("SELECT ticker, trade_date date, broker_code broker, buy_lot, sell_lot FROM ipot_broker_daily WHERE fd='all' AND board='all'", c); c.close()
    c = sqlite3.connect(os.path.join(ROOT, 'mbss_ohlcv.db')); o = pd.read_sql_query("SELECT ticker, date, volume FROM ohlcv_daily WHERE date>='2022-10-01' ORDER BY ticker,date", c); c.close()
    ms = d.groupby(['ticker', 'date']).sell_lot.transform('min').fillna(0); sl_ = d.sell_lot.fillna(0); bl_ = d.buy_lot.fillna(0)
    d['net_lo'] = np.where(d.buy_lot.isna(), -sl_, bl_ - np.where(d.sell_lot.isna(), ms, sl_))
    dg = {t: x for t, x in d.groupby('ticker')}; rows = []
    for t, g in o.groupby('ticker'):
        if t not in dg: continue
        cal = g.date.values; vol = g.volume.values / 100.0; vs5 = pd.Series(vol).rolling(5).sum().values; vs20 = pd.Series(vol).rolling(20).sum().values
        pv = dg[t].pivot_table(index='date', columns='broker', values='net_lo', aggfunc='sum').reindex(cal).fillna(0.0)
        roll5 = pv.rolling(5).sum().div(vs5, axis=0); pa = pv.abs().rolling(20).sum().shift(5).div(vs20, axis=0)
        rl = pd.Series({b: role_of(b) for b in pv.columns}); pos = roll5.clip(lower=0).where(pa < 0.0025, 0)
        keep = rl.index[rl.isin(['foreign_inst', 'local_other', 'foreign_retail_base'])]
        rows.append(pd.DataFrame({'ticker': [t] * len(cal), 'date': cal, 'nb_ref': pos[keep].sum(axis=1).values if len(keep) else 0.0}))
    return pd.concat(rows, ignore_index=True).replace([np.inf, -np.inf], np.nan)

def sim(h, l, o, c, i, tp, sl, H):
    """entry open(i+1); days i+1..i+H; same-day both -> SL first; gap through level fills at the open."""
    e = o[i + 1]
    if np.isnan(e) or i + H >= len(c): return np.nan
    for k in range(1, H + 1):
        j = i + k
        if k > 1:
            if o[j] <= e * (1 - sl / 100): return (o[j] / e - 1) * 100
            if o[j] >= e * (1 + tp / 100): return (o[j] / e - 1) * 100
        if l[j] <= e * (1 - sl / 100): return -sl
        if h[j] >= e * (1 + tp / 100): return tp
    return (c[i + H] / e - 1) * 100

def main():
    from engine.ihsg_regime import _build_regime
    S = {k: os.environ[k] for k in ('FLOW', 'DNA', 'JKSE')}
    F = pd.read_pickle(S['FLOW']).replace([np.inf, -np.inf], np.nan)
    Dn = pd.read_pickle(S['DNA'])[['ticker', 'date', 'rsi_0', 'pctb_0', 'd20_0', 'd50_0', 'atr_0', 'h20_dist', 'r10', 'body_0']]
    X = F.merge(Dn, on=['ticker', 'date'], how='inner', suffixes=('', '_d'))
    if 'atr_0_d' in X: X['atr_0'] = X['atr_0_d']
    j = pd.read_csv(S['JKSE']); j.index = pd.to_datetime(j.iloc[:, 0], utc=True).dt.tz_convert('Asia/Jakarta').dt.tz_localize(None)
    R = _build_regime(j); R['date'] = R.date.dt.strftime('%Y-%m-%d')
    ih = {r.date: dict(ret1=(r.close / R.close.shift(1)[i] - 1) * 100 if i else np.nan, ret10=r.ret_10, ret20=r.ret_20, dd100=r.dd_100 * 100, phase=r.regime_phase, ep=r.bear_episode_id)
          for i, r in R.reset_index(drop=True).iterrows()}
    X['dd100'] = X.date.map(lambda d: ih.get(d, {}).get('dd100', np.nan)); X['ep'] = X.date.map(lambda d: ih.get(d, {}).get('ep', 0))
    # prefilters (necessary conditions copied from each lane's first gates, with small margins)
    pf = {'BOS': (X.regime_phase == 'BOUNCE_OFF_LOW') & (X.ep > 0) & (X.r10 > 4.9),
          'MACDc': (X.regime_phase == 'BOUNCE_OFF_LOW'),
          'OSBv2': (X.dd100 <= -10) & (X.regime_phase != 'BEARISH_GRIND') & (X.d20_0 <= -9.9) & (X.body_0 > 0),
          'BOW': (X.pctb_0 <= 0.36) & (X.atr_0 <= 8) & (X.d50_0 > -15),
          'VCP': (X.rsi_0 >= 63) & (X.atr_0 <= 12) & (X.h20_dist >= -11)}
    jobs = {}
    for L, m in pf.items():
        for t, d in zip(X.ticker[m], X.date[m]): jobs.setdefault((t, d), []).append(L)
    print('prefiltered rows per lane:', {L: int(m.sum()) for L, m in pf.items()}, '| distinct (ticker,date):', len(jobs), flush=True)
    by_t = {}
    for (t, d), ls in jobs.items(): by_t.setdefault(t, []).append((t, d, ls))
    tickers = sorted(by_t); NW = 12; chunks = [tickers[i::NW] for i in range(NW)]
    res = []
    ih_s = {k: dict(v) for k, v in ih.items()}
    futs = []
    with ProcessPoolExecutor(max_workers=NW) as ex:
        for ch in chunks:
            jl = [x for t in ch for x in by_t[t]]
            futs.append(ex.submit(_run_chunk, ch, jl, ih_s))
        for f in futs: res += f.result()
    P = pd.DataFrame(res, columns=['ticker', 'date', 'lane']); print('replayed lane fires:', P.groupby('lane').size().to_dict(), flush=True)
    # signals
    NB = newcomer_ref(); X = X.merge(NB, on=['ticker', 'date'], how='left')
    N = X[X.regime_phase == 'NEUTRAL_BULLISH']; dts = np.sort(N.date.unique()); cut = dts[int(len(dts) * 0.6)]
    thr, thr_ref = N[N.date <= cut].new_buy.quantile(0.9), N[N.date <= cut].nb_ref.quantile(0.9)
    print('fixed thresholds from NEUTRAL discovery: new_buy %.4f  nb_ref %.4f' % (thr, thr_ref))
    X['sig'] = X.new_buy >= thr; X['sigref'] = X.nb_ref >= thr_ref
    X['x5'] = X.ret5 - X.groupby(['date', 'atr_cell']).ret5.transform('mean')
    P = P.merge(X[['ticker', 'date', 'regime_phase', 'sig', 'sigref', 'ret1', 'ret5', 'x5', 'atr_cell', 'liq', 'close']], on=['ticker', 'date'], how='left').dropna(subset=['ret5'])
    # dedup flag: first fire per ticker per lane per 7 calendar days (kept as a flag so both variants can be reported)
    P = P.sort_values(['lane', 'ticker', 'date']); keep = set(); last = {}
    for i, (L, t, d) in zip(P.index, P[['lane', 'ticker', 'date']].values):
        dd = pd.Timestamp(d); k = (L, t)
        if k not in last or (dd - last[k]).days > 7: keep.add(i); last[k] = dd
    P['dd'] = P.index.isin(keep)
    # native-exit and ret10 from OHLCV
    c = sqlite3.connect(os.path.join(ROOT, 'mbss_ohlcv.db')); raw = pd.read_sql_query("SELECT ticker, date, open, high, low, close FROM ohlcv_daily WHERE date>='2022-10-01' ORDER BY ticker, date", c); c.close()
    arr = {t: (g.date.values, g.high.values, g.low.values, g.open.values, g.close.values) for t, g in raw.groupby('ticker')}
    nat, r10 = [], []
    for L, t, d in P[['lane', 'ticker', 'date']].values:
        dt_, h, l, o, cl = arr[t]; i = int(np.searchsorted(dt_, d)); tp, sl, H = LANES[L]
        nat.append(sim(h, l, o, cl, i, tp, sl, H)); r10.append(sim(h, l, o, cl, i, 1e9, 1e9, 10))
    P['native'] = nat; P['ret10'] = r10
    P.to_pickle(os.path.join(os.path.dirname(S['FLOW']), 'lane_newbuyer_picks' + os.environ.get('TAG', '') + '.pkl'))
    report_all(P)

def _run_chunk(ch, jl, ih):
    _init(ch); return _work((jl, ih))

def bdiff(a, b, col, blk=5, n=300):
    rng = np.random.default_rng(3)
    if len(a) < 25 or len(b) < 25: return np.nan, np.nan
    ds = np.sort(pd.concat([a.date, b.date]).unique()); bid = {d: i // blk for i, d in enumerate(ds)}
    ga = a.assign(b=a.date.map(bid)).groupby('b')[col].agg(['sum', 'count']); gb = b.assign(b=b.date.map(bid)).groupby('b')[col].agg(['sum', 'count'])
    ids = np.arange(max(bid.values()) + 1); sa = ga['sum'].reindex(ids).fillna(0).values; ca = ga['count'].reindex(ids).fillna(0).values
    sb = gb['sum'].reindex(ids).fillna(0).values; cb = gb['count'].reindex(ids).fillna(0).values
    if len(ids) < 8: return a[col].mean() - b[col].mean(), np.nan
    d0 = sa.sum() / max(ca.sum(), 1) - sb.sum() / max(cb.sum(), 1); bs = []
    for _ in range(n):
        k = rng.integers(0, len(ids), len(ids))
        if ca[k].sum() < 5 or cb[k].sum() < 5: continue
        bs.append(sa[k].sum() / ca[k].sum() - sb[k].sum() / cb[k].sum())
    return d0, d0 / (np.std(bs) + 1e-9)

def report(P):
    pd.set_option('display.width', 250)
    print('\nLANE x REGIME: picks (deduped), with/without signal.  native = lane TP/SL/horizon expectancy %, r5/r10 = raw return from open(t+1), diff t = block bootstrap (5-date blocks)')
    for sg in ('sigref', 'sig'):
        print(f'\n##### signal = {sg}')
        rows = []
        for (L, rg), g in P.groupby(['lane', 'regime_phase']):
            if len(g) < 60: continue
            a, b = g[g[sg]], g[~g[sg]]
            if len(a) < 15: continue
            dn, tn = bdiff(a.dropna(subset=['native']), b.dropna(subset=['native']), 'native'); d5, t5 = bdiff(a, b, 'ret5')
            rows.append(dict(lane=L, regime=rg[:12], n=len(g), n_sig=len(a), share=round(100 * len(a) / len(g)), dates=g.date.nunique(),
                             nat_sig=a.native.mean(), nat_no=b.native.mean(), d_nat=dn, t_nat=tn, r5_sig=a.ret5.mean(), r5_no=b.ret5.mean(), d_r5=d5, t_r5=t5,
                             r10_sig=a.ret10.mean(), r10_no=b.ret10.mean(), up5_sig=(a.ret5 > 0).mean() * 100, up5_no=(b.ret5 > 0).mean() * 100))
        print(pd.DataFrame(rows).round(2).to_string(index=False))
    print('\nALL regimes pooled per lane (sigref):')
    for L, g in P.groupby('lane'):
        a, b = g[g.sigref], g[~g.sigref]; dn, tn = bdiff(a.dropna(subset=['native']), b.dropna(subset=['native']), 'native'); d5, t5 = bdiff(a, b, 'ret5')
        print(f'{L:6s} n={len(g)} n_sig={len(a)} native sig/no={a.native.mean():+.2f}/{b.native.mean():+.2f} diff={dn:+.2f}(t{tn:+.1f})  ret5 sig/no={a.ret5.mean():+.2f}/{b.ret5.mean():+.2f} diff={d5:+.2f}(t{t5:+.1f})')

def pooled(P, label, lanes=('BOS', 'BOW', 'MACDc', 'VCP'), sg='sigref'):
    LN = {k: v for k, v in LANES.items()}
    g = P[P.lane.isin(lanes)].dropna(subset=['native']).copy()
    g['sl'] = g.lane.map(lambda l: LN[l][1]); g['is_sl'] = g.native <= -g.sl + 0.01
    a, b = g[g[sg]], g[~g[sg]]
    if len(a) < 20: print(f'{label:46s} n_sig={len(a)} (too few)'); return
    dn, tn = bdiff(a, b, 'native'); d5, t5 = bdiff(a, b, 'ret5'); dx, tx = bdiff(a, b, 'x5'); d10, t10 = bdiff(a.dropna(subset=['ret10']), b.dropna(subset=['ret10']), 'ret10')
    print(f"{label:46s} n={len(g):6d} n_sig={len(a):5d} | win {100*(a.native>0).mean():.1f} vs {100*(b.native>0).mean():.1f} | mean {a.native.mean():+.2f} vs {b.native.mean():+.2f} ({dn:+.2f}, t{tn:+.1f}) | med {a.native.median():+.2f} vs {b.native.median():+.2f} | SL {100*a.is_sl.mean():.1f} vs {100*b.is_sl.mean():.1f} | ret5 {d5:+.2f}(t{t5:+.1f}) x5 {dx:+.2f}(t{tx:+.1f}) ret10 {d10:+.2f}(t{t10:+.1f}) | up5 {100*(a.ret5>0).mean():.0f} vs {100*(b.ret5>0).mean():.0f}")

def report_all(P):
    pd.set_option('display.width', 250)
    liquid = (P.liq >= 3e8) & (P.close >= 50)
    print('raw fires per lane (all rows / deduped / liquid-deduped):', {L: (int((P.lane == L).sum()), int(((P.lane == L) & P.dd).sum()), int(((P.lane == L) & P.dd & liquid).sum())) for L in LANES})
    for sg in ('sigref', 'sig'):
        print(f'########## signal = {sg}: 4 lanes pooled (BOS,BOW,MACDc,VCP)')
        pooled(P[P.dd & liquid], 'A0 liquid + dedup (previous run)', sg=sg)
        pooled(P[P.dd], 'A1 all liquidity + dedup', sg=sg)
        pooled(P[liquid], 'A2 liquid, NO dedup', sg=sg)
        pooled(P, 'A3 all liquidity, NO dedup (option A)', sg=sg)
        pooled(P[P.regime_phase == 'NEUTRAL_BULLISH'], 'A3 NEUTRAL only', sg=sg)
        pooled(P[P.regime_phase == 'BOUNCE_OFF_LOW'], 'A3 BOUNCE_OFF_LOW only', sg=sg)
        print('--- per lane, option A3 (all liquidity, no dedup):')
        for L in LANES:
            pooled(P[P.lane == L], f'  {L}', lanes=(L,), sg=sg)
        print('--- per lane x regime (option A3), n_sig>=30:')
        for (L, rg), g in P.groupby(['lane', 'regime_phase']):
            if g[sg].sum() >= 30: pooled(g, f'  {L} / {rg[:12]}', lanes=(L,), sg=sg)

if __name__ == '__main__':
    main()
