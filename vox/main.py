"""
Vox main event loop.

Orchestrates the wake word listener, push-to-talk, STT transcription,
text injection, and recording indicator. Reads configuration from config.yaml.

Entry point: vox.cli calls run() which calls main().
"""

import os
import sys
import time
import signal
import threading
import subprocess

import numpy as np
import pyaudio
import yaml

from vox.constants import (
    RECORDING_FLAG,
    LOG_FILE,
    DEFAULT_SAMPLE_RATE,
    DEFAULT_CHUNK_SIZE,
)
from vox.audio.mic import find_best_mic, open_mic_stream
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
# Config
# ---------------------------------------------------------------------------

_LOG_FILE = LOG_FILE
_LOG_MAX_BYTES = 1_000_000


def _load_config():
    """Load config.yaml from the current working directory or home dir."""
    candidates = [
        os.path.join(os.getcwd(), "config.yaml"),
        os.path.expanduser("~/.config/vox/config.yaml"),
        os.path.expanduser("~/vox/config.yaml"),
    ]
    for path in candidates:
        if os.path.exists(path):
            with open(path) as f:
                return yaml.safe_load(f)
    log("WARNING: No config.yaml found, using defaults")
    return {}


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def log(msg):
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


def _kill_orphan_indicators():
    """Kill any leftover indicator processes from previous sessions."""
    try:
        subprocess.run(
            ["pkill", "-9", "-f", "vox/hud/overlay.py"],
            capture_output=True, timeout=3,
        )
    except Exception:
        pass


def _show_recording_indicator(active):
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

def start_recording(ptt=False, cfg=None):
    """Begin a recording session.

    Sets is_recording flag, signals TTS to pause, plays listening cue,
    and shows the recording indicator.

    Args:
        ptt: True if triggered by push-to-talk (affects auto-send behavior).
        cfg: Config dict (used to resolve STT backend, superwhisper settings).
    """
    global is_recording, recording_start_time, _audio_buffer, _triggered_by_ptt
    if cfg is None:
        cfg = {}

    with _state_lock:
        if is_recording:
            return
        is_recording = True
        recording_start_time = time.time()
        _audio_buffer = []
        _triggered_by_ptt = ptt

    stt_backend = cfg.get("stt", {}).get("backend", "superwhisper")

    if stt_backend == "superwhisper":
        sw_url = cfg.get("superwhisper", {}).get("toggle_url", "superwhisper://record")
        subprocess.run(["open", "-g", sw_url], capture_output=True, timeout=5)

    # Signal TTS orchestrator to pause while recording
    try:
        open(RECORDING_FLAG, "w").close()
    except Exception:
        pass

    cues_dir = cfg.get("_cues_dir", get_cues_dir())
    audio_cue("listening", cues_dir)
    _show_recording_indicator(True)
    log("Recording started. Waiting for stop wake word.")


def stop_recording(cfg=None):
    """End a recording session and dispatch transcription.

    Checks minimum recording duration, plays feedback cue, and starts
    the transcription thread.

    Args:
        cfg: Config dict.
    """
    global is_recording, busy
    if cfg is None:
        cfg = {}

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

    min_rec = cfg.get("min_recording_secs", 1.5)
    cues_dir = cfg.get("_cues_dir", get_cues_dir())
    stt_backend = cfg.get("stt", {}).get("backend", "superwhisper")

    if duration < min_rec:
        log(f"Recording too short ({duration:.1f}s < {min_rec}s), cancelling")
        audio_cue("paused", cues_dir)
        if stt_backend == "superwhisper":
            sw_url = cfg.get("superwhisper", {}).get("toggle_url", "superwhisper://record")
            subprocess.run(["open", "-g", sw_url], capture_output=True, timeout=5)
        return

    audio_cue("ok", cues_dir)
    with _state_lock:
        busy = True

    if stt_backend == "local":
        # Trim audio cue from wake-word recording (speakers bleed into mic)
        # PTT starts silently — no trim needed
        chunk_size = cfg.get("chunk_size", DEFAULT_CHUNK_SIZE)
        sample_rate = cfg.get("sample_rate", DEFAULT_SAMPLE_RATE)
        if not _triggered_by_ptt:
            cue_trim = int(1.5 * sample_rate / chunk_size)
            recorded_chunks = recorded_chunks[cue_trim:] if len(recorded_chunks) > cue_trim else recorded_chunks
        threading.Thread(target=_send_local, args=(duration, recorded_chunks, cfg), daemon=True).start()
    else:
        threading.Thread(target=_send_superwhisper, args=(duration, cfg), daemon=True).start()


