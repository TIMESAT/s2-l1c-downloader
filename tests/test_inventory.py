from __future__ import annotations

from dataclasses import replace

from s2vomb.inventory import calculate_inventory, render_inventory


def test_inventory_calculations(app_config, product_record):
    second = replace(
        product_record,
        product_id="aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee",
        stac_id="second",
        acquisition_datetime="2025-07-08T10:00:00Z",
        platform="sentinel-2b",
        tile_id="T33UUB",
        cloud_cover=8.0,
        product_size_bytes=200,
        download_status="completed",
    )
    missing_size = replace(
        product_record,
        product_id="99999999-8888-4777-8666-555555555555",
        stac_id="third",
        acquisition_datetime="2025-07-18T10:00:00Z",
        cloud_cover=None,
        product_size_bytes=None,
    )

    inventory = calculate_inventory([product_record, second, missing_size])

    assert inventory.product_count == 3
    assert inventory.products_per_year == {"2024": 1, "2025": 2}
    assert inventory.products_per_month == {"2024-01": 1, "2025-07": 2}
    assert inventory.platforms == {"sentinel-2a": 2, "sentinel-2b": 1}
    assert inventory.tile_ids == {"T33UUB": 1, "T33UVB": 2}
    assert inventory.total_size_bytes == 323
    assert inventory.products_with_unknown_size == 1
    assert inventory.cloud_cover["bins"]["0-10"] == 1
    assert inventory.cloud_cover["missing"] == 1
    output = render_inventory(app_config, inventory)
    assert "Study area: Test Lake" in output
    assert "Products found: 3" in output
    assert "scene/tile metadata only" in output
