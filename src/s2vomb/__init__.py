"""Sentinel-2 L1C discovery and download tools for the Vombsjön archive."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("s2vomb")
except PackageNotFoundError:  # pragma: no cover - source tree without installation
    __version__ = "0.1.0"

__all__ = ["__version__"]
