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

Two-phase + DRY RUN by default: phase 1 transcribes every clip with no
filesystem changes; then the positives-quarantine rate is checked against a
hard brake BEFORE anything moves. Nothing moves unless ``--apply`` is passed
AND the rate is under the brake (or ``--force``). This exists because running
the naive one-pass version on the real corpus quarantined ~34% of positives,
~40% of them REAL wake words Whisper mis-heard -- see the score gate below.

Quarantine-only: this module NEVER deletes a file. A positive-dir clip is
quarantined only if Whisper finds no wake word AND it is not a trusted
high-confidence trigger (``_positive_should_quarantine`` / ``_TRUST_SCORE``):
a clip that fired at a high model score is a positive by the model's own
judgment, and a garble-prone STT must not overrule it (that is DEF-167's
evidence-free relabeling, inverted). A negative-dir clip that Whisper finds
DOES contain the wake word (a genuine miss -- the evidence-based fn-recovery
that replaces the deleted heuristic relabelers, DEF-167) moves to
``positives/`` (<=2.5s) or ``quarantine/`` (longer). Every move is recorded in
a timestamped, reversible manifest matching the ``recfp_cleanup_manifest_*``
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
import re
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

# A positive-dir clip whose model trigger score (parsed from the filename,
# e.g. ..._score1.00.wav) is at or above this is TRUSTED: it fired at high
# confidence, so a garble-prone STT must never overrule the model and
# quarantine it. Running the gate on real data showed Whisper mis-hears the
# made-up word "vox" as bucks/folks/books/Bob/Halevox/bare-"vox" often enough
# that blanket Whisper-gating of high-score triggers quarantined ~34% of
# positives, ~40% of them REAL wake words -- that is DEF-167's evidence-free
# relabeling, inverted (the score is the stronger signal here, not the STT).
# Only low/no-confidence positives (the fn_start-style retroactive garbage,
# scored in the 0.1-0.7 TN range) are eligible for Whisper quarantine.
_TRUST_SCORE = 0.8

# HARD BRAKE: if more than this fraction of checked positives would be
# quarantined, refuse to move anything without --force. A spike this size is
# almost always Whisper mis-hearing real wake words, not bad data -- surface
# it and stop, never silently shrink the positives set.
_MAX_SAFE_QUARANTINE_RATE = 0.15

_DEFAULT_STATE_DIR = Path("~/.config/heyvox/training/.gate_state/").expanduser()

# Model trigger score embedded in collector filenames as the trailing
# "_score<float>.wav" field. Curated clips (e.g. training/recordings/) carry
# no score and are always trusted.
_SCORE_RE = re.compile(r"_score([0-9]+(?:\.[0-9]+)?)\.wav$", re.IGNORECASE)


def _parse_score(name: str) -> float | None:
    """Return the model trigger score from a collector filename, or None."""
    m = _SCORE_RE.search(name)
    return float(m.group(1)) if m else None


