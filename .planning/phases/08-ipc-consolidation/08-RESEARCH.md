# Phase 8: IPC Consolidation — Research

**Researched:** 2026-04-11
**Domain:** Python IPC, atomic file writes, flag-file consolidation, queue garbage collection
**Confidence:** HIGH

---

<phase_requirements>
## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| IPC-01 | All flag/socket/PID/queue paths consolidated into heyvox/constants.py (single source of truth) | Full inventory of 25 missing paths catalogued; pattern for migrating each file identified |
| IPC-02 | Flag-file constellation replaced with atomic /tmp/heyvox-state.json (temp file + os.rename) | Atomic write pattern documented; fields for state file identified; per-file reader migration catalogued |
| IPC-03 | Periodic garbage collection added for orphaned WAV/timing/workspace files in queue directories | GC trigger point (orchestrator idle loop) identified; file patterns and age thresholds established |
</phase_requirements>

---

## Summary

Phase 8 is a pure-Python refactor with three separable tasks: (1) completing the constants consolidation started in Phase 7, (2) replacing individual flag files with a single atomic JSON state file, and (3) adding queue GC for orphaned WAV/sidecar files.

The codebase already has a solid foundation. `heyvox/constants.py` correctly centralises 25 paths. The remaining work is purely mechanical: 25 more hardcoded `/tmp/` strings scattered across 14 files need to be replaced with imports, two boolean flag files (`/tmp/heyvox-recording` and `/tmp/herald-mute`) and several small state files need to be merged into `/tmp/heyvox-state.json` with atomic writes, and a GC sweep needs to be wired into the orchestrator's existing idle loop.

No new external dependencies are needed. The atomic write pattern (`write temp + os.rename`) is already used in overlay.py (for `TTS_CMD_FILE`) and can be used as the reference implementation throughout.

**Primary recommendation:** Three sequential plans — (1) constants migration for missing paths, (2) atomic state file writer/reader, (3) queue GC routine in orchestrator. Each plan is independently committable and verifiable.

---

## Standard Stack

### Core (all stdlib — no new dependencies)
| Module | Purpose | Why Standard |
|--------|---------|--------------|
| `os.rename` | Atomic file replace on POSIX/macOS | Guaranteed atomic on same filesystem; already used in overlay.py |
| `json` | State file serialisation | Human-readable, easy to debug with `cat` |
| `tempfile.NamedTemporaryFile` | Temp file for atomic write | stdlib, safe across threads |
| `glob.glob` / `pathlib.Path.glob` | Queue GC file discovery | Already used throughout codebase |
| `threading.Lock` | Protect concurrent state file writes | Already in codebase |
| `time.time` | Age-based GC threshold | Already used throughout |

### No New Packages Needed
All required functionality is in the Python 3.12 stdlib. Do not add any new pip dependencies.

---

## Architecture Patterns

### Recommended Project Structure (unchanged)
```
heyvox/
├── constants.py        # ALL /tmp paths declared here — no exceptions
├── ipc/                # NEW: thin module for atomic state file read/write
│   ├── __init__.py     # exports read_state, write_state, update_state
│   └── state.py        # HeyVoxState dataclass + atomic writer
├── herald/
│   └── orchestrator.py # GC routine wired into existing idle loop
```

Note: `heyvox/ipc/` is a natural home for state file logic. The existing `heyvox/hud/ipc.py` handles socket protocol — keep them separate.

### Pattern 1: Atomic State File Write
**What:** Write to a `.tmp` sibling, then `os.rename` atomically replaces the target.
**When to use:** Any time cross-process state changes (recording start/stop, speaking, mute toggle).
**Reference:** Already used in `overlay.py` lines 492-496 for `TTS_CMD_FILE`.

```python
# Source: overlay.py lines 492-496 (existing pattern, verified in codebase)
import json, os, tempfile, threading
from pathlib import Path

_state_lock = threading.Lock()
STATE_FILE = Path("/tmp/heyvox-state.json")

def write_state(updates: dict) -> None:
    """Atomically merge `updates` into the state file."""
    with _state_lock:
        try:
            current = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {}
        except (json.JSONDecodeError, OSError):
            current = {}
        current.update(updates)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(current))
        os.rename(tmp, STATE_FILE)

def read_state() -> dict:
    """Read current state; returns {} on missing or corrupt file."""
    try:
        return json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
```

