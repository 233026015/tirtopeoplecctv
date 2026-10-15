"""
Pengaman form login: token CSRF + header keamanan.
Pengguna biasa masuk HANYA lewat Google (OAuth); tidak ada form daftar / kata sandi pengguna.
"""
import hashlib
import hmac
import random
import secrets
import threading
import time

from flask import Response, current_app, request, session

CSRF_PATHS = ("/login",)                       # form login admin
NO_STORE_PATHS = ("/login", "/captcha.png", "/auth/google", "/auth/google/callback")
CAPTCHA_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # tanpa 0/O/1/I agar mudah dibaca
CAPTCHA_LEN, CAPTCHA_TTL = 5, 180
MIN_PASSWORD, MAX_PASSWORD = 8, 128
COMMON_PASSWORDS = {"password", "password1", "password123", "12345678", "123456789", "1234567890", "qwerty123",
                    "qwertyuiop", "admin12345", "admin123456", "abc12345", "katasandi", "admin001", "tirtonadi",
                    "tirtopeople"}
_used_captcha = {}                              # salt -> waktu kedaluwarsa (captcha hanya boleh dipakai SEKALI)
_cap_lock = threading.Lock()


def validate_password(pw, username=""):
    """Kembalikan pesan error (str) atau None bila sandi memenuhi syarat."""
    if len(pw) < MIN_PASSWORD:
        return f"Kata sandi minimal {MIN_PASSWORD} karakter."
    if len(pw) > MAX_PASSWORD:
        return f"Kata sandi maksimal {MAX_PASSWORD} karakter."
    if not any(c.isalpha() for c in pw) or not any(c.isdigit() for c in pw):
        return "Kata sandi harus mengandung huruf dan angka."
    low = pw.lower()
    if low in COMMON_PASSWORDS or (username and low == username.lower()):
        return "Kata sandi terlalu mudah ditebak. Gunakan kombinasi yang lebih unik."
    return None


# ---------------------------- CAPTCHA (buatan sendiri, tanpa layanan luar) ----------------------------
def _cap_hmac(salt, code):
    key = current_app.secret_key.encode("utf-8") if isinstance(current_app.secret_key, str) else current_app.secret_key
    return hmac.new(key, (salt + code).encode("utf-8"), hashlib.sha256).hexdigest()


def new_captcha():
    """Buat kode baru dan simpan hash HMAC-nya di sesi (kode asli tidak pernah dikirim ke browser sebagai teks)."""
    code = "".join(secrets.choice(CAPTCHA_ALPHABET) for _ in range(CAPTCHA_LEN))
    salt = secrets.token_hex(8)
    session["_cap"] = {"s": salt, "h": _cap_hmac(salt, code), "t": time.time()}
    return code


def captcha_ok(answer):
    """Sekali pakai + kedaluwarsa. Cookie sesi lama yang diputar ulang (replay) ditolak lewat daftar salt terpakai."""
    data = session.pop("_cap", None)
    now = time.time()
    if not data or now - data.get("t", 0) > CAPTCHA_TTL:
        return False
    with _cap_lock:
        for k in [k for k, exp in _used_captcha.items() if exp < now]:
            del _used_captcha[k]
        if data["s"] in _used_captcha:
            return False
        _used_captcha[data["s"]] = now + CAPTCHA_TTL + 60
    ans = (answer or "").strip().upper().replace(" ", "")
    return hmac.compare_digest(data["h"], _cap_hmac(data["s"], ans))


def captcha_png(code):
    import cv2
    import numpy as np
    rng = random.SystemRandom()
    w, h = 170, 56
    img = np.full((h, w, 3), 243, np.uint8)
    for _ in range(7):   # garis pengganggu terang
        c = tuple(int(rng.randint(150, 215)) for _ in range(3))
        cv2.line(img, (rng.randint(0, w), rng.randint(0, h)), (rng.randint(0, w), rng.randint(0, h)), c, rng.randint(1, 2))
    for i, ch in enumerate(code):
        tile = np.full((h, 36, 3), 255, np.uint8)
        col = (rng.randint(20, 110), rng.randint(20, 110), rng.randint(20, 110))
        cv2.putText(tile, ch, (4, 40), cv2.FONT_HERSHEY_DUPLEX, rng.uniform(1.0, 1.3), col, 2, cv2.LINE_AA)
        M = cv2.getRotationMatrix2D((18, 28), rng.uniform(-24, 24), 1.0)
        tile = cv2.warpAffine(tile, M, (36, h), borderValue=(255, 255, 255))
        x = 10 + i * 30
        region = img[:, x:x + 36]
        np.minimum(region, tile[:, :region.shape[1]], out=region)
    for _ in range(2):   # garis gelap tipis melintasi huruf
        cv2.line(img, (0, rng.randint(8, h - 8)), (w, rng.randint(8, h - 8)), (90, 90, 90), 1, cv2.LINE_AA)
    noise = np.random.default_rng(secrets.randbits(32)).integers(0, 255, (h, w), dtype=np.uint8) > 249
    img[noise] = (80, 80, 80)
    ok, buf = cv2.imencode(".png", img)
    return buf.tobytes()



def csrf_token():
    tok = session.get("_csrf")
    if not tok:
        tok = secrets.token_urlsafe(24)
        session["_csrf"] = tok
    return tok


def csrf_ok():
    sent = request.form.get("csrf_token", "")
    mine = session.get("_csrf", "")
    return bool(sent) and bool(mine) and hmac.compare_digest(sent, mine)


def init_security(app, login_error):
    @app.context_processor
    def _inject_csrf():
        return {"csrf_token": csrf_token}

    @app.route("/captcha.png")
    def captcha_image():
        return Response(captcha_png(new_captcha()), mimetype="image/png")

    @app.before_request
    def _csrf_guard():
        if request.method == "POST" and request.path in CSRF_PATHS and not csrf_ok():
            session.pop("_csrf", None)
            return login_error("Formulir kedaluwarsa. Silakan coba lagi.")
        return None

    @app.after_request
    def _security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        if request.path in NO_STORE_PATHS:
            resp.headers["Cache-Control"] = "no-store"
            resp.headers["Referrer-Policy"] = "no-referrer"
        return resp
