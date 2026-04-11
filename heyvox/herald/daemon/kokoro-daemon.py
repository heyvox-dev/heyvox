#!/usr/bin/env python3
"""Kokoro TTS daemon — keeps model warm, auto-exits after idle timeout.

Listens on a Unix socket for JSON requests:
  {"text": "...", "voice": "af_sarah", "lang": "en-us", "speed": 1.2, "output": "/tmp/out.wav"}

Streaming mode (default): splits text into sentences, generates first sentence
immediately and writes it to output. Remaining sentences are written as
sequential files (output.part2.wav, output.part3.wav, etc.) that the orchestrator
picks up and plays back-to-back.

Returns JSON: {"ok": true, "duration": 1.23, "parts": 3} or {"ok": false, "error": "..."}

Auto-exits after IDLE_TIMEOUT seconds of no requests.

Engine: mlx-audio (Metal GPU via MLX framework) — ~5-10x faster than kokoro-onnx CPU.
Fallback: kokoro-onnx (CPU) if mlx-audio is not available.
"""

import json
import os
import re
import signal
import socket
import sys
import time
import threading
import wave

import numpy as np

SOCKET_PATH = "/tmp/kokoro-daemon.sock"
PID_FILE = "/tmp/kokoro-daemon.pid"
IDLE_TIMEOUT = int(os.environ.get("KOKORO_IDLE_TIMEOUT", "300"))

# Legacy kokoro-onnx paths (used for fallback)
ONNX_MODEL_PATH = os.path.expanduser("~/.kokoro-tts/kokoro-v1.0.onnx")
ONNX_VOICES_PATH = os.path.expanduser("~/.kokoro-tts/voices-v1.0.bin")

# mlx-audio model ID
MLX_MODEL_ID = "mlx-community/Kokoro-82M-bf16"

last_activity = time.time()
_daemon_start_time = time.time()
shutdown_event = threading.Event()

# Engine flag: "mlx" or "onnx"
ENGINE = "mlx"


def log(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] kokoro-daemon: {msg}", file=sys.stderr, flush=True)


# --- Language code mapping ---
# worker.sh sends full codes (en-us, ja, cmn, fr-fr, it, de)
# mlx-audio uses single-letter codes (a, b, j, z, f, e, i, p, h)
LANG_MAP = {
    "en-us": "a",
    "en-gb": "b",
    "ja": "j",
    "cmn": "z",
    "fr-fr": "f",
    "es": "e",
    "it": "i",
    "pt": "p",
    "hi": "h",
    # Single-letter codes pass through
    "a": "a", "b": "b", "j": "j", "z": "z", "f": "f",
    "e": "e", "i": "i", "p": "p", "h": "h",
}


def map_lang(lang):
    """Map full language code to mlx-audio single-letter code."""
    return LANG_MAP.get(lang, "a")


# --- Model loading ---

def load_model_mlx():
    """Load Kokoro via mlx-audio (Metal GPU)."""
    log("Loading Kokoro via mlx-audio (Metal GPU)...")
    t0 = time.time()
    from mlx_audio.tts.utils import load_model
    model = load_model(MLX_MODEL_ID)
    # Pre-warm: first generate compiles the MLX graph
    for v in ["af_sarah", "af_heart", "af_nova", "af_sky"]:
        try:
            for _ in model.generate("warmup", voice=v, speed=1.0, lang_code="a"):
                pass
        except Exception:
            pass
    log(f"mlx-audio loaded + warmed 4 voices in {time.time() - t0:.1f}s")
    return model


def load_model_onnx():
    """Load Kokoro via kokoro-onnx (CPU fallback)."""
    log("Loading Kokoro via kokoro-onnx (CPU fallback)...")
    t0 = time.time()
    _kokoro_lib = os.path.expanduser("~/.local/share/uv/tools/kokoro-tts/lib")
    if os.path.isdir(_kokoro_lib):
        for _d in sorted(os.listdir(_kokoro_lib), reverse=True):
            _sp = os.path.join(_kokoro_lib, _d, "site-packages")
            if os.path.isdir(_sp):
                sys.path.insert(0, _sp)
                break
    from kokoro_onnx import Kokoro
    kokoro = Kokoro(ONNX_MODEL_PATH, ONNX_VOICES_PATH)
    for v in ["af_sarah", "af_heart", "af_nova", "af_sky"]:
        try:
            kokoro.create("warmup", voice=v, speed=1.0, lang="en-us")
        except Exception:
            pass
    log(f"kokoro-onnx loaded + warmed 4 voices in {time.time() - t0:.1f}s")
    return kokoro


def load_model():
    global ENGINE
    # Try mlx-audio first (much faster on Apple Silicon)
    try:
        model = load_model_mlx()
        ENGINE = "mlx"
        return model
    except Exception as e:
        log(f"mlx-audio failed: {e}, falling back to kokoro-onnx")
    # Fallback to kokoro-onnx
    try:
        model = load_model_onnx()
        ENGINE = "onnx"
        return model
    except Exception as e:
        log(f"kokoro-onnx also failed: {e}")
        raise


# --- Sentence splitting ---

def split_sentences(text):
    """Split into 2 parts: first sentence (for fast start) + rest."""
    parts = re.split(r'(?<=[.!?])\s+', text.strip())
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) <= 1:
        return parts
    return [parts[0], " ".join(parts[1:])]


# --- WAV writing ---

