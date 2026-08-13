from __future__ import annotations

import ctypes
import hashlib
import json
import os
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
OPUS_DLL = ROOT / "bin" / "opus.dll"
OPUS_METADATA = ROOT / "bin" / "opus-build-info.json"
OPUS_LICENSE = ROOT / "licenses" / "OPUS.txt"
EXPECTED_SOURCE_SHA256 = (
    "6ffcb593207be92584df15b32466ed64bbec99109f007c82205f0194572411a1"
)
EXPECTED_LICENSE_SHA256 = (
    "01e1167d54a096d123cf6dfbbeb19587278845c6481d2d66d545669846079551"
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_opus_build_manifest_matches_bundled_runtime() -> None:
    metadata = json.loads(OPUS_METADATA.read_text(encoding="utf-8-sig"))

    assert metadata["version"] == "1.6.1"
    assert metadata["source_sha256"] == EXPECTED_SOURCE_SHA256
    assert metadata["architecture"] == "x86_64"
    assert metadata["configuration"] == "Release"
    assert metadata["hardening"] is True
    assert metadata["native_tests_passed"] is True
    assert metadata["dll_sha256"] == _sha256(OPUS_DLL)


def test_tracked_opus_license_is_the_verified_upstream_copy() -> None:
    assert _sha256(OPUS_LICENSE) == EXPECTED_LICENSE_SHA256


@pytest.mark.skipif(os.name != "nt", reason="bundled runtime is a Windows DLL")
def test_bundled_opus_reports_declared_version() -> None:
    library = ctypes.CDLL(os.fspath(OPUS_DLL))
    library.opus_get_version_string.restype = ctypes.c_char_p

    assert library.opus_get_version_string() == b"libopus 1.6.1"


def test_release_workflow_builds_verified_opus_before_python_validation() -> None:
    workflow = (ROOT / ".github" / "workflows" / "windows-release.yml").read_text(
        encoding="utf-8"
    )

    assert "PY_INTERCOM_ENABLE_PUBLIC_RELEASE" not in workflow
    assert workflow.index("Build verified Opus runtime") < workflow.index(
        "Validate sources"
    )
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "dist/OPUS_LICENSE.txt" in workflow
    assert "dist/OPUS_BUILD_INFO.json" in workflow
    opus_step = workflow[workflow.index("Build verified Opus runtime") :]
    assert opus_step.index("shell: powershell") < opus_step.index("Validate sources")
    assert "Verified Opus build failed" in opus_step
