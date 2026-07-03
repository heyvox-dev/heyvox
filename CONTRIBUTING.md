# Contributing to HeyVox

Thanks for your interest in improving HeyVox. This is a macOS-first, local-first
voice layer for AI coding agents; contributions of all sizes are welcome.

## Before you start

- **Platform:** HeyVox targets macOS. Most audio/UI code needs a Mac to run, and
  MLX Whisper needs Apple Silicon. You can edit and run the test suite anywhere,
  but manual verification generally needs a Mac.
- **Python:** 3.12 or newer.
- **Scope:** for anything larger than a bug fix or small enhancement, please open
  an issue first so we can agree on the approach before you invest the time.

## Development setup

```bash
git clone https://github.com/heyvox-dev/heyvox
cd heyvox
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,apple-silicon,chrome]"   # drop apple-silicon on Intel
```

## Tests and linting

Please run both before opening a PR:

```bash
ruff check heyvox/ tests/
pytest tests/ -k "not e2e"
```

CI runs the same lint + tests on macOS (Apple Silicon), plus a defect-guard suite
and security scans (gitleaks, pip-audit). New behavior should come with a test —
prefer fast, hardware-free unit tests (see `tests/` for the style; many guard
tests are net-free and mock external calls).

## Coding conventions

- **Match the surrounding code** — naming, structure, and comment density.
- **No app-specific hardcoding.** HeyVox is a generic voice layer; per-app
  behavior comes from config app-profiles, never from string-matching app names
  in logic branches. See the "No app-specific hardcoding" section in `CLAUDE.md`.
- **MCP/daemon logging goes to stderr** — stdout is reserved for stdio transport
  framing.
- **Keep audio processing local.** Any new outbound network path must be
  opt-in, default-off, and disclosed in `docs/privacy.html`.
- **Comments** explain constraints the code can't show — not what the next line
  does.

## Commits and pull requests

- Base PRs on `main`.
- Use clear, conventional-style commit subjects (`fix(stt): ...`,
  `feat(hud): ...`, `docs: ...`); keep commits atomic (one logical change each).
- Explain the "why" in the PR description, and note how you verified the change.
- Ensure lint + tests pass.

## A note on the defect log

Maintainers track every bug fix in an internal defect log that feeds periodic
testing/CI improvements. You don't need to write defect-log entries in your PR —
just describe the bug and the fix clearly, and a maintainer will fold it in.

## Reporting bugs and security issues

- **Bugs / features:** open a GitHub issue with repro steps, `heyvox --version`,
  and your macOS version + chip.
- **Security vulnerabilities:** please do *not* open a public issue — see
  [SECURITY.md](SECURITY.md) for private reporting.

## License

By contributing, you agree that your contributions are licensed under the
project's [MIT License](LICENSE).
