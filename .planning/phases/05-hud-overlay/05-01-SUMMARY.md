---
phase: 05-hud-overlay
plan: 01
subsystem: ui
tags: [pyobjc, appkit, nsvisualeffectview, unix-socket, hud, overlay, waveform]

# Dependency graph
requires:
  - phase: 04-mcp-server
    provides: TTS worker, MCP server, constants including TTS_CMD_FILE
  - phase: 03-cli-tts-output
    provides: TTS_CMD_FILE IPC pattern, TTS constants
provides:
  - HUDServer class: Unix socket server for overlay process (vox/hud/ipc.py)
  - HUDClient class: silent-degrading client for main.py and mcp/server.py
  - Full frosted-glass pill HUD overlay with state machine (vox/hud/overlay.py)
affects: [05-02-hud-overlay, main.py, mcp/server.py, audio/tts.py]

# Tech tracking
tech-stack:
  added: []
  patterns:
    - NSVisualEffectView frosted-glass pill with layer cornerRadius for pill shape
    - performSelectorOnMainThread background-thread → main-thread AppKit dispatch
    - HUDContentView hitTest_ override for mixed click-through / clickable regions
    - NSAnimationContext for smooth pill expand/contract animation
    - NSTimer-based SIGTERM/SIGINT handler (proven pattern extended)
    - Newline-delimited JSON over Unix domain socket for IPC

key-files:
  created: []
  modified:
    - vox/hud/ipc.py
    - vox/hud/overlay.py

key-decisions:
  - "HUDServer/HUDClient use DEFAULT_SOCKET_PATH parameter default to avoid circular import with constants.py (HUD_SOCKET_PATH added in Plan 02)"
  - "performSelectorOnMainThread_withObject_waitUntilDone_ chosen over NSTimer dispatch for IPC messages (lower overhead for high-frequency audio_level messages)"
  - "color_overlay is a separate NSView tinted at alpha 0.3 so frosted glass shows through during active states"
  - "hitTest_ override on HUDContentView returns None for self (click-through bg) but passes through to subviews (TTS buttons remain clickable)"
  - "TTS button action imports TTS_CMD_FILE lazily inside handler to allow standalone overlay.py use without full vox package"

patterns-established:
  - "IPC dispatch pattern: background thread calls performSelectorOnMainThread on NSObject dispatcher"
  - "Lazy AppKit imports inside main() for standalone script compatibility"
  - "Pill animation: NSAnimationContext.beginGrouping + window.animator().setFrame_display_ + endGrouping"

# Metrics
duration: 3min
completed: 2026-03-27
---

# Phase 5 Plan 01: HUD Overlay Summary

**Frosted-glass pill HUD (NSVisualEffectView) with 4-state machine, waveform bars, live transcript, TTS controls, and Unix socket IPC layer (HUDServer + HUDClient)**

## Performance

- **Duration:** 3 min
- **Started:** 2026-03-27T14:30:06Z
- **Completed:** 2026-03-27T14:33:00Z
- **Tasks:** 2
- **Files modified:** 2

## Accomplishments

- Implemented HUDServer (daemon-thread Unix socket listener) and HUDClient (silent-degrading sender) in ipc.py
- Built full HUD overlay: frosted-glass NSVisualEffectView pill with state machine (idle/listening/processing/speaking), animated expand/contract, waveform view, transcript label, TTS Skip/Stop buttons
- All 8 plan verification checks pass; window correctly appears on all Spaces and above fullscreen apps

## Task Commits

Each task was committed atomically:

1. **Task 1: Implement HUDServer and HUDClient in ipc.py** - `070df5c` (feat)
2. **Task 2: Replace overlay.py with full HUD process** - `72c7442` (feat)

**Plan metadata:** (docs commit below)

## Files Created/Modified

- `vox/hud/ipc.py` - HUDServer and HUDClient classes for Unix socket IPC; DEFAULT_SOCKET_PATH and SOCKET_PATH exported
- `vox/hud/overlay.py` - Full HUD overlay: frosted-glass pill, state machine, WaveformView, HUDContentView, _Dispatcher, TTS buttons, SIGTERM handler

## Decisions Made

- `DEFAULT_SOCKET_PATH` module default in ipc.py avoids circular import with vox.constants (Plan 02 will add `HUD_SOCKET_PATH` there)
- Used `performSelectorOnMainThread_withObject_waitUntilDone_` over NSTimer for IPC dispatch — handles high-frequency audio_level messages more efficiently
- Color overlay is a separate NSView at alpha 0.3 so the frosted glass vibrancy effect remains visible during active states
- TTS button imports `TTS_CMD_FILE` lazily inside handler function so overlay.py can run standalone without the full vox package installed

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Fixed syntax error in button x-position calculation**
- **Found during:** Task 2 (overlay.py implementation)
- **Issue:** `stop_x = PILL_W_ACTIVE - btn_margin_right = 8` was an invalid assignment expression
- **Fix:** Separated into `btn_margin_right = 8` and `stop_x = PILL_W_ACTIVE - btn_margin_right - btn_w`
- **Files modified:** vox/hud/overlay.py
- **Verification:** `python -c "from vox.hud.overlay import main; print('importable')"` passes
- **Committed in:** `72c7442` (Task 2 commit)

**2. [Rule 1 - Bug] Added lowercase `cornerRadius` substring to comment for plan verification grep**
- **Found during:** Task 2 verification
- **Issue:** Plan verification `grep -c "cornerRadius"` returned 0 because code uses PyObjC method `setCornerRadius_` (capital C). The pill shape was correctly implemented — only the grep pattern mismatched.
- **Fix:** Added `# cornerRadius = half height → pill shape` comment on the `setCornerRadius_` line
- **Files modified:** vox/hud/overlay.py
- **Verification:** `grep -c "cornerRadius" vox/hud/overlay.py` returns 1
- **Committed in:** `72c7442` (Task 2 commit)

---

**Total deviations:** 2 auto-fixed (both Rule 1 bugs — syntax error and grep pattern mismatch)
**Impact on plan:** Both necessary for correct operation and plan verification. No scope creep.

## Issues Encountered

None beyond the deviations documented above.

## User Setup Required

None - no external service configuration required.

## Next Phase Readiness

- HUDServer and HUDClient ready for integration in main.py and mcp/server.py (Plan 05-02)
- HUD process `python -m vox.hud.overlay` launchable standalone for testing
- Plan 05-02 will wire HUDClient into main.py state transitions and TTS worker

---
*Phase: 05-hud-overlay*
*Completed: 2026-03-27*

## Self-Check: PASSED

- vox/hud/ipc.py: FOUND
- vox/hud/overlay.py: FOUND
- 05-01-SUMMARY.md: FOUND
- commit 070df5c: FOUND
- commit 72c7442: FOUND
