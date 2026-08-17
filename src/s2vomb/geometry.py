"""GeoJSON loading without heavyweight GIS dependencies."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .utils import GeometryError, canonical_json, sha256_text


@dataclass(frozen=True, slots=True)
class GeometrySelection:
    source: Path
    feature_id: str
    name: str
    geometry: dict[str, Any]
    bbox: tuple[float, float, float, float]
    sha256: str

    def as_feature(self) -> dict[str, Any]:
        return {
            "type": "Feature",
            "id": self.feature_id,
            "properties": {"name": self.name},
            "geometry": self.geometry,
            "bbox": list(self.bbox),
        }


def _positions(value: Any) -> Iterator[tuple[float, float]]:
    if (
        isinstance(value, list)
        and len(value) >= 2
        and all(isinstance(item, (int, float)) and not isinstance(item, bool) for item in value[:2])
    ):
        yield float(value[0]), float(value[1])
        return
    if isinstance(value, list):
        for child in value:
            yield from _positions(child)


def _validate_geometry(geometry: Any) -> dict[str, Any]:
    if not isinstance(geometry, dict):
        raise GeometryError("GeoJSON feature has no geometry object")
    kind = geometry.get("type")
    if kind not in {"Polygon", "MultiPolygon"}:
        raise GeometryError(f"Search geometries must be Polygon or MultiPolygon, not {kind!r}")
    positions = list(_positions(geometry.get("coordinates")))
    if len(positions) < 4:
        raise GeometryError("GeoJSON geometry contains too few coordinate positions")
    for longitude, latitude in positions:
        if not -180 <= longitude <= 180 or not -90 <= latitude <= 90:
            raise GeometryError("GeoJSON coordinates must be WGS84 longitude/latitude")
    if positions[0] != positions[-1] and kind == "Polygon":
        raise GeometryError("GeoJSON polygon exterior ring is not closed")
    return geometry


def load_geometry(path: str | Path, feature_id: str) -> GeometrySelection:
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise GeometryError(f"Geometry file does not exist: {source}")
    try:
        document = json.loads(source.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise GeometryError(f"Invalid GeoJSON in {source}: {error}") from error
    if not isinstance(document, dict):
        raise GeometryError("GeoJSON root must be an object")

    feature: dict[str, Any] | None = None
    if document.get("type") == "FeatureCollection":
        features = document.get("features")
        if not isinstance(features, list):
            raise GeometryError("GeoJSON FeatureCollection has no features array")
        for candidate in features:
            if isinstance(candidate, dict) and str(candidate.get("id", "")) == feature_id:
                feature = candidate
                break
        if feature is None:
            raise GeometryError(f"Feature {feature_id!r} was not found in {source}")
    elif document.get("type") == "Feature":
        if str(document.get("id", "")) != feature_id:
            raise GeometryError(
                f"Configured feature {feature_id!r} does not match the GeoJSON feature"
            )
        feature = document
    else:
        raise GeometryError("GeoJSON must be a Feature or FeatureCollection")

    geometry = _validate_geometry(feature.get("geometry"))
    positions = list(_positions(geometry["coordinates"]))
    longitudes = [point[0] for point in positions]
    latitudes = [point[1] for point in positions]
    bbox = (min(longitudes), min(latitudes), max(longitudes), max(latitudes))
    properties = feature.get("properties") if isinstance(feature.get("properties"), dict) else {}
    name = str(properties.get("name") or feature_id)
    canonical = canonical_json(geometry)
    return GeometrySelection(source, feature_id, name, geometry, bbox, sha256_text(canonical))
