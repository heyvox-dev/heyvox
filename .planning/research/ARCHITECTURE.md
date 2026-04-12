# Architecture Research: HeyVox v1.2 — Polish & Reliability

**Domain:** macOS voice layer — process coordination, IPC, clipboard injection, test isolation
**Researched:** 2026-04-12
**Confidence:** HIGH (source code verified, test failures reproduced)

## Current System Overview

```
┌────────────────────────────────────────────────────────────┐
│  CLI  (heyvox start|stop|status|setup|speak)               │
│  launchd plist → com.heyvox.listener                        │
└─────────────────────┬──────────────────────────────────────┘
                      │ fork / exec
┌─────────────────────▼──────────────────────────────────────┐
│  Main Process  (heyvox/main.py + modules)                   │
│                                                             │
│  ┌────────────────┐  ┌──────────────┐  ┌────────────────┐  │
│  │ DeviceManager  │  │ RecordingSM  │  │ WakeWordProc.  │  │
│  │ (audio/mic)    │  │ (recording)  │  │ (audio/wakewd) │  │
│  └───────┬────────┘  └──────┬───────┘  └───────┬────────┘  │
│          │                  │                   │           │
│  ┌───────▼──────────────────▼───────────────────▼────────┐  │
│  │              AppContext (shared mutable state)         │  │
│  │  is_recording, busy, audio_buffer, adapter, hud_client│  │
│  └─────────────────────────┬─────────────────────────────┘  │
│                            │ Unix socket                    │
│  ┌─────────────────────────▼─────────────────────────────┐  │
│  │     MCP Server  (heyvox/mcp/server.py)  — stdio        │  │
│  │     voice_speak | voice_status | voice_queue | config  │  │
│  └─────────────────────────┬─────────────────────────────┘  │
│                            │ write flag files / queue WAVs  │
└────────────────────────────┼───────────────────────────────┘
                             │
          ┌──────────────────┼──────────────────────┐
          │                  │                       │
┌─────────▼─────┐  ┌─────────▼──────┐  ┌────────────▼──────┐
│  HUD Process  │  │Herald Orch.    │  │ Kokoro Daemon     │
│  (AppKit)     │  │(herald/        │  │ /tmp/kokoro-      │
│  /tmp/heyvox- │  │ orchestrator.  │  │ daemon.sock       │
│  hud.sock     │  │ py)            │  │ Metal GPU TTS     │
└───────────────┘  └────────────────┘  └───────────────────┘
```

## Component Boundaries (v1.1 Post-Decomposition)

### Core Event Loop — main.py

The 2000-line monolith was decomposed in v1.1 into four focused modules:

| Module | Responsibility | v1.2 Impact |
|--------|----------------|-------------|
| `main.py` (896 lines) | Orchestration, event loop, shim vars | **Remove 7 shim vars** (Phase A) |
| `device_manager.py` | Mic init, hotplug, zombie detection | None |
| `recording.py` | Start/stop/send_local state machine | tts_playing dual-write completion |
| `app_context.py` | Shared typed state (17+ globals replaced) | Add state file write path |
| `wakeword.py` (WakeWordProcessor) | Wake word detection + preroll | None |

### Text Injection — input/injection.py

Current flow:
```
type_text(text, app_name)
  → _chrome_type_text()           ← try Hush socket first
      FAIL → _osascript_type_text()
                pbcopy (stdin pipe)
                → verify via get_clipboard_text()  ← osascript call
                → time.sleep(0.05)
                → osascript: tell process, set frontmost, delay 0.3, keystroke "v"
```

**Known failures (reproduced):**
- `test_basic_paste` and `test_no_clipboard_restore` assert `call_count == 2` but get 4.
  Root cause: tests mock `subprocess.run` globally, but `_get_frontmost_app()` (called for logging) and `_save_frontmost_pid()` (which uses AppKit, not subprocess) both trigger. `_get_frontmost_app()` makes a `subprocess.run` call that the test doesn't account for. Tests were written for an older injection flow without diagnostic logging.
  Fix: Either mock `_get_frontmost_app` separately or restructure tests to not count calls.

