"""Suppress dead PyTorch weight in the Kokoro TTS daemon (DEF-193).

The MLX Kokoro path never uses PyTorch: inference runs on Metal/MLX and the
English g2p is the numpy-backed ``en_core_web_sm`` spaCy model. But importing
``misaki.en`` (Kokoro's g2p) does ``import spacy`` → ``thinc/compat.py``, which
eager-imports ``torch`` purely to probe for a torch backend it never uses here.
On the shared venv that also holds torch (mlx-whisper, silero-vad, torchaudio,
...) this maps ~230 MB of libtorch into the daemon that no code path touches.

``install_torch_suppressor()`` installs a ``sys.meta_path`` finder that makes
``import torch`` raise ``ModuleNotFoundError`` inside the daemon process only.
thinc catches that in its own ``try/except ImportError`` and sets
``has_torch=False`` — spaCy + ``en_core_web_sm`` keep working, torch stays out.

It also hides the ``spacy-curated-transformers`` entry-points. ``spacy.load()``
eager-loads every ``spacy_*`` entry-point via catalogue, and that torch-dependent
plugin (dragged in by ``misaki[en]`` but never used for Kokoro) would otherwise
trip the torch block and break ``spacy.load()``. Filtering its entry-points makes
spaCy skip it cleanly, whether or not the package is still installed.

Opt out with ``KOKORO_ALLOW_TORCH=1``. stdlib-only, so the daemon can import it
even from a lean venv where ``heyvox`` is only on ``sys.path``.
"""

import importlib.metadata as _md
import os
import sys

# Root package names whose import is blocked. Sub-imports (torch.nn, ...) are
# caught too because we match on the first path component.
_BLOCKED_ROOTS = frozenset({"torch", "torchaudio", "torchvision"})


class _TorchBlockingFinder:
    """meta_path finder that raises ModuleNotFoundError for torch imports.

    thinc / any well-behaved optional-torch consumer wraps ``import torch`` in
    ``try/except ImportError`` — ModuleNotFoundError is an ImportError subclass,
    so blocking this way is indistinguishable from torch simply not being
    installed, without actually uninstalling it (it's a hard dep of mlx-whisper
    et al. in the shared venv).
    """

    def find_spec(self, fullname, path, target=None):
        if fullname.split(".")[0] in _BLOCKED_ROOTS:
            raise ModuleNotFoundError(
                f"torch suppressed in kokoro-daemon ({fullname}); "
                "set KOKORO_ALLOW_TORCH=1 to re-enable"
            )
        return None  # defer every other name to the normal finders


def _allow_torch():
    return os.environ.get("KOKORO_ALLOW_TORCH", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


def _is_curated_entry_point(ep):
    """True if an entry-point belongs to spacy-curated-transformers."""
    value = (getattr(ep, "value", "") or "").lower()
    name = (getattr(ep, "name", "") or "").lower()
    return "curated" in value or "curated" in name


def _install_entry_point_filter():
    """Patch importlib.metadata.entry_points to hide curated-transformers.

    Idempotent: the wrapper is tagged so a second call is a no-op.
    """
    orig = _md.entry_points
    if getattr(orig, "_kokoro_filtered", False):
        return

    def filtered(*args, **kwargs):
        eps = orig(*args, **kwargs)
        try:
            kept = [e for e in eps if not _is_curated_entry_point(e)]
            return _md.EntryPoints(kept)
        except Exception:
            # Any unexpected shape (old dict API, etc.) — leave untouched.
            return eps

    filtered._kokoro_filtered = True
    _md.entry_points = filtered


def install_torch_suppressor():
    """Block torch + hide curated-transformers unless opted out.

    Returns True if suppression was installed, False if opted out
    (KOKORO_ALLOW_TORCH) or torch was already imported (too late to help).
    """
    if _allow_torch():
        return False
    if "torch" in sys.modules:
        # Already loaded — blocking now can't reclaim the memory and could break
        # code holding a torch reference. No-op.
        return False
    if not any(isinstance(f, _TorchBlockingFinder) for f in sys.meta_path):
        sys.meta_path.insert(0, _TorchBlockingFinder())
    _install_entry_point_filter()
    return True
