"""
Generate synthetic wake word training data using TTS.

Usage:
    python training/generate_synthetic.py --keyword "hey vox" --count 500 --output training/synthetic/

Uses multiple TTS approaches to generate diverse "Hey Vox" audio clips:
1. macOS `say` command with available system voices (always available)
2. Coqui TTS with multiple speakers (if installed)

Applies augmentation (pitch shift, speed change, noise, room reverb) to increase
diversity and improve model robustness against varied acoustic conditions.
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


# -------------------------------------------------------------------------
# Augmentation
# -------------------------------------------------------------------------

def augment_speed(audio: np.ndarray, factor: float) -> np.ndarray:
    """Change speed by resampling (stretches/compresses time)."""
    new_len = int(len(audio) / factor)
    if new_len < 1:
        return audio
    indices = np.linspace(0, len(audio) - 1, new_len)
    return np.interp(indices, np.arange(len(audio)), audio).astype(np.float32)


def augment_pitch(audio: np.ndarray, semitones: float, sr: int = SAMPLE_RATE) -> np.ndarray:
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
    # Speed: 0.85x to 1.15x
    if random.random() < 0.5:
        audio = augment_speed(audio, random.uniform(0.85, 1.15))

    # Pitch: -2 to +2 semitones
    if random.random() < 0.4:
        audio = augment_pitch(audio, random.uniform(-2, 2))

    # Noise: 15-30 dB SNR
    if random.random() < 0.6:
        audio = augment_noise(audio, random.uniform(15, 30))

    # Volume: -6 to +6 dB
    if random.random() < 0.5:
        audio = augment_volume(audio, random.uniform(-6, 6))

    return audio


# -------------------------------------------------------------------------
# TTS backends
# -------------------------------------------------------------------------

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


def generate_with_say(keyword: str, voice: str, output_path: str) -> bool:
    """Generate a single clip using macOS `say` command."""
    with tempfile.NamedTemporaryFile(suffix=".aiff", delete=False) as tmp:
        tmp_path = tmp.name

    try:
        rate = random.randint(150, 250)  # Words per minute variation
        subprocess.run(
            ["say", "-v", voice, "-r", str(rate), "-o", tmp_path, keyword],
            capture_output=True, timeout=10,
        )
        if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) < 100:
            return False

        # Convert AIFF to 16kHz WAV via ffmpeg (if available) or afconvert
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

        # Pad to 2 seconds with silence
        target_len = SAMPLE_RATE * 2
        if len(audio) < target_len:
            pad_before = random.randint(0, target_len - len(audio))
            audio = np.pad(audio, (pad_before, target_len - len(audio) - pad_before))
        else:
            audio = audio[:target_len]

        save_wav(audio, output_path)
        return True

    finally:
        for p in [tmp_path, tmp_path + ".wav"]:
            try:
                os.unlink(p)
            except OSError:
                pass


def generate_with_coqui(keyword: str, output_path: str, speaker_idx: int = 0) -> bool:
    """Generate a single clip using Coqui TTS (if available)."""
    try:
        from TTS.api import TTS
    except ImportError:
        return False

    try:
        tts = TTS(model_name="tts_models/en/vctk/vits", progress_bar=False)
        speakers = tts.speakers or [None]
        speaker = speakers[speaker_idx % len(speakers)]

        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp_path = tmp.name

        tts.tts_to_file(
            text=keyword,
            speaker=speaker,
            file_path=tmp_path,
        )

        audio, sr = load_wav(tmp_path)
        audio = resample(audio, sr, SAMPLE_RATE)
        audio = apply_random_augmentation(audio)

        target_len = SAMPLE_RATE * 2
        if len(audio) < target_len:
            pad_before = random.randint(0, target_len - len(audio))
            audio = np.pad(audio, (pad_before, target_len - len(audio) - pad_before))
        else:
            audio = audio[:target_len]

        save_wav(audio, output_path)
        os.unlink(tmp_path)
        return True

    except Exception as e:
        print(f"  Coqui TTS error: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Generate synthetic wake word data")
    parser.add_argument("--keyword", default="hey vox", help="Wake word phrase")
    parser.add_argument("--count", type=int, default=500, help="Number of samples to generate")
    parser.add_argument("--output", default="training/synthetic/", help="Output directory")
    parser.add_argument("--augment-only", help="Directory of existing clips to augment (creates variations)")
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    existing = list(output_dir.glob("*.wav"))
    start_idx = len(existing)
    remaining = args.count - start_idx

    if remaining <= 0:
        print(f"Already have {len(existing)} synthetic samples. Done!")
        return

    print(f"\nGenerating {remaining} synthetic samples for \"{args.keyword}\"")
    print(f"Output: {output_dir}/\n")

    # Check available backends
    voices = get_macos_voices()
    print(f"macOS voices: {len(voices)} ({', '.join(voices[:5])}{'...' if len(voices) > 5 else ''})")

    has_coqui = False
    try:
        import TTS
        has_coqui = True
        print("Coqui TTS: available")
    except ImportError:
        print("Coqui TTS: not installed (using macOS say only)")

    # Generate augmented versions of existing real recordings if available
    if args.augment_only:
        augment_dir = Path(args.augment_only)
        real_clips = list(augment_dir.glob("*.wav"))
        if real_clips:
            print(f"\nAugmenting {len(real_clips)} real recordings...")
            aug_count = 0
            for clip_path in real_clips:
                if aug_count >= remaining:
                    break
                audio, sr = load_wav(str(clip_path))
                audio = resample(audio, sr, SAMPLE_RATE)
                # Create 3-5 augmented versions of each real recording
                for j in range(random.randint(3, 5)):
                    if aug_count >= remaining:
                        break
                    augmented = apply_random_augmentation(audio)
                    idx = start_idx + aug_count
                    filename = f"aug_{args.keyword.replace(' ', '_')}_{idx:04d}.wav"
                    save_wav(augmented, str(output_dir / filename))
                    aug_count += 1
            print(f"  Created {aug_count} augmented clips")
            remaining -= aug_count
            start_idx += aug_count

    if remaining <= 0:
        print("Done!")
        return

    # Generate TTS samples
    generated = 0
    coqui_speaker_idx = 0

    for i in range(remaining):
        idx = start_idx + i
        # Alternate between backends, prefer Coqui for diversity
        use_coqui = has_coqui and (i % 3 != 0)  # 2/3 Coqui, 1/3 macOS say

        if use_coqui:
            filename = f"coqui_{args.keyword.replace(' ', '_')}_{idx:04d}.wav"
            filepath = output_dir / filename
            ok = generate_with_coqui(args.keyword, str(filepath), coqui_speaker_idx)
            coqui_speaker_idx += 1
        else:
            voice = random.choice(voices)
            filename = f"say_{voice}_{args.keyword.replace(' ', '_')}_{idx:04d}.wav"
            filepath = output_dir / filename
            ok = generate_with_say(args.keyword, voice, str(filepath))

        if ok:
            generated += 1
            if generated % 50 == 0:
                print(f"  Progress: {generated}/{remaining}")
        else:
            print(f"  Failed to generate clip {idx}")

    print(f"\nGenerated {generated} synthetic samples")
    total = start_idx + generated
    print(f"Total synthetic samples: {total}")


if __name__ == "__main__":
    main()
