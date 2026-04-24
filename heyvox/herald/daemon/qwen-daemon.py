#!/usr/bin/env python3
"""Qwen3-TTS daemon — German (and other non-Kokoro languages) via mlx-audio.

Mirrors kokoro-daemon.py protocol and lifecycle (Unix socket + PID lock +
idle timeout). Loaded lazily on first non-Kokoro-language TTS request so
the ~650 MB of model weights only hit RAM when needed. Idle-exits after
IDLE_TIMEOUT seconds, freeing the memory back to the OS.

Listens on a Unix socket for JSON requests:
  {"text": "...", "voice": "Serena", "lang": "de", "speed": 1.0, "output": "/tmp/out.wav"}

Returns: {"ok": true, "duration": 1.23, "parts": 3} or {"ok": false, "error": "..."}

Engine: mlx-audio (Metal GPU). Requires a Python with mlx-audio installed.
"""

import fcntl
import json
import os
import re
import signal
import socket
import sys
import threading
import time
import wave

import numpy as np

_TMP = os.environ.get("TMPDIR", "/tmp").rstrip("/")

SOCKET_PATH = f"{_TMP}/qwen-daemon.sock"
PID_FILE = f"{_TMP}/qwen-daemon.pid"
IDLE_TIMEOUT = int(os.environ.get("QWEN_IDLE_TIMEOUT", "300"))

# bf16 variant for clean audio — avoids the "tinny" 8-bit quant artifacts.
# Users can override via QWEN_TTS_MODEL env var.
MLX_MODEL_ID = os.environ.get(
    "QWEN_TTS_MODEL",
    "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-bf16",
)

# worker sends short ISO-ish codes (de, ko, ru, pl, ...);
# Qwen3 expects full English names as lang_code.
LANG_MAP = {
    "de": "German",
    "ko": "Korean",
    "ru": "Russian",
    "pl": "Polish",
    "nl": "Dutch",
    "cs": "Czech",
    "ar": "Arabic",
    "hu": "Hungarian",
    "tr": "Turkish",
}


def map_lang(lang):
    return LANG_MAP.get(lang, "German")


last_activity = time.time()
_daemon_start_time = time.time()
shutdown_event = threading.Event()

_pid_lock_fd = None


def log(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] qwen-daemon: {msg}", file=sys.stderr, flush=True)


# --- Model loading ---

def load_model():
    log(f"Loading Qwen3-TTS ({MLX_MODEL_ID})...")
    t0 = time.time()
    from mlx_audio.tts.utils import load_model as _load
    model = _load(MLX_MODEL_ID)
    # Warm: first generate compiles the MLX graph. Short German phrase
    # touches the actual German codebook path.
    try:
        for _ in model.generate("Hallo", voice="Serena", speed=1.0, lang_code="German"):
            pass
    except Exception as exc:
        log(f"Warmup failed (non-fatal): {exc}")
    log(f"Qwen3-TTS loaded + warmed in {time.time() - t0:.1f}s")
    return model


# --- Sentence splitting (same rule as kokoro-daemon) ---

def split_sentences(text):
    parts = re.split(r'(?<=[.!?])\s+', text.strip())
    return [p.strip() for p in parts if p.strip()]


# --- WAV writing ---

def normalize_samples(samples, target_rms=3000, scale_cap=3.0, peak_limit=24000):
    try:
        from heyvox.audio.normalize import normalize_samples_float32
        return normalize_samples_float32(samples, target_rms, scale_cap, peak_limit)
    except ImportError:
        if len(samples) < 1000:
            return samples
        int16_view = samples * 32767.0
        rms = np.sqrt(np.mean(int16_view ** 2))
        if rms < 50:
            return samples
        scale = min(target_rms / rms if rms > 0 else 1.0, scale_cap)
        scaled = int16_view * scale
        above = scaled > peak_limit
        below = scaled < -peak_limit
        scaled[above] = peak_limit + (scaled[above] - peak_limit) * 0.2
        scaled[below] = -peak_limit + (scaled[below] + peak_limit) * 0.2
        scaled = np.clip(scaled, -32768, 32767)
        return scaled / 32767.0


