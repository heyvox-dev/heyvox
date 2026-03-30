"""
Echo suppression and acoustic echo cancellation for heyvox.

Provides three layers of echo protection for speaker mode (internal mic + speakers):

1. **TTS echo buffer** (ECHO-03): Ring buffer of recently spoken TTS text.
   After STT transcription, strips any fragments that match recent TTS output.

2. **WebRTC AEC** (ECHO-05): Optional acoustic echo cancellation via livekit-rtc.
   Subtracts the known speaker signal from the mic input in real time.
   Requires `pip install heyvox[aec]` (livekit package).

Requirements: ECHO-03, ECHO-05, ECHO-06
"""

import logging
import time
import threading
from collections import deque
from typing import Optional

import numpy as np

from heyvox.constants import TTS_ECHO_BUFFER_SECS, AEC_DEFAULT_DELAY_MS

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ECHO-03: TTS echo text buffer
# ---------------------------------------------------------------------------

_echo_buffer_lock = threading.Lock()
_echo_buffer: deque[tuple[float, str]] = deque()  # (timestamp, spoken_text)


def register_tts_text(text: str) -> None:
    """Register text that was spoken by TTS for echo filtering.

    Called from tts.py before each message is synthesized. The text is stored
    with a timestamp so it can be evicted after TTS_ECHO_BUFFER_SECS.

    Args:
        text: The text being spoken by TTS.
    """
    now = time.time()
    with _echo_buffer_lock:
        _echo_buffer.append((now, text.strip().lower()))
        # Evict old entries
        cutoff = now - TTS_ECHO_BUFFER_SECS
        while _echo_buffer and _echo_buffer[0][0] < cutoff:
            _echo_buffer.popleft()


def filter_tts_echo(transcription: str) -> str:
    """Remove fragments of recently spoken TTS from a transcription.

    Compares the STT output against the echo buffer. If the transcription
    (or a substantial substring) matches a recent TTS message, strip it.

    Uses word-level overlap detection: if >60% of the transcription words
    appear in a recent TTS message (in order), the transcription is likely
    an echo and is stripped.

    Args:
        transcription: Raw STT transcription text.

    Returns:
        Cleaned transcription with echo fragments removed.
        Returns empty string if the entire transcription is an echo.

    Requirement: ECHO-03
    """
    if not transcription or not transcription.strip():
        return transcription

    now = time.time()
    cutoff = now - TTS_ECHO_BUFFER_SECS
    trans_lower = transcription.strip().lower()
    trans_words = trans_lower.split()

    if not trans_words:
        return transcription

    with _echo_buffer_lock:
        recent_texts = [
            text for ts, text in _echo_buffer if ts >= cutoff
        ]

    for tts_text in recent_texts:
        # Direct substring match (transcription is a fragment of TTS)
        if trans_lower in tts_text:
            return ""

        # Word overlap: check if most transcription words appear in TTS text
        tts_words = set(tts_text.split())
        matching = sum(1 for w in trans_words if w in tts_words)
        overlap_ratio = matching / len(trans_words)

        if overlap_ratio > 0.6:
            return ""

    return transcription


# ---------------------------------------------------------------------------
# ECHO-05: WebRTC AEC via livekit-rtc
# ---------------------------------------------------------------------------

_aec_lock = threading.Lock()
_apm = None  # livekit AudioProcessingModule instance
_aec_available: Optional[bool] = None  # None = not checked yet
_aec_delay_ms: int = AEC_DEFAULT_DELAY_MS


def init_aec(delay_ms: int = AEC_DEFAULT_DELAY_MS) -> bool:
    """Initialize the WebRTC AEC via livekit-rtc.

    Returns True if AEC is available and initialized, False otherwise.
    Safe to call multiple times — only initializes once.

    Args:
        delay_ms: Estimated speaker-to-mic delay in milliseconds.

    Requirement: ECHO-05
    """
    global _apm, _aec_available, _aec_delay_ms

    with _aec_lock:
        if _aec_available is not None:
            return _aec_available

        _aec_delay_ms = delay_ms

        try:
            from livekit.rtc import AudioFrame
            from livekit.rtc.apm import AudioProcessingModule

            _apm = AudioProcessingModule(
                echo_cancellation=True,
                noise_suppression=True,
                high_pass_filter=True,
                auto_gain_control=True,
            )
            _aec_available = True
            log.info("WebRTC AEC initialized (delay=%dms)", delay_ms)
            return True
        except ImportError:
            _aec_available = False
            log.info("WebRTC AEC not available (install livekit: pip install heyvox[aec])")
            return False
        except Exception as e:
            _aec_available = False
            log.warning("WebRTC AEC init failed: %s", e)
            return False


