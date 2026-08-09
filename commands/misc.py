"""
commands/misc.py — Command Layer: misc/system group (MBSS v2 Sprint 1, Phase 5b)

Telegram handlers for /start, /version, /whitelist, /glossary (+ /istilah),
/rebuildwhitelist, /winrate, /dbstats (+ /dbstatus), /populatedb,
/testbrief, /testopening.

All thin handlers — everything they call (safe_reply, whitelist builders,
DB stats, run_morning_brief, run_opening_dynamics, STATUS_LABEL_ID, ...)
stays in engine/legacy_core.py, accessed via `core.xxx`. Same circular-
import rule as every other Command Layer / engine module in this refactor
— see engine/nightly.py's docstring for the full explanation. Short
version: `import engine.legacy_core as core` here, `import commands.misc
as commands_misc` in legacy_core.py's build_app(), never `from module
import name`.

NOTE: db_stats_command and populate_db_command used to be defined as
NESTED functions inside build_app() itself (closures over module globals)
rather than top-level functions like everything else — an inconsistency
in the original code, not something this refactor introduced. Un-nested
here into ordinary top-level functions; behavior unchanged.
"""
from __future__ import annotations

import asyncio
import json
import os

import engine.legacy_core as core


GLOSSARY_TEXT = """📖 KAMUS ISTILAH BOT

━━━ SKOR FAKTOR ━━━
Nilai (Value): valuasi saham — PE, PB, yield dividen. Tinggi = murah secara fundamental.
Momentum: arah & kekuatan tren harga — RSI, MACD, SMA, pola chart.
Sentimen: tekanan beli/jual dari volume — CMF, OBV, rasio volume.
Final: gabungan tertimbang dari ketiganya (30% Nilai, 40% Momentum, 30% Sentimen).

━━━ INDIKATOR TEKNIKAL ━━━
RSI (Relative Strength Index): 0-100, mengukur jenuh beli/jual berdasarkan riwayat harga saham itu sendiri (adaptif). Sekitar 45-55 = netral sehat; mendekati 65-75+ = mulai jenuh beli.
MACD: indikator TIMING — beda dari RSI. RSI = "apakah harga sudah jenuh", MACD = "apakah momentum baru saja berbalik arah". Cross bullish/bearish = momentum baru berubah arah, sinyal timing yang lebih segar.
SMA50: rata-rata harga 50 hari — konteks tren menengah. "Di Bawah SMA50: True" = tren menengah masih lemah meski jangka pendek terlihat oke.
CMF (Chaikin Money Flow): -1 sampai +1. Apakah volume besar itu benar tekanan BELI (closing dekat harga tertinggi hari itu) atau tekanan JUAL (closing dekat terendah) — bukan cuma "volume tinggi = bagus".
OBV Divergence: membandingkan arah harga vs arah volume kumulatif. "bearish_divergence" = harga tenang/naik tapi volume sebenarnya menunjukkan distribusi diam-diam (jual). "bullish_divergence" = kebalikannya (akumulasi diam-diam).
Rentang 10 hari (day_range_pct_10d): seberapa jauh harga bergerak dalam 10 hari terakhir (tertinggi vs terendah). Rendah = harga "beku"/kurang aktif — TAPI ini bisa juga berarti ada akumulasi/distribusi diam-diam (lihat OBV Divergence), bukan otomatis berarti "tidak menarik".
Pola Chart (lower_highs_bearish): rangkaian puncak harga yang makin menurun — sinyal pelemahan meski indikator lain terlihat oke.

━━━ DATA FUNDAMENTAL ━━━
PE (Price-to-Earnings): harga saham dibanding laba per saham. Rendah = relatif murah, TAPI PE sangat rendah (<3) bisa juga tanda laba tidak wajar/sementara.
PB (Price-to-Book): harga saham dibanding nilai buku aset. <1 = secara teori diperdagangkan di bawah nilai aset bersihnya.
Yield Dividen: dividen tahunan dibagi harga saham, dalam %. Makin tinggi = makin menarik untuk strategi dividend capture.

━━━ DATA BROKER RIIL (Index Alpha, opt-in /myportfolio brokersum, maks 5 saham/hari) ━━━
Net Foreign Flow %: real net beli-jual ASING (bukan domestik — dibatasi jadi asing-saja karena limit keras 5x/hari), -100% sampai +100%. Ini DATA ASLI, bukan proksi seperti CMF/OBV.
Broker Concentration %: seberapa terkonsentrasi net-buy asing pada beberapa broker teratas. Tinggi (>25%) = sinyal lebih kuat (sedikit pemain besar bergerak) dibanding net-buy yang sama tapi tersebar di banyak broker.
Proxy Agreement: apakah data broker riil ini SESUAI atau BERTENTANGAN dengan CMF/OBV kita. "CONTRADICTION" = perlu perhatian ekstra, dua sumber data saling bertolak belakang.
Brokersum Adjusted: jika True, data riil ini SUDAH mengubah skor Sentimen & Final serta keputusan sistem (bukan cuma catatan tambahan) — hanya terjadi jika Broker Concentration cukup tinggi (≥10%) untuk dipercaya.

━━━ KEPUTUSAN SISTEM (dihitung otomatis, bukan pilihan AI) ━━━
BELI KUAT: skor tinggi, tidak ada flag risiko.
BELI / AKUMULASI: skor cukup baik, ATAU skor tinggi tapi ada 1 flag caution (jadi diturunkan dari BELI KUAT).
TAHAN: skor sedang, ATAU ada flag serius (distress finansial/pola lower-highs) yang membatasi ke level ini.
HINDARI / JUAL: skor rendah.
SINYAL CAMPURAN: skor komponen (Nilai/Momentum/Sentimen) saling bertentangan tajam, ATAU skor berada tepat di garis batas ditambah flag caution — sengaja TIDAK memberi kepastian palsu saat data memang tidak jelas arahnya.

━━━ FLAG LAINNYA ━━━
Overbought Caution: RSI mendekati area jenuh beli KHUSUS untuk saham ini (adaptif, bukan angka baku).
Distress: PE/PB negatif (laba/ekuitas negatif) — tanda masalah keuangan nyata.
Dekat Harga Dasar: harga di bawah ~Rp70, potensi saham "beku"/distressed (mis. WEGE, GIAA).
PE Tidak Wajar Rendah: PE di bawah ~3, mungkin laba tidak berulang, bukan murah sungguhan.
Lonjakan Volume: volume 3x+ normal — bisa jadi berita besar atau likuiditas tipis, bukan otomatis bullish.

Ketik /check, /myportfolio, atau /testbrief untuk melihat istilah-istilah ini dalam analisis nyata."""


