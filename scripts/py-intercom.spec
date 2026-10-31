# -*- mode: python ; coding: utf-8 -*-
# Builds py-intercom.exe (one file): the Régie or a Poste, picked at launch.
# Run through scripts/build_exe.ps1.

import os

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))  # noqa: F821 - injected by PyInstaller
ICON = os.path.join(ROOT, "src", "py_intercom", "img", "logo.ico")

# opus.dll lands at the bundle root, where bootstrap.prepare_windows_runtime looks for it.
BINARIES = [(os.path.join(ROOT, "bin", "opus.dll"), ".")] + collect_dynamic_libs("sounddevice")
DATAS = [(ICON, "img")]


analysis = Analysis(  # noqa: F821
    [os.path.join(ROOT, "run_intercom.py")],
    pathex=[ROOT, os.path.join(ROOT, "src")],
    binaries=BINARIES,
    datas=DATAS,
    hiddenimports=collect_submodules("py_intercom"),
)
EXE(  # noqa: F821
    PYZ(analysis.pure),  # noqa: F821
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    name="py-intercom",
    console=False,
    # UPX-packed executables are routinely flagged by antivirus software.
    upx=False,
    icon=ICON,
)