def is_aec_available() -> bool:
    """Check if AEC has been initialized and is ready."""
    return _aec_available is True


def process_mic_frame(audio: np.ndarray, sample_rate: int = 16000) -> np.ndarray:
    """Process a microphone audio frame through the AEC.

    Removes echo of recently played speaker audio from the mic signal.
    Returns the original audio unchanged if AEC is not available.

    Args:
        audio: Mic audio as int16 numpy array.
        sample_rate: Sample rate in Hz.

    Returns:
        Cleaned audio as int16 numpy array (same shape).

    Requirement: ECHO-05
    """
    if _apm is None or not _aec_available:
        return audio

    try:
        from livekit.rtc import AudioFrame

        # APM requires 10ms frames
        samples_per_10ms = sample_rate // 100
        total_samples = len(audio)
        result_chunks = []

        for offset in range(0, total_samples, samples_per_10ms):
            chunk = audio[offset:offset + samples_per_10ms]
            if len(chunk) < samples_per_10ms:
                # Pad the last chunk if needed
                chunk = np.pad(chunk, (0, samples_per_10ms - len(chunk)))

            frame = AudioFrame(
                data=chunk.astype(np.int16).tobytes(),
                sample_rate=sample_rate,
                num_channels=1,
                samples_per_channel=samples_per_10ms,
            )

            _apm.set_stream_delay_ms(_aec_delay_ms)
            _apm.process_stream(frame)

            processed = np.frombuffer(frame.data, dtype=np.int16)
            result_chunks.append(processed)

        result = np.concatenate(result_chunks)[:total_samples]
        return result

    except Exception as e:
        log.debug("AEC process_mic_frame error: %s", e)
        return audio


def process_speaker_frame(audio: np.ndarray, sample_rate: int = 24000) -> None:
    """Feed a speaker audio frame to the AEC as the reference signal.

    Must be called for every audio chunk played through the speakers.
    The AEC uses this to know what to subtract from the mic input.

    Args:
        audio: Speaker audio as float32 or int16 numpy array.
        sample_rate: Sample rate in Hz (Kokoro TTS uses 24000).

    Requirement: ECHO-05
    """
    if _apm is None or not _aec_available:
        return

    try:
        from livekit.rtc import AudioFrame

        # Convert float32 TTS audio to int16
        if audio.dtype == np.float32:
            audio_i16 = (audio * 32767).clip(-32768, 32767).astype(np.int16)
        else:
            audio_i16 = audio.astype(np.int16)

        # Resample to mic sample rate (16kHz) if needed — AEC needs matching rates
        if sample_rate != 16000:
            # Simple decimation for 24000->16000 (ratio 2:3)
            # For production, use scipy.signal.resample_poly
            target_len = int(len(audio_i16) * 16000 / sample_rate)
            indices = np.linspace(0, len(audio_i16) - 1, target_len).astype(int)
            audio_i16 = audio_i16[indices]
            sample_rate = 16000

        samples_per_10ms = sample_rate // 100

        for offset in range(0, len(audio_i16), samples_per_10ms):
            chunk = audio_i16[offset:offset + samples_per_10ms]
            if len(chunk) < samples_per_10ms:
                chunk = np.pad(chunk, (0, samples_per_10ms - len(chunk)))

            frame = AudioFrame(
                data=chunk.tobytes(),
                sample_rate=sample_rate,
                num_channels=1,
                samples_per_channel=samples_per_10ms,
            )

            _apm.process_reverse_stream(frame)

    except Exception as e:
        log.debug("AEC process_speaker_frame error: %s", e)
