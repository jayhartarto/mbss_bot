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

## 9. Keputusan terbuka
- Perpanjang riwayat ke 2024 (lebih banyak regime, +sekitar 8-10 jam di VPS) sebelum kesimpulan akhir?
- Tampilan: hanya baris informasi `/check`, atau juga masuk bundel `/tanya`?
- Seberapa jauh aturan "indikasi" boleh lebih longgar dari aturan tag (mis. replikasi 2 dari 3 blok)?
