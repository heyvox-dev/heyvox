"""Path and identifier redaction for bug reports.

Strips identifying paths so reports can be shared without leaking the user's
home directory layout or workspace names. Mic device names and config values
are intentionally NOT redacted — they are too important for debugging hardware
issues. The dialog gives the user a preview so they see exactly what leaves
the machine.
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path


_HOME = str(Path.home())
_TMP = tempfile.gettempdir()
_USER = os.environ.get("USER") or os.environ.get("LOGNAME") or ""


def redact_text(text: str) -> str:
    """Replace identifying paths in arbitrary text.

    Substitutions, in order:
    * Absolute home dir (``/Users/work``) → ``~``
    * Resolved /var/folders TMPDIR (gibberish per-user) → ``$TMPDIR``
    * Plain username matches → ``$USER`` (only if username is non-trivial)
    """
    if not text:
        return text

    # /var/folders/<a>/<b>/T/... → $TMPDIR/...
    text = re.sub(r"/var/folders/[^/\s]+/[^/\s]+/T(/|\b)", "$TMPDIR\\1", text)

    # /Users/<name>/... → ~/...
    text = re.sub(r"/Users/[^/\s]+", "~", text)

    # Substitute the resolved $TMPDIR explicitly (covers any value)
    if _TMP and _TMP != "/tmp":
        text = text.replace(_TMP, "$TMPDIR")

    # Replace bare username when ≥ 4 chars (avoid stripping short generic strings)
    if _USER and len(_USER) >= 4:
        text = re.sub(rf"\b{re.escape(_USER)}\b", "$USER", text)

    return text


def redact_path(path: str | Path) -> str:
    """Single-path version of :func:`redact_text`."""
    return redact_text(str(path))


def redact_config(config_text: str) -> str:
    """Redact a config.yaml file's contents.

    Same path substitutions as ``redact_text``. Config has no API keys today;
    this is forward-looking guard if future fields hold tokens.
    """
    redacted = redact_text(config_text)

    # Future-proof: blank out any line that looks like an API key / token.
    redacted = re.sub(
        r"^(\s*\w*(?:api_key|token|secret|password)\w*\s*:\s*).+$",
        r"\1<redacted>",
        redacted,
        flags=re.IGNORECASE | re.MULTILINE,
    )
    return redacted
