# Features Research: Vox — macOS Voice Layer

## Table Stakes (must have or users leave)

| Feature | Complexity | Dependencies | Notes |
|---------|-----------|--------------|-------|
| **Local STT** — speech-to-text that runs on-device | Medium | mlx-whisper, pyaudio | Core functionality; every competitor has this |
| **Push-to-talk** — hotkey to start/stop recording | Low | Quartz event tap | Expected by all voice tool users |
| **Text injection** — transcribed text appears in focused app | Low | osascript | Without this, STT output goes nowhere |
| **Silence detection** — auto-stop recording after silence | Low | pyaudio RMS | Users expect recording to stop automatically |
| **Visual recording indicator** — know when mic is hot | Low | AppKit | Critical for trust — users must know when they're being recorded |
| **CLI control** — start/stop/status from terminal | Low | argparse/click | Developer audience expects CLI |
| **Configuration file** — customize behavior without code | Low | PyYAML | Developers want to tweak settings |
| **Auto-start on login** — runs as background service | Low | launchd | Users won't remember to start it manually |
| **Cancellation** — abort recording without sending | Low | ESC key handler | Must be able to bail out |
| **Audio feedback** — sound cues for state changes | Low | afplay | Users need confirmation mic started/stopped |

## Differentiators (competitive advantage)

| Feature | Complexity | Dependencies | Notes |
|---------|-----------|--------------|-------|
| **Wake word detection** — hands-free activation | Medium | openwakeword | Only VS Code Speech has this ("Hey Code"), but VS Code-only. Our key differentiator |
| **Custom wake words** — train your own trigger phrase | High | Training pipeline | No competitor offers this. Creates user investment |
| **MCP voice server** — agents can trigger voice I/O | Medium | mcp SDK | Spokenly has basic MCP; ours is bidirectional with TTS |
| **Local TTS output** — agent speaks responses aloud | Medium | Kokoro/sherpa-onnx | Agent Voice has cloud TTS; we're fully local |
| **HUD overlay** — beautiful visual status across all spaces | High | AppKit, Unix socket | Nobody else has this. Demo video star |
| **Multi-agent targeting** — voice to specific agent/app | Medium | Adapter protocol | Nobody else targets multiple AI agents |
| **TTS coordination** — pause TTS when user speaks | Medium | IPC flag, audio ducking | Full-duplex coordination is unique |
| **TTS queue management** — skip, pause, mute | Medium | Queue + IPC | Multi-message queue with controls |
| **Adapter protocol** — extensible agent integrations | Medium | Protocol class | Structured way to add new agent targets |
| **Guided setup** — `vox setup` walks through permissions | Medium | Interactive CLI | Reduces 15-min setup to 3 minutes |

## Anti-Features (deliberately NOT building)

| Feature | Reason | Risk if built |
|---------|--------|---------------|
| Cloud STT/TTS | Core differentiator is "fully local" | Destroys trust, privacy story |
| Meeting transcription | Different product category, feature bloat (OpenWhispr went here) | Scope creep, unfocused product |
| Full IDE control (Talon-style) | Massive complexity, different audience | Years of work, alienates casual users |
| Cross-platform v1 | macOS-first for quality; Linux/Win abstractions dilute UX | Slower shipping, worse on all platforms |
| Voice-to-code (syntax dictation) | AI agents handle code generation; we handle conversation | Competing with Talon, wrong abstraction |
| Always-on recording/logging | Privacy nightmare | User trust destroyed |
| Native macOS app in v1 | SwiftUI app adds months of work | Delays launch, solo maintainer burnout |
| Custom voice models | Users don't want to manage ML models | Support burden, confusion |

## Feature Dependencies

```
pyaudio (mic) ──> openwakeword (wake word) ──> mlx-whisper (STT) ──> osascript (inject)
                                                                        │
                                                                   adapter protocol
                                                                        │
                                                              ┌─────────┼─────────┐
                                                           generic   conductor   cursor

afplay (cues) ──> standalone, no deps

AppKit (HUD) <── Unix socket <── main loop + TTS engine

MCP server ──> wraps existing functions as tools
```

## v1 vs v2 Feature Split

**v1 (OSS, "It Works for Anyone"):**
All table stakes + wake word + guided setup + adapter protocol + MCP server basics

**v2 (Pro):**
TTS output + HUD overlay + multi-agent orchestration + TTS queue + voice command extensions + native app
