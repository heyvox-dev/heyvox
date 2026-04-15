"""
Download and generate real-world negative examples for wake word training.

Three sources:
  1. Mozilla Common Voice (fr, de, en, es) via HuggingFace datasets — real human speech
  2. Confusable phrases via macOS `say` — phonetically similar to "hey vox"
  3. Synthetic environmental noise — fallback for MUSAN (11GB, not downloaded automatically)

All output: 16-bit PCM WAV, 16kHz mono, 2 seconds (padded or truncated).
Output directory: training/negatives_real/

Usage:
    python training/download_negatives.py
    python training/download_negatives.py --clips-per-language 500 --skip-commonvoice
    python training/download_negatives.py --output-dir /tmp/negatives

MUSAN note: Full MUSAN noise corpus is at https://www.openslr.org/17/ (11GB).
  Download and extract manually, then point the trainer at the noise/ subdirectory.
  This script generates synthetic noise as a lightweight substitute.
"""

import argparse
import os
import random
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000
CLIP_DURATION = 2  # seconds
CLIP_SAMPLES = SAMPLE_RATE * CLIP_DURATION

# ---------------------------------------------------------------------------
# WAV helpers
# ---------------------------------------------------------------------------

def save_wav(audio: np.ndarray, path: Path, sample_rate: int = SAMPLE_RATE) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())


def load_wav(path: str | Path) -> tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wf:
        sr = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
        n_channels = wf.getnchannels()
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32767.0
    if n_channels > 1:
        audio = audio.reshape(-1, n_channels).mean(axis=1)
    return audio, sr


def resample(audio: np.ndarray, orig_sr: int, target_sr: int = SAMPLE_RATE) -> np.ndarray:
    if orig_sr == target_sr:
        return audio
    new_len = int(len(audio) * target_sr / orig_sr)
    indices = np.linspace(0, len(audio) - 1, new_len)
    return np.interp(indices, np.arange(len(audio)), audio).astype(np.float32)


def normalize_clip(audio: np.ndarray) -> np.ndarray:
    """Pad or truncate to exactly CLIP_SAMPLES."""
    if len(audio) >= CLIP_SAMPLES:
        return audio[:CLIP_SAMPLES]
    return np.pad(audio, (0, CLIP_SAMPLES - len(audio))).astype(np.float32)


# ---------------------------------------------------------------------------
# Augmentation
# ---------------------------------------------------------------------------

def aug_speed(audio: np.ndarray, factor: float) -> np.ndarray:
    new_len = max(1, int(len(audio) / factor))
    indices = np.linspace(0, len(audio) - 1, new_len)
    return np.interp(indices, np.arange(len(audio)), audio).astype(np.float32)


def aug_pitch(audio: np.ndarray, semitones: float) -> np.ndarray:
    factor = 2 ** (semitones / 12.0)
    resampled = aug_speed(audio, factor)
    if len(resampled) >= len(audio):
        return resampled[:len(audio)]
    return np.pad(resampled, (0, len(audio) - len(resampled))).astype(np.float32)


def aug_noise(audio: np.ndarray, snr_db: float) -> np.ndarray:
    rms = np.sqrt(np.mean(audio ** 2)) + 1e-10
    rms_noise = rms / (10 ** (snr_db / 20))
    return audio + np.random.randn(len(audio)).astype(np.float32) * rms_noise


def apply_augmentation(audio: np.ndarray) -> np.ndarray:
    if random.random() < 0.5:
        audio = aug_speed(audio, random.uniform(0.85, 1.15))
    if random.random() < 0.4:
        audio = aug_pitch(audio, random.uniform(-2.0, 2.0))
    if random.random() < 0.6:
        audio = aug_noise(audio, random.uniform(15, 30))
    return audio


# ---------------------------------------------------------------------------
# Source 1: Mozilla Common Voice via HuggingFace datasets
# ---------------------------------------------------------------------------

COMMON_VOICE_LANGUAGES = ["fr", "de", "en", "es"]


