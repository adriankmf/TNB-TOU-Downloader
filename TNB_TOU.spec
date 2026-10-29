# TNB_TOU.spec -- PyInstaller build for TNB Smart Sync (run via "Build EXE.bat")

from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules

block_cipher = None

a = Analysis(
    ['app.py'],
    pathex=[str(Path('.').resolve())],
    binaries=[],
    datas=[('ui', 'ui')] + collect_data_files('selenium'),
    hiddenimports=(
        collect_submodules('webview')
        + ['keyring.backends.Windows', 'win32ctypes.core']
        + ['selenium.webdriver.chrome.webdriver', 'selenium.webdriver.edge.webdriver']
    ),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['matplotlib', 'scipy', 'PyQt5', 'PyQt6', 'PySide6', 'tkinter', 'pytest'],
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='TNB_TOU',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,          # window app, no console
    disable_windowed_traceback=False,
    target_arch=None,
)
