# Pitfalls Research

**Domain:** macOS voice automation tool — v1.2 polish and reliability additions
**Researched:** 2026-04-12
**Confidence:** HIGH (code inspected directly; pitfalls grounded in observed failures)

---

## Critical Pitfalls

### Pitfall 1: Injection Test Count Drift After Adding Chrome Fallback Path

**What goes wrong:**
Tests that assert an exact `mock_run.call_count` break when the code gains new subprocess calls. `test_basic_paste` already fails: it expects 2 subprocess calls (pbcopy + osascript), but the current code makes 4 because `_get_frontmost_app()` and `_save_frontmost_pid()` were added after the test was written. Any reliability fix that adds a retry, a verification call, or an additional osascript step will break count-based assertions again.

**Why it happens:**
The test patches `subprocess.run` globally and counts all calls from all helpers. Helpers added for reliability (diagnostic logging, clipboard verify, focus save/restore) all land in the same mock, making the count brittle. The test intends to verify "only one paste was sent" but actually verifies the total number of subprocess invocations.

**How to avoid:**
Replace `assert mock_run.call_count == N` with intent-based assertions: inspect `call_args_list` and verify that exactly one call contains `"keystroke"` and `"command down"`, not that call_count is a magic number. Alternatively, use separate patches for each subprocess helper (`_set_clipboard`, `_get_frontmost_app`, `_osascript_type_text`) so the main test only observes the unit under test.

**Warning signs:**
Any test that pins `call_count` to an integer is fragile. Grep for `call_count ==` in test_injection.py and test_injection_enter.py — every one of those is a mine.

**Phase to address:** Test stability phase (fix stale test failures before adding new injection logic).

---

### Pitfall 2: Clipboard Race — Another Process Overwrites Before Cmd-V Lands

