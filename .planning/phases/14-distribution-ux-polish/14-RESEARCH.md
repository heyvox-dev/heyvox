# Phase 14: Distribution & UX Polish — Research

**Researched:** 2026-05-11
**Domain:** Python package distribution (PyPI + Homebrew), macOS menu-bar UX, wake-word ML training
**Confidence:** HIGH for PyPI/Homebrew/HUD; MEDIUM for wake-word ship gate (depends on quality of training data, not infra)

## Summary

Phase 14 is **plumbing-heavy, not architecture-heavy**. Each of the six requirements maps to a well-trodden pattern with active 2024–2026 tooling — PyPI OIDC publishers via `pypa/gh-action-pypi-publish@release/v1`, Homebrew `Language::Python::Virtualenv` formulae with `brew update-python-resources` (official tool, replaces unmaintained `homebrew-pypi-poet`), NSStatusBarButton `setToolTip_` for menu-bar mic surfacing, and an in-place `_friendly_mic()` helper that already lives in `overlay.py`. The wake-word training pipeline (Colab + `train_model.py` + `retrain_heyvox.py`) is already proven — the work is a fresh diverse-voice training run, not a new pipeline.

The single area of real risk is the **wake-word ship gate**: TP ≥ 70% AND FP < 1/hour. Hitting both on the hybrid (synthetic + real-voice) test set is feasible — the prior `hey_jarvis_v0.1` shipped by openwakeword targets `target_fp_per_hour: 0.2` and the existing `colab_hey_vox.py` targets the same — but the gate must be measured on a held-out corpus the model has never seen, not the training set. Reusing openwakeword's published validation corpus (DiPCo + Santa Barbara + MUSDB, ~11 hours) gives a defensible FP number; reusing the personal recordings + `record.felberer.at` clips gives the TP number.

The PyPI side has one **one-time maintainer setup step** that must happen *before* the first tag push: register a **pending publisher** on PyPI for project `heyvox` (since the name is reserved but never published). This is a click-through form on the maintainer's PyPI account, not a CI step. If skipped, the first `publish.yml` run fails with "no trusted publisher matches this token."

**Primary recommendation:** Sequence the phase as (1) PyPI infra → (2) HUD changes → (3) wake-word training run → (4) setup-download wiring → (5) Homebrew tap. PyPI first because it's the longest-lead one-time setup. Wake-word training in parallel with HUD work — Colab runs independently. Homebrew last because the formula's `resource` block is generated from the published wheel.

## User Constraints (from CONTEXT.md)

### Locked Decisions (from CONTEXT.md `## Decisions`)

**PyPI Publish Pipeline:**
- **D-01:** Publish workflow lives at `.github/workflows/publish.yml`, triggered by `push` events on tags matching `v*` (semver tags). Manual `workflow_dispatch` is not added for v1.
- **D-02:** Authentication via PyPI OIDC Trusted Publisher — no API token stored in repo secrets. Requires one-time setup on PyPI side linking the publisher to this workflow file path.
- **D-03:** Workflow builds wheel + sdist via `python -m build`, uploads via `pypa/gh-action-pypi-publish@release/v1`. Build runs on `macos-14`.
- **D-04:** Version source of truth is `pyproject.toml`'s `[project] version` field. `heyvox/__init__.py:__version__` reads from `importlib.metadata.version("heyvox")`.
- **D-05:** First Phase 14 release ships as v1.0.0.

**pyproject.toml Metadata:**
- **D-06:** Classifier bumps to `Development Status :: 4 - Beta`.
- **D-07:** `readme = "README.md"` already set; no separate PyPI-specific README.

**Homebrew Formula:**
- **D-08:** Tap repo: separate `heyvox-dev/homebrew-heyvox` repository; formula lives at `Formula/heyvox.rb`.
- **D-09:** Resource enumeration via `homebrew-pypi-poet` (OR `brew update-python-resources` — see Risks).
- **D-10:** Formula update on new PyPI releases: **manual PR** in the tap repo. No auto-bump action for v1.
- **D-11:** ML deps install via PyPI's prebuilt Apple-Silicon wheels — no custom wheel hosting.

**HUD Menu Bar:**
- **D-12:** Menu-bar status item title: friendly mic name truncated to 8–10 chars, prefixed by state icon. Full friendly name in tooltip.
- **D-13:** Voice-isolation indicator rendered inside existing mic-switcher submenu; reads strictly from active profile.

**Wake-Word Training + Bundling:**
- **D-14:** Training environment: Google Colab (`training/hey_vox_colab.ipynb` + `retrain_heyvox.py`). Local MLX pipeline stays experimental.
- **D-15:** Test set: hybrid — synthetic (Kokoro/Qwen TTS) + real-voice (`record.felberer.at`).
- **D-16:** Ship gate: TP ≥ 70% AND FP < 1 per hour on hybrid test set.
- **D-17:** Model storage: GitHub Releases asset on `heyvox-dev/heyvox` (filename `hey_vox.onnx`). Downloaded to `~/.config/heyvox/models/hey_vox.onnx` on first `heyvox setup`.
- **D-18:** Default config ships `wake_words: [hey_vox, hey_jarvis_v0.1]` — both active, jarvis fallback.
- **D-19:** Setup wizard downloads model if absent; preserves user-trained model. `--redownload-wakeword` flag forces refresh.
- **D-20:** Training docs live in `docs/wakeword-training.md`.

**Default Config Migration:**
- **D-21:** No migration logic needed — no existing PyPI/Brew users yet.

### Claude's Discretion (from CONTEXT.md)
- Wheel/sdist build flags in `publish.yml` — default to `python -m build`
- Homebrew formula `test do` block — likely just `heyvox --help` + version assert
- Menu-bar truncation algorithm — pick what reads well on common BT names
- Tooltip implementation method — `NSStatusBarButton.setToolTip_` straightforward
- Mic-submenu entry text format for D-13 — separator + capitalization
- Colab notebook cleanup
- GitHub Releases asset upload mechanism — manual `gh release upload` first time
- Hashing strategy — sha256 baked into a constants file vs queried from GitHub API
- README "Customize wake word" section copy + placement

### Deferred Ideas (OUT OF SCOPE — from CONTEXT.md)
- Apple Developer Account + code-signing + notarization
- `.dmg` / `.pkg` GUI installer
- Auto-bump Homebrew formula on PyPI release
- Custom prebuilt wheels for ML deps hosted on GitHub Releases
- AVCaptureDevice live-probe for macOS Voice Isolation
- Mic name in HUD pill itself
- Auto-migration of legacy `hey_jarvis_v0.1` user configs
- `heyvox setup --with-heyvox-wakeword` opt-in flag — rejected in favor of always-download
- TestPyPI dry-run
- Synthetic wake-word for additional languages

## Phase Requirements

| ID | Description | Research Support |
|----|-------------|------------------|
| SPEC-R1 | PyPI publish workflow via OIDC Trusted Publisher | Sections "PyPI OIDC Publish Workflow", "Code Examples" |
| SPEC-R2 | pyproject.toml metadata aligned with Beta release | Sections "pyproject.toml Migration", "Code Examples" |
| SPEC-R3 | Homebrew tap repo + formula | Sections "Homebrew Formula Authoring", "Code Examples" |
| SPEC-R4 | Active mic name in menu bar | Sections "HUD Menu-Bar Mic Display", "Code Examples" |
| SPEC-R5 | Mic isolation mode in HUD submenu | Section "HUD Submenu — Voice Isolation Indicator" |
| SPEC-R6 | Synthetic Hey Vox wake-word + co-default fallback | Sections "Wake-Word Training Logistics", "Validation Architecture" |

## Architectural Responsibility Map

| Capability | Primary Tier | Secondary Tier | Rationale |
|------------|-------------|----------------|-----------|
| PyPI publish workflow | CI / GitHub Actions | — | Workflow runs on GitHub-hosted runner; not user-facing |
| pyproject.toml metadata | Build system | Python runtime (importlib.metadata) | Single source of truth for version + classifiers |
| Homebrew formula | Distribution layer (separate repo) | — | Tap lives outside main repo; no user code involved |
| Menu-bar mic title + tooltip | HUD process (overlay.py) | IPC consumer (reads ACTIVE_MIC_FILE) | Display-only; no audio path touched |
| Mic isolation submenu indicator | HUD process (overlay.py) | Config (MicProfileEntryConfig) | Read-only display of profile field |
| Wake-word model download | Setup wizard (heyvox/setup/wizard.py) | Filesystem (~/.config/heyvox/models/) | One-shot install step, not runtime path |
| Wake-word model loading | Audio loop (heyvox/audio/wakeword.py) | openwakeword library | Already wired; only needs new .onnx in known search path |
| Default config defaults | Config layer (heyvox/config.py) | — | Pure data change, no behavior change |

**Why this matters:** None of the changes cross a tier boundary the codebase doesn't already cross. The setup-wizard download step is the only new IPC-adjacent surface (HTTPS to GitHub Releases) — everything else is config tweaks or display strings.

## Standard Stack

### Core (verified)
| Library / Tool | Version | Purpose | Why Standard |
|----------------|---------|---------|--------------|
| `pypa/gh-action-pypi-publish` | `release/v1` (rolling) | PyPI upload via OIDC | The pypa-blessed action; only one supported by Trusted Publishers |
| `python -m build` | 1.4.2 (verified locally) | Build wheel + sdist | PEP 517 reference frontend, replaces deprecated `setup.py sdist bdist_wheel` |
| `actions/setup-python@v5` | v5 | Python toolchain on runner | Standard GHA pattern; already used by `ci.yml` |
| `actions/checkout@v4` | v4 | Repo checkout | Already used by `ci.yml` |
| `actions/upload-artifact@v4` / `actions/download-artifact@v4` | v4 | Pass dist/ between build and publish jobs | Pypa's recommended two-job pattern |
| `brew update-python-resources` | bundled with Homebrew (5.1.7 verified locally) | Generate `resource` stanzas for Python formula | Official Homebrew tool; replaces unmaintained `homebrew-pypi-poet` |
| `homebrew-pypi-poet` | 0.10.0 (last released 2018-02; STILL FUNCTIONAL) | Fallback resource generator | Listed in CONTEXT.md D-09; works but unmaintained |
| `openwakeword` | ≥0.6.0 (existing dep) | Wake-word inference; loads custom .onnx | Already in `pyproject.toml`; bundles melspectrogram + embedding + VAD models (downloads from openwakeword v0.5.1 GH Release on first use) |
| `gh` CLI | 2.87.3 (verified locally) | Release asset upload + repo create | Standard for `gh repo create heyvox-dev/homebrew-heyvox --public` and `gh release upload v1.0.0 hey_vox.onnx` |
| `rich` ≥13.0 (existing dep) | — | Setup wizard progress bars (sha256 download) | Already in `pyproject.toml`; setup wizard already uses it |
| `urllib.request` | stdlib | Download model from GitHub Releases | No new dep needed; setup wizard guidance from CONTEXT.md `<code_context>` |
| `hashlib.sha256` | stdlib | Model integrity check | Standard; pairs with GitHub API `.assets[].digest` field |

