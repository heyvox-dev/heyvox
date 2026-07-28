"""Guard tests for Lua-injection hardening in the Herald orchestrator (DEF-177).

``_show_alert()`` interpolates a workspace label / message into a single-quoted
Lua string that is executed via ``hs -c``. The workspace label can originate
from an externally-authored Conductor PR title, so a crafted backslash-quote
sequence must not break out of the ``'...'`` literal into attacker-controlled
Lua. The pre-fix ``.replace("'", "\\'")`` escaped the quote but not the
backslash, leaving ``\\'`` able to escape.

These tests are net-free: they exercise the pure escaper and drive the call
site with subprocess + Hammerspoon patched out (no real ``hs`` invocation).

References: .planning/DEFECT-LOG.md (DEF-177),
.context/release-audit/03-security.md §1
"""

from __future__ import annotations

from unittest.mock import patch

from heyvox.herald.orchestrator import _lua_str_escape, _show_alert

# INJECTION_PAYLOADS[0] is the security audit's proof-of-concept: a backslash
# immediately before a quote, which a naive quote-only escape leaves able to
# break out. The rest cover adjacent breakout shapes plus benign inputs.
INJECTION_PAYLOADS = [
    r"foo\' .. os.execute('touch /tmp/pwned') .. '",
    r"'; os.execute('id'); '",
    "back\\slash",
    "line\nbreak",
    "carriage\rreturn",
    "plain workspace",
    "nested \\\\ '' quotes",
]


def _lua_unescape_single_quoted(s: str) -> str:
    """Decode a single-quoted Lua string body using only the escapes we emit.

    Raises AssertionError on any *unescaped* single quote (a breakout) or a
    dangling backslash — exactly the conditions that would let a payload escape
    the surrounding ``'...'`` literal. A successful decode that round-trips to
    the original therefore proves the escaping is both safe and lossless.
    """
    decode = {"\\": "\\", "'": "'", "n": "\n", "r": "\r"}
    out: list[str] = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\":
            assert i + 1 < len(s), f"dangling backslash in {s!r}"
            nxt = s[i + 1]
            assert nxt in decode, f"unexpected escape \\{nxt} in {s!r}"
            out.append(decode[nxt])
            i += 2
        else:
            assert c != "'", f"unescaped single quote (breakout) in {s!r}"
            out.append(c)
            i += 1
    return "".join(out)


def test_lua_escape_roundtrips_and_never_breaks_out():
    """Escaping is safe (no bare quote / dangling backslash) and lossless."""
    for payload in INJECTION_PAYLOADS:
        escaped = _lua_str_escape(payload)
        assert _lua_unescape_single_quoted(escaped) == payload


def test_lua_escape_doubles_backslash_before_quote():
    r"""The `\'` case: the backslash must be doubled BEFORE the quote is escaped."""
    # Input: one literal backslash followed by one literal quote.
    assert _lua_str_escape("\\'") == "\\\\\\'"
    # Naive quote-only escaping yields "\\\\'" (escaped backslash + a BARE
    # quote) — a breakout. Assert we did not regress to that.
    assert _lua_str_escape("\\'") != "\\\\'"


def test_lua_escape_neutralizes_newlines():
    """Raw CR/LF (which terminate a single-quoted Lua string) are escaped."""
    assert "\n" not in _lua_str_escape("a\nb")
    assert "\r" not in _lua_str_escape("a\rb")


def test_show_alert_embeds_only_escaped_message():
    """_show_alert escapes its message before the hs -c interpolation."""
    payload = INJECTION_PAYLOADS[1]
    with patch(
        "heyvox.herald.orchestrator._hammerspoon_running", return_value=True
    ), patch(
        "heyvox.herald.orchestrator.shutil.which", return_value="/bin/sh"
    ), patch(
        "heyvox.herald.orchestrator.subprocess.Popen"
    ) as popen:
        _show_alert(payload)

    assert popen.called, "expected `hs -c` to be invoked"
    script = popen.call_args[0][0][2]
    assert _lua_str_escape(payload) in script
    assert "os.execute('id')" not in script
