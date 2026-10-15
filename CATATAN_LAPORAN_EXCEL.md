# Laporan Excel (Export XLSX) - versi lengkap

Tombol **Laporan & Export XLSX** di web sekarang menghasilkan workbook baru, dibuat langsung
dari data real-time di database (visitor_logs + cameras) saat tombol ditekan.

File yang berubah: `main.py` (write_report_file memanggil builder, endpoint /export_excel
mengirim alamat server untuk hyperlink foto) dan `report_builder.py` (BARU).

Sheet: DASHBOARD, DATA DETAIL, REKAP, REKAP PER KAMERA, ANALISIS, satu sheet per kamera,
FILTER KAMERA, VALIDASI, REFERENSI. Alur: DATA DETAIL -> REKAP -> ANALISIS/DASHBOARD
(semua angka berupa rumus yang membaca DATA DETAIL).

Pengaturan:
- `SNAPSHOT_MAX_AGE_HOURS` (default 24): foto lebih tua dari ini dihapus otomatis, sehingga
  event lama tertulis "File foto tidak tersedia". Naikkan bila foto perlu bisa dibuka di laporan lama.
- `REPORT_BASE_URL` (opsional, mis. http://192.168.1.10:5000/): alamat dasar hyperlink foto pada
  laporan otomatis terjadwal. Untuk unduhan lewat web, alamat diambil otomatis dari browser.

## Audit analisis per kamera (sheet ANALISIS, bagian C)
- Alur data diperiksa: `camera_detection_worker` -> `enqueue_count_event(cam_id)` -> `log_visitor(camera_id)`.
  Tiap kamera punya counter, tracker, dan model sendiri; `camera_id` tidak tercampur antarkamera.
- Rumus ANALISIS membaca kolom matriks kamera masing-masing (B, C, D, ...), bukan satu hasil bersama.
- Perbaikan: rasio MASUK:KELUAR memakai TEXT(...,"0.00") yang bergantung locale (di WPS/Excel
  Indonesia tampil "004 : 1"); diganti ROUND(...). Ditambah kolom ID Tracking Unik & Event per ID,
  aturan seri (jam paling awal) yang didokumentasikan, dan VALIDASI bagian C (penghitung kumulatif vs event).
- Tes: `python test_report_analysis.py` (butuh LibreOffice). Data sintetis hanya untuk tes.

## Jam Sepi: jam parsial tidak ikut dipilih (v3)
Pada data 09-10-2026 semua kamera baru menyala 08:54, sehingga jam 08:00 hanya berjalan ~6 menit
dan selalu tampak "paling sepi". Sekarang jam saat event pertama & terakhir (per kamera, dan untuk
semua kamera) dikeluarkan dari pemilihan Jam Sepi. Jam Ramai tidak berubah. Data bantu ada di
ANALISIS bagian D; waktu event pertama/terakhir tiap kamera tampil di bagian C.
Tes: `python test_report_analysis.py` (10 tes, termasuk kasus nyata menyala 08:54).

## v4 — Jam ramai/sepi per kamera dihitung di Python + sheet AUDIT KAMERA
- ANALISIS bagian C kini ditulis dari `compute_camera_stats()` (Python, per kamera dari data sumber), bukan rantai rumus
  yang bergantung hitung-ulang WPS/Excel. Blok bantu "D. jam parsial" dihapus.
- Jam sepi = event terendah di antara jam yang punya event (jam parsial TIDAK dikeluarkan lagi; ditandai di AUDIT KAMERA).
  Seri -> jam paling awal.
- Sheet baru AUDIT KAMERA: 24 jam per kamera, ramai/sepi, masuk/keluar, selisih terhadap rumus COUNTIFS, status validasi.
- Data uji `cctv_counting.db` di zip ini hanyalah SQLite lama (1 kamera, camera_id NULL), bukan data produksi MySQL.

## v5 — REKAP JAM per kamera, grafik, heatmap
- Temuan: angka 0 pada REKAP JAM lama bukan kesalahan rumus. Data 09-10-2026 hanya ada pukul 08–14 (1.194 event); jam
  00–07 dan 15–23 memang tanpa event. Total tabel jam = DATA DETAIL = REKAP = DASHBOARD.
- REKAP bagian IV: 24 jam × setiap kamera × setiap tanggal (No, Tanggal, Jam, Kamera, Masuk, Keluar, Total Event, Total Orang).
  Bagian V: gabungan "Semua Kamera" (jangan dijumlahkan dengan IV). VALIDASI membandingkan IV dan V.
- DASHBOARD: grafik tren jam (garis Masuk/Keluar), per kamera (Masuk/Keluar/Total Event), per tanggal, dan heatmap jam × kamera;
  semua mengikuti filter Kamera/Status/Tanggal/Jam di bagian atas.
- Workbook memakai fullCalcOnLoad: Excel/WPS menghitung ulang semua rumus saat dibuka.
- Tes: `python test_report_analysis.py` (14 tes).

## v6 — Filter kamera + diagram batang dinamis
- DASHBOARD: dropdown Kamera/Status/Tanggal/Jam mengendalikan 4 diagram batang (per jam, per hari, per kamera, Masuk vs Keluar);
  judul diagram terhubung ke sel (mis. "Diagram People Counting — cctv aglo (per Jam)").
- REKAP bagian IV (J:O; bagian IV/V lama per kamera dihapus, diganti filter): filter sendiri + tabel 24 jam + diagram batang dinamis.
- Daftar dropdown di REFERENSI!H (dengan slot kosong untuk kamera baru). Baris cadangan DATA DETAIL mengisi Tanggal/Waktu/Jam otomatis.
- Opsi `default_camera` pada build_report_workbook. Tes: 15 tes di test_report_analysis.py.
