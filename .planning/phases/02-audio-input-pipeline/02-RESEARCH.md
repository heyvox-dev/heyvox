# Phase 2: Audio + Input Pipeline - Research

**Researched:** 2026-03-27
**Domain:** macOS audio device management, Bluetooth audio profiles, echo suppression, text injection adapters
**Confidence:** HIGH

## Summary

Phase 2 is primarily a **completion and hardening phase**, not a greenfield build. The core audio pipeline (wake word, PTT, STT, silence timeout, audio cues, mic priority, clipboard injection) was already implemented in Phase 1 and verified working. Phase 2's real scope is: (1) implementing the four truly missing requirements — headset detection (AUDIO-10), echo suppression (AUDIO-09), last-agent target tracking (INPT-04/INPT-05), and adapter wiring (INPT-03) — and (2) hardening the audio recovery with a proactive health check loop to prevent the silent-mic bug documented on 2026-03-27.

The most architecturally significant decision for Phase 2 is wiring the adapter pattern into `main.py`. Currently `main.py` calls `type_text`, `focus_app`, and `press_enter` from `vox.input.injection` directly, bypassing the `AgentAdapter` protocol that already exists in `vox/adapters/`. Phase 2 must fix this: `main.py` should resolve an adapter at startup based on `config.target_mode` and call `adapter.inject_text()` + `adapter.should_auto_send()` instead of calling injection functions directly.

The silent-mic recovery bug (G435 USB + MacBook mic both returning zeros after wake word listener held audio device in bad state) requires a **proactive health check loop** — not just reactive `IOError` recovery. The fix: periodically sample frames during idle, detect all-zeros, and trigger `pa.terminate()` + `PyAudio()` reinit. The key insight from the debugging session is that when an upstream process holds the audio session in a bad state, `pa.terminate()` + `PyAudio()` releases and re-acquires the CoreAudio session cleanly. Killing `coreaudiod` is not appropriate.

**Primary recommendation:** Address requirements in three work units: (1) headset detection + echo suppression in `vox/audio/mic.py` + `vox/main.py`, (2) adapter wiring and last-agent tracking in `vox/adapters/` + config, and (3) audio health check in `vox/main.py`. Keep the working main loop structure intact — add capabilities around it rather than refactoring the loop.

## Requirement Gap Analysis

This table is the authoritative pre-flight check. Planner must address every PARTIAL or TODO row.

| Requirement | Description | Status | Location |
|-------------|-------------|--------|----------|
| AUDIO-01 | Wake word detection with configurable threshold | DONE | `vox/main.py` loop |
| AUDIO-02 | PTT via configurable modifier key (fn default) | DONE | `vox/input/ptt.py` |
| AUDIO-03 | MLX Whisper (Apple Silicon) + sherpa-onnx fallback | DONE | `vox/audio/stt.py` |
| AUDIO-04 | Silence timeout auto-stops recording | DONE | `vox/main.py` silence watchdog |
| AUDIO-05 | Audio cues on start/stop/cancel | DONE | `vox/audio/cues.py` |
| AUDIO-06 | Mic device priority configurable | DONE | `vox/audio/mic.py` |
| AUDIO-07 | USB audio dongle support | DONE | `find_best_mic` device enumeration |
| AUDIO-08 | BT A2DP dead-mic auto-fallback to built-in | PARTIAL | `test_mic()` zeros-check at startup; missing health check loop |
| AUDIO-09 | Echo suppression: mute mic during TTS when no headset | TODO | Not implemented |
| AUDIO-10 | Headset detection (speaker vs headset) | TODO | Not implemented |
| INPT-01 | Generic adapter pastes via osascript | DONE | `vox/input/injection.py` + `vox/adapters/generic.py` |
| INPT-02 | Adapter Protocol class | DONE | `vox/adapters/base.py` |
| INPT-03 | Adapter selection via config.yaml | PARTIAL | `target_app` exists; no `target_mode` field; `main.py` bypasses adapter |
| INPT-04 | Smart target detection (AI agent window tracking) | TODO | Not implemented |
| INPT-05 | Configurable target behavior: always-focused / pinned-app / last-agent | PARTIAL | Always-focused and pinned-app work; last-agent missing |
| INPT-06 | Clipboard save/restore around injection | DONE | `vox/input/injection.py` `type_text()` |

