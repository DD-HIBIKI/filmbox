# CyberCam 代码检查报告

- 检查日期：2026-11-22
- 检查对象：`cybercam.py`（2855 行，v0.5.3 改造版）、`selftest.py`、`install.sh`、
  `lutcompose.py`、`make_looks.py`、`build_looks.py`、`fit_all.py`、`luts/`（18 支 `.cube`）、
  `README.md`、`NOTES.md`、`luts/README.md`、`cybercam-film-share.patch`
- 检查基线：`c8f1cad`（`cybercam.py` 自 `56b69c3` 起未改动）
- 方法：逐行静态阅读 + `ruff` 静态检查 + 在本机（Windows，`fcntl` 打桩）对可离线验证的
  纯函数/HTTP 层/绘图/编码路径做实测；相机层（Picamera2、framebuffer、evdev）无法在本机运行，
  相关结论标注为「需真机确认」

## 结论摘要

| 级别 | 数量 | 说明 |
|---|---|---|
| A 回归/阻断 | 1 | 根 `README.md` 被 LUT 包说明覆盖，项目说明丢失 |
| B 功能性问题 | 7 | 设置不生效、错误静默、sensor mode 未锁、CLI 拉伸等 |
| C LUT 产物与工具链 | 7 | 已发布 look 与文档声称的生成方式不一致、双重编码、脚本不可复现 |
| D 文档漂移 | 8 | NOTES/README 与代码现状不符 |
| E 小问题 | 12 | 死代码、非原子写、资源累积等 |

**核心结论**：`cybercam.py` 的相机层、变焦几何、LUT 解析、HTTP 相册鉴权/断点续传、
MP4 导出、相册删除这些主路径实测都是对的；剩下的问题集中在
**「设置项没接上」「出错时静默」「sensor mode 没锁」** 三类，
以及最近一轮 LUT 授权清理留下的 **产物与文档不一致**。

---

## A. 回归 / 阻断

### A1. 根 `README.md` 被 LUT 包说明覆盖

- 位置：`README.md`（31 行）、`luts/README.md`（31 行）
- 现象：根 README 现在是「胶片 LUT 包（18 支）」的授权说明，与 `luts/README.md` 内容重复；
  原来的项目说明（实机数据、相机层结构、功能列表、安装、自检、目录、已知取舍）全部丢失。
- 证据：`git show 32d893a -- README.md` 显示 94 行被替换为 31 行；`git show 7ceee4b:README.md`
  仍是完整版。
- 影响：新用户/上架审核看不到项目怎么装、怎么跑、有什么限制。
- 建议：`git show 7ceee4b:README.md > README.md` 恢复，LUT 授权表只保留在 `luts/README.md`，
  根 README 里用一行链接指过去。

---

## B. 功能性问题（`cybercam.py`）

### B1. `preview_fps` / `--preview-fps` 完全没生效

- 位置：`cybercam.py:556`（`self.fps = fps`）、`cybercam.py:641-666`（`_loop`）
- 现象：`fps` 存进 `CameraStream.fps` 后没有任何地方读取；`_loop` 无节流，
  `create_video_configuration` 也没设 `FrameDurationLimits`。设置里的 12/15/20/24/25/30
  和 CLI 的 `--preview-fps` 都是空转，预览永远跑相机给的最大帧率。
- 证据：全文件 grep `self.fps`，`CameraStream` 内只有赋值没有读取。
- 建议：要么在 `_loop` 里按 `1/fps` 节流（或给配置加 `controls={"FrameDurationLimits": ...}`），
  要么把这个设置项和 CLI 参数删掉，别留假开关。

### B2. 相机出错时是静默的

- 位置：`cybercam.py:660`（`self.error = str(exc)[:80]`）
- 现象：`CameraStream.error` 被写入但全仓库没有任何地方读取。相机起不来
  （ENOMEM、numpy 缺失、残留进程占着 buffer）时，界面永远停在 `STARTING CAMERA`，
  `camera.log` 里也没有记录。
