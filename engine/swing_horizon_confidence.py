# -*- coding: utf-8 -*-
"""
MBSS v2 (Lane Lifecycle Redesign, user request 2026-08-29/30): tabel
ekspektasi horizon SHORT(1-2D)/MEDIUM(1-3D)/SWING(1-10D) -- PELENGKAP
TP1/TP2 dari engine/lane_confidence.py (model lane LAMA tetap primary/
proven utk TP1/TP2 konkret; model INI khusus utk "seberapa cepat/jauh
realistis" lintas SELURUH lifecycle post-cross sekaligus, BUKAN kotak2
lane terpisah -- lihat research/MBSS_Lane_Lifecycle_Redesign_Brief.md &
scratchpad/train_swing_horizon_model.py utk metodologi lengkap backtest).

Populasi TRAINING: unified post-cross (0<=days_ago<=40 sejak bullish
MACD cross terakhir, regime ABOVE) -- SATU populasi kontinu (FCM/
VALIDATION/CONTINUATION/MOMENTUM_EXTENDED semua masuk sini, tidak
dipisah). Fitur: dist_to_20d_high, adx14, gain_since_cross (+ gain_sq
utk U-shape -- pulled-back ATAU sudah proven-strong sama2 bagus,
zona 0-7% justru TERLEMAH), days_ago. AUC~0.66, top-decile actual WR
65-75% (out-of-sample validasi discovery/validation split sesi
2026-08-29/30) -- model final di sini di-refit di SEMUA data setelah
validasi itu, konvensi sama dgn lane_confidence.py.

TIDAK ADA individual WR yg dihitung terpisah utk fitur ini di luar
model (beda dari lane_confidence.py yg juga dipakai should_suppress) --
modul ini KHUSUS ekspektasi horizon, bukan pengganti suppress/gate
lane manapun.
"""
from __future__ import annotations

import json
import math
import os

_CONSTANTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "swing_horizon_constants.json")

with open(_CONSTANTS_FILE) as _f:
    _MODEL: dict = json.load(_f)

HORIZONS = ["SHORT", "MEDIUM", "SWING"]
GAIN_CENTER = _MODEL.get("gain_center", 1.0)

# Sama konvensi TP1_WR_TARGET_PCT di lane_confidence.py -- level TERJAUH
# yg WR-nya masih >=floor ini yg ditampilkan (bukan level tetap/hardcode).
EXPECTATION_WR_FLOOR_PCT = 60.0


def _predict_prob(horizon: str, level_key: str, features: dict) -> float | None:
    """None kalau fitur tak lengkap -- caller treat sbg 'tidak bisa dihitung utk horizon ini'."""
    horizon_data = _MODEL.get(horizon)
    if horizon_data is None:
        return None
    m = horizon_data.get("levels", {}).get(str(level_key))
    if m is None:
        return None
    x = []
    for col in m["feat_cols"]:
        if col == "gain_sq":
            base = features.get("gain_since_cross")
            if base is None:
                return None
            x.append((base - GAIN_CENTER) ** 2)
        else:
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


def compute_horizon_expectation(features: dict, ref_price: float) -> dict:
    """
    {"SHORT": {"level_pct", "wr_pct", "price", "window_days"}, "MEDIUM": {...}, "SWING": {...}}
    -- HANYA horizon yg py level dgn WR>=EXPECTATION_WR_FLOOR_PCT yg
    muncul (level TERJAUH yg masih lolos, asumsi WR menurun monoton
    makin jauh level -- sama pola compute_tp1_tp2 lane_confidence.py).
    Horizon dgn fitur tak lengkap ATAU level terdekat sudah di bawah
    floor TIDAK muncul di dict sama sekali (bukan tampil dgn angka
    lemah/kosong).
    """
    if not ref_price:
        return {}
    result = {}
    for horizon in HORIZONS:
        level_keys = sorted(_MODEL[horizon]["levels"].keys(), key=lambda k: float(k))
        best_level_key = None
        best_wr = None
        for lvl_key in level_keys:
            prob = _predict_prob(horizon, lvl_key, features)
            if prob is None:
                break  # fitur tak lengkap -- skip seluruh horizon ini
            if prob * 100 >= EXPECTATION_WR_FLOOR_PCT:
                best_level_key = lvl_key
                best_wr = prob * 100
            else:
                break  # WR sudah di bawah floor, berhenti (monoton)
        if best_level_key is not None:
            level_pct = float(best_level_key)
            result[horizon] = {
                "level_pct": level_pct, "wr_pct": round(best_wr, 1),
                "price": ref_price * (1 + level_pct / 100.0),
                "window_days": _MODEL[horizon]["window_days"],
            }
    return result


def format_expectation_lines(expectation: dict) -> list[str]:
    """["SHORT (1-2D)   877  WR 63%", ...] -- urutan tetap SHORT->MEDIUM->SWING, skip horizon yg absen di dict."""
    lines = []
    for horizon in HORIZONS:
        info = expectation.get(horizon)
        if info is None:
            continue
        lines.append(f"{horizon} ({info['window_days']}D)   {info['price']:,.0f}  WR {info['wr_pct']:.0f}%")
    return lines
