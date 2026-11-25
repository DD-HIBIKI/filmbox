# FilmBox (CyberCam) 开发红线与防踩坑指南（Minis 必读）

在修改本目录代码、从远端（如 `/var/minis/...` 或 Cardputer Zero 真机）同步文件或打包发版前，**必须逐条对照本文件**，避免重复踩已经修过的坑。

---

## 一、同步、目录结构与打包红线（高频翻车点）

1. **严禁直接用远端构建目录覆盖仓库文件而不看 `git diff`**
   - 本仓库布局是：**根目录直接放 `filmbox.py`、`icon-100.png`、`shutter.wav`、`luts/*.cube`**（没有 `src/` 子目录，也没有 `packaging/icon.png`）。
   - 如果你在远端构建机用了 `src/filmbox.py` 或 `packaging/icon.png` 的目录结构，**绝对不要**把远端简版 `make_package.py` 直接拷回仓库覆盖！仓库里的 `packaging/make_package.py` 已经兼容了双路径回退、内置打包 18 支 `.cube` 到 `/usr/share/filmbox/luts`，并声明了完整依赖（`python3-picamera2`、`python3-qrcode`、`rpicam-apps`）。
2. **严禁把带绝对路径前缀的压缩包直接解压进仓库**
   - 之前出现过把 `/var/minis/shared/cardputer-zero/cybercam/...` 整棵目录树解压并提交进仓库（+4197 行重复文件）。同步单文件请明确指定目标路径，提交前必须跑 `git status` 和 `git diff --stat` 检查文件列表。
3. **严禁“先在一个 commit 删旧文件、下个 commit 再加新文件”**
   - 重命名或替换文件必须在同一个工作树内完成后一次提交，保证任意 commit 都是完整可运行的。
4. **改版本号或改文件名必须四处同步**
   - 升版本号时同步更新：① `filmbox.py` 顶部 docstring 与 `VERSION = "..."`；② `packaging/make_package.py` 的 `Version:` 与输出 `.deb` 文件名；③ `README.md` 中的版本号示例。
   - 任何路径/入口变动必须同步检查 `install.sh` 和 `selftest.py`（注意 `install.sh` 必须保持 **LF** 换行，不能带 `\r\n`）。

---

## 二、VideoCore IV (`vc4`) OpenGL ES 2.0 / EGL 铁律

Cardputer Zero（CM0 / RP3A0）上的 Mesa `vc4` 驱动限制极严，改 `GpuFilm` 或 `tools/gpu_*.py` 时严守以下 6 条：

1. **`GL_RGB` 是 `0x1907`，`GL_RGBA` 是 `0x1908`，绝不能混成同一个值！**
   - 曾把 `GL_RGB` 写成 `0x1908`，导致传入 3 字节 RGB buffer 时驱动按 4 字节读取，越界多读 33% 堆内存直接触发 `vc4` **SIGSEGV** 且颜色错位。所有 3 字节 RGB 上传前必须设 `glPixelStorei(0x0CF5, 1)`（`GL_UNPACK_ALIGNMENT = 1`）。
2. **EGL 上下文只能在进程主线程创建（`GpuFilm()`），UI 侧通过独立后台子进程 `_gpu-worker` 异步预热**
   - 在 `vc4` 驱动上，从 `threading.Thread` 子线程首次调用 `eglCreateContext` 会在 C 层直接崩溃（Python `try...except` 拦不住）。
   - UI 模式使用独立后台子进程（`GpuWorker` → `filmbox.py _gpu-worker`）：子进程在**自身进程主线程**初始化 `GpuFilm()`，从而既不阻塞相机开屏进取景器，又严格满足 `vc4` 主线程约束，同时隔离任何 C 层驱动崩溃（子进程挂掉后主相机自动回退 CPU 渲染）。
   - 进程内路径（CLI `photo`、`tools/bench_accel.py`）初始化后立即 `eglMakeCurrent(dpy, 0, 0, 0)` 释放绑定，渲染时在 `_ACCEL_LOCK` 内成对执行 `_make_current()` 与 `finally: _release_current()`；`_gpu_ready()` 中当 `_GPU_STATE == 0` 且不是 `threading.main_thread()` 时，**必须直接返回 `False` 回退 CPU**，绝不在子线程懒加载 `GpuFilm()`。
3. **禁止读写同一纹理（GLES2 Feedback Loop）**
   - 绝不能把同一张纹理同时绑在 `GL_TEXTURE0`（片元着色器采样输入）和 `self.fb` 的 `GL_COLOR_ATTACHMENT0`（渲染输出）上。必须保持 `self.src_tex`（`GL_RGB`）与 `self.dst_tex`（`GL_RGBA`）分离。
4. **禁止对已分配纹理 ID 直接用新尺寸调 `glTexImage2D`**
   - 在 `13³`（`169×13`）与 `33³`（`1089×33`）LUT 间切换，或在 8MP / 2MP / 变焦间切换导致 `glow` 尺寸改变时，直接对旧 texture ID 调不同尺寸的 `glTexImage2D` 会泄漏 CMA BO 甚至崩驱动。
   - **规则**：尺寸不变一律走 `glTexSubImage2D`；尺寸变化先 `glDeleteTextures` 再 `glGenTextures`。