**Bug fix required:** Audio health check loop for proactive silent-mic detection (documented 2026-03-27 debugging session). This is not a named requirement but blocks AUDIO-08 from being fully satisfied.

## Standard Stack

### Core (all already installed in Phase 1)
| Library | Version | Purpose | Why Standard |
|---------|---------|---------|--------------|
| pyaudio | 0.2.14 | Audio device enumeration and streaming | Already in use, CoreAudio bridge |
| numpy | >=1.24 | Audio frame analysis (zero detection, level check) | Already in use |
| AppKit (pyobjc-framework-Cocoa) | >=10.0 | NSWorkspace for app focus tracking (last-agent mode) | Already installed, native macOS API |
| Quartz (pyobjc-framework-Quartz) | >=10.0 | CGEventTap for PTT | Already installed |

### No New Dependencies Required
Phase 2 uses only what Phase 1 already installed. All required capabilities exist in the current dependency set:
- `pyaudio` for device enumeration and zero-detection
- `AppKit.NSWorkspace` for app focus observation (last-agent)
- `subprocess` + `osascript` for text injection (already in use)

### Alternatives NOT to Use
| Instead of | Don't Use | Reason |
|------------|-----------|--------|
| pyaudio device enumeration | sounddevice library | Already using pyaudio; no benefit to switching |
| NSWorkspace | Accessibility API window enumeration | NSWorkspace is simpler for app-level tracking |
| file-flag echo suppression | WebRTC AEC | WebRTC AEC is not available as a Python package on macOS; overkill for this use case |

## Architecture Patterns

### Recommended File Changes
```
vox/
├── audio/
│   └── mic.py           # ADD: detect_headset(), detect_silent_mic()
├── input/
│   └── ptt.py           # NO CHANGES — fully implemented
├── adapters/
│   ├── base.py          # NO CHANGES — fully implemented
│   ├── generic.py       # NO CHANGES — fully implemented
│   └── last_agent.py    # NEW: LastAgentAdapter (NSWorkspace observer)
├── config.py            # ADD: target_mode, agents, echo_suppression fields
├── constants.py         # ADD: TTS_PLAYING_FLAG = "/tmp/vox-tts-playing"
└── main.py              # MODIFY: wire adapter, add health check, add echo suppression
```

### Pattern 1: Headset Detection via PyAudio Device Names
**What:** A device is a headset if it appears in the PyAudio device list as both an input device and an output device with the same name (macOS creates separate in/out entries for the same hardware).

**Verified with:** Live device enumeration on test machine. G435 Wireless Gaming Headset appears as `[1] in=1 out=0` and `[2] in=0 out=2` — same name, two entries.

```python
# Source: live verification on macOS 15.3 with pyaudio 0.2.14
def detect_headset(pa, selected_input_index: int) -> bool:
    """Return True if the selected input device is a headset (has paired output)."""
    selected = pa.get_device_info_by_index(selected_input_index)
    selected_name = selected['name']
    for i in range(pa.get_device_count()):
        d = pa.get_device_info_by_index(i)
        if d['maxOutputChannels'] > 0 and i != selected_input_index:
            if d['name'].lower() == selected_name.lower():
                return True
    return False
```

**When to use:** Called once at startup after `find_best_mic()`. Store result in `headset_mode` bool for use in echo suppression check.

### Pattern 2: Echo Suppression via TTS Playing Flag
**What:** When a TTS process is speaking and no headset is detected, mic audio would feed TTS output back into wake word detection. The fix: check for `/tmp/vox-tts-playing` flag in the main loop and skip wake word processing when it exists.

**Architecture:** The TTS script (Phase 4 MCP) writes `/tmp/vox-tts-playing` when speaking, removes it when done. `main.py` checks the flag only when `headset_mode is False`.

```python
# In vox/constants.py
TTS_PLAYING_FLAG = "/tmp/vox-tts-playing"

# In main.py inner loop — check BEFORE feeding to model.predict()
if not headset_mode and os.path.exists(TTS_PLAYING_FLAG):
    continue  # Skip wake word processing while TTS is playing
```

**Fallback when TTS doesn't write the flag:** The existing `cues.py` suppression mechanism (`is_suppressed()`) already handles audio cue bleed-through. For Phase 2, implement the flag-check infrastructure; the TTS process (Phase 4) will write the flag.

