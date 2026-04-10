# Codebase Concerns

**Analysis Date:** 2026-04-10

## Tech Debt

**main.py is a 1995-line monolith:**
- Issue: `heyvox/main.py` contains the entire main event loop, recording flow, mic recovery, device hotplug, silence watchdog, memory watchdog, HUD client management, wake word stripping, garbled-text filtering, and debug audio saving -- all in a single file with 17+ `global` statements.
- Files: `heyvox/main.py`
- Impact: Extremely difficult to test individual subsystems. Any change risks regretting behavior in unrelated code paths. The `main()` function alone spans ~900 lines.
- Fix approach: Extract into focused modules: `heyvox/recording.py` (start/stop/send_local), `heyvox/watchdog.py` (memory, heartbeat, dead mic), `heyvox/device_manager.py` (hotplug, mic switch, device scan). Pass state via a shared context object instead of globals.
- Severity: **high**

**Excessive /tmp flag-file IPC:**
- Issue: Cross-process coordination relies on ~20+ distinct `/tmp/` flag files (151 `/tmp/` references across 30 files). Examples: `/tmp/heyvox-recording`, `/tmp/herald-mute`, `/tmp/claude-tts-mute`, `/tmp/heyvox-media-paused-rec`, `/tmp/herald-media-paused-*`, `/tmp/heyvox-verbosity`, `/tmp/heyvox-tts-style`, `/tmp/heyvox-active-mic`, `/tmp/heyvox-mic-switch`, `/tmp/herald-playing.pid`, etc.
- Files: `heyvox/constants.py`, `heyvox/audio/tts.py`, `heyvox/audio/media.py`, `heyvox/main.py`, `heyvox/herald/lib/config.sh`
- Impact: Flag files can go stale after crashes (startup already has extensive cleanup code), race conditions between Python and bash readers/writers, no atomic read-modify-write semantics, naming inconsistency between `heyvox-*` and `herald-*` and `claude-*` namespaces.
- Fix approach: Consolidate to a single state file or lightweight IPC mechanism (e.g., a shared SQLite WAL-mode database at `/tmp/heyvox-state.db` or a Unix socket state server). At minimum, centralize all flag file paths in `constants.py` and add a `cleanup_all_flags()` function.
- Severity: **high**

**Duplicate mute flag sets:**
- Issue: `set_muted()` and `set_verbosity()` both manage `_MUTE_FLAGS = ["/tmp/claude-tts-mute", "/tmp/herald-mute"]` with identical logic, defined as local variables in each function.
- Files: `heyvox/audio/tts.py:207`, `heyvox/audio/tts.py:266`
- Impact: If a new mute flag path is added, both locations must be updated. Easy to miss.
- Fix approach: Define `_MUTE_FLAGS` as a module-level constant and share between both functions.
- Severity: **low**

**Legacy backward-compat code paths:**
- Issue: `Verbosity.SUMMARY` exists only for "backward compat -- treated as FULL". `execute_voice_command` accepts a `tts_script_path` parameter that is never meaningfully used. `_show_recording_indicator()` is a no-op kept for "call-site compatibility".
- Files: `heyvox/audio/tts.py:38`, `heyvox/audio/tts.py:375`, `heyvox/main.py:464-471`
- Impact: Dead code that confuses new readers. Low risk but adds noise.
- Fix approach: Remove `SUMMARY` enum variant, remove `tts_script_path` parameter, remove `_show_recording_indicator()`.
- Severity: **low**

**Module-level `import re` inside function body:**
- Issue: `_is_garbled()` and `_strip_wake_words()` do `import re` inside the function body despite `re` being a stdlib module with negligible import cost.
- Files: `heyvox/main.py:199`, `heyvox/main.py:259`
- Impact: Minor style inconsistency. No performance impact.
- Fix approach: Move to top-level imports.
- Severity: **low**

## Security Considerations

