"""Herald Python Orchestrator — plays queued WAV files sequentially.

Features:
  - Audio ducking: lowers system volume during playback, then restores
  - Workspace auto-switch: announces + switches app workspace on a cancelable
    countdown if it's the frontmost app (see _run_switch_countdown)
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
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration dataclass
# ---------------------------------------------------------------------------


from heyvox.constants import (  # noqa: E402 — after __future__ import
    HERALD_QUEUE_DIR, HERALD_HISTORY_DIR, HERALD_CLAIM_DIR,
    HERALD_DEBUG_LOG, HERALD_VIOLATIONS_LOG,
    HERALD_ORCH_PID, HERALD_PLAYING_PID, HERALD_ORIGINAL_VOL_FILE,
    HERALD_PAUSE_FLAG, HERALD_MUTE_FLAG, RECORDING_FLAG,
    HERALD_PENDING_SWITCH_FLAG, HERALD_CANCEL_SWITCH_FLAG,
    HERALD_LAST_PLAY, HERALD_STOP_TS_FILE, VERBOSITY_FILE,
    HERALD_WATCHER_HANDLED_DIR, TTS_PLAYING_FLAG,
)


@dataclass
class OrchestratorConfig:
    """All runtime configuration for the Herald orchestrator."""

    # Queue directories
    queue_dir: Path = field(default_factory=lambda: Path(HERALD_QUEUE_DIR))
    history_dir: Path = field(default_factory=lambda: Path(HERALD_HISTORY_DIR))
    claim_dir: Path = field(default_factory=lambda: Path(HERALD_CLAIM_DIR))

    # Log file
    debug_log: Path = field(default_factory=lambda: Path(HERALD_DEBUG_LOG))
    violations_log: Path = field(default_factory=lambda: Path(HERALD_VIOLATIONS_LOG))

    # PID / lock files
    orch_pid_file: Path = field(default_factory=lambda: Path(HERALD_ORCH_PID))
    playing_pid_file: Path = field(default_factory=lambda: Path(HERALD_PLAYING_PID))
    original_vol_file: Path = field(default_factory=lambda: Path(HERALD_ORIGINAL_VOL_FILE))

    # State files (shared with worker.py / main process)
    pause_flag: Path = field(default_factory=lambda: Path(HERALD_PAUSE_FLAG))
    mute_flag: Path = field(default_factory=lambda: Path(HERALD_MUTE_FLAG))
    recording_flag: Path = field(default_factory=lambda: Path(RECORDING_FLAG))
    pending_switch_file: Path = field(default_factory=lambda: Path(HERALD_PENDING_SWITCH_FLAG))
    cancel_switch_flag: Path = field(default_factory=lambda: Path(HERALD_CANCEL_SWITCH_FLAG))
    last_play_file: Path = field(default_factory=lambda: Path(HERALD_LAST_PLAY))
    stop_ts_file: Path = field(default_factory=lambda: Path(HERALD_STOP_TS_FILE))
    verbosity_file: Path = field(default_factory=lambda: Path(VERBOSITY_FILE))

    # Herald home (for relative paths)
    herald_home: Path = field(
        default_factory=lambda: Path(__file__).parent
    )

    # App profile config for workspace switching (loaded from HeyvoxConfig)
    workspace_provider: str = ""    # heyvox.adapters registry key, e.g. "conductor"
    workspace_app_name: str = ""     # App name to check if frontmost
    workspace_db: str = ""          # Path to the app's workspace DB (passed to the provider as profile.workspace_db)

    # Workspace-switch countdown (replaces the former hold-queue/idle-gate —
    # see WorkspaceSwitchConfig in heyvox/config.py). Countdown window before
    # a pending switch fires; cancel_key is informational here (the actual
    # key capture lives in ptt.py) — used for the alert text.
    switch_countdown_secs: float = 2.5
    switch_cancel_key: str = "right_ctrl"

    # Audio ducking
    duck_enabled: bool = True
    duck_level: float = 0.03      # 3% — HERALD_DUCK_LEVEL inherited from original bash orchestrator
    # Floor for the post-duck TTS volume. _set_tts_volume uses
    # max(original_vol, tts_min_volume) so TTS stays audible if the pre-duck
    # media volume was very low, but otherwise respects the user's slider.
    # Wired from config: tts.min_volume (heyvox/config.py). Default 0.10 ≈
    # respect the slider with a tiny safety floor.
    tts_min_volume: float = 0.10

    # Queue caps
    max_queued: int = 10   # drop oldest messages when queue exceeds this

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


def _generated_before_last_stop(wav_file: Path, cfg: OrchestratorConfig) -> bool:
    """DEF-235: True if wav_file was queued before the last Escape/full-stop.

    A part already sitting in HERALD_QUEUE_DIR when heyvox.herald.cli._cmd_stop
    fires can still be picked up here before that same command's directory
    clear lands — two separate processes, nothing serializes "kill current"
    against "clear the rest". Comparing this file's mtime against the shared
    stop timestamp catches it regardless of how that race resolves, the same
    defense worker.py's _stop_requested_after() already uses on the
    generation side (see HERALD_STOP_TS_FILE / _mark_stop_requested).
    """
    try:
        stop_ts = float(cfg.stop_ts_file.read_text())
    except (OSError, ValueError):
        return False
    try:
        return wav_file.stat().st_mtime < stop_ts
    except OSError:
        return False


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
    detected = ""
    try:
        import AppKit  # type: ignore
        app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is not None:
            detected = (app.localizedName() or "")
            if detected.lower() == app_lower:
                return True
    except Exception as e:
        _herald_log(f"ORCH: NSWorkspace frontmost lookup failed: {e}", cfg.debug_log)
    # Fallback: osascript (System Events returns lowercase process names)
    try:
        r = subprocess.run(
            ["osascript", "-e",
             "tell application \"System Events\" to get name of first application process whose frontmost is true"],
            capture_output=True, text=True, timeout=3.0,
        )
        detected_osa = r.stdout.strip()
        if detected_osa.lower() == app_lower:
            return True
        _herald_log(
            f"ORCH: frontmost check: want={app_lower!r} ns={detected!r} osa={detected_osa!r}",
            cfg.debug_log,
        )
    except Exception as e:
        _herald_log(f"ORCH: osascript frontmost lookup failed: {e}", cfg.debug_log)
    return False


def _switch_workspace(
    workspace: str, cfg: OrchestratorConfig, *,
    workspace_id: str = "", session_id: str = "", cwd: str = "",
) -> None:
    """Switch the workspace-aware app to the given workspace name.

    Delegates to the app's WorkspaceProvider.activate() (heyvox.adapters),
    looked up via cfg.workspace_provider — no more shelling out to an
    external switch script. activate() has its own already-on-target
    short-circuit and read-back verification, so this is a thin wrapper;
    mirrors the equivalent migration in heyvox.input.target's
    _yank_back_app_and_workspace.

    No more force/idle-gate bypass: that existed to override the old switch
    script's own hs.host.idleTime() gate, which activate() doesn't have —
    _run_switch_countdown's visible, cancelable window IS the consent now.

    DEF-237: when workspace_id is known, activates that identity directly
    (stable UUID, survives renames). Falls back to provider.resolve_by_name()
    when workspace_id resolution failed upstream (no DB, locked DB, no
    match) — same fallback intent as before this fix.

    DEF-244: when resolve_by_name() also finds no match, tries
    provider.resolve_by_cwd() as a last resort. This covers the case where
    `workspace` itself is neither the directory codename nor the display-name
    slug (confirmed live: an unrelated string, likely a stale value from a
    long-running process) — cwd is an independent signal, not another guess
    at the same name.
    """
    if not cfg.workspace_provider:
        return
    from heyvox.adapters import get_workspace_provider
    provider = get_workspace_provider(cfg.workspace_provider)
    if provider is None:
        _herald_log(
            f"ORCH: unknown workspace_provider {cfg.workspace_provider!r} — skipping switch",
            cfg.debug_log,
        )
        return

    from heyvox.adapters.base import WorkspaceIdentity
    from types import SimpleNamespace
    profile = SimpleNamespace(workspace_db=cfg.workspace_db)

    if workspace_id:
        identity = WorkspaceIdentity(workspace_id=workspace_id, session_id=session_id or None)
    else:
        try:
            identity = provider.resolve_by_name(workspace, profile)
        except Exception as e:
            _herald_log(f"ORCH: resolve_by_name({workspace!r}) raised {e!r}", cfg.debug_log)
            return
        if identity is None:
            _herald_log(f"ORCH: resolve_by_name({workspace!r}) found no match", cfg.debug_log)
            if not cwd:
                return
            try:
                identity = provider.resolve_by_cwd(cwd, profile)
            except Exception as e:
                _herald_log(f"ORCH: resolve_by_cwd({cwd!r}) raised {e!r}", cfg.debug_log)
                return
            if identity is None:
                _herald_log(f"ORCH: resolve_by_cwd({cwd!r}) found no match", cfg.debug_log)
                return
            _herald_log(
                f"ORCH: resolve_by_cwd({cwd!r}) -> workspace_id={identity.workspace_id!r}",
                cfg.debug_log,
            )

    try:
        ok = provider.activate(identity, profile)
        _herald_log(
            f"ORCH: provider.activate -> {ok} (workspace={workspace!r} "
            f"workspace_id={identity.workspace_id!r} session_id={identity.session_id!r})",
            cfg.debug_log,
        )
    except Exception as e:
        _herald_log(f"ORCH: provider.activate raised {e!r}", cfg.debug_log)


def _hammerspoon_running() -> bool:
    """True iff the Hammerspoon.app process is running.

    DEF-074: When Hammerspoon is not running, `hs -c` can trigger the macOS
    "Hammerspoon is not running — Launch?" dialog, interrupting the user.
    Gate every `hs` invocation with this check.
    """
    try:
        return subprocess.call(
            ["pgrep", "-q", "Hammerspoon"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=1.0,  # DEF-090 twin: bare pgrep can stall under fork-storm and block the TTS path
        ) == 0
    except (OSError, subprocess.SubprocessError):
        # subprocess.TimeoutExpired is a SubprocessError subclass — fail closed (treat as not running)
        return False


def _afplay_ceiling(wav_file) -> float:
    """Hard upper bound (seconds) for a single afplay run: clip duration + slack.

    afplay plays in real time, so a healthy run takes ~the clip's duration. This
    sizes a kill-ceiling to the actual clip (read from the WAV header) plus generous
    slack for afplay/CoreAudio startup latency, with a floor for very short cues and
    an absolute cap as a backstop when the header is unreadable. Guards against a
    stalled afplay (wedged CoreAudio, unreadable mount) — the recording watchdog
    only fires when a recording starts, not when playback itself hangs.
    """
    FLOOR = 15.0      # short clips still tolerate startup + CoreAudio latency
    ABS_CAP = 300.0   # no TTS clip runs this long; absolute backstop
    try:
        import wave
        with wave.open(str(wav_file), "rb") as w:
            rate = w.getframerate() or 1
            duration = w.getnframes() / float(rate)
        return min(max(duration + 10.0, FLOOR), ABS_CAP)
    except Exception:
        return ABS_CAP


def _lua_str_escape(value: str) -> str:
    """Escape a string for safe interpolation inside a single-quoted Lua literal.

    Order matters: escape the backslash first so it cannot consume the quote
    escape that follows, then the single quote, then neutralize raw CR/LF (a
    literal newline terminates a single-quoted Lua string). Without this a
    workspace label or externally-authored Conductor PR title containing a
    crafted ``\\'`` sequence breaks out of the ``hs -c`` Lua string into
    attacker-controlled code execution. Matches the escape order already used
    for AppleScript in injection.py/toast.py.
    """
    return (
        value.replace("\\", "\\\\")
        .replace("'", "\\'")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
    )


def _show_alert(message: str, duration: float = 1.5) -> None:
    """Show a transient Hammerspoon alert.

    Message is run through _lua_str_escape since callers may pass workspace
    display names, which can contain arbitrary characters (colons, commas,
    quotes) that would otherwise break out of the `hs -c` Lua literal
    (DEF-177 Lua-string-breakout guard).
    """
    hs = shutil.which("hs") or "/opt/homebrew/bin/hs"
    if not Path(hs).exists() or not _hammerspoon_running():
        return
    safe_message = _lua_str_escape(message)
    try:
        subprocess.Popen(
            [hs, "-c", f"hs.alert.show('{safe_message}', {duration})"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def _play_switch_pending_cue() -> None:
    """Play a short system sound announcing a pending workspace switch.

    Plays directly via afplay (mirrors device_change_cue in
    heyvox/audio/cues.py) rather than through heyvox.audio.cues.audio_cue():
    that function's wake-word suppression window (_cue_suppress_until) is
    process-local module state and would do nothing useful called from this
    separate orchestrator process. No new suppression wiring is needed here
    either — this plays while TTS_PLAYING_FLAG is already set (the countdown
    starts as the new message begins playing), which heyvox/main.py's
    wake-word loop already treats specially (raised threshold in
    echo_safe/headset mode, mic muted otherwise).
    """
    sound = "/System/Library/Sounds/Glass.aiff"
    if not os.path.exists(sound):
        return
    try:
        subprocess.Popen(
            ["afplay", sound],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except Exception:
        pass


def _run_switch_countdown(
    ws: str, cfg: OrchestratorConfig, debug_log: Path, stop_event: threading.Event,
    *, workspace_id: str = "", session_id: str = "", cwd: str = "",
) -> None:
    """Fire-and-forget: announce a pending switch, give cfg.switch_countdown_secs
    to cancel (Right Ctrl, or Escape/stop — see herald/cli.py::_cmd_stop), then
    switch if uncancelled.

    Runs on its own daemon thread (mirrors the watchdog_thread template —
    poll loop running parallel to playback) so audio is never blocked by it;
    the caller starts this thread and moves straight on to ducking/afplay.

    DEF-237: workspace_id/session_id just ride along to the final
    _switch_workspace call at the end of the countdown — see that
    function's docstring for what they change. DEF-244: cwd rides along the
    same way, as a last-resort resolution signal if ws doesn't resolve.
    """
    countdown_start = time.time()
    cfg.cancel_switch_flag.unlink(missing_ok=True)  # clear stale/poltergeist flag first
    try:
        cfg.pending_switch_file.write_text(str(countdown_start))
    except OSError:
        pass

    _show_alert(
        f"Switching to {ws} in {cfg.switch_countdown_secs:.0f}s — Right Ctrl to cancel",
        duration=cfg.switch_countdown_secs,
    )
    _play_switch_pending_cue()
    try:
        from heyvox.hud.surface import HUDSurface
        HUDSurface.banner(
            "info", "herald-switch-pending",
            f"Switching to {ws} in {cfg.switch_countdown_secs:.0f}s",
        )
    except Exception:
        pass

    cancelled = False
    deadline = countdown_start + cfg.switch_countdown_secs
    while time.time() < deadline:
        if stop_event.is_set():
            return  # superseded by a newer message's countdown
        try:
            flag_ts = float(cfg.cancel_switch_flag.read_text().strip())
            if flag_ts >= countdown_start:
                cancelled = True
                break
        except (OSError, ValueError):
            pass
        time.sleep(cfg.poll_interval)

    cfg.cancel_switch_flag.unlink(missing_ok=True)
    cfg.pending_switch_file.unlink(missing_ok=True)
    try:
        from heyvox.hud.surface import HUDSurface
        HUDSurface.clear("herald-switch-pending")
    except Exception:
        pass

    if cancelled:
        _herald_log(f"ORCH: switch to {ws!r} cancelled before deadline", debug_log)
        return
    if stop_event.is_set():
        return
    if cfg.recording_flag.exists() or not _workspace_app_is_frontmost(cfg):
        _herald_log(
            f"ORCH: switch to {ws!r} skipped at expiry (recording or app not frontmost)",
            debug_log,
        )
        return
    _switch_workspace(ws, cfg, workspace_id=workspace_id, session_id=session_id, cwd=cwd)


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
    # DEF-072: If we read a near-zero volume the device is either actually
    # muted (in which case ducking is pointless) or lying about having software
    # volume control (in which case saving 0.0 would zero the output on
    # restore). Either way, skip the sidecar + skip ducking entirely.
    if original_vol is None or original_vol < 0.05:
        _herald_log(
            f"ORCH: skipping duck — original_vol={original_vol} (dev={dev_id}) "
            f"looks bogus or muted; not writing sidecar",
            debug_log,
        )
        return None
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

    # DEF-113 / P-hotplug-cache: macOS reassigns CoreAudio device IDs on every
    # hotplug, so the pinned dev_id may point at a ghost device. Two-layer
    # defence:
    #   1. CoreAudioHandle.revalidate() — cheap pre-write probe via
    #      AudioObjectHasProperty. Catches the common case where the device
    #      went away while the sidecar lived (seconds).
    #   2. _set_volume_coreaudio() return value — backstop for the rare race
    #      where the device survives the probe but dies before the Set call.
    # Either failure path emits the same warn banner and falls back to
    # set_system_volume_cached() so the user's media isn't stranded at
    # duck-level.
    from heyvox.audio.device_handle import CoreAudioHandle
    ok = True
    fallback = False
    pre_check = "n/a"
    if dev_id is not None:
        handle = CoreAudioHandle(dev_id=dev_id)
        if handle.revalidate():
            pre_check = "alive"
            ok = _set_volume_coreaudio(handle.id, vol)
        else:
            pre_check = "ghost"
            ok = False
        if not ok:
            set_system_volume_cached(vol)
            fallback = True
            try:
                from heyvox.hud.surface import HUDSurface
                HUDSurface.banner(
                    level="warn",
                    source="herald-ghost-dev",
                    text="Audio restored via system volume (device gone)",
                    ttl_secs=30,
                )
            except Exception:
                pass
    else:
        set_system_volume_cached(vol)
    cfg.original_vol_file.unlink(missing_ok=True)
    _herald_log(
        f"ORCH: restored audio to {vol:.2f} (dev={dev_id}) "
        f"pre_check={pre_check} ok={ok} fallback={fallback}",
        debug_log,
    )


# ---------------------------------------------------------------------------
# Volume-zero guard
# ---------------------------------------------------------------------------

# Reuse the same threshold as _duck_audio's guard — below this the duck is
# skipped and TTS plays at essentially no volume.
_VOL_ZERO_THRESHOLD: float = 0.05
# Banner TTL slightly longer than the 30s periodic check so it auto-expires
# between checks once volume is fixed without needing a clear() call.
_VOL_ZERO_BANNER_TTL: float = 35.0


def _warn_if_vol_zero(cfg: OrchestratorConfig, debug_log: Path) -> bool:
    """Emit a menu-bar / HUD-overlay warning when system volume is near zero.

    Called before each TTS message and on a 30-second periodic poll from the
    orchestrator idle loop. Clears the banner automatically when volume is
    restored above the threshold.

    Returns True if volume is effectively zero (TTS will be inaudible).
    """
    try:
        from heyvox.herald.coreaudio import get_system_volume_cached
        from heyvox.hud.surface import HUDSurface
        vol = get_system_volume_cached(ttl=1.0)
        if vol <= _VOL_ZERO_THRESHOLD:
            HUDSurface.banner(
                level="warn",
                source="vol-zero",
                text=f"Volume {int(round(vol * 100))}% — TTS muted",
                ttl_secs=_VOL_ZERO_BANNER_TTL,
            )
            _herald_log(
                f"ORCH: [VOL-ZERO] vol={vol:.2f} ≤ {_VOL_ZERO_THRESHOLD} — TTS inaudible",
                debug_log,
            )
            return True
        HUDSurface.clear("vol-zero")
        return False
    except Exception:
        return False


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
    *,
    switch_stop_event: threading.Event | None = None,
) -> tuple[str, str, float | None, bool, threading.Event | None]:
    """Play a single WAV file, handling ducking, pausing, and workspace switching.

    Args:
        switch_stop_event: threading.Event for the most recently started
            _run_switch_countdown thread, or None. When a new non-continuation
            message arrives, any still-running countdown from a prior message
            is superseded (its stop_event is set) before starting a fresh one
            — carried across calls the same way current_workspace is.

    Returns:
        (new_last_msg_prefix, new_current_workspace, original_vol, was_interrupted,
         new_switch_stop_event)
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
                from heyvox.herald.workspace_label import read_switch_sidecar
                identity = read_switch_sidecar(workspace_file.read_text())
                ws = identity["workspace"]
                current_workspace = ws
                # DEF-070: Skip switch while HeyVox is recording/injecting. The
                # forced Hammerspoon sidebar click steals focus mid-paste, so
                # `keystroke return` lands on a sidebar item instead of the chat
                # text field and the message never submits.
                if cfg.recording_flag.exists():
                    _herald_log(
                        f"ORCH: skipping workspace switch to {ws!r} "
                        f"(HeyVox recording/injecting)",
                        debug_log,
                    )
                elif _workspace_app_is_frontmost(cfg):
                    if switch_stop_event is not None:
                        switch_stop_event.set()  # supersede any in-flight countdown
                    switch_stop_event = threading.Event()
                    threading.Thread(
                        target=_run_switch_countdown,
                        args=(ws, cfg, debug_log, switch_stop_event),
                        kwargs={
                            "workspace_id": identity["workspace_id"],
                            "session_id": identity["session_id"],
                            "cwd": identity["cwd"],
                        },
                        daemon=True,
                    ).start()
                else:
                    _herald_log("ORCH: skipping workspace switch (app not frontmost)", debug_log)
                workspace_file.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass

        if cfg.media_pause:
            _media_pause(cfg)
            _herald_log("ORCH: media PAUSED", debug_log)

        _warn_if_vol_zero(cfg, debug_log)
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
                _herald_log("ORCH: WATCHDOG killed afplay (recording started during playback)", debug_log)
                break
            time.sleep(0.1)

    watchdog_thread = threading.Thread(target=_watchdog, daemon=True)
    watchdog_thread.start()

    # Hard ceiling so a stalled afplay can't block the orchestrator loop forever.
    # The recording watchdog above only kills on recording-start; this catches a
    # wedged playback (DEF-140 follow-up: the orchestrator's afplay proc.wait()).
    play_ceiling = _afplay_ceiling(wav_file)
    try:
        play_exit = proc.wait(timeout=play_ceiling)
    except subprocess.TimeoutExpired:
        try:
            proc.kill()
            play_exit = proc.wait(timeout=2.0)
        except (subprocess.TimeoutExpired, ProcessLookupError, OSError):
            play_exit = -1
        _violation_check(f"orchestrator:afplay-stall-kill:{basename}", cfg)
        _herald_log(
            f"ORCH: killed stalled afplay after {play_ceiling:.0f}s ceiling for {basename}",
            debug_log,
        )
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

    # Check if queue is empty → resume media + restore volume
    # Also check for .parts manifests — a worker may still be generating parts.
    queue_empty = not any(cfg.queue_dir.glob("*.wav"))
    parts_coming = _parts_pending(cfg.queue_dir)
    if queue_empty and not parts_coming:
        if cfg.media_pause:
            _media_resume(cfg)
            _herald_log("ORCH: media RESUMED", debug_log)
        _restore_audio(original_vol, cfg, debug_log)
        original_vol = None

    return last_msg_prefix, current_workspace, original_vol, was_interrupted, switch_stop_event


