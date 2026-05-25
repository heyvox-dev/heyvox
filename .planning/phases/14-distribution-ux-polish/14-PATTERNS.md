# Phase 14: Distribution & UX Polish — Pattern Map

**Mapped:** 2026-05-11
**Files analyzed:** 14 (8 new, 6 modified)
**Analogs found:** 12 / 14 (2 files have no local analog — external/format mismatch)

## File Classification

| New/Modified File | Role | Data Flow | Closest Analog | Match Quality |
|---|---|---|---|---|
| `.github/workflows/publish.yml` (NEW) | CI workflow | event-driven (tag push → build → upload) | `.github/workflows/ci.yml` + `.github/workflows/install-test.yml` | exact (same runner/setup; different trigger+output) |
| `Formula/heyvox.rb` (NEW, external repo `heyvox-dev/homebrew-heyvox`) | distribution config (Ruby DSL) | declarative (resource enumeration) | none in this repo (cross-language) | no analog — pattern from RESEARCH.md §Homebrew Formula Authoring |
| `docs/wakeword-training.md` (NEW) | docs (Markdown) | docs (static reference) | `training/README.md` (Markdown, training-related) | role-match (format) |
| `training/evaluate_model.py` (NEW) | training utility script | batch (WAV in → metrics out) | `training/test_model.py` + `training/train_model.py` | exact (same dir, same openwakeword loader pattern) |
| `tests/test_version.py` (NEW) | unit test | request-response (import → assertion) | `tests/test_config.py::TestHeyvoxConfigDefaults` | role-match |
| `tests/test_menu_bar_title.py` (NEW) | unit test | request-response (state → title/tooltip) | `tests/test_hud_ipc.py::TestMessageSerialization` | role-match (HUD-domain) |
| `tests/test_overlay_vi_suffix.py` (NEW) | unit test | request-response (profile → suffix) | `tests/test_config.py` + `tests/test_mic_profile.py` | role-match |
| `tests/test_setup_wakeword_download.py` (NEW) | unit test (mocked HTTP+fs) | request-response (mocked urllib → file write) | `tests/test_app_fast_paste.py` (heavy `patch()` usage) | role-match (mocking style) |
| `tests/test_config_defaults.py` (NEW or append) | unit test | request-response (config → assertion) | `tests/test_config.py::TestHeyvoxConfigDefaults` (lines 14-60) | exact |
| `pyproject.toml` (MODIFY) | build config | declarative | (self) | exact |
| `heyvox/__init__.py` (MODIFY) | package init | static const | (self, plus `tests/conftest.py` import style) | exact |
| `heyvox/hud/overlay.py` (MODIFY lines 386-428, 988-1012, 1062-1073, 1745-1757) | UI state composition | event-driven (state → AppKit) | (self — extend existing) | exact (in-place modification) |
| `heyvox/setup/wizard.py` (MODIFY — new `_download_wakeword_model()` step) | setup pipeline step | request-response (HTTPS GET → file write + sha256) | (self — Kokoro download step at lines 226-256) | exact (mirror Kokoro step) |
| `heyvox/cli.py` (MODIFY — add `--redownload-wakeword` flag) | CLI parser | argparse declarative | (self — `sub_speak.add_argument` etc. lines 999-1020) | exact |
| `config.yaml.example` / `heyvox/config.py:767-770` (MODIFY) | example config (embedded in `config.py`) | static defaults | (self) | exact (already matches D-18, only comment polish) |
| `README.md` (MODIFY — "Customize wake word" section + first-install warning) | docs (Markdown) | docs (user-facing reference) | (self — existing "Configuration" section lines 142-182) | exact |

---

## Pattern Assignments

### `.github/workflows/publish.yml` (CI workflow, event-driven)

**Analog:** `.github/workflows/ci.yml` (setup steps) + `.github/workflows/install-test.yml` (clean-install validation)

**Trigger pattern** — model on `install-test.yml` (push trigger), NOT `ci.yml` (push+PR):

`.github/workflows/install-test.yml` lines 5-7:
```yaml
on:
  push:
    branches: [main]
```

Replace `branches: [main]` with `tags: ['v*']` per D-01 / SPEC R1.

**Runner + Python setup** — copy verbatim from `ci.yml` lines 9-21:
```yaml
jobs:
  test:
    name: Lint & Test
    runs-on: macos-14

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python 3.12
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"
```

Use `runs-on: macos-14` (D-03). Same `actions/checkout@v4` + `actions/setup-python@v5` versions.

**Two-job split** — pypa-blessed pattern (RESEARCH.md §Architecture Patterns); no precedent in this repo, build & publish jobs come from RESEARCH.md §"Code Examples → A. publish.yml (production-ready)".

**Critical:** publish job MUST declare:
```yaml
permissions:
  id-token: write
```
PyPA explicitly requires the `id-token: write` permission be on the publish job, not the build job (RESEARCH.md "Don't skip the build/publish job split" anti-pattern).

**Anti-pattern to avoid:** Do NOT use `@master` ref for the publish action (deprecated). Use `pypa/gh-action-pypi-publish@release/v1` (RESEARCH.md §State of the Art).

---

### `Formula/heyvox.rb` (Homebrew formula, declarative — EXTERNAL REPO)

**Analog:** None in this codebase (Ruby DSL, separate repo `heyvox-dev/homebrew-heyvox`).

**Pattern source:** RESEARCH.md §Homebrew Formula Authoring → "Formula Skeleton" (lines 486-525) and §Code Examples → D.

