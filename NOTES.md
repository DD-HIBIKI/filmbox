# CyberCam 改造：胶片 LUT + 扫码导出

基于 **CyberCam v0.5.3**（`cybern0mads/CyberCam`，MIT）打的两个补丁，
基线文件是上游 `src/cybercam.py`（2031 行）。

| 文件 | 说明 |
|---|---|
| `cybercam.py` | 打完补丁的完整文件（直接替换设备上那份） |
| `cybercam-film-share.patch` | `patch -p1` 用的 diff（对上游 v0.5.3） |

## 一、胶片 LUT

**用法**：把 `.cube` 丢进 `~/.config/cybercam/luts/`，重开 app

- **`5` / `F` → 全屏 LOOKS 选择器**：16 个内置滤镜 + 所有 LUT 一列排开
  （右侧实时预览当前高亮项，`BASE` / `LUT` 标签区分）
  - `↑↓` 移动、`←→` 翻 8 项、`Enter` 选中、`Esc` 返回
  - **`L` 直接从任意界面跳到第一个 LUT**
- `←→`（主界面）：保留原来的快速循环手感
- 预览：只套 LUT（320×170 原生帧，Picamera2 main 流直出）
- **存图（异步）**：拍照只负责出 8MP 原片（实测 0.8–1.0 s），**取景立刻恢复**（暂停 <1 s）；
  LUT → halation → grain 在独立线程里跑，界面显示 `FILM 40%…90%`
  - LUT 按 256 行分块（内存峰值压下来，分块结果与整幅逐字节一致）
  - halation 的 glow 在 **1/4 分辨率**算好，再按条 screen 混合（8MP 也不吃满内存）
  - grain 本来就分条；三者都不再需要一份全尺寸中间图
  - **内存守卫**：`MemAvailable ≥ 110MB` 才出 8MP，否则自动退 2MP（`LUT_SAVE_MAX`）
  - 出片期间可以继续拍，但一次只跑一张（`film_busy`）
- 帧率：`preview_fps` 默认 **25**（可选 12/15/20/24/25/30；`--preview-fps`）。生效方式是 `_loop` 在
  **BGR→RGB 转换之前**按 `1/fps×0.85` 节流。**不设 `FrameDurationLimits`** —— 那会把暗光下 AE 的最长曝光锁死
- 预览分辨率：**320×170**（= 屏幕），从 main 流直出，不再做缩放
- 存图格式：LUT 模式存 **JPEG q93**（内置滤镜仍存 PNG）

**LUT 选型（关键）**：必须 **sRGB in/out**；精度 13³ 与 33³ 并存（G'MIC 那批是 13³，自建 look 是 33³）。设备出的是 sRGB JPEG，
喂 Adobe RGB / 场景线性的表会偏色。
- ✅ sRGB 即用包：`YahiaAngelo/Film-Luts`（MIT，296 个）
- ❌ `TomPoczos/PoLUT` 效果最"物理"，但它的 cube 是 **Adobe RGB + 替代 AgX（吃线性输入）**，
  直接喂 sRGB JPEG 不对，要用得先做色彩转换或在 darktable 里用

### 附带的富士胶片模拟包

`luts/` 目录已配好 18 支（无 tgz），**sRGB in/out**，直接拷到
`~/.config/cybercam/luts/` 就能在选择器里看到：

| 有 | 说明 |
|---|---|
| PROVIA / VELVIA / ASTIA / CLASSIC CHROME / PRO NEG STD / PRO NEG HI / ACROS / ACROS+R | G'MIC `fujixtransiii` 那批，13³ |
| **CLASSIC NEG / NOSTALGIC NEG / BLEACH BYPASS**（已移除，见「一·十三」） | ~~32³，合成：G'MIC Provia → abpy 转换 LUT~~ |

⚠️ 为什么不直接套 abpy 的转换 LUT：它要求输入**已经是 Provia 渲染**的图，
直接套实测发灰、对比度全丢；先过一遍 Provia 才对。
⚠️ abpy 仓库没有 LICENSE → 自己用可以，别分发。
富士官方的 `.cube` 是 **F-Log2 / BT.2020 视频用**，不能喂 sRGB 照片。

