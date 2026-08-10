import sys
from pathlib import Path

_is_frozen = bool(getattr(sys, "frozen", False))
if _is_frozen:
    _root = Path(sys.executable).resolve().parent
else:
    _root = Path(__file__).resolve().parent
    sys.path.insert(0, str(_root / "src"))

from py_intercom.common.bootstrap import prepare_windows_runtime, run_application

prepare_windows_runtime(_root)

from py_intercom.server.main import main


if __name__ == "__main__":
    run_application(main, component="server", frozen=_is_frozen)
