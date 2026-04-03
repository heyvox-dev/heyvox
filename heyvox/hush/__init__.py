"""Hush — Browser media control for HeyVox.

Chrome extension + native messaging host that pauses/resumes browser media
(YouTube, Spotify Web, etc.) during TTS playback and voice recording.
"""

from pathlib import Path

# Package root
HUSH_HOME = Path(__file__).parent

HUSH_EXTENSION = HUSH_HOME / "extension"
HUSH_HOST = HUSH_HOME / "host"
HUSH_SCRIPTS = HUSH_HOME / "scripts"
