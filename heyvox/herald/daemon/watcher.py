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

PID_FILE = "/tmp/herald-watcher.pid"
HANDLED_FLAG_DIR = "/tmp/herald-watcher-handled"
KOKORO_SOCK = "/tmp/kokoro-daemon.sock"
QUEUE_DIR = "/tmp/herald-queue"
DEBUG_LOG = "/tmp/herald-debug.log"
POLL_INTERVAL = 0.3
CLAIM_DIR = "/tmp/herald-claim"

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


def get_tts_label(workspace_name):
    """Get workspace TTS label from Conductor DB."""
    if not workspace_name:
        return workspace_name
    try:
        db_path = os.path.expanduser(
            "~/Library/Application Support/com.conductor.app/conductor.db")
        r = subprocess.run(
            ["sqlite3", db_path,
             f"SELECT COALESCE(w.pr_title, '') FROM workspaces w WHERE w.directory_name='{workspace_name}'"],
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
      ~/.claude/projects/-Users-<user>-conductor-workspaces-<workspace>/...
    We match against known Conductor workspace names from the DB.
    """
    import re
    parts = jsonl_path.split("/")
    for part in parts:
        # Match any user's Conductor workspace path (not just a specific username)
        match = re.search(r"-Users-[^-]+-conductor-workspaces-(.+)", part)
        if match:
            remainder = match.group(1)
            try:
                db_path = os.path.expanduser(
                    "~/Library/Application Support/com.conductor.app/conductor.db")
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


VERBOSITY_FILE = "/tmp/heyvox-verbosity"


def _get_verbosity():
    """Read verbosity from shared state file. Returns 'full' if absent."""
    try:
        with open(VERBOSITY_FILE) as f:
            level = f.read().strip()
        return level if level in ("full", "summary", "short", "skip") else "full"
    except FileNotFoundError:
        return "full"


def _apply_verbosity(text, verbosity):
    """Apply verbosity filtering to speech text. Returns None to skip."""
    if verbosity == "skip":
        return None
    if verbosity == "full":
        return text
    if verbosity == "short":
        match = re.search(r'[.!?]', text)
        if match:
            return text[:match.end()].strip()[:100]
        return text[:100]
    if verbosity == "summary":
        if len(text) <= 150:
            return text
        trunc = text[:150]
        last_sp = trunc.rfind(' ')
        if last_sp > 0:
            trunc = trunc[:last_sp]
        return trunc + "..."
    return text


def send_to_kokoro(speech, voice="af_sarah", lang="en-us", speed=1.2,
                    workspace=""):
    """Send speech text to Kokoro daemon and enqueue result."""
    global last_tts_time

    # Apply verbosity filtering before synthesis
    verbosity = _get_verbosity()
    speech = _apply_verbosity(speech, verbosity)
    if speech is None:
        log(f"Verbosity=skip, dropping TTS")
        return False

    now = time.time()
    if now - last_tts_time < TTS_DEDUP_SECS:
        log(f"Dedup: skipping (last TTS {now - last_tts_time:.1f}s ago)")
        return False

    timestamp = str(time.time_ns())

    label = get_tts_label(workspace)
    if label:
        spoken_label = label.replace(" \u00b7 ", ", ")
        speech = f"{spoken_label}: {speech}"

    voice = detect_mood_voice(speech)

    temp_wav = f"/tmp/herald-watcher-{os.getpid()}.wav"

    req = json.dumps({
        "text": speech,
        "voice": voice,
        "lang": lang,
        "speed": speed,
        "output": temp_wav,
    })

    try:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(KOKORO_SOCK)
        s.sendall(req.encode())
        s.shutdown(socket.SHUT_WR)
        resp = b""
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            resp += chunk
        s.close()
        data = json.loads(resp)

        if not data.get("ok"):
            log(f"Kokoro error: {data.get('error')}")
            return False

        os.makedirs(QUEUE_DIR, exist_ok=True)
        wav_name = f"{timestamp}-01.wav"
        os.rename(temp_wav, f"{QUEUE_DIR}/{wav_name}")
        if workspace:
            with open(f"{QUEUE_DIR}/{wav_name.replace('.wav', '.workspace')}", "w") as f:
                f.write(workspace)

        base = temp_wav.replace(".wav", "")
        part = 2
        while os.path.exists(f"{base}.part{part}.wav"):
            part_name = f"{timestamp}-{part:02d}.wav"
            os.rename(f"{base}.part{part}.wav", f"{QUEUE_DIR}/{part_name}")
            if workspace:
                with open(f"{QUEUE_DIR}/{part_name.replace('.wav', '.workspace')}", "w") as f:
                    f.write(workspace)
            part += 1

        log(f"Enqueued {part - 1} part(s) in {data['duration']:.2f}s, ws={workspace}")
        last_tts_time = time.time()

        return True

    except FileNotFoundError:
        log("Kokoro daemon not running, skipping")
        return False
    except Exception as e:
        log(f"Error sending to Kokoro: {e}")
        return False


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
                    log(f"Detected TTS in {os.path.basename(filepath)}: "
                        f"\"{speech[:50]}...\"")
                    ok = send_to_kokoro(speech, workspace=workspace)
                    if not ok:
                        try:
                            os.unlink(claim_file)
                        except OSError:
                            pass
                        log(f"Released claim {speech_hash} (send failed)")

    except Exception as e:
        log(f"Error processing {filepath}: {e}")


def main():
    try:
        old_pid = int(open(PID_FILE).read().strip())
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