**osascript shell injection surface:**
- Risk: Text injection uses `osascript -e` with string interpolation for `app_name`. While there is basic escaping (`safe_name = app_name.replace('\\', '\\\\').replace('"', '\\"')`), AppleScript has additional escaping edge cases (e.g., backslash sequences, nested quotes in bundle names).
- Files: `heyvox/input/injection.py:159`, `heyvox/input/injection.py:199`, `heyvox/input/injection.py:264`, `heyvox/input/injection.py:277`
- Current mitigation: `app_name` comes from macOS APIs (NSWorkspace/CGWindowList), not user input. Double-quote and backslash escaping is applied.
- Recommendations: Consider using PyObjC's `NSAppleScript` API directly instead of shelling out to `osascript`, which eliminates the escaping problem entirely.
- Severity: **medium**

**Clipboard contents exposed during paste:**
- Risk: `type_text()` writes the transcribed text to the system clipboard via `pbcopy`, then pastes with Cmd+V. This overwrites whatever was on the clipboard before and leaves the transcription on the clipboard after.
- Files: `heyvox/input/injection.py:96-107`, `heyvox/input/injection.py:133-180`
- Current mitigation: None. The original clipboard content is lost.
- Recommendations: Save clipboard contents before paste, restore after. Or use Accessibility API `AXValue` setting to insert text without clipboard.
- Severity: **medium**

**WebSocket bridge binds to localhost without auth:**
- Risk: The Chrome bridge WebSocket server on `127.0.0.1:9285` has no authentication. Any local process can connect and send pause/play commands.
- Files: `heyvox/chrome/bridge.py:29-30`
- Current mitigation: Localhost-only binding prevents remote access.
- Recommendations: For a personal tool this is acceptable. For distribution, add a shared secret handshake or origin verification.
- Severity: **low**

**MediaRemote private framework usage:**
- Risk: Uses Apple's private `MediaRemote.framework` via ctypes for media control. This framework is undocumented and may change between macOS versions.
- Files: `heyvox/audio/media.py:126-135`
- Current mitigation: Wrapped in try/except with graceful fallback.
- Recommendations: Pin tested macOS versions in CI. Consider using `nowplaying-cli` as the primary path (it wraps MediaRemote) with media key as fallback.
- Severity: **medium**

## Reliability Risks

**Race condition between recording state and transcription thread:**
- Problem: `stop_recording()` captures `_audio_buffer` and `_triggered_by_ptt` under `_state_lock`, then spawns a daemon thread (`_send_local`) that runs the full STT-to-injection pipeline. During this time, `busy = True` prevents new recordings, but the `busy` flag is only reset in `_send_local`'s `finally` block. If the thread dies unexpectedly (e.g., unhandled exception in an import), `busy` stays True forever.
- Files: `heyvox/main.py:570-680`, `heyvox/main.py:794-1014`
- Cause: `_send_local` runs on a daemon thread with a broad try/except, but daemon threads can be silently killed on process exit, and some exceptions (e.g., `SystemExit` from a library) bypass the catch.
- Improvement path: Add a `_BUSY_TIMEOUT` watchdog (the variable exists at line 69 but is only checked in main loop for explicit timeout -- verify it covers all cases). Consider using `concurrent.futures.Future` instead of raw threads so failures propagate.
- Severity: **high**

**ThreadPoolExecutor leak in STT transcription:**
- Problem: Each `transcribe_audio()` call creates a new `ThreadPoolExecutor(max_workers=1)`. On timeout, `executor.shutdown(wait=False, cancel_futures=True)` is called, but the orphaned thread may continue running. Over time with repeated timeouts, these zombie threads accumulate.
- Files: `heyvox/audio/stt.py:234-247`, `heyvox/audio/stt.py:270-283`
- Cause: Python's `ThreadPoolExecutor` cannot truly cancel running threads. `cancel_futures=True` only cancels queued (not running) futures.
- Improvement path: Use a single shared executor (module-level) with a bounded pool. On timeout, log a warning but don't create new executors. Alternatively, use `multiprocessing` for true cancellation.
- Severity: **medium**

