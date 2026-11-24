#!/usr/bin/env python3
"""Compare the three film-copy engines on the same 8 MP frame."""
import importlib.util
import os
import sys
import time

from PIL import Image

spec = importlib.util.spec_from_file_location("fb", "/usr/lib/filmbox/filmbox.py")
fb = importlib.util.module_from_spec(spec)
sys.modules["fb"] = fb
spec.loader.exec_module(fb)

SRC = os.path.expanduser(sys.argv[1] if len(sys.argv) > 1
                         else "~/Pictures/FilmBox/Photos/cyber_20261123_105520_827_8mp.jpg")
CUBE = fb.LUT_DIR / "fuji_classic_chrome.cube"
OUT = "/tmp/accel_shots"
os.makedirs(OUT, exist_ok=True)

print("source:", os.path.basename(SRC))
base = Image.open(SRC).convert("RGB")
print("size:", base.size, "mem %d MB" % fb.available_mb())

results = {}
for engine in ("cpu", "multi", "gpu"):
    fb._ACCEL = engine
    fb._GPU_STATE = 0
    fb._GPU = None
    t = time.perf_counter()
    try:
        out = fb.film_copy(base.copy(), CUBE, progress=None)
        dt = time.perf_counter() - t
        path = os.path.join(OUT, "out_%s.jpg" % engine)
        out.save(path, quality=93, subsampling=1)
        results[engine] = (out, dt, os.path.getsize(path))
        print("%-6s %.2fs  %dx%d  %d KB  state=%d" %
              (engine, dt, out.width, out.height, os.path.getsize(path) // 1024, fb._GPU_STATE))
    except Exception as exc:
        print("%-6s FAILED %s: %s" % (engine, type(exc).__name__, exc))

if "cpu" in results:
    import numpy as np
    ref = results["cpu"][0]
    for name, (img, dt, _) in results.items():
        if name == "cpu":
            continue
        if img.size != ref.size:
            print(name, "size differs, skip diff")
            continue
        d = np.abs(np.asarray(img, dtype=np.int16) - np.asarray(ref, dtype=np.int16))
        print("diff %s vs cpu: mean %.2f max %d" % (name, d.mean(), d.max()))
        crop = (1200, 700, 2000, 1200)
        strip = Image.new("RGB", (800, 1000), "#111")
        strip.paste(ref.crop(crop), (0, 0))
        strip.paste(img.crop(crop), (0, 510))
        strip.save(os.path.join(OUT, "cmp_%s.png" % name))
print("saved to", OUT)
