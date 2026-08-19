"""Per-command scientific provenance manifests and credential-free effective config."""

from __future__ import annotations

import logging
import platform
import sys
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import __version__
from .config import AppConfig
from .geometry import GeometrySelection
from .utils import atomic_write_json, atomic_write_text, sha256_text, utc_now, utc_now_iso


@dataclass(slots=True)
class RunContext:
    config: AppConfig
    command: str
    run_id: str
    directory: Path
    manifest: dict[str, Any] = field(default_factory=dict)

    @property
    def manifest_path(self) -> Path:
        return self.directory / "manifest.json"

    @property
    def effective_config_path(self) -> Path:
        return self.directory / "effective-config.yaml"

    @property
    def log_path(self) -> Path:
        return self.directory / "run.log"

    def finish(self, *, status: str = "completed", **details: Any) -> Path:
        self.manifest.update(details)
        self.manifest["status"] = status
        self.manifest["finished_at"] = utc_now_iso()
        atomic_write_json(self.manifest_path, self.manifest)
        return self.manifest_path


def begin_run(
    config: AppConfig, command: str, geometry: GeometrySelection | None = None
) -> RunContext:
    stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    run_id = f"{stamp}_{command}_{uuid.uuid4().hex[:8]}"
    directory = config.provenance.directory / run_id
    directory.mkdir(parents=True, exist_ok=False)
    effective = config.effective_dict()
    atomic_write_text(
        directory / "effective-config.yaml",
        yaml.safe_dump(effective, sort_keys=False, allow_unicode=True),
    )
    source_text = config.source_path.read_text(encoding="utf-8")
    query_geometry: dict[str, Any] | None = None
    if geometry:
        query_geometry = geometry.as_feature()
        atomic_write_json(directory / "query-geometry.geojson", query_geometry)
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "run_id": run_id,
        "command": command,
        "status": "running",
        "started_at": utc_now_iso(),
        "package_version": __version__,
        "python_version": platform.python_version(),
        "platform": sys.platform,
        "source_config": str(config.source_path),
        "source_config_sha256": sha256_text(source_text),
        "effective_config": str(directory / "effective-config.yaml"),
        "query_geometry_file": str(directory / "query-geometry.geojson") if geometry else None,
        "query_geometry_sha256": geometry.sha256 if geometry else None,
        "query": {
            "collection": config.sentinel.collection,
            "processing_level": config.sentinel.processing_level,
            "spatial_mode": (
                "tile+full-processing-roi"
                if config.sentinel.tile_id
                and config.sentinel.require_full_processing_roi_coverage
                else "tile+intersects"
                if config.sentinel.tile_id
                else "full-processing-roi"
                if config.sentinel.require_full_processing_roi_coverage
                else "geometry-intersects"
            ),
            "start_date": config.sentinel.start_date.isoformat(),
            "end_date": config.sentinel.end_date.isoformat(),
            "end_date_was_open": config.sentinel.end_date_was_open,
            "platform": config.sentinel.platform,
            "tile_id": config.sentinel.tile_id,
            "require_full_processing_roi_coverage": (
                config.sentinel.require_full_processing_roi_coverage
            ),
            "max_scene_cloud_cover": config.sentinel.max_scene_cloud_cover,
        },
    }
    context = RunContext(config, command, run_id, directory, manifest)
    atomic_write_json(context.manifest_path, manifest)
    return context


def configure_logging(context: RunContext, *, verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger("s2vomb")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    file_handler = logging.FileHandler(context.log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    terminal = logging.StreamHandler()
    terminal.setLevel(logging.DEBUG if verbose else logging.INFO)
    terminal.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))
    logger.addHandler(terminal)
    return logger
