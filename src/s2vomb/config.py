"""Explicit YAML configuration parsing and validation."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from .utils import ConfigError, utc_today


@dataclass(frozen=True, slots=True)
class StudyAreaConfig:
    name: str
    geometry: Path
    search_feature: str
    processing_roi_feature: str


@dataclass(frozen=True, slots=True)
class SentinelConfig:
    collection: str
    processing_level: str
    start_date: date
    end_date: date
    end_date_was_open: bool
    platform: str | None
    tile_id: str | None
    require_full_processing_roi_coverage: bool
    max_scene_cloud_cover: float | None


@dataclass(frozen=True, slots=True)
class APIConfig:
    stac_search_url: str
    catalogue_odata_url: str
    token_url: str
    download_base_url: str
    request_timeout_seconds: int
    page_size: int


@dataclass(frozen=True, slots=True)
class DownloadConfig:
    directory: Path
    layout: str
    workers: int
    retries: int
    backoff_seconds: float
    chunk_size_mib: int
    verify_checksum: bool
    keep_source_archive: bool


@dataclass(frozen=True, slots=True)
class CatalogueConfig:
    directory: Path
    csv_name: str
    parquet_name: str
    stac_name: str
    write_parquet: bool

    @property
    def csv_path(self) -> Path:
        return self.directory / self.csv_name

    @property
    def parquet_path(self) -> Path:
        return self.directory / self.parquet_name

    @property
    def stac_path(self) -> Path:
        return self.directory / self.stac_name


@dataclass(frozen=True, slots=True)
class ProvenanceConfig:
    directory: Path


@dataclass(frozen=True, slots=True)
class AppConfig:
    source_path: Path
    project_root: Path
    study_area: StudyAreaConfig
    sentinel: SentinelConfig
    api: APIConfig
    download: DownloadConfig
    catalogue: CatalogueConfig
    provenance: ProvenanceConfig

    def effective_dict(self) -> dict[str, Any]:
        """Return the exact credential-free configuration applied by this run."""
        result = asdict(self)
        result.pop("source_path", None)
        result["project_root"] = str(self.project_root)
        result["study_area"]["geometry"] = str(self.study_area.geometry)
        result["sentinel"]["start_date"] = self.sentinel.start_date.isoformat()
        result["sentinel"]["end_date"] = self.sentinel.end_date.isoformat()
        result["download"]["directory"] = str(self.download.directory)
        result["catalogue"]["directory"] = str(self.catalogue.directory)
        result["provenance"]["directory"] = str(self.provenance.directory)
        return result


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ConfigError(f"Configuration section {name!r} must be a mapping")
    return value


def _required_text(section: Mapping[str, Any], key: str, section_name: str) -> str:
    value = section.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{section_name}.{key} must be a non-empty string")
    return value.strip()


def _date(value: Any, name: str) -> date:
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ConfigError(f"{name} must be an ISO date (YYYY-MM-DD)")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ConfigError(f"{name} must be an ISO date (YYYY-MM-DD)") from error


def _optional_text(value: Any, name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be null or a non-empty string")
    return value.strip()


def _positive_int(value: Any, name: str, *, minimum: int = 1, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ConfigError(f"{name} must be an integer >= {minimum}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{name} must be <= {maximum}")
    return value


def _bool(value: Any, name: str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(f"{name} must be true or false")
    return value


def _url(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.startswith("https://"):
        raise ConfigError(f"{name} must be an HTTPS URL")
    return value.rstrip("/")


def _official_url(value: Any, name: str, hostname: str) -> str:
    url = _url(value, name)
    if urlparse(url).hostname != hostname:
        raise ConfigError(f"{name} must use the official CDSE host {hostname}")
    return url


def _path(root: Path, value: Any, name: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} must be a non-empty path")
    candidate = Path(value).expanduser()
    return (root / candidate).resolve() if not candidate.is_absolute() else candidate.resolve()


def _normalize_platform(value: str | None) -> str | None:
    if value is None:
        return None
    compact = value.lower().replace("_", "-")
    aliases = {
        "s2a": "sentinel-2a",
        "s2b": "sentinel-2b",
        "s2c": "sentinel-2c",
        "s2d": "sentinel-2d",
        "sentinel2a": "sentinel-2a",
        "sentinel2b": "sentinel-2b",
        "sentinel2c": "sentinel-2c",
        "sentinel2d": "sentinel-2d",
    }
    compact = aliases.get(compact, compact)
    if compact not in {"sentinel-2a", "sentinel-2b", "sentinel-2c", "sentinel-2d"}:
        raise ConfigError("sentinel.platform must be S2A/S2B/S2C/S2D or sentinel-2a/.../2d")
    return compact


def _normalize_tile(value: str | None) -> str | None:
    if value is None:
        return None
    tile = value.upper()
    if tile.startswith("MGRS-"):
        tile = tile[5:]
    if tile.startswith("T"):
        tile = tile[1:]
    if len(tile) != 5 or not tile[:2].isdigit() or not tile[2:].isalpha():
        raise ConfigError("sentinel.tile_id must look like T33UVB, 33UVB, or MGRS-33UVB")
    return f"T{tile}"


def load_config(
    path: str | Path,
    *,
    today: date | None = None,
    environment: Mapping[str, str] | None = None,
) -> AppConfig:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise ConfigError(f"Configuration file does not exist: {source}")
    try:
        raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    except yaml.YAMLError as error:
        raise ConfigError(f"Invalid YAML in {source}: {error}") from error
    root_data = _mapping(raw, "root")
    project = _mapping(root_data.get("project", {}), "project")
    root_value = project.get("root", "..")
    project_root = _path(source.parent, root_value, "project.root")

    study = _mapping(root_data.get("study_area"), "study_area")
    study_config = StudyAreaConfig(
        name=_required_text(study, "name", "study_area"),
        geometry=_path(project_root, study.get("geometry"), "study_area.geometry"),
        search_feature=_required_text(study, "search_feature", "study_area"),
        processing_roi_feature=_required_text(study, "processing_roi_feature", "study_area"),
    )

    sentinel = _mapping(root_data.get("sentinel"), "sentinel")
    start = _date(sentinel.get("start_date"), "sentinel.start_date")
    open_end = sentinel.get("end_date") is None
    if open_end:
        end = today or utc_today()
    else:
        end = _date(sentinel.get("end_date"), "sentinel.end_date")
    if end < start:
        raise ConfigError("sentinel.end_date must not be earlier than sentinel.start_date")
    level = _required_text(sentinel, "processing_level", "sentinel").upper()
    if level != "L1C":
        raise ConfigError(
            "This archive implementation preserves Sentinel-2 L1C; processing_level must be L1C"
        )
    cloud_value = sentinel.get("max_scene_cloud_cover")
    try:
        cloud = None if cloud_value is None else float(cloud_value)
    except (TypeError, ValueError) as error:
        raise ConfigError(
            "sentinel.max_scene_cloud_cover must be null or a number between 0 and 100"
        ) from error
    if cloud is not None and not 0 <= cloud <= 100:
        raise ConfigError("sentinel.max_scene_cloud_cover must be null or between 0 and 100")
    collection = _required_text(sentinel, "collection", "sentinel")
    if collection != "sentinel-2-l1c":
        raise ConfigError("sentinel.collection must be sentinel-2-l1c for this L1C archive")
    sentinel_config = SentinelConfig(
        collection=collection,
        processing_level=level,
        start_date=start,
        end_date=end,
        end_date_was_open=open_end,
        platform=_normalize_platform(_optional_text(sentinel.get("platform"), "sentinel.platform")),
        tile_id=_normalize_tile(_optional_text(sentinel.get("tile_id"), "sentinel.tile_id")),
        require_full_processing_roi_coverage=_bool(
            sentinel.get("require_full_processing_roi_coverage", False),
            "sentinel.require_full_processing_roi_coverage",
        ),
        max_scene_cloud_cover=cloud,
    )

    api = _mapping(root_data.get("api"), "api")
    api_config = APIConfig(
        stac_search_url=_official_url(
            api.get("stac_search_url"),
            "api.stac_search_url",
            "stac.dataspace.copernicus.eu",
        ),
        catalogue_odata_url=_official_url(
            api.get(
                "catalogue_odata_url",
                "https://catalogue.dataspace.copernicus.eu/odata/v1",
            ),
            "api.catalogue_odata_url",
            "catalogue.dataspace.copernicus.eu",
        ),
        token_url=_official_url(
            api.get("token_url"),
            "api.token_url",
            "identity.dataspace.copernicus.eu",
        ),
        download_base_url=_official_url(
            api.get("download_base_url"),
            "api.download_base_url",
            "download.dataspace.copernicus.eu",
        ),
        request_timeout_seconds=_positive_int(
            api.get("request_timeout_seconds", 120), "api.request_timeout_seconds"
        ),
        page_size=_positive_int(api.get("page_size", 100), "api.page_size", maximum=1000),
    )

    download = _mapping(root_data.get("download"), "download")
    active_environment = os.environ if environment is None else environment
    download_directory = active_environment.get("S2VOMB_DOWNLOAD_DIRECTORY", "").strip()
    if not download_directory:
        download_directory = download.get("directory")
    layout = _required_text(download, "layout", "download")
    if layout not in {"tile/year", "tile", "year"}:
        raise ConfigError("download.layout must be 'tile/year', 'tile', or 'year'")
    try:
        backoff = float(download.get("backoff_seconds", 2.0))
    except (TypeError, ValueError) as error:
        raise ConfigError("download.backoff_seconds must be a non-negative number") from error
    if backoff < 0:
        raise ConfigError("download.backoff_seconds must be >= 0")
    download_config = DownloadConfig(
        directory=_path(project_root, download_directory, "download.directory"),
        layout=layout,
        workers=_positive_int(download.get("workers", 2), "download.workers", maximum=8),
        retries=_positive_int(download.get("retries", 5), "download.retries", minimum=0),
        backoff_seconds=backoff,
        chunk_size_mib=_positive_int(download.get("chunk_size_mib", 8), "download.chunk_size_mib"),
        verify_checksum=_bool(download.get("verify_checksum", True), "download.verify_checksum"),
        keep_source_archive=_bool(
            download.get("keep_source_archive", True), "download.keep_source_archive"
        ),
    )
    if not download_config.keep_source_archive:
        raise ConfigError("download.keep_source_archive must remain true for this source archive")

    catalogue = _mapping(root_data.get("catalogue"), "catalogue")
    catalogue_config = CatalogueConfig(
        directory=_path(project_root, catalogue.get("directory"), "catalogue.directory"),
        csv_name=_required_text(catalogue, "csv_name", "catalogue"),
        parquet_name=_required_text(catalogue, "parquet_name", "catalogue"),
        stac_name=_required_text(catalogue, "stac_name", "catalogue"),
        write_parquet=_bool(catalogue.get("write_parquet", True), "catalogue.write_parquet"),
    )

    provenance = _mapping(root_data.get("provenance"), "provenance")
    provenance_config = ProvenanceConfig(
        directory=_path(project_root, provenance.get("directory"), "provenance.directory")
    )
    return AppConfig(
        source_path=source,
        project_root=project_root,
        study_area=study_config,
        sentinel=sentinel_config,
        api=api_config,
        download=download_config,
        catalogue=catalogue_config,
        provenance=provenance_config,
    )
