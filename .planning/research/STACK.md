# Stack Research

**Domain:** macOS voice layer — v1.2 Polish & Reliability milestone additions
**Researched:** 2026-04-12
**Confidence:** HIGH (existing stack validated; additions verified against PyPI + official docs)

---

## Scope

This document covers ONLY new stack additions for v1.2. The existing stack (Python 3.12+, PyObjC, openwakeword, MLX Whisper, sherpa-onnx, Kokoro TTS, FastMCP, pyaudio, sounddevice, Pydantic, launchd, CoreAudio ctypes) is validated and unchanged.

Four new capability areas:

1. **Paste/injection reliability** — improve osascript clipboard pipeline
2. **Distribution prep** — Homebrew tap + PyPI publishing
3. **Test stability** — fix 6 stale failures, mock subprocess properly
4. **WebSocket modernization** — fix deprecation warning in chrome/bridge.py

---

## Recommended Stack Additions

### Paste / Injection Reliability

No new library additions needed. The reliability issues are in osascript subprocess call patterns, not missing libraries.

**Root cause (observed from test failures):**
- `test_basic_paste` expects 2 subprocess calls but sees 4 — injection.py added two focus detection calls (`_get_frontmost_pid`, `_restore_frontmost`) that the tests predate.
- Fix: update tests to match current call count, OR extract focus detection calls to a separate `subprocess.run` wrapper so they can be isolated.

**What does help (no new deps):**
- Use `NSPasteboard` directly via PyObjC (already a dependency) instead of `pbcopy` subprocess for clipboard write. This eliminates one subprocess call and is faster.
- `NSPasteboard.generalPasteboard()` with `setString:forType:` is synchronous and avoids the fork overhead of `pbcopy`.

**Pattern:**
```python
from AppKit import NSPasteboard, NSStringPboardType
pb = NSPasteboard.generalPasteboard()
pb.clearContents()
pb.setString_forType_(text, NSStringPboardType)
```

This is already within `pyobjc-framework-Cocoa` (existing dep). No new package needed.

### Test Stability

Two additions needed:

| Library | Version | Purpose | Why |
|---------|---------|---------|-----|
| `pytest-mock` | `>=3.15` | Cleaner mock fixtures via `mocker` | Replaces brittle `@patch` stacking with fixture injection; mock count assertions become `mocker.patch(...).call_count`. Standard in pytest ecosystem. |
| `pytest-subprocess` | `>=1.5` | Mock subprocess calls without spawning real processes | The 6 stale test failures are all "wrong call count" errors — injection and media tests patch `subprocess.run` but don't account for new focus-detection calls added since tests were written. pytest-subprocess lets you register specific command patterns and assert on them separately from other subprocess calls. |

**Alternative considered:** `pytest-rerunfailures` for flaky tests. Rejected — re-running is a band-aid. These are test logic bugs (wrong call count assertions), not non-deterministic failures.

**pytest-asyncio** (already a dev dep, version 1.3.0 installed) — already configured. The `asyncio_mode = auto` should be set in `[tool.pytest.ini_options]` in pyproject.toml to suppress per-test marker warnings.

### Distribution: PyPI Publishing

| Tool | Version | Purpose | Why |
|------|---------|---------|-----|
| `build` | `>=1.4` | Build sdist + wheel from pyproject.toml | Standard PyPA frontend, already installed (1.4.2). `python -m build` produces `dist/`. |
| `twine` | `>=6.2` | Upload to PyPI | Mature, simple. Already installed (6.2.0). |

**Recommended workflow:** PyPA Trusted Publishing via `pypa/gh-action-pypi-publish` GitHub Action. Uses OIDC instead of API tokens — no secret to rotate. Set up once in PyPI project settings, then:

```yaml
permissions:
  id-token: write
jobs:
  publish:
    steps:
      - uses: pypa/gh-action-pypi-publish@release/v1
```

**Package name:** `heyvox` is already registered on PyPI (returns HTTP 200) with description "Voice coding on macOS — coming soon" and no author listed — this appears to be a placeholder/squatter registration. **Verify ownership before publishing.** If squatted, claim via PyPI dispute process or use `heyvox-app`.