**耗时参考**（iSH x86 模拟，原生 A53 更快）：
LUT 224×170 10 ms ／ 2MP 存图全流程 2.3 s ／ 8MP 全流程 ~8 s
→ **建议 LUT 模式下用 `7 SIZE` 切到 2MP（1640×1232）**，画质差别很小、速度快 4 倍。

## 一·二、相机层换成 Picamera2 单会话（2026-11-22 真机验证）

原来的预览是 `rpicam-vid` 子进程、拍照是 `rpicam-still` 子进程，**必须停流才能拍**，
实测重启到第一帧要 **4.8s**，一次拍照冻结 ≈ 7s。现在改成一条 Picamera2 会话：

| 项 | 改前 | 改后（真机实测） |
|---|---|---|
| 预览帧率 | 21–25 | **25.7 fps**（默认 25，节流后实测） |
| 预览首帧 | 4.8 s | **1.7–3.2 s**（冷启动，视缓存） |
| 8MP 拍照 | 2.6 s + 重启 4.8 s | **0.86 s**（切配置 → capture_file → 切回） |
| 拍完回到取景 | ~7 s | **立即** |

配置：`video` = main 320×170 RGB888（buffer_count=3，sensor mode 锁 1640×1232），
`still` = 3280×2464 RGB888（buffer_count=1，8MP RGB 只能给 1 帧，2 帧就 ENOMEM）。

⚠️ 8MP RGB888 的 buffer **只能用 1 张**，实测 2 张直接 `Cannot allocate memory`。
⚠️ lores 流必须是 YUV420 且不得大于 main，实测颜色也对不上 → **已弃用，预览只走 main**。
⚠️ **只有延时摄影**还用 `rpicam-still` 子进程，所以 `do_timelapse` 里会先 `stream.stop()`，结束后 `stream.start()`；
录像已经改成同会话 MJPEG（见一·八）。

## 一·三、8MP 出片：从 12.6s 压到 7.6s

| 阶段 | 改前 | 改后 |
|---|---|---|
| LUT（分块） | 1.2 s | 1.2 s |
| halation | 2.6 s | **1.2 s**（glow 1/4 分辨率 + 原地混合 + NEAREST） |
| grain | 5.8 s | **2.8 s**（整数运算 + 权重按条上采样，计数器/浮点都用 int16） |
| 存 JPEG | 0.5 s | 0.35 s（原地处理省掉一次 24MB 拷贝） |

峰值内存：133MB → 75MB（原地处理 + 先释源图）。
出片线程 `os.nice(10)`，不抢取景。
⚠️ **内存判断必须在 `Image.open` 之前做** —— 8MP RGB 一解码就吃掉 24MB，
之后再判断可用内存会从 128MB 掉到 69MB，导致永远退回 2MP（踩过）。

## 一·四、两个真机 bug（2026-11-22 修）

**① 保存报错 `ENOMEM`（'NoneType' object has no attribute…）**
- 切 8MP 配置要申请 **24MB DMA buffer**，此时预览 buffer 还占着 heap → `dma_heap.alloc` 直接 Errno 12
- 修法：`stop() → switch_mode(still) → start() → capture_file() → stop() → switch_mode(video) → start()`
- 🔴 **残留进程会永久占着相机 buffer**：app 被 `kill`/崩溃后 kernel 里还挂着 `picamera2-N`，
  之后必然 ENOMEM。已加 SIGTERM/SIGINT 处理（`self.running=False` → `run()` 的 finally 里 `stream.stop()`）。
  排查方法：`cat /sys/kernel/debug/dma_buf/bufinfo | grep picamera2`

**③ R/B 通道反转（橙色变蓝，2026-11-22 二次修复）**
- 🔴 **libcamera 的 `RGB888` 在内存里是 BGR 顺序**，`Image.fromarray(pixels)` 直接当 RGB 用 → 橙变蓝
- 修法：`Image.fromarray(np.ascontiguousarray(pixels[:, :, ::-1]))`

**④ 拍照报 NoneType（同一轮）**
- `do_photo` 里残留旧的 `self.stream.stop()`，新相机层停流后 `picam` 变成 None → `attribute 'stop'`
- 修法：删掉 do_photo 的 stop/start，`capture_still` 内部自己切配置