### Pattern 2: Constants Import Migration
**What:** Replace inline `/tmp/` string with import from `heyvox.constants`.
**When to use:** Every file that currently hardcodes a `/tmp/` path.

```python
# Before
_HUSH_SOCK = "/tmp/hush.sock"
if os.path.exists("/tmp/herald-playing.pid"):

# After
from heyvox.constants import HUSH_SOCK, HERALD_PLAYING_PID
if os.path.exists(HERALD_PLAYING_PID):
```

**Special case — daemon scripts that cannot import heyvox:**
`kokoro-daemon.py` and `watcher.py` are standalone scripts. They should define their own module-level constant matching the value in `constants.py`, with a comment pointing to the source of truth. Full import is not possible without installing the package in daemon context.

### Pattern 3: Queue GC in Orchestrator Idle Loop
**What:** On every idle cycle (queue empty), scan queue/hold/history dirs for stale files and unlink them.
**When to use:** Wired into the existing `else: time.sleep(cfg.poll_interval)` branch.
**Age threshold:** 1 hour for WAV/sidecar files; already used for claim files (line 733).

```python
# Source: orchestrator.py lines 729-736 (existing GC pattern for claim files)
_GC_INTERVAL = 60  # Run GC at most once per minute
_last_gc: float = 0.0

def _gc_queue_dirs(cfg: OrchestratorConfig, debug_log: Path) -> None:
    """Remove orphaned WAV, timing (.txt), and workspace sidecar (.workspace) files."""
    global _last_gc
    now = time.time()
    if now - _last_gc < _GC_INTERVAL:
        return
    _last_gc = now
    max_age = 3600  # 1 hour
    dirs = [cfg.queue_dir, cfg.hold_dir, cfg.history_dir]
    patterns = ["*.wav", "*.txt", "*.workspace"]
    for d in dirs:
        for pattern in patterns:
            for f in d.glob(pattern):
                try:
                    if (now - f.stat().st_mtime) > max_age:
                        f.unlink(missing_ok=True)
                        _herald_log(f"GC: removed orphaned {f.name}", debug_log)
                except OSError:
                    pass
```

### Anti-Patterns to Avoid
- **Importing heyvox from daemon scripts:** `kokoro-daemon.py` and `watcher.py` run as standalone scripts. Importing from `heyvox` package inside them creates dependency issues. Use local constants with source-of-truth comments.
- **Replacing flag files with state file in shell scripts:** The bash `herald/bin/herald` CLI still reads flag files. Do NOT migrate the state file for shell-only callers — only Python callers benefit. The bash CLI is a thin shim anyway (Phase 7 decision).
- **Locking the state file for reads:** Reads should be best-effort (catch OSError/JSONDecodeError). The write lock prevents torn writes but readers should tolerate transient corruption.
- **Using `os.replace` instead of `os.rename`:** `os.replace` is the Python 3 portable name for `os.rename` on POSIX; both are atomic on the same filesystem. Either is fine — prefer `os.rename` to match existing overlay.py usage.
- **State file grows unbounded:** Only write known boolean/scalar fields. Do not store audio buffers or large state in the JSON file.

---

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| Atomic file write | Custom locking + multi-step write | `write temp + os.rename` | Already atomic on POSIX; proven pattern |
| Cross-process mutex | File lock dance | Avoid — use flag file presence or state file field | Sufficient for this use case |
| State file schema validation | JSON Schema / Pydantic | Simple `dict.get` with defaults | No external deps; schema is tiny |
| Recursive stale file cleanup | Custom walker | `Path.glob("*.wav")` + age check | Already in orchestrator claim GC |

---

## IPC-01: Full Inventory of Missing Constants

The following `/tmp/` paths appear in Python source but are NOT yet defined in `heyvox/constants.py`. Each needs a constant added and callers updated.

### High Priority (cross-process coordination — IPC-02 candidates)
| Path | Used In | Notes |
|------|---------|-------|
| `/tmp/heyvox-recording` | Already in constants as RECORDING_FLAG — callers hardcode it anyway | Callers: main.py:242, main.py:345 (use RECORDING_FLAG import) |
| `/tmp/heyvox.pid` | main.py:200, overlay.py:631,689 | Add `HEYVOX_PID_FILE` constant |
| `/tmp/heyvox-heartbeat` | main.py:340,567 | Add `HEYVOX_HEARTBEAT_FILE` constant |
| `/tmp/herald-mute` | tts.py:207, overlay.py:509,751 | Already in constants as HERALD_MUTE_FLAG — fix callers |
| `/tmp/claude-tts-mute` | tts.py:207,239,266, overlay.py:509,751, main.py:245 | Add `CLAUDE_TTS_MUTE_FLAG` constant; legacy path from v1.0 |
| `/tmp/claude-tts-playing.pid` | main.py:345,767 | Add `CLAUDE_TTS_PLAYING_PID` constant; legacy path |
| `/tmp/herald-playing.pid` | mcp/server.py:97, main.py:478 | Already in constants as HERALD_PLAYING_PID — fix callers |

