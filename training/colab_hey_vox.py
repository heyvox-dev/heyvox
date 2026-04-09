# %% [markdown]
# # Train Custom "Hey Vox" Wake Word Model
#
# Uses openwakeword's training pipeline with **license-clean** negative datasets only:
# - Common Voice 16.1 (CC-0) — crowd-sourced speech
# - LibriSpeech train-clean-100 (CC-BY) — read English speech
# - MUSAN noise+music (CC/public domain) — environmental sounds
#
# Output: `hey_vox.onnx` compatible with `openwakeword.Model()`
#
# **Key changes from v1** (which had too many false activations):
# - `max_negative_weight`: 1500 (was 3000 — too aggressive)
# - More negative data diversity (3 datasets instead of 1)
# - `target_fp_per_hour`: 0.2

# %% Install dependencies
# !pip install openwakeword datasets soundfile numpy scipy torchaudio
# !pip install piper-sample-generator  # synthetic positive generation

# %% Configuration
import os

WAKE_WORD = "hey vox"
MODEL_NAME = "hey_vox"

# Training parameters — tuned based on v1 failure analysis
CONFIG = {
    # Positive sample generation
    "n_samples": 50000,           # Synthetic positive clips
    "augmentation_rounds": 2,     # 2x augmentation diversity

    # Training
    "steps": 50000,
    "max_negative_weight": 1500,  # v1 used 3000 → too many false activations
    "target_fp_per_hour": 0.2,    # Strict but not insane
    "learning_rate": 0.001,

    # Negative datasets (all license-clean)
    "negative_datasets": [
        "mozilla-foundation/common_voice_16_1",  # CC-0
        "openslr/librispeech_asr",                # CC-BY-4.0
    ],
}

OUTPUT_DIR = f"/content/{MODEL_NAME}"
os.makedirs(OUTPUT_DIR, exist_ok=True)

print(f"Training '{WAKE_WORD}' wake word model")
print(f"Output: {OUTPUT_DIR}")

# %% Download negative datasets
from datasets import load_dataset
import soundfile as sf
import numpy as np
from pathlib import Path

NEG_DIR = f"{OUTPUT_DIR}/negatives"
os.makedirs(NEG_DIR, exist_ok=True)

# --- Common Voice (CC-0) — diverse crowd-sourced speech ---
print("Downloading Common Voice English subset...")
try:
    cv = load_dataset(
        "mozilla-foundation/common_voice_16_1",
        "en",
        split="train",
        streaming=True,
        trust_remote_code=True,
    )
    cv_count = 0
    for i, sample in enumerate(cv):
        if cv_count >= 5000:
            break
        audio = sample["audio"]["array"]
        sr = sample["audio"]["sampling_rate"]
        # Skip very short clips
        if len(audio) / sr < 1.0:
            continue
        # Skip if transcript contains wake word
        text = sample.get("sentence", "").lower()
        if "hey vox" in text or "heyvox" in text:
            continue
        out_path = f"{NEG_DIR}/cv_{cv_count:05d}.wav"
        sf.write(out_path, audio, sr)
        cv_count += 1
    print(f"  Common Voice: {cv_count} clips saved")
except Exception as e:
    print(f"  Common Voice failed: {e}")
    print("  You may need to accept the dataset license at huggingface.co")
    cv_count = 0

# --- LibriSpeech train-clean-100 (CC-BY) ---
print("Downloading LibriSpeech clean-100...")
try:
    ls = load_dataset(
        "openslr/librispeech_asr",
        "clean",
        split="train.100",
        streaming=True,
        trust_remote_code=True,
    )
    ls_count = 0
    for i, sample in enumerate(ls):
        if ls_count >= 5000:
            break
        audio = sample["audio"]["array"]
        sr = sample["audio"]["sampling_rate"]
        if len(audio) / sr < 1.0:
            continue
        text = sample.get("text", "").lower()
        if "hey vox" in text:
            continue
        out_path = f"{NEG_DIR}/ls_{ls_count:05d}.wav"
        sf.write(out_path, audio, sr)
        ls_count += 1
    print(f"  LibriSpeech: {ls_count} clips saved")
