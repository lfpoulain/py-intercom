import os
import sys

_is_frozen = bool(getattr(sys, "frozen", False))
if _is_frozen:
    _root = os.path.dirname(sys.executable)
else:
    _root = os.path.dirname(__file__)
    sys.path.insert(0, os.path.join(_root, "src"))

if os.name == "nt":
    _dll_dirs = []
    _meipass = getattr(sys, "_MEIPASS", None)
    if _meipass:
        _dll_dirs.append(_meipass)
    _dll_dirs.append(_root)
    _dll_dirs.append(os.path.join(_root, "bin"))
    for _d in _dll_dirs:
        if os.path.isdir(_d):
            try:
                os.add_dll_directory(_d)
            except Exception:
                pass
            os.environ["PATH"] = _d + os.pathsep + os.environ.get("PATH", "")
    try:
        import ctypes

        _candidates = []
        if _meipass:
            _candidates.append(os.path.join(_meipass, "opus.dll"))
            _candidates.append(os.path.join(_meipass, "bin", "opus.dll"))
        _candidates.append(os.path.join(_root, "opus.dll"))
        _candidates.append(os.path.join(_root, "bin", "opus.dll"))

        for _p in _candidates:
            if os.path.isfile(_p):
                try:
                    ctypes.CDLL(_p)
                    break
                except Exception:
                    pass
    except Exception:
        pass

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
