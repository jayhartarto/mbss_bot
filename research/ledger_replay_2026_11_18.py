"""research/ledger_replay_2026_11_18.py -- replay PRODUCTION lane engines (engine/oversold_bounce_v2.py, engine/buy_on_weakness.py) point-in-time.

Mekanisme: engine membaca OHLCV lewat core.get_ohlcv_daily_from_db(ticker, limit) dan IHSG lewat engine.market / engine.ihsg_regime.
Di sini fungsi2 itu di-monkeypatch supaya hanya melihat data s/d tanggal as-of (tanpa lookahead). Hasil: sinyal yang akan dihasilkan
engine produksi (aturan sekarang, termasuk FF-exclusion dari research/foreign_flow_2y.sqlite) pada setiap tanggal.
Sinyal disimpan ke research/ledger_replay_signals.csv: lane, ticker, trigger_date, tier, entry_ref(close sinyal), ff fields.
Pakai: PYTHONUTF8=1 python research/ledger_replay_2026_11_18.py START END [lane]   (mis. 2026-04-01 2026-11-16)
"""
import sys, os, time, json, sqlite3
import numpy as np, pandas as pd
sys.path.insert(0, os.getcwd())
import engine.legacy_core as core
import engine.market as market_engine
import engine.ihsg_regime as regime_engine
import engine.oversold_bounce_v2 as osb
import engine.buy_on_weakness as bow

START = sys.argv[1] if len(sys.argv) > 1 else '2026-04-01'
END = sys.argv[2] if len(sys.argv) > 2 else '2026-11-16'
LANES = (sys.argv[3].split(',') if len(sys.argv) > 3 else ['OSBv2', 'BOW'])
OUT = 'research/ledger_replay_signals.csv'

# ---- data penuh in-memory
conn = sqlite3.connect(core.OHLCV_DB_FILE)
raw = pd.read_sql_query("SELECT ticker,date,open,high,low,close,volume FROM ohlcv_daily ORDER BY ticker,date", conn, parse_dates=['date'])
FULL = {t: g.drop(columns='ticker').rename(columns=dict(open='Open', high='High', low='Low', close='Close', volume='Volume')).set_index('date')
        for t, g in raw.groupby('ticker')}
ASOF = {'d': None}
def fake_ohlcv(ticker, limit=500):
    df = FULL.get(ticker)
    if df is None: return pd.DataFrame()
    return df[df.index <= ASOF['d']].tail(limit)
core.get_ohlcv_daily_from_db = fake_ohlcv

# ---- IHSG as-of (pakai _build_regime produksi di atas close IHSG)
R = pd.read_pickle('research/.tmp_ihsg_regime_full.pkl'); R.index = pd.to_datetime(R.index).normalize()
ih = R.close
dd100 = (ih / ih.rolling(100, min_periods=20).max() - 1) * 100
ret1 = ih.pct_change() * 100
def asof_val(s):
    x = s[s.index <= ASOF['d']]; return float(x.iloc[-1]) if len(x) and pd.notna(x.iloc[-1]) else None
market_engine.get_ihsg_drawdown_100d = lambda: asof_val(dd100)
market_engine.get_ihsg_return_today = lambda: asof_val(ret1)
market_engine.get_ihsg_return_nd = lambda n=20: asof_val((ih / ih.shift(n) - 1) * 100)
def fake_regime():
    x = R[R.index <= ASOF['d']].iloc[-1]
    return {'date': str(x.name.date()), 'dd_100': float(x.dd_100), 'bear_episode_id': 0, 'regime_phase': str(x.regime_phase), 'ret_20': float(x.ret_20), 'ret_10': None}
regime_engine.get_ihsg_regime_today = fake_regime

wl = json.load(open('ticker_whitelist.json'))['eligible_tickers']
UNIV = [t for t in wl if t in FULL]
print(f"universe {len(UNIV)} (whitelist & DB); IHSG as-of {R.index.min().date()}..{R.index.max().date()}")
days = [d for d in sorted(raw.date.unique()) if pd.Timestamp(START) <= d <= pd.Timestamp(END)]
ffl = bow._load_ff_lookup()
rows = []; t0 = time.time()
for di, d in enumerate(days):
    ASOF['d'] = pd.Timestamp(d)
    if 'OSBv2' in LANES:
        for c in osb.compute_oversold_bounce_v2_candidates(UNIV):
            rows.append(dict(lane='OSBv2', ticker=c['ticker'], trigger_date=str(c['trigger_date'])[:10], tier=c.get('tier'), entry_ref=c['entry_ref_price'], ff=c.get('ff_priority'), phase=fake_regime()['regime_phase']))
    if 'BOW' in LANES:
        for c in bow.compute_buy_on_weakness_candidates(UNIV):
            rows.append(dict(lane='BOW', ticker=c['ticker'], trigger_date=str(c['trigger_date'])[:10], tier=c.get('tier'), entry_ref=c['entry_ref_price'], ff=c.get('ff_priority'), phase=fake_regime()['regime_phase']))
    if di % 10 == 0:
        print(f"  {di+1}/{len(days)} {pd.Timestamp(d).date()} sinyal so far {len(rows)} ({time.time()-t0:.0f}s)", flush=True)
        pd.DataFrame(rows).to_csv(OUT.replace('.csv', f'_{START}_{END}.csv'), index=False)
df = pd.DataFrame(rows); df.to_csv(OUT.replace('.csv', f'_{START}_{END}.csv'), index=False)
print(df.groupby('lane').size() if len(df) else 'tidak ada sinyal'); print(f"selesai {time.time()-t0:.0f}s")