except Exception as e:
    print(f"  LibriSpeech failed: {e}")
    ls_count = 0

# --- MUSAN noise + music (CC / public domain) ---
print("Downloading MUSAN noise/music...")
MUSAN_URL = "https://www.openslr.org/resources/17/musan.tar.gz"
try:
    import urllib.request
    import tarfile

    musan_tar = f"{OUTPUT_DIR}/musan.tar.gz"
    if not os.path.exists(musan_tar):
        urllib.request.urlretrieve(MUSAN_URL, musan_tar)

    musan_count = 0
    with tarfile.open(musan_tar, "r:gz") as tar:
        for member in tar.getmembers():
            if not member.name.endswith(".wav"):
                continue
            # Only noise and music (skip speech — we have enough)
            if "/noise/" not in member.name and "/music/" not in member.name:
                continue
            if musan_count >= 2000:
                break
            f = tar.extractfile(member)
            if f is None:
                continue
            out_path = f"{NEG_DIR}/musan_{musan_count:05d}.wav"
            with open(out_path, "wb") as out:
                out.write(f.read())
            musan_count += 1
    print(f"  MUSAN: {musan_count} clips saved")
except Exception as e:
    print(f"  MUSAN failed: {e}")
    musan_count = 0

total_neg = len(list(Path(NEG_DIR).glob("*.wav")))
print(f"\nTotal negative clips: {total_neg}")

# %% Upload personal recordings (optional but recommended)
# If you have personal "hey vox" recordings, upload them here.
# They significantly improve model quality vs synthetic-only.
#
# from google.colab import files
# uploaded = files.upload()  # Upload WAV files
#
# PERSONAL_DIR = f"{OUTPUT_DIR}/personal_positives"
# os.makedirs(PERSONAL_DIR, exist_ok=True)
# for name, data in uploaded.items():
#     with open(f"{PERSONAL_DIR}/{name}", "wb") as f:
#         f.write(data)
# print(f"Uploaded {len(uploaded)} personal recordings")

# %% Generate synthetic positive samples
import subprocess

SYNTH_DIR = f"{OUTPUT_DIR}/synthetic_positives"
os.makedirs(SYNTH_DIR, exist_ok=True)

print(f"Generating {CONFIG['n_samples']} synthetic '{WAKE_WORD}' samples...")
print("This takes 30-60 minutes on Colab GPU...")

# Use piper-sample-generator for high-quality TTS
# Note: piper struggles with very short words — "hey vox" is borderline.
# We generate extra and filter by duration to remove bad samples.
result = subprocess.run([
    "python", "-m", "piper_sample_generator",
    "--text", WAKE_WORD,
    "--output-dir", SYNTH_DIR,
    "--max-samples", str(CONFIG["n_samples"]),
    "--batch-size", "64",
    "--noise-scales", "0.6", "0.8", "1.0",
    "--length-scales", "0.8", "1.0", "1.2",
], capture_output=True, text=True)

if result.returncode != 0:
    print(f"piper-sample-generator failed: {result.stderr[:500]}")
    print("Falling back to basic TTS...")
    # Fallback: use espeak + augmentation
    for i in range(min(1000, CONFIG["n_samples"])):
        out_path = f"{SYNTH_DIR}/espeak_{i:05d}.wav"
        speed = np.random.randint(130, 200)
        pitch = np.random.randint(30, 70)
        subprocess.run([
            "espeak-ng", "-v", "en-us", "-s", str(speed), "-p", str(pitch),
            "-w", out_path, WAKE_WORD,
        ], capture_output=True)
    print(f"Generated {len(list(Path(SYNTH_DIR).glob('*.wav')))} espeak samples")
