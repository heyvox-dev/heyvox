"""Unit tests for heyvox log-health Paste section (Plan 15-07).

W8 (corrected per Fact 3): tests patch `heyvox.constants.*` and
`heyvox.config.load_config` — the SOURCE modules. Patches against
`heyvox.cli.*` would raise AttributeError because those four log-file
constants and `load_config` are function-local re-imports inside
`_cmd_log_health` (cli.py lines 425-436), not module attributes.

B6: canonical JSON key names are `tier_1_p95_ms` and `tier_2_p95_ms`
(no `_elapsed_` infix). Tests assert this deterministically — no
`is None or` hedging — and verify the old verbose names are absent.
"""

import json
from argparse import Namespace
from unittest.mock import MagicMock, patch

import pytest


FIXTURE_LINES = """[10:00:00] [PASTE] tier_used=1 reason=n/a elapsed_ms=42
[10:00:05] [PASTE] verified=true retried=false drift=false (ax_value_len=20)
[10:00:10] [PASTE] tier_used=1 reason=n/a elapsed_ms=55
[10:00:15] [PASTE] verified=true retried=true drift=false (retry-ax_value_len=20)
[10:00:20] [PASTE] tier_used=2 reason=n/a elapsed_ms=187
[10:00:25] [PASTE] verified=true retried=false drift=false (ax_value_len=15)
[10:00:30] [PASTE] tier_used=fail_closed reason=multi_field_no_shortcut elapsed_ms=33
[10:00:35] [PASTE] verified=false retried=true drift=true (drift first_len=0 second_len=0)
"""


def test_constants_patch_intercepts():
    """W8 sanity guard: confirms the patch targets actually intercept the
    function-local re-imports inside _cmd_log_health.

    Per Fact 3, _cmd_log_health re-imports LOG_FILE from heyvox.constants and
    load_config from heyvox.config on EVERY call. Patching `heyvox.cli.LOG_FILE`
    raises AttributeError — that name is never bound at module level.
    """
    import heyvox.constants
    import heyvox.config
    import heyvox.cli

    assert hasattr(heyvox.constants, "LOG_FILE"), (
        "Fact 3 violated: LOG_FILE missing from heyvox.constants"
    )
    assert hasattr(heyvox.config, "load_config"), (
        "Fact 3 violated: load_config missing from heyvox.config"
    )

    with patch("heyvox.constants.LOG_FILE", "/tmp/fake-test-log"):
        from heyvox.constants import LOG_FILE

        assert LOG_FILE == "/tmp/fake-test-log", (
            f"W8 patch target broken: got {LOG_FILE!r}"
        )

    assert not hasattr(heyvox.cli, "LOG_FILE"), (
        "heyvox.cli.LOG_FILE became a module attribute — Fact 3 outdated"
    )
    assert not hasattr(heyvox.cli, "load_config"), (
        "heyvox.cli.load_config became a module attribute — Fact 3 outdated"
    )


def _setup_logfile(tmp_path, content, name="heyvox.log"):
    f = tmp_path / name
    f.write_text(content)
    return str(f)


def _run_log_health(main_log, tmp_path, json_mode=False, capsys=None):
    """Invoke _cmd_log_health with all four constants patched on
    `heyvox.constants` AND load_config patched on `heyvox.config` (W8)."""
    from heyvox.cli import _cmd_log_health

    stt_log = _setup_logfile(tmp_path, "", name="heyvox-stt-debug.log")
    herald_log = _setup_logfile(tmp_path, "", name="herald-debug.log")
    herald_viol = _setup_logfile(tmp_path, "", name="herald-violations.log")
    args = Namespace(date=None, json=json_mode)

    fake_config = MagicMock()
    fake_config.log_file = main_log

    with patch("heyvox.constants.LOG_FILE", main_log), \
         patch("heyvox.constants.STT_DEBUG_LOG", stt_log), \
         patch("heyvox.constants.HERALD_DEBUG_LOG", herald_log), \
         patch("heyvox.constants.HERALD_VIOLATIONS_LOG", herald_viol), \
         patch("heyvox.config.load_config", return_value=fake_config):
        _cmd_log_health(args)
    return capsys.readouterr() if capsys else None


def test_paste_section_renders_with_data(tmp_path, capsys):
    p = _setup_logfile(tmp_path, FIXTURE_LINES)
    out = _run_log_health(p, tmp_path, json_mode=False, capsys=capsys)

    assert "Paste (current rotation" in out.out
    assert "Tier 1 hit rate:" in out.out
    assert "Tier 2 hit rate:" in out.out
    assert "Fail-closed rate:" in out.out
    assert "multi_field_no_shortcut" in out.out
    assert "Verify-drift rate:" in out.out


