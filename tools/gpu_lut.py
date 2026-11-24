#!/usr/bin/env python3
"""GPU 3D-LUT prototype: ES3 + sampler3D + tiled 8 MP round trip."""
import ctypes
import os
import time

os.environ["EGL_PLATFORM"] = "surfaceless"

egl = ctypes.CDLL("libEGL.so.1")
gl = ctypes.CDLL("libGLESv2.so.2")
egl.eglGetDisplay.restype = ctypes.c_void_p
egl.eglQueryString.restype = ctypes.c_char_p
gl.glGetString.restype = ctypes.c_char_p

EGL_SURFACE_TYPE, EGL_RENDERABLE_TYPE, EGL_NONE = 0x3033, 0x3040, 0x3038
EGL_OPENGL_ES3_BIT, EGL_OPENGL_ES_API, EGL_CTX_VER = 0x0004, 0x30A0, 0x3098
GL_RGB, GL_RGBA, GL_UNSIGNED_BYTE, GL_TEXTURE_2D = 0x1907, 0x1908, 0x1401, 0x0DE1
GL_TEXTURE_3D, GL_TEXTURE0, GL_TEXTURE1 = 0x806F, 0x84C0, 0x84C1
GL_LINEAR, GL_CLAMP = 0x2601, 0x812F
GL_TEXTURE_MIN_FILTER, GL_TEXTURE_MAG_FILTER = 0x2801, 0x2800
GL_TEXTURE_WRAP_S, GL_TEXTURE_WRAP_T, GL_TEXTURE_WRAP_R = 0x2802, 0x2803, 0x8072
GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_FRAMEBUFFER_COMPLETE = 0x8D40, 0x8CE0, 0x8CD5
GL_FLOAT, GL_TRIANGLE_STRIP = 0x1406, 0x0005


def init():
    dpy = ctypes.c_void_p(egl.eglGetDisplay(ctypes.c_void_p(0)))
    maj, mnr = ctypes.c_int(), ctypes.c_int()
    assert egl.eglInitialize(dpy, ctypes.byref(maj), ctypes.byref(mnr))
    attrs = (ctypes.c_int * 5)(EGL_RENDERABLE_TYPE, EGL_OPENGL_ES3_BIT, EGL_NONE, EGL_NONE, EGL_NONE)
    cfg, n = ctypes.c_void_p(), ctypes.c_int()
    assert egl.eglChooseConfig(dpy, attrs, ctypes.byref(cfg), 1, ctypes.byref(n)), "no ES3 config"
    egl.eglBindAPI(EGL_OPENGL_ES_API)
    cattrs = (ctypes.c_int * 3)(EGL_CTX_VER, 2, EGL_NONE)
    ctx = ctypes.c_void_p(egl.eglCreateContext(dpy, cfg, ctypes.c_void_p(0), cattrs))
    assert egl.eglMakeCurrent(dpy, ctypes.c_void_p(0), ctypes.c_void_p(0), ctx), "makeCurrent"
    return gl.glGetString(0x1F02).decode()


VS = """#version 100
attribute vec2 pos; varying vec2 vUv;
void main(){ vUv = pos * 0.5 + 0.5; gl_Position = vec4(pos, 0.0, 1.0); }"""

# 33^3 cube packed as one 1089x33 atlas: x = b*33 + r, y = g.
# r/g interpolate in hardware; b is mixed between two slices.
FS = """#version 100
precision highp float;
varying vec2 vUv;
uniform sampler2D tex;
uniform sampler2D lut;
uniform float n;
void main(){
  vec3 c = texture2D(tex, vUv).rgb;
  float b = clamp(c.b, 0.0, 1.0) * (n - 1.0);
  float b0 = floor(b);
  float b1 = min(n - 1.0, b0 + 1.0);
  float mixb = b - b0;
  vec2 rg = vec2(c.r * (n - 1.0) + 0.5, c.g * (n - 1.0) + 0.5);
  vec3 s0 = texture2D(lut, vec2((b0 * n + rg.x) / (n * n), rg.y / n)).rgb;
  vec3 s1 = texture2D(lut, vec2((b1 * n + rg.x) / (n * n), rg.y / n)).rgb;
  gl_FragColor = vec4(mix(s0, s1, mixb), 1.0);
}"""


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
        raise SystemExit("compile: " + log.value.decode())
    return s


def load_cube(path):
    size, vals = None, []
    for line in open(path, errors="ignore"):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        u = s.upper()
        if u.startswith("LUT_3D_SIZE"):
            size = int(s.split()[1]); continue
        if u[0].isalpha():
            continue
        try:
            vals += [float(x) for x in s.split()]
        except ValueError:
            pass
    return size, vals


