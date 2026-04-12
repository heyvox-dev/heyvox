---
phase: 11-tech-debt-cleanup
verified: 2026-04-12T10:30:00Z
status: passed
score: 6/6 must-haves verified
gaps: []
resolution_note: "Gaps fixed inline by orchestrator — tts_playing:False added to cleanup block, MCP server updated to state-first ordering. Commit e77fbde."
---

# Phase 11: Tech Debt Cleanup Verification Report

**Phase Goal:** Single source of truth for all shared state — no shim vars, no dual-write ambiguity, no deprecation warnings
**Verified:** 2026-04-12T10:30:00Z
**Status:** gaps_found
**Re-verification:** No — initial verification

## Goal Achievement

### Observable Truths

| # | Truth | Status | Evidence |
|---|-------|--------|----------|
| 1 | No module-level shim variables exist in main.py | VERIFIED | grep "shim\|backward.compat\|is_recording = False\|def start_recording\|def stop_recording" returns no matches |
| 2 | test_flag_coordination.py tests pass using AppContext and RecordingStateMachine directly | VERIFIED | 10/10 tests pass; zero references to "from heyvox import main as m" |
| 3 | No backward-compat wrapper functions in main.py | VERIFIED | grep "def start_recording\|def stop_recording" returns no matches |
| 4 | Starting HeyVox produces zero deprecation warnings from websockets | VERIFIED | python3 -W error -c "import heyvox.chrome.bridge" exits 0 with "import ok" |
| 5 | Herald orchestrator writes tts_playing=true to atomic state file when playing audio | PARTIAL | Lines 569 and 604 write True/False correctly; cleanup block at line 688 omits tts_playing:False — stuck-True risk on crash |
| 6 | Echo suppression reads tts_playing from atomic state as primary source | PARTIAL | main.py echo suppression is correct. MCP server speaking detection does NOT check tts_playing from state (flag files remain primary in server.py). Python TTS path (tts.py) does not write tts_playing at all (ROADMAP criterion 2). |

**Score:** 4/6 truths verified

### Required Artifacts

| Artifact | Expected | Status | Details |
|----------|----------|--------|---------|
| `tests/test_flag_coordination.py` | Flag coordination tests using AppContext API | VERIFIED | Contains AppContext and RecordingStateMachine imports; zero main.py shim refs |
| `heyvox/main.py` | Clean main module without shim vars | VERIFIED | No shim vars, no wrapper functions, threading import removed |
| `heyvox/chrome/bridge.py` | WebSocket bridge using websockets.asyncio.server API | VERIFIED | Line 63: "from websockets.asyncio.server import serve as _ws_serve" |
| `heyvox/herald/orchestrator.py` | Orchestrator with tts_playing dual-write to atomic state | PARTIAL | Lines 569/604 write True/False; cleanup block at line 688 missing tts_playing:False |
| `heyvox/mcp/server.py` | MCP status using atomic state as primary for tts_playing | FAILED | Lines 103-106 check flag files first; no _ipc_state.get("tts_playing") check; comment explicitly labels flag files "Primary" |

### Key Link Verification

| From | To | Via | Status | Details |
|------|----|-----|--------|---------|
| tests/test_flag_coordination.py | heyvox/app_context.py | direct import | VERIFIED | "from heyvox.app_context import AppContext" found at lines 20, 48, 155 |
| tests/test_flag_coordination.py | heyvox/recording.py | RecordingStateMachine | VERIFIED | RecordingStateMachine imported at lines 21, 49, 156 |
| heyvox/herald/orchestrator.py | heyvox/ipc/state.py | update_state with tts_playing | PARTIAL | Pattern "update_state.*tts_playing" found at lines 569 and 604; cleanup block at line 688 uses update_state WITHOUT tts_playing |
| heyvox/main.py | heyvox/ipc/state.py | read_state for echo suppression | VERIFIED | Line 742: "from heyvox.ipc.state import read_state as _read_ipc_state"; line 743 checks tts_playing |

### Data-Flow Trace (Level 4)

| Artifact | Data Variable | Source | Produces Real Data | Status |
|----------|---------------|--------|--------------------|--------|
| heyvox/mcp/server.py (voice_status) | speaking | _ipc_state + flag files | tts_playing not checked in state | HOLLOW — atomic state queried but tts_playing field ignored; flag files remain primary |
| heyvox/main.py (echo suppression) | _tts_active | read_state() then flag fallback | Yes — reads JSON state then flag files | FLOWING |
| heyvox/herald/orchestrator.py | tts_playing | update_state on play start/stop | Partial — missing from cleanup path | PARTIAL |