def download_common_voice(output_dir: Path, clips_per_language: int) -> int:
    try:
        from datasets import load_dataset  # type: ignore
    except ImportError:
        print(
            "[common_voice] SKIP — 'datasets' not installed.\n"
            "  Install with: pip install datasets soundfile\n"
        )
        return 0

    total = 0
    for lang in COMMON_VOICE_LANGUAGES:
        lang_dir = output_dir / "common_voice" / lang
        lang_dir.mkdir(parents=True, exist_ok=True)

        existing = list(lang_dir.glob("*.wav"))
        if len(existing) >= clips_per_language:
            print(f"[common_voice/{lang}] Already have {len(existing)} clips, skipping.")
            total += len(existing)
            continue

        start_idx = len(existing)
        needed = clips_per_language - start_idx
        print(f"[common_voice/{lang}] Downloading {needed} clips (have {start_idx})...")

        try:
            ds = load_dataset(
                "mozilla-foundation/common_voice_17_0",
                lang,
                split="train",
                streaming=True,
                trust_remote_code=True,
            )
        except Exception as exc:
            print(f"[common_voice/{lang}] Load failed: {exc}")
            continue

        count = 0
        for i, item in enumerate(ds):
            if count >= needed:
                break

            out_path = lang_dir / f"{lang}_{start_idx + count:05d}.wav"
            if out_path.exists():
                count += 1
                continue

            try:
                audio_dict = item["audio"]
                audio = np.array(audio_dict["array"], dtype=np.float32)
                sr = audio_dict["sampling_rate"]
                if sr != SAMPLE_RATE:
                    audio = resample(audio, sr)
                audio = normalize_clip(audio)
                save_wav(audio, out_path)
                count += 1
            except Exception as exc:
                print(f"[common_voice/{lang}] Item {i} failed: {exc}")
                continue

            if count % 200 == 0:
                print(f"  {lang}: {count}/{needed}")

        print(f"[common_voice/{lang}] Done: {count} clips saved.")
        total += count

    return total


# ---------------------------------------------------------------------------
# Source 2: Confusable phrases via macOS `say`
# ---------------------------------------------------------------------------

CONFUSABLE_PHRASES = [
    # English phonetic confusables
    "hey fox",
    "hey box",
    "hey docs",
    "next box",
    "hey socks",
    "hey locks",
    "hey rocks",
    "a box",
    "the fox",
    # German confusables
    "hey folks",
    "hey Fuchs",
    "hey doch",
    # French confusables
    "allez",
    "salut",
    "c'est vrai",
    "ah bon",
    # Common assistant wake words (model must reject these)
    "hey siri",
    "hey alexa",
    "ok google",
    "hey cortana",
    "hey google",
]

VOICES_EN = ["Alex", "Samantha", "Tom", "Fred", "Victoria", "Daniel", "Karen", "Moira"]
VOICES_DE = ["Anna", "Markus", "Yannick"]
VOICES_FR = ["Thomas", "Amelie"]
VOICE_FALLBACK = ["Alex", "Samantha", "Tom"]


def get_available_voices() -> set[str]:
    try:
        result = subprocess.run(
            ["say", "--voice=?"],
            capture_output=True, text=True, timeout=10,
        )
        lines = result.stdout.splitlines()
        return {line.split()[0] for line in lines if line.strip()}
    except Exception:
        return set()


def say_to_wav(text: str, voice: str, out_path: Path) -> bool:
    """Generate speech with macOS say, save as WAV. Returns True on success."""
    with tempfile.NamedTemporaryFile(suffix=".aiff", delete=False) as tmp:
        tmp_path = tmp.name
    try:
        subprocess.run(
            ["say", "--voice", voice, "--output-file", tmp_path, "--file-format", "AIFF", text],
            capture_output=True, timeout=30, check=True,
        )
        # Convert AIFF → WAV via afconvert
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp2:
            wav_tmp = tmp2.name
        subprocess.run(
            ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", tmp_path, wav_tmp],
            capture_output=True, timeout=30, check=True,
        )
        audio, sr = load_wav(wav_tmp)
        audio = normalize_clip(audio)
        audio = apply_augmentation(audio)
        audio = normalize_clip(audio)
        save_wav(audio, out_path)
        return True
    except Exception as exc:
        print(f"  say failed ({voice!r}, {text!r}): {exc}")
        return False
    finally:
        for p in (tmp_path, wav_tmp if "wav_tmp" in dir() else ""):
            if p and os.path.exists(p):
                os.unlink(p)


