"""
ARCHIVED 2026-09-20 -- old live BSJP ("Beli Sore Jual Pagi") system.

This is a REFERENCE-ONLY snapshot, not meant to be imported or run. It is
the exact code that was live in engine/scanalert.py, engine/legacy_core.py,
and commands/scan.py before being replaced by the new Stage-1/Stage-2/rocket
BSJP system (engine/bsjp2.py).

WHY ARCHIVED: a dedicated derisk-research project (2026-09-17 through
2026-09-19, see memory `project_bsjp_derisk_revisit_2026_09_17.md` in
~/.claude/projects/.../memory/) found this Fase1/Fase2/pyramid design had
materially worse risk-adjusted outcomes than a redesigned system built from
scratch on the same idea (median close_stretch of the live Fase1 gate was
NEGATIVE at matched frequency: -2.40%, vs the new Stage-2 TIER1's +2.57%
mean at a similar signal rate). The new system also fixes a real bug in TP3
tier-labeling that overstated "high conviction" rates ~2x. Full research
trail, exact formulas, and the implementation TODO that led to this
archival are in that memory file's later sections (search for "STAGE-1
REVISED", "STAGE-2 REBUILT", "ARA ROCKET TAG", "NEXT SESSION TODO").

WHAT'S BELOW: verbatim extracts of the removed code, in the order they
appeared in their original files, each section labeled with its ORIGINAL
file and line range (as of the 2026-09-20 removal commit -- check git log
if you need the exact diff/commit).

  1. engine/scanalert.py            lines 718-2585   (constants + ~40 functions:
                                                        Fase1 shortlist scan,
                                                        Fase2 live recheck,
                                                        entry-ceiling/delay
                                                        compensation, ENTRY SORE
                                                        pyramid tiers, TP plan
                                                        messages)
  2. engine/legacy_core.py          lines 7583-7619  (run_bsjp_recheck_job,
                                                        run_bsjp_shortlist_scan_job)
  3. engine/legacy_core.py          lines 7663-7684  (run_bsjp_pyramid_validation_job,
                                                        run_bsjp_pyramid_d1_job)
  4. commands/scan.py               lines 2528-2637  (bsjp_screening_command,
                                                        handles both /bsjp and
                                                        the old /bsjp tp)

Also removed (not reproduced here, trivial one-liners -- see git history):
  - engine/legacy_core.py:7870  CommandHandler("bsjp", commands_scan.bsjp_screening_command)
  - engine/legacy_core.py:7963  app.job_queue.run_repeating(run_bsjp_recheck_job, ...)
  - engine/legacy_core.py:7979-7983  app.job_queue.run_repeating(run_bsjp_shortlist_scan_job, ...)
  - engine/legacy_core.py:8006-8007  app.job_queue.run_repeating(run_bsjp_pyramid_validation_job/run_bsjp_pyramid_d1_job, ...)

No BSJP state files (bsjp_shortlist_state.json, bsjp_historical_base.json,
bsjp_pyramid_state.json) existed on disk at archival time -- nothing to
preserve there.
"""

# =====================================================================
# SECTION 1 -- engine/scanalert.py, original lines 718-2585
# =====================================================================

# MBSS v2 (user request 2026-08-29, REVISI TOTAL -- unified BSJP, blend
# ARA/second-wave jadi SATU sinyal, "Beli Sore Jual Pagi"): full parameter
# sweep (ret_1d band, volume multiplier, price-near-high tightness, volume
# vs MA200 -- 162 kombinasi, validasi CLOSE-based/exit-efficiency bukan
# touch-rate, daily_2y_issi_raw.pkl chronological discovery/validation
# split) menemukan gap-reaction TERBAIK justru saat SEMUA 4 kriteria
# berikut terpenuhi bersamaan:
#   1. ret_1d > 15% (lihat revisi 2026-08-31 di bawah)
#   2. Volume > 1.5x volume kemarin
#   3. High < 1.01x harga sekarang ("clean close" -- proksi close_pos_
#      today, korelasi TERKUAT ke gap besok yg ditemukan sesi ini, 0.327
#      Spearman, jauh di atas fitur lain yg diuji)
#   4. Volume > 1.0x rata-rata volume 200 hari
# Median gap reaction (Open besok vs harga saat SEMUA 4 lolos) = 3.1%,
# n=196-208 validation, out-of-sample (angka ASLI di threshold 18%, lihat
# revisi di bawah utk angka di 15%). TIDAK ada individual WR yg justified
# (semua korelasi fitur lanjutan <0.25 setelah gate ini) -- TP1 pakai
# angka grup, bukan model per-ticker.
#
# ARSITEKTUR 2-FASE (user request, "kalau EOD-only, belum tentu open besok
# up dari close, jadi ketinggalan kereta"): entry sebenarnya SORE INI
# (SEBELUM gap terjadi), BUKAN besok pagi (setelah gap, sudah kemahalan) --
# deteksi HARUS live intraday:
#   Fase 1 (/bsjp manual, ATAU run_bsjp_shortlist_scan_auto JobQueue tiap
#     15 menit 09:00-15:50 -- lihat revisi di bawah): scan SELURUH
#     universe, simpan yg lolos 4 kriteria sbg shortlist.
#   Fase 2 (run_bsjp_recheck_once, JobQueue tiap 15 menit 09:30-15:50):
#     re-cek HANYA shortlist (murah), kirim alert final ke ticker yg MASIH
#     lolos SEMUA 4 kriteria (proyeksi makin akurat makin sore) & belum
#     pernah dialert hari ini.
# Alert = sinyal "beli SEKARANG", bukan sekadar konfirmasi pasif. TP1 =
# harga saat alert x (1+3.1%), jual besok pagi begitu tersentuh (atau,
# kalau belum sempat entry sore, besok pagi masih ada room ke TP1 -> boleh
# masuk/averaging up; sudah dekat/lewat TP1 -> jangan kejar). JANGAN tahan
# ke closing besok -- gap fade cepat (D1 CLOSE median -5.93% pd threshold
# ini, jauh lebih dalam dari gap reaction-nya sendiri).
#
# (Histori 2026-08-31/09-01/09-02: beberapa iterasi threshold ret_1d/window/
# interval lama -- SEMUA SUDAH digantikan total oleh redesign 2026-09-03 di
# bawah ini, lihat git log kalau perlu rujuk detail lama.)
# MBSS v2 (REDESIGN BESAR 2026-09-03, sesi simulasi live intraday panjang --
# ganti TOTAL mekanisme Fase1/Fase2/tier/FADING lama di bawah ini. Motivasi:
# live case TRUK/UANG hari ini kena ARA sebelum sempat teralert (formula
# volume RAW [cumulative vs FULL-DAY prev_volume/vol_ma200] terbukti TIDAK
# FAIR utk checkpoint pagi -- di 09:15 baru ~5% hari bursa berjalan, hampir
# mustahil cumulative volume tembus perbandingan FULL DAY manapun). Fix:
# PACE-ADJUSTED volume -- proyeksikan volume_so_far ke "setara full-day"
# pakai rasio menit bursa berjalan, baru dibandingkan ke full-day prev_
# volume/vol_ma200 (vol_vs_X_fair = vol_vs_X_raw * TOTAL_DAY_BARS/menit_
# elapsed). Backtest ulang TOTAL (1m riil 5 hari bursa, checkpoint table
# 73rb baris) dgn formula fair ini nunjuk gate lama SALAH TOTAL: 73% dari
# kandidat genuine yg py fair volume bagus TIDAK PERNAH lolos gate raw sama
# sekali (bukan cuma telat -- betul2 gagal, krn clean_close keburu rusak
# sebelum cumulative volume raw sempat "catch up" ke perbandingan full-day).
#
# TOTAL_DAY_BARS=320 -- angka TETAP (BUKAN bar count aktual hari itu, itu
# look-ahead, belum diketahui saat masih live), diverifikasi cocok PERSIS
# dgn struktur sesi IDX riil: Sesi 1 09:00-12:00 (180 menit) + Sesi 2
# 13:30-15:50 (140 menit) = 320. Krn produksi TIDAK fetch data 1m (cuma
# yf.download period="5d" interval="1d", baris "hari ini" live-update),
# "menit_elapsed" dihitung dari WAKTU JAM SEKARANG via kalender sesi
# (_trading_minutes_elapsed di bawah), BUKAN hitung bar 1m -- otomatis
# skip jendela istirahat siang tanpa perlu tau jam pasti tiap hari.
TOTAL_DAY_BARS = 320

# Full sweep threshold (checkpoint table, ret_1d x volume grid, 36 kombinasi)
# + simulasi ulang beberapa hari bursa nyata utk validasi -- angka final:
#
# FASE 1 (shortlist, jaring AWAL): ret_1d>12% & vol_vs_prev_fair>3x &
# vol_vs_ma200_fair>3x & clean_close<1.15. ret_1d 12% BUKAN pilihan
# sembarang -- sweep nunjuk ret_1d py THRESHOLD EFFECT tajam (Q1-Q3 quartil
# ret_1d 1-19.6% touch10 CUMA 2.8-7.2%, baru Q4 [>=19.7%] loncat ke 61.4%),
# 12% dipilih spesifik krn user minta Fase1 dikompres ke ~10-15/hari dgn
# mean gain TERBESAR pada rentang itu (n=11.2/hari, mean=+10.75%, jauh di
# atas kombinasi ret_1d rendah manapun pada rentang count yg sama).
# clean_close<=1.15 (dilonggarkan dari 1.05 -- backtest nunjuk INI GRATIS,
# n naik 11.2->12.6/hari, touch10 IDENTIK 42.9%, median malah naik +6.39->
# +7.14%, krn Fase1 SENGAJA bukan titik keputusan akhir, longgar di sini
# tidak berbahaya spt Fase2).
BSJP_SHORTLIST_RET1D_MIN_PCT = 12.0
BSJP_SHORTLIST_VOL_MULT = 3.0
BSJP_SHORTLIST_CLEAN_CLOSE_MULT = 1.15

# FADING (2026-09-03, live case GRPH -- tier3 alert TETAP bisa fade 90 menit
# kemudian, backtest tier2/3 yg faded [n=12] msh py mean touch Day+1 +7.98%
# {0% negatif}, TAPI yg TETAP kuat s.d closing [n=4] jauh lebih baik +22.29%
# {100% touch10} -- FADING bukan sinyal "sudah rugi", tapi "odds turun dari
# istimewa ke sekadar layak", worth tetap TP1 moderat bukan panik jual).
# Gate = SAMA PERSIS spt Fase1 (vol_vs_X_fair>3x & clean_close<1.15), TAPI
# ret_1d floor DILONGGARKAN ke 5% (bukan 12% penuh, DAN bukan 8-10% yg
# sempat dites -- user pilih 5% final biar buffer LEBIH LEBAR drpd entry,
# ngurangi whipsaw thd wobble ret_1d wajar setelah entry). Fire SEKALI per
# ticker per hari begitu gagal (ret_1d ATAU vol ATAU clean_close, salah
# satu SAJA sudah cukup -- vol_vs_X_fair BISA TURUN seiring waktu krn
# pace_factor mengecil begitu menit_elapsed nambah, meski volume kumulatif
# mentah terus naik).
BSJP_FADING_RET1D_MIN_PCT = 5.0

# FASE 2 (keputusan akhir "BELI SORE INI"): ret_1d>12% (SAMA dgn Fase1 --
# 2026-09-03, ditemukan gap: sebelumnya Fase2 msh pakai floor 1% warisan
# lama, PADAHAL Fase2 downstream dari Fase1 yg SUDAH 12%, jadi longgar di
# Fase2 CUMA membuka celah "ret_1d decay" [shortlist di 12%+, lalu decay
# jauh sebelum volume akhirnya tembus 4x, tapi tetap teralert krn floor
# Fase2 cuma 1%] -- diverifikasi fix INI GRATIS, n turun tipis 33->32/5hari
# tapi touch10 & mean malah naik sedikit [45.5->46.9%, +11.37->+11.50%]).
# vol_vs_X_fair>4x (naik dari 3x Fase1 -- pembeda UTAMA Fase1 vs Fase2).
# clean_close<1.025 (naik dari 1.01 ketat lama -- sweep nunjuk 1.025 hampir
# GRATIS: mean +11.50->+11.41% [rounding], TAPI n naik 31% [6.4->8.4/hari];
# 1.05 TERBUKTI kemahalan [mean turun ke +10.82%, touch10 40.4%] jadi
# TIDAK dipakai -- 1.025 titik manis).
BSJP_RECHECK_RET1D_MIN_PCT = 12.0
BSJP_RECHECK_VOL_MULT = 4.0
BSJP_CLEAN_CLOSE_MULT = 1.025

