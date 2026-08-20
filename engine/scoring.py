"""
engine/scoring.py -- Central Scoring Engine (MBSS v2 Sprint 2, Tier 1.1)

The core deterministic scoring/decision layer, moved out of legacy_core.py:
  - classify_action_priority() / classify_risk_character() -- lightweight
    labels derived from already-computed signals (no new calculation).
  - compute_high_conviction_score() -- 8-criteria Minervini/IBD-style
    breakout conviction check.
  - compute_brokersum_priority() -- ranks candidates for scarce Index Alpha
    quota.
  - apply_brokersum_adjustment() / _apply_brokersum_adjustment_original() --
    folds real broker-flow data into a scoring dict's sentiment/final score.
  - decide_action() -- the deterministic BUY/HOLD/AVOID tier logic (used by
    compute_factor_scoring AND re-invoked inside apply_brokersum_adjustment
    after scores shift, which is why it moves together with both).
  - compute_factor_scoring() -- the ~615-line core per-ticker scoring
    function. THE single most-called function in the whole codebase --
    every command that shows a stock's score ultimately calls this (via
    engine.nightly's fetch pipeline, or directly for on-demand checks).

Scope note -- what did NOT move here
-------------------------------------
Deliberately left in engine/legacy_core.py, reached via `core.xxx`:
  - Raw technical-indicator math (calculate_rsi/macd/cmf/obv/adx,
    detect_obv_divergence, detect_lower_highs, percentile_rank,
    score_from_percentile) -- calculate_rsi specifically is also used
    OUTSIDE this scoring cluster, so ALL of these stay together as a shared
    "technical indicators" utility layer, same treatment as get_yf_ticker/
    get_ohlcv_smart/yf_fetch_with_retry.
  - compute_daytrade_score() -- despite living textually right in the
    middle of this cluster in the original file, it's used broadly by
    /screendaytrade, the winrate-lock mechanism, and GPTPick -- not part of
    the /check-style core scoring, so it stays in legacy_core.py.
  - classify_lifecycle_category(), estimate_tp_horizon() -- portfolio
    lifecycle concerns, used directly by commands/portfolio.py, not part
    of the core per-ticker scoring path.
  - SCORING_FORMULA_VERSION, MIN_HISTORY_FOR_ADAPTIVE, MIN_STOCK_PRICE --
    constants read/compared from multiple other modules (e.g. nightly.py,
    /version), so they stay put; referenced here via core.xxx.
  - load_or_build_whitelist(), itick_get_kline_batch(), ZAPI_* -- unrelated
    infra that happened to sit textually between blocks in the original
    file (not a scoring concern at all).

Same circular-import rule as every other engine/commands module
------------------------------------------------------------------
`compute_factor_scoring()` needs core.get_ohlcv_smart/get_yf_ticker/
itick_get_quote/yf_fetch_with_retry/the technical-indicator functions, AND
`market_engine.get_ihsg_return_today()`. Meanwhile legacy_core.py,
engine/nightly.py, and commands/scan.py, commands/check.py,
commands/portfolio.py all need to call INTO this module. Two-way
dependency, same fix as always: MODULE imports on both sides
(`import engine.scoring as scoring_engine` / `import engine.legacy_core as
core`), never `from module import name` -- see engine/nightly.py's
docstring for the full explanation of why the named form breaks depending
on import order.
"""
from __future__ import annotations

import pandas as pd

from engine import legacy_core as core
import engine.market as market_engine
import engine.broker as broker_engine

# MBSS v2 (user request — log /eodscan terlalu berisik): PB/PE anomali dari
# yfinance (lihat PB_SANITY_MAX/PE_SANITY_MAX di compute_factor_scoring) itu
# masalah yfinance yang KRONIS untuk ticker YANG SAMA tiap malam (bukan
# temuan baru tiap kali) — sebelumnya di-print SATU BARIS PER TICKER,
# langsung saat itu juga, jadi log /eodscan penuh puluhan baris berulang
# tanpa info baru.
#
# Fix: BATCH MODE toggle, bukan sekadar buffer polos — kalau cuma buffer
# tanpa penanda ini, panggilan compute_factor_scoring() dari luar loop
# bulk (/check, /myportfolio, dll — SEMUANYA lewat fungsi yang sama persis)
# akan diam-diam MENUMPUK ke buffer tanpa PERNAH di-flush, jadi warning-nya
# hilang sama sekali untuk pemakaian on-demand, bukan cuma jadi ringkas.
# Default batch mode = False (OFF) — pemakaian on-demand TETAP print
# langsung persis seperti sebelumnya, TIDAK ADA perubahan perilaku di sana.
# HANYA loop bulk (engine/nightly.py fetch_all_tickers_scored, yang
# menaungi baik /eodscan maupun fallback fetch /testbrief) yang menyalakan
# batch mode di sekitar loop-nya lalu flush di akhir.
_data_quality_warnings: list = []
_batch_mode_active = False


def set_scoring_batch_mode(active: bool):
    """Toggle SEKALI oleh caller bulk (fetch_all_tickers_scored) — lihat
    catatan di atas kenapa ini tidak boleh jadi default global."""
    global _batch_mode_active
    _batch_mode_active = active


def _report_data_quality_warning(short_note: str, full_message: str):
    """
    short_note dipakai kalau nanti diringkas (batch mode ON), full_message
    dipakai kalau print langsung (batch mode OFF, default — sama seperti
    perilaku sebelum perubahan ini).
    """
    if _batch_mode_active:
        _data_quality_warnings.append(short_note)
    else:
        print(full_message)


def flush_data_quality_warnings() -> int:
    """
    Cetak SATU baris ringkasan untuk semua anomali PB/PE yang terkumpul
    sepanjang batch ini, lalu kosongkan buffer-nya. Return jumlahnya (0
    kalau tidak ada). Dipanggil dari fetch_all_tickers_scored SETELAH
    set_scoring_batch_mode(False) — kalau batch mode masih ON belum
    dipanggil di sini, buffer akan menumpuk lintas-scan.
    """
    global _data_quality_warnings
    count = len(_data_quality_warnings)
    if count:
        shown = ", ".join(_data_quality_warnings[:30])
        more = f" ...dan {count - 30} lainnya" if count > 30 else ""
        print(f"⚠️ {count} ticker dengan data PB/PE tidak wajar dari yfinance (diperlakukan sebagai tidak diketahui, bukan sinyal distress): {shown}{more}")
    _data_quality_warnings = []
    return count

# MBSS v2 (user request — ditemukan lewat kasus nyata DOSS/FWCT/BAIK/PMUI
# "tidak pernah berhasil di-check" padahal user cek manual datanya ADA di
# Yahoo): compute_factor_scoring() punya 7 titik `return None` berbeda,
# tapi pemanggil (terutama /check) selama ini SELALU menampilkan pesan
# generik yang sama (menyalahkan iTick — sudah usang, sebagian besar jalur
# fetch sekarang lewat Yahoo/SQLite bukan iTick) untuk SEMUA kasus, bahkan
# untuk 2 titik yang sebelumnya tidak nge-print apa pun sama sekali ke log.
# Dict ini dicatat SEKALI per pemanggilan (dioverwrite tiap panggilan baru
# untuk ticker yang sama), supaya pemanggil bisa tanya "kenapa tadi
# excluded?" segera setelah compute_factor_scoring() return None/falsy.
_LAST_EXCLUSION_REASON: dict[str, str] = {}


def _excluded(ticker: str, reason: str):
    """Catat alasan exclusion lalu return None — dipakai di semua early-exit compute_factor_scoring()."""
    _LAST_EXCLUSION_REASON[ticker] = reason
    print(f"⚠️ {ticker}: excluded — {reason}")
    return None


def get_last_exclusion_reason(ticker: str) -> str | None:
    """Alasan exclusion TERAKHIR untuk ticker ini, atau None kalau belum pernah/tidak diketahui."""
    return _LAST_EXCLUSION_REASON.get(ticker)


def classify_action_priority(scoring: dict, lifecycle_category: str = None) -> dict:
    """
    Label sederhana untuk scan cepat pagi hari — TUJUANNYA supaya user bisa
    lihat label ini SAJA tanpa baca seluruh analisis, langsung tahu posisi mana
    yang perlu diperhatikan lebih dulu. Murni PEMETAAN deterministik dari sinyal
    yang SUDAH dihitung (lifecycle, action decision, jarak ke TP/CL) — tidak ada
    perhitungan baru, tidak ada keputusan tambahan, cuma menyederhanakan yang
    sudah ada jadi satu label yang gampang di-scan.

    Prioritas (dicek berurutan, yang pertama cocok yang dipakai):
    1. Target sudah tercapai → Take Profit Candidate (paling actionable, uang
       di atas meja, jangan sampai terlewat)
    2. Momentum jelas lemah/rekomendasi hindari → Exit Candidate
    3. Sinyal ambigu, kategori hati-hati, atau sangat dekat level kritis (TP/CL
       dalam jarak <3%) → Watch Closely
    4. Selain itu → Hold (kondisi stabil, tidak ada urgensi)
    """
    price = scoring.get("price", 0)
    targets = scoring.get("targets", {})
    tp1 = targets.get("tp_1")
    cut_loss = targets.get("cut_loss")
    action_id = scoring.get("action_id")

    if lifecycle_category == "TARGET_TERCAPAI":
        return {"priority": "TAKE_PROFIT_CANDIDATE", "reason": "harga sudah capai/lewati TP1"}

    if lifecycle_category == "EVALUASI" or action_id == "AVOID_SELL":
        return {"priority": "EXIT_CANDIDATE", "reason": "momentum lemah, belum capai target, atau rekomendasi hindari/jual"}

    dist_to_cutloss_pct = None
    dist_to_tp_pct = None
    if price and tp1 and cut_loss:
        dist_to_cutloss_pct = abs((price - cut_loss) / price * 100)
        dist_to_tp_pct = abs((tp1 - price) / price * 100)
    is_near_critical_level = (
        (dist_to_cutloss_pct is not None and dist_to_cutloss_pct < 3)
        or (dist_to_tp_pct is not None and dist_to_tp_pct < 3)
    )

    if action_id == "MIXED_SIGNALS" or lifecycle_category == "HATI_HATI" or is_near_critical_level:
        reasons = []
        if action_id == "MIXED_SIGNALS":
            reasons.append("sinyal campuran")
        if lifecycle_category == "HATI_HATI":
            reasons.append("kategori hati-hati")
        if is_near_critical_level:
            reasons.append("mendekati level TP/CL (<3%)")
        return {"priority": "WATCH_CLOSELY", "reason": ", ".join(reasons)}

    return {"priority": "HOLD", "reason": "kondisi stabil, tidak ada urgensi khusus saat ini"}


def classify_risk_character(scoring: dict) -> dict:
    """
    Klasifikasi Base/Defensif vs Swing/Agresif — dimensi BERBEDA dari lifecycle
    (Baru/Produktif/dst). Lifecycle soal "sudah berapa lama & masih on track",
    ini soal "apa peran/karakter saham ini". Keduanya bisa berdampingan.

    BASE/DEFENSIF: value_score tinggi (fundamental kuat), volatilitas rendah
    (day_range_pct_10d kecil), tidak ada flag risiko aktif — cocok jadi
    penyeimbang portofolio, bukan sumber cuan cepat.

    SWING/AGRESIF: volatilitas tinggi dan/atau momentum kuat, MUNGKIN ada flag
    risiko tapi didukung sinyal lain (broker riil, MACD, dll) — selalu disertai
    saran manajemen risiko eksplisit karena karakternya memang lebih spekulatif.
    """
    value_score = scoring.get("scores", {}).get("value", 0)
    momentum_score = scoring.get("scores", {}).get("momentum", 0)
    day_range = scoring.get("day_range_pct_10d", 0) or 0
    has_risk_flag = (
        scoring.get("is_overbought_caution", False)
        or scoring.get("is_volume_spike_anomaly", False)
        or scoring.get("chart_pattern") == "lower_highs_bearish"
        or scoring.get("obv_divergence") == "bearish_divergence"
    )

    if value_score >= 6.5 and day_range < 8.0 and not has_risk_flag:
        return {
            "character": "BASE_DEFENSIF",
            "reason": "fundamental kuat, volatilitas rendah, tidak ada flag risiko aktif — cocok jadi penyeimbang portofolio",
        }

    if day_range >= 8.0 or momentum_score >= 7.0:
        risk_note = "ada flag risiko aktif, perlu manajemen risiko lebih ketat" if has_risk_flag else "volatil tapi tanpa flag risiko aktif saat ini"
        return {
            "character": "SWING_AGRESIF",
            "reason": f"volatilitas/momentum tinggi ({risk_note})",
        }

    return {
        "character": "NETRAL",
        "reason": "tidak cukup defensif untuk Base, tidak cukup volatil/bermomentum untuk Swing Agresif",
    }