**Do NOT use:** hatch/hatchling as build backend. pyproject.toml currently uses `setuptools` build backend which works. Migration would touch `[build-system]` table without benefit for this milestone.

### Distribution: Homebrew Tap

No new Python tooling needed. The Homebrew formula is a Ruby `.rb` file in a `homebrew-heyvox` GitHub repo.

**Approach:** Custom tap (not homebrew-core). homebrew-core requires bottles for all macOS versions + CI; a custom tap avoids that.

**Tool for generating resource stanzas:** `homebrew-pypi-poet` (install in a venv, run `poet heyvox`, outputs resource blocks). Not a project dep — a one-time authoring tool.

**Formula skeleton:**
```ruby
class Heyvox < Formula
  include Language::Python::Virtualenv
  desc "macOS voice layer for AI coding agents"
  homepage "https://heyvox.dev"
  url "https://files.pythonhosted.org/packages/.../heyvox-1.2.0.tar.gz"
  sha256 "..."
  license "MIT"

  depends_on "python@3.12"
  depends_on "portaudio"        # required by pyaudio

  # resource blocks generated by homebrew-pypi-poet
  resource "pydantic" do ... end
  # ... etc

  def install
    virtualenv_install_with_resources
  end

  test do
    assert_match "heyvox", shell_output("#{bin}/heyvox --help")
  end
end
```

**Key constraint:** Homebrew formula must bundle ALL Python dependencies as `resource` blocks — it cannot pull from PyPI at install time. `homebrew-pypi-poet` automates this. Regenerate resource blocks for every version bump.

**Note on heavy ML deps:** MLX Whisper, openwakeword, and sherpa-onnx are large (~hundreds of MB). Homebrew formula size may be impractical. Consider making the Homebrew formula install only the core CLI + on-demand model download (`heyvox setup` already handles this). The Homebrew tap is a convenience wrapper; `pipx install heyvox` remains the primary distribution method.

### WebSocket Modernization (websockets 14+ migration)

`websockets` 15.0.1 is installed. The legacy `websockets.server.serve` API was deprecated in 14.0. Current `chrome/bridge.py` triggers `DeprecationWarning: websockets.server.serve is deprecated`.

**Fix (no new deps):** Migrate `chrome/bridge.py` to the new asyncio API:

```python
# Old (deprecated in 14.0)
import websockets.server
self._server = await websockets.server.serve(self._handler, host, port)

# New (websockets >= 14)
from websockets.asyncio.server import serve
self._server = await serve(self._handler, host, port)
```

The new API is a drop-in for this usage pattern. Handler signature changes slightly — `websocket` parameter now has type `websockets.asyncio.server.ServerConnection` instead of `WebSocketServerProtocol`, but the send/recv interface is identical.

Pin `websockets>=14.0` in pyproject.toml `[project.optional-dependencies].chrome` to lock to the version that introduced the new API.

---

## Dependency Changes to pyproject.toml

```toml
[project.optional-dependencies]
# existing entries unchanged, additions:
chrome = [
    "websockets>=14.0",   # was >=13.0; locks to new asyncio API
]

[project.optional-dependencies.dev]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "pytest-mock>=3.15",        # NEW — cleaner mock fixtures
    "pytest-subprocess>=1.5",   # NEW — subprocess call assertions
    "ruff>=0.15",
    "build>=1.4",
    "twine>=6.2",
]

[tool.pytest.ini_options]
asyncio_mode = "auto"           # suppress per-test asyncio marker warnings
```

---

## Alternatives Considered

| Recommended | Alternative | Why Not |
|-------------|-------------|---------|
| `pytest-mock` + `pytest-subprocess` | Fix `@patch` counts manually | Manual fixes break again when injection.py evolves; test-level subprocess mocking is more robust |
| NSPasteboard via PyObjC (existing dep) | `pbcopy` subprocess | pbcopy adds subprocess fork latency and a subprocess call that tests have to account for |
| Custom Homebrew tap | homebrew-core submission | Core requires bottles for all macOS versions + CI; impractical for Apple Silicon-only app with heavy ML deps |
| `pypa/gh-action-pypi-publish` + OIDC | `twine upload` with token | Tokens must be rotated; OIDC is token-free and the current PyPI best practice |
| websockets asyncio API | Pin `websockets<14` | Pinning old major is a dead end; legacy will be removed by 2030; migration is minimal |

