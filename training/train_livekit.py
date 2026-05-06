#!/usr/bin/env python3
"""Train hey_vox wake word using livekit-wakeword conv-attention pipeline.

Runs the full pipeline:
1. Setup — download VITS TTS model, ACAV100M features, RIRs, backgrounds
2. Generate — synthesize positive + adversarial negative clips via VITS TTS
3. Inject — copy real recordings into positive_train/ alongside synthetic clips
4. Augment — apply noise, RIR, EQ augmentation to all clips
5. Extract — compute Google speech embeddings (16x96 features)
6. Train — 3-phase adaptive training of conv-attention classifier
7. Export — ONNX model export
8. Eval — DET curve, FPPH, recall metrics

Usage:
    python training/train_livekit.py                    # full pipeline
    python training/train_livekit.py --skip-setup       # skip downloading deps
    python training/train_livekit.py --skip-generate    # skip TTS synthesis (reuse existing)
    python training/train_livekit.py --skip-acav        # skip 16GB ACAV100M download
"""

import argparse
import logging
import re
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("train_livekit")

TRAINING_DIR = Path(__file__).parent
PROJECT_ROOT = TRAINING_DIR.parent

# Real recording sources
RECORDINGS_SELF = TRAINING_DIR / "recordings"
RECORDINGS_FRIENDS = TRAINING_DIR / "recordings_friends"
NEGATIVES_COMMON_VOICE = TRAINING_DIR / "negatives"
NEGATIVES_REAL = TRAINING_DIR / "negatives_real"
NEGATIVES_MINED = Path.home() / ".config" / "heyvox" / "negatives"
POSITIVES_MINED = Path.home() / ".config" / "heyvox" / "positives"

# Oversampling: duplicate real recordings so they're ~50% of training set
REAL_OVERSAMPLE_FACTOR = 20


def inject_real_recordings(output_dir: Path, model_name: str) -> None:
    """Copy real WAV recordings into the generated clip directories.

    Real recordings are renamed to clip_NNNNNN.wav starting after the last
    synthetic clip index, so the augmentation pipeline processes them identically.
    """
    import soundfile as sf

    model_dir = output_dir / model_name
    positive_train = model_dir / "positive_train"
    positive_test = model_dir / "positive_test"
    negative_train = model_dir / "negative_train"

    # Collect all real positive recordings (manual + auto-mined)
    real_positives = []
    for src_dir in [RECORDINGS_SELF, RECORDINGS_FRIENDS, POSITIVES_MINED]:
        if src_dir.exists():
            wavs = sorted(src_dir.glob("*.wav"))
            real_positives.extend(wavs)
            logger.info(f"Found {len(wavs)} recordings in {src_dir}")

    if not real_positives:
        logger.warning("No real recordings found — training on synthetic only")
        return

    # Find next clip index in positive_train
    clip_re = re.compile(r"^clip_(\d{6})\.wav$")
    existing_indices = []
    if positive_train.exists():
        for f in positive_train.iterdir():
            m = clip_re.match(f.name)
            if m:
                existing_indices.append(int(m.group(1)))

    next_idx = max(existing_indices, default=-1) + 1

    # Split: 80% train, 20% test
    import random
    random.seed(42)
    shuffled = real_positives.copy()
    random.shuffle(shuffled)
    split_point = int(len(shuffled) * 0.8)
    train_recordings = shuffled[:split_point]
    test_recordings = shuffled[split_point:]

    # Inject into positive_train with oversampling
    positive_train.mkdir(parents=True, exist_ok=True)
    train_idx = next_idx
    total_injected = 0
    for wav_path in train_recordings:
        audio, sr = sf.read(str(wav_path))
        if audio.ndim > 1:
            audio = audio[:, 0]
        # Resample to 16kHz if needed
        if sr != 16000:
            import librosa
            audio = librosa.resample(audio, orig_sr=sr, target_sr=16000)
            sr = 16000
        # Write REAL_OVERSAMPLE_FACTOR copies of each real recording
        for _dup in range(REAL_OVERSAMPLE_FACTOR):
            dst = positive_train / f"clip_{train_idx:06d}.wav"
            sf.write(str(dst), audio, sr)
            train_idx += 1
            total_injected += 1
    logger.info(f"Injected {len(train_recordings)} real recordings x{REAL_OVERSAMPLE_FACTOR} = {total_injected} clips into positive_train (indices {next_idx}-{train_idx-1})")

    # Inject into positive_test
    positive_test.mkdir(parents=True, exist_ok=True)
    test_clip_re_existing = []
    if positive_test.exists():
        for f in positive_test.iterdir():
            m = clip_re.match(f.name)
            if m:
                test_clip_re_existing.append(int(m.group(1)))
    test_next_idx = max(test_clip_re_existing, default=-1) + 1

    for wav_path in test_recordings:
        audio, sr = sf.read(str(wav_path))
        if audio.ndim > 1:
            audio = audio[:, 0]
        if sr != 16000:
            import librosa
            audio = librosa.resample(audio, orig_sr=sr, target_sr=16000)
            sr = 16000
        dst = positive_test / f"clip_{test_next_idx:06d}.wav"
        sf.write(str(dst), audio, sr)
        test_next_idx += 1
    logger.info(f"Injected {len(test_recordings)} real recordings into positive_test")

    # Inject Common Voice negatives + mined negatives into negative_train
    real_negatives = []
    for neg_dir in [NEGATIVES_COMMON_VOICE, NEGATIVES_REAL / "confusables", NEGATIVES_MINED]:
        if neg_dir.exists():
            wavs = sorted(neg_dir.glob("*.wav"))
            real_negatives.extend(wavs)
            logger.info(f"Found {len(wavs)} negative recordings in {neg_dir}")

    if real_negatives:
        negative_train.mkdir(parents=True, exist_ok=True)
        neg_existing = []
        for f in negative_train.iterdir():
            m = clip_re.match(f.name)
            if m:
                neg_existing.append(int(m.group(1)))
        neg_next_idx = max(neg_existing, default=-1) + 1

        for wav_path in real_negatives:
            audio, sr = sf.read(str(wav_path))
            if audio.ndim > 1:
                audio = audio[:, 0]
            if sr != 16000:
                import librosa
                audio = librosa.resample(audio, orig_sr=sr, target_sr=16000)
                sr = 16000
            dst = negative_train / f"clip_{neg_next_idx:06d}.wav"
            sf.write(str(dst), audio, sr)
            neg_next_idx += 1
        logger.info(f"Injected {len(real_negatives)} real negatives into negative_train")


