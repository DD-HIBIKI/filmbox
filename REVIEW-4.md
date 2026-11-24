# FilmBox 0.8.6 代码复核与修复方案（REVIEW-4）

- 复核日期：2026-11-24
- 基线提交：`d67d4b4`（`FilmBox 0.8.6`，`filmbox.py` 3945 行）
- 硬件目标：M5Stack Cardputer Zero（Raspberry Pi CM0 / RP3A0，512 MB RAM，VideoCore IV，IMX219 8MP，320×170 RGB565 屏）

> **关于树莓派与 Debian 打包的说明**：树莓派官方系统 **Raspberry Pi OS** 底层正是基于 **Debian Linux**（使用 `apt` / `dpkg` 管理 `.deb` 软件包）。仓库内的 `packaging/make_package.py` 通过 `dpkg-deb` 构建 `.deb` 安装包并注册到 Cardputer Zero 的 `APPLaunch` 桌面启动器，因此修复打包脚本与 `install.sh` 对设备安装与分发直接生效。

---

## 问题清单与修复方案

### P0：阻断 / 崩溃 / 并发竞争与 OOM

| 编号 | 位置 | 问题描述 | 修复方案 |
|---|---|---|---|
| **P0-1** | `packaging/make_package.py` | ① 引用不存在的 `src/filmbox.py` 与 `packaging/icon.png` 直接崩溃；② 未把 `luts/*.cube` 打入包内且 `filmbox.py` 无系统 LUT 目录回退，装包后 0 支 LUT；③ `Depends` 缺少 `python3-picamera2`、`rpicam-apps`、`python3-qrcode`。 | 修正源码与图标路径（`filmbox.py`、`icon-100.png`）；将 `luts/*.cube` 打包至 `/usr/share/filmbox/luts`，并在 `filmbox.py` 中支持系统目录回退/自动同步；补齐包依赖声明。 |
| **P0-2** | `install.sh` / `selftest.py` | 重命名为 `filmbox.py` 后，脚本仍指向已删除的 `cybercam.py` 与旧目录 `/usr/lib/cybercam`、`~/.config/cybercam/luts`；`set -e` 下首次备份不存在目标直接报错退出。 | 更新为安装 `/usr/lib/filmbox/filmbox.py`、`/usr/bin/filmbox`、`/usr/share/filmbox/luts` 及 `~/.config/filmbox/luts`（同时兼容旧路径软链/覆盖）；`selftest.py` 优先加载仓库本地或 `/usr/lib/filmbox/filmbox.py`。 |
| **P0-3** | `filmbox.py` (`film_job` / `key` / `run` / `filtered_still`) | ① 8MP LUT 渲染期间 `render()` 停掉 `stream`（`picam=None`），但 `do_photo()` 已结束使 `self.busy=False`；`key()` 未拦截 `self.film_busy`，按拍照抛 `AttributeError` 或并发抢相机、按 `9`/延时摄影导致 `render` 结束时在后台误启相机抢硬件、按 `ESC` 直接退出留下写半截的坏片；② 内置滤镜 `quick()` 无并发互斥，连拍多张 8MP 同时解码易 OOM；③ `filtered_still()` 直接写最终路径而非原子写；④ `film_progress` 放在 `last_draw < 1.0` 节流门之后，进度条卡成 1 秒一跳且白算取景滤镜。 | ① `key()`、`enter_gallery()` 在 `film_busy` 时妥善拦截（仅允许帮助/安全查看，阻止快门/退出/切相册），`render()` 仅在仍处于取景态时恢复相机，`CameraStream.start/stop` 加 `camlock` 保护；② 内置滤镜与 LUT 统一走单任务串行保护 + `.tmp` 原子落盘（`os.replace`）；③ `film_progress` 提到主循环前部独立按需刷新。 |
| **P0-4** | `filmbox.py` (`stop_recording`) | `raw.read_bytes().count(b"\xff\xd8")` 将整段 `.mjpeg` 一次性读入内存；长视频（如 2 分钟 1080P 约 180 MB）在剩余 65–70 MB 内存的机器上直接抛未捕获的 `MemoryError` 或触发 OOM，导致重封装中断。 | 改为 64 KB 流式分块计数（处理跨块边界的 `\xff` 字节拼接），内存占用恒定 64 KB，并捕获异常安全回退。 |