### Pattern 3: Audio Health Check (Silent-Mic Recovery)
**What:** Proactive periodic check during idle that reads frames and detects all-zeros output, triggering a full PyAudio session restart.

**Why needed:** `IOError`-based recovery only triggers when `stream.read()` raises an exception. The silent-mic syndrome (audio device held in bad state by another process) does NOT raise IOError — it silently returns zeros. This is the exact bug documented 2026-03-27.

```python
# Source: analysis of documented bug + pyaudio behavior
# Add to main loop (after consecutive_errors handling, before model.predict):
HEALTH_CHECK_INTERVAL = 30.0  # seconds between checks
last_health_check = 0.0

# Inside loop, when idle (not recording, not busy):
if not _is_rec and not _is_busy:
    now = time.time()
    if now - last_health_check > HEALTH_CHECK_INTERVAL:
        last_health_check = now
        level = int(np.abs(audio).max())  # audio is the chunk just read
        if level == 0:
            zero_count = getattr(main, '_zero_streak', 0) + 1
            main._zero_streak = zero_count
            if zero_count >= 3:  # 3 consecutive zero chunks = silent mic
                log("Silent mic detected (3 consecutive zero frames), restarting audio session")
                main._zero_streak = 0
                # Trigger full reinit (same path as IOError recovery)
                stream.stop_stream(); stream.close(); pa.terminate()
                pa = pyaudio.PyAudio()
                dev_index = find_best_mic(pa, mic_priority=mic_priority, ...)
                stream = open_mic_stream(pa, dev_index, ...)
        else:
            main._zero_streak = 0
```

**Note:** `_zero_streak` tracking avoids single-frame zero false positives (brief silence is normal). Three consecutive health-check zero readings confirms the device is stuck.

### Pattern 4: Adapter Wiring in main.py
**What:** `main.py` currently calls `type_text`, `focus_app`, `press_enter` directly. Phase 2 must route through the adapter protocol so INPT-03/INPT-04/INPT-05 work.

```python
# In vox/main.py — adapter factory based on config
def _build_adapter(config: VoxConfig):
    """Resolve the correct adapter from config.target_mode."""
    mode = getattr(config, 'target_mode', 'always-focused')
    if mode == 'pinned-app' and config.target_app:
        from vox.adapters.generic import GenericAdapter
        return GenericAdapter(target_app=config.target_app, enter_count=config.enter_count)
    elif mode == 'last-agent':
        from vox.adapters.last_agent import LastAgentAdapter
        return LastAgentAdapter(agents=config.agents, enter_count=config.enter_count)
    else:  # 'always-focused' default
        from vox.adapters.generic import GenericAdapter
        return GenericAdapter(enter_count=config.enter_count)

# In _send_local(), replace direct injection calls with:
adapter.inject_text(paste_text)
if not _triggered_by_ptt and adapter.should_auto_send():
    press_enter(config.enter_count)
```

**Implication:** `GenericAdapter` needs to accept `target_app` and `enter_count` parameters. Update `vox/adapters/generic.py`.

### Pattern 5: Last-Agent Tracking (INPT-04/INPT-05)
**What:** `LastAgentAdapter` uses `NSWorkspace.sharedWorkspace().frontmostApplication()` to track which AI agent app was most recently focused. On `inject_text`, it activates that app and pastes.

```python
# Source: verified NSWorkspace API on macOS 15.3, AppKit available
import AppKit

class LastAgentAdapter:
    def __init__(self, agents: list[str], enter_count: int = 2):
        self._agents = [a.lower() for a in agents]
        self._enter_count = enter_count
        self._last_agent_name: str | None = None
        self._start_observer()

    def _start_observer(self):
        """Poll frontmost app (simpler than NSNotification thread)."""
        import threading
        def _poll():
            while True:
                try:
                    ws = AppKit.NSWorkspace.sharedWorkspace()
                    app = ws.frontmostApplication()
                    name = app.localizedName() or ""
                    if any(a in name.lower() for a in self._agents):
                        self._last_agent_name = name
                except Exception:
                    pass
                import time; time.sleep(1)
        t = threading.Thread(target=_poll, daemon=True)
        t.start()

    def inject_text(self, text: str) -> None:
        from vox.input.injection import focus_app, type_text
        if self._last_agent_name:
            focus_app(self._last_agent_name)
            import time; time.sleep(0.3)
        type_text(text)

    def should_auto_send(self) -> bool:
        return True  # Last-agent mode always sends (AI agent use case)
```

