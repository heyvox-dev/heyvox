"""
Train a custom openwakeword model for "Hey Vox".

Usage:
    python training/train_model.py \
        --positive-dir training/recordings/ training/synthetic/ \
        --output models/hey_vox.onnx \
        --epochs 50

This script:
1. Loads positive samples (wake word clips) from one or more directories
2. Generates negative samples from background audio / silence / common speech
3. Trains an openwakeword-compatible ONNX keyword spotter model
4. Exports the model ready for use with heyvox

The trained model is a small neural network that classifies 1280-sample
(80ms @ 16kHz) audio frames as containing the wake word or not, operating
on top of Google's speech embedding model (used by openwakeword).
"""

import argparse
import os
import sys
import wave
from pathlib import Path

import numpy as np


SAMPLE_RATE = 16000
FRAME_SIZE = 1280  # 80ms frames, same as openwakeword


def load_wav(path: str) -> np.ndarray:
    """Load WAV file as float32 array at 16kHz."""
    with wave.open(path, "rb") as wf:
        sr = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
        audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32767.0
    if sr != SAMPLE_RATE:
        ratio = SAMPLE_RATE / sr
        new_len = int(len(audio) * ratio)
        indices = np.linspace(0, len(audio) - 1, new_len)
        audio = np.interp(indices, np.arange(len(audio)), audio).astype(np.float32)
    return audio


def collect_clips(dirs: list[str]) -> list[np.ndarray]:
    """Collect all WAV clips from directories."""
    clips = []
    for d in dirs:
        p = Path(d)
        if not p.exists():
            print(f"WARNING: Directory {d} does not exist, skipping")
            continue
        wav_files = sorted(p.glob("*.wav"))
        for f in wav_files:
            try:
                audio = load_wav(str(f))
                clips.append(audio)
            except Exception as e:
                print(f"WARNING: Could not load {f}: {e}")
    return clips


def generate_negative_samples(count: int, clip_length: int = SAMPLE_RATE * 2) -> list[np.ndarray]:
    """Generate negative samples (silence, noise, random speech-like audio)."""
    negatives = []
    for i in range(count):
        kind = i % 3
        if kind == 0:
            # Silence with slight noise
            audio = np.random.randn(clip_length).astype(np.float32) * 0.001
        elif kind == 1:
            # Random noise (like ambient room)
            audio = np.random.randn(clip_length).astype(np.float32) * 0.02
        else:
            # Simulated speech-like signal (random frequencies)
            t = np.linspace(0, 2.0, clip_length)
            freqs = np.random.uniform(100, 800, size=5)
            audio = sum(np.sin(2 * np.pi * f * t) * np.random.uniform(0.01, 0.05) for f in freqs)
            audio = audio.astype(np.float32)
        negatives.append(audio)
    return negatives


def extract_embeddings(clips: list[np.ndarray]) -> np.ndarray:
    """Extract openwakeword-compatible embeddings from audio clips.

    Uses the same preprocessing pipeline as openwakeword:
    1. Compute mel spectrogram
    2. Pass through Google's speech embedding model

    Returns array of shape (N, embedding_dim).
    """
    try:
        import onnxruntime as ort
    except ImportError:
        print("ERROR: onnxruntime required. Install with: pip install onnxruntime")
        sys.exit(1)

    # openwakeword bundles the embedding model; we need to find it
    try:
        import openwakeword
        oww_dir = Path(openwakeword.__file__).parent
        # Look for the embedding model
        embedding_model_path = None
        for name in ["embedding_model.onnx", "melspectrogram.onnx"]:
            candidate = oww_dir / "resources" / name
            if candidate.exists():
                embedding_model_path = str(candidate)
                break

        if embedding_model_path is None:
            # Try to find via openwakeword's model utils
            from openwakeword.utils import download_models
            download_models()
            for name in ["embedding_model.onnx"]:
                candidate = oww_dir / "resources" / name
                if candidate.exists():
                    embedding_model_path = str(candidate)
                    break
    except ImportError:
        print("ERROR: openwakeword required. Install with: pip install openwakeword")
        sys.exit(1)

    if embedding_model_path is None:
        print("ERROR: Could not find openwakeword embedding model")
        sys.exit(1)

    print(f"Using embedding model: {embedding_model_path}")

    # Load melspectrogram model
    melspec_path = oww_dir / "resources" / "melspectrogram.onnx"
    if not melspec_path.exists():
        print("ERROR: melspectrogram.onnx not found in openwakeword resources")
        sys.exit(1)

    mel_session = ort.InferenceSession(str(melspec_path))
    emb_session = ort.InferenceSession(embedding_model_path)

    embeddings = []
    for i, clip in enumerate(clips):
        # Pad/truncate to 2 seconds
        target_len = SAMPLE_RATE * 2
        if len(clip) < target_len:
            clip = np.pad(clip, (0, target_len - len(clip)))
        else:
            clip = clip[:target_len]

        # Process through mel spectrogram
        mel_input = clip.reshape(1, -1).astype(np.float32)
        mel_out = mel_session.run(None, {mel_session.get_inputs()[0].name: mel_input})[0]

        # Process through embedding model
        emb_out = emb_session.run(None, {emb_session.get_inputs()[0].name: mel_out})[0]
        embeddings.append(emb_out.flatten())

        if (i + 1) % 100 == 0:
            print(f"  Extracted embeddings: {i + 1}/{len(clips)}")

    return np.array(embeddings, dtype=np.float32)