# Tier fire-emoji (2026-09-03, live case GRPH tier3): base pass = 1 fire.
# vol_vs_ma200_fair>=10x SENDIRIAN sudah cukup kuat (n=27, touch10=37.0%,
# vol_vs_prev_fair SENDIRIAN [tanpa ma200>=10x] TERBUKTI LEMAH [n=29,
# touch10 cuma 10.3%] -- makanya tier2 gate CUMA ma200, bukan "either").
# Tier3 (kedua dimensi >=10x bersamaan) py n kecil [10] tapi touch10=50.0%,
# mean+12.09% -- jelas beda kelas, dapat 3 fire.
BSJP_TIER_VOL_MULT = 10.0
BSJP_TP1_MEDIAN_GAP_PCT = 3.1  # dipakai _build_bsjp_message (alert live), TIDAK terkait tier/fading di atas
# MBSS v2 (user request 2026-09-09, delay-fetch review): BSJP py NOL proteksi
# thd gap delay yfinance (~10-15min) + waktu baca user -- current_price/
# decision_price ditampilkan sbg harga tunggal tanpa toleransi apapun, beda
# dari ENTRY PAGI (sudah py Entry Range+ceiling, commit 93d510e) & REBOUND
# (limit-order tolerance, commit 6e572c1). BSJP py thesis momentum/kontinuasi
# -- turun tidak pernah merusak thesis, jadi dipakai CEILING SATU SISI
# ("beli HANYA JIKA harga <= ceiling"), BUKAN range dua sisi spt ENTRY PAGI
# (yg py leg avg-down, BSJP fire alert tidak py itu).
#
# Backtest cepat (scratchpad bsjp_ceiling_tolerance.py): populasi kecil intraday
# (1m, n=314, gate disederhanakan TANPA filter volume pace-adjusted -- caveat
# eksplisit, BUKAN direct match popolasi produksi n=712/win71.6% di atas)
# menunjukkan entry TANPA ceiling sama sekali (harga alert dipakai apa
# adanya) mean D+1 NEGATIF (-0.88%), sedangkan ceiling di toleransi manapun
# (0.5/1.0/1.5/2.0%) membalik ke flat-positif (mean -0.05% s/d +0.15%,
# win 48-50%). Beda antar-toleransi kecil/noise-level pada sampel ini --
# dipilih 1.0% (konsisten dgn ENTRY PAGI yg sudah divalidasi lebih matang,
# posisi tengah dari sweep yg relatif datar), BUKAN diasumsikan otomatis
# sama krn instruksi eksplisit utk tidak reuse buta -- treat sbg direksional,
# bukan final-tuned spt REBOUND/ENTRY PAGI.
BSJP_CEILING_TOLERANCE_PCT = 1.0
# MBSS v2 (user request 2026-09-13 -- partial bar & delay yfinance): bar harian
# yfinance yang dipakai _fetch_bsjp_live_bar TERTINGGAL ~10-15 menit dari harga
# pasar riil (pengamatan user). Konsekuensi desain:
#   1. "current_price" yg tampil = harga per data, BUKAN harga detik ini --
#      message WAJIB menampilkan as-of time + peringatan delay.
#   2. Ceiling entry "sehat" TIDAK boleh cuma toleransi statis -- saham
#      momentum bisa sudah lari >1% dalam 10-15 menit. Toleransi dibuat
#      adaptif thd volatilitas: drift ~ N(0, atr_pct * sqrt(delay/session)),
#      dipakai BSJP_DELAY_DRIFT_SIGMA sigma. Lihat _bsjp_entry_ceiling.
BSJP_YF_DELAY_MINUTES = 12  # midpoint pengamatan 10-15 menit
BSJP_DELAY_DRIFT_SIGMA = 1.5
# MBSS v2 (user request 2026-09-13 -- tag HIGH-CONVICTION / hc di ENTRY SORE):
# riset breakout_1_5d_bsjp_hc_fidelity_v1.py + hc_early_v1.py -> tag hc
# (dist_high50>=-1 & cmf20>=0.1 & macd_hist>0) yang dihitung SAME-DAY (pakai
# close parsial) memangkas fade dari ~50% ke 6-13% dan menaikkan next-open
# 4-6x. PENTING: versi lag D-1 TIDAK ada edge (fade ~65%) -- jadi hc HARUS
# dihitung dgn data hari berjalan, di checkpoint sore. Fidelity parsial vs
# EOD ~62% (15:00) s.d 69% (15:30). Titik terbaik = 15:00-15:15 (fade 6%,
# next-open +5.0%), makanya dipasang di window ENTRY SORE 15:00-15:30.
BSJP_HC_DIST_HIGH50_MIN = -1.0
BSJP_HC_CMF_MIN = 0.1
# Range entry valid di checkpoint hc (data telat ~12m -> harga riil bisa beda):
# toleransi chasing +0.5% (user 2026-09-13); batas bawah -0.5% sbg penanda
# setup melemah kalau harga sudah turun lebih dari itu dari acuan.
BSJP_HC_CHASE_TOL_PCT = 0.5
BSJP_HC_BAND_DOWN_PCT = 0.5
# Alert hc paling cepat 15:10 -- supaya data yg dipakai (telat ~12 mnt) sudah
# represent kondisi 15:00 (bukan 14:48 kalau job jalan tepat 15:00). Window
# tutup tetap BSJP_PYRAMID_WINDOW_END (15:30).
BSJP_HC_ALERT_EARLIEST = datetime.time(15, 10)
BSJP_BASE_CACHE_VERSION = 2  # bump tiap kali field base berubah (v2 = + hc fields)
BSJP_RECHECK_WINDOW_START = datetime.time(9, 30)
BSJP_RECHECK_WINDOW_END = datetime.time(15, 50)
# MBSS v2 (user request 2026-09-02): 900s->300s -- alasan LANGSUNG terkait
# temuan presisi sesi ini: entry ASLI (harga saat checkpoint pertama lolos)
# rata-rata +0.85% s.d +1.81% DI ATAS Close(T) [snap["current_price"] pd
# saat alert fire vs closing hari itu] -- ticker biasanya SUDAH mulai fade
# dari titik lolos gate sebelum sempat dialert, cek lebih SERING mengurangi
# lag deteksi (bukan lag antar TICK, tapi lag SEJAK ticker genuinely lolos
# s.d TERDETEKSI) jadi entry lebih dekat ke harga saat genuinely lolos, BUKAN
# beberapa menit setelahnya. SENGAJA TIDAK disamakan dgn Fase 1 (TETAP 900s
# di bawah) -- Fase 1 cuma jaring kandidat awal (longgar, tidak time-
# sensitive), Fase 2 yg genuinely butuh presisi timing krn itu yg jadi
# harga alert beneran. first=250 (job registration, legacy_core.py)
# TIDAK bentrok dgn Fase 1 (first=460/900s) atau conviction sweep
# (first=100/900s) di interval BARU ini -- gcd(300,900)=300, (250-460) &
# (250-100) SAMA SEKALI TIDAK habis dibagi 300, jadi TIDAK PERNAH align.
BSJP_RECHECK_INTERVAL_SEC = 300  # 5 menit
BSJP_SHORTLIST_SCAN_WINDOW_START = datetime.time(11, 0)  # MBSS v2 (user request 2026-09-06): 09:00->11:00, "biar gak noisy" -- kandidat BARU Fase1 cukup mulai siang, carryover kemarin (shortlist dari yesterday_alerted) TETAP dicek Fase2 dari BSJP_RECHECK_WINDOW_START (09:30) spt biasa, TIDAK ikut mundur
BSJP_SHORTLIST_SCAN_INTERVAL_SEC = 900  # 15 menit -- Fase 1 TETAP, lihat catatan BSJP_RECHECK_INTERVAL_SEC knp Fase 2 dipercepat sendiri
BSJP_MIN_HISTORY_DAYS = 260  # >200 hari (MA200) + buffer hari libur/data hilang

# MBSS v2 (2026-09-03): Tier 2 "BSJP WATCH" (clean_close<=1.05 longgar +
# above_sma50) DIPENSIUNKAN -- SEPENUHNYA digantikan oleh mekanisme FADING +
# tier fire-emoji baru di atas (BSJP_FADING_RET1D_MIN_PCT dkk), yg dibangun
# dari threshold BARU (ret_1d>=12% dkk) sehingga WATCH lama (dikalibrasi ke
# threshold LAMA ret_1d>=1%) sudah tidak konsisten lagi & akan salah kaprah
# kalau tetap dipakai berdampingan. _build_bsjp_watch_message & source=
# "bsjp_watch" TIDAK dipakai lagi -- riwayat lengkap ada di git log kalau
# perlu dirujuk lagi.

# /bsjp tp -- panduan jual pre-open esok pagi. ANCHOR = CLOSING harga hari
# alert (2026-09-02, user request, live case KKES -- avg cost 111, closing
# ternyata 94-95, TP1/TP2 lama yg dihitung dari entry ALERT FIRE [109]
# terbukti TIDAK REALISTIS: backtest utk grup "faded>=5%" spt KKES nunjuk
# 0% (BUKAN rendah, NOL) ticker yg PERNAH balik ke harga entry lagi di Day+1,
# apalagi ke TP1/TP2 dari situ. User: "sesuai disiplin BSJP yg memang
# seharusnya beli di sore hari (>15:30)" -- closing HARIAN (yg sudah
# mencerminkan harga sore/akhir sesi) adalah anchor yg BENAR secara disiplin
# MAUPUN backtest, bukan harga alert-fire yg bisa jauh dari closing.
#
# Backtest presisi CORRECTED (Fase 2 ACCEPT, n=93, 5 hari bursa) dgn ANCHOR
# CLOSE (bukan entry): overall touch>=1%=74.2%/>=3%=48.4%/>=5%=34.4% (semua
# LEBIH TINGGI drpd anchor entry yg sebelumnya touch>=1%=57.0%/>=3%=40.9%/
# >=5%=33.3% -- closing adalah anchor yg lebih baik, bukan cuma lebih benar
# disiplin). Tier ret_1d_pct (segmen SAMA, floor-walk SAMA, TAPI touch
# dihitung dari CLOSE):
#   ret_1d 1-5%   (n=42): TP1=1.5% (52.4%)  TP2=3%  (31.0%)
#   ret_1d 5-10%  (n=17): TP1=3%   (58.8%)  TP2=7%  (29.4%)
#   ret_1d 10-20% (n=18): TP1=4%   (50.0%)  TP2=10% (27.8%)
#   ret_1d >=20%  (n=16): TP1=10%  (50.0%)  TP2=20% (25.0%)
#
# FADE CAP (kritis, live case KKES): drift entry->close BUKAN noise --
# ticker yg closing-nya SUDAH >=5% di bawah harga alert-fire (spt KKES,
# -12.8%) py profil BEDA SAMA SEKALI: n=11, TP1 floor-walk(50%) cuma 2.5%
# (54.5%), dan NOL kejadian tembus >=8% dari closing (bukan rendah, betul2
# nol di sampel ini). Kandidat spt ini TIDAK BOLEH dikasih TP2 stretch dari
# tier ret_1d biasa (misal tier >=20% yg nawarin TP2=20% -- itu klaim palsu
# utk ticker yg sudah crash separuh hari). Override: kalau drift<=-5%, TP1
# diturunkan ke BSJP_FADE_TP1_GAP_PCT & TP2 DIHILANGKAN SAMA SEKALI (bukan
# dikecilkan -- backtest literally 0% di atas 7-8%, menampilkan angka apa
# pun di situ menyesatkan).
BSJP_TP_TIERS = [
    # (ret1d_lo, ret1d_hi, tp1_gap_pct, tp1_hit_pct, tp2_gap_pct, tp2_hit_pct)
    (1.0, 5.0, 1.5, 52.4, 3.0, 31.0),
    (5.0, 10.0, 3.0, 58.8, 7.0, 29.4),
    (10.0, 20.0, 4.0, 50.0, 10.0, 27.8),
    (20.0, float("inf"), 10.0, 50.0, 20.0, 25.0),
]
BSJP_TP1_GAP_PCT = 2.0  # fallback (pick lama tanpa ret_1d_pct tersimpan)
BSJP_TP1_HISTORICAL_HIT_PCT = 46.2
BSJP_TP2_GAP_PCT = 6.0  # fallback (pick lama tanpa ret_1d_pct tersimpan)
BSJP_TP2_HISTORICAL_HIT_PCT = 26.9

BSJP_FADE_DRIFT_THRESHOLD_PCT = -5.0  # closing >=5% di bawah harga alert-fire
BSJP_FADE_TP1_GAP_PCT = 2.5
BSJP_FADE_TP1_HISTORICAL_HIT_PCT = 54.5


def _bsjp_tp_for_ret1d(ret1d_pct: float | None) -> tuple[float, float, float, float]:
    """Return (tp1_gap_pct, tp1_hit_pct, tp2_gap_pct, tp2_hit_pct) utk ret_1d_pct
    tertentu -- reuse BSJP_TP_TIERS, fallback ke flat BSJP_TP1_GAP_PCT/BSJP_TP2_
    GAP_PCT kalau ret1d_pct None atau di luar seluruh tier (mis. persis 1.0%).
    TIDAK menghandle fade-cap -- itu override TERPISAH di caller (butuh drift,
    bukan cuma ret1d_pct), lihat build_bsjp_tp_plan_message."""
    if ret1d_pct is not None:
        for lo, hi, tp1, tp1_hit, tp2, tp2_hit in BSJP_TP_TIERS:
            if lo <= ret1d_pct < hi:
                return tp1, tp1_hit, tp2, tp2_hit
    return BSJP_TP1_GAP_PCT, BSJP_TP1_HISTORICAL_HIT_PCT, BSJP_TP2_GAP_PCT, BSJP_TP2_HISTORICAL_HIT_PCT

STATE_FILE_BSJP = os.path.join(core.PROJECT_ROOT, "bsjp_shortlist_state.json")


def _load_bsjp_state() -> dict:
    if not os.path.exists(STATE_FILE_BSJP):
        return {}
    try:
        with open(STATE_FILE_BSJP) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_bsjp_state(state: dict):
    with open(STATE_FILE_BSJP, "w") as f:
        json.dump(state, f, indent=2)


STATE_FILE_BSJP_BASE = os.path.join(core.PROJECT_ROOT, "bsjp_historical_base.json")
_BSJP_FETCH_BATCH = 100  # chunk bulk yf.download -- hindari 1 request raksasa utk seluruh universe sekaligus


def _load_bsjp_base_cache() -> dict:
    if not os.path.exists(STATE_FILE_BSJP_BASE):
        return {}
    try:
        with open(STATE_FILE_BSJP_BASE) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_bsjp_base_cache(cache: dict):
    with open(STATE_FILE_BSJP_BASE, "w") as f:
        json.dump(cache, f)


def _fetch_bsjp_historical_base(tickers: list[str]) -> dict:
    """
    Bagian STATIS (prev_close/prev_volume/vol_ma200 -- SEMUA dari data
    SEBELUM hari ini, tidak pernah berubah sepanjang hari bursa berjalan)
    -- 260 hari, MAHAL, tapi dipanggil CUMA 1x/hari (di-cache, lihat
    _fetch_bsjp_universe_snapshot). Filter tanggal EKSPLISIT (bukan
    position-based iloc[:-1]) -- benar apapun waktu fetch ini terjadi
    (bisa saja base pertama kali dibangun SAAT market sudah buka, jadi
    baris terakhir yg di-download BISA jadi hari ini sendiri, bukan
    kemarin -- kalau pakai iloc[:-1] robotic itu keliru diasumsikan
    "prior" padahal itu hari ini).
    """
    today_date = datetime.datetime.now(core.WIB).date()
    base = {}
    for i in range(0, len(tickers), _BSJP_FETCH_BATCH):
        batch = tickers[i:i + _BSJP_FETCH_BATCH]
        symbols = [t + ".JK" for t in batch]
        data = yf.download(symbols, period=f"{BSJP_MIN_HISTORY_DAYS}d", interval="1d", group_by="ticker", threads=True, progress=False)
        for t in batch:
            sym = t + ".JK"
            try:
                d = data[sym].dropna(how="all")
            except Exception:
                continue
            prior = d[d.index.date < today_date]
            if len(prior) < 200:
                continue
            prev_close = float(prior["Close"].iloc[-1])
            prev_volume = float(prior["Volume"].iloc[-1])
            vol_ma200 = float(prior["Volume"].tail(200).mean())
            # sma50 causal (dari 50 Close SEBELUM hari ini SAJA, TIDAK ikut
            # close hari berjalan yg belum final) -- informational only sejak
            # Tier 2 "BSJP WATCH" dipensiunkan 2026-09-03, TIDAK dipakai gate
            # manapun lagi, tapi murah dihitung & tetap tersimpan di snap.
            sma50 = float(prior["Close"].tail(50).mean()) if len(prior) >= 50 else None
            # ATR14 (Wilder) dari bar SEBELUM hari ini -- dipakai _bsjp_entry_
            # ceiling utk toleransi delay adaptif thd volatilitas. Causal,
            # tidak ikut bar hari berjalan yg belum final.
            atr_pct = None
            if len(prior) >= 15:
                _pc = prior["Close"]
                _tr = pd.concat([
                    prior["High"] - prior["Low"],
                    (prior["High"] - _pc.shift(1)).abs(),
                    (prior["Low"] - _pc.shift(1)).abs(),
                ], axis=1).max(axis=1)
                _atr = _tr.ewm(alpha=1 / 14, adjust=False).mean().iloc[-1]
                if prev_close > 0 and pd.notna(_atr):
                    atr_pct = float(_atr / prev_close * 100)
            if prev_close <= 0:
                continue
            # Tag hc (2026-09-13): high50 SEBELUM hari ini (dist_high50 versi
            # parquet = rolling(50).max() TERMASUK hari ini, jadi parsial pakai
            # max(high50_prior, high_so_far)) + window pendek utk cmf20/macd
            # parsial. Disimpan sbg list kecil (bukan Series) supaya cache JSON
            # tetap ringan.
            high50_prior = float(prior["High"].tail(50).max()) if len(prior) >= 50 else None
            hc_closes = [float(x) for x in prior["Close"].tail(60)]
            hc_highs = [float(x) for x in prior["High"].tail(20)]
            hc_lows = [float(x) for x in prior["Low"].tail(20)]
            hc_vols = [float(x) for x in prior["Volume"].tail(20)]
            base[t] = {"prev_close": prev_close, "prev_volume": prev_volume, "vol_ma200": vol_ma200,
                       "sma50": sma50, "atr_pct": atr_pct, "high50_prior": high50_prior,
                       "hc_closes": hc_closes, "hc_highs": hc_highs, "hc_lows": hc_lows, "hc_vols": hc_vols}
        if i + _BSJP_FETCH_BATCH < len(tickers):
            time.sleep(0.5)
    return base