**Skeleton structure** (from RESEARCH.md):
```ruby
class Heyvox < Formula
  include Language::Python::Virtualenv

  desc "macOS voice layer for AI coding agents: wake word, STT, TTS, HUD"
  homepage "https://heyvox.dev"
  url "https://files.pythonhosted.org/packages/source/h/heyvox/heyvox-1.0.0.tar.gz"
  sha256 "<sha256 of the sdist>"
  license "MIT"

  depends_on "portaudio"
  depends_on "python@3.12"

  # Generated by `poet heyvox` (D-09) — ~30 resource blocks for transitive deps
  resource "openwakeword" do
    url "..."
    sha256 "..."
  end
  # ... more resources ...

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "heyvox", shell_output("#{bin}/heyvox --help")
    assert_match version.to_s, shell_output("#{bin}/heyvox --version 2>&1")
  end
end
```

**Dependencies to mirror from `pyproject.toml`:**
- `portaudio` (system dep, for pyaudio)
- `python@3.12` (Homebrew rule: Python formulae MUST declare an unconditional Python version dep)

**Resource enumeration** — D-09: `homebrew-pypi-poet`:
```bash
pip install homebrew-pypi-poet
poet heyvox > resources.rb
```

**`on_arm` block:** Skip per RESEARCH.md §"on_arm Block for ML Deps" — formula targets Apple Silicon only; PyPI's prebuilt arm64 wheels (mlx-whisper, sherpa-onnx, pyobjc-*) install cleanly without conditional blocks.

**Anti-patterns** (RESEARCH.md §brew audit --strict Failure Modes):
- Do NOT use `pip_install` outside `def install` — use `virtualenv_install_with_resources`
- Do NOT source the sdist URL from GitHub Releases — must be `files.pythonhosted.org`

---

### `docs/wakeword-training.md` (docs, Markdown)

**Analog:** `training/README.md` (existing, same domain — wake-word training docs).

**Pattern source (content outline):** RESEARCH.md §Wake-Word Training Logistics → "Documentation (D-20)" (lines 791-813).

**Suggested structure** (copy outline from RESEARCH.md verbatim — already concrete):
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

## Ship-gate methodology
TP ≥ 70% AND FP < 1/hour on hybrid synthetic+real test set
(D-16 + SPEC requirement 6).
```

**Linked from:** `README.md` (new "Customize wake word" section per D-20).

---

### `training/evaluate_model.py` (training utility, batch)

**Analog:** `training/test_model.py` (interactive mic-driven test) + `training/train_model.py` (batch processing).

**`test_model.py` lines 28-50 — openwakeword loader pattern (copy as-is):**
```python
try:
    from openwakeword.model import Model
except ImportError:
    print("ERROR: openwakeword required. Install with: pip install openwakeword")
    sys.exit(1)

print(f"Loading model: {args.model}")
model = Model(wakeword_models=[args.model])
```

**`train_model.py` lines 35-46 — WAV loading helper (copy as-is):**
```python
def load_wav(path: str) -> np.ndarray:
    """Load WAV file as float32 array at 16kHz."""
    with wave.open(path, "rb") as wf:
        sr = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
        audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32767.0
    if sr != SAMPLE_RATE:
        ratio = SAMPLE_RATE / sr
        new_len = int(len(audio) * ratio)
        indices = np.linspace(0, len(audio) - 1, new_len)
        audio = np.interp(indices, np.arange(len(audio)), audio).astype(np.float32)
    return audio
```

**`test_model.py` lines 60-78 — frame-by-frame inference pattern (adapt for batched WAV files):**
```python
audio = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32767.0
model.predict(audio)
for name, score in model.prediction_buffer.items():
    if len(score) > 0:
        current_score = score[-1]
        if current_score >= args.threshold:
            detections += 1
            model.reset()
```

For `evaluate_model.py`, use `model.reset()` between files so per-clip state doesn't leak.

**argparse pattern** — copy from `test_model.py` lines 22-26:
```python
parser = argparse.ArgumentParser(description="Test wake word model")
parser.add_argument("--model", required=True, help="Path to .onnx model")
parser.add_argument("--threshold", type=float, default=0.5, help="Detection threshold")
```

**Full implementation skeleton:** RESEARCH.md §Validation Architecture → "Wake-Word Ship-Gate Methodology" (lines 1273-1364). Use that template literally — it already wires sweep mode and gate-pass exit codes.

**Constants** — match `train_model.py` lines 31-32:
```python
SAMPLE_RATE = 16000
FRAME_SIZE = 1280  # 80ms frames, same as openwakeword
```

---

### `tests/test_version.py` (unit test, request-response)

**Analog:** `tests/test_config.py::TestHeyvoxConfigDefaults` (lines 14-60).

**Imports pattern** — copy from `tests/test_config.py` lines 1-11:
```python
"""Tests for heyvox.config — configuration loading and validation."""

import pytest

from heyvox.config import (
    AppProfileConfig,
    HeyvoxConfig,
    TTSConfig,
    WakeWordConfig,
    load_config,
)
```

For `test_version.py`, replace the import with:
```python
"""Tests for heyvox.__version__ — version is sourced from importlib.metadata."""

import heyvox
```

**Test class pattern** — copy structure from `test_config.py` lines 14-25:
```python
class TestVersion:
    """`__version__` must resolve via importlib.metadata.version("heyvox")."""

    def test_version_is_string(self):
        assert isinstance(heyvox.__version__, str)

    def test_version_matches_pyproject(self):
        # Read pyproject.toml [project] version, compare to runtime
        import tomllib
        from pathlib import Path
        pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
        with open(pyproject, "rb") as f:
            project_version = tomllib.load(f)["project"]["version"]
        assert heyvox.__version__ == project_version

    def test_fallback_when_uninstalled(self):
        # Verify the fallback string format used when PackageNotFoundError
        # (covered by inspecting the source, not by uninstalling at runtime)
        import re
        assert re.match(r"^\d+\.\d+\.\d+(-\w+)?$", heyvox.__version__)
