"""Tests for heyvox/herald/workspace_label.py — DEF-111.

Covers the resolution order in get_workspace_label:
    env override > config override > pr_title from DB > raw name
plus the announce_workspace master switch and middle-dot normalisation.
"""

from __future__ import annotations

import os
import subprocess
from unittest.mock import patch

import pytest

from heyvox.config import HeyvoxConfig, TTSConfig
from heyvox.herald import workspace_label
from heyvox.herald.workspace_label import get_workspace_label, reset_cache


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch):
    """Reset module-level cache + env var between tests."""
    reset_cache()
    monkeypatch.delenv("HEYVOX_WORKSPACE_LABEL", raising=False)
    yield
    reset_cache()


def _cfg(
    *,
    announce_workspace: bool = True,
    workspace_labels: dict[str, str] | None = None,
    announce_min_chars: int = 0,
) -> HeyvoxConfig:
    """Build a minimal HeyvoxConfig with the TTS knobs we care about."""
    return HeyvoxConfig(
        tts=TTSConfig(
            announce_workspace=announce_workspace,
            workspace_labels=workspace_labels or {},
            announce_min_chars=announce_min_chars,
        )
    )


class TestEmptyAndDisabled:
    def test_empty_workspace_returns_empty(self):
        assert get_workspace_label("", cfg=_cfg()) == ""

    def test_announce_disabled_returns_empty(self):
        # Even with a workspace and a labels override, the master switch wins.
        cfg = _cfg(announce_workspace=False, workspace_labels={"foo": "Bar"})
        assert get_workspace_label("foo", cfg=cfg) == ""


class TestResolutionOrder:
    def test_env_var_beats_everything(self, monkeypatch):
        monkeypatch.setenv("HEYVOX_WORKSPACE_LABEL", "EnvName")
        cfg = _cfg(workspace_labels={"seattle": "ConfigName"})
        assert get_workspace_label("seattle", cfg=cfg) == "EnvName"

    def test_config_override_beats_db(self, monkeypatch):
        cfg = _cfg(workspace_labels={"seattle": "ShortName"})
        # If the DB-path resolver is ever called, raise — config must short-circuit.
        monkeypatch.setattr(
            workspace_label,
            "_pr_title_from_db",
            lambda *a, **k: pytest.fail("DB lookup should be skipped"),
        )
        assert get_workspace_label("seattle", cfg=cfg) == "ShortName"

    def test_db_pr_title_used_when_no_override(self, monkeypatch):
        cfg = _cfg()
        monkeypatch.setattr(
            workspace_label, "_get_workspace_db_path", lambda c: "/fake/db"
        )
        monkeypatch.setattr(
            workspace_label,
            "_pr_title_from_db",
            lambda name, db: "Voice Resume Wip",
        )
        assert get_workspace_label("seattle", cfg=cfg) == "Voice Resume Wip"

    def test_falls_back_to_raw_name(self, monkeypatch):
        cfg = _cfg()
        # No DB, no override → raw name.
        monkeypatch.setattr(workspace_label, "_get_workspace_db_path", lambda c: "")
        assert get_workspace_label("vox-v2/seattle", cfg=cfg) == "vox-v2/seattle"


class TestNormalisation:
    def test_middle_dot_replaced(self, monkeypatch):
        cfg = _cfg()
        monkeypatch.setattr(
            workspace_label, "_get_workspace_db_path", lambda c: "/fake/db"
        )
        monkeypatch.setattr(
            workspace_label,
            "_pr_title_from_db",
            lambda name, db: "Personal · Source · Spell",
        )
        # Conductor's U+00B7 separator becomes ", " so Kokoro doesn't say "middle dot".
        assert (
            get_workspace_label("spell", cfg=cfg)
            == "Personal, Source, Spell"
        )

    def test_env_value_also_normalised(self, monkeypatch):
        monkeypatch.setenv("HEYVOX_WORKSPACE_LABEL", "Foo · Bar")
        cfg = _cfg()
        assert get_workspace_label("anything", cfg=cfg) == "Foo, Bar"

    def test_config_override_also_normalised(self, monkeypatch):
        cfg = _cfg(workspace_labels={"x": "Alpha · Beta"})
        assert get_workspace_label("x", cfg=cfg) == "Alpha, Beta"


class TestDBResolution:
    def test_pr_title_query_uses_directory_name(self, monkeypatch):
        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="My Title\n", stderr=""
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        out = workspace_label._pr_title_from_db("my-ws", "/some/db")
        assert out == "My Title"
        assert "/some/db" in captured["cmd"]
        # Query must filter by directory_name so callers don't need to URL-encode.
        assert "directory_name='my-ws'" in captured["cmd"][-1]

    def test_pr_title_escapes_single_quotes(self, monkeypatch):
        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="", stderr=""
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        workspace_label._pr_title_from_db("ws'evil", "/db")
        # Single quote must be doubled (SQL escape) — no raw quote in literal.
        assert "ws''evil" in captured["cmd"][-1]

    def test_pr_title_returns_empty_on_sqlite_missing(self, monkeypatch):
        def fake_run(*a, **kw):
            raise FileNotFoundError("sqlite3 not installed")

        monkeypatch.setattr(subprocess, "run", fake_run)
        # Should NOT raise — caller falls back to raw workspace_name.
        assert workspace_label._pr_title_from_db("ws", "/db") == ""

    def test_pr_title_returns_empty_on_timeout(self, monkeypatch):
        def fake_run(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="sqlite3", timeout=0.5)

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert workspace_label._pr_title_from_db("ws", "/db") == ""