def generate_confusables(output_dir: Path) -> int:
    conf_dir = output_dir / "confusables"
    conf_dir.mkdir(parents=True, exist_ok=True)

    available = get_available_voices()
    if not available:
        print("[confusables] SKIP — macOS `say` not available or no voices found.")
        return 0

    def pick_voices(candidates: list[str], n: int) -> list[str]:
        found = [v for v in candidates if v in available]
        if not found:
            found = [v for v in VOICE_FALLBACK if v in available]
        if not found:
            found = list(available)[:3]
        return found[:n]

    total = 0
    for phrase in CONFUSABLE_PHRASES:
        # Pick voices based on language heuristic
        if any(phrase.startswith(p) for p in ("hey Fuchs", "hey doch", "hey folks")):
            voices = pick_voices(VOICES_DE, 4)
        elif phrase in ("allez", "salut", "c'est vrai", "ah bon"):
            voices = pick_voices(VOICES_FR, 4)
        else:
            voices = pick_voices(VOICES_EN, 5)

        slug = phrase.lower().replace(" ", "_").replace("'", "").replace("é", "e")
        for voice in voices:
            out_path = conf_dir / f"{slug}__{voice.lower()}.wav"
            if out_path.exists():
                total += 1
                continue
            ok = say_to_wav(phrase, voice, out_path)
            if ok:
                total += 1
                print(f"  [confusables] {phrase!r} / {voice} -> {out_path.name}")

    print(f"[confusables] Done: {total} clips.")
    return total


# ---------------------------------------------------------------------------
# Source 3: Synthetic environmental noise (MUSAN substitute)
# ---------------------------------------------------------------------------

def gen_office_ambience(duration_samples: int) -> np.ndarray:
    """Simulate office ambience: low fan hum + occasional typing bursts."""
    t = np.arange(duration_samples, dtype=np.float32) / SAMPLE_RATE

    # Fan hum: 100Hz fundamental + harmonics
    hum = (
        0.03 * np.sin(2 * np.pi * 100 * t)
        + 0.015 * np.sin(2 * np.pi * 200 * t)
        + 0.008 * np.sin(2 * np.pi * 300 * t)
    )
    # Pink-ish noise floor
    white = np.random.randn(duration_samples).astype(np.float32) * 0.01
    # Typing bursts: 3-8 clicks at random positions
    clicks = np.zeros(duration_samples, dtype=np.float32)
    for _ in range(random.randint(3, 8)):
        pos = random.randint(0, duration_samples - 200)
        decay = np.exp(-np.arange(200) / 10).astype(np.float32)
        clicks[pos:pos + 200] += decay * random.uniform(0.05, 0.15)

    return np.clip(hum + white + clicks, -1.0, 1.0)


def gen_music_like(duration_samples: int) -> np.ndarray:
    """Simulate music: chord of sine waves with harmonics and amplitude envelope."""
    t = np.arange(duration_samples, dtype=np.float32) / SAMPLE_RATE
    # Random root frequency in musical range
    root = random.uniform(110, 440)
    # Major/minor-ish chord ratios
    ratios = [1.0, 1.25, 1.5, 2.0, 2.5]
    sig = np.zeros(duration_samples, dtype=np.float32)
    for ratio in ratios:
        freq = root * ratio
        amp = random.uniform(0.05, 0.15) / len(ratios)
        phase = random.uniform(0, 2 * np.pi)
        sig += amp * np.sin(2 * np.pi * freq * t + phase)
    # Amplitude envelope with some wobble
    wobble_freq = random.uniform(1, 4)
    envelope = 0.7 + 0.3 * np.sin(2 * np.pi * wobble_freq * t)
    return np.clip(sig * envelope, -1.0, 1.0)


