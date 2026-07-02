"""Mandatory batch Whisper quality gate for wake-word training data.

Single chokepoint that audits every training-data source directory (both
positive and negative sides) BEFORE featurization, using the same lenient
wake-word matcher already proven in ``tools/whisper_sanity_pass.py``
(imported here, not duplicated -- one source of truth for the regex).

Absorbs two prior one-off scripts into a permanent, resumable step:
  - ``tools/whisper_sanity_pass.py``'s ``_WW_RE`` / ``_HALLUCINATION_RE``
    matcher and ``_load_wav`` helper (imported, not copied).
  - ``.planning/quick/.../REFERENCE-fp_check_step0.py``'s robust/resumable
    driver: RMS silence pre-gate, write-ahead JSONL, per-clip decode
    params tuned against DEF-075/083 hallucination-loop failure modes.

Quarantine-only: this module NEVER deletes a file. A positive-dir clip
that Whisper finds contains no wake word moves to ``quarantine/``. A
negative-dir clip that Whisper finds DOES contain the wake word (a
genuine miss -- the evidence-based fn-recovery mechanism that replaces
the deleted heuristic relabelers, DEF-167) moves to ``positives/``
(short clips) or ``quarantine/`` (long clips, likely contain more than
just the wake word). Every move is recorded in a timestamped, reversible
manifest matching the existing ``recfp_cleanup_manifest_*.json``
precedent.

Incremental / resumable across runs: state (results.jsonl, inflight.txt)
lives under a persistent --state-dir (default
``~/.config/heyvox/training/.gate_state/``) that is NOT wiped between
invocations. A clip's absolute path, once it appears in results.jsonl,
is skipped on every subsequent run -- so only the FIRST full run over
the whole corpus pays the ~1-2 hour Whisper transcription cost; later
runs only process clips collected since the previous run. A clip that
gets moved (quarantine/recovery) leaves its original directory entirely,
so it is naturally never re-seen by a later glob; a clip that stays in
place is skipped purely by already having a results.jsonl entry for its
path.

A per-clip hard timeout (subprocess.run(timeout=...), SIGKILL on expiry)
is enforced by THIS module itself -- unlike REFERENCE-fp_check_step0.py,
which relied on being run under an external `timeout`/`gtimeout` wrapper.
A Python thread cannot interrupt a stuck native MLX/Metal call; a
subprocess can be killed by the OS.

Perf: the RMS silence check runs in the PARENT process (cheap, no mlx
import) BEFORE deciding whether to spawn the per-clip transcription
subprocess. A cold transcription subprocess costs ~3.7s; silent clips
skip that cost entirely and are verdicted has_ww=False without ever
spawning a worker.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from whisper_sanity_pass import (  # noqa: E402
    _WW_RE,  # noqa: F401 -- re-exported for callers that want the raw regex
    _HALLUCINATION_RE,  # noqa: F401 -- re-exported, same reason
    _contains_wake_word,
    _load_wav,
)

# Below this RMS (int16-equivalent, normalized) a clip is effectively
# silence/hum -- Whisper with a forced language loops on silence, and
# silence can't contain a wake word, so skip transcription entirely.
_SILENCE_RMS = 150.0 / 32768.0

# Positives-vs-quarantine split point for negative-dir clips that DO
# contain the wake word: short clips are almost certainly just the wake
# word (safe to promote to positives/); longer clips likely contain more
# than the wake word alone and go to quarantine/ for manual review.
_RECOVERY_MAX_SECS = 2.5

# Per-clip hard timeout, enforced via subprocess.run(timeout=...). Generous
# for a single 2-5s clip on Metal, short enough that one stuck clip can't
# stall a multi-hundred-clip run for more than ~7 minutes worst case.
_CLIP_TIMEOUT_SECS = 20

# Warn (not hard-fail) if more than this fraction of clips in a single run
# get quarantined -- guards against Whisper false-quarantining real
# positives. The gate must never silently shrink the positives set; a
# spike is surfaced, not swallowed.
_QUARANTINE_RATE_WARN_THRESHOLD = 0.15

_DEFAULT_STATE_DIR = Path("~/.config/heyvox/training/.gate_state/").expanduser()


# ---------------------------------------------------------------------------
# Per-clip transcription with a self-enforced hard timeout
# ---------------------------------------------------------------------------

def _transcribe_worker(wav_path: str) -> None:
    """Standalone worker entry point: load a wav, transcribe it, print JSON.

    Invoked via ``python3 tools/quality_gate.py --_worker <wav_path>`` as a
    subprocess so the parent can SIGKILL it on timeout (a Python thread
    cannot interrupt a stuck native MLX/Metal call).
    """
    path = Path(wav_path)
    audio, sr = _load_wav(path)
    rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
    if rms < _SILENCE_RMS:
        print(json.dumps({"text": "", "silent": True}))
        return

    import mlx_whisper

    result = mlx_whisper.transcribe(
        audio,
        path_or_hf_repo="mlx-community/whisper-large-v3-turbo",
        language="en",
        condition_on_previous_text=False,  # DEF-075: don't amplify loops
        compression_ratio_threshold=2.2,   # DEF-075: bail from degenerate decode
        logprob_threshold=-0.8,            # DEF-075
        no_speech_threshold=0.6,
        word_timestamps=False,
    )
    text = (result.get("text") or "").strip()
    print(json.dumps({"text": text}))


def transcribe_with_timeout(wav_path: Path) -> tuple[str, bool]:
    """Run the worker in a subprocess with a hard timeout.

    Returns (text, timed_out). On timeout, returns ("", True). On any
    other non-zero exit or unparseable output, logs a warning to stderr
    (never silently swallowed) and returns ("", False).
    """
    try:
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "--_worker", str(wav_path)],
            capture_output=True,
            text=True,
            timeout=_CLIP_TIMEOUT_SECS,
        )
    except subprocess.TimeoutExpired:
        return "", True

    if proc.returncode != 0:
        print(
            f"WARNING: worker exited {proc.returncode} for {wav_path.name}: "
            f"{proc.stderr.strip()[-300:]}",
            file=sys.stderr,
        )
        return "", False

    stdout = proc.stdout.strip()
    if not stdout:
        print(f"WARNING: worker produced no output for {wav_path.name}", file=sys.stderr)
        return "", False

    last_line = stdout.splitlines()[-1]
    try:
        record = json.loads(last_line)
    except json.JSONDecodeError:
        print(
            f"WARNING: worker output unparseable for {wav_path.name}: {last_line[:200]!r}",
            file=sys.stderr,
        )
        return "", False

    return record.get("text", ""), False


# ---------------------------------------------------------------------------
# Resumable write-ahead JSONL driver
# ---------------------------------------------------------------------------

def _results_path(state_dir: Path) -> Path:
    return state_dir / "results.jsonl"


def _inflight_path(state_dir: Path) -> Path:
    return state_dir / "inflight.txt"


def load_done(state_dir: Path) -> set[str]:
    """Read results.jsonl into a set of already-processed absolute clip paths."""
    done: set[str] = set()
    results = _results_path(state_dir)
    if results.exists():
        for line in results.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                done.add(json.loads(line)["path"])
            except Exception:  # noqa: BLE001
                pass
    return done


def _append_result(state_dir: Path, record: dict) -> None:
    with _results_path(state_dir).open("a") as f:
        f.write(json.dumps(record) + "\n")
        f.flush()


def _recover_stalled_inflight(state_dir: Path, done: set[str]) -> None:
    """Recover a clip that was mid-decode when a prior run was killed."""
    inflight = _inflight_path(state_dir)
    if not inflight.exists():
        return
    stalled = inflight.read_text().strip()
    if stalled and stalled not in done:
        _append_result(state_dir, {"path": stalled, "verdict": "timeout", "moved": False})
        done.add(stalled)
        print(f"  recovered: skipped stalled clip {stalled}", file=sys.stderr)
    inflight.unlink()


# ---------------------------------------------------------------------------
# Gather + verdict + move logic
# ---------------------------------------------------------------------------

def _gather_wavs(dirs: list[Path]) -> list[Path]:
    """Collect .wav files from any subset of dirs that actually exist.

    Missing dirs are skipped with a log line, never an error -- matches
    tools/collect_personal_features.py's gather() tolerance.
    """
    all_paths: list[Path] = []
    for d in dirs:
        if not d.is_dir():
            print(f"  skip (missing): {d}", file=sys.stderr)
            continue
        paths = sorted(d.glob("*.wav"))
        print(f"  {d.name}: {len(paths)} clips  ({d})", file=sys.stderr)
        all_paths.extend(paths)
    return all_paths


def _safe_dest(dest_dir: Path, name: str) -> Path:
    """Return a collision-free destination path inside dest_dir.

    Path.rename() silently overwrites an existing target on POSIX -- which
    would violate this module's never-delete guarantee if two clips ever
    share a filename across source dirs (rare given the collector's
    category+timestamp+score naming, but possible, and a silent overwrite
    is an unrecoverable loss). If dest_dir/name is free, return it;
    otherwise insert _dup1, _dup2, ... before the suffix until a free
    path is found.
    """
    candidate = dest_dir / name
    if not candidate.exists():
        return candidate
    stem, suffix = candidate.stem, candidate.suffix
    i = 1
    while True:
        alt = dest_dir / f"{stem}_dup{i}{suffix}"
        if not alt.exists():
            return alt
        i += 1


def _verdict_for_clip(clip: Path, state_dir: Path) -> tuple[str, bool, bool]:
    """Transcribe (with resumable write-ahead) and return (text, has_ww, timed_out)."""
    abs_path = str(clip.resolve())
    inflight = _inflight_path(state_dir)
    inflight.write_text(abs_path)
    with inflight.open("a") as f:
        f.flush()

    text, timed_out = transcribe_with_timeout(clip)
    has_ww = False if timed_out else _contains_wake_word(text)

    if inflight.exists():
        inflight.unlink()
    return text, has_ww, timed_out


def run_gate(
    positive_dirs: list[Path],
    negative_dirs: list[Path],
    positives_dir: Path,
    quarantine_dir: Path,
    *,
    state_dir: Path | None = None,
) -> dict:
    """Run the mandatory batch Whisper quality gate. Returns a summary dict.

    Never deletes a file -- quarantine-only moves, all recorded in a
    reversible, timestamped manifest.
    """
    if state_dir is None:
        state_dir = _DEFAULT_STATE_DIR
    state_dir.mkdir(parents=True, exist_ok=True)
    quarantine_dir.mkdir(parents=True, exist_ok=True)

    done = load_done(state_dir)
    _recover_stalled_inflight(state_dir, done)

    manifest: list[dict] = []
    positives_checked = 0
    negatives_checked = 0
    quarantined = 0
    recovered_to_positives = 0
    timeouts = 0
    errors = 0

    print("=== Quality gate: positive dirs ===", file=sys.stderr)
    for clip in _gather_wavs(positive_dirs):
        abs_path = str(clip.resolve())
        if abs_path in done:
            continue
        positives_checked += 1

        # Perf: RMS pre-gate in the PARENT process, before spawning any
        # transcription subprocess -- a silent clip never pays the ~3.7s
        # cold-transcribe cost.
        try:
            audio, _sr = _load_wav(clip)
            rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
        except Exception as e:  # noqa: BLE001
            print(f"WARNING: failed to load {clip.name}: {e}", file=sys.stderr)
            errors += 1
            _append_result(state_dir, {"path": abs_path, "verdict": "error", "moved": False})
            continue

        if rms < _SILENCE_RMS:
            text, has_ww, timed_out = "", False, False
        else:
            text, has_ww, timed_out = _verdict_for_clip(clip, state_dir)
            if timed_out:
                timeouts += 1

        _append_result(state_dir, {
            "path": abs_path, "verdict": "positive-checked",
            "has_ww": has_ww, "timeout": timed_out, "text": text,
            "moved": not has_ww,
        })

        if not has_ww:
            new_path = _safe_dest(quarantine_dir, clip.name)
            try:
                clip.rename(new_path)
            except OSError as e:
                print(f"WARNING: failed to move {clip} -> {new_path}: {e}", file=sys.stderr)
                continue
            quarantined += 1
            manifest.append({
                "from": str(clip), "to": str(new_path), "side": "positive",
                "text": text, "reason": "no-wake-word-in-positive-dir",
            })

    print("=== Quality gate: negative dirs ===", file=sys.stderr)
    for clip in _gather_wavs(negative_dirs):
        abs_path = str(clip.resolve())
        if abs_path in done:
            continue
        negatives_checked += 1

        try:
            audio, sr = _load_wav(clip)
            rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
        except Exception as e:  # noqa: BLE001
            print(f"WARNING: failed to load {clip.name}: {e}", file=sys.stderr)
            errors += 1
            _append_result(state_dir, {"path": abs_path, "verdict": "error", "moved": False})
            continue

        if rms < _SILENCE_RMS:
            text, has_ww, timed_out = "", False, False
        else:
            text, has_ww, timed_out = _verdict_for_clip(clip, state_dir)
            if timed_out:
                timeouts += 1

        _append_result(state_dir, {
            "path": abs_path, "verdict": "negative-checked",
            "has_ww": has_ww, "timeout": timed_out, "text": text,
            "moved": has_ww,
        })

        if has_ww:
            duration = len(audio) / sr if sr else 0.0
            if duration <= _RECOVERY_MAX_SECS:
                new_path = _safe_dest(positives_dir, clip.name)
                moved_to = "positives"
            else:
                new_path = _safe_dest(quarantine_dir, clip.name)
                moved_to = "quarantine"
            try:
                new_path.parent.mkdir(parents=True, exist_ok=True)
                clip.rename(new_path)
            except OSError as e:
                print(f"WARNING: failed to move {clip} -> {new_path}: {e}", file=sys.stderr)
                continue
            if moved_to == "positives":
                recovered_to_positives += 1
            else:
                quarantined += 1
            manifest.append({
                "from": str(clip), "to": str(new_path), "side": "negative",
                "text": text, "reason": "wake-word-in-negative-dir",
                "duration": round(duration, 2), "moved_to": moved_to,
            })

    total = positives_checked + negatives_checked
    quarantine_rate = (quarantined / total) if total else 0.0
    if quarantine_rate > _QUARANTINE_RATE_WARN_THRESHOLD:
        print(
            f"WARNING: quarantine_rate={quarantine_rate:.1%} exceeds "
            f"{_QUARANTINE_RATE_WARN_THRESHOLD:.0%} threshold this run -- "
            "possible Whisper false-quarantine spike, review the manifest "
            "before trusting the shrunk positives set.",
            file=sys.stderr,
        )

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    manifest_path = state_dir / f"manifest_{timestamp}.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    summary = {
        "positives_checked": positives_checked,
        "negatives_checked": negatives_checked,
        "quarantined": quarantined,
        "recovered_to_positives": recovered_to_positives,
        "timeouts": timeouts,
        "errors": errors,
        "quarantine_rate": round(quarantine_rate, 4),
        "manifest_path": str(manifest_path),
    }
    print(json.dumps(summary, indent=2))
    return summary


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--state-dir", type=Path, default=_DEFAULT_STATE_DIR,
        help=f"Persistent resumable state dir (default: {_DEFAULT_STATE_DIR})",
    )
    ap.add_argument(
        "--_worker", metavar="WAV_PATH", default=None,
        help=argparse.SUPPRESS,  # internal, not user-facing
    )
    args = ap.parse_args()

    if args._worker is not None:
        _transcribe_worker(args._worker)
        return 0

    # Deferred import to avoid a circular import: collect_personal_features.py
    # imports run_gate FROM this module (Task 3), so this module must not
    # import collect_personal_features.py at module level.
    import collect_personal_features as cpf

    positives_dir = cpf._expand("~/.config/heyvox/training/positives")
    quarantine_dir = cpf._expand("~/.config/heyvox/training/quarantine")
    run_gate(
        cpf.POSITIVE_DIRS, cpf.HARD_NEGATIVE_DIRS,
        positives_dir, quarantine_dir, state_dir=args.state_dir,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
