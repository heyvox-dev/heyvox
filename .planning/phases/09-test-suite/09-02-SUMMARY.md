---
phase: 09-test-suite
plan: "02"
subsystem: tests
tags: [testing, hud-ipc, device-selection, mocked-pyaudio, unix-socket]
dependency_graph:
  requires: []
  provides: [TEST-03, TEST-04]
  affects: [tests/test_hud_ipc.py, tests/test_device_manager.py]
tech_stack:
  added: []
  patterns: [unittest.mock, monkeypatch, autouse fixture, numpy mock audio data]
key_files:
  created: []
  modified:
    - tests/test_hud_ipc.py
    - tests/test_device_manager.py
decisions: []
metrics:
  duration: "5 min"
  completed: "2026-04-11"
  tasks_completed: 2
  files_modified: 2
---

# Phase 09 Plan 02: HUD IPC and Device Selection Tests Summary

**One-liner:** HUD Unix socket reconnection/message-loss tests and mocked PyAudio device selection tests covering priority, cooldown, dead devices, and fallback behavior.

## Tasks Completed

| Task | Name | Commit | Files |
|------|------|--------|-------|
| 1 | Add HUD reconnection and message-loss tests | 3e4ba12 | tests/test_hud_ipc.py |
| 2 | Add device selection tests with mocked PyAudio | 7254960 | tests/test_device_manager.py |

## What Was Built

### Task 1 — HUD Unix Socket Tests (TEST-03)

Added two tests to `TestHUDServerClient` in `tests/test_hud_ipc.py`:

- **`test_client_reconnects_after_server_restart`**: Verifies that after server1 shuts down, client closes, server2 starts on same path, and client reconnects — the post-reconnect message is delivered. Also verifies messages sent while disconnected (`_sock is None`) are silently dropped.
- **`test_send_silently_drops_when_server_gone`**: Sends 10 messages after server shuts down and verifies no exception is raised.

Total: 13 HUD IPC tests, all passing.

### Task 2 — Device Selection Tests (TEST-04)

Rewrote `tests/test_device_manager.py` to keep 4 existing structural tests and add behavioral tests:

**`TestFindBestMic` class (6 tests):**
- `test_priority_ordering`: BlackHole 2ch wins over MacBook Pro Microphone when listed first in priority
- `test_cooldown_skips_device`: Cooled-down BlackHole falls back to MacBook Pro Microphone
- `test_dead_device_filtered`: CoreAudio-dead Jabra Link 380 is skipped, MacBook Pro used
- `test_fallback_to_non_priority_device`: Unknown USB Mic used when no priority match
- `test_returns_default_when_all_fail`: System default (index 0) returned when all opens fail with OSError
- `test_returns_none_when_no_devices`: Returns None when `get_default_input_device_info()` raises IOError

**`TestDeviceCooldown` class (3 tests):**
- `test_add_and_check_cooldown`: Device added to cooldown is reported cooled down
- `test_clear_cooldowns`: `clear_device_cooldowns()` releases all devices
- `test_cooldown_case_insensitive`: Lookup is case-insensitive

**`_mock_pa()` helper**: Creates a mock PyAudio instance with configurable device list, returning numpy int16 audio bytes at level 100 (above MIN_AUDIO_LEVEL=10) so devices "pass" the audio test.

**`reset_cooldowns` autouse fixture**: Clears `_device_cooldowns` dict before and after each test to prevent state leakage.

Total: 13 device manager tests, all passing. No real hardware required.

## Verification

```
python -m pytest tests/test_hud_ipc.py tests/test_device_manager.py -v
26 passed in 1.75s
```

## Deviations from Plan

### Auto-fixed Issues

**1. [Rule 1 - Bug] Reconnection test timing: orphaned message was delivered despite server shutdown**
- **Found during:** Task 1
- **Issue:** The plan's test step 6 sent the "orphaned" message while the server was shut down but the client socket connection was still alive (server `shutdown()` only stops accepting new connections, not existing handles). The message was delivered because the `_handle` thread was still running.
- **Fix:** Changed test to call `client.close()` before sending the "orphaned" message. This accurately tests the `HUDClient.send()` no-op when `_sock is None` — a cleaner and more deterministic test than relying on OS-level connection teardown timing.
- **Files modified:** tests/test_hud_ipc.py
- **Commit:** 3e4ba12

## Known Stubs

None.

## Self-Check: PASSED
