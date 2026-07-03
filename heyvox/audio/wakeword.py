"""
Wake word model management for heyvox.

Thin wrapper around openwakeword Model loading. Supports custom .onnx models
(e.g. trained "hey_vox" model) with automatic fallback to built-in models.

Requirement: Phase 8 custom wake word support
"""

import os
from typing import Any


def _find_model_file(model_name: str, search_dirs: list[str]) -> str:
    """Find a wake word model file by name.

    Searches for {model_name}.onnx in each directory. Returns the model path
    if found, otherwise returns the model name as-is (openwakeword will try
    to load it as a built-in model).

    Args:
        model_name: Model name (e.g. "hey_vox" or "hey_jarvis_v0.1").
        search_dirs: List of directories to search for custom .onnx files.

    Returns:
        Full path to .onnx file if found, otherwise the model name string.
    """
    for d in search_dirs:
        custom_path = os.path.join(d, f"{model_name}.onnx")
        if os.path.exists(custom_path):
            return custom_path
    return model_name


def _default_search_dirs(extra_dir: str = "") -> list[str]:
    """Build the default list of directories to search for custom models.

    Search order:
    1. Config-specified models_dir (if provided)
    2. ~/.config/heyvox/models/ (user-local models)
    3. {package}/training/models/ (legacy path)
    """
    dirs = []
    if extra_dir:
        dirs.append(extra_dir)

    # User-local models directory (use same CONFIG_DIR as config.py)
    from heyvox.config import CONFIG_DIR
    user_models = os.path.join(str(CONFIG_DIR), "models")
    dirs.append(user_models)

    # Package-bundled models (shipped in pip package)
    pkg_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dirs.append(os.path.join(pkg_dir, "models"))
    # Bundled openwakeword models (hey_jarvis_v0.1 + shared feature extractors)
    dirs.append(os.path.join(pkg_dir, "models", "oww"))

    # Package-relative legacy path
    dirs.append(os.path.join(pkg_dir, "training", "models"))

    return dirs


# openwakeword loads these shared extractors from its OWN resources/models dir
# on every Model() init, regardless of which wake word is requested.
_OWW_FEATURE_MODELS = ("melspectrogram.onnx", "embedding_model.onnx", "silero_vad.onnx")

# openwakeword's built-in wake words (fetchable by bare name). Used to decide
# whether a missing model should be downloaded vs. treated as a user-supplied
# custom model that lives under ~/.config/heyvox/models/.
_OWW_BUILTIN_NAMES = frozenset({
    "alexa_v0.1", "hey_jarvis_v0.1", "hey_mycroft_v0.1",
    "hey_rhasspy_v0.1", "timer_v0.1", "weather_v0.1",
})


def _ensure_oww_models(builtin_names: list[str]) -> None:
    """Make openwakeword loadable on a fresh pip install.

    openwakeword ships NO model files in its wheel — its ``resources/models``
    dir is empty until ``download_models()`` runs. But ``Model()`` loads its
    shared feature extractors (melspectrogram, embedding) + VAD from that dir
    on every init, plus any built-in wake word (e.g. ``hey_jarvis_v0.1``)
    requested by bare name. So a fresh ``heyvox start`` crashed with a missing
    -file error (DEF-159 fixed only the default *name*, not the missing file).

    We mirror the onnx models bundled in the heyvox wheel into openwakeword's
    dir (offline-first, ~5 MB). If a requested built-in isn't bundled, or the
    dir isn't writable, fall back to openwakeword's network downloader.
    """
    import shutil

    try:
        import openwakeword
    except ImportError:
        return

    oww_dir = os.path.join(os.path.dirname(openwakeword.__file__), "resources", "models")
    bundled = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "models", "oww"
    )

    # Feature/VAD models openwakeword always needs, plus any requested built-in
    # wake word we ship bundled (so name-based loads resolve fully offline).
    wanted = list(_OWW_FEATURE_MODELS)
    for name in builtin_names:
        if os.path.exists(os.path.join(bundled, f"{name}.onnx")):
            wanted.append(f"{name}.onnx")

    missing = [f for f in wanted if not os.path.exists(os.path.join(oww_dir, f))]
    if missing:
        try:
            os.makedirs(oww_dir, exist_ok=True)
            for f in missing:
                src = os.path.join(bundled, f)
                if os.path.exists(src):
                    shutil.copy2(src, os.path.join(oww_dir, f))
        except OSError:
            pass  # read-only dir — the downloader below also fails; Model() then errors clearly

    # Requested built-ins we don't bundle (or a still-empty dir) → network fetch.
    to_download = [
        n for n in builtin_names
        if n in _OWW_BUILTIN_NAMES
        and not os.path.exists(os.path.join(oww_dir, f"{n}.onnx"))
    ]
    features_missing = any(
        not os.path.exists(os.path.join(oww_dir, f)) for f in _OWW_FEATURE_MODELS
    )
    if to_download or features_missing:
        try:
            from openwakeword.utils import download_models
            download_models(to_download)
        except Exception:
            pass  # load_models() surfaces a clear error if models truly can't load


def load_models(
    start_word: str,
    stop_word: str,
    models_dir: str = "",
    also_load: list[str] | None = None,
) -> tuple[Any, bool]:
    """Load openwakeword models for start/stop wake words.

    Looks for custom .onnx files in multiple directories, then falls back to
    built-in openwakeword model names.

    Args:
        start_word: Model name for recording start trigger.
        stop_word: Model name for recording stop trigger.
        models_dir: Additional directory to search for custom .onnx model files.
        also_load: Additional model names to load alongside start/stop.
            Any of these models can also trigger start/stop. Useful as
            fallback wake words (e.g. hey_jarvis alongside hey_vox).

    Returns:
        Tuple of (Model instance, use_separate_words flag).
        use_separate_words is True when start_word != stop_word.
    """
    from openwakeword.model import Model

    use_separate_words = start_word != stop_word
    models_to_load = list({start_word, stop_word})
    if also_load:
        for m in also_load:
            if m not in models_to_load:
                models_to_load.append(m)
    search_dirs = _default_search_dirs(models_dir)

    model_paths = []
    for m in models_to_load:
        resolved = _find_model_file(m, search_dirs)
        model_paths.append(resolved)

    # openwakeword ships no model files in its wheel, and tflite-runtime has no
    # wheel on Python 3.12+ — so always use onnx, and first make sure the shared
    # feature models (and any requested built-in wake word) are present on disk.
    name_only = [m for m, p in zip(models_to_load, model_paths) if not p.endswith(".onnx")]
    _ensure_oww_models(name_only)
    model = Model(
        wakeword_models=model_paths,
        inference_framework="onnx",
    )
    return model, use_separate_words
