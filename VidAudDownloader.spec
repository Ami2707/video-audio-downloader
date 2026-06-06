# -*- mode: python ; coding: utf-8 -*-
#
# PyInstaller spec for VidAudDownloader  (onedir, windowed).
# Build it through build.ps1 — that script generates build\icon.ico first and,
# AFTER PyInstaller runs, drops the updatable lib\ (yt-dlp), runtime\ (Node +
# PO-token server) and ffmpeg\ next to the .exe.
#
# yt-dlp IS bundled here (not excluded) on purpose: that's how PyInstaller
# discovers and ships every stdlib module yt-dlp needs (optparse, sqlite3, …).
# At runtime the app loads the loose, auto-updatable copy from lib\ instead, via
# a small sys.meta_path finder (_LibFirstImporter) — see the frozen-build
# bootstrap at the top of VideoAudioDownloader_UI.py. The frozen copy then just
# serves as the stdlib provider + a guaranteed-working fallback.

import os

_icon = os.path.join(SPECPATH, 'build', 'icon.ico')
_icon = _icon if os.path.exists(_icon) else None

a = Analysis(
    ['VideoAudioDownloader_UI.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='VidAudDownloader',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,            # GUI app — no console window
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_icon,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='VidAudDownloader',
)
