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

# Shared TTS helpers — single source of truth for mood / verbosity / extraction.
# Watcher.py and worker.py both consume from this module so the two producer
# paths cannot drift on shared logic. See DEFECT-LOG P-producer-parity and
# the comment at the top of heyvox/herald/tts_helpers.py.
from heyvox.herald.tts_helpers import (
    apply_verbosity as _apply_verbosity,
    extract_last_tts_block as extract_tts,
    get_verbosity as _get_verbosity,
    mood_voice as detect_mood_voice,
)

# User-scoped temp dir. _TMP is kept as a module-level constant for the
# IPC-flag paths below — historically watcher.py avoided importing
# heyvox.constants so the polling script could load in minimal Python
# environments. Today the heyvox.* imports above prove the constraint is
# moot, but we keep _TMP as-is to minimise diff churn.
_TMP = os.environ.get("TMPDIR", "/tmp").rstrip("/")

PID_FILE = f"{_TMP}/herald-watcher.pid"
HANDLED_FLAG_DIR = f"{_TMP}/herald-watcher-handled"
KOKORO_SOCK = f"{_TMP}/kokoro-daemon.sock"
QUEUE_DIR = f"{_TMP}/herald-queue"
DEBUG_LOG = f"{_TMP}/herald-debug.log"
POLL_INTERVAL = 0.3
CLAIM_DIR = f"{_TMP}/herald-claim"

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
    Used by detect_workspace_from_path; label resolution itself is delegated to
    heyvox.herald.workspace_label.
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


# extract_tts is imported from heyvox.herald.tts_helpers (see top of file).


def detect_workspace_from_path(jsonl_path):
    """Extract workspace name AND cwd from the JSONL path.

    Claude Code stores transcripts in paths like:
      ~/.claude/projects/-Users-<user>-<app>-workspaces-<workspace>/...
    We match against known workspace names from the workspace-aware app's DB.

    Returns (workspace_name, cwd) — both "" if no match. DEF-248: cwd is the
    matched row's own `workspace_path` column, not a reconstruction of the
    escaped path (un-escaping `-` back to `/` is ambiguous whenever the real
    path itself contains a literal hyphen, e.g. "vox-v2"). Matching forward —
    escape each DB candidate's workspace_path and check if it's a substring of
    the escaped jsonl_path, exactly like the existing directory_name match
    below — sidesteps that ambiguity entirely, mirroring worker.py's DEF-244
    cwd fallback so both TTS-trigger paths carry the same resolution signal.
    """
    db_path = _get_workspace_db_path()
    if not db_path:
        return "", ""
    parts = jsonl_path.split("/")
    for part in parts:
        # Match any workspace path pattern (not hardcoded to a specific app)
        match = re.search(r"-Users-[^-]+-\w+-workspaces-(.+)", part)
        if match:
            remainder = match.group(1)
            try:
                r = subprocess.run(
                    ["sqlite3", db_path,
                     "SELECT directory_name, COALESCE(workspace_path, '') FROM workspaces"],
                    capture_output=True, text=True, timeout=0.5)
                for row in r.stdout.strip().split("\n"):
                    ws_name, _, ws_path = row.strip().partition("|")
                    ws_name = ws_name.strip()
                    if ws_name and ws_name.replace("/", "-") in remainder:
                        return ws_name, ws_path
            except Exception:
                pass
    return "", ""


# _get_verbosity and _apply_verbosity are imported from
# heyvox.herald.tts_helpers (see top of file).
# VERBOSITY_FILE is resolved per-call by the helper so the test fixtures
# that monkeypatch heyvox.constants.VERBOSITY_FILE work uniformly.


def send_to_kokoro(speech, voice="af_sarah", lang="en-us", speed=1.2,
                    workspace="", hook_epoch_ms=0, session_id="", cwd=""):
    """Send speech text to Kokoro daemon and enqueue result."""
    global last_tts_time

    # DEF-237: resolve once per message (all parts below share it), not once
    # per part — same DB round-trip either way for one send_to_kokoro call.
    workspace_id = ""
    if workspace:
        try:
            from heyvox.herald.workspace_label import resolve_workspace_id
            workspace_id = resolve_workspace_id(workspace)
        except Exception:
            pass

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

    # DEF-111: label resolution is shared with worker.py via
    # heyvox.herald.workspace_label so both paths produce the same prefix
    # and honour the same config knobs (tts.announce_workspace,
    # tts.workspace_labels, HEYVOX_WORKSPACE_LABEL, announce_min_chars).
    try:
        from heyvox.config import load_config
        from heyvox.herald.workspace_label import get_workspace_label
        _cfg = load_config()
        _min_chars = getattr(_cfg.tts, "announce_min_chars", 0) or 0
        if _min_chars == 0 or len(speech) >= _min_chars:
            spoken_label = get_workspace_label(workspace, cfg=_cfg)
            if spoken_label:
                speech = f"{spoken_label}: {speech}"
    except Exception as e:
        log(f"workspace_label lookup failed (non-fatal): {e}")

    voice = detect_mood_voice(speech)

    temp_wav = f"{_TMP}/herald-watcher-{os.getpid()}.wav"

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
            from heyvox.herald.workspace_label import write_switch_sidecar
            write_switch_sidecar(f"{QUEUE_DIR}/{wav_name}", workspace, workspace_id, session_id, cwd)
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
                from heyvox.herald.workspace_label import write_switch_sidecar
                write_switch_sidecar(f"{QUEUE_DIR}/{part_name}", workspace, workspace_id, session_id, cwd)
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
        # WATCHER_FIRED — forensic tag for P-producer-parity: counts how often
        # the polling-fallback path actually produces a TTS that the hook
        # path didn't. Used to decide when worker.py can be the sole producer.
        log(f"WATCHER_FIRED Enqueued {part - 1} part(s) in {data['duration']:.2f}s, ws={workspace}")
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


# detect_mood_voice is imported from heyvox.herald.tts_helpers (see top of file).
# That import re-exposes the function under the same name so existing
# callers don't change.


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

        workspace, cwd = detect_workspace_from_path(filepath)
        # DEF-237: Claude Code names transcripts <session-id>.jsonl, so the
        # session that produced this line is the filename stem — no DB
        # lookup needed, unlike workspace_id.
        session_id = os.path.splitext(os.path.basename(filepath))[0]

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
                    # DEF-078: watcher also initiates TTS when the hook loses
                    # the race. Register with the cross-process echo journal so
                    # STT can strip speaker bleed.
                    try:
                        from heyvox.audio.echo import register_tts_text
                        register_tts_text(speech)
                    except Exception as _e:
                        log(f"DEF-078: register_tts_text failed: {_e}")
                    ok = send_to_kokoro(speech, workspace=workspace,
                                        hook_epoch_ms=detect_ms, session_id=session_id,
                                        cwd=cwd)
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
