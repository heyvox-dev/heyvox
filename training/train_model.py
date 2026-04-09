"""
Train a custom openwakeword model for "Hey Vox".

Usage:
    python training/train_model.py \
        --positive-dir training/recordings/ training/synthetic/ \
        --negative-dir training/negatives/ \
        --output models/hey_vox.onnx \
        --epochs 200 --false-weight 3.0

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


N_WINDOW_FRAMES = 16  # openwakeword uses 16 consecutive embedding frames as input


def extract_embeddings(clips: list[np.ndarray]) -> np.ndarray:
    """Extract openwakeword-compatible embedding windows from audio clips.

    Uses openwakeword's own AudioFeatures class to ensure the mel spectrogram
    → embedding pipeline matches exactly what the inference engine expects.

    Each clip produces one or more sliding windows of N_WINDOW_FRAMES consecutive
    96-dim embedding vectors. This matches the [1, 16, 96] input shape that
    openwakeword's built-in models use at inference time.

    Returns array of shape (N, 16, 96) where N is total windows across all clips.
    """
    try:
        from openwakeword.utils import AudioFeatures
    except ImportError:
        print("ERROR: openwakeword required. Install with: pip install openwakeword")
        sys.exit(1)

    audio_features = AudioFeatures(inference_framework="onnx")
    print("Using openwakeword AudioFeatures for embedding extraction")
    print(f"  Window size: {N_WINDOW_FRAMES} frames of 96-dim embeddings → input shape [1, {N_WINDOW_FRAMES}, 96]")

    all_windows = []
    for i, clip in enumerate(clips):
        # Pad to at least 2 seconds so we get enough embedding frames
        target_len = SAMPLE_RATE * 2
        if len(clip) < target_len:
            clip = np.pad(clip, (0, target_len - len(clip)))
        else:
            clip = clip[:target_len]

        clip_int16 = (clip * 32767).astype(np.int16)

        # Extract frame-level embeddings: shape (n_frames, 96)
        emb = audio_features._get_embeddings(clip_int16)

        # Create sliding windows of N_WINDOW_FRAMES consecutive frames
        n_frames = emb.shape[0]
        if n_frames >= N_WINDOW_FRAMES:
            for start in range(n_frames - N_WINDOW_FRAMES + 1):
                window = emb[start:start + N_WINDOW_FRAMES]  # (16, 96)
                all_windows.append(window)
        else:
            # Pad if too few frames (shouldn't happen with 2s clips)
            padded = np.zeros((N_WINDOW_FRAMES, 96), dtype=np.float32)
            padded[:n_frames] = emb
            all_windows.append(padded)

        if (i + 1) % 100 == 0:
            print(f"  Extracted embeddings: {i + 1}/{len(clips)}")

    result = np.array(all_windows, dtype=np.float32)
    print(f"  Total windows: {len(result)} from {len(clips)} clips")
    return result


def train_classifier(
    pos_windows: np.ndarray,
    neg_windows: np.ndarray,
    epochs: int = 200,
    lr: float = 0.001,
    false_weight: float = 3.0,
    early_stopping_patience: int = 20,
) -> tuple:
    """Train a simple MLP classifier on embedding windows.

    Input windows have shape (N, 16, 96) — matching openwakeword's inference
    format. The model flattens each window to (16*96=1536) before the MLP.

    Args:
        pos_windows: Positive sample embedding windows, shape (N, 16, 96).
        neg_windows: Negative sample embedding windows, shape (M, 16, 96).
        epochs: Maximum training epochs.
        lr: Learning rate.
        false_weight: Extra weight on false positive loss.
        early_stopping_patience: Stop if val_loss doesn't improve.

    Returns (weights, biases) for a 2-layer MLP that can be exported to ONNX.
    """
    # Flatten windows: (N, 16, 96) → (N, 1536)
    flat_dim = pos_windows.shape[1] * pos_windows.shape[2]  # 16 * 96 = 1536
    pos_flat = pos_windows.reshape(len(pos_windows), flat_dim)
    neg_flat = neg_windows.reshape(len(neg_windows), flat_dim)

    X = np.vstack([pos_flat, neg_flat])
    y = np.concatenate([
        np.ones(len(pos_flat)),
        np.zeros(len(neg_flat)),
    ])

    sample_weights = np.ones(len(y), dtype=np.float32)
    sample_weights[y == 0] = false_weight

    idx = np.random.permutation(len(X))
    X, y, sample_weights = X[idx], y[idx], sample_weights[idx]

    split = int(0.9 * len(X))
    X_train, X_val = X[:split], X[split:]
    y_train, y_val = y[:split], y[split:]
    w_train = sample_weights[:split]

    hidden_size = 64
    W1 = np.random.randn(flat_dim, hidden_size).astype(np.float32) * np.sqrt(2.0 / flat_dim)
    b1 = np.zeros(hidden_size, dtype=np.float32)
    W2 = np.random.randn(hidden_size, 1).astype(np.float32) * np.sqrt(2.0 / hidden_size)
    b2 = np.zeros(1, dtype=np.float32)

    def forward(X_batch):
        h = np.maximum(0, X_batch @ W1 + b1)
        logits = h @ W2 + b2
        return 1.0 / (1.0 + np.exp(-np.clip(logits.flatten(), -500, 500)))

    def weighted_bce(probs, labels, weights):
        eps = 1e-7
        probs = np.clip(probs, eps, 1 - eps)
        per_sample = -(labels * np.log(probs) + (1 - labels) * np.log(1 - probs))
        return np.mean(per_sample * weights)

    def binary_cross_entropy(probs, labels):
        eps = 1e-7
        probs = np.clip(probs, eps, 1 - eps)
        return -np.mean(labels * np.log(probs) + (1 - labels) * np.log(1 - probs))

    def compute_fpr(probs, labels):
        neg_mask = labels == 0
        if neg_mask.sum() == 0:
            return 0.0
        fp = np.sum((probs[neg_mask] > 0.5).astype(float))
        return float(fp / neg_mask.sum())

    n_val_pos = int(y_val.sum())
    n_val_neg = int(len(y_val) - n_val_pos)

    print(f"\nTraining classifier: flatten({N_WINDOW_FRAMES}x96={flat_dim}) -> {hidden_size} -> 1")
    print(f"  Positive windows: {len(pos_flat)}, Negative windows: {len(neg_flat)}")
    print(f"  Train: {len(X_train)}, Val: {len(X_val)} ({n_val_pos} pos, {n_val_neg} neg)")
    print(f"  False-positive loss weight: {false_weight}x")
    print(f"  Early stopping patience: {early_stopping_patience} epochs")

    best_val_loss = float("inf")
    best_weights = None
    patience_counter = 0
    batch_size = 64

    for epoch in range(epochs):
        perm = np.random.permutation(len(X_train))
        epoch_loss = 0
        n_batches = 0

        for start in range(0, len(X_train), batch_size):
            end = min(start + batch_size, len(X_train))
            batch_idx = perm[start:end]
            X_b = X_train[batch_idx]
            y_b = y_train[batch_idx]
            w_b = w_train[batch_idx]

            h = np.maximum(0, X_b @ W1 + b1)
            logits = h @ W2 + b2
            probs = 1.0 / (1.0 + np.exp(-np.clip(logits.flatten(), -500, 500)))

            grad_scale = w_b / len(y_b)
            dlogits = ((probs - y_b) * grad_scale).reshape(-1, 1)
            dW2 = h.T @ dlogits
            db2 = dlogits.sum(axis=0)
            dh = dlogits @ W2.T
            dh[h <= 0] = 0
            dW1 = X_b.T @ dh
            db1 = dh.sum(axis=0)

            W1 -= lr * dW1
            b1 -= lr * db1
            W2 -= lr * dW2
            b2 -= lr * db2

            epoch_loss += weighted_bce(probs, y_b, w_b)
            n_batches += 1

        val_probs = forward(X_val)
        val_loss = binary_cross_entropy(val_probs, y_val)
        val_acc = np.mean((val_probs > 0.5) == y_val)
        val_fpr = compute_fpr(val_probs, y_val)

        if val_loss < best_val_loss:
            best_val_loss = val_loss
            best_weights = (W1.copy(), b1.copy(), W2.copy(), b2.copy())
            patience_counter = 0
        else:
            patience_counter += 1

        if (epoch + 1) % 10 == 0:
            print(f"  Epoch {epoch + 1}/{epochs}: "
                  f"train_loss={epoch_loss / n_batches:.4f}, "
                  f"val_loss={val_loss:.4f}, val_acc={val_acc:.3f}, "
                  f"val_FPR={val_fpr:.4f}")

        if patience_counter >= early_stopping_patience:
            print(f"\n  Early stopping at epoch {epoch + 1} "
                  f"(no improvement for {early_stopping_patience} epochs)")
            break

    W1, b1, W2, b2 = best_weights
    val_probs = forward(X_val)
    final_fpr = compute_fpr(val_probs, y_val)
    final_acc = np.mean((val_probs > 0.5) == y_val)
    print(f"\n  Best model — val_acc={final_acc:.3f}, val_FPR={final_fpr:.4f}")

    return best_weights


def export_onnx(weights: tuple, output_path: str) -> None:
    """Export trained classifier as ONNX model with [1, 16, 96] input.

    openwakeword expects models with input shape [1, N_frames, 96] where
    N_frames is the number of consecutive embedding frames. The model
    flattens the input internally before the MLP layers.
    """
    try:
        import onnx
        from onnx import TensorProto, helper, numpy_helper
    except ImportError:
        print("ERROR: onnx package required for export. Install with: pip install onnx")
        sys.exit(1)

    W1, b1, W2, b2 = weights
    hidden_size = W1.shape[1]
    flat_dim = N_WINDOW_FRAMES * 96  # 1536

    # Input: [1, 16, 96] — openwakeword's standard format
    X = helper.make_tensor_value_info("input", TensorProto.FLOAT, [1, N_WINDOW_FRAMES, 96])
    Y = helper.make_tensor_value_info("output", TensorProto.FLOAT, [1, 1])

    # Reshape target: [1, 1536]
    reshape_shape = helper.make_tensor("reshape_shape", TensorProto.INT64, [2], [1, flat_dim])

    w1_init = helper.make_tensor("W1", TensorProto.FLOAT, W1.shape, W1.flatten().tolist())
    b1_init = helper.make_tensor("b1", TensorProto.FLOAT, [1, hidden_size], b1.tolist())
    w2_init = helper.make_tensor("W2", TensorProto.FLOAT, W2.shape, W2.flatten().tolist())
    b2_init = helper.make_tensor("b2", TensorProto.FLOAT, [1, 1], b2.tolist())

    nodes = [
        # Flatten [1, 16, 96] → [1, 1536]
        helper.make_node("Reshape", ["input", "reshape_shape"], ["flat"]),
        helper.make_node("MatMul", ["flat", "W1"], ["mm1"]),
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
        initializer=[reshape_shape, w1_init, b1_init, w2_init, b2_init],
    )

    model = helper.make_model(graph, opset_imports=[helper.make_opsetid("", 13)])
    model.ir_version = 7

    onnx.save(model, output_path)
    print(f"\nModel exported to: {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Train custom wake word model")
    parser.add_argument("--positive-dir", nargs="+", required=True, help="Directories with positive samples")
    parser.add_argument("--negative-dir", nargs="*", help="Directories with real negative samples")
    parser.add_argument("--output", default="models/hey_vox.onnx", help="Output ONNX model path")
    parser.add_argument("--epochs", type=int, default=200, help="Training epochs (default: 200)")
    parser.add_argument("--negative-ratio", type=float, default=10.0,
                        help="Ratio of negative to positive samples (default: 10.0)")
    parser.add_argument("--false-weight", type=float, default=3.0,
                        help="Extra weight on false positive loss (default: 3.0)")
    parser.add_argument("--patience", type=int, default=20,
                        help="Early stopping patience in epochs (default: 20)")
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

    # Extract embedding windows (shape: N, 16, 96)
    print("\nExtracting embedding windows (this may take a while)...")
    pos_windows = extract_embeddings(pos_clips)
    neg_windows = extract_embeddings(neg_clips)
    print(f"  Window shape: {pos_windows.shape[1:]} (frames x embedding_dim)")

    # Train classifier
    weights = train_classifier(
        pos_windows, neg_windows,
        epochs=args.epochs,
        false_weight=args.false_weight,
        early_stopping_patience=args.patience,
    )

    # Export ONNX
    export_onnx(weights, str(output_path))

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
