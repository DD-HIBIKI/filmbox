# CyberCam 第二轮审计（REVIEW-2）

- 审计日期：2026-11-22（新会话；上一轮会话卡死）
- 审计基线：HEAD `89a60d7`；`cybercam.py` 实测 **2840 行**（任务说明写 2855，以 `read` 到的 2840 为准）
- 方法：**只读静态审计**。仅用 `read` / `glob` / `write`，未执行 Python / shell（工作区 shell 被沙箱 ACL 挡住）。凡需运行/真机/浏览器才能确认的结论，均标「未验证 —— 需执行环境」
- 对照：上一轮 `REVIEW.md`（基线 `c8f1cad`）、`REVIEW-RESPONSE.md`（本轮修复自述）
- 分级：A 阻断/回归 · B 功能性问题 · C 可靠性/工程问题 · D 文档/注释漂移 · E 小问题
- 覆盖：`cybercam.py` 全文 2840 行逐块读完；`build_looks.py`、`fit_all.py`、`look_params.json`、`luts/README.md`、`README.md`、`NOTES.md`、`install.sh`、`tools/lutcompose.py`、`luts/fuji_provia.cube`、`luts/look_*.cube` 头部/角点/行数

## 结论摘要

| 级别 | 数量 | 说明 |
|---|---|---|
| **A** | **0** | 未发现阻断/回归；8 个重点里 5 个改动方向正确 |
| B | 7 | 节流实现（2 条）、错误状态"假就绪"、录像重封装丢数据、`-r 30`、MJPEG-in-MP4 浏览器、`display.draw` 无保护 |
| C | 11 | `save_settings` 无 fsync、Framebuffer 4 条、`start()` 泄漏、`g_lin` 死参数、`fit_all` Windows 不可跑、退出竞态等 |
| D | 13 | README/NOTES 仍有多处与代码/本轮数据不符（含"浏览器可播 MJPEG"） |
| E | 17 | 类型校验、死代码、默认值双写、文档细节等 |

### 8 个重点的逐项判定

| # | 重点 | 判定 | 主要依据 |
|---|---|---|---|
| 1 | CLI `video` 是否走 `CameraStream`、`Recording` 残留 | ✅ **成立** | `main()` 2814-2832 只构造 `CameraStream`；全文无 `Recording` 定义/实例化，只剩 `ActiveRecording`（890）与 `self.recording` 属性名 |
| 2 | `_loop` 的 `1/fps × 0.85` 边界 | ⚠️ **首帧/掉帧/fps 变化都无问题；但节流位置和"整数分频"是 B 级问题** | 667-668、663；发布速率=相机帧率整数分频，"设 12 得 13"是对隐藏帧率的取整 |
| 3 | `stream.error` 是否重复刷状态栏 | ⚠️ **不重复刷，但更糟：报一次、8 秒后消失、不恢复、不重报** | 672-675（从不擦除）、2558-2563（按字符串去重）、2609-2610（8 秒后重置 READY） |
| 4 | `_still_config`/`_record_config` 的 `sensor=` 与 `ScalerCrop` 冲突 | ✅ **静态不冲突** | 738-741/788-791 锁 sensor；810-811 录像按输出宽高比下发 crop；770 still 显式全幅 crop。仅"1640 模式下 ScalerCrop 按全阵列解释"需真机复核 |
| 5 | `load_settings` zoom 归一化 | ✅ **已修** | 134-140 覆盖字符串/None/越界/NaN/Inf；残留 `true`→1.0 的类型宽松（E） |
| 6 | `Framebuffer.stride` sysfs 回退 | ⚠️ **方向对；回退分支有 4 个 C 级坑** | 1427-1434：写死 fb0、回退不含 xoff、FIX_FMT 大小脆性、mmap 长度未对账 |
| 7 | `build_looks.py` 与 `look_*.cube` 一致性/可复现 | ✅ **一致（静态强证据）**；⚠️ 完整逐值复现/fit MAE 需执行环境 | 3 支 cube 头部 4 行、35941 行=4+33³、黑场角点与 `srgb_encode(lift)` 吻合到 2~5e-5；`g_lin` 是死参数（C） |
| 8 | `save_settings` 原子写、halation/grain 不复制整幅 | ⚠️ **rename 原子 ✓，掉电 durability ✗**；**不复制 ✓ 但契约改成原地修改** | 148-152（无 fsync）；280/304（原地 paste，当前调用方安全） |

