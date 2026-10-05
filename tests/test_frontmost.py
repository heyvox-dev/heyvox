"""DEF-260: live frontmost-app lookup (heyvox.input.frontmost).

NSWorkspace.frontmostApplication() is frozen inside the listener; the paste
path must use the lsappinfo-backed value instead.
"""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch


from heyvox.input import frontmost
from heyvox.input.frontmost import FrontmostApp
# Bound at import time, before the autouse conftest fixture stubs the module attribute.
REAL_LSAPPINFO_FRONT = frontmost._lsappinfo_front

LS_INFO = '"pid"=90130\n"CFBundleIdentifier"="com.stablyai.orca"\n"LSDisplayName"="Orca"\n'


def _fake_lsappinfo(front="ASN:0x0-0xbaf6aeb:\n", info=LS_INFO):
    def run(args, **kw):
        out = front if args[1] == "front" else info
        return subprocess.CompletedProcess(args, 0, stdout=out, stderr="")
    return run


class TestLsappinfoParse:
    def test_parses_pid_bundle_and_name(self):
        with patch("heyvox.input.frontmost.subprocess.run", side_effect=_fake_lsappinfo()):
            app = REAL_LSAPPINFO_FRONT()
        assert app == FrontmostApp(pid=90130, bundle_id="com.stablyai.orca", name="Orca")
        assert (app.processIdentifier(), app.bundleIdentifier(), app.localizedName()) == (
            90130, "com.stablyai.orca", "Orca")

    def test_app_without_bundle_id_gives_none_bundle(self):
        info = '"pid"=51\n"LSDisplayName"="helper"\n'
        with patch("heyvox.input.frontmost.subprocess.run", side_effect=_fake_lsappinfo(info=info)):
            app = REAL_LSAPPINFO_FRONT()
        assert app.pid == 51 and app.bundle_id is None

    def test_empty_front_is_none(self):
        with patch("heyvox.input.frontmost.subprocess.run", side_effect=_fake_lsappinfo(front="")):
            assert REAL_LSAPPINFO_FRONT() is None

    def test_unparseable_info_is_none(self):
        with patch("heyvox.input.frontmost.subprocess.run", side_effect=_fake_lsappinfo(info="garbage")):
            assert REAL_LSAPPINFO_FRONT() is None

    def test_missing_binary_is_none(self):
        with patch("heyvox.input.frontmost.subprocess.run", side_effect=FileNotFoundError):
            assert REAL_LSAPPINFO_FRONT() is None

    def test_timeout_is_none(self):
        with patch("heyvox.input.frontmost.subprocess.run",
                   side_effect=subprocess.TimeoutExpired("lsappinfo", 1)):
            assert REAL_LSAPPINFO_FRONT() is None


class TestFrontmostApp:
    def test_live_value_wins_over_stale_nsworkspace(self):
        stale = MagicMock()
        stale.bundleIdentifier.return_value = None  # terminated app, as in the listener
        mock_appkit = MagicMock()
        mock_appkit.NSWorkspace.sharedWorkspace.return_value.frontmostApplication.return_value = stale
        live = FrontmostApp(pid=90130, bundle_id="com.stablyai.orca", name="Orca")
        with patch("heyvox.input.frontmost._lsappinfo_front", return_value=live), \
             patch.dict("sys.modules", {"AppKit": mock_appkit}):
            assert frontmost.frontmost_app() is live

    def test_falls_back_to_nsworkspace(self):
        cached = MagicMock()
        mock_appkit = MagicMock()
        mock_appkit.NSWorkspace.sharedWorkspace.return_value.frontmostApplication.return_value = cached
        with patch("heyvox.input.frontmost._lsappinfo_front", return_value=None), \
             patch.dict("sys.modules", {"AppKit": mock_appkit}):
            assert frontmost.frontmost_app() is cached

    def test_none_when_everything_fails(self):
        mock_appkit = MagicMock()
        mock_appkit.NSWorkspace.sharedWorkspace.side_effect = RuntimeError("x")
        with patch("heyvox.input.frontmost._lsappinfo_front", return_value=None), \
             patch.dict("sys.modules", {"AppKit": mock_appkit}):
            assert frontmost.frontmost_app() is None


class TestPastePathUsesLiveValue:
    """The regression itself: stale NSWorkspace said 'None', live said Orca."""

    def _stale_appkit(self):
        stale = MagicMock()
        stale.bundleIdentifier.return_value = None
        stale.processIdentifier.return_value = 36573
        mock_appkit = MagicMock()
        mock_appkit.NSWorkspace.sharedWorkspace.return_value.frontmostApplication.return_value = stale
        return mock_appkit

    def test_verify_target_focused_passes_on_live_match(self):
        from heyvox.input.injection import _verify_target_focused
        live = FrontmostApp(pid=90130, bundle_id="com.stablyai.orca", name="Orca")
        with patch("heyvox.input.frontmost._lsappinfo_front", return_value=live), \
             patch.dict("sys.modules", {"AppKit": self._stale_appkit()}):
            assert _verify_target_focused("com.stablyai.orca") is True

    def test_verify_target_focused_still_fails_on_real_mismatch(self):
        from heyvox.input.injection import _verify_target_focused
        live = FrontmostApp(pid=7, bundle_id="com.apple.Safari", name="Safari")
        with patch("heyvox.input.frontmost._lsappinfo_front", return_value=live):
            assert _verify_target_focused("com.stablyai.orca") is False

    def test_save_frontmost_pid_is_live(self):
        from heyvox.input.injection import save_frontmost_pid
        live = FrontmostApp(pid=90130, bundle_id="com.stablyai.orca", name="Orca")
        with patch("heyvox.input.frontmost._lsappinfo_front", return_value=live), \
             patch.dict("sys.modules", {"AppKit": self._stale_appkit()}):
            assert save_frontmost_pid() == 90130
