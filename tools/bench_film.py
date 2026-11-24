#!/usr/bin/env python3
"""Measure where the film-copy pipeline spends time and RAM."""
import importlib.util
import os
import sys
import time

from PIL import Image, ImageFilter

spec = importlib.util.spec_from_file_location("fb", "/usr/lib/filmbox/filmbox.py")
fb = importlib.util.module_from_spec(spec)
sys.modules["fb"] = fb
spec.loader.exec_module(fb)

SRC = os.path.expanduser("~/Pictures/FilmBox/Photos/cyber_20261123_105520_827_8mp.jpg")
LUT = "fuji_classic_chrome"


def mem():
    return fb.available_mb()


def stage(name, fn):
    t = time.perf_counter()
    m0 = mem()
    out = fn()
    dt = time.perf_counter() - t
    print("  %-22s %6.2fs   mem %3d→%3d MB" % (name, dt, m0, mem()))
    return out, dt


print("start mem", mem())
for tag, size in (("8MP", (3280, 2464)), ("2MP", (1640, 1232))):
    print("\n==", tag, size)
    img, _ = stage("open+convert RGB", lambda: Image.open(SRC).convert("RGB"))
    if size != img.size:
        img, _ = stage("resize BOX", lambda: img.resize(size, Image.Resampling.BOX))
    lut = fb.load_cube_lut(str(fb.LUT_DIR / (LUT + ".cube")))
    img, _ = stage("LUT whole-image", lambda: img.filter(lut))
    t = time.perf_counter()
    img2 = img.copy()
    print("  %-22s %6.2fs" % ("copy (strip baseline)", time.perf_counter() - t))
    img2, _ = stage("halation", lambda: fb.add_halation(img2))
    img2, _ = stage("grain", lambda: fb.add_grain(img2))
    stage("save JPEG q93", lambda: img2.save("/tmp/bench.jpg", quality=93, subsampling=1))
    del img, img2

print("\n== upstream-style built-in filter for comparison")
img, _ = stage("open+convert RGB", lambda: Image.open(SRC).convert("RGB"))
stage("ImageFilter (C, whole 8MP)",
      lambda: img.filter(ImageFilter.EDGE_ENHANCE_MORE))
del img
print("end mem", mem())