**② 颜色发紫发绿**
- 原因：把 Picamera2 的 **YUV420 当成 RGB 用**（`make_array("lores")` 给的是 NV12）
- 修法：预览流直接用 **main 320×170 RGB888**（ISP 直出，无转换），lores 流整个去掉
- 校验：同一场景分别用 Picamera2 与 `rpicam-still` 拍，平均 RGB 差 **7/255**（主要是两次 AWB 差异）

## 一·五、两种界面（`T` 键切换，存进 settings 的 `clean_ui`）

| | 标准 | 纯净 |
|---|---|---|
| 右栏宽度 | 99 px | **26 px**（数字居中） |
| 面板透明度 | 0.62 | **0.34** |
| 内容 | 标题/四项/9 GALLERY/6 CAPTURE/FPS | **只有数字** 1 2 3 4 9 6 + 帧率 |

## 一·六、视野（FOV）与等效焦距切换（2026-11-22）

**取景比照片窄的根因**：Picamera2 给 320×170 自动挑了 **640×480 传感器模式**，
那是 `(1000,752)/1280×960` 的**2 倍中心裁切**；照片走 3280×2464 全幅。
→ 修法：`create_video_configuration(..., sensor={"output_size": (1640,1232), "bit_depth": 10})`
校验：预览 vs 照片全幅 MAD 23 / 中心 50% MAD 47 → 现在对得上。

**变焦**：M5 官方只写「IMX219 8MP」，没有镜头参数（实际镜头可能比 24mm 更广），
所以不做绝对焦距标定，直接用倍率：**1x / 1.5x / 2x / 3x**（1x = 全幅）。

- `Z` 键循环，存 `settings.json` 的 `zoom`
- 预览：`set_controls({"ScalerCrop": rect})`
- 🔴 **照片不能靠 ScalerCrop**：实测切配置后控制不生效，会拍出全幅（第二次踩到「双重裁切/不裁切」）。
  现在的做法是**先拍全幅，再用 PIL 软件裁切**，尺寸随焦距变小：
  1x→3280×2464、1.5x→2186×1642、2x→1640×1232、3x→1092×820（纯裁切不放大）

⚠️ 屏幕 320×170（1.88:1）对照片 4:3：**横向 FOV 完全一致，竖直会裁掉约 30%**。

## 一·八、录像时取景不再冻结（2026-11-22）

原方案录像要停流再起 `rpicam-vid`（独占相机）→ 取景冻结，录完才回来。
现在改走 Picamera2 的编码器，**录像期间预览照常跑**：

| 编码器 | 720p 录像时预览帧率 | 文件 |
|---|---|---|
| H.264（`H264Encoder`） | **~8 fps**（软件编码吃满 CPU） | mp4 直接可播 |
| **MJPEG（`MJPEGEncoder`，当前默认）** | **9.7–10 fps** | 停下后 `ffmpeg -c:v copy` 转封装成 mp4 |

- 录像配置只有 main 1280×720 RGB888；预览从**同一条 main** 取帧缩到 320×170（颜色与平时一致）
- ⚠️ **MP4 里是 MJPEG 编码**：VLC 与设备本机相册可播；**iOS 相册、多数浏览器 `<video>` 不支持**，需下载后用支持 MJPEG 的播放器；
  真要兼容就改 `H264Encoder`（代价是录像时预览掉到 ~8fps），或在 PC 上转一次
- 转封装失败：**抛错**给上层（UI 显示 `ERR REMUX FAILED`，CLI 返回码 1），原始 `.mjpeg` 保留，坏容器改名 `.mp4.partial`
- 转封装帧率取**实测值**（帧数 ÷ 录制时长，夹取 1–120fps），不再写死 30

## 一·九、取景被拉伸（2026-11-22）

**根因**：`ScalerCrop` 给的是 **4:3 全幅**（0,0,3280,2464），但输出流是 320×170（1.88:1）。
libcamera 会把 4:3 的画面**硬塞进 1.88 的窗口** → 横向拉宽 1.41 倍。
照片走 3280×2464（4:3→4:3）所以正常，只有预览变形。

**修法**：`crop_rect(aspect)` 按目标宽高比算裁切框。
- 预览：`aspect = 320/170 = 1.882` → 1x 时裁 (0, 361, 3280, 1743)
- 录像：`aspect = 视频宽高比`（720p → 1.78），预览再 `fill_size` 到 320×170
- **照片仍按 4:3 裁**（`_crop_file` 用 SENSOR_SIZE 比例），所以成片比例不变

