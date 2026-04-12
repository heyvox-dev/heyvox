# Custom Wake Word Training Pipeline

Train a personal "Hey Vox" wake word model for openwakeword.

## Prerequisites

```bash
pip install openwakeword sounddevice soundfile numpy scipy onnx onnxruntime
pip install datasets   # For downloading Common Voice negatives
pip install TTS        # Coqui TTS for synthetic data generation (optional)
```

## Quick Start (Recommended Pipeline)

```bash
# 1. Record your voice (75+ clips)
python training/record_samples.py --keyword "hey vox" --count 75 --output training/recordings/

# 2. Generate synthetic positives (500 clips, multiple TTS voices)
python training/generate_synthetic.py --keyword "hey vox" --count 500 --output training/synthetic/

# 3. Download real-world negatives (Common Voice, confusables, noise)
python training/download_negatives.py --output-dir training/negatives_real/

# 4. Record ambient audio from YOUR environment
python training/record_negatives.py --ambient-duration 600 --output training/negatives/

# 5. Train with all data
python training/train_model.py \
    --positive-dir training/recordings/ training/synthetic/ \
    --negative-dir training/negatives/ training/negatives_real/ \
    --output models/hey_vox.onnx \
    --epochs 200 --false-weight 5.0

# 6. Test
python training/test_model.py --model models/hey_vox.onnx --threshold 0.5
```

## Pipeline Details

### Step 1: Record personal samples (75+ minimum)

```bash
python training/record_samples.py --keyword "hey vox" --count 75 --output training/recordings/
```

Terminal UI guides you through varied conditions: normal, loud, quiet, fast, slow,
far from mic, close to mic. More clips = better speaker model.

**For multi-speaker training**: Ask friends to record via the web recorder
(see `web-recorder/` or record.felberer.at), then add their clips here.

### Step 2: Generate synthetic training data

```bash
python training/generate_synthetic.py --keyword "hey vox" --count 500 --output training/synthetic/
```

500+ clips using multiple TTS voices with pitch/speed augmentation.

### Step 3: Download real-world negative samples

**This is the most important step for reducing false activations.**

```bash
python training/download_negatives.py
```

Downloads/generates three types of negatives:

| Source | Count | What |
|--------|-------|------|
| Common Voice (FR, DE, EN, ES) | ~8,000 | Real human speech in 4 languages |
| Confusable phrases | ~100 | "hey fox", "hey box", "hey siri", etc. via macOS `say` |
| Synthetic noise | ~600 | Office ambience, music-like, street noise |

Options:
```bash
--clips-per-language 2000  # Default 2000 per language
--skip-commonvoice         # Skip HuggingFace download (needs `datasets` package)
--skip-confusables         # Skip macOS `say` generation
--skip-noise               # Skip synthetic noise
--output-dir DIR           # Default: training/negatives_real/
```

### Step 4: Record ambient audio from your environment

```bash
python training/record_negatives.py --ambient-duration 600 --output training/negatives/
```

Record your actual workspace sounds (typing, fan, music, conversation). Run
multiple times in different conditions. Also generates TTS speech negatives.

### Step 5: Train the model

```bash
python training/train_model.py \
    --positive-dir training/recordings/ training/synthetic/ \
    --negative-dir training/negatives/ training/negatives_real/ \
    --output models/hey_vox.onnx
```

| Flag | Default | Description |
|------|---------|-------------|
| `--epochs` | 200 | Total training epochs across 3 sequences |
| `--false-weight` | 5.0 | FP penalty weight (higher = fewer false triggers, more missed wake words) |
| `--negative-ratio` | 10.0 | Max neg:pos window ratio (subsamples if exceeded) |
| `--lr` | 0.001 | Initial learning rate |
| `--patience` | 20 | Early stopping patience per sequence |

**3-sequence training** (like openwakeword's official pipeline):
- **Seq 1** (80% of epochs): FP weight ramps 1.0 → configured value
- **Seq 2** (15%): LR/10, doubles FP weight if val FPR > 1%
- **Seq 3** (5%): LR/100, doubles again if needed

**Checkpoint averaging**: Keeps top-5 models by FPR (with recall > 90%),
averages their weights for the final model. Improves robustness.

### Step 6: Configure HeyVox

Copy the model and update config:

```bash
cp models/hey_vox.onnx ~/.config/heyvox/models/hey_vox.onnx
```

In `~/.config/heyvox/config.yaml`:
```yaml
wake_words:
  start: hey_vox
  stop: hey_vox
  model_thresholds:
    hey_vox: 0.7
```

## Iterating on False Positives

If you still get false activations:

1. **Check the log**: `grep TRIGGER /tmp/heyvox.log` — what scores are the false triggers getting?
2. **Record ambient audio** in the conditions where false positives happen
3. **Add confusable phrases** in the language that's causing triggers
4. **Increase `--false-weight`** to 10.0 or 15.0
5. **Raise the threshold** in config (0.7 → 0.8 → 0.85)
6. Re-train and test

## Multi-Speaker Training

For better generalization beyond your voice:

1. Share the web recorder link with friends (record.felberer.at)
2. Each person records 20+ "hey vox" clips
3. Download and extract to `training/recordings_friends/`
4. Include in training: `--positive-dir training/recordings/ training/recordings_friends/ training/synthetic/`

More diverse speakers = fewer false triggers from non-wake-word speech.