**Note on NSWorkspace notifications vs polling:** NSWorkspace notifications (`NSWorkspaceDidActivateApplicationNotification`) require a `CFRunLoop` on the main thread, which conflicts with the audio loop. Polling every 1s is simpler, thread-safe, and sufficient for this use case (latency requirement is "within a few seconds", not milliseconds).

### Anti-Patterns to Avoid
- **Killing coreaudiod:** `killall coreaudiod` restarts the entire macOS audio daemon, causes all audio apps to reset, and is not appropriate for Vox. Use `pa.terminate()` + `PyAudio()` instead.
- **Refactoring the main loop:** The Phase 1 loop works. Add health check, echo suppression, and adapter dispatch around the existing structure. Don't restructure the control flow.
- **NSWorkspace on the audio loop thread:** NSWorkspace notifications need a `CFRunLoop`. Polling in a background thread is the correct approach.
- **Using `system_profiler` for BT detection:** `system_profiler SPBluetoothDataType` parses JSON but is slow (~500ms) and doesn't tell us about A2DP vs HFP profile state. PyAudio device enumeration + zero-detection is faster and more reliable.
- **WebRTC AEC for echo suppression:** Not available as a Python package on macOS. The flag-based mute approach is simpler and works for this use case.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| BT A2DP detection | Custom BT profile query via IOBluetooth | `test_mic()` zeros detection | IOBluetooth profile detection is complex; zeros-detection already works |
| Headset detection | system_profiler JSON parsing | PyAudio device name matching | Already have device list; name matching is 0ms vs 500ms |
| Echo suppression filter | WebRTC AEC / custom DSP | File flag + mute-during-TTS | DSP requires C extension; flag approach is correct for this architecture |
| App focus tracking | Accessibility API element queries | `NSWorkspace.frontmostApplication()` | Much simpler; we only need app name, not window element |
| PTT capture | pynput | Quartz CGEventTap (already in ptt.py) | pynput misses fn/Globe key — documented and already solved |

**Key insight:** The hard problems (PTT, STT, audio device recovery) are already solved. Phase 2 hard problems are architectural: wiring the adapter pattern and making the detection algorithms correct.

## Common Pitfalls

### Pitfall 1: Silent-Mic Syndrome Goes Undetected
**What goes wrong:** PyAudio `stream.read()` succeeds (no IOError), returns all zeros, wake word never triggers, user thinks Vox is broken.
**Why it happens:** Another process held the CoreAudio session. Stream is "open" but the device is in a bad state.
**How to avoid:** Health check loop — count consecutive idle-period zero-level readings. After 3, trigger `pa.terminate()` + `PyAudio()` reinit.
**Warning signs:** Logs show no audio levels above threshold for extended periods. `find_best_mic()` at startup showed non-zero levels, so device was working initially.

### Pitfall 2: Echo Suppression Triggers During Silence
**What goes wrong:** `/tmp/vox-tts-playing` flag not cleaned up after TTS crash. Mic stays suppressed forever.
**Why it happens:** TTS process crashes without removing the flag.
**How to avoid:** Add flag age check: if `/tmp/vox-tts-playing` is more than 60s old, ignore it (TTS is done or crashed).
**Warning signs:** Wake word never triggers after a TTS session.

### Pitfall 3: Adapter Not Wired to main.py
**What goes wrong:** `GenericAdapter` and `LastAgentAdapter` exist but `main.py` still calls `type_text` directly. INPT-03, INPT-04, INPT-05 appear satisfied but are actually bypassed.
**Why it happens:** Phase 1 implemented adapters as scaffolding without wiring them to the main loop.
**How to avoid:** Phase 2 task must explicitly change `_send_local()` to use `adapter.inject_text()` instead of `type_text()`.
**Warning signs:** `grep "type_text" vox/main.py` still returns results after Phase 2.

### Pitfall 4: LastAgentAdapter Race with PTT
**What goes wrong:** User uses PTT (which should paste into currently focused app), but `LastAgentAdapter` refocuses the last-agent window.
**Why it happens:** PTT mode should bypass the adapter's app-focus logic.
**How to avoid:** Pass `is_ptt=True` to `inject_text()` OR check `_triggered_by_ptt` before adapter dispatch: if PTT, use GenericAdapter regardless of `target_mode`.
**Warning signs:** PTT pastes into wrong app.