---

## B. 功能性问题

### B1. `_loop` 的节流做在"采完帧之后"，丢掉的帧已付全部转换成本

- 位置：`cybercam.py:651-680`
- 证据：657 行 `capture_request` → `make_array`（659）→ 663 行 `Image.fromarray(np.ascontiguousarray(pixels[:,:,::-1]))` → 665 行可能 `fill_size`，**全部在 667-668 行的节流判断之前完成**；节流只省下 669-671 的 `JPEG` 编码。
- 影响：`preview_fps=12` 时相机仍全速做 BGR→RGB 拷贝 + PIL 对象构造（512 MB 单板机），设置只降低了编码/发布量，没降采集与转换的 CPU。
- 建议：把阈值判断移到 `capture_request` 之前，或给视频配置加 `controls={"FrameDurationLimits": ...}`；至少把 `Image.fromarray` 放到判断之后。

### B2. 有效帧率是"相机帧率整数分频"，0.85 只是补偿常数，遇到 30 fps 源会进平台期

- 位置：`cybercam.py:667-668`
- 证据：`now - _last_publish < (1.0/self.fps)*0.85`。发布间隔 ≈ `frame_interval × ceil(0.85/(fps×frame_interval))`。若相机 30 fps：`fps=24/25` 阈值 33.99/34 ms > 33.3 ms → 每 2 帧 = **15 fps**；`fps=12` → 每 3 帧 = 10 fps。`REVIEW-RESPONSE` 的实测（设 12→13.0、设 25→25.7）反推真机相机帧率约 50 fps（12→4 帧 80 ms=12.5；25→2 帧 40 ms=25.0），即**读数吻合是因为它对隐藏帧率取了整，而不是节流准**；0.85 的 +17% 过冲只在"每帧都发布"时出现。
- 建议：用 `FrameDurationLimits` 让相机自己出目标帧率；或按时间戳 `>=` + 明确余量；文档别把实测值写成"设置生效值"。
- 补充：首帧无问题（`_last_publish=0.0`，`monotonic()` 远大于阈值）；掉帧后恢复也无问题（672-675 不更新 `_last_publish`，恢复即补帧）。`self.fps` 运行期只由 `--preview-fps`/`config` 在下次启动时生效（`CameraStream` 构造 2135/2781/2803/2820），运行期没有改 fps 的入口。

### B3. `stream.error` 只写不擦 → 状态栏"报一次、8 秒后回到 READY"，相机持续故障时界面撒谎

- 位置：`cybercam.py:672-675`（只写）、`676-680`（成功发布不清）、`2558-2563`、`2609-2610`
- 证据：

```python
672:            except Exception as exc:
673:                self.error = str(exc)[:80]
...
2558:                stream_error = self.stream.error if self.stream else None
2559:                if stream_error and stream_error != self._cam_error_seen:
2560:                    self._cam_error_seen = stream_error
2563:                    self.set_status("CAM ERR " + stream_error[:28], 8)
...
2609:                if self.status != "REC" and not self.busy and now > self.status_deadline:
2610:                    self.status = "READY"
```

- 结论：**"重复刷状态栏"不成立**（同串只报一次，去重有效）。但 `error` 永不回清 → `stream_error` 恒真、`_cam_error_seen` 恒等于它：错误只显示 8 秒，之后状态栏回 `READY`，**预览冻结但界面显示就绪**，同一错误不再提示，也没有"已恢复"提示。
- 建议：`_loop` 成功发布一帧时 `self.error = None`；主循环在 `error` 为空时清 `_cam_error_seen`；错误持续期间 `set_status(..., float("inf"))`；恢复时给 2 秒 `CAM OK`。
- 附带（E）：`draw_ui` 2120-2123 只对 `status.startswith("ERR")` 用告警色，`CAM ERR` 仍按 MINT 画，视觉上不像错误。

