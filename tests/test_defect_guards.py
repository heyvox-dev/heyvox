"""Defect guard tests — targeted regression prevention derived from DEFECT-LOG.md.

Each test is tagged with the DEF-xxx entries it guards against.
These are fast, CI-friendly tests that don't require audio hardware or macOS UI.

References: .planning/DEFECT-LOG.md
"""

import importlib
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

    assert not failures, "py_compile failures:\n" + "\n".join(failures)


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
            in_try = any("try:" in prev_line for prev_line in preceding)
            if not in_try:
                bare_prints.append(f"  line {lineno}: {line.strip()}")

    assert not bare_prints, (
        "Bare print(file=sys.stderr) in injection.py (use _log() instead):\n"
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
        "Case-sensitive comparisons against OS-provided names "
        "(add .lower() to both sides):\n" + "\n".join(all_issues)
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
    """Herald must expose a configurable TTS volume floor and apply it when
    restoring volume after the duck.

    DEF-053 originally hard-coded the floor at 0.55 to keep TTS audible over
    quiet background media. That clamp is now user-tunable via `tts.min_volume`
    in config.yaml — users who want their slider fully respected can set it
    near 0.0. This test only guards the wiring (knob exists, source clamps),
    not the specific default.
    """
    from heyvox.herald.orchestrator import OrchestratorConfig
    cfg = OrchestratorConfig()
    assert hasattr(cfg, "tts_min_volume"), (
        "DEF-053: OrchestratorConfig must expose `tts_min_volume`"
    )
    assert 0.0 <= cfg.tts_min_volume <= 1.0, (
        f"DEF-053: tts_min_volume={cfg.tts_min_volume} outside [0.0, 1.0]"
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


# ---------------------------------------------------------------------------
# DEF-103 — Stop-wake silently drops 6/10 attempts (single-frame peak
# at G435/BT-HFP audio quality). Guards the three trigger paths added
# alongside the consecutive-frames gate: fast-path on score >= 0.92,
# and sliding-window 2-of-4. See DEFECT-LOG.md DEF-103 for the data.
# ---------------------------------------------------------------------------

def test_def103_high_confidence_fast_stop_constant():
    """`_HIGH_CONFIDENCE_FAST_STOP` must exist and sit above DEF-043's
    mid-sentence phoneme-flare ceiling (~0.85) but below 1.0 so real
    "Hey Vox" peaks (typically 0.99+) reliably exceed it.
    """
    src = _read_main_py()
    m = re.search(r"_HIGH_CONFIDENCE_FAST_STOP\s*=\s*([\d.]+)", src)
    assert m, (
        "DEF-103: `_HIGH_CONFIDENCE_FAST_STOP` constant missing — single-frame "
        "fast-stop path is the primary fix for the 6/10 lost-stop pattern."
    )
    val = float(m.group(1))
    assert 0.86 <= val <= 0.97, (
        f"DEF-103: _HIGH_CONFIDENCE_FAST_STOP={val} outside safe range "
        f"[0.86, 0.97]. Below 0.86 risks DEF-043 mid-sentence false stops; "
        f"above 0.97 misses normal real-speech peaks (BT-HFP can attenuate "
        f"the model's peak below 1.0)."
    )


def test_def103_sliding_window_constants():
    """`_STOP_WINDOW_FRAMES` and `_STOP_WINDOW_HITS_REQUIRED` must define
    the 2-of-N sliding window for the second stop-path. Window must be
    short enough (<= 6 frames ≈ 480 ms) that mid-sentence phoneme runs
    don't accumulate, but long enough (>= 3) to tolerate one dip frame
    between two real hits.
    """
    src = _read_main_py()
    m_win = re.search(r"_STOP_WINDOW_FRAMES\s*=\s*(\d+)", src)
    m_hit = re.search(r"_STOP_WINDOW_HITS_REQUIRED\s*=\s*(\d+)", src)
    assert m_win and m_hit, (
        "DEF-103: sliding-window constants missing — peak→dip→peak case "
        "(DEF-103 evidence: 149/252 single-peak frames lost) needs the "
        "window path to recover stops the consecutive gate kills."
    )
    win = int(m_win.group(1))
    hits = int(m_hit.group(1))
    assert 3 <= win <= 6, (
        f"DEF-103: _STOP_WINDOW_FRAMES={win} outside [3, 6]. Too narrow "
        f"loses to brief dips; too wide enables false stops on phoneme runs."
    )
    assert hits == 2, (
        f"DEF-103: _STOP_WINDOW_HITS_REQUIRED={hits}, expected 2. Single-hit "
        f"window is what fast-path covers; >=3 reverts to consecutive-style "
        f"strictness that DEF-103 was filed against."
    )


def test_def103_stop_hit_window_state_exists():
    """The `_stop_hit_window` per-wake-word deque tracker must exist —
    that's the data structure that lets the 2-of-4 path see across
    multiple frames. Without it the window stop-path can't fire.
    """
    src = _read_main_py()
    assert "_stop_hit_window" in src, (
        "DEF-103: `_stop_hit_window` state container missing. Sliding-window "
        "path needs per-ww deque of recent hit frame indices."
    )
    assert "collections.deque" in src or "from collections import deque" in src, (
        "DEF-103: collections.deque must back `_stop_hit_window` (popleft "
        "on each frame is O(1); list would scale poorly under long recordings)."
    )


def test_def103_fast_stop_trigger_path_present():
    """The fast-stop trigger condition must be wired into the trigger
    block — not just defined as a constant. Look for the conjunction of
    `_is_rec`, `triggered`, `> _HIGH_CONFIDENCE_FAST_STOP`, and
    `not _vad_silent` near the trigger gate.
    """
    src = _read_main_py()
    # Allow flexible whitespace / line breaks but require all four conjuncts.
    pattern = re.compile(
        r"_is_rec[\s\S]{0,80}triggered[\s\S]{0,80}_HIGH_CONFIDENCE_FAST_STOP"
        r"[\s\S]{0,80}not\s+_vad_silent",
        re.MULTILINE,
    )
    assert pattern.search(src), (
        "DEF-103: fast-stop trigger condition missing or mis-wired. Must "
        "combine: _is_rec AND triggered AND s > _HIGH_CONFIDENCE_FAST_STOP "
        "AND not _vad_silent."
    )


def test_def103_window_stop_trigger_path_present():
    """The sliding-window trigger must check the deque length against
    `_STOP_WINDOW_HITS_REQUIRED` while recording.
    """
    src = _read_main_py()
    pattern = re.compile(
        r"len\s*\(\s*_stop_hit_window[\s\S]{0,80}_STOP_WINDOW_HITS_REQUIRED",
        re.MULTILINE,
    )
    assert pattern.search(src), (
        "DEF-103: window-stop trigger missing. Must compare "
        "`len(_stop_hit_window[ww])` against `_STOP_WINDOW_HITS_REQUIRED`."
    )


def test_def103_stop_path_observability():
    """A `[STOP_PATH]` log line must record which of the three paths fired
    (consec / fast / window) so future regressions in stop reliability are
    attributable from logs alone — without this, a re-emergence of the
    DEF-103 lost-stop pattern is invisible to log-health.
    """
    src = _read_main_py()
    assert "[STOP_PATH]" in src, (
        "DEF-103: `[STOP_PATH]` observability log missing. Future stop-wake "
        "regressions need a log tag to grep for in heyvox.log; otherwise "
        "we'll only learn about them from user complaints (the original "
        "DEF-103 detection mode)."
    )


def test_def103_stop_path_disjunction_in_trigger():
    """The trigger condition must be the disjunction of all four stop
    paths: consecutive-frames + fast-path + sliding-window + the
    ultra-confidence bypass. Regression guard against accidentally
    collapsing back to fewer paths during a refactor.
    """
    src = _read_main_py()
    # Find the trigger expression — should be `if W or X or Y or Z:`
    # involving the four named flags.
    pattern = re.compile(
        r"if\s+_consec_trigger\s+or\s+_fast_stop\s+or\s+_window_stop"
        r"\s+or\s+_ultra_stop\s*:",
    )
    assert pattern.search(src), (
        "DEF-103: trigger guard missing or refactored away. The four stop "
        "paths must be disjunctively combined: `_consec_trigger or _fast_stop "
        "or _window_stop or _ultra_stop`. If you renamed any of these, update "
        "this test and the DEFECT-LOG entry together."
    )


# ---------------------------------------------------------------------------
# DEF-080: herald CLI must be spawned via sys.executable -m, not PATH
#
# A stale symlink at ~/.local/bin/herald pointing at a different workspace's
# bash herald used to spawn duplicate orchestrators (every TTS message
# played twice). Pinning to sys.executable removes the PATH dependency.
# ---------------------------------------------------------------------------


def test_def080_herald_cmd_is_python_dash_m_list():
    """HERALD_CMD must be a list starting with sys.executable, not the
    bare string "herald". A regression to PATH-based lookup would re-expose
    the duplicate-orchestrator bug."""
    import sys as _sys
    from heyvox.audio import tts

    assert isinstance(tts.HERALD_CMD, list), (
        "HERALD_CMD must be a list (DEF-080). Bare-string PATH lookup is "
        "vulnerable to stale ~/.local/bin/herald symlinks."
    )
    assert len(tts.HERALD_CMD) >= 3, (
        f"HERALD_CMD must look like [python, -m, heyvox.herald.cli]; "
        f"got {tts.HERALD_CMD!r}"
    )
    assert tts.HERALD_CMD[0] == _sys.executable, (
        "HERALD_CMD[0] must be sys.executable so the herald CLI runs under "
        "the same interpreter that imported tts.py (DEF-080)."
    )
    assert tts.HERALD_CMD[1] == "-m", (
        f"HERALD_CMD[1] must be '-m'; got {tts.HERALD_CMD[1]!r}"
    )
    assert tts.HERALD_CMD[2] == "heyvox.herald.cli", (
        f"HERALD_CMD[2] must point at heyvox.herald.cli; "
        f"got {tts.HERALD_CMD[2]!r}"
    )


def test_def080_herald_dispatch_spawns_via_pinned_argv():
    """The _herald() dispatcher must unpack HERALD_CMD into the subprocess
    argv. A regression that uses bare "herald" would silently drop the
    sys.executable pin."""
    from unittest.mock import patch, MagicMock
    from heyvox.audio import tts

    with patch("heyvox.audio.tts.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        tts._herald("speak", "test", input_text="hi")

        assert mock_run.called, "_herald must spawn subprocess.run"
        called_argv = mock_run.call_args[0][0]
        # Pinned argv must be the prefix; cmd + args follow.
        assert called_argv[: len(tts.HERALD_CMD)] == tts.HERALD_CMD, (
            f"_herald argv {called_argv!r} must start with "
            f"HERALD_CMD {tts.HERALD_CMD!r}"
        )
        assert called_argv[len(tts.HERALD_CMD)] == "speak"
        assert called_argv[len(tts.HERALD_CMD) + 1] == "test"


# ---------------------------------------------------------------------------
# DEF-117: fast-path stop-wake requires pre-silence gate
#
# Mid-sentence phoneme FP at score=0.982 (threshold 0.91) on
# "...momentan irrelevant" fired the fast-path before. Gate added 2026-05-25
# requires _recent_silence (DEF-096-B's _PRE_SILENCE_DISCOUNT_WINDOW horizon)
# for fast-path stop-wake to fire.
# ---------------------------------------------------------------------------


def _read_main_src() -> str:
    import heyvox
    return open(os.path.join(os.path.dirname(heyvox.__file__), "main.py")).read()


def test_def117_fast_stop_requires_recent_silence():
    """The _fast_stop predicate must include the _recent_silence term."""
    src = _read_main_src()
    # Find the _fast_stop = (...) block
    m = re.search(r"_fast_stop\s*=\s*\(([^)]+)\)", src)
    assert m is not None, "Could not find _fast_stop assignment in main.py"
    block = m.group(1)
    assert "_recent_silence" in block, (
        "_fast_stop predicate must include `and _recent_silence` (DEF-117). "
        "Otherwise mid-sentence high-confidence phoneme bursts trigger a "
        "false stop. See .planning/quick/260525-stop-wake-vad-gate/."
    )


def test_def117_near_miss_fast_blocked_tag_present():
    """NEAR_MISS_FAST_BLOCKED log tag must exist so forensic users can see
    how often the silence gate fires (P-detector-without-action)."""
    src = _read_main_src()
    assert "NEAR_MISS_FAST_BLOCKED" in src, (
        "NEAR_MISS_FAST_BLOCKED log tag missing — DEF-117 forensic visibility "
        "was wired alongside the gate; do not remove without replacing."
    )


def test_def117_stop_path_log_carries_pre_silence_field():
    """STOP_PATH log line must surface pre_silence= so a triggered stop's
    gate state is recoverable from logs."""
    src = _read_main_src()
    # Look for the STOP_PATH log block; the literal pre_silence= label must
    # appear in the same f-string region.
    m = re.search(r"\[STOP_PATH\][\s\S]{0,500}", src)
    assert m is not None, "STOP_PATH log block not found in main.py"
    assert "pre_silence" in m.group(0), (
        "STOP_PATH log line must include pre_silence= for DEF-117 forensics."
    )


# ---------------------------------------------------------------------------
# DEF-118: window-path stop-wake requires pre-silence gate
#
# Mid-sentence FP at score=0.997 win=2/2 hits=2/2 pre_silence=False fired
# the window-path on 2026-05-27 08:01:40 ("Ich denke der Grund ist ein...").
# DEF-117 had gated fast-path only; this extends the same gate to window-path
# because two-frame bursts in continuous German speech reach win=2/2 too.
# ---------------------------------------------------------------------------


def test_def118_window_stop_requires_recent_silence():
    """The _window_stop predicate must include the _recent_silence term."""
    src = _read_main_src()
    # _window_stop spans multiple lines and contains a nested ().
    # Match from `_window_stop = (` to the next line that is whitespace + `)`.
    m = re.search(r"_window_stop\s*=\s*\(([\s\S]+?)\n\s*\)", src)
    assert m is not None, "Could not find _window_stop assignment in main.py"
    block = m.group(1)
    assert "_recent_silence" in block, (
        "_window_stop predicate must include `and _recent_silence` (DEF-118). "
        "Otherwise two consecutive high-score phoneme bursts in continuous "
        "speech trigger a false stop via the window path."
    )


def test_def118_near_miss_window_blocked_tag_present():
    """NEAR_MISS_WINDOW_BLOCKED log tag must exist so forensic users can see
    how often the silence gate fires on the window path (P-detector-without-action)."""
    src = _read_main_src()
    assert "NEAR_MISS_WINDOW_BLOCKED" in src, (
        "NEAR_MISS_WINDOW_BLOCKED log tag missing — DEF-118 forensic visibility "
        "was wired alongside the gate; do not remove without replacing."
    )


# ---------------------------------------------------------------------------
# DEF-124: mic-zombie banner UX hardening
#
# Three coupled fixes to device_manager.reinit():
#   1. Banner text varies by mic type — built-in mics don't have a mute button,
#      so "check mute" is misleading; point at Permission/exclusive-hold/HAL.
#   2. After a successful recovery onto a different device, the warn banner
#      is stale and must be cleared explicitly.
#   3. HFP probe-fallback reinit is an *expected* cache flush, not an
#      unexpected zombie — banner + error toast suppressed via expected=True.
# ---------------------------------------------------------------------------


def _read_device_manager_src() -> str:
    import heyvox
    return open(os.path.join(os.path.dirname(heyvox.__file__), "device_manager.py")).read()


def test_def124_reinit_has_expected_kwarg():
    """reinit() must accept an expected= kwarg so callers can suppress
    user-visible banners when the reinit is a planned cache flush."""
    src = _read_device_manager_src()
    m = re.search(r"def reinit\(self,([^)]+)\)", src)
    assert m is not None, "Could not find reinit signature in device_manager.py"
    sig = m.group(1)
    assert "expected" in sig, (
        "reinit() must accept an `expected` kwarg (DEF-124). Without it, the "
        "HFP-probe-fallback reinit can't suppress the misleading "
        "'Mic zombie: reinitializing' toast and 'Mic silent' banner."
    )


def test_def124_reinit_banner_uses_builtin_specific_text():
    """The mic-silent banner text must branch on is_builtin_mic — built-in
    mics get a Permission/exclusive-hold hint, not the misleading 'check mute'."""
    src = _read_device_manager_src()
    assert "is_builtin_mic" in src, (
        "device_manager.py must call is_builtin_mic() to differentiate the "
        "mic-silent banner hint (DEF-124). Built-in mics have no mute button "
        "so 'check mute' is misleading."
    )
    assert "Microphone permission" in src, (
        "Built-in-mic hint must mention 'Microphone permission' as one of "
        "the actual causes (DEF-124)."
    )


def test_def124_reinit_clears_banner_after_recovery():
    """When reinit recovers onto a different device, the stale mic-silent
    banner must be cleared explicitly (HUDSurface.clear)."""
    src = _read_device_manager_src()
    assert 'HUDSurface.clear("mic-zombie")' in src, (
        'device_manager.py must call HUDSurface.clear("mic-zombie") in the '
        "recovery-success branch (DEF-124). Without it, the warn banner "
        "lingers in the menu bar for the full TTL even though the mic is "
        "live again."
    )


def test_def124_hfp_path_uses_expected_reinit():
    """The BT HFP probe-fallback must call reinit with expected=True so the
    user-visible banners stay quiet when the user is the one who triggered
    the flush."""
    import heyvox
    bt_src = open(os.path.join(os.path.dirname(heyvox.__file__), "audio", "bt.py")).read()
    src = _read_device_manager_src() + bt_src
    # Look for any reinit() call carrying expected=True; the HFP path is
    # the canonical caller introduced by DEF-124. After the BT isolation
    # refactor (issue #16) this call lives in BtHfpMixin in audio/bt.py.
    assert re.search(r"reinit\([^)]*expected\s*=\s*True", src), (
        "BT HFP probe-fallback (or any expected-reinit caller) must invoke "
        "reinit(expected=True) (DEF-124). Otherwise the misleading "
        "'Mic silent — check mute (MacBook Pro Microphone)' banner fires "
        "right when the user has actively clicked a different headset."
    )


# ---------------------------------------------------------------------------
# DEF-127: auto-HFP-probe when output device changes to a BT headset
# ---------------------------------------------------------------------------


def test_def127_output_change_auto_triggers_hfp_probe():
    """The output-device-change branch in scan() must call _bt_trigger_hfp_switch
    when the new output looks like an A2DP-only BT device."""
    src = _read_device_manager_src()
    assert "DEF-127" in src, (
        "DEF-127 marker missing from device_manager.py — auto-HFP-probe on "
        "output change was removed. Without it, the user has to manually "
        "click the headset in the HUD menu every time they plug a new BT mic."
    )
    # The auto path must set _bt_hfp_pin_mode = False (auto-switch, not pin).
    m = re.search(r"DEF-127[\s\S]{0,2000}?_bt_hfp_pin_mode\s*=\s*False", src)
    assert m is not None, (
        "DEF-127 auto-probe path must set _bt_hfp_pin_mode = False so the "
        "switch is a passive auto-detect, not a user pin (which has stronger "
        "stick semantics)."
    )


# ---------------------------------------------------------------------------
# DEF-128: hush-noop info banner removed from menu bar
# ---------------------------------------------------------------------------


def test_def128_hush_noop_banner_not_emitted():
    """heyvox/audio/media.py must NOT call HUDSurface.banner(source="hush-noop").
    The signal is recoverable from the [media] log without the menu bar cost."""
    import heyvox
    src = open(os.path.join(os.path.dirname(heyvox.__file__), "audio", "media.py")).read()
    assert not re.search(
        r'HUDSurface\.banner\([^)]*source\s*=\s*["\']hush-noop["\']',
        src,
    ), (
        "heyvox/audio/media.py must not call HUDSurface.banner(source='hush-noop') "
        "(DEF-128). That banner fired on every TTS event with no browser media — "
        "non-actionable noise in the menu bar."
    )


def test_def120_worker_logger_name_pinned():
    """Worker logger must be pinned to 'heyvox.herald.worker' — not getLogger(__name__).

    Reason: the hook shim runs `python3 -m heyvox.herald.worker`, which sets
    __name__ == "__main__". getLogger(__name__) would then orphan the logger
    from the heyvox.herald handler chain — every log call silently writes
    nowhere. This was DEF-120's root cause.
    """
    import heyvox
    src = open(os.path.join(os.path.dirname(heyvox.__file__), "herald", "worker.py")).read()
    assert 'getLogger("heyvox.herald.worker")' in src, (
        "DEF-120: worker.py must pin its logger name explicitly so the "
        "`python -m heyvox.herald.worker` entry path stays connected to the "
        "heyvox.herald file handler. Replacing with getLogger(__name__) "
        "silently breaks all hook-driven worker logging."
    )
    # Strip comment-only lines before grepping so the DEF-120 explanatory
    # comment (which DOES reference getLogger(__name__) as the anti-pattern)
    # doesn't false-positive against the actual binding.
    code_lines = [
        ln for ln in src.splitlines()
        if not ln.lstrip().startswith("#")
    ]
    code_src = "\n".join(code_lines)
    assert "getLogger(__name__)" not in code_src, (
        "DEF-120: worker.py has an actual getLogger(__name__) binding (not "
        "just a comment) — under the `python -m` entry point that resolves "
        "to 'logging.getLogger(\"__main__\")', orphaned from the heyvox.herald "
        "handler chain."
    )


def test_def120_worker_silent_skips_log_at_info():
    """The three worker early-exit branches must log at INFO with forensic context.

    Reason: silent DEBUG-level returns make "why didn't HeyVox speak?"
    unanswerable from herald-debug.log. DEF-120 promoted these to INFO with
    raw_len + hook + ws so the next missing-TTS is one grep away.
    """
    import heyvox
    src = open(os.path.join(os.path.dirname(heyvox.__file__), "herald", "worker.py")).read()
    for marker in (
        'WORKER: no <tts> block in response',
        'WORKER: <tts> block rejected',
        'WORKER: verbosity=skip',
    ):
        assert marker in src, (
            f"DEF-120: forensic breadcrumb {marker!r} missing — silent-skip "
            "branches must emit one INFO line so logs answer 'did the hook "
            "fire and skip, or did it not fire at all?'"
        )


def test_def120_worker_logger_writes_to_herald_debug_log_under_dash_m():
    """End-to-end: `python -m heyvox.herald.worker` with no TTS block must
    leave a 'WORKER: no <tts> block' line in herald-debug.log.

    This is the integration test that import-only unit tests miss — it
    catches the __name__=='__main__' logger orphan that DEF-120 was about.
    """
    from heyvox.constants import HERALD_DEBUG_LOG
    # Capture log size before
    try:
        before_size = os.path.getsize(HERALD_DEBUG_LOG)
    except OSError:
        before_size = 0
    # Invoke worker as -m with no TTS block
    result = subprocess.run(
        [sys.executable, "-m", "heyvox.herald.worker"],
        input="probe response with no tts block — def120 guard",
        capture_output=True, text=True, timeout=15,
    )
    assert result.returncode == 0, (
        f"worker exit={result.returncode} stderr={result.stderr[:200]!r}"
    )
    # Read what was appended
    try:
        with open(HERALD_DEBUG_LOG) as f:
            f.seek(before_size)
            appended = f.read()
    except OSError as e:
        pytest.fail(f"could not read {HERALD_DEBUG_LOG}: {e}")
    assert "WORKER: no <tts> block" in appended, (
        "DEF-120: running worker as `python -m` with no TTS block did not "
        "leave the forensic breadcrumb in herald-debug.log. Logger pin is "
        "broken, the silent-skip INFO promotion regressed, or the file path "
        f"differs from HERALD_DEBUG_LOG={HERALD_DEBUG_LOG!r}. "
        f"Appended chunk: {appended[:300]!r}"
    )


def test_def121_hooks_route_through_find_heyvox_python():
    """All hook shims must route through heyvox_run_worker / find_heyvox_python.

    Reason: Conductor / Claude Code may prepend project-local virtualenvs
    (Poetry, conda, venv) to PATH that don't have heyvox installed. Bare
    `python3 -m heyvox.herald.worker` then hits ModuleNotFoundError under
    the hook's /dev/null redirect — silent failure, accumulating TMPFILEs,
    no TTS for that workspace.
    """
    import heyvox
    hooks_dir = os.path.join(os.path.dirname(heyvox.__file__), "herald", "hooks")
    _lib = open(os.path.join(hooks_dir, "_lib.sh")).read()
    assert "find_heyvox_python()" in _lib, (
        "DEF-121: _lib.sh must define find_heyvox_python()."
    )
    assert "heyvox_run_worker()" in _lib, (
        "DEF-121: _lib.sh must define heyvox_run_worker() so hook shims "
        "share one place that resolves the interpreter."
    )
    # Every async-spawning hook shim must NOT use bare `python3 -m heyvox`.
    # on-ambient is sync (exec) and is allowed to use find_heyvox_python directly.
    for sh in ("on-response.sh", "on-notify.sh",
               "on-session-start.sh", "on-session-end.sh"):
        body = open(os.path.join(hooks_dir, sh)).read()
        assert "heyvox_run_worker " in body, (
            f"DEF-121: {sh} must call heyvox_run_worker — bare "
            f"`python3 -m heyvox.herald.worker` hits ModuleNotFoundError "
            f"in project-virtualenv workspaces."
        )
        assert "python3 -m heyvox.herald.worker" not in body, (
            f"DEF-121: {sh} still has a bare `python3 -m heyvox.herald.worker` "
            f"call — that resolves against the inherited PATH, including "
            f"project virtualenvs that don't have heyvox installed. Route "
            f"through heyvox_run_worker instead."
        )
    # on-ambient uses exec, so it's allowed to invoke its python directly,
    # but must still go through find_heyvox_python.
    amb = open(os.path.join(hooks_dir, "on-ambient.sh")).read()
    assert "find_heyvox_python" in amb, (
        "DEF-121: on-ambient.sh must resolve python via find_heyvox_python "
        "before exec, not use bare `python3`."
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


# Real `launchctl list com.heyvox.listener` output (label-mode): a property-list
# dict, NOT the tab-separated PID\tExit\tLabel rows of bare `launchctl list`.
_LAUNCHCTL_RUNNING = """{
\t"StandardOutPath" = "/tmp/heyvox.log";
\t"Label" = "com.heyvox.listener";
\t"OnDemand" = false;
\t"LastExitStatus" = 0;
\t"PID" = 14091;
\t"Program" = "/Users/work/.pyenv/versions/3.12.12/bin/python";
};
"""

# Loaded-but-stopped: dict has LastExitStatus but no "PID" key.
_LAUNCHCTL_STOPPED = """{
\t"StandardOutPath" = "/tmp/heyvox.log";
\t"Label" = "com.heyvox.listener";
\t"OnDemand" = false;
\t"LastExitStatus" = 0;
\t"Program" = "/Users/work/.pyenv/versions/3.12.12/bin/python";
};
"""


def test_def130_status_parses_running_plist_dict():
    """get_status() must read launchctl's label-mode plist-dict output.

    DEF-130: get_status() ran `launchctl list <label>` (which returns a
    plist dict) but parsed it as the tab-separated `PID\\tExit\\tLabel` rows
    of the bare `launchctl list`. The closing "};" line had <3 tab fields,
    so it always returned running=False — `heyvox status` reported "Stopped"
    while the daemon was alive. Feed the real dict format and assert it's
    read as running.
    """
    from unittest.mock import patch, MagicMock
    from heyvox.setup import launchd

    with patch("heyvox.setup.launchd.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=_LAUNCHCTL_RUNNING)
        status = launchd.get_status()

    assert status["running"] is True, (
        "DEF-130: a live daemon (PID present in launchctl plist dict) must "
        f"parse as running, got {status!r}"
    )
    assert status["pid"] == 14091, (
        f"DEF-130: must extract PID 14091 from the plist dict, got {status['pid']!r}"
    )
    assert status["loaded"] is True
    assert status["exit_code"] == 0


def test_def130_status_parses_stopped_plist_dict():
    """A loaded-but-stopped job has LastExitStatus but no PID key.

    DEF-130: must report running=False without a "PID" key, and still
    surface the last exit code.
    """
    from unittest.mock import patch, MagicMock
    from heyvox.setup import launchd

    with patch("heyvox.setup.launchd.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout=_LAUNCHCTL_STOPPED)
        status = launchd.get_status()

    assert status["running"] is False, (
        f"DEF-130: no PID key means not running, got {status!r}"
    )
    assert status["pid"] is None
    assert status["loaded"] is True
    assert status["exit_code"] == 0


# ---------------------------------------------------------------------------
# DEF-132: a freshly-(re)selected mic must not inherit the dead-air debt that
# accrued during the slow reinit/HFP switch. reinit()/handle_io_error() reset
# the AUDIO-13 timers but not ctx.last_read_time, so the main-loop no-data
# stall guard evicted a just-switched Bluetooth headset within the same second
# and bounced back to the built-in mic. Fix: _arm_post_switch_grace() resets
# last_read_time + arms a longer first-packet window (mic_just_switched).
# ---------------------------------------------------------------------------


def test_def132_appcontext_has_switch_grace_fields():
    """DEF-132: AppContext must declare the read-stall clock and grace flag."""
    from heyvox.app_context import AppContext
    fields = AppContext.__dataclass_fields__
    assert "last_read_time" in fields, "DEF-132: last_read_time must be declared"
    assert "mic_just_switched" in fields, "DEF-132: mic_just_switched must be declared"
    ctx = AppContext()
    assert ctx.mic_just_switched is False, "DEF-132: grace flag must default off"
    assert ctx.last_read_time == 0.0


def test_def132_recovery_paths_reset_stall_clock():
    """DEF-132: both slow recovery tails must reset the main-loop read-stall
    clock, or a freshly-switched BT headset is evicted before its first PCM
    packet and bounces straight back to the built-in mic."""
    src = _read_device_manager_src()
    assert "def _arm_post_switch_grace" in src, (
        "DEF-132: the _arm_post_switch_grace() helper must exist"
    )
    assert "self.ctx.last_read_time = time.monotonic()" in src, (
        "DEF-132: the grace helper must reset ctx.last_read_time to monotonic()"
    )
    assert src.count("self._arm_post_switch_grace()") >= 2, (
        "DEF-132: reinit() AND handle_io_error() must both call "
        "_arm_post_switch_grace() — dropping one re-opens the bounce bug"
    )


def test_def132_main_loop_honors_switch_grace():
    """DEF-132: the no-data stall guard must extend its window after a switch
    and revert once data flows, else it never returns to the 5s guard."""
    src = _read_main_src()
    assert "_POST_SWITCH_STALL_SECS" in src, (
        "DEF-132: the post-switch stall-grace constant must exist"
    )
    assert "ctx.mic_just_switched" in src, (
        "DEF-132: the stall guard must branch on ctx.mic_just_switched"
    )
    assert "ctx.mic_just_switched = False" in src, (
        "DEF-132: the grace flag must be cleared on the first successful read, "
        "else the extended window never reverts to the normal 5s guard"
    )


def test_def132_arm_post_switch_grace_runtime():
    """DEF-132: _arm_post_switch_grace() must reset the clock and arm the flag."""
    pytest.importorskip("pyaudio")
    import time as _t
    from heyvox.app_context import AppContext
    from heyvox.device_manager import DeviceManager
    ctx = AppContext()
    ctx.last_read_time = 0.0
    ctx.mic_just_switched = False
    dm = DeviceManager(
        ctx=ctx, config=None,
        log_fn=lambda *a, **k: None, hud_send=lambda *a, **k: None,
    )
    before = _t.monotonic()
    dm._arm_post_switch_grace()
    assert ctx.mic_just_switched is True, (
        "DEF-132: a mic (re)selection must arm the first-packet grace flag"
    )
    assert ctx.last_read_time >= before, (
        "DEF-132: last_read_time must be reset to ~now (monotonic) so the "
        "freshly-switched device isn't evicted on carried-over dead-air debt"
    )


# ---------------------------------------------------------------------------
# DEF-104: hotplug self-restart — a higher-priority mic that is live in
# CoreAudio but absent from PortAudio's per-process cache (plugged in after
# the daemon's first init) is invisible until restart. detect_missed_hotplug
# is the pure detection core; the main loop self-restarts on its signal.
# ---------------------------------------------------------------------------

def test_def104_detects_priority_device_missing_from_portaudio():
    from heyvox.audio.device_handle import detect_missed_hotplug
    # The exact 2026-06-06 case: Jabra live in CoreAudio, PortAudio only sees
    # the built-in mic, daemon stuck on the built-in fallback.
    missed = detect_missed_hotplug(
        live_input_names={"jabra link 390", "macbook pro microphone"},
        pa_input_names={"macbook pro microphone"},
        mic_priority=["Jabra Link 390", "MacBook Pro Microphone"],
        current_dev_name="MacBook Pro Microphone",
    )
    assert missed == "Jabra Link 390", (
        "DEF-104: a priority device live in CoreAudio but absent from PortAudio "
        "and outranking the current mic must be flagged for a cache-clearing restart"
    )


def test_def104_no_restart_when_device_already_in_portaudio():
    from heyvox.audio.device_handle import detect_missed_hotplug
    # PortAudio sees the Jabra too — the normal scan/switch handles it, no restart.
    missed = detect_missed_hotplug(
        live_input_names={"jabra link 390", "macbook pro microphone"},
        pa_input_names={"jabra link 390", "macbook pro microphone"},
        mic_priority=["Jabra Link 390", "MacBook Pro Microphone"],
        current_dev_name="MacBook Pro Microphone",
    )
    assert missed is None, (
        "DEF-104: if PortAudio already enumerates the device, restarting is "
        "pointless churn — the normal mic switch will pick it up"
    )


def test_def104_no_restart_for_lower_priority_device():
    from heyvox.audio.device_handle import detect_missed_hotplug
    # A live-but-uncached device ranking BELOW the current mic is not an upgrade.
    missed = detect_missed_hotplug(
        live_input_names={"jabra link 390", "g435"},
        pa_input_names={"g435"},
        mic_priority=["G435", "Jabra Link 390"],
        current_dev_name="G435",
    )
    assert missed is None, (
        "DEF-104: only a HIGHER-priority missing device justifies a restart; "
        "a lower-ranked one must not spin the daemon"
    )


def test_def104_no_false_fire_when_coreaudio_unavailable():
    from heyvox.audio.device_handle import detect_missed_hotplug
    # Empty live set = CoreAudio unavailable. Must degrade to no-op.
    missed = detect_missed_hotplug(
        live_input_names=set(),
        pa_input_names={"macbook pro microphone"},
        mic_priority=["Jabra Link 390", "MacBook Pro Microphone"],
        current_dev_name="MacBook Pro Microphone",
    )
    assert missed is None, (
        "DEF-104: with no CoreAudio data the detector must never fire — a "
        "restart on missing input would be a self-inflicted outage"
    )


def test_def104_detects_when_no_current_device():
    from heyvox.audio.device_handle import detect_missed_hotplug
    # No mic in use (None) → any live-but-uncached priority device is an upgrade.
    missed = detect_missed_hotplug(
        live_input_names={"jabra link 390"},
        pa_input_names=set(),
        mic_priority=["Jabra Link 390"],
        current_dev_name=None,
    )
    assert missed == "Jabra Link 390"


def test_def104_substring_match_like_find_best_mic():
    from heyvox.audio.device_handle import detect_missed_hotplug
    # priority entries are substrings; the real device name may be longer.
    missed = detect_missed_hotplug(
        live_input_names={"jabra link 390 (hands-free)"},
        pa_input_names=set(),
        mic_priority=["Jabra Link 390"],
        current_dev_name="MacBook Pro Microphone",
    )
    assert missed == "Jabra Link 390", (
        "DEF-104: detection must use the same substring match as find_best_mic "
        "so the two agree on what counts as present"
    )


# ---------------------------------------------------------------------------
# DEF-104 extension (2026-07-05): the CoreAudio *default input* is a self-heal
# candidate even when it isn't in mic_priority. macOS makes a freshly-plugged
# USB headset the default input; the priority-only scan missed it (the G435
# incident — daemon stuck on the built-in fallback, manual pins hit the stale
# PortAudio cache, no self-heal because G435 wasn't a priority candidate).
# ---------------------------------------------------------------------------

def test_def104_default_input_not_in_priority_still_heals():
    from heyvox.audio.device_handle import detect_missed_hotplug
    # The exact G435 case: live in CoreAudio + the macOS default input, but
    # absent from PortAudio's cache and NOT in mic_priority. Must self-heal.
    missed = detect_missed_hotplug(
        live_input_names={"g435 wireless gaming headset", "macbook pro microphone"},
        pa_input_names={"macbook pro microphone"},
        mic_priority=["MacBook Pro Microphone"],  # G435 deliberately not listed
        current_dev_name="MacBook Pro Microphone",
        default_input_name="G435 Wireless Gaming Headset",
    )
    assert missed == "G435 Wireless Gaming Headset", (
        "DEF-104: an actively-used mic macOS set as the default input must "
        "self-heal even when it isn't in mic_priority"
    )


def test_def104_default_input_already_current_no_restart():
    from heyvox.audio.device_handle import detect_missed_hotplug
    # The default input is already the mic in use → nothing to heal, no churn.
    missed = detect_missed_hotplug(
        live_input_names={"g435 wireless gaming headset"},
        pa_input_names={"macbook pro microphone"},
        mic_priority=["MacBook Pro Microphone"],
        current_dev_name="G435 Wireless Gaming Headset",
        default_input_name="G435 Wireless Gaming Headset",
    )
    assert missed is None, (
        "DEF-104: if the daemon is already on the default-input device, a "
        "restart is pointless — must not fire"
    )


def test_def104_default_input_visible_to_portaudio_no_restart():
    from heyvox.audio.device_handle import detect_missed_hotplug
    # PortAudio already enumerates the default input → the normal scan/switch
    # picks it up; restarting would be churn.
    missed = detect_missed_hotplug(
        live_input_names={"g435 wireless gaming headset", "macbook pro microphone"},
        pa_input_names={"g435 wireless gaming headset", "macbook pro microphone"},
        mic_priority=["MacBook Pro Microphone"],
        current_dev_name="MacBook Pro Microphone",
        default_input_name="G435 Wireless Gaming Headset",
    )
    assert missed is None, (
        "DEF-104: a default input PortAudio can already see is handled by the "
        "normal mic switch — no restart"
    )


def test_def104_priority_wins_over_default_input():
    from heyvox.audio.device_handle import detect_missed_hotplug
    # Both a priority device and the default input are missed. The priority
    # device must win (it's the explicit user preference).
    missed = detect_missed_hotplug(
        live_input_names={
            "jabra link 390",
            "g435 wireless gaming headset",
            "macbook pro microphone",
        },
        pa_input_names={"macbook pro microphone"},
        mic_priority=["Jabra Link 390", "MacBook Pro Microphone"],
        current_dev_name="MacBook Pro Microphone",
        default_input_name="G435 Wireless Gaming Headset",
    )
    assert missed == "Jabra Link 390", (
        "DEF-104: an explicit mic_priority hotplug outranks the default-input "
        "fallback"
    )


def test_def104_default_input_heals_with_empty_priority():
    from heyvox.audio.device_handle import detect_missed_hotplug
    # Empty/None priority list must not short-circuit the default-input path.
    missed = detect_missed_hotplug(
        live_input_names={"g435 wireless gaming headset"},
        pa_input_names=set(),
        mic_priority=[],
        current_dev_name="MacBook Pro Microphone",
        default_input_name="G435 Wireless Gaming Headset",
    )
    assert missed == "G435 Wireless Gaming Headset", (
        "DEF-104: an empty mic_priority must still allow the default-input "
        "self-heal (the early-return now gates on CoreAudio data, not priority)"
    )


def test_def104_default_input_no_false_fire_coreaudio_unavailable():
    from heyvox.audio.device_handle import detect_missed_hotplug
    # Empty live set = CoreAudio down. Even with a default input passed, no-op.
    missed = detect_missed_hotplug(
        live_input_names=set(),
        pa_input_names={"macbook pro microphone"},
        mic_priority=["MacBook Pro Microphone"],
        current_dev_name="MacBook Pro Microphone",
        default_input_name="G435 Wireless Gaming Headset",
    )
    assert missed is None, (
        "DEF-104: no CoreAudio data → never fire, even for a named default input"
    )


def test_def104_empty_or_none_priority_is_noop():
    from heyvox.audio.device_handle import detect_missed_hotplug
    assert detect_missed_hotplug({"x"}, set(), [], None) is None
    assert detect_missed_hotplug({"x"}, set(), None, None) is None


def test_def104_main_loop_wires_hotplug_check():
    # File-text inspection (no pyaudio import): the main loop must call the
    # restart helper gated on min age, and the helper must use the live-vs-cached
    # signal and the MIC_HOTPLUG_MISSED tag.
    import os
    import heyvox
    src = open(os.path.join(os.path.dirname(heyvox.__file__), "main.py")).read()
    assert "_maybe_restart_for_hotplug(" in src, (
        "DEF-104: main loop must invoke the hotplug self-restart helper"
    )
    assert "_HOTPLUG_MIN_AGE" in src, (
        "DEF-104: restart must be gated on minimum daemon age to let BT HFP settle"
    )
    assert "get_live_input_device_names" in src
    assert "detect_missed_hotplug" in src
    assert "MIC_HOTPLUG_MISSED" in src
    assert "_write_hotplug_marker" in src, (
        "DEF-104: a cooldown marker must be written to prevent restart loops"
    )


def test_def104_manual_usb_cache_miss_bypasses_bt_hfp_probe():
    # File-text inspection (no pyaudio import): when the user manually selects a
    # USB/Lightspeed mic that CoreAudio already sees but PortAudio's cached list
    # does not, DeviceManager must request the DEF-104 restart directly. Treating
    # that state as "likely BT A2DP" causes repeated HFP probes and visible
    # fallback to the built-in mic before the later periodic hotplug check fires.
    import os
    import heyvox
    src = open(os.path.join(os.path.dirname(heyvox.__file__), "device_manager.py")).read()
    assert "_is_coreaudio_live_portaudio_miss" in src, (
        "DEF-104: manual mic switch must distinguish USB PA-cache misses from "
        "Bluetooth A2DP no-input states"
    )
    assert "get_live_input_device_names" in src, (
        "DEF-104: manual cache-miss detection must consult live CoreAudio input names"
    )
    assert "get_bluetooth_input_device_names" in src, (
        "DEF-147: manual cache-miss detection must exclude Bluetooth before restart"
    )
    manual_start = src.find("if requested_name:")
    manual_end = src.find("# Check if the default output device changed", manual_start)
    manual_block = src[manual_start:manual_end]
    hfp_idx = manual_block.find("triggering HFP switch")
    restart_idx = manual_block.find("_request_hotplug_restart")
    assert manual_start != -1 and manual_end != -1
    assert restart_idx != -1 and hfp_idx != -1
    assert restart_idx < hfp_idx, (
        "DEF-104: manual USB/Lightspeed cache miss must request restart before "
        "falling through to the Bluetooth HFP probe"
    )


def test_def104_main_loop_honors_manual_hotplug_restart_request():
    import os
    import heyvox
    src = open(os.path.join(os.path.dirname(heyvox.__file__), "main.py")).read()
    assert "pop_hotplug_restart_request" in src, (
        "DEF-104: DeviceManager's manual hotplug restart request must be consumed"
    )
    assert "_restart_for_hotplug_candidate(" in src, (
        "DEF-104: manual cache-miss request must reuse the guarded restart helper"
    )
    request_idx = src.find("pop_hotplug_restart_request")
    restart_idx = src.find("_restart_for_hotplug_candidate(", request_idx)
    assert request_idx != -1 and restart_idx != -1 and request_idx < restart_idx


# ---------------------------------------------------------------------------
# DEF-101: per-mic software capture gain (BT-HFP G435 low-level workaround)
#
# The macOS input slider is decoupled from the Bluetooth-HFP codec gain, so a
# BT headset (G435) captures at a very low level the slider cannot raise. The
# fix is a per-mic `gain` in the active profile, applied on the capture hot
# path. The `gain` config field existed long before this but was never applied
# (dead config) — these guards ensure it actually multiplies the signal, stays
# int16, hard-clips instead of wrapping, and no-ops cheaply when unset.
# ---------------------------------------------------------------------------

def test_def101_apply_input_gain_scales_signal():
    import numpy as np
    from heyvox.audio.normalize import apply_input_gain
    src = np.array([100, -200, 300], dtype=np.int16)
    out = apply_input_gain(src, 4.0)
    assert out.dtype == np.int16, "gain output must stay int16 for openwakeword"
    assert list(out) == [400, -800, 1200], "gain must be a flat multiplier"


def test_def101_apply_input_gain_clips_no_wraparound():
    import numpy as np
    from heyvox.audio.normalize import apply_input_gain
    # 20000 * 4 = 80000 would wrap to a negative int16 without clamping.
    out = apply_input_gain(np.array([20000, -20000], dtype=np.int16), 4.0)
    assert out.dtype == np.int16
    assert int(out[0]) == 32767 and int(out[1]) == -32768, (
        "DEF-101: gain must hard-clip to int16 range, never wrap around"
    )


def test_def101_apply_input_gain_noop_when_unset():
    import numpy as np
    from heyvox.audio.normalize import apply_input_gain
    src = np.frombuffer(b"\x01\x00\x02\x00", dtype=np.int16)  # read-only view
    # None / 1.0 / <=0 must return the SAME object (zero copy on the hot path).
    assert apply_input_gain(src, None) is src
    assert apply_input_gain(src, 1.0) is src
    assert apply_input_gain(src, 0.0) is src
    assert apply_input_gain(src, -2.0) is src


def test_def101_main_loop_applies_profile_gain():
    # File-text inspection (main.py imports pyaudio): the capture loop must
    # apply the active profile's gain right after np.frombuffer.
    import os
    import heyvox
    src = open(os.path.join(os.path.dirname(heyvox.__file__), "main.py")).read()
    assert "apply_input_gain" in src, "DEF-101: capture loop must call apply_input_gain"
    assert "devices.active_profile.gain" in src, (
        "DEF-101: gain must come from the active mic profile so it switches "
        "with the device"
    )


def test_def101_gain_field_wired_through_profile_manager():
    # MicProfileEntryConfig.gain was dead config before DEF-101 — ensure the
    # profile manager copies it into the resolved entry and substring-matches.
    import pathlib
    import tempfile
    from heyvox.audio.profile import MicProfileManager
    from heyvox.config import MicProfileEntryConfig
    mgr = MicProfileManager(
        {"g435 bluetooth": MicProfileEntryConfig(gain=4.0)},
        pathlib.Path(tempfile.mkdtemp()),
    )
    assert mgr.get_profile("G435 Bluetooth Gaming Headset").gain == 4.0, (
        "DEF-101: per-mic gain must resolve through MicProfileManager.get_profile"
    )
    # A device without a matching profile gets no gain (None → no-op).
    assert mgr.get_profile("MacBook Pro Microphone").gain is None
    # The Lightspeed variant must NOT match the "G435 Bluetooth" key (no gain →
    # no clipping on the healthy USB level).
    assert mgr.get_profile("G435 Wireless Gaming Headset").gain is None


# ---------------------------------------------------------------------------
# DEF-147: DEF-104 hotplug self-restart must NEVER fire for a Bluetooth mic.
#
# A BT-HFP device is chronically "live in CoreAudio but absent from PortAudio"
# as it flaps A2DP<->HFP, so the DEF-104 detector misread it as a USB hotplug
# and restarted the daemon — each restart tearing the fragile SCO link apart
# (real regression: G435 over BT died repeatedly, stable the moment HeyVox was
# stopped). These guards ensure the BT-transport exclusion exists and is
# consulted before the restart.
# ---------------------------------------------------------------------------

def test_def147_bluetooth_helper_returns_set():
    # Must import and return a set even with no BT hardware (graceful
    # degradation — empty set, never raises).
    pytest.importorskip("pyaudio")
    from heyvox.audio.bt import get_bluetooth_input_device_names
    assert isinstance(get_bluetooth_input_device_names(), set)


def test_def147_enumerate_triples_dont_break_live_dead_helpers():
    # _enumerate_coreaudio_inputs now yields (name, alive, transport); the
    # live/dead helpers must still unpack without ValueError.
    pytest.importorskip("pyaudio")
    from heyvox.audio.mic import (
        get_live_input_device_names,
        get_dead_input_device_names,
    )
    assert isinstance(get_live_input_device_names(), set)
    assert isinstance(get_dead_input_device_names(), set)


def test_def147_main_loop_excludes_bluetooth_before_restart():
    # File-text inspection: the hotplug helper must consult the BT set and skip
    # the restart for BT devices BEFORE writing the marker / calling execv.
    import os
    import heyvox
    src = open(os.path.join(os.path.dirname(heyvox.__file__), "main.py")).read()
    assert "get_bluetooth_input_device_names" in src, (
        "DEF-147: hotplug restart must consult the BT-transport set"
    )
    bt_idx = src.find("is a Bluetooth device")
    marker_idx = src.find("_write_hotplug_marker(missed)")
    assert bt_idx != -1 and marker_idx != -1 and bt_idx < marker_idx, (
        "DEF-147: the BT exclusion must short-circuit BEFORE the restart marker/execv"
    )


# ---------------------------------------------------------------------------
# DEF-148: output keep-alive for USB power-saving headsets (G535/Lightspeed).
#
# USB wireless headsets park the output path after silence; the next stream's
# cold start (~0.7s) swallows short cues. A silent output stream held open keeps
# the device awake → cues play immediately. MUST be gated on USB transport
# (irrelevant on built-in/BT/virtual) and stopped on cleanup.
# ---------------------------------------------------------------------------

def test_def148_keepalive_wired_and_usb_gated():
    pytest.importorskip("pyaudio")
    from heyvox.config import AudioConfig
    from heyvox.audio.keepalive import default_output_is_usb, default_output_transport
    assert AudioConfig().output_keepalive is True, "keep-alive defaults on"
    assert isinstance(default_output_transport(), int)
    assert isinstance(default_output_is_usb(), bool)
    import os
    import heyvox
    src = open(os.path.join(os.path.dirname(heyvox.__file__), "main.py")).read()
    assert "OutputKeepAlive(" in src, "main loop must construct the keep-alive"
    assert "config.audio.output_keepalive" in src, "keep-alive must be config-gated"
    assert "_ka.stop()" in src, "keep-alive must be stopped in cleanup"


def test_def148_keepalive_opens_one_stream_and_closes(monkeypatch):
    pytest.importorskip("pyaudio")
    from heyvox.audio.keepalive import OutputKeepAlive
    events = []

    class FakeStream:
        def start_stream(self):
            events.append("start")

        def stop_stream(self):
            events.append("stop")

        def close(self):
            events.append("close")

    class FakePA:
        def open(self, **kw):
            events.append("open")
            return FakeStream()

    ka = OutputKeepAlive(lambda m: None)
    ka._pa = FakePA()  # inject context (DEF-153: keep-alive owns its own PA)
    ka._open_stream()
    ka._open_stream()  # idempotent — must NOT open a second stream
    assert events.count("open") == 1, "DEF-148: only one silent stream at a time"
    ka._close_stream()
    assert "close" in events, "DEF-148: stream must be released on close"


# ---------------------------------------------------------------------------
# DEF-153: keep-alive wedged forever on a stale PA context after a USB flap.
# The G535 power-cycled → CoreAudio assigned a new device ID → the PA context
# captured at daemon start failed every reopen with -9986/-10851 (every 5 s,
# no recovery), cues silently fell back to afplay — exactly the path the
# device swallows (DEF-148). The keep-alive must drop its own context on an
# open failure and recover with a freshly created one on the next tick.
# ---------------------------------------------------------------------------

def test_def153_keepalive_recreates_pa_context_after_open_failure(monkeypatch):
    pytest.importorskip("pyaudio")
    import pyaudio
    from heyvox.audio.keepalive import OutputKeepAlive

    created = []

    class FakeStream:
        def start_stream(self):
            pass

        def stop_stream(self):
            pass

        def close(self):
            pass

    class FakePA:
        def __init__(self):
            self.stale = len(created) == 0  # first context: stale after USB flap
            self.terminated = False
            created.append(self)

        def open(self, **kw):
            if self.stale:
                raise OSError(-9986, "Internal PortAudio error")
            return FakeStream()

        def terminate(self):
            self.terminated = True

    monkeypatch.setattr(pyaudio, "PyAudio", FakePA)
    ka = OutputKeepAlive(lambda m: None)
    ka._open_stream()  # stale context → open fails
    assert ka._stream is None
    assert created[0].terminated, "DEF-153: stale PA context must be dropped on failure"
    ka._open_stream()  # next tick → fresh context succeeds
    assert ka._stream is not None, "DEF-153: fresh context must recover the stream"
    assert len(created) == 2, "DEF-153: a new PA context must be created after the drop"
    ka.stop()
    assert created[1].terminated, "DEF-153: own context must be released on stop"


# ---------------------------------------------------------------------------
# DEF-214: the DEF-153 open-failure counter only ever logged to
# herald-debug.log — "no audio, why?" required reading the log after the
# fact. A real (multi-attempt) stall must surface a HUD banner, escalate to
# "error" at the 2-minute DEF-104 mark, and clear itself on recovery.
# ---------------------------------------------------------------------------

def test_def214_output_down_banner_fires_after_threshold_and_clears_on_recovery(monkeypatch):
    pytest.importorskip("pyaudio")
    import pyaudio
    from heyvox.audio import keepalive as ka_mod
    from heyvox.audio.keepalive import OutputKeepAlive
    from heyvox.hud.surface import HUDSurface

    class FakeStream:
        def start_stream(self):
            pass

        def stop_stream(self):
            pass

        def close(self):
            pass

    state = {"fail": True}

    class FakePA:
        def __init__(self):
            self.terminated = False

        def open(self, **kw):
            if state["fail"]:
                raise OSError(-9986, "Internal PortAudio error")
            return FakeStream()

        def terminate(self):
            self.terminated = True

    monkeypatch.setattr(pyaudio, "PyAudio", FakePA)
    banners = []
    monkeypatch.setattr(HUDSurface, "banner", staticmethod(
        lambda level, source, text, ttl_secs=60.0: banners.append((level, source, text))
    ))
    cleared = []
    monkeypatch.setattr(HUDSurface, "clear", staticmethod(lambda source: cleared.append(source)))

    # _open_stream() creates its own PA context lazily (self._pa is None at
    # __init__) and drops it via _drop_pa() on every failure — so failure is
    # driven by the shared `state` dict, not a per-instance flag, matching
    # the DEF-153 test's created-order approach above.
    ka = OutputKeepAlive(lambda m: None)

    # Below threshold: no banner yet.
    for _ in range(ka_mod._OUTPUT_DOWN_WARN_THRESHOLD - 1):
        ka._open_stream()
    assert not banners, "DEF-214: must not warn before the threshold (skip single blips)"

    # Threshold reached: warn banner fires exactly once.
    ka._open_stream()
    assert banners == [("warn", "output-down", "Audio output stalled — recovering automatically")]

    # Escalation to 24 failures: error-level banner, and the pre-existing
    # DEF-104 stale signal must still fire unchanged.
    for _ in range(24 - ka_mod._OUTPUT_DOWN_WARN_THRESHOLD):
        ka._open_stream()
    assert ka.stale.is_set(), "DEF-104: escalation flag must still fire"
    assert banners[-1] == (
        "error", "output-down", "Audio output down 2+ min — restarting automatically"
    )

    # Recovery: fresh context succeeds → banner cleared.
    state["fail"] = False
    ka._open_stream()
    assert ka._stream is not None
    assert "output-down" in cleared, "DEF-214: banner must clear on recovery"


def test_def214_stop_clears_lingering_banner(monkeypatch):
    pytest.importorskip("pyaudio")
    from heyvox.audio.keepalive import OutputKeepAlive
    from heyvox.hud.surface import HUDSurface

    cleared = []
    monkeypatch.setattr(HUDSurface, "clear", staticmethod(lambda source: cleared.append(source)))
    ka = OutputKeepAlive(lambda m: None)
    ka.stop()
    assert "output-down" in cleared, "DEF-214: stop() must not leave a stale banner behind"


def test_def215_hud_send_retries_once_on_stale_socket():
    """DEF-215: hud_send() must retry a message once immediately if send()
    reports failure due to a stale-but-not-yet-detected connection (e.g. the
    HUD overlay process having just restarted) — otherwise the first message
    post-restart, often the "state" transition the overlay's audio-level
    rendering gates on, is silently lost until the NEXT hud_send() call
    notices `_sock is None`.
    """
    src = _read_main_py()
    start = src.index("def hud_send(msg: dict) -> None:")
    end = src.index("\n    # Initialize STT backend", start)
    body = src[start:end]
    assert "ok = ctx.hud_client.send(msg)" in body, (
        "DEF-215: hud_send must capture send()'s success/failure return value"
    )
    assert "if not ok and ctx.hud_client._sock is None:" in body, (
        "DEF-215: hud_send must detect a send that failed due to a stale socket"
    )
    assert body.count("ctx.hud_client.send(msg)") >= 2, (
        "DEF-215: hud_send must retry send(msg) after reconnecting on a stale socket"
    )
    assert "if ok:" in body, (
        "DEF-215: the Sent-log line must only fire when the send actually succeeded"
    )


# ---------------------------------------------------------------------------
# Stop-gate quick-win 2026-06-11: ultra-confidence bypass + idle-only
# speaker multiplier
#
# Log evidence 06-09/10: 78 high-confidence stop frames blocked by the
# DEF-117/118 pre-silence gates, 60 of them >= 0.999 (53 at a flat 1.000),
# while setups exist where the VAD never reports a silent frame
# (last_silent=3033s observed) — every pre_silence-dependent lever is dead
# there. Documented mid-sentence phoneme flares reach 0.982 (DEF-117) and
# 0.997 (DEF-118), so the bypass bar must sit above BOTH.
# ---------------------------------------------------------------------------


def test_ultra_fast_stop_constant_above_documented_flares():
    """_ULTRA_CONFIDENCE_FAST_STOP must exist and clear every documented
    mid-sentence flare (0.982 DEF-117, 0.997 DEF-118) while staying below
    1.0 so flat-1.000 real peaks pass."""
    src = _read_main_py()
    m = re.search(r"_ULTRA_CONFIDENCE_FAST_STOP\s*=\s*([\d.]+)", src)
    assert m, (
        "_ULTRA_CONFIDENCE_FAST_STOP constant missing — the no-pre-silence "
        "bypass is the only stop path that works when the VAD never reports "
        "silence (all DEF-096-B/117/118 levers dead)."
    )
    val = float(m.group(1))
    assert 0.998 <= val < 1.0, (
        f"_ULTRA_CONFIDENCE_FAST_STOP={val} outside [0.998, 1.0). Below "
        f"0.998 admits documented phoneme flares (0.997, DEF-118) → "
        f"mid-sentence false stops that ABORT AND SEND half a prompt; at "
        f"1.0 even flat-peak real stops would need exact saturation."
    )


def test_ultra_fast_stop_bypasses_silence_gate_but_keeps_vad_guard():
    """_ultra_stop must NOT require _recent_silence (that bypass is its
    purpose) but MUST keep `not _vad_silent` (DEF-047: dead-stream silence
    bursts must not self-stop a recording)."""
    src = _read_main_py()
    m = re.search(r"_ultra_stop\s*=\s*\(([\s\S]+?)\n\s*\)", src)
    assert m is not None, "Could not find _ultra_stop assignment in main.py"
    block = m.group(1)
    assert "_recent_silence" not in block, (
        "_ultra_stop must not depend on _recent_silence — it exists exactly "
        "for setups where the VAD never reports silence."
    )
    assert "not _vad_silent" in block, (
        "_ultra_stop must keep the DEF-047 `not _vad_silent` guard."
    )
    assert "_ULTRA_CONFIDENCE_FAST_STOP" in block, (
        "_ultra_stop must gate on _ULTRA_CONFIDENCE_FAST_STOP."
    )


def test_ultra_fast_stop_has_distinct_stop_path_label():
    """[STOP_PATH] must label ultra-bypass stops distinctly ("ultra") so the
    FP rate of the new gate bypass is measurable from logs alone — without
    it, a false-stop regression is unattributable (P-detector-without-action
    in reverse)."""
    src = _read_main_py()
    assert '"ultra" if _ultra_stop' in src, (
        "STOP_PATH label for the ultra path missing — path=ultra counts are "
        "the direct measure of the bypass's benefit AND its FP risk."
    )


def test_near_miss_fast_blocked_excludes_ultra_band():
    """NEAR_MISS_FAST_BLOCKED must only tag scores that are actually still
    blocked (< _ULTRA_CONFIDENCE_FAST_STOP) — tagging fired ultra-stops as
    'blocked' would corrupt the gate-effectiveness metric the DEF-149
    tuning relies on."""
    src = _read_main_py()
    m = re.search(
        r"if\s*\(([\s\S]+?)\):\s*\n[\s\S]{0,400}?NEAR_MISS_FAST_BLOCKED", src
    )
    assert m is not None, "Could not find the NEAR_MISS_FAST_BLOCKED guard"
    assert "_ULTRA_CONFIDENCE_FAST_STOP" in m.group(1), (
        "The NEAR_MISS_FAST_BLOCKED condition must exclude scores >= "
        "_ULTRA_CONFIDENCE_FAST_STOP (those fire via the ultra path now)."
    )


def test_speaker_mult_applies_idle_only():
    """speaker_threshold_multiplier must not apply while recording: no TTS
    plays during a recording (media paused, TTS held behind RECORDING_FLAG),
    so the echo self-trigger it defends against can't hit the stop path —
    while the 1.4x penalty demonstrably killed real stops (2026-06-10 20:09
    score=0.999 vs thr=0.91)."""
    src = _read_main_py()
    m = re.search(r"_speaker_mult\s*=\s*\(([\s\S]+?)\n\s*\)", src)
    assert m is not None, "Could not find _speaker_mult assignment in main.py"
    assert "not _is_rec" in m.group(1), (
        "_speaker_mult must include `and not _is_rec` — the multiplier is an "
        "idle-time echo defense; applying it to the stop path raises the "
        "stop threshold to 0.91 in speaker mode for no protective benefit."
    )


def test_default_wake_word_loadable_on_fresh_install():
    """DEF-159: the shipped default wake word must load on a fresh pip install
    with no custom .onnx present. The old default `hey_vox` crashed
    `heyvox start` with `ValueError: Could not find pretrained model for model
    name 'hey_vox'` because that model ships nowhere yet (not bundled in the
    wheel, no setup download — plan 14-04 unbuilt). A shipped default must be
    an openwakeword-bundled name OR a package-bundled .onnx — never a bare
    name openwakeword can't resolve. Re-add 'hey_vox' only once its model is
    bundled/downloaded by setup."""
    import os

    import heyvox
    from heyvox.config import WakeWordConfig

    # openwakeword's bundled pretrained model names (no .onnx suffix needed)
    oww_bundled = {
        "alexa_v0.1", "hey_jarvis_v0.1", "hey_mycroft_v0.1",
        "hey_rhasspy_v0.1", "timer_v0.1", "weather_v0.1",
    }
    pkg_models_dir = os.path.join(os.path.dirname(heyvox.__file__), "models")

    def _resolvable(name: str) -> bool:
        if not name:
            return True  # empty stop is resolved to start by the validator
        if name in oww_bundled:
            return True
        return os.path.exists(os.path.join(pkg_models_dir, f"{name}.onnx"))

    cfg = WakeWordConfig()
    defaults = [cfg.start, cfg.stop, *cfg.also_load]
    unresolvable = [n for n in defaults if not _resolvable(n)]
    assert not unresolvable, (
        f"DEF-159: shipped default wake words {unresolvable} cannot load on a "
        f"fresh install — not openwakeword-bundled and no .onnx in "
        f"{pkg_models_dir}. openwakeword raises ValueError on unknown names. "
        f"Bundle the model or keep the default to a bundled name."
    )


def test_def164_preload_model_refreshes_idle_timer_when_already_loaded():
    """DEF-164: preload_model() used to no-op when the model was already
    loaded, leaving a stale idle-unload timer (armed by the *previous*
    transcription) ticking down unaffected by the new recording. If that
    recording ran long enough to cross the old deadline, the model got
    evicted mid-recording (_mlx_transcribing is False until
    transcribe_audio() actually runs) — forcing an un-hidden cold reload
    right after the user stopped talking. Confirmed twice in production
    logs on 2026-07-02 (unloads at 17:02:58 and 09:31:20, both firing
    after a new recording had already started). Fix: the already-loaded
    path must also refresh _mlx_last_use and reschedule the timer."""
    import time
    from unittest.mock import patch

    from heyvox.audio import stt

    saved_loaded = stt._mlx_loaded.is_set()
    saved_last_use = stt._mlx_last_use
    try:
        stt._mlx_loaded.set()
        stale_last_use = time.time() - 250  # armed by a use 250s ago
        stt._mlx_last_use = stale_last_use

        with patch.object(stt, "_schedule_unload") as mock_schedule:
            stt.preload_model()

        assert stt._mlx_last_use > stale_last_use, (
            "DEF-164: preload_model() must refresh _mlx_last_use when the "
            "model is already loaded, or a timer armed by the previous "
            "transcription can evict it mid-recording."
        )
        mock_schedule.assert_called_once()
    finally:
        if not saved_loaded:
            stt._mlx_loaded.clear()
        stt._mlx_last_use = saved_last_use


def test_def164_preload_model_still_spawns_load_when_not_loaded():
    """DEF-164 counterpart: the not-yet-loaded path must be untouched by
    the fix above — preload_model() should still kick off a background
    load (the normal cold-start case, which was never buggy)."""
    from unittest.mock import patch

    from heyvox.audio import stt

    saved_loaded = stt._mlx_loaded.is_set()
    try:
        stt._mlx_loaded.clear()
        with patch.object(stt.threading, "Thread") as mock_thread:
            stt.preload_model()
        mock_thread.assert_called_once()
        assert mock_thread.call_args.kwargs.get("target") == stt._load_mlx_model
    finally:
        if saved_loaded:
            stt._mlx_loaded.set()
        else:
            stt._mlx_loaded.clear()


def test_def191_ratio_guard_keeps_coherent_slow_stt():
    """DEF-191: the catastrophic-ratio guard (stt/audio > 0.6 on >=5s) must NOT
    discard coherent dictation that was merely slow (system load / quiet mic /
    GPU contention). A real thrash-hallucination still has a degenerate shape
    (low word diversity or very few words) and stays caught."""
    from heyvox.text_processing import is_garbled

    coherent = (
        "Lies bitte meine Konversation die letzten mit Andrew "
        "und hilf mir eine gute Frage"
    )
    # Slow (ratio 0.66) but coherent -> KEPT (the regression this fixes).
    assert is_garbled(coherent, stt_secs=11.4, audio_secs=17.4) is False
    # No timing supplied -> also kept (unchanged behaviour).
    assert is_garbled(coherent) is False
    # Real thrash loop + slow -> still discarded (guard still works).
    assert is_garbled(
        "doc doc doc doc doc doc doc doc doc doc", stt_secs=8.6, audio_secs=13.0
    ) is True
    # Grey zone: passes text checks (uniq 0.44) but not coherent + slow -> discarded.
    assert is_garbled(
        "the cat the cat the dog the dog run", stt_secs=9.0, audio_secs=13.0
    ) is True
    # Short output + slow -> discarded.
    assert is_garbled("k nud so", stt_secs=6.0, audio_secs=11.0) is True


# ---------------------------------------------------------------------------
# DEF-193: torch is dead weight in the Kokoro daemon — thinc eager-imports it
# (via misaki.en -> spacy for English g2p) but the MLX path never uses it.
# torch_suppressor blocks it in-process. Guard the suppressor mechanics so a
# refactor can't silently stop blocking torch and re-inflate the daemon ~230MB.
# ---------------------------------------------------------------------------

def _run_torch_suppressor_probe(env_extra=None):
    """Run the suppressor in a fresh interpreter and report its effect.

    A subprocess is required: the suppressor mutates process-global state
    (sys.meta_path + importlib.metadata.entry_points) that would leak into
    other tests. Uses the finder's own find_spec() so the assertion holds
    whether or not torch is actually installed in the test env (CI has none).
    """
    code = (
        "import sys\n"
        "from heyvox.herald.torch_suppressor import "
        "install_torch_suppressor, _TorchBlockingFinder\n"
        "active = install_torch_suppressor()\n"
        "finder = next((f for f in sys.meta_path "
        "if isinstance(f, _TorchBlockingFinder)), None)\n"
        "blocks = False\n"
        "if finder is not None:\n"
        "    try:\n"
        "        finder.find_spec('torch', None)\n"
        "    except ModuleNotFoundError:\n"
        "        blocks = True\n"
        "passes = finder.find_spec('json', None) is None if finder else False\n"
        "import importlib.metadata as md\n"
        "filtered = getattr(md.entry_points, '_kokoro_filtered', False)\n"
        "print(f'active={active} blocks={blocks} passes={passes} filtered={filtered}')\n"
    )
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    out = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )
    assert out.returncode == 0, f"probe failed: {out.stderr}"
    return out.stdout.strip()


def test_def193_torch_suppressor_blocks_torch_and_hides_curated():
    """Default: torch import blocked, non-torch deferred, curated eps hidden."""
    result = _run_torch_suppressor_probe()
    assert "active=True" in result, result
    assert "blocks=True" in result, result   # import torch -> ModuleNotFoundError
    assert "passes=True" in result, result   # non-torch names still resolve
    assert "filtered=True" in result, result  # entry_points wrapper installed


def test_def193_torch_suppressor_opt_out_via_env():
    """KOKORO_ALLOW_TORCH=1 disables suppression entirely (no finder installed)."""
    result = _run_torch_suppressor_probe({"KOKORO_ALLOW_TORCH": "1"})
    assert "active=False" in result, result
    assert "blocks=False" in result, result
    assert "filtered=False" in result, result


# ---------------------------------------------------------------------------
# DEF-194: TTS spoke raw markdown ("asterisk"/"backtick"/"backslash") from
# prose spliced between literal <tts> mentions (backtick-wrapped) whenever a
# response discussed the TTS mechanism. Strip code spans before matching;
# scrub markup from the final speech.
# ---------------------------------------------------------------------------

def test_def194_extraction_ignores_literal_tts_in_backticks():
    """A response mentioning `<tts>` literally (in backticks) plus a real
    block must extract ONLY the real block — no prose fragment with markdown."""
    from heyvox.herald.worker import HeraldWorker

    w = HeraldWorker.__new__(HeraldWorker)
    resp = (
        "Der Worker nutzt `^<tts>(.*?)</tts>` mit **DOTALL**.\n"
        "Meine `<tts>`-Blöcke sind `immer` Englisch.\n\n"
        "<tts>This is the real English speech.</tts>"
    )
    assert w._extract_tts_blocks(resp) == ["This is the real English speech."]


def test_def194_multiple_real_inline_blocks_not_regressed():
    """Genuine inline multi-block extraction must still work (no backticks)."""
    from heyvox.herald.worker import HeraldWorker

    w = HeraldWorker.__new__(HeraldWorker)
    assert w._extract_tts_blocks(
        "<tts>Hello</tts> other <tts>World</tts>"
    ) == ["Hello", "World"]


def test_def194_strip_speech_markup_removes_spoken_symbols():
    """Markdown/escape marks are removed so TTS never voices them literally."""
    from heyvox.herald.tts_helpers import strip_speech_markup

    out = strip_speech_markup("say **bold** and `code`, path a\\b, # heading")
    for ch in "*`\\#":
        assert ch not in out, (ch, out)
    assert "bold" in out and "code" in out and "heading" in out


# ---------------------------------------------------------------------------
# DEF-216: the DEF-096-A VAD-transition model reset must be debounced.
#
# A user repeating the stop word after a miss creates several speech→silence
# transitions in quick succession; an undebounced reset on each transition
# wipes the wake-word feature buffer faster than it can accumulate (~0.8s
# needed), pinning the live score at 0.00 — the retry pattern itself defeated
# the retry. Offline clip replay verified: resets ≤0.5s apart → 0.00 peak,
# ≥0.8s apart → 0.998. The fix gates the transition reset behind the same
# _last_model_reset clock the DEF-096-C periodic reset uses.
# ---------------------------------------------------------------------------


def test_def216_vad_transition_reset_is_debounced():
    import os
    import re
    import heyvox
    src = open(os.path.join(os.path.dirname(heyvox.__file__), "main.py")).read()
    # Find the DEF-096-A transition block and assert the reset inside it is
    # guarded by the shared _MODEL_RESET_INTERVAL debounce.
    m = re.search(
        r"if _vad_silent and not _was_vad_silent:\n(.*?)_was_vad_silent = _vad_silent",
        src,
        re.DOTALL,
    )
    assert m, "DEF-096-A transition-reset block not found in main.py"
    block = m.group(1)
    assert "model.reset()" in block, (
        "transition block no longer resets the model — if lever A was removed "
        "on purpose, retire this guard consciously"
    )
    guard_idx = block.find("_MODEL_RESET_INTERVAL")
    reset_idx = block.find("model.reset()")
    assert guard_idx != -1 and guard_idx < reset_idx, (
        "DEF-216: the DEF-096-A transition reset must check "
        "_last_model_reset/_MODEL_RESET_INTERVAL BEFORE calling model.reset() "
        "— undebounced per-transition resets zero out stop-word detection "
        "when the user repeats the wake word"
    )


def test_def216_reset_interval_at_least_800ms():
    """The clip-replay evidence: resets ≥0.8s apart keep detection at 0.998.
    Guard the shared interval against being lowered below that floor."""
    import os
    import re
    import heyvox
    src = open(os.path.join(os.path.dirname(heyvox.__file__), "main.py")).read()
    m = re.search(r"_MODEL_RESET_INTERVAL = ([0-9.]+)", src)
    assert m, "_MODEL_RESET_INTERVAL definition not found"
    assert float(m.group(1)) >= 0.8, (
        f"_MODEL_RESET_INTERVAL={m.group(1)} — below the 0.8s floor the "
        f"DEF-216 clip replay established as safe for stop-word detection"
    )
