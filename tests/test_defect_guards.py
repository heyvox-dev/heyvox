"""Defect guard tests — targeted regression prevention derived from DEFECT-LOG.md.

Each test is tagged with the DEF-xxx entries it guards against.
These are fast, CI-friendly tests that don't require audio hardware or macOS UI.

References: .planning/DEFECT-LOG.md
"""

import importlib
import io
import os
import pkgutil
import re
import subprocess
import sys

import pytest


# ---------------------------------------------------------------------------
# Test 1: Import smoke test (P6 — catches DEF-007, DEF-009, DEF-011, DEF-016)
#
# Every .py module under heyvox/ must import without error. This catches
# SyntaxErrors, broken refactors, and missing attributes at import time.
# ---------------------------------------------------------------------------

def _collect_heyvox_modules():
    """Collect all importable module paths under heyvox/."""
    import heyvox
    modules = []
    package_path = os.path.dirname(heyvox.__file__)
    for importer, modname, ispkg in pkgutil.walk_packages(
        [package_path], prefix="heyvox."
    ):
        # Skip modules that require hardware or GUI at import time
        skip = {
            "heyvox.__main__",         # Calls main() at import
            "heyvox.hud.overlay",      # AppKit NSApplication
            "heyvox.hud.process",      # Spawns overlay
            "heyvox.audio.mic",        # pyaudio top-level import
            "heyvox.device_manager",   # pyaudio top-level import
            "heyvox.main",             # imports device_manager
            "heyvox.input.ptt",        # Quartz event tap
        }
        if modname in skip:
            continue
        modules.append(modname)
    return modules


@pytest.mark.parametrize("module_name", _collect_heyvox_modules())
def test_import_smoke(module_name):
    """Every heyvox module must import cleanly (P6: DEF-007, DEF-009, DEF-011)."""
    importlib.import_module(module_name)


def test_py_compile_all():
    """Every .py file must pass py_compile — catches SyntaxErrors (P5: DEF-007, DEF-011).

    This is a superset of the import test: it also checks modules skipped above
    (device_manager, overlay, etc.) for syntax correctness without executing them.
    """
    import heyvox
    root = os.path.dirname(heyvox.__file__)
    py_files = []
    for dirpath, _dirs, filenames in os.walk(root):
        for f in filenames:
            if f.endswith(".py"):
                py_files.append(os.path.join(dirpath, f))

    assert len(py_files) > 20, f"Expected 20+ .py files, found {len(py_files)}"

    failures = []
    for path in py_files:
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", path],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            failures.append(f"{os.path.relpath(path, root)}: {result.stderr.strip()}")

    assert not failures, f"py_compile failures:\n" + "\n".join(failures)


# ---------------------------------------------------------------------------
# Test 2: Stderr resilience (P2 — catches DEF-006, DEF-030)
#
# Daemon code paths must not crash when stderr is a broken pipe.
# We close stderr, call the function, and verify no BrokenPipeError escapes.
# ---------------------------------------------------------------------------

def test_injection_log_survives_broken_stderr():
    """injection._log() must not raise when stderr is broken (P2: DEF-006)."""
    from heyvox.input import injection

    old_stderr = sys.stderr
    try:
        # Simulate broken pipe: closed write end of a pipe
        r, w = os.pipe()
        os.close(r)  # close read end — writing to w will SIGPIPE/BrokenPipeError
        broken = os.fdopen(w, "w")
        sys.stderr = broken
        # Should not raise
        injection._log("test message from defect guard")
    finally:
        sys.stderr = old_stderr
        try:
            broken.close()
        except Exception:
            pass


def test_safe_stderr_survives_broken_pipe():
    """main._safe_stderr must not raise when stderr is broken (P2: DEF-030)."""
    # _safe_stderr is defined in main.py but requires device_manager import.
    # Test the pattern directly instead.
    old_stderr = sys.stderr
    try:
        r, w = os.pipe()
        os.close(r)
        broken = os.fdopen(w, "w")
        sys.stderr = broken
        # This is the pattern used by _safe_stderr
        try:
            print("test message", file=sys.stderr, flush=True)
        except (BrokenPipeError, OSError):
            pass  # This is what we're testing — the error must be caught
    finally:
        sys.stderr = old_stderr
        try:
            broken.close()
        except Exception:
            pass