### Medium Priority (Herald internals)
| Path | Used In | Notes |
|------|---------|-------|
| `/tmp/herald-original-vol` | orchestrator.py:60 | Add `HERALD_ORIGINAL_VOL_FILE` constant; migrate OrchestratorConfig default |
| `/tmp/herald-ambient` | main.py:247 | Add `HERALD_AMBIENT_FLAG` constant |
| `/tmp/herald-workspace` | main.py:248 | Add `HERALD_WORKSPACE_FILE` constant |
| `/tmp/herald-generating-{pid}.wav` | worker.py:330 | Add `HERALD_GENERATING_WAV_PATTERN` = `"/tmp/herald-generating-"` (prefix) |
| `/tmp/herald-watcher.pid` | watcher.py:23 | Add `HERALD_WATCHER_PID` — note: watcher imports constants via local var |
| `/tmp/herald-watcher-handled` | watcher.py:24 | Add `HERALD_WATCHER_HANDLED_DIR` |
| `/tmp/herald-media-paused-*` | orchestrator, media.py | Add `HERALD_MEDIA_PAUSED_PREFIX` = `"/tmp/herald-media-paused-"` |

### Lower Priority (HUD / Hush / debug)
| Path | Used In | Notes |
|------|---------|-------|
| `/tmp/heyvox-hud-position.json` | overlay.py:36 | Add `HUD_POSITION_FILE` constant |
| `/tmp/heyvox-hud-stderr.log` | hud/process.py:92 | Add `HUD_STDERR_LOG` constant |
| `/tmp/heyvox-restart.log` | overlay.py:665 | Add `HEYVOX_RESTART_LOG` constant |
| `/tmp/heyvox-tts-style` | tts.py:73 | Add `TTS_STYLE_FILE` constant |
| `/tmp/heyvox-media-paused-rec` | media.py:49 | Add `HEYVOX_MEDIA_PAUSED_REC` constant |
| `/tmp/heyvox-media-paused-*` | media.py:519, main.py:362 | Add `HEYVOX_MEDIA_PAUSED_PREFIX` |
| `/tmp/hush.sock` | media.py:77, injection.py:39, hush_host.py:47, vox-media.py:71 | Add `HUSH_SOCK` constant |
| `/tmp/hush.log` | hush_host.py:50 | Add `HUSH_LOG` constant |
| `/tmp/heyvox.log` | main.py:67, config.py:257, media.py:36 | Already in constants as LOG_FILE_DEFAULT — fix callers |
| `/tmp/kokoro-out.wav` | kokoro-daemon.py:319 | Internal default only — leave as-is (daemon-internal) |

### Standalone Daemon Files (cannot import heyvox package)
These files define their own module-level constants. They cannot import from `heyvox.constants` because they may run before the package is installed. Strategy: keep local definition with a `# Source of truth: heyvox.constants.XXXX` comment.
- `heyvox/herald/daemon/watcher.py` — `PID_FILE`, `KOKORO_SOCK`, `QUEUE_DIR`, etc.
- `heyvox/herald/daemon/kokoro-daemon.py` — `SOCKET_PATH`, `PID_FILE`
- `heyvox/hush/host/hush_host.py` — `SOCKET_PATH`, `LOG_PATH`

---

## IPC-02: Atomic State File Design

### State Fields to Consolidate

The following flag files represent cross-process boolean/scalar state that can be merged into `/tmp/heyvox-state.json`:

