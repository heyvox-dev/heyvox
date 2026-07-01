"""
Audio cue playback for heyvox.

Plays .aiff sound files (listening, ok, paused, sending) via afplay.
Manages wake word suppression timing to prevent the mic from picking up
the cue sound and triggering a false wake word detection.
"""

import os
import signal
import subprocess
import threading
import time

# Auto-reap child processes (afplay) to prevent zombie accumulation.
signal.signal(signal.SIGCHLD, signal.SIG_IGN)

# Module-level suppression timestamp: wake word detection is skipped until this time.
_cue_suppress_until: float = 0.0
_suppress_lock = threading.Lock()

# Cache of decoded cue audio: cue_file path -> (data, samplerate). Populated on
# first successful soundfile.read() per cue file, reused thereafter to avoid
# disk I/O on the wake-word audible-feedback critical path. Plain dict is safe
# here: worst-case concurrent access is a redundant re-read of the same file
# under the GIL, never corruption.
_cue_cache: dict[str, tuple] = {}


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

    # Package dir is two levels up from this file (heyvox/audio/cues.py -> heyvox/audio/ -> heyvox/)
    package_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    resolved = os.path.join(package_dir, "cues")

    if not os.path.isdir(resolved):
        # Log warning — no crash, cues are optional
        print(f"WARNING: No cues directory found at {resolved}. Audio cues will be silent.", flush=True)

    return resolved


def _play_via_sounddevice(cue_file: str) -> bool:
    """Play a cue file via sounddevice using a pre-loaded, cached PCM buffer.

    Replaces the afplay subprocess spawn on the non-USB output path with a
    direct sounddevice.play() call, eliminating process-spawn latency on the
    wake-word audible-feedback critical path (WW_LATENCY).

    On cache miss, decodes the file via soundfile.read() and caches the
    result keyed by cue_file path. On cache hit, skips the disk read
    entirely. Any failure (missing/corrupt file, sounddevice/soundfile
    unavailable, device busy or removed mid-call) is swallowed and reported
    as False -- callers must fall back to the existing afplay path.

    Args:
        cue_file: Absolute path to the cue file (as constructed by audio_cue()).

    Returns:
        True if sounddevice.play() was successfully dispatched, False if any
        step failed and the caller should fall back to afplay.
    """
    try:
        if cue_file in _cue_cache:
            data, samplerate = _cue_cache[cue_file]
        else:
            import soundfile
            data, samplerate = soundfile.read(cue_file)
            _cue_cache[cue_file] = (data, samplerate)

        import sounddevice
        sounddevice.play(data, samplerate)
        return True
    except Exception:
        return False


def audio_cue(
    name: str,
    cues_dir: str | None = None,
    *,
    t1: float = 0.0,
    detect_ms: float = 0.0,
) -> None:
    """Play an audio cue by name and set wake word suppression window.

    Uses afplay (macOS built-in) to play the file asynchronously.
    Sets _cue_suppress_until to prevent the wake word detector from
    triggering on the cue audio bleeding back through the microphone.

    Args:
        name: Cue name without extension (e.g. "listening", "ok", "paused").
        cues_dir: Directory containing .aiff files. Defaults to package cues/.
        t1: perf_counter timestamp at trigger commit (from wake-word path). When
            non-zero, logs [WW_LATENCY] feedback/total lines at t2 dispatch point.
            Keyword-only so existing call sites are unaffected (default 0.0).
        detect_ms: Pre-computed (t1-t0)*1000 from the wake-word path. Passed in
            so the total latency line can be emitted here without needing t0.
            Keyword-only, default 0.0.
    """
    global _cue_suppress_until

    if cues_dir is None:
        cues_dir = get_cues_dir()

    cue_file = os.path.join(cues_dir, f"{name}.aiff")
    if not os.path.exists(cue_file):
        return

    # [WW_LATENCY] t2: dispatch timestamp, captured after file-existence check but
    # before the suppression window update and before any Popen/stream call. This
    # measures from trigger commit (t1) to the moment afplay/stream is invoked.
    if t1 > 0.0:
        t2 = time.perf_counter()
        feedback_ms = (t2 - t1) * 1000
        total_ms = detect_ms + feedback_ms
        print(
            f"[WW_LATENCY] feedback={feedback_ms:.0f}ms total={total_ms:.0f}ms cue={name}",
            flush=True,
        )

    # Estimate cue duration for suppression window (safe default for short files)
    duration = 1.0
    with _suppress_lock:
        _cue_suppress_until = time.time() + duration + 0.5

    # DEF-150: on a USB power-saving output (G535 over Lightspeed) a fresh
    # afplay process opens a cold stream and its short cue is swallowed. Route
    # the cue through the already-warm keep-alive stream instead. Returns False
    # on built-in/BT/virtual outputs (no keep-alive stream is held there) — then
    # fall back to afplay, which has no cold-start problem on those devices.
    try:
        from heyvox.audio.keepalive import play_cue_via_stream
        if play_cue_via_stream(name, cue_file):
            return
    except Exception:
        pass

    if not _play_via_sounddevice(cue_file):
        subprocess.Popen(
            ["afplay", cue_file],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )


def is_suppressed() -> bool:
    """Return True if wake word detection should be suppressed right now."""
    with _suppress_lock:
        return time.time() < _cue_suppress_until


def device_change_cue(device_name: str, device_type: str = "input") -> None:
    """Play a macOS system sound to notify the user of an audio device change.

    Uses the system Tink sound for a subtle notification.

    Args:
        device_name: Name of the new device (for logging).
        device_type: "input" (mic) or "output" (speaker).

    Requirement: AUDIO-11
    """
    global _cue_suppress_until

    # Use macOS system sounds — subtle ping for device changes
    sound = "/System/Library/Sounds/Tink.aiff"
    if not os.path.exists(sound):
        sound = "/System/Library/Sounds/Pop.aiff"
    if not os.path.exists(sound):
        return

    with _suppress_lock:
        _cue_suppress_until = time.time() + 1.0

    subprocess.Popen(
        ["afplay", sound],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