def _fetch_bsjp_live_bar(tickers: list[str]) -> dict:
    """
    Bagian LIVE (HARI INI saja) -- period KECIL (5 hari, bukan 260),
    dipanggil TIAP kali _fetch_bsjp_universe_snapshot jalan (beda dari
    historical base yg di-cache 1x/hari). Inilah fetch yg genuinely perlu
    fresh tiap siklus -- Fase 1 tiap 15 menit, Fase 2 tiap 5 menit (2026-
    09-02, lihat BSJP_RECHECK_INTERVAL_SEC) -- payload-nya jauh lebih kecil
    drpd base.
    """
    today_date = datetime.datetime.now(core.WIB).date()
    live = {}
    for i in range(0, len(tickers), _BSJP_FETCH_BATCH):
        batch = tickers[i:i + _BSJP_FETCH_BATCH]
        symbols = [t + ".JK" for t in batch]
        data = yf.download(symbols, period="5d", interval="1d", group_by="ticker", threads=True, progress=False)
        for t in batch:
            sym = t + ".JK"
            try:
                d = data[sym].dropna(how="all")
            except Exception:
                continue
            today_rows = d[d.index.date == today_date]
            if today_rows.empty:
                continue
            today = today_rows.iloc[-1]
            live[t] = {
                "current_price": float(today["Close"]),
                "high_so_far": float(today["High"]),
                "low_so_far": float(today["Low"]) if not pd.isna(today["Low"]) else None,
                "open_so_far": float(today["Open"]) if not pd.isna(today["Open"]) else None,
                "volume_so_far": float(today["Volume"]) if not pd.isna(today["Volume"]) else 0.0,
            }
        if i + _BSJP_FETCH_BATCH < len(tickers):
            time.sleep(0.5)
    return live


def _fetch_bsjp_closing_prices(picks: list[dict]) -> dict:
    """
    /bsjp tp -- closing HARIAN (bukan harga live) utk HARI ALERT ASLI tiap
    pick, dipakai sbg anchor TP1/TP2 (lihat catatan panjang di atas BSJP_
    TP_TIERS knp closing, bukan harga alert-fire).

    BUGFIX 2026-09-02 (live case: closing yg tampil PERSIS SAMA dgn cut_loss
    utk ICON/JARR/KKES -- ketahuan krn user bandingkan langsung): versi
    PERTAMA pakai pick_date sbg tanggal target fetch -- SALAH, pick_date
    SELALU 1 hari SEBELUM hari alert beneran (get_current_trading_day_close_
    marker() dipanggil SAAT lock, SEBELUM 16:30 WIB, jadi masih mundur 1
    hari -- lihat catatan panjang di build_bsjp_tp_plan_message). Fetch
    "closing pick_date" scr harfiah = fetch closing HARI SEBELUM alert =
    PERSIS prev_close/cut_loss yg SUDAH tersimpan -- itu sebabnya closing
    yg tampil identik dgn cut_loss.

    Fix: JANGAN coba rekonstruksi tanggal alert dari pick_date (rapuh
    lewat weekend/libur -- +1 hari kalender BISA salah kalau alert hari
    Senin [pick_date jadi Jumat, bukan Minggu]). Sebagai gantinya, cocokkan
    NILAI cut_loss (prev_close) yg SUDAH tersimpan tiap pick thd baris
    daily bars -- begitu ketemu baris yg cocok, ambil baris SETELAHNYA
    (posisi berikutnya di data yfinance, yg SUDAH otomatis skip weekend/
    libur dgn sendirinya) sbg closing hari alert yg SEBENARNYA. Robust
    thd off-by-one apa pun penyebabnya, tidak perlu tau exact tanggal.
    """
    tickers = sorted({p["ticker"] for p in picks})
    cut_loss_by_ticker = {}
    for p in picks:
        cl = p.get("cut_loss")
        if cl:
            cut_loss_by_ticker[p["ticker"]] = cl  # sama utk semua pick ticker yg sama di hari yg sama

    closes = {}
    for i in range(0, len(tickers), _BSJP_FETCH_BATCH):
        batch = tickers[i:i + _BSJP_FETCH_BATCH]
        symbols = [t + ".JK" for t in batch]
        data = yf.download(symbols, period="10d", interval="1d", group_by="ticker", threads=True, progress=False)
        for t in batch:
            sym = t + ".JK"
            prev_close = cut_loss_by_ticker.get(t)
            if not prev_close:
                continue
            try:
                d = data[sym].dropna(how="all")
            except Exception:
                continue
            if d.empty:
                continue
            close_series = d["Close"]
            match_idx = None
            for idx in range(len(close_series) - 1, -1, -1):  # cari dari PALING BARU mundur -- ambil match terdekat
                val = close_series.iloc[idx]
                if not pd.isna(val) and abs(val - prev_close) < 0.5:
                    match_idx = idx
                    break
            if match_idx is None or match_idx + 1 >= len(close_series):
                continue  # prev_close tidak ketemu di window 10 hari, ATAU match adalah baris TERAKHIR (belum ada hari sesudahnya di data)
            next_close = close_series.iloc[match_idx + 1]
            if pd.isna(next_close):
                continue
            closes[t] = float(next_close)
        if i + _BSJP_FETCH_BATCH < len(tickers):
            time.sleep(0.5)
    return closes


def _trading_minutes_elapsed(now_time: datetime.time) -> int:
    """
    Menit bursa yg SUDAH LEWAT sejak buka (09:00), MELOMPATI istirahat siang
    OTOMATIS -- dipakai pace-adjustment volume (lihat catatan panjang di
    atas TOTAL_DAY_BARS). Sesi 1 09:00-12:00 (180 menit) + Sesi 2 13:30-
    15:50 (140 menit) = 320 = TOTAL_DAY_BARS, dihitung dari KALENDER SESI
    (bukan hitung bar 1m -- produksi tidak fetch data 1m utk BSJP, cuma
    yf.download interval="1d" yg baris "hari ini"-nya live-update).
    """
    s1_start, s1_end = datetime.time(9, 0), datetime.time(12, 0)
    s2_start, s2_end = datetime.time(13, 30), datetime.time(15, 50)

    def to_min(t: datetime.time) -> int:
        return t.hour * 60 + t.minute

    if now_time <= s1_start:
        return 0
    if now_time <= s1_end:
        return to_min(now_time) - to_min(s1_start)
    if now_time < s2_start:
        return to_min(s1_end) - to_min(s1_start)  # sesi 1 penuh, msh istirahat
    if now_time <= s2_end:
        return (to_min(s1_end) - to_min(s1_start)) + (to_min(now_time) - to_min(s2_start))
    return TOTAL_DAY_BARS  # sesi 2 sudah tutup, hari bursa penuh


def _bsjp_hc_partial(b: dict, l: dict) -> dict:
    """
    Tag hc (2026-09-13) dihitung dari close PARSIAL hari berjalan + histori
    harian -- lihat catatan panjang di atas BSJP_HC_DIST_HIGH50_MIN. Return
    {dist_high50_partial, cmf20_partial, macd_hist_partial, hc_tag}, semua
    None-safe (data kurang -> hc_tag False, TIDAK pernah error).
    """
    price = l.get("current_price")
    high_so_far = l.get("high_so_far")
    low_so_far = l.get("low_so_far")
    vol_so_far = l.get("volume_so_far") or 0.0
    h50p = b.get("high50_prior")
    dist = None
    if price and (h50p or high_so_far):
        base_high = max(h50p or 0.0, high_so_far or 0.0)
        if base_high > 0:
            dist = (price - base_high) / base_high * 100

    cmf = None
    hs = b.get("hc_highs") or []
    ls = b.get("hc_lows") or []
    cs = b.get("hc_closes") or []
    vs = b.get("hc_vols") or []
    if len(hs) >= 19 and len(ls) >= 19 and len(cs) >= 19 and len(vs) >= 19 \
            and low_so_far is not None and high_so_far is not None and price:
        ph = list(hs[-19:]) + [high_so_far]
        pl = list(ls[-19:]) + [low_so_far]
        pc = list(cs[-19:]) + [price]
        pv = list(vs[-19:]) + [vol_so_far]
        mfm = [((c - lo) - (h - c)) / (h - lo + 1e-9) for h, lo, c in zip(ph, pl, pc)]
        mfv = [m * v for m, v in zip(mfm, pv)]
        cmf = sum(mfv) / (sum(pv) + 1e-9)

    macd_h = None
    closes = (list(b.get("hc_closes") or []) + [price]) if price else list(b.get("hc_closes") or [])
    if len(closes) >= 27:
        s = pd.Series(closes, dtype=float)
        macd = s.rolling(12).mean() - s.rolling(26).mean()
        sig = macd.ewm(span=9, adjust=False).mean()
        v = (macd - sig).iloc[-1]
        macd_h = float(v) if pd.notna(v) else None

    hc = bool(dist is not None and dist >= BSJP_HC_DIST_HIGH50_MIN
              and cmf is not None and cmf >= BSJP_HC_CMF_MIN
              and macd_h is not None and macd_h > 0)
    return {"dist_high50_partial": dist, "cmf20_partial": cmf,
            "macd_hist_partial": macd_h, "hc_tag": hc}


def _fetch_bsjp_universe_snapshot(tickers: list[str]) -> dict:
    """
    MBSS v2 (user request 2026-08-31, live case: panggilan KEDUA /bsjp di
    hari yg sama masih sangat lambat, SSH ikut lag): root cause -- SEBELUM
    ini, tiap panggilan re-fetch 260 hari PENUH utk SELURUH universe,
    padahal 259 dari 260 hari itu STATIS (tidak berubah selama hari bursa
    berjalan). Sekarang dipecah dua: bagian statis (_fetch_bsjp_historical_
    base) di-cache 1x/hari (bsjp_historical_base.json), bagian live
    (_fetch_bsjp_live_bar, period=5 hari SAJA) fetch fresh tiap panggilan.
    Panggilan PERTAMA hari itu masih mahal (base belum ada), tapi panggilan
    KEDUA dst (termasuk auto-scan tiap 15 menit) jauh lebih murah -- cuma
    fetch bagian live yg kecil, base-nya reuse dari cache.
    """
    today = _today_str()
    base_cache = _load_bsjp_base_cache()
    # Bump versi kalau field base berubah (2026-09-13: + high50_prior/hc_* utk
    # tag hc) -- tanpa ini, cache hari yg sama (format lama) tetap dipakai dan
    # hc_tag selalu False sampai cache rebuild besok.
    if (base_cache.get("trading_day_marker") != today or not base_cache.get("base")
            or base_cache.get("base_version") != BSJP_BASE_CACHE_VERSION):
        base = _fetch_bsjp_historical_base(tickers)
        base_cache = {"trading_day_marker": today, "base_version": BSJP_BASE_CACHE_VERSION, "base": base}
        _save_bsjp_base_cache(base_cache)
    else:
        base = base_cache["base"]
        missing = [t for t in tickers if t not in base]
        if missing:
            extra_base = _fetch_bsjp_historical_base(missing)
            base.update(extra_base)
            base_cache["base"] = base
            _save_bsjp_base_cache(base_cache)

    live = _fetch_bsjp_live_bar(tickers)

    # Pace-adjustment (2026-09-03) -- lihat catatan panjang di atas
    # TOTAL_DAY_BARS. bars_elapsed dihitung SEKALI per panggilan (SAMA utk
    # semua ticker, krn semua di-cek pada jam yg SAMA saat ini).
    now_wib = datetime.datetime.now(core.WIB)
    bars_elapsed = max(_trading_minutes_elapsed(now_wib.time()), 1)
    pace_factor = TOTAL_DAY_BARS / bars_elapsed
    # Partial-bar & delay yfinance (2026-09-13): estimasi waktu DATA (bukan
    # waktu fetch) = sekarang - BSJP_YF_DELAY_MINUTES, utk ditampilkan di
    # message. Satu nilai utk semua ticker (semua di-fetch pada waktu sama).
    data_asof = (now_wib - datetime.timedelta(minutes=BSJP_YF_DELAY_MINUTES)).strftime("%H:%M")

    snap = {}
    for t in tickers:
        b = base.get(t)
        l = live.get(t)
        if not b or not l:
            continue
        sma50 = b.get("sma50")
        prev_volume = b["prev_volume"]
        vol_ma200 = b["vol_ma200"]
        volume_so_far = l["volume_so_far"]
        vol_vs_prev_fair = (volume_so_far / prev_volume) * pace_factor if prev_volume else None
        vol_vs_ma200_fair = (volume_so_far / vol_ma200) * pace_factor if vol_ma200 else None
        # Partial-bar (2026-09-13): close_pos dari rentang HARI INI sejauh ini
        # + gap dari open hari ini -- dipakai utk konteks entry alert & validasi
        # kekuatan closing parsial (versi EOD-nya sudah divalidasi di riset).
        low_so_far = l.get("low_so_far")
        open_so_far = l.get("open_so_far")
        _rng = (l["high_so_far"] - low_so_far) if (low_so_far is not None) else 0.0
        close_pos_partial = (
            max(0.0, min(1.0, (l["current_price"] - low_so_far) / _rng)) if _rng and _rng > 0 else None
        )
        gap_from_open_pct = (
            (l["current_price"] / open_so_far - 1) * 100 if open_so_far else None
        )
        hc = _bsjp_hc_partial(b, l)
        snap[t] = {
            "current_price": l["current_price"], "high_so_far": l["high_so_far"], "volume_so_far": volume_so_far,
            "low_so_far": low_so_far, "open_so_far": open_so_far,
            "close_pos_partial": close_pos_partial, "gap_from_open_pct": gap_from_open_pct,
            "prev_close": b["prev_close"], "prev_volume": prev_volume, "vol_ma200": vol_ma200,
            "atr_pct": b.get("atr_pct"), "data_asof": data_asof,
            "ret_1d_pct": (l["current_price"] / b["prev_close"] - 1) * 100,
            "above_sma50": (l["current_price"] > sma50) if sma50 else None,
            "vol_vs_prev_fair": vol_vs_prev_fair, "vol_vs_ma200_fair": vol_vs_ma200_fair,
            **hc,
        }
    return snap


def _check_bsjp_criteria(
    snap: dict,
    ret1d_min: float,
    vol_mult: float,
    clean_close_mult: float,
) -> bool:
    """
    4 kriteria BSJP (REDESIGN 2026-09-03 -- lihat catatan panjang di atas
    TOTAL_DAY_BARS/BSJP_SHORTLIST_RET1D_MIN_PCT dkk utk alasan lengkap &
    riset pendukung). SEMUA parameter WAJIB eksplisit dari caller sekarang
    (Fase1/FADING/Fase2 py angka BEDA-BEDA, tidak ada lagi "default global"
    yg bisa salah kaprah kepakai lintas fase) -- vol_mult SATU angka utk
    KEDUA dimensi volume (vol_vs_prev_fair DAN vol_vs_ma200_fair, keduanya
    HARUS lolos, tidak ada toleransi). Pakai field _fair (pace-adjusted,
    lihat _fetch_bsjp_universe_snapshot) -- BUKAN volume_so_far/prev_volume
    mentah lagi (itu yg terbukti TIDAK FAIR utk checkpoint pagi, live case
    TRUK/UANG kena ARA sebelum sempat teralert).
    """
    ret_1d = snap.get("ret_1d_pct")
    vol_vs_prev_fair = snap.get("vol_vs_prev_fair")
    vol_vs_ma200_fair = snap.get("vol_vs_ma200_fair")
    current_price = snap.get("current_price")
    high_so_far = snap.get("high_so_far")
    if None in (ret_1d, vol_vs_prev_fair, vol_vs_ma200_fair, current_price, high_so_far):
        return False
    if ret_1d <= ret1d_min:
        return False
    if vol_vs_prev_fair <= vol_mult or vol_vs_ma200_fair <= vol_mult:
        return False
    if not current_price or high_so_far >= clean_close_mult * current_price:
        return False
    return True


def _bsjp_tier(snap: dict) -> int:
    """
    Tier fire-emoji (2026-09-03) -- lihat catatan panjang di atas
    BSJP_TIER_VOL_MULT. 1=base pass, 2=vol_vs_ma200_fair>=10x SENDIRIAN
    (vol_vs_prev_fair SENDIRIAN TERBUKTI LEMAH, sengaja TIDAK dipakai),
    3=KEDUA dimensi >=10x bersamaan (n kecil tapi touch10=50%, mean+12%).
    """
    vvp = snap.get("vol_vs_prev_fair") or 0
    vvm = snap.get("vol_vs_ma200_fair") or 0
    if vvm >= BSJP_TIER_VOL_MULT:
        return 3 if vvp >= BSJP_TIER_VOL_MULT else 2
    return 1


