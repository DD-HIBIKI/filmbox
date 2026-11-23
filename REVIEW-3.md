# CyberCam 第三轮复核（REVIEW-3）

- 复核日期：2026-11-22（新会话）
- 基线：HEAD `a93fae4`；`cybercam.py` 实测 **2896 行**（`read` 读到文件尾确认）
- 方法：**只读静态复核**。工作区 shell 被沙箱 ACL 挡住，本轮**未使用 pwsh、未申请提权**，只用 `read` / `glob` / `write`。凡需真机 / 执行环境 / 浏览器才能确认的结论一律标「未验证」。
- 对照：`REVIEW-2.md`（上一轮）、`REVIEW-RESPONSE.md`、本轮改动清单 1–9
- 分级：A 阻断/回归 · B 功能性问题 · C 可靠性/工程 · D 文档漂移 · E 小问题

> 行号地图校正（供后续会话）：`Framebuffer` 实际 **1456-1519**（不是 1400-1480），`run()` 实际 **2577-2740**（不是 2550+），`stop_recording` 实际 **847-890**。

## 结论摘要

| 级别 | 数量 | 说明 |
|---|---|---|
| **A** | **0** | 未发现阻断级问题；若 B1 在真机确认，可升级为 A（暗光预览可用性回归） |
| B | 7 | `FrameDurationLimits` 语义反向 + 暗光曝光上限、录像失败静默"假成功"、录像清理不在 `finally`、`CAM ERR` 仍会被覆盖、`display.draw` 只包了 1/6 处、`capture_metadata()` 无超时、实测 `FrameDuration` ≠ 文件帧率 |
| C | 12 | 录像 fps 未夹取、错误抖动无 debounce、`CAM OK` 顶掉 `REC` 状态、draw 失败日志膨胀、C6 修复不完整、`save_settings` 残留/目录未 fsync、fsync 落在按键线程等 |
| D | 8 | NOTES 与 README 关于浏览器播放**新出现互相矛盾**、LUT 数量/状态过期、`FrameDurationLimits` 描述与实现不符等 |
| E | 9 | 死分支/死字段、bool 强转、`raw.rename(raw)` 空操作、`.partial` 命名不一致等 |

### 9 条改动逐项判定

| # | 改动 | 判定 | 一句话依据 |
|---|---|---|---|
| 1 | B1 节流移到 `capture_request` 之后、`Image.fromarray` 之前 | ✅ **到位** | 670-685：`continue` 在 `with self.camlock:` **之外**（缩进 16 vs 20），`request.release()` 已在 679-680 的 `finally` 里执行；无锁泄漏、无重复发布 |
| 2 | B2 视频配置加 `FrameDurationLimits` | ⚠️ **方向对、语义反了** | 618：`(1000, 1e6/fps)` = 帧时长 **1ms…40ms** = 帧率 **25…1000**，只禁"慢于 25fps"，并不产出 25fps；代价是 AE 曝光被锁 ≤ ~40ms（见 B1） |
| 3 | B3 错误恢复 + `CAM OK` | ⚠️ **部分到位** | 697-703 成功清 `error` ✓、2610 用 `inf` deadline ✓、2611-2613 有 `CAM OK` ✓；但任何 `set_status` 都能顶掉横幅且 `_cam_error_seen` 不复位（见 B4） |
| 4 | B4/B5 `stop_recording` 先算 fps、成功才删 raw | ⚠️ **数据安全对、失败不可见** | 875-890 成功才 `unlink`、失败留 raw ✓；但失败分支**不写日志、不抛错**，上层仍提示 `VIDEO SAVED`（见 B2/B3/B7） |
| 5 | B7 `display.draw` 包 try/except | ⚠️ **只包了主绘制** | 2713-2724 已包；2617/2625/2632/2644/2653 五处 overlay 仍无保护，异常照旧杀进程（见 B5） |
| 6 | C1 fsync / C2 fbN / C3 xoff / C5 mmap 长度 / C6 start 失败 close | ✅ 4 项到位、C6 **部分** | C1 159、C2 1469、C3 1474、C5 1479-1482 均正确；C6 的 `try`（625-633）只覆盖 `configure`/`start`，612-624 的 `create_*` 仍在 `try` 外 |
| 7 | C7 删除死参数 `g_lin`（8 参数） | ✅ **到位** | `build_looks.py:82` 解包 8、`fit_all.py:71` 解包 8、`look_params.json` 各 8 项、代码/参数文件中已无 `g_lin`；三支 cube 黑场角点与上一轮记录**逐位相同**（0.074881/0.115327/0.061335），产物未漂移 |
| 8 | C8 `fit_all.py` 不再 `import cybercam` | ✅ **到位** | `fit_all.py:30-51` 本地 `load_cube`，全仓无 `import cybercam`；Windows 示例路径（`fit_all.py:9`）现在成立 |
| 9 | E1/E2 类型校验、E6 null 守卫、E18 告警色 | ✅ 到位（附副作用） | 140-145 zoom ✓、135-138 bool ✓、848-849 守卫 ✓、2167-2170 告警色 ✓；副作用：E6 早退不清 `_record_*`（E5）、`start_recording` 仍无守卫（C11） |

