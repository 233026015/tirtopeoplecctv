# Login Dua Jalur: Admin & Pengguna (Google) — v12

## Alur
| Jalur | Cara login | Peran | Hak akses |
|---|---|---|---|
| Admin | Username + password (akun `admin` yang sudah ada) | Sarana Digital | Penuh (kamera CRUD, atur posisi zones, export, menu Pengguna) |
| Pengguna | Tombol "Masuk / Daftar dengan Google" | Pengguna | HANYA LIHAT: Dashboard, Live Cameras, Zones, Laporan |

- Pengguna Google yang pertama kali masuk **otomatis terdaftar dan langsung aktif**.
- Akun Google tidak bisa masuk lewat form admin, dan email admin tidak bisa dipakai login Google.
- Hak akses ditegakkan di **server** (bukan hanya menyembunyikan tombol): pengguna yang mencoba
  mengubah kamera/posisi, export Excel, atau menandai orang mendapat 403.
- Pengguna juga tidak menerima IP / username / stream path CCTV dari `/api/cameras`.

## Menu baru "Pengguna" (khusus admin)
- Daftar akun: nama, email, peran, tanggal daftar, login terakhir, jumlah login.
- Riwayat 100 login terakhir (waktu, nama, email, jalur, IP).
- Tombol Aktif/Nonaktif untuk akun Pengguna. Akun yang dinonaktifkan langsung terputus (maks. 60 detik) dan tidak bisa login lagi.

## Setup Google (sekali saja, ±10 menit)
1. Buka https://console.cloud.google.com -> buat project -> **APIs & Services -> OAuth consent screen**
   (User type: External, isi nama aplikasi & email support; Publishing status: *In production*
   supaya semua akun Google bisa masuk, bukan hanya test user).
2. **Credentials -> Create Credentials -> OAuth client ID -> Web application**.
3. **Authorized redirect URIs**: isi persis alamat callback, contoh:
   - `https://alamat-domain-anda/auth/google/callback`
   - untuk uji lokal: `http://localhost:5000/auth/google/callback`
4. Salin Client ID & Client Secret, lalu set environment variable sebelum menjalankan aplikasi:

   Windows (PowerShell):
       $env:GOOGLE_CLIENT_ID="xxxx.apps.googleusercontent.com"
       $env:GOOGLE_CLIENT_SECRET="xxxx"
       $env:SECRET_KEY="isi-teks-acak-panjang"
       python main.py

   Linux:
       export GOOGLE_CLIENT_ID=... GOOGLE_CLIENT_SECRET=... SECRET_KEY=...
       python main.py

5. `pip install -r requirements.txt` (menambah Authlib & requests).

### PENTING
- Google **menolak** redirect URI berupa IP lokal (mis. `http://10.103.x.x:5000`). Yang diterima hanya
  `http://localhost...` atau alamat **https** dengan domain. Karena pengguna akan membuka web dari
  perangkat masing-masing, server perlu domain + HTTPS (mis. lewat Nginx / reverse proxy).
  Jika memakai reverse proxy, set juga `GOOGLE_REDIRECT_URI` ke alamat https lengkapnya.
- Selama `GOOGLE_CLIENT_ID/SECRET` belum diisi, tombol Google tampil nonaktif dengan pesan
  "belum dikonfigurasi"; login admin tetap berjalan normal.

## File yang berubah
- `auth_google.py` (BARU): login Google, pencatatan login, `admin_required`, API `/api/users`, `/api/login_logs`.
- `main.py`: migrasi kolom users (email, google_sub, is_active, last_login, login_count, dll.) + tabel `login_logs`
  otomatis saat start; login admin dibatasi ke jalur lokal; `@admin_required` pada rute pengubah data.
- `templates/login.html`: pilihan jalur Pengguna / Admin.
- `templates/index.html`: tombol/menu khusus admin disembunyikan untuk Pengguna; tab "Pengguna".
- `requirements.txt`: + Authlib, requests.

## Catatan keamanan yang sebaiknya ditindaklanjuti
- Password admin masih disimpan polos di database (`admin001`) dan dibandingkan langsung — sebaiknya di-hash.
- Password RTSP kamera juga tersimpan polos di tabel `cameras`.

