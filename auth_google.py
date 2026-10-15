"""
Autentikasi dua jalur untuk Tirtopeople Counting
================================================
1. ADMIN    : username + password (tabel users, auth_provider = 'local')
2. PENGGUNA : login / daftar otomatis lewat akun Google (auth_provider = 'google',
              role = 'Pengguna', hak akses hanya LIHAT)

Setiap login dicatat di tabel login_logs supaya admin bisa memastikan siapa saja
yang mengakses web (menu "Pengguna").

Konfigurasi lewat environment variable (tanpa edit kode):
    GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET   (wajib untuk login Google)
    GOOGLE_REDIRECT_URI                      (opsional, default dibentuk otomatis)
    SECRET_KEY                               (disarankan, untuk tanda tangan sesi)
"""
import hmac
import os
import re
import secrets
import time
from datetime import datetime
from functools import wraps

from flask import jsonify, redirect, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

ROLE_USER = "Pengguna"            # akun Google / email: hanya lihat
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 8
MAX_FAILS, FAIL_WINDOW = 8, 600   # batas percobaan gagal per IP: 8x / 10 menit
_fails = {}
MAX_EMAIL_FAILS = 5               # batas gagal per akun email: 5x / 10 menit (menahan serangan dari banyak IP)
_email_fails = {}
HASH_PREFIXES = ("scrypt:", "pbkdf2:")
_DUMMY_HASH = generate_password_hash("dummy-untuk-menyamakan-waktu")
RECHECK_SECONDS = 60              # cek ulang status aktif akun Pengguna tiap N detik

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "").strip()

_google = None  # klien OAuth, diisi oleh init_auth()


def google_enabled():
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def is_admin_session():
    return "user_id" in session and session.get("role") != ROLE_USER