## 一·十、变焦不生效 / 一缩放就拉伸（2026-11-22）

**根因**：`ScalerCrop` 的坐标系是**整个像素阵列 3280×2464**，
即使传感器跑的是 1640×1232 的预览模式。我按“当前模式尺寸”算裁切框 → 越界 → 被 libcamera 钳制，
结果就是：1x 看着正常（钳制回默认的 1.88 裁切），2x/3x 既没放大、又变成 4:3 硬塞进 1.88 → 横向拉伸。

**修法**：`crop_rect()` 一律用 `SENSOR_SIZE` 计算。校验（真机 metadata）：

| 倍率 | 下发的 ScalerCrop | 实际生效 |
|---|---|---|
| 1.0x | (0, 360, 3280, 1742) | 完全一致 |
| 1.5x | (546, 652, 2186, 1160) | 一致 |
| 2.0x | (820, 796, 1640, 870) | 一致 |
| 3.0x | (1094, 942, 1092, 580) | 一致 |

全部保持 1.882 宽高比 → 不拉伸，且逐步缩小 = 真变焦。

## 一·七、按键说明页（`H`）

全屏两列按键表，**键帽字号 11、标签 10**（原来 8 号字在 320×170 上糊成一团），
只留单词标签：LOOKS / LUTS / ZOOM / CLEAN UI / SHARE QR / MODES 与 PHOTO…CAPTURE。
底部一行 `7 size 8 sound H/ESC close`。`H`、`ESC`、`Q` 关闭。

## 一·十一、相机层重写 + 自检脚本（2026-11-22）

我前几轮连续打补丁，出现过**方法整段被误删**（`start_recording`/`stop_recording`/`_record_config`
被一次区间替换连带删掉），所以把相机层整体重写成**一个类** `CameraStream`（原名 `MjpegStream`）：

- `start/stop`、`latest/next_frame`（对 app 接口不变）
- `capture_still(path, resolution)` —— 先拍全幅，再软件裁切（变焦）
- `start_recording/stop_recording` —— MJPEG 硬件编码 + ffmpeg 转封装
- `set_zoom/crop_rect` —— ScalerCrop 用**全幅 3280×2464 坐标**，按目标宽高比算
- `_restore_preview()` —— 任何失败路径都切回预览配置

死代码一并清掉（`_yuv_to_pil`、`_rec_yuv_preview`、`still_size`、`_sensor_pixels`）。

### `selftest.py`（在设备上跑，约 60 秒）

覆盖：启动 / 预览帧率 / 四档变焦（比对 metadata）/ 8MP 拍照 / 变焦拍照 / 720p 录像 +
录像期间预览帧率 / 录完恢复。最近一次结果：

| 项 | 结果 |
|---|---|
| 首帧 | 3.17 s |
| 预览 | **25.7 fps** |
| 变焦 | 四档 ScalerCrop 全部按预期生效，1x vs 2x 画面差 51（确实在放大） |
| 8MP 拍照 | **1.0 s**，1.33 MB |
| 2x 拍照 | 1640×1232（裁切不放大） |
| 720p 录像 | 启动 0.29 s，录像中预览 **9.7 fps**，mp4 3.2 MB |
| 录完恢复 | 正常出帧 |
| 内存低点 | 37 MB（录像时） |

## 一·十二、相册删除 + 图标修复（2026-11-22）

**图标没变的原因**：deb 打包用的是 `~/cybercam-build/packaging/icon.png`，
我只改了本地的 `packaging/icon.png`，设备上那份还是旧图（27KB）。
→ 覆盖设备上的 icon.png 后重打包，校验 md5 `f9cea55f…` 一致。
启动器是 **systemd --user 的 APPLaunch.service**，可 `systemctl --user restart APPLaunch.service` 立即生效。

**相册删除**：`Gallery.request_delete()` 两段确认——
第一次 `DEL` 提示 `DEL AGAIN TO DELETE xxx.jpg`（4 秒内有效），再按一次真删并刷新列表。
按键 `KEY_DELETE`，相册底栏提示改成 `4 PREV 5 NEXT 6 BACK DEL DELETE 9 CAM`。

## 一·十三、LUT 授权清理（2026-11-22）

要公开/上架就必须清理第三方授权：

