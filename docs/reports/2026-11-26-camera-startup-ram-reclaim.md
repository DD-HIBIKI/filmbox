# Reclaim RAM before the camera starts

**中文提要**：开机后第一次拍照经常卡死，根因是内存/CMA 不足（拍时可用 94MB，8MP 需 130MB）。
希望 FilmBox 启动时主动把能清的内存清一遍（page cache、可挂起的桌面进程），
让可用内存稳定在 250MB 以上、CMA 80MB 以上。下面按风险分级列了方案、命令和验收标准。

Requested by the user on 2026-11-26. Target: Gemini, for implementation in `filmbox.py`.

---

## 1. Measured problem

Device: Cardputer Zero, 414 MB MemTotal, 128 MB CMA, zram0 424 MB.

| Moment | MemAvailable | CmaFree | Result |
|---|---|---|---|
| 19:17 cold boot, first shot | 94 MB | 31 MB | `capture ENOMEM try 1`, then `_restore_preview()` blocked in `picam.start()`, UI hung |
| 19:20 second attempt | ~140 MB | ~55 MB | shot saved, but the LUT render fell back to 2 MP (`available_mb() < 130`) |
| 19:30 idle, app closed | 206 MB | 70 MB | — |

Thresholds already in the code:

```python
room_for_full = available_mb() >= 130     # 8MP LUT render
if gpu_budget < GPU_MIN_FREE_MB:          # 70 MB, else "low_mem" fallback
```

Cold boot spends its peak on service start-up. Measured RSS right after boot:

```
pcmanfm --desktop                   16.8 MB
/usr/bin/python3 czpinlock.py daemon 14.4 MB
M5CardputerZero-APPLaunch           13.3 MB
sshd-session                         10.4 MB
NetworkManager                       9.3 MB
wireplumber                          8.5 MB
pipewire-pulse                       8.4 MB
```

Plus 108–136 MB of `buff/cache` and 130 MB of zram already in use.

## 2. Goal

At the moment the user presses the shutter (or opens FilmBox), guarantee:

| Metric | Target |
|---|---|
| `MemAvailable` | ≥ 250 MB |
| `CmaFree` | ≥ 80 MB |
| `available_mb()` before LUT render | ≥ 130 MB |

If the target cannot be reached, degrade **predictably and immediately** — never block the UI waiting on the camera.

## 3. Options, in order of risk

### A. Drop page cache (zero risk, needs root)

Recovers most of `buff/cache` (108–136 MB on this board). Kernel reclaims it anyway
under pressure, but doing it up front avoids the allocation spike that triggers ENOMEM.

```python
def _drop_page_cache():
    try:
        with open("/proc/sys/vm/drop_caches", "w") as f:
            f.write("1\n")          # 1 = page cache only, keep dentries/inodes
    except OSError:
        pass                        # not root: silently skip
```

Notes:

- Needs root. FilmBox runs as `pi`; use the existing passwordless path if the image has
  one, otherwise skip silently (never prompt).
- **Use `1`, not `3`.** Dropping dentries/inodes makes the next launch slower for no gain.
- Call it once, in `run()` right before `self.stream.start()`. Do not call per shot.

### B. Suspend desktop helpers (low risk, reversible)

`pcmanfm --desktop` (16.8 MB) is a file-manager desktop that the camera UI covers anyway.
Sending `SIGSTOP` and `SIGCONT` on exit is reversible and needs no root when the process
belongs to the same user.

```python
def _suspend_helpers():
    import signal, subprocess
    out = subprocess.run(["pgrep", "-f", "pcmanfm --desktop"],
                         capture_output=True, text=True)
    pids = [int(p) for p in out.stdout.split()]
    for pid in pids:
        try: os.kill(pid, signal.SIGSTOP)
        except OSError: pass
    return pids            # resume with SIGCONT in the shutdown path
```

Rules: keep a list, send `SIGCONT` in the `finally` of `run()`, and skip anything not
owned by the current user. Never touch `pcmanfm` when it is not running as `--desktop`.

### C. Shrink FilmBox's own footprint first

Cheaper than fighting the system:

- Start the GPU worker **lazily** (on the first LUT render) instead of at start-up.
  It holds a 2048² texture, 16 MB of CMA, and is useless for a NORMAL shot.
- Lower `buffer_count` in the still configuration from 2 to 1 when `CmaFree < 60`.
- Keep `decode_mb = 24` but check `CmaFree` rather than `MemAvailable` — CMA exhaustion is
  what actually killed the capture.

### D. Rejected

| Idea | Why not |
|---|---|
| `swapoff -a` | Pulls 130 MB of swapped pages back into RAM — makes it worse |
| Kill `NetworkManager` / `sshd` | Breaks the only way to reach the device remotely |
| Kill `wireplumber` / `pipewire` | Shutter sound goes through them; also owned by systemd |
| Raise `gpu_mem`/CMA further | Tested 128 MB CMA is the practical ceiling for the 8MP pipeline |
| `posix_fadvise` per file | Gains a few MB, costs complexity |
| Disable the desktop entirely | User needs APPLaunch to start apps; out of scope |

