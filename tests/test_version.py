"""Tests for heyvox.__version__ — version is sourced from importlib.metadata."""
import re
import tomllib
from pathlib import Path

import heyvox


class TestVersion:
    """`__version__` must resolve via importlib.metadata.version("heyvox")."""

    def test_version_is_string(self):
        assert isinstance(heyvox.__version__, str)
        assert heyvox.__version__  # non-empty

    def test_version_matches_pyproject(self):
        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        with open(pyproject, "rb") as f:
            project_version = tomllib.load(f)["project"]["version"]
        # When package is installed (editable or wheel), importlib.metadata
        # returns pyproject's version. If somehow uninstalled, fallback applies.
        assert heyvox.__version__ in (project_version, "0.0.0-dev")

    def test_version_format(self):
        # Matches both "1.0.0" and "0.0.0-dev"
        assert re.match(r"^\d+\.\d+\.\d+(-?\w+)?$", heyvox.__version__)


class TestClassifierIsBeta:
    """Phase 14 D-06: classifier bumped Alpha → Beta."""

    def test_pyproject_declares_beta(self):
        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        with open(pyproject, "rb") as f:
            data = tomllib.load(f)
        classifiers = data["project"]["classifiers"]
        assert "Development Status :: 4 - Beta" in classifiers
        assert "Development Status :: 3 - Alpha" not in classifiers