def _bsjp_entry_ceiling(snap: dict) -> tuple[float, float]:
    """
    Batas harga entry yg masih "sehat" mengingat data yfinance telat
    BSJP_YF_DELAY_MINUTES (lihat catatan di atas BSJP_YF_DELAY_MINUTES).
    Return (ceiling_price, tol_pct). Toleransi = max(statis, sigma * atr_pct *
    sqrt(delay/session)) -- adaptif thd volatilitas; saham ATR tinggi dapat
    allowance lebih lebar krn wajar bergerak >1% dalam 10-15 menit. Kalau
    atr_pct tak tersedia (data kurang), fallback ke toleransi statis.
    """
    price = snap.get("current_price")
    if not price:
        return 0.0, BSJP_CEILING_TOLERANCE_PCT
    atr_pct = snap.get("atr_pct")
    tol = BSJP_CEILING_TOLERANCE_PCT
    if atr_pct:
        drift = BSJP_DELAY_DRIFT_SIGMA * atr_pct * ((BSJP_YF_DELAY_MINUTES / TOTAL_DAY_BARS) ** 0.5)
        tol = max(tol, drift)
    return price * (1 + tol / 100.0), tol


def _build_bsjp_message(ticker: str, snap: dict, tier: int) -> str:
    price_raw = snap["current_price"]
    current_price = _idx_round_tick(price_raw)
    ceiling_raw, tol = _bsjp_entry_ceiling(snap)
    ceiling = _idx_round_tick(ceiling_raw)
    tp1_price = _idx_round_tick(price_raw * (1 + BSJP_TP1_MEDIAN_GAP_PCT / 100.0))
    vol_vs_prev = snap["vol_vs_prev_fair"]
    vol_vs_ma200 = snap["vol_vs_ma200_fair"]
    cpp = snap.get("close_pos_partial")
    gapo = snap.get("gap_from_open_pct")
    asof = snap.get("data_asof") or "-"
    fire = "🔥" * tier
    label = "BUY POWER >=10x" if tier > 1 else "BUY POWER KUAT"
    lines = [
        "BSJP",
        f"{fire} {ticker} {label} — vol {vol_vs_prev:.1f}x kemarin, {vol_vs_ma200:.1f}x avg 200hr, harga dekat high hari ini",
        f"⚠️ Data per {asof} WIB (yfinance telat ~10-15 mnt — harga riil bisa sudah beda).",
        f"Harga (data): {current_price:,.0f}",
    ]
    ctx = []
    if cpp is not None:
        ctx.append(f"posisi closing parsial {cpp*100:.0f}% rentang hari")
    if gapo is not None:
        ctx.append(f"{gapo:+.1f}% dari open")
    if ctx:
        lines.append("Konteks: " + ", ".join(ctx) + ".")
    lines += [
        f"Batas entry SEHAT: <= {ceiling:,.0f} (+{tol:.1f}%, sudah termasuk allowance delay).",
        "Harga riil di atas batas itu? SKIP, jangan dikejar.",
        f"TP1 (estimasi): {tp1_price:,.0f}",
        "BELI SORE INI. Jual besok pagi begitu TP1 tersentuh.",
    ]
    return "\n".join(lines)


def _bsjp_hc_entry_band(snap: dict) -> tuple[float, float]:
    """
    Range harga entry yang masih valid di checkpoint hc (2026-09-13). Acuan =
    current_price DATA (telat ~12 mnt); ceiling = +BSJP_HC_CHASE_TOL_PCT
    (toleransi chasing, user request 0.5%), floor = -BSJP_HC_BAND_DOWN_PCT
    (kalau harga sudah turun lebih dari ini dari acuan -> setup melemah).
    """
    price = snap.get("current_price") or 0.0
    return (price * (1 - BSJP_HC_BAND_DOWN_PCT / 100.0),
            price * (1 + BSJP_HC_CHASE_TOL_PCT / 100.0))


def _build_bsjp_hc_entry_message(ticker: str, snap: dict) -> str:
    """
    Alert ENTRY SORE -- HIGH CONVICTION (tag hc, 2026-09-13). Dikirim di window
    ENTRY SORE 15:00-15:30 (titik terbaik 15:00-15:15; alert paling cepat
    ~15:10-15:12 krn yfinance telat ~12 mnt). Range entry ditegaskan dgn
    toleransi chasing +BSJP_HC_CHASE_TOL_PCT.
    """
    price_raw = snap.get("current_price") or 0.0
    price = _idx_round_tick(price_raw)
    floor_raw, ceil_raw = _bsjp_hc_entry_band(snap)
    floor = _idx_round_tick_floor(floor_raw)
    ceil = _idx_round_tick_ceil(ceil_raw)
    tp1 = _idx_round_tick(price_raw * (1 + BSJP_TP1_MEDIAN_GAP_PCT / 100.0))
    asof = snap.get("data_asof") or "-"
    d = snap.get("dist_high50_partial")
    cmf = snap.get("cmf20_partial")
    macd = snap.get("macd_hist_partial")
    parts = [
        "⚡ ENTRY SORE — HIGH CONVICTION",
        f"{ticker} — dekat high 50hr + money-flow & MACD positif (hari ini)",
        f"⚠️ Data per {asof} WIB (yfinance telat ~10-15 mnt).",
        f"Harga acuan (data): {price:,.0f}",
        f"Range entry valid: {floor:,.0f} – {ceil:,.0f} "
        f"(chasing maks +{BSJP_HC_CHASE_TOL_PCT:.1f}%, setup melemah kalau < {floor:,.0f})",
        "Harga riil di ATAS range? SKIP, jangan dikejar.",
        f"TP1 (estimasi): {tp1:,.0f}",
        "BELI SORE INI. Jual besok pagi begitu TP1 tersentuh.",
    ]
    comp = []
    if d is not None:
        comp.append(f"dist high50 {d:+.1f}%")
    if cmf is not None:
        comp.append(f"CMF {cmf:+.2f}")
    if macd is not None:
        comp.append(f"MACD hist {macd:+.2f}")
    if comp:
        parts.insert(4, "Komponen hc: " + ", ".join(comp) + ".")
    return "\n".join(parts)


def _build_bsjp_fading_message(ticker: str, snap: dict, reasons: list[str]) -> str:
    """
    FADING (2026-09-03) -- lihat catatan panjang di atas BSJP_FADING_
    RET1D_MIN_PCT. Fire SEKALI begitu ticker yg SUDAH teralert tidak lagi
    lolos gate FADING (ret_1d>5% & vol_vs_X_fair>3x & clean_close<1.15).
    BUKAN sinyal "sudah rugi" -- backtest: grup yg fade dari tier2/3 msh
    py mean touch Day+1 positif (+7.98%, 0% negatif di sampel), CUMA odds-
    nya turun dari istimewa ke sekadar layak (vs +22.29% yg TETAP kuat).
    """
    current_price = snap["current_price"]
    return (
        f"BSJP FADING\n"
        f"🔻 {ticker} — sinyal melemah ({', '.join(reasons)})\n"
        f"Harga sekarang: {current_price:,.0f}\n"
        f"⚠️ BUKAN berarti rugi -- historis msh py peluang gain moderat Day+1, TAPI odds winner besar jauh menurun. Cocok TP1 saja, jangan tahan berharap TP2/stretch."
    )


async def run_bsjp_shortlist_scan(tickers: list[str]) -> list[dict]:
    """
    FASE 1 -- scan SELURUH universe thd 4 kriteria, simpan yg lolos sbg
    shortlist utk di-recheck Fase 2. Return list kandidat (dict lengkap
    termasuk snapshot). Dipanggil DUA cara (MBSS v2, user request
    2026-08-31): manual (/bsjp) DAN otomatis (run_bsjp_shortlist_scan_auto,
    JobQueue tiap 15 menit 09:00-15:50) -- lihat catatan BSJP_SHORTLIST_
    SCAN_INTERVAL_SEC di atas utk alasan kenapa manual-only tidak cukup.
    """
    snapshot = await _fetch_with_timeout(_fetch_bsjp_universe_snapshot, tickers, timeout=150, default={})
    passed = [
        {"ticker": t, **snap} for t, snap in snapshot.items()
        if _check_bsjp_criteria(snap, ret1d_min=BSJP_SHORTLIST_RET1D_MIN_PCT, vol_mult=BSJP_SHORTLIST_VOL_MULT, clean_close_mult=BSJP_SHORTLIST_CLEAN_CLOSE_MULT)
    ]

    state = _load_bsjp_state()
    today = _today_str()
    if state.get("trading_day_marker") != today:
        # MBSS v2 (user request 2026-09-01, live case "nailunn" BSJP list --
        # 14/14 checkable ticker SEMUA sudah lolos 4 kriteria PENUH KEMARIN
        # sebelum direkomendasikan lagi hari ini): backtest 2thn/576 ISSI
        # (chronological 70/30, validasi, n=347): ticker lolos BSJP PENUH
        # hari T py touch>=3/5/6/10% hari T+1 = 69.5/65.4/63.4/58.2% (vs
        # baseline 29.5/17.3/13.6/6.0%) -- edge KUAT, berdiri sendiri TANPA
        # syarat tambahan hari T+1. User EKSPLISIT: BUKAN continuation/tahan
        # posisi lebih lama (nama "Beli Sore Jual Pagi" py 1-malam hold yg
        # tidak boleh diubah) -- cukup pastikan ticker kemarin TIDAK
        # TERLEWAT dari kandidat pool hari ini. Tidak ada threshold/alert/
        # exit BARU -- ticker ini masuk shortlist SAMA PERSIS spt kandidat
        # lain, tetap lewat Fase 2 recheck STRICT & alert final standar yg
        # SAMA SEKALI tidak berubah ("BELI SORE INI, jual besok pagi").
        yesterday_alerted = state.get("alerted", []) if state.get("trading_day_marker") else []
        state = {
            "trading_day_marker": today,
            "shortlist": sorted(set(yesterday_alerted)),
            "alerted": [],
            "faded": [],  # 2026-09-03 -- ticker yg SUDAH alerted lalu gagal gate FADING, TIDAK dicek lagi hari itu
            "tier": {},  # 2026-09-03 -- {ticker: tier_terakhir} utk deteksi UPGRADE tier
            "last_ret1d": {},  # 2026-09-04 -- {ticker: ret_1d checkpoint TERAKHIR} utk "BSJP MENGUAT"
        }
    state["shortlist"] = sorted(set(state.get("shortlist", [])) | {c["ticker"] for c in passed})
    state.setdefault("alerted", [])
    state.setdefault("faded", [])
    state.setdefault("tier", {})
    state.setdefault("last_ret1d", {})
    _save_bsjp_state(state)
    return passed


def _build_bsjp_shortlist_new_message(ticker: str, snap: dict) -> str:
    vol_vs_prev = snap["vol_vs_prev_fair"]
    vol_vs_ma200 = snap["vol_vs_ma200_fair"]
    return (
        f"BSJP SHORTLIST (Fase 1 otomatis)\n"
        f"🌆 {ticker} baru masuk shortlist — {snap['current_price']:,.0f} ({snap['ret_1d_pct']:+.1f}%)\n"
        f"Vol {vol_vs_prev:.1f}x kemarin | {vol_vs_ma200:.1f}x MA200\n"
        f"⚠️ BUKAN alert entry -- akan di-recheck live 09:30-15:50 WIB, alert final (dgn TP1) baru dikirim kalau MASIH lolos semua kriteria."
    )


async def run_bsjp_shortlist_scan_auto() -> dict:
    """
    MBSS v2 (user request 2026-08-31, live case BALI/KICI lolos shortlist
    tapi sudah +25%/+24.4% -- practically ARA, tidak bisa dibeli lagi):
    Fase 1 OTOMATIS, JobQueue tiap BSJP_SHORTLIST_SCAN_INTERVAL_SEC
    (no-op murah di luar jendela 09:00-15:50 WIB). SEBELUMNYA Fase 1 CUMA
    manual-trigger (/bsjp) -- kalau user baru cek siang/sore, kandidat yg
    nembus threshold pagi2 sudah lanjut lari ke ARA sebelum sempat
    ketahuan (lihat riset room-to-ARA di atas). Checking lebih SERING &
    lebih PAGI menambah room riil, INDEPENDEN dari BSJP_RET1D_MIN_PCT
    itu sendiri -- dua perbaikan ini saling melengkapi, bukan alternatif.

    Kirim notifikasi HANYA utk ticker BARU masuk shortlist (dedup thd
    shortlist SEBELUM scan ini dimulai) -- bukan re-broadcast seluruh
    shortlist tiap 15 menit (spam).
    """
    summary = {"skipped_reason": None, "scanned": 0, "new_shortlist": 0, "instant_phase2": 0}
    now_wib = datetime.datetime.now(core.WIB)
    if now_wib.weekday() >= 5:
        summary["skipped_reason"] = "weekend"
        return summary
    if not (BSJP_SHORTLIST_SCAN_WINDOW_START <= now_wib.time() <= BSJP_RECHECK_WINDOW_END):
        summary["skipped_reason"] = "outside_window"
        return summary
    if await asyncio.to_thread(core.is_idx_market_holiday_today):
        summary["skipped_reason"] = "holiday"
        return summary
    if not is_scan_alert_enabled():
        summary["skipped_reason"] = "toggled_off"
        return summary

    import engine.nightly as nightly_engine  # import lokal -- hindari circular import di level modul
    scored = nightly_engine.load_daily_scan_cache()
    if not scored:
        summary["skipped_reason"] = "no_cache"
        return summary
    universe = sorted(scored.keys())

    state_before = _load_bsjp_state()
    today = _today_str()
    shortlist_before = set(state_before.get("shortlist", [])) if state_before.get("trading_day_marker") == today else set()

    passed = await run_bsjp_shortlist_scan(universe)
    summary["scanned"] = len(universe)

    new_tickers = sorted({c["ticker"] for c in passed} - shortlist_before)
    passed_by_ticker = {c["ticker"]: c for c in passed}
    # MBSS v2 (user request 2026-09-04, "BSJP MENGUAT" -- backtest: kandidat
    # yg ret_1d MASIH naik antar 2 checkpoint Fase1 pertama py touch10 Day+1
    # ~2x drpd yg sudah plateau [~50% vs ~28%, n=53, sampel kecil treat sbg
    # directional]): fire TIAP siklus (BUKAN sekali) selama masih naik
    # drpd checkpoint SEBELUMNYA (bukan drpd checkpoint pertama) -- ticker
    # yg sudah shortlisted TAPI belum alerted, dibandingkan ke last_ret1d
    # tersimpan, lalu last_ret1d SELALU di-update ke nilai skrg (baseline
    # SEGAR utk siklus berikutnya) terlepas naik/tidak.
    still_shortlisted = (set(passed_by_ticker) & shortlist_before) - set(new_tickers)
    if new_tickers or still_shortlisted:
        # MBSS v2 (user request 2026-09-04, live case TRUK/UANG -- kejar
        # entry ARA-bound berarti masuk secepat mungkin, bukan nunggu siklus
        # Fase2 berikutnya [s.d 5 menit lagi]): snapshot Fase1 SUDAH punya
        # SEMUA field yg dibutuhkan _check_bsjp_criteria (ret_1d_pct,
        # vol_vs_X_fair, high_so_far/current_price) -- cek Fase2 LANGSUNG di
        # snapshot yg SAMA, ZERO fetch tambahan. Kalau ticker BARU masuk
        # shortlist TERNYATA juga sudah lolos Fase2 saat itu juga, kirim
        # alert Fase2 PENUH (dgn tier) LANGSUNG drpd cuma pesan shortlist
        # biasa -- shortlist message "BUKAN alert entry" jadi TIDAK relevan
        # lagi utk kasus ini, redundant kalau dikirim berdua.
        bot = _get_shared_bot()
        state = _load_bsjp_state()
        alerted_list = state.setdefault("alerted", [])
        tier_map = state.setdefault("tier", {})
        last_ret1d = state.setdefault("last_ret1d", {})
        already_alerted = set(alerted_list)
        fired = []
        state_dirty = False
        for t in new_tickers:
            snap = passed_by_ticker[t]
            if t not in already_alerted and _check_bsjp_criteria(
                snap, ret1d_min=BSJP_RECHECK_RET1D_MIN_PCT, vol_mult=BSJP_RECHECK_VOL_MULT,
                clean_close_mult=BSJP_CLEAN_CLOSE_MULT,
            ):
                tier = _bsjp_tier(snap)
                msg = _build_bsjp_message(t, snap, tier)
                if bot is not None:
                    await core.safe_reply(bot, msg, chat_id=core.TELEGRAM_CHAT_ID)
                else:
                    print(f"[NO TELEGRAM TOKEN] {msg}")
                alerted_list.append(t)
                tier_map[t] = tier
                state_dirty = True
                _record_bsjp_pyramid_entry(t, snap)
                fired.append({
                    "ticker": t, "current_price": snap["current_price"],
                    "ret_1d_pct": snap["ret_1d_pct"], "tier": tier,
                    "targets": {"tp_1": snap["current_price"] * (1 + BSJP_TP1_MEDIAN_GAP_PCT / 100.0), "cut_loss": snap["prev_close"]},
                })
                summary["new_shortlist"] += 1
                summary["instant_phase2"] = summary.get("instant_phase2", 0) + 1
                continue

            msg = _build_bsjp_shortlist_new_message(t, snap)
            if bot is not None:
                await core.safe_reply(bot, msg, chat_id=core.TELEGRAM_CHAT_ID)
            else:
                print(f"[NO TELEGRAM TOKEN] {msg}")
            summary["new_shortlist"] += 1
            last_ret1d[t] = snap["ret_1d_pct"]
            state_dirty = True

        for t in still_shortlisted:
            if t in already_alerted:
                continue  # sudah lulus ke Fase2 -- continuous monitoring (run_bsjp_recheck_once) yg ambil alih
            snap = passed_by_ticker[t]
            prev_ret1d = last_ret1d.get(t)
            current_ret1d = snap["ret_1d_pct"]
            if prev_ret1d is not None and current_ret1d > prev_ret1d:
                msg = (
                    f"BSJP MENGUAT\n"
                    f"🚀 {t} masih naik — ret_1d {prev_ret1d:.1f}% → {current_ret1d:.1f}% (checkpoint sebelumnya)\n"
                    f"Harga sekarang: {snap['current_price']:,.0f}\n"
                    f"⚠️ Masih di Fase 1, belum lolos Fase2."
                )
                if bot is not None:
                    await core.safe_reply(bot, msg, chat_id=core.TELEGRAM_CHAT_ID)
                else:
                    print(f"[NO TELEGRAM TOKEN] {msg}")
                summary["menguat"] = summary.get("menguat", 0) + 1
            last_ret1d[t] = current_ret1d
            state_dirty = True

        if state_dirty:
            _save_bsjp_state(state)
        if fired:
            try:
                await asyncio.to_thread(core.lock_daily_daytrade_picks, fired, "bsjp")
            except Exception as e:
                print(f"⚠️ Gagal mengunci picks BSJP (instant Fase2) untuk /winrate: {e}")

    print(f"✅ BSJP shortlist scan (auto): {summary['scanned']} ticker discan, {summary['new_shortlist']} baru masuk shortlist "
          f"({summary.get('instant_phase2', 0)} langsung lolos Fase2 juga, {summary.get('menguat', 0)} MENGUAT).")
    return summary


