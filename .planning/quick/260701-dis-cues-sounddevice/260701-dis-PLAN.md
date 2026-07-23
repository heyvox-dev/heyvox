---
phase: quick-260701-dis-cues-sounddevice
plan: 01
type: execute
wave: 1
depends_on: []
files_modified:
  - heyvox/audio/cues.py
  - tests/test_cues.py
autonomous: true
requirements: [DIS-01]
must_haves:
  truths:
    - "audio_cue() on a non-USB output device plays via sounddevice.play() with pre-loaded PCM data instead of spawning afplay"
    - "First call to a given cue file loads audio via soundfile.read() and caches the result; subsequent calls to the same cue file reuse the cached array without touching disk"
    - "If sounddevice/soundfile is unavailable or raises, audio_cue() falls back to the existing subprocess.Popen(['afplay', ...]) path with no crash"
    - "device_change_cue() behavior is unchanged (still afplay, out of scope per constraints)"
  artifacts:
    - path: "heyvox/audio/cues.py"
      provides: "_cue_cache module-level dict, _play_via_sounddevice() helper, sounddevice.play() dispatch with afplay fallback"
      contains: "_cue_cache"
    - path: "tests/test_cues.py"
      provides: "Updated TestAudioCue coverage for sounddevice success path, cache reuse, and afplay fallback"
      contains: "sounddevice"
  key_links:
    - from: "heyvox/audio/cues.py:audio_cue"
      to: "sounddevice.play"
      via: "call to _play_via_sounddevice(cue_file), which invokes sounddevice.play after cache lookup/load"
      pattern: "sounddevice\\.play\\("
    - from: "heyvox/audio/cues.py:audio_cue"
      to: "_cue_cache"
      via: "dict keyed by cue_file path, populated via soundfile.read() on cache miss"
      pattern: "_cue_cache\\[.*\\]"
---

<objective>
Replace the afplay subprocess spawn in `audio_cue()`'s non-USB output path with `sounddevice.play()` using pre-loaded, cached PCM data. Baseline measurement showed afplay spawn p99=212ms/max=237ms under system load — a spike on the wake-word audible-feedback critical path (WW_LATENCY). sounddevice.play() with cached data eliminates the process-spawn cost, targeting p99 <50ms.

Purpose: Eliminate the p99 latency spike on the wake-word audible-feedback path for non-USB output devices (Bluetooth, built-in speakers). The USB path (`play_cue_via_stream`) is already warm via a keep-alive stream and unaffected by this change.
Output: `heyvox/audio/cues.py` with a module-level cue cache and sounddevice dispatch (graceful afplay fallback preserved on any failure); `tests/test_cues.py` updated to cover the new success path, cache-hit path, and fallback path.
</objective>

<execution_context>
@$HOME/.claude/get-shit-done/workflows/execute-plan.md
@$HOME/.claude/get-shit-done/templates/summary.md
</execution_context>

<context>
@.planning/STATE.md

<interfaces>
<!-- Current audio_cue() flow in heyvox/audio/cues.py (lines 48-114) that this plan modifies. -->
<!-- Executor should edit in place -- no codebase exploration needed. -->

From heyvox/audio/cues.py (current state):
```python
def audio_cue(
    name: str,
    cues_dir: str | None = None,
    *,
    t1: float = 0.0,
    detect_ms: float = 0.0,
) -> None:
    global _cue_suppress_until
    if cues_dir is None:
        cues_dir = get_cues_dir()
    cue_file = os.path.join(cues_dir, f"{name}.aiff")
    if not os.path.exists(cue_file):
        return
    # [WW_LATENCY] t2 block -- measures trigger commit (t1) to dispatch. MUST stay
    # positioned after file-existence check, before suppression window update,
    # before any play call. Do not move this block.
    if t1 > 0.0:
        t2 = time.perf_counter()
        feedback_ms = (t2 - t1) * 1000
        total_ms = detect_ms + feedback_ms
        print(f"[WW_LATENCY] feedback={feedback_ms:.0f}ms total={total_ms:.0f}ms cue={name}", flush=True)
    duration = 1.0
    with _suppress_lock:
        _cue_suppress_until = time.time() + duration + 0.5
    # USB warm-stream path -- untouched by this plan
    try:
        from heyvox.audio.keepalive import play_cue_via_stream
        if play_cue_via_stream(name, cue_file):
            return
    except Exception:
        pass
    # THIS is the path being replaced:
    subprocess.Popen(["afplay", cue_file], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
```

