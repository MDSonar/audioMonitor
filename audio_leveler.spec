# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for Audio Auto-Leveler.

Usage:  pyinstaller audio_leveler.spec
Output: dist/AudioLeveler/AudioLeveler.exe
"""

import os
import sys
import glob

# ── Locate VLC ──────────────────────────────────────────────────
VLC_DIR = None
for candidate in [
    os.path.join(os.environ.get("ProgramFiles", ""), "VideoLAN", "VLC"),
    os.path.join(os.environ.get("ProgramFiles(x86)", ""), "VideoLAN", "VLC"),
]:
    if os.path.isfile(os.path.join(candidate, "libvlc.dll")):
        VLC_DIR = candidate
        break

if not VLC_DIR:
    raise FileNotFoundError(
        "VLC not found. Install 64-bit VLC or set ProgramFiles path.")

# Collect VLC DLLs (top-level)
vlc_dlls = [(f, ".") for f in glob.glob(os.path.join(VLC_DIR, "*.dll"))]

# Collect VLC plugins (preserve subdirectory structure)
vlc_plugins = []
plugins_dir = os.path.join(VLC_DIR, "plugins")
for root, dirs, files in os.walk(plugins_dir):
    for fname in files:
        src = os.path.join(root, fname)
        rel = os.path.relpath(root, VLC_DIR)
        vlc_plugins.append((src, rel))

# ── Locate ffmpeg from imageio-ffmpeg ───────────────────────────
import imageio_ffmpeg
ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
ffmpeg_dir = os.path.dirname(ffmpeg_exe)
ffmpeg_bins = [(f, "ffmpeg") for f in glob.glob(os.path.join(ffmpeg_dir, "*"))]

# ── Locate ffprobe (may be next to ffmpeg) ──────────────────────
ffprobe_bins = []
for f in glob.glob(os.path.join(ffmpeg_dir, "*ffprobe*")):
    ffprobe_bins.append((f, "ffmpeg"))

# ── Read version from source ───────────────────────────────────
version = "0.0.0"
with open("audio_monitor.py", "r", encoding="utf-8") as fh:
    for line in fh:
        if line.startswith("__version__"):
            version = line.split("=")[1].strip().strip('"').strip("'")
            break

# ── Analysis ───────────────────────────────────────────────────
a = Analysis(
    ["audio_monitor.py"],
    pathex=[],
    binaries=vlc_dlls + vlc_plugins + ffmpeg_bins + ffprobe_bins,
    datas=[],
    hiddenimports=[
        "numpy",
        "matplotlib",
        "matplotlib.backends.backend_tkagg",
        "tkinter",
        "tkinter.ttk",
        "vlc",
        "imageio_ffmpeg",
        "PIL",
        "PIL._tkinter_finder",
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "pytest", "IPython", "notebook", "sphinx",
        "cv2", "scipy", "pandas",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AudioLeveler",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,                 # windowed app, no console
    icon=None,                     # TODO: add .ico file
    version_info=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="AudioLeveler",
)