STARTUP_DISCLAIMER = (
    "⚠️ CATATAN PENTING (ditampilkan sekali saat bot aktif):\n"
    "Semua analisis dari bot ini HANYA berdasarkan data harga, volume, dan rasio "
    "valuasi dasar. TIDAK termasuk data net buy/sell broker asing/lokal, dan TIDAK "
    "mencakup pengumuman resmi IDX secara menyeluruh (laporan keuangan formal, aksi "
    "korporasi) — hanya berita publik yang tersedia.\n\n"
    "Ini adalah alat bantu screening berbasis data, BUKAN saran keuangan profesional. "
    "Selalu lakukan verifikasi independen sebelum mengambil keputusan investasi.\n\n"
    "Catatan ini tidak akan diulang lagi di setiap pesan — cukup jadi pengingat bahwa "
    "bot sedang aktif."
)


async def start(update, context):
    await core.safe_reply(update.message, STARTUP_DISCLAIMER)
    await core.safe_reply(update.message,
        "🤖 Sharia Stock Bot aktif.\n\n"
        "Perintah analisa:\n"
        "/check TICKER — analisa saham apa saja on-demand (aware posisi jika sudah dimiliki)\n"
        "  Contoh: /check TLKM\n"
        "/check TICKER zapi — + data broker riil dari Zapi (sumber alternatif,\n"
        "  belum terverifikasi live vs harian — lihat catatan di hasil)\n\n"
        "Perintah portofolio:\n"
        "/buy TICKER HARGA LOT — catat posisi beli (butuh cash cukup)\n"
        "  Contoh: /buy TLKM 2850 10\n"
        "/sell TICKER LOT HARGA — jual & hitung realized P&L otomatis\n"
        "  Contoh: /sell TLKM 5 2900\n"
        "/addcash JUMLAH — tambah saldo cash\n"
        "  Contoh: /addcash 5000000\n"
        "/withdrawcash JUMLAH — kurangi saldo cash\n"
        "/resetportfolio — hapus SEMUA posisi & cash (perlu konfirmasi)\n"
        "/setentrydate TICKER YYYY-MM-DD — isi/perbaiki tanggal beli (untuk kategori siklus)\n"
        "/watchlist — lihat/kelola watchlist (maks 3 saham, belum dimiliki)\n"
        "  /watchlist add TICKER | /watchlist remove TICKER\n"
        "/summary — ringkasan cepat + tombol cek harga cepat + order tracking\n"
        "/order buy TICKER LOT HARGA — pantau order beli (PERKIRAAN status, bukan\n"
        "  kepastian — kita tidak punya akses ke app broker, cuma bandingkan harga)\n"
        "/order sell TICKER LOT HARGA — sama untuk order jual\n"
        "/order — lihat semua order dipantau | /order clear TICKER — hapus pantauan\n"
        "/myportfolio — analisis mendalam + kategori siklus (Baru/Produktif/Hati-hati/Evaluasi)\n"
        "/myportfolio brokersum — + data broker asing RIIL (Index Alpha), auto-pilih 5\n"
        "  saham prioritas tertinggi (maks 5/hari — batas keras dari Index Alpha)\n"
        "/myportfolio brokersum TICKER1 TICKER2 — pilih manual saham mana yang dicek\n"
        "/myportfolio brokersum zapi — sama, tapi via Zapi, SEMUA posisi+watchlist\n"
        "  tercover sekaligus (budget lebih longgar, ~10-12/hari aman dari 300/bulan)\n"
        "Setelah /check, bot akan tanya apakah kamu punya screenshot Broker Sum dari\n"
        "  app-mu hari ini — kirim untuk memperkaya analisis, atau tekan tombol Lewati.\n\n"
        "/glossary — kamus istilah (RSI, MACD, CMF, OBV, dll) yang dipakai di analisis\n\n"
        "/screendaytrade — screening khusus saham paling aktif/volatil HARI INI\n"
        "  (beda bobot dari brief pagi, fokus momentum bukan value; tanpa data broker\n"
        "  karena horizonnya jam-menit, bukan cocok untuk data EOD)\n"
        "/screendaytrade issi — sama, tapi universe ISSI (ratusan saham syariah,\n"
        "  bukan cuma ISSI yang 70) dengan filter likuiditas (harga + volume 10hr\n"
        "  bursa >=500rb lembar). Cache 2 minggu — pemakaian pertama tiap 2 minggu\n"
        "  butuh beberapa menit (via Zapi bulk), setelahnya instan sampai 2 minggu berikutnya\n"
        "/screendaytrade live — urutkan MURNI dari sinyal live (VWAP + volume pace\n"
        "  SEKARANG), bukan lane EOD. Untuk buru saham yang lagi aktif/dikejar buyer\n"
        "  saat ini juga — cuma berguna & bakal kosong di luar jam bursa\n"
        "/gptpick — shortlist syariah likuid terbaik untuk besok; ranking top 3/5\n"
        "/hc — top 10 saham HIGH CONVICTION dari cache /eodscan, urut skor final\n"
        "  (instan, tidak fetch apa pun — is_high_conviction sudah dihitung saat scan)\n"
        "  pakai cache malam + broker flow bila tersedia\n"
        "/eodscan — jalankan full scan EOD manual sekali jalan; semua analisis\n"
        "  berikutnya baca cache yang sama\n"
        "/winrate — scorecard uji akurasi rekomendasi /screendaytrade (TP1/CL asli\n"
        "  dari rekomendasi hari itu, entry = harga open besoknya, dicek harian\n"
        "  s/d 5 hari bursa). Picks otomatis terkunci tiap kali /screendaytrade\n"
        "  dijalankan, diresolusi tiap malam setelah scan jam 22:00\n\n"
        "Testing (jalankan manual tanpa nunggu jadwal):\n"
        "/testbrief — jalankan Morning Brief sekarang\n"
        "/testopening — jalankan Opening Dynamics sekarang\n\n"
        "Otomatis terjadwal:\n"
        "🌙 04:00 WIB — Morning Brief Sharia (ISSI), sebelum market buka\n"
        "🕤 09:45 WIB — Opening Dynamics (gap, momentum, volume pagi)"
    )


