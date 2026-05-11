"""HeyVox — macOS voice layer for AI coding agents."""
from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("heyvox")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"
