# MBSS v2 — Sprint 2 (Revisi)
## Intelligence & Scoring Engine

Status: READY TO START
Revisi dari draf asli — direstrukturisasi jadi 3 tier berdasarkan apa yang
sudah ada di kode vs genuinely baru, plus riset sumber data untuk item yang
sebelumnya belum ada sumbernya.

Prerequisite:
- ✅ Sprint 1 (Engine Refactor) COMPLETE — Bootstrap, CacheManager,
  NightlyEngine, MarketContextEngine (foundation), BrokerEngine, Command
  Layer (33 handler, seluruhnya di `commands/*.py`)

---

# Kenapa direvisi

Draf asli menulis 10 phase sebagai daftar linear, seolah semua dimulai dari
nol. Setelah Sprint 1 selesai, kenyataannya:

- **Sebagian sudah ada** (breadth/regime dasar, bandarmology proxy, macro
  news) — mengerjakannya lagi dari nol berisiko bikin 2 versi logic yang
  beda angka.
- **Ada inkonsistensi internal** antar-phase yang perlu diputuskan dulu,
  bukan ditemukan pas coding.
- **Beberapa deliverable tidak bisa diukur** tanpa infrastruktur tambahan
  yang tidak disebut di draf asli (winrate tracking, histori regime).
- **Beberapa sumber data belum dikonfirmasi ada** — sama seperti riset
  breadth/MarketContext di Sprint 1, harus dicek dulu sebelum di-planning
  sebagai deliverable pasti.

Struktur baru: **3 tier**, bukan 10 phase berurutan wajib.

---

# TIER 1 — Wajib, fondasi, risiko rendah

Ini prasyarat sebelum tier lain punya arti. Tanpa ini, Tier 2/3 akan
dibangun di atas fondasi yang masih pecah jadi 3 sistem skor berbeda.

## 1.1 — Central Scoring Engine (`engine/scoring.py`) ✅ SELESAI

Dipindah dari `legacy_core.py` (5846 baris, turun dari 6953) ke
`engine/scoring.py` (1201 baris) — 7 fungsi: `compute_factor_scoring`,
`apply_brokersum_adjustment` (+`_apply_brokersum_adjustment_original`),
`compute_brokersum_priority`, `classify_action_priority`,
`classify_risk_character`, `compute_high_conviction_score`, `decide_action`.

**Keputusan 3-sistem-skor-paralel**: TIDAK diselesaikan sepenuhnya di fase
ini — `_gptpick_score` di `commands/scan.py` tetap terpisah dari
`engine/scoring.py` untuk sekarang (belum diserap). Alasannya berubah dari
draf awal: setelah investigasi nyata (RR jelek, ADX arah-buta, AVOID_SELL
lolos filter — semua diperbaiki di sesi sebelumnya), `_gptpick_score`
terbukti punya fungsi yang genuinely berbeda (ranking/shortlist top-N
dengan filter tambahan), bukan sekadar duplikat `compute_factor_scoring`.
Penyerapan penuh bisa jadi item terpisah kalau memang diperlukan nanti.

**Yang TIDAK ikut pindah** (tetap di `legacy_core.py`, ternyata nyempil di
tengah cluster yang dikira kontinu):
- Kalkulator indikator mentah (`calculate_rsi/macd/cmf/obv/adx`,
  `detect_obv_divergence`, `detect_lower_highs`, `percentile_rank`,
  `score_from_percentile`) — `calculate_rsi` dipakai juga di luar cluster
  ini, jadi semuanya dibiarkan sebagai layer "indikator teknikal" bersama,
  sama seperti `get_yf_ticker`/`get_ohlcv_smart`.
- `compute_daytrade_score` — walau posisinya di tengah-tengah cluster
  secara tekstual, dipakai luas oleh `/screendaytrade`, winrate-lock, dan
  GPTPick — bukan bagian scoring inti gaya `/check`.
