# Custom Wake Word Training Pipeline

Train a personal "Hey Vox" wake word model for openwakeword.

## Prerequisites

```bash
pip install openwakeword sounddevice soundfile numpy scipy
pip install TTS  # Coqui TTS for synthetic data generation (optional)
```

## Steps

### 1. Record personal samples (75 minimum)

```bash
python training/record_samples.py --keyword "hey vox" --count 75 --output training/recordings/
```

This opens a terminal UI that guides you through recording 75 clips of "Hey Vox"
with varied speed, volume, and tone.

### 2. Generate synthetic training data

```bash
python training/generate_synthetic.py --keyword "hey vox" --count 500 --output training/synthetic/
```

Generates 500 synthetic "Hey Vox" clips using multiple TTS voices with pitch/speed
augmentation.

### 3. Record and generate negative samples

This is the most important step for reducing false activations. The model needs
to hear what "not the wake word" sounds like — ambient noise, typing, speech
that does NOT contain "hey vox", and confusable phrases.

```bash
# Full pipeline: 5 minutes of ambient audio + TTS speech negatives
python training/record_negatives.py --output training/negatives/

# Record 10 minutes of ambient audio for noisier environments
python training/record_negatives.py --ambient-duration 600 --output training/negatives/

# TTS speech negatives only (no microphone required)
python training/record_negatives.py --skip-ambient --output training/negatives/

# Ambient recording only (no TTS)
python training/record_negatives.py --skip-tts --output training/negatives/
```

The script generates:
- **Ambient clips**: Your actual environment (typing, fan, music, conversation)
  split into overlapping 2-second clips
- **TTS speech clips**: Common English phrases via macOS `say` — greetings,
  dev terminology, confusable words ("next box", "gray fox"), and other
  wake-word-assistant triggers ("alexa", "siri", "okay google")
- **Augmented copies**: Each clip gets pitch/speed/noise/volume augmentation

**Tip**: Run the ambient recording multiple times in different conditions — with
music playing, while typing, during a video call (muted), with a fan on.

### 4. Train the model

```bash
python training/train_model.py \
    --positive-dir training/recordings/ training/synthetic/ \
    --negative-dir training/negatives/ \
    --output models/hey_vox.onnx \
    --epochs 200
```

Key training parameters:

| Flag | Default | Description |
|------|---------|-------------|
| `--epochs` | 200 | Maximum training epochs |
| `--negative-ratio` | 10.0 | Ratio of negative to positive samples (supplements real negatives with synthetic if needed) |
| `--false-weight` | 3.0 | Extra penalty on false positives (higher = fewer false activations, more missed wake words) |
| `--patience` | 20 | Early stopping: stop training if validation loss doesn't improve for this many epochs |

The trainer will:
- Use your real negative samples from `--negative-dir`
- Supplement with synthetic negatives (noise/sine waves) if the ratio isn't met
- Apply weighted loss so false positives cost 3x more than false negatives
- Stop early if the model converges before reaching max epochs
- Print false positive rate (FPR) on the validation set every 10 epochs

### 5. Test the model

```bash
python training/test_model.py --model models/hey_vox.onnx --threshold 0.5
```

### 6. Configure HeyVox

In `~/.config/heyvox/config.yaml`:

```yaml
wake_word:
  start: hey_vox
  stop: hey_vox
```

The model will be loaded from `models/hey_vox.onnx` automatically.

## Iterating on False Positives

If you still get false activations after training:

1. **Record more ambient audio** in the conditions where false positives happen
2. **Increase `--false-weight`** to 5.0 or even 10.0
3. **Increase `--negative-ratio`** to 15.0 or 20.0
4. **Add confusable phrases** to `record_negatives.py` that sound like your wake word
5. Re-train and test
