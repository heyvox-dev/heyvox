"""Tests for HUDSurface — unified banner primitive (P-new / P-detector-without-action).

Covers:
- banner/read round-trip
- TTL expiry filters records on read
- Source-based dedup (latest write per source wins)
- top_active() picks highest-level then newest
- clear() removes a single source
- Legacy MIC_WARN_FILE compat: read_active() synthesises a record
- OSError tolerance: banner() never raises
"""

from __future__ import annotations

import json
import os
import time

import pytest


@pytest.fixture(autouse=True)
def _isolate_banner_path(tmp_path, monkeypatch):
    """Redirect HUD_BANNERS_FILE + MIC_WARN_FILE to a per-test tmp dir.

    HUDSurface resolves both via function-local imports from heyvox.constants,
    so monkeypatching the constants module is enough — no need to reach into
    surface.py.
    """
    banners = str(tmp_path / "heyvox-hud-banners.json")
    mic_warn = str(tmp_path / "heyvox-mic-warn")
    monkeypatch.setattr("heyvox.constants.HUD_BANNERS_FILE", banners)
    monkeypatch.setattr("heyvox.constants.MIC_WARN_FILE", mic_warn)
    yield {"banners": banners, "mic_warn": mic_warn}


def test_banner_write_then_read_active():
    from heyvox.hud.surface import HUDSurface

    HUDSurface.banner(level="warn", source="mic-zombie", text="Mic silent", ttl_secs=60)
    live = HUDSurface.read_active(include_legacy=False)

    assert len(live) == 1
    rec = live[0]
    assert rec["source"] == "mic-zombie"
    assert rec["level"] == "warn"
    assert rec["text"] == "Mic silent"
    assert rec["ttl"] == 60.0
    assert rec["ts"] <= time.time() + 1


def test_expired_records_filtered_on_read(_isolate_banner_path):
    """A banner with ts+ttl < now must not appear in read_active()."""
    # Hand-craft an expired record on disk
    rec = {
        "source": "stale",
        "level": "info",
        "text": "old",
        "ts": time.time() - 120,
        "ttl": 30.0,
    }
    with open(_isolate_banner_path["banners"], "w") as f:
        json.dump([rec], f)

    from heyvox.hud.surface import HUDSurface
    live = HUDSurface.read_active(include_legacy=False)
    assert live == []


def test_source_dedup_latest_wins():
    """Calling banner() twice with the same source replaces, not appends."""
    from heyvox.hud.surface import HUDSurface

    HUDSurface.banner(level="info", source="dup", text="first", ttl_secs=60)
    HUDSurface.banner(level="warn", source="dup", text="second", ttl_secs=60)

    live = HUDSurface.read_active(include_legacy=False)
    assert len(live) == 1
    assert live[0]["text"] == "second"
    assert live[0]["level"] == "warn"


def test_top_active_picks_highest_level():
    """error > warn > info; tie-broken by newest ts."""
    from heyvox.hud.surface import HUDSurface

    HUDSurface.banner(level="info", source="a", text="info-msg", ttl_secs=60)
    HUDSurface.banner(level="error", source="b", text="error-msg", ttl_secs=60)
    HUDSurface.banner(level="warn", source="c", text="warn-msg", ttl_secs=60)

    top = HUDSurface.top_active()
    assert top is not None
    assert top["level"] == "error"
    assert top["source"] == "b"


def test_top_active_returns_none_when_empty():
    from heyvox.hud.surface import HUDSurface
    assert HUDSurface.top_active() is None


def test_clear_removes_single_source():
    from heyvox.hud.surface import HUDSurface

    HUDSurface.banner(level="warn", source="x", text="x-msg", ttl_secs=60)
    HUDSurface.banner(level="warn", source="y", text="y-msg", ttl_secs=60)

    HUDSurface.clear("x")
    live = HUDSurface.read_active(include_legacy=False)
    sources = {r["source"] for r in live}
    assert sources == {"y"}


def test_clear_unknown_source_is_noop():
    from heyvox.hud.surface import HUDSurface

    HUDSurface.banner(level="warn", source="present", text="x", ttl_secs=60)
    HUDSurface.clear("does-not-exist")
    assert len(HUDSurface.read_active(include_legacy=False)) == 1