### B4. 录像重封装失败时会删掉唯一的完整 `.mjpeg`，留下半截 mp4

- 位置：`cybercam.py:823-848`（关键 840-848）
- 证据：

```python
840:        if raw is not None and raw.exists():
841:            try:
842:                run_command(["ffmpeg", "-y", ...], timeout=90)
843:            finally:
844:                if final.exists() and final.stat().st_size > 0:
845:                    raw.unlink(missing_ok=True)
846:                else:
847:                    raw.rename(final.with_suffix(".mjpeg"))
```

- `run_command` 在 ffmpeg 非 0 退出/超时时抛异常；`ffmpeg -y` 通常已写出非空半截 `final` → `finally` 删掉 raw、保留坏 mp4，异常继续上抛。
- 建议：只在确认 ffmpeg 成功（返回码 0 + `ffprobe` 时长/帧数校验）后删 raw；失败时保留 raw 并提示。

### B5. `-r 30` 硬编码重封装帧率（未验证 —— 需执行环境）

- 位置：`cybercam.py:842`
- 证据：`["ffmpeg","-y","-loglevel","error","-r","30","-i",raw,"-c:v","copy",final]`；MJPEGEncoder 按传感器实际帧率写帧，`-r 30` 强行按 30 fps 计时。1640×1232 10-bit 模式上限约 41.6 fps，若实际偏高，成片慢放；偏低则快放。
- `REVIEW-RESPONSE` 的 `ffprobe` 只报了 1280×720/mjpeg，**没报帧率与时长**。
- 建议：录制时记录起止时间/帧数，换算真实 fps 传给 ffmpeg（或写 `-r` 为实测值）。

### B6. MJPEG-in-MP4 的浏览器播放（未验证 —— 需浏览器/真机；且文档明确宣称可播）

- 位置：`cybercam.py:813-843`（`-c:v copy` 把 MJPEG 原样封进 mp4）；`README.md:94`、`NOTES.md:151` 都写"VLC / 浏览器 / 本机相册都能播"
- 分析：Chrome/Firefox 基本不解码 MP4 容器内的 Motion-JPEG；设备本地相册走 ffmpeg（`VideoPlayback` 1497-1598）没问题，但 **WifiShare 网页 `<video>`（`_index` 1238）很可能播不了录像**（延时用 libx264，不受影响）。
- 建议：实测手机浏览器；若不支持，改为网页侧提示"下载后播放"或转码；同时修正 README/NOTES 的断言。

### B7. 主循环 `display.draw(...)` 没有任何异常保护

- 位置：`cybercam.py:2530-2668`（2567、2575、2582、2594、2603、2663 六处 `self.display.draw(...)` 调用）
- 证据：B3 的修复只包了 `self.key(code)`（2551-2557）；`Framebuffer.draw`（1440-1467）里的 mmap 切片越界（见 C5）或 fb 被占用会抛异常，直接冒到 `run()` 的 `finally` 退出进程。
- 建议：把 draw 也包一层，失败时降级（跳过该帧 / 状态栏写错），保持"单个事件不杀进程"的既定目标。

---

## C. 可靠性 / 工程问题

### C1. `save_settings` 没有 fsync，掉电仍可能得到 0 字节/旧内容；注释过度承诺

- 位置：`cybercam.py:148-152`
- 证据：`tmp.write_text(...)` → `os.replace(tmp, CONFIG)`，注释 "atomic: a power cut cannot half-write it"。rename 原子 ≠ 数据落盘：未 `fsync(tmp)`、未 fsync 目录时，掉电可出现"rename 已生效、文件内容未落盘"。
- 建议：`tmp.write_text(...)` 后 `os.fsync(tmp.fileno())`，`os.replace` 后 fsync 父目录；注释改成"不会出现半截 JSON"。另（E）：`write_text` 未指定 `encoding`；异常时 `.tmp` 残留。

### C2. `Framebuffer` 的 sysfs stride 写死 `fb0`，与构造参数脱钩

