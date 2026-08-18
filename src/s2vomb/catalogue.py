"""Official CDSE STAC discovery and normalized local catalogue persistence."""

from __future__ import annotations

import csv
import io
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from . import __version__
from .config import AppConfig
from .geometry import GeometrySelection
from .models import (
    CATALOGUE_FIELDS,
    ProductRecord,
    deduplicate_records,
    normalize_stac_item,
    sort_records,
)
from .utils import CatalogueError, atomic_write_json, atomic_write_text, canonical_json


@dataclass(slots=True)
class SearchResult:
    records: list[ProductRecord]
    feature_collection: dict[str, Any]
    request_body: dict[str, Any]
    duplicates_removed: list[str]
    pages: int


def build_search_body(config: AppConfig, geometry: GeometrySelection) -> dict[str, Any]:
    sentinel = config.sentinel
    body: dict[str, Any] = {
        "collections": [sentinel.collection],
        "datetime": (
            f"{sentinel.start_date.isoformat()}T00:00:00Z/"
            f"{sentinel.end_date.isoformat()}T23:59:59.999999Z"
        ),
        "limit": config.api.page_size,
        "sortby": [{"field": "properties.datetime", "direction": "asc"}],
    }
    query: dict[str, Any] = {}
    if sentinel.platform:
        query["platform"] = {"eq": sentinel.platform}
    if sentinel.tile_id:
        query["grid:code"] = {"eq": f"MGRS-{sentinel.tile_id.removeprefix('T')}"}
    else:
        body["intersects"] = geometry.geometry
    if sentinel.max_scene_cloud_cover is not None:
        query["eo:cloud_cover"] = {"lte": sentinel.max_scene_cloud_cover}
    if query:
        body["query"] = query
    return body