Module-level state already present:
```python
signal.signal(signal.SIGCHLD, signal.SIG_IGN)  # keep -- device_change_cue() still uses afplay
_cue_suppress_until: float = 0.0
_suppress_lock = threading.Lock()
```

Dependencies already declared in pyproject.toml -- do not add new ones:
- `sounddevice>=0.4.0`
- `soundfile>=0.12`

Existing test that will break for the wrong reason if left as-is (tests/test_cues.py, TestAudioCue class):
```python
@patch("heyvox.audio.cues.subprocess.Popen")
def test_plays_existing_cue(self, mock_popen, tmp_path):
    cue_file = tmp_path / "listening.aiff"
    cue_file.touch()  # creates a 0-byte file
    audio_cue("listening", str(tmp_path))
    mock_popen.assert_called_once()
    call_args = mock_popen.call_args
    assert call_args[0][0][0] == "afplay"
    assert str(cue_file) in call_args[0][0][1]
```
This test creates an empty `.aiff` (via `.touch()`, 0 bytes) and asserts afplay is invoked. After this change, `soundfile.read()` on a 0-byte file raises, the fallback path fires, and `mock_popen.assert_called_once()` still passes -- but only because the input is invalid, not because the test exercises real fallback logic intentionally. Task 2 rewrites this into two explicit tests: one proving the sounddevice success path with valid decodable audio, one proving the afplay fallback fires on invalid/undecodable audio.
</interfaces>
</context>

<tasks>

