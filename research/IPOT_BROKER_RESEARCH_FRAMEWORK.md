# Kerangka riset: eksplorasi broker summary IPOT (rancangan 2026-10-22)

Tujuan: menemukan **indikasi pemenang / pecundang** dari data broker summary harian IPOT, tanpa terikat hipotesis lama. Cukup berupa *indikasi*
yang bisa dicek manual di aplikasi (bukan aturan trading otomatis). Contoh arah yang diminta: identifikasi market maker (MM), rata-rata harganya,
dan pakai itu sebagai **plafon ekspektasi mark-up** setelah ritel keluar/berkurang.

Prinsip: terbuka pada semua fitur dan kombinasi, tetapi **ketat pada bukti**. Prior kita rendah: riset broker sebelumnya hampir semuanya nol
(CC/AK/YU/ZP, akumulasi whitelist, kombinasi broker); hanya XL kontrarian yang lemah. Karena itu pencarian luas wajib disertai kontrol
tebakan-kebetulan (permutation null), replikasi waktu, dan koreksi untuk banyaknya uji.

## 1. Fakta data yang menentukan desain (diperiksa pada 35 ticker x 10 hari; backfill penuh 150 ticker x 12 bulan sedang berjalan)
- Satu baris = (ticker, hari, varian fd/board, broker): beli kotor dan jual kotor (lot, nilai, harga rata-rata), **top-10 per sisi, diurutkan lot**.
  Broker yang tidak muncul = di bawah urutan ke-10 sisi itu, **bukan nol**. Nilai net hanya bisa dihitung bila broker ada di kedua sisi
  (48% baris); selain itu hanya satu sisi yang diketahui -> batas bawah.
- Varian: `all/all`, `F/all` (order investor asing), `D/all` (domestik), `all/NG` (papan negosiasi). F dan D punya top-10 sendiri, jadi cakupan
  broker kecil lebih baik. 77 broker unik pada sampel kecil.
- Sinyal dua sisi: XL dua sisi 98% harinya, CC 90%, AK 80%, YP 68%; hanya 17% untuk OD. Median selisih harga rata-rata beli vs jual broker dua sisi 0,00%
  (P10 -1,7%, P90 +1,2%). Konsentrasi: pembeli #1 ~31% nilai top-10 beli, penjual #1 ~33%.
- Harga rata-rata per broker per hari = **basis biaya** yang bisa dilacak lintas hari (VWAP inventori) -> inti ide plafon MM.
- Ketersediaan: ringkasan harian terbit setelah penutupan -> fitur hari t hanya boleh dipakai untuk entry penutupan t atau pembukaan t+1.
- Periode ~12 bulan: kira-kira dua regime (CRASH_FAST 2025 dan 2026), jadi semua temuan harus dipisah per regime.

## 2. Fase
**Fase 0 - Audit (tanpa hipotesis).** Cakupan per ticker/hari, batas top-10 per hari (nilai terkecil di daftar), stabilitas kode broker,
rekonsiliasi dengan baris RapidAPI yang tumpang tindih (apakah nilai sama), proporsi hari "kosong". Keluaran: tabel kualitas data + aturan buang.
**Fase 1 - Atlas fitur.** Bangun tabel lebar (ticker x hari) dari semua kelompok di bagian 3. Satu skrip, deterministik, bisa dijalankan ulang.
**Fase 2 - Mesin eksplorasi** (bagian 4), dijalankan berurutan; tiap mesin menghasilkan kandidat + angka kebetulan.
**Fase 3 - Validasi & kartu indikasi** (bagian 6). Hanya yang lolos dilaporkan.
**Fase 4 - Tampilan.** Baris informasi di `/check` (dan mungkin `/tanya`): "🔎 indikasi bandar: ..." **tidak pernah mengubah tag BELI/HINDARI**
kecuali lolos validasi penuh dan disetujui terpisah.

## 3. Atlas fitur (dibangkitkan sistematis, bukan dipilih tangan)
A. **Struktur & konsentrasi** - HHI sisi beli/jual, top1/top3 share, jumlah broker untuk mencapai 50% nilai top-10, asimetri beli-jual, rasio total top-10
   terhadap nilai transaksi harian, pangsa asing (F vs all), pangsa papan negosiasi (NG vs all).