**MLX model unload via importlib.reload() is fragile:**
- Problem: `_unload_mlx_model()` attempts to free GPU memory by calling `importlib.reload(mlx_whisper)` and `gc.collect()`. This is a hack -- `importlib.reload` doesn't guarantee module-level caches are cleared, and the reloaded module may have stale references.
- Files: `heyvox/audio/stt.py:86-98`
- Cause: mlx_whisper caches the model internally with no public unload API.
- Improvement path: Track actual memory usage before/after unload. If memory doesn't decrease, fall back to subprocess-based STT (run transcription in a child process that exits after use).
- Severity: **medium**

**Stale HUD socket can block startup:**
- Problem: The HUD overlay creates a Unix socket at `/tmp/heyvox-hud.sock`. If the overlay is killed without cleanup, the stale socket file remains. The new overlay's `bind()` call will fail with `Address already in use`.
- Files: `heyvox/hud/ipc.py`, `heyvox/main.py:1066-1079`
- Current mitigation: `_acquire_singleton()` cleans up `/tmp/heyvox-hud.sock` on startup.
- Improvement path: The cleanup works but relies on the specific file being listed in the glob patterns. A missed socket path would cause startup failure. Centralize all cleanup paths.
- Severity: **low**

**PyAudio terminate/reinit is not thread-safe:**
- Problem: The main loop calls `pa.terminate()` followed by `pa = pyaudio.PyAudio()` during mic recovery. If a concurrent thread (e.g., `_send_local` or media pause) references the old `pa` instance, it will crash.
- Files: `heyvox/main.py:1441-1443`, `heyvox/main.py:1483-1485`, `heyvox/main.py:1704-1705`
- Current mitigation: Recovery only happens when `not _is_rec and not _is_busy`, but threads spawned earlier may still be running.
- Improvement path: Wrap PyAudio in a manager class with a lock. Or use a dedicated audio thread that owns the PyAudio instance.
- Severity: **medium**

## Performance Bottlenecks

**osascript subprocess calls block the main loop:**
- Problem: `is_muted()` calls `_is_system_muted()` which spawns `osascript` with a 2-second timeout. This is called on every `speak()` invocation. Additionally, `pause_media()` can chain multiple osascript calls (JS test + video state + video control = up to 6 seconds).
- Files: `heyvox/audio/tts.py:227-234`, `heyvox/audio/media.py:159-212`, `heyvox/audio/media.py:248-307`
- Cause: AppleScript execution is inherently slow (~100-500ms per call).
- Improvement path: Cache `_is_system_muted()` result for 5 seconds. Run `pause_media()` in background thread (already done for recording path, but not for TTS path). Consider replacing osascript calls with PyObjC equivalents where possible.
- Severity: **medium**

**Device hotplug scan creates/destroys PyAudio every 3 seconds:**
- Problem: Every 3 seconds, the main loop creates a temporary `pyaudio.PyAudio()` instance to scan for new devices, then immediately terminates it. PyAudio initialization opens PortAudio which queries all audio drivers.
- Files: `heyvox/main.py:1666-1678`
- Cause: PortAudio caches device lists, so a new instance is needed to detect hotplug.
- Improvement path: Increase scan interval to 5-10 seconds. Or use CoreAudio property listeners (`kAudioHardwarePropertyDevices` with `AudioObjectAddPropertyListener`) for event-driven hotplug detection instead of polling.
- Severity: **low**

**Kokoro daemon pre-warms 4 voices on every startup:**
- Problem: `load_model_mlx()` generates warmup audio for 4 voices (`af_sarah`, `af_heart`, `af_nova`, `af_sky`). Each warmup compiles the MLX compute graph, adding ~2-5 seconds per voice to startup time.
- Files: `heyvox/herald/daemon/kokoro-daemon.py:88-93`
- Cause: MLX requires graph compilation on first use per voice.
- Improvement path: Warm only the default voice on startup. Lazy-warm others on first use. Cache compiled graphs if MLX supports it.
- Severity: **low**

## Fragile Areas

