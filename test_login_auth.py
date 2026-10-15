"""Uji login username + kata sandi + captcha, pengguna buatan admin, pencatatan IP, CSRF, pembatasan percobaan, hak akses, Google opsional.
Memakai SQLite in-memory sebagai pengganti MySQL (adapter kecil di bawah). Jalankan: python test_login_auth.py"""
import os, re, sqlite3, tempfile, unittest
from flask import Flask, session, render_template, redirect, url_for
import auth_google as ag
import auth_security as sec


class _Cur:
    def __init__(self, conn, dictionary=False):
        self.c, self.d = conn.cursor(), dictionary
    def execute(self, sql, params=()):
        self.c.execute(sql.replace("%s", "?"), params); return self
    def fetchone(self):
        r = self.c.fetchone()
        return dict(r) if (r is not None and self.d) else r
    def fetchall(self):
        return [dict(r) for r in self.c.fetchall()] if self.d else self.c.fetchall()
    @property
    def lastrowid(self): return self.c.lastrowid
    @property
    def rowcount(self): return self.c.rowcount
    def close(self): pass


class _Conn:
    def __init__(self, raw): self.raw = raw
    def cursor(self, dictionary=False): return _Cur(self.raw, dictionary)
    def commit(self): self.raw.commit()
    def rollback(self): self.raw.rollback()
    def close(self): pass


def make_app():
    raw = sqlite3.connect(":memory:", check_same_thread=False)
    raw.row_factory = sqlite3.Row
    raw.executescript("""
      CREATE TABLE users(id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE NOT NULL, password TEXT NOT NULL,
        full_name TEXT NOT NULL, role TEXT, email TEXT UNIQUE, auth_provider TEXT NOT NULL DEFAULT 'local', google_sub TEXT UNIQUE,
        picture TEXT, is_active INTEGER NOT NULL DEFAULT 1, created_at TEXT, last_login TEXT, login_count INTEGER NOT NULL DEFAULT 0, password_changed_at TEXT, last_ip TEXT);
      CREATE TABLE login_logs(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INT, username TEXT, full_name TEXT, email TEXT,
        role TEXT, provider TEXT, ip_address TEXT, user_agent TEXT, logged_in_at TEXT);
      INSERT INTO users(id,username,password,full_name,role,auth_provider,is_active) VALUES(1,'admin','admin001','Sarana digital','Sarana Digital','local',1);
    """)
    get_db = lambda: _Conn(raw)
    here = os.path.dirname(os.path.abspath(__file__))
    app = Flask(__name__, template_folder=os.path.join(here, "templates"), static_folder=os.path.join(here, "static"))
    app.secret_key = "test"
    ag.init_auth(app, get_db)

    # Fungsi login_page ASLI diambil dari main.py (main.py tidak bisa diimpor langsung: butuh MySQL/kamera)
    import ast
    from flask import request, redirect as _redirect
    src = open(os.path.join(here, "main.py"), encoding="utf-8").read()
    fn = next(n for n in ast.parse(src).body if isinstance(n, ast.FunctionDef) and n.name == "login_page")
    fn.decorator_list = []
    mod = ast.Module(body=[fn], type_ignores=[])
    ns = dict(session=session, request=request, redirect=_redirect, url_for=url_for, render_template=render_template,
              get_db_connection=get_db, start_session=ag.start_session, authenticate_local=ag.authenticate_local, captcha_ok=sec.captcha_ok, note_login_fail=ag.note_login_fail, ROLE_USER=ag.ROLE_USER, google_enabled=ag.google_enabled)
    exec(compile(ast.fix_missing_locations(mod), "main.py:login_page", "exec"), ns)
    app.add_url_rule("/login", "login_page", ns["login_page"], methods=["GET", "POST"])

    @app.route("/")
    def index():
        return "role=" + str(session.get("role")) if "user_id" in session else redirect(url_for("login_page"))

    @app.route("/ubah")
    @ag.admin_required
    def ubah(): return "ok"
    return app, raw


class _FakeGoogle:
    info = {}
    def authorize_redirect(self, uri, **kw): return "REDIRECT-GOOGLE"
    def authorize_access_token(self): return {"userinfo": dict(self.info)}


