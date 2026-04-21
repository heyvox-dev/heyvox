"""
Echo suppression and acoustic echo cancellation for heyvox.

Provides three layers of echo protection for speaker mode (internal mic + speakers):

1. **TTS echo buffer** (ECHO-03): Ring buffer of recently spoken TTS text.
   After STT transcription, strips any fragments that match recent TTS output.

2. **Cross-process echo journal** (DEF-078): JSONL file shared by all TTS
   producers (MCP in-process `say()`, Herald worker spawned by Claude Code
   hooks). Lets the STT filter see Herald-initiated TTS, which lives in a
   different process than the one running STT.

3. **WebRTC AEC** (ECHO-05): Optional acoustic echo cancellation via livekit-rtc.
   Subtracts the known speaker signal from the mic input in real time.
   Requires `pip install heyvox[aec]` (livekit package).

Requirements: ECHO-03, ECHO-05, ECHO-06
"""

import json
import logging
import os
import tempfile
import time
import threading
from collections import deque
from typing import Optional

import numpy as np

from heyvox.constants import (
    TTS_ECHO_BUFFER_SECS,
    TTS_ECHO_JOURNAL,
    AEC_DEFAULT_DELAY_MS,
)

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ECHO-03 + DEF-078: TTS echo text buffer (in-memory + cross-process journal)
# ---------------------------------------------------------------------------

_echo_buffer_lock = threading.Lock()
_echo_buffer: deque[tuple[float, str]] = deque()  # (timestamp, spoken_text)

# In-memory guard so the same process doesn't repeatedly re-read its own
# just-written journal entries. Journal reads happen in filter_tts_echo().
_journal_read_lock = threading.Lock()


def _append_journal(ts: float, text: str) -> None:
    """Append a (timestamp, text) record to the cross-process echo journal.

    Swallows all errors — the in-memory buffer is the authoritative local
    source; the journal is a best-effort cross-process signal.

    DEF-078.
    """
    try:
        line = json.dumps({"ts": ts, "text": text}, ensure_ascii=False)
        # Open/append/close each time keeps the file small and lets other
        # processes read it consistently even if the writer crashes mid-write.
        # O_APPEND on POSIX guarantees atomic append of the short line.
        with open(TTS_ECHO_JOURNAL, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception as e:
        log.debug("echo journal append failed: %s", e)


def _read_journal_recent(cutoff: float) -> list[str]:
    """Read the journal and return lowercase texts whose timestamp >= cutoff.

    Also prunes the journal opportunistically when it contains > 256 lines
    to prevent unbounded growth. Prune is best-effort (atomic rewrite); failure
    to prune never blocks reads.

    DEF-078.
    """
    path = TTS_ECHO_JOURNAL
    if not os.path.exists(path):
        return []

    with _journal_read_lock:
        try:
            with open(path, "r", encoding="utf-8") as fh:
                lines = fh.readlines()
        except Exception as e:
            log.debug("echo journal read failed: %s", e)
            return []

        fresh: list[tuple[float, str]] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                ts = float(rec.get("ts", 0.0))
                text = str(rec.get("text", "")).strip().lower()
            except Exception:
                continue
            if ts >= cutoff and text:
                fresh.append((ts, text))

        # Opportunistic prune: rewrite journal with only fresh entries when
        # we detect bloat. Use atomic replace to avoid partial writes.
        if len(lines) > 256 and len(fresh) < len(lines):
            try:
                dir_ = os.path.dirname(path) or tempfile.gettempdir()
                fd, tmp_path = tempfile.mkstemp(
                    prefix=".heyvox-tts-echo.", suffix=".tmp", dir=dir_
                )
                try:
                    with os.fdopen(fd, "w", encoding="utf-8") as tmp:
                        for ts, text in fresh:
                            tmp.write(json.dumps({"ts": ts, "text": text},
                                                  ensure_ascii=False) + "\n")
                    os.replace(tmp_path, path)
                except Exception:
                    try:
                        os.unlink(tmp_path)
                    except FileNotFoundError:
                        pass
                    raise
            except Exception as e:
                log.debug("echo journal prune failed: %s", e)

        return [text for _, text in fresh]


def register_tts_text(text: str) -> None:
    """Register text that was spoken by TTS for echo filtering.

    Called from every TTS producer (MCP in-process say(), Herald worker)
    before each message is synthesized. The text is stored with a timestamp
    in both the in-memory buffer (fast) and the shared journal (cross-process).
    In-memory entries are evicted after TTS_ECHO_BUFFER_SECS; journal entries
    are pruned opportunistically on read.

    Args:
        text: The text being spoken by TTS.

    Requirements: ECHO-03, DEF-078
    """
    if not text:
        return
    cleaned = text.strip().lower()
    if not cleaned:
        return
    now = time.time()
    with _echo_buffer_lock:
        _echo_buffer.append((now, cleaned))
        cutoff = now - TTS_ECHO_BUFFER_SECS
        while _echo_buffer and _echo_buffer[0][0] < cutoff:
            _echo_buffer.popleft()
    _append_journal(now, cleaned)


def filter_tts_echo(transcription: str, aggressive: bool = False) -> str:
    """Remove fragments of recently spoken TTS from a transcription.

    Compares the STT output against recent TTS texts from two sources:
    (a) the in-memory buffer (local process), and
    (b) the cross-process journal at TTS_ECHO_JOURNAL (DEF-078).

    Uses word-level overlap detection: by default, if >60% of the transcription
    words appear in a recent TTS message, the transcription is stripped.

    When ``aggressive=True`` (passed by the recording state machine when TTS
    was playing at any point during the recording window), the threshold is
    lowered to 0.4 to catch partial bleed where the user spoke over the top of
    a TTS tail.

    Args:
        transcription: Raw STT transcription text.
        aggressive: Use 0.4 overlap threshold instead of 0.6.

    Returns:
        Cleaned transcription with echo fragments removed.
        Returns empty string if the entire transcription is an echo.

    Requirements: ECHO-03, DEF-078
    """
    if not transcription or not transcription.strip():
        return transcription

    now = time.time()
    cutoff = now - TTS_ECHO_BUFFER_SECS
    trans_lower = transcription.strip().lower()
    trans_words = trans_lower.split()

    if not trans_words:
        return transcription

    # Collect candidate TTS texts from both sources. Dedup on text body so we
    # don't pay the comparison cost twice for the common case where the same
    # text is in both the in-memory buffer and the journal (this process just
    # spoke it).
    with _echo_buffer_lock:
        mem_texts = [text for ts, text in _echo_buffer if ts >= cutoff]

    journal_texts = _read_journal_recent(cutoff)

    seen: set[str] = set()
    candidates: list[str] = []
    for text in mem_texts + journal_texts:
        if text and text not in seen:
            seen.add(text)
            candidates.append(text)

    overlap_threshold = 0.4 if aggressive else 0.6

    for tts_text in candidates:
        # Direct substring match (transcription is a fragment of TTS)
        if trans_lower in tts_text:
            return ""

        # Word overlap: check if most transcription words appear in TTS text.
        # Skip this check for very short transcriptions (< 3 words) — a single
        # word like "yes" matching TTS text containing "yes or no" is almost
        # certainly intentional user input, not an echo (C5 false-positive fix).
        if len(trans_words) >= 3:
            tts_words = set(tts_text.split())
            matching = sum(1 for w in trans_words if w in tts_words)
            overlap_ratio = matching / len(trans_words)

            if overlap_ratio > overlap_threshold:
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
            from livekit.rtc import AudioFrame  # noqa: F401
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