### Wake-word training stack (existing, in `training/`)
| Library | Purpose | Notes |
|---------|---------|-------|
| `piper-sample-generator` | Synthetic positive clip generation | Used by `colab_hey_vox.py`; Linux-only (Colab) |
| `datasets` (Hugging Face) | Common Voice + LibriSpeech negative corpora | License-clean (CC-0, CC-BY) |
| `onnx`, `onnxruntime` | Model export + validation | Standard openwakeword toolchain |
| `soundfile`, `numpy`, `torchaudio` | Audio I/O + tensor ops | Already in existing training scripts |

### Alternatives Considered
| Instead of | Could Use | Tradeoff |
|------------|-----------|----------|
| `homebrew-pypi-poet` | `brew update-python-resources` | **brew tool is the better choice for v1+** — official, maintained. CONTEXT.md D-09 says poet; both work. Use poet for the first version (matches D-09), document brew tool as the maintenance path |
| `python -m build` | `cibuildwheel` | Not needed — heyvox is pure Python (no C extensions). `python -m build` produces `heyvox-1.0.0-py3-none-any.whl` |
| `urllib.request` for model download | `requests` (new dep) | stdlib avoids adding a runtime dep just for one HTTPS GET; `rich` already provides progress UI; `requests` ergonomics not worth the dep cost |
| `mislav/bump-homebrew-formula-action` | manual PR | D-10 says manual; auto-bump explicitly defers per CONTEXT.md deferred-ideas. Confirmed via web search that Python formulae with `resource` blocks aren't supported by the action anyway |
| Bake sha256 into Python constants | Query GitHub API `.assets[].digest` at download time | sha256 baked into a constants file is more robust (works offline, no rate-limit, immutable) — recommend baking. API field exists since 2024-09 but only for new releases. Plan tradeoff: one-line constant bump per model release |

### Installation (verified)
```bash
# Build prerequisites (publish.yml)
pip install build  # produces wheel + sdist

# Homebrew formula generation (one-time + per-release)
pip install homebrew-pypi-poet  # OR use `brew update-python-resources heyvox`
```

**Version verification (run during planning):**
```bash
# Confirm latest gh-action-pypi-publish tag
gh api repos/pypa/gh-action-pypi-publish/releases/latest --jq .tag_name

# Confirm pypi-publish action is at release/v1 (verified: documented as canonical pin)

# Confirm homebrew-pypi-poet latest
pip index versions homebrew-pypi-poet  # → 0.10.0
```

## Architecture Patterns

### Two-Job Publish Workflow (PyPA blessed pattern)

```
on: push (tag matches v*)
  ↓
job: build (any runner, e.g. ubuntu-latest or macos-14)
  - checkout
  - setup-python 3.12
  - pip install build
  - python -m build  → dist/heyvox-X.Y.Z-py3-none-any.whl + dist/heyvox-X.Y.Z.tar.gz
  - upload-artifact dist/
  ↓
job: publish (needs: build)
  permissions: { id-token: write }   ← required for OIDC
  environment: { name: pypi, url: https://pypi.org/p/heyvox }
  - download-artifact dist/
  - pypa/gh-action-pypi-publish@release/v1  ← no `with:` args needed; OIDC token auto-exchanged
```

**Why two jobs:** PyPA security recommendation — build runs with default permissions, publish runs with `id-token: write`. Separates build-time injection risk from publish privilege.

**Environment use:** the `environment: pypi` block lets the maintainer set deployment protection rules (e.g., require approval on first run, restrict to tagged releases). For v1, leave protection rules off — the OIDC trust scope is already narrow (only this workflow + tag refs can publish).

### Homebrew Python Formula Pattern (Language::Python::Virtualenv)

```ruby
class Heyvox < Formula
  include Language::Python::Virtualenv

  desc "macOS voice layer for AI coding agents"
  homepage "https://heyvox.dev"
  url "https://files.pythonhosted.org/packages/source/h/heyvox/heyvox-1.0.0.tar.gz"
  sha256 "<sha256 of the sdist>"
  license "MIT"

  depends_on "portaudio"
  depends_on "python@3.12"

  # Generated by `poet heyvox` or `brew update-python-resources heyvox`
  resource "openwakeword" do
    url "https://files.pythonhosted.org/..."
    sha256 "..."
  end
  resource "pyaudio" do
    url "..."
    sha256 "..."
  end
  # ... ~30 more resource blocks for all transitive deps

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "heyvox", shell_output("#{bin}/heyvox --help")
    assert_match version.to_s, shell_output("#{bin}/heyvox --version")
  end
end
```

**Key points:**
- `virtualenv_install_with_resources` creates an isolated venv at `libexec/`, installs each `resource` into it, then installs the main package. The venv is rooted in libexec (Homebrew convention).
- `depends_on "python@3.12"` — Homebrew rule: Python formulae **must** declare an unconditional Python version dep. Not optional.
- `depends_on "portaudio"` — PyAudio compiles against the system PortAudio, which Brew installs.
- `resource` blocks must enumerate **all** transitive deps. `pip` is not allowed to resolve them at install time. `brew audit --strict` fails if a transitive dep is missing.
- `class Heyvox < Formula` — class name is the formula filename capitalized (heyvox.rb → Heyvox).

### Menu-Bar Title Composition (in-place edit of overlay.py:386)

```python
# Existing (overlay.py:386):
_bar_title = f"{icon}{label}"

# Target (D-12):
# When idle and not muted and no warning, mic name appears in title.
# Use existing _friendly_mic() helper.
def _truncate_mic(name: str, max_len: int = 10) -> str:
    name = _friendly_mic(name)
    if len(name) <= max_len:
        return name
    return name[:max_len - 1] + "…"

# At the idle/title-set site (~line 424):
btn.setTitle_(f" {_truncate_mic(_active_mic)}{_idle_suffix}")
btn.setToolTip_(f"Mic: {_friendly_mic(_active_mic)}")  # full name, no truncation
```

**Why a separate `_truncate_mic`:** keeps truncation policy testable in isolation. Unit test: assert `_truncate_mic("Evolve2 75 UC") == "Evolve2 7…"`, `_truncate_mic("AirPods") == "AirPods"`.

### Mic-Submenu Voice-Isolation Indicator (D-13)

Each mic entry in the submenu (overlay.py:1062–1073) gets a suffix derived from the profile registry:

```python
# Read voice_isolation_mode from active config profile for this device
def _vi_suffix(dev_name: str, config) -> str:
    profile = config.mic_profiles.get(_match_key(dev_name, config))
    if profile is None or profile.voice_isolation_mode is None:
        return ""
    mode = profile.voice_isolation_mode
    return f"  ·  VI: {'On' if mode else 'Off'}"

_title = f"{_friendly_mic(_dev_name)}{_vi_suffix(_dev_name, config)}"
_mic_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(_title, "switchMic:", "")
```

**Match logic (`_match_key`):** mirror the existing partial case-insensitive substring match used by MicProfileManager (`heyvox/audio/profile.py`). Falls through to "" if no profile entry exists for the device, in which case the suffix is empty — no fake "Off".

**No AVCaptureDevice imports.** Acceptance criterion #11.

### Setup-Wizard Wake-Word Download Step (D-19)

New helper inserted between Step 3 (Kokoro model) and Step 4 (microphone test) of `wizard.py`:

```python
def _download_wakeword_model(console, force: bool = False) -> bool:
    """Download hey_vox.onnx from GitHub Releases to ~/.config/heyvox/models/."""
    from heyvox.config import CONFIG_DIR

    models_dir = CONFIG_DIR / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    target = models_dir / "hey_vox.onnx"

    if target.exists() and not force:
        console.print(f"  [green]✓[/green] hey_vox model already present: {target}")
        return True

    # URL + sha256 baked into constants (see Risks for tradeoff vs GitHub API query)
    from heyvox.constants import HEY_VOX_MODEL_URL, HEY_VOX_MODEL_SHA256

    import urllib.request, hashlib, tempfile
    with tempfile.NamedTemporaryFile(delete=False, suffix=".onnx") as tmp:
        # Stream into temp file with progress bar
        with urllib.request.urlopen(HEY_VOX_MODEL_URL) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            with Progress(...) as progress:
                task = progress.add_task("Downloading hey_vox.onnx", total=total)
                hasher = hashlib.sha256()
                while chunk := resp.read(64 * 1024):
                    tmp.write(chunk)
                    hasher.update(chunk)
                    progress.update(task, advance=len(chunk))
        actual = hasher.hexdigest()
        if actual != HEY_VOX_MODEL_SHA256:
            os.unlink(tmp.name)
            console.print(f"  [red]✗[/red] sha256 mismatch: expected {HEY_VOX_MODEL_SHA256[:12]}, got {actual[:12]}")
            return False
        os.replace(tmp.name, target)

    console.print(f"  [green]✓[/green] hey_vox model downloaded: {target}")
    return True
```

**Idempotency:** `target.exists()` short-circuit preserves user-trained models (D-19).
**`--redownload-wakeword` flag:** CLI passes `force=True` to the helper.
**Constants in `heyvox/constants.py`:** `HEY_VOX_MODEL_URL = "https://github.com/heyvox-dev/heyvox/releases/download/v1.0.0/hey_vox.onnx"`, `HEY_VOX_MODEL_SHA256 = "..."`. Bumped per release.

### Anti-Patterns to Avoid

- **Don't use `requests` to download the model.** Stdlib `urllib.request` is sufficient; adding a runtime dep just for an HTTPS GET is dependency bloat.
- **Don't bundle the .onnx in the wheel.** Acceptance criterion is wheel-size-friendly (CONTEXT.md: "wheel itself stays small"). 1–5 MB is small enough in absolute terms, but the runtime-download pattern matches Kokoro's existing flow (Step 3 of wizard) and keeps semver-bumps for model improvements decoupled from package version.
- **Don't hardcode app names in app-profile parsing.** CLAUDE.md rule. Mic-isolation submenu reads profile-by-name match, never special-cases a known headset.
- **Don't import AVCaptureDevice or AVFoundation.** Acceptance criterion #11; CONTEXT.md `<deferred>` paragraph confirms.
- **Don't use the `master` tag of `gh-action-pypi-publish`** (deprecated). Use `release/v1`.
- **Don't skip the build/publish job split.** PyPA explicitly recommends two jobs — putting `id-token: write` on the build job opens an injection escalation vector.