- 位置：`cybercam.py:1414-1430`
- 证据：`Framebuffer.__init__(self, path="/dev/fb0")` 但 1428 行读 `/sys/class/graphics/fb0/stride`。传 `/dev/fb1` 时读错设备。
- 建议：从 `self.path` 派生 `fb{N}`（`Path(path).name`）。

### C3. stride 回退分支不含 `xoff`，带 panning 偏移的 fb 会直接起不来

- 位置：`cybercam.py:1430`、`1433`
- 证据：回退 `self.stride = self.w*2`；随后 `if self.stride < (self.xoff+self.w)*2: raise RuntimeError`。`xoff>0` 时必然抛错，而不是退到保守值。
- 建议：回退 `(xoff + w) * 2`（或向上取偶），并把"无法推断 stride"作为单独错误。

### C4. `FIX_FMT` 的缓冲区长度与内核 `struct fb_fix_screeninfo` 不保证一致（未验证 —— 需执行环境）

- 位置：`cybercam.py:1411`、`1422-1425`
- 证据：`FIX_FMT = "16sL" + "I"*6 + "HHHI"`，`bytearray(struct.calcsize(FIX_FMT))` 传 `FBIOGET_FSCREENINFO`。该 ioctl 是整结构 `copy_to_user`，用户缓冲区偏小会 EFAULT。静态估算 32 位 ARM 下 calcsize ≈56 字节，内核结构约 64 字节；真机能跑说明该内核/驱动恰好匹配，属脆性依赖。`parsed[2]`（`smem_len`）的 32/64 位偏移是对的（20/24）。
- 建议：用 128 字节 buffer + `struct.unpack_from`，或按内核头文件 sizeof 分配；顺手删掉没人用的 `I*6/HHHI`。

### C5. `mmap` 长度未与 `stride*h` 对账，`draw()` 有每帧崩的路径

- 位置：`cybercam.py:1435`、`1462-1467`
- 证据：`self.mm = mmap.mmap(self.fd, size, ...)`，`size = parsed[2]`（`smem_len`）；`draw()` 的慢路径按 `stride/yoff` 切片写入。若驱动 `smem_len` 偏小或 `xoff/yoff` 超界 → mmap 切片越界 `IndexError`，主循环无保护（B7）直接退出。
- 建议：加 `size >= stride*(yoff+h)` 校验；不满足时明确报错。

### C6. `CameraStream.start()` 失败时不 `close()` 已构造的 `Picamera2`

- 位置：`cybercam.py:597-619`
- 证据：`picam = Picamera2()`（603）之后 `create_*_configuration`/`configure`/`start` 抛异常时没有 `picam.close()`；`self.picam` 仍为 None。重试可能"设备忙"（NOTES 一·四 ①的 ENOMEM/占 buffer 问题）。
- 建议：`try: ... except: picam.close(); raise`。

### C7. `g_lin` 是**死参数**：fit_all 拟合了它，build_looks 解包后从不使用

- 位置：`build_looks.py:82`（解包）、`73-92`（函数体）；`fit_all.py:54`（同样解包不用）、`96`（随机搜索仍采样它）；`look_params.json:1-80`（第 2 个值，如 CN=0.93747）
- 影响：拟合浪费一个维度、`look_params.json` 名义上"9 参数"实际只有 8 个生效；文档 `NOTES.md:251` 又写成 "9-9 个参数"；`REVIEW-RESPONSE`/`luts/README.md:11` 的"9 参数模型"表述不准确。
- 建议：要么把 `g_lin` 接进模型（拟合时用），要么从模型/参数文件/文档里删掉。

### C8. `fit_all.py` 在 Windows 上无法运行，与 docstring 的 Windows 示例路径矛盾

- 位置：`fit_all.py:30-34`（`import cybercam`）、`9-10`（`--ref "D:/path/..."`）；`cybercam.py:22` 顶层 `import fcntl`
- 影响：`cybercam` 顶层 import `fcntl` 在 Windows 不存在 → `ImportError`；即"过程记录"脚本只能 Linux 跑。`build_looks.py` 只用 numpy，可以在任意平台复现产物。
- 建议：`fit_all` 里只复制 `load_cube_lut` 的解析逻辑（或 try/except 打桩 `fcntl`），别 `import cybercam`；文档写明 fit 需 Linux。

