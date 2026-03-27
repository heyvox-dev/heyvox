# Phase 3: CLI + TTS Output - Research

**Researched:** 2026-03-27
**Domain:** Python CLI (argparse), Kokoro TTS (PyTorch), sounddevice audio playback, launchd service management, macOS permission deep-links
**Confidence:** HIGH (stack and APIs verified), MEDIUM (TTS interrupt pattern), HIGH (launchd)

## Summary

Phase 3 adds two major capabilities: full launchd-managed service lifecycle (start/stop/restart/status/logs) plus real TTS synthesis using Kokoro 82M. The existing `vox/cli.py` already has the subcommand scaffold — only the implementations need to be filled in. The existing `vox/audio/tts.py` currently delegates to a bash script; Phase 3 replaces that with a Python TTS engine: `KPipeline` (kokoro) → numpy audio arrays → `sounddevice.play()` with interruptible queue.

The critical architectural decision is threading: TTS synthesis runs in a background thread, audio playback runs via `sounddevice.play()` which is non-blocking by default, and a `threading.Event` stop flag enables immediate interruption when wake word or PTT is detected. The prototype proved the queue needs a MAX_HELD cap (5 messages) and volume must be read dynamically from macOS system volume via `osascript`. These are proven patterns that must be carried forward.

The `vox setup` command is the most complex CLI task: it requires checking three macOS TCC permissions (Accessibility, Microphone, Screen Recording), opening deep-link URLs to the correct System Settings panes, and downloading Kokoro model weights from Hugging Face. All three have verified URL patterns and API paths.

**Primary recommendation:** Use `kokoro>=0.9.4` with `sounddevice>=0.5.0` for TTS. Wire interruption via a shared `threading.Event` stop flag checked in the synthesis loop. Use `launchctl bootstrap/bootout` (not deprecated load/unload) for service management. Use `rich` for setup progress display.

## Standard Stack

### Core
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| kokoro | >=0.9.4 | TTS synthesis via KPipeline | Already chosen (CLAUDE.md), proven in prototype, 82M params, Apache license, local-only |
| sounddevice | >=0.5.0 | NumPy array audio playback with stop() | Cross-platform PortAudio bindings, sd.play()/sd.stop() API proven on macOS, latest 0.5.5 (Jan 2026) |
| rich | >=13.0 | Progress bars, colored terminal output for setup | Already in ecosystem (Python), best-in-class for CLI setup UX |

### Supporting
| Library | Version | Purpose | When to Use |
|---------|---------|---------|-------------|
| huggingface_hub | latest (via kokoro dep) | Model download with progress | Already pulled in as kokoro dependency; use `snapshot_download()` for Kokoro model weights |
| soundfile | >=0.12.0 | Save TTS audio to .wav if needed | Only for debug/export; primary path is sounddevice playback |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| sounddevice | pyaudio | pyaudio already used for mic input — DO NOT use pyaudio for TTS output (separate stream conflicts, callback complexity); sounddevice uses PortAudio differently and is cleaner for playback-only |
| sounddevice | subprocess afplay | afplay cannot stop mid-playback reliably from Python; sounddevice.stop() is instant |
| kokoro KPipeline | kokoro-onnx | ONNX variant avoids PyTorch dependency but loses Apple Silicon acceleration; stick with KPipeline since project already requires Apple Silicon (MLX Whisper) |
| rich | tqdm | rich is more capable for setup wizard UX (spinners, panels, rules, status); tqdm is fine for simple progress bars only |

**Installation (additions to pyproject.toml):**
```bash
pip install "kokoro>=0.9.4" "sounddevice>=0.5.0" "rich>=13.0" soundfile
# kokoro pulls in: torch, transformers, huggingface_hub, numpy, misaki[en], loguru
```

**Note:** kokoro requires Python `>=3.10, <3.14`. Vox targets Python 3.12+ so this is compatible. kokoro has a `torch` dependency — this is a large install (~1-2GB). The setup command should warn users.

## Architecture Patterns

