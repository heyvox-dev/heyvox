"""DeviceHandle — hotplug-safe wrappers around kernel-assigned device IDs.

Closes DEFECT-LOG pattern **P-hotplug-cache**: any subsystem that caches a
kernel-assigned identifier (CoreAudio ``dev_id``, PortAudio device index,
USB endpoint, network interface index) must either subscribe to hotplug
notifications or revalidate the cached ID on every use. macOS reassigns
CoreAudio device IDs on every connect/disconnect — the same physical headset
appears as 973, then 123, then 1020 over a single session.

Two backends, one ``revalidate() -> bool`` contract:

- ``CoreAudioHandle(dev_id)`` — checks ``AudioObjectHasProperty`` against a
  device-scope property. Returns False for ghost IDs. Used by the Herald
  orchestrator before every ``_set_volume_coreaudio`` to fail fast instead
  of relying on the post-write fallback (still in place from DEF-113).

- ``PortAudioHandle(pa, idx, expected_name)`` — re-enumerates
  ``pa.get_device_count()`` and asserts the device at ``idx`` still has the
  expected name (or finds it under a different index and updates). Catches
  the PortAudio HAL cache stale state (DEF-104) where ``pa.terminate()`` +
  ``pyaudio.PyAudio()`` does not invalidate the underlying device list.

Both handles are designed for "use once per Set*, throw away" rather than
long-lived. They're cheap (microseconds for CoreAudio, ~tens of µs for a
device-count enumeration) and stateless beyond what was passed in at
construction time.

Naming convention: the public attribute ``.id`` always returns the
*currently-believed* device identifier, which may have been updated by a
prior ``revalidate()`` call. ``.dropped`` flips to True once a revalidation
permanently fails (no live device under the expected name); after that,
callers should fall back to a system-default code path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


# ---------------------------------------------------------------------------
# CoreAudio
# ---------------------------------------------------------------------------


@dataclass
class CoreAudioHandle:
    """Hotplug-safe wrapper around a CoreAudio AudioObjectID.

    Usage::

        h = CoreAudioHandle(dev_id=sidecar_dev_id)
        if h.revalidate():
            _set_volume_coreaudio(h.id, vol)
        else:
            set_system_volume_cached(vol)  # device gone — fall back

    The handle holds the *currently believed* ID in ``.id``; after a
    successful ``revalidate()`` this is still the same ID (CoreAudio device
    IDs are reassigned by hotplug, not changed in-place — a ghost is a ghost
    forever; the live device under the same physical port has a fresh ID).

    ``.dropped`` flips to True after a failed revalidate so callers don't
    retry the dead ID in a loop.
    """

    dev_id: int
    dropped: bool = field(default=False, init=False)

    @property
    def id(self) -> int:
        return int(self.dev_id)

    def revalidate(self) -> bool:
        """Return True if the underlying CoreAudio device is still alive.

        Sets ``.dropped`` on permanent failure so subsequent calls short-
        circuit. Exceptions raised by the ctypes layer are treated as
        "device gone" — the handle's whole reason to exist is to keep
        callers stable across CoreAudio failures.
        """
        if self.dropped:
            return False
        try:
            from heyvox.herald.coreaudio import _is_coreaudio_device_alive
        except ImportError:
            return False
        try:
            alive = _is_coreaudio_device_alive(self.dev_id)
        except Exception:
            alive = False
        if not alive:
            self.dropped = True
        return alive


# ---------------------------------------------------------------------------
# PortAudio
# ---------------------------------------------------------------------------


@dataclass
class PortAudioHandle:
    """Hotplug-safe wrapper around a PortAudio device index.

    Usage::

        h = PortAudioHandle(pa=self.pa, idx=self.dev_index,
                            expected_name=self.dev_name)
        if h.revalidate():
            stream = pa.open(input_device_index=h.idx, ...)
        else:
            # Re-scan via find_best_mic() — expected device is gone or
            # has shifted to an index we can't find by name.
            ...

    The wrapper does NOT touch ``pa.terminate()`` or recreate the PyAudio
    instance — that's the caller's job. It only verifies the *cached
    mapping* of index → name within the *current* PyAudio instance is
    still consistent.

    On a hotplug-driven index shuffle, ``revalidate()`` updates ``.idx`` to
    the new index where the expected name now lives and returns True. The
    caller can then re-open the stream against ``h.idx`` and refresh any
    bookkeeping that depends on the index.
    """

    pa: Any  # avoid pyaudio import at module load
    idx: int
    expected_name: str
    dropped: bool = field(default=False, init=False)

    def revalidate(self) -> bool:
        """Return True if a device with ``expected_name`` is reachable.

        Behaviour:
        1. Query ``pa.get_device_info_by_index(self.idx)``. If it succeeds
           AND the name matches, return True with no side effects.
        2. Otherwise scan all current devices. If a device with the expected
           name is found at a *different* index, update ``self.idx`` and
           return True (drift recovery).
        3. If no device with the expected name is present, set
           ``self.dropped`` and return False (caller should re-scan via
           ``find_best_mic`` or fall back to the system default).

        OSError / KeyError / IndexError from the PyAudio layer all map to
        "device gone" — defensively, because the failure modes vary across
        PortAudio host APIs.
        """
        if self.dropped:
            return False
        if not self.expected_name:
            # Without a name we can't disambiguate — assume alive (no-op).
            return True

        # Fast path: index still maps to the expected device.
        try:
            info = self.pa.get_device_info_by_index(self.idx)
            name = (info or {}).get("name", "")
            if name == self.expected_name:
                return True
        except (OSError, KeyError, IndexError, AttributeError):
            pass  # fall through to drift scan

        # Drift scan: search by name across the current device list.
        try:
            count = int(self.pa.get_device_count())
        except (OSError, AttributeError):
            self.dropped = True
            return False

        for i in range(count):
            try:
                info = self.pa.get_device_info_by_index(i)
            except (OSError, KeyError, IndexError):
                continue
            if (info or {}).get("name", "") == self.expected_name:
                self.idx = int(i)
                return True

        self.dropped = True
        return False


# ---------------------------------------------------------------------------
# DEF-104 detection — device live in CoreAudio but absent from PortAudio's cache
# ---------------------------------------------------------------------------


def _matches(prio_name: str, names: set[str]) -> bool:
    """True if ``prio_name`` is a case-insensitive substring of any name.

    Mirrors ``find_best_mic``'s matching (``prio_name.lower() in dev_name``),
    so detection agrees with selection. ``names`` is expected lowercased.
    """
    p = prio_name.lower()
    return any(p in n for n in names)


def detect_missed_hotplug(
    live_input_names: set[str],
    pa_input_names: set[str],
    mic_priority: list[str] | None,
    current_dev_name: Optional[str],
    default_input_name: Optional[str] = None,
) -> Optional[str]:
    """Return a ``mic_priority`` entry that is the DEF-104 signature, else None.

    The signature: a configured-priority device that the **live CoreAudio HAL**
    reports (``live_input_names``) but PortAudio's cached enumeration does NOT
    (``pa_input_names``), *and* that outranks whatever mic is in use right now.
    Such a device was hotplugged after the daemon's first PortAudio init and is
    invisible to every PortAudio code path until the process restarts.

    Pure function — both name sets are lowercased CoreAudio/PortAudio device
    names; matching is the same substring rule as ``find_best_mic``. No I/O, so
    it's unit-testable without audio hardware.

    Args:
        live_input_names: lowercase names of live input devices (CoreAudio).
        pa_input_names: lowercase names PortAudio currently enumerates (in_ch>0).
        mic_priority: configured priority list (substrings, highest first).
        current_dev_name: the mic the daemon is using now (any case), or None.
        default_input_name: the macOS CoreAudio default input device name (any
            case), or None. DEF-104 fallback candidate — when no priority device
            matches, an actively-used default input showing the same
            live-but-uncached signature is flagged too (macOS makes a freshly
            hotplugged USB headset the default input even when it isn't in
            mic_priority). BT exclusion + restart-loop guard are the caller's.

    Returns:
        The highest-ranked priority entry matching the signature, or None. When
        ``live_input_names`` is empty (CoreAudio unavailable) this returns None
        rather than false-firing — detection degrades to a no-op.
    """
    if not live_input_names:
        return None  # CoreAudio unavailable — never false-fire a restart.

    if mic_priority:
        # Rank of the device in use now. Unmatched / None → worst rank, so any
        # live-but-uncached priority device counts as an upgrade.
        cur = (current_dev_name or "").lower()
        current_rank = len(mic_priority)
        for rank, prio_name in enumerate(mic_priority):
            if cur and prio_name.lower() in cur:
                current_rank = rank
                break

        for rank, prio_name in enumerate(mic_priority):
            if rank >= current_rank:
                # Not an upgrade over the current mic — stop (list is ordered).
                break
            if _matches(prio_name, live_input_names) and not _matches(
                prio_name, pa_input_names
            ):
                return prio_name

    # DEF-104 fallback (2026-07-05): the priority scan above only heals listed
    # devices. macOS makes a freshly-hotplugged USB headset the *default input*,
    # which carries the same signature (live in CoreAudio, absent from
    # PortAudio's cache) without being in mic_priority. Treat it as a candidate
    # so an actively-used mic self-heals too — unless it's already the mic we're
    # on. (BT exclusion + restart-loop guard are applied by the caller.)
    if default_input_name:
        d = default_input_name.lower()
        cur = (current_dev_name or "").lower()
        already_current = bool(cur) and (d in cur or cur in d)
        if (
            not already_current
            and _matches(default_input_name, live_input_names)
            and not _matches(default_input_name, pa_input_names)
        ):
            return default_input_name
    return None


__all__ = ["CoreAudioHandle", "PortAudioHandle", "detect_missed_hotplug"]
