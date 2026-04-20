#!/usr/bin/env python3
"""
HeyVox Wake Word Retrain Script v7
====================================
    # Cell 1:
    from google.colab import drive
    drive.mount('/content/drive')
    !pip install -q git+https://github.com/dscripka/openWakeWord.git
    !pip install -q onnx onnxruntime huggingface_hub pyyaml torch torchaudio
    !pip install -q torchinfo torchmetrics speechbrain audiomentations torch-audiomentations
    !pip install -q onnxscript mutagen acoustics pronouncing deep-phonemizer
    !pip install -q webrtcvad espeak-phonemizer soundfile scipy datasets librosa
    !python /content/drive/MyDrive/heyvox_training_checkpoints/retrain_heyvox.py

    # Cell 2:
    from google.colab import files
    files.download('/content/drive/MyDrive/heyvox_training_checkpoints/hey_vox_complete.onnx')

Issues handled:
- v1: openwakeword.train needs GitHub install (not PyPI)
- v2: generate_samples needs piper-sample-generator cloned
- v3: total_length must be in config (normally set by augment_clips)
- v4: importlib.reload doesn't execute __main__ block - use subprocess
- v5: melspectrogram.onnx + embedding_model.onnx not bundled in git install
- v5: convert_onnx_to_tflite crashes without tensorflow - ONNX saved before crash
- v7: merge personal features (1,589 user positives + 757 hard-negatives).
      Personal positives oversampled 10x so real-voice signal survives inside
      the 50K synthetic positive pool. Hard-negatives added as their own
      class (personal_hard_negative) to escape ACAV100M feature averaging.
      Produce personal_features.tar.gz with `tools/collect_personal_features.py`.
"""

import os
import sys
import tarfile
import shutil
import subprocess
import glob
import urllib.request

# ============================================================
# CONFIG
# ============================================================
OUTPUT_DIR = '/content/hey_vox_training'
CHECKPOINT_DIR = '/content/drive/MyDrive/heyvox_training_checkpoints'
PIPER_DIR = '/content/piper-sample-generator'
# Single source of truth — openwakeword.train derives its working paths
# (positive_test_output_dir, feature_save_dir at train.py:656,659) from
# config['model_name'], so MODEL_SUBDIR must match or training sees zero
# features and zero positive_test WAVs. See DEF-056.
MODEL_NAME = 'hey_vox'
MODEL_SUBDIR = os.path.join(OUTPUT_DIR, MODEL_NAME)

# v7: personal features tarball produced locally and uploaded to Drive.
PERSONAL_FEATURES_TAR = os.path.join(CHECKPOINT_DIR, 'personal_features.tar.gz')
# Oversample factor for personal positives — copies the 1,589 user-voice
# embeddings 10x so they make up ~24% of each 128-sample positive batch
# instead of 3%. Real-voice signal otherwise drowns in the 50K synthetic pool.
PERSONAL_POSITIVE_OVERSAMPLE = 10

# ============================================================
# STEP 0: Create directories
# ============================================================
print("[0/9] Creating directories...")
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(MODEL_SUBDIR, exist_ok=True)

# ============================================================
# STEP 1: Clone piper-sample-generator
# ============================================================
print("[1/9] Setting up piper-sample-generator...")
if not os.path.exists(os.path.join(PIPER_DIR, 'generate_samples.py')):
    if os.path.exists(PIPER_DIR):
        shutil.rmtree(PIPER_DIR)
    subprocess.run(['git', 'clone', 'https://github.com/dscripka/piper-sample-generator.git', PIPER_DIR], check=True)
    subprocess.run([sys.executable, '-m', 'pip', 'install', '-q', '-r', os.path.join(PIPER_DIR, 'requirements.txt')], check=True)
    print("  piper-sample-generator cloned and deps installed.")
else:
    print("  piper-sample-generator already present.")

# ============================================================
# STEP 2: Download openwakeword resource models
# (Not bundled in GitHub source install, only in PyPI wheel)
# ============================================================
print("[2/9] Downloading openwakeword resource models...")
import openwakeword
RES_DIR = os.path.dirname(openwakeword.__file__) + '/resources/models'
os.makedirs(RES_DIR, exist_ok=True)