def train_classifier(
    pos_embeddings: np.ndarray,
    neg_embeddings: np.ndarray,
    epochs: int = 50,
    lr: float = 0.001,
) -> tuple:
    """Train a simple MLP classifier on embeddings.

    Returns (weights, biases) for a 2-layer MLP that can be exported to ONNX.
    """
    # Simple 2-layer MLP: embedding_dim -> 64 -> 1
    emb_dim = pos_embeddings.shape[1]

    # Prepare dataset
    X = np.vstack([pos_embeddings, neg_embeddings])
    y = np.concatenate([
        np.ones(len(pos_embeddings)),
        np.zeros(len(neg_embeddings)),
    ])

    # Shuffle
    idx = np.random.permutation(len(X))
    X, y = X[idx], y[idx]

    # Split train/val (90/10)
    split = int(0.9 * len(X))
    X_train, X_val = X[:split], X[split:]
    y_train, y_val = y[:split], y[split:]

    # Initialize weights (Xavier)
    hidden_size = 64
    W1 = np.random.randn(emb_dim, hidden_size).astype(np.float32) * np.sqrt(2.0 / emb_dim)
    b1 = np.zeros(hidden_size, dtype=np.float32)
    W2 = np.random.randn(hidden_size, 1).astype(np.float32) * np.sqrt(2.0 / hidden_size)
    b2 = np.zeros(1, dtype=np.float32)

    def forward(X_batch):
        h = np.maximum(0, X_batch @ W1 + b1)  # ReLU
        logits = h @ W2 + b2
        return 1.0 / (1.0 + np.exp(-np.clip(logits.flatten(), -500, 500)))  # Sigmoid

    def binary_cross_entropy(probs, labels):
        eps = 1e-7
        probs = np.clip(probs, eps, 1 - eps)
        return -np.mean(labels * np.log(probs) + (1 - labels) * np.log(1 - probs))

    print(f"\nTraining classifier: {emb_dim} -> {hidden_size} -> 1")
    print(f"  Positive: {len(pos_embeddings)}, Negative: {len(neg_embeddings)}")
    print(f"  Train: {len(X_train)}, Val: {len(X_val)}")

    best_val_loss = float("inf")
    best_weights = None
    batch_size = 32

    for epoch in range(epochs):
        # Mini-batch SGD
        perm = np.random.permutation(len(X_train))
        epoch_loss = 0
        n_batches = 0

        for start in range(0, len(X_train), batch_size):
            end = min(start + batch_size, len(X_train))
            batch_idx = perm[start:end]
            X_b = X_train[batch_idx]
            y_b = y_train[batch_idx]

            # Forward
            h = np.maximum(0, X_b @ W1 + b1)
            logits = h @ W2 + b2
            probs = 1.0 / (1.0 + np.exp(-np.clip(logits.flatten(), -500, 500)))

            # Backward
            dlogits = (probs - y_b).reshape(-1, 1) / len(y_b)
            dW2 = h.T @ dlogits
            db2 = dlogits.sum(axis=0)
            dh = dlogits @ W2.T
            dh[h <= 0] = 0  # ReLU gradient
            dW1 = X_b.T @ dh
            db1 = dh.sum(axis=0)

            # Update
            W1 -= lr * dW1
            b1 -= lr * db1
            W2 -= lr * dW2
            b2 -= lr * db2

            epoch_loss += binary_cross_entropy(probs, y_b)
            n_batches += 1

        # Validation
        val_probs = forward(X_val)
        val_loss = binary_cross_entropy(val_probs, y_val)
        val_acc = np.mean((val_probs > 0.5) == y_val)

        if val_loss < best_val_loss:
            best_val_loss = val_loss
            best_weights = (W1.copy(), b1.copy(), W2.copy(), b2.copy())

        if (epoch + 1) % 10 == 0:
            print(f"  Epoch {epoch + 1}/{epochs}: "
                  f"train_loss={epoch_loss / n_batches:.4f}, "
                  f"val_loss={val_loss:.4f}, val_acc={val_acc:.3f}")

    return best_weights


