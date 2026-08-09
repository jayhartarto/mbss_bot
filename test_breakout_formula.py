"""
Test formula breakout — validasi untuk berbagai kondisi pasar
Jalankan: python test_breakout_formula.py
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

def run_breakout(ticker, bars):
    if bars is None or bars.empty or len(bars) < 8:
        return None

    closes  = bars["Close"]
    highs   = bars["High"]
    lows    = bars["Low"]
    volumes = bars["Volume"]

    current_price = float(closes.iloc[-1])
    open_price    = float(closes.iloc[0])

    # Cluster resistance
    high_values = highs.values
    best_resistance, best_count = None, 0
    for ref in high_values:
        radius = ref * 0.005
        count  = sum(1 for h in high_values if abs(h - ref) <= radius)
        if count > best_count or (count == best_count and ref > (best_resistance or 0)):
            best_count, best_resistance = count, ref
    resistance  = float(best_resistance) if best_resistance else float(highs.max())
    atr_session = float((highs - lows).mean())
    distance_pct     = ((resistance - current_price) / max(resistance, 1e-9)) * 100
    distance_in_atr  = (resistance - current_price) / max(atr_session, 1e-9)

    lookback  = min(20, len(bars))
    avg_vol   = float(volumes.tail(lookback).mean())
    vol_ratio = float(volumes.iloc[-1] / max(avg_vol, 1e-9))

    ema5  = float(closes.ewm(span=5,  adjust=False).mean().iloc[-1])
    ema20 = float(closes.ewm(span=20, adjust=False).mean().iloc[-1])
    rsi14 = float(calculate_rsi(closes, 14).iloc[-1]) if len(closes) >= 14 else None
    higher_low = lows.iloc[-1] >= float(lows.tail(min(5, len(lows))).min())

    # Scoring v2
    score = 0
    vol_warning = ""
    if distance_in_atr < -1.0:
        score += 25; br_status = f"POST-BREAKOUT (+{abs(distance_in_atr):.1f} ATR)"
    elif distance_in_atr < 0:
        score += 25; br_status = "BARU BREAKOUT"
    elif distance_in_atr <= 0.3:
        score += 25; br_status = "TEPAT DI RESISTANCE"
    elif distance_in_atr <= 0.8:
        score += 18; br_status = "DEKAT"
    elif distance_in_atr <= 1.5:
        score += 10; br_status = "MENDEKAT"
    else:
        score += 3;  br_status = "JAUH"

    if vol_ratio >= 2.5:   score += 25
    elif vol_ratio >= 1.8: score += 18
    elif vol_ratio >= 1.3: score += 12
    elif vol_ratio >= 0.8: score += 6
    else:
        score += 2
        if distance_in_atr <= 0.3:
            vol_warning = "⚠️ Vol rendah"

    score += 20 if ema5 > ema20 else 7
    if rsi14:
        if 55 <= rsi14 <= 70: score += 15
        elif 45 <= rsi14 < 55: score += 8
        elif rsi14 > 70: score += 3
        else: score += 5
    score += 15 if higher_low else 5
    if best_count >= 3: score += 5
    elif best_count == 2: score += 2
    score = int(min(100, score))

    label   = "🔥 TINGGI" if score >= 75 else "📈 SEDANG" if score >= 55 else "⬇️ RENDAH"
    mom_pct = ((float(closes.iloc[-6:].mean()) - float(closes.iloc[:6].mean()))
               / max(float(closes.iloc[:6].mean()), 1e-9) * 100) if len(closes) >= 12 else 0.0
    mom_lbl = "MENGUAT" if mom_pct > 0.5 else "MELEMAH" if mom_pct < -0.5 else "SIDEWAYS"

    return {
        "ticker": ticker, "bars": len(bars), "price": current_price,
        "open": open_price, "high": float(highs.max()), "low": float(lows.min()),
        "resistance": resistance, "cluster_count": best_count,
        "distance_pct": distance_pct, "distance_atr": distance_in_atr,
        "atr": atr_session, "vol_ratio": vol_ratio,
        "ema_bull": ema5 > ema20, "rsi14": rsi14,
        "higher_low": higher_low, "score": score, "label": label,
        "br_status": br_status, "vol_warning": vol_warning,
        "momentum": mom_lbl, "mom_pct": mom_pct,
    }

def print_result(r, expected=""):
    if r is None:
        return
    exp_str = f"  [Expected: {expected}]" if expected else ""
    print(f"\n  {r['ticker']:6} | {r['label']} ({r['score']:3}/100) | {r['br_status']}{exp_str}")
    print(f"  Harga {r['price']:.0f} | Open {r['open']:.0f} | H {r['high']:.0f} / L {r['low']:.0f}")
    print(f"  Resist {r['resistance']:.0f} ({r['cluster_count']}x) | Jarak {r['distance_pct']:+.1f}% = {r['distance_atr']:.1f}x ATR ({r['atr']:.1f})")
    print(f"  Vol {r['vol_ratio']:.2f}x | EMA {'BULL' if r['ema_bull'] else 'BEAR'} | RSI {r['rsi14']:.1f if r['rsi14'] else 'N/A'} | HL {'✅' if r['higher_low'] else '❌'}")
    print(f"  Momentum: {r['momentum']} ({r['mom_pct']:+.1f}%)"
          + (f" | {r['vol_warning']}" if r['vol_warning'] else ""))

# ──────────────────────────────────────────────────────────────
# Test matrix: breakout kemarin (KOTA, BUKA) + sideways (KICI, SMSM)
# + beberapa saham lain untuk variasi
# ──────────────────────────────────────────────────────────────
TEST_CASES = [
    # (ticker, expected_label)
    ("KOTA",  "TINGGI  — breakout kemarin"),
    ("BUKA",  "TINGGI  — breakout kemarin"),
    ("KICI",  "RENDAH/SEDANG — volatile tapi sideways"),
    ("SMSM",  "RENDAH/SEDANG — defensif, sideways"),
    ("TLKM",  "RENDAH  — blue chip, cenderung sideways"),
    ("BBCA",  "RENDAH  — sangat stabil"),
    ("ANTM",  "SEDANG  — komoditas, moderat"),
    ("ERAA",  "SEDANG/TINGGI — ada momentum kemarin"),
]

print(f"\n{'='*60}")
print(f"  BREAKOUT FORMULA TEST — {datetime.datetime.now(WIB).strftime('%Y-%m-%d %H:%M WIB')}")
print(f"{'='*60}")

results = []
for ticker, expected in TEST_CASES:
    try:
        stock = yf.Ticker(f"{ticker}.JK")
        bars  = stock.history(period="5d", interval="5m")
        if bars.empty:
            print(f"\n  {ticker}: ❌ tidak ada data")
            continue
        bars.index = bars.index.tz_convert(WIB)
        # Ambil hari terakhir yang ada data
        last_date = bars.index.date[-1]
        day_bars  = bars[bars.index.date == last_date]
        r = run_breakout(ticker, day_bars)
        if r:
            results.append(r)
            print_result(r, expected)
    except Exception as e:
        print(f"\n  {ticker}: ❌ Error — {e}")

# Summary tabel
print(f"\n\n{'='*60}")
print(f"  SUMMARY RANKING")
print(f"{'='*60}")
print(f"  {'Ticker':6} {'Score':>6} {'Label':>10}  Status")
print(f"  {'─'*50}")
for r in sorted(results, key=lambda x: x['score'], reverse=True):
    print(f"  {r['ticker']:6} {r['score']:6}/100  {r['label']}  {r['br_status']}")