```

**Validation source:** RESEARCH.md §Code Examples → C (`heyvox/__init__.py`).

---

### `tests/test_menu_bar_title.py` (unit test, request-response)

**Analog:** `tests/test_hud_ipc.py::TestMessageSerialization` (lines 175-208, HUD-domain unit test).

**Imports pattern** — copy from `tests/test_hud_ipc.py` lines 1-9:
```python
"""Tests for heyvox.hud.ipc — Unix socket IPC for HUD communication."""

import json
import os
import time
import pytest

from heyvox.hud.ipc import HUDServer, HUDClient
```

For `test_menu_bar_title.py`:
```python
"""Tests for heyvox.hud.menu_bar_title — pure title/tooltip composition."""

import pytest

from heyvox.hud.menu_bar_title import truncate_mic, format_menu_bar_title
```

**Test class pattern** — copy parametrize style from `test_hud_ipc.py::test_all_message_types_serialize` lines 191-208:
```python
class TestTruncateMic:
    """Truncation helper for menu-bar title (D-12, 8-10 char budget)."""

    @pytest.mark.parametrize("name,expected", [
        ("Evolve2 75", "Evolve2 75"),       # 10 chars, fits
        ("Evolve2 75 UC", "Evolve2 7…"),    # truncate
        ("AirPods Pro", "AirPods"),          # word-boundary preference
        ("Built-in", "Built-in"),            # short, fits
        ("", "None"),                        # empty → "None"
    ])
    def test_truncation_examples(self, name, expected):
        assert truncate_mic(name) == expected


class TestFormatMenuBarTitle:
    """format_menu_bar_title returns dict of title + tooltip + flags."""

    def test_idle_shows_mic_name(self):
        out = format_menu_bar_title(
            state="idle", friendly_mic="Evolve2 75",
        )
        assert "Evolve2 75" in out["title"]
        assert out["tooltip"] == "Mic: Evolve2 75"
        assert out["use_brand_icon"] is True

    def test_listening_overrides_with_label(self):
        out = format_menu_bar_title(
            state="listening", friendly_mic="Evolve2 75",
        )
        assert "Recording" in out["title"]
        assert out["tooltip"] == "Mic: Evolve2 75"  # tooltip stays
        assert out["use_brand_icon"] is False

    def test_mic_warning_overrides_state(self):
        out = format_menu_bar_title(
            state="idle", friendly_mic="Evolve2 75",
            mic_warning="silent mic",
        )
        assert "silent mic" in out["title"]

    def test_held_count_appended(self):
        out = format_menu_bar_title(
            state="idle", friendly_mic="Built-in", held_count=3,
        )
        assert "📥3" in out["title"] or "3" in out["title"]
```

**Full reference implementation of helper:** RESEARCH.md §Code Examples → G (lines 1050-1148). Use as the source for `heyvox/hud/menu_bar_title.py` (new file).

---

### `tests/test_overlay_vi_suffix.py` (unit test, request-response)

**Analog:** `tests/test_mic_profile.py` (existing — covers `MicProfileManager`) + `tests/test_config.py::TestHeyvoxConfigDefaults` for config-loading pattern.

**Imports pattern:**
```python
"""Tests for the voice-isolation suffix appended to mic-submenu entries (D-13)."""

import pytest

from heyvox.config import HeyvoxConfig, MicProfileEntryConfig
```

**Test pattern — verify suffix output without spinning up AppKit:**
```python
class TestVISuffix:
    """`_vi_suffix_for(dev_name)` returns ' · VI: On/Off' or empty."""

    def test_vi_on_when_profile_true(self):
        config = HeyvoxConfig(
            mic_profiles={
                "evolve2": MicProfileEntryConfig(voice_isolation_mode=True),
            },
        )
        # Pure helper extracted from overlay.py for testability
        from heyvox.hud.menu_bar_title import vi_suffix_for_device
        assert vi_suffix_for_device("Evolve2 75 UC", config) == "  ·  VI: On"

    def test_vi_off_when_profile_false(self):
        config = HeyvoxConfig(
            mic_profiles={
                "evolve2": MicProfileEntryConfig(voice_isolation_mode=False),
            },
        )
        from heyvox.hud.menu_bar_title import vi_suffix_for_device
        assert vi_suffix_for_device("Evolve2 75 UC", config) == "  ·  VI: Off"

    def test_no_suffix_when_mode_none(self):
        config = HeyvoxConfig(
            mic_profiles={
                "evolve2": MicProfileEntryConfig(voice_isolation_mode=None),
            },
        )
        from heyvox.hud.menu_bar_title import vi_suffix_for_device
        assert vi_suffix_for_device("Evolve2 75 UC", config) == ""

    def test_no_suffix_when_no_profile_match(self):
        config = HeyvoxConfig()  # empty mic_profiles
        from heyvox.hud.menu_bar_title import vi_suffix_for_device
        assert vi_suffix_for_device("Unknown Headset", config) == ""

    def test_partial_substring_match_case_insensitive(self):
        """Mirrors MicProfileManager substring matching (D-13)."""
        config = HeyvoxConfig(
            mic_profiles={
                "AIRPODS": MicProfileEntryConfig(voice_isolation_mode=True),
            },
        )
        from heyvox.hud.menu_bar_title import vi_suffix_for_device
        assert vi_suffix_for_device("airpods pro max", config).startswith("  ·")


