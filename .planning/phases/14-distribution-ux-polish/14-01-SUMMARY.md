---
phase: 14-distribution-ux-polish
plan: 01
status: complete
executed: 2026-05-11
requirements_addressed: [SPEC-R1, SPEC-R2, DIST-01, DIST-02]
---

# Plan 14-01 Summary — PyPI publish workflow + version sync

## What was built

### Files created
- `.github/workflows/publish.yml` — tag-triggered (`v*`) two-job workflow (build on macos-14 + publish on ubuntu-latest). Build runs `python -m build`; publish uses `pypa/gh-action-pypi-publish@release/v1` via OIDC with `id-token: write` scoped to the publish job only. Environment `pypi` block ready for deployment protection rules later. No `workflow_dispatch` (D-01).
- `tests/test_version.py` — 4 regression tests across two classes:
  - `TestVersion.test_version_is_string` — `heyvox.__version__` is a non-empty str
  - `TestVersion.test_version_matches_pyproject` — runtime version equals pyproject `[project].version` (or `0.0.0-dev` fallback)
  - `TestVersion.test_version_format` — semver-ish regex
  - `TestClassifierIsBeta.test_pyproject_declares_beta` — Beta in classifiers, Alpha absent

### Files modified
- `.github/workflows/ci.yml` — inserted `Lint GitHub Actions workflows` step right after `Checkout`, before `Set up Python 3.12`. Uses `rhysd/actionlint-action@v1` with `fail_on_error: true`. Catches workflow syntax typos at PR time (defends `publish.yml` against bad edits BEFORE the first tag is consumed).
- `pyproject.toml` — single-line classifier bump `Development Status :: 3 - Alpha` → `Development Status :: 4 - Beta` (D-06). Nothing else touched; `version = "1.0.0"` stays per D-05.
- `heyvox/__init__.py` — rewritten from single hardcoded line to `importlib.metadata.version("heyvox")` with `PackageNotFoundError` fallback to `"0.0.0-dev"` (D-04). Single source of truth is now `pyproject.toml`.

### Acceptance criteria

- [x] `pytest tests/test_version.py -x -v --tb=short` → 4 tests green
- [x] `grep -c "Development Status :: 4 - Beta" pyproject.toml` → 1
- [x] `grep -c "Development Status :: 3 - Alpha" pyproject.toml` → 0
- [x] `grep -c "importlib.metadata" heyvox/__init__.py` → 1
- [x] `grep -c "^__version__ = \"1.0.0\"$" heyvox/__init__.py` → 0 (hardcoded line removed)
- [x] `.github/workflows/publish.yml` exists
- [x] `grep -c "pypa/gh-action-pypi-publish@release/v1" .github/workflows/publish.yml` → 1
- [x] `grep -c "id-token: write" .github/workflows/publish.yml` → 1
- [x] `grep -c "python -m build" .github/workflows/publish.yml` → 1
- [x] Tag trigger present (`tags:` + `- 'v*'`)
- [x] `grep -c "workflow_dispatch" .github/workflows/publish.yml` → 0
- [x] `grep -c "@master" .github/workflows/publish.yml` → 0
- [x] `grep -c "rhysd/actionlint-action@v1" .github/workflows/ci.yml` → 1
- [x] `grep -c "fail_on_error: true" .github/workflows/ci.yml` → 1
- [ ] CI run on the PR shows actionlint step green — **pending PR/push** (out of scope for inline execution; will be visible on first PR for branch)

### PyPI pending publisher (Task 0)

Maintainer confirmed (2026-05-11): registered at `https://pypi.org/manage/project/heyvox/settings/publishing/`. Note that the project-level Trusted Publisher form was used (not the pending-publisher form on the account page) because `heyvox` was already a full PyPI project, not just a reserved name. Saved memory `reference_pypi_heyvox_project.md` so future research doesn't repeat the incorrect pending-publisher assumption.

Form values:
- Owner: `heyvox-dev`
- Repository: `heyvox`
- Workflow filename: `publish.yml`
- Environment name: `pypi`

GitHub side: the `pypi` environment must also exist on the repo before first tag push (`Settings → Environments → New environment → pypi`).

### Test regression check

Existing quick suite (`pytest tests/ --ignore=test_e2e --ignore=test_stress --ignore=test_defect_guards`): 636 passed, 5 skipped, **9 pre-existing failures** in `tests/test_injection*` (paste pipeline tests — verified pre-existing by stashing and re-running). No regressions introduced by 14-01.

## Threat model status

- **T-14-02 (Tampering — publish.yml)** — mitigated via OIDC binding to specific workflow file path + environment name + tag-only trigger; `id-token: write` scoped to publish job only; actionlint now catches syntax-level tampering in PR review. Maintainer should add branch protection on `main` for additional defense.
- **T-14-02b (Spoofing — OIDC token exchange)** — delegated to `pypa/gh-action-pypi-publish@release/v1`. No hand-rolled crypto.

## Open work / handoffs for downstream plans

### 14-05 (Homebrew tap)
Formula `url` field will reference `files.pythonhosted.org/packages/source/h/heyvox/heyvox-1.0.0.tar.gz` once this plan's first PyPI release succeeds. Until then, 14-05 is blocked.

### 14-06 (docs polish)
PyPI README rendering can be verified after the first publish lands. README install section update can be drafted now but final "first install can take 5-10 min" warning should land in the same release tag.

### Runbook reminder — first tag push
Before `git tag v1.0.0 && git push --tags`:
1. Confirm `pypi` environment exists in `heyvox-dev/heyvox` GitHub repo settings
2. Confirm Trusted Publisher row visible at https://pypi.org/manage/project/heyvox/settings/publishing/
3. For tags that ship the wake-word model (later releases, plan 14-04): upload `hey_vox.onnx` as a GitHub Releases asset BEFORE pushing the tag (RESEARCH.md §"Pitfall 6: GitHub Releases asset upload races with publish.yml"). The first PyPI release in 14-01 does not need an .onnx asset — model download is wired in 14-04.

## Files committed

Modified:
- `.github/workflows/ci.yml`
- `pyproject.toml`
- `heyvox/__init__.py`

Created:
- `.github/workflows/publish.yml`
- `tests/test_version.py`
- `.planning/phases/14-distribution-ux-polish/14-01-SUMMARY.md` (this file)
