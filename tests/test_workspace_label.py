"""Tests for heyvox/herald/workspace_label.py — DEF-111.

Covers the resolution order in get_workspace_label:
    env override > config override > sidebar label from DB
    (workspace_name-if-user-set else pr_title) > raw name
plus the announce_workspace master switch and middle-dot normalisation.
"""

from __future__ import annotations

import subprocess

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
            "_sidebar_label_from_db",
            lambda *a, **k: pytest.fail("DB lookup should be skipped"),
        )
        assert get_workspace_label("seattle", cfg=cfg) == "ShortName"

    def test_db_sidebar_label_used_when_no_override(self, monkeypatch):
        cfg = _cfg()
        monkeypatch.setattr(
            workspace_label, "_get_workspace_db_path", lambda c: "/fake/db"
        )
        monkeypatch.setattr(
            workspace_label,
            "_sidebar_label_from_db",
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
            "_sidebar_label_from_db",
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
    """DEF-111 follow-up: workspace_name (user-set) beats pr_title, which
    drifts to a Conventional-Commit string on every PR merge — see
    claude-conductor-setup's CLAUDE.md "Workspace Naming" section."""

    SEP = workspace_label._DB_FIELD_SEP

    def _stub_row(self, ws_name: str, user_set: str, pr_title: str) -> str:
        return f"{ws_name}{self.SEP}{user_set}{self.SEP}{pr_title}\n"

    def test_query_uses_directory_name(self, monkeypatch):
        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout=self._stub_row("My Title", "1", ""),
                stderr="",
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        out = workspace_label._sidebar_label_from_db("my-ws", "/some/db")
        assert out == "My Title"
        assert "/some/db" in captured["cmd"]
        # Query must filter by directory_name so callers don't need to URL-encode.
        assert "directory_name='my-ws'" in captured["cmd"][-1]

    def test_workspace_name_wins_when_user_set(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **kw: subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout=self._stub_row(
                    "Claude Setup", "1", "feat(hooks): add x"
                ),
                stderr="",
            ),
        )
        # Regression: pr_title had drifted to a commit message, but the
        # drift-proof workspace_name field must win.
        assert workspace_label._sidebar_label_from_db("manama", "/db") == "Claude Setup"

    def test_pr_title_used_when_not_user_set(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **kw: subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout=self._stub_row("", "0", "Some PR Title"),
                stderr="",
            ),
        )
        assert workspace_label._sidebar_label_from_db("ws", "/db") == "Some PR Title"

    def test_pr_title_used_when_user_set_but_name_empty(self, monkeypatch):
        # Defensive: user_set_workspace_name=1 with an empty workspace_name
        # shouldn't happen in practice, but must not return "".
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **kw: subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout=self._stub_row("", "1", "Fallback Title"),
                stderr="",
            ),
        )
        assert workspace_label._sidebar_label_from_db("ws", "/db") == "Fallback Title"

    def test_escapes_single_quotes(self, monkeypatch):
        captured = {}

        def fake_run(cmd, **kw):
            captured["cmd"] = cmd
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        workspace_label._sidebar_label_from_db("ws'evil", "/db")
        # Single quote must be doubled (SQL escape) — no raw quote in literal.
        assert "ws''evil" in captured["cmd"][-1]

    def test_returns_empty_on_no_matching_row(self, monkeypatch):
        monkeypatch.setattr(
            subprocess,
            "run",
            lambda cmd, **kw: subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="", stderr=""
            ),
        )
        assert workspace_label._sidebar_label_from_db("ws", "/db") == ""

    def test_returns_empty_on_sqlite_missing(self, monkeypatch):
        def fake_run(*a, **kw):
            raise FileNotFoundError("sqlite3 not installed")

        monkeypatch.setattr(subprocess, "run", fake_run)
        # Should NOT raise — caller falls back to raw workspace_name.
        assert workspace_label._sidebar_label_from_db("ws", "/db") == ""

    def test_returns_empty_on_timeout(self, monkeypatch):
        def fake_run(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="sqlite3", timeout=0.5)

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert workspace_label._sidebar_label_from_db("ws", "/db") == ""


