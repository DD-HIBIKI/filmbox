# CyberCam for Cardputer Zero — 胶片 LUT / 变焦 / 扫码导出改造版

基于上游 **CyberCam v0.5.3**（`cybern0mads/CyberCam`，MIT）改造的相机应用，
跑在 **M5Stack Cardputer Zero**（Raspberry Pi CM0，IMX219 8MP，320×170 屏）上。

> 上游原始文件是 `src/cybercam.py`（2031 行）；这里的是改造后的单文件版本，
> 差异见 `cybercam-film-share.patch`，改动细节见 [`NOTES.md`](NOTES.md)。

## 这台设备上的实机数据

| 项 | 结果 |
|---|---|
| 预览 | 320×170，**25.7 fps**（默认 25；设 12 实测 13.0） |
| 首帧 | 1.7–3.2 s（冷启动） |
| 8MP 拍照 | **0.8–1.0 s**（拍完立刻回取景，暂停 <1 s） |
| 变焦 | 1x / 1.5x / 2x / 3x，四档 `ScalerCrop` 实测生效 |
| 720p 录像 | MJPEG，**录像中预览 9.7–10 fps**；成片帧率按实测（75 帧/3.58 s ≈ 20.9 fps） |
| 胶片副本 | 8MP 后台渲染 **~7.6 s**（2MP 2.4 s） |
| 内存低点 | 65–70 MB（录像时） |

## 相机层

一条 **Picamera2 单会话**包办预览、拍照、录像（早期版本是 rpicam 子进程，拍照要停流 4.8 s）：

```
CameraStream
├── 预览   main 320×170 RGB888 → JPEG → UI（25 fps）
├── 拍照   切 3280×2464 配置 → capture_file → 切回（~1 s）
├── 变焦   ScalerCrop（全幅 3280×2464 坐标，按输出宽高比算）
└── 录像   MJPEG → .mjpeg → ffmpeg -c:v copy → .mp4
```

## 功能

- **胶片 LUT**：内置 `/usr/share/filmbox/luts/*.cube`（用户可在 `~/.config/filmbox/luts/*.cube` 新增或覆盖），`5` 打开选择器（内置滤镜 + LUT，右侧实时预览）
- **胶片副本**：LUT → halation（彩色暖光晕 / 黑白银盐中性光晕）→ grain（按亮度调制的颗粒）→ JPEG，原片保留
- **变焦**：`Z` 循环 1x/1.5x/2x/3x，照片按同样倍率软件裁切（不放大）
- **扫码导出**：`W` 起局域网相册（token 保护）+ 全屏二维码，手机扫码下载
- **相册删除**：`DEL` 两次确认
- **两种界面**：`T` 切换标准 / 纯净（窄条只留数字）
- **按键说明页**：`H`

## LUT 包（`luts/`，18 支，sRGB in/out）

**全部可再分发**（详见 `luts/README.md`）：

| 来源 | 内容 | 许可 |
|---|---|---|
| G'MIC `fujixtransiii` | PROVIA / VELVIA / ASTIA / CLASSIC CHROME / PRO NEG HI·STD / ACROS(+R) | MIT © 2024 Yahia |
| G'MIC 胶片库 | Portra 160VC / Portra 400 / Ektar 100 / Kodachrome 64 / Ektachrome 100 / HP5 400 | MIT © 2024 Yahia |
| **自建参数化 look**（`build_looks.py` + `fit_all.py`） | CLASSIC NEG / NOSTALGIC NEG / BLEACH BYPASS | 本项目 |
| **自建近似** | HASSELBLAD NATURAL（不是 HNCS 复刻） | 本项目 |

> 早期有 3 支来自 `abpy/FujifilmCameraProfiles`（无 LICENSE 文件 → 保留所有权利），
> **已移除并换成自建 look**。

⚠️ 富士官方 LUT、`V-Log-Alchemy` 的哈苏 LUT 都是 log/BT.2020 **视频**用，不能喂 sRGB 照片。
⚠️ 商标：Kodak / Fujifilm / Ilford / Hasselblad 为注册商标，商店描述建议用中性说法。

## 安装（设备上）

```bash
scp -r . pi@<cpz>:~/cybercam
ssh pi@<cpz> 'sh ~/cybercam/install.sh'
# 或者在设备上直接打包安装 deb（Raspberry Pi OS 基于 Debian）：
#   python3 packaging/make_package.py && sudo apt install -y ../filmbox_0.8.6_all.deb
```

## 自检

改完相机相关代码后，在设备上跑：

```bash
python3 selftest.py     # 约 60 秒，覆盖启动/预览/四档变焦/8MP+1080P/录像/恢复
```

## 目录

```
filmbox.py                  应用本体（单文件，Python，FilmBox 0.8.6）
packaging/make_package.py   Debian / Raspberry Pi OS (.deb) 打包脚本
tools/                      GPU (EGL/GLES2) / 多核加速实验与基准脚本
cybercam-film-share.patch   早期相对上游 v0.5.3 的历史 diff
selftest.py                 相机层自检
install.sh                  一键安装（备份 + 安装 filmbox + 拷 LUT + 补齐依赖）
luts/                       18 支 .cube + 说明
icon-100.png                像素风图标（25×25 ×4）
NOTES.md                    改动记录与踩坑
```

## 生成器

```
fit_all.py       拟合 look 参数（需外部的参考 LUT 目录 --ref；产出 look_params.json）
build_looks.py   Provia + 参数层 → luts/look_*.cube（只需仓库内文件，任意平台可复现）
```

## 文档

| 文件 | 用途 |
|---|---|
| `README.md` | 本文件：项目概览、实机数据、安装 |
| `NOTES.md` | 改动记录 15 节（含每个坑的原因与修法） |
| `REVIEW.md` / `REVIEW-2.md` / `REVIEW-3.md` / `REVIEW-4.md` | 四轮代码审计与修复记录 |
| `REVIEW-RESPONSE.md` | 第一轮的逐条处理记录 |
| `luts/README.md` | 18 支 LUT 的来源与授权 |
| `selftest.py` | 相机层自检（60 秒 7 项） |

## 已知取舍

- 屏幕 320×170（1.88:1）对照片 4:3 → **横向视野一致，竖直裁掉约 30%**
- 录像 MP4 内是 **MJPEG**：VLC / 设备本机相册可播；**iOS 相册和多数浏览器 `<video>` 不认**，需下载后用支持 MJPEG 的播放器
- 720p 录像时预览约 10 fps（软件编码与预览抢 CPU）