**Injection timing breakdown:**
- `time.sleep(0.05)` — before osascript (hardcoded)
- `delay 0.3` inside osascript tell-process block — focus settle time
- Total sequential time: ~400ms before keystroke lands

**Reliability gap:** No verification that Cmd+V actually pasted (only that osascript succeeded). If the target app didn't have focus at keystroke moment, paste is silently lost.

### IPC Architecture

Flag files at `/tmp/` are the primary cross-process IPC mechanism. v1.1 consolidated 25+ flags into `heyvox-state.json` with dual-write (old flags still primary, state file written in parallel).

Current state of dual-write:
- `tts_playing` field in state file: incomplete — old `TTS_PLAYING_FLAG` is still primary
- Recording flag: consolidated, state file updated alongside
- Practical impact: HUD and echo suppression read the old flag files; state file is supplementary

```
/tmp/ IPC surface:
  heyvox-state.json      ← atomic state (dual-write target, incomplete)
  heyvox-recording       ← primary (recording active)
  heyvox-tts-playing     ← primary (TTS speaking, mute mic)
  heyvox-tts-cmd         ← CLI → TTS control (skip/mute/quiet)
  heyvox-hud.sock        ← main → HUD (JSON messages)
  heyvox-verbosity       ← verbosity level
  heyvox-active-mic      ← current mic name for HUD
  heyvox-mic-switch      ← HUD → main (switch request)
  heyvox.pid             ← PID file
  heyvox-heartbeat       ← watchdog heartbeat
  herald-queue/          ← WAV files waiting to play
  herald-hold/           ← WAV files held (wrong workspace)
  kokoro-daemon.sock     ← main TTS engine socket
  hush.sock              ← Chrome media control socket
```

### Herald TTS Architecture

Pure Python pipeline (no shell subprocesses):
```
voice_speak(text)                          ← MCP tool
  → heyvox/herald/worker.py               ← generate WAV via Kokoro daemon
      → /tmp/kokoro-daemon.sock            ← Metal GPU TTS
      → /tmp/herald-queue/uuid.wav         ← queue drop
  → heyvox/herald/orchestrator.py         ← file watcher picks up
      → media pause (Hush → MediaRemote → media key)
      → sounddevice play + sd.wait()       ← interruptible
      → media resume
      → /tmp/herald-history/ (archive)
```

### Test Isolation Architecture

Tests use `conftest.py:isolate_flags` (autouse=True) which patches flag paths to `tmp_path`. The patch applies to `heyvox.constants.*` and known module-level re-imports in `tts.py`, `main.py`, `recording.py`.

**Known stale test failures (4 confirmed, 2 unconfirmed):**

| Test | Failure Mode | Root Cause |
|------|-------------|------------|
| `test_injection.py::test_basic_paste` | `call_count == 4`, expected 2 | `_get_frontmost_app()` added without updating test count |
| `test_injection.py::test_no_clipboard_restore` | Same as above | Same root cause |
| `test_media.py::TestPauseMedia::test_noop_when_no_session` | `AttributeError: _browser_has_video_tab` | Attribute renamed or removed in media.py after tests were written |
| `test_media.py::TestPauseMedia::test_falls_back_gracefully_when_mr_unavailable` | Same `AttributeError` | Same root cause |

The `_browser_has_video_tab` attribute no longer exists on `heyvox.audio.media`. Tests patch an internal function that was refactored away.

### Distribution Architecture

Current packaging state:
- `pyproject.toml` with `setuptools` build backend
- Package name: `heyvox` (claimed in pyproject.toml; must verify PyPI availability)
- Entry points: `heyvox`, `herald`, `heyvox-chrome-bridge`
- Optional extras: `apple-silicon`, `tts`, `aec`, `chrome`, `dev`
- Portaudio system dep: required for PyAudio, not expressible in pyproject.toml
- `pipx install` works today; Homebrew formula does not exist

