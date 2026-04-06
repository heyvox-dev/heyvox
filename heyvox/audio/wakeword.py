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

    # User-local models directory
    from platformdirs import user_config_dir
    user_models = os.path.join(user_config_dir("heyvox"), "models")
    dirs.append(user_models)

    # Package-relative legacy path
    pkg_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    dirs.append(os.path.join(pkg_dir, "training", "models"))

    return dirs


def load_models(start_word: str, stop_word: str, models_dir: str = "") -> tuple[Any, bool]:
    """Load openwakeword models for start/stop wake words.

    Looks for custom .onnx files in multiple directories, then falls back to
    built-in openwakeword model names.

    Args:
        start_word: Model name for recording start trigger.
        stop_word: Model name for recording stop trigger.
        models_dir: Additional directory to search for custom .onnx model files.

    Returns:
        Tuple of (Model instance, use_separate_words flag).
        use_separate_words is True when start_word != stop_word.
    """
    from openwakeword.model import Model

    use_separate_words = start_word != stop_word
    models_to_load = list({start_word, stop_word})
    search_dirs = _default_search_dirs(models_dir)

    model_paths = []
    for m in models_to_load:
        resolved = _find_model_file(m, search_dirs)
        model_paths.append(resolved)

    model = Model(wakeword_models=model_paths)
    return model, use_separate_words
