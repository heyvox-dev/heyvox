---
phase: 03-cli-tts-output
plan: 02
subsystem: cli
tags: [launchd, launchctl, setup-wizard, permissions, pyaudio, huggingface, rich, mcp, tts-interrupt]

# Dependency graph
requires:
  - phase: 03-cli-tts-output/03-01
    provides: Kokoro TTS engine (start_worker, interrupt, shutdown, skip_current, stop_all, set_muted) and TTS constants

provides:
  - launchd service lifecycle (vox/setup/launchd.py: write_plist, bootstrap, bootout, get_status, restart)
  - macOS permission checking with deep-links (vox/setup/permissions.py: check_accessibility, check_microphone, check_screen_recording, open_permission_settings)
  - Interactive 8-step setup wizard (vox/setup/wizard.py: run_setup) including MCP auto-approve writing to ~/.claude/settings.json
  - Complete CLI: start (--daemon/-d), stop, restart, status (PID display), logs (--lines/-n, tail -f), setup
  - TTS interrupt wired into main loop start_recording() (TTS-03)
  - TTS worker starts automatically in main() when tts.enabled=True
  - TTS worker shuts down cleanly in main() finally block
  - Voice commands skip/stop/mute dispatch to native Kokoro TTS engine

affects:
  - 04-mcp-server (launchd service is running, TTS ready for voice_speak tool)

# Tech tracking
tech-stack:
  added: [rich (Console/Panel/Progress/Live - lazy import in wizard), huggingface_hub (snapshot_download - lazy import)]
  patterns:
    - Lazy import pattern for setup-only deps (rich, huggingface_hub) inside run_setup() function, not at module top
    - launchctl bootstrap/bootout/list for macOS LaunchAgent lifecycle (not launchctl load/unload which is deprecated)
    - sys.executable in plist ProgramArguments — always points to current venv Python, no hardcoded paths
    - Permission checks via PyObjC (AXIsProcessTrusted), pyaudio stream test, osascript heuristic

key-files:
  created:
    - vox/setup/__init__.py
    - vox/setup/launchd.py
    - vox/setup/permissions.py
    - vox/setup/wizard.py
  modified:
    - vox/cli.py
    - vox/main.py

key-decisions:
  - "bootout() guards against missing plist — returns 'Not running (not installed)' instead of running launchctl with a missing plist path"
  - "bootout() treats exit codes 3 and 5 both as 'Not running' — code 5 occurs when plist exists but service not loaded"
  - "sys.executable in plist ProgramArguments — ensures launchd always uses the same venv Python, avoiding activation issues"
  - "TTS interrupt uses try/except ImportError — allows main.py to run without sounddevice installed (non-TTS environments)"
  - "Voice command dispatch: skip/stop/mute call native TTS engine directly; tts-next/tts-replay fall through to execute_voice_command (not yet natively implemented)"
  - "MCP auto-approve writes to ~/.claude/settings.json mcpServers key — adds vox entry with sys.executable path for portability"

patterns-established:
  - "launchd plist generation: use sys.executable for ProgramArguments[0], run -m vox.main (not CLI) to avoid re-entrant launchctl"
  - "Setup wizard steps: welcome → permissions (with deep-link open + retry loop) → model download → mic test → config → launchd → MCP → summary"
  - "bootout safety: always check plist existence before calling launchctl bootout to avoid confusing error messages"

# Metrics
duration: 5min
completed: 2026-03-27
---

# Phase 3 Plan 02: CLI + TTS Output Summary

**launchd service lifecycle (bootstrap/bootout/status/logs), interactive 8-step setup wizard with macOS permission deep-links and Claude Code MCP auto-approve, and TTS interrupt wired into the main event loop**

## Performance

- **Duration:** 5 min
- **Started:** 2026-03-27T10:16:22Z
- **Completed:** 2026-03-27T10:20:41Z
- **Tasks:** 2
- **Files modified:** 6

