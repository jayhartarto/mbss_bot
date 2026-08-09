"""
Replay formula breakout dengan data menit kemarin (2026-07-27)
Simulasi: formula dijalankan di berbagai titik waktu selama sesi
sehingga kita bisa lihat bagaimana score berkembang.

Validasi yang diharapkan:
- KOTA/BUKA breakout: score RENDAH pagi → naik saat breakout terjadi
- KICI/SMSM sideways: score tetap RENDAH sepanjang hari
"""
import pandas as pd
import numpy as np
import datetime
import yfinance as yf

WIB = datetime.timezone(datetime.timedelta(hours=7))

def calculate_rsi(series, period=14):
    delta = series.diff()
    gain  = delta.clip(lower=0).ewm(alpha=1/period, adjust=False).mean()
    loss  = (-delta.clip(upper=0)).ewm(alpha=1/period, adjust=False).mean()
    rs    = gain / loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))

def score_at(bars_so_far):
    """Jalankan formula pada subset bar (simulasi snapshot waktu tertentu)."""
    if bars_so_far is None or len(bars_so_far) < 8:
        return None

    closes  = bars_so_far["Close"]
    highs   = bars_so_far["High"]
    lows    = bars_so_far["Low"]
    volumes = bars_so_far["Volume"]

    current_price = float(closes.iloc[-1])

    # Resistance dari 70% pertama bar yang tersedia saat ini
    n_hist     = max(int(len(bars_so_far) * 0.70), min(8, len(bars_so_far)))
    hist_highs = highs.iloc[:n_hist]
    high_vals  = hist_highs.values
    best_res, best_cnt = None, 0
    for ref in high_vals:
        r = ref * 0.005
        c = sum(1 for h in high_vals if abs(h - ref) <= r)
        if c > best_cnt or (c == best_cnt and ref > (best_res or 0)):
            best_cnt, best_res = c, ref
    resistance = float(best_res) if best_res else float(hist_highs.max())

    atr     = float((highs - lows).mean())
    d_pct   = ((resistance - current_price) / max(resistance, 1e-9)) * 100
    d_atr   = (resistance - current_price) / max(atr, 1e-9)

    lb      = min(20, len(bars_so_far))
    avg_vol = float(volumes.tail(lb).mean())
    vol_r   = float(volumes.iloc[-1] / max(avg_vol, 1e-9))
    ema5    = float(closes.ewm(span=5,  adjust=False).mean().iloc[-1])
    ema20   = float(closes.ewm(span=20, adjust=False).mean().iloc[-1])
    rsi14   = float(calculate_rsi(closes, 14).iloc[-1]) if len(closes) >= 14 else None
    hl      = lows.iloc[-1] >= float(lows.tail(min(5, len(lows))).min())

    score = 0
    if d_atr < -1.0:   score += 25; st = f"POST-BO"
    elif d_atr < 0:    score += 25; st = "BARU BO"
    elif d_atr <= 0.3: score += 25; st = "DI RESIST"
    elif d_atr <= 0.8: score += 18; st = "DEKAT"
    elif d_atr <= 1.5: score += 10; st = "MENDEKAT"
    else:              score += 3;  st = "JAUH"

    if vol_r >= 2.5:   score += 25
    elif vol_r >= 1.8: score += 18
    elif vol_r >= 1.3: score += 12
    elif vol_r >= 0.8: score += 6
    else:              score += 2

    score += 20 if ema5 > ema20 else 7
    if rsi14 is not None:
        if 55 <= rsi14 <= 70: score += 15
        elif 45 <= rsi14 < 55: score += 8
        elif rsi14 > 70: score += 3
        else: score += 5
    score += 15 if hl else 5
    if best_cnt >= 3: score += 5
    elif best_cnt == 2: score += 2
    score = int(min(100, score))
    lbl = "🔥" if score >= 75 else "📈" if score >= 55 else "⬇️"

    return dict(price=current_price, resistance=resistance,
                d_pct=d_pct, vol_r=vol_r, score=score, lbl=lbl, st=st)

