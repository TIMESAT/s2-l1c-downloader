"""Small shared utilities and domain-specific exceptions."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any


class S2VombError(Exception):
    """Base class for expected, user-facing application failures."""


class ConfigError(S2VombError):
    """Raised when configuration is missing, inconsistent, or unsafe."""


class GeometryError(S2VombError):
    """Raised when a configured GeoJSON geometry cannot be used."""


class CatalogueError(S2VombError):
    """Raised when catalogue discovery or persistence fails."""


class AuthenticationError(S2VombError):
    """Raised when secure CDSE authentication cannot be completed."""


class DownloadError(S2VombError):
    """Raised when a source product cannot be downloaded or verified."""


def utc_now() -> datetime:
    return datetime.now(UTC)


def utc_now_iso() -> str:
    return utc_now().isoformat().replace("+00:00", "Z")


def utc_today() -> date:
    return utc_now().date()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def atomic_write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def format_bytes(size: int | None) -> str:
    if size is None:
        return "unknown"
    amount = float(size)
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if abs(amount) < 1024.0 or unit == units[-1]:
            return f"{amount:.2f} {unit}"
        amount /= 1024.0
    return f"{size} B"  # pragma: no cover


def parse_iso_datetime(value: str) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    result = datetime.fromisoformat(normalized)
    if result.tzinfo is None:
        result = result.replace(tzinfo=UTC)
    return result.astimezone(UTC)


def safe_product_filename(product_name: str) -> str:
    if not product_name or product_name in {".", ".."}:
        raise DownloadError("The catalogue contains an empty or unsafe product name")
    if Path(product_name).name != product_name or "/" in product_name or "\\" in product_name:
        raise DownloadError(f"Unsafe product name in catalogue: {product_name!r}")
    return product_name if product_name.lower().endswith(".zip") else f"{product_name}.zip"


def decode_varint(data: bytes, offset: int = 0) -> tuple[int, int]:
    value = 0
    shift = 0
    for index in range(offset, len(data)):
        byte = data[index]
        value |= (byte & 0x7F) << shift
        if byte & 0x80 == 0:
            return value, index + 1
        shift += 7
        if shift > 63:
            break
    raise ValueError("Invalid unsigned varint")


def parse_multihash(value: str) -> tuple[str | None, str | None]:
    """Return a hashlib name and digest hex from a STAC file:checksum multihash."""
    if not value:
        return None, None
    try:
        raw = bytes.fromhex(value)
        code, offset = decode_varint(raw)
        length, offset = decode_varint(raw, offset)
    except (ValueError, TypeError):
        return None, None
    digest = raw[offset:]
    if len(digest) != length:
        return None, None
    algorithms = {
        0x11: "sha1",
        0x12: "sha256",
        0x13: "sha512",
        0xD5: "md5",
    }
    return algorithms.get(code), digest.hex()


def checksum_file(path: Path, algorithm: str, chunk_size: int = 8 * 1024 * 1024) -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()
