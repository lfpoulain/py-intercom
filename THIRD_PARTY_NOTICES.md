# Third-party notices

This inventory belongs to the Windows desktop distribution of py-intercom.
Versions follow the locked runtime dependencies in `requirements.txt`.
The corresponding full licence texts and native build manifests are shipped
in `py-intercom-licenses.zip` with every release.

## Native Opus runtime

| Item | Recorded value |
|---|---|
| File | `bin/opus.dll` |
| Embedded version string | `libopus 1.6.1` |
| Official source | `https://downloads.xiph.org/releases/opus/opus-1.6.1.tar.gz` |
| Official source SHA-256 | `6ffcb593207be92584df15b32466ed64bbec99109f007c82205f0194572411a1` |
| Repository DLL SHA-256 | `7a6c5a059beaad0f46ca0ef0e6a49026ae7e8125a3309d7a92ba79d571161497` |
| Architecture/configuration | x86-64, Release, hardening enabled, static MSVC runtime |
| Build recipe | `scripts/build_opus.ps1` |
| Neural features | Deep PLC enabled; DRED and OSCE disabled |
| Upstream licence family | BSD-3-Clause |
| Full upstream licence | `licenses/OPUS.txt` / distributed as `OPUS_LICENSE.txt` |

The build script downloads only the pinned Xiph source archive, rejects a
checksum mismatch, verifies the tracked licence byte-for-byte, builds an x64
DLL with Visual Studio and runs the five upstream native tests in a separate
static build. Each build generates `OPUS_BUILD_INFO.json` with the actual DLL
hash and toolchain versions; the executables embed that exact DLL.

## Python runtime dependencies

| Distribution | Version | Licence metadata |
|---|---:|---|
| cffi | 2.0.0 | MIT |
| colorama | 0.4.6 | BSD-3-Clause |
| loguru | 0.7.0 | MIT |
| numpy | 2.4.2 | BSD-3-Clause and bundled component licences |
| opuslib | 3.0.1 | BSD-3-Clause |
| packaging | 26.0 | Apache-2.0 OR BSD-2-Clause |
| pycparser | 3.0 | BSD-3-Clause |
| pynput | 1.7.6 | LGPL-3.0 |
| PySide6 / Addons / Essentials | 6.10.2 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only |
| setuptools | 82.0.0 | MIT |
| shiboken6 | 6.10.2 | LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only |
| six | 1.17.0 | MIT |
| sounddevice | 0.5.5 | MIT |
| wheel | 0.46.3 | MIT |
| win32_setctime | 1.2.0 | MIT |

This table is an inventory, not a substitute for the licence files shipped by
each distribution. NumPy and Qt/PySide in particular contain additional
components whose notices must be preserved when redistributing binaries.