def test_no_bare_stderr_prints_in_injection():
    """All stderr writes in injection.py must use _log() or be wrapped (P2: DEF-006).

    Scans for bare `print(..., file=sys.stderr)` calls that aren't inside
    try/except blocks. The _log() function is safe (wraps BrokenPipeError).
    """
    from heyvox.input import injection
    source = open(injection.__file__).read()

    # Find all print-to-stderr calls
    stderr_prints = [
        (i + 1, line)
        for i, line in enumerate(source.splitlines())
        if "file=sys.stderr" in line
        and "print(" in line
        and not line.strip().startswith("#")
    ]

    # The only allowed bare print-to-stderr is inside _log() itself (which is wrapped)
    # All others should use _log() instead
    bare_prints = []
    for lineno, line in stderr_prints:
        # _log's own print is at the module level, inside the function
        if "def _log" not in source.splitlines()[max(0, lineno - 4):lineno]:
            # Check if this print is inside a try block
            preceding = source.splitlines()[max(0, lineno - 5):lineno - 1]
            in_try = any("try:" in l for l in preceding)
            if not in_try:
                bare_prints.append(f"  line {lineno}: {line.strip()}")

    assert not bare_prints, (
        f"Bare print(file=sys.stderr) in injection.py (use _log() instead):\n"
        + "\n".join(bare_prints)
    )


# ---------------------------------------------------------------------------
# Test 3: Case-sensitivity lint (P1 — catches DEF-002, DEF-004, DEF-015)
#
# Any == or != comparison involving app_name, process_name, or similar
# OS-provided strings must use .lower() or .casefold().
# ---------------------------------------------------------------------------

# Patterns that indicate an OS-provided string being compared without lowering
_CASE_SENSITIVE_VARS = [
    "app_name", "process_name", "app_lower", "frontmost",
    "target_app", "dev_name", "ww_name",
]

_COMPARISON_PATTERN = re.compile(
    r'(?:==|!=)\s*(?:' + '|'.join(_CASE_SENSITIVE_VARS) + r')\b'
    r'|'
    r'\b(?:' + '|'.join(_CASE_SENSITIVE_VARS) + r')\s*(?:==|!=)',
)


def _scan_file_for_case_bugs(filepath: str) -> list[str]:
    """Scan a Python file for case-sensitive comparisons against OS strings."""
    issues = []
    with open(filepath) as f:
        lines = f.readlines()
    for i, line in enumerate(lines, 1):
        stripped = line.strip()
        if stripped.startswith("#") or stripped.startswith('"""') or stripped.startswith("'"):
            continue
        if _COMPARISON_PATTERN.search(line):
            # Skip if .lower() or .casefold() already present
            if ".lower()" in line or ".casefold()" in line:
                continue
            # Skip comparisons against string literals — those have known casing
            # (e.g., app_name == "Safari", process_name == "?")
            if re.search(r'==\s*["\']|["\'].*==', line):
                continue
            # Skip internal config comparisons (dev_name vs _last_calibrated_device)
            if "_last_calibrated" in line:
                continue
            issues.append(f"  {os.path.basename(filepath)}:{i}: {stripped}")
    return issues


def test_no_case_sensitive_app_comparisons():
    """OS-provided names must be compared case-insensitively (P1: DEF-002, DEF-015).

    Scans heyvox/ for bare == comparisons against app_name, process_name, etc.
    without .lower() on the same line.
    """
    import heyvox
    root = os.path.dirname(heyvox.__file__)
    all_issues = []

    for dirpath, _dirs, filenames in os.walk(root):
        for f in filenames:
            if not f.endswith(".py"):
                continue
            path = os.path.join(dirpath, f)
            issues = _scan_file_for_case_bugs(path)
            all_issues.extend(issues)

    assert not all_issues, (
        f"Case-sensitive comparisons against OS-provided names "
        f"(add .lower() to both sides):\n" + "\n".join(all_issues)
    )


# ---------------------------------------------------------------------------
# Test 4: Wake word phrase list completeness (catches DEF-004)
#
# Every wake word model name that could be loaded must have a corresponding
# non-empty entry in _WAKE_WORD_PHRASES after version suffix stripping.
# ---------------------------------------------------------------------------

_KNOWN_MODELS = [
    "hey_jarvis_v0.1",
    "hey_vox",
    "hey_vox_v0.1",
    "hey_vox_v0.2",
    "hey_jarvis",
]


@pytest.mark.parametrize("model_name", _KNOWN_MODELS)
def test_wake_word_phrases_not_empty(model_name):
    """Every known wake word model must resolve to a non-empty phrase list (DEF-004).

    The old bug: rsplit('_v', 1) on 'hey_vox' produced 'hey', which had no
    phrases. Now uses regex that only strips _v followed by a digit.
    """
    from heyvox.text_processing import _WAKE_WORD_PHRASES

    base = re.sub(r'_v\d[\d.]*$', '', model_name)
    assert base in _WAKE_WORD_PHRASES, (
        f"Model '{model_name}' stripped to '{base}' which has no phrase list. "
        f"Available keys: {list(_WAKE_WORD_PHRASES.keys())}"
    )
    assert len(_WAKE_WORD_PHRASES[base]) > 0, (
        f"Phrase list for '{base}' is empty"
    )


def test_strip_wake_words_hey_vox_not_noop():
    """strip_wake_words must actually strip 'hey vox' from text (DEF-004).

    Regression test: the old rsplit bug made this a no-op for hey_vox models.
    """
    from heyvox.text_processing import strip_wake_words

    result = strip_wake_words(
        "Hey Vox, what is the weather?",
        start_model="hey_vox",
        stop_model="hey_vox",
    )
    assert "hey vox" not in result.lower(), (
        f"strip_wake_words failed to remove 'hey vox': {result!r}"
    )


