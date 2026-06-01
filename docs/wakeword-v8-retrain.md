# Wake-word retrain — v8 runbook

End-to-end recipe to retrain the `hey_vox` wake-word model. Local steps run on
the Mac (featurisation, gate eval); the actual training runs on Colab (GPU).

## Why v8 (the baseline that motivates it)

A real gate evaluation of the **currently deployed** `~/.config/heyvox/models/hey_vox.onnx`
against 5.39 h of LibriSpeech dev-clean (`tools/fp_rate_eval.py`):

| threshold | FP (over 5.39 h) | FP/h | recall (friends) | D-16 gate |
|-----------|------------------|------|------------------|-----------|
| 0.5–0.8 | 8 | 1.48 | 96.7 % | ❌ |
| 0.9 | 7 | 1.30 | 96.7 % | ❌ |
| 0.95 | 6 | 1.11 | 95.0 % | ❌ |

- **Recall is excellent and threshold-stable (95–97 %).** Not the problem.
- **Precision fails the gate**: ~1.1–1.5 FP/h vs the `<1/h` target. Over 5.39 h,
  `<1/h` means `≤5` FPs — we have 6–8, so we must remove ~3.
- **Threshold tuning can't fix it**: 0.5→0.95 drops FPs only 8→6 because 6 of
  them score `≥0.95`. They are high-confidence false fires.
- **Diagnosis** (`tools/fp_diagnose.py` + Whisper on the trigger windows): the 8
  FPs are unrelated fragments of ordinary read speech ("…thinks **of**",
  "Jean **Val**jean", "so **fast** to wake up", "…**faith**ful to Henry") with
  **no shared "hey vox" homophone**. This is an over-broad positive boundary
  from training on synthetic TTS positives + too few natural-speech negatives —
  the fixable kind, curable with hard-negatives at low recall risk.

**v8 fix:** add windowed connected speech (LibriSpeech) as a *second* negative
class `speech_hard_negative`, alongside the user's own ambient
`personal_hard_negative` (TN + FP). Two classes → independent per-batch weights,
so generic speech doesn't dilute the user-specific runtime negatives.

## Data inventory (collected over ~6 weeks of real use, ~40 active days)

`~/.config/heyvox/training/`: 1000 tp, 1003 fn, 950 tn, 246 fp, 381 positives,
60 friends. Featurised into `personal_features.tar.gz` (2459 positives +
1807 hard-negatives) — **+55 % / +139 % vs v7**.

> ⚠️ Label noise: the fn/fp labels are auto-collected, not ground truth. A
> Whisper sanity pass over `fn` (recover via `reference_garbled_fp_recovery`)
> before training avoids teaching the model to fire on non-wake audio.

## Step 1 — Local featurisation (on the Mac)

```bash
# 1a. Personal clips → personal_positive + personal_hard_negative
python3 tools/collect_personal_features.py \
    --out-dir /tmp/personal_features \
    --tarball /tmp/personal_features.tar.gz

# 1b. Connected-speech negatives → speech_hard_negative (windowed, flac-aware)
#     Get a corpus first, e.g. LibriSpeech dev-clean (~337 MB, 5.4 h):
#       curl -fL -o /tmp/dev-clean.tar.gz https://www.openslr.org/resources/12/dev-clean.tar.gz
#       tar -xzf /tmp/dev-clean.tar.gz -C /tmp
python3 tools/featurise_speech_negatives.py \
    --corpus /tmp/LibriSpeech/dev-clean \
    --tarball /tmp/speech_negatives.tar.gz \
    --max-windows 12000
```

## Step 2 — Upload to Google Drive

Copy both tarballs into the Colab checkpoint folder
(`heyvox_training_checkpoints`, see `reference_gdrive_training_checkpoints`):

- `personal_features.tar.gz`
- `speech_negatives.tar.gz`  ← v8 addition (optional; absent → v8 == v7)
- `retrain_heyvox_v8.py`

The pre-trained synthetic feature checkpoint `trained_model.tar.gz` must already
be in that folder (reused from v7).

## Step 3 — Train on Colab (GPU runtime)

```python
# Cell 1
from google.colab import drive
drive.mount('/content/drive')
!pip install -q git+https://github.com/dscripka/openWakeWord.git
!pip install -q onnx onnxruntime huggingface_hub pyyaml torch torchaudio
!pip install -q torchinfo torchmetrics speechbrain audiomentations torch-audiomentations
!pip install -q onnxscript mutagen acoustics pronouncing deep-phonemizer
!pip install -q webrtcvad espeak-phonemizer soundfile scipy datasets librosa
!python /content/drive/MyDrive/heyvox_training_checkpoints/retrain_heyvox_v8.py

# Cell 2
from google.colab import files
files.download('/content/drive/MyDrive/heyvox_training_checkpoints/hey_vox_complete.onnx')
```

`retrain_heyvox_v8.py` merges both negative classes (STEP 3.5 personal, STEP 3.6
speech), registers them in the training config, trains 75k steps, and writes
`hey_vox_complete.onnx` back to Drive. Tune `SPEECH_HN_BATCH_N` (default 256) in
the script to weight the speech class more/less aggressively.

## Step 4 — Gate eval the candidate (on the Mac, BEFORE deploying)

```bash
python3 tools/fp_rate_eval.py \
    --model /path/to/hey_vox_complete.onnx \
    --negatives /tmp/LibriSpeech/dev-clean \
    --positives /tmp/heyvox-baseline/pos_friends
```

**Ship criterion:** `≤5 FP` over the 5.39 h corpus (`<1/h`) AND recall `≥95 %`
on the friends set, at the same threshold. Compare against the baseline table
above — only deploy if it strictly improves precision without losing recall.

> For a tighter number, extend the negative corpus to ~11 h (add LibriSpeech
> `test-clean`). And `--positives` should ideally be a held-out set the model
> was NOT trained on — the friends clips qualify only if excluded from Step 1b.

## Step 5 — Deploy

Replace `~/.config/heyvox/models/hey_vox.onnx` with the validated candidate and
restart: `launchctl kickstart "gui/$UID/com.heyvox.listener"`. Keep the previous
model as `hey_vox.onnx.bak` for instant rollback.

## Files

| File | Role |
|------|------|
| `tools/collect_personal_features.py` | personal clips → tarball (positives + ambient negs) |
| `tools/featurise_speech_negatives.py` | **v8** windowed speech corpus → speech-neg tarball |
| `tools/featurise_clips.py` | shared featuriser (melspec+embedding, last-2s per clip) |
| `tools/retrain_heyvox_v8.py` | **v8** Colab trainer (two negative classes) |
| `tools/fp_rate_eval.py` | fast large-corpus FP/TP gate eval (single-pass, flac) |
| `tools/fp_diagnose.py`* | per-utterance FP diagnostic with transcripts |
| `training/evaluate_model.py` | original D-16 gate (small corpus; onnx-framework fixed) |

*`fp_diagnose.py` is a throwaway diagnostic; lives in `/tmp` unless promoted.
