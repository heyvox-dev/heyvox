"""
Speech-to-text engine management for vox.

Supports two backends:
- "mlx": MLX Whisper (Metal GPU, Apple Silicon only) — fast, preferred
- "sherpa": sherpa-onnx Whisper (CPU, int8 quantized) — universal fallback

Lazy imports keep the module importable without either library installed.
"""

import os
import sys
import numpy as np

from vox.constants import DEFAULT_SAMPLE_RATE


# Global sherpa recognizer (initialized once, reused across calls)
_recognizer = None


def init_local_stt(
    engine: str = "mlx",
    mlx_model: str = "mlx-community/whisper-small-mlx",
    model_dir: str = "",
    language: str = "",
    threads: int = 4,
    log_fn=None,
) -> None:
    """Initialize local STT engine.

    Must be called before transcribe_audio when using local backend.

    Args:
        engine: "mlx" (Metal GPU) or "sherpa" (CPU int8).
        mlx_model: HuggingFace repo ID for MLX model.
        model_dir: Directory containing sherpa-onnx model files.
        language: Language code (e.g. "en") or "" for auto-detect.
        threads: CPU thread count for sherpa backend.
        log_fn: Optional callable(str) for log messages. Defaults to print.
    """
    global _recognizer

    def _log(msg):
        if log_fn:
            log_fn(msg)
        else:
            print(msg, flush=True)

    if engine == "mlx":
        import mlx_whisper  # lazy: not available on Intel Macs
        # Warm up: first call downloads/loads weights into GPU memory
        _log(f"Loading MLX whisper model ({mlx_model})...")
        dummy = np.zeros(16000, dtype=np.float32)
        mlx_whisper.transcribe(dummy, path_or_hf_repo=mlx_model)
        _log(f"Local STT ready (MLX Metal GPU, lang={'auto' if not language else language})")
    else:
        import sherpa_onnx  # lazy: large binary dependency

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
    audio_chunks: list,
    engine: str = "mlx",
    mlx_model: str = "mlx-community/whisper-small-mlx",
    language: str = "",
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> str:
    """Transcribe recorded audio chunks using the configured engine.

    Args:
        audio_chunks: List of numpy int16 arrays from the mic stream.
        engine: "mlx" or "sherpa" — must match what init_local_stt used.
        mlx_model: HuggingFace repo ID (only used for "mlx" engine).
        language: Language code or "" for auto-detect.
        sample_rate: Sample rate of the audio (Hz).

    Returns:
        Transcribed text string (stripped), or "" if audio_chunks is empty.
    """
    if not audio_chunks:
        return ""

    audio = np.concatenate(audio_chunks)
    samples = audio.astype(np.float32) / 32768.0

    if engine == "mlx":
        import mlx_whisper  # lazy import
        kwargs = dict(path_or_hf_repo=mlx_model)
        if language:
            kwargs["language"] = language
        result = mlx_whisper.transcribe(samples, **kwargs)
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
