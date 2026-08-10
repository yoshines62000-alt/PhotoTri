# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['gui.py'],
    pathex=[],
    binaries=[],
    datas=[('opl_assets', 'opl_assets'), ('icon.ico', '.')],
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
    # UPX desactive (trouvaille d'audit du 2026-07-28, alignee sur
    # GuideExpress qui a deja ce meme correctif pour la meme raison) : la
    # compression UPX est une technique aussi largement utilisee par des
    # malwares pour echapper a la detection par signature - sa presence est
    # un facteur aggravant bien identifie de faux positif antivirus pour les
    # executables PyInstaller, en particulier non signes (comme PhotoTri.exe
    # aujourd'hui - aucune signature de code Windows en place). Desactiver
    # UPX est le levier le plus simple et le plus documente pour reduire ce
    # risque, au prix d'un executable un peu plus volumineux - compromis
    # raisonnable ici.
    upx=False,
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
