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
from heyvox.constants import RECORDING_FLAG, TTS_PLAYING_FLAG, STT_DEBUG_DIR

if TYPE_CHECKING:
    from heyvox.app_context import AppContext
    from heyvox.config import HeyvoxConfig

# Minimum audio energy (dBFS) to proceed with STT. Recordings below this
# threshold are treated as silence — skips Whisper to avoid hallucinations.
# -60 dBFS catches only true silence/near-silence. Normal quiet speech is ~-45 to -35 dBFS.
_MIN_AUDIO_DBFS = -60.0


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
    _MIN_AUDIO_DBFS = -60.0     # Energy gate for STT
    _BUSY_TIMEOUT = 60.0        # Force-reset busy after this many seconds
    _ZOMBIE_FAIL_THRESHOLD = 2  # Force reinit after N consecutive failed recordings

    def __init__(self, ctx: "AppContext", config: "HeyvoxConfig", log_fn, hud_send) -> None:
        self.ctx = ctx
        self.config = config
        self._log = log_fn
        self._hud_send = hud_send

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
            # Snapshot which app/text field is focused right now, so we can
            # restore it at injection time even if the user clicks away.
            from heyvox.input.target import snapshot_target
            self.ctx.recording_target = snapshot_target(config=self.config)
            if self.ctx.recording_target:
                ws_info = (
                    f", workspace={self.ctx.recording_target.detected_workspace!r}"
                    if self.ctx.recording_target.detected_workspace else ""
                )
                self._log(
                    f"[snapshot] app={self.ctx.recording_target.app_name}, "
                    f"pid={self.ctx.recording_target.app_pid}, "
                    f"window={self.ctx.recording_target.window_title!r}, "
                    f"element={self.ctx.recording_target.element_role}{ws_info}"
                )
            else:
                self._log("[snapshot] WARNING: no target snapshot (AppKit unavailable?)")

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

        from heyvox.audio.cues import audio_cue, get_cues_dir
        cues_dir = get_cues_dir(self.config.cues_dir)
        audio_cue("listening", cues_dir)
        self._hud_send({"type": "state", "state": "listening"})
        self._log("Recording started. Waiting for stop wake word.")
        print(
            f"[recording] Started, target="
            f"{self.ctx.recording_target.app_name if self.ctx.recording_target else 'None'}",
            file=sys.stderr,
        )

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

                # Save raw audio BEFORE any trimming (for debug analysis)
                _save_debug_audio("raw", recorded_chunks, self.config.audio.sample_rate, {
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

                    # Save trimmed audio for comparison
                    _save_debug_audio(
                        "trimmed", recorded_chunks, self.config.audio.sample_rate,
                        log_fn=self._log,
                    )

                # _send_local has its own finally block that resets busy = False
                threading.Thread(
                    target=self._send_local,
                    args=(duration, recorded_chunks, raw_rms_db),
                    kwargs={"ptt": ptt_snapshot, "recording_target": target_snapshot},
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
    ) -> None:
        """Transcribe locally and inject text into target app."""
        import subprocess as _subprocess
        from heyvox.audio.stt import transcribe_audio
        from heyvox.audio.cues import audio_cue, get_cues_dir
        from heyvox.input.injection import type_text, save_frontmost_pid, restore_frontmost
        from heyvox.input.target import restore_target
        from heyvox.audio.tts import check_voice_command, execute_voice_command

        try:
            # Energy gate: skip STT on silent recordings to avoid Whisper hallucinations.
            # Uses raw_rms_db computed BEFORE wake word trim (wake word is the loudest part).
            if raw_rms_db < _MIN_AUDIO_DBFS:
                self._log(
                    f"Recording too quiet ({raw_rms_db:.1f} dBFS < {_MIN_AUDIO_DBFS} dBFS), skipping STT"
                )
                cues_dir = get_cues_dir(self.config.cues_dir)
                audio_cue("paused", cues_dir)
                return

            self._log(f"Recording was {duration:.1f}s ({raw_rms_db:.1f} dBFS), transcribing...")
            print(f"[recording] Transcribing {duration:.1f}s audio...", file=sys.stderr)
            t0 = time.time()
            text = transcribe_audio(
                audio_chunks,
                engine=self.config.stt.local.engine,
                mlx_model=self.config.stt.local.mlx_model,
                language=self.config.stt.local.language,
                sample_rate=self.config.audio.sample_rate,
            )
            elapsed = time.time() - t0
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
            echo_filtered = False
            if text and self.config.echo_suppression.stt_echo_filter:
                try:
                    from heyvox.audio.echo import filter_tts_echo
                    filtered = filter_tts_echo(text)
                    if filtered != text:
                        self._log(f"ECHO-03: Stripped TTS echo from transcription (was: {text[:60]})")
                        echo_filtered = True
                        text = filtered
                except Exception:
                    pass

            # Quality filter: discard garbled/nonsensical STT output
            if text and is_garbled(text):
                self._log(f"FILTER: Discarding garbled transcription: {text[:80]}")
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
                audio_cue("paused", cues_dir)
                return

            # Check if cancelled during transcription
            if self.ctx.cancel_transcription.is_set():
                self._log("Transcription cancelled by user (Escape)")
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
            if text != pre_strip:
                self._log(f"Wake word strip: '{pre_strip[:80]}' -> '{text[:80]}'")

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

            target_app = recording_target.app_name if recording_target else None
            target_window = recording_target.window_title if recording_target else None
            self._log(
                f"[inject] target_app={target_app}, window={target_window!r}, "
                f"mode={'PTT' if ptt else 'wake word'}, "
                f"text={len(paste_text)} chars: {paste_text[:60]!r}"
            )
            print(
                f"[recording] Injecting -> {target_app or 'frontmost'} "
                f"(window={target_window!r})",
                file=sys.stderr,
            )

            # Save the user's current focus so we can restore it if injection
            # steals focus from the SAME app they're already in.
            pre_inject_pid = save_frontmost_pid()
            target_pid = recording_target.app_pid if recording_target else 0
            self._log(
                f"[inject] saved pre-inject frontmost pid={pre_inject_pid}, "
                f"target pid={target_pid}"
            )

            # NOTE: Direct socket injection (e.g. Conductor sidecar) is disabled.
            # The sidecar registers methods on an internal tunnel, not the external
            # Unix socket. Kept heyvox/input/conductor.py for future use if an
            # app-specific injection API becomes available.
            _injected_via_conductor = False

            if not _injected_via_conductor:
                if recording_target:
                    if recording_target.detected_workspace:
                        self._log(
                            f"[inject] Restoring workspace "
                            f"'{recording_target.detected_workspace}'"
                            f" for {recording_target.app_name}"
                        )
                    restore_target(recording_target, config=self.config)
                    self._log(f"[inject] Restored target: {recording_target.app_name}")

                type_text(paste_text, app_name=target_app)

            # Auto-send Enter in wake word mode if adapter says so
            adapter = self.ctx.adapter
            auto_send = not ptt and adapter.should_auto_send()
            self._log(
                f"[inject] auto_send={auto_send} (ptt={ptt}, "
                f"adapter.should_auto_send={adapter.should_auto_send()}, "
                f"enter_count={adapter.enter_count})"
            )
            if auto_send:
                time.sleep(1.0)
                from heyvox.input.injection import press_enter as _press_enter
                self._log(f"Pressing Enter x{adapter.enter_count} -> {target_app or 'frontmost'}...")
                _press_enter(adapter.enter_count, app_name=target_app)
                self._log("Sent!")
            else:
                self._log(f"Injected (paste, {'PTT' if ptt else 'wake word'})")

            # Only restore focus if the user was already in the target app before injection.
            if pre_inject_pid and pre_inject_pid == target_pid:
                time.sleep(0.3)
                restore_frontmost(pre_inject_pid)
                self._log(
                    f"[inject] restored frontmost to pid={pre_inject_pid} (was already on target)"
                )
            elif pre_inject_pid and pre_inject_pid != target_pid:
                self._log(
                    f"[inject] NOT restoring frontmost (user moved to pid={pre_inject_pid} "
                    "during transcription, staying on target)"
                )

            # Show "Sent to [agent]" confirmation in HUD
            if ptt:
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
            # Resume media that we paused at recording start
            try:
                from heyvox.audio.media import resume_media
                resume_media()
            except Exception as e:
                self._log(f"WARNING: media resume failed: {e}")
            self._hud_send({"type": "state", "state": "idle"})
            self._log("Ready for next wake word.")
