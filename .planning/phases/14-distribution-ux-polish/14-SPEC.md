# Phase 14: Distribution & UX Polish — Specification

**Created:** 2026-05-11
**Ambiguity score:** 0.13 (gate: ≤ 0.20)
**Requirements:** 6 locked

## Goal

HeyVox installs cleanly via `pip install heyvox` or `brew install heyvox-dev/heyvox/heyvox` on macOS Apple Silicon, ships with a bundled general-purpose "Hey Vox" wake-word model that works for first-time users without training, surfaces the active microphone in the menu bar, and exposes the configured mic-isolation mode in the HUD menu.

## Background

The repo has been public on GitHub since 2026-03-30 with a Beta banner in README, but installation today still requires `git clone` + `pip install -e ".[apple-silicon,chrome]"`. PyPI has the `heyvox` name reserved but only shows a placeholder description. `pyproject.toml` declares `version = "1.0.0"` and `Development Status :: 3 - Alpha` — out of sync with the README's Beta framing. No publish workflow exists (`.github/workflows/` contains `ci.yml` + `install-test.yml` only). No Homebrew formula or tap repository exists.

On the UX side, `heyvox/hud/overlay.py:988-1012` already reads `_active_mic` from `ACTIVE_MIC_FILE` and has a `_friendly_mic()` helper, but the friendly name is only used inside the mic-switcher submenu — the menu-bar title and the pill itself never expose which mic is active. `voice_isolation_mode` exists per-profile (`heyvox/audio/profile.py:64`) but is not surfaced in any UI element.

The wake-word default is still `hey_jarvis_v0.1` from openwakeword's pretrained zoo. A personalized `hey_vox` MLP model exists (memory: `project_wakeword_training.md`) but is trained on the project owner's voice and won't generalize. The Colab pipeline (`retrain_heyvox.py`) is in place and previously produced working ONNX bundles. Without a general-purpose "Hey Vox" model bundled by default, a fresh `pip install heyvox` user would either get the wrong wake word ("hey jarvis") or nothing at all.

## Requirements

1. **PyPI publish workflow**: Tag-triggered GitHub Actions workflow publishes to PyPI via OIDC Trusted Publisher.
   - Current: `.github/workflows/` has `ci.yml` + `install-test.yml` only; no publish workflow; PyPI page shows placeholder "coming soon"
   - Target: New workflow `.github/workflows/publish.yml` triggered by git tags matching `v*` builds wheel + sdist and uploads to PyPI via OIDC (no static API token in repo secrets); PyPI page shows latest release within 5 min of tag push
   - Acceptance: Pushing tag `v1.0.0` (or chosen version) results in PyPI page displaying README content, version, and dependencies; `gh workflow view publish` shows successful run; `pip install heyvox==<version>` from a fresh venv succeeds

2. **pyproject.toml metadata aligned with Beta release**: Classifier bumped, version policy clarified.
   - Current: `version = "1.0.0"`, `Development Status :: 3 - Alpha`; README banner says "Beta"
   - Target: `Development Status :: 4 - Beta`; all classifiers valid per PyPI rules; `version` field becomes the source of truth (single update point), `heyvox/__init__.py` reads from package metadata or is updated to match; first published version is the next tag after merge
   - Acceptance: `pip install heyvox` returns the version declared in `pyproject.toml`; PyPI project page renders README correctly (description, badges, install instructions); classifier filter `Development Status :: 4 - Beta` returns the package on PyPI

3. **Homebrew tap repo + formula**: Separate `heyvox-dev/homebrew-heyvox` repo hosting the formula.
   - Current: No Homebrew formula, no tap repo
   - Target: New public repo `heyvox-dev/homebrew-heyvox` with `Formula/heyvox.rb`; formula declares `depends_on "portaudio"`, `depends_on "python@3.12"`; uses `Language::Python::Virtualenv` to install from PyPI; `on_arm` block ensures Apple Silicon-only ML dependencies (`mlx-whisper`) install; formula version tracks PyPI releases
   - Acceptance: `brew tap heyvox-dev/heyvox && brew install heyvox` succeeds on clean macOS 14+ Apple Silicon; `heyvox --help` after install exits 0; `brew audit --strict heyvox` passes

