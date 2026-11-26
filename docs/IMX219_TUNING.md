# IMX219 偏色分析、软件修正与硬件 ISP 调优指南

本文档记录 Cardputer Zero（Raspberry Pi CM0 / VideoCore IV / Sony IMX219 8MP）上的偏色原因、内置软件修正模型，以及如何在硬件 ISP 层加载自定义 `imx219.json` 调优文件。

---

## 一、偏色现象与成因（对比 iPhone 15 Pro + 真机室内实测）

同场景室外对照见 [`comparisons/colour_comparison.jpg`](comparisons/colour_comparison.jpg)，真机室内测试报告见 [`reports/2026-11-26-colour-fix-test.md`](reports/2026-11-26-colour-fix-test.md)（配图 [`reports/indoor-before-after.jpg`](reports/indoor-before-after.jpg)）。结合设备原版 [`tuning/imx219-vc4.json`](tuning/imx219-vc4.json) 的 `rpi.ccm` 分析，原因如下：

1. **室内白平衡本身准确，不能施加全局红绿乘数**
   - 室内白墙实测原始值 `113/114/111`（三通道差 <= 3）；全局 `r_mul=0.91 / g_mul=1.05` 会把白墙拉成 `102/119/111`（明显泛绿）。
2. **蓝天发紫的核心诱因在 `imx219-vc4.json` 的高色温 `rpi.ccm` 矩阵**
   - 室内段（`2860–4650 K`）的 `G_row` 蓝通道负串扰在 `-0.83 .. -1.06`；但到了 `7580 K`（蓝天/开阔阴影）骤增至 **`-1.50047`**（对角 `+3.07922`，行和仍为 `1.00000`）。
   - 行和 `1.00000` 使中性白墙不受影响，但蓝天（`B > G`）的绿通道被显著压低，表现为 `R ≈ G < B` 偏紫。
3. **逆光绿叶偏红褐（弱 IR-cut 近红外漏光）**
   - 叶绿素反射 700–900 nm 近红外光渗入红通道，属模组光学特性，无法靠全局压红在不破坏室内的前提下完全消除。

---

## 二、软件层修正模型（`colour_fix` 0..3，仅作用于 `B > G` 蓝区）

为了同时保证**室内白墙/肤色/绿叶 0 改动**且修正蓝天发紫，所有内置档位保持全局 `(R, G, B) = (1.00, 1.00, 1.00)`，仅对 `B > G` 的蓝区像素按 `G += (B - G) * sky_lift` 提绿：

| 档位 (`colour_fix`) | 参数 `(R, G, B, sky_lift)` | 室内白墙 `113/114/111` 变化 | 适用场景 |
|---:|---|---:|---|
| `0` (OFF) | `(1.00, 1.00, 1.00, 0.00)` | `0 / 0 / 0` | 关闭（启用自定义 `imx219.json` 时推荐） |
| `1` (LOW) | `(1.00, 1.00, 1.00, 0.12)` | `0 / 0 / 0` | 轻度蓝天提绿 |
| `2` (MED，默认) | `(1.00, 1.00, 1.00, 0.20)` | `0 / 0 / 0` | 日常平衡档（中性色严格不变） |
| `3` (HIGH) | `(1.00, 1.00, 1.00, 0.28)` | `0 / 0 / 0` | 强蓝天去紫（等同旧版 0.28 强度） |

### 管线与性能特点（已在 Cardputer Zero 真机验证）
- **LUT 胶片路径（GPU）**：在 GLES2 片元着色器 `FS` 中通过 `uniform vec4 colourFix;` 无分支执行，紧接 3D LUT 采样前完成——**0 ms 额外耗时、0 额外内存、单次 JPEG 解码**，不改动原片。
- **CPU / 缩略图 / 取景器路径**：256 行分带原地写回（8MP 峰值 RSS 增量仅 **+23 MB**），内置档位跳过全图乘表、仅扫描 `B > G` 掩码，非蓝条带零拷贝直接跳过；`PHOTO` 模式取景器与 `LookPicker` 在调用 `filter_image(..., preview=True)` 之前已先执行 `_fix_magenta_band`，实现所见即所得且不会双重叠加。
- **`NORMAL` 模式**：通过 `colour_fix_job()` 在后台 daemon 线程以原子写盘（`_atomic_save_image`）静默修正，共享 `film_busy` 且低于 60 MB 剩余内存自动跳过，**不冻结取景器**。
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

### 3. 真机实测候选文件与部署步骤

仓库已基于真机原版 [`tuning/imx219-vc4.json`](tuning/imx219-vc4.json) 生成一份仅微调日光/蓝天段 `rpi.ccm`（室内 `2860–4650 K` 字节级不变、每行和严格保持 `1.00000`）的候选文件：[`tuning/imx219-vc4-skyfix.json`](tuning/imx219-vc4-skyfix.json)。

```bash
# 1) 测试仓库内置的蓝天 CCM 候选调优文件（关闭软件修正 --colour-fix 0，避免双重补偿）
mkdir -p ~/.config/filmbox
cp docs/tuning/imx219-vc4-skyfix.json ~/.config/filmbox/imx219.json
filmbox diagnose                      # 确认 isp tuning 显示为 ~/.config/filmbox/imx219.json
filmbox photo --colour-fix 0          # 室外蓝天 + 室内白墙各拍一张对照

# 2) 若效果满意，持久化关闭软件修正，让硬件 ISP 零开销同时生效于拍照/预览/录像/延时：
filmbox config --colour-fix 0

# 3) 临时切回系统默认做 A/B 对照（无需删除文件）：
FILMBOX_TUNING_FILE=0 filmbox photo --colour-fix 0
```