# ---------------------------------------------------------------------------
# Main orchestrator class
# ---------------------------------------------------------------------------


class HeraldOrchestrator:
    """Pure-Python Herald orchestrator.

    Runs as a singleton daemon process. Polls the herald-queue directory,
    plays WAV files via afplay, handles audio ducking, workspace switching
    (via a cancelable countdown — see _run_switch_countdown), and recording
    watchdog.

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
        for d in (cfg.queue_dir, cfg.history_dir, cfg.claim_dir):
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

        # DEF-072: If a previous orchestrator crashed mid-duck, the sidecar may
        # contain a stale (possibly bogus 0.0) original volume. Clear it so we
        # don't "restore" to a dead value on the first TTS event.
        stale_sidecar = cfg.original_vol_file.exists()
        cfg.original_vol_file.unlink(missing_ok=True)

        _herald_log(
            f"ORCH: started (pid={os.getpid()}) stale_sidecar={stale_sidecar}",
            debug_log,
        )

        original_vol: float | None = None
        current_workspace: str = ""
        last_msg_prefix: str = ""
        switch_stop_event: threading.Event | None = None
        _last_vol_check: float = 0.0

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

                    # DEF-235: drop parts queued before the last Escape/stop —
                    # closes the race where this loop picks up an
                    # already-generated part before _cmd_stop()'s own
                    # directory clear lands.
                    if _generated_before_last_stop(next_wav, cfg):
                        next_wav.unlink(missing_ok=True)
                        next_wav.with_suffix(".workspace").unlink(missing_ok=True)
                        _herald_log(
                            f"ORCH: dropping stale part {next_wav.name} (queued before last stop)",
                            debug_log,
                        )
                        continue

                    (last_msg_prefix, current_workspace, original_vol,
                     interrupted, switch_stop_event) = _play_wav(
                        next_wav, last_msg_prefix, current_workspace, original_vol, cfg,
                        switch_stop_event=switch_stop_event,
                    )

                    # If recording interrupted playback, drop remaining parts of this message
                    if interrupted and last_msg_prefix:
                        _purge_message_parts(last_msg_prefix, cfg.queue_dir, debug_log)
                        last_msg_prefix = ""  # reset so next message isn't treated as continuation

                else:
                    time.sleep(cfg.poll_interval)
                    _gc_queue_dirs(cfg, cfg.debug_log)
                    # Periodic volume-zero check — refresh banner every 30s
                    _now_mono = time.monotonic()
                    if _now_mono - _last_vol_check >= 30.0:
                        _last_vol_check = _now_mono
                        _warn_if_vol_zero(cfg, cfg.debug_log)

        finally:
            self._cleanup(original_vol)


# ---------------------------------------------------------------------------
# Singleton enforcement (belt-and-suspenders against duplicate orchestrators)
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
    ws_provider = ""
    ws_app_name = ""
    ws_db = ""
    tts_min_volume: float | None = None
    switch_countdown_secs: float | None = None
    switch_cancel_key: str | None = None
    try:
        from heyvox.config import load_config
        heyvox_cfg = load_config()
        for profile in heyvox_cfg.app_profiles:
            if profile.has_workspace_detection and profile.workspace_provider:
                ws_provider = profile.workspace_provider
                ws_app_name = profile.name
                ws_db = profile.workspace_db
                break
        tts_min_volume = float(heyvox_cfg.tts.min_volume)
        switch_countdown_secs = float(heyvox_cfg.workspace_switch.countdown_secs)
        switch_cancel_key = heyvox_cfg.workspace_switch.cancel_key
    except Exception:
        pass

    cfg_kwargs = dict(
        queue_dir=Path(args.queue_dir),
        duck_enabled=not args.no_duck,
        media_pause=not args.no_media_pause,
        workspace_provider=ws_provider,
        workspace_app_name=ws_app_name,
        workspace_db=ws_db,
    )
    if tts_min_volume is not None:
        cfg_kwargs["tts_min_volume"] = tts_min_volume
    if switch_countdown_secs is not None:
        cfg_kwargs["switch_countdown_secs"] = switch_countdown_secs
    if switch_cancel_key is not None:
        cfg_kwargs["switch_cancel_key"] = switch_cancel_key
    cfg = OrchestratorConfig(**cfg_kwargs)

    orch = HeraldOrchestrator(config=cfg)
    if not _enforce_singleton(cfg):
        print("Herald orchestrator already running — exiting", file=sys.stderr)
        sys.exit(0)

    orch.run()


if __name__ == "__main__":
    main()