### Recommended Module Structure for Phase 3
```
vox/
├── cli.py                    # Fill in: start/stop/restart/status/logs/speak/skip/mute
├── audio/
│   └── tts.py               # REPLACE stub: full Kokoro TTS engine
├── setup/
│   └── wizard.py            # NEW: vox setup interactive wizard
└── constants.py             # Add: TTS_PLAYING_FLAG already exists
```

### Pattern 1: TTS Engine with Interruptible Queue
**What:** Background thread runs KPipeline synthesis, another monitors a stop Event, sounddevice.play() is called per chunk with the stop flag checked between chunks.
**When to use:** Whenever TTS is queued (MCP call or `vox speak`)

```python
# Source: kokoro PyPI docs (verified), sounddevice docs (verified)
import threading
import queue
import sounddevice as sd
from kokoro import KPipeline

_pipeline = None  # initialized once on first use (lazy, avoids startup cost)
_tts_queue: queue.Queue = queue.Queue()
_stop_event = threading.Event()
MAX_HELD = 5  # proven cap from prototype

def get_pipeline() -> KPipeline:
    global _pipeline
    if _pipeline is None:
        _pipeline = KPipeline(lang_code='a')  # 'a' = US English
    return _pipeline

def speak(text: str, voice: str = 'af_heart', speed: float = 1.0) -> None:
    """Enqueue text for TTS synthesis and playback."""
    # Drop oldest if queue full (prototype lesson: stale messages accumulate)
    while _tts_queue.qsize() >= MAX_HELD:
        try:
            _tts_queue.get_nowait()
        except queue.Empty:
            break
    _tts_queue.put((text, voice, speed))

def interrupt() -> None:
    """Stop current TTS immediately — called on wake word or PTT detected."""
    _stop_event.set()
    sd.stop()  # Immediately halts sounddevice playback

def _tts_worker() -> None:
    """Background thread: dequeue and synthesize+play TTS."""
    pipeline = get_pipeline()
    while True:
        text, voice, speed = _tts_queue.get()
        if text is None:  # shutdown sentinel
            break
        _stop_event.clear()
        _set_tts_flag(True)
        try:
            for _gs, _ps, audio in pipeline(text, voice=voice, speed=speed):
                if _stop_event.is_set():
                    break
                sd.play(audio, samplerate=24000)
                sd.wait()  # blocks until chunk done, can be interrupted by sd.stop()
        finally:
            _set_tts_flag(False)
        _tts_queue.task_done()
```

### Pattern 2: TTS Flag IPC (Echo Suppression Integration)
**What:** Write `/tmp/vox-tts-playing` when TTS starts, remove when done. Already consumed by main.py echo suppression.
**When to use:** Always — wrap synthesis in try/finally.

```python
# Source: vox/constants.py (existing), vox/main.py echo suppression (existing)
import os
from pathlib import Path
from vox.constants import TTS_PLAYING_FLAG

def _set_tts_flag(active: bool) -> None:
    flag = Path(TTS_PLAYING_FLAG)
    if active:
        flag.touch()
    else:
        flag.unlink(missing_ok=True)
```

### Pattern 3: Dynamic Volume Read + Restore
**What:** Read macOS system volume via osascript before playback, boost by configurable amount, cap at 100, restore after.
**When to use:** Every TTS playback session (proven necessary from prototype — fixed volume was too loud).

```python
# Source: osascript docs verified via WebSearch
import subprocess

def _get_system_volume() -> int:
    """Read current macOS output volume (0-100)."""
    result = subprocess.run(
        ["osascript", "-e", "output volume of (get volume settings)"],
        capture_output=True, text=True, timeout=3
    )
    return int(result.stdout.strip())

def _set_system_volume(level: int) -> None:
    subprocess.run(
        ["osascript", "-e", f"set volume output volume {level}"],
        timeout=3
    )
```

### Pattern 4: launchd Service Management
**What:** Use `launchctl bootstrap/bootout` (modern API) not deprecated `load/unload`.
**When to use:** `vox start`, `vox stop`, `vox restart`, `vox status`

