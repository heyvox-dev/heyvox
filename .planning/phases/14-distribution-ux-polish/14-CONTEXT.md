# Phase 14: Distribution & UX Polish - Context

**Gathered:** 2026-05-11
**Status:** Ready for planning

<domain>
## Phase Boundary

Ship HeyVox as a real installable package on PyPI and Homebrew, surface the active microphone and its isolation mode in the HUD menu bar, and bundle a general-purpose "Hey Vox" wake-word model so first-time users get branding out of the box. Code-signing, .dmg installers, and AVCaptureDevice probing remain out of scope (see SPEC.md).

</domain>

<spec_lock>
## Requirements (locked via SPEC.md)

**6 requirements are locked.** See `14-SPEC.md` for full requirements, boundaries, and acceptance criteria.

Downstream agents MUST read `14-SPEC.md` before planning or implementing. Requirements are not duplicated here.

**In scope (from SPEC.md):**
- `.github/workflows/publish.yml` with OIDC publishing to PyPI
- `pyproject.toml` classifier bump (Alpha → Beta) and version policy clarification
- New repo `heyvox-dev/homebrew-heyvox` with audited Formula
- Menu-bar active-mic display (NSStatusItem title or tooltip)
- HUD menu entry showing `voice_isolation_mode` (read-only, config-sourced)
- Synthetic "Hey Vox" general wake-word model — training run + ONNX bundling
- Default-config switch + cleanup of stale references (see D-13 below — scope adjusted: jarvis remains as fallback, not removed)
- Training pipeline documentation (so users can retrain for their own voice)

**Out of scope (from SPEC.md):**
- Apple Developer Account / code signing / notarization — deferred until `.dmg` release phase
- `.pkg` / `.dmg` GUI installer
- AVCaptureDevice probing for actual macOS Voice Isolation state — UX-02 reads config only
- TestPyPI dry-run release — direct to production PyPI
- Mic name inside the HUD pill itself — only menu bar/tooltip
- New wake-word architecture, additional wake words beyond hey_vox retraining

⚠ **SPEC.md amendment required:** Requirement 6 currently states "remove dead hey_jarvis_v0.1 references" and acceptance criterion #8 says "grep -r hey_jarvis_v0.1 returns zero matches in default config". The discussion landed on keeping hey_jarvis as a co-default fallback (D-13). Before planning starts, SPEC.md Requirement 6 should be amended to reflect "default config ships with `wake_words: [hey_vox, hey_jarvis_v0.1]`; hey_jarvis remains as a known co-default fallback, not removed".

</spec_lock>

<decisions>
## Implementation Decisions

### PyPI Publish Pipeline
- **D-01:** Publish workflow lives at `.github/workflows/publish.yml`, triggered by `push` events on tags matching `v*` (semver tags). Manual `workflow_dispatch` is not added for v1 — keep the surface narrow.
- **D-02:** Authentication via PyPI OIDC Trusted Publisher — no API token stored in repo secrets. Requires one-time setup on PyPI side linking the publisher to this workflow file path.
- **D-03:** Workflow builds wheel + sdist via `python -m build`, uploads via `pypa/gh-action-pypi-publish@release/v1`. Build runs on `macos-14` to match the install-test target.
- **D-04:** Version source of truth is `pyproject.toml`'s `[project] version` field. `heyvox/__init__.py:__version__` reads from `importlib.metadata.version("heyvox")` so there is no manual sync drift.
- **D-05:** First Phase 14 release ships as v1.0.0 (PyPI page is currently empty placeholder; the existing `version = "1.0.0"` in pyproject is the canonical first-ship version). Subsequent releases bump per semver.

### pyproject.toml Metadata
- **D-06:** Classifier bumps to `Development Status :: 4 - Beta` — matches README banner.
- **D-07:** `readme = "README.md"` already set; `long_description_content_type` is implicit via PEP 621 readme field. No separate PyPI-specific README — the existing README renders correctly on PyPI (GitHub anchor links degrade gracefully). Verify rendering after first publish; fix only if visibly broken.