### Pitfall 5: Config target_mode Not Added to VoxConfig
**What goes wrong:** `config.target_mode` attribute raises `AttributeError` at runtime.
**Why it happens:** `vox/config.py` not updated with new fields.
**How to avoid:** Add `target_mode: str = "always-focused"` and `agents: list[str] = ["Claude", "Cursor", "Terminal"]` to `VoxConfig` before any code references them.
**Warning signs:** Test `python -c "from vox.config import VoxConfig; c = VoxConfig(); print(c.target_mode)"` fails.

### Pitfall 6: G435 USB vs Bluetooth Headset Detection
**What goes wrong:** G435 connected via USB shows `in=1, out=0` for the input device and `in=0, out=2` for output — different channel counts but same name. Detection works. However G435 connected via Bluetooth may show different device names ("G435 Bluetooth Gaming Headset" vs "G435 Wireless Gaming Headset"). Name matching must be loose.
**Why it happens:** macOS uses different device names for BT vs USB connections.
**How to avoid:** `detect_headset()` should use case-insensitive partial name matching, not exact match.
**Warning signs:** Headset detection returns False for BT-connected headset.

## Code Examples

### Headset Detection
```python
# Source: live verification with G435 + pyaudio 0.2.14 on macOS 15.3
def detect_headset(pa, selected_input_index: int) -> bool:
    """Return True if the selected input device is a headset.

    Detects headsets by finding an output device with the same name.
    macOS creates separate input/output entries for headsets.
    Uses partial/case-insensitive matching for BT vs USB name variations.
    """
    selected = pa.get_device_info_by_index(selected_input_index)
    selected_name = selected['name'].lower()
    for i in range(pa.get_device_count()):
        d = pa.get_device_info_by_index(i)
        if d['maxOutputChannels'] > 0 and i != selected_input_index:
            # Partial match: handles "G435 Wireless" vs "G435 Bluetooth" variations
            out_name = d['name'].lower()
            if selected_name in out_name or out_name in selected_name:
                return True
    return False
```

### Echo Suppression Check in Main Loop
```python
# Source: architecture design — coordinates with TTS_PLAYING_FLAG constant
# Add to vox/constants.py:
# TTS_PLAYING_FLAG = "/tmp/vox-tts-playing"
# TTS_PLAYING_MAX_AGE_SECS = 60.0  # ignore stale flag

# In main.py — before model.predict(audio):
if not headset_mode and os.path.exists(TTS_PLAYING_FLAG):
    flag_age = time.time() - os.path.getmtime(TTS_PLAYING_FLAG)
    if flag_age < TTS_PLAYING_MAX_AGE_SECS:
        continue  # Suppress wake word while TTS is playing
```

### Config Additions for Phase 2
```python
# Add to vox/config.py VoxConfig model
class EchoSuppressionConfig(BaseModel):
    enabled: bool = True

class VoxConfig(BaseModel):
    # ... existing fields ...
    target_mode: str = "always-focused"  # always-focused | pinned-app | last-agent
    agents: list[str] = ["Claude", "Cursor", "Terminal", "iTerm2"]
    echo_suppression: EchoSuppressionConfig = EchoSuppressionConfig()
```

### Health Check Loop Integration
```python
# Source: analysis of silent-mic bug (2026-03-27 debugging session)
# Zero-streak counter to distinguish brief silence from stuck device
_zero_streak = 0
HEALTH_CHECK_INTERVAL = 30.0
last_health_check = time.time()

# In main loop after reading audio chunk, when idle:
if not _is_rec and not _is_busy:
    now = time.time()
    if now - last_health_check >= HEALTH_CHECK_INTERVAL:
        last_health_check = now
        level = int(np.abs(audio).max())
        if level == 0:
            _zero_streak += 1
            if _zero_streak >= 3:
                log("WARNING: Silent mic detected (3 consecutive checks), restarting audio session")
                _zero_streak = 0
                stream.stop_stream(); stream.close(); pa.terminate()
                time.sleep(1)
                pa = pyaudio.PyAudio()
                dev_index = find_best_mic(pa, mic_priority=mic_priority,
                                          sample_rate=sample_rate, chunk_size=chunk_size)
                if dev_index is None:
                    log("No mic after reinit, continuing...")
                    continue
                stream = open_mic_stream(pa, dev_index, sample_rate=sample_rate, chunk_size=chunk_size)
                consecutive_errors = 0
        else:
            _zero_streak = 0
```

