#!/usr/bin/env python3
"""FilmBox 0.9.0 — film-simulation camera for M5Stack Cardputer Zero.

Fork of CyberCam 0.5.3 by Cybern0mads (MIT).  Adds Picamera2 single-session
camera control, 18 film LUTs, 4-step zoom, full-bleed viewfinder and a
QR-code Wi-Fi gallery.

RGB565 framebuffer + evdev UI; Picamera2 camera layer (stills + MJPEG video).
Timelapse still uses rpicam-still.
No modification to other camera applications is required.
"""
from __future__ import annotations

# Original work Copyright (c) 2026 Cybern0mads (MIT).
# Modifications Copyright (c) 2026 HIBIKI (MIT).

import argparse
import html
import hmac
import mimetypes
import secrets
import tempfile
import urllib.parse
import weakref
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from functools import lru_cache
import array
import ctypes
try:
    import fcntl
except ImportError:
    fcntl = None
import gc
import io
import json
import mmap
import os
from pathlib import Path
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from datetime import datetime
from collections import deque

from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps

try:
    from evdev import InputDevice, ecodes, list_devices
except ImportError:
    InputDevice = None
    ecodes = None
    list_devices = None

VERSION = "0.9.0"
HOME = Path.home()
ROOT = HOME / "Pictures" / "FilmBox"


def _link_or_copy(src, dst):
    """Hardlink media on the same filesystem (zero extra space, instant); copy otherwise."""
    try:
        os.link(src, dst)
    except OSError:
        shutil.copy2(src, dst)


def _atomic_copytree(src, dst, copy_fn=shutil.copy2):
    tmp = dst.with_name("." + dst.name + ".migrating")
    if tmp.exists():
        shutil.rmtree(tmp, ignore_errors=True)
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, tmp, copy_function=copy_fn)
        os.replace(tmp, dst)
    except OSError:
        shutil.rmtree(tmp, ignore_errors=True)


def _migrate_legacy_paths():
    """Migrate settings/LUTs/photos from the pre-0.6 CyberCam layout, once."""
    legacy_cfg = HOME / ".config" / "cybercam"
    new_cfg = HOME / ".config" / "filmbox"
    if legacy_cfg.is_dir() and not new_cfg.exists():
        _atomic_copytree(legacy_cfg, new_cfg, shutil.copy2)
    legacy_photos = HOME / "Pictures" / "CyberCam"
    if legacy_photos.is_dir() and not ROOT.exists():
        _atomic_copytree(legacy_photos, ROOT, _link_or_copy)


_migrate_legacy_paths()
CONFIG = HOME / ".config" / "filmbox" / "settings.json"
LOG = HOME / ".local" / "state" / "filmbox" / "camera.log"
SHUTTER = Path(__file__).resolve().parent / "shutter.wav"
RESOLUTIONS = [(3280, 2464), (1920, 1080), (1640, 1232), (640, 480)]
RESOLUTION_LABELS = ("8MP", "1080P", "2MP", "VGA")
# 1640x1232 uses the entire IMX219 sensor (2x2 binned), unlike the
# high-FPS 640x480 sensor mode which has a substantial center crop.
# Keep widescreen output optional: 16:9 loses top/bottom field of view.
FULL_FOV_MODE = "1640:1232:10:P"   # only used by the rpicam-based timelapse path
FULL_STILL_MODE = "3280:2464:10:P"
VIDEOS = [
    (1280, 960, 30, 8_000_000),   # 4:3 WIDE, default
    (1440, 1080, 30, 12_000_000), # 4:3 WIDE high detail
    (1280, 720, 30, 8_000_000),   # 16:9 (vertical crop)
    (640, 480, 30, 4_000_000),    # low bandwidth, full FoV
]
VIDEO_LABELS = ("WIDE 960P", "WIDE 1080P", "16:9 720P", "WIDE 480P")
BASE_FILTERS = ("NORMAL", "MONO", "GBOY", "GREEN", "4TONE", "PIXEL", "HICON",
                "GAMEBOY+", "VHS1998", "CYBERVISION", "FILM1999", "VIRTUALRED",
                "C64RETRO", "NEONEDGE", "PIXELDREAM", "CYBERGLITCH")
FILTERS = list(BASE_FILTERS)

# --- film LUT support -------------------------------------------------------
# Drop .cube files in ~/.config/filmbox/luts/ (overrides packaged LUTs in /usr/share/filmbox/luts).
LUT_DIR = Path(os.path.expanduser("~/.config/filmbox/luts"))
SYSTEM_LUT_DIRS = (
    LUT_DIR,
    Path("/usr/share/filmbox/luts"),
    Path(__file__).resolve().parent / "luts",
)
LUT_PREFIX = "LUT:"
LUT_SAVE_MAX = (1640, 1232)   # fallback ceiling when RAM is tight
LUT_FULL_RES = True           # full 8MP film copy (~6s background, viewfinder stays live)
PREVIEW_SIZE = (320, 170)     # viewfinder streams at screen size (full-bleed)
SENSOR_SIZE = (3280, 2464)
ZOOMS = (1.0, 1.5, 2.0, 3.0)   # 1x = full sensor field


def available_luts():
    """All .cube files across user/system/repo LUT dirs (user dir wins on duplicate stems)."""
    found = {}
    for folder in SYSTEM_LUT_DIRS:
        try:
            if not folder.is_dir():
                continue
            for cube in sorted(folder.glob("*.cube")):
                found.setdefault(cube.stem, cube)
        except OSError:
            continue
    return [found[k] for k in sorted(found)]


def resolve_lut_path(stem):
    for folder in SYSTEM_LUT_DIRS:
        candidate = folder / (stem + ".cube")
        try:
            if candidate.is_file():
                return candidate
        except OSError:
            continue
    return LUT_DIR / (stem + ".cube")


def refresh_filters():
    global FILTERS
    FILTERS[:] = list(BASE_FILTERS) + [LUT_PREFIX + p.stem for p in available_luts()]
    return FILTERS


refresh_filters()
EXTRA_MODES = ("TIMELAPSE", "STOPMOTION", "BOOMERANG")
MODES = ("PHOTO", "GIF", "VIDEO", "TIMELAPSE", "STOPMOTION", "BOOMERANG")
MODE_BUTTONS = {"PHOTO": "1", "GIF": "2", "VIDEO": "3", "TIMELAPSE": "4"}
MENU_CHOICES = MODES + ("GALLERY",)
TIMELAPSE_INTERVALS = (2, 5, 10, 30, 60, 300, 1800)
# Keep original media; only generated animation/video frames are downscaled for
# a practical FFmpeg memory footprint on this 512 MB RAM handheld.
MOVIE_SIZE = (640, 480)
MAGENTA = (255, 68, 145)
MINT = (111, 240, 205)
PAPER = (230, 241, 242)
# Optional acceleration; package declares python3-numpy on device.
try:
    import numpy as np
except ImportError:
    np = None



def camera_log():
    LOG.parent.mkdir(parents=True, exist_ok=True)
    return open(LOG, "ab", buffering=0)


def timestamp():
    return datetime.now().strftime("%Y%m%d_%H%M%S_%f")[:-3]


COLOUR_FIX_NONE = (1.0, 1.0, 1.0, 0.0)
# (r_mul, g_mul, b_mul, sky_lift): neutralizes IMX219 global red/magenta cast
# (weak IR-cut + AWB bias) plus residual blue-sky violet lift.
COLOUR_FIX_PRESETS = (
    (1.00, 1.00, 1.00, 0.00),  # 0: OFF
    (0.95, 1.03, 1.00, 0.09),  # 1: LOW  (gentle; safest for warm indoor skin)
    (0.91, 1.05, 1.00, 0.14),  # 2: MED  (default; best match vs iPhone 15 Pro)
    (0.87, 1.07, 1.00, 0.19),  # 3: HIGH (strong daylight / heavy IR leakage)
)


def _normalize_colour_fix(spec):
    """Normalize colour-fix spec to (r_mul, g_mul, b_mul, sky_lift)."""
    if spec is None or isinstance(spec, bool):
        return COLOUR_FIX_NONE
    if isinstance(spec, (tuple, list)) and len(spec) == 4:
        r, g, b, sky = (float(x) for x in spec)
        return (
            max(0.5, min(1.5, r)),
            max(0.5, min(1.5, g)),
            max(0.5, min(1.5, b)),
            max(0.0, min(1.0, sky)),
        )
    if isinstance(spec, (int, float)):
        return (1.0, 1.0, 1.0, max(0.0, min(1.0, float(spec))))
    return COLOUR_FIX_NONE


def _colour_fix_active(spec):
    r, g, b, sky = _normalize_colour_fix(spec)
    return sky > 0.0 or abs(r - 1.0) > 1e-4 or abs(g - 1.0) > 1e-4 or abs(b - 1.0) > 1e-4


def colour_fix_spec(settings):
    if not isinstance(settings, dict):
        return COLOUR_FIX_PRESETS[2]
    idx = settings.get("colour_fix", 2)
    if isinstance(idx, bool) or not isinstance(idx, int) or not (0 <= idx < len(COLOUR_FIX_PRESETS)):
        idx = 2
    return COLOUR_FIX_PRESETS[idx]