async def run_bsjp_recheck_once() -> dict:
    """
    FASE 2 (JobQueue, tiap BSJP_RECHECK_INTERVAL_SEC, no-op murah di luar
    jendela BSJP_RECHECK_WINDOW_START-END): REDESIGN 2026-09-03 -- CONTINUOUS
    MONITORING s.d closing (BUKAN lagi stop-setelah-alert-pertama spt
    sebelumnya). Tiap ticker di shortlist diperlakukan beda tergantung
    status HARI INI:
      - Belum alerted, belum faded: cek Fase2 (ret_1d>12%, vol_vs_X_fair>4x,
        clean_close<1.025) -> ALERT (tier 1/2/3, lihat _bsjp_tier).
      - SUDAH alerted, belum faded: cek gate FADING (ret_1d>5%, vol_vs_X_
        fair>3x, clean_close<1.15 -- SAMA persis Fase1 tapi ret_1d floor
        dilonggarkan, lihat BSJP_FADING_RET1D_MIN_PCT). Gagal -> FADING
        (fire SEKALI, ticker berhenti dicek sisa hari itu). Masih lolos ->
        cek UPGRADE tier (vol_vs_ma200_fair/vol_vs_prev_fair naik lintas
        ambang 10x sejak alert pertama).
      - Sudah faded: skip total, tidak dicek lagi hari itu.
    """
    now_wib = datetime.datetime.now(core.WIB)
    if not (BSJP_RECHECK_WINDOW_START <= now_wib.time() <= BSJP_RECHECK_WINDOW_END):
        return {"checked": 0, "alerted": 0, "faded": 0, "upgraded": 0}

    state = _load_bsjp_state()
    today = _today_str()
    if state.get("trading_day_marker") != today:
        return {"checked": 0, "alerted": 0, "faded": 0, "upgraded": 0}  # belum /bsjp hari ini

    alerted_already = set(state.get("alerted", []))
    faded_already = set(state.get("faded", []))
    to_check = [t for t in state.get("shortlist", []) if t not in faded_already]
    if not to_check:
        return {"checked": 0, "alerted": 0, "faded": 0, "upgraded": 0}

    snapshot = await _fetch_with_timeout(_fetch_bsjp_universe_snapshot, to_check, default={})
    bot = _get_shared_bot()

    n_alerted = 0
    n_faded = 0
    n_upgraded = 0
    alerted_list = state.setdefault("alerted", [])
    faded_list = state.setdefault("faded", [])
    tier_map = state.setdefault("tier", {})
    fired = []

    for t in to_check:
        snap = snapshot.get(t)
        if not snap:
            continue

        if t in alerted_already:
            fading_ok = _check_bsjp_criteria(
                snap, ret1d_min=BSJP_FADING_RET1D_MIN_PCT, vol_mult=BSJP_SHORTLIST_VOL_MULT,
                clean_close_mult=BSJP_SHORTLIST_CLEAN_CLOSE_MULT,
            )
            if not fading_ok:
                reasons = []
                ret_1d = snap.get("ret_1d_pct")
                vvp = snap.get("vol_vs_prev_fair")
                vvm = snap.get("vol_vs_ma200_fair")
                clean = (snap["high_so_far"] / snap["current_price"]) if snap.get("current_price") else None
                if ret_1d is not None and ret_1d <= BSJP_FADING_RET1D_MIN_PCT:
                    reasons.append(f"ret_1d turun ke {ret_1d:+.1f}%")
                if vvp is not None and vvp <= BSJP_SHORTLIST_VOL_MULT:
                    reasons.append(f"vol vs kemarin turun ke {vvp:.1f}x")
                if vvm is not None and vvm <= BSJP_SHORTLIST_VOL_MULT:
                    reasons.append(f"vol vs avg 200hr turun ke {vvm:.1f}x")
                if clean is not None and clean >= BSJP_SHORTLIST_CLEAN_CLOSE_MULT:
                    reasons.append(f"sudah {100*(clean-1):.1f}% dari high")
                msg = _build_bsjp_fading_message(t, snap, reasons or ["gagal gate FADING"])
                if bot is not None:
                    await core.safe_reply(bot, msg, chat_id=core.TELEGRAM_CHAT_ID)
                else:
                    print(f"[NO TELEGRAM TOKEN] {msg}")
                faded_list.append(t)
                n_faded += 1
                continue

            # Masih lolos gate FADING -- cek upgrade tier.
            new_tier = _bsjp_tier(snap)
            old_tier = tier_map.get(t, 1)
            if new_tier > old_tier:
                fire = "🔥" * new_tier
                upgrade_price = _idx_round_tick(snap["current_price"])
                upgrade_ceiling = _idx_round_tick(snap["current_price"] * (1 + BSJP_CEILING_TOLERANCE_PCT / 100.0))
                msg = (
                    f"BSJP UPGRADE\n"
                    f"{fire} {t} naik ke tier {new_tier} — vol {snap['vol_vs_prev_fair']:.1f}x kemarin, "
                    f"{snap['vol_vs_ma200_fair']:.1f}x avg 200hr\n"
                    f"Harga sekarang: {upgrade_price:,.0f}\n"
                    f"Beli HANYA JIKA harga masih <= {upgrade_ceiling:,.0f}. Sudah tembus? SKIP, jangan dikejar."
                )
                if bot is not None:
                    await core.safe_reply(bot, msg, chat_id=core.TELEGRAM_CHAT_ID)
                else:
                    print(f"[NO TELEGRAM TOKEN] {msg}")
                tier_map[t] = new_tier
                n_upgraded += 1
            continue

        # Belum pernah alerted -- cek Fase2.
        if _check_bsjp_criteria(
            snap, ret1d_min=BSJP_RECHECK_RET1D_MIN_PCT, vol_mult=BSJP_RECHECK_VOL_MULT,
            clean_close_mult=BSJP_CLEAN_CLOSE_MULT,
        ):
            tier = _bsjp_tier(snap)
            msg = _build_bsjp_message(t, snap, tier)
            if bot is not None:
                await core.safe_reply(bot, msg, chat_id=core.TELEGRAM_CHAT_ID)
            else:
                print(f"[NO TELEGRAM TOKEN] {msg}")
            alerted_list.append(t)
            tier_map[t] = tier
            n_alerted += 1
            _record_bsjp_pyramid_entry(t, snap)
            # MBSS v2 (user request -- lock HANYA saat alert BENERAN fire, bukan
            # di shortlist Fase 1 yg belum tentu konfirmasi): cut_loss pakai
            # prev_close (thesis "buy power" gagal kalau balik ke bawah closing
            # kemarin) -- proksi wajar, bukan angka backtest terpisah.
            fired.append({
                "ticker": t, "current_price": snap["current_price"],
                # ret_1d_pct (2026-09-02) -- mengalir otomatis ke feature_
                # snapshot lock_daily_daytrade_picks (sudah ada field ini di
                # sana), dipakai build_bsjp_tp_plan_message utk pilih tier
                # TP1/TP2 individualized -- lihat BSJP_TP_TIERS. tier (2026-
                # 09-03) -- informational, ditampilkan di /bsjp tp.
                "ret_1d_pct": snap["ret_1d_pct"], "tier": tier,
                "targets": {"tp_1": snap["current_price"] * (1 + BSJP_TP1_MEDIAN_GAP_PCT / 100.0), "cut_loss": snap["prev_close"]},
            })

    _save_bsjp_state(state)
    if fired:
        try:
            await asyncio.to_thread(core.lock_daily_daytrade_picks, fired, "bsjp")
        except Exception as e:
            print(f"⚠️ Gagal mengunci picks BSJP untuk /winrate: {e}")
    return {"checked": len(to_check), "alerted": n_alerted, "faded": n_faded, "upgraded": n_upgraded}


# ═══════════════════ ENTRY SORE — Sequential Tier1-3 @ 15:00 + D+1 exit ═══════════════════
# MBSS v2 (user request 2026-09-06, full redesign confirmed setelah user
# eksplisit pilih "full redesign" atas pertanyaan konfirmasi cakupan):
# lapisan BARU di ATAS mekanisme Fase2 live yg SUDAH ADA (alert real-time +
# gate FADING + UPGRADE tier, run_bsjp_recheck_once di atas -- TIDAK
# disentuh/diganti sama sekali, tetap jalan persis spt sebelumnya). Lapisan
# ini MENAMBAHKAN klasifikasi "trajektori sejak entry Fase2" di titik
# keputusan 15:00 WIB (riset session 2026-09-04/05, terkunci di memory
# project_bsjp_sequential_pyramid_tier1_4_2026_09_04.md):
#
#   TIER1 "masih menguat kuat" : delta_ret1d>0 DAN delta_vvm>0 (keduanya naik
#                                  sejak entry Fase2)
#   TIER2 "masih menguat (ret1d)": delta_ret1d>0 SENDIRIAN
#   TIER3 "masih menguat (vol)"  : delta_vvm>0 SENDIRIAN
#   rest (TIDAK dialert)         : keduanya turun -- user request eksplisit
#                                  "cukup tier 1-3 saja" (Tier4/rest DIHAPUS
#                                  dari desain asli, TERMASUK mekanisme
#                                  filler Fase1-only-never-Fase2 -- tidak
#                                  dibangun sama sekali). Backtest 18-hari:
#                                  "rest" TETAP positive-EV (+5.70%/80.3%
#                                  win) tapi terlemah -- gate FADING yg
#                                  SUDAH ADA (live) tetap jadi safety net
#                                  utknya, bukan mekanisme baru ini.
#
# Backtest 18-hari (n lebih besar, MENGGANTIKAN angka 8-hari yg overstated):
# TIER1 +12.0%/69.2% win, TIER2/3 +8.3%/78.8% win.
#
# State file TERPISAH LAGI (bsjp_pyramid_state.json, BUKAN bsjp_shortlist_
# state.json) -- SENGAJA, krn bsjp_shortlist_state.json di-reset TIAP HARI
# (run_bsjp_shortlist_scan, trading_day_marker mismatch -> state baru),
# padahal posisi D+1 exit (avg-down/TP/SL) lapisan ini HARUS bertahan
# lintas hari (dari 15:00 hari alert s.d closing D+1). Baseline entry
# Fase2 (dicatat _record_bsjp_pyramid_entry, dipanggil dari KEDUA titik
# alert-fire di atas) & posisi aktif SEMUA hidup di file terpisah ini.
BSJP_PYRAMID_DECISION_TIME = datetime.time(15, 0)
BSJP_PYRAMID_WINDOW_END = datetime.time(15, 30)  # MBSS v2 (user request 2026-09-07): dilebarkan dari 15:15 -- job re-run tiap siklus 3 menit dlm jendela ini, kandidat yg blm resolve terus dicek ulang sampai window tutup
BSJP_ARA_TOLERANCE_PCT = 0.5  # ret_1d dlm 0.5pp dari ceiling ARA dianggap "kena ARA"
BSJP_ARA_NEAR_HIGH_PCT = 0.5  # current_price dlm 0.5% dari high_so_far hari itu

# Avg-down 3-tier (external doc's mechanic, riset 2026-09-05: lift win rate
# +15 s.d +21pp vs tanpa avg-down) -- tier2/tier3 checked HANYA dari D+1
# 09:00 (BUKAN sore yg sama), sesuai disiplin "beli sore, kelola besok".
BSJP_D1_TIER2_DIP_PCT = -2.0
BSJP_D1_TIER3_DIP_PCT = -5.0
BSJP_D1_SPLIT = (0.4, 0.3, 0.3)  # tier1(15:00)/tier2(-2%)/tier3(-5%) porsi modal

BSJP_D1_TIER1_TP_PCT = 12.0
BSJP_D1_TIER1_SL_PCT = 6.0

BSJP_D1_TIER23_TP_S1_PCT = 12.0        # flat, Sesi 1 D+1 (09:00-11:59)
BSJP_D1_TIER23_TP_S2_START_PCT = 6.0   # decay linear -> floor, Sesi 2 D+1 (13:30-15:49)
BSJP_D1_TIER23_TP_S2_FLOOR_PCT = 2.0
BSJP_D1_TIER23_SL_S1_PCT = 8.0         # flat, Sesi 1
BSJP_D1_TIER23_SL_S2_FLOOR_PCT = 2.0   # decay linear 8%->2%, Sesi 2