def _send_local(duration, audio_chunks, cfg):
    """Transcribe locally and inject text into target app."""
    global busy

    stt_cfg = cfg.get("stt", {}).get("local", {})
    engine = stt_cfg.get("engine", "mlx")
    mlx_model = stt_cfg.get("mlx_model", "mlx-community/whisper-small-mlx")
    language = stt_cfg.get("language", "")
    sample_rate = cfg.get("sample_rate", DEFAULT_SAMPLE_RATE)
    target_app = cfg.get("target_app", "")
    enter_count = cfg.get("enter_count", 2)
    prefix = cfg.get("transcription_prefix", "")
    cues_dir = cfg.get("_cues_dir", get_cues_dir())
    tts_script = cfg.get("_tts_script_path")

    try:
        log(f"Recording was {duration:.1f}s, transcribing...")
        t0 = time.time()
        text = transcribe_audio(audio_chunks, engine=engine, mlx_model=mlx_model,
                                language=language, sample_rate=sample_rate)
        elapsed = time.time() - t0
        log(f"Transcription ({elapsed:.1f}s): {text[:80]}{'...' if len(text) > 80 else ''}")

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
            execute_voice_command(action_key, feedback, tts_script_path=tts_script, log_fn=log)
            audio_cue("paused", cues_dir)
            return

        paste_text = f"{prefix}{text}" if prefix else text

        if _triggered_by_ptt:
            log("Typing into active app (PTT mode)...")
        else:
            log(f"Focusing {target_app}...")
            focus_app(target_app)
            time.sleep(0.3)

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
            log(f"Pressing Enter x{enter_count}...")
            press_enter(enter_count)
            log("Sent!")
    except subprocess.TimeoutExpired:
        log("WARNING: Subprocess timed out during send phase")
    except Exception as e:
        log(f"ERROR in send phase: {e}")
    finally:
        with _state_lock:
            busy = False
        log("Ready for next wake word.")


def _send_superwhisper(duration, cfg):
    """SuperWhisper flow: wait for clipboard change, then press Enter."""
    global busy
    from vox.input.injection import get_clipboard_text, clipboard_is_image

    sw_cfg = cfg.get("superwhisper", cfg.get("stt", {}).get("superwhisper", {}))
    sw_url = sw_cfg.get("toggle_url", "superwhisper://record")
    sw_max_wait = sw_cfg.get("max_wait_secs", 15)
    target_app = cfg.get("target_app", "")
    enter_count = cfg.get("enter_count", 2)
    cues_dir = cfg.get("_cues_dir", get_cues_dir())

    # Snapshot clipboard before recording started (captured in start_recording)
    clipboard_before = cfg.get("_clipboard_before", "")
    clipboard_was_image = cfg.get("_clipboard_was_image", False)

    try:
        log(f"Recording was {duration:.1f}s")
        log(f"Focusing {target_app}...")
        focus_app(target_app)
        time.sleep(0.3)

        log("Stopping SuperWhisper...")
        subprocess.run(["open", "-g", sw_url], capture_output=True, timeout=5)

        poll = 0.1
        elapsed = 0
        phase = "waiting_for_transcription"
        log("Watching clipboard for paste completion...")

        while elapsed < sw_max_wait:
            time.sleep(poll)
            elapsed += poll
            try:
                clip = get_clipboard_text()
            except subprocess.TimeoutExpired:
                continue

            if phase == "waiting_for_transcription":
                if clip and clip != clipboard_before:
                    log(f"Transcription on clipboard after {elapsed:.1f}s: {clip[:50]}...")
                    phase = "waiting_for_restore"
            elif phase == "waiting_for_restore":
                if clip == clipboard_before or (clipboard_was_image and (clip == "" or clipboard_is_image())):
                    log(f"Clipboard restored after {elapsed:.1f}s - paste complete!")
                    break
        else:
            if phase == "waiting_for_transcription":
                log("WARNING: No transcription appeared on clipboard, aborting")
                return
            else:
                log("Clipboard not restored yet, but transcription was pasted - proceeding")

        time.sleep(0.2)
        log(f"Pressing Enter x{enter_count}...")
        press_enter(enter_count)
        log("Enter sent!")
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

