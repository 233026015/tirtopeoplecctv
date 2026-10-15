import os
import re
import io
import secrets
import cv2
import numpy as np
import time
import threading
import queue
import urllib.parse
from collections import deque
from functools import wraps
from datetime import datetime, timedelta
import pandas as pd
import mysql.connector
from mysql.connector import pooling as mysql_pooling
from mysql.connector import Error as MySQLError
from flask import Flask, render_template, Response, send_file, jsonify, request, redirect, url_for, session
from auth_google import (init_auth, ensure_auth_schema, admin_required, start_session,
                         google_enabled, is_admin_session, authenticate_local, note_login_fail, ROLE_USER)
from auth_security import captcha_ok
from werkzeug.security import generate_password_hash
from line_counter import LineCounter
import report_builder

# 1. Transport TCP FFmpeg agar stream CCTV anti-freeze & stabil
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|max_delay;500000"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# ==============================================================================
# PARAMETER AKURASI COUNTING
# Semua bisa diubah lewat environment variable TANPA mengedit kode, mis. di
# Windows:  set DETECT_IMGSZ=800   lalu   python main.py
# ==============================================================================
# Resolusi input YOLO. Dulu 256 (frame sudah dikecilkan ke 480x270 lebih dulu),
# sehingga orang jadi terlalu kecil & kabur untuk dideteksi dan dilacak.
DETECT_IMGSZ = int(os.environ.get("DETECT_IMGSZ", 640))
# Frame dari kamera hanya diperkecil kalau lebih lebar dari ini (rasio asli
# DIPERTAHANKAN - dulu dipaksa 480x270 sehingga orang jadi gepeng/melar).
DETECT_MAX_WIDTH = int(os.environ.get("DETECT_MAX_WIDTH", 960))
# conf rendah SENGAJA: ByteTrack memakai deteksi berskor rendah untuk
# mempertahankan orang yang sebagian tertutup. Track BARU hanya dibuat dari
# deteksi yang yakin (lihat tracker_counting.yaml: new_track_thresh).
DETECT_CONF = float(os.environ.get("DETECT_CONF", 0.10))
DETECT_NMS_IOU = float(os.environ.get("DETECT_NMS_IOU", 0.60))
# Titik di tubuh yang dipakai untuk menentukan "melewati garis":
#   center = tengah kotak (default, sama seperti versi lama)
#   bottom = tengah-bawah kotak (kaki). Cocok kalau garis digambar di lantai
#            dan kamera menghadap miring (bukan dari atas).
COUNT_ANCHOR = os.environ.get("COUNT_ANCHOR", "center").lower()
# Lebar dead-band histeresis, pecahan dari tinggi frame (0.02 = 2%).
COUNT_DEADBAND = float(os.environ.get("COUNT_DEADBAND", 0.02))
# Filter bentuk kotak: dulu tinggi < 0.9*lebar dibuang (terlalu galak, orang
# yang duduk/jongkok/terpotong/dilihat dari atas ikut terbuang -> track putus).
MIN_BOX_ASPECT = float(os.environ.get("MIN_BOX_ASPECT", 0.6))
MIN_BOX_HEIGHT_FRAC = float(os.environ.get("MIN_BOX_HEIGHT_FRAC", 0.04))
TRACKER_CFG = os.path.join(BASE_DIR, "tracker_counting.yaml")
LOW_FPS_WARN = float(os.environ.get("LOW_FPS_WARN", 4.0))
# Ukuran frame yang DITAMPILKAN di dashboard (tidak memengaruhi deteksi).
RENDER_W, RENDER_H = 640, 360
SHOW_TRAILS = os.environ.get("SHOW_TRAILS", "1") != "0"

app = Flask(__name__)


def _load_secret_key():
    """Kunci tanda tangan sesi. Urutan: env SECRET_KEY -> file .secret_key (dibuat acak otomatis).
    Dulu kunci tertulis di kode sehingga siapa pun yang punya kodenya bisa memalsukan sesi admin."""
    env = os.environ.get("SECRET_KEY", "").strip()
    if env:
        return env
    path = os.path.join(BASE_DIR, ".secret_key")
    try:
        with open(path, "r", encoding="utf-8") as f:
            key = f.read().strip()
            if key:
                return key
    except FileNotFoundError:
        pass
    key = secrets.token_hex(32)
    with open(path, "w", encoding="utf-8") as f:
        f.write(key)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


app.secret_key = _load_secret_key()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # Set COOKIE_SECURE=1 bila web diakses lewat HTTPS (disarankan di produksi)
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1",
)
# Di belakang Nginx/reverse proxy (HTTPS): set TRUST_PROXY=1 agar redirect Google memakai https & domain yang benar.
if os.environ.get("TRUST_PROXY") == "1":
    from werkzeug.middleware.proxy_fix import ProxyFix
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

# ==============================================================================
# KONFIGURASI DATABASE MySQL (LOCALHOST — XAMPP/Laragon + phpMyAdmin)
# ==============================================================================
# Sesuaikan bagian ini kalau setup MySQL Anda beda (mis. root pakai password,
# atau port MySQL bukan 3306). Untuk XAMPP/Laragon default biasanya:
# user='root', password='' (kosong), host='localhost', port=3306.
DB_CONFIG = {
    "host": "localhost",
    "port": 3306,
    "user": "root",
    "password": "",
    "database": "tirtopeople_counting",
    "connection_timeout": 8  # PENTING: tanpa ini, kalau MySQL mati/tidak bisa
                             # dihubungi, percobaan konek bisa menggantung
                             # TANPA BATAS WAKTU (bisa berjam-jam) tanpa pesan
                             # error apa pun - persis gejala "main.py freeze
                             # dari detik pertama, gak ada tulisan sama sekali"
                             # yang sebelumnya terjadi.
}

# ==============================================================================
# CONNECTION POOL (bukan buka koneksi baru dari nol tiap query)
# ==============================================================================
# Sebelumnya SETIAP query (dari puluhan thread kamera yang nulis log/snapshot
# tiap detik, ditambah setiap request web) buka koneksi TCP+auth MySQL baru
# dari nol lalu ditutup lagi. Selain lambat, ini juga bisa membuat MySQL
# kehabisan slot koneksi kalau banyak thread aktif bersamaan - dan kalau itu
# terjadi, permintaan berikutnya (termasuk klik tombol "Simpan" di CRUD
# kamera) bisa ikut menggantung menunggu slot koneksi kosong, terlihat
# seperti tombol "ngebug"/tidak merespons. Connection pool membatasi & pakai
# ulang koneksi secara efisien supaya ini tidak lagi jadi masalah.
_db_pool = None
_db_pool_lock = threading.Lock()

def _get_pool():
    global _db_pool
    with _db_pool_lock:
        if _db_pool is None:
            _db_pool = mysql_pooling.MySQLConnectionPool(
                pool_name="tirtopeople_pool",
                pool_size=20,
                pool_reset_session=True,
                **DB_CONFIG
            )
        return _db_pool

def get_db_connection():
    """Ambil koneksi dari pool (dipakai ulang, bukan bikin baru tiap saat).
    Pemanggilan tetap sama seperti sebelumnya (conn = get_db_connection()),
    cukup jangan lupa conn.close() seperti biasa - untuk koneksi dari pool,
    .close() berarti 'kembalikan ke pool', bukan benar-benar menutup TCP-nya."""
    try:
        return _get_pool().get_connection()
    except MySQLError as e:
        # Pool penuh/habis (semua 20 slot sedang dipakai) -> daripada request
        # menggantung tanpa penjelasan, buka 1 koneksi langsung di luar pool
        # sebagai fallback supaya tetap bisa jalan (agak lebih lambat, tapi
        # tidak macet total).
        print(f"[DB][WARN] Pool penuh/gagal ({e}), fallback ke koneksi langsung.")
        return mysql.connector.connect(**DB_CONFIG)

