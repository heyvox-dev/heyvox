"""
Record personal wake word samples for openwakeword training.

Usage:
    python training/record_samples.py --keyword "hey vox" --count 75 --output training/recordings/

Records short audio clips (~2 seconds each) of the user saying the wake word.
Guides the user through varied recording conditions (normal, whisper, loud,
fast, slow) to improve model robustness.
"""

import argparse
import os
import sys
import time
import wave
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000
CHANNELS = 1
RECORD_SECONDS = 2.0
SILENCE_THRESHOLD = 0.01  # RMS threshold to detect speech


def record_clip(duration: float = RECORD_SECONDS) -> np.ndarray:
    """Record a single audio clip from the default microphone."""
    import sounddevice as sd
    print("  Recording... ", end="", flush=True)
    audio = sd.rec(
        int(duration * SAMPLE_RATE),
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype="float32",
    )
    sd.wait()
    print("done.")
    return audio.flatten()


def save_wav(audio: np.ndarray, path: str) -> None:
    """Save float32 audio as 16-bit PCM WAV."""
    pcm = (audio * 32767).astype(np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm.tobytes())


def audio_rms(audio: np.ndarray) -> float:
    """Compute RMS energy of audio signal."""
    return float(np.sqrt(np.mean(audio ** 2)))


PROMPTS = [
    ("normal", "Say it naturally"),
    ("loud", "Say it a bit louder"),
    ("quiet", "Say it quietly"),
    ("fast", "Say it quickly"),
    ("slow", "Say it slowly"),
    ("far", "Move back from the mic and say it"),
    ("close", "Get close to the mic and say it"),
]


def main():
    parser = argparse.ArgumentParser(description="Record wake word samples")
    parser.add_argument("--keyword", default="hey vox", help="Wake word phrase")
    parser.add_argument("--count", type=int, default=75, help="Number of samples to record")
    parser.add_argument("--output", default="training/recordings/", help="Output directory")
    parser.add_argument("--duration", type=float, default=RECORD_SECONDS, help="Seconds per clip")
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Count existing recordings to allow resuming
    existing = list(output_dir.glob("*.wav"))
    start_idx = len(existing)
    remaining = args.count - start_idx

    if remaining <= 0:
        print(f"Already have {len(existing)} recordings (target: {args.count}). Done!")
        return

    print(f"\n{'=' * 60}")
    print(f"  Wake Word Recording: \"{args.keyword}\"")
    print(f"  Target: {args.count} samples ({start_idx} existing, {remaining} to go)")
    print(f"  Duration: {args.duration}s per clip")
    print(f"  Output: {output_dir}/")
    print(f"{'=' * 60}\n")
    print("Press Enter to record each sample. Press Ctrl+C to stop early.\n")

    recorded = 0
    try:
        for i in range(remaining):
            idx = start_idx + i
            prompt_type, prompt_text = PROMPTS[idx % len(PROMPTS)]

            print(f"[{idx + 1}/{args.count}] {prompt_text}: \"{args.keyword}\"")
            input("  Press Enter when ready...")

            audio = record_clip(args.duration)
            rms = audio_rms(audio)

            if rms < SILENCE_THRESHOLD:
                print(f"  WARNING: Very quiet recording (RMS={rms:.4f}). Re-record? [y/N] ", end="")
                if input().strip().lower() == "y":
                    audio = record_clip(args.duration)
                    rms = audio_rms(audio)

            filename = f"{args.keyword.replace(' ', '_')}_{idx:04d}_{prompt_type}.wav"
            filepath = output_dir / filename
            save_wav(audio, str(filepath))
            recorded += 1
            print(f"  Saved: {filepath} (RMS={rms:.4f})\n")

    except KeyboardInterrupt:
        print(f"\n\nStopped early.")

    total = start_idx + recorded
    print(f"\nTotal recordings: {total}/{args.count}")
    if total >= args.count:
        print("All samples collected! Ready for training.")
    else:
        print(f"Need {args.count - total} more. Run again to continue.")


if __name__ == "__main__":
    main()