def main():
    """Main event loop — loads config, starts PTT, runs wake word detection."""
    global is_recording, _audio_buffer, busy

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
            cues_dir = cfg.get("_cues_dir", get_cues_dir())
            audio_cue("paused", cues_dir)
            log("Recording cancelled.")

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGUSR1, handle_cancel)

    # Load configuration
    cfg = _load_config()

    # Resolve paths
    script_dir = os.path.dirname(os.path.abspath(__file__))
    cues_dir_path = os.path.join(script_dir, "..", cfg.get("cues_dir", "cues"))
    cues_dir_path = os.path.normpath(cues_dir_path)
    if not os.path.isdir(cues_dir_path):
        cues_dir_path = get_cues_dir()
    cfg["_cues_dir"] = cues_dir_path

    # Inline settings
    wake_cfg = cfg.get("wake_words", {})
    start_word = wake_cfg.get("start", "hey_jarvis_v0.1")
    stop_word = wake_cfg.get("stop", start_word)
    use_separate_words = start_word != stop_word
    threshold = cfg.get("threshold", 0.2)
    cooldown = cfg.get("cooldown_secs", 2.0)
    sample_rate = cfg.get("sample_rate", DEFAULT_SAMPLE_RATE)
    chunk_size = cfg.get("chunk_size", DEFAULT_CHUNK_SIZE)
    mic_priority = cfg.get("mic_priority", ["MacBook Pro Microphone"])
    stt_backend = cfg.get("stt", {}).get("backend", "superwhisper")
    ptt_cfg = cfg.get("push_to_talk", {})
    ptt_enabled = ptt_cfg.get("enabled", False)
    ptt_key = ptt_cfg.get("key", "fn")
    silence_timeout = cfg.get("silence_timeout_secs", 5.0)
    silence_threshold = cfg.get("silence_threshold", 200)

    _kill_orphan_indicators()

    # Initialize STT backend
    log(f"STT backend: {stt_backend}")
    if stt_backend == "local":
        stt_local = cfg.get("stt", {}).get("local", {})
        init_local_stt(
            engine=stt_local.get("engine", "mlx"),
            mlx_model=stt_local.get("mlx_model", "mlx-community/whisper-small-mlx"),
            model_dir=os.path.join(script_dir, stt_local.get("model_dir", "models/sherpa-onnx-whisper-small")),
            language=stt_local.get("language", ""),
            threads=stt_local.get("threads", 4),
            log_fn=log,
        )

    # Start push-to-talk listener if enabled
    if ptt_enabled:
        from vox.input.ptt import start_ptt_listener

        def _ptt_callbacks():
            return {
                "on_start": lambda: start_recording(ptt=True, cfg=cfg),
                "on_stop": lambda: stop_recording(cfg=cfg),
                "on_cancel_transcription": lambda: _cancel_transcription.set(),
                "on_cancel_recording": lambda: _cancel_recording_from_ptt(cfg),
                "is_busy": lambda: busy,
                "is_recording": lambda: is_recording,
            }

        start_ptt_listener(ptt_key, _ptt_callbacks(), log_fn=log)

    # Load wake word models
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

    if use_separate_words:
        log(f"Ready! Say '{start_word}' to start, '{stop_word}' to stop.")
    else:
        log(f"Ready! Say '{start_word}' to start/stop voice input.")

    last_trigger = 0
    consecutive_errors = 0

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
                if _is_rec and stt_backend == "local":
                    _audio_buffer.append(audio.copy())

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
                            audio_cue("paused", cues_dir_path)
                            log("Ready for next wake word.")
                            continue

            if _is_busy:
                continue

            # Suppress wake word detection while audio cue plays
            if is_suppressed():
                continue

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
                                start_recording(cfg=cfg)
                            elif stop_word in ww_name and is_recording:
                                stop_recording(cfg=cfg)
                        else:
                            if not is_recording:
                                start_recording(cfg=cfg)
                            else:
                                stop_recording(cfg=cfg)
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


def _cancel_recording_from_ptt(cfg):
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
    cues_dir = cfg.get("_cues_dir", get_cues_dir())
    audio_cue("paused", cues_dir)
    log("Recording cancelled.")


def run():
    """CLI entry point — called by vox.cli on 'vox start'."""
    main()
