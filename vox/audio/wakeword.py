"""
Wake word model management for vox.

Thin wrapper around openwakeword Model loading.
"""

import os
from typing import Any


def load_models(start_word: str, stop_word: str, models_dir: str) -> tuple[Any, bool]:
    """Load openwakeword models for start/stop wake words.

    Looks for custom .onnx files in models_dir first, then falls back to
    built-in openwakeword model names.

    Args:
        start_word: Model name for recording start trigger.
        stop_word: Model name for recording stop trigger.
        models_dir: Directory to search for custom .onnx model files.

    Returns:
        Tuple of (Model instance, use_separate_words flag).
        use_separate_words is True when start_word != stop_word.
    """
    from openwakeword.model import Model

    use_separate_words = start_word != stop_word
    models_to_load = list(set([start_word, stop_word]))
    model_paths = []

    for m in models_to_load:
        custom_path = os.path.join(models_dir, f"{m}.onnx")
        if os.path.exists(custom_path):
            model_paths.append(custom_path)
        else:
            model_paths.append(m)

    model = Model(wakeword_models=model_paths)
    return model, use_separate_words
