# -*- coding: utf-8 -*-
"""
Individualized per-ticker confidence (TP1/TP2 + win-rate) untuk setup lane
MACD (FCM/ABOVE_MOMENTUM/CONTINUATION/VALIDATION/MOMENTUM_EXTENDED) --
MBSS v2, user request 2026-08-27. Gantikan angka rata-rata grup statis
(LANE_INFO, commands/scan.py) dengan confidence per ticker berdasar nilai
indikator dia SENDIRI (dist_to_sma20, pct_b, ret10_pre_cross_pct,
gap_slope_3d), pakai logistic regression TERPISAH per level TP (+6% s/d
+15%, 1% per step), dilatih 576 ISSI/2thn dengan metodologi split
kronologis 70/30 (discovery/validation) PERSIS research/brights_imminent_
cross_backtest_v1.py's split_period() -- semua 5 lane terbukti AUC 0.58-
0.66 & Brier score lebih baik dari baseline rata-rata grup DI DATA
VALIDATION (belum pernah dilihat model saat fit), lihat riwayat chat sesi
2026-08-27 untuk detail lengkap backtest per lane.

FAST_RECOVERY/EARLY_RECOVERY SENGAJA TIDAK ADA DI lane_confidence_
constants.json -- angka resmi (62.35%/59.2%, dari research/brights_
imminent_cross_backtest_v1.py) beda cukup jauh dari quick-recompute
sandbox sesi ini (45.0%/52.9%), belum diinvestigasi kenapa. Jangan taruh
model individual di atas fondasi yang sendiri belum jelas -- caller WAJIB
fallback ke angka statis untuk dua tier ini (cek `lane in SUPPORTED_LANES`
sebelum memanggil compute_tp1_tp2).

Model di-refit pakai data PENUH (discovery+validation digabung) SESUDAH
metodologi split di atas membuktikan pendekatannya valid di data yang
model belum pernah lihat -- praktik standar: validasi di held-out split,
baru deploy model final pakai semua data yang ada untuk memaksimalkan
presisi produksi.
"""
from __future__ import annotations

import json
import math
import os

_CONSTANTS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lane_confidence_constants.json")

with open(_CONSTANTS_FILE) as _f:
    _MODELS: dict = json.load(_f)

LEVELS = [6, 7, 8, 9, 10, 11, 12, 13, 14, 15]

# MBSS v2 (user request 2026-08-27 -- "adaptif saja dengan tp1, yang di
# lock wr>50% nya, bukan 6% nya... kalau convictionnya tinggi, TP1 ambil
# di wr>60% dulu"): TP1 BUKAN dikunci di level +6% -- dia level TERJAUH
# yang WR individual masih >=TP1_WR_TARGET_PCT (conviction tinggi -> TP1
# maju lebih jauh dari +6% secara otomatis), fallback ke level terdekat
# (+6%) kalau TIDAK ADA level yang tembus 60% sama sekali. TP2 tetap floor
# lama (>=50%) sebagai ceiling terjauh yang masih layak.
TP1_WR_TARGET_PCT = 60.0
TP2_WR_FLOOR_PCT = 50.0

SUPPORTED_LANES = set(_MODELS.keys())  # FCM, ABOVE_MOMENTUM, CONTINUATION, VALIDATION, MOMENTUM_EXTENDED (FAST_RECOVERY/EARLY_RECOVERY sengaja absen)


def _predict_prob(lane: str, level: int, features: dict) -> float | None:
    """None kalau fitur yang dibutuhkan lane/level ini tidak lengkap -- caller HARUS treat sebagai 'tidak bisa dihitung', bukan 0%."""
    lane_models = _MODELS.get(lane)
    if lane_models is None:
        return None
    m = lane_models.get("levels", {}).get(str(level))
    if m is None:
        return None
    ret10_center = lane_models.get("ret10_center")
    x = []
    for col in m["feat_cols"]:
        if col == "ret10_pre_cross_pct_sq":
            base = features.get("ret10_pre_cross_pct")
            if base is None or ret10_center is None:
                return None
            x.append((base - ret10_center) ** 2)
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


def compute_level_probabilities(lane: str, features: dict) -> dict[int, float | None]:
    """{level: prob_individual (0-1)} untuk semua level 6-15%. Dict kosong kalau lane tidak didukung (lihat SUPPORTED_LANES)."""
    if lane not in SUPPORTED_LANES:
        return {}
    return {lvl: _predict_prob(lane, lvl, features) for lvl in LEVELS}


