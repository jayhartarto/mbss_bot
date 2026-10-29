"""Short strategy guide (entry / TP / SL / hold days) shown under each lane header.
Numbers come from the lane engines' constants so the guide cannot drift from the picks."""
from __future__ import annotations

import engine.bull as bull
import engine.buy_on_strength as bos
import engine.buy_on_weakness as bow
import engine.macd_confirm_pillar as macd
import engine.oversold_bounce_v2 as osb
import engine.vcp_pillar as vcp

VCP_MAX_HOLD_DAYS = 12  # locked VCP recipe (2026-09-21): horizon capped 12 trading days


def _g(*lines: str) -> str:
    return "📖 " + "\n".join(lines)


def bow_guide() -> str:
    lo = min(r[0] for r in bow.TIER_SWING_RANGE.values())
    hi = max(r[2] for r in bow.TIER_SWING_RANGE.values())
    return _g("Entry: di kisaran Entry (low–high)",
              "TP: TP1 lalu TP2 (angka tiap pick)",
              f"SL: -{bow.SL_PCT:.0f}%",
              f"Tahan: ~{lo}–{hi} hari (lihat tiap pick)")


def osb_guide() -> str:
    return _g("Entry: di Entry ref",
              f"TP: +{osb.TP1_PCT:.0f}% (TP1) / +{osb.TP2_PCT:.0f}% (TP2)",
              f"SL: {osb.SL_PCT:.0f}%",
              f"BE: setelah naik +{osb.BE_TRIGGER_PCT:.0f}%, geser SL ke harga beli (impas, tidak bisa rugi lagi)",
              f"Tahan: maks {osb.ALERT_MAX_AGE_DAYS} hari")


def vcp_guide() -> str:
    return _g(f"Entry: di Entry ref, hanya hari 1–{vcp.ALERT_MAX_AGE_DAYS}",
              f"TP: +{vcp.TP1_PCT:g}% / +{vcp.TP2_PCT:g}% / +{vcp.TP3_PCT:g}%",
              f"SL: -{vcp.SL_PCT:.0f}%",
              f"Tahan: maks {VCP_MAX_HOLD_DAYS} hari")


def macd_guide() -> str:
    return _g("Entry: di Entry ref",
              f"TP: +{macd.TP_PCT:.0f}%",
              f"SL: -{macd.SL_PCT:.0f}%",
              f"Tahan: maks {macd.ALERT_MAX_AGE_DAYS} hari bursa")


def bos_guide() -> str:
    return _g("Entry: di Entry ref",
              f"TP: +{bos.TP_PCT:.0f}%",
              f"SL: {bos.SL_PCT_NORMAL:.0f}% (jadi {bos.SL_PCT_TIGHT:.0f}% bila hari 1 tutup merah ≥2%)",
              f"Tahan: ~{bos.MAX_HOLD_GUIDANCE_DAYS} hari")


def bull_guide() -> str:
    return _g("Entry: beli di open besok setelah alert (alert dikonfirmasi 2x dalam 3 hari)",
              f"TP: +{bull.TP_PCT:.0f}%",
              f"SL: -{bull.SL_PCT:.0f}%",
              f"Trailing: setelah naik +{bull.TRAIL_ARM_PCT:.0f}%, SL naik mengikuti {bull.TRAIL_WIDTH_PCT:.0f}% di bawah harga tertinggi",
              f"Tahan: maks {bull.MAX_HOLD_DAYS} hari")