class LoginTest(unittest.TestCase):
    def setUp(self):
        ag._fails.clear(); ag._email_fails.clear(); sec._used_captcha.clear(); ag.RECHECK_SECONDS = 60; sec.CAPTCHA_TTL = 180
        os.environ.pop("ALLOWED_EMAIL_DOMAINS", None)
        self.app, self.raw = make_app()
        self.codes = []
        self._orig_new = sec.new_captcha
        def spy():
            c = self._orig_new(); self.codes.append(c); return c
        sec.new_captcha = spy
        self.c = self.app.test_client()

    def tearDown(self):
        sec.new_captcha = self._orig_new; ag.GOOGLE_CLIENT_ID = ag.GOOGLE_CLIENT_SECRET = ""; ag._google = None

    # ---- bantuan ----
    def login(self, client, username, password, captcha=None, ip=None, fetch_captcha=True):
        html = client.get("/login").get_data(as_text=True)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
        if fetch_captcha:
            client.get("/captcha.png")
        data = dict(username=username, password=password, csrf_token=csrf, captcha=self.codes[-1] if captcha is None else captcha)
        return client.post("/login", data=data, follow_redirects=True, environ_overrides={"REMOTE_ADDR": ip} if ip else None).get_data(as_text=True)

    def as_admin(self, client=None):
        c = client or self.app.test_client(); self.login(c, "admin", "admin001"); return c

    def create_user(self, admin, username="budi", pw="rahasia123", name="Budi Santoso"):
        return admin.post("/api/users", json=dict(full_name=name, username=username, password=pw))

    def users(self):
        return self.raw.execute("SELECT COUNT(*) FROM users WHERE role='Pengguna'").fetchone()[0]

    # ---- halaman & captcha ----
    def test_halaman_login_username_password_captcha_tanpa_daftar(self):
        h = self.c.get("/login").get_data(as_text=True)
        for need in ('name="username"', 'name="password"', 'name="captcha"', "/captcha.png", 'name="csrf_token"'):
            self.assertIn(need, h, need)
        for gone in ("/register", "/forgot-password", "Daftar", "Login Admin", 'type="email"', "Masuk dengan Google"):
            self.assertNotIn(gone, h, gone)

    def test_captcha_gambar_png_valid_dan_berganti_tiap_muat(self):
        r = self.c.get("/captcha.png")
        self.assertEqual(r.mimetype, "image/png"); self.assertEqual(r.data[:8], b"\x89PNG\r\n\x1a\n"); self.assertEqual(r.headers["Cache-Control"], "no-store")
        self.c.get("/captcha.png"); self.assertEqual(len(self.codes), 2); self.assertEqual(len(self.codes[0]), sec.CAPTCHA_LEN)

    def test_captcha_salah_ditolak_walau_sandi_benar(self):
        out = self.login(self.c, "admin", "admin001", captcha="SALAH")
        self.assertIn("captcha salah", out); self.assertNotIn("role=", out)

    def test_captcha_tidak_bisa_dipakai_dua_kali_atau_di_replay(self):
        c = self.app.test_client(); c.get("/login"); c.get("/captcha.png")
        cookie = c.get_cookie("session").value                   # cookie sesi berisi captcha yang akan dipakai
        csrf_c = self.app.test_client(); csrf_c.set_cookie("session", cookie)
        # pakai pertama (sandi salah, tapi captcha benar -> captcha hangus)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', csrf_c.get("/login").get_data(as_text=True)).group(1)
        cookie2 = csrf_c.get_cookie("session").value
        r1 = csrf_c.post("/login", data=dict(username="admin", password="ngawur", csrf_token=csrf, captcha=self.codes[-1]), follow_redirects=True)
        self.assertNotIn("captcha salah", r1.get_data(as_text=True))
        replay = self.app.test_client(); replay.set_cookie("session", cookie2)
        r2 = replay.post("/login", data=dict(username="admin", password="admin001", csrf_token=csrf, captcha=self.codes[-1]), follow_redirects=True)
        self.assertIn("captcha salah", r2.get_data(as_text=True))

    def test_captcha_kedaluwarsa(self):
        sec.CAPTCHA_TTL = -1
        self.assertIn("captcha salah", self.login(self.c, "admin", "admin001"))

    # ---- admin & pengguna buatan admin ----
    def test_admin_masuk_dan_password_polos_otomatis_di_hash(self):
        out = self.login(self.c, "admin", "admin001")
        self.assertIn("role=Sarana Digital", out)
        self.assertTrue(self.raw.execute("SELECT password FROM users WHERE id=1").fetchone()[0].startswith(("scrypt:", "pbkdf2:")))
        self.assertIn("role=Sarana Digital", self.login(self.app.test_client(), "admin", "admin001"))
        self.assertIn("Username atau kata sandi salah", self.login(self.app.test_client(), "admin", "ngawur"))

    def test_admin_membuat_pengguna_lalu_pengguna_bisa_masuk(self):
        admin = self.as_admin()
        r = self.create_user(admin); self.assertEqual(r.json["status"], "success")
        row = self.raw.execute("SELECT * FROM users WHERE username='budi'").fetchone()
        self.assertEqual((row["role"], row["auth_provider"], row["is_active"]), ("Pengguna", "local", 1))
        self.assertNotEqual(row["password"], "rahasia123"); self.assertTrue(row["password"].startswith(("scrypt:", "pbkdf2:")))
        self.assertIn("role=Pengguna", self.login(self.c, "budi", "rahasia123"))

    def test_ip_pengguna_tercatat_di_daftar_admin(self):
        admin = self.as_admin(); self.create_user(admin)
        self.login(self.c, "budi", "rahasia123", ip="10.103.5.77")
        users = {u["username"]: u for u in admin.get("/api/users").json}
        self.assertEqual(users["budi"]["last_ip"], "10.103.5.77"); self.assertEqual(users["budi"]["login_count"], 1)
        logs = admin.get("/api/login_logs").json
        self.assertEqual([(l["username"], l["ip_address"]) for l in logs if l["username"] == "budi"], [("budi", "10.103.5.77")])

    def test_header_xforwardedfor_palsu_tidak_dipercaya(self):
        admin = self.as_admin(); self.create_user(admin)
        c = self.app.test_client(); c.get("/login"); c.get("/captcha.png")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', c.get("/login").get_data(as_text=True)).group(1)
        c.get("/captcha.png")
        c.post("/login", data=dict(username="budi", password="rahasia123", csrf_token=csrf, captcha=self.codes[-1]),
               headers={"X-Forwarded-For": "1.2.3.4"}, environ_overrides={"REMOTE_ADDR": "10.9.9.9"})
        self.assertEqual(self.raw.execute("SELECT last_ip FROM users WHERE username='budi'").fetchone()[0], "10.9.9.9")

    def test_validasi_pembuatan_pengguna(self):
        admin = self.as_admin()
        for kw, frag in [(dict(pw="pendek1"), "minimal 8"), (dict(pw="abcdefgh"), "huruf dan angka"),
                         (dict(pw="password123"), "terlalu mudah"), (dict(username="ab"), "Username"),
                         (dict(username="admin"), "Username"), (dict(username="a b c"), "Username"), (dict(name=" "), "Nama")]:
            args = dict(username="budi", pw="rahasia123", name="Budi"); args.update(kw)
            r = self.create_user(admin, **args); self.assertEqual(r.status_code, 400, kw); self.assertIn(frag, r.json["message"])
        self.assertEqual(self.users(), 0)
        self.create_user(admin); self.assertEqual(self.create_user(admin).status_code, 409)

    def test_pengguna_biasa_tidak_bisa_kelola_akun(self):
        admin = self.as_admin(); self.create_user(admin)
        u = self.app.test_client(); self.login(u, "budi", "rahasia123")
        self.assertEqual(u.post("/api/users", json=dict(full_name="X Y", username="xyz", password="rahasia123")).status_code, 403)
        self.assertEqual(u.get("/api/users").status_code, 403)
        self.assertEqual(u.get("/ubah").status_code, 403)

    def test_reset_sandi_oleh_admin_mengeluarkan_sesi_lama(self):
        admin = self.as_admin(); self.create_user(admin)
        u = self.app.test_client(); self.login(u, "budi", "rahasia123")
        uid = self.raw.execute("SELECT id FROM users WHERE username='budi'").fetchone()[0]
        self.assertEqual(admin.put(f"/api/users/{uid}/password", json=dict(password="baruABC123")).json["status"], "success")
        self.assertEqual(admin.put(f"/api/users/{uid}/password", json=dict(password="pendek")).status_code, 400)
        self.assertEqual(admin.put("/api/users/1/password", json=dict(password="baruABC123")).status_code, 400)   # admin tak bisa
        ag.RECHECK_SECONDS = 0
        self.assertIn("masuk kembali", u.get("/", follow_redirects=True).get_data(as_text=True))
        self.assertIn("salah", self.login(self.app.test_client(), "budi", "rahasia123"))
        self.assertIn("role=Pengguna", self.login(self.app.test_client(), "budi", "baruABC123"))

    def test_hapus_dan_nonaktifkan_pengguna(self):
        admin = self.as_admin(); self.create_user(admin); uid = self.raw.execute("SELECT id FROM users WHERE username='budi'").fetchone()[0]
        admin.put(f"/api/users/{uid}/active", json=dict(is_active=False))
        self.assertIn("dinonaktifkan", self.login(self.app.test_client(), "budi", "rahasia123"))
        self.assertIn("salah", self.login(self.app.test_client(), "budi", "salah"))      # sandi salah tidak membocorkan status akun
        self.assertEqual(admin.delete(f"/api/users/{uid}").json["status"], "success"); self.assertEqual(self.users(), 0)
        self.assertEqual(admin.delete("/api/users/1").status_code, 400)

    # ---- pembatasan & CSRF ----
    def test_pembatasan_percobaan_per_ip_dan_per_akun_pengguna(self):
        admin = self.as_admin(); self.create_user(admin); ag._fails.clear()
        for _ in range(ag.MAX_EMAIL_FAILS):
            self.login(self.app.test_client(), "budi", "salah")
        self.assertIn("Terlalu banyak percobaan untuk akun ini", self.login(self.app.test_client(), "budi", "rahasia123"))
        ag._fails.clear(); ag._email_fails.clear()
        for i in range(ag.MAX_FAILS):
            self.login(self.app.test_client(), f"u{i}", "salah")
        self.assertIn("Terlalu banyak percobaan", self.login(self.app.test_client(), "admin", "admin001"))

    def test_admin_tidak_bisa_dikunci_lewat_tebakan_per_akun(self):
        for _ in range(ag.MAX_EMAIL_FAILS + 1):
            self.login(self.app.test_client(), "admin", "salah"); ag._fails.clear()
        self.assertIn("role=Sarana Digital", self.login(self.app.test_client(), "admin", "admin001"))

    def test_csrf_wajib(self):
        c = self.app.test_client(); c.get("/captcha.png")
        r = c.post("/login", data=dict(username="admin", password="admin001", captcha=self.codes[-1]), follow_redirects=True)
        self.assertIn("Formulir kedaluwarsa", r.get_data(as_text=True))

    def test_header_keamanan(self):
        r = self.c.get("/login")
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff"); self.assertEqual(r.headers["Cache-Control"], "no-store")

    # ---- Google (opsional) ----
    def google_on(self):
        ag.GOOGLE_CLIENT_ID, ag.GOOGLE_CLIENT_SECRET = "id", "secret"; ag._google = _FakeGoogle()
        _FakeGoogle.info = dict(email="budi@gmail.com", email_verified=True, sub="g-1", name="Budi G", picture="")

    def test_tombol_google_hanya_tampil_bila_dikonfigurasi(self):
        self.assertNotIn("Masuk dengan Google", self.c.get("/login").get_data(as_text=True))
        self.google_on(); self.assertIn("Masuk dengan Google", self.c.get("/login").get_data(as_text=True))

    def test_login_google_opsional_tercatat_dengan_ip(self):
        self.google_on()
        self.assertIn("role=Pengguna", self.c.get("/auth/google/callback", follow_redirects=True, environ_overrides={"REMOTE_ADDR": "10.1.1.1"}).get_data(as_text=True))
        self.assertEqual(self.raw.execute("SELECT last_ip FROM users WHERE email='budi@gmail.com'").fetchone()[0], "10.1.1.1")
        _FakeGoogle.info["email_verified"] = False
        self.assertIn("belum terverifikasi", self.app.test_client().get("/auth/google/callback", follow_redirects=True).get_data(as_text=True))


if __name__ == "__main__":
    unittest.main(verbosity=2)