B. **Identitas broker** - per kode: kehadiran, sisi, peringkat, net (bila dua sisi) atau batas bawah; kelas broker **berbasis perilaku** (klaster) dengan label
   manual hanya sebagai pembanding: asing institusi, domestik institusi, aplikasi ritel (mis. XL, YP, XC), prop/scalper (CP, MG), pemerintah (CC, OD, NI, DX).
C. **Harga & biaya** - harga rata-rata beli/jual tiap broker dibanding penutupan; jarak penutupan ke basis biaya (VWAP inventori) broker dominan; spread antara
   rata-rata pembeli #1 dan penjual #1; apakah kelas tertentu sedang "rugi" (harga di bawah basis biayanya) atau "untung".
D. **Persistensi & dinamika** - berapa hari beruntun broker ada di top beli/jual, pembalikan (pembeli -> penjual), akselerasi akumulasi, perubahan konsentrasi hari-ke-hari,
   z-score terhadap riwayat ticker itu sendiri (konvensi persentil-vs-riwayat-sendiri) dan terhadap universe hari itu.
E. **Kandidat MM / penyedia likuiditas** - skor dua-sisi = min(beli,jual)/max(beli,jual) lot, pangsa dari total volume, spread rata-rata beli-jual, kehadiran konsisten
   N hari; "inventori" perkiraan = kumulatif (beli-jual) pada hari terlihat dua sisi (batas bawah); VWAP inventori jendela 5/10/20 hari.
F. **Konteks pengendali** - regime IHSG, keanggotaan/umur lane dan tag, ret 1/5/10 hari, rasio volume, bendera lonjakan/EXT (supaya efek lonjakan tidak
   ditemukan ulang sebagai "efek broker"), likuiditas.

G. **Pola akumulasi harian** (catatan pengguna 2026-10-22: mendadak vs rutin vs lainnya). Taksonomi yang diukur, bukan dipercaya:
   1. *Letupan* - pembeli #1 hari ini >= k kali median pangsa-hariannya sendiri (20 hari) dan pangsanya di top-10 beli >= P90, disertai lonjakan volume.
   2. *Rutin / senyap (stealth)* - broker yang sama di top-3 pembeli pada >= m dari n hari (mis. 6 dari 10), sebaran pangsa net-harian rendah, harga datar (|ret 10 hari| kecil,
      ATR menyempit), harga rata-rata beli stabil ("menjaga area") -> inventori kumulatif naik sementara harga tidak.
   3. *Tangga / rampa* - pangsa net-harian naik berurutan dan harga rata-rata beli ikut naik (tanda awal mark-up).
   4. *Terpecah / rotasi* - pembelian terkonsentrasi tetapi dibagi ke beberapa broker yang bergerak bersama (aliansi) vs net-buy tersebar di banyak broker kecil (minat ritel).
   5. *Penyerapan* - net-buy besar pada hari turun dengan penutupan jauh dari low (kapitulasi diserap).
   6. *Distribusi tersamar* - harga datar/naik sedikit, volume tinggi, jual besar dari asing/institusi, sisi beli terfragmentasi (ke ritel).
   7. *Churn / pencucian* - kotor besar, net kecil, putaran beli-jual berukuran serupa berulang (MM atau wash) -> pembeda dari akumulasi nyata.
   8. *Blok negosiasi* - transaksi besar di papan NG (`all/NG`): pindah kepemilikan, bukan akumulasi pasar.
   Tiap pola dibuat sebagai fitur kontinu (skor) plus bendera, lalu dibiarkan bersaing di mesin 1-6; tidak ada pola yang diasumsikan menang.

