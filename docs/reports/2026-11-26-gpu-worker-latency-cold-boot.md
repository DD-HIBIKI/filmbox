# GPU worker: cold boot still degrades, and every LUT shot waits 3–5 s

Feedback from the user after testing commit `c300b8b` on 2026-11-26 20:46.
Both complaints are about the same trade-off: the lazy worker saved RAM and spent
the user's time.

## 1. What the user said

> 刚才是开机状态，我打开相机拍照的话是正常的 8mp，重启机器之后前几张是降级的。
> 这个没满足我的需求。
>
> 还有就是现在每次拍照都要等 gpu 加载，体感比之前 cpu 处理还要慢。

Two separate problems:

| # | Symptom | When |
|---|---|---|
| A | First shots after a reboot come out at 2 MP | cold boot only |
| B | Every LUT shot pauses while the worker starts | every shot, after 30 s idle |

## 2. Measured: A — cold boot degrades the capture itself, not just the LUT

Photos from the session (the base file is the capture, before any LUT):

```
20:46:39  458 KB  cyber_..._photo.jpg                       ← 2 MP capture
20:46:39  614 KB  cyber_..._photo_lut-p_nn_abpy_2mp.jpg     ← 2 MP LUT
20:46:49  452 KB  cyber_..._photo.jpg                       ← 2 MP capture
20:46:49  604 KB  cyber_..._photo_lut-p_nn_abpy_2mp.jpg     ← 2 MP LUT
20:46:58  1.68 MB cyber_..._8mp.jpg                         ← 8 MP capture
20:46:58  2.24 MB cyber_..._8mp_lut-p_nn_abpy.jpg           ← 8 MP LUT
```

The first two are `photo.jpg` — **the sensor mode had already stepped down**, so no
LUT setting could have fixed them. That happens in `capture_still`, whose retry loop
steps 8 MP → 2 MP after ENOMEM.

The structured log shows why:

```
[GPU] 2mp_guard mode=LUT:p_nn_abpy avail=120MB<60MB or cma=8MB<10MB | Free=27MB Available=120MB ...
```

`Available` was 120 MB — comfortably above the 60 MB threshold — but **CmaFree was 8 MB**,
below the 10 MB gate. CMA, not MemAvailable, is the binding constraint right after boot.

Two defects in that log line itself:

1. It prints `avail=120MB<60MB` when the failing condition was the CMA one. The
   message should name the condition that actually tripped:
   `reason=cma cma=8MB<10MB`.
2. The guard runs on the **LUT** path but the degradation had already happened at
   capture time. The capture path needs its own CMA check, or it needs to wait.

## 3. Measured: B — the worker costs 1.3–5.7 s to spawn, every time

```
[GPU] worker ready pid=6222 rss=101MB spawn=5.65s | Free=56MB
[GPU] worker ready pid=6239 rss=104MB spawn=4.82s | Free=55MB
[GPU] worker ready pid=6255 rss=105MB spawn=2.02s | Free=80MB
[GPU] worker ready pid=1654 rss= 93MB spawn=4.10s | Free=48MB
[GPU] worker ready pid=1693 rss=104MB spawn=2.94s | Free=27MB
[GPU] worker ready pid=1710 rss=104MB spawn=1.32s | Free=41MB
[GPU] worker ready pid=1728 rss=103MB spawn=2.49s | Free=66MB
```

| | |
|---|---|
| spawn | **1.3 – 5.7 s**, typically 3–4 s |
| worker RSS | **93–105 MB** |

So each LUT shot pays 3–4 s before rendering even begins, and the worker still holds
~100 MB. Dropping NumPy saved less than expected — the RSS is dominated by something
else (EGL/Mesa, the Pillow decode buffer, or the tile textures). Worth profiling with
`/proc/<pid>/smaps` before optimising further.

For comparison, the old always-on CPU render was 5–6 s end to end with no start-up
pause. The new path is not faster in wall-clock — it just front-loads the wait.

## 4. The trade-off nobody asked for

| Strategy | LUT shot latency | RAM held | Cold boot |
|---|---|---|---|
| worker always on (old) | 1.6 s | ~100 MB | worse |
| worker lazy + 30 s idle (now) | 3–5 s + render | 0 between shots | better |
| **what the user wants** | **~1.6 s** | **acceptable** | **8 MP from shot one** |

The user explicitly prefers speed. They rebooted, shot, and got 2 MP — twice — before
the machine settled. That is the worst first impression the app can make.

## 5. Proposal

### P1. Warm the worker when the app opens, not on the first shot

The user takes 5–15 s from launching FilmBox to pressing the shutter (framing, choosing
a look). Spawning the worker during that window costs nothing perceptible and removes
problem B entirely.

```python
def run(self):
    ...
    if self.settings["filter"].startswith(LUT_PREFIX):
        self.gpu_worker = GpuWorker()
        self.gpu_worker.start()          # async, non-blocking
```

Keep the lazier path when the current filter is NORMAL — those users never need it.

### P2. Stretch the idle timeout to 5 minutes, and only reap under pressure

30 s is shorter than the gap between two deliberate shots. Reap the worker when any of
these is true instead: idle > 300 s, **or** `CmaFree < 20 MB`, **or** the app is
backgrounded. That keeps the fast path hot in normal use and still frees memory when it
matters.

### P3. Fix cold boot at the capture path

`capture_still` should check CMA **before** trying 8 MP:

```python
free = cma_free_mb()
if free is not None and free < 45:
    # wait up to 3 s for the pipeline to settle, then shoot 2 MP
```
or better, hold the shutter open and retry once after `_drop_page_cache()` +
1.5 s sleep. The user would rather wait one second than get a 2 MP file.

Also: right after `_suspend_helpers()` and `_drop_page_cache()`, give the kernel a
moment before the first capture — the very first request is currently the one that
fails.

### P4. Make degradation visible

The user found out about the 2 MP shot by looking at the filename. Show it:
`2MP - LOW MEM` in the status bar for 2 s, and log the reason with the actual numbers.

### P5. Profile the 100 MB worker

Before more thresholds are tuned, find out where 100 MB goes. If the Pillow decode is
the culprit, decode inside the main process and hand the worker a texture (or render in
bands). If it is Mesa, consider `EGL_PLATFORM=surfaceless` and no depth/stencil buffers.

## 6. Acceptance

| Test | Pass |
|---|---|
| Reboot → open app → shoot LUT within 10 s | 8 MP base file, 8 MP LUT file |
| Shoot LUT, wait 60 s, shoot again | second shot takes < 2.5 s total |
| Idle 6 min | worker reaped, `pgrep -f _gpu-worker` empty |
| Any degradation | status bar says `2MP LOW MEM`, log names the failing condition |

## 7. Note on the log line

```
2mp_guard mode=... avail=120MB<60MB or cma=8MB<10MB
```

reads as if both failed. It should state which one did:

```
2mp_guard reason=cma mode=... cma=8MB<10MB avail=120MB (>=60MB)
```

Worth fixing while touching this code — it made the diagnosis take longer than it should.