# MBSS v2 (BUGFIX, user report 2026-08-27 -- live case VERN: setelah push,
# /screendaytrade & /hc jadi KOSONG TOTAL, padahal /check masih benar
# menampilkan VERN FCM). Root cause: caller (commands/scan.py, engine/
# scanalert.py) menyamakan compute_tp1_tp2()==None dgn "suppress ticker
# ini" -- tapi None JUGA muncul kalau fitur (pct_b dkk) TIDAK LENGKAP,
# bukan cuma kalau WR genuinely <50%. Cache /eodscan semalam dihitung
# SEBELUM pct_b ditambahkan ke compute_factor_scoring, jadi SEMUA ticker
# di cache itu punya pct_b=None -> compute_tp1_tp2 selalu None -> SEMUA
# ticker ke-suppress, bukan cuma yg genuinely lemah. /check tidak kena
# krn dia compute_factor_scoring LIVE (fresh, kode baru, pct_b terisi).
# should_suppress() memisahkan dua kasus ini secara eksplisit -- caller
# WAJIB pakai INI utk keputusan suppress, BUKAN `compute_tp1_tp2(...) is
# None` (konsisten "missing=neutral, never penalize" convention codebase
# ini, lihat skill finance). compute_tp1_tp2 sendiri TETAP bisa return
# None utk fitur tak lengkap -- itu OK utk PENAMPILAN (fallback ke teks
# statis lama), tapi TIDAK BOLEH dipakai utk keputusan suppress lagi.
def should_suppress(lane: str, features: dict, ref_price: float | None) -> bool:
    """
    True HANYA kalau lane didukung, ref_price ada, fitur LENGKAP, DAN WR
    individual di level terdekat (+6%) < TP2_WR_FLOOR_PCT -- genuinely
    gagal ambang. False (JANGAN suppress) kalau data tidak lengkap/lane
    tak didukung -- caller fallback ke perilaku lama (tampil dgn teks WR
    grup statis), bukan hilang begitu saja.
    """
    if lane not in SUPPORTED_LANES or not ref_price:
        return False
    nearest = _predict_prob(lane, LEVELS[0], features)
    if nearest is None:  # fitur tak lengkap -- TIDAK BISA dihitung, beda dari "gagal ambang"
        return False
    return nearest * 100 < TP2_WR_FLOOR_PCT


def compute_tp1_tp2(lane: str, features: dict, ref_price: float) -> dict | None:
    """
    ref_price = closing HARI ticker qualify untuk lane ini (basis TP,
    konsisten dgn konvensi hit6/hit10 seluruh sesi backtest -- BUKAN harga
    live/hari ini).

    Return None kalau: lane tidak didukung, fitur tidak lengkap, ATAU
    level terdekat (+6%) individual WR < TP2_WR_FLOOR_PCT. PENTING: caller
    TIDAK BOLEH pakai `is None` di sini utk keputusan SUPPRESS (lihat
    should_suppress() di atas) -- fungsi ini murni utk PENAMPILAN, None di
    sini artinya "tidak ada angka utk ditampilkan", bukan otomatis "ticker
    ini harus disembunyikan".
    caller TIDAK BOLEH memproduksi sinyal sama sekali untuk kasus ini
    (bukan tampil dengan angka di bawah floor).
    """
    probs = compute_level_probabilities(lane, features)
    if not probs or probs.get(LEVELS[0]) is None:
        return None
    nearest_level = LEVELS[0]
    if probs[nearest_level] * 100 < TP2_WR_FLOOR_PCT:
        return None

    # Asumsi WR menurun monoton makin jauh levelnya (tervalidasi di backtest
    # grup) -- berhenti di kegagalan pertama, bukan cari level jauh yg
    # "kebetulan" masih lolos setelah ada yg gagal di tengah (hindari
    # sinyal melompati zona lemah).
    tp1_level = nearest_level
    for lvl in LEVELS:
        wr = probs.get(lvl)
        if wr is not None and wr * 100 >= TP1_WR_TARGET_PCT:
            tp1_level = lvl
        else:
            break

    tp2_level = nearest_level
    for lvl in LEVELS:
        wr = probs.get(lvl)
        if wr is not None and wr * 100 >= TP2_WR_FLOOR_PCT:
            tp2_level = lvl
        else:
            break

    result = {
        "lane": lane,
        "tp1_level": tp1_level, "tp1_wr": round(probs[tp1_level] * 100, 1),
        "tp1_price": ref_price * (1 + tp1_level / 100.0),
    }
    if tp2_level > tp1_level:
        result["tp2_level"] = tp2_level
        result["tp2_wr"] = round(probs[tp2_level] * 100, 1)
        result["tp2_price"] = ref_price * (1 + tp2_level / 100.0)
    return result


def format_tp_lines(tp_info: dict, current_price: float | None = None) -> list[str]:
    """
    ["TP1 424 (WR 75%)", "TP2 432 (WR 65%)"] -- atau "(Tercapai!)" kalau
    current_price (harga live intraday) sudah menyentuh level itu.
    current_price=None (konteks EOD/`/hc`/`/screendaytrade`, belum ada
    harga live hari berjalan) -> selalu tampilkan WR, tidak pernah "Tercapai!".
    """
    lines = []
    tp1_reached = current_price is not None and current_price >= tp_info["tp1_price"]
    tag1 = "Tercapai!" if tp1_reached else f"WR {tp_info['tp1_wr']:.0f}%"
    lines.append(f"TP1 {tp_info['tp1_price']:,.0f} ({tag1})")
    if "tp2_price" in tp_info:
        tp2_reached = current_price is not None and current_price >= tp_info["tp2_price"]
        tag2 = "Tercapai!" if tp2_reached else f"WR {tp_info['tp2_wr']:.0f}%"
        lines.append(f"TP2 {tp_info['tp2_price']:,.0f} ({tag2})")
    return lines
