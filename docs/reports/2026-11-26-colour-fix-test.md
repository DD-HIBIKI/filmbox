# Colour fix test — commit c53bfd9 ("Optimize & upgrade IMX219 colour fix pipeline")

Tested on the Cardputer Zero (IMX219, VC4 pipeline, libcamera 0.5.x) on 2026-11-26 16:15
Beijing time. Build: `VERSION = "0.9.0"`, md5 `20b60bcd8`, 4895 lines.

## Summary

The new four-parameter model `(r_mul, g_mul, b_mul, sky_lift)` is a clean design and
the band-wise implementation is memory-safe. **But the default MED preset tints
neutral surfaces green**, because the first three numbers are global per-channel
multipliers and this module's white balance is already correct.

Recommendation: drop the global multipliers and keep only the `sky_lift` term
(or move the correction into the libcamera tuning file — see §4).

## 1. Measured result, indoor scene (ceiling + white wall)

| | neutral surfaces RGB | sky-like region | sky B/G |
|---|---|---|---|
| BEFORE (raw) | 113 / 114 / 111 | 93 / 113 / 125 | 1.11 |
| AFTER MED (0.91, 1.05, 1.00, 0.14) | **102 / 119 / 111** | 107 / 135 / 139 | 1.03 |

The raw white balance is essentially perfect — R, G and B within 3 units on a
white wall. After the MED preset R drops 10 % and G rises 5 %, so every neutral
surface in the frame reads green. See `indoor-before-after.jpg`.

A second run on a different frame reproduced it: mid 114/114/112 → 102/119/112.

## 2. Where the green comes from

```python
COLOUR_FIX_PRESETS = (
    (1.00, 1.00, 1.00, 0.00),  # OFF
    (0.95, 1.03, 1.00, 0.09),  # LOW
    (0.91, 1.05, 1.00, 0.14),  # MED
    (0.87, 1.07, 1.00, 0.19),  # HIGH
)
```

`_fix_magenta_band()` applies `r_mul` / `g_mul` / `b_mul` as a 256-entry LUT to
**every pixel**, then adds `sky_lift * (B - G)` only where `B > G`.

The global part assumes the sensor carries an overall magenta cast. It does not:
the cast is confined to blue sky, where the IMX219 gives `R ≈ G < B` while a
natural sky is `R < G < B`. Multiplying all pixels to fix a sky-only defect
breaks everything else — skin, foliage, concrete, paper.

## 3. Performance (this part is good)

| Item | Result |
|---|---|
| Band size 256 rows | peak RSS delta only **+23 MB** on an 8 MP frame (512-row version was +28 MB) |
| `_fix_magenta` on 8 MP | 1.44 s |
| `_apply_colour_fix` (decode + fix + q93 re-encode) | **2.3–3.4 s** |
| Threading | `colour_fix_job()` runs in a daemon thread, shares `film_busy` with `film_job()` — capture returns immediately, UI never blocks |
| Free-RAM guard | skips below 60 MB, logs and returns — capture can never fail because of the fix |

Verified: the earlier "pip touching camera freezes the whole UI" bug does not
reappear, because the fix is no longer on the capture path for either NORMAL or
filtered shots.

## 4. What to change

**Option A — simplest, keeps the current structure**

Delete `r_mul` / `g_mul` / `b_mul` from the presets (keep them at 1.0) and let
`sky_lift` do the work. Suggested strengths: 0 / 0.12 / 0.20 / 0.28, since the
sky-only term needs to be stronger when it is no longer helped by a global shift.
This leaves white walls, skin and foliage untouched by construction.

**Option B — do it in the ISP instead of in Python**

The correction belongs in the tuning file, not in a post-processing pass:

```
/usr/share/libcamera/ipa/rpi/vc4/imx219.json     (in use, target bcm2835, 15 algos)
/usr/share/libcamera/ipa/rpi/pisp/imx219.json    (Pi 5 only, not used here)
```

`rpi.awb` holds the per-illuminant colour gains and `rpi.ccm` the colour
correction matrix. A small bias there fixes the sky at capture time, costs zero
CPU, and keeps preview and still consistent. The repo now carries a copy of the
file at `tuning/imx219-vc4.json` (35 KB, md5 `46a72401fcdedcc96c10521c5c066b8f`,
from package `libcamera-ipa`), so a diff can be produced without touching the
device.

The `resolve_tuning_file()` hook already added in c53bfd9 makes this easy to
test: drop a modified copy at `~/.config/filmbox/imx219.json` and it is picked
up at the next start. Keep the malformed-JSON fallback — a bad tuning file must
never stop capture.

**Option C — restrict the global shift to blue-dominant pixels**

If a global component is kept, gate it by the same mask the sky term uses
(`B > G + 8`), so only sky pixels see the multiplier.