5. **禁止用 `id(pil_image)` 作为跨调用缓存键**
   - 临时 `glow` 图像在函数返回释放后，下一张同尺寸照片新建的 `PIL.Image` 极易复用同一内存地址（ABA 问题），导致跳过上传、把上一张照片的高光鬼影印到下一张图上。必须用 `weakref.ref` 并在 `apply()` 退出时清空。
6. **瓦片坐标、颗粒归一化与 VC4 着色器精度**
   - PIL 顶行上传到纹理 `v=0`，配合 `vUv = pos * 0.5 + 0.5`，`glReadPixels` 读回的第 0 行**本来就是图像顶行**——**不要**再做 `FLIP_TOP_BOTTOM`，边缘瓦片直接用 `glViewport(0, 0, cols, rows)` 1:1 渲染，不要拉伸到 `512×512` 再裁切。
   - GLSL 中 `col` 范围是 `0.0..1.0`，灰阶颗粒强度必须除以 `255.0`（否则噪点放大百倍变成黑白雪花）。
   - VC4 片元浮点精度有限，绝不能写 `sin(...) * 43758.5453`（大数没有小数位，`fract()` 会退化成 0 或条纹），哈希前必须先对像素坐标 `mod(floor(p), 251.0)`。

---

## 三、相机层（Picamera2 / IMX219）与 512 MB 内存红线

1. **拍照变焦与比例裁切不要依赖 `switch_mode` 后的 `ScalerCrop`**
   - IMX219 切到单缓冲 `still_config` 后立即发 `ScalerCrop` 会因管线延迟出现“不裁切或双重裁切”。拍照固定下发全画幅 `(0, 0, 3280, 2464)`，统一在 `_crop_file()` 做确定性的软件裁切（同时处理 `1080P` 16:9 比例与数码变焦）。
2. **预览 `ScalerCrop` 一律使用全幅 `3280×2464` 坐标系**
   - 即使传感器跑在 `1640×1232` 模式，libcamera 的 `ScalerCrop` 坐标基准依然是 `SENSOR_SIZE (3280, 2464)`，且必须按当前输出流宽高比（取景 `320/170`、录像按视频比例）计算，否则画面会被拉伸。
3. **预览流不要加 `FrameDurationLimits`**
   - `(1000, 1e6/fps)` 只限制最长帧时长（把暗光 AE 最长曝光锁死在 ~40 ms），并不会限制最高帧率；预览控帧统一在 `_loop` 做 BGR→RGB 转换**之前**按时间间隔节流。
4. **严控 512 MB（可用 ~352 MB + CMA）内存峰值与并发状态**
   - **长文件禁全量读内存**：统计 `.mjpeg` 帧数必须按 64 KB 分块扫描（`_count_mjpeg_frames`），严禁 `raw.read_bytes()`（录 2 分钟视频就超过 150 MB 必触发 OOM）。
   - **渲染期锁操作**：8MP 胶片渲染会停预览流（`self.stream.stop()`），期间 `self.film_progress is not None` 时必须在 `key()` 拦截快门、进相册和退出键；`render()` 结束的 `finally` 只有在 `self.running and self.gallery is None and self.timelapse is None` 时才能重启预览流。
   - **成片原子落盘**：所有后台生成的 JPG/PNG 必须走 `_atomic_save_image`（先写隐藏 `.tmp` 再 `os.replace`），防止中断留下半截坏图或被 Wi-Fi 相册读到残片。
   - **冷启动先亮屏且不阻塞等 GPU**：`run()` 里必须先打开 `Framebuffer` 画一帧 `"STARTING CAMERA"`（<50 ms 响应），随后启动非阻塞后台 `GpuWorker` 子进程并立即 `self.stream.start()` 进入实时取景，禁止在 `run()` 主线程同步等待 2–3 秒 GPU 初始化。若用户首张秒拍时 GPU 仍在加载，通过 `FILM COPY` 界面的 `note` 提示 `"LOADING GPU... (FIRST SHOT)"`。

---

## 四、色彩与胶片模拟细节

- **黑白胶片严禁叠暖橙色光晕**：`fuji_acros`、`fuji_acros_red`、`ilford_hp5_400` 是纯单色黑白 LUT（RGB 通道差为 0）。`build_glow()` / `add_halation()` 对单色 LUT 必须使用中性银盐白光晕 `(mask, mask, mask)`，只有彩色胶片才用暖橙红光晕 `(1.0, 0.72, 0.42)`。
- **光晕必须在套用 LUT 之后计算**：无论全图、GPU 缩略图还是 `film_copy_raw` 流式路径，都必须先对 1/4 缩略图过一遍 LUT 再调 `build_glow()`。

---

## 五、每次提交前自检清单（5 条命令）

```bash
# 1. 确认没有误带入外部路径、临时文件或意外回退别人的修复
git status
git diff --stat

# 2. 语法检查与 install.sh LF 换行检查
python3 -m py_compile filmbox.py packaging/make_package.py selftest.py tools/accel_build.py
python3 -c "assert b'\r' not in open('install.sh','rb').read()"

# 3. 验证打包脚本在仓库根目录能正常跑通
python3 packaging/make_package.py

# 4. 在真机上验证相机全链路与 GPU/CPU 一致性
python3 selftest.py
FILMBOX_ACCEL=gpu python3 tools/bench_accel.py
```
