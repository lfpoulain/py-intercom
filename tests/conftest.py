import sys
from pathlib import Path

# Make `py_intercom` importable when running `pytest` from the repo root,
# without requiring a pip install -e .
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from py_intercom.common.bootstrap import prepare_windows_runtime  # noqa: E402

# Mirror the application entry points: the bundled libopus must be visible
# before any test imports opuslib, otherwise opuslib binds to whatever
# libopus the host exposes (or none) for the whole session.
prepare_windows_runtime(ROOT)
