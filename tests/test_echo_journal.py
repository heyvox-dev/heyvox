"""DEF-078: Cross-process TTS echo journal.

Tests the file-backed echo buffer that lets the heyvox daemon's STT filter
see TTS text that Herald's worker process (spawned by Claude Code hooks) has
spoken. Before DEF-078, only the MCP in-process `say()` path populated the
echo buffer, and it was process-local — so speaker-to-mic bleed from Herald-
initiated TTS passed `filter_tts_echo()` unfiltered and got pasted into
whichever app had focus.
"""

from __future__ import annotations

import json
import os
import time

import pytest


@pytest.fixture(autouse=True)
def clear_echo_state():
    """Reset the in-memory deque between tests (journal is isolated per-test
    via the autouse isolate_flags fixture in conftest.py)."""
    import heyvox.audio.echo as echo_mod
    with echo_mod._echo_buffer_lock:
        echo_mod._echo_buffer.clear()
    yield
    with echo_mod._echo_buffer_lock:
        echo_mod._echo_buffer.clear()


def _clear_memory_buffer_only() -> None:
    """Simulate a different process: drop the in-memory buffer so only the
    on-disk journal is visible to filter_tts_echo()."""
    import heyvox.audio.echo as echo_mod
    with echo_mod._echo_buffer_lock:
        echo_mod._echo_buffer.clear()


class TestJournalWriteReadRoundtrip:
    """Basic journal semantics: register_tts_text writes a JSONL line, and
    filter_tts_echo reads it back even when the in-memory buffer is empty."""

    def test_register_creates_journal_file(self):
        from heyvox.audio.echo import register_tts_text
        from heyvox.constants import TTS_ECHO_JOURNAL

        register_tts_text("Please commit the fix now")

        assert os.path.exists(TTS_ECHO_JOURNAL), \
            "register_tts_text must append to the cross-process journal"

    def test_journal_line_is_valid_jsonl(self):
        from heyvox.audio.echo import register_tts_text
        from heyvox.constants import TTS_ECHO_JOURNAL

        register_tts_text("Deploy succeeded")

        with open(TTS_ECHO_JOURNAL) as fh:
            lines = [ln for ln in fh.read().splitlines() if ln.strip()]

        assert len(lines) == 1
        rec = json.loads(lines[0])
        assert rec["text"] == "deploy succeeded"  # normalized lower
        assert isinstance(rec["ts"], (int, float))

    def test_filter_hits_journal_when_memory_empty(self):
        """This is the DEF-078 core invariant: a separate process (simulated
        by clearing the memory deque) must still be able to strip echo that
        another process registered."""
        from heyvox.audio.echo import register_tts_text, filter_tts_echo

        register_tts_text("The weather today is sunny and warm")
        _clear_memory_buffer_only()

        result = filter_tts_echo("The weather today is sunny and warm")
        assert result == "", "Journal-only filter must strip exact-match echo"


class TestHeraldWorkerRegistersText:
    """DEF-078: Herald's process_response() must register the finalized speech
    text with the echo buffer so cross-process STT can filter it."""

    def test_worker_registers_before_generation(self, monkeypatch, tmp_path):
        """When process_response processes a <tts> block, the text must appear
        in the echo journal even if TTS generation is stubbed out."""
        from heyvox.audio.echo import _echo_buffer
        from heyvox.constants import TTS_ECHO_JOURNAL
        from heyvox.herald import worker as worker_mod

        # Stub out the expensive/impure bits: TTS generation, mood detection,
        # orchestrator spawn, and the dedup claim file.
        monkeypatch.setattr(
            worker_mod.HeraldWorker, "_generate",
            lambda self, *a, **k: True,
        )
        monkeypatch.setattr(
            worker_mod, "_ensure_orchestrator", lambda: None,
        )
        # Redirect claim dir so we don't collide with a real Herald install.
        claim_dir = tmp_path / "claim"
        monkeypatch.setattr(worker_mod, "HERALD_CLAIM_DIR", str(claim_dir))

        raw = (
            "Some response text.\n"
            "<tts>Committed to the local claude-config repo</tts>\n"
        )

        w = worker_mod.HeraldWorker()
        ok = w.process_response(raw, hook_type="response")
        assert ok

        assert os.path.exists(TTS_ECHO_JOURNAL), \
            "Herald worker must register TTS text in the cross-process journal"

        with open(TTS_ECHO_JOURNAL) as fh:
            body = fh.read().lower()
        assert "committed to the local" in body


