---
phase: 13-audio-reliability
verified: 2026-04-13T11:10:00Z
status: passed
score: 14/14 must-haves verified
gaps: []
human_verification:
  - test: "Connect a Bluetooth headset (e.g. G435), say 'Hey Jarvis' while TTS is playing"
    expected: "Wake word fires, TTS playback stops immediately, recording starts"
    why_human: "Requires physical Bluetooth headset and live TTS playback to test D-05/D-08 integration"
  - test: "Press Escape while TTS is playing"
    expected: "TTS stops immediately, queue is cleared, HUD returns to idle"
    why_human: "Requires live TTS playback and Escape key event — can't be verified with grep"
  - test: "Connect a Bluetooth mic (no headset), speak in a loud room, check silence detection"
    expected: "Silence detection uses 85% threshold — occasional noise spikes don't prematurely end recording"
    why_human: "Requires Bluetooth hardware with noise spikes to exercise percentage-based threshold"
  - test: "Run 'heyvox calibrate' with no mic arguments"
    expected: "Records 3 seconds of ambient noise, prints noise_floor + silence_threshold, saves to ~/.cache/heyvox/mic-profiles.json"
    why_human: "Requires actual microphone hardware; can't run in CI without audio device"
---

# Phase 13: Audio Reliability Verification Report

**Phase Goal:** Robust audio pipeline across mic types with per-device profiles, headset-aware echo suppression, and instant TTS interruption
**Verified:** 2026-04-13T11:10:00Z
**Status:** PASSED
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | MicProfileManager loads profiles from config.yaml mic_profiles and cached calibration data | VERIFIED | `heyvox/audio/profile.py` — `_load_cache()` on init, `get_profile()` merges cache + config |
| 2 | Config.yaml overrides always take priority over cached calibration values | VERIFIED | `get_profile()` applies config fields last (lines 170-188 in profile.py), test_config_override_wins_over_cache passes |
| 3 | Calibration data persists to ~/.cache/heyvox/mic-profiles.json with 30-day expiry | VERIFIED | `_write_cache()` with atomic tempfile+os.replace; `_CACHE_EXPIRY_SECS = 30 * 24 * 3600` enforced in get_profile |
| 4 | Profile lookup uses partial case-insensitive name matching | VERIFIED | `key.lower() in device_key` loop in get_profile; test_partial_name_match and test_case_insensitive_match pass |
| 5 | Auto-calibration collects ~50 chunks of ambient noise and computes noise_floor + silence_threshold | VERIFIED | `_calibrating` + 50-chunk collection in main.py _run_loop (lines 651-665); run_calibration: median + 3.5x cap=500 |
| 6 | Silence detection uses 85% chunk percentage threshold, not single-spike max | VERIFIED | main.py lines 826 and 845: `if quiet_pct >= 0.85:` — both false-trigger cancel and speech-end paths |
| 7 | herald stop command kills afplay PID and clears entire queue | VERIFIED | `_cmd_stop()` in herald/cli.py calls `_kill_afplay()` then `_cmd_skip()`; all 3 TestCmdStop* test classes pass |
| 8 | herald interrupt command kills afplay PID but preserves unrelated queued messages | VERIFIED | `_cmd_interrupt()` calls `_kill_afplay()` but NOT `_cmd_skip()`; test_cmd_interrupt_does_not_clear_queue passes |
| 9 | Escape key stops TTS playback immediately via herald stop | VERIFIED | tts.py `stop_all()` calls `_herald("stop")` which now routes to working `_cmd_stop()`; test_tts_stop_all_calls_herald_stop passes |
| 10 | stop_all() in tts.py calls herald stop (not a broken unknown command) | VERIFIED | `_herald("stop")` in stop_all() confirmed; dispatch routes "stop" to _cmd_stop returning 0 |
| 11 | Wake word detection stays active during TTS when headset is connected (echo_safe=true) | VERIFIED | main.py: `if _tts_active and not _echo_safe: continue` — only suppresses when NOT echo_safe; test_wake_word_active_during_tts_with_headset passes |
| 12 | Wake word detection is suppressed during TTS when using built-in speakers (echo_safe=false) | VERIFIED | Same block: headset_mode=False → _echo_safe=False → suppression active; test_wake_word_suppressed_during_tts_speaker_mode passes |
| 13 | Post-TTS grace period is 0.5s for headset devices and 2.0s for speaker mode | VERIFIED | main.py: `_echo_grace = 0.5 if _echo_safe else 2.0`; TestEchoSafeGating::test_grace_period_* passes |
| 14 | heyvox calibrate CLI command runs noise measurement and saves results | VERIFIED | `_cmd_calibrate` in cli.py with `run_calibration` + `save_calibration`; registered as "calibrate" subparser |