def gen_street_noise(duration_samples: int) -> np.ndarray:
    """Simulate outdoor/street noise: broadband noise + occasional transients."""
    white = np.random.randn(duration_samples).astype(np.float32)
    # Low-pass-ish: average adjacent samples a few times
    for _ in range(4):
        white = (white[:-1] + white[1:]) / 2
        white = np.pad(white, (0, 1))
    white *= 0.15

    # Occasional car-pass-like transients
    for _ in range(random.randint(1, 4)):
        start = random.randint(0, duration_samples // 2)
        length = random.randint(SAMPLE_RATE // 4, SAMPLE_RATE)
        length = min(length, duration_samples - start)
        env = np.sin(np.pi * np.arange(length) / length).astype(np.float32)
        bump = np.random.randn(length).astype(np.float32) * env * 0.2
        white[start:start + length] += bump

    return np.clip(white, -1.0, 1.0)


NOISE_GENERATORS = {
    "office": gen_office_ambience,
    "music": gen_music_like,
    "street": gen_street_noise,
}


def generate_synthetic_noise(output_dir: Path, count_per_type: int = 200) -> int:
    noise_dir = output_dir / "noise_synthetic"
    noise_dir.mkdir(parents=True, exist_ok=True)
    total = 0
    for noise_type, gen_fn in NOISE_GENERATORS.items():
        for i in range(count_per_type):
            out_path = noise_dir / f"{noise_type}_{i:04d}.wav"
            if out_path.exists():
                total += 1
                continue
            audio = gen_fn(CLIP_SAMPLES)
            audio = normalize_clip(audio)
            save_wav(audio, out_path)
            total += 1
        print(f"[noise_synthetic/{noise_type}] {count_per_type} clips done.")
    return total


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download/generate negative examples for wake word training.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--output-dir",
        default="training/negatives_real",
        help="Output directory (default: training/negatives_real/)",
    )
    parser.add_argument(
        "--clips-per-language",
        type=int,
        default=2000,
        help="Common Voice clips to download per language (default: 2000)",
    )
    parser.add_argument(
        "--noise-clips",
        type=int,
        default=200,
        help="Synthetic noise clips per type (default: 200)",
    )
    parser.add_argument(
        "--skip-commonvoice",
        action="store_true",
        help="Skip Mozilla Common Voice download",
    )
    parser.add_argument(
        "--skip-confusables",
        action="store_true",
        help="Skip confusable phrase generation via macOS say",
    )
    parser.add_argument(
        "--skip-noise",
        action="store_true",
        help="Skip synthetic noise generation",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"Output directory: {output_dir}\n")

    totals: dict[str, int] = {}

    if not args.skip_commonvoice:
        print("=" * 60)
        print("Source 1: Mozilla Common Voice")
        print("=" * 60)
        totals["common_voice"] = download_common_voice(output_dir, args.clips_per_language)
    else:
        print("[common_voice] Skipped.")

    if not args.skip_confusables:
        print()
        print("=" * 60)
        print("Source 2: Confusable phrases (macOS say)")
        print("=" * 60)
        totals["confusables"] = generate_confusables(output_dir)
    else:
        print("[confusables] Skipped.")

    if not args.skip_noise:
        print()
        print("=" * 60)
        print("Source 3: Synthetic environmental noise")
        print("=" * 60)
        totals["noise"] = generate_synthetic_noise(output_dir, args.noise_clips)
    else:
        print("[noise] Skipped.")

    print()
    print("=" * 60)
    print("Summary")
    print("=" * 60)
    grand_total = 0
    for source, count in totals.items():
        print(f"  {source:20s}: {count:>5d} clips")
        grand_total += count
    print(f"  {'TOTAL':20s}: {grand_total:>5d} clips")
    print(f"\nAll clips saved to: {output_dir}")

    # Remind about MUSAN
    print(
        "\nNote: For more diverse noise negatives, download MUSAN manually:\n"
        "  https://www.openslr.org/17/  (~11GB)\n"
        "  Extract and add the noise/ subdirectory to your training pipeline."
    )


if __name__ == "__main__":
    main()
