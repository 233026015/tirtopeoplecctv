"""
Tes logika counting. Jalankan:  python test_line_counter.py
Tidak butuh kamera / YOLO / MySQL.

Bagian 1: unit test logika BARU (LineCounter)
Bagian 2: perbandingan logika LAMA (salinan persis dari main.py versi
          sebelumnya) vs logika BARU pada skenario yang sama.
"""
import random
from line_counter import LineCounter

# ---------------------------------------------------------------------------
# Helper: bikin lintasan orang (koordinat ternormalisasi 0..1)
# ---------------------------------------------------------------------------
def walk(x0, y0, x1, y1, steps):
    return [(x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps) for i in range(steps + 1)]

def run_new(path_norm, line=(0.0, 0.5, 1.0, 0.5), flip=False, W=960, H=540, fps=5.0, tid=1, lc=None):
    lc = lc or LineCounter()
    lc.set_frame_size(W, H)
    lc.set_line_norm(*line, flip=flip)
    ins = outs = 0
    t = 1000.0
    for (nx, ny) in path_norm:
        ev = lc.update(tid, (nx * W, ny * H), now=t)
        ins += ev == "IN"
        outs += ev == "OUT"
        t += 1.0 / fps
    return ins, outs

# ---------------------------------------------------------------------------
# LOGIKA LAMA (salinan dari main.py sebelum diperbaiki)
# ---------------------------------------------------------------------------
def _orientation(a, b, c):
    val = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    if val > 1e-9: return 1
    if val < -1e-9: return -1
    return 0

def old_crossing(p1, p2, prev_pt, curr_pt):
    o1 = _orientation(p1, p2, prev_pt); o2 = _orientation(p1, p2, curr_pt)
    o3 = _orientation(prev_pt, curr_pt, p1); o4 = _orientation(prev_pt, curr_pt, p2)
    if o1 != o2 and o3 != o4 and o1 != 0 and o2 != 0:
        return True, o2
    return False, o2

def run_old(path_norm, line=(0.0, 0.5, 1.0, 0.5), flip=False, tid=1):
    W, H = 480, 270
    p1, p2 = (line[0] * W, line[1] * H), (line[2] * W, line[3] * H)
    prev, id_status, cin, cout = {}, {}, 0, 0
    for (nx, ny) in path_norm:
        cx, cy = int(nx * W), int(ny * H)          # kode lama: titik integer
        if tid in prev:
            crossed, dest = old_crossing(p1, p2, prev[tid], (cx, cy))
            is_masuk = is_keluar = False
            if crossed:
                if (dest > 0) != flip: is_masuk = True
                else: is_keluar = True
            st = id_status.get(tid)
            if is_masuk and st is None:
                cin += 1; id_status[tid] = "MASUK"
            elif is_keluar and st == "MASUK":
                cout += 1; id_status[tid] = "KELUAR"
        prev[tid] = (cx, cy)
    return cin, cout

# ---------------------------------------------------------------------------
# Skenario
# ---------------------------------------------------------------------------
def jitter_path(seed=3):
    """Orang mendekat dari atas, berdiri/ragu di garis dengan kotak deteksi
    bergetar +-5 px (di frame 540p), lalu akhirnya jalan masuk."""
    rnd = random.Random(seed)
    p = walk(0.5, 0.30, 0.5, 0.49, 6)
    p += [(0.5 + rnd.uniform(-.004, .004), 0.5 + rnd.uniform(-.010, .010)) for _ in range(25)]
    p += walk(0.5, 0.52, 0.5, 0.80, 8)
    return p

SCENARIOS = [
    # (nama, path, line, flip, harapan (in,out))
    ("1. Jalan masuk (atas->bawah), langkah 25px",
        walk(0.5, 0.213, 0.5, 0.813, 24), (0, .5, 1, .5), False, (1, 0)),
    ("2. Jalan KELUAR, orang sudah di dalam sebelum sistem nyala",
        walk(0.5, 0.813, 0.5, 0.213, 24), (0, .5, 1, .5), False, (0, 1)),
    ("3. Masuk lalu keluar (ID sama)",
        walk(0.5, 0.213, 0.5, 0.813, 24) + walk(0.5, 0.813, 0.5, 0.213, 24), (0, .5, 1, .5), False, (1, 1)),
    ("4. Masuk, keluar, masuk lagi (ID sama)",
        walk(0.5, 0.213, 0.5, 0.813, 24) + walk(0.5, 0.813, 0.2, 0.213, 24) + walk(0.2, 0.213, 0.5, 0.813, 24),
        (0, .5, 1, .5), False, (2, 1)),
    ("5. Ragu/berdiri di garis (box bergetar) lalu masuk",
        jitter_path(), (0, .5, 1, .5), False, (1, 0)),
    ("6. Jalan masuk, titik kebetulan tepat di garis",
        walk(0.5, 0.40, 0.5, 0.60, 4), (0, .5, 1, .5), False, (1, 0)),   # sampel jatuh persis di y=0.5
    ("7. Garis pendek di tengah, orang lewat MEMUTAR di luar ujung garis",
        walk(0.9, 0.213, 0.9, 0.813, 24), (0.3, .5, 0.7, .5), False, (0, 0)),
    ("8. Garis diagonal, jalan menyilang",
        walk(0.3, 0.213, 0.7, 0.813, 20), (0.2, .3, 0.8, .7), False, None),
    ("9. Arah dibalik (flip) - jalan atas->bawah jadi KELUAR",
        walk(0.5, 0.213, 0.5, 0.813, 24), (0, .5, 1, .5), True, (0, 1)),
]