async def show_version(update, context):
    await core.safe_reply(
        update.message,
        f"📌 Scoring formula version: {core.SCORING_FORMULA_VERSION}\n\n"
        "Skor dari versi formula yang berbeda tidak bisa dibandingkan langsung — "
        "jika skor untuk saham yang sama terlihat berbeda antar hari, cek dulu apakah "
        "versi formulanya berubah sebelum menganggap itu ketidakkonsistenan data."
    )


async def show_glossary(update, context):
    await core.safe_reply(update.message, GLOSSARY_TEXT)


async def show_whitelist_status(update, context):
    if not os.path.exists(core.WHITELIST_CACHE_FILE):
        await core.safe_reply(update.message, "📋 Belum ada whitelist tersimpan — akan dibuat otomatis saat brief berikutnya jalan.")
        return
    try:
        with open(core.WHITELIST_CACHE_FILE) as f:
            cache = json.load(f)
        eligible = cache.get("eligible_tickers", [])
        excluded = cache.get("excluded_tickers", {})
        excluded_lines = "\n".join(f"- {t}: {r}" for t, r in list(excluded.items())[:20])
        await core.safe_reply(
            update.message,
            f"📋 Whitelist bulan {cache.get('generated_month')}:\n"
            f"Eligible: {len(eligible)} saham\n"
            f"Excluded: {len(excluded)} saham\n\n{excluded_lines}"
        )
    except Exception as e:
        await core.safe_reply(update.message, f"⚠️ Gagal membaca whitelist: {e}")