## Component Map for v1.2 Changes

### Area A: Tech Debt — Shim Vars in main.py

**Current structure** (lines 157-166 in main.py):
```python
# Module-level compat shims (test_flag_coordination.py reads these)
is_recording = False   # Synced from ctx in main loop
busy = False
recording_start_time = 0.0
_audio_buffer = []
_triggered_by_ptt = False
_recording_target = None
_state_lock = threading.Lock()
_recording: RecordingStateMachine | None = None
```

**Integration point:** `test_flag_coordination.py` (and possibly others) imports from `heyvox.main`. Before removing shims, confirm which tests import module-level names.

**Removal strategy:**
1. `grep -r "heyvox.main.is_recording\|from heyvox.main import" tests/` — find all consumers
2. Migrate consuming tests to use `AppContext` directly
3. Remove shim vars from main.py
4. Remove sync code in main loop that keeps shims in sync with ctx

**Risk:** Low. Shims are explicitly marked with removal instructions. AppContext has all the same state with proper thread safety.

### Area B: tts_playing Dual-Write Completion

**Current state:** `TTS_PLAYING_FLAG` is still primary for echo suppression. State file `heyvox-state.json` has `tts_playing` field but it's not consistently written.

**Integration points:**
- `heyvox/audio/echo.py` — reads `TTS_PLAYING_FLAG` directly
- `heyvox/audio/tts.py` — writes `TTS_PLAYING_FLAG`
- `heyvox/herald/orchestrator.py` — writes `TTS_PLAYING_FLAG` during Herald playback
- `heyvox/main.py` — reads flag for wake word suppression

**Migration path:** Complete state file writes in all three writers (tts.py, orchestrator.py, and any direct main.py writes), then update all readers to use state file. Flag file can remain as legacy fallback with max-age guard (already implemented).

**Risk:** Medium. Echo suppression failures = mic picks up TTS and re-transcribes it. Test in speaker mode before removing flag as primary.

### Area C: Paste Injection Reliability

**Problem:** Silent failure when target app loses focus between STT completion and Cmd+V delivery.

**Current safeguards:**
- Target app name captured at recording start (`recording_target` snapshot)
- `set frontmost to true` in osascript before keystroke
- 0.3s delay inside osascript for focus settle
- Clipboard verify before paste

**Missing safeguards:**
- No post-paste verification (did the text actually appear?)
- No retry on focus-steal (another app popped up during the 400ms window)
- `delay 0.3` is hardcoded; some apps need more (Electron apps, especially Claude Code)

**Architecture for reliability improvements:**
```
type_text(text, app_name)
  → capture frontmost PID BEFORE                         ← exists
  → set clipboard + verify                                ← exists
  → focus target + delay (configurable per-app)           ← partial
  → send Cmd+V                                            ← exists
  → verify focus didn't change                            ← exists (logging only)
  → [NEW] retry up to N times if focus changed            ← missing
  → [NEW] configurable per-app delay in config.yaml       ← missing
  → restore original frontmost (if different from target) ← exists
```

The retry logic should be in `_osascript_type_text()` not in the caller. Config key: `injection.focus_delay_ms` per app name, default 300ms.

### Area D: Test Stability

**Root cause of injection test failures:**
`_get_frontmost_app()` was added after tests were written. It makes a `subprocess.run` call purely for diagnostic logging. Tests that count `mock_run.call_count` break because they don't account for this call.

**Two fix strategies:**

Strategy 1 (minimal): Mock `_get_frontmost_app` in affected tests:
```python
@patch("heyvox.input.injection._get_frontmost_app", return_value="TestApp")
@patch("heyvox.input.injection.subprocess.run")
def test_basic_paste(self, mock_run, mock_frontmost):
    ...
    assert mock_run.call_count == 2  # pbcopy + osascript only
```

Strategy 2 (structural): Extract diagnostic logging from hot path into a separate function that tests can silence via autouse fixture in conftest.py. Better for future-proofing.

