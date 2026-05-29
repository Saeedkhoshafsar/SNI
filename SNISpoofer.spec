# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for SNI Spoofer (step 13) — single windowed exe.

Build with::

    pyinstaller SNISpoofer.spec            # or: python scripts/build_exe.py

What it bundles
---------------
* ``bin/`` — xray.exe, vwarp.exe, wintun.dll, geoip.dat, geosite.dat and the
  helper scripts. ``core.binary_utils.get_bin_dir()`` resolves them from
  ``sys._MEIPASS/bin`` at runtime, matching the ``bin -> bin`` mapping here.
* ``strategies.example.json`` — sample remote-strategy manifest (if present).
* ``assets/app.ico`` — taskbar / window icon.
* WinDivert (``WinDivert.dll`` / ``WinDivert64.sys``) shipped with *pydivert*,
  collected via ``collect_dynamic_libs`` + ``collect_data_files`` so the inject
  path works inside the frozen exe (it needs the driver next to the binary).

Notes
-----
* ``console=False`` — no terminal window (GUI app). Admin elevation is handled
  in ``app.py`` via ``core.admin.ensure_admin`` (ShellExecuteW runas), not here.
* This spec is portable: missing optional pieces (pydivert on non-Windows,
  the .ico) degrade gracefully so it can at least be parsed for inspection.
"""
import os

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs

block_cipher = None

PROJECT_ROOT = os.path.abspath(os.getcwd())


def _bin_datas():
    """Map every file under ./bin into the frozen bin/ folder."""
    out = []
    bin_dir = os.path.join(PROJECT_ROOT, "bin")
    if os.path.isdir(bin_dir):
        for name in os.listdir(bin_dir):
            src = os.path.join(bin_dir, name)
            if os.path.isfile(src):
                out.append((src, "bin"))
    return out


datas = []
datas += _bin_datas()

# Sample strategies manifest (read by core.strategies_remote in dev/demo).
_ex = os.path.join(PROJECT_ROOT, "strategies.example.json")
if os.path.isfile(_ex):
    datas.append((_ex, "."))

# pydivert ships the WinDivert driver/dll as package data + native libs.
binaries = []
try:
    datas += collect_data_files("pydivert")
    binaries += collect_dynamic_libs("pydivert")
except Exception:
    # pydivert isn't importable off-Windows; the real build runs on Windows.
    pass

icon_path = os.path.join(PROJECT_ROOT, "assets", "app.ico")
icon_arg = icon_path if os.path.isfile(icon_path) else None

hiddenimports = [
    # PySide6 submodules used across the UI.
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    # Inject path.
    "pydivert",
    # Pure-python crypto / remote strategy stack (imported lazily in places).
    "core.ed25519",
    "core.strategies_remote",
    "core.resilience",
    "core.diagnostics",
    "core.prober",
    "core.admin",
]


a = Analysis(
    ["app.py"],
    pathex=[PROJECT_ROOT],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Trim weight: we don't use these heavy stacks.
        "tkinter", "matplotlib", "numpy", "PIL", "pytest",
        "PySide6.QtWebEngineCore", "PySide6.QtWebEngineWidgets",
        "PySide6.Qt3DCore", "PySide6.QtMultimedia", "PySide6.QtQuick",
        "PySide6.QtNetworkAuth", "PySide6.QtCharts",
        "PySide6.QtDataVisualization",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
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
    name="SNISpoofer",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,           # windowed GUI app, no console
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon_arg,
)