- 建议：`run()` 主循环里把 `self.stream.error` 显示到状态栏并写一次日志（去重，别每帧刷）。

### B3. 主循环没有异常保护

- 位置：`cybercam.py:2588`（`for code in read_keys(self.devices): self.key(code)`）
- 现象：`key()` 里 `leave_gallery()` 的 `self.stream.start()`（`cybercam.py:2388`）、
  `save_settings()`、`Gallery()` 的 `stat()` 都可能抛异常，一次就整个 app 退出。
- 建议：包一层 `try/except Exception`，记日志 + 设状态，别让单个按键事件杀掉进程。

### B4. 录像/拍照的 sensor mode 没锁 → 480P / VGA 是 2× 中心裁切

- 位置：`cybercam.py:767-775`（`_record_config`）、`cybercam.py:718-726`（`_still_config`）
- 现象：`start()`（`cybercam.py:595-598`）明确传了 `sensor={"output_size": (1640,1232)}`，
  注释也写了「640×480 是 2× 中心裁切」；但 `_record_config` / `_still_config` 都没传。
  libcamera 会挑「最接近请求尺寸」的模式，于是：
  - `VIDEOS[3] = (640,480)` 标着 **"WIDE 480P / full FoV"**，实际落到 640×480 裁切模式
    → 视野是 2× 中心裁切，注释是错的；
  - 拍照选 **VGA** 同理，且 `_crop_file`（`cybercam.py:728-738`）仍按全幅坐标算裁切框
    → VGA + 2x 实际变成 4×；
  - 720p 录像落到原生 1280×720（16:9 裁切）模式，`_apply_crop(1.778)` 的 ScalerCrop
    会被钳制；录完切回 4:3 预览时取景会跳一下。
- 需真机确认：`st.picam.camera_configuration()['sensor']` 看实际选的模式。
- 建议：`_record_config` / `_still_config` 也传 `sensor={"output_size": (1640,1232), "bit_depth": 10}`；
  或者把 480P 预设的注释改成「2× 裁切」。

### B5. CLI `video --video-quality 720p` 会横向拉伸

- 位置：`cybercam.py:897-900`（`Recording.start` 的 `--mode 1640:1232:10:P`）
- 现象：rpicam-vid 把 4:3 全幅缩放到 1280×720 → 拉伸。UI 的录像路径没这问题
  （走 `_record_config`），只有 CLI 这条老路径有。
- 建议：CLI 也改走 `CameraStream.start_recording`，或按输出宽高比传 `--mode`。

### B6. `settings.json` 里 `zoom` 是坏值 → app 起不来

- 位置：`cybercam.py:120-138`（`load_settings`）、`cybercam.py:2174`（`float(...)`）
- 现象：`load_settings` 校验了 filter/resolution/video/rotation/preview_fps/timelapse_interval，
  唯独没校验 `zoom`；`CyberCamUI.__init__` 直接 `float(...)` 抛 `ValueError`，
  而 `main()` 只捕获 `OSError/RuntimeError/TimeoutExpired` → 直接 traceback。
- 证据：实测 `{"zoom": "abc"}` 被原样返回。
- 建议：`load_settings` 里把 `zoom` 归一化到 `ZOOMS`（非法值回落 1.0）。

### B7. `Framebuffer` 的 `FIX_FMT` 解析错位（潜伏）

- 位置：`cybercam.py:1454`（`FIX_FMT`）、`cybercam.py:1468`（`parsed[8]`）
- 现象：`"16sL" + "I"*6 + "HHHI"` 里 `parsed[8]` 实际是 `line_length` 的**低 16 位**，
  不是 `line_length` 本身。当前 320×170/16bpp 的 `line_length=640` 刚好没暴露；
  一旦换屏或 `line_length ≥ 65536` 就会算错 stride。
- 建议：改成 `"16sL" + "I"*4 + "HHH" + "I"` 取 `parsed[9]`，或 `struct.unpack_from("I", f, 48)`。

---

## C. LUT 产物与工具链

