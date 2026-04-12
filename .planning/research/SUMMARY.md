# Project Research Summary

**Project:** HeyVox v1.2 — Polish & Reliability
**Domain:** macOS voice layer for AI coding agents (subsequent milestone)
**Researched:** 2026-04-12
**Confidence:** HIGH

## Executive Summary

HeyVox v1.2 is a polish-and-reliability milestone on a fully-shipped v1.1 product. All primary features (wake word, PTT, STT, HUD, Herald TTS, Hush media control, MCP server, CLI, launchd) exist and work. The v1.2 scope is narrowly defined: fix the #1 UX pain point (paste injection destroying the clipboard and silently failing in Electron apps), stabilize a CI test suite broken by 6 stale failures, clean up tech debt introduced during v1.1 decomposition, and ship distribution packaging (PyPI + Homebrew tap). No new architectural components are needed; the work is refinement, test hygiene, and packaging.

The recommended execution order is dependency-driven: fix tests first (CI must be green before adding code), then remove tech debt shim vars (clean state before modifying logic), then complete the tts_playing dual-write (IPC consolidation), then improve paste injection (the production UX fix), then distribution prep. All four research areas converge on the same ordering independently — architecture, features, and pitfalls all flag "fix tests before touching injection" as non-negotiable. The single biggest decision-gate is PyPI name verification: `heyvox` shows a "coming soon" placeholder on PyPI with no listed author, which may be a squatter registration. This must be resolved before any distribution work starts.

The main risks in v1.2 are subtle and integration-level: (1) test count-based assertions will break again the moment any subprocess call is added to the injection path — the fix must be structural (intent-based assertions), not numeric; (2) the dual-write cutover must cover bash shell scripts in Herald, not just Python code — a Python-only cutover will silently break TTS-during-recording echo suppression; (3) restoring the clipboard after paste is a known anti-pattern (Electron reads clipboard asynchronously, causing a second paste of the old content) — the existing test `test_no_clipboard_restore` documents this explicitly. All three risks are avoidable with careful sequencing.

---

## Key Findings

### Recommended Stack

The existing stack is validated and unchanged. v1.2 adds exactly two dev dependencies (`pytest-mock>=3.15`, `pytest-subprocess>=1.5`) and updates one runtime constraint (`websockets>=14.0` to lock to the new asyncio API). No new runtime libraries are required. The paste reliability fix uses `NSPasteboard` via the already-installed `pyobjc-framework-Cocoa` — replacing the `pbcopy` subprocess call eliminates one subprocess, reduces test mock complexity, and is faster.

**Stack additions (v1.2 only):**
- `pytest-mock>=3.15`: cleaner `mocker` fixture injection — replaces brittle `@patch` stacking in injection and media tests
- `pytest-subprocess>=1.5`: subprocess call assertions by command pattern — fixes count-drift problem without numeric pinning
- `websockets>=14.0` (runtime, already installed at 15.0.1): migration from deprecated `websockets.server.serve` to `websockets.asyncio.server.serve` — 2-line change in `chrome/bridge.py`
- `asyncio_mode = "auto"` in `[tool.pytest.ini_options]`: suppresses per-test asyncio marker warnings with zero code changes

**Distribution tooling (already installed, add to dev deps):**
- `build>=1.4` + `twine>=6.2`: standard PyPA wheel build + upload pipeline
- `pypa/gh-action-pypi-publish` GitHub Action: OIDC trusted publishing (no API token to rotate)
- `homebrew-pypi-poet`: one-time authoring tool to generate Homebrew resource stanzas (not a project dep)

### Expected Features

Research confirms the v1.2 feature scope is correct and well-prioritized. All P1 items are low-complexity with high user impact. The AXUIElement fast-path for native apps is confirmed as a useful differentiator but correctly deferred to a v1.x patch — the clipboard path must be bulletproof before layering a fast-path on top.

