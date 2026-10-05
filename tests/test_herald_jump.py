"""Tests for the Herald jump key (workspace_switch.mode "jump_key").

Covers the three pieces without Quartz, Orca or a real Herald:

  * ModifierDoubleTap — clean double-tap detection on one device modifier
  * the jump target file — written per announcement by the orchestrator
  * jump_to_target — app to front, then the provider switch
"""

from __future__ import annotations

import json

import pytest

from heyvox.herald import jump as jump_mod
from heyvox.herald.jump import (
    clear_jump_target, jump_to_target, read_jump_target, write_jump_target,
)
from heyvox.input.ptt import _DEVICE_MODIFIER_BITS, ModifierDoubleTap

LCTRL = _DEVICE_MODIFIER_BITS["left_ctrl"] | 0x40000  # device bit + generic Control
RCTRL = _DEVICE_MODIFIER_BITS["right_ctrl"] | 0x40000
LSHIFT = _DEVICE_MODIFIER_BITS["left_shift"] | 0x20000


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t

    def advance(self, secs):
        self.t += secs


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def tap(clock):
    return ModifierDoubleTap(_DEVICE_MODIFIER_BITS["left_ctrl"], clock=clock)


def _press(tap, clock, flags=LCTRL, hold=0.05, gap=0.1):
    fired = tap.on_flags(flags)
    clock.advance(hold)
    fired = tap.on_flags(0) or fired
    clock.advance(gap)
    return fired


class TestModifierDoubleTap:
    def test_double_tap_fires_once(self, tap, clock):
        assert _press(tap, clock) is False
        assert _press(tap, clock) is True

    def test_single_tap_does_nothing(self, tap, clock):
        assert _press(tap, clock) is False

    def test_third_tap_starts_over(self, tap, clock):
        _press(tap, clock)
        assert _press(tap, clock) is True
        assert _press(tap, clock) is False
        assert _press(tap, clock) is True

    def test_gap_too_long(self, tap, clock):
        _press(tap, clock, gap=0.5)
        assert _press(tap, clock) is False

    def test_held_too_long_is_no_tap(self, tap, clock):
        _press(tap, clock, hold=0.6)
        assert _press(tap, clock) is False

    def test_ctrl_c_in_between_breaks_it(self, tap, clock):
        _press(tap, clock)
        tap.on_flags(LCTRL)
        tap.on_other_key()  # the "c"
        clock.advance(0.05)
        assert tap.on_flags(0) is False
        assert _press(tap, clock) is False

    def test_typing_between_taps_breaks_it(self, tap, clock):
        _press(tap, clock)
        tap.on_other_key()
        assert _press(tap, clock) is False

    def test_with_another_modifier_does_not_count(self, tap, clock):
        _press(tap, clock, flags=LCTRL | LSHIFT)
        assert _press(tap, clock, flags=LCTRL | LSHIFT) is False

    def test_right_ctrl_is_not_left_ctrl(self, tap, clock):
        _press(tap, clock, flags=RCTRL)
        assert _press(tap, clock, flags=RCTRL) is False

    def test_caps_lock_is_ignored(self, tap, clock):
        caps = 0x10000
        _press(tap, clock, flags=LCTRL | caps)
        assert tap.on_flags(LCTRL | caps) is False
        clock.advance(0.05)
        assert tap.on_flags(caps) is True

    def test_fn_held_does_not_count(self, tap, clock):
        fn = 0x800000
        _press(tap, clock, flags=LCTRL | fn)
        assert _press(tap, clock, flags=LCTRL | fn) is False


IDENTITY = {
    "workspace": "Workspace-Sprung per Doppeltipp", "workspace_id": "r1::/x",
    "session_id": "claude_abc", "cwd": "/x", "provider": "orca",
}


class TestTargetFile:
    def test_roundtrip(self, tmp_path):
        path = tmp_path / "target.json"
        write_jump_target(path, IDENTITY)
        assert read_jump_target(path) == IDENTITY
        assert "ts" in json.loads(path.read_text())

    def test_missing_or_broken_is_none(self, tmp_path):
        path = tmp_path / "target.json"
        assert read_jump_target(path) is None
        path.write_text("{nope")
        assert read_jump_target(path) is None

    def test_clear(self, tmp_path):
        path = tmp_path / "target.json"
        write_jump_target(path, IDENTITY)
        clear_jump_target(path)
        assert not path.exists()
        clear_jump_target(path)  # idempotent


