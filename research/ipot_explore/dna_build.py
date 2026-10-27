"""research/ipot_explore/dna_build.py — FROM SCRATCH (2026-10-27): ticker-day feature panel for DNA matching of short-swing winners.
Label (realisable): win5 = close(t+5)/open(t+1)-1 >= +5%.  Also ret5, ret1 (open t+1 -> close t+1, day-trade/BTST view), gap0 (open t+1 vs close t).
Features describe days -10..0 (day 0 = t, known after the close): technical snapshots at 0/-3/-5/-10 + slopes, broker role nets (conservative net_lo, top-10 truncation)
over 1/3/5/10 days and the prior 5 days, breadth/concentration. Universe: close>=50, 20d value traded >= Rp0.3bn, gap0<=7% (no ARA-open chase).
Run: PYTHONUTF8=1 JKSE=<csv> OUT=<pkl> PYTHONPATH=. python research/ipot_explore/dna_build.py"""
import os, sys, warnings, numpy as np, pandas as pd
warnings.filterwarnings('ignore')
sys.path.insert(0, os.getcwd())
import research.ipot_explore.accum_episode as A
from engine.broker_roles import role_of
from engine.ihsg_regime import _build_regime

ROLES = ('foreign_inst', 'gov_inst', 'foreign_retail_base', 'retail_app', 'scalper', 'local_other')
d, o = A.load()
ms = d.groupby(['ticker', 'date']).sell_lot.transform('min').fillna(0)
sl_ = d.sell_lot.fillna(0); bl_ = d.buy_lot.fillna(0)
d['net_lo'] = np.where(d.buy_lot.isna(), -sl_, bl_ - np.where(d.sell_lot.isna(), ms, sl_))
d['role'] = d.broker.map(role_of)
o = o.sort_values(['ticker', 'date']).reset_index(drop=True)
dg = {t: x for t, x in d.groupby('ticker')}

def rsi(c, n=14):
    dl = c.diff(); up = dl.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean(); dn = (-dl.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))