---

## B. 功能性问题

### B1【新·最高优先】`FrameDurationLimits(1000, 1e6/fps)` 把 AE 曝光钉死，且**没有**实现"相机自己出目标帧率"

- 位置：`cybercam.py:614-618`
- 证据：

```python
614:            self.video_config = picam.create_video_configuration(
...
618:                controls={"FrameDurationLimits": (1000, int(1e6 / max(1, self.fps)))})
```

- 语义：`FrameDurationLimits = (min_frame_duration, max_frame_duration)`，单位 µs。当前值 = 帧时长 **1 ms … 40 ms**（fps=25），换算成帧率是 **25 … 1000 fps**：
  - 它**只禁止**"慢于 25fps"（上限帧时长），**没有**禁止"快于 25fps"（下限仍是 1000µs），所以相机仍按传感器最快速度出帧，`_loop` 的节流（683-685）才真正在控制发布率。**B2 想要的"让相机出目标帧率"没有发生**。
  - 真正的副作用：AE 需要更长曝光时（暗光），帧时长被夹在 ≤40 ms → 曝光上限约 40 ms 减读出时间。**暗光下预览会变暗/噪点变多**，而改前 AE 可以掉到 5–10 fps 换曝光。这正是你担心的那条。
- 另外：`_record_config`（808-817）和 `_still_config`（756-767）**没有**这个 control，所以录像/拍照不受影响（这一点是对的，别顺手加上去）。
- 建议：二选一，并且写清语义
  1. **回到不加这个 control**（`_loop` 的节流已经够用），让 AE 自由降帧；
  2. 如果确实要"最短 25fps"，保留但把上限放宽（如 `(1000, 200000)`），或提供"锁定帧率"开关，并在 README/NOTES 明确写"会限制最长曝光"。
  另：真正想锁帧率应设 `min=max=1e6/fps`，但那会让曝光 = 帧时长、强光过曝/人工光闪烁，不建议默认开。
- 未验证 —— 需真机：暗光（≤10 lux）下对比改前/改后预览亮度与噪点、`capture_metadata()["ExposureTime"]` 与 `FrameDuration` 的实际上限。

### B2【新】重封装失败被吞掉：UI/CLI 报 `VIDEO SAVED`，文件却被改名成不可见后缀

- 位置：`cybercam.py:875-890`（失败分支 883-890）、`2337-2347`（`VIDEO SAVED`）、`2884-2887`（CLI `Saved:`）
- 证据：

```python
875:        if raw is not None and raw.exists():
876:            ok = False
877:            try:
878:                run_command(["ffmpeg", ... str(final)], timeout=90)
880:                ok = final.exists() and final.stat().st_size > 0
881:            except Exception:
882:                ok = False                     # ← 无日志、无异常、无状态
883:            if ok:
884:                raw.unlink(missing_ok=True)
885:            else:
887:                broken = final.with_suffix(".mp4.partial")
889:                    final.rename(broken)
890:                raw.rename(final.with_suffix(".mjpeg"))
```

- 后果链：
  1. `ok=False` 时函数**正常返回**，`CyberCamUI.stop_recording` 的 `operation()` 继续执行 2344 行 `set_status("VIDEO SAVED " + ...)`；CLI 2887 行无条件 `print("Saved:", path)` → **用户以为录到了**。
  2. 实际名字是 `xxx.mp4.partial` 与 `xxx.mjpeg`，两者都不在 `WEB_EXTENSIONS`（1182）/`GALLERY_SUFFIXES`（1526）里 → **相册/网页都看不到**，等于静默丢片。
  3. `selftest.py:64` 的 `os.path.getsize(vid)` 会变成 `FileNotFoundError` —— 测试脚本比产品先报错，容易被当成测试问题忽略。
  4. 设备上若没装 ffmpeg（`FileNotFoundError` 也被 881 行 `except Exception` 吞掉），**每一次录像都会走这条路**。
- 建议：失败分支必须 `camera_log()` 写一行 `remux failed: ...`，并把失败传出去（返回 `(final_or_none, error)` 或抛 `RuntimeError`），让上层：CLI 打印原始路径并返回码 1；UI 显示 `ERR VIDEO REMUX FAILED`；同时把 `.mjpeg` 加入 Gallery 可见白名单（或至少提示"原片在 Videos/*.mjpeg"）。
- 未验证 —— 需真机：断网/删除 ffmpeg/故意让 ffmpeg 失败，确认 UI 提示与相册可见性。

