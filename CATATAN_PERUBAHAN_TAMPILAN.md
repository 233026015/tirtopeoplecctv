# Perubahan Tampilan (mengikuti desain tirtopeople.pdf)

Hanya tampilan yang diubah. Backend (main.py), YOLO, tracking, line crossing, worker kamera,
laporan histori 30 hari dan export XLSX TIDAK disentuh.

## File yang berubah
| File | Perubahan |
|---|---|
| templates/index.html | Tema terang navy + emas, sidebar Kemenhub, header, Dashboard, Live Cameras, Zones, Settings, modal, grafik |
| static/logo-kemenhub.png | BARU - logo Kemenhub (diambil dari PDF, latar transparan) |

## Yang disesuaikan dengan PDF
- Sidebar navy (#0b2035) + logo + "RUANG KENDALI", menu aktif bergaris emas, kartu "Pemantauan terintegrasi".
- Header putih: pencarian (shortcut Ctrl/⌘ + K), tombol "Laporan & ekspor XLSX", profil.
- Dashboard: judul + tanggal otomatis, banner sesi, "Cakupan pemantauan", 4 kartu KPI, grafik "Pergerakan orang"
  (Masuk biru / Keluar emas), "Okupansi zona", tabel "Jumlah orang per CCTV", "Pemantauan langsung", "Log peristiwa terbaru".
- Live Cameras: bar ringkasan, kartu kamera (kartu terakhir yang ganjil melebar), tombol Tautan / Edit / Hapus.
- Zones & Settings: judul + eyebrow emas, kartu putih.
- Semua modal (tambah/edit CCTV, atur garis counting, laporan, riwayat target) ikut tema terang.

## Keputusan yang perlu kamu tahu
- Teks dari PDF yang hanya artefak "tampilan statis" TIDAK dipakai karena akan salah di aplikasi live:
  "Cuplikan dari dashboard sumber; bukan koneksi video aktif", "Pratinjau statis dari sesi sumber", dsb.
- Link "Lihat log →" di kartu log tidak dibuat karena belum ada halaman log di aplikasi.
- Banner sesi tetap menampilkan hitung mundur laporan (fungsi lama), teksnya: "Sesi penghitungan aktif — laporan otomatis tercatat dalam [timer]".
- Tombol "Tautan" di kartu kamera memakai fungsi lama "Atur Garis & Label Counting" (ikon penggaris -> klip kertas).
- Halaman login diubah mengikuti gambar login (lihat templates/login.html).
