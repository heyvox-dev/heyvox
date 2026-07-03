"""System diagnostics for heyvox — `heyvox doctor`.

Answers "why isn't it working" (complementary to `heyvox status`, which answers
"is it running"): platform/STT-engine fit, macOS permissions, wake-word model
presence + loadability, key optional deps, and config location. Every check is
independently guarded so a single failure degrades to a warning line rather
than crashing the command (the previous behaviour: the module didn't exist and
`heyvox doctor` raised ModuleNotFoundError).

Returns a plain-text report; `heyvox.cli._cmd_doctor` prints it.
"""
import os
import platform
import sys

_OK = "✓"
_FAIL = "✗"
_WARN = "!"


def _line(mark: str, label: str, detail: str = "") -> str:
    return f"  {mark} {label}" + (f" — {detail}" if detail else "")


def _check_platform(lines: list[str]) -> bool:
    lines.append("[Platform]")
    machine = platform.machine()
    is_arm = machine == "arm64"
    lines.append(_line(
        _OK if is_arm else _WARN, f"CPU: {machine}",
        "" if is_arm else "Intel — MLX Whisper is unavailable; use the sherpa STT engine",
    ))
    lines.append(_line(_OK, f"macOS: {platform.mac_ver()[0] or '?'}"))
    py_ok = sys.version_info >= (3, 12)
    lines.append(_line(
        _OK if py_ok else _FAIL, f"Python: {platform.python_version()}",
        "" if py_ok else "3.12+ required",
    ))
    lines.append("")
    return is_arm


def _check_permissions(lines: list[str]) -> None:
    lines.append("[Permissions]")
    try:
        from heyvox.setup import permissions as perm
    except Exception as e:  # pragma: no cover - import guard
        lines.append(_line(_WARN, "permission checks unavailable", str(e)))
        lines.append("")
        return
    for name, fn in (
        ("Microphone", perm.check_microphone),
        ("Accessibility", perm.check_accessibility),
        ("Screen Recording", perm.check_screen_recording),
    ):
        try:
            ok = fn()
        except Exception as e:
            lines.append(_line(_WARN, name, f"check failed: {e}"))
            continue
        lines.append(_line(_OK if ok else _FAIL, name,
                           "" if ok else "not granted — run `heyvox setup`"))
    lines.append("")


def _load_config():
    try:
        from heyvox.config import load_config
        return load_config()
    except Exception:
        return None


def _check_config(lines: list[str]) -> None:
    lines.append("[Config]")
    try:
        from heyvox.config import CONFIG_DIR
        cfg_path = os.path.join(str(CONFIG_DIR), "config.yaml")
        if os.path.exists(cfg_path):
            lines.append(_line(_OK, "config.yaml", cfg_path))
        else:
            lines.append(_line(_WARN, "config.yaml",
                               f"not found at {cfg_path} — run `heyvox setup`"))
    except Exception as e:
        lines.append(_line(_WARN, "config location unavailable", str(e)))
    lines.append("")


def _check_wakeword(lines: list[str], cfg) -> None:
    lines.append("[Wake word]")
    try:
        from heyvox.audio import wakeword
    except Exception as e:
        lines.append(_line(_WARN, "wake-word module unavailable", str(e)))
        lines.append("")
        return
    ww = getattr(cfg, "wake_words", None)
    start = getattr(ww, "start", "hey_jarvis_v0.1") or "hey_jarvis_v0.1"
    stop = getattr(ww, "stop", start) or start
    pkg = os.path.dirname(os.path.dirname(os.path.abspath(wakeword.__file__)))
    bundled = os.path.join(pkg, "models", "oww", "hey_jarvis_v0.1.onnx")
    if os.path.exists(bundled):
        lines.append(_line(_OK, "bundled models present", "heyvox/models/oww/"))
    else:
        lines.append(_line(_FAIL, "bundled models MISSING",
                           "wheel built without models — reinstall heyvox"))
    try:
        wakeword.load_models(start, stop)
        lines.append(_line(_OK, f"model loads ({start})"))
    except Exception as e:
        lines.append(_line(_FAIL, f"model load failed ({start})", str(e)[:80]))
    lines.append("")


def _check_deps(lines: list[str], cfg, is_arm: bool) -> None:
    lines.append("[STT / TTS deps]")
    stt_local = getattr(getattr(cfg, "stt", None), "local", None)
    engine = getattr(stt_local, "engine", "mlx") if stt_local else "mlx"
    lines.append(_line(_OK, f"STT engine: {engine}"))
    if engine == "mlx":
        try:
            import mlx_whisper  # noqa: F401
            lines.append(_line(_OK, "mlx-whisper installed"))
        except ImportError:
            mark = _FAIL if is_arm else _WARN
            hint = ("pip install 'heyvox[apple-silicon]'" if is_arm
                    else "Intel Mac — set stt.local.engine to sherpa")
            lines.append(_line(mark, "mlx-whisper NOT installed", hint))
    try:
        import mlx_audio  # noqa: F401
        lines.append(_line(_OK, "mlx-audio installed (TTS)"))
    except ImportError:
        lines.append(_line(_WARN, "mlx-audio not installed (TTS)",
                           "pip install 'heyvox[tts]'"))
    lines.append("")


def run_doctor() -> str:
    """Build and return the diagnostics report."""
    lines: list[str] = ["heyvox doctor — system diagnostics", ""]
    try:
        is_arm = _check_platform(lines)
        _check_permissions(lines)
        _check_config(lines)
        cfg = _load_config()
        _check_wakeword(lines, cfg)
        _check_deps(lines, cfg, is_arm)
        lines.append("For running-process status, run: heyvox status")
    except Exception as e:  # pragma: no cover - last-resort guard
        lines.append(f"  {_FAIL} doctor encountered an unexpected error: {e}")
    return "\n".join(lines)
