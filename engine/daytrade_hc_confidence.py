# -*- coding: utf-8 -*-
"""
MBSS v2 (Lane Lifecycle Redesign, user request 2026-08-30): individual WR
utk DAY TRADE / EOD High Conviction section /go -- PENGGANTI target
statis lama (yang dari targets dict compute_intraday_targets, tidak
pernah punya WR terkalibrasi sama sekali).

Populasi: proksi HC (>=4/5 kriteria compute_high_conviction_score yg
daily-computable). Fitur: day_range_pct_10d, vol_ratio, value_traded,
relative_strength_vs_ihsg -- PERSIS field yg sudah ada di
compute_factor_scoring's return dict (bukan versi custom/beda window).
relative_strength_vs_ihsg ditambah 2026-08-30 setelah terbukti corr=0.175
thd target (sebanding fitur lain) DAN dikonfirmasi predictive power-nya
BERTAHAN sampai entry Open(D+1) (metrik RS itu SENDIRI mean-revert
hari-ke-hari, corr=-0.077 rs_today->rs_tomorrow, TAPI itu pertanyaan
BEDA dari "apakah masih prediktif thd harga absolut esok" -- lihat
riwayat chat 2026-08-30 utk penjelasan lengkap). SENGAJA BUKAN
criteria_met (jumlah kriteria HC terpenuhi) krn terbukti TIDAK prediktif
thd same-day (corr=-0.018) MAUPUN horizon asli HC 5-hari (corr=0.02) --
backlog audit HC gate individual criteria masih terbuka terpisah (lihat
TaskList #21).

Target: SAME-DAY -- entry Open(D+1), exit SAMA HARI (High D+1 vs Open
D+1). AUC=0.611, top-decile actual WR=69.2% (out-of-sample validasi,
naik dari 52.7% versi 3-fitur sebelum relative_strength_vs_ihsg
ditambah -- AUC keseluruhan nyaris tidak berubah, tapi top-decile jauh
lebih tajam) -- floor TP1 60% (sama konvensi lane_confidence.py/
swing_horizon_confidence.py) SEKARANG tercapai, tidak perlu diturunkan lagi.

PENTING: close-based (hold Open D+1 -> Close D+1) median NEGATIF
(-0.72%, 28.3% positif, exit_eff=0.00) -- SAMA pola give-back dgn BSJP.
TP1 di sini HANYA valid dgn disiplin EXIT CEPAT, bukan hold-ke-closing --
caller WAJIB tampilkan warning ini, lihat DAYTRADE_EXIT_WARNING.
"""
from __future__ import annotations

import json
import math
import os

_CONSTANTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "daytrade_hc_constants.json")

with open(_CONSTANTS_FILE) as _f:
    _MODEL: dict = json.load(_f)

# Sama konvensi TP1_WR_TARGET_PCT lane_confidence.py -- floor 60% SEKARANG
# tercapai (top-decile empiris 69.2%) setelah relative_strength_vs_ihsg
# ditambah, tidak perlu lagi diturunkan spt versi 3-fitur sebelumnya.
TP_WR_FLOOR_PCT = 60.0

DAYTRADE_EXIT_WARNING = "TP CEPAT, JANGAN tahan sampai closing -- data: hold ke closing median RUGI (-0.72%, cuma 28% positif)."


def _predict_prob(level_key: str, features: dict) -> float | None:
    m = _MODEL.get("levels", {}).get(str(level_key))
    if m is None:
        return None
    x = []
    for col in m["feat_cols"]:
        val = features.get(col)
        if val is None:
            return None
        x.append(val)
    mu, sigma, coef, intercept = m["mu"], m["sigma"], m["coef"], m["intercept"]
    z = intercept + sum(c * ((xi - mi) / si) for c, xi, mi, si in zip(coef, x, mu, sigma))
    try:
        return 1.0 / (1.0 + math.exp(-z))
    except OverflowError:
        return 0.0 if z < 0 else 1.0


def compute_tp1(features: dict, ref_price: float) -> dict | None:
    """
    {"level_pct", "wr_pct", "price"} utk level TERJAUH yg WR-nya masih
    >=TP_WR_FLOOR_PCT (asumsi WR menurun monoton makin jauh level, sama
    pola compute_tp1_tp2 lane_confidence.py) -- None kalau fitur tak
    lengkap ATAU level terdekat sudah di bawah floor.
    """
    if not ref_price:
        return None
    level_keys = sorted(_MODEL["levels"].keys(), key=lambda k: float(k))
    best_level_key = None
    best_wr = None
    for lvl_key in level_keys:
        prob = _predict_prob(lvl_key, features)
        if prob is None:
            return None  # fitur tak lengkap
        if prob * 100 >= TP_WR_FLOOR_PCT:
            best_level_key = lvl_key
            best_wr = prob * 100
        else:
            break
    if best_level_key is None:
        return None
    level_pct = float(best_level_key)
    return {"level_pct": level_pct, "wr_pct": round(best_wr, 1), "price": ref_price * (1 + level_pct / 100.0)}