class TestNoAVCaptureDeviceImport:
    """SPEC R5 / acceptance #11: no AVCaptureDevice import added."""

    def test_overlay_does_not_import_avcapturedevice(self):
        import inspect
        from heyvox.hud import overlay
        source = inspect.getsource(overlay)
        assert "AVCaptureDevice" not in source
        assert "AVFoundation" not in source
```

**Reference helper signature:** RESEARCH.md §HUD Submenu (lines 651-684) — `match_profile_key()` exported from `heyvox/audio/profile.py` and `_vi_suffix_for()` in the menu-bar-title module.

---

### `tests/test_setup_wakeword_download.py` (unit test, mocked HTTP+fs)

**Analog:** `tests/test_app_fast_paste.py` (heavy mocking of subprocess + filesystem, lines 1-80).

**Imports + dataclass + helper pattern** — copy from `tests/test_app_fast_paste.py` lines 1-28:
```python
"""Unit tests for the wake-word model download step in heyvox.setup.wizard.

Mocks urllib.request.urlopen + hashlib so tests don't need network access
and don't depend on the real GitHub Releases asset.
"""

from unittest.mock import MagicMock, patch
import hashlib


def _make_mock_response(content: bytes):
    """Build a fake urlopen response."""
    resp = MagicMock()
    resp.headers.get.return_value = str(len(content))
    chunks = [content[i:i+64*1024] for i in range(0, len(content), 64*1024)] + [b""]
    resp.read.side_effect = chunks
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp
```

**Mocking pattern** — copy from `tests/test_app_fast_paste.py` lines 43-48 (multi-patch context manager):
```python
def test_download_writes_file_with_correct_sha256(tmp_path, monkeypatch):
    from heyvox.setup import wizard

    content = b"FAKE_ONNX_BYTES_x100" * 1000
    expected_sha = hashlib.sha256(content).hexdigest()

    monkeypatch.setattr(
        "heyvox.constants.HEY_VOX_MODEL_URL",
        "https://example.com/hey_vox.onnx",
    )
    monkeypatch.setattr(
        "heyvox.constants.HEY_VOX_MODEL_SHA256",
        expected_sha,
    )
    monkeypatch.setattr("heyvox.config.CONFIG_DIR", tmp_path)

    console = MagicMock()
    with patch("urllib.request.urlopen", return_value=_make_mock_response(content)):
        ok = wizard._download_wakeword_model(console, force=False)

    assert ok is True
    target = tmp_path / "models" / "hey_vox.onnx"
    assert target.exists()
    assert target.read_bytes() == content


def test_sha256_mismatch_aborts_and_returns_false(tmp_path, monkeypatch):
    """If sha256 doesn't match, the helper unlinks the temp file and returns False."""
    from heyvox.setup import wizard

    content = b"WRONG_BYTES"
    monkeypatch.setattr(
        "heyvox.constants.HEY_VOX_MODEL_URL", "https://example.com/hey_vox.onnx",
    )
    monkeypatch.setattr(
        "heyvox.constants.HEY_VOX_MODEL_SHA256",
        "0" * 64,  # any sha that doesn't match `content`
    )
    monkeypatch.setattr("heyvox.config.CONFIG_DIR", tmp_path)

    console = MagicMock()
    with patch("urllib.request.urlopen", return_value=_make_mock_response(content)):
        ok = wizard._download_wakeword_model(console, force=False)

    assert ok is False
    target = tmp_path / "models" / "hey_vox.onnx"
    assert not target.exists()


def test_existing_file_preserved_without_force(tmp_path, monkeypatch):
    """D-19: idempotency — existing user-trained model preserved."""
    from heyvox.setup import wizard

    monkeypatch.setattr("heyvox.config.CONFIG_DIR", tmp_path)
    models_dir = tmp_path / "models"
    models_dir.mkdir(parents=True)
    target = models_dir / "hey_vox.onnx"
    target.write_bytes(b"USER_TRAINED_MODEL")

    console = MagicMock()
    with patch("urllib.request.urlopen") as mock_urlopen:
        ok = wizard._download_wakeword_model(console, force=False)

    assert ok is True
    mock_urlopen.assert_not_called()  # No HTTP request — file preserved
    assert target.read_bytes() == b"USER_TRAINED_MODEL"


def test_force_redownload_overrides_idempotency(tmp_path, monkeypatch):
    """`--redownload-wakeword` flag (D-19) bypasses the exists() guard."""
    from heyvox.setup import wizard

    new_content = b"NEW_MODEL_BYTES" * 100
    expected_sha = hashlib.sha256(new_content).hexdigest()

    monkeypatch.setattr(
        "heyvox.constants.HEY_VOX_MODEL_URL", "https://example.com/hey_vox.onnx",
    )
    monkeypatch.setattr(
        "heyvox.constants.HEY_VOX_MODEL_SHA256", expected_sha,
    )
    monkeypatch.setattr("heyvox.config.CONFIG_DIR", tmp_path)
    models_dir = tmp_path / "models"
    models_dir.mkdir(parents=True)
    (models_dir / "hey_vox.onnx").write_bytes(b"OLD_BYTES")

    console = MagicMock()
    with patch("urllib.request.urlopen", return_value=_make_mock_response(new_content)):
        ok = wizard._download_wakeword_model(console, force=True)

    assert ok is True
    assert (models_dir / "hey_vox.onnx").read_bytes() == new_content