def write_wav(path, samples, sample_rate):
    samples_int16 = (samples * 32767).astype(np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(samples_int16.tobytes())


# --- TTS generation (multi-part streaming, like kokoro-daemon) ---

def generate_tts(model, text, voice, lang, speed, output_path):
    t0 = time.time()
    lang_code = map_lang(lang)
    sentences = split_sentences(text)

    def _gen(sentence):
        chunks = list(model.generate(
            sentence, voice=voice, speed=speed, lang_code=lang_code,
        ))
        audio = np.concatenate([np.asarray(c.audio) for c in chunks])
        return normalize_samples(audio), chunks[0].sample_rate

    if len(sentences) <= 1:
        audio, sr = _gen(text)
        write_wav(output_path, audio, sr)
        duration = time.time() - t0
        audio_len = len(audio) / sr
        log(f"Generated {audio_len:.1f}s in {duration:.2f}s (1 part) -> {output_path}")
        return {"ok": True, "duration": duration, "audio_length": audio_len, "parts": 1}

    total_audio_len = 0.0
    base = output_path.replace(".wav", "")

    audio, sr = _gen(sentences[0])
    write_wav(output_path, audio, sr)
    part1_time = time.time() - t0
    total_audio_len += len(audio) / sr
    log(f"  Part 1: {len(audio) / sr:.1f}s in {part1_time:.2f}s -> {output_path}")

    for i, sentence in enumerate(sentences[1:], start=2):
        part_path = f"{base}.part{i}.wav"
        audio, sr = _gen(sentence)
        write_wav(part_path, audio, sr)
        total_audio_len += len(audio) / sr
        log(f"  Part {i}: {len(audio) / sr:.1f}s in {time.time() - t0 - part1_time:.2f}s -> {part_path}")

    duration = time.time() - t0
    log(f"Generated {total_audio_len:.1f}s total in {duration:.2f}s ({len(sentences)} parts)")
    return {"ok": True, "duration": duration, "audio_length": total_audio_len, "parts": len(sentences)}


# --- Client handling ---

def handle_client(conn, model):
    global last_activity
    last_activity = time.time()

    try:
        raw = b""
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            raw += chunk
            try:
                request = json.loads(raw.decode("utf-8"))
                break
            except json.JSONDecodeError:
                continue

        if not raw:
            return

        action = request.get("action", "")
        if action == "ping":
            uptime = time.time() - _daemon_start_time
            conn.sendall(json.dumps({"ok": True, "engine": "qwen3-mlx", "uptime": round(uptime, 1)}).encode("utf-8"))
            return

        text = request.get("text", "")
        voice = request.get("voice", "Serena")
        lang = request.get("lang", "de")
        speed = request.get("speed", 1.0)
        output = request.get("output", f"{_TMP}/qwen-out.wav")

        if not text:
            response = {"ok": False, "error": "empty text"}
        else:
            response = generate_tts(model, text, voice, lang, speed, output)

        conn.sendall(json.dumps(response).encode("utf-8"))
    except Exception as e:
        log(f"Error handling client: {e}")
        try:
            conn.sendall(json.dumps({"ok": False, "error": str(e)}).encode("utf-8"))
        except Exception:
            pass
    finally:
        conn.close()
        last_activity = time.time()


# --- Idle watchdog (same as kokoro-daemon) ---

def idle_watchdog():
    while not shutdown_event.is_set():
        idle = time.time() - last_activity
        if idle > IDLE_TIMEOUT:
            log(f"Idle for {idle:.0f}s (limit {IDLE_TIMEOUT}s), shutting down")
            shutdown_event.set()
            try:
                s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                s.connect(SOCKET_PATH)
                s.close()
            except Exception:
                pass
            return
        shutdown_event.wait(timeout=10)


def is_pid_alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def check_and_acquire_pid_lock():
    """Mirror kokoro-daemon DEF-062 fix: soft-check + flock before loading weights."""
    global _pid_lock_fd

    try:
        with open(PID_FILE, "r") as f:
            existing = int(f.read().strip() or "0")
        if existing and existing != os.getpid() and is_pid_alive(existing):
            log(f"Another qwen-daemon already running (pid={existing}), exiting")
            sys.exit(0)
    except (FileNotFoundError, ValueError):
        pass

    _pid_lock_fd = os.open(PID_FILE, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(_pid_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("Another qwen-daemon holds the PID lock, exiting")
        os.close(_pid_lock_fd)
        _pid_lock_fd = None
        sys.exit(0)

    os.ftruncate(_pid_lock_fd, 0)
    os.pwrite(_pid_lock_fd, f"{os.getpid()}\n".encode(), 0)


def main():
    # Own process group — idle-timeout shutdown must not signal siblings.
    try:
        os.setpgrp()
    except OSError:
        pass

    check_and_acquire_pid_lock()
    model = load_model()

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        os.unlink(SOCKET_PATH)
    except FileNotFoundError:
        pass
    try:
        server.bind(SOCKET_PATH)
    except OSError as e:
        log(f"Cannot bind {SOCKET_PATH}: {e}")
        server.close()
        return
    server.listen(2)
    server.settimeout(5.0)
    os.chmod(SOCKET_PATH, 0o600)

    log(f"Listening on {SOCKET_PATH} (idle timeout: {IDLE_TIMEOUT}s)")

    watchdog = threading.Thread(target=idle_watchdog, daemon=True)
    watchdog.start()

    def handle_signal(signum, frame):
        log(f"Signal {signum}, shutting down")
        shutdown_event.set()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    try:
        while not shutdown_event.is_set():
            try:
                conn, _ = server.accept()
                if shutdown_event.is_set():
                    conn.close()
                    break
                handle_client(conn, model)
            except socket.timeout:
                continue
            except OSError:
                if shutdown_event.is_set():
                    break
                raise
    finally:
        # Mirror DEF-062: unlink socket + PID before the Python interpreter
        # tears down MLX (can take seconds via GC).
        try:
            server.close()
        except Exception:
            pass
        try:
            os.unlink(SOCKET_PATH)
        except FileNotFoundError:
            pass
        try:
            os.unlink(PID_FILE)
        except FileNotFoundError:
            pass
        log("Daemon stopped")


if __name__ == "__main__":
    main()
