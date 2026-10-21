# -*- mode: python ; coding: utf-8 -*-
# Builds py-intercom-server.exe and py-intercom-client.exe (one file each).
# Run through scripts/build_exe.ps1.

import os

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

ROOT = os.path.abspath(os.path.join(SPECPATH, os.pardir))  # noqa: F821 - injected by PyInstaller
ICON = os.path.join(ROOT, "src", "py_intercom", "img", "logo.ico")

# opus.dll lands at the bundle root, where bootstrap.prepare_windows_runtime looks for it.
BINARIES = [(os.path.join(ROOT, "bin", "opus.dll"), ".")] + collect_dynamic_libs("sounddevice")
DATAS = [(ICON, "img")]


def build(component: str) -> None:
    analysis = Analysis(  # noqa: F821
        [os.path.join(ROOT, f"run_{component}.py")],
        pathex=[ROOT, os.path.join(ROOT, "src")],
        binaries=BINARIES,
        datas=DATAS,
        hiddenimports=collect_submodules(f"py_intercom.{component}") + collect_submodules("py_intercom.common"),
    )
    EXE(  # noqa: F821
        PYZ(analysis.pure),  # noqa: F821
        analysis.scripts,
        analysis.binaries,
        analysis.datas,
        name=f"py-intercom-{component}",
        console=False,
        # UPX-packed executables are routinely flagged by antivirus software.
        upx=False,
        icon=ICON,
    )


build("server")
build("client")
