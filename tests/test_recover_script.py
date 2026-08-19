from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest


def _write_zip(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("PRODUCT.SAFE/manifest.safe", "metadata")
        archive.writestr("PRODUCT.SAFE/GRANULE/test.jp2", b"pixels")


def _run(script: Path, archive_root: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), "--directory", str(archive_root), *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.skipif(shutil.which("unzip") is None, reason="unzip command is unavailable")
def test_recovery_script_previews_then_restores_crc_valid_zip(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    script = repository / "scripts/recover_quarantined.sh"
    final = tmp_path / "PRODUCT.SAFE.zip"
    quarantined = tmp_path / "PRODUCT.SAFE.zip.invalid-20260819T000000Z"
    _write_zip(quarantined)

    preview = _run(script, tmp_path)
    assert preview.returncode == 0, preview.stdout + preview.stderr
    assert quarantined.exists()
    assert not final.exists()
    assert "WOULD_RESTORE_VERIFIED_ZIP" in preview.stdout

    applied = _run(script, tmp_path, "--apply")
    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert final.exists()
    assert not quarantined.exists()
    assert "RESTORED_VERIFIED_ZIP" in applied.stdout


@pytest.mark.skipif(shutil.which("unzip") is None, reason="unzip command is unavailable")
def test_recovery_script_removes_only_byte_identical_duplicate(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    script = repository / "scripts/recover_quarantined.sh"
    final = tmp_path / "PRODUCT.SAFE.zip"
    quarantined = tmp_path / "PRODUCT.SAFE.zip.invalid-20260819T000000Z"
    _write_zip(final)
    shutil.copyfile(final, quarantined)

    applied = _run(script, tmp_path, "--apply")

    assert applied.returncode == 0, applied.stdout + applied.stderr
    assert final.exists()
    assert not quarantined.exists()
    assert "REMOVED_IDENTICAL_DUPLICATE" in applied.stdout
