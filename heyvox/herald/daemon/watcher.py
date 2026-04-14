#!/usr/bin/env python3
"""Herald Watcher — monitors Claude Code transcript JSONL files for <tts> blocks.

Races the Stop hook by detecting TTS content as soon as it's written to the
transcript file. When a <tts> block is found, immediately sends it to the
Kokoro daemon and enqueues the result.

Usage: watcher.py [--watch-dir DIR]
  Default watch dir: ~/.claude/projects/
"""

import glob
import json
import os
import re
import hashlib
import signal
import socket
import subprocess
import sys
import time

# User-scoped temp dir — matches heyvox.constants._TMP (cannot import package here).
_TMP = os.environ.get("TMPDIR", "/tmp").rstrip("/")

PID_FILE = f"{_TMP}/herald-watcher.pid"  # Must match heyvox.constants.HERALD_WATCHER_PID
HANDLED_FLAG_DIR = f"{_TMP}/herald-watcher-handled"  # Must match heyvox.constants.HERALD_WATCHER_HANDLED_DIR
KOKORO_SOCK = f"{_TMP}/kokoro-daemon.sock"  # Must match heyvox.constants.KOKORO_DAEMON_SOCK
QUEUE_DIR = f"{_TMP}/herald-queue"  # Must match heyvox.constants.HERALD_QUEUE_DIR
DEBUG_LOG = f"{_TMP}/herald-debug.log"  # Must match heyvox.constants.HERALD_DEBUG_LOG
POLL_INTERVAL = 0.3
CLAIM_DIR = f"{_TMP}/herald-claim"  # Must match heyvox.constants.HERALD_CLAIM_DIR

file_positions = {}
last_tts_time = 0
TTS_DEDUP_SECS = 3.0


def log(msg):
    ts = time.strftime("%H:%M:%S")
    line = f"[{ts}] herald-watcher: {msg}"
    print(line, flush=True)
    try:
        with open(DEBUG_LOG, "a") as f:
            f.write(f"[{time.strftime('%a %b %d %H:%M:%S %Z %Y')}] {line}\n")
    except Exception:
        pass


def _load_workspace_db_path():
    """Load the workspace DB path from the app profile config.

    Returns the expanded DB path, or empty string if no profile has workspace detection.
    """
    try:
        from heyvox.config import load_config
        cfg = load_config()
        for profile in cfg.app_profiles:
            if profile.has_workspace_detection and profile.workspace_db:
                return os.path.expanduser(profile.workspace_db)
    except Exception:
        pass
    return ""


# Cache the DB path at module level (loaded once on first use)
_cached_ws_db_path = None


def _get_workspace_db_path():
    """Get the cached workspace DB path."""
    global _cached_ws_db_path
    if _cached_ws_db_path is None:
        _cached_ws_db_path = _load_workspace_db_path()
    return _cached_ws_db_path


def get_tts_label(workspace_name):
    """Get workspace TTS label from the workspace-aware app's DB."""
    if not workspace_name:
        return workspace_name
    db_path = _get_workspace_db_path()
    if not db_path:
        return workspace_name
    try:
        # Escape single quotes to prevent SQL injection from workspace names
        safe_name = workspace_name.replace("'", "''")
        r = subprocess.run(
            ["sqlite3", db_path,
             f"SELECT COALESCE(w.pr_title, '') FROM workspaces w WHERE w.directory_name='{safe_name}'"],
            capture_output=True, text=True, timeout=0.5)
        if r.stdout.strip():
            return r.stdout.strip()
    except Exception:
        pass
    return workspace_name


def extract_tts(text):
    """Extract last <tts> block from text, only if it's at the end."""
    matches = re.findall(r'<tts>(.*?)</tts>', text, re.DOTALL)
    if not matches:
        return None
    speech = matches[-1].strip()
    if not speech or speech == "SKIP" or len(speech) < 5:
        return None
    last_tts_pos = text.rfind("<tts>")
    if last_tts_pos < len(text) * 0.5:
        return None
    last_close = text.rfind("</tts>")
    remaining = text[last_close + 6:].strip() if last_close >= 0 else ""
    if len(remaining) > 50:
        return None
    return speech


def detect_workspace_from_path(jsonl_path):
    """Extract workspace name from the JSONL path.

    Claude Code stores transcripts in paths like:
      ~/.claude/projects/-Users-<user>-<app>-workspaces-<workspace>/...
    We match against known workspace names from the workspace-aware app's DB.
    """
    import re
    db_path = _get_workspace_db_path()
    if not db_path:
        return ""
    parts = jsonl_path.split("/")
    for part in parts:
        # Match any workspace path pattern (not hardcoded to a specific app)
        match = re.search(r"-Users-[^-]+-\w+-workspaces-(.+)", part)
        if match:
            remainder = match.group(1)
            try:
                r = subprocess.run(
                    ["sqlite3", db_path,
                     "SELECT directory_name FROM workspaces"],
                    capture_output=True, text=True, timeout=0.5)
                for ws_name in r.stdout.strip().split("\n"):
                    ws_name = ws_name.strip()
                    if ws_name and ws_name.replace("/", "-") in remainder:
                        return ws_name
            except Exception:
                pass
    return ""