class TestDetectWorkspaceFromCwd:
    """DEF-111: Conductor doesn't export workspace env vars to the hook,
    so the worker must derive the workspace from cwd."""

    def test_exact_match_returns_directory_name(self, monkeypatch):
        monkeypatch.setattr(
            workspace_label, "_get_workspace_db_path", lambda c: "/fake/db"
        )

        def fake_run(cmd, **kw):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="seattle\n", stderr=""
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert (
            workspace_label.detect_workspace_from_cwd(
                "/Users/work/conductor/workspaces/vox-v2/seattle"
            )
            == "seattle"
        )

    def test_subdirectory_match(self, monkeypatch):
        """A shell that cd'd into a subdir still resolves correctly."""
        captured = {}
        monkeypatch.setattr(
            workspace_label, "_get_workspace_db_path", lambda c: "/fake/db"
        )

        def fake_run(cmd, **kw):
            captured["sql"] = cmd[-1]
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="seattle\n", stderr=""
            )

        monkeypatch.setattr(subprocess, "run", fake_run)
        out = workspace_label.detect_workspace_from_cwd(
            "/Users/work/conductor/workspaces/vox-v2/seattle/heyvox/herald"
        )
        assert out == "seattle"
        # Query must use LIKE so a cd-into-subdir still hits the workspace row.
        assert "LIKE workspace_path" in captured["sql"]

    def test_fallback_to_basename_when_db_missing(self, monkeypatch):
        monkeypatch.setattr(workspace_label, "_get_workspace_db_path", lambda c: "")
        assert (
            workspace_label.detect_workspace_from_cwd(
                "/Users/me/conductor/workspaces/foo/seattle"
            )
            == "seattle"
        )

    def test_fallback_to_basename_when_no_row_matches(self, monkeypatch):
        monkeypatch.setattr(
            workspace_label, "_get_workspace_db_path", lambda c: "/fake/db"
        )

        def fake_run(*a, **kw):
            return subprocess.CompletedProcess(args=a, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        assert (
            workspace_label.detect_workspace_from_cwd("/Users/me/projects/heyvox")
            == "heyvox"
        )

    def test_empty_cwd_returns_empty(self, monkeypatch):
        monkeypatch.setattr(workspace_label, "_get_workspace_db_path", lambda c: "")
        assert workspace_label.detect_workspace_from_cwd("") == ""

    def test_db_quote_escape_for_path(self, monkeypatch):
        """Paths with single quotes shouldn't break the SQL literal."""
        captured = {}
        monkeypatch.setattr(
            workspace_label, "_get_workspace_db_path", lambda c: "/fake/db"
        )

        def fake_run(cmd, **kw):
            captured["sql"] = cmd[-1]
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        monkeypatch.setattr(subprocess, "run", fake_run)
        workspace_label.detect_workspace_from_cwd("/path/with'quote/dir")
        # Single quote in the path must be doubled in the SQL literal.
        assert "/path/with''quote/dir" in captured["sql"]


class TestResolveWorkspaceId:
    """DEF-237: workspace_id resolution feeding the switch sidecar."""

    def test_empty_directory_name_short_circuits(self, monkeypatch):
        monkeypatch.setattr(
            workspace_label, "_get_workspace_db_path",
            lambda c: pytest.fail("should not be reached for empty directory_name"),
        )
        assert workspace_label.resolve_workspace_id("") == ""

    def test_no_db_path_returns_empty(self, monkeypatch):
        monkeypatch.setattr(workspace_label, "_get_workspace_db_path", lambda c: "")
        assert workspace_label.resolve_workspace_id("seattle") == ""

    def test_resolves_workspace_id_from_adapter(self, monkeypatch):
        monkeypatch.setattr(workspace_label, "_get_workspace_db_path", lambda c: "/fake/db")

        class _Identity:
            workspace_id = "6d9c2881-edfa-4238-acdf-1d26e9b0103d"

        monkeypatch.setattr(
            "heyvox.adapters.conductor.get_active_workspace_and_session",
            lambda directory_name, db_path: _Identity(),
        )
        assert workspace_label.resolve_workspace_id("seattle") == "6d9c2881-edfa-4238-acdf-1d26e9b0103d"

    def test_adapter_returning_none_yields_empty(self, monkeypatch):
        monkeypatch.setattr(workspace_label, "_get_workspace_db_path", lambda c: "/fake/db")
        monkeypatch.setattr(
            "heyvox.adapters.conductor.get_active_workspace_and_session",
            lambda directory_name, db_path: None,
        )
        assert workspace_label.resolve_workspace_id("seattle") == ""

    def test_adapter_exception_yields_empty(self, monkeypatch):
        monkeypatch.setattr(workspace_label, "_get_workspace_db_path", lambda c: "/fake/db")

        def _raise(*a, **kw):
            raise RuntimeError("db locked")

        monkeypatch.setattr("heyvox.adapters.conductor.get_active_workspace_and_session", _raise)
        assert workspace_label.resolve_workspace_id("seattle") == ""


class TestSwitchSidecar:
    """DEF-237: JSON .workspace sidecar carrying workspace_id/session_id.
    DEF-244 added a fourth field, cwd — a last-resort resolution signal."""

    def test_write_then_read_round_trip(self, tmp_path):
        wav = tmp_path / "1700000000000-01.wav"
        workspace_label.write_switch_sidecar(
            str(wav), "seattle", "ws-uuid-123", "sess-uuid-456", "/ws/vox-v2/seattle",
        )
        sidecar = tmp_path / "1700000000000-01.workspace"
        assert sidecar.exists()
        identity = workspace_label.read_switch_sidecar(sidecar.read_text())
        assert identity == {
            "workspace": "seattle",
            "workspace_id": "ws-uuid-123",
            "session_id": "sess-uuid-456",
            "cwd": "/ws/vox-v2/seattle",
        }

    def test_write_skips_when_workspace_empty(self, tmp_path):
        wav = tmp_path / "msg-01.wav"
        workspace_label.write_switch_sidecar(str(wav), "", "ws-uuid-123", "sess-uuid-456")
        assert not (tmp_path / "msg-01.workspace").exists()

    def test_write_defaults_ids_to_empty(self, tmp_path):
        wav = tmp_path / "msg-01.wav"
        workspace_label.write_switch_sidecar(str(wav), "seattle")
        identity = workspace_label.read_switch_sidecar((tmp_path / "msg-01.workspace").read_text())
        assert identity == {
            "workspace": "seattle", "workspace_id": "", "session_id": "", "cwd": "",
        }

    def test_read_legacy_plain_string_sidecar(self):
        """Sidecars written by a pre-DEF-237 worker/watcher still switch the workspace."""
        assert workspace_label.read_switch_sidecar("seattle") == {
            "workspace": "seattle", "workspace_id": "", "session_id": "", "cwd": "",
        }

    def test_read_handles_malformed_json_as_plain_label(self):
        # Starts with "{" but isn't valid JSON — must not raise or return empty.
        assert workspace_label.read_switch_sidecar("{not json") == {
            "workspace": "{not json", "workspace_id": "", "session_id": "", "cwd": "",
        }

    def test_read_strips_whitespace(self):
        assert workspace_label.read_switch_sidecar("  seattle\n") == {
            "workspace": "seattle", "workspace_id": "", "session_id": "", "cwd": "",
        }

    def test_read_pre_def243_json_sidecar_defaults_cwd_to_empty(self):
        """A DEF-237-format sidecar (no cwd key) written before DEF-244 shipped
        must still parse — cwd defaults to "" rather than raising/dropping."""
        pre_def243_json = (
            '{"workspace": "seattle", "workspace_id": "ws-1", "session_id": "sess-1"}'
        )
        assert workspace_label.read_switch_sidecar(pre_def243_json) == {
            "workspace": "seattle", "workspace_id": "ws-1", "session_id": "sess-1", "cwd": "",
        }