### B3【新】`stop_recording` 的重封装与状态复位**不在 `finally`**，相机收尾异常时整段丢失且状态残留

- 位置：`cybercam.py:850-890`（关键 861 抛错时 870-890 不执行）
- 证据：

```python
860:            try:
861:                picam.stop_recording()
862:            finally:
863:                try:  # 恢复预览，异常都被吞
...
870:        raw, final = self._record_raw, self._record_path     # ← 861 抛错时到不了这里
873:        self._record_path = None
```

- 后果：`picam.stop_recording()` 若抛异常（编码器已出错 / 从未 start / 状态不一致），异常穿透整个方法，**不做重封装、不把 raw 改名、不清 `_record_raw/_record_path/_record_fps`**。raw 文件名本来就是 `.mjpeg`，于是同样"不可见 + 状态残留"。下次 `stop_recording` 若因故没有新的 `_record_raw`，还可能拿旧路径去重封装。
- 建议：把 870-890 整段放进 `finally`（或用 `try/except/finally` 包住 861），并且在 `start_recording` 开头就复位 `_record_raw/_record_path/_record_fps`。
- 未验证 —— 需真机：模拟 `stop_recording` 抛错（例如编码过程中拔 buffer / 强制 encoder 失败）观察 raw 落点。

### B4【残留】`CAM ERR` 横幅仍会被任何一次 `set_status` 顶掉，之后不再出现 → 长故障又变"假就绪"

- 位置：`cybercam.py:2605-2613`（置位）、`2206-2208`（`set_status`）、`2659-2660`（READY 复位）
- 证据：

```python
2610:                    self.set_status("CAM ERR " + stream_error[:28], float("inf"))
...
2206:    def set_status(self, value, delay=3):
2207:        self.status = value                 # ← 覆盖式，没有优先级
2208:        self.status_deadline = time.monotonic() + delay
2659:                if self.status != "REC" and not self.busy and now > self.status_deadline:
2660:                    self.status = "READY"
```

- 分析：
  - `inf` deadline 确实挡住了 2659-2660 的 READY 复位（`now > inf` 恒 False）—— 这部分**修对了**。
  - 但 `set_status` 是无优先级覆盖：任何按键（`select_mode` 2407、look 2436、zoom 2568、`job()` 2353 的 headline、`CAM OK` 2613…）都会把 `CAM ERR` 换成有限 deadline 的普通消息；2–30 s 后 2659 行把状态改回 `READY`。此时 `_cam_error_seen` 仍等于同一个错误串，**不会重新触发** 2606 行 → 摄像头持续坏、界面又显示就绪。
  - 复位条件（2611）本身没问题：只在 `_loop` 成功发布、`error` 被清空后才清 `_cam_error_seen`。
- 建议：给状态加"粘滞/优先级"概念（例如 `sticky_status` 单独字段，`draw_ui` 优先画它；或在 2606 行比较 `self.status` 是否仍是 `CAM ERR…`，不是就重设）。最小改法：把 `elif not stream_error and self._cam_error_seen` 改成"每帧检查：`stream_error` 非空且当前 status 不是 CAM ERR 就重设"。
- 未验证 —— 需真机：拔掉/占用相机后随意按键，确认横幅不会被清掉。

### B5【残留】`display.draw` 只包了主绘制一处，5 处 overlay 仍可整进程退出

- 位置：已保护 `2713-2724`；未保护 `2617`（help）、`2625`（picker）、`2632`（share）、`2644`（gallery）、`2653`（mode menu）
- 证据：`run()` 的 `while` 循环没有整体 try，`key()` 的 try 只包 2599-2604；上述 5 处任意一处抛（mmap 切片越界 / fb 被占用 / `gallery.path` 竞态）都会冒到 `finally`（2725）后退出进程 —— 与 REVIEW-2 B7 的原始故障模式完全相同，只是触发概率低。
- 建议：抽一个小函数 `self.safe_draw(image, tag)`，六处统一走它；失败时 `set_status("DISP ERR …", 3)` 并保持循环。
- 未验证 —— 需真机：让 `mmap` 写失败（例如并发 `fbset`/`cat /dev/fb0` 抢屏）后切到相册/帮助页。

### B6【新】`stop_recording` 里的 `capture_metadata()` 无超时、且在 `camlock` 内 → 可能冻住预览与 UI

- 位置：`cybercam.py:850-858`
- 证据：

```python
850:        with self.camlock:
851:            self._recording = False
852:            try:
853:                meta = self.picam.capture_metadata()
854:                duration = meta.get("FrameDuration")
```

