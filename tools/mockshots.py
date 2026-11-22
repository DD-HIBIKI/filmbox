#!/usr/bin/env python3
"""用合成场景渲染 CyberCam 界面截图 —— 不启相机、不碰硬件。

在设备上跑（需要 APP 里的字体、LUT 目录）：
    python3 mockshots.py
输出：$OUT/01-viewfinder.png … 06-gallery.png（320×170）

原理：draw_ui / LookPicker.render / draw_gallery 都是纯绘制函数，
只需要一张 PIL Image 当"取景器画面"，所以可以完全离线出图。
"""
import importlib.util
import os
import sys
from pathlib import Path
from types import SimpleNamespace

from PIL import Image

APP = os.environ.get("CZ_APP", "/usr/lib/cybercam/cybercam.py")
SCENES = Path(os.environ.get("SCENES", "/tmp/scenes"))
OUT = Path(os.environ.get("OUT", "/tmp/mockshots"))
W, H = 320, 170

spec = importlib.util.spec_from_file_location("czmod", APP)
cz = importlib.util.module_from_spec(spec)
sys.modules["czmod"] = cz
spec.loader.exec_module(cz)


def scene(name):
    return Image.open(SCENES / f"{name}.png").convert("RGB")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    shots = {}

    shots["01-viewfinder"] = cz.draw_ui(
        scene("landscape"), mode="PHOTO", style="LUT:fuji_classic_chrome",
        resolution="8MP", status="READY", sound=True, fps=25, zoom=1.0)

    shots["02-still-portra"] = cz.draw_ui(
        scene("still"), mode="PHOTO", style="LUT:kodak_portra_400",
        resolution="8MP", status="READY", sound=True, fps=25, zoom=1.0)

    shots["03-city-acros"] = cz.draw_ui(
        scene("city"), mode="PHOTO", style="LUT:fuji_acros",
        resolution="8MP", status="READY", sound=True, fps=25, zoom=3.0)

    shots["04-clean"] = cz.draw_ui(
        scene("landscape"), mode="PHOTO", style="LUT:fuji_velvia",
        resolution="8MP", status="READY", sound=True, fps=25, clean=True)

    picker = cz.LookPicker(current="LUT:fuji_classic_chrome", frame=scene("landscape"))
    shots["05-looks"] = picker.render(W, H)

    gallery = SimpleNamespace(
        full=False, kind="jpg", index=0,
        items=["cyber_193045_812.jpg"],
        path=Path("cyber_193045_812.jpg"),
        video=None, playing=False, message=None)
    shots["06-gallery"] = cz.draw_gallery(scene("still"), gallery, W, H)

    for name, image in shots.items():
        image.save(OUT / (name + ".png"))
    print("ok", len(shots))


if __name__ == "__main__":
    main()