### C9. `run()` 退出时 worker 只等 15 s；延时/编码任务可能被静默中断

- 位置：`cybercam.py:2669-2684`
- 证据：`self.worker.join(timeout=15)`；而 `TimelapseCapture.run()` 单帧 `rpicam-still` timeout 35 s（1048）、`encode_mp4` timeout 90 s（843/995）。15 s 后继续关 display/devices 并退出，daemon 线程/ffmpeg 子进程仍可能在写文件 → 半截 `.partial.mp4` 或空帧。
- 建议：`busy`/`ENCODING` 时提示并延长等待或显式 kill 子进程。

---

## D. 文档 / 注释漂移

| # | 位置 | 问题 |
|---|---|---|
| D1 | `cybercam.py:4` | 文件头仍写 "rpicam still/video/MJPEG backend"，CLI 录像已改 `CameraStream` |
| D2 | `cybercam.py:60`、`2702` | `FULL_FOV_MODE` 已不被预览/录像使用（sensor 改由 dict 锁），只剩 `command_diagnose` 打印 → 名字误导；`FULL_STILL_MODE` 仍用于 rpicam still/延时，保留合理 |
| D3 | `README.md:86` | 目录里仍写根目录 `lutcompose.py`；实际已移到 `tools/lutcompose.py`（其文件头也标注"历史工具，不参与出包"） |
| D4 | `README.md:94`、`NOTES.md:151` | "MP4 里是 MJPEG：VLC/浏览器/本机相册可播" —— 浏览器对 MJPEG-in-MP4 的支持很差，见 B6 |
| D5 | `NOTES.md:20-31`、`41` | 「一、胶片 LUT」整段仍描述旧 rpicam 实现：预览 224×168（现在是 Picamera2 320×170）、`preview_fps` 默认 30（`cybercam.py:121` 实为 25）、`luts/` "11 支 + fuji-luts.tgz"（实为 18 支、无 tgz） |
| D6 | `NOTES.md:235` | 「一·十三」仍写 "换成自建参数化 look（`make_looks.py`）"，该脚本已删除（`REVIEW-RESPONSE` C6） |
| D7 | `NOTES.md:251`、`254` | "9-9 个参数"笔误；CN 7.4 / NN 5.5 / BB 7.1 的 MAE 需外部参考 LUT（不在仓库）+ 跑 `fit_all` 才能复现（未验证 —— 需外部依赖） |
| D8 | `README.md:15,17,18`；`NOTES.md:67,211,214` | 实测数字互相对不上：8MP 拍照 README 1.0 s / NOTES 0.86 s / `REVIEW-RESPONSE` 0.8 s；720p 录像启动 NOTES 0.29 s / response 0.13 s；1x vs 2x 画面差 NOTES 51 / response 72；录像中预览 9.7 vs 10.0 fps；胶片 7.6 s vs `cybercam.py:78` 注释 "~6s" |
| D9 | `NOTES.md:288-294` | 「安装」仍只有 `python3-qrcode`；`install.sh:22` 已补 picamera2/numpy/PIL/evdev/ffmpeg，NOTES 没跟上 |
| D10 | `NOTES.md:298`、`305` | 标题"已做的验证（本地，非真机）"与末行"✅ 已在真机验证"仍矛盾（上一轮 D4 未清干净） |
| D11 | `luts/README.md:11` | "参数由 `fit_all.py` 对目标观感做最小二乘拟合" —— 实际是随机搜索（`fit_all.py:95-105`） |
| D12 | `README.md:78-89` | 目录清单缺 `build_looks.py` / `fit_all.py` / `look_params.json` / `tools/` / `shutter.wav` / `REVIEW*.md` |
| D13 | `NOTES.md:33`、`46` | "必须是 33³" 与随后"G'MIC 那批，13³"自相矛盾（实际 `fuji_provia.cube` 是 13³，`look_*.cube` 是 33³） |

