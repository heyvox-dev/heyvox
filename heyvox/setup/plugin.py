"""Claude Code plugin generator for HeyVox (DEF-227).

Replaces hand-editing the user's ~/.claude/settings.json with a plugin, which
Claude Code owns end to end: it carries the Herald hooks AND the MCP server as
one artifact, resolves its own paths via ${CLAUDE_PLUGIN_ROOT}, and uninstalls
cleanly. Verified to cost 0 always-on context tokens.

Why this exists: writing a third party's config format by hand went wrong twice
independently (DEF-223: flat hook entries and a non-existent "Stop_session"
event; the MCP key written to a file Claude Code does not read for MCP). Both
were silent — setup printed green checkmarks over dead config. A plugin removes
the guessing: the schema belongs to Claude Code, and `claude plugin validate`
checks our output against it.

Layout generated under CONFIG_DIR/claude-plugin/ (the marketplace root):

    .claude-plugin/marketplace.json     catalog listing the one plugin
    plugins/heyvox/
        .claude-plugin/plugin.json      name, version (tracks the package)
        hooks/hooks.json                Stop / Notification / SessionEnd
        hooks/*.sh                      copied from the installed package
        .mcp.json                       MCP server, sys.executable baked in

The plugin is GENERATED from the installed package rather than shipped
pre-built, so its version always equals the package version. That matters:
Claude Code caches by version string, so a stale version field would serve an
old plugin forever — silently, which is the exact failure mode this replaces.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

from heyvox.herald import HERALD_HOOKS

MARKETPLACE_NAME = "heyvox"
PLUGIN_NAME = "heyvox"
# Qualified name is REQUIRED by `claude plugin update` — the bare name fails
# with "Plugin not found". Install accepts either; update does not.
QUALIFIED_NAME = f"{PLUGIN_NAME}@{MARKETPLACE_NAME}"

# Claude Code event → hook script. These are the real event names; note
# SessionEnd, not "Stop_session" (DEF-223).
_HOOK_EVENTS = {
    "Stop": "on-response.sh",
    "Notification": "on-notify.sh",
    "SessionEnd": "on-session-end.sh",
}

# Sourced by every hook shim for the workspace-aware PYTHONPATH walk-up
# (DEF-111); without it the hooks fail with "no such file" on every event.
_HOOK_LIB = "_lib.sh"


def marketplace_dir() -> Path:
    """Root of the generated local marketplace."""
    from heyvox.config import CONFIG_DIR
    return Path(CONFIG_DIR) / "claude-plugin"


def plugin_dir() -> Path:
    """Root of the generated plugin itself."""
    return marketplace_dir() / "plugins" / PLUGIN_NAME


def claude_cli() -> str | None:
    """Path to the `claude` binary, or None if Claude Code isn't installed.

    Without it there is nothing to register a plugin with — callers fall back
    to the legacy settings.json path.
    """
    return shutil.which("claude")


def _package_version() -> str:
    from heyvox import __version__
    return __version__


def generate() -> Path:
    """Materialise the marketplace + plugin on disk. Returns the plugin dir.

    Safe to call repeatedly: files are rewritten in place, so a package upgrade
    refreshes the version and the hook scripts.
    """
    mkt = marketplace_dir()
    plug = plugin_dir()
    hooks = plug / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    (mkt / ".claude-plugin").mkdir(parents=True, exist_ok=True)
    (plug / ".claude-plugin").mkdir(parents=True, exist_ok=True)

    # Hook shims + the shared library they source.
    for script in (*_HOOK_EVENTS.values(), _HOOK_LIB):
        src = HERALD_HOOKS / script
        if not src.exists():
            raise FileNotFoundError(f"hook script missing from package: {src}")
        dest = hooks / script
        shutil.copy2(src, dest)
        dest.chmod(0o755)

    version = _package_version()

    _write_json(plug / ".claude-plugin" / "plugin.json", {
        "name": PLUGIN_NAME,
        "description": "Local voice layer — speaks responses aloud, exposes voice control tools",
        "version": version,
        "author": {"name": "Franz Felberer", "url": "https://heyvox.dev"},
        "license": "MIT",
        "homepage": "https://heyvox.dev",
        "keywords": ["voice", "tts", "speech", "accessibility"],
    })

    # ${CLAUDE_PLUGIN_ROOT} is expanded by Claude Code, so the plugin stays
    # relocatable and we never bake an absolute hook path into the config.
    _write_json(hooks / "hooks.json", {
        "hooks": {
            event: [{
                "hooks": [{
                    "type": "command",
                    "command": f'bash "${{CLAUDE_PLUGIN_ROOT}}/hooks/{script}"',
                }],
            }]
            for event, script in _HOOK_EVENTS.items()
        },
    })

    # sys.executable, not a resolver script: this is the interpreter heyvox is
    # actually installed into, known for certain at generation time.
    _write_json(plug / ".mcp.json", {
        "mcpServers": {
            "heyvox": {
                "command": sys.executable,
                "args": ["-m", "heyvox.mcp.server"],
            },
        },
    })

    _write_json(mkt / ".claude-plugin" / "marketplace.json", {
        "name": MARKETPLACE_NAME,
        "description": "HeyVox — local voice layer for AI coding agents",
        "owner": {"name": "Franz Felberer", "url": "https://heyvox.dev"},
        "plugins": [{
            "name": PLUGIN_NAME,
            "source": f"./plugins/{PLUGIN_NAME}",
            "description": "Local voice layer for AI coding agents",
        }],
    })

    return plug


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def _run(args: list[str], timeout: int = 120) -> tuple[bool, str]:
    """Run a `claude` subcommand. Returns (ok, combined output)."""
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        return False, "claude CLI not found"
    except subprocess.TimeoutExpired:
        return False, f"timed out after {timeout}s: {' '.join(args[:3])}"
    out = (proc.stdout + proc.stderr).strip()
    return proc.returncode == 0, out


def is_installed(claude: str) -> bool:
    ok, out = _run([claude, "plugin", "list"], timeout=60)
    return ok and QUALIFIED_NAME in out


def install() -> list[tuple[bool, str]]:
    """Generate the plugin and register it with Claude Code.

    Returns (success, message) tuples in the same shape as the legacy hooks
    installer, so the wizard can render either path identically.
    """
    results: list[tuple[bool, str]] = []

    claude = claude_cli()
    if not claude:
        return [(False, "Claude Code CLI not found — skipping plugin install")]

    try:
        plug = generate()
    except Exception as e:
        return [(False, f"Failed to generate plugin: {e}")]
    results.append((True, f"Plugin generated ({plug})"))

    mkt = str(marketplace_dir())
    already = is_installed(claude)

    # `marketplace add` is idempotent-ish but errors if the name is taken by a
    # different source; update covers the already-registered case.
    verb, past = ("update", "updated") if already else ("add", "added")
    args = ([claude, "plugin", "marketplace", "update", MARKETPLACE_NAME] if already
            else [claude, "plugin", "marketplace", "add", mkt])
    ok, out = _run(args)
    if not ok:
        return results + [(False, f"marketplace {verb} failed: {_tail(out)}")]
    results.append((True, f"Marketplace {past} ({MARKETPLACE_NAME})"))

    if already:
        # install() is a no-op on an installed plugin ("already installed"), so
        # a version bump only lands via update — and only with the QUALIFIED
        # name; the bare name fails with "Plugin not found".
        ok, out = _run([claude, "plugin", "update", QUALIFIED_NAME])
        results.append((ok, f"Plugin updated ({QUALIFIED_NAME})" if ok
                        else f"Plugin update failed: {_tail(out)}"))
    else:
        ok, out = _run([claude, "plugin", "install", QUALIFIED_NAME, "--scope", "user"])
        results.append((ok, f"Plugin installed ({QUALIFIED_NAME})" if ok
                        else f"Plugin install failed: {_tail(out)}"))

    return results


def uninstall() -> list[tuple[bool, str]]:
    """Remove the plugin and its marketplace, then delete the generated files."""
    results: list[tuple[bool, str]] = []
    claude = claude_cli()

    if claude:
        ok, out = _run([claude, "plugin", "uninstall", QUALIFIED_NAME], timeout=60)
        results.append((ok, f"Plugin uninstalled ({QUALIFIED_NAME})" if ok
                        else f"Plugin uninstall: {_tail(out)}"))
        ok, out = _run([claude, "plugin", "marketplace", "remove", MARKETPLACE_NAME], timeout=60)
        results.append((ok, f"Marketplace removed ({MARKETPLACE_NAME})" if ok
                        else f"Marketplace remove: {_tail(out)}"))
    else:
        results.append((False, "Claude Code CLI not found — remove the plugin manually"))

    mkt = marketplace_dir()
    if mkt.exists():
        try:
            shutil.rmtree(mkt)
            results.append((True, f"Generated files removed ({mkt})"))
        except OSError as e:
            results.append((False, f"Could not remove {mkt}: {e}"))

    return results


def _tail(output: str, limit: int = 160) -> str:
    """Last meaningful line of CLI output, for a one-line result message."""
    lines = [ln.strip() for ln in output.splitlines() if ln.strip()]
    return (lines[-1] if lines else "no output")[:limit]