---

### P1：`GpuFilm`（VideoCore IV EGL/GLES2）底层根因修复

| 编号 | 位置 | 根因分析 | 修复方案 |
|---|---|---|---|
| **P1-1** | `filmbox.py` / `tools/accel_build.py` / `tools/gpu_*.py` | `GL_RGB` 被写成 `0x1908`（实际 `0x1908` 是 4 字节 `GL_RGBA`，**`GL_RGB` 应为 `0x1907`**）。每次上传 3 字节 RGB 缓冲时驱动按 4 字节读取，越界多读 33% 堆内存触发 `vc4` SIGSEGV，且通道全部错位。 | 修正 `GL_RGB = 0x1907`，`GL_RGBA = 0x1908`。 |
| **P1-2** | `filmbox.py` (`GpuFilm`) | `eglMakeCurrent` 只绑定初始化线程，而每拍一张照片都新建 `Thread`，第二张起新线程无 GL 上下文。 | 每次 `apply` / `apply_band` 入口调用 `_ensure_current()` 绑定当前线程上下文，结束时解绑（配合 `_ACCEL_LOCK` 串行化访问）。 |
| **P1-3** | `filmbox.py` / `tools/accel_build.py` | 同一纹理 `self.tex` 同时绑在 `GL_TEXTURE0`（采样输入）和 `self.fb`（渲染目标），违反 GLES2 规范产生读写反馈环（Feedback Loop）。 | 拆分为独立的输入纹理 `self.src_tex`（`GL_RGB`）与 FBO 颜色附件纹理 `self.dst_tex`（`GL_RGBA`）。 |
| **P1-4** | `filmbox.py` / `tools/accel_build.py` | ① `glReadPixels` 第 0 行已对应上传图像顶行，额外 `FLIP_TOP_BOTTOM` 导致每个 `512×512` 瓦片单独上下颠倒；② 边缘不足 `tile` 的块使用 `glViewport(0,0,tile,tile)` 配 `uvScale` 会把画面拉伸到 512 再裁切。 | 去掉多余的上下翻转；边缘块使用 `glViewport(0, 0, cols, rows)` 与 `glReadPixels(0, 0, cols, rows, ...)` 实现 1:1 像素映射。 |
| **P1-5** | `filmbox.py` / `tools/accel_build.py` | ① 片元着色器中 `col` 为 `0..1`，`grainAmp=5.0` 未除以 `255.0` 直接相加导致噪点放大约 106 倍变成雪花；② VC4 片元浮点精度下 `sin()*43758.5453` 丢失小数位导致 `fract()` 退化。 | 将颗粒幅度换算到归一化与 CPU 一致的 `±(1.2 * grain / 255.0)` 量级，并用对低精度友好的先取模坐标哈希替代大常数 `sin` 哈希。 |

---

### P2：相机画质、色彩科学与功能边界

