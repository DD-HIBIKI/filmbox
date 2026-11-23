# 胶片 LUT 包（18 支，全部 sRGB in/out）

放到设备：`~/.config/cybercam/luts/`

## 授权状态（可公开发布）

| 文件 | 来源 | 许可 |
|---|---|---|
| `fuji_provia` `fuji_velvia` `fuji_astia` `fuji_classic_chrome` `fuji_pro_neg_hi` `fuji_pro_neg_std` `fuji_acros` `fuji_acros_red` | G'MIC `fujixtransiii`（`YahiaAngelo/Film-Luts`） | **MIT © 2024 Yahia** |
| `kodak_portra_160_vc` `kodak_portra_400` `kodak_ektar_100` `kodak_kodachrome_64` `kodak_ektachrome_100` `ilford_hp5_400` | G'MIC 胶片库（同上仓库） | **MIT © 2024 Yahia** |
| `look_classic_neg` `look_nostalgic_neg` `look_bleach_bypass` | **本项目自建**（`build_looks.py`：G'MIC Provia 打底 + 自己拟合的一层参数——曝光/脚趾与肩部/3×3 色矩阵/饱和/分离色调；参数由 `fit_all.py` 做随机搜索拟合（8 个参数）） | 本项目 + G'MIC MIT |
| `hasselblad_natural` | **本项目自建**近似（暖中性 + 压高饱和 + 高光软肩） | 本项目 |

> 之前有 3 支来自 `abpy/FujifilmCameraProfiles`（Classic Neg / Nostalgic Neg / Bleach Bypass 的「Provia→X」转换 LUT 合成）。
> **那个仓库没有 LICENSE 文件，默认保留所有权利**，因此 **已移除**，换成上表的自建 look。

## 关于商标

`Kodak` / `Fujifilm` / `Ilford` / `Hasselblad` 是注册商标。文件里保留这些名字便于自用，
但 **商店描述、截图、App 标题里建议用中性说法**（如 “warm negative / vivid / muted green / B&W”）。

## 另：光谱物理模型生成的版本（可选，未随包）

`~/_sfl/gen_luts.py`（PC 上）用 **`JanLohse/spectral_film_lut`（MIT）** 结合公开的胶片数据表，
可以生成 Portra 160/400/800、Ektar、Gold、UltraMax、Pro 400H、Superia、C200 等 10 支。

- 优点：有物理依据，许可干净（工具 MIT + 数据表为公开事实）
- 缺点：它假设输入是**场景线性**，而我们的 JPEG 已经压过曲线，于是高光发灰、暗部压死；
  用“归一化 + gamma 0.85”能救回来一部分，但整体比自建 look 更硬

需要时再启用；默认包里用的是上面 18 支。
