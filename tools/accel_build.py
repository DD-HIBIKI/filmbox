"""Two accelerators for the film-copy pipeline, inserted into cybercam.py.

- MultiCoreFilm : fork N workers, split the frame into bands (LUT pass and
                  grain/glow pass), no GPU needed, works everywhere.
- GpuFilm       : EGL surfaceless + GLES2 fragment shader doing 3D LUT,
                  halation screen and grain in one pass, tiled to the max
                  texture size (2048 on this device).

Both expose apply(image, cube_path, halation=True, grain=5.0) -> PIL Image.
Nothing here imports Pillow-only helpers from the app, so it can be tested
standalone:  python3 accel_build.py --selftest
"""
import ctypes
import os
import struct
import sys
import time

from PIL import Image, ImageChops, ImageFilter

try:
    import numpy as np
except Exception:                      # pragma: no cover
    np = None


# ---------------------------------------------------------------- cube data
def read_cube(path):
    """(size, bytes RGB8) for a .cube file, cached by mtime."""
    stamp = os.path.getmtime(path)
    key = (path, stamp)
    hit = _CUBE_CACHE.get(key)
    if hit:
        return hit
    size, vals = None, []
    dmin, dmax = (0.0, 0.0, 0.0), (1.0, 1.0, 1.0)
    with open(path, errors="ignore") as handle:
        for line in handle:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            u = s.upper()
            if u.startswith("LUT_3D_SIZE"):
                size = int(s.split()[1])
                continue
            if u.startswith("DOMAIN_MIN"):
                dmin = tuple(float(x) for x in s.split()[1:4])
                continue
            if u.startswith("DOMAIN_MAX"):
                dmax = tuple(float(x) for x in s.split()[1:4])
                continue
            if u[0].isalpha():
                continue
            try:
                vals += [float(x) for x in s.split()]
            except ValueError:
                pass
    if size is None or len(vals) != size ** 3 * 3:
        raise ValueError("bad cube: " + path)
    if dmin != (0.0, 0.0, 0.0) or dmax != (1.0, 1.0, 1.0):
        norm = []
        for i in range(0, len(vals), 3):
            for c in range(3):
                span = (dmax[c] - dmin[c]) or 1.0
                norm.append(min(1.0, max(0.0, (vals[i + c] - dmin[c]) / span)))
        vals = norm
    if np is not None:
        arr = np.array(vals, dtype=np.float32).reshape(size, size, size, 3)
        data = (np.clip(arr, 0, 1) * 255 + 0.5).astype(np.uint8).tobytes()
        lut3 = arr
    else:
        data = bytes(min(255, max(0, int(v * 255 + 0.5))) for v in vals)
        lut3 = None
    out = (size, data, lut3)
    _CUBE_CACHE[key] = out
    return out


_CUBE_CACHE = {}