```python
# Source: launchd.info (verified)
import subprocess
import os
from pathlib import Path
from vox.constants import LAUNCHD_LABEL

PLIST_DIR = Path.home() / "Library" / "LaunchAgents"
PLIST_PATH = PLIST_DIR / f"{LAUNCHD_LABEL}.plist"
GUI_DOMAIN = f"gui/{os.getuid()}"

def _bootstrap() -> int:
    """Load and start service. Returns returncode."""
    return subprocess.run(
        ["launchctl", "bootstrap", GUI_DOMAIN, str(PLIST_PATH)]
    ).returncode

def _bootout() -> int:
    """Stop and unload service. Returns returncode."""
    return subprocess.run(
        ["launchctl", "bootout", GUI_DOMAIN, str(PLIST_PATH)]
    ).returncode

def _status() -> dict:
    """Returns {'running': bool, 'pid': int|None, 'exit_code': int|None}."""
    result = subprocess.run(
        ["launchctl", "list", LAUNCHD_LABEL],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        return {"running": False, "pid": None, "exit_code": None}
    # Output: PID ExitStatus Label (tab-separated)
    parts = result.stdout.strip().split("\t")
    pid = int(parts[0]) if parts[0] != "-" else None
    return {"running": pid is not None, "pid": pid, "exit_code": int(parts[1])}
```

**launchd plist template** (written by `vox setup`):
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.vox.listener</string>
    <key>ProgramArguments</key>
    <array>
        <string>/path/to/python3</string>
        <string>-m</string>
        <string>vox</string>
        <string>start</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/tmp/vox.log</string>
    <key>StandardErrorPath</key>
    <string>/tmp/vox.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin</string>
    </dict>
</dict>
</plist>
```

**Critical:** Use `sys.executable` (not hardcoded path) when writing ProgramArguments — this captures the active venv Python.

### Pattern 5: macOS Permission Deep-Links
**What:** Open specific System Settings pane via URL scheme rather than generic "open System Settings".
**When to use:** `vox setup` when permission check fails.

```python
# Source: verified via WebSearch + GitHub gist rmcdongit
PERMISSION_URLS = {
    "microphone": "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_Microphone",
    "accessibility": "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_Accessibility",
    "screen_recording": "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_ScreenCapture",
}

def open_permission_settings(permission: str) -> None:
    url = PERMISSION_URLS[permission]
    subprocess.run(["open", url])
```

**Note on checking permissions:** Directly querying the TCC sqlite database requires Full Disk Access (catch-22). The practical approach is: attempt the operation and catch the failure, then redirect to System Settings. For Accessibility, use `AXIsProcessTrusted()` via PyObjC. For Microphone, attempt `AVCaptureDevice.requestAccessForMediaType_completionHandler_` — but this is complex. Simplest reliable check: try the action, catch `PermissionError`/non-zero return from subprocess, then guide user.

### Pattern 6: Verbosity Mode Filtering
**What:** TTS verbosity (full/summary/short/skip) applied before enqueuing text.
**When to use:** Every MCP `voice_speak` call and `vox speak`.

```python
# Source: project requirements TTS-01, TTS-02
from enum import Enum

class Verbosity(str, Enum):
    FULL = "full"
    SUMMARY = "summary"
    SHORT = "short"
    SKIP = "skip"

def apply_verbosity(text: str, verbosity: Verbosity) -> str | None:
    """Return text to speak, or None if should be skipped."""
    if verbosity == Verbosity.SKIP:
        return None
    if verbosity == Verbosity.FULL:
        return text
    if verbosity == Verbosity.SHORT:
        # One sentence — take up to first sentence break
        import re
        match = re.search(r'[.!?]', text)
        return text[:match.end()] if match else text[:100]
    if verbosity == Verbosity.SUMMARY:
        # Truncate to ~150 chars at word boundary
        if len(text) <= 150:
            return text
        trunc = text[:150].rsplit(' ', 1)[0]
        return trunc + "…"
    return text