- `abpy/FujifilmCameraProfiles` **没有 LICENSE**（默认保留所有权利）→ 之前从它合成的
  CLASSIC NEG / NOSTALGIC NEG / BLEACH BYPASS **已删除**
- 换成**自建参数化 look**（`build_looks.py` + `fit_all.py`）：Provia 打底 + 曝光/脚趾肩部/3×3 矩阵/饱和/分离色调，
  输出 33³ .cube。名字中性化（`look_classic_neg` 等）
- 富士官方 LUT 不可用：输入是 **F-Log2 / BT.2020**，且不提供再分发许可
- 试过 `JanLohse/spectral_film_lut`（MIT，按胶片数据表做光谱模拟）：
  能生成 Portra 400 等 10 支，但模型假设输入是**场景线性**，我们的 JPEG 已压过曲线
  → 高光发灰、暗部压死，归一化 + gamma 0.85 也只能救回一部分（脚本留在 `~/sfl/gen_luts.py`，未随包）
- 最终包内 18 支：G'MIC MIT ×14 + 自建 ×4

## 一·十四、自建 look 追平 abpy 观感（2026-11-22）

用户反馈「abpy 的 CN 更胶片」。分析后确认差别来自结构：abpy 的 CN 是 **Provia 渲染 → CN 转换** 两级串联，
第一级提供了富士的底子（亮中间调、高光滚降、绿色偏青）。

处理方式（**不使用对方文件**）：
1. 用 MIT 的 G'MIC **Provia** 作为第一级
2. 第二级写成可参数化模型：曝光 / 脚趾+肩部双 gamma / 3×3 色矩阵 / 饱和度 / 分离色调 / 抬黑
3. 用 `fit_all.py` 把这 **8 个参数**对目标观感做随机搜索拟合（三张样图 80×N 像素，约 2500 次采样）
4. 把两级合成为**单支 33³ LUT**

拟合结果（与目标图的平均像素差，0-255）：CN **7.4** / NN **5.5** / BB **7.1**——肉眼看不出差别。

⚠️ 这是**独立实现 + 参数拟合**，没有复制对方的 LUT 文件；观感（color grade）本身不受版权保护。

## 一·十五、按 dsh 审计报告修的一批（2026-11-22）

dsh 写了一份 `REVIEW.md`（281 行），逐条核对后修了这些：

| 编号 | 问题 | 修法 |
|---|---|---|
| B1 | `preview_fps` 存了不用 | `_loop` 里按 1/fps 节流发布（阈值 ×0.85，实测设 12 得 13.0fps） |
| B2 | 相机错误静默 | 主循环检测 `stream.error`，写日志 + 状态栏 `CAM ERR ...`（去重） |
| B3 | 主循环无异常保护 | `key()` 包 try/except，记日志 + 状态栏，不再整进程退出 |
| B4 | still/record 配置没锁 sensor mode | 两处 `create_*_configuration` 都补 `sensor={"output_size": ...}` |
| B6 | 设置里 `zoom` 坏值会让 app 起不来 | `load_settings` 归一化到 `ZOOMS` |
| B7 | `FIX_FMT` 解析错位（`parsed[8]` 不是 line_length） | 改读 `/sys/class/graphics/fb0/stride`，失败回退 `w*2` |
| C1 | 仓库里的 `look_*.cube` 是旧版（make_looks） | 换成 `build_looks.py`（Provia+拟合）产物，并提交 `look_params.json` |
| C6 | 两个生成器并存 | 删掉 `make_looks.py`，唯一路径 = `fit_all.py` → `build_looks.py` |

生成器现在可复现：`fit_all.py`（拟合，产出 `look_params.json`）→ `build_looks.py`（合成单支 LUT）。

## 二、扫码导出（Wi-Fi Share）

- **`W` 键**：起一个局域网 HTTP 相册服务（0.0.0.0:8765，token 保护），
  全屏显示二维码 `http://<设备IP>:8765/?key=<token>`
- 手机（同一 Wi-Fi）扫码 → 浏览器进相册 → 点照片下载 / 长按存相册
- `W` / `ESC` / `Q` 关闭界面，**服务继续跑**（底部提示 "SHARE STILL ON"）
- 二维码用 `python3-qrcode` 渲染；没装则只显示 IP:端口 + 访问方式
  → `install.sh` 会检查/安装 picamera2 / numpy / PIL / evdev / ffmpeg / qrcode
