# Roadmap: HeyVox

## Milestones

- ✅ **v1.0 MVP** — Phases 1-5 (shipped 2026-03-27)
- ✅ **v1.1 Architecture Hardening** — Phases 6-9 (shipped 2026-04-11)
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

### 🔧 v1.2 Paste Injection Reliability (Active)

- [x] Phase 12: Paste Injection Reliability (1/3 plans) — in progress (completed 2026-04-13)
- [x] Phase 13: Audio Reliability — device profiles, TTS/recording interaction, silence detection robustness (completed 2026-04-13)
  **Goal:** Robust audio pipeline across mic types with per-device profiles, headset-aware echo suppression, and instant TTS interruption
  **Plans:** 4 plans
  Plans:
  - [x] 13-01-PLAN.md — Device profiles: MicProfileManager, config model, cache, calibration
  - [x] 13-02-PLAN.md — TTS interruption: fix herald stop, add interrupt, fix Escape handler
  - [x] 13-03-PLAN.md — Integration: wire profiles into main loop, echo suppression gate, auto-calibration
  - [x] 13-04-PLAN.md — CLI: add heyvox calibrate command
- [ ] Phase 14: Distribution & UX Polish — PyPI, Homebrew, HUD mic display
- [ ] Phase 15: Paste Target Lock — record-start TargetLock + three-tier resolve ladder + fail-closed policy
  **Goal:** Transcribed speech lands in the exact text field that held the cursor at recording start, even after app/workspace/session change; unreachable target → fail-closed (clipboard + history + toast)
  **Requirements:** [R1, R2, R3, R4, R5, R6, R7, R8]
  **Plans:** 7 plans
  Plans:
  - [ ] 15-01-PLAN.md — Conductor adapter + DB schema coupling
  - [ ] 15-02-PLAN.md — TargetLock dataclass + capture_lock() (replaces TargetSnapshot)
  - [ ] 15-03-PLAN.md — AppProfileConfig extension + app_fast_paste generalization
  - [ ] 15-04-PLAN.md — Toast helper (heyvox/input/toast.py)
  - [ ] 15-05-PLAN.md — Resolve ladder + fail-closed pipeline (resolve_lock + integration)
  - [ ] 15-06-PLAN.md — Post-paste verification (verify_paste + drift detection + retry)
  - [ ] 15-07-PLAN.md — heyvox log-health Paste section

### 📋 v2.0 Cross-Platform & Polish (Planned)

Phases TBD — define via `/gsd:new-milestone`

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
| 12. Paste Injection Reliability | v1.2 | 1/3 | Complete    | 2026-04-13 |
| 13. Audio Reliability | v1.2 | 3/4 | Complete    | 2026-04-13 |
| 14. Distribution & UX Polish | v1.2 | 0/? | Planned | — |
| 15. Paste Target Lock | v1.2 | 0/7 | Planned | — |