def admin_required(f):
    """Hanya admin. Akun 'Pengguna' (Google) ditolak di sisi server."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"status": "error", "message": "Sesi habis, silakan login ulang"}), 401
            return redirect(url_for("login_page"))
        if session.get("role") == ROLE_USER:
            if request.path.startswith("/api/") or request.method != "GET":
                return jsonify({"status": "error", "message": "Akses ditolak: fitur ini hanya untuk admin"}), 403
            return "Akses ditolak: fitur ini hanya untuk admin.", 403
        return f(*args, **kwargs)
    return decorated


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _throttled():
    ip = _client_ip()
    now = time.time()
    _fails[ip] = [t for t in _fails.get(ip, []) if now - t < FAIL_WINDOW]
    return len(_fails[ip]) >= MAX_FAILS


def _note_fail():
    _fails.setdefault(_client_ip(), []).append(time.time())


def _email_throttled(email):
    now = time.time()
    _email_fails[email] = [t for t in _email_fails.get(email, []) if now - t < FAIL_WINDOW]
    return len(_email_fails[email]) >= MAX_EMAIL_FAILS


def _note_email_fail(email):
    _email_fails.setdefault(email, []).append(time.time())


def note_login_fail():
    _note_fail()


def authenticate_local(get_db_connection, username, password):
    """Login username + kata sandi (admin DAN pengguna yang dibuat admin).
    Kembalikan (user, None) bila berhasil, atau (None, pesan_error).
    Kata sandi lama yang masih polos otomatis di-hash saat login pertama yang berhasil."""
    generic = "Username atau kata sandi salah."
    if _throttled():
        return None, "Terlalu banyak percobaan. Coba lagi beberapa menit lagi."
    conn = get_db_connection()
    cur = conn.cursor(dictionary=True)
    ok = False
    user = None
    try:
        cur.execute("SELECT * FROM users WHERE username = %s AND auth_provider IN ('local', 'email') LIMIT 1", (username,))
        user = cur.fetchone()
        if user and user.get("role") == ROLE_USER and _email_throttled(username.lower()):
            return None, "Terlalu banyak percobaan untuk akun ini. Coba lagi beberapa menit lagi."
        stored = (user or {}).get("password") or ""
        if user and stored.startswith(HASH_PREFIXES):
            ok = check_password_hash(stored, password)
        elif user and not stored.startswith("!") and stored:
            ok = hmac.compare_digest(stored.encode("utf-8"), password.encode("utf-8"))
            if ok:
                cur.execute("UPDATE users SET password = %s WHERE id = %s", (generate_password_hash(password), user["id"]))
                conn.commit()
        else:
            check_password_hash(_DUMMY_HASH, password)   # samakan waktu respons
    finally:
        cur.close()
        conn.close()
    if not ok:
        _note_fail()
        if user and user.get("role") == ROLE_USER:
            _note_email_fail(username.lower())
        return None, generic
    if not user.get("is_active", 1):
        return None, "Akun Anda dinonaktifkan oleh admin."
    return user, None


def _client_ip():
    """IP pengunjung. Header X-Forwarded-For TIDAK dipercaya langsung (bisa dipalsukan pengunjung);
    di belakang Nginx set TRUST_PROXY=1 agar ProxyFix mengisi remote_addr dari proxy yang dipercaya."""
    return request.remote_addr or ""


# ------------------------------------------------------------------------------
# SKEMA DATABASE (dipanggil dari init_db di main.py, cursor biasa / non-dictionary)
# ------------------------------------------------------------------------------
def ensure_auth_schema(cursor):
    cursor.execute("SHOW COLUMNS FROM users")
    existing = {r[0] for r in cursor.fetchall()}
    migrations = {
        "email": "ALTER TABLE users ADD COLUMN email VARCHAR(190) DEFAULT NULL",
        "auth_provider": "ALTER TABLE users ADD COLUMN auth_provider VARCHAR(20) NOT NULL DEFAULT 'local'",
        "google_sub": "ALTER TABLE users ADD COLUMN google_sub VARCHAR(64) DEFAULT NULL",
        "picture": "ALTER TABLE users ADD COLUMN picture VARCHAR(500) DEFAULT NULL",
        "is_active": "ALTER TABLE users ADD COLUMN is_active TINYINT(1) NOT NULL DEFAULT 1",
        "created_at": "ALTER TABLE users ADD COLUMN created_at DATETIME DEFAULT NULL",
        "last_login": "ALTER TABLE users ADD COLUMN last_login DATETIME DEFAULT NULL",
        "login_count": "ALTER TABLE users ADD COLUMN login_count INT NOT NULL DEFAULT 0",
        "password_changed_at": "ALTER TABLE users ADD COLUMN password_changed_at DATETIME DEFAULT NULL",
        "last_ip": "ALTER TABLE users ADD COLUMN last_ip VARCHAR(64) DEFAULT NULL",
    }
    for col, ddl in migrations.items():
        if col not in existing:
            cursor.execute(ddl)

    cursor.execute("SHOW INDEX FROM users")
    index_names = {r[2] for r in cursor.fetchall()}
    if "uq_users_email" not in index_names:
        cursor.execute("ALTER TABLE users ADD UNIQUE KEY uq_users_email (email)")
    if "uq_users_google_sub" not in index_names:
        cursor.execute("ALTER TABLE users ADD UNIQUE KEY uq_users_google_sub (google_sub)")

    cursor.execute("""
        CREATE TABLE IF NOT EXISTS login_logs (
            id INT AUTO_INCREMENT PRIMARY KEY,
            user_id INT NOT NULL,
            username VARCHAR(190),
            full_name VARCHAR(150),
            email VARCHAR(190),
            role VARCHAR(50),
            provider VARCHAR(20),
            ip_address VARCHAR(64),
            user_agent VARCHAR(255),
            logged_in_at DATETIME,
            INDEX idx_login_logs_time (logged_in_at),
            INDEX idx_login_logs_user (user_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)


# ------------------------------------------------------------------------------
# PENCATATAN LOGIN
# ------------------------------------------------------------------------------
def start_session(user, provider, get_db_connection):
    """Isi session + catat login (dipakai login admin DAN login Google)."""
    session.clear()
    session["user_id"] = user["id"]
    session["username"] = user["username"]
    session["full_name"] = user["full_name"]
    session["role"] = user["role"]
    session["email"] = user.get("email")
    session["active_checked_at"] = datetime.now().timestamp()
    session["pwv"] = str(user.get("password_changed_at") or "")

    conn = get_db_connection()
    cur = conn.cursor()
    ts = _now()
    cur.execute(
        "UPDATE users SET last_login = %s, last_ip = %s, login_count = COALESCE(login_count, 0) + 1 WHERE id = %s",
        (ts, _client_ip()[:64], user["id"]),
    )
    cur.execute(
        """INSERT INTO login_logs
           (user_id, username, full_name, email, role, provider, ip_address, user_agent, logged_in_at)
           VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (user["id"], user["username"], user["full_name"], user.get("email"), user["role"],
         provider, _client_ip()[:64], (request.headers.get("User-Agent", "") or "")[:255], ts),
    )
    conn.commit()
    cur.close()
    conn.close()


# ------------------------------------------------------------------------------
# REGISTRASI ROUTE
# ------------------------------------------------------------------------------
def init_auth(app, get_db_connection):
    global _google

    if google_enabled():
        from authlib.integrations.flask_client import OAuth
        oauth = OAuth(app)
        oauth.register(
            name="google",
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )
        _google = oauth.google

    if not google_enabled():
        print("[auth] Login Google NONAKTIF: GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET belum di-set. "
              "Pengguna belum bisa masuk (hanya admin).")
    else:
        print("[auth] Login Google aktif. Redirect URI harus terdaftar di Google Cloud Console: "
              + (GOOGLE_REDIRECT_URI or "<alamat-server>/auth/google/callback (dibentuk otomatis dari alamat yang dibuka)"))

    def login_error(msg, mode="user", prefill=None):
        session["login_error"] = msg
        session["login_mode"] = mode
        if prefill:
            session["login_prefill"] = prefill
        return redirect(url_for("login_page"))

    from auth_security import init_security
    init_security(app, login_error)

    @app.route("/auth/google")
    def auth_google():
        if not google_enabled() or _google is None:
            return login_error("Login Google belum dikonfigurasi di server. Hubungi admin.")
        redirect_uri = GOOGLE_REDIRECT_URI or url_for("auth_google_callback", _external=True)
        return _google.authorize_redirect(redirect_uri, prompt="select_account")

    @app.route("/auth/google/callback")
    def auth_google_callback():
        if not google_enabled() or _google is None:
            return login_error("Login Google belum dikonfigurasi di server. Hubungi admin.")
        try:
            token = _google.authorize_access_token()
            info = token.get("userinfo") or _google.userinfo()
        except Exception as exc:
            # Penyebab umum: redirect_uri_mismatch, mismatching_state (SECRET_KEY/cookie), client secret salah
            app.logger.exception("Login Google gagal: %s", exc)
            return login_error("Login Google gagal (%s). Silakan coba lagi." % type(exc).__name__)

        email = (info.get("email") or "").strip().lower()
        sub = str(info.get("sub") or "")
        if not email or not sub or not info.get("email_verified", False):
            return login_error("Email Google belum terverifikasi.")

        allowed = [d.strip().lower() for d in os.environ.get("ALLOWED_EMAIL_DOMAINS", "").split(",") if d.strip()]
        if allowed and email.rsplit("@", 1)[-1] not in allowed:
            return login_error("Hanya akun email dengan domain berikut yang diizinkan: " + ", ".join(allowed) + ".")

        full_name = (info.get("name") or email.split("@")[0]).strip()[:150]
        picture = (info.get("picture") or "")[:500] or None

        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("SELECT * FROM users WHERE google_sub = %s OR email = %s LIMIT 1", (sub, email))
            row = cur.fetchone()
            if row and row.get("role") != ROLE_USER:
                return login_error("Email ini terdaftar sebagai akun admin. Gunakan Login Admin.")

            if row:
                # Akun lama (daftar email/kata sandi) dialihkan ke Google; kata sandi lamanya dimatikan.
                cur.execute(
                    "UPDATE users SET full_name = %s, email = %s, google_sub = %s, picture = %s, "
                    "auth_provider = 'google', password = %s WHERE id = %s",
                    (full_name, email, sub, picture, "!" + secrets.token_hex(16), row["id"]),
                )
                user_id = row["id"]
            else:
                # Daftar otomatis: akun langsung aktif, role Pengguna (hanya lihat)
                cur.execute(
                    """INSERT INTO users
                       (username, password, full_name, role, email, auth_provider, google_sub, picture, is_active, created_at)
                       VALUES (%s, %s, %s, %s, %s, 'google', %s, %s, 1, %s)""",
                    (email, "!" + secrets.token_hex(16), full_name, ROLE_USER, email, sub, picture, _now()),
                )
                user_id = cur.lastrowid
            conn.commit()
            cur.execute("SELECT * FROM users WHERE id = %s", (user_id,))
            user = cur.fetchone()
        except Exception:
            conn.rollback()
            return login_error("Gagal memproses pendaftaran akun. Silakan coba lagi.")
        finally:
            cur.close()
            conn.close()

        if not user.get("is_active", 1):
            return login_error("Akun Anda dinonaktifkan oleh admin.")

        start_session(user, "google", get_db_connection)
        return redirect(url_for("index"))

    # Akun Pengguna yang dinonaktifkan admin langsung terputus (cek tiap RECHECK_SECONDS)
    @app.before_request
    def recheck_user_active():
        if session.get("role") != ROLE_USER or "user_id" not in session:
            return None
        if request.endpoint in ("static", "logout", "login_page"):
            return None
        now = datetime.now().timestamp()
        if now - session.get("active_checked_at", 0) < RECHECK_SECONDS:
            return None
        conn = get_db_connection()
        cur = conn.cursor()
        cur.execute("SELECT is_active, password_changed_at FROM users WHERE id = %s", (session["user_id"],))
        row = cur.fetchone()
        cur.close()
        conn.close()
        pw_changed = bool(row) and str(row[1] or "") != session.get("pwv", "")
        if not row or not row[0] or pw_changed:
            msg = "Kata sandi akun diubah. Silakan masuk kembali." if (row and row[0] and pw_changed) else "Akun Anda dinonaktifkan oleh admin."
            session.clear()
            if request.path.startswith("/api/") or request.path.startswith("/video_feed"):
                return jsonify({"status": "error", "message": "Akun dinonaktifkan"}), 401
            session["login_error"] = msg
            session["login_mode"] = "masuk"
            return redirect(url_for("login_page"))
        session["active_checked_at"] = now
        return None

    # ---------------- API khusus admin: daftar pengguna & riwayat login ----------------
    def _fmt(v):
        return v.strftime("%Y-%m-%d %H:%M:%S") if hasattr(v, "strftime") else v

    @app.route("/api/users")
    @admin_required
    def api_users():
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        cur.execute("""SELECT id, username, full_name, email, role, auth_provider, picture,
                              is_active, created_at, last_login, login_count, last_ip
                       FROM users ORDER BY (role = %s) DESC, last_login DESC, id DESC""", (ROLE_USER,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        for r in rows:
            r["created_at"] = _fmt(r.get("created_at"))
            r["last_login"] = _fmt(r.get("last_login"))
        return jsonify(rows)

    USERNAME_RE = re.compile(r"^[A-Za-z0-9._@-]{3,50}$")

    @app.route("/api/users", methods=["POST"])
    @admin_required
    def api_user_create():
        from auth_security import validate_password
        data = request.get_json(silent=True) or {}
        name = (data.get("full_name") or "").strip()[:150]
        username = (data.get("username") or "").strip()
        pw = data.get("password") or ""
        if len(name) < 2:
            return jsonify({"status": "error", "message": "Nama lengkap wajib diisi"}), 400
        if not USERNAME_RE.match(username) or username.lower() == "admin":
            return jsonify({"status": "error", "message": "Username 3-50 karakter (huruf, angka, . _ - @) dan bukan 'admin'"}), 400
        err = validate_password(pw, username)
        if err:
            return jsonify({"status": "error", "message": err}), 400
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        try:
            cur.execute("SELECT id FROM users WHERE username = %s OR email = %s LIMIT 1", (username, username))
            if cur.fetchone():
                return jsonify({"status": "error", "message": "Username sudah dipakai"}), 409
            cur.execute(
                """INSERT INTO users (username, password, full_name, role, auth_provider, is_active, created_at)
                   VALUES (%s, %s, %s, %s, 'local', 1, %s)""",
                (username, generate_password_hash(pw), name, ROLE_USER, _now()),
            )
            new_id = cur.lastrowid
            conn.commit()
        except Exception:
            conn.rollback()
            app.logger.exception("Gagal membuat pengguna")
            return jsonify({"status": "error", "message": "Gagal membuat akun"}), 500
        finally:
            cur.close()
            conn.close()
        return jsonify({"status": "success", "id": new_id})

    def _get_managed_user(cur, user_id):
        cur.execute("SELECT id, role, auth_provider FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        if not row:
            return None, (jsonify({"status": "error", "message": "Pengguna tidak ditemukan"}), 404)
        if row["role"] != ROLE_USER:
            return None, (jsonify({"status": "error", "message": "Akun admin tidak bisa diubah dari sini"}), 400)
        return row, None

    @app.route("/api/users/<int:user_id>/password", methods=["PUT"])
    @admin_required
    def api_user_password(user_id):
        from auth_security import validate_password
        data = request.get_json(silent=True) or {}
        pw = data.get("password") or ""
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        try:
            row, err_resp = _get_managed_user(cur, user_id)
            if err_resp:
                return err_resp
            if row["auth_provider"] == "google":
                return jsonify({"status": "error", "message": "Akun ini memakai Google, tidak punya kata sandi"}), 400
            cur.execute("SELECT username FROM users WHERE id = %s", (user_id,))
            err = validate_password(pw, (cur.fetchone() or {}).get("username", ""))
            if err:
                return jsonify({"status": "error", "message": err}), 400
            cur.execute("UPDATE users SET password = %s, password_changed_at = %s WHERE id = %s",
                        (generate_password_hash(pw), _now(), user_id))
            conn.commit()
        finally:
            cur.close()
            conn.close()
        return jsonify({"status": "success"})

    @app.route("/api/users/<int:user_id>", methods=["DELETE"])
    @admin_required
    def api_user_delete(user_id):
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        try:
            row, err_resp = _get_managed_user(cur, user_id)
            if err_resp:
                return err_resp
            cur.execute("DELETE FROM users WHERE id = %s", (user_id,))
            conn.commit()
        finally:
            cur.close()
            conn.close()
        return jsonify({"status": "success"})

    @app.route("/api/users/<int:user_id>/active", methods=["PUT"])
    @admin_required
    def api_user_active(user_id):
        data = request.get_json(silent=True) or {}
        active = 1 if data.get("is_active") else 0
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT id, role FROM users WHERE id = %s", (user_id,))
        row = cur.fetchone()
        if not row:
            cur.close(); conn.close()
            return jsonify({"status": "error", "message": "Pengguna tidak ditemukan"}), 404
        if row["role"] != ROLE_USER:
            cur.close(); conn.close()
            return jsonify({"status": "error", "message": "Akun admin tidak bisa dinonaktifkan dari sini"}), 400
        cur.execute("UPDATE users SET is_active = %s WHERE id = %s", (active, user_id))
        conn.commit()
        cur.close()
        conn.close()
        return jsonify({"status": "success", "is_active": active})

    @app.route("/api/login_logs")
    @admin_required
    def api_login_logs():
        try:
            limit = min(500, max(1, int(request.args.get("limit", 100))))
        except ValueError:
            limit = 100
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        cur.execute("""SELECT id, user_id, username, full_name, email, role, provider,
                              ip_address, logged_in_at
                       FROM login_logs ORDER BY id DESC LIMIT %s""", (limit,))
        rows = cur.fetchall()
        cur.close()
        conn.close()
        for r in rows:
            r["logged_in_at"] = _fmt(r.get("logged_in_at"))
        return jsonify(rows)
