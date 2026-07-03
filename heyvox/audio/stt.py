"""
Speech-to-text engine management for heyvox.

Supports two backends:
- "mlx": MLX Whisper (Metal GPU, Apple Silicon only) — fast, preferred
- "sherpa": sherpa-onnx Whisper (CPU, int8 quantized) — universal fallback

MLX model is lazy-loaded on first use and unloaded after idle timeout
to free ~855MB of GPU/unified memory when not dictating.
"""

import ctypes
import ctypes.util
import os
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

import numpy as np

# Timeout for model loading and transcription calls
_LOAD_TIMEOUT = 120  # seconds — cold load can be very slow under swap pressure
_TRANSCRIBE_TIMEOUT = 30  # seconds — no transcription should take this long

# DEF-171: pthread QoS class for the transcription worker thread. One tier
# below QOS_CLASS_USER_INTERACTIVE (0x21) on purpose — that top tier is
# reserved for UI-blocking work, and this codebase already has a thread that
# needs to win there (the PTT CGEventTap callback, see DEF-168 — starving it
# under load makes Escape/PTT go silently unresponsive). user_initiated
# (0x19) still outranks the process/thread default, which is what actually
# matters on a system oversubscribed by parallel Conductor/Claude sessions
# (load averages of 13-32 observed on this 8-core machine, see DEF-170).
_QOS_CLASS_USER_INITIATED = 0x19


def _boost_transcribe_thread_priority() -> None:
    """Raise the CALLING thread's QoS class. Must be invoked from inside the
    worker thread itself (pthread_set_qos_class_self_np has no "set on
    another thread" variant) — call this as the first line of whatever
    function actually runs on the ThreadPoolExecutor worker.

    Best-effort and silent: a failure here (non-Darwin, missing symbol,
    sandboxed environment) just leaves the thread at its current QoS.
    Never raises — this must not be able to break transcription itself.
    """
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
        libc.pthread_set_qos_class_self_np(_QOS_CLASS_USER_INITIATED, 0)
    except Exception:
        pass

from heyvox.constants import DEFAULT_SAMPLE_RATE  # noqa: E402


# Global sherpa recognizer (initialized once, reused across calls)
_recognizer = None

# MLX lazy-load state
_mlx_model_id: str = ""
_mlx_language: str = ""
_mlx_loaded = threading.Event()  # Set when model is ready
_mlx_lock = threading.Lock()
_mlx_last_use: float = 0.0
_mlx_unload_secs: float = 300.0  # idle → unload; configurable via stt.local.unload_secs (too-short timeouts cause slow reloads under swap pressure)
_mlx_unloader: threading.Timer | None = None
_mlx_transcribing: bool = False  # Guard: prevents unload during active transcription
_mlx_unavailable: bool = False  # True once an mlx-whisper import fails (e.g. Intel Mac) — makes the load wait fail fast instead of blocking _LOAD_TIMEOUT
_log_fn: Callable[[str], None] | None = None
_mlx_initial_prompt: str = ""   # Phase 16: glossary bias for the first decode window


def _log(msg: str) -> None:
    if _log_fn:
        _log_fn(msg)
    else:
        print(msg, flush=True)


def _load_mlx_model() -> None:
    """Load MLX Whisper model into GPU memory (blocking)."""
    global _mlx_last_use, _mlx_unavailable
    if _mlx_loaded.is_set():
        return
    with _mlx_lock:
        if _mlx_loaded.is_set():
            return  # Another thread loaded while we waited
        try:
            import mlx_whisper
        except ImportError:
            _mlx_unavailable = True
            _log("ERROR: mlx-whisper is not installed. Install with: pip install 'heyvox[apple-silicon]'")
            _log("MLX Whisper requires Apple Silicon. Use engine: sherpa for Intel Macs.")
            return
        _log(f"Loading MLX whisper model ({_mlx_model_id})...")
        t0 = time.perf_counter()
        dummy = np.zeros(16000, dtype=np.float32)
        mlx_whisper.transcribe(dummy, path_or_hf_repo=_mlx_model_id)
        elapsed = time.perf_counter() - t0
        _mlx_last_use = time.time()
        _mlx_loaded.set()
        _log(f"MLX model loaded in {elapsed:.1f}s")
        _schedule_unload()