async def rebuild_whitelist_command(update, context):
    await core.safe_reply(update.message, "🔄 Membangun ulang whitelist bulanan, ini akan memakan waktu beberapa menit...")
    try:
        sharia_universe = await asyncio.to_thread(core.fetch_online_sharia_list)
        eligible = await asyncio.to_thread(core.load_or_build_whitelist, list(sharia_universe), True)
        await core.safe_reply(update.message, f"✅ Whitelist selesai dibangun ulang: {len(eligible)} saham eligible.")
    except Exception as e:
        await core.safe_reply(update.message, f"⚠️ Gagal membangun ulang whitelist: {e}")


async def show_winrate(update, context):
    """
    Scorecard uji winrate rekomendasi — TP1/cut_loss ASLI dari rekomendasi
    hari itu (bukan angka tetap terpisah), entry = harga OPEN hari bursa
    setelah pick dikunci. Mencakup pick dari /screendaytrade DAN /gptpick
    (dibedakan field "source", MBSS v2 Sprint 2 Tier 1.4) — dikelompokkan
    juga per jenis sinyal (lane /screendaytrade atau action label /gptpick)
    supaya kelihatan sinyal mana yang secara empiris lebih akurat. Saham
    yang sama BISA muncul di beberapa tanggal — ini SENGAJA (tiap hari =
    sinyal/keputusan independen yang diuji terpisah, bukan "apakah saham X
    bagus secara umum").
    """
    history = core.load_daytrade_picks_history()
    if not history:
        await core.safe_reply(update.message, "📊 Belum ada data winrate — /screendaytrade perlu dijalankan dulu untuk mulai mengunci picks.")
        return

    history_sorted = sorted(history, key=lambda p: (p["pick_date"], p["ticker"]))

    # Detail per-pick dibatasi 5 hari bursa TERAKHIR saja (bukan seluruh histori) —
    # daftar ini tumbuh terus tiap /screendaytrade dijalankan, jadi tanpa batas ini
    # pesan Telegram lama-lama jadi sangat panjang. Ringkasan per-tanggal & agregat
    # keseluruhan di bawah tetap pakai SELURUH histori (tetap ringkas, 1 baris/hari).
    RECENT_DETAIL_TRADING_DAYS = 5
    recent_dates = sorted({p["pick_date"] for p in history_sorted})[-RECENT_DETAIL_TRADING_DAYS:]
    recent_picks = [p for p in history_sorted if p["pick_date"] in recent_dates]

    today_str = core.get_current_trading_day_close_marker()
    lines = ["📊 WINRATE STOCKPICK\n", f"Today: {today_str}\n"]
    lines.append("Saham // Tanggal // P/L // Status // Sumber  (5 hari bursa terakhir)")
    for p in recent_picks:
        pnl_str = f"{p['pnl_pct']:+.1f}%" if p["pnl_pct"] is not None else "-"
        status_str = core.STATUS_LABEL_ID.get(p["status"], p["status"])
        source_str = p.get("source", "screendaytrade")  # pick lama (pra-Sprint 2) belum punya field ini
        lines.append(f"{p['ticker']} // {p['pick_date']} // {pnl_str} // {status_str} // {source_str}")

    # MBSS v2 Sprint 2 (Tier 1.4, lanjutan): winrate DIKELOMPOKKAN per jenis sinyal
    # (bukan per tanggal) — supaya kelihatan langsung sinyal mana yang secara
    # empiris lebih akurat (mis. apakah "SINYAL CAMPURAN" justru menang lebih
    # sering dari sinyal yang kelihatan lebih meyakinkan). Pick lama (sebelum
    # field ini ada) dikelompokkan sebagai "N/A (data lama)" — tetap ikut dihitung
    # di KESELURUHAN, cuma tidak bisa dipecah per sinyal.
    lines.append("\nWinrate per Jenis Sinyal (seluruh histori, diurutkan tertinggi)")
    by_signal = {}
    for p in history_sorted:
        label = p.get("signal_label") or "N/A (data lama)"
        by_signal.setdefault(label, []).append(p)

    signal_rows = []
    for label, picks in by_signal.items():
        resolved = [p for p in picks if p["status"] in ("win", "lose", "win_timebased", "lose_timebased")]
        if not resolved:
            continue  # semua masih pending untuk grup ini — belum ada apa-apa untuk dibandingkan
        wins = [p for p in resolved if p["status"] in ("win", "win_timebased")]
        winrate_pct = len(wins) / len(resolved) * 100
        avg_gain = sum(p["pnl_pct"] for p in resolved if p["pnl_pct"] is not None) / len(resolved)
        signal_rows.append((label, winrate_pct, len(wins), len(resolved), avg_gain))

    signal_rows.sort(key=lambda row: row[1], reverse=True)
    for label, winrate_pct, wins, resolved, avg_gain in signal_rows:
        lines.append(f"{label}: {winrate_pct:.0f}% Win ({wins}/{resolved}); avg {avg_gain:+.1f}%/pick")

    # MBSS v2 (user request): winrate per PANJANG STREAK kemunculan
    # berturut-turut — uji hipotesis "makin sering muncul beruntun di
    # scanner, makin yakin sinyalnya benar (meski kadang telat)". Cuma
    # berlaku pick BARU (consecutive_streak baru mulai dicatat sekarang) —
    # pick lama otomatis masuk grup "N/A (data lama)".
    by_streak = {}
    for p in history_sorted:
        streak = p.get("consecutive_streak")
        label = f"Streak {streak}x" if streak else "N/A (data lama)"
        by_streak.setdefault(label, []).append(p)

    streak_rows = []
    for label, picks in by_streak.items():
        resolved = [p for p in picks if p["status"] in ("win", "lose", "win_timebased", "lose_timebased")]
        if not resolved:
            continue
        wins = [p for p in resolved if p["status"] in ("win", "win_timebased")]
        winrate_pct = len(wins) / len(resolved) * 100
        avg_gain = sum(p["pnl_pct"] for p in resolved if p["pnl_pct"] is not None) / len(resolved)
        streak_rows.append((label, winrate_pct, len(wins), len(resolved), avg_gain))

    if any(r[0] != "N/A (data lama)" for r in streak_rows):
        lines.append("\nWinrate per Panjang Streak Kemunculan Berturut-turut")
        streak_rows.sort(key=lambda row: row[0])  # urut "Streak 1x, 2x, 3x..." bukan winrate, biar mudah dibaca trennya
        for label, winrate_pct, wins, resolved, avg_gain in streak_rows:
            lines.append(f"{label}: {winrate_pct:.0f}% Win ({wins}/{resolved}); avg {avg_gain:+.1f}%/pick")

    # MBSS v2 (user request): rata-rata berapa HARI dari entry sampai
    # resolusi, dipisah per jenis status — "win"/"lose" = kena TP1/SL bersih
    # (days_checked dihitung dari hari SETELAH entry, bukan dari pick_date).
    # win_timebased/lose_timebased selalu tepat di hari ke-5 by design
    # (WINRATE_RESOLUTION_WINDOW_DAYS), jadi ditampilkan cuma buat konfirmasi
    # konsisten, bukan informasi baru.
    days_by_status = {}
    for p in history_sorted:
        if p.get("days_checked") and p["status"] in ("win", "lose", "win_timebased", "lose_timebased"):
            days_by_status.setdefault(p["status"], []).append(p["days_checked"])

    if days_by_status:
        lines.append("\nRata-rata Hari dari Entry sampai Resolusi")
        status_order = ["win", "lose", "win_timebased", "lose_timebased"]
        status_label_days = {
            "win": "✅ Win bersih (kena TP1)", "lose": "❌ Lose bersih (kena SL)",
            "win_timebased": "✅ Win (time-based, hari ke-5)", "lose_timebased": "❌ Lose (time-based, hari ke-5)",
        }
        for status in status_order:
            days_list = days_by_status.get(status)
            if not days_list:
                continue
            avg_days = sum(days_list) / len(days_list)
            lines.append(f"{status_label_days[status]}: rata-rata {avg_days:.1f} hari (n={len(days_list)})")

    lines.append("\nSummary per Tanggal")
    by_date = {}
    for p in history_sorted:
        by_date.setdefault(p["pick_date"], []).append(p)

    for pick_date, picks in sorted(by_date.items()):
        resolved = [p for p in picks if p["status"] in ("win", "lose", "win_timebased", "lose_timebased")]
        pending = [p for p in picks if p["status"] in ("pending_entry", "pending_resolution")]
        if not resolved and pending:
            lines.append(f"{pick_date}: {len(pending)} pick masih berjalan, belum ada hasil final")
            continue
        wins = [p for p in resolved if p["status"] in ("win", "win_timebased")]
        winrate_pct = (len(wins) / len(resolved) * 100) if resolved else 0
        total_gain = sum(p["pnl_pct"] for p in resolved if p["pnl_pct"] is not None)
        pending_note = f" ({len(pending)} masih berjalan)" if pending else ""
        lines.append(f"{pick_date}: {winrate_pct:.0f}% Win ({len(wins)}/{len(resolved)}); Total gain {total_gain:+.1f}%{pending_note}")

    # Ringkasan keseluruhan — bukti agregat apakah stockpick genuinely solid
    all_resolved = [p for p in history if p["status"] in ("win", "lose", "win_timebased", "lose_timebased")]
    if all_resolved:
        all_wins = [p for p in all_resolved if p["status"] in ("win", "win_timebased")]
        overall_winrate = len(all_wins) / len(all_resolved) * 100
        overall_gain = sum(p["pnl_pct"] for p in all_resolved if p["pnl_pct"] is not None)
        avg_gain = overall_gain / len(all_resolved)
        lines.append(f"\n📈 KESELURUHAN ({len(all_resolved)} pick selesai): {overall_winrate:.0f}% Win — "
                      f"rata-rata {avg_gain:+.2f}%/pick, total {overall_gain:+.1f}%")

    await core.safe_reply(update.message, "\n".join(lines))


