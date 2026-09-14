# engine/daytrade_2d_screen.py
"""
DAYTRADE-2D screen A/B/C — wired ke /screendaytrade (user request 2026-09-14).

Sumber riset: research/daytrade_2d_mfe_screen_2026_09_14.md
Script riset:  research/daytrade_2d_mfe_screen_v1.py

Metrik riset (entry = OPEN D+1, hold maks 2 hari, gain = MFE_2d, risk = close_2d
BUKAN MAE) — angka TEST (out-of-sample, split 2026-01-15, 626 ticker/2thn):

  Screen A (STABIL)     : dist_high50>=-2 & foreign_net_ratio_1d>=5% & harga<=500
  Screen B (BROAD)      : rsi>=70 & foreign_net_ratio_1d>=5% & 1<=atr_pct14<=6
  Screen C (CONVICTION) : dist_high50>=0 & value_traded_20d_avg>=1e9 & foreign_net_ratio_1d>0

Semua threshold adalah HASIL RISET — jangan re-tune tanpa 20-30 hari data forward
(disiplin proyek, lihat .claude/skills/finance).

CATATAN FIELD PRODUKSI:
  - `dist_high50` riset = (close-high50)/high50*100 (negatif = di bawah high).
    Produksi `dist_to_50d_high_pct` = (high50-close)/close*100 (positif = di bawah
    high) -> kondisi A `dist_high50>=-2` ≈ `dist_to_50d_high_pct<=2.0`,
    C `dist_high50>=0` ≈ `dist_to_50d_high_pct<=0.0`.
  - `foreign_net_ratio_1d` = foreign_net/volume (engine/nightly.py, D-1). MISSING =
    netral -> kandidat tidak muncul (bukan dihukum), TIDAK hard-block command.
  - `atr_pct14` = ATR(14 Wilder)/close*100; `value_traded_20d_avg` = rata2
    close*volume 20 hari (engine/scoring.py).
"""
from __future__ import annotations

import math

# --- thresholds tervalidasi (JANGAN ubah tanpa data forward) ---
FF_MIN = 0.05              # foreign_net_ratio_1d >= 5%
A_PRICE_MAX = 500          # fraksi/tick rendah (Scalping Trader §3)
A_DIST50_MAX = 2.0         # dist_to_50d_high_pct <= 2.0  (~ dist_high50 >= -2%)
B_RSI_MIN = 70.0
B_ATR_MIN = 1.0
B_ATR_MAX = 6.0
C_DIST50_MAX = 0.0         # dist_high50 >= 0 (di/atas high 50 hari)
C_LIQ_MIN = 1e9            # value_traded_20d_avg >= Rp1 M

# --- spec level eksekusi (dari riset: gain target MFE +3%, risk close_2d <2%) ---
TP_PCT = 3.0
SL_PCT = 2.0
AVGDOWN_PCT = -2.0
RR_NOMINAL = TP_PCT / SL_PCT  # 1.50

SCREEN_META = {
    "A": {
        "label": "A · STABIL",
        "rule": "dist_high50>=-2 & FF>=5% & harga<=500",
        "note": "risk-adjusted terbaik: close_2d rata2 POSITIF, 81% loss <3%",
        "n_per_day": 1.1, "mfe": 5.34, "p_mfe3": 48.6, "c2": 1.25,
        "win_close": 48.1, "win_cap": 61.1, "loss_fail": -1.58,
        "exp_cap": 0.65, "rr_real": 1.90,
    },
    "B": {
        "label": "B · BROAD",
        "rule": "rsi>=70 & FF>=5% & atr_pct14 1-6",
        "note": "kandidat terbanyak & win-close tertinggi (51.5% vs baseline 39%)",
        "n_per_day": 4.0, "mfe": 4.33, "p_mfe3": 44.4, "c2": 0.84,
        "win_close": 51.5, "win_cap": 60.3, "loss_fail": -1.66,
        "exp_cap": 0.41, "rr_real": 1.81,
    },
    "C": {
        "label": "C · CONVICTION",
        "rule": "dist_high50>=0 & value_20d_avg>=1e9 & FF>0",
        "note": "MFE/win-cap tertinggi (7.35%/70%) tapi tail paling gemuk",
        "n_per_day": 1.1, "mfe": 7.35, "p_mfe3": 54.5, "c2": -0.46,
        "win_close": 47.2, "win_cap": 70.2, "loss_fail": -2.50,
        "exp_cap": 0.50, "rr_real": 1.20,
    },
}


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and not math.isnan(x) and not math.isinf(x)


