# Project Retrospective

*A living document updated after each milestone. Lessons feed forward into future planning.*

## Milestone: v1.1 — Architecture Hardening

**Shipped:** 2026-04-11
**Phases:** 4 | **Plans:** 14 | **Commits:** 83

### What Was Built
- Decomposed 2000-line main.py into 4 focused modules (RecordingStateMachine, DeviceManager, WakeWordProcessor, AppContext)
- Ported Herald TTS orchestrator from bash to pure Python — eliminated all shell boundary crossings
- CoreAudio ctypes bindings for volume control (replacing osascript)
- Atomic state file replacing 25+ /tmp flag files for cross-process coordination
- Periodic garbage collection for Herald queue directories
- 114-test pytest suite covering pure functions, state machines, IPC, and device selection

### What Worked
- **Strict phase ordering** (Decomp → Herald → IPC → Tests) — each phase built cleanly on the prior, no backtracking needed
- **Dual-write migration strategy** for IPC — old flag files kept working alongside new state file, safe rollback path
- **Nyquist test-first approach** in Phase 6 — test stubs created before extraction, unskipped as modules materialized
- **Audit before completion** — the milestone audit caught stale REQUIREMENTS.md checkboxes and quantified tech debt cleanly

### What Was Inefficient
- **5 bugs introduced by GSD refactor sessions** — ThreadPoolExecutor shutdown, watchdog threshold, overlay syntax, socket injection. All were mechanical errors (wrong parameter, bad indentation, untested assumption). None were architectural. Could have been caught with a smoke test after each phase.
- **Stale REQUIREMENTS.md checkboxes** — HERALD-03/04 were verified satisfied but checkboxes weren't updated during phase execution. Manual checkbox maintenance is error-prone.
- **Some phase statuses in ROADMAP.md showed "In Progress"** when all plans were actually complete — ROADMAP wasn't updated at phase completion boundaries.

### Patterns Established
- **AppContext as constructor injection** — all modules receive shared state via constructor, no globals
- **Backward-compat shim pattern** — re-export old names from main.py during migration, clean up in test phase
- **Inline try/except around IPC calls** — prevents state file failures from crashing callers
- **Post-refactor smoke test** — after v1.1 bugs, mandatory live test after any refactor phase

### Key Lessons
1. **GSD agents make mechanical errors reliably** — unindented code, wrong function args, threshold below baseline. Always run the system after refactor phases, don't trust "all tests pass" alone.
2. **ThreadPoolExecutor requires `with` or explicit `shutdown(wait=True)`** — `shutdown(wait=False)` creates orphaned threads that hold resources. This bit twice (MLX + sherpa paths).
3. **Memory watchdog thresholds must account for loaded model baselines** — MLX Whisper is ~1050MB when loaded; a 1000MB threshold causes a restart loop.
4. **Socket injection to Conductor sidecar doesn't work** — the query RPC is on an internal Electron tunnel only. External calls return null silently. Always test integration points end-to-end.
5. **Dual-write is the right migration pattern for IPC** — new system proves itself alongside old, callers migrate incrementally. The tts_playing incomplete migration proves the pattern works: old flag file still serves until new path is wired.

### Cost Observations
- Model mix: ~70% opus (execution), ~20% sonnet (planning), ~10% haiku (summaries)
- Sessions: ~8 across 2 days
- Notable: 14 plans in 2 days is high velocity for refactoring work. The bugs-found-and-fixed cycle added ~2 hours of manual debugging.

---

## Milestone: v1.0 — MVP

**Shipped:** 2026-03-27
**Phases:** 5 | **Plans:** 10

### What Was Built
- Complete macOS voice layer: wake word, STT, TTS, HUD, MCP server
- 4,280 LOC Python in 2 days

### What Worked
- Rapid code generation with GSD framework
- Clean separation of concerns from the start (adapters, IPC sockets)

### What Was Inefficient
- No tests shipped — addressed in v1.1
- Some globals and monolithic structure — addressed in v1.1

### Key Lessons
1. Ship fast, harden later — v1.0 proved the concept, v1.1 made it maintainable
2. macOS permissions (Accessibility, Microphone) are the #1 user friction point

---

## Cross-Milestone Trends

### Process Evolution

| Milestone | Commits | Phases | Key Change |
|-----------|---------|--------|------------|
| v1.0 | ~40 | 5 | Initial GSD project, no tests |
| v1.1 | 83 | 4 | Added audit step, Nyquist compliance, test-first |

### Cumulative Quality

| Milestone | Tests | Coverage | Notable |
|-----------|-------|----------|---------|
| v1.0 | 0 | 0% | Code generation, no testing |
| v1.1 | 114 | Core paths | Pure functions + state machines + IPC |

### Top Lessons (Verified Across Milestones)

1. **Always smoke-test after refactoring** — v1.0 had no tests to catch issues; v1.1 had tests but mechanical errors still slipped through to runtime
2. **Dual-write for migrations** — proven in v1.1 IPC work, should be standard pattern for any breaking change