### Homebrew Formula
- **D-08:** Tap repo: separate `heyvox-dev/homebrew-heyvox` repository (per SPEC). Created manually before the formula is written; formula lives at `Formula/heyvox.rb`.
- **D-09:** Formula generation: **`homebrew-pypi-poet`** for resource enumeration (`pip install homebrew-pypi-poet && poet heyvox > resources.rb`). The 30+ transitive deps (openwakeword, mlx-whisper, sherpa-onnx, pyobjc-*, mcp, pydantic, etc.) are auto-extracted with correct url+sha256+version. The `class Heyvox` wrapper, install block, test block, and `depends_on` (portaudio, python@3.12) are hand-written.
- **D-10:** Formula update strategy on new PyPI releases: **manual PR** in the tap repo by the maintainer (no auto-bump action for v1 — keep moving parts low).
- **D-11:** ML deps (mlx-whisper, sherpa-onnx) install via PyPI's prebuilt Apple-Silicon wheels — no custom wheel hosting in Phase 14. README adds an explicit warning: "first install can take 5-10 min for ML dependencies (~200MB download)".

### HUD Menu Bar — UX-01 + UX-02
- **D-12:** Menu-bar status item title: shows the **friendly mic name truncated to 8-10 characters**, prefixed by the existing state icon. Example: `🎤 Evolve2 75` → if too long, `🎤 Evolve2 7…`. Full friendly name lives in the **NSStatusItem tooltip** (hover reveal). Pill overlay remains untouched.
- **D-13:** Voice-isolation indicator (UX-02) is rendered **inside the existing mic-switcher submenu** — each mic entry suffixes its `voice_isolation_mode` value (e.g. "Evolve2 75 ✓ — Voice Isolation: On", "Built-in — Voice Isolation: Off"). Reads strictly from the active profile's `voice_isolation_mode` field (Phase 13 D-02). Never imports AVCaptureDevice or probes macOS state.

### Wake-Word Training + Bundling
- **D-14:** Training environment: **Google Colab** (`training/hey_vox_colab.ipynb` + `retrain_heyvox.py` on Drive folder `heyvox_training_checkpoints/`). Proven path — the currently shipping MLP came from there. Local MLX pipeline stays in `training/` as experimental, not used for ship.
- **D-15:** Test set: **hybrid** — synthetic clips via Kokoro/Qwen TTS for volume + diverse voices, plus real-voice clips collected via `record.felberer.at` web recorder. Synthetic fills the gap, real catches overfit.
- **D-16:** Ship gate: **TP ≥ 70%** (per SPEC) AND **FP < 1 per hour of normal speech**. Both thresholds must pass on the test set before the model is uploaded to GitHub Releases.
- **D-17:** Model storage: **GitHub Releases asset** on `heyvox-dev/heyvox` (filename `hey_vox.onnx`, attached to each release). `heyvox setup` downloads it to `~/.config/heyvox/models/hey_vox.onnx` on first run. The wheel itself stays small (no ML model bundled inside the package).
- **D-18:** Default config ships with `wake_words: [hey_vox, hey_jarvis_v0.1]` — both active, jarvis as silent fallback if hey_vox detection misses. Single-wake-word setups remain opt-in by user editing config.
- **D-19:** `heyvox setup` first-run flow: detects missing `~/.config/heyvox/models/hey_vox.onnx`, downloads from GitHub Releases (HTTPS, validates sha256 against release asset metadata), shows progress. If existing file present: leaves it untouched (user-trained personalized model is preserved). To force re-download: `heyvox setup --redownload-wakeword` flag.
- **D-20:** Training pipeline documentation lives in **`docs/wakeword-training.md`** — references the Colab notebook, explains data flow (synthetic + real samples), shows how to validate before swapping models. Linked from README's "Customize wake word" section.

### Default Config Migration
- **D-21** [informational]: No migration logic needed — no existing PyPI/Brew users exist yet (Phase 14 is the launch). The `config.yaml.example` ships with the new default; setup wizard writes a fresh config on first install. Existing source-install users (the maintainer only) update their config manually. *No plan task required — the decision is to do nothing.*

### Claude's Discretion
- Exact wheel/sdist build flags in `publish.yml` (cibuildwheel? plain build? — keep it simple, default to `python -m build`)
- Homebrew formula `test do` block contents (likely just `heyvox --help` + version assert)
- Menu-bar truncation algorithm details (which 8-10 chars — first N, last N, smart-trim) — pick what reads well on common BT names
- Tooltip implementation method (NSStatusBarButton has `toolTip` property — straight forward)
- Mic-submenu entry text format for D-13 (exact separator, capitalization)
- Colab notebook cleanup — what to leave in for users, what to strip
- GitHub Releases asset upload mechanism (manual `gh release upload` first time, automation later if friction shows)
- Hashing strategy for the downloaded model (sha256 baked into a constants file vs queried from GitHub API)
- README "Customize wake word" section copy + placement