## Don't Hand-Roll

| Problem | Don't Build | Use Instead | Why |
|---------|-------------|-------------|-----|
| PyPI upload with auth | Custom curl + API token | `pypa/gh-action-pypi-publish@release/v1` | Only action that supports OIDC Trusted Publishers; PyPA-blessed |
| Wheel + sdist build | `setup.py sdist bdist_wheel` | `python -m build` | PEP 517 standard; `setup.py` invocations are soft-deprecated |
| Python homebrew formula skeleton | Hand-write all 30+ resource blocks | `poet heyvox` or `brew update-python-resources heyvox` | Auto-derives url + sha256 + version; manual is error-prone (one wrong sha breaks the formula) |
| Menu-bar status display | `NSWindow` + screen-region tracking | `NSStatusBar.systemStatusBar().statusItemWithLength_(...)` (already in use) | Standard AppKit pattern; status item handles dark-mode, resolution scaling, animation context, hover for free |
| Tooltip on menu bar | Custom hover detection | `status_button.setToolTip_(full_name)` | AppKit handles hover-delay, dismissal, accessibility |
| sha256 verification of downloaded file | Write your own digest loop | `hashlib.sha256()` + stream chunks | Stdlib, deterministic, no dep |
| Wake-word neural net design | Train CNN from scratch | openwakeword's frozen-backbone + small classifier head | Library is a known-good baseline; `target_fp_per_hour: 0.2` config converges in <1hr on Colab T4 |
| Wake-word test corpus | Record your own 11-hour FP corpus | openwakeword's DiPCo + Santa Barbara + MUSDB (~11h) | Pre-curated, published, license-clean; matches the FP measurement methodology openwakeword itself uses |

**Key insight:** Every problem in this phase has a 2025-blessed standard tool. The codebase already imports each library — the work is wiring, not selection.

## Runtime State Inventory

**Trigger applies:** Phase 14 renames `Alpha → Beta` (cosmetic), bumps default `wake_words.start` (no rename — already `hey_vox`), and introduces a new file at `~/.config/heyvox/models/hey_vox.onnx`. No legacy string rename across the codebase.

| Category | Items Found | Action Required |
|----------|-------------|------------------|
| Stored data | None — `~/.config/heyvox/models/` is a new directory the wizard creates. No existing files conflict. | None |
| Live service config | launchd `com.heyvox.listener` plist points to `heyvox` binary — survives package re-install via `pip install heyvox` (binary path stable). | None — verified via plist convention |
| OS-registered state | None — Phase 14 doesn't touch launchd, AX permissions, or Notification Center registrations. | None |
| Secrets/env vars | PyPI OIDC needs no static secrets in repo. Homebrew tap repo needs no secrets. The maintainer's PyPI account needs `Add Publisher` form filled (one-time). | One-time PyPI maintainer task before first tag push |
| Build artifacts | `dist/` directory in repo root will accumulate `heyvox-X.Y.Z-py3-none-any.whl` + `.tar.gz` from `python -m build`. Already in `.gitignore` (line: `dist/`). | None — .gitignore covers it |
| Existing `hey_vox.onnx` (legacy) | User memory `project_wakeword_training.md` notes an existing personalized MLP model trained on the maintainer's voice. D-19 explicitly preserves user-trained models. | Wizard checks `target.exists()` before downloading — preserves existing |

**The canonical question (refactor lens):** Phase 14 is additive — no rename, no path migration. Only new state is the `~/.config/heyvox/models/hey_vox.onnx` file the wizard downloads.

## PyPI OIDC Publish Workflow

### One-Time Maintainer Setup (before first tag push)

1. **Log in to PyPI** as the project owner (account that holds the `heyvox` name reservation).
2. The `heyvox` project on PyPI currently shows a placeholder "coming soon" (per SPEC background). Since no version has been published yet, the project is in **pending publisher** state — go to **Your account → Publishing** (sidebar), not the project page.
3. **Add a new pending publisher** form:
   - **PyPI project name:** `heyvox`
   - **Owner:** `heyvox-dev`
   - **Repository name:** `heyvox`
   - **Workflow filename:** `publish.yml` (just the basename, not the full path)
   - **Environment name:** `pypi` (matches the `environment:` block in publish.yml)
4. Click **Add**. The pending publisher does NOT reserve the name — the name is only secured on first successful publish. CONTEXT.md SPEC says PyPI already shows a placeholder, which suggests the name is already claimed via some other route (manual upload of a placeholder, or pre-2023 manual registration). Either way: the Trusted Publisher registration is independent.
5. After first successful publish (tag `v1.0.0` push triggers `publish.yml`, action exchanges OIDC token, PyPI accepts upload), the pending publisher auto-converts to a normal publisher. No further action.

### publish.yml Workflow Structure

```yaml
name: Publish to PyPI

on:
  push:
    tags:
      - 'v*'

jobs:
  build:
    name: Build distribution
    runs-on: macos-14    # D-03; could also be ubuntu-latest since heyvox is pure-Python
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - run: pip install --upgrade pip build
      - run: python -m build
      - uses: actions/upload-artifact@v4
        with:
          name: dist-${{ github.sha }}
          path: dist/

  publish:
    name: Publish to PyPI
    needs: build
    runs-on: ubuntu-latest    # Publish job can be anywhere; lighter runner is fine
    environment:
      name: pypi
      url: https://pypi.org/p/heyvox
    permissions:
      id-token: write   # MANDATORY for OIDC Trusted Publishers
    steps:
      - uses: actions/download-artifact@v4
        with:
          name: dist-${{ github.sha }}
          path: dist/
      - uses: pypa/gh-action-pypi-publish@release/v1
        # No `with:` block needed. Action auto-detects OIDC token.
```

**Notes:**
- `runs-on: macos-14` for build is fine but **not necessary** — heyvox produces a pure-Python wheel (`heyvox-X.Y.Z-py3-none-any.whl`). `ubuntu-latest` would also work and is faster. Recommend `macos-14` to match the rest of the CI fleet for consistency.
- `pypa/gh-action-pypi-publish@release/v1` is a **rolling pointer** to the latest v1.x release. Pypa explicitly recommends this pin over a hash; v1.x is API-stable.
- The `environment: pypi` block can be left in even without configuring environment protection rules in the repo settings — the workflow runs fine. Protection rules are optional hardening.
- The `if: github.event_name == 'push' && startsWith(github.ref, 'refs/tags/v')` guard is implicit because `on.push.tags` already restricts; no extra guard needed.

### What the Maintainer Sees on First Tag Push

```
git tag v1.0.0
git push origin v1.0.0
# → triggers .github/workflows/publish.yml
# → build job: produces dist/heyvox-1.0.0-py3-none-any.whl + dist/heyvox-1.0.0.tar.gz
# → publish job: exchanges OIDC token, uploads via pypa-action
# → Within 60s, PyPI page https://pypi.org/p/heyvox shows v1.0.0
# → `pip install heyvox==1.0.0` works from any fresh venv globally
```

If the OIDC exchange fails (no pending publisher registered, or workflow path mismatch), the publish step emits a precise error: `no trusted publisher matches this token request`. Easy to diagnose.

### Version Sync (D-04)

`heyvox/__init__.py` becomes:
```python
from importlib.metadata import version, PackageNotFoundError
try:
    __version__ = version("heyvox")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"   # Fallback when running from source without install
```

This makes `pyproject.toml [project] version` the **single source of truth**. Bumping the version requires only one file edit. The `0.0.0-dev` fallback handles the `pip install -e .` case where the import precedes a fresh install.

## Homebrew Formula Authoring

### Tap Repo Creation (one-time)

```bash
gh repo create heyvox-dev/homebrew-heyvox --public \
    --description "Homebrew tap for heyvox"
git clone https://github.com/heyvox-dev/homebrew-heyvox
cd homebrew-heyvox
mkdir Formula
# Formula/heyvox.rb goes here
```

### Resource Stanza Generation

Two valid approaches; CONTEXT.md D-09 selects `homebrew-pypi-poet`.

**Option A — homebrew-pypi-poet (CONTEXT.md D-09):**
```bash
python3 -m venv /tmp/poetenv
source /tmp/poetenv/bin/activate
pip install heyvox==1.0.0 homebrew-pypi-poet
poet heyvox > resources.rb        # writes ~30 resource blocks
deactivate; rm -rf /tmp/poetenv
```

**Option B — `brew update-python-resources` (recommended for v1.1+, official tool):**
```bash
brew update-python-resources heyvox          # in-place edit of Formula/heyvox.rb
# OR
brew update-python-resources --print-only heyvox  # preview to stdout
```

`brew update-python-resources` requires the formula to already exist with `url` + `sha256` of the new PyPI version pre-filled. It then queries PyPI for each dependency and rewrites the `resource` blocks. **Maintained by Homebrew team; replaces unmaintained `homebrew-pypi-poet`.**

**Recommendation:** Use `homebrew-pypi-poet` for initial formula authorship (matches CONTEXT.md D-09 verbatim) but document `brew update-python-resources heyvox` as the per-release maintenance command in the tap repo's README.

### Formula Skeleton (template the planner can adapt)

```ruby
class Heyvox < Formula
  include Language::Python::Virtualenv

  desc "macOS voice layer for AI coding agents: wake word, STT, TTS, HUD"
  homepage "https://heyvox.dev"
  url "https://files.pythonhosted.org/packages/source/h/heyvox/heyvox-1.0.0.tar.gz"
  sha256 "<sha256 of the sdist published to PyPI>"
  license "MIT"

  depends_on "portaudio"
  depends_on "python@3.12"

  # ── BEGIN poet output ──
  resource "openwakeword" do
    url "https://files.pythonhosted.org/packages/.../openwakeword-X.Y.Z.tar.gz"
    sha256 "..."
  end
  resource "pyaudio" do
    url "..."
    sha256 "..."
  end
  resource "pyobjc-framework-Cocoa" do
    url "..."
    sha256 "..."
  end
  # ... ~30 more
  # ── END poet output ──

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "heyvox", shell_output("#{bin}/heyvox --help")
    assert_match version.to_s, shell_output("#{bin}/heyvox --version 2>&1")
  end
end
```

### `on_arm` Block for ML Deps (SPEC R3)

