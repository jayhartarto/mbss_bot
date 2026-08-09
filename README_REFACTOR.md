# MBSS v2 Sprint 1 — Refactor Progress

## Status: Fase 5 SELESAI TOTAL — seluruh Command Layer sudah terpecah

Fase 1-4 (Bootstrap, CacheManager, NightlyEngine, MarketContextEngine,
BrokerEngine) tervalidasi penuh di Termux Anda. Fase 5 (dikerjakan dalam
4 sub-fase: scan/screening, misc/system, portfolio, check/screenshot)
memecah **seluruh 33 handler Telegram** dari `legacy_core.py` (9010 baris)
ke `commands/*.py`. `legacy_core.py` sekarang **6920 baris** — turun ~23%,
dan isinya murni "engine" (scoring, DB, portfolio state, integrasi Gemini)
tanpa satu pun handler Telegram langsung.

**Konfirmasi**: `build_app()` sekarang mendaftarkan 33 handler, dan
ke-33-nya berasal dari modul `commands.*` — nol yang masih langsung dari
`legacy_core.py`.

## Struktur folder FINAL

```
mbss/
├── bot.py                  ← Bootstrap/entrypoint
├── engine/
│   ├── __init__.py
│   ├── cache.py             ← CacheManager (Fase 1)
│   ├── nightly.py           ← NightlyEngine (Fase 2, universe diperluas 3b)
│   ├── market.py            ← MarketContextEngine (Fase 3 + breadth/regime 3b)
│   ├── broker.py            ← BrokerEngine (Fase 4)
│   └── legacy_core.py       ← scoring engine, DB, portfolio state, Gemini (6920 baris)
├── commands/
│   ├── __init__.py
│   ├── scan.py               ← /screendaytrade /gptpick /executiongate /eodscan (Fase 5a)
│   ├── misc.py                ← /start /version /whitelist /glossary /winrate
│   │                             /dbstats /dbstatus /populatedb /testbrief /testopening (Fase 5b)
│   ├── portfolio.py           ← /buy /sell /addcash /withdrawcash /resetportfolio
│   │                             /setentrydate /watchlist /summary /order /myportfolio
│   │                             + callback order-clear & screendaytrade-brokersum (Fase 5c)
│   └── check.py                ← /check /brokersum + handler screenshot + callback
│                                  skip/quick-check (Fase 5d)
└── cache/
```

## Bug yang ditemukan & diperbaiki selama Fase 5 (dikonfirmasi ke Anda dulu)

1. **`quick_check_callback` (tombol qchk_...)** — punya 5 baris kode nyasar
   di akhir fungsi, duplikat PERSIS dari isi `skip_brokersum_callback`, tapi
   tanpa `async def` sendiri — karena aturan indentasi Python, itu otomatis
   ikut jalan sebagai bagian dari `quick_check_callback` SETIAP kali tombol
   quick-check ditekan (ikut menghapus pending brokersum check chat itu,
   menghapus reply markup pesan, dan kirim pesan tambahan "👍 Dilewati."
   yang tidak relevan). Anda konfirmasi untuk diperbaiki — sudah dihapus di
   `commands/check.py`, dan sudah divalidasi dengan `inspect.getsource()`.
2. **Label basi "Universe: ISSI liquid"** di pesan `/eodscan` — sudah
   diperbaiki jadi "ISSI eligible (Yahoo whitelist, ~389 ticker)" sesuai
   Fase 3b (ditemukan & diperbaiki di Fase 5a).
3. **Duplikat registrasi `/testopening`** — terdaftar 2x di `build_app()`
   lama (harmless tapi sloppy), sudah dirapikan jadi 1x.
4. **`STARTUP_DISCLAIMER` referensi liar** — `send_startup_notice()`
   memakai `STARTUP_DISCLAIMER` yang sempat terhapus saat `start` handler
   dipindah; ditemukan lewat pencarian sistematis sebelum sempat jadi bug
   nyata di produksi, sudah diperbaiki (`commands_misc.STARTUP_DISCLAIMER`).
5. **`brokersum_line` dan `freshness_line` di `/check` dihitung tapi tidak
   pernah ditampilkan** — ditemukan lewat laporan Anda ("check tidak minta
   upload foto"), ternyata bukan soal itu (logic skip-cache-nya memang
   benar), tapi soal LAIN: blok "💹 BROKER RIIL" dan warning data freshness
   dihitung penuh oleh kode, tapi tidak pernah dimasukkan ke pesan `msg1`
   yang benar-benar dikirim ke Telegram — bug ini SUDAH ADA di kode asli
   Anda (bukan buatan refactor ini, saya cuma memindahkan persis apa
   adanya saat Fase 5d). Sudah diperbaiki di `commands/check.py`: kedua
   baris itu sekarang masuk ke `msg1`. `intraday_line` (variabel lain yang
   juga dihitung tapi tidak dipakai) SENGAJA dibiarkan tidak dipakai —
   itu duplikat dari yang sudah ditampilkan `intraday_status`, jadi
   menambahkannya akan bikin "High/Low" tampil dua kali. Diverifikasi
   dengan test lengkap: brokersum & freshness sekarang benar-benar muncul
   di pesan.