</decisions>

<canonical_refs>
## Canonical References

**Downstream agents MUST read these before planning or implementing.**

### Phase Spec (locked requirements)
- `.planning/phases/14-distribution-ux-polish/14-SPEC.md` — Locked requirements, boundaries, acceptance criteria. MUST read before planning.

### Distribution & Packaging
- `pyproject.toml` — current metadata, version, classifiers, dependencies
- `.github/workflows/ci.yml` — existing CI pattern (macos-14, ruff, pytest), reuse setup steps
- `.github/workflows/install-test.yml` — clean-install validation pattern, useful as smoke test after publish
- `README.md` — package description rendered on PyPI; "first install can take 5-10 min" warning to be added here
- PyPI OIDC docs: https://docs.pypi.org/trusted-publishers/adding-a-publisher/ — one-time setup before workflow can succeed

### Homebrew Formula
- `homebrew-pypi-poet` PyPI page: https://pypi.org/project/homebrew-pypi-poet/ — resource generation tool
- Homebrew "Python for Formula Authors" cookbook: https://docs.brew.sh/Python-for-Formula-Authors
- `brew audit --strict` is the formula validation gate

### HUD / Menu Bar
- `heyvox/hud/overlay.py:988-1012` — `_active_mic` read + `_friendly_mic()` helper, already used in submenu
- `heyvox/hud/overlay.py:324` `_STATUS_LABELS` — state icon mapping, needed to compose new title
- `heyvox/hud/overlay.py:428` — current title set (`status_item.button().setTitle_(_bar_title)`); target for D-12 changes
- `heyvox/hud/overlay.py:1745-1757` — status_item creation, where tooltip wiring goes
- `heyvox/constants.py` — `ACTIVE_MIC_FILE` constant
- `heyvox/audio/profile.py:64` — `voice_isolation_mode: bool | None` field on profile dataclass
- `heyvox/config.py:462` — `voice_isolation_mode` on config model

### Wake-Word
- `training/hey_vox_colab.ipynb` — current Colab notebook (entry point for training run)
- `training/train_model.py`, `training/train_livekit.py`, `training/download_negatives.py` — local pipelines (experimental, not the ship path)
- `heyvox/audio/wakeword.py` — openwakeword loader; consumes the downloaded ONNX
- `heyvox/audio/training_collector.py` — auto-collects positive clips for future iterations
- `heyvox/setup/wizard.py` — extension point for D-19 (setup downloads model)
- Google Drive folder `heyvox_training_checkpoints/` (ID `1DZ02RE8zZiU4r6LkyMTa_ofYRrZezzcu`) — training feature checkpoints (~3.5GB), `retrain_heyvox.py`

### Prior Phase Decisions Carried Forward
- `.planning/phases/13-audio-reliability/13-CONTEXT.md` D-02 — `voice_isolation_mode` is a per-mic-profile field, set in `mic_profiles:` config block. UX-02 reads from this.
- `.planning/phases/13-audio-reliability/13-CONTEXT.md` D-03 — Config.yaml overrides always take priority over cache. UX-02 must respect this — read the active profile, not stale cache.

### Codebase Maps
- `.planning/codebase/STACK.md` — Python 3.12+, PyObjC, openwakeword, MLX Whisper, sherpa-onnx, Kokoro, FastMCP
- `.planning/codebase/STRUCTURE.md` — monorepo layout (heyvox/, herald/, hush/)
- `.planning/codebase/ARCHITECTURE.md` — voice IN / voice OUT split, IPC boundaries

</canonical_refs>

<code_context>
## Existing Code Insights

### Reusable Assets
- `_friendly_mic(name)` helper in `overlay.py:1000-1011` — already strips "MacBook Pro Microphone" → "Built-in" and common suffixes. Reuse for the truncated menu-bar title (D-12) and for the submenu entries (D-13).
- `_STATUS_LABELS` dict in `overlay.py:324` — state icon mapping; new title format is `{icon} {truncated_mic_name}`.
- `NSStatusBarButton.setToolTip_()` — standard PyObjC API; wire the full friendly mic name here for D-12.
- `heyvox/setup/wizard.py` — already orchestrates first-run setup steps (permissions, hooks, MCP); natural extension point for the wake-word model download (D-19).
- `heyvox/audio/wakeword.py` — already loads custom ONNX with `inference_framework="onnx"` (from memory `project_wakeword_training.md`). The downloaded `hey_vox.onnx` slots in without code changes.
- `.github/workflows/install-test.yml` — clean-install harness already validates `pip install` end-to-end; same shape applies to the post-publish smoke test.