```

### Anti-Patterns to Avoid
- **Using `afplay` for TTS output:** Cannot stop mid-playback from Python reliably. Use `sounddevice.stop()` instead.
- **Using pyaudio for playback:** pyaudio is already claimed by mic input; mixing streams causes conflicts. sounddevice is the right tool for output.
- **Global KPipeline at import time:** Kokoro loads PyTorch models — 2-3 second startup. Use lazy initialization on first `speak()` call.
- **Using `launchctl load/unload`:** These are deprecated. Use `bootstrap`/`bootout`.
- **Writing plist with hardcoded python path:** Always use `sys.executable` for the Python path in ProgramArguments.
- **TTS queue without cap:** Prototype found 52 stale messages. Always enforce MAX_HELD=5, drop oldest.
- **Fixed TTS volume:** Prototype lesson — read system volume dynamically, apply configurable boost, restore after.
- **Blocking the main thread during synthesis:** Synthesis is slow (model inference). Always use background thread.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| TTS audio playback | Custom pyaudio output stream | `sounddevice.play()` + `sd.stop()` | PortAudio handles device routing, sample rate conversion, thread safety; stop() is immediate |
| Terminal progress/colors | ANSI escape codes manually | `rich` Progress, Spinner, Panel | Handles terminal width, Windows compat, no-color detection, all baked in |
| Model download | Custom HTTP download | `huggingface_hub.snapshot_download()` | Resume-capable, cache-aware, already pulled in by kokoro dep |
| Audio chunking for streaming | Custom text splitter | kokoro's `split_pattern` parameter | KPipeline accepts `split_pattern=r'\n+'` to chunk text for streaming synthesis |

**Key insight:** The kokoro generator pattern already solves streaming TTS — it yields audio chunks as they synthesize rather than waiting for the entire text. This gives immediate start-of-speech while long responses continue synthesizing.

## Common Pitfalls

### Pitfall 1: Kokoro KPipeline is Not Thread-Safe
**What goes wrong:** Two threads calling the same `KPipeline` instance concurrently causes crashes or corrupted audio.
**Why it happens:** PyTorch model inference is not reentrant.
**How to avoid:** Single worker thread owns the pipeline. All synthesis goes through a queue consumed by that single thread.
**Warning signs:** Random crashes during rapid TTS invocations.

### Pitfall 2: sounddevice.wait() Blocks Forever If stop() Called Before wait()
**What goes wrong:** Race condition — if `sd.stop()` is called before `sd.wait()`, wait() may never return.
**Why it happens:** PortAudio callback state machine edge case.
**How to avoid:** Check stop_event after `sd.play()` before calling `sd.wait()`. Use a short timeout: `sd.wait()` does not accept a timeout parameter — wrap it in a thread with join(timeout=0.5) instead. Alternatively, use sd.play(blocking=True) in the worker thread where stop() from another thread will interrupt it.
**Warning signs:** TTS hangs after interrupt command.

### Pitfall 3: launchd Environment Variables Missing
**What goes wrong:** `vox start` via launchd fails because $PATH doesn't include venv or Homebrew paths.
**Why it happens:** launchd spawns processes in a bare environment — not your shell's $PATH.
**How to avoid:** Explicitly set EnvironmentVariables in the plist. Better: use the full absolute path to the venv's Python via `sys.executable` in ProgramArguments.
**Warning signs:** `vox status` shows launchd loaded but process crashes immediately with ModuleNotFoundError.

### Pitfall 4: Kokoro Model Download on First synthesis (Unexpected Delay)
**What goes wrong:** First `vox speak` takes 30-60 seconds while Kokoro downloads model weights from Hugging Face (~300MB).
**Why it happens:** `KPipeline.__init__` triggers `huggingface_hub` download if model not cached.
**How to avoid:** `vox setup` explicitly downloads the model with progress indication (CLI-04). After setup, the model is cached in `~/.cache/huggingface/hub/`.
**Warning signs:** First TTS call hangs silently.

### Pitfall 5: TTS Playing Flag Left Behind on Crash
**What goes wrong:** `/tmp/vox-tts-playing` persists after crash, permanently muting the mic via echo suppression.
**Why it happens:** TTS process crashes before the `finally` block.
**How to avoid:** `TTS_PLAYING_MAX_AGE_SECS = 60.0` already in constants.py — main.py ignores stale flags. Ensure TTS worker uses try/finally to remove flag. Also: startup should clean up any existing stale flag.
**Warning signs:** Mic permanently muted after crash; wake word never triggers.

### Pitfall 6: macOS TCC Permission Check Race Condition
**What goes wrong:** `vox setup` reports permissions OK, but launchd-launched process still lacks permission.
**Why it happens:** TCC grants permission per-bundle-identifier. A Terminal-launched Python and a launchd-launched Python may be seen as different clients.
**How to avoid:** `vox setup` should instruct user to grant permission to the actual launchd agent (not just Terminal). Provide clear instructions; don't try to programmatically grant permissions.
**Warning signs:** Microphone/Accessibility errors only when running as launchd service, not when running `vox start` directly.

### Pitfall 7: Kokoro Requires Python < 3.14
**What goes wrong:** If project Python requirement is bumped past 3.13, kokoro installation fails.
**Why it happens:** kokoro specifies `requires-python = ">=3.10, <3.14"`.
**How to avoid:** Document this. Current Vox target is Python 3.12 — safe.
**Warning signs:** `pip install kokoro` fails with Python version error.

## Code Examples

### Minimal Kokoro synthesis (verified pattern)
```python
# Source: https://pypi.org/project/kokoro/ (verified 2026-03-27)
from kokoro import KPipeline
import sounddevice as sd