| Current Flag File | State Field | Type | Writers | Readers |
|-------------------|-------------|------|---------|---------|
| `/tmp/heyvox-recording` | `recording` | bool | main.py (RecordingStateMachine) | orchestrator.py, echo.py, mcp/server.py |
| `/tmp/heyvox-tts-playing` (TTS_PLAYING_FLAG) | `tts_playing` | bool | tts.py worker | main.py, mcp/server.py, echo.py |
| `/tmp/herald-playing.pid` | `herald_playing_pid` | int or null | orchestrator.py | mcp/server.py, cli.py, overlay.py |
| `/tmp/herald-mute` | `muted` | bool | cli mute command, overlay | orchestrator.py, tts.py, overlay.py |
| `/tmp/heyvox-verbosity` | `verbosity` | str ("full"/"summary"/"short"/"skip") | cli verbosity command | orchestrator.py, tts.py, mcp/server.py |
| `/tmp/herald-pause` | `paused` | bool | cli pause command | orchestrator.py |
| `/tmp/herald-mode` | `herald_mode` | str | herald cli | orchestrator.py |
| `/tmp/herald-last-play` | `last_play_ts` | float (unix timestamp) | orchestrator.py | orchestrator (idle detection) |

### Fields to NOT Consolidate
- PID files (`heyvox.pid`, `herald-orchestrator.pid`, `kokoro-daemon.pid`) — these are lock files with advisory flock semantics, not just state. Keep as separate files.
- Socket files (`heyvox-hud.sock`, `kokoro-daemon.sock`, `hush.sock`) — these are filesystem-addressed sockets, not data files.
- Queue directories (`herald-queue/`, `herald-hold/`) — these are directories with WAV files, not state files.
- Media-paused flags (`heyvox-media-paused-rec`) — these use a "who paused" pattern where the flag's presence AND content matters for coordination. Leave as-is.

### State File Schema
```json
{
  "recording": false,
  "tts_playing": false,
  "herald_playing_pid": null,
  "muted": false,
  "verbosity": "full",
  "paused": false,
  "herald_mode": "ambient",
  "last_play_ts": 0.0
}
```

### Migration Strategy
Migrate incrementally: keep writing both the old flag file AND updating the state file for 1-2 commits (dual-write), then remove old flag file reads. This prevents breaking cross-process coordination mid-migration.

For Phase 8 scope: the state file is the PRIMARY source of truth. Individual flag files become deprecated but may remain for one more phase cycle.

---

## IPC-03: Queue GC Design

### Files to Clean
| Directory | File Pattern | Orphan Condition | Safe to Delete After |
|-----------|-------------|-----------------|----------------------|
| `/tmp/herald-queue/` | `*.wav` | Not claimed, age > 1hr | 1 hour |
| `/tmp/herald-queue/` | `*.workspace` | Sibling WAV missing, age > 1hr | 1 hour |
| `/tmp/herald-queue/` | `*.txt` (timing files) | Sibling WAV missing, age > 1hr | 1 hour |
| `/tmp/herald-hold/` | `*.wav`, `*.workspace` | age > 4hrs (held intentionally) | 4 hours |
| `/tmp/herald-history/` | `*.wav`, `*.workspace` | age > 24hrs | 24 hours |
| `/tmp/herald-claim/` | `*` | age > 1hr | Already implemented in orchestrator |
| `/tmp/herald-watcher-handled/` | `*` | age > 1hr | 1 hour |

### GC Integration Point
The orchestrator's idle loop already has a claim file GC at lines 729-736. The new queue GC extends this with a frequency gate (at most once per minute) and covers the additional directories.

### GC Should NOT Delete
- Files currently being played (`cfg.playing_pid_file` is alive)
- Files younger than their threshold (fresh queue items)
- WAV files whose claim file still exists (being processed)

---

## Common Pitfalls

### Pitfall 1: Circular Import in constants.py
**What goes wrong:** Adding a Path object or importing pathlib at the top of constants.py causes circular imports in modules that do `from heyvox.constants import *` early.
**Why it happens:** constants.py is imported by almost every module; any dependency it adds creates a cycle risk.
**How to avoid:** Keep constants.py as pure string/int/float literals only. Callers convert to `Path` at point of use.
**Warning signs:** `ImportError: cannot import name 'X' from partially initialized module`

### Pitfall 2: os.rename Across Filesystems
**What goes wrong:** `os.rename(tmp, target)` raises `OSError: [Errno 18] Invalid cross-device link` if temp and target are on different filesystems.
**Why it happens:** `/tmp/` is usually tmpfs; this is not an issue if both paths are in `/tmp/`. Risk if config.log_file is changed to a non-tmp location.
**How to avoid:** Always write the temp file to the same directory as the target (`Path(target).with_suffix(".tmp")` or `target.parent / (target.name + ".tmp")`).
**Warning signs:** `OSError: [Errno 18]`

