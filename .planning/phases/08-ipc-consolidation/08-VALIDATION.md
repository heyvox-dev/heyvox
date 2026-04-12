---
phase: 8
slug: ipc-consolidation
status: draft
nyquist_compliant: false
wave_0_complete: false
created: 2026-04-11
---

# Phase 8 — Validation Strategy

> Per-phase validation contract for feedback sampling during execution.

---

## Test Infrastructure

| Property | Value |
|----------|-------|
| **Framework** | pytest 7.x |
| **Config file** | pyproject.toml |
| **Quick run command** | `python -m pytest tests/ -x -q --tb=short` |
| **Full suite command** | `python -m pytest tests/ -q --tb=short` |
| **Estimated runtime** | ~5 seconds |

---

## Sampling Rate

- **After every task commit:** Run `python -m pytest tests/ -x -q --tb=short`
- **After every plan wave:** Run `python -m pytest tests/ -q --tb=short`
- **Before `/gsd:verify-work`:** Full suite must be green
- **Max feedback latency:** 10 seconds

---

## Per-Task Verification Map

| Task ID | Plan | Wave | Requirement | Test Type | Automated Command | File Exists | Status |
|---------|------|------|-------------|-----------|-------------------|-------------|--------|
| 08-01-01 | 01 | 1 | IPC-01 | grep | `grep -r "/tmp/" heyvox/ --include="*.py" \| grep -v "constants.py" \| grep -v "import"` | ✅ | ⬜ pending |
| 08-02-01 | 02 | 2 | IPC-02 | unit | `python -m pytest tests/test_ipc_state.py -x -q` | ❌ W0 | ⬜ pending |
| 08-03-01 | 03 | 2 | IPC-03 | unit | `python -m pytest tests/test_queue_gc.py -x -q` | ❌ W0 | ⬜ pending |

*Status: ⬜ pending · ✅ green · ❌ red · ⚠️ flaky*

---

## Wave 0 Requirements

- [ ] `tests/test_ipc_state.py` — stubs for IPC-02 atomic state read/write
- [ ] `tests/test_queue_gc.py` — stubs for IPC-03 queue cleanup

*Existing test infrastructure (pytest, conftest) already in place from Phase 7.*

---

## Manual-Only Verifications

| Behavior | Requirement | Why Manual | Test Instructions |
|----------|-------------|------------|-------------------|
| Multi-process state coordination | IPC-02 | Requires HeyVox + Herald running concurrently | Start HeyVox, trigger TTS, verify state.json reflects recording/speaking transitions |

---

## Validation Sign-Off

- [ ] All tasks have `<automated>` verify or Wave 0 dependencies
- [ ] Sampling continuity: no 3 consecutive tasks without automated verify
- [ ] Wave 0 covers all MISSING references
- [ ] No watch-mode flags
- [ ] Feedback latency < 10s
- [ ] `nyquist_compliant: true` set in frontmatter

**Approval:** pending
