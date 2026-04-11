"""Herald Python Orchestrator — plays queued WAV files sequentially.

Pure Python replacement for heyvox/herald/lib/orchestrator.sh.

Features:
  - Audio ducking: lowers system volume during playback, then restores
  - Workspace auto-switch: switches Conductor ONLY if it's the frontmost app
  - Hold mode: if user is active, hold messages from other workspaces
  - Media pause/resume (Hush / MediaRemote) during playback
  - Recording watchdog: kills afplay if recording starts mid-playback
  - WAV normalization: RMS-based loudness matching inline in Python
  - Volume via CoreAudio ctypes (cached, no osascript per request)

Requirements: HERALD-01, HERALD-02, HERALD-03, HERALD-04
"""

from __future__ import annotations

import logging
import math
import os
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import wave
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Configuration dataclass
# ---------------------------------------------------------------------------


@dataclass
class OrchestratorConfig:
    """All runtime configuration for the Herald orchestrator."""

    # Queue directories
    queue_dir: Path = field(default_factory=lambda: Path("/tmp/herald-queue"))
    hold_dir: Path = field(default_factory=lambda: Path("/tmp/herald-hold"))
    history_dir: Path = field(default_factory=lambda: Path("/tmp/herald-history"))
    claim_dir: Path = field(default_factory=lambda: Path("/tmp/herald-claim"))

    # Log file
    debug_log: Path = field(default_factory=lambda: Path("/tmp/herald-debug.log"))
    violations_log: Path = field(default_factory=lambda: Path("/tmp/herald-violations.log"))

    # PID / lock files
    orch_pid_file: Path = field(default_factory=lambda: Path("/tmp/herald-orchestrator.pid"))
    playing_pid_file: Path = field(default_factory=lambda: Path("/tmp/herald-playing.pid"))
    original_vol_file: Path = field(default_factory=lambda: Path("/tmp/herald-original-vol"))

    # State files (shared with worker.sh / main process)
    pause_flag: Path = field(default_factory=lambda: Path("/tmp/herald-pause"))
    mute_flag: Path = field(default_factory=lambda: Path("/tmp/herald-mute"))
    recording_flag: Path = field(default_factory=lambda: Path("/tmp/heyvox-recording"))
    play_next_flag: Path = field(default_factory=lambda: Path("/tmp/herald-play-next"))
    last_play_file: Path = field(default_factory=lambda: Path("/tmp/herald-last-play"))
    verbosity_file: Path = field(default_factory=lambda: Path("/tmp/heyvox-verbosity"))

    # Herald home (for conductor-switch-workspace and relative paths)
    herald_home: Path = field(
        default_factory=lambda: Path(__file__).parent
    )

    # Audio ducking
    duck_enabled: bool = True
    duck_level: float = 0.03      # 3% — same as orchestrator.sh HERALD_DUCK_LEVEL=3/100

    # Hold queue
    max_held: int = 5

    # Media pause
    media_pause: bool = True
    resume_delay: float = 1.0

    # WAV normalization
    normalize_target_rms: int = 3000
    normalize_scale_cap: float = 3.0
    normalize_peak_limit: int = 24000

    # Poll interval
    poll_interval: float = 0.3

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


# WAV normalization (legacy fallback — primary normalization is in kokoro-daemon.py)


def normalize_wav(path: Path, target_rms: int = 3000, scale_cap: float = 3.0,
                  peak_limit: int = 24000) -> None:
    """Normalize WAV loudness in-place via RMS matching.

    Legacy fallback for externally-generated WAVs. Primary normalization
    happens in kokoro-daemon.py normalize_samples() at generation time (HERALD-02).
    Skips files with RMS < 50 (silence / already quiet).
    """
    try:
        with wave.open(str(path), "rb") as wf:
            params = wf.getparams()
            raw_frames = wf.readframes(params.nframes)

        n = params.nframes
        samples = list(struct.unpack(f"<{n}h", raw_frames))
        if not samples:
            return

        rms = math.sqrt(sum(s * s for s in samples) / len(samples))
        if rms < 50:
            return

        scale = min(target_rms / rms if rms > 0 else 1.0, scale_cap)
        out: list[int] = []
        for s in samples:
            fs = s * scale
            if fs > peak_limit:
                fs = peak_limit + (fs - peak_limit) * 0.2
            elif fs < -peak_limit:
                fs = -peak_limit + (fs + peak_limit) * 0.2
            out.append(max(-32768, min(32767, int(fs))))

        normalized = struct.pack(f"<{len(out)}h", *out)
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


