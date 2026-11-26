# Debugging FilmBox on the device from the Windows PC

The Cardputer Zero (`cpz`, hostname `HIBIKI`) sits on the same LAN as the PC.
You can edit, deploy, run and measure without leaving the PC.

Everything below is PowerShell, because that is what the PC runs.

---

## 1. Connect

A dedicated key already exists at `%USERPROFILE%\.ssh\cpz_ed25519` and is in the
device's `authorized_keys`.

```powershell
$cpz = "pi@<device-ip>"
$key = "$env:USERPROFILE\.ssh\cpz_ed25519"

# connectivity + identity
ssh -i $key -T $cpz "hostname; uname -m; date"
```

Expected: `HIBIKI`, `aarch64`, and the current time.

> **Ignore this banner.** The device prints
> `Please note that SSH may not work until a valid user has been set up.`
> on every login. It is a leftover Raspberry Pi first-boot notice, not an error.
> The command after it still runs.

If the address has moved (DHCP), find it again:

```powershell
arp -a | Select-String "<lan>."
```

or ask the device to print its own address over the existing channel.

## 2. Where things live

| Path | What |
|---|---|
| `/usr/lib/filmbox/filmbox.py` | the running app (root-owned, ~5400 lines) |
| `/usr/local/bin/filmbox` | launcher wrapper |
| `~/.config/filmbox/settings.json` | settings (filter, resolution, colour_fix) |
| `~/.local/state/filmbox/camera.log` | the log — everything lands here |
| `~/Pictures/FilmBox/Photos/` | captures; `_2mp` suffix means degraded |
| `/tmp/` | 208 MB tmpfs — **counts against RAM**, clean up after use |

## 3. Deploy a change

```powershell
$cpz = "pi@<device-ip>"; $key = "$env:USERPROFILE\.ssh\cpz_ed25519"

# 1. syntax check locally first - a broken file bricks the app until reflashed
python -m py_compile D:\path\to\cybercam\filmbox.py

# 2. copy up
scp -i $key D:\path\to\cybercam\filmbox.py "${cpz}:/tmp/filmbox_new.py"

# 3. verify what landed
ssh -i $key -T $cpz "python3 -m py_compile /tmp/filmbox_new.py && echo SYNTAX_OK; md5sum /tmp/filmbox_new.py"

# 4. install (root owns /usr/lib/filmbox)
ssh -i $key -T $cpz "echo '<SEE SECTION 4>' | sudo -S -p '' cp /tmp/filmbox_new.py /usr/lib/filmbox/filmbox.py"

# 5. stop the old process so the next launch loads the new code
ssh -i $key -T $cpz "pkill -TERM -f 'filmbox.py'; sleep 3; pgrep -af filmbox.py | grep -v pgrep || echo CLEAN"

# 6. confirm
ssh -i $key -T $cpz "md5sum /usr/lib/filmbox/filmbox.py"
```

**Always `pkill -TERM`, never `-KILL`.** `-KILL` leaves an orphaned `_gpu-worker`
that keeps holding CMA, and then the next capture fails with ENOMEM for reasons that
look unrelated.

## 4. sudo

`pi` is **not** in the NOPASSWD list, so installing needs the device password.
Ask the user for it rather than guessing; it is also on the device's own notes.

```powershell
ssh -i $key -T $cpz "echo 'THE_PASSWORD' | sudo -S -p '' cp /tmp/filmbox_new.py /usr/lib/filmbox/filmbox.py"
```

`-p ''` suppresses the prompt text so nothing garbles the output. The password will
appear in your command line — run it in a session you control and avoid committing it.

Everything else (reading logs, `pkill` of your own processes, `scp` to `/tmp`) needs
no sudo.

## 5. Measure

### Memory and CMA — the numbers that decide everything

```powershell
ssh -i $key -T $cpz "grep -E 'MemAvailable|MemFree|CmaFree|CmaTotal' /proc/meminfo"
```

`MemAvailable` is what the app's `available_mb()` reports. **`CmaFree` is what actually
kills a capture** — the ISP needs contiguous memory and it is only 128 MB total.

### Who is using the RAM

```powershell
ssh -i $key -T $cpz "ps -eo rss,args --sort=-rss | head -8"
```

### The log

```powershell
ssh -i $key -T $cpz "tail -40 ~/.local/state/filmbox/camera.log"
ssh -i $key -T $cpz "grep -E 'GPU|2mp_guard|startup mem|idle_exit' ~/.local/state/filmbox/camera.log | tail -20"
```

