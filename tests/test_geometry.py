from __future__ import annotations

import json

import pytest

from s2vomb.geometry import geometry_covers, load_geometry
from s2vomb.utils import GeometryError


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
