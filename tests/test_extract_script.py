from __future__ import annotations

import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest


@pytest.mark.skipif(shutil.which("unzip") is None, reason="unzip command is unavailable")
def test_extract_script_verifies_safe_before_removing_zip(tmp_path):
    repository = Path(__file__).resolve().parents[1]
    script = repository / "scripts/extract_verified.sh"
    archive_root = tmp_path / "raw"
    safe_name = "S2A_MSIL1C_20240101T000000_TEST.SAFE"
    source_safe = tmp_path / "source" / safe_name
    image_data = source_safe / "GRANULE" / "L1C_TEST" / "IMG_DATA"
    image_data.mkdir(parents=True)
    (source_safe / "manifest.safe").write_text("manifest", encoding="utf-8")
    (source_safe / "MTD_MSIL1C.xml").write_text("<xml/>", encoding="utf-8")
    (image_data / "B01.jp2").write_bytes(b"jp2-data")

    archive_root.mkdir()
    archive = archive_root / f"{safe_name}.zip"
    with zipfile.ZipFile(archive, "w") as output:
        for source in source_safe.rglob("*"):
            if source.is_file():
                output.write(source, source.relative_to(source_safe.parent))

    result = subprocess.run(
        ["bash", str(script), "--directory", str(archive_root)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not archive.exists()
    assert (archive_root / safe_name / "manifest.safe").is_file()
    assert "extracted=1" in result.stdout
    assert "zip_removed=1" in result.stdout
