# Roadmap: HeyVox

## Milestones

- ✅ **v1.0 MVP** — Phases 1-5 (shipped 2026-03-27)
- ✅ **v1.1 Architecture Hardening** — Phases 6-9 (shipped 2026-04-11)
- 📋 **v1.2 Polish & Reliability** — Phases 10-13 (in progress)
- 📋 **v2.0 Cross-Platform & Polish** — TTS server, MediaRemote, custom wake word, Chrome extension, cross-platform client

## Phases

<details>
<summary>✅ v1.0 MVP (Phases 1-5) — SHIPPED 2026-03-27</summary>

- [x] Phase 1: Foundation (2/2 plans) — completed 2026-03-27
- [x] Phase 2: Audio + Input Pipeline (2/2 plans) — completed 2026-03-27
- [x] Phase 3: CLI + TTS Output (2/2 plans) — completed 2026-03-27
- [x] Phase 4: MCP Server (2/2 plans) — completed 2026-03-27
- [x] Phase 5: HUD Overlay (2/2 plans) — completed 2026-03-27

</details>

<details>
<summary>✅ v1.1 Architecture Hardening (Phases 6-9) — SHIPPED 2026-04-11</summary>

- [x] Phase 6: Decomposition (4/4 plans) — completed 2026-04-11
- [x] Phase 7: Herald Python Port (5/5 plans) — completed 2026-04-11
- [x] Phase 8: IPC Consolidation (3/3 plans) — completed 2026-04-11
- [x] Phase 9: Test Suite (2/2 plans) — completed 2026-04-11

</details>

### 📋 v1.2 Polish & Reliability

- [x] **Phase 10: Test Stability** — 2/3 plans complete, 1 gap closure plan pending (completed 2026-04-12)
- [x] **Phase 11: Tech Debt Cleanup** — Remove 7 shim vars, complete tts_playing dual-write, fix websockets deprecation (completed 2026-04-12)
- [ ] **Phase 12: Paste Injection Reliability** — NSPasteboard direct write, per-app focus delays, retry on focus steal
- [ ] **Phase 13: Distribution & UX Polish** — PyPI publish pipeline, Homebrew formula, HUD mic display

### 📋 v2.0 Cross-Platform & Polish (Planned)

Phases TBD — define via `/gsd:new-milestone`

## Phase Details

### Phase 10: Test Stability
**Goal**: CI runs green on every PR with reliable, structurally-sound test assertions
**Depends on**: Phase 9 (existing test suite baseline)
**Requirements**: TEST-01, TEST-02, TEST-03
**Success Criteria** (what must be TRUE):
  1. All 120 tests pass in CI on macos-14 runner (no audio device required)
  2. Injection tests assert which subprocess command ran, not how many times subprocess was called
  3. Media tests reference the current function name and pass without manual patch updates
  4. A new developer can run `pytest` locally without audio hardware and see all tests green
**Plans:** 3/3 plans complete
Plans:
- [x] 10-01-PLAN.md — Add dev dependencies (pytest-mock, pytest-subprocess) and pytest marker registration
- [x] 10-02-PLAN.md — Fix 5 stale test failures and update CI workflow command
- [x] 10-03-PLAN.md — Gap closure: add missing dev deps, requires_audio marker, intent-based injection assertions

### Phase 11: Tech Debt Cleanup
**Goal**: Single source of truth for all shared state — no shim vars, no dual-write ambiguity, no deprecation warnings
**Depends on**: Phase 10
**Requirements**: DEBT-01, DEBT-02, DEBT-03
**Success Criteria** (what must be TRUE):
  1. `grep "shim\|backward.compat\|# TODO.*shim" heyvox/main.py` returns no matches
  2. tts_playing state is written to the atomic state file in both the Python TTS path and Herald orchestrator — old flag file is a parallel write, not the primary
  3. Starting HeyVox produces zero deprecation warnings in the log (no websockets asyncio warning)
  4. All tests still pass after shim removal (no regressions from AppContext migration)
**Plans:** 2/2 plans complete
Plans:
- [x] 11-01-PLAN.md — Migrate test_flag_coordination.py to AppContext API and remove shim vars from main.py
- [x] 11-02-PLAN.md — Fix websockets deprecation and add tts_playing dual-write to atomic state

### Phase 12: Paste Injection Reliability
**Goal**: Users can dictate into any AI coding agent and their text appears correctly without losing clipboard contents
**Depends on**: Phase 11
**Requirements**: PASTE-01, PASTE-02, PASTE-03, PASTE-04, PASTE-05, PASTE-06
**Success Criteria** (what must be TRUE):
  1. Text dictated into Claude Code (Electron) appears in the input field on the first attempt, with no clipboard corruption
  2. Text dictated into Cursor appears reliably without a manual retry
  3. When paste fails, user hears an error audio cue instead of silent wrong-content injection
  4. Clipboard content the user had before dictating is still available after the injection completes
  5. Dictating into iTerm2 or Terminal (native AppKit) succeeds faster than into Electron apps
**Plans**: TBD
**UI hint**: yes

### Phase 13: Distribution & UX Polish
**Goal**: HeyVox is installable on a fresh Mac in two commands, and the HUD shows which microphone is active
**Depends on**: Phase 12
**Requirements**: DIST-01, DIST-02, DIST-03, UX-01, UX-02
**Success Criteria** (what must be TRUE):
  1. `pipx install heyvox` on a fresh macOS machine succeeds and `heyvox setup` runs to completion
  2. `brew tap heyvox/tap && brew install heyvox` installs and produces a working binary
  3. GitHub Actions CI publishes a wheel to PyPI on version tag push (OIDC, no stored secrets)
  4. The HUD pill shows the active microphone name (e.g., "Jabra Evolve2 55") while recording
  5. The HUD pill shows mic mode (Standard / Voice Isolation) when voice isolation is active
**Plans**: TBD
**UI hint**: yes

## Progress

| Phase | Milestone | Plans Complete | Status | Completed |
|-------|-----------|----------------|--------|-----------|
| 1. Foundation | v1.0 | 2/2 | Complete | 2026-03-27 |
| 2. Audio + Input Pipeline | v1.0 | 2/2 | Complete | 2026-03-27 |
| 3. CLI + TTS Output | v1.0 | 2/2 | Complete | 2026-03-27 |
| 4. MCP Server | v1.0 | 2/2 | Complete | 2026-03-27 |
| 5. HUD Overlay | v1.0 | 2/2 | Complete | 2026-03-27 |
| 6. Decomposition | v1.1 | 4/4 | Complete | 2026-04-11 |
| 7. Herald Python Port | v1.1 | 5/5 | Complete | 2026-04-11 |
| 8. IPC Consolidation | v1.1 | 3/3 | Complete | 2026-04-11 |
| 9. Test Suite | v1.1 | 2/2 | Complete | 2026-04-11 |
| 10. Test Stability | v1.2 | 3/3 | Complete    | 2026-04-12 |
| 11. Tech Debt Cleanup | v1.2 | 2/2 | Complete    | 2026-04-12 |
| 12. Paste Injection Reliability | v1.2 | 0/? | Not started | - |
| 13. Distribution & UX Polish | v1.2 | 0/? | Not started | - |