VERBOSITY_FILE = f"{_TMP}/heyvox-verbosity"  # Must match heyvox.constants.VERBOSITY_FILE


def _get_verbosity():
    """Read verbosity from shared state file. Returns 'full' if absent."""
    try:
        with open(VERBOSITY_FILE) as f:
            level = f.read().strip()
        return level if level in ("full", "summary", "short", "skip") else "full"
    except FileNotFoundError:
        return "full"


def _apply_verbosity(text, verbosity):
    """Apply TTS playback filtering. Returns None to skip."""
    if verbosity == "skip":
        return None
    if verbosity == "short":
        match = re.search(r'[.!?]', text)
        if match:
            return text[:match.end()].strip()
        return text[:100]
    # "full" and "summary" (legacy) both play everything
    return text


def send_to_kokoro(speech, voice="af_sarah", lang="en-us", speed=1.2,
                    workspace="", hook_epoch_ms=0):
    """Send speech text to Kokoro daemon and enqueue result."""
    global last_tts_time

    # Apply verbosity filtering before synthesis
    verbosity = _get_verbosity()
    speech = _apply_verbosity(speech, verbosity)
    if speech is None:
        log("Verbosity=skip, dropping TTS")
        return False

    now = time.time()
    if now - last_tts_time < TTS_DEDUP_SECS:
        log(f"Dedup: skipping (last TTS {now - last_tts_time:.1f}s ago)")
        return False

    watcher_start_ms = int(time.time() * 1000)
    if not hook_epoch_ms:
        hook_epoch_ms = watcher_start_ms

    timestamp = str(time.time_ns())

    label = get_tts_label(workspace)
    if label:
        spoken_label = label.replace(" \u00b7 ", ", ")
        speech = f"{spoken_label}: {speech}"

    voice = detect_mood_voice(speech)

    temp_wav = f"{_TMP}/herald-watcher-{os.getpid()}.wav"  # Must match heyvox.constants.HERALD_WATCHER_PID prefix

    req = json.dumps({
        "text": speech,
        "voice": voice,
        "lang": lang,
        "speed": speed,
        "output": temp_wav,
    })

    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.connect(KOKORO_SOCK)
            s.sendall(req.encode())
            s.shutdown(socket.SHUT_WR)
            resp = b""
            while True:
                chunk = s.recv(4096)
                if not chunk:
                    break
                resp += chunk
        data = json.loads(resp)

        if not data.get("ok"):
            log(f"Kokoro error: {data.get('error')}")
            return False

        os.makedirs(QUEUE_DIR, exist_ok=True)
        tts_end_ms = int(time.time() * 1000)

        # Check if multi-part — write manifest so orchestrator waits for all parts
        parts_count = data.get("parts", 1)
        parts_file = f"{QUEUE_DIR}/{timestamp}.parts"
        if parts_count > 1:
            with open(parts_file, "w") as f:
                f.write(str(parts_count))

        wav_name = f"{timestamp}-01.wav"
        os.rename(temp_wav, f"{QUEUE_DIR}/{wav_name}")
        if workspace:
            with open(f"{QUEUE_DIR}/{wav_name.replace('.wav', '.workspace')}", "w") as f:
                f.write(workspace)
        # Write timing sidecar
        timing_str = f"{hook_epoch_ms}|{watcher_start_ms}|{watcher_start_ms}|{tts_end_ms}"
        with open(f"{QUEUE_DIR}/{wav_name.replace('.wav', '.timing')}", "w") as f:
            f.write(timing_str)

        base = temp_wav.replace(".wav", "")
        part = 2
        while os.path.exists(f"{base}.part{part}.wav"):
            part_name = f"{timestamp}-{part:02d}.wav"
            os.rename(f"{base}.part{part}.wav", f"{QUEUE_DIR}/{part_name}")
            if workspace:
                with open(f"{QUEUE_DIR}/{part_name.replace('.wav', '.workspace')}", "w") as f:
                    f.write(workspace)
            part_ms = int(time.time() * 1000)
            with open(f"{QUEUE_DIR}/{part_name.replace('.wav', '.timing')}", "w") as f:
                f.write(f"{hook_epoch_ms}|{watcher_start_ms}|{watcher_start_ms}|{part_ms}")
            part += 1

        # Remove parts manifest — all parts are enqueued
        try:
            os.unlink(parts_file)
        except FileNotFoundError:
            pass

        log(f"TIMING: watcher tts={tts_end_ms - watcher_start_ms}ms, hook->enqueue={tts_end_ms - hook_epoch_ms}ms")
        log(f"Enqueued {part - 1} part(s) in {data['duration']:.2f}s, ws={workspace}")
        last_tts_time = time.time()

        return True

    except FileNotFoundError:
        log("Kokoro daemon not running, skipping")
        return False
    except Exception as e:
        log(f"Error sending to Kokoro: {e}")
        return False
    finally:
        # Clean up temp WAV if it was not moved to the queue
        try:
            os.unlink(temp_wav)
        except FileNotFoundError:
            pass


