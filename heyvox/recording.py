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
#
# DEF-101: per-mic override resolved by `_resolve_min_audio_dbfs()` below.
# This is the global fallback when no per-mic profile applies.
_MIN_AUDIO_DBFS = -48.0


def _resolve_min_audio_dbfs(config) -> float:
    """Return the energy-gate dBFS floor for the currently active mic.

    Resolution order (DEF-101):
      1. config.mic_profiles[<partial match for active mic>].min_audio_dbfs
      2. Global _MIN_AUDIO_DBFS fallback

    Active mic is read from ACTIVE_MIC_FILE (written by main.py on device
    init/switch). Match against config keys is partial + case-insensitive,
    same algorithm as MicProfileManager.get_profile().
    """
    try:
        from heyvox.constants import ACTIVE_MIC_FILE
        with open(ACTIVE_MIC_FILE) as _f:
            mic_name = _f.read().strip().split("\n")[0].lower()
    except (OSError, AttributeError):
        return _MIN_AUDIO_DBFS
    profiles = getattr(config, "mic_profiles", None) or {}
    for key, profile in profiles.items():
        if key.lower() in mic_name:
            override = getattr(profile, "min_audio_dbfs", None)
            if override is not None:
                return float(override)
            break
    return _MIN_AUDIO_DBFS


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


def _reverify_garbled_async(
    raw_wav_path: str,
    *,
    language: str = "",
    hud_send=None,
    log=None,
) -> None:
    """Background reverify of a garbled-flagged recording via Whisper Large v3.

    Pattern P-stochastic-stt: whisper-small can hallucinate repetition on a
    clean recording (live run trips is_garbled), while large-v3 on the same
    raw WAV usually produces clean text. We don't auto-paste (focus may have
    moved on by the time large-v3 finishes — ~5–15 s on M-series), instead
    we copy the recovered text to the clipboard, persist it to history,
    and surface a HUD note so the user knows ⌘V will paste it.

    Non-fatal — every failure path logs and returns silently.
    """
    def _worker():
        try:
            os.environ.setdefault("HF_HUB_DISABLE_PROGRESS_BARS", "1")
            import wave
            import subprocess

            if log:
                log(f"Reverify: loading raw audio from {raw_wav_path}")
            with wave.open(raw_wav_path, "rb") as w:
                sr = w.getframerate()
                raw = w.readframes(w.getnframes())
            if not raw:
                if log:
                    log("Reverify: empty raw WAV — giving up")
                return
            audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
            audio_secs = len(audio) / sr

            try:
                import mlx_whisper  # type: ignore
            except ImportError:
                if log:
                    log("Reverify: mlx_whisper not available — skipping")
                return

            if log:
                log(f"Reverify: transcribing {audio_secs:.1f}s with whisper-large-v3...")
            t0 = time.time()
            kwargs = {
                "path_or_hf_repo": "mlx-community/whisper-large-v3-mlx",
                "word_timestamps": False,
                # Same defensive params as the live pipeline so behaviour stays
                # consistent: no segment-to-segment context, escape degenerate
                # decoding sooner.
                "condition_on_previous_text": False,
                "compression_ratio_threshold": 2.2,
                "logprob_threshold": -0.8,
            }
            if language:
                kwargs["language"] = language
            result = mlx_whisper.transcribe(audio, **kwargs)
            text = (result.get("text") or "").strip()
            elapsed = time.time() - t0

            if not text:
                if log:
                    log(f"Reverify: empty output after {elapsed:.1f}s — giving up")
                return
            if is_garbled(text, stt_secs=elapsed, audio_secs=audio_secs):
                if log:
                    log(f"Reverify: large-v3 also garbled after {elapsed:.1f}s — giving up")
                return

            text = strip_wake_words(text, "hey vox", "hey vox")
            if not text:
                if log:
                    log("Reverify: nothing left after wake-word strip — giving up")
                return

            preview = text[:80] + ("..." if len(text) > 80 else "")
            if log:
                log(
                    f"Reverify: recovered {len(text)} chars in {elapsed:.1f}s — "
                    f"copying to clipboard"
                )

            try:
                subprocess.run(
                    ["pbcopy"],
                    input=text.encode("utf-8"),
                    check=True,
                    timeout=5,
                )
            except (subprocess.SubprocessError, OSError) as e:
                if log:
                    log(f"Reverify: pbcopy failed: {e}")
                return

            if hud_send is not None:
                try:
                    hud_send({
                        "type": "transcript",
                        "text": f"Recovered (⌘V to paste): {preview}",
                    })
                except Exception as e:
                    if log:
                        log(f"Reverify: HUD send failed: {e}")

            try:
                from heyvox.history import save as _save_transcript
                _save_transcript(text, duration=audio_secs, ptt=False)
            except Exception as e:
                if log:
                    log(f"Reverify: history save failed: {e}")
        except Exception as e:
            if log:
                log(f"Reverify: unexpected error {type(e).__name__}: {e}")

    threading.Thread(
        target=_worker,
        daemon=True,
        name="heyvox-garbled-reverify",
    ).start()


