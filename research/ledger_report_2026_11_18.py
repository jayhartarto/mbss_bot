"""research/ledger_report_2026_11_18.py -- hasil per periode dari pick_ledger (sinyal replay engine produksi + live). Run: PYTHONUTF8=1 python research/ledger_report_2026_11_18.py"""
import os, sys
sys.path.insert(0, os.getcwd())
import pandas as pd
import engine.pick_ledger as pl
D = pl.load_ledger(first_only=True)
print('episode', len(D), D.groupby(['lane', 'source']).size().to_dict())
PER = [('2025 (Jan-Des)', '2025-01-01', '2026-01-01'), ('2026 Jan-Mar', '2026-01-01', '2026-04-01'), ('2026 Apr-Agu', '2026-04-01', '2026-09-01'), ('2026 Sep-6Okt', '2026-09-01', '2026-10-07'),
       ('7-20 Okt (awal crash)', '2026-10-07', '2026-10-21'), ('21 Okt-6 Nov (dasar)', '2026-10-21', '2026-11-07'), ('7-16 Nov (rebound)', '2026-11-07', '2026-11-17')]
def row(name, g, rule):
    s = pl.rule_stats(g, rule)
    return f"    {name:26s} n={s['n']:3d} win {s['win']:4.0f}% mean {s['mean']:+6.2f}% {s['days']:5.1f}hr ret/hr {s['rpd']:+.3f}% p10 {s['p10']:+6.1f} open {s['open']}"
for pn, a, b in PER:
    G = D[(D.signal_date >= a) & (D.signal_date < b)]
    if G.empty: print('\n==', pn, 'kosong'); continue
    F = G[G.filt == 1]
    print(f"\n== {pn}: {len(G)} episode (OSBv2 {int((G.lane=='OSBv2').sum())}, BOW {int((G.lane=='BOW').sum())}, lolos filter {len(F)})")
    for lane, rule in (('OSBv2', 'osb_exist'), ('BOW', 'bow_exist')):
        g = G[G.lane == lane]
        if len(g) >= 3: print(row(f'{lane} existing (semua)', g, rule))
    if len(F) >= 3:
        for r in ('new_tp10_sl20', 'new_tp12_sl20', 'tp10_nosl'): print(row(f'NEW/{r} terfilter', F, r))
    ge = pd.concat([G[G.lane == 'OSBv2'].assign(r=G[G.lane == 'OSBv2'].osb_exist_ret, d=G[G.lane == 'OSBv2'].osb_exist_days), G[G.lane == 'BOW'].assign(r=G[G.lane == 'BOW'].bow_exist_ret, d=G[G.lane == 'BOW'].bow_exist_days)])
    if len(ge): print(f"    {'EXISTING gabungan':26s} n={len(ge):3d} win {100*(ge.r>0).mean():4.0f}% mean {ge.r.mean():+6.2f}% {ge.d.mean():5.1f}hr ret/hr {ge.r.mean()/ge.d.mean():+.3f}%")
