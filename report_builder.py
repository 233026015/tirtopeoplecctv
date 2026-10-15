"""
Pembuat LAPORAN PEOPLE COUNTING - TERMINAL TIPE A TIRTONADI (XLSX).

Prinsip:  DATA DETAIL  ->  REKAP  ->  DASHBOARD
  * Satu-satunya data "mentah" ada di sheet DATA DETAIL (Excel Table + filter).
  * Semua angka rekap / dashboard / analisis / validasi adalah RUMUS yang membaca
    DATA DETAIL, jadi kalau isi DATA DETAIL diubah, angka lain ikut berubah.
  * Tidak ada data dikarang: kolom yang tidak ada di database ditulis
    "Data tidak tersedia"; foto yang file-nya tidak ada di server TIDAK diberi hyperlink.

Dipanggil dari main.py (tombol Export XLSX di web & laporan otomatis).
"""
import os
import re
from datetime import datetime, date, time
from urllib.parse import quote

import pandas as pd
from openpyxl import Workbook
from openpyxl.chart import BarChart, LineChart, Reference
from openpyxl.chart.title import Title
from openpyxl.chart.text import Text
from openpyxl.chart.data_source import StrRef, StrData, StrVal
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter as L, column_index_from_string
from openpyxl.formatting.rule import FormulaRule, ColorScaleRule
from openpyxl.worksheet.datavalidation import DataValidation
from openpyxl.worksheet.hyperlink import Hyperlink
from openpyxl.worksheet.table import Table, TableStyleInfo

# ------------------------------------------------------------------ konstanta
S_DASH, S_DET, S_REK, S_CAM = "DASHBOARD", "DATA DETAIL", "REKAP", "REKAP PER KAMERA"
S_ANA, S_FLT, S_VAL, S_REF = "ANALISIS", "FILTER KAMERA", "VALIDASI", "REFERENSI"
S_AUD = "AUDIT KAMERA"
RESERVED = {s.lower() for s in (S_DASH, S_DET, S_REK, S_CAM, S_ANA, S_FLT, S_VAL, S_REF, S_AUD)}

NA = "Data tidak tersedia"
NO_PHOTO = "File foto tidak tersedia"
ALL_CAM = "Semua Kamera"
TITLE1 = "LAPORAN PEOPLE COUNTING"
TITLE2 = "TERMINAL TIPE A TIRTONADI"
BULAN = ["", "Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli",
         "Agustus", "September", "Oktober", "November", "Desember"]

HDR, FIRST = 6, 7            # baris header & baris data pertama di DATA DETAIL
EXTRA_ROWS = 1000            # rumus rekap membaca sampai baris terakhir + 1000 (data baru yang ditambahkan)
HEAVY_LIMIT = 8000           # di atas ini, kolom pemeriksaan O(n^2) ditulis sebagai nilai (bukan rumus)

FN = "Arial"
NAVY = "1F3A5F"
F_BASE = Font(name=FN, size=10)
F_BOLD = Font(name=FN, size=10, bold=True)
F_HEAD = Font(name=FN, size=10, bold=True, color="FFFFFF")
F_T1 = Font(name=FN, size=15, bold=True, color=NAVY)
F_T2 = Font(name=FN, size=12, bold=True, color=NAVY)
F_SUB = Font(name=FN, size=10, italic=True, color="595959")
F_NOTE = Font(name=FN, size=9, italic=True, color="595959")
F_LINK = Font(name=FN, size=10, color="0563C1", underline="single")
F_SEC = Font(name=FN, size=11, bold=True, color=NAVY)
F_INPUT = Font(name=FN, size=10, bold=True, color="0000FF")
FILL_HEAD = PatternFill("solid", fgColor=NAVY)
FILL_TOT = PatternFill("solid", fgColor="E7ECF3")
FILL_INPUT = PatternFill("solid", fgColor="FFF9DB")
THIN = Side(style="thin", color="BFBFBF")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
CENTER = Alignment(horizontal="center", vertical="center", wrap_text=True)
LEFT = Alignment(horizontal="left", vertical="center", wrap_text=False)
WRAP = Alignment(horizontal="left", vertical="top", wrap_text=True)
FMT_DATE, FMT_TIME, FMT_TS = "dd/mm/yyyy", "hh:mm:ss", "dd/mm/yyyy hh:mm:ss"
FMT_HOUR = '00":00"'
FMT_PCT = "0.0%"


def q(name):
    """Nama sheet untuk dipakai di rumus (selalu diberi kutip)."""
    return "'" + str(name).replace("'", "''") + "'"


def fmt_tanggal_id(d):
    return f"{d.day:02d} {BULAN[d.month]} {d.year}"


def safe_sheet_name(name, used):
    base = re.sub(r"[\[\]:*?/\\]", "-", str(name or "")).strip().strip("'") or "Kamera"
    base = base[:31]
    cand, n = base, 2
    while cand.lower() in used:
        suf = f" ({n})"
        cand = base[:31 - len(suf)] + suf
        n += 1
    used.add(cand.lower())
    return cand


def excel_serial(dt):
    return (dt - datetime(1899, 12, 30)).total_seconds() / 86400.0


# ------------------------------------------------------------------ helper tampilan
def style_range(ws, ref, font=None, fill=None, align=None, border=None, fmt=None):
    for row in ws[ref]:
        for c in (row if isinstance(row, tuple) else (row,)):
            if font:
                c.font = font
            if fill:
                c.fill = fill
            if align:
                c.alignment = align
            if border:
                c.border = border
            if fmt:
                c.number_format = fmt


def head_row(ws, row, col1, titles, height=30):
    for i, t in enumerate(titles):
        c = ws.cell(row=row, column=col1 + i, value=t)
        c.font, c.fill, c.alignment, c.border = F_HEAD, FILL_HEAD, CENTER, BOX
    ws.row_dimensions[row].height = height


def title_block(ws, ncols, periode, sub=None):
    ws.sheet_view.showGridLines = False
    for r, (txt, f) in enumerate(((TITLE1, F_T1), (TITLE2, F_T2), (periode, F_SUB)), start=1):
        c = ws.cell(row=r, column=1, value=txt)
        c.font, c.alignment = f, Alignment(horizontal="left", vertical="center")
    if sub:
        c = ws.cell(row=4, column=1, value=sub)
        c.font = F_BOLD
    for r in range(1, 4):
        ws.row_dimensions[r].height = 22 if r < 3 else 16
    # garis tipis di bawah judul (identitas laporan resmi)
    for col in range(1, ncols + 1):
        ws.cell(row=4, column=col).border = Border(top=Side(style="medium", color=NAVY))


def section(ws, row, text, ncols=7, col=1):
    c = ws.cell(row=row, column=col, value=text)
    c.font = F_SEC
    for k in range(col, col + ncols):
        ws.cell(row=row, column=k).border = Border(bottom=Side(style="thin", color=NAVY))


def page_setup(ws, landscape=True, title_rows=None):
    ws.page_setup.orientation = "landscape" if landscape else "portrait"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    ws.page_margins.left = ws.page_margins.right = 0.4
    ws.page_margins.top = ws.page_margins.bottom = 0.5
    ws.print_options.horizontalCentered = True
    ws.oddFooter.left.text = "Laporan People Counting - Terminal Tipe A Tirtonadi"
    ws.oddFooter.right.text = "Halaman &P dari &N"
    if title_rows:
        ws.print_title_rows = title_rows


RED = Font(name=FN, bold=True, color="B71C1C")
GREEN = Font(name=FN, bold=True, color="2E7D32")


def flag_colors(ws, ref, bad_text, good_text=None):
    """Beri warna merah pada sel berisi teks `bad_text` (dan hijau untuk `good_text`)."""
    first = ref.split(":")[0].replace("$", "")
    ws.conditional_formatting.add(ref, FormulaRule(formula=[f'ISNUMBER(SEARCH("{bad_text}",{first}))'], font=RED))
    if good_text:
        ws.conditional_formatting.add(ref, FormulaRule(formula=[f'ISNUMBER(SEARCH("{good_text}",{first}))'], font=GREEN))


def widths(ws, ws_widths):
    for i, w in enumerate(ws_widths, start=1):
        ws.column_dimensions[L(i)].width = w


def put(ws, ref, value, font=F_BASE, fmt=None, align=None, border=None, fill=None):
    c = ws[ref]
    c.value = value
    c.font = font
    if fmt:
        c.number_format = fmt
    if align:
        c.alignment = align
    if border:
        c.border = border
    if fill:
        c.fill = fill
    return c


# ------------------------------------------------------------------ persiapan data
def _clean(v):
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    if hasattr(v, "item"):
        v = v.item()
    if isinstance(v, str) and v.strip() == "":
        return None
    return v


def _prepare(logs_df, cameras_df, snapshot_dir, base_url):
    cols = ["id", "timestamp", "person_id", "status", "total_in", "total_out", "camera_id", "snapshot_path"]
    logs = logs_df.reindex(columns=cols).copy()
    logs["camera_id"] = pd.to_numeric(logs["camera_id"], errors="coerce").astype("Int64")
    logs = logs.sort_values("id", kind="stable")

    # --- daftar kamera: semua kamera di DB + kamera terhapus yang masih punya log + log tanpa kamera
    cams, known, used_labels = [], set(), set()

    def label_for(name, cid):
        lab = name
        if lab.lower() in used_labels:
            lab = f"{name} #{cid}"
        used_labels.add(lab.lower())
        return lab

    for _, r in cameras_df.sort_values("id").iterrows():
        cid = int(r["id"])
        known.add(cid)
        nm = _clean(r.get("name")) or f"Kamera {cid}"
        cams.append({"cid": cid, "name": label_for(str(nm), cid), "loc": _clean(r.get("location")) or NA})
    for cid in sorted(int(x) for x in logs["camera_id"].dropna().unique() if int(x) not in known):
        cams.append({"cid": cid, "name": label_for(f"Kamera #{cid} (dihapus)", cid), "loc": NA})
    if logs["camera_id"].isna().any():
        cams.append({"cid": None, "name": NA, "loc": NA})
        used_labels.add(NA.lower())
    by_cid = {c["cid"]: c for c in cams}

    def photo_info(snap):
        """-> (status_foto, teks_file, path_atau_url, catatan). TIDAK mengarang file."""
        if not snap:
            return "Tidak tersedia", NO_PHOTO, NA
        fpath = os.path.join(snapshot_dir, snap) if snapshot_dir else None
        if fpath and os.path.isfile(fpath):
            if base_url:
                link = base_url.rstrip("/") + "/static/snapshots/" + quote(snap)
            else:
                link = os.path.abspath(fpath)
            return "Tersedia", snap, link
        return "Tidak tersedia", NO_PHOTO, f"{NA} (nama file tercatat: {snap}, file tidak ada di server)"

    recs = []
    for no, (_, r) in enumerate(logs.iterrows(), start=1):
        ts = _clean(r["timestamp"])
        ts = pd.to_datetime(ts, errors="coerce") if ts is not None else None
        ts = None if (ts is None or pd.isna(ts)) else ts.to_pydatetime()
        cid = None if pd.isna(r["camera_id"]) else int(r["camera_id"])
        cam = by_cid.get(cid) or by_cid.get(None) or {"name": NA, "loc": NA}
        pid = _clean(r["person_id"])
        st = _clean(r["status"])
        stf, ftxt, flink = photo_info(_clean(r["snapshot_path"]))
        recs.append({
            "no": no, "id": int(r["id"]), "pid": int(pid) if pid is not None else None,
            "cam": cam["name"], "loc": cam["loc"], "cid": cid, "ts": ts,
            "status": st, "tin": _clean(r["total_in"]), "tout": _clean(r["total_out"]),
            "stf": stf, "ftxt": ftxt, "flink": flink,
        })
    return recs, cams


