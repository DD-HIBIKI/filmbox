#!/usr/bin/env python3
"""生成三张合成场景图（无真实照片），给商店截图当取景器画面。

每张 960×510（3 倍屏），渲染时 fill_size 会缩到 320×170。
风格：简洁几何 + 强色彩层次，适合展示 LUT 的肤色/高光/色彩差异。
"""
import math
import random

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

W, H = 960, 510


def lerp_stops(y, stops):
    """按 y/H 在多段颜色之间插值。stops=[(pos,(r,g,b)),...]"""
    t = y / H
    for i in range(len(stops) - 1):
        p0, c0 = stops[i]
        p1, c1 = stops[i + 1]
        if p0 <= t <= p1:
            k = (t - p0) / (p1 - p0) if p1 > p0 else 0
            return tuple(int(c0[j] + (c1[j] - c0[j]) * k) for j in range(3))
    return stops[-1][1]


def sunset():
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    horizon = int(H * 0.66)
    stops = [(0.0, (14, 20, 46)), (0.28, (52, 48, 92)), (0.46, (128, 82, 108)),
             (0.56, (206, 106, 74)), (0.63, (243, 176, 104)), (0.66, (255, 214, 150))]
    for y in range(horizon):
        d.line([(0, y), (W, y)], fill=lerp_stops(y, stops))

    # 太阳 + 光晕
    sun = (int(W * 0.66), int(horizon - H * 0.055))
    glow = Image.new("RGB", (W, H), (0, 0, 0))
    gd = ImageDraw.Draw(glow)
    for r, a in ((150, 40), (100, 70), (66, 110), (46, 170)):
        gd.ellipse([sun[0] - r, sun[1] - r * 0.8, sun[0] + r, sun[1] + r * 0.8],
                   fill=(int(255 * a / 255), int(190 * a / 255), int(110 * a / 255)))
    glow = glow.filter(ImageFilter.GaussianBlur(28))
    img = Image.blend(img, Image.fromarray(
        np.clip(np.asarray(img, dtype=np.int16) + np.asarray(glow, dtype=np.int16) * 2,
                0, 255).astype(np.uint8)), 0.85)
    d = ImageDraw.Draw(img)
    d.ellipse([sun[0] - 26, sun[1] - 24, sun[0] + 26, sun[1] + 24], fill=(255, 238, 190))

    # 两层远山
    rnd = random.Random(4102)
    for layer, (base, amp, col) in enumerate(((0.075, 34, (58, 48, 74)),
                                              (0.045, 22, (38, 31, 52)))):
        pts = [(0, H)]
        ph = rnd.uniform(0, 6)
        for x in range(0, W + 12, 12):
            y = horizon - H * base + amp * (math.sin(x / 130 + ph) * 0.6
                                            + math.sin(x / 47 + ph * 2) * 0.4)
            pts.append((x, y))
        pts += [(W, H)]
        d.polygon(pts, fill=col)

    # 水面倒影
    water = img.crop((0, horizon - 150, W, horizon)).transpose(Image.FLIP_TOP_BOTTOM)
    water = water.resize((W, H - horizon)).filter(ImageFilter.GaussianBlur(3))
    water = Image.eval(water, lambda v: int(v * 0.5))
    img.paste(water, (0, horizon))
    d = ImageDraw.Draw(img)
    # 波纹：横向细线
    for y in range(horizon, H):
        k = (y - horizon) / (H - horizon)
        for _ in range(2):
            x0 = rnd.randint(0, W - 60)
            d.line([(x0, y), (x0 + rnd.randint(24, 90), y)],
                   fill=(int(150 + 70 * k), int(120 + 60 * k), int(110 + 70 * k)))
    # 芦苇
    for _ in range(9):
        x = rnd.randint(0, W)
        h = rnd.randint(50, 140)
        d.line([(x, H), (x + rnd.randint(-14, 14), H - h)], fill=(18, 14, 22), width=2)
        d.ellipse([x - 3, H - h - 12, x + 4, H - h + 6], fill=(24, 18, 26))
    return img