**Score:** 14/14 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `heyvox/audio/profile.py` | MicProfileManager class with get_profile, save_calibration, run_calibration | VERIFIED | 242 lines; all 3 methods present; MicProfileEntry dataclass with 9 fields |
| `heyvox/config.py` | MicProfileEntryConfig + mic_profiles on HeyvoxConfig + force_disabled on EchoSuppressionConfig | VERIFIED | MicProfileEntryConfig lines 232-257; mic_profiles line 303; force_disabled line 229 |
| `heyvox/herald/cli.py` | stop and interrupt commands in dispatch | VERIFIED | _cmd_stop (101-108), _cmd_interrupt (111-120), _kill_afplay (123-133), _clear_tts_state (136-147); dispatch routes both |
| `heyvox/audio/tts.py` | interrupt() calls herald interrupt, stop_all() calls herald stop, clear_queue() calls herald skip | VERIFIED | Lines 183, 193, 198 confirmed |
| `heyvox/device_manager.py` | profile_manager + active_profile; updated on every detect_headset call | VERIFIED | profile_manager (line 91), active_profile (line 99); updated in init (152), reinit (272), _recover_silent_mic (451), scan hotplug (655, 705) |
| `heyvox/main.py` | echo_safe gate, profile-aware silence threshold, auto-calibration, wake-word-interrupt-TTS | VERIFIED | _echo_safe block (895-902), _calibrating loop (558-665), tts_interrupt call (948-952) |
| `heyvox/cli.py` | calibrate subcommand with run_calibration + save_calibration | VERIFIED | _cmd_calibrate (509), registered at line 853 with --device, --duration, --show args |
| `tests/test_mic_profile.py` | Unit tests for MicProfileManager | VERIFIED | 16 tests (6 TestMicProfileEntryConfig + 10 TestMicProfileManager); all pass |
| `tests/test_herald_cli.py` | Unit tests for herald stop/interrupt | VERIFIED | 15 tests in 5 classes; all pass |
| `tests/test_echo_suppression.py` | Tests for echo_safe gating including test_wake_word_active_during_tts_with_headset | VERIFIED | TestEchoSafeGating (8 tests) + TestEchoSuppressionConfig (2 tests); all pass (36 pass, 2 skipped) |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| `heyvox/audio/profile.py` | `heyvox/config.py` | `MicProfileEntryConfig` import | VERIFIED | `from heyvox.config import MicProfileEntryConfig` (TYPE_CHECKING guard) |
| `heyvox/audio/profile.py` | `~/.cache/heyvox/mic-profiles.json` | JSON cache read/write | VERIFIED | `self._cache_file = cache_dir / "mic-profiles.json"` in __init__ |
| `heyvox/audio/tts.py` | `heyvox/herald/cli.py` | `_herald("stop")` subprocess | VERIFIED | tts.py calls _herald("stop"); herald/cli.py dispatch routes to _cmd_stop() |
| `heyvox/herald/cli.py` | `HERALD_PLAYING_PID` | `os.kill(pid, SIGTERM)` | VERIFIED | `_kill_afplay()` reads HERALD_PLAYING_PID, calls `os.kill(pid, signal.SIGTERM)` |
| `heyvox/main.py` | `heyvox/audio/profile.py` | `profile_manager.get_profile` | VERIFIED | main.py line 23: `from heyvox.audio.profile import MicProfileManager`; get_profile called at 464 |
| `heyvox/main.py` | `heyvox/device_manager.py` | `devices.headset_mode` for echo_safe | VERIFIED | main.py line 896: `_echo_safe = devices.headset_mode` |
| `heyvox/device_manager.py` | `heyvox/audio/profile.py` | `active_profile` updated on device switch | VERIFIED | 4 call sites in device_manager.py (lines 152, 272, 451, 655, 705) |
| `heyvox/cli.py` | `heyvox/audio/profile.py` | `run_calibration` + `save_calibration` | VERIFIED | cli.py lines 640-641: `mgr.run_calibration(chunks)` and `mgr.save_calibration(target_name, ...)` |

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
|----------|---------------|--------|--------------------|--------|
| `heyvox/main.py` | `silence_threshold` | `devices.active_profile.silence_threshold` or `config.silence_threshold` | Yes — profile reads from cache (JSON) or config YAML | FLOWING |
| `heyvox/main.py` | `_echo_safe` | `devices.headset_mode` (PyAudio device detection) | Yes — detect_headset() queries PyAudio at runtime | FLOWING |
| `heyvox/main.py` | `_calibrating` / calibration chunks | Audio stream read via `stream.read()` | Yes — reads from real mic stream | FLOWING |
| `heyvox/audio/profile.py` | `_cache` | `mic-profiles.json` via JSON read on init | Yes — real file I/O; also written on save_calibration | FLOWING |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| profile.py imports cleanly | `python -c "from heyvox.audio.profile import MicProfileManager, MicProfileEntry"` | Exit 0 | PASS |
| config.py loads mic_profiles | `python -c "from heyvox.config import MicProfileEntryConfig, HeyvoxConfig; c = HeyvoxConfig(mic_profiles={'G435': {'silence_threshold': 300}}); print(c.mic_profiles)"` | Prints MicProfileEntryConfig with silence_threshold=300 | PASS |
| herald stop returns 0 | `python -c "from heyvox.herald.cli import dispatch; r = dispatch(['stop']); assert r == 0"` | Exit 0 | PASS |
| herald interrupt returns 0 | `python -c "from heyvox.herald.cli import dispatch; r = dispatch(['interrupt']); assert r == 0"` | Exit 0 | PASS |
| cli calibrate registered | `grep "calibrate" heyvox/cli.py` | _cmd_calibrate + subparser found | PASS |
| test_mic_profile passes | `python -m pytest tests/test_mic_profile.py -q` | 16 passed | PASS |
| test_herald_cli passes | `python -m pytest tests/test_herald_cli.py -q` | 15 passed | PASS |
| test_echo_suppression passes | `python -m pytest tests/test_echo_suppression.py -q` | 36 passed, 2 skipped | PASS |
| combined test run | `python -m pytest tests/test_mic_profile.py tests/test_herald_cli.py tests/test_echo_suppression.py -q` | 67 passed, 2 skipped | PASS |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|------------|-------------|--------|----------|
| D-01 | 13-01-PLAN | Config file + auto-calibrate for device profiles | SATISFIED | MicProfileManager with both YAML config_profiles and JSON cache |
| D-02 | 13-01-PLAN | Full profile fields: noise_floor, silence_threshold, buffer_size, cooldown_tier, sample_rate, chunk_size, gain, voice_isolation_mode, echo_safe | SATISFIED | MicProfileEntryConfig has all 9 fields; MicProfileEntry dataclass matches |
| D-03 | 13-01-PLAN | Cache persists to ~/.cache/heyvox/mic-profiles.json, auto-expires, config wins over cache | SATISFIED | 30-day expiry enforced; config overlay always applied last in get_profile() |
| D-04 | 13-01-PLAN, 13-04-PLAN | Calibration auto (on mic connect) and manual via `heyvox calibrate` | SATISFIED | Auto-calibration in main.py _run_loop; manual via cli.py _cmd_calibrate |
| D-05 | 13-02-PLAN, 13-03-PLAN | Wake word during TTS: kill afplay immediately, start recording instantly | SATISFIED | main.py lines 947-952: writes RECORDING_FLAG then calls tts_interrupt() |
| D-06 | 13-02-PLAN | Remaining queued TTS: drop current message parts, other messages survive | SATISFIED | `herald interrupt` kills afplay but does NOT clear queue — orchestrator handles selective purge |
| D-07 | 13-02-PLAN | Escape kills TTS + drops entire queue | SATISFIED | `herald stop` = _kill_afplay + _cmd_skip (clear all); called by stop_all() |
| D-08 | 13-03-PLAN | echo_safe = headset_mode by default; auto-set from device type | SATISFIED | `_echo_safe = devices.headset_mode` as default in main.py |
| D-09 | 13-03-PLAN | When echo_safe=true, wake word stays active during TTS; when false, suppressed | SATISFIED | `if _tts_active and not _echo_safe: continue` |
| D-10 | 13-03-PLAN | Post-TTS grace: 0.5s headset, 2.0s speaker | SATISFIED | `_echo_grace = 0.5 if _echo_safe else 2.0` in main.py |
| D-11 | 13-03-PLAN | echo_suppression.force_disabled config flag | SATISFIED | EchoSuppressionConfig.force_disabled; main.py reads via getattr |
| D-12 | 13-01-PLAN | Calibration: median of per-chunk peaks (not mean) for Bluetooth robustness | SATISFIED | `peak_levels = [int(np.abs(chunk).max()) for chunk in chunks]; noise_floor = int(np.median(peak_levels))` |
| D-13 | 13-03-PLAN | Silence threshold reads from device profile on every device switch | SATISFIED | Silence threshold re-read after devices.scan() and zombie reinit in main.py (lines 694-698, 787-791) |
| AUDIO-01 | 13-01-PLAN | Per-device profiles (noise floor, buffer size, silence threshold) auto-calibrated on first use | SATISFIED | MicProfileManager + auto-calibration in _run_loop; manual calibrate command also available |
| AUDIO-02 | 13-03-PLAN | Wake word detection stays active during TTS playback when headset detected | SATISFIED | Headset detection + echo_safe gate in main.py; test_wake_word_active_during_tts_with_headset passes |
| AUDIO-03 | 13-02-PLAN | Escape key stops TTS playback immediately (kill afplay, clear queue) | SATISFIED | herald stop command now works; stop_all() → _herald("stop") → _cmd_stop() |
| AUDIO-04 | 13-03-PLAN | Recording blocks TTS from starting; TTS holds in queue until recording finishes | SATISFIED | RECORDING_FLAG checked by orchestrator's _is_paused(); writing flag BEFORE tts_interrupt() (D-05 pitfall) |
| AUDIO-05 | 13-01-PLAN | Silence detection uses percentage-based threshold (not single-spike max) for Bluetooth robustness | IMPLEMENTED (tracking inconsistency) | main.py lines 822-826 and 843-845: `quiet_pct >= 0.85` on both paths. Code meets requirement. REQUIREMENTS.md still shows "Pending" — tracking artifact only. |

