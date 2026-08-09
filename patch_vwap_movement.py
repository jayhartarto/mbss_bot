from pathlib import Path
import re, textwrap

path = Path("bot_dev.py")
src = path.read_text(encoding="utf-8")

helper_block = textwrap.dedent('''

def _compute_intraday_vwap_window(bars: pd.DataFrame, min_bars: int) -> dict:
    """Hitung VWAP movement untuk satu window intraday pendek."""
    if bars is None or bars.empty or len(bars) < min_bars:
        return {"available": False, "reason": f"data belum cukup (<{min_bars} bar)"}

    try:
        bars = bars.dropna().copy()
        if bars.empty or len(bars) < min_bars:
            return {"available": False, "reason": f"data belum cukup (<{min_bars} bar)"}

        closes = bars["Close"].astype(float)
        highs = bars["High"].astype(float)
        lows = bars["Low"].astype(float)
        volumes = bars["Volume"].fillna(0).astype(float)

        open_price = float(bars["Open"].iloc[0])
        current_price = float(closes.iloc[-1])
        total_vol = float(volumes.sum())
        if total_vol <= 0:
            return {"available": False, "reason": "volume nol"}

        typical_price = (highs + lows + closes) / 3
        vwap_val = float((typical_price * volumes).sum() / total_vol)
        vwap_distance_pct = ((current_price - vwap_val) / max(vwap_val, 1e-9)) * 100
        change_from_open_pct = ((current_price - open_price) / max(open_price, 1e-9)) * 100

        if vwap_distance_pct >= 0.30:
            signal = "naik"
            bias = "bullish"
        elif vwap_distance_pct <= -0.30:
            if change_from_open_pct <= -0.30:
                signal = "fall down"
            else:
                signal = "pullback"
            bias = "bearish"
        else:
            signal = "sideways"
            bias = "netral"

        return {
            "available": True,
            "price": round(current_price, 2),
            "open_price": round(open_price, 2),
            "vwap": round(vwap_val, 2),
            "vwap_distance_pct": round(vwap_distance_pct, 2),
            "change_from_open_pct": round(change_from_open_pct, 2),
            "signal": signal,
            "bias": bias,
            "bars": int(len(bars)),
        }
    except Exception as e:
        return {"available": False, "reason": str(e)[:120]}


def compute_vwap_movement_context(ticker: str) -> dict:
    """
    VWAP movement 15m / 30m / 60m untuk /check saat market open.
    Menggunakan 5m bars dari Yahoo lalu melihat window terakhir 3/6/12 bar.
    """
    bars = yfinance_get_intraday_5m(ticker, period="1d")
    if bars is None or bars.empty:
        return {
            "available": False,
            "reason": "data intraday belum tersedia",
            "15m": {"available": False, "reason": "data intraday belum tersedia"},
            "30m": {"available": False, "reason": "data intraday belum tersedia"},
            "60m": {"available": False, "reason": "data intraday belum tersedia"},
            "overall_signal": "N/A",
        }

    session = get_current_idx_session()
    if session is None:
        return {
            "available": False,
            "reason": "di luar jam bursa",
            "15m": {"available": False, "reason": "di luar jam bursa"},
            "30m": {"available": False, "reason": "di luar jam bursa"},
            "60m": {"available": False, "reason": "di luar jam bursa"},
            "overall_signal": "N/A",
        }

    bars = _normalize_intraday_index(bars).dropna()
    _, start_dt, _ = _get_session_window()
    session_bars = bars[bars.index >= start_dt].dropna() if start_dt is not None else bars
    if session_bars is None or session_bars.empty:
        return {
            "available": False,
            "reason": "data intraday belum tersedia",
            "15m": {"available": False, "reason": "data intraday belum tersedia"},
            "30m": {"available": False, "reason": "data intraday belum tersedia"},
            "60m": {"available": False, "reason": "data intraday belum tersedia"},
            "overall_signal": "N/A",
        }

    window_map = {"15m": 3, "30m": 6, "60m": 12}
    out = {"available": False, "session": session, "overall_signal": "N/A"}
    score_map = []

    for label, min_bars in window_map.items():
        window_bars = session_bars.tail(min_bars)
        res = _compute_intraday_vwap_window(window_bars, min_bars)
        out[label] = res
        if res.get("available"):
            out["available"] = True
            score_map.append(1 if res.get("bias") == "bullish" else -1 if res.get("bias") == "bearish" else 0)

    if score_map:
        total = sum(score_map)
        if total >= 2:
            out["overall_signal"] = "bullish"
        elif total <= -2:
            out["overall_signal"] = "bearish"
        else:
            out["overall_signal"] = "mixed"

    return out


def format_vwap_movement_block(vwap_move: dict) -> str:
    """Format ringkas VWAP movement untuk ditampilkan di /check."""
    lines = ["📊 VWAP MOVEMENT"]

    if not vwap_move or not vwap_move.get("available"):
        lines.append("15m : N/A")
        lines.append("30m : N/A")
        lines.append("60m : N/A")
        lines.append("Bias: N/A")
        return "\\n".join(lines)

    for label in ("15m", "30m", "60m"):
        d = vwap_move.get(label) or {}
        if not d.get("available"):
            lines.append(f"{label} : N/A")
            continue

        price_txt = f"{int(round(float(d.get('price', 0)))):,}".replace(",", ".")
        vwap_txt = f"{int(round(float(d.get('vwap', 0)))):,}".replace(",", ".")
        dist = d.get("vwap_distance_pct")
        dist_txt = f"{dist:+.2f}%" if isinstance(dist, (int, float)) else "N/A"
        signal = d.get("signal", "N/A")
        if signal == "naik":
            icon = "↑"
        elif signal in ("fall down", "pullback"):
            icon = "↓"
        else:
            icon = "→"
        lines.append(f"{label} : Harga {price_txt} | VWAP {vwap_txt} ({dist_txt}) | {signal} {icon}")

    lines.append(f"Bias: {vwap_move.get('overall_signal', 'N/A')}")
    return "\\n".join(lines)
''')

