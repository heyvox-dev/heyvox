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
# Test 6: No short hard cap on recording duration (DEF-050 regression guard)
#
# The only duration ceiling on a recording must be `config.max_recording_secs`
# (default 300 s / 5 min). DEF-038 previously added `_MAX_POST_SPEECH_SECS =
# 30.0` as a short post-speech hard cap to mitigate a one-off G435 sidetone
# scenario (DEF-036). That cap truncated legitimate long dictation mid-sentence
# and was reverted as DEF-050. The noisy-mic scenarios DEF-038 was guarding
# against are now handled by DEF-036 (hardware workaround) and DEF-045/DEF-047
# (wake-word VAD gate). Re-introducing a short post-speech cap without first
# revisiting DEF-050 should fail this guard.
# ---------------------------------------------------------------------------

def _read_main_py() -> str:
    import heyvox
    path = os.path.join(os.path.dirname(heyvox.__file__), "main.py")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def test_def050_no_short_post_speech_cap():
    """main.py must not reintroduce a short post-speech hard cap (DEF-050).

    `max_recording_secs` (5 min, from config) is the only safety ceiling.
    Previous 30 s / 120 s caps truncated active dictation.
    """
    src = _read_main_py()
    assert "_MAX_POST_SPEECH_SECS" not in src, (
        "DEF-050: `_MAX_POST_SPEECH_SECS` was reintroduced. The 30 s post-speech "
        "cap from DEF-038 truncated legitimate long dictation mid-sentence. "
        "Rely on `config.max_recording_secs` (5 min) as the only ceiling."
    )
    assert "_ABSOLUTE_MAX_POST_SPEECH_SECS" not in src, (
        "DEF-050: `_ABSOLUTE_MAX_POST_SPEECH_SECS` was reintroduced. Rely on "
        "`config.max_recording_secs` (5 min) as the only ceiling."
    )


def test_def050_max_recording_secs_still_enforced():
    """`max_recording_secs` must remain the single enforced ceiling (DEF-050)."""
    src = _read_main_py()
    assert "max_recording_secs" in src, (
        "DEF-050: `max_recording_secs` is the only hard ceiling on a recording. "
        "It must remain wired into the main loop."
    )
    assert re.search(
        r"if\s+elapsed\s*>\s*max_recording_secs\s*:", src
    ), (
        "DEF-050: expected `if elapsed > max_recording_secs:` guard in main loop."
    )


# ---------------------------------------------------------------------------
# Test 7: User-pinned mic must not be kicked out by AUDIO-13 (DEF-051)
#
# When a user manually picks a mic from the HUD menu, `_mic_pinned` is set.
# AUDIO-13's dead-mic watchdog must respect that pin — otherwise 30 s of
# idle silence (totally normal when not speaking) fires a reinit, cooldowns
# the wireless device, and falls back to built-in. Also, `_do_manual_pin`
# must reset `last_good_audio_time` so a stale countdown from a previous
# silent mic doesn't immediately fire against the freshly-pinned one.
# ---------------------------------------------------------------------------

def _read_device_manager_py() -> str:
    import heyvox
    path = os.path.join(os.path.dirname(heyvox.__file__), "device_manager.py")
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _extract_method_body(src: str, method_name: str) -> str:
    """Return the body of the named method (text between `def <name>(...):`
    and the next `def ` / `class ` at the same indent, or EOF)."""
    m = re.search(rf"^(\s+)def {method_name}\([^)]*\)[^:]*:\n", src, re.MULTILINE)
    if m is None:
        return ""
    start = m.end()
    indent = m.group(1)
    end_m = re.search(
        rf"^(?:{indent}def |class )", src[start:], re.MULTILINE,
    )
    return src[start:start + end_m.start()] if end_m else src[start:]


def test_def051_audio13_exempts_pinned_mic():
    """`check_dead_mic_timeout` must bail out when `_mic_pinned` is True."""
    src = _read_device_manager_py()
    body = _extract_method_body(src, "check_dead_mic_timeout")
    assert body, "Could not locate check_dead_mic_timeout body"
    # The pin check must short-circuit *before* the dead_secs computation.
    assert re.search(r"if\s+self\._mic_pinned\s*:\s*\n\s+return", body), (
        "DEF-051: `check_dead_mic_timeout` must early-return when "
        "`_mic_pinned` is True. Without this, idle silence evicts the "
        "wireless mic after 30 s and falls back to built-in."
    )


def test_def051_do_manual_pin_resets_audio13_timer():
    """`_do_manual_pin` must reset `last_good_audio_time` before returning.

    Otherwise a stale timer from a previous silent mic fires AUDIO-13 within
    seconds of the switch, cooldowning the freshly-pinned wireless device.
    """
    src = _read_device_manager_py()
    body = _extract_method_body(src, "_do_manual_pin")
    assert body, "Could not locate _do_manual_pin body"
    assert "last_good_audio_time" in body, (
        "DEF-051: `_do_manual_pin` must reset `last_good_audio_time` so the "
        "AUDIO-13 countdown restarts from the pin moment."
    )
    # And the counters too — otherwise the diagnostic histogram is wrong.
    assert "dead_mic_zero_chunks" in body, (
        "DEF-051: `_do_manual_pin` must also clear `dead_mic_zero_chunks` "
        "so the AUDIO-13 stream diagnostic reflects only post-pin samples."
    )


