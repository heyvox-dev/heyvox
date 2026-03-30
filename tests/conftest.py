"""Shared fixtures for heyvox tests."""

import os
import subprocess
import pytest


def _blackhole_available() -> bool:
    """Check if BlackHole virtual audio driver is installed."""
    try:
        r = subprocess.run(
            ["system_profiler", "SPAudioDataType"],
            capture_output=True, text=True, timeout=5,
        )
        return "BlackHole" in r.stdout
    except Exception:
        return False


def _vox_running() -> bool:
    """Check if heyvox process is running."""
    return os.path.exists("/tmp/heyvox.pid")


# Markers for conditional test skipping
blackhole_installed = pytest.mark.skipif(
    not _blackhole_available(),
    reason="BlackHole virtual audio driver not installed (brew install blackhole-2ch)",
)

vox_running = pytest.mark.skipif(
    not _vox_running(),
    reason="Vox is not running (start with: heyvox start)",
)