**Root cause of media test failures:**
`_browser_has_video_tab` was patched by tests as a module-level function but was renamed or inlined during media.py refactoring. Tests must be updated to patch the current API.

Fix: `grep "_browser_has_video_tab" heyvox/audio/media.py` to find current function name, then update test patches.

### Area E: Distribution Prep

**Package name resolution:**
- `heyvox` in pyproject.toml — verify availability on PyPI before publishing
- Homebrew: `vox` is taken; `heyvox` likely available
- Alternative candidates if `heyvox` is taken: `voxcode`, `hotmic`, `murmur`, `hark`

**Homebrew formula architecture:**
```ruby
class Heyvox < Formula
  desc "macOS voice layer for AI coding agents"
  homepage "https://heyvox.dev"
  url "..."         # tarball or git tag
  license "MIT"

  depends_on "portaudio"         # PyAudio C extension requirement
  depends_on "python@3.12"

  resource "heyvox" do            # pip install from PyPI
    url "..."
    sha256 "..."
  end

  def install
    virtualenv_install_with_resources
  end

  test do
    system bin/"heyvox", "--version"
  end
end
```

**Key constraint:** Homebrew does not support `mlx-whisper` (Apple Silicon only dep) in standard formula. Options:
- Separate `heyvox-mlx` cask or bottle for Apple Silicon
- Post-install message directing users to `pip install heyvox[apple-silicon]`
- Use `on_arm` block in formula (supported in Homebrew Ruby DSL)

**pipx compatibility:** Currently works. No changes needed for v1.2 unless package name changes.

## Data Flow for v1.2 Changes

### Paste Injection (improved)

```
STT complete → text + recording_target{app_name, pid}
  ↓
adapter.inject(text)
  ↓
injection.type_text(text, app_name=target.app_name)
  ↓
  ├─ [try] Chrome Hush socket: {"action": "type-text", "text": text}
  │         success → done (most reliable for browser apps)
  │
  └─ [fallback] osascript paste:
       1. pbcopy (stdin pipe, no escaping needed)
       2. clipboard verify (osascript read-back)
       3. focus target app (tell process ... set frontmost to true)
       4. delay N ms (per-app config, default 300ms)
       5. keystroke "v" using command down
       6. [NEW] verify focus unchanged
       7. [NEW] retry up to 2x if focus stolen
```

### State File (dual-write completion)

```
TTS speaking → tts.py / orchestrator.py:
  write /tmp/heyvox-tts-playing   ← existing (primary, keep for compat)
  write /tmp/heyvox-state.json    ← complete this dual-write

TTS done:
  remove /tmp/heyvox-tts-playing
  update /tmp/heyvox-state.json tts_playing: false
```

### Shim Var Removal

```
BEFORE (main.py module level):
  is_recording = False  ← module global
  busy = False
  ... 5 more shim vars

  # In main loop:
  is_recording = ctx.is_recording  ← sync shim to ctx

AFTER:
  # No module-level shims
  # Consumers access ctx directly or via RecordingStateMachine API
```

## Suggested Build Order for v1.2

Ordered by dependency and risk:

1. **Test fixes** — First. Failing tests block CI and mask regressions. No production risk. Fix `test_injection.py` (mock `_get_frontmost_app`) and `test_media.py` (update `_browser_has_video_tab` patches).

2. **Tech debt: shim var removal** — Second. Low risk, well-scoped. Required before adding new state to AppContext (otherwise two sources of truth remain). Depends on test fixes being green first.

