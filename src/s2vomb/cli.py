"""Command-line entry point for catalogue, inventory, and download stages."""

from __future__ import annotations

import argparse
import csv
import io
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .catalogue import CatalogueStore, STACClient
from .config import AppConfig, load_config
from .download import DownloadBatchResult, download_products
from .geometry import GeometrySelection, load_geometry
from .inventory import calculate_inventory, render_inventory, write_inventory
from .models import (
    CATALOGUE_FIELDS,
    ProductRecord,
    filter_records_by_year,
    select_one_per_year_near_cloud,
)
from .provenance import RunContext, begin_run, configure_logging
from .utils import S2VombError, atomic_write_json, atomic_write_text


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="s2vomb",
        description="Discover, inventory, and download complete Sentinel-2 L1C products from CDSE.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command", required=True)

    search = subparsers.add_parser("search", help="query CDSE STAC and write a local catalogue")
    _common_arguments(search)
    search.add_argument(
        "--max-items",
        type=_positive_int,
        default=None,
        help="stop after N items (diagnostics only; omitted for a complete catalogue)",
    )

    inventory = subparsers.add_parser("inventory", help="summarize the local catalogue")
    _common_arguments(inventory)
    inventory.add_argument("--year", type=_year, help="summarize one acquisition year")

    download = subparsers.add_parser(
        "download", help="download complete L1C source ZIP archives from the local catalogue"
    )
    _common_arguments(download)
    download.add_argument("--year", type=_year, help="download one acquisition year")
    download.add_argument(
        "--dry-run",
        action="store_true",
        help="show exact targets without authenticating or downloading",
    )
    download.add_argument(
        "--yes", action="store_true", help="skip the interactive storage confirmation"
    )
    download.add_argument(
        "--one-per-year-near-cloud",
        type=_cloud_cover,
        metavar="PERCENT",
        help="select one product per year whose scene cloud cover is nearest PERCENT",
    )
    return parser


def _common_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", type=Path, required=True, help="path to YAML configuration")
    parser.add_argument("--verbose", action="store_true", help="show debug logging in the terminal")


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be >= 1")
    return parsed


def _year(value: str) -> int:
    parsed = int(value)
    if parsed < 2015 or parsed > 2200:
        raise argparse.ArgumentTypeError("must be a four-digit Sentinel-2 acquisition year")
    return parsed


def _cloud_cover(value: str) -> float:
    parsed = float(value)
    if not 0 <= parsed <= 100:
        raise argparse.ArgumentTypeError("must be between 0 and 100")
    return parsed


def _load_geometries(config: AppConfig) -> tuple[GeometrySelection, GeometrySelection]:
    search = load_geometry(config.study_area.geometry, config.study_area.search_feature)
    roi = load_geometry(config.study_area.geometry, config.study_area.processing_roi_feature)
    return search, roi


def _snapshot_catalogue(context: RunContext, records: list[ProductRecord]) -> None:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=CATALOGUE_FIELDS, lineterminator="\n")
    writer.writeheader()
    for record in records:
        writer.writerow(record.to_row())
    atomic_write_text(context.directory / "catalogue.csv", stream.getvalue())


def _search(args: argparse.Namespace, config: AppConfig, context: RunContext) -> int:
    logger = configure_logging(context, verbose=args.verbose)
    search_geometry, roi = _load_geometries(config)
    logger.info("Searching CDSE STAC for %s", config.study_area.name)
    result = STACClient(config).search(search_geometry, max_items=args.max_items)
    store = CatalogueStore(config)
    records = store.merge_previous_state(result.records)
    paths = store.write_all(records, feature_collection=result.feature_collection)
    inventory = calculate_inventory(records)
    inventory_path = write_inventory(config.catalogue.directory / "inventory.json", inventory)
    _snapshot_catalogue(context, records)
    atomic_write_json(context.directory / "catalogue.stac.json", result.feature_collection)
    atomic_write_json(context.directory / "inventory.json", inventory.to_dict())
    print(render_inventory(config, inventory, heading="CDSE catalogue search complete"))
    print(f"Catalogue CSV: {paths['csv']}")
    if paths["parquet"]:
        print(f"Catalogue Parquet: {paths['parquet']}")
    else:
        print(f"Catalogue Parquet: not written ({paths['parquet_note']})")
    print(f"Inventory JSON: {inventory_path}")
    print(f"Run manifest: {context.manifest_path}")
    context.finish(
        status="completed",
        catalogue={
            "products_discovered": len(records),
            "duplicates_removed": result.duplicates_removed,
            "pages_queried": result.pages,
            "max_items": args.max_items,
            "stac_request_body": result.request_body,
            "paths": paths,
        },
        processing_roi={
            "feature_id": roi.feature_id,
            "geometry_sha256": roi.sha256,
            "used_to_crop_downloads": False,
        },
        inventory_file=str(inventory_path),
    )
    return 0