BSJP_D1_SESSION1_START = datetime.time(9, 0)
BSJP_D1_SESSION1_END = datetime.time(11, 59, 59)
BSJP_D1_SESSION2_START = datetime.time(13, 30)
BSJP_D1_SESSION2_END = datetime.time(15, 49)

STATE_FILE_BSJP_PYRAMID = os.path.join(core.PROJECT_ROOT, "bsjp_pyramid_state.json")


def _load_bsjp_pyramid_state() -> dict:
    if not os.path.exists(STATE_FILE_BSJP_PYRAMID):
        return {}
    try:
        with open(STATE_FILE_BSJP_PYRAMID) as f:
            return json.load(f)
    except Exception:
        return {}


def _save_bsjp_pyramid_state(state: dict):
    with open(STATE_FILE_BSJP_PYRAMID, "w") as f:
        json.dump(state, f, indent=2)


def _record_bsjp_pyramid_entry(ticker: str, snap: dict):
    """
    Simpan baseline Fase2-entry (ret_1d/vol_vs_ma200_fair/harga) SEKALI
    saat alert Fase2 PERTAMA fire -- dipakai run_bsjp_pyramid_validation_
    once utk hitung delta di titik keputusan 15:00. Dipanggil dari KEDUA
    titik alert-fire (run_bsjp_shortlist_scan_auto's instant-Fase2 path &
    run_bsjp_recheck_once's path normal) -- try/except sendiri, KEGAGALAN
    DI SINI TIDAK BOLEH mengganggu alert Fase2 asli yg SUDAH terkirim.
    """
    try:
        state = _load_bsjp_pyramid_state()
        entries = state.setdefault("entries", {})
        entries[ticker] = {
            "date": _today_str(), "entry_ret1d": snap.get("ret_1d_pct"),
            "entry_vvm": snap.get("vol_vs_ma200_fair"), "entry_price": snap.get("current_price"),
        }
        _save_bsjp_pyramid_state(state)
    except Exception as e:
        print(f"⚠️ Entry Sore: gagal simpan baseline pyramid utk {ticker}: {e}")


def _bsjp_ara_ceiling_pct(prev_close: float) -> float:
    """Band auto-reject-atas IDX (external doc, lebih presisi drpd heuristik plateau)."""
    if prev_close < 200:
        return 35.0
    if prev_close < 5000:
        return 25.0
    return 20.0


def _bsjp_is_ara_locked(snap: dict) -> bool:
    """
    ARA-lock exclusion (memory: "ticker sudah beku di ceiling ARA saat/
    sebelum titik keputusan -- untradeable, tidak ada likuiditas di harga
    itu"). Pakai band ARA riil IDX (bukan heuristik plateau 1m yg butuh
    data granular yg tak tersedia di sumber data BSJP) -- ret_1d SUDAH
    dekat ceiling band DAN harga sekarang dekat high hari ini.
    """
    prev_close = snap.get("prev_close")
    current_price = snap.get("current_price")
    high_so_far = snap.get("high_so_far")
    ret_1d = snap.get("ret_1d_pct")
    if not prev_close or not current_price or not high_so_far or ret_1d is None:
        return False
    ceiling = _bsjp_ara_ceiling_pct(prev_close)
    near_ceiling = ret_1d >= ceiling - BSJP_ARA_TOLERANCE_PCT
    near_high = current_price >= high_so_far * (1 - BSJP_ARA_NEAR_HIGH_PCT / 100.0)
    return near_ceiling and near_high


def _bsjp_pyramid_tier(entry: dict, snap: dict) -> str | None:
    """
    Return 'TIER1'/'TIER2'/'TIER3', atau None ('rest' -- TIDAK dialert,
    lihat catatan panjang di atas blok ENTRY SORE knp Tier4/rest dihapus).
    """
    entry_ret1d, entry_vvm = entry.get("entry_ret1d"), entry.get("entry_vvm")
    current_ret1d, current_vvm = snap.get("ret_1d_pct"), snap.get("vol_vs_ma200_fair")
    if None in (entry_ret1d, entry_vvm, current_ret1d, current_vvm):
        return None
    delta_ret1d = current_ret1d - entry_ret1d
    delta_vvm = current_vvm - entry_vvm
    if delta_ret1d > 0 and delta_vvm > 0:
        return "TIER1"
    if delta_ret1d > 0:
        return "TIER2"
    if delta_vvm > 0:
        return "TIER3"
    return None


_BSJP_TIER_LABEL = {
    "TIER1": "masih menguat kuat (ret_1d & volume dua-duanya naik sejak entry)",
    "TIER2": "masih menguat (ret_1d naik, volume melandai sejak entry)",
    "TIER3": "masih menguat (volume naik, ret_1d melandai sejak entry)",
}


def _bsjp_d1_tp_sl_for_tier1(avg_cost: float) -> tuple[float, float]:
    return avg_cost * (1 + BSJP_D1_TIER1_TP_PCT / 100.0), avg_cost * (1 - BSJP_D1_TIER1_SL_PCT / 100.0)


def _bsjp_d1_decay_pct(now_time: datetime.time, start_pct: float, floor_pct: float) -> float:
    """
    Sesi 1 D+1 (09:00-11:59): FLAT start_pct. Sesi 2 D+1 (13:30-15:49):
    decay LINEAR start_pct->floor_pct. Di luar kedua sesi (istirahat siang
    11:59-13:30): masih flat start_pct (belum masuk sesi 2). Dipakai utk
    TIER2/3 TP (start=12,floor=2) & SL (start=8,floor=2) -- riset 2026-
    09-05: decay LEBIH BAIK drpd hybrid/lompatan tunggal.
    """
    if now_time < BSJP_D1_SESSION2_START:
        return start_pct
    total_s2 = (
        datetime.datetime.combine(datetime.date.today(), BSJP_D1_SESSION2_END)
        - datetime.datetime.combine(datetime.date.today(), BSJP_D1_SESSION2_START)
    ).total_seconds()
    elapsed = (
        datetime.datetime.combine(datetime.date.today(), min(now_time, BSJP_D1_SESSION2_END))
        - datetime.datetime.combine(datetime.date.today(), BSJP_D1_SESSION2_START)
    ).total_seconds()
    frac = max(0.0, min(1.0, elapsed / total_s2)) if total_s2 > 0 else 1.0
    return start_pct - (start_pct - floor_pct) * frac


def _bsjp_d1_tp_sl_for_tier23(avg_cost: float, now_time: datetime.time) -> tuple[float, float]:
    # TP Sesi2 decay dari 6% (BUKAN dari 12% flat Sesi1) -- lihat konstanta TP_S2_START_PCT.
    if now_time >= BSJP_D1_SESSION2_START:
        tp_pct = _bsjp_d1_decay_pct(now_time, BSJP_D1_TIER23_TP_S2_START_PCT, BSJP_D1_TIER23_TP_S2_FLOOR_PCT)
    else:
        tp_pct = BSJP_D1_TIER23_TP_S1_PCT
    sl_pct = _bsjp_d1_decay_pct(now_time, BSJP_D1_TIER23_SL_S1_PCT, BSJP_D1_TIER23_SL_S2_FLOOR_PCT)
    return avg_cost * (1 + tp_pct / 100.0), avg_cost * (1 - sl_pct / 100.0)


def _render_bsjp_pyramid_message(positions: list[dict]) -> str:
    """
    Satu pesan per hari alert, di-edit-in-place sepanjang D+1 (avg-down/TP/SL/masuk sesi 2).

    Ceiling +BSJP_CEILING_TOLERANCE_PCT dari decision_price ditambahkan
    (2026-09-09) HANYA sbg instruksi pesan utk status OPEN -- posisi itu
    SUDAH lolos validasi 15:00 & avg_cost/TP/SL SUDAH dikunci di
    decision_price utk konsistensi dgn backtest (BUKAN diubah di sini,
    lihat run_bsjp_pyramid_validation_once) -- ceiling ini cuma membantu
    user yg baru sempat baca pesan beberapa menit kemudian tahu batas
    wajar eksekusi manual, bukan mengubah mekanisme tracking posisi.
    """
    header = (
        "ENTRY SORE\n"
        "Posisi lolos validasi 15:00 -- avg down maks 2x besok (tier2 -2%, tier3 -5%)\n"
        "TIER1: TP+12%/SL-6% tetap sepanjang D+1\n"
        "TIER2/3: TP & SL menurun dari sesi 1 ke sesi 2 D+1"
    )
    marker = {
        "OPEN": "⏳ Menunggu D+1",
        "SESSION2": "🔄 Masuk Sesi 2",
        "TP": "✅ TP TERCAPAI",
        "SL": "✅ SL KENA",
        "FORCE_EXIT": "🔚 FORCE EXIT (closing D+1)",
    }
    blocks = []
    for i, p in enumerate(positions, start=1):
        avgdown_note = ""
        if p.get("filled_tier3"):
            avgdown_note = " (avg down tier2+tier3 TERISI)"
        elif p.get("filled_tier2"):
            avgdown_note = " (avg down tier2 TERISI)"
        tp = _idx_round_tick(p["tp"])
        sl = _idx_round_tick(p["sl"])
        if p["status"] == "SESSION2":
            blocks.append(
                f"TICK {i}\n{p['ticker']} — {p['tier']}\n"
                f"🔄 Masuk Sesi 2\nTP : {tp:,.0f} | SL : {sl:,.0f}"
            )
        else:
            decision_price = _idx_round_tick(p["decision_price"])
            ceiling_line = ""
            if p["status"] == "OPEN":
                ceiling = _idx_round_tick(p["decision_price"] * (1 + BSJP_CEILING_TOLERANCE_PCT / 100.0))
                ceiling_line = f"Beli HANYA JIKA harga masih <= {ceiling:,.0f}. Sudah tembus? SKIP, jangan dikejar.\n"
            blocks.append(
                f"TICK {i}\n"
                f"{p['ticker']} — {p['tier']}{' ⚡HC' if p.get('hc') else ''} ({_BSJP_TIER_LABEL.get(p['tier'], '')})\n"
                f"Harga keputusan (15:00): {decision_price:,.0f}\n"
                f"{ceiling_line}"
                f"TP : {tp:,.0f}\n"
                f"SL : {sl:,.0f}\n"
                f"{marker.get(p['status'], p['status'])}{avgdown_note}"
            )
    return header + "\n\n" + "\n\n".join(blocks)


async def _bsjp_pyramid_send_new_message(text: str) -> int | None:
    bot = _get_shared_bot()
    if bot is None:
        print(f"[NO TELEGRAM TOKEN] {text}")
        return None
    for attempt in range(2):
        try:
            sent = await bot.send_message(chat_id=core.TELEGRAM_CHAT_ID, text=text)
            return sent.message_id
        except Exception as e:
            print(f"⚠️ Entry Sore pyramid: gagal kirim pesan awal (attempt {attempt + 1}): {e}")
            if attempt == 0:
                await asyncio.sleep(3)
    return None


async def _bsjp_pyramid_edit_message(state: dict):
    if not state.get("message_id"):
        return
    bot = _get_shared_bot()
    if bot is None:
        return
    try:
        await bot.edit_message_text(
            chat_id=state.get("chat_id", core.TELEGRAM_CHAT_ID), message_id=state["message_id"],
            text=_render_bsjp_pyramid_message(list(state["positions"].values())),
        )
    except Exception as e:
        print(f"⚠️ Entry Sore pyramid: gagal edit pesan: {e}")


BSJP_PYRAMID_FINAL_CALL_BUFFER_MIN = 4  # siklus dlm N menit terakhir jendela dianggap "kesempatan terakhir" -> kirim ringkasan final rest


