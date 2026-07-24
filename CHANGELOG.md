# Changelog

All notable changes to HeyVox are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project aims to
follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [1.1.3] - 2026-07-24

STT reliability fixes, a wake-word stop-word retry fix, and the last Conductor-specific
code paths generalized into the per-app profile system.

### Fixed

- STT warm-decode could balloon to 5-8s and still return garbled text; the
  temperature-fallback step is capped again (DEF-195).
- Emphatic word repetition ("viel viel viel viel") no longer gets a whole
  coherent dictation discarded as garbled (DEF-199).
- Auto-glossary no longer inflates short terms via plain substring matching,
  and no longer extracts fake corrections from a garbled wake-word tail
  (DEF-218, DEF-219).
- Repeated stop-word attempts with pauses in between no longer reset the
  wake-word model's accumulated confidence, so "Hey Vox… Hey Vox" reliably
  stops listening (DEF-216).
- A process-wide `SIGCHLD` handler in the audio-cue player no longer wedges
  the Escape / push-to-talk event tap (DEF-220).
- The 30s STT transcription timeout is now real — the code no longer blocks
  on joining an already-wedged worker thread, and an abandoned timeout
  worker can no longer overlap the next transcription (DEF-221).

### Changed

- Removed the remaining Conductor-specific branches from workspace/injection
  code; app behavior is now driven entirely by the generic
  `WorkspaceProvider` protocol and per-app profile flags.

## [1.1.2] - 2026-07-19

Startup and mic-recovery hardening — a wedged init no longer hangs the whole
daemon, and a flaky USB mic no longer gets demoted for 30 minutes.

### Added

- Out-of-process liveness watchdog: catches startup/init wedges (e.g. a
  17-minute GIL-held hang) that happen before the in-process watchdog exists
  (DEF-213).
- HUD banner for output-audio stalls, so a dead output device is visible
  instead of silently logged (DEF-214).

### Fixed

- Audio cues no longer crackle on internal speakers / Bluetooth — cue files
  are resampled to the device's actual rate instead of played raw (DEF-207).
- USB microphones rebuilt onto a gentler, USB-aware recovery/cooldown path
  instead of inheriting the old 30-minute Bluetooth-era cooldown on a
  transient blip (DEF-208, DEF-209, DEF-210, DEF-211).
- A USB mic that briefly drops out of CoreAudio no longer degrades to the
  old 30-minute cooldown (DEF-217).
- HUD overlay restart no longer drops the first state message, which used
  to desync the menu bar until the next transition (DEF-215).

## [1.1.1] - 2026-07-13

Paste-speed and STT/wake-word reliability, plus a lower Kokoro idle memory
footprint.

### Added

- Live transcription progress shown in the HUD.

### Fixed

- Raw markdown ("asterisk", "backtick", "backslash") is no longer spoken
  aloud from prose spliced between literal `<tts>` mentions (DEF-194).
- STT latency, wake-word reliability, and mic self-heal fixes (DEF-188,
  DEF-189, DEF-190, DEF-191).
- Restored the sub-millisecond Accessibility-API paste fast-path for
  Conductor, which had been disabled by a stale workaround — paste is
  roughly 8x faster (DEF-192).
- Kokoro TTS daemon idle RAM usage cut by suppressing an unused PyTorch
  import and trimming warmup/idle behavior (DEF-193).

### Changed

- Bluetooth HFP handling isolated into its own `heyvox.audio.bt` module.

## [1.1.0] - 2026-07-03

Launch-readiness hardening — the first release where a fresh `pip install heyvox`
actually runs, plus security fixes and public-repo hygiene.

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

[Unreleased]: https://github.com/heyvox-dev/heyvox/compare/v1.1.3...HEAD
[1.1.3]: https://github.com/heyvox-dev/heyvox/compare/v1.1.2...v1.1.3
[1.1.2]: https://github.com/heyvox-dev/heyvox/compare/v1.1.1...v1.1.2
[1.1.1]: https://github.com/heyvox-dev/heyvox/compare/v1.1.0...v1.1.1
[1.1.0]: https://github.com/heyvox-dev/heyvox/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/heyvox-dev/heyvox/releases/tag/v1.0.0