def write_wav(path, samples, sample_rate):
    samples_int16 = (samples * 32767).astype(np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(samples_int16.tobytes())


# --- TTS generation ---

def generate_mlx(model, text, voice, lang, speed, output_path):
    """Generate TTS using mlx-audio (Metal GPU)."""
    t0 = time.time()
    lang_code = map_lang(lang)
    sentences = split_sentences(text)

    if len(sentences) <= 1:
        for result in model.generate(text, voice=voice, speed=speed, lang_code=lang_code):
            audio = np.array(result.audio)
            write_wav(output_path, audio, result.sample_rate)
            duration = time.time() - t0
            audio_len = len(audio) / result.sample_rate
            log(f"Generated {audio_len:.1f}s audio in {duration:.2f}s (1 part, mlx) -> {output_path}")
            return {"ok": True, "duration": duration, "audio_length": audio_len, "parts": 1}

    total_audio_len = 0.0
    base = output_path.replace(".wav", "")

    # Part 1 — first sentence (fast start)
    for result in model.generate(sentences[0], voice=voice, speed=speed, lang_code=lang_code):
        audio = np.array(result.audio)
        write_wav(output_path, audio, result.sample_rate)
        sample_rate = result.sample_rate
        part1_time = time.time() - t0
        part1_audio = len(audio) / sample_rate
        total_audio_len += part1_audio
        log(f"  Part 1: {part1_audio:.1f}s audio in {part1_time:.2f}s -> {output_path}")

    # Part 2+ — remaining sentences
    for i, sentence in enumerate(sentences[1:], start=2):
        part_path = f"{base}.part{i}.wav"
        for result in model.generate(sentence, voice=voice, speed=speed, lang_code=lang_code):
            audio = np.array(result.audio)
            write_wav(part_path, audio, result.sample_rate)
            part_audio = len(audio) / result.sample_rate
            total_audio_len += part_audio
            log(f"  Part {i}: {part_audio:.1f}s audio in {time.time() - t0 - part1_time:.2f}s -> {part_path}")

    duration = time.time() - t0
    log(f"Generated {total_audio_len:.1f}s total in {duration:.2f}s ({len(sentences)} parts, mlx)")
    return {"ok": True, "duration": duration, "audio_length": total_audio_len, "parts": len(sentences)}


def generate_onnx(model, text, voice, lang, speed, output_path):
    """Generate TTS using kokoro-onnx (CPU fallback)."""
    t0 = time.time()
    sentences = split_sentences(text)

    if len(sentences) <= 1:
        samples, sample_rate = model.create(text, voice=voice, speed=speed, lang=lang)
        write_wav(output_path, samples, sample_rate)
        duration = time.time() - t0
        audio_len = len(samples) / sample_rate
        log(f"Generated {audio_len:.1f}s audio in {duration:.2f}s (1 part, onnx) -> {output_path}")
        return {"ok": True, "duration": duration, "audio_length": audio_len, "parts": 1}

    total_audio_len = 0.0
    base = output_path.replace(".wav", "")

    samples, sample_rate = model.create(sentences[0], voice=voice, speed=speed, lang=lang)
    write_wav(output_path, samples, sample_rate)
    part1_time = time.time() - t0
    part1_audio = len(samples) / sample_rate
    total_audio_len += part1_audio
    log(f"  Part 1: {part1_audio:.1f}s audio in {part1_time:.2f}s -> {output_path}")

    for i, sentence in enumerate(sentences[1:], start=2):
        part_path = f"{base}.part{i}.wav"
        samples, sample_rate = model.create(sentence, voice=voice, speed=speed, lang=lang)
        write_wav(part_path, samples, sample_rate)
        part_audio = len(samples) / sample_rate
        total_audio_len += part_audio
        log(f"  Part {i}: {part_audio:.1f}s audio in {time.time() - t0 - part1_time:.2f}s -> {part_path}")

    duration = time.time() - t0
    log(f"Generated {total_audio_len:.1f}s total in {duration:.2f}s ({len(sentences)} parts, onnx)")
    return {"ok": True, "duration": duration, "audio_length": total_audio_len, "parts": len(sentences)}


def generate_tts(model, text, voice, lang, speed, output_path):
    if ENGINE == "mlx":
        return generate_mlx(model, text, voice, lang, speed, output_path)
    else:
        return generate_onnx(model, text, voice, lang, speed, output_path)


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

        # Health check — lightweight ping, no TTS work
        if action == "ping":
            uptime = time.time() - _daemon_start_time
            response = {"ok": True, "engine": ENGINE, "uptime": round(uptime, 1)}
            conn.sendall(json.dumps(response).encode("utf-8"))
            return

        text = request.get("text", "")
        voice = request.get("voice", "af_sarah")
        lang = request.get("lang", "en-us")
        speed = request.get("speed", 1.2)
        output = request.get("output", "/tmp/kokoro-out.wav")

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


# --- Idle watchdog ---

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


def cleanup():
    for f in (SOCKET_PATH, PID_FILE):
        try:
            os.unlink(f)
        except FileNotFoundError:
            pass


def main():
    cleanup()

    model = load_model()

    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(SOCKET_PATH)
    server.listen(2)
    server.settimeout(5.0)
    os.chmod(SOCKET_PATH, 0o600)

    log(f"Listening on {SOCKET_PATH} (engine={ENGINE}, idle timeout: {IDLE_TIMEOUT}s)")

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
        server.close()
        cleanup()
        log("Daemon stopped")


if __name__ == "__main__":
    main()