def _inventory(args: argparse.Namespace, config: AppConfig, context: RunContext) -> int:
    configure_logging(context, verbose=args.verbose)
    records = filter_records_by_year(CatalogueStore(config).read(), args.year)
    inventory = calculate_inventory(records)
    filename = f"inventory-{args.year}.json" if args.year else "inventory.json"
    path = write_inventory(config.catalogue.directory / filename, inventory)
    atomic_write_json(context.directory / "inventory.json", inventory.to_dict())
    print(render_inventory(config, inventory, year=args.year))
    print(f"Inventory JSON: {path}")
    print(f"Run manifest: {context.manifest_path}")
    context.finish(
        status="completed",
        inventory_file=str(path),
        product_count=inventory.product_count,
        filters={"year": args.year},
    )
    return 0


def _confirm_download() -> bool:
    if not sys.stdin.isatty():
        raise S2VombError("Non-interactive downloads require --yes after reviewing the inventory")
    answer = input("Proceed with full-product downloads? [y/N] ").strip().lower()
    return answer in {"y", "yes"}


def _write_failures(context: RunContext, result: DownloadBatchResult) -> str | None:
    failures = [outcome for outcome in result.outcomes if outcome.status == "failed"]
    if not failures:
        return None
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream,
        fieldnames=["product_id", "path", "bytes_downloaded", "attempts", "error"],
        lineterminator="\n",
    )
    writer.writeheader()
    for failure in failures:
        writer.writerow(
            {
                "product_id": failure.product_id,
                "path": failure.path,
                "bytes_downloaded": failure.bytes_downloaded,
                "attempts": failure.attempts,
                "error": failure.error,
            }
        )
    path = context.directory / "failed-downloads.csv"
    atomic_write_text(path, stream.getvalue())
    return str(path)


def _write_download_results(context: RunContext, result: DownloadBatchResult) -> str:
    path = context.directory / "download-results.json"
    atomic_write_json(
        path,
        {
            "selected": result.selected,
            "downloaded": result.downloaded,
            "already_present": result.already_present,
            "failed": result.failed,
            "products": [
                {
                    "product_id": outcome.product_id,
                    "status": outcome.status,
                    "path": str(outcome.path),
                    "bytes_downloaded": outcome.bytes_downloaded,
                    "checksum_verified": outcome.checksum_verified,
                    "attempts": outcome.attempts,
                    "error": outcome.error,
                }
                for outcome in result.outcomes
            ],
        },
    )
    return str(path)


def _download(args: argparse.Namespace, config: AppConfig, context: RunContext) -> int:
    logger = configure_logging(context, verbose=args.verbose)
    store = CatalogueStore(config)
    records = filter_records_by_year(store.read(), args.year)
    if args.one_per_year_near_cloud is not None:
        records = select_one_per_year_near_cloud(records, args.one_per_year_near_cloud)
    inventory = calculate_inventory(records)
    print(render_inventory(config, inventory, heading="Download selection", year=args.year))
    if not records:
        print("No catalogue records match the requested filters.")
        context.finish(
            status="completed",
            filters={
                "year": args.year,
                "one_per_year_near_cloud": args.one_per_year_near_cloud,
            },
            download={"selected": 0},
        )
        return 0
    if not args.dry_run and not args.yes and not _confirm_download():
        print("Download cancelled; the catalogue was not changed.")
        context.finish(
            status="cancelled",
            filters={
                "year": args.year,
                "one_per_year_near_cloud": args.one_per_year_near_cloud,
            },
            download={"selected": 0},
        )
        return 0

    result = download_products(config, records, store, dry_run=args.dry_run, logger=logger)
    if args.dry_run:
        print("\nDry-run targets (no authentication or network download performed):")
        for outcome in result.outcomes:
            print(f"  {outcome.path}")
    else:
        print(
            f"Download result: {result.downloaded} downloaded, "
            f"{result.already_present} already complete, {result.failed} failed"
        )
    all_records = store.read()
    _snapshot_catalogue(context, all_records)
    results_file = _write_download_results(context, result)
    failure_file = _write_failures(context, result)
    context.finish(
        status="completed" if result.failed == 0 else "completed-with-failures",
        filters={
            "year": args.year,
            "one_per_year_near_cloud": args.one_per_year_near_cloud,
        },
        download={
            "dry_run": args.dry_run,
            "selected": result.selected,
            "downloaded": result.downloaded,
            "already_present": result.already_present,
            "failed": result.failed,
            "results_file": results_file,
            "failed_downloads_file": failure_file,
        },
    )
    print(f"Run manifest: {context.manifest_path}")
    return 1 if result.failed else 0


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    context: RunContext | None = None
    try:
        config = load_config(args.config)
        search_geometry = load_geometry(
            config.study_area.geometry, config.study_area.search_feature
        )
        query_geometry = None if config.sentinel.tile_id else search_geometry
        context = begin_run(config, args.command, query_geometry)
        if args.command == "search":
            return _search(args, config, context)
        if args.command == "inventory":
            return _inventory(args, config, context)
        if args.command == "download":
            return _download(args, config, context)
        raise S2VombError(f"Unsupported command: {args.command}")
    except (S2VombError, OSError, ValueError) as error:
        if context is not None:
            context.finish(
                status="failed",
                error={"type": type(error).__name__, "message": str(error)},
            )
            print(f"Run manifest: {context.manifest_path}", file=sys.stderr)
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
