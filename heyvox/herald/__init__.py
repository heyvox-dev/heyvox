"""Herald — TTS orchestration service for HeyVox.

Handles voice output: Kokoro TTS generation, queue management, audio ducking,
media pause/resume, and workspace-aware playback.
"""

import os
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
    from heyvox.herald.orchestrator import HeraldOrchestrator, OrchestratorConfig
    ws_switch_cmd = ""
    ws_app_name = ""
    try:
        from heyvox.config import load_config
        cfg = load_config()
        for profile in cfg.app_profiles:
            if profile.has_workspace_detection and profile.workspace_switch_cmd:
                ws_switch_cmd = profile.workspace_switch_cmd
                ws_app_name = profile.name
                break
    except Exception:
        pass
    orch_cfg = OrchestratorConfig(
        workspace_switch_cmd=ws_switch_cmd,
        workspace_app_name=ws_app_name,
    )
    orch = HeraldOrchestrator(config=orch_cfg)
    orch.run()


# Python orchestrator (pure Python replacement for orchestrator.sh)
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