### Pitfall 3: State File Read During Write (torn read)
**What goes wrong:** A reader sees partial JSON (e.g., `{"recording": tr`) if reading while the write is in progress.
**Why it happens:** Python `open().write()` is not atomic — only `os.rename` of the complete file is.
**How to avoid:** Always write to a temp file first, rename last. Readers catch `json.JSONDecodeError` and return `{}`.
**Warning signs:** JSON parse errors in logs

### Pitfall 4: Stale State File After Crash
**What goes wrong:** heyvox crashes with `recording: true` in the state file; next launch reads stale state and thinks it's recording.
**Why it happens:** State file persists across process restarts.
**How to avoid:** In `_acquire_singleton()`, reset transient state fields (`recording`, `tts_playing`, `herald_playing_pid`, `paused`) to their default values when taking over. The existing cleanup in lines 241-254 already does this for flag files — extend it.
**Warning signs:** System appears to be recording after restart

### Pitfall 5: OrchestratorConfig Field Migration
**What goes wrong:** `orchestrator.py` uses `OrchestratorConfig` dataclass with `field(default_factory=lambda: Path("/tmp/..."))`. After IPC-01 migration, these fields should reference the new constants. But the config is passed to standalone `HeraldOrchestrator` instances — if constants change and config defaults don't, they diverge.
**How to avoid:** Update `OrchestratorConfig` default factories to use the constants: `field(default_factory=lambda: Path(HERALD_QUEUE_DIR))`.
**Warning signs:** Orchestrator writes to different path than what cli.py reads

### Pitfall 6: watcher.py Cannot Import heyvox.constants
**What goes wrong:** `watcher.py` is a standalone daemon that runs via subprocess. If it imports `heyvox.constants`, it fails when run outside the installed package context.
**How to avoid:** Keep local module-level constants in watcher.py that mirror the values. Add a comment: `# Must match heyvox/constants.py:HERALD_WATCHER_PID`.
**Warning signs:** `ModuleNotFoundError: No module named 'heyvox'` in watcher stderr

---

## Code Examples

Verified patterns from existing codebase:

### Atomic Write (existing pattern in overlay.py)
```python
# Source: heyvox/hud/overlay.py lines 492-496
tmp_path = cmd_path + ".tmp"
with open(tmp_path, "w") as f:
    f.write(cmd)
os.rename(tmp_path, cmd_path)
```

### Flag File Read with Staleness Guard (existing pattern)
```python
# Source: heyvox/constants.py + echo.py usage
TTS_PLAYING_MAX_AGE_SECS = 60.0  # Guard against permanent mic mute if TTS crashes

def _is_tts_playing() -> bool:
    if not os.path.exists(TTS_PLAYING_FLAG):
        return False
    age = time.time() - os.path.getmtime(TTS_PLAYING_FLAG)
    return age < TTS_PLAYING_MAX_AGE_SECS
```

### Import Pattern for Constants Migration
```python
# Before (heyvox/audio/tts.py)
_STYLE_FILE = "/tmp/heyvox-tts-style"
_MUTE_FLAGS = ["/tmp/claude-tts-mute", "/tmp/herald-mute"]

# After
from heyvox.constants import TTS_STYLE_FILE, CLAUDE_TTS_MUTE_FLAG, HERALD_MUTE_FLAG
_MUTE_FLAGS = [CLAUDE_TTS_MUTE_FLAG, HERALD_MUTE_FLAG]
```

### State File Read (defensive)
```python
def read_state(key: str, default=None):
    try:
        state = json.loads(Path("/tmp/heyvox-state.json").read_text())
        return state.get(key, default)
    except (OSError, json.JSONDecodeError):
        return default
```

### OrchestratorConfig Using Constants
```python
# Before
queue_dir: Path = field(default_factory=lambda: Path("/tmp/herald-queue"))

# After
from heyvox.constants import HERALD_QUEUE_DIR
queue_dir: Path = field(default_factory=lambda: Path(HERALD_QUEUE_DIR))
```

---

## State of the Art

| Old Approach | Current Approach | Impact for Phase 8 |
|--------------|------------------|-------------------|
| Individual flag files per state bit | Atomic JSON state file | 8 flag files → 1 state file for cross-process coordination |
| Module-level hardcoded paths | Named constants with single source of truth | 25 paths need migration; 25 already done |
| GC only for claim files | GC for all queue dirs | Extend existing orchestrator pattern |

---

## Open Questions

