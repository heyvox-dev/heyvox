"""
Vox main event loop.

Orchestrates the wake word listener, push-to-talk, STT transcription,
text injection, and recording indicator. Loads configuration from
~/.config/vox/config.yaml via the pydantic config system.

Entry point: vox.cli calls run() which calls main().

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

from vox.config import load_config, VoxConfig
from vox.constants import (
    RECORDING_FLAG,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_CHUNK_SIZE,
    TTS_PLAYING_FLAG,
    TTS_PLAYING_MAX_AGE_SECS,
)
from vox.audio.mic import find_best_mic, open_mic_stream, detect_headset
from vox.audio.cues import audio_cue, is_suppressed, get_cues_dir
from vox.audio.stt import init_local_stt, transcribe_audio
from vox.audio.tts import check_voice_command, execute_voice_command
from vox.input.injection import type_text, press_enter, focus_app


# ---------------------------------------------------------------------------
# State (protected by _state_lock)
# ---------------------------------------------------------------------------

_state_lock = threading.Lock()
is_recording = False
recording_start_time = 0.0
busy = False
_audio_buffer = []
_triggered_by_ptt = False
_cancel_transcription = threading.Event()
_shutdown = threading.Event()

# ---------------------------------------------------------------------------
# Logging (module-level path set from config at startup)
# ---------------------------------------------------------------------------

_LOG_FILE = "/tmp/vox.log"
_LOG_MAX_BYTES = 1_000_000


def _init_log(log_file: str, log_max_bytes: int) -> None:
    """Set the log file path and rotation limit from config."""
    global _LOG_FILE, _LOG_MAX_BYTES
    _LOG_FILE = log_file
    _LOG_MAX_BYTES = log_max_bytes


def log(msg: str) -> None:
    """Write timestamped message to stdout and log file with rotation."""
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
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


def _kill_orphan_indicators() -> None:
    """Kill any leftover indicator processes from previous sessions."""
    try:
        subprocess.run(
            ["pkill", "-9", "-f", "vox/hud/overlay.py"],
            capture_output=True, timeout=3,
        )
    except Exception:
        pass


def _show_recording_indicator(active: bool) -> None:
    """Show/hide the recording indicator overlay in a separate process."""
    global _indicator_proc
    if active:
        try:
            overlay_script = os.path.join(
                os.path.dirname(os.path.abspath(__file__)), "hud", "overlay.py"
            )
            _indicator_proc = subprocess.Popen(
                [sys.executable, overlay_script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            log(f"WARNING: Could not show indicator: {e}")
    else:
        if _indicator_proc:
            try:
                _indicator_proc.kill()
                _indicator_proc.wait(timeout=2)
            except Exception:
                pass
            _indicator_proc = None


# ---------------------------------------------------------------------------
# Recording flow
# ---------------------------------------------------------------------------

def start_recording(ptt: bool = False, config: VoxConfig = None) -> None:
    """Begin a recording session.

    Sets is_recording flag, signals TTS to pause, plays listening cue,
    and shows the recording indicator.

    Args:
        ptt: True if triggered by push-to-talk (affects auto-send behavior).
        config: VoxConfig instance. Required.
    """
    global is_recording, recording_start_time, _audio_buffer, _triggered_by_ptt
    if config is None:
        return

    with _state_lock:
        if is_recording:
            return
        is_recording = True
        recording_start_time = time.time()
        _audio_buffer = []
        _triggered_by_ptt = ptt

    # Signal TTS orchestrator to pause while recording
    # Requirement: DECP-04
    try:
        open(RECORDING_FLAG, "w").close()
    except Exception:
        pass

    cues_dir = get_cues_dir(config.cues_dir)
    audio_cue("listening", cues_dir)
    _show_recording_indicator(True)
    log("Recording started. Waiting for stop wake word.")


def stop_recording(config: VoxConfig = None) -> None:
    """End a recording session and dispatch transcription.

    Checks minimum recording duration, plays feedback cue, and starts
    the transcription thread.

    Args:
        config: VoxConfig instance. Required.
    """
    global is_recording, busy
    if config is None:
        return

    with _state_lock:
        if not is_recording:
            return
        is_recording = False
        duration = time.time() - recording_start_time
        recorded_chunks = list(_audio_buffer)

    log("Stopping recording...")
    _show_recording_indicator(False)

    # Release TTS orchestrator
    try:
        os.remove(RECORDING_FLAG)
    except FileNotFoundError:
        pass

    cues_dir = get_cues_dir(config.cues_dir)

    if duration < config.min_recording_secs:
        log(f"Recording too short ({duration:.1f}s < {config.min_recording_secs}s), cancelling")
        audio_cue("paused", cues_dir)
        return

    audio_cue("ok", cues_dir)
    with _state_lock:
        busy = True

    if config.stt.backend == "local":
        # Trim audio cue from wake-word recording (speakers bleed into mic)
        # PTT starts silently — no trim needed
        if not _triggered_by_ptt:
            cue_trim = int(1.5 * config.audio.sample_rate / config.audio.chunk_size)
            recorded_chunks = (
                recorded_chunks[cue_trim:] if len(recorded_chunks) > cue_trim else recorded_chunks
            )
        threading.Thread(
            target=_send_local,
            args=(duration, recorded_chunks, config),
            daemon=True,
        ).start()


def _send_local(duration: float, audio_chunks: list, config: VoxConfig) -> None:
    """Transcribe locally and inject text into target app."""
    global busy

    try:
        log(f"Recording was {duration:.1f}s, transcribing...")
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
            tts_script = config.tts.script_path if config.tts.enabled else None
            execute_voice_command(action_key, feedback, tts_script_path=tts_script, log_fn=log)
            audio_cue("paused", cues_dir)
            return

        paste_text = f"{config.transcription_prefix}{text}" if config.transcription_prefix else text

        if _triggered_by_ptt:
            log("Typing into active app (PTT mode)...")
        elif config.target_app:
            # Only focus a specific app if target_app is configured
            # Requirement: DECP-01
            log(f"Focusing {config.target_app}...")
            focus_app(config.target_app)
            time.sleep(0.3)
        else:
            log("Pasting into focused app (target_app not set)...")

        # Re-check cancellation right before typing
        if _cancel_transcription.is_set():
            log("Transcription cancelled by user (Escape)")
            audio_cue("paused", cues_dir)
            _cancel_transcription.clear()
            return

        type_text(paste_text)

        if _triggered_by_ptt:
            log("Pasted (PTT mode, no auto-Enter)")
        else:
            time.sleep(0.2)
            log(f"Pressing Enter x{config.enter_count}...")
            press_enter(config.enter_count)
            log("Sent!")
    except subprocess.TimeoutExpired:
        log("WARNING: Subprocess timed out during send phase")
    except Exception as e:
        log(f"ERROR in send phase: {e}")
    finally:
        with _state_lock:
            busy = False
        log("Ready for next wake word.")


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def main() -> None:
    """Main event loop — loads config, starts PTT, runs wake word detection."""
    global is_recording, _audio_buffer, busy

    # Load configuration from ~/.config/vox/config.yaml (or defaults)
    # Requirement: CONF-01
    config = load_config()
    _init_log(config.log_file, config.log_max_bytes)

    # Signal handlers for clean shutdown
    def handle_signal(signum, frame):
        log(f"Received signal {signum}, shutting down...")
        _shutdown.set()

    def handle_cancel(signum, frame):
        """SIGUSR1 = cancel active recording."""
        global is_recording
        if is_recording:
            log("Received cancel signal (USR1)")
            _show_recording_indicator(False)
            with _state_lock:
                is_recording = False
                _audio_buffer.clear()
            cues_dir = get_cues_dir(config.cues_dir)
            audio_cue("paused", cues_dir)
            log("Recording cancelled.")

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
        from vox.input.ptt import start_ptt_listener

        ptt_callbacks = {
            "on_start": lambda: start_recording(ptt=True, config=config),
            "on_stop": lambda: stop_recording(config=config),
            "on_cancel_transcription": lambda: _cancel_transcription.set(),
            "on_cancel_recording": lambda: _cancel_recording_from_ptt(config),
            "is_busy": lambda: busy,
            "is_recording": lambda: is_recording,
        }
        start_ptt_listener(config.push_to_talk.key, ptt_callbacks, log_fn=log)

    # Load wake word models
    script_dir = os.path.dirname(os.path.abspath(__file__))
    models_dir = os.path.join(script_dir, "training", "models")
    from vox.audio.wakeword import load_models
    model, use_separate_words = load_models(start_word, stop_word, models_dir)

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

    headset_mode = detect_headset(pa, dev_index)
    log(f"Headset detected: {headset_mode} (echo suppression {'inactive' if headset_mode else 'active'})")

    if use_separate_words:
        log(f"Ready! Say '{start_word}' to start, '{stop_word}' to stop.")
    else:
        log(f"Ready! Say '{start_word}' to start/stop voice input.")

    cues_dir = get_cues_dir(config.cues_dir)
    last_trigger = 0.0
    consecutive_errors = 0

    # Silent-mic health check state (AUDIO-08 completion)
    _zero_streak = 0
    HEALTH_CHECK_INTERVAL = 30.0
    last_health_check = time.time()

    try:
        while not _shutdown.is_set():
            try:
                audio = np.frombuffer(
                    stream.read(chunk_size, exception_on_overflow=False),
                    dtype=np.int16,
                )
                consecutive_errors = 0
            except IOError as e:
                consecutive_errors += 1
                if consecutive_errors <= 3:
                    log(f"Audio read error ({consecutive_errors}/3): {e}")
                    time.sleep(0.5)
                    continue
                log("Mic appears disconnected, searching for new mic...")
                try:
                    stream.stop_stream()
                    stream.close()
                except Exception:
                    pass
                pa.terminate()
                time.sleep(2)
                pa = pyaudio.PyAudio()
                dev_index = find_best_mic(pa, mic_priority=mic_priority,
                                          sample_rate=sample_rate, chunk_size=chunk_size)
                if dev_index is None:
                    log("No mic found, retrying in 5s...")
                    time.sleep(5)
                    continue
                dev_name = pa.get_device_info_by_index(dev_index)['name']
                log(f"Switched to: [{dev_index}] {dev_name}")
                stream = open_mic_stream(pa, dev_index, sample_rate=sample_rate, chunk_size=chunk_size)
                consecutive_errors = 0
                continue

            # Buffer audio during recording (for local STT)
            with _state_lock:
                _is_rec = is_recording
                _is_busy = busy
                _is_ptt = _triggered_by_ptt
                if _is_rec and config.stt.backend == "local":
                    _audio_buffer.append(audio.copy())

            # Proactive silent-mic health check (catches A2DP bad-state without IOError)
            # Requirement: AUDIO-08
            if not _is_rec and not _is_busy:
                now = time.time()
                if now - last_health_check >= HEALTH_CHECK_INTERVAL:
                    last_health_check = now
                    level = int(np.abs(audio).max())
                    if level == 0:
                        _zero_streak += 1
                        if _zero_streak >= 3:
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
                            consecutive_errors = 0
                            continue
                    else:
                        _zero_streak = 0

            # Silence watchdog — cancel if no speech for silence_timeout
            # Only for wake-word triggered recordings (PTT has natural release)
            if _is_rec and not _is_ptt and silence_timeout > 0:
                elapsed = time.time() - recording_start_time
                if elapsed > silence_timeout:
                    with _state_lock:
                        recent_chunks = _audio_buffer[-int(silence_timeout * sample_rate / chunk_size):]
                    if recent_chunks:
                        max_recent = max(int(np.abs(c).max()) for c in recent_chunks)
                        if max_recent < silence_threshold:
                            log(f"Silence timeout ({silence_timeout}s, max_level={max_recent}), cancelling")
                            _show_recording_indicator(False)
                            try:
                                os.remove(RECORDING_FLAG)
                            except FileNotFoundError:
                                pass
                            with _state_lock:
                                is_recording = False
                                _audio_buffer.clear()
                            audio_cue("paused", cues_dir)
                            log("Ready for next wake word.")
                            continue

            if _is_busy:
                continue

            # Suppress wake word detection while audio cue plays
            if is_suppressed():
                continue

            # Echo suppression: skip wake word while TTS is playing in speaker mode
            # Requirement: AUDIO-09, AUDIO-10
            if not headset_mode and config.echo_suppression.enabled:
                if os.path.exists(TTS_PLAYING_FLAG):
                    try:
                        flag_age = time.time() - os.path.getmtime(TTS_PLAYING_FLAG)
                        if flag_age < TTS_PLAYING_MAX_AGE_SECS:
                            continue  # Suppress wake word during TTS playback
                    except OSError:
                        pass  # Flag removed between exists() and getmtime()

            model.predict(audio)

            for ww_name, score in model.prediction_buffer.items():
                s = score[-1]
                if s > threshold * 0.5:
                    log(f"  [{ww_name}] score={s:.3f} {'>>> TRIGGER' if s > threshold else ''}")
                if s > threshold:
                    now = time.time()
                    if now - last_trigger > cooldown:
                        last_trigger = now
                        if use_separate_words:
                            if start_word in ww_name and not is_recording:
                                start_recording(config=config)
                            elif stop_word in ww_name and is_recording:
                                stop_recording(config=config)
                        else:
                            if not is_recording:
                                start_recording(config=config)
                            else:
                                stop_recording(config=config)
                    model.reset()

    except KeyboardInterrupt:
        log("Stopped by user")
    finally:
        log("Cleaning up...")
        _show_recording_indicator(False)
        try:
            stream.stop_stream()
            stream.close()
        except Exception:
            pass
        pa.terminate()
        log("Shutdown complete.")


def _cancel_recording_from_ptt(config: VoxConfig) -> None:
    """Cancel an active recording (used by PTT Escape handler)."""
    global is_recording
    _show_recording_indicator(False)
    try:
        os.remove(RECORDING_FLAG)
    except FileNotFoundError:
        pass
    with _state_lock:
        is_recording = False
        _audio_buffer.clear()
    cues_dir = get_cues_dir(config.cues_dir)
    audio_cue("paused", cues_dir)
    log("Recording cancelled.")


def run() -> None:
    """CLI entry point — called by vox.cli on 'vox start'."""
    main()
