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


Point = tuple[float, float]
Ring = list[Point]
PolygonRings = list[Ring]


def _polygon_rings(geometry: dict[str, Any]) -> list[PolygonRings]:
    """Return Polygon/MultiPolygon coordinates as numeric rings."""
    kind = geometry.get("type")
    coordinates = geometry.get("coordinates")
    if not isinstance(coordinates, list):
        return []
    polygons = [coordinates] if kind == "Polygon" else coordinates if kind == "MultiPolygon" else []
    result: list[PolygonRings] = []
    for polygon in polygons:
        if not isinstance(polygon, list):
            continue
        rings: PolygonRings = []
        for ring in polygon:
            if not isinstance(ring, list):
                continue
            points = [
                (float(position[0]), float(position[1]))
                for position in ring
                if isinstance(position, list)
                and len(position) >= 2
                and isinstance(position[0], (int, float))
                and isinstance(position[1], (int, float))
            ]
            if len(points) >= 4 and points[0] == points[-1]:
                rings.append(points)
        if rings:
            result.append(rings)
    return result


def _point_on_segment(point: Point, start: Point, end: Point, epsilon: float = 1e-10) -> bool:
    cross = (point[0] - start[0]) * (end[1] - start[1]) - (
        point[1] - start[1]
    ) * (end[0] - start[0])
    return abs(cross) <= epsilon and (
        min(start[0], end[0]) - epsilon <= point[0] <= max(start[0], end[0]) + epsilon
        and min(start[1], end[1]) - epsilon <= point[1] <= max(start[1], end[1]) + epsilon
    )


def _point_in_ring(point: Point, ring: Ring) -> tuple[bool, bool]:
    """Return (inside, on_boundary) for a closed ring."""
    inside = False
    for start, end in zip(ring, ring[1:], strict=False):
        if _point_on_segment(point, start, end):
            return True, True
        if (start[1] > point[1]) != (end[1] > point[1]):
            crossing_x = (
                (end[0] - start[0])
                * (point[1] - start[1])
                / (end[1] - start[1])
                + start[0]
            )
            if point[0] < crossing_x:
                inside = not inside
    return inside, False


def _point_in_polygon(point: Point, polygon: PolygonRings) -> bool:
    in_exterior, on_exterior = _point_in_ring(point, polygon[0])
    if not in_exterior and not on_exterior:
        return False
    for hole in polygon[1:]:
        in_hole, on_hole = _point_in_ring(point, hole)
        if in_hole and not on_hole:
            return False
    return True


def _orientation(start: Point, end: Point, point: Point) -> float:
    return (end[0] - start[0]) * (point[1] - start[1]) - (
        end[1] - start[1]
    ) * (point[0] - start[0])


def _properly_crosses(a: Point, b: Point, c: Point, d: Point, epsilon: float = 1e-10) -> bool:
    first = _orientation(a, b, c)
    second = _orientation(a, b, d)
    third = _orientation(c, d, a)
    fourth = _orientation(c, d, b)
    return (
        (first > epsilon and second < -epsilon) or (first < -epsilon and second > epsilon)
    ) and ((third > epsilon and fourth < -epsilon) or (third < -epsilon and fourth > epsilon))


def _polygon_covers_ring(container: PolygonRings, target: Ring) -> bool:
    sample_points = list(target[:-1])
    sample_points.extend(
        ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)
        for start, end in zip(target, target[1:], strict=False)
    )
    if not all(_point_in_polygon(point, container) for point in sample_points):
        return False
    return not any(
        _properly_crosses(start, end, boundary_start, boundary_end)
        for start, end in zip(target, target[1:], strict=False)
        for boundary in container
        for boundary_start, boundary_end in zip(boundary, boundary[1:], strict=False)
    )


def geometry_covers(container: dict[str, Any], target: dict[str, Any]) -> bool:
    """Return whether a footprint fully contains every target polygon."""
    container_polygons = _polygon_rings(container)
    target_polygons = _polygon_rings(target)
    if not container_polygons or not target_polygons:
        return False
    return all(
        any(_polygon_covers_ring(candidate, polygon[0]) for candidate in container_polygons)
        for polygon in target_polygons
    )