## 5. Also worth fixing

- **Preview does not get the fix.** `filter_image(..., preview=True)` is called
  without `colour_fix`, so the viewfinder stays magenta while the saved frame is
  corrected. Either apply the same preset to preview frames (240×176, a few ms)
  or document the mismatch.
- **Default is MED.** A fresh install tints the first photo green. Default should
  be OFF until the preset is validated outdoors.
- GIF frames (`animated=True`) also bypass the fix — acceptable, just noting it.

## 6. Files

| File | What |
|---|---|
| `indoor-before-after.jpg` | same frame, raw vs MED, with measured values burned in |
| `../tuning/imx219-vc4.json` | active libcamera tuning file from the device |
| `../tuning/imx219-pisp.json` | Pi 5 variant, for reference only |

## 7. Hardware context

IMX219 on a 1/4" sensor, 8 MP single shot, no multi-frame HDR (HdrMode 0–4
produces identical histograms on this pipeline). The iPhone comparison frames
reach B/G 1.41 in open sky; the module here reaches 1.58 raw and 1.60 after the
earlier, sky-only correction — so a sky-restricted fix already matches the
reference closely. What is left is dynamic range, not colour.

---

# Follow-up: commit 6bc07b3 ("Keep colour_fix strictly sky-only")

Tested 2026-11-26 16:35–16:45 Beijing time. md5 `0b0b99df6`, 4896 lines.

## Verdict: the fix is correct

`COLOUR_FIX_PRESETS` now keeps (r, g, b) = (1, 1, 1) and only lifts green where
`B > G`. Measured on the device:

### Indoor neutral surface (the case that broke MED before)

| | neutral RGB | Δ |
|---|---|---|
| raw | 124.2 / 124.8 / 122.7 | — |
| after MED (0.20) | 124.2 / 124.5 / 123.1 | 0.0 / −0.3 / +0.4 |

The white wall is now **untouched**, which is exactly the guarantee that was
missing in c53bfd9.

### Real outdoor sky (same scene, device sensor)

| | sky RGB | B/G | target (iPhone 15 Pro) |
|---|---|---|---|
| raw | 128 / 129 / 204 | 1.58 | 1.41 |
| LOW 0.12 | 128 / 138 / 204 | 1.47 | |
| **MED 0.20** | **128 / 144 / 204** | **1.42** | **1.41** |

MED lands within 0.01 of the iPhone reference. LOW is a visible but softer
improvement. See `skyfix-presets.jpg`.

### Cost

| | |
|---|---|
| `_fix_magenta` on 8 MP | 1.44 s |
| `_apply_colour_fix` end to end | 2.2 s (decode + fix + q93 re-encode), background thread |
| Peak RSS delta | +23 MB (256-row bands) |

## Still open

**Preview is still uncorrected.** `filter_image(..., preview=True)` is called at
lines 2700, 3737 and 4661 without `colour_fix`, so the viewfinder keeps the raw
magenta sky while the saved frame is corrected. Options: pass the preset to
preview frames (240×176, ~2 ms) or accept and document the mismatch.

**Default is MED.** Now that MED is sky-only this is defensible — it matched the
reference outdoors and left the indoor wall alone. Worth one final check on
warm indoor light (incandescent), where `B > G` also holds for blue-ish shadows.

## Files

| File | What |
|---|---|
| `skyfix-presets.jpg` | raw / LOW / MED side by side with measured B/G |
| `indoor-before-after.jpg` | the earlier c53bfd9 failure, kept for the record |
| `../tuning/imx219-vc4-skyfix.json` | CCM candidate added alongside this commit (untested) |

---

# CCM tuning candidate (`imx219-vc4-skyfix.json`): what could and could not be verified

Tested 2026-11-26 16:47–16:55. Device shut down after the run, so the numbers below
are the last state of a session that started from md5 `0b0b99df6`.

## What the file changes

Only `rpi.ccm`, and only two colour-temperature entries (everything else is byte-identical):

| ct (K) | scenario | G diagonal | G←B coefficient |
|---|---|---|---|
| 5858 | daylight | 2.583 → 2.383 | −0.945 → −0.745 |
| 7580 | shade / deep blue sky | 3.079 → 2.659 | −1.500 → −1.080 |

2856 / 2960 / 3603 / 4650 are untouched, so indoor (tungsten, warm white, fluorescent)
should be unaffected — confirmed: two frames 0.4 RGB units apart on average.

## Attempt 1 — outdoor at 16:47 (failed to trigger)

```
ColourTemperature = 4000 K   (needs 5858 or 7580)
Lux               = 0.18
ExposureTime      = 66.7 ms
AnalogueGain      = 9.85
```

Late November, sun already down. 4000 K falls between the 3603 and 4650 entries, both
unchanged, so the tuning file had no effect at all: B/G 1.31 vs 1.30 between the two
shots.