## v13 — Daftar/masuk email untuk pengguna, "Login Admin" di bawah tombol Google
- Halaman login: tombol **Masuk dengan Google**, tepat di bawahnya tautan **Login Admin** (membuka form admin; ada tombol kembali),
  lalu bagian "atau dengan email" dengan tab **Masuk** / **Daftar**. Tab Pengguna/Admin yang lama dihapus.
- Daftar pengguna biasa: nama, email, kata sandi (min. 8 karakter, disimpan **ter-hash**), langsung aktif dengan peran Pengguna (hanya lihat).
  Route baru: `POST /register`, `POST /login/user`. Batas 8 percobaan gagal / 10 menit per IP.
- Akun email tidak bisa masuk lewat Login Admin; akun Google tidak bisa masuk dengan kata sandi; email admin tidak bisa didaftarkan.
- Google: penyebab "tidak bisa masuk" = GOOGLE_CLIENT_ID/SECRET belum di-set di server (tombol dulu mati tanpa penjelasan).
  Sekarang tombol memberi pesan jelas, error callback dicatat di log server (redirect_uri_mismatch, mismatching_state, dll.),
  dan ada `TRUST_PROXY=1` (ProxyFix) bila di belakang Nginx/HTTPS. Setup tetap seperti bagian "Setup Google" di atas.
- Tes: `python test_login_auth.py` (11 tes, memakai SQLite sebagai pengganti MySQL).

## v13.1 — perbaikan
- Perbaiki `NameError: prefill` di /login (halaman login 500). Tes kini menjalankan fungsi `login_page` asli dari main.py.
- Perbaiki pesan `[DETECTION][ERROR] unsupported format string passed to NoneType`: nilai stream_fps awal None saat memformat peringatan FPS.

## v16 — Login username + kata sandi + CAPTCHA (akun dibuat admin), IP pengguna tercatat
- **Halaman login:** username, kata sandi, dan captcha (gambar kode 5 karakter, bisa diganti dengan tombol refresh). Tombol Google hanya
  muncul bila `GOOGLE_CLIENT_ID/SECRET` diisi (opsional; tidak perlu untuk memakai web ini). Tidak ada pendaftaran sendiri.
- **Akun pengguna dibuat oleh admin:** menu **Pengguna** -> kartu *Tambah Pengguna* (nama, username, kata sandi). Admin juga bisa
  **Reset sandi**, **Nonaktifkan/Aktifkan**, dan **Hapus**. Reset sandi / nonaktif membuat pengguna yang sedang login otomatis keluar (maks. 60 dtk).
  Dengan begini tidak ada orang asing yang bisa mengisi data ke admin (tidak terkesan phishing).
- **IP pengguna:** setiap login mencatat IP. Tabel Akun menampilkan *IP Terakhir*, tabel Riwayat Login menampilkan IP tiap login.
  IP diambil dari koneksi langsung (header X-Forwarded-For dari pengunjung TIDAK dipercaya karena bisa dipalsukan).
  Di belakang Nginx/reverse proxy WAJIB set `TRUST_PROXY=1` agar yang tercatat IP pengguna, bukan IP proxy.
- **Captcha:** dibuat sendiri oleh server (tanpa layanan luar), kunci jawaban di sesi berupa HMAC bertanda tangan server, sekali pakai,
  kedaluwarsa 3 menit, cookie lama yang diputar ulang ditolak. Captcha salah juga dihitung sebagai percobaan gagal.
- **Syarat sandi pengguna:** 8-128 karakter, huruf + angka, bukan sandi umum / sama dengan username.
- **Pembatasan:** per IP (8x/10 mnt) dan per akun pengguna (5x/10 mnt). Akun admin tidak dikunci per-akun (agar tidak bisa dikunci orang lain), tetap kena batas per IP.
- Tetap: CSRF, hash sandi, SECRET_KEY acak, cookie aman, `ADMIN_PASSWORD`, `COOKIE_SECURE=1`, `ALLOWED_EMAIL_DOMAINS` (khusus Google opsional).
- Akun lama hasil daftar email tetap bisa masuk memakai email sebagai username.
- File: `auth_security.py` (captcha, CSRF, header, aturan sandi), `auth_google.py` (login lokal, API kelola pengguna, IP), `main.py`,
  `templates/login.html`, `templates/index.html` (tab Pengguna). Tes: `python test_login_auth.py` (19 tes).
- Lupa kata sandi pengguna: hubungi admin (Reset sandi di menu Pengguna).
