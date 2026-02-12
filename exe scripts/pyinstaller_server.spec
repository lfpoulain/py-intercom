# -*- mode: python ; coding: utf-8 -*-

import os

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules


_spec_path = globals().get("SPECPATH")
if _spec_path:
    _project_root = os.path.abspath(_spec_path)
else:
    _spec_file = globals().get("__file__")
    if _spec_file:
        _project_root = os.path.abspath(os.path.dirname(_spec_file))
    else:
        _project_root = os.path.abspath(os.getcwd())

if not os.path.exists(os.path.join(_project_root, "run_server.py")):
    _parent = os.path.abspath(os.path.join(_project_root, os.pardir))
    if os.path.exists(os.path.join(_parent, "run_server.py")):
        _project_root = _parent

_binaries = [
    (os.path.join(_project_root, "bin", "opus.dll"), "."),
]

try:
    _binaries += collect_dynamic_libs("sounddevice")
except Exception:
    pass

_datas = []
try:
    _datas += collect_data_files("qt_material")
except Exception:
    pass

_hiddenimports = []
_hiddenimports += collect_submodules("py_intercom.server")
_hiddenimports += collect_submodules("py_intercom.common")
try:
    _hiddenimports += collect_submodules("qt_material")
except Exception:
    pass


a = Analysis(
    [os.path.join(_project_root, "run_server.py")],
    pathex=[_project_root, os.path.join(_project_root, "src")],
    binaries=_binaries,
    datas=_datas,
    hiddenimports=_hiddenimports,
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
    a.zipfiles,
    a.datas,
    name="server",
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
)
