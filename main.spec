# -*- mode: python ; coding: utf-8 -*-
"""本地用 PyInstaller 打包为单文件 exe 的规格文件。
CI 中改用命令行（带 --add-binary 内嵌 ffmpeg），会以命令行参数覆盖此处设置。"""

import os

block_cipher = None

a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        "cue2flac",
        "cue2flac.cli",
        "cue2flac.cue_parser",
        "cue2flac.splitter",
        "cue2flac.ffmpeg_provider",
        "tkinter",
        "tkinter.ttk",
        "tkinter.filedialog",
        "tkinter.messagebox",
    ],
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
    a.binaries,
    a.datas,
    name="cue2flac",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