- 说明 1：你问的"`capture_metadata()` 在 `stop_recording()` 之后调用是否安全" —— **代码里它在之前**（853 在 861 之前），这点与描述不符；语义上"先取录制配置下的帧时长再停编码器"是合理的。
- 说明 2：真正的风险是**阻塞**。`capture_metadata()` 等的是"下一个完成帧的元数据"，没有传 `timeout`；它又在 `with self.camlock` 里，而 `_loop`（670-680）也靠同一把锁 `capture_request()`。若编码器/管线卡住导致没有新帧完成，这里可能长时间不返回：预览冻结、`busy` 一直为 True、状态停在 `FINALIZING MP4...`。
- 建议：`capture_metadata(timeout=...)`（若所用 picamera2 版本支持，未验证）或先把 `FrameDuration` 缓存在 `_loop` 最近一次成功帧里（每帧 `request.get_metadata().get("FrameDuration")` 存 `self._last_frame_duration`），`stop_recording` 直接读缓存，完全绕开这个阻塞点。
- 未验证 —— 需真机/查 picamera2 版本签名：`capture_metadata` 是否支持 timeout、编码中调用是否会抢 encoder 的 request。

### B7【新】用 `1e6/FrameDuration` 当整段 fps：那是**传感器帧时长**，不是文件里的帧数

- 位置：`cybercam.py:853-856`、`870-871`、`878`
- 证据：

```python
854:                duration = meta.get("FrameDuration")
856:                    self._record_fps = 1e6 / float(duration)
871:        fps = self._record_fps or 30
```

- 分析：
  1. `FrameDuration` 是**最后一帧**的传感器帧时长（随 AE 变化），不是整段平均；AE 在录制中变化时，全片会匀速播放但实际是变速的 → 快放/慢放。
  2. 更关键：MJPEG 文件里是**编码器实际写出的帧数**。若编码/管线跟不上而丢帧（NOTES:148 自己写着"录像中预览 9.7–10 fps"，说明竞争很严重），文件真实帧率远低于 `1e6/FrameDuration`，此时 `-r` 偏高 → 成片**快放**，比原来硬编码 30 更隐蔽（数值看起来很"科学"）。
  3. 没有夹取：`duration` 异常（极小/极大）会得到荒谬 fps，`-r 1000000.000` 之类直接让 ffmpeg 失败 → 掉进 B2 的静默丢片。
- 建议：别用 metadata。改成**实测**：`start_recording` 记 `time.monotonic()`，`stop_recording` 记结束时间，`raw` 里数 JPEG 帧（`data.count(b"\xff\xd8")`，C 速度、几 MB 文件开销可忽略），`fps = clamp(frames / elapsed, 1, 120)`；失败才回退 30。同时记录 frames/elapsed 到日志，方便下次真机核对。
- 未验证 —— 需真机：录 5 s 720p，`ffprobe -count_frames` 取 `nb_read_frames` 与 `duration`，和 `-r` 值对比；再看 `ffprobe` 报的 `avg_frame_rate` 是否合理。

---

## C. 可靠性 / 工程

### C1【新】实测 fps 未夹取就直接进 ffmpeg
- 位置：`cybercam.py:856、871、878`
- 证据：`self._record_fps = 1e6 / float(duration)` → `fps = self._record_fps or 30` → `"%.3f" % fps`。`duration` 只挡了 0（`if duration`），负数/极小值/NaN 都会原样传到 `-r`。
- 建议：`fps = min(120.0, max(1.0, fps))`，并在异常时回退 30 且记日志（与 B7 一起改）。

### C2【新】瞬时 fps 当整段 fps（同 B7 的分析 1）—— 建议帧数/时长法，不要在 stop 时取单帧
- 位置：同上。

### C3【新】相机错误抖动没有 debounce：横幅 `CAM ERR`/`CAM OK` 每 0.2 s 翻一次，日志同步膨胀
- 位置：`693-696`（失败 sleep 0.2）、`701-702`（成功清 error）、`2606-2613`（每次新错误都写日志）
- 证据：`_loop` 一帧成功就清 `error`，`run()` 一看到 None 就清 `_cam_error_seen` 并显示 `CAM OK`；下一次失败又是"新错误"→ 再写一行 `camera.log`。间歇性失败（隔帧失败）时状态栏闪、日志每 0.4 s 一行。
- 建议：错误路径加最小间隔/计数（连续 N 次失败才置位、连续 M 次成功才恢复），日志只在状态翻转时写。