### C1. 已发布的 `look_*.cube` 不是 `build_looks.py` 的产物

- 位置：`luts/look_*.cube`、`build_looks.py`、`fit_all.py`、`NOTES.md:243-256`
- 现象：`NOTES.md` 一·十四 声称 CN/NN/BB 已「用 G'MIC Provia 打底 + 拟合参数」重做，
  拟合 MAE 7.4 / 5.5 / 7.1；但 `32d893a` 只加了脚本和参考 LUT，**没有重新生成
  `luts/look_*.cube`**。实测仓库里的三支 look 与 `make_looks.py` 的输出**逐值完全一致**
  （max diff 0.000000），即仍是旧的显示空间版本。
- 影响：文档描述的产物与实际发布的文件不是一回事；一·十四 的 MAE 数字与产物无关。
- 建议：要么用 `build_looks.py` 重新生成并提交三支 look（同时提交 `look_params.json`），
  要么把一·十四 改成「已实现但未替换进包」。

### C2. `make_looks.py` 双重 sRGB 编码，BLEACH BYPASS 明显偏亮

- 位置：`make_looks.py:57-81`
- 现象：`classic_neg` 等三个函数都是 `v = srgb_encode(rgb)` → `_display_ops(v)` →
  `srgb_decode(out)`。但 LUT 输入 `rgb` **本来就是 sRGB 编码值**，`_display_ops` 的
  docstring 也写着「v: display-encoded RGB」。实测中间调：

  | look | 现在（encode→ops→decode） | 直接按 display 空间算 |
  |---|---|---|
  | classic_neg | 125 | 126 |
  | nostalgic_neg | 138 | 134 |
  | **bleach_bypass** | **161** | **130** |

  即 `contrast=0.30` 的漂白效果实际出来是「发灰发亮」，不是高反差。
- 影响：**已发布的 `look_bleach_bypass.cube` 就是这个版本**（灰阶中点 162）。
- 建议：去掉 encode/decode 这对（它只对恒等操作才抵消），或改成先 `srgb_decode`
  做线性光操作再 `srgb_encode`。

### C3. `build_looks.py` / `fit_all.py` 不可复现

- 位置：`build_looks.py:2,6,7,33`、`fit_all.py:2,6,8-10,14-16,50`
- 现象：硬编码 `/tmp/CyberCam-lut/src`、`/var/minis/shared/cardputer-zero/cybercam/luts/`、
  `/tmp/fujiprofiles/provia conversion luts/`、`/tmp/color2.jpg`、`/tmp/look_params.json`；
  拟合参数文件未提交，参考 LUT 已从仓库删除。在仓库里直接跑必然失败。
- 建议：把路径改成相对仓库的默认值 + 命令行参数，把 `look_params.json` 提交进仓库
  （它是本项目自己的拟合结果，不含第三方数据）。

### C4. `fit_all.py` 仍以 abpy 参考 LUT 作为拟合目标（授权灰区）

- 位置：`fit_all.py:7-11`
- 现象：拟合目标仍是 `/tmp/fujiprofiles/.../Provia to Classic Neg sRGB.cube` 等
  abpy 文件。虽然最终产物是「9 参数模型 + MIT Provia」的独立实现，但参数是朝对方输出
  拟合出来的。`NOTES.md:256` 的说法是「独立实现 + 参数拟合，观感不受版权保护」。
- 建议：这是法律判断，不是代码判断。建议在 `luts/README.md` 里把「拟合目标来自何处、
  为什么认为可再分发」写清楚，并保留 `fit_all.py` 作为过程证据；如果要更保守，
  可以改用自己拍的参考图 + 主观调参，彻底不碰对方文件。

### C5. `lutcompose.py` 已过时

- 位置：`lutcompose.py`
- 现象：它原来用来合成 abpy 的转换 LUT；现在三支 look 改由 `make_looks.py` /
  `build_looks.py` 生成，`lutcompose.py` 不再参与出包，且**没有任何文档再引用它**
  （grep `lutcompose` 在 README/NOTES/luts/README 中 0 命中）。