BASE_URL = 'https://github.com/dscripka/openWakeWord/releases/download/v0.5.1'
for model_name in ['melspectrogram.onnx', 'embedding_model.onnx']:
    dest = os.path.join(RES_DIR, model_name)
    if not os.path.exists(dest):
        print(f'  Downloading {model_name}...')
        urllib.request.urlretrieve(f'{BASE_URL}/{model_name}', dest)
        size_kb = os.path.getsize(dest) // 1024
        print(f'  Saved to {dest} ({size_kb} KB)')
    else:
        print(f'  {model_name} already present.')

# ============================================================
# STEP 2.5: Verify openwakeword.train is importable
# ============================================================
print("[2.5/9] Verifying openwakeword.train module...")
try:
    if PIPER_DIR not in sys.path:
        sys.path.insert(0, PIPER_DIR)
    import openwakeword.train
    print("  openwakeword.train module OK")
except ImportError as e:
    print(f"FATAL: {e}")
    sys.exit(1)

# ============================================================
# STEP 3: Restore features from Drive checkpoint
# ============================================================
print("[3/9] Restoring features from Drive checkpoint...")
tar_path = os.path.join(CHECKPOINT_DIR, 'trained_model.tar.gz')

if not os.path.exists(os.path.join(MODEL_SUBDIR, 'positive_features_train.npy')):
    if not os.path.exists(tar_path):
        print(f"ERROR: Checkpoint not found at {tar_path}")
        sys.exit(1)

    print(f"  Extracting {tar_path} ...")
    with tarfile.open(tar_path, 'r:gz') as tf:
        names = tf.getnames()
        top_dir = names[0].split('/')[0]
        tf.extractall('/content')

    extracted = f'/content/{top_dir}'
    if extracted != MODEL_SUBDIR:
        if os.path.exists(MODEL_SUBDIR):
            shutil.rmtree(MODEL_SUBDIR)
        shutil.move(extracted, MODEL_SUBDIR)
    print("  Features restored.")
else:
    print("  Features already present, skipping extraction.")

for f in ['positive_features_train.npy', 'negative_features_train.npy',
          'positive_features_test.npy', 'negative_features_test.npy']:
    path = os.path.join(MODEL_SUBDIR, f)
    if not os.path.exists(path):
        print(f"ERROR: Missing required file: {path}")
        sys.exit(1)
print("  All feature files verified.")

# ============================================================
# STEP 3.5: Merge personal features (v7)
# ============================================================
# Idempotent guard. Training runs can be re-attempted in the same notebook.
# We record the merge in a sentinel so we don't double-oversample on rerun.
MERGE_SENTINEL = os.path.join(MODEL_SUBDIR, '.personal_features_merged')
PERSONAL_HN_PATH = os.path.join(MODEL_SUBDIR, 'personal_hard_negative_features.npy')

print("[3.5/9] Merging personal features...")
if os.path.exists(MERGE_SENTINEL):
    print("  Already merged on a prior run (sentinel present). Skipping.")
    if not os.path.exists(PERSONAL_HN_PATH):
        print(f"  WARNING: sentinel present but {PERSONAL_HN_PATH} missing — "
              "rerun will fail. Delete sentinel to re-merge.")