**Note on AUDIO-05:** Plan 13-01 listed AUDIO-05 in its `requirements:` frontmatter but the 13-01-SUMMARY did not list it under `requirements-completed`. REQUIREMENTS.md also still marks it "Pending". However, the code in main.py clearly implements it (85% percentage-based threshold on both the no-speech cancel path and the speech-end silence timeout path). The requirement is satisfied in code; only the requirement-tracking metadata is stale. Recommend updating REQUIREMENTS.md to mark AUDIO-05 as complete.

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| `heyvox/cli.py` | 484-497, 894-900 | Duplicate helper functions `_calibrate_open_pa` and `_calibrate_get_cache_dir` defined twice in same file | Warning | Minor — duplicate code but both copies are identical and functional; no behavioral impact |
| `tests/test_echo_suppression.py` | 171-181 | `_make_echo_safe` helper duplicates main.py logic | Info | Testing pattern — intentional for unit isolation, not a production stub |

No blockers found. The duplicate helper functions in cli.py are code smell (DRY violation) but do not affect correctness.

### Human Verification Required

#### 1. Wake Word During TTS (Headset Mode)

**Test:** Connect a Bluetooth headset (e.g. G435). Start TTS playback with `heyvox speak "hello world"`. While TTS is playing, say "Hey Jarvis".
**Expected:** Wake word fires immediately, TTS stops, recording indicator activates
**Why human:** Requires physical Bluetooth headset and live TTS playback

