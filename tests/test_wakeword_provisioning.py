"""Guard tests for wake-word model provisioning (fresh-install crash class).

DEF-159 fixed the default wake-word *name* (hey_jarvis_v0.1). These guard the
actual model *files* being shippable + loadable: openwakeword ships NO model
files in its wheel, so a fresh `heyvox start` crashed until the models were
bundled and mirrored into openwakeword's dir. See
``heyvox/audio/wakeword.py::_ensure_oww_models``.

Net-free: no network, no real openwakeword init — kept out of test_defect_guards
deliberately (that file has concurrent unrelated work).
"""
import os
import sys
import types

import heyvox
from heyvox.audio import wakeword


def test_bundled_oww_models_present_in_package():
    """The onnx models must physically ship in the wheel (package-data:
    models/oww/*.onnx), else a fresh pip install has nothing to mirror."""
    oww = os.path.join(os.path.dirname(heyvox.__file__), "models", "oww")
    required = ["hey_jarvis_v0.1.onnx", *wakeword._OWW_FEATURE_MODELS]
    missing = [f for f in required if not os.path.exists(os.path.join(oww, f))]
    assert not missing, f"bundled openwakeword models missing from package: {missing}"


def test_default_wake_word_is_bundled_or_builtin():
    """The shipped default must resolve to a bundled .onnx or a known
    openwakeword built-in — otherwise a fresh install crashes."""
    from heyvox.config import WakeWordConfig

    oww = os.path.join(os.path.dirname(heyvox.__file__), "models", "oww")
    cfg = WakeWordConfig()
    for name in [cfg.start, cfg.stop, *cfg.also_load]:
        if not name:
            continue
        bundled = os.path.exists(os.path.join(oww, f"{name}.onnx"))
        builtin = name in wakeword._OWW_BUILTIN_NAMES
        assert bundled or builtin, (
            f"default wake word {name!r} is neither bundled in models/oww/ nor a "
            f"known openwakeword built-in — fresh install would crash"
        )


def test_ensure_oww_models_mirrors_into_empty_dir(tmp_path, monkeypatch):
    """_ensure_oww_models must populate an empty openwakeword resources/models
    dir from the bundled set — the exact fresh-pip-install condition."""
    fake_pkg = tmp_path / "oww_pkg"
    (fake_pkg / "resources" / "models").mkdir(parents=True)
    fake_mod = types.ModuleType("openwakeword")
    fake_mod.__file__ = str(fake_pkg / "__init__.py")
    monkeypatch.setitem(sys.modules, "openwakeword", fake_mod)

    wakeword._ensure_oww_models(["hey_jarvis_v0.1"])

    dst = fake_pkg / "resources" / "models"
    for f in wakeword._OWW_FEATURE_MODELS:
        assert (dst / f).exists(), f"feature model {f} not mirrored into {dst}"
    assert (dst / "hey_jarvis_v0.1.onnx").exists(), "bundled wake word not mirrored"
