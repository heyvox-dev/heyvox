# Changelog

All notable changes to HeyVox are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

Launch-readiness hardening for the public release. Not yet published to PyPI.

### Fixed

- Fresh installs now provision the wake-word model automatically, so
  `heyvox start` no longer crashes on a clean install.
- Intel Macs no longer hang ~120 s per dictation — the STT engine defaults to
  `sherpa` on non-Apple-Silicon and fails fast when MLX is unavailable.
- `heyvox doctor` now runs a real diagnostics checklist (previously crashed with
  a missing-module error).

### Added

- `SECURITY.md`, `CONTRIBUTING.md`, and this changelog.
- CI security scanning: gitleaks secret scan (blocking) and pip-audit dependency
  audit (reporting).

### Changed

- Text-to-speech dependency (`mlx-audio`) is now part of the `tts` extra, so
  `pip install 'heyvox[tts]'` yields a working TTS daemon.
- Model downloads are pinned to specific, tested revisions instead of tracking a
  moving `main`.
- Privacy policy (`docs/privacy.html`) now accurately discloses the two optional,
  off-by-default network features (anonymous telemetry; `learn-vocab`).

### Security

- Removed an unauthenticated local TCP port opened by the Hush native host that
  could inject text into the focused browser tab.
- Fixed a Lua string-escaping flaw in the Herald notifier that a crafted
  workspace label / PR title could exploit.
- Documented the MCP HTTP server's loopback-only, single-user-Mac assumption and
  added a guard against binding a non-loopback address.
- Validated the `QWEN_TTS_MODEL` override against a trusted source.

### Removed

- Stale duplicate Chrome extension at the repository root (the canonical copy
  lives in `heyvox/hush/extension/`).
- The opt-in Chrome WebSocket bridge (superseded by the Hush extension).

## [1.0.0]

Initial public release: macOS voice layer for AI coding agents — wake-word
detection, local STT (MLX Whisper / sherpa-onnx), local TTS (Kokoro / Piper),
HUD overlay, MCP integration, and Hush browser media control. Earlier
development history is available in the git log.

[Unreleased]: https://github.com/heyvox-dev/heyvox/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/heyvox-dev/heyvox/releases/tag/v1.0.0
