"""
Vox main event loop.

Orchestrates the wake word listener, push-to-talk, STT transcription,
text injection, and recording indicator. Loads configuration from
~/.config/heyvox/config.yaml via the pydantic config system.

Entry point: heyvox.cli calls run() which calls main().

Requirement: CONF-01, DECP-01 through DECP-06
"""

import os
import sys
import time
import signal
import threading
import subprocess

import numpy as np
import pyaudio

from heyvox.config import load_config, HeyvoxConfig
from heyvox.app_context import AppContext
from heyvox.device_manager import DeviceManager
from heyvox.constants import (
    RECORDING_FLAG,
    TTS_PLAYING_FLAG,
    TTS_PLAYING_MAX_AGE_SECS,
    HUD_SOCKET_PATH,
    STT_DEBUG_DIR,
)
from heyvox.audio.cues import audio_cue, is_suppressed, get_cues_dir
from heyvox.audio.stt import init_local_stt, transcribe_audio
from heyvox.audio.tts import check_voice_command, execute_voice_command
from heyvox.input.injection import type_text, save_frontmost_pid, restore_frontmost
from heyvox.input.target import snapshot_target, restore_target

# Backward-compat re-exports (tests import these; remove in Phase 9)
from heyvox.text_processing import (
    is_garbled as _is_garbled,
    strip_wake_words as _strip_wake_words,
    _WAKE_WORD_PHRASES,
)


# ---------------------------------------------------------------------------
# State (protected by _state_lock)
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()
is_recording = False
recording_start_time = 0.0
busy = False
_audio_buffer = []
_triggered_by_ptt = False
_recording_target = None  # TargetSnapshot: app + text field at recording start
_cancel_transcription = threading.Event()
_shutdown = threading.Event()
_cancel_requested = threading.Event()  # Set by SIGUSR1 signal, checked in main loop
_adapter = None  # Initialized in main() via _build_adapter(config)
_last_inject_time = 0.0
_inject_lock = threading.Lock()
_INJECT_DEDUP_SECS = 2.0  # Suppress duplicate injections within this window

# ---------------------------------------------------------------------------
# Zombie stream detection (AUDIO-12)
# Tracks consecutive failed recordings to detect corrupted audio streams
# that pass the level-based health check but produce unusable audio.
# ---------------------------------------------------------------------------
_consecutive_failed_recordings = 0
_zombie_mic_reinit = False  # Set True to force mic reinit on next main loop iteration
_ZOMBIE_FAIL_THRESHOLD = 2  # Force reinit after N consecutive failed recordings
_busy_since: float = 0.0  # Timestamp when busy flag was set (watchdog)
_BUSY_TIMEOUT = 60.0  # Force-reset busy after this many seconds

# ---------------------------------------------------------------------------
# Time-based dead mic detection (AUDIO-13)
# Catches dead mic even during rapid PTT usage where idle health checks
# never run.  Updated on every main-loop iteration that sees real audio.
# ---------------------------------------------------------------------------
_last_good_audio_time: float = 0.0  # Set on startup & whenever level >= 10
_DEAD_MIC_TIMEOUT = 30.0  # Force reinit after this many seconds of silence

# ---------------------------------------------------------------------------
# HUD client state (Phase 5 — optional, never crashes main loop)
# ---------------------------------------------------------------------------

_hud_client = None
_hud_last_reconnect = 0.0
_HUD_RECONNECT_INTERVAL = 1.0  # Retry every 1s (fast reconnect after overlay startup)
_HUD_LEVEL_INTERVAL = 0.05  # 20fps throttle for audio_level messages
_hud_last_level_send = 0.0


def _hud_send(msg: dict) -> None:
    """Send a message to the HUD overlay. No-op if not connected.

    All HUD sends are wrapped here so the rest of the code never has to
    guard against HUD failures — the HUD is strictly optional.
    Auto-reconnects if socket is disconnected (handles overlay startup delay).
    """
    global _hud_client, _hud_last_reconnect
    if _hud_client is None:
        log(f"[HUD-DBG] _hud_client is None, skipping {msg.get('type')}")
        return
    # Auto-reconnect if socket dropped (e.g. overlay just started)
    if _hud_client._sock is None:
        now = time.time()
        if now - _hud_last_reconnect < _HUD_RECONNECT_INTERVAL:
            return
        _hud_last_reconnect = now
        log(f"[HUD-DBG] Attempting reconnect for {msg.get('type')}...")
        try:
            _hud_client.reconnect()
        except Exception as e:
            log(f"[HUD-DBG] Reconnect failed: {e}")
            return
        if _hud_client._sock is None:
            log("[HUD-DBG] Reconnect succeeded but sock still None")
            return
        log("[HUD-DBG] Reconnected!")
    try:
        _hud_client.send(msg)
        log(f"[HUD-DBG] Sent {msg.get('type')}: {msg.get('state', '')}")
    except Exception as e:
        log(f"[HUD-DBG] Send failed: {e}")


def _hud_ensure_connected() -> None:
    """Attempt periodic reconnect if the HUD connection was lost.

    Called in the idle section of the main loop to recover after
    HUD restart without requiring a full heyvox restart.
    """
    global _hud_client, _hud_last_reconnect
    if _hud_client is None:
        return
    # Already connected — nothing to do
    if _hud_client._sock is not None:
        return
    now = time.time()
    if now - _hud_last_reconnect >= _HUD_RECONNECT_INTERVAL:
        _hud_last_reconnect = now
        try:
            _hud_client.reconnect()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Wake word stripping from transcriptions
# Moved to heyvox/text_processing.py — re-exported above for backward compat
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Logging (module-level path set from config at startup)
# ---------------------------------------------------------------------------

_LOG_FILE = "/tmp/heyvox.log"
_LOG_MAX_BYTES = 1_000_000


def _init_log(log_file: str, log_max_bytes: int) -> None:
    """Set the log file path and rotation limit from config."""
    global _LOG_FILE, _LOG_MAX_BYTES
    _LOG_FILE = log_file
    _LOG_MAX_BYTES = log_max_bytes


