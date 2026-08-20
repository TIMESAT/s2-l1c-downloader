from __future__ import annotations

import json
from pathlib import Path

import pytest

from s2l1c.geometry import geometry_covers, load_geometry
from s2l1c.utils import GeometryError


def test_loads_named_geometry_and_stable_hash(app_config):
    first = load_geometry(app_config.study_area.geometry, "search")
    second = load_geometry(app_config.study_area.geometry, "search")

    assert first.name == "Test lake"
    assert first.bbox == (13.5, 55.6, 13.7, 55.8)
    assert first.sha256 == second.sha256
    assert first.as_feature()["geometry"]["type"] == "Polygon"


def test_rejects_missing_feature(app_config):
    with pytest.raises(GeometryError, match="was not found"):
        load_geometry(app_config.study_area.geometry, "missing")


def test_rejects_unclosed_polygon(tmp_path):
    path = tmp_path / "bad.geojson"
    path.write_text(
        json.dumps(
            {
                "type": "Feature",
                "id": "bad",
                "properties": {},
                "geometry": {
                    "type": "Polygon",
                    "coordinates": [[[13, 55], [14, 55], [14, 56], [13, 56]]],
                },
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(GeometryError, match="not closed"):
        load_geometry(path, "bad")


def test_geometry_covers_complete_roi_but_rejects_partial_swath():
    roi = {
        "type": "Polygon",
        "coordinates": [[[13.4, 55.6], [13.8, 55.6], [13.8, 55.8], [13.4, 55.8], [13.4, 55.6]]],
    }
    complete = {
        "type": "Polygon",
        "coordinates": [[[13, 55], [15, 55], [15, 56], [13, 56], [13, 55]]],
    }
    partial = {
        "type": "Polygon",
        "coordinates": [[[13.6, 55], [15, 55], [15, 56], [13.6, 56], [13.6, 55]]],
    }

    assert geometry_covers(complete, roi)
    assert not geometry_covers(partial, roi)


def test_erken_example_roi_contains_search_geometry():
    repository = Path(__file__).resolve().parents[1]
    geometry_path = repository / "config/erken.geojson"
    search = load_geometry(geometry_path, "erken-search")
    roi = load_geometry(geometry_path, "erken-processing-roi-5km")

    assert search.bbox == (18.469922, 59.827092, 18.659227, 59.86222)
    assert roi.bbox == (18.380777, 59.782354, 18.748406, 59.907034)
    assert geometry_covers(roi.geometry, search.geometry)