- `SCORING_FORMULA_VERSION`, `MIN_HISTORY_FOR_ADAPTIVE`, `MIN_STOCK_PRICE`
  — dipakai lintas modul lain, tetap di `legacy_core.py`.

**Validasi**: compile OK, circular-import aman dari 10 urutan impor
berbeda, `build_app()` tetap 33 handler tanpa regresi, **dan yang paling
penting — `compute_factor_scoring` benar-benar DIJALANKAN (bukan cuma
diimpor) dengan 250 hari data OHLCV realistis, sampai selesai tanpa
`NameError`**, plus simulasi penuh `run_nightly_full_scan` end-to-end
lewat jalur `scoring_engine` yang baru.

---

## 1.2 — Selaraskan formula Unified Score (2.3 vs 2.9, draf asli)

Draf asli mendefinisikan komponen Unified Score dua kali dengan hasil
beda:

| Sumber | Komponen |
|---|---|
| Phase 2.3 (contoh output) | Technical/Liquidity/**Breakout**/Broker/Market/Sector/Risk (20+20+20+15+10+10+5) |
| Phase 2.9 (formula) | Technical 30% / Liquidity 20% / **Momentum** 15% / Broker 15% / Market 10% / Sector 5% / Risk 5% |

Ini harus diputuskan SATU versi sebelum implementasi — "Breakout" dan
"Momentum" kedengarannya mirip tapi tidak otomatis sama (breakout = event
teknikal spesifik, momentum = arah tren berkelanjutan). Rekomendasi:
pakai daftar 2.9 (formula persentase, lebih mudah di-*tune*), dan definisikan
"Momentum" secara eksplisit mencakup elemen breakout di dalamnya kalau itu
maksudnya.

## 1.3 — Selaraskan universe `/testbrief` & `/screendaytrade` dengan `/eodscan`

Item yang sudah dibahas sebelumnya dan Anda tunda ("nanti saja"). **Ini
sekarang jadi prasyarat**, bukan lagi opsional — performance target Sprint
2 (`/testbrief <1 detik`) secara matematis tidak mungkin tercapai selama
`/testbrief` masih fetch live untuk ticker yang tidak overlap dengan
universe `/eodscan`.

## 1.4 — Tag `source` + label sinyal SERAGAM di pick tracking yang sudah ada

**Direvisi 2x setelah diskusi dengan user**: awalnya diusulkan sebagai
sistem tracking terpisah untuk GPTPick. Setelah dicek ulang, GPTPick bukan
mesin prediksi yang tervalidasi terpisah — bobotnya (`_gptpick_score`)
hand-tuned, bukan hasil belajar dari data winrate riil, dan seluruh bahan
skornya sudah ada di `compute_factor_scoring`. Jadi GPTPick secara
struktural adalah re-ranking dari sinyal yang sama, dikemas jadi shortlist
top-N.

Yang genuinely dibutuhkan: **field `source`** ditambahkan ke mekanisme
lock/resolve yang SUDAH ADA (`lock_daily_daytrade_picks` /
`resolve_daytrade_picks` / `load_daytrade_picks_history`), supaya pick dari
`/screendaytrade` dan `/gptpick` sama-sama tercatat lewat satu sistem yang
sama.

