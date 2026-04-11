# Requirements: HeyVox v1.1

**Defined:** 2026-04-10
**Core Value:** One voice layer that works across ALL your AI coding agents — wake word, local STT, local TTS, beautiful HUD — without sending audio to the cloud.

## v1.1 Requirements

Requirements for Architecture Hardening milestone. Each maps to roadmap phases.

### Decomposition

- [x] **DECOMP-01**: Recording logic extracted into RecordingStateMachine with start/stop/send_local + state
- [x] **DECOMP-02**: Device management extracted into DeviceManager (hotplug, zombie detection, health checks, cooldown)
- [x] **DECOMP-03**: Wake word processing extracted into WakeWordProcessor (stripping, garbled detection)
- [x] **DECOMP-04**: 17+ module globals replaced with shared AppContext dataclass passed by reference

### Herald Python Port

- [x] **HERALD-01**: orchestrator.sh ported to Python (heyvox/herald/orchestrator.py) with equivalent behavior
- [x] **HERALD-02**: WAV normalization moved from orchestrator to Kokoro daemon (normalize at generation time)
- [ ] **HERALD-03**: osascript volume calls replaced with CoreAudio ctypes bindings
- [ ] **HERALD-04**: Mute/volume detection cached (check every 5s, not every 300ms loop iteration)

### IPC Consolidation

- [x] **IPC-01**: All flag/socket/PID/queue paths consolidated into heyvox/constants.py (single source of truth)
- [ ] **IPC-02**: Flag-file constellation replaced with atomic /tmp/heyvox-state.json (temp file + os.rename)
- [ ] **IPC-03**: Periodic garbage collection added for orphaned WAV/timing/workspace files in queue directories

### Test Suite

- [ ] **TEST-01**: Pure function tests for garbled detection, wake word stripping, config loading, echo filtering
- [ ] **TEST-02**: State machine transition tests for recording start/stop/busy/cancel flows
- [ ] **TEST-03**: IPC round-trip tests for HUD Unix socket (client/server, reconnection, message loss)
- [ ] **TEST-04**: Device selection tests with mocked PyAudio (priority, cooldown, fallback)

## Future Requirements

Deferred to v2.0+. Tracked but not in current roadmap.

### Cross-Platform
- **XPLAT-01**: TTS server mode (Kokoro on Mac Mini, stream to clients)
- **XPLAT-02**: Lightweight client for Windows/Linux (sherpa-onnx STT, no Apple Silicon)

### Polish
- **POLISH-01**: Custom "Hey Vox" wake word (trained openwakeword model)
- **POLISH-02**: PyPI / Homebrew distribution
- **POLISH-03**: Demo video for heyvox.dev

## Out of Scope

| Feature | Reason |
|---------|--------|
| New user-facing features | This is a reliability/refactoring milestone only |
| Herald bash script removal | Python port replaces it; old script kept for rollback |
| Full IPC protocol redesign | Atomic state file is incremental improvement, not full redesign |
| Performance benchmarks | Focus is correctness, not speed |

## Traceability

| Requirement | Phase | Status |
|-------------|-------|--------|
| DECOMP-01 | Phase 6 | Complete |
| DECOMP-02 | Phase 6 | Complete |
| DECOMP-03 | Phase 6 | Complete |
| DECOMP-04 | Phase 6 | Complete |
| HERALD-01 | Phase 7 | Complete |
| HERALD-02 | Phase 7 | Complete |
| HERALD-03 | Phase 7 | Pending |
| HERALD-04 | Phase 7 | Pending |
| IPC-01 | Phase 8 | Complete |
| IPC-02 | Phase 8 | Pending |
| IPC-03 | Phase 8 | Pending |
| TEST-01 | Phase 9 | Pending |
| TEST-02 | Phase 9 | Pending |
| TEST-03 | Phase 9 | Pending |
| TEST-04 | Phase 9 | Pending |

**Coverage:**
- v1.1 requirements: 15 total
- Mapped to phases: 15
- Unmapped: 0

---
*Requirements defined: 2026-04-10*
*Last updated: 2026-04-10 — all 15 requirements mapped to phases 6-9*