def _release_recording_guard(flag_delay: float = 0.0) -> None:
    """Release the recording guard — both in-process event and cross-process file flag.

    Called after STT->paste completes (or on early exit) so the TTS hook
    knows it's safe to speak again.

    flag_delay: if > 0, defer the cross-process RECORDING_FLAG removal by this
    many seconds in a daemon thread. The in-process TTS-echo flag and the HUD
    state are still cleared synchronously. Used after auto-Enter paste so a
    Herald-driven workspace switch can't race Conductor's async submit handler
    and steal focus mid-submit (DEF-070-style intra-Conductor focus steal).
    """
    try:
        from heyvox.audio.tts import set_recording as _tts_set_rec
        _tts_set_rec(False)
    except ImportError:
        pass

    def _remove_flag() -> None:
        try:
            os.remove(RECORDING_FLAG)
        except FileNotFoundError:
            pass

    if flag_delay > 0:
        def _delayed() -> None:
            time.sleep(flag_delay)
            _remove_flag()
        threading.Thread(target=_delayed, daemon=True).start()
    else:
        _remove_flag()

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
        self._quiet_streak = 0  # consecutive too-quiet recordings; banner only after ≥2

    def start(self, ptt: bool = False, preroll=None, handsfree: bool = False) -> None:
        """Begin a recording session.

        Sets is_recording flag, signals TTS to pause, plays listening cue,
        and shows the recording indicator.

        Args:
            ptt: True if triggered by push-to-talk (affects auto-send behavior).
            preroll: Iterable of audio chunks captured before the wake word trigger.
                Prepended to the audio buffer so the first words aren't clipped.
            handsfree: True if triggered by a PTT-key double-tap (continuous
                mode). Ends like a wake-word recording (silence/stop-word/Escape
                via the main-loop watchdogs, which require triggered_by_ptt to be
                False), but stop() skips the wake-word audio trim because there
                is no spoken wake word to remove. Mutually exclusive with ptt.
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
            # DEF-155: fresh score horizon for this recording's stop-miss
            # diagnostics (written by the main loop, read by stop()).
            self.ctx.rec_stop_score_max = 0.0
            # Pre-roll: prepend recent audio so first words aren't clipped
            self.ctx.audio_buffer = list(preroll) if preroll else []
            self.ctx.triggered_by_ptt = ptt
            # handsfree (double-tap) keeps triggered_by_ptt False so the main
            # loop's silence/stop-word watchdogs run, but flags itself so stop()
            # skips the wake-word trim (no spoken wake word to remove).
            self.ctx.handsfree = handsfree and not ptt
            self.ctx.stopped_via_ptt_mid_recording = False  # DEF-116: reset per-recording
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
        # [WW_LATENCY] consume t1/detect_ms set by _run_loop just before this call.
        # Reset to 0.0 immediately so stale values don't leak to the next activation.
        # PTT and handsfree paths leave these at 0.0 (no wake-word timing to report).
        try:
            import heyvox.main as _main_mod
            _ww_t1 = _main_mod._ww_t1
            _ww_detect_ms = _main_mod._ww_detect_ms
            _main_mod._ww_t1 = 0.0
            _main_mod._ww_detect_ms = 0.0
        except Exception:
            _ww_t1 = 0.0
            _ww_detect_ms = 0.0
        audio_cue("listening", cues_dir, t1=_ww_t1, detect_ms=_ww_detect_ms)
        self._hud_send({"type": "state", "state": "listening"})
        self._log("Recording started. Waiting for stop wake word.")

        # Preload STT model in background while user speaks — hides the
        # model load latency (varies by model, see MLX log lines) behind
        # recording time. If already loaded, DEF-164: also refreshes the
        # idle-unload timer so a stale countdown can't evict it mid-recording.
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

    def stop(self, reason: str = "other") -> None:
        """End a recording session and dispatch transcription.

        Checks minimum recording duration, plays feedback cue, and starts
        the transcription thread.

        Args:
            reason: What ended the session — "stop_wake" (stop wake word),
                "silence_timeout", "max_duration", "ptt", "ptt_interrupt"
                (user gave up waiting for the stop wake word, DEF-116) or
                "other". Forwarded to _send_local so the wake-word strip can
                emit [STOP_MISSED] when a spoken stop word survived into the
                transcript without having ended the recording (DEF-151).
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
            handsfree_snapshot = self.ctx.handsfree
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

                # DEF-155/156: snapshot the UN-trimmed tail for training
                # collection before the wake-word trim below removes the very
                # audio the collector needs. Long enough to reach back past a
                # full silence-timeout to a missed stop wake word. The observed
                # stop-score travels with it so saved fn/tp clips distinguish
                # model-blind misses from gate-blocked ones in the filename.
                _raw_tail: list = []
                _observed_stop_score = 0.0
                if self.training_collector:
                    _tail_chunk_count = int(
                        (self.config.silence_timeout_secs + 3.0)
                        * self.config.audio.sample_rate
                        / self.config.audio.chunk_size
                    ) + 1
                    _raw_tail = list(recorded_chunks[-_tail_chunk_count:])
                    _observed_stop_score = getattr(
                        self.ctx, "rec_stop_score_max", 0.0
                    )

                if not ptt_snapshot:
                    # Wake word audio trim -- remove wake word from both ends so
                    # Whisper never sees it. This is the primary defense; the text-level
                    # strip_wake_words() is a fallback for imperfect trims.
                    #
                    # Start trim: ~1.5s covers pre-roll buffer (500ms) + wake word (~1000ms).
                    # End trim: 0.5s -- conservative, only cuts actual stop wake word.
                    #
                    # DEF-116: if the recording was stopped by PTT mid-way (user
                    # gave up waiting for the stop wake-word to fire), skip the
                    # end-trim entirely — there is no stop-wake-word at the end
                    # to remove, and 480 ms of trim would clip the user's last
                    # word(s). Start-trim still applies — the recording was
                    # started by wake-word, so the start wake-word IS there.
                    #
                    # Hands-free (double-tap) recordings have NO spoken wake word
                    # at either end, so both trims are zeroed — any start-trim
                    # would clip the user's opening words. A stop wake word, if
                    # the user spoke one, is removed at the text level by
                    # strip_wake_words() below, so dropping the audio end-trim
                    # here is safe.
                    if handsfree_snapshot:
                        ww_start_trim_secs = 0.0
                        ww_end_trim_secs = 0.0
                    else:
                        ww_start_trim_secs = 1.5
                        ww_end_trim_secs = 0.0 if self.ctx.stopped_via_ptt_mid_recording else 0.5
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
                            "stop_time": _stop_t0, "stop_reason": reason,
                            "raw_tail": _raw_tail,
                            "observed_stop_score": _observed_stop_score},
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
        stop_reason: str = "other",
        raw_tail: list | None = None,
        observed_stop_score: float = 0.0,
    ) -> None:
        """Transcribe locally and inject text into target app."""
        import subprocess as _subprocess
        from heyvox.audio.stt import transcribe_audio
        from heyvox.audio.cues import audio_cue, get_cues_dir
        from heyvox.input.injection import (
            type_text, save_frontmost_pid, _settle_delay_for, app_fast_paste,
            _set_clipboard,
        )
        from heyvox.input.target import resolve_lock
        from heyvox.input.toast import show_failure_toast
        from heyvox.audio.tts import check_voice_command, execute_voice_command

        try:
            # Energy gate: skip STT on silent recordings to avoid Whisper hallucinations.
            # Uses raw_rms_db computed BEFORE wake word trim (wake word is the loudest part).
            # DEF-101: per-mic threshold from config.mic_profiles[<mic>].min_audio_dbfs.
            min_dbfs = _resolve_min_audio_dbfs(self.config)
            if raw_rms_db < min_dbfs:
                self._quiet_streak += 1
                self._log(
                    f"Recording too quiet ({raw_rms_db:.1f} dBFS < {min_dbfs} dBFS), skipping STT"
                    f" [streak={self._quiet_streak}]"
                )
                # DEF-101: surface silent-skip via HUDSurface banner — but only
                # after 2 consecutive quiet recordings so a single wake-word false
                # positive (background noise briefly triggers the model) doesn't
                # show a misleading "mic too quiet" warning to the user.
                if self._quiet_streak >= 2:
                    try:
                        from heyvox.hud.surface import HUDSurface
                        from heyvox.constants import MIC_WARN_TTL_SECS
                        _mic_name = ""
                        try:
                            from heyvox.constants import ACTIVE_MIC_FILE
                            _mic_name = open(ACTIVE_MIC_FILE).read().strip().split("\n")[0][:40]
                        except OSError:
                            pass
                        _warn = (
                            f"Mic too quiet ({raw_rms_db:.0f} dBFS)"
                            + (f" — {_mic_name}" if _mic_name else "")
                        )
                        HUDSurface.banner(
                            level="warn",
                            source="recording-quiet",
                            text=_warn,
                            ttl_secs=MIC_WARN_TTL_SECS,
                        )
                    except Exception:
                        pass
                # Training: wake fired but mic captured only noise → FP.
                if self.training_collector:
                    self.training_collector.save_fp(
                        audio_chunks, self.config.audio.sample_rate,
                        reason="low-energy",
                    )
                cues_dir = get_cues_dir(self.config.cues_dir)
                audio_cue("paused", cues_dir)
                # Reset HUD to idle so the menu bar refresh picks up the warn file
                self._hud_send({"type": "state", "state": "idle"})
                return

            _t_stt_start = time.time()
            if stop_time:
                self._log(f"[TIMING] stop→STT start: {_t_stt_start - stop_time:.2f}s")
            self._quiet_streak = 0
            self._log(f"Recording was {duration:.1f}s ({raw_rms_db:.1f} dBFS), transcribing...")
            try:
                print(f"[recording] Transcribing {duration:.1f}s audio...", file=sys.stderr)
            except (BrokenPipeError, OSError):
                pass
            t0 = time.time()
            # Capture warm/cold BEFORE transcribe: warm=False means this STT pays
            # the cold model-load cost (force-unload under RAM pressure or 10min idle).
            # Tagging it makes model-swap + cold-reload latency regressions greppable.
            from heyvox.audio.stt import model_loaded as _mlx_model_loaded
            _stt_was_warm = _mlx_model_loaded()
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
            # Short model id for log tagging: mlx-community/whisper-large-v3-turbo → large-v3-turbo
            _stt_model_short = (
                self.config.stt.local.mlx_model.split("/")[-1]
                .replace("whisper-", "").replace("-mlx", "")
            )
            if stop_time:
                self._log(
                    f"[TIMING] stop→STT done: {_t_stt_done - stop_time:.2f}s "
                    f"(STT={elapsed:.1f}s model={_stt_model_short} warm={_stt_was_warm})"
                )
            self._log(
                f"Transcription ({elapsed:.1f}s): {text[:80]}{'...' if len(text) > 80 else ''}"
            )

            # Log raw STT output for debug
            _save_debug_audio("_stt_result", [], self.config.audio.sample_rate, {
                "stt_raw": text[:200],
                "stt_engine": self.config.stt.local.engine,
                "stt_model": _stt_model_short,
                "stt_warm": _stt_was_warm,
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

            # DEF-091: Strip wake-word repetitions from the trailing/leading
            # edges of the transcription BEFORE running is_garbled. MLX
            # frequently transcribes the user's stop wake word three or more
            # times in a row at the end of the audio (each "Hey Vox" attempt
            # becomes "Hey Wax. Hey Wax. Hey Wax. ..." in MLX output, plus
            # temperature-fallback often duplicates). The repeated bigrams
            # then trip is_garbled's tail-window or consecutive-duplicate
            # check and the *entire transcription* is discarded — including
            # the user's clean dictation prefix. Stripping first keeps the
            # garbled-detector focused on real hallucination rather than
            # legitimate stop-wake-word echoes the strip would have removed
            # anyway. Save_fn_stop/save_tp_stop training tracking moves with
            # the strip; the original strip block at the end of this method
            # has been replaced with a no-op since `text` is already cleaned.
            pre_strip_text = text
            text = strip_wake_words(
                text,
                self.config.wake_words.start,
                self.config.wake_words.stop,
            )
            _wake_word_stripped = text != pre_strip_text
            _end_stripped = False
            if _wake_word_stripped:
                self._log(
                    f"Wake word strip: '{pre_strip_text[:80]}' -> '{text[:80]}'"
                )
                # DEF-151 observability: trailing wake words in the transcript
                # of a recording that was NOT ended by the stop-wake path are
                # ground truth for missed stops — the user audibly said
                # "Hey Vox" (STT heard it!) but the detector/gates dropped
                # every attempt and a timeout/PTT/cap ended the session.
                # End-strip is detected via common-prefix: a start-only strip
                # changes the head of the text, an end-strip keeps it.
                # reason == "stop_wake" with a stripped tail is the normal
                # DEF-091 imperfect-trim case and stays untagged.
                _pre_n = pre_strip_text.strip()
                _post_n = text.strip()
                _end_stripped = (
                    len(_pre_n) > len(_post_n)
                    and _pre_n.lower().startswith(_post_n.lower())
                )
                if _end_stripped and stop_reason != "stop_wake":
                    self._log(
                        f"[STOP_MISSED] reason={stop_reason} "
                        f"tail='{_pre_n[len(_post_n):].strip()[:60]}'"
                    )
            # DEF-155/156: label the recording tail for wake-word training.
            # Labeling rules live in classify_stop_outcome (unit-tested):
            # fn = proven miss (end-strip on a non-stop_wake recording),
            # tp = confirmed stop_wake detection (strip or not — DEF-091
            # trim leftovers are TPs, the old code filed them as FN).
            # Clips cut from the UN-trimmed raw_tail so the spoken wake
            # word is actually in them; the trimmed _training_chunks
            # (wake word removed) remain for the FP paths below.
            if self.training_collector:
                from heyvox.audio.training_collector import classify_stop_outcome
                _label = classify_stop_outcome(
                    stop_reason,
                    end_stripped=_end_stripped,
                    has_text=bool(text and text.strip()),
                )
                _tail_for_training = raw_tail if raw_tail else _training_chunks
                if _label and _tail_for_training:
                    if _label == "fn":
                        self.training_collector.save_fn_stop(
                            _tail_for_training, _training_sr,
                            score=observed_stop_score,
                        )
                    else:
                        self.training_collector.save_tp_stop(
                            _tail_for_training, _training_sr,
                            score=observed_stop_score,
                        )

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
                _raw_for_reverify = None
                try:
                    if _last_raw_wav:
                        self._log(f"FILTER (garbled): raw audio preserved at {_last_raw_wav}")
                        _raw_for_reverify = _last_raw_wav
                except NameError:
                    pass
                # DEF-133: the large-v3 recovery needs a raw WAV, but
                # _save_debug_audio only writes one when STT_DEBUG_DIR exists.
                # With debug capture off (the default), persist *this* garbled
                # recording on-demand so the reverify still runs — and the
                # failure stays provable on disk. Only fires on the rare garbled
                # path, so it never bloats the per-recording hot path.
                if _raw_for_reverify is None and audio_chunks:
                    try:
                        import wave as _wave
                        import tempfile as _tempfile
                        _gfd, _gpath = _tempfile.mkstemp(
                            prefix="heyvox-garbled-", suffix=".wav"
                        )
                        os.close(_gfd)
                        _gaudio = np.concatenate(audio_chunks)
                        with _wave.open(_gpath, "wb") as _gwf:
                            _gwf.setnchannels(1)
                            _gwf.setsampwidth(2)  # int16
                            _gwf.setframerate(self.config.audio.sample_rate)
                            _gwf.writeframes(_gaudio.tobytes())
                        _raw_for_reverify = _gpath
                        self._log(
                            f"FILTER (garbled): raw audio captured on-demand for reverify at {_gpath}"
                        )
                    except Exception as _ge:
                        self._log(f"FILTER (garbled): on-demand raw save failed: {_ge}")
                # P-stochastic-stt: whisper-small can hallucinate repetition on
                # a clean recording; re-run large-v3 in the background and copy
                # any clean recovery to the clipboard. No auto-paste — by the
                # time large-v3 finishes (~5–15 s) the user's focus has often
                # moved on, and silently injecting into the wrong app is worse
                # than asking them to ⌘V into the right one.
                hud_msg = f"Garbled STT ({elapsed:.1f}s) - reverifying with large-v3..." \
                    if _raw_for_reverify else f"Garbled STT ({elapsed:.1f}s) - try again"
                self._hud_send({"type": "transcript", "text": hud_msg})
                if _raw_for_reverify:
                    _reverify_garbled_async(
                        _raw_for_reverify,
                        language=getattr(self.config.stt.local, "language", "") or "",
                        hud_send=self._hud_send,
                        log=self._log,
                    )
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

            # DEF-091: wake-word strip moved upstream (right after the echo
            # filter, before is_garbled). `pre_strip_text` and
            # `_wake_word_stripped` are already populated. Keep the variable
            # alias `pre_strip` for the debug-audio payload below so external
            # log readers (heyvox log-health) keep parsing.
            pre_strip = pre_strip_text

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
                        # in Plan 15-03). Pass combined_enter explicitly so
                        # PTT mode (combined_enter=0) suppresses auto-Enter
                        # instead of falling through to profile.enter_count
                        # — fix for the PTT double-paste-then-send bug.
                        paste_ok = app_fast_paste(
                            profile, paste_text, enter_count=combined_enter,
                        )
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
                    # DEF-088: each step is logged so a future hang in this
                    # branch is pin-pointed instead of stalling silently for
                    # 60 s until the busy-flag watchdog force-resets.
                    self._log("[PASTE] fail-closed: writing clipboard")
                    ok_clip, _ = _set_clipboard(paste_text)
                    if not ok_clip:
                        self._log(
                            "[PASTE] WARNING: clipboard write failed "
                            "during fail-closed"
                        )
                    self._log("[PASTE] fail-closed: playing error cue")
                    audio_cue("error", cues_dir)
                    self._log("[PASTE] fail-closed: showing failure toast")
                    show_failure_toast(outcome.message, title="HeyVox paste")
                    self._log(
                        f"[PASTE] FAIL_CLOSED reason={outcome.reason.value} "
                        f"message={outcome.message}"
                    )
                    # Persistent menu-bar surface (toast is transient). The
                    # reason answers "why did paste fail?" without grepping
                    # the log. P-detector-without-action.
                    try:
                        from heyvox.hud.surface import HUDSurface
                        HUDSurface.banner(
                            level="error",
                            source="paste-fail",
                            text=f"Paste failed → clipboard ({outcome.reason.value})",
                            ttl_secs=30,
                        )
                    except Exception:
                        pass

            # --- 15-06: post-paste verification (SPEC R7) ---
            # W12 — defensive guard: outcome may be None when recording_target
            # was None (15-05 explicitly initializes outcome = None per W6).
            # Gate on BOTH `outcome is not None` AND `outcome.ok` to avoid
            # AttributeError on `outcome.element` when no resolution happened.
            # DEF-090: skip verify when auto-Enter is active. The send-key
            # clears Conductor's chat input on a successful submit, so the
            # post-Enter AX value is "1-char placeholder" or empty — which
            # verify_paste correctly fails to match against the transcript.
            # The original retry then fires Cmd+V into the just-cleared
            # field, the drift branch fires `show_failure_toast`, and the
            # whole branch hangs ~60 s in the toast subprocess until the
            # busy-flag watchdog force-resets state. verify_paste was
            # designed for "did paste land in the right field?" without an
            # Enter step — it cannot tell auto-Enter success from drift.
            run_verify = (
                paste_ok
                and recording_target is not None
                and outcome is not None
                and outcome.ok
                and combined_enter == 0
            )
            if run_verify:
                from heyvox.input.target import verify_paste
                verify = verify_paste(
                    recording_target,
                    outcome.element,  # may be None for Tier 2 — verify_paste
                                      # re-acquires via AXFocusedUIElement (W3)
                    paste_text,
                    profile,
                )
                self._log(
                    f"[PASTE] verify result: verified={verify.verified} "
                    f"retried={verify.retried} drift={verify.drift} "
                    f"detail={verify.detail!r}"
                )
                if verify.drift:
                    # Content WAS sent (just possibly to wrong place) — do
                    # NOT downgrade paste_ok. Surface: error cue + toast.
                    audio_cue("error", cues_dir)
                    drift_message = (
                        "HeyVox: paste verification failed — "
                        "content may have landed in the wrong field."
                    )
                    show_failure_toast(drift_message, title="HeyVox paste drift")
            elif paste_ok and combined_enter > 0:
                # Brief log so the operator can correlate "no verify ran"
                # with the auto-Enter path during triage.
                self._log("[PASTE] verify skipped (auto-Enter clears field)")

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
            # Hold RECORDING_FLAG ~2s past Sent! when auto-Enter was used so
            # Herald's workspace-switch can't race Conductor's async submit
            # handler. paste_ok and combined_enter are initialised at the top
            # of the try-block, so they're safe to read here even on early
            # exception paths. (Hybrid option C; partner change is --force
            # removal + 2s idle gate in heyvox/herald/orchestrator.py.)
            try:
                _post_send_grace = 2.0 if (paste_ok and combined_enter > 0) else 0.0
            except NameError:
                _post_send_grace = 0.0
            _release_recording_guard(flag_delay=_post_send_grace)
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
