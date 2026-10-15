# ==============================================================================
# SKRIP MIGRASI DATA LAMA: SQLite (cctv_counting.db) -> MySQL (localhost)
# ==============================================================================
# Jalankan skrip ini SEKALI SAJA (satu kali) kalau Anda punya data lama di
# file cctv_counting.db (dari versi sebelum pakai MySQL) dan ingin datanya
# (daftar kamera, riwayat log MASUK/KELUAR) dipindahkan ke MySQL supaya
# tidak hilang.
#
# Cara pakai:
#   1. Pastikan file cctv_counting.db (yang lama) ada di folder yang sama
#      dengan skrip ini.
#   2. Pastikan MySQL (XAMPP/Laragon) sudah menyala.
#   3. Jalankan main.py TERLEBIH DAHULU minimal sekali (biar tabel MySQL
#      otomatis terbentuk), lalu stop.
#   4. Baru jalankan: python migrate_sqlite_to_mysql.py
#
# Skrip ini AMAN dijalankan berkali-kali - data yang sudah ada tidak akan
# digandakan (cameras & visitor_logs dicek dulu, kalau tabel MySQL sudah
# tidak kosong, migrasi akan dilewati untuk tabel tersebut).
# ==============================================================================
import sqlite3
import mysql.connector

SQLITE_DB_PATH = "cctv_counting.db"

MYSQL_CONFIG = {
    "host": "localhost",
    "port": 3306,
    "user": "root",
    "password": "",
    "database": "tirtopeople_counting"
}


def migrate():
    try:
        sqlite_conn = sqlite3.connect(SQLITE_DB_PATH)
        sqlite_conn.row_factory = sqlite3.Row
    except Exception as e:
        print(f"[MIGRASI] Tidak bisa membuka {SQLITE_DB_PATH}: {e}")
        print("[MIGRASI] Lewati migrasi - mungkin memang belum ada data lama.")
        return

    mysql_conn = mysql.connector.connect(**MYSQL_CONFIG)
    mysql_cursor = mysql_conn.cursor()

    # --- Migrasi tabel cameras ---
    mysql_cursor.execute("SELECT COUNT(*) FROM cameras")
    if mysql_cursor.fetchone()[0] <= 1:  # cuma ada seed default / kosong
        sqlite_cursor = sqlite_conn.cursor()
        try:
            sqlite_cursor.execute("SELECT * FROM cameras")
            rows = sqlite_cursor.fetchall()
            for row in rows:
                r = dict(row)
                mysql_cursor.execute("""
                    INSERT INTO cameras (name, location, brand, ip_address, port, username, password, stream_path, is_active, created_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """, (
                    r.get("name"), r.get("location"), r.get("brand"), r.get("ip_address"),
                    r.get("port"), r.get("username"), r.get("password"), r.get("stream_path"),
                    r.get("is_active"), r.get("created_at")
                ))
            mysql_conn.commit()
            print(f"[MIGRASI] {len(rows)} data kamera berhasil dipindahkan ke MySQL.")
        except sqlite3.OperationalError:
            print("[MIGRASI] Tabel 'cameras' tidak ditemukan di SQLite lama, dilewati.")
    else:
        print("[MIGRASI] Tabel cameras di MySQL sudah berisi data, migrasi kamera dilewati.")

    # --- Migrasi tabel visitor_logs ---
    mysql_cursor.execute("SELECT COUNT(*) FROM visitor_logs")
    if mysql_cursor.fetchone()[0] == 0:
        sqlite_cursor = sqlite_conn.cursor()
        try:
            sqlite_cursor.execute("SELECT * FROM visitor_logs")
            rows = sqlite_cursor.fetchall()
            for row in rows:
                r = dict(row)
                mysql_cursor.execute("""
                    INSERT INTO visitor_logs (timestamp, person_id, status, total_in, total_out, camera_id, snapshot_path)
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                """, (
                    r.get("timestamp"), r.get("person_id"), r.get("status"),
                    r.get("total_in"), r.get("total_out"), r.get("camera_id"), r.get("snapshot_path")
                ))
            mysql_conn.commit()
            print(f"[MIGRASI] {len(rows)} riwayat log MASUK/KELUAR berhasil dipindahkan ke MySQL.")
        except sqlite3.OperationalError:
            print("[MIGRASI] Tabel 'visitor_logs' tidak ditemukan di SQLite lama, dilewati.")
    else:
        print("[MIGRASI] Tabel visitor_logs di MySQL sudah berisi data, migrasi log dilewati.")

    mysql_cursor.close()
    mysql_conn.close()
    sqlite_conn.close()
    print("[MIGRASI] Selesai.")


if __name__ == "__main__":
    migrate()