def test_paste_json_includes_all_keys(tmp_path, capsys):
    p = _setup_logfile(tmp_path, FIXTURE_LINES)
    out = _run_log_health(p, tmp_path, json_mode=True, capsys=capsys)
    payload = json.loads(out.out)
    paste = payload["paste"]

    expected_keys = (
        "total_resolves",
        "tier_1_hit_count",
        "tier_2_hit_count",
        "fail_closed_count",
        "tier_1_hit_rate_pct",
        "tier_2_hit_rate_pct",
        "fail_closed_rate_pct",
        "fail_closed_by_reason",
        "verify_total",
        "verify_drift_count",
        "verify_drift_rate_pct",
        "verify_retried_count",
        "tier_1_p95_ms",
        "tier_2_p95_ms",
    )
    for key in expected_keys:
        assert key in paste, f"missing key: {key}"

    # B6: deterministic (no hedging). Fixture has tier_1 = [42, 55] and
    # tier_2 = [187]. p95 = element at int(len*0.95):
    #   tier_1: index int(2*0.95)=1 -> 55
    #   tier_2: index int(1*0.95)=0 -> 187
    assert paste["tier_1_p95_ms"] == 55, (
        f"expected p95=55 from fixture, got {paste['tier_1_p95_ms']}"
    )
    assert paste["tier_2_p95_ms"] == 187, (
        f"expected p95=187 from fixture, got {paste['tier_2_p95_ms']}"
    )
    # B6: old verbose keys must NOT appear
    assert "tier_1_elapsed_p95_ms" not in paste
    assert "tier_2_elapsed_p95_ms" not in paste

    # By-reason breakdown
    assert paste["fail_closed_by_reason"]["multi_field_no_shortcut"] == 1
    assert paste["fail_closed_by_reason"]["no_text_field_at_start"] == 0
    assert paste["fail_closed_by_reason"]["target_unreachable"] == 0


def test_paste_tier_counts_and_rates(tmp_path, capsys):
    p = _setup_logfile(tmp_path, FIXTURE_LINES)
    out = _run_log_health(p, tmp_path, json_mode=True, capsys=capsys)
    payload = json.loads(out.out)
    paste = payload["paste"]

    # 2 tier-1 + 1 tier-2 + 1 fail-closed = 4 total
    assert paste["total_resolves"] == 4
    assert paste["tier_1_hit_count"] == 2
    assert paste["tier_2_hit_count"] == 1
    assert paste["fail_closed_count"] == 1
    # Tier 1 hit rate = 2/3 of non-fail
    assert paste["tier_1_hit_rate_pct"] == pytest.approx(66.67, abs=0.01)
    assert paste["tier_2_hit_rate_pct"] == pytest.approx(33.33, abs=0.01)
    # Fail-closed rate = 1/4 total
    assert paste["fail_closed_rate_pct"] == pytest.approx(25.0, abs=0.01)


def test_paste_verify_counts(tmp_path, capsys):
    p = _setup_logfile(tmp_path, FIXTURE_LINES)
    out = _run_log_health(p, tmp_path, json_mode=True, capsys=capsys)
    payload = json.loads(out.out)
    paste = payload["paste"]

    # Fixture has 4 verified= lines: 3 verified=true + 1 drift=true
    assert paste["verify_total"] == 4
    assert paste["verify_drift_count"] == 1
    assert paste["verify_drift_rate_pct"] == pytest.approx(25.0, abs=0.01)
    # Retried count: one "retried=true" in fixture (retry-ax_value_len=20)
    # plus one "retried=true" in the drift line => 2 retries total
    assert paste["verify_retried_count"] == 2


def test_empty_log_renders_gracefully(tmp_path, capsys):
    p = _setup_logfile(tmp_path, "")
    out = _run_log_health(p, tmp_path, json_mode=False, capsys=capsys)
    assert "no [PASTE] events" in out.out


def test_empty_log_json_has_zero_or_none_values(tmp_path, capsys):
    p = _setup_logfile(tmp_path, "")
    out = _run_log_health(p, tmp_path, json_mode=True, capsys=capsys)
    payload = json.loads(out.out)
    paste = payload["paste"]

    assert paste["total_resolves"] == 0
    assert paste["tier_1_hit_count"] == 0
    assert paste["verify_total"] == 0
    # B6 deterministic: empty log → p95 is None
    assert paste["tier_1_p95_ms"] is None
    assert paste["tier_2_p95_ms"] is None
    # All reason buckets still present with zero counts
    assert paste["fail_closed_by_reason"]["no_text_field_at_start"] == 0
    assert paste["fail_closed_by_reason"]["multi_field_no_shortcut"] == 0
    assert paste["fail_closed_by_reason"]["target_unreachable"] == 0


def test_only_fail_closed_lines(tmp_path, capsys):
    content = "\n".join([
        "[PASTE] tier_used=fail_closed reason=no_text_field_at_start elapsed_ms=12",
        "[PASTE] tier_used=fail_closed reason=target_unreachable elapsed_ms=100",
    ])
    p = _setup_logfile(tmp_path, content + "\n")
    out = _run_log_health(p, tmp_path, json_mode=True, capsys=capsys)
    paste = json.loads(out.out)["paste"]

    assert paste["total_resolves"] == 2
    assert paste["fail_closed_count"] == 2
    assert paste["fail_closed_rate_pct"] == 100.0
    assert paste["fail_closed_by_reason"]["no_text_field_at_start"] == 1
    assert paste["fail_closed_by_reason"]["target_unreachable"] == 1
    # No successful tier => p95 None
    assert paste["tier_1_p95_ms"] is None
    assert paste["tier_2_p95_ms"] is None


def test_existing_sections_still_present(tmp_path, capsys):
    """Paste section is additive — other sections must still appear."""
    p = _setup_logfile(tmp_path, FIXTURE_LINES)
    out = _run_log_health(p, tmp_path, json_mode=False, capsys=capsys)

    assert "## Wake word" in out.out
    assert "## Herald" in out.out or "Herald" in out.out
    assert "## Workspace switch" in out.out
