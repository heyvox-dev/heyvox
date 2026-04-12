# Requirements: HeyVox v1.2

**Defined:** 2026-04-12
**Core Value:** One voice layer that works across ALL your AI coding agents — wake word, local STT, local TTS, beautiful HUD — without sending audio to the cloud.

## v1.2 Requirements

Requirements for Polish & Reliability release. Each maps to roadmap phases.

### Test Stability

- [x] **TEST-01**: All 6 stale test failures fixed with intent-based assertions
- [x] **TEST-02**: CI runs green with audio device mocking (conftest.py + `@pytest.mark.requires_audio`)
- [x] **TEST-03**: pytest-mock + pytest-subprocess added as dev dependencies

### Tech Debt

- [ ] **DEBT-01**: 7 backward-compat shim vars removed from main.py
- [ ] **DEBT-02**: tts_playing dual-write completed (Python + bash scripts migrated to atomic state)
- [ ] **DEBT-03**: websockets deprecation warning fixed (asyncio.server import)

### Paste Injection

- [ ] **PASTE-01**: NSPasteboard direct replaces pbcopy subprocess for clipboard writes
- [ ] **PASTE-02**: Configurable focus settle delay with retry on focus steal
- [ ] **PASTE-03**: Per-app delay profiles (Conductor, Cursor, Windsurf, Terminal, generic)
- [ ] **PASTE-04**: AXUIElement fast-path for native AppKit apps
- [ ] **PASTE-05**: Focus detection verifies correct app/field before paste, with fallback to clipboard + notification
- [ ] **PASTE-06**: Paste works reliably in non-Conductor apps (Cursor, Windsurf, VS Code, iTerm, etc.)

### Distribution

- [ ] **DIST-01**: PyPI name verified/secured and publish workflow configured (GitHub Actions OIDC)
- [ ] **DIST-02**: pyproject.toml metadata complete (description, classifiers, URLs, license)
- [ ] **DIST-03**: Homebrew tap formula with `on_arm` block for ML dependencies

### UX Polish

- [ ] **UX-01**: Active microphone name displayed in HUD pill
- [ ] **UX-02**: Mic mode indicator (standard/voice isolation) in HUD

## Future Requirements

Deferred to v2.0+.

### Cross-Platform
- **XPLAT-01**: TTS server mode (Kokoro on Mac Mini, WebSocket streaming to clients)
- **XPLAT-02**: Lightweight client for Windows/Linux (sherpa-onnx STT, no Apple Silicon)

### Wake Word
- **WAKE-01**: Custom "Hey Vox" wake word model (trained, replaces hey_jarvis)

### History
- **HIST-01**: Transcript history stored in ~/.vox/transcript_history.json
- **HIST-02**: CLI `vox history` to review/copy past transcriptions

## Out of Scope

| Feature | Reason |
|---------|--------|
| Native macOS .app (SwiftUI) | v2 Pro feature, not v1.x |
| Multi-agent workspace orchestration | v2 Pro feature |
| Smart transcription (context-aware) | v2 Pro feature |
| Meeting transcription | Different product category |
| Mac App Store distribution | Sandboxing blocks Accessibility API |
| Cloud STT/TTS | Zero cloud is core differentiator |
| Clipboard restore after paste | Electron anti-pattern per research — breaks more than it helps |

## Traceability

Which phases cover which requirements. Updated during roadmap creation.

| Requirement | Phase | Status |
|-------------|-------|--------|
| TEST-01 | Phase 10 | Complete |
| TEST-02 | Phase 10 | Complete |
| TEST-03 | Phase 10 | Complete |
| DEBT-01 | Phase 11 | Pending |
| DEBT-02 | Phase 11 | Pending |
| DEBT-03 | Phase 11 | Pending |
| PASTE-01 | Phase 12 | Pending |
| PASTE-02 | Phase 12 | Pending |
| PASTE-03 | Phase 12 | Pending |
| PASTE-04 | Phase 12 | Pending |
| PASTE-05 | Phase 12 | Pending |
| PASTE-06 | Phase 12 | Pending |
| DIST-01 | Phase 13 | Pending |
| DIST-02 | Phase 13 | Pending |
| DIST-03 | Phase 13 | Pending |
| UX-01 | Phase 13 | Pending |
| UX-02 | Phase 13 | Pending |

**Coverage:**
- v1.2 requirements: 17 total
- Mapped to phases: 17
- Unmapped: 0

---
*Requirements defined: 2026-04-12*
*Last updated: 2026-04-12 after roadmap creation*