def ensure_database_exists():
    """
    Dijalankan sekali di awal: connect ke MySQL server TANPA memilih database
    dulu, lalu buat database 'tirtopeople_counting' kalau belum ada. Jadi
    Anda tidak perlu bikin database manual dulu di phpMyAdmin — cukup pastikan
    service MySQL di XAMPP/Laragon sudah dinyalakan.
    """
    conf = DB_CONFIG.copy()
    db_name = conf.pop("database")
    print(f"[DB] Mencoba konek ke MySQL localhost:{conf.get('port')} (timeout {conf.get('connection_timeout')}s)...")
    try:
        conn = mysql.connector.connect(**conf)
        cursor = conn.cursor()
        cursor.execute(f"CREATE DATABASE IF NOT EXISTS `{db_name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        conn.commit()
        cursor.close()
        conn.close()
        print(f"[DB] Database '{db_name}' siap dipakai di MySQL localhost.")
    except MySQLError as e:
        print(f"[DB][FATAL] Tidak bisa konek ke MySQL localhost: {e}")
        print("[DB][FATAL] Pastikan XAMPP/Laragon sudah jalan dan service MySQL AKTIF.")
        raise

ensure_database_exists()

DB_NAME = DB_CONFIG["database"]  # dipertahankan supaya referensi lama tidak error

# ==============================================================================
# KONFIGURASI LAPORAN OTOMATIS (Kebutuhan #6 & #7)
# ==============================================================================
# Jika folder Downloads OS tersedia, laporan otomatis akan disimpan langsung
# ke sana. Kalau tidak ada (mis. jalan di server Linux tanpa folder Downloads),
# akan fallback ke folder lokal "auto_reports" di dalam project.
_downloads_dir = os.path.join(os.path.expanduser("~"), "Downloads")
AUTO_REPORT_DIR = _downloads_dir if os.path.isdir(_downloads_dir) else os.path.join(os.getcwd(), "auto_reports")
os.makedirs(AUTO_REPORT_DIR, exist_ok=True)

SESSION_ACTIVE_DURATION = 23 * 3600   # 23 jam sesi counting aktif
SESSION_PAUSE_DURATION = 1 * 3600     # 1 jam jeda sebelum sesi baru dimulai lagi

# Folder penyimpanan screenshot otomatis tiap orang tercatat MASUK/KELUAR
# (Kebutuhan: bisa lihat wujud orang dari ID di log tanpa perlu buka rekaman)
SNAPSHOT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static", "snapshots")
os.makedirs(SNAPSHOT_DIR, exist_ok=True)

# Screenshot otomatis dihapus setelah 24 jam supaya folder tidak menumpuk
SNAPSHOT_MAX_AGE_HOURS = float(os.environ.get("SNAPSHOT_MAX_AGE_HOURS", 24))  # naikkan agar foto laporan lama tetap bisa dibuka
SNAPSHOT_CLEANUP_INTERVAL_SECONDS = 30 * 60  # cek tiap 30 menit

def cleanup_old_snapshots():
    """
    Berjalan terus di background: setiap SNAPSHOT_CLEANUP_INTERVAL_SECONDS,
    cek semua file di static/snapshots -> hapus yang usianya sudah lebih
    dari SNAPSHOT_MAX_AGE_HOURS jam, lalu bersihkan juga referensinya di
    visitor_logs (snapshot_path -> NULL) supaya UI tidak menampilkan foto
    rusak/hilang.
    """
    while True:
        try:
            now = time.time()
            max_age_seconds = SNAPSHOT_MAX_AGE_HOURS * 3600
            deleted_files = []

            for fname in os.listdir(SNAPSHOT_DIR):
                fpath = os.path.join(SNAPSHOT_DIR, fname)
                if not os.path.isfile(fpath):
                    continue
                file_age = now - os.path.getmtime(fpath)
                if file_age > max_age_seconds:
                    try:
                        os.remove(fpath)
                        deleted_files.append(fname)
                    except Exception as e:
                        print(f"[CLEANUP][ERROR] Gagal hapus {fname}: {e}")

            if deleted_files:
                conn = get_db_connection()
                cursor = conn.cursor()
                placeholders = ",".join(["%s"] * len(deleted_files))
                cursor.execute(
                    f"UPDATE visitor_logs SET snapshot_path = NULL WHERE snapshot_path IN ({placeholders})",
                    deleted_files
                )
                conn.commit()
                cursor.close()
                conn.close()
                print(f"[CLEANUP] {len(deleted_files)} screenshot lama (>{SNAPSHOT_MAX_AGE_HOURS} jam) dihapus otomatis, referensi DB dibersihkan.")
        except Exception as e:
            print(f"[CLEANUP][ERROR] {e}")

        time.sleep(SNAPSHOT_CLEANUP_INTERVAL_SECONDS)

threading.Thread(target=cleanup_old_snapshots, daemon=True).start()

def save_person_snapshot(frame, box, camera_id, track_id, status):
    """
    Crop area orang dari frame saat melintasi garis hitung, lalu simpan sebagai
    file JPG kecil di static/snapshots. Mengembalikan nama file (bukan path
    lengkap) supaya bisa diakses lewat /static/snapshots/<nama_file>.
    """
    try:
        if frame is None:
            print("[SNAPSHOT][ERROR] Frame kosong, snapshot dilewati.")
            return None

        x1, y1, x2, y2 = box
        h, w = frame.shape[:2]
        pad = 8
        x1c = max(0, int(x1) - pad)
        y1c = max(0, int(y1) - pad)
        x2c = min(w, int(x2) + pad)
        y2c = min(h, int(y2) + pad)

        if x2c <= x1c or y2c <= y1c:
            print(f"[SNAPSHOT][ERROR] Koordinat crop tidak valid: ({x1c},{y1c})-({x2c},{y2c})")
            return None

        crop = frame[y1c:y2c, x1c:x2c].copy()
        if crop.size == 0:
            print("[SNAPSHOT][ERROR] Hasil crop kosong (size 0).")
            return None

        ts = datetime.now().strftime("%Y%m%d_%H%M%S%f")
        filename = f"cam{camera_id}_id{track_id}_{status}_{ts}.jpg"
        filepath = os.path.join(SNAPSHOT_DIR, filename)

        ok = cv2.imwrite(filepath, crop, [cv2.IMWRITE_JPEG_QUALITY, 85])
        if not ok:
            print(f"[SNAPSHOT][ERROR] cv2.imwrite gagal menulis ke: {filepath}")
            return None

        print(f"[SNAPSHOT] Tersimpan: {filepath}")
        return filename
    except Exception as e:
        print(f"[SNAPSHOT][ERROR] Gagal menyimpan snapshot: {e}")
        return None

# Status sesi counting (dipakai oleh worker AI & endpoint /api/session_status)
session_state = {
    "active": True,
    "phase_started_at": datetime.now(),
    "next_change_at": datetime.now() + timedelta(seconds=SESSION_ACTIVE_DURATION),
    "last_auto_report": None
}
counting_active = threading.Event()
counting_active.set()  # mulai dalam kondisi aktif menghitung

# Target ID yang sedang ditandai/dicari lewat fitur search (Kebutuhan #3)
highlight_state = {"target_id": None}
highlight_lock = threading.Lock()

# ==============================================================================
# DATABASE SCHEMA & MIGRATIONS
# ==============================================================================
def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    
    # Tabel User Login
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INT AUTO_INCREMENT PRIMARY KEY,
            username VARCHAR(100) UNIQUE NOT NULL,
            password VARCHAR(255) NOT NULL,
            full_name VARCHAR(150) NOT NULL,
            role VARCHAR(50) DEFAULT 'Operator'
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)
    
    # Tabel Logs Visitor
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS visitor_logs (
            id INT AUTO_INCREMENT PRIMARY KEY,
            timestamp DATETIME,
            person_id INT,
            status VARCHAR(20),
            total_in INT,
            total_out INT,
            camera_id INT,
            snapshot_path VARCHAR(255)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)

    # Migrasi ringan: pastikan kolom camera_id & snapshot_path ada meski DB lama belum punya
    cursor.execute("""
        SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'visitor_logs'
    """, (DB_CONFIG["database"],))
    existing_cols = [row[0] for row in cursor.fetchall()]
    if "camera_id" not in existing_cols:
        cursor.execute("ALTER TABLE visitor_logs ADD COLUMN camera_id INT")
    if "snapshot_path" not in existing_cols:
        cursor.execute("ALTER TABLE visitor_logs ADD COLUMN snapshot_path VARCHAR(255)")

    # Index pada kolom timestamp supaya query laporan berperiode (30 hari / lebih)
    # tetap cepat walau visitor_logs sudah berisi banyak event. Aman: hanya dibuat
    # kalau belum ada, dan kegagalan di sini tidak boleh menghentikan aplikasi.
    try:
        cursor.execute("""
            SELECT COUNT(*) FROM INFORMATION_SCHEMA.STATISTICS
            WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'visitor_logs'
              AND COLUMN_NAME = 'timestamp' AND SEQ_IN_INDEX = 1
        """, (DB_CONFIG["database"],))
        if cursor.fetchone()[0] == 0:
            cursor.execute("CREATE INDEX idx_visitor_logs_timestamp ON visitor_logs (timestamp)")
    except Exception as e:
        print(f"[DB][WARN] Index timestamp tidak dibuat (laporan tetap jalan): {e}")
    
    # Tabel Kamera CCTV (CRUD)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS cameras (
            id INT AUTO_INCREMENT PRIMARY KEY,
            name VARCHAR(150) NOT NULL,
            location VARCHAR(150) NOT NULL,
            brand VARCHAR(100) DEFAULT 'Generic',
            ip_address VARCHAR(50) NOT NULL,
            port INT DEFAULT 554,
            username VARCHAR(100),
            password VARCHAR(255),
            stream_path VARCHAR(255),
            is_active TINYINT DEFAULT 1,
            created_at DATETIME,
            line_position FLOAT DEFAULT 0.5,
            label_masuk VARCHAR(50) DEFAULT 'MASUK',
            label_keluar VARCHAR(50) DEFAULT 'KELUAR',
            count_direction VARCHAR(20) DEFAULT 'down_in',
            max_capacity INT DEFAULT 100
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)

    # Migrasi ringan: pastikan kolom pengaturan counting ada meski tabel lama belum punya
    cursor.execute("""
        SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS
        WHERE TABLE_SCHEMA = %s AND TABLE_NAME = 'cameras'
    """, (DB_CONFIG["database"],))
    existing_cam_cols = [row[0] for row in cursor.fetchall()]
    cam_migrations = {
        "line_position": "ALTER TABLE cameras ADD COLUMN line_position FLOAT DEFAULT 0.5",
        "label_masuk": "ALTER TABLE cameras ADD COLUMN label_masuk VARCHAR(50) DEFAULT 'MASUK'",
        "label_keluar": "ALTER TABLE cameras ADD COLUMN label_keluar VARCHAR(50) DEFAULT 'KELUAR'",
        "count_direction": "ALTER TABLE cameras ADD COLUMN count_direction VARCHAR(20) DEFAULT 'down_in'",
        "max_capacity": "ALTER TABLE cameras ADD COLUMN max_capacity INT DEFAULT 100",
        # Garis counting FLEKSIBEL 360 derajat: disimpan sebagai 2 titik ujung
        # (x1,y1) -> (x2,y2), masing-masing pecahan 0.0-1.0 dari lebar/tinggi
        # frame. line_position (atas) TETAP DIPERTAHANKAN untuk kompatibilitas
        # kamera lama yang belum pernah diatur ulang garisnya - saat sebuah
        # kamera belum punya line_x1..line_y2 yang valid, sistem menurunkan
        # garis horizontal default dari line_position lama (lihat kode
        # pemakaiannya di bawah).
        "line_x1": "ALTER TABLE cameras ADD COLUMN line_x1 FLOAT DEFAULT NULL",
        "line_y1": "ALTER TABLE cameras ADD COLUMN line_y1 FLOAT DEFAULT NULL",
        "line_x2": "ALTER TABLE cameras ADD COLUMN line_x2 FLOAT DEFAULT NULL",
        "line_y2": "ALTER TABLE cameras ADD COLUMN line_y2 FLOAT DEFAULT NULL",
        # Tag bebas - label pendek yang bisa diisi sesuka admin (mis. "VIP",
        # "Zona A", nomor gate, dll), ditampilkan di kartu kamera.
        "tag_name": "ALTER TABLE cameras ADD COLUMN tag_name VARCHAR(100) DEFAULT NULL",
        # Posisi titik kamera di denah terminal (menu Zones), pecahan 0.0-1.0
        # dari lebar/tinggi gambar denah. NULL = kamera belum diposisikan
        # (otomatis muncul di area "belum diposisikan" pada denah).
        "map_x": "ALTER TABLE cameras ADD COLUMN map_x FLOAT DEFAULT NULL",
        "map_y": "ALTER TABLE cameras ADD COLUMN map_y FLOAT DEFAULT NULL",
    }
    for col, ddl in cam_migrations.items():
        if col not in existing_cam_cols:
            cursor.execute(ddl)
    
    # Kolom/tabel autentikasi: email, google_sub, is_active, last_login, login_logs, dll.
    ensure_auth_schema(cursor)

    # Pastikan Akun Admin Default Selalu Ada & Sinkron (admin / admin001).
    # ON DUPLICATE KEY UPDATE (bukan REPLACE) agar last_login & login_count admin tidak ter-reset.
    # Password admin kini di-HASH. Akun dibuat sekali dengan sandi awal (env ADMIN_PASSWORD, default
    # 'admin001'); restart TIDAK lagi menimpa sandi. Set ADMIN_PASSWORD untuk mengganti sandi admin.
    admin_pw = os.environ.get("ADMIN_PASSWORD", "").strip()
    cursor.execute("SELECT id FROM users WHERE id = 1")
    if cursor.fetchone() is None:
        cursor.execute("""
            INSERT INTO users (id, username, password, full_name, role, auth_provider, is_active)
            VALUES (1, 'admin', %s, 'Sarana digital', 'Sarana Digital', 'local', 1)
        """, (generate_password_hash(admin_pw or 'admin001'),))
    else:
        cursor.execute("""
            UPDATE users SET username = 'admin', full_name = 'Sarana digital', role = 'Sarana Digital',
                auth_provider = 'local', is_active = 1 WHERE id = 1
        """)
        if admin_pw:
            cursor.execute("UPDATE users SET password = %s WHERE id = 1", (generate_password_hash(admin_pw),))

    # Seed Kamera Default (jika tabel masih kosong)
    cursor.execute("SELECT COUNT(*) FROM cameras")
    if cursor.fetchone()[0] == 0:
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("""
            INSERT INTO cameras (name, location, brand, ip_address, port, username, password, stream_path, is_active, created_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            "CAM-01 Pintu Penurunan",
            "Pintu Masuk & Drop-off",
            "Dahua",
            "10.103.0.91",
            554,
            "admin",
            "@031323Asd",
            "/cam/realmonitor?channel=1&subtype=1",
            1,
            now
        ))
        
    conn.commit()
    cursor.close()
    conn.close()

init_db()

# Decorator untuk proteksi halaman yang butuh login
def login_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated_function

# Login Google (Pengguna), pembatasan hak akses, daftar pengguna & riwayat login admin
init_auth(app, get_db_connection)

def log_visitor(person_id, status, c_in, c_out, camera_id=1, snapshot_path=None, ts=None):
    conn = get_db_connection()
    cursor = conn.cursor()
    now = (ts or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    cursor.execute("""
        INSERT INTO visitor_logs (timestamp, person_id, status, total_in, total_out, camera_id, snapshot_path) 
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (now, person_id, status, c_in, c_out, camera_id, snapshot_path))
    conn.commit()
    cursor.close()
    conn.close()
    if snapshot_path is None:
        print(f"[LOG] ID {person_id} {status} tercatat TANPA snapshot (cek log [SNAPSHOT] di atas untuk alasannya)")

# ------------------------------------------------------------------------------
# ANTREAN PENULIS EVENT (snapshot + INSERT database) - di thread TERPISAH.
# Dulu thread deteksi menulis JPG & menunggu INSERT MySQL SECARA SINKRON, tepat
# pada saat orang melewati garis. Kalau MySQL/disk sedang lambat (100-500ms+),
# deteksi macet dan frame-frame berikutnya ter-skip -> pelacakan orang yang
# sedang bergerak putus justru di momen terpenting. Sekarang hitungan di memori
# langsung naik, sedangkan penulisan ke disk/DB dilakukan di belakang layar.
# ------------------------------------------------------------------------------
_event_queue = queue.Queue(maxsize=2000)

def enqueue_count_event(frame, box, cam_id, track_id, status, c_in, c_out):
    try:
        _event_queue.put_nowait((frame.copy(), box, cam_id, track_id, status, c_in, c_out, datetime.now()))
    except queue.Full:
        print(f"[EVENT][ERROR] Antrean penuh, event ID {track_id} {status} kamera {cam_id} TIDAK tercatat di log (hitungan di memori tetap naik).")

def _event_writer_loop():
    while True:
        frame, box, cam_id, track_id, status, c_in, c_out, ts = _event_queue.get()
        try:
            snap_name = save_person_snapshot(frame, box, cam_id, track_id, status)
        except Exception as e:
            print(f"[EVENT][ERROR] Snapshot gagal: {e}")
            snap_name = None
        for attempt in range(3):
            try:
                log_visitor(track_id, status, c_in, c_out, camera_id=cam_id, snapshot_path=snap_name, ts=ts)
                break
            except Exception as e:
                print(f"[EVENT][ERROR] INSERT log gagal (percobaan {attempt + 1}/3): {e}")
                time.sleep(1.0 * (attempt + 1))
        else:
            print(f"[EVENT][ERROR] Event ID {track_id} {status} kamera {cam_id} HILANG dari log setelah 3x gagal.")

threading.Thread(target=_event_writer_loop, daemon=True).start()

