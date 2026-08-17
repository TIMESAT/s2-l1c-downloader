"""Restart-safe, conservative full-product archive downloads from CDSE OData."""

from __future__ import annotations

import logging
import os
import re
import time
import uuid
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import requests

from . import __version__
from .auth import TokenManager
from .catalogue import CatalogueStore
from .config import AppConfig
from .models import ProductRecord
from .utils import (
    AuthenticationError,
    DownloadError,
    checksum_file,
    parse_multihash,
    safe_product_filename,
    utc_now,
    utc_now_iso,
)

_TRANSIENT_STATUSES = {408, 425, 429, 500, 502, 503, 504}
_CONTENT_RANGE = re.compile(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class VerificationResult:
    valid: bool
    checksum_verified: bool
    size: int
    reason: str = ""


@dataclass(frozen=True, slots=True)
class DownloadOutcome:
    product_id: str
    status: str
    path: Path
    bytes_downloaded: int
    checksum_verified: bool
    attempts: int
    error: str = ""


@dataclass(slots=True)
class DownloadBatchResult:
    outcomes: list[DownloadOutcome]
    selected: int
    downloaded: int
    already_present: int
    failed: int


def product_target(config: AppConfig, record: ProductRecord) -> Path:
    filename = safe_product_filename(record.product_name)
    year = str(record.year)
    if config.download.layout == "tile/year":
        tile = record.tile_id or "unknown-tile"
        if not re.fullmatch(r"T[0-9]{2}[A-Z]{3}|unknown-tile", tile):
            raise DownloadError(f"Unsafe or invalid tile ID in catalogue: {tile!r}")
        return config.download.directory / tile / year / filename
    return config.download.directory / year / filename


def verify_local_file(
    path: Path,
    record: ProductRecord,
    *,
    verify_checksum: bool = True,
    expected_size: int | None = None,
) -> VerificationResult:
    if not path.is_file():
        return VerificationResult(False, False, 0, "file does not exist")
    size = path.stat().st_size
    required_size = record.product_size_bytes if expected_size is None else expected_size
    if required_size is not None and size != required_size:
        return VerificationResult(
            False, False, size, f"size mismatch: expected {required_size}, found {size}"
        )
    if size == 0:
        return VerificationResult(False, False, 0, "file is empty")
    if not zipfile.is_zipfile(path):
        return VerificationResult(False, False, size, "file is not a readable ZIP archive")

    algorithm, expected_digest = parse_multihash(record.checksum)
    checksum_verified = False
    if verify_checksum and record.checksum and algorithm and expected_digest:
        actual_digest = checksum_file(path, algorithm)
        if actual_digest.lower() != expected_digest.lower():
            return VerificationResult(False, False, size, f"{algorithm} checksum mismatch")
        checksum_verified = True
    elif required_size is None:
        try:
            with zipfile.ZipFile(path) as archive:
                corrupt_member = archive.testzip()
        except (OSError, zipfile.BadZipFile) as error:
            return VerificationResult(False, False, size, f"ZIP integrity check failed: {error}")
        if corrupt_member:
            return VerificationResult(
                False, False, size, f"ZIP member failed CRC check: {corrupt_member}"
            )
    return VerificationResult(True, checksum_verified, size)


def _download_url(config: AppConfig, record: ProductRecord) -> str:
    try:
        product_uuid = str(uuid.UUID(record.product_id))
    except (ValueError, AttributeError):
        product_uuid = ""
    if product_uuid:
        return f"{config.api.download_base_url}/Products({product_uuid})/$value"
    candidate = urlparse(record.product_url)
    trusted = urlparse(config.api.download_base_url)
    if (
        candidate.scheme == "https"
        and candidate.hostname
        and candidate.hostname == trusted.hostname
        and "/Products(" in candidate.path
    ):
        return record.product_url
    raise DownloadError(
        f"Product {record.product_name} has no valid CDSE OData UUID or trusted download URL"
    )


def _response_total(response: requests.Response, offset: int) -> int | None:
    content_range = response.headers.get("Content-Range", "")
    match = _CONTENT_RANGE.fullmatch(content_range.strip())
    if match and match.group(3) != "*":
        return int(match.group(3))
    length = response.headers.get("Content-Length", "")
    try:
        return offset + int(length)
    except ValueError:
        return None


def _retry_delay(response: requests.Response | None, base: float, attempt: int) -> float:
    if response is not None:
        retry_after = response.headers.get("Retry-After", "")
        try:
            return min(120.0, max(0.0, float(retry_after)))
        except ValueError:
            pass
    return min(120.0, base * (2 ** max(0, attempt - 1)))


def _download_one(
    record: ProductRecord,
    config: AppConfig,
    tokens: TokenManager,
    *,
    session_factory: Callable[[], requests.Session] = requests.Session,
    sleep: Callable[[float], None] = time.sleep,
) -> DownloadOutcome:
    target = product_target(config, record)
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(f"{target.name}.part")
    url = _download_url(config, record)
    log = logging.getLogger("s2vomb")
    latest_error = "download did not start"
    attempts_used = 0
    max_attempts = config.download.retries + 1
    session = session_factory()
    session.headers["User-Agent"] = f"s2vomb/{__version__}"
    try:
        for attempt in range(1, max_attempts + 1):
            attempts_used = attempt
            response: requests.Response | None = None
            try:
                offset = partial.stat().st_size if partial.is_file() else 0
                headers = {"Authorization": f"Bearer {tokens.get_token()}"}
                if offset:
                    headers["Range"] = f"bytes={offset}-"
                response = session.get(
                    url,
                    headers=headers,
                    stream=True,
                    timeout=config.api.request_timeout_seconds,
                    allow_redirects=True,
                )
                if response.status_code == 401:
                    tokens.invalidate()
                    latest_error = "CDSE rejected the access token"
                    log.warning(
                        "Refreshing rejected CDSE token for %s (attempt %s/%s)",
                        record.product_name,
                        attempt,
                        max_attempts,
                    )
                    continue
                if response.status_code == 416 and partial.is_file():
                    verification = verify_local_file(
                        partial, record, verify_checksum=config.download.verify_checksum
                    )
                    if verification.valid:
                        os.replace(partial, target)
                        return DownloadOutcome(
                            record.product_id,
                            "completed",
                            target,
                            verification.size,
                            verification.checksum_verified,
                            attempt,
                        )
                    partial.unlink(missing_ok=True)
                    latest_error = f"server rejected resume range; {verification.reason}"
                    if attempt < max_attempts:
                        log.warning(
                            "Restarting rejected partial archive for %s (attempt %s/%s)",
                            record.product_name,
                            attempt,
                            max_attempts,
                        )
                        sleep(_retry_delay(response, config.download.backoff_seconds, attempt))
                        continue
                if response.status_code in _TRANSIENT_STATUSES:
                    latest_error = f"transient HTTP {response.status_code}"
                    if attempt < max_attempts:
                        log.warning(
                            "Retrying %s after HTTP %s (attempt %s/%s)",
                            record.product_name,
                            response.status_code,
                            attempt,
                            max_attempts,
                        )
                        sleep(_retry_delay(response, config.download.backoff_seconds, attempt))
                        continue
                if response.status_code not in {200, 206}:
                    raise DownloadError(
                        f"CDSE returned HTTP {response.status_code} for {record.product_name}"
                    )

                mode = "ab" if response.status_code == 206 and offset else "wb"
                if response.status_code == 206:
                    content_range = response.headers.get("Content-Range", "")
                    match = _CONTENT_RANGE.fullmatch(content_range.strip())
                    if offset and (not match or int(match.group(1)) != offset):
                        raise DownloadError(
                            f"Invalid Content-Range while resuming {record.product_name}: "
                            f"{content_range!r}"
                        )
                else:
                    offset = 0
                expected_from_http = _response_total(response, offset)
                with partial.open(mode) as stream:
                    for chunk in response.iter_content(
                        chunk_size=config.download.chunk_size_mib * 1024 * 1024
                    ):
                        if chunk:
                            stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                verification = verify_local_file(
                    partial,
                    record,
                    verify_checksum=config.download.verify_checksum,
                    expected_size=record.product_size_bytes or expected_from_http,
                )
                if not verification.valid:
                    latest_error = verification.reason
                    expected_total = record.product_size_bytes or expected_from_http
                    if expected_total is not None and verification.size >= expected_total:
                        partial.unlink(missing_ok=True)
                    if attempt < max_attempts:
                        log.warning(
                            "Retrying verification for %s: %s (attempt %s/%s)",
                            record.product_name,
                            latest_error,
                            attempt,
                            max_attempts,
                        )
                        sleep(_retry_delay(response, config.download.backoff_seconds, attempt))
                        continue
                    break
                os.replace(partial, target)
                return DownloadOutcome(
                    record.product_id,
                    "completed",
                    target,
                    verification.size,
                    verification.checksum_verified,
                    attempt,
                )
            except requests.RequestException as error:
                latest_error = f"network error: {error}"
                if attempt < max_attempts:
                    log.warning(
                        "Retrying network transfer for %s (attempt %s/%s): %s",
                        record.product_name,
                        attempt,
                        max_attempts,
                        error,
                    )
                    sleep(_retry_delay(response, config.download.backoff_seconds, attempt))
                    continue
            except (AuthenticationError, DownloadError) as error:
                latest_error = str(error)
                break
            except OSError as error:
                latest_error = f"local I/O error: {error}"
                break
            finally:
                if response is not None:
                    response.close()
    finally:
        session.close()
    return DownloadOutcome(
        record.product_id,
        "failed",
        partial if partial.is_file() else target,
        partial.stat().st_size if partial.is_file() else 0,
        False,
        attempts_used,
        latest_error,
    )


def _apply_outcome(record: ProductRecord, outcome: DownloadOutcome) -> None:
    record.attempts += outcome.attempts
    record.downloaded_bytes = outcome.bytes_downloaded
    record.local_path = str(outcome.path)
    record.last_error = outcome.error
    if outcome.status == "completed":
        record.download_status = "completed"
        record.downloaded_at = utc_now_iso()
        record.checksum_verified = outcome.checksum_verified
    elif outcome.status == "failed":
        record.download_status = "failed"
        record.checksum_verified = False


def _quarantine_invalid(path: Path) -> Path:
    stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
    candidate = path.with_name(f"{path.name}.invalid-{stamp}")
    counter = 1
    while candidate.exists():
        candidate = path.with_name(f"{path.name}.invalid-{stamp}-{counter}")
        counter += 1
    path.replace(candidate)
    return candidate


def download_products(
    config: AppConfig,
    records: list[ProductRecord],
    store: CatalogueStore,
    *,
    dry_run: bool = False,
    logger: logging.Logger | None = None,
    token_manager: TokenManager | None = None,
    session_factory: Callable[[], requests.Session] = requests.Session,
) -> DownloadBatchResult:
    log = logger or logging.getLogger("s2vomb")
    if dry_run:
        outcomes = [
            DownloadOutcome(
                record.product_id, "planned", product_target(config, record), 0, False, 0
            )
            for record in records
        ]
        return DownloadBatchResult(outcomes, len(records), 0, 0, 0)

    outcomes: list[DownloadOutcome] = []
    pending: list[ProductRecord] = []
    for record in records:
        target = product_target(config, record)
        if target.is_file():
            verification = verify_local_file(
                target, record, verify_checksum=config.download.verify_checksum
            )
            if verification.valid:
                outcome = DownloadOutcome(
                    record.product_id,
                    "already-present",
                    target,
                    verification.size,
                    verification.checksum_verified,
                    0,
                )
                record.download_status = "completed"
                record.local_path = str(target)
                record.downloaded_bytes = verification.size
                record.checksum_verified = verification.checksum_verified
                record.last_error = ""
                outcomes.append(outcome)
                log.info("Already complete: %s", record.product_name)
                continue
            quarantine = _quarantine_invalid(target)
            log.warning(
                "Moved invalid existing archive to %s (%s)",
                quarantine,
                verification.reason,
            )
        partial = target.with_name(f"{target.name}.part")
        if partial.is_file():
            verification = verify_local_file(
                partial, record, verify_checksum=config.download.verify_checksum
            )
            if verification.valid:
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(partial, target)
                outcome = DownloadOutcome(
                    record.product_id,
                    "already-present",
                    target,
                    verification.size,
                    verification.checksum_verified,
                    0,
                )
                record.download_status = "completed"
                record.local_path = str(target)
                record.downloaded_bytes = verification.size
                record.checksum_verified = verification.checksum_verified
                record.last_error = ""
                outcomes.append(outcome)
                continue
            record.download_status = "partial"
            record.downloaded_bytes = partial.stat().st_size
            record.local_path = str(partial)
        elif record.download_status == "completed":
            record.download_status = "missing"
            record.last_error = "catalogue marked complete but local archive is missing"
        pending.append(record)

    store.write_csv(_all_records_with_updates(store, records))
    if not pending:
        store.write_parquet(_all_records_with_updates(store, records))
        return DownloadBatchResult(outcomes, len(records), 0, len(outcomes), 0)

    tokens = token_manager or TokenManager(
        config.api.token_url, timeout=config.api.request_timeout_seconds
    )
    record_by_id = {record.product_id: record for record in records}
    with ThreadPoolExecutor(
        max_workers=config.download.workers, thread_name_prefix="s2vomb"
    ) as pool:
        futures = {
            pool.submit(
                _download_one,
                record,
                config,
                tokens,
                session_factory=session_factory,
            ): record
            for record in pending
        }
        for future in as_completed(futures):
            record = futures[future]
            try:
                outcome = future.result()
            except Exception as error:  # defensive: retain state even for unexpected worker failure
                outcome = DownloadOutcome(
                    record.product_id,
                    "failed",
                    product_target(config, record),
                    0,
                    False,
                    1,
                    f"unexpected worker failure: {error}",
                )
            outcomes.append(outcome)
            _apply_outcome(record_by_id[outcome.product_id], outcome)
            merged = _all_records_with_updates(store, records)
            store.write_csv(merged)
            if outcome.status == "completed":
                log.info("Downloaded: %s", record.product_name)
            else:
                log.error("Failed: %s (%s)", record.product_name, outcome.error)
    store.write_parquet(_all_records_with_updates(store, records))
    return DownloadBatchResult(
        outcomes=outcomes,
        selected=len(records),
        downloaded=sum(outcome.status == "completed" for outcome in outcomes),
        already_present=sum(outcome.status == "already-present" for outcome in outcomes),
        failed=sum(outcome.status == "failed" for outcome in outcomes),
    )


def _all_records_with_updates(
    store: CatalogueStore, selected_records: list[ProductRecord]
) -> list[ProductRecord]:
    """Merge selected state into the complete catalogue before every atomic checkpoint."""
    complete = store.read_if_present()
    if not complete:
        return selected_records
    selected_by_id = {record.product_id: record for record in selected_records}
    return [selected_by_id.get(record.product_id, record) for record in complete]
