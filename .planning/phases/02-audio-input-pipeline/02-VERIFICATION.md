---
phase: 02-audio-input-pipeline
verified: 2026-03-27T09:17:15Z
status: passed
score: 11/11 must-haves verified
re_verification: false
gaps: []
human_verification:
  - test: "Say configured wake word, then speak a sentence"
    expected: "Recording indicator appears, cue plays, text appears in focused app"
    why_human: "Requires running audio hardware, openwakeword model, and actual mic input"
  - test: "Hold fn key, speak, release"
    expected: "PTT recording starts on press, stops on release, text injected into focused app without Enter"
    why_human: "Requires Accessibility permissions and live Quartz event tap"
  - test: "Stop speaking for silence_timeout_secs"
    expected: "Recording auto-cancels with 'paused' cue"
    why_human: "Requires live audio stream and real silence"
  - test: "Copy text to clipboard, trigger voice input, check clipboard after injection"
    expected: "Original clipboard content is restored after paste"
    why_human: "Requires live osascript execution and clipboard state observation"
  - test: "Run with no headset, trigger TTS (write /tmp/vox-tts-playing), say wake word"
    expected: "Wake word detection is suppressed while flag is fresh"
    why_human: "Requires real audio hardware and TTS flag file coordination"
  - test: "Run with BT headset connected and set as mic"
    expected: "detect_headset() returns True, echo suppression logs 'inactive'"
    why_human: "Requires real macOS PyAudio device enumeration with headset hardware"
---

# Phase 02: Audio + Input Pipeline Verification Report

**Phase Goal:** User can speak via wake word or push-to-talk and have transcribed text appear in the focused app
**Verified:** 2026-03-27T09:17:15Z
**Status:** PASSED
**Re-verification:** No — initial verification
**Plans covered:** 02-01 (echo suppression + health check) and 02-02 (adapter dispatch + last-agent)

---

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | Wake word activates recording; fn key activates PTT | VERIFIED | `vox/audio/wakeword.py` loads openwakeword Model; `vox/input/ptt.py` creates Quartz CGEventTap on `fn` (0x800000); both call `start_recording()` / `stop_recording()` in `main.py` |
| 2 | Speech transcribed locally via MLX Whisper; text appears in focused app | VERIFIED | `vox/audio/stt.py` implements `init_local_stt` + `transcribe_audio` with mlx_whisper and sherpa-onnx; `_send_local()` calls `transcribe_audio` then routes through adapter or `type_text()` |
| 3 | Silence timeout auto-stops recording; audio cues play on start/stop/cancel | VERIFIED | Silence watchdog in `main.py` lines 513–532 checks `_audio_buffer` against `silence_threshold`; `audio_cue("listening")` in `start_recording()`, `audio_cue("ok"/"paused")` in `stop_recording()`, `audio_cue("paused")` on cancel paths |
| 4 | Clipboard is saved/restored around paste (user clipboard preserved) | VERIFIED | `vox/input/injection.py` `type_text()` calls `get_clipboard_text()` + `clipboard_is_image()` before paste and restores original text after Cmd-V via osascript |
| 5 | Mic device priority respected; USB dongle support; BT A2DP dead-mic auto-falls back; echo suppression in speaker mode | VERIFIED | `find_best_mic()` in `mic.py` tests devices by level with priority list; IOError path in `main.py` lines 441–466 reinits PyAudio and calls `find_best_mic` again; health check loop (lines 476–509) detects 3 consecutive zero-level reads and triggers full PyAudio reinit; `detect_headset()` uses bidirectional substring matching; echo suppression block lines 541–550 checks `TTS_PLAYING_FLAG` when `not headset_mode and config.echo_suppression.enabled` |

**Score:** 5/5 truths verified

---

### Required Artifacts

#### Plan 02-01 Artifacts