def build_rtsp_url(cam):
    user = urllib.parse.quote(cam.get('username', '')) if cam.get('username') else ""
    pwd = urllib.parse.quote(cam.get('password', '')) if cam.get('password') else ""
    auth = f"{user}:{pwd}@" if user or pwd else ""
    path = cam.get('stream_path', '')
    if not path.startswith('/'):
        path = f"/{path}"
    return f"rtsp://{auth}{cam.get('ip_address')}:{cam.get('port', 554)}{path}"

# ==============================================================================
# STREAM & AI ENGINE (YOLOv8 + BoT-SORT) — MULTI KAMERA + ANTI-FREEZE WATCHDOG
# ==============================================================================
class RTSPStreamFast:
    """
    Kelas capture RTSP per kamera. Dilengkapi watchdog: kalau tidak ada frame
    baru dalam beberapa detik (indikasi stream freeze/stuck), koneksi RTSP
    akan dipaksa reconnect otomatis tanpa perlu restart aplikasi.

    PENTING: setiap kali reconnect, dibuat THREAD BARU + VideoCapture BARU
    dari nol (bukan pakai ulang thread lama). Ini karena cv2.VideoCapture
    .read() pada RTSP yang benar-benar macet BISA HANG SELAMANYA di level
    driver/FFmpeg (timeout OpenCV tidak selalu dihormati semua build). Kalau
    reconnect memaksa thread lama yang sedang nyangkut untuk pakai object
    capture baru, video malah bisa freeze PERMANEN (persis gejala "muncul
    1 frame lalu diam total"). Dengan skema generasi ini, thread lama yang
    macet cukup ditinggal sebagai daemon thread (tidak mengganggu apa pun),
    sementara thread baru langsung mengambil alih pengiriman frame.
    """
    def __init__(self, src, cam_id=None, stale_timeout=5, initial_connect_timeout=20):
        self.src = src
        self.cam_id = cam_id
        self.stale_timeout = stale_timeout
        # Grace period KHUSUS untuk koneksi PERTAMA kali (belum pernah dapat
        # satu frame pun). Harus lebih besar dari OPEN_TIMEOUT_MSEC (8s) +
        # margin jaringan, supaya watchdog tidak "mendahului" cap.open()
        # yang masih berjalan normal dan memaksa reconnect prematur.
        self.initial_connect_timeout = initial_connect_timeout
        self.frame = None
        self.ret = False
        self.running = True
        self.lock = threading.Lock()
        self.last_frame_time = time.time()
        self.got_first_frame = False  # jadi True setelah frame pertama berhasil didapat
        self.generation = 0  # token "generasi" koneksi yang sedang aktif
        self.frame_id = 0    # naik 1 tiap frame BARU - supaya AI tidak memproses frame yang sama 2x

        self._spawn_capture_thread()

        self.watchdog_thread = threading.Thread(target=self._watchdog_loop, daemon=True)
        self.watchdog_thread.start()

    def _spawn_capture_thread(self):
        self.generation += 1
        my_gen = self.generation
        t = threading.Thread(target=self._capture_loop, args=(my_gen,), daemon=True)
        t.start()

    def _open_new_capture(self):
        """
        Buka VideoCapture BARU dengan urutan yang benar: buat object kosong
        dulu, PASANG timeout-nya, baru panggil .open(). Kalau langsung
        cv2.VideoCapture(src, backend) satu baris, koneksi RTSP-nya sudah
        dicoba SEBELUM sempat di-set timeout custom-nya - jadi timeout kita
        tidak pernah benar-benar kepakai di percobaan pertama. Ini salah
        satu penyebab kegagalan konek yang sulit dilacak sebelumnya.
        """
        cap = cv2.VideoCapture()
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        try:
            cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 8000)
            cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 8000)
        except Exception:
            pass  # tidak semua build OpenCV punya properti ini

        t0 = time.time()
        opened = cap.open(self.src, cv2.CAP_FFMPEG)
        elapsed = time.time() - t0

        if opened:
            print(f"[STREAM] Kamera {self.cam_id}: koneksi RTSP berhasil dibuka ({elapsed:.1f}s).")
        else:
            print(f"[STREAM][ERROR] Kamera {self.cam_id}: GAGAL membuka koneksi RTSP setelah {elapsed:.1f}s. "
                  f"Cek baris [rtsp @ ...] di atas ini untuk alasan pastinya (401=password salah, "
                  f"404=path salah, timeout/refused=jaringan atau NVR menolak koneksi baru).")
        return cap, opened

    def _capture_loop(self, my_gen):
        """
        Loop capture untuk SATU generasi koneksi. Kalau generasi ini sudah
        digantikan generasi baru (di-reconnect watchdog) sementara thread ini
        masih hidup/nyangkut, loop akan berhenti menulis ke shared state dan
        keluar dengan sendirinya - tidak akan pernah "menabrak" generasi baru.
        """
        cap, opened = self._open_new_capture()

        consecutive_fail = 0
        consecutive_open_fail = 0 if opened else 1
        try:
            while self.running and my_gen == self.generation:
                if not opened or not cap.isOpened():
                    # Backoff bertahap kalau GAGAL BUKA KONEKSI berkali-kali
                    # berturut-turut (mis. 401 password salah, 404 path salah).
                    # Sebelumnya retry setiap 1 detik TANPA JEDA BERTAMBAH -
                    # kalau penyebabnya salah password, ini artinya sistem
                    # menghajar kamera dengan percobaan login berkali-kali per
                    # menit, yang di banyak kamera/NVR (Dahua/Hikvision/Imou)
                    # bisa memicu fitur anti-bruteforce & mengunci akunnya
                    # sementara - mengubah "password salah" jadi "akun
                    # terkunci" walau passwordnya sudah dibetulkan.
                    wait = min(30, 2 ** min(consecutive_open_fail, 5))
                    time.sleep(wait)
                    if my_gen != self.generation:
                        break
                    try:
                        cap.release()
                    except Exception:
                        pass
                    cap, opened = self._open_new_capture()
                    consecutive_open_fail = 0 if opened else consecutive_open_fail + 1
                    continue
                consecutive_open_fail = 0

                ret, frame = cap.read()  # berpotensi hang lama kalau RTSP benar2 macet

                # Kalau ternyata sudah keburu di-reconnect generasi baru SAAT
                # read() ini akhirnya kembali, buang hasilnya & keluar - jangan
                # tulis ke shared state lagi supaya tidak bentrok/menimpa
                # generasi yang baru.
                if my_gen != self.generation:
                    break

                if ret and frame is not None:
                    with self.lock:
                        self.frame = frame
                        self.ret = True
                        self.last_frame_time = time.time()
                        self.got_first_frame = True
                        self.frame_id += 1
                    consecutive_fail = 0
                else:
                    consecutive_fail += 1
                    if consecutive_fail == 1:
                        print(f"[STREAM][WARN] Kamera {self.cam_id} (gen {my_gen}): cap.read() gagal/kosong, mencoba lagi...")
                    if consecutive_fail >= 15:
                        print(f"[STREAM][ERROR] Kamera {self.cam_id} (gen {my_gen}): 15x gagal baca frame berturut-turut, keluar dari generasi ini.")
                        # Keluar dari generasi ini; watchdog/caller yang akan
                        # menyalakan generasi baru dari nol.
                        break
                    time.sleep(0.05)
        except Exception as e:
            print(f"[STREAM][ERROR] Kamera {self.cam_id} (gen {my_gen}): {e}")
        finally:
            try:
                cap.release()
            except Exception:
                pass

    def _watchdog_loop(self):
        # Kalau sudah tidak ada frame baru sama sekali melebihi stale_timeout,
        # anggap stream freeze/stuck -> buka koneksi generasi baru dari nol.
        #
        # PENTING: untuk koneksi yang BELUM PERNAH dapat frame sama sekali
        # (kamera baru saja ditambahkan / baru start), pakai ambang batas
        # yang lebih longgar (initial_connect_timeout). Sebelumnya watchdog
        # memakai stale_timeout (5 detik) bahkan untuk percobaan konek
        # pertama, padahal cap.open() sendiri diberi waktu sampai 8 detik
        # (OPEN_TIMEOUT_MSEC) + proses handshake RTSP di jaringan nyata bisa
        # lebih lama dari itu. Akibatnya watchdog memaksa reconnect SEBELUM
        # koneksi pertama sempat selesai, generasi terus naik tanpa henti,
        # dan kamera yang baru ditambahkan kelihatan "terus terputus" padahal
        # sebenarnya belum pernah benar-benar diberi kesempatan konek.
        while self.running:
            time.sleep(2)
            with self.lock:
                got_first_frame = self.got_first_frame
            threshold = self.stale_timeout if got_first_frame else self.initial_connect_timeout
            if time.time() - self.last_frame_time > threshold:
                reason = "freeze" if got_first_frame else "koneksi awal belum berhasil"
                print(f"[WATCHDOG] Kamera {self.cam_id}: {reason} terdeteksi (>{threshold}s tanpa frame), buka koneksi baru (generasi {self.generation + 1})...")
                self._spawn_capture_thread()
                self.last_frame_time = time.time()

    def get_latest_frame(self):
        with self.lock:
            if self.ret and self.frame is not None:
                return True, self.frame.copy()
            return False, None

    def get_latest_frame_ex(self):
        """Seperti get_latest_frame() tapi juga mengembalikan nomor frame, supaya
        pemanggil bisa melewati frame yang sudah pernah diproses."""
        with self.lock:
            if self.ret and self.frame is not None:
                return True, self.frame.copy(), self.frame_id
            return False, None, self.frame_id

    def stop(self):
        self.running = False
        self.generation += 1  # thread capture manapun yang masih hidup langsung berhenti nulis state

