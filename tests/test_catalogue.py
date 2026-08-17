from __future__ import annotations

from dataclasses import replace

from conftest import make_stac_item

from s2vomb.catalogue import CatalogueStore, STACClient, build_search_body
from s2vomb.geometry import load_geometry
from s2vomb.models import (
    deduplicate_records,
    filter_records_by_year,
    normalize_stac_item,
)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"unexpected HTTP {self.status_code}")

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, pages):
        self.pages = iter(pages)
        self.posts = []
        self.gets = []
        self.closed = False

    def post(self, url, json, timeout):
        self.posts.append((url, json, timeout))
        return FakeResponse(next(self.pages))

    def get(self, url, timeout):
        self.gets.append((url, timeout))
        return FakeResponse(next(self.pages))

    def close(self):
        self.closed = True


def test_search_body_has_intersection_and_no_default_cloud_filter(app_config):
    geometry = load_geometry(app_config.study_area.geometry, "search")
    body = build_search_body(app_config, geometry)

    assert body["collections"] == ["sentinel-2-l1c"]
    assert body["intersects"] == geometry.geometry
    assert body["datetime"].startswith("2017-01-01T00:00:00Z/")
    assert "query" not in body


def test_stac_pagination_and_normalization(app_config):
    first = make_stac_item()
    second = make_stac_item(
        stac_id="S2B_MSIL1C_20240203T102431_N0510_R065_T33UVB_20240203T120000",
        product_uuid="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        acquired="2024-02-03T10:24:31Z",
    )
    next_url = "https://stac.dataspace.copernicus.eu/v1/search?token=next"
    pages = [
        {
            "type": "FeatureCollection",
            "features": [first],
            "links": [{"rel": "next", "href": next_url}],
        },
        {"type": "FeatureCollection", "features": [second], "links": []},
    ]
    session = FakeSession(pages)
    geometry = load_geometry(app_config.study_area.geometry, "search")

    result = STACClient(app_config, session_factory=lambda: session).search(geometry)

    assert [record.stac_id for record in result.records] == [first["id"], second["id"]]
    assert result.records[0].product_id == "11111111-2222-4333-8444-555555555555"
    assert result.records[0].product_name.endswith(".SAFE")
    assert result.records[0].tile_id == "T33UVB"
    assert result.records[0].processing_level == "L1C"
    assert result.records[0].cloud_cover == 41.2
    assert result.pages == 2
    assert session.closed


def test_duplicate_detection_and_previous_state(app_config):
    record = normalize_stac_item(make_stac_item(), "hash")
    unique, duplicates = deduplicate_records([record, replace(record)])
    assert len(unique) == 1
    assert duplicates == [record.product_id]

    record.download_status = "completed"
    record.local_path = "/archive/product.zip"
    store = CatalogueStore(app_config)
    store.write_csv([record])
    refreshed = normalize_stac_item(make_stac_item(size=999), "new-hash")
    merged = store.merge_previous_state([refreshed])
    assert merged[0].product_size_bytes == 999
    assert merged[0].download_status == "completed"
    assert merged[0].local_path == "/archive/product.zip"


def test_year_filtering(product_record):
    older = replace(
        product_record,
        product_id="00000000-0000-4000-8000-000000000000",
        stac_id="older",
        acquisition_datetime="2022-06-01T10:00:00Z",
    )
    assert filter_records_by_year([product_record, older], 2024) == [product_record]
    assert len(filter_records_by_year([product_record, older], None)) == 2
