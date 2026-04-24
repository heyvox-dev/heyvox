"""Herald Worker — TTS extraction and WAV generation.

Port of heyvox/herald/lib/worker.sh to Python.

Handles the full pipeline from raw Claude response text to WAV files
enqueued in the herald queue directory:
  1. Extract <tts>...</tts> blocks from response text
  2. Apply verbosity filtering (skip/short/full)
  3. Detect mood → voice selection
  4. Detect language → voice override
  5. Multi-agent voice routing
  6. Send generation request to Kokoro daemon via AF_UNIX socket
  7. Early-enqueue part 1 as soon as it appears (multi-part streaming)
  8. Piper TTS fallback if Kokoro unavailable
  9. Write .workspace sidecar next to each WAV

Requirements: HERALD-01 (producer side), HERALD-02 (Piper normalization)
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import wave
from pathlib import Path

from heyvox.constants import (
    HERALD_DEBUG_LOG,
    HERALD_QUEUE_DIR,
    HERALD_ORCH_PID,
    HERALD_CLAIM_DIR,
    KOKORO_DAEMON_PID,
    KOKORO_DAEMON_SOCK,
    QWEN_DAEMON_PID,
    QWEN_DAEMON_SOCK,
    VERBOSITY_FILE,
    HERALD_GENERATING_WAV_PREFIX,
)

log = logging.getLogger(__name__)

# File-based log handler — matches herald_log() in config.sh
# DEF-064: force INFO level so voice-selection lines reach the log file.
# Without this, root logger defaults to WARNING, silencing forensic "Generating TTS" entries.
_file_handler = logging.FileHandler(HERALD_DEBUG_LOG, delay=True)
_file_handler.setFormatter(logging.Formatter("[%(asctime)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
_herald_logger = logging.getLogger("heyvox.herald")
_herald_logger.addHandler(_file_handler)
_herald_logger.setLevel(logging.INFO)

# ---------------------------------------------------------------------------
# Voice constants (from worker.sh)
# ---------------------------------------------------------------------------

MOOD_VOICES: dict[str, str] = {
    "neutral": "af_sarah",
    "cheerful": "af_heart",
    "alert": "af_nova",
    "thoughtful": "af_sky",
}

AGENT_VOICE_POOL = [
    "af_alloy", "af_bella", "af_jessica", "af_kore", "af_nicole",
    "af_river", "am_adam", "am_eric", "am_liam", "am_puck",
]

DEFAULT_VOICE = "af_sarah"
DEFAULT_LANG = "en-us"
DEFAULT_SPEED = 1.2

# Qwen3-TTS voice roster (used for non-Kokoro languages like German).
# Voice identity is cross-lingual — same character speaks all Qwen3 langs.
QWEN_MOOD_VOICES: dict[str, str] = {
    "neutral": "Serena",
    "cheerful": "Vivian",
    "alert": "Davis",
    "thoughtful": "Aria",
}

QWEN_DEFAULT_VOICE = "Serena"

# Languages routed to the Qwen3 daemon (lazy-loaded). Kokoro covers
# en-us, en-gb, es, fr-fr, it, pt, ja, cmn, hi — everything else goes
# here. Start with German since that's what we've validated; extend
# when we confirm quality for other langs.
QWEN_LANGS: frozenset[str] = frozenset({"de"})


def _engine_for_lang(lang: str) -> str:
    """Return 'qwen' or 'kokoro' for the given language code."""
    return "qwen" if lang in QWEN_LANGS else "kokoro"

# ---------------------------------------------------------------------------
# Standalone utility functions
# ---------------------------------------------------------------------------


def normalize_wav_in_place(path: str) -> None:
    """RMS-based WAV loudness normalization (in-place).

    Thin wrapper around heyvox.audio.normalize.normalize_wav_int16.
    Used by the Piper TTS fallback path (HERALD-02).
    """
    from heyvox.audio.normalize import normalize_wav_int16

    try:
        with wave.open(path, "rb") as w:
            params = w.getparams()
            raw_frames = w.readframes(params.nframes)

        normalized = normalize_wav_int16(raw_frames)
        if normalized is not raw_frames:
            with wave.open(path, "wb") as w:
                w.setparams(params)
                w.writeframes(normalized)
    except Exception as exc:
        log.warning("normalize_wav_in_place failed for %s: %s", path, exc)


# ---------------------------------------------------------------------------
# Module-level helper functions (exported for tests)
# ---------------------------------------------------------------------------


def detect_mood(text: str) -> str:
    """Detect the emotional mood of a TTS text fragment.

    Ports detect_mood() from worker.sh lines 73-95.
    Returns one of: 'alert', 'cheerful', 'thoughtful', 'neutral'.
    """
    t = text.lower()
    alert_words = [
        "error", "fail", "broke", "crash", "warning", "careful",
        "danger", "critical", "urgent", "problem", "bug",
    ]
    cheerful_words = [
        "done", "success", "passed", "complete", "fixed", "great",
        "perfect", "working", "deployed", "shipped", "merged",
        "awesome", "congrats", "excellent",
    ]
    thoughtful_words = [
        "should we", "want me to", "would you", "what do you",
        "how about", "shall i", "let me know", "hmm", "consider", "interesting",
    ]
    if any(w in t for w in alert_words):
        return "alert"
    if any(w in t for w in cheerful_words):
        return "cheerful"
    if any(w in t for w in thoughtful_words):
        return "thoughtful"
    return "neutral"


def detect_language(text: str) -> tuple[str, str | None]:
    """Detect language and return (lang_code, voice_override_or_None).

    Kokoro supports English, French, Italian, Spanish, Portuguese,
    Japanese, Mandarin, Hindi. For those we return the matching voice.
    German is explicitly detected but routed to English — Kokoro has
    no German voice and the Piper Thorsten fallback sounds unnatural.
    All regexes use tight multi-word phrases or unambiguous single
    tokens only (DEF-064: earlier `c.est` / `sono` patterns flipped
    English text to accented voices).
    """
    # CJK detection — character-range based, zero false-positive risk.
    # Japanese check first: JP text mixes kana + kanji, and kanji alone
    # would misroute to Mandarin. Kana presence is the Japanese tell.
    if re.search(r"[\u3040-\u309f\u30a0-\u30ff]", text):
        return "ja", "jf_alpha"
    if re.search(r"[\u4e00-\u9fff]", text):
        return "cmn", "zf_xiaoxiao"

    # German — umlauts + anchored ich/wir phrases. Routes to Qwen3-TTS
    # daemon (lazy-loaded). Voice is picked by mood in _select_voice.
    if re.search(r"[äöüÄÖÜß]", text) or re.search(
        r"\b(ich bin|ich habe|wir haben|das ist|was ist|guten (morgen|tag|abend)|"
        r"danke sch[öo]n|bitte sch[öo]n|auf wiedersehen|entschuldigung)\b",
        text, re.IGNORECASE,
    ):
        return "de", None

    # French — anchored phrases only. No `c.est` wildcard (matched "chest"/"crest").
    if re.search(
        r"\b(je suis|nous sommes|nous avons|vous avez|c'est|il y a|"
        r"s'il vous pla[îi]t|merci beaucoup|bonjour monsieur)\b",
        text, re.IGNORECASE,
    ):
        return "fr-fr", "ff_siwis"

    # Italian — multi-word phrases; single `sono` is a real English word.
    if re.search(
        r"\b(buongiorno|buonasera|buonanotte|grazie mille|prego signore|"
        r"mi scusi|non capisco|parla italiano|come stai|arrivederci)\b",
        text, re.IGNORECASE,
    ):
        return "it", "if_sara"

    # Spanish — greetings and unambiguous phrases.
    if re.search(
        r"\b(hola|buenos d[íi]as|buenas (tardes|noches)|por favor|"
        r"muchas gracias|lo siento|c[óo]mo est[áa]s|hasta luego)\b",
        text, re.IGNORECASE,
    ):
        return "es", "ef_dora"

    # Portuguese — greetings; "obrigado/a" is the cleanest tell.
    if re.search(
        r"\b(ol[áa]|bom dia|boa (tarde|noite)|obrigad[oa]|"
        r"por favor|at[ée] logo|como vai)\b",
        text, re.IGNORECASE,
    ):
        return "pt", "pf_dora"

    return "en-us", None


# ---------------------------------------------------------------------------
# Orchestrator auto-start
# ---------------------------------------------------------------------------

def _ensure_orchestrator() -> None:
    """Start the Herald orchestrator if it isn't already running.

    Called after each successful WAV generation so the queue is always drained.
    Uses flock to prevent multiple simultaneous spawns (TOCTOU-safe).
    """
    import fcntl

    lock_path = HERALD_ORCH_PID + ".lock"
    try:
        lock_fd = os.open(lock_path, os.O_WRONLY | os.O_CREAT, 0o644)
    except OSError:
        return  # Can't open lock file — skip

    try:
        # Non-blocking exclusive lock — if another worker is already spawning, bail
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(lock_fd)
        return  # Another worker is handling the spawn

    try:
        # Re-check PID while holding the lock (no TOCTOU race now)
        if os.path.exists(HERALD_ORCH_PID):
            try:
                pid = int(open(HERALD_ORCH_PID).read().strip())
                os.kill(pid, 0)
                return  # Already running
            except (OSError, ValueError):
                pass  # Stale PID file — start a new one

        subprocess.Popen(
            [sys.executable, "-m", "heyvox.herald.cli", "orchestrator"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        log.info("Auto-started Herald orchestrator")
    except Exception as exc:
        log.warning("Failed to auto-start orchestrator: %s", exc)
    finally:
        # Release lock — the orchestrator's own flock in run() takes over
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


# ---------------------------------------------------------------------------
# HeraldWorker class
# ---------------------------------------------------------------------------


class HeraldWorker:
    """TTS extraction and WAV generation for Herald.

    Ports heyvox/herald/lib/worker.sh to Python.
    Stateless per call — multiple instances can run concurrently.
    """

    def __init__(self) -> None:
        # Workspace name from environment (D-04: only env var, no DB query).
        # HEYVOX_WORKSPACE is the generic env var; CONDUCTOR_WORKSPACE_NAME
        # is kept for backward compatibility with Conductor hook environments.
        self._workspace: str = (
            os.environ.get("HEYVOX_WORKSPACE", "")
            or os.environ.get("CONDUCTOR_WORKSPACE_NAME", "")
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def process_response(self, raw_text: str, hook_type: str = "response") -> bool:
        """Main entry point: extract TTS block from raw text and generate WAV.

        Returns True on success or intentional skip, False on generation failure.
        """
        # Extract <tts>...</tts> blocks (multiline, last match wins)
        texts = self._extract_tts_blocks(raw_text)
        if not texts:
            log.debug("No TTS blocks found in response")
            return True

        speech = texts[-1].strip()

        # Validate content
        if not speech or speech == "SKIP" or len(speech) < 5:
            log.debug("TTS block empty or SKIP — skipping")
            return True

        # Apply mode filter (notify: truncate to first sentence ≤ 60 chars)
        mode = self._read_mode()
        if mode == "notify":
            first = re.split(r"[.!?]", speech)[0].strip()
            speech = (first[:57] + "...") if len(first) > 60 else first

        # Apply verbosity filtering
        verbosity = self._read_verbosity()
        if verbosity == "skip":
            log.debug("Verbosity=skip — not generating TTS")
            return True
        elif verbosity == "short":
            m = re.search(r"[.!?]", speech)
            if m:
                speech = speech[: m.end()].strip()
            else:
                speech = speech[:100]
        # 'full' and 'summary' (legacy) both play everything

        # Claim dedup — prevent watcher from generating a duplicate
        speech_hash = hashlib.md5(speech.encode()).hexdigest()[:16]
        os.makedirs(HERALD_CLAIM_DIR, exist_ok=True)
        claim_file = os.path.join(HERALD_CLAIM_DIR, speech_hash)
        try:
            fd = os.open(claim_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, b"hook")
            os.close(fd)
        except FileExistsError:
            log.debug("TTS already claimed by watcher (%s) — skipping", speech_hash)
            return True

        # Voice/language/mood selection
        mood = self._detect_mood(speech)
        lang, lang_voice = self._detect_language(speech)
        voice = self._select_voice(mood, lang, lang_voice)

        # DEF-078: Register the finalized speech text with the cross-process
        # echo buffer BEFORE generation. The heyvox daemon's STT filter reads
        # the shared journal to strip speaker-to-mic bleed from transcriptions.
        # This worker runs in its own short-lived process spawned by a Claude
        # Code hook, so the in-process buffer wouldn't reach the STT filter.
        try:
            from heyvox.audio.echo import register_tts_text
            register_tts_text(speech)
        except Exception as e:
            log.debug("DEF-078: register_tts_text failed (non-fatal): %s", e)

        # Generate WAV
        log.info("Generating TTS: mood=%s lang=%s voice=%s len=%d", mood, lang, voice, len(speech))
        ok = self._generate(speech, voice, lang, DEFAULT_SPEED)
        if ok:
            _ensure_orchestrator()
        return ok

    # ------------------------------------------------------------------
    # Private: text extraction
    # ------------------------------------------------------------------

    def _extract_tts_blocks(self, text: str) -> list[str]:
        """Extract all <tts>...</tts> blocks (DOTALL, multiline).

        Ports from worker.sh lines 38-42 (multiline match with inline fallback).
        """
        # Try anchored form first (at start of line, as Claude often emits)
        matches = re.findall(r"^<tts>(.*?)</tts>", text, re.DOTALL | re.MULTILINE)
        if not matches:
            # Fallback: anywhere in text (handles same-line and inline blocks)
            matches = re.findall(r"<tts>(.*?)</tts>", text, re.DOTALL)
        else:
            # Even if anchored matched some, also get any non-anchored ones
            # to handle mixed content (like tests with both anchored and inline)
            all_matches = re.findall(r"<tts>(.*?)</tts>", text, re.DOTALL)
            if len(all_matches) > len(matches):
                matches = all_matches
        return matches

    # ------------------------------------------------------------------
    # Private: mood/language/voice (thin wrappers for testability)
    # ------------------------------------------------------------------

    def _detect_mood(self, text: str) -> str:
        return detect_mood(text)

    def _detect_language(self, text: str) -> tuple[str, str | None]:
        return detect_language(text)

    def _select_voice(self, mood: str, lang: str, lang_voice: str | None) -> str:
        """Select TTS voice name from mood + language + agent context.

        Ports voice selection logic from worker.sh lines 86-131.
        Priority: agent env var > language override > mood.
        Different voice roster per engine: Kokoro (af_*/am_*) vs Qwen3
        (Serena/Ethan/etc.) — picked based on lang.
        """
        engine = _engine_for_lang(lang)

        # Mood → default voice for this engine
        if engine == "qwen":
            voice = QWEN_MOOD_VOICES.get(mood, QWEN_DEFAULT_VOICE)
        else:
            voice = MOOD_VOICES.get(mood, DEFAULT_VOICE)

        # Language override (higher priority than mood)
        if lang_voice and lang != "en-us":
            voice = lang_voice

        # Multi-agent voice routing (highest priority) — Kokoro only.
        # Qwen3's 9-voice roster isn't large enough for agent hashing,
        # and cross-lingual voice identity already gives agents stable
        # character across languages.
        if engine == "kokoro":
            agent_name = (
                os.environ.get("HEYVOX_AGENT", "")
                or os.environ.get("CONDUCTOR_AGENT", "")
                or os.environ.get("CLAUDE_AGENT_NAME", "")
            )
            if agent_name:
                idx = int(hashlib.md5(agent_name.encode()).hexdigest(), 16) % len(AGENT_VOICE_POOL)
                voice = AGENT_VOICE_POOL[idx]

        # Explicit voice override from environment
        if engine == "qwen":
            env_voice = os.environ.get("QWEN_VOICE", "")
        else:
            env_voice = os.environ.get("KOKORO_VOICE", "")
        if env_voice:
            voice = env_voice

        return voice

    # ------------------------------------------------------------------
    # Private: generation dispatch
    # ------------------------------------------------------------------

    def _generate(self, text: str, voice: str, lang: str, speed: float) -> bool:
        """Generate TTS WAV and enqueue it.

        Route by language: QWEN_LANGS (e.g. German) → Qwen3 daemon,
        everything else → Kokoro daemon. Both fall back to Piper if
        their daemon is unreachable.
        """
        timestamp = str(int(time.time() * 1000))
        temp_wav = f"{HERALD_GENERATING_WAV_PREFIX}{os.getpid()}.wav"

        engine = _engine_for_lang(lang)
        if engine == "qwen":
            if self._generate_qwen(text, voice, lang, speed, temp_wav, timestamp):
                return True
            log.warning("Qwen3 daemon unavailable — falling back to Piper (lang=%s)", lang)
            return self._generate_piper(text, voice, temp_wav, timestamp, lang)

        if self._generate_kokoro(text, voice, lang, speed, temp_wav, timestamp):
            return True

        log.warning("Kokoro daemon unavailable — falling back to Piper (lang=%s)", lang)
        return self._generate_piper(text, voice, temp_wav, timestamp, lang)

    def _generate_qwen(
        self,
        text: str,
        voice: str,
        lang: str,
        speed: float,
        temp_wav: str,
        timestamp: str,
    ) -> bool:
        """Generate WAV via Qwen3 daemon. Mirrors _generate_kokoro protocol."""
        if not self._ensure_qwen_daemon():
            return False

        request = {
            "text": text,
            "voice": voice,
            "lang": lang,
            "speed": speed,
            "output": temp_wav,
        }

        watcher_stop = threading.Event()
        watcher_thread = threading.Thread(
            target=self._early_enqueue_watcher,
            args=(temp_wav, timestamp, watcher_stop),
            daemon=True,
        )
        watcher_thread.start()

        try:
            resp = self._qwen_request(request, timeout=60.0)
        except Exception as exc:
            log.warning("Qwen3 daemon request failed: %s", exc)
            watcher_stop.set()
            watcher_thread.join(timeout=2.0)
            return False
        finally:
            watcher_stop.set()
            watcher_thread.join(timeout=2.0)

        if not resp.get("ok"):
            log.warning("Qwen3 daemon returned error: %s", resp.get("error"))
            return False

        parts = resp.get("parts", 1)
        log.info("Qwen3 generated %d part(s) in %.2fs", parts, resp.get("duration", 0))

        base = temp_wav.replace(".wav", "")
        for part_num in range(2, parts + 1):
            part_path = f"{base}.part{part_num}.wav"
            if os.path.isfile(part_path):
                wav_name = f"{timestamp}-{part_num:02d}.wav"
                dest = os.path.join(HERALD_QUEUE_DIR, wav_name)
                try:
                    shutil.move(part_path, dest)
                    self._write_workspace_sidecar(dest)
                except OSError as exc:
                    log.warning("Failed to enqueue part %d: %s", part_num, exc)

        # DEF-073 parity: rescue part 1 if the watcher lost the race.
        part1_name = f"{timestamp}-01.wav"
        part1_dest = os.path.join(HERALD_QUEUE_DIR, part1_name)
        if not os.path.isfile(part1_dest) and os.path.isfile(temp_wav):
            try:
                os.makedirs(HERALD_QUEUE_DIR, exist_ok=True)
                shutil.move(temp_wav, part1_dest)
                self._write_workspace_sidecar(part1_dest)
                log.info("rescued part 1 via main-thread fallback -> %s", part1_name)
            except OSError as exc:
                log.warning("Failed to rescue part 1: %s", exc)

        parts_file = os.path.join(HERALD_QUEUE_DIR, f"{timestamp}.parts")
        try:
            os.unlink(parts_file)
        except FileNotFoundError:
            pass

        try:
            os.unlink(temp_wav)
        except FileNotFoundError:
            pass

        return True

    def _generate_kokoro(
        self,
        text: str,
        voice: str,
        lang: str,
        speed: float,
        temp_wav: str,
        timestamp: str,
    ) -> bool:
        """Generate WAV via Kokoro daemon (AF_UNIX socket).

        Ports the daemon communication block from worker.sh lines 171-243.
        """
        if not self._ensure_kokoro_daemon():
            return False

        # Build request
        request = {
            "text": text,
            "voice": voice,
            "lang": lang,
            "speed": speed,
            "output": temp_wav,
        }

        # Start early-enqueue watcher for part 1
        watcher_stop = threading.Event()
        watcher_thread = threading.Thread(
            target=self._early_enqueue_watcher,
            args=(temp_wav, timestamp, watcher_stop),
            daemon=True,
        )
        watcher_thread.start()

        try:
            resp = self._kokoro_request(request, timeout=30.0)
        except Exception as exc:
            log.warning("Kokoro daemon request failed: %s", exc)
            watcher_stop.set()
            watcher_thread.join(timeout=2.0)
            return False
        finally:
            watcher_stop.set()
            watcher_thread.join(timeout=2.0)

        if not resp.get("ok"):
            log.warning("Kokoro daemon returned error: %s", resp.get("error"))
            return False

        parts = resp.get("parts", 1)
        log.info("Kokoro generated %d part(s) in %.2fs", parts, resp.get("duration", 0))

        # Enqueue remaining parts (part 2+)
        base = temp_wav.replace(".wav", "")
        for part_num in range(2, parts + 1):
            part_path = f"{base}.part{part_num}.wav"
            if os.path.isfile(part_path):
                wav_name = f"{timestamp}-{part_num:02d}.wav"
                dest = os.path.join(HERALD_QUEUE_DIR, wav_name)
                try:
                    shutil.move(part_path, dest)
                    self._write_workspace_sidecar(dest)
                    log.debug("Enqueued part %d -> %s", part_num, wav_name)
                except OSError as exc:
                    log.warning("Failed to enqueue part %d: %s", part_num, exc)

        # DEF-073: Safety net for part 1.
        # The _early_enqueue_watcher polls at 100ms intervals to copy part 1
        # from temp_wav -> queue. For short generations (~0.26s, one part),
        # the watcher's stop signal can fire before a single poll has seen
        # temp_wav, and its final-sweep path handles parts 2+ only. Result:
        # part 1 is left at temp_wav, the unlink below deletes it, and the
        # message is silently dropped with no "ORCH: playing" ever logged.
        part1_name = f"{timestamp}-01.wav"
        part1_dest = os.path.join(HERALD_QUEUE_DIR, part1_name)
        if not os.path.isfile(part1_dest) and os.path.isfile(temp_wav):
            try:
                os.makedirs(HERALD_QUEUE_DIR, exist_ok=True)
                shutil.move(temp_wav, part1_dest)
                self._write_workspace_sidecar(part1_dest)
                log.info("DEF-073: rescued part 1 via main-thread fallback -> %s", part1_name)
            except OSError as exc:
                log.warning("Failed to rescue part 1: %s", exc)

        # Remove parts manifest now that all parts are enqueued
        parts_file = os.path.join(HERALD_QUEUE_DIR, f"{timestamp}.parts")
        try:
            os.unlink(parts_file)
        except FileNotFoundError:
            pass

        # Clean up temp file if not moved by watcher or rescue above
        try:
            os.unlink(temp_wav)
        except FileNotFoundError:
            pass

        return True

    def _early_enqueue_watcher(
        self,
        temp_wav: str,
        timestamp: str,
        stop: threading.Event,
    ) -> None:
        """Stream-enqueue WAV parts as they appear during Kokoro generation.

        Runs in a daemon thread. Enqueues part 1 as soon as it appears, then
        continues watching for parts 2+ so they reach the queue while part 1
        is still playing (instead of waiting for generation to finish).

        Also writes a .parts manifest when part 1 is enqueued, signaling the
        orchestrator that more parts may be coming. The manifest is removed
        by the main thread after all parts are enqueued.
        """
        os.makedirs(HERALD_QUEUE_DIR, exist_ok=True)
        base = temp_wav.replace(".wav", "")
        enqueued_parts: set[int] = set()

        for _ in range(300):  # Up to 30 seconds (300 × 0.1s)
            if stop.is_set():
                # Generation finished — do one final sweep then exit
                self._sweep_parts(base, timestamp, enqueued_parts)
                break

            # Part 1: main temp_wav file
            if 1 not in enqueued_parts and os.path.isfile(temp_wav) and os.path.getsize(temp_wav) > 0:
                wav_name = f"{timestamp}-01.wav"
                dest = os.path.join(HERALD_QUEUE_DIR, wav_name)
                try:
                    shutil.copy2(temp_wav, dest)
                    self._write_workspace_sidecar(dest)
                    enqueued_parts.add(1)
                    log.debug("Stream-enqueued part 1 -> %s", wav_name)
                    # Write parts manifest — tells orchestrator more parts may follow
                    parts_file = os.path.join(HERALD_QUEUE_DIR, f"{timestamp}.parts")
                    Path(parts_file).write_text(timestamp)
                except OSError as exc:
                    log.warning("Stream-enqueue part 1 failed: %s", exc)

            # Parts 2+: look for .partN.wav files from Kokoro daemon
            self._sweep_parts(base, timestamp, enqueued_parts)

            time.sleep(0.1)

    def _sweep_parts(
        self,
        base: str,
        timestamp: str,
        enqueued_parts: set[int],
    ) -> None:
        """Sweep for any new .partN.wav files and enqueue them."""
        part_num = 2
        while True:
            if part_num in enqueued_parts:
                part_num += 1
                continue
            part_path = f"{base}.part{part_num}.wav"
            if os.path.isfile(part_path) and os.path.getsize(part_path) > 0:
                wav_name = f"{timestamp}-{part_num:02d}.wav"
                dest = os.path.join(HERALD_QUEUE_DIR, wav_name)
                try:
                    shutil.move(part_path, dest)
                    self._write_workspace_sidecar(dest)
                    enqueued_parts.add(part_num)
                    log.debug("Stream-enqueued part %d -> %s", part_num, wav_name)
                except OSError:
                    pass
                part_num += 1
            else:
                break

    def _generate_piper(
        self,
        text: str,
        voice: str,
        temp_wav: str,
        timestamp: str,
        lang: str = "en-us",
    ) -> bool:
        """Generate WAV via Piper TTS (fallback path).

        Ports the CLI fallback from worker.sh lines 247-260.
        Normalizes output via normalize_wav_in_place() per HERALD-02.
        """
        # Find piper model path matching the requested language
        model_path = self._find_piper_model(lang)
        if not model_path:
            log.warning("Piper model not found — TTS unavailable")
            return False

        try:
            result = subprocess.run(
                [
                    sys.executable, "-m", "piper",
                    "--model", model_path,
                    "--output_file", temp_wav,
                ],
                input=text.encode(),
                capture_output=True,
                timeout=30.0,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
            log.warning("Piper generation failed: %s", exc)
            return False

        if result.returncode != 0 or not os.path.isfile(temp_wav):
            log.warning("Piper failed (exit=%d): %s", result.returncode, result.stderr[:200])
            return False

        # Normalize Piper output (HERALD-02 — Piper doesn't normalize at generation time)
        normalize_wav_in_place(temp_wav)

        # Enqueue
        wav_name = f"{timestamp}-01.wav"
        dest = os.path.join(HERALD_QUEUE_DIR, wav_name)
        try:
            os.makedirs(HERALD_QUEUE_DIR, exist_ok=True)
            shutil.move(temp_wav, dest)
            self._write_workspace_sidecar(dest)
            log.info("Piper: enqueued %s", wav_name)
            return True
        except OSError as exc:
            log.warning("Failed to enqueue Piper WAV: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Private: Kokoro daemon management
    # ------------------------------------------------------------------

    def _ensure_kokoro_daemon(self) -> bool:
        """Ensure Kokoro daemon is running. Start it if not.

        Ports ensure_daemon() from worker.sh lines 155-167.
        Returns True if daemon is ready, False if startup failed.
        """
        if self._kokoro_daemon_alive():
            return True

        # DEF-069: a stale daemon (process alive, socket missing) will trip the
        # new daemon's PID-lock soft-check and abort spawn, causing Piper
        # fallback. Reap it before spawning so the new daemon can take over.
        self._reap_stale_kokoro_daemon()

        log.info("Starting Kokoro daemon...")
        daemon_script = self._find_kokoro_daemon_script()
        if not daemon_script:
            log.warning("Kokoro daemon script not found")
            return False

        python_exe = self._find_kokoro_python()
        try:
            subprocess.Popen(
                [python_exe, str(daemon_script)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=open(HERALD_DEBUG_LOG, "a"),
                start_new_session=True,
            )
        except OSError as exc:
            log.warning("Failed to launch Kokoro daemon: %s", exc)
            return False

        # Wait up to 8 seconds for socket to appear
        for _ in range(80):
            if os.path.exists(KOKORO_DAEMON_SOCK):
                log.info("Kokoro daemon started successfully")
                return True
            time.sleep(0.1)

        log.warning("Kokoro daemon failed to start (socket never appeared)")
        return False

    def _kokoro_daemon_alive(self) -> bool:
        """Check if Kokoro daemon socket exists and PID is alive."""
        if not os.path.exists(KOKORO_DAEMON_SOCK):
            return False
        try:
            pid_str = Path(KOKORO_DAEMON_PID).read_text().strip()
            pid = int(pid_str)
            os.kill(pid, 0)  # Signal 0 = liveness check
            return True
        except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
            return False

    def _reap_stale_kokoro_daemon(self) -> None:
        """Terminate a live-but-socketless Kokoro daemon blocking new spawns.

        DEF-069: The kokoro-daemon PID-lock's soft check (is_pid_alive) aborts
        startup whenever the PID file points to any live process — even one
        that has already closed its socket or is stuck in MLX teardown. The
        worker then waits 8 s for a socket that will never appear and falls
        back to Piper. Proactively SIGTERM the stuck daemon so the lock
        releases and the new daemon can bind.
        """
        try:
            pid_str = Path(KOKORO_DAEMON_PID).read_text().strip()
            pid = int(pid_str)
        except (FileNotFoundError, ValueError):
            return
        if pid <= 0 or pid == os.getpid():
            return
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return

        log.warning(
            "Stale Kokoro daemon pid=%d has no socket — terminating before respawn",
            pid,
        )
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return

        for _ in range(30):  # up to ~3 s
            try:
                os.kill(pid, 0)
            except (ProcessLookupError, PermissionError):
                return
            time.sleep(0.1)

        log.warning("Stale Kokoro daemon pid=%d ignored SIGTERM — SIGKILL", pid)
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        time.sleep(0.2)

    def _find_kokoro_daemon_script(self) -> Path | None:
        """Find the kokoro-daemon.py script path."""
        # Relative to this module's package (heyvox/herald/daemon/)
        script = Path(__file__).parent / "daemon" / "kokoro-daemon.py"
        if script.is_file():
            return script
        # Fallback: env var
        env_home = os.environ.get("HERALD_HOME", "")
        if env_home:
            s = Path(env_home) / "daemon" / "kokoro-daemon.py"
            if s.is_file():
                return s
        return None

    # ------------------------------------------------------------------
    # Private: Qwen3 daemon lifecycle (mirrors the Kokoro lifecycle)
    # ------------------------------------------------------------------

    def _ensure_qwen_daemon(self) -> bool:
        """Ensure Qwen3 daemon is running. Start it lazily on first German hit."""
        if self._qwen_daemon_alive():
            return True

        self._reap_stale_qwen_daemon()

        log.info("Starting Qwen3 daemon...")
        daemon_script = self._find_qwen_daemon_script()
        if not daemon_script:
            log.warning("Qwen3 daemon script not found")
            return False

        python_exe = self._find_kokoro_python()  # same interpreter has mlx-audio
        try:
            subprocess.Popen(
                [python_exe, str(daemon_script)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=open(HERALD_DEBUG_LOG, "a"),
                start_new_session=True,
            )
        except OSError as exc:
            log.warning("Failed to launch Qwen3 daemon: %s", exc)
            return False

        # First-load can take ~5s (bf16 weights + warmup). Wait up to 30s.
        for _ in range(300):
            if os.path.exists(QWEN_DAEMON_SOCK):
                log.info("Qwen3 daemon started successfully")
                return True
            time.sleep(0.1)

        log.warning("Qwen3 daemon failed to start (socket never appeared)")
        return False

    def _qwen_daemon_alive(self) -> bool:
        if not os.path.exists(QWEN_DAEMON_SOCK):
            return False
        try:
            pid_str = Path(QWEN_DAEMON_PID).read_text().strip()
            pid = int(pid_str)
            os.kill(pid, 0)
            return True
        except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
            return False

    def _reap_stale_qwen_daemon(self) -> None:
        """Mirror _reap_stale_kokoro_daemon for the Qwen3 PID lock."""
        try:
            pid_str = Path(QWEN_DAEMON_PID).read_text().strip()
            pid = int(pid_str)
        except (FileNotFoundError, ValueError):
            return
        if pid <= 0 or pid == os.getpid():
            return
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return

        log.warning(
            "Stale Qwen3 daemon pid=%d has no socket — terminating before respawn",
            pid,
        )
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            return

        for _ in range(30):
            try:
                os.kill(pid, 0)
            except (ProcessLookupError, PermissionError):
                return
            time.sleep(0.1)

        log.warning("Stale Qwen3 daemon pid=%d ignored SIGTERM — SIGKILL", pid)
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        time.sleep(0.2)

    def _find_qwen_daemon_script(self) -> Path | None:
        script = Path(__file__).parent / "daemon" / "qwen-daemon.py"
        if script.is_file():
            return script
        env_home = os.environ.get("HERALD_HOME", "")
        if env_home:
            s = Path(env_home) / "daemon" / "qwen-daemon.py"
            if s.is_file():
                return s
        return None

    def _qwen_request(self, request: dict, timeout: float = 60.0) -> dict:
        """Send JSON request to Qwen3 daemon and return parsed response."""
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        try:
            sock.connect(QWEN_DAEMON_SOCK)
            sock.sendall(json.dumps(request).encode("utf-8"))
            raw = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                raw += chunk
            if not raw:
                return {"ok": False, "error": "empty response"}
            return json.loads(raw.decode("utf-8"))
        finally:
            sock.close()

    def _find_kokoro_python(self) -> str:
        """Find the Python executable to use for the Kokoro daemon.

        Prefers system python3 (which has mlx-audio for Metal GPU TTS).
        Falls back to uv tool venv (kokoro-onnx CPU only).
        """
        # Check env var first (set by config.sh KOKORO_DAEMON_PYTHON)
        env_python = os.environ.get("KOKORO_DAEMON_PYTHON", "")
        if env_python and os.path.isfile(env_python):
            return env_python

        # Prefer system python3 — has mlx-audio for Metal GPU acceleration
        pyenv_python = os.path.expanduser("~/.pyenv/shims/python3")
        if os.path.isfile(pyenv_python):
            return pyenv_python

        # Fallback to uv tool venv (kokoro-onnx CPU only)
        uv_python = os.path.expanduser("~/.local/share/uv/tools/kokoro-tts/bin/python")
        if os.path.isfile(uv_python):
            return uv_python

        return sys.executable

    def _find_piper_model(self, lang: str = "en-us") -> str | None:
        """Find a Piper TTS model file.

        DEF-065: Previous implementation returned `matches[0]` from an
        alphabetical glob, so `de_DE-thorsten-*.onnx` always beat
        `en_US-*.onnx`. English text then played with a German voice.
        Now we score candidates: (a) exact lang prefix match first
        (e.g. `en_US` for en-us), (b) any `en_*` as second choice,
        (c) never silently fall to a different language. German
        (`de_*`) is skipped unless explicitly requested.
        """
        import glob

        search_dirs = [
            os.path.expanduser("~/.local/share/piper"),
            os.path.expanduser("~/.local/share/piper-voices"),
            os.path.expanduser("~/.piper"),
            "/usr/local/share/piper",
        ]
        candidates: list[str] = []
        for d in search_dirs:
            candidates.extend(glob.glob(os.path.join(d, "**", "*.onnx"), recursive=True))

        if not candidates:
            return None

        # Map request language → piper file prefix
        lang_prefix = {
            "en-us": "en_US", "en-gb": "en_GB",
            "fr-fr": "fr_FR", "it": "it_IT",
            "es": "es_ES", "pt": "pt_PT",
            "ja": "ja_JP", "cmn": "zh_CN",
        }.get(lang, "en_US")

        def score(path: str) -> int:
            # Match against both filename and parent dir (piper-voices layout
            # uses `<lang>/<voice>.onnx` with unprefixed basenames).
            base = os.path.basename(path).lower()
            parent = os.path.basename(os.path.dirname(path)).lower()
            tokens = f"{parent}/{base}"
            if lang_prefix.lower() in tokens or parent == lang_prefix.split("_")[0].lower():
                return 0  # exact lang match
            if "en_" in tokens or parent == "en":
                return 1  # English fallback
            if "de_" in tokens or "thorsten" in base or parent == "de":
                return 99  # German last resort only (user dislikes Thorsten)
            return 50  # other language, ahead of German

        best = sorted(candidates, key=score)[0]
        if score(best) >= 99:
            log.warning("No suitable Piper model; only German present — skipping TTS")
            return None
        return best

    # ------------------------------------------------------------------
    # Private: socket communication
    # ------------------------------------------------------------------

    def _kokoro_request(self, request: dict, timeout: float = 30.0) -> dict:
        """Send a request to the Kokoro daemon via AF_UNIX socket.

        Ports the daemon call from worker.sh lines 198-222.
        """
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(KOKORO_DAEMON_SOCK)
            sock.sendall(json.dumps(request).encode())
            sock.shutdown(socket.SHUT_WR)
            data = b""
            while True:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                data += chunk
        return json.loads(data)

    # ------------------------------------------------------------------
    # Private: queue management
    # ------------------------------------------------------------------

    def _write_workspace_sidecar(self, wav_path: str) -> None:
        """Write .workspace sidecar next to WAV file if workspace is known."""
        if self._workspace:
            sidecar = wav_path.replace(".wav", ".workspace")
            try:
                Path(sidecar).write_text(self._workspace)
            except OSError as exc:
                log.debug("Failed to write workspace sidecar: %s", exc)

    # ------------------------------------------------------------------
    # Private: state readers
    # ------------------------------------------------------------------

    def _read_verbosity(self) -> str:
        """Read verbosity level from shared state file."""
        try:
            return Path(VERBOSITY_FILE).read_text().strip() or "full"
        except FileNotFoundError:
            return "full"

    def _read_mode(self) -> str:
        """Read herald mode from shared state file."""
        try:
            from heyvox.constants import HERALD_MODE_FILE
            return Path(HERALD_MODE_FILE).read_text().strip() or "narrate"
        except (FileNotFoundError, ImportError):
            return "narrate"


# ---------------------------------------------------------------------------
# Module __main__ entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    # Entry point for hook shims: python3 -m heyvox.herald.worker [raw_file]
    worker = HeraldWorker()

    raw_file = None
    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        raw_file = sys.argv[1]
        raw = Path(raw_file).read_text()
    else:
        raw = sys.stdin.read()

    hook_type = os.environ.get("HERALD_HOOK_TYPE", "response")
    success = worker.process_response(raw, hook_type=hook_type)

    # Clean up temp file created by async hook shims
    if raw_file and raw_file.startswith("/tmp/herald-hook."):
        try:
            os.unlink(raw_file)
        except OSError:
            pass

    sys.exit(0 if success else 1)