def _is_garbage(text: str) -> bool:
    """True for a transcript that is unambiguous garbage: empty/silence, a
    known Whisper spam/hallucination phrase, or a long degenerate repeat loop.

    Deliberately conservative on repeats: a short repeat like 'hey vox hey vox'
    is a legit double-utterance and must NOT be flagged. Only long loops
    (>=8 tokens, <=1/4 unique, e.g. 'and go and go and go ...') qualify.
    """
    t = (text or "").strip()
    if not t:
        return True
    if _HALLUCINATION_RE.search(t):
        return True
    words = t.lower().split()
    return len(words) >= 8 and len(set(words)) <= max(1, len(words) // 4)


def _positive_should_quarantine(text: str, has_ww: bool, score: float | None) -> bool:
    """A positive is AUTO-quarantined ONLY when it is not a trusted high-score
    trigger AND its transcript is clear garbage (empty/silent or a long
    hallucination loop). A low-score clip that merely lacks a clean wake-word
    transcription is NOT auto-removed: Whisper failing to read 'hey vox' on a
    quiet/far/noisy clip does not prove it isn't the wake word -- that is
    exactly the hard case, and the most valuable positive to keep. Those are
    left for manual by-ear review (tools/review_clips.py). Clips with no
    parseable score (curated recordings) are always trusted. has_ww kept for
    signature clarity; a has_ww clip is never garbage anyway.
    """
    if score is None or score >= _TRUST_SCORE:
        return False
    return _is_garbage(text)


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
    apply: bool = False,
    force: bool = False,
) -> dict:
    """Two-phase batch Whisper quality gate. Returns a summary dict.

    Phase 1 transcribes every clip (resumable write-ahead, NO filesystem
    changes). Then the positives-quarantine rate is computed over the FULL
    corpus and checked against a hard brake BEFORE anything moves. Moves run
    only when apply=True AND (the rate is under the brake OR force=True).
    Default (apply=False) is a DRY RUN: it reports what would move and touches
    nothing. Positives are score-gated (a high-confidence trigger is never
    quarantined on Whisper's say-so). Never deletes -- every move is a
    quarantine/recovery recorded in a reversible manifest.
    """
    if state_dir is None:
        state_dir = _DEFAULT_STATE_DIR
    state_dir.mkdir(parents=True, exist_ok=True)
    quarantine_dir.mkdir(parents=True, exist_ok=True)

    done = load_done(state_dir)
    _recover_stalled_inflight(state_dir, done)

    # ---- Phase 1: verdicts only (transcribe, NO moves) ----
    for side, dirs in (("positive", positive_dirs), ("negative", negative_dirs)):
        print(f"=== Quality gate verdict pass: {side} dirs ===", file=sys.stderr)
        for clip in _gather_wavs(dirs):
            abs_path = str(clip.resolve())
            if abs_path in done:
                continue
            score = _parse_score(clip.name)
            # Perf: a trusted high-confidence (or curated, score-less) positive
            # can NEVER be quarantined -- the score gate keeps it regardless of
            # the transcript -- so skip the expensive ~3.7s transcription
            # entirely. This alone skips every tp/ and recordings/ clip.
            if side == "positive" and (score is None or score >= _TRUST_SCORE):
                _append_result(state_dir, {
                    "path": abs_path, "side": side, "has_ww": False,
                    "score": score, "dur": 0.0, "timeout": False,
                    "text": "", "trusted": True,
                })
                continue
            # Perf: RMS pre-gate in the PARENT, before spawning any subprocess.
            try:
                audio, sr = _load_wav(clip)
                rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
            except Exception as e:  # noqa: BLE001
                print(f"WARNING: failed to load {clip.name}: {e}", file=sys.stderr)
                _append_result(state_dir, {
                    "path": abs_path, "side": side, "verdict": "error",
                    "has_ww": False, "score": _parse_score(clip.name),
                    "dur": 0.0, "timeout": False, "text": "",
                })
                continue
            dur = len(audio) / sr if sr else 0.0
            if rms < _SILENCE_RMS:
                text, has_ww, timed_out = "", False, False
            else:
                text, has_ww, timed_out = _verdict_for_clip(clip, state_dir)
            _append_result(state_dir, {
                "path": abs_path, "side": side, "has_ww": has_ww,
                "score": _parse_score(clip.name), "dur": round(dur, 3),
                "timeout": timed_out, "text": text,
            })

    # ---- Read the FULL results.jsonl and compute pending moves (resume-safe:
    # moves are derived from every verdict ever recorded, so a kill/resume mid
    # verdict-pass never applies over a partial corpus -- apply only runs when a
    # verdict pass reaches this point without being killed) ----
    records: list[dict] = []
    rp = _results_path(state_dir)
    if rp.exists():
        for line in rp.read_text().splitlines():
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except Exception:  # noqa: BLE001
                    pass

    pending: list[tuple[dict, Path, str]] = []  # (record, dest_dir, moved_to)
    pos_total = neg_total = timeouts = errors = 0
    for r in records:
        if r.get("verdict") == "error":
            errors += 1
            continue
        if r.get("timeout"):
            timeouts += 1
        side = r.get("side")
        if side == "positive":
            pos_total += 1
            if _positive_should_quarantine(r.get("text", ""), bool(r.get("has_ww")), r.get("score")):
                pending.append((r, quarantine_dir, "quarantine"))
        elif side == "negative":
            neg_total += 1
            if r.get("has_ww") and not r.get("timeout"):
                dur = float(r.get("dur") or 0.0)
                if dur <= _RECOVERY_MAX_SECS:
                    pending.append((r, positives_dir, "positives"))
                else:
                    pending.append((r, quarantine_dir, "quarantine"))

    would_quarantine = sum(1 for r, _d, m in pending
                           if m == "quarantine" and r["side"] == "positive")
    would_recover = sum(1 for r, _d, _m in pending if r["side"] == "negative")
    pos_quar_rate = (would_quarantine / pos_total) if pos_total else 0.0
    brake_tripped = pos_quar_rate > _MAX_SAFE_QUARANTINE_RATE
    do_apply = apply and (force or not brake_tripped)

    print(f"positives checked={pos_total}, would-quarantine={would_quarantine} "
          f"({pos_quar_rate:.1%}); negatives checked={neg_total}, "
          f"would-recover={would_recover}; timeouts={timeouts} errors={errors}",
          file=sys.stderr)
    if brake_tripped:
        print(f"HARD BRAKE: positives quarantine rate {pos_quar_rate:.1%} exceeds "
              f"{_MAX_SAFE_QUARANTINE_RATE:.0%}. Refusing to move without --force -- "
              "a spike this size is usually Whisper mis-hearing real wake words, "
              "not bad data. Review the would-quarantine set first.", file=sys.stderr)
    if not apply:
        print("DRY RUN (default): nothing moved. Re-run with --apply to execute "
              "the moves above (still subject to the hard brake).", file=sys.stderr)

    manifest: list[dict] = []
    manifest_path = None
    if do_apply:
        for r, dest, moved_to in pending:
            src = Path(r["path"])
            if not src.exists():
                continue
            new_path = _safe_dest(dest, src.name)
            try:
                new_path.parent.mkdir(parents=True, exist_ok=True)
                src.rename(new_path)
            except OSError as e:
                print(f"WARNING: failed to move {src} -> {new_path}: {e}", file=sys.stderr)
                continue
            manifest.append({
                "from": str(src), "to": str(new_path), "side": r["side"],
                "moved_to": moved_to, "text": r.get("text", ""),
                "score": r.get("score"), "dur": r.get("dur"),
            })
        timestamp = time.strftime("%Y%m%d_%H%M%S")
        manifest_path = state_dir / f"manifest_{timestamp}.json"
        manifest_path.write_text(json.dumps(manifest, indent=2))

    summary = {
        "positives_checked": pos_total,
        "negatives_checked": neg_total,
        "would_quarantine": would_quarantine,
        "would_recover": would_recover,
        "positives_quarantine_rate": round(pos_quar_rate, 4),
        "brake_tripped": brake_tripped,
        "applied": do_apply,
        "moves_made": len(manifest),
        "timeouts": timeouts,
        "errors": errors,
        "manifest_path": str(manifest_path) if manifest_path else None,
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
        "--apply", action="store_true",
        help="Actually move clips (quarantine/recover). Default is a DRY RUN "
             "that only reports what would move.",
    )
    ap.add_argument(
        "--force", action="store_true",
        help="With --apply, move even if the positives quarantine rate trips "
             f"the {_MAX_SAFE_QUARANTINE_RATE:.0%} hard brake. Use only after "
             "reviewing the dry-run output.",
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
    summary = run_gate(
        cpf.POSITIVE_DIRS, cpf.HARD_NEGATIVE_DIRS,
        positives_dir, quarantine_dir, state_dir=args.state_dir,
        apply=args.apply, force=args.force,
    )
    # Non-zero exit when the brake tripped and moves were NOT forced, so a
    # caller/pipeline can detect "gate wants review" rather than silently pass.
    return 1 if (summary["brake_tripped"] and not summary["applied"]) else 0


if __name__ == "__main__":
    sys.exit(main())
