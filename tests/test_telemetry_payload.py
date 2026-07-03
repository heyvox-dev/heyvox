"""Guard tests: telemetry payload is metrics-only — no transcript/log content.

The product is privacy-first and the opt-in telemetry is documented as sending
only a version, a hashed hostname, and 5 numeric counters. These tests plant
sensitive transcript-like text into the log the counter reader scans and prove
it never reaches the payload — only counts and the allowlisted system fields do.

Net-free: no upload happens; load_config and the telemetry paths are
monkeypatched to a tmp dir.

References: .context/release-audit/03-security.md §6, 00-CONSOLIDATED.md SHOULD 12
"""

import json
import platform
from types import SimpleNamespace

from heyvox.telemetry import events as tel

# Distinctive string that must never appear in an outbound event.
SENSITIVE = "SECRET-transcript hunter2 transfer all funds to account 12345"

ALLOWED_EVENT_KEYS = {"type", "ts", "tag", "delta", "system"}
ALLOWED_SYSTEM_KEYS = {
    "heyvox_version",
    "macos_version",
    "mac_model",
    "python",
    "machine_hash",
}


def _write_fake_log(tmp_path):
    log = tmp_path / "heyvox.log"
    lines = []
    for i in range(3):  # 3 WAKE_VAD_DROP markers, each carrying sensitive text
        lines.append(f"[WAKE_VAD_DROP] transcript={SENSITIVE!r} idx={i}")
    for i in range(2):  # 2 NEAR_MISS markers, ditto
        lines.append(f"[NEAR_MISS] user said: {SENSITIVE} ({i})")
    lines.append(f"plain line with {SENSITIVE} and no tag marker")
    log.write_text("\n".join(lines) + "\n")
    return log


def _setup(monkeypatch, tmp_path):
    log = _write_fake_log(tmp_path)
    monkeypatch.setattr(
        "heyvox.config.load_config",
        lambda: SimpleNamespace(log_file=str(log)),
    )
    # Baseline snapshot = zeros: point at a nonexistent file; keep writes in tmp.
    monkeypatch.setattr(tel, "TELEMETRY_COUNTER_SNAPSHOT", str(tmp_path / "snap.json"))
    monkeypatch.setattr(tel, "TELEMETRY_DIR", str(tmp_path / "teldir"))
    return log


def test_payload_contains_no_log_line_content(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    blob = json.dumps(tel.build_events(commit_snapshot=False))
    assert SENSITIVE not in blob, "log line content leaked into telemetry payload"
    assert "transcript=" not in blob
    assert "user said" not in blob


def test_counter_deltas_are_counts_only(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    events = tel.build_events(commit_snapshot=False)
    deltas = {e["tag"]: e["delta"] for e in events if e["type"] == "counter.delta"}
    assert deltas.get("WAKE_VAD_DROP") == 3
    assert deltas.get("NEAR_MISS") == 2
    # A line without a tag marker contributes nothing.
    assert "USER_EFFORT" not in deltas


def test_every_event_key_is_allowlisted(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    events = tel.build_events(commit_snapshot=False)
    assert events, "expected at least a heartbeat event"
    for e in events:
        assert set(e).issubset(ALLOWED_EVENT_KEYS), f"unexpected top-level key: {e}"
        assert set(e["system"]).issubset(ALLOWED_SYSTEM_KEYS), f"unexpected system key: {e}"
        if e["type"] == "counter.delta":
            assert e["tag"] in tel.TRACKED_TAGS
            assert isinstance(e["delta"], int)


def test_hostname_is_hashed_not_raw(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    events = tel.build_events(commit_snapshot=False)
    blob = json.dumps(events)
    host = platform.node()
    if host and host != "unknown":
        assert host not in blob, "raw hostname leaked into payload; must be hashed"
    machine_hash = events[0]["system"]["machine_hash"]
    assert len(machine_hash) == 16
    assert all(c in "0123456789abcdef" for c in machine_hash)


def test_tracked_tags_are_the_known_five():
    """A new counter tag must be a conscious change reviewed against this guard."""
    assert set(tel.TRACKED_TAGS) == {
        "WAKE_VAD_DROP",
        "NEAR_MISS",
        "USER_EFFORT",
        "MIC_ZOMBIE",
        "KOKORO_RESTART",
    }
