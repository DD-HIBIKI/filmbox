#!/usr/bin/env python3
"""On-device camera layer self-test (~60s)."""
import io
import os
from pathlib import Path
import sys
import time
import importlib.util
import numpy as np
from PIL import Image

KEEP = "--keep" in sys.argv[1:]
CANDIDATES = (
    Path(__file__).resolve().with_name("filmbox.py"),
    Path("/usr/lib/filmbox/filmbox.py"),
    Path("/usr/lib/cybercam/cybercam.py"),
)
TARGET = next((p for p in CANDIDATES if p.exists()), None)
if TARGET is None:
    raise SystemExit("filmbox.py not found in repo or /usr/lib/filmbox/")

spec = importlib.util.spec_from_file_location("fb", str(TARGET))
C = importlib.util.module_from_spec(spec)
sys.modules["fb"] = C
spec.loader.exec_module(C)


def grab(st, timeout=10):
    seq, jpg = 0, None
    t = time.time()
    while jpg is None and time.time() - t < timeout:
        seq, jpg = st.next_frame(seq, timeout=1)
    return Image.open(io.BytesIO(jpg)).convert("RGB") if jpg else None


def mem():
    for line in open("/proc/meminfo"):
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) // 1024
    return 0


created = []
st = C.CameraStream(rotation=180, fps=25)
try:
    print("== 0. target ==", TARGET, flush=True)
    print("== 1. start ==", flush=True)
    t0 = time.time()
    st.start()
    first = grab(st)
    print("   first frame %.2fs %s" % (time.time() - t0, first.size if first else None), flush=True)
    if first is None:
        raise RuntimeError("Preview failed to produce a first frame")

    print("== 2. preview fps ==", flush=True)
    seq, _ = st.latest()
    n = 0
    t = time.time()
    while time.time() - t < 3:
        s2, j = st.next_frame(seq, timeout=1)
        if j:
            seq = s2
            n += 1
    print("   %.1f fps | mem %d MB" % (n / 3, mem()), flush=True)

    print("== 3. zoom ==", flush=True)
    frames = {}
    for z in C.ZOOMS:
        st.set_zoom(z)
        time.sleep(0.5)
        md = st.picam.capture_metadata()
        frame = grab(st)
        if frame is None:
            raise RuntimeError(f"No preview frame at {z}x zoom")
        frames[z] = np.asarray(frame, dtype=np.float32)
        print("   %sx crop %s applied %s" % (z, st.crop_rect(), md.get("ScalerCrop")), flush=True)
    print("   diff 1x vs 2x: %.1f" % np.abs(frames[1.0] - frames[2.0]).mean(), flush=True)
    st.set_zoom(1.0)

    print("== 4. still 8MP & 1080P ==", flush=True)
    t0 = time.time()
    p = C.reserve_path("Photos", "8mp", "jpg")
    created.append(p)
    st.capture_still(p, None)
    with Image.open(p) as im:
        sz8 = im.size
    print("   8MP %s %.1fs %d KB | mem %d" % (sz8, time.time() - t0, os.path.getsize(p) // 1024, mem()), flush=True)

    p1080 = C.reserve_path("Photos", "photo", "jpg")
    created.append(p1080)
    st.capture_still(p1080, (1920, 1080))
    with Image.open(p1080) as im:
        print("   1080P %s %d KB" % (im.size, os.path.getsize(p1080) // 1024), flush=True)

    print("== 5. zoomed still ==", flush=True)
    st.set_zoom(2.0)
    p2 = C.reserve_path("Photos", "8mp", "jpg")
    created.append(p2)
    st.capture_still(p2, None)
    with Image.open(p2) as im:
        print("   2x 8MP still %s" % (im.size,), flush=True)
    p2_vga = C.reserve_path("Photos", "photo", "jpg")
    created.append(p2_vga)
    st.capture_still(p2_vga, (640, 480))
    with Image.open(p2_vga) as im:
        print("   2x VGA still %s" % (im.size,), flush=True)
    st.set_zoom(1.0)

    print("== 6. record 720p ==", flush=True)
    vid = C.reserve_path("Videos", "video", "mp4")
    created.append(vid)
    t0 = time.time()
    st.start_recording(vid, size=(1280, 720), bitrate=8_000_000)
    print("   start %.2fs" % (time.time() - t0), flush=True)
    time.sleep(1.2)
    seq, _ = st.latest()
    n = 0
    t = time.time()
    while time.time() - t < 3:
        s2, j = st.next_frame(seq, timeout=1)
        if j:
            seq = s2
            n += 1
    print("   preview during REC %.1f fps | mem %d" % (n / 3, mem()), flush=True)
    st.stop_recording()
    if vid.exists():
        print("   mp4 %d KB" % (os.path.getsize(vid) // 1024), flush=True)
    else:
        print("   mp4 MISSING (see camera.log)", flush=True)

    print("== 7. restart again ==", flush=True)
    img2 = grab(st)
    print("   frame after record %s" % (img2.size if img2 else None,), flush=True)
finally:
    st.stop()
    if not KEEP:
        for f in created:
            try:
                Path(f).unlink(missing_ok=True)
            except OSError:
                pass
print("== done | mem %d MB ==" % mem(), flush=True)
