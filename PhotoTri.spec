# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['gui.py'],
    pathex=[],
    binaries=[],
    datas=[('icon.ico', '.')],
    hiddenimports=['pillow_heif', '_pillow_heif'],
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
    name='PhotoTri',
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
    icon=['icon.ico'],
    # Correctif audit G1 : embarque des metadonnees de version Windows
    # (FileVersion/ProductVersion/CompanyName/FileDescription/...) dans
    # l'executable, absentes auparavant (onglet "Details" des proprietes
    # Windows entierement vide) - voir version_info.txt pour le detail et
    # la procedure de mise a jour a chaque nouvelle version.
    version='version_info.txt',
)
