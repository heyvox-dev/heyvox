---
phase: 15
name: paste-target-lock
status: passed
verified: 2026-04-24
goal: "Transcribed speech lands in the exact text field that held the cursor at recording start, even after app/workspace/session change; unreachable target → fail-closed (clipboard + history + toast)"
---

## Goal Achievement

**PASSED.** The three-tier resolver + unconditional yank-back + post-paste
verification + fail-closed fallback collectively deliver the phase goal.
Every requirement R1-R8 has landed artifact(s) and passing tests.

## Requirement Traceability

| R  | Requirement | Plans | Artifacts | Tests |
|----|-------------|-------|-----------|-------|
| R1 | Immutable record-start lock | 15-02 | `TargetLock` frozen dataclass | `test_target_lock_is_frozen` + 17 more |
| R2 | Stable identity fields | 15-01, 15-02 | `app_bundle_id`, `window_number`, `ax_role_path`, leaf tie-breakers, `ConductorIdentity` | `test_capture_lock_populates_stable_fields` |
| R3 | Sqlite coupling isolated to one file | 15-01 | `heyvox/adapters/conductor.py` (sole sqlite3 import in adapters); 100ms budget | `test_under_100ms_p95` (measured 0.17ms); `test_locked_db_returns_none_without_raising` (W9) |
| R4 | Three-tier ladder | 15-05 | `resolve_lock` → tier-1 role-path walk, tier-2 profile shortcut, tier-3 fail-closed | `test_tier1_succeeds_when_role_path_walks_cleanly`, `test_tier2_fires_with_focus_shortcut`, `test_tier1_plus_tier2_fail_returns_target_unreachable` |
| R5 | Fail-closed preserves history, writes clipboard, no Cmd+V, error cue, toast | 15-04, 15-05 | `show_failure_toast`, `_send_local` fail-closed branch, `FailReason` taxonomy | `test_focused_was_text_field_false_fails_closed`, `test_no_focus_shortcut_gives_multi_field_no_shortcut`, `test_heyvox_history_save_is_the_patch_target` (W5) |
| R6 | Unconditional yank-back app + workspace + session | 15-05 | `_yank_back_app_and_workspace` via NSRunningApplication bundle-id + `conductor-switch-workspace --id [--session] --force` | `test_yank_back_uses_id_flag`, `test_session_id_triggers_session_flag` (B4), `test_no_session_id_omits_session_flag` |
| R7 | Post-paste AXValue verification + single retry | 15-06 | `verify_paste` + `_acquire_focused_element` (W3 Tier-2 parity) + W11 clipboard re-set | 16 `test_verify_paste.py` tests covering all paths |
| R8 | `conductor_paste_and_send` generalized | 15-03, 15-05 | `app_fast_paste(profile, text)` replaces conductor-specific fn; 15-05 wires it as tier-2 paste executor | `test_profile_with_focus_shortcut_builds_correct_order` (13 tests) + integration via `grep "app_fast_paste(profile" heyvox/recording.py` returns 1 match |

**All 8 requirements verified.**

## Must-Haves Check (truths from each plan)

### 15-01 — Conductor adapter

| Truth | Evidence |
|---|---|
| Sole sqlite coupling in `heyvox/adapters/conductor.py` | `grep -rn "sqlite" heyvox/adapters/` returns only `conductor.py` |
| 100ms budget, None on error | `test_under_100ms_p95` (p95=0.17ms), `test_locked_db_returns_none_without_raising` |
| W9 locked-DB regression guard | Explicit test passes with PRAGMA journal_mode=DELETE |

### 15-02 — TargetLock

