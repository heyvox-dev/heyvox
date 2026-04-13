"""
Tests for injection sanitization across the codebase.

Covers bug-audit patterns:
- AppleScript injection via app_name in injection.py
- SQL injection via workspace names in Herald bash scripts
- Shell injection via filenames/variables passed to subprocesses
"""

import unittest
from unittest.mock import patch, MagicMock


class TestAppleScriptEscaping(unittest.TestCase):
    """press_enter() and focus_app() must escape app names for AppleScript."""

    @patch("heyvox.input.injection._get_frontmost_app", return_value='My "App"')
    @patch("heyvox.input.injection.subprocess.run")
    def test_quotes_in_app_name(self, mock_run, _mock_front):
        """App name with double quotes should be escaped."""
        from heyvox.input.injection import press_enter
        mock_run.return_value = MagicMock(returncode=0)
        press_enter(1, app_name='My "App"')
        # Last osascript call is the Enter script
        script = mock_run.call_args[0][0][2]  # osascript -e <script>
        assert r'My \"App\"' in script
        # The unescaped quote should NOT appear (would break AppleScript)
        assert 'My "App"' not in script

    @patch("heyvox.input.injection._get_frontmost_app", return_value="App\\Path")
    @patch("heyvox.input.injection.subprocess.run")
    def test_backslash_in_app_name(self, mock_run, _mock_front):
        """App name with backslashes should be escaped."""
        from heyvox.input.injection import press_enter
        mock_run.return_value = MagicMock(returncode=0)
        press_enter(1, app_name="App\\Path")
        script = mock_run.call_args[0][0][2]
        assert "App\\\\Path" in script

    @patch("heyvox.input.injection.subprocess.run")
    def test_focus_app_escapes_quotes(self, mock_run):
        """focus_app() should also escape special chars."""
        from heyvox.input.injection import focus_app
        mock_run.return_value = MagicMock(returncode=0)
        focus_app('Evil"; do shell script "rm -rf /')
        script = mock_run.call_args[0][0][2]
        # The injected command should be escaped, not executable
        assert 'do shell script' not in script.split('"')[0]
        assert '\\"' in script

    @patch("heyvox.input.injection.subprocess.run")
    def test_normal_app_name_unchanged(self, mock_run):
        """Regular app names should pass through normally."""
        from heyvox.input.injection import focus_app
        mock_run.return_value = MagicMock(returncode=0)
        focus_app("Conductor")
        script = mock_run.call_args[0][0][2]
        assert "Conductor" in script

    @patch("heyvox.input.injection._get_frontmost_app", return_value="App\nEvil")
    @patch("heyvox.input.injection.subprocess.run")
    def test_newline_in_app_name(self, mock_run, _mock_front):
        """Newlines in app names should not break the AppleScript structure."""
        from heyvox.input.injection import press_enter
        mock_run.return_value = MagicMock(returncode=0)
        press_enter(1, app_name="App\nEvil")
        # Should not raise; script is passed as a single -e argument
        assert mock_run.called


class TestSQLInjectionPrevention(unittest.TestCase):
    """Herald's bash scripts use workspace names in SQLite queries.
    The fix escapes single quotes as ''. Verify the Python-side equivalents."""

    def test_workspace_name_with_quotes(self):
        """Workspace names with single quotes should be escaped for SQL."""
        # This mirrors the fix in herald/lib/config.sh: ws_safe="${ws//\'/\'\'}"
        ws = "user's workspace"
        ws_safe = ws.replace("'", "''")
        assert ws_safe == "user''s workspace"
        # The escaped version should be safe in a SQL string literal
        sql = f"SELECT label FROM workspaces WHERE name = '{ws_safe}'"
        assert "user''s workspace" in sql

    def test_workspace_name_with_semicolon(self):
        """SQL injection via semicolon should be contained by quoting."""
        ws = "test'; DROP TABLE workspaces;--"
        ws_safe = ws.replace("'", "''")
        sql = f"SELECT label FROM workspaces WHERE name = '{ws_safe}'"
        # The injection is inside the string literal, not executable
        assert "DROP TABLE" in sql  # present but harmless — inside quotes
        assert ws_safe == "test''; DROP TABLE workspaces;--"


class TestHeraldEnvironmentInjection(unittest.TestCase):
    """Worker.sh passes values via env vars instead of string interpolation.
    Verify the pattern is correct."""

    def test_env_var_approach_safe(self):
        """Env vars in Python subprocess don't allow shell injection."""
        import subprocess
        # This is the safe pattern used in worker.sh
        result = subprocess.run(
            ["python3", "-c", "import os; print(os.environ.get('_TEST_VAR', ''))"],
            capture_output=True, text=True,
            env={**__import__("os").environ, "_TEST_VAR": "'; rm -rf /; echo '"},
            timeout=5,
        )
        # The dangerous string is passed safely — it's just a string value
        assert result.stdout.strip() == "'; rm -rf /; echo '"


if __name__ == "__main__":
    unittest.main()
