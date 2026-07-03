"""Pinned HuggingFace model revisions for supply-chain integrity (DEF-179).

Canonical registry of the exact commit SHAs HeyVox's default models are pinned
to. An upstream repo update cannot then silently change the weights a fresh
install pulls: loading resolves the specific tested commit, not a moving `main`.

Only *default* models are pinned. A user-configured custom model (e.g. a
different STT repo) has no registry entry and loads unpinned — logged, never
broken. Pinning must never make loading worse than the unpinned baseline.

The mlx loaders accept a pin differently, so two accessors are provided:

- ``revision_for(repo)`` — the SHA, for loaders that take a ``revision=`` kwarg
  (``mlx_audio.load_model``; used by the TTS daemons, which inline their SHA
  because they run in a separate interpreter that cannot import heyvox).
- ``resolve_pinned(repo)`` — a *local snapshot dir* at the pinned revision, for
  loaders that take only a path and expose no revision (``mlx_whisper``; used by
  ``heyvox/audio/stt.py``). Falls back to the repo id on any resolution error.

Keep the daemon-inlined SHAs (kokoro-daemon.py, qwen-daemon.py) in sync with
``MODEL_REVISIONS`` here.
"""

from __future__ import annotations

import os
from typing import Callable

# repo_id -> pinned commit SHA. Update deliberately (verify the SHA against the
# HF repo); never point at a branch or tag — those move.
MODEL_REVISIONS: dict[str, str] = {
    "mlx-community/Kokoro-82M-bf16": "a71e4d38b236d968966a2002c4c895dbd12b1c3c",
    "mlx-community/whisper-small-mlx": "45f3915923c7a79a5a5b5a7d909d39aeb0e5630e",
    "mlx-community/Qwen3-TTS-12Hz-0.6B-Base-bf16": "1eccf1cb2519b5a4e8a95b5f0544f3303568164f",
}

# The Kokoro repo the runtime actually loads — NOT hexgrad/Kokoro-82M, which the
# setup wizard used to download but nothing ever loads (DEF-179).
KOKORO_REPO = "mlx-community/Kokoro-82M-bf16"

# Every model HeyVox ships comes from this trusted org. A model override (e.g.
# the QWEN_TTS_MODEL env var) must come from here or be an existing local dir.
TRUSTED_REPO_PREFIX = "mlx-community/"


def revision_for(repo_id: str) -> str | None:
    """Return the pinned commit SHA for a known default model, else None."""
    return MODEL_REVISIONS.get(repo_id)


def resolve_pinned(repo_id: str, log: Callable[[str], None] | None = None) -> str:
    """Resolve a model ref to a local snapshot dir at its pinned revision.

    Returns a local path when ``repo_id`` is a pinned default model and the
    revision resolves; otherwise returns ``repo_id`` unchanged (custom/unknown
    model, or a resolution failure). NEVER raises — pinning must not make loading
    worse than the unpinned baseline.

    Args:
        repo_id: HF repo id (an already-local path is returned unchanged).
        log: optional callable(str) for a one-line status / fallback message.
    """
    sha = MODEL_REVISIONS.get(repo_id)
    if not sha:
        return repo_id  # custom / unknown model — load as-is (unpinned)
    try:
        from huggingface_hub import snapshot_download

        local = snapshot_download(repo_id, revision=sha)
        if log:
            log(f"model pin: {repo_id}@{sha[:8]} -> local snapshot")
        return local
    except Exception as exc:  # network / cache / HF error — degrade to unpinned
        if log:
            log(f"model pin: could not resolve {repo_id}@{sha[:8]} ({exc}); loading unpinned")
        return repo_id


def validate_model_override(
    raw: str | None, default: str, log: Callable[[str], None] | None = None
) -> str:
    """Return ``raw`` if it is a trusted model override, else ``default``.

    An override (from an env var etc.) must come from ``TRUSTED_REPO_PREFIX`` or
    be an existing local directory. Anything else — e.g. an attacker-set env
    value pointing at an arbitrary repo — is rejected with a log line, and
    ``default`` is used. Defense-in-depth against an unvalidated model source.
    """
    raw = (raw or "").strip()
    if not raw:
        return default
    if raw.startswith(TRUSTED_REPO_PREFIX) or os.path.isdir(raw):
        return raw
    if log:
        log(
            f"model override {raw!r} rejected (must start with "
            f"{TRUSTED_REPO_PREFIX!r} or be a local dir); using {default!r}"
        )
    return default