## 4. What "before the camera starts" should mean

Two call sites, both cheap:

```python
def run(self):
    ...
    _drop_page_cache()          # A
    held = _suspend_helpers()   # B
    try:
        self.stream.start()
        ...
    finally:
        _resume_helpers(held)
```

And a **precondition check** immediately before `capture_still`:

```python
free = cma_free_mb()
if free < 45:                  # 8MP needs ~50 MB CMA
    set_status("LOW MEM - USING 2MP")
    limit = (1640, 1232)
```

This alone would have turned the 19:17 hang into a 2MP shot without blocking.

## 5. Acceptance

Run this before and after, on a cold boot, and paste both numbers into the commit:

```bash
grep -E "MemAvailable|CmaFree" /proc/meminfo
```

Required to pass:

1. Cold boot → open FilmBox → shoot 8MP NORMAL → file has **no** `_2mp` suffix.
2. Cold boot → open FilmBox → shoot with a LUT → file has **no** `_2mp` suffix.
3. Repeat 5 times back to back: no `capture ENOMEM` in `camera.log`.
4. Kill the app mid-shot: no orphan `_gpu-worker` (`pgrep -f _gpu-worker` empty).
5. If `_drop_page_cache` fails (non-root), everything above still works, just slower.

## 6. Constraints

- **Never block the UI thread on memory work.** All of A/B run once at start-up.
- **Everything must degrade, nothing may hang.** If a step fails, log it and continue.
- Keep the existing `_restore_preview()` but add a timeout — it is the actual hang site
  (see `2026-11-26-colour-fix-test.md` §1).
- No new dependencies. Pure stdlib + existing helpers.
- The device has no swap headroom worth using: zram is already 130 MB deep.

## 7. Why this is worth doing

The camera is the point of the device. A first shot that hangs the whole app on every cold
boot is the worst possible first impression, and the fix is ~40 lines of standard Linux
memory hygiene, not a re-architecture.

---

# 附录：2026-11-26 20:05 实测修正

原文的 A 方案（drop_caches）**收益远小于预期**，实测数据如下（设备上用 `sudo` 执行）：

| | 清理前 | 清理后 |
|---|---|---|
| MemAvailable | 109 MB | 100 MB |
| Cached | 70 MB | 58 MB |
| CmaFree | 33 MB | 33 MB |

`drop_caches=1` 只释放了 **12 MB**，CMA 一点没变。原因很简单：`buff/cache` 只剩 70 MB，
系统在长期内存压力下早就在持续回收它了，没有"脏钱"可捡。

## 真正的大头

同一时刻的进程 RSS（合计 361 MB）：

```
60.1 MB  /usr/bin/python3 /usr/lib/filmbox/filmbox.py ui
59.1 MB  /usr/bin/python3 -u /usr/lib/filmbox/filmbox.py _gpu-worker
13.5 MB  /usr/bin/wf-panel-pi
10.4 MB  sshd-session
 8.7 MB  /sbin/init
 8.4 MB  /usr/bin/pipewire-pulse
 8.3 MB  APPLaunch
 7.7 MB  NetworkManager
 7.6 MB  wireplumber
 7.3 MB  czpinlock
```

**FilmBox 自己占了 119 MB**（UI 60 + GPU worker 59），比所有系统服务加起来还多。
`_gpu-worker` 是常驻的，即使一张 LUT 都不拍也占着 59 MB —— 这就是开机第一次拍照卡死的直接原因：
新进程启动 → worker 预热 → 纹理占用 → 8MP 采集要 CMA → 没了。

## 结论：优先级要改

| 原方案 | 修正后 |
|---|---|
| A. drop_caches | **降级为可选**，只值 12 MB |
| B. 挂起桌面进程 | 保留，pcmanfm 16.8 MB 也算钱 |
| **C. 优化 FilmBox 自身** | **升为第一优先** |

具体做 C：

1. **GPU worker 懒启动** —— 第一次 LUT 渲染才起，NORMAL 拍照完全不需要它
2. **worker 空闲退出** —— 30 秒没任务就 `quit`，下次用再起（省 59 MB）
3. **UI 进程 60 MB 的构成要查** —— 是常驻纹理、缓存还是 Pillow 未释放？用 `tracemalloc` 或 `/proc/<pid>/smaps` 看
4. 拍照前读 `CmaFree`，`< 45 MB` 直接用 2MP，不试 8MP

按这个顺序做，收益是 drop_caches 的 5–10 倍，而且不依赖 root。

## 给实现者的补充验收

```
冷启动 → 只开 App 不拍照 → grep CmaFree   预期 ≥ 70 MB（worker 未启动）
冷启动 → 拍一张 NORMAL → grep CmaFree     预期 ≥ 60 MB（worker 从未启动）
拍一张 LUT → 等 35 秒 → pgrep -f _gpu-worker   预期为空（空闲退出生效）
```
