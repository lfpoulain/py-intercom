import os
import sys

_is_frozen = bool(getattr(sys, "frozen", False))
if _is_frozen:
    _root = os.path.dirname(sys.executable)
else:
    _root = os.path.dirname(__file__)
    sys.path.insert(0, os.path.join(_root, "src"))

_bin = os.path.join(_root, "bin")
if os.name == "nt" and os.path.isdir(_bin):
    try:
        os.add_dll_directory(_bin)
    except Exception:
        pass
    os.environ["PATH"] = _bin + os.pathsep + os.environ.get("PATH", "")

from py_intercom.server.main import main


if __name__ == "__main__":
    if _is_frozen and len(sys.argv) == 1:
        sys.argv.append("--gui")
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except Exception:
        try:
            import traceback

            base_dir = os.path.join(os.path.expanduser("~"), "py-intercom")
            os.makedirs(base_dir, exist_ok=True)
            crash_path = os.path.join(base_dir, "server_crash.txt")
            with open(crash_path, "a", encoding="utf-8") as f:
                f.write(traceback.format_exc())
                f.write("\n")
            if _is_frozen and os.name == "nt":
                try:
                    import ctypes

                    ctypes.windll.user32.MessageBoxW(0, f"Erreur: voir {crash_path}", "py-intercom server", 0x10)
                except Exception:
                    pass
        finally:
            raise