## Pola "thin handler" — ringkasan keputusan tiap kelompok

Konsisten di semua 4 sub-fase: HANYA fungsi handler Telegram (yang
terdaftar via `add_handler`) yang pindah. Semua logic dalam (scoring,
whitelist builder, portfolio.json read/write, DB, reasoning Gemini) TETAP
di `legacy_core.py`, diakses lewat `core.xxx` / `broker_engine.xxx` /
`nightly_engine.xxx`. Pengecualian: cluster GPTPICK di `commands/scan.py`
(Fase 5a) dipindah utuh karena terkonfirmasi tidak dipakai di luar
cluster-nya sendiri.

## Soal circular import (pola sama, sudah 4x dipakai berulang)

`commands/scan.py`, `commands/misc.py`, `commands/portfolio.py`,
`commands/check.py` semua saling butuh dua arah dengan `legacy_core.py`
(`build_app()` perlu fungsi handler untuk registrasi; tiap modul command
perlu `core.xxx` untuk logic dalam). Pola yang sama terus dipakai: MODULE
import di kedua sisi, tidak pernah `from module import name`. Sudah dites
dari **9 urutan impor berbeda** (tiap modul `commands.*`/`engine.*`
masing-masing diimpor duluan, plus `legacy_core`/`bot.py`) — semua aman.

## Yang SUDAH saya tes (sandbox saya)

- ✅ `py_compile` semua file.
- ✅ Import dari 9 urutan berbeda.
- ✅ `build_app()` dijalankan sungguhan: 33 handler terdaftar, 100% dari
  `commands.*` (5 scan + 11 misc + 12 portfolio + 5 check).
- ✅ `bot.py --dbstats` end-to-end.
- ✅ Bugfix `quick_check_callback` diverifikasi lewat `inspect.getsource()`
  — tidak ada lagi jejak kode nyasar.
- ✅ (dari fase-fase sebelumnya) Unit test breadth/regime, brokersum
  fetch/cache/history, gptpick scoring — semua masih identik.

## Yang BELUM bisa saya tes di sini

Semua 33 command sungguhan lewat Telegram (fetch data live, Gemini
reasoning, portfolio state) — sudah sebagian besar Anda validasi manual
sepanjang percakapan ini (`/check` + screenshot, `/screendaytrade`,
`/gptpick`, `/executiongate`, `/eodscan`). Kelompok portfolio (`/buy`,
`/myportfolio`, dll) dan misc (`/start`, `/winrate`, dll) belum dicoba
ulang setelah dipindah — sebaiknya di-spot-check sebelum dianggap final.

## Cara pasang di Termux

```bash
cd ~/mbss
mkdir -p commands
# timpa engine/legacy_core.py
# tambahkan/timpa: commands/scan.py, commands/misc.py, commands/portfolio.py,
#                   commands/check.py, commands/__init__.py
python -m py_compile bot.py engine/*.py commands/*.py && echo "compile OK"
python bot.py
```

Saran urutan spot-check di Telegram: `/start` → `/summary` (atau
`/myportfolio` kalau ada posisi) → `/check TICKER` (+ coba kirim
screenshot kalau ada) → `/winrate` → `/watchlist`.

**Rollback**: `bot_dev_eodscan_fixed.py` asli Anda tidak pernah saya ubah,
dari awal sampai sekarang.

## Rencana lanjutan (opsional, di luar Sprint 1 asli)

Refactor arsitektur inti (Bootstrap, Cache, Nightly, Market, Broker,
Command Layer) sudah selesai sesuai Executive Summary. Yang tersisa kalau
mau dilanjutkan:
- `engine/scoring.py` — ekstrak `compute_factor_scoring`,
  `apply_brokersum_adjustment`, `compute_brokersum_priority`, dan fungsi
  klasifikasi (`classify_action_priority`, dll) dari `legacy_core.py` ke
  modul scoring tersendiri (saat ini "dipinjam" oleh hampir semua modul
  lain via `core.xxx`).
- `engine/gptscore.py` — pisahkan cluster GPTPICK dari `commands/scan.py`
  kalau mau benar-benar match nama modul di Executive Summary.
- Selaraskan universe `/testbrief`/`/screendaytrade`/`/gptpick` supaya
  konsisten dengan `/eodscan` (389 ticker) — item tertunda dari
  percakapan sebelumnya, Anda pilih "nanti saja".


