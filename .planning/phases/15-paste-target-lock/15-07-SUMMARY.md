---
phase: 15
plan: 07
title: heyvox log-health Paste section
status: complete
completed: 2026-04-24
requirements: [R5, R7]
addendum_applied: [B6, W8-Fact3]
---

## What Shipped

New `## Paste` section in `heyvox log-health` surfaces SPEC acceptance
criterion 11 observability: fail-closed rate + per-reason breakdown +
verify-drift rate + tier 1/2 hit rates + p95 latency. Both human-readable
and `--json` output extended. Purely additive — zero regression to existing
Wake/STT/Herald/Workspace sections.

## Human-Readable Output (Format)

```
## Paste (current rotation of heyvox.log)
  Total resolves:        12
  Tier 1 hit rate:       83.3%   (10/12 non-fail)
  Tier 2 hit rate:       16.7%   (2/12 non-fail)
  Fail-closed rate:      7.7%    (1/13 total)
    by reason:
      no_text_field_at_start: 1
  Verify-drift rate:     16.7%   (1/6 verifies)
  Verify retried (1/N):  2/6
  Tier 1 elapsed p95:    55ms
  Tier 2 elapsed p95:    187ms
```

On empty log: `  (no [PASTE] events in current rotation)`.

## JSON Payload (all 14 keys always present)

```json
"paste": {
  "total_resolves": 4,
  "tier_1_hit_count": 2,
  "tier_2_hit_count": 1,
  "fail_closed_count": 1,
  "tier_1_hit_rate_pct": 66.67,
  "tier_2_hit_rate_pct": 33.33,
  "fail_closed_rate_pct": 25.0,
  "fail_closed_by_reason": {
    "no_text_field_at_start": 0,
    "multi_field_no_shortcut": 1,
    "target_unreachable": 0
  },
  "verify_total": 4,
  "verify_drift_count": 1,
  "verify_drift_rate_pct": 25.0,
  "verify_retried_count": 2,
  "tier_1_p95_ms": 55,
  "tier_2_p95_ms": 187
}
```

## B6 Canonical Key Names

Keys are `tier_1_p95_ms` and `tier_2_p95_ms` — no `_elapsed_` infix.
Tests assert this deterministically (no `is None or` hedging) AND verify
the old verbose names (`tier_1_elapsed_p95_ms`, `tier_2_elapsed_p95_ms`)
are absent from the payload.

## W8 Patch-Target Evidence (Fact 3)

Verified before test design:

```
$ python3 -c "import heyvox.constants as c; print(hasattr(c, 'LOG_FILE'))"
True
$ python3 -c "import heyvox.config as cfg; print(hasattr(cfg, 'load_config'))"
True
$ python3 -c "import heyvox.cli as cli; print(hasattr(cli, 'LOG_FILE'))"
False
$ python3 -c "import heyvox.cli as cli; print(hasattr(cli, 'load_config'))"
False
```

Tests patch `heyvox.constants.*` + `heyvox.config.load_config` — patching
`heyvox.cli.*` would raise AttributeError because those are function-local
re-imports inside `_cmd_log_health` (cli.py:425-436).

The sanity-guard test `test_constants_patch_intercepts` asserts this
exact attribute shape; if anyone later moves the imports to module level,
the guard still passes but we should revisit the patch strategy
(attributes would then be bound on both modules).

## Baseline Observed on Live Log

Live smoke test against `/tmp/heyvox.log`:
```
## Paste (current rotation of heyvox.log)
  (no [PASTE] events in current rotation)
```

No PASTE events yet (Phase 15 just landed; first use will populate the
metrics). Framework is live and ready. `--json` output parses cleanly.

## Tests (9 new, all pass in 0.15s)

Located in `tests/test_log_health_paste.py`:
1. `test_constants_patch_intercepts` — W8 sanity guard
2. `test_paste_section_renders_with_data` — human output has expected headings
3. `test_paste_json_includes_all_keys` — 14 keys + B6 canonical + p95 values
4. `test_paste_tier_counts_and_rates` — arithmetic verification
5. `test_paste_verify_counts` — drift and retry arithmetic
6. `test_empty_log_renders_gracefully` — "(no [PASTE] events)" message
7. `test_empty_log_json_has_zero_or_none_values` — empty-state JSON shape
8. `test_only_fail_closed_lines` — all-fail edge case
9. `test_existing_sections_still_present` — regression guard (Wake/Herald/Workspace)

## Acceptance Criteria — all met

- [x] `## Paste (current rotation` heading renders
- [x] Tier 1/2 hit rates + fail-closed rate + drift rate + p95 all shown
- [x] `"paste":` JSON key present with all 14 expected sub-keys
- [x] B6 canonical `tier_1_p95_ms` + `tier_2_p95_ms` (no `_elapsed_`)
- [x] All three FailReason keys in `fail_closed_by_reason` (always present)
- [x] Empty log handled without crash
- [x] No regression to existing sections (smoke + test coverage)
- [x] W8 patch strategy matches live code's import shape

## For the Phase 15 Verifier

`heyvox log-health --json` is now the authoritative end-to-end observability
for SPEC R5 (fail-closed) and R7 (drift). The verifier can poll this output
in a real paste session to confirm:
- R5: fail_closed_count > 0 when paste fails closed; the appropriate reason
  increments in `fail_closed_by_reason`
- R7: verify_drift_count and verify_total track the 4 `[PASTE] verified=`
  variants; drift_rate_pct reflects mis-pastes
- R4: tier_1 + tier_2 hit counts ≈ successful resolves (fail_closed is R5 only)

## Self-Check: PASSED

SPEC AC 11 satisfied. B6 + W8(Fact 3) corrections applied. Framework live
and ready for real-world metrics as PASTE events accumulate.
