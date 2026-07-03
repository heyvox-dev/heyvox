"""Guard tests for the model-revision pinning registry (DEF-179).

Net-free: `resolve_pinned` is exercised with `snapshot_download` monkeypatched,
so no network or HF cache is touched. Verifies the pin registry is well-formed,
that resolution degrades gracefully (never raises, never worse than unpinned),
and that model overrides are validated against the trusted org.

References: .planning/DEFECT-LOG.md (DEF-179),
.context/release-audit/05-supply-chain.md
"""

import re

import pytest

from heyvox import model_pins


def test_all_revisions_are_full_commit_shas():
    """Every pin must be a 40-hex commit SHA — never a branch/tag/HEAD."""
    assert model_pins.MODEL_REVISIONS, "registry is empty"
    for repo, sha in model_pins.MODEL_REVISIONS.items():
        assert re.fullmatch(r"[0-9a-f]{40}", sha), f"{repo} pin {sha!r} is not a SHA"


def test_kokoro_repo_is_the_loaded_one_not_the_dead_hexgrad_repo():
    """The wizard must download the repo the runtime loads (DEF-179)."""
    assert model_pins.KOKORO_REPO == "mlx-community/Kokoro-82M-bf16"
    assert model_pins.KOKORO_REPO in model_pins.MODEL_REVISIONS
    assert "hexgrad" not in model_pins.KOKORO_REPO


def test_revision_for_known_and_unknown():
    known = "mlx-community/whisper-small-mlx"
    assert model_pins.revision_for(known) == model_pins.MODEL_REVISIONS[known]
    assert model_pins.revision_for("someone/custom-model") is None


def test_resolve_pinned_uses_revision_for_known_model(monkeypatch):
    calls = {}

    def fake_snapshot_download(repo_id, revision=None, **kw):
        calls["repo_id"] = repo_id
        calls["revision"] = revision
        return f"/fake/cache/{repo_id}/snapshots/{revision}"

    monkeypatch.setattr(
        "huggingface_hub.snapshot_download", fake_snapshot_download
    )
    repo = "mlx-community/whisper-small-mlx"
    out = model_pins.resolve_pinned(repo)
    assert calls["repo_id"] == repo
    assert calls["revision"] == model_pins.MODEL_REVISIONS[repo]
    assert out.endswith(model_pins.MODEL_REVISIONS[repo])


def test_resolve_pinned_passes_through_unknown_model(monkeypatch):
    """A custom model has no pin: resolve returns it unchanged, no download."""
    def boom(*a, **k):  # must not be called for an unpinned model
        raise AssertionError("snapshot_download should not run for unknown repo")

    monkeypatch.setattr("huggingface_hub.snapshot_download", boom)
    assert model_pins.resolve_pinned("someone/custom") == "someone/custom"


def test_resolve_pinned_falls_back_to_repo_id_on_error(monkeypatch):
    """A resolution failure degrades to the bare repo id — never raises."""
    def boom(*a, **k):
        raise RuntimeError("offline / HF down")

    monkeypatch.setattr("huggingface_hub.snapshot_download", boom)
    repo = "mlx-community/Kokoro-82M-bf16"
    # Must not raise, and must not return something worse than the repo id.
    assert model_pins.resolve_pinned(repo) == repo


def test_validate_model_override():
    default = "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-bf16"
    # Empty / None -> default.
    assert model_pins.validate_model_override("", default) == default
    assert model_pins.validate_model_override(None, default) == default
    # Trusted-org override -> accepted.
    trusted = "mlx-community/Qwen3-TTS-12Hz-1.7B-Base-bf16"
    assert model_pins.validate_model_override(trusted, default) == trusted
    # Untrusted repo -> rejected, falls back to default.
    assert (
        model_pins.validate_model_override("evil/backdoor-tts", default) == default
    )


def test_validate_model_override_accepts_existing_local_dir(tmp_path):
    default = "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-bf16"
    assert model_pins.validate_model_override(str(tmp_path), default) == str(tmp_path)
    # A non-existent local-looking path is not a dir -> rejected.
    missing = str(tmp_path / "nope")
    assert model_pins.validate_model_override(missing, default) == default


if __name__ == "__main__":
    pytest.main([__file__, "-q"])
