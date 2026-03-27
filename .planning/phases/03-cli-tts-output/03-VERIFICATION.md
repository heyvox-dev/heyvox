---
phase: 03-cli-tts-output
verified: 2026-03-27T10:25:00Z
status: passed
score: 16/16 must-haves verified
re_verification: false
---

# Phase 3: CLI + TTS Output Verification Report

**Phase Goal:** User can control Vox via CLI commands and AI agents can produce spoken output
**Verified:** 2026-03-27T10:25:00Z
**Status:** PASSED
**Re-verification:** No — initial verification

---

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | `vox speak 'hello world'` produces audible Kokoro TTS output | VERIFIED | `_cmd_speak` loads config, calls `start_worker(config)`, enqueues text via `speak()`, waits `_tts_queue.join()`, shuts down. KPipeline lazy-imported in `_tts_worker`, `sd.play(audio, samplerate=24000)` + `sd.wait()` pattern used |
| 2 | `vox skip` stops current TTS playback immediately | VERIFIED | `_cmd_skip` writes `"skip\n"` to `TTS_CMD_FILE`; `_check_cmd_file()` sets `_stop_event` and calls `sd.stop()` via `interrupt()` |
| 3 | `vox mute` silences all TTS; again unmutes | VERIFIED | `_cmd_mute` writes `"mute-toggle\n"` to `TTS_CMD_FILE`; worker reads, toggles `_muted`, stops all on mute |
| 4 | `vox quiet` sets verbosity to short for the session | VERIFIED | `_cmd_quiet` writes `"quiet\n"` to `TTS_CMD_FILE`; worker sets `_verbosity = Verbosity.SHORT` |
| 5 | TTS volume follows macOS system volume with configurable boost | VERIFIED | `_get_system_volume()` reads via osascript; `boosted = min(100, original_volume + volume_boost)` set before playback; restored in `finally` |
| 6 | TTS queue drops oldest message when MAX_HELD=5 exceeded | VERIFIED | `speak()` drains oldest via `_tts_queue.get_nowait()` + `task_done()` while `_tts_queue.qsize() >= TTS_MAX_HELD` |
| 7 | Echo suppression flag `/tmp/vox-tts-playing` written during playback, cleaned on stop/crash | VERIFIED | `_set_tts_flag(True)` inside `try:` block, `_set_tts_flag(False)` in `finally:` block — guaranteed cleanup |
| 8 | TTS pauses immediately when wake word or PTT detected | VERIFIED | `start_recording()` calls `_tts_int()` (imported as `from vox.audio.tts import interrupt`) before playing cue, wrapped in `try/except ImportError` |
| 9 | Verbosity modes full/summary/short/skip filter text before enqueuing | VERIFIED | `apply_verbosity()` tested: SKIP returns None, SHORT returns first sentence (max 100 chars), SUMMARY truncates at 150 chars word boundary with "..." |
| 10 | Audio ducking reduces system volume during TTS playback | VERIFIED | Worker reads `original_volume`, applies `ducked = int(original_volume * ducking_percent / 100)` if `ducking_percent > 0 and < 100`; restores in `finally` |
| 11 | `vox stop` stops the running launchd service | VERIFIED | `_cmd_stop` calls `bootout()` via `launchctl bootout gui/{uid} {plist_path}`; handles exit codes 3/5 as "Not running"; handles missing plist |
| 12 | `vox start --daemon` loads and starts via launchctl bootstrap | VERIFIED | `_cmd_start` with `--daemon` calls `bootstrap()` which runs `launchctl bootstrap gui/{uid} {plist_path}` |
| 13 | `vox status` shows running/stopped state with PID | VERIFIED | `_cmd_status` calls `get_status()` → parses `launchctl list com.vox.listener` output (PID\tExitCode\tLabel format) |
| 14 | `vox logs` tails the service log file | VERIFIED | `_cmd_logs` runs `subprocess.run(["tail", f"-n{lines}", "-f", log_path])`; checks file exists; exits cleanly on Ctrl+C |
| 15 | `vox setup` walks through permission checks, model download, mic test, MCP auto-approve | VERIFIED | `run_setup()` implements 8 steps: welcome, 3 permission checks with deep-links + retry loop, Kokoro download (huggingface_hub), mic live-level test, config init, launchd install, settings.json write, summary |
| 16 | TTS worker shuts down cleanly in main.py finally block | VERIFIED | `_shutdown_tts()` called at line 620 in `finally:` block; sends `None` sentinel, joins worker thread with 10s timeout |

