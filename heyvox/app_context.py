"""
Shared application context for heyvox.

Replaces 17+ module-level globals in main.py with a single typed dataclass
passed via constructor injection.

Requirements: DECOMP-04
"""
from __future__ import annotations
import dataclasses
import threading
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    pass


@dataclasses.dataclass
class AppContext:
    """All shared mutable state for the heyvox main event loop.

    Designed to replace the 17+ module-level globals in main.py. Fields are
    grouped by concern: recording state, device state, HUD state, and process state.

    Usage::

        ctx = AppContext()
        # Pass ctx to functions instead of reading module globals:
        with ctx.lock:
            ctx.is_recording = True

    Notes:
        - All Lock/Event/list fields use ``dataclasses.field(default_factory=...)``
          so each AppContext instance gets its own independent objects.
        - Fields that reference external types (HUDClient) use TYPE_CHECKING guard
          to avoid circular imports at runtime.
    """

    # -------------------------------------------------------------------------
    # Recording state (protected by lock)
    # -------------------------------------------------------------------------

    lock: threading.Lock = dataclasses.field(default_factory=threading.Lock)
    """Protects is_recording, recording_start_time, busy, audio_buffer."""

    is_recording: bool = False
    recording_start_time: float = 0.0
    busy: bool = False
    busy_since: float = 0.0
    """Timestamp when busy flag was set (used by busy watchdog)."""

    audio_buffer: list = dataclasses.field(default_factory=list)
    triggered_by_ptt: bool = False
    recording_target: object = None
    """TargetSnapshot: app + text field captured at recording start."""

    cancel_transcription: threading.Event = dataclasses.field(
        default_factory=threading.Event
    )
    shutdown: threading.Event = dataclasses.field(default_factory=threading.Event)
    cancel_requested: threading.Event = dataclasses.field(
        default_factory=threading.Event
    )
    """Set by SIGUSR1 signal; checked in main loop."""

    adapter: object = None
    """AgentAdapter instance, initialized in main() via _build_adapter(config)."""

    last_inject_time: float = 0.0
    inject_lock: threading.Lock = dataclasses.field(default_factory=threading.Lock)

    # -------------------------------------------------------------------------
    # Device state (AUDIO-12, AUDIO-13)
    # -------------------------------------------------------------------------

    consecutive_failed_recordings: int = 0
    """Tracks consecutive failed recordings to detect zombie audio streams."""

    zombie_mic_reinit: bool = False
    """Set True to force mic reinit on next main loop iteration."""

    last_good_audio_time: float = 0.0
    """Updated whenever audio level >= threshold; drives dead mic detection."""

    dead_mic_zero_chunks: int = 0
    """Count of all-zero chunks since last_good_audio_time (AUDIO-13 diagnostic)."""

    dead_mic_low_chunks: int = 0
    """Count of chunks with level 1-9 since last_good_audio_time (AUDIO-13 diagnostic)."""

    # -------------------------------------------------------------------------
    # HUD state (Phase 5 — optional, never crashes main loop)
    # -------------------------------------------------------------------------

    hud_client: object = None
    """HUDClient instance (typed as object to avoid circular import at runtime)."""

    hud_last_reconnect: float = 0.0
    hud_last_level_send: float = 0.0

    # -------------------------------------------------------------------------
    # Process state
    # -------------------------------------------------------------------------

    indicator_proc: object = None
    """Subprocess handle for the HUD overlay process."""