### Did the shot degrade?

```powershell
ssh -i $key -T $cpz "ls -lt ~/Pictures/FilmBox/Photos/*.jpg | head -4"
```

`_2mp` in the filename = the render was downscaled. A base file named `_photo.jpg`
instead of `_8mp.jpg` means the **capture itself** stepped down — a different bug.

### Is the worker alive?

```powershell
ssh -i $key -T $cpz "pgrep -af _gpu-worker || echo NO_WORKER"
```

## 6. Pull a diagnostic bundle

The user asked for logs to land on the PC after each test round:

```powershell
$stamp = Get-Date -Format "yyyyMMdd-HHmm"
$dst = "D:\path\to\cybercam\logs\$stamp"
New-Item -ItemType Directory -Force $dst | Out-Null

scp -i $key "${cpz}:/home/pi/.local/state/filmbox/camera.log" "$dst\camera.log"
scp -i $key "${cpz}:/proc/meminfo" "$dst\meminfo.txt"          # may fail; use the ssh form below
ssh -i $key -T $cpz "grep -E 'MemAvailable|CmaFree' /proc/meminfo" | Out-File "$dst\memory.txt"
ssh -i $key -T $cpz "ps -eo pid,rss,args --sort=-rss" | Out-File "$dst\ps.txt"
ssh -i $key -T $cpz "ls -l ~/Pictures/FilmBox/Photos/*.jpg" | Out-File "$dst\photos.txt"
```

`/proc` files cannot be copied with `scp` — always read them through `ssh`.

## 7. Take a control shot without the UI

The camera is exclusive: stop the app first, then drive Picamera2 directly.
Useful to separate "the code is slow" from "the UI is slow".

```powershell
# write a script on the device, then run it
ssh -i $key -T $cpz @'
cat > /tmp/shot.py <<'PY'
import sys, time, json
from picamera2 import Picamera2
p = Picamera2()
p.configure(p.create_still_configuration({"size": (3280, 2464)}))
p.start(); time.sleep(2.2)
md = p.capture_metadata()
p.capture_file(sys.argv[1])
print(json.dumps({k: md.get(k) for k in ("ColourTemperature","Lux","ExposureTime","AnalogueGain")}))
p.stop(); p.close()
PY
'@
ssh -i $key -T $cpz "pkill -TERM -f filmbox.py; sleep 2; python3 /tmp/shot.py /tmp/ctrl.jpg"
```

To test a custom libcamera tuning file, prefix with the environment variable:

```powershell
ssh -i $key -T $cpz "LIBCAMERA_RPI_TUNING_FILE=/tmp/candidate.json python3 /tmp/shot.py /tmp/tuned.jpg"
```

**Check `ColourTemperature` before trusting any colour measurement.** Below 5500 K the
daylight CCM entries are not in play and the comparison is meaningless.

## 8. Restart the app after deploying

There is no service — the app is launched from the desktop (APPLaunch). After step 5
the user has to open it again, or:

```powershell
ssh -i $key -T $cpz "DISPLAY=:0 nohup /usr/local/bin/filmbox >/dev/null 2>&1 &"
```

This only works if a graphical session is up. When in doubt, ask the user to tap the
icon — it is faster than fighting the session.

## 9. Things that will bite you

| Symptom | Cause |
|---|---|
| `stdio forwarding failed` (from the phone's tunnel) | unrelated to the PC path; ignore when using direct SSH |
| `capture ENOMEM try 1` | `CmaFree` too low — check it before blaming the code |
| Two `GPU worker ready` lines 2 s apart | the worker idle-exited between shots; expected with the 30 s timeout |
| App shows "CAPTURING..." forever | the capture thread is blocked; grab `py-spy dump` if available, else the log |
| `_2mp` on a LUT file but `_8mp` base | render-side guard tripped, not the capture |
| `_photo.jpg` (no `_8mp`) | capture-side step-down — CMA exhausted before the shot |

## 10. Loop that actually works

```
edit on PC  ->  py_compile  ->  scp to /tmp  ->  sudo cp  ->  pkill -TERM
            ->  user opens the app  ->  user takes 2-3 shots
            ->  pull log + photos + meminfo into logs\<stamp>\
            ->  compare against the numbers in the previous commit message
```

Every claim in a commit message should be backed by a line from `camera.log` and a
`CmaFree` reading. That is how the 2 MP bug was pinned down: the log said
`avail=120MB` while the file was 2 MP, and only `CmaFree=8MB` explained it.