SPEC says formula uses `on_arm` block for Apple-Silicon ML deps. But the ML deps (`mlx-whisper`, `sherpa-onnx`) install fine via PyPI's prebuilt arm64 wheels (verified above) — they don't need conditional inclusion. The formula can declare them as unconditional `resource` blocks since:

1. Homebrew formulae for `heyvox-dev/heyvox` only target Apple Silicon (SPEC constraint: `macOS 14+ Apple Silicon`). No x86_64 install path.
2. PyPI's `mlx-whisper-X.Y.Z-py3-none-macosx_14_0_arm64.whl` installs cleanly on macOS-14 arm64.

**Recommendation:** Skip the `on_arm` block — it's unnecessary when the tap only targets arm64 anyway. If you want belt-and-suspenders, gate the ML resources:

```ruby
on_arm do
  resource "mlx-whisper" do
    url "..."
    sha256 "..."
  end
end
```

…but this is defensive coding for a platform the formula doesn't claim to support.

### `brew audit --strict` Failure Modes

| Audit Failure | Cause | Fix |
|---------------|-------|-----|
| `missing resource for ${dep}` | `pyproject.toml` lists dep but no `resource` block in formula | Re-run `poet` or `brew update-python-resources` |
| `unused resource ${dep}` | `resource` block in formula but dep no longer in `pyproject.toml` | Remove resource block manually |
| `python virtualenv usage` lint | Used `pip_install` outside `def install` | Wrap install in `virtualenv_install_with_resources` |
| `unstable url` | sdist URL is on GitHub releases, not files.pythonhosted.org | Use the PyPI URL, not GitHub — the sdist must be served from PyPI |
| `formula must have a Cellar` | Implicit when virtualenv is correct; usually a knock-on of a deeper issue | Investigate other audit messages first |

### Smoke-Test the Formula Locally

```bash
brew tap heyvox-dev/heyvox          # adds the tap
brew install --build-from-source heyvox
heyvox --help                       # exits 0
heyvox setup                        # interactive
brew audit --strict heyvox-dev/heyvox/heyvox    # passes
brew test heyvox                    # runs the `test do` block
```

## HUD Menu-Bar Mic Display (D-12)

### NSStatusBarButton setToolTip Semantics

- Method signature (PyObjC): `status_button.setToolTip_("Mic: Evolve2 75 UC")`.
- Tooltip refreshes on next hover after `setToolTip_` is called. **No live re-rendering required** — set once when the mic changes, the OS handles hover display.
- No tooltip lifecycle pitfalls — the string is retained by AppKit until the next `setToolTip_` call. Releasing the original Python `str` is safe (NSString is copied).
- Accessibility-friendly: VoiceOver reads the tooltip.

### Menu-Bar Title Length

- Apple does NOT document a hard character limit.
- Empirically (web search): system items truncate other status items at ~64 chars before showing ellipsis. The actual budget depends on screen width and number of other menu-bar items.
- For HeyVox in idle state with no held messages and no warnings, the title is currently empty (just the brand icon). Adding 8–10 char mic name + space = 9–11 chars — well within budget.
- If the title gets too long (warning + held count + mic name), the OS auto-truncates the rightmost item first — which is fine, that's the lowest-priority info.

### Truncation Algorithm Recommendation

```python
def _truncate_mic(name: str, max_len: int = 10) -> str:
    """Truncate friendly mic name for menu-bar title.

    Examples:
      "Evolve2 75"      → "Evolve2 75"  (10 chars, fits)
      "Evolve2 75 UC"   → "Evolve2 7…"  (10 chars, truncated)
      "AirPods Pro"     → "AirPods Pr…" (wait — 11 chars; trim to first word: "AirPods")
      "Built-in"        → "Built-in"    (8 chars, fits)
    """
    friendly = _friendly_mic(name)  # already strips suffixes
    if len(friendly) <= max_len:
        return friendly
    # If first word is shorter than max_len, prefer word boundary
    first_word = friendly.split()[0]
    if len(first_word) <= max_len:
        return first_word
    return friendly[:max_len - 1] + "…"
```

Word-boundary preference is a quality-of-life touch: "AirPods" reads better than "AirPods P…" or "AirPods Pr". Optional polish.

### Composition with Existing Title Logic (overlay.py:386–428)

The current code has multiple title states:
- `idle + no muted + no held + no warning` → title=`""`, brand-icon image
- `idle + mic muted` → SF Symbol `mic.slash`, title=" 📥{n}" if held
- `listening/processing/speaking` → emoji icon + label text
- `crashed` → ⚠️ {what} crashed
- `mic warning` → ⚠️ {warning text}

**Recommendation:** Add the mic name only to the idle/normal path (where the title is currently empty or just suffixes). For active states (recording, transcribing, speaking) the existing label ("Recording…", "Transcribing…", "Speaking…") communicates more useful info than the mic name. Add a tooltip in **all** states so users can always check which mic is active.

### Unit-Testability

Extract the truncation + composition logic to a standalone helper:

```python
# heyvox/hud/menu_bar_title.py (new file)
def format_menu_bar_title(
    state: str,
    mic_name: str,
    is_mic_muted: bool,
    held_count: int,
    mic_warning: str,
    crashed: list[str],
) -> tuple[str, str]:
    """Returns (display_title, tooltip).

    Pure function — no PyObjC imports — for testability.
    """
    ...
```

Tests in `tests/test_menu_bar_title.py` can exercise every state combination without spinning up AppKit.

## HUD Submenu — Voice Isolation Indicator (D-13)

### Reading from Profile (no AVCaptureDevice)

The `MicProfileManager` (heyvox/audio/profile.py:69) already manages per-mic profiles. The HUD currently doesn't have a handle to it (it runs in a separate AppKit process). Two options:

**Option A — config-only read (recommended, minimal coupling):**
The mic-submenu is rebuilt on every menu open (`menuNeedsUpdate_` delegate at overlay.py:1779). At rebuild time, load the config fresh:

```python
from heyvox.config import load_config
config = load_config()  # cheap, YAML parse only

def _vi_suffix_for(dev_name: str) -> str:
    """Find the matching profile entry by partial substring match."""
    for key, profile in config.mic_profiles.items():
        if key.lower() in dev_name.lower():
            if profile.voice_isolation_mode is None:
                return ""
            return f"  ·  VI: {'On' if profile.voice_isolation_mode else 'Off'}"
    return ""
```

**Option B — IPC the profile state from the main daemon to the HUD.** Overkill for read-only display. Skip.

### Match Logic Consistency

`MicProfileManager` uses partial case-insensitive substring matching. The HUD submenu **must mirror this exact logic** — otherwise the displayed VI: state could disagree with what's actually applied to the mic at runtime. Encapsulate in a shared helper:

```python
# heyvox/audio/profile.py (export new helper)
def match_profile_key(dev_name: str, profiles: dict) -> str | None:
    """Find the config-key whose substring matches dev_name (case-insensitive).

    Returns the key, or None if no match. Mirrors MicProfileManager.get() logic.
    """
    for key in profiles:
        if key.lower() in dev_name.lower():
            return key
    return None
```

HUD calls `match_profile_key(dev_name, config.mic_profiles)` → if not None, use `config.mic_profiles[key].voice_isolation_mode` for the suffix.

### Display Format (CONTEXT.md `<specifics>`)

