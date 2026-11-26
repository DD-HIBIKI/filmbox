# IMX219 libcamera tuning files (copied from the device, 2026-11-26)

| File | Source on device | Target | Size | md5 |
|---|---|---|---|---|
| `imx219-vc4.json` | `/usr/share/libcamera/ipa/rpi/vc4/imx219.json` | `bcm2835` | 35 KB | `46a72401fcdedcc96c10521c5c066b8f` |
| `imx219-pisp.json` | `/usr/share/libcamera/ipa/rpi/pisp/imx219.json` | `pisp` | 87 KB | `63148cc4dcb46ef923d7c283cd10364f` |

Both come from package `libcamera-ipa.` The Cardputer Zero uses the **vc4** file
(libcamera logs `Using tuning file /usr/share/libcamera/ipa/rpi/vc4/imx219.json`);
the pisp variant is included only for reference.

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
