# Colour comparison — same scene, three cameras

Shot on the same spot within seconds of each other: iPhone 15 Pro, the
Cardputer Zero built-in camera, and FilmBox 0.9.0.

`colour_comparison.jpg` lays them out three per row, with the sampled sky
colour and B/G ratio under each frame.

## What it shows

| Scene | iPhone B/G | built-in B/G | FilmBox B/G |
|---|---|---|---|
| Side-lit subject | 1.21 | 1.40 | 1.36 |
| Blue sky | 1.41 | 1.58 | 1.60 |
| Backlight | 1.13 | 1.32 | 1.37 |

The gap that mattered was not the ratio but the green channel: a clear sky
from the IMX219 reads R≈G<B (magenta-leaning) while a natural sky is R<G<B.
FilmBox applies `G += (B-G) * k` in bands (k = 0.00/0.18/0.28/0.36, default
0.28), which only touches pixels where blue dominates, so greens, skin and
neutrals are untouched.

## Rows

1. **Side-lit subject** — white balance on a neutral object; shadows and
   a saturated blue frame.
2. **Blue sky** — the case that originally looked magenta.
3. **Backlight** — sun through leaves; highlight headroom.

## Note

Exposure, not colour, was the first problem: stills used to be captured
80 ms after a sensor mode switch, before auto-exposure had settled, which
produced frames anywhere from 3 stops under to blown out. `_wait_for_exposure()`
now blocks until ExposureTime and AnalogueGain hold steady.

Remaining difference to the iPhone is hardware: 8 MP single-shot on a
1/4" sensor versus a 48 MP quad-bayer sensor with multi-frame HDR.