pipeline = KPipeline(lang_code='a')  # US English
stop_event = threading.Event()

for gs, ps, audio in pipeline("Hello, world!", voice='af_heart', speed=1.0):
    if stop_event.is_set():
        break
    sd.play(audio, samplerate=24000)
    sd.wait()
```

### Read macOS system volume (verified)
```python
# Source: osascript, verified via WebSearch multiple sources
import subprocess

def get_system_volume() -> int:
    r = subprocess.run(
        ["osascript", "-e", "output volume of (get volume settings)"],
        capture_output=True, text=True, timeout=3
    )
    return int(r.stdout.strip())
```

### launchctl status check (verified)
```python
# Source: launchd.info (verified)
import subprocess
from vox.constants import LAUNCHD_LABEL

result = subprocess.run(
    ["launchctl", "list", LAUNCHD_LABEL],
    capture_output=True, text=True
)
# returncode != 0 means not loaded
# stdout: "PID\tExitCode\tLabel"
```

### Open macOS permission settings (verified)
```python
# Source: github.com/rmcdongit (gist, verified)
import subprocess
subprocess.run(["open",
    "x-apple.systempreferences:com.apple.settings.PrivacySecurity.extension?Privacy_Microphone"
])
```

### rich progress for model download
```python
# Source: rich docs (verified via pypi.org/project/rich)
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn
from huggingface_hub import snapshot_download

with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}"),
              BarColumn(), transient=True) as progress:
    task = progress.add_task("Downloading Kokoro model...", total=None)
    snapshot_download(repo_id="hexgrad/Kokoro-82M")
    progress.update(task, completed=True)
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| `launchctl load/unload` | `launchctl bootstrap/bootout` | macOS 10.10 (Yosemite) | Old commands still work but deprecated; new commands are more explicit about domain |
| External bash TTS script | Python KPipeline in-process | Phase 3 | Eliminates subprocess overhead, enables direct stop_event integration |
| Fixed TTS volume | Dynamic read + configurable boost | Prototype learning | Prevents TTS being too loud/quiet depending on system volume |
| Unlimited TTS queue | Capped queue (MAX_HELD=5) | Prototype learning | Prevents 50+ stale messages accumulating |

