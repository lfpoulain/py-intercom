# Third-party notices

This inventory belongs to the Windows desktop distribution of py-intercom.
Versions follow the locked runtime dependencies in `requirements.txt`.
Before any public release, the corresponding full licence texts must be
included beside the executables.

## Native Opus runtime

| Item | Recorded value |
|---|---|
| File | `bin/opus.dll` |
| Embedded version string | `libopus 1.3.1-18-g923bebde-dirty-fixed` |
| Size | 441,856 bytes |
| SHA-256 | `c84da836e8d92421ac305842cfe5a724898ed09d340d46b129e210bdc9448131` |
| Upstream licence family | BSD-3-Clause |
| Original download/build provenance | **Not recorded — release blocker** |

The binary is reproducibly identified by its hash, but its upstream URL,
toolchain and build recipe are absent from the repository. It may be used for
internal qualification, but a distributable release should replace it with a
traceable upstream artifact or a documented reproducible build.

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
