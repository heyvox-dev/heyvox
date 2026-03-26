# Pitfalls Research: Vox — macOS Voice Layer

## Critical Pitfalls

### P1: macOS Permission Hell
**Severity:** CRITICAL
**Warning signs:** "Nothing happens when I speak," indicator doesn't appear, fn key not detected.
**Details:** Three separate permissions required: Microphone, Accessibility (event tap/keystroke injection), Screen Recording (some overlay behaviors). Each requires manual user grant. No API to request — only check status and deep-link.
**Prevention:** `vox setup` checks each permission with instructions. Deep-link to System Preferences panes. Verify with test actions. Show actionable runtime errors.
**Phase:** Setup/onboarding

### P2: Bluetooth A2DP → HFP Switching
**Severity:** HIGH
**Warning signs:** Terrible audio quality with AirPods/Bluetooth headphones.
**Details:** Bluetooth uses A2DP (high quality, no mic) vs HFP (low quality, has mic). Opening Bluetooth mic degrades all audio.
**Prevention:** Detect Bluetooth in setup and warn. Default mic priority to built-in. Don't auto-select Bluetooth mics.
**Phase:** Audio pipeline

### P3: Hardcoded Paths Crash Non-Conductor Users
**Severity:** HIGH (existing bug)
**Warning signs:** FileNotFoundError on `/Users/work/.claude/hooks/tts-ctl.sh`
**Details:** Five voice command lambdas reference hardcoded `tts-ctl.sh`. Crashes immediately for non-Conductor users.
**Prevention:** Make TTS path configurable. Gracefully disable when not configured. Test on fresh user account.
**Phase:** Decoupling (Phase 1)

### P4: MLX Whisper Requires Apple Silicon
**Severity:** MEDIUM
**Warning signs:** ImportError on Intel Macs.
**Details:** MLX only works on M1+. ~30% of developer Macs still Intel.
**Prevention:** Detect `platform.machine()` at startup. Auto-fallback to sherpa-onnx. Document requirement. `vox setup` detects and configures.
**Phase:** Audio pipeline

### P5: Model Download on First Run
**Severity:** MEDIUM
**Warning signs:** First `vox start` hangs 2-5 minutes, users think it's broken.
**Details:** MLX Whisper downloads ~1.5GB, openwakeword ~50MB. No progress in background service.
**Prevention:** Download in `vox setup` with progress bars. Check for models before audio loop. Clear error if missing.
**Phase:** Setup/CLI

### P6: Recording Indicator Subprocess Lifecycle
**Severity:** MEDIUM
**Warning signs:** Zombie processes, stuck indicator after crash.
**Details:** Current code uses SIGKILL (not graceful). Crash leaves indicator visible.
**Prevention:** SIGTERM with graceful handler. atexit cleanup. Auto-exit if parent PID dies. PID file for cleanup.
**Phase:** HUD/overlay

### P7: stdio MCP Transport + Logging Conflict
**Severity:** MEDIUM
**Warning signs:** MCP client receives garbage, tool calls fail.
**Details:** MCP stdio uses stdout for JSON-RPC. Any print()/logging to stdout breaks protocol.
**Prevention:** ALL logging to stderr. Use logging module with stderr handler. No print() in production. Test with real MCP client early.
**Phase:** MCP server

### P8: pyaudio/portaudio Installation Friction
**Severity:** MEDIUM
**Warning signs:** "portaudio.h not found" during install.
**Details:** pyaudio requires portaudio C library via `brew install portaudio`.
**Prevention:** Document prominently. `vox setup` checks and offers to install. Copy-paste instructions.
**Phase:** Distribution

## Moderate Pitfalls

### P9: Config Location & Migration
Use `~/.config/vox/config.yaml` (XDG). Migrate from old location. Never overwrite on upgrade.

### P10: launchd Edge Cases
Use `launchctl bootstrap`/`bootout` (modern API). Unique label `com.vox.listener`. Check for stale services.

### P11: Clipboard Collision
Save clipboard before injection, restore after paste. Small delay between operations.

### P12: Wake Word False Positives
Configurable confidence threshold (default 0.7+). Confirmation sound before recording.
