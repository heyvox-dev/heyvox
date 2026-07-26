"""Guard tests for the Herald hooks installer (DEF-223).

These pin the exact structure written into the user's ~/.claude/settings.json.
The installer shipped broken for four releases because nothing asserted its
OUTPUT — only that it returned without raising. The maintainer's own settings
file was hand-written in the correct shape and so never exercised this path.

Claude Code's settings schema requires each hook event to hold matcher groups,
each wrapping a handler list with a mandatory "type":

    "Stop": [{"hooks": [{"type": "command", "command": "..."}]}]

Anything flatter is not dispatched. "Stop_session" is not an event at all.
"""

import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture()
def hooks_mod(tmp_path, monkeypatch):
    """Import the installer with HOME pointed at a throwaway directory.

    _STABLE_HOOKS_DIR is resolved at import time, so HOME must be patched
    before the (re)import — otherwise the test writes to the real ~/.claude.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    import heyvox.setup.hooks as mod
    mod = importlib.reload(mod)
    (tmp_path / ".claude").mkdir(parents=True, exist_ok=True)
    yield mod, tmp_path
    importlib.reload(mod)  # restore real-HOME module state for other tests


def _settings(root: Path) -> dict:
    return json.loads((root / ".claude" / "settings.json").read_text())


def _herald_commands(settings: dict, event: str) -> list[str]:
    """Commands of Herald handlers under `event`, nested shape only."""
    return [
        h["command"]
        for entry in settings.get("hooks", {}).get(event, [])
        for h in entry.get("hooks", [])
        if "herald" in h.get("command", "").lower()
    ]


def test_writes_nested_shape_with_explicit_type(hooks_mod):
    """Every installed entry is a matcher group whose handlers carry type=command."""
    mod, root = hooks_mod
    results = mod.install_herald_hooks()
    assert all(ok for ok, _ in results), results

    settings = _settings(root)
    for event in ("Stop", "Notification", "SessionEnd"):
        entries = settings["hooks"][event]
        assert entries, f"{event} has no entries"
        for entry in entries:
            assert "hooks" in entry, f"{event}: flat entry is not dispatched: {entry}"
            assert "command" not in entry, f"{event}: legacy flat key present: {entry}"
            for handler in entry["hooks"]:
                assert handler.get("type") == "command", f"{event}: missing type: {handler}"
                assert handler["command"].startswith("bash "), handler


def test_uses_real_event_names_only(hooks_mod):
    """Stop_session is not a Claude Code event and must never be written."""
    mod, root = hooks_mod
    mod.install_herald_hooks()
    events = set(_settings(root)["hooks"])
    assert "Stop_session" not in events
    assert "SessionEnd" in events


def test_migrates_legacy_flat_entries_and_bogus_event(hooks_mod):
    """A config written by heyvox <= 1.1.3 is repaired, not duplicated."""
    mod, root = hooks_mod
    (root / ".claude" / "settings.json").write_text(json.dumps({
        "model": "opus",
        "hooks": {
            "Stop": [
                {"hooks": [{"type": "command", "command": "bash /other/tool.sh"}]},
                {"command": "bash /old/path/herald/on-response.sh"},
            ],
            "Stop_session": [
                {"command": "bash /old/path/herald/on-session-end.sh"},
            ],
        },
    }))

    mod.install_herald_hooks()
    settings = _settings(root)

    # Bogus event gone, correct one present.
    assert "Stop_session" not in settings["hooks"]
    assert len(_herald_commands(settings, "SessionEnd")) == 1

    # Legacy flat entry replaced in place — not left alongside a new one.
    assert len(_herald_commands(settings, "Stop")) == 1
    assert not any("/old/path/" in c for c in _herald_commands(settings, "Stop"))

    # Unrelated config and third-party hooks survive untouched.
    assert settings["model"] == "opus"
    assert any(
        h["command"] == "bash /other/tool.sh"
        for entry in settings["hooks"]["Stop"]
        for h in entry.get("hooks", [])
    )


def test_reinstall_is_idempotent(hooks_mod):
    """Re-running setup updates the path instead of appending a duplicate."""
    mod, root = hooks_mod
    mod.install_herald_hooks()
    first = _settings(root)
    mod.install_herald_hooks()
    second = _settings(root)

    assert first == second
    for event in ("Stop", "Notification", "SessionEnd"):
        assert len(_herald_commands(second, event)) == 1


def test_uninstall_removes_both_shapes_and_leaves_others(hooks_mod):
    """Uninstall clears Herald entries written by any version, and nothing else."""
    mod, root = hooks_mod
    (root / ".claude" / "settings.json").write_text(json.dumps({
        "hooks": {
            "Stop": [
                {"hooks": [{"type": "command", "command": "bash /other/tool.sh"}]},
                {"command": "bash /old/herald/on-response.sh"},  # legacy flat
            ],
        },
    }))
    mod.install_herald_hooks()
    mod.uninstall_herald_hooks()

    settings = _settings(root)
    remaining = [
        h["command"]
        for entries in settings.get("hooks", {}).values()
        for entry in entries
        for h in entry.get("hooks", [])
    ]
    assert remaining == ["bash /other/tool.sh"]


def test_settings_write_is_atomic(hooks_mod, monkeypatch):
    """A crash mid-write must not truncate the user's live Claude config."""
    mod, root = hooks_mod
    settings_path = root / ".claude" / "settings.json"
    original = json.dumps({"model": "opus", "hooks": {}}, indent=2)
    settings_path.write_text(original)

    def boom(*a, **kw):
        raise RuntimeError("interrupted mid-write")

    monkeypatch.setattr(mod.json, "dump", boom)
    with pytest.raises(RuntimeError):
        mod._write_settings(settings_path, {"model": "opus", "hooks": {}})

    # Untouched, still valid JSON — the temp file took the damage.
    assert settings_path.read_text() == original
    assert not list((root / ".claude").glob(".settings-*.tmp"))