4. **Active mic name in menu bar**: NSStatusItem surfaces the currently-active microphone.
   - Current: `_active_mic` read from `ACTIVE_MIC_FILE` (`overlay.py:988`); `_friendly_mic()` already strips suffixes; both only used inside the mic-switcher submenu
   - Target: Menu-bar status item title (or tooltip if title-space contested) shows the friendly mic name; updates on mic switch via existing IPC; no visual change to the pill overlay
   - Acceptance: With BT headset connected, hovering or reading the menu-bar status item shows the friendly mic name (e.g. "Evolve2 75"); switching to built-in changes the display to "Built-in"; HUD pill unchanged

5. **Mic isolation mode in HUD menu**: Per-profile `voice_isolation_mode` exposed as menu entry.
   - Current: `voice_isolation_mode: bool | None` defined in `heyvox/audio/profile.py:64` and `heyvox/config.py:462`; never surfaced in UI; macOS Voice Isolation system state is NOT probed
   - Target: HUD dropdown menu shows current mic's `voice_isolation_mode` as a read-only text entry (e.g. "Voice Isolation: On / Off / Auto"); reads strictly from the active profile, never from AVCaptureDevice or system APIs
   - Acceptance: HUD menu entry shows the `voice_isolation_mode` value for the active mic profile; toggling between two profiles with different settings updates the menu entry; no AVCaptureDevice import or call is added

6. **Synthetic "Hey Vox" general wake-word model bundled as default**: Re-trained model + pipeline cleanup + docs.
   - Current: Default wake word in config is `hey_jarvis_v0.1` from openwakeword zoo; personalized `hey_vox` MLP exists but is owner-specific; Colab pipeline + `retrain_heyvox.py` are in place
   - Target: Run synthetic + diverse-voice training pass via existing pipeline; produce `hey_vox.onnx` bundled inside the package (`heyvox/wakeword_models/`); update default config from `hey_jarvis_v0.1` → `hey_vox`; remove dead `hey_jarvis_v0.1` references; document the `retrain_heyvox.py` workflow in `docs/wakeword-training.md` (or README section) so users can train their own
   - Acceptance: Fresh `pip install heyvox` followed by `heyvox setup` + `heyvox start` recognizes "Hey Vox" wake word with ≥70% TP rate on internal test set; bundled model loads without external download; `grep -r hey_jarvis heyvox/ config.yaml.example` returns zero matches in code defaults (only allowed in legacy notes); training docs exist and reference the script

## Boundaries

**In scope:**
- `.github/workflows/publish.yml` with OIDC publishing to PyPI
- `pyproject.toml` classifier bump (Alpha → Beta) and version policy clarification
- New repo `heyvox-dev/homebrew-heyvox` with audited Formula
- Menu-bar active-mic display (NSStatusItem title or tooltip)
- HUD menu entry showing `voice_isolation_mode` (read-only, config-sourced)
- Synthetic "Hey Vox" general wake-word model — training run + ONNX bundling
- Default-config switch `hey_jarvis_v0.1` → `hey_vox` + cleanup of stale references
- Training pipeline documentation (so users can retrain for their own voice)

