# Custom Wake Word Training Pipeline

Train a personal "Hey Vox" wake word model for openwakeword.

## Prerequisites

```bash
pip install openwakeword sounddevice soundfile numpy scipy
pip install TTS  # Coqui TTS for synthetic data generation
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

### 3. Train the model

```bash
python training/train_model.py \
    --positive-dir training/recordings/ training/synthetic/ \
    --output models/hey_vox.onnx \
    --epochs 50
```

### 4. Test the model

```bash
python training/test_model.py --model models/hey_vox.onnx --threshold 0.5
```

### 5. Configure HeyVox

In `~/.config/heyvox/config.yaml`:

```yaml
wake_word:
  start: hey_vox
  stop: hey_vox
```

The model will be loaded from `models/hey_vox.onnx` automatically.
