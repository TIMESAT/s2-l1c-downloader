"""Storage and temporal inventory summaries independent of live CDSE access."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean, median
from typing import Any

from .config import AppConfig
from .models import ProductRecord
from .utils import atomic_write_json, format_bytes, utc_now_iso


@dataclass(frozen=True, slots=True)
class InventorySummary:
    generated_at: str
    product_count: int
    products_per_year: dict[str, int]
    products_per_month: dict[str, int]
    platforms: dict[str, int]
    tile_ids: dict[str, int]
    cloud_cover: dict[str, Any]
    total_size_bytes: int
    products_with_unknown_size: int
    download_status: dict[str, int]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def calculate_inventory(records: Iterable[ProductRecord]) -> InventorySummary:
    products = list(records)
    clouds = [record.cloud_cover for record in products if record.cloud_cover is not None]
    cloud_bins = Counter({"0-10": 0, "10-25": 0, "25-50": 0, "50-75": 0, "75-100": 0})
    for cloud in clouds:
        if cloud <= 10:
            cloud_bins["0-10"] += 1
        elif cloud <= 25:
            cloud_bins["10-25"] += 1
        elif cloud <= 50:
            cloud_bins["25-50"] += 1
        elif cloud <= 75:
            cloud_bins["50-75"] += 1
        else:
            cloud_bins["75-100"] += 1
    cloud_summary: dict[str, Any] = {
        "count": len(clouds),
        "missing": len(products) - len(clouds),
        "minimum": min(clouds) if clouds else None,
        "maximum": max(clouds) if clouds else None,
        "mean": mean(clouds) if clouds else None,
        "median": median(clouds) if clouds else None,
        "bins": dict(cloud_bins),
    }
    sizes = [
        record.product_size_bytes for record in products if record.product_size_bytes is not None
    ]
    return InventorySummary(
        generated_at=utc_now_iso(),
        product_count=len(products),
        products_per_year=dict(sorted(Counter(str(record.year) for record in products).items())),
        products_per_month=dict(sorted(Counter(record.month for record in products).items())),
        platforms=dict(
            sorted(Counter(record.platform or "unknown" for record in products).items())
        ),
        tile_ids=dict(sorted(Counter(record.tile_id or "unknown" for record in products).items())),
        cloud_cover=cloud_summary,
        total_size_bytes=sum(sizes),
        products_with_unknown_size=len(products) - len(sizes),
        download_status=dict(
            sorted(Counter(record.download_status or "unknown" for record in products).items())
        ),
    )


def write_inventory(path: Path, inventory: InventorySummary) -> Path:
    atomic_write_json(path, inventory.to_dict())
    return path


def render_inventory(
    config: AppConfig,
    inventory: InventorySummary,
    *,
    heading: str = "Archive inventory",
    year: int | None = None,
) -> str:
    sentinel = config.sentinel
    end = "present" if sentinel.end_date_was_open else sentinel.end_date.isoformat()
    date_range = f"{sentinel.start_date.isoformat()} – {end}"
    if year is not None:
        date_range = f"{year}-01-01 – {year}-12-31"
    years = list(inventory.products_per_year)
    years_text = "none" if not years else f"{years[0]}–{years[-1]}"
    tiles = ", ".join(inventory.tile_ids) if inventory.tile_ids else "none"
    unknown = inventory.products_with_unknown_size
    size_text = format_bytes(inventory.total_size_bytes)
    if unknown:
        size_text += f" ({unknown} product size{'s' if unknown != 1 else ''} unknown)"
    cloud = inventory.cloud_cover
    cloud_text = "no metadata"
    if cloud["count"]:
        cloud_text = (
            f"{cloud['minimum']:.1f}–{cloud['maximum']:.1f}% "
            f"(median {cloud['median']:.1f}%; scene/tile metadata only)"
        )
    return "\n".join(
        [
            heading,
            f"Study area: {config.study_area.name}",
            f"Level: Sentinel-2 {sentinel.processing_level}",
            f"Date range: {date_range}",
            f"Products found: {inventory.product_count:,}",
            f"Estimated archive size: {size_text}",
            f"Years covered: {years_text}",
            f"Tile IDs: {tiles}",
            f"Platforms: {', '.join(inventory.platforms) if inventory.platforms else 'none'}",
            f"Scene cloud cover: {cloud_text}",
        ]
    )