def _unload_mlx_model() -> None:
    """Unload MLX Whisper model to free GPU memory."""
    global _mlx_unloader
    with _mlx_lock:
        if not _mlx_loaded.is_set():
            return
        if _mlx_transcribing:
            # Transcription in progress — reschedule, don't unload
            _schedule_unload()
            return
        idle = time.time() - _mlx_last_use
        if idle < _mlx_unload_secs:
            # Not idle long enough — reschedule
            _schedule_unload()
            return
        try:
            import mlx.core as mx
            mx.metal.clear_cache()
        except ImportError:
            pass
        # Force Python to release the module's cached model
        import mlx_whisper
        # Clear any cached state in mlx_whisper
        import importlib
        importlib.reload(mlx_whisper)
        import gc
        gc.collect()
        _mlx_loaded.clear()
        _log(f"MLX model unloaded after {idle:.0f}s idle (memory freed)")


def _schedule_unload() -> None:
    """Schedule model unload after idle timeout. Must hold _mlx_lock or be called from within it."""
    global _mlx_unloader
    if _mlx_unloader is not None:
        _mlx_unloader.cancel()
    _mlx_unloader = threading.Timer(_mlx_unload_secs, _unload_mlx_model)
    _mlx_unloader.daemon = True
    _mlx_unloader.start()


def preload_model() -> None:
    """Start loading MLX model in background thread.

    Call this when wake word triggers to hide load latency behind
    the user's speaking time.

    DEF-164: if the model is already loaded, this must still touch the
    idle-unload timer. Otherwise a timer armed by the *previous*
    transcription keeps counting down unaffected by this new recording,
    and can fire mid-recording (_mlx_transcribing is False until
    transcribe_audio() actually runs) — evicting a model that's warm
    and in active use, forcing an un-hidden cold reload right after the
    user stops talking.
    """
    global _mlx_last_use
    with _mlx_lock:
        if _mlx_loaded.is_set():
            _mlx_last_use = time.time()
            _schedule_unload()
            return
    t = threading.Thread(target=_load_mlx_model, daemon=True)
    t.start()


# DEF-152: the glossary initial_prompt collapses prompt-fragile Whisper decoders —
# whisper-small degenerates to "!" on healthy audio, large-v3 stalls — while only the
# "turbo" class (large-v3-turbo, turbo-german-f16-q4) is prompt-robust. Gate the glossary
# so switching to a fragile model auto-disables biasing instead of breaking dictation.
# Prompts are model-sensitive (cf. P-detector-tuned-to-model, DEF-137).
def _model_supports_glossary(model_id: str) -> bool:
    """True only for prompt-robust MLX Whisper models (the turbo class, DEF-152)."""
    return "turbo" in (model_id or "").lower()


def init_local_stt(
    engine: str = "mlx",
    mlx_model: str = "mlx-community/whisper-small-mlx",
    model_dir: str = "",
    language: str = "",
    threads: int = 4,
    log_fn: Callable[[str], None] | None = None,
    initial_prompt: str = "",
    unload_secs: float = 300.0,
) -> None:
    """Initialize local STT engine.

    For MLX: stores config but does NOT load the model (lazy loading).
    For sherpa: loads immediately (small model, always needed).

    Args:
        engine: "mlx" (Metal GPU) or "sherpa" (CPU int8).
        mlx_model: HuggingFace repo ID for MLX model.
        model_dir: Directory containing sherpa-onnx model files.
        language: Language code (e.g. "en") or "" for auto-detect.
        threads: CPU thread count for sherpa backend.
        log_fn: Optional callable(str) for log messages. Defaults to print.
        initial_prompt: Rendered glossary string for MLX Whisper biasing (Phase 16).
        unload_secs: Idle seconds before the MLX model is unloaded from RAM.
    """
    global _recognizer, _mlx_model_id, _mlx_language, _log_fn, _mlx_initial_prompt
    global _mlx_unload_secs
    _log_fn = log_fn

    if engine == "mlx":
        _mlx_model_id = mlx_model
        _mlx_language = language
        # DEF-152 model-gate: only turbo-class models survive the glossary prompt.
        if initial_prompt and not _model_supports_glossary(mlx_model):
            _log(f"glossary DISABLED for prompt-fragile model {mlx_model} "
                 f"(DEF-152 — only turbo-class models are prompt-robust); biasing skipped")
            _mlx_initial_prompt = ""
        else:
            _mlx_initial_prompt = initial_prompt
        if unload_secs > 0:
            _mlx_unload_secs = unload_secs
        _log(f"Local STT configured (MLX Metal GPU, lazy load, "
             f"lang={'auto' if not language else language}, "
             f"glossary={'on' if _mlx_initial_prompt else 'off'}, "
             f"unload={int(_mlx_unload_secs)}s)")
    else:
        try:
            import sherpa_onnx
        except ImportError:
            _log("ERROR: sherpa-onnx is not installed. Install with: pip install sherpa-onnx>=1.0")
            _log("This is required for STT on Intel Macs (or as fallback on Apple Silicon).")
            sys.exit(1)

        encoder = os.path.join(model_dir, "small-encoder.int8.onnx")
        decoder = os.path.join(model_dir, "small-decoder.int8.onnx")
        tokens = os.path.join(model_dir, "small-tokens.txt")

        for f in [encoder, decoder, tokens]:
            if not os.path.exists(f):
                _log(f"FATAL: Whisper model file not found: {f}")
                _log("Download from: https://github.com/k2-fsa/sherpa-onnx/releases/tag/asr-models")
                sys.exit(1)

        kwargs = dict(
            encoder=encoder,
            decoder=decoder,
            tokens=tokens,
            num_threads=threads,
            task="transcribe",
        )
        if language:
            kwargs["language"] = language

        _recognizer = sherpa_onnx.OfflineRecognizer.from_whisper(**kwargs)
        _log(f"Local STT ready (sherpa-onnx CPU int8, lang={'auto' if not language else language})")


