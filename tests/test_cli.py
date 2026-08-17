from __future__ import annotations

import json

from s2vomb.catalogue import CatalogueStore
from s2vomb.cli import main


def test_year_dry_run_requires_no_credentials_or_download(app_config, product_record, capsys):
    CatalogueStore(app_config).write_csv([product_record])

    result = main(
        [
            "download",
            "--config",
            str(app_config.source_path),
            "--year",
            "2024",
            "--dry-run",
        ]
    )

    output = capsys.readouterr().out
    assert result == 0
    assert "Date range: 2024-01-01 – 2024-12-31" in output
    assert "Dry-run targets" in output
    assert ".SAFE.zip" in output
    assert not app_config.download.directory.exists()
    result_files = list(app_config.provenance.directory.glob("*/download-results.json"))
    assert len(result_files) == 1
    ledger = json.loads(result_files[0].read_text(encoding="utf-8"))
    assert ledger["products"][0]["status"] == "planned"