## 4. Mesin eksplorasi
1. **Sapuan univariat** semua fitur x kuartil/ekstrem x horizon (1/3/5/10 hari) x metrik hasil. Bandingkan dengan null permutasi (acak hasil, pertahankan fitur).
2. **Kombinasi dangkal** - pohon keputusan kedalaman 2-3 / boosting kecil dengan validasi walk-forward; hanya aturan sederhana (<=3 syarat) yang diteruskan.
3. **Arketipe jejak** - klaster "sidik jari broker" per ticker-hari (pangsa tiap kelas, konsentrasi, dua-sisi, asing) lalu lihat hasil per klaster.
4. **Keterampilan broker** - per broker: return ke depan saat ia pembeli/penjual teratas vs pasar, dengan penyusutan empiris-Bayes (banyak broker -> banyak kebetulan),
   per regime; cari "broker pintar" dan "broker kontrarian" yang stabil.
5. **Jaringan/aliansi** - broker yang bergerak bersama (korelasi sisi-kehadiran per ticker) = kemungkinan pemilik yang sama / kelompok; ukur apakah kehadiran
   bersama bergerak lebih prediktif daripada satu broker.
6. **Studi peristiwa** - sejajarkan pada hari "ritel mulai keluar", "pembeli #1 berganti", "dua-sisi mendadak hilang"; lihat jalur harga rata-rata hari -10..+10
   (pola jalur memberi tahu waktu distribusi/mark-up tanpa hipotesis arah).
7. **Modul plafon MM** (hipotesis pengguna, dirumuskan terukur):
   - Identifikasi: broker dua-sisi persisten (skor >0,7 pada >=60% hari dalam 10 hari) dengan pangsa volume besar -> kandidat MM; VWAP inventorinya = "rata-rata MM".
   - Ritel keluar: pangsa jual kelas aplikasi-ritel naik / pembeli ritel turun (definisi kelas dari klaster).
   - Uji: (a) distribusi (high maksimum N hari ke depan / rata-rata MM - 1); (b) apakah harga cenderung berhenti dekat rata-rata MM atau pada kelipatan tertentu;
     (c) dibandingkan dengan kontrol (saham serupa tanpa MM, atau hari tanpa ritel-keluar); (d) apakah pola ini ada hanya di sekitar lonjakan (pengacau).
   - Keluaran: plafon ekspektasi (persentil 50/75 mark-up) per ticker bila terbukti, sebagai informasi "perkiraan area jual MM".

8. **Replikasi heuristik populer** - tiru apa adanya alat yang dipakai ritel (Top-1/3/5 net ratio dan label Acc/Dist kecil-normal-besar ala Bandar Detector, rata-rata
   modal bandar 10 hari, net-buy terkonsentrasi di sedikit broker vs tersebar, harga rata-rata beli stabil) lalu ujikan *apa adanya*. Tujuannya dua: melihat apakah ada
   edge, atau justru kontrarian (selaras dengan XL: sinyal yang ramai dibaca ritel cenderung berbalik).
9. **Uji bergaya literatur** - (a) persistensi keterampilan: korelasi peringkat broker antar jendela 10 hari berdekatan (jangkar: studi dompet on-chain mendapat 0,52); (b) jackknife
   "leave-one-broker-out": apakah aliran satu broker berbeda nyata dari sisa pasar (jangkar: studi Praha); (c) ukuran: apakah transaksi/aliran *menengah* lebih informatif daripada
   yang sangat besar atau kecil (stealth trading); (d) runtun satu sisi berurutan oleh broker yang sama (jangkar: Frino-Johnstone-Zheng, ASX); (e) sentralitas broker (NBER).

## 5. Definisi hasil (winner / loser)
Utama: return ke depan 1/3/5/10 hari relatif terhadap median universe likuid hari yang sama (netral terhadap regime). Pendamping: hasil jalur
(TP8/SL6/5 hari dan TP10/SL20/20 hari seperti lane), MFE/MAE, dan "plafon tercapai". Tag 'winner/loser' = tercile atas/bawah relatif.

## 6. Protokol validasi (wajib sebelum satu pun dilaporkan)
- Pembagian waktu 3-4 blok **dengan embargo** (jendela hasil tidak boleh tumpang tindih antar blok); temuan harus searah di >=2 blok dan >=2 regime atau
  dilaporkan khusus-regime.
