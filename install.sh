#!/bin/sh
# Install the patched CyberCam + film LUT pack on the Cardputer Zero.
# Run ON the device, from the directory that holds cybercam.py and luts/.
set -e
SRC=$(cd "$(dirname "$0")" && pwd)
APP=/usr/lib/cybercam/cybercam.py
LUTDIR="$HOME/.config/cybercam/luts"

echo "== 1/4 backup"
sudo cp -n "$APP" "$APP.bak-$(date +%Y%m%d-%H%M%S)" && echo "   backed up"

echo "== 2/4 install patched app"
sudo cp "$SRC/cybercam.py" "$APP"
sudo chmod 644 "$APP"

echo "== 3/4 install LUT pack"
mkdir -p "$LUTDIR"
cp "$SRC"/luts/*.cube "$LUTDIR"/ 2>/dev/null || true
echo "   $(ls "$LUTDIR"/*.cube | wc -l) LUTs in $LUTDIR"

echo "== 4/4 dependencies (needs network)"
for pkg in python3-picamera2 python3-numpy python3-pil python3-evdev ffmpeg python3-qrcode; do
  if dpkg -s "$pkg" >/dev/null 2>&1; then
    echo "   $pkg ok"
  else
    sudo apt-get install -y "$pkg" >/dev/null 2>&1 && echo "   $pkg installed" \
      || echo "   $pkg MISSING - install later: sudo apt install $pkg"
  fi
done

# stop a running instance so APPLaunch starts the new code
pkill -f '/usr/lib/cybercam/cybercam.py' 2>/dev/null || true
echo
echo "Done. Reopen CyberCam from APPLaunch."
echo "  L / 5  = look picker     W = Wi-Fi share QR     7 = resolution"