def log(msg: str) -> None:
    """Write timestamped message to log file with rotation.

    Only writes to the file directly — avoids double-logging when
    stdout is redirected to the same log file.
    """
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    try:
        if os.path.exists(_LOG_FILE) and os.path.getsize(_LOG_FILE) > _LOG_MAX_BYTES:
            rotated = _LOG_FILE + ".1"
            try:
                os.replace(_LOG_FILE, rotated)
            except OSError:
                pass
        with open(_LOG_FILE, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Recording indicator (separate process)
# ---------------------------------------------------------------------------

_indicator_proc = None
_hud_log_fh = None  # stderr log file handle for HUD subprocess


def _kill_overlay_pids(pids: list[int]) -> None:
    """Kill overlay processes by PID. Uses SIGKILL — SIGTERM is unreliable on orphaned AppKit processes."""
    for pid in pids:
        try:
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
    # Wait for macOS to reclaim window server resources (ghost icon cleanup)
    if pids:
        time.sleep(0.5)


def _kill_orphan_indicators() -> None:
    """Kill any leftover overlay processes from previous sessions."""
    my_pid = os.getpid()
    try:
        result = subprocess.run(
            ["pgrep", "-f", "heyvox.hud.overlay"],
            capture_output=True, text=True, timeout=3,
        )
        pids = []
        for pid_str in result.stdout.strip().split('\n'):
            if pid_str.strip():
                pid = int(pid_str.strip())
                if pid != my_pid:
                    pids.append(pid)
        _kill_overlay_pids(pids)
    except Exception:
        pass


def _kill_duplicate_overlays(keep_pid: int | None = None) -> None:
    """Ensure only one overlay process is running. Kill extras."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "heyvox.hud.overlay"],
            capture_output=True, text=True, timeout=3,
        )
        pids = [int(p.strip()) for p in result.stdout.strip().split('\n') if p.strip()]
        if len(pids) <= 1:
            return
        to_kill = [pid for pid in pids if pid != keep_pid]
        _kill_overlay_pids(to_kill)
        for pid in to_kill:
            log(f"Killed duplicate overlay (pid={pid})")
    except Exception:
        pass


def _launch_hud_overlay(menu_bar_only: bool = False) -> None:
    """Launch the HUD overlay process once. It stays alive for the entire session.

    Kills any orphan/duplicate overlays first to guarantee exactly one instance.
    """
    global _indicator_proc, _hud_log_fh
    if _indicator_proc is not None and _indicator_proc.poll() is None:
        # Already running — just ensure no duplicates
        _kill_duplicate_overlays(keep_pid=_indicator_proc.pid)
        return
    # Kill anything leftover before launching
    _kill_orphan_indicators()
    try:
        cmd = [sys.executable, "-m", "heyvox.hud.overlay"]
        if menu_bar_only:
            cmd.append("--menu-bar-only")
        # Close previous log handle if restarting overlay
        if _hud_log_fh is not None:
            try:
                _hud_log_fh.close()
            except OSError:
                pass
        _hud_log_fh = open("/tmp/heyvox-hud-stderr.log", "a")
        _indicator_proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=_hud_log_fh,
        )
        log(f"HUD overlay launched (pid={_indicator_proc.pid})")
    except Exception as e:
        log(f"WARNING: Could not launch HUD overlay: {e}")


def _stop_hud_overlay() -> None:
    """Terminate the HUD overlay process on shutdown."""
    global _indicator_proc, _hud_log_fh
    if _indicator_proc:
        try:
            _indicator_proc.terminate()
            _indicator_proc.wait(timeout=3)
        except Exception:
            try:
                _indicator_proc.kill()
            except Exception:
                pass
        _indicator_proc = None
    if _hud_log_fh is not None:
        try:
            _hud_log_fh.close()
        except OSError:
            pass
        _hud_log_fh = None


def _show_recording_indicator(active: bool) -> None:
    """Legacy recording indicator — now a no-op.

    The HUD overlay stays alive for the entire session. State is driven
    by _hud_send() messages instead of launching/killing processes.
    Kept for call-site compatibility.
    """
    pass


# ---------------------------------------------------------------------------
# Recording flow
# ---------------------------------------------------------------------------

def start_recording(ptt: bool = False, config: HeyvoxConfig = None, preroll=None) -> None:
    """Begin a recording session.

    Sets is_recording flag, signals TTS to pause, plays listening cue,
    and shows the recording indicator.

    Args:
        ptt: True if triggered by push-to-talk (affects auto-send behavior).
        config: HeyvoxConfig instance. Required.
        preroll: Iterable of audio chunks captured before the wake word trigger.
            Prepended to the audio buffer so the first words aren't clipped.
    """
    global is_recording, recording_start_time, _audio_buffer, _triggered_by_ptt, _recording_target
    if config is None:
        return
    if _shutdown.is_set():
        return  # Don't start recording during shutdown

    # AUDIO-13: Don't start recording on a known-dead mic stream.
    # The main loop will pick up the flag and reinit before we get here again.
    if _zombie_mic_reinit:
        log("start_recording blocked: zombie mic reinit pending, skipping")
        _hud_send({"type": "error", "text": "Mic reinitializing…"})
        return

    with _state_lock:
        if is_recording:
            return
        is_recording = True
        recording_start_time = time.time()
        # Pre-roll: prepend recent audio so first words aren't clipped
        _audio_buffer = list(preroll) if preroll else []
        _triggered_by_ptt = ptt
        # Snapshot which app/text field is focused right now, so we can
        # restore it at injection time even if the user clicks away.
        _recording_target = snapshot_target()
        if _recording_target:
            ws_info = f", conductor_workspace={_recording_target.conductor_workspace!r}" if _recording_target.conductor_workspace else ""
            log(f"[snapshot] app={_recording_target.app_name}, pid={_recording_target.app_pid}, "
                f"window={_recording_target.window_title!r}, element={_recording_target.element_role}{ws_info}")
        else:
            log("[snapshot] WARNING: no target snapshot (AppKit unavailable?)")

    # Preload STT model in background while user speaks — hides the ~1s
    # model load latency behind recording time. No-op if already loaded.
    if config.stt.backend == "local":
        from heyvox.audio.stt import preload_model
        preload_model()

    # Signal Herald to pause TTS during recording (TTS-03, DECP-04)
    # Herald stops current playback and holds new items until resume.
    try:
        from heyvox.audio.tts import set_recording as _tts_set_rec
        _tts_set_rec(True)
    except ImportError:
        pass

    # Pause browser/native media during recording (YouTube, Spotify, etc.)
    # Run in background thread — pause_media() can block for seconds on
    # osascript calls (Chrome JS access test), which would delay recording start.
    def _bg_pause():
        try:
            from heyvox.audio.media import pause_media
            pause_media()
        except Exception as e:
            log(f"WARNING: media pause failed: {e}")
    threading.Thread(target=_bg_pause, daemon=True, name="vox-media-pause").start()

    # Write recording flag for cross-process coordination
    try:
        with open(RECORDING_FLAG, "w"):
            pass
    except Exception:
        pass

    cues_dir = get_cues_dir(config.cues_dir)
    audio_cue("listening", cues_dir)
    _show_recording_indicator(True)
    _hud_send({"type": "state", "state": "listening"})
    log("Recording started. Waiting for stop wake word.")
    print(f"[recording] Started, target={_recording_target.app_name if _recording_target else 'None'}", file=sys.stderr)


def stop_recording(config: HeyvoxConfig = None) -> None:
    """End a recording session and dispatch transcription.

    Checks minimum recording duration, plays feedback cue, and starts
    the transcription thread.

    Args:
        config: HeyvoxConfig instance. Required.
    """
    global is_recording, busy
    if config is None:
        return

    with _state_lock:
        if not is_recording:
            return
        is_recording = False
        # Set busy immediately to prevent re-entry from other triggers
        busy = True
        duration = time.time() - recording_start_time
        recorded_chunks = list(_audio_buffer)
        # Capture PTT flag and recording target under lock — _send_local runs on
        # a daemon thread and must not read globals (could be overwritten by a new recording).
        ptt_snapshot = _triggered_by_ptt
        target_snapshot = _recording_target

    log("Stopping recording...")
    _show_recording_indicator(False)
    _hud_send({"type": "state", "state": "processing"})

    # Zombie stream detection: track consecutive failed recordings (AUDIO-12)
    global _consecutive_failed_recordings, _zombie_mic_reinit
    if len(recorded_chunks) == 0:
        _consecutive_failed_recordings += 1
        log(f"WARNING: Recording produced 0 chunks (consecutive failures: {_consecutive_failed_recordings})")
        if _consecutive_failed_recordings >= _ZOMBIE_FAIL_THRESHOLD:
            log(f"WARNING: {_ZOMBIE_FAIL_THRESHOLD} consecutive empty recordings — flagging zombie stream for reinit")
            _zombie_mic_reinit = True
            _consecutive_failed_recordings = 0
    else:
        _consecutive_failed_recordings = 0

    # NOTE: Recording flag (RECORDING_FLAG + _recording_active event) stays set
    # through the STT→paste pipeline. It is released in _send_local's finally block
    # (or in the early-exit paths below). This prevents Conductor's TTS hook from
    # firing and stealing focus while we're still transcribing/pasting.

    cues_dir = get_cues_dir(config.cues_dir)

    if duration < config.min_recording_secs:
        log(f"Recording too short ({duration:.1f}s < {config.min_recording_secs}s), cancelling")
        _release_recording_guard()
        with _state_lock:
            busy = False
        try:
            from heyvox.audio.media import resume_media
            resume_media()
        except Exception:
            pass
        audio_cue("paused", cues_dir)
        _hud_send({"type": "state", "state": "idle"})
        return

    if not ptt_snapshot:
        audio_cue("ok", cues_dir)

    try:
        if config.stt.backend == "local":
            # Compute energy on raw audio BEFORE trimming (wake word is loud,
            # removing it would make the remaining audio seem quieter)
            raw_rms_db = _audio_rms(recorded_chunks, config.audio.sample_rate)

            # Save raw audio BEFORE any trimming (for debug analysis)
            _save_debug_audio("raw", recorded_chunks, config.audio.sample_rate, {
                "ptt": ptt_snapshot,
                "raw_rms_dbfs": round(raw_rms_db, 1),
            })

            if not ptt_snapshot:
                # Wake word audio trim — remove wake word from both ends so
                # Whisper never sees it. This is the primary defense; the text-level
                # _strip_wake_words() is a fallback for imperfect trims.
                #
                # Start trim: ~1.5s covers pre-roll buffer (500ms) + wake word (~1000ms).
                # End trim: 0.5s — conservative, only cuts actual stop wake word.
                # Without this, pre-roll audio (TTS, system sounds) gets transcribed.
                ww_start_trim_secs = 1.5
                ww_end_trim_secs = 0.5
                start_trim_chunks = int(ww_start_trim_secs * config.audio.sample_rate / config.audio.chunk_size)
                end_trim_chunks = int(ww_end_trim_secs * config.audio.sample_rate / config.audio.chunk_size)

                pre_trim_count = len(recorded_chunks)

                # Trim start wake word + cue bleed from front
                if len(recorded_chunks) > start_trim_chunks + end_trim_chunks:
                    recorded_chunks = recorded_chunks[start_trim_chunks:]
                # Trim stop wake word from end (only if recording is long enough)
                if end_trim_chunks > 0 and len(recorded_chunks) > end_trim_chunks:
                    recorded_chunks = recorded_chunks[:-end_trim_chunks]

                log(f"Audio trim: {pre_trim_count} chunks → {len(recorded_chunks)} "
                    f"(start={start_trim_chunks}, end={end_trim_chunks})")

                # Save trimmed audio for comparison
                _save_debug_audio("trimmed", recorded_chunks, config.audio.sample_rate)

            # _send_local has its own finally block that resets busy = False
            threading.Thread(
                target=_send_local,
                args=(duration, recorded_chunks, config, _adapter, raw_rms_db),
                kwargs={"ptt": ptt_snapshot, "recording_target": target_snapshot},
                daemon=True,
            ).start()
    except Exception as e:
        log(f"ERROR starting transcription: {e}")
        _release_recording_guard()
        with _state_lock:
            busy = False
        _hud_send({"type": "state", "state": "idle"})


def _build_adapter(config: HeyvoxConfig):
    """Resolve the correct adapter from config.target_mode.

    Requirement: INPT-03 (adapter selection via config)
    """
    mode = config.target_mode
    if mode == "pinned-app" and config.target_app:
        from heyvox.adapters.generic import GenericAdapter
        return GenericAdapter(target_app=config.target_app, enter_count=config.enter_count)
    elif mode == "last-agent":
        from heyvox.adapters.last_agent import LastAgentAdapter
        return LastAgentAdapter(agents=config.agents, enter_count=config.enter_count)
    else:  # "always-focused" default
        from heyvox.adapters.generic import GenericAdapter
        return GenericAdapter(enter_count=config.enter_count)


def _release_recording_guard() -> None:
    """Release the recording guard — both in-process event and cross-process file flag.

    Called after STT→paste completes (or on early exit) so Conductor's TTS hook
    knows it's safe to speak again.
    """
    try:
        from heyvox.audio.tts import set_recording as _tts_set_rec
        _tts_set_rec(False)
    except ImportError:
        pass
    try:
        os.remove(RECORDING_FLAG)
    except FileNotFoundError:
        pass


def _save_debug_audio(
    label: str,
    chunks: list,
    sample_rate: int,
    extra_info: dict | None = None,
) -> str | None:
    """Save raw audio chunks to a WAV file in the debug directory.

    Returns the file path, or None if debug dir doesn't exist / saving fails.
    Only saves when STT_DEBUG_DIR exists (create it to enable: mkdir /tmp/heyvox-debug).
    """
    if not os.path.isdir(STT_DEBUG_DIR):
        return None
    try:
        import wave
        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{ts}_{label}.wav"
        filepath = os.path.join(STT_DEBUG_DIR, filename)

        audio = np.concatenate(chunks) if chunks else np.array([], dtype=np.int16)
        with wave.open(filepath, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # int16
            wf.setframerate(sample_rate)
            wf.writeframes(audio.tobytes())

        duration = len(audio) / sample_rate if sample_rate > 0 else 0
        rms = _audio_rms(chunks, sample_rate) if chunks else -96.0

        # Write structured log entry
        from heyvox.constants import STT_DEBUG_LOG
        info = {
            "timestamp": ts,
            "label": label,
            "file": filename,
            "duration_s": round(duration, 2),
            "rms_dbfs": round(rms, 1),
            "num_chunks": len(chunks),
        }
        if extra_info:
            info.update(extra_info)
        import json
        with open(STT_DEBUG_LOG, "a") as f:
            f.write(json.dumps(info) + "\n")

        return filepath
    except Exception as e:
        log(f"DEBUG: Failed to save audio: {e}")
        return None


def _audio_rms(chunks: list, sample_rate: int) -> float:
    """Compute RMS energy of recorded audio chunks in dBFS.

    Returns a negative value (0 dBFS = max, -96 dBFS ~ silence for 16-bit).
    Used to skip STT on silent/near-silent recordings that would cause
    Whisper to hallucinate ("Thank you for watching", etc.).
    """
    import numpy as np
    if not chunks:
        return -96.0
    audio = np.concatenate(chunks).astype(np.float32)
    if len(audio) == 0:
        return -96.0
    rms = np.sqrt(np.mean(audio ** 2))
    if rms < 1e-10:
        return -96.0
    # Convert to dBFS (assuming 16-bit int range mapped to float)
    return 20 * np.log10(rms / 32768.0)


# Minimum audio energy (dBFS) to proceed with STT. Recordings below this
# threshold are treated as silence — skips Whisper to avoid hallucinations.
# -60 dBFS catches only true silence/near-silence. Normal quiet speech is ~-45 to -35 dBFS.
_MIN_AUDIO_DBFS = -60.0


def _send_local(duration: float, audio_chunks: list, config: HeyvoxConfig, adapter, raw_rms_db: float = 0.0, *, ptt: bool = False, recording_target=None) -> None:
    """Transcribe locally and inject text into target app."""
    global busy

    try:
        # Energy gate: skip STT on silent recordings to avoid Whisper hallucinations.
        # Uses raw_rms_db computed BEFORE wake word trim (wake word is the loudest part).
        if raw_rms_db < _MIN_AUDIO_DBFS:
            log(f"Recording too quiet ({raw_rms_db:.1f} dBFS < {_MIN_AUDIO_DBFS} dBFS), skipping STT")
            cues_dir = get_cues_dir(config.cues_dir)
            audio_cue("paused", cues_dir)
            return

        log(f"Recording was {duration:.1f}s ({raw_rms_db:.1f} dBFS), transcribing...")
        print(f"[recording] Transcribing {duration:.1f}s audio...", file=sys.stderr)
        t0 = time.time()
        text = transcribe_audio(
            audio_chunks,
            engine=config.stt.local.engine,
            mlx_model=config.stt.local.mlx_model,
            language=config.stt.local.language,
            sample_rate=config.audio.sample_rate,
        )
        elapsed = time.time() - t0
        log(f"Transcription ({elapsed:.1f}s): {text[:80]}{'...' if len(text) > 80 else ''}")

        # Log raw STT output for debug
        _save_debug_audio("_stt_result", [], config.audio.sample_rate, {
            "stt_raw": text[:200],
            "stt_engine": config.stt.local.engine,
            "stt_time_s": round(elapsed, 2),
        })

        # ECHO-03: Filter TTS echo from transcription (speaker mode protection).
        # If the STT output matches recently spoken TTS text, it's echo, not the user.
        echo_filtered = False
        if text and config.echo_suppression.stt_echo_filter:
            try:
                from heyvox.audio.echo import filter_tts_echo
                filtered = filter_tts_echo(text)
                if filtered != text:
                    log(f"ECHO-03: Stripped TTS echo from transcription (was: {text[:60]})")
                    echo_filtered = True
                    text = filtered
            except Exception:
                pass

        # Quality filter: discard garbled/nonsensical STT output
        if text and _is_garbled(text):
            log(f"FILTER: Discarding garbled transcription: {text[:80]}")
            cues_dir = get_cues_dir(config.cues_dir)
            audio_cue("paused", cues_dir)
            return

        _hud_send({"type": "transcript", "text": text})

        # Persist transcript BEFORE paste attempt — guarantees no text is ever lost
        if text and text.strip():
            try:
                from heyvox.history import save as _save_transcript
                _save_transcript(text, duration=duration, ptt=ptt)
            except Exception as e:
                log(f"WARNING: Failed to save transcript to history: {e}")

        cues_dir = get_cues_dir(config.cues_dir)

        if not text:
            log("WARNING: Empty transcription, skipping")
            audio_cue("paused", cues_dir)
            return

        # Check if cancelled during transcription
        if _cancel_transcription.is_set():
            log("Transcription cancelled by user (Escape)")
            audio_cue("paused", cues_dir)
            _cancel_transcription.clear()
            return

        # Check for voice commands
        cmd_result = check_voice_command(text)
        if cmd_result:
            action_key, feedback = cmd_result
            log(f"Voice command: {action_key} ({feedback})")

            # Dispatch to native TTS engine for skip/stop/mute when enabled
            # Requirement: TTS-03
            _handled_natively = False
            if config.tts.enabled:
                if action_key == "tts-skip":
                    from heyvox.audio.tts import skip_current
                    skip_current()
                    _handled_natively = True
                elif action_key == "tts-stop":
                    from heyvox.audio.tts import stop_all
                    stop_all()
                    _handled_natively = True
                elif action_key == "tts-mute":
                    from heyvox.audio.tts import set_muted, is_muted
                    set_muted(not is_muted())
                    _handled_natively = True

            # Fall through to execute_voice_command for tts-next/tts-replay
            # (not yet implemented natively) or when TTS is disabled.
            if not _handled_natively:
                tts_script = config.tts.script_path if config.tts.enabled else None
                execute_voice_command(action_key, feedback, tts_script_path=tts_script, log_fn=log)

            audio_cue("paused", cues_dir)
            return

        # Strip wake word phrases from transcription (start and end)
        pre_strip = text
        text = _strip_wake_words(text, config.wake_words.start, config.wake_words.stop)
        if text != pre_strip:
            log(f"Wake word strip: '{pre_strip[:80]}' → '{text[:80]}'")

        # Final debug log entry with full pipeline result
        _save_debug_audio("_final", [], config.audio.sample_rate, {
            "stt_raw": pre_strip[:200] if 'pre_strip' in dir() else "",
            "echo_filtered": echo_filtered if 'echo_filtered' in dir() else False,
            "wake_word_stripped": text != pre_strip if 'pre_strip' in dir() else False,
            "final_text": text[:200],
        })

        paste_text = f"{config.transcription_prefix}{text}" if config.transcription_prefix else text

        # Re-check cancellation right before typing
        if _cancel_transcription.is_set():
            log("Transcription cancelled by user (Escape)")
            audio_cue("paused", cues_dir)
            _cancel_transcription.clear()
            return

        # Dedup guard: prevent multiple injections from concurrent _send_local threads
        global _last_inject_time
        with _inject_lock:
            now = time.time()
            if now - _last_inject_time < _INJECT_DEDUP_SECS:
                log(f"WARNING: Duplicate injection suppressed ({now - _last_inject_time:.1f}s since last)")
                return
            _last_inject_time = now

        target_app = recording_target.app_name if recording_target else None
        target_window = recording_target.window_title if recording_target else None
        log(f"[inject] target_app={target_app}, window={target_window!r}, "
            f"mode={'PTT' if ptt else 'wake word'}, text={len(paste_text)} chars: {paste_text[:60]!r}")
        print(f"[recording] Injecting → {target_app or 'frontmost'} (window={target_window!r})", file=sys.stderr)

        # Save the user's current focus so we can restore it if injection
        # steals focus from the SAME app they're already in. We only restore
        # if the user hasn't moved away from the target — if they switched
        # apps during transcription, we stay on the target (their intent at
        # recording start) rather than chasing them to a different app.
        pre_inject_pid = save_frontmost_pid()
        target_pid = recording_target.app_pid if recording_target else 0
        log(f"[inject] saved pre-inject frontmost pid={pre_inject_pid}, target pid={target_pid}")

        if recording_target:
            if recording_target.conductor_workspace:
                log(f"[inject] Restoring Conductor workspace '{recording_target.conductor_workspace}'")
            restore_target(recording_target)
            log(f"[inject] Restored target: {recording_target.app_name}")

        type_text(paste_text, app_name=target_app)

        # Auto-send Enter in wake word mode if adapter says so
        auto_send = not ptt and adapter.should_auto_send()
        log(f"[inject] auto_send={auto_send} (ptt={ptt}, adapter.should_auto_send={adapter.should_auto_send()}, enter_count={adapter.enter_count})")
        if auto_send:
            time.sleep(1.0)
            # target_app already resolved above
            from heyvox.input.injection import press_enter as _press_enter
            log(f"Pressing Enter x{adapter.enter_count} → {target_app or 'frontmost'}...")
            _press_enter(adapter.enter_count, app_name=target_app)
            log("Sent!")
        else:
            log(f"Pasted ({'PTT' if ptt else 'wake word'})")

        # Only restore focus if the user was already in the target app before
        # injection (i.e., we didn't need to switch apps). If the user moved
        # to a different app during transcription, stay on the target — that's
        # where they intended the text to go when they started recording.
        if pre_inject_pid and pre_inject_pid == target_pid:
            time.sleep(0.3)
            restore_frontmost(pre_inject_pid)
            log(f"[inject] restored frontmost to pid={pre_inject_pid} (was already on target)")
        elif pre_inject_pid and pre_inject_pid != target_pid:
            log(f"[inject] NOT restoring frontmost (user moved to pid={pre_inject_pid} during transcription, staying on target)")
        # Show "Sent to [agent]" confirmation in HUD
        _target_name = None
        if not ptt:
            _target_name = (
                getattr(adapter, 'last_agent_name', None)
                or getattr(adapter, '_target_app', None)
            )
        if ptt:
            # PTT mode: no auto-Enter, just pasted — don't say "Sending"
            _hud_send({"type": "state", "state": "idle", "text": "Pasted"})
            audio_cue("ok", cues_dir)
            log("Pasted (PTT)")
        else:
            sent_msg = "Sent to AI"
            _hud_send({"type": "state", "state": "idle", "text": sent_msg})
            audio_cue("sending", cues_dir)
            log(sent_msg)
    except subprocess.TimeoutExpired:
        log("WARNING: Subprocess timed out during send phase")
    except Exception as e:
        log(f"ERROR in send phase: {e}")
    finally:
        _release_recording_guard()
        with _state_lock:
            busy = False
        # Resume media that we paused at recording start
        try:
            from heyvox.audio.media import resume_media
            resume_media()
        except Exception as e:
            log(f"WARNING: media resume failed: {e}")
        _hud_send({"type": "state", "state": "idle"})
        log("Ready for next wake word.")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

_PID_FILE = "/tmp/heyvox.pid"


def _acquire_singleton():
    """Ensure only one vox instance runs at a time via PID file lock.

    If a previous instance is still running, SIGTERM it (then SIGKILL after 1s).
    Also cleans up stale flag files left by a forcefully killed predecessor.
    """
    if os.path.exists(_PID_FILE):
        try:
            with open(_PID_FILE) as _f:
                old_pid = int(_f.read().strip())
            os.kill(old_pid, 0)  # Check if alive
            # Verify the process is actually heyvox (not a recycled PID).
            # psutil is more reliable than a ps subprocess: it reads /proc (or
            # the macOS equivalent) atomically, avoids a fork/exec race, and
            # raises NoSuchProcess if the PID disappears between the os.kill(0)
            # check above and this point.
            try:
                import psutil as _psutil
                _proc = _psutil.Process(old_pid)
                _cmdline = " ".join(_proc.cmdline()).lower()
                if "heyvox" not in _cmdline:
                    log(
                        f"PID {old_pid} is not heyvox "
                        f"(cmd: {_cmdline!r}), removing stale PID file"
                    )
                    raise ProcessLookupError("not heyvox")
            except (_psutil.NoSuchProcess, _psutil.AccessDenied):
                raise ProcessLookupError("gone or inaccessible")
            log(f"Killing previous vox instance (PID {old_pid})")
            os.kill(old_pid, signal.SIGTERM)
            time.sleep(1)
            try:
                os.kill(old_pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        except (ProcessLookupError, ValueError):
            pass  # Old process already dead or not vox
        except PermissionError:
            log(f"WARNING: Cannot kill existing vox (PID {old_pid}), permission denied")

    # Clean up stale flag files and sockets from previous instance
    import glob as _glob
    for pattern in ("/tmp/heyvox-recording", "/tmp/heyvox-media-paused-*",
                     "/tmp/herald-media-paused-*", "/tmp/herald-pause",
                     "/tmp/heyvox-hud.sock",
                     "/tmp/claude-tts-mute", "/tmp/herald-mute", "/tmp/heyvox-verbosity",
                     # Herald state files that can go stale after crash
                     "/tmp/herald-ambient", "/tmp/herald-mode",
                     "/tmp/herald-last-play", "/tmp/herald-workspace",
                     # Temp WAVs from crashed TTS worker
                     "/tmp/herald-generating-*.wav"):
        for stale in _glob.glob(pattern):
            try:
                os.unlink(stale)
            except (FileNotFoundError, IsADirectoryError):
                pass

    # Write PID file and hold an advisory lock for the lifetime of the process.
    # This eliminates the race window between reading the old PID and writing
    # the new one — a concurrent starter will block on flock().
    import fcntl
    global _pid_fd
    _pid_fd = open(_PID_FILE, "w")
    try:
        fcntl.flock(_pid_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        log("Another vox instance holds the PID lock — exiting")
        _pid_fd.close()
        sys.exit(1)
    _pid_fd.write(str(os.getpid()))
    _pid_fd.flush()


# File descriptor kept open to hold the flock for the process lifetime.
_pid_fd = None


def _release_singleton():
    """Release PID lock and remove PID file on exit."""
    global _pid_fd
    try:
        if _pid_fd is not None:
            import fcntl
            fcntl.flock(_pid_fd, fcntl.LOCK_UN)
            _pid_fd.close()
            _pid_fd = None
        if os.path.exists(_PID_FILE):
            with open(_PID_FILE) as _f:
                pid = int(_f.read().strip())
            if pid == os.getpid():
                os.unlink(_PID_FILE)
    except (OSError, ValueError):
        pass


def main() -> None:
    """Main event loop — loads config, starts PTT, runs wake word detection."""
    global is_recording, _audio_buffer, busy, _zombie_mic_reinit, _consecutive_failed_recordings, _busy_since

    # Load configuration from ~/.config/heyvox/config.yaml (or defaults)
    # Requirement: CONF-01
    config = load_config()
    _init_log(config.log_file, config.log_max_bytes)
    # Diagnostic: verify log() is working (both print to stderr and log to file)
    print(f"[diag] _LOG_FILE={_LOG_FILE}, exists={os.path.exists(_LOG_FILE)}", file=sys.stderr, flush=True)
    log("STARTUP: log() initialized")
    # Verify it actually wrote
    try:
        with open(_LOG_FILE) as f:
            last = f.readlines()[-1].strip()
        print(f"[diag] log() wrote: {last}", file=sys.stderr, flush=True)
    except Exception as e:
        print(f"[diag] log() verification FAILED: {e}", file=sys.stderr, flush=True)

    # Last-resort crash logging: catches exceptions that slip through main try/except
    def _excepthook(exc_type, exc_value, exc_tb):
        import traceback as _tb
        msg = "".join(_tb.format_exception(exc_type, exc_value, exc_tb))
        log(f"UNHANDLED EXCEPTION (excepthook):\n{msg}")
    sys.excepthook = _excepthook

    # Singleton: kill any previous instance and write our PID
    _acquire_singleton()
    import atexit
    atexit.register(_release_singleton)
    atexit.register(lambda: log("EXIT: atexit handler fired — process is terminating"))

    # Startup cleanup: remove stale flags from previous crash/kill
    # Check heartbeat from previous instance — if it exists and is stale,
    # the old process was killed without clean shutdown (SIGKILL / native crash).
    try:
        hb_age = time.time() - os.path.getmtime("/tmp/heyvox-heartbeat")
        if hb_age > 30:
            log(f"WARNING: Previous instance died without clean shutdown (heartbeat stale by {hb_age:.0f}s)")
    except FileNotFoundError:
        pass
    for stale_flag in (RECORDING_FLAG, "/tmp/heyvox-tts-playing", "/tmp/claude-tts-playing.pid",
                       "/tmp/heyvox-media-paused-rec"):
        try:
            age = time.time() - os.path.getmtime(stale_flag)
            if age > 60:
                os.unlink(stale_flag)
                log(f"Removed stale flag: {stale_flag} (age={age:.0f}s)")
            else:
                os.unlink(stale_flag)
                log(f"Removed leftover flag: {stale_flag}")
        except FileNotFoundError:
            pass
        except OSError:
            pass

    # B6: Clean up orphaned media-pause flags that were not removed by
    # _acquire_singleton (e.g. left by a SIGKILL'd predecessor that used a
    # workspace-specific suffix).  Only remove files older than 60 seconds so
    # we don't disturb a flag written by a concurrent legitimate pause.
    import glob as _glob_b6
    for _mp_pattern in ("/tmp/herald-media-paused-*", "/tmp/heyvox-media-paused-*"):
        for _mp_file in _glob_b6.glob(_mp_pattern):
            try:
                _mp_age = time.time() - os.path.getmtime(_mp_file)
                if _mp_age > 60:
                    os.unlink(_mp_file)
                    log(f"Cleaned stale media-pause flag: {_mp_file} (age={_mp_age:.0f}s)")
            except OSError:
                pass

    # Start native TTS worker if enabled
    # Requirement: TTS-03
    from heyvox.audio.tts import start_worker as _start_tts, shutdown as _shutdown_tts
    if config.tts.enabled:
        _start_tts(config)
        log("TTS worker started (Kokoro native engine)")

    # Signal handlers for clean shutdown
    def handle_signal(signum, frame):
        log(f"Received signal {signum}, shutting down...")
        _shutdown.set()

    def handle_cancel(signum, frame):
        """SIGUSR1 = request recording cancellation (deferred to main loop).

        Signal handlers must avoid I/O and locks — just set an event.
        The main loop checks _cancel_requested and does the actual cleanup.
        """
        _cancel_requested.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGUSR1, handle_cancel)

    # Wake word settings
    start_word = config.wake_words.start
    stop_word = config.wake_words.stop
    use_separate_words = start_word != stop_word
    threshold = config.threshold
    cooldown = config.cooldown_secs
    sample_rate = config.audio.sample_rate
    chunk_size = config.audio.chunk_size
    mic_priority = config.mic_priority
    silence_timeout = config.silence_timeout_secs
    silence_threshold = config.silence_threshold

    _kill_orphan_indicators()

    # Launch HUD overlay process (persistent — stays alive for entire session)
    # Then connect HUD client for IPC. The overlay needs ~0.5s to start its
    # socket server, so the initial connect may fail — _hud_send auto-reconnects.
    # Requirement: HUD-08
    global _hud_client, _indicator_proc
    if config.hud_enabled or config.hud_menu_bar_only:
        _launch_hud_overlay(menu_bar_only=config.hud_menu_bar_only)
        from heyvox.hud.ipc import HUDClient
        _hud_client = HUDClient(HUD_SOCKET_PATH)
        try:
            _hud_client.connect()
        except Exception:
            pass
    else:
        log("HUD overlay disabled via config")

    # Initialize STT backend
    log(f"STT backend: {config.stt.backend}")
    if config.stt.backend == "local":
        script_dir = os.path.dirname(os.path.abspath(__file__))
        init_local_stt(
            engine=config.stt.local.engine,
            mlx_model=config.stt.local.mlx_model,
            model_dir=os.path.join(script_dir, config.stt.local.model_dir),
            language=config.stt.local.language,
            threads=config.stt.local.threads,
            log_fn=log,
        )

    # Start push-to-talk listener if enabled
    if config.push_to_talk.enabled:
        from heyvox.input.ptt import start_ptt_listener

        ptt_callbacks = {
            "on_start": lambda: start_recording(ptt=True, config=config),
            "on_stop": lambda: stop_recording(config=config),
            "on_cancel_transcription": lambda: _cancel_transcription.set(),
            "on_cancel_recording": lambda: _cancel_recording_from_ptt(config),
            "on_cancel_tts": _stop_tts_from_escape,
            "is_busy": lambda: busy,
            "is_recording": lambda: is_recording,
            "is_speaking": lambda: os.path.exists(TTS_PLAYING_FLAG) or os.path.exists("/tmp/herald-playing.pid"),
        }
        start_ptt_listener(config.push_to_talk.key, ptt_callbacks, log_fn=log)

    # Load wake word models
    # Phase 8: supports custom models dir from config, with fallback search
    from heyvox.audio.wakeword import load_models
    model, use_separate_words = load_models(
        start_word, stop_word, config.wake_words.models_dir,
        also_load=config.wake_words.also_load,
    )
    _loaded_models = list(model.models.keys()) if hasattr(model, 'models') else []
    print(f"[wakeword] Loaded models: {_loaded_models}, also_load={config.wake_words.also_load}", file=sys.stderr, flush=True)
    log(f"Wake word models loaded: {_loaded_models}")

    # Build text injection adapter based on config.target_mode
    # Requirement: INPT-03
    global _adapter
    _adapter = _build_adapter(config)
    log(f"Target mode: {config.target_mode} (adapter: {type(_adapter).__name__})")

    # Open audio stream — delegate to DeviceManager
    # AppContext bridges recording state (still in module globals in Plan 02)
    _ctx = AppContext()
    _ctx.last_good_audio_time = time.time()
    devices = DeviceManager(ctx=_ctx, config=config, log_fn=log, hud_send=_hud_send)
    devices.init()
    # Expose device attributes as local aliases (used by rest of main())
    pa = devices.pa
    stream = devices.stream
    dev_index = devices.dev_index
    dev_name = devices.dev_name
    headset_mode = devices.headset_mode

    # ECHO-01: Track when TTS was last seen active (for post-TTS cooldown)
    _tts_last_seen = 0.0

    # ECHO-05: Initialize WebRTC AEC if configured and in speaker mode
    _aec_active = False
    if config.echo_suppression.aec_enabled and not headset_mode:
        try:
            from heyvox.audio.echo import init_aec
            _aec_active = init_aec(delay_ms=config.echo_suppression.aec_delay_ms)
            if _aec_active:
                log(f"WebRTC AEC active (delay={config.echo_suppression.aec_delay_ms}ms)")
            else:
                log("WebRTC AEC requested but not available (pip install heyvox[aec])")
        except Exception as e:
            log(f"WebRTC AEC init error: {e}")

    if use_separate_words:
        log(f"Ready! Say '{start_word}' to start, '{stop_word}' to stop.")
    else:
        log(f"Ready! Say '{start_word}' to start/stop voice input.")

    cues_dir = get_cues_dir(config.cues_dir)
    last_trigger = 0.0
    consecutive_errors = 0

    # Pre-roll ring buffer: captures ~500ms of audio before wake word trigger
    # so the first words of the command aren't clipped. (Research recommendation)
    from collections import deque
    _PREROLL_CHUNKS = max(1, int(0.5 * sample_rate / chunk_size))  # ~500ms
    _preroll_buffer: deque = deque(maxlen=_PREROLL_CHUNKS)

    # Device state variables removed — now managed by DeviceManager (Plan 02).
    # _zero_streak, _health_cv_history, _mic_pinned, _last_device_scan,
    # _last_output_device, HEALTH_CHECK_INTERVAL, last_health_check all moved.

    # Memory watchdog — warn if RSS exceeds threshold
    _MEM_WARN_MB = 1500
    _last_mem_check = time.time()
    _MEM_CHECK_INTERVAL = 60.0  # Check every 60s

    # SIGKILL-proof heartbeat: mtime on this file is the last proof of life.
    # If the process dies without logging (SIGKILL, native crash), check
    # this file's mtime to narrow down when the death occurred.
    _HEARTBEAT_FILE = "/tmp/heyvox-heartbeat"
    _HEARTBEAT_INTERVAL = 10.0  # Touch every 10s (low overhead)
    _last_heartbeat = 0.0

    try:
        while not _shutdown.is_set():
            # Bridge recording state from module globals to AppContext (Plan 02 glue,
            # removed in Plan 03 when recording state moves fully to AppContext).
            _ctx.is_recording = is_recording
            _ctx.busy = busy
            _ctx.zombie_mic_reinit = _zombie_mic_reinit

            # Heartbeat: touch file periodically as proof of life
            _now_hb = time.time()
            if _now_hb - _last_heartbeat >= _HEARTBEAT_INTERVAL:
                _last_heartbeat = _now_hb
                try:
                    with open(_HEARTBEAT_FILE, "w") as _hbf:
                        _hbf.write(f"{int(_now_hb)}\n")
                except Exception:
                    pass
            # Handle deferred cancel from SIGUSR1 (signal-safe: no I/O in handler)
            if _cancel_requested.is_set():
                _cancel_requested.clear()
                if is_recording:
                    log("Received cancel signal (USR1)")
                    _show_recording_indicator(False)
                    _release_recording_guard()
                    with _state_lock:
                        is_recording = False
                        busy = False
                        _audio_buffer.clear()
                    try:
                        from heyvox.audio.media import resume_media
                        resume_media()
                    except Exception:
                        pass
                    cues_dir = get_cues_dir(config.cues_dir)
                    audio_cue("paused", cues_dir)
                    _hud_send({"type": "state", "state": "idle"})
                    log("Recording cancelled via signal.")

            try:
                audio = np.frombuffer(
                    stream.read(chunk_size, exception_on_overflow=False),
                    dtype=np.int16,
                )
                consecutive_errors = 0

                # AUDIO-13: track last time we saw real audio (level >= 10)
                if int(np.abs(audio).max()) >= 10:
                    _ctx.last_good_audio_time = time.time()
                    _last_good_audio_time = _ctx.last_good_audio_time  # keep global in sync

            except IOError as e:
                consecutive_errors += 1
                if consecutive_errors <= 2:
                    log(f"Audio read error ({consecutive_errors}/2): {e}")
                    time.sleep(0.1)
                    continue
                if not devices.handle_io_error():
                    continue
                # Update local aliases after device switch
                pa = devices.pa
                stream = devices.stream
                dev_index = devices.dev_index
                dev_name = devices.dev_name
                headset_mode = devices.headset_mode
                model.reset()  # Clear corrupted wake word state (AUDIO-12)
                consecutive_errors = 0
                continue

            # AUDIO-13: time-based dead mic detection — delegates to DeviceManager
            devices.check_dead_mic_timeout()
            # Sync flag back to module global for start_recording() guard
            _zombie_mic_reinit = _ctx.zombie_mic_reinit

            # Zombie stream reinit — triggered by consecutive failed recordings (AUDIO-12)
            # or time-based dead mic timeout (AUDIO-13)
            if _ctx.zombie_mic_reinit:
                _ctx.zombie_mic_reinit = False
                _zombie_mic_reinit = False
                if not devices.reinit(require_audio=True):
                    continue
                # Update local aliases after reinit
                pa = devices.pa
                stream = devices.stream
                dev_index = devices.dev_index
                dev_name = devices.dev_name
                headset_mode = devices.headset_mode
                model.reset()
                consecutive_errors = 0
                continue

            # Buffer audio during recording (for local STT)
            with _state_lock:
                _is_rec = is_recording
                _is_busy = busy
                _is_ptt = _triggered_by_ptt
                if _is_rec and config.stt.backend == "local":
                    _audio_buffer.append(audio.copy())
                elif not _is_rec and not _is_busy:
                    # Feed pre-roll buffer when idle — captures audio before wake word
                    _preroll_buffer.append(audio.copy())

            # Send live audio level to HUD at ~20fps during recording (HUD-08)
            # Uses RMS + log scaling for sensitivity to whispers
            if _is_rec:
                global _hud_last_level_send
                now_level = time.time()
                if now_level - _hud_last_level_send >= _HUD_LEVEL_INTERVAL:
                    _hud_last_level_send = now_level
                    rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))
                    # Log scale: map RMS ~30-10000 to 0.0-1.0
                    import math
                    if rms > 1.0:
                        db = 20.0 * math.log10(rms)
                        # ~30 dB (whisper) → 0.0, ~80 dB (loud) → 1.0
                        level = max(0.0, min(1.0, (db - 30.0) / 50.0))
                    else:
                        level = 0.0
                    _hud_send({"type": "audio_level", "level": round(level, 3)})

            # Proactive silent-mic health check (catches A2DP bad-state without IOError)
            # Requirement: AUDIO-08 / AUDIO-12 — delegated to DeviceManager
            if not _is_rec and not _is_busy:
                _hud_ensure_connected()
                now = time.time()
                _prev_stream = devices.stream
                devices.health_check(audio)
                # If health_check recovered to a new stream, update local aliases
                if devices.stream is not _prev_stream and devices.stream is not None:
                    pa = devices.pa
                    stream = devices.stream
                    dev_index = devices.dev_index
                    dev_name = devices.dev_name
                    headset_mode = devices.headset_mode
                    model.reset()  # Clear corrupted wake word state (AUDIO-12)
                    consecutive_errors = 0
                # Sync zombie flag back (health_check may set it if no mic was found)
                if _ctx.zombie_mic_reinit:
                    _zombie_mic_reinit = True

                # Memory watchdog — check RSS every 60s
                _MEM_CRITICAL_MB = 1000
                if now - _last_mem_check >= _MEM_CHECK_INTERVAL:
                    _last_mem_check = now
                    # Use current RSS (not peak/high-water mark) so the watchdog
                    # doesn't false-trigger after MLX model is loaded then unloaded.
                    import psutil
                    rss_mb = psutil.Process().memory_info().rss / 1024 / 1024
                    if rss_mb > _MEM_CRITICAL_MB:
                        log(f"WATCHDOG: Memory critical ({rss_mb:.0f} MB), auto-restarting...")
                        _hud_send({"type": "error", "text": f"Restarting: {rss_mb:.0f}MB"})
                        time.sleep(0.5)
                        # Release PID lock before execv so the new process image
                        # can acquire it cleanly (execv preserves PID but closes fds)
                        _release_singleton()
                        try:
                            os.execv(sys.executable, [sys.executable, "-m", "heyvox.main"])
                        except Exception as exc:
                            # execv failed (e.g. syntax error in new code) — fall back
                            # to subprocess so the process doesn't just die silently.
                            log(f"WATCHDOG: execv failed ({exc}), falling back to subprocess restart")
                            import subprocess as _sp
                            _sp.Popen([sys.executable, "-m", "heyvox.main"],
                                      start_new_session=True)
                            _shutdown.set()
                    elif rss_mb > _MEM_WARN_MB:
                        log(f"WARNING: Memory usage high: {rss_mb:.0f} MB (threshold: {_MEM_WARN_MB} MB)")
                        _hud_send({"type": "error", "text": f"Memory: {rss_mb:.0f}MB"})

            # Overlay health: relaunch if dead, kill duplicates (piggyback on device scan interval)
            _now_scan = time.time()
            if not _is_rec and not _is_busy and _now_scan - devices._last_device_scan >= devices._DEVICE_SCAN_INTERVAL:
                if _indicator_proc is not None:
                    if _indicator_proc.poll() is not None:
                        log(f"WARNING: HUD overlay exited (rc={_indicator_proc.returncode}), relaunching")
                        _indicator_proc = None
                        _launch_hud_overlay(menu_bar_only=config.hud_menu_bar_only)
                    else:
                        _kill_duplicate_overlays(keep_pid=_indicator_proc.pid)

            # Device hotplug — delegated to DeviceManager (includes mic switch + output change)
            _prev_stream_scan = devices.stream
            devices.scan()
            # Update local aliases if scan changed the device
            if devices.stream is not _prev_stream_scan and devices.stream is not None:
                pa = devices.pa
                stream = devices.stream
                dev_index = devices.dev_index
                dev_name = devices.dev_name
                headset_mode = devices.headset_mode

            # Silence watchdog — end recording after silence_timeout seconds of quiet
            # Only for wake-word triggered recordings (PTT has natural release)
            # If speech was captured before the silence, transcribe it.
            # Only discard if the entire recording is silent (false wake word trigger).
            if _is_rec and not _is_ptt and silence_timeout > 0:
                elapsed = time.time() - recording_start_time
                if elapsed > silence_timeout:
                    with _state_lock:
                        recent_chunks = _audio_buffer[-int(silence_timeout * sample_rate / chunk_size):]
                        all_chunks = list(_audio_buffer)
                    if recent_chunks:
                        max_recent = max(int(np.abs(c).max()) for c in recent_chunks)
                        if max_recent < silence_threshold:
                            # Recent audio is silent — check if ANY speech was captured
                            max_overall = max(int(np.abs(c).max()) for c in all_chunks) if all_chunks else 0
                            if max_overall < silence_threshold:
                                # Entire recording is silent — discard (false trigger)
                                log(f"Silence timeout ({silence_timeout}s, all silent max={max_overall}), cancelling")
                                _show_recording_indicator(False)
                                _release_recording_guard()
                                with _state_lock:
                                    is_recording = False
                                    busy = False
                                    _audio_buffer.clear()
                                audio_cue("paused", cues_dir)
                                _hud_send({"type": "state", "state": "idle"})
                                log("Ready for next wake word.")
                                continue
                            else:
                                # Speech was captured before silence — transcribe it
                                log(f"Silence timeout ({silence_timeout}s, max_recent={max_recent}), "
                                    f"but speech detected (max_overall={max_overall}), transcribing")
                                stop_recording(config=config)
                                continue

            if _is_busy:
                # Busy flag watchdog — force-reset if stuck (AUDIO-12)
                if _busy_since == 0.0:
                    _busy_since = time.time()
                elif time.time() - _busy_since > _BUSY_TIMEOUT:
                    log(f"WARNING: busy flag stuck for {_BUSY_TIMEOUT}s, force-resetting (watchdog)")
                    print(f"[watchdog] busy flag stuck for {_BUSY_TIMEOUT}s, resetting", file=sys.stderr, flush=True)
                    with _state_lock:
                        busy = False
                    _busy_since = 0.0
                    _release_recording_guard()
                    _hud_send({"type": "state", "state": "idle"})
                    # Fall through to wake word processing
                else:
                    continue
            else:
                _busy_since = 0.0

            # Suppress wake word detection while audio cue plays
            if is_suppressed():
                continue

            # Echo suppression: skip wake word while ANY TTS is playing.
            # Even headsets can have mic bleed (e.g. G435 wireless).
            # Check both Vox's own flag and Conductor's external TTS flag.
            # Requirement: AUDIO-09, AUDIO-10
            _tts_active = False
            for _tts_flag in (TTS_PLAYING_FLAG, "/tmp/claude-tts-playing.pid"):
                if os.path.exists(_tts_flag):
                    try:
                        flag_age = time.time() - os.path.getmtime(_tts_flag)
                        if flag_age < TTS_PLAYING_MAX_AGE_SECS:
                            _tts_active = True
                            _tts_last_seen = time.time()
                            break
                    except OSError:
                        pass
            if _tts_active:
                continue  # Suppress wake word during TTS playback

            # ECHO-01: Post-TTS cooldown — suppress wake word for grace period
            # after TTS ends to handle room reverb tail.
            _echo_grace = config.echo_suppression.grace_after_tts
            if _echo_grace > 0 and _tts_last_seen > 0:
                since_tts = time.time() - _tts_last_seen
                if since_tts < _echo_grace:
                    continue  # Still in reverb tail grace period

            # ECHO-05: Process mic frame through WebRTC AEC if enabled
            if _aec_active:
                from heyvox.audio.echo import process_mic_frame
                audio = process_mic_frame(audio, sample_rate=sample_rate)

            model.predict(audio)

            # ECHO-02: Dynamic threshold in speaker mode — raise threshold
            # when no headset is detected to reduce false triggers from ambient audio.
            _speaker_mult = config.echo_suppression.speaker_threshold_multiplier if not headset_mode else 1.0

            # Cooldown is shorter during recording — repeated stop attempts should
            # get through quickly. Start cooldown stays normal to prevent double-triggers.
            stop_cooldown = min(cooldown, 0.5)

            _model_thresholds = config.wake_words.model_thresholds

            for ww_name, score in model.prediction_buffer.items():
                s = score[-1]
                # Per-model threshold override (e.g. hey_vox: 0.95 for noisy models)
                base_thr = _model_thresholds.get(ww_name, threshold)
                active_threshold = (base_thr * _speaker_mult * 0.85) if _is_rec else (base_thr * _speaker_mult)
                active_cooldown = stop_cooldown if _is_rec else cooldown
                log_threshold = active_threshold * 0.5
                if s > log_threshold:
                    triggered = s > active_threshold
                    msg = f"  [{ww_name}] score={s:.3f} (thr={active_threshold:.2f}) {'>>> TRIGGER' if triggered else ''}"
                    log(msg)
                    if triggered:
                        print(f"[wakeword] {msg.strip()}", file=sys.stderr, flush=True)
                if s > active_threshold:
                    now = time.time()
                    if now - last_trigger > active_cooldown:
                        last_trigger = now
                        # PTT owns the recording lifecycle — ignore wake words
                        # Use _is_ptt/_is_rec snapshots (taken under _state_lock above)
                        # to avoid reading bare globals from the main thread.
                        if _is_ptt and _is_rec:
                            pass
                        elif use_separate_words:
                            if start_word in ww_name and not _is_rec:
                                start_recording(config=config, preroll=_preroll_buffer)
                            elif stop_word in ww_name and _is_rec:
                                stop_recording(config=config)
                        else:
                            if not _is_rec:
                                start_recording(config=config, preroll=_preroll_buffer)
                            else:
                                stop_recording(config=config)
                    model.reset()

    except KeyboardInterrupt:
        log("Stopped by user")
    except SystemExit as e:
        log(f"FATAL: SystemExit({e.code}) in main loop")
        import traceback
        log(traceback.format_exc())
    except Exception:
        log("FATAL: Unhandled exception in main loop")
        import traceback
        log(traceback.format_exc())
    finally:
        log("Cleaning up...")
        # Always clean up flag files to avoid blocking TTS orchestrator
        for flag in (RECORDING_FLAG, TTS_PLAYING_FLAG, "/tmp/herald-pause"):
            try:
                os.unlink(flag)
            except FileNotFoundError:
                pass
        # Clean up media pause flags (both heyvox and herald namespaces)
        import glob as _glob
        for f in _glob.glob("/tmp/heyvox-media-paused-*") + _glob.glob("/tmp/herald-media-paused-*"):
            try:
                os.unlink(f)
            except FileNotFoundError:
                pass
        if _hud_client:
            _hud_client.close()
        # Only kill HUD on explicit stop (SIGTERM/SIGINT), not on watchdog restart
        # or unhandled exception — so the user keeps the menu bar icon to restart.
        if _shutdown.is_set():
            _stop_hud_overlay()
        # Shut down native TTS worker cleanly (drains queue + joins thread)
        # Requirement: TTS-03
        if config.tts.enabled:
            _shutdown_tts()
            log("TTS worker stopped")
        devices.cleanup()
        # Keep ACTIVE_MIC_FILE across restarts so HUD shows last-known mic
        # instead of "None" during the brief startup window before mic detection.
        log("Shutdown complete.")


def _stop_tts_from_escape() -> None:
    """Stop TTS playback and clear queue (used by Escape key handler)."""
    from heyvox.audio.tts import stop_all
    stop_all()
    _hud_send({"type": "state", "state": "idle"})
    log("TTS stopped via Escape key.")


def _cancel_recording_from_ptt(config: HeyvoxConfig) -> None:
    """Cancel an active recording (used by PTT Escape handler)."""
    global is_recording, busy
    _show_recording_indicator(False)
    _release_recording_guard()
    with _state_lock:
        is_recording = False
        busy = False
        _audio_buffer.clear()
    try:
        from heyvox.audio.media import resume_media
        resume_media()
    except Exception:
        pass
    cues_dir = get_cues_dir(config.cues_dir)
    audio_cue("paused", cues_dir)
    _hud_send({"type": "state", "state": "idle"})
    log("Recording cancelled.")


def run() -> None:
    """CLI entry point — called by vox.cli on 'heyvox start'."""
    main()


if __name__ == "__main__":
    run()