3. **tts_playing dual-write completion** — Third. Medium risk. Completes the IPC consolidation from v1.1. Keep old flag as parallel write (don't remove yet). Validate echo suppression still works.

4. **Paste injection reliability** — Fourth. Highest user impact, medium implementation risk. Depends on clean test suite to verify. Add per-app delay config, retry logic, better failure logging.

5. **Distribution prep** — Last. PyPI name check, Homebrew formula skeleton, README install section. No production risk but requires package name decision first.

## Integration Points Summary

| v1.2 Change | Files Modified | Files Affected (read) | New Files |
|-------------|---------------|----------------------|-----------|
| Remove shim vars | `main.py` | `tests/test_flag_coordination.py` | None |
| Complete tts_playing dual-write | `tts.py`, `orchestrator.py` | `echo.py`, `main.py` | None |
| Injection reliability | `input/injection.py` | `adapters/generic.py`, `config.py` | None |
| Injection test fixes | `tests/test_injection.py` | `conftest.py` | None |
| Media test fixes | `tests/test_media.py` | `audio/media.py` | None |
| Homebrew formula | new `Formula/heyvox.rb` | `pyproject.toml` | `Formula/heyvox.rb` |

## Anti-Patterns to Avoid

### Anti-Pattern 1: Removing Flag Files Too Early

**What people do:** Complete the state file dual-write, then immediately remove the old flag files as a cleanup step.
**Why it's wrong:** External processes (Herald orchestrator, echo suppression) may still be reading old flag paths. The dual-write exists precisely to allow parallel operation during transition.
**Do this instead:** Complete dual-write (all writers write both). Leave old flags for v1.2. Remove in v1.3 after a full release cycle confirms state file readers are stable.

### Anti-Pattern 2: Counting subprocess.run Calls in Injection Tests

**What people do:** `assert mock_run.call_count == N` — breaks whenever diagnostic logging adds a subprocess call.
**Why it's wrong:** Diagnostic calls (`_get_frontmost_app`, frontmost-before/after logging) are implementation details that shouldn't affect test assertions.
**Do this instead:** Assert on what actually matters — which osascript script was sent, that pbcopy received the correct bytes, that Cmd+V was included. Mock diagnostic helpers separately.

### Anti-Pattern 3: osascript for Focus Verification

**What people do:** Call `osascript -e 'frontmost application name'` after each paste to verify success.
**Why it's wrong:** Each osascript invocation spawns a JXA interpreter (~80-100ms). Adding verification calls doubles injection latency.
**Do this instead:** Use PyObjC `NSWorkspace.frontmostApplication()` for focus checks — it's a direct API call, <1ms. Already used by `_save_frontmost_pid()`.

### Anti-Pattern 4: Global Subprocess Mock Without Side Effect Isolation

**What people do:** `@patch("subprocess.run")` (global) — captures every subprocess call including logging helpers.
**Why it's wrong:** Injection.py calls subprocess.run for 3 different purposes (pbcopy, osascript paste, frontmost-app logging). A global mock conflates them.
**Do this instead:** Patch at the module level (`heyvox.input.injection.subprocess.run`), and mock `_get_frontmost_app` separately when you need call-count precision.

## Sources

- Source: `/Users/work/conductor/workspaces/vox-v2/seattle/heyvox/input/injection.py` — current injection implementation
- Source: `/Users/work/conductor/workspaces/vox-v2/seattle/heyvox/main.py` lines 157-200 — shim vars
- Source: `/Users/work/conductor/workspaces/vox-v2/seattle/heyvox/constants.py` — IPC surface inventory
- Source: `/Users/work/conductor/workspaces/vox-v2/seattle/tests/conftest.py` — test isolation strategy
- Source: `/Users/work/conductor/workspaces/vox-v2/seattle/tests/test_injection.py` — failing tests (call count mismatch)
- Source: `/Users/work/conductor/workspaces/vox-v2/seattle/tests/test_media.py` — failing tests (`_browser_has_video_tab` gone)
- Source: `/Users/work/conductor/workspaces/vox-v2/seattle/pyproject.toml` — distribution config
- Reproduced: 4 test failures via `pytest tests/test_injection.py tests/test_media.py --tb=short`
- Confidence: HIGH — all findings from direct source inspection and test execution

---
*Architecture research for: HeyVox v1.2 Polish & Reliability*
*Researched: 2026-04-12*
