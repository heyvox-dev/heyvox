"""Herald Python Orchestrator — plays queued WAV files sequentially.

Pure Python replacement for heyvox/herald/lib/orchestrator.sh.

Features:
  - Audio ducking: lowers system volume during playback, then restores
  - Workspace auto-switch: switches app workspace if it's the frontmost app
  - Hold mode: if user is active, hold messages from other workspaces
  - Media pause/resume (Hush / MediaRemote) during playback
  - Recording watchdog: kills afplay if recording starts mid-playback
  - WAV normalization: RMS-based loudness matching inline in Python
  - Volume via CoreAudio ctypes (cached, no osascript per request)

Requirements: HERALD-01, HERALD-02, HERALD-03, HERALD-04
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration dataclass
# ---------------------------------------------------------------------------


from heyvox.constants import (  # noqa: E402 — after __future__ import
    HERALD_QUEUE_DIR, HERALD_HOLD_DIR, HERALD_HISTORY_DIR, HERALD_CLAIM_DIR,
    HERALD_DEBUG_LOG, HERALD_VIOLATIONS_LOG,
    HERALD_ORCH_PID, HERALD_PLAYING_PID, HERALD_ORIGINAL_VOL_FILE,
    HERALD_PAUSE_FLAG, HERALD_MUTE_FLAG, RECORDING_FLAG, HERALD_PLAY_NEXT,
    HERALD_LAST_PLAY, VERBOSITY_FILE, HERALD_WATCHER_HANDLED_DIR,
    TTS_PLAYING_FLAG,
)


@dataclass
class OrchestratorConfig:
    """All runtime configuration for the Herald orchestrator."""

    # Queue directories
    queue_dir: Path = field(default_factory=lambda: Path(HERALD_QUEUE_DIR))
    hold_dir: Path = field(default_factory=lambda: Path(HERALD_HOLD_DIR))
    history_dir: Path = field(default_factory=lambda: Path(HERALD_HISTORY_DIR))
    claim_dir: Path = field(default_factory=lambda: Path(HERALD_CLAIM_DIR))

    # Log file
    debug_log: Path = field(default_factory=lambda: Path(HERALD_DEBUG_LOG))
    violations_log: Path = field(default_factory=lambda: Path(HERALD_VIOLATIONS_LOG))

    # PID / lock files
    orch_pid_file: Path = field(default_factory=lambda: Path(HERALD_ORCH_PID))
    playing_pid_file: Path = field(default_factory=lambda: Path(HERALD_PLAYING_PID))
    original_vol_file: Path = field(default_factory=lambda: Path(HERALD_ORIGINAL_VOL_FILE))

    # State files (shared with worker.sh / main process)
    pause_flag: Path = field(default_factory=lambda: Path(HERALD_PAUSE_FLAG))
    mute_flag: Path = field(default_factory=lambda: Path(HERALD_MUTE_FLAG))
    recording_flag: Path = field(default_factory=lambda: Path(RECORDING_FLAG))
    play_next_flag: Path = field(default_factory=lambda: Path(HERALD_PLAY_NEXT))
    last_play_file: Path = field(default_factory=lambda: Path(HERALD_LAST_PLAY))
    verbosity_file: Path = field(default_factory=lambda: Path(VERBOSITY_FILE))

    # Herald home (for relative paths)
    herald_home: Path = field(
        default_factory=lambda: Path(__file__).parent
    )

    # App profile config for workspace switching (loaded from HeyvoxConfig)
    workspace_switch_cmd: str = ""  # Path to workspace switch CLI tool
    workspace_app_name: str = ""     # App name to check if frontmost

    # Audio ducking
    duck_enabled: bool = True
    duck_level: float = 0.03      # 3% — same as orchestrator.sh HERALD_DUCK_LEVEL=3/100
    # DEF-053: TTS plays at the user's pre-duck media volume. When the user's
    # music is at 37 %, TTS also plays at 37 %, which sounds "rather low" even
    # though the logic is working correctly. Enforce a minimum so TTS is always
    # audible regardless of the background media level. 0.55 ≈ normal speaking
    # level on the G435 / MacBook speakers without being jarring.
    tts_min_volume: float = 0.55

    # Queue caps
    max_queued: int = 10   # drop oldest messages when queue exceeds this
    max_held: int = 5

    # Media pause
    media_pause: bool = True
    resume_delay: float = 1.0

    # WAV normalization
    normalize_target_rms: int = 3000
    normalize_scale_cap: float = 3.0
    normalize_peak_limit: int = 24000

    # Poll interval
    poll_interval: float = 0.1

    # Recording flag staleness threshold
    recording_flag_max_age: int = 120  # seconds

    # Volume cache TTL (HERALD-04: at most every 5 seconds)
    volume_cache_ttl: float = 5.0

    # History cap
    history_cap: int = 50


# ---------------------------------------------------------------------------
# File-based logging (mirrors config.sh herald_log)
# ---------------------------------------------------------------------------


_log_lock = threading.Lock()
_LOG_ROTATE_SIZE = 2 * 1024 * 1024  # 2 MB


def _herald_log(msg: str, debug_log: Path) -> None:
    """Append a timestamped line to the Herald debug log."""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}\n"
    with _log_lock:
        try:
            with open(debug_log, "a") as f:
                f.write(line)
            size = debug_log.stat().st_size if debug_log.exists() else 0
            if size > _LOG_ROTATE_SIZE:
                rotated = debug_log.with_suffix(".log.1")
                shutil.move(str(debug_log), str(rotated))
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Queue garbage collection
# ---------------------------------------------------------------------------

_GC_INTERVAL = 60  # seconds — run GC at most once per minute
_last_gc: float = 0.0


def _gc_queue_dirs(cfg: "OrchestratorConfig", debug_log: "Path") -> int:
    """Remove orphaned WAV, timing, and workspace sidecar files.

    Runs at most once per _GC_INTERVAL seconds (frequency gate).
    Returns the count of files removed.
    """
    global _last_gc
    now = time.time()
    if now - _last_gc < _GC_INTERVAL:
        return 0
    _last_gc = now

    removed = 0
    # Directory -> max age threshold in seconds
    dir_thresholds = [
        (cfg.queue_dir, 3600),    # 1 hour
        (cfg.hold_dir, 14400),    # 4 hours
        (cfg.history_dir, 86400), # 24 hours
        (cfg.claim_dir, 3600),    # 1 hour (replaces inline claim GC)
    ]
    patterns = ["*.wav", "*.txt", "*.workspace", "*.parts"]

    for directory, max_age in dir_thresholds:
        if not directory.exists():
            continue
        for pattern in patterns:
            for f in directory.glob(pattern):
                try:
                    if (now - f.stat().st_mtime) > max_age:
                        f.unlink(missing_ok=True)
                        _herald_log(f"GC: removed orphaned {f.name}", debug_log)
                        removed += 1
                except OSError:
                    pass

    # Also clean watcher handled dir
    handled_dir = Path(HERALD_WATCHER_HANDLED_DIR)
    if handled_dir.exists():
        for f in handled_dir.iterdir():
            try:
                if f.is_file() and (now - f.stat().st_mtime) > 3600:
                    f.unlink(missing_ok=True)
                    removed += 1
            except OSError:
                pass

    return removed


# ---------------------------------------------------------------------------
# Parts manifest check — prevents premature restore between multi-part TTS
# ---------------------------------------------------------------------------


def _parts_pending(queue_dir: Path, max_age: float = 10.0) -> bool:
    """Check if any .parts manifest files indicate more WAVs are coming.

    Workers write a {timestamp}.parts file when multi-part generation starts,
    and remove it after all parts are enqueued. If one exists and is fresh,
    the orchestrator should not restore volume / resume media yet.

    Stale manifests (> max_age seconds) are cleaned up to prevent hangs
    from crashed workers.
    """
    now = time.time()
    for pf in queue_dir.glob("*.parts"):
        try:
            if now - pf.stat().st_mtime < max_age:
                return True
            else:
                pf.unlink(missing_ok=True)
        except OSError:
            pass
    return False


# WAV normalization (legacy fallback — primary normalization is in kokoro-daemon.py)


def normalize_wav(path: Path, target_rms: int = 3000, scale_cap: float = 3.0,
                  peak_limit: int = 24000) -> None:
    """Normalize WAV loudness in-place via RMS matching.

    Thin wrapper around heyvox.audio.normalize.normalize_wav_int16.
    Legacy fallback for externally-generated WAVs. Primary normalization
    happens in kokoro-daemon.py at generation time (HERALD-02).
    """
    from heyvox.audio.normalize import normalize_wav_int16

    try:
        with wave.open(str(path), "rb") as wf:
            params = wf.getparams()
            raw_frames = wf.readframes(params.nframes)

        normalized = normalize_wav_int16(raw_frames, target_rms, scale_cap, peak_limit)
        if normalized is not raw_frames:
            with wave.open(str(path), "wb") as wf:
                wf.setparams(params)
                wf.writeframes(normalized)
    except Exception as e:
        log.debug("normalize_wav(%s) failed: %s", path, e)


# ---------------------------------------------------------------------------
# State helpers
# ---------------------------------------------------------------------------


def _is_paused(cfg: OrchestratorConfig, debug_log: Path) -> bool:
    """Check if Herald is paused (manual pause or recording in progress)."""
    if cfg.pause_flag.exists():
        return True
    if cfg.recording_flag.exists():
        try:
            age = time.time() - cfg.recording_flag.stat().st_mtime
            if age > cfg.recording_flag_max_age:
                cfg.recording_flag.unlink(missing_ok=True)
                _herald_log(
                    f"ORCH: removed stale recording flag (age={age:.0f}s)", debug_log
                )
                return False
        except OSError:
            pass
        return True
    return False


def _is_muted(cfg: OrchestratorConfig) -> bool:
    """Check if Herald is muted (flag file or system mute)."""
    if cfg.mute_flag.exists():
        return True
    try:
        from heyvox.herald.coreaudio import is_system_muted
        return is_system_muted()
    except Exception:
        return False


def _get_verbosity(cfg: OrchestratorConfig) -> str:
    """Read verbosity from shared flag file. Default 'full'."""
    try:
        return cfg.verbosity_file.read_text().strip() or "full"
    except OSError:
        return "full"


def _is_skip(cfg: OrchestratorConfig) -> bool:
    return _get_verbosity(cfg) == "skip"


def _user_is_active(cfg: OrchestratorConfig) -> bool:
    """Return True if user was recently listening (within 15s) or Herald is paused."""
    if _is_paused(cfg, cfg.debug_log):
        return True
    try:
        last_play = float(cfg.last_play_file.read_text().strip())
        return (time.time() - last_play) < 15
    except (OSError, ValueError):
        return False


def _workspace_app_is_frontmost(cfg: OrchestratorConfig) -> bool:
    """Return True if the workspace-aware app is the frontmost application.

    Uses cfg.workspace_app_name to check. Returns False if no app name configured.
    """
    if not cfg.workspace_app_name:
        return False
    app_lower = cfg.workspace_app_name.lower()
    try:
        import AppKit  # type: ignore
        app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return False
        return app.localizedName().lower() == app_lower
    except Exception:
        pass
    # Fallback: osascript (System Events returns lowercase process names)
    try:
        r = subprocess.run(
            ["osascript", "-e",
             "tell application \"System Events\" to get name of first application process whose frontmost is true"],
            capture_output=True, text=True, timeout=3.0,
        )
        return r.stdout.strip().lower() == app_lower
    except Exception:
        return False


def _switch_workspace(workspace: str, cfg: OrchestratorConfig) -> None:
    """Switch the workspace-aware app to the given workspace name.

    Uses cfg.workspace_switch_cmd from the app profile. Falls back to
    searching PATH and ~/.local/bin/ if not explicitly configured.
    """
    if not cfg.workspace_switch_cmd:
        return
    switch_cmd = os.path.expanduser(cfg.workspace_switch_cmd)
    if not Path(switch_cmd).exists():
        # Try PATH
        found = shutil.which(os.path.basename(switch_cmd))
        if found:
            switch_cmd = found
        else:
            return
    try:
        subprocess.run(
            [switch_cmd, workspace],
            capture_output=True, timeout=5.0,
        )
    except Exception as e:
        _herald_log(f"ORCH: workspace switch failed: {e}", cfg.debug_log)


def _notify_held(workspace: str, cfg: OrchestratorConfig) -> None:
    """Send a Hammerspoon notification for a held workspace message."""
    held = list(cfg.hold_dir.glob("*.wav"))
    count = len(held)
    ws_escaped = workspace.replace("'", "\\'")
    hs = shutil.which("hs") or "/opt/homebrew/bin/hs"
    if not Path(hs).exists():
        return
    script = (
        f"hs.notify.new({{"
        f"title='Workspace message held',"
        f"informativeText='{ws_escaped} has a message ({count} pending). Press Cmd+Shift+N to play.',"
        f"withdrawAfter=10}}"
        f"):send(); hs.alert.show('{ws_escaped}: message held ({count})', 2)"
    )
    try:
        subprocess.Popen([hs, "-c", script],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Media pause/resume (via heyvox.audio.media)
# ---------------------------------------------------------------------------


def _media_pause(cfg: OrchestratorConfig) -> None:
    """Pause browser / native media via heyvox.audio.media."""
    if not cfg.media_pause:
        return
    try:
        from heyvox.audio.media import pause_media
        pause_media()
    except Exception as e:
        _herald_log(f"ORCH: media pause failed: {e}", cfg.debug_log)


def _media_resume(cfg: OrchestratorConfig) -> None:
    """Resume browser / native media via heyvox.audio.media."""
    if not cfg.media_pause:
        return
    try:
        from heyvox.audio.media import resume_media
        resume_media()
    except Exception as e:
        _herald_log(f"ORCH: media resume failed: {e}", cfg.debug_log)


# ---------------------------------------------------------------------------
# Audio ducking
# ---------------------------------------------------------------------------


def _parse_ducked_state(text: str) -> tuple[int | None, float] | None:
    """Parse the ducked-state sidecar. Supports legacy 'vol' and new 'dev_id:vol'."""
    text = text.strip()
    if not text:
        return None
    if ":" in text:
        dev_str, vol_str = text.split(":", 1)
        try:
            return int(dev_str), float(vol_str)
        except ValueError:
            return None
    try:
        return None, float(text)  # legacy: volume only, no device pinning
    except ValueError:
        return None


def _duck_audio(cfg: OrchestratorConfig, debug_log: Path) -> float | None:
    """Lower system volume for TTS ducking. Returns the original volume or None.

    DEF-046: Saves the ducked device_id alongside the volume so that restore
    always targets the originally-ducked device, even if the user switches
    the default output mid-playback. Without the device pin, the duck level
    sticks on device A while restore writes to device B, leaving A at 3%.
    """
    if not cfg.duck_enabled:
        return None

    from heyvox.herald.coreaudio import (
        _get_default_output_device, _set_volume_coreaudio,
        get_system_volume_cached, set_system_volume_cached,
    )

    # Only save original if not already ducked (avoid saving already-ducked level on restart)
    if cfg.original_vol_file.exists():
        try:
            parsed = _parse_ducked_state(cfg.original_vol_file.read_text())
            if parsed is not None:
                _dev_id, saved = parsed
                # Duck the pinned device if we have one, else current default
                if _dev_id is not None:
                    _set_volume_coreaudio(_dev_id, cfg.duck_level)
                else:
                    set_system_volume_cached(cfg.duck_level)
                time.sleep(0.05)
                return saved
        except OSError:
            pass

    original_vol = get_system_volume_cached(cfg.volume_cache_ttl)
    dev_id = _get_default_output_device()
    try:
        if dev_id is not None:
            cfg.original_vol_file.write_text(f"{dev_id}:{original_vol}")
        else:
            cfg.original_vol_file.write_text(str(original_vol))
    except OSError:
        pass
    set_system_volume_cached(cfg.duck_level)
    time.sleep(0.05)
    _herald_log(
        f"ORCH: ducked audio from {original_vol:.2f} to {cfg.duck_level:.2f} (dev={dev_id})",
        debug_log,
    )
    return original_vol


def _set_tts_volume(original_vol: float | None, cfg: OrchestratorConfig) -> None:
    """Restore volume to TTS (full) level after ducking.

    Targets the originally-ducked device via the sidecar file so that a mid-
    playback output device change doesn't leave the previous device muted.
    """
    if not cfg.duck_enabled or original_vol is None:
        _herald_log(
            f"ORCH: _set_tts_volume skipped (duck_enabled={cfg.duck_enabled} "
            f"original_vol={original_vol})",
            cfg.debug_log,
        )
        return
    # DEF-053: enforce minimum TTS volume floor so TTS stays audible even when
    # the user's pre-duck media volume was low (e.g. background music at 37 %).
    tts_vol = max(original_vol, cfg.tts_min_volume)
    from heyvox.herald.coreaudio import _set_volume_coreaudio, set_system_volume_cached
    dev_id = None
    try:
        parsed = _parse_ducked_state(cfg.original_vol_file.read_text())
        if parsed is not None:
            dev_id, _ = parsed
    except (OSError, ValueError):
        pass
    if dev_id is not None:
        ok = _set_volume_coreaudio(dev_id, tts_vol)
        _herald_log(
            f"ORCH: set TTS volume to {tts_vol:.2f} (orig={original_vol:.2f}, "
            f"floor={cfg.tts_min_volume:.2f}) via CA dev={dev_id} ok={ok}",
            cfg.debug_log,
        )
    else:
        set_system_volume_cached(tts_vol)
        _herald_log(
            f"ORCH: set TTS volume to {tts_vol:.2f} (orig={original_vol:.2f}, "
            f"floor={cfg.tts_min_volume:.2f}) via system-cached (dev=None)",
            cfg.debug_log,
        )


def _restore_audio(original_vol: float | None, cfg: OrchestratorConfig, debug_log: Path) -> None:
    """Restore volume after all TTS parts are done.

    DEF-046: Restores to the pinned device_id captured at duck time, not the
    current default. If the user switched output during playback, the original
    device would otherwise stay stuck at 3%.
    """
    if not cfg.duck_enabled:
        return

    from heyvox.herald.coreaudio import _set_volume_coreaudio, set_system_volume_cached

    dev_id: int | None = None
    vol = original_vol

    # Read sidecar to get the pinned device (and volume as fallback)
    try:
        parsed = _parse_ducked_state(cfg.original_vol_file.read_text())
        if parsed is not None:
            file_dev, file_vol = parsed
            dev_id = file_dev
            if vol is None:
                vol = file_vol
    except (OSError, ValueError):
        pass

    if vol is None:
        return

    if dev_id is not None:
        _set_volume_coreaudio(dev_id, vol)
    else:
        set_system_volume_cached(vol)
    cfg.original_vol_file.unlink(missing_ok=True)
    _herald_log(f"ORCH: restored audio to {vol:.2f} (dev={dev_id})", debug_log)


# ---------------------------------------------------------------------------
# Violation check
# ---------------------------------------------------------------------------


def _violation_check(context: str, cfg: OrchestratorConfig) -> bool:
    """Log a violation if TTS is playing during recording. Returns True if violated."""
    reasons = []
    if cfg.pause_flag.exists():
        reasons.append("herald-pause flag present")
    if cfg.recording_flag.exists():
        reasons.append("heyvox-recording flag present")
    if not reasons:
        return False
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    reason_str = " + ".join(reasons)
    entry = f"[{ts}] VIOLATION in {context}: {reason_str}\n"
    try:
        with open(cfg.violations_log, "a") as f:
            f.write(entry)
    except OSError:
        pass
    _herald_log(f"VIOLATION: {context} — {reason_str}", cfg.debug_log)
    return True


# ---------------------------------------------------------------------------
# Queue management helpers
# ---------------------------------------------------------------------------


def _purge_message_parts(msg_prefix: str, queue_dir: Path, debug_log: Path) -> int:
    """Remove all remaining WAV parts for a message prefix from the queue.

    Returns number of files purged.
    """
    purged = 0
    for wav in sorted(queue_dir.glob("*.wav")):
        prefix = wav.name.split("-")[0] if "-" in wav.name else wav.name
        if prefix == msg_prefix:
            wav.unlink(missing_ok=True)
            wav.with_suffix(".workspace").unlink(missing_ok=True)
            wav.with_suffix(".timing").unlink(missing_ok=True)
            purged += 1
    if purged:
        _herald_log(f"ORCH: purged {purged} remaining parts of interrupted message {msg_prefix}", debug_log)
    return purged


def _enforce_queue_cap(cfg: "OrchestratorConfig", debug_log: Path) -> int:
    """Drop oldest complete messages when queue exceeds cap.

    Returns number of files dropped.
    """
    queue_wavs = sorted(cfg.queue_dir.glob("*.wav"))
    if len(queue_wavs) <= cfg.max_queued:
        return 0

    # Group by message prefix
    messages: dict[str, list[Path]] = {}
    for wav in queue_wavs:
        prefix = wav.name.split("-")[0] if "-" in wav.name else wav.name
        messages.setdefault(prefix, []).append(wav)

    # Drop oldest complete messages until under cap
    dropped = 0
    msg_prefixes = list(messages.keys())  # already sorted (timestamp-based names)
    for prefix in msg_prefixes:
        if len(queue_wavs) - dropped <= cfg.max_queued:
            break
        parts = messages[prefix]
        for wav in parts:
            wav.unlink(missing_ok=True)
            wav.with_suffix(".workspace").unlink(missing_ok=True)
            wav.with_suffix(".timing").unlink(missing_ok=True)
            dropped += 1
        _herald_log(f"ORCH: dropped {len(parts)} parts of {prefix} (queue cap={cfg.max_queued})", debug_log)

    return dropped


# ---------------------------------------------------------------------------
# WAV playback
# ---------------------------------------------------------------------------


def _play_wav(
    wav_file: Path,
    last_msg_prefix: str,
    current_workspace: str,
    original_vol: float | None,
    cfg: OrchestratorConfig,
) -> tuple[str, str, float | None, bool]:
    """Play a single WAV file, handling ducking, pausing, and workspace switching.

    Returns:
        (new_last_msg_prefix, new_current_workspace, original_vol, was_interrupted)
    """
    debug_log = cfg.debug_log
    basename = wav_file.name
    workspace_file = wav_file.with_suffix(".workspace")

    msg_prefix = basename.split("-")[0] if "-" in basename else basename
    is_continuation = bool(last_msg_prefix and msg_prefix == last_msg_prefix)
    last_msg_prefix = msg_prefix

    # Wait while paused
    while _is_paused(cfg, debug_log):
        _herald_log(f"ORCH: waiting (paused) for {basename}", debug_log)
        time.sleep(0.3)

    if not is_continuation:
        # Workspace switch -- only if the workspace-aware app is frontmost
        if workspace_file.exists():
            try:
                ws = workspace_file.read_text().strip()
                current_workspace = ws
                if _workspace_app_is_frontmost(cfg):
                    _switch_workspace(ws, cfg)
                    time.sleep(0.3)
                else:
                    _herald_log("ORCH: skipping workspace switch (app not frontmost)", debug_log)
                workspace_file.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass

        if cfg.media_pause:
            _media_pause(cfg)
            _herald_log("ORCH: media PAUSED", debug_log)

        original_vol = _duck_audio(cfg, debug_log)
        _set_tts_volume(original_vol, cfg)
    else:
        workspace_file.unlink(missing_ok=True)

    file_size = wav_file.stat().st_size if wav_file.exists() else 0
    _herald_log(
        f"ORCH: playing {wav_file} size={file_size} cont={is_continuation} ws={current_workspace}",
        debug_log,
    )

    # Archive to history
    cfg.history_dir.mkdir(parents=True, exist_ok=True)
    hist_name = f"{time.strftime('%Y%m%d-%H%M%S')}-{basename}"
    try:
        shutil.copy2(str(wav_file), str(cfg.history_dir / hist_name))
    except OSError:
        pass

    if not is_continuation:
        # Purge old history (keep 50)
        try:
            hist_wavs = sorted(
                cfg.history_dir.glob("*.wav"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for old in hist_wavs[cfg.history_cap:]:
                old.unlink(missing_ok=True)
        except OSError:
            pass

    # Final pause check before playback
    if _is_paused(cfg, debug_log):
        _herald_log(f"ORCH: BLOCKED at afplay gate (pause detected) for {basename}", debug_log)
        while _is_paused(cfg, debug_log):
            time.sleep(0.3)
        _herald_log(f"ORCH: unblocked, proceeding with {basename}", debug_log)

    # Violation check pre-play
    _violation_check(f"orchestrator:pre-play:{basename}", cfg)

    # Play via afplay with watchdog thread
    proc = subprocess.Popen(["afplay", str(wav_file)])
    try:
        cfg.playing_pid_file.write_text(str(proc.pid))
    except OSError:
        pass
    # Dual-write: atomic state file (primary) + legacy flag file (parallel write).
    # main.py echo suppression reads both; atomic state is the new source of truth.
    try:
        from heyvox.ipc import update_state
        update_state({"herald_playing_pid": proc.pid, "tts_playing": True})
    except Exception:
        pass
    try:
        open(TTS_PLAYING_FLAG, "w").close()
    except OSError:
        pass

    # Watchdog: kill afplay if recording starts mid-playback
    watchdog_stop = threading.Event()

    def _watchdog():
        while not watchdog_stop.is_set():
            if proc.poll() is not None:
                break
            if _is_paused(cfg, debug_log):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                _violation_check(f"orchestrator:watchdog-kill:{basename}", cfg)
                _herald_log(f"ORCH: WATCHDOG killed afplay (recording started during playback)", debug_log)
                break
            time.sleep(0.1)

    watchdog_thread = threading.Thread(target=_watchdog, daemon=True)
    watchdog_thread.start()

    play_exit = proc.wait()
    watchdog_stop.set()
    watchdog_thread.join(timeout=0.5)
    cfg.playing_pid_file.unlink(missing_ok=True)
    # Dual-write: clear atomic state (primary) + remove legacy flag file (parallel).
    try:
        from heyvox.ipc import update_state
        update_state({"herald_playing_pid": None, "tts_playing": False})
    except Exception:
        pass
    try:
        os.unlink(TTS_PLAYING_FLAG)
    except FileNotFoundError:
        pass

    # If watchdog killed playback, wait for recording to finish
    was_interrupted = play_exit != 0 and _is_paused(cfg, debug_log)
    if was_interrupted:
        _herald_log("ORCH: playback interrupted, waiting for pause to clear", debug_log)
        while _is_paused(cfg, debug_log):
            time.sleep(0.3)

    wav_file.unlink(missing_ok=True)

    # Record last play timestamp
    try:
        cfg.last_play_file.write_text(str(int(time.time())))
    except OSError:
        pass
    try:
        from heyvox.ipc import update_state
        update_state({"last_play_ts": time.time()})
    except Exception:
        pass

    # Check if queue and hold are empty → resume media + restore volume
    # Also check for .parts manifests — a worker may still be generating parts.
    queue_empty = not any(cfg.queue_dir.glob("*.wav"))
    hold_empty = not any(cfg.hold_dir.glob("*.wav"))
    parts_coming = _parts_pending(cfg.queue_dir)
    if queue_empty and hold_empty and not parts_coming:
        if cfg.media_pause:
            _media_resume(cfg)
            _herald_log("ORCH: media RESUMED", debug_log)
        _restore_audio(original_vol, cfg, debug_log)
        original_vol = None

    return last_msg_prefix, current_workspace, original_vol, was_interrupted


# ---------------------------------------------------------------------------
# Main orchestrator class
# ---------------------------------------------------------------------------


class HeraldOrchestrator:
    """Pure-Python Herald orchestrator — equivalent to orchestrator.sh.

    Runs as a singleton daemon process. Polls the herald-queue directory,
    plays WAV files via afplay, handles audio ducking, workspace switching,
    hold queue, and recording watchdog.

    Usage:
        orch = HeraldOrchestrator()
        orch.run()  # blocks until orch.stop() called from another thread
    """

    def __init__(self, config: OrchestratorConfig | None = None) -> None:
        self.cfg = config or OrchestratorConfig()
        self._stop_event = threading.Event()

    def stop(self) -> None:
        """Signal the main loop to exit cleanly."""
        self._stop_event.set()

    def _cleanup(self, original_vol: float | None) -> None:
        """Restore state on exit."""
        cfg = self.cfg
        debug_log = cfg.debug_log
        _herald_log(f"ORCH DYING: pid={os.getpid()}", debug_log)
        if cfg.media_pause:
            _media_resume(cfg)
            _herald_log("ORCH: media RESUMED (cleanup)", debug_log)
        _restore_audio(original_vol, cfg, debug_log)
        # Only remove PID file if it still contains our PID
        try:
            if cfg.orch_pid_file.read_text().strip() == str(os.getpid()):
                cfg.orch_pid_file.unlink(missing_ok=True)
        except OSError:
            pass
        # Release singleton lock
        lock_fd = getattr(self, "_lock_fd", None)
        if lock_fd is not None:
            try:
                import fcntl
                fcntl.lockf(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            except OSError:
                pass
        cfg.playing_pid_file.unlink(missing_ok=True)
        cfg.play_next_flag.unlink(missing_ok=True)
        try:
            from heyvox.ipc import update_state
            update_state({"herald_playing_pid": None, "tts_playing": False})
        except Exception:
            pass

    def run(self) -> None:
        """Main orchestrator loop — blocks until stop() is called or signal received."""
        cfg = self.cfg
        debug_log = cfg.debug_log

        # Ensure runtime directories exist
        for d in (cfg.queue_dir, cfg.hold_dir, cfg.history_dir, cfg.claim_dir):
            d.mkdir(parents=True, exist_ok=True)

        # Singleton lock: only one orchestrator can run at a time.
        # Use lockf (POSIX record locks via fcntl F_SETLK) — more reliable on
        # macOS than BSD flock() which failed under simultaneous spawns.
        import fcntl
        lock_path = str(cfg.orch_pid_file) + ".lock"
        self._lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o644)
        try:
            fcntl.lockf(self._lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            _herald_log("ORCH: another orchestrator holds the lock — exiting", debug_log)
            os.close(self._lock_fd)
            self._lock_fd = None
            return

        # Write PID file
        try:
            cfg.orch_pid_file.write_text(str(os.getpid()))
        except OSError:
            pass

        _herald_log(f"ORCH: started (pid={os.getpid()})", debug_log)

        original_vol: float | None = None
        current_workspace: str = ""
        last_msg_prefix: str = ""

        # Signal handlers
        def _handle_signal(signum, frame):
            self._stop_event.set()

        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            try:
                signal.signal(sig, _handle_signal)
            except (OSError, ValueError):
                pass

        try:
            while not self._stop_event.is_set():
                # Play-next flag: drain hold queue first
                if cfg.play_next_flag.exists():
                    cfg.play_next_flag.unlink(missing_ok=True)
                    held = sorted(cfg.hold_dir.glob("*.wav"))
                    if held:
                        next_held = held[0]
                        if next_held.exists():
                            last_msg_prefix, current_workspace, original_vol, _ = _play_wav(
                                next_held, last_msg_prefix, current_workspace, original_vol, cfg
                            )
                            remaining = list(cfg.hold_dir.glob("*.wav"))
                            if remaining:
                                self._show_alert(f"{len(remaining)} more pending")
                            continue

                # Enforce queue cap before picking next file
                _enforce_queue_cap(cfg, debug_log)

                # Find next WAV in queue (sorted by name = timestamp order)
                queue_wavs = sorted(cfg.queue_dir.glob("*.wav"))

                if queue_wavs:
                    next_wav = queue_wavs[0]
                    if not next_wav.exists():
                        continue

                    # Skip if muted or skip-verbosity
                    if _is_muted(cfg) or _is_skip(cfg):
                        next_wav.unlink(missing_ok=True)
                        next_wav.with_suffix(".workspace").unlink(missing_ok=True)
                        continue

                    # Workspace hold logic
                    workspace_file = next_wav.with_suffix(".workspace")
                    next_workspace = ""
                    if workspace_file.exists():
                        try:
                            next_workspace = workspace_file.read_text().strip()
                        except (OSError, ValueError):
                            pass

                    if (
                        next_workspace
                        and current_workspace
                        and next_workspace != current_workspace
                        and _user_is_active(cfg)
                    ):
                        # Move to hold queue
                        basename = next_wav.name
                        hold_target = cfg.hold_dir / basename
                        try:
                            shutil.move(str(next_wav), str(hold_target))
                            if workspace_file.exists():
                                shutil.move(
                                    str(workspace_file),
                                    str(cfg.hold_dir / workspace_file.name),
                                )
                        except (OSError, ValueError):
                            pass
                        _herald_log(
                            f"ORCH: held {basename} from {next_workspace} (user active on {current_workspace})",
                            debug_log,
                        )
                        if not _is_paused(cfg, debug_log):
                            _notify_held(next_workspace, cfg)

                        # Enforce hold cap
                        held_wavs = sorted(
                            cfg.hold_dir.glob("*.wav"),
                            key=lambda p: p.stat().st_mtime,
                        )
                        excess = len(held_wavs) - cfg.max_held
                        if excess > 0:
                            for old in held_wavs[:excess]:
                                old.unlink(missing_ok=True)
                                old.with_suffix(".workspace").unlink(missing_ok=True)
                                _herald_log(
                                    f"ORCH: dropped oldest held {old.name} (cap={cfg.max_held})",
                                    debug_log,
                                )
                        continue

                    last_msg_prefix, current_workspace, original_vol, interrupted = _play_wav(
                        next_wav, last_msg_prefix, current_workspace, original_vol, cfg
                    )

                    # If recording interrupted playback, drop remaining parts of this message
                    if interrupted and last_msg_prefix:
                        _purge_message_parts(last_msg_prefix, cfg.queue_dir, debug_log)
                        last_msg_prefix = ""  # reset so next message isn't treated as continuation

                else:
                    # Queue empty — check hold queue auto-drain
                    held_wavs = sorted(cfg.hold_dir.glob("*.wav"))
                    if held_wavs and not _user_is_active(cfg):
                        _herald_log(
                            f"ORCH: auto-draining held queue ({len(held_wavs)} pending)",
                            debug_log,
                        )
                        # Play all consecutive parts of the same message back-to-back
                        while held_wavs:
                            next_held = held_wavs[0]
                            if not next_held.exists():
                                held_wavs = held_wavs[1:]
                                continue
                            last_msg_prefix, current_workspace, original_vol, interrupted = _play_wav(
                                next_held, last_msg_prefix, current_workspace, original_vol, cfg
                            )
                            if interrupted and last_msg_prefix:
                                # Purge remaining parts of this message from hold too
                                for h in list(cfg.hold_dir.glob("*.wav")):
                                    hp = h.name.split("-")[0] if "-" in h.name else h.name
                                    if hp == last_msg_prefix:
                                        h.unlink(missing_ok=True)
                                        h.with_suffix(".workspace").unlink(missing_ok=True)
                                _herald_log(f"ORCH: purged held parts of interrupted {last_msg_prefix}", debug_log)
                                last_msg_prefix = ""
                                break
                            # Check if next held file is a continuation of same message
                            held_wavs = sorted(cfg.hold_dir.glob("*.wav"))
                            if not held_wavs:
                                break
                            next_prefix = held_wavs[0].name.split("-")[0] if "-" in held_wavs[0].name else ""
                            if next_prefix != last_msg_prefix:
                                break  # Different message — re-enter main loop for activity check
                        remaining = list(cfg.hold_dir.glob("*.wav"))
                        if remaining:
                            self._show_alert(f"{len(remaining)} more pending")
                    else:
                        time.sleep(cfg.poll_interval)
                        _gc_queue_dirs(cfg, cfg.debug_log)

        finally:
            self._cleanup(original_vol)

    def _show_alert(self, message: str) -> None:
        """Show a transient Hammerspoon alert."""
        hs = shutil.which("hs") or "/opt/homebrew/bin/hs"
        if not Path(hs).exists():
            return
        try:
            subprocess.Popen(
                [hs, "-c", f"hs.alert.show('{message}', 1.5)"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Singleton enforcement (mirrors orchestrator.sh belt-and-suspenders logic)
# ---------------------------------------------------------------------------


def _enforce_singleton(cfg: OrchestratorConfig) -> bool:
    """Return True if we are the sole orchestrator, False if another is running."""
    pid_file = cfg.orch_pid_file
    my_pid = os.getpid()

    # Check PID file
    if pid_file.exists():
        try:
            old_pid = int(pid_file.read_text().strip())
            if old_pid != my_pid:
                # Check if that process is still running
                try:
                    os.kill(old_pid, 0)
                    return False  # Another orchestrator is alive
                except (ProcessLookupError, PermissionError):
                    pass  # Process gone — we can take over
        except (ValueError, OSError):
            pass

    return True


# ---------------------------------------------------------------------------
# __main__ entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """Entry point for `python3 -m heyvox.herald.orchestrator`."""
    # Own process group so we survive kokoro-daemon restarts/crashes
    # (shared PGID lets resource_tracker signals bleed across daemons).
    try:
        os.setpgrp()
    except OSError:
        pass

    import argparse

    parser = argparse.ArgumentParser(
        description="Herald Python Orchestrator — plays queued TTS WAV files"
    )
    parser.add_argument("--queue-dir", default=HERALD_QUEUE_DIR,
                        help="Queue directory for WAV files")
    parser.add_argument("--no-duck", action="store_true",
                        help="Disable audio ducking")
    parser.add_argument("--no-media-pause", action="store_true",
                        help="Disable media pause/resume")
    parser.add_argument("--log-level", default="WARNING",
                        help="Python logging level (DEBUG/INFO/WARNING/ERROR)")
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.WARNING),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )

    # Load app profile config for workspace switching
    ws_switch_cmd = ""
    ws_app_name = ""
    try:
        from heyvox.config import load_config
        heyvox_cfg = load_config()
        for profile in heyvox_cfg.app_profiles:
            if profile.has_workspace_detection and profile.workspace_switch_cmd:
                ws_switch_cmd = profile.workspace_switch_cmd
                ws_app_name = profile.name
                break
    except Exception:
        pass

    cfg = OrchestratorConfig(
        queue_dir=Path(args.queue_dir),
        duck_enabled=not args.no_duck,
        media_pause=not args.no_media_pause,
        workspace_switch_cmd=ws_switch_cmd,
        workspace_app_name=ws_app_name,
    )

    orch = HeraldOrchestrator(config=cfg)
    if not _enforce_singleton(cfg):
        print("Herald orchestrator already running — exiting", file=sys.stderr)
        sys.exit(0)

    orch.run()


if __name__ == "__main__":
    main()