else:
    n_generated = len(list(Path(SYNTH_DIR).glob("*.wav")))
    print(f"Generated {n_generated} synthetic samples")

    # Filter out bad samples (too short or too long)
    filtered = 0
    for wav_path in Path(SYNTH_DIR).glob("*.wav"):
        try:
            audio, sr = sf.read(wav_path)
            duration = len(audio) / sr
            # "hey vox" should be ~0.5-2.0 seconds
            if duration < 0.3 or duration > 3.0:
                wav_path.unlink()
                filtered += 1
        except Exception:
            wav_path.unlink()
            filtered += 1
    print(f"Filtered {filtered} bad samples (too short/long)")

n_synth = len(list(Path(SYNTH_DIR).glob("*.wav")))
print(f"Final synthetic positives: {n_synth}")

# %% Augment positive samples
print("Augmenting positive samples...")

AUG_DIR = f"{OUTPUT_DIR}/augmented_positives"
os.makedirs(AUG_DIR, exist_ok=True)

def augment_audio(audio, sr):
    """Apply random augmentation to audio."""
    augmented = audio.copy().astype(np.float32)

    # Pitch shift (±2 semitones via resampling)
    if np.random.random() < 0.5:
        shift = np.random.uniform(-2, 2)
        factor = 2 ** (shift / 12)
        indices = np.arange(0, len(augmented), factor)
        indices = indices[indices < len(augmented)].astype(int)
        augmented = augmented[indices]

    # Speed change (0.85x - 1.15x)
    if np.random.random() < 0.5:
        speed = np.random.uniform(0.85, 1.15)
        indices = np.arange(0, len(augmented), speed)
        indices = indices[indices < len(augmented)].astype(int)
        augmented = augmented[indices]

    # Add noise
    if np.random.random() < 0.4:
        snr_db = np.random.uniform(15, 30)
        noise = np.random.randn(len(augmented)).astype(np.float32)
        signal_power = np.mean(augmented ** 2)
        noise_power = signal_power / (10 ** (snr_db / 10))
        augmented += noise * np.sqrt(noise_power)

    # Volume change (±6 dB)
    if np.random.random() < 0.5:
        gain_db = np.random.uniform(-6, 6)
        augmented *= 10 ** (gain_db / 20)

    # Clip to prevent distortion
    augmented = np.clip(augmented, -1.0, 1.0)
    return augmented

aug_count = 0
for round_idx in range(CONFIG["augmentation_rounds"]):
    for wav_path in Path(SYNTH_DIR).glob("*.wav"):
        audio, sr = sf.read(wav_path)
        augmented = augment_audio(audio, sr)
        out_path = f"{AUG_DIR}/aug_r{round_idx}_{wav_path.stem}.wav"
        sf.write(out_path, augmented, sr)
        aug_count += 1

# Also augment personal recordings if they exist
PERSONAL_DIR = f"{OUTPUT_DIR}/personal_positives"
if os.path.isdir(PERSONAL_DIR):
    for round_idx in range(CONFIG["augmentation_rounds"] * 3):  # 3x more augmentation for personal
        for wav_path in Path(PERSONAL_DIR).glob("*.wav"):
            audio, sr = sf.read(wav_path)
            augmented = augment_audio(audio, sr)
            out_path = f"{AUG_DIR}/aug_personal_r{round_idx}_{wav_path.stem}.wav"
            sf.write(out_path, augmented, sr)
            aug_count += 1

print(f"Generated {aug_count} augmented positive samples")

# %% Train the model
print("=" * 60)
print("TRAINING")
print("=" * 60)

from openwakeword import train

# Collect all positive directories
positive_dirs = [SYNTH_DIR, AUG_DIR]
if os.path.isdir(f"{OUTPUT_DIR}/personal_positives"):
    positive_dirs.append(f"{OUTPUT_DIR}/personal_positives")

