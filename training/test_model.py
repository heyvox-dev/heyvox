"""
Test a trained custom wake word model interactively.

Usage:
    python training/test_model.py --model models/hey_vox.onnx --threshold 0.5

Listens on the microphone and prints detection scores in real-time.
"""

import argparse
import sys
import time

import numpy as np


SAMPLE_RATE = 16000
CHUNK_SIZE = 1280  # 80ms frames


def main():
    parser = argparse.ArgumentParser(description="Test wake word model")
    parser.add_argument("--model", required=True, help="Path to .onnx model")
    parser.add_argument("--threshold", type=float, default=0.5, help="Detection threshold")
    parser.add_argument("--duration", type=int, default=30, help="Test duration in seconds")
    args = parser.parse_args()

    try:
        from openwakeword.model import Model
    except ImportError:
        print("ERROR: openwakeword required. Install with: pip install openwakeword")
        sys.exit(1)

    try:
        import pyaudio
    except ImportError:
        print("ERROR: pyaudio required. Install with: pip install pyaudio")
        sys.exit(1)

    print(f"Loading model: {args.model}")
    model = Model(wakeword_models=[args.model])

    pa = pyaudio.PyAudio()
    stream = pa.open(
        format=pyaudio.paInt16,
        channels=1,
        rate=SAMPLE_RATE,
        input=True,
        frames_per_buffer=CHUNK_SIZE,
    )

    print(f"\nListening for {args.duration}s (threshold={args.threshold})...")
    print("Say \"Hey Vox\" to test detection.\n")

    start = time.time()
    detections = 0

    try:
        while time.time() - start < args.duration:
            data = stream.read(CHUNK_SIZE, exception_on_overflow=False)
            audio = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32767.0

            # Feed to model
            model.predict(audio)

            # Check scores
            for name, score in model.prediction_buffer.items():
                if len(score) > 0:
                    current_score = score[-1]
                    bar = "█" * int(current_score * 40)
                    status = " DETECTED!" if current_score >= args.threshold else ""
                    print(f"\r  {name}: {current_score:.3f} |{bar:<40}|{status}", end="", flush=True)

                    if current_score >= args.threshold:
                        detections += 1
                        model.reset()
                        print()  # New line after detection
                        time.sleep(0.5)  # Cooldown

    except KeyboardInterrupt:
        pass
    finally:
        stream.stop_stream()
        stream.close()
        pa.terminate()

    elapsed = time.time() - start
    print(f"\n\nResults ({elapsed:.0f}s):")
    print(f"  Detections: {detections}")
    print(f"  Threshold: {args.threshold}")

    if detections > 0:
        print("\nModel is working! Adjust threshold if needed.")
    else:
        print("\nNo detections. Try lowering the threshold or retraining with more data.")


if __name__ == "__main__":
    main()
