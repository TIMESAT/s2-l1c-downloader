"""Normalized catalogue record model and STAC-to-archive mapping."""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from dataclasses import dataclass, fields
from datetime import date
from typing import Any

from .utils import CatalogueError, parse_iso_datetime, parse_multihash

_PRODUCT_UUID = re.compile(r"Products\(([0-9a-fA-F-]{36})\)")


@dataclass(slots=True)
class ProductRecord:
    product_id: str
    stac_id: str
    product_name: str
    acquisition_datetime: str
    platform: str
    tile_id: str
    processing_level: str
    cloud_cover: float | None
    product_url: str
    product_size_bytes: int | None
    online_status: str
    checksum: str
    checksum_algorithm: str
    stac_item_url: str
    collection: str
    published: str
    search_geometry_sha256: str
    search_intersection: str
    item_bbox: str
    item_geometry: str
    download_status: str = "discovered"
    local_path: str = ""
    downloaded_bytes: int = 0
    downloaded_at: str = ""
    checksum_verified: bool = False
    last_error: str = ""
    attempts: int = 0

    @property
    def year(self) -> int:
        return parse_iso_datetime(self.acquisition_datetime).year

    @property
    def month(self) -> str:
        return parse_iso_datetime(self.acquisition_datetime).strftime("%Y-%m")

    def to_row(self) -> dict[str, Any]:
        return {field.name: getattr(self, field.name) for field in fields(self)}

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> ProductRecord:
        values: dict[str, Any] = {}
        integer_fields = {"product_size_bytes", "downloaded_bytes", "attempts"}
        for field in fields(cls):
            value = row.get(field.name, "")
            if field.name == "cloud_cover":
                values[field.name] = None if value in (None, "") else float(value)
            elif field.name == "product_size_bytes":
                values[field.name] = None if value in (None, "") else int(value)
            elif field.name in integer_fields:
                values[field.name] = int(value or 0)
            elif field.name == "checksum_verified":
                values[field.name] = value is True or str(value).lower() in {"1", "true", "yes"}
            else:
                values[field.name] = "" if value is None else str(value)
        return cls(**values)


CATALOGUE_FIELDS = [field.name for field in fields(ProductRecord)]


def _asset(item: dict[str, Any]) -> dict[str, Any]:
    assets = item.get("assets") if isinstance(item.get("assets"), dict) else {}
    for name in ("Product", "product", "archive"):
        candidate = assets.get(name)
        if isinstance(candidate, dict):
            return candidate
    for candidate in assets.values():
        if isinstance(candidate, dict) and "archive" in candidate.get("roles", []):
            return candidate
    return {}


def _self_link(item: dict[str, Any]) -> str:
    links = item.get("links") if isinstance(item.get("links"), list) else []
    for link in links:
        if isinstance(link, dict) and link.get("rel") == "self":
            return str(link.get("href", ""))
    return ""


def _tile_id(value: Any, stac_id: str) -> str:
    if isinstance(value, str) and value:
        tile = value.upper().removeprefix("MGRS-").removeprefix("T")
        return f"T{tile}"
    match = re.search(r"_T([0-9]{2}[A-Z]{3})_", stac_id)
    return f"T{match.group(1)}" if match else ""


