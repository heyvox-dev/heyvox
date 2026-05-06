"""Herald — TTS orchestration service for HeyVox.

Handles voice output: Kokoro TTS generation, queue management, audio ducking,
media pause/resume, and workspace-aware playback.
"""

from pathlib import Path

# Package root — used by hook shims
HERALD_HOME = Path(__file__).parent

# Subpackage paths (kept for backward compat, used by setup/hooks.py)
HERALD_HOOKS = HERALD_HOME / "hooks"


def get_herald_home() -> str:
    """Return HERALD_HOME path as string, for use in environment variables."""
    return str(HERALD_HOME)


def run_herald(*args: str, env: dict | None = None) -> int:
    """Run a Herald command via the Python CLI.

    Example: run_herald("speak", "Hello world")
    """
    from heyvox.herald.cli import dispatch
    return dispatch(list(args))


def start_orchestrator() -> None:
    """Start the Herald orchestrator daemon (blocking).

    Loads the app profile config to configure workspace switching.
    """
    from pathlib import Path as _Path
    from heyvox.herald.orchestrator import HeraldOrchestrator, OrchestratorConfig

    # DEF-092 hybrid C: lower bash idle gate from 5s to 2s, paired with
    # --force removal in _switch_workspace and the post-Sent flag grace
    # in recording.py. Brief listening pauses no longer false-skip;
    # real typing/clicking (sub-2s idle) still gates the switch.
    try:
        _Path("/tmp/herald-switch-idle-threshold").write_text("2\n")
    except OSError:
        pass

    ws_switch_cmd = ""
    ws_app_name = ""
    hold_queue_enabled = False
    try:
        from heyvox.config import load_config
        cfg = load_config()
        for profile in cfg.app_profiles:
            if profile.has_workspace_detection and profile.workspace_switch_cmd:
                ws_switch_cmd = profile.workspace_switch_cmd
                ws_app_name = profile.name
                break
        hold_queue_enabled = bool(cfg.hold_queue.enabled)
    except Exception:
        pass
    orch_cfg = OrchestratorConfig(
        workspace_switch_cmd=ws_switch_cmd,
        workspace_app_name=ws_app_name,
        hold_queue_enabled=hold_queue_enabled,
    )
    orch = HeraldOrchestrator(config=orch_cfg)
    orch.run()


# Python orchestrator
from heyvox.herald.orchestrator import HeraldOrchestrator, OrchestratorConfig  # noqa: E402

__all__ = [
    "HERALD_HOME",
    "HERALD_HOOKS",
    "get_herald_home",
    "run_herald",
    "start_orchestrator",
    "HeraldOrchestrator",
    "OrchestratorConfig",
]