**Deprecated/outdated:**
- `launchctl load/unload`: works but deprecated, use `bootstrap`/`bootout`
- `tts.script_path` in TTSConfig: The Phase 1 `script_path` field was a bridge to the prototype bash script. Phase 3 replaces this with in-process Kokoro. The field can remain for backward compat but the primary TTS path is now Python-native.

## Open Questions

1. **Verbosity "summary" implementation depth**
   - What we know: Requirements say "condensed" — unclear if this means word-count truncation or actual summarization
   - What's unclear: Does summary mode require LLM call to actually summarize, or just truncate?
   - Recommendation: Implement as smart truncation (150 chars at word boundary). LLM summarization would require network call and defeats local-first philosophy. If user wants true summarization, that's a future Pro feature.

2. **Audio ducking (TTS-04)**
   - What we know: "Lower TTS volume briefly on system sounds" — macOS does have audio ducking APIs but they're complex (CoreAudio)
   - What's unclear: Can this be done purely via osascript/subprocess without CoreAudio framework calls?
   - Recommendation: Implement as a simplified version — detect when macOS issues a sound (very hard without CoreAudio) OR interpret this requirement as "reduce TTS volume to X% of system volume" (simpler). Flag for discussion if full ducking on system sounds is required.

3. **`vox setup` MCP auto-approve (CLI-02)**
   - What we know: Requirement mentions "MCP auto-approve" in setup
   - What's unclear: Which MCP client's auto-approve config? Claude Code uses `.mcp.json`, Cursor has its own format
   - Recommendation: Implement for Claude Code first (`~/.claude/settings.json` or `.mcp.json`), document path for other clients. Flag if multi-client support needed in Phase 3.

4. **Kokoro voice selection config**
   - What we know: kokoro has 54+ voices, default `af_heart` is female US English
   - What's unclear: Should voice be user-configurable in `config.yaml`?
   - Recommendation: Add `tts.voice` and `tts.speed` to TTSConfig. Default `af_heart` / `1.0`. Must update TTSConfig in config.py.

## Sources

### Primary (HIGH confidence)
- https://pypi.org/project/kokoro/ — version 0.9.4, KPipeline API, language codes, requirements
- https://github.com/hexgrad/kokoro — pyproject.toml deps: torch, transformers, huggingface_hub, misaki, numpy, loguru
- https://python-sounddevice.readthedocs.io/ — sd.play(), sd.stop(), sd.wait(), 24kHz support
- https://launchd.info/ — bootstrap, bootout, kickstart, list commands; deprecated load/unload
- Existing codebase: vox/constants.py (TTS_PLAYING_FLAG, LAUNCHD_LABEL), vox/config.py (TTSConfig), vox/cli.py (scaffold)

### Secondary (MEDIUM confidence)
- WebSearch + gist: macOS permission URL schemes verified with multiple sources (github.com/rmcdongit gist, macosadventures.com)
- WebSearch: osascript volume read/set pattern — `output volume of (get volume settings)` — confirmed by multiple dev blogs
- WebSearch: sounddevice threading — `sd.stop()` interrupts `sd.play()` instantly on macOS (macOS-specific confirmation, Windows has issues)
- pypi.org/project/sounddevice/ — version 0.5.5 (Jan 23, 2026), PortAudio bindings, MIT license

### Tertiary (LOW confidence)
- Prototype learnings: MAX_HELD=5 cap, dynamic volume boost pattern — these are from the project context/memory, not independently verified source code

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — kokoro and sounddevice verified on PyPI with dates; launchd commands verified on launchd.info
- Architecture: HIGH — patterns derived from verified APIs; TTS interrupt pattern (MEDIUM — sounddevice threading behavior has platform nuances)
- Pitfalls: HIGH for launchd/env/queue pitfalls (verified); MEDIUM for TCC permission check pitfall (based on known macOS behavior)

**Research date:** 2026-03-27
**Valid until:** 2026-06-27 (kokoro is active development; check for API changes; sounddevice stable)
