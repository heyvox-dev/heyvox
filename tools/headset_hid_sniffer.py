#!/usr/bin/env python3
"""
Headset HID sniffer — raw IOKit reports from the Jabra Link dongle.

The buttons that never become NSSystemDefined events — the headset's mute and
call answer/end keys — arrive as raw USB-HID input on the Telephony usage page
(0x0B: Hook Switch, Phone Mute). Through the Jabra Link 380 dongle these are
visible on the HID bus (the dongle presents as a USB-HID composite device),
unlike direct Bluetooth pairing where the mute often stays firmware-local.

This taps IOHIDManager directly via ctypes — no pip dependency, only the system
IOKit + CoreFoundation frameworks. It opens devices *non-exclusively* (never
seizes), so audio and normal key handling are unaffected. For each HID input
value it prints the device, usage page, usage, and value, so you can see exactly
which report a button press generates.

By default it filters to Jabra (USB vendor 0x0B0E). Pass --all to see every HID
device (noisy: also internal keyboard/trackpad).

The one open question this answers: some dongles only emit Phone-Mute reports
during an active call stream (HFP). If the mute button prints nothing here while
idle, that's the cause — and the script will have proved it rather than guessed.

Permission: needs Input Monitoring for the binary running it (System Settings →
Privacy & Security → Input Monitoring). If the manager opens but no events ever
print for ANY device (try --all), permission is the likely cause.

Usage:
    /Users/work/.pyenv/versions/3.12.12/bin/python tools/headset_hid_sniffer.py
    ... tools/headset_hid_sniffer.py --seconds 60
    ... tools/headset_hid_sniffer.py --all        # every HID device (noisy)
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import sys
import time
from ctypes import CFUNCTYPE, byref, c_bool, c_char_p, c_double, c_int32, c_long, c_uint32, c_void_p

JABRA_VENDOR_ID = 0x0B0E  # GN Netcom / Jabra

# HID usage pages worth labelling.
_USAGE_PAGE = {
    0x01: "Generic Desktop",
    0x07: "Keyboard",
    0x0B: "Telephony",     # <-- mute / hook-switch / call control lives here
    0x0C: "Consumer",      # <-- volume / play-pause / media transport
    0x08: "LED",
    0x09: "Button",
}

# Telephony (0x0B) usages most relevant to a headset.
_TELEPHONY_USAGE = {
    0x20: "Hook Switch",
    0x21: "Flash",
    0x22: "Feature",
    0x24: "Redial",
    0x2F: "Phone Mute",
    0x07: "Programmable Button",
}
# Consumer (0x0C) transport usages.
_CONSUMER_USAGE = {
    0xB0: "Play", 0xB1: "Pause", 0xCD: "Play/Pause", 0xB5: "Next", 0xB6: "Prev",
    0xB7: "Stop", 0xE2: "Mute", 0xE9: "Volume Up", 0xEA: "Volume Down",
}

_UTF8 = 0x08000100          # kCFStringEncodingUTF8
_NUM_SINT32 = 0x3           # kCFNumberSInt32Type


def _load():
    """Wire up the CoreFoundation + IOKit symbols we need, with prototypes."""
    cf = ctypes.cdll.LoadLibrary(ctypes.util.find_library("CoreFoundation"))
    io = ctypes.cdll.LoadLibrary(ctypes.util.find_library("IOKit"))

    cf.CFRunLoopGetCurrent.restype = c_void_p
    cf.CFRunLoopRunInMode.argtypes = [c_void_p, c_double, c_bool]
    cf.CFRunLoopRunInMode.restype = c_int32
    cf.CFStringCreateWithCString.argtypes = [c_void_p, c_char_p, c_uint32]
    cf.CFStringCreateWithCString.restype = c_void_p
    cf.CFStringGetCString.argtypes = [c_void_p, c_char_p, c_long, c_uint32]
    cf.CFStringGetCString.restype = c_bool
    cf.CFNumberGetValue.argtypes = [c_void_p, c_long, c_void_p]
    cf.CFNumberGetValue.restype = c_bool

    io.IOHIDManagerCreate.argtypes = [c_void_p, c_uint32]
    io.IOHIDManagerCreate.restype = c_void_p
    io.IOHIDManagerSetDeviceMatching.argtypes = [c_void_p, c_void_p]
    io.IOHIDManagerScheduleWithRunLoop.argtypes = [c_void_p, c_void_p, c_void_p]
    io.IOHIDManagerOpen.argtypes = [c_void_p, c_uint32]
    io.IOHIDManagerOpen.restype = c_int32
    io.IOHIDValueGetElement.argtypes = [c_void_p]
    io.IOHIDValueGetElement.restype = c_void_p
    io.IOHIDValueGetIntegerValue.argtypes = [c_void_p]
    io.IOHIDValueGetIntegerValue.restype = c_long
    io.IOHIDElementGetUsagePage.argtypes = [c_void_p]
    io.IOHIDElementGetUsagePage.restype = c_uint32
    io.IOHIDElementGetUsage.argtypes = [c_void_p]
    io.IOHIDElementGetUsage.restype = c_uint32
    io.IOHIDElementGetDevice.argtypes = [c_void_p]
    io.IOHIDElementGetDevice.restype = c_void_p
    io.IOHIDDeviceGetProperty.argtypes = [c_void_p, c_void_p]
    io.IOHIDDeviceGetProperty.restype = c_void_p
    return cf, io


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--seconds", type=float, default=45.0,
                    help="how long to listen before exiting (default 45)")
    ap.add_argument("--all", action="store_true",
                    help="show every HID device, not just Jabra (noisy)")
    args = ap.parse_args()

    try:
        cf, io = _load()
    except Exception as e:  # pragma: no cover - env guard
        print(f"ERROR: could not load IOKit/CoreFoundation: {e}", file=sys.stderr)
        return 2

    kcfdefaultmode = c_void_p.in_dll(cf, "kCFRunLoopDefaultMode")

    def cfstr(s: str) -> c_void_p:
        return c_void_p(cf.CFStringCreateWithCString(None, s.encode("utf-8"), _UTF8))

    key_vendor = cfstr("VendorID")
    key_product = cfstr("ProductID")
    key_name = cfstr("Product")

    def dev_int(dev: int, key: c_void_p):
        ref = io.IOHIDDeviceGetProperty(dev, key)
        if not ref:
            return None
        out = c_int32(0)
        if cf.CFNumberGetValue(ref, _NUM_SINT32, byref(out)):
            return out.value
        return None

    def dev_name(dev: int):
        ref = io.IOHIDDeviceGetProperty(dev, key_name)
        if not ref:
            return None
        buf = ctypes.create_string_buffer(256)
        if cf.CFStringGetCString(ref, buf, 256, _UTF8):
            return buf.value.decode("utf-8", "replace")
        return None

    dev_cache: dict[int, tuple] = {}
    seen: set[tuple[int, int, int]] = set()
    counters = {"events": 0}

    def describe_usage(page: int, usage: int) -> str:
        if page == 0x0B:
            return _TELEPHONY_USAGE.get(usage, f"usage 0x{usage:02x}")
        if page == 0x0C:
            return _CONSUMER_USAGE.get(usage, f"usage 0x{usage:02x}")
        return f"usage 0x{usage:02x}"

    def callback(context, result, sender, value):
        try:
            counters["events"] += 1
            element = io.IOHIDValueGetElement(value)
            if not element:
                return
            dev = io.IOHIDElementGetDevice(element)
            if dev not in dev_cache:
                dev_cache[dev] = (dev_int(dev, key_vendor),
                                  dev_int(dev, key_product),
                                  dev_name(dev))
            vid, pid, name = dev_cache[dev]
            if not args.all and vid != JABRA_VENDOR_ID:
                return
            page = io.IOHIDElementGetUsagePage(element)
            usage = io.IOHIDElementGetUsage(element)
            ival = io.IOHIDValueGetIntegerValue(value)
            page_name = _USAGE_PAGE.get(page, f"page 0x{page:02x}")
            ts = time.strftime("%H:%M:%S")
            label = describe_usage(page, usage)
            tag = ""
            if page in (0x0B, 0x0C):
                tag = "  <-- telephony/consumer (button candidate)"
            vidpid = f"{(vid or 0):04x}:{(pid or 0):04x}"
            print(f"[{ts}] {vidpid} {(name or '?')[:22]:<22} "
                  f"{page_name:<16} {label:<18} value={ival}{tag}", flush=True)
            seen.add((page, usage, dev))
        except Exception as e:
            print(f"  (callback error: {e})", flush=True)

    CALLBACK = CFUNCTYPE(None, c_void_p, c_int32, c_void_p, c_void_p)
    cb = CALLBACK(callback)  # keep a reference alive — GC of this = crash
    io.IOHIDManagerRegisterInputValueCallback.argtypes = [c_void_p, CALLBACK, c_void_p]

    mgr = io.IOHIDManagerCreate(None, 0)
    if not mgr:
        print("ERROR: IOHIDManagerCreate returned NULL.", file=sys.stderr)
        return 3
    io.IOHIDManagerSetDeviceMatching(mgr, None)   # match all; we filter in cb
    io.IOHIDManagerRegisterInputValueCallback(mgr, cb, None)
    io.IOHIDManagerScheduleWithRunLoop(mgr, cf.CFRunLoopGetCurrent(), kcfdefaultmode)
    rc = io.IOHIDManagerOpen(mgr, 0)              # kIOHIDOptionsTypeNone — no seize
    if rc != 0:
        print(f"WARNING: IOHIDManagerOpen returned 0x{rc & 0xFFFFFFFF:08x} "
              f"(non-zero = not fully granted).", file=sys.stderr)
        print("If nothing prints, grant Input Monitoring to this python binary:",
              file=sys.stderr)
        print(f"  {sys.executable}", file=sys.stderr)
        print("System Settings → Privacy & Security → Input Monitoring, then re-run.",
              file=sys.stderr)

    print("=" * 72)
    print(" HID sniffer running (non-exclusive — audio & keys unaffected).")
    scope = "ALL HID devices" if args.all else f"Jabra only (vendor 0x{JABRA_VENDOR_ID:04x})"
    print(f" Scope: {scope}.  Press: mute, call answer/end, then volume +/-.")
    print(" Telephony (0x0B) Phone Mute / Hook Switch is the clean trigger target.")
    print(f" Listening for {args.seconds:.0f}s — Ctrl-C to stop early.")
    print("=" * 72, flush=True)

    deadline = time.time() + args.seconds
    try:
        while time.time() < deadline:
            cf.CFRunLoopRunInMode(kcfdefaultmode, 0.25, False)
    except KeyboardInterrupt:
        pass

    print("-" * 72)
    print(f" Total HID input values seen: {counters['events']}")
    if seen:
        pages = sorted({p for p, _, _ in seen})
        print(f" Pages with activity (filtered): "
              f"{', '.join(_USAGE_PAGE.get(p, hex(p)) for p in pages)}")
        tele = sorted({u for p, u, _ in seen if p == 0x0B})
        if tele:
            names = ", ".join(_TELEPHONY_USAGE.get(u, hex(u)) for u in tele)
            print(f" Telephony buttons usable as trigger: {names}")
    elif not args.all:
        print(" No Jabra HID activity. Re-run with --all to confirm the dongle is")
        print(" enumerating at all, and check Input Monitoring permission.")
    else:
        print(" No HID activity for ANY device — almost certainly Input Monitoring")
        print(" permission. Grant it to the python binary above and re-run.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