- 用的是上游自带的 `GalleryHTTP`（一次性 token 换 HttpOnly cookie，
  只允许访问 `Photos/` `GIF/` 等白名单目录，禁止符号链接和路径穿越）

## 安装

```bash
ssh cpz
sudo cp /tmp/cybercam.py /usr/lib/cybercam/cybercam.py   # 覆盖前先备份
# 或者：cd /tmp && patch -p1 < cybercam-film-share.patch   （在含 src/ 的目录里）
sudo apt install -y python3-qrcode
# 重启 CyberCam（APPLaunch 重新进）
```

回滚：`sudo cp /usr/lib/cybercam/cybercam.py.bak /usr/lib/cybercam/cybercam.py`

## 已做的验证（本地离线 + 真机）

- `py_compile` 通过；内置滤镜 / NORMAL / LUT 三条路径都出图正常
- 缺失 LUT 文件时回落到原图（不崩）
- Wi-Fi Share：token 错 → 403；带 cookie 取首页 200 且列出照片；`/media?path=` 下载 86 KB 正常
- 二维码用 iOS Vision 解码验证，payload 与 URL 完全一致
- 补丁 `patch -p1 --dry-run` 对上游 v0.5.3 干净应用
- ✅ 已在真机验证：帧率、8MP 内存峰值、变焦四档、录像、相册删除


## 一·十六、第三轮复核（2026-11-22）

dsh 的 `REVIEW-3.md`（A 0 / B 7 / C 12 / D 8 / E 9）指出我上一轮把 `FrameDurationLimits` 的语义写反了：
`(1000, 1e6/fps)` 是「帧时长 1ms…40ms」= 只禁止慢于 25fps，**没有**让相机按目标帧率出帧，
反而把暗光下 AE 的最长曝光锁死在 40ms → 预览变暗、噪点变多。**已移除该 control**，回到“让 AE 自由降帧 + 循环节流”。

同时修的：

| 编号 | 问题 | 修法 |
|---|---|---|
| B2/B3 | 重封装失败仍然报 `VIDEO SAVED`，文件改名成看不见的后缀 | 失败抛错；UI/CLI 分别报错；`.mjpeg` 保留、坏 mp4 改 `.mp4.partial`；重封装放进 `finally` |
| B4 | `CAM ERR` 被任意按键顶掉后不再出现 | 错误粘滞：每帧检查，`status` 不是 `CAM ERR` 就重设；恢复需连续 5 帧成功 |
| B5 | 只保护了主绘制，5 处 overlay 仍可杀进程 | 统一走 `safe_draw(image, tag)` |
| B6 | `stop_recording` 在锁内 `capture_metadata()`，可能冻住 UI | 改为在 `_loop` 每帧缓存 `FrameDuration`，停止时读缓存 |
| B7/C1 | 用单帧 `FrameDuration` 当整段 fps（还会快放/慢放） | 改成 `帧数 ÷ 录制时长`，夹取 1–120fps |
| C3 | 错误横幅抖动、日志每 0.4s 一行 | 连续 3 次失败才报、连续 5 次成功才恢复 |
| C4 | `CAM OK` 会把 `REC` 顶掉且不恢复 | `REC` 改为从 `self.recording` 派生 |
| C5 | `create_*_configuration` 仍在 try 外 | 抽 `_build_configs()`，整体包 try/except 并 `close()` |
| C6 | `.json.tmp` 残留、父目录未 fsync | 失败清理 + `fsync(dir)` |

真机验证：CLI 720p 录像 4s → 成片 3.96s；日志 `remux: 99 frames in 4.15s = 23.86 fps`（原来是写死 30，成片会快放 25%）。

---

## 一·十七、四轮之后：GPU 真正跑起来、曝光修好、去品红（2026-11-24 ~ 11-26）

### 1. GPU 崩溃的真正原因：`GL_RGB` 写成 `0x1908`

排查了十几轮（纹理尺寸、FBO 绑定、缓冲区生命周期、ctypes 签名、`UNPACK_ALIGNMENT`、uniform 调用、shader 复杂度）全部是伪线索。**根因是一个常量**：

```python
GL_RGB, GL_RGBA, GL_UBYTE = 0x1908, 0x1908, 0x1401   # 错
GL_RGB, GL_RGBA, GL_UBYTE = 0x1907, 0x1908, 0x1401   # 对
```