**What goes wrong:**
The paste sequence is: pbcopy → (10-50ms gap) → osascript Cmd-V. If any other process writes to the clipboard in that gap (screenshot tool, another app's paste, Alfred, 1Password) the wrong content gets pasted into the agent's input. This is silent — the user sees garbage text with no error.

**Why it happens:**
macOS clipboard is a shared global resource. The existing code has a verify step (`get_clipboard_text()`) but it fires immediately after pbcopy, before the gap. By the time osascript runs, another process may have already overwritten it.

**How to avoid:**
Do not add a post-paste restore ("restore the original clipboard content") — that creates its own race window and the existing test `test_no_clipboard_restore` explicitly documents this decision. Instead: keep the pre-paste delay at 50ms minimum, log clipboard content at the moment of paste failure, and add a retry-with-backoff (max 2 retries, 100ms apart) that re-copies before each attempt. Consider using `NSPasteboard` via PyObjC directly (avoids two subprocess calls and is lower-latency than pbcopy).

**Warning signs:**
Users report "pasted something from my clipboard, not my voice command." Injection logs show "clipboard verified OK" but wrong text appears in the target app.

**Phase to address:** Paste/injection reliability phase.

---

### Pitfall 3: osascript Focus Race — Target App Not Yet Frontmost When Cmd-V Fires

**What goes wrong:**
The script does `set frontmost to true` then `delay 0.3` then `keystroke "v"`. The 0.3s delay is a hardcoded guess. On a loaded system, window manager animations, Mission Control, or another frontmost-grabbing event can mean the target app is still not frontmost when Cmd-V fires. The keystroke lands in the wrong window silently.

**Why it happens:**
osascript `set frontmost to true` is a request, not a synchronous guarantee. The script does not verify frontmost state after the delay before sending the keystroke.

**How to avoid:**
After `set frontmost to true`, poll `(frontmost of process "X" is true)` in a tight loop with a maximum timeout rather than a blind `delay`. Alternatively, use the `tell application X to activate` + separate `keystroke` approach with AX-level focus verification. The current diagnostic log (`frontmost app BEFORE/AFTER`) is good — extend it with an actual retry if AFTER != expected target.

**Warning signs:**
Injection log shows `WARNING: target was X but frontmost is Y`. This is already instrumented — it just doesn't retry yet.

**Phase to address:** Paste/injection reliability phase.

---

### Pitfall 4: Shim Removal Breaks test_flag_coordination Without Touching the Test File

**What goes wrong:**
`test_flag_coordination.py` calls `m.start_recording()`, reads `m.is_recording`, `m._audio_buffer`, and `m._triggered_by_ptt` directly as module-level attributes. When the backward-compat shims in `main.py` are removed, these tests fail with `AttributeError` — even if the actual functionality moved correctly into `RecordingStateMachine`. The failure looks like a main.py regression, not a test cleanup issue.

**Why it happens:**
The shims are annotated "Remove in Phase 9" but the tests that depend on them are in a separate file without a matching removal comment. When shim vars are deleted from main.py, the tests break because they reached through the module to private state.

**How to avoid:**
Before removing any shim, grep every test file for references to that shim name. Update tests to call the new API first, then remove the shim. Never delete the shim in the same commit as the test update — that obscures which side of the change caused a regression. The removal order must be: (1) add new test coverage for `RecordingStateMachine` API, (2) migrate `test_flag_coordination.py` to new API, (3) then delete shims.

**Warning signs:**
`AttributeError: module 'heyvox.main' has no attribute 'is_recording'` appearing in CI.

**Phase to address:** Tech debt cleanup phase (shim removal).

---

### Pitfall 5: Dual-Write Cutover — Old Flag File Becomes the Ground Truth After Crash

**What goes wrong:**
The current state system dual-writes: `update_state({"recording": True})` writes to `/tmp/heyvox-state.json` AND `open(RECORDING_FLAG, "w")` still writes the legacy flag file. The old flag file is still the primary truth for external processes (Herald hooks check `/tmp/heyvox-recording` via shell). If the cutover removes the dual-write but Herald's bash scripts still check the old path, recording coordination silently breaks — TTS plays during recording.

**Why it happens:**
The dual-write was introduced as a safe migration strategy (old flags remain functional for rollback). But Herald hooks, echo suppression, and any external shell script that checks `/tmp/heyvox-recording` directly will not automatically switch to reading `/tmp/heyvox-state.json`. The Python side may be migrated but the bash side is overlooked.

**How to avoid:**
Before removing the dual-write, grep every Herald lib/*.sh, hooks/*.sh, and modes/*.sh for the old flag paths. Create a migration checklist. The cutover must happen atomically from the shell side: replace `test -f /tmp/heyvox-recording` with a small shell function that reads `.recording` from `heyvox-state.json` using `python3 -c "import json,sys; print(json.load(open('/tmp/heyvox-state.json')).get('recording',False))"` or `jq`. Do not cut over just the Python side.

**Warning signs:**
After removing dual-write, TTS starts playing mid-dictation. Herald logs show no pause trigger when recording starts.

**Phase to address:** Tech debt cleanup phase (dual-write completion).

---

### Pitfall 6: PyPI Name Squatting — "heyvox" May Already Be Taken

**What goes wrong:**
`pyproject.toml` declares `name = "heyvox"`. If "heyvox" is already registered on PyPI (even as an abandoned package), `pip install heyvox` installs the wrong package. Users get a confusing error or, worse, silently install a placeholder. The package name check was flagged as pending ("Package name TBD") in PROJECT.md.

**Why it happens:**
Package names are registered first-come-first-served on PyPI. Common short names are frequently squatted. The project has not yet verified availability.

**How to avoid:**
Verify availability before the distribution phase: `pip install heyvox --dry-run` or check [pypi.org/project/heyvox](https://pypi.org/project/heyvox). If taken, the name must be changed in `pyproject.toml` before publishing — not after. Also check that the chosen name does not conflict with any existing Homebrew formula (`brew search heyvox`). The name change cascades to: pyproject.toml, README install instructions, launchd label, any hardcoded `heyvox` references in setup scripts.

**Warning signs:**
`pip install heyvox` installs a package that is not HeyVox. This is a hard public-facing failure once announced.

**Phase to address:** Distribution prep phase (must run before any public announcement).

---

### Pitfall 7: Non-Executable Shell Scripts in Package Data Are Silently Skipped

**What goes wrong:**
`pyproject.toml` includes `herald/bin/*` and `herald/lib/*.sh` as package data. When installed via pip/pipx, the wheel preserves file content but not Unix file permissions. Shell scripts arrive as `0644` (not executable). `herald speak` silently fails or throws `Permission denied`.

**Why it happens:**
Wheels (`.whl`) are zip archives; zip does not preserve Unix permissions in a cross-platform way. pip/wheel do preserve execute bits on POSIX if they were set in the source tree, but only if the build process captures them. setuptools with the standard backend usually preserves them, but not always — especially if the source was checked out on Windows or git config strips permissions.

**How to avoid:**
Add a post-install check in `heyvox setup` that runs `chmod +x` on all scripts in the Herald bin directory. Verify in the CI install-test workflow that `herald speak --help` exits 0 after `pip install`. Test with `pipx install .` locally and inspect permissions with `ls -l` on the installed scripts.

**Warning signs:**
`herald speak` returns `Permission denied` or `bad interpreter: No such file or directory` after a fresh pipx install. CI install-test passes but real user installs fail.

**Phase to address:** Distribution prep phase.

---

## Technical Debt Patterns

| Shortcut | Immediate Benefit | Long-term Cost | When Acceptable |
|----------|-------------------|----------------|-----------------|
| Module-level shim vars in main.py (`is_recording`, `busy`, etc.) | Tests can import without running main() | Any refactor of main.py risks breaking test state; module-level mutation makes parallelized test runs unreliable | Never: already committed to removal in Phase 9 |
| Hardcoded `delay 0.3` in osascript | Works on developer's machine | Fails on slow/loaded systems; impossible to tune per-user | Never for reliability fix: replace with polling |
| `call_count` assertions in injection tests | Simple to write | Breaks on any helper addition | Never: replace with intent-based assertions |
| Dual-write (flag file + state JSON) | Safe rollback during migration | Perpetuates two sources of truth; bash callers may not migrate | Acceptable only as a transient migration state with a hard cutover deadline |
| `osascript` for `get_clipboard_text()` (subprocess per call) | No extra dependencies | ~50ms per call; slows down the verify step | Acceptable for now; PyObjC `NSPasteboard` is the v2 upgrade path |

---

## Integration Gotchas

| Integration | Common Mistake | Correct Approach |
|-------------|----------------|------------------|
| osascript + System Events | Assuming `set frontmost to true` is synchronous | Poll frontmost state in a loop with timeout; log BEFORE/AFTER |
| macOS clipboard | Restoring clipboard after paste | Do NOT restore — Electron apps read clipboard asynchronously, restoration causes a second paste of the old content |
| PyPI publish | Publishing before name availability check | Verify name on pypi.org AND test-pypi before any CI publish step |
| Homebrew formula | Formula references GitHub release tarball before the tag exists | Create the formula only after a release tag is pushed; use a `bottle do` block for binary distribution |
| GitHub Actions macos-14 | `brew install portaudio` is slow and uncached | Cache Homebrew prefix with `actions/cache` keyed on `Brewfile.lock.json` or portaudio version |
| pipx install | Script permissions not set in source tree | `git add --chmod=+x` for all `.sh` and `bin/` files; verify with `git ls-files -s` (mode 100755 vs 100644) |

---

## Performance Traps

| Trap | Symptoms | Prevention | When It Breaks |
|------|----------|------------|----------------|
| `get_clipboard_text()` via subprocess on every paste | Adds 50ms to every injection even when verify is fast | Cache for 100ms or switch to PyObjC NSPasteboard | Every injection on slow systems |
| `_get_frontmost_app()` osascript call fires before AND after paste | Adds two extra 50ms subprocess calls to the hot path | Move diagnostic to debug-only logging; suppress in production config | Every injection on loaded system |
| Stale test mock state leaking between tests | Intermittent failures depending on test order | Use `autouse` fixtures that reset module-level state; never rely on test ordering | Any test suite with module-level state (currently: main.py shim vars) |

---

## UX Pitfalls

| Pitfall | User Impact | Better Approach |
|---------|-------------|-----------------|
| Silent paste failure (wrong content, no error) | User says a command; wrong text appears; no feedback | Log clipboard content at failure; play error audio cue; consider showing "Paste failed" in HUD |
| Menu bar icon disappears during HUD crash | User loses all status visibility | HUD process must auto-restart within 2s; heartbeat from main process triggers relaunch |
| `heyvox setup` runs post-install but not post-upgrade | Config schema changes silently break on upgrade | `heyvox start` validates config on every launch; migration prompts if schema version mismatch |

---

## "Looks Done But Isn't" Checklist

- [ ] **Paste reliability:** Verify the fix handles multi-monitor setups (focus can jump to secondary display during 0.3s delay) — not just single-monitor
- [ ] **Shim removal:** Verify `test_flag_coordination.py` passes with shims removed — it will fail until the test is migrated too
- [ ] **Dual-write cutover:** Check ALL bash scripts in `herald/lib/`, `herald/hooks/`, `herald/modes/` for `/tmp/heyvox-recording` path — Python-side cutover alone is insufficient
- [ ] **PyPI name:** Confirm `heyvox` is available on both PyPI and Test-PyPI before first publish — not just before announcement
- [ ] **Script permissions:** After `pipx install .`, run `ls -l $(pipx runpip heyvox show -f | grep herald/bin)` to confirm `+x` — not just `pip install -e .` locally
- [ ] **Test stability:** After fixing stale tests, run `pytest -p no:randomly` AND `pytest -p randomly` to verify order independence — flaky tests often hide ordering dependencies
- [ ] **CI portaudio cache:** Measure `brew install portaudio` time in CI; if >30s, add caching — uncached brew steps are a common CI slowdown
- [ ] **Distribution:** `heyvox --help` exits 0 AND `heyvox setup --check` exits 0 after clean pipx install on a machine that has never run heyvox before — not just on your dev machine

---

## Recovery Strategies

| Pitfall | Recovery Cost | Recovery Steps |
|---------|---------------|----------------|
| Injection call_count tests break after adding helper | LOW | Replace count assertion with `call_args_list` inspection; 15-minute fix per test |
| PyPI name taken | MEDIUM | Change name in pyproject.toml + README + launchd label + all install docs; ~2 hours but no code changes |
| Dual-write cutover breaks Herald coordination | HIGH | Re-add flag file write as emergency rollback (one-line change); then fix bash scripts before re-cutting over |
| Script permissions lost in wheel | LOW | Add `chmod +x` to `heyvox setup`; publish patch release; existing installs self-fix on next `heyvox setup` run |
| Shim removal breaks test_flag_coordination | LOW | Revert shim deletion; migrate tests first; then re-delete |

---

## Pitfall-to-Phase Mapping

| Pitfall | Prevention Phase | Verification |
|---------|------------------|--------------|
| Injection test count drift (P1) | Test stability phase — fix stale failures FIRST before adding injection logic | All 6 stale tests pass; no `call_count ==` assertions remain |
| Clipboard race (P2) | Paste/injection reliability phase | Integration test: paste during clipboard activity from another process |
| osascript focus race (P3) | Paste/injection reliability phase | Manual test on loaded system with animations enabled; log shows AFTER == target |
| Shim removal breaks tests (P4) | Tech debt cleanup phase — migrate tests before deleting shims | `pytest tests/test_flag_coordination.py` passes with shim vars removed from main.py |
| Dual-write cutover skips bash (P5) | Tech debt cleanup phase — grep all shell scripts before cutting over | Record session with dual-write removed; TTS does not play during dictation |
| PyPI name squatting (P6) | Distribution prep phase — name check is the first task | `pip install heyvox` installs correct package; check pypi.org/project/heyvox |
| Script permissions lost (P7) | Distribution prep phase — verify after `pipx install` on clean machine | `herald speak --help` exits 0 after pipx install from wheel |

---

## Sources

- Direct code inspection: `heyvox/input/injection.py`, `heyvox/ipc/state.py`, `heyvox/main.py`
- Direct test inspection: `tests/test_injection.py`, `tests/test_flag_coordination.py`, `tests/test_e2e.py`, `tests/test_media.py`
- Observed test failure: `test_injection.py::TestTypeText::test_basic_paste` (call_count == 4, expected 2) — confirmed in live test run
- PROJECT.md known issues: "Package name TBD", "6 stale test failures", "tts_playing dual-write incomplete"
- `pyproject.toml`: package data includes shell scripts; build backend is setuptools (permissions preserved but conditionally)
- Memory files: `bug_paste_injection_slow.md` (paste is #1 UX pain point), `project_config_splitbrain.md`, `project_monorepo_stale_flags.md`

---
*Pitfalls research for: HeyVox v1.2 — Polish & Reliability*
*Researched: 2026-04-12*