CONTEXT.md suggestion: `"Evolve2 75  ·  VI: On"` (compact). Three states:
- `voice_isolation_mode: True` → `"  ·  VI: On"`
- `voice_isolation_mode: False` → `"  ·  VI: Off"`
- `voice_isolation_mode: None` (not set in config) → `""` (no suffix — don't fake a state)

## Wake-Word Training Logistics

### Pipeline Already in Place

The repo has two training paths (per CONTEXT.md D-14):
1. **Local pipeline** (`training/train_model.py`, `training/generate_synthetic.py`, `training/download_negatives.py`) — runs on macOS, used by the maintainer for personal model. **Stays experimental, not used for ship.**
2. **Colab pipeline** (`training/hey_vox_colab.ipynb`, `training/colab_hey_vox.py` script form) — Linux GPU, downloads Common Voice + LibriSpeech for negatives, uses `piper-sample-generator` for synthetic positives. **Ship path.**

### Synthetic Positive Generation

`training/colab_hey_vox.py:CONFIG` uses:
- `n_samples: 50000` synthetic positive clips via `piper-sample-generator`
- `augmentation_rounds: 2` for diversity

The Colab notebook already orchestrates Piper voice-pack rotation (multiple speaker IDs) with pitch/speed/noise augmentation. **No code change needed for the synthetic path.**

### Real-Voice Positives

CONTEXT.md D-15: "real-voice clips collected via `record.felberer.at`" — the web recorder. Memory `reference_web_recorder.md`: 20 clips per session, friends record samples.

Current state: `training/personal_recordings.zip` exists (2.7MB) — the maintainer's voice. For a general-purpose model, need clips from 5–10+ different speakers (CONTEXT.md `<code_context>`: existing model is "owner-specific").

**Action for the planner:** Before the training run, the maintainer collects N additional speaker recordings via `record.felberer.at`. Not a coding task per se — but a prerequisite the plan must surface.

### Test Set Construction (D-15, hybrid)

| Subset | Source | Approx Size | Purpose |
|--------|--------|-------------|---------|
| Synthetic positives | Held-out 10% of generated set | 5,000 clips | Quick TP signal during training |
| Real-voice positives | Held-out 20% of `record.felberer.at` clips | 50–200 clips (depends on collected total) | Generalization signal |
| FP corpus (general speech) | openwakeword's published validation: DiPCo + Santa Barbara + MUSDB | ~11 hours | The FP-per-hour gate |
| FP corpus (confusables) | macOS `say` "hey fox", "hey box", "hey siri" (already in `download_negatives.py`) | ~100 clips | Reduce known-similar trigger phrases |

### Ship Gate Methodology (D-16: TP ≥ 70% AND FP < 1/hour)

**TP rate:**
```
true_positives = (predicted_score >= threshold) for clips in real-voice-positives test set
TP_rate = true_positives / total_positives_in_test
```
With threshold sweep: test at thresholds 0.5, 0.6, 0.7, 0.8, 0.9, pick the lowest threshold meeting both gates.

**FP per hour:**
```
total_audio_hours = sum of clip lengths in seconds / 3600  (DiPCo + Santa Barbara + MUSDB = ~11h)
false_positives = (predicted_score >= threshold) for any clip in FP corpus
FP_per_hour = false_positives / total_audio_hours
```

**Pass criteria:** Both gates hold simultaneously at some chosen threshold. If threshold sweep finds no point where both pass, the model fails the gate — retrain (more data, higher false_weight, or both).

### Existing `colab_hey_vox.py` Already Targets This

```python
CONFIG = {
    "n_samples": 50000,
    "augmentation_rounds": 2,
    "steps": 50000,
    "max_negative_weight": 1500,    # was 3000 in v1 (too aggressive, too many FPs)
    "target_fp_per_hour": 0.2,       # STRICTER than D-16's 1/hour
    "learning_rate": 0.001,
    "negative_datasets": [
        "mozilla-foundation/common_voice_16_1",
        "openslr/librispeech_asr",
    ],
}
```

`target_fp_per_hour: 0.2` is the openwakeword **training target** — the loss function penalizes FPs until the model achieves ≤0.2 FP/hour on the training validation set. This is stricter than the SHIP gate (D-16: < 1/hour). Confidence: if training converges at the configured target, the ship gate is comfortably met. Standard config — D-16 confirms.

### Model Output Format

`train_model.py` produces a single `.onnx` file (e.g., `hey_vox.onnx`, ~250 KB to 2 MB depending on model size). The melspectrogram + embedding + VAD models are **bundled in the openwakeword pip package** — they're downloaded from `openwakeword v0.5.1` GH releases on first use of the library, to `${site-packages}/openwakeword/resources/models/`. **The HeyVox runtime doesn't need to download or bundle these separately.** The custom `hey_vox.onnx` slots into a stack openwakeword already provides.

Verified via openwakeword source: `Model(wakeword_models=["path/to/hey_vox.onnx"])` with no metadata sidecar. Model name extraction: `os.path.splitext(os.path.basename(path))[0]` — so `hey_vox.onnx` becomes the key `"hey_vox"` in predictions. Matches `WakeWordConfig.start = "hey_vox"`.

### Model Storage + Distribution (D-17)

1. Upload to GitHub Releases as a release asset:
   ```bash
   gh release upload v1.0.0 hey_vox.onnx --repo heyvox-dev/heyvox
   ```
2. Stable URL pattern: `https://github.com/heyvox-dev/heyvox/releases/download/v1.0.0/hey_vox.onnx`
3. sha256 for integrity check:
   ```bash
   shasum -a 256 hey_vox.onnx > hey_vox.onnx.sha256
   gh release upload v1.0.0 hey_vox.onnx.sha256 --repo heyvox-dev/heyvox
   ```
   Optional — easier to bake the sha256 directly into `heyvox/constants.py`:
   ```python
   HEY_VOX_MODEL_URL = "https://github.com/heyvox-dev/heyvox/releases/download/v1.0.0/hey_vox.onnx"
   HEY_VOX_MODEL_SHA256 = "abc123..."   # update per release
   ```
4. Model versioning: rev the URL per release (`v1.0.0`, `v1.0.1`, …). The wizard's idempotency check (`target.exists()`) means existing installs don't auto-upgrade. **Acceptable for v1; users can `heyvox setup --redownload-wakeword` if they want the new model.**

### Documentation (D-20)

Create `docs/wakeword-training.md`. Outline:

```markdown
# Training Your Own "Hey Vox" Wake Word

## Why retrain?
The shipped model is trained on diverse synthetic + real-voice samples.
If you find detection is unreliable for your voice or accent, retrain
with your own samples.

## Quick path (Colab, ~1 hour)
1. Open training/hey_vox_colab.ipynb in Google Colab
2. Run all cells (uses GPU runtime)
3. Download hey_vox.onnx
4. Copy to ~/.config/heyvox/models/hey_vox.onnx
5. heyvox restart

## Slow path (local, ~4 hours)
See training/README.md for full local pipeline.

## Validating before swapping
Run `python training/test_model.py --model models/hey_vox.onnx --threshold 0.5`
to measure detection on your microphone before replacing the production model.
```

Linked from `README.md`'s "Customize wake word" section (new section to add).

## Setup Wizard Model Download (D-19)

Pattern is documented in "Architecture Patterns" above. Open questions for the planner:

- **Where to insert the step?** Recommendation: between current Step 3 (Kokoro) and Step 4 (mic test). The Kokoro download is huge (~300MB); the wake-word .onnx is tiny (1–5 MB). Stack the small one after the big one — if Kokoro times out, the wake-word still installs.
- **What if download fails?** Graceful failure: log warning, suggest `heyvox setup --redownload-wakeword`, fall through to `hey_jarvis_v0.1` fallback (the `also_load` co-default ensures wake-word detection still works even without `hey_vox.onnx`).
- **What about offline installs (`pip install heyvox` then `heyvox setup` with no internet)?** Same graceful fallback — `hey_jarvis_v0.1` is bundled in the openwakeword pip package itself, so wake-word detection works even with zero downloads beyond the package install.

## Common Pitfalls

### Pitfall 1: Forgetting to register the pending publisher BEFORE first tag push
**What goes wrong:** First `publish.yml` run fails with `no trusted publisher matches this token request`. Tag is now pushed and consumed (you can't re-trigger by re-pushing the same tag).
**Why it happens:** PyPI-side setup is decoupled from the GitHub side — easy to assume the workflow alone is sufficient.
**How to avoid:** First plan task is "register pending publisher on PyPI (one-time, maintainer action)" — must happen *before* the publish.yml workflow file is committed.
**Warning signs:** OIDC exchange failure on workflow run.

### Pitfall 2: macOS-14 runner needs no special config for pure-Python build
**What goes wrong:** Maintainer adds `pip install setuptools wheel` or other heuristic prep that isn't needed; or worse, adds `cibuildwheel` config thinking heyvox needs binary wheels.
**Why it happens:** Confusion between heyvox itself (pure Python, plain wheel) and its dependencies (pyobjc, mlx-whisper — binary, but installed at user-install time, not built here).
**How to avoid:** `python -m build` with no flags is sufficient. The wheel filename will be `heyvox-X.Y.Z-py3-none-any.whl` — `py3-none-any` confirms it's pure Python.
**Warning signs:** Wheel filename contains `cp312-cp312-macosx_14_0_arm64` instead of `py3-none-any` → indicates accidental binary wheel build.

### Pitfall 3: brew audit --strict fails on missing resource
**What goes wrong:** Maintainer adds a new dep to `pyproject.toml` between PyPI releases, forgets to re-run `poet` or `brew update-python-resources`. New formula PR fails audit.
**Why it happens:** Two sources of truth (pyproject.toml deps + formula resources) drift without automation.
**How to avoid:** Formula update is a single command (`brew update-python-resources heyvox`); document this in the tap repo README as the canonical update step. Optional v1.2+: GHA workflow that auto-runs `brew update-python-resources` after PyPI publish.
**Warning signs:** `brew audit --strict heyvox` output: `missing resource for ${dep}` or `${dep} is in deps but not declared as resource`.

### Pitfall 4: openwakeword Model() loader silently falls back to a built-in if .onnx not found
**What goes wrong:** `~/.config/heyvox/models/hey_vox.onnx` is missing. The loader does `_find_model_file("hey_vox", search_dirs)` → not found → returns the bare string `"hey_vox"` → openwakeword tries to load `"hey_vox"` as a built-in model name → fails. Or worse, loads silently if there's a naming collision with a future built-in.
**Why it happens:** The fallback-to-builtin-name design in `wakeword.py:_find_model_file` is intentional (lets users override built-in names with custom files), but silently failing to load is confusing.
**How to avoid:** Setup wizard ensures `hey_vox.onnx` exists before `heyvox start` runs for the first time. Default config has `also_load: [hey_jarvis_v0.1]` so even if hey_vox fails to load, the daemon stays functional with the bundled jarvis model.
**Warning signs:** `[wakeword] Loaded models: ['hey_jarvis_v0.1']` instead of `['hey_vox', 'hey_jarvis_v0.1']` in startup log.

### Pitfall 5: Voice-isolation submenu shows stale state after config edit
**What goes wrong:** User edits `mic_profiles.evolve2.voice_isolation_mode: true → false` in config.yaml. HUD submenu still shows "VI: On" until daemon restart.
**Why it happens:** HUD caches config at startup. Submenu only refreshes on `menuNeedsUpdate_` (when menu is opened), but the cached config is stale.
**How to avoid:** Reload config fresh on each `menuNeedsUpdate_` call (cheap YAML parse, <1ms). Don't cache config in the HUD process.
**Warning signs:** Submenu VI state disagrees with current config file content. Mitigation: include the config-file mtime in the submenu rebuild's snapshot, log a warning if it changes between rebuilds.

### Pitfall 6: GitHub Releases asset upload races with publish.yml
**What goes wrong:** Maintainer pushes tag `v1.0.0` → publish.yml triggers immediately → wizard URLs reference `releases/download/v1.0.0/hey_vox.onnx` → but the asset hasn't been uploaded yet → users get 404 on `heyvox setup`.
**Why it happens:** PyPI publish is automated; GitHub release + asset upload is manual (per CONTEXT.md `<discretion>`).
**How to avoid:** Maintainer workflow is: (1) upload hey_vox.onnx as a GH Release asset FIRST, (2) THEN push the git tag. Document this ordering in `docs/release-process.md` (new file) or in the README's contributor section.
**Warning signs:** Wizard fails with 404 on first install after a new release.

### Pitfall 7: Default config shipped via wheel doesn't include new wake_words list
**What goes wrong:** User installs `heyvox==1.0.0` over an existing `heyvox==0.9.x` install. Their config.yaml at `~/.config/heyvox/config.yaml` retains `wake_words.start: hey_jarvis_v0.1` from the old setup. They never see the new default.
**Why it happens:** Config files are user-owned; pip installs don't touch them.
**How to avoid:** Already addressed by D-21 (no migration needed; no existing users yet). For future migrations, surface a "your config is using a legacy default" warning at daemon startup.
**Warning signs:** N/A for v1 (no users to break).

## Code Examples

### A. publish.yml (production-ready)

```yaml
# .github/workflows/publish.yml
name: Publish to PyPI

on:
  push:
    tags:
      - 'v*'

jobs:
  build:
    name: Build distribution
    runs-on: macos-14
    steps:
      - name: Checkout
        uses: actions/checkout@v4
      - name: Set up Python 3.12
        uses: actions/setup-python@v5
        with:
          python-version: '3.12'
      - name: Install build
        run: pip install --upgrade pip build
      - name: Build wheel and sdist
        run: python -m build
      - name: Upload dist artifacts
        uses: actions/upload-artifact@v4
        with:
          name: heyvox-dist-${{ github.sha }}
          path: dist/

  publish:
    name: Publish to PyPI
    needs: build
    runs-on: ubuntu-latest
    environment:
      name: pypi
      url: https://pypi.org/p/heyvox
    permissions:
      id-token: write
    steps:
      - name: Download dist artifacts
        uses: actions/download-artifact@v4
        with:
          name: heyvox-dist-${{ github.sha }}
          path: dist/
      - name: Publish to PyPI
        uses: pypa/gh-action-pypi-publish@release/v1
```

### B. pyproject.toml diff

```diff
 [project]
 name = "heyvox"
 version = "1.0.0"
 description = "macOS voice layer for AI coding agents — wake word, STT, TTS, HUD, media control"
 license = "MIT"
 requires-python = ">=3.12"
 readme = "README.md"
 ...
 classifiers = [
-    "Development Status :: 3 - Alpha",
+    "Development Status :: 4 - Beta",
     "Environment :: MacOS X",
     ...
 ]
```

### C. heyvox/__init__.py (single-source-of-truth)

```python
"""HeyVox — macOS voice layer for AI coding agents."""
from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("heyvox")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"
```

### D. Formula/heyvox.rb (skeleton)

See "Homebrew Formula Authoring → Formula Skeleton" section above.

### E. Default config: wake_words list (config.yaml.example)

Already present in `heyvox/config.py:WakeWordConfig`:
```python
class WakeWordConfig(BaseModel):
    start: str = "hey_vox"
    stop: str = ""  # Empty = use same as start
    also_load: list[str] = ["hey_jarvis_v0.1"]
```

And in the example YAML at `heyvox/config.py:770`:
```yaml
wake_words:
  start: hey_vox
  stop: hey_vox
  also_load: [hey_jarvis_v0.1]
```

**Status:** Already meets D-18. No change needed unless we want to phrase the comment as "both active, jarvis as fallback."

### F. Setup wizard model-download helper

```python
# heyvox/setup/wizard.py — new function inserted at Step 3.5

def _download_wakeword_model(console, force: bool = False) -> bool:
    """Download hey_vox.onnx from GitHub Releases to ~/.config/heyvox/models/.

    Args:
        console: rich.Console for output.
        force: If True, re-download even if file exists.

    Returns:
        True if model is present after this call (downloaded or already there).
    """
    from heyvox.config import CONFIG_DIR
    from heyvox.constants import HEY_VOX_MODEL_URL, HEY_VOX_MODEL_SHA256
    import urllib.request, hashlib, tempfile, os
    from rich.progress import Progress, BarColumn, DownloadColumn, TransferSpeedColumn, TextColumn

    models_dir = CONFIG_DIR / "models"
    models_dir.mkdir(parents=True, exist_ok=True)
    target = models_dir / "hey_vox.onnx"

    if target.exists() and not force:
        console.print(f"  [green]✓[/green] hey_vox model present: {target}")
        return True

    try:
        with urllib.request.urlopen(HEY_VOX_MODEL_URL) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            with tempfile.NamedTemporaryFile(
                delete=False, suffix=".onnx", dir=str(models_dir),
            ) as tmp:
                hasher = hashlib.sha256()
                with Progress(
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(),
                    DownloadColumn(),
                    TransferSpeedColumn(),
                    console=console,
                ) as progress:
                    task = progress.add_task("Downloading hey_vox.onnx", total=total)
                    while chunk := resp.read(64 * 1024):
                        tmp.write(chunk)
                        hasher.update(chunk)
                        progress.update(task, advance=len(chunk))
                tmp_path = tmp.name

        actual = hasher.hexdigest()
        if actual != HEY_VOX_MODEL_SHA256:
            os.unlink(tmp_path)
            console.print(
                f"  [red]✗[/red] sha256 mismatch: "
                f"expected {HEY_VOX_MODEL_SHA256[:12]}…, got {actual[:12]}…"
            )
            return False

        os.replace(tmp_path, target)
        console.print(f"  [green]✓[/green] hey_vox model downloaded: {target}")
        return True

    except Exception as e:
        console.print(f"  [yellow]![/yellow] Download failed: {e}")
        console.print(
            "  [dim]The bundled hey_jarvis_v0.1 fallback will be used until "
            "you retry with `heyvox setup --redownload-wakeword`.[/dim]"
        )
        return False
```

### G. Menu-bar title composition (testable)

```python
# heyvox/hud/menu_bar_title.py (new)
"""Pure functions for menu-bar title + tooltip formatting.

No PyObjC imports — kept testable in isolation.
"""

def truncate_mic(name: str, max_len: int = 10) -> str:
    """Truncate a friendly mic name for menu-bar display.

    Prefer word boundaries: 'AirPods Pro' → 'AirPods' rather than 'AirPods P…'.
    """
    if not name:
        return "None"
    if len(name) <= max_len:
        return name
    first_word = name.split()[0]
    if len(first_word) <= max_len:
        return first_word
    return name[:max_len - 1] + "…"


def format_menu_bar_title(
    *,
    state: str,
    friendly_mic: str,
    held_count: int = 0,
    is_mic_muted: bool = False,
    mic_warning: str = "",
    crashed: list[str] | None = None,
    speaker_muted: bool = False,
) -> dict:
    """Compose menu-bar title text + tooltip from state.

    Returns a dict with keys:
      - title: text to render in NSStatusBarButton.title
      - tooltip: text for NSStatusBarButton.toolTip
      - use_brand_icon: True if the brand glyph (template image) should be used
      - mute_icon: True if SF Symbol mic.slash should be used
    """
    crashed = crashed or []
    tooltip = f"Mic: {friendly_mic or 'None'}"

    if mic_warning:
        return {
            "title": f"⚠️ {mic_warning}",
            "tooltip": tooltip,
            "use_brand_icon": False,
            "mute_icon": False,
        }

    if crashed:
        return {
            "title": f"⚠️ {'+'.join(crashed)} crashed",
            "tooltip": tooltip,
            "use_brand_icon": False,
            "mute_icon": False,
        }

    if state in ("listening", "processing", "speaking"):
        icons = {"listening": "🔴", "processing": "🟡", "speaking": "🟢"}
        labels = {"listening": " Recording...", "processing": " Transcribing...", "speaking": " Speaking..."}
        title = icons[state] + labels[state]
        if held_count:
            title += f"  📥{held_count}"
        return {
            "title": title,
            "tooltip": tooltip,
            "use_brand_icon": False,
            "mute_icon": False,
        }

    # Idle path: surface the mic name + optional suffixes
    suffix = ""
    if held_count:
        suffix += f"  📥{held_count}"
    if speaker_muted:
        suffix += " 🔇"

    if is_mic_muted:
        return {
            "title": f"{suffix}".strip() or "",
            "tooltip": f"{tooltip} (muted)",
            "use_brand_icon": False,
            "mute_icon": True,
        }

    short = truncate_mic(friendly_mic) if friendly_mic else ""
    if short:
        title = f" {short}{suffix}"
    else:
        title = suffix
    return {
        "title": title,
        "tooltip": tooltip,
        "use_brand_icon": True,
        "mute_icon": False,
    }
```

## State of the Art

| Old Approach | Current Approach | When Changed | Impact |
|--------------|------------------|--------------|--------|
| PyPI auth via static API token in `secrets.PYPI_TOKEN` | OIDC Trusted Publishers (no token) | PyPI launched 2023-04 | Required for new projects; mature path |
| `setup.py sdist bdist_wheel` | `python -m build` | PEP 517 (2017); ecosystem default ~2022 | `setup.py` invocations soft-deprecated; `build` is the standard |
| `homebrew-pypi-poet` for resource stanzas | `brew update-python-resources` (built-in) | Homebrew added the command ~2020 | Official, maintained tool; poet last released 2018 |
| Single-job publish workflow | Two-job (build → publish) split | PyPA security recommendation, post-2023 | Reduces build-step privilege escalation risk |
| `from heyvox import __version__` hardcoded | `importlib.metadata.version("heyvox")` | PEP 396 (long-standing); best practice since pkg_resources deprecation 2022 | Single source of truth in pyproject.toml |
| openwakeword built-in models only | Custom `.onnx` via `wakeword_models=[path]` | openwakeword 0.5.0 (2023) | Production path for branded wake words |

**Deprecated/outdated:**
- `setup.py` direct invocation (replace with `python -m build`)
- `homebrew-pypi-poet` (still works, but `brew update-python-resources` is the maintained successor)
- `pypa/gh-action-pypi-publish@master` (deprecated; use `@release/v1`)

## Assumptions Log

| # | Claim | Section | Risk if Wrong |
|---|-------|---------|---------------|
| A1 | The `heyvox` name on PyPI is already reserved by the maintainer's account (per SPEC.md background "PyPI has the heyvox name reserved but only shows a placeholder description") | PyPI OIDC Publish Workflow | If wrong: first publish.yml run fails because the project doesn't exist and pending-publisher registration must happen first. Mitigation: maintainer verifies https://pypi.org/p/heyvox before merging Phase 14 plans. Low risk — SPEC explicitly says the name is reserved. |
| A2 | The maintainer can register a pending publisher in PyPI's UI without root-level org permissions | PyPI OIDC Publish Workflow | If wrong: blocker — need to escalate to PyPI org admin. Mitigation: verify by logging into PyPI and visiting `/manage/account/publishing` before starting. |
| A3 | Apple Silicon arm64 wheels are available on PyPI for all heyvox runtime deps (mlx-whisper, sherpa-onnx, pyobjc-*) | Standard Stack | If wrong: brew install would compile from source, taking 10–30+ minutes. Verified for mlx-whisper, sherpa-onnx via web search; pyobjc-* has had arm64 wheels since 2021. |
| A4 | openwakeword's bundled FP validation corpus (DiPCo + Santa Barbara + MUSDB) is suitable as a "normal speech" benchmark for English-language FP measurement | Validation Architecture | If wrong: FP measurement is biased. The corpus is what openwakeword itself uses; results are comparable to other openwakeword-trained models. |
| A5 | Existing `colab_hey_vox.py` config (`target_fp_per_hour: 0.2`, `max_negative_weight: 1500`) reliably produces a model meeting D-16's ship gate (TP ≥ 70%, FP < 1/hour) on the hybrid test set | Wake-Word Training Logistics | If wrong: training run produces a model below the gate, requires hyperparameter tuning. Mitigation: training is iterative anyway; first run validates the gate, second run tunes if needed. The previous shipped model (memory: `project_wakeword_training.md`) hit similar gates. |
| A6 | The HUD overlay process can read `~/.config/heyvox/config.yaml` directly without IPC complications | HUD Submenu — Voice Isolation Indicator | If wrong: need IPC channel from main daemon to HUD for profile state. Current overlay.py already imports `heyvox.config` and `heyvox.constants` at module load — read-only YAML parse on `menuNeedsUpdate_` is safe. |
| A7 | The friendly_mic helper at overlay.py:997-1010 correctly handles the 5–10 most common Bluetooth headset naming conventions on macOS | Code Examples → G | If wrong: title shows raw device name. Mitigation: add specific suffix-strip rules to `_friendly_mic` for known cases (already does Evolve2, AirPods are short enough as-is). Low impact — worst case is suboptimal label, not a bug. |

**Confirmed (not assumed):**
- gh-action-pypi-publish action is at `release/v1` rolling pointer (web-verified)
- homebrew-pypi-poet 0.10.0 is the latest version (pip index verified locally)
- Python's `importlib.metadata.version` is the canonical replacement for hardcoded `__version__` (PEP 396, long-standing)
- `python -m build` produces `heyvox-1.0.0-py3-none-any.whl` for pure-Python packages (verified locally — `build 1.4.2` installed)

## Open Questions (RESOLVED)

### Q1: Should `publish.yml` build on `macos-14` or `ubuntu-latest`?
- What we know: heyvox is pure Python, so the runner OS doesn't affect the wheel artifact (always `py3-none-any.whl`)
- What's unclear: CONTEXT.md D-03 says `macos-14`; using `ubuntu-latest` would be cheaper (faster start, free minutes) but adds inconsistency with `ci.yml`
- Recommendation: Use `macos-14` to match D-03 literally and to match the rest of the CI fleet. Cost is trivial (single publish per release, ~3 min).

### Q2: Should the model URL constant include the version, or be a "latest" pointer?
- What we know: GitHub Releases assets are immutable per-tag (`/releases/download/v1.0.0/hey_vox.onnx` is forever-stable)
- What's unclear: Two paradigms — version-pinned URL (rev per release, baked into `constants.py`) vs latest-release URL (`/releases/latest/download/hey_vox.onnx`, GitHub auto-resolves)
- Recommendation: Version-pinned in `constants.py`. Reasons: (1) sha256 baked in matches the specific file, (2) reproducible — `heyvox setup` from a 2026 install always pulls the same model the version was tested with, (3) easier to roll back if a model release introduces regressions.

### Q3: What happens when the user has hey_vox.onnx but no hey_jarvis_v0.1 download (offline install)?
- What we know: openwakeword's first-use behavior downloads melspectrogram + embedding + VAD from its GH Releases; if offline, this fails
- What's unclear: Does openwakeword raise on first `Model(...)` instantiation, or only fail at inference time?
- Recommendation: Add an `install-test.yml` post-publish smoke test that does `heyvox status` in an offline-simulated env. Out of scope for Phase 14 v1 — adequate to document the offline limitation.

### Q4: Should the truncation algorithm prefer word boundaries (`AirPods Pro` → `AirPods`) or fixed cutoff (`AirPods P…`)?
- What we know: CONTEXT.md `<specifics>` suggests word-boundary trimming for AirPods Pro
- What's unclear: User aesthetic preference; either reads fine
- Recommendation: Word-boundary preference (Code Example G shows this). Cheap to implement, easy to test, marginally nicer.

### Q5: Should `--redownload-wakeword` be a flag on `heyvox setup`, or a separate subcommand `heyvox update-wakeword`?
- What we know: CONTEXT.md D-19 says "`heyvox setup --redownload-wakeword` flag"
- What's unclear: Discoverability — users running `heyvox setup` again expect idempotency; the flag is hidden
- Recommendation: Stick with the flag (D-19). Document it in `heyvox setup --help` output. Future v1.1+: consider promoting to `heyvox update-wakeword`.

### Q6: Should the formula's `test do` block also exercise `heyvox setup` (which downloads ~300MB Kokoro)?
- What we know: `brew test` is a smoke test that should be fast (<60s ideally)
- What's unclear: Whether to include any network-touching commands
- Recommendation: Test block just runs `heyvox --help` + version assert. `heyvox setup` is interactive and network-bound — skip.

## Environment Availability

| Dependency | Required By | Available | Version | Fallback |
|------------|------------|-----------|---------|----------|
| Python 3.12 | publish.yml, formula deps, runtime | ✓ | 3.12.12 (local) / 3.12 (CI) | — |
| `python -m build` | publish.yml | ✓ | 1.4.2 (local) | — |
| `gh` CLI | repo create, release upload | ✓ | 2.87.3 (local) | git CLI + manual upload (worse UX) |
| Homebrew | tap install + audit | ✓ | 5.1.7 (local) | — |
| `homebrew-pypi-poet` | resource generation | available via pip | 0.10.0 | `brew update-python-resources` (built-in) |
| PortAudio | pyaudio runtime dep, also formula `depends_on` | brew-installable | — | None — required for audio capture |
| Google Colab GPU | wake-word training (D-14) | external | — | local MLX pipeline (experimental, D-14 marks as not-ship-path) |
| `record.felberer.at` web recorder | real-voice positive collection (D-15) | external | — | None — needed for general-purpose model |
| GitHub Releases | model hosting (D-17) | ✓ | — | Hugging Face Hub (alt — but matches CONTEXT.md D-17, no change needed) |
| openwakeword library | runtime wake-word inference, custom .onnx loader | ✓ (existing pyproject.toml dep) | ≥0.6.0 | — |

**Missing dependencies with no fallback:**
- Real-voice training samples from multiple speakers — the maintainer needs to drive `record.felberer.at` traffic OR generate enough TTS-voice diversity in synthetic positives to compensate. This is a project management task, not a coding task.

**Missing dependencies with fallback:**
- None — all coding-task deps are available.

## Validation Architecture (D-16 + SPEC R6 ship gate)

### Test Framework

| Property | Value |
|----------|-------|
| Framework (Python unit/integration) | pytest with markers (`integration`, `requires_audio`) — already configured in `pyproject.toml:103-110` |
| Config file | `pyproject.toml [tool.pytest.ini_options]` |
| Quick run command | `pytest tests/ --ignore=tests/test_e2e.py --ignore=tests/test_stress.py -v --tb=short` (matches `ci.yml`) |
| Full suite command | `pytest tests/` |
| Wake-word ship-gate eval script | NEW — `training/evaluate_model.py` (Phase 14 deliverable) |
| Formula audit | `brew audit --strict heyvox-dev/heyvox/heyvox` |
| Smoke install test | `pip install heyvox && heyvox --help` (gh-action: `install-test.yml`) |

### Phase Requirements → Test Map

| Req ID | Behavior | Test Type | Automated Command | File Exists? |
|--------|----------|-----------|-------------------|-------------|
| SPEC-R1 | publish.yml triggers on tag push and uploads to PyPI | manual smoke + post-publish check | Push tag v0.0.1rc to test; pip install heyvox==0.0.1rc | ❌ Wave 0 needs publish.yml |
| SPEC-R1 | Workflow file syntactically valid | actionlint (CI) | `actionlint .github/workflows/publish.yml` | ❌ Wave 0 |
| SPEC-R2 | `__version__` reads from importlib.metadata | unit | `pytest tests/test_version.py -x` | ❌ Wave 0 |
| SPEC-R2 | Classifier bumped to Beta | static | `grep "Development Status :: 4" pyproject.toml` | ❌ Wave 0 |
| SPEC-R3 | Formula passes brew audit --strict | manual | `brew audit --strict heyvox-dev/heyvox/heyvox` | ❌ Wave 0 (formula doesn't exist) |
| SPEC-R3 | brew install succeeds on macOS-14 arm64 | manual smoke + workflow | `brew tap heyvox-dev/heyvox && brew install heyvox` | ❌ Wave 0 |
| SPEC-R4 | Menu-bar title shows truncated friendly mic name | unit | `pytest tests/test_menu_bar_title.py -x` | ❌ Wave 0 |
| SPEC-R4 | Tooltip shows full friendly name | unit | (same test file) | ❌ Wave 0 |
| SPEC-R5 | Submenu shows voice_isolation_mode from active profile | unit | `pytest tests/test_overlay_vi_suffix.py -x` (new) | ❌ Wave 0 |
| SPEC-R5 | No AVCaptureDevice import added | static | `grep -r AVCaptureDevice heyvox/` returns nothing | ❌ Wave 0 (assertion test) |
| SPEC-R6 | Wake-word model meets TP ≥ 70% AND FP < 1/hour | scripted eval | `python training/evaluate_model.py --model hey_vox.onnx --test-set ./test/` | ❌ Wave 0 (new script) |
| SPEC-R6 | Default config has `wake_words.also_load: [hey_jarvis_v0.1]` | unit | `pytest tests/test_config_defaults.py::test_co_default_wake_words -x` | ❌ Wave 0 |
| SPEC-R6 | Setup wizard downloads hey_vox.onnx when absent | integration (mocked HTTP) | `pytest tests/test_setup_wakeword_download.py -x` | ❌ Wave 0 |
| SPEC-R6 | Setup wizard preserves existing user-trained model | unit | (same test file) | ❌ Wave 0 |
| SPEC-R6 | `heyvox setup --redownload-wakeword` overrides idempotency | unit | (same test file) | ❌ Wave 0 |

### Wake-Word Ship-Gate Methodology (the heart of D-16)

```python
# training/evaluate_model.py (new file — Wave 0 deliverable)
"""Evaluate a trained wake-word model against the D-16 ship gate.

Usage:
    python training/evaluate_model.py \
        --model models/hey_vox.onnx \
        --positives test/real_voice/ \
        --negatives test/fp_corpus/ \
        --threshold 0.7

Gate: TP ≥ 70% AND FP < 1/hour. Both must hold simultaneously.

The negative corpus is openwakeword's standard validation set (DiPCo + Santa Barbara
+ MUSDB, ~11h). Download once via the bootstrap script and cache locally.
"""

import argparse, json, sys, wave
from pathlib import Path
import numpy as np

def evaluate(model_path: str, positives_dir: str, negatives_dir: str, threshold: float) -> dict:
    from openwakeword.model import Model
    model = Model(wakeword_models=[model_path])
    wake_name = Path(model_path).stem  # "hey_vox"

    # TP: percentage of positive clips that score >= threshold somewhere in the window
    pos_files = sorted(Path(positives_dir).glob("*.wav"))
    tp = 0
    for f in pos_files:
        if _detect(model, f, wake_name, threshold):
            tp += 1
    tp_rate = tp / max(len(pos_files), 1)

    # FP: count of clips in negative corpus that triggered, normalized to FP/hour
    neg_files = sorted(Path(negatives_dir).glob("*.wav"))
    fp = 0
    total_seconds = 0.0
    for f in neg_files:
        model.reset()
        with wave.open(str(f), "rb") as wf:
            total_seconds += wf.getnframes() / wf.getframerate()
        if _detect(model, f, wake_name, threshold):
            fp += 1
    fp_per_hour = fp / (total_seconds / 3600.0) if total_seconds > 0 else 0.0

    return {
        "tp_rate": tp_rate,
        "tp_count": tp,
        "positive_total": len(pos_files),
        "fp_count": fp,
        "fp_per_hour": fp_per_hour,
        "negative_total_seconds": total_seconds,
        "threshold": threshold,
        "gate_pass": tp_rate >= 0.70 and fp_per_hour < 1.0,
    }

def _detect(model, wav_path, wake_name, threshold):
    """Stream 80ms frames through the model; return True if any frame >= threshold."""
    # ... (implementation reads WAV in 1280-sample chunks, feeds model.predict)
    ...

if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--model", required=True)
    p.add_argument("--positives", required=True)
    p.add_argument("--negatives", required=True)
    p.add_argument("--threshold", type=float, default=0.5)
    p.add_argument("--sweep", action="store_true", help="Sweep thresholds 0.5..0.95")
    args = p.parse_args()

    if args.sweep:
        results = []
        for t in [0.5, 0.6, 0.7, 0.8, 0.9, 0.95]:
            r = evaluate(args.model, args.positives, args.negatives, t)
            results.append(r)
        # Find lowest threshold meeting both gates
        passing = [r for r in results if r["gate_pass"]]
        if passing:
            best = min(passing, key=lambda r: r["threshold"])
            print(f"PASS — recommend threshold={best['threshold']}")
            print(json.dumps(best, indent=2))
            sys.exit(0)
        else:
            print("FAIL — no threshold satisfies TP ≥ 70% AND FP < 1/hour")
            print(json.dumps(results, indent=2))
            sys.exit(1)
    else:
        r = evaluate(args.model, args.positives, args.negatives, args.threshold)
        print(json.dumps(r, indent=2))
        sys.exit(0 if r["gate_pass"] else 1)
```

### Sampling Rate

- **Per task commit:** `pytest tests/ --ignore=tests/test_e2e.py --ignore=tests/test_stress.py -v --tb=short` (existing ci.yml command)
- **Per wave merge:** Full suite + `pytest tests/test_defect_guards.py` (existing ci.yml step) + `actionlint .github/workflows/`
- **Phase gate before `/gsd-verify-work`:**
  1. Full pytest suite green
  2. `python training/evaluate_model.py --model models/hey_vox.onnx --sweep` exits 0 with chosen threshold documented in `docs/wakeword-training.md`
  3. `brew audit --strict heyvox-dev/heyvox/heyvox` passes (after first formula PR is merged)
  4. Manual: tag v1.0.0 → publish.yml succeeds → `pip install heyvox==1.0.0` works from fresh venv

### Wave 0 Gaps (test infrastructure needs)

- [ ] `tests/test_version.py` — verify `heyvox.__version__` resolves via importlib.metadata
- [ ] `tests/test_config_defaults.py` — verify `WakeWordConfig.also_load == ["hey_jarvis_v0.1"]`
- [ ] `tests/test_menu_bar_title.py` — covers truncation + composition (Code Example G)
- [ ] `tests/test_overlay_vi_suffix.py` — VI suffix appears, matches profile, no AVCaptureDevice
- [ ] `tests/test_setup_wakeword_download.py` — mock urllib + sha256 + filesystem
- [ ] `training/evaluate_model.py` — ship-gate eval script (above)
- [ ] `.github/workflows/publish.yml` — pypa pattern
- [ ] `Formula/heyvox.rb` in `heyvox-dev/homebrew-heyvox` — outside this checkout
- [ ] `docs/wakeword-training.md` — new file
- [ ] `docs/release-process.md` (optional) — documents the upload-asset-before-tag-push ordering

## Security Domain

### Applicable ASVS Categories

| ASVS Category | Applies | Standard Control |
|---------------|---------|-----------------|
| V2 Authentication | yes (PyPI publish) | OIDC Trusted Publisher via `pypa/gh-action-pypi-publish@release/v1` — no static credentials. `id-token: write` permission narrowly scoped. |
| V3 Session Management | no | N/A — no user sessions in scope |
| V4 Access Control | partial | GitHub Actions `permissions: id-token: write` block restricts token scope to one job. Environment protection rules on `pypi` environment provide an additional gate. |
| V5 Input Validation | yes (setup wizard URL handling, sha256 verification) | sha256 verification of downloaded model is non-optional — code rejects mismatch and aborts. Wizard does not pass user-supplied strings to shell. |
| V6 Cryptography | yes (sha256 integrity, OIDC HMAC-signed JWTs) | Use stdlib `hashlib.sha256` (FIPS-approved, no hand-rolled). OIDC JWT verification is delegated to the pypa action. |
| V8 Data Protection | no | No PII processed in scope |
| V9 Communications | yes | All downloads over HTTPS to github.com / pypi.org / files.pythonhosted.org. Stdlib `urllib.request` validates TLS certs by default on macOS via system trust store. |
| V14 Configuration | yes (`brew audit --strict`, dependency pinning) | Resource stanzas pin sha256 of each dep — supply-chain integrity. PyPI uploads sign with OIDC; no static token leakage risk. |

### Known Threat Patterns for {publish + download}

| Pattern | STRIDE | Standard Mitigation |
|---------|--------|---------------------|
| Compromised PyPI API token published in repo | Spoofing/Information disclosure | Use OIDC Trusted Publishers — no static token to leak |
| Compromised GitHub repo pushes malicious wheel | Tampering | Two-job split (build, publish) limits blast radius; environment protection rules can require manual approval on first publish |
| Downloaded model swapped for malicious .onnx | Tampering | sha256 verification at download time, hash baked in constants.py |
| Man-in-the-middle on model download | Tampering | HTTPS to github.com (TLS), plus sha256 |
| Dependency confusion (homebrew formula transitive deps) | Tampering | `brew audit --strict` enforces explicit `resource` blocks with sha256; pip's automatic resolution disallowed by virtualenv_install_with_resources contract |
| Stale gh-action-pypi-publish version with CVE | Tampering | `release/v1` is rolling pointer to latest 1.x patch — auto-receives security updates without manual bumps |

## Sources

### Primary (HIGH confidence)
- [PyPI Trusted Publishers documentation — adding a publisher](https://docs.pypi.org/trusted-publishers/adding-a-publisher/) — one-time setup UI
- [PyPI Trusted Publishers — creating a project through OIDC](https://docs.pypi.org/trusted-publishers/creating-a-project-through-oidc/) — pending publisher for unpublished projects
- [pypa/gh-action-pypi-publish (GitHub)](https://github.com/pypa/gh-action-pypi-publish) — minimal workflow example, pinning policy
- [Homebrew — Python for Formula Authors](https://docs.brew.sh/Python-for-Formula-Authors) — `Language::Python::Virtualenv`, `depends_on "python@3.12"`, `virtualenv_install_with_resources`
- [openWakeWord — README & model loader](https://github.com/dscripka/openWakeWord) — Model() loader, custom .onnx interface, `wakeword_model_names` extraction logic
- [openWakeWord — automatic_model_training.ipynb (referenced)](https://github.com/dscripka/openWakeWord/blob/main/notebooks/automatic_model_training.ipynb) — FP/hour gate methodology, hybrid corpus
- Existing repo: `heyvox/audio/wakeword.py`, `heyvox/audio/profile.py`, `heyvox/config.py`, `heyvox/hud/overlay.py`, `heyvox/setup/wizard.py`, `training/colab_hey_vox.py` — verified by file read
- Local `pip index versions homebrew-pypi-poet` — confirms 0.10.0 (2018) is latest
- Local `gh --version` (2.87.3), `brew --version` (5.1.7), `python3 --version` (3.12.12) — verified available

### Secondary (MEDIUM confidence)
- [Publishing package distribution releases using GitHub Actions](https://packaging.python.org/en/latest/guides/publishing-package-distribution-releases-using-github-actions-ci-cd-workflows/) — PyPA publishing guide
- [mislav/bump-homebrew-formula-action](https://github.com/mislav/bump-homebrew-formula-action) — confirms auto-bump can't handle Python resource blocks, justifying D-10 manual PR decision
- [GitHub community discussion — Release checksums](https://github.com/orgs/community/discussions/23512) — `.assets[].digest` field documents sha256 availability
- [openwakeword GitHub release v0.5.1](https://github.com/dscripka/openWakeWord/releases) — confirms melspectrogram/embedding/VAD model URLs
- [piper-sample-generator (rhasspy)](https://github.com/rhasspy/piper-sample-generator) — synthetic positive generation, the Colab pipeline dep
- [Apple — NSStatusBarButton documentation](https://developer.apple.com/documentation/appkit/nsstatusbarbutton) — setToolTip semantics

### Tertiary (LOW confidence — single source or needs human review)
- Web search consensus on macOS menu-bar title length budget (~64 chars before truncation by neighbors) — empirical, no Apple-documented limit
- Word-boundary preference in `_truncate_mic` — UX call, no authoritative source

## Metadata

**Confidence breakdown:**
- PyPI OIDC publish workflow: HIGH — multiple authoritative sources, pattern is well-established
- pyproject.toml metadata bump: HIGH — single-line classifier change, trivial
- Homebrew formula: HIGH for skeleton, MEDIUM for resource generation tradeoff (poet vs brew tool — both work)
- Menu-bar title/tooltip: HIGH — NSStatusBarButton setToolTip semantics confirmed, existing _friendly_mic helper handles the data side
- Voice-isolation submenu: HIGH — pattern is config read + string formatting, no new infrastructure
- Wake-word training run logistics: MEDIUM — pipeline is in place but ship-gate depends on real-voice sample diversity (project-management concern, not infra)
- Setup wizard download: HIGH — stdlib urllib + rich progress is a proven pattern, already used for Kokoro download
- Validation architecture: HIGH for unit/integration tests, MEDIUM for ship-gate eval script (new file, but methodology is concrete)

**Research date:** 2026-05-11
**Valid until:** 2026-06-11 (30 days for stable infra; sooner if PyPI changes Trusted Publishers UI or pypa/gh-action-pypi-publish ships a breaking v2)
