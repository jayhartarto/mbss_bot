"""
engine/broker_flags.py — MBSS v2 (2026-10-21).

Display-only broker warning for pick lists (/swing, /rank): "XL ramai beli". Reads research/broker_flow_history.sqlite (nightly
sweep, single-day rows; earlier weekly backfill rows are read the same way) and normalises XL's net by the stock's traded value over
the span those rows cover. NEVER hides a pick and NEVER touches a rank score (booster-over-gate preference; the evidence is a
contrarian effect from 18 weeks of one broker: XL net >= +1% of value -> -1.0%/week vs rest, t -3.9; see engine/broker_roles.py).
Data is flagged as partial/stale instead of silently shown as fresh.
"""
from __future__ import annotations

import datetime

import engine.broker_roles as roles


def xl_flags(tickers: list, since_days: int = 8) -> dict:
    """{ticker: {"pct": XL net / traded value, "level": "strong"|"warn", "as_of": str, "days": int, "stale": bool}} — ONLY for
    tickers at/above the warning threshold. Best-effort: any failure returns {} (a missing tag must never break /swing or /rank)."""
    try:
        import engine.broker_flow_db as flow_db
        import engine.legacy_core as core
        import engine.nightly as nightly
        tickers = sorted({t for t in tickers if t})
        if not tickers:
            return {}
        conn = flow_db.connect(nightly.BROKER_FLOW_DB)
        try:
            first = flow_db.recent_flow_summary(conn, tickers, None, since_days=since_days)
            out = {}
            today = datetime.date.today()
            for t, rec in first.items():
                if rec.get("stale") or "XL" not in rec["net_idr_by_broker"]:
                    continue
                df = core.get_ohlcv_daily_from_db(t, 40)
                if df is None or df.empty:
                    continue
                idx = df.index.strftime("%Y-%m-%d")
                m = df[(idx >= rec["from"]) & (idx <= rec["as_of"])]
                val = float((m["Close"] * m["Volume"]).sum()) if not m.empty else 0.0
                if val <= 0:
                    continue
                pct = rec["net_idr_by_broker"]["XL"] / val
                if pct >= roles.XL_CROWD_BUY:
                    age = (today - datetime.date.fromisoformat(rec["as_of"])).days
                    out[t] = {"pct": pct, "level": "strong" if pct >= roles.XL_CROWD_BUY_STRONG else "warn",
                              "as_of": rec["as_of"], "days": int(len(m)), "stale": age > 4}
            return out
        finally:
            conn.close()
    except Exception as e:  # noqa: BLE001
        print(f"⚠️ xl_flags gagal (diabaikan): {e}")
        return {}


def short_tag(flag: dict) -> str:
    """Compact inline tag for a pick header."""
    icon = "⚠️⚠️" if flag["level"] == "strong" else "⚠️"
    part = " (data parsial)" if flag["days"] < 4 else ""
    stale = f" [{flag['as_of'][5:]}]" if flag["stale"] else ""
    return f" {icon}XL beli {flag['pct']*100:+.1f}%{stale}{part}"


def summary_line(flags: dict) -> str:
    """One closing line for /swing; empty when nothing is flagged."""
    if not flags:
        return ""
    items = ", ".join(f"{t} {f['pct']*100:+.1f}%" for t, f in sorted(flags.items(), key=lambda kv: -kv[1]["pct"]))
    return (f"\n⚠️ XL (Stockbit) ramai beli di antara pick aktif: {items}. Peringatan kontrarian, bukti LEMAH (backtest 18 pekan: "
            f"−1,0%/pekan vs pasar, t −3,9), pick TIDAK disembunyikan dan skor tidak berubah. Cek detail: /bandar TICKER.")