def main():
    print(init())
    maxt = ctypes.c_int()
    gl.glGetIntegerv(0x0D33, ctypes.byref(maxt))
    print("MAX_TEX", maxt.value)

    prog = gl.glCreateProgram()
    gl.glAttachShader(prog, shader(0x8B31, VS))
    gl.glAttachShader(prog, shader(0x8B30, FS))
    gl.glLinkProgram(prog)
    gl.glUseProgram(prog)

    # 3D LUT texture
    size, vals = load_cube(os.path.expanduser(
        "~/.config/filmbox/luts/fuji_classic_chrome.cube"))
    # cube order is r fastest, then g, then b -> reshape to [b][g][r], transpose to atlas
    import numpy as np
    arr = np.array(vals, dtype=np.float32).reshape(size, size, size, 3)
    atlas = arr.transpose(1, 0, 2, 3).reshape(size, size * size, 3)
    lut8 = (np.clip(atlas, 0, 1) * 255 + 0.5).astype(np.uint8).tobytes()
    lt = ctypes.c_uint()
    gl.glGenTextures(1, ctypes.byref(lt))
    gl.glActiveTexture(GL_TEXTURE1)
    gl.glBindTexture(GL_TEXTURE_2D, lt)
    for p in (GL_TEXTURE_MIN_FILTER, GL_TEXTURE_MAG_FILTER):
        gl.glTexParameteri(GL_TEXTURE_2D, p, GL_LINEAR)
    for p in (GL_TEXTURE_WRAP_S, GL_TEXTURE_WRAP_T):
        gl.glTexParameteri(GL_TEXTURE_2D, p, GL_CLAMP)
    t = time.perf_counter()
    gl.glTexImage2D(GL_TEXTURE_2D, 0, GL_RGB, size * size, size, 0, GL_RGB,
                    GL_UNSIGNED_BYTE, ctypes.c_char_p(lut8))
    gl.glFinish()
    print("LUT %d^3 atlas %dx%d upload: %.3fs" % (size, size*size, size, time.perf_counter() - t))
    gl.glUniform1i(gl.glGetUniformLocation(prog, b"lut"), 1)
    gl.glUniform1f(gl.glGetUniformLocation(prog, b"n"), ctypes.c_float(size))

    # tiled 8 MP: 2x2 blocks of 1640x1232
    TW, TH = 1640, 1232
    tex, fb = ctypes.c_uint(), ctypes.c_uint()
    gl.glGenTextures(1, ctypes.byref(tex))
    gl.glActiveTexture(GL_TEXTURE0)
    gl.glBindTexture(GL_TEXTURE_2D, tex)
    for p in (GL_TEXTURE_MIN_FILTER, GL_TEXTURE_MAG_FILTER):
        gl.glTexParameteri(GL_TEXTURE_2D, p, GL_LINEAR)
    gl.glTexImage2D(GL_TEXTURE_2D, 0, GL_RGB, TW, TH, 0, GL_RGB, GL_UNSIGNED_BYTE, None)
    gl.glGenFramebuffers(1, ctypes.byref(fb))
    gl.glBindFramebuffer(GL_FRAMEBUFFER, fb)
    gl.glFramebufferTexture2D(GL_FRAMEBUFFER, GL_COLOR_ATTACHMENT0, GL_TEXTURE_2D, tex, 0)
    print("FBO complete:", gl.glCheckFramebufferStatus(GL_FRAMEBUFFER) == GL_FRAMEBUFFER_COMPLETE)
    gl.glUniform1i(gl.glGetUniformLocation(prog, b"tex"), 0)
    gl.glViewport(0, 0, TW, TH)
    quad = (ctypes.c_float * 8)(-1, -1, 1, -1, -1, 1, 1, 1)
    loc = gl.glGetAttribLocation(prog, b"pos")
    gl.glVertexAttribPointer(loc, 2, GL_FLOAT, 0, 0, quad)
    gl.glEnableVertexAttribArray(loc)

    block = bytes(TW * TH * 3)
    out = ctypes.create_string_buffer(TW * TH * 4)
    t_all = time.perf_counter()
    for _ in range(4):
        gl.glTexImage2D(GL_TEXTURE_2D, 0, GL_RGB, TW, TH, 0, GL_RGB, GL_UNSIGNED_BYTE,
                        ctypes.c_char_p(block))
        gl.glDrawArrays(GL_TRIANGLE_STRIP, 0, 4)
        gl.glReadPixels(0, 0, TW, TH, GL_RGBA, GL_UNSIGNED_BYTE, out)
    gl.glFinish()
    total = time.perf_counter() - t_all
    print("4 blocks (8.08 MP) LUT round-trip: %.3fs total, %.3fs per block"
          % (total, total / 4))


if __name__ == "__main__":
    main()