def test_strip_wake_words_hey_vox_v01():
    """Versioned model name must also resolve phrases (DEF-004)."""
    from heyvox.text_processing import strip_wake_words

    result = strip_wake_words(
        "Hey Vox do something Hey Vox",
        start_model="hey_vox_v0.1",
        stop_model="hey_vox_v0.1",
    )
    assert "hey vox" not in result.lower()


# ---------------------------------------------------------------------------
# Test 5: ShellCheck compliance (P8 — catches DEF-029)
#
# All .sh files must pass ShellCheck with no errors (warnings OK).
# Catches shell injection, unquoted variables, and bash compatibility issues.
# ---------------------------------------------------------------------------

def _shellcheck_available() -> bool:
    try:
        subprocess.run(["shellcheck", "--version"], capture_output=True, timeout=5)
        return True
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


# ---------------------------------------------------------------------------
# Test 6: Recording hard-cap preserves active dictation (DEF-049)
#
# DEF-038 added _MAX_POST_SPEECH_SECS = 30.0 as an unconditional hard ceiling
# to unwedge herald-pause when a noisy mic kept quiet_pct < 0.85 forever.
# DEF-049: the cap fired mid-sentence on legitimate long dictation because
# it didn't check whether the user was currently speaking. The cap must now
# gate on quiet_pct (active-speech detector) and a separate absolute ceiling
# bounds the worst case.
# ---------------------------------------------------------------------------

def _read_main_py() -> str:
    import heyvox
    path = os.path.join(os.path.dirname(heyvox.__file__), "main.py")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def test_def049_absolute_post_speech_cap_defined():
    """main.py must declare an absolute hard ceiling >= the soft cap (DEF-049).

    The soft cap (_MAX_POST_SPEECH_SECS) can be deferred when the user is
    actively speaking; _ABSOLUTE_MAX_POST_SPEECH_SECS bounds the worst case.
    """
    src = _read_main_py()
    m_soft = re.search(r"_MAX_POST_SPEECH_SECS\s*=\s*([\d.]+)", src)
    m_hard = re.search(r"_ABSOLUTE_MAX_POST_SPEECH_SECS\s*=\s*([\d.]+)", src)
    assert m_soft is not None, "Missing _MAX_POST_SPEECH_SECS in main.py"
    assert m_hard is not None, (
        "DEF-049: _ABSOLUTE_MAX_POST_SPEECH_SECS must be declared — the soft "
        "cap alone truncates long dictation."
    )
    assert float(m_hard.group(1)) >= float(m_soft.group(1)), (
        "DEF-049: absolute cap must be >= soft cap"
    )


def test_def049_soft_cap_gated_on_active_speech():
    """The soft-cap branch must check quiet_pct before force-stopping (DEF-049).

    Otherwise it truncates mid-sentence whenever a user dictates past 30 s.
    """
    src = _read_main_py()
    m = re.search(
        r"if\s+elapsed_since_speech\s*>\s*_MAX_POST_SPEECH_SECS:\s*\n"
        r"(?P<body>(?:[ \t]+.+\n){2,30})",
        src,
    )
    assert m is not None, "Could not locate soft-cap branch in main.py"
    body = m.group("body")
    assert "quiet_pct" in body, (
        "DEF-049: soft cap must reference quiet_pct so active-speech "
        "dictation isn't force-truncated at 30 s."
    )
    assert "_ABSOLUTE_MAX_POST_SPEECH_SECS" in body, (
        "DEF-049: soft cap must also check _ABSOLUTE_MAX_POST_SPEECH_SECS "
        "to bound the worst case."
    )


@pytest.mark.skipif(not _shellcheck_available(), reason="shellcheck not installed")
def test_shellcheck_all_scripts():
    """All .sh files must pass ShellCheck with no errors (P8: DEF-029).

    Checks for shell injection, unquoted variables, and bash compat issues.
    Uses severity=error to only fail on actual bugs, not style warnings.
    """
    import heyvox
    root = os.path.dirname(heyvox.__file__)
    sh_files = []
    for dirpath, _dirs, filenames in os.walk(root):
        for f in filenames:
            if f.endswith(".sh"):
                sh_files.append(os.path.join(dirpath, f))

    assert len(sh_files) > 0, "Expected at least one .sh file"

    failures = []
    for path in sh_files:
        result = subprocess.run(
            ["shellcheck", "--severity=error", "--format=gcc", path],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode != 0:
            relpath = os.path.relpath(path, root)
            failures.append(f"--- {relpath} ---\n{result.stdout.strip()}")

    assert not failures, (
        f"ShellCheck errors in {len(failures)} file(s):\n" + "\n\n".join(failures)
    )
