# Perbaikan Akurasi Counting (MASUK / KELUAR)

## Diagnosis singkat
Masalahnya **bukan cuma mesin YOLOv8**. YOLO hanya mendeteksi orang; yang menentukan
"masuk/keluar" adalah (1) kualitas input ke YOLO, (2) pelacak ID, dan (3) logika
lewat-garis. Ketiganya punya kelemahan di versi lama.

| # | Penyebab | Dampak | Perbaikan |
|---|----------|--------|-----------|
| 1 | Aturan "satu ID hanya boleh MASUK sekali, dan KELUAR hanya sah setelah MASUK" | Orang yang sudah di dalam sebelum sistem menyala, atau yang keluar-masuk lagi, **tidak pernah terhitung** | Setiap perpindahan sisi garis dihitung sesuai arahnya (`line_counter.py`) |
| 2 | Tidak ada histeresis | Kotak deteksi yang bergetar di garis memicu hitungan palsu / ganda | Dead-band: orang baru dianggap pindah sisi setelah benar-benar melewati zona di sekitar garis |
| 3 | Titik pas di garis dianggap "bukan di sisi mana pun" (titik kamera integer + garis 0.5 = tepat 135 px) | Perlintasan meleset ~15% pada uji simulasi | Titik di zona garis diabaikan lalu dibandingkan dengan titik terakhir di sisi asal |
| 4 | Frame dikecilkan paksa ke 480x270 lalu YOLO `imgsz=256` | Orang jadi kecil & kabur, lebih sering terlewat / ID putus. Frame 4:3 juga jadi gepeng | Rasio asli dipertahankan (maks 960 px lebar), `imgsz=640` |
| 5 | `conf=0.30` + filter `tinggi < 0.9*lebar` dibuang | Orang terhalang / duduk / terpotong hilang sesaat -> track putus -> ID ganti | `conf=0.10` + tracker `tracker_counting.yaml` (ID baru hanya dari deteksi yakin), filter bentuk dilonggarkan jadi 0.6 |
| 6 | Tracker default (buffer 30) | ID mudah ganti saat orang sempat tertutup | `track_buffer=60` |
| 7 | Frame yang sama bisa diproses berulang; render loop ~60x/detik memakan CPU; INSERT MySQL + tulis JPG dilakukan di thread deteksi | FPS AI turun, frame ter-skip tepat saat orang melewati garis | Hanya frame baru diproses, render diringankan, snapshot + DB ditulis di thread terpisah |

## File yang berubah / baru
- `main.py` (diubah) - pipeline deteksi, render, antrean event
- `line_counter.py` (BARU) - logika lewat-garis
- `tracker_counting.yaml` (BARU) - konfigurasi tracker
- `test_line_counter.py` (BARU) - tes logika: `python test_line_counter.py`

Tidak ada dependensi baru. Tabel database tidak berubah. Editor garis di UI tidak perlu diubah.

## Yang tampil di video sekarang
- Panah hijau di garis = sisi yang dihitung **MASUK** (cek apakah arahnya benar).
- Titik merah di tiap orang = titik yang dipakai menghitung.
- Garis biru tipis = jejak lintasan orang.
- `AI x.x fps` di pojok kanan atas. **Ini indikator utama**: kalau merah (< 4 fps),
  penyebabnya kecepatan AI/komputer, bukan logika. Detail lain: `/api/counting_diag`.

## Tuning (environment variable, tanpa edit kode)
Windows: `set NAMA=nilai` lalu `python main.py`.

| Variabel | Default | Kapan diubah |
|----------|---------|--------------|
| `YOLO_MODEL` | `yolov8n.pt` | `yolov8s.pt` / `yolov8m.pt` untuk akurasi lebih tinggi (butuh CPU/GPU lebih kuat; file diunduh otomatis sekali) |
| `DETECT_IMGSZ` | 640 | Turunkan ke 480/416 kalau AI fps terlalu rendah; naikkan ke 800 kalau orang kecil/jauh |
| `COUNT_ANCHOR` | `center` | `bottom` kalau kamera miring dan garis digambar di lantai |
| `COUNT_DEADBAND` | 0.02 | Naikkan (0.03-0.04) kalau masih ada hitungan ganda di garis |
| `DETECT_CONF` | 0.10 | Jarang perlu diubah |
| `SHOW_TRAILS` | 1 | `0` untuk mematikan jejak |

## Saran pemasangan kamera (berpengaruh besar)
- Sub-stream (`subtype=1`) biasanya resolusi sangat rendah. Kalau FPS AI masih cukup,
  coba `subtype=0` (main stream); frame otomatis dikecilkan ke maks 960 px.
- Taruh garis tegak lurus arah jalan orang, di area tanpa halangan, bukan tepat di pintu
  yang sering tertutup daun pintu / orang berkerumun.
- Kamera dari atas (top-down) / sudut tinggi jauh lebih akurat daripada sejajar mata.
