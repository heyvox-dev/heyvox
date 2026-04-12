# Roadmap: HeyVox

## Milestones

- ✅ **v1.0 MVP** — Phases 1-5 (shipped 2026-03-27)
- 🚧 **v1.1 Architecture Hardening** — Phases 6-9 (in progress)
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

### 🚧 v1.1 Architecture Hardening (In Progress)

**Milestone Goal:** Refactor the core architecture for reliability, testability, and maintainability — decompose the monolithic main loop, eliminate the bash/Python boundary in Herald, consolidate fragile flag-file IPC, and add a test suite. No new user-facing features.

- [ ] **Phase 6: Decomposition** — Break main.py monolith into focused modules with shared context
- [ ] **Phase 7: Herald Python Port** — Replace bash orchestrator with pure Python, eliminate shell boundary
- [ ] **Phase 8: IPC Consolidation** — Replace 25+ /tmp flag files with atomic state file and clean constants
- [x] **Phase 9: Test Suite** — Add tests for pure functions, state machines, IPC, and device selection (completed 2026-04-11)

## Phase Details

### Phase 6: Decomposition
**Goal**: main.py's 2000-line monolith is split into focused, independently testable modules with no module-level globals
**Depends on**: Phase 5 (v1.0 complete)
**Requirements**: DECOMP-01, DECOMP-02, DECOMP-03, DECOMP-04
**Success Criteria** (what must be TRUE):
  1. RecordingStateMachine exists as a standalone class with start/stop/send_local methods and explicit state
  2. DeviceManager handles hotplug, zombie detection, health checks, and cooldown without touching main.py logic
  3. WakeWordProcessor handles wake word stripping and garbled detection as a self-contained unit
  4. AppContext dataclass holds all shared state; no module-level globals remain in main.py
  5. HeyVox starts and responds to voice normally after the refactor (behavior unchanged)
**Plans:** 3/4 plans executed
Plans:
- [x] 06-00-PLAN.md — Test scaffolds for extracted modules (Nyquist compliance)
- [x] 06-01-PLAN.md — AppContext dataclass + text_processing.py extraction
- [x] 06-02-PLAN.md — DeviceManager extraction
- [x] 06-03-PLAN.md — RecordingStateMachine extraction + main.py thinning

### Phase 7: Herald Python Port
**Goal**: The Herald TTS orchestrator runs entirely in Python — no bash/Python boundary crossings per TTS request
**Depends on**: Phase 6
**Requirements**: HERALD-01, HERALD-02, HERALD-03, HERALD-04
**Success Criteria** (what must be TRUE):
  1. heyvox/herald/orchestrator.py exists and handles queue playback, workspace switching, and hold queue with equivalent behavior to orchestrator.sh
  2. WAV normalization happens inside the Kokoro daemon at generation time, not in the orchestrator
  3. System volume reads and writes use CoreAudio ctypes bindings, not osascript
  4. Mute/volume state is checked at most once every 5 seconds (cached), not on every 300ms loop tick
  5. TTS pipeline completes a full speak-to-audio cycle with the new Python orchestrator
**Plans:** 5 plans (4 executed + 1 gap closure)
Plans:
- [x] 07-01-PLAN.md — Constants + CoreAudio volume + Kokoro WAV normalization
- [x] 07-02-PLAN.md — HeraldWorker (TTS extraction, generation, multi-part streaming)
- [x] 07-03-PLAN.md — HeraldOrchestrator (playback loop, ducking, hold queue)
- [x] 07-04-PLAN.md — Wiring (hooks, CLI, __init__.py) + bash script cleanup
- [x] 07-05-PLAN.md — Gap closure: daemon normalization + media pause/resume fix

### Phase 8: IPC Consolidation
**Goal**: All IPC paths are declared in one place and cross-process coordination uses an atomic state file instead of a constellation of flag files
**Depends on**: Phase 7
**Requirements**: IPC-01, IPC-02, IPC-03
**Success Criteria** (what must be TRUE):
  1. Every socket, flag, PID, and queue path in the codebase is imported from heyvox/constants.py — no hardcoded /tmp strings elsewhere
  2. Cross-process state (recording, speaking, mute, active workspace) reads/writes /tmp/heyvox-state.json via atomic temp file + os.rename — no individual flag files
  3. A cleanup routine runs periodically and removes orphaned WAV, timing, and workspace sidecar files from queue directories
**Plans:** 2/3 plans executed
Plans:
- [x] 08-01-PLAN.md — Constants consolidation + caller migration (IPC-01)
- [x] 08-02-PLAN.md — Atomic state file module + dual-write wiring (IPC-02)
- [x] 08-03-PLAN.md — Queue garbage collection in orchestrator (IPC-03)

### Phase 9: Test Suite
**Goal**: A pytest suite exists that validates pure functions, state machine transitions, IPC round-trips, and device selection logic without requiring real hardware
**Depends on**: Phase 8
**Requirements**: TEST-01, TEST-02, TEST-03, TEST-04
**Success Criteria** (what must be TRUE):
  1. pytest passes for garbled detection, wake word stripping, config loading, and echo filter logic with no hardware or audio device required
  2. RecordingStateMachine transition tests cover start, stop, busy, and cancel flows with all edge cases verified
  3. HUD Unix socket tests verify client/server round-trips, reconnection after server restart, and message loss behavior
  4. Device selection tests with mocked PyAudio verify priority ordering, cooldown enforcement, and fallback behavior
**Plans:** 2/2 plans complete
Plans:
- [x] 09-01-PLAN.md — Pure function tests (TEST-01) + state machine tests (TEST-02) + adapter fix
- [x] 09-02-PLAN.md — HUD IPC tests (TEST-03) + device selection tests (TEST-04)

## Progress

| Phase | Milestone | Plans Complete | Status | Completed |
|-------|-----------|----------------|--------|-----------|
| 1. Foundation | v1.0 | 2/2 | Complete | 2026-03-27 |
| 2. Audio + Input Pipeline | v1.0 | 2/2 | Complete | 2026-03-27 |
| 3. CLI + TTS Output | v1.0 | 2/2 | Complete | 2026-03-27 |
| 4. MCP Server | v1.0 | 2/2 | Complete | 2026-03-27 |
| 5. HUD Overlay | v1.0 | 2/2 | Complete | 2026-03-27 |
| 6. Decomposition | v1.1 | 3/4 | In Progress|  |
| 7. Herald Python Port | v1.1 | 4/5 | In Progress|  |
| 8. IPC Consolidation | v1.1 | 2/3 | In Progress|  |
| 9. Test Suite | v1.1 | 2/2 | Complete   | 2026-04-11 |