# ─── Ambil data kemarin ───────────────────────────────────────────
TARGET_DATE = (datetime.datetime.now(WIB) - datetime.timedelta(days=1)).date()
# Kalau kemarin weekend, mundur ke Jumat
while TARGET_DATE.weekday() >= 5:
    TARGET_DATE -= datetime.timedelta(days=1)

TICKERS = {
    "KOTA": "🔥 Breakout kemarin",
    "BUKA": "🔥 Breakout kemarin",
    "KICI": "⬇️  Sideways/volatile",
    "SMSM": "⬇️  Defensif sideways",
    "TLKM": "⬇️  Blue chip stabil",
    "ERAA": "📈  Ada momentum",
}

# Snapshot tiap 30 menit: 09:30, 10:00, 10:30, 11:00, 11:30, 12:00, 14:00, 14:30, 15:00, 15:30
SNAPSHOTS = [
    datetime.time(9, 30), datetime.time(10, 0), datetime.time(10, 30),
    datetime.time(11, 0), datetime.time(11, 30), datetime.time(12, 0),
    datetime.time(14, 0), datetime.time(14, 30), datetime.time(15, 0), datetime.time(15, 30),
]

print(f"\n{'='*65}")
print(f"  SESSION REPLAY — {TARGET_DATE}")
print(f"  Formula dijalankan di setiap snapshot waktu")
print(f"{'='*65}")

for ticker, desc in TICKERS.items():
    try:
        raw = yf.Ticker(f"{ticker}.JK").history(period="5d", interval="5m")
        if raw.empty:
            print(f"\n{ticker}: ❌ tidak ada data"); continue
        raw.index = raw.index.tz_convert(WIB)
        day_bars = raw[raw.index.date == TARGET_DATE]
        if day_bars.empty:
            print(f"\n{ticker}: ❌ tidak ada data untuk {TARGET_DATE}"); continue

        print(f"\n{'─'*65}")
        print(f"  {ticker} — {desc}  ({len(day_bars)} bar, {TARGET_DATE})")
        print(f"  Open {day_bars['Close'].iloc[0]:.0f} | "
              f"High {day_bars['High'].max():.0f} | "
              f"Low {day_bars['Low'].min():.0f} | "
              f"Close {day_bars['Close'].iloc[-1]:.0f}")
        print(f"  {'Waktu':>8}  {'Bars':>5}  {'Harga':>6}  {'Resist':>7}  "
              f"{'Jarak':>6}  {'Vol':>5}  {'Score':>6}  Status")
        print(f"  {'─'*60}")

        prev_score = None
        for snap_time in SNAPSHOTS:
            snap_dt = datetime.datetime.combine(TARGET_DATE, snap_time, tzinfo=WIB)
            bars_so_far = day_bars[day_bars.index <= snap_dt]
            if len(bars_so_far) < 4:
                continue
            r = score_at(bars_so_far)
            if r is None:
                continue
            # Tandai kalau score berubah signifikan
            arrow = ""
            if prev_score is not None:
                if r['score'] - prev_score >= 10: arrow = " ↑↑"
                elif r['score'] - prev_score >= 5:  arrow = " ↑"
                elif r['score'] - prev_score <= -10: arrow = " ↓↓"
                elif r['score'] - prev_score <= -5:  arrow = " ↓"
            prev_score = r['score']
            print(f"  {snap_time.strftime('%H:%M'):>8}  {len(bars_so_far):5}  "
                  f"{r['price']:6.0f}  {r['resistance']:7.0f}  "
                  f"{r['d_pct']:+6.1f}%  {r['vol_r']:5.2f}x  "
                  f"{r['lbl']} {r['score']:3}{arrow:3}  {r['st']}")

    except Exception as e:
        print(f"\n{ticker}: ❌ Error — {e}")

print(f"\n{'='*65}")
print("Selesai.")