print(f"Positive dirs: {positive_dirs}")
print(f"Negative dir: {NEG_DIR}")
print(f"Steps: {CONFIG['steps']}")
print(f"Max negative weight: {CONFIG['max_negative_weight']}")
print(f"Target FP/hour: {CONFIG['target_fp_per_hour']}")

# Train using openwakeword's built-in training
# This trains the full pipeline: feature extraction → classifier
model_path = train.train_custom_model(
    positive_audio_dirs=positive_dirs,
    negative_audio_dirs=[NEG_DIR],
    output_dir=OUTPUT_DIR,
    model_name=MODEL_NAME,
    n_epochs=CONFIG["steps"],
    max_negative_weight=CONFIG["max_negative_weight"],
    target_fp_per_hour=CONFIG["target_fp_per_hour"],
    learning_rate=CONFIG["learning_rate"],
)

print(f"\nModel saved: {model_path}")

# %% Evaluate the model
from openwakeword import Model
import glob

print("=" * 60)
print("EVALUATION")
print("=" * 60)

model = Model(wakeword_models=[model_path])

# Test on negative samples (measure false positive rate)
neg_files = sorted(glob.glob(f"{NEG_DIR}/*.wav"))[:500]
fp_count = 0
for wav_path in neg_files:
    audio, sr = sf.read(wav_path)
    audio_16k = audio.astype(np.float32)
    if sr != 16000:
        # Simple resample
        indices = np.arange(0, len(audio_16k), sr / 16000)
        audio_16k = audio_16k[indices.astype(int)]

    # Feed in chunks (openwakeword expects streaming)
    chunk_size = 1280  # 80ms at 16kHz
    for i in range(0, len(audio_16k) - chunk_size, chunk_size):
        chunk = audio_16k[i:i + chunk_size]
        prediction = model.predict(chunk)
        scores = list(prediction.values())
        if any(s > 0.5 for s in scores):
            fp_count += 1
            break

fpr = fp_count / len(neg_files)
print(f"False positive rate on {len(neg_files)} negative clips: {fpr:.1%}")
print(f"  ({fp_count} false triggers)")

# Test on synthetic positives (measure recall)
pos_files = sorted(glob.glob(f"{SYNTH_DIR}/*.wav"))[:200]
tp_count = 0
for wav_path in pos_files:
    audio, sr = sf.read(wav_path)
    audio_16k = audio.astype(np.float32)
    if sr != 16000:
        indices = np.arange(0, len(audio_16k), sr / 16000)
        audio_16k = audio_16k[indices.astype(int)]

    chunk_size = 1280
    detected = False
    for i in range(0, len(audio_16k) - chunk_size, chunk_size):
        chunk = audio_16k[i:i + chunk_size]
        prediction = model.predict(chunk)
        scores = list(prediction.values())
        if any(s > 0.5 for s in scores):
            detected = True
            break
    if detected:
        tp_count += 1

recall = tp_count / len(pos_files) if pos_files else 0
print(f"Recall on {len(pos_files)} positive clips: {recall:.1%}")
print(f"  ({tp_count} detected)")

print(f"\n{'=' * 60}")
print(f"SUMMARY")
print(f"  FPR:    {fpr:.1%}")
print(f"  Recall: {recall:.1%}")
print(f"  Model:  {model_path}")
print(f"{'=' * 60}")

# %% Download the model
# Uncomment to download from Colab:
# from google.colab import files
# files.download(model_path)
#
# Then place in: ~/.config/heyvox/models/hey_vox.onnx
# Or in the heyvox package: heyvox/models/hey_vox.onnx

print(f"\nTo deploy locally:")
print(f"  1. Download {MODEL_NAME}.onnx from {OUTPUT_DIR}/")
print(f"  2. Copy to ~/.config/heyvox/models/{MODEL_NAME}.onnx")
print(f"  3. Set in config.yaml:")
print(f"     wake_word:")
print(f"       start: {MODEL_NAME}")
print(f"       stop: {MODEL_NAME}")