else:
    if not os.path.exists(PERSONAL_FEATURES_TAR):
        print(f"ERROR: personal features tar not found: {PERSONAL_FEATURES_TAR}")
        print("  Produce it locally with: "
              "python3 tools/collect_personal_features.py "
              "--tarball personal_features.tar.gz")
        print("  Then upload to Drive checkpoints folder.")
        sys.exit(1)

    import numpy as np

    # Extract into a temp dir beside MODEL_SUBDIR to avoid path collisions.
    pf_staging = os.path.join(OUTPUT_DIR, '_personal_features_staging')
    os.makedirs(pf_staging, exist_ok=True)
    with tarfile.open(PERSONAL_FEATURES_TAR, 'r:gz') as tf:
        tf.extractall(pf_staging)
    print(f"  Extracted personal features to {pf_staging}")

    pos_npy = os.path.join(pf_staging, 'personal_positive.npy')
    neg_npy = os.path.join(pf_staging, 'personal_hard_negative.npy')
    for p in (pos_npy, neg_npy):
        if not os.path.exists(p):
            print(f"ERROR: missing in tar: {p}")
            sys.exit(1)

    personal_pos = np.load(pos_npy)
    personal_neg = np.load(neg_npy)
    print(f"  Loaded personal_positive: {personal_pos.shape}")
    print(f"  Loaded personal_hard_negative: {personal_neg.shape}")

    # Merge positives into existing positive_features_train.npy.
    # Shape must be (N, 16, 96) float32 to match what train.py expects.
    orig_pos_path = os.path.join(MODEL_SUBDIR, 'positive_features_train.npy')
    orig_pos = np.load(orig_pos_path)
    print(f"  Original synthetic positives: {orig_pos.shape}")

    # Oversample personal positives to boost their per-batch sampling prob.
    tiled = np.tile(personal_pos, (PERSONAL_POSITIVE_OVERSAMPLE, 1, 1))
    print(f"  Oversampled {PERSONAL_POSITIVE_OVERSAMPLE}x → {tiled.shape}")

    # Defensive shape + dtype alignment. Original is float32; cast to match.
    if tiled.dtype != orig_pos.dtype:
        tiled = tiled.astype(orig_pos.dtype)
    if tiled.shape[1:] != orig_pos.shape[1:]:
        print(f"ERROR: shape mismatch {tiled.shape} vs {orig_pos.shape}")
        sys.exit(1)

    merged_pos = np.concatenate([orig_pos, tiled], axis=0)
    # Atomic write — swap in only after the new file is fully written.
    # np.save(path, ...) auto-appends '.npy' when path doesn't already end in
    # '.npy', so we pass an open handle to write to exactly tmp_path.
    tmp_path = orig_pos_path + '.tmp'
    with open(tmp_path, 'wb') as f:
        np.save(f, merged_pos)
    os.replace(tmp_path, orig_pos_path)
    print(f"  positive_features_train.npy now {merged_pos.shape}")

    # Hard-negatives go into their own file / class. Dtype match matters.
    if personal_neg.dtype != np.float32:
        personal_neg = personal_neg.astype(np.float32)
    np.save(PERSONAL_HN_PATH, personal_neg)
    print(f"  personal_hard_negative_features.npy written: {personal_neg.shape}")

    with open(MERGE_SENTINEL, 'w') as f:
        f.write(
            f"orig_synthetic={orig_pos.shape[0]}  "
            f"personal_positives={personal_pos.shape[0]}x{PERSONAL_POSITIVE_OVERSAMPLE}  "
            f"personal_hard_negatives={personal_neg.shape[0]}\n"
        )
    print("  Merge complete.")

# ============================================================
# STEP 4: Download ACAV100M features (~4GB)
# ============================================================
print("[4/9] Downloading ACAV100M features (~4GB, this takes a while)...")
ACAV_FEATURES = os.path.join(OUTPUT_DIR, 'openwakeword_features_ACAV100M_2000_hrs_16bit.npy')

if not os.path.exists(ACAV_FEATURES):
    from huggingface_hub import hf_hub_download
    downloaded = hf_hub_download(
        'davidscripka/openwakeword_features',
        'openwakeword_features_ACAV100M_2000_hrs_16bit.npy',
        repo_type='dataset',
        local_dir=OUTPUT_DIR
    )
    if not os.path.exists(ACAV_FEATURES) and os.path.exists(downloaded):
        shutil.move(downloaded, ACAV_FEATURES)
    print("  ACAV100M features downloaded.")
else:
    print("  ACAV100M features already present.")

# ============================================================
# STEP 5: Download validation features (~100MB)
# ============================================================
print("[5/9] Downloading validation features (~100MB)...")
VAL_FEATURES = os.path.join(OUTPUT_DIR, 'validation_set_features.npy')

if not os.path.exists(VAL_FEATURES):
    from huggingface_hub import hf_hub_download
    downloaded = hf_hub_download(
        'davidscripka/openwakeword_features',
        'validation_set_features.npy',
        repo_type='dataset',
        local_dir=OUTPUT_DIR
    )
    if not os.path.exists(VAL_FEATURES) and os.path.exists(downloaded):
        shutil.move(downloaded, VAL_FEATURES)
    print("  Validation features downloaded.")
else:
    print("  Validation features already present.")