def city():
    img = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(img)
    stops = [(0.0, (8, 12, 30)), (0.45, (24, 30, 66)), (0.7, (58, 40, 74)),
             (1.0, (86, 48, 66))]
    for y in range(H):
        d.line([(0, y), (W, y)], fill=lerp_stops(y, stops))
    rnd = random.Random(9931)
    for _ in range(70):
        x, y = rnd.randint(0, W), rnd.randint(0, int(H * 0.55))
        img.putpixel((x, y), (215, 220, 235))

    ground = int(H * 0.78)
    x = -20
    while x < W + 40:
        w = rnd.randint(40, 110)
        h = rnd.randint(90, 300)
        top = ground - h
        d.rectangle([x, top, x + w, ground], fill=(13, 16, 26))
        # 窗户
        for wy in range(top + 12, ground - 10, 16):
            for wx in range(x + 8, x + w - 8, 14):
                if rnd.random() < 0.55:
                    c = rnd.choice([(255, 214, 140), (255, 236, 190), (150, 220, 255)])
                    d.rectangle([wx, wy, wx + 5, wy + 7], fill=c)
        x += w + rnd.randint(4, 18)

    # 霓虹条 + 湿地反光
    for col, yy in (((255, 64, 150), 300), ((64, 224, 255), 250), ((255, 180, 60), 330)):
        band = Image.new("RGB", (W, H), (0, 0, 0))
        bd = ImageDraw.Draw(band)
        bd.rectangle([0, yy, W, yy + 5], fill=col)
        band = band.filter(ImageFilter.GaussianBlur(6))
        img = Image.fromarray(np.clip(np.asarray(img, dtype=np.int16)
                                      + np.asarray(band, dtype=np.int16), 0, 255).astype(np.uint8))
    d = ImageDraw.Draw(img)
    d.rectangle([0, ground, W, H], fill=(18, 18, 30))
    for x in range(0, W, 6):                     # 湿地反光条纹
        for col in ((255, 64, 150), (64, 224, 255)):
            d.line([(x, ground + 2), (x, ground + rnd.randint(4, 22))],
                   fill=tuple(int(c * 0.25) for c in col))
    return img


