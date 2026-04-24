"""
Recording state machine for heyvox.

Manages start/stop/send_local with explicit thread-safe state.

Requirements: DECOMP-01, DECOMP-04
"""
import os
import sys
import time
import threading
from typing import TYPE_CHECKING

import numpy as np

from heyvox.text_processing import is_garbled, strip_wake_words
from heyvox.constants import RECORDING_FLAG, STT_DEBUG_DIR, TTS_PLAYING_FLAG

if TYPE_CHECKING:
    from heyvox.app_context import AppContext
    from heyvox.config import HeyvoxConfig

# Minimum audio energy (dBFS) to proceed with STT. Recordings below this
# threshold are treated as silence — skips Whisper to avoid hallucinations.
# Normal speech is -30 to -42 dBFS. False triggers on background noise are
# typically -48 to -55 dBFS. Set to -48 to catch those while allowing quiet speech.
_MIN_AUDIO_DBFS = -48.0


def _audio_rms(chunks: list, sample_rate: int) -> float:
    """Compute RMS energy of recorded audio chunks in dBFS.

    Returns a negative value (0 dBFS = max, -96 dBFS ~ silence for 16-bit).
    Used to skip STT on silent/near-silent recordings that would cause
    Whisper to hallucinate ("Thank you for watching", etc.).
    """
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