def test_legacy_mic_warn_compat(_isolate_banner_path):
    """A live MIC_WARN_FILE on disk surfaces as a legacy-mic-warn record."""
    mic_warn_path = _isolate_banner_path["mic_warn"]
    with open(mic_warn_path, "w") as f:
        f.write("Mic too quiet — legacy")

    from heyvox.hud.surface import HUDSurface
    live = HUDSurface.read_active(include_legacy=True)
    legacy = [r for r in live if r["source"] == "legacy-mic-warn"]
    assert len(legacy) == 1
    assert legacy[0]["level"] == "warn"
    assert "legacy" in legacy[0]["text"]


def test_legacy_compat_skipped_when_native_record_exists(_isolate_banner_path):
    """If a native banner with source='legacy-mic-warn' already exists, the
    file-based synth must not append a duplicate.

    (Not a typical case — guards against accidental shadowing if a detector
    explicitly uses 'legacy-mic-warn' as a source.)
    """
    with open(_isolate_banner_path["mic_warn"], "w") as f:
        f.write("file-based")

    from heyvox.hud.surface import HUDSurface
    HUDSurface.banner(level="warn", source="legacy-mic-warn", text="native", ttl_secs=60)

    live = HUDSurface.read_active(include_legacy=True)
    legacy = [r for r in live if r["source"] == "legacy-mic-warn"]
    assert len(legacy) == 1
    assert legacy[0]["text"] == "native"


def test_legacy_mic_warn_expired_filtered(_isolate_banner_path, monkeypatch):
    """Legacy file older than MIC_WARN_TTL_SECS must NOT appear."""
    mic_warn_path = _isolate_banner_path["mic_warn"]
    with open(mic_warn_path, "w") as f:
        f.write("ancient")
    # Backdate the file
    old_ts = time.time() - 9999
    os.utime(mic_warn_path, (old_ts, old_ts))

    from heyvox.hud.surface import HUDSurface
    live = HUDSurface.read_active(include_legacy=True)
    assert all(r["source"] != "legacy-mic-warn" for r in live)


def test_banner_invalid_level_falls_back_to_info():
    from heyvox.hud.surface import HUDSurface

    HUDSurface.banner(level="bogus", source="s", text="x", ttl_secs=60)
    live = HUDSurface.read_active(include_legacy=False)
    assert len(live) == 1
    assert live[0]["level"] == "info"


def test_banner_text_truncated_to_160_chars():
    from heyvox.hud.surface import HUDSurface

    long = "x" * 500
    HUDSurface.banner(level="info", source="s", text=long, ttl_secs=60)
    live = HUDSurface.read_active(include_legacy=False)
    assert len(live[0]["text"]) == 160


def test_banner_zero_or_negative_ttl_silently_dropped():
    from heyvox.hud.surface import HUDSurface
    HUDSurface.banner(level="info", source="s", text="x", ttl_secs=0)
    HUDSurface.banner(level="info", source="s2", text="x", ttl_secs=-5)
    assert HUDSurface.read_active(include_legacy=False) == []


def test_banner_oserror_tolerant(monkeypatch):
    """A write failure in the underlying file path must not raise."""
    from heyvox.hud import surface as surf

    def boom(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(surf, "_atomic_write", boom)
    # Should not raise — banner failures must be silent for the caller.
    surf.HUDSurface.banner(level="warn", source="s", text="x", ttl_secs=60)


def test_corrupt_file_returns_empty(_isolate_banner_path):
    """A malformed JSON file must not crash read_active()."""
    with open(_isolate_banner_path["banners"], "w") as f:
        f.write("{not valid json")

    from heyvox.hud.surface import HUDSurface
    live = HUDSurface.read_active(include_legacy=False)
    assert live == []


def test_records_missing_fields_filtered(_isolate_banner_path):
    """Records that don't have all required keys are silently dropped."""
    with open(_isolate_banner_path["banners"], "w") as f:
        json.dump([
            {"source": "ok", "level": "info", "text": "x", "ts": time.time(), "ttl": 60.0},
            {"source": "missing-ttl", "level": "warn", "text": "x", "ts": time.time()},
            "not-a-dict",
        ], f)

    from heyvox.hud.surface import HUDSurface
    live = HUDSurface.read_active(include_legacy=False)
    assert {r["source"] for r in live} == {"ok"}
