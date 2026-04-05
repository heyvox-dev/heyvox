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
from heyvox.constants import (
    RECORDING_FLAG,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_CHUNK_SIZE,
    TTS_PLAYING_FLAG,
    TTS_PLAYING_MAX_AGE_SECS,
    HUD_SOCKET_PATH,
    STT_DEBUG_DIR,
    GRACE_AFTER_TTS,
    ACTIVE_MIC_FILE,
    MIC_SWITCH_REQUEST_FILE,
)
from heyvox.audio.mic import find_best_mic, open_mic_stream, detect_headset
from heyvox.audio.cues import audio_cue, is_suppressed, get_cues_dir, device_change_cue
from heyvox.audio.stt import init_local_stt, transcribe_audio
from heyvox.audio.tts import check_voice_command, execute_voice_command
from heyvox.input.injection import type_text
from heyvox.input.target import snapshot_target, restore_target


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
            log(f"[HUD-DBG] Reconnect succeeded but sock still None")
            return
        log(f"[HUD-DBG] Reconnected!")
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
# ---------------------------------------------------------------------------

# Common transcription variants of wake word model names.
# Whisper may transcribe "hey_jarvis_v0.1" as "Hey Jarvis", "hey jarvis",
# "Hey, Jarvis", "Hey Travis", "Hey Chavez", etc.
_WAKE_WORD_PHRASES: dict[str, list[str]] = {
    "hey_jarvis": [
        "hey jarvis", "hey, jarvis",
        "hey travis", "hey, travis",
        "hey chavez", "hey, chavez",
        "hey chavis", "hey, chavis",
        "hey charmis", "hey, charmis",
        "hey charvis", "hey, charvis",
        "hey charles", "hey, charles",
        "hey javis", "hey, javis",
        "hey javi", "hey, javi",
        "hey java", "hey, java",
        "hey job is", "hey job",
        "hey charisma",
        "hey javas", "hey, javas",
        "h-arvis", "h arvis",
        "jarvis", "jarvis.",
        "hrvs", "hrs", "hr",
        "j.a.r.v.i.s", "jar",
    ],
    "hey_vox": [
        "hey vox", "hey, vox",
        "hey box", "hey, box",
        "hey fox", "hey, fox",
        "hey vocs", "hey, vocs",
        "hey vokes", "hey, vokes",
        "hey vos", "hey, vos",
        "hey boks", "hey, boks",
        "hey vaux", "hey, vaux",
        "hey voxx", "hey, voxx",
        "hey rocks", "hey, rocks",
        "hey docs", "hey, docs",
        "hey locks", "hey, locks",
        "hey socks", "hey, socks",
        "vox", "vox.",
    ],
}


def _is_garbled(text: str) -> bool:
    """Detect garbled/nonsensical STT output from accidental wake word triggers.

    Catches common Whisper hallucination patterns:
    - Excessive repeated words/phrases
    - Mostly non-alphanumeric characters
    - Known Whisper filler hallucinations
    """
    import re

    cleaned = text.strip()
    if not cleaned:
        return False

    # Too short to be useful (single word that isn't a command)
    words = cleaned.split()
    if len(words) <= 1 and len(cleaned) < 4:
        return True

    # High ratio of repeated words (e.g. "the the the the")
    if len(words) >= 4:
        unique = set(w.lower() for w in words)
        if len(unique) / len(words) < 0.25:
            return True

    # Repeated phrases: split into bigrams and check repetition
    if len(words) >= 6:
        bigrams = [f"{words[i]} {words[i+1]}" for i in range(len(words) - 1)]
        unique_bigrams = set(b.lower() for b in bigrams)
        if len(unique_bigrams) / len(bigrams) < 0.3:
            return True

    # Mostly non-letter characters (Unicode garbage)
    alpha_chars = sum(1 for c in cleaned if c.isalpha())
    if len(cleaned) > 3 and alpha_chars / len(cleaned) < 0.4:
        return True

    # Known Whisper hallucination patterns
    hallucination_patterns = [
        r"^\.+$",                          # Just dots
        r"^[\s.,:;!?]+$",                  # Just punctuation
        r"(?i)^(thanks? for watching|subscribe)",  # YouTube artifacts
        r"(?i)^(music|applause|laughter)\s*$",     # Sound descriptions
        r"(?i)^you$",                       # Common short hallucination
    ]
    for pattern in hallucination_patterns:
        if re.match(pattern, cleaned):
            return True

    return False