if "def _compute_intraday_vwap_window(" not in src:
    src = src.replace(
        "def _normalize_intraday_index(df: pd.DataFrame) -> pd.DataFrame:",
        helper_block + "\n\ndef _normalize_intraday_index(df: pd.DataFrame) -> pd.DataFrame:",
        1,
    )

src = re.sub(
    r'active_breakout = compute_active_breakout_score\(ticker\)\n\n    return \{\n(?P<body>[\s\S]*?)\n    \}',
    lambda m: 'active_breakout = compute_active_breakout_score(ticker)\n    vwap_movement = compute_vwap_movement_context(ticker)\n\n    return {\n' + m.group('body') + '\n        "vwap_movement": vwap_movement,\n    }',
    src,
    count=1,
)

src = src.replace(
    '''        result["intraday_momentum"] = intraday_ctx.get("momentum", {"available": False, "reason": "error teknis"})
        result["intraday_breakout"] = intraday_ctx.get("breakout", {"available": False, "reason": "error teknis"})
        result["active_breakout"] = intraday_ctx.get("active_breakout", {"available": False, "reason": "error teknis"})
        if intraday_ctx.get("available"):
''',
    '''        result["intraday_momentum"] = intraday_ctx.get("momentum", {"available": False, "reason": "error teknis"})
        result["intraday_breakout"] = intraday_ctx.get("breakout", {"available": False, "reason": "error teknis"})
        result["active_breakout"] = intraday_ctx.get("active_breakout", {"available": False, "reason": "error teknis"})
        result["vwap_movement"] = intraday_ctx.get("vwap_movement", {"available": False, "reason": "error teknis"})
        if intraday_ctx.get("available"):
''',
    1,
)

src = src.replace(
    '''    except Exception as e:
        print(f"⚠️ Gagal fetch intraday context untuk {ticker}: {e}")
        result["intraday_momentum"] = {"available": False, "reason": "error teknis"}
        result["intraday_breakout"] = {"available": False, "reason": "error teknis"}
        result["active_breakout"] = {"available": False, "reason": "error teknis"}''',
    '''    except Exception as e:
        print(f"⚠️ Gagal fetch intraday context untuk {ticker}: {e}")
        result["intraday_momentum"] = {"available": False, "reason": "error teknis"}
        result["intraday_breakout"] = {"available": False, "reason": "error teknis"}
        result["active_breakout"] = {"available": False, "reason": "error teknis"}
        result["vwap_movement"] = {"available": False, "reason": "error teknis", "15m": {"available": False, "reason": "error teknis"}, "30m": {"available": False, "reason": "error teknis"}, "60m": {"available": False, "reason": "error teknis"}, "overall_signal": "N/A"}''',
    1,
)

src = src.replace(
    '''    if im.get("available"):
        sess = "Sesi 1" if im["session"] == "sesi_1" else "Sesi 2"
        intraday_status += f"Momentum {sess}: {im['reading']} ({im['change_pct']:+.2f}%)\\n"
    elif hi and lo:
        intraday_status += "Momentum: di luar jam bursa\\n"

    br = result.get("intraday_breakout", {})''',
    '''    if im.get("available"):
        sess = "Sesi 1" if im["session"] == "sesi_1" else "Sesi 2"
        intraday_status += f"Momentum {sess}: {im['reading']} ({im['change_pct']:+.2f}%)\\n"
    elif hi and lo:
        intraday_status += "Momentum: di luar jam bursa\\n"

    vwap_movement = result.get("vwap_movement") or {}
    intraday_status += format_vwap_movement_block(vwap_movement) + "\\n"

    br = result.get("intraday_breakout", {})''',
    1,
)

path.write_text(src, encoding="utf-8")
print("patched:", path)