**Must have (table stakes — v1.2):**
- Clipboard contents preserved across injection (save before, restore timing is critical — after CGEvent flush, not immediately)
- 50-100ms settle delay before Cmd+V for Electron apps (Claude Code, Cursor, VS Code all affected)
- Menu bar status text: idle/Recording.../Transcribing... labels via `NSStatusItem.variableLength`
- First menu item = disabled status string (universal macOS menu bar utility pattern)
- 6 stale CI test failures fixed (audio mock in conftest.py + intent-based injection assertions)
- Package name verified and `pipx install <name>` works on a fresh macOS machine
- Homebrew tap formula (`brew tap heyvox/tap && brew install heyvox`)
- HUD visual QA pass (Retina, dark mode, external monitor)
- 7 shim vars in `main.py` removed
- `tts_playing` dual-write consolidated to single state file source

**Should have (v1.x patch):**
- AXUIElement fast-path for native AppKit apps (~0.72ms vs ~150ms clipboard round-trip)
- `heyvox update` CLI command delegating to `pipx upgrade` or `brew upgrade`
- "Open Config" menu item (`NSWorkspace.openFile()` on config path)

**Defer (v2+):**
- Full preferences window (AppKit/SwiftUI) — significant effort, YAML config sufficient for CLI tool
- Auto-update via Sparkle — requires bundled .app, not CLI
- homebrew-core submission — requires significant install count
- Per-app injection method configuration UI — auto-detection handles 95% of cases

**Confirmed anti-feature:** Do NOT restore clipboard content after paste. Electron reads clipboard asynchronously — a restore causes a second paste of the old content. The existing `test_no_clipboard_restore` test documents this decision and must not be changed.

### Architecture Approach

The v1.1 decomposition split the 2000-line `main.py` monolith into focused modules (`device_manager.py`, `recording.py`, `app_context.py`, `wakeword.py`). v1.2 completes this decomposition by removing the 7 backward-compat shim vars from `main.py` that bridge the old module-level state to the new `AppContext`. The IPC surface is stable (13 `/tmp/` flag files + 3 Unix sockets) with one outstanding consolidation: `tts_playing` still has dual-write with the old flag file as primary. The architecture is correct; the task is finishing the migration.

**Major components and v1.2 touch points:**
1. `heyvox/input/injection.py` — paste reliability fix: NSPasteboard clipboard write, configurable per-app focus delay, retry on focus steal, intent-based test assertions
2. `heyvox/main.py` — shim var removal (7 module-level globals syncing to AppContext)
3. `heyvox/audio/tts.py` + `herald/orchestrator.py` — complete `tts_playing` dual-write to state file
4. `tests/test_injection.py` + `tests/test_media.py` — fix 4 confirmed stale failures (wrong call counts, `_browser_has_video_tab` renamed)
5. `chrome/bridge.py` — 2-line websockets API migration
6. `pyproject.toml` — add dev deps, update `websockets` pin, add `asyncio_mode = auto`
7. New `Formula/heyvox.rb` — Homebrew tap formula (in separate `homebrew-heyvox` repo)

**Build order mandated by architecture:**
Test fixes → shim removal → dual-write completion → paste injection → distribution prep

### Critical Pitfalls

1. **Injection test count drift** — `assert mock_run.call_count == N` breaks the moment any subprocess call is added. Replace with `call_args_list` inspection: assert that exactly one call contains `"keystroke"` and `"command down"`. Mock `_get_frontmost_app` separately. The structural fix prevents recurrence; numeric fixes break again.

2. **Clipboard race condition** — Another process (Alfred, 1Password, screenshot) can overwrite the clipboard in the 10-50ms gap between `pbcopy` and `osascript Cmd-V`. Use NSPasteboard directly (faster), keep pre-paste delay at 50ms minimum, add retry-with-backoff (max 2 retries, 100ms apart). Never restore clipboard after paste — Electron reads it asynchronously and will paste the restored old content.

3. **osascript focus race** — `set frontmost to true` is a request, not a guarantee. A 0.3s hardcoded blind delay fails on loaded systems, during Mission Control, or with multi-monitor setups. Fix: poll `(frontmost of process "X" is true)` in a tight loop with timeout rather than blind `delay`. The WARNING log is already instrumented — add retry when AFTER != target.

