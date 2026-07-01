"""Zip-bundle builder for "Report Issue…".

Bundles logs, redacted config, system info, mic diagnostics, defect counters,
and the user's comment into a single ``~/Downloads/heyvox-report-<ts>.zip``.

The dialog (``heyvox.reporting.dialog``) and CLI (``heyvox bugreport --bundle``)
both call :func:`build_bundle`.

Privacy: all log/config text is passed through ``redact_text`` before being
written to the zip. The user is shown a preview path so they can inspect
the zip before submitting.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import zipfile
from dataclasses import dataclass
from pathlib import Path

from heyvox.reporting.redact import redact_config, redact_text
from heyvox.reporting.text_report import (
    _counter_summary,
    _last_log_lines,
    collect_system_info,
)


@dataclass
class BundleOptions:
    """Per-include toggles for the report bundle.

    Defaults match the dialog defaults: everything on except transcripts.
    """
    comment: str = ""
    include_logs: bool = True
    include_config: bool = True
    include_system_info: bool = True
    include_mic_diag: bool = True
    include_counters: bool = True
    include_transcripts: bool = False  # opt-in, private content


# Default candidate logs — only files that exist on the system make it in.
def _candidate_logs() -> list[Path]:
    try:
        from heyvox.config import load_config
        main_log = Path(load_config().log_file)
    except Exception:
        from heyvox.constants import LOG_FILE_DEFAULT
        main_log = Path(LOG_FILE_DEFAULT)

    from heyvox.constants import (
        HERALD_DEBUG_LOG,
        HERALD_VIOLATIONS_LOG,
        HUSH_LOG,
        STT_DEBUG_LOG,
        HEYVOX_RESTART_LOG,
        HUD_STDERR_LOG,
    )
    return [
        main_log,
        Path(HERALD_DEBUG_LOG),
        Path(HERALD_VIOLATIONS_LOG),
        Path(HUSH_LOG),
        Path(STT_DEBUG_LOG),
        Path(HEYVOX_RESTART_LOG),
        Path(HUD_STDERR_LOG),
    ]


def _mic_diagnostics() -> dict:
    """Snapshot of mic + output device state at report time."""
    diag: dict = {}

    # Active mic file (written by main.py)
    try:
        from heyvox.constants import ACTIVE_MIC_FILE
        diag["active_mic"] = Path(ACTIVE_MIC_FILE).read_text().strip()
    except Exception:
        diag["active_mic"] = None

    # Cached calibration profiles, if any
    try:
        from pathlib import Path as _P
        try:
            from platformdirs import user_cache_dir
            cache = _P(user_cache_dir("heyvox"))
        except ImportError:
            cache = _P.home() / ".cache" / "heyvox"
        prof_file = cache / "mic-profiles.json"
        if prof_file.exists():
            diag["mic_profiles"] = json.loads(prof_file.read_text())
    except Exception as exc:
        diag["mic_profiles_error"] = str(exc)

    # Current output device, best-effort via CoreAudio helper
    try:
        from heyvox.audio.output import list_output_devices
        devices = list_output_devices()
        diag["output_devices"] = [
            {"name": d.name, "is_default": d.is_default}
            for d in devices
        ]
    except Exception as exc:
        diag["output_devices_error"] = str(exc)

    return diag


def _config_text_redacted() -> str | None:
    """Read user's config.yaml and run it through ``redact_config``."""
    try:
        from heyvox.config import CONFIG_FILE
        if not Path(CONFIG_FILE).exists():
            return None
        return redact_config(Path(CONFIG_FILE).read_text())
    except Exception as exc:
        return f"# Could not read config: {exc}\n"


def _recent_transcripts(n: int = 20) -> list[dict]:
    """Last ``n`` transcript history entries (opt-in only)."""
    try:
        from heyvox.history import load as _load_history
        return list(_load_history(n))
    except Exception:
        return []


def _bundle_filename() -> str:
    ts = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"heyvox-report-{ts}.zip"


def build_bundle(opts: BundleOptions, dest_dir: Path | None = None) -> Path:
    """Build the report zip and return its path.

    ``dest_dir`` defaults to ``~/Downloads`` (falls back to ``$TMPDIR`` if
    Downloads isn't writable).
    """
    if dest_dir is None:
        downloads = Path.home() / "Downloads"
        if downloads.is_dir() and os.access(downloads, os.W_OK):
            dest_dir = downloads
        else:
            import tempfile as _t
            dest_dir = Path(_t.gettempdir())

    out_path = dest_dir / _bundle_filename()

    with zipfile.ZipFile(out_path, "w", compression=zipfile.ZIP_DEFLATED) as z:
        # 1. User comment (always present so the issue body has context).
        comment_md = (
            "# Report from HeyVox menu\n\n"
            + (opts.comment.strip() or "_(no comment provided)_\n")
        )
        z.writestr("USER_COMMENT.md", redact_text(comment_md))

        # 2. System info
        if opts.include_system_info:
            sys_info = collect_system_info()
            z.writestr("system_info.json", json.dumps(sys_info, indent=2))

        # 3. Mic diagnostics
        if opts.include_mic_diag:
            diag = _mic_diagnostics()
            z.writestr(
                "mic_diagnostics.json",
                redact_text(json.dumps(diag, indent=2, default=str)),
            )

        # 4. Defect counters
        if opts.include_counters:
            counters = _counter_summary()
            z.writestr("defect_counters.json", json.dumps(counters, indent=2))

        # 5. Logs — copy each that exists, redacted.
        if opts.include_logs:
            for log_path in _candidate_logs():
                if not log_path.exists():
                    continue
                try:
                    text = log_path.read_text(errors="replace")
                except Exception as exc:
                    text = f"<read failed: {exc}>\n"
                z.writestr(f"logs/{log_path.name}", redact_text(text))

        # 6. Redacted config
        if opts.include_config:
            cfg = _config_text_redacted()
            if cfg is not None:
                z.writestr("config.yaml", cfg)

        # 7. Recent transcripts (opt-in only)
        if opts.include_transcripts:
            entries = _recent_transcripts(20)
            z.writestr(
                "recent_transcripts.json",
                redact_text(json.dumps(entries, indent=2, default=str)),
            )

        # 8. Tail of main log as quick preview for the issue body.
        tail = _last_log_lines(60)
        if tail:
            z.writestr("logs/heyvox.log.tail", redact_text("".join(tail)))

    return out_path


def summarize_bundle(zip_path: Path) -> str:
    """Return a short, human-readable summary of a bundle for previews."""
    if not zip_path.exists():
        return f"(bundle missing: {zip_path})"
    try:
        with zipfile.ZipFile(zip_path) as z:
            names = z.namelist()
        size_kb = zip_path.stat().st_size // 1024
        return f"{zip_path.name} ({size_kb} KB, {len(names)} files)"
    except Exception as exc:
        return f"(bundle unreadable: {exc})"