def _save_debug_audio(
    label: str,
    chunks: list,
    sample_rate: int,
    extra_info: dict | None = None,
    log_fn=None,
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

        # Convert numpy types to Python natives for JSON serialization
        def _jsonable(v):
            if isinstance(v, (np.integer,)):
                return int(v)
            if isinstance(v, (np.floating,)):
                return float(v)
            return v

        info = {k: _jsonable(v) for k, v in info.items()}
        with open(STT_DEBUG_LOG, "a") as f:
            f.write(json.dumps(info) + "\n")

        return filepath
    except Exception as e:
        if log_fn:
            log_fn(f"DEBUG: Failed to save audio: {e}")
        return None


def _release_recording_guard() -> None:
    """Release the recording guard — both in-process event and cross-process file flag.

    Called after STT->paste completes (or on early exit) so the TTS hook
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
    try:
        from heyvox.ipc import update_state
        update_state({"recording": False})
    except Exception:
        pass


class RecordingStateMachine:
    """Encapsulates the recording pipeline: start, stop, transcribe, inject.

    All recording state is accessed via self.ctx (AppContext). No module-level
    globals are used — thread safety is achieved through ctx.lock.

    Args:
        ctx: AppContext instance — holds all shared mutable state.
        config: HeyvoxConfig instance.
        log_fn: Callable[[str], None] — the main.py log() function.
        hud_send: Callable[[dict], None] — sends a message to the HUD overlay.

    Requirements: DECOMP-01, DECOMP-04
    """

    # Constants
    _INJECT_DEDUP_SECS = 2.0    # Suppress duplicate injections within this window
    _BUSY_TIMEOUT = 60.0        # Force-reset busy after this many seconds
    _ZOMBIE_FAIL_THRESHOLD = 2  # Force reinit after N consecutive failed recordings

    def __init__(self, ctx: "AppContext", config: "HeyvoxConfig", log_fn, hud_send) -> None:
        self.ctx = ctx
        self.config = config
        self._log = log_fn
        self._hud_send = hud_send
        self.training_collector = None  # Set by main.py when collect_negatives is enabled

    def start(self, ptt: bool = False, preroll=None) -> None:
        """Begin a recording session.

        Sets is_recording flag, signals TTS to pause, plays listening cue,
        and shows the recording indicator.

        Args:
            ptt: True if triggered by push-to-talk (affects auto-send behavior).
            preroll: Iterable of audio chunks captured before the wake word trigger.
                Prepended to the audio buffer so the first words aren't clipped.
        """
        if self.config is None:
            return
        if self.ctx.shutdown.is_set():
            return  # Don't start recording during shutdown

        # AUDIO-13: Don't start recording on a known-dead mic stream.
        # The main loop will pick up the flag and reinit before we get here again.
        if self.ctx.zombie_mic_reinit:
            self._log("start_recording blocked: zombie mic reinit pending, skipping")
            self._hud_send({"type": "error", "text": "Mic reinitializing..."})
            return

        with self.ctx.lock:
            if self.ctx.is_recording:
                return
            self.ctx.is_recording = True
            self.ctx.recording_start_time = time.time()
            # Pre-roll: prepend recent audio so first words aren't clipped
            self.ctx.audio_buffer = list(preroll) if preroll else []
            self.ctx.triggered_by_ptt = ptt
            self.ctx.recording_target = None  # Will be filled by background snapshot
            # DEF-078: Seed tts-during-recording flag from the current TTS flag
            # state. If Herald is mid-speech when the recording starts, the
            # first ~100-500 ms of audio almost certainly contains speaker
            # bleed. filter_tts_echo() uses this in aggressive mode.
            self.ctx.tts_seen_during_recording = os.path.exists(TTS_PLAYING_FLAG)
            # DEF-084: Reset cancel_transcription at recording boundary so a
            # stale Escape-set flag from a prior STT (e.g. one that took the
            # garbled / empty-stt / voice-command early-return path and didn't
            # clear it) can't spuriously cancel this recording's injection.
            if self.ctx.cancel_transcription.is_set():
                self._log(
                    "CANCEL_LEAK: cancel_transcription was still set at start() — "
                    "clearing (DEF-084)"
                )
            self.ctx.cancel_transcription.clear()

        # === Instant feedback FIRST — before any blocking work ===
        from heyvox.audio.cues import audio_cue, get_cues_dir
        cues_dir = get_cues_dir(self.config.cues_dir)
        audio_cue("listening", cues_dir)
        self._hud_send({"type": "state", "state": "listening"})
        self._log("Recording started. Waiting for stop wake word.")

        # Preload STT model in background while user speaks — hides the ~1s
        # model load latency behind recording time. No-op if already loaded.
        if self.config.stt.backend == "local":
            from heyvox.audio.stt import preload_model
            preload_model()

        # Signal Herald to pause TTS during recording (TTS-03, DECP-04)
        # Herald stops current playback and holds new items until resume.
        try:
            from heyvox.audio.tts import set_recording as _tts_set_rec
            _tts_set_rec(True)
        except ImportError:
            pass

        # Capture target lock in background thread — AX tree walk can take
        # 5-10s for Conductor workspace detection, and we must not block
        # the "listening" feedback for that.
        def _bg_snapshot():
            try:
                from heyvox.input.target import capture_lock
                snap = capture_lock(config=self.config)
                with self.ctx.lock:
                    self.ctx.recording_target = snap
                if snap:
                    ws_info = (
                        f", conductor_ws={snap.conductor_workspace_id!r}, "
                        f"conductor_sess={snap.conductor_session_id!r}"
                        if snap.conductor_workspace_id else ""
                    )
                    self._log(
                        f"[lock] app={snap.app_name}, "
                        f"pid={snap.app_pid}, "
                        f"window_number={snap.window_number}, "
                        f"leaf_role={snap.leaf_role}, "
                        f"text_field={snap.focused_was_text_field}{ws_info}"
                    )
                else:
                    self._log("[lock] WARNING: no target lock (AppKit unavailable?)")
            except Exception as e:
                self._log(f"[lock] ERROR: {e}")
        threading.Thread(target=_bg_snapshot, daemon=True, name="vox-snapshot").start()

        # Pause browser/native media during recording (YouTube, Spotify, etc.)
        # Run in background thread — pause_media() can block for seconds on
        # osascript calls (Chrome JS access test), which would delay recording start.
        def _bg_pause():
            try:
                from heyvox.audio.media import pause_media
                pause_media()
            except Exception as e:
                self._log(f"WARNING: media pause failed: {e}")
        threading.Thread(target=_bg_pause, daemon=True, name="vox-media-pause").start()

        # Write recording flag for cross-process coordination
        try:
            with open(RECORDING_FLAG, "w"):
                pass
        except Exception:
            pass
        try:
            from heyvox.ipc import update_state
            update_state({"recording": True})
        except Exception:
            pass
        try:
            print(
                f"[recording] Started, target="
                f"{self.ctx.recording_target.app_name if self.ctx.recording_target else 'None'}",
                file=sys.stderr,
            )
        except (BrokenPipeError, OSError):
            pass

    def stop(self) -> None:
        """End a recording session and dispatch transcription.

        Checks minimum recording duration, plays feedback cue, and starts
        the transcription thread.
        """
        if self.config is None:
            return

        with self.ctx.lock:
            if not self.ctx.is_recording:
                return
            self.ctx.is_recording = False
            # Set busy immediately to prevent re-entry from other triggers
            self.ctx.busy = True
            duration = time.time() - self.ctx.recording_start_time
            recorded_chunks = list(self.ctx.audio_buffer)
            # Capture PTT flag and recording target under lock — _send_local runs on
            # a daemon thread and must not read ctx fields that could be overwritten.
            ptt_snapshot = self.ctx.triggered_by_ptt
            target_snapshot = self.ctx.recording_target

        # If background snapshot hasn't finished yet, wait briefly (usually <1s)
        if target_snapshot is None:
            for _ in range(50):  # 50 * 0.1s = 5s max
                time.sleep(0.1)
                with self.ctx.lock:
                    target_snapshot = self.ctx.recording_target
                if target_snapshot is not None:
                    break

        _stop_t0 = time.time()
        self._log("Stopping recording...")
        self._hud_send({"type": "state", "state": "processing"})

        # Zombie stream detection: track consecutive failed recordings (AUDIO-12)
        if len(recorded_chunks) == 0:
            self.ctx.consecutive_failed_recordings += 1
            self._log(
                f"WARNING: Recording produced 0 chunks "
                f"(consecutive failures: {self.ctx.consecutive_failed_recordings})"
            )
            if self.ctx.consecutive_failed_recordings >= self._ZOMBIE_FAIL_THRESHOLD:
                self._log(
                    f"WARNING: {self._ZOMBIE_FAIL_THRESHOLD} consecutive empty recordings "
                    "-- flagging zombie stream for reinit"
                )
                self.ctx.zombie_mic_reinit = True
                self.ctx.consecutive_failed_recordings = 0
        else:
            self.ctx.consecutive_failed_recordings = 0

        # NOTE: Recording flag (RECORDING_FLAG) stays set through the STT->paste pipeline.
        # It is released in _send_local's finally block (or in the early-exit paths below).
        # This prevents the TTS hook from firing and stealing focus while we're
        # still transcribing/pasting.

        from heyvox.audio.cues import audio_cue, get_cues_dir
        cues_dir = get_cues_dir(self.config.cues_dir)

        if duration < self.config.min_recording_secs:
            self._log(
                f"Recording too short ({duration:.1f}s < {self.config.min_recording_secs}s), cancelling"
            )
            _release_recording_guard()
            with self.ctx.lock:
                self.ctx.busy = False
            try:
                from heyvox.audio.media import resume_media
                resume_media()
            except Exception:
                pass
            audio_cue("paused", cues_dir)
            self._hud_send({"type": "state", "state": "idle"})
            return

        if not ptt_snapshot:
            audio_cue("ok", cues_dir)

        try:
            if self.config.stt.backend == "local":
                # Compute energy on raw audio BEFORE trimming (wake word is loud,
                # removing it would make the remaining audio seem quieter)
                raw_rms_db = _audio_rms(recorded_chunks, self.config.audio.sample_rate)

                # Save raw audio BEFORE any trimming (for debug analysis).
                # DEF-081: capture the path so the garbled-filter branch can
                # surface a recovery hint if the transcription is discarded.
                _last_raw_wav = _save_debug_audio("raw", recorded_chunks, self.config.audio.sample_rate, {
                    "ptt": ptt_snapshot,
                    "raw_rms_dbfs": round(raw_rms_db, 1),
                }, log_fn=self._log)

                if not ptt_snapshot:
                    # Wake word audio trim -- remove wake word from both ends so
                    # Whisper never sees it. This is the primary defense; the text-level
                    # strip_wake_words() is a fallback for imperfect trims.
                    #
                    # Start trim: ~1.5s covers pre-roll buffer (500ms) + wake word (~1000ms).
                    # End trim: 0.5s -- conservative, only cuts actual stop wake word.
                    ww_start_trim_secs = 1.5
                    ww_end_trim_secs = 0.5
                    start_trim_chunks = int(
                        ww_start_trim_secs * self.config.audio.sample_rate / self.config.audio.chunk_size
                    )
                    end_trim_chunks = int(
                        ww_end_trim_secs * self.config.audio.sample_rate / self.config.audio.chunk_size
                    )

                    pre_trim_count = len(recorded_chunks)

                    # Trim start wake word + cue bleed from front
                    if len(recorded_chunks) > start_trim_chunks + end_trim_chunks:
                        recorded_chunks = recorded_chunks[start_trim_chunks:]
                    # Trim stop wake word from end (only if recording is long enough)
                    if end_trim_chunks > 0 and len(recorded_chunks) > end_trim_chunks:
                        recorded_chunks = recorded_chunks[:-end_trim_chunks]

                    self._log(
                        f"Audio trim: {pre_trim_count} chunks -> {len(recorded_chunks)} "
                        f"(start={start_trim_chunks}, end={end_trim_chunks})"
                    )

                    # After trimming, check if enough audio remains for meaningful
                    # transcription. Very short post-trim audio causes Whisper to
                    # hallucinate ("Thank you", "Thanks for watching", etc.)
                    _post_trim_secs = len(recorded_chunks) * self.config.audio.chunk_size / self.config.audio.sample_rate
                    if _post_trim_secs < 0.8:
                        self._log(
                            f"Post-trim audio too short ({_post_trim_secs:.1f}s), "
                            f"cancelling (Whisper hallucination risk)"
                        )
                        # Training: trigger fired but recording has no real content → FP.
                        if self.training_collector:
                            if self.training_collector.reclassify_tp_start_as_fp(
                                "post-trim-short"
                            ):
                                self._log(
                                    "Training: reclassified tp_start → FP "
                                    "(post-trim-short)"
                                )
                        _release_recording_guard()
                        with self.ctx.lock:
                            self.ctx.busy = False
                        try:
                            from heyvox.audio.media import resume_media
                            resume_media()
                        except Exception:
                            pass
                        audio_cue("paused", cues_dir)
                        self._hud_send({"type": "state", "state": "idle"})
                        return

                    # Save trimmed audio for comparison
                    _save_debug_audio(
                        "trimmed", recorded_chunks, self.config.audio.sample_rate,
                        log_fn=self._log,
                    )

                # _send_local has its own finally block that resets busy = False
                threading.Thread(
                    target=self._send_local,
                    args=(duration, recorded_chunks, raw_rms_db),
                    kwargs={"ptt": ptt_snapshot, "recording_target": target_snapshot,
                            "stop_time": _stop_t0},
                    daemon=True,
                ).start()
        except Exception as e:
            self._log(f"ERROR starting transcription: {e}")
            _release_recording_guard()
            with self.ctx.lock:
                self.ctx.busy = False
            self._hud_send({"type": "state", "state": "idle"})

    def cancel(self) -> None:
        """Cancel the current recording session.

        Clears recording state, releases the recording guard, resumes media,
        and sends idle HUD state. Used by PTT cancel and SIGUSR1 signal handler.
        """
        _release_recording_guard()
        with self.ctx.lock:
            self.ctx.is_recording = False
            self.ctx.busy = False
            self.ctx.audio_buffer.clear()
        try:
            from heyvox.audio.media import resume_media
            resume_media()
        except Exception:
            pass
        if self.config is not None:
            from heyvox.audio.cues import audio_cue, get_cues_dir
            cues_dir = get_cues_dir(self.config.cues_dir)
            audio_cue("paused", cues_dir)
        self._hud_send({"type": "state", "state": "idle"})
        self._log("Recording cancelled.")

    def _send_local(
        self,
        duration: float,
        audio_chunks: list,
        raw_rms_db: float = 0.0,
        *,
        ptt: bool = False,
        recording_target=None,
        stop_time: float = 0.0,
    ) -> None:
        """Transcribe locally and inject text into target app."""
        import subprocess as _subprocess
        from heyvox.audio.stt import transcribe_audio
        from heyvox.audio.cues import audio_cue, get_cues_dir
        from heyvox.input.injection import (
            type_text, save_frontmost_pid, _settle_delay_for, app_fast_paste,
            _set_clipboard,
        )
        from heyvox.input.target import resolve_lock, PasteOutcome, FailReason
        from heyvox.input.toast import show_failure_toast
        from heyvox.audio.tts import check_voice_command, execute_voice_command

        try:
            # Energy gate: skip STT on silent recordings to avoid Whisper hallucinations.
            # Uses raw_rms_db computed BEFORE wake word trim (wake word is the loudest part).
            if raw_rms_db < _MIN_AUDIO_DBFS:
                self._log(
                    f"Recording too quiet ({raw_rms_db:.1f} dBFS < {_MIN_AUDIO_DBFS} dBFS), skipping STT"
                )
                # Training: wake fired but mic captured only noise → FP.
                if self.training_collector:
                    if self.training_collector.reclassify_tp_start_as_fp(
                        "low-energy"
                    ):
                        self._log(
                            "Training: reclassified tp_start → FP (low-energy)"
                        )
                    # Also save the noise tail with FP label for training.
                    self.training_collector.save_fp(
                        audio_chunks, self.config.audio.sample_rate,
                        reason="low-energy",
                    )
                cues_dir = get_cues_dir(self.config.cues_dir)
                audio_cue("paused", cues_dir)
                return

            _t_stt_start = time.time()
            if stop_time:
                self._log(f"[TIMING] stop→STT start: {_t_stt_start - stop_time:.2f}s")
            self._log(f"Recording was {duration:.1f}s ({raw_rms_db:.1f} dBFS), transcribing...")
            try:
                print(f"[recording] Transcribing {duration:.1f}s audio...", file=sys.stderr)
            except (BrokenPipeError, OSError):
                pass
            t0 = time.time()
            text = transcribe_audio(
                audio_chunks,
                engine=self.config.stt.local.engine,
                mlx_model=self.config.stt.local.mlx_model,
                language=self.config.stt.local.language,
                sample_rate=self.config.audio.sample_rate,
            )
            elapsed = time.time() - t0
            # Snapshot audio tail for training data before clearing
            _training_chunks = list(audio_chunks) if self.training_collector else []
            _training_sr = self.config.audio.sample_rate
            # Free audio chunks immediately — no longer needed after transcription
            audio_chunks.clear()

            # Post-STT memory check: if MLX Whisper ballooned, force unload now
            try:
                import psutil
                _rss_mb = psutil.Process().memory_info().rss / 1024 / 1024
                if _rss_mb > 1500:
                    self._log(
                        f"WARNING: RSS {_rss_mb:.0f}MB after STT, "
                        "force-unloading MLX model"
                    )
                    from heyvox.audio.stt import _unload_mlx_model
                    _unload_mlx_model()
            except (ImportError, Exception) as e:
                self._log(f"Post-STT memory check error: {e}")
            _t_stt_done = time.time()
            if stop_time:
                self._log(f"[TIMING] stop→STT done: {_t_stt_done - stop_time:.2f}s (STT={elapsed:.1f}s)")
            self._log(
                f"Transcription ({elapsed:.1f}s): {text[:80]}{'...' if len(text) > 80 else ''}"
            )

            # Log raw STT output for debug
            _save_debug_audio("_stt_result", [], self.config.audio.sample_rate, {
                "stt_raw": text[:200],
                "stt_engine": self.config.stt.local.engine,
                "stt_time_s": round(elapsed, 2),
            }, log_fn=self._log)

            # ECHO-03: Filter TTS echo from transcription (speaker mode protection).
            # If the STT output matches recently spoken TTS text, it's echo, not the user.
            # DEF-078: When TTS_PLAYING_FLAG was observed during the recording
            # window, bleed is almost certain — escalate to aggressive mode
            # (overlap threshold 0.4 instead of 0.6).
            echo_filtered = False
            if text and self.config.echo_suppression.stt_echo_filter:
                try:
                    from heyvox.audio.echo import filter_tts_echo
                    aggressive = bool(getattr(self.ctx, "tts_seen_during_recording", False))
                    filtered = filter_tts_echo(text, aggressive=aggressive)
                    if filtered != text:
                        mode = " (aggressive)" if aggressive else ""
                        self._log(f"ECHO-03{mode}: Stripped TTS echo from transcription (was: {text[:60]})")
                        echo_filtered = True
                        text = filtered
                except Exception:
                    pass
            # Reset the flag so the next recording starts clean.
            try:
                self.ctx.tts_seen_during_recording = False
            except Exception:
                pass

            # Quality filter: discard garbled/nonsensical STT output.
            # DEF-076 + DEF-081: surface the discard to the user with a HUD
            # event and point at the raw WAV so the transcription is
            # recoverable by re-running through MLX.
            # DEF-083: pass STT elapsed + audio duration so the detector can
            # catch hallucinations that slip past text-level checks when
            # Whisper's temperature-fallback loop fires (abnormally slow STT).
            if text and is_garbled(text, stt_secs=elapsed, audio_secs=duration):
                self._log(
                    f"FILTER (garbled, stt={elapsed:.1f}s): Discarding transcription: {text[:80]}"
                )
                try:
                    if _last_raw_wav:
                        self._log(f"FILTER (garbled): raw audio preserved at {_last_raw_wav}")
                except NameError:
                    pass
                self._hud_send({
                    "type": "transcript",
                    "text": f"Garbled STT ({elapsed:.1f}s) - try again",
                })
                # Training: save as false positive (trigger led to garbled output)
                if self.training_collector and _training_chunks:
                    self.training_collector.save_fp(_training_chunks, _training_sr, reason="garbled")
                cues_dir = get_cues_dir(self.config.cues_dir)
                audio_cue("paused", cues_dir)
                return

            self._hud_send({"type": "transcript", "text": text})

            # Persist transcript BEFORE paste attempt -- guarantees no text is ever lost
            if text and text.strip():
                try:
                    from heyvox.history import save as _save_transcript
                    _save_transcript(text, duration=duration, ptt=ptt)
                except Exception as e:
                    self._log(f"WARNING: Failed to save transcript to history: {e}")

            cues_dir = get_cues_dir(self.config.cues_dir)

            if not text:
                self._log("WARNING: Empty transcription, skipping")
                # Training: STT returned nothing from a triggered recording → FP.
                if self.training_collector:
                    if self.training_collector.reclassify_tp_start_as_fp(
                        "empty-stt"
                    ):
                        self._log(
                            "Training: reclassified tp_start → FP (empty-stt)"
                        )
                    if _training_chunks:
                        self.training_collector.save_fp(
                            _training_chunks, _training_sr, reason="empty-stt"
                        )
                audio_cue("paused", cues_dir)
                return

            # Check if cancelled during transcription
            if self.ctx.cancel_transcription.is_set():
                self._log("Transcription cancelled by user (Escape)")
                # Training: user explicitly cancelled → likely FP (trigger was wrong).
                if self.training_collector:
                    if self.training_collector.reclassify_tp_start_as_fp(
                        "user-cancelled"
                    ):
                        self._log(
                            "Training: reclassified tp_start → FP (user-cancelled)"
                        )
                    if _training_chunks:
                        self.training_collector.save_fp(
                            _training_chunks, _training_sr,
                            reason="user-cancelled",
                        )
                audio_cue("paused", cues_dir)
                self.ctx.cancel_transcription.clear()
                return

            # Check for voice commands
            cmd_result = check_voice_command(text)
            if cmd_result:
                action_key, feedback = cmd_result
                self._log(f"Voice command: {action_key} ({feedback})")

                # Dispatch to native TTS engine for skip/stop/mute when enabled
                # Requirement: TTS-03
                _handled_natively = False
                if self.config.tts.enabled:
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
                    tts_script = self.config.tts.script_path if self.config.tts.enabled else None
                    execute_voice_command(
                        action_key, feedback, tts_script_path=tts_script, log_fn=self._log
                    )

                audio_cue("paused", cues_dir)
                return

            # Strip wake word phrases from transcription (start and end)
            pre_strip = text
            text = strip_wake_words(text, self.config.wake_words.start, self.config.wake_words.stop)
            _wake_word_stripped = text != pre_strip
            if _wake_word_stripped:
                self._log(f"Wake word strip: '{pre_strip[:80]}' -> '{text[:80]}'")
                # Training: STT found wake word in recording tail → model missed it (FN-stop)
                if self.training_collector and _training_chunks:
                    self.training_collector.save_fn_stop(_training_chunks, _training_sr)
            elif self.training_collector and _training_chunks and text and text.strip():
                # Clean transcription with no wake word remnants → confirmed TP-stop
                self.training_collector.save_tp_stop(_training_chunks, _training_sr)

            # Final debug log entry with full pipeline result
            _save_debug_audio("_final", [], self.config.audio.sample_rate, {
                "stt_raw": pre_strip[:200],
                "echo_filtered": echo_filtered,
                "wake_word_stripped": text != pre_strip,
                "final_text": text[:200],
            }, log_fn=self._log)

            paste_text = (
                f"{self.config.transcription_prefix}{text}"
                if self.config.transcription_prefix
                else text
            )

            # Re-check cancellation right before typing
            if self.ctx.cancel_transcription.is_set():
                self._log("Transcription cancelled by user (Escape)")
                audio_cue("paused", cues_dir)
                self.ctx.cancel_transcription.clear()
                return

            # Dedup guard: prevent multiple injections from concurrent _send_local threads
            with self.ctx.inject_lock:
                now = time.time()
                if now - self.ctx.last_inject_time < self._INJECT_DEDUP_SECS:
                    self._log(
                        f"WARNING: Duplicate injection suppressed "
                        f"({now - self.ctx.last_inject_time:.1f}s since last)"
                    )
                    return
                self.ctx.last_inject_time = now

            _t_inject_start = time.time()
            if stop_time:
                self._log(f"[TIMING] stop→inject start: {_t_inject_start - stop_time:.2f}s")
            target_app = recording_target.app_name if recording_target else None
            window_number = recording_target.window_number if recording_target else 0
            self._log(
                f"[inject] target_app={target_app}, window_number={window_number}, "
                f"mode={'PTT' if ptt else 'wake word'}, "
                f"text={len(paste_text)} chars: {paste_text[:60]!r}"
            )
            try:
                print(
                    f"[recording] Injecting -> {target_app or 'frontmost'} "
                    f"(window_number={window_number})",
                    file=sys.stderr,
                )
            except (BrokenPipeError, OSError):
                pass

            # Save the user's current focus so we can restore it if injection
            # steals focus from the SAME app they're already in.
            pre_inject_pid = save_frontmost_pid()
            target_pid = recording_target.app_pid if recording_target else 0
            self._log(
                f"[inject] saved pre-inject frontmost pid={pre_inject_pid}, "
                f"target pid={target_pid}"
            )

            # --- 15-05: resolve_lock + tier-aware paste + fail-closed branch ---
            # DEF-070 PRESERVED: this paste-time workspace+session switch fires
            # AFTER recording stopped. The orchestrator's RECORDING_FLAG check
            # that prevents Herald-driven switches DURING recording is in
            # heyvox/herald/orchestrator.py and is NOT touched by this path.
            # The conductor-switch-workspace script itself does NOT consult
            # RECORDING_FLAG (verified Plan 15-05 Task 0).
            paste_ok = False
            outcome = None  # W6: explicit init so later consumers can test
                            # `outcome is not None` safely (e.g. Plan 15-06
                            # verify_paste gating).
            combined_enter = 0

            adapter = self.ctx.adapter
            auto_send = not ptt and adapter.should_auto_send()

            # Look up app profile for enter_count and enter_delay overrides.
            # Profile values take precedence over adapter defaults.
            profile = self.config.get_app_profile(target_app) if target_app else None
            if auto_send:
                combined_enter = profile.enter_count if profile else adapter.enter_count
            else:
                combined_enter = 0
            enter_delay = profile.enter_delay if profile else 0.05

            if recording_target is None:
                self._log("[inject] WARNING: no recording_target — skipping paste")
            else:
                outcome = resolve_lock(recording_target, config=self.config)
                self._log(
                    f"[PASTE] outcome ok={outcome.ok} tier_used={outcome.tier_used} "
                    f"reason={outcome.reason.value if outcome.reason else 'n/a'} "
                    f"elapsed_ms={outcome.elapsed_ms}"
                )

                if outcome.ok:
                    # Tier 1: refocus succeeded; tier 2: profile shortcut
                    # focused the input. Either way, paste via app_fast_paste
                    # if profile has a focus_shortcut (R8 — Phase 12 fast-path);
                    # else fall back to type_text.
                    if profile and profile.focus_shortcut:
                        # R8: first caller of app_fast_paste (landed orphaned
                        # in Plan 15-03).
                        paste_ok = app_fast_paste(profile, paste_text)
                    else:
                        injection_cfg = getattr(self.config, "injection", None)
                        if injection_cfg:
                            settle = _settle_delay_for(
                                target_app, injection_cfg.app_delays,
                                injection_cfg.focus_settle_secs,
                            )
                            max_retries = injection_cfg.max_retries
                        else:
                            settle = 0.1
                            max_retries = 2
                        paste_ok = type_text(
                            paste_text,
                            app_name=target_app,
                            snap=recording_target,
                            settle_secs=settle,
                            max_retries=max_retries,
                            enter_count=combined_enter,
                            enter_delay=enter_delay,
                            focus_shortcut="",  # tier-1 success -> input focused
                        )
                else:
                    # Fail-closed: write clipboard, NO Cmd+V, error cue, toast (R5).
                    # W5: History write happens UNCONDITIONALLY upstream at the
                    # _save_transcript call (Fact 2) — fail-closed does not lose
                    # the transcript from history. Clipboard write is explicit
                    # here so the user has the transcript even if their original
                    # target is gone.
                    ok_clip, _ = _set_clipboard(paste_text)
                    if not ok_clip:
                        self._log(
                            "[PASTE] WARNING: clipboard write failed "
                            "during fail-closed"
                        )
                    audio_cue("error", cues_dir)
                    show_failure_toast(outcome.message, title="HeyVox paste")
                    self._log(
                        f"[PASTE] FAIL_CLOSED reason={outcome.reason.value} "
                        f"message={outcome.message}"
                    )

            if paste_ok:
                if combined_enter > 0:
                    self._log("Sent!")
                else:
                    self._log(f"Injected (paste, {'PTT' if ptt else 'wake word'})")
            else:
                self._log("[inject] paste failed")

            # Only restore focus if the user moved to a DIFFERENT app during
            # transcription. If they're still on the target app, no restore needed.
            if pre_inject_pid and pre_inject_pid != target_pid:
                self._log(
                    f"[inject] NOT restoring frontmost (user moved to pid={pre_inject_pid} "
                    "during transcription, staying on target)"
                )
            else:
                self._log(f"[inject] already on target pid={target_pid}, no restore needed")

            if stop_time:
                self._log(f"[TIMING] stop→done: {time.time() - stop_time:.2f}s")

            # Show confirmation in HUD — use paste_ok to decide cue and message
            if not paste_ok:
                if outcome is not None and outcome.reason is not None:
                    # Fail-closed: transcript saved to clipboard + history;
                    # toast already fired by the fail-closed branch above.
                    self._hud_send({
                        "type": "state", "state": "idle",
                        "text": "Paste failed (clipboard saved)",
                    })
                    self._log(
                        f"Paste FAIL_CLOSED — reason={outcome.reason.value}; "
                        f"clipboard + history retained"
                    )
                else:
                    self._hud_send({
                        "type": "state", "state": "idle", "text": "Paste failed",
                    })
                    self._log("Paste FAILED — error cue played by injection")
            elif ptt:
                # PTT mode: no auto-Enter, just pasted -- don't say "Sending"
                self._hud_send({"type": "state", "state": "idle", "text": "Pasted"})
                audio_cue("ok", cues_dir)
                self._log("Pasted (PTT)")
            else:
                sent_msg = "Sent to AI"
                self._hud_send({"type": "state", "state": "idle", "text": sent_msg})
                audio_cue("sending", cues_dir)
                self._log(sent_msg)
        except _subprocess.TimeoutExpired:
            self._log("WARNING: Subprocess timed out during send phase")
        except Exception as e:
            self._log(f"ERROR in send phase: {e}")
        finally:
            _release_recording_guard()
            with self.ctx.lock:
                self.ctx.busy = False
            # DEF-084: Clear cancel_transcription unconditionally at STT-path
            # exit. Every post-STT early-return (garbled, empty-stt,
            # voice-command) used to leak this flag; the user-cancelled branch
            # and the pre-type re-check each cleared it locally. Centralising
            # the reset here keeps the flag's lifecycle symmetrical with the
            # STT call — future filters can return early without reasoning
            # about flag cleanup.
            self.ctx.cancel_transcription.clear()
            # Resume media that we paused at recording start
            try:
                from heyvox.audio.media import resume_media
                resume_media()
            except Exception as e:
                self._log(f"WARNING: media resume failed: {e}")
            self._hud_send({"type": "state", "state": "idle"})
            self._log("Ready for next wake word.")
