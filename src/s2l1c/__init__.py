"""Reusable Sentinel-2 L1C discovery and archive download tools."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("s2l1c")
except PackageNotFoundError:  # pragma: no cover - source tree without installation
    __version__ = "0.1.0"

__all__ = ["__version__"]
