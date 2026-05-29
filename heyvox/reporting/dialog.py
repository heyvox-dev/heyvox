"""Cocoa dialog for the menu's "Report Issue…" item.

Builds an ``NSAlert`` with an accessory view containing a comment textarea
and a column of include-checkboxes. Runs modal. Returns
:class:`heyvox.reporting.bundle.BundleOptions` on Submit, ``None`` on Cancel.

This module only imports PyObjC when called — keeps the rest of the
reporting module importable from headless contexts (CLI, tests, CI).
"""

from __future__ import annotations

from typing import Optional

from heyvox.reporting.bundle import BundleOptions


_CHECKBOX_OPTIONS = [
    # (attr_name, label, default_on)
    ("include_logs", "Logs (heyvox.log, herald, hush)", True),
    ("include_config", "Config (paths redacted)", True),
    ("include_system_info", "System info (macOS, Mac model, version)", True),
    ("include_mic_diag", "Mic diagnostics (active mic, output devices)", True),
    ("include_counters", "Signal counters (USER_EFFORT, NEAR_MISS, …)", True),
    ("include_transcripts", "Recent transcripts (private — off by default)", False),
]


def prompt_for_report() -> Optional[BundleOptions]:
    """Show the Report Issue dialog. Returns options on Submit, None on Cancel.

    Must be called on the main thread (PyObjC requirement).
    """
    from AppKit import (
        NSAlert,
        NSAlertFirstButtonReturn,
        NSBezelBorder,
        NSButton,
        NSColor,
        NSFont,
        NSMakeRect,
        NSScrollView,
        NSSwitchButton,
        NSTextField,
        NSTextView,
        NSView,
    )
    from Foundation import NSMakeSize

    # ── Layout constants ───────────────────────────────────────────────
    width = 460
    pad = 12
    text_h = 110
    cb_h = 22
    cb_count = len(_CHECKBOX_OPTIONS)

    # Total accessory height: comment area + checkbox column + paddings
    total_h = (
        pad
        + text_h
        + pad
        + (cb_h * cb_count)
        + pad
    )

    accessory = NSView.alloc().initWithFrame_(NSMakeRect(0, 0, width, total_h))

    # ── Multiline comment ─────────────────────────────────────────────
    # Anchored at top, leaving room below for checkboxes.
    text_y = total_h - pad - text_h
    scroll = NSScrollView.alloc().initWithFrame_(
        NSMakeRect(pad, text_y, width - 2 * pad, text_h)
    )
    scroll.setHasVerticalScroller_(True)
    scroll.setBorderType_(NSBezelBorder)

    text_view = NSTextView.alloc().initWithFrame_(
        NSMakeRect(0, 0, width - 2 * pad, text_h)
    )
    text_view.setMinSize_(NSMakeSize(0, text_h))
    text_view.setMaxSize_(NSMakeSize(1e7, 1e7))
    text_view.setVerticallyResizable_(True)
    text_view.setHorizontallyResizable_(False)
    text_view.setFont_(NSFont.systemFontOfSize_(13))
    try:
        text_view.setPlaceholderString_(
            "Describe what happened — what did you expect, what happened instead?"
        )
    except Exception:
        # setPlaceholderString_ is recent-macOS; ignore if unavailable.
        pass
    scroll.setDocumentView_(text_view)
    accessory.addSubview_(scroll)

    # ── Checkbox column (top-down, stacked under the textarea) ─────────
    checkboxes: dict[str, "NSButton"] = {}
    cb_y_top = text_y - pad - cb_h  # first checkbox sits just below textarea

    for i, (attr, label, default_on) in enumerate(_CHECKBOX_OPTIONS):
        y = cb_y_top - (i * cb_h)
        btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(pad, y, width - 2 * pad, cb_h)
        )
        btn.setButtonType_(NSSwitchButton)
        btn.setTitle_(label)
        btn.setState_(1 if default_on else 0)
        accessory.addSubview_(btn)
        checkboxes[attr] = btn

    # ── Alert assembly ────────────────────────────────────────────────
    alert = NSAlert.alloc().init()
    alert.setMessageText_("Report a HeyVox issue")
    alert.setInformativeText_(
        "We'll build a zip in ~/Downloads and open a pre-filled GitHub Issue "
        "in your browser. Drag the zip into the comment box before submitting."
    )
    alert.addButtonWithTitle_("Submit")
    alert.addButtonWithTitle_("Cancel")
    alert.setAccessoryView_(accessory)

    response = alert.runModal()
    if response != NSAlertFirstButtonReturn:
        return None

    comment = text_view.string() or ""

    opts = BundleOptions(comment=str(comment))
    for attr, btn in checkboxes.items():
        setattr(opts, attr, bool(btn.state()))
    return opts
