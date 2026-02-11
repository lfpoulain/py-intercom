import os
import sys

_root = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(_root, "src"))

_bin = os.path.join(_root, "bin")
if os.name == "nt" and os.path.isdir(_bin):
    try:
        os.add_dll_directory(_bin)
    except Exception:
        pass
    os.environ["PATH"] = _bin + os.pathsep + os.environ.get("PATH", "")

from py_intercom.web.main import main


if __name__ == "__main__":
    raise SystemExit(main())
