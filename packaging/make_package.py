#!/usr/bin/env python3
"""Build an architecture-independent Debian/Raspberry Pi OS package for FilmBox."""
from pathlib import Path
import math
import random
import shutil
import struct
import subprocess
import wave

project = Path(__file__).resolve().parents[1]
root = project / "packaging" / "stage"
if root.exists():
    shutil.rmtree(root)

(root / "DEBIAN").mkdir(parents=True)
(root / "usr/lib/filmbox").mkdir(parents=True)
(root / "usr/share/filmbox/luts").mkdir(parents=True)
(root / "usr/bin").mkdir(parents=True)
(root / "usr/share/APPLaunch/applications").mkdir(parents=True)
(root / "usr/share/APPLaunch/share/images").mkdir(parents=True)

src_script = project / "filmbox.py"
if not src_script.exists():
    src_script = project / "src" / "filmbox.py"
shutil.copy2(src_script, root / "usr/lib/filmbox/filmbox.py")

for cube in sorted((project / "luts").glob("*.cube")):
    shutil.copy2(cube, root / "usr/share/filmbox/luts" / cube.name)
if (project / "luts" / "README.md").exists():
    shutil.copy2(project / "luts" / "README.md", root / "usr/share/filmbox/luts/README.md")

(root / "usr/bin/filmbox").write_text(
    '#!/bin/sh\nexec /usr/bin/python3 /usr/lib/filmbox/filmbox.py "$@"\n',
    encoding="utf-8",
)
(root / "usr/bin/filmbox").chmod(0o755)

(root / "DEBIAN/control").write_text(
    """Package: filmbox
Version: 0.9.0
Section: graphics
Priority: optional
Architecture: all
Maintainer: HIBIKI <DD-HIBIKI@users.noreply.github.com>
Depends: python3, python3-pil, python3-evdev, python3-numpy, python3-picamera2, alsa-utils, ffmpeg
Recommends: python3-qrcode, rpicam-apps
Description: FilmBox - film-simulation camera for Cardputer Zero
 8MP photos, 18 film LUTs, 4-step zoom, GIF/MP4, timelapse,
 gallery and QR-code Wi-Fi sharing. Uses the IMX219 camera,
 RGB565 framebuffer and evdev keyboard.
""",
    encoding="utf-8",
)

(root / "usr/share/APPLaunch/applications/filmbox.desktop").write_text(
    """[Desktop Entry]
Type=Application
Name=FilmBox
Comment=Film camera: 18 film looks, zoom, QR export
Exec=/usr/bin/filmbox ui
Icon=share/images/filmbox.png
Terminal=false
Categories=Graphics;
StartupNotify=false
""",
    encoding="utf-8",
)

icon_src = project / "packaging" / "icon.png"
if not icon_src.exists():
    icon_src = project / "icon-100.png"
shutil.copy2(icon_src, root / "usr/share/APPLaunch/share/images/filmbox.png")

shutter_src = project / "shutter.wav"
if shutter_src.exists():
    shutil.copy2(shutter_src, root / "usr/lib/filmbox/shutter.wav")
else:
    # Synthetic, original short two-pulse shutter sound, no third-party audio.
    rate = 24000
    random.seed(37)
    samples = []
    for i in range(int(rate * 0.16)):
        t = i / rate
        one = math.exp(-52 * t) * (0.68 * math.sin(2 * math.pi * 900 * t) + 0.25 * random.uniform(-1, 1))
        s = max(0.0, t - 0.055)
        two = (
            math.exp(-48 * s) * (0.55 * math.sin(2 * math.pi * 1180 * s) + 0.17 * random.uniform(-1, 1))
            if t >= 0.055
            else 0.0
        )
        value = max(-1.0, min(1.0, (one + two) * 0.58))
        samples.append(int(value * 32767))
    with wave.open(str(root / "usr/lib/filmbox/shutter.wav"), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(rate)
        f.writeframes(struct.pack("<%dh" % len(samples), *samples))

for item in root.rglob("*"):
    if item.is_dir():
        item.chmod(0o755)
    elif item != root / "usr/bin/filmbox":
        item.chmod(0o644)
(root / "DEBIAN").chmod(0o755)
(root / "DEBIAN/control").chmod(0o644)

output = project.parent / "filmbox_0.9.0_all.deb"
if shutil.which("dpkg-deb"):
    subprocess.run(["dpkg-deb", "--build", "--root-owner-group", str(root), str(output)], check=True)
    print(output)
else:
    print(f"Staged package tree at {root} (dpkg-deb not found on this host)")