def _passes_A(r: dict) -> bool:
    ff = r.get("foreign_net_ratio_1d")
    d50 = r.get("dist_to_50d_high_pct")
    price = r.get("price")
    return (_finite(ff) and _finite(d50) and _finite(price)
            and ff >= FF_MIN and d50 <= A_DIST50_MAX and price <= A_PRICE_MAX)


def _passes_B(r: dict) -> bool:
    ff = r.get("foreign_net_ratio_1d")
    rsi = r.get("rsi")
    atr = r.get("atr_pct14")
    return (_finite(ff) and _finite(rsi) and _finite(atr)
            and rsi >= B_RSI_MIN and ff >= FF_MIN and B_ATR_MIN <= atr <= B_ATR_MAX)


def _passes_C(r: dict) -> bool:
    ff = r.get("foreign_net_ratio_1d")
    d50 = r.get("dist_to_50d_high_pct")
    liq = r.get("value_traded_20d_avg")
    return (_finite(ff) and _finite(d50) and _finite(liq)
            and ff > 0 and d50 <= C_DIST50_MAX and liq >= C_LIQ_MIN)


_PASS = {"A": _passes_A, "B": _passes_B, "C": _passes_C}


def compute_levels(price: float) -> dict:
    """Entry ref (open sesi berikutnya) + avg-down 1x + TP/SL. Rounding tick IDX
    dilakukan di layer command (reuse scanalert._idx_round_tick)."""
    if not _finite(price) or price <= 0:
        return {}
    return {
        "entry_ref": price,
        "avg_down": price * (1 + AVGDOWN_PCT / 100.0),
        "tp": price * (1 + TP_PCT / 100.0),
        "sl": price * (1 - SL_PCT / 100.0),
        "rr": RR_NOMINAL,
    }


def select_daytrade_2d_candidates(results: list) -> dict:
    """results = daftar scoring-dict (sama yg dipakai /screendaytrade lain).
    Return {"A":[...], "B":[...], "C":[...]} — tiap item = dict scoring + level.
    Screen independen satu sama lain (ticker bisa muncul di >1 screen)."""
    out = {"A": [], "B": [], "C": []}
    for r in results:
        for key, fn in _PASS.items():
            try:
                ok = fn(r)
            except Exception:
                ok = False
            if not ok:
                continue
            lv = compute_levels(r.get("price"))
            if not lv:
                continue
            item = dict(r)
            item["screen"] = key
            item.update(lv)
            # lock_daily_daytrade_picks baca r["targets"]["tp_1"/"cut_loss"] utk
            # winrate tracking -> override ke level screen ini (+3%/-2%), copy
            # dict-nya dulu supaya TIDAK memutasi targets asli ticker.
            item["targets"] = dict(r.get("targets") or {})
            item["targets"]["tp_1"] = int(round(lv["tp"]))
            item["targets"]["cut_loss"] = int(round(lv["sl"]))
            # label utk /winrate supaya bisa disegmentasi per screen.
            item["_positive_lane"] = f"2D-{key}"
            out[key].append(item)
    for key in out:
        out[key].sort(key=lambda x: x.get("foreign_net_ratio_1d") or 0, reverse=True)
    return out


def any_foreign_flow_available(results: list) -> bool:
    """True kalau minimal 1 ticker punya foreign_net_ratio_1d — dipakai command
    untuk kasih catatan kalau nightly foreign-flow belum jalan (missing=neutral)."""
    return any(r.get("foreign_net_ratio_1d") is not None for r in results)
