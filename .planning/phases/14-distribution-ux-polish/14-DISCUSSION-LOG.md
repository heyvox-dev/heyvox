# Phase 14: Distribution & UX Polish - Discussion Log

> **Audit trail only.** Do not use as input to planning, research, or execution agents.
> Decisions are captured in CONTEXT.md — this log preserves the alternatives considered.

**Date:** 2026-05-11
**Phase:** 14-distribution-ux-polish
**Areas discussed:** Homebrew depth, Menu-bar title & mic-mode position, Wake-word training, Default-config migration

---

## Homebrew Depth — Formula Generation

| Option | Description | Selected |
|--------|-------------|----------|
| A | `brew create --python <pypi-url>` — auto-scaffold; transitive `resource` blocks must be added manually | |
| B | Hand-written from scratch — 30+ transitive deps manually; max control, max effort | |
| C | `homebrew-pypi-poet` — auto-generates all transitive `resource` blocks; de-facto standard for Python brew formulas | ✓ |

**User's choice:** C — homebrew-pypi-poet
**Notes:** User asked for more context/tradeoffs first; after seeing that A still requires manual enumeration of 30+ resource blocks and B is even more tedious, picked C as the standard-tooling path.

## Homebrew Depth — Formula Update Strategy

| Option | Description | Selected |
|--------|-------------|----------|
| Manual PR in tap repo after each release | Simple, controlled | ✓ |
| GitHub Action with `dawidd6/action-homebrew-bump-formula` | Auto-PR on PyPI release | |
| Main-repo Action that commits to tap repo | Fully automated | |

**User's choice:** Manual PR
**Notes:** Solo-maintainer context — manual PR keeps complexity low for v1; auto-bump can be added later if cadence demands it.

## Homebrew Depth — ML Deps Install Time

| Option | Description | Selected |
|--------|-------------|----------|
| Accept slow `pip install` from PyPI wheels (~5-10 min) | No custom infra | |
| Explicit README warning about install time | Same install path, set expectations | ✓ |
| Optional ML stack (`brew install heyvox --with-ml`) | Smaller default install | |

**User's choice:** Explicit README warning
**Notes:** Apple-Silicon wheels exist on PyPI, just ~200MB to download. Warning manages expectations without adding install-flag complexity.

---

## Menu-bar Title & Mic-mode Position — Mic-name Placement

| Option | Description | Selected |
|--------|-------------|----------|
| Friendly mic name in title (e.g. `🎤 Evolve2`) | Always visible, may be long | |
| Tooltip on hover only | Title stays clean, name on demand | |
| Both — title truncates to 8-10 chars + tooltip with full name | Compact title + full name discoverable | ✓ |
| Dropdown header (NSMenu first row) instead of title | Title stays clean, visible on click | |

**User's choice:** Both — title truncates to 8-10 chars + tooltip
**Notes:** Balances visibility (always-on title hint) with cleanliness (no BT-name overflow).

## Menu-bar Title & Mic-mode Position — Voice Isolation Indicator Placement

| Option | Description | Selected |
|--------|-------------|----------|
| Top-level entry below mic name | Highly visible | |
| Inside mic-switcher submenu — each entry suffixes its mode | Grouped with the mic it belongs to | ✓ |
| Both | Maximum surface | |

**User's choice:** Inside mic-switcher submenu
**Notes:** Per-mic data lives with the per-mic UI — no duplication.

---

## Wake-Word Training — Training Environment

| Option | Description | Selected |
|--------|-------------|----------|
| Colab (`training/hey_vox_colab.ipynb` + Drive `heyvox_training_checkpoints/`) | Proven path, reproducible, data already there | ✓ |
| Local MLX | Faster on Apple Silicon, no upload, but overfit risk shown in prior runs | |
| Hybrid — Colab for ship, local for iteration | | |

**User's choice:** Colab
**Notes:** User asked for tradeoffs context first; after reviewing prior training-run results (memory shows Colab MLP is the active model; local conv-attention overfit at 74-92% recall), confirmed Colab for the ship-run. Local pipeline stays available for future iteration but is not the ship path.

## Wake-Word Training — Test Set Source

| Option | Description | Selected |
|--------|-------------|----------|
| Synthetic via Kokoro/Qwen TTS | High volume, diverse voices, reproducible | |
| Real recordings from `record.felberer.at` | Real voices, smaller pool | |
| Hybrid (both) | Volume + reality check | ✓ |

**User's choice:** Hybrid
**Notes:** Aligns with the 76-real-samples-are-insufficient finding from prior training runs.

## Wake-Word Training — FP Rate Threshold

