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

    # Package-relative legacy path
    dirs.append(os.path.join(pkg_dir, "training", "models"))

    return dirs


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

    # Use onnx framework if any custom .onnx paths are provided
    has_onnx = any(p.endswith(".onnx") for p in model_paths)
    model = Model(
        wakeword_models=model_paths,
        inference_framework="onnx" if has_onnx else "tflite",
    )
    return model, use_separate_words
