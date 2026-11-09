"""In-app update from the latest GitHub release, without any Qt.

Checking is one small HTTPS request; installing downloads the release's
``py-intercom-setup.exe``, verifies its SHA-256 and runs it silently. The
installer replaces the installed program in place and starts it again.

Everything here is conservative: HTTPS only, GitHub hosts only (also after each
redirect), a size cap, and no install without a checksum that matches. A failure
is reported with a plain French sentence and never touches the running program.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

GITHUB_REPO = "lfpoulain/py-intercom"
LATEST_RELEASE_API = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
INSTALLER_NAME = "py-intercom-setup.exe"
CHECKSUMS_NAME = "SHA256SUMS.txt"

# A release is a ~50 MB installer; anything far above that is not ours.
MAX_INSTALLER_BYTES = 400 * 1024 * 1024
_MAX_JSON_BYTES = 2 * 1024 * 1024
_CHUNK_BYTES = 64 * 1024
_DOWNLOAD_DEADLINE_S = 20 * 60
_ALLOWED_HOSTS = ("github.com", "githubusercontent.com")
_USER_AGENT = "py-intercom-updater"
_VERSION_RE = re.compile(r"^v?(\d+(?:\.\d+){0,3})$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")

ENV_DISABLE = "PY_INTERCOM_NO_UPDATE_CHECK"
ENV_FORCE = "PY_INTERCOM_UPDATE_CHECK"


class UpdateError(Exception):
    """A failed check or install; ``str(error)`` is a sentence for the operator."""


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    page_url: str
    installer_url: str
    installer_size: int
    sha256: str | None
    checksums_url: str | None


def parse_version(text: str) -> tuple[int, ...] | None:
    """``v3.0.9`` -> (3, 0, 9); None for anything that is not a plain version."""
    match = _VERSION_RE.match(str(text).strip())
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def is_newer(candidate: str, current: str) -> bool:
    new, old = parse_version(candidate), parse_version(current)
    if new is None or old is None:
        return False
    width = max(len(new), len(old))
    return new + (0,) * (width - len(new)) > old + (0,) * (width - len(old))


def is_installed_build() -> bool:
    """True for the installed Windows program (PyInstaller), where updating makes sense."""
    return bool(getattr(sys, "frozen", False)) and sys.platform == "win32"


def checks_enabled() -> bool:
    """The check runs from the installed build, unless disabled by the environment."""
    if os.environ.get(ENV_DISABLE, "").strip() not in ("", "0"):
        return False
    return is_installed_build() or os.environ.get(ENV_FORCE, "").strip() not in ("", "0")


def _host_allowed(url: str) -> bool:
    parts = urllib.parse.urlsplit(url)
    host = (parts.hostname or "").lower()
    return parts.scheme == "https" and any(host == h or host.endswith("." + h) for h in _ALLOWED_HOSTS)


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    """Follows redirects only to HTTPS GitHub hosts (assets live on a CDN host)."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: N802
        if not _host_allowed(newurl):
            raise UpdateError("Le téléchargement a été redirigé hors de GitHub : mise à jour refusée.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url: str, timeout: float, accept: str = "application/octet-stream"):
    if not _host_allowed(url):
        raise UpdateError("Adresse de mise à jour non autorisée.")
    opener = urllib.request.build_opener(_CheckedRedirects)
    request = urllib.request.Request(url, headers={"User-Agent": _USER_AGENT, "Accept": accept})
    return opener.open(request, timeout=timeout)


def _network_error(exc: Exception) -> UpdateError:
    if isinstance(exc, UpdateError):
        return exc
    if isinstance(exc, urllib.error.HTTPError):
        return UpdateError(f"GitHub a répondu une erreur {exc.code}.")
    return UpdateError("GitHub est injoignable (pas d'accès à Internet ?).")


def _read_limited(response, limit: int) -> bytes:
    data = response.read(limit + 1)
    if len(data) > limit:
        raise UpdateError("Réponse de GitHub trop volumineuse.")
    return data


def release_from_json(payload: dict) -> ReleaseInfo | None:
    """The installer and checksum of a release document; None if it has no installer."""
    tag = str(payload.get("tag_name", ""))
    if parse_version(tag) is None or payload.get("draft") or payload.get("prerelease"):
        return None
    assets = {str(a.get("name", "")): a for a in payload.get("assets", []) if isinstance(a, dict)}
    installer = assets.get(INSTALLER_NAME)
    if installer is None:
        return None
    url = str(installer.get("browser_download_url", ""))
    if not _host_allowed(url):
        return None
    algorithm, _, value = str(installer.get("digest", "")).partition(":")
    sha256 = value.lower() if algorithm.lower() == "sha256" and _SHA256_RE.match(value) else None
    sums = assets.get(CHECKSUMS_NAME)
    sums_url = str(sums.get("browser_download_url", "")) if sums else ""
    page = str(payload.get("html_url", ""))
    return ReleaseInfo(
        version=".".join(str(n) for n in parse_version(tag) or ()),
        page_url=page if _host_allowed(page) else f"https://github.com/{GITHUB_REPO}/releases/latest",
        installer_url=url,
        installer_size=int(installer.get("size") or 0),
        sha256=sha256,
        checksums_url=sums_url if sums_url and _host_allowed(sums_url) else None,
    )


def fetch_latest_release(timeout: float = 6.0) -> ReleaseInfo | None:
    """The latest published release, or None when it has no installer."""
    try:
        with _open(LATEST_RELEASE_API, timeout, "application/vnd.github+json") as response:
            payload = json.loads(_read_limited(response, _MAX_JSON_BYTES))
    except UpdateError:
        raise
    except json.JSONDecodeError as exc:
        raise UpdateError("Réponse de GitHub illisible.") from exc
    except Exception as exc:
        raise _network_error(exc) from exc
    if not isinstance(payload, dict):
        raise UpdateError("Réponse de GitHub illisible.")
    return release_from_json(payload)


def parse_checksums(text: str, name: str) -> str | None:
    """The SHA-256 of ``name`` in a ``sha256sum`` listing (``<hash>  <file>`` lines)."""
    for line in text.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[1].lstrip("*") == name and _SHA256_RE.match(parts[0]):
            return parts[0].lower()
    return None


def _expected_sha256(release: ReleaseInfo, timeout: float) -> str:
    if release.sha256:
        return release.sha256
    if release.checksums_url:
        try:
            with _open(release.checksums_url, timeout) as response:
                text = _read_limited(response, 64 * 1024).decode("utf-8", "replace")
        except Exception as exc:
            raise _network_error(exc) from exc
        found = parse_checksums(text, INSTALLER_NAME)
        if found:
            return found
    raise UpdateError("Cette version n'a pas de somme de contrôle : installation refusée par sécurité.")


def update_dir() -> Path:
    return Path(tempfile.gettempdir()) / "py-intercom-update"


def cleanup_update_dir() -> None:
    """Remove the installer of an earlier update (best effort)."""
    shutil.rmtree(update_dir(), ignore_errors=True)


_LOCAL_WRITE_ERROR = "Impossible d'écrire le fichier de mise à jour (disque plein, antivirus ?)."
_TOO_BIG_ERROR = "Le fichier de mise à jour est anormalement gros : installation refusée."


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_installer(path: Path, expected_sha256: str) -> None:
    """Check, right before running it, that the installer is still the verified file."""
    try:
        actual = file_sha256(path)
    except OSError as exc:
        raise UpdateError("Le fichier de mise à jour a disparu ou n'est plus lisible.") from exc
    if actual != expected_sha256:
        raise UpdateError("Le fichier de mise à jour a été modifié depuis son téléchargement : installation annulée.")


def download_installer(
    release: ReleaseInfo,
    progress: Callable[[int, int], None] | None = None,
    cancel: threading.Event | None = None,
    *,
    timeout: float = 15.0,
    directory: Path | None = None,
) -> Path:
    """Download the installer, check its size and SHA-256, return the file."""
    expected = _expected_sha256(release, timeout)
    folder = directory or update_dir()
    final = folder / f"py-intercom-setup-{release.version}.exe"
    partial = final.with_suffix(".part")
    try:
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise UpdateError(_LOCAL_WRITE_ERROR) from exc
    digest = hashlib.sha256()
    received = 0
    deadline = time.monotonic() + _DOWNLOAD_DEADLINE_S
    try:
        with _open(release.installer_url, timeout) as response:
            total = int(response.headers.get("Content-Length") or release.installer_size or 0)
            if total > MAX_INSTALLER_BYTES:
                raise UpdateError(_TOO_BIG_ERROR)
            try:
                out = open(partial, "wb")
            except OSError as exc:
                raise UpdateError(_LOCAL_WRITE_ERROR) from exc
            with out:
                while True:
                    if cancel is not None and cancel.is_set():
                        raise UpdateError("Téléchargement annulé.")
                    if time.monotonic() > deadline:
                        raise UpdateError("Le téléchargement est trop lent : réessayez plus tard.")
                    chunk = response.read(_CHUNK_BYTES)
                    if not chunk:
                        break
                    received += len(chunk)
                    if received > MAX_INSTALLER_BYTES:
                        raise UpdateError(_TOO_BIG_ERROR)
                    digest.update(chunk)
                    try:
                        out.write(chunk)
                    except OSError as exc:
                        raise UpdateError(_LOCAL_WRITE_ERROR) from exc
                    if progress is not None:
                        progress(received, total)
        if release.installer_size and received != release.installer_size:
            raise UpdateError("Téléchargement incomplet : réessayez.")
        if digest.hexdigest() != expected:
            raise UpdateError(
                "Le fichier téléchargé est corrompu (somme de contrôle différente) : installation annulée."
            )
        try:
            os.replace(partial, final)
        except OSError as exc:
            raise UpdateError(_LOCAL_WRITE_ERROR) from exc
    except BaseException as exc:
        with contextlib.suppress(OSError):
            partial.unlink(missing_ok=True)
        if isinstance(exc, Exception) and not isinstance(exc, UpdateError):
            raise _network_error(exc) from exc
        raise
    return final


def installer_arguments(relaunch_role: str | None) -> str:
    """Silent install over the existing one; the installer starts the program again."""
    args = "/SILENT /SUPPRESSMSGBOXES /NORESTART /CLOSEAPPLICATIONS"
    if relaunch_role in ("server", "client"):
        args += f" /RELAUNCH={relaunch_role}"
    return args


def needs_elevation(folder: Path) -> bool:
    """True when the installed program's folder is not writable (a Program Files install)."""
    # One direct attempt: tempfile retries a refused name thousands of times on
    # Windows, which would freeze the window for minutes in Program Files.
    probe = folder / f".py-intercom-write-test-{os.getpid()}"
    try:
        os.close(os.open(probe, os.O_CREAT | os.O_EXCL | os.O_WRONLY))
    except OSError:
        return True
    with contextlib.suppress(OSError):
        os.unlink(probe)
    return False


def launch_installer(path: Path, relaunch_role: str | None, *, elevate: bool | None = None) -> None:
    """Start the installer detached.

    An install in Program Files needs the administrator: it is started with the
    "runas" verb, so that a declined consent comes back here as an error. (Setup's
    own manifest only asks to be elevated later, from inside, where a refusal
    would go unnoticed and leave this PC with no intercom running.)
    """
    if sys.platform != "win32":
        raise UpdateError("L'installation automatique n'est disponible que sous Windows.")
    import ctypes

    if elevate is None:
        elevate = needs_elevation(Path(sys.executable).parent)
    result = ctypes.windll.shell32.ShellExecuteW(
        None, "runas" if elevate else "open", str(path), installer_arguments(relaunch_role), str(path.parent), 1
    )
    if int(result) <= 32:
        raise UpdateError("Windows n'a pas lancé l'installateur : l'autorisation administrateur a été refusée.")


def other_instances() -> int:
    """How many other py-intercom programs run from this install (Windows; 0 elsewhere).

    A Régie and its local Poste are two processes of the same files: an update
    started from one would make the installer close the other without asking.
    A process whose path cannot be read counts, to be safe.
    """
    if sys.platform != "win32":
        return 0
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", ctypes.c_long),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", ctypes.c_wchar * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

    mine = os.path.normcase(os.path.abspath(sys.executable))
    name = os.path.basename(mine)
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == ctypes.c_void_p(-1).value:
        return 0
    count = 0
    try:
        entry = ProcessEntry()
        entry.dwSize = ctypes.sizeof(ProcessEntry)
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            if entry.th32ProcessID != os.getpid() and os.path.normcase(entry.szExeFile) == name:
                path = None
                handle = kernel32.OpenProcess(0x1000, False, entry.th32ProcessID)  # QUERY_LIMITED_INFORMATION
                if handle:
                    buffer = ctypes.create_unicode_buffer(1024)
                    size = wintypes.DWORD(len(buffer))
                    if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                        path = os.path.normcase(os.path.abspath(buffer.value))
                    kernel32.CloseHandle(handle)
                if path is None or path == mine:
                    count += 1
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return count