#### 2. Escape Key TTS Cancellation

**Test:** Start TTS playback with `heyvox speak "a long message"`. Press Escape while it plays.
**Expected:** TTS stops instantly, HUD returns to idle state, queue cleared
**Why human:** Requires live TTS playback and Escape key event — wiring to ptt.py Escape handler needs runtime confirmation

#### 3. Bluetooth Silence Detection Robustness

**Test:** With a Bluetooth mic in a moderately noisy environment, trigger a recording. Observe that occasional spikes don't end the recording prematurely.
**Expected:** Recording continues until 85% of chunks are quiet, not just one quiet chunk
**Why human:** Requires Bluetooth hardware with characteristic noise spike behavior

#### 4. Manual Calibration

**Test:** Run `heyvox calibrate` with no arguments.
**Expected:** Prints "Calibrating: [device name]", records 3 seconds, prints noise_floor and silence_threshold values, confirms save path
**Why human:** Requires real audio hardware — cannot run in CI without an actual microphone

### Gaps Summary

No gaps found. All 14 observable truths are verified against actual codebase. All 4 plan summaries report "No known stubs." All test suites pass (67 tests pass, 2 skipped for missing external files).

The only administrative issue is that AUDIO-05 is implemented but its status in REQUIREMENTS.md and the Plan 01 SUMMARY were not updated to reflect completion. This is a tracking inconsistency, not a code gap.

---

_Verified: 2026-04-13T11:10:00Z_
_Verifier: Claude (gsd-verifier)_