| Artifact | Provides | Status | Details |
|----------|----------|--------|---------|
| `vox/audio/mic.py` | `detect_headset()` function | VERIFIED | Lines 112–151; bidirectional substring match `selected_name in out_name or out_name in selected_name`; AUDIO-10 referenced in docstring |
| `vox/constants.py` | `TTS_PLAYING_FLAG` and `TTS_PLAYING_MAX_AGE_SECS` | VERIFIED | Lines 24–29; `TTS_PLAYING_FLAG = "/tmp/vox-tts-playing"`, `TTS_PLAYING_MAX_AGE_SECS = 60.0`; AUDIO-09 referenced |
| `vox/config.py` | `EchoSuppressionConfig` nested model on `VoxConfig` | VERIFIED | Lines 94–104 (`EchoSuppressionConfig`); line 142 (`echo_suppression: EchoSuppressionConfig = EchoSuppressionConfig()` on `VoxConfig`); YAML template section lines 313–316 |
| `vox/main.py` | Echo suppression check and health check loop | VERIFIED | Echo suppression: lines 541–550 (TTS_PLAYING_FLAG check before model.predict); health check: lines 428–431 (init) and 476–509 (loop with 3-strike reinit); TTS_PLAYING_FLAG imported line 28–29 |

#### Plan 02-02 Artifacts

| Artifact | Provides | Status | Details |
|----------|----------|--------|---------|
| `vox/config.py` | `target_mode` and `agents` fields on `VoxConfig` | VERIFIED | Lines 132–133; `target_mode: str = "always-focused"` with `field_validator` lines 152–158; `agents: list[str] = ["Claude", "Cursor", "Terminal", "iTerm2"]` |
| `vox/adapters/generic.py` | `GenericAdapter` with `target_app` and `enter_count` constructor params | VERIFIED | Lines 29–58; `__init__(target_app="", enter_count=2)`; `inject_text()` calls `focus_app` when `self._target_app` is set then `type_text()`; `should_auto_send()` returns `bool(self._target_app)` |
| `vox/adapters/last_agent.py` | `LastAgentAdapter` with NSWorkspace polling and `inject_text` | VERIFIED | Full 83-line implementation; daemon thread polls `NSWorkspace.frontmostApplication()` every 1s with lazy AppKit import; `inject_text()` focuses last-seen agent then calls `type_text()` |
| `vox/main.py` | `_build_adapter()` factory and `adapter.inject_text()` dispatch in `_send_local()` | VERIFIED | `_build_adapter()` lines 222–236; module-level `_adapter = None` line 50; global set in `main()` lines 399–401; `_send_local()` dispatches via `adapter.inject_text(paste_text)` line 296 |

---

### Key Link Verification

#### Plan 02-01 Key Links

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `vox/main.py` | `vox/audio/mic.py` | `detect_headset()` called after `open_mic_stream()` | WIRED | `detect_headset` imported line 31; called line 416; re-called after health check reinit line 505 |
| `vox/main.py` | `vox/constants.py` | `TTS_PLAYING_FLAG` import + echo suppression check | WIRED | Imported lines 28–30; used in echo suppression block line 544 (`os.path.exists(TTS_PLAYING_FLAG)`) and `os.path.getmtime(TTS_PLAYING_FLAG)` line 546 |
| `vox/main.py` | `vox/config.py` | `config.echo_suppression.enabled` controls echo check | WIRED | Line 543: `if not headset_mode and config.echo_suppression.enabled:` |

#### Plan 02-02 Key Links

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `vox/main.py` | `vox/adapters/generic.py` | `_build_adapter()` instantiates `GenericAdapter` for always-focused and pinned-app | WIRED | Lines 229–230 (pinned-app), lines 235–236 (always-focused fallback) |
| `vox/main.py` | `vox/adapters/last_agent.py` | `_build_adapter()` instantiates `LastAgentAdapter` for last-agent mode | WIRED | Lines 231–233 |
| `vox/main.py` `_send_local()` | `adapter.inject_text()` | Replaces direct `type_text`/`focus_app` calls in wake-word path | WIRED | Line 296: `adapter.inject_text(paste_text)` in the `else` branch (non-PTT path) |
| `vox/config.py` | `vox/main.py` | `config.target_mode` drives adapter selection | WIRED | `_build_adapter(config)` reads `config.target_mode` line 227; confirmed no direct `focus_app` in `_send_local` |