- 建议：删除，或移到 `tools/` 并在 NOTES 里注明「历史工具，不再使用」。

### C6. `make_looks.py` 与 `build_looks.py` 并存，文档两处说法冲突

- 位置：`NOTES.md:235`（说 make_looks）、`NOTES.md:251`（说 fit_all/build_looks）、
  `README.md:11`（说 build_looks + fit_all）
- 现象：同一件事在三处有不同说法，且实际产物是 make_looks 的。
- 建议：确定唯一生成路径后，把另一个脚本删掉或标注为 deprecated。

### C7. `build_looks.py` 把 33³ 网格量化成 uint8 再喂 Provia

- 位置：`build_looks.py:29-30`
- 现象：`(grid*255).round().astype(np.uint8)` 后再 `filter(provia)`，输入格点从
  `linspace(0,1,33)` 变成 `round(x*255)/255`，有 ±0.5/255 的偏差。
- 影响：很小（LUT 输出本来就是 8-bit），但既然要重做，顺手用浮点查表更干净。

---

## D. 文档 / 仓库漂移

| # | 位置 | 问题 |
|---|---|---|
| D1 | `NOTES.md:140-153` | 一·八 描述的 lores 流已不存在（`_record_config` 只有 main 一条流）；「MJPEG 25.2 fps」与 README 的「9.7 fps」矛盾，后者才符合当前代码 |
| D2 | `NOTES.md:75-76` | 一·二 说「录像/延时仍走 rpicam-vid/rpicam-still」——录像已改成 Picamera2 MJPEG，只有延时还用 rpicam-still |
| D3 | `NOTES.md:66` vs `NOTES.md:209` | 首帧 0.39 s vs selftest 的 3.17 s，两处矛盾 |
| D4 | `NOTES.md` 末尾 | 「⚠️ 未上真机」与前面多处「2026-11-22 真机验证」冲突，是没删的旧尾巴 |
| D5 | 原 `README.md`（现已被覆盖） | `python3 packaging/make_package.py` 指向的 `packaging/` 目录在本仓库不存在 |
| D6 | `cybercam.py:2716` | `command_diagnose` 不检查 `picamera2`——现在最关键的依赖反而没诊断 |
| D7 | `install.sh:22` | 只装 `python3-qrcode`，不装也不查 `python3-picamera2` / `python3-numpy` / `ffmpeg` |
| D8 | 仓库根 | `shutter.wav` 不在仓库里（`click()` 会静默没声音，靠设备上 deb 自带的那份） |

---

## E. 小问题

| # | 位置 | 问题 |
|---|---|---|
| E1 | `cybercam.py:1413-1420` | `WifiShare.stop()` 从未被调用（死代码），且只 `shutdown()` 没 `server_close()`，真调用会漏监听 socket |
| E2 | `cybercam.py:872-878`、`2651` | `ActiveRecording.proc` 永远是 `None`，所以「录像进程挂了」的检测是死代码 |
| E3 | `cybercam.py:523-544`、`881-937` | 模块级 `capture_still`/`take_photo`/`Recording` 只服务 CLI，和 UI 的 Picamera2 是两套路径（CLI 的 `photo` 也不吃 `--lut-full`、不应用变焦） |
| E4 | `cybercam.py:16,21,38,1992,2273` | 未使用：`tempfile`、`array`、`deque` 导入；`LookPicker.render` 的 `luts`；`export_stopmotion` 的 `path` |
| E5 | `cybercam.py:141-143` | `save_settings` 非原子写（`write_text` 直接覆盖），掉电可能写坏配置 |
| E6 | `cybercam.py:1276` | 网页相册 `_index` 只列最新 150 个文件，没有分页 |
| E7 | `cybercam.py:1320` | `Content-Disposition` 没过滤换行（本地文件名注入，低危） |
| E8 | `cybercam.py:2202-2204` | 连拍两张时第二张的胶片副本会被静默跳过（`FILM BUSY` 一闪就被覆盖） |
| E9 | `cybercam.py:1071-1072`、`1114-1116` | `Timelapse/lapse_*/` 和 `StopMotion/frames_*/` 的源帧永久累积，没有清理入口 |
| E10 | `selftest.py:14,38` | 不清理测试文件；`grab()` 返回 `None` 时 `np.asarray(None)` 会炸 |
| E11 | `cybercam.py:271,301` | `add_halation`/`add_grain` 里的 `convert("RGB")` 会复制整幅图，NOTES 说的「原地处理」不完全成立（8MP 峰值多一份 ~24 MB） |
| E12 | `cybercam.py:738` | `_crop_file` 对变焦照片重新 JPEG 编码（q93），有二次压缩损失 |

