#!/usr/bin/env python3
"""
HeyVox Wake Word — LOCAL SMOKE TEST
=====================================
Runs the v7 retrain pipeline end-to-end on the local Mac.  Uses the
already-downloaded feature files.  Produces hey_vox_smoke_complete.onnx
under /tmp so the real model in ~/.config/heyvox/models/ is untouched.

Feature sources (local, not Drive):
  ~/Downloads/hey_vox/positive_features_train.npy  (50K synthetic + later merged)
  ~/Downloads/hey_vox/negative_features_train.npy
  ~/Downloads/hey_vox/positive_features_test.npy
  ~/Downloads/hey_vox/negative_features_test.npy
  training/data/features/openwakeword_features_ACAV100M_2000_hrs_16bit.npy (17GB)
  training/data/features/validation_set_features.npy (185MB)

Usage:
  python3 tools/retrain_heyvox_local_smoke.py

If it produces /tmp/hey_vox_smoke_complete.onnx, the Colab full run is
very likely to succeed too.
"""

from __future__ import annotations

import os
import sys
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

import numpy as np
import yaml

# ---------------------------------------------------------------------------
# Paths (all local)
# ---------------------------------------------------------------------------
REPO = Path(__file__).resolve().parent.parent
OUTPUT_DIR = Path("/tmp/heyvox_local_smoke")
MODEL_SUBDIR = OUTPUT_DIR / "hey_vox_smoke"
DOWNLOADED_FEATURES = Path("/Users/work/Downloads/hey_vox")
ACAV_FEATURES = REPO / "training/data/features/openwakeword_features_ACAV100M_2000_hrs_16bit.npy"
VAL_FEATURES = REPO / "training/data/features/validation_set_features.npy"
PERSONAL_FEATURES_TAR = Path("/tmp/personal_features.tar.gz")
PERSONAL_POSITIVE_OVERSAMPLE = 10

FINAL_OUTPUT = Path("/tmp/hey_vox_smoke_complete.onnx")


def step(n: str, msg: str):
    print(f"\n[{n}] {msg}")