async def test_morning_brief(update, context):
    await core.safe_reply(update.message, "🧪 Menjalankan Morning Brief manual, mohon tunggu...")
    await core.run_morning_brief(context)


async def test_opening_dynamics(update, context):
    await core.safe_reply(update.message, "🧪 Menjalankan Opening Dynamics manual, mohon tunggu...")
    await core.run_opening_dynamics(context)


async def db_stats_command(update, context):
    stats = core.get_db_stats()
    if not stats:
        await update.message.reply_text("📦 DB belum ada atau kosong. Jalankan /populatedb dulu.")
        return
    await update.message.reply_text(
        f"📦 OHLCV Database\n"
        f"Daily: {stats['daily_tickers']} ticker, {stats['daily_rows']:,} bar\n"
        f"4H: {stats['h4_tickers']} ticker\n"
        f"Ukuran: {stats['size_mb']} MB\n\n"
        f"Last update: {stats.get('last_ohlcv_update_at') or '-'}\n"
        f"Updated s/d: {stats.get('last_ohlcv_update_marker') or '-'}\n"
        f"Night scan: {stats.get('last_nightly_scan_at') or '-'}\n"
        f"Night marker: {stats.get('last_nightly_scan_marker') or '-'}"
    )


async def populate_db_command(update, context):
    """
    Populate DB dari yfinance — ISSI saja, deduplicate.
    ISSI bisa ratusan ticker, proses bisa 10-20 menit — bot tetap bisa digunakan
    selama proses berlangsung karena dijalankan di background thread.
    """
    await update.message.reply_text(
        "📥 Memulai populate DB dari yfinance (ISSI, 2 tahun histori)...\n"
        "Proses bisa 10-20 menit. Bot tetap bisa digunakan selama ini."
    )
    try:
        issi = set(core.fetch_online_sharia_list(index_key="ISSI"))
        all_tickers = list(issi)
        await update.message.reply_text(
            f"📋 Total: {len(all_tickers)} ticker ISSI unik"
        )
        stats_payload = await asyncio.to_thread(core.populate_from_yfinance, all_tickers, "2y", 50)
        stats = core.get_db_stats()
        msg = (
            f"✅ DB berhasil dipopulate!\n"
            f"{stats.get('daily_tickers', 0)} ticker, {stats.get('daily_rows', 0):,} bar, "
            f"{stats.get('size_mb', 0)} MB\n"
            f"Updated s/d: {stats.get('last_ohlcv_update_marker') or stats_payload.get('latest_marker') or '-'}"
        )
        await update.message.reply_text(msg)
        try:
            await context.bot.send_message(
                chat_id=core.TELEGRAM_CHAT_ID,
                text=(
                    f"✅ DB update sukses\n"
                    f"Universe: ISSI\n"
                    f"Ticker: {stats_payload.get('tickers', len(all_tickers))}\n"
                    f"Updated s/d: {stats_payload.get('latest_marker') or stats.get('last_ohlcv_update_marker') or '-'}\n"
                    f"Rows written: {stats_payload.get('rows_written', 0):,}"
                ),
            )
        except Exception as notify_error:
            print(f"⚠️ Gagal kirim notifikasi DB update: {notify_error}")
    except Exception as e:
        await update.message.reply_text(f"⚠️ Gagal populate DB: {e}")