**Out of scope:**
- Apple Developer Account / code signing / notarization — deferred until `.dmg` release phase (pip + brew don't need signatures)
- `.pkg` / `.dmg` GUI installer — pip + brew is sufficient for v1.x distribution
- AVCaptureDevice probing for actual macOS Voice Isolation state — explicit decision in Round 1; UX-02 reads config only
- Mic name inside the HUD pill itself — explicit decision in Round 3; menu-bar/tooltip only
- TestPyPI dry-run release — direct to production PyPI is acceptable risk
- New wake-word architecture, additional wake words, or multi-language wake-word — only "hey vox" retraining
- Personalized voice-clone training tooling — different feature, not v1.x

**Deferred to future phases / backlog:**
- Cross-platform distribution (Linux/Windows) — XPLAT-01/02 in v2.0
- `.dmg` installer with Sparkle auto-update
- Synthetic wake-word for additional languages

## Constraints

- **Platform**: macOS 14+ Apple Silicon (MLX Whisper requirement, unchanged)
- **Python**: 3.12+ (unchanged)
- **PortAudio**: Required system dependency (brew install portaudio); Homebrew formula must declare `depends_on "portaudio"`
- **PyPI auth**: OIDC Trusted Publisher (no static API tokens in repo secrets); requires one-time setup on PyPI side linking the GitHub workflow
- **ML deps install time**: `mlx-whisper`, `sherpa-onnx` may compile from source on first `pip install`; acceptable but install-test.yml workflow should keep the build green to catch breakage early
- **Wake-word ONNX**: Trained model must be valid openwakeword-compatible ONNX (so existing detection code at `heyvox/audio/wakeword.py` loads it without code changes)
- **Wake-word size**: Bundled model must stay under 5 MB to keep the wheel small
- **Homebrew formula**: Must pass `brew audit --strict` and use `Language::Python::Virtualenv` (not Cellar shenanigans)
- **No regressions in CI**: Existing `ci.yml` and `install-test.yml` must remain green throughout the phase

## Acceptance Criteria

- [ ] Pushing git tag `v1.0.0` (or chosen version) triggers `publish.yml`; PyPI receives the release within 5 min
- [ ] `pip install heyvox` from a fresh venv on macOS 14+ Apple Silicon succeeds within 10 min and installs all runtime deps
- [ ] PyPI project page displays README description, Beta classifier, and current version (not "coming soon")
- [ ] `brew tap heyvox-dev/heyvox && brew install heyvox` succeeds on clean macOS Apple Silicon; `heyvox --help` exits 0
- [ ] `brew audit --strict heyvox-dev/heyvox/heyvox` passes
- [ ] Menu-bar status item shows the friendly active-mic name; updates when mic switches
- [ ] HUD menu entry shows `voice_isolation_mode` of the active mic profile; updates on profile change
- [ ] Fresh install + `heyvox setup` + `heyvox start` recognises "Hey Vox" wake word with ≥70% TP on internal test set
- [ ] `grep -r hey_jarvis_v0.1 heyvox/` returns zero matches in default config (legacy notes/comments allowed)
- [ ] `docs/wakeword-training.md` (or equivalent) explains how a user trains their own wake-word model
- [ ] No AVCaptureDevice import or call added (UX-02 stays config-sourced)
- [ ] Existing `ci.yml` and `install-test.yml` workflows remain green

## Ambiguity Report

| Dimension          | Score | Min  | Status | Notes                                                                 |
|--------------------|-------|------|--------|-----------------------------------------------------------------------|
| Goal Clarity       | 0.88  | 0.75 | ✓      | 6 concrete deliverables, each with measurable target                  |
| Boundary Clarity   | 0.90  | 0.70 | ✓      | Explicit in/out lists; code-signing + AVCaptureDevice excluded        |
| Constraint Clarity | 0.85  | 0.65 | ✓      | OIDC auth, ONNX compat, model size, brew audit all spelled out        |
| Acceptance Criteria| 0.85  | 0.70 | ✓      | 12 pass/fail checkboxes                                               |
| **Ambiguity**      | 0.13  | ≤0.20| ✓      | Gate passed with margin                                               |

## Interview Log

| Round | Perspective              | Question summary                                  | Decision locked                                                      |
|-------|--------------------------|---------------------------------------------------|----------------------------------------------------------------------|
| 1     | Researcher               | PyPI status (Alpha/Beta/Stable)?                  | Beta (4) — aligns with README banner                                 |
| 1     | Researcher               | Homebrew form (own tap repo vs inline)?           | Separate `heyvox-dev/homebrew-heyvox` tap repo                       |
| 1     | Researcher               | UX-02 source (config vs macOS probe)?             | Config-sourced (`voice_isolation_mode` from profile)                 |
| 2     | Researcher + Simplifier  | Code-signing / Apple Dev Account in Phase 14?     | OUT — pip/brew need no signature; defer to .dmg phase                |
| 2     | Researcher + Simplifier  | Synthetic wake-word model in Phase 14?            | IN — without it, fresh installs are unusable                         |
| 2     | Researcher + Simplifier  | PyPI release trigger?                             | Git-tag `v*` push                                                    |
| 3     | Boundary Keeper          | Wake-word scope precision?                        | Training run + pipeline cleanup + user-facing docs                   |
| 3     | Boundary Keeper          | UX-01 placement (pill vs menu-bar vs submenu)?    | Menu-bar title / tooltip; pill stays unchanged                       |

---

*Phase: 14-distribution-ux-polish*
*Spec created: 2026-05-11*
*Next step: `/gsd-discuss-phase 14` — implementation decisions (publish.yml structure, formula details, training run logistics, menu-bar title vs tooltip choice)*