def transcribe_audio(
    audio_chunks: list[np.ndarray],
    engine: str = "mlx",
    mlx_model: str = "mlx-community/whisper-small-mlx",
    language: str = "",
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> str:
    """Transcribe recorded audio chunks using the configured engine.

    For MLX: loads model on first call if not already loaded (lazy).

    Args:
        audio_chunks: List of numpy int16 arrays from the mic stream.
        engine: "mlx" or "sherpa" — must match what init_local_stt used.
        mlx_model: HuggingFace repo ID (only used for "mlx" engine).
        language: Language code or "" for auto-detect.
        sample_rate: Sample rate of the audio (Hz).

    Returns:
        Transcribed text string (stripped), or "" if audio_chunks is empty.
    """
    global _mlx_last_use
    if not audio_chunks:
        return ""

    audio = np.concatenate(audio_chunks)
    samples = audio.astype(np.float32) / 32768.0

    if engine == "mlx":
        # DEF-081: hard 30s splits cut mid-sentence and leaked silence/partial
        # words into segment boundaries, producing repetition hallucinations
        # that `is_garbled()` then discarded. mlx_whisper.transcribe handles
        # long-form internally via 30s windows with proper context, so split
        # only above 4 minutes (safety net against memory ballooning for very
        # long recordings). Normal long dictations now use whole-file mode.
        split_after_secs = 240
        max_samples = split_after_secs * sample_rate
        segments = [samples[i:i + max_samples] for i in range(0, len(samples), max_samples)]
        if len(segments) > 1:
            _log(f"Long recording ({len(samples)/sample_rate:.1f}s), splitting into {len(segments)} segments for MLX")

        # Ensure model is loaded (blocks if preload hasn't finished yet).
        # B1: Always go through _mlx_loaded.wait() so the total block is
        # bounded by _LOAD_TIMEOUT, regardless of whether a background
        # preload thread is already running or we trigger the load here.
        if _mlx_unavailable:
            _log("ERROR: MLX unavailable (mlx-whisper not installed). Set "
                 "stt.local.engine to 'sherpa' on Intel Macs, or install "
                 "heyvox[apple-silicon].")
            return ""
        if not _mlx_loaded.is_set():
            threading.Thread(target=_load_mlx_model, daemon=True).start()
        # Poll for load, but bail the instant the load thread reports mlx-whisper
        # is unavailable — otherwise a missing import blocks the full
        # _LOAD_TIMEOUT (120s) on the first dictation before failing (DEF-175).
        _deadline = time.time() + _LOAD_TIMEOUT
        while not _mlx_loaded.is_set():
            if _mlx_unavailable:
                _log("ERROR: MLX unavailable (mlx-whisper not installed) — set "
                     "stt.local.engine to 'sherpa' or install heyvox[apple-silicon].")
                return ""
            if time.time() > _deadline:
                _log(f"ERROR: MLX model failed to load within {_LOAD_TIMEOUT}s")
                return ""
            time.sleep(0.1)

        try:
            import mlx_whisper
        except ImportError:
            _log("ERROR: mlx-whisper is not installed. Install with: pip install 'heyvox[apple-silicon]'")
            return ""
        kwargs = dict(path_or_hf_repo=_mlx_model_id or mlx_model)
        if _mlx_language or language:
            kwargs["language"] = _mlx_language or language
        # DEF-075: defensive Whisper config for interactive dictation.
        # condition_on_previous_text=True amplifies repetition loops across
        # segments; tighter compression_ratio + logprob thresholds let the
        # temperature fallback escape degenerate decoding sooner.
        kwargs["condition_on_previous_text"] = False
        kwargs["compression_ratio_threshold"] = 2.2
        kwargs["logprob_threshold"] = -0.8
        # Phase 16: bias the first decode window toward the learned glossary. Engine-gated
        # (sherpa-onnx has no initial_prompt equivalent, Pitfall 5) AND model-gated to the
        # prompt-robust turbo class (DEF-152 — fragile models collapse under the prompt;
        # init already clears _mlx_initial_prompt for them, this is belt-and-suspenders).
        # With condition_on_previous_text=False (above) this only affects the first 30s window.
        if _mlx_initial_prompt and engine == "mlx" and _model_supports_glossary(_mlx_model_id or mlx_model):
            kwargs["initial_prompt"] = _mlx_initial_prompt

        # Run transcription with timeout to prevent hangs.
        # Use context manager so the executor waits for completion on success,
        # ensuring MLX memory is released before we continue. On timeout,
        # abandon the thread (it will finish eventually) but force-unload
        # the model to reclaim memory.
        global _mlx_transcribing
        _timed_out = False
        parts = []
        with _mlx_lock:
            _mlx_transcribing = True
        def _run_mlx_transcribe(seg):
            _boost_transcribe_thread_priority()
            return mlx_whisper.transcribe(seg, **kwargs)

        try:
            for seg_idx, segment in enumerate(segments):
                with ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(_run_mlx_transcribe, segment)
                    result = future.result(timeout=_TRANSCRIBE_TIMEOUT)
                text = result["text"].strip()
                if text:
                    parts.append(text)
                if len(segments) > 1:
                    _log(f"Segment {seg_idx+1}/{len(segments)}: {len(text)} chars")
        except FuturesTimeout:
            _timed_out = True
            _log(f"ERROR: MLX transcription timed out after {_TRANSCRIBE_TIMEOUT}s")
            # Force-unload model to reclaim memory from the orphaned thread
            try:
                import mlx.core as mx
                mx.metal.clear_cache()
            except Exception:
                pass
            # Return whatever we transcribed so far
            return " ".join(parts)
        except Exception as e:
            _log(f"ERROR: MLX transcription failed: {e}")
            return " ".join(parts)
        finally:
            with _mlx_lock:
                _mlx_transcribing = False

        with _mlx_lock:
            _mlx_last_use = time.time()
            _schedule_unload()  # Reset the idle timer
        return " ".join(parts)
    else:
        # sherpa-onnx: split into <=30s segments (Whisper's input limit).
        # B2: Wrap the entire sherpa transcription loop in a thread with
        # _TRANSCRIBE_TIMEOUT to match the protection the MLX path has.
        def _sherpa_transcribe() -> str:
            _boost_transcribe_thread_priority()
            max_samples = 30 * sample_rate
            parts = []
            for i in range(0, len(samples), max_samples):
                chunk = samples[i:i + max_samples]
                stream = _recognizer.create_stream()
                stream.accept_waveform(sample_rate, chunk)
                _recognizer.decode_stream(stream)
                text = stream.result.text.strip()
                if text:
                    parts.append(text)
            return " ".join(parts)

        try:
            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(_sherpa_transcribe)
                return future.result(timeout=_TRANSCRIBE_TIMEOUT)
        except FuturesTimeout:
            _log(f"ERROR: Sherpa transcription timed out after {_TRANSCRIBE_TIMEOUT}s")
            return ""
        except Exception as e:
            _log(f"ERROR: Sherpa transcription failed: {e}")
            return ""


def model_loaded() -> bool:
    """Return True if the MLX model is currently loaded in memory.

    Kept for test_stress.py memory introspection.
    """
    return _mlx_loaded.is_set()


def memory_mb() -> float:
    """Return approximate memory used by the STT model (MB).

    Kept for test_stress.py memory introspection.
    """
    if _mlx_loaded.is_set():
        return 855.0  # Measured: whisper-small-mlx baseline
    return 0.0
