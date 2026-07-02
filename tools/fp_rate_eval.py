#!/usr/bin/env python3
"""Fast false-positive / true-positive rate evaluator over a LARGE corpus.

Companion to training/evaluate_model.py (the D-16 ship gate). That script
re-loads the model and re-scans every clip once per threshold (6x work) and
reads only WAV via the stdlib `wave` module. For a multi-hour negative corpus
(LibriSpeech / MUSAN), both are showstoppers.

This script:
  * loads the model once,
  * scores each clip in a SINGLE streaming pass and keeps the max score,
  * applies every threshold to the cached max-scores (sweep is free),
  * reads wav AND flac via soundfile, recursing into subdirs,
  * reports FP count + FP/hour and (optional) TP rate per threshold.

Local-vs-remote retrain/eval boundary: retrain itself runs on Colab (remote
GPU, tools/retrain_heyvox_v8.py or equivalent). This script runs LOCALLY --
it needs both the LibriSpeech/negative-corpus WAV files AND the downloaded
.onnx model artifact on local disk, neither of which exist inside the Colab
notebook's ephemeral environment. "Run fp_rate_eval after every retrain" is
therefore a documented manual local post-download step in the retrain
workflow, NOT a CI hook and NOT something that runs automatically inside the
Colab notebook. Every run appends one record to --history-file so the
threshold-sweep results of every retrain are auditable over time without
re-running the corpus scan.

Usage:
    python3 tools/fp_rate_eval.py \
        --model ~/.config/heyvox/models/hey_vox.onnx \
        --negatives /tmp/LibriSpeech/dev-clean \
        --positives /tmp/heyvox-baseline/pos_friends \
        [--limit-neg 0] [--wake-name hey_vox] [--history-file PATH]
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from datetime import datetime, timezone

import numpy as np

try:
    import soundfile as sf
except ImportError:
    print("ERROR: soundfile required (pip install soundfile)", file=sys.stderr)
    sys.exit(2)

TARGET_SR = 16000
FRAME = 1280  # openwakeword native 80ms frame @ 16kHz
THRESHOLDS = [0.5, 0.6, 0.7, 0.8, 0.9, 0.95]
TP_GATE = 0.70          # D-16: detection rate must clear this
FP_PER_HOUR_GATE = 1.0  # D-16: false positives per hour must stay below this


def _load_int16_16k(path: str):
    """Read any wav/flac as mono int16 @ 16 kHz. Returns (samples, seconds)."""
    audio, sr = sf.read(path, dtype="int16", always_2d=False)
    if audio.ndim > 1:
        audio = audio[:, 0]  # take channel 0
    seconds = len(audio) / sr if sr else 0.0
    if sr != TARGET_SR and len(audio):
        # Linear-interp resample. Crude but adequate for a negative corpus;
        # heyvox clips are already 16k so this only fires on odd inputs.
        x = audio.astype(np.float32)
        n_new = max(1, int(round(len(x) * TARGET_SR / sr)))
        x = np.interp(
            np.linspace(0.0, 1.0, n_new, endpoint=False),
            np.linspace(0.0, 1.0, len(x), endpoint=False),
            x,
        )
        audio = x.astype(np.int16)
    return audio, seconds


def _max_score(model, audio, wake: str) -> float:
    """Stream a clip through the model, return its peak wake score."""
    model.reset()  # clear streaming buffer so clips don't bleed into each other
    peak = 0.0
    for i in range(0, len(audio) - FRAME + 1, FRAME):
        scores = model.predict(audio[i : i + FRAME])
        s = float(scores.get(wake, 0.0))
        if s > peak:
            peak = s
    return peak


def _collect(model, files, wake, label):
    """Return (max_scores list, total_seconds) for a file list."""
    scores, total_s = [], 0.0
    n = len(files)
    for idx, f in enumerate(files):
        try:
            audio, secs = _load_int16_16k(f)
        except Exception as e:
            print(f"  skip {os.path.basename(f)}: {e}", file=sys.stderr)
            continue
        total_s += secs
        if len(audio) >= FRAME:
            scores.append(_max_score(model, audio, wake))
        if (idx + 1) % 250 == 0:
            print(f"  {label}: {idx + 1}/{n} scored ({total_s / 3600:.2f}h)")
    return scores, total_s


def main() -> int:
    p = argparse.ArgumentParser(description="Fast FP/TP rate evaluator (large corpus)")
    p.add_argument("--model", required=True)
    p.add_argument("--negatives", required=True, help="dir of negative wav/flac (recursive)")
    p.add_argument("--positives", default="", help="optional dir of positive wav/flac (recursive)")
    p.add_argument("--wake-name", default="", help="defaults to model filename stem")
    p.add_argument("--limit-neg", type=int, default=0, help="cap negative files (0 = all)")
    p.add_argument(
        "--history-file",
        default=os.path.expanduser("~/.config/heyvox/training/eval_history.jsonl"),
        help="Append-only JSONL log of every eval run "
             "(default: ~/.config/heyvox/training/eval_history.jsonl)",
    )
    args = p.parse_args()

    try:
        from openwakeword.model import Model
    except ImportError:
        print("ERROR: openwakeword required", file=sys.stderr)
        return 2

    framework = "onnx" if args.model.endswith(".onnx") else "tflite"
    model = Model(wakeword_models=[args.model], inference_framework=framework)
    wake = args.wake_name or os.path.splitext(os.path.basename(args.model))[0]

    def _find(d):
        out = []
        for ext in ("wav", "flac", "WAV", "FLAC"):
            out += glob.glob(os.path.join(d, "**", f"*.{ext}"), recursive=True)
        return sorted(set(out))

    neg_files = _find(args.negatives)
    if args.limit_neg > 0:
        neg_files = neg_files[: args.limit_neg]
    print(f"Negatives: {len(neg_files)} files from {args.negatives}")
    neg_scores, neg_secs = _collect(model, neg_files, wake, "neg")
    neg_hours = neg_secs / 3600.0
    print(f"Negative corpus: {neg_hours:.2f} hours, {len(neg_scores)} scored\n")

    pos_scores, pos_n = [], 0
    if args.positives:
        pos_files = _find(args.positives)
        print(f"Positives: {len(pos_files)} files from {args.positives}")
        pos_scores, _ = _collect(model, pos_files, wake, "pos")
        pos_n = len(pos_scores)

    print(f"\n{'thr':>5} {'FP':>5} {'FP/h':>8} {'TP%':>7} {'gate':>6}")
    results = []
    for t in THRESHOLDS:
        fp = sum(1 for s in neg_scores if s >= t)
        fp_h = fp / neg_hours if neg_hours > 0 else 0.0
        tp_rate = (sum(1 for s in pos_scores if s >= t) / pos_n) if pos_n else None
        gate = (fp_h < FP_PER_HOUR_GATE) and (tp_rate is None or tp_rate >= TP_GATE)
        tp_str = f"{tp_rate * 100:5.1f}" if tp_rate is not None else "   n/a"
        print(f"{t:>5} {fp:>5} {fp_h:>8.2f} {tp_str:>7} {'PASS' if gate else 'fail':>6}")
        results.append({
            "threshold": t, "fp_count": fp, "fp_per_hour": round(fp_h, 3),
            "tp_rate": tp_rate, "gate_pass": gate,
        })

    print("\n" + json.dumps({
        "negative_hours": round(neg_hours, 3),
        "negative_files": len(neg_scores),
        "positive_files": pos_n,
        "results": results,
    }, indent=2))

    # Lowest threshold (THRESHOLDS is already ascending) whose gate passed,
    # or None if nothing passed this run.
    best_passing_threshold = next(
        (r["threshold"] for r in results if r["gate_pass"]), None
    )
    history_record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "model": args.model,
        "negative_hours": round(neg_hours, 3),
        "negative_files": len(neg_scores),
        "positive_files": pos_n,
        "per_threshold": results,
        "best_passing_threshold": best_passing_threshold,
    }
    os.makedirs(os.path.dirname(args.history_file), exist_ok=True)
    with open(args.history_file, "a") as f:  # append-only, NEVER "w" (would truncate)
        f.write(json.dumps(history_record) + "\n")

    return 0


if __name__ == "__main__":
    sys.exit(main())
