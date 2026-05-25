---
phase: 14-distribution-ux-polish
plan: 03
status: partial
executed: 2026-05-11
requirements_addressed: [SPEC-R6]
remaining_tasks: [Task 2 (Colab training run), Task 3 (constants — depends on Task 2 sha256)]
---

# Plan 14-03 Summary — Wake-word ship-gate eval (partial)

## What was built (Task 1 — autonomous portion)

### Files created
- `training/evaluate_model.py` — D-16 ship-gate CLI, 165 lines:
  - `evaluate(model_path, positives_dir, negatives_dir, threshold)` returns dict with `tp_rate`, `tp_count`, `positive_total`, `fp_count`, `fp_per_hour`, `negative_total_seconds`, `threshold`, `gate_pass`
  - `_detect(model, wav_path, wake_name, threshold)` streams an 80ms-frame loop through `openwakeword.model.Model.predict()`, padding the tail to a FRAME_SIZE multiple, resetting model state between clips
  - `_load_wav(path)` loads + resamples to 16 kHz mono float32
  - Module-level `TP_GATE = 0.70` and `FP_PER_HOUR_GATE = 1.0` constants (D-16)
  - CLI: `--threshold` single-shot mode + `--sweep` (0.5/0.6/0.7/0.8/0.9/0.95) + exit 0 on pass / 1 on fail

### Acceptance criteria

- [x] `training/evaluate_model.py` exists
- [x] `python -c "import ast; ast.parse(open('training/evaluate_model.py').read())"` exits 0
- [x] `python training/evaluate_model.py --help` exits 0; help includes `--threshold` + `--sweep`
- [x] D-16 gate values bound to module-level constants (`TP_GATE = 0.70`, `FP_PER_HOUR_GATE = 1.0`); evaluated as `tp_rate >= TP_GATE and fp_per_hour < FP_PER_HOUR_GATE`
- [x] `def evaluate` + `def _detect` present
- [x] `openwakeword` import + `Model(wakeword_models=[...])` + `model.predict()` pattern present (lazy-imported inside `evaluate()` so module-load doesn't require openwakeword)

## What's deferred (Task 2 + Task 3 — maintainer-only)

### Task 2 — Manual Colab training + GitHub Releases upload

Cannot run in this session (Colab notebook execution + asset upload). Maintainer steps remain:

1. **Phase A — Training (~1 h Colab GPU):** Open `training/hey_vox_colab.ipynb` in Colab, mount Drive folder `heyvox_training_checkpoints` (id `1DZ02RE8zZiU4r6LkyMTa_ofYRrZezzcu`), run all cells, download resulting `hey_vox.onnx`.
2. **Phase B — Test set assembly:** Build `~/heyvox-eval/test/real_voice/` (held-out positives from `record.felberer.at`) + `~/heyvox-eval/test/fp_corpus/` (openwakeword's bundled DiPCo + Santa Barbara + MUSDB negatives, ~11h).
3. **Phase C — Ship-gate evaluation:**
   ```bash
   python training/evaluate_model.py \
       --model ~/Downloads/hey_vox.onnx \
       --positives ~/heyvox-eval/test/real_voice/ \
       --negatives ~/heyvox-eval/test/fp_corpus/ \
       --sweep
   ```
   Pass-criterion: exit code 0, at least one threshold returns `gate_pass=true`.
4. **Phase D — Upload (CRITICAL ORDERING per RESEARCH.md Pitfall 6):**
   ```bash
   shasum -a 256 ~/Downloads/hey_vox.onnx  # → record 64-char hex
   gh release upload v1.0.0 ~/Downloads/hey_vox.onnx --repo heyvox-dev/heyvox --clobber
   # — OR if v1.0.0 release doesn't yet exist:
   gh release create v1.0.0 ~/Downloads/hey_vox.onnx --repo heyvox-dev/heyvox --title "v1.0.0" --draft
   ```
   The `.onnx` asset MUST land before the git tag `v1.0.0` is pushed (Pitfall 6 — publish.yml expects the release page to exist with the asset).

### Task 3 — `heyvox/constants.py` constants (blocked on Task 2 sha256)

Append to `heyvox/constants.py` once the maintainer has the real sha256:

```python

# ---------------------------------------------------------------------------
# Wake-word model bundle (Phase 14 / SPEC R6 / D-17, D-19)
# ---------------------------------------------------------------------------
HEY_VOX_MODEL_URL = (
    "https://github.com/heyvox-dev/heyvox/releases/download/v1.0.0/hey_vox.onnx"
)
HEY_VOX_MODEL_SHA256 = "<64-char-hex-from-shasum>"
```

Then verify:
```bash
python -c "from heyvox.constants import HEY_VOX_MODEL_URL, HEY_VOX_MODEL_SHA256; \
    assert HEY_VOX_MODEL_URL.startswith('https://github.com/heyvox-dev/heyvox/releases/download/v'); \
    assert len(HEY_VOX_MODEL_SHA256) == 64; \
    assert all(c in '0123456789abcdef' for c in HEY_VOX_MODEL_SHA256.lower()); \
    print('OK')"
```

This unblocks Plan 14-04 (`heyvox setup` download flow), which imports both constants.

## Threat model status

- **T-14-01 (Tampering — hey_vox.onnx GitHub Releases asset)** — mitigation depends on Task 3 (sha256 baked into constants.py). Pending until Task 2 produces a sha256 and Task 3 lands it.
- **T-14-01b (Spoofing — attacker substitutes a malicious .onnx)** — same mitigation; same status.
- **T-14-06 (DoS — CDN unreachable)** — accepted; Plan 14-04's download helper falls through to `hey_jarvis_v0.1` co-default on failure (D-18 / D-19).

## Files committed (Task 1 only)

Created:
- `training/evaluate_model.py`
- `.planning/phases/14-distribution-ux-polish/14-03-SUMMARY.md` (this file)

## Next steps to close 14-03 fully

1. Maintainer runs Colab notebook → downloads `hey_vox.onnx`
2. Maintainer builds test corpora + runs `python training/evaluate_model.py --sweep` → records passing threshold + sha256
3. Maintainer uploads asset to `heyvox-dev/heyvox` release v1.0.0
4. Append HEY_VOX_MODEL_URL + HEY_VOX_MODEL_SHA256 to `heyvox/constants.py` (Task 3 — small ~10-line edit)
5. Commit Task 2 confirmation + Task 3 constants change as a follow-up `feat(14): plan 03 — wake-word model shipped (sha256=...)` commit
6. Plan 14-04 (setup wizard download) can then land — it depends on the constants existing

Plan 14-06's `docs/wakeword-training.md` should reference the passing threshold from Task 2's sweep output.
