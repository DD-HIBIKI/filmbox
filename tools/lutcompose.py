"""Compose .cube LUTs (A then B) into a single LUT, float precision.

历史工具：曾用于把 G'MIC Provia 与第三方「Provia→X」转换表合成。
现在的 look 由 build_looks.py 生成（Provia + 自拟合层），本脚本不再参与出包；
保留是因为它仍是把任何两级 LUT 合表的最简实现。
"""
import os
import numpy as np


def read_cube(path):
    size, vals = None, []
    dmin, dmax = (0.0, 0.0, 0.0), (1.0, 1.0, 1.0)
    for line in open(path, errors="ignore"):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        u = s.upper()
        if u.startswith("LUT_3D_SIZE"):
            size = int(s.split()[1]); continue
        if u.startswith("DOMAIN_MIN"):
            dmin = tuple(float(x) for x in s.split()[1:4]); continue
        if u.startswith("DOMAIN_MAX"):
            dmax = tuple(float(x) for x in s.split()[1:4]); continue
        if u[0].isalpha():
            continue
        vals += [float(x) for x in s.split()]
    arr = np.array(vals, dtype=np.float32).reshape(size, size, size, 3)  # [b][g][r]
    return arr, dmin, dmax


def apply_cube(arr, rgb):
    """Trilinear lookup. rgb: (N,3) float in 0..1 -> (N,3)."""
    n = arr.shape[0]
    p = np.clip(rgb, 0.0, 1.0) * (n - 1)
    i = np.minimum(np.floor(p).astype(np.int32), n - 2)
    f = p - i
    r0, g0, b0 = i[:, 0], i[:, 1], i[:, 2]
    fr, fg, fb = f[:, 0:1], f[:, 1:2], f[:, 2:3]
    out = np.zeros_like(rgb[:])
    for dr in (0, 1):
        wr = fr if dr else 1 - fr
        for dg in (0, 1):
            wg = fg if dg else 1 - fg
            for db in (0, 1):
                wb = fb if db else 1 - fb
                out += arr[b0 + db, g0 + dg, r0 + dr] * (wr * wg * wb)
    return np.clip(out, 0.0, 1.0)


def compose(path_a, path_b, out_path, size=32, title=None):
    a, _, _ = read_cube(path_a)
    b, _, _ = read_cube(path_b)
    axis = np.linspace(0.0, 1.0, size, dtype=np.float32)
    # .cube order is b outer, g, r fastest -> build points in that exact order
    bb, gg, rr = np.meshgrid(axis, axis, axis, indexing="ij")
    flat = np.stack([rr.ravel(), gg.ravel(), bb.ravel()], -1).astype(np.float32)
    mid = apply_cube(a, flat)
    out = apply_cube(b, mid).reshape(size, size, size, 3)
    with open(out_path, "w") as fh:
        fh.write('TITLE "%s"\n' % (title or os.path.basename(out_path)))
        fh.write("LUT_3D_SIZE %d\n" % size)
        fh.write("DOMAIN_MIN 0.0 0.0 0.0\nDOMAIN_MAX 1.0 1.0 1.0\n")
        for b_i in range(size):
            for g_i in range(size):
                for r_i in range(size):
                    r, g, bb = out[b_i, g_i, r_i]
                    fh.write("%.6f %.6f %.6f\n" % (r, g, bb))
    return out


if __name__ == "__main__":
    import sys
    compose(sys.argv[1], sys.argv[2], sys.argv[3])
    print("wrote", sys.argv[3])