```

**Function-local imports gotcha** — patch the source modules, not the consumer:
```python
monkeypatch.setattr("heyvox.constants.HEY_VOX_MODEL_URL", "...")  # source
monkeypatch.setattr("heyvox.config.CONFIG_DIR", tmp_path)         # source
```
Function-local imports in `_download_wakeword_model` re-resolve on every call, so patching the source modules is the right move. See user memory `feedback_function_local_import_patching.md`.

---

### `tests/test_config_defaults.py` (or addition to `tests/test_config.py`)

**Analog:** `tests/test_config.py::TestHeyvoxConfigDefaults` (lines 14-25) — exact match.

**Direct extension (recommend appending to `test_config.py` rather than new file):**
```python
class TestCoDefaultWakeWords:
    """D-18: default config ships with both hey_vox + hey_jarvis_v0.1 active."""

    def test_start_is_hey_vox(self):
        cfg = HeyvoxConfig()
        assert cfg.wake_words.start == "hey_vox"

    def test_stop_defaults_to_start(self):
        cfg = HeyvoxConfig()
        assert cfg.wake_words.stop == "hey_vox"

    def test_also_load_contains_hey_jarvis_fallback(self):
        cfg = HeyvoxConfig()
        assert "hey_jarvis_v0.1" in cfg.wake_words.also_load
```

**Already passes** per `heyvox/config.py:47-54` — `start="hey_vox"`, `also_load=["hey_jarvis_v0.1"]`. The test locks down the current default so accidental edits trip CI.

---

### `pyproject.toml` (MODIFY — classifier bump)

**Analog:** (self — line 16)

**Current** (`pyproject.toml:16`):
```toml
classifiers = [
    "Development Status :: 3 - Alpha",
    ...
]
```

**Target** (D-06, SPEC R2):
```toml
classifiers = [
    "Development Status :: 4 - Beta",
    ...
]
```

Single-line change. Source for diff: RESEARCH.md §Code Examples → B.

**Additional comment** — at top of `[project]` block, document version-of-truth (D-04):
```toml
# Version is the single source of truth. heyvox/__init__.py reads from
# importlib.metadata.version("heyvox"); do NOT hardcode it elsewhere.
version = "1.0.0"
```

---

### `heyvox/__init__.py` (MODIFY — read version from package metadata)

**Analog:** (self — currently a single line; new pattern from RESEARCH.md §PyPI OIDC §Version Sync)

**Current** (`heyvox/__init__.py:1`):
```python
__version__ = "1.0.0"
```

**Target** (D-04, copied verbatim from RESEARCH.md §Code Examples → C):
```python
"""HeyVox — macOS voice layer for AI coding agents."""
from importlib.metadata import version, PackageNotFoundError

try:
    __version__ = version("heyvox")
except PackageNotFoundError:
    __version__ = "0.0.0-dev"
```

The `PackageNotFoundError` fallback handles the editable-install-before-install moment.

---

### `heyvox/hud/overlay.py` (MODIFY — title + tooltip + VI suffix)

**Analog:** (self — existing title build logic at lines 386-428, mic submenu at 1062-1073)

**Existing title-build code (lines 419-424) — the integration point:**
```python
            _idle_suffix = ""
            if _held_count > 0:
                _idle_suffix += f"  \U0001f4e5{_held_count}"
            if _spk_muted:
                _idle_suffix += " \U0001f507"
            btn.setTitle_(_idle_suffix)
```

**Target (D-12)** — replace `_idle_suffix` line with truncated mic + suffix:
```python
            from heyvox.hud.menu_bar_title import truncate_mic
            _mic_short = truncate_mic(_friendly_mic(_active_mic))
            _idle_suffix = ""
            if _held_count > 0:
                _idle_suffix += f"  \U0001f4e5{_held_count}"
            if _spk_muted:
                _idle_suffix += " \U0001f507"
            _title_text = f" {_mic_short}{_idle_suffix}" if _mic_short else _idle_suffix
            btn.setTitle_(_title_text)
            btn.setToolTip_(f"Mic: {_friendly_mic(_active_mic) or 'None'}")
```

**Tooltip set-once point (lines 1745-1757) — initial creation:**
```python
status_item = status_bar.statusItemWithLength_(NSVariableStatusItemLength)
status_button = status_item.button()
...
status_button.setImage_(_brand_menubar_image())
status_button.setTitle_("")
```

Add after `setTitle_("")`:
```python
status_button.setToolTip_("Mic: (initializing)")
```

This sets a baseline tooltip immediately; the per-state `setToolTip_` calls in `_apply_state` keep it fresh.

**`_friendly_mic` helper (lines 997-1010)** — already correct, no change needed. Extract a copy to `heyvox/hud/menu_bar_title.py` for testability OR keep as nested and pass through. Recommend: move to module-level so tests can import it.

**Mic-submenu suffix (lines 1062-1073) — D-13 integration point:**

Current code:
```python
for _dev_name in _input_devices:
    _is_active = _dev_name == _active_mic
    _mic_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        _friendly_mic(_dev_name), "switchMic:", "",
    )
    ...
```

Target (D-13):
```python
from heyvox.hud.menu_bar_title import vi_suffix_for_device
config = load_config()
for _dev_name in _input_devices:
    _is_active = _dev_name == _active_mic
    _vi = vi_suffix_for_device(_dev_name, config)
    _title = f"{_friendly_mic(_dev_name)}{_vi}"
    _mic_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        _title, "switchMic:", "",
    )
    ...
