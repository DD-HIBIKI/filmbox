"""Fit the parametric CN / NN / BB layer used by build_looks.py.

Process record. It needs a *reference* LUT pack that is NOT in this repository
(third-party, no license) plus a few sample photos; point at them with
--ref / --samples. The fitted numbers are committed as look_params.json, so the
shipped LUTs can be rebuilt by build_looks.py without ever running this script.

Usage:
    python fit_all.py --ref "D:/path/to/provia conversion luts" \
                      --samples photo1.jpg photo2.jpg [--out look_params.json] [--iter 2500]
"""
import argparse
import json
import random
import sys
from pathlib import Path

import numpy as np
from PIL import Image

HERE = Path(__file__).resolve().parent
PIVOT = 0.18
TARGETS = {
    "look_classic_neg": "Provia to Classic Neg sRGB.cube",
    "look_nostalgic_neg": "Provia to Nostalgic Neg sRGB.cube",
    "look_bleach_bypass": "Provia to Bleach Bypass sRGB.cube",
}


def load_cube(path):
    """Parse a .cube into a Pillow Color3DLUT (local copy: importing the app
    pulls in `fcntl`, which does not exist on Windows)."""
    from PIL import ImageFilter
    size, vals = None, []
    for line in open(path, errors="ignore"):
        s2 = line.strip()
        if not s2 or s2.startswith("#"):
            continue
        u = s2.upper()
        if u.startswith("LUT_3D_SIZE"):
            size = int(s2.split()[1])
            continue
        if u.startswith(("DOMAIN_MIN", "DOMAIN_MAX", "TITLE", "LUT_1D_SIZE")):
            continue
        try:
            vals += [float(x) for x in s2.split()]
        except ValueError:
            continue
    if size is None or len(vals) != size ** 3 * 3:
        raise SystemExit("bad cube: %s" % path)
    return ImageFilter.Color3DLUT(size, vals)


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


def model(v, p):
    exposure, toe, shoulder, sat, st, ht, lift, mp = p
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


def sample(path, provia, size=(80, 54)):
    image = Image.open(path).convert("RGB").resize(size, Image.BOX)
    return (np.asarray(image.filter(provia), dtype=np.float32).reshape(-1, 3) / 255.0)


def main():
    ap = argparse.ArgumentParser(description="fit the look parameters")
    ap.add_argument("--ref", required=True, help="directory with the reference Provia->X LUTs")
    ap.add_argument("--samples", nargs="+", required=True, help="sample photos")
    ap.add_argument("--out", default=str(HERE / "look_params.json"))
    ap.add_argument("--iter", type=int, default=2500)
    ap.add_argument("--seed", type=int, default=5)
    args = ap.parse_args()

    provia = load_cube(HERE / "luts" / "fuji_provia.cube")
    ref_dir = Path(args.ref)
    xs = np.concatenate([sample(p, provia) for p in args.samples])

    result = {}
    for name, filename in TARGETS.items():
        target_lut = load_cube(ref_dir / filename)
        ys = np.concatenate([
            np.asarray(Image.open(p).convert("RGB").resize((80, 54), Image.BOX)
                       .filter(provia).filter(target_lut),
                       dtype=np.float32).reshape(-1, 3) / 255.0
            for p in args.samples])
        rng = random.Random(args.seed)
        best = None
        for _ in range(args.iter):
            p = (rng.uniform(1.0, 1.9), rng.uniform(0.9, 1.6),
                 rng.uniform(0.8, 1.08), rng.uniform(0.4, 1.0),
                 tuple(rng.uniform(0.88, 1.1) for _ in range(3)),
                 tuple(rng.uniform(0.92, 1.1) for _ in range(3)),
                 rng.uniform(0.0, 0.05),
                 tuple(rng.uniform(-0.25, 0.25) for _ in range(6)))
            err = float(np.abs(model(xs, p) - ys).mean() * 255.0)
            if best is None or err < best[0]:
                best = (err, p)
        print("%-20s MAE %.2f" % (name, best[0]))
        p = best[1]
        result[name] = [p[0], p[1], p[2], p[3], list(p[4]), list(p[5]),
                        p[6], list(p[7])]
    Path(args.out).write_text(json.dumps(result, indent=1) + "\n")
    print("wrote", args.out)


if __name__ == "__main__":
    main()
