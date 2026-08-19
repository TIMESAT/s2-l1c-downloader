from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from s2l1c.config import load_config
from s2l1c.utils import ConfigError


def test_example_configuration_resolves_open_end_date():
    repository = Path(__file__).resolve().parents[1]
    config = load_config(repository / "config/vombsjon.yaml", today=date(2026, 8, 17))

    assert config.sentinel.start_date == date(2017, 1, 1)
    assert config.sentinel.end_date == date(2026, 8, 17)
    assert config.sentinel.end_date_was_open is True
    assert config.sentinel.max_scene_cloud_cover is None
    assert config.sentinel.require_full_processing_roi_coverage is True
    assert config.download.workers == 2
    assert config.download.layout == "tile"
    assert config.api.catalogue_odata_url == (
        "https://catalogue.dataspace.copernicus.eu/odata/v1"
    )
    assert config.study_area.geometry == repository / "config/vombsjon.geojson"


def test_invalid_cloud_threshold_is_rejected(app_config):
    text = app_config.source_path.read_text(encoding="utf-8")
    app_config.source_path.write_text(
        text.replace("max_scene_cloud_cover: null", "max_scene_cloud_cover: 101"),
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match="between 0 and 100"):
        load_config(app_config.source_path)


def test_environment_overrides_download_directory(app_config, tmp_path):
    archive = tmp_path / "shared" / "S2L1C"
    config = load_config(
        app_config.source_path,
        environment={"S2L1C_DOWNLOAD_DIRECTORY": str(archive)},
    )

    assert config.download.directory == archive
    assert config.effective_dict()["download"]["directory"] == str(archive)


def test_legacy_download_directory_environment_alias(app_config, tmp_path):
    archive = tmp_path / "legacy" / "S2L1C"
    config = load_config(
        app_config.source_path,
        environment={"S2VOMB_DOWNLOAD_DIRECTORY": str(archive)},
    )

    assert config.download.directory == archive
