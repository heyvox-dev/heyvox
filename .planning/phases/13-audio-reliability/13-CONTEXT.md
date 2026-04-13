# Phase 13: Audio Reliability - Context

**Gathered:** 2026-04-13
**Status:** Ready for planning

<domain>
## Phase Boundary

Make the audio pipeline robust across different microphone types (built-in, Bluetooth SCO, USB dongle) by introducing per-device profiles with auto-calibration, fixing TTS/recording interaction priorities (wake word during TTS, Escape stops TTS), and hardening silence detection against Bluetooth noise spikes. The loop refactor (AudioStage pipeline) is explicitly deferred to a follow-up phase.

</domain>

<decisions>
## Implementation Decisions

### Device Profile Storage
- **D-01:** Config file + auto-calibrate. Auto-calibrate noise floor on first mic connect, user can override in `config.yaml` under `mic_profiles:` (e.g., `mic_profiles: G435: silence_threshold: 300`).
- **D-02:** Full profile fields: `noise_floor`, `silence_threshold`, `buffer_size`, `cooldown_tier`, `sample_rate`, `chunk_size`, `gain`, `voice_isolation_mode`. Future-proof even if not all fields are used immediately.
- **D-03:** Calibration data persists to cache file (`~/.cache/heyvox/mic-profiles.json`). Auto-expires after N days. Config.yaml overrides always take priority over cached values.
- **D-04:** Calibration happens both automatically (on first audio chunks after mic connect, ~2-3 seconds of ambient noise measurement) and manually via `heyvox calibrate` CLI command.

### TTS Interruption Behavior
- **D-05:** Wake word during TTS: kill afplay immediately, start recording instantly. No waiting for sentence to finish.
- **D-06:** Remaining queued TTS: drop the current (interrupted) message's remaining parts. Other unrelated queued messages survive.
- **D-07:** Escape and wake-word-interrupt behave identically: both kill TTS + drop current message's queue. Simple mental model.

### Echo Suppression vs Headset Bypass
- **D-08:** Per-device profile flag `echo_safe: true/false`. Auto-set based on device type (headset = true, built-in mic = false). User can override in config.yaml.
- **D-09:** When `echo_safe` is true, wake word detection stays active during TTS playback. When false, wake word is suppressed during TTS (existing behavior).
- **D-10:** Post-TTS grace period: 0.5s for headset/echo_safe devices, 2s for speaker mode. Applies to all modes.
- **D-11:** Config flag `echo_suppression.force_disabled` allows user to force echo suppression off even in speaker mode.

### Silence Detection Robustness
- **D-12:** Silence detection uses percentage-based threshold (85% of chunks below threshold = silence) instead of single-spike max. Already partially implemented this session — needs proper testing and integration with device profiles.
- **D-13:** Silence threshold reads from device profile (not global config) so each mic type has its own appropriate threshold.

### Loop Refactor (DEFERRED)
- **D-14:** AudioStage pipeline refactor (AUDIO-06) is explicitly deferred to a separate follow-up phase. Phase 13 focuses on functional fixes (device profiles, TTS interaction). The structural refactor comes after, with lower risk per change.
- **D-15:** When the loop refactor happens, it should use an AudioStage protocol with named stages (read_audio, check_silence, detect_wake, etc.) as a chain. Unit tests for each extracted function with synthetic audio.

### Claude's Discretion
- Device profile auto-detection heuristics (how to classify built-in vs Bluetooth vs USB)
- Cache expiry duration (N days)
- Calibration algorithm (how many chunks, percentile calculation for noise floor)
- How `heyvox calibrate` CLI UX works (interactive? background? duration?)
- Integration of device profile into existing DeviceManager class vs new module

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Audio Pipeline
- `heyvox/main.py` — Main event loop, silence detection, echo suppression, wake word prediction (~lines 580-850)
- `heyvox/audio/mic.py` — Device selection, cooldown tiers, `find_best_mic()`, `add_device_cooldown()`, `clear_device_cooldown()`
- `heyvox/device_manager.py` — DeviceManager class, health checks, `handle_io_error()`, headset detection

### TTS Interaction
- `heyvox/herald/orchestrator.py` — TTS playback loop, `_is_paused()`, `_play_wav()`, recording flag check, afplay watchdog
- `heyvox/input/ptt.py` — Escape key handler, `is_speaking` callback, `on_cancel_tts` callback
- `heyvox/audio/tts.py` — `stop_all()` function called by Escape handler
- `heyvox/audio/media.py` — `pause_media()` with no-media cache (added this session)

### Echo Suppression
- `heyvox/main.py` (~lines 801-844) — Echo suppression block that skips wake word during TTS
- `heyvox/audio/echo.py` — WebRTC AEC integration

### Config
- `~/.config/heyvox/config.yaml` — User config (silence_threshold, echo_suppression, mic_priority)
- `heyvox/config.py` — Pydantic config model

### Codebase Context
- `.planning/codebase/ARCHITECTURE.md` — Full architecture overview, data flow diagrams
- `.planning/phases/06-decomposition/06-CONTEXT.md` — AppContext pattern, constructor injection (D-07, D-08)

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `DeviceManager` class already tracks `dev_name`, `headset_mode`, `stream` — natural home for device profile reference
- `find_best_mic()` in mic.py already iterates devices with priority — can inject profile lookup
- `_get_adaptive_cooldown()` already has per-device failure tracking — extend to per-device profiles
- `clear_device_cooldown()` added this session — ties into "confirmed working" signal for calibration

### Established Patterns
- Config via Pydantic dataclass (`HeyvoxConfig`) with YAML backing
- IPC via flag files (`/tmp/heyvox-recording`, `/tmp/heyvox-tts-playing`)
- Constructor injection: DeviceManager, RecordingStateMachine take `AppContext` (Phase 6)
- Per-device state tracking already in `_device_cooldowns`, `_device_failure_counts` dicts in mic.py

### Integration Points
- `_run_loop()` in main.py reads `silence_threshold` from global config — needs to read from active device profile instead
- Echo suppression block (main.py:801-824) checks `_tts_active` then `continue` — needs headset/echo_safe gate
- `health_check()` in DeviceManager calls `clear_device_cooldown()` — natural place to trigger/update calibration
- Escape handler chain in ptt.py (busy > recording > speaking) — TTS kill already wired, may need priority adjustment

</code_context>

<specifics>
## Specific Ideas

- G435 Bluetooth headset has 1024-frame hardware buffer periods (not 1280) — device profile must handle this
- G435 noise floor: median 25, but spikes to 455 in ~8% of chunks — percentage-based silence detection handles this
- Bluetooth SCO at 16kHz delivers in fixed hardware periods — `get_read_available()` guard must check `< 1` not `< chunk_size`
- The `pause_media()` no-media cache (15s TTL) was added this session to fix 5s TTS delay — device profiles should not interfere with this

</specifics>

<deferred>
## Deferred Ideas

- **AudioStage pipeline refactor** — Loop restructuring into named stage chain with protocol. Separate phase after device profiles are stable. Will include unit tests for each stage with synthetic audio. (AUDIO-06 moved here)
- **Per-mic auto-gain** — Normalize input levels across mics. Not needed for Phase 13 (silence threshold handles the core issue).
- **Bluetooth A2DP → SCO profile switching** — Detect when macOS is in wrong audio profile and prompt user. Complex macOS internals, defer.

</deferred>

---

*Phase: 13-audio-reliability*
*Context gathered: 2026-04-13*
