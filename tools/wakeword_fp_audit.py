#!/usr/bin/env python3
"""
Retroactive False-Positive Audit for HeyVox tp_start clips.

Background: every wake word trigger writes a 2s "tp_start" clip to
~/.config/heyvox/training/tp/ *before* recording begins. If that clip is
actually background noise (no speech), the trigger was a false positive
that slipped through the live capture path because the subsequent recording
either stopped early, was low-energy, or had an empty/garbled STT result
(all 4 leak paths silently return without reclassifying).

This tool re-scores every tp_start clip against acoustic speech features
and moves suspected FPs to training/fp_suspect/ for manual review.

Calibration (from live data — known FP vs TP buckets):
  speech_frac: FPs median 6%, TPs median 18%  ← primary discriminator
  rms:         FPs median 349, TPs median 835  ← secondary
  peak/zcr:    too overlapped to use (wake-word fricatives push ZCR up)

Flag rule (default, "balanced"):
  1. speech_frac < 0.10                         → flag (mostly silence)
  2. speech_frac < 0.15 AND rms < 450           → flag (weak + quiet)

Strict mode (--strict) widens the catchment: 0.12 / 0.18 / 550.

Usage:
    python3 tools/wakeword_fp_audit.py               # dry-run, balanced
    python3 tools/wakeword_fp_audit.py --strict      # tighter thresholds
    python3 tools/wakeword_fp_audit.py --move        # relocate flagged
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

import numpy as np
import soundfile as sf

TRAINING = Path.home() / ".config/heyvox/training"
TP_DIR = TRAINING / "tp"
FP_SUSPECT = TRAINING / "fp_suspect"

FRAME_MS = 20
RMS_SPEECH = 400.0      # per-frame energy considered "voiced"

# Default (balanced) thresholds — calibrated against live FP/TP corpus.
THRESHOLDS_DEFAULT = {
    "speech_hard": 0.10,   # below this → flag regardless
    "speech_soft": 0.15,   # below this AND low rms → flag
    "rms_soft":    450.0,
}
# Strict: widen catchment, risks a few real-TP mis-flags.
THRESHOLDS_STRICT = {
    "speech_hard": 0.12,
    "speech_soft": 0.18,
    "rms_soft":    550.0,
}


def clip_features(path: Path) -> dict:
    audio, sr = sf.read(str(path), dtype="int16")
    if audio.ndim > 1:
        audio = audio[:, 0]
    if audio.size == 0:
        return {"err": "empty"}

    a = audio.astype(np.float32)
    rms = float(np.sqrt(np.mean(a * a)))
    peak = float(np.abs(a).max())

    hop = int(sr * FRAME_MS / 1000)
    if hop > 0 and len(a) >= hop:
        n_frames = len(a) // hop
        frames = a[: n_frames * hop].reshape(n_frames, hop)
        frame_rms = np.sqrt(np.mean(frames * frames, axis=1))
        speech_frac = float(np.mean(frame_rms > RMS_SPEECH))
    else:
        speech_frac = 0.0

    zc = int(np.sum(np.diff(np.sign(a)) != 0))
    zcr = zc / max(len(a) - 1, 1)

    return {
        "rms": rms, "peak": peak, "speech_frac": speech_frac, "zcr": zcr,
        "sr": sr, "n_samples": len(a),
    }


def flag_reason(f: dict, t: dict) -> str | None:
    if "err" in f:
        return "empty-file"
    if f["speech_frac"] < t["speech_hard"]:
        return "mostly-silence"
    if f["speech_frac"] < t["speech_soft"] and f["rms"] < t["rms_soft"]:
        return "weak-and-quiet"
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--move", action="store_true",
                    help="move flagged clips to fp_suspect/ (default: dry-run)")
    ap.add_argument("--strict", action="store_true",
                    help="use stricter thresholds (more clips flagged)")
    ap.add_argument("--limit", type=int, default=0,
                    help="audit only the first N clips (0 = all)")
    args = ap.parse_args()

    if not TP_DIR.exists():
        print(f"error: {TP_DIR} does not exist", file=sys.stderr)
        return 2

    t = THRESHOLDS_STRICT if args.strict else THRESHOLDS_DEFAULT
    mode = "strict" if args.strict else "balanced"

    clips = sorted(TP_DIR.glob("*.wav"))
    if args.limit:
        clips = clips[: args.limit]
    if not clips:
        print("no tp/ clips to audit")
        return 0

    print(f"Auditing {len(clips)} clips in {TP_DIR}")
    print(f"Mode: {mode}  "
          f"(speech_hard={t['speech_hard']:.2f}, "
          f"speech_soft={t['speech_soft']:.2f}, rms_soft={t['rms_soft']:.0f})")
    print()

    flagged: list[tuple[Path, dict, str]] = []
    for c in clips:
        try:
            f = clip_features(c)
        except Exception as e:
            print(f"  SKIP  {c.name:60s} read-error: {e}")
            continue
        reason = flag_reason(f, t)
        if reason:
            flagged.append((c, f, reason))

    print(f"{'name':<70s} {'rms':>6s} {'spch':>5s} {'zcr':>5s}  reason")
    print("-" * 115)
    for c, f, reason in flagged[:60]:
        if "err" in f:
            print(f"{c.name:<70s}  {reason}")
            continue
        print(f"{c.name:<70s} {f['rms']:>6.0f} "
              f"{f['speech_frac']:>5.0%} {f['zcr']:>5.2f}  {reason}")
    if len(flagged) > 60:
        print(f"  ... and {len(flagged) - 60} more")

    print()
    print("=== Summary ===")
    print(f"  Total tp/ clips:        {len(clips)}")
    print(f"  Suspected FPs:          {len(flagged)}  "
          f"({len(flagged) / len(clips):.1%} of corpus)")
    by_reason: dict[str, int] = {}
    for _, _, r in flagged:
        by_reason[r] = by_reason.get(r, 0) + 1
    for r, n in sorted(by_reason.items(), key=lambda kv: -kv[1]):
        print(f"    {r:<20s} {n}")

    if args.move and flagged:
        FP_SUSPECT.mkdir(parents=True, exist_ok=True)
        moved = 0
        for c, f, reason in flagged:
            new_name = c.name.replace("tp_", f"fp_suspect-{reason}_", 1)
            dst = FP_SUSPECT / new_name
            try:
                shutil.move(str(c), str(dst))
                moved += 1
            except OSError as e:
                print(f"  move-error {c.name}: {e}")
        print(f"\n  Moved {moved} clips → {FP_SUSPECT}")
    elif flagged:
        print(f"\n  (dry-run — pass --move to relocate to {FP_SUSPECT})")

    return 0


if __name__ == "__main__":
    sys.exit(main())