| Truth | Evidence |
|---|---|
| TargetLock frozen | `test_target_lock_is_frozen` raises FrozenInstanceError |
| capture_lock captures all required fields | `test_capture_lock_populates_stable_fields` |
| TargetSnapshot deleted | `grep -rn "class\s+TargetSnapshot" heyvox/` returns 0 |
| `_detect_app_workspace` + `_switch_app_workspace` deleted | grep returns 0 |
| Branch filter used (W-fix) | `test_capture_lock_passes_detected_branch_to_adapter` |
| Phase 12 fast-path alive (B1) | `test_ax_inject_text_phase12_fastpath_remains_under_5ms` (mean <5ms) |
| Executor no-leak (B2) | `test_executor_no_thread_leak_across_30_capture_lock_calls` |

### 15-03 — app_fast_paste

| Truth | Evidence |
|---|---|
| AppProfileConfig has 3 new fields with correct defaults | `TestAppProfileNewFields` 9 tests |
| Conductor has has_session_detection=True, ax_settle=0.15 | Verified |
| Terminal/iTerm2 supports_ax_verify=False | Verified |
| conductor_paste_and_send DELETED | `grep -n "conductor_paste_and_send" heyvox/` returns 0 |
| app_fast_paste driven entirely from profile | `test_fictional_app_with_cmd_k_works_identically` |
| Zero hardcoded "conductor" in heyvox/input/ | `grep -niE '"conductor"' heyvox/input/` returns 0 |

### 15-04 — toast helper

| Truth | Evidence |
|---|---|
| HS up → hs.alert.show; HS down → osascript fallback | 12 tests in `test_toast.py` |
| DEF-074 liveness gate | `test_hammerspoon_running_uses_pgrep_q_hammerspoon` |
| JSON-quoted message | `test_hs_message_is_json_quoted` |
| Silent subprocess failures | `test_hs_popen_oserror_falls_through_to_osascript`, `test_both_paths_oserror_silent` |
| No heyvox.herald imports | `grep -n "from heyvox.herald" heyvox/input/toast.py` returns 0 |
| 98 LOC, 3 top-level defs | Within 40-130 bound |

### 15-05 — resolve_lock

| Truth | Evidence |
|---|---|
| PasteOutcome Ok(element) vs FailClosed(reason) | `test_paste_outcome_is_frozen` + tier tests |
| Tier 1 role-path walk | `test_tier1_succeeds_when_role_path_walks_cleanly` |
| Tier 2 profile shortcut | `test_tier2_fires_with_focus_shortcut` |
| Tier 3 fail-closed writes history + clipboard, skips Cmd+V, plays error cue, fires toast | Implementation in `_send_local`, history unchanged at recording.py:639-640 |
| Unconditional yank-back (R6) | `test_session_id_triggers_session_flag` (unconditional append) |
| DEF-070 guard preserved | No edits to `heyvox/herald/orchestrator.py`; comment in recording.py |
| conductor-switch-workspace does NOT check RECORDING_FLAG | `grep -n RECORDING_FLAG ~/.local/bin/conductor-switch-workspace` returns 0 |
| conductor-switch-workspace supports `--session` | Script extended, `bash --help` shows session flag |
| --id used (not positional) | `test_yank_back_uses_id_flag` |
| W13: all reasons format with {app_name} | `test_all_fail_reason_messages_format_with_app_name` |
| outcome=None explicit init (W6) | `grep "outcome = None" heyvox/recording.py` returns 1 |
| W8: No hardcoded focus_app import | `grep "from heyvox.input.injection import focus_app" heyvox/input/target.py` returns 0 |
| subprocess module-level (Fact 5) | `grep "^import subprocess" heyvox/input/target.py` returns 1 |
| app_fast_paste wired (R8) | `grep "app_fast_paste(profile" heyvox/recording.py` returns 1 |

### 15-06 — verify_paste

