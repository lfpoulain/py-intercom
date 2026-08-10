from __future__ import annotations

import ctypes
import os
import sys
import traceback
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn


@dataclass(frozen=True)
class WindowsRuntime:
    """Result of the optional Windows DLL bootstrap."""

    dll_directories: tuple[Path, ...] = ()
    opus_path: Path | None = None


# ``os.add_dll_directory`` unregisters a directory when its result is closed or
# garbage-collected. Keep those handles alive for the lifetime of the process.
_DLL_DIRECTORY_HANDLES: list[object] = []
_LOADED_LIBRARIES: list[object] = []


def _is_windows() -> bool:
    return os.name == "nt"


def _unique_paths(paths: list[Path]) -> tuple[Path, ...]:
    result: list[Path] = []
    seen: set[str] = set()
    for path in paths:
        normalized = os.path.normcase(os.path.abspath(os.fspath(path)))
        if normalized in seen:
            continue
        seen.add(normalized)
        result.append(Path(path))
    return tuple(result)


def _dll_directories(root: Path, meipass: Path | None) -> tuple[Path, ...]:
    candidates: list[Path] = []
    if meipass is not None:
        candidates.append(meipass)
    candidates.extend((root, root / "bin"))
    return _unique_paths(candidates)


def _opus_candidates(root: Path, meipass: Path | None) -> tuple[Path, ...]:
    candidates: list[Path] = []
    if meipass is not None:
        candidates.extend((meipass / "opus.dll", meipass / "bin" / "opus.dll"))
    candidates.extend((root / "opus.dll", root / "bin" / "opus.dll"))
    return _unique_paths(candidates)


def _prepend_to_path(directories: tuple[Path, ...], environ: MutableMapping[str, str]) -> None:
    current = [entry for entry in environ.get("PATH", "").split(os.pathsep) if entry]
    requested = [os.fspath(path) for path in directories]
    requested_keys = {
        os.path.normcase(os.path.abspath(entry))
        for entry in requested
    }
    retained = [
        entry
        for entry in current
        if os.path.normcase(os.path.abspath(entry)) not in requested_keys
    ]
    environ["PATH"] = os.pathsep.join((*requested, *retained))


def prepare_windows_runtime(
    root: str | os.PathLike[str],
    *,
    meipass: str | os.PathLike[str] | None = None,
    environ: MutableMapping[str, str] | None = None,
) -> WindowsRuntime:
    """Make bundled Opus DLL locations visible before importing audio modules.

    This is intentionally safe to call in source and PyInstaller builds. On
    non-Windows platforms it is a no-op.
    """

    if not _is_windows():
        return WindowsRuntime()

    root_path = Path(root).resolve()
    raw_meipass = meipass if meipass is not None else getattr(sys, "_MEIPASS", None)
    meipass_path = Path(raw_meipass).resolve() if raw_meipass else None
    existing_directories = tuple(
        path for path in _dll_directories(root_path, meipass_path) if path.is_dir()
    )

    add_dll_directory = getattr(os, "add_dll_directory", None)
    if add_dll_directory is not None:
        for directory in existing_directories:
            try:
                handle = add_dll_directory(os.fspath(directory))
            except OSError:
                continue
            _DLL_DIRECTORY_HANDLES.append(handle)

    _prepend_to_path(existing_directories, environ if environ is not None else os.environ)

    loaded_opus: Path | None = None
    for candidate in _opus_candidates(root_path, meipass_path):
        if not candidate.is_file():
            continue
        try:
            library = ctypes.CDLL(os.fspath(candidate))
        except OSError:
            continue
        _LOADED_LIBRARIES.append(library)
        loaded_opus = candidate
        break

    return WindowsRuntime(dll_directories=existing_directories, opus_path=loaded_opus)


def _crash_directory() -> Path:
    return Path.home() / "py-intercom"


def write_crash_report(
    component: str,
    traceback_text: str,
    *,
    directory: str | os.PathLike[str] | None = None,
) -> Path | None:
    """Append a traceback to the component crash report, without masking it."""

    safe_component = "".join(
        character for character in str(component) if character.isalnum() or character in "-_"
    ) or "app"
    try:
        target_directory = Path(directory) if directory is not None else _crash_directory()
        crash_path = target_directory / f"{safe_component}_crash.txt"
        target_directory.mkdir(parents=True, exist_ok=True)
        with crash_path.open("a", encoding="utf-8") as stream:
            stream.write(traceback_text)
            if not traceback_text.endswith("\n"):
                stream.write("\n")
            stream.write("\n")
    except Exception:
        # Crash reporting is best-effort and must never replace the original
        # application exception with a filesystem or encoding error.
        return None
    return crash_path


def _show_crash_message(component: str, crash_path: Path) -> None:
    try:
        ctypes.windll.user32.MessageBoxW(  # type: ignore[attr-defined]
            0,
            f"Erreur: voir {crash_path}",
            f"py-intercom {component}",
            0x10,
        )
    except Exception:
        # The original application exception is still active at this point.
        pass


def run_application(
    entrypoint: Callable[[], int | None],
    *,
    component: str,
    frozen: bool | None = None,
    crash_directory: str | os.PathLike[str] | None = None,
) -> NoReturn:
    """Run a GUI entrypoint and preserve diagnostics for an unhandled error."""

    try:
        exit_code = entrypoint()
    except SystemExit:
        raise
    except Exception:
        crash_path = write_crash_report(
            component,
            traceback.format_exc(),
            directory=crash_directory,
        )
        is_frozen = bool(getattr(sys, "frozen", False)) if frozen is None else bool(frozen)
        if crash_path is not None and is_frozen and _is_windows():
            _show_crash_message(component, crash_path)
        raise
    raise SystemExit(exit_code)