---

## E. 小问题

| # | 位置 | 问题 / 建议 |
|---|---|---|
| E1 | `cybercam.py:134-140` | `"zoom": true` 因 `float(True)==1.0` 被接受；建议先 `isinstance(x,(int,float)) and not isinstance(x,bool)` |
| E2 | `cybercam.py:120-145` | `sound` / `lut_full_res` / `clean_ui` 无类型校验（`"sound":"false"` 为真值） |
| E3 | `cybercam.py:150-152` | `write_text` 未指定 encoding；异常时 `.tmp` 残留 |
| E4 | `cybercam.py:321-330` | grain 以 288 行为条带生成、`np.roll` 条带内自循环，8MP 图条带边缘可能有轻微纹理接缝 |
| E5 | `cybercam.py:657-663` | `request.release()` 后才用 `pixels`，依赖 picamera2 `make_array()` 默认 `copy=True`；建议显式 `copy=True` |
| E6 | `cybercam.py:824-836` | `stop_recording` 无 `self.picam is None` guard，会抛 `AttributeError`（UI/CLI 的 finally 里可能再抛） |
| E7 | `cybercam.py:2315` | `self.stream.proc is None` 恒真（E2 的 legacy 属性）→ 兜底条件实际只剩 `recording is None`，误导 |
| E8 | `cybercam.py:2163-2201` | `film_busy` 不占 `busy`：8MP 胶片渲染期间仍可再按快门，内存峰值叠加窗口 |
| E9 | `cybercam.py:121` vs `2178` | `lut_full_res` 默认 True vs `.get(..., False)` 双写 |
| E10 | `cybercam.py:1827-1833` | DejaVu 缺失时 `ImageFont.load_default()` 不带 size，320×170 上字极小 |
| E11 | `cybercam.py:890-896`、`1233`、`1277`、`1370-1377` | E2/E6/E7/E1 遗留：`ActiveRecording.proc` 死代码；`_index` 仍 `[:150]` 无分页；`Content-Disposition` 未过滤控制字符；`WifiShare.stop()` 无 `server_close()` 且无调用点 |
| E12 | `cybercam.py:2834-2836` | `main` 只捕获 `OSError/RuntimeError/TimeoutExpired`；CLI `video` 的 `time.sleep` 按 Ctrl+C 会裸 traceback |
| E13 | `cybercam.py:2814-2832` | CLI `video` 不应用 `--filter`/look（与 UI 录像一致），文档应写明 |
| E14 | `build_looks.py:36` | `read_cube` 直接跳过 `DOMAIN_MIN/MAX` 不归一化（当前 Provia 是 0-1，安全；换源 LUT 会错） |
| E15 | `fit_all.py:67-69`、`88-92` | 拟合用 PIL 8-bit `filter()` 取样，build 用 float 三线性，两者有极小量化差 |
| E16 | `cybercam.py:2773-2779` | CLI `photo` 走模块级 rpicam `take_photo`，忽略 `--lut-full`/变焦（E3 遗留，不在本轮 8 项） |
| E17 | `cybercam.py:663` | numpy 缺失时 `_loop` 每 0.2 s 抛 `AttributeError` 空转 |
| E18 | `cybercam.py:2120-2123` | `CAM ERR` 不以 `ERR` 开头 → 告警色不生效（与 B3 同） |

---

## 已验证项（本次静态证据）