### C4【新】`CAM OK` / `CAM ERR` 会顶掉 `REC` 状态，且**没有地方恢复**
- 位置：`2611-2613`（CAM OK）、`2659-2660`（READY 复位）、`2134-2141`、`1883-1895`
- 证据：`REC` 是"模式状态"而不是普通消息：`draw_ui` 用它画计时块（2159-2162）、按钮文案（`capture_action_label` 1886）、`recording_active`（2134）。一旦 `set_status("CAM OK")` 覆盖，2 s 后 2659 行把状态改成 `READY`，录像继续进行但界面不再显示 REC、按钮变成"6 RECORD"。
- 建议：`REC` 状态改为派生（由 `self.recording is not None and 正在录` 计算），`set_status` 只负责短消息层；或恢复时记住 previous status。

### C5【新】C6 修复不完整：`create_*_configuration` 在 `try` 之外，失败仍泄漏 `Picamera2`
- 位置：`cybercam.py:612-633`
- 证据：`picam = Picamera2()`（612）→ `create_video_configuration`（614-618）→ `create_still_configuration`（621-624）→ **625 才开始 try**。前两步任一抛异常（不支持的 sensor mode、参数错误、内存不足），`picam.close()` 不会执行，`self.picam` 仍为 None → `stop()` 的 `if self.picam is None: return`（641-642）也救不回来。这正是 NOTES 一·四① 记录的"残留进程永久占 buffer → ENOMEM"那一类故障。
- 建议：把 612-633 整体包进 `try: … except: picam.close(); raise`；`self.picam = picam` 保持到最后一行。

### C6【新】`save_settings` 失败路径：`.json.tmp` 会残留，父目录仍未 fsync
- 位置：`cybercam.py:153-160`
- 证据：`with open(tmp, "w")` → `write` → `flush` → `os.fsync`（159）→ `os.replace`（160）。失败路径没有 `try/except` 清理，写失败/`replace` 失败时 `settings.json.tmp` 留在 `~/.config/cybercam/`；`replace` 之后也没有对父目录 fsync（掉电后"rename 丢失、旧文件仍在"仍可能，注释 159 只说"数据先落盘"，措辞是准确的）。
- 影响：残留文件不影响功能（下次覆盖写），但 `command_diagnose`/排障时容易被误认成坏配置。
- 建议：`try: ... finally:` 里判断 `tmp.exists()` 就 `unlink(missing_ok=True)`（成功 replace 后 tmp 已不存在）；目录 fsync 可选（`os.open(CONFIG.parent, os.O_DIRECTORY)`）。

### C7【新】fsync 落在 UI 按键线程：改设置类按键会阻塞一帧
- 位置：`2539、2550、2567、2571、2574` 调 `save_settings`，最终 159 行 fsync
- 分析：`←→` 连续切滤镜时，每次按键都 `fsync` 到 SD 卡（Pi 上常见 10–100 ms），期间主循环不能画帧（`key()` 在 2598-2604 的同线程里）。不会死，但会有可感知的卡顿/掉帧。
- 建议：设置写盘 100–300 ms 合并一次（后台线程或 dirty 标记 + 主循环空闲时落盘）；或只在"退出/切页"时落盘。

### C8【残留】`last_error` 未初始化、无人消费，且 `latest()` docstring 描述了一个不存在的契约
- 位置：`cybercam.py:701`（唯一写入）、`705-706`（docstring）
- 证据：`__init__`（571-596）没有 `self.last_error`；`self.last_error = self.error` 只在成功发布分支执行；全仓除 docstring 外**没有任何读取**。docstring 却说 "error stays sticky in last_error for the UI to report" —— UI 实际读的是 `.error`（2605）。
- 建议：要么在 `__init__` 里 `self.last_error = None` 并在 `latest()` 返回它（真做"粘滞错误"），要么删掉这个字段和 docstring 里那句话。

### C9【新】`raw.rename(final.with_suffix(".mjpeg"))` 是同一路径的空操作；`.mp4.partial` 没有清理入口
- 位置：`cybercam.py:887-890`
- 证据：`start_recording` 里 `raw = path.with_suffix(".mjpeg")`（827），`_record_path = path`（845）；`stop_recording` 里 `final.with_suffix(".mjpeg") == raw`。所以 890 行是 `rename(自己)`（POSIX 下成功、无效果）。本意（把临时名改成正式名）在 `raw` 一开始就是正式名的情况下不成立。
- 另外 `*.mp4.partial`（887）与 `encode_mp4` 的 `*.partial.mp4`（978）命名规则不一致，且新名字不在任何清理/展示逻辑里，坏片会长期躺在 `Videos/`。
- 建议：失败时把 raw 明确改成 `final.with_name(final.stem + "_raw.mjpeg")`（或直接保留原 `.mjpeg` 并**记日志说明路径**）；`.mp4.partial` 与 `encode_mp4` 统一命名为 `.partial.mp4`，并在相册/启动时提供清理提示。

