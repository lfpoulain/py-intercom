import sys
from pathlib import Path

# Make `py_intercom` importable when running `pytest` from the repo root,
# without requiring a pip install -e .
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