def _conductor_is_frontmost() -> bool:
    """Return True if Conductor is the frontmost application."""
    try:
        import AppKit  # type: ignore
        app = AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return False
        return app.localizedName() == "Conductor"
    except Exception:
        pass
    # Fallback: osascript
    try:
        r = subprocess.run(
            ["osascript", "-e",
             "tell application \"System Events\" to get name of first application process whose frontmost is true"],
            capture_output=True, text=True, timeout=3.0,
        )
        return r.stdout.strip() == "Conductor"
    except Exception:
        return False


def _switch_workspace(workspace: str, cfg: OrchestratorConfig) -> None:
    """Switch Conductor to the given workspace name."""
    conductor_switch = shutil.which("conductor-switch-workspace") or str(
        Path.home() / ".local/bin/conductor-switch-workspace"
    )
    if not Path(conductor_switch).exists():
        return
    try:
        subprocess.run(
            [conductor_switch, workspace],
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


def _duck_audio(cfg: OrchestratorConfig, debug_log: Path) -> float | None:
    """Lower system volume for TTS ducking. Returns the original volume or None."""
    if not cfg.duck_enabled:
        return None

    from heyvox.herald.coreaudio import get_system_volume_cached, set_system_volume_cached

    # Only save original if not already ducked (avoid saving already-ducked level on restart)
    if cfg.original_vol_file.exists():
        try:
            saved = float(cfg.original_vol_file.read_text().strip())
            set_system_volume_cached(cfg.duck_level)
            time.sleep(0.15)
            return saved
        except (ValueError, OSError):
            pass

    original_vol = get_system_volume_cached(cfg.volume_cache_ttl)
    try:
        cfg.original_vol_file.write_text(str(original_vol))
    except OSError:
        pass
    set_system_volume_cached(cfg.duck_level)
    time.sleep(0.15)
    _herald_log(f"ORCH: ducked audio from {original_vol:.2f} to {cfg.duck_level:.2f}", debug_log)
    return original_vol


def _set_tts_volume(original_vol: float | None, cfg: OrchestratorConfig) -> None:
    """Restore volume to TTS (full) level after ducking."""
    if not cfg.duck_enabled or original_vol is None:
        return
    from heyvox.herald.coreaudio import set_system_volume_cached
    set_system_volume_cached(original_vol)


def _restore_audio(original_vol: float | None, cfg: OrchestratorConfig, debug_log: Path) -> None:
    """Restore volume after all TTS parts are done."""
    if not cfg.duck_enabled:
        return
    # Try in-memory first, then from file
    vol = original_vol
    if vol is None:
        try:
            vol = float(cfg.original_vol_file.read_text().strip())
        except (OSError, ValueError):
            return
    from heyvox.herald.coreaudio import set_system_volume_cached
    set_system_volume_cached(vol)
    cfg.original_vol_file.unlink(missing_ok=True)
    _herald_log(f"ORCH: restored audio to {vol:.2f}", debug_log)


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
# WAV playback
# ---------------------------------------------------------------------------


def _play_wav(
    wav_file: Path,
    last_msg_prefix: str,
    current_workspace: str,
    original_vol: float | None,
    cfg: OrchestratorConfig,
) -> tuple[str, str, float | None]:
    """Play a single WAV file, handling ducking, pausing, and workspace switching.

    Returns:
        (new_last_msg_prefix, new_current_workspace, original_vol)
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
        # Workspace switch — only if Conductor is frontmost
        if workspace_file.exists():
            try:
                ws = workspace_file.read_text().strip()
                current_workspace = ws
                if _conductor_is_frontmost():
                    _switch_workspace(ws, cfg)
                    time.sleep(0.3)
                else:
                    _herald_log("ORCH: skipping workspace switch (Conductor not frontmost)", debug_log)
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

    # If watchdog killed playback, wait for recording to finish
    if play_exit != 0 and _is_paused(cfg, debug_log):
        _herald_log("ORCH: playback interrupted, waiting for pause to clear", debug_log)
        while _is_paused(cfg, debug_log):
            time.sleep(0.3)

    wav_file.unlink(missing_ok=True)

    # Record last play timestamp
    try:
        cfg.last_play_file.write_text(str(int(time.time())))
    except OSError:
        pass

    # Check if queue and hold are empty → resume media + restore volume
    queue_empty = not any(cfg.queue_dir.glob("*.wav"))
    hold_empty = not any(cfg.hold_dir.glob("*.wav"))
    if queue_empty and hold_empty:
        if cfg.media_pause:
            _media_resume(cfg)
            _herald_log("ORCH: media RESUMED", debug_log)
        _restore_audio(original_vol, cfg, debug_log)
        original_vol = None

    return last_msg_prefix, current_workspace, original_vol


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
        cfg.playing_pid_file.unlink(missing_ok=True)
        cfg.play_next_flag.unlink(missing_ok=True)

    def run(self) -> None:
        """Main orchestrator loop — blocks until stop() is called or signal received."""
        cfg = self.cfg
        debug_log = cfg.debug_log

        # Ensure runtime directories exist
        for d in (cfg.queue_dir, cfg.hold_dir, cfg.history_dir, cfg.claim_dir):
            d.mkdir(parents=True, exist_ok=True)

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
                            last_msg_prefix, current_workspace, original_vol = _play_wav(
                                next_held, last_msg_prefix, current_workspace, original_vol, cfg
                            )
                            remaining = list(cfg.hold_dir.glob("*.wav"))
                            if remaining:
                                self._show_alert(f"{len(remaining)} more pending")
                            continue

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

                    last_msg_prefix, current_workspace, original_vol = _play_wav(
                        next_wav, last_msg_prefix, current_workspace, original_vol, cfg
                    )

                else:
                    # Queue empty — check hold queue auto-drain
                    held_wavs = sorted(cfg.hold_dir.glob("*.wav"))
                    if held_wavs and not _user_is_active(cfg):
                        _herald_log(
                            f"ORCH: auto-draining held queue ({len(held_wavs)} pending)",
                            debug_log,
                        )
                        next_held = held_wavs[0]
                        if next_held.exists():
                            last_msg_prefix, current_workspace, original_vol = _play_wav(
                                next_held, last_msg_prefix, current_workspace, original_vol, cfg
                            )
                            remaining = list(cfg.hold_dir.glob("*.wav"))
                            if remaining:
                                self._show_alert(f"{len(remaining)} more pending")
                                time.sleep(1.0)
                    else:
                        time.sleep(cfg.poll_interval)
                        # Periodic cleanup: purge stale claim files (older than 1 hour)
                        try:
                            now = time.time()
                            for claim_file in cfg.claim_dir.iterdir():
                                if claim_file.is_file() and (now - claim_file.stat().st_mtime) > 3600:
                                    claim_file.unlink(missing_ok=True)
                        except (OSError, ValueError):
                            pass

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
    import argparse

    parser = argparse.ArgumentParser(
        description="Herald Python Orchestrator — plays queued TTS WAV files"
    )
    parser.add_argument("--queue-dir", default="/tmp/herald-queue",
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

    cfg = OrchestratorConfig(
        queue_dir=Path(args.queue_dir),
        duck_enabled=not args.no_duck,
        media_pause=not args.no_media_pause,
    )

    orch = HeraldOrchestrator(config=cfg)
    if not _enforce_singleton(cfg):
        print("Herald orchestrator already running — exiting", file=sys.stderr)
        sys.exit(0)

    orch.run()


if __name__ == "__main__":
    main()
