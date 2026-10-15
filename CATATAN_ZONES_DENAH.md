# Zones: Denah Terminal + Titik CCTV (v11)

## Yang ditambahkan
- Tab **Zones** sekarang menampilkan denah terminal (`static/denah_terminal.jpg`).
- Tiap CCTV = 1 titik di denah. Klik titik -> popup kecil: video live (sudah dengan overlay counting),
  Masuk / Keluar / Di Dalam, lokasi, dan bar kapasitas. Angka ikut update tiap 4 detik.
- Titik kamera **terkunci** secara default (tidak bisa digeser, hanya bisa diklik untuk melihat popup).
- Tombol **Atur Posisi Kamera** membuka kunci. Titik bisa diseret bebas (perubahan belum tersimpan, ditandai garis putus-putus).
  Klik **Apply** untuk menyimpan semua perubahan sekaligus dan mengunci lagi, atau **Batal** untuk membuang perubahan.
- CCTV baru (Live Cameras -> Tambah Unit CCTV) otomatis muncul di denah sebagai titik kuning berkedip
  di strip atas ("belum diposisikan"); tinggal diseret ke lokasinya. CCTV yang dihapus otomatis hilang.

## File yang diubah
| File | Perubahan |
|---|---|
| main.py | +2 kolom `map_x`, `map_y` di tabel `cameras` (migrasi otomatis saat start) dan endpoint `PUT /api/cameras/<id>/map_position` |
| templates/index.html | Isi tab Zones diganti denah interaktif + JS (renderZoneMap, popup, drag) |
| static/denah_terminal.jpg | File baru (denah dari PNG/JPG Anda, margin putih dipotong) |

## Tidak diubah
YOLO, tracking, line crossing, worker kamera, dashboard, laporan/export.

## Catatan
- Popup hanya membuka 1 stream live pada satu waktu dan stream dilepas saat popup ditutup / pindah tab.
- Ganti denah: timpa `static/denah_terminal.jpg` (rasio gambar diatur di `aspect-ratio` pada `#zonemap-stage`).