| 项 | 证据 |
|---|---|
| CLI `video` 与 UI 共用 `CameraStream`；无 `Recording` 残留 | 全文通读；`main()` 2814-2832；无 `def Recording`/`Recording(` |
| still/record 配置都锁了 sensor mode（B4 修复） | 738-741、788-791；`capture_still` 770 显式全幅 `ScalerCrop` |
| 录像不再横向拉伸、480P 为全 FoV | 810-811 按输出宽高比下发 crop；`_record_config` sensor 1640×1232 |
| `load_settings` zoom 坏值不再崩 | 134-140：`TypeError/ValueError` 与"不在 ZOOMS"双重兜底 |
| `save_settings` rename 原子 | 150-152 同目录 tmp + `os.replace` |
| `add_halation`/`add_grain` 不再复制整幅，且当前调用方安全 | 280/304、310/330；`filtered_still` 512-515 传的是新建 `result` |
| `CAM ERR` 不会每帧刷屏 | 2559 按字符串去重 |
| `crop_rect` 四档与 NOTES 表格一致 | `_rect_in` 静态计算 = (0,360,3280,1742)/(546,652,2186,1160)/(820,796,1640,870)/(1094,942,1092,580) |
| `look_*.cube` 确为 `build_looks.py` + `look_params.json` 产物 | 三支 cube：4 行头 + 35941 行（=4+33³）、`TITLE "look_*"`；黑场首行 = `srgb_encode(lift)` 预测值：CN 0.07490/0.07510/0.07507 vs 实测 0.074881/0.075080/0.075057；BB 0.06137/0.06151/0.06149 vs 0.061335/0.061496/0.061438；NN 0.11533 vs 0.115327~0.115486（含 Provia 黑场偏色与矩阵交叉项） |
| `build_looks.py` 具备独立复现条件 | 依赖仅 numpy；`fuji_provia.cube`（13³, MIT）与 `look_params.json` 均在仓库；路径相对 `HERE`；float 三线性、无量化、无随机；写入格式确定 |
| `install.sh` 依赖检查已补全（D7 修复） | `install.sh:22` 六个包 |
| `command_diagnose` 已含 picamera2（D6 修复） | 2695-2699 |
| `tools/lutcompose.py` 已移到 `tools/` 且标注历史工具（C5 修复） | `tools/lutcompose.py:1-6` |

## 未验证项（需执行环境 / 真机 / 浏览器）

- `ScalerCrop` 坐标在 1640×1232 传感器模式下是否真按 3280×2464 全阵列解释（视频+变焦）
- 相机实际帧率与 `FrameDurationLimits` 行为；节流后真实发布 fps（B1/B2 的量化预测）
- 录像真实 fps 与 `-r 30` 是否导致速度失真（B5）
- 手机浏览器能否播放 MJPEG-in-MP4（B6）
- `Framebuffer`：sysfs stride 实际值、`FIX_FMT` calcsize 与内核结构是否一致、mmap 长度是否够（C2-C5）
- `build_looks.py` 全量逐值 diff（本次只验证了角点/格式/行数）；`fit_all.py` 的 MAE 7.4/5.5/7.1（需仓库外的参考 LUT + 三张样图）
- `stop_recording` 的 ffmpeg 失败路径实际行为；`encode_mp4` 在真机上的耗时
- `selftest.py` 全流程、`install.sh` 实机安装

## 建议修复顺序

1. **B3 + B7**：错误状态"假就绪"与 `display.draw` 保护——用户直接感知、成本低
2. **B1 + B2**：节流位置与 fps 语义（`FrameDurationLimits` 一次解决两处）
3. **B4 + B5**：录像重封装的数据丢失与帧率（同一段代码，一起改并实测 ffprobe）
4. **B6**：实测浏览器，再决定转码或改文案
5. **C1**：`save_settings` fsync；**C2-C5**：Framebuffer 一批
6. **C7 + C8**：LUT 工具链（`g_lin` 死参数、`fit_all` 平台限制）与 `build_looks` 复现命令写进 README
7. **D1-D13**：文档一次性对齐（尤其 D4 的"浏览器可播"与 D8 的实测数字）
8. **E 类**：清理死代码与类型校验

## 复现命令（供下一轮在可执行环境里跑）

```bash
# 全量重建 look 并比较（需 numpy）
python3 build_looks.py --out /tmp/look2
for f in look_classic_neg look_nostalgic_neg look_bleach_bypass; do
  diff -q /tmp/look2/$f.cube luts/$f.cube
done

# 录像帧率/编码（真机）
python3 cybercam.py video --seconds 5 --video-quality 720p
ffprobe -v error -show_entries stream=codec_name,width,height,avg_frame_rate,duration -of json Videos/*.mp4

# 相机帧率（真机）
python3 selftest.py
```