def export_onnx(weights: tuple, emb_dim: int, output_path: str) -> None:
    """Export trained classifier as ONNX model."""
    try:
        import onnx
        from onnx import TensorProto, helper
    except ImportError:
        print("ERROR: onnx package required for export. Install with: pip install onnx")
        sys.exit(1)

    W1, b1, W2, b2 = weights
    hidden_size = W1.shape[1]

    # Build ONNX graph
    X = helper.make_tensor_value_info("input", TensorProto.FLOAT, [1, emb_dim])
    Y = helper.make_tensor_value_info("output", TensorProto.FLOAT, [1, 1])

    w1_init = helper.make_tensor("W1", TensorProto.FLOAT, W1.shape, W1.flatten().tolist())
    b1_init = helper.make_tensor("b1", TensorProto.FLOAT, [1, hidden_size], b1.tolist())
    w2_init = helper.make_tensor("W2", TensorProto.FLOAT, W2.shape, W2.flatten().tolist())
    b2_init = helper.make_tensor("b2", TensorProto.FLOAT, [1, 1], b2.tolist())

    nodes = [
        helper.make_node("MatMul", ["input", "W1"], ["mm1"]),
        helper.make_node("Add", ["mm1", "b1"], ["h1"]),
        helper.make_node("Relu", ["h1"], ["relu1"]),
        helper.make_node("MatMul", ["relu1", "W2"], ["mm2"]),
        helper.make_node("Add", ["mm2", "b2"], ["logits"]),
        helper.make_node("Sigmoid", ["logits"], ["output"]),
    ]

    graph = helper.make_graph(
        nodes,
        "hey_vox_detector",
        [X],
        [Y],
        initializer=[w1_init, b1_init, w2_init, b2_init],
    )

    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 7

    onnx.save(model, output_path)
    print(f"\nModel exported to: {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Train custom wake word model")
    parser.add_argument("--positive-dir", nargs="+", required=True, help="Directories with positive samples")
    parser.add_argument("--negative-dir", nargs="*", help="Directories with negative samples (optional)")
    parser.add_argument("--output", default="models/hey_vox.onnx", help="Output ONNX model path")
    parser.add_argument("--epochs", type=int, default=50, help="Training epochs")
    parser.add_argument("--negative-ratio", type=float, default=3.0, help="Ratio of negative to positive samples")
    args = parser.parse_args()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Collect positive samples
    print("Loading positive samples...")
    pos_clips = collect_clips(args.positive_dir)
    if len(pos_clips) < 10:
        print(f"ERROR: Only {len(pos_clips)} positive clips found. Need at least 10.")
        print("Run record_samples.py and/or generate_synthetic.py first.")
        sys.exit(1)
    print(f"  Loaded {len(pos_clips)} positive clips")

    # Collect or generate negative samples
    neg_count = int(len(pos_clips) * args.negative_ratio)
    if args.negative_dir:
        print("Loading negative samples...")
        neg_clips = collect_clips(args.negative_dir)
        # Supplement with generated if not enough
        if len(neg_clips) < neg_count:
            extra = generate_negative_samples(neg_count - len(neg_clips))
            neg_clips.extend(extra)
    else:
        print(f"Generating {neg_count} negative samples...")
        neg_clips = generate_negative_samples(neg_count)
    print(f"  Using {len(neg_clips)} negative clips")

    # Extract embeddings
    print("\nExtracting embeddings (this may take a while)...")
    pos_emb = extract_embeddings(pos_clips)
    neg_emb = extract_embeddings(neg_clips)
    print(f"  Embedding shape: {pos_emb.shape[1]}")

    # Train classifier
    weights = train_classifier(pos_emb, neg_emb, epochs=args.epochs)

    # Export ONNX
    export_onnx(weights, pos_emb.shape[1], str(output_path))

    # Summary
    print(f"\nTraining complete!")
    print(f"  Model: {output_path}")
    print(f"  Positive samples: {len(pos_clips)}")
    print(f"  Negative samples: {len(neg_clips)}")
    print(f"\nTo use with HeyVox, update ~/.config/heyvox/config.yaml:")
    print(f"  wake_word:")
    print(f"    start: hey_vox")
    print(f"    stop: hey_vox")


if __name__ == "__main__":
    main()
