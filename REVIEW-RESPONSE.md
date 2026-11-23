# REVIEW 处理结果（对 `REVIEW.md` 的逐条回应）

- 回应日期：2026-11-22
- 处理基线：`REVIEW.md`（dsh 审计，281 行，基线 `c8f1cad`）
- 本轮提交：代码修复 + 文档对齐（见文末 commit）

## 已修（本轮）

| 编号 | 问题 | 处理 |
|---|---|---|
| **A1** | 根 `README.md` 被 LUT 包说明覆盖 | 用完整项目说明恢复（103 行）；LUT 授权表只留在 `luts/README.md`，根 README 一行指过去 |
| **B1** | `preview_fps` 存了不用 | `CameraStream._loop` 按 `1/fps × 0.85` 节流发布；实测设 12 → 13.0 fps |
| **B2** | 相机错误静默（只写 `self.error`） | 主循环检测变化 → 写 `camera.log` + 状态栏 `CAM ERR …`（去重，不刷屏） |
| **B3** | 主循环无异常保护 | 每个按键事件包 `try/except`，记日志 + 状态栏，不再杀进程 |
| **B4** | `_still_config` / `_record_config` 没锁 sensor mode | 两处都补 `sensor={"output_size": …}`（still 用 3280×2464，录像用 1640×1232） |
| **B5** | CLI `video --video-quality 720p` 横向拉伸 | CLI 改走 `CameraStream.start_recording`；老的 `Recording` 类（rpicam-vid + libav）删除。实测 `ffprobe` 得 1280×720 / mjpeg，sensor mode 1640×1232 |
| **B6** | `zoom` 坏值导致 app 起不来 | `load_settings` 归一化到 `ZOOMS`，非法值回落 1.0 |
| **B7** | `FIX_FMT` 的 `parsed[8]` 不是 `line_length` | 改读 `/sys/class/graphics/fb0/stride`（实测 640），失败回退 `w*2` |
| **C1** | 仓库里 `look_*.cube` 是旧版（`make_looks` 产物） | 换成 `build_looks.py` 产物；`look_params.json` 已提交 |
| **C2** | `make_looks.py` 双重 sRGB 编码（BB 中点 161 vs 130） | 该脚本**已删除**，唯一路径是 `build_looks.py`（在显示输入上直接做参数层，无二次编码） |
| **C3** | 生成器硬编码 `/tmp`、参数文件未提交 | `build_looks.py` / `fit_all.py` 改成 argparse + 仓库相对路径；参数已入库；`build_looks.py` 可离线复现（新产物与原产物平均差 0.24/255） |
| **C5** | `lutcompose.py` 过时且无引用 | 移到 `tools/lutcompose.py`，文件头标注「历史工具，不参与出包」 |
| **C6** | 两个生成器并存、文档说法冲突 | 删 `make_looks.py`；README/NOTES 统一为 `fit_all.py` → `build_looks.py` |
| **C7** | `build_looks.py` 把网格量化成 uint8 再喂 Provia | 改成浮点三线性查表 |
| **D1** | 一·八 仍描述已删除的 lores 流；25.2 fps 与 9.7 fps 矛盾 | 改为「main 320×170 RGB888 / lores 已弃用」「录像时预览 9.7–10 fps」 |
| **D2** | 「录像/延时仍走 rpicam」 | 改为「只有延时还用 rpicam-still」 |
| **D3** | 首帧 0.39 s 与 3.17 s 矛盾 | 统一为「1.7–3.2 s（冷启动）」 |
| **D4** | 文末残留「未上真机」 | 改为「已在真机验证」并列实测项 |
| **D5** | README 安装命令指向不存在的 `packaging/` | 注明打包脚本在上游仓库，本仓库只放改造后的单文件 |
| **D6** | `command_diagnose` 不检查 picamera2 | 已加（缺失时提示 `sudo apt install python3-picamera2`） |
| **D7** | `install.sh` 只装 qrcode | 改成检查/安装 picamera2、numpy、PIL、evdev、ffmpeg、qrcode |
| **D8** | `shutter.wav` 不在仓库 | 从设备复制入库（7.7 KB） |
| **E5** | `save_settings` 非原子写 | 写 `.json.tmp` 后 `os.replace` |
| **E11** | `add_halation` / `add_grain` 的 `convert("RGB")` 多复制整幅 | 已是 RGB 时不再复制 |
| **E3**（部分） | 模块级 `Recording` 类与 CLI 双路径 | 类已删除；CLI 录像与 UI 共用 `CameraStream` |

## 未处理（附判断）

| 编号 | 项 | 说明 |
|---|---|---|
| **B4 残留** | 480P/VGA 预设的自由注释 | sensor mode 已锁全幅，代码层面不再 2× 裁切；预设里的 `WIDE` 说明保持原样（上游文案） |
| **C4** | `fit_all.py` 以第三方 LUT 为拟合目标 | 法律判断。`fit_all.py` 现在**需要显式传入 `--ref` 目录**，仓库内不含任何第三方 LUT；`luts/README.md` 写明了产物构成（MIT Provia + 自拟合参数）。若需更保守，可改用自拍样张 + 主观调参 |
| **E1** | `WifiShare.stop()` 从不调用 | 保留（关界面后服务需继续跑是设计），已补 `server_close()` 的坑记在这里；未改 |
| **E2** | `ActiveRecording.proc` 恒为 None | 死代码，可删；留着不影响 |
| **E4** | 未使用导入（`tempfile`/`array`/`deque` 等） | 未清，纯风格 |
| **E6** | 网页相册只列 150 个、无分页 | 未做 |
| **E7** | `Content-Disposition` 未过滤换行 | 低危；未做 |
| **E8** | 连拍第二张胶片副本被跳过 | 设计如此（`FILM BUSY`），可改为排队 |
| **E9** | 延时/定格的源帧永久累积 | 未做清理入口 |
| **E10** | `selftest.py` 不清理测试文件、`grab()` 可能返回 None | 未改 |
| **E12** | 变焦照片二次 JPEG 编码 | 未改（q93，损失可接受） |

## 真机复测（本轮改动后）

```
首帧 1.7–3.2 s   ·  预览 13.0 fps（设 12）/ 25.7 fps（设 25）
变焦四档 ScalerCrop 与下发值逐位一致，1x vs 2x 画面差 72
8MP 拍照 0.8 s / 1.7 MB     2x 拍照 1640×1232
720p 录像：启动 0.13 s，录像中预览 10.0 fps，mp4 3.4 MB
CLI 录像：1280×720 mjpeg mp4（不再拉伸）
```