def compute_bull_flag_pullback_signal(hist_daily: pd.DataFrame, lookback: int = 15, min_flag_bars: int = 2, max_retracement_pct: float = 50.0) -> dict:
    """
    PROTOTYPE (MBSS v2, user request — riset "lower high" sebagai kandidat
    gate HC, lihat diskusi /finance): deteksi pola bull flag dari daily
    bars — pole (gerak naik kuat) diikuti flag (pullback terkontrol dengan
    LOWER HIGH yang drift turun ringan + volume mengecil), lalu trigger =
    harga hari ini reclaim HIGH FLAG-nya sendiri (bukan high pole).

    PENTING, dua makna "lower high" yang beda arah (lihat riset): lower
    high di PULLBACK PENDEK dalam uptrend besar (yang dideteksi di sini)
    = pola KONTINUASI bullish (bull flag). Lower high di SWING UTAMA
    (jangka lebih panjang) = justru sinyal AWAL PELEMAHAN/reversal —
    fungsi ini SENGAJA cuma menangani makna pertama (flag pendek), bukan
    dipakai untuk baca swing utama.

    BELUM di-wire ke HC gate/skor apa pun — murni informational/prototype
    dulu, sesuai disiplin tag-and-track (sama seperti tight_trailing_
    support/fast_candidate) — validasi forward dulu sebelum dipertimbangkan
    masuk Stage 2 funnel HC yang diusulkan.

    Deteksi SEDERHANA, bukan swing-detection penuh:
    1. Pole: dari LOW terendah di window ke swing HIGH (max High) di window.
    2. Flag: segmen SETELAH swing high sampai KEMARIN (hari ini dikecualikan
       — itu kandidat breakout day, bukan bagian pola).
    3. Flag valid kalau: highs drift TURUN (avg paruh kedua < paruh
       pertama), volume flag < volume pole (kontraksi >=15%), retracement
       (swing_high - flag_low)/(swing_high - pole_low) <= max_retracement_pct.
    4. Trigger: close HARI INI > flag_high (reclaim, bukan pole_high).
    """
    if hist_daily is None or len(hist_daily) < lookback + 1:
        return {"available": False}

    window = hist_daily.tail(lookback + 1)
    today = window.iloc[-1]
    history = window.iloc[:-1]  # semua SEBELUM hari ini (kandidat breakout)

    pole_low = float(history["Low"].min())
    swing_high_pos = int(history["High"].values.argmax())
    swing_high = float(history["High"].iloc[swing_high_pos])

    flag = history.iloc[swing_high_pos + 1:]
    if len(flag) < min_flag_bars:
        return {"available": False, "reason": f"flag terlalu pendek (<{min_flag_bars} bar sejak swing high)"}

    pole_range = swing_high - pole_low
    if pole_range <= 0:
        return {"available": False, "reason": "pole range invalid"}

    flag_low = float(flag["Low"].min())
    retracement_pct = (swing_high - flag_low) / pole_range * 100

    # Drift highs selama flag — bandingkan rata-rata paruh pertama vs kedua
    # (proxy sederhana, bukan regresi penuh — cukup untuk prototype).
    mid = max(1, len(flag) // 2)
    highs_first_half = flag["High"].iloc[:mid].mean()
    highs_second_half = flag["High"].iloc[mid:].mean() if len(flag) > mid else flag["High"].iloc[-1]
    drifting_down = highs_second_half < highs_first_half

    pole_bars = history.iloc[:swing_high_pos + 1]
    pole_avg_vol = float(pole_bars["Volume"].mean()) if not pole_bars.empty else 0.0
    flag_avg_vol = float(flag["Volume"].mean())
    volume_contracted = pole_avg_vol > 0 and flag_avg_vol < pole_avg_vol * 0.85

    flag_high = float(flag["High"].max())
    today_close = float(today["Close"])
    reclaimed = today_close > flag_high

    valid_pullback = drifting_down and volume_contracted and retracement_pct <= max_retracement_pct
    is_bull_flag_breakout = bool(valid_pullback and reclaimed)

    return {
        "available": True,
        "is_bull_flag_breakout": is_bull_flag_breakout,
        "drifting_down": bool(drifting_down),
        "volume_contracted": bool(volume_contracted),
        "retracement_pct": round(retracement_pct, 1),
        "flag_bars": len(flag),
        "flag_high": round(flag_high, 2),
        "reclaimed_flag_high": bool(reclaimed),
        "pole_range_pct": round(pole_range / pole_low * 100, 1) if pole_low else None,
    }


def compute_high_conviction_score(ticker: str, scoring: dict, hist_daily: pd.DataFrame = None, action_id: str = None) -> dict:
    """
    8 kriteria High Conviction Breakout dari framework Minervini/IBD style
    (sumber: video yang ditinjau user), diadaptasi untuk IDX EOD + 4H Yahoo Finance.

    Kriteria yang diadaptasi (bukan dipakai verbatim karena konteks berbeda):
    - Candle body >5% (4H): pakai 4H native Yahoo Finance (terkonfirmasi selaras
      sesi IDX — 09:00 Sesi 1, 13:00 Sesi 2, tanpa perlu agregasi manual)
    - Market cap: digantikan core.MIN_STOCK_PRICE + volume filter (sudah di whitelist)
    - Timeframe: video pakai 4H untuk entry, kita adaptasi ke kombinasi EOD + 4H

    Return dict dengan setiap kriteria (True/False/None), jumlah yang terpenuhi,
    dan flag is_high_conviction (>=5 dari 7 kriteria yang bisa dicek).

    BUGFIX (user report, revisit dari kasus RAJA di Explosive/FAST — HC punya
    celah struktural yang sama: kriteria 1-8 di sini MURNI teknikal, tidak
    pernah cek action_id/blend Value-Momentum-Sentimen sama sekali, jadi bisa
    badge "HIGH CONVICTION" untuk ticker yang core blend-nya SUDAH bilang
    HINDARI/JUAL). `action_id` opsional (caller kirim dari decide_action() yang
    SUDAH dihitung lebih dulu di compute_factor_scoring) — kalau
    "AVOID_SELL", is_high_conviction dipaksa False terlepas dari berapa
    kriteria teknikal yang terpenuhi. TIDAK mengubah criteria_met/checkable
    individual (tetap apa adanya, transparan), cuma flag akhirnya.
    """
    result = {
        "consolidation_tight": None,      # Kriteria 1: konsolidasi <12%
        "breakout_close_confirmed": None, # Kriteria 2: close >2% di atas resistance
        "candle_body_strong_4h": None,    # Kriteria 3: body candle 4H >5%
        "relative_volume_ok": None,       # Kriteria 5: vol ratio >=1.5x
        "avg_volume_ok": None,            # Kriteria 6: avg vol harian memadai
        "near_high": None,                # Kriteria 7: dalam 10% dari high 20/50hr
        "above_ma20_and_ma50": None,      # Kriteria 8: di atas EMA21 dan SMA50
        "criteria_met": 0,
        "criteria_checkable": 0,
        "is_high_conviction": False,
        "summary": [],
    }

    price = scoring.get("price")
    if not price:
        return result

    # MBSS v2 (user request, real case: MSIN/PPRE lolos HC walau volume
    # kering -- kriteria 5/6 di bawah cuma "vote" di antara 7-8 kriteria
    # lain, gampang diabaikan kalau kriteria lain kompak lolos duluan).
    # Filter ABSOLUT di AWAL sekarang, bukan lagi cuma salah satu vote:
    # value_traded (RUPIAH, BUKAN share count seperti kriteria 6 di bawah
    # yang pakai 300rb LEMBAR -- angka lembar tidak seragam lintas tier
    # harga, saham Rp100 vs Rp5000 sama-sama "300rb lembar" tapi nilai
    # transaksinya beda 50x). Value_traded < floor = TIDAK lolos HC sama
    # sekali, terlepas berapa kriteria lain yang lolos. Floor 3B REUSE
    # dari compute_activity_score_v5's floor sendiri (engine/legacy_
    # core.py) -- angka yang SUDAH established di codebase (dipakai juga
    # sebagai batas bawah Activity score), bukan threshold baru yang
    # dikarang tanpa dasar. Missing data (None) TIDAK menggagalkan --
    # "missing = neutral", cuma value yang EKSPLISIT rendah yang di-reject.
    HC_MIN_VALUE_TRADED_IDR = core.LIQUIDITY_FLOOR_VALUE_TRADED_IDR  # MBSS v2: satu shared constant (engine/legacy_core.py), sekarang juga dipakai Backbone gate -- lihat BACKBONE_FORMULA_VERSION 1.9
    value_traded = scoring.get("value_traded")
    if value_traded is not None and value_traded < HC_MIN_VALUE_TRADED_IDR:
        result["summary"].append(
            f"❌ Value traded Rp{value_traded/1e9:.1f}M di bawah floor likuiditas Rp{HC_MIN_VALUE_TRADED_IDR/1e9:.0f}M "
            f"— TIDAK lolos HC sama sekali, terlepas kriteria teknikal lain"
        )
        return result

    # --- Kriteria 1: Konsolidasi ketat <=20%, jendela 5 hari (direvisi dari
    # <12%/10 hari, user request — tujuan prediksi breakout 1-2 hari ke depan,
    # jendela lebih pendek lebih relevan daripada rata-rata 10 hari yang bisa
    # "diencerkan" pergerakan lebih lama yang sudah tidak relevan lagi) ---
    if hist_daily is not None and not hist_daily.empty and len(hist_daily) >= 5:
        result["criteria_checkable"] += 1
        recent5 = hist_daily.tail(5)
        high5 = recent5["High"].max()
        low5 = recent5["Low"].min()
        day_range_5d = (high5 - low5) / max(price, 1e-9) * 100 if price else None
        tight = day_range_5d is not None and day_range_5d <= 20.0
        result["consolidation_tight"] = tight
        if tight:
            result["criteria_met"] += 1
            result["summary"].append(f"✅ Konsolidasi ketat: rentang 5hr {day_range_5d:.1f}% (<=20%)")
        else:
            result["summary"].append(f"❌ Konsolidasi terlalu lebar: {day_range_5d:.1f}% (harus <=20%)" if day_range_5d is not None else "❌ Konsolidasi tidak bisa dihitung")

    # --- Kriteria 2: Konfirmasi penutupan breakout >2% di atas resistance ---
    # Dari video: "breakout candle must close at least 2% above the upper boundary"
    # Adaptasi: bandingkan close (harga saat ini) vs resistance_10d
    resistance = None
    if hist_daily is not None and not hist_daily.empty and len(hist_daily) >= 10:
        resistance = hist_daily["High"].tail(10).max()
    if resistance and price:
        result["criteria_checkable"] += 1
        pct_above = (price - resistance) / resistance * 100
        confirmed = pct_above >= 2.0
        result["breakout_close_confirmed"] = confirmed
        if confirmed:
            result["criteria_met"] += 1
            result["summary"].append(f"✅ Breakout terkonfirmasi: {pct_above:+.1f}% di atas resistance {resistance:.0f}")
        else:
            result["summary"].append(f"❌ Belum breakout: {pct_above:+.1f}% vs resistance {resistance:.0f} (butuh >+2%)")

    # --- Kriteria 3: Body candle 4H >=5%, ATAU body sedang (>=2%) + closing
    # di 30% teratas rentang candle (user request — body murni bisa gagal
    # mendeteksi "buyer menang setelah ditekan seller": harga naik, ditahan,
    # turun, naik lagi dalam candle yang sama menghasilkan wick panjang tapi
    # body kecil, padahal itu justru buyer sedang menyerap tekanan jual —
    # sinyal positif yang terlewat kalau cuma andalkan body murni) — DAN
    # (user request lanjutan, diadopsi dari riset formula BSJP komunitas):
    # candle HARI INI SATU HARI PENUH juga harus positif (harga sekarang >=
    # open hari ini) — kekuatan 4H doang bisa menipu kalau ternyata hari itu
    # OVERALL masih merah (mis. bar 4H terakhir rebound kuat, tapi dari
    # posisi yang jauh di bawah open pagi ini) ---
    try:
        hist_4h = core.get_ohlcv_4h(ticker, period="5d")
        today_open = hist_daily["Open"].iloc[-1] if hist_daily is not None and not hist_daily.empty else None
        daily_candle_positive = today_open is not None and price >= today_open

        if not hist_4h.empty and len(hist_4h) >= 2:
            result["criteria_checkable"] += 1
            last_bar = hist_4h.iloc[-1]
            body_pct = abs(last_bar["Close"] - last_bar["Open"]) / last_bar["Open"] * 100
            bar_range = last_bar["High"] - last_bar["Low"]
            close_pos_bar = (last_bar["Close"] - last_bar["Low"]) / max(bar_range, 1e-9)
            path_a = body_pct >= 5.0  # gerak bersih kuat
            path_b = body_pct >= 2.0 and close_pos_bar >= 0.7  # ditekan tapi buyer menang di closing
            strong_4h = path_a or path_b
            strong = strong_4h and daily_candle_positive
            result["candle_body_strong_4h"] = strong
            daily_note = "✅ candle hari ini positif" if daily_candle_positive else "❌ candle hari ini masih merah (harga < open)"
            if path_a and daily_candle_positive:
                result["criteria_met"] += 1
                result["summary"].append(f"✅ Body candle 4H kuat: {body_pct:.1f}% (>=5%), {daily_note}")
            elif path_b and daily_candle_positive:
                result["criteria_met"] += 1
                result["summary"].append(
                    f"✅ Body candle 4H sedang ({body_pct:.1f}%) tapi closing kuat "
                    f"({close_pos_bar*100:.0f}% dari rentang candle) — buyer menang meski sempat ditekan, {daily_note}"
                )
            elif strong_4h and not daily_candle_positive:
                result["summary"].append(
                    f"❌ Body 4H kuat ({body_pct:.1f}%), TAPI {daily_note} — kekuatan 4H terakhir "
                    f"belum cukup mengangkat hari ini jadi positif secara keseluruhan"
                )
            else:
                result["summary"].append(
                    f"❌ Body candle 4H lemah: {body_pct:.1f}% (butuh >=5%, atau >=2% dengan closing "
                    f"di 30% teratas rentang candle — closing sekarang {close_pos_bar*100:.0f}%), {daily_note}"
                )
    except Exception:
        result["summary"].append("⚠️ Data 4H tidak tersedia")

    # --- Kriteria 5: Volume relatif >=2x (direvisi dari 1.5x, user request) ---
    vol_ratio = scoring.get("vol_ratio")
    if vol_ratio is not None:
        result["criteria_checkable"] += 1
        ok = vol_ratio >= 2.0
        result["relative_volume_ok"] = ok
        if ok:
            result["criteria_met"] += 1
            result["summary"].append(f"✅ Volume relatif tinggi: {vol_ratio}x (>=2x)")
        else:
            result["summary"].append(f"❌ Volume relatif rendah: {vol_ratio}x (butuh >=2x)")

    # --- Kriteria 6: Avg volume 5 hari >=300rb, ATAU volume MAKSIMUM dalam
    # 1-3 hari terakhir >=300rb (user request — kasus nyata JKON/JGLE: saham
    # diam berminggu-minggu lalu 1 hari meledak, RATA-RATA 5 hari "diencerkan"
    # 4 hari sepi sebelumnya sehingga gagal ambang walau hari ledakannya
    # sendiri jauh di atasnya. Jalur B khusus tangkap lonjakan segar, tanpa
    # perlu histori likuiditas konsisten sebelumnya. Ambang jalur B SENGAJA
    # disamakan dengan jalur A [300rb, bukan 500rb] — percobaan pertama pakai
    # 500rb ternyata GAGAL menangkap JKON sendiri, kasus yang jadi alasan
    # fitur ini dibuat, karena lonjakan volumenya cuma sekitar 450rb.) ---
    if hist_daily is not None and not hist_daily.empty and len(hist_daily) >= 5:
        result["criteria_checkable"] += 1
        avg_vol = hist_daily["Volume"].tail(5).mean()
        max_vol_3d = hist_daily["Volume"].tail(3).max()
        path_a = avg_vol >= 300_000
        path_b = max_vol_3d >= 300_000
        ok = path_a or path_b
        result["avg_volume_ok"] = ok
        if path_a:
            result["summary"].append(f"✅ Avg volume 5hr memadai: {int(avg_vol):,}/hari")
            result["criteria_met"] += 1
        elif path_b:
            result["summary"].append(
                f"✅ Lonjakan volume segar: {int(max_vol_3d):,} dalam 3 hari terakhir "
                f"(avg 5hr cuma {int(avg_vol):,}, tapi ada hari meledak)"
            )
            result["criteria_met"] += 1
        else:
            result["summary"].append(
                f"❌ Volume kurang: avg 5hr {int(avg_vol):,} DAN max 3hr {int(max_vol_3d):,} (keduanya butuh >=300rb)"
            )

    # --- Kriteria 7: Harga dalam <5% dari high 10 hari (direvisi dari <=10%
    # dari high 20/50 hari, user request — jendela dipersempit ke 10 hari
    # saja dan ambang diperketat, konsisten dengan tujuan prediksi 1-2 hari
    # ke depan: high 50 hari terlalu jauh ke belakang untuk relevan) ---
    if hist_daily is not None and not hist_daily.empty and len(hist_daily) >= 10:
        result["criteria_checkable"] += 1
        high_10d = hist_daily["High"].tail(10).max()
        pct_from_10 = (high_10d - price) / max(high_10d, 1e-9) * 100
        near = pct_from_10 < 5.0
        result["near_high"] = near
        if near:
            result["criteria_met"] += 1
            result["summary"].append(f"✅ Dekat high 10hr: {pct_from_10:.1f}% di bawah (high10={high_10d:.0f})")
        else:
            result["summary"].append(f"❌ Terlalu jauh dari high 10hr: {pct_from_10:.1f}% (harus <5%)")

    # --- Kriteria 8: Di atas EMA9 dan SMA20 (direvisi dari EMA21/SMA50, user
    # request — MA lebih pendek lebih responsif untuk horizon prediksi 1-2
    # hari; EMA21/SMA50 cenderung "telat" bereaksi untuk tujuan ini). Dihitung
    # LANGSUNG dari hist_daily di sini (bukan pakai is_below_ema21/is_below_sma50
    # yang dikirim dari compute_factor_scoring — itu tetap EMA21/SMA50 untuk
    # keperluan lain di luar fungsi ini, sengaja tidak diubah biar tidak
    # mempengaruhi bagian lain dari sistem).
    if hist_daily is not None and not hist_daily.empty and len(hist_daily) >= 20:
        result["criteria_checkable"] += 1
        ema9 = hist_daily["Close"].ewm(span=9, adjust=False).mean().iloc[-1]
        sma20 = hist_daily["Close"].tail(20).mean()
        above_ema9 = price > ema9
        above_sma20 = price > sma20
        both_above = above_ema9 and above_sma20
        result["above_ma20_and_ma50"] = both_above  # nama field dipertahankan biar kompatibel, isinya sekarang EMA9/SMA20
        if both_above:
            result["criteria_met"] += 1
            result["summary"].append(f"✅ Di atas EMA9 ({ema9:.0f}) dan SMA20 ({sma20:.0f})")
        elif above_ema9:
            result["summary"].append(f"⚠️ Di atas EMA9 ({ema9:.0f}), tapi di bawah SMA20 ({sma20:.0f})")
        elif above_sma20:
            result["summary"].append(f"⚠️ Di atas SMA20 ({sma20:.0f}), tapi di bawah EMA9 ({ema9:.0f})")
        else:
            result["summary"].append(f"❌ Di bawah EMA9 ({ema9:.0f}) dan SMA20 ({sma20:.0f})")

    # High conviction kalau >= 5 dari kriteria yang bisa dicek terpenuhi
    checkable = result["criteria_checkable"]
    met = result["criteria_met"]
    threshold = max(5, round(checkable * 0.7))  # 70% dari yang bisa dicek, min 5
    technically_qualified = met >= threshold
    result["is_high_conviction"] = technically_qualified and action_id != "AVOID_SELL"
    if technically_qualified and action_id == "AVOID_SELL":
        result["conviction_label"] = f"⚪ Low conviction ({met}/{checkable} kriteria teknikal, TAPI action_id AVOID_SELL)"
    else:
        result["conviction_label"] = (
            "🔥 HIGH CONVICTION" if result["is_high_conviction"]
            else f"⚪ Low conviction ({met}/{checkable} kriteria)"
        )
    return result


# MBSS v2 (user request — "HC dan SDT justru butuh regime aware"): HC's
# checkable-criteria fraction (0.70, dipakai di atas) TIDAK bisa dihitung
# regime-aware DI DALAM compute_high_conviction_score itu sendiri --
# classify_market_regime baru bisa dihitung SETELAH seluruh universe
# selesai di-score malam ini (breadth-nya butuh action_id semua ticker,
# lihat engine/nightly.py run_nightly_full_scan urutan compute_backbone
# SETELAH fetch_tickers_scored_with_cache). Jadi regime-awareness HC
# diterapkan sebagai RECHECK di sini, dibaca ulang dari criteria_met/
# criteria_checkable yang SUDAH tersimpan di r["high_conviction"] (tanpa
# perlu hist_daily lagi, murah) begitu market_regime sudah diketahui --
# dipanggil di command layer (commands/scan.py) yang sudah punya
# backbone_result["market_regime"], BUKAN mengganti r["high_conviction"]
# ["is_high_conviction"] yang sudah frozen di nightly cache. Filosofi sama
# dengan DANGER_GATE_QUANTILE_BY_REGIME/SDT_LANE_TIGHTEN_BY_REGIME -- hanya
# R1 tervalidasi forward, regime lain sengaja lebih ketat, placeholder.
HC_MET_FRACTION_BY_REGIME = {
    "R1_BULL_STABLE": 0.70,
    "R2_BULL_HIGH_VOL": 0.75,
    "R3_SIDEWAYS": 0.75,
    "R4_RISK_OFF": 0.80,
    "R5_STRESS": 0.85,
    "R0_UNKNOWN": 0.80,
}


def is_high_conviction_regime_aware(r: dict, market_regime: str | None) -> bool:
    """Regime-scaled recheck of an already-computed r["high_conviction"] verdict — see HC_MET_FRACTION_BY_REGIME above for why this can't live inside compute_high_conviction_score itself."""
    hc = r.get("high_conviction") or {}
    checkable = hc.get("criteria_checkable", 0) or 0
    met = hc.get("criteria_met", 0) or 0
    if checkable <= 0:
        return bool(hc.get("is_high_conviction", False))
    fraction = HC_MET_FRACTION_BY_REGIME.get(market_regime, HC_MET_FRACTION_BY_REGIME["R0_UNKNOWN"]) if market_regime else HC_MET_FRACTION_BY_REGIME["R1_BULL_STABLE"]
    threshold = max(5, round(checkable * fraction))
    return met >= threshold and r.get("action_id") != "AVOID_SELL"


def compute_brokersum_priority(scoring: dict, total_stock_value: float = 0) -> float:
    """
    Ranks a stock's priority for spending scarce daily Index Alpha budget (5
    calls/day, 1 call/ticker = 5 tickers/day max). Combines:
    - urgency: how close price is to cut_loss or take_profit (closer = more
      time-sensitive, a wrong call here costs money NOW)
    - volatility: day_range_pct_10d — is this stock actively "in play"
    - ambiguity: MIXED_SIGNALS / active OBV divergence / overbought caution —
      cases where our own proxy is ALREADY uncertain, so real data resolves the
      most (a quiet stock with OBV divergence can be MORE worth checking than an
      obviously trending one — see the ANTM case: flat net masked real activity)
    - size: bigger positions deserve scrutiny regardless of how exciting the
      technicals look, since more capital is on the line if the call is wrong

    Watchlist entries (no position yet) get a flat modest size baseline.
    """
    price = scoring.get("price", 0)
    targets = scoring.get("targets", {})
    tp1 = targets.get("tp_1")
    cut_loss = targets.get("cut_loss")

    if price and tp1 and cut_loss:
        dist_to_cutloss_pct = abs((price - cut_loss) / price * 100)
        dist_to_tp_pct = abs((tp1 - price) / price * 100)
        closest_dist_pct = min(dist_to_cutloss_pct, dist_to_tp_pct)
        urgency = max(0.0, 10.0 - closest_dist_pct)
    else:
        urgency = 0.0

    volatility = min(10.0, scoring.get("day_range_pct_10d", 0) or 0)

    ambiguity = 0.0
    if scoring.get("action_id") == "MIXED_SIGNALS":
        ambiguity += 5.0
    if scoring.get("obv_divergence") in ("bearish_divergence", "bullish_divergence"):
        ambiguity += 3.0
    if scoring.get("is_overbought_caution"):
        ambiguity += 2.0
    ambiguity = min(10.0, ambiguity)

    market_value = scoring.get("market_value_idr")
    if market_value and total_stock_value > 0:
        size = min(10.0, (market_value / total_stock_value) * 10)
    else:
        size = 3.0  # flat baseline for watchlist (not owned yet, no position size)

    priority = urgency * 0.35 + volatility * 0.25 + ambiguity * 0.30 + size * 0.10
    return round(priority, 2)


def _apply_brokersum_adjustment_original(scoring: dict, brokersum: dict) -> dict:
    """
    Lets REAL broker flow data actually influence the score and decision, not
    just sit alongside it as narrative context. Only adjusts when
    broker_concentration_pct >= 10% — below that, the signal is too diffuse/
    noisy to trust (mirrors the ANTM lesson: concentration is what makes this
    data meaningful, not raw net flow alone).

    Adjustment is capped at ±3 points on sentiment_score, scaled by how
    concentrated the flow is (full weight at 40%+ concentration). At
    sentiment's 0.3 weight on final_score, that bounds the total swing to
    about ±0.9 on final_score — real influence, not something that alone
    flips a HOLD into a STRONG BUY by itself.

    Re-runs decide_action() with the adjusted scores so the actual decision
    (not just the displayed number) reflects real data when available.
    """
    concentration_pct = brokersum.get("broker_concentration_pct", 0)
    if concentration_pct < 10:
        scoring["brokersum_adjusted"] = False
        return scoring

    net_foreign_flow_pct = brokersum.get("net_foreign_flow_pct", 0)
    concentration_factor = min(1.0, concentration_pct / 40)
    adjustment = (net_foreign_flow_pct / 100) * 3.0 * concentration_factor

    old_sentiment = scoring["scores"]["sentiment"]
    new_sentiment = max(1.0, min(10.0, old_sentiment + adjustment))

    value_score = scoring["scores"]["value"]
    momentum_score = scoring["scores"]["momentum"]
    new_final = (value_score * 0.25) + (momentum_score * 0.45) + (new_sentiment * 0.30)

    decision = decide_action(
        final_score=new_final, value_score=value_score, momentum_score=momentum_score,
        sentiment_score=new_sentiment, is_financial_distress_flag=scoring.get("is_financial_distress_flag", False),
        chart_pattern=scoring.get("chart_pattern", "none"), is_overbought_caution=scoring.get("is_overbought_caution", False),
        obv_divergence=scoring.get("obv_divergence", "none"), is_volume_spike_anomaly=scoring.get("is_volume_spike_anomaly", False),
        is_near_price_floor=scoring.get("is_near_price_floor", False), is_unusually_low_pe=scoring.get("is_unusually_low_pe", False),
        macd_bearish_cross=scoring.get("macd_bearish_cross", False), is_below_sma50=scoring.get("is_below_sma50", False),
    )

    scoring["scores"]["sentiment"] = round(new_sentiment, 1)
    scoring["scores"]["final"] = round(new_final, 1)
    scoring["action_id"] = decision["action_id"]
    scoring["action_label_id"] = decision["action_label_id"]
    scoring["action_ceiling_applied"] = decision["ceiling_applied"]
    scoring["action_component_spread"] = decision["component_spread"]
    scoring["brokersum_adjusted"] = True
    scoring["brokersum_adjustment_applied"] = round(adjustment, 2)
    return scoring




def apply_brokersum_adjustment(scoring: dict, brokersum: dict) -> dict:
    """
    Wrapper broker-flow adjustment.

    Untuk source lama / Index Alpha / Zapi foreign flow:
    tetap pakai logic original.

    Untuk screenshot Broker Summary ALL 3D:
    gunakan smart_money_confirmation_score sebagai confirmation layer,
    bukan net foreign flow aggregate, karena ALL tab aggregate net normalnya 0.
    """
    if not brokersum:
        return scoring

    flow_scope = brokersum.get("flow_scope")

    if flow_scope != "ALL_BROKERS_3D":
        return _apply_brokersum_adjustment_original(scoring, brokersum)

    adjusted = dict(scoring)

    try:
        sm_score = int(float(brokersum.get("smart_money_confirmation_score", 0) or 0))
    except Exception:
        sm_score = 0

    sm_score = max(-15, min(15, sm_score))
    flow_label = brokersum.get("smart_money_flow_label", "NEUTRAL")
    reasons = brokersum.get("smart_money_reasons") or []

    old_sentiment = float(adjusted.get("sentiment_score", 0) or 0)
    old_final = float(adjusted.get("final_score", 0) or 0)

    # Konversi score -15..+15 ke adjustment konservatif.
    # Sentiment bergerak lebih besar, final lebih kecil agar tidak override teknikal.
    sentiment_delta = round(sm_score / 15.0 * 1.2, 2)

    if sm_score >= 0:
        final_delta = round(sm_score / 15.0 * 0.60, 2)
    else:
        final_delta = round(sm_score / 15.0 * 0.80, 2)

    new_sentiment = max(1.0, min(10.0, old_sentiment + sentiment_delta))
    new_final = max(1.0, min(10.0, old_final + final_delta))

    adjusted["sentiment_score"] = round(new_sentiment, 2)
    adjusted["final_score"] = round(new_final, 2)

    adjusted["brokersum_adjusted"] = True
    adjusted["brokersum_source"] = "screenshot_all_3d"
    adjusted["smart_money_confirmation_score"] = sm_score
    adjusted["smart_money_flow_label"] = flow_label
    adjusted["smart_money_reasons"] = reasons
    adjusted["smart_money_final_delta"] = final_delta
    adjusted["smart_money_sentiment_delta"] = sentiment_delta
    adjusted["broker_concentration_pct"] = brokersum.get("broker_concentration_pct")
    adjusted["top3_buy_concentration_pct"] = brokersum.get("top3_buy_concentration_pct")
    adjusted["top3_sell_concentration_pct"] = brokersum.get("top3_sell_concentration_pct")
    adjusted["dominant_buyer_avg"] = brokersum.get("dominant_buyer_avg")
    adjusted["top_net_buyers"] = brokersum.get("top_net_buyers", [])
    adjusted["top_net_sellers"] = brokersum.get("top_net_sellers", [])

    # Recompute deterministic action setelah final/sentiment berubah.
    try:
        action = decide_action(
            adjusted.get("final_score", 0),
            adjusted.get("value_score", 0),
            adjusted.get("momentum_score", 0),
            adjusted.get("sentiment_score", 0),
            adjusted.get("is_financial_distress_flag", False),
            adjusted.get("chart_pattern"),
            adjusted.get("is_overbought_caution", False),
            adjusted.get("obv_divergence", "none"),
            adjusted.get("is_volume_spike_anomaly", False),
            adjusted.get("is_near_price_floor", False),
            adjusted.get("is_unusually_low_pe", False),
            adjusted.get("macd_bearish_cross", False),
            adjusted.get("is_below_sma50", False),
        )

        if isinstance(action, dict):
            adjusted.update(action)
        elif isinstance(action, str):
            adjusted["action"] = action
            adjusted["action_label_id"] = ACTION_LABEL_ID.get(action, action)

    except Exception as e:
        print(f"⚠️ Gagal recompute action setelah Smart Money ALL 3D: {str(e)[:120]}")

    return adjusted


def apply_whitelist_accumulation_adjustment(scoring: dict, signal: dict | None) -> dict:
    """
    Nightly-batch accumulation/distribution adjustment (MBSS v2, RapidAPI
    integration) — same mechanism as _apply_brokersum_adjustment_original
    (bounded sentiment adjustment, decide_action() re-run so the ACTUAL
    decision reflects it, not just the displayed number), but three
    deliberate differences:

    1. Sourced from the nightly whitelist sweep
       (engine.broker.compute_whitelist_accumulation_signal) — applied for
       FREE to every ticker the sweep covers during the nightly batch
       itself, not just opt-in per-tool enrichment. Index Alpha's scarce
       5-10 calls/day quota never made this affordable before; the
       whitelist sweep's flat 13-call cost does.
    2. Gated on WHITELIST broker presence specifically
       (num_whitelist_brokers), not generic concentration — concentration
       alone doesn't tell you WHO is concentrated; a single large
       retail-serving desk having a big day looks identical to genuine
       informed accumulation without this distinction.
    3. ASYMMETRIC: distribution penalty (-4) weighted heavier than
       accumulation bonus (+3) — a deliberate capital-preservation bias.
       Missing a good buy costs less than buying into real distribution.

    signal: output of compute_whitelist_accumulation_signal, or None (no
    whitelist broker active in this ticker — most tickers, most nights).
    """
    if not signal:
        scoring["whitelist_accumulation_adjusted"] = False
        return scoring

    net_pct = signal.get("net_pct", 0)
    num_brokers = signal.get("num_whitelist_brokers", 0)
    if num_brokers < 1:
        scoring["whitelist_accumulation_adjusted"] = False
        return scoring

    # Full weight at 3+ distinct whitelist brokers agreeing — less likely a
    # single desk's idiosyncratic flow, more likely genuine institutional lean.
    confidence_factor = min(1.0, num_brokers / 3)

    if net_pct >= 15:
        adjustment = 3.0 * confidence_factor
    elif net_pct <= -15:
        adjustment = -4.0 * confidence_factor
    else:
        scoring["whitelist_accumulation_adjusted"] = False
        return scoring

    old_sentiment = scoring["scores"]["sentiment"]
    new_sentiment = max(1.0, min(10.0, old_sentiment + adjustment))

    value_score = scoring["scores"]["value"]
    momentum_score = scoring["scores"]["momentum"]
    new_final = (value_score * 0.25) + (momentum_score * 0.45) + (new_sentiment * 0.30)

    decision = decide_action(
        final_score=new_final, value_score=value_score, momentum_score=momentum_score,
        sentiment_score=new_sentiment, is_financial_distress_flag=scoring.get("is_financial_distress_flag", False),
        chart_pattern=scoring.get("chart_pattern", "none"), is_overbought_caution=scoring.get("is_overbought_caution", False),
        obv_divergence=scoring.get("obv_divergence", "none"), is_volume_spike_anomaly=scoring.get("is_volume_spike_anomaly", False),
        is_near_price_floor=scoring.get("is_near_price_floor", False), is_unusually_low_pe=scoring.get("is_unusually_low_pe", False),
        macd_bearish_cross=scoring.get("macd_bearish_cross", False), is_below_sma50=scoring.get("is_below_sma50", False),
    )

    scoring["scores"]["sentiment"] = round(new_sentiment, 1)
    scoring["scores"]["final"] = round(new_final, 1)
    scoring["action_id"] = decision["action_id"]
    scoring["action_label_id"] = decision["action_label_id"]
    scoring["action_ceiling_applied"] = decision["ceiling_applied"]
    scoring["action_component_spread"] = decision["component_spread"]
    scoring["whitelist_accumulation_adjusted"] = True
    scoring["whitelist_accumulation_adjustment_applied"] = round(adjustment, 2)
    scoring["whitelist_accumulation_net_pct"] = net_pct
    scoring["whitelist_num_brokers"] = num_brokers
    return scoring


ACTION_RANK = {"AVOID_SELL": 0, "HOLD": 1, "BUY_ACCUMULATE": 2, "STRONG_BUY": 3}
ACTION_LABEL_ID = {
    "STRONG_BUY": "BELI KUAT",
    "BUY_ACCUMULATE": "BELI / AKUMULASI",
    "HOLD": "TAHAN",
    "AVOID_SELL": "HINDARI / JUAL",
    "MIXED_SIGNALS": "SINYAL CAMPURAN",
}


def decide_action(final_score, value_score, momentum_score, sentiment_score,
                   is_financial_distress_flag, chart_pattern, is_overbought_caution,
                   obv_divergence, is_volume_spike_anomaly, is_near_price_floor,
                   is_unusually_low_pe, macd_bearish_cross=False, is_below_sma50=False):
    # 1. Base tier from the blended score
    if final_score >= 7.5:
        base_tier = "STRONG_BUY"
    elif final_score >= 6.0:
        base_tier = "BUY_ACCUMULATE"
    elif final_score >= 4.5:
        base_tier = "HOLD"
    else:
        base_tier = "AVOID_SELL"

    # 2. Hard ceiling from serious flags — these override the score, not just adjust it
    if is_financial_distress_flag or chart_pattern == "lower_highs_bearish":
        ceiling = "HOLD"
    elif (is_overbought_caution or obv_divergence == "bearish_divergence"
          or is_volume_spike_anomaly or macd_bearish_cross):
        # MACD bearish crossover = momentum turning down RIGHT NOW — a fresh timing
        # signal, treated the same as our other "don't give this a STRONG BUY" flags.
        ceiling = "BUY_ACCUMULATE"
    else:
        ceiling = "STRONG_BUY"

    tier = base_tier if ACTION_RANK[base_tier] <= ACTION_RANK[ceiling] else ceiling

    # 3. Component disagreement: if value/momentum/sentiment strongly disagree with
    # each other, a single blended number is hiding real tension — surface it honestly
    # instead of picking a confident label that averages over a real conflict.
    component_spread = max(value_score, momentum_score, sentiment_score) - min(value_score, momentum_score, sentiment_score)
    strong_disagreement = component_spread >= 4.0

    # 4. Borderline buffer near the score cliffs — avoids false precision where 7.49
    # vs 7.51 would otherwise flip confidently between two very different labels.
    thresholds = [4.5, 6.0, 7.5]
    is_borderline = any(abs(final_score - t) < 0.3 for t in thresholds)
    # is_below_sma50 (still in a weaker medium-term trend) joins the other soft flags —
    # relevant for swing-trade framing since a good short-term setup inside a still-
    # weak medium-term regime is exactly the kind of tension worth surfacing.
    soft_flags_present = is_near_price_floor or is_unusually_low_pe or is_overbought_caution or is_below_sma50

    mixed_signals = strong_disagreement or (is_borderline and soft_flags_present)
    final_action = "MIXED_SIGNALS" if mixed_signals else tier

    return {
        "action_id": final_action,
        "action_label_id": ACTION_LABEL_ID[final_action],
        "base_tier_before_caps": base_tier,
        "ceiling_applied": tier != base_tier,
        "component_spread": round(component_spread, 1),
        "is_borderline": is_borderline,
    }


def compute_factor_scoring(ticker, include_quote_check=True):
    """
    Returns a scored dict for one ticker, or None if data is unavailable/insufficient.

    Scoring is ADAPTIVE PER STOCK: RSI, momentum (price vs SMA20), and volume are each
    scored by where today's reading falls within THAT stock's own trailing history,
    rather than one fixed threshold applied to every stock. This means a "high" volume
    ratio for a thin, rarely-traded stock isn't scored the same as a "high" ratio for a
    heavily-traded one like TLKM.

    DATA SOURCE: price/volume history now comes from the local SQLite EOD cache,
    which is seeded and refreshed via Yahoo Finance. yfinance is also used for the
    lighter .info lookup (PE/PB/dividend). This keeps /screendaytrade and related
    EOD flows independent from the expired iTick key.

    include_quote_check: retained for compatibility. EOD screening now relies on
    Yahoo-backed OHLCV data, so the extra direct quote check is optional and only
    used where a separate live-status signal is still desired.
    """
    # Gunakan SQLite layer dulu (get_ohlcv_smart) — fetch delta dari iTick hanya
    # kalau bar hari ini belum ada di DB. Menghemat ~99% panggilan iTick untuk
    # ticker yang sudah pernah di-scan sebelumnya.
    hist = core.get_ohlcv_smart(ticker, limit=500)

    if hist is None or hist.empty or len(hist) < 20:
        bars = 0 if hist is None or hist.empty else len(hist)
        return _excluded(ticker, f"data historis tidak cukup ({bars} bar, butuh minimal 20) — "
                                  f"kemungkinan baru listing, kode ticker salah, atau data belum ter-populate di DB lokal")

    # MBSS v2 (user request): proxy DIY untuk suspensi — lihat catatan lengkap
    # di STALE_TRADING_DAYS_THRESHOLD (legacy_core.py). Kalau bar TERAKHIR di
    # histori sudah tertinggal >=5 hari bursa dari hari ini, kemungkinan besar
    # saham ini disuspensi (atau setidaknya berhenti diperdagangkan dengan
    # alasan lain) — get_ohlcv_smart() SUDAH mencoba refresh dari Yahoo tiap
    # kali dipanggil, jadi kalau tetap stale di sini, itu genuinely bukan
    # "belum sempat di-refresh", datanya memang tidak nambah dari sumbernya.
    last_bar_date_str = hist.index[-1].strftime("%Y-%m-%d")
    stale_days = core.count_trading_days_between(last_bar_date_str)
    if stale_days is not None and stale_days >= core.STALE_TRADING_DAYS_THRESHOLD:
        # MBSS v2 (user request): langsung blacklist dari deteksi PERTAMA — ini
        # bukti langsung dari DB, bukan dugaan gagal-fetch yang perlu 3x
        # konfirmasi. Supaya eodscan RUN BERIKUTNYA sudah bisa skip cepat,
        # bukan menunggu 3 run lagi.
        core.record_direct_evidence_blacklist(ticker, f"stale-trading {stale_days} hari bursa (kemungkinan suspended)")
        return _excluded(ticker, f"data terakhir {last_bar_date_str} ({stale_days} hari bursa lalu tanpa update) — "
                                  f"kemungkinan suspended/berhenti diperdagangkan, ATAU refresh dari Yahoo Finance "
                                  f"gagal diam-diam untuk ticker ini (cek manual di Yahoo kalau ragu)")

    # Direct halt/delisted check via iTick's trading status — more authoritative than
    # inferring "frozen" purely from price action (which is still kept as a backup
    # check further below regardless of this setting, in case this is skipped or fails).
    quote = None
    if include_quote_check:
        quote = core.itick_get_quote(ticker)
        if quote is not None:
            trading_status = quote.get("ts")
            if trading_status in (1, 2):  # 1=Halt, 2=Delisted
                status_label = "halted" if trading_status == 1 else "delisted"
                return _excluded(ticker, f"iTick reports trading status = {status_label}")

    stock = core.get_yf_ticker(f"{ticker}.JK")
    # yfinance is now just an ENHANCEMENT (PE/PB/dividend) on top of iTick's core
    # price/technical data — not a hard dependency. If Yahoo is rate-limited or
    # down (which has been a recurring problem this session), we still have
    # perfectly good iTick data for this ticker and shouldn't drop it entirely
    # just because the secondary fundamentals lookup failed. Falls back to an
    # empty info dict, which the existing pe==0/pb==0 handling already treats as
    # neutral rather than crashing.
    try:
        info = core.yf_fetch_with_retry(lambda: stock.info)
    except Exception as e:
        print(f"⚠️ {ticker}: yfinance PE/PB/dividend lookup failed ({e}) — scoring with iTick data only, "
              f"value score will be neutral on the fundamentals it can't see.")
        info = {}
    close_prices = hist["Close"]
    volumes = hist["Volume"]
    low_prices = hist["Low"]
    high_prices = hist["High"]

    current_price = close_prices.iloc[-1]
    as_of_date = str(hist.index[-1].date())
    has_adaptive_baseline = len(hist) >= core.MIN_HISTORY_FOR_ADAPTIVE

    # DATA FRESHNESS CHECK: cross-check historical bar against iTick's own live quote
    # (rather than yfinance's, since iTick is now the primary price source).
    data_freshness_warning = None
    intraday_high = None
    intraday_low = None
    try:
        live_price = quote.get("ld") if quote else None
        if quote:
            intraday_high = quote.get("h")  # live intraday high, updates during trading hours
            intraday_low = quote.get("l")   # live intraday low, updates during trading hours
        if live_price and current_price:
            live_diff_pct = abs(live_price - current_price) / current_price * 100
            if live_diff_pct > 2.0:
                data_freshness_warning = (
                    f"Historical bar (Rp{int(current_price)}, as of {as_of_date}) differs "
                    f"from live quote (Rp{int(live_price)}) by {live_diff_pct:.1f}% — "
                    f"today's price action may not be fully reflected yet."
                )
    except Exception:
        pass

    # --- 0. DISTRESS / PRICE-FLOOR GUARD ---
    # IDX has an absolute minimum trade price of Rp50 ("gocap" among retail traders).
    # A stock stuck there — or one whose price hasn't genuinely moved in 10 days — isn't
    # "cheap", it's often frozen due to financial distress, suspension risk, or a
    # regulatory floor. Scoring "distance below own historical range" as a value signal
    # would wrongly rate this as maximally attractive. Exclude these outright rather
    # than let a flat price series generate a fake buy signal.
    support_10d_raw = low_prices.tail(10).min()
    resistance_10d_raw = high_prices.tail(10).max()
    price_range_pct = ((resistance_10d_raw - support_10d_raw) / max(support_10d_raw, 1)) * 100

    if current_price <= 51 or price_range_pct < 2.0:
        return _excluded(ticker, f"harga beku/di dasar (harga={current_price}, rentang 10hr={price_range_pct:.1f}%)")

    # Broader risk-preference exclusion: very low nominal-price IDX stocks correlate
    # strongly with post-restructuring/distress situations in practice (both WEGE and
    # GIAA — the two confirmed anomalies found so far — sit under Rp100). This is a
    # blunt filter, not a claim that all sub-Rp100 stocks are bad, but given a capital
    # preservation-focused strategy it's a reasonable tradeoff: excludes some legitimate
    # low-priced stocks in exchange for meaningfully reducing exposure to the riskiest,
    # most anomaly-prone segment. Adjust core.MIN_STOCK_PRICE if this feels too aggressive.
    if current_price < core.MIN_STOCK_PRICE:
        return _excluded(ticker, f"harga {current_price} di bawah batas minimum ({core.MIN_STOCK_PRICE})")

    # Doesn't fully exclude, but flags stocks trading close to the Rp50 floor (e.g.
    # post-restructuring stocks like GIAA) — these are genuinely tradeable, not
    # frozen, but carry real distress/liquidity risk that deserves explicit caution
    # rather than being scored the same as a healthy mid/large-cap stock.
    is_near_price_floor = current_price < 70

    # --- 1. SUPPORT / RESISTANCE BOUNDS (unchanged: still short-term, 10-day) ---
    support_10d = low_prices.tail(10).min()
    resistance_10d = high_prices.tail(10).max()

    # Volume ratio + histori, dan CMF — DIPINDAH ke sini (lebih awal dari biasanya)
    # karena sekarang dibutuhkan Momentum (reward/penalti volume, kondisi hapus
    # otomatis pakai CMF), bukan cuma Sentiment seperti sebelumnya. Sentiment di
    # bawah nanti REUSE variabel yang sama, tidak dihitung ulang.
    avg_vol_20d = volumes.rolling(window=20).mean().iloc[-1]
    current_vol = volumes.iloc[-1]
    vol_ratio = current_vol / (avg_vol_20d + 1e-9)
    vol_ratio_full_series = volumes / (volumes.rolling(window=20).mean() + 1e-9)

    cmf_series = core.calculate_cmf(high_prices, low_prices, close_prices, volumes)
    current_cmf = cmf_series.iloc[-1] if not cmf_series.empty else None

    # NOTE: target_buy_min/max, tp_1, cut_loss are computed FURTHER DOWN, after
    # momentum/MACD/decision are known — TP1 is now target-%-driven (min +5%,
    # up to +10% if confidence is high AND estimated horizon is under 5 days),
    # which needs those signals as input.

    # --- 2. VALUE SCORE ---
    # PREVIOUS VERSION BUG: this used to blend in "price percentile within its own
    # 1-2yr trading range" as a value signal. That's actually a MOMENTUM/mean-reversion
    # signal, not a value signal — a stock that just rallied hard on strong fundamentals
    # would get penalized here purely because its price is now higher in its own range,
    # even if its actual valuation (PBV, dividend yield) still looks cheap. Fixed by
    # removing that and using genuine fundamental factors instead: PE, PB, and dividend
    # yield — the same things real analysts and dividend-focused investors weigh.
    # BUGFIX (kasus nyata PSSI, ditemukan lewat log user): yfinance TIDAK selalu
    # mengembalikan angka murni untuk field .info ini — beberapa ticker (data
    # quality edge case dari Yahoo, biasa terjadi pada saham kecil/baru listing)
    # mengembalikan string aneh (mis. "Infinity") alih-alih float/None. Sebelumnya
    # cuma `or 0` (yang tidak menangkap string non-kosong), jadi `pe < 0` di bawah
    # bisa meledak dengan "'<' not supported between instances of 'str' and 'int'".
    # core._safe_float() memaksa jadi angka atau fallback 0 (masuk jalur
    # "missing/unknown" yang sudah ada, bukan dianggap 0 secara keliru).
    pe = core._safe_float(info.get("trailingPE"), default=0.0)
    pb = core._safe_float(info.get("priceToBook"), default=0.0)

    # SANITY BOUND (kasus nyata IATA, dikonfirmasi user lewat cek manual ke
    # Yahoo langsung): sistem kita sempat baca PB IATA = 25.333,3 dari
    # yfinance .info["priceToBook"] — padahal PB ASLI di halaman Yahoo sendiri
    # cuma 1.00. PE untuk ticker yang SAMA cocok persis (42.0 vs 42.01 asli),
    # jadi ini BUKAN bug perhitungan di kode kita (cuma `info.get(...)` polos,
    # tidak ada kalkulasi tambahan) — ini murni data quality issue dari
    # yfinance/.info sendiri untuk field priceToBook, kemungkinan sumber
    # berbeda dari yang dipakai halaman "Statistics" Yahoo. PB/PE ekstrem
    # BUKAN sinyal distress asli (PB 1.00 justru sehat) — diperlakukan sebagai
    # "tidak bisa dipercaya", direset ke 0 supaya masuk jalur "missing/unknown
    # -> netral 5.0" yang sudah ada, BUKAN dipakai untuk menghukum skor Nilai.
    PB_SANITY_MAX = 100
    PE_SANITY_MAX = 500  # PE bisa sah tinggi utk perusahaan hampir-tanpa-laba, ambang lebih longgar dari PB
    if pb > PB_SANITY_MAX:
        # MBSS v2 (user request — temuan lewat log produksi nyata: TOBA, WINS,
        # IPOL, ADRO, PGAS, SMMT, GDYR, BELL, GGRP SEMUA kena di 1x /eodscan
        # — termasuk ADRO & PGAS yang saham BLUE-CHIP BESAR, bukan saham
        # tipis/obscure seperti IATA kemarin. Skala masalahnya jauh lebih
        # luas dari dugaan awal — bukan cuma kasus langka, kemungkinan
        # `priceToBook` yfinance memang bermasalah SISTEMIK untuk saham IDX).
        # SEBELUM langsung buang jadi "tidak diketahui", coba hitung ULANG
        # sendiri dari price ÷ bookValue (field TERPISAH dari priceToBook di
        # info dict, tidak bergantung pada rasio pra-hitung Yahoo yang
        # ternyata sering salah) — kalau hasilnya masuk akal, PAKAI itu,
        # jangan buang sinyal yang sebenarnya bisa diselamatkan.
        book_value_per_share = core._safe_float(info.get("bookValue"), default=0.0)
        pb_recomputed = (current_price / book_value_per_share) if book_value_per_share > 0 else 0.0
        if 0 < pb_recomputed <= PB_SANITY_MAX:
            _report_data_quality_warning(
                f"{ticker} (PB dipulihkan: {pb_recomputed:.1f})",
                f"⚠️ {ticker}: PB {pb} dari field priceToBook rusak (>{PB_SANITY_MAX}x) — "
                f"dihitung ULANG dari price/bookValue = {pb_recomputed:.2f}, dipakai sebagai ganti."
            )
            pb = pb_recomputed
        else:
            _report_data_quality_warning(
                f"{ticker} (PB {pb:.0f})",
                f"⚠️ {ticker}: PB {pb} dari yfinance melewati batas wajar ({PB_SANITY_MAX}x) — "
                f"kemungkinan data quality issue, diperlakukan sebagai tidak diketahui, bukan sinyal distress"
            )
            pb = 0.0
    if pe > PE_SANITY_MAX:
        _report_data_quality_warning(
            f"{ticker} (PE {pe:.0f})",
            f"⚠️ {ticker}: PE {pe} dari yfinance melewati batas wajar ({PE_SANITY_MAX}x) — "
            f"kemungkinan data quality issue, diperlakukan sebagai tidak diketahui"
        )
        pe = 0.0

    # MBSS v2 (user request — perkaya insights, "explore variable data yfinance
    # lagi"): field TAMBAHAN yang GRATIS — semuanya dari info dict yang SAMA
    # yang sudah kita fetch buat PE/PB/dividend/sector, TIDAK ADA fetch
    # jaringan baru. Sengaja BELUM diikutsertakan ke formula skor Value —
    # itu keputusan terpisah (lihat diskusi "fundamental sebagai bonus vs
    # komponen inti" sebelumnya) — untuk sekarang murni informasi tambahan,
    # ditampilkan/tersimpan, siap dipakai nanti kalau memang mau diintegrasikan.
    revenue_growth_pct = core._safe_float(info.get("revenueGrowth"), default=None)
    if revenue_growth_pct is not None:
        revenue_growth_pct = round(revenue_growth_pct * 100, 1)  # yfinance kasih desimal (0.12 = 12%)

    roe_pct = core._safe_float(info.get("returnOnEquity"), default=None)
    if roe_pct is not None:
        roe_pct = round(roe_pct * 100, 1)

    profit_margin_pct = core._safe_float(info.get("profitMargins"), default=None)
    if profit_margin_pct is not None:
        profit_margin_pct = round(profit_margin_pct * 100, 1)

    industry = info.get("industry")  # lebih granular dari sector, mis. "Coking Coal" vs sector "Energy"

    forward_pe = core._safe_float(info.get("forwardPE"), default=None)
    if forward_pe is not None and forward_pe > PE_SANITY_MAX:
        # Sama seperti trailingPE — bisa kena data quality issue yang sama,
        # jangan asumsikan forwardPE otomatis bersih cuma karena field baru.
        forward_pe = None

    peg_ratio = core._safe_float(info.get("pegRatio"), default=None)
    if peg_ratio is not None and (peg_ratio <= 0 or peg_ratio > 10):
        # PEG rasio sehat biasanya jauh di bawah 10 — di luar itu kemungkinan
        # besar data quality issue yang sama seperti PB/PE (rasio turunan,
        # rawan pola kerusakan yang sama).
        peg_ratio = None

    dividend_yield_raw = core._safe_float(info.get("dividendYield"), default=0.0)
    # yfinance sometimes returns this as a decimal (0.0886) and sometimes as a whole
    # percentage number (8.86) depending on ticker/version — normalize to a percentage.
    dividend_yield_pct = dividend_yield_raw * 100 if dividend_yield_raw < 1 else dividend_yield_raw

    # Smooth continuous scoring instead of int()-truncated ratios — the previous
    # int(25/pe) style formula had a harsh cliff that collapsed to score 1 for ANY
    # PE above ~12.5, treating completely normal blue-chip valuations (e.g. PE 15-20,
    # common for banks/telcos) the same as genuinely expensive stocks. This is what
    # was still dragging TLKM's value score down even after removing the price-
    # percentile contamination.
    # BUG FIX: pe<=0 or pb<=0 used to score a perfect 10 — meant to handle "missing
    # data" but this also silently rewarded genuinely NEGATIVE earnings (loss-making
    # company) or negative book value (accumulated losses wiping out equity — a real
    # distress signal) with the maximum possible value score. Fixed: missing/zero data
    # is now neutral (5), and a NEGATIVE ratio (confirmed loss/negative equity, not
    # just missing data) is scored low (2), since that's a genuine red flag.
    if pe is None or pe == 0:
        pe_score_fixed = 5.0  # unknown — don't reward or penalize blindly
    elif pe < 0:
        pe_score_fixed = 2.0  # confirmed negative earnings — real caution signal
    else:
        pe_score_fixed = max(1.0, min(10.0, 10 - (pe - 5) / 3))

    if pb is None or pb == 0:
        pb_score_fixed = 5.0
    elif pb < 0:
        pb_score_fixed = 2.0  # negative book value — equity wiped out by losses
    else:
        pb_score_fixed = max(1.0, min(10.0, 10 - (pb - 0.5) / 0.35))

    is_financial_distress_flag = (pe is not None and pe < 0) or (pb is not None and pb < 0)
    dividend_score = max(1.0, min(10.0, dividend_yield_pct))  # roughly 1 point per 1% yield, capped

    # Bobot direvisi user: PE 25 / PB 35 / Dividend 15 / Growth Catalyst 25 (dihapus,
    # susah dikuantifikasi) — dinormalisasi proporsional ke 100% dari 3 komponen
    # yang tersisa, mempertahankan rasio relatif PE<PB, Dividend paling kecil.
    value_score = (pe_score_fixed * 0.333) + (pb_score_fixed * 0.467) + (dividend_score * 0.20)

    # --- 3. MOMENTUM SCORE (RSI + price-vs-SMA20, both adaptive per stock) ---
    sma20_series = close_prices.rolling(window=20).mean()
    sma20 = sma20_series.iloc[-1]
    rsi_series = core.calculate_rsi(close_prices)
    current_rsi = rsi_series.iloc[-1]

    if pd.isna(current_rsi) or pd.isna(sma20):
        return _excluded(ticker, "RSI atau SMA20 tidak bisa dihitung (data historis ada celah/tidak lengkap)")

    if has_adaptive_baseline:
        # Use this stock's OWN historical RSI distribution to define what "healthy middle
        # ground" vs "overbought/oversold" means for it, instead of fixed 45/65/75 bands.
        rsi_hist = rsi_series.iloc[:-1].dropna()
        p30, p45, p55, p65, p75 = (
            rsi_hist.quantile(0.30), rsi_hist.quantile(0.45), rsi_hist.quantile(0.55),
            rsi_hist.quantile(0.65), rsi_hist.quantile(0.75),
        ) if len(rsi_hist) >= core.MIN_HISTORY_FOR_ADAPTIVE else (30, 45, 55, 65, 75)
    else:
        p30, p45, p55, p65, p75 = 30, 45, 55, 65, 75

    # Split the old flat "45-65 = ideal" zone: RSI approaching the upper end (55-65) is
    # genuinely warmer / closer to overbought than the true middle (45-55), and should
    # score lower accordingly — not get the same max score as a stock sitting comfortably
    # in the middle. This prevents the scoring from silently rewarding a stock while the
    # generated text separately (and correctly) warns "watch out for overbought."
    is_overbought_caution = False
    if p45 <= current_rsi <= p55:
        rsi_score = 9
    elif p55 < current_rsi <= p65:
        rsi_score = 6.5
        is_overbought_caution = True
    elif p30 <= current_rsi < p45:
        rsi_score = 7
    elif p65 < current_rsi <= p75:
        rsi_score = 4.5
        is_overbought_caution = True
    elif current_rsi > p75:
        rsi_score = 3
        is_overbought_caution = True
    else:
        rsi_score = 5

    sma_dist_pct = ((current_price - sma20) / sma20) * 100
    if has_adaptive_baseline:
        dist_series = ((close_prices - sma20_series) / sma20_series * 100).iloc[:-1]
        dist_pct_rank = core.percentile_rank(dist_series, sma_dist_pct)
        sma_score = core.score_from_percentile(dist_pct_rank)
    else:
        sma_score = max(1, min(10, int(5 + (sma_dist_pct * 2))))

    # EMA21: sama pola adaptif dengan SMA20, tapi lebih reaktif terhadap harga
    # terbaru (weighted, bukan rata rata datar) — dipilih khusus karena user
    # berorientasi swing pendek 1-5 hari, butuh sinyal yang lebih cepat merespons
    # dibanding SMA20 yang cenderung lamban untuk horizon sependek itu.
    ema21_series = close_prices.ewm(span=21, adjust=False).mean()
    ema21 = ema21_series.iloc[-1]
    ema21_dist_pct = ((current_price - ema21) / ema21) * 100
    if has_adaptive_baseline:
        ema21_dist_series = ((close_prices - ema21_series) / ema21_series * 100).iloc[:-1]
        ema21_dist_pct_rank = core.percentile_rank(ema21_dist_series, ema21_dist_pct)
        ema21_score = core.score_from_percentile(ema21_dist_pct_rank)
    else:
        ema21_score = max(1, min(10, int(5 + (ema21_dist_pct * 2))))
    is_below_ema21 = bool(current_price < ema21)

    # Direvisi user: EMA21 jadi SATU-SATUNYA base Momentum (bukan lagi RSI+SMA20
    # weighted seperti sebelumnya) — RSI dipindah jadi FILTER/plafon di akhir
    # formula (lihat bawah, setelah semua lapisan tambahan), bukan komponen
    # linear berbobot lagi. SMA20 tidak lagi dipakai di base sama sekali — EMA21
    # lebih reaktif, lebih cocok horizon swing pendek 1-5 hari.
    momentum_score = ema21_score

    # --- MACD: timing/crossover signal, distinct from RSI's overbought/oversold read ---
    # Swing/day-trade oriented: we care about RECENT crossovers (momentum turning right
    # now), not just the static current state, since a stock can sit "MACD bearish" for
    # a long stable stretch without that being a fresh, actionable signal.
    macd_line, signal_line, macd_hist = core.calculate_macd(close_prices)
    current_macd_hist = macd_hist.iloc[-1]
    prev_macd_hist = macd_hist.iloc[-2] if len(macd_hist) > 1 else current_macd_hist
    macd_bullish_cross = bool(current_macd_hist > 0 and prev_macd_hist <= 0)
    macd_bearish_cross = bool(current_macd_hist < 0 and prev_macd_hist >= 0)
    macd_state = "bullish" if current_macd_hist > 0 else "bearish"
    # MBSS v2 (user request — Explosive Lane "TRUE EXPLOSIVE" review): MACD
    # line di ATAS garis nol menandakan regime bullish yang lebih established
    # (bukan cuma histogram baru saja positif tipis di dekat nol, yang bisa
    # jadi cross lemah/masih dalam fase pemulihan dari downtrend). Beda dari
    # macd_state yang cuma baca TANDA histogram (macd_line - signal_line).
    macd_line_above_zero = bool(macd_line.iloc[-1] > 0)

    # MBSS v2 (user request, real observasi live intraday — saham dengan
    # "order buy tebal" yang bergerak mengikuti harga, seolah dijaga rapi
    # di level tertentu yang naik bareng harga). Bot TIDAK punya akses
    # order-book depth sama sekali (tidak bisa lihat wall order beneran) —
    # ini PROXY dari OHLCV harian yang SUDAH ada: seberapa rapat LOW harian
    # mengikuti EMA9 yang NAIK selama 10 hari terakhir, minim undercut.
    # MURNI INFORMASIONAL/BONUS — user eksplisit TIDAK mau ini jadi filter
    # yang mengecilkan pool kandidat, cuma tag tambahan buat kualitas pick,
    # sama disiplin tag-and-track dengan fast_candidate/bollinger_squeeze
    # (snapshot dulu, validasi forward, baru pertimbangkan jadi skor kalau
    # prospectively terbukti).
    tight_trailing_support = False
    ema9_slope_pct = None
    trailing_support_undercut_days = None
    TRAILING_SUPPORT_LOOKBACK = 10
    if len(close_prices) >= TRAILING_SUPPORT_LOOKBACK + 9:
        ema9_series = close_prices.ewm(span=9, adjust=False).mean()
        recent_ema9 = ema9_series.tail(TRAILING_SUPPORT_LOOKBACK)
        recent_lows = low_prices.tail(TRAILING_SUPPORT_LOOKBACK)
        ema9_start = float(recent_ema9.iloc[0])
        if abs(ema9_start) > 1e-9:
            ema9_slope_pct = round((float(recent_ema9.iloc[-1]) - ema9_start) / ema9_start * 100, 2)
        undercut_days = sum(
            1 for lo, ma in zip(recent_lows, recent_ema9) if float(lo) < float(ma) * 0.99
        )
        trailing_support_undercut_days = undercut_days
        is_rising = ema9_slope_pct is not None and ema9_slope_pct > 1.0  # EMA9 naik >1% selama 10 hari, bukan cuma flat/noise
        tight_trailing_support = bool(is_rising and undercut_days <= 2)  # maks 2 dari 10 hari boleh undercut signifikan

    # Berapa hari sejak cross terakhir (mundur cari kapan tanda histogram
    # terakhir berubah) — dipakai untuk formula PELURUHAN (decay) pengaruh
    # terhadap skor: cross 1 hari lalu = hampir penuh pengaruhnya, cross 5 hari
    # lalu = nyaris habis pengaruhnya, pas dengan horizon swing 1-5 hari user
    # (bukan lagi cuma True/False biner "baru saja" yang tidak presisi harinya).
    macd_cross_days_ago = None
    macd_cross_direction = None
    current_sign = current_macd_hist > 0
    for days_back in range(1, min(30, len(macd_hist))):
        past_sign = macd_hist.iloc[-1 - days_back] > 0
        if past_sign != current_sign:
            macd_cross_days_ago = days_back
            macd_cross_direction = "bullish" if current_sign else "bearish"
            break

    if macd_cross_days_ago is not None:
        decay = max(0.0, 1 - (macd_cross_days_ago / 5))
        if macd_cross_direction == "bearish":
            momentum_score = max(1.0, momentum_score - 1.5 * decay)
        elif macd_cross_direction == "bullish":
            momentum_score = min(10.0, momentum_score + 1.0 * decay)

    # --- SMA50: medium-term trend filter (swing-relevant horizon, not SMA200/long-term
    # investing horizon) — catches "looks fine short-term but still in a weaker medium-
    # term regime" cases that SMA20 alone (already used above) can miss.
    is_below_sma50 = False
    if len(close_prices) >= 50:
        sma50 = close_prices.rolling(window=50).mean().iloc[-1]
        is_below_sma50 = bool(current_price < sma50)

    # --- ADX: pengali KEPERCAYAAN terhadap momentum_score, bukan sekadar info.
    # ADX rendah (<20) = pasar sideways/noise — sinyal momentum apa pun di
    # kondisi ini kurang bisa dipercaya, jadi skor ditarik mendekati netral
    # (bukan dihapus sepenuhnya, cuma dikecilkan pengaruhnya).
    adx_series = core.calculate_adx(high_prices, low_prices, close_prices)
    current_adx = adx_series.iloc[-1]
    is_weak_trend = bool(current_adx < 20)
    if is_weak_trend:
        momentum_score = 5.0 + (momentum_score - 5.0) * 0.5

    # --- Breakout High 20 hari: harga tertinggi baru dalam 20 hari — sinyal
    # breakout klasik, beda dari resistance 10-hari yang sudah dipakai di
    # target harga (window lebih pendek, ini window lebih panjang khusus untuk
    # deteksi breakout).
    is_new_high_20d = False
    if len(high_prices) >= 20:
        high_20d = high_prices.tail(20).max()
        is_new_high_20d = bool(current_price >= high_20d)
        if is_new_high_20d:
            momentum_score = min(10.0, momentum_score + 1.0)

    # --- Relative Strength vs IHSG: DIGANTI dari versi 10-hari kumulatif ke
    # HARIAN atas permintaan user — untuk swing pendek, 10 hari kurang responsif.
    # Saham naik 1% hari ini itu biasa saja kalau IHSG naik 2% hari itu juga
    # (saham itu sebenarnya KALAH dari pasar HARI INI) — RS mengukur kekuatan
    # RELATIF harian, bukan cuma arah sendirian. Threshold diturunkan dari ±3%
    # (yang cocok untuk kumulatif 10 hari) ke ±1% (lebih masuk akal untuk
    # perbedaan satu hari — 3% beda dalam SATU hari itu sudah sangat ekstrem).
    relative_strength_vs_ihsg = None
    if len(close_prices) >= 2:
        stock_return_today = ((current_price - close_prices.iloc[-2]) / close_prices.iloc[-2]) * 100
        ihsg_return_today = market_engine.get_ihsg_return_today()
        if ihsg_return_today is not None:
            relative_strength_vs_ihsg = round(stock_return_today - ihsg_return_today, 2)
            if relative_strength_vs_ihsg > 1:
                momentum_score = min(10.0, momentum_score + 0.5)
            elif relative_strength_vs_ihsg < -1:
                momentum_score = max(1.0, momentum_score - 0.5)

    # Chart structure check: a lower-highs pattern signals weakening upside even when
    # RSI/SMA look fine in isolation — this is what addresses "wait for breakout above
    # X" style feedback that pure indicator-based scoring misses entirely.
    swing_analysis = core.detect_lower_highs(high_prices)
    if swing_analysis["pattern"] == "lower_highs_bearish":
        momentum_score = max(1.0, momentum_score - 2.0)

    # --- Reward volume besar: breakout hampir selalu dimulai dari volume, jadi
    # dapat bonus langsung ke Momentum (bukan cuma memengaruhi Sentiment seperti
    # sebelumnya). Ambil TIER TERTINGGI saja, tidak kumulatif.
    if vol_ratio > 3.0:
        momentum_score = min(10.0, momentum_score + 1.5)
    elif vol_ratio > 2.0:
        momentum_score = min(10.0, momentum_score + 1.0)
    elif vol_ratio > 1.5:
        momentum_score = min(10.0, momentum_score + 0.5)

    # --- Penalti saham "mati": volume rendah (<0.8x) BERTURUT-TURUT, bukan cuma
    # 1 hari (wajar) — 3 hari mulai menunjukkan memang tidak ada minat pasar.
    # Hilang otomatis begitu ada tanda uang mulai masuk lagi (volume pulih,
    # breakout+volume, atau CMF jelas positif) — supaya tidak "menghukum"
    # saham yang baru saja mulai bergerak lagi.
    consecutive_low_volume_days = 0
    for i in range(1, min(10, len(vol_ratio_full_series)) + 1):
        if vol_ratio_full_series.iloc[-i] < 0.8:
            consecutive_low_volume_days += 1
        else:
            break

    dead_stock_penalty_lifted = (
        vol_ratio >= 1.2
        or (is_new_high_20d and vol_ratio > 1.0)  # breakout disertai volume
        or (current_cmf is not None and not pd.isna(current_cmf) and current_cmf > 0.15)  # CMF jelas positif
    )

    if consecutive_low_volume_days >= 3 and not dead_stock_penalty_lifted:
        if consecutive_low_volume_days >= 8:
            momentum_score = max(1.0, momentum_score - 2.0)
        elif consecutive_low_volume_days >= 5:
            momentum_score = max(1.0, momentum_score - 1.5)
        else:  # >= 3
            momentum_score = max(1.0, momentum_score - 1.0)

    # --- RSI sebagai FILTER, bukan komponen berbobot lagi: cuma membatasi SISI
    # ATAS (overbought ekstrem, ambang adaptif p75 yang sudah ada) — TIDAK
    # memaksa turun saat oversold, karena oversold pada swing justru sering jadi
    # peluang entry, bukan sinyal buruk. Plafon 7.0 adalah pilihan awal, bisa
    # disesuaikan lagi setelah dicoba di data nyata.
    RSI_OVERBOUGHT_MOMENTUM_CAP = 7.0
    if current_rsi > p75:
        momentum_score = min(momentum_score, RSI_OVERBOUGHT_MOMENTUM_CAP)

    # --- 4. SENTIMENT SCORE (volume, adaptive per stock, sharpened by CMF + OBV divergence) ---
    # vol_ratio, current_cmf sudah dihitung lebih awal (dibutuhkan Momentum duluan) —
    # reuse di sini, tidak dihitung ulang.
    if has_adaptive_baseline:
        vol_ratio_series = vol_ratio_full_series.iloc[:-1]
        vol_pct_rank = core.percentile_rank(vol_ratio_series, vol_ratio)
        sentiment_score = core.score_from_percentile(vol_pct_rank)
    else:
        if vol_ratio > 2.0:
            sentiment_score = 10
        elif vol_ratio > 1.5:
            sentiment_score = 8
        elif vol_ratio > 1.0:
            sentiment_score = 6
        elif vol_ratio < 0.5:
            sentiment_score = 2
        else:
            sentiment_score = 4

    # CMF: was volume actually buying pressure (closes near daily highs) or selling
    # pressure (closes near daily lows)? Pulls the raw volume-ratio score toward
    # what the money flow direction actually shows, instead of treating all high
    # volume as automatically bullish.
    if current_cmf is not None and not pd.isna(current_cmf):
        # current_cmf ranges roughly -1 to +1; nudge sentiment score toward it
        cmf_adjustment = current_cmf * 2.0  # e.g. CMF of -0.5 pulls score down ~1 point
        sentiment_score = max(1.0, min(10.0, sentiment_score + cmf_adjustment))
    else:
        current_cmf = None

    # --- Bollinger Band position + squeeze (MBSS v2, user request — diskusi
    # Investopedia Bollinger Bands). Dua sinyal terpisah dari band yang sama:
    #
    # 1) Band touch (bounce/waspada) — sentuh lower/upper band adalah sinyal
    #    mean-reversion KLASIK, TAPI cuma valid di kondisi ranging/lemah — di
    #    trend KUAT harga bisa "band walking" (nempel di satu sisi band
    #    berhari-hari, itu justru KELANJUTAN trend, bukan reversal). Gate
    #    pakai current_adx (cutoff 25, sama seperti format_adx_label "tren
    #    kuat") + is_below_ema21 (arah trend) supaya tidak salah kaprah treat
    #    band-walking sebagai sinyal reversal:
    #    - ADX<25 (ranging) ATAU arah trend BERLAWANAN dari band yang
    #      disentuh (mis. dekat upper band tapi is_below_ema21=True) ->
    #      mean-reversion genuinely lebih kredibel -> adjustment penuh ke
    #      sentiment_score.
    #    - ADX>=25 DAN arah trend SEARAH band yang disentuh -> band walking,
    #      bukan reversal -> adjustment ditekan ke 0.
    #    Konfirmasi tambahan dari CMF (ambang 0.15, sama dengan cmf_adjustment
    #    di atas) mengurangi keyakinan adjustment kalau arus uang masih kuat
    #    melawan arah reversal yang diharapkan.
    #
    # 2) Squeeze (bandwidth di persentil rendah histori ~6 bulan) — sinyal
    #    PRA-breakout, muncul SEBELUM harga mulai bergerak. SENGAJA TIDAK
    #    dijadikan kriteria di compute_high_conviction_score: kandidat yang
    #    lagi squeeze biasanya belum breakout_close_confirmed/near_high sama
    #    sekali (masih konsolidasi di tengah), jadi menggabungkannya ke
    #    threshold HC yang mensyaratkan >=70% kriteria breakout-SUDAH-terjadi
    #    justru membuatnya nyaris tidak pernah lolos — pick yang muncul dari
    #    scanner sering kali memang sudah terlanjur naik (user observation).
    #    Disimpan sebagai field informasi murni (bollinger_squeeze,
    #    bollinger_bandwidth_percentile) — TIDAK mengubah skor apa pun —
    #    supaya bisa ditampilkan terpisah sebagai watchlist pra-breakout
    #    (pola sama seperti AKUMULASI/PRA-BREAKOUT di /hc).
    bb_signal_note = None
    bollinger_squeeze = None
    bollinger_bandwidth_percentile = None
    if len(close_prices) >= 20:
        _bb_sma20_series = close_prices.rolling(20).mean()
        _bb_std20_series = close_prices.rolling(20).std()
        _bb_sma20 = _bb_sma20_series.iloc[-1]
        _bb_std20 = _bb_std20_series.iloc[-1]
        bb_upper_val = _bb_sma20 + 2 * _bb_std20
        bb_lower_val = _bb_sma20 - 2 * _bb_std20
        bb_width = bb_upper_val - bb_lower_val

        _bandwidth_series = ((_bb_sma20_series + 2 * _bb_std20_series) - (_bb_sma20_series - 2 * _bb_std20_series)) / _bb_sma20_series
        _bandwidth_history = _bandwidth_series.dropna().tail(core.MIN_HISTORY_FOR_ADAPTIVE)
        if len(_bandwidth_history) >= 20:
            current_bandwidth = _bandwidth_history.iloc[-1]
            bandwidth_pct_rank = core.percentile_rank(_bandwidth_history.iloc[:-1], current_bandwidth)
            bollinger_bandwidth_percentile = round(bandwidth_pct_rank * 100, 1)
            bollinger_squeeze = bool(bandwidth_pct_rank <= 0.20)

        if bb_width > 0:
            percent_b = (current_price - bb_lower_val) / bb_width
            strong_trend = bool(current_adx >= 25)
            trend_bullish = not is_below_ema21
            bb_adjustment = 0.0
            if percent_b <= 0.1:  # dekat/menembus lower band
                band_walking_down = strong_trend and is_below_ema21
                if not band_walking_down:
                    bb_adjustment = 1.5
                    if current_cmf is not None and not pd.isna(current_cmf) and current_cmf < -0.15:
                        bb_adjustment = 0.5  # arus jual masih dominan, kurangi keyakinan bounce
                    bb_signal_note = "near_lower_band_bounce_candidate"
                else:
                    bb_signal_note = "band_walking_down"
            elif percent_b >= 0.9:  # dekat/menembus upper band
                band_walking_up = strong_trend and trend_bullish
                if not band_walking_up:
                    bb_adjustment = -1.5
                    if current_cmf is not None and not pd.isna(current_cmf) and current_cmf > 0.15:
                        bb_adjustment = -0.5  # arus beli masih kuat, kurangi urgency waspada
                    bb_signal_note = "near_upper_band_caution"
                else:
                    bb_signal_note = "band_walking_up"
            if bb_adjustment:
                sentiment_score = max(1.0, min(10.0, sentiment_score + bb_adjustment))

    # OBV divergence: the key check for "price looks fine but volume flow disagrees"
    obv_series = core.calculate_obv(close_prices, volumes)
    obv_divergence = core.detect_obv_divergence(close_prices, obv_series)
    if obv_divergence == "bearish_divergence":
        sentiment_score = max(1.0, sentiment_score - 2.5)  # override: flow says distribution
    elif obv_divergence == "bullish_divergence":
        sentiment_score = min(10.0, sentiment_score + 1.0)

    # Extreme volume spikes (e.g. 5x+ normal) are anomalies, not automatically bullish —
    # they're often one-off news/rumor-driven or thin-liquidity events that can reverse
    # sharply. Flag rather than let a huge ratio blindly max out the sentiment score.
    is_volume_spike_anomaly = vol_ratio > 3.0

    # Direvisi user: dari Value 30/Momentum 40/Sentiment 30 jadi Value 25/Momentum
    # 45/Sentiment 30 — untuk swing pendek, Momentum harus mengalahkan Value
    # (mengejar uang beberapa hari, bukan mencari saham termurah).
    final_score = (value_score * 0.25) + (momentum_score * 0.45) + (sentiment_score * 0.30)

    # MBSS v2 (user request — Bias Bandar sebagai KALKULASI, bukan cuma
    # peringatan, per studi kasus manual TMPO/MDIA/JGLE/DOOH/ICON): penalti
    # bertingkat, digabung dengan posisi MA50 (dua sinyal searah = penalti
    # lebih berat, bukan cuma dijumlah). Lazy import nightly.py (bukan di
    # atas file) — scoring.py di-import OLEH nightly.py, import balik di
    # level modul bakal circular.
    bias_bandar_label = None
    try:
        import engine.nightly as _nightly_engine
        daily_history = _nightly_engine.load_broksum_daily_history()
        bias = broker_engine.classify_bias_bandar(ticker, daily_history)
        bias_bandar_label = bias["label"]
        extended = is_below_sma50 is False  # "di atas MA50" = kondisi extended untuk kombinasi penalti

        if bias_bandar_label == "DISTRIBUSI":
            final_score -= 3.0 if extended else 1.5
        elif bias_bandar_label == "TANPA DUKUNGAN":
            final_score -= 2.0 if extended else 1.0
        final_score = max(0.0, final_score)  # jangan sampai negatif
    except Exception as e:
        print(f"⚠️ {ticker}: gagal hitung penalti Bias Bandar: {e}")

    company_name = info.get("longName") or info.get("shortName") or ticker
    sector = info.get("sector") or "N/A"

    # An unusually low PE (e.g. under ~3) can indicate a one-off/non-recurring gain
    # distorting trailing earnings, not genuine sustainable cheapness — a different
    # flavor of "too good to be true" value read than the negative-PE distress case.
    is_unusually_low_pe = (pe is not None and 0 < pe < 3)

    # FULL DETERMINISTIC DECISION — Gemini explains this, it does not choose or
    # override it. See decide_action() docstring/comments for why this moved out
    # of the prompt entirely.
    decision = decide_action(
        final_score=final_score, value_score=value_score, momentum_score=momentum_score,
        sentiment_score=sentiment_score, is_financial_distress_flag=is_financial_distress_flag,
        chart_pattern=swing_analysis["pattern"], is_overbought_caution=is_overbought_caution,
        obv_divergence=obv_divergence, is_volume_spike_anomaly=is_volume_spike_anomaly,
        is_near_price_floor=is_near_price_floor, is_unusually_low_pe=is_unusually_low_pe,
        macd_bearish_cross=macd_bearish_cross, is_below_sma50=is_below_sma50,
    )

    # --- TARGET HARGA BERBASIS % ---
    # TP1 sekarang diarahkan ke target persentase (bukan murni level resistance
    # teknikal seperti sebelumnya): default minimal +5%, naik ke +10% HANYA kalau
    # momentum/MACD/keputusan semuanya mendukung DAN estimasi hari untuk capai
    # +10% (berdasar volatilitas 10hr saham itu sendiri) di bawah 5 hari — supaya
    # target 10% tidak asal dipasang tanpa dasar realistis.
    target_buy_min = round(support_10d / 5) * 5
    target_buy_max = round((current_price * 1.01) / 5) * 5

    # CUT LOSS — dihitung LEBIH DULU sekarang (sebelum TP1), karena TP1 perlu
    # tahu jarak SL untuk menjamin risk:reward yang layak (lihat di bawah).
    MAX_CUTLOSS_DISTANCE_FROM_ENTRY_PCT = 5.0
    cut_loss_from_support = support_10d * 0.96
    cut_loss_max_distance = target_buy_max * (1 - MAX_CUTLOSS_DISTANCE_FROM_ENTRY_PCT / 100)
    cut_loss = max(cut_loss_from_support, cut_loss_max_distance)
    cut_loss = min(cut_loss, target_buy_min * 0.98)  # pengaman: SL selalu di bawah entry_min
    cut_loss = round(cut_loss / 5) * 5
    if cut_loss >= target_buy_min:
        # Artefak pembulatan ke kelipatan 5 — nilai sebelum dibulatkan sudah di
        # bawah entry_min, tapi pembulatan bisa membuat keduanya "bertemu" jadi
        # sama, membuat risk_at_min = 0. Turunkan 1 kelipatan pembulatan lagi
        # untuk memastikan strictly di bawah.
        cut_loss = target_buy_min - 5

    # --- TARGET HARGA BERBASIS % ---
    # TP1: default minimal +5%, naik ke +10% kalau momentum/MACD/keputusan semua
    # mendukung DAN horizon <5 hari, ATAU level resistance teknikal kalau lebih
    # tinggi dari itu — SEPENUHNYA dari sinyal teknikal asli, TIDAK disesuaikan
    # berdasarkan RR yang diinginkan.
    #
    # CATATAN KOREKSI (ditemukan lewat pertanyaan tajam user, "apa dasarnya,
    # atau dikarang biar RR-nya bagus?"): versi sebelumnya SEMPAT menambahkan
    # "tp1_for_min_rr" — memaksa TP1 naik sampai RR di entry_max mencapai
    # 1.5:1. Itu SALAH METODOLOGI: TP1 jadi direkayasa MUNDUR dari rasio yang
    # diinginkan, bukan dihitung dari sinyal teknikal genuine — persis
    # kekhawatiran user. DIBATALKAN. Kalau TP1 (dari resistance/confidence asli)
    # dan SL (dari support asli) menghasilkan RR jelek, itu BUKAN masalah yang
    # perlu "diperbaiki" dengan menggeser target harga — itu INFORMASI JUJUR
    # bahwa entry di harga sekarang memang kurang menguntungkan untuk saham ini.
    # RR tetap dihitung & ditampilkan apa adanya (lihat instruksi Gemini: kalau
    # RR@max di bawah 1:1, katakan terus terang, jangan disamarkan).
    tp1_pct_target = 0.05  # default minimal +5%
    daily_pace = max(0.3, price_range_pct / 10)  # pace harian dari volatilitas 10hr sendiri
    est_days_for_10pct = 10.0 / daily_pace
    horizon_confidence_high = (
        momentum_score >= 6.0
        and macd_state == "bullish"
        and decision["action_id"] in ("STRONG_BUY", "BUY_ACCUMULATE")
        and est_days_for_10pct < 5
    )
    if horizon_confidence_high:
        tp1_pct_target = 0.10

    tp1_from_pct = current_price * (1 + tp1_pct_target)
    tp_1 = max(tp1_from_pct, resistance_10d)  # murni % target atau resistance teknikal, TIDAK ADA lagi floor RR
    tp_1 = round(tp_1 / 5) * 5

    # Risk:Reward — dihitung di KEDUA ujung entry range dari TP1/SL yang SUDAH
    # final (genuine, bukan direkayasa), murni untuk INFORMASI — rasio yang
    # didapat SANGAT berbeda tergantung di mana benar-benar entry (dekat
    # entry_min = rasio jauh lebih baik daripada dekat entry_max/harga sekarang,
    # yang notabene entry paling umum untuk day trade breakout).
    risk_at_min = target_buy_min - cut_loss
    reward_at_min = tp_1 - target_buy_min
    rr_at_min = round(reward_at_min / risk_at_min, 2) if risk_at_min > 0 else None

    risk_at_max = target_buy_max - cut_loss
    reward_at_max = tp_1 - target_buy_max
    rr_at_max = round(reward_at_max / risk_at_max, 2) if risk_at_max > 0 else None

    # Defense in depth: if all price targets collapsed to the same number (can happen
    # from rounding on extremely low-priced/illiquid stocks even outside the floor-price
    # case above), the data has no genuine signal — exclude rather than show a fake range.
    if len({int(target_buy_min), int(target_buy_max), int(tp_1), int(cut_loss)}) == 1:
        return _excluded(ticker, f"target harga semuanya sama ({int(tp_1)}) — data terlalu tipis/tidak likuid untuk dihitung wajar")

    result = {
        "ticker": ticker,
        "name": company_name,
        "sector": sector,
        "company_name": company_name,  # BUGFIX: dihitung tapi tidak pernah disimpan — dibutuhkan buat fetch_company_news di pre-filter BSJP-ARA
        # Field tambahan gratis (user request — perkaya insights) — belum masuk
        # formula skor, murni informasi/siap pakai buat pengembangan berikutnya.
        "industry": industry,
        "bias_bandar": bias_bandar_label,
        "revenue_growth_pct": revenue_growth_pct,
        "roe_pct": roe_pct,
        "profit_margin_pct": profit_margin_pct,
        "forward_pe": round(forward_pe, 1) if forward_pe is not None else None,
        "peg_ratio": round(peg_ratio, 2) if peg_ratio is not None else None,
        "price": int(current_price),
        "as_of_date": as_of_date,
        "data_freshness_warning": data_freshness_warning,
        "intraday_high": intraday_high,
        "intraday_low": intraday_low,
        "pe": round(pe, 1) if pe else "N/A",
        "pb": round(pb, 1) if pb else "N/A",
        "is_unusually_low_pe": is_unusually_low_pe,
        "dividend_yield_pct": round(dividend_yield_pct, 2) if dividend_yield_pct else "N/A",
        "is_financial_distress_flag": is_financial_distress_flag,
        "is_near_price_floor": is_near_price_floor,
        "macd_state": macd_state,
        "macd_hist": round(float(current_macd_hist), 4),
        "macd_bullish_cross": macd_bullish_cross,
        "macd_bearish_cross": macd_bearish_cross,
        "macd_line_above_zero": macd_line_above_zero,
        "tight_trailing_support": tight_trailing_support,  # informational/bonus only, TIDAK menggating apa pun -- lihat catatan di atas
        "ema9_slope_pct": ema9_slope_pct,
        "trailing_support_undercut_days": trailing_support_undercut_days,
        "is_below_sma50": is_below_sma50,
        "is_below_ema21": is_below_ema21,
        "adx": round(current_adx, 1),
        "is_weak_trend": is_weak_trend,
        "bollinger_squeeze": bollinger_squeeze,  # True kalau bandwidth BB di persentil <=20 histori ~6bln — sinyal PRA-breakout, lihat catatan di atas
        "bollinger_bandwidth_percentile": bollinger_bandwidth_percentile,
        "bb_signal_note": bb_signal_note,  # near_lower_band_bounce_candidate / near_upper_band_caution / band_walking_up / band_walking_down / None
        "macd_cross_days_ago": macd_cross_days_ago,
        "macd_cross_direction": macd_cross_direction,
        "is_new_high_20d": is_new_high_20d,
        "relative_strength_vs_ihsg": relative_strength_vs_ihsg,
        "consecutive_low_volume_days": consecutive_low_volume_days,
        "dead_stock_penalty_lifted": dead_stock_penalty_lifted,
        "high_conviction": compute_high_conviction_score(ticker, {
            "price": current_price,
            "day_range_pct_10d": price_range_pct,
            "vol_ratio": vol_ratio,
            "is_below_ema21": is_below_ema21,
            "is_below_sma50": is_below_sma50,
            "value_traded": int(float(current_price * current_vol)),
        }, hist_daily=hist, action_id=decision["action_id"]),
        # PROTOTYPE (user request — riset "lower high" bull flag sebagai
        # kandidat gate HC): murni informational, BELUM menggating apa pun.
        # Reuse `hist` yang sudah di-fetch, zero cost tambahan.
        "bull_flag_pullback": compute_bull_flag_pullback_signal(hist),
        "action_id": decision["action_id"],
        "action_label_id": decision["action_label_id"],
        "action_ceiling_applied": decision["ceiling_applied"],
        "action_component_spread": decision["component_spread"],
        "rsi": round(current_rsi, 1),
        "ret_1d_pct": round(float(stock_return_today), 2) if 'stock_return_today' in locals() else None,
        "intraday_range_pct": round(float((high_prices.iloc[-1] - low_prices.iloc[-1]) / max(current_price, 1e-9) * 100), 2),
        "close_pos_day": round(float((current_price - low_prices.iloc[-1]) / max(high_prices.iloc[-1] - low_prices.iloc[-1], 1e-9)), 3),
        "value_traded": int(float(current_price * current_vol)),
        "price_vs_sma20_pct": round(float(sma_dist_pct), 2),
        "dist_to_20d_high_pct": round(float((high_20d - current_price) / max(current_price, 1e-9) * 100), 2) if len(high_prices) >= 20 else None,
        "obv_slope_5_pct": round(float((obv_series.iloc[-1] - obv_series.iloc[-6]) / max(abs(obv_series.iloc[-6]), 1e-9) * 100), 2) if len(obv_series) >= 6 else None,
        "vol_ratio": round(vol_ratio, 2),
        "cmf": round(current_cmf, 2) if current_cmf is not None else "N/A",
        "obv_divergence": obv_divergence,
        "is_overbought_caution": is_overbought_caution,
        "is_volume_spike_anomaly": is_volume_spike_anomaly,
        "chart_pattern": swing_analysis["pattern"],
        "scoring_formula_version": core.SCORING_FORMULA_VERSION,
        "breakout_level": swing_analysis["breakout_level"],
        "adaptive_scoring_used": has_adaptive_baseline,
        "day_range_pct_10d": round(price_range_pct, 1),  # transparency: how much this stock actually moved
        "targets": {
            "buy_range": f"{int(target_buy_min)} - {int(target_buy_max)}",
            "tp_1": int(tp_1),
            "cut_loss": int(cut_loss),
            "risk_reward_at_min": rr_at_min,
            "risk_reward_at_max": rr_at_max,
        },
        "scores": {
            "value": round(value_score, 1),
            "momentum": round(momentum_score, 1),
            "sentiment": round(sentiment_score, 1),
            "final": round(final_score, 1),
        },
    }

    # MBSS v2 BUGFIX (ditemukan lewat kasus nyata EKAD — /eodscan sempat
    # dijalankan ulang SETELAH harga sudah meledak hari itu, dan Yahoo
    # Finance ternyata SUDAH menyertakan bar HARI INI ke dalam histori
    # begitu tersedia — bukan cuma di akhir hari): sebelumnya
    # day_change_pct/vol_ratio_prior_day dihitung dari "bar terakhir vs
    # sebelum-terakhir" TANPA cek apakah bar terakhir itu genuinely
    # KEMARIN atau ternyata sudah HARI INI. Kalau HARI INI sudah masuk,
    # "kemarin vs kemarin-lusa" jadi salah total — ikut mengukur harga
    # SUDAH MELEDAK sebagai "histori kemarin", persis yang menolak EKAD
    # dari BSJP-ARA padahal itu pola yang seharusnya ditangkap.
    # FIX: cek tanggal bar TERAKHIR eksplisit vs tanggal hari ini — kalau
    # sama (bar hari ini sudah masuk), geser mundur 1 supaya genuinely
    # dapat kemarin vs kemarin-lusa.
    _today_wib = core.datetime.datetime.now(core.WIB).date()
    _last_bar_is_today = len(close_prices) >= 1 and close_prices.index[-1].date() == _today_wib
    _dc_offset = 1 if _last_bar_is_today else 0
    result["day_change_pct"] = (
        round((float(close_prices.iloc[-1 - _dc_offset]) - float(close_prices.iloc[-2 - _dc_offset]))
              / float(close_prices.iloc[-2 - _dc_offset]) * 100, 2)
        if len(close_prices) >= 2 + _dc_offset else None
    )
    # Field TERPISAH dari vol_ratio (yang dipakai skor momentum inti, sengaja
    # tetap "paling baru" termasuk hari ini kalau ada) — khusus buat pre-filter
    # BSJP-ARA yang genuinely butuh "kemarin", bukan "paling baru".
    result["vol_ratio_prior_day"] = (
        round(float(volumes.iloc[-1 - _dc_offset]) / (float(volumes.rolling(window=20).mean().iloc[-1 - _dc_offset]) + 1e-9), 2)
        if len(volumes) >= 21 + _dc_offset else None
    )

    risk_character = classify_risk_character(result)
    result["risk_character"] = risk_character["character"]
    result["risk_character_reason"] = risk_character["reason"]

    return result