4. **Dual-write cutover must cover bash** — Herald's `lib/*.sh`, `hooks/*.sh`, and `modes/*.sh` check `/tmp/heyvox-recording` directly via `test -f`. A Python-only cutover leaves shell scripts reading stale flags. Grep all shell scripts before removing dual-write. Keep old flag as parallel write throughout v1.2; remove in v1.3.

5. **Shim removal breaks tests without touching them** — `test_flag_coordination.py` reads `heyvox.main.is_recording`, `._audio_buffer`, `._triggered_by_ptt` as module attributes. Migrate tests to `AppContext` API BEFORE deleting shims. Never delete shim and update test in the same commit.

6. **PyPI name squatting** — `heyvox` on PyPI shows "Voice coding on macOS — coming soon" with no listed author. Verify ownership via `pip install heyvox --dry-run` and check pypi.org before any distribution work. If squatted, name change cascades to `pyproject.toml`, README, launchd label, all install docs.

7. **Script permissions lost in wheel** — Herald `bin/` and `lib/*.sh` must be tracked as `100755` in git (`git add --chmod=+x`). After `pipx install .`, verify `herald speak --help` exits 0. Add `chmod +x` to `heyvox setup` as a safety net.

---

## Implications for Roadmap

Based on combined research, the v1.2 milestone naturally organizes into four phases with hard sequencing dependencies.

### Phase 1: Test Stability & CI Green

**Rationale:** All other phases add or modify code. A broken test suite masks regressions. This phase has zero production risk — it only touches tests and test infrastructure. Architecture research confirmed 4 failures with root causes; pitfalls research confirmed the structural fix to prevent recurrence.

**Delivers:** Green CI on every PR, reliable signal for subsequent phases, no more `call_count` fragility