---

## 已验证项（本机实测）

| 项 | 结果 |
|---|---|
| 18 支 LUT 解析 | 全部通过，尺寸 13³/32³/33³ 正确，灰阶单调、无异常裁切 |
| 16 内置滤镜 + 18 LUT × preview/still/animated | 全部出图，尺寸正确（320×170 / 原尺寸 / 240×176） |
| `filtered_still`（LUT / 内置 / 创意滤镜） | 全部出片，文件名与后缀正确 |
| `crop_rect()` 四档 | 与 NOTES 表格逐位一致：1x (0,360,3280,1742)、1.5x (546,652,2186,1160)、2x (820,796,1640,870)、3x (1094,942,1092,580) |
| `safe_web_path` | 正常/穿越/绝对路径/符号链接/错误扩展名/缺失文件 6 个用例全部符合预期 |
| HTTP 相册 | 无 token 403、错 token 403、正确 token 303+HttpOnly cookie、Range 206/416、多段 Range 416、下载头、404 全部正确 |
| `encode_mp4` | 顺序帧快速路径、任意文件名 + LUT 的 rawvideo 管道两条路径都出片 |
| 相册 | 两段删除、GIF 播放、`VideoPlayback` 正常与坏文件 |
| `ruff` | 无未定义名（F821），只有未使用导入/变量等风格问题 |
| `make_looks.py` 可复现性 | 重新生成的三支 look 与仓库文件逐值一致（仅 CRLF/LF 差异） |

## 未验证项（需真机）

- Picamera2 实际选择的 sensor mode（B4）、内存峰值、预览/录像帧率
- framebuffer 实际 `line_length`（B7 目前靠 640 < 65536 侥幸正确）
- evdev 键位映射（`W`/`L`/`DEL`）
- 8MP 胶片副本的实际耗时与内存
- `selftest.py` 全流程

---

## 建议修复顺序

1. **A1** 恢复根 README（`git show 7ceee4b:README.md > README.md`）
2. **B1 / B2 / B3**（设置接上、错误可见、主循环兜底）——用户能直接感知
3. **B4**（sensor mode 加锁）+ 在 `selftest.py` 里打印实际 sensor mode
4. **C1 / C2 / C6**（确定唯一 LUT 生成路径，重出三支 look，修双重编码）
5. **B6 / B7 / B5**（健壮性）
6. **D1-D8**（文档一次性对齐）
7. **E1-E12**（清理）

---

## 附录：复现命令

```bash
# 静态检查
python -m py_compile cybercam.py selftest.py lutcompose.py make_looks.py
python -m ruff check --select F,E9,B cybercam.py

# 本机离线验证（Windows 需先给 fcntl 打桩）
python - <<'PY'
import sys, types
sys.modules['fcntl'] = types.SimpleNamespace(ioctl=lambda *a, **k: 0)
import importlib.util
spec = importlib.util.spec_from_file_location("cc", "cybercam.py")
C = importlib.util.module_from_spec(spec); spec.loader.exec_module(C)
from pathlib import Path
C.LUT_DIR = Path("luts")
for p in sorted(Path("luts").glob("*.cube")):
    print(p.name, C.load_cube_lut(str(p)).size)
PY

# 真机
python3 selftest.py
```
