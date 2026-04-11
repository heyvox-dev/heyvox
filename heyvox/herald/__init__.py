"""Herald — TTS orchestration service for HeyVox.

Handles voice output: Kokoro TTS generation, queue management, audio ducking,
media pause/resume, and workspace-aware playback.
"""

import os
from pathlib import Path

# Package root — used by bash scripts via HERALD_HOME env var
HERALD_HOME = Path(__file__).parent

# Export for Python consumers
HERALD_BIN = HERALD_HOME / "bin" / "herald"
HERALD_LIB = HERALD_HOME / "lib"
HERALD_DAEMON = HERALD_HOME / "daemon"
HERALD_HOOKS = HERALD_HOME / "hooks"
HERALD_MODES = HERALD_HOME / "modes"


def get_herald_home() -> str:
    """Return HERALD_HOME path as string, for use in environment variables."""
    return str(HERALD_HOME)


def run_herald(*args: str, env: dict | None = None) -> int:
    """Run a Herald CLI command.

    Example: run_herald("speak", "Hello world")
    """
    import subprocess

    cmd_env = os.environ.copy()
    cmd_env["HERALD_HOME"] = str(HERALD_HOME)
    if env:
        cmd_env.update(env)

    result = subprocess.run(
        ["bash", str(HERALD_BIN), *args],
        env=cmd_env,
        capture_output=True,
    )
    return result.returncode


# Python orchestrator (pure Python replacement for orchestrator.sh)
from heyvox.herald.orchestrator import HeraldOrchestrator, OrchestratorConfig

__all__ = [
    "HERALD_HOME",
    "HERALD_BIN",
    "HERALD_LIB",
    "HERALD_DAEMON",
    "HERALD_HOOKS",
    "HERALD_MODES",
    "get_herald_home",
    "run_herald",
    "HeraldOrchestrator",
    "OrchestratorConfig",
]