def _strip_wake_words(text: str, start_model: str, stop_model: str) -> str:
    """Remove wake word phrases from the beginning and end of transcription.

    Whisper transcribes the wake word along with the user's speech. Since the
    wake word is just a trigger mechanism, it should not appear in the injected
    text. Uses both an explicit phrase list AND a fuzzy regex fallback to catch
    novel Whisper mistranscriptions (e.g. "Hey Chavis", "Hey Job is").

    Args:
        text: Raw transcription from STT.
        start_model: Wake word model name for start trigger (e.g. "hey_jarvis_v0.1").
        stop_model: Wake word model name for stop trigger.

    Returns:
        Cleaned text with wake word phrases removed from start/end.
    """
    import re

    if not text:
        return text

    # Collect all known phrases for the configured wake word models
    phrases = set()
    for model in (start_model, stop_model):
        # Strip version suffix: "hey_jarvis_v0.1" → "hey_jarvis"
        base = model.rsplit("_v", 1)[0] if "_v" in model else model
        if base in _WAKE_WORD_PHRASES:
            phrases.update(_WAKE_WORD_PHRASES[base])
        # Also add the raw model name as a phrase (underscores → spaces)
        phrases.add(base.replace("_", " "))

    # Sort longest first so "hey, jarvis" matches before "hey"
    sorted_phrases = sorted(phrases, key=len, reverse=True)

    cleaned = text.strip()

    # --- Pass 1: Exact phrase matching (handles known variants) ---
    stripped = False

    # Strip from end first (stop wake word)
    for phrase in sorted_phrases:
        lower = cleaned.lower().rstrip(" .,!?")
        if lower.endswith(phrase):
            idx = len(cleaned.rstrip(" .,!?")) - len(phrase)
            cleaned = cleaned[:idx].rstrip(" .,!?")
            stripped = True
            break

    # Strip from start (start wake word — happens with toggle mode)
    for phrase in sorted_phrases:
        lower = cleaned.lower().lstrip(" .,!?")
        if lower.startswith(phrase):
            cleaned = cleaned[len(phrase):].lstrip(" .,!?")
            stripped = True
            break

    # --- Pass 2: Fuzzy regex fallback (catches novel Whisper variants) ---
    # Matches "Hey <1-2 words>" at start/end that look like wake word attempts.
    # Only runs if the explicit list didn't already catch something.
    if not stripped:
        # Start: "Hey Jarvis/Chavis/Travis/etc." — 1-2 short words after "hey"
        cleaned = re.sub(
            r'^[Hh]ey[,.]?\s+\w{2,8}(\s+\w{2,5})?\s*[.,!?]*\s*',
            '', cleaned, count=1
        ).strip()
        # End: same pattern at the end of the text
        cleaned = re.sub(
            r'\s*[.,!?]*\s*[Hh]ey[,.]?\s+\w{2,8}(\s+\w{2,5})?[.,!?]*\s*$',
            '', cleaned, count=1
        ).strip()

    return cleaned.strip()


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

    # Write recording flag for cross-process coordination
    try:
        open(RECORDING_FLAG, "w").close()
    except Exception:
        pass

    cues_dir = get_cues_dir(config.cues_dir)
    audio_cue("listening", cues_dir)
    _show_recording_indicator(True)
    _hud_send({"type": "state", "state": "listening"})
    log("Recording started. Waiting for stop wake word.")


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
        # Capture PTT flag under lock — _send_local runs on a daemon thread
        # and must not read the global (which could be overwritten by a new recording).
        ptt_snapshot = _triggered_by_ptt

    log("Stopping recording...")
    _show_recording_indicator(False)
    _hud_send({"type": "state", "state": "processing"})

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
                kwargs={"ptt": ptt_snapshot},
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


