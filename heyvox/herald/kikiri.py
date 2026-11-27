"""kikiri German Kokoro weights: download, convert to MLX, locate.

kikiri (https://huggingface.co/kikiri-tts, Apache-2.0) publishes German
single-speaker Kokoro/StyleTTS2 checkpoints as PyTorch ``.pth`` files. The
Kokoro daemon runs mlx-audio, which loads a directory holding ``config.json``
plus ``*.safetensors`` and voice packs as ``voices/<name>.safetensors``. This
module builds that directory once per speaker under the user cache dir.

Conversion needs torch (to read the ``.pth``), which the Kokoro daemon blocks
on import (DEF-193), so the daemon runs it as a subprocess:

    python -m heyvox.herald.kikiri prepare martin

Two non-obvious details:

1. kikiri stores weight-norm parameters in the new torch parametrization
   layout (``parametrizations.weight.original0/1``). Kokoro expects the old
   ``weight_g``/``weight_v`` names. Loading without the rename leaves 178
   conv weights at random init and produces noise (silently, with torch
   ``strict=False``). After the rename the key set and every tensor shape
   must match the reference MLX Kokoro checkpoint exactly; ``prepare``
   verifies that and refuses to write a model otherwise.
2. mlx-audio picks the model class from ``config.json``'s ``model_type`` or
   from the directory name; the reference config has no ``model_type``, so it
   is added here.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

SPEAKERS = ("martin", "victoria")

_REF_REPO = "mlx-community/Kokoro-82M-bf16"
_REF_WEIGHTS = "kokoro-v1_0.safetensors"


def repo_for(speaker: str) -> str:
    return f"kikiri-tts/kikiri-german-{speaker}"


def _revision(repo: str) -> str:
    from heyvox.model_pins import MODEL_REVISIONS

    return MODEL_REVISIONS[repo]


def cache_root() -> Path:
    env = os.environ.get("HEYVOX_KIKIRI_DIR")
    if env:
        return Path(env)
    from platformdirs import user_cache_dir

    return Path(user_cache_dir("heyvox")) / "kikiri"


def model_dir(speaker: str) -> Path:
    """Directory the converted model for ``speaker`` lives in (may not exist)."""
    rev = _revision(repo_for(speaker))
    return cache_root() / f"{speaker}-{rev[:8]}"


def is_prepared(speaker: str) -> bool:
    d = model_dir(speaker)
    return (
        (d / "config.json").is_file()
        and (d / "model.safetensors").is_file()
        and (d / "voices" / f"{speaker}.safetensors").is_file()
    )


def _rename_key(key: str) -> str:
    key = key.replace("parametrizations.weight.original0", "weight_g")
    key = key.replace("parametrizations.weight.original1", "weight_v")
    if key.startswith("module."):
        key = key[len("module."):]
    return key


def flatten_state_dict(sd: dict) -> dict:
    """kikiri ``{module: {param: tensor}}`` → flat Kokoro keys ``module.param``."""
    flat = {}
    for module, params in sd.items():
        for key, value in params.items():
            flat[f"{module}.{_rename_key(key)}"] = value
    return flat


def prepare(speaker: str, log=print) -> Path:
    """Download + convert ``speaker`` if needed; return the model directory."""
    if speaker not in SPEAKERS:
        raise ValueError(f"unknown kikiri speaker {speaker!r} (known: {SPEAKERS})")
    target = model_dir(speaker)
    if is_prepared(speaker):
        return target

    import mlx.core as mx
    import numpy as np
    import torch
    from huggingface_hub import hf_hub_download
    from safetensors import safe_open

    repo = repo_for(speaker)
    rev = _revision(repo)
    log(f"kikiri: downloading {repo}@{rev[:8]}")
    pth = hf_hub_download(repo, f"kikiri_german_{speaker}_ep10.pth", revision=rev)
    voice_pt = hf_hub_download(repo, f"voices/{speaker}.pt", revision=rev)
    ref_rev = _revision(_REF_REPO)
    ref_config = hf_hub_download(_REF_REPO, "config.json", revision=ref_rev)
    ref_weights = hf_hub_download(_REF_REPO, _REF_WEIGHTS, revision=ref_rev)

    sd = torch.load(pth, map_location="cpu", weights_only=True)
    flat = flatten_state_dict(sd)

    with safe_open(ref_weights, "np") as ref:
        ref_shapes = {k: tuple(ref.get_slice(k).get_shape()) for k in ref.keys()}
    missing = sorted(set(ref_shapes) - set(flat))
    extra = sorted(set(flat) - set(ref_shapes))
    bad_shape = sorted(
        k for k in set(ref_shapes) & set(flat) if tuple(flat[k].shape) != ref_shapes[k]
    )
    if missing or extra or bad_shape:
        raise RuntimeError(
            f"kikiri {speaker}: checkpoint does not match Kokoro layout "
            f"(missing={missing[:5]} extra={extra[:5]} shape={bad_shape[:5]})"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".{speaker}-", dir=target.parent))
    try:
        (tmp / "voices").mkdir()
        weights = {
            k: mx.array(v.float().numpy()).astype(mx.bfloat16) for k, v in flat.items()
        }
        mx.save_safetensors(str(tmp / "model.safetensors"), weights)

        voice = torch.load(voice_pt, map_location="cpu", weights_only=True)
        mx.save_safetensors(
            str(tmp / "voices" / f"{speaker}.safetensors"),
            {"voice": mx.array(np.asarray(voice.float().numpy()))},
        )

        config = json.loads(Path(ref_config).read_text())
        config["model_type"] = "kokoro"
        (tmp / "config.json").write_text(json.dumps(config, indent=1))
        (tmp / "SOURCE").write_text(f"{repo}@{rev}\n")

        if target.exists():
            shutil.rmtree(target)
        tmp.rename(target)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    log(f"kikiri: {speaker} ready in {target}")
    return target


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) == 2 and argv[0] == "prepare":
        prepare(argv[1], log=lambda m: print(m, file=sys.stderr, flush=True))
        return 0
    if len(argv) == 2 and argv[0] == "path":
        print(model_dir(argv[1]))
        return 0 if is_prepared(argv[1]) else 1
    print("usage: python -m heyvox.herald.kikiri prepare|path <speaker>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