---

### Requirements Coverage

| Requirement | Status | Notes |
|-------------|--------|-------|
| AUDIO-08 (health check loop) | SATISFIED | 30s interval, 3-strike zero-level detection, full PyAudio reinit in `main.py` |
| AUDIO-09 (echo suppression) | SATISFIED | TTS flag check before `model.predict()` in speaker mode; stale flag guard (60s) |
| AUDIO-10 (headset detection) | SATISFIED | `detect_headset()` in `mic.py` with bidirectional partial name matching |
| INPT-03 (adapter selection via config) | SATISFIED | `_build_adapter()` factory + `target_mode` validated field on `VoxConfig` |
| INPT-04 (smart target detection) | SATISFIED | `LastAgentAdapter` polls `NSWorkspace.frontmostApplication()` in daemon thread |
| INPT-05 (last-agent mode + PTT bypass) | SATISFIED | `LastAgentAdapter` implemented; PTT path calls `type_text()` directly, bypasses adapter |

---

### Anti-Patterns Found

None. No TODO/FIXME/PLACEHOLDER comments. No empty implementations. No stub return values. All functions have substantive implementations wired into the main event loop.

---

### Human Verification Required

The following items cannot be verified programmatically and require live hardware testing:

#### 1. Wake Word End-to-End

**Test:** Run `vox start` with a configured openwakeword model. Say the wake word, then speak a sentence.
**Expected:** Recording indicator appears, listening cue plays, transcription runs locally, text is injected into the focused app.
**Why human:** Requires working microphone, openwakeword model file, and running macOS GUI.

#### 2. Push-to-Talk (fn key)

**Test:** Hold fn while vox is running. Speak. Release fn.
**Expected:** Recording starts on press (cue plays), stops on release, text injected into focused app without pressing Enter.
**Why human:** Requires Quartz Accessibility permission grant and live keyboard event tap.

#### 3. Silence Timeout

**Test:** Trigger wake word recording. Stop speaking and wait.
**Expected:** After `silence_timeout_secs` (default 5s) of silence, recording auto-cancels with "paused" cue; no text injected.
**Why human:** Requires live audio stream and real silence detection.

#### 4. Clipboard Preservation

**Test:** Copy something to clipboard. Trigger vox (wake word or PTT). Speak. After text injection, check clipboard.
**Expected:** Clipboard contains the original pre-paste content, not the transcribed text.
**Why human:** Requires live osascript execution and observable clipboard state.

#### 5. Echo Suppression (speaker mode)

**Test:** Run vox with no headset. Write the flag file `touch /tmp/vox-tts-playing`. Say the wake word.
**Expected:** Wake word is NOT triggered while flag is present and fresh.
**Why human:** Requires real openwakeword model evaluation loop and flag file coordination.

#### 6. BT Headset Detection

**Test:** Connect a Bluetooth headset and configure it as the input mic. Start vox.
**Expected:** Log shows "Headset detected: True (echo suppression inactive)".
**Why human:** Requires real BT hardware and macOS PyAudio device enumeration.

---

## Gaps Summary

No gaps. All 11 must-have items from both plans (02-01 and 02-02) are verified as EXISTING, SUBSTANTIVE, and WIRED.

All four task commits are present in the git log (b0d5f48, ebbdd64, e8b7d32, 1c597f1). The codebase matches what the summaries describe — no discrepancy found between summary claims and actual code.

The phase goal is structurally achieved: the pipeline from audio capture through wake word/PTT detection through local STT transcription through adapter-based text injection is fully implemented and connected. Runtime correctness (model loading, hardware access) requires human verification on target hardware.

---

_Verified: 2026-03-27T09:17:15Z_
_Verifier: Claude (gsd-verifier)_