def main() -> int:
    step("0/7", "Preflight — checking inputs")
    for p in [DOWNLOADED_FEATURES, ACAV_FEATURES, VAL_FEATURES, PERSONAL_FEATURES_TAR]:
        if not p.exists():
            print(f"  MISSING: {p}")
            return 2
        print(f"  OK: {p}")

    # Fresh run — wipe prior smoke artifacts so we don't get confused results.
    if OUTPUT_DIR.exists():
        print(f"  Removing prior run: {OUTPUT_DIR}")
        shutil.rmtree(OUTPUT_DIR)
    MODEL_SUBDIR.mkdir(parents=True)

    step("1/7", "Stage feature files from Downloads")
    for f in [
        "positive_features_train.npy",
        "negative_features_train.npy",
        "positive_features_test.npy",
        "negative_features_test.npy",
    ]:
        src = DOWNLOADED_FEATURES / f
        dst = MODEL_SUBDIR / f
        # Use symlink to avoid 600MB copy; mutation is only for positive_train
        # which we'll replace below with the merged version.
        if f == "positive_features_train.npy":
            shutil.copy2(src, dst)  # real copy — we'll mutate this one
            print(f"  copied {f} ({dst.stat().st_size // (1024*1024)} MB)")
        else:
            os.symlink(src, dst)
            print(f"  linked {f}")

    step("2/7", "Merge personal features (step 3.5 logic)")
    with tempfile.TemporaryDirectory() as td:
        with tarfile.open(PERSONAL_FEATURES_TAR, "r:gz") as tf:
            tf.extractall(td)
        pp = np.load(f"{td}/personal_positive.npy")
        pn = np.load(f"{td}/personal_hard_negative.npy")
        print(f"  personal_positive:      {pp.shape} {pp.dtype}")
        print(f"  personal_hard_negative: {pn.shape} {pn.dtype}")

        orig_pos_path = MODEL_SUBDIR / "positive_features_train.npy"
        orig = np.load(orig_pos_path)
        print(f"  original synthetic positives: {orig.shape}")

        tiled = np.tile(pp, (PERSONAL_POSITIVE_OVERSAMPLE, 1, 1)).astype(orig.dtype)
        assert tiled.shape[1:] == orig.shape[1:], (tiled.shape, orig.shape)
        merged = np.concatenate([orig, tiled], axis=0)
        # numpy appends .npy automatically; name the tmp so the final path
        # already ends in .npy and os.replace works.
        tmp = orig_pos_path.parent / "positive_features_train.tmp.npy"
        np.save(tmp, merged)
        os.replace(tmp, orig_pos_path)
        print(f"  merged positives: {merged.shape}")

        pn_out = MODEL_SUBDIR / "personal_hard_negative_features.npy"
        np.save(pn_out, pn.astype(np.float32))
        print(f"  wrote {pn_out.name}: {pn.shape}")

    step("3/7", "Ensure RIR + background dirs exist")
    rir_dir = OUTPUT_DIR / "mit_rirs"
    bg_dir = OUTPUT_DIR / "background_clips"
    rir_dir.mkdir(exist_ok=True)
    bg_dir.mkdir(exist_ok=True)
    print(f"  {rir_dir} (empty — fine for smoke)")
    print(f"  {bg_dir} (empty — fine for smoke)")

    # openwakeword/train.py L741-745 unconditionally reads 50 random WAVs from
    # positive_test/ to auto-tune total_length.  Even with --train_model only
    # (no generate_clips, no augment_clips), that probe runs.  Seed 50 silent
    # 32000-sample 16kHz WAVs so the probe passes — total_length is already
    # pinned to 32000 in the config, so the computed median won't change it.
    step("3.5/7", "Seed fake WAVs for train.py duration probe")
    from scipy.io import wavfile
    pos_test_dir = OUTPUT_DIR / "hey_vox_smoke" / "positive_test"
    pos_test_dir.mkdir(parents=True, exist_ok=True)
    silence = np.zeros(32000, dtype=np.int16)
    for i in range(50):
        wavfile.write(pos_test_dir / f"dummy_{i:03d}.wav", 16000, silence)
    print(f"  wrote 50 x 32000-sample silence WAVs to {pos_test_dir}")

    step("4/7", "Write training config YAML")
    config = {
        "model_name": "hey_vox_smoke",
        "target_phrase": ["hey vox"],
        "custom_negative_phrases": [
            "hey box", "hey fox", "hey socks", "hey rocks", "hey docs",
            "next box", "gray fox", "hey boss", "hey bro", "hey bob",
            "hey there", "hey man", "hey you", "hey what", "okay",
            "hey siri", "alexa", "okay google", "hey google",
            "hey cortana", "hey jarvis", "computer",
        ],
        "n_samples": 50000,
        "n_samples_val": 5000,
        "tts_batch_size": 64,
        "augmentation_batch_size": 16,
        "augmentation_rounds": 1,
        "piper_sample_generator_path": "/tmp/piper-not-used",
        "output_dir": str(OUTPUT_DIR),
        "total_length": 32000,
        "rir_paths": [str(rir_dir)],
        "background_paths": [str(bg_dir)],
        "background_paths_duplication_rate": [1],
        "feature_data_files": {
            "ACAV100M_sample": str(ACAV_FEATURES),
            "personal_hard_negative": str(MODEL_SUBDIR / "personal_hard_negative_features.npy"),
        },
        "batch_n_per_class": {
            "ACAV100M_sample": 1024,
            "adversarial_negative": 128,
            "positive": 128,
            "personal_hard_negative": 128,
        },
        "false_positive_validation_data_path": str(VAL_FEATURES),
        "model_type": "dnn",
        "layer_size": 64,
        "steps": 500,
        "max_negative_weight": 2000,
        "target_false_positives_per_hour": 0.1,
    }

    config_path = OUTPUT_DIR / "retrain_config.yaml"
    with open(config_path, "w") as f:
        yaml.dump(config, f, default_flow_style=False)
    print(f"  config at {config_path}")

    step("5/7", "Run openwakeword.train (--train_model)")
    # openwakeword/train.py unconditionally imports `generate_samples` at
    # module load, so piper-sample-generator must be importable even though
    # we only use --train_model (no generation).  Cloned to /tmp on first run.
    piper_dir = "/tmp/piper-sample-generator"
    if not Path(piper_dir, "generate_samples.py").exists():
        print(f"  ERROR: {piper_dir}/generate_samples.py missing. "
              "Clone https://github.com/dscripka/piper-sample-generator there.")
        return 2
    env = os.environ.copy()
    env["PYTHONPATH"] = piper_dir + ":" + env.get("PYTHONPATH", "")
    # train.py builds a DataLoader with num_workers=os.cpu_count()//2 and
    # pickles its closure — which contains a lambda.  On macOS the default
    # start method is 'spawn' (pickle required); switching to 'fork' lets
    # the workers inherit the lambda by reference without pickling it.
    # The runpy bits below are how `python -m openwakeword.train` works —
    # we just wedge set_start_method('fork') in before the module loads.
    bootstrap = (
        "import multiprocessing as mp, runpy, sys; "
        "mp.set_start_method('fork', force=True); "
        "sys.argv[0] = 'openwakeword.train'; "
        "runpy.run_module('openwakeword.train', run_name='__main__', alter_sys=True)"
    )
    cmd = [sys.executable, "-c", bootstrap,
           "--training_config", str(config_path),
           "--train_model"]
    print("  cmd:", " ".join(cmd))
    print("  PYTHONPATH:", env["PYTHONPATH"])
    print("  " + "=" * 58)
    # stream output directly so we can watch progress
    result = subprocess.run(cmd, env=env)
    print("  " + "=" * 58)
    if result.returncode != 0:
        print(f"  NOTE: exit code {result.returncode} "
              "(expected if TFLite conversion crashed — ONNX is saved before that)")

    step("6/7", "Locate trained ONNX model")
    onnx_candidates: list[Path] = []
    for p in OUTPUT_DIR.rglob("*.onnx"):
        s = str(p)
        if p.name.endswith(".onnx.data"):
            continue
        if "resources/models" in s or ".cache" in s:
            continue
        onnx_candidates.append(p)
    if not onnx_candidates:
        print("  ERROR: no .onnx produced")
        return 1
    # Prefer hey_vox_smoke.onnx
    model_path = next((p for p in onnx_candidates if "hey_vox_smoke" in p.name), None)
    if model_path is None:
        model_path = next((p for p in onnx_candidates if "hey_vox" in p.name), onnx_candidates[0])
    print(f"  found: {model_path} ({model_path.stat().st_size} bytes)")

    step("7/7", f"Save final ONNX -> {FINAL_OUTPUT}")
    data_file = Path(str(model_path) + ".data")
    if data_file.exists() or model_path.stat().st_size < 100_000:
        import onnx
        model = onnx.load(str(model_path))
        onnx.save_model(model, str(FINAL_OUTPUT), save_as_external_data=False)
    else:
        shutil.copy2(model_path, FINAL_OUTPUT)
    print(f"  {FINAL_OUTPUT} ({FINAL_OUTPUT.stat().st_size} bytes)")
    print("\nSMOKE PIPELINE OK. The merge + config + train + export all worked locally.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