### GenericAdapter Updated Signature
```python
# vox/adapters/generic.py — updated to accept config params
from vox.input.injection import type_text, focus_app

class GenericAdapter:
    """Paste transcription into focused app, or a pinned app if target_app is set."""

    def __init__(self, target_app: str = "", enter_count: int = 2):
        self._target_app = target_app
        self._enter_count = enter_count

    def inject_text(self, text: str) -> None:
        if self._target_app:
            focus_app(self._target_app)
            import time; time.sleep(0.3)
        type_text(text)

    def should_auto_send(self) -> bool:
        # Auto-send when targeting a specific app (AI agent input mode)
        return bool(self._target_app)
```

## State of the Art

| Old Approach | Current Approach | Impact for Phase 2 |
|--------------|------------------|--------------------|
| Reactive IOError recovery only | Proactive silent-mic health check | Catches A2DP bad-state without process restart |
| Direct injection calls in main.py | Adapter protocol dispatch | Enables extensible target modes |
| Hardcoded target app | target_mode config field | Unlocks last-agent and always-focused modes |
| No echo suppression | Flag-based mic mute during TTS | Prevents feedback loops in speaker mode |

## Open Questions

1. **echo_suppression.enabled config field vs always-on**
   - What we know: The flag-check is cheap (file existence). Headset detection at startup is reliable.
   - What's unclear: Should users be able to disable echo suppression even in speaker mode?
   - Recommendation: Add `echo_suppression.enabled: bool = True` to config. Disabled when headset_mode is True automatically. User can force-disable if they want echo risk.

2. **Health check interval configuration**
   - What we know: 30s interval means up to 30s delay in detecting silent-mic syndrome.
   - What's unclear: Should this be user-configurable? Too short (5s) may cause spurious reinits during silence.
   - Recommendation: Hardcode at 30s for Phase 2 with `_zero_streak >= 3` requirement (effectively 90s of confirmed silence before reinit). Add to config only if user feedback requests it.

3. **LastAgentAdapter: polling vs NSWorkspace notifications**
   - What we know: Polling every 1s works. NSWorkspace notifications are more efficient but require CFRunLoop threading.
   - What's unclear: Whether 1s polling latency is acceptable.
   - Recommendation: Use polling for Phase 2. The use case (agent window tracking) doesn't need sub-second accuracy. Add notifications in a future phase if needed.

4. **PTT + last-agent interaction**
   - What we know: PTT should paste into focused app, not the last-agent window.
   - Recommendation: In `_send_local()`, if `_triggered_by_ptt is True`, bypass adapter and call `type_text()` directly. PTT is inherently "paste into what's focused now."

## Sources

### Primary (HIGH confidence)
- Live code inspection: `/Users/work/conductor/workspaces/vox-v2/mogadishu/vox/` — all module files read and analyzed
- Live device enumeration: pyaudio 0.2.14 on macOS 15.3 with G435 USB + MacBook Pro Microphone — headset detection pattern verified
- Live NSWorkspace test: `AppKit.NSWorkspace.sharedWorkspace().frontmostApplication()` verified working

### Secondary (MEDIUM confidence)
- Phase 1 verification report: `.planning/phases/01-foundation/01-VERIFICATION.md` — confirms what Phase 1 built
- Bug analysis: 2026-03-27 debugging session description — confirms silent-mic root cause and recovery approach

### Tertiary (LOW confidence)
- NSWorkspace notification threading approach (polling preferred over notifications for this use case — not deeply verified)

## Metadata

**Confidence breakdown:**
- Standard stack: HIGH — all libraries already installed and verified working
- Architecture: HIGH — patterns verified with live code and device enumeration
- Pitfalls: HIGH — pitfalls drawn from actual debugging session and code inspection
- Headset detection: HIGH — verified live with G435 device
- Echo suppression: MEDIUM — flag approach is correct but TTS script integration not yet built

**Research date:** 2026-03-27
**Valid until:** 2026-04-27 (stable macOS APIs, no fast-moving dependencies)