**Wake word stripping logic:**
- Files: `heyvox/main.py:152-314`
- Why fragile: The wake word phrase list (`_WAKE_WORD_PHRASES`) is manually maintained with 40+ Whisper misheard variants. The fuzzy regex fallback (`r'^[Hh]ey[,.]?\s+\w{2,8}(\s+\w{2,5})?\s*[.,!?]*\s*'`) is aggressive and can eat legitimate user words (documented in `bug_stt_trimming.md` and `bug_stt_end_trimming.md`).
- Safe modification: Add new phrases to the explicit list; do NOT widen the regex. Test with the debug audio pipeline (`/tmp/heyvox-debug/`).
- Test coverage: No automated tests for wake word stripping. Only validated through manual testing.
- Severity: **high**

**Audio trim timing constants:**
- Files: `heyvox/main.py:647-650`
- Why fragile: Hardcoded `ww_start_trim_secs = 1.5` and `ww_end_trim_secs = 0.5` determine how much audio is cut from the beginning/end of recordings. These depend on preroll buffer size (500ms), wake word model response time, and audio cue duration. Changing any of these without updating the trim constants will either clip user speech or leave wake word audio in the transcription.
- Safe modification: Document the dependency chain. Consider auto-calibrating trim length based on detected wake word position in the audio.
- Test coverage: No automated tests.
- Severity: **medium**

**Conductor workspace detection via AX tree walking:**
- Files: `heyvox/input/target.py:67-167`
- Why fragile: Depends on Conductor's exact AX tree structure (first `AXStaticText` after first `AXSplitter` = branch name). Any Conductor UI update that changes the tree structure will silently break workspace detection.
- Safe modification: When modifying, dump the AX tree first to verify the structure. Add a fallback to the DB-based detection if AX tree walk returns empty.
- Test coverage: No automated tests. Manual verification only.
- Severity: **medium**

**Herald bash/Python boundary:**
- Files: `heyvox/herald/lib/worker.sh`, `heyvox/herald/lib/orchestrator.sh`, `heyvox/herald/daemon/kokoro-daemon.py`
- Why fragile: The TTS pipeline crosses Python -> bash -> Python boundaries: `tts.py` calls `herald speak` (bash) which runs `worker.sh` (bash) which calls inline Python for text extraction, then sends JSON to `kokoro-daemon.py` (Python) via Unix socket. State is passed via environment variables, file flags, and temporary files. Debugging requires correlating logs across 4 processes.
- Safe modification: Test changes by running `herald speak` manually and checking `/tmp/herald-debug.log`. Grep for `TIMING:` lines to verify pipeline latency.
- Test coverage: No automated tests.
- Severity: **high**

## Scaling Limits

**Hold queue has no enforced cap:**
- Problem: When messages arrive for inactive Conductor workspaces, they're held in `/tmp/herald-hold/`. The CLAUDE.md mentions "Hold queue cap enforcement" as pending. Without a cap, a chatty agent in an inactive workspace could fill `/tmp` with WAV files.
- Files: `heyvox/herald/lib/orchestrator.sh`, `heyvox/constants.py:36` (TTS_MAX_HELD = 5 defined but only for in-process queue)
- Current capacity: Unbounded on disk. Each WAV is ~50-200KB.
- Limit: `/tmp` partition size (typically RAM-backed on macOS = half of physical memory).
- Scaling path: Implement the cap in `orchestrator.sh` -- count files in hold dir, delete oldest when count exceeds `TTS_MAX_HELD`.
- Severity: **medium**

**Memory growth from MLX Whisper + Kokoro coexistence:**
- Problem: When both STT (MLX Whisper, ~855MB) and TTS (Kokoro MLX, ~400MB) models are loaded, unified memory usage exceeds 1.2GB. The watchdog triggers restart at 1GB RSS.
- Files: `heyvox/main.py:1620-1645`, `heyvox/audio/stt.py:37` (`_mlx_unload_secs: float = 120.0`)
- Current capacity: Works on 16GB+ Macs. On 8GB Macs, the watchdog will trigger frequent restarts.
- Scaling path: The STT idle unload (2min timeout) helps. Consider reducing the timeout or adding coordination: unload STT immediately when TTS starts, and vice versa.
- Severity: **medium**