def main():
    # ---------------- unit test logika baru ----------------
    fails = 0
    print("=" * 78)
    print("UNIT TEST LOGIKA BARU")
    print("=" * 78)
    for name, path, line, flip, expect in SCENARIOS:
        got = run_new(path, line, flip)
        if expect is None:
            ok = (got[0] + got[1]) == 1
            exp_txt = "tepat 1 event"
        else:
            ok = got == expect
            exp_txt = f"harapan {expect}"
        fails += (not ok)
        print(f"[{'PASS' if ok else 'FAIL'}] {name:62s} hasil={got}  ({exp_txt})")

    # Track basi: jeda stream > stale_seconds tidak boleh dihitung sebagai perlintasan
    lc = LineCounter(); lc.set_frame_size(960, 540); lc.set_line_norm(0, .5, 1, .5)
    lc.update(7, (480, 100), now=0.0)
    ev = lc.update(7, (480, 450), now=10.0)   # jeda 10 detik, pindah sisi
    ok = ev is None
    fails += (not ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {'10. Jeda stream 10 detik lalu muncul di sisi lain (tidak dihitung)':62s} hasil={ev}")

    # Beberapa orang berbeda secara bersamaan
    lc = LineCounter(); lc.set_frame_size(960, 540); lc.set_line_norm(0, .5, 1, .5)
    evs = []
    for i in range(0, 25):
        t = 100 + i * 0.2
        ya = 0.2 + 0.6 * i / 24     # orang A turun (masuk)
        yb = 0.8 - 0.6 * i / 24     # orang B naik (keluar)
        evs += [lc.update(1, (200, ya * 540), now=t), lc.update(2, (700, yb * 540), now=t)]
    ok = evs.count("IN") == 1 and evs.count("OUT") == 1
    fails += (not ok)
    print(f"[{'PASS' if ok else 'FAIL'}] {'11. Dua orang berlawanan arah bersamaan':62s} IN={evs.count('IN')} OUT={evs.count('OUT')}")

    # ---------------- perbandingan lama vs baru ----------------
    print()
    print("=" * 78)
    print("PERBANDINGAN LOGIKA LAMA vs BARU (hasil = (masuk, keluar))")
    print("=" * 78)
    print(f"{'Skenario':64s} {'Harapan':9s} {'LAMA':9s} {'BARU':9s}")
    for name, path, line, flip, expect in SCENARIOS:
        if expect is None:
            continue
        o, n = run_old(path, line, flip), run_new(path, line, flip)
        mark_o = "ok " if o == expect else "SALAH"
        mark_n = "ok " if n == expect else "SALAH"
        print(f"{name:64s} {str(expect):9s} {str(o):9s}{mark_o:6s} {str(n):9s}{mark_n}")

    # Skenario 6 diulang untuk banyak "fase" langkah (seberapa sering lama meleset)
    miss_old = miss_new = total = 0
    for step_px in (3, 4, 5, 6, 8, 10, 12):
        for phase in range(0, step_px):
            # orang jalan turun dgn langkah step_px (di frame 270p) mulai dari fase berbeda-beda
            y_start = 100 + phase
            ys = [y_start + k * step_px for k in range(0, 40) if y_start + k * step_px <= 180]
            path = [(0.5, y / 270.0) for y in ys]
            o = run_old(path); n = run_new(path)
            total += 1
            miss_old += (o != (1, 0)); miss_new += (n != (1, 0))
    print()
    print(f"Garis horizontal tengah (0.5), langkah 3-12 px, {total} variasi fase:")
    print(f"  logika LAMA meleset : {miss_old}/{total} ({100*miss_old/total:.0f}%)")
    print(f"  logika BARU meleset : {miss_new}/{total} ({100*miss_new/total:.0f}%)")

    print()
    print("HASIL:", "SEMUA UNIT TEST LULUS" if fails == 0 else f"{fails} TES GAGAL")
    return fails

if __name__ == "__main__":
    raise SystemExit(main())
