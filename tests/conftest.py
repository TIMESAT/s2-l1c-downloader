from __future__ import annotations

import hashlib
import io
import zipfile
from dataclasses import replace
from datetime import date

import pytest

from s2l1c.config import load_config
from s2l1c.models import ProductRecord, normalize_stac_item


@pytest.fixture
def app_config(tmp_path):
    geometry = tmp_path / "area.geojson"
    geometry.write_text(
        """{
  "type": "FeatureCollection",
  "features": [
    {
      "type": "Feature", "id": "search", "properties": {"name": "Test lake"},
      "geometry": {
        "type": "Polygon",
        "coordinates": [[[13.5,55.6],[13.7,55.6],[13.7,55.8],[13.5,55.8],[13.5,55.6]]]
      }
    },
    {
      "type": "Feature", "id": "roi", "properties": {"name": "Test ROI"},
      "geometry": {
        "type": "Polygon",
        "coordinates": [[[13.4,55.5],[13.8,55.5],[13.8,55.9],[13.4,55.9],[13.4,55.5]]]
      }
    }
  ]
}
""",
        encoding="utf-8",
    )
    config_file = tmp_path / "test.yaml"
    config_file.write_text(
        f"""
project:
  root: .
study_area:
  name: Test Lake
  geometry: {geometry.name}
  search_feature: search
  processing_roi_feature: roi
sentinel:
  collection: sentinel-2-l1c
  processing_level: L1C
  start_date: '2017-01-01'
  end_date: null
  platform: null
  tile_id: null
  max_scene_cloud_cover: null
api:
  stac_search_url: https://stac.dataspace.copernicus.eu/v1/search
  token_url: https://identity.dataspace.copernicus.eu/token
  download_base_url: https://download.dataspace.copernicus.eu/odata/v1
  request_timeout_seconds: 30
  page_size: 2
download:
  directory: raw
  layout: tile/year
  workers: 1
  retries: 2
  backoff_seconds: 0
  chunk_size_mib: 1
  verify_checksum: true
  keep_source_archive: true
catalogue:
  directory: catalogue
  csv_name: catalogue.csv
  parquet_name: catalogue.parquet
  stac_name: catalogue.stac.json
  write_parquet: false
provenance:
  directory: logs/runs
""",
        encoding="utf-8",
    )
    return load_config(config_file, today=date(2026, 8, 17))


def make_stac_item(
    *,
    stac_id: str = "S2A_MSIL1C_20240102T102431_N0510_R065_T33UVB_20240102T120000",
    product_uuid: str = "11111111-2222-4333-8444-555555555555",
    acquired: str = "2024-01-02T10:24:31Z",
    size: int = 123,
    checksum: str = "",
):
    product_name = f"{stac_id}.SAFE"
    return {
        "type": "Feature",
        "id": stac_id,
        "collection": "sentinel-2-l1c",
        "bbox": [13.0, 55.0, 14.0, 56.0],
        "geometry": {
            "type": "Polygon",
            "coordinates": [[[13, 55], [14, 55], [14, 56], [13, 56], [13, 55]]],
        },
        "links": [{"rel": "self", "href": f"https://stac.example/items/{stac_id}"}],
        "assets": {
            "Product": {
                "href": (
                    "https://download.dataspace.copernicus.eu/odata/v1/"
                    f"Products({product_uuid})/$value"
                ),
                "file:size": size,
                "file:checksum": checksum,
                "file:local_path": f"{product_name}.zip",
                "roles": ["data", "metadata", "archive"],
            }
        },
        "properties": {
            "datetime": acquired,
            "published": "2024-01-02T12:00:00Z",
            "platform": "sentinel-2a",
            "grid:code": "MGRS-33UVB",
            "product:type": "S2MSI1C",
            "eo:cloud_cover": 41.2,
            "_private": {"product_name": product_name, "product_uuid": product_uuid},
        },
    }


@pytest.fixture
def product_record() -> ProductRecord:
    return normalize_stac_item(make_stac_item(), "geometry-hash")


def zip_payload() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("PRODUCT.SAFE/manifest.safe", "metadata")
        archive.writestr("PRODUCT.SAFE/GRANULE/test.jp2", b"pixels")
    return buffer.getvalue()


def md5_multihash(payload: bytes) -> str:
    return (b"\xd5\x01\x10" + hashlib.md5(payload).digest()).hex()


@pytest.fixture
def zipped_record(product_record):
    payload = zip_payload()
    return replace(
        product_record,
        product_size_bytes=len(payload),
        checksum=md5_multihash(payload),
        checksum_algorithm="md5",
    ), payload