class TestRecordJumpTarget:
    """Orchestrator side: each announcement replaces the target."""

    def _cfg(self, tmp_path):
        from heyvox.herald.orchestrator import OrchestratorConfig
        return OrchestratorConfig(
            jump_target_file=tmp_path / "target.json",
            debug_log=tmp_path / "debug.log",
        )

    def _sidecar(self, tmp_path, identity):
        from heyvox.herald.workspace_label import write_switch_sidecar
        wav = tmp_path / "msg-1.wav"
        write_switch_sidecar(
            str(wav), identity["workspace"], identity["workspace_id"],
            identity["session_id"], identity["cwd"], identity["provider"],
        )
        return wav.with_suffix(".workspace")

    def test_sidecar_becomes_target_and_is_consumed(self, tmp_path):
        from heyvox.herald.orchestrator import _record_jump_target
        cfg = self._cfg(tmp_path)
        sidecar = self._sidecar(tmp_path, IDENTITY)
        assert _record_jump_target(sidecar, cfg) == IDENTITY["workspace"]
        assert read_jump_target(cfg.jump_target_file) == IDENTITY
        assert not sidecar.exists()

    def test_message_without_workspace_clears_old_target(self, tmp_path):
        from heyvox.herald.orchestrator import _record_jump_target
        cfg = self._cfg(tmp_path)
        write_jump_target(cfg.jump_target_file, IDENTITY)
        assert _record_jump_target(tmp_path / "none.workspace", cfg) == ""
        assert not cfg.jump_target_file.exists()

    def test_never_switches_on_its_own(self, tmp_path, monkeypatch):
        from heyvox.herald import orchestrator
        called = []
        monkeypatch.setattr(orchestrator, "_switch_workspace", lambda *a, **k: called.append(a))
        monkeypatch.setattr(orchestrator, "_run_switch_countdown", lambda *a, **k: called.append(a))
        cfg = self._cfg(tmp_path)
        orchestrator._record_jump_target(self._sidecar(tmp_path, IDENTITY), cfg)
        assert called == []


class TestJumpToTarget:
    @pytest.fixture
    def switch(self, monkeypatch):
        from heyvox.config import HeyvoxConfig
        from heyvox.herald import orchestrator
        calls = []

        def fake_switch(workspace, cfg, **kw):
            calls.append((workspace, kw))
            return True

        monkeypatch.setattr(orchestrator, "_switch_workspace", fake_switch)
        monkeypatch.setattr("heyvox.config.load_config", lambda *a, **k: HeyvoxConfig())
        activated = []
        monkeypatch.setattr(jump_mod, "_activate_app", lambda name: activated.append(name) or True)
        return calls, activated

    def test_no_target(self, tmp_path, switch):
        calls, activated = switch
        assert jump_to_target(tmp_path / "t.json", recording_flag=tmp_path / "rec") is False
        assert calls == [] and activated == []

    def test_skipped_while_recording(self, tmp_path, switch):
        calls, _ = switch
        write_jump_target(tmp_path / "t.json", IDENTITY)
        (tmp_path / "rec").touch()
        assert jump_to_target(tmp_path / "t.json", recording_flag=tmp_path / "rec") is False
        assert calls == []

    def test_activates_app_then_switches_with_session(self, tmp_path, switch):
        calls, activated = switch
        write_jump_target(tmp_path / "t.json", IDENTITY)
        assert jump_to_target(tmp_path / "t.json", recording_flag=tmp_path / "rec") is True
        assert activated == ["Orca"]
        assert calls == [(IDENTITY["workspace"], {
            "workspace_id": "r1::/x", "session_id": "claude_abc", "cwd": "/x",
            "provider_name": "orca",
        })]

    def test_target_survives_the_jump(self, tmp_path, switch):
        write_jump_target(tmp_path / "t.json", IDENTITY)
        jump_to_target(tmp_path / "t.json", recording_flag=tmp_path / "rec")
        assert read_jump_target(tmp_path / "t.json") == IDENTITY

    def test_unknown_provider(self, tmp_path, switch):
        calls, _ = switch
        write_jump_target(tmp_path / "t.json", {**IDENTITY, "provider": "nope"})
        assert jump_to_target(tmp_path / "t.json", recording_flag=tmp_path / "rec") is False
        assert calls == []


class TestConfig:
    def test_defaults(self):
        from heyvox.config import WorkspaceSwitchConfig
        cfg = WorkspaceSwitchConfig()
        assert (cfg.mode, cfg.jump_key) == ("jump_key", "left_ctrl")

    def test_mode_validated(self):
        from pydantic import ValidationError
        from heyvox.config import WorkspaceSwitchConfig
        assert WorkspaceSwitchConfig(mode="Countdown").mode == "countdown"
        with pytest.raises(ValidationError):
            WorkspaceSwitchConfig(mode="auto")
