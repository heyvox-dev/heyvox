# Roadmap: HeyVox

## Milestones

- ✅ **v1.0 MVP** — Phases 1-5 (shipped 2026-03-27)
- ✅ **v1.1 Architecture Hardening** — Phases 6-9 (shipped 2026-04-11)
- 🔧 **v1.2 Paste Injection Reliability** — Phases 12-15 (Phase 14 in progress, 3/6 plans)
- ✅ **v1.3 Robustness Sweep** — 5 defect-driven quick tasks (shipped 2026-05-25)
- 🎯 **v1.4 STT Accuracy** — Phase 16 (Auto-Glossary → Whisper `initial_prompt`); 4 plans planned (Spike 001)
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
  **Goal:** HeyVox installs cleanly via `pip install heyvox` or `brew install heyvox-dev/heyvox/heyvox` on macOS Apple Silicon, ships with a bundled general-purpose "Hey Vox" wake-word model, surfaces the active microphone in the menu bar, and exposes the configured mic-isolation mode in the HUD menu
  **Requirements:** [SPEC-R1, SPEC-R2, SPEC-R3, SPEC-R4, SPEC-R5, SPEC-R6]
  **Plans:** 6 plans
  Plans:
  - [ ] 14-01-PLAN.md — PyPI publish workflow (OIDC) + pyproject Beta classifier + importlib.metadata version sync [SPEC-R1, R2]
  - [ ] 14-02-PLAN.md — HUD menu-bar mic display (truncated title + tooltip) + voice-isolation submenu suffix [SPEC-R4, R5]
  - [ ] 14-03-PLAN.md — Wake-word training run + ship-gate eval script + GH Releases asset upload [SPEC-R6]
  - [ ] 14-04-PLAN.md — Setup wizard model download + sha256 validation + --redownload-wakeword + co-default lock [SPEC-R6]
  - [ ] 14-05-PLAN.md — Homebrew tap repo + Formula/heyvox.rb + release runbook docs [SPEC-R3]
  - [ ] 14-06-PLAN.md — Training pipeline docs (docs/wakeword-training.md) + README install polish [SPEC-R2, R6]
- [x] Phase 15: Paste Target Lock (2026-04-24) — record-start TargetLock + three-tier resolve ladder + fail-closed policy
  **Goal:** Transcribed speech lands in the exact text field that held the cursor at recording start, even after app/workspace/session change; unreachable target → fail-closed (clipboard + history + toast)
  **Requirements:** [R1, R2, R3, R4, R5, R6, R7, R8]
  **Plans:** 7 plans
  Plans:
  - [x] 15-01-PLAN.md — Conductor adapter + DB schema coupling
  - [x] 15-02-PLAN.md — TargetLock dataclass + capture_lock() (replaces TargetSnapshot)
  - [x] 15-03-PLAN.md — AppProfileConfig extension + app_fast_paste generalization
  - [x] 15-04-PLAN.md — Toast helper (heyvox/input/toast.py)
  - [x] 15-05-PLAN.md — Resolve ladder + fail-closed pipeline (resolve_lock + integration)
  - [x] 15-06-PLAN.md — Post-paste verification (verify_paste + drift detection + retry)
  - [x] 15-07-PLAN.md — heyvox log-health Paste section

### ✅ v1.3 Robustness Sweep (Shipped 2026-05-25)

Defect-driven quick tasks aimed at the recurring patterns from DEFECT-LOG —
P-new (ux invisibility), P-detector-without-action, P-hotplug-cache,
P-producer-parity, P-stochastic-wake. No formal phases — each primitive
landed as an atomic quick task with its own PLAN.md + SUMMARY.md.

- [x] **260525-hsb** — HUDSurface banner primitive — unified API for
      silent-state-change detectors (5 detector sites migrated/added;
      closes DEF-113). Commit `356aa3ec6`.
- [x] **260525-dvh** — DeviceHandle primitive — hotplug-safe wrappers for
      CoreAudio + PortAudio device IDs (pre-write validation in Herald,
      diagnostic in DeviceManager.reinit). Commit `293f1a49b`.