1. **Claude-legacy flag files (`/tmp/claude-tts-mute`, `/tmp/claude-tts-playing.pid`)**
   - What we know: These paths exist from the v1.0 era when TTS was a separate `claude-tts` process. They are still checked in `tts.py:207`, `main.py:345`.
   - What's unclear: Are there any external processes (legacy scripts, cron) still writing to these paths that aren't in this repo?
   - Recommendation: Add constants, keep checks for now. Document as "legacy compatibility" constants.

2. **State file location in config vs. hardcoded `/tmp/`**
   - What we know: `config.py` allows `log_file` to be overridden. State file should also be configurable.
   - What's unclear: Does Franz want the state file path configurable?
   - Recommendation: Hardcode to `/tmp/heyvox-state.json` for Phase 8 (same as all other /tmp paths). Post-Phase-8 can expose as config option.

3. **hush/integration/vox-media.py — is this file still used?**
   - What we know: It duplicates `heyvox/audio/media.py` with slightly different paths. It was the standalone integration file before the monorepo merge.
   - What's unclear: Is it still deployed anywhere, or is it dead code kept for reference?
   - Recommendation: Add constants for its paths to maintain consistency, but do not refactor its internals — it may be intentionally standalone.

---

## Environment Availability

Step 2.6: SKIPPED — Phase 8 is purely Python code/config changes. No external tools, services, or runtimes beyond the existing Python 3.12 environment are required.

---

## Validation Architecture

Note: `workflow.nyquist_validation` is not set to `false` in `.planning/config.json` (key is absent). Validation section included.

### Test Framework
| Property | Value |
|----------|-------|
| Framework | pytest (assumed from Phase 9 plans; no pytest.ini found yet) |
| Config file | None — see Wave 0 |
| Quick run command | `cd /Users/work/conductor/workspaces/vox-v2/seattle && python -m pytest tests/ -x -q 2>/dev/null \|\| echo "no tests yet"` |
| Full suite command | `python -m pytest tests/ -v` |

### Phase Requirements → Test Map
| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| IPC-01 | No hardcoded /tmp strings in non-constants modules | smoke (grep check) | `grep -r '"/tmp/' heyvox/ --include='*.py' \| grep -v constants.py \| grep -v '# '` | N/A — grep |
| IPC-02 | Atomic write survives concurrent reads | unit | `pytest tests/test_ipc_state.py -x` | ❌ Wave 0 |
| IPC-02 | State file resets transient fields on restart | unit | `pytest tests/test_ipc_state.py::test_startup_reset -x` | ❌ Wave 0 |
| IPC-03 | GC removes files older than threshold | unit | `pytest tests/test_ipc_gc.py -x` | ❌ Wave 0 |
| IPC-03 | GC does not remove recent files | unit | `pytest tests/test_ipc_gc.py::test_gc_skips_recent -x` | ❌ Wave 0 |

### Wave 0 Gaps
- [ ] `tests/test_ipc_state.py` — covers IPC-02 (atomic write, concurrent read, startup reset)
- [ ] `tests/test_ipc_gc.py` — covers IPC-03 (age-based deletion, boundary cases)
- [ ] `tests/__init__.py` — package marker
- [ ] Framework install: `pip install pytest` — if not already installed

---

## Sources

### Primary (HIGH confidence)
- Direct codebase inspection — `/Users/work/conductor/workspaces/vox-v2/seattle/heyvox/constants.py` — full constant inventory
- Direct codebase inspection — grep `/tmp/` across all `.py` files — complete hardcoded path inventory
- `heyvox/hud/overlay.py` lines 492-496 — existing atomic write pattern reference
- `heyvox/herald/orchestrator.py` lines 729-736 — existing claim file GC pattern

### Secondary (MEDIUM confidence)
- Python 3.12 stdlib docs (os.rename, json, tempfile) — all referenced patterns are stdlib

### Tertiary (LOW confidence)
- None

---

## Metadata

**Confidence breakdown:**
- Constants inventory (IPC-01): HIGH — direct grep of all Python source files
- Atomic state file design (IPC-02): HIGH — pattern exists in codebase, stdlib only
- Queue GC design (IPC-03): HIGH — extends existing claim GC pattern in orchestrator
- Daemon script exception (watcher.py, kokoro-daemon.py): HIGH — verified they are subprocess-launched standalone scripts

**Research date:** 2026-04-11
**Valid until:** 2026-05-11 (stable Python stdlib patterns; codebase unlikely to change significantly)