| Truth | Evidence |
|---|---|
| Normalized substring match, case preserved (D-05) | `test_normalize_preserves_case` + `test_verify_succeeds_first_try_when_ax_value_contains_transcript` |
| Single retry with W11 clipboard re-set | `test_verify_drift_after_retry_resets_clipboard` |
| Non-AX focus-unchanged fallback | `test_non_ax_profile_focus_unchanged_verifies` |
| W3 Tier-2 re-acquire | `test_tier2_acquires_focused_element_when_element_none` |
| W3 graceful degradation | `test_tier2_acquire_fail_falls_back_to_focus_unchanged` |
| W7 profile=None log | `test_verify_with_profile_none_emits_explicit_log` |
| W12 defensive gate in caller | `test_recording_send_local_has_defensive_outcome_guard` |
| 4 [PASTE] log variants | `test_verified_true_first_try_log`, `test_drift_log_format` |

### 15-07 — log-health paste section

| Truth | Evidence |
|---|---|
| `## Paste` section renders (human + JSON) | `test_paste_section_renders_with_data`, `test_paste_json_includes_all_keys` |
| B6 canonical keys `tier_1_p95_ms` | `grep '"tier_1_elapsed_p95_ms"' heyvox/cli.py` returns 0 |
| All 3 FailReason keys in breakdown | `test_empty_log_json_has_zero_or_none_values` checks zeros |
| Empty log graceful | `test_empty_log_renders_gracefully` |
| Existing sections unchanged | `test_existing_sections_still_present` |
| W8 patch targets match live (Fact 3) | `test_constants_patch_intercepts` |

## Regression Audit

| State | Failure Count | Delta |
|---|---|---|
| Pre-15 baseline (stash) | 25 | — |
| Post-15 final | 25 | **Net zero new regressions** |

Changes: 2 TestAXFastPath tests FIXED by 15-02 migration; 2
TestFocusShortcutInOsascript tests were previously hidden by
`--ignore=tests/test_target_restore.py` in the diff (pre-existing
mock `.decode()` bug, unrelated to Phase 15).

Phase 15 added **74 new tests** (all passing):
- 10 (conductor adapter)
- 9 (config app profile new fields)
- 13 (app_fast_paste)
- 12 (toast)
- 17 (target_lock)
- 12 (resolve_lock)
- 16 (verify_paste)
- 9 (log_health_paste)

## End-to-End Observability

`heyvox log-health --json` now exposes the live `paste` metrics. Smoke-run
confirmed valid JSON with canonical B6 keys. Future regressions will
surface in fail_closed_rate_pct, verify_drift_rate_pct, and p95 latency.

## Files Touched (net)

| File | Plan | Change |
|---|---|---|
| `heyvox/adapters/conductor.py` | 15-01 | Created |
| `heyvox/adapters/base.py` | 15-02 | Docstring updated |
| `heyvox/config.py` | 15-03 | 3 new fields + 3 profile overrides |
| `heyvox/input/injection.py` | 15-02, 15-03 | `_ax_inject_text` migrated; `conductor_paste_and_send` deleted; `app_fast_paste` added |
| `heyvox/input/target.py` | 15-02, 15-05, 15-06 | TargetSnapshot→TargetLock; resolve_lock + PasteOutcome + FailReason; verify_paste + VerifyResult |
| `heyvox/input/toast.py` | 15-04 | Created |
| `heyvox/recording.py` | 15-02, 15-05, 15-06 | capture_lock + resolve_lock + fail-closed branch + verify_paste + drift toast |
| `heyvox/app_context.py` | 15-02 | Annotation updated |
| `heyvox/cli.py` | 15-07 | `## Paste` section + JSON payload extension |
| `~/.local/bin/conductor-switch-workspace` | 15-05 Task 0 | Extended with `--id` and `--session` flags (idempotent sqlite UPDATE) |

Plus 8 new test files totaling 74 tests.

## Self-Check: PASSED

All 8 requirements delivered. Net-zero regressions. 74 new tests pass.
Live smoke of log-health Paste section valid. All REVISION-ADDENDUM
blockers + warnings (B1-B6, W3, W5-W13, Facts 1-6) applied.