# ================================================================== WORKBOOK
def compute_camera_stats(recs, cams):
    """Statistik per kamera, dihitung EKSPLISIT di Python dari data sumber (bukan dari rumus Excel).

    Event valid = punya timestamp (sama dengan dasar COUNTIFS jam di Excel); status selain MASUK/KELUAR dan event tanpa timestamp ditandai PERLU VALIDASI di AUDIT KAMERA & VALIDASI. Jam = timestamp lokal (jam dinding server, apa adanya di DB).
    Jam ramai  = jam dengan event terbanyak milik kamera itu; seri -> jam PALING AWAL.
    Jam sepi   = jam dengan event paling sedikit di antara jam yang punya event (>0); seri -> jam PALING AWAL.
    Jam parsial (jam event pertama/terakhir) TIDAK dikeluarkan; ditandai di kolom info agar bisa dinilai manual.
    """
    out = {}
    for c in cams:
        nm = c["name"]
        sub = [r for r in recs if r["cam"] == nm]
        valid = [r for r in sub if r["ts"]]
        hourly = [0] * 24
        for r in valid:
            hourly[r["ts"].hour] += 1
        active = [(h, v) for h, v in enumerate(hourly) if v > 0]
        ramai = sepi = None
        if active:
            mx = max(v for _, v in active)
            mn = min(v for _, v in active)
            ramai = (min(h for h, v in active if v == mx), mx)
            sepi = (min(h for h, v in active if v == mn), mn)
        tss = [r["ts"] for r in valid]
        out[nm] = {
            "cid": c["cid"], "events_total": len(sub), "valid": len(valid), "hourly": hourly,
            "ramai": ramai, "sepi": sepi,
            # Masuk/Keluar = semua event berstatus tsb (sama dengan REKAP PER KAMERA); event tanpa timestamp
            # tetap terhitung di sini tetapi tidak masuk hitungan per jam -> terlihat sebagai PERLU VALIDASI di AUDIT KAMERA
            "masuk": sum(1 for r in sub if r["status"] == "MASUK"),
            "keluar": sum(1 for r in sub if r["status"] == "KELUAR"),
            "first": min(tss) if tss else None, "last": max(tss) if tss else None,
            "active_hours": len(active),
            "ties_ramai": sum(1 for h, v in active if ramai and v == ramai[1]),
            "ties_sepi": sum(1 for h, v in active if sepi and v == sepi[1]),
        }
    return out


