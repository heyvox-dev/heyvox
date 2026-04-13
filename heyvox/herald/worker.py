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
import math
import os
import re
import shutil
import socket
import struct
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
    VERBOSITY_FILE,
    HERALD_GENERATING_WAV_PREFIX,
)

log = logging.getLogger(__name__)

# File-based log handler — matches herald_log() in config.sh
_file_handler = logging.FileHandler(HERALD_DEBUG_LOG, delay=True)
_file_handler.setFormatter(logging.Formatter("[%(asctime)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
logging.getLogger("heyvox.herald").addHandler(_file_handler)

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

    Ports detect_language() from worker.sh lines 105-130.
    Returns e.g. ('en-us', None), ('ja', 'jf_alpha'), ('fr-fr', 'ff_siwis').
    """
    # CJK detection — Chinese or Japanese characters
    if re.search(r"[\u4e00-\u9fff]", text):
        return "cmn", "zf_xiaoxiao"
    if re.search(r"[\u3040-\u309f\u30a0-\u30ff]", text):
        return "ja", "jf_alpha"

    # French
    if re.search(
        r"\b(je suis|merci|bonjour|s.il vous|c.est|nous avons|vous avez)\b",
        text, re.IGNORECASE
    ):
        return "fr-fr", "ff_siwis"

    # Italian
    if re.search(
        r"\b(grazie|buongiorno|ciao|sono|questo|quello|perch.)\b",
        text, re.IGNORECASE
    ):
        return "it", "if_sara"

    # German
    if re.search(
        r"\b(ich|nicht|haben|werden|k.nnen|m.ssen|danke|bitte)\b",
        text, re.IGNORECASE
    ):
        return "en-gb", "bf_emma"

    return "en-us", None


# ---------------------------------------------------------------------------
# Orchestrator auto-start
# ---------------------------------------------------------------------------

def _ensure_orchestrator() -> None:
    """Start the Herald orchestrator if it isn't already running.

    Called after each successful WAV generation so the queue is always drained.
    Uses a PID file check to avoid spawning duplicates.
    """
    if os.path.exists(HERALD_ORCH_PID):
        try:
            pid = int(open(HERALD_ORCH_PID).read().strip())
            os.kill(pid, 0)
            return  # Already running
        except (OSError, ValueError):
            pass  # Stale PID file — start a new one

    try:
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
        """Select Kokoro voice name from mood + language + agent context.

        Ports voice selection logic from worker.sh lines 86-131.
        Priority: agent env var > language override > mood.
        """
        # Mood → default voice
        voice = MOOD_VOICES.get(mood, DEFAULT_VOICE)

        # Language override (higher priority than mood)
        if lang_voice and lang != "en-us":
            voice = lang_voice

        # Multi-agent voice routing (highest priority).
        # HEYVOX_AGENT is the generic env var; CONDUCTOR_AGENT and
        # CLAUDE_AGENT_NAME are kept for backward compatibility.
        agent_name = (
            os.environ.get("HEYVOX_AGENT", "")
            or os.environ.get("CONDUCTOR_AGENT", "")
            or os.environ.get("CLAUDE_AGENT_NAME", "")
        )
        if agent_name:
            idx = int(hashlib.md5(agent_name.encode()).hexdigest(), 16) % len(AGENT_VOICE_POOL)
            voice = AGENT_VOICE_POOL[idx]

        # Explicit voice override from environment (e.g. KOKORO_VOICE)
        env_voice = os.environ.get("KOKORO_VOICE", "")
        if env_voice:
            voice = env_voice

        return voice

    # ------------------------------------------------------------------
    # Private: generation dispatch
    # ------------------------------------------------------------------

    def _generate(self, text: str, voice: str, lang: str, speed: float) -> bool:
        """Generate TTS WAV and enqueue it. Tries Kokoro daemon first, Piper fallback."""
        timestamp = str(int(time.time() * 1000))
        temp_wav = f"{HERALD_GENERATING_WAV_PREFIX}{os.getpid()}.wav"

        if self._generate_kokoro(text, voice, lang, speed, temp_wav, timestamp):
            return True

        log.warning("Kokoro daemon unavailable — falling back to Piper")
        return self._generate_piper(text, voice, temp_wav, timestamp)

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

        # Remove parts manifest now that all parts are enqueued
        parts_file = os.path.join(HERALD_QUEUE_DIR, f"{timestamp}.parts")
        try:
            os.unlink(parts_file)
        except FileNotFoundError:
            pass

        # Clean up temp file if not moved by watcher
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
    ) -> bool:
        """Generate WAV via Piper TTS (fallback path).

        Ports the CLI fallback from worker.sh lines 247-260.
        Normalizes output via normalize_wav_in_place() per HERALD-02.
        """
        # Find piper model path
        model_path = self._find_piper_model()
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

    def _find_piper_model(self) -> str | None:
        """Find a Piper TTS model file."""
        search_dirs = [
            os.path.expanduser("~/.local/share/piper"),
            os.path.expanduser("~/.piper"),
            "/usr/local/share/piper",
        ]
        for d in search_dirs:
            for pattern in ["*.onnx"]:
                import glob
                matches = glob.glob(os.path.join(d, "**", pattern), recursive=True)
                if matches:
                    return matches[0]
        return None

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

    if len(sys.argv) > 1 and os.path.isfile(sys.argv[1]):
        raw = Path(sys.argv[1]).read_text()
    else:
        raw = sys.stdin.read()

    hook_type = os.environ.get("HERALD_HOOK_TYPE", "response")
    success = worker.process_response(raw, hook_type=hook_type)
    sys.exit(0 if success else 1)