```

`load_config()` is cheap (YAML parse, < 1ms — RESEARCH.md §Pitfall 5) and the rebuild runs on `menuNeedsUpdate_` only, so freshness is guaranteed.

---

### `heyvox/setup/wizard.py` (MODIFY — add `_download_wakeword_model()` step)

**Analog:** (self — existing Kokoro download step at lines 226-256)

**Existing Kokoro pattern (lines 226-256) — exact template:**
```python
    # ---------------------------------------------------------------------------
    # Step 3: Kokoro model download
    # ---------------------------------------------------------------------------
    console.print("[bold]Step 3: Kokoro TTS Model[/bold]")

    kokoro_cache = Path.home() / ".cache" / "huggingface" / "hub" / "models--hexgrad--Kokoro-82M"
    if kokoro_cache.exists():
        console.print("  [green]✓[/green] Kokoro model already downloaded")
    else:
        console.print("  [yellow]![/yellow] Kokoro model not found (~300 MB download required)")
        ...
        download = console.input("  Download now? [y/N] ").strip().lower()
        if download == "y":
            try:
                from huggingface_hub import snapshot_download
                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    console=console,
                ) as progress:
                    progress.add_task("Downloading hexgrad/Kokoro-82M...", total=None)
                    snapshot_download(repo_id="hexgrad/Kokoro-82M")
                console.print("  [green]✓[/green] Kokoro model downloaded successfully")
            except ImportError:
                ...
            except Exception as e:
                console.print(f"  [red]✗[/red] Download failed: {e}")
```

**Target** — insert new "Step 3.5: Wake-word model" after the Kokoro block (between lines 258 and 260):
```python
    # ---------------------------------------------------------------------------
    # Step 3.5: Wake-word model (hey_vox.onnx) — small, always-download
    # ---------------------------------------------------------------------------
    console.print("[bold]Step 3.5: Hey Vox Wake-Word Model[/bold]")
    _download_wakeword_model(console, force=getattr(args, "redownload_wakeword", False))
    console.print()
```

**`_download_wakeword_model()` helper (NEW)** — reference impl in RESEARCH.md §Code Examples → F (lines 979-1046). Copy that body verbatim into `wizard.py` (top-level, near `_register_mcp_agent`).

**Key behavioral contracts** (from D-19):
- Idempotent: `target.exists()` short-circuit
- `force=True` overrides idempotency (CLI: `--redownload-wakeword`)
- sha256 verified; mismatch → unlink temp + return False
- Failure (network, sha mismatch) is non-fatal — wizard continues, `hey_jarvis_v0.1` fallback ensures listener still works

**Constants to add to `heyvox/constants.py`:**
```python
# Wake-word model URL + sha256 — bumped per release (D-17, version-pinned per Q2)
HEY_VOX_MODEL_URL = (
    "https://github.com/heyvox-dev/heyvox/releases/download/v1.0.0/hey_vox.onnx"
)
HEY_VOX_MODEL_SHA256 = "..."  # 64-char hex; updated post-training-run
```

---

### `heyvox/cli.py` (MODIFY — add `--redownload-wakeword` flag)

**Analog:** (self — existing subcommand argument pattern at lines 999-1020 for `sub_speak`)

**Existing pattern (lines 1009-1020) — argparse for sub-command flags:**
```python
sub_speak.add_argument(
    "--speed",
    type=float,
    default=None,
    help="Playback speed multiplier (default: from config, e.g. 1.0)",
)
sub_speak.add_argument(
    "--verbosity",
    choices=["full", "summary", "short", "skip"],
    default=None,
    help="Verbosity mode: full (default) | summary | short | skip",
)
```

**Existing `sub_setup` (lines 983-985) — current state with no flags:**
```python
    # setup
    sub_setup = subparsers.add_parser("setup", help="Run initial setup")
    sub_setup.set_defaults(func=_cmd_setup)
```

**Target (D-19) — add flag between parser creation and `set_defaults`:**
```python
    # setup
    sub_setup = subparsers.add_parser("setup", help="Run initial setup")
    sub_setup.add_argument(
        "--redownload-wakeword",
        action="store_true",
        help="Force re-download of hey_vox.onnx even if already present",
    )
    sub_setup.set_defaults(func=_cmd_setup)
```

`_cmd_setup` (line 108) — pass `args` through to `run_setup` so the wizard can read `args.redownload_wakeword`:
```python
def _cmd_setup(args):
    from heyvox.config import load_config
    from heyvox.setup.wizard import run_setup
    config = load_config()
    run_setup(config, args=args)  # pass args through
```

And `run_setup(config, args=None)` signature gains an optional `args` so existing callers still work.

---

### `config.yaml.example` / `heyvox/config.py:767-770` (MODIFY — comment polish only)

**Analog:** (self — embedded example at `heyvox/config.py:767-770`)

**Current (lines 767-770) — already matches D-18:**
```yaml
wake_words:
  start: hey_vox                   # Model name (from models/ directory)
  stop: hey_vox                    # Leave same as start to toggle; use different for separate start/stop
  also_load: [hey_jarvis_v0.1]     # Extra fallback wake words loaded alongside start/stop
```

**Target** — small comment polish to surface "co-default" framing per CONTEXT.md amendment:
```yaml
wake_words:
  start: hey_vox                   # Primary wake word (custom model in ~/.config/heyvox/models/)
  stop: hey_vox                    # Same model used for stop; change for separate stop word
  also_load: [hey_jarvis_v0.1]     # Fallback wake word — works even if hey_vox.onnx missing
```

If a standalone `config.yaml.example` file is desired at repo root (currently doesn't exist), generate from `heyvox/config.py` embedded YAML. RESEARCH.md §Code Examples → E confirms current state already meets D-18.

---

### `README.md` (MODIFY — wake-word section + first-install warning)

**Analog:** (self — existing "Configuration" section at lines 142-182)

**Current wake-word reference (lines 147-148) — outdated:**
```yaml
wake_words:
  start: hey_jarvis_v0.1   # Wake word model (custom "hey_vox" coming soon)