- Bootstrap berkelompok per ticker (dan per minggu) untuk CI; **null permutasi** untuk setiap mesin: laporkan berapa banyak kandidat yang diharapkan secara kebetulan.
- Kendali tebakan-palsu (Benjamini-Hochberg pada p-value permutasi); lantai efek (>=0,5 poin per transaksi relatif) dan n minimum (>=200 pengamatan, >=30 ticker).
- **Inkremental**: harus menambah informasi di dalam sel lane/tag yang sudah ada (mis. di dalam BELI-A/B dan NETRAL), bukan sekadar mengulang lonjakan, regime, atau EXT.
- Kriteria berhenti: bila mesin 1-6 tidak menghasilkan satu pun kandidat yang melewati null permutasi dan replikasi, kesimpulannya "tidak ada indikasi" dan kita berhenti.
- Jaga dari bias: kehadiran = bukan nol (cutoff top-10), kode broker berpindah, hari parsial, look-ahead (fitur hari t -> entry t atau t+1 saja).

## 7. Keluaran
Kartu indikasi per kandidat: definisi, populasi, n, win/rata-rata + CI, replikasi per blok dan regime, kebetulan yang diharapkan, korelasi dengan tag lane, cara
mengeceknya di aplikasi (apa yang dilihat di broker summary / running trade), dan batas keyakinan. Keluaran juga: tabel fitur (`research/ipot_features`) yang bisa dipakai ulang.

## 8. Struktur file & ketergantungan
`research/ipot_explore/` -> `audit.py`, `features.py` (atlas), `sweep.py`, `trees.py`, `archetypes.py`, `broker_skill.py`, `alliances.py`, `events.py`,
`mm_ceiling.py`, `report.py`. Data masuk: `research/ipot_broker_daily.sqlite` (VPS -> laptop setelah backfill selesai), OHLCV, regime, tabel lane.
Pemeriksaan awal dan atlas fitur bisa mulai dengan data kecil di laptop; pencarian sungguhan menunggu backfill 12 bulan selesai.

## 9. Dua tujuan hasil, dua standar bukti (keputusan pengguna 2026-10-22)
**Jalur 1 - Penguat conviction di lane yang sudah ada.** Pertanyaan: apakah fitur broker menaikkan hasil *di dalam* sel lane x regime yang sudah ada
(BELI-A/B/NETRAL), mis. memisahkan pick OSB-v2 yang baik dari yang buruk, atau mengangkat BOW/BOS di BOUNCE_OFF_LOW ke BELI-A (hipotesis lama yang gagal
dengan booster OHLCV/FF: p=0,29 terhadap null). Standar: kenaikan win/rata-rata *di dalam sel* yang replikasi searah di >=2 blok waktu, lolos null permutasi,
n minimum per sub-kelompok >=30. Keluaran: baris informasi atau penanda pada pick (mis. "🔎 broker mendukung/menolak"), tidak mengubah level tag tanpa persetujuan terpisah.
**Jalur 2 - Lane baru murni dari broker summary / bandarmology / market maker.** Standar lebih berat, setara lane produksi: aturan masuk yang sederhana (<=3 syarat),
mekanik keluar tetap (TP/SL/maks hari) disimulasikan seperti lane lain, populasi >=200 transaksi dengan dedup per ticker per episode, hasil per regime (bukan hanya agregat),
replikasi waktu dengan embargo, dan **tidak tumpang tindih** dengan lane yang ada (cek korelasi hari-sinyal; ingat irisan lane terbukti tanpa edge). Lalu masuk ke
tabel scorecard dengan aturan tag yang sama (BELI-A/B butuh n>=30 per sel, win/rata-rata/CI). Lane baru *tidak* dibuat dari ambang yang dioptimalkan pada seluruh data.
Kesimpulan "belum ada lane baru" adalah hasil yang sah.
Kelonggaran "indikasi" (disetujui): untuk kartu indikasi yang dicek manual di aplikasi, replikasi 2 dari 3 blok cukup; tetap wajib lolos null permutasi.