# Inisialisasi Model AI. PENTING: TIDAK dipakai bersama antar kamera - lihat
# get_model_for_camera() di bawah. model.track(..., persist=True) menyimpan
# status tracking di DALAM objek model, jadi tiap kamera wajib punya instance
# sendiri supaya ID/posisi orang antar kamera tidak saling tabrakan.
#
# Model default yolov8n (paling ringan, paling tidak akurat). Untuk akurasi
# lebih tinggi:  set YOLO_MODEL=yolov8s.pt   (atau yolov8m.pt kalau ada GPU).
# Kalau ada GPU NVIDIA (CUDA), otomatis dipakai + mencoba yolov8s.pt dulu.
YOLO_MODEL_PATH = os.path.join(BASE_DIR, "yolov8n.pt") if os.path.isfile(os.path.join(BASE_DIR, "yolov8n.pt")) else "yolov8n.pt"
_yolo_available = True
DEVICE = "cpu"
USE_HALF = False
try:
    from ultralytics import YOLO
    import torch
    # Batasi thread CPU per inferensi supaya banyak kamera tidak rebutan core.
    torch.set_num_threads(int(os.environ.get("TORCH_THREADS", max(2, (os.cpu_count() or 4) // 2))))
    if torch.cuda.is_available():
        DEVICE, USE_HALF = 0, True

    _requested = os.environ.get("YOLO_MODEL")
    if _requested:
        _candidates = [_requested]
    elif DEVICE == 0:
        _candidates = ["yolov8s.pt", YOLO_MODEL_PATH]
    else:
        _candidates = [YOLO_MODEL_PATH]

    _loaded = None
    for _cand in _candidates:
        try:
            YOLO(_cand)  # tes muat sekali di awal, biar ketahuan dari log kalau filenya rusak
            _loaded = _cand
            break
        except Exception as _e:
            print(f"[WARN] Gagal memuat model '{_cand}': {_e}")
    if _loaded is None:
        raise RuntimeError("tidak ada model YOLO yang bisa dimuat")
    YOLO_MODEL_PATH = _loaded
    print(f"[AI] Model: {os.path.basename(str(YOLO_MODEL_PATH))} | device: {'GPU (CUDA)' if DEVICE == 0 else 'CPU'} | "
          f"imgsz={DETECT_IMGSZ} | conf={DETECT_CONF} | titik hitung={COUNT_ANCHOR} | tracker={os.path.basename(TRACKER_CFG)}")
    if DEVICE == "cpu":
        print("[AI][INFO] Berjalan di CPU. Kalau FPS AI rendah (lihat tulisan 'AI x.x fps' di video), akurasi counting "
              "turun karena orang yang bergerak cepat bisa 'loncat' antar frame. Cek /api/counting_diag.")
except Exception as e:
    print(f"[WARN] YOLO tidak dapat dimuat: {e}")
    _yolo_available = False

_camera_models = {}          # { cam_id: instance YOLO khusus kamera itu }
_camera_models_lock = threading.Lock()

def get_model_for_camera(cam_id):
    """Kembalikan instance YOLO KHUSUS untuk satu kamera (dibuat sekali,
    lalu dipakai ulang terus oleh kamera yang sama saja) - supaya status
    tracking antar kamera tidak pernah saling tabrakan."""
    if not _yolo_available:
        return None
    with _camera_models_lock:
        m = _camera_models.get(cam_id)
        if m is None:
            m = YOLO(YOLO_MODEL_PATH)
            _camera_models[cam_id] = m
        return m

# Runtime state semua kamera aktif: { cam_id: {stream, count_in, count_out, ...} }
camera_runtime = {}
camera_runtime_lock = threading.Lock()

def camera_detection_worker(cam_id):
    """
    Thread KHUSUS untuk inferensi YOLO + pelacakan + logika counting.
    Dipisah dari thread render supaya video tidak patah-patah oleh lamanya AI.

    Alur per frame BARU:
      1. ambil frame (rasio asli, hanya diperkecil jika lebih lebar dari DETECT_MAX_WIDTH)
      2. YOLOv8 + ByteTrack (tracker_counting.yaml) -> kotak + ID orang
      3. filter bentuk yang longgar (bukan lagi tinggi >= 0.9*lebar)
      4. LineCounter.update() -> "IN" / "OUT" / None (histeresis, dua arah, tanpa
         batas satu-ID-satu-masuk)
      5. kalau ada event: hitungan memori naik SEKARANG, snapshot+DB ditulis di
         thread lain (enqueue_count_event)

    Seluruh isi loop dibungkus try/except supaya thread tidak mati diam-diam.
    """
    last_frame_id = -1
    last_prune = 0.0
    fps_t0 = time.time()
    fps_n = 0
    fps_id0 = None
    trail_store = {}   # track_id -> {"pts": deque, "seen": float}

    while True:
        try:
            with camera_runtime_lock:
                rt = camera_runtime.get(cam_id)
            if rt is None or not rt["running"]:
                return  # kamera sudah dihapus/dinonaktifkan -> hentikan thread ini

            success, frame, frame_id = rt["stream"].get_latest_frame_ex()
            if not success or frame is None or frame_id == last_frame_id:
                time.sleep(0.01)   # belum ada frame baru -> jangan proses frame yang sama 2x
                continue
            last_frame_id = frame_id

            cam_model = get_model_for_camera(cam_id)
            if cam_model is None:
                time.sleep(0.05)
                continue

            # Rasio asli dipertahankan; hanya diperkecil kalau terlalu lebar.
            h0, w0 = frame.shape[:2]
            if w0 > DETECT_MAX_WIDTH:
                scale = DETECT_MAX_WIDTH / float(w0)
                frame_det = cv2.resize(frame, (DETECT_MAX_WIDTH, max(1, int(round(h0 * scale)))),
                                       interpolation=cv2.INTER_AREA)
            else:
                frame_det = frame
            H, W = frame_det.shape[:2]
            rt["frame_size"] = (W, H)

            counter = rt["counter"]
            counter.set_frame_size(W, H)

            results = cam_model.track(
                frame_det,
                persist=True,
                classes=[0],
                tracker=TRACKER_CFG,
                conf=DETECT_CONF,
                iou=DETECT_NMS_IOU,
                imgsz=DETECT_IMGSZ,
                device=DEVICE,
                half=USE_HALF,
                verbose=False
            )

            now = time.time()
            current_boxes = []      # koordinat TERNORMALISASI 0..1 (independen dari resolusi)
            current_trails = {}

            if results[0].boxes is not None and results[0].boxes.id is not None:
                boxes = results[0].boxes.xyxy.cpu().numpy()
                track_ids = results[0].boxes.id.int().cpu().numpy()

                for box, track_id in zip(boxes, track_ids):
                    x1, y1, x2, y2 = [float(v) for v in box]
                    bw, bh = x2 - x1, y2 - y1

                    # Filter bentuk LONGGAR: hanya buang yang jelas bukan orang.
                    if bw <= 0 or bh <= 0:
                        continue
                    if bh < bw * MIN_BOX_ASPECT:
                        continue
                    if bh < MIN_BOX_HEIGHT_FRAC * H:
                        continue

                    tid = int(track_id)
                    px = (x1 + x2) / 2.0
                    py = y2 if COUNT_ANCHOR == "bottom" else (y1 + y2) / 2.0
                    current_boxes.append((x1 / W, y1 / H, x2 / W, y2 / H, tid, px / W, py / H))

                    if SHOW_TRAILS:
                        tr = trail_store.get(tid)
                        if tr is None:
                            tr = {"pts": deque(maxlen=25), "seen": now}
                            trail_store[tid] = tr
                        tr["pts"].append((px / W, py / H))
                        tr["seen"] = now
                        current_trails[tid] = list(tr["pts"])

                    event = counter.update(tid, (px, py), now)
                    if event is not None and counting_active.is_set():
                        if event == LineCounter.IN:
                            rt["count_in"] += 1
                            status = "MASUK"
                        else:
                            rt["count_out"] += 1
                            status = "KELUAR"
                        enqueue_count_event(frame_det, (x1, y1, x2, y2), cam_id, tid, status,
                                            rt["count_in"], rt["count_out"])

            with rt["boxes_lock"]:
                rt["last_boxes"] = current_boxes
                rt["last_trails"] = current_trails

            # Bersih-bersih state lama (hemat memori, ID lama tidak menumpuk)
            if now - last_prune > 5.0:
                last_prune = now
                counter.prune(now)
                for k in [k for k, v in trail_store.items() if now - v["seen"] > 3.0]:
                    del trail_store[k]

            # Pantau FPS AI & FPS stream (diagnosis: apakah AI-nya yang kewalahan?)
            fps_n += 1
            if now - fps_t0 >= 2.0:
                dt = now - fps_t0
                rt["det_fps"] = fps_n / dt
                if fps_id0 is not None:
                    rt["stream_fps"] = (frame_id - fps_id0) / dt
                fps_id0, fps_t0, fps_n = frame_id, now, 0
                if rt["det_fps"] < LOW_FPS_WARN and now - rt.get("_last_fps_warn", 0) > 60:
                    rt["_last_fps_warn"] = now
                    print(f"[AI][WARN] Kamera {cam_id}: AI hanya {rt['det_fps']:.1f} fps (stream {(rt.get('stream_fps') or 0):.1f} fps). "
                          f"Orang yang berjalan cepat bisa 'loncat' antar frame sehingga ID putus & hitungan meleset. "
                          f"Solusi: pakai GPU, kurangi jumlah kamera aktif, atau turunkan DETECT_IMGSZ.")
        except Exception as e:
            print(f"[DETECTION][ERROR] Kamera {cam_id}: {e}")
            time.sleep(0.5)  # jeda sebentar biar tidak spam error kalau terus gagal

def make_placeholder_frame(text_line1, text_line2=""):
    """Bikin frame abu-abu gelap dengan teks status, dipakai saat kamera
    belum pernah dapat frame sama sekali (bukan layar hitam polos)."""
    frame = np.full((RENDER_H, RENDER_W, 3), 30, dtype=np.uint8)
    cv2.putText(frame, text_line1, (24, RENDER_H // 2 - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 165, 255), 1, cv2.LINE_AA)
    if text_line2:
        cv2.putText(frame, text_line2, (24, RENDER_H // 2 + 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (148, 163, 184), 1, cv2.LINE_AA)
    return frame

def camera_render_worker(cam_id):
    """
    Thread RINGAN: ambil frame mentah terbaru + gambar kotak/ID/garis dari hasil
    deteksi terakhir, simpan ke rt['latest_frame'] untuk MJPEG. Tidak menunggu
    YOLO. Hanya menggambar ulang kalau ada frame BARU (dulu loop ini berputar
    ~60x/detik menyalin & me-resize frame yang sama berulang-ulang, memakan CPU
    yang seharusnya dipakai AI).
    """
    last_rendered_id = -1
    while True:
        try:
            with camera_runtime_lock:
                rt = camera_runtime.get(cam_id)
            if rt is None or not rt["running"]:
                return

            success, raw_frame, frame_id = rt["stream"].get_latest_frame_ex()
            now = time.time()

            if success and raw_frame is not None:
                rt["last_good_frame_time"] = now
                if frame_id == last_rendered_id:
                    time.sleep(0.01)
                    continue
                last_rendered_id = frame_id
                # Tampilan SELALU memenuhi 16:9 (sama seperti editor garis di UI),
                # jadi koordinat garis 0..1 cocok persis dengan yang digambar.
                frame = cv2.resize(raw_frame, (RENDER_W, RENDER_H))
            else:
                # Tidak ada frame baru saat ini - JANGAN tampilkan layar hitam.
                with rt["frame_lock"]:
                    cached = rt["latest_frame"]
                seconds_since_good = now - rt.get("last_good_frame_time", 0)

                if cached is not None and rt.get("last_good_frame_time", 0) > 0 and seconds_since_good < 30:
                    frame = cached.copy()
                    cv2.rectangle(frame, (0, frame.shape[0] - 22), (frame.shape[1], frame.shape[0]), (0, 0, 0), -1)
                    cv2.putText(frame, "MENYAMBUNGKAN ULANG KE KAMERA...", (6, frame.shape[0] - 7),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 165, 255), 1)
                else:
                    frame = make_placeholder_frame("SINYAL TERPUTUS", "Memeriksa koneksi RTSP kamera...")

                with rt["frame_lock"]:
                    rt["latest_frame"] = frame
                time.sleep(0.2)
                continue

            with rt["boxes_lock"]:
                boxes_snapshot = list(rt["last_boxes"])
                trails_snapshot = dict(rt.get("last_trails", {}))

            with highlight_lock:
                target_id = highlight_state["target_id"]

            RW, RH = RENDER_W, RENDER_H

            if SHOW_TRAILS:
                for pts in trails_snapshot.values():
                    if len(pts) >= 2:
                        poly = np.array([[int(x * RW), int(y * RH)] for x, y in pts], dtype=np.int32)
                        cv2.polylines(frame, [poly], False, (255, 200, 0), 1, cv2.LINE_AA)

            for (nx1, ny1, nx2, ny2, track_id, ncx, ncy) in boxes_snapshot:
                x1, y1, x2, y2 = int(nx1 * RW), int(ny1 * RH), int(nx2 * RW), int(ny2 * RH)
                cx, cy = int(ncx * RW), int(ncy * RH)
                is_target = (target_id is not None and int(track_id) == int(target_id))
                box_color = (0, 255, 255) if is_target else (0, 255, 0)  # kuning utk target terkunci
                thickness = 3 if is_target else 2
                cv2.rectangle(frame, (x1, y1), (x2, y2), box_color, thickness)
                cv2.circle(frame, (cx, cy), 4, (0, 0, 255), -1)   # titik yang dipakai menghitung
                label = f"ID:{track_id}" + (" [TARGET]" if is_target else "")
                cv2.putText(frame, label, (x1, max(12, y1 - 5)),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, box_color if is_target else (255, 255, 255), 1)

            # Garis counting + PANAH arah MASUK (supaya jelas sisi mana yang dianggap masuk)
            lx1, ly1, lx2, ly2 = rt["line_x1"], rt["line_y1"], rt["line_x2"], rt["line_y2"]
            p1 = (int(lx1 * RW), int(ly1 * RH))
            p2 = (int(lx2 * RW), int(ly2 * RH))
            cv2.line(frame, p1, p2, (0, 0, 255), 2)
            cv2.circle(frame, p1, 4, (0, 0, 255), -1)
            cv2.circle(frame, p2, 4, (0, 0, 255), -1)
            try:
                rx, ry = (p2[0] - p1[0]), (p2[1] - p1[1])
                ln = max(1.0, (rx * rx + ry * ry) ** 0.5)
                nx, ny = -ry / ln, rx / ln                         # normal ke sisi "positif"
                if rt.get("count_direction") == "up_in":
                    nx, ny = -nx, -ny
                mx, my = (p1[0] + p2[0]) // 2, (p1[1] + p2[1]) // 2
                tip = (int(mx + nx * 34), int(my + ny * 34))
                cv2.arrowedLine(frame, (mx, my), tip, (0, 255, 0), 2, tipLength=0.35)
                cv2.putText(frame, str(rt.get("label_masuk", "MASUK")), (tip[0] + 10, tip[1] + 4),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 0), 1)
            except Exception:
                pass

            hud_text = f"{rt.get('label_masuk','MASUK')}: {rt['count_in']}   {rt.get('label_keluar','KELUAR')}: {rt['count_out']}"
            (tw, th), _ = cv2.getTextSize(hud_text, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
            cv2.rectangle(frame, (0, 0), (min(frame.shape[1], tw + 14), 22), (0, 0, 0), -1)
            cv2.putText(frame, hud_text, (5, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 1)

            # Indikator FPS AI: kalau merah/rendah, penyebab counting meleset adalah KECEPATAN AI
            det_fps = rt.get("det_fps")
            if det_fps is not None:
                fps_txt = f"AI {det_fps:.1f} fps"
                fcol = (0, 200, 0) if det_fps >= LOW_FPS_WARN * 1.5 else ((0, 165, 255) if det_fps >= LOW_FPS_WARN else (0, 0, 255))
                (fw, fh), _ = cv2.getTextSize(fps_txt, cv2.FONT_HERSHEY_SIMPLEX, 0.45, 1)
                cv2.rectangle(frame, (RW - fw - 12, 0), (RW, 20), (0, 0, 0), -1)
                cv2.putText(frame, fps_txt, (RW - fw - 6, 15), cv2.FONT_HERSHEY_SIMPLEX, 0.45, fcol, 1)

            if not counting_active.is_set():
                cv2.rectangle(frame, (0, frame.shape[0] - 22), (frame.shape[1], frame.shape[0]), (0, 0, 0), -1)
                cv2.putText(frame, "COUNTING PAUSED - LAPORAN OTOMATIS", (6, frame.shape[0] - 7),
                            cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 165, 255), 1)

            with rt["frame_lock"]:
                rt["latest_frame"] = frame

            time.sleep(0.005)
        except Exception as e:
            print(f"[RENDER][ERROR] Kamera {cam_id}: {e}")
            time.sleep(0.5)

def _restore_counts_from_db(cam_id):
    """Pulihkan hitungan MASUK/KELUAR satu kamera dari visitor_logs sejak
    sesi counting saat ini dimulai. Aman: kalau gagal, kembali (0, 0)."""
    try:
        since = session_state["phase_started_at"].strftime("%Y-%m-%d %H:%M:%S")
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT status, COUNT(*) FROM visitor_logs "
            "WHERE camera_id = %s AND timestamp >= %s GROUP BY status",
            (cam_id, since))
        counts = {row[0]: int(row[1]) for row in cursor.fetchall()}
        cursor.close()
        conn.close()
        return counts.get("MASUK", 0), counts.get("KELUAR", 0)
    except Exception as e:
        print(f"[CAMERA] Gagal memulihkan hitungan kamera #{cam_id}: {e}")
        return 0, 0

def apply_camera_config_live(cam_row):
    """Terapkan perubahan konfigurasi counting (garis, label, arah, kapasitas,
    nama, lokasi, tag) ke worker yang SEDANG berjalan tanpa restart - jadi
    stream tidak putus dan hitungan tidak ter-reset. Return True kalau
    worker ada dan berhasil di-update."""
    cam = dict(cam_row)
    cam_id = cam["id"]

    def _c(v):
        try:
            return min(max(float(v), 0.0), 1.0)
        except (TypeError, ValueError):
            return None

    lx1, ly1, lx2, ly2 = (_c(cam.get("line_x1")), _c(cam.get("line_y1")),
                          _c(cam.get("line_x2")), _c(cam.get("line_y2")))
    if None in (lx1, ly1, lx2, ly2):
        try:
            pos = float(cam.get("line_position") if cam.get("line_position") is not None else 0.5)
        except (TypeError, ValueError):
            pos = 0.5
        pos = min(max(pos, 0.05), 0.95)
        lx1, ly1, lx2, ly2 = 0.0, pos, 1.0, pos

    with camera_runtime_lock:
        rt = camera_runtime.get(cam_id)
        if rt is None or not rt.get("running"):
            return False
        old_line = (rt.get("line_x1"), rt.get("line_y1"), rt.get("line_x2"), rt.get("line_y2"))
        rt["line_x1"], rt["line_y1"], rt["line_x2"], rt["line_y2"] = lx1, ly1, lx2, ly2
        rt["label_masuk"] = cam.get("label_masuk") or "MASUK"
        rt["label_keluar"] = cam.get("label_keluar") or "KELUAR"
        rt["count_direction"] = cam.get("count_direction") or "down_in"
        rt["max_capacity"] = int(cam.get("max_capacity") or 100)
        rt["tag_name"] = cam.get("tag_name") or ""
        rt["name"] = cam.get("name")
        rt["location"] = cam.get("location")
        flip = rt["count_direction"] == "up_in"
        if old_line != (lx1, ly1, lx2, ly2):
            # Garis pindah -> posisi lama tidak relevan lagi (cegah crossing palsu)
            rt["counter"].set_line_norm(lx1, ly1, lx2, ly2, flip=flip)
        else:
            rt["counter"].set_flip(flip)
    return True

STREAM_FIELDS = ("ip_address", "port", "username", "password", "stream_path")

def start_camera_worker(cam_row):
    """
    Menyalakan stream + AI worker untuk satu unit kamera. Dipanggil otomatis
    saat kamera baru ditambahkan lewat CRUD, atau saat aplikasi start untuk
    semua kamera yang berstatus aktif (Kebutuhan: kamera baru otomatis
    ke-counting & otomatis muncul di dashboard / Live Cameras).
    """
    cam = dict(cam_row)
    cam_id = cam["id"]

    # PENTING: hitungan MASUK/KELUAR hanya hidup di memori (camera_runtime).
    # Dulu setiap kali worker di-restart (simpan/ubah kamera), hitungan
    # kembali 0 sehingga angka di dashboard "ke-reset". Sekarang hitungan
    # lama dibawa ke worker baru; kalau worker belum pernah jalan (mis.
    # kamera baru diaktifkan lagi) hitungan dipulihkan dari visitor_logs
    # sejak sesi counting berjalan dimulai.
    carried_in, carried_out = 0, 0
    with camera_runtime_lock:
        existing = camera_runtime.get(cam_id)
        if existing is not None:
            carried_in = existing.get("count_in", 0)
            carried_out = existing.get("count_out", 0)
    if existing is None:
        carried_in, carried_out = _restore_counts_from_db(cam_id)
    if existing is not None and existing.get("running"):
        stop_camera_worker(cam_id)  # restart bersih kalau sudah jalan (mis. update kredensial)

    rtsp_url = build_rtsp_url(cam)
    stream = RTSPStreamFast(rtsp_url, cam_id=cam_id)

    # Konfigurasi counting (garis, label, arah, kapasitas) - bisa diedit lewat
    # CRUD kamera / modal "Atur Counting". Pakai nilai default kalau kosong.
    #
    # Garis counting FLEKSIBEL 360 DERAJAT: disimpan di DB sebagai 2 titik
    # ujung (line_x1,y1) -> (line_x2,y2), masing-masing pecahan 0.0-1.0 dari
    # lebar/tinggi frame (pecahan, bukan piksel). Kalau kamera ini belum pernah
    # diatur ulang garisnya (kolom baru masih NULL, kamera lama), diturunkan
    # garis horizontal selebar frame dari line_position lama - supaya kamera
    # lama tidak tiba-tiba kehilangan garis countingnya.
    def _clamp01(v):
        try:
            return min(max(float(v), 0.0), 1.0)
        except (TypeError, ValueError):
            return None

    lx1 = _clamp01(cam.get("line_x1"))
    ly1 = _clamp01(cam.get("line_y1"))
    lx2 = _clamp01(cam.get("line_x2"))
    ly2 = _clamp01(cam.get("line_y2"))

    if None in (lx1, ly1, lx2, ly2):
        try:
            legacy_pos = float(cam.get("line_position") if cam.get("line_position") is not None else 0.5)
        except (TypeError, ValueError):
            legacy_pos = 0.5
        legacy_pos = min(max(legacy_pos, 0.05), 0.95)
        lx1, ly1, lx2, ly2 = 0.0, legacy_pos, 1.0, legacy_pos

    counter = LineCounter(deadband_frac=COUNT_DEADBAND)
    counter.set_line_norm(lx1, ly1, lx2, ly2, flip=((cam.get("count_direction") or "down_in") == "up_in"))

    with camera_runtime_lock:
        camera_runtime[cam_id] = {
            "stream": stream,
            "count_in": carried_in,
            "count_out": carried_out,
            "counter": counter,   # logika lewat-garis (line_counter.py): histeresis, dua arah
            "latest_frame": None,
            "frame_lock": threading.Lock(),
            "last_boxes": [],
            "last_trails": {},
            "boxes_lock": threading.Lock(),
            "det_fps": None,
            "stream_fps": None,
            "frame_size": None,
            "running": True,
            "line_x1": lx1, "line_y1": ly1, "line_x2": lx2, "line_y2": ly2,
            "label_masuk": cam.get("label_masuk") or "MASUK",
            "label_keluar": cam.get("label_keluar") or "KELUAR",
            "count_direction": cam.get("count_direction") or "down_in",
            "max_capacity": int(cam.get("max_capacity") or 100),
            "tag_name": cam.get("tag_name") or "",
            "last_good_frame_time": 0,
            "name": cam.get("name"),
            "location": cam.get("location"),
        }

    t_detect = threading.Thread(target=camera_detection_worker, args=(cam_id,), daemon=True)
    t_render = threading.Thread(target=camera_render_worker, args=(cam_id,), daemon=True)
    t_detect.start()
    t_render.start()
    print(f"[CAMERA] Worker dimulai untuk kamera #{cam_id} - {cam.get('name')}")

def stop_camera_worker(cam_id):
    """Mematikan stream + AI worker untuk satu kamera (dipakai saat delete/nonaktifkan)."""
    with camera_runtime_lock:
        rt = camera_runtime.pop(cam_id, None)
    if rt:
        rt["running"] = False
        try:
            rt["stream"].stop()
        except Exception:
            pass
        with _camera_models_lock:
            _camera_models.pop(cam_id, None)  # lepas model YOLO khusus kamera ini, bebaskan memori
        print(f"[CAMERA] Worker dihentikan untuk kamera #{cam_id}")

def load_all_cameras_and_start():
    """Dipanggil sekali saat aplikasi start: nyalakan semua kamera aktif di DB."""
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT COUNT(*) as total FROM cameras")
    total_all = cursor.fetchone()["total"]
    cursor.execute("SELECT * FROM cameras WHERE is_active = 1")
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    print(f"[STARTUP] Total baris di tabel cameras: {total_all}. Yang aktif & akan dinyalakan: {len(rows)}.")
    if len(rows) > 6:
        print(f"[STARTUP][WARNING] {len(rows)} kamera aktif sekaligus itu BANYAK untuk CPU biasa (tanpa GPU) - "
              f"tiap kamera load model YOLO sendiri & jalan terus-menerus, gampang bikin semua kamera lag/patah-patah. "
              f"Kalau sebagian dari ini kamera lama/gagal/duplikat, cek tabel 'cameras' di phpMyAdmin dan hapus/nonaktifkan yang tidak dipakai.")
    for row in rows:
        print(f"[STARTUP]   -> menyalakan kamera #{row['id']}: {row.get('name')}")
        start_camera_worker(row)

load_all_cameras_and_start()

# ==============================================================================
# SCHEDULER LAPORAN OTOMATIS (Kebutuhan #6 & #7)
# Sesi counting berjalan 23 jam -> otomatis stop & export xlsx -> jeda 1 jam
# -> sesi baru dimulai lagi (counter direset untuk periode berikutnya)
# ==============================================================================
# ------------------------------------------------------------------------------
# PEMBUAT LAPORAN XLSX (dipakai laporan otomatis & tombol Export Excel)
# Isi workbook dibangun oleh report_builder.py (lihat docstring-nya). Sheet:
#   DASHBOARD, DATA DETAIL (Excel Table + filter), REKAP, REKAP PER KAMERA, ANALISIS,
#   1 sheet per kamera, FILTER KAMERA, VALIDASI, REFERENSI.
# Daftar kamera dibaca dari tabel `cameras` SAAT laporan dibuat, jadi unit CCTV
# baru otomatis punya sheet sendiri tanpa perlu mengubah kode.
# ------------------------------------------------------------------------------
REPORT_COLUMNS = ["id", "timestamp", "person_id", "status", "total_in", "total_out", "camera_id", "snapshot_path"]
_REPORT_COL_WIDTHS = {"id": 8, "timestamp": 20, "person_id": 11, "status": 10, "total_in": 10,
                      "total_out": 11, "camera_id": 11, "snapshot_path": 48, "nama_kamera": 26}

def _safe_sheet_name(name, used, alt=None):
    """Nama sheet valid Excel: maks 31 karakter, tanpa []:*?/\\, unik (tidak peka huruf besar/kecil)."""
    base = re.sub(r"[\[\]:*?/\\]", "-", str(name or "")).strip().strip("'") or "Kamera"
    base = base[:31]
    if alt and base.lower() in used:
        # Nama kembar -> beri penanda ID kamera, mis. "Aglo #2"
        base = (base[:31 - len(alt) - 1] + " " + alt)[:31]
    candidate, n = base, 2
    while candidate.lower() in used:
        suffix = f" ({n})"
        candidate = base[:31 - len(suffix)] + suffix
        n += 1
    used.add(candidate.lower())
    return candidate

def _xl_value(v):
    """Ubah nilai pandas/numpy ke tipe Python biasa yang dimengerti openpyxl."""
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if isinstance(v, pd.Timestamp):
        return v.to_pydatetime()
    if hasattr(v, "item"):
        return v.item()
    return v

# ------------------------------------------------------------------------------
# HISTORI LAPORAN (periode tanggal)
# Satu-satunya sumber data untuk halaman laporan (/api/report) DAN file XLSX
# (/export_excel), sehingga isi keduanya selalu sama. Data dibaca dari
# visitor_logs berdasarkan kolom `timestamp`; tidak ada yang dihapus/ditimpa.
# Default periode = REPORT_DEFAULT_DAYS (30) hari kalender terakhir s/d hari ini (dinamis).
# ------------------------------------------------------------------------------
REPORT_DEFAULT_DAYS = 30
_BULAN_ID = ["", "Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli",
             "Agustus", "September", "Oktober", "November", "Desember"]

def _fmt_tanggal_id(d):
    """date -> '07 Oktober 2026'"""
    return f"{d.day:02d} {_BULAN_ID[d.month]} {d.year}"

def _parse_report_date(value):
    """Terima 'YYYY-MM-DD' (input type=date) atau 'DD-MM-YYYY' / 'DD/MM/YYYY'. Tidak valid -> None."""
    if not value:
        return None
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None

def resolve_report_range(start_str=None, end_str=None):
    """Tentukan (tanggal_mulai, tanggal_akhir). Kosong/tidak valid -> default dinamis:
    akhir = hari ini, mulai = akhir - (REPORT_DEFAULT_DAYS - 1) hari, yaitu 30 hari
    KALENDER inklusif (hari ini 07-10 -> mulai 08-09). Jika terbalik, ditukar."""
    end = _parse_report_date(end_str) or datetime.now().date()
    start = _parse_report_date(start_str) or (end - timedelta(days=REPORT_DEFAULT_DAYS - 1))
    if start > end:
        start, end = end, start
    return start, end

def _report_bounds(start_date, end_date):
    """Batas DATETIME [mulai 00:00:00, akhir+1 hari 00:00:00) - hari terakhir ikut penuh
    dan kolom timestamp tetap bisa memakai index (tanpa DATE(timestamp) di WHERE)."""
    lo = datetime.combine(start_date, datetime.min.time())
    hi = datetime.combine(end_date + timedelta(days=1), datetime.min.time())
    return lo, hi

def fetch_daily_summary(start_date, end_date):
    """Ringkasan per TANGGAL x KAMERA (urut tanggal terbaru dulu). Hanya hari yang
    benar-benar punya record yang muncul - hari tanpa data tidak dipalsukan jadi 0."""
    lo, hi = _report_bounds(start_date, end_date)
    conn = get_db_connection()
    try:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT DATE(timestamp) AS tgl, camera_id,
                   SUM(status = 'MASUK') AS masuk, SUM(status = 'KELUAR') AS keluar
            FROM visitor_logs
            WHERE timestamp >= %s AND timestamp < %s
            GROUP BY DATE(timestamp), camera_id
            ORDER BY tgl DESC, camera_id ASC
        """, (lo, hi))
        raw = cursor.fetchall()
        cursor.execute("SELECT id, name FROM cameras")
        cam_names = {r[0]: r[1] for r in cursor.fetchall()}
        cursor.close()
    finally:
        conn.close()

    rows = []
    for tgl, cam_id, masuk, keluar in raw:
        if cam_id is None:
            cam_label = "Tanpa Kamera"
        else:
            cam_label = cam_names.get(cam_id) or f"Kamera #{cam_id} (dihapus)"
        rows.append({
            "tanggal": tgl,                       # datetime.date
            "camera_id": cam_id,
            "kamera": cam_label,
            "masuk": int(masuk or 0),
            "keluar": int(keluar or 0),
        })
    return rows

def fetch_report_logs(start_date, end_date):
    """Event mentah dalam periode (untuk sheet detail di XLSX) + daftar kamera."""
    lo, hi = _report_bounds(start_date, end_date)
    conn = get_db_connection()
    try:
        logs_df = pd.read_sql_query(
            "SELECT id, timestamp, person_id, status, total_in, total_out, camera_id, snapshot_path "
            "FROM visitor_logs WHERE timestamp >= %s AND timestamp < %s ORDER BY id",
            conn, params=(lo, hi))
        cams_df = pd.read_sql_query("SELECT id, name, location FROM cameras ORDER BY id", conn)
    finally:
        conn.close()
    return logs_df, cams_df

def write_report_file(target, start_date=None, end_date=None, base_url=None):
    """Ambil data periode [start_date, end_date] dari DB lalu tulis laporan XLSX ke `target`.
    Tanpa argumen (mis. laporan otomatis) = default 30 hari terakhir sampai hari ini.

    Isi workbook dibangun oleh report_builder.py: DATA DETAIL (Excel Table + filter) sebagai
    sumber utama, lalu REKAP / REKAP PER KAMERA / ANALISIS / DASHBOARD / VALIDASI yang semuanya
    berupa rumus dari DATA DETAIL, plus sheet per kamera & FILTER KAMERA.
    base_url = alamat web server (mis. http://192.168.1.10:5000/) untuk hyperlink foto."""
    start_date, end_date = resolve_report_range(start_date, end_date)
    logs_df, cams_df = fetch_report_logs(start_date, end_date)
    base_url = base_url or os.environ.get("REPORT_BASE_URL") or None
    return report_builder.build_report_workbook(
        target, logs_df, cams_df, start_date=start_date, end_date=end_date,
        snapshot_dir=SNAPSHOT_DIR, base_url=base_url)

def export_auto_report():
    timestamp_str = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"Laporan_People_Counting_Sarana_{timestamp_str}.xlsx"
    filepath = os.path.join(AUTO_REPORT_DIR, filename)
    try:
        write_report_file(filepath)
        session_state["last_auto_report"] = {"file": filename, "path": filepath, "time": timestamp_str}
        print(f"[AUTO-REPORT] Laporan otomatis tersimpan: {filepath}")
    except Exception as e:
        print(f"[AUTO-REPORT][ERROR] Gagal menyimpan laporan otomatis: {e}")

def auto_report_scheduler():
    while True:
        # --- FASE AKTIF: hitung selama 23 jam ---
        session_state["active"] = True
        session_state["phase_started_at"] = datetime.now()
        session_state["next_change_at"] = datetime.now() + timedelta(seconds=SESSION_ACTIVE_DURATION)
        counting_active.set()
        time.sleep(SESSION_ACTIVE_DURATION)

        # --- FASE JEDA: stop counting, cetak laporan otomatis, tunggu 1 jam ---
        counting_active.clear()
        session_state["active"] = False
        session_state["phase_started_at"] = datetime.now()
        session_state["next_change_at"] = datetime.now() + timedelta(seconds=SESSION_PAUSE_DURATION)
        export_auto_report()

        # Reset counter SEMUA kamera untuk sesi periode berikutnya
        with camera_runtime_lock:
            for cam_id, rt in camera_runtime.items():
                rt["count_in"] = 0
                rt["count_out"] = 0
                rt["counter"].reset()

        time.sleep(SESSION_PAUSE_DURATION)

threading.Thread(target=auto_report_scheduler, daemon=True).start()

def generate_frames(cam_id):
    """Streaming MJPEG untuk satu kamera spesifik (mendukung multi kamera)."""
    missing_since = None
    while True:
        try:
            with camera_runtime_lock:
                rt = camera_runtime.get(cam_id)
            if rt is None:
                # Kamera sedang restart / dihapus / dinonaktifkan. Tunggu sebentar
                # (restart normal < 1-2 detik), tapi JANGAN berputar selamanya:
                # generator yang tidak pernah yield tidak bisa tahu klien sudah
                # pergi, sehingga thread server & koneksinya bocor.
                if missing_since is None:
                    missing_since = time.time()
                elif time.time() - missing_since > 15:
                    return
                time.sleep(0.5)
                continue
            missing_since = None

            with rt["frame_lock"]:
                if rt["latest_frame"] is None:
                    frame = None
                else:
                    frame = rt["latest_frame"].copy()

            if frame is None:
                time.sleep(0.03)
                continue

            ret, buffer = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, 75])
            if not ret:
                continue

            frame_bytes = buffer.tobytes()
            yield (b'--frame\r\n'
                   b'Content-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n')
        except GeneratorExit:
            # Browser/tab menutup koneksi video -> keluar dengan wajar, bukan error
            raise
        except Exception as e:
            print(f"[VIDEO_FEED][ERROR] Kamera {cam_id}: {e}")
            time.sleep(0.2)
        time.sleep(0.033)

# ==============================================================================
# AUTHENTICATION ROUTES
# ==============================================================================
@app.route('/login', methods=['GET', 'POST'])
def login_page():
    if 'user_id' in session:
        return redirect(url_for('index'))

    error = session.pop('login_error', None)
    mode = session.pop('login_mode', 'masuk')  # 'masuk' / 'daftar' (pengguna) / 'admin'
    prefill = session.pop('login_prefill', None) or {}
    if request.method == 'POST':
        # Gunakan .strip() agar aman dari salah input/spasi ter-copy
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '').strip()
        prefill = {"username": username}

        # 1) captcha (sekali pakai), 2) username + kata sandi (admin maupun pengguna buatan admin)
        if not captcha_ok(request.form.get('captcha', '')):
            note_login_fail()
            error = "Kode captcha salah atau kedaluwarsa. Coba lagi."
        else:
            user, auth_error = authenticate_local(get_db_connection, username, password)
            if user:
                start_session(user, 'local', get_db_connection)
                return redirect(url_for('index'))
            error = auth_error

    info = session.pop('login_info', None)
    return render_template('login.html', error=error, info=info, mode=mode, prefill=prefill,
                           google_enabled=google_enabled(), reset_token=None)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login_page'))

# ==============================================================================
# CORE & CRUD ROUTES
# ==============================================================================
@app.route('/')
@login_required
def index():
    return render_template('index.html', user=session, is_admin=is_admin_session())

@app.route('/video_feed')
@login_required
def video_feed():
    # Backward-compat: tanpa ID -> tampilkan kamera aktif pertama yang tersedia
    with camera_runtime_lock:
        cam_ids = list(camera_runtime.keys())
    if not cam_ids:
        return "Belum ada kamera aktif", 404
    return Response(generate_frames(cam_ids[0]), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/video_feed/<int:cam_id>')
@login_required
def video_feed_by_id(cam_id):
    return Response(generate_frames(cam_id), mimetype='multipart/x-mixed-replace; boundary=frame')

@app.route('/api/stats')
@login_required
def api_stats():
    # Filter opsional per-CCTV: /api/stats?camera_id=3  (kosong = semua kamera)
    cam_filter = request.args.get('camera_id', type=int)

    conn = get_db_connection()
    cursor = conn.cursor()
    if cam_filter:
        cursor.execute(
            "SELECT id, timestamp, person_id, status, camera_id, snapshot_path FROM visitor_logs "
            "WHERE camera_id = %s ORDER BY id DESC LIMIT 5", (cam_filter,))
    else:
        cursor.execute(
            "SELECT id, timestamp, person_id, status, camera_id, snapshot_path FROM visitor_logs "
            "ORDER BY id DESC LIMIT 5")
    recent_logs = cursor.fetchall()

    cursor.execute("SELECT COUNT(*) FROM cameras")
    total_cam_count = cursor.fetchone()[0]
    # Nama & kapasitas dibaca dari tabel cameras, jadi kamera baru otomatis ikut
    cursor.execute("SELECT id, name, location, max_capacity FROM cameras")
    cam_info = {r[0]: {"name": r[1], "location": r[2], "capacity": r[3] or 100} for r in cursor.fetchall()}
    cursor.close()
    conn.close()

    logs_data = [{
        "log_id": row[0],
        "time": row[1].strftime("%Y-%m-%d %H:%M:%S") if hasattr(row[1], "strftime") else row[1],
        "id": row[2],
        "status": row[3],
        "camera_id": row[4],
        "camera_name": (cam_info.get(row[4]) or {}).get("name"),
        "snapshot_url": f"/static/snapshots/{row[5]}" if row[5] else None
    } for row in recent_logs]

    # Agregat total masuk/keluar: semua kamera yang berjalan, atau 1 kamera saja bila difilter
    with camera_runtime_lock:
        if cam_filter:
            rts = [camera_runtime[cam_filter]] if cam_filter in camera_runtime else []
            running_ids = [cam_filter] if cam_filter in camera_runtime else []
        else:
            rts = list(camera_runtime.values())
            running_ids = list(camera_runtime.keys())
        total_in = sum(rt["count_in"] for rt in rts)
        total_out = sum(rt["count_out"] for rt in rts)
        running_cam_count = len(running_ids)

    if cam_filter:
        capacity = (cam_info.get(cam_filter) or {}).get("capacity", 100)
        total_cameras_shown = 1 if cam_filter in cam_info else 0
    else:
        # Kapasitas gabungan = jumlah max_capacity semua kamera
        capacity = sum(c["capacity"] for c in cam_info.values()) or 100
        total_cameras_shown = total_cam_count

    return jsonify({
        "camera_id": cam_filter,
        "count_in": total_in,
        "count_out": total_out,
        "total_inside": max(0, total_in - total_out),
        "capacity": capacity,
        "active_cameras": running_cam_count,
        "total_cameras": total_cameras_shown,
        "recent_logs": logs_data
    })

# --- STATUS SESI COUNTING (untuk banner laporan otomatis di UI) ---
@app.route('/api/session_status')
@login_required
def api_session_status():
    now = datetime.now()
    remaining = max(0, int((session_state["next_change_at"] - now).total_seconds()))
    return jsonify({
        "active": session_state["active"],
        "phase_started_at": session_state["phase_started_at"].strftime("%Y-%m-%d %H:%M:%S"),
        "next_change_at": session_state["next_change_at"].strftime("%Y-%m-%d %H:%M:%S"),
        "remaining_seconds": remaining,
        "last_auto_report": session_state["last_auto_report"]
    })

# --- DIAGNOSIS AKURASI COUNTING: apakah AI-nya kewalahan, atau penyebab lain? ---
@app.route('/api/counting_diag')
@login_required
def api_counting_diag():
    cams = []
    with camera_runtime_lock:
        for cid, rt in camera_runtime.items():
            fps = rt.get("det_fps")
            fs = rt.get("frame_size")
            cams.append({
                "camera_id": cid,
                "name": rt.get("name"),
                "ai_fps": round(fps, 1) if fps is not None else None,
                "stream_fps": round(rt["stream_fps"], 1) if rt.get("stream_fps") is not None else None,
                "frame_size": f"{fs[0]}x{fs[1]}" if fs else None,
                "active_tracks": rt["counter"].active_tracks,
                "count_in": rt["count_in"],
                "count_out": rt["count_out"],
                "verdict": ("belum ada data" if fps is None else
                            "AI terlalu lambat - counting bisa meleset" if fps < LOW_FPS_WARN else
                            "AI cukup cepat"),
            })
    return jsonify({
        "model": os.path.basename(str(YOLO_MODEL_PATH)),
        "device": "GPU" if DEVICE == 0 else "CPU",
        "imgsz": DETECT_IMGSZ,
        "conf": DETECT_CONF,
        "count_anchor": COUNT_ANCHOR,
        "deadband_frac": COUNT_DEADBAND,
        "event_queue_size": _event_queue.qsize(),
        "cameras": cams,
    })

# --- GRAFIK REALTIME (Kebutuhan #1: ganti data dummy dengan data asli) ---
@app.route('/api/hourly_traffic')
@login_required
def api_hourly_traffic():
    """
    Grafik People Count Over Time - jendela BERGULIR (rolling window) 3 jam
    terakhir dengan resolusi per-5-menit, supaya pergerakannya jelas terlihat
    dari waktu ke waktu (bukan cuma nambah pelan-pelan per jam seperti versi
    lama). Grafik ini di-refresh tiap 5 detik oleh frontend.
    """
    bucket_minutes = 5
    window_hours = 3
    cam_filter = request.args.get('camera_id', type=int)
    now = datetime.now()
    start_time = now - timedelta(hours=window_hours)
    # Bulatkan awal jendela ke kelipatan 5 menit terdekat biar label rapi
    start_time = start_time.replace(
        minute=(start_time.minute // bucket_minutes) * bucket_minutes,
        second=0, microsecond=0
    )

    conn = get_db_connection()
    cursor = conn.cursor()
    if cam_filter:
        cursor.execute("""
            SELECT timestamp, status FROM visitor_logs
            WHERE timestamp >= %s AND camera_id = %s
        """, (start_time, cam_filter))
    else:
        cursor.execute("""
            SELECT timestamp, status FROM visitor_logs
            WHERE timestamp >= %s
        """, (start_time,))
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    labels = []
    in_data = []
    out_data = []
    bucket_starts = []
    i = 0
    while True:
        bt = start_time + timedelta(minutes=bucket_minutes * i)
        if bt > now:
            break
        bucket_starts.append(bt)
        labels.append(bt.strftime("%H:%M"))
        in_data.append(0)
        out_data.append(0)
        i += 1

    for ts, status in rows:
        if hasattr(ts, "timestamp"):
            ts_dt = ts
        else:
            ts_dt = datetime.strptime(str(ts), "%Y-%m-%d %H:%M:%S")
        if ts_dt < start_time:
            continue
        idx = int((ts_dt - start_time).total_seconds() // (bucket_minutes * 60))
        if 0 <= idx < len(labels):
            if status == "MASUK":
                in_data[idx] += 1
            elif status == "KELUAR":
                out_data[idx] += 1

    return jsonify({"labels": labels, "in_data": in_data, "out_data": out_data})

# --- PEAK HOUR REALTIME (Kebutuhan #5: dulu dummy "13:00 - 14:00") ---
@app.route('/api/peak_hour')
@login_required
def api_peak_hour():
    cam_filter = request.args.get('camera_id', type=int)
    conn = get_db_connection()
    cursor = conn.cursor()
    cam_sql = " AND camera_id = %s" if cam_filter else ""
    cursor.execute(f"""
        SELECT HOUR(timestamp) as jam, COUNT(*) as total
        FROM visitor_logs
        WHERE DATE(timestamp) = CURDATE() AND status = 'MASUK'{cam_sql}
        GROUP BY jam
        ORDER BY total DESC
        LIMIT 1
    """, (cam_filter,) if cam_filter else ())
    row = cursor.fetchone()
    cursor.close()
    conn.close()

    if not row:
        return jsonify({"has_data": False, "range": "-", "total": 0})

    jam, total = row
    jam = int(jam)
    return jsonify({
        "has_data": True,
        "range": f"{jam:02d}:00 - {(jam + 1) % 24:02d}:00",
        "total": total
    })

# --- SEARCH ENGINE (Kebutuhan #4) + PENCARIAN RIWAYAT ID ORANG (Kebutuhan #3) ---
@app.route('/api/search')
@login_required
def api_search():
    q = request.args.get('q', '').strip()
    if not q:
        return jsonify({"type": "empty", "results": []})

    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # Kalau query berupa angka -> dianggap pencarian ID orang (Target ID)
    if q.isdigit():
        person_id = int(q)
        cursor.execute("""
            SELECT timestamp, status, total_in, total_out, camera_id, snapshot_path
            FROM visitor_logs WHERE person_id = %s
            ORDER BY timestamp ASC
        """, (person_id,))
        history = []
        for row in cursor.fetchall():
            h = dict(row)
            if hasattr(h.get("timestamp"), "strftime"):
                h["timestamp"] = h["timestamp"].strftime("%Y-%m-%d %H:%M:%S")
            h["snapshot_url"] = f"/static/snapshots/{h['snapshot_path']}" if h.get("snapshot_path") else None
            history.append(h)
        cursor.close()
        conn.close()

        if not history:
            return jsonify({"type": "person", "found": False, "person_id": person_id, "history": []})

        # Snapshot paling relevan untuk ditampilkan besar di modal: ambil yang
        # paling baru dari riwayat (biar wajah/postur terbaru orang tsb kelihatan)
        latest_snapshot = next((h["snapshot_url"] for h in reversed(history) if h["snapshot_url"]), None)

        return jsonify({
            "type": "person",
            "found": True,
            "person_id": person_id,
            "total_events": len(history),
            "first_seen": history[0]["timestamp"],
            "last_seen": history[-1]["timestamp"],
            "latest_snapshot": latest_snapshot,
            "history": history
        })

    # Kalau query teks -> cari di nama/lokasi kamera & daftar zona statis
    cursor.execute("""
        SELECT id, name, location, is_active FROM cameras
        WHERE name LIKE %s OR location LIKE %s
    """, (f"%{q}%", f"%{q}%"))
    camera_matches = [dict(row) for row in cursor.fetchall()]
    cursor.close()
    conn.close()

    zone_names = ["Pintu Penurunan", "Lobby Timur", "Food Court Area", "Basement Parking"]
    zone_matches = [z for z in zone_names if q.lower() in z.lower()]

    return jsonify({
        "type": "text",
        "cameras": camera_matches,
        "zones": zone_matches
    })

# --- TANDAI / LEPAS TARGET ID DI LIVE FEED (Kebutuhan #3) ---
@app.route('/api/track_person', methods=['POST'])
@admin_required
def api_track_person():
    data = request.json or {}
    person_id = data.get('person_id')
    with highlight_lock:
        highlight_state["target_id"] = int(person_id) if person_id not in (None, "") else None
    return jsonify({"status": "success", "target_id": highlight_state["target_id"]})

# --- CRUD CAMERAS API ---
@app.route('/api/cameras', methods=['GET'])
@login_required
def get_cameras():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT * FROM cameras ORDER BY id DESC")
    rows = cursor.fetchall()
    cursor.close()
    conn.close()

    result = []
    with camera_runtime_lock:
        for row in rows:
            cam = dict(row)
            if hasattr(cam.get("created_at"), "strftime"):
                cam["created_at"] = cam["created_at"].strftime("%Y-%m-%d %H:%M:%S")
            rt = camera_runtime.get(cam["id"])
            cam["is_streaming"] = rt is not None
            cam["count_in"] = rt["count_in"] if rt else 0
            cam["count_out"] = rt["count_out"] if rt else 0
            cam["occupancy"] = max(0, cam["count_in"] - cam["count_out"])
            # Jangan kirim password mentah ke frontend list, cukup tandai ada/tidaknya
            cam["has_password"] = bool(cam.get("password"))
            cam.pop("password", None)
            if not is_admin_session():
                # Akun Pengguna (Google) hanya lihat: jangan bocorkan detail koneksi CCTV
                for k in ("ip_address", "port", "username", "stream_path", "has_password"):
                    cam.pop(k, None)
            result.append(cam)
    return jsonify(result)

@app.route('/api/cameras', methods=['POST'])
@admin_required
def add_camera():
    data = request.json

    # Validasi dasar - kamera baru wajib punya nama, IP, dan password RTSP.
    # Sebelumnya field ini bisa lolos kosong (placeholder form sempat salah
    # bikin orang kira password boleh dikosongkan), lalu tersimpan ke
    # database dengan password kosong dan otomatis gagal 401 setiap saat.
    missing = []
    if not data.get('name', '').strip():
        missing.append('Nama Kamera')
    if not data.get('ip_address', '').strip():
        missing.append('Alamat IP Kamera')
    if not data.get('password', '').strip():
        missing.append('Password RTSP')
    if missing:
        return jsonify({
            "status": "error",
            "message": f"Field wajib belum diisi: {', '.join(missing)}"
        }), 400

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # Cegah 2 kamera berbeda menunjuk ke IP yang sama - ini akar dari
    # hampir semua kejadian "SINYAL TERPUTUS terus" sebelumnya: kalau 2
    # baris kamera pegang IP fisik yang sama, device-nya cuma sanggup 1
    # koneksi RTSP dan salah satunya pasti gagal/menggantung. Lebih baik
    # ditolak jelas di sini daripada baru ketahuan pas sudah connect-connect
    # gagal berulang.
    cursor.execute("SELECT id, name FROM cameras WHERE ip_address = %s", (data.get('ip_address').strip(),))
    bentrok = cursor.fetchone()
    if bentrok:
        cursor.close()
        conn.close()
        return jsonify({
            "status": "error",
            "message": f"Alamat IP ini sudah dipakai kamera \"{bentrok['name']}\" (ID {bentrok['id']}). Satu IP kamera cuma boleh dipakai 1 unit di sistem - hapus/ubah kamera itu dulu, atau pastikan IP yang kamu masukkan benar."
        }), 409

    cursor.execute("""
        INSERT INTO cameras (name, location, brand, ip_address, port, username, password, stream_path, is_active, created_at, line_position, label_masuk, label_keluar, count_direction, max_capacity, line_x1, line_y1, line_x2, line_y2, tag_name)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
    """, (
        data.get('name'),
        data.get('location'),
        data.get('brand', 'Generic'),
        data.get('ip_address'),
        int(data.get('port', 554)),
        data.get('username', ''),
        data.get('password', ''),
        data.get('stream_path', '/cam/realmonitor?channel=1&subtype=1'),
        int(data.get('is_active', 1)),
        now,
        float(data.get('line_position', 0.5)),
        data.get('label_masuk') or 'MASUK',
        data.get('label_keluar') or 'KELUAR',
        data.get('count_direction') or 'down_in',
        int(data.get('max_capacity', 100)),
        # Garis counting 360 derajat - default horizontal tengah kalau belum diatur
        float(data.get('line_x1', 0.0)),
        float(data.get('line_y1', 0.5)),
        float(data.get('line_x2', 1.0)),
        float(data.get('line_y2', 0.5)),
        (data.get('tag_name') or '').strip()[:100] or None,
    ))
    conn.commit()
    new_id = cursor.lastrowid

    # Ambil ulang row lengkap lalu langsung nyalakan counting untuk kamera baru
    # (Kebutuhan: kamera baru otomatis ke-counting & otomatis tampil di
    # Dashboard / Live Cameras tanpa perlu restart aplikasi)
    cursor.execute("SELECT * FROM cameras WHERE id = %s", (new_id,))
    new_row = cursor.fetchone()
    cursor.close()
    conn.close()

    if new_row and int(data.get('is_active', 1)) == 1:
        start_camera_worker(new_row)

    return jsonify({"status": "success", "message": "Kamera berhasil ditambahkan & langsung mulai counting", "id": new_id})

@app.route('/api/cameras/<int:cam_id>', methods=['PUT'])
@admin_required
def update_camera(cam_id):
    data = request.json
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)

    # Ambil data lama dulu, dipakai sebagai fallback untuk field yang tidak
    # dikirim (mis. saat quick-update khusus counting saja dari modal lain)
    cursor.execute("SELECT * FROM cameras WHERE id = %s", (cam_id,))
    existing = cursor.fetchone()
    if not existing:
        cursor.close()
        conn.close()
        return jsonify({"status": "error", "message": "Kamera tidak ditemukan"}), 404

    new_ip = (data.get('ip_address') or existing.get('ip_address') or '').strip()
    if new_ip:
        # Sama seperti add_camera: cegah IP bentrok dengan kamera LAIN
        # (kecuali dirinya sendiri, karena edit tanpa ganti IP itu wajar).
        cursor.execute("SELECT id, name FROM cameras WHERE ip_address = %s AND id != %s", (new_ip, cam_id))
        bentrok = cursor.fetchone()
        if bentrok:
            cursor.close()
            conn.close()
            return jsonify({
                "status": "error",
                "message": f"Alamat IP ini sudah dipakai kamera \"{bentrok['name']}\" (ID {bentrok['id']}). Satu IP kamera cuma boleh dipakai 1 unit di sistem - hapus/ubah kamera itu dulu, atau pastikan IP yang kamu masukkan benar."
            }), 409

    new_password = data.get('password') if data.get('password') else existing.get('password', '')

    cursor.execute("""
        UPDATE cameras 
        SET name=%s, location=%s, brand=%s, ip_address=%s, port=%s, username=%s, password=%s, stream_path=%s, is_active=%s,
            line_position=%s, label_masuk=%s, label_keluar=%s, count_direction=%s, max_capacity=%s,
            line_x1=%s, line_y1=%s, line_x2=%s, line_y2=%s, tag_name=%s
        WHERE id=%s
    """, (
        data.get('name', existing.get('name')),
        data.get('location', existing.get('location')),
        data.get('brand', existing.get('brand')),
        data.get('ip_address', existing.get('ip_address')),
        int(data.get('port', existing.get('port') or 554)),
        data.get('username', existing.get('username')),
        new_password,
        data.get('stream_path', existing.get('stream_path')),
        int(data.get('is_active', existing.get('is_active', 1))),
        float(data.get('line_position', existing.get('line_position') or 0.5)),
        data.get('label_masuk', existing.get('label_masuk') or 'MASUK'),
        data.get('label_keluar', existing.get('label_keluar') or 'KELUAR'),
        data.get('count_direction', existing.get('count_direction') or 'down_in'),
        int(data.get('max_capacity', existing.get('max_capacity') or 100)),
        # Garis counting 360 derajat - kalau tidak dikirim (mis. update dari
        # form lain yang tidak menyentuh garis), pertahankan nilai lama
        float(data.get('line_x1', existing.get('line_x1') if existing.get('line_x1') is not None else 0.0)),
        float(data.get('line_y1', existing.get('line_y1') if existing.get('line_y1') is not None else 0.5)),
        float(data.get('line_x2', existing.get('line_x2') if existing.get('line_x2') is not None else 1.0)),
        float(data.get('line_y2', existing.get('line_y2') if existing.get('line_y2') is not None else 0.5)),
        (data.get('tag_name', existing.get('tag_name')) or '').strip()[:100] or None,
        cam_id
    ))
    conn.commit()

    cursor.execute("SELECT * FROM cameras WHERE id = %s", (cam_id,))
    updated_row = cursor.fetchone()
    cursor.close()
    conn.close()

    # Sinkronkan worker counting sesuai status terbaru: kalau dinonaktifkan ->
    # stop; kalau aktif (baru diaktifkan / kredensial/garis/label berubah) -> (re)start
    # HANYA restart worker kalau memang perlu. Restart memutus stream & (dulu)
    # mereset hitungan, jadi dihindari untuk perubahan yang tidak menyentuh
    # koneksi RTSP (nama, lokasi, garis, label, arah, kapasitas, tag).
    if updated_row:
        if int(updated_row.get('is_active', 1)) == 1:
            stream_changed = any(
                str(existing.get(f) if existing.get(f) is not None else '') !=
                str(updated_row.get(f) if updated_row.get(f) is not None else '')
                for f in STREAM_FIELDS
            )
            with camera_runtime_lock:
                already_running = (camera_runtime.get(cam_id) or {}).get("running", False)
            if already_running and not stream_changed:
                apply_camera_config_live(updated_row)
            else:
                start_camera_worker(updated_row)
        else:
            stop_camera_worker(cam_id)

    return jsonify({"status": "success", "message": "Kamera berhasil diperbarui"})

@app.route('/api/cameras/<int:cam_id>', methods=['DELETE'])
@admin_required
def delete_camera(cam_id):
    stop_camera_worker(cam_id)
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM cameras WHERE id=%s", (cam_id,))
    conn.commit()
    cursor.close()
    conn.close()
    return jsonify({"status": "success", "message": "Kamera berhasil dihapus"})

# --- POSISI TITIK KAMERA DI DENAH (menu Zones) ---
# Endpoint terpisah & ringan: hanya menyentuh kolom map_x/map_y, tidak
# menyentuh worker kamera / counting sama sekali.
@app.route('/api/cameras/<int:cam_id>/map_position', methods=['PUT'])
@admin_required
def update_camera_map_position(cam_id):
    data = request.get_json(silent=True) or {}
    raw_x, raw_y = data.get('map_x'), data.get('map_y')

    if raw_x is None and raw_y is None:
        # Reset: kembalikan kamera ke status "belum diposisikan"
        map_x = map_y = None
    else:
        try:
            map_x = min(1.0, max(0.0, float(raw_x)))
            map_y = min(1.0, max(0.0, float(raw_y)))
        except (TypeError, ValueError):
            return jsonify({"status": "error", "message": "Posisi tidak valid"}), 400

    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM cameras WHERE id = %s", (cam_id,))
    if not cursor.fetchone():
        cursor.close()
        conn.close()
        return jsonify({"status": "error", "message": "Kamera tidak ditemukan"}), 404
    cursor.execute("UPDATE cameras SET map_x = %s, map_y = %s WHERE id = %s", (map_x, map_y, cam_id))
    conn.commit()
    cursor.close()
    conn.close()
    return jsonify({"status": "success", "map_x": map_x, "map_y": map_y})

@app.route('/api/cameras/test-connection', methods=['POST'])
@admin_required
def test_connection():
    data = request.json
    rtsp_url = build_rtsp_url(data)
    print(f"[TEST-CONNECTION] Mencoba konek ke: {rtsp_url.split('@')[-1] if '@' in rtsp_url else rtsp_url}")

    # PENTING: cap.open() dijalankan di THREAD TERPISAH dengan batas waktu
    # KERAS dari Python sendiri (join timeout), bukan cuma mengandalkan
    # CAP_PROP_OPEN_TIMEOUT_MSEC punya OpenCV/FFmpeg. Timeout bawaan OpenCV
    # itu TIDAK SELALU DIHORMATI - kalau TCP handshake ke device macet total
    # (mis. device cuma bisa 1 koneksi RTSP dan IP-nya sudah dipakai kamera
    # lain yang sedang berjalan), cap.open() bisa menggantung JAUH lebih
    # lama dari 8 detik, bahkan tanpa batas - dan karena itu panggilan C-
    # extension yang blocking, request Flask ini ikut menggantung selamanya,
    # bikin tombol "Tes Koneksi" & "Simpan" di web kelihatan macet/ngebug.
    # Dengan pendekatan ini, permintaan HTTP tetap dibalas dalam waktu wajar
    # meskipun percobaan koneksi di baliknya masih menggantung di background.
    result = {"opened": False, "success": False, "elapsed": 0.0}

    def _attempt():
        cap = cv2.VideoCapture()
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        try:
            cap.set(cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 8000)
            cap.set(cv2.CAP_PROP_READ_TIMEOUT_MSEC, 8000)
        except Exception:
            pass
        t0 = time.time()
        opened = cap.open(rtsp_url, cv2.CAP_FFMPEG)
        result["opened"] = opened
        result["elapsed"] = time.time() - t0
        if opened:
            for attempt in range(5):
                success, _ = cap.read()
                if success:
                    result["success"] = True
                    break
                time.sleep(0.5)
        cap.release()

    worker = threading.Thread(target=_attempt, daemon=True)
    worker.start()
    worker.join(timeout=12)  # batas keras dari sisi Python - request TIDAK akan menggantung lebih dari ini

    if worker.is_alive():
        # Masih menggantung di titik ini (biasanya TCP connect yang macet
        # total). Balas ke browser sekarang juga - thread di background akan
        # tetap jalan sendiri sampai selesai/timeout OpenCV-nya lalu berhenti.
        print(f"[TEST-CONNECTION] Timeout - percobaan koneksi masih menggantung setelah 12s (kemungkinan IP sudah dipakai kamera lain, atau device tidak merespon sama sekali).")
        return jsonify({
            "status": "offline",
            "message": "Waktu tes habis (>12s). Kemungkinan IP ini sudah dipakai kamera lain yang sedang aktif, atau device sama sekali tidak merespon."
        }), 408

    print(f"[TEST-CONNECTION] cap.open() {'berhasil' if result['opened'] else 'GAGAL'} setelah {result['elapsed']:.1f}s")

    if result["success"]:
        print(f"[TEST-CONNECTION] Berhasil.")
        return jsonify({"status": "online", "message": "Koneksi RTSP Berhasil! Feed Aktif."})
    else:
        print(f"[TEST-CONNECTION] Gagal - cek log [rtsp @ ...] atau [STREAM] di atas untuk alasan pastinya (401/404/timeout).")
        return jsonify({"status": "offline", "message": "Gagal terhubung. Cek IP, Port, atau Password. (Detail error ada di terminal server)"}), 400

@app.route('/api/auto_reports')
@admin_required
def api_auto_reports():
    files = []
    if os.path.isdir(AUTO_REPORT_DIR):
        for f in sorted(os.listdir(AUTO_REPORT_DIR), reverse=True):
            if f.lower().endswith('.xlsx') and f.startswith('Laporan_People_Counting_Sarana_'):
                files.append(f)
    return jsonify({"folder": AUTO_REPORT_DIR, "files": files[:20]})

@app.route('/download_auto_report/<path:filename>')
@admin_required
def download_auto_report(filename):
    filepath = os.path.join(AUTO_REPORT_DIR, filename)
    if not os.path.isfile(filepath):
        return jsonify({"status": "error", "message": "File tidak ditemukan"}), 404
    return send_file(filepath, as_attachment=True)

@app.route('/api/report')
@login_required
def api_report():
    """Data halaman laporan. Tanpa parameter = 30 hari terakhir s/d hari ini.
    ?start_date=YYYY-MM-DD&end_date=YYYY-MM-DD untuk periode manual."""
    start, end = resolve_report_range(request.args.get('start_date'), request.args.get('end_date'))
    try:
        rows = fetch_daily_summary(start, end)
    except Exception as e:
        print(f"[REPORT][ERROR] {e}")
        return jsonify({"status": "error", "message": "Gagal mengambil data laporan dari database."}), 500
    return jsonify({
        "status": "ok",
        "start_date": start.strftime("%Y-%m-%d"),
        "end_date": end.strftime("%Y-%m-%d"),
        "period_label": f"{_fmt_tanggal_id(start)} - {_fmt_tanggal_id(end)}",
        "has_data": bool(rows),
        "total_masuk": sum(r["masuk"] for r in rows),
        "total_keluar": sum(r["keluar"] for r in rows),
        "rows": [{
            "no": i,
            "tanggal": r["tanggal"].strftime("%Y-%m-%d"),
            "tanggal_label": r["tanggal"].strftime("%d-%m-%Y"),
            "camera_id": r["camera_id"],
            "kamera": r["kamera"],
            "masuk": r["masuk"],
            "keluar": r["keluar"],
        } for i, r in enumerate(rows, start=1)]
    })

@app.route('/export_excel')
@admin_required
def export_excel():
    """Export XLSX untuk periode yang sama dengan /api/report (parameter identik)."""
    start, end = resolve_report_range(request.args.get('start_date'), request.args.get('end_date'))
    buf = io.BytesIO()
    write_report_file(buf, start, end, base_url=request.host_url)
    buf.seek(0)
    return send_file(buf, as_attachment=True,
                     download_name=f"Laporan_People_Counting_{start:%Y-%m-%d}_sd_{end:%Y-%m-%d}.xlsx",
                     mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)