**Addresses:**
- Mock audio devices at module import level in `conftest.py` (fixes CI macos-14 no-audio-device failures)
- Replace `call_count == N` assertions in `test_injection.py` with `call_args_list` inspection (structural fix that won't drift)
- Mock `_get_frontmost_app` separately in injection tests
- Fix `test_media.py` patches: `_browser_has_video_tab` → current function name (grep to confirm)
- Add `@pytest.mark.requires_audio` marker + skip in CI addopts
- Set `asyncio_mode = "auto"` in `pyproject.toml`
- Add `pytest-mock>=3.15` and `pytest-subprocess>=1.5` to dev deps

**Avoids:** Pitfall 1 (injection test count drift), stale test masking regressions

**Research flag:** Standard patterns — no deeper research needed. Root causes confirmed via direct test execution.

---

### Phase 2: Tech Debt Cleanup

**Rationale:** Shim var removal and dual-write completion are prerequisite housekeeping before modifying injection logic. Removing shims clears the two-sources-of-truth problem in AppContext. Both are medium-risk (dual-write) to low-risk (shim vars) changes that are much safer with a green test suite from Phase 1.

**Delivers:** Single source of truth for all shared state, no more shim sync code in main loop, `tts_playing` fully consolidated to state file (Python side)

**Addresses:**
- Grep `test_flag_coordination.py` for `heyvox.main.<shim>` imports; migrate to `AppContext` API first
- Remove 7 shim vars from `main.py` + their sync code in main loop
- Complete `tts_playing` writes in `tts.py` and `herald/orchestrator.py` (keep old flag as parallel write — bash migration is v1.3)
- WebSocket bridge: 2-line change in `chrome/bridge.py` to `websockets.asyncio.server`
- Pin `websockets>=14.0` in `pyproject.toml`

**Avoids:** Pitfall 4 (dual-write bash cutover — Python side only, not bash), Pitfall 5 (shim removal breaks test_flag_coordination — tests migrated first, shims deleted second)

**Research flag:** Standard patterns. Architecture research provides exact file list, line numbers, and migration order.

---

### Phase 3: Paste Injection Reliability

**Rationale:** The #1 UX pain point. Implementing after Phases 1-2 means the test suite is green and codebase is clean — changes can be verified and regressions caught immediately. Implementing before Phase 1 would mean fixing tests and modifying the code under test simultaneously, obscuring causation.

**Delivers:** Clipboard preserved across injection, reliable paste in Electron apps, audible failure feedback instead of silent wrong-content paste

**Addresses:**
- Replace `pbcopy` subprocess with `NSPasteboard.generalPasteboard().setString_forType_()` (existing dep, faster, one less subprocess)
- 50-100ms configurable settle delay before Cmd+V (config key: `injection.focus_delay_ms`, default 300ms)
- Focus polling loop after `set frontmost to true` instead of blind `delay 0.3`
- Retry up to 2x with 100ms backoff if focus stolen during injection window
- Error audio cue on paste failure (existing afplay infrastructure)
- Log clipboard content at failure point for diagnostics
- Do NOT add clipboard restore — explicitly prohibited (Electron async read anti-pattern)

**Avoids:** Pitfall 2 (clipboard race), Pitfall 3 (focus race), the clipboard-restore anti-pattern

**Research flag:** No deeper research needed. Competitor pattern analysis complete. Timing values (50-100ms) may need empirical tuning on specific Electron apps — validate with Claude Code and Cursor before finalizing.

---

### Phase 4: Distribution Prep

**Rationale:** Last because it depends on a working, installable package. Name verification is the first task of this phase — it gates all other distribution work. Distribution before paste is fixed would mean announcing a product with the #1 UX pain point unfixed.

**Delivers:** `pipx install heyvox` on a fresh macOS machine, `brew tap heyvox/tap && brew install heyvox`, CI publish pipeline with OIDC

**Addresses:**
- Verify `heyvox` PyPI name (check pypi.org + test-pypi; dispute or rename if squatted) — MUST be first task
- Add `pypa/gh-action-pypi-publish` GitHub Action (OIDC trusted publishing, no secrets to rotate)
- `git add --chmod=+x` all `herald/bin/` and `herald/lib/*.sh`; verify `git ls-files -s` shows mode `100755`
- Add `chmod +x` self-healing to `heyvox setup`
- HUD visual QA pass (Retina, dark mode, external monitor — primarily human testing)
- Run `homebrew-pypi-poet` in clean venv to generate resource stanzas
- Create `Formula/heyvox.rb` in `github.com/heyvox/homebrew-heyvox` repo
- Cache `brew install portaudio` in GitHub Actions (key on portaudio version)

**Avoids:** Pitfall 6 (PyPI name squatting — verify first, publish second), Pitfall 7 (script permissions)

**Research flag:** Homebrew formula for heavy ML deps needs validation. The `mlx-whisper` dep is Apple Silicon-only; use `on_arm` block in formula Ruby DSL, or post-install message directing to `heyvox setup --install-models`. Run `homebrew-pypi-poet` and evaluate output size before committing — if formula exceeds practical limits, ship pipx-only for v1.2 and defer Homebrew to v1.3.

---

### Phase Ordering Rationale

- **Tests before code changes:** All three non-stack research files independently flag this as non-negotiable. Broken tests mask regressions in injection, dual-write, and shim removal.
- **Tech debt before injection:** Shim vars create a second source of truth for state that injection code reads. Clean state before modifying logic.
- **Injection before distribution:** Paste is the #1 UX pain point. Announcing with it broken sets poor first impressions.
- **Name verification is day-one of Phase 4:** If `heyvox` is squatted on PyPI, all other distribution work pauses. Resolve before investing in formula, CI pipeline, or announcements.

### Research Flags

Phases with standard patterns (skip `/gsd:research-phase`):
- **Phase 1 (Test Stability):** Root causes confirmed via direct test execution. pytest-mock and pytest-subprocess are well-documented. No research needed.
- **Phase 2 (Tech Debt):** Architecture research provides exact file list, line numbers, and migration order. Patterns are straightforward.
- **Phase 3 (Paste Injection):** Competitor pattern analysis complete. PyObjC NSPasteboard API is well-documented. Timing values may need empirical adjustment but no research blocker.

Phases that may benefit from targeted research during planning:
- **Phase 4 (Distribution):** The `on_arm` Homebrew block for Apple Silicon-only deps is a nuanced area. If formula proves impractical due to ML dep sizes, the fallback decision (pipx-only v1.2) needs to be made before writing the formula.

---

## Confidence Assessment

| Area | Confidence | Notes |
|------|------------|-------|
| Stack | HIGH | Existing stack validated in production. Additions verified against PyPI + official docs. websockets migration confirmed in changelog + migration guide. |
| Features | MEDIUM-HIGH | Paste timing values (50-100ms Electron delay) from competitor analysis, not direct measurement. Menu bar and test stability APIs are HIGH. Distribution workflow is HIGH via official docs. |
| Architecture | HIGH | All findings from direct source inspection and live test execution. 4 failures reproduced. File paths and line numbers confirmed. |
| Pitfalls | HIGH | Code-grounded. All pitfalls derived from observed failures, direct file inspection, and documented anti-patterns. Not speculative. |

**Overall confidence:** HIGH

### Gaps to Address

- **Paste timing empirical validation:** The 50-100ms Electron settle delay is based on competitor patterns and community findings, not measurement in this codebase. The current hardcoded 0.3s (300ms) may already be sufficient. Test with Claude Code, Cursor, and VS Code specifically before adding per-app config overhead — a single 100ms default may handle all cases.

- **PyPI name resolution:** `heyvox` on PyPI shows a placeholder but no author. Ownership is unconfirmed. This is a blocking decision — if squatted, a rename cascades everywhere. Resolve via PyPI dispute form or contact before Phase 4 begins.

- **Homebrew formula size:** MLX Whisper, openwakeword, and sherpa-onnx are collectively hundreds of MB. Evaluate `homebrew-pypi-poet` output size before committing to the Homebrew path for v1.2. `pipx install heyvox` is the more reliable primary path regardless.

- **`_browser_has_video_tab` current name:** ARCHITECTURE.md identified the rename as root cause of 2 media test failures but the current function name requires a grep to confirm. Run `grep -n "browser.*video\|video.*tab\|_browser" heyvox/audio/media.py` at the start of Phase 1.

---

## Sources

### Primary (HIGH confidence)
- Direct source inspection: `heyvox/input/injection.py`, `heyvox/main.py`, `heyvox/audio/media.py`, `heyvox/constants.py`, `tests/test_injection.py`, `tests/test_media.py`, `tests/conftest.py`, `pyproject.toml`
- Live test execution: 4 failures reproduced via `pytest tests/test_injection.py tests/test_media.py --tb=short`
- PyPI heyvox check: https://pypi.org/project/heyvox/ — placeholder registration, no author
- websockets changelog: https://websockets.readthedocs.io/en/stable/project/changelog.html — 14.0 deprecation confirmed
- PyPI Trusted Publishing: https://docs.pypi.org/trusted-publishers/
- Homebrew Python formula docs: https://docs.brew.sh/Python-for-Formula-Authors

### Secondary (MEDIUM confidence)
- Competitor analysis: VocaMac, STTInput, koe, EdgeWhisper — all use clipboard+Cmd+V as universal injection path
- Electron AXUIElement bug: electron/electron#36337 — confirms AX setValue unreliable in Electron
- Simon Willison — Homebrew Python packaging: https://til.simonwillison.net/homebrew/packaging-python-cli-for-homebrew
- pytest-subprocess docs: https://pypi.org/project/pytest-subprocess/ — v1.5.4
- GitHub Actions macOS audio workaround: daktilo issue #12 pattern

### Tertiary (LOW confidence / empirical)
- Electron paste settle delay (50-100ms): community reports, not measured in this codebase — validate empirically in Phase 3
- `on_arm` Homebrew block for Apple Silicon deps: pattern exists in DSL but exact syntax for ML deps not validated

---

*Research completed: 2026-04-12*
*Ready for roadmap: yes*