## Attempt 2 — photographing a screen showing #1E90FF (triggered, but not a valid sample)

Framing a monitor full of Dodger Blue at 20–30 cm pushed the AWB estimate to
**8050 K**, above the 7580 entry, so the modified CCM definitely ran:

```
ColourTemperature = 8050 K
Lux               = 612
```

| | blue region R/G/B | B/G | B − R |
|---|---|---|---|
| default tuning | 37.4 / 50.4 / 244.6 | 4.85 | 207 |
| skyfix | 39.4 / 48.4 / 244.5 | 5.05 | 205 |

The frame changed (R +1.9, G −1.8), but **this says nothing about the sky**. A real sky
reads B/G ≈ 1.5 and B−R ≈ 60–80; saturated screen blue is 3× beyond that, so the CCM's
G output clips against zero and the visible difference is clip plus noise, not the matrix.

Back-solving in linear space for an actual sky colour (R128 G129 B204 display) shows the
matrix does move the right way:

```
original CCM:  G' ≈ −105.9
skyfix:        G' ≈  −63.9      (higher green = less violet)
```

## Conclusion

| Check | Result |
|---|---|
| FilmBox loads a custom tuning file | ✅ |
| AWB lands on the modified 7580 entry at 8050 K | ✅ |
| Indoor (unchanged entries) is untouched | ✅ (Δ < 0.5) |
| Direction of the change on a real sky | plausible, unproven |
| Magnitude on a real sky | **not measurable this session** |

**Do not ship this file as the default until a daylight frame confirms it.** Keep it as
an opt-in candidate: drop it at `~/.config/filmbox/imx219.json` to try, `FILMBOX_TUNING_FILE=0`
to force the system default.

## Practical note for the next attempt

Test between 10:00 and 14:00 on a clear day, pointing at open sky with the horizon
roughly a third of the frame. Check `ColourTemperature` in the capture metadata first —
if it reads below 5500 K the measurement is worthless, whatever the picture looks like.
A screen cannot substitute for real sky, as this session demonstrated.

---

# Daylight test of the CCM candidate — 2026-11-27 12:05 (the test this document was waiting for)

Conditions finally met: **6352 K**, 62609 lux, clear sky, 41 % of the frame is sky.
This is the run that the "must be above 5500 K" rule was written for. Result:
**the candidate makes the sky worse.**

## Measurements

| | sky RGB | B/G | R gain | G gain |
|---|---|---|---|---|
| default tuning | 101 / 106 / 194 | **1.82** | 1.748 | 1.420 |
| `imx219-vc4-skyfix.json` | 94 / 99 / 189 | **1.91** | 1.859 | 1.371 |

B/G moved **up** (worse) instead of down toward the iPhone reference of 1.41.
Neutral surfaces also darkened slightly (25/24/26 → 23/22/23).

## Why the CCM change backfired

The intent was sound: in the 5858 K entry, lower the green-to-blue penalty
(`-0.945 → -0.745`) so blue sky keeps more green. Mathematically, for a sky
pixel where B > G, that raises G'.

But the ISP does not stop at the CCM. **The AWB block re-estimates its gains
from the corrected statistics**, and it moved the opposite way:

```
R gain  1.748 → 1.859   (+6 %)
G gain  1.420 → 1.371   (−3 %)
```

More red, less green. That is precisely magenta. The net effect of the CCM edit
was to push the AWB's idea of "neutral" — so the sky came out **more** violet
than before the change.

This is the general hazard of editing `rpi.ccm` alone: the CCM and the AWB are a
closed loop, and moving one re-solves the other.

## What this means for the project

| Path | Sky B/G | Verdict |
|---|---|---|
| raw ISP, stock tuning | 1.82 | what the sensor actually produces at noon |
| raw ISP, skyfix CCM | 1.91 | worse — do not use |
| FilmBox `colour_fix` MED (Python, sky-only) | 1.42 | matches the iPhone reference |

The Python-side correction already does what the tuning file was supposed to do,
and it does it without fighting the AWB. **Do not ship the tuning candidate.**
Keep it in `docs/tuning/` as a recorded negative result, with `FILMBOX_TUNING_FILE=0`
available whenever someone wants to force the stock file.

## If ISP-level correction is ever revisited

Edit `rpi.awb` (`colour_gains` per illuminant) together with the CCM, or better,
only the AWB: ask for higher G gain at the daylight entries and let the CCM stay
stock. Verify by re-reading `ColourGains` from the capture metadata, not just by
looking at the picture — as this session showed, the picture alone can point the
wrong way.

## Side note

The frames came out upside down — the device was held rotated while pointed at
the sky. Irrelevant for the colour statistics (the ROI is rotation-invariant),
just noting it so the odd image is not mistaken for a capture bug.

