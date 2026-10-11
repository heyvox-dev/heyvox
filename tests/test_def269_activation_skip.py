"""Guard tests for DEF-269: activateWithOptions_ blocks ~1 s off the main thread.

The paste path runs on a worker thread, and every Orca dictation spent ~1.07 s in
`yank: app activate` re-activating an app that was already frontmost. Activation is
now skipped when the target is the live frontmost app.
"""
import sys
from unittest.mock import MagicMock, patch

from heyvox.input import activation, frontmost


def _app(pid=1234):
    app = MagicMock()
    app.processIdentifier.return_value = pid
    app.bundleIdentifier.return_value = "com.stablyai.orca"
    return app


def _front(monkeypatch, pid):
    front = MagicMock()
    front.processIdentifier.return_value = pid
    monkeypatch.setattr(frontmost, "frontmost_app", lambda: front)


def test_already_frontmost_app_is_not_activated(monkeypatch):
    _front(monkeypatch, 1234)
    app = _app(1234)
    with patch.dict(sys.modules, {"AppKit": MagicMock()}):
        assert activation.activate_running_app(app) is True
    app.activateWithOptions_.assert_not_called()


def test_background_app_is_activated(monkeypatch):
    _front(monkeypatch, 999)
    app = _app(1234)
    with patch.dict(sys.modules, {"AppKit": MagicMock()}):
        activation.activate_running_app(app)
    app.activateWithOptions_.assert_called_once()


def test_activate_pid_skips_frontmost_target(monkeypatch):
    _front(monkeypatch, 1234)
    app = _app(1234)
    appkit = MagicMock()
    appkit.NSRunningApplication.runningApplicationWithProcessIdentifier_.return_value = app
    monkeypatch.setattr(activation._time, "sleep", lambda s: None)
    with patch.dict(sys.modules, {"AppKit": appkit}):
        assert activation.activate_pid(1234, "Orca") is True
    app.activateWithOptions_.assert_not_called()


def test_yank_does_not_reactivate_frontmost_app(monkeypatch):
    from heyvox.input import target

    _front(monkeypatch, 1234)
    app = _app(1234)
    appkit = MagicMock()
    appkit.NSRunningApplication.runningApplicationsWithBundleIdentifier_.return_value = [app]
    lock = MagicMock(app_bundle_id="com.stablyai.orca", app_pid=1234, workspace_id=None)
    with patch.dict(sys.modules, {"AppKit": appkit}):
        target._yank_back_app_and_workspace(lock, None, None)
    app.activateWithOptions_.assert_not_called()