def normalize_stac_item(
    item: dict[str, Any],
    search_geometry_sha256: str,
    *,
    search_method: str = "stac-intersects",
) -> ProductRecord:
    if not isinstance(item, dict) or item.get("type") != "Feature":
        raise CatalogueError("STAC search returned a non-Feature item")
    stac_id = str(item.get("id", ""))
    if not stac_id:
        raise CatalogueError("STAC item has no id")
    properties = item.get("properties") if isinstance(item.get("properties"), dict) else {}
    private = properties.get("_private") if isinstance(properties.get("_private"), dict) else {}
    asset = _asset(item)
    product_url = str(asset.get("href", ""))
    uuid_match = _PRODUCT_UUID.search(product_url)
    product_id = str(
        private.get("product_uuid") or (uuid_match.group(1) if uuid_match else stac_id)
    )
    local_path = str(asset.get("file:local_path", ""))
    product_name = str(private.get("product_name") or "")
    if not product_name and local_path.lower().endswith(".zip"):
        product_name = local_path[:-4]
    if not product_name:
        product_name = stac_id if stac_id.endswith(".SAFE") else f"{stac_id}.SAFE"

    acquired = str(properties.get("datetime") or properties.get("start_datetime") or "")
    if not acquired:
        raise CatalogueError(f"STAC item {stac_id} has no acquisition datetime")
    try:
        acquired = parse_iso_datetime(acquired).isoformat().replace("+00:00", "Z")
    except ValueError as error:
        raise CatalogueError(f"STAC item {stac_id} has an invalid acquisition datetime") from error

    size_value = asset.get("file:size", private.get("product_size"))
    try:
        size = int(size_value) if size_value not in (None, "") else None
    except (TypeError, ValueError):
        size = None
    cloud_value = properties.get("eo:cloud_cover")
    try:
        cloud = float(cloud_value) if cloud_value not in (None, "") else None
    except (TypeError, ValueError):
        cloud = None
    checksum = str(asset.get("file:checksum", ""))
    checksum_algorithm, _ = parse_multihash(checksum)
    product_type = str(properties.get("product:type", ""))
    collection = str(item.get("collection", ""))
    level = (
        "L1C"
        if product_type == "S2MSI1C" or collection.endswith("l1c")
        else str(properties.get("processing:level", ""))
    )
    bbox = item.get("bbox") if isinstance(item.get("bbox"), list) else []
    geometry = item.get("geometry") if isinstance(item.get("geometry"), dict) else {}
    online = properties.get("online")
    online_status = str(online).lower() if isinstance(online, bool) else str(online or "")
    return ProductRecord(
        product_id=product_id,
        stac_id=stac_id,
        product_name=product_name,
        acquisition_datetime=acquired,
        platform=str(properties.get("platform", "")),
        tile_id=_tile_id(properties.get("grid:code"), stac_id),
        processing_level=level,
        cloud_cover=cloud,
        product_url=product_url,
        product_size_bytes=size,
        online_status=online_status,
        checksum=checksum,
        checksum_algorithm=checksum_algorithm or "",
        stac_item_url=_self_link(item),
        collection=collection,
        published=str(properties.get("published", "")),
        search_geometry_sha256=search_geometry_sha256,
        search_intersection=search_method,
        item_bbox=json.dumps(bbox, separators=(",", ":")),
        item_geometry=json.dumps(geometry, separators=(",", ":")),
    )


def sort_records(records: Iterable[ProductRecord]) -> list[ProductRecord]:
    return sorted(records, key=lambda record: (record.acquisition_datetime, record.product_name))


def deduplicate_records(
    records: Iterable[ProductRecord],
) -> tuple[list[ProductRecord], list[str]]:
    unique: list[ProductRecord] = []
    seen: set[str] = set()
    duplicates: list[str] = []
    for record in sort_records(records):
        key = record.product_id or record.stac_id
        if key in seen:
            duplicates.append(record.product_id)
            continue
        seen.add(key)
        unique.append(record)
    return unique, duplicates


def filter_records_by_year(
    records: Iterable[ProductRecord], year: int | None
) -> list[ProductRecord]:
    if year is None:
        return list(records)
    return [record for record in records if record.year == year]


def filter_records_by_date(
    records: Iterable[ProductRecord],
    start_date: date | None,
    end_date: date | None,
) -> list[ProductRecord]:
    """Filter records inclusively by UTC acquisition date."""
    if start_date and end_date and end_date < start_date:
        raise ValueError("end date must not be earlier than start date")
    selected: list[ProductRecord] = []
    for record in records:
        acquired = parse_iso_datetime(record.acquisition_datetime).date()
        if start_date is not None and acquired < start_date:
            continue
        if end_date is not None and acquired > end_date:
            continue
        selected.append(record)
    return selected


def select_one_per_year_near_cloud(
    records: Iterable[ProductRecord], target_cloud_cover: float
) -> list[ProductRecord]:
    """Select one deterministic scene per year nearest a scene-cloud target."""
    if not 0 <= target_cloud_cover <= 100:
        raise ValueError("target cloud cover must be between 0 and 100")
    grouped: dict[int, list[ProductRecord]] = {}
    for record in records:
        if record.cloud_cover is not None:
            grouped.setdefault(record.year, []).append(record)
    return [
        min(
            grouped[year],
            key=lambda record: (
                abs(record.cloud_cover - target_cloud_cover),  # type: ignore[operator]
                record.acquisition_datetime,
                record.product_name,
            ),
        )
        for year in sorted(grouped)
    ]