async def run_bsjp_pyramid_validation_once() -> dict:
    """
    MBSS v2 (redesign 2026-09-07, user request -- jendela dilebarkan 15:00-
    15:15 -> 15:00-15:30 DAN dijalankan BERULANG tiap siklus [bukan sekali
    lalu kunci total] supaya status pyramid ikut ter-update kalau ada
    ticker yg tadinya 'rest' lalu momentumnya balik naik dlm jendela itu.

    Per SIKLUS (job ini dipanggil tiap 180s oleh JobQueue, no-op murah di
    luar jendela): HANYA proses ticker yg BELUM resolve hari ini (belum py
    posisi Tier1-3 aktif) -- yg sudah resolve TIDAK dicek ulang (posisi
    sudah dikunci di decision_price saat itu, re-cek ulang akan salah
    kaprah menimpa decision_price). Begitu ADA yg baru resolve ke Tier1-3,
    pesan TICK di-update SEKARANG JUGA (bukan nunggu window tutup). Ringkasan
    "masih rest" utk sisa yg TAK KUNJUNG resolve HANYA dikirim SEKALI, di
    kesempatan terakhir jendela (BSJP_PYRAMID_FINAL_CALL_BUFFER_MIN menit
    sblm window tutup) -- supaya TIDAK spam pesan serupa tiap 3 menit
    selama 30 menit penuh.
    """
    summary = {"skipped_reason": None, "positions": 0}
    now_wib = datetime.datetime.now(core.WIB)
    if now_wib.weekday() >= 5:
        summary["skipped_reason"] = "weekend"
        return summary
    if not (BSJP_PYRAMID_DECISION_TIME <= now_wib.time() <= BSJP_PYRAMID_WINDOW_END):
        summary["skipped_reason"] = "outside_window"
        return summary
    if await asyncio.to_thread(core.is_idx_market_holiday_today):
        summary["skipped_reason"] = "holiday"
        return summary
    if not is_scan_alert_enabled():
        summary["skipped_reason"] = "toggled_off"
        return summary

    today = _today_str()
    pyramid_state = _load_bsjp_pyramid_state()
    if pyramid_state.get("validation_date") != today:
        # Hari baru -- reset tracking siklus (posisi Tier1-3 LAMA di
        # "positions" dibiarkan apa adanya, itu urusan run_bsjp_pyramid_d1_
        # once, BUKAN dihapus di sini).
        pyramid_state["validation_date"] = today
        pyramid_state["message_id"] = None
        pyramid_state["chat_id"] = None
        pyramid_state["final_summary_sent"] = False
        pyramid_state["hc_alerted"] = []  # 2026-09-13 -- ticker yg sudah dapat alert hc hari ini

    bsjp_state = _load_bsjp_state()
    if bsjp_state.get("trading_day_marker") != today:
        summary["skipped_reason"] = "no_bsjp_today"
        return summary
    alerted = set(bsjp_state.get("alerted", [])) - set(bsjp_state.get("faded", []))
    entries = pyramid_state.get("entries", {})
    positions = pyramid_state.setdefault("positions", {})
    hc_alerted = pyramid_state.setdefault("hc_alerted", [])
    already_resolved_today = {t for t, p in positions.items() if p.get("decision_date") == today}
    all_candidates_today = [t for t in alerted if entries.get(t, {}).get("date") == today]
    pending = [t for t in all_candidates_today if t not in already_resolved_today]

    is_final_call = now_wib.time() >= (
        datetime.datetime.combine(datetime.date.today(), BSJP_PYRAMID_WINDOW_END)
        - datetime.timedelta(minutes=BSJP_PYRAMID_FINAL_CALL_BUFFER_MIN)
    ).time()

    if not all_candidates_today:
        summary["skipped_reason"] = "no_candidates"
        _save_bsjp_pyramid_state(pyramid_state)
        return summary

    # Fetch snapshot utk SEMUA kandidat hari ini (bukan cuma pending) supaya
    # tag hc tetap bisa dialertkan utk kandidat yg sudah resolve tier lebih awal.
    snapshot = await _fetch_with_timeout(_fetch_bsjp_universe_snapshot, all_candidates_today, timeout=150, default={})

    # Alert hc (2026-09-13) -- independen dari tier, sekali per ticker, hanya
    # setelah data sore representatif (>= BSJP_HC_ALERT_EARLIEST). hc = filter
    # kualitas kandidat (fade 17%->0% di riset), bukan tier.
    if now_wib.time() >= BSJP_HC_ALERT_EARLIEST:
        bot_hc = _get_shared_bot()
        for t in all_candidates_today:
            snap = snapshot.get(t)
            if not snap or t in hc_alerted or not snap.get("hc_tag"):
                continue
            hc_msg = _build_bsjp_hc_entry_message(t, snap)
            if bot_hc is not None:
                await core.safe_reply(bot_hc, hc_msg, chat_id=core.TELEGRAM_CHAT_ID)
            else:
                print(f"[NO TELEGRAM TOKEN] {hc_msg}")
            hc_alerted.append(t)
            summary["hc_alerted"] = summary.get("hc_alerted", 0) + 1

    if not pending:
        summary["skipped_reason"] = "all_resolved"
        _save_bsjp_pyramid_state(pyramid_state)
        return summary

    new_positions = []
    still_pending = []
    excluded_final = []  # HANYA diisi kalau is_final_call (utk ringkasan penutup)
    for t in pending:
        snap = snapshot.get(t)
        if not snap:
            still_pending.append((t, "data tidak tersedia"))
            continue
        if _bsjp_is_ara_locked(snap):
            excluded_final.append((t, "ARA-lock (untradeable)"))
            continue  # ARA-lock TIDAK akan berubah -> keluarkan dari pending seterusnya
        tier = _bsjp_pyramid_tier(entries[t], snap)
        if tier is None:
            still_pending.append((t, "melandai sejak alert Fase2 (rest)"))
            continue
        decision_price = snap["current_price"]
        positions[t] = {
            "ticker": t, "tier": tier, "decision_date": today, "decision_price": decision_price,
            "avg_cost": decision_price, "filled_tier2": False, "filled_tier3": False,
            "status": "OPEN", "hc": bool(snap.get("hc_tag")),
            "tp": (_bsjp_d1_tp_sl_for_tier1(decision_price)[0] if tier == "TIER1"
                   else _bsjp_d1_tp_sl_for_tier23(decision_price, BSJP_D1_SESSION1_START)[0]),
            "sl": (_bsjp_d1_tp_sl_for_tier1(decision_price)[1] if tier == "TIER1"
                   else _bsjp_d1_tp_sl_for_tier23(decision_price, BSJP_D1_SESSION1_START)[1]),
        }
        new_positions.append(positions[t])

    # Pesan TICK di-update SEGERA begitu ada yg baru resolve (bukan nunggu
    # window tutup). PENTING: scope ke posisi HARI INI SAJA (decision_date
    # == today) -- BUKAN pakai _bsjp_pyramid_edit_message (itu render SEMUA
    # isi state["positions"], termasuk carryover D+1 dari hari SEBELUMNYA
    # yg msh OPEN & py message_id TERPISAH milik run_bsjp_pyramid_d1_once
    # sendiri; kalau dicampur di sini pesan validasi hari ini jadi salah
    # isi posisi lama).
    if new_positions:
        positions_today = [p for t, p in positions.items() if p.get("decision_date") == today]
        text = _render_bsjp_pyramid_message(positions_today)
        bot = _get_shared_bot()
        if pyramid_state.get("message_id") and bot is not None:
            try:
                await bot.edit_message_text(chat_id=pyramid_state["chat_id"], message_id=pyramid_state["message_id"], text=text)
            except Exception as e:
                print(f"⚠️ Entry Sore pyramid: gagal edit pesan validasi: {e}")
        else:
            pyramid_state["message_id"] = await _bsjp_pyramid_send_new_message(text)
            pyramid_state["chat_id"] = core.TELEGRAM_CHAT_ID

    # Ringkasan "masih rest" -- HANYA sekali, di kesempatan terakhir jendela.
    if is_final_call and not pyramid_state.get("final_summary_sent"):
        never_resolved = still_pending + excluded_final
        if never_resolved:
            lines = [f"ENTRY SORE — Validasi 15:00-15:30 selesai: {len(never_resolved)} kandidat TIDAK PERNAH lolos ke TIER1-3."]
            for t, reason in never_resolved:
                lines.append(f"  {t} — {reason}")
            bot = _get_shared_bot()
            status_msg = "\n".join(lines)
            if bot is not None:
                try:
                    await core.safe_reply(bot, status_msg, chat_id=core.TELEGRAM_CHAT_ID)
                except Exception as e:
                    print(f"⚠️ Entry Sore pyramid: gagal kirim ringkasan final: {e}")
            else:
                print(f"[NO TELEGRAM TOKEN] {status_msg}")
        pyramid_state["final_summary_sent"] = True

    _save_bsjp_pyramid_state(pyramid_state)
    summary["positions"] = len(new_positions)
    print(f"✅ Entry Sore pyramid validation (siklus): {len(pending)} pending dicek, {len(new_positions)} baru resolve ke TIER1-3.")
    return summary


async def run_bsjp_pyramid_d1_once() -> dict:
    """
    Monitor D+1 posisi TIER1-3 dari run_bsjp_pyramid_validation_once: cek
    avg-down tier2(-2%)/tier3(-5%) dari decision_price (HANYA mulai D+1
    09:00 -- BUKAN sore yg sama), lalu TP/SL sesuai tier (TIER1 statis,
    TIER2/3 decay). Edit pesan HARI ALERT in-place selama masih hari yg
    sama runtime-nya sesuai tanggal decision_date (pesan TERSIMPAN dari
    hari alert -- TIDAK butuh pesan baru krn masih pesan yg sama dilanjut).
    """
    summary = {"skipped_reason": None, "updated": 0}
    now_wib = datetime.datetime.now(core.WIB)
    if now_wib.weekday() >= 5:
        summary["skipped_reason"] = "weekend"
        return summary
    if not (BSJP_D1_SESSION1_START <= now_wib.time() <= BSJP_D1_SESSION2_END):
        summary["skipped_reason"] = "outside_window"
        return summary
    if await asyncio.to_thread(core.is_idx_market_holiday_today):
        summary["skipped_reason"] = "holiday"
        return summary

    pyramid_state = _load_bsjp_pyramid_state()
    positions = pyramid_state.get("positions") or {}
    today = _today_str()
    open_positions = [
        p for p in positions.values()
        if p["status"] in ("OPEN", "SESSION2") and p["decision_date"] != today  # D+1 = HARUS beda hari dari decision
    ]
    if not open_positions:
        summary["skipped_reason"] = "no_open_positions"
        return summary

    # BUGFIX (ditemukan sebelum deploy, 2026-09-06): _fetch_bsjp_universe_
    # snapshot HANYA punya "high_so_far" (dari yf.download daily bar hari
    # berjalan), TIDAK ADA low_so_far sama sekali -- tidak bisa dipakai
    # deteksi avg-down/SL yg butuh LOW. Pakai _fetch_today_1m (bar 1m,
    # SAMA pola dgn ENTRY PAGI) supaya dapat High/Low intrabar riil.
    tickers = [p["ticker"] for p in open_positions]
    data = await _get_shared_1m_bars(tickers)  # 2026-09-11: shared-fetch cache, lihat catatan di _shared_1m_cache
    if data is None or (hasattr(data, "empty") and data.empty):
        summary["skipped_reason"] = "no_intraday_data"
        return summary

    dirty = False
    now_time = now_wib.time()
    in_session2 = now_time >= BSJP_D1_SESSION2_START
    force_exit = now_time >= BSJP_D1_SESSION2_END
    for p in open_positions:
        sym = p["ticker"] + ".JK"
        try:
            bars = data[sym].dropna(how="all").sort_index()
        except Exception:
            continue
        if bars.empty:
            continue
        try:
            day_high = float(bars["High"].astype(float).max())
            day_low = float(bars["Low"].astype(float).min())
            current_price = float(bars["Close"].astype(float).iloc[-1])
        except Exception:
            continue

        tier2_price = p["decision_price"] * (1 + BSJP_D1_TIER2_DIP_PCT / 100.0)
        tier3_price = p["decision_price"] * (1 + BSJP_D1_TIER3_DIP_PCT / 100.0)
        t1, t2, t3 = BSJP_D1_SPLIT
        if not p["filled_tier2"] and day_low is not None and day_low <= tier2_price:
            p["filled_tier2"] = True
            p["avg_cost"] = p["decision_price"] * t1 + tier2_price * t2
            p["avg_cost"] /= (t1 + t2)
            dirty = True
        if p["filled_tier2"] and not p["filled_tier3"] and day_low is not None and day_low <= tier3_price:
            p["filled_tier3"] = True
            weighted = p["decision_price"] * t1 + tier2_price * t2 + tier3_price * t3
            p["avg_cost"] = weighted / (t1 + t2 + t3)
            dirty = True

        if p["tier"] == "TIER1":
            tp, sl = _bsjp_d1_tp_sl_for_tier1(p["avg_cost"])
        else:
            tp, sl = _bsjp_d1_tp_sl_for_tier23(p["avg_cost"], now_time)
        if p["tp"] != tp or p["sl"] != sl:
            p["tp"], p["sl"] = tp, sl
            dirty = True

        if p["status"] == "OPEN" and in_session2:
            p["status"] = "SESSION2"
            dirty = True

        if day_high is not None and day_high >= p["tp"]:
            p["status"] = "TP"
            dirty = True
        elif day_low is not None and day_low <= p["sl"]:
            p["status"] = "SL"
            dirty = True
        elif force_exit:
            p["status"] = "FORCE_EXIT"
            dirty = True

    if dirty:
        summary["updated"] = sum(1 for p in open_positions if p["status"] not in ("OPEN",))
        await _bsjp_pyramid_edit_message(pyramid_state)
        _save_bsjp_pyramid_state(pyramid_state)
    return summary


def build_bsjp_pyramid_tp_message() -> str | None:
    """
    /bsjp tp (VERSI BARU, MBSS v2 2026-09-06 -- ENTRY SORE full redesign):
    render dari bsjp_pyramid_state.json (posisi TIER1-3 hasil validasi
    15:00), grouped by tier, avg-down/TP/SL LIVE (avg_cost-based, ikut
    naik-turun sesuai fill avg-down & decay sesi -- BUKAN lagi static
    historical-hit-rate dari closing spt build_bsjp_tp_plan_message lama).
    Return None kalau BELUM ADA posisi pyramid sama sekali (belum pernah
    lolos validasi 15:00) -- caller fallback ke pesan closing lama.
    """
    state = _load_bsjp_pyramid_state()
    positions = list((state.get("positions") or {}).values())
    if not positions:
        return None

    by_tier = {"TIER1": [], "TIER2": [], "TIER3": []}
    done = []
    for p in positions:
        if p["status"] in ("TP", "SL", "FORCE_EXIT"):
            done.append(p)
        elif p["tier"] in by_tier:
            by_tier[p["tier"]].append(p)

    marker_done = {"TP": "✅ TP TERCAPAI", "SL": "✅ SL KENA", "FORCE_EXIT": "🔚 FORCE EXIT (closing D+1)"}
    lines = ["ENTRY SORE — /bsjp tp (posisi live pasca-validasi 15:00)"]
    any_open = False
    for tier_name in ("TIER1", "TIER2", "TIER3"):
        cands = by_tier[tier_name]
        if not cands:
            continue
        any_open = True
        lines.append(f"\n{tier_name}")
        for p in cands:
            avgdown_note = (
                " (avg down tier2+tier3 TERISI)" if p.get("filled_tier3")
                else " (avg down tier2 TERISI)" if p.get("filled_tier2") else ""
            )
            status_note = "🔄 Masuk Sesi 2" if p["status"] == "SESSION2" else "⏳ Menunggu"
            lines.append(
                f"  {p['ticker']} — keputusan {p['decision_price']:,.0f}, avg cost {p['avg_cost']:,.0f}{avgdown_note}\n"
                f"    TP : {p['tp']:,.0f} | SL : {p['sl']:,.0f} | {status_note}"
            )
    if done:
        lines.append("\n✅ SELESAI")
        for p in done:
            lines.append(f"  {p['ticker']} ({p['tier']}) — {marker_done.get(p['status'], p['status'])}, avg cost {p['avg_cost']:,.0f}")
    if not any_open and not done:
        return None
    return "\n".join(lines)


