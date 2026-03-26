# Stack Research: Vox — macOS Voice Layer

## Recommended Stack

### Core Runtime
| Component | Choice | Version | Confidence | Rationale |
|-----------|--------|---------|------------|-----------|
| Language | Python | 3.12+ | HIGH | Existing codebase, PyObjC compatibility, ML ecosystem |
| Package manager | uv | latest | HIGH | Faster than pip/pipx, lockfile support, replacing pipx in ecosystem |
| Build system | hatchling | latest | HIGH | Modern, PEP 517 compliant, good for CLI apps |

### Audio Pipeline
| Component | Choice | Version | Confidence | Rationale |
|-----------|--------|---------|------------|-----------|
| Mic input | pyaudio | 0.2.14+ | HIGH | Proven, low-latency, existing code uses it. Requires portaudio via brew |
| Wake word | openwakeword | 0.6+ | HIGH | Pure Python, custom training pipeline already built, works well |
| STT (primary) | mlx-whisper | latest | HIGH | Metal GPU acceleration on Apple Silicon, fastest local option |
| STT (fallback) | sherpa-onnx | latest | MEDIUM | CPU fallback, wider model support, but slower |
| TTS | Kokoro (via sherpa-onnx) | latest | HIGH | High-quality local TTS, no cloud dependency |
| Audio playback | afplay (system) | n/a | HIGH | Built-in macOS, zero dependencies, sufficient for cues |

### macOS Integration
| Component | Choice | Version | Confidence | Rationale |
|-----------|--------|---------|------------|-----------|
| GUI framework | PyObjC (AppKit) | 10.3+ | HIGH | Existing code, NSVisualEffectView for HUD, NSWindow for overlay |
| Event tap (PTT) | PyObjC (Quartz) | 10.3+ | HIGH | Fn key detection, existing working code |
| Text injection | osascript | system | HIGH | Clipboard + keystroke, works with any app |
| Service mgmt | launchd | system | HIGH | macOS native, existing plist pattern |

### MCP & IPC
| Component | Choice | Version | Confidence | Rationale |
|-----------|--------|---------|------------|-----------|
| MCP SDK | mcp (Python) | 1.x | HIGH | Official Anthropic SDK, FastMCP pattern |
| IPC | Unix domain socket | n/a | HIGH | /tmp/vox-hud.sock, JSON messages, proven reliable |
| Config | PyYAML | 6.x | HIGH | Existing config.yaml format, user-friendly |

### Development & Distribution
| Component | Choice | Version | Confidence | Rationale |
|-----------|--------|---------|------------|-----------|
| Linter | ruff | latest | HIGH | Fast, replaces flake8+isort+black |
| Type checker | pyright | latest | MEDIUM | Good Python type checking, optional |
| Testing | pytest | latest | HIGH | Standard Python testing |
| Distribution | pipx/uv tool install | latest | HIGH | Isolated environments, no conflicts |

## What NOT to Use

| Alternative | Why Not |
|-------------|---------|
| sounddevice (instead of pyaudio) | pyaudio is battle-tested in existing code, sounddevice adds numpy dependency |
| whisper.cpp Python bindings | MLX Whisper is faster on Apple Silicon, better maintained |
| pynput (instead of Quartz) | Quartz event tap is more reliable on macOS, already working |
| Tkinter/Qt (instead of AppKit) | Can't do NSVisualEffectView frosted glass, worse macOS integration |
| gRPC (instead of Unix socket) | Overkill for single-machine IPC, adds heavy dependency |
| Docker | macOS audio/permissions don't work in containers |
| faster-whisper | CUDA-focused, not optimized for Apple Silicon |

## Key Version Considerations

- **Python 3.12+**: Required for PyObjC 10.x compatibility and performance improvements
- **MLX Whisper**: Requires Apple Silicon (M1+), no Intel fallback — sherpa-onnx covers this
- **MCP SDK**: Evolving rapidly — pin version, test on updates
- **PyObjC**: Must match macOS SDK version; 10.3+ for macOS 15 Sequoia

## Migration Notes (from existing codebase)

1. **pyproject.toml** needs all dependencies declared (mlx-whisper, sherpa-onnx, pyobjc-framework-Cocoa, pyobjc-framework-Quartz are missing)
2. Consider `uv` instead of `pipx` for installation — faster, better dependency resolution
3. Entry point should be `[project.scripts] vox = "vox.cli:main"` in pyproject.toml