| Option | Description | Selected |
|--------|-------------|----------|
| < 1 FP per hour of normal speech | Strict, less annoying | ✓ |
| < 3 FP per hour | More room for TP, possibly annoying | |
| Subjective only (1 day dogfooding) | Faster ship | |

**User's choice:** < 1 FP per hour
**Notes:** Matches the prior French-speech-FP problem; tight gate prevents shipping a regression.

## Wake-Word Training — Model Storage Path

| Option | Description | Selected |
|--------|-------------|----------|
| `heyvox/wakeword_models/hey_vox.onnx` (in wheel) | Bundled, no extra download | |
| `heyvox/data/wakeword/hey_vox.onnx` (generic data dir) | Future-proof for more models | |
| GitHub Releases asset, downloaded by `heyvox setup` | Wheel stays small | ✓ |

**User's choice:** GitHub Releases + setup downloads
**Notes:** Keeps the published wheel small (~1MB vs ~5MB with model). One-time download on first setup.

---

## Default-Config Migration — Wake-Word Setup

| Option | Description | Selected |
|--------|-------------|----------|
| Only update `config.yaml.example`, leave user configs alone | No surprises | |
| Auto-migrate on next start with warning + backup | Friendlier for users | |
| On-start prompt — explicit Y/n | Maximum transparency | |

**User's choice:** Free-text — "both wake words should remain possible (hey_jarvis is maybe better). There are probably no existing users."
**Notes:** User reframed the question — instead of jarvis-vs-vox, both stay active. Ships with `wake_words: [hey_vox, hey_jarvis_v0.1]`. No existing users means no migration logic needed. Triggered an amendment to SPEC.md Requirement 6 + acceptance criterion #8 to reflect this co-default approach.

## Default-Config Migration — Personalized Model Handling

| Option | Description | Selected |
|--------|-------------|----------|
| Keep personalized `hey_vox.onnx`, name new general as `hey_vox_general.onnx` | | |
| Rename personalized to `hey_vox.user.onnx`, general becomes `hey_vox.onnx` | | |
| Setup asks before overwriting | | |

**User's choice:** (no direct answer — answered via follow-up question)
**Notes:** Folded into D-19 — setup leaves existing `hey_vox.onnx` untouched if present (preserves personalized models); `--redownload-wakeword` flag for forced refresh.

## Default-Config Migration — Out-of-the-Box Behavior

| Option | Description | Selected |
|--------|-------------|----------|
| Both active: `wake_words: [hey_vox, hey_jarvis_v0.1]` — hey_jarvis fallback | | ✓ |
| Only hey_jarvis as default, hey_vox opt-in | | |
| Only hey_vox as default, hey_jarvis opt-in | | |

**User's choice:** Both active
**Notes:** Confirms the reframe above.

## Default-Config Migration — Setup Behavior

| Option | Description | Selected |
|--------|-------------|----------|
| Setup downloads general-hey_vox.onnx — out-of-the-box branding | | ✓ |
| Setup does not download — hey_vox is advanced-only | | |
| Opt-in via `--with-heyvox-wakeword` | | |

**User's choice:** Setup downloads
**Notes:** Brand reasoning — first-run users get "Hey Vox" working immediately.

---

## Claude's Discretion

- Wheel/sdist build flags in `publish.yml` (default `python -m build` unless proven otherwise)
- Homebrew formula `test do` block contents
- Menu-bar truncation algorithm (which 8-10 chars, smart-trim heuristics)
- Tooltip implementation method (NSStatusBarButton.toolTip)
- Mic-submenu D-13 entry text format (separator, glyph)
- Colab notebook cleanup before public ship
- GitHub Releases asset upload mechanism (first time manual, automation later if friction)
- sha256 strategy for downloaded model (constants file vs GitHub API query)
- README "Customize wake word" section copy + placement

## Deferred Ideas

- Apple Developer Account + code-signing + notarization — future `.dmg` phase
- `.dmg` / `.pkg` GUI installer — separate distribution channel
- Auto-bump Homebrew formula on PyPI release (`dawidd6/action-homebrew-bump-formula`) — defer until manual PR friction shows
- Custom prebuilt wheels for ML deps on GitHub Releases — only if PyPI install times complaints surface
- AVCaptureDevice live-probe for actual macOS Voice Isolation state — explicitly out of scope
- Mic name in HUD pill itself — pill stays clean
- Auto-migration of legacy `hey_jarvis_v0.1` user configs — moot for Phase 14 (no users yet)
- `heyvox setup --with-heyvox-wakeword` opt-in flag — rejected in favor of always-download
- TestPyPI dry-run release — straight to production PyPI
- Synthetic wake-word for additional languages — wake-word stays English-only in v1.x
