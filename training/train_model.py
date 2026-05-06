"""
Train a custom openwakeword model for "Hey Vox".

Usage:
    python training/train_model.py \
        --positive-dir training/recordings/ training/synthetic/ \
        --negative-dir training/negatives/ training/negatives_real/ \
        --output models/hey_vox.onnx \
        --epochs 200 --false-weight 5.0 --lr 0.001

This script:
1. Loads positive samples (wake word clips) from one or more directories
2. Loads negative samples from real speech/noise directories (or generates synthetic ones)
3. Trains an openwakeword-compatible ONNX keyword spotter model using 3-sequence training
4. Averages the top-5 checkpoints by validation FPR for robustness
5. Exports the model ready for use with heyvox

The trained model is a small neural network that classifies 1280-sample
(80ms @ 16kHz) audio frames as containing the wake word or not, operating
on top of Google's speech embedding model (used by openwakeword).
"""

import argparse
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
    """Generate negative samples (silence, noise, random speech-like audio).

    Only used when no --negative-dir is provided. Real speech samples are
    always preferred over these synthetic ones.
    """
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
    false_weight: float = 5.0,
    early_stopping_patience: int = 20,
) -> tuple:
    """Train a 3-layer MLP classifier on embedding windows using 3-sequence training.

    Input windows have shape (N, 16, 96) — matching openwakeword's inference
    format. The model flattens each window to (16*96=1536) before the MLP.

    Training proceeds in 3 sequences (like openwakeword's training approach):
      - Sequence 1 (80% of epochs): false_weight ramps from 1.0 to configured value
      - Sequence 2 (15% of epochs): LR/10, doubles false_weight if val FPR > 0.01
      - Sequence 3 (5% of epochs): LR/10 again, doubles again if still high FPR

    The top-5 checkpoints by val FPR (among those with recall > 0.9) are
    averaged at the end, matching openwakeword's checkpoint averaging strategy.

    Args:
        pos_windows: Positive sample embedding windows, shape (N, 16, 96).
        neg_windows: Negative sample embedding windows, shape (M, 16, 96).
        epochs: Maximum training epochs across all sequences.
        lr: Initial learning rate (decayed in sequences 2 and 3).
        false_weight: Final weight on false positive loss (sequence 1 ramps to this).
        early_stopping_patience: Stop if val_loss doesn't improve.

    Returns:
        Tuple of (W1, b1, W2, b2) weight arrays for the 2-layer MLP, averaged
        across the top-5 checkpoints.
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

    idx = np.random.permutation(len(X))
    X, y = X[idx], y[idx]

    split = int(0.9 * len(X))
    X_train, X_val = X[:split], X[split:]
    y_train, y_val = y[:split], y[split:]

    hidden_size = 128
    W1 = np.random.randn(flat_dim, hidden_size).astype(np.float32) * np.sqrt(2.0 / flat_dim)
    b1 = np.zeros(hidden_size, dtype=np.float32)
    W2 = np.random.randn(hidden_size, 1).astype(np.float32) * np.sqrt(2.0 / hidden_size)
    b2 = np.zeros(1, dtype=np.float32)

    n_val_pos = int(y_val.sum())
    n_val_neg = int(len(y_val) - n_val_pos)

    print(f"\nTraining classifier: flatten({N_WINDOW_FRAMES}x96={flat_dim}) -> {hidden_size} -> 1")
    print(f"  Positive windows: {len(pos_flat)}, Negative windows: {len(neg_flat)}")
    print(f"  Train: {len(X_train)}, Val: {len(X_val)} ({n_val_pos} pos, {n_val_neg} neg)")
    print(f"  False-positive loss weight (final): {false_weight}x")
    print(f"  Early stopping patience: {early_stopping_patience} epochs")

    def forward(X_batch: np.ndarray) -> np.ndarray:
        h = np.maximum(0, X_batch @ W1 + b1)
        logits = h @ W2 + b2
        return 1.0 / (1.0 + np.exp(-np.clip(logits.flatten(), -500, 500)))

    def compute_metrics(probs: np.ndarray, labels: np.ndarray) -> dict:
        eps = 1e-7
        probs_c = np.clip(probs, eps, 1 - eps)
        loss = -np.mean(labels * np.log(probs_c) + (1 - labels) * np.log(1 - probs_c))
        acc = float(np.mean((probs > 0.5) == labels))

        neg_mask = labels == 0
        fpr = 0.0
        if neg_mask.sum() > 0:
            fpr = float(np.mean(probs[neg_mask] > 0.5))

        pos_mask = labels == 1
        recall = 0.0
        if pos_mask.sum() > 0:
            recall = float(np.mean(probs[pos_mask] > 0.5))

        return {"loss": float(loss), "acc": acc, "fpr": fpr, "recall": recall}

    # Checkpoint store: list of (fpr, recall, (W1, b1, W2, b2))
    checkpoints: list[tuple[float, float, tuple]] = []
    TOP_K = 5
    MIN_RECALL_FOR_CHECKPOINT = 0.9

    def maybe_store_checkpoint(metrics: dict) -> None:
        if metrics["recall"] < MIN_RECALL_FOR_CHECKPOINT:
            return
        snap = (W1.copy(), b1.copy(), W2.copy(), b2.copy())
        checkpoints.append((metrics["fpr"], metrics["recall"], snap))
        # Keep only top-5 by lowest FPR
        checkpoints.sort(key=lambda x: x[0])
        if len(checkpoints) > TOP_K:
            checkpoints.pop()

    def run_sequence(
        seq_num: int,
        n_epochs: int,
        seq_lr: float,
        start_fw: float,
        end_fw: float,
        label: str,
    ) -> dict:
        """Run one training sequence, returning final val metrics."""
        nonlocal W1, b1, W2, b2

        print(f"\n--- Sequence {seq_num}: {label} ({n_epochs} epochs, lr={seq_lr:.5f}) ---")
        print(f"  false_weight: {start_fw:.2f} → {end_fw:.2f}")

        best_val_loss = float("inf")
        best_snap = (W1.copy(), b1.copy(), W2.copy(), b2.copy())
        patience_counter = 0
        batch_size = 64

        for epoch in range(n_epochs):
            # Linearly ramp false_weight over the sequence
            progress = epoch / max(n_epochs - 1, 1)
            fw = start_fw + (end_fw - start_fw) * progress

            perm = np.random.permutation(len(X_train))
            epoch_loss = 0.0
            n_batches = 0

            for start in range(0, len(X_train), batch_size):
                end = min(start + batch_size, len(X_train))
                batch_idx = perm[start:end]
                X_b = X_train[batch_idx]
                y_b = y_train[batch_idx]

                # Build per-sample weights: negatives get fw, positives get 1.0
                sample_weights = np.ones(len(y_b), dtype=np.float32)
                sample_weights[y_b == 0] = fw

                h = np.maximum(0, X_b @ W1 + b1)
                logits = h @ W2 + b2
                eps = 1e-7
                probs = 1.0 / (1.0 + np.exp(-np.clip(logits.flatten(), -500, 500)))
                probs_c = np.clip(probs, eps, 1 - eps)

                # Weighted BCE loss
                weighted_loss = -np.mean(
                    sample_weights * (
                        y_b * np.log(probs_c) + (1 - y_b) * np.log(1 - probs_c)
                    )
                )

                # Gradient with sample weights propagated
                dlogits = (sample_weights * (probs - y_b)).reshape(-1, 1) / len(y_b)
                dW2 = h.T @ dlogits
                db2 = dlogits.sum(axis=0)
                dh = dlogits @ W2.T
                dh[h <= 0] = 0
                dW1 = X_b.T @ dh
                db1 = dh.sum(axis=0)

                W1 -= seq_lr * dW1
                b1 -= seq_lr * db1
                W2 -= seq_lr * dW2
                b2 -= seq_lr * db2

                epoch_loss += float(weighted_loss)
                n_batches += 1

            val_probs = forward(X_val)
            val_metrics = compute_metrics(val_probs, y_val)
            maybe_store_checkpoint(val_metrics)

            if val_metrics["loss"] < best_val_loss:
                best_val_loss = val_metrics["loss"]
                best_snap = (W1.copy(), b1.copy(), W2.copy(), b2.copy())
                patience_counter = 0
            else:
                patience_counter += 1

            if (epoch + 1) % 10 == 0:
                print(
                    f"  Epoch {epoch + 1}/{n_epochs}: "
                    f"train_loss={epoch_loss / n_batches:.4f}, "
                    f"val_loss={val_metrics['loss']:.4f}, "
                    f"val_acc={val_metrics['acc']:.3f}, "
                    f"val_FPR={val_metrics['fpr']:.4f}, "
                    f"val_recall={val_metrics['recall']:.4f}  "
                    f"[fw={fw:.2f}]"
                )

            if patience_counter >= early_stopping_patience:
                print(
                    f"\n  Early stopping at epoch {epoch + 1} "
                    f"(no improvement for {early_stopping_patience} epochs)"
                )
                break

        # Restore best snap from this sequence
        W1[:], b1[:], W2[:], b2[:] = best_snap

        val_probs = forward(X_val)
        final = compute_metrics(val_probs, y_val)
        print(
            f"\n  Sequence {seq_num} summary — "
            f"val_acc={final['acc']:.3f}, "
            f"val_loss={final['loss']:.4f}, "
            f"val_FPR={final['fpr']:.4f}, "
            f"val_recall={final['recall']:.4f}"
        )
        return final

    # --- Sequence 1: 80% of epochs, false_weight 1.0 → configured value ---
    seq1_epochs = max(1, int(epochs * 0.80))
    seq1_metrics = run_sequence(
        seq_num=1,
        n_epochs=seq1_epochs,
        seq_lr=lr,
        start_fw=1.0,
        end_fw=false_weight,
        label="main training, FP weight ramp",
    )

    # --- Sequence 2: 15% of epochs, LR/10, double FW if FPR still high ---
    seq2_fw = false_weight * 2.0 if seq1_metrics["fpr"] > 0.01 else false_weight
    if seq1_metrics["fpr"] > 0.01:
        print(f"\n  val FPR={seq1_metrics['fpr']:.4f} > 0.01 — doubling false_weight to {seq2_fw:.2f} for sequence 2")
    seq2_epochs = max(1, int(epochs * 0.15))
    seq2_metrics = run_sequence(
        seq_num=2,
        n_epochs=seq2_epochs,
        seq_lr=lr / 10.0,
        start_fw=seq2_fw,
        end_fw=seq2_fw,
        label="fine-tune, LR/10",
    )

    # --- Sequence 3: 5% of epochs, LR/100, double again if still high FPR ---
    seq3_fw = seq2_fw * 2.0 if seq2_metrics["fpr"] > 0.01 else seq2_fw
    if seq2_metrics["fpr"] > 0.01:
        print(f"\n  val FPR={seq2_metrics['fpr']:.4f} > 0.01 — doubling false_weight to {seq3_fw:.2f} for sequence 3")
    seq3_epochs = max(1, int(epochs * 0.05))
    run_sequence(
        seq_num=3,
        n_epochs=seq3_epochs,
        seq_lr=lr / 100.0,
        start_fw=seq3_fw,
        end_fw=seq3_fw,
        label="final refinement, LR/100",
    )

    # --- Checkpoint averaging ---
    if checkpoints:
        print(f"\nAveraging top-{len(checkpoints)} checkpoints by val FPR (recall >= {MIN_RECALL_FOR_CHECKPOINT}):")
        for rank, (fpr, recall, _) in enumerate(checkpoints):
            print(f"  #{rank + 1}: FPR={fpr:.4f}, recall={recall:.4f}")

        avg_W1 = np.mean([c[2][0] for c in checkpoints], axis=0)
        avg_b1 = np.mean([c[2][1] for c in checkpoints], axis=0)
        avg_W2 = np.mean([c[2][2] for c in checkpoints], axis=0)
        avg_b2 = np.mean([c[2][3] for c in checkpoints], axis=0)

        averaged_weights = (avg_W1, avg_b1, avg_W2, avg_b2)

        # Report averaged model metrics
        W1[:], b1[:], W2[:], b2[:] = averaged_weights
        val_probs = forward(X_val)
        avg_metrics = compute_metrics(val_probs, y_val)
        print(
            f"\n  Averaged model — "
            f"val_acc={avg_metrics['acc']:.3f}, "
            f"val_loss={avg_metrics['loss']:.4f}, "
            f"val_FPR={avg_metrics['fpr']:.4f}, "
            f"val_recall={avg_metrics['recall']:.4f}"
        )
        return averaged_weights
    else:
        print(
            "\nWARNING: No checkpoint had recall >= 0.9. "
            "Returning final model weights. Consider more positive samples or fewer epochs."
        )
        return (W1.copy(), b1.copy(), W2.copy(), b2.copy())


def export_onnx(weights: tuple, output_path: str) -> None:
    """Export trained classifier as ONNX model with [1, 16, 96] input.

    openwakeword expects models with input shape [1, N_frames, 96] where
    N_frames is the number of consecutive embedding frames. The model
    flattens the input internally before the MLP layers.

    The hidden_size is inferred from W1.shape[1], so this works for any
    hidden layer size (64, 128, etc.).
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

    print(f"\nExporting ONNX model: input=[1, {N_WINDOW_FRAMES}, 96] → flatten → {flat_dim} → {hidden_size} → 1")

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
    print(f"Model exported to: {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Train custom wake word model")
    parser.add_argument("--positive-dir", nargs="+", required=True,
                        help="Directories with positive samples (WAV files)")
    parser.add_argument("--negative-dir", nargs="*",
                        help="Directories with real negative samples (WAV files). "
                             "When provided, random noise generation is skipped entirely.")
    parser.add_argument("--output", default="models/hey_vox.onnx",
                        help="Output ONNX model path (default: models/hey_vox.onnx)")
    parser.add_argument("--epochs", type=int, default=200,
                        help="Total training epochs across all 3 sequences (default: 200)")
    parser.add_argument("--lr", type=float, default=0.001,
                        help="Initial learning rate (default: 0.001)")
    parser.add_argument("--negative-ratio", type=float, default=10.0,
                        help="Max ratio of negative to positive windows — "
                             "negatives are subsampled if they exceed this (default: 10.0)")
    parser.add_argument("--false-weight", type=float, default=5.0,
                        help="Final weight on false positive loss in sequence 1+ (default: 5.0)")
    parser.add_argument("--patience", type=int, default=20,
                        help="Early stopping patience per sequence in epochs (default: 20)")
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
    if args.negative_dir:
        # Real speech negatives — skip synthetic generation entirely
        print("Loading real negative samples (skipping random noise generation)...")
        neg_clips = collect_clips(args.negative_dir)
        if len(neg_clips) == 0:
            print("ERROR: --negative-dir provided but no WAV files found. "
                  "Check directory paths.")
            sys.exit(1)
        print(f"  Loaded {len(neg_clips)} real negative clips")
    else:
        neg_count = int(len(pos_clips) * args.negative_ratio)
        print(f"No --negative-dir provided. Generating {neg_count} synthetic negative samples...")
        print("WARNING: Synthetic negatives are much weaker than real speech. "
              "Provide --negative-dir for better FPR.")
        neg_clips = generate_negative_samples(neg_count)
        print(f"  Generated {len(neg_clips)} synthetic negative clips")

    # Extract embedding windows (shape: N, 16, 96)
    print("\nExtracting embedding windows (this may take a while)...")
    pos_windows = extract_embeddings(pos_clips)
    neg_windows = extract_embeddings(neg_clips)
    print(f"  Positive windows: {pos_windows.shape}, Negative windows: {neg_windows.shape}")
    print(f"  Window shape: {pos_windows.shape[1:]} (frames x embedding_dim)")

    # Apply negative-ratio subsampling on windows (not clips)
    max_neg_windows = int(len(pos_windows) * args.negative_ratio)
    if len(neg_windows) > max_neg_windows:
        print(
            f"\nSubsampling negatives: {len(neg_windows)} → {max_neg_windows} windows "
            f"(--negative-ratio={args.negative_ratio})"
        )
        idx = np.random.choice(len(neg_windows), max_neg_windows, replace=False)
        neg_windows = neg_windows[idx]

    # Train classifier
    weights = train_classifier(
        pos_windows, neg_windows,
        epochs=args.epochs,
        lr=args.lr,
        false_weight=args.false_weight,
        early_stopping_patience=args.patience,
    )

    # Export ONNX
    export_onnx(weights, str(output_path))

    # Summary
    print("\nTraining complete!")
    print(f"  Model: {output_path}")
    print(f"  Positive samples: {len(pos_clips)}")
    print(f"  Negative samples: {len(neg_clips)}")
    print("\nTo use with HeyVox, update ~/.config/heyvox/config.yaml:")
    print("  wake_word:")
    print("    start: hey_vox")
    print("    stop: hey_vox")


if __name__ == "__main__":
    main()
