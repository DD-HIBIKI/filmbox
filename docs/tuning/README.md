# IMX219 libcamera tuning files (copied from the device, 2026-11-26)

| File | Source on device | Target | Size | md5 |
|---|---|---|---|---|
| `imx219-vc4.json` | `/usr/share/libcamera/ipa/rpi/vc4/imx219.json` | `bcm2835` | 35 KB | `46a72401fcdedcc96c10521c5c066b8f` |
| `imx219-vc4-skyfix.json` | Candidate patch based on `imx219-vc4.json` (daylight `rpi.ccm` only) | `bcm2835` | 35 KB | — |
| `imx219-pisp.json` | `/usr/share/libcamera/ipa/rpi/pisp/imx219.json` | `pisp` | 87 KB | `63148cc4dcb46ef923d7c283cd10364f` |

Both stock files come from package `libcamera-ipa`. The Cardputer Zero uses the **vc4** file
(libcamera logs `Using tuning file /usr/share/libcamera/ipa/rpi/vc4/imx219.json`);
the pisp variant is included only for reference.

## `imx219-vc4-skyfix.json` (daylight sky-only CCM candidate)

In stock `imx219-vc4.json`, `rpi.ccm` keeps `G_row`'s blue subtraction around `-0.83..-1.06`
for indoor illuminants (`2860–4650 K`), then jumps sharply to **`-1.50047`** at `7580 K`
(open blue sky). That leaves neutral grey/white (`R=G=B`) untouched because the row sums
to `1.0000`, but suppresses green on blue sky (`B > G`), turning sky violet.

`imx219-vc4-skyfix.json` leaves `2860 / 2960 / 3603 / 4650 K` byte-for-byte identical
(so indoor white walls stay neutral) and only moderates the blue-into-green cross-term
at daylight/sky colour temperatures while keeping every row sum strictly `1.00000`:

- `ct: 5858`: `G_row` `[-0.63821, 2.58348, -0.94527]` → `[-0.63821, 2.38348, -0.74527]`
- `ct: 7580`: `G_row` `[-0.57875, 3.07922, -1.50047]` → `[-0.57875, 2.65922, -1.08047]`

To test on Cardputer Zero (disable software `colour_fix` to avoid double correction):

```bash
mkdir -p ~/.config/filmbox
cp docs/tuning/imx219-vc4-skyfix.json ~/.config/filmbox/imx219.json
filmbox diagnose
filmbox photo --colour-fix 0
```

## Structure

Top level: `version`, `target`, `algorithms` (15 entries):

```
rpi.black_level  rpi.dpc  rpi.lux  rpi.noise  rpi.geq  rpi.sdn
rpi.awb  rpi.nn.awb  rpi.agc  rpi.alsc  rpi.contrast  rpi.ccm
rpi.sharpen  rpi.hdr  rpi.sync
```

Colour lives in two of them:

- **`rpi.awb`** — per-illuminant `ct_lo`/`ct_hi` blocks with `colour_gains`
  (r, g, b). Biasing these fixes the sky cast at capture time.
- **`rpi.ccm`** — colour correction matrix, per illuminant.

## How to try a modified copy

FilmBox (commit c53bfd9) looks for a custom file at, in order:

1. `$FILMBOX_TUNING_FILE`
2. `$LIBCAMERA_RPI_TUNING_FILE`
3. `~/.config/filmbox/imx219.json`
4. `<app_dir>/tuning/imx219.json`, `<app_dir>/imx219.json`
5. `/usr/share/filmbox/imx219.json`

Set `FILMBOX_TUNING_FILE=0` to force the system default. A malformed file is
logged and ignored, so capture never breaks.

Note: the file must be a JSON **object**. Keep the basename `imx219.json` — the
loader resolves sibling includes by name.