- [x] **260525-hdd** — Herald producer parity — shared `tts_helpers`
      module + WATCHER_FIRED forensic tag + drift-guard tests.
      Commit `f513b27a5`.
- [x] **260525-hsb (banner UX hotfix)** — Banner shows symbol-only in
      menu bar, full text in tooltip. Commit `d9983c54e`.
- [x] **260525-svg** — Stop-wake VAD silence gate — fast-path stop-wake
      requires recent silence (closes DEF-117); NEAR_MISS_FAST_BLOCKED
      forensic tag added. Commit `c351f2c55`.
- [x] **260525-d80** — DEF-080 herald CLI pinned to `sys.executable -m
      heyvox.herald.cli` — closes the 5-week-old fix that was parked on
      `heyvox/voice-resume-wip`. Stufe 1 of the WIP triage; isolated
      1-line change with two defect guards. Commit `40e254046`.

**Defects resolved:** DEF-080 (herald CLI PATH race), DEF-113 (Herald
ghost-device), DEF-117 (mid-sentence fast-stop FP). **Patterns mitigated:**
P-new + P-detector-without-action (HUDSurface), P-hotplug-cache for
CoreAudio (DeviceHandle), P-producer-parity for Herald (tts_helpers +
drift guard), P-stochastic-wake (silence gate). **Defects still open:**
DEF-104 (PortAudio HAL cache prozessweit — diagnostiziert, nicht
behoben).

### 🎯 v1.4 STT Accuracy (Planned)

- [ ] **Phase 16: STT Auto-Glossary** — Learn a vocabulary glossary of
      recurring proper-noun / tech-term STT mis-transcriptions from the real
      dictation history (Claude Haiku via Max subscription, no API key) and
      feed the top-N into Whisper's `initial_prompt`. Off-hot-path, opt-in,
      with mandatory guardrails (wake-word exclude, seed list, gibberish
      prefilter, confidence/frequency gate). Validated by **Spike 001**
      (cloud-gated; local 7B not viable).
  **AI-SPEC:** `.planning/phases/16-stt-auto-glossary/16-AI-SPEC.md`
  **Context:** Spike 001 (`.planning/spikes/001-auto-glossary-extraction/`)
  **Plans:** 4 plans (3 waves)
  Plans:
  - [x] 16-01-PLAN.md — Wave 0 test scaffolding: promote spike fixtures + deterministic scorer eval + pipeline test stubs + test_config extensions
  - [ ] 16-02-PLAN.md — Consumer wiring: STTLocalConfig.initial_prompt + VocabLearnerConfig + stt.py module-global thread + main.py call site
  - [ ] 16-03-PLAN.md — Learner pipeline (vocab_learner.py): GlossaryItem + wake/gibberish guardrails + Haiku extraction (CLI + API) + idempotent merge + 223-token cap + fail-closed learn_vocab
  - [ ] 16-04-PLAN.md — CLI surface: heyvox learn-vocab subcommand + config write + run summary + Open Question 2 insert test + anthropic optional extra

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
| 14. Distribution & UX Polish | v1.2 | 3/6 | In progress | partial 2026-04-13..05 |
| 15. Paste Target Lock | v1.2 | 7/7 | Complete | 2026-04-24 |
| Q. HUDSurface banner primitive | v1.3 | 1/1 | Complete | 2026-05-25 |
| Q. DeviceHandle primitive | v1.3 | 1/1 | Complete | 2026-05-25 |
| Q. Herald producer parity | v1.3 | 1/1 | Complete | 2026-05-25 |
| Q. Banner UX (icon + tooltip) | v1.3 | 1/1 | Complete | 2026-05-25 |
| Q. Stop-wake VAD silence gate | v1.3 | 1/1 | Complete | 2026-05-25 |
| Q. DEF-080 herald CLI pin | v1.3 | 1/1 | Complete | 2026-05-25 |
| 16. STT Auto-Glossary | v1.4 | 1/4 | In Progress|  |
