"""Evaluate a trained wake-word model against the D-16 ship gate.

Phase 14 / SPEC R6 / D-16. The gate: TP >= 70% AND FP < 1 per hour, both
must hold simultaneously on a hybrid synthetic + real-voice test set.

Usage:
    # Single-threshold evaluation
    python training/evaluate_model.py \\
        --model models/hey_vox.onnx \\
        --positives test/real_voice/ \\
        --negatives test/fp_corpus/ \\
        --threshold 0.7

    # Sweep mode — try 0.5, 0.6, 0.7, 0.8, 0.9, 0.95 and recommend the lowest
    # threshold that satisfies both gates
    python training/evaluate_model.py \\
        --model models/hey_vox.onnx \\
        --positives test/real_voice/ \\
        --negatives test/fp_corpus/ \\
        --sweep

Exit codes:
    0 = gate PASSED (in single mode: at the supplied threshold; in sweep mode:
        at least one threshold satisfies both gates)
    1 = gate FAILED (no threshold satisfies the gate; or supplied threshold misses)

The negative corpus should be openwakeword's standard validation set
(DiPCo + Santa Barbara + MUSDB, ~11h total) plus any project-specific real
noise samples. The positive corpus should be hybrid: synthetic Kokoro/Qwen
TTS clips for volume + real-voice clips collected via record.felberer.at.
"""

from __future__ import annotations

import argparse
import json
import sys
import wave
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000
FRAME_SIZE = 1280  # 80ms frames, openwakeword native frame size

TP_GATE = 0.70      # SPEC R6 / D-16 — minimum true-positive rate
FP_PER_HOUR_GATE = 1.0  # D-16 — maximum false-positive rate per hour of normal speech


def _load_wav(path: Path) -> np.ndarray:
    """Load a WAV file as float32 mono at 16 kHz."""
    with wave.open(str(path), "rb") as wf:
        sr = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
        audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32767.0
    if sr != SAMPLE_RATE:
        ratio = SAMPLE_RATE / sr
        new_len = int(len(audio) * ratio)
        indices = np.linspace(0, len(audio) - 1, new_len)
        audio = np.interp(indices, np.arange(len(audio)), audio).astype(np.float32)
    return audio


def _detect(model, wav_path: Path, wake_name: str, threshold: float) -> bool:
    """Stream the WAV through the model in 80 ms frames.

    Returns True if ANY frame's predicted score for `wake_name` >= threshold.
    Resets model state at end so per-clip state doesn't leak between files.
    """
    model.reset()
    audio = _load_wav(wav_path)
    # Pad to a multiple of FRAME_SIZE so the last partial frame doesn't go
    # unfed (openwakeword expects exactly FRAME_SIZE samples per predict()).
    pad = (-len(audio)) % FRAME_SIZE
    if pad:
        audio = np.concatenate([audio, np.zeros(pad, dtype=np.float32)])
    triggered = False
    for i in range(0, len(audio), FRAME_SIZE):
        frame = audio[i : i + FRAME_SIZE]
        # openwakeword's predict() expects int16 PCM, not float32 — convert back.
        frame_int16 = (frame * 32767.0).astype(np.int16)
        model.predict(frame_int16)
        score_buf = model.prediction_buffer.get(wake_name)
        if score_buf and len(score_buf) > 0 and score_buf[-1] >= threshold:
            triggered = True
            break
    model.reset()
    return triggered


def evaluate(
    model_path: str,
    positives_dir: str,
    negatives_dir: str,
    threshold: float,
) -> dict:
    """Run the full TP / FP evaluation at one threshold. Returns a result dict."""
    try:
        from openwakeword.model import Model
    except ImportError:
        print("ERROR: openwakeword required. Install with: pip install openwakeword", file=sys.stderr)
        sys.exit(2)

    # openwakeword defaults to inference_framework="tflite"; an .onnx model
    # then raises "tflite ... but onnx models were provided". Pick the framework
    # from the extension, mirroring heyvox/audio/wakeword.py's runtime loader.
    framework = "onnx" if str(model_path).endswith(".onnx") else "tflite"
    model = Model(wakeword_models=[model_path], inference_framework=framework)
    wake_name = Path(model_path).stem  # "hey_vox.onnx" -> "hey_vox"

    # True-positive rate: fraction of positive clips that triggered at threshold
    pos_files = sorted(Path(positives_dir).glob("*.wav"))
    tp = 0
    for f in pos_files:
        if _detect(model, f, wake_name, threshold):
            tp += 1
    tp_rate = tp / max(len(pos_files), 1)

    # False-positive rate: count of triggering clips / total negative hours
    neg_files = sorted(Path(negatives_dir).glob("*.wav"))
    fp = 0
    total_seconds = 0.0
    for f in neg_files:
        with wave.open(str(f), "rb") as wf:
            total_seconds += wf.getnframes() / wf.getframerate()
        if _detect(model, f, wake_name, threshold):
            fp += 1
    fp_per_hour = fp / (total_seconds / 3600.0) if total_seconds > 0 else 0.0

    return {
        "tp_rate": tp_rate,
        "tp_count": tp,
        "positive_total": len(pos_files),
        "fp_count": fp,
        "fp_per_hour": fp_per_hour,
        "negative_total_seconds": total_seconds,
        "threshold": threshold,
        "gate_pass": tp_rate >= TP_GATE and fp_per_hour < FP_PER_HOUR_GATE,
    }


def main() -> int:
    p = argparse.ArgumentParser(description="Wake-word ship-gate evaluator (D-16)")
    p.add_argument("--model", required=True, help="Path to .onnx wake-word model")
    p.add_argument("--positives", required=True, help="Directory of positive (hey_vox) WAV clips")
    p.add_argument("--negatives", required=True, help="Directory of negative (FP corpus) WAV clips")
    p.add_argument("--threshold", type=float, default=0.5)
    p.add_argument(
        "--sweep",
        action="store_true",
        help="Sweep thresholds [0.5, 0.6, 0.7, 0.8, 0.9, 0.95] and recommend the lowest passing one",
    )
    args = p.parse_args()

    if args.sweep:
        results = []
        for t in [0.5, 0.6, 0.7, 0.8, 0.9, 0.95]:
            results.append(evaluate(args.model, args.positives, args.negatives, t))
        passing = [r for r in results if r["gate_pass"]]
        if passing:
            best = min(passing, key=lambda r: r["threshold"])
            print(f"PASS — recommend threshold={best['threshold']}")
            print(json.dumps(best, indent=2))
            return 0
        print(f"FAIL — no threshold in {[r['threshold'] for r in results]} satisfies TP >= {TP_GATE} AND FP < {FP_PER_HOUR_GATE}/hour")
        print(json.dumps(results, indent=2))
        return 1

    r = evaluate(args.model, args.positives, args.negatives, args.threshold)
    print(json.dumps(r, indent=2))
    return 0 if r["gate_pass"] else 1


if __name__ == "__main__":
    sys.exit(main())