# ============================================================
# STEP 6: Ensure RIR/background dirs exist
# ============================================================
print("[6/9] Ensuring RIR and background directories exist...")
rir_dir = os.path.join(OUTPUT_DIR, 'mit_rirs')
bg_dir = os.path.join(OUTPUT_DIR, 'background_clips')
os.makedirs(rir_dir, exist_ok=True)
os.makedirs(bg_dir, exist_ok=True)

# ============================================================
# STEP 6.5: Seed positive_test/ with dummy WAVs (safety net)
# ============================================================
# openwakeword/train.py lines 741-745 unconditionally globs positive_test/
# for 50 WAVs to auto-tune total_length, even when --generate_clips is off.
# If the restored tarball doesn't include those WAVs the script dies with
# `ValueError: high <= 0` from np.random.randint on an empty list.
# Seed 50 silent 32000-sample WAVs as a safety net — total_length is already
# pinned to 32000 in the config below, so the computed median is a no-op.
print("[6.5/9] Seeding positive_test/ with dummy WAVs (safety net)...")
from scipy.io import wavfile as _wavfile
import numpy as _np
_pos_test_dir = os.path.join(MODEL_SUBDIR, 'positive_test')
os.makedirs(_pos_test_dir, exist_ok=True)
_existing = [p for p in os.listdir(_pos_test_dir) if p.endswith('.wav')]
if len(_existing) < 50:
    _silence = _np.zeros(32000, dtype=_np.int16)
    for _i in range(50 - len(_existing)):
        _wavfile.write(os.path.join(_pos_test_dir, f'_seed_{_i:03d}.wav'),
                       16000, _silence)
    print(f"  Seeded {50 - len(_existing)} dummy WAVs "
          f"({len(_existing)} already present)")
else:
    print(f"  positive_test/ already has {len(_existing)} WAVs, no seed needed")

# ============================================================
# STEP 7: Write training config YAML
# ============================================================
print("[7/9] Writing training config...")
import yaml

config = {
    'model_name': MODEL_NAME,
    'target_phrase': ['hey vox'],
    'custom_negative_phrases': [
        'hey box', 'hey fox', 'hey socks', 'hey rocks', 'hey docs',
        'next box', 'gray fox', 'hey boss', 'hey bro', 'hey bob',
        'hey there', 'hey man', 'hey you', 'hey what', 'okay',
        'hey siri', 'alexa', 'okay google', 'hey google',
        'hey cortana', 'hey jarvis', 'computer'
    ],
    'n_samples': 50000,
    'n_samples_val': 5000,
    'tts_batch_size': 64,
    'augmentation_batch_size': 16,
    'augmentation_rounds': 2,
    'piper_sample_generator_path': PIPER_DIR,
    'output_dir': OUTPUT_DIR,
    # total_length: normally computed by --augment_clips from clip durations.
    # 32000 = 2 seconds at 16kHz, standard minimum for short wake words.
    'total_length': 32000,
    'rir_paths': [rir_dir],
    'background_paths': [bg_dir],
    'background_paths_duplication_rate': [1],
    'feature_data_files': {
        'ACAV100M_sample': ACAV_FEATURES,
        # v7: personal hard-negatives. Any key here that is not literally
        # "positive" gets label 0 via openwakeword/train.py's label_transforms
        # loop (line ~832). That makes it a negative class.
        'personal_hard_negative': PERSONAL_HN_PATH,
    },
    'batch_n_per_class': {
        'ACAV100M_sample': 1024,
        'adversarial_negative': 128,
        'positive': 128,
        # v7: same per-batch weight as adversarial_negative. Combined with
        # the small pool size (~757), personal hard-negatives will be seen
        # multiple epochs per run — that's the intent.
        'personal_hard_negative': 128,
    },
    'false_positive_validation_data_path': VAL_FEATURES,
    'model_type': 'dnn',
    'layer_size': 64,
    'steps': 75000,
    'max_negative_weight': 2000,
    'target_false_positives_per_hour': 0.1,
}

config_path = os.path.join(OUTPUT_DIR, 'retrain_config.yaml')
with open(config_path, 'w') as f:
    yaml.dump(config, f, default_flow_style=False)
print(f"  Config written to {config_path}")

# ============================================================
# STEP 8: Run training via subprocess
# ============================================================
print("[8/9] Starting training (75000 steps, ~20-40 min on Colab GPU)...")
print("=" * 60)
print()

