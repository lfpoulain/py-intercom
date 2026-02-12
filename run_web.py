import os
import sys
import socket

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


def _detect_lan_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
        finally:
            s.close()
        if ip and not ip.startswith("127."):
            return str(ip)
    except Exception:
        pass

    try:
        host = socket.gethostname()
        infos = socket.getaddrinfo(host, None, family=socket.AF_INET)
        for info in infos:
            ip = info[4][0]
            if ip and not ip.startswith("127."):
                return str(ip)
    except Exception:
        pass

    return "0.0.0.0"


if __name__ == "__main__":
    if len(sys.argv) == 1:
        sys.argv.extend(
            [
                "--host",
                _detect_lan_ip(),
                "--port",
                "8443",
                "--ssl-adhoc",
                "--debug",
            ]
        )
    raise SystemExit(main())
