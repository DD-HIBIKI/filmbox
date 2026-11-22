"""Compose the shipped film looks (CN / NN / BB) into single 33^3 sRGB LUTs.

Pipeline (all inputs live in this repository):
    sRGB grid -> G'MIC "fuji_provia" LUT -> fitted parametric layer -> .cube

The parameters in look_params.json were fitted by fit_all.py.
Nothing is quantised: the Provia stage is evaluated with float trilinear
interpolation, so the 33^3 grid keeps its exact sample positions.

Usage:
    python build_looks.py [--luts DIR] [--params FILE] [--out DIR] [--size N]
"""
import argparse
import json
import os
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
PIVOT = 0.18


def read_cube(path):
    """Parse a .cube into an array indexed [b][g][r] (the file's own order)."""
    size, vals = None, []
    for line in open(path, errors="ignore"):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        u = s.upper()
        if u.startswith("LUT_3D_SIZE"):
            size = int(s.split()[1])
            continue
        if u.startswith(("DOMAIN_MIN", "DOMAIN_MAX", "LUT_1D_SIZE", "TITLE")):
            continue
        try:
            vals += [float(x) for x in s.split()]
        except ValueError:
            continue
    if size is None or len(vals) != size ** 3 * 3:
        raise SystemExit("bad cube: %s" % path)
    return np.array(vals, dtype=np.float32).reshape(size, size, size, 3)


def apply_cube(table, rgb):
    """Float trilinear lookup; rgb is (N,3) in 0..1."""
    n = table.shape[0]
    p = np.clip(rgb, 0.0, 1.0) * (n - 1)
    i = np.minimum(np.floor(p).astype(np.int32), n - 2)
    f = (p - i).astype(np.float32)
    out = np.zeros_like(rgb, dtype=np.float32)
    for dr in (0, 1):
        wr = f[:, 0:1] if dr else 1.0 - f[:, 0:1]
        for dg in (0, 1):
            wg = f[:, 1:2] if dg else 1.0 - f[:, 1:2]
            for db in (0, 1):
                wb = f[:, 2:3] if db else 1.0 - f[:, 2:3]
                out += table[i[:, 2] + db, i[:, 1] + dg, i[:, 0] + dr] * (wr * wg * wb)
    return np.clip(out, 0.0, 1.0)


def srgb_decode(v):
    v = np.clip(v, 0.0, 1.0)
    return np.where(v <= 0.04045, v / 12.92, ((v + 0.055) / 1.055) ** 2.4)


def srgb_encode(v):
    v = np.clip(v, 0.0, 1.0)
    return np.where(v <= 0.0031308, v * 12.92, 1.055 * v ** (1 / 2.4) - 0.055)


def matrix(params):
    a, b, c, d, e, f = params
    return np.array([[1 - a - b, a, b], [c, 1 - c - d, d], [e, f, 1 - e - f]],
                    dtype=np.float32)


def apply_layer(v, q):
    """Fitted layer: exposure, toe/shoulder gamma, 3x3 matrix, sat, split tone."""
    exposure, toe, shoulder, sat, st, ht, lift, mp = q
    lin = srgb_decode(v) * exposure
    ratio = np.maximum(lin / PIVOT, 1e-6)
    lin = np.where(lin < PIVOT, ratio ** toe * PIVOT, ratio ** shoulder * PIVOT)
    lin = lin @ matrix(mp).T
    y = (lin @ np.array([0.2126, 0.7152, 0.0722]))[:, None]
    lin = y + (lin - y) * sat
    w = np.clip(y / PIVOT, 0.0, 1.0)
    lin = lin * (np.array(st)[None, :] * (1 - w) + np.array(ht)[None, :] * w)
    lin = lin * (1 - lift) + lift
    return np.clip(srgb_encode(lin), 0.0, 1.0)


def main():
    ap = argparse.ArgumentParser(description="build the shipped look LUTs")
    ap.add_argument("--luts", default=str(HERE / "luts"), help="where fuji_provia.cube lives")
    ap.add_argument("--params", default=str(HERE / "look_params.json"))
    ap.add_argument("--out", default=str(HERE / "out"))
    ap.add_argument("--size", type=int, default=33)
    args = ap.parse_args()

    provia_path = Path(args.luts) / "fuji_provia.cube"
    if not provia_path.exists():
        raise SystemExit("missing %s (the G'MIC Provia LUT is the base layer)" % provia_path)
    provia = read_cube(str(provia_path))
    params = json.loads(Path(args.params).read_text())

    size = args.size
    axis = np.linspace(0.0, 1.0, size)
    bb, gg, rr = np.meshgrid(axis, axis, axis, indexing="ij")   # .cube order
    grid = np.stack([rr.ravel(), gg.ravel(), bb.ravel()], -1).astype(np.float32)
    base = apply_cube(provia, grid)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for name, q in params.items():
        table = apply_layer(base, q).reshape(size, size, size, 3)
        path = out_dir / (name + ".cube")
        with path.open("w") as fh:
            fh.write('TITLE "%s"\nLUT_3D_SIZE %d\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 1 1 1\n'
                     % (name, size))
            for bi in range(size):
                for gi in range(size):
                    for ri in range(size):
                        r, g, b = table[bi, gi, ri]
                        fh.write("%.6f %.6f %.6f\n" % (r, g, b))
        print("wrote", path)


if __name__ == "__main__":
    main()