# PYTHONPATH must include piper-sample-generator for generate_samples import.
# Training will:
#   1. Load features, build model, train 75K steps
#   2. Export ONNX model (train.py line 898)
#   3. Try TFLite conversion (line 901) - WILL FAIL (no tensorflow)
#      This is fine: ONNX is already saved at step 2.
env = os.environ.copy()
env['PYTHONPATH'] = PIPER_DIR + ':' + env.get('PYTHONPATH', '')

# Force multiprocessing to 'fork'. Colab/Linux already defaults to 'fork',
# so this is a no-op there; belt-and-suspenders in case the default ever
# flips to 'spawn' (Python 3.14+ forkserver migration, etc.). The
# DataLoader built at train.py L864 uses num_workers=cpu_count//2 and its
# worker closure contains a lambda (the `f` data-transform at L818) —
# under 'spawn' that pickles and fails with `Can't pickle <lambda>`.
_bootstrap = (
    "import multiprocessing as mp, runpy, sys; "
    "mp.set_start_method('fork', force=True); "
    "runpy.run_module('openwakeword.train', run_name='__main__', alter_sys=True)"
)
result = subprocess.run(
    [sys.executable, '-c', _bootstrap,
     '--training_config', config_path,
     '--train_model'],
    env=env,
    capture_output=False
)

if result.returncode != 0:
    print()
    print(f"NOTE: Training process exited with code {result.returncode}")
    print("This is expected if it failed on TFLite conversion (after ONNX export).")
    print("Checking if ONNX model was generated...")

# ============================================================
# STEP 9: Find, bundle, and save the ONNX model
# ============================================================
print()
print("[9/9] Looking for trained ONNX model...")

onnx_files = glob.glob(os.path.join(OUTPUT_DIR, '**', '*.onnx'), recursive=True)
# Exclude .onnx.data files AND openwakeword resource models
onnx_files = [f for f in onnx_files
              if not f.endswith('.onnx.data')
              and 'resources/models' not in f
              and '.cache' not in f]

if not onnx_files:
    print("ERROR: No .onnx model found after training!")
    print()
    print("Contents of output dir:")
    for root, dirs, files in os.walk(OUTPUT_DIR):
        # Skip large directories
        if any(skip in root for skip in ['negative_train', 'negative_test', 'positive_train', 'positive_test', '.cache']):
            continue
        level = root.replace(OUTPUT_DIR, '').count(os.sep)
        indent = ' ' * 2 * level
        print(f"{indent}{os.path.basename(root)}/")
        subindent = ' ' * 2 * (level + 1)
        for file in files[:10]:
            filepath = os.path.join(root, file)
            size = os.path.getsize(filepath)
            print(f"{subindent}{file} ({size:,} bytes)")
        if len(files) > 10:
            print(f"{subindent}... and {len(files) - 10} more files")
    sys.exit(1)

print(f"  Found model(s): {onnx_files}")

model_path = None
for f in onnx_files:
    if 'hey_vox' in os.path.basename(f):
        model_path = f
        break
if model_path is None:
    model_path = onnx_files[0]

print(f"  Using: {model_path}")
model_size = os.path.getsize(model_path)
print(f"  Size: {model_size:,} bytes")

data_file = model_path + '.data'
if os.path.exists(data_file) or model_size < 100_000:
    print("  Bundling into single ONNX file...")
    import onnx
    model = onnx.load(model_path)
    output_path = os.path.join(CHECKPOINT_DIR, 'hey_vox_complete.onnx')
    onnx.save_model(model, output_path, save_as_external_data=False)
    print(f"  Bundled model saved to: {output_path}")
    print(f"  Bundled size: {os.path.getsize(output_path):,} bytes")
else:
    output_path = os.path.join(CHECKPOINT_DIR, 'hey_vox_complete.onnx')
    shutil.copy2(model_path, output_path)
    print(f"  Model copied to: {output_path}")

print()
print("=" * 60)
print("SUCCESS! Model saved to Google Drive:")
print(f"  {output_path}")
print()
print("Run in next cell:")
print("  from google.colab import files")
print("  files.download('/content/drive/MyDrive/heyvox_training_checkpoints/hey_vox_complete.onnx')")
print("=" * 60)
