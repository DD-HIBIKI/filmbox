# IMX219 偏色分析、软件修正与硬件 ISP 调优指南

本文档记录 Cardputer Zero（Raspberry Pi CM0 / VideoCore IV / Sony IMX219 8MP）上的偏色原因、内置软件修正模型，以及如何在硬件 ISP 层加载自定义 `imx219.json` 调优文件。

---

## 一、偏色现象与成因（对比 iPhone 15 Pro）

同场景实拍对照见 [`comparisons/colour_comparison.jpg`](comparisons/colour_comparison.jpg)（说明见 [`comparisons/README.md`](comparisons/README.md)）。对 3 组场景 9 个同名区域逐像素分析后，IMX219 模组相对 iPhone 15 Pro 的主要差异有两点：

1. **全画面红通道偏高（+7%～13%）、绿通道偏低（-3%～13%）**
   - 蓝天原本呈明显薰衣草紫（例如 `151/140/213`，色相 `249°`，而 iPhone 为 `130/160/215`，色相 `219°`）。
   - 中性灰地砖、白墙、云层与沥青路面也普遍泛粉红/品红（`R/G` 达到 `1.14–1.22`，iPhone 为 `0.96–1.03`）。
2. **逆光绿叶偏红褐色（硬件 IR-cut 偏弱 / 近红外漏光）**
   - 植物叶绿素在 700–900 nm 强烈反射近红外光并主要渗入红通道，导致第 3 组逆光树叶呈红褐（`50/33/38`）而非深绿（`36/44/41`）。仅靠软件后处理无法 100% 复原，但压低全局红通道可显著减轻。

---

## 二、软件层修正模型（`colour_fix` 0..3，默认已开启）

早期版本仅在 `B > G` 区域提绿（`G += (B-G) * k`），只能修正蓝天误差的约一半，且完全无法改善地砖/白墙/云层的粉红底色；同时在 `film_job()` 中同步覆写 8MP 原图导致额外 2.5–3 s 卡顿与 ~72 MB 内存峰值。

当前版本已升级为 **4 参数模型 `(r_mul, g_mul, b_mul, sky_lift)`** 并融合进渲染管线：

| 档位 (`colour_fix`) | 参数 `(R, G, B, sky_lift)` | 9 区平均色度误差（vs iPhone） | 蓝天色度误差 | 适用场景 |
|---:|---|---:|---:|---|
| `0` (OFF) | `(1.00, 1.00, 1.00, 0.00)` | 7.00% | 5.76% | 关闭（启用硬件 `imx219.json` 时推荐） |
| `1` (LOW) | `(0.95, 1.03, 1.00, 0.09)` | 5.07% | 3.32% | 室内暖光 / 人像保守档 |
| `2` (MED，默认) | `(0.91, 1.05, 1.00, 0.14)` | **4.01%** | **1.73%** | 室外日常综合最优 |
| `3` (HIGH) | `(0.87, 1.07, 1.00, 0.19)` | **3.46%** | **0.57%** | 强日光 / 重度近红外偏红场景 |

### 管线与性能特点
- **LUT 胶片路径（GPU）**：在 GLES2 片元着色器 `FS` 中通过 `uniform vec4 colourFix;` 无分支执行，紧接 3D LUT 采样前完成——**0 ms 额外耗时、0 额外内存、单次 JPEG 解码**，不改动原片。
- **CPU / 缩略图 / 取景器路径**：使用 256 字节缓存通道查表 + 掩码 `uint16` 定点提绿，256 行分带原地写回；`320×170` 实时取景仅约 `0.39 ms/帧`。
- **`NORMAL` 模式**：通过 `colour_fix_job()` 在后台线程以原子写盘（`_atomic_save_image`）静默修正，**不冻结取景器**。
- **黑白 LUT 保护**：`fuji_acros` / `ilford_hp5_400` 等黑白胶片在修正后仍经单色 LUT 与中性银盐白光晕，输出严格单色。

### 命令行调整档位
```bash
# 查看当前配置（含 colour_fix 与 isp tuning）
filmbox diagnose
filmbox config

# 持久化修改默认档位（0=关 1=低 2=中 3=高）
filmbox config --colour-fix 2

# 单次 CLI 拍照临时指定档位
filmbox photo --filter LUT:fuji_classic_chrome --colour-fix 2
```

