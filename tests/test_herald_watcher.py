"""Tests for heyvox/herald/daemon/watcher.py — DEF-237 session_id/workspace_id.

This module has no prior test file. Scope here is deliberately narrow: only
the new sidecar-identity plumbing this fix adds, not the daemon's full
JSONL-tailing runtime (file-position tracking, claim-file locking, signal
handling) — that would need a much larger test harness unrelated to this fix.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from heyvox.herald.daemon import watcher


def _fake_kokoro_socket(response: dict) -> MagicMock:
    """A mock socket.socket() usable as `with socket.socket(...) as s:`."""
    mock_sock = MagicMock()
    mock_sock.__enter__.return_value = mock_sock
    mock_sock.__exit__.return_value = False
    mock_sock.recv.side_effect = [json.dumps(response).encode(), b""]
    return mock_sock


class TestSendToKokoroSidecar:
    def _send(self, tmp_path, monkeypatch, *, workspace="seattle", session_id="sess-uuid-456",
              workspace_id="ws-uuid-123"):
        queue_dir = tmp_path / "queue"
        monkeypatch.setattr(watcher, "QUEUE_DIR", str(queue_dir))
        monkeypatch.setattr(watcher, "_TMP", str(tmp_path))
        monkeypatch.setattr(watcher, "last_tts_time", 0)
        monkeypatch.setattr(watcher, "_get_verbosity", lambda: "full")
        monkeypatch.setattr(watcher, "_apply_verbosity", lambda speech, v: speech)
        monkeypatch.setattr(watcher, "detect_mood_voice", lambda speech: "af_sarah")

        # send_to_kokoro renames this exact path — must exist before the call.
        temp_wav = Path(f"{tmp_path}/herald-watcher-{os.getpid()}.wav")
        temp_wav.write_bytes(b"RIFF....")

        with patch("socket.socket", return_value=_fake_kokoro_socket({"ok": True, "parts": 1, "duration": 0.1})), \
             patch("heyvox.herald.workspace_label.resolve_workspace_id", return_value=workspace_id):
            ok = watcher.send_to_kokoro(
                "hello from the test", workspace=workspace, session_id=session_id,
            )
        return ok, queue_dir

    def test_writes_sidecar_with_session_and_workspace_id(self, tmp_path, monkeypatch):
        ok, queue_dir = self._send(tmp_path, monkeypatch)
        assert ok is True

        sidecar_files = [p for p in os.listdir(queue_dir) if p.endswith(".workspace")]
        assert len(sidecar_files) == 1
        from heyvox.herald.workspace_label import read_switch_sidecar
        identity = read_switch_sidecar((queue_dir / sidecar_files[0]).read_text())
        assert identity == {
            "workspace": "seattle", "workspace_id": "ws-uuid-123", "session_id": "sess-uuid-456",
        }

    def test_no_workspace_skips_workspace_id_resolution(self, tmp_path, monkeypatch):
        """No workspace known — resolve_workspace_id must not even be called,
        matching HeraldWorker's equivalent guard."""
        queue_dir = tmp_path / "queue"
        monkeypatch.setattr(watcher, "QUEUE_DIR", str(queue_dir))
        monkeypatch.setattr(watcher, "_TMP", str(tmp_path))
        monkeypatch.setattr(watcher, "last_tts_time", 0)
        monkeypatch.setattr(watcher, "_get_verbosity", lambda: "full")
        monkeypatch.setattr(watcher, "_apply_verbosity", lambda speech, v: speech)
        monkeypatch.setattr(watcher, "detect_mood_voice", lambda speech: "af_sarah")

        temp_wav = Path(f"{tmp_path}/herald-watcher-{os.getpid()}.wav")
        temp_wav.write_bytes(b"RIFF....")

        with patch("socket.socket", return_value=_fake_kokoro_socket({"ok": True, "parts": 1, "duration": 0.1})), \
             patch(
                 "heyvox.herald.workspace_label.resolve_workspace_id",
                 side_effect=AssertionError("must not be called without a workspace"),
             ):
            ok = watcher.send_to_kokoro("hello", workspace="", session_id="sess-uuid-456")

        assert ok is True
        assert not any(p.endswith(".workspace") for p in os.listdir(queue_dir))

    def test_multipart_writes_sidecar_for_each_part(self, tmp_path, monkeypatch):
        queue_dir = tmp_path / "queue"
        monkeypatch.setattr(watcher, "QUEUE_DIR", str(queue_dir))
        monkeypatch.setattr(watcher, "_TMP", str(tmp_path))
        monkeypatch.setattr(watcher, "last_tts_time", 0)
        monkeypatch.setattr(watcher, "_get_verbosity", lambda: "full")
        monkeypatch.setattr(watcher, "_apply_verbosity", lambda speech, v: speech)
        monkeypatch.setattr(watcher, "detect_mood_voice", lambda speech: "af_sarah")

        base = Path(f"{tmp_path}/herald-watcher-{os.getpid()}")
        Path(f"{base}.wav").write_bytes(b"RIFF....")
        Path(f"{base}.part2.wav").write_bytes(b"RIFF....")

        with patch("socket.socket", return_value=_fake_kokoro_socket({"ok": True, "parts": 2, "duration": 0.1})), \
             patch("heyvox.herald.workspace_label.resolve_workspace_id", return_value="ws-uuid-123"):
            ok = watcher.send_to_kokoro(
                "hello", workspace="seattle", session_id="sess-uuid-456",
            )

        assert ok is True
        sidecar_files = sorted(p for p in os.listdir(queue_dir) if p.endswith(".workspace"))
        assert len(sidecar_files) == 2
        from heyvox.herald.workspace_label import read_switch_sidecar
        for name in sidecar_files:
            identity = read_switch_sidecar((queue_dir / name).read_text())
            assert identity == {
                "workspace": "seattle", "workspace_id": "ws-uuid-123", "session_id": "sess-uuid-456",
            }


class TestProcessNewLinesSessionId:
    def test_session_id_derived_from_jsonl_filename(self, tmp_path, monkeypatch):
        """Claude Code names transcripts <session-id>.jsonl — process_new_lines
        must derive session_id from that filename and thread it through to
        send_to_kokoro."""
        jsonl_path = tmp_path / "c02c05c3-cf55-458a-978d-76fb8842c81a.jsonl"
        # extract_last_tts_block requires the <tts> tag to sit in the back
        # half of the text (near-the-tail heuristic) — pad the front so a
        # short test message doesn't get rejected by that check.
        text = "x" * 100 + " <tts>hi there</tts>"
        message = {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": text}]},
        }
        jsonl_path.write_text(json.dumps(message) + "\n")

        monkeypatch.setattr(watcher, "file_positions", {})
        monkeypatch.setattr(watcher, "detect_workspace_from_path", lambda p: "seattle")
        # Never let this test touch Franz's real, shared herald-claim dir.
        monkeypatch.setattr(watcher, "CLAIM_DIR", str(tmp_path / "claim"))

        captured = {}

        def _fake_send(speech, workspace="", hook_epoch_ms=0, session_id=""):
            captured["session_id"] = session_id
            captured["workspace"] = workspace
            return True

        with patch.object(watcher, "send_to_kokoro", side_effect=_fake_send), \
             patch("heyvox.audio.echo.register_tts_text", create=True):
            watcher.process_new_lines(str(jsonl_path))

        assert captured["session_id"] == "c02c05c3-cf55-458a-978d-76fb8842c81a"
        assert captured["workspace"] == "seattle"
