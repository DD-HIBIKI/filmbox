import io, os, time, importlib.util
import numpy as np
from PIL import Image

spec = importlib.util.spec_from_file_location("cc", "/usr/lib/cybercam/cybercam.py")
C = importlib.util.module_from_spec(spec)
spec.loader.exec_module(C)

def grab(st, timeout=10):
    seq, jpg = 0, None
    t = time.time()
    while jpg is None and time.time() - t < timeout:
        seq, jpg = st.next_frame(seq, timeout=1)
    return Image.open(io.BytesIO(jpg)).convert("RGB") if jpg else None

def mem():
    for line in open('/proc/meminfo'):
        if line.startswith('MemAvailable:'):
            return int(line.split()[1]) // 1024

print("== 1. start ==", flush=True)
st = C.CameraStream(rotation=180, fps=25)
t0 = time.time(); st.start()
first = grab(st); print("   first frame %.2fs %s" % (time.time()-t0, first.size if first else None), flush=True)

print("== 2. preview fps ==", flush=True)
seq, _ = st.latest(); n = 0; t = time.time()
while time.time() - t < 3:
    s2, j = st.next_frame(seq, timeout=1)
    if j: seq = s2; n += 1
print("   %.1f fps | mem %d MB" % (n/3, mem()), flush=True)

print("== 3. zoom ==", flush=True)
frames = {}
for z in C.ZOOMS:
    st.set_zoom(z); time.sleep(0.5)
    md = st.picam.capture_metadata()
    frames[z] = np.asarray(grab(st), dtype=np.float32)
    print("   %sx crop %s applied %s" % (z, st.crop_rect(), md.get("ScalerCrop")), flush=True)
print("   diff 1x vs 2x: %.1f" % np.abs(frames[1.0]-frames[2.0]).mean(), flush=True)
st.set_zoom(1.0)

print("== 4. still 8MP ==", flush=True)
t0 = time.time(); p = C.reserve_path("Photos", "8mp", "jpg")
st.capture_still(p, None)
print("   %s %.1fs %d KB | mem %d" % (Image.open(p).size, time.time()-t0, os.path.getsize(p)//1024, mem()), flush=True)

print("== 5. zoomed still ==", flush=True)
st.set_zoom(2.0); p2 = C.reserve_path("Photos", "8mp", "jpg")
st.capture_still(p2, None); print("   2x still %s" % (Image.open(p2).size,), flush=True)
st.set_zoom(1.0)

print("== 6. record 720p ==", flush=True)
vid = C.reserve_path("Videos", "video", "mp4")
t0 = time.time(); st.start_recording(vid, size=(1280, 720), bitrate=8_000_000)
print("   start %.2fs" % (time.time()-t0), flush=True)
time.sleep(1.2)
seq, _ = st.latest(); n = 0; t = time.time()
while time.time() - t < 3:
    s2, j = st.next_frame(seq, timeout=1)
    if j: seq = s2; n += 1
print("   preview during REC %.1f fps | mem %d" % (n/3, mem()), flush=True)
st.stop_recording()
print("   mp4 %d KB" % (os.path.getsize(vid)//1024), flush=True)

print("== 7. restart again ==", flush=True)
img2 = grab(st); print("   frame after record %s" % (img2.size if img2 else None,), flush=True)
st.stop()
print("== done | mem %d MB ==" % mem(), flush=True)
