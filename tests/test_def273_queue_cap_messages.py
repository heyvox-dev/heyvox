"""DEF-273: the Herald queue cap counts messages, not sentence parts.

The cap used to compare the number of WAV files (one per sentence part) with
``max_queued=10`` and drop from the oldest prefix, which was usually the message
being played. Every TTS longer than ten parts was cut after its first sentence
(2026-09-17 to 2026-10-13, e.g. 18 of 19 parts dropped at 10:59:56 on 10-13).
"""

from pathlib import Path

from heyvox.herald.orchestrator import OrchestratorConfig, _enforce_queue_cap


def _cfg(tmp_path: Path, max_queued: int = 10) -> OrchestratorConfig:
    queue = tmp_path / "herald-queue"
    queue.mkdir()
    return OrchestratorConfig(
        queue_dir=queue, debug_log=tmp_path / "herald-debug.log", max_queued=max_queued,
    )


def _queue(cfg: OrchestratorConfig, prefix: str, parts: range) -> None:
    for n in parts:
        wav = cfg.queue_dir / f"{prefix}-{n:02d}.wav"
        wav.write_bytes(b"RIFF")
        wav.with_suffix(".workspace").write_text("ws")


def _prefixes(cfg: OrchestratorConfig) -> list[str]:
    return sorted({p.name.split("-")[0] for p in cfg.queue_dir.glob("*.wav")})


def test_long_single_message_is_not_truncated(tmp_path):
    """One 19-part message (part 01 already played) keeps all remaining parts."""
    cfg = _cfg(tmp_path)
    _queue(cfg, "1791881977260", range(2, 21))
    dropped = _enforce_queue_cap(cfg, cfg.debug_log, active_prefix="1791881977260")
    assert dropped == 0
    assert len(list(cfg.queue_dir.glob("*.wav"))) == 19


def test_long_message_not_yet_started_is_not_truncated(tmp_path):
    """A single long message fits the cap even before it starts playing."""
    cfg = _cfg(tmp_path)
    _queue(cfg, "1000", range(1, 21))
    assert _enforce_queue_cap(cfg, cfg.debug_log) == 0


def test_cap_drops_oldest_waiting_messages_by_count(tmp_path):
    """12 two-part messages with cap 10: the two oldest messages go, whole."""
    cfg = _cfg(tmp_path)
    prefixes = [str(1000 + i) for i in range(12)]
    for p in prefixes:
        _queue(cfg, p, range(1, 3))
    dropped = _enforce_queue_cap(cfg, cfg.debug_log)
    assert dropped == 4
    assert _prefixes(cfg) == prefixes[2:]
    assert not list(cfg.queue_dir.glob(f"{prefixes[0]}-*.workspace"))


def test_cap_never_drops_the_message_in_flight(tmp_path):
    """The playing message is the oldest prefix; the next-oldest waiting ones go instead."""
    cfg = _cfg(tmp_path, max_queued=3)
    prefixes = [str(1000 + i) for i in range(5)]
    _queue(cfg, prefixes[0], range(5, 15))  # in flight, 10 parts left
    for p in prefixes[1:]:
        _queue(cfg, p, range(1, 3))
    _enforce_queue_cap(cfg, cfg.debug_log, active_prefix=prefixes[0])
    assert _prefixes(cfg) == [prefixes[0], prefixes[3], prefixes[4]]
    assert len(list(cfg.queue_dir.glob(f"{prefixes[0]}-*.wav"))) == 10