class TestAggressiveOverlapThreshold:
    """DEF-078: When the recording state machine detects that TTS was playing
    during the recording window, it passes aggressive=True. The filter drops
    the word-overlap threshold from 0.6 to 0.4, so partial bleed gets stripped."""

    def test_moderate_overlap_passes_without_aggressive(self):
        """45% overlap must pass through at the default threshold."""
        from heyvox.audio.echo import register_tts_text, filter_tts_echo

        # TTS text: 10 distinct words. Transcription: 11 words with 5 matches
        # → 5/11 ≈ 45% overlap → below default 60% threshold.
        register_tts_text(
            "alpha bravo charlie delta echo foxtrot golf hotel india juliet"
        )
        transcription = (
            "alpha bravo charlie delta echo please show me the next step"
        )

        result = filter_tts_echo(transcription, aggressive=False)
        assert result == transcription, \
            "45% overlap must NOT be stripped at default threshold 0.6"

    def test_moderate_overlap_stripped_with_aggressive(self):
        """Same 45% overlap must be stripped when aggressive=True."""
        from heyvox.audio.echo import register_tts_text, filter_tts_echo

        register_tts_text(
            "alpha bravo charlie delta echo foxtrot golf hotel india juliet"
        )
        transcription = (
            "alpha bravo charlie delta echo please show me the next step"
        )

        result = filter_tts_echo(transcription, aggressive=True)
        assert result == "", \
            "45% overlap must be stripped at aggressive threshold 0.4"

    def test_low_overlap_passes_even_with_aggressive(self):
        """Aggressive mode must still let genuine user speech through."""
        from heyvox.audio.echo import register_tts_text, filter_tts_echo

        register_tts_text("the deployment succeeded without warnings")
        # User says something completely unrelated.
        transcription = "please run the integration tests in parallel"

        result = filter_tts_echo(transcription, aggressive=True)
        # "the" is the only matching word → ~14% overlap → must pass.
        assert result == transcription


class TestJournalStaleness:
    """Entries older than TTS_ECHO_BUFFER_SECS must not participate in filtering."""

    def test_stale_entry_ignored(self):
        from heyvox.audio.echo import filter_tts_echo
        from heyvox.constants import TTS_ECHO_JOURNAL, TTS_ECHO_BUFFER_SECS

        stale_ts = time.time() - (TTS_ECHO_BUFFER_SECS + 5.0)
        with open(TTS_ECHO_JOURNAL, "w") as fh:
            fh.write(json.dumps({
                "ts": stale_ts,
                "text": "the old tts message from long ago",
            }) + "\n")

        _clear_memory_buffer_only()

        transcription = "The old TTS message from long ago"
        result = filter_tts_echo(transcription)
        assert result == transcription, \
            "Entries older than the buffer window must not strip transcription"


class TestJournalPrune:
    """Opportunistic prune keeps the journal bounded without requiring a
    separate cleanup process."""

    def test_prune_rewrites_when_bloated(self):
        from heyvox.audio.echo import _read_journal_recent
        from heyvox.constants import TTS_ECHO_JOURNAL, TTS_ECHO_BUFFER_SECS

        now = time.time()
        stale_ts = now - (TTS_ECHO_BUFFER_SECS + 10.0)

        # Write 300 stale lines + 3 fresh lines (>256 → triggers prune).
        with open(TTS_ECHO_JOURNAL, "w") as fh:
            for i in range(300):
                fh.write(json.dumps({"ts": stale_ts, "text": f"stale {i}"}) + "\n")
            for i in range(3):
                fh.write(json.dumps({"ts": now, "text": f"fresh {i}"}) + "\n")

        cutoff = now - TTS_ECHO_BUFFER_SECS
        fresh = _read_journal_recent(cutoff)

        assert len(fresh) == 3

        # After prune, the on-disk file should contain only the 3 fresh lines.
        with open(TTS_ECHO_JOURNAL) as fh:
            remaining = [ln for ln in fh.read().splitlines() if ln.strip()]
        assert len(remaining) == 3, \
            f"Expected journal pruned to 3 fresh entries, got {len(remaining)}"


class TestBackwardsCompatibility:
    """filter_tts_echo must remain callable with the legacy 1-arg signature
    (no `aggressive` kwarg) — it's called from the recording state machine and
    other pre-DEF-078 call sites."""

    def test_default_signature_still_works(self):
        from heyvox.audio.echo import register_tts_text, filter_tts_echo

        register_tts_text("hello world this is a test")
        result = filter_tts_echo("hello world this is a test")
        assert result == ""

    def test_empty_transcription_passthrough(self):
        from heyvox.audio.echo import filter_tts_echo

        assert filter_tts_echo("") == ""
        assert filter_tts_echo("   ") == "   "