def build_bsjp_tp_plan_message() -> str:
    """
    /bsjp tp (MBSS v2, user request 2026-09-02, REVISI setelah live case
    KKES) -- panduan jual pre-open esok pagi utk SEMUA ticker yang lolos
    Fase 2 (source="bsjp") ATAU Tier 2 WATCH (source="bsjp_watch") hari
    bursa terakhir. ANCHOR = closing HARI ALERT (fetch live, BUKAN harga
    alert-fire) -- user: "sesuai disiplin BSJP yg memang seharusnya beli di
    sore hari (>15:30)", DAN backtest presisi CORRECTED konfirmasi closing
    anchor genuinely lebih baik (bukan cuma lebih benar disiplin) -- lihat
    catatan panjang di atas BSJP_TP_TIERS. Entry ALERT-FIRE (dari "tp1"
    tersimpan, direkonstruksi) TETAP dihitung & ditampilkan sbg REFERENSI
    (drift context), TAPI bukan lagi anchor TP.

    FADE CAP: kalau closing sudah >=5% di bawah harga alert-fire (drift),
    TP2 stretch DIHILANGKAN & TP1 diturunkan ke BSJP_FADE_TP1_GAP_PCT --
    backtest nunjuk grup ini literally 0% pernah tembus >=8% dari closing.
    Live case: KKES (avg cost user 111, closing 94-95, drift ~-13%) --
    dgn desain lama TP1/TP2 dari entry (109) nunjuk 111/116 yg TIDAK PERNAH
    kejadian di backtest (0/11). Dgn fade cap: TP1 = closing*1.025 (~97,
    ~55% historis), TIDAK ada TP2.

    PENTING soal tanggal (bug yg ketemu & DIPERBAIKI 2026-09-02, sebelum
    sempat dipakai produksi): jangan panggil ulang get_current_trading_day_
    close_marker() DI SINI utk cari pick_date target -- alert Fase 2 SELALU
    dikunci SAAT market masih buka (sebelum 16:30 WIB), jadi marker SAAT
    lock masih mundur 1 hari. Fix: cari pick_date TERBARU yang genuinely
    ada di history (gabungan kedua source) -- robust terlepas kapan /bsjp
    tp dipanggil.
    """
    history = core.load_daytrade_picks_history()
    bsjp_picks = [p for p in history if p.get("source") == "bsjp" and p.get("pick_date")]
    if not bsjp_picks:
        return "📋 Belum pernah ada BSJP Fase 2 -- tidak ada panduan TP."

    target_date = max(p["pick_date"] for p in bsjp_picks)
    main_picks = [p for p in bsjp_picks if p["pick_date"] == target_date]

    staleness_note = ""
    try:
        target_dt = datetime.datetime.strptime(target_date, "%Y-%m-%d").date()
        days_old = (datetime.datetime.now(core.WIB).date() - target_dt).days
        if days_old > 2:
            staleness_note = f"\n⚠️ Pick TERBARU dari {target_date} ({days_old} hari lalu) -- kemungkinan TIDAK ada alert baru-baru ini, ini BUKAN rencana malam ini.\n"
    except Exception:
        pass

    closes = _fetch_bsjp_closing_prices(main_picks)

    lines = [f"🌅 BSJP TP PLAN — {target_date} ({len(main_picks)} alert utama){staleness_note}\n"]

    def _entry_and_close(p: dict, tp1_multiplier_pct: float) -> tuple[float, float | None] | None:
        # MBSS v2 (bug ketemu & diperbaiki 2026-09-02, live case: /bsjp tp
        # tampil kosong -- lock_daily_daytrade_picks TIDAK PERNAH simpan
        # current_price mentah, cuma "tp1"/"cut_loss" flat di top level.
        # tp1 tersimpan SELALU = entry * (1+tp1_multiplier_pct/100) --
        # deterministik, entry ASLI direkonstruksi persis dari situ.
        tp1_stored = p.get("tp1")
        if not tp1_stored:
            return None
        entry = tp1_stored / (1 + tp1_multiplier_pct / 100.0)
        close_px = closes.get(p["ticker"])
        return entry, close_px

    if main_picks:
        lines.append("🔥 ALERT UTAMA (BELI SORE INI)")
        for p in sorted(main_picks, key=lambda x: x["ticker"]):
            res = _entry_and_close(p, BSJP_TP1_MEDIAN_GAP_PCT)
            if res is None:
                continue
            entry, close_px = res
            cut_loss = p.get("cut_loss")
            tier = (p.get("feature_snapshot") or {}).get("tier") or 1
            tier_tag = f" {'🔥'*tier}" if tier > 1 else ""
            # MBSS v2 (user request 2026-09-03, live case SMBR/MDIA: closing
            # sudah DI BAWAH cut_loss [prev_close] sendiri saat /bsjp tp
            # ditampilkan -- thesis "buy power" sudah gagal per definisinya
            # sendiri, TP1 moderat msh tampil seolah setup normal, menyesatkan
            # sama spt pelajaran KKES). Tag eksplisit, bukan cuma dihitung diam2.
            invalid_tag = " 🚫INVALID" if (close_px is not None and cut_loss and close_px < cut_loss) else ""

            if close_px is None:
                # Closing belum berhasil di-fetch -- fallback entry-anchor
                # LAMA drpd tidak nampilkan apa-apa, TAPI beri label jelas.
                ret1d_at_entry = (p.get("feature_snapshot") or {}).get("ret_1d_pct")
                tp1_gap, tp1_hit, tp2_gap, tp2_hit = _bsjp_tp_for_ret1d(ret1d_at_entry)
                line = (
                    f"{p['ticker']}{tier_tag}{invalid_tag} — entry alert-fire {entry:,.0f} (closing GAGAL di-fetch, pakai entry sbg fallback)\n"
                    f"  TP1: {entry * (1 + tp1_gap / 100.0):,.0f} (+{tp1_gap:.1f}%)   TP2: {entry * (1 + tp2_gap / 100.0):,.0f} (+{tp2_gap:.1f}%)"
                )
            else:
                drift = (close_px / entry - 1) * 100
                drift_label = f"{drift:+.1f}%"
                if drift <= BSJP_FADE_DRIFT_THRESHOLD_PCT:
                    tp1 = close_px * (1 + BSJP_FADE_TP1_GAP_PCT / 100.0)
                    line = (
                        f"{p['ticker']}{tier_tag}{invalid_tag} — closing {close_px:,.0f} (entry alert-fire {entry:,.0f}, drift {drift_label} — FADE, "
                        f"TP2 DIHILANGKAN, backtest 0% tembus >=8% dari closing utk grup ini)\n"
                        f"  TP1 (moderat, ~{BSJP_FADE_TP1_HISTORICAL_HIT_PCT:.0f}% historis touch Day+1): {tp1:,.0f} (+{BSJP_FADE_TP1_GAP_PCT:.1f}%)"
                    )
                else:
                    ret1d_at_entry = (p.get("feature_snapshot") or {}).get("ret_1d_pct")
                    tp1_gap, tp1_hit, tp2_gap, tp2_hit = _bsjp_tp_for_ret1d(ret1d_at_entry)
                    tp1 = close_px * (1 + tp1_gap / 100.0)
                    tp2 = close_px * (1 + tp2_gap / 100.0)
                    line = (
                        f"{p['ticker']}{tier_tag}{invalid_tag} — closing {close_px:,.0f} (entry alert-fire {entry:,.0f}, drift {drift_label})\n"
                        f"  TP1 (moderat, ~{tp1_hit:.0f}% historis touch Day+1): {tp1:,.0f} (+{tp1_gap:.1f}%)\n"
                        f"  TP2 (stretch, ~{tp2_hit:.0f}% historis touch Day+1): {tp2:,.0f} (+{tp2_gap:.1f}%)"
                    )
            if cut_loss:
                line += f"\n  Cut loss: {cut_loss:,.0f}"
            lines.append(line)

    lines.append(
        "\n⚠️ TP1/TP2 dihitung dari CLOSING hari alert (sesuai disiplin BSJP beli sore >15:30), BUKAN dari harga "
        "alert-fire lagi -- backtest presisi (1m riil, 5 hari bursa) konfirmasi closing anchor lebih akurat, "
        "terutama utk ticker yg fade jauh dari harga alert-fire. FADE (drift closing vs alert-fire <=-5%) hanya "
        "dapat TP1 moderat, TIDAK ada TP2 -- backtest 0% tembus di atas 7-8% dari closing utk grup itu. "
        "Sampel per tier masih kecil (11-42), treat sbg directional."
    )
    return "\n\n".join(lines)


# =====================================================================
# SECTION 2 -- engine/legacy_core.py, original lines 7583-7619
# (run_bsjp_recheck_job, run_bsjp_shortlist_scan_job)
# =====================================================================

async def run_bsjp_recheck_job(context: ContextTypes.DEFAULT_TYPE):
    """
    JobQueue callback TERPISAH (MBSS v2, user request 2026-08-29 --
    unified BSJP "Beli Sore Jual Pagi", 2-fase): Fase 2 -- re-cek live
    shortlist yang disimpan /bsjp (Fase 1, akhir sesi 1) tiap
    BSJP_RECHECK_INTERVAL_SEC (30 menit), kirim alert final ke ticker yg
    MASIH lolos semua 4 kriteria saat itu (engine/scanalert.py
    run_bsjp_recheck_once). State file terpisah (bsjp_shortlist_state.json)
    -- no-op murah di luar jendela BSJP_RECHECK_WINDOW_START-END
    (14:00-15:50 WIB) ATAU kalau belum ada shortlist hari ini (/bsjp belum
    dijalankan), aman didaftarkan interval rapat sama seperti conviction
    sweep di atas.
    """
    try:
        await scanalert_engine.run_bsjp_recheck_once()
    except Exception as e:
        print(f"⚠️ BSJP recheck job gagal: {e}")


async def run_bsjp_shortlist_scan_job(context: ContextTypes.DEFAULT_TYPE):
    """
    JobQueue callback TERPISAH (MBSS v2, user request 2026-08-31, live case
    BALI/KICI lolos shortlist tapi sudah +25%/+24.4% -- practically ARA,
    sudah tidak bisa dibeli): Fase 1 OTOMATIS -- SEBELUMNYA Fase 1 CUMA
    manual-trigger (/bsjp), jadi kandidat yg nembus threshold pagi2 bisa
    sudah lanjut lari ke ARA sebelum sempat ketahuan kalau user baru cek
    siang/sore. Scan tiap BSJP_SHORTLIST_SCAN_INTERVAL_SEC (15 menit),
    no-op murah di luar jendela 09:00-15:50 WIB (engine/scanalert.py
    run_bsjp_shortlist_scan_auto) -- aman didaftarkan interval rapat sama
    spt job lain di atas. /bsjp manual TETAP ada, dua cara ini saling
    melengkapi (union shortlist yg sama, bukan duplikat state terpisah).
    """
    try:
        await scanalert_engine.run_bsjp_shortlist_scan_auto()
    except Exception as e:
        print(f"⚠️ BSJP shortlist scan (auto) job gagal: {e}")



# =====================================================================
# SECTION 3 -- engine/legacy_core.py, original lines 7663-7684
# (run_bsjp_pyramid_validation_job, run_bsjp_pyramid_d1_job)
# =====================================================================

async def run_bsjp_pyramid_validation_job(context: ContextTypes.DEFAULT_TYPE):
    """
    JobQueue callback TERPISAH -- ENTRY SORE Sequential Tier1-3 @ 15:00
    (full redesign, user request 2026-09-06), engine/scanalert.py
    run_bsjp_pyramid_validation_once. State file SENDIRI (bsjp_pyramid_
    state.json) -- TIDAK menyentuh mekanisme Fase2 live yg sudah ada.
    """
    try:
        await scanalert_engine.run_bsjp_pyramid_validation_once()
    except Exception as e:
        print(f"⚠️ Entry Sore pyramid validation job gagal: {e}")


async def run_bsjp_pyramid_d1_job(context: ContextTypes.DEFAULT_TYPE):
    """
    JobQueue callback TERPISAH -- ENTRY SORE D+1 exit (avg-down tier2/3,
    TP/SL per tier), engine/scanalert.py run_bsjp_pyramid_d1_once.
    """
    try:
        await scanalert_engine.run_bsjp_pyramid_d1_once()
    except Exception as e:
        print(f"⚠️ Entry Sore pyramid D+1 job gagal: {e}")


# =====================================================================
# SECTION 4 -- commands/scan.py, original lines 2528-2637
# (bsjp_screening_command -- handled both /bsjp and the old /bsjp tp)
# =====================================================================

# ==========================================
# BSJP SCREENING (MBSS v2, user request 2026-08-29 -- REVISI TOTAL:
# unified 4-kriteria, blend ARA/second-wave/6-kriteria-lama/pullback jadi
# SATU sinyal "Beli Sore Jual Pagi". Full parameter sweep (162 kombinasi,
# close-based/exit-efficiency validation, daily_2y_issi_raw.pkl) -- lihat
# catatan lengkap di engine/scanalert.py run_bsjp_shortlist_scan/run_bsjp_
# recheck_once. Command ini = FASE 1 (scan penuh universe akhir sesi 1,
# simpan shortlist) -- FASE 2 (recheck live tiap 5 menit 09:30-15:50 --
# dipercepat dari 15 menit 2026-09-02, lihat catatan lengkap di atas
# BSJP_RECHECK_INTERVAL_SEC engine/scanalert.py, kirim alert final) jalan
# otomatis via JobQueue, lihat engine/legacy_core.py run_bsjp_recheck_job.
# ==========================================


async def bsjp_screening_command(update, context):
    """
    /bsjp -- FASE 1 unified BSJP: scan SELURUH universe ISSI thd 4 kriteria
    wajib (AND, REDESIGN 2026-09-03, lihat engine/scanalert.py utk detail &
    sumber angka lengkap -- BSJP_SHORTLIST_RET1D_MIN_PCT dkk):
      1. ret_1d > 12%
      2. Volume hari ini > 3x volume kemarin (pace-adjusted, lihat TOTAL_DAY_BARS)
      3. High hari ini < 1.15x harga sekarang ("clean close", wide net)
      4. Volume hari ini > 3x rata-rata volume 200 hari (pace-adjusted)
    Simpan yg lolos sbg shortlist (dipakai FASE 2 -- recheck live otomatis
    tiap 5 menit 09:30-15:50 WIB, ret_1d>12% & vol>4x & clean_close<1.025,
    lihat run_bsjp_recheck_job).

    MBSS v2 (user request 2026-08-31 -- "harusnya tetap bisa di running
    ketika istirahat, kan hanya untuk jaring kandidat awal?"): jendela
    DIPERLEBAR dari get_current_idx_session() (yg return None saat istirahat
    siang 12:00-13:30/11:30-14:00 Jumat) ke SELURUH hari bursa 09:00-16:00 --
    BEDA dgn command lain yg genuinely butuh sesi AKTIF (harga bergerak
    detik-ini). BSJP Fase 1 cuma butuh data HARI INI SEJAUH INI (ret_1d,
    volume-so-far, high-so-far via yf.download partial-day bar) -- data itu
    SUDAH final/beku begitu sesi 1 tutup, TIDAK berubah lagi selama istirahat
    (baru update lagi begitu sesi 2 buka), jadi genuinely valid dicek kapan
    pun 09:00-16:00, termasuk pas istirahat. Sebelum 09:00 TETAP ditolak
    (belum ada data hari ini SAMA SEKALI, bukan cuma beku).
    """
    import engine.scanalert as scanalert_engine  # import lokal -- hindari circular import di level modul

    # MBSS v2 (user request 2026-09-02): /bsjp tp -- panduan jual pre-open
    # esok pagi (TP1/TP2 dari CLOSING hari alert, lihat catatan lengkap di
    # atas build_bsjp_tp_plan_message), TIDAK dibatasi jendela 09:00-16:00
    # spt scan Fase 1 di bawah -- justru dipakai MALAM hari yg sama atau
    # PAGI besok SEBELUM market buka. Sekarang fetch closing LIVE (blocking
    # I/O) -- wrap _fetch_with_timeout spt fetch BSJP lain di file ini.
    if context.args and context.args[0].lower() == "tp":
        # MBSS v2 (user request 2026-09-06 -- ENTRY SORE full redesign):
        # prioritaskan pesan pyramid LIVE (avg-down/TP/SL dari validasi
        # 15:00) kalau sudah ada posisi -- fallback ke pesan closing lama
        # (build_bsjp_tp_plan_message) kalau belum ada posisi pyramid sama
        # sekali (mis. belum ada ticker yg lolos validasi 15:00 hari itu).
        pyramid_msg = scanalert_engine.build_bsjp_pyramid_tp_message()
        if pyramid_msg is not None:
            await core.safe_reply(update.message, pyramid_msg)
            return
        msg = await scanalert_engine._fetch_with_timeout(
            scanalert_engine.build_bsjp_tp_plan_message, timeout=60,
            default="⚠️ Gagal ambil harga closing (timeout/Yahoo error) -- coba lagi.",
        )
        await core.safe_reply(update.message, msg)
        return

    now_wib = datetime.datetime.now(core.WIB)
    bsjp_window_start = datetime.time(9, 0)
    bsjp_window_end = datetime.time(16, 0)  # akhir pra-penutupan, sama batas atas semua sesi IDX
    is_holiday = await asyncio.to_thread(core.is_idx_market_holiday_today)
    if now_wib.weekday() >= 5 or is_holiday or not (bsjp_window_start <= now_wib.time() < bsjp_window_end):
        await core.safe_reply(
            update.message,
            "⚠️ /bsjp cuma berguna selama hari bursa berjalan (09:00-16:00 WIB) -- di luar itu belum/tidak ada data hari ini utk dicek."
        )
        return

    scored = nightly_engine.load_daily_scan_cache()
    if not scored:
        await core.safe_reply(update.message, "⚠️ Cache /eodscan belum ada/basi -- jalankan /eodscan dulu (dari kemarin sore, bukan hari ini).")
        return
    universe = sorted(scored.keys())

    await core.safe_reply(update.message, f"🌆 Scan BSJP (4 kriteria unified) dari {len(universe)} ticker universe, mengecek data live...")

    try:
        passed = await scanalert_engine.run_bsjp_shortlist_scan(universe)
    except Exception as e:
        await core.safe_reply(update.message, f"⚠️ Scan BSJP gagal: {e}")
        return

    if not passed:
        await core.safe_reply(
            update.message,
            "📋 Tidak ada kandidat yang lolos SEMUA 4 kriteria BSJP saat ini (formula ketat -- wajar kalau kosong, itu justru tujuannya). "
            "Kalau ada shortlist tersimpan dari /bsjp sebelumnya hari ini, itu TETAP dipantau (tidak dihapus)."
        )
        return

    passed.sort(key=lambda r: r["ret_1d_pct"], reverse=True)
    lines = [f"🌆 BSJP SHORTLIST — {len(passed)} kandidat lolos SEMUA 4 kriteria (akan di-recheck live tiap 5 menit 09:30-15:50)\n"]
    for i, r in enumerate(passed, 1):
        vol_vs_prev = r["volume_so_far"] / max(r["prev_volume"], 1.0)
        vol_vs_ma200 = r["volume_so_far"] / max(r["vol_ma200"], 1.0)
        lines.append(
            f"{i}. {r['ticker']} — {r['current_price']:,.0f} ({r['ret_1d_pct']:+.1f}%)\n"
            f"   Vol {vol_vs_prev:.1f}x kemarin | {vol_vs_ma200:.1f}x MA200"
        )
    lines.append("\n⚠️ Ini shortlist FASE 1, BUKAN alert entry -- alert final (dgn TP1) dikirim otomatis kalau kandidat MASIH lolos semua kriteria saat recheck 09:30-15:50 WIB.")

    buttons = core.build_check_buttons([r["ticker"] for r in passed])
    await core.safe_reply(update.message, "\n\n".join(lines), reply_markup=buttons)