## 10. Jadwal & ketergantungan
- Backfill 12 bulan (150 ticker, 4 varian) sedang berjalan di VPS; setelah selesai, unduh `research/ipot_broker_daily.sqlite` ke laptop.
- **Jumat malam 2026-10-23, setelah `/eodscan` malam selesai:** perpanjang riwayat ke 2024-10-01..2025-09-30 dengan skrip yang sama (resumable),
  perkiraan 8-10 jam; gabungkan DB lama dan baru (`engine/ipot_broker.merge_into`). Hasil: ~24 bulan, mencakup episode crash 2025 dan 2026 sehingga bisa diuji lintas regime.
- Sementara itu, di data kecil laptop: Fase 0 (audit) dan skrip atlas fitur (Fase 1) boleh disiapkan dan diuji.
- Keputusan tampilan ditunda ("kita atur nanti").

## 11. Sumber luar yang sudah dibaca (2026-10-22) dan cara memakainya
**Bahan hipotesis, bukan bukti** (artikel edukasi ritel; sebagian halaman memblokir akses sehingga hanya ringkasan hasil pencarian yang terbaca):
- Ajaib (cara baca akumulasi/bandarmologi): akumulasi jangka panjang, bertahap, biasanya lebih dari satu broker; pembelian dipecah agar harga tidak langsung naik; net-buy terkonsentrasi
  di sedikit broker = akumulasi kuat, tersebar di banyak broker = minat ritel; broker konsisten net-buy berminggu-minggu tanpa harga naik; rata-rata beli relatif stabil; distribusi =
  harga datar/naik tipis, volume tinggi, jual besar asing/institusi, jual tersebar ke banyak broker kecil; distribusi sering berupa letupan pendek setelah berita.
- Stockbit (Bandar Detector / help center): level Neutral / Small / Normal / Big Acc-Dist, tampilan Top 1 / Top 3 / Top 5 / Average (konsentrasi beberapa broker), B.Avg (harga rata-rata
  beli) sebagai perkiraan modal bandar; rumus resmi tidak dipublikasikan. Peringatan mereka sendiri: broker summary hanya salah satu alat dan satu broker melayani banyak investor.
- GitHub `ihkaru/stockbit-explorer`: merekonstruksi rata-rata modal bandar dari broker summary harian 10 hari bursa dan rasio net Top-3 pembeli vs penjual; **tanpa backtest atau validasi**.
- Tidak ditemukan satu pun proyek IDX publik dengan sinyal broker yang di-backtest -> prior publik rendah; mudah populer karena dijual ke ritel, bukan karena terbukti.
**Jangkar akademik (broker-level data)**: Frino, Johnstone & Zheng (ASX, 2010): runtun transaksi satu sisi oleh broker yang sama berdampak permanen besar dan transaksi *ukuran menengah* paling
informatif (stealth trading); studi Helsinki (aliran order broker meramal return, ukuran/gaya broker penting); MPRA 61710 (aliran order broker harian meramal penampang return);
NBER (broker sentral menghasilkan alfa ~40 bps/bulan); studi dompet Hyperliquid (informativitas persisten, korelasi peringkat 0,52 antar jendela 10 hari); Hanousek & Kopriva (Praha,
jackknife leave-one-out untuk perilaku market maker); catatan Elliptic: wash trading tampak sebagai putaran beli-jual berukuran serupa dengan net hampir nol.
Catatan: semua studi itu memakai data transaksi berpengenal broker per order, lebih kaya daripada top-10 harian IPOT; transfer ke data kita bersifat hipotesis.

## 12. Hasil Fase 0 dan Fase 1 pada data dev (2026-10-22; 30 saham likuid x 31 hari, varian all/F/D; file `research/ipot_dev.sqlite`)
Kode: `research/ipot_explore/` (`common.py`, `audit.py`, `features.py`, `test_features.py`); `python -m research.ipot_explore.audit --db ...`, `... .features --db ...`,
`... .test_features` (tes sintetis: batas net, pola rutin/letupan/churn). Data dev diunduh dengan `research/ipot_backfill.py --top 30 --start 2026-09-09 --end 2026-10-21
--variants all/all,F/all,D/all --db research/ipot_dev.sqlite` (2.780 panggilan, ±3,5 menit, tanpa blokir).
Temuan audit:
- **Cakupan top-10 besar**: nilai beli yang tampak median 83% dari nilai transaksi OHLCV (P10 72%, P90 ~102%; >100% karena IPOT mencakup papan negosiasi/tunai), 0% hari di bawah 40%.
  Jadi pada saham likuid, net broker teratas dapat diperkirakan dengan batas yang sempit (jauh lebih baik dari kekhawatiran awal). Perlu dicek ulang pada saham kurang likuid.