def main():
    parser = argparse.ArgumentParser(description="Train hey_vox with livekit-wakeword")
    parser.add_argument("--config", default=str(TRAINING_DIR / "hey_vox_livekit.yaml"))
    parser.add_argument("--skip-setup", action="store_true", help="Skip downloading dependencies")
    parser.add_argument("--skip-generate", action="store_true", help="Skip TTS synthesis")
    parser.add_argument("--skip-acav", action="store_true", help="Skip ACAV100M download (16GB)")
    parser.add_argument("--inject-only", action="store_true", help="Only inject real recordings, then stop")
    args = parser.parse_args()

    from livekit.wakeword.config import load_config

    config = load_config(args.config)
    logger.info(f"Config: {config.model_name}, model={config.model.model_type.value}/{config.model.model_size.value}")
    logger.info(f"Steps: {config.steps}, target FPPH: {config.target_fp_per_hour}")

    output_dir = Path(config.output_dir)

    # Step 1: Setup (download VITS model, ACAV100M, RIRs, backgrounds)
    if not args.skip_setup:
        logger.info("=== Step 1: Downloading dependencies ===")
        # Call setup logic directly (it's a typer command, so invoke via CLI)
        import subprocess
        import sys
        cmd = [sys.executable, "-m", "livekit.wakeword", "setup", "--data-dir", config.data_dir]
        if args.skip_acav:
            cmd.append("--skip-acav")
        subprocess.run(cmd, check=True)

    # Step 2: Generate synthetic clips
    if not args.skip_generate:
        from livekit.wakeword.data import run_generate
        logger.info("=== Step 2: Generating synthetic clips ===")
        run_generate(config)

    # Step 3: Inject real recordings
    logger.info("=== Step 3: Injecting real recordings ===")
    inject_real_recordings(output_dir, config.model_name)

    if args.inject_only:
        logger.info("Inject-only mode — stopping here")
        return

    # Step 4: Augment
    from livekit.wakeword.data import run_augment
    logger.info("=== Step 4: Augmenting clips ===")
    run_augment(config)

    # Step 5: Extract features
    from livekit.wakeword.data import run_extraction
    logger.info("=== Step 5: Extracting features ===")
    run_extraction(config)

    # Step 6: Train
    from livekit.wakeword.training.trainer import run_train
    logger.info("=== Step 6: Training conv-attention model ===")
    run_train(config)

    # Step 7: Export ONNX
    from livekit.wakeword.export.onnx import run_export
    logger.info("=== Step 7: Exporting ONNX model ===")
    onnx_path = run_export(config)

    # Step 8: Evaluate
    from livekit.wakeword.eval.evaluate import run_eval
    logger.info("=== Step 8: Evaluating model ===")
    results = run_eval(config, onnx_path)
    logger.info(
        f"Results: AUT={results['aut']:.4f}  FPPH={results['fpph']:.2f}  "
        f"Recall={results['recall']:.1%}  Threshold={results['threshold']:.2f}"
    )

    # Report output location
    onnx_path = config.model_output_dir / f"{config.model_name}.onnx"
    if onnx_path.exists():
        size_kb = onnx_path.stat().st_size / 1024
        logger.info(f"\n{'='*60}")
        logger.info(f"Model ready: {onnx_path} ({size_kb:.0f} KB)")
        logger.info(f"Deploy: cp {onnx_path} ~/.config/heyvox/models/")
        logger.info(f"{'='*60}")


if __name__ == "__main__":
    main()
