#!/usr/bin/env python3
"""
Headset media-key sniffer — what reaches macOS as NSSystemDefined events.

Many headset buttons (volume up/down, play/pause, next/prev, mute) arrive on
macOS as ``NSSystemDefined`` events (CGEventType 14, subtype 8 =
NX_SUBTYPE_AUX_CONTROL_BUTTONS) rather than ordinary key events. HeyVox's PTT
tap (heyvox/input/ptt.py) only listens for kCGEventFlagsChanged + kCGEventKeyDown,
so these never show up there today.

This is a *listen-only* CGEventTap: it observes, never consumes, so your normal
volume/playback keys keep working while you test. Press each button on the
headset and watch which ones print a line — those are the candidates that can be
wired into the existing GestureRecognizer (double-tap → hands-free) later.

Companion to tools/headset_hid_sniffer.py, which catches the *telephony* buttons
(mute / call answer-end) that arrive as raw HID and never become NSSystemDefined.

Permission: needs Accessibility (or Input Monitoring) for the binary running it.
If nothing ever prints, grant the python binary that in System Settings →
Privacy & Security → Accessibility, then re-run.

Usage:
    /Users/work/.pyenv/versions/3.12.12/bin/python tools/headset_mediakey_sniffer.py
    ... tools/headset_mediakey_sniffer.py --seconds 60
"""
from __future__ import annotations

import argparse
import sys
import time

# NX_KEYTYPE_* — the keyCode field of an aux-control NSSystemDefined event.
_NX_KEYTYPE = {
    0: "SOUND_UP",
    1: "SOUND_DOWN",
    2: "BRIGHTNESS_UP",
    3: "BRIGHTNESS_DOWN",
    4: "CAPS_LOCK",
    5: "HELP",
    6: "POWER",
    7: "MUTE",
    8: "UP_ARROW",
    9: "DOWN_ARROW",
    10: "NUM_LOCK",
    11: "CONTRAST_UP",
    12: "CONTRAST_DOWN",
    13: "LAUNCH_PANEL",
    14: "EJECT",
    15: "VIDMIRROR",
    16: "PLAY",
    17: "NEXT",
    18: "PREVIOUS",
    19: "FAST",
    20: "REWIND",
    21: "ILLUMINATION_UP",
    22: "ILLUMINATION_DOWN",
    23: "ILLUMINATION_TOGGLE",
}

_NSSYSTEMDEFINED = 14            # NSEventTypeSystemDefined
_NX_SUBTYPE_AUX = 8             # NX_SUBTYPE_AUX_CONTROL_BUTTONS
_NX_KEYDOWN = 0x0A
_NX_KEYUP = 0x0B


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=45.0,
                    help="how long to listen before exiting (default 45)")
    args = ap.parse_args()

    try:
        import Quartz
        from AppKit import NSEvent
    except ImportError as e:  # pragma: no cover - env guard
        print(f"ERROR: pyobjc not available in this interpreter: {e}", file=sys.stderr)
        print("Run with the HeyVox daemon python (has Quartz/AppKit).", file=sys.stderr)
        return 2

    seen: set[tuple[int, int]] = set()

    def callback(proxy, etype, event, refcon):
        # Re-enable if macOS disabled the tap (timeout / user-input glitch).
        if etype in (Quartz.kCGEventTapDisabledByTimeout,
                     Quartz.kCGEventTapDisabledByUserInput):
            Quartz.CGEventTapEnable(tap, True)
            return event
        try:
            ns = NSEvent.eventWithCGEvent_(event)
            if ns is None or ns.type() != _NSSYSTEMDEFINED or ns.subtype() != _NX_SUBTYPE_AUX:
                return event
            data1 = ns.data1()
            key_code = (data1 & 0xFFFF0000) >> 16
            key_flags = data1 & 0x0000FFFF
            key_state = (key_flags & 0xFF00) >> 8
            is_repeat = key_flags & 0x1
            name = _NX_KEYTYPE.get(key_code, f"UNKNOWN({key_code})")
            state = {_NX_KEYDOWN: "DOWN", _NX_KEYUP: "UP"}.get(key_state, f"0x{key_state:02x}")
            ts = time.strftime("%H:%M:%S")
            rpt = " repeat" if is_repeat else ""
            marker = ""
            if key_state == _NX_KEYDOWN and (key_code, key_state) not in seen:
                seen.add((key_code, key_state))
                marker = "   <-- NEW button (usable trigger candidate)"
            print(f"[{ts}] SystemDefined  keyCode={key_code:>2} {name:<18} {state}{rpt}{marker}",
                  flush=True)
        except Exception as e:  # keep the tap alive no matter what
            print(f"  (decode error, tap preserved: {e})", flush=True)
        return event

    mask = Quartz.CGEventMaskBit(_NSSYSTEMDEFINED)
    tap = Quartz.CGEventTapCreate(
        Quartz.kCGSessionEventTap,
        Quartz.kCGHeadInsertEventTap,
        Quartz.kCGEventTapOptionListenOnly,   # observe only — never consume
        mask,
        callback,
        None,
    )
    if tap is None:
        print("ERROR: could not create CGEventTap.", file=sys.stderr)
        print("Grant Accessibility (or Input Monitoring) to this python binary:",
              file=sys.stderr)
        print(f"  {sys.executable}", file=sys.stderr)
        print("System Settings → Privacy & Security → Accessibility, then re-run.",
              file=sys.stderr)
        return 3

    source = Quartz.CFMachPortCreateRunLoopSource(None, tap, 0)
    loop = Quartz.CFRunLoopGetCurrent()
    Quartz.CFRunLoopAddSource(loop, source, Quartz.kCFRunLoopDefaultMode)
    Quartz.CGEventTapEnable(tap, True)

    print("=" * 72)
    print(" Media-key sniffer running (listen-only — your keys still work).")
    print(" Press each headset button: volume +/-, play/pause, next, prev, mute.")
    print(" A line per event means that button is catchable via CGEventTap.")
    print(f" Listening for {args.seconds:.0f}s — Ctrl-C to stop early.")
    print("=" * 72, flush=True)

    deadline = time.time() + args.seconds
    try:
        while time.time() < deadline:
            Quartz.CFRunLoopRunInMode(Quartz.kCFRunLoopDefaultMode, 0.25, False)
    except KeyboardInterrupt:
        pass

    print("-" * 72)
    if seen:
        names = sorted({_NX_KEYTYPE.get(kc, f"UNKNOWN({kc})") for kc, _ in seen})
        print(f" Catchable buttons this run: {', '.join(names)}")
    else:
        print(" No SystemDefined media-key events seen. Either the headset sends")
        print(" nothing catchable here (try headset_hid_sniffer.py for the mute/")
        print(" call buttons), or Accessibility/Input-Monitoring isn't granted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