- Konsistensi internal baik: val/(lot*100) vs harga rata-rata median 0,24% (pembulatan 0,1 M/B/T); (F + D)/all median 1,000 (P10 0,989, P90 1,005); harga rata-rata beli di luar rentang
  harga hari itu hanya 3,1% baris (kemungkinan papan negosiasi).
- 73 kode broker; broker besar selalu hadir. **Dua-sisi berkorelasi dengan ukuran**: XL 96%, CC 96%, AK 92% hari dua-sisi (median baris dua-sisi 51%). Maka "dua-sisi" TIDAK cukup untuk
  menandai market maker; harus disesuaikan ukuran (net/kotor kecil + kesimetrisan harga + persisten) dan dibandingkan dengan perilaku broker ritel agregat seperti XL.
- Rekonsiliasi dengan RapidAPI belum bisa dijalankan di laptop (baris harian tunggal hanya ada di VPS); jalankan audit di salinan VPS atau salin `broker_flow_history.sqlite`.
Temuan atlas fitur (bentuk distribusi saja, bukan kesimpulan):
- **Definisi pertama "rutin" (top-3 pembeli kotor) jenuh**: median 0,9 dan 85% hari >= 0,6, karena broker besar selalu ada. Diganti `routine_net_score` (top-3 pembeli NET, hanya net>0): median 0,56,
  >= 0,6 pada 42% hari. `routine_gross_*` dipertahankan hanya sebagai kontrol ukuran. Tambahan: `dom_pos_days` (porsi hari akumulator dominan menjadi pembeli net).
- `churn_share` median 0,58 (jenuh oleh broker besar dua-sisi) -> pada tahap analisis pakai z-score terhadap riwayat ticker itu sendiri dan dibanding broker lain, bukan nilai mentah.
- `burst_ratio` median 1,0, >=3 pada 3,9% hari; dibatasi maksimum 20 (basis kecil membuat rasio meledak).
- Hindari tabrakan nama: fitur harga mundur bernama `past_ret_10d`, hasil maju `ret_{1,3,5,10}d` / `rel_*` / `mfe_5d` / `mae_5d` (entry = open t+1).
Berikutnya: mesin 1 (sapuan univariat dengan null permutasi) dan mesin 7 (modul plafon MM) pada data penuh setelah backfill 12 bulan + perpanjangan 2024.

## 13. Mesin 1 (sapuan univariat) - dibangun dan diuji pada data dev (2026-10-22)
Kode: `research/ipot_explore/sweep.py` (`python -m research.ipot_explore.sweep --B 500 [--null shift|date] [--inject FITUR --inject-size 0.06]`).
Semua fitur x potongan (<=P20, <=P33, >=P67, >=P80 atau ==1) x horizon (rel_1d/3d/5d/10d) diuji; kalibrasi dengan null permutasi dan koreksi maxT (Westfall-Young); replikasi tanda di 3 blok tanggal.
Pelajaran yang sekarang tertanam di kode:
1. **Fitur berskala rupiah mentah** (`*_avg`, `*_vis`, `*_mid`, `dom_vwap`) mengidentifikasi saham itu sendiri (tingkat harga, likuiditas) dan "meramal" hanya lewat identitas ticker:
   pada sapuan pertama semua bin teratas adalah harga rata-rata mentah (saham murah kalah di tiga minggu itu). Dikeluarkan; hanya rasio bebas-skala yang disapu.
2. **Null harus menjaga struktur per ticker**: mengacak hasil dalam tanggal (antar ticker) menghasilkan 'temuan' palsu dengan p_adj 0,003 untuk fitur yang melekat per ticker. Default sekarang
   `--null shift`: geser sirkular deret hasil tiap ticker dengan offset acak (autokorelasi dan drift ticker terjaga, kaitannya dengan fitur diputus). Dengan null ini bin teratas p_adj 0,58-0,99 (tidak ada).