### C10【新】`selftest.py` 假定 `vid` 一定存在，重封装失败时会以 `FileNotFoundError` 崩在测试里
- 位置：`selftest.py:63-64`
- 证据：`st.stop_recording(); print("mp4 %d KB" % (os.path.getsize(vid)//1024))`。结合 B2/B3，失败时 `vid` 已被改名 → 测试崩溃且看不出是哪一步坏。同时 selftest 也没有覆盖本轮新增的 `FrameDurationLimits`（没有量测 `FrameDuration`/`ExposureTime` 上下限）与录制帧率。
- 建议：`getsize` 前判存在，失败打印 `MISSING (see camera.log)` 并继续；新增一段"暗光/AE"与"录制帧数"的输出（对 B1/B7 的验证非常关键）。

### C11【新】`start_recording` 仍没有 `self.picam is None` 守卫（E6 只补了 stop 侧）
- 位置：`cybercam.py:830-832`
- 证据：`with self.camlock: picam = self.picam; picam.stop()` —— 相机未启动时抛 `AttributeError: 'NoneType'`，被 840 行 `except` 接住后 `_restore_preview()`（本身有 None 守卫）再 `raise`，上层看到的是 `AttributeError` 而不是"相机未就绪"。
- 建议：与 848-849 对称，开头 `if self.picam is None: raise RuntimeError("camera not started")`。

### C12【新】`_record_*` 生命周期没有单点复位
- 位置：`__init__:594-596`、`start_recording:844-845`、`stop_recording:870-874`、早退 `848-849`
- 证据：`_record_fps` 只在 stop 的正常路径清；`_record_raw/_record_path` 在 start 成功后才赋值、在 stop 正常路径才清。任何早退/异常（848、861、B3 场景）都会留下旧值；下一次 stop 若没被 start 覆盖，可能重封装**上一段**的 raw。
- 建议：`start_recording` 开头统一清三个字段（`self._record_raw = self._record_path = self._record_fps = None`），`stop_recording` 用 `try/finally` 收尾。

---

## D. 文档漂移（本轮修复引出的新矛盾已标【新】）

| # | 位置 | 问题 |
|---|---|---|
| D1【新】 | `README.md:101` vs `NOTES.md:151` | README 已改成"iOS 相册和多数浏览器 `<video>` 不认"，NOTES 仍写"VLC / 浏览器 / 本机相册都能播" —— **同一事实两处相反**，比原来两边都错更糟 |
| D2【残】 | `NOTES.md:41`、`:47` | 仍写"`luts/`（+ `fuji-luts.tgz`）已配好 **11 支**"、"CLASSIC NEG / NOSTALGIC NEG / BLEACH BYPASS **（已移除）**"；实际 18 支、三支自建 look 在发货（`README.md:43`、`luts/README.md:1`） |
| D3【新】 | `NOTES.md:29` | "视频配置下发 `FrameDurationLimits` 上限" 被写成"设置生效方式"，暗示帧率由此决定；实现上它只限最长曝光（见 B1），真正生效的是 `_loop` 节流 |
| D4【残】 | `NOTES.md:298`、`:305` | 标题"已做的验证（本地，非真机）"与末行"✅ 已在真机验证"仍自相矛盾（REVIEW-2 D10 未清） |
| D5【残】 | `NOTES.md:286-294` | 安装段仍只 `apt install python3-qrcode`；`install.sh:22` 已装 picamera2/numpy/PIL/evdev/ffmpeg/qrcode（REVIEW-2 D9） |
| D6【残】 | `README.md:13-19` vs `NOTES.md:65,210-216` | 25.7 fps / 3.2 s / 0.86 vs 1.0 s / 9.7 与 10 fps / 51 vs 72 等仍对不上（REVIEW-2 D8）；且"25.7 fps"贴着默认 25 容易被读成"设置生效值" |
| D7【残】 | `NOTES.md:264` | "实测设 12 得 13.0 fps" 未随 `FrameDurationLimits` 更新；本轮 B1 若上真机，这个数字必须重测 |
| D8【新】 | `cybercam.py:705-706` | `latest()` docstring 声称 `last_error` 供 UI 使用，实际不存在/无人读（同 C8），属于代码内文档漂移 |

## E. 小问题