| 编号 | 位置 | 问题描述 | 修复方案 |
|---|---|---|---|
| **P2-1** | `filmbox.py` (`_capture_once` / `_crop_file` / `capture_still`) | ① 选 `1080P` (`1920×1080`) 时按 4:3 最小缩放比拍出 `1436×1080`；② `2MP`/`VGA` 配合变焦时先缩图再二次裁切（如 `VGA` 3x 只剩 `212×160`）；③ `ENOMEM` 重试 4 次从未真正下调传感器模式与目标分辨率。 | 保持“全幅采集 + 软件确定性裁切”（避开 IMX219 切模式后 `ScalerCrop` 延迟失效的硬件坑），但计算采集尺寸时乘上变焦倍率并覆盖目标宽高比，在 `_crop_file` 一步完成比例（16:9 / 4:3）与变焦裁切（不放大超过传感器像素）；`ENOMEM` 第 3 次重试起真正降级到 `1640×1232` 传感器模式确保出片。 |
| **P2-2** | `filmbox.py` (`add_halation` / `build_glow` / `film_copy_raw` / `GpuFilm`) | ① 纯黑白胶片 LUT（`fuji_acros`、`fuji_acros_red`、`ilford_hp5_400`）套用后被无条件叠加暖橙色 `(1.0, 0.72, 0.42)` 光晕，导致黑白片高光发橙；② `film_copy_raw` 在套 LUT 前的原图缩略图上算光晕。 | 自动检测单色 LUT / 灰阶画面，黑白胶片改用中性银盐白光晕 `(1.0, 1.0, 1.0)`；`film_copy_raw` 先对 `thumb` 套用 LUT 再生成 `glow`。 |
| **P2-3** | `filmbox.py` (`_migrate_legacy_paths`) | 模块导入阶段同步 `copytree` 整个旧照片目录，照片多时启动黑屏卡死且占用双倍 SD 卡容量，满盘中断则留下残缺目录。 | 优先使用硬链接 `copy_function=os.link`（零额外空间、毫秒级完成，失败自动回退 `copy2`），先写临时目录再原子改名。 |
| **P2-4** | `filmbox.py` (`WifiShare` / `GalleryHTTP` / `draw_share_overlay`) | ① 未连 Wi-Fi 时按 `W` 会把 `127.0.0.1` 永久缓存在 `self.url`；重试成功后不清除旧 `self.error`；退出程序未 `stop()` 释放端口；② 缺 `python3-qrcode` 时截断了 `?key=` 导致手输也无法登录；③ `GalleryHTTP` 缺少 `do_HEAD` 返回 501。 | 每次打开分享动态刷新 `lan_ip()` 并提示离线状态，成功时清 `self.error`，退出时 `stop()` + `server_close()`；无二维码时完整展示带 token 的访问地址；补齐 `do_HEAD` 与文件名安全过滤。 |

---

### P3：性能、内存与交互细节

| 编号 | 位置 | 问题描述 | 修复方案 |
|---|---|---|---|
| **P3-1** | `filmbox.py` (`read_cube` / `_LUT_CACHE`) | `read_cube` 与 `_CUBE_CACHE` 重复定义两遍；`_LUT_CACHE` 无上限，浏览全部 LUT 后常驻约 8–10 MB。 | 删除重复定义；对 `_LUT_CACHE` 与 `_CUBE_CACHE` 做容量上限淘汰（保留最近 4 支）。 |
| **P3-2** | `filmbox.py` (`LookPicker` / `available_luts`) | ① `open_picker()` 先补黑边再套滤镜再裁切，且运行时新拷入的 `.cube` 不刷新；② `label()` 未剥离 `look_` 前缀，显示 `LUT look classic neg`。 | 动态扫描用户与系统 LUT 目录；预览图直接按预览窗比例 `fill_size` 裁好再套滤镜（省约 40% 像素开销且无黑边干扰）；剥离 `look_` 前缀。 |
| **P3-3** | `filmbox.py` (相册删除 / 定格动画 / 键盘 / CLI / 重绘状态) | ① 相册删除延时/定格成片后遗留 `lapse_*/`、`frames_*/` 源帧目录；② 定格动画中途换滤镜未同步 `session.look`；③ `get_keyboards()` 泄漏多余设备 fd；④ CLI `photo` 忽略 `--lut-full`；⑤ 主循环 `state` 缺少 `clean_ui`/`zoom`/定格帧数。 | 删除成片时若无同名其他成片存在则同步清理对应源帧目录；定格抓帧/导出同步当前滤镜；关闭未选中的 `InputDevice`；CLI `photo` 透传 `--lut-full` 并修正帮助说明；补全 `state` 字段。 |