def detect_mood_voice(text):
    """Match the mood detection in worker.sh."""
    t = text.lower()
    if any(w in t for w in ["error", "fail", "broke", "crash", "warning",
                             "careful", "danger", "critical", "urgent",
                             "problem", "bug"]):
        return "af_nova"
    if any(w in t for w in ["done", "success", "passed", "complete", "fixed",
                             "great", "perfect", "working", "deployed",
                             "shipped", "merged"]):
        return "af_heart"
    if any(w in t for w in ["should we", "want me to", "would you",
                             "what do you", "how about", "shall i",
                             "let me know"]):
        return "af_sky"
    return "af_sarah"


def find_active_transcripts():
    """Find all recent JSONL transcript files across all workspaces."""
    base = os.path.expanduser("~/.claude/projects")
    pattern = os.path.join(base, "*", "*.jsonl")
    files = glob.glob(pattern)
    cutoff = time.time() - 3600
    return [f for f in files if os.path.getmtime(f) > cutoff]


def process_new_lines(filepath):
    """Read new lines from a JSONL file and check for TTS blocks."""
    pos = file_positions.get(filepath, 0)

    try:
        size = os.path.getsize(filepath)
        if size <= pos:
            return

        with open(filepath) as f:
            f.seek(pos)
            new_data = f.read()
            file_positions[filepath] = f.tell()

        workspace = detect_workspace_from_path(filepath)

        for line in new_data.strip().split("\n"):
            if not line.strip():
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue

            if d.get("type") != "assistant":
                continue

            content = d.get("message", {}).get("content", [])
            for block in content:
                if not isinstance(block, dict) or block.get("type") != "text":
                    continue
                text = block.get("text", "")
                speech = extract_tts(text)
                if speech:
                    speech_hash = hashlib.md5(speech.encode()).hexdigest()[:16]
                    os.makedirs(CLAIM_DIR, exist_ok=True)
                    claim_file = f"{CLAIM_DIR}/{speech_hash}"
                    try:
                        fd = os.open(claim_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                        os.write(fd, b"watcher")
                        os.close(fd)
                    except FileExistsError:
                        log(f"Skipped (hook claimed {speech_hash}): \"{speech[:50]}...\"")
                        continue
                    detect_ms = int(time.time() * 1000)
                    log(f"Detected TTS in {os.path.basename(filepath)}: "
                        f"\"{speech[:50]}...\"")
                    ok = send_to_kokoro(speech, workspace=workspace,
                                        hook_epoch_ms=detect_ms)
                    if not ok:
                        try:
                            os.unlink(claim_file)
                        except OSError:
                            pass
                        log(f"Released claim {speech_hash} (send failed)")

    except Exception as e:
        log(f"Error processing {filepath}: {e}")


def main():
    # Own process group so other daemon restarts don't kill us
    try:
        os.setpgrp()
    except OSError:
        pass

    try:
        with open(PID_FILE) as _f:
            old_pid = int(_f.read().strip())
        os.kill(old_pid, signal.SIGTERM)
        time.sleep(0.5)
    except Exception:
        pass

    with open(PID_FILE, "w") as f:
        f.write(str(os.getpid()))

    os.makedirs(HANDLED_FLAG_DIR, exist_ok=True)

    log("Starting Herald watcher")

    def handle_signal(signum, frame):
        log("Shutting down")
        try:
            os.unlink(PID_FILE)
        except Exception:
            pass
        sys.exit(0)

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    for filepath in find_active_transcripts():
        file_positions[filepath] = os.path.getsize(filepath)
        log(f"Watching: {os.path.basename(filepath)} (pos={file_positions[filepath]})")

    while True:
        for filepath in find_active_transcripts():
            if filepath not in file_positions:
                file_positions[filepath] = os.path.getsize(filepath)
                log(f"New transcript: {os.path.basename(filepath)}")
            process_new_lines(filepath)

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
