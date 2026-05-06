"""
Record and generate negative (non-wake-word) training samples.

Usage:
    python training/record_negatives.py --output training/negatives/

    # Record 10 minutes of ambient audio + generate TTS speech negatives
    python training/record_negatives.py --ambient-duration 600 --output training/negatives/

    # Only generate TTS speech negatives (no mic required)
    python training/record_negatives.py --skip-ambient --output training/negatives/

This script produces two types of negative samples:
1. Ambient audio from your microphone, split into 2-second clips
2. Non-wake-word speech generated via macOS `say` with diverse phrases

All clips are augmented with the same pipeline used for positive samples
(pitch shift, speed change, noise, volume variation).
"""

import argparse
import os
import random
import subprocess
import tempfile
import wave
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000
CHANNELS = 1
CLIP_DURATION = 2.0  # seconds per clip
CLIP_SAMPLES = int(SAMPLE_RATE * CLIP_DURATION)

# ---------------------------------------------------------------------------
# Phrases for TTS negative generation
# IMPORTANT: None of these should contain "hey", "vox", or "hey vox"
# ---------------------------------------------------------------------------

GREETINGS = [
    "hello there",
    "good morning",
    "good afternoon",
    "good evening",
    "what's up",
    "how are you",
    "nice to meet you",
    "welcome back",
    "hi everyone",
    "greetings",
]

DEV_SPEECH = [
    "let's refactor this",
    "run the tests",
    "check the logs",
    "open a pull request",
    "merge the branch",
    "deploy to staging",
    "looks like a bug",
    "can you review this",
    "push to main",
    "revert that commit",
    "start the server",
    "check the database",
    "update the config",
    "run the linter",
    "add a unit test",
    "the build failed",
    "that's a race condition",
    "increase the timeout",
    "scroll down",
    "go to line fifty",
    "close this file",
    "open the terminal",
    "what's the status",
    "let me think about this",
    "try again",
    "stop the process",
    "restart the service",
    "looks good to me",
    "ship it",
    "needs more tests",
]

COMMON_PHRASES = [
    "the weather is nice today",
    "I need a coffee",
    "let's take a break",
    "sounds good",
    "absolutely",
    "no problem",
    "one moment please",
    "I'll be right back",
    "that makes sense",
    "interesting point",
    "can you repeat that",
    "I don't think so",
    "maybe later",
    "sure thing",
    "not right now",
    "okay thanks",
    "perfect",
    "exactly",
    "wait a second",
    "hold on",
]

# Similar-sounding but NOT the wake word
CONFUSABLES = [
    "next box",
    "text blocks",
    "gray fox",
    "play rocks",
    "bay docks",
    "day walks",
    "say what",
    "pay talks",
    "may box",
    "stay locks",
    "okay google",
    "alexa",
    "siri",
    "computer",
]

ALL_PHRASES = GREETINGS + DEV_SPEECH + COMMON_PHRASES + CONFUSABLES


# ---------------------------------------------------------------------------
# Audio I/O
# ---------------------------------------------------------------------------

def save_wav(audio: np.ndarray, path: str, sample_rate: int = SAMPLE_RATE) -> None:
    """Save float32 audio as 16-bit PCM WAV."""
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(path, "wb") as wf:
        wf.setnchannels(CHANNELS)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())


def load_wav(path: str) -> tuple[np.ndarray, int]:
    """Load a WAV file as float32 numpy array."""
    with wave.open(path, "rb") as wf:
        sr = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
        audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32767.0
    return audio, sr


def resample(audio: np.ndarray, orig_sr: int, target_sr: int) -> np.ndarray:
    """Simple linear interpolation resample."""
    if orig_sr == target_sr:
        return audio
    ratio = target_sr / orig_sr
    new_len = int(len(audio) * ratio)
    indices = np.linspace(0, len(audio) - 1, new_len)
    return np.interp(indices, np.arange(len(audio)), audio).astype(np.float32)


# ---------------------------------------------------------------------------
# Augmentation (same pipeline as generate_synthetic.py)
# ---------------------------------------------------------------------------

def augment_speed(audio: np.ndarray, factor: float) -> np.ndarray:
    """Change speed by resampling."""
    new_len = int(len(audio) / factor)
    if new_len < 1:
        return audio
    indices = np.linspace(0, len(audio) - 1, new_len)
    return np.interp(indices, np.arange(len(audio)), audio).astype(np.float32)


def augment_pitch(audio: np.ndarray, semitones: float) -> np.ndarray:
    """Shift pitch by resampling then truncating/padding."""
    factor = 2 ** (semitones / 12.0)
    resampled = augment_speed(audio, factor)
    target_len = len(audio)
    if len(resampled) > target_len:
        return resampled[:target_len]
    return np.pad(resampled, (0, target_len - len(resampled)))


