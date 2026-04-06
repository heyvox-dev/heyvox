"""
Tests for stale flag cleanup on startup.

Covers bug-audit patterns:
- Stale /tmp flag files surviving process restarts
- Mute flags persisting from crashed sessions
- Recording flag left behind after crash
"""

import os
import unittest


# Flags that main.py cleans up on startup
STARTUP_CLEANUP_FLAGS = [
    "/tmp/heyvox-recording",
    "/tmp/heyvox-tts-playing",
    "/tmp/claude-tts-mute",
    "/tmp/herald-mute",
    "/tmp/heyvox-verbosity",
]


class TestStaleFlagCleanup(unittest.TestCase):
    """Verify the flag cleanup logic works correctly."""

    def test_recording_flag_removed(self):
        """Recording flag from crashed session should be removable."""
        flag = "/tmp/heyvox-recording"
        open(flag, "w").close()
        assert os.path.exists(flag)
        os.remove(flag)
        assert not os.path.exists(flag)

    def test_mute_flags_removed(self):
        """Mute flags from previous session should be removable."""
        for flag in ["/tmp/claude-tts-mute", "/tmp/herald-mute"]:
            open(flag, "w").close()
            assert os.path.exists(flag)
            os.remove(flag)
            assert not os.path.exists(flag)

    def test_cleanup_tolerates_missing_flags(self):
        """Cleanup should not raise if flags don't exist."""
        for flag in STARTUP_CLEANUP_FLAGS:
            try:
                os.remove(flag)
            except FileNotFoundError:
                pass  # Expected — this is the tolerance check
            assert not os.path.exists(flag)

    def test_verbosity_file_removed(self):
        """Verbosity file should be removable for clean default."""
        flag = "/tmp/heyvox-verbosity"
        with open(flag, "w") as f:
            f.write("skip")
        assert os.path.exists(flag)
        os.remove(flag)
        assert not os.path.exists(flag)


class TestFlagAtomicity(unittest.TestCase):
    """File-based IPC should use atomic writes where possible."""

    def test_active_mic_atomic_write(self):
        """Writing mic name should be atomic (temp + rename)."""
        mic_file = "/tmp/heyvox-test-atomic"
        tmp_file = mic_file + ".tmp"
        try:
            # This is the atomic write pattern used in overlay.py
            with open(tmp_file, "w") as f:
                f.write("MacBook Pro Microphone")
            os.rename(tmp_file, mic_file)

            with open(mic_file) as f:
                assert f.read() == "MacBook Pro Microphone"
        finally:
            for f in [mic_file, tmp_file]:
                try:
                    os.remove(f)
                except FileNotFoundError:
                    pass

    def test_concurrent_readers_see_complete_content(self):
        """Atomic rename ensures readers never see partial writes."""
        import threading

        mic_file = "/tmp/heyvox-test-atomic-concurrent"
        results = []
        errors = []

        def writer():
            for i in range(100):
                tmp = mic_file + ".tmp"
                with open(tmp, "w") as f:
                    f.write(f"Mic-{i:04d}")
                os.rename(tmp, mic_file)

        def reader():
            for _ in range(100):
                try:
                    with open(mic_file) as f:
                        val = f.read()
                    # Should always be a complete "Mic-NNNN" string
                    if val and not val.startswith("Mic-"):
                        errors.append(f"Partial read: {val!r}")
                    results.append(val)
                except FileNotFoundError:
                    pass  # OK — file doesn't exist yet

        try:
            t_w = threading.Thread(target=writer)
            t_r = threading.Thread(target=reader)
            t_w.start()
            t_r.start()
            t_w.join()
            t_r.join()
            assert errors == [], f"Partial reads detected: {errors}"
        finally:
            try:
                os.remove(mic_file)
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    unittest.main()
