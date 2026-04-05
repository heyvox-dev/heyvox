"""
Speech-to-text engine management for heyvox.

Supports two backends:
- "mlx": MLX Whisper (Metal GPU, Apple Silicon only) — fast, preferred
- "sherpa": sherpa-onnx Whisper (CPU, int8 quantized) — universal fallback

MLX model is lazy-loaded on first use and unloaded after idle timeout
to free ~855MB of GPU/unified memory when not dictating.
"""

import os
import sys
import threading
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout

import numpy as np

# Timeout for model loading and transcription calls
_LOAD_TIMEOUT = 60  # seconds — cold load can be slow
_TRANSCRIBE_TIMEOUT = 30  # seconds — no transcription should take this long

from heyvox.constants import DEFAULT_SAMPLE_RATE


# Global sherpa recognizer (initialized once, reused across calls)
_recognizer = None

# MLX lazy-load state
_mlx_model_id: str = ""
_mlx_language: str = ""
_mlx_loaded = threading.Event()  # Set when model is ready
_mlx_lock = threading.Lock()
_mlx_last_use: float = 0.0
_mlx_unload_secs: float = 120.0  # 2 minutes idle → unload
_mlx_unloader: threading.Timer | None = None
_log_fn: Callable[[str], None] | None = None


def _log(msg: str) -> None:
    if _log_fn:
        _log_fn(msg)
    else:
        print(msg, flush=True)


def _load_mlx_model() -> None:
    """Load MLX Whisper model into GPU memory (blocking)."""
    global _mlx_last_use
    if _mlx_loaded.is_set():
        return
    with _mlx_lock:
        if _mlx_loaded.is_set():
            return  # Another thread loaded while we waited
        import mlx_whisper
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
        idle = time.time() - _mlx_last_use
        if idle < _mlx_unload_secs:
            # Not idle long enough — reschedule
            _schedule_unload()
            return
        import mlx.core as mx
        mx.metal.clear_cache()
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
    the user's speaking time. No-op if model is already loaded.
    """
    if _mlx_loaded.is_set():
        return
    t = threading.Thread(target=_load_mlx_model, daemon=True)
    t.start()


def init_local_stt(
    engine: str = "mlx",
    mlx_model: str = "mlx-community/whisper-small-mlx",
    model_dir: str = "",
    language: str = "",
    threads: int = 4,
    log_fn: Callable[[str], None] | None = None,
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
    """
    global _recognizer, _mlx_model_id, _mlx_language, _log_fn
    _log_fn = log_fn

    if engine == "mlx":
        _mlx_model_id = mlx_model
        _mlx_language = language
        _log(f"Local STT configured (MLX Metal GPU, lazy load, lang={'auto' if not language else language})")
    else:
        import sherpa_onnx

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
        # Ensure model is loaded (blocks if preload hasn't finished yet)
        if not _mlx_loaded.is_set():
            _load_mlx_model()
        else:
            _mlx_loaded.wait(timeout=_LOAD_TIMEOUT)

        if not _mlx_loaded.is_set():
            _log("ERROR: MLX model failed to load within timeout")
            return ""

        import mlx_whisper
        kwargs = dict(path_or_hf_repo=_mlx_model_id or mlx_model)
        if _mlx_language or language:
            kwargs["language"] = _mlx_language or language

        # Run transcription with timeout to prevent hangs
        try:
            with ThreadPoolExecutor(max_workers=1) as pool:
                future = pool.submit(mlx_whisper.transcribe, samples, **kwargs)
                result = future.result(timeout=_TRANSCRIBE_TIMEOUT)
        except FuturesTimeout:
            _log(f"ERROR: MLX transcription timed out after {_TRANSCRIBE_TIMEOUT}s")
            return ""
        except Exception as e:
            _log(f"ERROR: MLX transcription failed: {e}")
            return ""

        _mlx_last_use = time.time()
        with _mlx_lock:
            _schedule_unload()  # Reset the idle timer
        return result["text"].strip()
    else:
        # sherpa-onnx: split into <=30s segments (Whisper's input limit)
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


def model_loaded() -> bool:
    """Return True if the MLX model is currently loaded in memory."""
    return _mlx_loaded.is_set()


def memory_mb() -> float:
    """Return approximate memory used by the STT model (MB)."""
    if _mlx_loaded.is_set():
        return 855.0  # Measured: whisper-small-mlx baseline
    return 0.0