**Score:** 16/16 truths verified

---

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `vox/audio/tts.py` | Kokoro TTS engine with queue, interrupt, verbosity, volume, ducking | VERIFIED | 534 lines; all 12 public API functions exported; Verbosity enum; `_tts_worker`; KPipeline lazy-init; backward-compat functions preserved |
| `vox/config.py` | TTSConfig with voice/speed/verbosity/volume_boost/ducking_percent | VERIFIED | `class TTSConfig` at line 59; all 5 new fields present with pydantic validators; `enabled=True` default |
| `vox/constants.py` | TTS constants | VERIFIED | `TTS_MAX_HELD=5`, `TTS_SAMPLE_RATE=24000`, `TTS_DEFAULT_VOICE`, `TTS_DEFAULT_SPEED`, `TTS_DEFAULT_VOLUME_BOOST=10`, `TTS_DEFAULT_DUCKING_PERCENT=60`, `TTS_CMD_FILE="/tmp/vox-tts-cmd"` |
| `vox/cli.py` | speak/skip/mute/quiet + start/stop/restart/status/logs/setup subcommands | VERIFIED | `_cmd_speak`, `_cmd_skip`, `_cmd_mute`, `_cmd_quiet`, `_cmd_start` (--daemon), `_cmd_stop`, `_cmd_restart`, `_cmd_status`, `_cmd_logs` (--lines/-n), `_cmd_setup` all implemented |
| `vox/setup/wizard.py` | 8-step setup wizard with permissions, model download, mic test, MCP auto-approve | VERIFIED | `run_setup()` implements all 8 steps; lazy rich/huggingface_hub imports; settings.json write at step 7 |
| `vox/setup/permissions.py` | macOS permission checking and deep-link opening | VERIFIED | `check_accessibility()` (PyObjC AXIsProcessTrusted), `check_microphone()` (pyaudio stream), `check_screen_recording()` (osascript), `PERMISSION_URLS` dict, `open_permission_settings()` |
| `vox/setup/launchd.py` | launchd plist generation and service management | VERIFIED | `write_plist()` (sys.executable, RunAtLoad, KeepAlive), `bootstrap()`, `bootout()` (handles codes 3/5 and missing plist), `get_status()`, `restart()` |
| `vox/main.py` | TTS worker startup, interrupt wiring, and clean shutdown | VERIFIED | `_start_tts(config)` at line 363; `_tts_int()` in `start_recording()` at line 157-159; `_shutdown_tts()` in finally at line 620 |

---

### Key Link Verification

#### Plan 03-01 Key Links

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `vox/audio/tts.py` | `/tmp/vox-tts-playing` | `_set_tts_flag` in try/finally | WIRED | Lines 259/292: `_set_tts_flag(True)` in try, `_set_tts_flag(False)` in finally — guaranteed cleanup |
| `vox/audio/tts.py` | sounddevice | `sd.play(audio, 24000)` + `sd.wait()` + `sd.stop()` | WIRED | Lines 276-285: `sd.play(audio, samplerate=TTS_SAMPLE_RATE)`, `sd.wait()`, `sd.stop()` for interrupt |
| `vox/audio/tts.py` | `kokoro.KPipeline` | lazy-initialized pipeline | WIRED | Lines 158-159: `from kokoro import KPipeline` inside `_get_pipeline()` lock, singleton pattern |
| `vox/cli.py` | `vox/audio/tts.py` | speak/skip/mute/quiet calling tts functions | WIRED | `_cmd_speak` imports `speak, start_worker, shutdown, _tts_queue`; skip/mute/quiet write to `TTS_CMD_FILE` |
| `vox/main.py` | `vox/audio/tts.py` | `start_recording` calls `tts.interrupt()` | WIRED | Lines 157-160: `from vox.audio.tts import interrupt as _tts_int; _tts_int()` in `try/except ImportError` |