def still():
    """水果静物：盘子 + 橙/苹果/柠檬，靠径向高光做出体积感。"""
    rnd = random.Random(2255)
    img = Image.new("RGB", (W, H), (26, 18, 16))
    # 背景：暖光从左上打进来
    bg = Image.new("RGB", (W, H), (22, 15, 14))
    bd = ImageDraw.Draw(bg)
    for r in range(520, 0, -10):
        k = 1 - r / 520
        bd.ellipse([W * 0.28 - r, H * 0.18 - r * 0.7, W * 0.28 + r, H * 0.18 + r * 0.7],
                   fill=(int(26 + 150 * k ** 2), int(18 + 96 * k ** 2), int(16 + 56 * k ** 2)))
    img = bg.filter(ImageFilter.GaussianBlur(38))
    d = ImageDraw.Draw(img)

    # 桌面
    table = int(H * 0.58)
    for y in range(table, H):
        k = (y - table) / (H - table)
        d.line([(0, y), (W, y)], fill=(int(118 - 52 * k), int(76 - 34 * k), int(50 - 24 * k)))
    d.line([(0, table), (W, table)], fill=(150, 104, 70))

    cx, cy = int(W * 0.52), int(H * 0.70)
    # 盘子和投影
    d.ellipse([cx - 300, cy - 44, cx + 300, cy + 96], fill=(96, 62, 42))
    d.ellipse([cx - 290, cy - 64, cx + 290, cy + 80], fill=(232, 226, 214))
    d.ellipse([cx - 268, cy - 52, cx + 268, cy + 66], fill=(244, 240, 232))

    def ball(x, y, r, base, shade=(0, 0, 0), hl=(255, 255, 255)):
        """径向渐变的球：左上亮、右下暗，带一点镜面高光。"""
        layer = Image.new("RGB", (2 * r + 4, 2 * r + 4), (0, 0, 0))
        ld = ImageDraw.Draw(layer)
        for rr in range(r, 0, -1):
            k = rr / r                          # 1=边缘 0=中心
            lit = 0.72 + 0.28 * (1 - k)         # 边缘 0.72，中心 1.0
            col = tuple(min(255, int(base[i] * lit)) for i in range(3))
            # 光源在左上：整体往右上偏一点，让暗部落在右下
            dx = int((1 - k) * r * 0.16)
            dy = int((1 - k) * r * 0.18)
            ld.ellipse([r - rr + dx + 2, r - rr + dy + 2,
                        r + rr + dx + 2, r + rr + dy + 2], fill=col)
        mask = Image.new("L", layer.size, 0)
        ImageDraw.Draw(mask).ellipse([2, 2, 2 * r + 1, 2 * r + 1], fill=255)
        # 高光
        hd = ImageDraw.Draw(layer)
        hd.ellipse([2 + r * 0.22, 2 + r * 0.16, 2 + r * 0.46, 2 + r * 0.34], fill=hl)
        layer = layer.filter(ImageFilter.GaussianBlur(1.2))
        img.paste(layer, (x - r - 2, y - r - 2), mask)

    # 三个水果（从后到前）
    # 接触阴影
    for gx, gr in ((cx - 120, 80), (cx + 96, 76), (cx - 6, 68)):
        d.ellipse([gx - gr, cy + 18, gx + gr, cy + 44], fill=(180, 172, 160))
    ball(cx - 120, cy - 34, 76, (238, 118, 40))         # 橙
    ball(cx + 96, cy - 38, 72, (198, 38, 44))           # 苹果
    ball(cx - 6, cy - 52, 64, (236, 206, 82))           # 柠檬
    # 苹果柄 + 叶
    d.line([(cx + 96, cy - 100), (cx + 100, cy - 122)], fill=(84, 52, 34), width=6)
    leaf = Image.new("RGBA", (70, 30), (0, 0, 0, 0))
    ImageDraw.Draw(leaf).ellipse([0, 0, 69, 29], fill=(64, 118, 58, 255))
    leaf = leaf.rotate(-28, expand=True, resample=Image.BICUBIC)
    img.paste(leaf, (cx + 98, cy - 138), leaf)

    # 左侧虚化的杯子轮廓，增加层次（不做细节，缩到 320 看不清）
    cup = Image.new("RGBA", (150, 190), (0, 0, 0, 0))
    cd = ImageDraw.Draw(cup)
    cd.rounded_rectangle([10, 30, 130, 180], 26, fill=(236, 230, 220, 255))
    cd.ellipse([10, 10, 130, 52], fill=(226, 218, 206, 255))
    cd.ellipse([26, 20, 114, 44], fill=(78, 50, 32, 255))
    cup = cup.filter(ImageFilter.GaussianBlur(9))
    img.paste(cup, (40, int(H * 0.30)), cup)

    # 整体压暗四角
    vig = Image.new("L", (W, H), 0)
    vd = ImageDraw.Draw(vig)
    for r in range(660, 120, -20):
        v = int(118 + 137 * (1 - r / 660))     # 边缘 118，中心 255（全亮）
        vd.ellipse([W / 2 - r * 1.25, H / 2 - r, W / 2 + r * 1.25, H / 2 + r], fill=v)
    dark = Image.new("RGB", (W, H), (8, 6, 6))
    img = Image.composite(img, dark, vig.filter(ImageFilter.GaussianBlur(50)))
    return img


if __name__ == "__main__":
    import os
    out = "/var/minis/shared/cardputer-zero/cybercam/store/scenes"
    os.makedirs(out, exist_ok=True)
    for name, fn in (("landscape", sunset), ("city", city), ("still", still)):
        im = fn()
        im.save(os.path.join(out, "%s-960.png" % name))
        print(name, im.size)