### Established Patterns
- Pydantic config (`HeyvoxConfig`) with YAML backing — `config.yaml.example` is the seed users edit.
- Per-mic profile pattern (Phase 13) — `voice_isolation_mode` already lives on the profile, no new data model needed for UX-02.
- IPC via flag files / Unix sockets (`ACTIVE_MIC_FILE`, `/tmp/heyvox-recording`, `/tmp/heyvox-hud.sock`) — the active-mic IPC is already wired; D-12 only adds a new consumer.
- CI on `macos-14` (`.github/workflows/ci.yml`) — same runner for the publish workflow.

### Integration Points
- `overlay.py:428` (status item title) is the single line to amend for D-12. The truncation logic should live in a small helper (`_format_bar_title(state, mic_name, max_len)`) for testability.
- `overlay.py` mic-switcher submenu (~lines 1010-1100, where `_mic_short` is currently used) — extend the menu-item title to append `voice_isolation_mode` per D-13. Read the value from the profile registry (Phase 13 D-02), not from a global config flag.
- `heyvox/setup/wizard.py` — add a `_download_wakeword_model()` step gated on missing `~/.config/heyvox/models/hey_vox.onnx`. Use `urllib.request` (no new dependency); validate sha256.
- `pyproject.toml` field `classifiers` — single-line bump from `3 - Alpha` to `4 - Beta`.
- New file `.github/workflows/publish.yml` — model after `ci.yml` for environment setup, then add the `pypa/gh-action-pypi-publish` step.
- New repo `heyvox-dev/homebrew-heyvox` — outside this checkout; created via `gh repo create heyvox-dev/homebrew-heyvox --public`. Formula lives there; not committed to this repo.
- New file `docs/wakeword-training.md` — companion doc for D-20; links from README and from the Colab notebook header.

</code_context>

<specifics>
## Specific Ideas

- Truncation example for D-12: "Evolve2 75 UC" → "Evolve2 75" (10 chars); "AirPods Pro" → "AirPods Pr" or smarter trim to word boundary "AirPods" if shorter looks cleaner.
- Mic-submenu D-13 format suggestion: "Evolve2 75  •  VI: On" (compact) — exact glyph/separator at Claude's discretion.
- For D-19, the GitHub Releases asset URL pattern is stable: `https://github.com/heyvox-dev/heyvox/releases/download/v{version}/hey_vox.onnx` — easy to template.
- `heyvox setup --redownload-wakeword` (D-19) lets the maintainer iterate without nuking the user's models dir.
- Wake-word training Pass-Gate: TP ≥ 70% (SPEC requirement 6) + FP < 1/hour (D-16). Both must hold on the hybrid synthetic+real test set.

</specifics>

<deferred>
## Deferred Ideas

- **Apple Developer Account + code-signing + notarization** — required only for `.dmg` / `.pkg` distribution; pip + brew don't need it. Picks up in a future "GUI installer" phase.
- **`.dmg` / `.pkg` GUI installer** — separate distribution channel; not in v1.x.
- **Auto-bump Homebrew formula on PyPI release** (e.g., `dawidd6/action-homebrew-bump-formula`) — defer until manual PR friction shows up. Probably v1.2+ once release cadence is known.
- **Custom prebuilt wheels for ML deps hosted on GitHub Releases** — only if PyPI install times become a real complaint. Today: README warning is enough.
- **AVCaptureDevice live-probe for actual macOS Voice Isolation state** — would surface the real macOS toggle, but requires private API or AVFoundation guesswork. Explicitly out of scope for UX-02 (SPEC + this discussion).
- **Mic name in HUD pill itself** — explicit decision in spec-phase Round 3 to keep the pill clean. Stays deferred.
- **Auto-migration of legacy `hey_jarvis_v0.1` user configs** — moot for Phase 14 (no users exist yet). If user reports surface post-launch, add a one-shot migration in a follow-up.
- **`heyvox setup --with-heyvox-wakeword` opt-in flag** — rejected in favor of always-download (D-19). Could revive if model size grows.
- **TestPyPI dry-run release** — Round 2 decision to ship straight to production. Revisit only if a botched publish happens.
- **Synthetic wake-word for additional languages** — wake-word stays English-only in v1.x.

</deferred>

---

*Phase: 14-distribution-ux-polish*
*Context gathered: 2026-05-11*
