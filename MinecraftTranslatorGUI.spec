# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all

opencc_datas, opencc_binaries, opencc_hiddenimports = collect_all('opencc')

a = Analysis(
    ['MinecraftTranslatorGUI.py'],
    pathex=[],
    binaries=opencc_binaries,
    datas=[
        ('app_icon.png', '.'),
        ('app_icon.ico', '.'),
    ] + opencc_datas,
    hiddenimports=list(opencc_hiddenimports) + ['opencc'],
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
    a.binaries,
    a.datas,
    [],
    name='MinecraftTranslatorGUI',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='app_icon.ico',
)
