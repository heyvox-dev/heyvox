"""Guard tests for seed_only TTS initialisation (DEF-228).

verbosity and style live in shared files under $TMPDIR so Herald's bash hooks
can read them. That makes them process-global state with several writers:

  - the listener daemon, which legitimately owns them and resets on service start
  - `heyvox speak`, a one-shot process
  - the MCP server, of which the stdio transport starts ONE PER AGENT SESSION

Only the first may reset unconditionally. These tests pin that, because the
failure is silent: the user sets verbosity to "short", opens another editor
session, and it is quietly "full" again with nothing logged.

This was latent until the plugin (DEF-227) made MCP registration actually take
effect — before that the stdio server was registered into a file Claude Code
does not read for MCP, so it rarely started at all.
"""

import os
from types import SimpleNamespace

import pytest

from heyvox.audio import tts


@pytest.fixture()
def shared_state(tmp_path, monkeypatch):
    """Point the shared verbosity/style files at a throwaway dir."""
    verbosity = tmp_path / "heyvox-verbosity"
    style = tmp_path / "heyvox-tts-style"
    mute = tmp_path / "herald-mute"
    # set_verbosity imports VERBOSITY_FILE function-locally, so patching the
    # constant reaches it. set_tts_style uses a module-level alias bound at
    # import — that one has to be patched on the tts module itself.
    monkeypatch.setattr("heyvox.constants.VERBOSITY_FILE", str(verbosity))
    monkeypatch.setattr("heyvox.constants.HERALD_MUTE_FLAG", str(mute))
    monkeypatch.setattr(tts, "_STYLE_FILE", str(style))
    return verbosity, style


def _config(verbosity="full", style="detailed", engine="kokoro"):
    return SimpleNamespace(tts=SimpleNamespace(
        verbosity=verbosity, style=style, engine=engine,
    ))


def test_seed_only_preserves_runtime_verbosity(shared_state):
    """A new per-session process must not undo `heyvox quiet`."""
    verbosity, _ = shared_state
    tts.set_verbosity("short")           # user asked for short at runtime
    assert verbosity.read_text() == "short"

    # Config default is "full" — an unconditional reset would DELETE the file.
    tts.start_worker(_config(verbosity="full"), seed_only=True)

    assert verbosity.exists(), "per-session init wiped the user's verbosity"
    assert verbosity.read_text() == "short"


def test_seed_only_preserves_runtime_style(shared_state):
    """Same for the TTS style, which the HUD menu and voice_config also set."""
    _, style = shared_state
    tts.set_tts_style("briefing")
    assert style.read_text().strip() == "briefing"

    tts.start_worker(_config(style="detailed"), seed_only=True)

    assert style.read_text().strip() == "briefing"


def test_seed_only_still_seeds_when_nothing_is_set(shared_state):
    """A genuinely fresh machine must still get the configured defaults."""
    verbosity, style = shared_state
    assert not verbosity.exists() and not style.exists()

    tts.start_worker(_config(verbosity="short", style="technical"), seed_only=True)

    assert verbosity.read_text() == "short"
    assert style.read_text().strip() == "technical"


def test_daemon_path_still_resets_unconditionally(shared_state):
    """The listener owns these files — service start is a legitimate reset."""
    verbosity, style = shared_state
    tts.set_verbosity("short")
    tts.set_tts_style("briefing")

    tts.start_worker(_config(verbosity="full", style="detailed"), seed_only=False)

    # Both defaults are represented by the file's absence: set_verbosity("full")
    # and set_tts_style("detailed") remove theirs rather than writing the value.
    assert not verbosity.exists()
    assert not style.exists()


def test_engine_env_is_exported_either_way(shared_state, monkeypatch):
    """Herald reads the engine from the environment — both paths must set it."""
    monkeypatch.delenv("HEYVOX_TTS_ENGINE", raising=False)
    tts.start_worker(_config(engine="kokoro"), seed_only=True)
    assert os.environ["HEYVOX_TTS_ENGINE"] == "kokoro"
