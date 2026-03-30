"""
Audio cue playback for heyvox.

Plays .aiff sound files (listening, ok, paused, sending) via afplay.
Manages wake word suppression timing to prevent the mic from picking up
the cue sound and triggering a false wake word detection.
"""

import os
import subprocess
import time


# Module-level suppression timestamp: wake word detection is skipped until this time.
_cue_suppress_until: float = 0.0


def get_cues_dir(config_cues_dir: str = "") -> str:
    """Resolve the cues directory location.

    Args:
        config_cues_dir: Path from config (cues_dir field). If set and exists,
            use it directly. Otherwise, look for 'cues/' relative to the
            package installation root.

    Returns:
        Absolute path to the cues directory. May not exist if cues are missing.
    """
    if config_cues_dir and os.path.isdir(config_cues_dir):
        return config_cues_dir

    # Package root is two levels up from this file (vox/audio/cues.py -> vox/ -> package_root/)
    package_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    resolved = os.path.join(package_root, "cues")

    if not os.path.isdir(resolved):
        # Log warning — no crash, cues are optional
        print(f"WARNING: No cues directory found at {resolved}. Audio cues will be silent.", flush=True)

    return resolved


def audio_cue(name: str, cues_dir: str | None = None) -> None:
    """Play an audio cue by name and set wake word suppression window.

    Uses afplay (macOS built-in) to play the file asynchronously.
    Sets _cue_suppress_until to prevent the wake word detector from
    triggering on the cue audio bleeding back through the microphone.

    Args:
        name: Cue name without extension (e.g. "listening", "ok", "paused").
        cues_dir: Directory containing .aiff files. Defaults to package cues/.
    """
    global _cue_suppress_until

    if cues_dir is None:
        cues_dir = get_cues_dir()

    cue_file = os.path.join(cues_dir, f"{name}.aiff")
    if not os.path.exists(cue_file):
        return

    # Estimate cue duration for suppression window (safe default for short files)
    duration = 1.0
    _cue_suppress_until = time.time() + duration + 0.5

    subprocess.Popen(
        ["afplay", cue_file],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def is_suppressed() -> bool:
    """Return True if wake word detection should be suppressed right now."""
    return time.time() < _cue_suppress_until