## Accomplishments
- Complete launchd service management: write_plist() uses sys.executable (no hardcoded paths), bootstrap/bootout/get_status/restart all functional with correct exit code handling
- Interactive setup wizard with rich UI: permission checks with deep-links to System Settings (Accessibility, Microphone, Screen Recording), Kokoro model download, mic level test, config init, launchd install, MCP auto-approve
- All CLI commands complete: vox start (foreground or --daemon launchd), stop, restart, status (shows PID), logs (--lines/-n, tail -f with clean Ctrl+C), setup
- TTS wired into main loop: start_worker() at startup, interrupt() on wake word/PTT trigger, skip/stop/mute dispatch to native Kokoro engine, shutdown() in finally block

## Task Commits

1. **Task 1: launchd service management and setup wizard with MCP auto-approve** - `bb3ad6f` (feat)
2. **Task 2: Wire CLI service commands, logs, and TTS interrupt into main loop** - `19ead03` (feat)

**Plan metadata:** (this commit)

## Files Created/Modified
- `vox/setup/__init__.py` - Package init
- `vox/setup/launchd.py` - write_plist(), bootstrap(), bootout(), get_status(), restart() with correct launchctl bootstrap/bootout semantics
- `vox/setup/permissions.py` - check_accessibility() (PyObjC AXIsProcessTrusted), check_microphone() (pyaudio stream test), check_screen_recording() (osascript heuristic), PERMISSION_URLS deep-links, open_permission_settings()
- `vox/setup/wizard.py` - run_setup(): 8-step guided wizard using rich, lazy imports for all heavy deps, MCP settings.json writer
- `vox/cli.py` - _cmd_start (--daemon/-d flag), _cmd_stop/restart/status (launchd wired), _cmd_logs (--lines/-n, tail -f, Ctrl+C clean), _cmd_setup (wired to wizard); all stub implementations replaced
- `vox/main.py` - TTS start_worker() at startup, interrupt() in start_recording() (TTS-03), native dispatch for skip/stop/mute voice commands, _shutdown_tts() in finally block

## Decisions Made
- `bootout()` guards missing plist — returns "Not running (not installed)" rather than confusingly invoking `launchctl bootout` with a missing plist path
- Exit codes 3 and 5 both treated as "Not running" in bootout() — macOS returns code 5 when plist exists but service isn't loaded (vs. code 3 for truly missing service)
- `sys.executable` in plist ProgramArguments ensures launchd always invokes the exact venv Python, no activation magic needed
- TTS interrupt wrapped in `try/except ImportError` in main.py — allows running without sounddevice installed
- Voice commands skip/stop/mute call native TTS functions directly; `tts-next`/`tts-replay` fall through to `execute_voice_command()` (not yet natively implemented)

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed bootout() to handle missing plist gracefully**
- **Found during:** Task 2 verification (vox stop)
- **Issue:** `launchctl bootout gui/{uid} /path/to/missing.plist` returns exit code 5 with a confusing "Boot-out failed: 5: Input/output error" message when plist doesn't exist
- **Fix:** Added plist existence check before calling launchctl; also added exit code 5 to the "Not running" success cases
- **Files modified:** vox/setup/launchd.py
- **Verification:** `vox stop` returns "Not running (service not installed)" when no plist exists
- **Committed in:** `19ead03` (Task 2 commit)

---

**Total deviations:** 1 auto-fixed (Rule 1 - Bug)
**Impact on plan:** Essential fix for correct user experience. Stop/restart commands would have shown confusing error messages otherwise.

## Issues Encountered
None.

## User Setup Required
None — setup wizard handles all user-facing configuration interactively via `vox setup`.

## Next Phase Readiness
- launchd service lifecycle fully working: install/start/stop/restart/status/logs
- TTS engine fully integrated with main event loop (interrupt on wake word, clean shutdown)
- Setup wizard ready for onboarding new users
- Phase 3 complete — ready for Phase 4: MCP server (voice_speak tool, voice_listen, queue management)

## Self-Check: PASSED

All files verified present. All task commits verified in git log.

---
*Phase: 03-cli-tts-output*
*Completed: 2026-03-27*