# ------------------------------------------------------------- glow (shared)
def build_glow(image, threshold=210, strength=0.6, monochrome=None):
    """1/4-scale highlight glow, neutral on B&W stocks and warm on color."""
    rgb = image if image.mode == "RGB" else image.convert("RGB")
    w, h = rgb.size
    sw, sh = max(1, w // 4), max(1, h // 4)
    small = rgb.resize((sw, sh), Image.Resampling.BOX)
    if monochrome is None and np is not None:
        arr = np.asarray(small, dtype=np.int16)
        monochrome = bool(np.max(np.abs(arr[:, :, 0] - arr[:, :, 1])) <= 1
                          and np.max(np.abs(arr[:, :, 1] - arr[:, :, 2])) <= 1)

    def ramp(v):
        if v < threshold:
            return 0
        return min(255, int((v - threshold) * 255 // (255 - threshold)))

    mask = small.convert("L").point(ramp)
    if monochrome:
        glow = Image.merge("RGB", (mask, mask, mask))
    else:
        glow = Image.merge("RGB", (mask,
                                   mask.point(lambda v: int(v * 0.72)),
                                   mask.point(lambda v: int(v * 0.42))))
    glow = glow.filter(ImageFilter.GaussianBlur(max(1.0, max(w, h) / 3200.0)))
    if strength != 1.0:
        glow = glow.point(lambda v: int(v * strength))
    return glow


# --------------------------------------------------------------------- GPU
GL_RGB, GL_RGBA, GL_UBYTE = 0x1907, 0x1908, 0x1401
GL_TEX2D, GL_NEAREST, GL_LINEAR, GL_CLAMP = 0x0DE1, 0x2600, 0x2601, 0x812F
GL_MIN_F, GL_MAG_F = 0x2801, 0x2800
GL_WRAP_S, GL_WRAP_T = 0x2802, 0x2803
GL_FBO, GL_COLOR_ATT, GL_FBO_OK = 0x8D40, 0x8CE0, 0x8CD5
GL_FLOAT, GL_STRIP = 0x1406, 0x0005
GL_TEXTURE0, GL_TEXTURE1 = 0x84C0, 0x84C1
GL_TEXTURE2 = 0x84C2

VS = """#version 100
attribute vec2 pos; varying vec2 vUv;
void main(){ vUv = pos * 0.5 + 0.5; gl_Position = vec4(pos, 0.0, 1.0); }"""

FS = """#version 100
precision highp float;
varying vec2 vUv;
uniform sampler2D tex;
uniform sampler2D lut;
uniform sampler2D glow;
uniform vec2 blockSize;      // pixels in this tile
uniform vec2 blockOrigin;    // tile offset in the full image, pixels
uniform vec2 imgSize;
uniform float n;             // LUT edge (13 or 33)
uniform float grainAmp;      // normalized grey amplitude (0..1)
uniform float glowMix;       // 0 disables halation
uniform vec2 uvScale;        // active content ratio vs allocated tile texture

float hash(vec2 p){
  vec2 q = mod(floor(p), 251.0);
  return fract((q.x * 17.31 + q.y * 43.17) * (q.x * 0.13 + q.y * 0.07 + 1.13));
}

void main(){
  vec2 uv = vUv * uvScale;
  vec3 c = texture2D(tex, uv).rgb;

  // 3D LUT from a 2D atlas: x = b*n + r, y = g
  float b = clamp(c.b, 0.0, 1.0) * (n - 1.0);
  float b0 = floor(b);
  float b1 = min(n - 1.0, b0 + 1.0);
  float mb = b - b0;
  vec2 rg = vec2(c.r * (n - 1.0) + 0.5, c.g * (n - 1.0) + 0.5);
  vec3 s0 = texture2D(lut, vec2((b0 * n + rg.x) / (n * n), rg.y / n)).rgb;
  vec3 s1 = texture2D(lut, vec2((b1 * n + rg.x) / (n * n), rg.y / n)).rgb;
  vec3 col = mix(s0, s1, mb);

  // halation: screen with a 1/4-scale glow texture
  if (glowMix > 0.0) {
    vec2 g = (blockOrigin + vUv * blockSize) / imgSize;
    vec3 gl = texture2D(glow, g).rgb * glowMix;
    col = 1.0 - (1.0 - col) * (1.0 - gl);
  }

  // grain: full-image pixel coordinates keep tiles seamless
  if (grainAmp > 0.0) {
    vec2 px = blockOrigin + vUv * blockSize;
    float n1 = (hash(px) + hash(px + vec2(17.0, 31.0))) * 0.5 * 2.0 - 1.0;
    float luma = dot(col, vec3(0.299, 0.587, 0.114));
    float w = clamp(1.35 - luma * 1.5, 0.15, 1.0);
    col += n1 * grainAmp * w;
  }
  gl_FragColor = vec4(clamp(col, 0.0, 1.0), 1.0);
}"""


class GpuFilm:
    """Lazy EGL/GLES2 renderer. Construction raises on unsupported devices."""

    def __init__(self):
        os.environ.setdefault("EGL_PLATFORM", "surfaceless")
        egl = ctypes.CDLL("libEGL.so.1")
        gl = ctypes.CDLL("libGLESv2.so.2")
        egl.eglGetDisplay.restype = ctypes.c_void_p
        egl.eglQueryString.restype = ctypes.c_char_p
        gl.glGetString.restype = ctypes.c_char_p

        dpy = ctypes.c_void_p(egl.eglGetDisplay(ctypes.c_void_p(0)))
        maj, mnr = ctypes.c_int(), ctypes.c_int()
        if not egl.eglInitialize(dpy, ctypes.byref(maj), ctypes.byref(mnr)):
            raise RuntimeError("eglInitialize")
        attrs = (ctypes.c_int * 5)(0x3040, 0x0004, 0x3038, 0x3038, 0x3038)
        cfg, num = ctypes.c_void_p(), ctypes.c_int()
        if not egl.eglChooseConfig(dpy, attrs, ctypes.byref(cfg), 1, ctypes.byref(num)):
            raise RuntimeError("eglChooseConfig")
        egl.eglBindAPI(0x30A0)
        ctx = ctypes.c_void_p(egl.eglCreateContext(
            dpy, cfg, ctypes.c_void_p(0), (ctypes.c_int * 3)(0x3098, 2, 0x3038)))
        if not ctx.value:
            raise RuntimeError("eglCreateContext")
        if not egl.eglMakeCurrent(dpy, ctypes.c_void_p(0), ctypes.c_void_p(0), ctx):
            raise RuntimeError("eglMakeCurrent")
        self.egl, self.gl = egl, gl
        u, i, f, v = ctypes.c_uint, ctypes.c_int, ctypes.c_float, ctypes.c_void_p
        gl.glTexImage2D.argtypes = [u, i, i, i, i, i, u, u, v]
        gl.glTexSubImage2D.argtypes = [u, i, i, i, i, i, u, u, v]
        gl.glReadPixels.argtypes = [i, i, i, i, u, u, v]
        gl.glDrawArrays.argtypes = [u, i, i]
        gl.glViewport.argtypes = [i, i, i, i]
        gl.glBindTexture.argtypes = [u, u]
        gl.glBindFramebuffer.argtypes = [u, u]
        gl.glFramebufferTexture2D.argtypes = [u, u, u, u, i]
        gl.glTexParameteri.argtypes = [u, u, i]
        gl.glPixelStorei.argtypes = [u, i]
        gl.glActiveTexture.argtypes = [u]
        gl.glUniform1i.argtypes = [i, i]
        gl.glUniform1f.argtypes = [i, f]
        gl.glUniform2f.argtypes = [i, f, f]
        gl.glGetUniformLocation.argtypes = [u, ctypes.c_char_p]
        gl.glGetUniformLocation.restype = i
        maxt = ctypes.c_int()
        gl.glGetIntegerv(0x0D33, ctypes.byref(maxt))
        self.maxt = maxt.value or 2048
        self.tile = min(self.maxt, 512)
        self.renderer = (gl.glGetString(0x1F01) or b"").decode(errors="ignore")
        self.prog = self._program()
        self.lut_tex = None
        self.lut_key = None
        self.src_tex, self.dst_tex, self.fb = ctypes.c_uint(), ctypes.c_uint(), ctypes.c_uint()
        gl.glGenTextures(1, ctypes.byref(self.src_tex))
        gl.glGenTextures(1, ctypes.byref(self.dst_tex))
        gl.glGenFramebuffers(1, ctypes.byref(self.fb))
        gl.glActiveTexture(GL_TEXTURE0)
        gl.glBindTexture(GL_TEX2D, self.src_tex)
        gl.glTexParameteri(GL_TEX2D, GL_MIN_F, GL_NEAREST)
        gl.glTexParameteri(GL_TEX2D, GL_MAG_F, GL_NEAREST)
        gl.glTexParameteri(GL_TEX2D, GL_WRAP_S, GL_CLAMP)
        gl.glTexParameteri(GL_TEX2D, GL_WRAP_T, GL_CLAMP)
        gl.glPixelStorei(0x0CF5, 1)
        gl.glTexImage2D(GL_TEX2D, 0, GL_RGB, self.tile, self.tile, 0, GL_RGB, GL_UBYTE, None)
        gl.glBindTexture(GL_TEX2D, self.dst_tex)
        gl.glTexParameteri(GL_TEX2D, GL_MIN_F, GL_NEAREST)
        gl.glTexParameteri(GL_TEX2D, GL_MAG_F, GL_NEAREST)
        gl.glTexParameteri(GL_TEX2D, GL_WRAP_S, GL_CLAMP)
        gl.glTexParameteri(GL_TEX2D, GL_WRAP_T, GL_CLAMP)
        gl.glTexImage2D(GL_TEX2D, 0, GL_RGBA, self.tile, self.tile, 0, GL_RGBA, GL_UBYTE, None)
        gl.glBindFramebuffer(GL_FBO, self.fb)
        gl.glFramebufferTexture2D(GL_FBO, GL_COLOR_ATT, GL_TEX2D, self.dst_tex, 0)
        self.uniforms = {}
        for name in (b"tex", b"lut", b"glow", b"blockSize", b"blockOrigin",
                     b"imgSize", b"n", b"grainAmp", b"glowMix", b"uvScale"):
            self.uniforms[name.decode()] = gl.glGetUniformLocation(self.prog, name)
        quad = (ctypes.c_float * 8)(-1, -1, 1, -1, -1, 1, 1, 1)
        self._quad = quad
        gl.glUseProgram(self.prog)
        loc = gl.glGetAttribLocation(self.prog, b"pos")
        gl.glVertexAttribPointer(loc, 2, GL_FLOAT, 0, 0, quad)
        gl.glEnableVertexAttribArray(loc)
        gl.glUniform1i(self.uniforms["tex"], 0)
        gl.glUniform1i(self.uniforms["lut"], 1)
        gl.glUniform1i(self.uniforms["glow"], 2)

    def _program(self):
        gl = self.gl

        def shader(kind, code):
            s = gl.glCreateShader(kind)
            arr = (ctypes.c_char_p * 1)(ctypes.c_char_p(code.encode()))
            gl.glShaderSource(s, 1, arr, None)
            gl.glCompileShader(s)
            ok = ctypes.c_int()
            gl.glGetShaderiv(s, 0x8B81, ctypes.byref(ok))
            if not ok.value:
                log = ctypes.create_string_buffer(2048)
                gl.glGetShaderInfoLog(s, 2048, None, log)
                raise RuntimeError("shader: " + log.value.decode(errors="ignore"))
            return s

        prog = gl.glCreateProgram()
        gl.glAttachShader(prog, shader(0x8B31, VS))
        gl.glAttachShader(prog, shader(0x8B30, FS))
        gl.glLinkProgram(prog)
        status = ctypes.c_int()
        gl.glGetProgramiv(prog, 0x8B82, ctypes.byref(status))
        if not status.value:
            log = ctypes.create_string_buffer(2048)
            gl.glGetProgramInfoLog(prog, 2048, None, log)
            raise RuntimeError("link: " + log.value.decode(errors="ignore"))
        return prog

    def _upload_lut(self, cube_path):
        key = (cube_path, os.path.getmtime(cube_path))
        if key == self.lut_key:
            return
        size, data, _ = read_cube(cube_path)
        gl = self.gl
        if self.lut_tex is None:
            self.lut_tex = ctypes.c_uint()
            gl.glGenTextures(1, ctypes.byref(self.lut_tex))
        gl.glActiveTexture(GL_TEXTURE1)
        gl.glBindTexture(GL_TEX2D, self.lut_tex)
        for p in (GL_MIN_F, GL_MAG_F):
            gl.glTexParameteri(GL_TEX2D, p, GL_LINEAR)
        for p in (GL_WRAP_S, GL_WRAP_T):
            gl.glTexParameteri(GL_TEX2D, p, GL_CLAMP)
        gl.glPixelStorei(0x0CF5, 1)
        gl.glTexImage2D(GL_TEX2D, 0, GL_RGB, size * size, size, 0, GL_RGB,
                        GL_UBYTE, ctypes.c_char_p(self._atlas(size, data)))
        self.lut_key = key
        self.lut_size = float(size)

    @staticmethod
    def _atlas(size, data):
        if np is None:
            return data
        arr = np.frombuffer(data, dtype=np.uint8).reshape(size, size, size, 3)
        return np.ascontiguousarray(arr.transpose(1, 0, 2, 3).reshape(size, size * size, 3)).tobytes()

    def apply(self, image, cube_path, halation=True, grain=5.0):
        rgb = image if image.mode == "RGB" else image.convert("RGB")
        w, h = rgb.size
        gl = self.gl
        self._upload_lut(cube_path)
        size, data, _ = read_cube(cube_path)
        lut_obj = ImageFilter.Color3DLUT(size, [v / 255.0 for v in struct.unpack("B" * (size ** 3 * 3), data)])
        glow = None
        if halation:
            sw, sh = max(1, w // 4), max(1, h // 4)
            glow = build_glow(rgb.resize((sw, sh), Image.Resampling.BOX).filter(lut_obj))
        if glow is not None:
            if not hasattr(self, "glow_tex"):
                self.glow_tex = ctypes.c_uint()
                gl.glGenTextures(1, ctypes.byref(self.glow_tex))
            gl.glActiveTexture(GL_TEXTURE2)
            gl.glBindTexture(GL_TEX2D, self.glow_tex)
            for p in (GL_MIN_F, GL_MAG_F):
                gl.glTexParameteri(GL_TEX2D, p, GL_LINEAR)
            for p in (GL_WRAP_S, GL_WRAP_T):
                gl.glTexParameteri(GL_TEX2D, p, GL_CLAMP)
            gl.glPixelStorei(0x0CF5, 1)
            gl.glTexImage2D(GL_TEX2D, 0, GL_RGB, glow.width, glow.height, 0,
                            GL_RGB, GL_UBYTE, ctypes.c_char_p(glow.tobytes()))

        gl.glUseProgram(self.prog)
        gl.glUniform1f(self.uniforms["n"], ctypes.c_float(self.lut_size))
        gl.glUniform1f(self.uniforms["grainAmp"], ctypes.c_float(max(0.0, grain) * 1.2 / 255.0))
        gl.glUniform1f(self.uniforms["glowMix"], ctypes.c_float(1.0 if glow is not None else 0.0))
        gl.glUniform2f(self.uniforms["imgSize"], ctypes.c_float(w), ctypes.c_float(h))

        out = Image.new("RGB", (w, h))
        tile = self.tile
        buf = ctypes.create_string_buffer(tile * tile * 4)
        gl.glBindFramebuffer(GL_FBO, self.fb)
        gl.glActiveTexture(GL_TEXTURE0)
        gl.glBindTexture(GL_TEX2D, self.src_tex)
        gl.glPixelStorei(0x0CF5, 1)
        for top in range(0, h, tile):
            rows = min(tile, h - top)
            for left in range(0, w, tile):
                cols = min(tile, w - left)
                raw = rgb.crop((left, top, left + cols, top + rows)).tobytes()
                gl.glTexSubImage2D(GL_TEX2D, 0, 0, 0, cols, rows, GL_RGB,
                                   GL_UBYTE, ctypes.c_char_p(raw))
                gl.glViewport(0, 0, cols, rows)
                gl.glUniform2f(self.uniforms["blockSize"],
                               ctypes.c_float(cols), ctypes.c_float(rows))
                gl.glUniform2f(self.uniforms["uvScale"],
                               ctypes.c_float(cols / float(tile)),
                               ctypes.c_float(rows / float(tile)))
                gl.glUniform2f(self.uniforms["blockOrigin"],
                               ctypes.c_float(left), ctypes.c_float(top))
                gl.glDrawArrays(GL_STRIP, 0, 4)
                gl.glReadPixels(0, 0, cols, rows, GL_RGBA, GL_UBYTE, buf)
                part = Image.frombytes("RGBA", (cols, rows), buf.raw[:cols * rows * 4]).convert("RGB")
                out.paste(part, (left, top))
        return out


# ------------------------------------------------------------- multicore
_MP = {}

def _band_lut(job):
    top, bottom = job
    img, lut = _MP["img"], _MP["lut"]
    return top, bottom, img.crop((0, top, img.width, bottom)).filter(lut).tobytes()


def _band_finish(job):
    top, bottom = job
    img = _MP["img"]
    glow = _MP["glow"]
    grain = _MP["grain"]
    rows = bottom - top
    block = img.crop((0, top, img.width, bottom))
    if glow is not None:
        sw, sh = glow.size
        band = glow.crop((0, top * sh // img.height, sw,
                          min(sh, max(top * sh // img.height + 1, bottom * sh // img.height))))
        band = band.resize((img.width, rows), Image.Resampling.NEAREST)
        block = ImageChops.screen(block, band)
    if grain and np is not None:
        rng = np.random.default_rng(1000 + top)
        arr = np.asarray(block, dtype=np.int16)
        n = rng.integers(-120, 121, (rows, img.width), dtype=np.int16)
        n += rng.integers(-120, 121, (rows, img.width), dtype=np.int16)
        n //= 2
        small = block.resize((max(1, img.width // 8), max(1, rows // 8)),
                             Image.Resampling.BOX)
        lw = np.asarray(small, dtype=np.uint16)
        luma = (lw[:, :, 0] * 77 + lw[:, :, 1] * 150 + lw[:, :, 2] * 29) >> 8
        weight = np.clip(345 - (luma.astype(np.int32) * 3) // 2, 38, 255).astype(np.uint8)
        wt = np.asarray(Image.fromarray(weight, "L").resize((img.width, rows),
                                                             Image.Resampling.BILINEAR),
                        dtype=np.int32)
        grain_arr = (((n.astype(np.int32) * wt) >> 8) * max(1, int(round(grain)))) // 100
        arr += grain_arr.astype(np.int16)[:, :, None]
        block = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")
    return top, bottom, block.tobytes()


class MultiCoreFilm:
    def __init__(self, jobs=None):
        self.jobs = jobs or min(4, os.cpu_count() or 1)

    def apply(self, image, cube_path, halation=True, grain=5.0):
        from PIL import Image as _I, ImageFilter as _F
        size, data, _ = read_cube(cube_path)
        lut_obj = _F.Color3DLUT(size, [v / 255.0 for v in
                                       struct.unpack("B" * (size ** 3 * 3), data)])
        import multiprocessing as mp
        rgb = image if image.mode == "RGB" else image.convert("RGB")
        w, h = rgb.size
        jobs = max(1, min(self.jobs, h // 256))
        if jobs < 2:
            return None
        bands = [(i * h // jobs, (i + 1) * h // jobs) for i in range(jobs)]
        ctx = mp.get_context("fork")
        _MP.clear()
        _MP.update(img=rgb, lut=lut_obj, glow=None, grain=grain)
        with ctx.Pool(jobs) as pool:
            parts = pool.map(_band_lut, bands)
        stage = Image.new("RGB", (w, h))
        for top, bottom, data in parts:
            stage.paste(Image.frombytes("RGB", (w, bottom - top), data), (0, top))
        if halation:
            _MP["img"] = stage
            _MP["glow"] = build_glow(stage)
        else:
            _MP["glow"] = None
        _MP["img"] = stage
        with ctx.Pool(jobs) as pool:
            parts = pool.map(_band_finish, bands)
        out = Image.new("RGB", (w, h))
        for top, bottom, data in parts:
            out.paste(Image.frombytes("RGB", (w, bottom - top), data), (0, top))
        _MP.clear()
        return out


# ------------------------------------------------------------- self test
def _selftest():
    src = sys.argv[2] if len(sys.argv) > 2 else None
    cube = sys.argv[3] if len(sys.argv) > 3 else os.path.expanduser(
        "~/.config/filmbox/luts/fuji_classic_chrome.cube")
    if not src:
        print("usage: accel_build.py --selftest <image> [cube]")
        return 1
    base = Image.open(src).convert("RGB")
    if max(base.size) > 2048:
        base = base.resize((1640, 1232), Image.Resampling.BOX) if base.width > base.height \
            else base.resize((1232, 1640))
    gpu = None
    try:
        gpu = GpuFilm()
        print("GPU:", gpu.renderer, "MAX_TEX", gpu.maxt)
    except Exception as exc:
        print("GPU unavailable:", exc)
    mc = MultiCoreFilm()
    print("CPUs:", mc.jobs)
    lut_obj = True
    if gpu:
        t = time.perf_counter()
        gpu.apply(base, cube, True, 5.0).save("/tmp/gpu_out.jpg", quality=93)
        print("GPU apply: %.2fs" % (time.perf_counter() - t))
    if lut_obj:
        t = time.perf_counter()
        out = mc.apply(base, cube, True, 5.0)
        if out is not None:
            out.save("/tmp/mc_out.jpg", quality=93)
            print("multi-core apply: %.2fs" % (time.perf_counter() - t))
    return 0


if __name__ == "__main__":
    sys.exit(_selftest())
