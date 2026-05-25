"""HUD banner surface — unified primitive for silent-state-change detectors.

Closes DEFECT-LOG patterns:
- P-new (ux invisibility): silent state changes need a visible signal
- P-detector-without-action: a detector that fires with no user-visible
  consequence ranks as "no detector" from the user's perspective.

Replaces the ad-hoc DEF-101 MIC_WARN_FILE write+read+expire dance scattered
across recording.py, device_manager.py, and overlay.py with a single API.

Storage: one JSON file at ``constants.HUD_BANNERS_FILE`` (defaults to a path
under ``$TMPDIR``). Each banner is a record:

    {"source": "mic-zombie", "level": "warn", "text": "…", "ts": 1234.5, "ttl": 60}

Latest write per ``source`` wins (dedup); ``ts + ttl < now`` is filtered on
read. Writes are atomic (temp+rename); concurrent writers race-but-don't-
corrupt because each write is a single rename on the same filesystem.

Backwards-compat: ``read_active()`` also reads the legacy ``MIC_WARN_FILE``
single-line text + mtime and synthesises a ``legacy-mic-warn`` record so
existing direct writers keep working through one release.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from typing import Iterable

LEVELS = ("info", "warn", "error")
_LEVEL_RANK = {lvl: i for i, lvl in enumerate(LEVELS)}


def _path() -> str:
    """Re-resolve the on-disk banner file path on every call.

    Imported function-locally so monkeypatching ``heyvox.constants.HUD_BANNERS_FILE``
    in tests takes effect without restarting the process.
    """
    from heyvox.constants import HUD_BANNERS_FILE
    return HUD_BANNERS_FILE


def _legacy_mic_warn() -> dict | None:
    """Synthesise a banner record from the legacy DEF-101 MIC_WARN_FILE if
    present and within TTL. Returns ``None`` if no legacy file or stale.
    """
    try:
        from heyvox.constants import MIC_WARN_FILE, MIC_WARN_TTL_SECS
    except ImportError:
        return None
    try:
        st = os.stat(MIC_WARN_FILE)
    except OSError:
        return None
    age = time.time() - st.st_mtime
    if age >= MIC_WARN_TTL_SECS:
        return None
    try:
        with open(MIC_WARN_FILE) as f:
            text = f.read().strip()
    except OSError:
        return None
    if not text:
        return None
    return {
        "source": "legacy-mic-warn",
        "level": "warn",
        "text": text[:120],
        "ts": st.st_mtime,
        "ttl": float(MIC_WARN_TTL_SECS - age),
    }


def _load_all() -> list[dict]:
    try:
        with open(_path()) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for rec in data:
        if not isinstance(rec, dict):
            continue
        if not all(k in rec for k in ("source", "level", "text", "ts", "ttl")):
            continue
        out.append(rec)
    return out


def _atomic_write(records: Iterable[dict]) -> None:
    path = _path()
    dirpath = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(prefix=".hud-banners-", dir=dirpath)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(list(records), f)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _filter_live(records: Iterable[dict], now: float | None = None) -> list[dict]:
    if now is None:
        now = time.time()
    return [r for r in records if r["ts"] + r["ttl"] > now]


class HUDSurface:
    """Static-method facade for the banner store.

    Detectors call ``HUDSurface.banner(...)`` to surface a degraded-state
    signal. The HUD overlay calls ``HUDSurface.read_active()`` on every
    menu-bar refresh and picks the highest-level record to render.
    """

    @staticmethod
    def banner(level: str, source: str, text: str, ttl_secs: float = 60.0) -> None:
        """Emit (or refresh) a banner from ``source``.

        Latest write per source wins — calling twice with the same source
        overwrites the previous record.

        OSError-tolerant: callers do not need to wrap; banner writes must
        never break the calling pipeline.
        """
        if level not in _LEVEL_RANK:
            level = "info"
        if not isinstance(source, str) or not source:
            return
        text = (text or "")[:160]
        try:
            ttl = float(ttl_secs)
        except (TypeError, ValueError):
            ttl = 60.0
        if ttl <= 0:
            return
        try:
            now = time.time()
            existing = _load_all()
            kept = [r for r in existing if r.get("source") != source]
            kept.append({
                "source": source,
                "level": level,
                "text": text,
                "ts": now,
                "ttl": ttl,
            })
            # Trim expired records opportunistically to keep the file small.
            kept = _filter_live(kept, now=now)
            _atomic_write(kept)
        except OSError:
            pass

    @staticmethod
    def clear(source: str) -> None:
        """Remove the banner for ``source`` if present."""
        if not isinstance(source, str) or not source:
            return
        try:
            existing = _load_all()
            kept = [r for r in existing if r.get("source") != source]
            if len(kept) == len(existing):
                return
            _atomic_write(kept)
        except OSError:
            pass

    @staticmethod
    def read_active(include_legacy: bool = True) -> list[dict]:
        """Return live (non-expired) banner records, newest-first per source.

        ``include_legacy=True`` (default) merges the DEF-101 MIC_WARN_FILE
        compat record. The native record wins when both name ``mic-zombie``
        et al. — legacy is a fallback, not a replacement.
        """
        now = time.time()
        live = _filter_live(_load_all(), now=now)
        if include_legacy:
            legacy = _legacy_mic_warn()
            if legacy is not None:
                known_sources = {r["source"] for r in live}
                if legacy["source"] not in known_sources:
                    live.append(legacy)
        return live

    @staticmethod
    def top_active() -> dict | None:
        """Return the highest-level live banner, or ``None`` if no banner.

        Tie-break: highest level wins; among equal levels, most-recent ``ts``
        wins. This is what the overlay renders on the menu bar.
        """
        live = HUDSurface.read_active()
        if not live:
            return None
        return max(live, key=lambda r: (_LEVEL_RANK.get(r["level"], 0), r["ts"]))
