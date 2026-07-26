"""Guard tests for the Claude Code plugin generator (DEF-227).

These pin the generated artifacts against the schema Claude Code actually
requires. The whole point of moving to a plugin was to stop guessing a third
party's config format — so the generator's OUTPUT is what needs asserting, not
that it ran without raising. `claude plugin validate` is the authoritative
check and runs in CI; these tests cover the same ground without needing the
binary, plus the wiring the validator does not look at (version tracking,
argv construction, uninstall).
"""

import json
from pathlib import Path

import pytest

from heyvox.setup import plugin as plugin_mod


@pytest.fixture()
def generated(tmp_path, monkeypatch):
    """Generate the plugin into a throwaway config dir."""
    monkeypatch.setattr("heyvox.config.CONFIG_DIR", tmp_path)
    plug = plugin_mod.generate()
    return plug, plugin_mod.marketplace_dir()


def _json(path: Path) -> dict:
    return json.loads(path.read_text())


def test_plugin_manifest_is_valid(generated):
    """name is the only required field; version must track the package."""
    from heyvox import __version__
    plug, _ = generated
    manifest = _json(plug / ".claude-plugin" / "plugin.json")
    assert manifest["name"] == "heyvox"
    # Claude Code caches by version string — a version that does not move with
    # the package would serve a stale plugin forever, silently.
    assert manifest["version"] == __version__


def test_marketplace_points_at_the_plugin(generated):
    """The catalog's source path must resolve to the generated plugin dir."""
    plug, mkt = generated
    catalog = _json(mkt / ".claude-plugin" / "marketplace.json")
    assert catalog["name"] == "heyvox"
    entries = catalog["plugins"]
    assert len(entries) == 1
    source = entries[0]["source"]
    assert (mkt / source).resolve() == plug.resolve()


def test_hooks_use_real_events_and_nested_shape(generated):
    """Same schema constraints that DEF-223 got wrong in settings.json."""
    plug, _ = generated
    hooks = _json(plug / "hooks" / "hooks.json")["hooks"]

    assert set(hooks) == {"Stop", "Notification", "SessionEnd"}
    assert "Stop_session" not in hooks  # not a Claude Code event

    for event, entries in hooks.items():
        for entry in entries:
            assert "hooks" in entry, f"{event}: flat entry is not dispatched"
            for handler in entry["hooks"]:
                assert handler["type"] == "command", f"{event}: type is mandatory"
                # Must stay relocatable — no absolute path baked in.
                assert "${CLAUDE_PLUGIN_ROOT}" in handler["command"], handler


def test_hook_scripts_are_shipped_and_executable(generated):
    """Every referenced script exists next to the _lib.sh it sources."""
    plug, _ = generated
    hooks_dir = plug / "hooks"
    referenced = {
        handler["command"].rsplit("/", 1)[-1].rstrip('"')
        for entries in _json(hooks_dir / "hooks.json")["hooks"].values()
        for entry in entries
        for handler in entry["hooks"]
    }
    assert referenced == {"on-response.sh", "on-notify.sh", "on-session-end.sh"}

    for name in referenced | {"_lib.sh"}:
        script = hooks_dir / name
        assert script.exists(), f"{name} missing from generated plugin"
        assert script.stat().st_mode & 0o111, f"{name} is not executable"


def test_mcp_server_points_at_this_interpreter(generated):
    """The MCP command must be the interpreter heyvox is installed into."""
    import sys
    plug, _ = generated
    servers = _json(plug / ".mcp.json")["mcpServers"]
    assert set(servers) == {"heyvox"}
    assert servers["heyvox"]["command"] == sys.executable
    assert servers["heyvox"]["args"] == ["-m", "heyvox.mcp.server"]


def test_generate_is_idempotent(tmp_path, monkeypatch):
    """Re-running setup rewrites in place rather than accumulating files."""
    monkeypatch.setattr("heyvox.config.CONFIG_DIR", tmp_path)
    plugin_mod.generate()
    first = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file())
    plugin_mod.generate()
    second = sorted(p.relative_to(tmp_path) for p in tmp_path.rglob("*") if p.is_file())
    assert first == second


def test_install_without_claude_cli_is_a_clean_skip(monkeypatch):
    """No Claude Code means no plugin — the wizard falls back, not crashes."""
    monkeypatch.setattr(plugin_mod, "claude_cli", lambda: None)
    results = plugin_mod.install()
    assert results == [(False, "Claude Code CLI not found — skipping plugin install")]


def test_update_uses_the_qualified_name(tmp_path, monkeypatch):
    """`claude plugin update` fails on the bare name — it must be name@marketplace."""
    monkeypatch.setattr("heyvox.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr(plugin_mod, "claude_cli", lambda: "/usr/bin/claude")
    monkeypatch.setattr(plugin_mod, "is_installed", lambda _c: True)

    calls: list[list[str]] = []

    def fake_run(args, timeout=120):
        calls.append(args)
        return True, "ok"

    monkeypatch.setattr(plugin_mod, "_run", fake_run)
    plugin_mod.install()

    update = next(c for c in calls if c[1:3] == ["plugin", "update"])
    assert update[3] == "heyvox@heyvox", "bare name fails with 'Plugin not found'"
    # install() is a no-op on an installed plugin, so it must not be used here.
    assert not any(c[1:3] == ["plugin", "install"] for c in calls)


def test_uninstall_removes_generated_files(tmp_path, monkeypatch):
    monkeypatch.setattr("heyvox.config.CONFIG_DIR", tmp_path)
    monkeypatch.setattr(plugin_mod, "claude_cli", lambda: "/usr/bin/claude")
    monkeypatch.setattr(plugin_mod, "_run", lambda args, timeout=120: (True, "ok"))

    plugin_mod.generate()
    assert plugin_mod.marketplace_dir().exists()
    plugin_mod.uninstall()
    assert not plugin_mod.marketplace_dir().exists()