def build_report_workbook(target, logs_df, cameras_df, start_date=None, end_date=None,
                          snapshot_dir=None, base_url=None, default_camera=None):
    recs, cams = _prepare(logs_df, cameras_df, snapshot_dir, base_url)
    n = len(recs)
    now = datetime.now()
    heavy_ok = n <= HEAVY_LIMIT
    LAST = max(FIRST, HDR + n)
    EXT = LAST + EXTRA_ROWS

    if start_date is None or end_date is None:
        tss = [r["ts"] for r in recs if r["ts"]]
        start_date = start_date or (min(tss).date() if tss else now.date())
        end_date = end_date or (max(tss).date() if tss else now.date())
    periode = f"Periode: {fmt_tanggal_id(start_date)} – {fmt_tanggal_id(end_date)}"

    cam_names = [c["name"] for c in cams]
    ncam = len(cams)
    stats = compute_camera_stats(recs, cams)

    def R(col):
        return f"{q(S_DET)}!${col}${FIRST}:${col}${EXT}"

    ID, KAM, TGL, JAM, STAT = R("B"), R("D"), R("F"), R("I"), R("J")
    TOT, FOTO, ISSUE, VALID, KEY = R("K"), R("N"), R("Q"), R("R"), R("T")

    wb = Workbook()
    ws_dash = wb.active
    ws_dash.title = S_DASH
    ws_det = wb.create_sheet(S_DET)
    ws_rek = wb.create_sheet(S_REK)
    ws_cam = wb.create_sheet(S_CAM)
    ws_ana = wb.create_sheet(S_ANA)
    ws_aud = wb.create_sheet(S_AUD)
    used = set(RESERVED)
    cam_sheet = {}
    for c in cams:
        cam_sheet[c["name"]] = safe_sheet_name(c["name"], used)
    cam_ws = {nm: wb.create_sheet(sn) for nm, sn in cam_sheet.items()}
    ws_flt = wb.create_sheet(S_FLT)
    ws_val = wb.create_sheet(S_VAL)
    ws_ref = wb.create_sheet(S_REF)
    for ws, color in ((ws_dash, NAVY), (ws_det, "2E7D32"), (ws_rek, "7F7F7F"), (ws_cam, "7F7F7F"),
                      (ws_ana, "7F7F7F"), (ws_aud, "6A1B9A"), (ws_flt, "C77700"), (ws_val, "B71C1C"), (ws_ref, "BFBFBF")):
        ws.sheet_properties.tabColor = color
    for ws in cam_ws.values():
        ws.sheet_properties.tabColor = "2E7D32"

    # ============================================================ REFERENSI (daftar pilihan + definisi)
    title_block(ws_ref, 6, periode, "REFERENSI, DEFINISI & CATATAN")
    section(ws_ref, 5, "Daftar pilihan (dipakai dropdown)", 3)
    put(ws_ref, "A6", "Kamera", F_BOLD)
    put(ws_ref, "B6", "Status", F_BOLD)
    put(ws_ref, "A7", ALL_CAM)
    for i, nm in enumerate(cam_names):
        put(ws_ref, f"A{8 + i}", nm)
    cam_list_ref = f"{q(S_REF)}!$A$8:$A${7 + max(ncam, 1)}"
    SPARE = 10
    cam_list_all = f"{q(S_REF)}!$H$7:$H${7 + ncam + SPARE}"      # daftar dropdown filter: Semua Kamera + kamera + slot kosong untuk kamera baru
    put(ws_ref, "H6", "Daftar kamera untuk dropdown filter", F_BOLD)
    put(ws_ref, "H7", ALL_CAM)
    for i, nm in enumerate(cam_names):
        put(ws_ref, f"H{8 + i}", nm)
    for i in range(SPARE):
        put(ws_ref, f"H{8 + ncam + i}", None, F_INPUT, fill=FILL_INPUT, border=BOX)
    put(ws_ref, f"H{8 + ncam + SPARE}", "↑ Kamera baru di DATA DETAIL: ketik namanya persis di sel kuning, lalu muncul di dropdown.", F_NOTE)
    guide = ["PETUNJUK FILTER & DIAGRAM",
             "1. DASHBOARD sel C7: pilih kamera (atau Semua Kamera). F7 status, C8/F8 rentang tanggal, C9/F9 rentang jam. Empat diagram batang & judulnya mengikuti.",
             "2. REKAP bagian VI (kolom J:O): filter sendiri (K6 kamera, K7 status, K8/M8 tanggal, K9/M9 jam); tabel 24 jam & diagram jam mengikuti.",
             "3. Data baru: ekspor ulang dari sistem, atau tempel baris baru tepat di bawah DATA DETAIL (isi ID, ID Orang, Kamera, Timestamp, Status, Total Orang); Tanggal/Waktu/Jam terisi otomatis (±1000 baris).",
             "4. Workbook BUKAN real-time: tidak terhubung ke CCTV/database; data baru masuk lewat ekspor/tempel lalu hitung ulang (F9) otomatis."]
    for i, t_ in enumerate(guide):
        put(ws_ref, f"H{10 + ncam + SPARE + i}", t_, F_BOLD if i == 0 else F_NOTE)
    ws_ref.column_dimensions["H"].width = 34
    for i, s in enumerate(["Semua", "MASUK", "KELUAR"]):
        put(ws_ref, f"B{7 + i}", s)
    stat_list_ref = f"{q(S_REF)}!$B$7:$B$9"
    r0 = 8 + max(ncam, 1) + 2
    section(ws_ref, r0, "Definisi kolom & istilah", 6)
    defs = [
        ("ID Event", "Kolom `id` pada tabel visitor_logs di database sistem (ID asli, tidak diganti). Dipakai sebagai kunci penelusuran."),
        ("ID Orang (Tracking ID)", "Kolom `person_id` = nomor ID pelacak (tracker) YOLO saat orang melewati garis. Bukan identitas orang."),
        ("Tanggal / Waktu / Timestamp", "Diambil dari kolom `timestamp` database; disimpan sebagai tanggal-jam Excel (bukan teks) sehingga bisa difilter."),
        ("Status", "Persis seperti di database (MASUK / KELUAR). Status lain, bila ada, dipertahankan dan ditandai PERLU VALIDASI."),
        ("Total Event", "Jumlah baris kejadian counting (1 baris = 1 perlintasan garis)."),
        ("Total Orang", "Jumlah kolom `Total Orang`. Database hanya mencatat 1 orang per perlintasan, sehingga nilainya 1 per event dan Total Orang = Total Event. Bila kelak sistem mencatat >1 orang per event, kolom ini sudah terpisah."),
        ("Total Masuk / Total Keluar", "Jumlah EVENT berstatus MASUK / KELUAR."),
        ("Total Masuk/Keluar Kumulatif", "Kolom `total_in` / `total_out` dari database: nilai penghitung berjalan kamera saat event terjadi (apa adanya)."),
        ("Event Tertinggi", "Jumlah event terbanyak yang terjadi dalam SATU jam pada kamera tersebut."),
        ("Aturan seri (imbang)", "Jika beberapa jam sama-sama tertinggi/terendah, dipilih jam paling awal (konsisten untuk semua kamera)."),
        ("Event vs ID Tracking Unik vs Kumulatif", "Event = tiap perlintasan garis. ID Tracking Unik = banyaknya nomor pelacak berbeda pada kamera itu (bukan orang unik sebenarnya). Kumulatif = penghitung berjalan kamera (total_in/total_out). Ketiganya berbeda makna dan tidak boleh dipertukarkan."),
        ("Jam Tersibuk / Tersepi", "Jam dengan event terbanyak / tersedikit. Jam tersepi hanya dihitung di antara jam yang memiliki event (jam kosong tidak dianggap)."),
        ("File Foto", "Nama file snapshot di server (static/snapshots). Hyperlink hanya dibuat jika file benar-benar ada saat laporan dibuat. Jika tidak: \"File foto tidak tersedia\"."),
        ("Status Validasi", "VALID bila lolos semua pemeriksaan di kolom Keterangan (rumus). PERLU VALIDASI bila ada temuan. Data bermasalah TIDAK dihapus."),
    ]
    for i, (a, b) in enumerate(defs):
        rr = r0 + 1 + i
        put(ws_ref, f"A{rr}", a, F_BOLD, align=Alignment(vertical="top", wrap_text=True))
        ws_ref.merge_cells(start_row=rr, start_column=2, end_row=rr, end_column=6)
        put(ws_ref, f"B{rr}", b, align=WRAP)
        ws_ref.row_dimensions[rr].height = 15 * (1 + len(b) // 95)
    r1 = r0 + len(defs) + 3
    section(ws_ref, r1, "Catatan penting & batasan", 6)
    notes = [
        f"Laporan dibuat {now:%d/%m/%Y %H:%M:%S} dari tabel visitor_logs (periode {fmt_tanggal_id(start_date)} – {fmt_tanggal_id(end_date)}); {n} baris event diekspor.",
        "Akurasi deteksi/penghitungan AI TIDAK diklaim: belum ada data pembanding (hitung manual/ground truth). Angka adalah hasil sistem apa adanya.",
        "Foto snapshot di server otomatis dihapus setelah SNAPSHOT_MAX_AGE_HOURS (default 24 jam). Event yang lebih lama akan tertulis \"File foto tidak tersedia\". Naikkan nilai ini bila foto perlu tersimpan lebih lama.",
        "Hyperlink foto berupa alamat web server sistem (mis. http://<alamat-server>:5000/static/snapshots/...). Foto hanya terbuka bila komputer pembuka terhubung ke jaringan server.",
        "Rekap tanggal×kamera memuat tanggal yang ADA datanya saat laporan dibuat; hari tanpa data tidak dipalsukan menjadi 0. Unduh ulang laporan untuk memasukkan data terbaru.",
        "Slicer Excel tidak tersedia pada file hasil program; sebagai gantinya: filter Tabel di DATA DETAIL, sheet FILTER KAMERA, dan kotak filter interaktif (kamera/status/tanggal/jam) di DASHBOARD.",
        "Logo/identitas instansi tidak ditemukan pada proyek sehingga tidak ditambahkan; sisipkan logo di sel kosong kanan-atas DASHBOARD bila diperlukan.",
    ]
    for i, t in enumerate(notes):
        rr = r1 + 1 + i
        ws_ref.merge_cells(start_row=rr, start_column=1, end_row=rr, end_column=6)
        put(ws_ref, f"A{rr}", f"{i + 1}. {t}", align=WRAP)
        ws_ref.row_dimensions[rr].height = 15 * (1 + len(t) // 115)
    widths(ws_ref, [30, 24, 20, 20, 20, 20])
    page_setup(ws_ref, landscape=False)

    # ============================================================ DATA DETAIL
    title_block(ws_det, 21, periode, None)
    put(ws_det, "A4", "DATA DETAIL — sumber utama seluruh rekap, analisis & dashboard. Gunakan tombol filter ▼ pada judul kolom (Kamera, Status, Tanggal, Waktu/Jam, ID, Status Foto).", F_NOTE)
    put(ws_det, "A5", "Kolom berlatar abu pada rumus (Jam, Keterangan, Status Validasi, Key Kamera) dihitung otomatis; jangan dihapus.", F_NOTE)
    ws_det.sheet_view.showGridLines = True
    det_cols = ["No", "ID Event", "ID Orang (Tracking ID)", "Kamera", "Lokasi Kamera", "Tanggal", "Waktu",
                "Timestamp", "Jam", "Status", "Total Orang", "Total Masuk Kumulatif", "Total Keluar Kumulatif",
                "Status Foto", "File Foto", "Path / URL Foto", "Keterangan", "Status Validasi", "Camera ID",
                "Key Kamera", "Penanda ID Tracking Unik"]
    head_row(ws_det, HDR, 1, det_cols, 42)

    # duplikat ID (dipakai bila mode rumus berat dimatikan)
    seen_pid, uniq_flag = set(), {}
    for r_ in recs:
        k_ = (r_["cam"], r_["pid"])
        uniq_flag[r_["no"]] = 0 if (r_["pid"] is None or k_ in seen_pid) else 1
        seen_pid.add(k_)
    id_counts = {}
    for r in recs:
        id_counts[r["id"]] = id_counts.get(r["id"], 0) + 1
    gen_serial = excel_serial(now) + 1.0 / 24
    cam_run = {}

    for i, rec in enumerate(recs):
        r = FIRST + i
        ws_det.cell(r, 1, rec["no"])
        ws_det.cell(r, 2, rec["id"])
        ws_det.cell(r, 3, rec["pid"] if rec["pid"] is not None else None)
        ws_det.cell(r, 4, rec["cam"])
        ws_det.cell(r, 5, rec["loc"])
        if rec["ts"]:
            ws_det.cell(r, 6, datetime.combine(rec["ts"].date(), time(0, 0))).number_format = FMT_DATE
            ws_det.cell(r, 7, rec["ts"].time().replace(microsecond=0)).number_format = FMT_TIME
            ws_det.cell(r, 8, rec["ts"].replace(microsecond=0)).number_format = FMT_TS
        ws_det.cell(r, 9, f'=IF(H{r}="","",HOUR(H{r}))')
        ws_det.cell(r, 10, rec["status"])
        ws_det.cell(r, 11, 1)
        ws_det.cell(r, 12, rec["tin"])
        ws_det.cell(r, 13, rec["tout"])
        ws_det.cell(r, 14, rec["stf"])
        c_f = ws_det.cell(r, 15, rec["ftxt"])
        if rec["stf"] == "Tersedia":
            c_f.hyperlink = rec["flink"]
            c_f.font = F_LINK
        ws_det.cell(r, 16, rec["flink"])
        dup_expr = (f'IF(COUNTIF($B${FIRST}:$B${LAST},B{r})>1,"ID duplikat; ","")' if heavy_ok
                    else ('"ID duplikat; "' if id_counts[rec["id"]] > 1 else '""'))
        issue = (
            f'=TRIM(IF(H{r}="","Timestamp kosong; ","")'
            f'&IF(OR(D{r}="",D{r}="{NA}"),"Kamera kosong; ","")'
            f'&IF(J{r}="","Status kosong; ","")'
            f'&IF(AND(J{r}<>"",J{r}<>"MASUK",J{r}<>"KELUAR"),"Status di luar MASUK/KELUAR; ","")'
            f'&{dup_expr}'
            f'&IF(C{r}="","ID orang kosong; ","")'
            f'&IF(N{r}<>"Tersedia","Foto tidak tersedia; ","")'
            f'&IF(AND(H{r}<>"",H{r}>{gen_serial:.6f}),"Timestamp di masa depan; ","")'
            f'&IF(OR(NOT(ISNUMBER(L{r})),NOT(ISNUMBER(M{r}))),"Total kumulatif tidak valid; ",'
            f'IF(OR(L{r}<0,M{r}<0),"Total kumulatif negatif; ","")))'
        )
        ws_det.cell(r, 17, issue)
        ws_det.cell(r, 18, f'=IF(Q{r}="","Valid","PERLU VALIDASI")')
        ws_det.cell(r, 19, rec["cid"])
        k = cam_run.get(rec["cam"], 0) + 1
        cam_run[rec["cam"]] = k
        ws_det.cell(r, 20, f'=D{r}&"|"&COUNTIF($D${FIRST}:D{r},D{r})' if heavy_ok else f'{rec["cam"]}|{k}')
        ws_det.cell(r, 21, (f'=IF(OR(C{r}="",D{r}=""),0,IF(COUNTIFS($D${FIRST}:D{r},D{r},$C${FIRST}:C{r},C{r})=1,1,0))'
                            if heavy_ok else uniq_flag[rec["no"]]))
    if n == 0:
        ws_det.cell(FIRST, 2, None)
        put(ws_det, f"D{FIRST}", NA, F_NOTE)

    for r in range(FIRST, LAST + 1):
        for cidx in range(1, 22):
            c = ws_det.cell(r, cidx)
            if c.font != F_LINK:
                c.font = F_BASE
            c.border = BOX
        for cidx in (1, 2, 3, 6, 7, 8, 9, 10, 11, 12, 13, 14, 18, 19, 21):
            ws_det.cell(r, cidx).alignment = Alignment(horizontal="center")
    for r in range(LAST + 1, EXT + 1):   # baris cadangan: tanggal/waktu/jam dihitung otomatis dari Timestamp (kolom H) untuk data baru
        ws_det.cell(r, 6, f'=IF(H{r}="","",INT(H{r}))').number_format = FMT_DATE
        ws_det.cell(r, 7, f'=IF(H{r}="","",MOD(H{r},1))').number_format = FMT_TIME
        ws_det.cell(r, 9, f'=IF(H{r}="","",HOUR(H{r}))')
    tab = Table(displayName="tblDetail", ref=f"A{HDR}:U{LAST}")
    tab.tableStyleInfo = TableStyleInfo(name="TableStyleLight1", showRowStripes=True)
    ws_det.add_table(tab)
    widths(ws_det, [6, 10, 13, 26, 24, 12, 10, 19, 6, 10, 9, 12, 12, 12, 30, 30, 38, 16, 9, 26, 14])
    ws_det.freeze_panes = f"E{FIRST}"
    page_setup(ws_det, title_rows=f"{HDR}:{HDR}")
    ws_det.print_area = f"A1:R{LAST}"
    flag_colors(ws_det, f"R{FIRST}:R{LAST}", "PERLU VALIDASI")

    # ============================================================ REKAP
    title_block(ws_rek, 7, periode, "REKAP KESELURUHAN (otomatis dari DATA DETAIL)")
    section(ws_rek, 5, "I. RINGKASAN", 7)
    summ = [
        ("Total Masuk", f'=COUNTIF({STAT},"MASUK")', "Jumlah event berstatus MASUK", "0"),
        ("Total Keluar", f'=COUNTIF({STAT},"KELUAR")', "Jumlah event berstatus KELUAR", "0"),
        ("Total Event", f"=COUNTA({ID})", "Jumlah seluruh baris event pada DATA DETAIL", "0"),
        ("Total Orang", f"=SUM({TOT})", "Jumlah kolom Total Orang", "0"),
        ("Event status lain / kosong", "=C8-C6-C7", "Event selain MASUK/KELUAR (tidak masuk kolom Masuk/Keluar)", "0"),
        ("Tanggal awal data", f'=IF(COUNT({TGL})=0,"{NA}",MIN({TGL}))', "Tanggal event paling awal", FMT_DATE),
        ("Tanggal akhir data", f'=IF(COUNT({TGL})=0,"{NA}",MAX({TGL}))', "Tanggal event paling akhir", FMT_DATE),
    ]
    for i, (lab, f, note, nf) in enumerate(summ):
        rr = 6 + i
        put(ws_rek, f"B{rr}", lab, F_BOLD, border=BOX)
        put(ws_rek, f"C{rr}", f, F_BOLD, nf, Alignment(horizontal="right"), BOX)
        put(ws_rek, f"D{rr}", note, F_NOTE)
    REK_TOT = {"masuk": "C6", "keluar": "C7", "event": "C8", "orang": "C9"}

    # ---- hari yang punya data (urut naik)
    days = sorted({r["ts"].date() for r in recs if r["ts"]})
    dc_pairs = []
    cam_order = {nm: i for i, nm in enumerate(cam_names)}
    seen = set()
    for r in recs:
        if r["ts"]:
            seen.add((r["ts"].date(), r["cam"]))
    dc_pairs = sorted(seen, key=lambda t: (t[0], cam_order.get(t[1], 999)))

    def count_cols(ws, r, key_tgl=None, key_cam=None, key_hour=None, c0=4):
        """Tulis rumus Masuk/Keluar/Total Event/Total Orang mulai kolom c0 (default D..G) baris r."""
        crit = []
        if key_tgl:
            crit.append(f"{TGL},{key_tgl}")
        if key_cam:
            crit.append(f"{KAM},{key_cam}")
        if key_hour:
            crit.append(f"{JAM},{key_hour}")
        base = ",".join(crit)
        sep = "," if base else ""
        ws.cell(r, c0, f'=COUNTIFS({base}{sep}{STAT},"MASUK")')
        ws.cell(r, c0 + 1, f'=COUNTIFS({base}{sep}{STAT},"KELUAR")')
        if base:
            ws.cell(r, c0 + 2, f"=COUNTIFS({base})")
            ws.cell(r, c0 + 3, f"=SUMIFS({TOT},{base})")
        for cc in range(c0, c0 + 4):
            ws.cell(r, cc).number_format = "#,##0"

    rek_heads = ["No", "Tanggal", "Kamera", "Masuk", "Keluar", "Total Event", "Total Orang"]

    # ---- II. harian (semua kamera)
    r = 15
    section(ws_rek, r - 1, "II. REKAP HARIAN — SEMUA KAMERA", 7)
    head_row(ws_rek, r, 1, rek_heads, 24)
    d_first = r + 1
    for i, d in enumerate(days):
        rr = d_first + i
        ws_rek.cell(rr, 1, i + 1)
        ws_rek.cell(rr, 2, datetime.combine(d, time(0, 0))).number_format = FMT_DATE
        ws_rek.cell(rr, 3, ALL_CAM)
        count_cols(ws_rek, rr, key_tgl=f"$B{rr}")
    if not days:
        put(ws_rek, f"B{d_first}", NA, F_NOTE)
    d_last = d_first + max(len(days), 1) - 1
    d_tot = d_last + 1
    ws_rek.cell(d_tot, 3, "TOTAL")
    for cc in range(4, 8):
        ws_rek.cell(d_tot, cc, f"=SUM({L(cc)}{d_first}:{L(cc)}{d_last})").number_format = "#,##0"
    style_range(ws_rek, f"A{d_first}:G{d_last}", F_BASE, border=BOX)
    style_range(ws_rek, f"A{d_tot}:G{d_tot}", F_BOLD, FILL_TOT, border=BOX)
    DAILY = {"first": d_first, "last": d_last, "tot": d_tot}

    # ---- III. tanggal x kamera
    r = d_tot + 3
    section(ws_rek, r - 1, "III. REKAP BERDASARKAN TANGGAL DAN KAMERA", 7)
    head_row(ws_rek, r, 1, rek_heads, 24)
    dc_first = r + 1
    for i, (d, nm) in enumerate(dc_pairs):
        rr = dc_first + i
        ws_rek.cell(rr, 1, i + 1)
        ws_rek.cell(rr, 2, datetime.combine(d, time(0, 0))).number_format = FMT_DATE
        ws_rek.cell(rr, 3, nm)
        count_cols(ws_rek, rr, key_tgl=f"$B{rr}", key_cam=f"$C{rr}")
    if not dc_pairs:
        put(ws_rek, f"B{dc_first}", NA, F_NOTE)
    dc_last = dc_first + max(len(dc_pairs), 1) - 1
    dc_tot = dc_last + 1
    ws_rek.cell(dc_tot, 3, "TOTAL")
    for cc in range(4, 8):
        ws_rek.cell(dc_tot, cc, f"=SUM({L(cc)}{dc_first}:{L(cc)}{dc_last})").number_format = "#,##0"
    style_range(ws_rek, f"A{dc_first}:G{dc_last}", F_BASE, border=BOX)
    style_range(ws_rek, f"A{dc_tot}:G{dc_tot}", F_BOLD, FILL_TOT, border=BOX)
    DC = {"first": dc_first, "last": dc_last, "tot": dc_tot}

    put(ws_rek, f"A{dc_tot + 1}", "Catatan: event tanpa timestamp tidak dapat ditempatkan pada tanggal/jam; selisihnya ditampilkan di sheet VALIDASI. Rekap per jam & per kamera: lihat bagian IV (filter) di kolom J:O.", F_NOTE)
    for rr in range(d_first, dc_last + 1):
        ws_rek.cell(rr, 1).alignment = Alignment(horizontal="center")
        ws_rek.cell(rr, 2).alignment = Alignment(horizontal="center")
    widths(ws_rek, [6, 28, 30, 12, 12, 13, 13])
    ws_rek.freeze_panes = None

    # ---- IV. REKAP JAM DENGAN FILTER (kolom J:O, di sebelah kanan) + diagram batang dinamis
    section(ws_rek, 5, "IV. REKAP JAM DENGAN FILTER — pilih sel kuning; tabel & diagram mengikuti", 6, col=10)
    ctr = [("J6", "Kamera", "K6", default_camera or ALL_CAM), ("J7", "Status", "K7", "Semua"),
           ("J8", "Dari Tanggal", "K8", None), ("L8", "Sampai Tanggal", "M8", None),
           ("J9", "Dari Jam (0–23)", "K9", None), ("L9", "Sampai Jam (0–23)", "M9", None)]
    for lref, lab, vref, val in ctr:
        put(ws_rek, lref, lab, F_BOLD)
        put(ws_rek, vref, val, F_INPUT, align=Alignment(horizontal="center"), border=BOX, fill=FILL_INPUT)
    ws_rek.merge_cells("K6:M6")
    for cc in ("L6", "M6"):
        ws_rek[cc].border = BOX
    ws_rek["K8"].number_format = ws_rek["M8"].number_format = FMT_DATE
    dvr1 = DataValidation(type="list", formula1=f"={cam_list_all}", allow_blank=False)
    dvr2 = DataValidation(type="list", formula1=f"={stat_list_ref}", allow_blank=False)
    dvr3 = DataValidation(type="date", operator="greaterThan", formula1="36526", allow_blank=True)
    dvr4 = DataValidation(type="whole", operator="between", formula1="0", formula2="23", allow_blank=True)
    for d_ in (dvr1, dvr2, dvr3, dvr4):
        ws_rek.add_data_validation(d_)
    dvr1.add("K6"); dvr2.add("K7"); dvr3.add("K8"); dvr3.add("M8"); dvr4.add("K9"); dvr4.add("M9")
    put(ws_rek, "J10", "Kosongkan Tanggal/Jam untuk semua. Jam di luar rentang tampil 0. Kamera baru: tambahkan di REFERENSI!H.", F_NOTE)
    # kriteria turunan (kolom R:S)
    put(ws_rek, "R5", "Kriteria otomatis (jangan diubah)", F_NOTE)
    rcrit = [("R6", "S6", "Kamera", '=IF(OR($K$6="",$K$6="Semua Kamera"),"*",$K$6)'),
             ("R7", "S7", "Status", '=IF(OR($K$7="",$K$7="Semua"),"*",$K$7)'),
             ("R8", "S8", "Tgl dari", '=IF($K$8="",0,$K$8)'),
             ("R9", "S9", "Tgl sampai", '=IF($M$8="",2958465,$M$8)'),
             ("R10", "S10", "Jam dari", '=IF($K$9="",0,$K$9)'),
             ("R11", "S11", "Jam sampai", '=IF($M$9="",23,$M$9)')]
    for lref, vref, lab, f in rcrit:
        put(ws_rek, lref, lab, F_NOTE)
        put(ws_rek, vref, f, F_NOTE)
    put(ws_rek, "R12", "Judul diagram", F_NOTE)
    put(ws_rek, "S12", '="Diagram People Counting — "&IF($S$6="*","Semua Kamera",$K$6)', F_NOTE)
    RF = (f'{KAM},$S$6,{STAT},$S$7,{TGL},">="&$S$8,{TGL},"<="&$S$9,{JAM},">="&$S$10,{JAM},"<="&$S$11')
    head_row(ws_rek, 12, 10, ["Jam", "Kamera", "Masuk", "Keluar", "Total Event", "Total Orang"], 24)
    RF1 = 13
    for h in range(24):
        rr = RF1 + h
        ws_rek.cell(rr, 10, h).number_format = FMT_HOUR
        ws_rek.cell(rr, 11, '=IF($S$6="*","Semua Kamera",$K$6)')
        ws_rek.cell(rr, 12, f'=COUNTIFS({RF},{JAM},$J{rr},{STAT},"MASUK")')
        ws_rek.cell(rr, 13, f'=COUNTIFS({RF},{JAM},$J{rr},{STAT},"KELUAR")')
        ws_rek.cell(rr, 14, f"=COUNTIFS({RF},{JAM},$J{rr})")
        ws_rek.cell(rr, 15, f"=SUMIFS({TOT},{RF},{JAM},$J{rr})")
    RF2 = RF1 + 23
    ws_rek.cell(RF2 + 1, 11, "TOTAL (sesuai filter)")
    for cc in range(12, 16):
        ws_rek.cell(RF2 + 1, cc, f"=SUM({L(cc)}{RF1}:{L(cc)}{RF2})")
    style_range(ws_rek, f"J{RF1}:O{RF2}", F_BASE, border=BOX, align=Alignment(horizontal="center"), fmt="#,##0")
    style_range(ws_rek, f"J{RF2 + 1}:O{RF2 + 1}", F_BOLD, FILL_TOT, border=BOX, align=Alignment(horizontal="center"), fmt="#,##0")
    for rr in range(RF1, RF2 + 1):
        ws_rek.cell(rr, 10).number_format = FMT_HOUR
    rc = BarChart()
    rc.type, rc.grouping = "col", "clustered"
    rc.title = Title(tx=Text(strRef=StrRef(f=f"{q(S_REK)}!$S$12", strCache=StrData(ptCount=1, pt=[StrVal(idx=0, v="Diagram People Counting — " + (default_camera or ALL_CAM))]))), overlay=False)
    rc.y_axis.title, rc.x_axis.title = "Jumlah event", "Jam"
    rc.height, rc.width = 9, 17
    rc.add_data(Reference(ws_rek, min_col=12, max_col=13, min_row=12, max_row=RF2), titles_from_data=True)
    rc.set_categories(Reference(ws_rek, min_col=10, min_row=RF1, max_row=RF2))
    rc.legend.position = "b"
    rc.x_axis.delete = False
    rc.y_axis.delete = False
    rc.x_axis.number_format = FMT_HOUR
    ws_rek.add_chart(rc, f"J{RF2 + 4}")
    for col_, w_ in zip("JKLMNO", (14, 26, 12, 12, 13, 13)):
        ws_rek.column_dimensions[col_].width = w_
    REKAP_FLT = {"first": RF1, "last": RF2, "tot": RF2 + 1}
    page_setup(ws_rek, landscape=False)

    # ============================================================ ANALISIS (matriks jam x kamera)
    ncol_ana = 3 + ncam
    title_block(ws_ana, max(ncol_ana, 8), periode, "ANALISIS POLA COUNTING (otomatis dari DATA DETAIL)")
    section(ws_ana, 5, "A. JUMLAH EVENT PER JAM PER KAMERA (perbandingan antar kamera pada jam yang sama)", max(ncol_ana, 8))
    M_HDR = 7
    head_row(ws_ana, M_HDR, 1, ["Jam"] + cam_names + [ALL_CAM], 36)
    M1, M2 = M_HDR + 1, M_HDR + 24
    sum_col = L(2 + ncam)
    for h in range(24):
        rr = M1 + h
        ws_ana.cell(rr, 1, h).number_format = FMT_HOUR
        for i in range(ncam):
            col = L(2 + i)
            ws_ana.cell(rr, 2 + i, f"=COUNTIFS({KAM},{col}${M_HDR},{JAM},$A{rr})")
        ws_ana.cell(rr, 2 + ncam, f"=SUM(B{rr}:{L(1 + ncam)}{rr})" if ncam else 0)
    tr = M2 + 1
    ws_ana.cell(tr, 1, "TOTAL")
    for cc in range(2, 3 + ncam):
        ws_ana.cell(tr, cc, f"=SUM({L(cc)}{M1}:{L(cc)}{M2})")
    style_range(ws_ana, f"A{M1}:{sum_col}{M2}", F_BASE, border=BOX, align=Alignment(horizontal="center"))
    style_range(ws_ana, f"A{tr}:{sum_col}{tr}", F_BOLD, FILL_TOT, border=BOX, align=Alignment(horizontal="center"))
    sumrng = f"${sum_col}${M1}:${sum_col}${M2}"
    hrs = f"$A${M1}:$A${M2}"

    def hour_text(idx_expr):
        return f'TEXT({idx_expr},"00")&":00 – "&TEXT({idx_expr},"00")&":59"'

    ir = tr + 3
    section(ws_ana, ir - 1, "B. RINGKASAN POLA (semua kamera)", max(ncol_ana, 8))
    # --- blok bantu "jam penuh": jam pertama & terakhir data (jam yang belum berjalan penuh) tidak ikut jam sepi
    min_pos = f"IFERROR(SMALL({sumrng},COUNTIF({sumrng},0)+1),0)"
    dly_ev = f"{q(S_REK)}!$F${d_first}:$F${d_last}"
    dly_dt = f"{q(S_REK)}!$B${d_first}:$B${d_last}"
    pat = [
        ("Jam paling ramai", f'=IF(MAX({sumrng})=0,"{NA}",{hour_text(f"INDEX({hrs},MATCH(MAX({sumrng}),{sumrng},0))")})', None),
        ("Event pada jam paling ramai", f"=MAX({sumrng})", "0"),
        ("Jam paling sepi (jam yang punya event)", f'=IF({min_pos}=0,"{NA}",{hour_text(f"INDEX({hrs},MATCH({min_pos},{sumrng},0))")})', None),
        ("Event pada jam paling sepi", f"={min_pos}", "0"),
        ("Hari tersibuk", f'=IF(COUNT({dly_dt})=0,"{NA}",INDEX({dly_dt},MATCH(MAX({dly_ev}),{dly_ev},0)))', FMT_DATE),
        ("Event pada hari tersibuk", f"=IF(COUNT({dly_dt})=0,0,MAX({dly_ev}))", "0"),
        ("Hari tersepi (di antara hari yang punya data)", f'=IF(COUNT({dly_dt})=0,"{NA}",INDEX({dly_dt},MATCH(MIN({dly_ev}),{dly_ev},0)))', FMT_DATE),
        ("Rata-rata event per hari (hari berdata)", f'=IF(COUNT({dly_dt})=0,"{NA}",{q(S_REK)}!C8/COUNT({dly_dt}))', "0.0"),
        ("Rasio MASUK : KELUAR", f'=IF({q(S_REK)}!C7=0,"{NA}",ROUND({q(S_REK)}!C6/{q(S_REK)}!C7,2)&" : 1")', None),
    ]
    for i, (lab, f, nf) in enumerate(pat):
        rr = ir + i
        ws_ana.merge_cells(start_row=rr, start_column=1, end_row=rr, end_column=3)
        put(ws_ana, f"A{rr}", lab, F_BOLD, border=BOX)
        for cc in (2, 3):
            ws_ana.cell(rr, cc).border = BOX
        put(ws_ana, f"D{rr}", f, F_BOLD, nf, Alignment(horizontal="right"), BOX)
    pr_end = ir + len(pat)

    cr = pr_end + 3
    section(ws_ana, cr - 1, "C. POLA PER KAMERA", max(ncol_ana, 8))
    head_row(ws_ana, cr, 1, ["No", "Kamera", "Jam Ramai", "Event Jam Ramai", "Jam Sepi*", "Event Jam Sepi*",
                             "Masuk", "Keluar", "Selisih (Masuk − Keluar)", "ID Tracking Unik**", "Event per ID Tracking**", "Event Pertama", "Event Terakhir"], 36)
    def _hl(h):
        return f"{h:02d}:00 – {h:02d}:59"
    for i, nm in enumerate(cam_names):
        rr = cr + 1 + i
        st_ = stats[nm]
        ws_ana.cell(rr, 1, i + 1)
        ws_ana.cell(rr, 2, nm)
        ws_ana.cell(rr, 3, _hl(st_["ramai"][0]) if st_["ramai"] else NA)
        ws_ana.cell(rr, 4, st_["ramai"][1] if st_["ramai"] else 0)
        ws_ana.cell(rr, 5, _hl(st_["sepi"][0]) if st_["sepi"] else NA)
        ws_ana.cell(rr, 6, st_["sepi"][1] if st_["sepi"] else 0)
        ws_ana.cell(rr, 7, st_["masuk"])
        ws_ana.cell(rr, 8, st_["keluar"])
        ws_ana.cell(rr, 9, f"=G{rr}-H{rr}")
        ws_ana.cell(rr, 10, f"=SUMIFS({R('U')},{KAM},$B{rr})")
        ws_ana.cell(rr, 11, f'=IF(J{rr}=0,"{NA}",ROUND({q(S_CAM)}!F{7 + i}/J{rr},2))')
        ws_ana.cell(rr, 12, st_["first"] if st_["first"] else NA).number_format = FMT_TS
        ws_ana.cell(rr, 13, st_["last"] if st_["last"] else NA).number_format = FMT_TS
    cr_end = cr + max(ncam, 1)
    style_range(ws_ana, f"A{cr + 1}:M{cr_end}", F_BASE, border=BOX, align=Alignment(horizontal="center"))
    put(ws_ana, f"A{cr_end + 1}", "Bagian C dihitung di Python per kamera dari data sumber (lihat sheet AUDIT KAMERA untuk 24 jam penuh & pembuktian). * Jam sepi = jam dengan event TERENDAH di antara jam yang memiliki event (jam tanpa event tidak dihitung; jam parsial, mis. program baru menyala 08:54, TIDAK dikeluarkan — lihat kolom Jam Parsial di AUDIT KAMERA). Selisih Masuk−Keluar bukan jumlah orang di dalam terminal kecuali penghitungan dimulai dari kondisi kosong.", F_NOTE)
    put(ws_ana, f"A{cr_end + 2}", "Sistem belum memiliki data pembanding (ground truth); akurasi penghitungan tidak diklaim.", F_NOTE)
    put(ws_ana, f"A{cr_end + 3}", "Aturan seri: bila beberapa jam memiliki jumlah event sama (tertinggi/terendah), dipilih jam PALING AWAL. Setiap kamera dihitung sendiri-sendiri (Python) dari data sumber; hasilnya diuji silang dengan rumus pada AUDIT KAMERA.", F_NOTE)
    put(ws_ana, f"A{cr_end + 4}", "** ID Tracking = nomor pelacak YOLO, BUKAN jumlah orang unik: ID bisa berganti saat orang tertutup/terpotong dan dipakai ulang setelah sistem dijalankan ulang. Jam ramai di sini adalah jumlah EVENT perlintasan garis, bukan orang unik.", F_NOTE)

    ch = BarChart()
    ch.type, ch.grouping = "col", "clustered"
    ch.title = "Event per Jam per Kamera"
    ch.y_axis.title = "Jumlah event"
    ch.x_axis.title = "Jam"
    ch.add_data(Reference(ws_ana, min_col=2, max_col=1 + ncam, min_row=M_HDR, max_row=M2), titles_from_data=True) if ncam else None
    ch.set_categories(Reference(ws_ana, min_col=1, min_row=M1, max_row=M2))
    ch.height, ch.width = 9, 24
    ch.legend.position = "b"
    ch.x_axis.delete = False
    ch.y_axis.delete = False
    ch.x_axis.number_format = FMT_HOUR
    ws_ana.add_chart(ch, f"A{cr_end + 6}")

    ws_ana.column_dimensions["L"].width = max(ws_ana.column_dimensions["L"].width or 0, 20)
    ws_ana.column_dimensions["M"].width = max(ws_ana.column_dimensions["M"].width or 0, 20)
    widths(ws_ana, [14, 28] + [18] * max(ncam, 1) + [16, 16, 16, 16])
    ws_ana.column_dimensions["A"].width = 14
    for _c in ("I", "J", "K"):
        ws_ana.column_dimensions[_c].width = max(ws_ana.column_dimensions[_c].width or 0, 16)
    page_setup(ws_ana)


    # ============================================================ AUDIT KAMERA (pembuktian perhitungan per kamera)
    nc_ = max(ncam, 1)
    title_block(ws_aud, 30, periode, "AUDIT PERHITUNGAN PER KAMERA (Python vs rumus Excel)")
    section(ws_aud, 5, "A. EVENT VALID PER JAM PER KAMERA (dihitung di Python dari data sumber; jam = timestamp lokal)", 30)
    A_HDR = 6
    head_row(ws_aud, A_HDR, 1, ["Kamera", "ID Kamera", "Event Valid"] + [f"{h:02d}.00" for h in range(24)] + ["Jumlah 24 Jam", "Jam Parsial"], 30)
    for i, nm in enumerate(cam_names):
        rr = A_HDR + 1 + i
        st_ = stats[nm]
        ws_aud.cell(rr, 1, nm)
        ws_aud.cell(rr, 2, st_["cid"] if st_["cid"] is not None else "-")
        ws_aud.cell(rr, 3, st_["valid"])
        for h in range(24):
            ws_aud.cell(rr, 4 + h, st_["hourly"][h])
        ws_aud.cell(rr, 28, f"=SUM(D{rr}:AA{rr})")
        if st_["first"]:
            ws_aud.cell(rr, 29, f"{st_['first'].hour:02d}.00 (pertama, mulai {st_['first'].strftime('%H:%M')}); {st_['last'].hour:02d}.00 (terakhir, sampai {st_['last'].strftime('%H:%M')})")
        else:
            ws_aud.cell(rr, 29, NA)
    A1, A2 = A_HDR + 1, A_HDR + nc_
    style_range(ws_aud, f"A{A1}:AC{A2}", F_BASE, border=BOX, align=Alignment(horizontal="center"))
    for rr in range(A1, A2 + 1):
        ws_aud.cell(rr, 1).alignment = LEFT
        ws_aud.cell(rr, 29).alignment = LEFT

    B_SEC = A2 + 2
    D_HDR = B_SEC + 2 + nc_ + 2 + 4 + 5 + 2     # posisi blok D (di bawah blok C), dihitung di muka
    section(ws_aud, B_SEC, "B. HASIL PER KAMERA & STATUS VALIDASI (hasil Python dibandingkan dengan rumus COUNTIFS pada ANALISIS & REKAP PER KAMERA)", 30)
    B_HDR = B_SEC + 1
    head_row(ws_aud, B_HDR, 1, ["Kamera", "Event Valid", "Jam Ramai", "Event Jam Ramai", "Jam dgn Event Sama (ramai)", "Jam Sepi", "Event Jam Sepi",
                                "Jam dgn Event Sama (sepi)", "Jam Berevent", "Total Masuk", "Total Keluar", "Selisih Rumus (24 jam)",
                                "Masuk=REKAP?", "Keluar=REKAP?", "Status Validasi"], 44)
    for i, nm in enumerate(cam_names):
        rr = B_HDR + 1 + i
        ar = A_HDR + 1 + i
        st_ = stats[nm]
        ws_aud.cell(rr, 1, nm)
        ws_aud.cell(rr, 2, f"=C{ar}")
        ws_aud.cell(rr, 3, f"{st_['ramai'][0]:02d}.00–{st_['ramai'][0]:02d}.59" if st_["ramai"] else NA)
        ws_aud.cell(rr, 4, st_["ramai"][1] if st_["ramai"] else 0)
        ws_aud.cell(rr, 5, st_["ties_ramai"])
        ws_aud.cell(rr, 6, f"{st_['sepi'][0]:02d}.00–{st_['sepi'][0]:02d}.59" if st_["sepi"] else NA)
        ws_aud.cell(rr, 7, st_["sepi"][1] if st_["sepi"] else 0)
        ws_aud.cell(rr, 8, st_["ties_sepi"])
        ws_aud.cell(rr, 9, st_["active_hours"])
        ws_aud.cell(rr, 10, st_["masuk"])
        ws_aud.cell(rr, 11, st_["keluar"])
        mc = L(2 + i)
        ws_aud.cell(rr, 12, f"=SUMPRODUCT(ABS(D{D_HDR + 1 + i}:AA{D_HDR + 1 + i}))")
        ws_aud.cell(rr, 13, f'=IF(J{rr}={q(S_CAM)}!D{7 + i},"Ya","TIDAK")')
        ws_aud.cell(rr, 14, f'=IF(K{rr}={q(S_CAM)}!E{7 + i},"Ya","TIDAK")')
        ws_aud.cell(rr, 15, f'=IF(AND(L{rr}=0,M{rr}="Ya",N{rr}="Ya",B{rr}=AB{ar},B{rr}=J{rr}+K{rr}),"SESUAI","PERLU VALIDASI")')
    B1, B2 = B_HDR + 1, B_HDR + nc_
    style_range(ws_aud, f"A{B1}:O{B2}", F_BASE, border=BOX, align=Alignment(horizontal="center"))
    for rr in range(B1, B2 + 1):
        ws_aud.cell(rr, 1).alignment = LEFT
        ws_aud.cell(rr, 15).font = F_BOLD
    flag_colors(ws_aud, f"O{B1}:O{B2}", "PERLU VALIDASI", "SESUAI")

    C_SEC = B2 + 2
    section(ws_aud, C_SEC, "C. PERBANDINGAN DISTRIBUSI ANTAR KAMERA", 30)
    pairs_same = []
    for i in range(ncam):
        for j in range(i + 1, ncam):
            if stats[cam_names[i]]["valid"] > 0 and stats[cam_names[i]]["hourly"] == stats[cam_names[j]]["hourly"]:
                pairs_same.append((cam_names[i], cam_names[j]))
    rm = {stats[n]["ramai"][0] for n in cam_names if stats[n]["ramai"]}
    sp = {stats[n]["sepi"][0] for n in cam_names if stats[n]["sepi"]}
    active_cams = [n for n in cam_names if stats[n]["valid"] > 0]
    lines = [
        f"Kamera dengan event: {len(active_cams)} dari {ncam}. Jam ramai berbeda antar kamera: {len(rm)} nilai unik; jam sepi berbeda: {len(sp)} nilai unik.",
        ("Distribusi 24 jam IDENTIK antar kamera: " + "; ".join(f"{a} = {b}" for a, b in pairs_same)) if pairs_same
        else "Tidak ada dua kamera dengan distribusi 24 jam yang identik.",
    ]
    if len(active_cams) > 1 and len(rm) == 1:
        lines.append("Seluruh kamera berjam ramai sama. Bandingkan baris pada tabel A: bila jumlah event per jam berbeda, kesamaan hanya kebetulan puncak jam yang sama; hasil ini mengikuti data, tidak dipaksa.")
    if len(active_cams) > 1 and len(sp) == 1:
        lines.append("Seluruh kamera berjam sepi sama; periksa tabel A, dan kolom Jam Parsial (jam saat program baru menyala/berhenti biasanya paling sedikit event).")
    for k, t in enumerate(lines):
        put(ws_aud, f"A{C_SEC + 1 + k}", t, F_BASE)
    nr = C_SEC + 2 + len(lines)
    put(ws_aud, f"A{nr}", "Aturan: event valid = punya timestamp; status di luar MASUK/KELUAR atau tanpa timestamp membuat status baris PERLU VALIDASI. Jam ramai = event terbanyak; jam sepi = event paling sedikit di antara jam yang punya event (>0). Bila seri, dipilih jam PALING AWAL.", F_NOTE)
    put(ws_aud, f"A{nr + 1}", "Timestamp dibaca apa adanya dari kolom `timestamp` database (waktu lokal server saat event dicatat; tidak ada konversi zona waktu).", F_NOTE)
    put(ws_aud, f"A{nr + 2}", "Kolom 'Selisih Rumus' = jumlah selisih mutlak 24 jam antara hitungan Python (tabel A) dan COUNTIFS pada ANALISIS bagian A; 0 berarti identik. Rumus array: bila WPS menampilkan salah, tekan Ctrl+Shift+Enter.", F_NOTE)
    assert nr + 3 < D_HDR, "blok D bertabrakan"
    section(ws_aud, D_HDR - 1, "D. SELISIH PER JAM: hitungan Python (tabel A) dikurangi rumus COUNTIFS pada ANALISIS bagian A (harus 0 semua)", 30)
    head_row(ws_aud, D_HDR, 1, ["Kamera", "", ""] + [f"{h:02d}.00" for h in range(24)], 24)
    for i, nm in enumerate(cam_names):
        rr = D_HDR + 1 + i
        ws_aud.cell(rr, 1, nm)
        mc = L(2 + i)
        for h in range(24):
            ws_aud.cell(rr, 4 + h, f"={L(4 + h)}{A_HDR + 1 + i}-{q(S_ANA)}!{mc}{M1 + h}")
    style_range(ws_aud, f"A{D_HDR + 1}:AA{D_HDR + nc_}", F_BASE, border=BOX, align=Alignment(horizontal="center"))
    widths(ws_aud, [30, 12, 12] + [7] * 24 + [14, 46])
    ws_aud.freeze_panes = "B7"
    page_setup(ws_aud)

    # ============================================================ REKAP PER KAMERA
    title_block(ws_cam, 14, periode, "REKAP PER KAMERA (otomatis dari DATA DETAIL)")
    section(ws_cam, 5, "Setiap angka dihitung dengan rumus dari DATA DETAIL; klik \"Buka\" untuk melihat data detail kamera.", 14)
    cam_heads = ["No", "Kamera", "Lokasi", "Total Masuk", "Total Keluar", "Total Event", "Total Orang",
                 "Kontribusi (%)", "Ranking", "Event Tertinggi (per jam)", "Jam Tersibuk",
                 "Event PERLU VALIDASI", "Event Tanpa Foto", "Data Detail"]
    head_row(ws_cam, 6, 1, cam_heads, 44)
    c1, c2 = 7, 6 + max(ncam, 1)
    for i, c in enumerate(cams):
        rr = 7 + i
        mcol = L(2 + i)
        rg = f"{q(S_ANA)}!${mcol}${M1}:${mcol}${M2}"
        ws_cam.cell(rr, 1, i + 1)
        ws_cam.cell(rr, 2, c["name"])
        ws_cam.cell(rr, 3, c["loc"])
        ws_cam.cell(rr, 4, f'=COUNTIFS({KAM},$B{rr},{STAT},"MASUK")')
        ws_cam.cell(rr, 5, f'=COUNTIFS({KAM},$B{rr},{STAT},"KELUAR")')
        ws_cam.cell(rr, 6, f"=COUNTIFS({KAM},$B{rr})")
        ws_cam.cell(rr, 7, f"=SUMIFS({TOT},{KAM},$B{rr})")
        ws_cam.cell(rr, 8, f"=IF($F${c2 + 1}=0,0,F{rr}/$F${c2 + 1})").number_format = FMT_PCT
        ws_cam.cell(rr, 9, f"=RANK(F{rr},$F${c1}:$F${c2})")
        ws_cam.cell(rr, 10, f"=MAX({rg})")
        ws_cam.cell(rr, 11, f'=IF(J{rr}=0,"{NA}",TEXT(INDEX({q(S_ANA)}!$A${M1}:$A${M2},MATCH(J{rr},{rg},0)),"00")&":00 – "&TEXT(INDEX({q(S_ANA)}!$A${M1}:$A${M2},MATCH(J{rr},{rg},0)),"00")&":59")')
        ws_cam.cell(rr, 12, f'=COUNTIFS({KAM},$B{rr},{VALID},"PERLU VALIDASI")')
        ws_cam.cell(rr, 13, f'=COUNTIFS({KAM},$B{rr},{FOTO},"<>Tersedia")')
        lk = ws_cam.cell(rr, 14, "Buka")
        lk.hyperlink = Hyperlink(ref=lk.coordinate, location=f"{q(cam_sheet[c['name']])}!A1", display="Buka")
        lk.font = F_LINK
    tr = c2 + 1
    ws_cam.cell(tr, 2, "TOTAL SELURUH KAMERA")
    for cc in (4, 5, 6, 7, 12, 13):
        ws_cam.cell(tr, cc, f"=SUM({L(cc)}{c1}:{L(cc)}{c2})")
    ws_cam.cell(tr, 8, f"=SUM(H{c1}:H{c2})").number_format = FMT_PCT
    style_range(ws_cam, f"A{c1}:N{c2}", F_BASE, border=BOX, align=Alignment(horizontal="center"))
    style_range(ws_cam, f"A{tr}:N{tr}", F_BOLD, FILL_TOT, border=BOX, align=Alignment(horizontal="center"))
    for rr in range(c1, c2 + 1):
        ws_cam.cell(rr, 2).alignment = LEFT
        ws_cam.cell(rr, 3).alignment = LEFT
        ws_cam.cell(rr, 14).font = F_LINK
        ws_cam.cell(rr, 8).number_format = FMT_PCT
    ws_cam.cell(tr, 8).number_format = FMT_PCT
    put(ws_cam, f"A{tr + 2}", "Total Masuk/Keluar = jumlah event berstatus MASUK/KELUAR. Total Orang = jumlah kolom Total Orang (1 orang per event).", F_NOTE)
    put(ws_cam, f"A{tr + 3}", "Event Tertinggi = jumlah event terbanyak dalam satu jam. Jam Tersibuk = jam tempat nilai tersebut terjadi.", F_NOTE)
    widths(ws_cam, [5, 28, 24, 13, 13, 13, 13, 13, 9, 15, 17, 15, 13, 11])
    ws_cam.freeze_panes = "C7"
    page_setup(ws_cam)
    CAMROW = {c["name"]: 7 + i for i, c in enumerate(cams)}

    # ============================================================ SHEET PER KAMERA (rumus dari DATA DETAIL)
    pk_heads = ["No", "ID Event", "ID Orang", "Tanggal", "Waktu", "Status", "Total Orang", "File Foto",
                "Keterangan", "Status Validasi", "Baris Sumber di DATA DETAIL"]
    per_cam_counts = {}
    for r_ in recs:
        per_cam_counts[r_["cam"]] = per_cam_counts.get(r_["cam"], 0) + 1

    def build_cam_rows(ws, cam_cell, hdr, nrows, first_col_key):
        """Tulis nrows baris rumus penarik data dari DATA DETAIL untuk kamera di `cam_cell`."""
        det = lambda col: f"{q(S_DET)}!${col}${FIRST}:${col}${EXT}"
        for k in range(nrows):
            rr = hdr + 1 + k
            ws.cell(rr, 11, f'=IFERROR(MATCH({cam_cell}&"|"&{k + 1},{det("T")},0),"")')
            ix = lambda col: f'INDEX({det(col)},$K{rr})'
            ws.cell(rr, 1, f'=IF($K{rr}="","",{k + 1})')
            ws.cell(rr, 2, f'=IF($K{rr}="","",{ix("B")})')
            ws.cell(rr, 3, f'=IF($K{rr}="","",IF({ix("C")}="","",{ix("C")}))')
            ws.cell(rr, 4, f'=IF($K{rr}="","",IF({ix("F")}="","",{ix("F")}))').number_format = FMT_DATE
            ws.cell(rr, 5, f'=IF($K{rr}="","",IF({ix("G")}="","",{ix("G")}))').number_format = FMT_TIME
            ws.cell(rr, 6, f'=IF($K{rr}="","",IF({ix("J")}="","",{ix("J")}))')
            ws.cell(rr, 7, f'=IF($K{rr}="","",{ix("K")})')
            ws.cell(rr, 8, f'=IF($K{rr}="","",IF({ix("N")}="Tersedia",HYPERLINK({ix("P")},{ix("O")}),{ix("O")}))')
            ws.cell(rr, 9, f'=IF($K{rr}="","",{ix("Q")})')
            ws.cell(rr, 10, f'=IF($K{rr}="","",{ix("R")})')
            for cc in range(1, 12):
                c = ws.cell(rr, cc)
                c.font = F_LINK if cc == 8 else F_BASE
                c.border = BOX
                c.alignment = LEFT if cc in (8, 9) else Alignment(horizontal="center")
        ws.auto_filter.ref = f"A{hdr}:K{hdr + nrows}"
        ws.freeze_panes = f"A{hdr + 1}"
        widths(ws, [6, 10, 10, 12, 10, 10, 11, 32, 40, 17, 14])

    for c in cams:
        ws = cam_ws[c["name"]]
        title_block(ws, 11, periode, None)
        put(ws, "A4", "DATA KAMERA", F_BOLD)
        put(ws, "B4", c["name"], F_T2)
        ws["A4"].border = Border()
        put(ws, "A5", f"Lokasi: {c['loc']}  |  Data ditarik otomatis dari DATA DETAIL (bukan diketik ulang).", F_NOTE)
        cell_name = "$B$4"
        nrows = per_cam_counts.get(c["name"], 0) + 25
        head_row(ws, HDR, 1, pk_heads, 36)
        build_cam_rows(ws, cell_name, HDR, nrows, "T")
        lk = ws.cell(2, 9, "← Kembali ke REKAP PER KAMERA")
        lk.hyperlink = Hyperlink(ref=lk.coordinate, location=f"{q(S_CAM)}!A1", display=lk.value)
        lk.font = F_LINK
        page_setup(ws, title_rows=f"{HDR}:{HDR}")

    # ============================================================ FILTER KAMERA (pilih kamera -> seluruh data detail)
    title_block(ws_flt, 11, periode, "FILTER KAMERA — pilih satu kamera, seluruh data detailnya langsung tampil")
    put(ws_flt, "A5", "Pilih kamera:", F_BOLD)
    ws_flt.merge_cells("B5:D5")
    put(ws_flt, "B5", cam_names[0] if cam_names else NA, F_INPUT, align=LEFT, border=BOX, fill=FILL_INPUT)
    for cc in (3, 4):
        ws_flt.cell(5, cc).border = BOX
    dv = DataValidation(type="list", formula1=f"={cam_list_ref}", allow_blank=False, showDropDown=False)
    dv.error, dv.errorTitle = "Pilih kamera dari daftar.", "Kamera tidak valid"
    ws_flt.add_data_validation(dv)
    dv.add("B5")
    put(ws_flt, "F5", "Total Event:", F_BOLD)
    put(ws_flt, "G5", f"=COUNTIFS({KAM},$B$5)", F_BOLD, "0", Alignment(horizontal="center"), BOX)
    put(ws_flt, "H5", "Masuk:", F_BOLD)
    put(ws_flt, "I5", f'=COUNTIFS({KAM},$B$5,{STAT},"MASUK")', F_BOLD, "0", Alignment(horizontal="center"), BOX)
    put(ws_flt, "J5", "Keluar:", F_BOLD)
    put(ws_flt, "K5", f'=COUNTIFS({KAM},$B$5,{STAT},"KELUAR")', F_BOLD, "0", Alignment(horizontal="center"), BOX)
    head_row(ws_flt, HDR, 1, pk_heads, 36)
    max_cam = max(per_cam_counts.values()) if per_cam_counts else 0
    build_cam_rows(ws_flt, "$B$5", HDR, max_cam + 25, "T")
    page_setup(ws_flt, title_rows=f"{HDR}:{HDR}")

    # ============================================================ DASHBOARD
    title_block(ws_dash, 9, periode, None)
    ws_dash.sheet_view.showGridLines = False
    put(ws_dash, "A4", "DASHBOARD — ringkasan visual (seluruh angka dibaca dari DATA DETAIL melalui rumus)", F_NOTE)
    ws_dash["A4"].border = Border()
    # kolom A sempit sebagai margin
    section(ws_dash, 6, "FILTER INTERAKTIF (ubah sel kuning; semua angka & grafik di bawah mengikuti)", 8, col=2)
    inputs = [
        ("B7", "Kamera", "C7", default_camera or ALL_CAM), ("E7", "Status", "F7", "Semua"),
        ("B8", "Dari Tanggal", "C8", None), ("E8", "Sampai Tanggal", "F8", None),
        ("B9", "Dari Jam (0–23)", "C9", None), ("E9", "Sampai Jam (0–23)", "F9", None),
    ]
    for lref, lab, vref, val in inputs:
        put(ws_dash, lref, lab, F_BOLD)
        put(ws_dash, vref, val, F_INPUT, align=Alignment(horizontal="center"), border=BOX, fill=FILL_INPUT)
    ws_dash.merge_cells("C7:D7")
    ws_dash["D7"].border = BOX
    ws_dash["C8"].number_format = ws_dash["F8"].number_format = FMT_DATE
    dv1 = DataValidation(type="list", formula1=f"={cam_list_all}", allow_blank=False)
    dv2 = DataValidation(type="list", formula1=f"={stat_list_ref}", allow_blank=False)
    dv3 = DataValidation(type="date", operator="greaterThan", formula1="36526", allow_blank=True)
    dv4 = DataValidation(type="whole", operator="between", formula1="0", formula2="23", allow_blank=True)
    for d_ in (dv1, dv2, dv3, dv4):
        ws_dash.add_data_validation(d_)
    dv1.add("C7"); dv2.add("F7"); dv3.add("C8"); dv3.add("F8"); dv4.add("C9"); dv4.add("F9")
    put(ws_dash, "B10", "Kosongkan Tanggal/Jam untuk menampilkan semua. Untuk melihat rincian per event, gunakan sheet FILTER KAMERA / DATA DETAIL.", F_NOTE)
    # kriteria turunan (kolom K:L, di luar area cetak)
    put(ws_dash, "K6", "Kriteria otomatis (jangan diubah)", F_NOTE)
    crit = [("K7", "Kamera", '=IF(OR($C$7="",$C$7="Semua Kamera"),"*",$C$7)'),
            ("K8", "Status", '=IF(OR($F$7="",$F$7="Semua"),"*",$F$7)'),
            ("K9", "Tgl dari", '=IF($C$8="",0,$C$8)'),
            ("K10", "Tgl sampai", '=IF($F$8="",2958465,$F$8)'),
            ("K11", "Jam dari", '=IF($C$9="",0,$C$9)'),
            ("K12", "Jam sampai", '=IF($F$9="",23,$F$9)')]
    for ref, lab, f in crit:
        rr = int(ref[1:])
        put(ws_dash, f"J{rr}", lab, F_NOTE)
        put(ws_dash, ref, f, F_NOTE)
    FLT = f'{KAM},$K$7,{STAT},$K$8,{TGL},">="&$K$9,{TGL},"<="&$K$10,{JAM},">="&$K$11,{JAM},"<="&$K$12'
    FLT_NOCAM = f'{STAT},$K$8,{TGL},">="&$K$9,{TGL},"<="&$K$10,{JAM},">="&$K$11,{JAM},"<="&$K$12'

    # --- Total keseluruhan (tanpa filter) dari REKAP
    section(ws_dash, 12, "TOTAL KESELURUHAN (seluruh data, tanpa filter)", 8, col=2)
    kp = [("Total Masuk", f"={q(S_REK)}!{REK_TOT['masuk']}"), ("Total Keluar", f"={q(S_REK)}!{REK_TOT['keluar']}"),
          ("Total Event", f"={q(S_REK)}!{REK_TOT['event']}"), ("Total Orang", f"={q(S_REK)}!{REK_TOT['orang']}")]
    for i, (lab, f) in enumerate(kp):
        col = 2 + 2 * i
        ws_dash.merge_cells(start_row=13, start_column=col, end_row=13, end_column=col + 1)
        ws_dash.merge_cells(start_row=14, start_column=col, end_row=14, end_column=col + 1)
        put(ws_dash, f"{L(col)}13", lab, F_HEAD, align=CENTER, fill=FILL_HEAD)
        ws_dash.cell(13, col + 1).fill = FILL_HEAD
        put(ws_dash, f"{L(col)}14", f, Font(name=FN, size=18, bold=True, color=NAVY), "#,##0", CENTER)
        for cc in (col, col + 1):
            ws_dash.cell(13, cc).border = BOX
            ws_dash.cell(14, cc).border = BOX
    ws_dash.row_dimensions[14].height = 34
    DASH_TOT = {"masuk": "B14", "keluar": "D14", "event": "F14", "orang": "H14"}

    # --- hasil filter
    section(ws_dash, 16, "HASIL FILTER (mengikuti pilihan di atas)", 8, col=2)
    fk = [("Total Masuk", f'=COUNTIFS({FLT},{STAT},"MASUK")'), ("Total Keluar", f'=COUNTIFS({FLT},{STAT},"KELUAR")'),
          ("Total Event", f"=COUNTIFS({FLT})"), ("Total Orang", f"=SUMIFS({TOT},{FLT})")]
    for i, (lab, f) in enumerate(fk):
        col = 2 + 2 * i
        ws_dash.merge_cells(start_row=17, start_column=col, end_row=17, end_column=col + 1)
        ws_dash.merge_cells(start_row=18, start_column=col, end_row=18, end_column=col + 1)
        put(ws_dash, f"{L(col)}17", lab, F_HEAD, align=CENTER, fill=FILL_HEAD)
        ws_dash.cell(17, col + 1).fill = FILL_HEAD
        put(ws_dash, f"{L(col)}18", f, Font(name=FN, size=16, bold=True), "#,##0", CENTER)
        for cc in (col, col + 1):
            ws_dash.cell(17, cc).border = BOX
            ws_dash.cell(18, cc).border = BOX
    ws_dash.row_dimensions[18].height = 30
    DASH_FLT = {"masuk": "B18", "keluar": "D18", "event": "F18", "orang": "H18"}
    put(ws_dash, "B19", "Event tanpa timestamp tidak ikut hasil filter (lihat sheet VALIDASI).", F_NOTE)

    # --- periode
    section(ws_dash, 20, "PERIODE (hari & jam tersibuk mengikuti filter)", 8, col=2)
    # tabel data grafik di bagian bawah (posisi ditentukan di bawah) -> rumus periode merujuk ke sana
    per_cam_first = 26
    per_cam_last = per_cam_first + max(ncam, 1) - 1
    charts_row = per_cam_last + 4
    data_row = charts_row + 36 + 30       # heatmap (charts_row+36 .. +63) berada di atas tabel data grafik
    hour_first_d = data_row + 2
    hour_last_d = hour_first_d + 23
    mvk_first = hour_last_d + 4
    day_first_d = mvk_first + 5
    day_last_d = day_first_d + max(len(days), 1) - 1

    put(ws_dash, "B21", "Tanggal awal data", F_BOLD)
    put(ws_dash, "C21", f"={q(S_REK)}!C11", F_BASE, FMT_DATE, Alignment(horizontal="left"))
    put(ws_dash, "E21", "Tanggal akhir data", F_BOLD)
    put(ws_dash, "F21", f"={q(S_REK)}!C12", F_BASE, FMT_DATE, Alignment(horizontal="left"))
    dh = f"$D${hour_first_d}:$D${hour_last_d}"
    ddt = f"$B${day_first_d}:$B${day_last_d}"
    dde = f"$E${day_first_d}:$E${day_last_d}"
    put(ws_dash, "B22", "Hari tersibuk", F_BOLD)
    put(ws_dash, "C22", f'=IF(OR(COUNT({ddt})=0,MAX({dde})=0),"{NA}",INDEX({ddt},MATCH(MAX({dde}),{dde},0)))',
        F_BASE, FMT_DATE, Alignment(horizontal="left"))
    put(ws_dash, "E22", "Jam tersibuk", F_BOLD)
    hh = f"$A${hour_first_d}:$A${hour_last_d}"
    put(ws_dash, "F22", f'=IF(MAX({dh})=0,"{NA}",TEXT(INDEX({hh},MATCH(MAX({dh}),{dh},0)),"00")&":00 – "&TEXT(INDEX({hh},MATCH(MAX({dh}),{dh},0)),"00")&":59")',
        F_BASE, None, Alignment(horizontal="left"))

    # --- per kamera (tabel + ranking)
    section(ws_dash, per_cam_first - 2, "PER KAMERA (mengikuti filter status/tanggal/jam)", 8, col=2)
    head_row(ws_dash, per_cam_first - 1, 2, ["Kamera", "Masuk", "Keluar", "Total Event", "Total Orang", "Kontribusi", "Ranking"], 28)
    cf, cl = per_cam_first, per_cam_last
    for i, c in enumerate(cams):
        rr = cf + i
        ws_dash.cell(rr, 2, c["name"])
        ws_dash.cell(rr, 3, f'=COUNTIFS({KAM},$B{rr},{FLT_NOCAM},{STAT},"MASUK")')
        ws_dash.cell(rr, 4, f'=COUNTIFS({KAM},$B{rr},{FLT_NOCAM},{STAT},"KELUAR")')
        ws_dash.cell(rr, 5, f"=COUNTIFS({KAM},$B{rr},{FLT_NOCAM})")
        ws_dash.cell(rr, 6, f"=SUMIFS({TOT},{KAM},$B{rr},{FLT_NOCAM})")
        ws_dash.cell(rr, 7, f"=IF(SUM($E${cf}:$E${cl})=0,0,E{rr}/SUM($E${cf}:$E${cl}))").number_format = FMT_PCT
        ws_dash.cell(rr, 8, f"=RANK(E{rr},$E${cf}:$E${cl})")
    style_range(ws_dash, f"B{cf}:H{cl}", F_BASE, border=BOX, align=Alignment(horizontal="center"))
    for rr in range(cf, cl + 1):
        ws_dash.cell(rr, 2).alignment = LEFT
        ws_dash.cell(rr, 7).number_format = FMT_PCT
    put(ws_dash, f"B{cl + 1}", "Ranking berdasarkan jumlah event counting.", F_NOTE)

    # --- tabel data grafik
    section(ws_dash, data_row, "DATA GRAFIK (mengikuti filter)", 8, col=1)
    head_row(ws_dash, data_row + 1, 1, ["Jam", "Masuk", "Keluar", "Total Event"], 24)
    for h in range(24):
        rr = hour_first_d + h
        ws_dash.cell(rr, 1, h).number_format = FMT_HOUR
        ws_dash.cell(rr, 2, f'=COUNTIFS({FLT},{JAM},$A{rr},{STAT},"MASUK")')
        ws_dash.cell(rr, 3, f'=COUNTIFS({FLT},{JAM},$A{rr},{STAT},"KELUAR")')
        ws_dash.cell(rr, 4, f"=COUNTIFS({FLT},{JAM},$A{rr})")
    style_range(ws_dash, f"A{hour_first_d}:D{hour_last_d}", F_BASE, border=BOX, align=Alignment(horizontal="center"))
    head_row(ws_dash, mvk_first - 1, 1, ["Status", "Jumlah"], 22)
    ws_dash.cell(mvk_first, 1, "MASUK"); ws_dash.cell(mvk_first, 2, f"={DASH_FLT['masuk']}")
    ws_dash.cell(mvk_first + 1, 1, "KELUAR"); ws_dash.cell(mvk_first + 1, 2, f"={DASH_FLT['keluar']}")
    style_range(ws_dash, f"A{mvk_first}:B{mvk_first + 1}", F_BASE, border=BOX, align=Alignment(horizontal="center"))
    head_row(ws_dash, day_first_d - 1, 1, ["No", "Tanggal", "Masuk", "Keluar", "Total Event"], 24)
    for i, d in enumerate(days):
        rr = day_first_d + i
        ws_dash.cell(rr, 1, i + 1)
        ws_dash.cell(rr, 2, f"={q(S_REK)}!B{d_first + i}").number_format = FMT_DATE
        ws_dash.cell(rr, 3, f'=COUNTIFS({FLT},{TGL},$B{rr},{STAT},"MASUK")')
        ws_dash.cell(rr, 4, f'=COUNTIFS({FLT},{TGL},$B{rr},{STAT},"KELUAR")')
        ws_dash.cell(rr, 5, f"=COUNTIFS({FLT},{TGL},$B{rr})")
    if not days:
        put(ws_dash, f"B{day_first_d}", NA, F_NOTE)
    style_range(ws_dash, f"A{day_first_d}:E{day_last_d}", F_BASE, border=BOX, align=Alignment(horizontal="center"))
    for rr in range(day_first_d, day_last_d + 1):
        ws_dash.cell(rr, 2).number_format = FMT_DATE

    # --- grafik (semua batang; judul dinamis dari sel; data dari tabel DATA GRAFIK yang mengikuti filter)
    def dyn_title(sheet, ref, text=""):
        # judul terhubung ke sel (berubah saat dropdown berubah); cache berisi teks awal agar tampil di semua aplikasi
        return Title(tx=Text(strRef=StrRef(f=f"{q(sheet)}!{ref}", strCache=StrData(ptCount=1, pt=[StrVal(idx=0, v=text)]))), overlay=False)

    def bar(ws, title_ref, cats, data_refs, anchor, ytitle="Jumlah event", xtitle=None, w=11.6, h=7.8, fmt=None, gap=60, ttext=""):
        c = BarChart()
        c.type = "col"
        c.grouping = "clustered"
        c.title = dyn_title(ws.title, title_ref, ttext)
        c.y_axis.title = ytitle
        if xtitle:
            c.x_axis.title = xtitle
        c.height, c.width = h, w
        c.gapWidth = gap
        for ref in data_refs:
            c.add_data(ref, titles_from_data=True)
        c.set_categories(cats)
        c.legend.position = "b"
        c.x_axis.delete = False
        c.y_axis.delete = False
        if fmt:
            c.x_axis.number_format = fmt
        ws.add_chart(c, anchor)
        return c

    # teks judul dinamis (kolom J:K di luar area cetak) -> berubah mengikuti dropdown Kamera
    KAMTXT = 'IF(OR($C$7="",$C$7="Semua Kamera"),"Semua Kamera",$C$7)'
    TXT_ROW = 14
    for i, (lab, suffix) in enumerate([("Judul grafik jam", " (per Jam)"), ("Judul grafik hari", " (per Hari)"),
                                       ("Judul grafik kamera", " (per Kamera)"), ("Judul grafik Masuk vs Keluar", " (Total)")]):
        put(ws_dash, f"J{TXT_ROW + i}", lab, F_NOTE)
        put(ws_dash, f"K{TXT_ROW + i}", f'="Diagram People Counting — "&{KAMTXT}&"{suffix}"', F_NOTE)
    section(ws_dash, charts_row - 1, "GRAFIK (batang; data & judul berubah mengikuti filter Kamera/Status/Tanggal/Jam di atas)", 8, col=2)
    bar(ws_dash, f"$K${TXT_ROW}", Reference(ws_dash, min_col=1, min_row=hour_first_d, max_row=hour_last_d),
        [Reference(ws_dash, min_col=2, max_col=3, min_row=hour_first_d - 1, max_row=hour_last_d)], f"B{charts_row}",
        xtitle="Jam", fmt=FMT_HOUR, w=10.2, ttext="Diagram People Counting — " + (default_camera or ALL_CAM) + " (per Jam)")
    bar(ws_dash, f"$K${TXT_ROW + 1}", Reference(ws_dash, min_col=2, min_row=day_first_d, max_row=day_last_d),
        [Reference(ws_dash, min_col=3, max_col=4, min_row=day_first_d - 1, max_row=day_last_d)], f"E{charts_row}",
        xtitle="Tanggal", fmt=FMT_DATE, w=10.8, ttext="Diagram People Counting — " + (default_camera or ALL_CAM) + " (per Hari)")
    bar(ws_dash, f"$K${TXT_ROW + 2}", Reference(ws_dash, min_col=2, min_row=cf, max_row=cl),
        [Reference(ws_dash, min_col=3, max_col=5, min_row=cf - 1, max_row=cl)], f"B{charts_row + 17}", xtitle="Kamera", w=10.2, ttext="Diagram People Counting — " + (default_camera or ALL_CAM) + " (per Kamera)")
    bar(ws_dash, f"$K${TXT_ROW + 3}", Reference(ws_dash, min_col=1, min_row=mvk_first, max_row=mvk_first + 1),
        [Reference(ws_dash, min_col=2, min_row=mvk_first - 1, max_row=mvk_first + 1)], f"E{charts_row + 17}", w=10.8, ttext="Diagram People Counting — " + (default_camera or ALL_CAM) + " (Total)")

    # --- Grafik 4: heatmap jam x kamera (sel berwarna, terhubung rumus; mengikuti filter status/tanggal/jam)
    hm_sec = charts_row + 36
    section(ws_dash, hm_sec, "DISTRIBUSI AKTIVITAS PER JAM DAN KAMERA (heatmap event; makin gelap makin ramai)", 8, col=2)
    head_row(ws_dash, hm_sec + 1, 2, ["Jam"] + cam_names + ["Semua Kamera"], 28)
    hm1 = hm_sec + 2
    for h in range(24):
        rr = hm1 + h
        c_ = ws_dash.cell(rr, 2, h)
        c_.number_format = FMT_HOUR
        for i in range(ncam):
            ws_dash.cell(rr, 3 + i, f"=COUNTIFS({KAM},{L(3 + i)}${hm_sec + 1},{FLT_NOCAM},{JAM},$B{rr})")
        ws_dash.cell(rr, 3 + ncam, f"=SUM({L(3)}{rr}:{L(2 + ncam)}{rr})" if ncam else 0)
    hm2 = hm1 + 23
    style_range(ws_dash, f"B{hm1}:{L(3 + ncam)}{hm2}", F_BASE, border=BOX, align=Alignment(horizontal="center"), fmt="#,##0")
    if ncam:
        for i in range(ncam):  # skala warna terpisah per kamera -> jam sibuk & sepi tiap kamera terlihat
            col_ = L(3 + i)
            ws_dash.conditional_formatting.add(f"{col_}{hm1}:{col_}{hm2}", ColorScaleRule(
                start_type="num", start_value=0, start_color="FFFFFF", end_type="max", end_color="5B8DB8"))
    put(ws_dash, f"B{hm2 + 1}", "Warna dinormalkan per kamera (kolom): jam tergelap = jam paling ramai kamera itu. Angka adalah jumlah event; 0 = tidak ada event.", F_NOTE)
    widths(ws_dash, [10, 28, 13, 13, 19, 13, 13, 13, 6, 11, 12])
    ws_dash.print_area = f"A1:I{hm2 + 2}"
    page_setup(ws_dash, landscape=False)

    # ============================================================ VALIDASI
    title_block(ws_val, 12, periode, "VALIDASI DATA & KONSISTENSI ANGKA")
    section(ws_val, 5, "A. PEMERIKSAAN KUALITAS DATA (dari kolom Keterangan pada DATA DETAIL)", 6, col=2)
    head_row(ws_val, 6, 2, ["Pemeriksaan", "Jumlah", "Hasil", "Keterangan"], 24)
    qc = [
        ("ID duplikat", f'=COUNTIF({ISSUE},"*ID duplikat*")', "ID Event muncul lebih dari satu kali"),
        ("Timestamp kosong", f'=COUNTIF({ISSUE},"*Timestamp kosong*")', "Tanggal/waktu tidak ada (event tidak masuk rekap tanggal/jam)"),
        ("Kamera kosong", f'=COUNTIF({ISSUE},"*Kamera kosong*")', "Kamera tidak tercatat / tidak dapat dikenali"),
        ("Status kosong", f'=COUNTIF({ISSUE},"*Status kosong*")', "Status MASUK/KELUAR tidak tercatat"),
        ("Status di luar MASUK/KELUAR", f'=COUNTIF({ISSUE},"*Status di luar*")', "Status lain dipertahankan apa adanya"),
        ("Event tanpa foto", f'=COUNTIF({ISSUE},"*Foto tidak tersedia*")', "Tidak ada nama file / file tidak ada di server (mis. dihapus pembersihan otomatis)"),
        ("Data tidak wajar", f'=COUNTIF({ISSUE},"*masa depan*")+COUNTIF({ISSUE},"*Total kumulatif*")+COUNTIF({ISSUE},"*ID orang kosong*")', "Timestamp masa depan / total kumulatif tidak valid / ID orang kosong"),
        ("Total event PERLU VALIDASI", f'=COUNTIF({VALID},"PERLU VALIDASI")', "Event dengan minimal satu temuan di atas (event TIDAK dihapus)"),
    ]
    for i, (lab, f, note) in enumerate(qc):
        rr = 7 + i
        put(ws_val, f"B{rr}", lab, F_BOLD, border=BOX)
        put(ws_val, f"C{rr}", f, F_BASE, "0", Alignment(horizontal="center"), BOX)
        put(ws_val, f"D{rr}", f'=IF(C{rr}=0,"OK","PERLU VALIDASI")', F_BOLD, None, Alignment(horizontal="center"), BOX)
        put(ws_val, f"E{rr}", note, F_NOTE)
    QC = {"dup": "C7", "ts": "C8", "cam": "C9", "stkosong": "C10", "stlain": "C11", "foto": "C12"}

    cr0 = 7 + len(qc) + 2
    section(ws_val, cr0, "B. KONSISTENSI ANGKA: DATA DETAIL = REKAP = DASHBOARD", 11, col=2)
    head_row(ws_val, cr0 + 1, 2, ["Angka", "DATA DETAIL", "REKAP (ringkasan)", "REKAP harian", "REKAP tanggal×kamera",
                                  "DATA DETAIL (punya jam)", "REKAP per kamera", "DASHBOARD (total)", "Selisih maks", "Status"], 40)
    rs = f"{q(S_REK)}!"
    cons = [
        ("Total Masuk", f'=COUNTIF({STAT},"MASUK")', REK_TOT["masuk"], "D", "masuk"),
        ("Total Keluar", f'=COUNTIF({STAT},"KELUAR")', REK_TOT["keluar"], "E", "keluar"),
        ("Total Event", f"=COUNTA({ID})", REK_TOT["event"], "F", "event"),
        ("Total Orang", f"=SUM({TOT})", REK_TOT["orang"], "G", "orang"),
    ]
    cam_col = {"masuk": "D", "keluar": "E", "event": "F", "orang": "G"}
    # kolom "punya jam": event yang bisa ditempatkan ke slot jam (timestamp valid) -> selisih = event tanpa timestamp
    hour_cov = {"masuk": f'=COUNTIFS({JAM},">=0",{STAT},"MASUK")', "keluar": f'=COUNTIFS({JAM},">=0",{STAT},"KELUAR")',
                "event": f'=COUNTIFS({JAM},">=0")', "orang": f'=SUMIFS({TOT},{JAM},">=0")'}
    for i, (lab, fdet, rtot, colx, key) in enumerate(cons):
        rr = cr0 + 2 + i
        put(ws_val, f"B{rr}", lab, F_BOLD, border=BOX)
        ws_val.cell(rr, 3, fdet)
        ws_val.cell(rr, 4, f"={rs}{rtot}")
        ws_val.cell(rr, 5, f"={rs}{colx}{DAILY['tot']}")
        ws_val.cell(rr, 6, f"={rs}{colx}{DC['tot']}")
        ws_val.cell(rr, 7, hour_cov[key])
        ws_val.cell(rr, 8, f"={q(S_CAM)}!{cam_col[key]}{c2 + 1}")
        ws_val.cell(rr, 9, f"={q(S_DASH)}!{DASH_TOT[key]}")
        ws_val.cell(rr, 10, f"=MAX(C{rr}:I{rr})-MIN(C{rr}:I{rr})")
        ws_val.cell(rr, 11, f'=IF(J{rr}=0,"SESUAI","PERLU VALIDASI DATA")')
    cons_first, cons_last = cr0 + 2, cr0 + 1 + len(cons)
    style_range(ws_val, f"C{cons_first}:K{cons_last}", F_BASE, border=BOX, align=Alignment(horizontal="center"), fmt="#,##0")
    for rr in range(cons_first, cons_last + 1):
        ws_val.cell(rr, 11).font = F_BOLD

    sr = cons_last + 2
    put(ws_val, f"B{sr}", "STATUS KESELURUHAN", F_BOLD)
    put(ws_val, f"C{sr}", f'=IF(COUNTIF(K{cons_first}:K{cons_last},"SESUAI")={len(cons)},"SEMUA ANGKA KONSISTEN","PERLU VALIDASI DATA")',
        Font(name=FN, size=11, bold=True), align=LEFT)
    put(ws_val, f"B{sr + 1}", "Penjelasan sumber selisih", F_BOLD)
    ws_val.merge_cells(start_row=sr + 1, start_column=3, end_row=sr + 1, end_column=11)
    expl = (f'=IF(COUNTIF(K{cons_first}:K{cons_last},"SESUAI")={len(cons)},"Tidak ada selisih: seluruh angka pada DATA DETAIL, REKAP dan DASHBOARD sama.",'
            f'"Selisih ditemukan. Kemungkinan sumber: "'
            f'&IF({QC["ts"]}>0,{QC["ts"]}&" event tanpa timestamp (tidak masuk rekap harian/jam/tanggal×kamera); ","")'
            f'&IF({QC["cam"]}>0,{QC["cam"]}&" event tanpa kamera; ","")'
            f'&IF({QC["stkosong"]}+{QC["stlain"]}>0,({QC["stkosong"]}+{QC["stlain"]})&" event berstatus kosong/selain MASUK-KELUAR; ","")'
            f'&IF({QC["dup"]}>0,{QC["dup"]}&" event ber-ID duplikat; ","")'
            f'&IF(AND({QC["ts"]}=0,{QC["stkosong"]}+{QC["stlain"]}=0),"belum teridentifikasi — periksa DATA DETAIL, mis. baris ditambahkan di luar tabel atau rumus rekap diubah.",""))')
    put(ws_val, f"C{sr + 1}", expl, F_BASE, align=WRAP)
    ws_val.row_dimensions[sr + 1].height = 48
    put(ws_val, f"B{sr + 3}", "Selisih TIDAK disembunyikan: bila angka berbeda, status berubah menjadi PERLU VALIDASI DATA dan sumbernya dijelaskan di atas.", F_NOTE)
    put(ws_val, f"B{sr + 4}", "Akurasi sistem AI tidak diklaim; pemeriksaan ini hanya menguji konsistensi data & kelengkapan pencatatan, bukan kebenaran hitungan terhadap kenyataan.", F_NOTE)
    flag_colors(ws_val, f"D7:D{6 + len(qc)}", "PERLU VALIDASI", "OK")
    flag_colors(ws_val, f"K{cons_first}:K{cons_last}", "PERLU VALIDASI", "SESUAI")
    flag_colors(ws_val, f"C{sr}", "PERLU VALIDASI", "KONSISTEN")
    vc = sr + 7
    section(ws_val, vc, "C. PENGHITUNG KUMULATIF KAMERA vs EVENT LAPORAN (informasi, per kamera)", 10, col=2)
    head_row(ws_val, vc + 1, 2, ["Kamera", "Event MASUK (laporan)", "Penghitung MASUK tertinggi (DB)",
                                 "Event KELUAR (laporan)", "Penghitung KELUAR tertinggi (DB)", "Keterangan"], 40)
    for i, c in enumerate(cams):
        rr = vc + 2 + i
        ws_val.cell(rr, 2, c["name"])
        ws_val.cell(rr, 3, f'=COUNTIFS({KAM},$B{rr},{STAT},"MASUK")')
        ws_val.cell(rr, 4, f'=_xlfn.MAXIFS({R("L")},{KAM},$B{rr})')
        ws_val.cell(rr, 5, f'=COUNTIFS({KAM},$B{rr},{STAT},"KELUAR")')
        ws_val.cell(rr, 6, f'=_xlfn.MAXIFS({R("M")},{KAM},$B{rr})')
        ws_val.merge_cells(start_row=rr, start_column=7, end_row=rr, end_column=11)
        ws_val.cell(rr, 7, f'=IF(AND(C{rr}=D{rr},E{rr}=F{rr}),"Penghitung = jumlah event (periode mencakup seluruh sesi).",'
                           f'"Berbeda: wajar bila periode tidak mencakup seluruh sesi / penghitung di-reset (sesi 23 jam); periksa bila periode = satu sesi penuh.")')
    ve = vc + 1 + max(ncam, 1)
    style_range(ws_val, f"B{vc + 2}:F{ve}", F_BASE, border=BOX, align=Alignment(horizontal="center"), fmt="#,##0")
    for rr in range(vc + 2, ve + 1):
        ws_val.cell(rr, 2).alignment = LEFT
        ws_val.cell(rr, 7).font = F_NOTE
    widths(ws_val, [3, 32, 16, 18, 16, 20, 14, 17, 18, 13, 24])
    page_setup(ws_val)

    wb.active = 0
    wb.calculation.fullCalcOnLoad = True      # paksa Excel/WPS menghitung ulang semua rumus saat dibuka
    wb.save(target)
    return {"events": n, "cameras": ncam, "days": len(days), "heavy_formulas": heavy_ok}
