#!/usr/bin/env python3
"""Build an architecture-independent Debian package for FilmBox."""
from pathlib import Path
import math
import shutil
import subprocess
import struct
import wave
import random

project = Path(__file__).resolve().parents[1]
root = project / 'packaging' / 'stage'
if root.exists():
    shutil.rmtree(root)
(root / 'DEBIAN').mkdir(parents=True)
(root / 'usr/lib/filmbox').mkdir(parents=True)
(root / 'usr/bin').mkdir(parents=True)
(root / 'usr/share/APPLaunch/applications').mkdir(parents=True)
(root / 'usr/share/APPLaunch/share/images').mkdir(parents=True)
shutil.copy2(project / 'src/filmbox.py', root / 'usr/lib/filmbox/filmbox.py')
(root / 'usr/bin/filmbox').write_text('#!/bin/sh\nexec /usr/bin/python3 /usr/lib/filmbox/filmbox.py "$@"\n')
(root / 'usr/bin/filmbox').chmod(0o755)
(root / 'DEBIAN/control').write_text('''Package: filmbox
Version: 0.6.7
Section: graphics
Priority: optional
Architecture: all
Maintainer: HIBIKI <DD-HIBIKI@users.noreply.github.com>
Depends: python3, python3-pil, python3-evdev, python3-numpy, alsa-utils, ffmpeg
Description: FilmBox - film-simulation camera for Cardputer Zero
 8MP photos, 18 film LUTs, 4-step zoom, GIF/MP4, timelapse,
 gallery and QR-code Wi-Fi sharing. Uses the IMX219 camera,
 RGB565 framebuffer and evdev keyboard.
''')
(root / 'usr/share/APPLaunch/applications/filmbox.desktop').write_text('''[Desktop Entry]
Type=Application
Name=FilmBox
Comment=Film camera: 18 film looks, zoom, QR export
Exec=/usr/bin/filmbox ui
Icon=share/images/filmbox.png
Terminal=false
Categories=Graphics;
StartupNotify=false
''')

shutil.copy2(project / 'packaging' / 'icon.png', root / 'usr/share/APPLaunch/share/images/filmbox.png')

# Synthetic, original short two-pulse shutter sound, no third-party audio.
rate = 24000
random.seed(37)
samples = []
for i in range(int(rate * 0.16)):
    t = i/rate
    one = math.exp(-52*t) * (0.68*math.sin(2*math.pi*900*t) + 0.25*random.uniform(-1,1))
    s = max(0, t-0.055)
    two = math.exp(-48*s) * (0.55*math.sin(2*math.pi*1180*s) + 0.17*random.uniform(-1,1)) if t >= 0.055 else 0
    value = max(-1,min(1,(one+two)*0.58))
    samples.append(int(value*32767))
with wave.open(str(root / 'usr/lib/filmbox/shutter.wav'), 'wb') as f:
    f.setnchannels(1)
    f.setsampwidth(2)
    f.setframerate(rate)
    f.writeframes(struct.pack('<%dh' % len(samples), *samples))

for folder in root.rglob('*'):
    if folder.is_dir():
        folder.chmod(0o755)
(root / 'DEBIAN').chmod(0o755)
(root / 'DEBIAN/control').chmod(0o644)
output = project.parent / 'filmbox_0.6.7_all.deb'
subprocess.run(['dpkg-deb', '--build', '--root-owner-group', str(root), str(output)], check=True)
print(output)