3. **Selalu laporkan efek minimum yang terdeteksi (MDE)** lewat kontrol positif. Pada data dev (30 saham x ~21 hari berhasil): suntikan +3% pada rel_5d tidak terdeteksi (t 5,3 tetapi p_adj 0,97),
   +6% terdeteksi (peringkat #1, p_adj 0,003) -> MDE sekitar 5% selisih 5-hari. Data penuh (150 saham x ~24 bulan) harus diuji ulang dengan kontrol positif yang sama sebelum menyimpulkan 'tidak ada efek'.
Hasil pada data dev sendiri: tidak ada bin yang lolos (konsisten dengan data sependek itu); 'tidak ada temuan' di sini BUKAN bukti tidak adanya efek kecil.

## 14. Mesin 7 - modul plafon market maker (dibangun 2026-10-22; `research/ipot_explore/mm_ceiling.py`, tes `test_mm.py`)
Hipotesis pengguna dirumuskan terukur: broker rujukan (MM) -> harga rata-rata jualnya (VWAP jual 10 hari) sebagai plafon mark-up; diuji dengan harga tertinggi 5 dan 10 hari berikutnya,
dipisah menurut ritel sedang keluar (net jual kelas aplikasi ritel).
Temuan metodologis (data dev, 30 saham x ~21 hari berhasil; semuanya deskriptif):
1. **Aturan simetri (dua sisi + lot seimbang + net kecil + spread kecil) TIDAK mengisolasi market maker**: 43% broker besar lolos, didominasi XL, CC, AK, YP, XC, PD (aplikasi ritel dan
   institusi besar), karena semua broker besar memang dua sisi dan seimbang di jendela 10 hari. Dua-sisi mengikuti ukuran.
2. **Sidik jari perilaku memisahkan jenis broker** (korelasi net hari t dengan return hari t, pooled 30 saham): aplikasi ritel kontrarian/pembeli dip (XL -0,24; YP -0,32; XC -0,31), institusi
   asing dan sebagian lain pengejar harga (AK +0,14; YU +0,13; AZ +0,18); korelasi ke return esok ~0. Ini bahan utama klaster perilaku (mesin 3).
3. Mode kedua dibangun: **counterparty** = broker non-ritel yang net-nya bergerak berlawanan dengan net kelas ritel (cp_corr <= -0,30, pangsa >= 4%, hadir >= 70% hari). Pertanyaan yang lebih
   tajam daripada 'siapa MM' adalah 'siapa lawan transaksi ritel'. Hasil dev: yang terpilih AK, CC, ZP, YU, MG, BK (institusi/asing/scalper), bukan aplikasi ritel.
4. **Plasebo pertama saya SALAH** (mengacak jarak acuan antar saham pada tanggal yang sama): memutus kaitan jarak dengan volatilitas saham dan menghasilkan 'plafon' palsu (p=0,003 di jarak 5-10%).
   Diganti `placebo_other_brokers`: bandingkan tingkat sentuh acuan broker rujukan dengan acuan broker besar acak LAIN pada saham-hari yang sama (volatilitas dan jendela sama), per ember jarak.
   Terbukti dengan dua tes sintetis (mendeteksi plafon sungguhan p=0,010; tidak menciptakan plafon bila rujukan hanya broker biasa p=0,91).
5. Hasil dev dengan plasebo benar: tingkat sentuh broker rujukan = broker acak (selisih ~+0,5 sampai +1 poin; p_ref_lower 0,5-0,8; mode counterparty -0,4 poin, p 0,36). **Tidak ada bukti plafon pada data dev**;
   daya uji rendah (n efektif jauh lebih kecil dari 448 baris karena hari-hari tumpang tindih). Ujian sebenarnya pada data penuh 24 bulan, dipisah per regime dan per kelas broker.
Berikutnya: klaster perilaku (mesin 3) untuk kelas broker yang tidak statis, mesin keterampilan broker (mesin 4), dan menjalankan semua mesin pada data penuh dengan kontrol positif/daya (MDE) lebih dulu.