**Soal label sinyal — revisi kedua (koreksi klaim salah saya sebelumnya):**
Draf pertama saya usulkan "tier kasar" (HIGH/MEDIUM/LOW) karena saya kira
lane classifier `/screendaytrade` (`compute_screendaytrade_positive_bias`)
butuh data live/intraday — **ini salah**, dikoreksi oleh user. Setelah
dicek ulang: lane classifier itu 100% berbasis field EOD
(`compute_daytrade_v5_summary`, komentarnya sendiri bilang "Radar labels,
not live entry signals") — data live (`active_breakout`) cuma bonus kecil
opsional SETELAH lane ditentukan, bukan prasyarat. Jadi solusi yang benar
jauh lebih sederhana: **panggil lane classifier yang SAMA persis untuk
kandidat GPTPick**, gratis (tidak ada fetch tambahan, semua bahannya sudah
di cache EOD yang sama), menghasilkan label yang GENUINELY sebanding
lintas source — bukan mapping manual/tier kasar. Sudah diimplementasikan
di `commands/scan.py` (`_run_gptpick` menghitung `_positive_lane` sebelum
mengunci pick), diverifikasi jalan tanpa `active_breakout` sama sekali.

**Kenapa ini penting untuk Sprint 3, bukan cuma fitur `/winrate`**: histori
winrate yang rapi dan konsisten ini adalah bahan baku untuk Learning Engine
Sprint 3 (lihat "Out of Scope Sprint 3" — Adaptive Weight belajar dari
histori winrate). Kalau tracking-nya berantakan atau tidak konsisten
sekarang, Sprint 3 tidak akan punya data bersih untuk "belajar" dari situ.
Merapikan ini di Sprint 2 = investasi langsung ke fondasi Sprint 3.

---

# TIER 2 — Konsolidasi (data & logic sudah ada, tinggal dirapikan + extend)

Ini BUKAN "bangun modul baru dari nol" — ini "ambil yang sudah tersebar,
formalkan jadi modul, isi bagian yang genuinely kurang."

## 2.1 — Market Context Score (dari draf Phase 2.2)

**Sudah ada** (Sprint 1, Fase 3b): `compute_market_breadth()` (advancers/
decliners/unchanged, rata-rata return per sektor) dan
`classify_market_regime()` (5 label snapshot harian).

**Yang genuinely perlu ditambah:**
- New High / New Low count, Advance/Decline Ratio presisi (breadth yang
  ada sekarang cuma hitung naik/turun/tetap, belum rasio maupun new-high/low)
- **Histori breadth harian** — deliverable baru yang wajib: `regime`
  sekarang cuma snapshot 1-hari (sengaja diberi disclaimer "bukan model
  trending/ranging multi-hari" waktu dibangun). Klasifikasi Trending vs
  Sideways vs Risk-Off yang diminta draf asli SECARA DEFINISI butuh data
  beberapa hari — tanpa nyimpan histori, tidak bisa dibedakan dari snapshot
  harian yang sudah ada.
- Sector Rotation bonus (belum ada — breadth per-sektor sudah ada, tapi
  belum ada "bonus skor kalau saham dari sektor terkuat")
- ATR Percentile, Relative Volume Percentile (belum ada)

**Catatan penting soal sektor**: field `sector` yang sudah mengalir ke
scoring itu dari `yfinance.info["sector"]` — taksonomi GICS (Financial
Services, Basic Materials, Energy, dst), BUKAN breakdown IDX-spesifik
("Bank", "Property" ala draf asli — itu sub-industri GICS, bukan sektor).
**Putuskan dulu**: pakai GICS apa adanya (gratis, sudah ada), atau bangun
mapping sektor IDX sendiri (kerja tambahan, tidak disebut di draf asli).
Rekomendasi: pakai GICS dulu untuk Sprint 2, evaluasi mapping IDX-spesifik
di Sprint 3 kalau memang dibutuhkan.

## 2.2 — Broker Intelligence (dari draf Phase 2.6)

**Sudah ada** (di `legacy_core.py`, bukan lewat refactor Sprint 1 —
ini logic lama yang sudah jalan): `compute_executiongate_bandarmology_proxy()`
menghitung `accumulation_score`, `distribution_risk`, dan `phase`
(ACCUMULATION / DISTRIBUTION_RISK / EARLY_MARKUP / DISTRIBUTION_WATCH /
NEUTRAL) — tapi cuma dipakai di `/executiongate`, belum di GPTPick. Broker
concentration & trend historis juga sudah ada di `engine/broker.py`
(`compute_brokersum_trend`, Fase 4 Sprint 1).

**Yang genuinely perlu ditambah:**
- Ekspos bandarmology proxy yang sudah ada ke GPTPick/Unified Score
  (kerja integrasi, bukan bangun ulang)
- Retail vs Institution Estimate (belum ada — dan perlu dicek dulu apakah
  ada sinyal proxy yang wajar dari data yang tersedia, atau butuh sumber baru)
- Top Buyer Persistence (belum ada — turunan dari histori broker yang
  sudah ada di `engine/broker.py`, kemungkinan besar bisa dihitung dari
  data yang sudah disimpan tanpa fetch baru)

## 2.3 — News/Macro Intelligence (dari draf Phase 2.8)

**Sudah ada** (Sprint 1, Fase 3): `fetch_company_news()` (berita per-emiten),
`fetch_market_news_headlines()` (berita pasar umum via Google News RSS),
`fetch_macro_context()` (Wall St, Nikkei, Hang Seng, USD/IDR, minyak — via
yfinance).

**Yang genuinely perlu ditambah:**
- Coal & Nickel — ⚠️ **belum ada sumber terkonfirmasi.** Tidak ketemu
  ticker Yahoo Finance yang reliable untuk harga komoditas ini secara
  langsung (beda dari Gold/Oil yang sudah jalan). Opsi: (a) proxy saham
  emiten tambang batubara/nikel IDX sebagai indikator tidak langsung —
  sudah bisa langsung pakai `yfinance` yang ada, tapi itu proxy bukan
  harga komoditas asli; (b) cari sumber harga komoditas khusus (perlu
  riset lanjutan, API terpisah). Rekomendasi: mulai dengan opsi (a) untuk
  Sprint 2, cari sumber asli di Sprint 3 kalau proxy tidak cukup akurat.
- Catalyst Score (belum ada — logic baru, turunan dari news yang sudah
  di-fetch, tidak perlu sumber data baru)

## 2.4 — Liquidity Engine (`engine/liquidity.py`, dari draf Phase 2.4)

**Bahan bakunya sudah ada** sebagai field di `compute_factor_scoring`
(value_traded, vol_ratio). Ini kerja **konsolidasi jadi modul formal**,
bukan bangun dari nol:
- Average Daily Value, Relative Volume — sudah ada, tinggal dipindah
- Median Volume, Turnover, Volume Stability — perlu dihitung baru, tapi
  dari data yang sudah tersedia di DB OHLCV (tidak perlu fetch baru)
- Spread Estimation — genuinely baru, IDX tidak publish bid-ask spread
  historis gratis; kemungkinan perlu diestimasi dari high-low range
  (proxy), bukan spread asli

---

# TIER 3 — Genuinely baru, riset/keputusan dulu sebelum commit

## 3.1 — Risk Engine (`engine/risk.py`, dari draf Phase 2.5)

**Perlu diklarifikasi dulu**: kode sudah punya `classify_risk_character()`
(BASE_DEFENSIF / SWING_AGRESIF / NETRAL). Apakah Risk Engine baru ini
MENGGANTIKAN itu, atau CO-EXIST sebagai layer terpisah (mis. yang lama
tetap untuk gaya trading, yang baru untuk risk sizing)? Draf asli tidak
menyebutkan `classify_risk_character` sama sekali — kemungkinan besar
timnya tidak sadar itu sudah ada. Putuskan dulu sebelum implementasi,
supaya tidak ada dua classifier risk yang saling tumpang tindih/bertentangan.

Kalau memang perlu dibangun terpisah: Gap Risk, ATR Risk, Extended Move,
Distance EMA20/50 — semua bisa dihitung dari data OHLCV yang sudah ada,
tidak perlu sumber baru.

## 3.2 — Corporate Action Engine (dari draf Phase 2.7)

**Riset sumber data (baru, hasil sesi ini):**

| Sumber | Cakupan | Catatan |
|---|---|---|
| `idx.co.id` "Keterbukaan Informasi" (resmi) | Semua jenis pengumuman (dividen, rights issue, RUPS, suspensi) | Gratis, tapi dokumen PDF — perlu parsing PDF, bukan JSON siap pakai |
| Wrapper pihak ketiga (parse.bot) — endpoint `get_company_announcements` | Pengumuman terstruktur JSON + lampiran PDF | Kandidat paling praktis (JSON, bukan PDF-scraping) — **belum dicek pricing/rate limit**, perlu verifikasi dulu sebelum commit sebagai deliverable pasti |
| NeaByteLab/IDX-API (open source) | Sync data resmi IDX ke SQLite | Gratis, tapi dibangun pakai Deno — butuh proses terpisah atau reimplementasi endpoint-nya di Python |

Trading Halt/Suspension khususnya: berdasarkan berita terbaru, penyebab
paling umum di IDX adalah pelanggaran free-float atau keterlambatan
laporan keuangan (bukan cuma lonjakan harga) — kalau mau dicover penuh,
scope-nya lebih luas dari sekadar "pantau harga ekstrem".

**Rekomendasi**: jangan commit ke satu sumber dulu — mulai dengan
verifikasi cepat parse.bot punya free tier yang cukup, sebagai smoke test
sebelum investasi waktu besar ke integrasi.

## 3.3 — GPTPick Output Format (dari draf Phase 2.10)

Turunan langsung dari 1.1–1.2, 2.1–2.4. Kerjakan **terakhir**, setelah
Unified Score benar-benar stabil dari Tier 1 & 2 — mengerjakan format
output duluan sebelum skornya jadi cuma bikin bongkar-pasang ulang.

---

# Performance Target (tidak berubah dari draf asli, dengan catatan)

```
/gptpick         < 2 detik
/testbrief        < 1 detik   ⚠️ butuh 1.3 (universe alignment) selesai dulu
/screendaytrade   < 2 detik
```

# Quality Target (tidak berubah, dengan catatan)

```
Win Rate TP1      > 60%   ⚠️ butuh 1.4 (winrate tracker) untuk bisa diukur
Average RR        > 2
Maximum Candidate  5
Universe          ISSI Eligible Only
```

# Out of Scope (Sprint 3) — tidak berubah dari draf asli

Adaptive Weight, Machine Learning, Historical Learning, Automatic Weight
Adjustment, Backtest Dashboard, Execution Quality Score. Ditambah dari
revisi ini: mapping sektor IDX-spesifik (kalau GICS di 2.1 dianggap tidak
cukup), sumber harga Coal/Nickel asli (kalau proxy saham di 2.3 dianggap
tidak cukup akurat.

---

# Success Criteria (direvisi)

Sprint 2 dianggap selesai apabila:

- Seluruh scoring berasal dari `engine/scoring.py` — **termasuk**
  `_gptpick_score` (diserap atau dihapus, bukan dibiarkan paralel)
- GPTPick menggunakan Market Context (breadth + regime + histori minimal)
- Broker (termasuk bandarmology proxy yang sudah ada) menjadi bagian scoring
- Sector Rotation aktif (berbasis GICS, dengan keputusan eksplisit soal
  mapping IDX kalau diperlukan)
- Liquidity Engine aktif
- Risk Engine aktif (dengan resolusi eksplisit vs `classify_risk_character`)
- Output GPTPick menjelaskan alasan pemilihan saham
- Tidak ada duplicate scoring di command layer
- Seluruh command tetap membaca cache dari `/eodscan`
- **Baru**: GPTPick punya winrate tracker sendiri, terukur lewat `/winrate`
  atau command setara
- **Baru**: universe `/testbrief`/`/screendaytrade` selaras dengan `/eodscan`

---

# Target Release

MBSS v2 Beta — target tidak diubah dari draf asli, tapi disarankan
prioritaskan Tier 1 penuh dulu sebagai milestone terpisah sebelum
mengklaim Beta, supaya fondasi skor tidak berubah lagi di tengah jalan
saat Tier 2/3 sedang dikerjakan.

Setelah Sprint 2 selesai, Sprint 3 fokus ke Learning Engine, Backtesting,
dan Adaptive AI.