| # | 位置 | 问题 / 建议 |
|---|---|---|
| E1 | `cybercam.py:684`、`618` | `if self.fps` 与 `max(1, self.fps)` 都是**不可达的防御分支**：`fps` 只来自 `load_settings`（146-149，白名单）和 argparse `choices`（2794），永远 ∈ {12,15,20,24,25,30}。要么在 `CameraStream.__init__` 里 `assert fps in (...)`，要么删防御并注释"此处仅接受白名单值" |
| E2 | `cybercam.py:593` | `self._recording` 只写不读（UI 用的是 `self.recording`），死状态，建议删 |
| E3 | `cybercam.py:135-138` | bool 强转把 `NaN` 变 True（`isinstance(nan,float)` 为真）；`timelapse_interval=True` 会被当索引 1 用（148 行的 `isinstance(int)` 放行 bool）。加 `not isinstance(x,bool)` 即可 |
| E4 | `cybercam.py:619-620` | `start()` 里连续两个空行，风格噪声 |
| E5 | `cybercam.py:848-849` | E6 守卫的副作用：直接 `return` 不清 `_record_raw/_record_path/_record_fps`（同 C12） |
| E6 | `cybercam.py:887` | `.mp4.partial` 与 `encode_mp4` 的 `.partial.mp4`（978）不一致，建议统一 |
| E7 | `cybercam.py:878` | 重封装没有 `-movflags +faststart`（`encode_mp4` 1002 行有），手机浏览器拖动加载会更差 |
| E8 | `cybercam.py:2713-2724` | draw 失败只写日志、不给用户任何提示；建议 `set_status("DISP ERR", 3)`（与 B5 一起改） |
| E9 | `selftest.py:22` | 固定 `fps=25` 构造 stream，未覆盖 `preview_fps` 12/30 与 `FrameDurationLimits` 边界，真机验证 B1 时不够用 |

---

## 四个"特别关注"的直接回答

1. **`FrameDurationLimits` 暗光压黑** —— 会。`(1000, 1e6/fps)` 只夹"最长帧时长"，等价于把 AE 曝光上限锁在 ~1/fps 秒；暗光下改前可通过降帧换取更长曝光，改后不行（见 B1）。同时它**不产出**目标帧率，所以 B2 的原始诉求并未解决，真正控速的仍是 `_loop` 的节流。需真机低照度对比（未验证）。
2. **节流移位后是否会重复发布** —— 不会。`_last_publish` 只在发布分支（697-703）写、只有 `_loop` 一个线程读写；判断用的是同一个 `now`（683 取值、700 落库），保证两次发布在"采集时间"上间隔 ≥ 阈值。唯一的时序偏差是 `now` 取在 JPEG 编码**之前**，使记录的发布时刻偏早（最多一个编码时长），可能让实际出帧率比配置值高几个百分点，但不会重复发布、也不会倒退。
3. **`capture_metadata()` 与 `stop_recording()` 的次序** —— 代码里是**先** `capture_metadata()`（853）**后** `picam.stop_recording()`（861），与你描述的次序相反；先读是合理的（停完编码器再读会拿到重建后的预览配置）。真正的安全问题不是次序而是 **无 `timeout` + 持 `camlock`**，可能冻结预览/UI（B6）；另外用它当整段 fps 不成立（B7）。
4. **`save_settings` 加 fsync 后的失败路径** —— 成功路径正确（数据先落盘再 rename），失败路径会留下 `.json.tmp`（C6），`replace` 后父目录未 fsync（掉电仍可能丢这次 rename，但旧文件完整）；另外 fsync 在 UI 按键线程里同步等待 SD 卡，连续切滤镜会掉帧（C7）。

## 已验证（本轮静态证据，均为 ✅）

| 项 | 证据 |
|---|---|
| `continue` 前锁与 request 均已释放 | 670-685 缩进：`with self.camlock:` 在 673（16 空格），`continue` 在 685（20 空格，位于 `try` 体、`with` 之外）；`request.release()` 在 679-680 的 `finally` |
| B1 节流确实在 BGR→RGB 拷贝之前 | 683-685 在 687 `Image.fromarray` 之前；注释内容与实现一致 |
| `fps` 取值域封闭，无 0/负值路径 | `load_settings:146-149` 白名单；`main:2794` `choices`；四处 `CameraStream(...)` 都用 `settings["preview_fps"]`（2182/2837/2859/2876） |
| `CAM ERR` 的 `inf` deadline 不会被 READY 复位覆盖 | 2610 `set_status(..., float("inf"))`；2659 `now > self.status_deadline` 对 `inf` 恒 False |
| 录像只有成功才删 raw、失败不误删 | 883-890：`raw.unlink` 仅在 `ok` 分支；失败分支只 rename，raw 始终保留 |
| `frame_image` 无跨作用域引用、无死循环 | 2714/2719 都在同一 `try` 内，之后不再使用；`last_draw/last_status`（2685-2686）在 try 之前已更新，最坏是每帧重试+`sleep(0.1)`，非紧循环（但会刷日志，见 C4） |
| C1 fsync 已加、注释已收窄 | 153-160；`os.fsync(fh.fileno())` 在 `os.replace` 之前 |
| C2 fbN 已从 path 派生 | 1469 `fbname = Path(path).name or "fb0"`；1471 用 `fbname` |
| C3 回退 stride 含 xoff | 1474 `self.stride = (self.xoff + self.w) * 2` |
| C5 mmap 长度已对账 | 1479-1482 `needed = stride*(yoff+h)`，`size < needed` 直接报错 |
| C7 8 参数完全自洽、产物未漂移 | `build_looks.py:82` 解包 8（`apply_layer`）；`fit_all.py:71` 解包 8（`model`）；`fit_all.py:113-118` 采样 8 项且顺序一致；`look_params.json` 每支 8 项（CN/NN/BB）；代码/参数文件中已无 `g_lin`（仅 `REVIEW*.md` 文档提及）；三支 cube 黑场角点与 REVIEW-2 记录逐位相同 → 删除 `g_lin` 未改产物 |
| C8 不再 `import cybercam` | `fit_all.py:30-51` 自带 `load_cube`；全仓无 `import cybercam` |
| E1 zoom / E2 bool / E6 stop 守卫 / E18 告警色 | 140-145；135-138；848-849；2167-2170（`startswith(("ERR","CAM ERR"))`） |