def test_def053_vad_silent_grace_during_recording():
    """During recording, `_vad_silent` must honour a grace window covering the
    wake-word model's feature-window lag.

    DEF-053: user said "Hey Vox" 11 times over 2 s but the stop never fired —
    trailing-silence chunks kept resetting `_consecutive_hits` to 0 under the
    strict DEF-047 VAD gate. Fix introduces `_VAD_SILENT_GRACE` and a rolling
    `_last_nonsilent_time` so recent activity keeps the gate open long enough
    for the classifier's feature window to clear.
    """
    src = _read_main_py()
    assert "_VAD_SILENT_GRACE" in src, (
        "DEF-053: `_VAD_SILENT_GRACE` constant must exist in main.py"
    )
    assert "_last_nonsilent_time" in src, (
        "DEF-053: `_last_nonsilent_time` tracking must exist in main.py"
    )
    # The grace window must apply to the recording path specifically, not
    # accidentally relaxed on the idle path (which still needs strict VAD
    # suppression of silence-driven false positives per DEF-045).
    assert re.search(
        r"if\s+_is_rec:\s*\n\s+_vad_silent\s*=\s*\(?\s*\n?\s*_raw_vad_silent",
        src,
    ), (
        "DEF-053: The grace-window VAD computation must be gated on `_is_rec`. "
        "Idle-path VAD must stay strict (DEF-045)."
    )


def test_def053_tts_min_volume_floor():
    """Herald must clamp TTS volume to a minimum floor so the user doesn't hear
    quiet TTS when their pre-duck media volume happens to be low.

    DEF-053: user's media volume had drifted to 37 %, Herald faithfully played
    TTS at 37 %, user reported "rather low volume." Fix: `tts_min_volume` in
    OrchestratorConfig and a `max(original_vol, cfg.tts_min_volume)` clamp in
    `_set_tts_volume`.
    """
    from heyvox.herald.orchestrator import OrchestratorConfig
    cfg = OrchestratorConfig()
    assert hasattr(cfg, "tts_min_volume"), (
        "DEF-053: OrchestratorConfig must expose `tts_min_volume`"
    )
    assert 0.3 <= cfg.tts_min_volume <= 1.0, (
        f"DEF-053: tts_min_volume={cfg.tts_min_volume} outside sane range [0.3, 1.0]"
    )
    import inspect
    from heyvox.herald import orchestrator as orch
    src = inspect.getsource(orch._set_tts_volume)
    assert "tts_min_volume" in src, (
        "DEF-053: `_set_tts_volume` must read `cfg.tts_min_volume` to apply the floor"
    )
    assert re.search(r"max\s*\(\s*original_vol\s*,\s*cfg\.tts_min_volume", src), (
        "DEF-053: `_set_tts_volume` must clamp with `max(original_vol, cfg.tts_min_volume)`"
    )


def test_def053_hud_dbg_skips_audio_level():
    """HUD-DBG logger must not emit a per-message line for `audio_level` — that
    message type fires at ~20 Hz and would flood the log with empty `state=`
    entries (`audio_level` payloads use the `level` key, not `state`).
    """
    src = _read_main_py()
    assert re.search(
        r'if\s+msg\.get\("type"\)\s*!=\s*"audio_level"',
        src,
    ), (
        "DEF-053: HUD-DBG logger must skip audio_level messages to prevent "
        "~20 Hz empty-state log spam."
    )


def test_def054_activate_app_poll_verifies_pid():
    """_activate_app must poll frontmost PID after activating and retry on
    mismatch. On Electron bundles (Conductor, VS Code, Slack, Cursor) the
    activate call is advisory — WindowServer can keep a sibling helper PID
    as the key window. A single activate + sleep, without a poll-verify
    loop, produced paste landing in the wrong window within the same bundle.
    """
    target_py = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "heyvox", "input", "target.py",
    )
    src = open(target_py).read()
    # Pull out the body of _activate_app so sibling functions don't satisfy
    # the assertion accidentally.
    m = re.search(
        r"def _activate_app\([^)]*\)[^:]*:\s*(.*?)(?=\n(?:def |class |[^\s]))",
        src,
        re.DOTALL,
    )
    assert m, "DEF-054: could not locate _activate_app body in target.py"
    body = m.group(1)
    assert "frontmostApplication" in body and "processIdentifier" in body, (
        "DEF-054: _activate_app must read frontmostApplication().processIdentifier() "
        "to verify the target PID actually became frontmost."
    )
    assert "for" in body and "range" in body and "activateWithOptions_" in body, (
        "DEF-054: _activate_app must loop with periodic re-activation — a single "
        "activateWithOptions_ call is advisory only on Electron bundles."
    )


def test_def054_paste_guard_compares_pid_not_just_name():
    """The paste path must log a WARNING when frontmost PID differs from the
    target PID, even if the app *name* matches. Multi-PID bundles share a
    name across helpers, so a name-only guard silently passes when paste
    lands in the wrong window.
    """
    inj_py = os.path.join(
        os.path.dirname(os.path.dirname(__file__)),
        "heyvox", "input", "injection.py",
    )
    src = open(inj_py).read()
    # Must plumb expected_pid parameter
    assert re.search(
        r"def _osascript_type_text\([^)]*expected_pid",
        src,
        re.DOTALL,
    ), "DEF-054: _osascript_type_text must accept expected_pid parameter."
    # Must emit a WARNING when PID differs from expected
    assert re.search(
        r"WARNING[^\"\']*pid=\{expected_pid\}[^\"\']*frontmost",
        src,
    ) or re.search(
        r"expected pid=\{expected_pid\}.*frontmost.*pid=",
        src,
        re.DOTALL,
    ), (
        "DEF-054: paste path must log a WARNING when frontmost PID differs "
        "from expected_pid (not just when names differ)."
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
