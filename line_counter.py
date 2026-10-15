"""
line_counter.py - Logika hitung lewat-garis (MASUK / KELUAR) yang tahan banting.

Kenapa dipisah dari main.py?
  Supaya logika ini bisa diuji sendiri (tanpa kamera / YOLO / MySQL) dan
  gampang dituning. File ini murni Python (tanpa dependensi).

Perbedaan utama dengan logika lama (line_segment_crossing + id_status):

  1. HISTERESIS / DEAD-BAND
     Titik orang yang berada dalam jarak <= `deadband` piksel dari garis
     DIABAIKAN (tidak dianggap di sisi mana pun). Orang baru dianggap
     pindah sisi kalau sudah benar-benar melewati dead-band. Ini mencegah:
       - hitungan ganda/palsu saat kotak deteksi "bergetar" di dekat garis
       - orang yang berdiri di garis ke-hitung bolak-balik

  2. TIDAK ADA LAGI "SATU ID = SATU MASUK SEUMUR HIDUP"
     Dulu, ID yang pertama kali terlihat melintas ke arah KELUAR diabaikan
     total (dianggap anomali), dan ID yang sudah KELUAR tidak pernah bisa
     dihitung lagi. Akibatnya: orang yang sudah ada di dalam sebelum sistem
     menyala, atau yang keluar-masuk lagi, TIDAK terhitung. Sekarang setiap
     perpindahan sisi yang valid dihitung sesuai arahnya.

  3. TITIK AWAL = TITIK TERAKHIR DI SISI ASAL (bukan cuma frame sebelumnya)
     Pengecekan potong-garis memakai ruas dari "titik terakhir yang valid di
     sisi asal" ke "titik pertama yang valid di sisi tujuan". Jadi kalau ada
     frame yang jatuh di dead-band / deteksi sempat hilang beberapa frame,
     perlintasan tetap terdeteksi.

  4. GARIS PENDEK TETAP DIHORMATI
     Perlintasan hanya dihitung bila titik potongnya jatuh di sepanjang ruas
     garis yang digambar (dengan toleransi kecil di ujung), jadi orang yang
     lewat memutar di luar ujung garis tidak ikut terhitung.

  5. TRACK BASI DIREFRESH
     Kalau suatu ID tidak terlihat > `stale_seconds` (mis. stream putus lalu
     nyambung lagi), posisi lamanya dibuang, supaya lompatan posisi akibat
     jeda tidak dihitung sebagai perlintasan.
"""

import math
import threading
import time