### Behavioral Spot-Checks

| Behavior | Command | Result | Status |
|----------|---------|--------|--------|
| test_flag_coordination.py passes | python -m pytest tests/test_flag_coordination.py -v | 10 passed, 3 warnings | PASS |
| bridge.py imports without deprecation warning | python3 -W error -c "import heyvox.chrome.bridge" | "import ok" | PASS |
| Full test suite (no regressions) | python -m pytest tests/ -x -q | 383 passed, 2 skipped, 6 deselected, 3 warnings | PASS |
| tts_playing True write in orchestrator | grep "tts_playing.*True" heyvox/herald/orchestrator.py | Line 569 matched | PASS |
| tts_playing False write in orchestrator cleanup | grep "tts_playing" on line 688 | Not found — only herald_playing_pid:None | FAIL |

### Requirements Coverage

| Requirement | Source Plan | Description | Status | Evidence |
|-------------|------------|-------------|--------|----------|
| DEBT-01 | 11-01-PLAN.md | 7 backward-compat shim vars removed from main.py | SATISFIED | grep shim/is_recording/start_recording all return no matches; 10 tests pass using AppContext directly |
| DEBT-02 | 11-02-PLAN.md | tts_playing dual-write completed (Python + bash scripts migrated to atomic state) | PARTIAL | Herald orchestrator dual-write exists for normal path; cleanup block incomplete; Python TTS path (tts.py) not updated; MCP server does not use tts_playing from state as primary |
| DEBT-03 | 11-02-PLAN.md | websockets deprecation warning fixed (asyncio.server import) | SATISFIED | websockets.asyncio.server.serve used; python3 -W error import succeeds clean |

**REQUIREMENTS.md status:** File contains unresolved git merge conflict markers (lines 78-86). The traceability table has two conflicting versions of Phase 11 status — one showing DEBT-01 Pending / DEBT-02,03 Complete and another showing the opposite. This must be resolved.

**Orphaned requirements:** No requirements mapped to Phase 11 in REQUIREMENTS.md are missing from the plans. DEBT-01, DEBT-02, DEBT-03 all appear in plan frontmatter.

### Anti-Patterns Found

| File | Line | Pattern | Severity | Impact |
|------|------|---------|----------|--------|
| .planning/REQUIREMENTS.md | 78-86 | Unresolved git merge conflict markers (<<<<<<< / ======= / >>>>>>>) | Warning | Traceability table for Phase 11 is ambiguous — two conflicting states exist in the file simultaneously |
| heyvox/herald/orchestrator.py | 688 | update_state missing tts_playing:False in cleanup block | Blocker | On orchestrator crash/abnormal exit after playback starts, tts_playing remains True in state file permanently — echo suppression will suppress all wake words until HeyVox restarts |
| heyvox/mcp/server.py | 103-106 | speaking detection ignores _ipc_state["tts_playing"]; flag files remain "Primary" | Warning | voice_status() will not report speaking:true during Herald-managed playback unless TTS_PLAYING_FLAG file also exists — dual-write incomplete from the read side |

### Human Verification Required

None — all checks were automatable for this phase.

### Gaps Summary

Two gaps block full goal achievement for Phase 11:

**Gap 1 — Orchestrator cleanup block incomplete (DEBT-02, partial):**
The Herald orchestrator correctly dual-writes `tts_playing: True` when playback starts and `tts_playing: False` when playback ends normally (lines 569 and 604). However, the cleanup/finally block that runs on abnormal exit (line 688) only resets `herald_playing_pid` and does not include `tts_playing: False`. If the orchestrator process is killed or crashes after setting `tts_playing: True`, the atomic state file will permanently show TTS as active until a HeyVox restart, causing echo suppression to block all wake word detection. Fix: add `tts_playing: False` to the `update_state` call at line 688.

**Gap 2 — MCP server and Python TTS path not updated (DEBT-02, failed):**
The 11-02-PLAN acceptance criteria required `_ipc_state.get("tts_playing")` to be the first condition in `mcp/server.py`'s speaking detection. The actual code still lists flag files first (comment at line 101 explicitly labels them "Primary"). The `tts_playing` field from state is never checked. Additionally, the ROADMAP success criterion 2 requires the dual-write to be complete in "both the Python TTS path and Herald orchestrator" — `heyvox/audio/tts.py` does not write `tts_playing` to the atomic state at all. These two items were not implemented, leaving DEBT-02 partially satisfied rather than complete.

---

_Verified: 2026-04-12T10:30:00Z_
_Verifier: Claude (gsd-verifier)_