---

## 三、硬件 ISP 调优支持（自定义 `imx219.json`）

> ⚠️ **仅限 VC4 ISP 文件**：Cardputer Zero（CM0 / RP3A0）使用树莓派 **VC4** 管线，只能使用 `vc4` 格式的 `imx219.json`，**不可使用** Raspberry Pi 5 的 `pisp` 调优文件。

FilmBox 已在 `CameraStream`（`Picamera2` 预览/拍照/录像）、`capture_still`（CLI `rpicam-still`）和 `TimelapseCapture`（延时 `rpicam-still`）三处统一接入自定义调优文件自动发现与加载。

### 1. 自动查找优先级（`resolve_tuning_file()`）
1. 环境变量 `FILMBOX_TUNING_FILE`（设为 `0` / `off` / `none` / `default` 时强制使用系统默认）
2. 环境变量 `LIBCAMERA_RPI_TUNING_FILE`
3. 用户目录覆盖：`~/.config/filmbox/imx219.json`
4. 仓库/安装目录：`tuning/imx219.json` 或 `./imx219.json`
5. 系统包内置：`/usr/share/filmbox/imx219.json`（`packaging/make_package.py` 会自动打包仓库里的 `tuning/imx219.json`）
6. 若上述均不存在或 JSON 损坏，自动记录日志并回退系统默认 `/usr/share/libcamera/ipa/rpi/vc4/imx219.json`，不会导致相机无法启动。

### 2. 参考资源
- **官方 Raspberry Pi `libcamera` VC4 调优目录**：
  - 仓库路径：[raspberrypi/libcamera `src/ipa/rpi/vc4/data/`](https://github.com/raspberrypi/libcamera/tree/main/src/ipa/rpi/vc4/data)
  - 标准版：`/usr/share/libcamera/ipa/rpi/vc4/imx219.json`
  - NoIR 去红外滤片版：`/usr/share/libcamera/ipa/rpi/vc4/imx219_noir.json`（改用 grey-world AWB 压制无 IR-cut 导致的粉红/品红偏色）
- **第三方模组暗角偏红 / 镜头阴影（ALSC）校准**：
  - [Arducam Lens Shading Calibration 指南](https://www.docs.arducam.com/Raspberry-Pi-Camera/Native-Camera/Lens-Shading-Calibration/)
  - [Arducam IMX219 调优文件参考](https://www.docs.arducam.com/Raspberry-Pi-Camera/Focus-Camera/Arducam-8MP-IMX219-Motorized-Focus-Camera-For-Raspberry-Pi/)
- **官方相机标定工具（CTT）**：
  - `raspberrypi/libcamera` 中的 `utils/raspberrypi/ctt/`，使用灰卡 + 24 色卡 DNG 可标定出该模组专属的 `rpi.alsc`、`rpi.awb`、`rpi.ccm`。

### 3. 真机实测与部署步骤
```bash
# 1) 查看设备自带的 imx219 调优文件
ls -la /usr/share/libcamera/ipa/rpi/vc4/imx219*.json

# 2) 先测官方 NoIR 文件（务必关掉软件修正 --colour-fix 0，避免双重校正）
#    - 若蓝天/树叶恢复自然 → 模组属于弱/无 IR-cut，可直接使用或微调 imx219_noir.json
#    - 若整张画面明显偏绿 → 模组带 IR-cut，应基于标准 imx219.json 微调 rpi.awb / rpi.ccm
FILMBOX_TUNING_FILE=/usr/share/libcamera/ipa/rpi/vc4/imx219_noir.json \
  filmbox photo --colour-fix 0

# 3) 放置专属调优文件使其长期生效（同时作用于拍照、实时预览、录像与延时）
mkdir -p ~/.config/filmbox
cp /usr/share/libcamera/ipa/rpi/vc4/imx219.json ~/.config/filmbox/imx219.json
# 编辑 ~/.config/filmbox/imx219.json 中的 rpi.awb / rpi.ccm / rpi.alsc 后：
filmbox diagnose                # 确认 isp tuning 显示为 ~/.config/filmbox/imx219.json
filmbox config --colour-fix 0   # 硬件 ISP 已校准时关闭软件修正，避免重复补偿
```