## 未验证项（需执行环境 / 真机）

- **B1**：暗光下 AE 曝光/亮度/噪点对比；`capture_metadata()["ExposureTime"]`、`FrameDuration` 实际被夹到多少；`FrameDurationLimits` 在 1640×1232 10-bit 模式下是否被 libcamera 钳制/报错
- **B6**：`capture_metadata()` 是否支持 `timeout`；编码中调用是否会抢 encoder 的 request 或阻塞
- **B7/C1**：`ffprobe -count_frames` 得到的真实帧数/时长与 `-r` 的偏差；录制 5 s 在 `Videos/` 与 `camera.log` 的实际产物
- **B2/B3/C9**：删除 ffmpeg / 强制失败后 UI 与 CLI 的提示、文件落点、`selftest.py` 行为
- **C3**：间歇性相机故障时状态栏与 `camera.log` 的实际抖动
- **B4/B5**：按键/切页是否会清掉 `CAM ERR`；并发抢 fb 时 overlay 绘制是否真会抛
- **B1/C7 影响面**：`save_settings` fsync 在真机 SD 卡上的耗时（按键卡顿是否可感知）
- 延续未验证：`FIX_FMT` 与内核结构一致性（REVIEW-2 C4，本轮未改）、`ScalerCrop` 在 1640×1232 下的解释、`build_looks.py` 全量逐值 diff、`fit_all` MAE 复现

## 仍遗留（REVIEW-2 未在本轮清单内，供决策）

| 编号 | 状态 |
|---|---|
| C4 `FIX_FMT` 缓冲区/`L` 位宽脆性 | 未改（32 位 armhf 可跑，aarch64 会错） |
| C9 `run()` 退出只 join 15 s，编码/延时子进程可能被截断 | 未改 |
| B6 MJPEG-in-MP4 浏览器播放 | **文案只改了一半**（D1） |
| E3–E5、E7–E17（`write_text` encoding、grain 条带、`make_array` 显式 copy、`proc` 死代码、`full_res` 默认双写、字体回退、`ActiveRecording.proc`、`Content-Disposition` 过滤、CLI 异常捕获、`fit_all` DOMAIN 归一化、PIL 量化差、CLI photo 路径、numpy 缺失空转） | 未改 |
| D1–D13 | 部分改（见 D 表），多数仍在 |

## 建议修复顺序

1. **B2 + B3 + C9 + C10**（一处改动，收益最大）：`stop_recording` 用 `try/finally` 收尾；失败必须记日志 + 抛/返回错误；UI/CLI 不要无条件报"已保存"；`.mjpeg` 至少要在相册可见（或提示路径）；同步修 `selftest.py`
2. **B1**：先决定"要不要锁最低帧率"，再回退/放宽 `FrameDurationLimits`，并同步 NOTES:29 的措辞；用 selftest 加暗光对照
3. **B7 + C1 + C2**：改成"帧数 / 墙钟时长"求 fps，夹取 1–120，失败回退 30
4. **B4 + C4**：状态分层（粘滞错误 vs 短消息 vs `REC` 派生状态）
5. **B5 + C5 + E8**：抽 `safe_draw()`，六处统一；`start()` 整体 try/close
6. **B6 + C12**：`FrameDuration` 改读 `_loop` 缓存，`_record_*` 单点复位
7. **C6 + C7**：`save_settings` 失败清理 + 合并写盘
8. **C8 + D1–D8 + E 类**：文档与死代码一次性对齐
