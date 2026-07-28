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
    from heyvox.herald.orchestrator import HeraldOrchestrator, OrchestratorConfig

    ws_switch_cmd = ""
    ws_app_name = ""
    tts_min_volume: float | None = None
    switch_countdown_secs: float | None = None
    switch_cancel_key: str | None = None
    try:
        from heyvox.config import load_config
        cfg = load_config()
        for profile in cfg.app_profiles:
            if profile.has_workspace_detection and profile.workspace_switch_cmd:
                ws_switch_cmd = profile.workspace_switch_cmd
                ws_app_name = profile.name
                break
        tts_min_volume = float(cfg.tts.min_volume)
        switch_countdown_secs = float(cfg.workspace_switch.countdown_secs)
        switch_cancel_key = cfg.workspace_switch.cancel_key
    except Exception:
        pass
    orch_kwargs = dict(
        workspace_switch_cmd=ws_switch_cmd,
        workspace_app_name=ws_app_name,
    )
    if tts_min_volume is not None:
        orch_kwargs["tts_min_volume"] = tts_min_volume
    if switch_countdown_secs is not None:
        orch_kwargs["switch_countdown_secs"] = switch_countdown_secs
    if switch_cancel_key is not None:
        orch_kwargs["switch_cancel_key"] = switch_cancel_key
    orch_cfg = OrchestratorConfig(**orch_kwargs)
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
