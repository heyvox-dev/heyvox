"""DEF-272 guards: an orphaned PyAudio reader must never outlive its stream.

PortAudio's blocking ReadStream (pa_mac_core_blocking.c, v19.7.0) busy-waits on
the stream's ring buffer with no stream-state check. The main loop used to read
a full chunk as soon as ONE frame was available, so a device stalling mid-chunk
blocked the read forever; the 3 s timeout then abandoned the reader thread and
recovered in-process via handle_io_error(), which closed the stream and freed
the ring buffer the orphan still polled. Hours later: SIGSEGV in
PaUtil_ReadRingBuffer (2026-10-07, 2026-10-13 — the second lost a dictation).

Source-level guards in the style of test_defect_guards.py: the main loop is a
single large function that needs real audio hardware to run.
"""

import os
import re


def _read_main_py() -> str:
    path = os.path.join(os.path.dirname(__file__), "..", "heyvox", "main.py")
    with open(path, encoding="utf-8") as f:
        return f.read()


def _timeout_handler_block(src: str) -> str:
    """Return the body of the read-timeout except block in the main loop."""
    m = re.search(
        r"\.result\(timeout=3\.0\)\s*\n\s*except \(_cf\.TimeoutError, TimeoutError\):\n(.*?)\n\s*audio = np\.frombuffer\(_raw",
        src,
        re.S,
    )
    assert m, "DEF-272: guarded main-loop read with a 3 s timeout not found in main.py"
    # Code only: the rationale comment names the forbidden calls on purpose.
    return "\n".join(
        line for line in m.group(1).splitlines() if not line.strip().startswith("#")
    )


def test_def272_read_waits_for_a_full_chunk():
    """The read gate must require chunk_size frames, not just one."""
    src = _read_main_py()
    assert "_read_avail = devices.stream.get_read_available()" in src
    assert "if _read_avail < chunk_size:" in src, (
        "DEF-272: the main loop must only call stream.read(chunk_size) once a full "
        "chunk is available. Reading more frames than available can block forever "
        "in PortAudio's ReadStream busy-wait."
    )
    assert "if _read_avail < 1:" not in src, (
        "DEF-272: the old 1-frame gate is back; it lets stream.read() block mid-chunk."
    )


def test_def272_read_timeout_does_not_recover_in_process():
    """A read timeout must restart the process, never close the stream under the orphan."""
    block = _timeout_handler_block(_read_main_py())
    for forbidden in ("handle_io_error", "reinit(", ".close()", "terminate()"):
        assert forbidden not in block, (
            f"DEF-272: the read-timeout handler calls {forbidden!r}. The abandoned "
            "reader thread is still inside ReadStream; freeing its stream causes a "
            "delayed use-after-free SIGSEGV. Restart the process instead."
        )
    assert "os.execv(" in block, (
        "DEF-272: the read-timeout handler must re-exec the process — a fresh "
        "process image is the only state without the orphaned reader."
    )
    assert "_release_singleton()" in block, (
        "DEF-272: release the singleton lock before execv like the other restart paths."
    )


def test_def272_rationale_comment_present():
    """Keep the rationale next to the code so the gate isn't 'simplified' back."""
    assert _read_main_py().count("DEF-272") >= 2
