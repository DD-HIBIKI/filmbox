#!/bin/sh
# Install FilmBox + 18 film LUTs on the Cardputer Zero (Raspberry Pi OS).
# Run ON the device, from the directory that holds filmbox.py and luts/.
set -e
SRC=$(cd "$(dirname "$0")" && pwd)
LIBDIR=/usr/lib/filmbox
APP="$LIBDIR/filmbox.py"
SYS_LUTDIR=/usr/share/filmbox/luts
USER_LUTDIR="$HOME/.config/filmbox/luts"
LEGACY_APP=/usr/lib/cybercam/cybercam.py

echo "== 1/4 backup existing install"
STAMP=$(date +%Y%m%d-%H%M%S)
if [ -f "$APP" ]; then
  sudo cp -n "$APP" "$APP.bak-$STAMP" && echo "   backed up $APP"
fi
if [ -f "$LEGACY_APP" ]; then
  sudo cp -n "$LEGACY_APP" "$LEGACY_APP.bak-$STAMP" && echo "   backed up legacy $LEGACY_APP"
fi

echo "== 2/4 install FilmBox app"
sudo mkdir -p "$LIBDIR" /usr/bin
sudo cp "$SRC/filmbox.py" "$APP"
sudo chmod 644 "$APP"
if [ -f "$SRC/shutter.wav" ]; then
  sudo cp "$SRC/shutter.wav" "$LIBDIR/shutter.wav"
  sudo chmod 644 "$LIBDIR/shutter.wav"
fi
printf '#!/bin/sh\nexec /usr/bin/python3 /usr/lib/filmbox/filmbox.py "$@"\n' | sudo tee /usr/bin/filmbox >/dev/null
sudo chmod 755 /usr/bin/filmbox

# Keep legacy APPLaunch entry working if the user had CyberCam installed instead of filmbox.deb
if [ -d /usr/lib/cybercam ]; then
  sudo cp "$SRC/filmbox.py" "$LEGACY_APP"
  sudo chmod 644 "$LEGACY_APP"
  if [ -f "$SRC/shutter.wav" ]; then
    sudo cp "$SRC/shutter.wav" /usr/lib/cybercam/shutter.wav
  fi
fi
if [ -d /usr/share/APPLaunch/applications ] && [ ! -f /usr/share/APPLaunch/applications/filmbox.desktop ]; then
  sudo mkdir -p /usr/share/APPLaunch/share/images
  if [ -f "$SRC/icon-100.png" ]; then
    sudo cp "$SRC/icon-100.png" /usr/share/APPLaunch/share/images/filmbox.png
  fi
  cat <<'EOF' | sudo tee /usr/share/APPLaunch/applications/filmbox.desktop >/dev/null
[Desktop Entry]
Type=Application
Name=FilmBox
Comment=Film camera: 18 film looks, zoom, QR export
Exec=/usr/bin/filmbox ui
Icon=share/images/filmbox.png
Terminal=false
Categories=Graphics;
StartupNotify=false
EOF
fi

echo "== 3/4 install LUT pack"
sudo mkdir -p "$SYS_LUTDIR"
mkdir -p "$USER_LUTDIR"
sudo cp "$SRC"/luts/*.cube "$SYS_LUTDIR"/ 2>/dev/null || true
cp "$SRC"/luts/*.cube "$USER_LUTDIR"/ 2>/dev/null || true
COUNT=$(ls -1 "$USER_LUTDIR"/*.cube 2>/dev/null | wc -l)
echo "   $COUNT LUTs in $USER_LUTDIR"

echo "== 4/4 dependencies (needs network if missing)"
for pkg in python3-picamera2 python3-numpy python3-pil python3-evdev alsa-utils ffmpeg python3-qrcode rpicam-apps; do
  if dpkg -s "$pkg" >/dev/null 2>&1; then
    echo "   $pkg ok"
  else
    sudo apt-get install -y "$pkg" >/dev/null 2>&1 && echo "   $pkg installed" \
      || echo "   $pkg MISSING - install later: sudo apt install $pkg"
  fi
done

# Stop any running instance so APPLaunch starts the new code cleanly
pkill -f '/usr/lib/filmbox/filmbox.py' 2>/dev/null || true
pkill -f '/usr/lib/cybercam/cybercam.py' 2>/dev/null || true
echo
echo "Done. Reopen FilmBox from APPLaunch (or run: filmbox ui)."
echo "  L / 5  = look picker     W = Wi-Fi share QR     7 = resolution     Z = zoom"