```

**Target — update to reflect Phase 14 reality:**
```yaml
wake_words:
  start: hey_vox                   # Branded model, downloaded by `heyvox setup`
  also_load: [hey_jarvis_v0.1]     # Fallback (bundled with openwakeword)
```

**New "Customize wake word" subsection** — insert between existing "Configuration" (line 182) and "Supported Agents" (line 184). Linked from `docs/wakeword-training.md`:
```markdown
### Customize the wake word

By default HeyVox listens for "Hey Vox" (custom model) and falls back to
"Hey Jarvis" (bundled). To train your own wake word for your voice or accent,
see [docs/wakeword-training.md](docs/wakeword-training.md). The Colab path
takes about an hour with a free GPU runtime.

If detection feels unreliable, increase the per-model threshold in
`~/.config/heyvox/config.yaml`:

\`\`\`yaml
wake_words:
  model_thresholds:
    hey_vox: 0.6
\`\`\`
```

**First-install warning (D-11)** — add to the existing "Install" section near line 50-60 (PyPI install instructions):
```markdown
### From PyPI

```bash
pip install heyvox
heyvox setup
```

**First install takes 5-10 min** — `mlx-whisper`, `sherpa-onnx`, and other
ML dependencies compile or download Apple Silicon wheels (~200 MB total).
This is one-time; subsequent upgrades are fast.
```

---

## Shared Patterns

### Pattern S1: Lazy imports inside CLI / wizard handlers

**Source:** `heyvox/cli.py:12-26` (`_cmd_start`), `heyvox/setup/wizard.py:122-138` (`run_setup`)

**Excerpt:**
```python
def _cmd_setup(args):
    """Run the interactive guided setup wizard."""
    from heyvox.config import load_config
    from heyvox.setup.wizard import run_setup
    config = load_config()
    run_setup(config)
```

Inside `run_setup`:
```python
def run_setup(config) -> None:
    from rich.console import Console
    from rich.panel import Panel
    ...
```

**Apply to:** New `_cmd_setup` argparse handler (no change needed, already lazy), new `_download_wakeword_model` helper — lazy-import `urllib.request, hashlib, tempfile` inside the function body (mirrors `huggingface_hub` import on line 240).

---

### Pattern S2: Pytest mocking via `unittest.mock.patch` + `monkeypatch.setattr` on source modules

**Source:** `tests/test_app_fast_paste.py:1-80`, `tests/conftest.py:62-117`

**Excerpt (`test_app_fast_paste.py:42-48`):**
```python
with patch("heyvox.input.injection._set_clipboard", return_value=(True, 5)), \
     patch("heyvox.input.injection.get_clipboard_text", return_value="hello"), \
     patch("heyvox.input.injection._get_frontmost_app", return_value="testapp"), \
     patch("heyvox.input.injection.subprocess.run") as mock_run:
    mock_run.return_value = MagicMock(returncode=0, stderr=b"")
    ok = app_fast_paste(profile, "hello")
```

**Apply to:** `tests/test_setup_wakeword_download.py` — patch `heyvox.constants.HEY_VOX_MODEL_URL`, `heyvox.constants.HEY_VOX_MODEL_SHA256`, `heyvox.config.CONFIG_DIR` (the SOURCE modules, not the consumers — function-local imports in `_download_wakeword_model` re-resolve on every call).

---

### Pattern S3: GitHub Actions `macos-14` runner with portaudio brew install + setup-python@v5

**Source:** `.github/workflows/ci.yml:9-29`, `.github/workflows/install-test.yml:9-25`

**Excerpt:**
```yaml
jobs:
  test:
    name: Lint & Test
    runs-on: macos-14

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Set up Python 3.12
        uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install system dependencies
        run: |
          brew install portaudio shellcheck
```

