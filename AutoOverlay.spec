# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_data_files


imageio_ffmpeg_datas = collect_data_files("imageio_ffmpeg", includes=["binaries/*"])
license_datas = [("keys/license_public.pem", "keys")]


a = Analysis(
    ["main.py"],
    pathex=[],
    binaries=[],
    datas=imageio_ffmpeg_datas + license_datas,
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="AutoOverlay",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="AutoOverlay",
)