#### Plan 03-02 Key Links

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `vox/cli.py` | `vox/setup/launchd.py` | start/stop/restart/status calling launchd functions | WIRED | `from vox.setup.launchd import bootstrap/bootout/restart/get_status` in each handler |
| `vox/cli.py` | `vox/setup/wizard.py` | setup command calling `run_setup()` | WIRED | `_cmd_setup` imports `from vox.setup.wizard import run_setup` |
| `vox/setup/launchd.py` | launchctl | subprocess calls to bootstrap/bootout/list | WIRED | Lines 84/113/142: `["launchctl", "bootstrap"/"bootout"/"list", ...]` |
| `vox/main.py` | `vox/audio/tts.py` | `start_worker()` at startup, `interrupt()` in `start_recording()` | WIRED | Line 361: import; line 363: `_start_tts(config)`; line 157: `_tts_int()` |
| `vox/main.py` | `vox/audio/tts.py` | `shutdown()` in finally block | WIRED | Line 361: `from vox.audio.tts import ... shutdown as _shutdown_tts`; line 620: `_shutdown_tts()` |
| `vox/setup/wizard.py` | `~/.claude/settings.json` | Step 7 writes MCP server allowlist entry | WIRED | Lines 262-291: reads/creates settings.json, adds `mcpServers.vox` entry with `sys.executable` |

---

### Requirements Coverage

| Requirement | Status | Evidence |
|-------------|--------|----------|
| `vox start/stop/restart/status/logs` manages launchd service | SATISFIED | All 5 commands implemented; `bootout`/`bootstrap` use correct `launchctl bootstrap/bootout` (not deprecated load/unload) |
| `vox setup` walks through permissions, model downloads, mic test | SATISFIED | 8-step wizard: 3 permissions with deep-links + 3-retry loop, `snapshot_download` with Progress spinner, 2s live mic level test |
| `vox speak "hello"` produces TTS audio; skip/mute/quiet control playback | SATISFIED | `_cmd_speak` wires full TTS cycle; skip/mute/quiet use command file IPC |
| TTS verbosity configurable and overridable per-message | SATISFIED | `TTSConfig.verbosity` validated against `{"full","summary","short","skip"}`; `speak(verbosity=...)` override |
| TTS pauses immediately when user starts speaking | SATISFIED | `start_recording()` calls `interrupt()` before playing listening cue |

---

### Anti-Patterns Found

None detected. Scanned all 8 modified files for: TODO/FIXME/HACK/PLACEHOLDER, return null/empty stub patterns, empty handlers. Clean.

---

### Human Verification Required

The following behaviors require running the application and cannot be verified programmatically:

#### 1. Kokoro TTS Audio Output

**Test:** Run `vox speak "Hello, this is Vox speaking"` in the terminal
**Expected:** Audible speech through speakers at system volume + 10 point boost
**Why human:** Requires audio hardware, Kokoro model downloaded, and sounddevice working on the machine

#### 2. Audio Ducking During Playback

**Test:** Play background music, then run `vox speak "This is a test of audio ducking"`
**Expected:** Background music volume reduces to 60% of original during TTS playback, then restores after
**Why human:** Requires multiple audio streams and subjective volume perception

#### 3. Cross-Process TTS Interrupt

**Test:** Terminal 1: `vox speak "This is a very long sentence that should take several seconds to play back through your speakers"`. Terminal 2 (immediately): `vox skip`
**Expected:** Playback stops promptly in Terminal 1
**Why human:** Requires timing coordination between two terminal sessions

#### 4. Wake Word → TTS Pause

**Test:** Start Vox (`vox start`), trigger `vox speak` from another terminal with long text, then say the wake word while TTS is playing
**Expected:** TTS stops immediately when wake word is detected
**Why human:** Requires running daemon, microphone, and wake word model

#### 5. Setup Wizard Visual Flow

**Test:** Run `vox setup` on a fresh system (or with permissions revoked)
**Expected:** Rich UI with colored checkmarks, permission deep-link opens correct System Settings pane, mic level bar animates
**Why human:** Visual terminal UI, permission state, System Settings interaction

---

### Gaps Summary

No gaps. All 16 observable truths are backed by substantive, wired implementations. All 8 artifacts exist and are connected. All 11 key links are verified. No stub patterns detected.

The implementation faithfully delivers the phase goal: CLI commands control the Vox service lifecycle and TTS output, and AI agents can produce spoken output through the Kokoro engine.

---

_Verified: 2026-03-27T10:25:00Z_
_Verifier: Claude (gsd-verifier)_