out = []
for t, g in o.groupby('ticker'):
    if t not in dg or len(g) < 120: continue
    g = g.reset_index(drop=True); c, h, l, op, v = g.close, g.high, g.low, g.open, g.volume.astype(float)
    cal = g.date.values; f = pd.DataFrame({'ticker': t, 'date': cal})
    pc = c.shift(1); tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    atr = tr.rolling(14).mean() / c * 100
    ema12, ema26 = c.ewm(span=12, adjust=False).mean(), c.ewm(span=26, adjust=False).mean(); macd = ema12 - ema26
    mh = (macd - macd.ewm(span=9, adjust=False).mean()) / c * 100; ml = macd / c * 100
    sma = {n: c.rolling(n).mean() for n in (5, 10, 20, 50)}
    sd20 = c.rolling(20).std(); pctb = (c - (sma[20] - 2 * sd20)) / (4 * sd20); bw = 4 * sd20 / sma[20] * 100
    volr = v / v.rolling(20).mean(); rs = rsi(c); cp = (c - l) / (h - l).replace(0, np.nan)
    body = (c - op) / op * 100; uw = (h - np.maximum(c, op)) / c * 100
    daily = {'rsi': rs, 'atr': atr, 'mh': mh, 'ml': ml, 'pctb': pctb, 'bw': bw, 'volr': volr, 'cp': cp, 'body': body, 'uw': uw,
             'd5': (c / sma[5] - 1) * 100, 'd10': (c / sma[10] - 1) * 100, 'd20': (c / sma[20] - 1) * 100, 'd50': (c / sma[50] - 1) * 100,
             'r1': c.pct_change(1) * 100, 'r5': c.pct_change(5) * 100}
    for k, s in daily.items():
        f[k + '_0'] = s.values
        if k in ('rsi', 'mh', 'pctb', 'bw', 'volr', 'd20', 'atr', 'r5', 'cp'):
            for lag in (3, 5, 10): f[f'{k}_L{lag}'] = s.shift(lag).values
    f['rsi_sl'] = (rs - rs.shift(5)).values; f['mh_sl'] = (mh - mh.shift(3)).values; f['bw_sl'] = (bw - bw.shift(5)).values
    f['sma20_sl'] = ((sma[20] / sma[20].shift(5) - 1) * 100).values; f['sma50_sl'] = ((sma[50] / sma[50].shift(10) - 1) * 100).values
    f['h20_dist'] = ((c / h.rolling(20).max() - 1) * 100).values; f['l20_dist'] = ((c / l.rolling(20).min() - 1) * 100).values
    f['r10'] = (c.pct_change(10) * 100).values; f['green3'] = (c > op).astype(float).rolling(3).sum().values
    f['liq'] = (c * v).rolling(20).mean().values; f['close'] = c.values
    f['volr5'] = (v.rolling(5).mean() / v.rolling(20).mean()).values
    # broker
    bd = dg[t]; vol_lot = (v / 100).values
    has = pd.Series(cal).isin(set(bd.date.unique())).values
    pv = bd.pivot_table(index='date', columns='broker', values='net_lo', aggfunc='sum').reindex(cal).fillna(0.0)
    rl = pd.Series({b: role_of(b) for b in pv.columns}); vs = pd.Series(vol_lot)
    for r_ in ROLES:
        cols = rl.index[rl == r_]; s = pv[cols].sum(axis=1).reset_index(drop=True) if len(cols) else pd.Series(0.0, index=range(len(cal)))
        for w in (1, 3, 5, 10): f[f'b_{r_}_{w}'] = (s.rolling(w).sum() / vs.rolling(w).sum()).values
        f[f'b_{r_}_p5'] = (s.shift(5).rolling(5).sum() / vs.shift(5).rolling(5).sum()).values
    p5 = pv.rolling(5).sum().div(vs.rolling(5).sum().values, axis=0)
    ic = rl.index[rl.isin(['foreign_inst', 'gov_inst'])]
    f['b_n_inst5'] = (p5[ic] >= 0.01).sum(axis=1).values if len(ic) else 0
    f['b_n_buy5'] = (p5 >= 0.005).sum(axis=1).values; f['b_n_sell5'] = (p5 <= -0.005).sum(axis=1).values
    srt = np.sort(p5.values, axis=1)
    f['b_top1'] = srt[:, -1]; f['b_top3'] = srt[:, -3:].sum(axis=1) if srt.shape[1] >= 3 else np.nan
    f['b_bot3'] = srt[:, :3].sum(axis=1) if srt.shape[1] >= 3 else np.nan
    f.loc[~has, [x for x in f.columns if x.startswith('b_')]] = np.nan
    # labels
    on = op.shift(-1); f['gap0'] = ((on / c - 1) * 100).values
    f['ret5'] = ((c.shift(-5) / on - 1) * 100).values; f['ret1'] = ((c.shift(-1) / on - 1) * 100).values
    f['hi5'] = ((h.shift(-1).rolling(5).max().shift(-4) / on - 1) * 100).values
    # exit simulation from open(t+1) over days t+1..t+5: TP +5% touch; SL variants; same-day TP&SL -> SL first; gap through level fills at the open
    e = on.values; H = [h.shift(-k).values for k in range(1, 6)]; L = [l.shift(-k).values for k in range(1, 6)]
    O = [op.shift(-k).values for k in range(1, 6)]; C5 = c.shift(-5).values
    for nm, slp in (('tr_tp5_sl5', 5.0), ('tr_tp5_nosl', None)):
        res = np.full(len(e), np.nan); done = np.zeros(len(e), bool)
        for k in range(5):
            tp = e * 1.05; st = e * (1 - slp / 100) if slp else np.full(len(e), -np.inf)
            ok = ~done & ~np.isnan(H[k]) & ~np.isnan(e)
            gd = ok & (O[k] <= st) & (k > 0); gu = ok & (O[k] >= tp) & (k > 0)
            sh = ok & ~gd & ~gu & (L[k] <= st); th = ok & ~gd & ~gu & ~sh & (H[k] >= tp)
            for m, px in ((gd, O[k]), (gu, O[k]), (sh, st), (th, tp)):
                res[m] = (px[m] / e[m] - 1) * 100; done[m] = True
            if k == 4:
                m = ok & ~done; res[m] = (C5[m] / e[m] - 1) * 100; done[m] = True
        f[nm] = res
    out.append(f)
P = pd.concat(out, ignore_index=True)
P = (P[P.gap0 <= 7] if os.environ.get('WIDE') else P[(P.close >= 50) & (P.liq >= 3e8) & (P.gap0 <= 7)]).copy()   # WIDE=1: only the no-ARA-open filter (option A, 2026-10-27)
reg = pd.read_csv(os.environ['JKSE']); reg.index = pd.to_datetime(reg.iloc[:, 0], utc=True).dt.tz_convert('Asia/Jakarta').dt.tz_localize(None)
reg = _build_regime(reg)[['date', 'regime_phase']]; reg['date'] = reg.date.dt.strftime('%Y-%m-%d')
P = P.merge(reg, on='date', how='left')
P['win5'] = (P.hi5 >= 5).astype(float); P.loc[P.hi5.isna(), 'win5'] = np.nan   # TP-touch winner: high within t+1..t+5 >= open(t+1)*1.05
P['atr_cell'] = P.groupby('date').atr_0.transform(lambda s: pd.qcut(s.rank(method='first'), 3, labels=False, duplicates='drop'))
P.to_pickle(os.environ['OUT'])
lab = P.dropna(subset=['hi5'])
print('rows', len(P), 'labelled', len(lab), 'tickers', P.ticker.nunique(), 'features', P.shape[1])
print(lab.groupby('regime_phase').agg(n=('win5', 'size'), win5=('win5', 'mean'), ret5=('ret5', 'mean'), tp5sl5=('tr_tp5_sl5', 'mean'), tp5=('tr_tp5_nosl', 'mean')).round(3))
print('broker coverage (non-null b_top1):', round(P.b_top1.notna().mean(), 3))