**Apply to:** `.github/workflows/publish.yml` — copy the runner + python-setup block verbatim. Skip `brew install portaudio` in the `build` job (pure-Python wheel build doesn't need it). The `publish` job runs on `ubuntu-latest` per RESEARCH.md (lighter, faster).

---

### Pattern S4: argparse subparser with `set_defaults(func=_cmd_*)` dispatch

**Source:** `heyvox/cli.py:983-1045` (subparser registration)

**Excerpt:**
```python
sub_setup = subparsers.add_parser("setup", help="Run initial setup")
sub_setup.set_defaults(func=_cmd_setup)
```

For multi-arg sub-commands (lines 998-1021):
```python
sub_speak = subparsers.add_parser("speak", help="Speak text via Kokoro TTS")
sub_speak.add_argument("text", nargs="+", ...)
sub_speak.add_argument("--voice", default=None, ...)
sub_speak.set_defaults(func=_cmd_speak)
```

**Apply to:** Adding `--redownload-wakeword` flag to `sub_setup`.

---

### Pattern S5: Rich Console + progress bar for long-running setup steps

**Source:** `heyvox/setup/wizard.py:240-250` (Kokoro download)

**Excerpt:**
```python
from huggingface_hub import snapshot_download
with Progress(
    SpinnerColumn(),
    TextColumn("[progress.description]{task.description}"),
    console=console,
) as progress:
    progress.add_task("Downloading hexgrad/Kokoro-82M...", total=None)
    snapshot_download(repo_id="hexgrad/Kokoro-82M")
console.print("  [green]✓[/green] Kokoro model downloaded successfully")
```

**Apply to:** `_download_wakeword_model()` helper — use `Progress` with `BarColumn + DownloadColumn + TransferSpeedColumn` (RESEARCH.md §Code Examples → F shows the exact import). Determinate progress bar since `Content-Length` header gives the total.

---

### Pattern S6: Embedded YAML example in `config.py` as the single source of truth

**Source:** `heyvox/config.py:760-810` (`# Location: ~/.config/heyvox/config.yaml` comment header + example)

**Excerpt:**
```python
# Location: ~/.config/heyvox/config.yaml
# All values shown are defaults — only override what you need.

# ---------------------------------------------------------------------------
# Wake word detection
# ---------------------------------------------------------------------------

wake_words:
  start: hey_vox                   # Model name (from models/ directory)
  stop: hey_vox                    # Leave same as start to toggle; use different for separate start/stop
  also_load: [hey_jarvis_v0.1]     # Extra fallback wake words loaded alongside start/stop
```

**Apply to:** The `config.yaml.example` file in CONTEXT.md scope. Since no standalone `config.yaml.example` exists at repo root, treat the embedded `heyvox/config.py:760+` block as the example. Only comment polish needed (D-18 already met).

---

### Pattern S7: Pydantic BaseModel + field default + model_validator

**Source:** `heyvox/config.py:47-69` (`WakeWordConfig`)

**Excerpt:**
```python
class WakeWordConfig(BaseModel):
    """Wake word model names for start and stop triggers."""
    start: str = "hey_vox"
    stop: str = ""  # Empty = use same as start
    also_load: list[str] = ["hey_jarvis_v0.1"]
    ...

    @model_validator(mode="after")
    def set_stop_default(self) -> "WakeWordConfig":
        if not self.stop:
            self.stop = self.start
        return self
```

**Apply to:** No new Pydantic models added in Phase 14. Tests validate existing defaults.

---

## No Analog Found

| File | Role | Data Flow | Reason |
|---|---|---|---|
| `Formula/heyvox.rb` | Homebrew formula (Ruby DSL) | declarative | This is a Ruby file in a separate external repo (`heyvox-dev/homebrew-heyvox`). No Ruby precedent in the heyvox codebase. Pattern source: RESEARCH.md §Homebrew Formula Authoring + RESEARCH.md §Code Examples → D. |
| `docs/wakeword-training.md` | docs (Markdown) | docs (user-facing) | The existing `docs/` dir contains only HTML (landing page) and image assets. `training/README.md` exists and is the closest in domain. Use that for tone + structure; content from RESEARCH.md §Wake-Word Training Logistics → "Documentation" (lines 791-813). |

---

## Metadata

**Analog search scope:**
- `.github/workflows/` — 2 files (ci.yml, install-test.yml)
- `heyvox/setup/` — wizard.py, launchd.py, permissions.py, hooks.py
- `heyvox/hud/` — overlay.py (1900+ lines, targeted reads only), ipc.py, menu_bar_title.py (does not yet exist)
- `heyvox/cli.py` — 1100+ lines, scanned argparse + handler patterns
- `heyvox/config.py` — Pydantic models + embedded YAML example
- `heyvox/__init__.py`, `pyproject.toml`
- `tests/` — 47 test files (conftest, test_config, test_hud_ipc, test_app_fast_paste, test_mic_profile, test_defect_guards)
- `training/` — train_model.py, test_model.py, colab_hey_vox.py
- `docs/` — index.html (no Markdown content)
- `README.md` — for documentation analog

**Files scanned:** ~25 files via Read + ~15 via Grep
**Pattern extraction date:** 2026-05-11

---

## PATTERN MAPPING COMPLETE

**Phase:** 14 - Distribution & UX Polish
**Files classified:** 14 (8 new, 6 modified)
**Analogs found:** 12 / 14

### Coverage
- Files with exact analog: 10
- Files with role-match analog: 2
- Files with no analog: 2 (`Formula/heyvox.rb` — Ruby DSL external; `docs/wakeword-training.md` — Markdown user docs, only HTML exists in `docs/`)

### Key Patterns Identified
- `.github/workflows/` files: `runs-on: macos-14` + `actions/setup-python@v5` + `python-version: "3.12"` is the canonical CI runner pattern; publish workflow follows pypa-blessed two-job split (build → publish with `id-token: write`).
- Setup wizard pattern: lazy-import inside `run_setup()`, Rich console + progress bar per step, idempotent `exists()` guards, `try/except` graceful-failure copy. New `_download_wakeword_model` mirrors the Kokoro step at `wizard.py:226-256`.
- Tests pattern: Pydantic `HeyvoxConfig()` instantiation for default-validation tests (`tests/test_config.py:14-25`); `unittest.mock.patch` + `monkeypatch.setattr` on source modules for HTTP/filesystem mocking (`tests/test_app_fast_paste.py:1-80`).
- HUD pattern: extract pure helpers to a new `heyvox/hud/menu_bar_title.py` module (no PyObjC imports) for unit-testability — `truncate_mic`, `format_menu_bar_title`, `vi_suffix_for_device` all callable without spinning up AppKit.
- Wake-word eval: openwakeword `Model(wakeword_models=[path])` + `model.predict(audio)` + `model.prediction_buffer[name]` loop is the canonical inference pattern (`training/test_model.py:60-78`); `model.reset()` between clips when running batch.
- Anti-patterns to enforce: no AVCaptureDevice import (SPEC R5 acceptance #11); no `@master` ref for pypa action; no `pip_install` in Homebrew formula install block; no hardcoded app names in app-profile parsing (CLAUDE.md rule).

### File Created
`/Users/work/conductor/workspaces/vox-v2/seattle/.planning/phases/14-distribution-ux-polish/14-PATTERNS.md`

### Ready for Planning
Pattern mapping complete. Planner can now reference analog patterns in PLAN.md files.
