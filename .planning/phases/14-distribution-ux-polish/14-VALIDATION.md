---
phase: 14
slug: distribution-ux-polish
status: "draft — tests-first within owning Wave 1 plan (no discrete Wave 0); executor flips nyquist_compliant on sign-off"
nyquist_compliant: false
wave_0_complete: false
created: 2026-05-11
---

# Phase 14 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.
> Detailed methodology, code examples, and threshold-sweep logic in
> `14-RESEARCH.md` § "Validation Architecture (D-16 + SPEC R6 ship gate)".

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 7.x (already configured — `pyproject.toml:103-110`) |
| **Config file** | `pyproject.toml [tool.pytest.ini_options]` |
| **Quick run command** | `pytest tests/ --ignore=tests/test_e2e.py --ignore=tests/test_stress.py -v --tb=short` |
| **Full suite command** | `pytest tests/` |
| **Estimated runtime** | ~60 seconds (quick) / ~3-5 min (full) |
| **Wake-word ship-gate eval** | `python training/evaluate_model.py --model models/hey_vox.onnx --positives test/real_voice/ --negatives test/fp_corpus/ --threshold 0.7` (NEW — Phase 14 deliverable) |
| **Formula audit** | `brew audit --strict heyvox-dev/heyvox/heyvox` (manual, post-tap-creation) |
| **Smoke install test** | reuse `.github/workflows/install-test.yml` shape; add post-publish PyPI smoke step |

---

## Sampling Rate

- **After every task commit:** Run `pytest tests/ --ignore=tests/test_e2e.py --ignore=tests/test_stress.py -v --tb=short` (quick suite)
- **After every plan wave:** Run `pytest tests/` (full suite, excludes audio-requiring fixtures unless mics present)
- **Before `/gsd-verify-work`:** Full suite must be green; wake-word ship-gate eval must be green if Wave touches `training/` or `hey_vox.onnx`.
- **Max feedback latency:** 90 seconds (quick suite) for typical task commits.

---

## Per-Task Verification Map

> **Planner fills this table** during `/gsd-plan-phase` by mapping each PLAN.md task to a SPEC.md requirement and a test command. The grid below seeds the requirement → test mapping; the planner adds Task ID rows once plans exist.

### Requirement → Test Mapping (seed — from RESEARCH.md § Validation Architecture)

| Requirement | Behavior | Test Type | Automated Command | File Exists |
|-------------|----------|-----------|-------------------|-------------|
| SPEC-R1 | `publish.yml` syntactically valid | static | `actionlint .github/workflows/publish.yml` (run inside CI via `rhysd/actionlint-action@v1` step in `ci.yml`) | ❌ created in 14-01 |
| SPEC-R1 | Tag push triggers PyPI publish | manual smoke | Push tag `v0.0.1rc` → check PyPI page | manual (one-time) |
| SPEC-R2 | `__version__` reads from `importlib.metadata` | unit | `pytest tests/test_version.py -x` | ❌ created in 14-01 |
| SPEC-R2 | Classifier bumped to Beta | static grep | `grep "Development Status :: 4 - Beta" pyproject.toml` | ❌ updated in 14-01 |
| SPEC-R3 | Formula passes `brew audit --strict` | manual | `brew audit --strict heyvox-dev/heyvox/heyvox` | ❌ created in 14-05 |
| SPEC-R3 | `brew install heyvox` succeeds on macOS-14 arm64 | manual smoke | `brew tap heyvox-dev/heyvox && brew install heyvox && heyvox --help` | ❌ created in 14-05 |
| SPEC-R4 | Menu-bar title shows truncated friendly mic name | unit | `pytest tests/test_menu_bar_title.py -x` | ❌ created in 14-02 |
| SPEC-R4 | Tooltip shows full friendly name | unit | (same test file) | ❌ created in 14-02 |
| SPEC-R5 | Submenu shows `voice_isolation_mode` from active profile | unit | `pytest tests/test_overlay_vi_suffix.py -x` | ❌ created in 14-02 |
| SPEC-R5 | No `AVCaptureDevice` import added | static grep | `grep -r AVCaptureDevice heyvox/` returns empty | ❌ regression-guarded in 14-02 |
| SPEC-R6 | Wake-word model meets TP ≥ 70% AND FP < 1/hour | scripted eval | `python training/evaluate_model.py …` | ❌ created in 14-03 |
| SPEC-R6 | Default config has `wake_words.also_load: [hey_jarvis_v0.1]` | unit | `pytest tests/test_config_defaults.py::test_co_default_wake_words -x` | ❌ created in 14-04 |
| SPEC-R6 | Setup wizard downloads `hey_vox.onnx` when absent | integration (HTTP mocked) | `pytest tests/test_setup_wakeword_download.py -x` | ❌ created in 14-04 |
| SPEC-R6 | Setup wizard preserves existing user-trained model | unit | (same test file) | ❌ created in 14-04 |
| SPEC-R6 | `heyvox setup --redownload-wakeword` overrides idempotency | unit | (same test file) | ❌ created in 14-04 |

