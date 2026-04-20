"""Featurise WAV clips using the same melspec + embedding chain the runtime uses.

Takes a directory of audio clips, normalises them to 16 kHz mono 2-second
windows, runs openwakeword's `AudioFeatures.embed_clips`, and saves the result
as a single `.npy` of shape `(N, 16, 96)` — the format expected by
openwakeword.train's `feature_data_files` entries.

Usage:
    python3 tools/featurise_clips.py \\
        --input-dir ~/.config/heyvox/training/tp \\
        --output    /tmp/personal_tp.npy
    # Optionally filter by glob / walk recursively:
    python3 tools/featurise_clips.py \\
        --input-dir ~/.config/heyvox/training \\
        --glob "tp/*.wav" "fn/*.wav" "positives/*.wav" \\
        --output /tmp/personal_positive.npy --recursive
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import numpy as np

TARGET_SR = 16000
TARGET_SAMPLES = 32000  # 2 seconds at 16 kHz (openwakeword convention for hey_vox)
_MIN_RMS = 50            # drop near-silent clips (ten seconds of dead mic is not signal)


def load_wav_normalised(path: Path, target_sr: int = TARGET_SR,
                        target_len: int = TARGET_SAMPLES) -> np.ndarray | None:
    """Load WAV, force mono + 16 kHz + fixed length. Return int16 or None if bad."""
    import soundfile as sf
    try:
        audio, sr = sf.read(str(path), dtype="int16", always_2d=False)
    except Exception as e:
        print(f"  skip {path.name}: read failed ({e})", file=sys.stderr)
        return None
    if audio.ndim == 2:
        audio = audio.mean(axis=1).astype(np.int16)
    if sr != target_sr:
        try:
            from scipy.signal import resample_poly
            from math import gcd
            g = gcd(target_sr, sr)
            audio = resample_poly(audio.astype(np.float32), target_sr // g, sr // g)
            audio = np.clip(audio, -32768, 32767).astype(np.int16)
        except Exception as e:
            print(f"  skip {path.name}: resample failed ({e})", file=sys.stderr)
            return None
    rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
    if rms < _MIN_RMS:
        return None
    if len(audio) >= target_len:
        return audio[-target_len:]
    pad = np.zeros(target_len - len(audio), dtype=np.int16)
    return np.concatenate([pad, audio])


def collect_wav_paths(input_dir: Path, globs: list[str] | None,
                      recursive: bool) -> list[Path]:
    paths: list[Path] = []
    if globs:
        for g in globs:
            paths.extend(sorted(input_dir.glob(g)))
    elif recursive:
        paths = sorted(input_dir.rglob("*.wav"))
    else:
        paths = sorted(input_dir.glob("*.wav"))
    # Dedupe while preserving order
    seen: set[str] = set()
    out: list[Path] = []
    for p in paths:
        key = str(p.resolve())
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def featurise(paths: list[Path], batch_size: int = 128) -> np.ndarray:
    """Return (N, frames, embed_dim) embeddings for clips. Drops bad clips."""
    from openwakeword.utils import AudioFeatures
    af = AudioFeatures()

    # Batch the read+normalise to keep peak memory bounded (int16 @ 2s = 64 KB/clip).
    batches: list[np.ndarray] = []
    good_paths: list[Path] = []
    buf: list[np.ndarray] = []
    buf_paths: list[Path] = []

    def flush():
        if not buf:
            return
        x = np.stack(buf, axis=0)
        emb = af.embed_clips(x, batch_size=min(batch_size, len(buf)))
        batches.append(emb)
        good_paths.extend(buf_paths)
        buf.clear()
        buf_paths.clear()

    for i, p in enumerate(paths, 1):
        a = load_wav_normalised(p)
        if a is None:
            continue
        buf.append(a)
        buf_paths.append(p)
        if len(buf) >= batch_size:
            flush()
            print(f"  featurised {len(good_paths)}/{len(paths)}", file=sys.stderr)
    flush()
    if not batches:
        return np.empty((0, 16, 96), dtype=np.float32)
    out = np.concatenate(batches, axis=0)
    print(f"  done: {out.shape} from {len(good_paths)}/{len(paths)} clips", file=sys.stderr)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--input-dir", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--glob", nargs="*", default=None,
                    help="Glob(s) relative to input-dir (e.g. tp/*.wav). "
                         "If omitted, uses *.wav (or **/*.wav with --recursive).")
    ap.add_argument("--recursive", action="store_true")
    ap.add_argument("--batch-size", type=int, default=128)
    ap.add_argument("--limit", type=int, default=0,
                    help="Stop after N clips (smoke testing).")
    args = ap.parse_args()

    if not args.input_dir.is_dir():
        print(f"ERROR: not a directory: {args.input_dir}", file=sys.stderr)
        return 2

    paths = collect_wav_paths(args.input_dir, args.glob, args.recursive)
    if args.limit:
        paths = paths[: args.limit]
    if not paths:
        print("ERROR: no .wav files found", file=sys.stderr)
        return 2
    print(f"Found {len(paths)} clips under {args.input_dir}", file=sys.stderr)

    embeddings = featurise(paths, batch_size=args.batch_size)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    np.save(args.output, embeddings)
    size_mb = args.output.stat().st_size / (1024 * 1024)
    print(f"Saved {embeddings.shape} → {args.output} ({size_mb:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