**Pre-roll buffer grows with chunk size changes:**
- Problem: `_PREROLL_CHUNKS = max(1, int(0.5 * sample_rate / chunk_size))` calculates buffer size based on sample rate and chunk size. The buffer stores raw numpy arrays. At 16kHz/1280 chunk size, this is ~6 chunks (~15KB). Not an issue now, but if sample rate or buffer duration is increased, memory grows linearly.
- Files: `heyvox/main.py:1344`
- Current capacity: ~15KB. Negligible.
- Limit: Only a concern if preroll duration is increased significantly.
- Severity: **low**

## Dependencies at Risk

**PyAudio (portaudio wrapper):**
- Risk: PyAudio is effectively unmaintained (last release 2017, but builds from source). The project depends on it for all mic I/O. Apple Silicon builds require manual portaudio compilation or homebrew.
- Impact: Installation failures for new users. No ARM64 wheels on PyPI.
- Migration plan: `sounddevice` (based on libsoundfile) is the modern alternative. Would require rewriting `mic.py` and `open_mic_stream()`.
- Severity: **medium**

**openwakeword:**
- Risk: Small community project. Custom wake word training requires careful negative data curation (documented in `project_wakeword_training.md` -- v1 failed with false activations).
- Impact: Wake word accuracy is core to the product. A regression in openwakeword would directly affect UX.
- Migration plan: None obvious. Could add Picovoice Porcupine as a fallback but it requires a license key.
- Severity: **medium**

## Missing Critical Features

**No automated test suite:**
- Problem: Zero test files exist in the repository. All validation is manual. For a system with complex state machines (recording flow, mic recovery, echo suppression, media pause/resume), this is the single biggest reliability risk.
- Blocks: Refactoring main.py, updating wake word stripping, changing audio trim constants, modifying the Herald pipeline -- all carry regression risk.
- Severity: **critical**

**No graceful degradation when Accessibility permissions are missing:**
- Problem: Push-to-talk (`ptt.py`) requires Accessibility permission for CGEventTap. Target snapshot (`target.py`) requires it for AXUIElement. If permission is denied, PTT silently fails to register, and text injection falls back to frontmost-app guessing.
- Files: `heyvox/input/ptt.py:150-152`, `heyvox/input/target.py:243-245`
- Current behavior: Warning logged, but user may not see logs.
- Improvement: Surface permission status in HUD menu bar. Show a one-time notification on first failure.
- Severity: **medium**

**No crash recovery for Kokoro daemon:**
- Problem: If `kokoro-daemon.py` crashes or hangs, TTS generation fails silently. The worker.sh retries the socket connection but has no daemon restart logic.
- Files: `heyvox/herald/daemon/kokoro-daemon.py`, `heyvox/herald/lib/worker.sh:29-37`
- Current behavior: Worker starts orchestrator if dead, but not the daemon.
- Improvement: Add health check ping to daemon socket. Auto-restart daemon if it fails to respond within 5 seconds.
- Severity: **medium**

## Test Coverage Gaps

**Everything:**
- What's not tested: The entire codebase has zero automated tests.
- Files: All files under `heyvox/`
- Risk: Any code change can introduce regressions that are only caught through manual use. The complex state interactions (recording + mic recovery + echo suppression + media control + HUD IPC) are especially prone to edge-case bugs.
- Priority: **Critical** -- this is the #1 concern. Start with:
  1. Unit tests for pure functions: `_is_garbled()`, `_strip_wake_words()`, `apply_verbosity()`, `check_voice_command()`, `filter_tts_echo()`, `detect_mood()` (in worker.sh inline Python)
  2. Integration tests for mic selection: mock PyAudio device list, verify `find_best_mic()` priority logic
  3. State machine tests: recording start/stop transitions, busy flag lifecycle

---

*Concerns audit: 2026-04-10*
