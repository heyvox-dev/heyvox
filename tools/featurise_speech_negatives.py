#!/usr/bin/env python3
"""Featurise a large speech corpus into WINDOWED hard-negatives for v8 training.

Why this exists (v8): the 5.4h LibriSpeech gate eval (tools/fp_rate_eval.py)
exposed that the deployed hey_vox model fires at score ~1.0 on ordinary
connected speech with NO shared "hey vox" homophone — a classic over-broad
positive boundary from training on synthetic TTS positives + too few natural
speech negatives. The fix is to feed fluent speech as label-0 examples.

Unlike tools/featurise_clips.py (which keeps only the LAST 2s of each short
wake-word clip), this slices each long utterance into overlapping 2s windows
(hop 1s) so a 10s sentence yields ~9 negatives, and reads flac as well as wav.

Output: speech_hard_negative.npy (N, 16, 96) float32 + tarball, ingested by
retrain_heyvox_v8.py as its own negative class `speech_hard_negative`.

Usage:
    python3 tools/featurise_speech_negatives.py \
        --corpus /tmp/LibriSpeech/dev-clean \
        --tarball /tmp/speech_negatives.tar.gz \
        --max-windows 12000
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
import tarfile

import numpy as np

TARGET_SR = 16000
WIN = 32000   # 2s window — matches openwakeword hey_vox total_length
HOP = 16000   # 1s hop → 50% overlap
MIN_RMS = 50  # skip near-silent windows (gaps between utterances)


def iter_windows(path: str):
    """Yield 2s int16 windows from one audio file (mono, 16 kHz)."""
    import soundfile as sf
    try:
        a, sr = sf.read(path, dtype="int16", always_2d=False)
    except Exception as e:
        print(f"  skip {os.path.basename(path)}: {e}", file=sys.stderr)
        return
    if a.ndim > 1:
        a = a.mean(axis=1).astype(np.int16)
    if sr != TARGET_SR:
        from math import gcd
        from scipy.signal import resample_poly
        g = gcd(TARGET_SR, sr)
        a = np.clip(resample_poly(a.astype(np.float32), TARGET_SR // g, sr // g),
                    -32768, 32767).astype(np.int16)
    if len(a) < WIN:
        pad = np.zeros(WIN - len(a), dtype=np.int16)
        yield np.concatenate([pad, a])
        return
    for s in range(0, len(a) - WIN + 1, HOP):
        w = a[s:s + WIN]
        if np.sqrt(np.mean(w.astype(np.float64) ** 2)) >= MIN_RMS:
            yield w


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--corpus", required=True, help="dir of speech audio (recursive, wav/flac)")
    ap.add_argument("--out-dir", default="/tmp/speech_negatives")
    ap.add_argument("--tarball", default="/tmp/speech_negatives.tar.gz")
    ap.add_argument("--max-windows", type=int, default=12000,
                    help="random-subsample to this many windows (seed=0)")
    ap.add_argument("--batch-size", type=int, default=256)
    args = ap.parse_args()

    files = []
    for ext in ("flac", "wav", "FLAC", "WAV"):
        files += glob.glob(os.path.join(args.corpus, "**", f"*.{ext}"), recursive=True)
    files = sorted(set(files))
    if not files:
        print(f"ERROR: no audio under {args.corpus}", file=sys.stderr)
        return 2
    print(f"{len(files)} files under {args.corpus}", file=sys.stderr)

    wins = []
    for i, f in enumerate(files):
        wins.extend(iter_windows(f))
        if (i + 1) % 300 == 0:
            print(f"  windowed {i + 1}/{len(files)} files → {len(wins)} windows", file=sys.stderr)
    print(f"total windows: {len(wins)}", file=sys.stderr)

    rng = np.random.default_rng(0)
    if len(wins) > args.max_windows:
        idx = sorted(rng.choice(len(wins), args.max_windows, replace=False))
        wins = [wins[j] for j in idx]
        print(f"subsampled → {len(wins)} windows", file=sys.stderr)

    from openwakeword.utils import AudioFeatures
    af = AudioFeatures()
    out = []
    for i in range(0, len(wins), args.batch_size):
        x = np.stack(wins[i:i + args.batch_size], axis=0)
        out.append(af.embed_clips(x, batch_size=min(args.batch_size, len(x))))
        print(f"  featurised {min(i + args.batch_size, len(wins))}/{len(wins)}", file=sys.stderr)
    emb = np.concatenate(out, axis=0).astype(np.float32)

    os.makedirs(args.out_dir, exist_ok=True)
    npy = os.path.join(args.out_dir, "speech_hard_negative.npy")
    np.save(npy, emb)
    man = os.path.join(args.out_dir, "MANIFEST.txt")
    with open(man, "w") as f:
        f.write(
            f"speech_hard_negative.npy  shape={tuple(emb.shape)}  label=0\n"
            f"source: {args.corpus}\n"
            f"window: 2s, hop: 1s, min_rms: {MIN_RMS}\n"
            f"feature_format: openwakeword melspec+embedding (16 frames x 96 dim, float32)\n"
        )
    with tarfile.open(args.tarball, "w:gz") as tf:
        tf.add(npy, arcname="speech_hard_negative.npy")
        tf.add(man, arcname="MANIFEST.txt")
    print(f"wrote {emb.shape} → {args.tarball} "
          f"({os.path.getsize(args.tarball) / 1e6:.1f} MB)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
