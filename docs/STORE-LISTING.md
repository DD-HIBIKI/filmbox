# 商店上架文案与流程 / Store listing & publishing

> 元数据以 `app-builder.json` 的 `store` 段为准，这份文件是给人看的版本和操作手册。

## 一、商店文案（中文）

**标题**：FilmBox 胶片盒子

**一句话**（≤80 字符）：
> 给 Cardputer Zero 的胶片相机：18 支胶片 LUT、8MP、变焦、扫码取图

**详情**：

> FilmBox 把 Cardputer Zero 变成一台口袋胶片相机。
> 800 万像素全画幅拍照，18 支可切换的胶片观感（Provia、Velvia、Classic Chrome、
> ACROS、Portra、Ektar、Ektachrome、HP5……），1×/1.5×/2×/3× 变焦，
> 还有 GIF、静音 720p 录像、延时和定格动画。
> 全屏取景，右栏半透明叠在画面上；也可以切纯净模式，只留画面。
> 按 W 会启动一个带口令的 Wi-Fi 相册并显示二维码，手机扫码就能在浏览器里浏览、保存照片。
> 存图时把胶片效果、光晕和颗粒一起烘焙进去。
> 商店截图由应用自己的绘制代码渲染生成，不含任何真实照片。
> 基于 CyberCam v0.5.3（MIT）改造，18 支 LUT 均为 MIT 授权、可再分发。

**分类**：Media、Creative & Office
**权限**：相机 ✓、网络 ✓、麦克风 ✗、IMU ✗、其他硬件 ✗、后台服务 ✗、外接显示 ✗
**分享码**：`CYBK`

## 二、Store listing (English)

**Title**: FilmBox

**Summary** (76 chars):
> Film camera for Cardputer Zero: 18 film looks, 8MP, zoom and QR Wi-Fi export

**Description**:

> FilmBox turns the Cardputer Zero into a pocket film camera.
> 8 MP photos on the full IMX219 sensor, 18 switchable film looks (Provia, Velvia,
> Classic Chrome, ACROS, Portra, Ektar, Ektachrome, HP5 and more),
> 1x/1.5x/2x/3x zoom, GIF, silent 720p video, timelapse and stop-motion.
> A full-bleed viewfinder with a see-through side panel, or a slim pure-framing mode.
> Press W to start a token-protected Wi-Fi gallery and scan the QR code: photos open
> in any phone browser. Photos are saved with the film look baked in, plus halation
> and grain. Screenshots are rendered from the app's own drawing code; no photograph
> is bundled. Fork of CyberCam v0.5.3 (MIT); all 18 LUTs are MIT and redistributable.

**Categories**: Media, Creative & Office
**Share code**: `CYBK`

## 三、上架流程（CardputerZero AppStore）

官方工具是 `czdev`（M5Stack / CardputerZero-AppBuilder），流程如下：

```bash
# 0. 一次性：clone 官方构建工具
git clone https://github.com/m5stack/CardputerZero-AppBuilder.git
cd CardputerZero-AppBuilder

# 1. 登录（GitHub OAuth device flow，浏览器里输一次码）
./czdev login

# 2. 在仓库目录构建 .deb（本项目自带打包脚本，不用 CI）
cd ~/filmbox && python3 packaging/make_package.py
# → ../filmbox_0.6.0_all.deb

# 3. 发布：校验 → 上传 .deb 到 GitHub Release → 提 metadata PR
cd CardputerZero-AppBuilder
./czdev publish --deb ~/filmbox_0.6.0_all.deb

# 4. 只改商店文案/截图（发新版时才需要 --deb）
./czdev publish --meta-only
```

**校验规则**（`czdev` 本地就会跑一遍）：

| 项 | 要求 | 我们的状态 |
|---|---|---|
| `store.screenshots` | 1–6 张 **320×170** PNG | ✅ 6 张 |
| `store.icon` | 正方形 PNG，128–512px | ✅ 256 |
| `store.summary` | ≤80 字符 | ✅ 76 |
| `store.categories` | 1–2 个，固定枚举 | ✅ Media / Creative & Office |
| `store.author.display_name` | 不能是 `M5Stack` / `Your Name` | ✅ HIBIKI |
| `store.share_code` | 4 位，全站唯一 | ✅ CYBK |
| `.deb` 服务 | 不能以 root 运行 | ✅ 无 service，仅启动器 |
| `Maintainer` | 不能是模板默认值 | ✅ HIBIKI |

**首次发布不会自动合并**：PR 建好后，要在 PR 评论里贴一段**真机运行视频**，
管理员看完才会合并。版本更新在通过校验后自动合并。

> ⚠️ 包名 `filmbox` 先到先得：第一个发布它的 GitHub 账号拥有该包名。
> 发布后只有该账号（或仓库管理员）能发新版本。

## 四、发布前自查

```bash
# 1. 敏感信息：不应有个人路径、IP、密钥
rg -n '192\.168\.|/home/|C:\\|Users\\|BEGIN .*PRIVATE|ghp_' --glob '!screenshots/**' .

# 2. LUT 授权：luts/ 只应有 18 支公开版，没有 p_*_abpy
ls luts/*.cube | wc -l        # 期望 18
ls luts/p_* 2>/dev/null       # 期望无输出

# 3. 截图尺寸
python3 -c "from PIL import Image; import glob; \
print([Image.open(f).size for f in glob.glob('screenshots/*.png')])"
```
