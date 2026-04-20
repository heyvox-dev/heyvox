"""Hush — Browser media control for HeyVox.

Chrome extension + native messaging host that pauses/resumes browser media
(YouTube, Spotify Web, etc.) during TTS playback and voice recording.
"""

import json
import shutil
from pathlib import Path

# Package root
HUSH_HOME = Path(__file__).parent

HUSH_EXTENSION = HUSH_HOME / "extension"
HUSH_HOST = HUSH_HOME / "host"
HUSH_SCRIPTS = HUSH_HOME / "scripts"

# Stable install location — survives Conductor workspace archival.
# The native messaging manifest points here instead of into the workspace.
_STABLE_HOST_DIR = Path.home() / ".config" / "heyvox" / "hush"

# Chrome native messaging host manifest location (macOS)
_NMH_DIR = Path.home() / "Library" / "Application Support" / "Google" / "Chrome" / "NativeMessagingHosts"
_NMH_MANIFEST = _NMH_DIR / "com.hush.bridge.json"


def install_hush_host(extension_id: str = "khaokodhclonjnbdnbcgdggffnnmpnim") -> tuple[bool, str]:
    """Install Hush native messaging host to a stable path.

    Copies hush_host.py from the package to ~/.config/heyvox/hush/ and writes
    the Chrome native messaging manifest pointing to that stable path.

    Returns (success, message).
    """
    src = HUSH_HOST / "hush_host.py"
    if not src.exists():
        return False, f"hush_host.py not found: {src}"

    # Copy host script to stable location
    _STABLE_HOST_DIR.mkdir(parents=True, exist_ok=True)
    dest = _STABLE_HOST_DIR / "hush_host.py"
    shutil.copy2(src, dest)
    dest.chmod(0o755)

    # Write native messaging manifest
    _NMH_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {
        "name": "com.hush.bridge",
        "description": "Hush native messaging host for media control",
        "path": str(dest),
        "type": "stdio",
        "allowed_origins": [f"chrome-extension://{extension_id}/"],
    }
    with open(_NMH_MANIFEST, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")

    return True, f"Installed to {dest}, manifest at {_NMH_MANIFEST}"
