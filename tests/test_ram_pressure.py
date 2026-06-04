"""Tests for the system RAM-pressure detector (heyvox/ram_pressure.py).

Covers the pure decision core (``evaluate``) across the warn/critical/clear
matrix and both trigger paths (free-RAM floor + macOS pressure level), plus
the ``check_and_surface`` banner-routing (banner on pressure, clear on
recovery). Fast, no hardware, no macOS UI — CI-friendly.

Feature: system RAM-pressure menu-bar banner (Pattern P-new — silent state
change made visible).
"""

import types

import pytest

from heyvox import ram_pressure
from heyvox.ram_pressure import evaluate


# --------------------------------------------------------------------------
# evaluate() — pure decision matrix
# --------------------------------------------------------------------------

def test_plenty_of_ram_normal_pressure_clears():
    level, text = evaluate(available_mb=8000, warn_mb=2048, crit_mb=1024,
                           pressure_level=ram_pressure.PRESSURE_NORMAL)
    assert level is None
    assert text == ""


def test_below_warn_floor_warns():
    level, text = evaluate(available_mb=1800, warn_mb=2048, crit_mb=1024,
                           pressure_level=ram_pressure.PRESSURE_NORMAL)
    assert level == "warn"
    assert "1.8 GB" in text


def test_below_critical_floor_errors():
    level, text = evaluate(available_mb=900, warn_mb=2048, crit_mb=1024,
                           pressure_level=ram_pressure.PRESSURE_NORMAL)
    assert level == "error"
    assert "critical" in text.lower()


def test_macos_warn_pressure_trips_even_with_free_ram():
    # Kernel says "warn" while free RAM looks fine — banner must still fire.
    level, _ = evaluate(available_mb=8000, warn_mb=2048, crit_mb=1024,
                        pressure_level=ram_pressure.PRESSURE_WARN)
    assert level == "warn"


def test_macos_critical_pressure_trips_even_with_free_ram():
    level, _ = evaluate(available_mb=8000, warn_mb=2048, crit_mb=1024,
                        pressure_level=ram_pressure.PRESSURE_CRITICAL)
    assert level == "error"


def test_pressure_level_none_falls_back_to_floor():
    # sysctl unreadable (None) → decision rests on the free-RAM floor alone.
    assert evaluate(8000, 2048, 1024, None)[0] is None
    assert evaluate(1500, 2048, 1024, None)[0] == "warn"
    assert evaluate(500, 2048, 1024, None)[0] == "error"


def test_critical_takes_precedence_over_warn():
    # Both floors crossed → the worse (error) wins.
    level, _ = evaluate(available_mb=500, warn_mb=2048, crit_mb=1024,
                        pressure_level=ram_pressure.PRESSURE_WARN)
    assert level == "error"


# --------------------------------------------------------------------------
# check_and_surface() — banner routing (banner on pressure, clear on recovery)
# --------------------------------------------------------------------------

class _FakeSurface:
    """Records HUDSurface.banner/clear calls for assertion."""
    def __init__(self):
        self.banners = []
        self.cleared = []

    def banner(self, level, source, text, ttl_secs=60.0):
        self.banners.append((level, source, text))

    def clear(self, source):
        self.cleared.append(source)


@pytest.fixture
def fake_surface(monkeypatch):
    fake = _FakeSurface()
    import heyvox.hud.surface as surface_mod
    monkeypatch.setattr(surface_mod.HUDSurface, "banner",
                        staticmethod(fake.banner))
    monkeypatch.setattr(surface_mod.HUDSurface, "clear",
                        staticmethod(fake.clear))
    return fake


def _patch_available(monkeypatch, available_mb):
    import psutil
    monkeypatch.setattr(
        psutil, "virtual_memory",
        lambda: types.SimpleNamespace(available=int(available_mb * 1024 * 1024)),
    )


def test_check_clears_banner_when_healthy(monkeypatch, fake_surface):
    _patch_available(monkeypatch, 8000)
    monkeypatch.setattr(ram_pressure, "macos_pressure_level", lambda: 1)
    level = ram_pressure.check_and_surface(warn_mb=2048, crit_mb=1024)
    assert level is None
    assert ram_pressure.BANNER_SOURCE in fake_surface.cleared
    assert fake_surface.banners == []


def test_check_emits_warn_banner_when_low(monkeypatch, fake_surface):
    _patch_available(monkeypatch, 1500)
    monkeypatch.setattr(ram_pressure, "macos_pressure_level", lambda: 1)
    level = ram_pressure.check_and_surface(warn_mb=2048, crit_mb=1024)
    assert level == "warn"
    assert len(fake_surface.banners) == 1
    emitted_level, source, _ = fake_surface.banners[0]
    assert emitted_level == "warn"
    assert source == ram_pressure.BANNER_SOURCE


def test_check_never_raises_when_psutil_missing(monkeypatch):
    # A monitoring path must never break the main loop.
    import psutil

    def _boom():
        raise RuntimeError("psutil exploded")

    monkeypatch.setattr(psutil, "virtual_memory", _boom)
    # Must return None, not propagate.
    assert ram_pressure.check_and_surface() is None
