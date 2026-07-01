"""Tests for the voice-isolation suffix appended to mic-submenu entries.

Phase 14 / SPEC R5 / D-13. Also includes the AVCaptureDevice import
regression guard (SPEC R5 acceptance #11).
"""
import re


from heyvox.config import HeyvoxConfig, MicProfileEntryConfig
from heyvox.hud.menu_bar_title import vi_suffix_for_device


# Regex matches actual import/usage patterns, not docstring mentions or
# inline "do not use AVCaptureDevice" comments. Catches:
#   import AVFoundation
#   from AVFoundation import ...
#   AVCaptureDevice.defaultDeviceWithMediaType_(...)
#   AVFoundation.AVCaptureDevice(...)
_AV_USAGE_PATTERNS = [
    r"^\s*import\s+AVFoundation\b",
    r"^\s*from\s+AVFoundation\b",
    r"\bAVCaptureDevice\s*\.\s*\w",   # attribute access AVCaptureDevice.something
    r"\bAVCaptureDevice\s*\(",        # constructor call AVCaptureDevice(...)
    r"\bAVFoundation\s*\.\s*\w",      # AVFoundation.x — not "AVFoundation." in prose
]


class TestVISuffix:
    """vi_suffix_for_device returns the right suffix based on the profile."""

    def test_vi_on_when_profile_true(self):
        config = HeyvoxConfig(
            mic_profiles={
                "evolve2": MicProfileEntryConfig(voice_isolation_mode=True),
            },
        )
        assert vi_suffix_for_device("Evolve2 75 UC", config) == "  ·  VI: On"

    def test_vi_off_when_profile_false(self):
        config = HeyvoxConfig(
            mic_profiles={
                "evolve2": MicProfileEntryConfig(voice_isolation_mode=False),
            },
        )
        assert vi_suffix_for_device("Evolve2 75 UC", config) == "  ·  VI: Off"

    def test_no_suffix_when_mode_none(self):
        config = HeyvoxConfig(
            mic_profiles={
                "evolve2": MicProfileEntryConfig(voice_isolation_mode=None),
            },
        )
        assert vi_suffix_for_device("Evolve2 75 UC", config) == ""

    def test_no_suffix_when_no_profile_match(self):
        config = HeyvoxConfig()
        assert vi_suffix_for_device("Unknown Headset", config) == ""

    def test_case_insensitive_substring_match(self):
        config = HeyvoxConfig(
            mic_profiles={
                "AIRPODS": MicProfileEntryConfig(voice_isolation_mode=True),
            },
        )
        result = vi_suffix_for_device("airpods pro max", config)
        assert result.startswith("  ·")

    def test_empty_dev_name(self):
        config = HeyvoxConfig()
        assert vi_suffix_for_device("", config) == ""


class TestNoAVCaptureDeviceImport:
    """SPEC R5 / acceptance #11: no AVCaptureDevice or AVFoundation import added.

    Regression guard — if a future executor adds AVFoundation to probe macOS
    Voice Isolation state directly, this test fails.
    """

    def test_overlay_does_not_import_avcapturedevice(self):
        import inspect
        from heyvox.hud import overlay
        source = inspect.getsource(overlay)
        for pattern in _AV_USAGE_PATTERNS:
            assert not re.search(pattern, source, re.MULTILINE), (
                f"overlay.py contains AVCaptureDevice/AVFoundation usage matching: {pattern}"
            )

    def test_menu_bar_title_does_not_import_avcapturedevice(self):
        import inspect
        from heyvox.hud import menu_bar_title
        source = inspect.getsource(menu_bar_title)
        for pattern in _AV_USAGE_PATTERNS:
            assert not re.search(pattern, source, re.MULTILINE), (
                f"menu_bar_title.py contains AVCaptureDevice/AVFoundation usage matching: {pattern}"
            )
        # And the module should declare itself PyObjC-free — no actual imports
        # (the docstring may mention AppKit/pyobjc in "no imports" context, so
        # we check for actual import statements, not substring presence)
        assert not re.search(r"^\s*(import|from)\s+AppKit\b", source, re.MULTILINE)
        assert not re.search(r"^\s*(import|from)\s+objc\b", source, re.MULTILINE)
        assert not re.search(r"^\s*(import|from)\s+PyObjC\b", source, re.MULTILINE)