def _send_local(duration: float, audio_chunks: list, config: HeyvoxConfig, adapter, raw_rms_db: float = 0.0, *, ptt: bool = False) -> None:
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

        # PTT mode: paste into currently focused app — no restore_target needed.
        # The Globe key is suppressed during PTT (ptt.py returns None), so focus
        # stays on whatever app the user was in when they pressed FN.
        # Requirement: INPT-05 (PTT = always-focused regardless of target_mode)
        if ptt:
            log(f"Typing into active app (PTT mode, snapshot was "
                f"{_recording_target.app_name if _recording_target else 'none'})...")
            type_text(paste_text)
            log("Pasted (PTT mode, no auto-Enter)")
        else:
            # Wake word mode: use the adapter's own focusing logic.
            # The adapter knows the correct target (e.g. LastAgentAdapter tracks
            # the last-focused agent). This is more reliable than restore_target
            # which can capture the wrong app if focus shifted before recording.
            log(f"Injecting via {type(adapter).__name__} "
                f"(snapshot was {_recording_target.app_name if _recording_target else 'none'})...")
            adapter.inject_text(paste_text)
            if adapter.should_auto_send():
                time.sleep(1.0)
                target_app = (
                    getattr(adapter, '_last_agent_name', None)
                    or getattr(adapter, '_target_app', None)
                )
                from heyvox.input.injection import press_enter as _press_enter
                log(f"Pressing Enter x{adapter.enter_count} → {target_app or 'frontmost'}...")
                _press_enter(adapter.enter_count, app_name=target_app)
                log("Sent!")
            else:
                log("Pasted (no auto-send)")
        # Show "Sent to [agent]" confirmation in HUD
        target_name = None
        if not ptt:
            target_name = (
                getattr(adapter, '_last_agent_name', None)
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
            old_pid = int(open(_PID_FILE).read().strip())
            os.kill(old_pid, 0)  # Check if alive
            # Verify the process is actually vox (not a recycled PID)
            try:
                result = subprocess.run(
                    ["ps", "-p", str(old_pid), "-o", "command="],
                    capture_output=True, text=True, timeout=2,
                )
                if "vox" not in result.stdout.lower():
                    log(f"PID {old_pid} is not vox (cmd: {result.stdout.strip()}), removing stale PID file")
                    raise ProcessLookupError("not vox")
            except subprocess.TimeoutExpired:
                pass  # Proceed with kill if ps hangs
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
    for pattern in ("/tmp/heyvox-recording", "/tmp/heyvox-media-paused-*", "/tmp/heyvox-hud.sock",
                     "/tmp/claude-tts-mute", "/tmp/herald-mute", "/tmp/heyvox-verbosity"):
        for stale in _glob.glob(pattern):
            try:
                os.unlink(stale)
            except (FileNotFoundError, IsADirectoryError):
                pass

    with open(_PID_FILE, "w") as f:
        f.write(str(os.getpid()))


def _release_singleton():
    """Remove PID file on exit."""
    try:
        if os.path.exists(_PID_FILE):
            pid = int(open(_PID_FILE).read().strip())
            if pid == os.getpid():
                os.unlink(_PID_FILE)
    except (OSError, ValueError):
        pass


def main() -> None:
    """Main event loop — loads config, starts PTT, runs wake word detection."""
    global is_recording, _audio_buffer, busy

    # Load configuration from ~/.config/heyvox/config.yaml (or defaults)
    # Requirement: CONF-01
    config = load_config()
    _init_log(config.log_file, config.log_max_bytes)

    # Singleton: kill any previous instance and write our PID
    _acquire_singleton()
    import atexit
    atexit.register(_release_singleton)

    # Startup cleanup: remove stale flags from previous crash/kill
    for stale_flag in (RECORDING_FLAG, "/tmp/heyvox-tts-playing", "/tmp/claude-tts-playing.pid"):
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
        start_word, stop_word, config.wake_words.models_dir
    )

    # Build text injection adapter based on config.target_mode
    # Requirement: INPT-03
    global _adapter
    _adapter = _build_adapter(config)
    log(f"Target mode: {config.target_mode} (adapter: {type(_adapter).__name__})")

    # Open audio stream
    log("Opening audio stream...")
    pa = pyaudio.PyAudio()
    dev_index = find_best_mic(pa, mic_priority=mic_priority,
                              sample_rate=sample_rate, chunk_size=chunk_size)
    if dev_index is None:
        log("FATAL: No microphone available, exiting")
        sys.exit(1)

    dev_name = pa.get_device_info_by_index(dev_index)['name']
    log(f"Using input: [{dev_index}] {dev_name}")
    stream = open_mic_stream(pa, dev_index, sample_rate=sample_rate, chunk_size=chunk_size)

    # Write active mic name so HUD menu can display it
    def _write_active_mic(name: str) -> None:
        try:
            with open(ACTIVE_MIC_FILE, "w") as f:
                f.write(name)
        except OSError:
            pass

    _write_active_mic(dev_name)

    headset_mode = detect_headset(pa, dev_index)
    log(f"Headset detected: {headset_mode} (echo suppression {'inactive' if headset_mode else 'active'})")

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

    # Silent-mic health check state (AUDIO-08 completion)
    _zero_streak = 0
    HEALTH_CHECK_INTERVAL = 15.0  # Check every 15s (was 30s) for faster dead-mic recovery
    last_health_check = time.time()

    # Device hotplug detection — periodically check if a higher-priority mic appeared
    # Also tracks output device changes for audio feedback. Requirement: AUDIO-11
    _DEVICE_SCAN_INTERVAL = 3.0  # seconds — fast detection for USB/BT hotplug
    _last_device_scan = time.time()
    _last_output_device = ""
    try:
        _default_out = pa.get_default_output_device_info()
        _last_output_device = _default_out['name']
    except Exception:
        pass

    # Memory watchdog — warn if RSS exceeds threshold
    _MEM_WARN_MB = 1500
    _last_mem_check = time.time()
    _MEM_CHECK_INTERVAL = 60.0  # Check every 60s

    try:
        while not _shutdown.is_set():
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
            except IOError as e:
                consecutive_errors += 1
                if consecutive_errors <= 2:
                    log(f"Audio read error ({consecutive_errors}/2): {e}")
                    time.sleep(0.1)
                    continue
                log("Mic appears disconnected, searching for new mic...")
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
                pa.terminate()
                time.sleep(0.5)
                pa = pyaudio.PyAudio()
                dev_index = find_best_mic(pa, mic_priority=mic_priority,
                                          sample_rate=sample_rate, chunk_size=chunk_size)
                if dev_index is None:
                    log("No mic found, retrying in 2s...")
                    time.sleep(2)
                    continue
                dev_name = pa.get_device_info_by_index(dev_index)['name']
                log(f"Switched to: [{dev_index}] {dev_name}")
                stream = open_mic_stream(pa, dev_index, sample_rate=sample_rate, chunk_size=chunk_size)
                _write_active_mic(dev_name)
                device_change_cue(dev_name, "input")
                _hud_send({"type": "state", "text": f"Mic: {dev_name}"})
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
            # Requirement: AUDIO-08
            if not _is_rec and not _is_busy:
                _hud_ensure_connected()
                now = time.time()
                if now - last_health_check >= HEALTH_CHECK_INTERVAL:
                    last_health_check = now
                    level = int(np.abs(audio).max())
                    if level == 0:
                        _zero_streak += 1
                        if _zero_streak >= 2:  # 2 × 15s = 30s to detect (was 3 × 30s = 90s)
                            log("WARNING: Silent mic detected (3 consecutive zero checks), restarting audio session")
                            _zero_streak = 0
                            try:
                                stream.stop_stream()
                                stream.close()
                            except Exception:
                                pass
                            pa.terminate()
                            time.sleep(1)
                            pa = pyaudio.PyAudio()
                            dev_index = find_best_mic(pa, mic_priority=mic_priority,
                                                      sample_rate=sample_rate, chunk_size=chunk_size)
                            if dev_index is None:
                                log("No mic after reinit, retrying in 5s...")
                                time.sleep(5)
                                continue
                            dev_name = pa.get_device_info_by_index(dev_index)['name']
                            log(f"Reinitialized audio: [{dev_index}] {dev_name}")
                            stream = open_mic_stream(pa, dev_index, sample_rate=sample_rate, chunk_size=chunk_size)
                            headset_mode = detect_headset(pa, dev_index)
                            _write_active_mic(dev_name)
                            device_change_cue(dev_name, "input")
                            _hud_send({"type": "state", "text": f"Mic: {dev_name}"})
                            consecutive_errors = 0
                            continue
                    else:
                        _zero_streak = 0

                # Memory watchdog — check RSS every 60s
                _MEM_CRITICAL_MB = 1000
                if now - _last_mem_check >= _MEM_CHECK_INTERVAL:
                    _last_mem_check = now
                    import resource
                    rss_mb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024
                    if rss_mb > _MEM_CRITICAL_MB:
                        log(f"WATCHDOG: Memory critical ({rss_mb:.0f} MB), auto-restarting...")
                        _hud_send({"type": "error", "text": f"Restarting: {rss_mb:.0f}MB"})
                        time.sleep(0.5)
                        os.execv(sys.executable, [sys.executable, "-c", "from heyvox.main import run; run()"])
                    elif rss_mb > _MEM_WARN_MB:
                        log(f"WARNING: Memory usage high: {rss_mb:.0f} MB (threshold: {_MEM_WARN_MB} MB)")
                        _hud_send({"type": "error", "text": f"Memory: {rss_mb:.0f}MB"})

            # Overlay health: relaunch if dead, kill duplicates (piggyback on device scan interval)
            if not _is_rec and not _is_busy and now - _last_device_scan >= _DEVICE_SCAN_INTERVAL:
                if _indicator_proc is not None:
                    if _indicator_proc.poll() is not None:
                        log(f"WARNING: HUD overlay exited (rc={_indicator_proc.returncode}), relaunching")
                        _indicator_proc = None
                        _launch_hud_overlay(menu_bar_only=config.hud_menu_bar_only)
                    else:
                        _kill_duplicate_overlays(keep_pid=_indicator_proc.pid)

            # Device hotplug — check if a higher-priority mic appeared (only when idle)
            if not _is_rec and not _is_busy and now - _last_device_scan >= _DEVICE_SCAN_INTERVAL:
                _last_device_scan = now
                try:
                    # PortAudio caches the device list — create a temporary instance
                    # to discover newly connected devices (e.g. USB/Bluetooth hotplug)
                    _scan_pa = pyaudio.PyAudio()
                    current_count = _scan_pa.get_device_count()
                    current_names = set()
                    for _di in range(current_count):
                        try:
                            _info = _scan_pa.get_device_info_by_index(_di)
                            if _info['maxInputChannels'] > 0:
                                current_names.add(_info['name'])
                        except Exception:
                            pass
                    _scan_pa.terminate()

                    # Check if a higher-priority device is available but not currently selected.
                    # Use tracked dev_name (not pa.get_device_info which may have stale indices).
                    better_available = False
                    for prio_name in mic_priority:
                        matching = [n for n in current_names if prio_name.lower() in n.lower()]
                        if matching:
                            if prio_name.lower() in dev_name.lower():
                                break  # Already using this priority level or higher
                            better_available = True
                            log(f"Higher-priority mic detected: {matching[0]} (current: {dev_name})")
                            break

                    if better_available:
                        try:
                            stream.stop_stream()
                            stream.close()
                        except Exception:
                            pass
                        pa.terminate()
                        pa = pyaudio.PyAudio()
                        dev_index = find_best_mic(pa, mic_priority=mic_priority,
                                                  sample_rate=sample_rate, chunk_size=chunk_size)
                        if dev_index is not None:
                            dev_name = pa.get_device_info_by_index(dev_index)['name']
                            log(f"Switched to: [{dev_index}] {dev_name}")
                            stream = open_mic_stream(pa, dev_index, sample_rate=sample_rate, chunk_size=chunk_size)
                            headset_mode = detect_headset(pa, dev_index)
                            log(f"Headset mode: {headset_mode}")
                            _write_active_mic(dev_name)
                            device_change_cue(dev_name, "input")
                            _hud_send({"type": "state", "text": f"Mic: {dev_name}"})
                    # Check for manual mic switch request from HUD menu
                    if os.path.exists(MIC_SWITCH_REQUEST_FILE):
                        try:
                            with open(MIC_SWITCH_REQUEST_FILE) as f:
                                requested_name = f.read().strip()
                            os.unlink(MIC_SWITCH_REQUEST_FILE)
                            if requested_name:
                                log(f"Mic switch requested from menu: {requested_name}")
                                # Find the requested device in fresh scan results
                                target_index = None
                                for n in current_names:
                                    if requested_name.lower() in n.lower():
                                        # Get index from scan PA
                                        _scan2 = pyaudio.PyAudio()
                                        for _di2 in range(_scan2.get_device_count()):
                                            try:
                                                _d2 = _scan2.get_device_info_by_index(_di2)
                                                if _d2['name'] == n and _d2['maxInputChannels'] > 0:
                                                    target_index = _di2
                                                    break
                                            except Exception:
                                                pass
                                        _scan2.terminate()
                                        break
                                if target_index is not None:
                                    try:
                                        stream.stop_stream()
                                        stream.close()
                                    except Exception:
                                        pass
                                    pa.terminate()
                                    pa = pyaudio.PyAudio()
                                    dev_index = target_index
                                    dev_name = pa.get_device_info_by_index(dev_index)['name']
                                    log(f"Switched to: [{dev_index}] {dev_name}")
                                    stream = open_mic_stream(pa, dev_index, sample_rate=sample_rate, chunk_size=chunk_size)
                                    headset_mode = detect_headset(pa, dev_index)
                                    _write_active_mic(dev_name)
                                    device_change_cue(dev_name, "input")
                                    _hud_send({"type": "state", "text": f"Mic: {dev_name}"})
                                else:
                                    log(f"Requested mic not found: {requested_name}")
                        except Exception as e:
                            log(f"Mic switch request error: {e}")

                    # Also check if the default output device changed (AUDIO-11)
                    try:
                        _cur_out = pa.get_default_output_device_info()
                        _cur_out_name = _cur_out['name']
                        if _cur_out_name != _last_output_device and _last_output_device:
                            log(f"Output device changed: {_last_output_device} → {_cur_out_name}")
                            device_change_cue(_cur_out_name, "output")
                            _hud_send({"type": "state", "text": f"Speaker: {_cur_out_name}"})
                        _last_output_device = _cur_out_name
                    except Exception:
                        pass

                except Exception as e:
                    pass  # Don't crash the main loop on scan errors

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
                continue

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

            # Stop threshold is lower than start — saying the wake word mid-speech
            # produces weaker scores due to overlapping audio energy.
            stop_threshold = threshold * _speaker_mult * 0.7  # e.g. 0.5 * 1.4 * 0.7 = 0.49
            # Cooldown is shorter during recording — repeated stop attempts should
            # get through quickly. Start cooldown stays normal to prevent double-triggers.
            stop_cooldown = min(cooldown, 0.5)

            for ww_name, score in model.prediction_buffer.items():
                s = score[-1]
                active_threshold = stop_threshold if _is_rec else (threshold * _speaker_mult)
                active_cooldown = stop_cooldown if _is_rec else cooldown
                log_threshold = active_threshold * 0.5
                if s > log_threshold:
                    triggered = s > active_threshold
                    log(f"  [{ww_name}] score={s:.3f} (thr={active_threshold:.2f}) {'>>> TRIGGER' if triggered else ''}")
                if s > active_threshold:
                    now = time.time()
                    if now - last_trigger > active_cooldown:
                        last_trigger = now
                        # PTT owns the recording lifecycle — ignore wake words
                        if _triggered_by_ptt and is_recording:
                            pass
                        elif use_separate_words:
                            if start_word in ww_name and not is_recording:
                                start_recording(config=config, preroll=_preroll_buffer)
                            elif stop_word in ww_name and is_recording:
                                stop_recording(config=config)
                        else:
                            if not is_recording:
                                start_recording(config=config, preroll=_preroll_buffer)
                            else:
                                stop_recording(config=config)
                    model.reset()

    except KeyboardInterrupt:
        log("Stopped by user")
    except Exception:
        log(f"FATAL: Unhandled exception in main loop")
        import traceback
        log(traceback.format_exc())
    finally:
        log("Cleaning up...")
        # Always clean up flag files to avoid blocking TTS orchestrator
        for flag in (RECORDING_FLAG, TTS_PLAYING_FLAG):
            try:
                os.unlink(flag)
            except FileNotFoundError:
                pass
        # Clean up media pause flags
        import glob as _glob
        for f in _glob.glob("/tmp/heyvox-media-paused-rec"):
            try:
                os.unlink(f)
            except FileNotFoundError:
                pass
        if _hud_client:
            _hud_client.close()
        _stop_hud_overlay()
        # Shut down native TTS worker cleanly (drains queue + joins thread)
        # Requirement: TTS-03
        if config.tts.enabled:
            _shutdown_tts()
            log("TTS worker stopped")
        try:
            stream.stop_stream()
            stream.close()
        except Exception:
            pass
        pa.terminate()
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
    cues_dir = get_cues_dir(config.cues_dir)
    audio_cue("paused", cues_dir)
    _hud_send({"type": "state", "state": "idle"})
    log("Recording cancelled.")


def run() -> None:
    """CLI entry point — called by vox.cli on 'heyvox start'."""
    main()