def resolve_tuning_file():
    """Return an active custom IMX219 libcamera tuning JSON path, or None.

    Lookup priority (VC4 ISP only on Cardputer Zero / CM0):
      1. FILMBOX_TUNING_FILE ("0"/"off"/"none"/"default" forces system default)
      2. LIBCAMERA_RPI_TUNING_FILE
      3. ~/.config/filmbox/imx219.json
      4. <app_dir>/tuning/imx219.json or <app_dir>/imx219.json
      5. /usr/share/filmbox/imx219.json
    Malformed files are logged and ignored so a bad JSON never breaks capture.
    """
    env_fb = os.environ.get("FILMBOX_TUNING_FILE", "").strip()
    if env_fb.lower() in ("0", "off", "none", "default"):
        return None
    candidates = []
    if env_fb:
        candidates.append(Path(env_fb).expanduser())
    env_lc = os.environ.get("LIBCAMERA_RPI_TUNING_FILE", "").strip()
    if env_lc:
        candidates.append(Path(env_lc).expanduser())
    app_dir = Path(__file__).resolve().parent
    candidates.extend((
        CONFIG.parent / "imx219.json",
        app_dir / "tuning" / "imx219.json",
        app_dir / "imx219.json",
        Path("/usr/share/filmbox/imx219.json"),
    ))
    for cand in candidates:
        try:
            if not cand.is_file():
                continue
            data = json.loads(cand.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return cand
            with camera_log() as log:
                log.write(("FilmBox tuning ignored (not a JSON object): %s\n"
                           % cand).encode())
        except Exception as exc:
            with camera_log() as log:
                log.write(("FilmBox tuning ignored (%s): %s\n"
                           % (cand, exc)).encode())
    return None


def _load_picam_tuning(picam_cls):
    """Load custom tuning dict for Picamera2, falling back to None on any error."""
    path = resolve_tuning_file()
    if path is None:
        return None
    try:
        try:
            return picam_cls.load_tuning_file(path.name, dir=str(path.parent))
        except TypeError:
            return picam_cls.load_tuning_file(str(path))
    except Exception as exc:
        with camera_log() as log:
            log.write(("FilmBox tuning load failed (%s): %s\n"
                       % (path, exc)).encode())
        return None


def load_settings():
    refresh_filters()
    defaults = {
        "sound": True, "filter": "NORMAL", "resolution": 0, "video": 0,
        "rotation": 180, "preview_fps": 25, "timelapse_interval": 1,
        "lut_full_res": True, "clean_ui": False, "zoom": 1.0, "colour_fix": 2,
    }
    try:
        values = json.loads(CONFIG.read_text())
        defaults.update({k: values[k] for k in defaults if k in values})
    except (OSError, ValueError, TypeError):
        pass
    if defaults["filter"] not in FILTERS:
        defaults["filter"] = "NORMAL"
    if isinstance(defaults.get("colour_fix"), bool) or not isinstance(
            defaults.get("colour_fix"), int) or not (
            0 <= defaults["colour_fix"] < len(COLOUR_FIX_PRESETS)):
        defaults["colour_fix"] = 2
    if isinstance(defaults.get("timelapse_interval"), bool) or not isinstance(
            defaults.get("timelapse_interval"), int) or not (
            0 <= defaults["timelapse_interval"] < len(TIMELAPSE_INTERVALS)):
        defaults["timelapse_interval"] = 1
    for key, count in (("resolution", len(RESOLUTIONS)), ("video", len(VIDEOS))):
        if isinstance(defaults[key], bool) or not isinstance(defaults[key], int) or not (0 <= defaults[key] < count):
            defaults[key] = 0
    if isinstance(defaults["rotation"], bool) or defaults["rotation"] not in (0, 180):
        defaults["rotation"] = 180
    for key in ("sound", "lut_full_res", "clean_ui"):
        if not isinstance(defaults.get(key), bool):
            defaults[key] = bool(defaults.get(key)) if isinstance(
                defaults.get(key), (int, float)) else False
    raw_zoom = defaults.get("zoom", ZOOMS[0])
    if isinstance(raw_zoom, bool) or not isinstance(raw_zoom, (int, float)):
        defaults["zoom"] = ZOOMS[0]
    elif float(raw_zoom) not in ZOOMS:
        defaults["zoom"] = ZOOMS[0]
    else:
        defaults["zoom"] = float(raw_zoom)
    if isinstance(defaults["preview_fps"], bool) or defaults["preview_fps"] not in (12, 15, 20, 24, 25, 30):
        defaults["preview_fps"] = 25
    return defaults


def save_settings(values):
    CONFIG.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG.with_suffix(".json.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(values, indent=2) + "\n")
            fh.flush()
            os.fsync(fh.fileno())    # rename is atomic, but the data must land
        os.replace(tmp, CONFIG)
        dir_fd = os.open(str(CONFIG.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def click(sound=True):
    if sound and SHUTTER.exists() and shutil.which("aplay"):
        try:
            subprocess.Popen(["aplay", "-q", str(SHUTTER)],
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError:
            pass


def run_command(command, timeout=45):
    with camera_log() as log:
        result = subprocess.run(command, stdout=log, stderr=log, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"Camera command failed (exit {result.returncode}); see {LOG}")


def reserve_path(subfolder, kind, extension):
    directory = ROOT / subfolder
    directory.mkdir(parents=True, exist_ok=True)
    p = directory / f"cyber_{timestamp()}_{kind}.{extension}"
    i = 1
    while p.exists():
        p = directory / f"cyber_{timestamp()}_{kind}_{i}.{extension}"
        i += 1
    return p


def preview_size(img, size):
    return ImageOps.pad(img.convert("RGB"), size, method=Image.Resampling.BILINEAR,
                        color=(14, 20, 31))


def blend_rect(image, box, colour, alpha):
    """Translucent bar over whatever is already drawn (top/bottom info strips)."""
    x0, y0, x1, y1 = box
    strip = image.crop((x0, y0, x1 + 1, y1 + 1))
    image.paste(Image.blend(strip, Image.new("RGB", strip.size, colour), alpha),
                (x0, y0))


def fill_size(img, size):
    """Crop-to-fill (no letterbox) for the full-bleed viewfinder."""
    return ImageOps.fit(img.convert("RGB"), size, method=Image.Resampling.BILINEAR)


def _palette(colors):
    palette = Image.new("P", (1, 1))
    values = [c for color in colors for c in color]
    palette.putpalette(values + [0] * (768 - len(values)))
    return palette


GREEN_COLORS = ((22, 36, 32), (83, 117, 101), (164, 190, 155), (227, 242, 210))
GRAY_COLORS = ((20, 24, 32), (89, 99, 112), (173, 184, 190), (243, 246, 241))
GREEN_PALETTE = _palette(GREEN_COLORS)
GRAY_PALETTE = _palette(GRAY_COLORS)


_LUT_CACHE = {}
_LUT_MONO = {}
_LUT_CACHE_MAX = 4


def _cache_put(cache, key, value, limit=_LUT_CACHE_MAX):
    if key in cache:
        cache.pop(key, None)
    elif len(cache) >= limit:
        cache.pop(next(iter(cache)), None)
    cache[key] = value


_LIBC = None


def reclaim_memory():
    """Give freed Pillow buffers back to the kernel.

    CPython + glibc keep released 24 MB image buffers inside the heap, so a
    burst of shots slowly starves the CMA allocator. malloc_trim(0) releases
    that free arena memory immediately.
    """
    global _LIBC
    gc.collect()
    try:
        if _LIBC is None:
            _LIBC = ctypes.CDLL("libc.so.6")
        _LIBC.malloc_trim(0)
    except Exception:
        pass


def mem_snapshot():
    """One-line memory report used in logs when something runs out of RAM."""
    keys = ("MemAvailable", "MemFree", "Cached", "Dirty", "CmaFree", "SwapFree")
    got = {}
    try:
        with open("/proc/meminfo") as handle:
            for line in handle:
                name = line.split(":")[0]
                if name in keys:
                    got[name] = int(line.split()[1]) // 1024
    except OSError:
        return "meminfo unavailable"
    return " ".join("%s=%dMB" % (k.replace("Mem", ""), v) for k, v in got.items())


def available_mb():
    """MemAvailable in MB (Linux); assume plenty when unreadable."""
    try:
        with open("/proc/meminfo") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return 512


def load_cube_lut(path):
    """Parse a .cube into a Pillow Color3DLUT (cached). Pillow's table order
    matches .cube (red changing fastest), so values pass through verbatim."""
    try:
        stamp = os.path.getmtime(path)
    except OSError:
        stamp = 0
    key = (path, stamp)
    if key in _LUT_CACHE:
        return _LUT_CACHE[key]
    size, vals = None, []
    dmin, dmax = (0.0, 0.0, 0.0), (1.0, 1.0, 1.0)
    with open(path, errors="ignore") as handle:
        for line in handle:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            u = s.upper()
            if u.startswith("LUT_3D_SIZE"):
                size = int(s.split()[1])
                continue
            if u.startswith("LUT_1D_SIZE"):
                raise ValueError("1D LUT not supported")
            if u.startswith("DOMAIN_MIN"):
                dmin = tuple(float(x) for x in s.split()[1:4])
                continue
            if u.startswith("DOMAIN_MAX"):
                dmax = tuple(float(x) for x in s.split()[1:4])
                continue
            if u[0].isalpha():
                continue
            try:
                vals += [float(x) for x in s.split()]
            except ValueError:
                continue
    if size is None or len(vals) != size ** 3 * 3:
        raise ValueError("bad cube: " + path)
    if dmin != (0.0, 0.0, 0.0) or dmax != (1.0, 1.0, 1.0):
        norm = []
        for i in range(0, len(vals), 3):
            for c in range(3):
                span = (dmax[c] - dmin[c]) or 1.0
                norm.append(min(1.0, max(0.0, (vals[i + c] - dmin[c]) / span)))
        vals = norm
    mono = all(
        abs(vals[i] - vals[i + 1]) < 1e-3 and abs(vals[i + 1] - vals[i + 2]) < 1e-3
        for i in range(0, len(vals), 3)
    )
    _LUT_MONO[key] = mono
    lut = ImageFilter.Color3DLUT(size, vals)
    _cache_put(_LUT_CACHE, key, lut)
    return lut


def is_monochrome_cube(path):
    path = str(path)
    try:
        stamp = os.path.getmtime(path)
    except OSError:
        stamp = 0
    key = (path, stamp)
    if key not in _LUT_MONO:
        load_cube_lut(path)
    return bool(_LUT_MONO.get(key, False))


def _is_grayscale_image(small_rgb):
    if np is not None:
        arr = np.asarray(small_rgb, dtype=np.int16)
        return bool(np.max(np.abs(arr[:, :, 0] - arr[:, :, 1])) <= 1
                    and np.max(np.abs(arr[:, :, 1] - arr[:, :, 2])) <= 1)
    ext = small_rgb.getextrema()
    return ext[0] == ext[1] == ext[2]


def add_halation(image, threshold=210, strength=0.6, monochrome=None):
    """Highlight glow. Built at 1/4 scale, blended per strip (8MP-safe, fast)."""
    rgb = image if image.mode == "RGB" else image.convert("RGB")
    glow = build_glow(rgb, threshold=threshold, strength=strength, monochrome=monochrome)
    w, h = rgb.size
    sw, sh = glow.size
    step = 288
    for top in range(0, h, step):
        bottom = min(h, top + step)
        gy0 = top * sh // h
        gy1 = min(sh, max(gy0 + 1, bottom * sh // h))
        band = glow.crop((0, gy0, sw, gy1)).resize((w, bottom - top),
                                                   Image.Resampling.NEAREST)
        rgb.paste(ImageChops.screen(rgb.crop((0, top, w, bottom)), band), (0, top))
    return rgb


def add_grain(image, strength=5.0, seed=None):
    """Luma-weighted grain in integer maths.

    The weight map is built once at 1/8 scale and resized a band at a time;
    everything else stays int16/int32 so an 8 MP pass costs ~4 s → ~1.5 s.
    """
    rgb = image if image.mode == "RGB" else image.convert("RGB")
    if np is None:
        return rgb
    w, h = rgb.size
    rng = np.random.default_rng(seed)
    small = rgb.resize((max(1, w // 8), max(1, h // 8)), Image.Resampling.BOX)
    lw = np.asarray(small, dtype=np.uint16)
    luma = (lw[:, :, 0] * 77 + lw[:, :, 1] * 150 + lw[:, :, 2] * 29) >> 8   # 0..255
    weight = np.clip(345 - (luma.astype(np.int32) * 3) // 2, 38, 255).astype(np.uint8)
    wimg = Image.fromarray(weight, "L")
    amp = max(1, int(round(strength)))         # grey levels at full weight
    step = 288
    for top in range(0, h, step):
        bottom = min(h, top + step)
        rows = bottom - top
        block = np.asarray(rgb.crop((0, top, w, bottom)), dtype=np.int16)
        # Two independent draws averaged: gaussian-ish, no directional bias.
        # (A 3-tap average with np.roll leaves visible horizontal streaks.)
        n = rng.integers(-120, 121, (rows, w), dtype=np.int16)
        n += rng.integers(-120, 121, (rows, w), dtype=np.int16)
        n //= 2                                                   # ±120
        wt = np.asarray(wimg.resize((w, rows), Image.Resampling.BILINEAR), dtype=np.int32)
        # weight is 0..255 and the amplitude is in 1/100 grey levels, so the
        # result stays within ±(120*strength/100) = ±6 for the default 5.
        grain = (((n.astype(np.int32) * wt) >> 8) * amp) // 100
        block += grain.astype(np.int16)[:, :, None]
        rgb.paste(Image.fromarray(np.clip(block, 0, 255).astype(np.uint8), "RGB"), (0, top))
    return rgb


def filter_image(source, mode, animated=False, preview=False, colour_fix=COLOUR_FIX_NONE):
    """Efficient retro effects. Still photos preserve original dimensions/ratio.

    Pixel/detail deliberately reduced for vintage look; resizing doesn't add detail.
    """
    cf = _normalize_colour_fix(colour_fix)
    cf_on = _colour_fix_active(cf)
    if mode.startswith(LUT_PREFIX):
        # 3D LUT: full resolution, no downscale. Preview frames are already LCD-size.
        try:
            lut = load_cube_lut(str(resolve_lut_path(mode[len(LUT_PREFIX):])))
            rgb = source.convert("RGB")
            if cf_on:
                _fix_magenta(rgb, cf)
            return rgb.filter(lut)
        except (OSError, ValueError):
            rgb = source.convert("RGB")
            return _fix_magenta(rgb, cf) if cf_on else rgb

    if mode == "NORMAL":
        if animated:
            out = preview_size(source, (240, 176))
        else:
            out = source.convert("RGB")
        return _fix_magenta(out, cf) if cf_on else out
    if mode in ("MONO", "HICON"):
        working = preview_size(source, (240, 176)) if animated else source.convert("RGB")
        if cf_on:
            _fix_magenta(working, cf)
        gray = ImageOps.autocontrast(working.convert("L"))
        if mode == "HICON":
            gray = ImageEnhance.Contrast(gray).enhance(2.0)
        return gray.convert("RGB")
    if mode in ("GAMEBOY+", "VHS1998", "CYBERVISION", "FILM1999", "VIRTUALRED",
                "C64RETRO", "NEONEDGE", "PIXELDREAM", "CYBERGLITCH"):
        # Bound working size before running effects to avoid 8MP RGB tripling
        # memory consumption. The resulting still PNG retains original dimensions.
        original_size = source.size
        if animated:
            base = preview_size(source, (240, 176))
        elif preview:
            base = source.convert("RGB")
        else:
            scale = min(1.0, 820/source.width, 616/source.height)
            base = source.convert("RGB").resize((max(1, int(source.width*scale)),
                max(1, int(source.height*scale))), Image.Resampling.BOX)
        if cf_on:
            base = _fix_magenta_band(base, cf)
        out = creative_filter(base, mode)
        if not animated and not preview and out.size != original_size:
            out = out.resize(original_size, Image.Resampling.NEAREST)
        return out
    if mode not in ("GBOY", "GREEN", "4TONE", "PIXEL"):
        raise ValueError(f"Unknown filter {mode}")

    original_size = source.size
    if animated:
        target = (240, 176)
        base = preview_size(source, target)
    elif preview:
        target = source.size
        base = source.convert("RGB")
    else:
        # 4x block-dither on an 8 MP JPEG (820x616) is practical on 512 MB RAM.
        target = (min(source.width, 820), min(source.height, 616))
        base = source.resize(target, Image.Resampling.BOX).convert("RGB")
    if cf_on:
        base = _fix_magenta_band(base, cf)

    if mode == "PIXEL":
        thumb = base.resize((max(1, target[0] // 4), max(1, target[1] // 4)), Image.Resampling.BOX)
        out = thumb.quantize(colors=16, dither=Image.Dither.NONE).convert("RGB").resize(
            target, Image.Resampling.NEAREST)
    else:
        gray = ImageOps.autocontrast(base.convert("L"))
        if mode == "GBOY":
            out = ImageEnhance.Contrast(gray).enhance(1.35).convert(
                "1", dither=Image.Dither.FLOYDSTEINBERG).convert("RGB")
        else:
            out = gray.convert("RGB").quantize(
                palette=GREEN_PALETTE if mode == "GREEN" else GRAY_PALETTE,
                dither=Image.Dither.FLOYDSTEINBERG).convert("RGB")
    if not animated and not preview and out.size != original_size:
        out = out.resize(original_size, Image.Resampling.NEAREST)
    return out



@lru_cache(maxsize=4)
def film_vignette(w,h):
    mask = Image.new("L",(w,h),0)
    px = mask.load()
    for y in range(h):
        yy = ((y-h/2)/(h/2))**2
        for x in range(w):
            v = ((x-w/2)/(w/2))**2 + yy
            px[x,y] = max(100, min(255, int(255-75*v)))
    return mask


def creative_filter(base, mode):
    """Retro/cyber looks processed at bounded sizes; no dependence on OpenCV."""
    from PIL import ImageFilter, ImageChops
    rgb = base.convert("RGB")
    w, h = rgb.size
    gray = ImageOps.autocontrast(rgb.convert("L"))
    if mode == "GAMEBOY+":
        boosted = ImageEnhance.Contrast(gray).enhance(1.7)
        pal = ((12, 31, 27), (56, 96, 66), (142, 177, 107), (220, 242, 174))
        quant = boosted.convert("RGB").quantize(palette=_palette(pal),
                            dither=Image.Dither.FLOYDSTEINBERG).convert("RGB")
        return quant
    if mode == "VIRTUALRED":
        levels = gray.point(lambda n: (n//64)*85)
        return Image.merge("RGB", (levels, levels.point(lambda n:n//12), levels.point(lambda n:n//18)))
    if mode == "C64RETRO":
        colors = [(0,0,0), (255,255,255), (136,0,0), (170,255,238),
                  (204,68,204), (0,204,85), (0,0,170), (238,238,119),
                  (221,136,85), (102,68,0), (255,119,119), (51,51,51),
                  (119,119,119), (170,255,102), (0,136,255), (187,187,187)]
        small = rgb.resize((max(1,w//3), max(1,h//3)), Image.Resampling.BOX)
        return small.quantize(palette=_palette(colors), dither=Image.Dither.FLOYDSTEINBERG).convert("RGB").resize((w,h), Image.Resampling.NEAREST)
    if mode == "PIXELDREAM":
        small = rgb.resize((max(1,w//5),max(1,h//5)), Image.Resampling.BOX)
        small = ImageEnhance.Color(small).enhance(1.8)
        return small.quantize(colors=24, dither=Image.Dither.NONE).convert("RGB").resize((w,h), Image.Resampling.NEAREST)
    if mode == "CYBERVISION":
        # Stylized false color, not thermal or infrared sensor data.
        ramp = [(5,12,27), (0,65,100), (0,215,230), (112,62,217), (243,74,184), (248,232,150)]
        lut = [ramp[min(5,i//43)] for i in range(256)]
        return Image.merge("RGB", tuple(gray.point([c[k] for c in lut]) for k in range(3)))
    if mode == "NEONEDGE":
        edges = ImageOps.autocontrast(gray.filter(ImageFilter.FIND_EDGES))
        edge = ImageEnhance.Contrast(edges).enhance(2.5)
        return Image.merge("RGB", (edge.point(lambda n: n//3), edge,
                     edge.point(lambda n: min(255, 65+n//2))))
    if mode == "FILM1999":
        # Gentle warming; vignette mask cached so it isn't regenerated at 15 FPS.
        from PIL import ImageChops
        r,g,b = rgb.split()
        warm = Image.merge("RGB", (r.point(lambda n:min(255,int(n*1.06+5))),
                       g.point(lambda n:min(255,int(n*1.02))), b.point(lambda n:int(n*.85))))
        tint = film_vignette(w,h)
        return Image.merge("RGB", [ImageChops.multiply(ch,tint) for ch in warm.split()])
    if mode == "VHS1998":
        out = rgb.copy()
        r,g,b = out.split()
        # chromatic channel offset, subtle dark scanlines and tape border
        r = ImageChops.offset(r, 2, 0)
        b = ImageChops.offset(b, -2, 0)
        out = Image.merge('RGB',(r,g,b))
        draw = ImageDraw.Draw(out)
        for y in range(0,h,4):
            draw.line((0,y,w,y),fill=(16,24,34),width=1)
        return out
    if mode == "CYBERGLITCH":
        # A deterministic stripe displacement; animated frames vary as input varies.
        out = rgb.copy()
        r,g,b = out.split()
        out = Image.merge('RGB', (ImageChops.offset(r,3,0),g,ImageChops.offset(b,-3,0)))
        for y in range(7,h,31):
            band = out.crop((0,y,w,min(h,y+3)))
            out.paste(ImageChops.offset(band,(y*11)%21-10,0),(0,y))
        return out
    raise ValueError('Unknown creative filter: ' + mode)



# =============================================================================
# Film-copy accelerators: GPU (EGL/GLES2) and multi-core (fork).
# Selected at runtime by FILMBOX_ACCEL=auto|gpu|multi|cpu.
# =============================================================================
_CUBE_CACHE = {}


def read_cube(path):
    """(size, bytes RGB8, float array) for a .cube file, cached by mtime."""
    path = str(path)
    stamp = os.path.getmtime(path)
    key = (path, stamp)
    hit = _CUBE_CACHE.get(key)
    if hit:
        return hit
    size, vals = None, []
    dmin, dmax = (0.0, 0.0, 0.0), (1.0, 1.0, 1.0)
    with open(path, errors="ignore") as handle:
        for line in handle:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            u = s.upper()
            if u.startswith("LUT_3D_SIZE"):
                size = int(s.split()[1])
                continue
            if u.startswith("DOMAIN_MIN"):
                dmin = tuple(float(x) for x in s.split()[1:4])
                continue
            if u.startswith("DOMAIN_MAX"):
                dmax = tuple(float(x) for x in s.split()[1:4])
                continue
            if u[0].isalpha():
                continue
            try:
                vals += [float(x) for x in s.split()]
            except ValueError:
                pass
    if size is None or len(vals) != size ** 3 * 3:
        raise ValueError("bad cube: " + path)
    if dmin != (0.0, 0.0, 0.0) or dmax != (1.0, 1.0, 1.0):
        norm = []
        for i in range(0, len(vals), 3):
            for c in range(3):
                span = (dmax[c] - dmin[c]) or 1.0
                norm.append(min(1.0, max(0.0, (vals[i + c] - dmin[c]) / span)))
        vals = norm
    if np is not None:
        arr = np.array(vals, dtype=np.float32).reshape(size, size, size, 3)
        data = (np.clip(arr, 0, 1) * 255 + 0.5).astype(np.uint8).tobytes()
        mono = bool(np.max(np.ptp(arr, axis=-1)) < 1e-3)
    else:
        arr = None
        data = bytes(min(255, max(0, int(v * 255 + 0.5))) for v in vals)
        mono = all(
            abs(vals[i] - vals[i + 1]) < 1e-3 and abs(vals[i + 1] - vals[i + 2]) < 1e-3
            for i in range(0, len(vals), 3)
        )
    _LUT_MONO[key] = mono
    out = (size, data, arr)
    _cache_put(_CUBE_CACHE, key, out)
    return out


# ------------------------------------------------------------- glow (shared)
def build_glow(image, threshold=210, strength=0.6, monochrome=None):
    """1/4-scale highlight glow. Neutral silver bloom on B&W stocks, warm halation on color."""
    rgb = image if image.mode == "RGB" else image.convert("RGB")
    w, h = rgb.size
    sw, sh = max(1, w // 4), max(1, h // 4)
    small = rgb.resize((sw, sh), Image.Resampling.BOX)
    if monochrome is None:
        monochrome = _is_grayscale_image(small)

    def ramp(v):
        if v < threshold:
            return 0
        return min(255, int((v - threshold) * 255 // (255 - threshold)))

    mask = small.convert("L").point(ramp)
    if monochrome:
        glow = Image.merge("RGB", (mask, mask, mask))
    else:
        glow = Image.merge("RGB", (mask,
                                   mask.point(lambda v: int(v * 0.72)),
                                   mask.point(lambda v: int(v * 0.42))))
    glow = glow.filter(ImageFilter.GaussianBlur(max(1.0, max(w, h) / 3200.0)))
    if strength != 1.0:
        glow = glow.point(lambda v: int(v * strength))
    return glow


# --------------------------------------------------------------------- GPU
GL_RGB, GL_RGBA, GL_UBYTE = 0x1907, 0x1908, 0x1401
GL_TEX2D, GL_NEAREST, GL_LINEAR, GL_CLAMP = 0x0DE1, 0x2600, 0x2601, 0x812F
GL_MIN_F, GL_MAG_F = 0x2801, 0x2800
GL_WRAP_S, GL_WRAP_T = 0x2802, 0x2803
GL_FBO, GL_COLOR_ATT, GL_FBO_OK = 0x8D40, 0x8CE0, 0x8CD5
GL_FLOAT, GL_STRIP = 0x1406, 0x0005
GL_TEXTURE0, GL_TEXTURE1 = 0x84C0, 0x84C1
GL_TEXTURE2 = 0x84C2

VS = """#version 100
attribute vec2 pos; varying vec2 vUv;
void main(){ vUv = pos * 0.5 + 0.5; gl_Position = vec4(pos, 0.0, 1.0); }"""

FS = """#version 100
precision highp float;
varying vec2 vUv;
uniform sampler2D tex;
uniform sampler2D lut;
uniform sampler2D glow;
uniform vec2 blockSize;      // active pixels in this tile (cols, rows)
uniform vec2 blockOrigin;    // tile offset in the full image, pixels
uniform vec2 imgSize;
uniform float n;             // LUT edge (13 or 33)
uniform float grainAmp;      // normalized grey amplitude (0..1)
uniform float glowMix;       // 0 disables halation
uniform vec4 colourFix;      // (rMul, gMul, bMul, skyLift); (1,1,1,0) disables
uniform vec2 uvScale;        // active content ratio vs allocated tile texture

float hash(vec2 p){
  vec2 q = mod(floor(p), 251.0);
  return fract((q.x * 17.31 + q.y * 43.17) * (q.x * 0.13 + q.y * 0.07 + 1.13));
}

void main(){
  vec2 uv = vUv * uvScale;
  vec3 c = texture2D(tex, uv).rgb;
  c = clamp(c * colourFix.rgb, 0.0, 1.0);
  c.g = min(1.0, c.g + max(0.0, c.b - c.g) * colourFix.w);

  // 3D LUT from a 2D atlas: x = b*n + r, y = g
  float b = clamp(c.b, 0.0, 1.0) * (n - 1.0);
  float b0 = floor(b);
  float b1 = min(n - 1.0, b0 + 1.0);
  float mb = b - b0;
  vec2 rg = vec2(c.r * (n - 1.0) + 0.5, c.g * (n - 1.0) + 0.5);
  vec3 s0 = texture2D(lut, vec2((b0 * n + rg.x) / (n * n), rg.y / n)).rgb;
  vec3 s1 = texture2D(lut, vec2((b1 * n + rg.x) / (n * n), rg.y / n)).rgb;
  vec3 col = mix(s0, s1, mb);

  // halation: screen with a 1/4-scale glow texture
  if (glowMix > 0.0) {
    vec2 g = (blockOrigin + vUv * blockSize) / imgSize;
    vec3 gl = texture2D(glow, g).rgb * glowMix;
    col = 1.0 - (1.0 - col) * (1.0 - gl);
  }

  // grain: full-image pixel coordinates keep tiles seamless and VC4-FP16 safe
  if (grainAmp > 0.0) {
    vec2 px = blockOrigin + vUv * blockSize;
    float n1 = (hash(px) + hash(px + vec2(17.0, 31.0))) * 0.5 * 2.0 - 1.0;  // ±1 triangular
    float luma = dot(col, vec3(0.299, 0.587, 0.114));
    float w = clamp(1.35 - luma * 1.5, 0.15, 1.0);
    col += n1 * grainAmp * w;
  }
  gl_FragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}"""


FS_SIMPLE = """#version 100
precision highp float;
varying vec2 vUv;
uniform sampler2D tex;
void main(){ gl_FragColor = vec4(texture2D(tex, vUv).rgb, 1.0); }"""


class GpuFilm:
    """Lazy EGL/GLES2 renderer. Construction raises on unsupported devices."""

    def __init__(self):
        os.environ.setdefault("EGL_PLATFORM", "surfaceless")
        egl = ctypes.CDLL("libEGL.so.1")
        gl = ctypes.CDLL("libGLESv2.so.2")
        egl.eglGetDisplay.restype = ctypes.c_void_p
        egl.eglQueryString.restype = ctypes.c_char_p
        egl.eglMakeCurrent.argtypes = [ctypes.c_void_p, ctypes.c_void_p,
                                       ctypes.c_void_p, ctypes.c_void_p]
        egl.eglMakeCurrent.restype = ctypes.c_uint
        gl.glGetString.restype = ctypes.c_char_p

        dpy = ctypes.c_void_p(egl.eglGetDisplay(ctypes.c_void_p(0)))
        maj, mnr = ctypes.c_int(), ctypes.c_int()
        if not egl.eglInitialize(dpy, ctypes.byref(maj), ctypes.byref(mnr)):
            raise RuntimeError("eglInitialize")
        attrs = (ctypes.c_int * 5)(0x3040, 0x0004, 0x3038, 0x3038, 0x3038)
        cfg, num = ctypes.c_void_p(), ctypes.c_int()
        if not egl.eglChooseConfig(dpy, attrs, ctypes.byref(cfg), 1, ctypes.byref(num)):
            raise RuntimeError("eglChooseConfig")
        egl.eglBindAPI(0x30A0)
        ctx = ctypes.c_void_p(egl.eglCreateContext(
            dpy, cfg, ctypes.c_void_p(0), (ctypes.c_int * 3)(0x3098, 2, 0x3038)))
        if not ctx.value:
            raise RuntimeError("eglCreateContext")
        if not egl.eglMakeCurrent(dpy, ctypes.c_void_p(0), ctypes.c_void_p(0), ctx):
            raise RuntimeError("eglMakeCurrent")
        self.egl, self.gl = egl, gl
        self.dpy, self.ctx = dpy, ctx
        try:
            u, i, f, v = ctypes.c_uint, ctypes.c_int, ctypes.c_float, ctypes.c_void_p
            gl.glGenTextures.argtypes = [i, ctypes.POINTER(u)]
            gl.glDeleteTextures.argtypes = [i, ctypes.POINTER(u)]
            gl.glTexImage2D.argtypes = [u, i, i, i, i, i, u, u, v]
            gl.glTexSubImage2D.argtypes = [u, i, i, i, i, i, u, u, v]
            gl.glReadPixels.argtypes = [i, i, i, i, u, u, v]
            gl.glDrawArrays.argtypes = [u, i, i]
            gl.glViewport.argtypes = [i, i, i, i]
            gl.glBindTexture.argtypes = [u, u]
            gl.glBindFramebuffer.argtypes = [u, u]
            gl.glFramebufferTexture2D.argtypes = [u, u, u, u, i]
            gl.glCheckFramebufferStatus.argtypes = [u]
            gl.glCheckFramebufferStatus.restype = u
            gl.glTexParameteri.argtypes = [u, u, i]
            gl.glPixelStorei.argtypes = [u, i]
            gl.glActiveTexture.argtypes = [u]
            gl.glUniform1i.argtypes = [i, i]
            gl.glUniform1f.argtypes = [i, f]
            gl.glUniform2f.argtypes = [i, f, f]
            gl.glUniform4f.argtypes = [i, f, f, f, f]
            gl.glGetUniformLocation.argtypes = [u, ctypes.c_char_p]
            gl.glGetUniformLocation.restype = i
            maxt = ctypes.c_int()
            gl.glGetIntegerv(0x0D33, ctypes.byref(maxt))
            self.maxt = maxt.value or 2048
            self.tile = min(self.maxt, 512)
            self.renderer = (gl.glGetString(0x1F01) or b"").decode(errors="ignore")
            self.prog = self._program()
            self.lut_tex = None
            self.lut_key = None
            self._lut_alloc_size = None
            self.glow_tex = None
            self._glow_ref = None
            self._glow_alloc_size = None
            self.src_tex, self.dst_tex, self.fb = ctypes.c_uint(), ctypes.c_uint(), ctypes.c_uint()
            gl.glGenTextures(1, ctypes.byref(self.src_tex))
            gl.glGenTextures(1, ctypes.byref(self.dst_tex))
            gl.glGenFramebuffers(1, ctypes.byref(self.fb))

            # Pre-allocate distinct source (RGB) and render-target (RGBA) tile textures
            # so we never sample and render into the same texture (GLES2 feedback loop).
            gl.glActiveTexture(GL_TEXTURE0)
            gl.glBindTexture(GL_TEX2D, self.src_tex)
            gl.glTexParameteri(GL_TEX2D, GL_MIN_F, GL_NEAREST)
            gl.glTexParameteri(GL_TEX2D, GL_MAG_F, GL_NEAREST)
            gl.glTexParameteri(GL_TEX2D, GL_WRAP_S, GL_CLAMP)
            gl.glTexParameteri(GL_TEX2D, GL_WRAP_T, GL_CLAMP)
            gl.glPixelStorei(0x0CF5, 1)
            gl.glTexImage2D(GL_TEX2D, 0, GL_RGB, self.tile, self.tile, 0, GL_RGB, GL_UBYTE, None)

            gl.glBindTexture(GL_TEX2D, self.dst_tex)
            gl.glTexParameteri(GL_TEX2D, GL_MIN_F, GL_NEAREST)
            gl.glTexParameteri(GL_TEX2D, GL_MAG_F, GL_NEAREST)
            gl.glTexParameteri(GL_TEX2D, GL_WRAP_S, GL_CLAMP)
            gl.glTexParameteri(GL_TEX2D, GL_WRAP_T, GL_CLAMP)
            gl.glTexImage2D(GL_TEX2D, 0, GL_RGBA, self.tile, self.tile, 0, GL_RGBA, GL_UBYTE, None)
            gl.glBindFramebuffer(GL_FBO, self.fb)
            gl.glFramebufferTexture2D(GL_FBO, GL_COLOR_ATT, GL_TEX2D, self.dst_tex, 0)
            if gl.glCheckFramebufferStatus(GL_FBO) != GL_FBO_OK:
                raise RuntimeError("incomplete FBO")

            self.uniforms = {}
            for name in (b"tex", b"lut", b"glow", b"blockSize", b"blockOrigin",
                         b"imgSize", b"n", b"grainAmp", b"glowMix", b"colourFix", b"uvScale"):
                self.uniforms[name.decode()] = gl.glGetUniformLocation(self.prog, name)
            quad = (ctypes.c_float * 8)(-1, -1, 1, -1, -1, 1, 1, 1)
            self._quad = quad
            self.pos_loc = gl.glGetAttribLocation(self.prog, b"pos")
            gl.glUseProgram(self.prog)
            gl.glVertexAttribPointer(self.pos_loc, 2, GL_FLOAT, 0, 0, quad)
            gl.glEnableVertexAttribArray(self.pos_loc)
            gl.glUniform1i(self.uniforms["tex"], 0)
            gl.glUniform1i(self.uniforms["lut"], 1)
            gl.glUniform1i(self.uniforms["glow"], 2)
        finally:
            egl.eglMakeCurrent(dpy, ctypes.c_void_p(0), ctypes.c_void_p(0), ctypes.c_void_p(0))

    def _make_current(self):
        if not self.egl.eglMakeCurrent(self.dpy, ctypes.c_void_p(0), ctypes.c_void_p(0), self.ctx):
            raise RuntimeError("eglMakeCurrent worker")

    def _release_current(self):
        self.egl.eglMakeCurrent(self.dpy, ctypes.c_void_p(0), ctypes.c_void_p(0), ctypes.c_void_p(0))

    def _program(self):
        gl = self.gl

        def shader(kind, code):
            s = gl.glCreateShader(kind)
            arr = (ctypes.c_char_p * 1)(ctypes.c_char_p(code.encode()))
            gl.glShaderSource(s, 1, arr, None)
            gl.glCompileShader(s)
            ok = ctypes.c_int()
            gl.glGetShaderiv(s, 0x8B81, ctypes.byref(ok))
            if not ok.value:
                log = ctypes.create_string_buffer(2048)
                gl.glGetShaderInfoLog(s, 2048, None, log)
                raise RuntimeError("shader: " + log.value.decode(errors="ignore"))
            return s

        prog = gl.glCreateProgram()
        gl.glAttachShader(prog, shader(0x8B31, VS))
        gl.glAttachShader(prog, shader(0x8B30,
                                       FS_SIMPLE if os.environ.get("FB_SIMPLE_FS") else FS))
        gl.glLinkProgram(prog)
        status = ctypes.c_int()
        gl.glGetProgramiv(prog, 0x8B82, ctypes.byref(status))
        if not status.value:
            log = ctypes.create_string_buffer(2048)
            gl.glGetProgramInfoLog(prog, 2048, None, log)
            raise RuntimeError("link: " + log.value.decode(errors="ignore"))
        return prog

    def _upload_glow(self, glow):
        gl = self.gl
        if glow is None:
            return
        if self._glow_ref is not None and self._glow_ref() is glow:
            return
        size_wh = (glow.width, glow.height)
        raw = glow.tobytes()
        gl.glActiveTexture(GL_TEXTURE2)
        gl.glPixelStorei(0x0CF5, 1)
        if self.glow_tex is not None and self._glow_alloc_size == size_wh:
            gl.glBindTexture(GL_TEX2D, self.glow_tex)
            gl.glTexSubImage2D(GL_TEX2D, 0, 0, 0, size_wh[0], size_wh[1],
                               GL_RGB, GL_UBYTE, ctypes.c_char_p(raw))
        else:
            if self.glow_tex is not None:
                gl.glDeleteTextures(1, ctypes.byref(self.glow_tex))
            self.glow_tex = ctypes.c_uint()
            gl.glGenTextures(1, ctypes.byref(self.glow_tex))
            gl.glBindTexture(GL_TEX2D, self.glow_tex)
            for p in (GL_MIN_F, GL_MAG_F):
                gl.glTexParameteri(GL_TEX2D, p, GL_LINEAR)
            for p in (GL_WRAP_S, GL_WRAP_T):
                gl.glTexParameteri(GL_TEX2D, p, GL_CLAMP)
            gl.glTexImage2D(GL_TEX2D, 0, GL_RGB, size_wh[0], size_wh[1], 0,
                            GL_RGB, GL_UBYTE, ctypes.c_char_p(raw))
            self._glow_alloc_size = size_wh
        self._glow_ref = weakref.ref(glow)

    def apply_band(self, block, top, w, h, cube_path, glow, grain=5.0,
                   colour_fix=COLOUR_FIX_NONE):
        """Render one horizontal band with the shared LUT and glow textures."""
        with _ACCEL_LOCK:
            self._make_current()
            try:
                return self._render_tiles(block, top, w, h, cube_path, glow, grain,
                                          colour_fix=colour_fix)
            finally:
                self._release_current()

    def _render_tiles(self, rgb, origin_top, full_w, full_h, cube_path, glow, grain,
                      progress=None, inplace=False, colour_fix=COLOUR_FIX_NONE):
        self._upload_lut(cube_path)
        self._upload_glow(glow)
        gl = self.gl
        tile = self.tile
        bw, bh = rgb.size
        grain_norm = max(0.0, float(grain)) * 1.2 / 255.0
        cf_r, cf_g, cf_b, cf_sky = _normalize_colour_fix(colour_fix)
        gl.glUseProgram(self.prog)
        gl.glVertexAttribPointer(self.pos_loc, 2, GL_FLOAT, 0, 0, self._quad)
        gl.glEnableVertexAttribArray(self.pos_loc)
        gl.glUniform1f(self.uniforms["n"], ctypes.c_float(self.lut_size))
        gl.glUniform1f(self.uniforms["grainAmp"], ctypes.c_float(grain_norm))
        gl.glUniform1f(self.uniforms["glowMix"], ctypes.c_float(1.0 if glow is not None else 0.0))
        gl.glUniform4f(self.uniforms["colourFix"],
                       ctypes.c_float(cf_r), ctypes.c_float(cf_g),
                       ctypes.c_float(cf_b), ctypes.c_float(cf_sky))
        gl.glUniform2f(self.uniforms["imgSize"], ctypes.c_float(full_w), ctypes.c_float(full_h))
        gl.glBindFramebuffer(GL_FBO, self.fb)
        gl.glActiveTexture(GL_TEXTURE0)
        gl.glBindTexture(GL_TEX2D, self.src_tex)
        gl.glPixelStorei(0x0CF5, 1)
        out = rgb if inplace else Image.new("RGB", (bw, bh))
        buf = ctypes.create_string_buffer(tile * tile * 4)
        total_tiles = max(1, ((bh + tile - 1) // tile) * ((bw + tile - 1) // tile))
        done_tiles = 0
        for y in range(0, bh, tile):
            rows = min(tile, bh - y)
            for x in range(0, bw, tile):
                cols = min(tile, bw - x)
                raw = rgb.crop((x, y, x + cols, y + rows)).tobytes()
                gl.glTexSubImage2D(GL_TEX2D, 0, 0, 0, cols, rows, GL_RGB,
                                   GL_UBYTE, ctypes.c_char_p(raw))
                gl.glViewport(0, 0, cols, rows)
                gl.glUniform2f(self.uniforms["blockSize"],
                               ctypes.c_float(cols), ctypes.c_float(rows))
                gl.glUniform2f(self.uniforms["uvScale"],
                               ctypes.c_float(cols / float(tile)),
                               ctypes.c_float(rows / float(tile)))
                gl.glUniform2f(self.uniforms["blockOrigin"],
                               ctypes.c_float(x), ctypes.c_float(origin_top + y))
                gl.glDrawArrays(GL_STRIP, 0, 4)
                gl.glReadPixels(0, 0, cols, rows, GL_RGBA, GL_UBYTE, buf)
                part = Image.frombytes("RGBA", (cols, rows), buf.raw[:cols * rows * 4]).convert("RGB")
                out.paste(part, (x, y))
                done_tiles += 1
                if progress is not None:
                    progress(min(90, 8 + done_tiles * 82 // total_tiles))
        return out

    def _upload_lut(self, cube_path):
        key = (cube_path, os.path.getmtime(cube_path))
        if key == self.lut_key:
            return
        size, data, _ = read_cube(cube_path)
        atlas = self._atlas(size, data)
        size_wh = (size * size, size)
        gl = self.gl
        gl.glActiveTexture(GL_TEXTURE1)
        gl.glPixelStorei(0x0CF5, 1)   # atlas rows are not 4-byte aligned
        if self.lut_tex is not None and self._lut_alloc_size == size_wh:
            gl.glBindTexture(GL_TEX2D, self.lut_tex)
            gl.glTexSubImage2D(GL_TEX2D, 0, 0, 0, size_wh[0], size_wh[1],
                               GL_RGB, GL_UBYTE, ctypes.c_char_p(atlas))
        else:
            if self.lut_tex is not None:
                gl.glDeleteTextures(1, ctypes.byref(self.lut_tex))
            self.lut_tex = ctypes.c_uint()
            gl.glGenTextures(1, ctypes.byref(self.lut_tex))
            gl.glBindTexture(GL_TEX2D, self.lut_tex)
            for p in (GL_MIN_F, GL_MAG_F):
                gl.glTexParameteri(GL_TEX2D, p, GL_LINEAR)
            for p in (GL_WRAP_S, GL_WRAP_T):
                gl.glTexParameteri(GL_TEX2D, p, GL_CLAMP)
            gl.glTexImage2D(GL_TEX2D, 0, GL_RGB, size_wh[0], size_wh[1], 0, GL_RGB,
                            GL_UBYTE, ctypes.c_char_p(atlas))
            self._lut_alloc_size = size_wh
        self.lut_key = key
        self.lut_size = float(size)

    @staticmethod
    def _atlas(size, data):
        if np is None:
            return data
        arr = np.frombuffer(data, dtype=np.uint8).reshape(size, size, size, 3)
        return np.ascontiguousarray(arr.transpose(1, 0, 2, 3).reshape(size, size * size, 3)).tobytes()

    def apply(self, image, cube_path, halation=True, grain=5.0, progress=None,
              colour_fix=COLOUR_FIX_NONE):
        rgb = image if image.mode == "RGB" else image.convert("RGB")
        w, h = rgb.size
        cf = _normalize_colour_fix(colour_fix)
        glow = None
        if halation:
            mono = is_monochrome_cube(cube_path)
            sw, sh = max(1, w // 4), max(1, h // 4)
            small = rgb.resize((sw, sh), Image.Resampling.BOX)
            if _colour_fix_active(cf):
                small = _fix_magenta_band(small, cf)
            small_graded = small.filter(load_cube_lut(str(cube_path)))
            del small
            glow = build_glow(small_graded, monochrome=mono)
            del small_graded
        if progress is not None:
            progress(8)
        with _ACCEL_LOCK:
            self._make_current()
            try:
                return self._render_tiles(rgb, 0, w, h, cube_path, glow, grain,
                                          progress=progress, inplace=True,
                                          colour_fix=cf)
            finally:
                self._glow_ref = None
                self._release_current()


# ------------------------------------------------------------- multicore
_MP = {}

def _band_lut(job):
    top, bottom = job
    img, lut = _MP["img"], _MP["lut"]
    block = img.crop((0, top, img.width, bottom))
    cf = _MP.get("colour_fix", COLOUR_FIX_NONE)
    if _colour_fix_active(cf):
        block = _fix_magenta_band(block, cf)
    return top, bottom, block.filter(lut).tobytes()


def _band_finish(job):
    top, bottom = job
    img = _MP["img"]
    glow = _MP["glow"]
    grain = _MP["grain"]
    rows = bottom - top
    block = img.crop((0, top, img.width, bottom))
    if glow is not None:
        sw, sh = glow.size
        band = glow.crop((0, top * sh // img.height, sw,
                          min(sh, max(top * sh // img.height + 1, bottom * sh // img.height))))
        band = band.resize((img.width, rows), Image.Resampling.NEAREST)
        block = ImageChops.screen(block, band)
    if grain and np is not None:
        rng = np.random.default_rng(1000 + top)
        arr = np.asarray(block, dtype=np.int16)
        n = rng.integers(-120, 121, (rows, img.width), dtype=np.int16)
        n += rng.integers(-120, 121, (rows, img.width), dtype=np.int16)
        n //= 2
        small = block.resize((max(1, img.width // 8), max(1, rows // 8)),
                             Image.Resampling.BOX)
        lw = np.asarray(small, dtype=np.uint16)
        luma = (lw[:, :, 0] * 77 + lw[:, :, 1] * 150 + lw[:, :, 2] * 29) >> 8
        weight = np.clip(345 - (luma.astype(np.int32) * 3) // 2, 38, 255).astype(np.uint8)
        wt = np.asarray(Image.fromarray(weight, "L").resize((img.width, rows),
                                                             Image.Resampling.BILINEAR),
                        dtype=np.int32)
        grain_arr = (((n.astype(np.int32) * wt) >> 8) * max(1, int(round(grain)))) // 100
        arr += grain_arr.astype(np.int16)[:, :, None]
        block = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")
    return top, bottom, block.tobytes()


class MultiCoreFilm:
    MIN_FREE_MB = 110      # below this, forking a second image copy is suicide

    def __init__(self, jobs=None):
        self.jobs = jobs or min(2, os.cpu_count() or 1)

    def apply(self, image, cube_path, halation=True, grain=5.0, lut_obj=None,
              colour_fix=COLOUR_FIX_NONE):
        if lut_obj is None:
            size, data, _ = read_cube(cube_path)
            lut_obj = ImageFilter.Color3DLUT(size, [v / 255.0 for v in
                                                    struct.unpack("B" * (size ** 3 * 3), data)])
        import multiprocessing as mp
        if available_mb() < self.MIN_FREE_MB:
            with camera_log() as log:
                log.write(("FilmBox accel: multi-core skipped, only %d MB free\n"
                           % available_mb()).encode())
            return None
        rgb = image if image.mode == "RGB" else image.convert("RGB")
        w, h = rgb.size
        # 8 MP proved fatal even with two workers on this 352 MB board: the
        # fork copy plus one Pillow buffer per worker tips it into the OOM
        # killer. Multi-core is only used for the 2 MP fallback and below.
        if w * h > 2_500_000:
            with camera_log() as log:
                log.write(("FilmBox accel: multi-core skipped for %.1f MP\n"
                           % (w * h / 1e6)).encode())
            return None
        jobs = max(1, min(self.jobs, h // 256))
        if jobs < 2:
            return None
        bands = [(i * h // jobs, (i + 1) * h // jobs) for i in range(jobs)]
        ctx = mp.get_context("fork")
        _MP.clear()
        _MP.update(img=rgb, lut=lut_obj, glow=None, grain=grain,
                   colour_fix=_normalize_colour_fix(colour_fix))
        with ctx.Pool(jobs) as pool:
            parts = pool.map(_band_lut, bands)
        stage = Image.new("RGB", (w, h))
        for top, bottom, data in parts:
            stage.paste(Image.frombytes("RGB", (w, bottom - top), data), (0, top))
        if halation:
            _MP["img"] = stage
            _MP["glow"] = build_glow(stage, monochrome=is_monochrome_cube(cube_path))
        else:
            _MP["glow"] = None
        _MP["img"] = stage
        with ctx.Pool(jobs) as pool:
            parts = pool.map(_band_finish, bands)
        out = Image.new("RGB", (w, h))
        for top, bottom, data in parts:
            out.paste(Image.frombytes("RGB", (w, bottom - top), data), (0, top))
        _MP.clear()
        return out




def _accel_status():
    """Human-readable accelerator report for `filmbox diagnose`."""
    bits = []
    try:
        if _GPU_STATE == 1:
            bits.append("GPU ready (%s)" % _GPU.renderer)
        elif _GPU_STATE == 2:
            bits.append("GPU unavailable")
        else:
            bits.append("GPU untried")
    except Exception:
        bits.append("GPU unknown")
    jobs = min(4, os.cpu_count() or 1)
    bits.append("multi-core %d jobs" % jobs if jobs > 1 else "multi-core n/a")
    return ", ".join(bits)


GRAIN_STRENGTH = 5.0
GPU_MAX_PIXELS = 8_500_000
GPU_MIN_FREE_MB = 70
_ACCEL = os.environ.get("FILMBOX_ACCEL", "auto").strip().lower()
_ACCEL_LOCK = threading.Lock()
_GPU = None
_GPU_STATE = 0            # 0 untried, 1 ready, 2 unavailable


def film_copy(image, cube_path, progress=None, gpu_budget=None,
              colour_fix=COLOUR_FIX_NONE):
    """Apply colour fix + LUT + halation + grain with the best available engine.

    Order: GPU → multi-core → single-threaded CPU.  Any failure falls through,
    and the choice is logged so `filmbox diagnose` can show what was used.
    """
    global _GPU, _GPU_STATE
    cf = _normalize_colour_fix(colour_fix)
    cf_on = _colour_fix_active(cf)
    free_now = available_mb() if gpu_budget is None else gpu_budget
    if _gpu_ready(image.width * image.height, free_now):
        try:
            out = _GPU.apply(image, str(cube_path), True, GRAIN_STRENGTH,
                             progress=progress, colour_fix=cf)
            if progress:
                progress(90)
            return out
        except Exception as exc:
            _GPU_STATE = 2
            with camera_log() as log:
                log.write(("FilmBox accel: GPU failed mid-run (%s)\n" % exc).encode())
    if _ACCEL in ("auto", "multi", "gpu"):
        try:
            lut = load_cube_lut(str(cube_path))
            out = MultiCoreFilm().apply(image, str(cube_path), True, GRAIN_STRENGTH,
                                        lut, colour_fix=cf)
            if out is not None:
                if progress:
                    progress(90)
                return out
        except Exception as exc:
            with camera_log() as log:
                log.write(("FilmBox accel: multi-core failed (%s)\n" % exc).encode())
    lut = load_cube_lut(str(cube_path))
    w, h = image.size
    result = image
    step = 256
    for top in range(0, h, step):
        bottom = min(h, top + step)
        block = result.crop((0, top, w, bottom))
        if cf_on:
            block = _fix_magenta_band(block, cf)
        result.paste(block.filter(lut), (0, top))
        if progress:
            progress(min(60, (top + step) * 60 // max(1, h)))
    result = add_halation(result, monochrome=is_monochrome_cube(cube_path))
    if progress:
        progress(75)
    result = add_grain(result, GRAIN_STRENGTH)
    return result


def _raw_prepare(original, workdir):
    """Decode a JPEG to raw rgb24 (+ a 1/4 thumbnail) with ffmpeg.

    Pillow would hold the whole 8 MP frame (24 MB) while the film copy needs
    its own output buffer; going through a raw file keeps only the tile we are
    working on resident.
    """
    raw = workdir / "input.raw"
    small = workdir / "thumb.raw"
    run_command(["ffmpeg", "-y", "-loglevel", "error", "-i", str(original),
                 "-f", "rawvideo", "-pix_fmt", "rgb24", str(raw)], timeout=120)
    with Image.open(original) as handle:
        w, h = handle.size
    if not raw.exists() or raw.stat().st_size < w * h * 3:
        return None
    sw, sh = max(1, w // 4), max(1, h // 4)
    try:
        run_command(["ffmpeg", "-y", "-loglevel", "error", "-i", str(original),
                     "-vf", "scale=%d:%d" % (sw, sh),
                     "-f", "rawvideo", "-pix_fmt", "rgb24", str(small)], timeout=60)
    except Exception:
        return None
    if not small.exists() or small.stat().st_size < sw * sh * 3:
        return None
    return raw, small, w, h, sw, sh


def film_copy_raw(raw, small, w, h, sw, sh, cube_path, progress=None,
                  colour_fix=COLOUR_FIX_NONE):
    """Streaming film copy: per-tile reads from mmap, one output buffer."""
    step = 512
    out = Image.new("RGB", (w, h))
    row = w * 3
    lut = load_cube_lut(str(cube_path))
    mono = is_monochrome_cube(cube_path)
    cf = _normalize_colour_fix(colour_fix)
    cf_on = _colour_fix_active(cf)
    with open(raw, "rb") as handle:
        mm = mmap.mmap(handle.fileno(), 0, access=mmap.ACCESS_READ)
        with open(small, "rb") as sh_handle:
            thumb = Image.frombuffer("RGB", (sw, sh), sh_handle.read(), "raw", "RGB", 0, 1)
            if cf_on:
                thumb = _fix_magenta_band(thumb, cf)
            glow = build_glow(thumb.filter(lut), monochrome=mono)
        gpu = _gpu_ready(w * h, available_mb())
        done = 0
        for top in range(0, h, step):
            rows = min(step, h - top)
            block = Image.frombuffer("RGB", (w, rows), mm[top * row:(top + rows) * row],
                                     "raw", "RGB", 0, 1)
            band = _render_band(block, top, w, h, cube_path, glow, gpu, colour_fix=cf)
            out.paste(band, (0, top))
            done += rows
            if progress:
                progress(min(95, done * 95 // max(1, h)))
        mm.close()
    return out


def _gpu_ready(pixels, free_mb=None):
    global _GPU, _GPU_STATE
    if _ACCEL in ("cpu", "multi"):
        return False
    if pixels > GPU_MAX_PIXELS:
        return False
    if free_mb is not None and free_mb < GPU_MIN_FREE_MB:
        return False
    with _ACCEL_LOCK:
        if _GPU_STATE == 0:
            # Mesa/vc4 requires EGL context creation on the main thread; never
            # call GpuFilm() lazily from a worker thread.
            if threading.current_thread() is not threading.main_thread():
                return False
            try:
                _GPU = GpuFilm()
                _GPU_STATE = 1
                with camera_log() as log:
                    log.write(("FilmBox accel: GPU (%s, max tex %d)\n"
                               % (_GPU.renderer, _GPU.maxt)).encode())
            except Exception as exc:
                _GPU_STATE = 2
                with camera_log() as log:
                    log.write(("FilmBox accel: no GPU (%s)\n" % exc).encode())
    return _GPU_STATE == 1


def _render_band(block, top, w, h, cube_path, glow, use_gpu, colour_fix=COLOUR_FIX_NONE):
    """One horizontal band of the film copy, sharing the glow texture."""
    cf = _normalize_colour_fix(colour_fix)
    if use_gpu:
        try:
            return _GPU.apply_band(block, top, w, h, str(cube_path), glow, GRAIN_STRENGTH,
                                   colour_fix=cf)
        except Exception as exc:
            with camera_log() as log:
                log.write(("FilmBox accel: GPU band failed (%s)\n" % exc).encode())
    lut = load_cube_lut(str(cube_path))
    if _colour_fix_active(cf):
        block = _fix_magenta_band(block, cf)
    band = block.filter(lut)
    if glow is not None:
        sw, sh = glow.size
        g0 = top * sh // h
        g1 = min(sh, max(g0 + 1, (top + band.height) * sh // h))
        piece = glow.crop((0, g0, sw, g1)).resize((w, band.height),
                                                  Image.Resampling.NEAREST)
        band = ImageChops.screen(band, piece)
    return add_grain(band, GRAIN_STRENGTH, seed=top)


def _render_lut_with_gpu(gpu, original, mode, cube_path, want_full, progress=None,
                         colour_fix=COLOUR_FIX_NONE):
    """Decode JPEG, apply GpuFilm with 2MP memory fallback, and save atomically."""
    original = Path(original)
    cf = _normalize_colour_fix(colour_fix)
    room_for_full = available_mb() >= 130
    decode_mb = 24 if (want_full and room_for_full) else 6
    gpu_budget = available_mb() - decode_mb
    if gpu_budget < GPU_MIN_FREE_MB:
        return None, "low_mem"
    with Image.open(original) as handle:
        if not (want_full and room_for_full):
            try:
                handle.draft("RGB", (LUT_SAVE_MAX[0] * 2, LUT_SAVE_MAX[1] * 2))
            except Exception:
                pass
        image = handle.convert("RGB")
    limit = None if (want_full and room_for_full) else LUT_SAVE_MAX
    if limit and max(image.size) > max(limit):
        scale = min(limit[0] / image.width, limit[1] / image.height)
        image = image.resize((max(1, int(image.width * scale)),
                              max(1, int(image.height * scale))),
                             Image.Resampling.BOX)
    if image.width * image.height > GPU_MAX_PIXELS:
        return None, "too_large"
    try:
        result = gpu.apply(image, str(cube_path), True, GRAIN_STRENGTH,
                           progress=progress, colour_fix=cf)
    except (MemoryError, OSError) as exc:
        with camera_log() as log:
            log.write(("FilmBox GPU memory fallback: %s | %s\n"
                       % (exc, mem_snapshot())).encode())
        scale = min(LUT_SAVE_MAX[0] / image.width, LUT_SAVE_MAX[1] / image.height)
        small = image.resize((max(1, int(image.width * scale)),
                              max(1, int(image.height * scale))),
                             Image.Resampling.BOX)
        del image
        image = small
        result = gpu.apply(image, str(cube_path), True, GRAIN_STRENGTH,
                           progress=progress, colour_fix=cf)
    tag = "" if max(result.size) > max(LUT_SAVE_MAX) else "_%dmp" % round(
        result.width * result.height / 1e6)
    out_path = original.with_name(original.stem + "_" +
                                  mode.replace(":", "-").lower() + tag + ".jpg")
    _atomic_save_image(result, out_path, "JPEG", quality=93, subsampling=1)
    del image, result
    reclaim_memory()
    return out_path, None


def command_gpu_worker():
    """Dedicated background GPU process: owns EGL on its own main thread.

    Running EGL in a separate process lets the camera UI open the live
    viewfinder immediately (zero 2-3 s cold-start stall), obeys Mesa/vc4's
    main-thread EGL creation requirement, and isolates any driver crash.
    """
    try:
        os.nice(5)
    except (AttributeError, OSError):
        pass
    try:
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except (ValueError, OSError):
        pass

    def emit(payload):
        sys.stdout.write(json.dumps(payload) + "\n")
        sys.stdout.flush()

    try:
        gpu = GpuFilm()
    except Exception as exc:
        emit({"event": "fail", "error": str(exc)})
        return 1
    emit({"event": "ready", "renderer": gpu.renderer, "maxt": gpu.maxt})

    for raw_line in sys.stdin:
        line = raw_line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except ValueError:
            emit({"event": "error", "error": "bad request"})
            continue
        cmd = req.get("cmd")
        if cmd == "quit":
            break
        if cmd != "render":
            emit({"event": "error", "error": "unknown cmd"})
            continue
        try:
            out_path, reason = _render_lut_with_gpu(
                gpu,
                req["original"],
                req["mode"],
                req["cube"],
                bool(req.get("full_res", True)),
                progress=lambda pct: emit({"event": "progress", "pct": int(pct)}),
                colour_fix=_normalize_colour_fix(req.get("colour_fix")),
            )
            if out_path is None:
                emit({"event": "fallback", "reason": reason or "budget"})
            else:
                emit({"event": "done", "path": str(out_path)})
        except Exception as exc:
            emit({"event": "error", "error": "%s: %s" % (type(exc).__name__, exc)})
    return 0


class GpuWorker:
    """Async controller for the background `_gpu-worker` helper process."""

    def __init__(self, script_path=None):
        self.script_path = str(script_path or Path(__file__).resolve())
        self.proc = None
        self.reader = None
        self.state = "idle"     # idle | warming | ready | failed | stopped
        self.renderer = ""
        self.maxt = 0
        self.error = None
        self._cond = threading.Condition()
        self._job_lock = threading.Lock()
        self._progress_cb = None
        self._job_result = None

    def start(self):
        with self._cond:
            if self.state in ("warming", "ready"):
                return
            self.state = "warming"
            self.error = None
        log_fh = None
        try:
            log_fh = camera_log()
            self.proc = subprocess.Popen(
                [sys.executable, "-u", self.script_path, "_gpu-worker"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=log_fh,
                text=True,
                bufsize=1,
            )
        except Exception as exc:
            with self._cond:
                self.state = "failed"
                self.error = str(exc)
                self._cond.notify_all()
            return
        finally:
            if log_fh is not None:
                try:
                    log_fh.close()
                except OSError:
                    pass
        self.reader = threading.Thread(target=self._read_loop, daemon=True)
        self.reader.start()

    def is_warming(self):
        with self._cond:
            return self.state == "warming"

    def is_ready(self):
        with self._cond:
            return self.state == "ready"

    def _read_loop(self):
        proc = self.proc
        try:
            assert proc is not None and proc.stdout is not None
            for line in proc.stdout:
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                ev = msg.get("event")
                if ev == "progress":
                    cb = self._progress_cb
                    if cb is not None:
                        try:
                            cb(int(msg.get("pct", 0)))
                        except Exception:
                            pass
                    continue
                with self._cond:
                    if ev == "ready":
                        self.state = "ready"
                        self.renderer = str(msg.get("renderer", ""))
                        self.maxt = int(msg.get("maxt", 0))
                        with camera_log() as log:
                            log.write(("FilmBox accel: GPU worker ready (%s, max tex %d)\n"
                                       % (self.renderer, self.maxt)).encode())
                    elif ev == "fail":
                        self.state = "failed"
                        self.error = str(msg.get("error", "init failed"))
                        with camera_log() as log:
                            log.write(("FilmBox accel: GPU worker unavailable (%s)\n"
                                       % self.error).encode())
                    elif ev in ("done", "error", "fallback"):
                        self._job_result = msg
                    self._cond.notify_all()
        except Exception as exc:
            with self._cond:
                if self.state != "stopped":
                    self.state = "failed"
                    self.error = str(exc)
                if self._job_result is None:
                    self._job_result = {"event": "error", "error": str(exc)}
                self._cond.notify_all()
        finally:
            with self._cond:
                if self.state not in ("stopped", "failed"):
                    self.state = "failed"
                    self.error = self.error or "GPU worker exited"
                if self._job_result is None:
                    self._job_result = {"event": "error", "error": "GPU worker exited"}
                self._cond.notify_all()

    def render(self, original, mode, cube_path, full_res=True, progress=None,
               on_status=None, wait_timeout=10.0, render_timeout=60.0,
               colour_fix=COLOUR_FIX_NONE):
        """Render a LUT copy in the background GPU worker.

        If the worker is still warming up on the first shot, notifies the UI
        via `on_status` and waits up to `wait_timeout` seconds. Returns a Path
        on success, or None to let the caller fall back seamlessly to CPU.
        """
        with self._job_lock:
            with self._cond:
                if self.state == "warming":
                    if on_status is not None:
                        on_status("LOADING GPU... (FIRST SHOT)")
                    deadline = time.monotonic() + wait_timeout
                    while self.state == "warming":
                        rem = deadline - time.monotonic()
                        if rem <= 0:
                            break
                        self._cond.wait(timeout=rem)
                    if self.state == "warming":
                        with camera_log() as log:
                            log.write(b"FilmBox accel: GPU worker warm-up timed out; falling back to CPU\n")
                        if on_status is not None:
                            on_status("GPU BUSY -> CPU RENDER")
                        return None
                if self.state != "ready" or self.proc is None or self.proc.stdin is None:
                    if on_status is not None and self.state == "failed":
                        on_status("GPU UNAVAILABLE -> CPU")
                    return None
                self._job_result = None
                self._progress_cb = progress
                if on_status is not None:
                    on_status("GPU RENDERING")
            req = {
                "cmd": "render",
                "original": str(original),
                "mode": mode,
                "cube": str(cube_path),
                "full_res": bool(full_res),
                "colour_fix": list(_normalize_colour_fix(colour_fix)),
            }
            try:
                self.proc.stdin.write(json.dumps(req) + "\n")
                self.proc.stdin.flush()
            except OSError as exc:
                with self._cond:
                    self._progress_cb = None
                    self.state = "failed"
                    self.error = str(exc)
                if on_status is not None:
                    on_status("GPU LOST -> CPU RENDER")
                return None
            with self._cond:
                deadline = time.monotonic() + render_timeout
                while self._job_result is None and self.state == "ready":
                    rem = deadline - time.monotonic()
                    if rem <= 0:
                        break
                    self._cond.wait(timeout=rem)
                result = self._job_result
                self._progress_cb = None
            if not result:
                with camera_log() as log:
                    log.write(b"FilmBox accel: GPU worker render timed out\n")
                self.stop()
                if on_status is not None:
                    on_status("GPU TIMEOUT -> CPU")
                return None
            if result.get("event") == "done":
                return Path(result["path"])
            with camera_log() as log:
                log.write(("FilmBox accel: GPU worker fallback (%s)\n"
                           % (result.get("error") or result.get("reason") or "unknown")).encode())
            if on_status is not None:
                on_status("CPU FALLBACK")
            return None

    def stop(self):
        with self._cond:
            self.state = "stopped"
            self._cond.notify_all()
        proc = self.proc
        self.proc = None
        if proc is not None:
            try:
                if proc.stdin and not proc.stdin.closed:
                    try:
                        proc.stdin.write('{"cmd":"quit"}\n')
                        proc.stdin.flush()
                    except OSError:
                        pass
                    proc.stdin.close()
            except OSError:
                pass
            try:
                proc.wait(timeout=1.5)
            except subprocess.TimeoutExpired:
                try:
                    proc.terminate()
                    proc.wait(timeout=1.5)
                except Exception:
                    try:
                        proc.kill()
                        proc.wait(timeout=1.0)
                    except Exception:
                        pass
            if proc.stdout:
                try:
                    proc.stdout.close()
                except OSError:
                    pass
        reader = self.reader
        self.reader = None
        if reader is not None and reader is not threading.current_thread():
            reader.join(timeout=1.5)


def _atomic_save_image(image, path, fmt=None, **kwargs):
    """Write image to a hidden temp file first so interrupted saves never appear in Gallery."""
    path = Path(path)
    tmp = path.with_name("." + path.name + ".tmp")
    fmt = fmt or ("JPEG" if path.suffix.lower() in (".jpg", ".jpeg") else "PNG")
    try:
        image.save(tmp, format=fmt, **kwargs)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


@lru_cache(maxsize=16)
def _channel_mul_lut(mul):
    if np is None:
        return [min(255, int(v * mul + 0.5)) for v in range(256)]
    vals = np.rint(np.arange(256, dtype=np.float32) * float(mul))
    return np.clip(vals, 0, 255).astype(np.uint8)


def _fix_magenta_band(band, spec=COLOUR_FIX_PRESETS[2]):
    """Apply IMX219 WB channel LUTs + uint16 blue-sky green lift (~2.5 MB/band).

    Accepts either a 4-tuple (r_mul, g_mul, b_mul, sky_lift) or a legacy scalar
    sky_lift float. Uses 256-byte LUT lookups for channel scaling and masked
    uint16 fixed-point arithmetic for sky lift (zero float64 arrays).
    """
    r_mul, g_mul, b_mul, sky_k = _normalize_colour_fix(spec)
    has_r = abs(r_mul - 1.0) > 1e-4
    has_g = abs(g_mul - 1.0) > 1e-4
    has_b = abs(b_mul - 1.0) > 1e-4
    has_wb = has_r or has_g or has_b
    k = int(round(sky_k * 256))
    if not has_wb and k <= 0:
        return band
    rgb = band if band.mode == "RGB" else band.convert("RGB")
    if np is None:
        if has_wb:
            lut = _channel_mul_lut(r_mul) + _channel_mul_lut(g_mul) + _channel_mul_lut(b_mul)
            return rgb.point(lut)
        return rgb
    arr = np.array(rgb, dtype=np.uint8)
    changed = False
    if has_r:
        arr[:, :, 0] = _channel_mul_lut(r_mul)[arr[:, :, 0]]
        changed = True
    if has_g:
        arr[:, :, 1] = _channel_mul_lut(g_mul)[arr[:, :, 1]]
        changed = True
    if has_b:
        arr[:, :, 2] = _channel_mul_lut(b_mul)[arr[:, :, 2]]
        changed = True
    if k > 0:
        g = arr[:, :, 1]
        b = arr[:, :, 2]
        mask = b > g
        if mask.any():
            diff = b[mask].astype(np.uint16) - g[mask].astype(np.uint16)
            g[mask] = np.minimum(
                255, g[mask].astype(np.uint16) + ((diff * k + 128) >> 8)
            ).astype(np.uint8)
            changed = True
    return Image.fromarray(arr, "RGB") if changed else rgb


def _fix_magenta(image, spec=COLOUR_FIX_PRESETS[2], step=256):
    """Apply IMX219 colour correction in-place, 256 rows at a time (~3 MB temp RAM).

    Compensates the IMX219 module's global red/magenta cast (weak IR-cut + AWB
    bias) via 256-byte per-channel LUTs and lifts residual blue-sky violet cast
    where B > G.
    """
    cf = _normalize_colour_fix(spec)
    if not _colour_fix_active(cf):
        return image
    rgb = image if image.mode == "RGB" else image.convert("RGB")
    w, h = rgb.size
    for top in range(0, h, step):
        bottom = min(h, top + step)
        band = rgb.crop((0, top, w, bottom))
        fixed = _fix_magenta_band(band, cf)
        if fixed is not band:
            rgb.paste(fixed, (0, top))
    return rgb


def _apply_colour_fix(path, spec):
    """Atomically rewrite a captured JPEG with in-place IMX219 colour correction."""
    cf = _normalize_colour_fix(spec)
    if not _colour_fix_active(cf):
        return
    if available_mb() < 60:
        with camera_log() as log:
            log.write(("FilmBox colour fix skipped: only %d MB free\n"
                       % available_mb()).encode())
        return
    try:
        with Image.open(path) as handle:
            rgb = handle.convert("RGB")
        _fix_magenta(rgb, cf)
        _atomic_save_image(rgb, path, "JPEG", quality=93, subsampling=1)
        del rgb
        reclaim_memory()
    except Exception as exc:
        with camera_log() as log:
            log.write(("FilmBox colour fix skipped: %s\n" % exc).encode())


def _wait_for_exposure(picam, timeout=1.5, tol=0.05):
    """Block briefly until AE settles after a sensor mode switch.

    capture_metadata() is already frame-paced by libcamera, so polling without
    extra sleep() settles in ~3-5 frames (~0.3-0.6 s) instead of 1.5-2.5 s.
    """
    deadline = time.monotonic() + timeout
    prev = None
    stable = 0
    while time.monotonic() < deadline:
        try:
            md = picam.capture_metadata()
        except Exception:
            break
        if md.get("AeLocked"):
            break
        exp = float(md.get("ExposureTime", 0) or 0)
        gain = float(md.get("AnalogueGain", 0) or 0)
        if prev and exp and prev[0]:
            de = abs(exp - prev[0]) / prev[0]
            dg = abs(gain - prev[1]) / max(1e-6, prev[1])
            if de < tol and dg < tol:
                stable += 1
                if stable >= 2:
                    break
            else:
                stable = 0
        prev = (exp, gain)


def filtered_still(original, mode, progress=None, full_res=None,
                   colour_fix=COLOUR_FIX_NONE):
    """Render the film copy. LUT work is tiled so an 8MP frame stays in RAM."""
    if mode == "NORMAL":
        return None
    is_lut = mode.startswith(LUT_PREFIX)
    cf = _normalize_colour_fix(colour_fix)
    cube_file = resolve_lut_path(mode[len(LUT_PREFIX):]) if is_lut else None
    # Measure free RAM *before* decoding: an 8MP RGB copy eats ~24 MB (2MP eats ~6 MB).
    want_full = LUT_FULL_RES if full_res is None else bool(full_res)
    room_for_full = available_mb() >= 130
    decode_mb = 24 if (want_full and room_for_full) else 6
    gpu_budget = available_mb() - decode_mb
    if (is_lut and want_full and room_for_full
            and os.environ.get("FILMBOX_STREAM", "0") == "1"):
        try:
            with Image.open(original) as probe:
                pw, ph = probe.size
        except Exception:
            pw = ph = 0
        if pw * ph >= 3_000_000:
            work = Path(tempfile.mkdtemp(prefix="filmbox-"))
            try:
                prep = _raw_prepare(original, work)
                if prep is not None:
                    result = film_copy_raw(*prep, cube_file, progress, colour_fix=cf)
                    path = original.with_name(original.stem + "_" +
                                              mode.replace(":", "-").lower() + ".jpg")
                    _atomic_save_image(result, path, "JPEG", quality=93, subsampling=1)
                    if progress:
                        progress(100)
                    reclaim_memory()
                    return path
            except Exception as exc:
                with camera_log() as log:
                    log.write(("FilmBox stream failed: %s\n" % exc).encode())
            finally:
                shutil.rmtree(work, ignore_errors=True)
    with Image.open(original) as handle:
        if is_lut and not room_for_full:
            # Ask libjpeg for a 1/2 or 1/4 scale decode: the 2 MP path then costs
            # ~1/4 of the memory and time of decoding 8 MP and throwing it away.
            try:
                handle.draft("RGB", (LUT_SAVE_MAX[0] * 2, LUT_SAVE_MAX[1] * 2))
            except Exception:
                pass
        image = handle.convert("RGB")
        if is_lut:
            limit = None if (want_full and room_for_full) else LUT_SAVE_MAX
            if limit and max(image.size) > max(limit):
                scale = min(limit[0] / image.width, limit[1] / image.height)
                image = image.resize((int(image.width * scale), int(image.height * scale)),
                                     Image.Resampling.BOX)
            try:
                result = film_copy(image, cube_file, progress, gpu_budget, colour_fix=cf)
            except (MemoryError, OSError) as exc:
                # An 8 MP render needs ~130 MB. Boot-time fragmentation can leave
                # less than that even though the frame decoded fine, so retry once
                # at 2 MP instead of failing the whole shot.
                with camera_log() as log:
                    log.write(("FilmBox memory fallback: %s | %s\n"
                               % (exc, mem_snapshot())).encode())
                scale = min(LUT_SAVE_MAX[0] / image.width, LUT_SAVE_MAX[1] / image.height)
                small = image.resize((max(1, int(image.width * scale)),
                                      max(1, int(image.height * scale))),
                                     Image.Resampling.BOX)
                del image
                image = small
                result = film_copy(image, cube_file, progress, gpu_budget, colour_fix=cf)
        else:
            result = filter_image(image, mode, colour_fix=cf)
    if is_lut:
        tag = "" if max(result.size) > max(LUT_SAVE_MAX) else "_%dmp" % round(
            result.width * result.height / 1e6)
        path = original.with_name(original.stem + "_" +
                                  mode.replace(":", "-").lower() + tag + ".jpg")
        _atomic_save_image(result, path, "JPEG", quality=93, subsampling=1)
    else:
        path = original.with_name(original.stem + "_" + mode.lower() + ".png")
        _atomic_save_image(result, path, "PNG")
    if progress:
        progress(100)
    reclaim_memory()
    return path


def capture_still(resolution=(3280, 2464), sound=True, rotation=180):
    """Sensor capture only; film rendering happens later, off the viewfinder path."""
    output = reserve_path("Photos", "8mp" if resolution == (3280, 2464) else "photo", "jpg")
    click(sound)
    w, h = resolution
    try:
        tuning_path = resolve_tuning_file()
        cmd = ["rpicam-still", "-n", "-t", "1300", "--width", str(w),
               "--height", str(h), "--mode", FULL_STILL_MODE,
               "--rotation", str(rotation)]
        if tuning_path is not None:
            cmd.extend(["--tuning-file", str(tuning_path)])
        cmd.extend(["-o", str(output)])
        run_command(cmd, timeout=35)
        with Image.open(output) as im:
            im.verify()
    except Exception:
        if output.exists() and output.stat().st_size < 1024:
            output.unlink()
        raise
    return output


def take_photo(mode="NORMAL", resolution=(3280, 2464), sound=True, rotation=180,
               full_res=None, colour_fix=COLOUR_FIX_NONE):
    output = capture_still(resolution, sound, rotation)
    cf = _normalize_colour_fix(colour_fix)
    if mode != "NORMAL":
        filtered = filtered_still(output, mode, full_res=full_res, colour_fix=cf)
    else:
        if _colour_fix_active(cf):
            _apply_colour_fix(output, cf)
        filtered = None
    return output, filtered


class CameraStream:
    """One Picamera2 session for preview, 8 MP stills and MJPEG video.

    Interface kept for the rest of the app: start/stop/latest/next_frame,
    capture_still, start_recording/stop_recording, set_zoom.
    """

    def __init__(self, rotation=180, fps=25, size=PREVIEW_SIZE):
        self.rotation = rotation
        self.fps = fps
        self.size = tuple(size)
        self.zoom = ZOOMS[0]
        self.picam = None
        self.proc = None                     # legacy attribute, always None
        self.lock = threading.Condition()
        self.camlock = threading.RLock()
        self.seq = 0
        self.jpg = None
        self.running = False
        self.worker = None
        self.error = None
        self.logfile = None
        self._last_publish = 0.0
        self.video_config = None
        self.still_config = None
        self._still_cache = {}
        self._record_cache = {}
        self._encoder = None
        self._output = None
        self._recording = False
        self._record_raw = None
        self._record_path = None
        self._record_started = None
        self._last_frame_duration = None

    # ---------------------------------------------------------------- setup
    def _transform(self):
        try:
            from libcamera import Transform
        except Exception:
            return None
        return Transform(hflip=True, vflip=True) if self.rotation == 180 else Transform()

    def start(self):
        with self.camlock:
            if self.picam is not None:
                return
            from picamera2 import Picamera2
            self.error = None
            transform = self._transform()
            tuning = _load_picam_tuning(Picamera2)
            picam = Picamera2(tuning=tuning) if tuning is not None else Picamera2()
            try:
                self._build_configs(picam, transform)
                picam.configure(self.video_config)
                picam.start()
            except Exception:
                try:
                    picam.close()
                except Exception:
                    pass
                raise
            self.picam = picam
            self._still_cache.clear()
            self._record_cache.clear()
            self._apply_crop()
            self.running = True
            self.worker = threading.Thread(target=self._loop, daemon=True)
            self.worker.start()

    def _build_configs(self, picam, transform):
        # 1640x1232 keeps the full field: the 640x480 mode is a 2x centre crop.
        self.video_config = picam.create_video_configuration(
            main={"size": self.size, "format": "RGB888"},
            sensor={"output_size": (1640, 1232), "bit_depth": 10},
            buffer_count=3, transform=transform)

        self.still_config = picam.create_still_configuration(
            main={"size": SENSOR_SIZE, "format": "RGB888"},
            sensor={"output_size": SENSOR_SIZE, "bit_depth": 10},
            buffer_count=1, transform=transform)

    def stop(self):
        if self.picam is None:
            return
        self.running = False
        with self.lock:
            self.lock.notify_all()
        worker = self.worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=3)
        with self.camlock:
            if self.picam is not None:
                try:
                    self.picam.stop()
                    self.picam.close()
                except Exception:
                    pass
                self.picam = None
            self.worker = None

    def _restore_preview(self):
        """Return to the live-view configuration after any failure."""
        try:
            with self.camlock:
                if self.picam is None:
                    return
                self.picam.stop()
                self.picam.switch_mode(self.video_config)
                self.picam.start()
                self._apply_crop()
        except Exception:
            pass

    # -------------------------------------------------------------- preview
    def _loop(self):
        while self.running:
            try:
                with self.camlock:
                    if not self.running:
                        break
                    request = self.picam.capture_request()
                    try:
                        pixels = request.make_array("main")
                        meta = request.get_metadata()
                    finally:
                        request.release()
                duration = meta.get("FrameDuration")
                if duration:
                    self._last_frame_duration = float(duration)
                # throttle BEFORE the BGR->RGB copy: skipping the frame later
                # would still pay for the conversion on a 512 MB board
                now = time.monotonic()
                if self.fps and now - self._last_publish < (1.0 / self.fps) * 0.85:
                    continue
                # libcamera lays RGB888 out as BGR in memory
                frame = Image.fromarray(np.ascontiguousarray(pixels[:, :, ::-1]))
                if frame.size != self.size:
                    frame = fill_size(frame, self.size)
                buffer = io.BytesIO()
                frame.save(buffer, "JPEG", quality=72)
                jpg = buffer.getvalue()
            except Exception as exc:
                self.error = str(exc)[:80]
                time.sleep(0.2)
                continue
            with self.lock:
                self.jpg = jpg
                self.seq += 1
                self._last_publish = now
                self.error = None            # recovered: let the UI clear the banner
                self.lock.notify_all()

    def latest(self):
        """(seq, jpeg) — newest preview frame, or (seq, None) before the first."""
        with self.lock:
            return self.seq, self.jpg

    def next_frame(self, since, timeout=2):
        with self.lock:
            self.lock.wait_for(lambda: self.seq > since or not self.running,
                               timeout=timeout)
            if self.seq > since:
                return self.seq, self.jpg
            return since, None

    # ----------------------------------------------------------------- zoom
    @staticmethod
    def _rect_in(sw, sh, aspect, factor):
        """Centred rectangle of `aspect`, shrunk by `factor` (1.0 = full)."""
        w = sw * factor
        h = w / aspect
        if h > sh * factor:
            h = sh * factor
            w = h * aspect
        w = max(2, int(w) // 2 * 2)
        h = max(2, int(h) // 2 * 2)
        x = max(0, (sw - w) // 2 // 2 * 2)
        y = max(0, (sh - h) // 2 // 2 * 2)
        return (x, y, w, h)

    def crop_rect(self, aspect=None):
        """ScalerCrop in full pixel-array coordinates (3280x2464).

        The control's range is the whole array even when the sensor runs the
        1640x1232 mode; using mode pixels gets clamped (no zoom + stretch).
        """
        sw, sh = SENSOR_SIZE
        ar = aspect if aspect else (self.size[0] / float(self.size[1]))
        return self._rect_in(sw, sh, ar, 1.0 / float(self.zoom))

    def _apply_crop(self, aspect=None):
        if self.picam is None:
            return
        try:
            self.picam.set_controls({"ScalerCrop": self.crop_rect(aspect)})
        except Exception:
            pass

    def set_zoom(self, zoom):
        self.zoom = zoom if zoom in ZOOMS else ZOOMS[0]
        self._apply_crop()

    # --------------------------------------------------------------- stills
    def _still_config(self, size, sensor_size=SENSOR_SIZE):
        size = (int(size[0]), int(size[1]))
        sensor_size = (int(sensor_size[0]), int(sensor_size[1]))
        key = (size, sensor_size)
        cached = self._still_cache.get(key)
        if cached is None:
            # keep the full sensor field: without this libcamera may pick the
            # 640x480 mode (2x centre crop) for small output sizes
            cached = self.picam.create_still_configuration(
                main={"size": size, "format": "RGB888"},
                sensor={"output_size": sensor_size, "bit_depth": 10},
                buffer_count=1, transform=self._transform())
            self._still_cache[key] = cached
        return cached

    def _crop_file(self, path, limit=None):
        """Software crop for zoom and non-4:3 presets (e.g. 1080P 16:9)."""
        sw, sh = SENSOR_SIZE
        target = limit or SENSOR_SIZE
        target_aspect = target[0] / float(target[1])
        sensor_aspect = sw / float(sh)
        zoom = float(self.zoom)
        if zoom <= ZOOMS[0] and abs(target_aspect - sensor_aspect) < 1e-3:
            return
        with Image.open(path) as handle:
            image = handle.convert("RGB")
        iw, ih = image.size
        x, y, cw, ch = self._rect_in(iw, ih, target_aspect, 1.0 / zoom)
        cropped = image.crop((x, y, x + cw, y + ch))
        if cropped.width > target[0] or cropped.height > target[1]:
            cropped = cropped.resize(target, Image.Resampling.LANCZOS)
        _atomic_save_image(cropped, path, "JPEG", quality=93, subsampling=1)

    def capture_still(self, path, resolution=None):
        limit = resolution or SENSOR_SIZE
        # Bursts of 8 MP shots can outrun the CMA pool: a capture then fails with
        # ENOMEM. Back off first, then step the sensor mode down to 2x2 binned 2MP.
        last = None
        for attempt in range(4):
            sensor_mode = SENSOR_SIZE if attempt < 2 else (1640, 1232)
            eff_limit = limit if attempt < 2 else (min(limit[0], 1640), min(limit[1], 1232))
            try:
                return self._capture_once(path, eff_limit, sensor_mode)
            except OSError as exc:
                last = exc
                if getattr(exc, "errno", None) != 12:
                    raise
                with camera_log() as log:
                    log.write(("FilmBox capture ENOMEM try %d (%s) | %s\n"
                               % (attempt + 1, sensor_mode, mem_snapshot())).encode())
                self._restore_preview()
                reclaim_memory()
                time.sleep(0.4 + 0.3 * attempt)
        raise last

    def _capture_once(self, path, limit, sensor_size=SENSOR_SIZE):
        sw, sh = SENSOR_SIZE
        zoom = float(self.zoom)
        scale = min(1.0, max(limit[0] / float(sw), limit[1] / float(sh)) * zoom)
        size = (max(2, min(sensor_size[0], int(round(sw * scale))) // 2 * 2),
                max(2, min(sensor_size[1], int(round(sh * scale))) // 2 * 2))
        with self.camlock:
            picam = self.picam
            if picam is None:
                raise RuntimeError("camera not started")
            config = self._still_config(size, sensor_size=sensor_size)
            picam.stop()
            picam.switch_mode(config)
            picam.start()
            try:
                # full sensor into the file; zoom & aspect crop applied deterministically below
                picam.set_controls({"ScalerCrop": (0, 0) + tuple(SENSOR_SIZE)})
                _wait_for_exposure(picam)
                picam.capture_file(str(path))
            finally:
                try:
                    picam.stop()
                    picam.switch_mode(self.video_config)
                    picam.start()
                    self._apply_crop()
                except Exception:
                    self._restore_preview()
        self._crop_file(path, limit)
        # The colour fix rewrites the whole 8 MP JPEG; running it inside the
        # capture job stalled the UI, so it moved to the render/background path.

    # ---------------------------------------------------------------- video
    def _record_config(self, size):
        size = (int(size[0]), int(size[1]))
        cached = self._record_cache.get(size)
        if cached is None:
            cached = self.picam.create_video_configuration(
                main={"size": size, "format": "RGB888"},
                sensor={"output_size": (1640, 1232), "bit_depth": 10},
                buffer_count=6, transform=self._transform())
            self._record_cache[size] = cached
        return cached

    def start_recording(self, path, size=(1280, 720), bitrate=8_000_000):
        """MJPEG from the same session: the viewfinder keeps updating.

        A software H.264 encoder starves the preview on this CPU, so MJPEG is
        recorded and remuxed to MP4 on stop.
        """
        from picamera2.encoders import MJPEGEncoder
        from picamera2.outputs import FileOutput
        if self.picam is None:
            raise RuntimeError("camera not started")
        self._record_raw = None
        self._record_path = None
        self._record_started = None
        raw = path.with_suffix(".mjpeg")
        try:
            config = self._record_config(size)
            with self.camlock:
                picam = self.picam
                picam.stop()
                picam.switch_mode(config)
                picam.start()
                self._apply_crop(size[0] / float(size[1]))
                self._recording = True
                self._encoder = MJPEGEncoder(bitrate=int(bitrate))
                self._output = FileOutput(str(raw))
                self._record_started = time.monotonic()
                picam.start_recording(self._encoder, self._output)
        except Exception:
            self._recording = False
            self._restore_preview()
            raise
        self._record_raw = raw
        self._record_path = path

    def stop_recording(self):
        """Stop the encoder, then remux. Raises RuntimeError if the remux fails,
        so callers can tell the user instead of printing "saved" for a file they
        will never see (the raw .mjpeg is kept in that case)."""
        if self.picam is None:
            return None
        raw = final = None
        try:
            with self.camlock:
                self._recording = False
                picam = self.picam
                try:
                    picam.stop_recording()
                finally:
                    try:
                        picam.stop()
                        picam.switch_mode(self.video_config)
                        picam.start()
                        self._apply_crop()
                    except Exception:
                        self._restore_preview()
        finally:
            raw, final = self._record_raw, self._record_path
            started = self._record_started
            self._record_started = None
            self._record_raw = None
            self._record_path = None
            self._last_frame_duration = None

        if raw is None or not raw.exists():
            return None
        fps = 30.0
        elapsed = max(0.5, time.monotonic() - started) if started else 0.0
        frames = _count_mjpeg_frames(raw)
        if frames and elapsed:
            fps = min(120.0, max(1.0, frames / elapsed))
        with camera_log() as log:
            log.write(("FilmBox remux: %d frames in %.2fs = %.2f fps\n"
                       % (frames, elapsed, fps)).encode())
        ok = False
        try:
            run_command(["ffmpeg", "-y", "-loglevel", "error", "-r", "%.3f" % fps,
                         "-i", str(raw), "-c:v", "copy",
                         "-movflags", "+faststart", str(final)], timeout=90)
            ok = final.exists() and final.stat().st_size > 0
        except Exception as exc:
            with camera_log() as log:
                log.write(("FilmBox remux failed: %s: %s\n"
                           % (type(exc).__name__, exc)).encode())
        if ok:
            raw.unlink(missing_ok=True)
            return final
        # keep the only complete copy; hide the broken container
        broken = final.with_suffix(".partial.mp4")
        if final.exists():
            final.rename(broken)
        with camera_log() as log:
            log.write(("FilmBox raw kept: %s\n" % raw).encode())
        raise RuntimeError("remux failed; raw kept at %s" % raw.name)


def _count_mjpeg_frames(path, chunk_size=65536):
    """Count JPEG SOI markers (0xFF 0xD8) in bounded 64 KB chunks to avoid OOM on long videos."""
    count = 0
    tail = b""
    try:
        with open(path, "rb") as fh:
            while True:
                buf = fh.read(chunk_size)
                if not buf:
                    break
                data = tail + buf
                count += data.count(b"\xff\xd8")
                tail = buf[-1:] if buf[-1:] == b"\xff" else b""
    except (OSError, MemoryError):
        return 0
    return count


def save_gif(frames, mode, output, duration=133):
    if not frames:
        raise RuntimeError("No frames received from camera")
    processed = [filter_image(frame, mode, animated=True).convert("P", palette=Image.Palette.ADAPTIVE,
                   colors=32 if mode != "GBOY" else 2) for frame in frames]
    processed[0].save(output, format="GIF", save_all=True, append_images=processed[1:],
                      duration=duration, loop=0, disposal=2, optimize=False)
    return output


def gif_burst(stream, mode="NORMAL", count=15, sound=True, progress=None):
    # ~2s at 7.5 captured frames per second, independent of faster viewfinder.
    output = reserve_path("GIFs", mode.lower(), "gif")
    click(sound)
    seq, _ = stream.latest()
    frames = []
    started = time.monotonic()
    for index in range(count):
        target_time = started + (index * 2.0 / count)
        delay = target_time - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        seq, jpg = stream.next_frame(seq, timeout=2.5)
        if jpg is None:
            break
        try:
            with Image.open(io.BytesIO(jpg)) as image:
                image.load()
                frames.append(image.convert("RGB"))
        except OSError:
            continue
        if progress:
            progress(len(frames), count)
    if len(frames) < 2:
        raise RuntimeError(f"Camera provided only {len(frames)} GIF frames; see {LOG}")
    save_gif(frames, mode, output)
    return output, len(frames)


class ActiveRecording:
    """State holder for the UI; the encoder itself lives in the stream."""

    def __init__(self, path):
        self.path = path
        self.started = time.monotonic()
        self.proc = None


def _sequential_jpeg_pattern(frames):
    """Return a safe image2 sequence for consecutive numbered JPEGs, if possible."""
    if not frames:
        return None
    import re
    match = re.fullmatch(r"(.*?)([0-9]+)(\.jpe?g)", frames[0].name, re.I)
    if not match:
        return None
    prefix, number, extension = match.groups()
    width = len(number)
    for offset, path in enumerate(frames):
        expected = f"{prefix}{int(number) + offset:0{width}d}{extension}"
        if path.parent != frames[0].parent or path.name != expected:
            return None
    return str(frames[0].parent / f"{prefix}%0{width}d{extension}"), int(number)


def encode_mp4(frames, destination, look="NORMAL", fps=15, size=MOVIE_SIZE, progress=None):
    """Stream JPEGs to FFmpeg without creating a temporary PNG copy.

    NORMAL uses FFmpeg's fast image2 JPEG decoder directly. Other looks are
    processed one frame at a time and streamed to FFmpeg over rawvideo stdin,
    preserving the chosen filter without materializing hundreds of PNGs.
    The destination becomes visible only after FFmpeg finishes successfully.
    Original JPEGs are never modified or deleted.
    """
    frames = [Path(f) for f in frames]
    if not frames:
        raise RuntimeError("Cannot encode video without frames")
    if not shutil.which("ffmpeg"):
        raise RuntimeError("FFmpeg is required: sudo apt install ffmpeg")
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    fps = max(1, min(30, int(fps)))
    w, h = size
    if w <= 0 or h <= 0 or w % 2 or h % 2:
        raise ValueError("Video dimensions must be positive and even")
    partial = destination.with_name(destination.stem + ".partial.mp4")
    count = len(frames)
    sequence = _sequential_jpeg_pattern(frames) if look == "NORMAL" else None

    def announce(stage, n):
        if progress:
            progress(stage, n, count)

    failures = []
    # The fallback encoder is available in minimal FFmpeg builds.
    encoders = (("libx264", ["-c:v", "libx264", "-preset", "ultrafast", "-crf", "26"]),
                ("mpeg4", ["-c:v", "mpeg4", "-q:v", "5"]))
    try:
        for name, encoder in encoders:
            partial.unlink(missing_ok=True)
            base = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-threads", "1"]
            try:
                if sequence:
                    pattern, first = sequence
                    input_args = ["-framerate", str(fps), "-start_number", str(first),
                                  "-i", pattern]
                    vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                          f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=0x0e141f,format=yuv420p")
                    command = base + ["-stats_period", "1", "-progress", "pipe:1"] + input_args + [
                        "-vf", vf, "-threads", "1", "-movflags", "+faststart"] + encoder + [str(partial)]
                    announce("ENCODING", 0)
                    with camera_log() as log:
                        with subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                              stdout=subprocess.PIPE, stderr=log,
                                              text=True, bufsize=1) as proc:
                            for line in proc.stdout:
                                if line.startswith("frame="):
                                    try:
                                        announce("ENCODING", min(count, int(line.split("=", 1)[1])))
                                    except ValueError:
                                        pass
                            code = proc.wait()
                else:
                    # Arbitrary JPG filenames and creative looks stream through a
                    # bounded raw-RGB pipe; only one source JPEG is opened at a time.
                    input_args = ["-f", "rawvideo", "-pix_fmt", "rgb24", "-s:v", f"{w}x{h}",
                                  "-framerate", str(fps), "-i", "pipe:0"]
                    command = base + input_args + ["-pix_fmt", "yuv420p", "-threads", "1",
                                                   "-movflags", "+faststart"] + encoder + [str(partial)]
                    announce("PROCESSING", 0)
                    with camera_log() as log:
                        with subprocess.Popen(command, stdin=subprocess.PIPE,
                                              stdout=subprocess.DEVNULL, stderr=log) as proc:
                            try:
                                for i, filename in enumerate(frames, 1):
                                    with Image.open(filename) as photo:
                                        frame = preview_size(photo, size)
                                    if look != "NORMAL":
                                        frame = filter_image(frame, look, preview=True)
                                    proc.stdin.write(frame.convert("RGB").tobytes())
                                    announce("PROCESSING", i)
                                proc.stdin.close()
                                proc.stdin = None  # Popen context must not flush a closed pipe
                                announce("ENCODING", count)
                                code = proc.wait(timeout=max(120, count * 12))
                            except Exception:
                                proc.kill()
                                proc.communicate()
                                raise
                if code:
                    raise RuntimeError(f"FFmpeg {name} failed (exit {code}); see {LOG}")
                if not partial.exists() or partial.stat().st_size < 1024:
                    raise RuntimeError(f"FFmpeg {name} produced an empty video")
                partial.replace(destination)
                announce("DONE", count)
                with camera_log() as log:
                    log.write(f"FilmBox MP4 export SUCCESS: {count} frames, {destination}\n".encode())
                return destination
            except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
                failures.append(str(exc))
                partial.unlink(missing_ok=True)
        raise RuntimeError("MP4 export failed (JPEG frames preserved): " + "; ".join(failures))
    finally:
        # A failure never leaves a misleading, incomplete .mp4 visible in Gallery.
        partial.unlink(missing_ok=True)


class TimelapseCapture:
    """Manual stop from UI; fixed frame count for CLI. Originals are retained."""
    def __init__(self, interval=5, resolution=(1640,1232), rotation=180, look="NORMAL", fps=15, zoom=1.0):
        self.interval = max(2.0, float(interval))
        self.resolution = resolution
        self.rotation = rotation
        self.look = look
        self.fps = fps
        self.zoom = float(zoom) if zoom in ZOOMS else 1.0
        self.stop_event = threading.Event()
        self.stamp = timestamp()
        self.folder = ROOT / "Timelapse" / ("lapse_" + self.stamp)
        self.output = ROOT / "Timelapse" / ("lapse_" + self.stamp + ".mp4")
        self.frames = []

    def stop(self):
        self.stop_event.set()

    def run(self, count=None, progress=None, export_progress=None):
        if count is not None and not (1 <= count <= 20000):
            raise ValueError("Timelapse frame count must be 1..20000")
        if not shutil.which("rpicam-still"):
            raise RuntimeError("Timelapse requires rpicam-still (sudo apt install rpicam-apps)")
        self.folder.mkdir(parents=True, exist_ok=True)
        n = 0
        roi_args = []
        if self.zoom > 1.0:
            span = 1.0 / self.zoom
            off = (1.0 - span) * 0.5
            roi_args = ["--roi", f"{off:.4f},{off:.4f},{span:.4f},{span:.4f}"]
        tuning_path = resolve_tuning_file()
        tuning_args = ["--tuning-file", str(tuning_path)] if tuning_path is not None else []
        # Scheduling accounts for capture duration so intervals stay accurate without overlapping.
        while not self.stop_event.is_set() and (count is None or n < count):
            frame_started = time.monotonic()
            path = self.folder / f"frame_{n:06d}.jpg"
            w,h = self.resolution
            command = ["rpicam-still", "-n", "-t", "950", "--mode", FULL_STILL_MODE,
                       "--width", str(w), "--height", str(h),
                       "--rotation", str(self.rotation)] + tuning_args + roi_args + ["-o", str(path)]
            run_command(command, timeout=35)
            if path.stat().st_size < 1024:
                raise RuntimeError("Timelapse frame is empty: " + str(path))
            self.frames.append(path)
            n += 1
            if progress:
                progress(n)
            if count is not None and n >= count:
                break
            wait_for = max(0.05, self.interval - (time.monotonic() - frame_started))
            if self.stop_event.wait(wait_for):
                break
        if not self.frames:
            raise RuntimeError("Timelapse stopped without any frames")
        if progress:
            progress(len(self.frames), exporting=True)
        encode_mp4(self.frames, self.output, self.look, self.fps, progress=export_progress)
        return self.output


class StopMotionSession:
    """Capture manually from the running preview with onion-skin framing."""
    def __init__(self, look="NORMAL"):
        self.stamp = timestamp()
        self.folder = ROOT / "StopMotion" / ("frames_" + self.stamp)
        self.output = ROOT / "StopMotion" / ("stopmotion_" + self.stamp + ".mp4")
        self.gif_output = ROOT / "GIFs" / ("stopmotion_" + self.stamp + ".gif")
        self.look = look
        self.frames = []
        self.last_frame = None

    def capture(self, jpg):
        if not jpg:
            raise RuntimeError("No live preview frame to capture")
        with Image.open(io.BytesIO(jpg)) as source:
            image = source.convert("RGB")
        self.folder.mkdir(parents=True, exist_ok=True)
        path = self.folder / f"frame_{len(self.frames):06d}.jpg"
        image.save(path, quality=92)
        self.frames.append(path)
        self.last_frame = image
        return path

    def onion_skin(self, current):
        if self.last_frame is None:
            return current
        previous = preview_size(self.last_frame, current.size)
        return Image.blend(current.convert("RGB"), previous, .34)

    def export(self):
        if len(self.frames) < 2:
            raise RuntimeError("Stop motion needs at least two frames")
        # GIF playback and MP4 both retain the chosen look and source images.
        pictures = []
        for filename in self.frames:
            with Image.open(filename) as img:
                pictures.append(img.convert("RGB"))
        self.gif_output.parent.mkdir(parents=True, exist_ok=True)
        save_gif(pictures, self.look, self.gif_output, duration=125)
        return encode_mp4(self.frames, self.output, self.look, fps=8)


def boomerang_gif(stream, look="CYBERGLITCH", count=12, sound=True, progress=None):
    """Short forward/reverse GIF burst (no separate video or original frames)."""
    output = reserve_path("GIFs", "boomerang_" + look.lower(), "gif")
    click(sound)
    seq, _ = stream.latest()
    frames = []
    start = time.monotonic()
    for i in range(count):
        delay = start + (1.8 * i/count) - time.monotonic()
        if delay > 0:
            time.sleep(delay)
        seq, jpg = stream.next_frame(seq, timeout=2.5)
        if not jpg:
            break
        try:
            with Image.open(io.BytesIO(jpg)) as im:
                frames.append(im.convert("RGB"))
        except OSError:
            continue
        if progress:
            progress(len(frames), count)
    if len(frames) < 3:
        raise RuntimeError("Boomerang needs at least three camera frames")
    bounce = frames + list(reversed(frames[1:-1]))
    save_gif(bounce, look, output, duration=95)
    return output, len(bounce)


# ------------------------ Authenticated Wi-Fi gallery ---------------------

WEB_FOLDERS = ("Photos", "GIFs", "Videos", "Timelapse", "StopMotion")
WEB_EXTENSIONS = frozenset((".jpg", ".jpeg", ".png", ".gif", ".mp4"))


def web_media(root=ROOT):
    root = Path(root)
    found = []
    for name in WEB_FOLDERS:
        directory = root / name
        if directory.is_symlink() or not directory.exists():
            continue
        # Only public output files in each folder, not thousands of timelapse
        # source frames inside per-session subdirectories.
        for path in directory.iterdir():
            if path.is_file() and not path.is_symlink() and path.suffix.lower() in WEB_EXTENSIONS:
                found.append(path)
    return sorted(found, key=lambda p:p.stat().st_mtime_ns, reverse=True)


def safe_web_path(rel, root=ROOT):
    """Only allow files inside recognized media dirs, never links or traversal."""
    root = Path(root).resolve()
    path = Path(rel)
    if path.is_absolute() or len(path.parts) != 2 or path.parts[0] not in WEB_FOLDERS:
        raise ValueError("Invalid media path")
    target = root.joinpath(*path.parts)
    if (root/path.parts[0]).is_symlink() or target.is_symlink() or target.suffix.lower() not in WEB_EXTENSIONS:
        raise ValueError("Unsupported media")
    try:
        resolved = target.resolve(strict=True)
    except OSError:
        raise ValueError("File not found")
    if resolved.parent != (root/path.parts[0]).resolve() or not resolved.is_relative_to(root):
        raise ValueError("Invalid target")
    return resolved


class GalleryHTTP(BaseHTTPRequestHandler):
    server_version = "FilmBox/" + VERSION

    def log_message(self, fmt, *args):
        # Do not log URLs, as they may include the one-time gallery access token.
        return

    def _headers(self, status, content_type, length=None, extra=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' data:; media-src 'self'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'")
        if length is not None:
            self.send_header("Content-Length", str(length))
        for k,v in (extra or {}).items():
            self.send_header(k,v)
        self.end_headers()

    def _authorized(self):
        cookie = cookies.SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except cookies.CookieError:
            return False
        session = cookie.get("filmbox_session")
        return bool(session and hmac.compare_digest(session.value, self.server.gallery_token))

    def do_HEAD(self):
        self._dispatch(send_body=False)

    def do_GET(self):
        self._dispatch(send_body=True)

    def _dispatch(self, send_body=True):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        # One-time token sets an HttpOnly cookie and redirects to URL without token.
        if parsed.path == "/" and "key" in params:
            value = params.get("key", [""])[0]
            if not hmac.compare_digest(value, self.server.gallery_token):
                self._headers(403, "text/plain; charset=utf-8", 0)
                return
            self._headers(303,"text/plain; charset=utf-8",0,
                          {"Location":"/", "Set-Cookie":
                           "filmbox_session="+value+"; HttpOnly; SameSite=Strict; Path=/"})
            return
        if not self._authorized():
            body=b"FilmBox: access token required. Use the URL shown by 'filmbox web'."
            self._headers(403,"text/plain; charset=utf-8",len(body))
            if send_body:
                self.wfile.write(body)
            return
        if parsed.path == "/":
            self._index(send_body=send_body)
        elif parsed.path == "/media":
            self._media(params, send_body=send_body)
        else:
            self._headers(404,"text/plain; charset=utf-8",0)

    def _index(self, send_body=True):
        items = web_media(self.server.media_root)
        try:
            page = max(1, int(urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query).get("page", ["1"])[0]))
        except ValueError:
            page = 1
        per_page = 120
        total_pages = max(1, (len(items) + per_page - 1) // per_page)
        page = min(page, total_pages)
        subset = items[(page - 1) * per_page: page * per_page]
        cards = []
        for item in subset:
            rel = item.relative_to(self.server.media_root).as_posix()
            link = "/media?path=" + urllib.parse.quote(rel, safe="")
            name = html.escape(item.name)
            if item.suffix.lower() == ".mp4":
                art = '<video controls preload="metadata" playsinline src="'+link+'"></video>'
            else:
                art = '<img loading="lazy" alt="'+name+'" src="'+link+'">'
            cards.append('<article>'+art+'<div class="info"><strong>'+name+'</strong><a href="'+link+'&download=1">Download</a></div></article>')
        pager = ""
        if total_pages > 1:
            prev_link = f'<a href="/?page={page - 1}">&larr; Newer</a> ' if page > 1 else ""
            next_link = f' <a href="/?page={page + 1}">Older &rarr;</a>' if page < total_pages else ""
            pager = f'<nav style="padding:10px 5%;text-align:center">{prev_link}<span>Page {page}/{total_pages} ({len(items)} items)</span>{next_link}</nav>'
        html_doc = '''<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>FilmBox Gallery</title><style>body{margin:0;background:#080e18;color:#e8f9fa;font:15px system-ui,sans-serif}header{padding:20px 5%;background:#112134;border-bottom:2px solid #11dddd}h1{margin:0;color:#17f4e4}small{color:#abc}main{padding:15px 5%;display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px}article{background:#132333;border:1px solid #275565;border-radius:10px;overflow:hidden}img,video{width:100%;height:200px;object-fit:contain;background:#070c12}.info{padding:12px;overflow-wrap:anywhere}.info strong{display:block;margin-bottom:8px}a{color:#1bfff0}</style></head><body><header><h1>FILMBOX</h1><small>film camera · Local Wi-Fi Gallery · Downloads</small></header><main>'''+"".join(cards)+'''</main>'''+pager+'''</body></html>'''
        data=html_doc.encode("utf-8")
        self._headers(200,"text/html; charset=utf-8",len(data))
        if send_body:
            self.wfile.write(data)

    def _media(self, params, send_body=True):
        try:
            rel = params.get("path", [""])[0]
            path = safe_web_path(rel,self.server.media_root)
        except (ValueError, OSError):
            self._headers(404,"text/plain; charset=utf-8",0)
            return
        size=path.stat().st_size
        start,end=0,max(0, size-1)
        range_header=self.headers.get("Range", "")
        if range_header and size > 0:
            try:
                if not range_header.startswith("bytes=") or "," in range_header:
                    raise ValueError()
                first,last=range_header[6:].split("-",1)
                if not first:
                    n=int(last)
                    start=max(0,size-n)
                else:
                    start=int(first)
                if last and first:
                    end=min(size-1,int(last))
                if start<0 or start> end or start>=size:
                    raise ValueError()
            except (ValueError,IndexError):
                self._headers(416,"text/plain; charset=utf-8",0,{"Content-Range":f"bytes */{size}"})
                return
        headers={"Accept-Ranges":"bytes"}
        if params.get("download", [""])[0] == "1":
            safe_name = path.name.replace('"', "").replace("\r", "").replace("\n", "")
            headers["Content-Disposition"]='attachment; filename="'+safe_name+'"'
        if range_header and size > 0:
            headers["Content-Range"]=f"bytes {start}-{end}/{size}"
        mime=mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        content_len = (end - start + 1) if size > 0 else 0
        self._headers(206 if (range_header and size > 0) else 200, mime, content_len, headers)
        if not send_body or content_len <= 0:
            return
        try:
            with path.open("rb") as handle:
                handle.seek(start)
                remaining=content_len
                while remaining>0:
                    chunk=handle.read(min(65536,remaining))
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    remaining-=len(chunk)
        except (BrokenPipeError,ConnectionResetError):
            pass


def serve_gallery(bind="127.0.0.1", port=8765, root=ROOT, token=None):
    token=token or secrets.token_urlsafe(20)
    with ThreadingHTTPServer((bind,int(port)), GalleryHTTP) as server:
        server.gallery_token=token
        server.media_root=Path(root).resolve()
        print(f"FilmBox Wi-Fi Gallery on {bind}:{port}",flush=True)
        print(f"Open http://<Cardputer-IP>:{port}/?key={token}",flush=True)
        if bind == "127.0.0.1":
            print(f"For Mac: ssh -L {port}:127.0.0.1:{port} pi0@CardputerZero.local",flush=True)
            print(f"Then open http://127.0.0.1:{port}/?key={token}",flush=True)
        print("Anyone with this URL can access FilmBox media while sharing is on. Press Ctrl+C to stop.",flush=True)
        try:
            server.serve_forever(poll_interval=.2)
        except KeyboardInterrupt:
            pass


def lan_ip():
    """Best-effort LAN address; connecting a UDP socket sends nothing."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("10.255.255.255", 1))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def share_url(port, token):
    return "http://" + lan_ip() + ":" + str(port) + "/?key=" + token


def qr_image(text, box=150, border=2):
    """QR as an RGB image; None when python3-qrcode is not installed."""
    try:
        import qrcode
        from qrcode.constants import ERROR_CORRECT_L
    except ImportError:
        return None
    code = qrcode.QRCode(error_correction=ERROR_CORRECT_L, border=border, box_size=1)
    code.add_data(text)
    code.make(fit=True)
    img = code.make_image(fill_color="black", back_color="white").convert("RGB")
    return img.resize((box, box), Image.Resampling.NEAREST)


class WifiShare:
    """Serve the gallery to the LAN so a phone can scan the screen and download."""

    def __init__(self, root=ROOT, port=8765):
        self.root = Path(root)
        self.port = port
        self.token = secrets.token_urlsafe(9)
        self.server = None
        self.url = None
        self.error = None

    def start(self):
        if self.server is not None:
            self.error = None
            self.url = share_url(self.port, self.token)
            return self.url
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            server = ThreadingHTTPServer(("0.0.0.0", int(self.port)), GalleryHTTP)
            server.gallery_token = self.token
            server.media_root = self.root.resolve()
            threading.Thread(target=server.serve_forever,
                             kwargs={"poll_interval": 0.2}, daemon=True).start()
            self.server = server
            self.error = None
            self.url = share_url(self.port, self.token)
        except OSError as exc:
            self.error = str(exc)[:26]
        return self.url

    def stop(self):
        if self.server is not None:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass
        self.server = None
        self.url = None


def draw_share_overlay(url, width=320, height=170, error=None):
    image = Image.new("RGB", (width, height), (8, 14, 22))
    d = ImageDraw.Draw(image)
    d.rectangle((0, 0, width, 18), fill=(16, 44, 46))
    d.text((6, 3), "WI-FI SHARE", font=font(10, True), fill=MINT)
    d.text((width - 96, 4), "W / ESC  CLOSE", font=font(8), fill=(178, 214, 225))
    if error:
        d.text((8, 42), "SHARE START FAILED", font=font(10, True), fill=(255, 120, 140))
        d.text((8, 62), error, font=font(8), fill=PAPER)
        d.text((8, 88), "Check Wi-Fi is connected and", font=font(8), fill=(190, 210, 220))
        d.text((8, 102), "no other gallery server runs.", font=font(8), fill=(190, 210, 220))
        return image
    host = url.split("//")[-1].split("/?")[0] if url else "no url"
    offline = host.startswith("127.0.0.1:")
    if offline:
        d.text((8, 36), "NO WI-FI CONNECTION", font=font(12, True), fill=(255, 120, 140))
        d.text((8, 58), "Connect Cardputer Zero to Wi-Fi,", font=font(9), fill=PAPER)
        d.text((8, 74), "then press W again to refresh QR.", font=font(9), fill=PAPER)
        d.text((8, 102), url[:44] if url else "", font=font(8), fill=(168, 190, 202))
        return image
    qr = qr_image(url, box=height - 24) if url else None
    if qr is not None:
        image.paste(qr, (4, 20))
        left = qr.width + 12
        d.text((left, 22), "SCAN TO DOWNLOAD", font=font(13, True), fill=MINT)
        d.text((left, 41), host, font=font(13, True), fill=PAPER)
        y = 68
        for line in ("phone on Wi-Fi", "scan a photo", "save or share"):
            d.text((left, y), line, font=font(13), fill=(206, 224, 232))
            y += 20
        d.text((left, y + 1), "token protected", font=font(10), fill=(168, 190, 202))
    else:
        d.text((8, 24), "OPEN URL ON PHONE (NO QR PKG)", font=font(10, True), fill=MINT)
        full = url or "no url"
        d.text((8, 46), full[:42], font=font(9, True), fill=PAPER)
        if len(full) > 42:
            d.text((8, 62), full[42:84], font=font(9, True), fill=PAPER)
        d.text((8, 92), "Install QR support: sudo apt install python3-qrcode",
               font=font(8), fill=(168, 190, 202))
    return image


FBIOGET_VSCREENINFO = 0x4600
FBIOGET_FSCREENINFO = 0x4602
FIX_FMT = "16sL" + "I" * 6 + "HHHI"


class Framebuffer:
    def __init__(self, path="/dev/fb0"):
        self.fd = os.open(path, os.O_RDWR)
        self.mm = None
        try:
            v = bytearray(160)
            fcntl.ioctl(self.fd, FBIOGET_VSCREENINFO, v)
            self.w, self.h, _, _, self.xoff, self.yoff, self.bpp, _ = struct.unpack_from("8I", v)
            f = bytearray(struct.calcsize(FIX_FMT))
            fcntl.ioctl(self.fd, FBIOGET_FSCREENINFO, f)
            parsed = struct.unpack(FIX_FMT, f)
            size = parsed[2]
            # the struct layout varies with alignment, so prefer sysfs
            fbname = Path(path).name or "fb0"
            try:
                with open("/sys/class/graphics/%s/stride" % fbname) as fh:
                    self.stride = int(fh.read())
            except (OSError, ValueError):
                self.stride = (self.xoff + self.w) * 2
            if self.bpp != 16:
                raise RuntimeError(f"FilmBox requires RGB565 framebuffer, found {self.bpp}bpp")
            if self.stride < (self.xoff + self.w) * 2:
                raise RuntimeError("Framebuffer stride too small")
            needed = self.stride * (self.yoff + self.h)
            if size < needed:
                raise RuntimeError("framebuffer map too small: %d < %d" % (size, needed))
            self.mm = mmap.mmap(self.fd, size, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE)
        except Exception:
            os.close(self.fd)
            raise

    def draw(self, image):
        if image.size != (self.w, self.h):
            image = image.resize((self.w, self.h), Image.Resampling.BILINEAR)
        rgb = image.convert("RGB")
        rowbytes = self.w * 2
        if np is not None:
            # Vectorized RGB565 little-endian, avoids ~54k per-pixel Python loops.
            a = np.asarray(rgb, dtype=np.uint8)
            r = a[:, :, 0].astype(np.uint16)
            g = a[:, :, 1].astype(np.uint16)
            b = a[:, :, 2].astype(np.uint16)
            values = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
            out = values.astype("<u2", copy=False).tobytes()
        else:
            px = rgb.tobytes()
            out = bytearray(self.w * self.h * 2)
            for i in range(self.w * self.h):
                pos = i * 3
                r, g, b = px[pos], px[pos + 1], px[pos + 2]
                value = ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
                out[2 * i] = value & 255
                out[2 * i + 1] = (value >> 8) & 255
        if self.stride == rowbytes and self.xoff == 0 and self.yoff == 0:
            self.mm[:len(out)] = out
        else:
            for y in range(self.h):
                offset = (self.yoff + y) * self.stride + self.xoff * 2
                self.mm[offset: offset + rowbytes] = out[y * rowbytes:(y+1) * rowbytes]

    def close(self):
        if self.mm is not None:
            self.mm.close()
        os.close(self.fd)



# Playback is deliberately limited to FilmBox's own media directories.
# Gallery file browsing never modifies or deletes recordings.
GALLERY_DIRS = ("Photos", "GIFs", "Videos", "Timelapse", "StopMotion")
GALLERY_SUFFIXES = frozenset((".jpg", ".jpeg", ".png", ".gif", ".mp4"))
VIDEO_PLAYBACK_FPS = 15


def gallery_files(root=None):
    root = ROOT if root is None else Path(root)
    paths = []
    for folder in GALLERY_DIRS:
        directory = root / folder
        if not directory.is_dir():
            continue
        for path in directory.iterdir():
            if path.is_file() and not path.is_symlink() and path.suffix.lower() in GALLERY_SUFFIXES:
                paths.append(path)
    # Newest first; fall back to filename on identical timestamps.
    return sorted(paths, key=lambda p: (p.stat().st_mtime_ns, p.name), reverse=True)


class VideoPlayback:
    """Decode MP4 to small RGB frames with ffmpeg; never monopolize the UI thread.

    Audio is intentionally disabled because FilmBox records silent video.
    No attempt is made to accelerate decode when hardware APIs are unavailable.
    """
    def __init__(self, path, size=(224, 170), fps=VIDEO_PLAYBACK_FPS):
        self.path = Path(path)
        self.size = size
        self.fps = fps
        self.proc = None
        self.worker = None
        self.stop_event = threading.Event()
        self.paused = False
        self.completed = False
        self.error = None
        self.frame = None
        self.seq = 0
        self.lock = threading.Lock()

    def command(self):
        w, h = self.size
        # scale+pad preserves the full recorded field of view.
        vf = (f"fps={self.fps},scale={w}:{h}:force_original_aspect_ratio=decrease,"
              f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=0x0e141f")
        return ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin",
                "-threads", "2", "-i", str(self.path), "-an", "-vf", vf,
                "-pix_fmt", "rgb24", "-f", "rawvideo", "-y", "pipe:1"]

    def start(self):
        if not shutil.which("ffmpeg"):
            raise RuntimeError("Video playback needs ffmpeg (sudo apt install ffmpeg)")
        cmd = self.command()
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)
        self.worker = threading.Thread(target=self._decode, daemon=True)
        self.worker.start()

    def _decode(self):
        frame_bytes = self.size[0] * self.size[1] * 3
        n = 0
        started = time.monotonic()
        proc = self.proc
        try:
            while not self.stop_event.is_set() and proc is not None and proc.stdout:
                # Pause before read, so a paused view stays on one frame.
                while self.paused and not self.stop_event.is_set():
                    time.sleep(0.03)
                    started = time.monotonic() - n / self.fps
                if self.stop_event.is_set():
                    break
                data = bytearray()
                while len(data) < frame_bytes and not self.stop_event.is_set():
                    part = proc.stdout.read(frame_bytes - len(data))
                    if not part:
                        break
                    data.extend(part)
                if len(data) != frame_bytes:
                    break
                frame = Image.frombytes("RGB", self.size, bytes(data))
                with self.lock:
                    self.frame = frame
                    self.seq += 1
                n += 1
                remaining = started + (n / self.fps) - time.monotonic()
                if remaining > 0:
                    self.stop_event.wait(remaining)
            if not self.stop_event.is_set():
                if n == 0:
                    self.error = "No video frames decoded"
                self.completed = True
        except Exception as exc:
            self.error = str(exc)
            self.completed = True
        finally:
            if proc is not None and proc.poll() is None:
                try:
                    proc.terminate()
                except OSError:
                    pass

    def latest(self):
        with self.lock:
            return self.seq, self.frame

    def stop(self):
        self.stop_event.set()
        proc = self.proc
        if proc is not None:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=2)
            if proc.stdout:
                proc.stdout.close()
        if self.worker is not None and self.worker is not threading.current_thread():
            self.worker.join(timeout=2)
        self.proc = None
        self.worker = None


class Gallery:
    """Local playback state; safe for use from UI loop, no background camera work."""
    def __init__(self, root=None):
        self.items = gallery_files(root)
        self.index = 0
        self.reader = None
        self.video = None
        self.scene = None
        self.playing = True
        self.full = False
        self.next_frame_at = 0.0
        self.seq = 0
        self.message = ""
        self.pending_delete = None
        self.delete_deadline = 0.0
        self.kind = ""
        self._video_seq = -1
        self.select(0)

    def request_delete(self):
        """Two presses: DEL arms, DEL again removes the current file."""
        path = self.path
        if path is None:
            self.message = "NOTHING TO DELETE"
            self.seq += 1
            return False
        now = time.monotonic()
        if self.pending_delete == path and now < self.delete_deadline:
            self.close_current()
            try:
                path.unlink()
            except OSError as exc:
                self.message = "DELETE FAILED: " + str(exc)[:18]
                self.pending_delete = None
                self.seq += 1
                return False
            _cleanup_orphan_frames(path)
            self.items = [item for item in self.items if item != path]
            self.pending_delete = None
            self.index = min(self.index, max(0, len(self.items) - 1))
            self.select(0)
            self.message = "DELETED " + path.name[:20]
            self.seq += 1
            return True
        self.pending_delete = path
        self.delete_deadline = now + 4.0
        self.message = "DEL AGAIN TO DELETE " + path.name[:18]
        self.seq += 1
        return False

    @property
    def path(self):
        if not self.items:
            return None
        return self.items[self.index]

    def close_current(self):
        if self.video is not None:
            self.video.stop()
            self.video = None
        if self.reader is not None:
            self.reader.close()
            self.reader = None

    def close(self):
        self.close_current()

    def select(self, offset):
        self.close_current()
        self.full = False
        self.message = ""
        self._video_seq = -1
        self.playing = True
        self.index = (self.index + offset) % len(self.items) if self.items else 0
        self.scene = None
        path = self.path
        self.kind = path.suffix.lower().lstrip(".") if path else ""
        self.seq += 1
        if path is None:
            self.message = "NO MEDIA - CAPTURE FIRST"
            return
        if self.kind == "mp4":
            self.message = "PRESS 6 TO PLAY"
            return
        try:
            self.reader = Image.open(path)
            self.reader.seek(0)
            self.scene = self.reader.convert("RGB")
            self.next_frame_at = time.monotonic() + self._delay()
        except (OSError, ValueError) as exc:
            self.message = "IMAGE ERROR: " + str(exc)[:20]
            self.close_current()

    def _delay(self):
        if self.reader is None:
            return 0.13
        return max(0.04, min(3, self.reader.info.get("duration", 130) / 1000.0))

    def toggle(self):
        if not self.items:
            return
        if self.kind == "mp4":
            if self.video is None or self.video.completed:
                if self.video is not None:
                    self.video.stop()
                self.video = VideoPlayback(self.path)
                try:
                    self.video.start()
                except (OSError, RuntimeError) as exc:
                    self.message = str(exc)[:35]
                    self.video = None
                    return
                self.message = "PLAYING"
                self.playing = True
            else:
                self.video.paused = not self.video.paused
                self.playing = not self.video.paused
                self.message = "PLAYING" if self.playing else "PAUSED"
        elif self.kind == "gif":
            self.playing = not self.playing
            self.message = "PLAYING" if self.playing else "PAUSED"
        else:
            self.full = not self.full
        self.seq += 1

    def tick(self):
        now = time.monotonic()
        if self.kind == "gif" and self.playing and self.reader is not None and now >= self.next_frame_at:
            try:
                frame_index = (self.reader.tell() + 1) % self.reader.n_frames
                self.reader.seek(frame_index)
                self.scene = self.reader.convert("RGB")
                self.next_frame_at = now + self._delay()
                self.seq += 1
            except (OSError, ValueError, EOFError):
                self.message = "GIF READ ERROR"
                self.playing = False
        if self.kind == "mp4" and self.video is not None:
            seq, frame = self.video.latest()
            if frame is not None and seq != self._video_seq:
                self.scene = frame
                self._video_seq = seq
                self.seq += 1
            if self.video.completed:
                self.message = "FINISHED - 6 REPLAY" if not self.video.error else "VIDEO ERROR"
        return self.scene


def draw_gallery(scene, gallery, width=320, height=170):
    """Gallery lives on the LCD; all buttons stay visible on a 320x170 device."""
    if gallery.full and gallery.kind not in ("gif", "mp4") and scene is not None:
        image = preview_size(scene, (width, height))
        d = ImageDraw.Draw(image)
        d.rectangle((0, height - 16, width, height), fill=(13, 22, 35))
        d.text((6, height - 13), "4 PREV 5 NEXT 6 BACK DEL DELETE 9 CAM", font=font(8), fill=MINT)
        return image
    image = Image.new("RGB", (width, height), (10, 16, 25))
    panel_w = 96 if width >= 290 else max(65, width // 3)
    left_w = width - panel_w
    if scene is not None:
        image.paste(preview_size(scene, (left_w, height)), (0, 0))
    d = ImageDraw.Draw(image)
    d.rectangle((left_w, 0, width, height), fill=(13, 22, 35))
    d.line((left_w, 0, left_w, height), fill=MINT, width=2)
    d.text((left_w+7, 4), "GALLERY", font=font(11, True), fill=MINT)
    if gallery.items:
        d.text((left_w+7, 22), f"{gallery.index+1}/{len(gallery.items)} {gallery.kind.upper()}",
               font=font(9), fill=PAPER)
        name = gallery.path.name
        d.text((left_w+7, 39), name[:14], font=font(8), fill=(175, 190, 198))
        d.text((left_w+7, 50), name[14:28], font=font(8), fill=(175, 190, 198))
    else:
        d.text((6, height//2), "NO SAVED MEDIA", font=font(11, True), fill=PAPER)
    for y, label in ((67, "4 < PREV"), (91, "5 > NEXT")):
        d.rounded_rectangle((left_w+5,y,width-5,y+21), radius=4,
                            fill=(27,36,49), outline=(55,70,80), width=1)
        d.text((left_w+12,y+4), label, font=font(10), fill=PAPER)
    if gallery.kind == "mp4":
        action = "6 PAUSE" if gallery.video and not gallery.video.completed and gallery.playing else "6 PLAY"
    elif gallery.kind == "gif":
        action = "6 PAUSE" if gallery.playing else "6 PLAY"
    else:
        action = "6 FULL"
    d.rounded_rectangle((left_w+5,118,width-5,140), radius=4, fill=MINT)
    d.text((left_w+12,123), action, font=font(9, True), fill=(9,24,28))
    d.text((left_w+7,149), "9 CAMERA", font=font(9, True), fill=(168,215,205))
    if gallery.message:
        d.rectangle((2,height-19,left_w-2,height-2), fill=(17,32,43))
        d.text((6,height-16), gallery.message[:27], font=font(9), fill=MINT)
    return image


def _cleanup_orphan_frames(deleted_path):
    """Remove per-session raw frame dirs once their exported MP4/GIF is deleted."""
    name = deleted_path.stem
    root = deleted_path.parents[1] if len(deleted_path.parents) > 1 else ROOT
    if name.startswith("lapse_"):
        folder = deleted_path.parent / name
        if folder.is_dir() and not folder.is_symlink():
            shutil.rmtree(folder, ignore_errors=True)
    elif name.startswith("stopmotion_"):
        stamp = name[len("stopmotion_"):]
        mp4 = root / "StopMotion" / f"stopmotion_{stamp}.mp4"
        gif = root / "GIFs" / f"stopmotion_{stamp}.gif"
        folder = root / "StopMotion" / f"frames_{stamp}"
        if not mp4.exists() and not gif.exists() and folder.is_dir() and not folder.is_symlink():
            shutil.rmtree(folder, ignore_errors=True)


def get_keyboards():
    if list_devices is None:
        return []
    found = []
    for path in list_devices():
        try:
            dev = InputDevice(path)
            capabilities = dev.capabilities().get(ecodes.EV_KEY, [])
            if ecodes.KEY_ESC in capabilities and ecodes.KEY_ENTER in capabilities:
                found.append(dev)
            else:
                dev.close()
        except (OSError, PermissionError):
            continue
    # Favor Cardputer's TCA8418 keyboard; close unused device handles to avoid FD leaks.
    found.sort(key=lambda d: ("tca8418" not in d.name.lower(), d.path))
    for extra in found[1:]:
        try:
            extra.close()
        except OSError:
            pass
    return found[:1]


def read_keys(devices):
    if not devices:
        return []
    try:
        readable, _, _ = select.select([d.fd for d in devices], [], [], 0)
        keys = []
        for dev in devices:
            if dev.fd in readable:
                for event in dev.read():
                    if event.type == ecodes.EV_KEY and event.value == 1:
                        keys.append(event.code)
        return keys
    except (OSError, BlockingIOError):
        return []


@lru_cache(maxsize=32)
def font(size=12, bold=False):
    base = "/usr/share/fonts/truetype/dejavu/DejaVuSans"
    try:
        return ImageFont.truetype(base + ("-Bold" if bold else "") + ".ttf", size)
    except OSError:
        return ImageFont.load_default()


def capture_action_label(mode, status):
    """Show the actual hardware capture key prominently on the button."""
    if mode == "VIDEO":
        return "6 STOP" if status == "REC" else "6 RECORD"
    if mode == "TIMELAPSE":
        if status.startswith(("STOPPING", "PROCESSING", "ENCODING", "EXPORTING")):
            return "6 WAIT"
        return "6 STOP" if status.startswith("LAPSE") else "6 START"
    if mode == "STOPMOTION":
        return "6 FRAME"
    if mode == "BOOMERANG":
        return "6 LOOP"
    return "6 CAPTURE"


def draw_mode_menu(current="PHOTO", selected=0, width=320, height=170):
    """Seven genuinely selectable tiles, with a prominent Gallery destination.

    6 camera modes in a two-column grid; Gallery spans the full bottom row.
    """
    canvas = Image.new("RGB", (width, height), (9, 16, 28))
    d = ImageDraw.Draw(canvas)
    d.rectangle((0, 0, width, 23), fill=(17, 31, 46))
    d.text((9, 4), "FILMBOX  /  SELECT MODE", font=font(12, True), fill=MINT)
    d.text((width-55, 7), "0 BACK", font=font(8), fill=(175, 195, 209))
    choices = (
        ("PHOTO", "1 PHOTO", "8MP + FILTERS"),
        ("GIF", "2 GIF", "ANIMATED BURST"),
        ("VIDEO", "3 VIDEO", "MP4 RECORDING"),
        ("TIMELAPSE", "4 TIMELAPSE", "FRAMES TO MP4"),
        ("STOPMOTION", "5 STOP MOTION", "ONION-SKIN FRAMES"),
        ("BOOMERANG", "6 BOOMERANG", "FORWARD + REVERSE"),
    )
    gutter, gap = 8, 8
    tile_w = (width - 2*gutter - gap)//2
    for index, (name, title, subtitle) in enumerate(choices):
        col, row = index%2, index//2
        x = gutter + col*(tile_w+gap)
        y = 26 + row*33
        active = index == selected
        bg = (31, 83, 83) if active else (24, 38, 55)
        outline = MINT if active else (57, 77, 96)
        d.rounded_rectangle((x, y, x+tile_w-1, y+29), radius=5,
                            fill=bg, outline=outline, width=2 if active else 1)
        d.text((x+9, y+2), title, font=font(10, active), fill=PAPER if active else (205, 225, 233))
        d.text((x+9, y+18), subtitle, font=font(8), fill=(164, 220, 210) if active else (156, 177, 188))
        if name == current:
            d.ellipse((x+tile_w-15, y+5, x+tile_w-9, y+11), fill=MAGENTA)
    active = selected == len(MODES)
    d.rounded_rectangle((gutter, 127, width-gutter, 150), radius=6,
                        fill=(79, 40, 104) if active else (36, 35, 70),
                        outline=MINT if active else (191, 110, 224), width=2 if active else 1)
    # Graphic album icon signals a different destination from the camera modes.
    d.rounded_rectangle((18, 132, 32, 144), radius=2, outline=MINT, width=2)
    d.ellipse((21, 134, 25, 138), fill=MINT)
    d.line((20, 142, 24, 139, 27, 141, 30, 137), fill=MINT, width=1)
    d.text((40, 131), "9 GALLERY", font=font(11, True), fill=PAPER)
    d.text((139, 134), "VIEW PHOTOS / GIFS / VIDEO", font=font(9), fill=(199, 216, 238))
    d.rectangle((0, height-17, width, height), fill=(13, 26, 38))
    d.text((9, height-14), "ARROWS MOVE  |  ENTER SELECT  |  0 BACK", font=font(8), fill=(190, 218, 222))
    return canvas


class LookPicker:
    """Full-screen look chooser: built-in filters plus film LUTs, with preview."""

    ROWS = 8
    ROW_H = 18

    def __init__(self, current="NORMAL", frame=None):
        self.looks = list(refresh_filters())
        self.index = self.looks.index(current) if current in self.looks else 0
        self.offset = 0
        self.frame = frame
        self._cache = {}

    @property
    def value(self):
        return self.looks[self.index]

    def move(self, delta):
        if not self.looks:
            return
        self.index = (self.index + delta) % len(self.looks)

    def jump_lut(self):
        for i, name in enumerate(self.looks):
            if name.startswith(LUT_PREFIX):
                self.index = i
                return True
        return False

    @staticmethod
    def label(value):
        if not value.startswith(LUT_PREFIX):
            return value
        name = value[len(LUT_PREFIX):]
        for brand in ("look_", "kodak_", "fuji_", "agfa_", "ilford_", "lomography_", "cinestill_"):
            if name.startswith(brand):
                name = name[len(brand):]
                break
        return "LUT " + name.replace("_", " ")

    def preview(self):
        if self.frame is None:
            return None
        key = self.value
        if key not in self._cache:
            try:
                self._cache[key] = filter_image(self.frame, key, preview=True)
            except Exception:
                self._cache[key] = self.frame
        return self._cache[key]

    def render(self, width=320, height=170):
        image = Image.new("RGB", (width, height), (10, 14, 22))
        d = ImageDraw.Draw(image)
        d.rectangle((0, 0, width, 16), fill=(16, 44, 46))
        luts = sum(1 for f in self.looks if f.startswith(LUT_PREFIX))
        d.text((6, 2), "LOOKS  " + str(self.index + 1) + "/" + str(len(self.looks)),
               font=font(10, True), fill=MINT)
        d.text((width - 128, 3), "ENTER PICK  ESC BACK", font=font(8), fill=(178, 214, 225))
        if self.index < self.offset:
            self.offset = self.index
        if self.index >= self.offset + self.ROWS:
            self.offset = self.index - self.ROWS + 1
        list_w = 164
        for row in range(self.ROWS):
            i = self.offset + row
            if i >= len(self.looks):
                break
            y = 18 + row * self.ROW_H
            active = i == self.index
            is_lut = self.looks[i].startswith(LUT_PREFIX)
            d.rectangle((0, y, list_w, y + self.ROW_H - 1),
                        fill=(31, 82, 76) if active else (15, 23, 33))
            text = (" " + self.label(self.looks[i]))[:24]
            d.text((5, y + 3), text, font=font(9, active),
                   fill=PAPER if active else (166, 188, 200))
            d.text((list_w - 26, y + 4), "LUT" if is_lut else "BASE", font=font(8),
                   fill=(250, 165, 95) if is_lut else (110, 138, 152))
        px, py = list_w + 2, 18
        pw, ph = width - px - 2, height - py - 2
        d.rectangle((px, py, width - 1, height - 1), fill=(9, 13, 20), outline=MINT)
        prev = self.preview()
        if prev is not None:
            image.paste(fill_size(prev, (pw - 2, ph - 2)), (px + 1, py + 1))
        label = self.label(self.value)[:22]
        d.rectangle((px + 1, height - 15, width - 2, height - 2), fill=(10, 18, 26))
        d.text((px + 4, height - 14), label, font=font(8, True), fill=MINT)
        return image


HELP_LEFT = (("5", "LOOKS"), ("L", "LUTS"), ("Z", "ZOOM"),
             ("T", "CLEAN UI"), ("W", "SHARE QR"), ("0", "MODES"))
HELP_RIGHT = (("1", "PHOTO"), ("2", "GIF"), ("3", "VIDEO"),
              ("4", "LAPSE"), ("9", "GALLERY"), ("6", "CAPTURE"))


def draw_help(width=320, height=170):
    image = Image.new("RGB", (width, height), (8, 14, 22))
    d = ImageDraw.Draw(image)
    d.rectangle((0, 0, width, 18), fill=(16, 44, 46))
    d.text((6, 2), "KEYS", font=font(12, True), fill=MINT)
    key_font = font(11, True)
    label_font = font(10)
    row = 22
    for col, rows in ((4, HELP_LEFT), (width // 2 + 2, HELP_RIGHT)):
        y = 23
        for key, label in rows:
            d.rounded_rectangle((col, y, col + 24, y + 16), radius=4, fill=(30, 84, 78))
            tw = d.textlength(key, font=key_font)
            d.text((col + (24 - tw) / 2, y + 1), key, font=key_font, fill=MINT)
            d.text((col + 30, y + 3), label, font=label_font, fill=(214, 230, 238))
            y += row
    d.text((6, height - 15), "7 size    8 sound    H / ESC close",
           font=font(9), fill=(158, 184, 196))
    return image


def draw_film_overlay(percent, label="", width=320, height=170, note=""):
    """Full-screen film-copy screen: the viewfinder is frozen while rendering."""
    image = Image.new("RGB", (width, height), (6, 10, 16))
    d = ImageDraw.Draw(image)
    d.text((10, 8), "FILM COPY", font=font(14, True), fill=MINT)
    if label:
        d.text((10, 30), label[:30], font=font(12), fill=(186, 210, 222))
    bar_x, bar_y, bar_w = 10, 60, width - 20
    d.rounded_rectangle((bar_x, bar_y, bar_x + bar_w, bar_y + 14), 7,
                        fill=(22, 32, 44), outline=(40, 62, 74), width=1)
    filled = max(0, min(100, percent)) * bar_w // 100
    if filled:
        d.rounded_rectangle((bar_x, bar_y, bar_x + filled, bar_y + 14), 7, fill=MINT)
    d.text((10, 82), "%d%%" % percent, font=font(22, True), fill=PAPER)
    if note:
        note_color = MINT if note == "GPU RENDERING" else (250, 195, 95)
        d.text((10, 114), note[:34], font=font(11, True), fill=note_color)
    d.text((10, height - 24), "viewfinder frozen", font=font(10), fill=(140, 168, 182))
    return image


def draw_ui(scene, mode="PHOTO", style="NORMAL", resolution="8MP", status="READY",
            sound=True, recording_seconds=0, width=320, height=170, fps=0, clean=False,
            zoom=None):
    """Final camera UI: Photo/GIF/Video/Lapse + a full-width 9 GALLERY button.

    Gallery is a prominent action rather than a tiny keyboard hint. The
    viewfinder remains as wide as v0.5.1, preserving full-sensor framing.
    """
    if style.startswith(LUT_PREFIX):
        style = LookPicker.label(style)[:22]
    image = Image.new("RGB", (width, height), (10, 16, 25))
    panel_w = 26 if clean else (99 if width >= 290 else max(74, width//3))
    panel_x = width - panel_w
    if scene is not None:
        # full-bleed viewfinder; the control panel is drawn translucent on top
        image.paste(fill_size(scene, (width, height)), (0, 0))
    if scene is not None:
        strip = image.crop((panel_x, 0, width, height))
        image.paste(Image.blend(strip, Image.new("RGB", strip.size, (8, 14, 24)),
                                0.34 if clean else 0.62), (panel_x, 0))
    else:
        d0 = ImageDraw.Draw(image)
        d0.rectangle((panel_x, 0, width, height), fill=(13, 22, 35))
    view_w = panel_x
    d = ImageDraw.Draw(image)
    d.line((view_w, 0, view_w, height), fill=MINT, width=1 if clean else 2)
    if clean:
        # slim rail: numbers only, more transparent
        f12 = font(12, True)

        def centred(text, y, colour, fnt=None):
            fnt = fnt or f12
            tw = d.textlength(text, font=fnt)
            d.text((view_w + (panel_w - tw) / 2, y), text, font=fnt, fill=colour)

        for i, (value, label) in enumerate((("PHOTO", "1"), ("GIF", "2"),
                                            ("VIDEO", "3"), ("TIMELAPSE", "4"))):
            active = mode == value
            if active:
                d.rectangle((view_w + 1, 20 + i * 22, width - 1, 40 + i * 22),
                            fill=(30, 74, 68))
            centred(label, 23 + i * 22, MINT if active else (150, 175, 188))
        centred("9", 110, (196, 126, 226))
        centred("6", 132, MINT)
        centred("%d" % fps, height - 12, (150, 175, 188), font(7))
    else:
        d.text((view_w+7, 2), "FILMBOX", font=font(10, True), fill=MINT)
        d.text((view_w+7, 16), "0 ALL MODES", font=font(8, True), fill=(176, 211, 235))
    for i, (value, label) in enumerate((
        ("PHOTO", "1 PHOTO"), ("GIF", "2 GIF"),
        ("VIDEO", "3 VIDEO"), ("TIMELAPSE", "4 TIMELAPSE"),
    )):
        if clean:
            break
        y = 29 + i*18
        active = mode == value
        d.rounded_rectangle((view_w+5, y, width-5, y+16), radius=4,
                            fill=(36, 98, 91) if active else (27, 36, 49),
                            outline=MINT if active else (50, 70, 82), width=1)
        d.text((view_w+12, y+2), label, font=font(9, active),
               fill=PAPER if active else (183, 204, 216))
    # High-contrast, one-press Gallery action. No nested menus needed.
    if not clean:
        d.rounded_rectangle((view_w+5, 104, width-5, 127), radius=5,
                            fill=(52, 40, 85), outline=(185, 100, 225), width=2)
        d.text((view_w+12, 108), "9 GALLERY", font=font(11, True), fill=PAPER)
    recording_active = mode == "VIDEO" and status == "REC"
    if not clean:
        d.rounded_rectangle((view_w+5, 132, width-5, 154), radius=5,
                            fill=MAGENTA if recording_active else MINT)
        d.text((view_w+10, 136), capture_action_label(mode, status),
               font=font(9, True), fill=(9, 24, 28))
        d.text((view_w+7, 158), f"{fps:.0f}FPS {'SND' if sound else 'MUTE'}",
               font=font(8), fill=(177, 199, 204))
    if mode == "TIMELAPSE":
        detail, hint = f"LAPSE: EVERY {resolution}", "7 INTERVAL  |  6 START/STOP"
    elif mode == "STOPMOTION":
        detail, hint = f"STOP MOTION: {resolution}", "6 FRAME  |  7 EXPORT"
    elif mode == "BOOMERANG":
        detail, hint = "BOOMERANG", "5 LOOK  |  6 LOOP"
    elif mode == "VIDEO":
        detail, hint = f"VIDEO: {resolution}", "7 SIZE  |  6 RECORD/STOP"
    elif mode == "PHOTO":
        head = f"{zoom:g}x {style}" if zoom else style
        detail, hint = f"{head} · {resolution}", (
            "5 LOOKS Z ZOOM T UI H HELP" if any(f.startswith(LUT_PREFIX) for f in FILTERS)
            else "5 LOOK  |  7 SIZE")
    else:
        detail, hint = style, "5 LOOK  |  6 CAPTURE"
    blend_rect(image, (2, 1, view_w - 2, 17), (14, 30, 43), 0.62)
    d.text((6, 3), detail[:34], font=font(9, True), fill=MINT)
    if status == "REC":
        d.rounded_rectangle((5, 22, min(view_w-5, 114), 44), radius=4, fill=(100, 21, 39))
        d.ellipse((12, 29, 20, 37), fill=(255, 66, 89))
        d.text((25, 27), f"REC {recording_seconds}s", font=font(11, True), fill=PAPER)
    elif scene is None:
        d.text((10, height//2), "STARTING CAMERA", font=font(10), fill=PAPER)
    blend_rect(image, (2, height-17, view_w-2, height-2), (17, 32, 43), 0.62)
    d.text((6, height-15), hint[:34], font=font(8), fill=(186, 220, 230))
    if status not in ("READY", "REC"):
        d.rectangle((2, height-34, view_w-2, height-18), fill=(17, 32, 43))
        d.text((6, height-32), status[:31], font=font(9, True),
               fill=MAGENTA if status.startswith(("ERR", "CAM ERR")) else MINT)
    return image


class FilmBoxUI:
    def __init__(self):
        self.settings = load_settings()
        self.mode = "PHOTO"
        self.mode_menu = False
        self.mode_selected = 0
        self.gallery = None
        self.status = "READY"
        self.stream = CameraStream(rotation=self.settings["rotation"], fps=self.settings["preview_fps"])
        self.stream.zoom = float(self.settings.get("zoom", ZOOMS[0]))
        self.recording = None
        self.timelapse = None
        self.timelapse_exporting = False
        self.stopmotion = None
        self.display = None
        self.devices = []
        self.busy = False
        self.preview = None
        self.last_seq = -1
        self.running = True
        self.status_deadline = 0
        self.worker = None
        self.fps = 0.0
        self.share = None
        self.share_overlay = False
        self.picker = None
        self.help_open = False
        self.film_busy = False
        self.film_worker = None
        self.gpu_worker = None
        self._cam_error_seen = None
        self._cam_err_streak = 0
        self._cam_ok_streak = 0
        self.film_progress = None
        self.film_label = ""
        self.film_note = ""
        self._settings_dirty = False
        self._settings_flushed = 0.0
        self.frame_counter = 0
        self.fps_time = time.monotonic()

    def touch_settings(self):
        """Mark settings dirty; the main loop flushes at most once a second so a
        held arrow key cannot fsync the SD card on every key press."""
        self._settings_dirty = True

    def flush_settings(self, force=False):
        if not self._settings_dirty:
            return
        now = time.monotonic()
        if not force and now - self._settings_flushed < 1.0:
            return
        save_settings(self.settings)
        self._settings_dirty = False
        self._settings_flushed = now

    def safe_draw(self, image, tag="screen"):
        try:
            self.display.draw(image)
        except Exception as exc:
            with camera_log() as log:
                log.write(f"FilmBox {tag} error: {type(exc).__name__}: {exc}\n".encode())
            try:
                self.set_status("DISP ERR - CHECK LOG", 3)
            except Exception:
                pass
            time.sleep(0.05)

    def set_status(self, value, delay=3):
        self.status = value
        self.status_deadline = time.monotonic() + delay

    def _colour_fix(self):
        return colour_fix_spec(self.settings)

    def colour_fix_job(self, original, spec):
        """Apply IMX219 colour fix to a NORMAL photo in the background without freezing UI."""
        cf = _normalize_colour_fix(spec)
        if not _colour_fix_active(cf) or self.film_busy:
            return
        self.film_busy = True

        def work():
            try:
                _apply_colour_fix(original, cf)
            finally:
                self.film_busy = False

        self.film_worker = threading.Thread(target=work, daemon=True)
        self.film_worker.start()

    def film_job(self, original, look):
        """Render the filter copy in its own thread, serialized to protect RAM and camera state."""
        if self.film_busy:
            self.set_status("FILM BUSY - ONE AT A TIME", 4)
            return
        cf = self._colour_fix()
        self.film_busy = True
        if not look.startswith(LUT_PREFIX):
            # Built-in filters run at bounded resolution: no need to freeze the viewfinder,
            # while still serializing with film_busy so rapid bursts cannot OOM 512 MB RAM.
            def quick():
                try:
                    filtered_still(original, look, colour_fix=cf)
                    self.set_status("SAVED +FILTER", 4)
                except Exception as exc:
                    with camera_log() as log:
                        log.write(f"FilmBox filter error: {type(exc).__name__}: {exc}\n".encode())
                    self.set_status("FILTER ERR " + str(exc)[:24], 8)
                finally:
                    self.film_busy = False
            self.film_worker = threading.Thread(target=quick, daemon=True)
            self.film_worker.start()
            return
        was_live = bool(getattr(self.stream, "running", False))
        want = RESOLUTION_LABELS[self.settings.get("resolution", 0)]
        # The viewfinder is about to be frozen, which frees ~16 MB, so a frame
        # that looks tight right now may still make full resolution.
        tight = available_mb() < 130 and self.settings.get("resolution", 0) == 0
        label = (LookPicker.label(look) + "  " + want +
                 ("  (LOW MEM -> 2MP)" if tight else ""))
        self.film_progress = 0
        self.film_label = label
        self.film_note = (
            "LOADING GPU... (FIRST SHOT)"
            if (self.gpu_worker is not None and self.gpu_worker.is_warming())
            else ""
        )

        def on_progress(percent):
            self.film_progress = max(0, min(100, percent))

        def on_note(text):
            self.film_note = text

        def render():
            try:
                if was_live:
                    # Freezing the viewfinder gives the render ~14 % more CPU and
                    # about 16 MB of RAM, which is what lets 8 MP succeed.
                    try:
                        self.stream.stop()
                    except Exception:
                        pass
                full_res = self.settings.get("lut_full_res", True)
                output_path = None
                if (self.gpu_worker is not None
                        and os.environ.get("FILMBOX_STREAM", "0") != "1"):
                    cube_file = resolve_lut_path(look[len(LUT_PREFIX):])
                    output_path = self.gpu_worker.render(
                        original, look, cube_file,
                        full_res=full_res,
                        progress=on_progress,
                        on_status=on_note,
                        colour_fix=cf,
                    )
                    if output_path is not None:
                        on_progress(100)
                if output_path is None:
                    output_path = filtered_still(original, look, progress=on_progress,
                                                 full_res=full_res, colour_fix=cf)
                output_name = output_path.name if output_path is not None else ""
                try:
                    with Image.open(original.with_name(output_name)) as probe:
                        pixels = probe.width * probe.height
                except Exception:
                    pixels = 0
                size_tag = ("%dMP" % round(pixels / 1e6)) if pixels else "FILM"
                self.set_status("SAVED +FILM " + size_tag, 5)
            except Exception as exc:
                with camera_log() as log:
                    log.write(f"FilmBox film error: {type(exc).__name__}: {exc}\n".encode())
                self.set_status("FILM ERR " + str(exc)[:26], 10)
            finally:
                self.film_progress = None
                self.film_note = ""
                self.film_busy = False
                if was_live and self.running and self.gallery is None and self.timelapse is None:
                    try:
                        self.stream.start()
                    except Exception:
                        pass
        self.film_worker = threading.Thread(target=render, daemon=True)
        self.film_worker.start()

    def do_photo(self):
        def operation():
            # The stream owns the camera now; capture_still switches config
            # internally and hands the live view straight back.
            reclaim_memory()
            original = reserve_path("Photos", "8mp" if self.settings["resolution"] == 0
                                    else "photo", "jpg")
            click(self.settings["sound"])
            self.stream.capture_still(
                original, RESOLUTIONS[self.settings["resolution"]])
            self.set_status("SAVED " + original.name[:24], 3)
            look = self.settings["filter"]
            if look != "NORMAL":
                self.film_job(original, look)
            else:
                cf = self._colour_fix()
                if _colour_fix_active(cf):
                    self.colour_fix_job(original, cf)
        self.job(operation, "CAPTURING...")

    def do_gif(self):
        def operation():
            path, count = gif_burst(self.stream, self.settings["filter"],
                progress=lambda n, total: self.set_status(f"GIF {n}/{total}", 8),
                sound=self.settings["sound"])
            self.set_status(f"GIF SAVED {count} FRAMES", 4)
        self.job(operation, "GIF 0/15")

    def do_boomerang(self):
        def operation():
            path, count = boomerang_gif(self.stream, self.settings["filter"],
                sound=self.settings["sound"],
                progress=lambda n, total:self.set_status(f"LOOP {n}/{total}", 10))
            self.set_status(f"BOOM SAVED {count}F", 5)
        self.job(operation, "BOOMERANG...")

    def do_stopmotion_frame(self):
        if self.stopmotion is None:
            self.stopmotion = StopMotionSession(self.settings["filter"])
        else:
            self.stopmotion.look = self.settings["filter"]
        seq, jpg = self.stream.latest()
        try:
            self.stopmotion.capture(jpg)
            click(self.settings["sound"])
            self.set_status(f"FRAME {len(self.stopmotion.frames)} - 7 EXPORT", 6)
        except (OSError, RuntimeError) as exc:
            self.set_status("ERR " + str(exc)[:26], 5)

    def export_stopmotion(self):
        if self.stopmotion is None or len(self.stopmotion.frames) < 2:
            self.set_status("NEED AT LEAST 2 FRAMES", 4)
            return
        self.stopmotion.look = self.settings["filter"]
        session = self.stopmotion
        def operation():
            path = session.export()
            self.stopmotion = None
            self.set_status("SAVED STOP MOTION MP4+GIF", 6)
        self.job(operation, "EXPORTING MP4 + GIF...")

    def do_timelapse(self):
        interval = TIMELAPSE_INTERVALS[self.settings["timelapse_interval"]]
        self.timelapse = TimelapseCapture(interval=interval,
                    resolution=RESOLUTIONS[self.settings["resolution"]],
                    rotation=self.settings["rotation"],
                    look=self.settings["filter"],
                    zoom=self.settings.get("zoom", 1.0))
        session = self.timelapse
        def capture_progress(n, exporting=False):
            if exporting:
                self.timelapse_exporting = True
                self.set_status("ENCODING LAPSE MP4...", float("inf"))
            else:
                self.set_status(f"LAPSE {n}F - 6 STOP", float("inf"))

        def export_progress(stage, done, total):
            if stage == "DONE":
                return
            self.timelapse_exporting = True
            self.set_status(f"{stage} {done}/{total}", float("inf"))

        def operation():
            self.flush_settings(force=True)
            self.stream.stop()
            try:
                session.run(progress=capture_progress, export_progress=export_progress)
                self.set_status("SAVED TIMELAPSE MP4", 8)
            finally:
                self.timelapse = None
                self.timelapse_exporting = False
                self.stream.start()
        self.job(operation, "LAPSE STARTING...")

    def start_recording(self):
        if self.busy:
            return
        self.busy = True
        try:
            w, h, fps, bitrate = VIDEOS[self.settings["video"]]
            path = reserve_path("Videos", "video", "mp4")
            click(self.settings["sound"])
            self.stream.start_recording(path, size=(w, h), bitrate=bitrate)
            self.recording = ActiveRecording(path)
            self.status = "REC"
        except Exception as exc:
            self.recording = None
            self.set_status("ERR " + str(exc)[:37], 7)
            self.stream.start()
        finally:
            self.busy = False

    def stop_recording(self):
        def operation():
            current = self.recording
            try:
                path = self.stream.stop_recording()
                click(self.settings["sound"])
                if path is not None:
                    self.set_status("VIDEO SAVED " + path.name[:20], 5)
            except Exception as exc:
                with camera_log() as log:
                    log.write(f"FilmBox remux error: {exc}\n".encode())
                self.set_status("ERR REMUX FAILED - RAW KEPT", 10)
            finally:
                self.recording = None
        self.job(operation, "FINALIZING MP4...")

    def job(self, task, headline):
        if self.busy:
            return
        self.busy = True
        self.set_status(headline, 30)
        def wrapper():
            try:
                task()
            except Exception as exc:
                with camera_log() as log:
                    log.write(f"FilmBox worker error: {type(exc).__name__}: {exc}\n".encode())
                self.set_status("ERR " + str(exc)[:36], 12)
                # A failed render can leave the camera in its still configuration;
                # start() returns early when picam exists, so force a restore.
                if self.recording is None:
                    try:
                        self.stream._restore_preview()
                    except Exception:
                        pass
            finally:
                self.busy = False
        self.worker = threading.Thread(target=wrapper, daemon=True)
        self.worker.start()

    def enter_gallery(self):
        if self.recording is not None:
            self.set_status("STOP VIDEO FIRST", 3)
            return
        if self.busy or self.film_busy:
            if self.timelapse_exporting:
                self.set_status("EXPORTING MP4 - PLEASE WAIT", float("inf"))
            elif self.timelapse is not None:
                self.set_status("STOP TIMELAPSE FIRST", 4)
            elif self.film_busy:
                self.set_status("RENDERING FILM - PLEASE WAIT", 4)
            else:
                self.set_status("BUSY - PLEASE WAIT", 4)
            return
        self.stream.stop()
        self.gallery = Gallery()
        self.preview = None
        self.last_seq = -1

    def leave_gallery(self):
        if self.gallery is None:
            return
        self.gallery.close()
        self.gallery = None
        self.preview = None
        self.last_seq = -1
        self.fps_time = time.monotonic()
        self.frame_counter = 0
        self.stream.start()

    def select_mode(self, value):
        """Switch mode without starting a capture. Preserve all media/settings."""
        if value not in MODES:
            raise ValueError(value)
        self.mode = value
        self.mode_selected = MODES.index(value)
        self.mode_menu = False
        self.set_status("READY", .1)

    def open_picker(self, target_lut=False):
        frame = None
        if self.preview is not None and self.display is not None:
            pw = max(64, self.display.w - 168)
            ph = max(64, self.display.h - 20)
            frame = fill_size(self.preview, (pw, ph))
            cf = self._colour_fix()
            if _colour_fix_active(cf) and self.mode == "PHOTO":
                try:
                    frame = _fix_magenta_band(frame, cf)
                except Exception:
                    pass
        self.picker = LookPicker(self.settings["filter"], frame)
        if target_lut and not self.picker.jump_lut():
            self.picker = None
            self.set_status("NO LUT FILES - PUT .cube IN LUTS DIR", 4)

    def key(self, code):
        if self.help_open:
            if code in (ecodes.KEY_H, ecodes.KEY_ESC, ecodes.KEY_Q):
                self.help_open = False
            return
        # While 8MP film LUT render freezes the viewfinder (stream stopped), block
        # actions that would touch the stopped camera or terminate mid-render.
        if self.film_progress is not None:
            if code == ecodes.KEY_H:
                self.help_open = True
            elif code in (ecodes.KEY_8, ecodes.KEY_S):
                self.settings["sound"] = not self.settings["sound"]
                self.touch_settings()
                click(self.settings["sound"])
            return
        if self.picker is not None:
            if code in (ecodes.KEY_ESC, ecodes.KEY_Q, ecodes.KEY_5, ecodes.KEY_F):
                self.picker = None
            elif code in (ecodes.KEY_UP, ecodes.KEY_DOWN):
                self.picker.move(-1 if code == ecodes.KEY_UP else 1)
            elif code in (ecodes.KEY_LEFT, ecodes.KEY_RIGHT):
                self.picker.move(-self.picker.ROWS if code == ecodes.KEY_LEFT
                                 else self.picker.ROWS)
            elif code == ecodes.KEY_L:
                self.picker.jump_lut()
            elif code in (ecodes.KEY_ENTER, ecodes.KEY_SPACE):
                self.settings["filter"] = self.picker.value
                self.touch_settings()
                self.set_status("LOOK " + LookPicker.label(self.picker.value)[:22], 3)
                self.picker = None
            return
        if self.share_overlay:
            if code in (ecodes.KEY_ESC, ecodes.KEY_W, ecodes.KEY_Q):
                self.share_overlay = False
                self.set_status("SHARE STILL ON - W TO REOPEN", 3)
            return
        if self.gallery is not None:
            if code in (ecodes.KEY_ESC, ecodes.KEY_Q, ecodes.KEY_9):
                self.leave_gallery()
            elif code in (ecodes.KEY_4, ecodes.KEY_LEFT):
                self.gallery.select(-1)
            elif code in (ecodes.KEY_5, ecodes.KEY_RIGHT):
                self.gallery.select(1)
            elif code in (ecodes.KEY_ENTER, ecodes.KEY_SPACE, ecodes.KEY_6):
                self.gallery.toggle()
            elif code == ecodes.KEY_DELETE:
                self.gallery.request_delete()
            return
        # Key 6 interrupts timelapse even while its worker is marked busy.
        if self.timelapse is not None and code in (ecodes.KEY_6, ecodes.KEY_ENTER, ecodes.KEY_SPACE):
            if not self.timelapse_exporting:
                self.timelapse.stop()
                self.set_status("STOPPING LAPSE...", float("inf"))
            return
        if self.mode_menu:
            if code in (ecodes.KEY_ESC, ecodes.KEY_Q, ecodes.KEY_0, ecodes.KEY_M):
                self.mode_menu = False
            elif code in (ecodes.KEY_LEFT, ecodes.KEY_RIGHT, ecodes.KEY_UP, ecodes.KEY_DOWN, ecodes.KEY_TAB):
                if code == ecodes.KEY_LEFT:
                    self.mode_selected = (self.mode_selected - 1) % len(MENU_CHOICES)
                elif code in (ecodes.KEY_RIGHT, ecodes.KEY_TAB):
                    self.mode_selected = (self.mode_selected + 1) % len(MENU_CHOICES)
                elif code == ecodes.KEY_UP:
                    self.mode_selected = 4 if self.mode_selected == 6 else max(0, self.mode_selected - 2)
                else:
                    self.mode_selected = 6 if self.mode_selected >= 4 else self.mode_selected + 2
            elif code in (ecodes.KEY_1, ecodes.KEY_2, ecodes.KEY_3,
                          ecodes.KEY_4, ecodes.KEY_5, ecodes.KEY_6):
                number = (ecodes.KEY_1, ecodes.KEY_2, ecodes.KEY_3,
                          ecodes.KEY_4, ecodes.KEY_5, ecodes.KEY_6).index(code)
                self.select_mode(MODES[number])
            elif code in (ecodes.KEY_ENTER, ecodes.KEY_SPACE):
                if self.mode_selected == len(MODES):
                    self.mode_menu = False
                    self.enter_gallery()
                else:
                    self.select_mode(MODES[self.mode_selected])
            elif code == ecodes.KEY_9:
                self.mode_menu = False
                self.enter_gallery()
            return
        if code == ecodes.KEY_9:
            self.enter_gallery()
            return
        if code in (ecodes.KEY_ESC, ecodes.KEY_Q):
            if not self.busy and not self.film_busy:
                self.running = False
            elif self.film_busy:
                self.set_status("SAVING FILTER - PLEASE WAIT", 3)
            return
        if self.busy:
            return
        if code in (ecodes.KEY_ENTER, ecodes.KEY_SPACE, ecodes.KEY_6):
            if self.mode == "PHOTO":
                self.do_photo()
            elif self.mode == "GIF":
                self.do_gif()
            elif self.mode == "VIDEO":
                if self.recording:
                    self.stop_recording()
                else:
                    self.start_recording()
            elif self.mode == "TIMELAPSE":
                self.do_timelapse()
            elif self.mode == "STOPMOTION":
                self.do_stopmotion_frame()
            elif self.mode == "BOOMERANG":
                self.do_boomerang()
            return
        # Never switch the camera mode during an active MP4 recording.
        if self.recording:
            return
        if code in (ecodes.KEY_0, ecodes.KEY_M):
            self.mode_menu = True
            self.mode_selected = MODES.index(self.mode)
        elif code in (ecodes.KEY_1, ecodes.KEY_2, ecodes.KEY_3, ecodes.KEY_4,
                      ecodes.KEY_P, ecodes.KEY_G, ecodes.KEY_V):
            value = {ecodes.KEY_1: "PHOTO", ecodes.KEY_2: "GIF",
                     ecodes.KEY_3: "VIDEO", ecodes.KEY_4: "TIMELAPSE",
                     ecodes.KEY_P: "PHOTO", ecodes.KEY_G: "GIF",
                     ecodes.KEY_V: "VIDEO"}[code]
            self.select_mode(value)
        elif code in (ecodes.KEY_UP, ecodes.KEY_DOWN, ecodes.KEY_TAB):
            direction = -1 if code == ecodes.KEY_UP else 1
            self.select_mode(MODES[(MODES.index(self.mode) + direction) % len(MODES)])
        elif code == ecodes.KEY_L:
            self.open_picker(target_lut=True)
        elif code in (ecodes.KEY_5, ecodes.KEY_F):
            self.open_picker()
        elif code in (ecodes.KEY_RIGHT, ecodes.KEY_LEFT):
            direction = -1 if code == ecodes.KEY_LEFT else 1
            i = FILTERS.index(self.settings["filter"])
            self.settings["filter"] = FILTERS[(i + direction) % len(FILTERS)]
            self.touch_settings()
        elif code in (ecodes.KEY_7, ecodes.KEY_R):
            if self.mode == "STOPMOTION":
                self.export_stopmotion()
                return
            if self.mode == "TIMELAPSE":
                field, count = "timelapse_interval", len(TIMELAPSE_INTERVALS)
            else:
                field = "video" if self.mode == "VIDEO" else "resolution"
                count = len(VIDEOS) if field == "video" else len(RESOLUTIONS)
            self.settings[field] = (self.settings[field] + 1) % count
            self.touch_settings()
        elif code == ecodes.KEY_W:
            if self.share is None:
                self.share = WifiShare()
            self.share.start()
            self.share_overlay = True
        elif code == ecodes.KEY_H:
            self.help_open = True
        elif code == ecodes.KEY_Z:
            try:
                i = ZOOMS.index(float(self.settings.get("zoom", ZOOMS[0])))
            except ValueError:
                i = 0
            zoom = ZOOMS[(i + 1) % len(ZOOMS)]
            self.settings["zoom"] = zoom
            if self.stream is not None:
                self.stream.set_zoom(zoom)
            self.touch_settings()
            self.set_status(f"{zoom:g}X", 2)
        elif code == ecodes.KEY_T:
            self.settings["clean_ui"] = not self.settings.get("clean_ui", False)
            self.touch_settings()
        elif code in (ecodes.KEY_8, ecodes.KEY_S):
            self.settings["sound"] = not self.settings["sound"]
            self.touch_settings()
            click(self.settings["sound"])

    def run(self, gallery_only=False):
        def _shutdown(signum, frame):
            self.running = False

        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(sig, _shutdown)
            except (ValueError, OSError):
                pass
        try:
            self.display = Framebuffer()
            self.devices = get_keyboards()
            if not self.devices:
                raise RuntimeError("No evdev keyboard accessible; see README permissions")
            if gallery_only:
                self.gallery = Gallery()
            else:
                # Light up the LCD immediately (<50 ms), spawn the GPU worker in
                # the background (its own process main thread owns EGL), and go
                # straight into the live viewfinder without a 2-3 s GPU stall.
                self.safe_draw(draw_ui(
                    None, self.mode, self.settings["filter"],
                    RESOLUTION_LABELS[self.settings["resolution"]],
                    "STARTING CAMERA", self.settings["sound"], 0,
                    width=self.display.w, height=self.display.h, fps=0,
                    clean=self.settings.get("clean_ui", False),
                    zoom=self.settings.get("zoom", 1.0)), "boot")
                if _ACCEL in ("auto", "gpu"):
                    self.gpu_worker = GpuWorker()
                    self.gpu_worker.start()
                self.stream.start()
            last_draw = 0.0
            last_status = None
            while self.running:
                for code in read_keys(self.devices):
                    try:
                        self.key(code)
                    except Exception as exc:
                        with camera_log() as log:
                            log.write(f"FilmBox key error: {type(exc).__name__}: {exc}\n".encode())
                        self.set_status("ERR " + str(exc)[:32], 6)
                stream_error = self.stream.error if self.stream else None
                if stream_error:
                    self._cam_ok_streak = 0
                    self._cam_err_streak += 1
                    if self._cam_err_streak == 3:
                        with camera_log() as log:
                            log.write(f"FilmBox camera error: {stream_error}\n".encode())
                        self._cam_error_seen = stream_error
                        self.set_status("CAM ERR " + stream_error[:28], float("inf"))
                    elif self._cam_error_seen and not self.status.startswith("CAM ERR"):
                        # sticky: a key press must not hide a broken camera
                        self.set_status("CAM ERR " + self._cam_error_seen[:28], float("inf"))
                else:
                    self._cam_err_streak = 0
                    self._cam_ok_streak += 1
                    if self._cam_error_seen and self._cam_ok_streak >= 5:
                        self._cam_error_seen = None
                        self.set_status("CAM OK", 2)
                now = time.monotonic()
                if self.help_open:
                    if last_status != "HELP":
                        self.safe_draw(draw_help(self.display.w, self.display.h), "help")
                        last_draw = now
                        last_status = "HELP"
                    time.sleep(0.05)
                    continue
                if self.picker is not None:
                    key = ("picker", self.picker.index)
                    if last_status != key:
                        self.safe_draw(self.picker.render(self.display.w, self.display.h), "picker")
                        last_draw = now
                        last_status = key
                    time.sleep(0.05)
                    continue
                if self.share_overlay:
                    if last_status != "SHARE":
                        self.safe_draw(draw_share_overlay(
                            self.share.url if self.share else None,
                            self.display.w, self.display.h,
                            self.share.error if self.share else None), "share")
                        last_draw = now
                        last_status = "SHARE"
                    time.sleep(0.05)
                    continue
                if self.gallery is not None:
                    scene = self.gallery.tick()
                    gallery_state = ("gallery", self.gallery.seq, self.gallery.message)
                    if gallery_state != last_status or now - last_draw > 0.3:
                        self.safe_draw(draw_gallery(scene, self.gallery,
                                                      width=self.display.w, height=self.display.h))
                        last_draw = now
                        last_status = gallery_state
                    time.sleep(0.02)
                    continue
                if self.mode_menu:
                    menu_state = ("modes", self.mode, self.mode_selected)
                    if menu_state != last_status or now - last_draw > 2.0:
                        self.safe_draw(draw_mode_menu(self.mode, self.mode_selected,
                                           width=self.display.w, height=self.display.h),
                                           "modes")
                        last_draw = now
                        last_status = menu_state
                    time.sleep(0.03)
                    continue
                if self.film_progress is not None:
                    film_state = ("film", self.film_progress, self.film_label, self.film_note)
                    if film_state != last_status or now - last_draw > 0.25:
                        self.safe_draw(draw_film_overlay(self.film_progress, self.film_label,
                                                         width=self.display.w, height=self.display.h,
                                                         note=self.film_note),
                                       "film")
                        last_draw = now
                        last_status = film_state
                    self.flush_settings()
                    time.sleep(0.03)
                    continue
                if self.status != "REC" and not self.busy and now > self.status_deadline:
                    self.status = "READY"
                self.flush_settings()
                seq, jpg = self.stream.latest()
                new_frame = seq != self.last_seq and jpg is not None
                if new_frame:
                    try:
                        with Image.open(io.BytesIO(jpg)) as im:
                            self.preview = im.convert("RGB")
                        self.last_seq = seq
                        self.frame_counter += 1
                    except OSError:
                        new_frame = False
                if now - self.fps_time >= 1.0:
                    self.fps = self.frame_counter / max(.001, now - self.fps_time)
                    self.frame_counter = 0
                    self.fps_time = now
                # Only redraw when the camera provides a frame or UI state changes.
                # While recording, draw clock twice a second, not 8x unnecessarily.
                state = (self.status, self.mode, self.settings["filter"],
                         self.settings["resolution"], self.settings["video"],
                         self.settings["timelapse_interval"], self.settings["sound"],
                         self.settings.get("clean_ui", False), self.settings.get("zoom", 1.0),
                         self.settings.get("colour_fix", 2),
                         len(self.stopmotion.frames) if self.stopmotion else 0)
                if not new_frame and state == last_status and now - last_draw < (0.5 if self.recording else 1.0):
                    time.sleep(0.012)
                    continue
                last_draw = now
                last_status = state
                mode = self.mode
                style = self.settings["filter"]
                if self.preview is not None and self.recording is None:
                    # Filter only LCD-size frames. 15 FPS is a target, not guaranteed.
                    scene = fill_size(self.preview, (self.display.w, self.display.h))
                    if mode == "STOPMOTION" and self.stopmotion is not None:
                        scene = self.stopmotion.onion_skin(scene)
                    cf = self._colour_fix()
                    if _colour_fix_active(cf) and mode == "PHOTO":
                        try:
                            scene = _fix_magenta_band(scene, cf)
                        except Exception:
                            pass
                    if style != "NORMAL" and mode != "VIDEO":
                        try:
                            scene = filter_image(scene, style, preview=True)
                        except Exception:
                            pass
                else:
                    scene = self.preview
                w, h = RESOLUTIONS[self.settings["resolution"]]
                if mode == "VIDEO":
                    w, h, fps, bitrate = VIDEOS[self.settings["video"]]
                    label = VIDEO_LABELS[self.settings["video"]]
                elif mode == "TIMELAPSE":
                    label = str(TIMELAPSE_INTERVALS[self.settings["timelapse_interval"]]) + "s"
                elif mode == "STOPMOTION":
                    label = str(len(self.stopmotion.frames) if self.stopmotion else 0) + "F"
                else:
                    label = (RESOLUTION_LABELS[self.settings["resolution"]]
                             if mode == "PHOTO" else "15F/2S")
                elapsed = int(now - self.recording.started) if self.recording and self.recording.started else 0
                live_status = "REC" if self.recording is not None else self.status
                frame_image = draw_ui(scene, mode, style, label,
                        live_status, self.settings["sound"], elapsed,
                        width=self.display.w, height=self.display.h, fps=self.fps,
                        clean=self.settings.get("clean_ui", False),
                        zoom=self.settings.get("zoom", 1.0))
                self.safe_draw(frame_image, "camera")
                time.sleep(0.008)
        finally:
            self.flush_settings(force=True)
            if self.share is not None:
                self.share.stop()
                self.share = None
            if self.gallery is not None:
                self.gallery.close()
                self.gallery = None
            if self.recording is not None:
                try:
                    self.stream.stop_recording()
                except Exception:
                    pass
            self.stream.stop()
            if self.worker and self.worker.is_alive():
                self.worker.join(timeout=15)
            if self.film_worker and self.film_worker.is_alive():
                self.film_worker.join(timeout=15)
            if self.gpu_worker is not None:
                self.gpu_worker.stop()
                self.gpu_worker = None
            for dev in self.devices:
                dev.close()
            if self.display:
                self.display.close()


def command_diagnose():
    print("FilmBox", VERSION, "diagnostic")
    print("Device:", sys.platform, os.uname().machine if hasattr(os, "uname") else "unknown")
    for command in ("rpicam-still", "rpicam-vid", "aplay", "ffmpeg"):
        print(f"{command}:", shutil.which(command) or "MISSING")
    for name in ("/dev/fb0", "/dev/input", str(SHUTTER)):
        print(name, "OK" if Path(name).exists() else "MISSING")
    print("evdev:", "available" if InputDevice else "missing: sudo apt install python3-evdev")
    try:
        import picamera2
        print("picamera2:", getattr(picamera2, "__version__", "available"))
    except ImportError:
        print("picamera2: MISSING - sudo apt install python3-picamera2")
    print("numpy acceleration:", "available" if np is not None else "unavailable")
    print("film-copy engine:", _ACCEL, "|", _accel_status())
    settings = load_settings()
    tuning_path = resolve_tuning_file()
    print("isp tuning:", str(tuning_path) if tuning_path is not None else "system default (vc4/imx219.json)")
    print("colour fix:", settings.get("colour_fix", 2), colour_fix_spec(settings))
    print("settings:", settings)
    print("full-field camera mode (preview/video):", FULL_FOV_MODE)
    print("video presets:", VIDEO_LABELS)
    print("output:", ROOT)
    print("log:", LOG)
    return 0


def command_demo():
    """Offline demonstration without requiring a device or camera."""
    d = Image.new("RGB", (640, 480), (16, 29, 44))
    draw = ImageDraw.Draw(d)
    for x in range(0, 640, 13):
        draw.line((x, 0, (x + 210) % 640, 480), fill=(x % 120 + 40, 120, 145), width=1)
    draw.ellipse((173, 53, 467, 346), fill=(50, 204, 156), outline=(240, 240, 240), width=4)
    draw.rectangle((238, 158, 400, 257), fill=(18, 39, 55))
    draw.text((263, 197), "CYBER", font=font(22, True), fill=PAPER)
    path = Path.cwd() / "filmbox_ui_demo.png"
    draw_ui(d).save(path)
    gifpath = Path.cwd() / "filmbox_gboy_demo.gif"
    frames = [ImageEnhance.Brightness(d).enhance(0.65+i/20) for i in range(15)]
    save_gif(frames, "GREEN", gifpath)
    print("Created", path, "and", gifpath)
    return 0


def main(argv=None):
    raw_args = sys.argv[1:] if argv is None else list(argv)
    if raw_args == ["_gpu-worker"]:
        return command_gpu_worker()
    refresh_filters()
    parser = argparse.ArgumentParser(description="FilmBox for Cardputer Zero")
    parser.add_argument("command", nargs="?", choices=["ui", "gallery", "photo", "gif", "video", "timelapse", "boomerang", "web", "demo", "diagnose", "config"], default="ui")
    parser.add_argument("--filter", choices=FILTERS, default="NORMAL")
    parser.add_argument("--resolution", choices=[f"{w}x{h}" for w, h in RESOLUTIONS], default="3280x2464")
    parser.add_argument("--seconds", type=int, default=8, help="CLI video duration, 1-600 seconds")
    parser.add_argument("--no-sound", action="store_true")
    parser.add_argument("--rotation", type=int, choices=(0, 180), default=None,
                        help="Photo, GIF and video orientation (0 or 180)")
    parser.add_argument("--lut-full", choices=["on", "off"], default=None,
                        help="Film copy at full 8MP (on, default) or 2MP (off)")
    parser.add_argument("--colour-fix", type=int, choices=(0, 1, 2, 3), default=None,
                        help="Magenta sky correction level 0=off..3=high (default 2)")
    parser.add_argument("--preview-fps", type=int, choices=(12, 15, 20, 24, 25, 30), default=None,
                        help="Set preview target FPS with the config command")
    parser.add_argument("--video-quality", choices=("wide", "720p", "1080p", "480p"), default="wide")
    parser.add_argument("--frames", type=int, default=12, help="Timelapse frame count, 1..20000")
    parser.add_argument("--interval", type=float, default=5, help="Timelapse interval seconds, minimum 2")
    parser.add_argument("--fps", type=int, default=15, help="Timelapse output FPS, 1..30")
    parser.add_argument("--bind", default="127.0.0.1", help="Gallery listen address; 0.0.0.0 allows LAN access")
    parser.add_argument("--port", type=int, default=8765, help="Gallery HTTP port")
    args = parser.parse_args(argv)
    try:
        settings = load_settings()
        if args.command == "config":
            if args.rotation is not None:
                settings["rotation"] = args.rotation
            if args.preview_fps is not None:
                settings["preview_fps"] = args.preview_fps
            if args.lut_full is not None:
                settings["lut_full_res"] = args.lut_full == "on"
            if args.colour_fix is not None:
                settings["colour_fix"] = args.colour_fix
            if (args.rotation is not None or args.preview_fps is not None
                    or args.lut_full is not None or args.colour_fix is not None):
                save_settings(settings)
            print(json.dumps(settings, indent=2))
            return 0
        rotation = settings["rotation"] if args.rotation is None else args.rotation
        if args.command == "demo":
            return command_demo()
        if args.command == "diagnose":
            return command_diagnose()
        if args.command == "gallery":
            return FilmBoxUI().run(gallery_only=True) or 0
        if args.command == "web":
            if not (1024 <= args.port <= 65535):
                parser.error("--port must be 1024..65535")
            serve_gallery(bind=args.bind, port=args.port)
            return 0
        if args.command == "photo":
            if _ACCEL in ("auto", "gpu") and args.filter.startswith(LUT_PREFIX):
                try:
                    _gpu_ready(1, max(available_mb(), 200))
                except Exception:
                    pass
            dims = tuple(map(int, args.resolution.split("x")))
            full_res = settings.get("lut_full_res", True) if args.lut_full is None else (args.lut_full == "on")
            cf_idx = settings.get("colour_fix", 2) if args.colour_fix is None else args.colour_fix
            original, filtered = take_photo(
                args.filter, dims, not args.no_sound, rotation,
                full_res=full_res, colour_fix=COLOUR_FIX_PRESETS[cf_idx],
            )
            print("Original:", original)
            if filtered:
                print("Filtered:", filtered)
            return 0
        if args.command == "gif":
            s = CameraStream(rotation=rotation, fps=settings["preview_fps"])
            try:
                s.start()
                seq, first = s.next_frame(s.seq, timeout=8)
                if first is None:
                    raise RuntimeError(f"No camera preview frames; see {LOG}")
                path, count = gif_burst(s, args.filter, sound=not args.no_sound)
                print(f"GIF ({count} frames):", path)
            finally:
                s.stop()
            return 0
        if args.command == "timelapse":
            if not (2 <= args.interval <= 1800 and 1 <= args.frames <= 20000 and 1 <= args.fps <= 30):
                parser.error("Timelapse --interval 2..1800; --frames 1..20000; --fps 1..30")
            session = TimelapseCapture(interval=args.interval,
                         resolution=tuple(map(int,args.resolution.split("x"))),
                         rotation=rotation, look=args.filter, fps=args.fps,
                         zoom=settings.get("zoom", 1.0))
            path = session.run(count=args.frames, progress=lambda n,exporting=False:
                    print("Encoding MP4..." if exporting else f"Captured {n}/{args.frames}",flush=True))
            print("Timelapse MP4:",path,"Original frames:",session.folder)
            return 0
        if args.command == "boomerang":
            stream = CameraStream(rotation=rotation,fps=settings["preview_fps"])
            try:
                stream.start()
                _, jpg = stream.next_frame(stream.seq,timeout=8)
                if not jpg:
                    raise RuntimeError("Camera preview unavailable")
                path,count = boomerang_gif(stream,args.filter,sound=not args.no_sound)
                print(f"Boomerang ({count} frames):",path)
            finally:
                stream.stop()
            return 0
        if args.command == "video":
            if not (1 <= args.seconds <= 600):
                parser.error("--seconds must be 1..600")
            index = {"wide": 0, "1080p": 1, "720p": 2, "480p": 3}[args.video_quality]
            w, h, _fps, bitrate = VIDEOS[index]
            path = reserve_path("Videos", "video", "mp4")
            stream = CameraStream(rotation=rotation, fps=settings["preview_fps"])
            stream.zoom = float(settings.get("zoom", ZOOMS[0]))
            stream.start()
            try:
                click(not args.no_sound)
                stream.start_recording(path, size=(w, h), bitrate=bitrate)
                print(f"Recording {args.seconds}s at {args.video_quality}; saving to {path}...")
                time.sleep(args.seconds)
            finally:
                saved = None
                try:
                    saved = stream.stop_recording()
                except Exception as exc:
                    print("Remux FAILED:", exc)
                stream.stop()
            if saved is None:
                return 1
            print("Saved:", saved)
            return 0
        return FilmBoxUI().run() or 0
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"FilmBox ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