## What NOT to Add

| Avoid | Why | Use Instead |
|-------|-----|-------------|
| `pytest-rerunfailures` | Masks root cause of test failures | Fix the test logic (wrong call count assertions from stale tests) |
| `hatchling` build backend | Unnecessary migration cost | Keep `setuptools`; both produce valid wheels |
| `pasteboard` PyPI package | Thin wrapper around NSPasteboard | Use NSPasteboard directly via existing `pyobjc-framework-Cocoa` dep |
| `poetry` | Too heavy, not in existing workflow | `build` + `twine` is sufficient for single-maintainer project |
| Accessibility framework (AXUIElement) for injection | Requires per-app AX element hunting, fragile across app updates | osascript + NSPasteboard is the correct tradeoff for universal injection |

## Version Compatibility

| Package | Compatible With | Notes |
|---------|-----------------|-------|
| `websockets>=14.0` | Python 3.12+ | New asyncio API requires 14.0+; 15.x is current stable |
| `pytest-mock>=3.15` | pytest 9.x | Already using pytest 9.0.2; compatible |
| `pytest-subprocess>=1.5` | pytest 8+, Python 3.12+ | Current latest is 1.5.4 |
| `pytest-asyncio>=0.23` (1.3.0 installed) | asyncio_mode=auto | 1.x series is stable; no change needed |
| `build>=1.4` (1.4.2 installed) | setuptools build-backend | Already installed, just add to dev deps |

## Integration Notes

**Paste fix (NSPasteboard):** The clipboard write in `heyvox/input/injection.py` (`_osascript_type_text`) currently does:
1. `subprocess.run(["pbcopy"], input=text.encode(), ...)` — clipboard write
2. Verify via `get_clipboard_text()` 
3. `subprocess.run(["osascript", "-e", keystroke_cmd_v])` — Cmd-V

Replacing step 1 with NSPasteboard call removes one subprocess, makes verify faster, and reduces test mock complexity to 1 expected `subprocess.run` call (just the Cmd-V).

**Test fixes:** The 6 failing tests are all in `test_injection.py`, `test_e2e.py`, and `test_media.py`. Failures are:
- `test_injection.py`: expects `call_count == 2` but gets 4 (focus detection added since tests written)
- `test_media.py`: patches `_browser_has_video_tab` which no longer exists in `media.py` (renamed/removed)
- `test_e2e.py::TestTimingBaseline::test_stt_latency`: timing assertion, likely environment-sensitive

These are test maintenance issues, not library gaps.

---

## Sources

- PyPI heyvox package check: https://pypi.org/project/heyvox/ — "Voice coding on macOS — coming soon", no author (possible squatter/placeholder)
- websockets deprecation: https://websockets.readthedocs.io/en/stable/project/changelog.html — 14.0 deprecated legacy in Nov 2024
- websockets migration guide: https://websockets.readthedocs.io/en/stable/howto/upgrade.html — asyncio API drop-in migration
- pytest-subprocess: https://pypi.org/project/pytest-subprocess/ — v1.5.4, Simon Willison TIL confirms subprocess mocking pattern
- pytest-mock: https://pypi.org/project/pytest-mock/ — v3.15.1
- PyPI Trusted Publishing: https://docs.pypi.org/trusted-publishers/ — OIDC-based, recommended since 2023
- pypa/gh-action-pypi-publish: https://github.com/pypa/gh-action-pypi-publish — official GitHub Action
- Homebrew Python formula: https://docs.brew.sh/Python-for-Formula-Authors — virtualenv_install_with_resources pattern
- homebrew-pypi-poet: https://github.com/tdsmith/homebrew-pypi-poet — resource stanza generator
- Simon Willison Homebrew packaging: https://til.simonwillison.net/homebrew/packaging-python-cli-for-homebrew
- build (PyPA): https://pypi.org/project/build/ — v1.4.3 latest

---
*Stack research for: HeyVox v1.2 Polish & Reliability (subsequent milestone)*
*Researched: 2026-04-12*
