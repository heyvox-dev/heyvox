"""Unit tests for heyvox.input.toast.show_failure_toast (Plan 15-04).

Covers HS-up path, HS-down osascript fallback, hs-binary-missing fallthrough,
subprocess error swallowing, quote/newline safety via json.dumps, title
parameter plumbing.
"""

from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Tier 1: Hammerspoon path
# ---------------------------------------------------------------------------


def test_hs_up_uses_hammerspoon():
    from heyvox.input.toast import show_failure_toast

    with patch("heyvox.input.toast.subprocess.call", return_value=0), \
         patch(
             "heyvox.input.toast.shutil.which",
             return_value="/opt/homebrew/bin/hs",
         ), \
         patch("heyvox.input.toast.Path") as mock_path, \
         patch("heyvox.input.toast.subprocess.Popen") as mock_popen:
        mock_path.return_value.exists.return_value = True
        show_failure_toast("test msg")

        args = mock_popen.call_args.args[0]
        assert args[0] == "/opt/homebrew/bin/hs"
        assert args[1] == "-c"
        assert "hs.alert.show" in args[2]


def test_hs_message_is_json_quoted():
    """Quotes and backslashes survive via json.dumps."""
    from heyvox.input.toast import show_failure_toast

    with patch("heyvox.input.toast.subprocess.call", return_value=0), \
         patch(
             "heyvox.input.toast.shutil.which",
             return_value="/opt/homebrew/bin/hs",
         ), \
         patch("heyvox.input.toast.Path") as mock_path, \
         patch("heyvox.input.toast.subprocess.Popen") as mock_popen:
        mock_path.return_value.exists.return_value = True
        show_failure_toast('foo "bar" baz\nnext')

        script = mock_popen.call_args.args[0][2]
        # json.dumps escapes quotes as \" and newlines as \n
        assert r'\"bar\"' in script or '\\"bar\\"' in script
        assert r"\n" in script or "\\n" in script


def test_hs_up_but_binary_missing_falls_through():
    """hs exe missing from path → osascript fallback."""
    from heyvox.input.toast import show_failure_toast

    with patch("heyvox.input.toast.subprocess.call", return_value=0), \
         patch(
             "heyvox.input.toast.shutil.which",
             return_value="/opt/homebrew/bin/hs",
         ), \
         patch("heyvox.input.toast.Path") as mock_path, \
         patch("heyvox.input.toast.subprocess.Popen") as mock_popen:
        mock_path.return_value.exists.return_value = False
        show_failure_toast("test")

        args = mock_popen.call_args.args[0]
        assert args[0] == "osascript"
        assert "display notification" in args[2]


# ---------------------------------------------------------------------------
# Tier 2: osascript fallback
# ---------------------------------------------------------------------------


def test_hs_down_falls_back_to_osascript():
    """pgrep returns 1 → osascript path used (hs skipped entirely)."""
    from heyvox.input.toast import show_failure_toast

    with patch("heyvox.input.toast.subprocess.call", return_value=1), \
         patch("heyvox.input.toast.subprocess.Popen") as mock_popen:
        show_failure_toast("test msg")

        args = mock_popen.call_args.args[0]
        assert args[0] == "osascript"
        assert "display notification" in args[2]
        # verify the message is inside the script
        assert "test msg" in args[2]


def test_osascript_default_title_present():
    """Default title 'HeyVox paste' ends up in osascript script."""
    from heyvox.input.toast import show_failure_toast

    with patch("heyvox.input.toast.subprocess.call", return_value=1), \
         patch("heyvox.input.toast.subprocess.Popen") as mock_popen:
        show_failure_toast("msg")

        script = mock_popen.call_args.args[0][2]
        assert 'with title "HeyVox paste"' in script


def test_osascript_custom_title_plumbed():
    from heyvox.input.toast import show_failure_toast

    with patch("heyvox.input.toast.subprocess.call", return_value=1), \
         patch("heyvox.input.toast.subprocess.Popen") as mock_popen:
        show_failure_toast("msg", title="Custom Title")

        script = mock_popen.call_args.args[0][2]
        assert 'with title "Custom Title"' in script


def test_osascript_quotes_escaped():
    from heyvox.input.toast import show_failure_toast

    with patch("heyvox.input.toast.subprocess.call", return_value=1), \
         patch("heyvox.input.toast.subprocess.Popen") as mock_popen:
        show_failure_toast('has "quotes"')

        script = mock_popen.call_args.args[0][2]
        assert 'display notification "has \\"quotes\\""' in script


# ---------------------------------------------------------------------------
# Silent failure — subprocess errors must not propagate
# ---------------------------------------------------------------------------


def test_hs_popen_oserror_falls_through_to_osascript():
    from heyvox.input.toast import show_failure_toast

    calls = []

    def _popen(*args, **kwargs):
        calls.append(args[0][0])
        if args[0][0].endswith("hs"):
            raise OSError("boom")
        return MagicMock()

    with patch("heyvox.input.toast.subprocess.call", return_value=0), \
         patch(
             "heyvox.input.toast.shutil.which",
             return_value="/opt/homebrew/bin/hs",
         ), \
         patch("heyvox.input.toast.Path") as mock_path, \
         patch("heyvox.input.toast.subprocess.Popen", side_effect=_popen):
        mock_path.return_value.exists.return_value = True
        show_failure_toast("test")  # must not raise

    # HS attempted, then osascript
    assert calls[0].endswith("hs")
    assert calls[-1] == "osascript"


def test_both_paths_oserror_silent():
    """Even when both subprocess attempts fail, no exception propagates."""
    from heyvox.input.toast import show_failure_toast

    with patch("heyvox.input.toast.subprocess.call", return_value=1), \
         patch(
             "heyvox.input.toast.subprocess.Popen",
             side_effect=OSError("dead"),
         ):
        show_failure_toast("final")  # must not raise


def test_pgrep_oserror_treated_as_hs_not_running():
    """If pgrep itself can't run, _hammerspoon_running returns False safely."""
    from heyvox.input.toast import show_failure_toast

    with patch(
        "heyvox.input.toast.subprocess.call", side_effect=OSError("no pgrep")
    ), \
         patch("heyvox.input.toast.subprocess.Popen") as mock_popen:
        show_failure_toast("msg")

        args = mock_popen.call_args.args[0]
        assert args[0] == "osascript"


# ---------------------------------------------------------------------------
# Hammerspoon liveness gate (DEF-074) — pgrep call shape
# ---------------------------------------------------------------------------


def test_hammerspoon_running_uses_pgrep_q_hammerspoon():
    from heyvox.input.toast import _hammerspoon_running

    with patch("heyvox.input.toast.subprocess.call", return_value=0) as mock_call:
        assert _hammerspoon_running() is True
        args = mock_call.call_args.args[0]
        assert args == ["pgrep", "-q", "Hammerspoon"]


def test_hammerspoon_running_returns_false_on_exit_1():
    from heyvox.input.toast import _hammerspoon_running

    with patch("heyvox.input.toast.subprocess.call", return_value=1):
        assert _hammerspoon_running() is False