### Per-Task Grid (filled by planner)

| Task ID | Plan | Wave | Requirement | Threat Ref | Secure Behavior | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|------------|-----------------|-----------|-------------------|-------------|--------|
| _planner fills during /gsd-plan-phase_ | | | | | | | | | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 1 tests-first artifacts (created inside the owning plan, not as a discrete Wave 0)

> **Model:** This phase does NOT use a separate Wave 0 to scaffold tests. Each Wave 1 plan
> creates its own tests *first* (RED) inside the same task that ships the production code,
> per the per-plan `tdd="true"` task flag. This satisfies the Nyquist-sampling intent
> (every implementation change is paired with an automated verification artifact) without
> the overhead of a discrete bootstrap wave for a phase with only 6 plans. The executor
> flips `nyquist_compliant: true` in this frontmatter once all the artifacts below are
> committed and green.

These are **net-new test/eval artifacts** the planner has routed into their owning Wave 1 plans
(or earliest wave that gates a downstream change):

- [ ] `tests/test_version.py` — assert `heyvox.__version__ == importlib.metadata.version("heyvox")` (SPEC-R2) → **owned by 14-01 Task 1**
- [ ] `tests/test_menu_bar_title.py` — assert `truncate_mic()` truncates correctly + `format_menu_bar_title()` returns title + tooltip + flags (SPEC-R4) → **owned by 14-02 Task 1**
- [ ] `tests/test_overlay_vi_suffix.py` — assert submenu entries append the active profile's `voice_isolation_mode` and no `AVCaptureDevice` import is added (SPEC-R5) → **owned by 14-02 Task 1**
- [ ] `tests/test_config_defaults.py::test_co_default_wake_words` — assert `WakeWordConfig().also_load == ["hey_jarvis_v0.1"]` (SPEC-R6 / D-18 already in code; this is a regression guard) → **owned by 14-04 Task 1**
- [ ] `tests/test_setup_wakeword_download.py` — mocked-HTTP integration tests for download, sha256 validation, idempotency, `--redownload-wakeword`, network-failure-preserves-existing (SPEC-R6 / D-19) → **owned by 14-04 Task 1**
- [ ] `training/evaluate_model.py` — ship-gate eval CLI per RESEARCH.md § Validation Architecture (SPEC-R6 / D-16) → **owned by 14-03**
- [ ] `.github/workflows/ci.yml` — adds `rhysd/actionlint-action@v1` step so workflow syntax errors (incl. `publish.yml`) are caught at PR-time (SPEC-R1) → **owned by 14-01 Task 2**

*Existing pytest infrastructure covers test framework needs — only new files above are required.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| First tag push triggers PyPI publish via OIDC | SPEC-R1 | Requires one-time `pending publisher` registration on PyPI.org by maintainer + real tag push; cannot be automated cheaply | (1) Register pending publisher at `pypi.org/manage/account/publishing` (project=heyvox, owner=heyvox-dev, repo=heyvox, workflow=publish.yml, env=pypi). (2) Push tag `v1.0.0rc1` to a test branch. (3) Verify `gh workflow view publish` shows green run. (4) `pip install heyvox==1.0.0rc1` from fresh venv succeeds. |
| `brew install heyvox` end-to-end | SPEC-R3 | Tap repo lives outside this checkout (`heyvox-dev/homebrew-heyvox`) — clean macOS install verification can only run post-publish | `brew tap heyvox-dev/heyvox && brew install heyvox && heyvox --help` on a clean Apple-Silicon Mac; `brew audit --strict heyvox-dev/heyvox/heyvox` must pass. |
| Wake-word ship-gate against **real** test corpus | SPEC-R6 / D-16 | Test corpus assembly (real-voice clips from `record.felberer.at` + openwakeword's bundled DiPCo/Santa-Barbara/MUSDB negatives) is data-curation work — sample diversity drives the pass/fail outcome | After training run on Colab, download `hey_vox.onnx` artefact; run `python training/evaluate_model.py --model hey_vox.onnx --positives test/real_voice/ --negatives test/fp_corpus/ --threshold 0.7 --sweep`; only upload to GitHub Releases if both gates pass simultaneously. |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or upstream-Wave dependencies that produce them in the same wave
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Tests-first-within-task: every `tdd="true"` task creates its test file before the implementation it covers, satisfying the Nyquist intent without a discrete Wave 0
- [ ] No watch-mode flags
- [ ] Feedback latency < 90s
- [ ] `nyquist_compliant: true` set in frontmatter (executor flips when all green)

**Approval:** pending
</content>