def augment_noise(audio: np.ndarray, snr_db: float = 20.0) -> np.ndarray:
    """Add white noise at given SNR."""
    rms_signal = np.sqrt(np.mean(audio ** 2)) + 1e-10
    rms_noise = rms_signal / (10 ** (snr_db / 20))
    noise = np.random.randn(len(audio)).astype(np.float32) * rms_noise
    return audio + noise


def augment_volume(audio: np.ndarray, gain_db: float) -> np.ndarray:
    """Apply volume gain in dB."""
    return audio * (10 ** (gain_db / 20))


def apply_random_augmentation(audio: np.ndarray) -> np.ndarray:
    """Apply a random combination of augmentations."""
    if random.random() < 0.5:
        audio = augment_speed(audio, random.uniform(0.85, 1.15))
    if random.random() < 0.4:
        audio = augment_pitch(audio, random.uniform(-2, 2))
    if random.random() < 0.6:
        audio = augment_noise(audio, random.uniform(15, 30))
    if random.random() < 0.5:
        audio = augment_volume(audio, random.uniform(-6, 6))
    return audio


def pad_or_trim(audio: np.ndarray, target_len: int = CLIP_SAMPLES) -> np.ndarray:
    """Pad with silence or trim to exact length."""
    if len(audio) >= target_len:
        # Random offset so the speech isn't always at the start
        max_start = len(audio) - target_len
        start = random.randint(0, max_start) if max_start > 0 else 0
        return audio[start:start + target_len]
    pad_before = random.randint(0, target_len - len(audio))
    return np.pad(audio, (pad_before, target_len - len(audio) - pad_before))


# ---------------------------------------------------------------------------
# Ambient recording
# ---------------------------------------------------------------------------

def record_ambient(duration: float) -> np.ndarray:
    """Record ambient audio from the default microphone."""
    import sounddevice as sd

    total_samples = int(duration * SAMPLE_RATE)
    print(f"Recording {duration:.0f}s of ambient audio...")
    print("  Tip: Let normal background sounds play — typing, fan noise, music,")
    print("  conversations that do NOT contain the wake word.")
    print()

    audio = sd.rec(
        total_samples,
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype="float32",
    )
    # Show progress every 10 seconds
    elapsed = 0
    interval = 10
    while elapsed < duration:
        wait = min(interval, duration - elapsed)
        sd.sleep(int(wait * 1000))
        elapsed += wait
        pct = min(100, int(elapsed / duration * 100))
        print(f"  {elapsed:.0f}s / {duration:.0f}s ({pct}%)", flush=True)

    sd.wait()
    print("  Recording complete.\n")
    return audio.flatten()


def split_ambient_into_clips(
    audio: np.ndarray,
    overlap: float = 0.5,
) -> list[np.ndarray]:
    """Split continuous audio into overlapping 2-second clips.

    Args:
        audio: Raw ambient audio.
        overlap: Overlap ratio between consecutive clips (0.0-0.9).
    """
    step = int(CLIP_SAMPLES * (1.0 - overlap))
    if step < 1:
        step = CLIP_SAMPLES
    clips = []
    for start in range(0, len(audio) - CLIP_SAMPLES + 1, step):
        clip = audio[start:start + CLIP_SAMPLES]
        clips.append(clip)
    return clips


# ---------------------------------------------------------------------------
# TTS speech negatives
# ---------------------------------------------------------------------------

def get_macos_voices() -> list[str]:
    """Get available macOS `say` voices (English only)."""
    try:
        result = subprocess.run(
            ["say", "-v", "?"],
            capture_output=True, text=True, timeout=5,
        )
        voices = []
        for line in result.stdout.strip().split("\n"):
            parts = line.split()
            if len(parts) >= 2 and "en_" in line:
                voices.append(parts[0])
        return voices or ["Samantha"]
    except Exception:
        return ["Samantha"]