class LineCounter:
    IN = "IN"
    OUT = "OUT"

    def __init__(self, deadband_frac=0.02, end_tolerance=0.08,
                 stale_seconds=3.0, forget_seconds=30.0):
        """
        deadband_frac : lebar dead-band = deadband_frac * tinggi frame (piksel)
        end_tolerance : toleransi di ujung garis, pecahan dari panjang garis
        stale_seconds : ID tak terlihat selama ini -> posisi lamanya dibuang
        forget_seconds: ID tak terlihat selama ini -> state-nya dihapus (hemat memori)
        """
        self.deadband_frac = deadband_frac
        self.end_tolerance = end_tolerance
        self.stale_seconds = stale_seconds
        self.forget_seconds = forget_seconds

        self._lock = threading.Lock()
        self._line_norm = (0.0, 0.5, 1.0, 0.5)
        self.flip = False
        self._frame_w = 0
        self._frame_h = 0
        self._p1 = (0.0, 0.0)
        self._p2 = (1.0, 0.0)
        self._len = 1.0
        self._deadband = 4.0
        # track_id -> {"side": +1/-1/None, "anchor": (x, y), "seen": float}
        self._tracks = {}

    # ------------------------------------------------------------------ setup
    def set_line_norm(self, x1, y1, x2, y2, flip=False):
        """Atur garis dalam koordinat ternormalisasi 0..1 (sama seperti yang
        disimpan di database & digambar di UI). Mengosongkan state track,
        karena posisi lama tidak relevan lagi terhadap garis yang baru."""
        with self._lock:
            self._line_norm = (float(x1), float(y1), float(x2), float(y2))
            self.flip = bool(flip)
            self._tracks.clear()
            self._recompute_geometry()

    def set_flip(self, flip):
        with self._lock:
            self.flip = bool(flip)

    def set_frame_size(self, w, h):
        """Panggil tiap frame (murah). Kalau resolusi berubah, geometri
        dihitung ulang & state track dikosongkan."""
        if w == self._frame_w and h == self._frame_h:
            return
        with self._lock:
            self._frame_w, self._frame_h = int(w), int(h)
            self._tracks.clear()
            self._recompute_geometry()

    def _recompute_geometry(self):
        w, h = self._frame_w, self._frame_h
        if w <= 0 or h <= 0:
            return
        x1, y1, x2, y2 = self._line_norm
        self._p1 = (x1 * w, y1 * h)
        self._p2 = (x2 * w, y2 * h)
        self._len = max(1e-6, math.hypot(self._p2[0] - self._p1[0], self._p2[1] - self._p1[1]))
        self._deadband = max(4.0, self.deadband_frac * h)

    def reset(self):
        """Kosongkan semua state track (dipakai saat sesi counting baru)."""
        with self._lock:
            self._tracks.clear()

    # --------------------------------------------------------------- geometry
    def _signed_distance(self, pt):
        (ax, ay), (bx, by) = self._p1, self._p2
        cross = (bx - ax) * (pt[1] - ay) - (by - ay) * (pt[0] - ax)
        return cross / self._len

    def _crossing_inside_segment(self, a, b):
        """Titik potong ruas a->b dengan GARIS (p1->p2): jatuh di dalam ruas
        garis yang digambar (plus toleransi ujung)?"""
        (p1x, p1y), (p2x, p2y) = self._p1, self._p2
        rx, ry = p2x - p1x, p2y - p1y
        sx, sy = b[0] - a[0], b[1] - a[1]
        denom = rx * sy - ry * sx
        if abs(denom) < 1e-9:
            return False
        apx, apy = a[0] - p1x, a[1] - p1y
        t = (apx * sy - apy * sx) / denom   # posisi di sepanjang garis (0 = p1, 1 = p2)
        return -self.end_tolerance <= t <= 1.0 + self.end_tolerance

    # ----------------------------------------------------------------- update
    def update(self, track_id, point, now=None):
        """
        Masukkan posisi terbaru satu orang. Return:
          "IN"  -> baru saja melintas ke arah MASUK
          "OUT" -> baru saja melintas ke arah KELUAR
          None  -> tidak ada perlintasan
        `point` dalam piksel frame yang sama dengan set_frame_size().
        """
        if now is None:
            now = time.time()
        if self._frame_w <= 0:
            return None

        with self._lock:
            d = self._signed_distance(point)
            st = self._tracks.get(track_id)

            # Track lama yang sudah basi (mis. stream sempat putus) -> mulai ulang
            if st is not None and now - st["seen"] > self.stale_seconds:
                st["side"] = None
                st["anchor"] = None
            if st is None:
                st = {"side": None, "anchor": None, "seen": now}
                self._tracks[track_id] = st
            st["seen"] = now

            # Di dalam dead-band: abaikan (belum jelas di sisi mana)
            if abs(d) <= self._deadband:
                return None

            side = 1 if d > 0 else -1

            if st["side"] is None:
                st["side"], st["anchor"] = side, point
                return None

            if side == st["side"]:
                st["anchor"] = point
                return None

            # Pindah sisi (sudah melewati dead-band). Valid kalau memotong
            # RUAS garis yang digambar, bukan memutar di luar ujungnya.
            anchor = st["anchor"] or point
            crossed = self._crossing_inside_segment(anchor, point)
            st["side"], st["anchor"] = side, point
            if not crossed:
                return None

            # side > 0 = tujuan di sisi "kanan" garis p1->p2 = MASUK (sama
            # seperti konvensi lama); flip membalik arah.
            is_in = (side > 0) != self.flip
            return self.IN if is_in else self.OUT

    # ------------------------------------------------------------------ prune
    def prune(self, now=None):
        """Buang state track yang sudah lama tidak terlihat."""
        if now is None:
            now = time.time()
        with self._lock:
            dead = [tid for tid, st in self._tracks.items() if now - st["seen"] > self.forget_seconds]
            for tid in dead:
                del self._tracks[tid]

    @property
    def active_tracks(self):
        with self._lock:
            return len(self._tracks)
