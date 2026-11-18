"""research/ledger_backfill_2026_11_18.py -- isi pick_ledger.sqlite: replay engine produksi (ledger_replay_2026_11_18.py) + pick live dari file picks lokal.
Run: PYTHONUTF8=1 python research/ledger_backfill_2026_11_18.py"""
import glob, json, os, sys
sys.path.insert(0, os.getcwd())
import pandas as pd
import engine.pick_ledger as pl
files = [f for f in glob.glob('research/ledger_replay_signals_20*_20*.csv') if '2025-01-02_2025-12-31' in f or '2026-01-02_2026-11-16' in f]
df = pd.concat([pd.read_csv(f) for f in files]).drop_duplicates(['lane', 'ticker', 'trigger_date'])
df.to_csv('research/ledger_seed.csv', index=False)   # seed kecil utk VPS (git): python -c "import engine.pick_ledger as p; p.import_csv('research/ledger_seed.csv')"
print('replay sinyal', len(df), df.groupby('lane').size().to_dict(), df.trigger_date.min(), df.trigger_date.max())
print('import replay:', pl.import_csv('research/ledger_seed.csv'))
for lane, f in (('OSBv2', 'oversold_bounce_v2_picks.json'), ('BOW', 'buy_on_weakness_picks.json')):
    if os.path.exists(f):
        try:
            p = json.load(open(f)); n = pl.record_picks(lane, p, 'live'); print(f'live {lane}: {len(p)} dibaca, {n} baru')
        except Exception as e: print('skip', f, e)
print('resolve:', pl.resolve_all())