def generate_speech_negative(phrase: str, voice: str, output_path: str) -> bool:
    """Generate a single speech clip using macOS `say`."""
    with tempfile.NamedTemporaryFile(suffix=".aiff", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        rate = random.randint(150, 250)
        subprocess.run(
            ["say", "-v", voice, "-r", str(rate), "-o", tmp_path, phrase],
            capture_output=True, timeout=10,
        )
        if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) < 100:
            return False

        wav_tmp = tmp_path + ".wav"
        try:
            subprocess.run(
                ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", tmp_path, wav_tmp],
                capture_output=True, timeout=10,
            )
        except Exception:
            return False

        if not os.path.exists(wav_tmp):
            return False

        audio, sr = load_wav(wav_tmp)
        audio = resample(audio, sr, SAMPLE_RATE)
        audio = apply_random_augmentation(audio)
        audio = pad_or_trim(audio)
        save_wav(audio, output_path)
        return True

    finally:
        for p in [tmp_path, tmp_path + ".wav"]:
            try:
                os.unlink(p)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Record and generate negative training samples"
    )
    parser.add_argument(
        "--output", default="training/negatives/",
        help="Output directory (default: training/negatives/)",
    )
    parser.add_argument(
        "--ambient-duration", type=float, default=300.0,
        help="Seconds of ambient audio to record (default: 300 = 5 minutes)",
    )
    parser.add_argument(
        "--ambient-overlap", type=float, default=0.5,
        help="Overlap ratio for ambient clip splitting (default: 0.5)",
    )
    parser.add_argument(
        "--speech-count", type=int, default=0,
        help="Number of TTS speech clips to generate (default: auto = 3x phrase list)",
    )
    parser.add_argument(
        "--skip-ambient", action="store_true",
        help="Skip ambient recording (generate TTS speech negatives only)",
    )
    parser.add_argument(
        "--skip-tts", action="store_true",
        help="Skip TTS generation (ambient recording only)",
    )
    parser.add_argument(
        "--augment-copies", type=int, default=2,
        help="Number of augmented copies per clip (default: 2)",
    )
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    existing = list(output_dir.glob("*.wav"))
    print(f"\n{'=' * 60}")
    print("  Negative Sample Generator")
    print(f"  Output: {output_dir}/")
    print(f"  Existing clips: {len(existing)}")
    print(f"{'=' * 60}\n")

    total_saved = 0
    idx = len(existing)

    # -----------------------------------------------------------------------
    # Part 1: Ambient recording
    # -----------------------------------------------------------------------
    if not args.skip_ambient:
        ambient_audio = record_ambient(args.ambient_duration)
        ambient_clips = split_ambient_into_clips(ambient_audio, args.ambient_overlap)
        print(f"Split ambient audio into {len(ambient_clips)} clips")

        # Save raw ambient clips
        for clip in ambient_clips:
            filename = f"ambient_{idx:05d}.wav"
            save_wav(clip, str(output_dir / filename))
            idx += 1
            total_saved += 1

        # Save augmented copies of ambient clips
        if args.augment_copies > 0:
            print(f"Generating {args.augment_copies} augmented copy(ies) per ambient clip...")
            aug_count = 0
            for clip in ambient_clips:
                for _ in range(args.augment_copies):
                    augmented = apply_random_augmentation(clip)
                    filename = f"ambient_aug_{idx:05d}.wav"
                    save_wav(augmented, str(output_dir / filename))
                    idx += 1
                    aug_count += 1
            total_saved += aug_count
            print(f"  Created {aug_count} augmented ambient clips")

        print()

    # -----------------------------------------------------------------------
    # Part 2: TTS speech negatives
    # -----------------------------------------------------------------------
    if not args.skip_tts:
        voices = get_macos_voices()
        print(f"macOS voices available: {len(voices)}")

        # Determine how many TTS clips to generate
        speech_count = args.speech_count
        if speech_count <= 0:
            # Default: 3 passes through all phrases (each with random voice)
            speech_count = len(ALL_PHRASES) * 3

        print(f"Generating {speech_count} TTS speech negatives...\n")
        generated = 0
        failed = 0

        for i in range(speech_count):
            phrase = ALL_PHRASES[i % len(ALL_PHRASES)]
            voice = random.choice(voices)
            filename = f"speech_{idx:05d}.wav"
            filepath = output_dir / filename

            ok = generate_speech_negative(phrase, voice, str(filepath))
            if ok:
                generated += 1
                idx += 1

                # Also create augmented copies
                if args.augment_copies > 0:
                    base_audio, _ = load_wav(str(filepath))
                    for c in range(args.augment_copies):
                        augmented = apply_random_augmentation(base_audio)
                        augmented = pad_or_trim(augmented)
                        aug_filename = f"speech_aug_{idx:05d}.wav"
                        save_wav(augmented, str(output_dir / aug_filename))
                        idx += 1
                        generated += 1
            else:
                failed += 1

            if (i + 1) % 20 == 0:
                print(f"  Progress: {i + 1}/{speech_count} phrases "
                      f"({generated} clips saved, {failed} failed)")

        total_saved += generated
        print(f"\n  TTS speech clips: {generated} saved, {failed} failed")

    # -----------------------------------------------------------------------
    # Summary
    # -----------------------------------------------------------------------
    final_count = len(list(output_dir.glob("*.wav")))
    print(f"\n{'=' * 60}")
    print(f"  Done! Saved {total_saved} new negative clips this run.")
    print(f"  Total negative clips in {output_dir}: {final_count}")
    print(f"{'=' * 60}")
    print("\nNext step: train with negatives included:")
    print("  python training/train_model.py \\")
    print("    --positive-dir training/recordings/ training/synthetic/ \\")
    print("    --negative-dir training/negatives/ \\")
    print("    --output models/hey_vox.onnx")


if __name__ == "__main__":
    main()
