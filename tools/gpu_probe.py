#!/usr/bin/env python3
"""Minimal EGL/GLES2 probe + 8 MP round-trip benchmark (ctypes, no deps)."""
import ctypes
import os
import time

os.environ["EGL_PLATFORM"] = "surfaceless"

egl = ctypes.CDLL("libEGL.so.1")
gl = ctypes.CDLL("libGLESv2.so.2")

egl.eglGetDisplay.restype = ctypes.c_void_p
egl.eglQueryString.restype = ctypes.c_char_p
gl.glGetString.restype = ctypes.c_char_p

EGL_SURFACE_TYPE, EGL_RENDERABLE_TYPE = 0x3033, 0x3040
EGL_OPENGL_ES2_BIT, EGL_NONE = 0x0004, 0x3038
EGL_CONTEXT_CLIENT_VERSION = 0x3098
EGL_OPENGL_ES_API = 0x30A0

dpy = ctypes.c_void_p(egl.eglGetDisplay(ctypes.c_void_p(0)))
maj, mnr = ctypes.c_int(), ctypes.c_int()
if not egl.eglInitialize(dpy, ctypes.byref(maj), ctypes.byref(mnr)):
    raise SystemExit("eglInitialize failed")
print("EGL", maj.value, mnr.value, egl.eglQueryString(dpy, 0x3053).decode())

attrs = (ctypes.c_int * 7)(EGL_SURFACE_TYPE, 0, EGL_RENDERABLE_TYPE, EGL_OPENGL_ES2_BIT,
                           EGL_NONE, EGL_NONE, EGL_NONE)
cfg = ctypes.c_void_p()
n = ctypes.c_int()
if not egl.eglChooseConfig(dpy, attrs, ctypes.byref(cfg), 1, ctypes.byref(n)):
    raise SystemExit("eglChooseConfig failed")
egl.eglBindAPI(EGL_OPENGL_ES_API)
cattrs = (ctypes.c_int * 3)(EGL_CONTEXT_CLIENT_VERSION, 2, EGL_NONE)
ctx = ctypes.c_void_p(egl.eglCreateContext(dpy, cfg, ctypes.c_void_p(0), cattrs))
if not egl.eglMakeCurrent(dpy, ctypes.c_void_p(0), ctypes.c_void_p(0), ctx):
    raise SystemExit("eglMakeCurrent failed")

print("GL_VERSION:", gl.glGetString(0x1F02).decode())
print("GL_RENDERER:", gl.glGetString(0x1F01).decode())
maxt = ctypes.c_int()
gl.glGetIntegerv(0x0D33, ctypes.byref(maxt))
print("GL_MAX_TEXTURE_SIZE:", maxt.value)

W, H = 3280, 2464
tex = ctypes.c_uint()
gl.glGenTextures(1, ctypes.byref(tex))
gl.glBindTexture(0x0DE1, tex)
gl.glTexParameteri(0x0DE1, 0x2801, 0x2601)
gl.glTexParameteri(0x0DE1, 0x2802, 0x812F)
gl.glTexParameteri(0x0DE1, 0x2803, 0x812F)
data = bytes(W * H * 3)
t = time.perf_counter()
gl.glTexImage2D(0x0DE1, 0, 0x1908, W, H, 0, 0x1908, 0x1401,
                ctypes.c_char_p(data))
gl.glFinish()
print("upload 8MP RGB %.0f MB: %.3fs" % (len(data) / 1e6, time.perf_counter() - t))

src = """#version 100
precision highp float;
varying vec2 vUv;
uniform sampler2D tex;
void main() { gl_FragColor = texture2D(tex, vUv) * 0.98; }
"""
vs = """#version 100
attribute vec2 pos;
varying vec2 vUv;
void main() { vUv = pos * 0.5 + 0.5; gl_Position = vec4(pos, 0.0, 1.0); }
"""


def shader(kind, code):
    s = gl.glCreateShader(kind)
    buf = ctypes.c_char_p(code.encode())
    arr = (ctypes.c_char_p * 1)(buf)
    gl.glShaderSource(s, 1, arr, None)
    gl.glCompileShader(s)
    ok = ctypes.c_int()
    gl.glGetShaderiv(s, 0x8B81, ctypes.byref(ok))
    if not ok.value:
        log = ctypes.create_string_buffer(1024)
        gl.glGetShaderInfoLog(s, 1024, None, log)
        raise SystemExit("compile: " + log.value.decode())
    return s


prog = gl.glCreateProgram()
gl.glAttachShader(prog, shader(0x8B31, vs))
gl.glAttachShader(prog, shader(0x8B30, src))
gl.glLinkProgram(prog)
gl.glUseProgram(prog)

fb = ctypes.c_uint()
gl.glGenFramebuffers(1, ctypes.byref(fb))
gl.glBindFramebuffer(0x8D40, fb)
gl.glFramebufferTexture2D(0x8D40, 0x8CE0, 0x0DE1, tex, 0)
print("FBO complete:", gl.glCheckFramebufferStatus(0x8D40) == 0x8CD5)

gl.glViewport(0, 0, W, H)
quad = (ctypes.c_float * 8)(-1, -1, 1, -1, -1, 1, 1, 1)


def draw():
    loc = gl.glGetAttribLocation(prog, b"pos")
    gl.glVertexAttribPointer(loc, 2, 0x1406, 0, 0, quad)
    gl.glEnableVertexAttribArray(loc)
    gl.glDrawArrays(0x0005, 0, 4)


t = time.perf_counter()
for _ in range(3):
    draw()
    gl.glFinish()
print("3 passes 8MP shader: %.3fs each" % ((time.perf_counter() - t) / 3))

out = ctypes.create_string_buffer(W * H * 4)
t = time.perf_counter()
gl.glReadPixels(0, 0, W, H, 0x1908, 0x1401, out)
gl.glFinish()
print("readPixels 8MP RGBA %.0f MB: %.3fs" % (len(out) / 1e6, time.perf_counter() - t))
