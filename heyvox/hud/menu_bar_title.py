"""Pure functions for menu-bar title + tooltip formatting.

No PyObjC imports — kept testable in isolation. Consumed by
heyvox/hud/overlay.py at three points:
  1. _apply_state idle branch (truncate_mic + format_menu_bar_title)
  2. status_item creation (initial tooltip)
  3. mic-switcher submenu rebuild (vi_suffix_for_device)

Phase 14 D-12 / D-13 / SPEC R4 / SPEC R5.
"""

from __future__ import annotations


def truncate_mic(name: str, max_len: int = 10) -> str:
    """Truncate a friendly mic name for menu-bar display.

    Word-boundary preferred: 'AirPods Pro' -> 'AirPods'. Heuristic shows
    the first word when it fits — even if subsequent words could also fit —
    because the first word is the most identifying chunk (per Q4
    recommendation in 14-RESEARCH.md). Falls back to a hard cutoff with
    ellipsis ONLY when even the first word exceeds max_len.
    """
    if not name:
        return "None"
    if len(name) <= max_len:
        return name
    first_word = name.split()[0]
    if len(first_word) <= max_len:
        return first_word
    return name[: max_len - 1] + "…"


def format_menu_bar_title(
    *,
    state: str,
    friendly_mic: str,
    held_count: int = 0,
    is_mic_muted: bool = False,
    mic_warning: str = "",
    crashed: list[str] | None = None,
    speaker_muted: bool = False,
) -> dict:
    """Compose menu-bar title text + tooltip + image-mode flags from state.

    Returns dict with keys: title, tooltip, use_brand_icon, mute_icon.

    State priority (highest -> lowest):
      mic_warning  ->  crashed  ->  active states (listening/processing/speaking)
        ->  is_mic_muted  ->  idle (mic name visible).
    """
    crashed = crashed or []
    tooltip = f"Mic: {friendly_mic or 'None'}"

    if mic_warning:
        return {
            "title": f"⚠️ {mic_warning}",
            "tooltip": tooltip,
            "use_brand_icon": False,
            "mute_icon": False,
        }

    if crashed:
        return {
            "title": f"⚠️ {'+'.join(crashed)} crashed",
            "tooltip": tooltip,
            "use_brand_icon": False,
            "mute_icon": False,
        }

    if state in ("listening", "processing", "speaking"):
        icons = {"listening": "\U0001f534", "processing": "\U0001f7e1", "speaking": "\U0001f7e2"}
        labels = {
            "listening": " Recording...",
            "processing": " Transcribing...",
            "speaking": " Speaking...",
        }
        title = icons[state] + labels[state]
        if held_count:
            title += f"  \U0001f4e5{held_count}"
        return {
            "title": title,
            "tooltip": tooltip,
            "use_brand_icon": False,
            "mute_icon": False,
        }

    # Idle path: surface the mic name + optional suffixes
    suffix = ""
    if held_count:
        suffix += f"  \U0001f4e5{held_count}"
    if speaker_muted:
        suffix += " \U0001f507"

    if is_mic_muted:
        return {
            "title": suffix.strip(),
            "tooltip": f"{tooltip} (muted)",
            "use_brand_icon": False,
            "mute_icon": True,
        }

    # Mic name lives in the tooltip only (hover). Title carries icon + suffixes.
    return {
        "title": suffix,
        "tooltip": tooltip,
        "use_brand_icon": True,
        "mute_icon": False,
    }


def vi_suffix_for_device(dev_name: str, config) -> str:
    """Return the voice-isolation suffix for a device name.

    Reads strictly from config.mic_profiles using case-insensitive substring
    matching (mirrors MicProfileManager.find_profile semantics).

    Returns:
      '  ·  VI: On'   when matched profile.voice_isolation_mode is True
      '  ·  VI: Off'  when matched profile.voice_isolation_mode is False
      ''              when voice_isolation_mode is None OR no profile matches

    IMPORTANT: NEVER probes macOS Voice Isolation system state directly via
    AppleVisionFoundation APIs — reads only from the user-edited profile config.
    See SPEC R5 / D-13 for the rationale.
    """
    if not dev_name or not config:
        return ""
    profiles = getattr(config, "mic_profiles", None) or {}
    dev_lower = dev_name.lower()
    for key, profile in profiles.items():
        if not key:
            continue
        if key.lower() in dev_lower:
            mode = getattr(profile, "voice_isolation_mode", None)
            if mode is True:
                return "  ·  VI: On"
            if mode is False:
                return "  ·  VI: Off"
            return ""
    return ""
