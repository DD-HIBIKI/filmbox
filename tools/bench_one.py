#!/usr/bin/env python3
"""Run ONE engine on one 8 MP frame so memory never stacks up.

usage: python3 bench_one.py cpu|multi|gpu
"""
import importlib.util
import os
import sys
import time

from PIL import Image

engine = sys.argv[1] if len(sys.argv) > 1 else "cpu"
spec = importlib.util.spec_from_file_location("fb", "/usr/lib/filmbox/filmbox.py")
fb = importlib.util.module_from_spec(spec)
sys.modules["fb"] = fb
spec.loader.exec_module(fb)

SRC = os.path.expanduser("~/Pictures/FilmBox/Photos/cyber_20261123_105520_827_8mp.jpg")
CUBE = fb.LUT_DIR / "fuji_classic_chrome.cube"
fb._ACCEL = engine
fb._GPU_STATE = 0
fb._GPU = None

base = Image.open(SRC).convert("RGB")
print("in : %dx%d  mem %d MB" % (base.width, base.height, fb.available_mb()), flush=True)
t = time.perf_counter()
out = fb.film_copy(base, CUBE, progress=None)
dt = time.perf_counter() - t
path = "/tmp/out_%s.jpg" % engine
out.save(path, quality=93, subsampling=1)
print("out: %dx%d  %.2fs  %d KB  engine_state=%d  mem %d MB" %
      (out.width, out.height, dt, os.path.getsize(path) // 1024,
       fb._GPU_STATE, fb.available_mb()), flush=True)