<task type="auto" tdd="true">
  <name>Task 1: Route non-USB cue playback through sounddevice with a pre-loaded cache</name>
  <files>heyvox/audio/cues.py</files>
  <behavior>
    - Cold cache + valid audio file: first call to the helper for a given cue file calls `soundfile.read(cue_file)`, stores `(data, samplerate)` in `_cue_cache[cue_file]`, then calls `sounddevice.play(data, samplerate)`. Returns True.
    - Warm cache: second call to the helper for the same cue file does NOT call `soundfile.read()` again (cache hit), but still calls `sounddevice.play()` with the cached array. Returns True.
    - Exception during import, `soundfile.read()`, or `sounddevice.play()`: helper returns False, no exception propagates, and the cache is NOT populated for that key (so a transient failure doesn't poison future calls).
    - `audio_cue()` calls the helper first; if it returns False, falls back to the original `subprocess.Popen(["afplay", cue_file], ...)` call unchanged.
    - USB warm-stream path (`play_cue_via_stream` returns True) still returns early from `audio_cue()` before ever reaching the sounddevice helper -- unchanged behavior.
    - `device_change_cue()` is untouched -- still uses `subprocess.Popen(["afplay", ...])` unconditionally (explicitly out of scope per task constraints; not a WW_LATENCY path).
  </behavior>
  <action>
    In `heyvox/audio/cues.py`:

    1. Add a module-level cache directly below the existing `_suppress_lock = threading.Lock()` line: `_cue_cache: dict[str, tuple] = {}` (stores `(data, samplerate)` tuples keyed by the `cue_file` path string as already constructed in `audio_cue()`). Use a plain dict -- concurrent access here only risks a redundant re-read of the same file under the GIL, never corruption, so no additional lock is needed.

    2. Add a private helper function `_play_via_sounddevice(cue_file: str) -> bool` placed after `get_cues_dir()` and before `audio_cue()`:
       - On cache miss for `cue_file`: lazy-import `soundfile` inside the function body, call `soundfile.read(cue_file)` to get `(data, samplerate)`, store the tuple in `_cue_cache[cue_file]`.
       - On cache hit: read `(data, samplerate)` from `_cue_cache[cue_file]` directly, skip `soundfile.read()` entirely.
       - Lazy-import `sounddevice` inside the function body, call `sounddevice.play(data, samplerate)`.
       - Wrap the entire body in `try/except Exception`, returning `False` on any failure. On failure, do not write to `_cue_cache` (only cache after a successful `soundfile.read()`).
       - Return `True` after a successful `sounddevice.play()` call.
       - Keep both imports function-local (not module-level), matching the existing lazy-import convention already used for `from heyvox.audio.keepalive import play_cue_via_stream` in this same file -- this avoids import cost for users who never exercise this path (e.g., USB-only setups using `play_cue_via_stream` exclusively).

    3. In `audio_cue()`, replace the final `subprocess.Popen(["afplay", cue_file], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)` call with:
       - `if not _play_via_sounddevice(cue_file):` followed by the exact same `subprocess.Popen(["afplay", cue_file], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)` call as the fallback body, unchanged from today.

    4. Do NOT touch the `[WW_LATENCY]` t2 logging block, the suppression-window update (`_cue_suppress_until`), the `play_cue_via_stream` USB branch, or `device_change_cue()`. Only the final dispatch call at the bottom of `audio_cue()` changes.

    5. Do not add `sounddevice`/`soundfile` to module-level imports at the top of the file -- keep them function-local inside `_play_via_sounddevice()` per point 2.
  </action>
  <verify>
    <automated>cd /Users/work/conductor/workspaces/vox-v2/seattle && python3 -c "
import heyvox.audio.cues as cues
from unittest.mock import patch
import numpy as np

cues._cue_cache.clear()

with patch('soundfile.read', return_value=(np.zeros(100), 22050)) as mock_read, patch('sounddevice.play') as mock_play:
    ok = cues._play_via_sounddevice('/fake/path.aiff')
    assert ok is True, 'expected True on success'
    mock_read.assert_called_once()
    mock_play.assert_called_once()
    assert '/fake/path.aiff' in cues._cue_cache, 'cache not populated after successful load'

with patch('soundfile.read') as mock_read2, patch('sounddevice.play') as mock_play2:
    ok = cues._play_via_sounddevice('/fake/path.aiff')
    assert ok is True
    mock_read2.assert_not_called(), 'cache hit must not re-read from disk'
    mock_play2.assert_called_once()

cues._cue_cache.clear()
with patch('soundfile.read', side_effect=RuntimeError('bad file')):
    ok = cues._play_via_sounddevice('/fake/broken.aiff')
    assert ok is False, 'expected False on exception'
    assert '/fake/broken.aiff' not in cues._cue_cache, 'cache must not be populated on failure'

print('OK: sounddevice cache + fallback helper verified')
"
    </automated>
  </verify>
  <done>
    `_cue_cache` dict and `_play_via_sounddevice()` helper exist in `heyvox/audio/cues.py`; `audio_cue()` calls the helper before falling back to `subprocess.Popen(["afplay", ...])`; the verify script above exits 0 with no assertion errors; `play_cue_via_stream` USB branch and `device_change_cue()` are byte-for-byte unchanged (confirm via `git diff heyvox/audio/cues.py` showing no hunks touching those regions).
  </done>
</task>

<task type="auto" tdd="true">
  <name>Task 2: Update test_cues.py to cover sounddevice success, cache reuse, and afplay fallback</name>
  <files>tests/test_cues.py</files>
  <behavior>
    - `test_plays_existing_cue` (in TestAudioCue) is replaced by a test using a real minimal valid WAV fixture (not an empty `.touch()` file), asserting `sounddevice.play` is called and `subprocess.Popen` is NOT called for that scenario.
    - A new test proves cache reuse: two `audio_cue()` calls for the same cue name/file result in exactly one `soundfile.read()` call but two `sounddevice.play()` calls.
    - A new test proves the afplay fallback: when `sounddevice.play` (or `soundfile.read`) raises, `audio_cue()` still calls `subprocess.Popen(["afplay", ...])` exactly as before, with no exception escaping.
    - `test_skips_missing_cue` and `test_sets_suppression_window` continue to pass unmodified (missing-cue short-circuit and suppression-window timing are unaffected by this change).
    - `_cue_cache` is cleared in a fixture/setup so tests don't leak cached entries into each other (this file already imports `heyvox.audio.cues as cues_module` for `_cue_suppress_until` manipulation -- reuse that same module reference for `_cue_cache.clear()`).
  </behavior>
  <action>
    In `tests/test_cues.py`:

    1. Add a helper to write a minimal valid WAV file for test fixtures -- use Python's stdlib `wave` module (already used elsewhere in this codebase, e.g. `heyvox/audio/keepalive.py`) to write a tiny valid mono 16-bit PCM WAV (e.g. 100 frames of silence at 16000 Hz) to a given path. Name the cue file with a `.aiff` extension to match existing fixture naming in this test file even though the bytes are WAV-formatted PCM -- `soundfile.read()` sniffs the file header, not the extension, so this matches how `audio_cue()` constructs paths (`f"{name}.aiff"`) without needing a real AIFF encoder. If this assumption proves wrong when you run the test (soundfile requires the extension to match container format), write actual AIFF bytes via the stdlib `aifc` module instead -- verify with a quick standalone `python3 -c` check before committing to the approach, and note in the test docstring which format was used and why.

    2. Add `setup_method` (or per-test) logic that calls `cues_module._cue_cache.clear()` before each test in `TestAudioCue`, so cache state from one test never leaks into the next.

    3. Replace `test_plays_existing_cue` with `test_plays_existing_cue_via_sounddevice`:
       - Write the valid WAV/AIFF fixture from step 1 to `tmp_path / "listening.aiff"`.
       - Patch `heyvox.audio.cues.subprocess.Popen` (to assert it's NOT called) alongside real (unmocked) `soundfile.read` and a mocked `sounddevice.play` (`@patch("sounddevice.play")`) -- do not mock `soundfile.read` here since the point is to prove real decoding works against a real fixture.
       - Call `audio_cue("listening", str(tmp_path))`.
       - Assert `sounddevice.play` was called once.
       - Assert `subprocess.Popen` was NOT called (`mock_popen.assert_not_called()`).

    4. Add `test_cue_cache_reuse`:
       - Same fixture as step 3, patch `soundfile.read` with `wraps=soundfile.read` (or patch `sounddevice.play` only and spy on `soundfile.read` via `@patch("soundfile.read", wraps=soundfile.read)`) so you can assert call count while still exercising real decode logic.
       - Call `audio_cue("listening", str(tmp_path))` twice.
       - Assert `soundfile.read` was called exactly once (cache hit on second call).
       - Assert `sounddevice.play` was called exactly twice.

    5. Add `test_afplay_fallback_on_sounddevice_failure`:
       - Use the existing empty-file `.touch()` fixture pattern (0-byte file) OR patch `sounddevice.play` with `side_effect=RuntimeError("device busy")` -- prefer the explicit `side_effect` mock since it directly proves the exception-handling contract from Task 1 rather than relying on `soundfile.read` happening to fail on empty input.
       - Patch `heyvox.audio.cues.subprocess.Popen`.
       - Call `audio_cue("listening", str(tmp_path))` with the WAV/AIFF fixture from step 1 present.
       - Assert `mock_popen.assert_called_once()` and that `call_args[0][0][0] == "afplay"` (same assertions as the original test, now proving the fallback path explicitly rather than accidentally).

    6. Leave `test_skips_missing_cue` and `test_sets_suppression_window` as-is -- they test paths this plan does not change (file-not-found short-circuit, suppression timing), and adding cache-clear setup in step 2 does not alter their behavior since no cue file exists to cache in either case.
  </action>
  <verify>
    <automated>cd /Users/work/conductor/workspaces/vox-v2/seattle && python3 -m pytest tests/test_cues.py -v</automated>
  </verify>
  <done>
    `python3 -m pytest tests/test_cues.py -v` passes with all tests green, including the three new/rewritten tests (`test_plays_existing_cue_via_sounddevice`, `test_cue_cache_reuse`, `test_afplay_fallback_on_sounddevice_failure`) plus the four unmodified pre-existing tests (`test_skips_missing_cue`, `test_sets_suppression_window`, and the two `TestIsSuppressed` tests). No test relies on an empty `.touch()`-created file to accidentally exercise the fallback path.
  </done>
</task>

</tasks>

<threat_model>
## Trust Boundaries

| Boundary | Description |
|----------|--------------|
| filesystem -> soundfile.read | Cue files are shipped with the package or user-configured (`cues_dir`); not user-uploaded at runtime, but a corrupted/truncated cue file must not crash the wake-word feedback path |
| sounddevice -> CoreAudio | sounddevice.play() writes to the OS default output device; a device disconnect mid-call must not propagate an unhandled exception into `audio_cue()`'s caller (main event loop) |

## STRIDE Threat Register

| Threat ID | Category | Component | Disposition | Mitigation Plan |
|-----------|----------|-----------|-------------|------------------|
| T-quick260701-01 | Denial of Service | `_play_via_sounddevice()` | mitigate | Broad `except Exception` around the entire helper body guarantees `audio_cue()` never raises regardless of soundfile/sounddevice internal failure mode (corrupt file, device busy, device removed mid-call) -- falls back to the pre-existing afplay path, preserving current reliability floor |
| T-quick260701-02 | Denial of Service | `_cue_cache` | accept | Cache is bounded by the fixed, small set of packaged cue names (`listening`, `ok`, `paused`, `sending`, etc.) -- not user-controlled or unbounded growth; no eviction policy needed at this scale |
| T-quick260701-03 | Tampering | soundfile/sounddevice packages | accept | Both already declared as direct dependencies in `pyproject.toml` prior to this plan (`sounddevice>=0.4.0`, `soundfile>=0.12`) -- no new package install introduced by this change, so no new supply-chain surface |
</threat_model>

<verification>
1. `python3 -m pytest tests/test_cues.py -v` -- all tests pass.
2. Manual runtime check (per task constraints): restart heyvox (`launchctl kickstart -k "gui/$UID/com.heyvox.listener"` or equivalent per `reference_launchctl_kickstart` memory), trigger the wake word several times on a non-USB output device (built-in speakers or Bluetooth), and observe `[WW_LATENCY] feedback=` log lines -- p99 should drop from the 212ms baseline toward <50ms.
3. `git diff heyvox/audio/cues.py` confirms `device_change_cue()` and the USB `play_cue_via_stream` branch have zero diff hunks -- only the final dispatch line in `audio_cue()` and the new cache/helper additions changed.
</verification>

<success_criteria>
- `heyvox/audio/cues.py` uses `sounddevice.play()` with cached pre-loaded audio data for the non-USB cue path, with a verified graceful fallback to `subprocess.Popen(["afplay", ...])` on any failure.
- `tests/test_cues.py` passes and no longer relies on an accidental empty-file exception to validate the fallback path.
- No new dependencies added (sounddevice/soundfile already in `pyproject.toml`).
- `device_change_cue()` and the WW_LATENCY logging block are unmodified.
</success_criteria>

<output>
Create `.planning/quick/260701-dis-cues-sounddevice/260701-dis-SUMMARY.md` when done
</output>