def _retrying_session(retries: int) -> requests.Session:
    retry = Retry(
        total=retries,
        connect=retries,
        read=retries,
        status=retries,
        backoff_factor=1.0,
        status_forcelist=(408, 425, 429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session = requests.Session()
    session.headers["User-Agent"] = f"s2vomb/{__version__}"
    session.mount("https://", adapter)
    return session


class STACClient:
    def __init__(
        self,
        config: AppConfig,
        *,
        session_factory: Callable[[], requests.Session] | None = None,
    ) -> None:
        self.config = config
        self.session_factory = session_factory or (
            lambda: _retrying_session(config.download.retries)
        )

    def search(self, geometry: GeometrySelection, *, max_items: int | None = None) -> SearchResult:
        body = build_search_body(self.config, geometry)
        if max_items is not None:
            body["limit"] = min(body["limit"], max_items)
        session = self.session_factory()
        features: list[dict[str, Any]] = []
        pages = 0
        next_url = self.config.api.stac_search_url
        next_method = "POST"
        next_body: dict[str, Any] | None = body
        seen_pages: set[tuple[str, str, str]] = set()
        try:
            while next_url:
                page_fingerprint = (
                    next_method,
                    next_url,
                    canonical_json(next_body) if next_body is not None else "",
                )
                if page_fingerprint in seen_pages:
                    raise CatalogueError(f"STAC pagination loop detected at {next_url}")
                seen_pages.add(page_fingerprint)
                self._validate_page_url(next_url)
                try:
                    if next_method == "POST":
                        response = session.post(
                            next_url,
                            json=next_body,
                            timeout=self.config.api.request_timeout_seconds,
                        )
                    else:
                        response = session.get(
                            next_url, timeout=self.config.api.request_timeout_seconds
                        )
                    response.raise_for_status()
                    page = response.json()
                except requests.RequestException as error:
                    raise CatalogueError(f"CDSE STAC search failed: {error}") from error
                except ValueError as error:
                    raise CatalogueError("CDSE STAC search returned invalid JSON") from error
                if not isinstance(page, dict) or not isinstance(page.get("features"), list):
                    raise CatalogueError(
                        "CDSE STAC response has no FeatureCollection features array"
                    )
                pages += 1
                for feature in page["features"]:
                    if isinstance(feature, dict):
                        features.append(feature)
                        if max_items is not None and len(features) >= max_items:
                            break
                if max_items is not None and len(features) >= max_items:
                    features = features[:max_items]
                    break
                next_url = ""
                next_method = "GET"
                next_body = None
                links = page.get("links") if isinstance(page.get("links"), list) else []
                for link in links:
                    if isinstance(link, dict) and link.get("rel") == "next":
                        next_url = str(link.get("href", ""))
                        next_method = str(link.get("method", "GET")).upper()
                        next_body = link.get("body") if isinstance(link.get("body"), dict) else None
                        if next_method not in {"GET", "POST"}:
                            raise CatalogueError(
                                f"Unsupported STAC pagination method: {next_method}"
                            )
                        break
        finally:
            session.close()

        tile_search = self.config.sentinel.tile_id is not None
        normalized = [
            normalize_stac_item(
                item,
                "" if tile_search else geometry.sha256,
                search_method="stac-tile-query" if tile_search else "stac-intersects",
            )
            for item in features
        ]
        records, duplicates = deduplicate_records(normalized)
        collection = {
            "type": "FeatureCollection",
            "features": features,
            "numberReturned": len(features),
            "s2vomb:search": body,
        }
        return SearchResult(records, collection, body, duplicates, pages)

    def _validate_page_url(self, url: str) -> None:
        expected = urlparse(self.config.api.stac_search_url)
        candidate = urlparse(url)
        if candidate.scheme != "https" or candidate.hostname != expected.hostname:
            raise CatalogueError(f"Refusing an untrusted STAC pagination URL: {url}")


_STATE_FIELDS = {
    "download_status",
    "local_path",
    "downloaded_bytes",
    "downloaded_at",
    "checksum_verified",
    "last_error",
    "attempts",
}


class CatalogueStore:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.csv_path = config.catalogue.csv_path

    def read(self) -> list[ProductRecord]:
        if not self.csv_path.is_file():
            raise CatalogueError(
                f"Catalogue not found: {self.csv_path}. Run 's2vomb search --config ...' first."
            )
        try:
            with self.csv_path.open("r", encoding="utf-8", newline="") as stream:
                reader = csv.DictReader(stream)
                missing = set(CATALOGUE_FIELDS) - set(reader.fieldnames or [])
                if missing:
                    raise CatalogueError(
                        f"Catalogue {self.csv_path} is missing fields: {', '.join(sorted(missing))}"
                    )
                return sort_records(ProductRecord.from_row(dict(row)) for row in reader)
        except OSError as error:
            raise CatalogueError(f"Could not read {self.csv_path}: {error}") from error

    def read_if_present(self) -> list[ProductRecord]:
        return self.read() if self.csv_path.is_file() else []

    def merge_previous_state(
        self, new_records: list[ProductRecord], old_records: list[ProductRecord] | None = None
    ) -> list[ProductRecord]:
        old = old_records if old_records is not None else self.read_if_present()
        by_id = {record.product_id: record for record in old}
        by_name = {record.product_name: record for record in old}
        for record in new_records:
            previous = by_id.get(record.product_id) or by_name.get(record.product_name)
            if previous:
                for name in _STATE_FIELDS:
                    setattr(record, name, getattr(previous, name))
        return sort_records(new_records)

    def write_csv(self, records: list[ProductRecord]) -> Path:
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=CATALOGUE_FIELDS, lineterminator="\n")
        writer.writeheader()
        for record in sort_records(records):
            writer.writerow(record.to_row())
        atomic_write_text(self.csv_path, stream.getvalue())
        return self.csv_path

    def write_parquet(self, records: list[ProductRecord]) -> tuple[Path | None, str]:
        if not self.config.catalogue.write_parquet:
            return None, "disabled by configuration"
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError:
            return None, "pyarrow is not installed; install s2vomb[parquet]"
        destination = self.config.catalogue.parquet_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.tmp")
        table = pa.Table.from_pylist([record.to_row() for record in sort_records(records)])
        pq.write_table(table, temporary)
        os.replace(temporary, destination)
        return destination, "written"

    def write_stac(self, feature_collection: dict[str, Any]) -> Path:
        atomic_write_json(self.config.catalogue.stac_path, feature_collection)
        return self.config.catalogue.stac_path

    def write_all(
        self,
        records: list[ProductRecord],
        *,
        feature_collection: dict[str, Any] | None = None,
    ) -> dict[str, str | None]:
        csv_path = self.write_csv(records)
        parquet_path, parquet_note = self.write_parquet(records)
        stac_path = self.write_stac(feature_collection) if feature_collection is not None else None
        return {
            "csv": str(csv_path),
            "parquet": str(parquet_path) if parquet_path else None,
            "parquet_note": parquet_note,
            "stac": str(stac_path) if stac_path else None,
        }
