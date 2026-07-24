# HeyVox Release Runbook

Maintainer checklist for shipping a new HeyVox version. Solo-maintainer
workflow, no team coordination needed.

## Prerequisites (one-time, per maintainer machine)

- `gh` CLI authenticated against an account with `heyvox-dev` org access
- PyPI Trusted Publisher already registered for the `heyvox` project (OIDC,
  no stored token — see `.github/workflows/publish.yml`, environment `pypi`)
- `python -m build` working in a clean venv (`pip install build`), for local
  sanity checks before pushing a tag

## Release sequence (per version)

### 1. Land fixes on the release branch

Fixes accumulate as normal commits (`fix(...): DEF-NNN ...`) on a branch
named `heyvox/release-X.Y.Z`, merged into `main` via PR as they're ready.
CI (`.github/workflows/ci.yml` — ruff lint + pytest) must be green on `main`
before proceeding to the next step.

### 2. Confirm every fix is logged

Per CLAUDE.md's Defect Log Protocol, each fix should already have a
`.planning/DEFECT-LOG.md` entry from when it was made — this step is just a
confirmation, not a batch write:

```
git log <last-tag>..HEAD --oneline | grep -oE 'DEF-[0-9]+' | sort -u
grep -c "## DEF-<number>" .planning/DEFECT-LOG.md   # for each one
```

### 3. Bump the version

```
git checkout -b heyvox/release-X.Y.Z   # if not already on one
```

Edit `pyproject.toml` → `[project].version = "X.Y.Z"`. Nothing else needs
touching — `heyvox/__init__.py` reads the version from `importlib.metadata`
at runtime.

```
git add pyproject.toml
git commit -m "chore: bump version to X.Y.Z"
git push
```

### 4. Update CHANGELOG.md — BEFORE tagging

Add a new `## [X.Y.Z] - YYYY-MM-DD` section above the previous version,
summarizing the DEF-numbered fixes from step 2 in Keep-a-Changelog
categories (Added / Changed / Fixed / Security). Add the new compare link
at the bottom and repoint `[Unreleased]`:

```
[Unreleased]: https://github.com/heyvox-dev/heyvox/compare/vX.Y.Z...HEAD
[X.Y.Z]: https://github.com/heyvox-dev/heyvox/compare/v<prev>...vX.Y.Z
```

1.1.1 through 1.1.3 all shipped without a changelog entry — this step exists
specifically so that doesn't happen again. Commit it with the version bump
or as a follow-up commit on the same branch, merged before tagging.

### 5. Merge to main, then tag and push

The tag is what actually triggers the release — merging the version bump
alone does nothing.

```
git tag vX.Y.Z
git push origin vX.Y.Z
gh run watch --repo heyvox-dev/heyvox
```

`.github/workflows/publish.yml` triggers on any `v*` tag push:
1. Builds on `macos-14`.
2. **Verifies the tag matches `pyproject.toml`'s version** and fails the
   build otherwise (`TAG != PY` → `exit 1`) — a real safety net, not just a
   convention.
3. Publishes to PyPI via OIDC (`pypa/gh-action-pypi-publish`, environment
   `pypi`, `id-token: write`) — no stored credentials.

Verify after the run completes:

```
pip install heyvox==X.Y.Z   # in a fresh venv
```

### 6. Wake-word model — no separate step today

Unlike what earlier Phase 14 planning assumed, the wake-word `.onnx` files
are **bundled directly in the package** (`pyproject.toml`
`[tool.setuptools.package-data]` → `models/*.onnx`, `models/oww/*.onnx`),
not downloaded from a GitHub Release. Nothing to upload for a routine
release.

The shipped default is still `hey_jarvis_v0.1` (`heyvox/config.py`). A
custom-trained `hey_vox` model exists (training pipeline under `training/`,
see `docs/wakeword-v8-retrain.md`) but has not been wired in as the default
yet — that's a separate, not-yet-scheduled change, not part of this
routine flow.

### 7. Homebrew — not set up yet

`heyvox-dev/homebrew-heyvox` doesn't exist. `pip install heyvox` /
`pip install 'heyvox[apple-silicon]'` / `pip install 'heyvox[tts]'` is the
only supported install path today. See
`.planning/phases/14-distribution-ux-polish/14-05-PLAN.md` for the scoped
formula-authoring steps (tap repo creation, `homebrew-pypi-poet` resource
generation, `brew audit --strict`) when that gets picked up — it needs
`gh`/`brew` hands-on time and can't be automated end-to-end.

## Pitfalls

- **CHANGELOG drift** — three consecutive releases shipped without an
  entry. Step 4 exists to stop that; don't skip it under time pressure.
- **Unpinned lint/tooling deps** — an unpinned `ruff` broke CI on a
  version-bump-only commit once already (DEF-222). `ruff` is now pinned in
  `pyproject.toml`'s dev dependencies; if you bump that pin, run full CI
  before merging, not just locally.
- **Tag before CI is green on `main`** — the tag push triggers the publish
  workflow immediately; there's no separate manual-approval step in
  `publish.yml` today. Don't push the tag until the merge commit's CI run
  is green.

## Related docs

- `docs/wakeword-v8-retrain.md` — wake-word training/retraining notes
- `.planning/DEFECT-LOG.md` — per-fix detail behind every changelog entry
- `.planning/phases/14-distribution-ux-polish/` — Phase 14 design context
  (PyPI publish workflow, Homebrew tap, HUD mic display)