传 3 字节缓冲、驱动按 4 字节读，每行越界 33% → `vc4` SIGSEGV。Gemini 找到的。

配套三处：
- 源纹理 `src_tex`(RGB) 与 FBO 目标 `dst_tex`(RGBA) 必须分开（GLES2 反馈环）
- `glReadPixels` 第 0 行就是图像顶行，**不要**再 `FLIP_TOP_BOTTOM`
- 颗粒强度要除以 255（GLSL 里 `col` 是 0..1）

### 2. EGL 上下文只能在主线程创建

子线程首次 `eglCreateContext` 会在 C 层直接崩，`try/except` 拦不住，还会把整个进程标记成"无 GPU"。

两代方案：
1. 启动时主线程 `_gpu_ready()` 同步初始化 → 启动慢 2.7s
2. **最终版**：`_gpu-worker` 独立子进程持有 EGL，主进程通过管道下发任务 → 启动 3.0s（旧版 5s），GPU 崩了也不带崩相机

### 3. 曝光才是"天空偏紫"的主因

拍 still 时切传感器模式会让 AE 归零，而代码只 `time.sleep(0.08)` 就抓帧。结果随机：

| 拍到的 | 天空 RGB | 表现 |
|---|---|---|
| 欠曝 3 档 | 47/55/118 | B/G 2.14 → 深紫 |
| 过曝 | 181/175/208 | R/G 已削顶 → 发白偏品红 |

`_wait_for_exposure()` 循环读 `capture_metadata()`，直到 `ExposureTime` 与 `AnalogueGain` 连续两次变化 <4%（最多等 2.5s，通常 0.5–1s）。修完后与自带相机吻合（B/G 1.36/1.60/1.37 vs 1.40/1.58/1.32）。

### 4. 去品红：把绿通道补到蓝通道里

自然天空是 `R<G<B`，IMX219 给的是 `R≈G<B`（G 少 ~30）。所以不是饱和度问题（iPhone 反而更饱和），是色相。

```python
lift = max(0, B - G) * gain      # 只在蓝占优的像素生效
G += lift
```

`gain` 分四档 `colour_fix = 0/1/2/3`（0/0.18/0.28/0.36，默认 2）。

**踩的坑**：第一版一次性处理 8MP，`arr.copy()` + 多个 int16 临时数组峰值 85MB、耗时 1.9s，而且**同步跑在拍照流程里**——内存一紧就卡死（NORMAL 模式也卡，因为没有滤镜分支）。改为 512 行分块（峰值 ~28MB）+ 移到 `film_job` 渲染线程，NORMAL 完全不动原图。

### 5. HDR 这条线走不通

`HdrMode` 控件存在（0–4）且都能设，但实测：

```
0/1/2 → 直方图完全相同（121/23/142/201）
3     → 极暗（mean 26，21% 像素全黑）
4     → 略亮（mean 147）
```

pipeline 没有真正的多帧合成。要接近 iPhone 只能自己 bracket + 融合，成本高且有鬼影，暂不做。

### 6. 内存相关的既有结论

| 项 | 改动 | 收益 |
|---|---|---|
| `gpu_mem` | 128 → 64 | MemTotal 361 → **415 MB** |
| CMA | cma-64 → cma-128 | 8MP 连拍不再失败 |
| `malloc_trim(0)` | 渲染后/拍照前调用 | 连续 5 张全 8MP，内存稳定 174–180MB |

⚠️ `/tmp` 是 208MB tmpfs，放大文件等于直接吃内存（曾有两个 100MB 的 wav 把可用内存压掉一半）。

### 7. 商店

PR [CardputerZero/packages#799](https://github.com/CardputerZero/packages/pull/799)：`filmbox 0.9.0`，CI `validate`+`capture` 全过，演示视频已内嵌，等人工合并（队里 39 个）。

### 8. 已知未解决

- **worker 反复重启**：日志连续 `GPU worker ready`，渲染从 2.9s 变 7.67s。怀疑 `pkill -9` 留下孤儿进程导致接管冲突，待重启设备后确认
- **NORMAL 模式不修图**：故意为之（避免卡死），原图仍是偏品红的
- GitHub 落后 PC 两个提交（用户要求暂不推）
