"""
HUD overlay process for Vox voice layer.

Implements a frosted-glass pill window positioned at the top-right of the
main screen (avoiding the macOS notch/camera area), communicating voice
state visually.

State machine:
- idle:       compact gray pill (12x12), click-through, no content
- listening:  expanded red pill (200x28), waveform amplitude bars
- processing: expanded amber pill (200x28), "Transcribing..." label
- speaking:   expanded green pill (200x28), text snippet + Skip/Stop buttons

IPC: HUDServer receives JSON messages over /tmp/heyvox-hud.sock on a daemon
thread and dispatches state changes to the main AppKit thread via
performSelectorOnMainThread_withObject_waitUntilDone_.

Requirements: HUD-01 through HUD-08
"""

import json
import os
import signal
import sys
import time


# ---------------------------------------------------------------------------
# Layout constants
# ---------------------------------------------------------------------------

PILL_W = 100
PILL_H = 28
PILL_MARGIN_TOP = 8
PILL_MARGIN_RIGHT = 16  # Default distance from right edge of screen
ANIM_DURATION = 0.2
from heyvox.constants import HUD_POSITION_FILE as POSITION_FILE  # Persists user-dragged position
_MENU_BAR_ONLY = False  # Set by main() — when True, only show menu bar icon, no pill
_LAST_STATE = "idle"    # DEF-135: last state applied; lets the overlay-mode toggle re-apply it
_REAPPLY_STATE = None   # DEF-135: set by main() — re-applies _LAST_STATE to pill + menu bar
_PROCESSING_TIMER = None

_MENUBAR_ICON_PATH = os.path.join(os.path.dirname(__file__), "assets", "menubar.png")

# Mic-level menu bar meter — small volume-reactive bars next to the red dot
# while listening, so there's a "is it hearing me" signal even when the
# floating pill is hidden (hud_menu_bar_only mode).
_MIC_METER_BARS = 3
_MIC_METER_BAR_W = 3
_MIC_METER_GAP = 2
_MIC_METER_H = 18  # was 14 — taller bars, still comfortably inside the ~22-24pt menu bar
_MIC_METER_MIN_H = 1  # near-flat at silence (was 3 — too visible at rest)
_MIC_METER_GAIN = 1.4  # pre-curve amplification so normal speech reaches the top of the range
_MIC_METER_LOG_K = 9.0  # log-taper curve constant (log1p-based) — higher = more low-end expansion
_MIC_METER_IMG_W = _MIC_METER_BARS * _MIC_METER_BAR_W + (_MIC_METER_BARS - 1) * _MIC_METER_GAP
_MIC_METER_SMOOTHED = 0.0


def _brand_menubar_image():
    """Load the HeyVox brand glyph (bubble + caret + sparkle) as a macOS
    template image — black silhouette tinted by the system to match the
    menu bar appearance (white on dark, black on light). Source SVG is
    rendered to PNG at build/install time via rsvg-convert.
    """
    from AppKit import NSImage
    from Foundation import NSSize

    img = NSImage.alloc().initWithContentsOfFile_(_MENUBAR_ICON_PATH)
    if img is None:
        return NSImage.imageWithSystemSymbolName_accessibilityDescription_(
            "mic", "Microphone",
        )
    img.setSize_(NSSize(26, 26))
    img.setTemplate_(True)
    return img


def _mic_level_bars_image(level):
    """Render the volume-reactive bars shown next to the red dot while
    listening — 3 bars whose heights track the smoothed mic level, like a
    tiny equalizer. Template image so macOS tints it correctly for light
    and dark menu bars, same mechanism as the brand glyph.
    """
    global _MIC_METER_SMOOTHED
    import math
    from AppKit import NSBezierPath, NSColor, NSImage
    from Foundation import NSSize

    level = max(0.0, min(1.0, level * _MIC_METER_GAIN))
    # Exponential smoothing: fast attack, slow release — same shape as the
    # pill's WaveformView so the two indicators feel consistent.
    alpha = 0.6 if level > _MIC_METER_SMOOTHED else 0.15
    _MIC_METER_SMOOTHED = alpha * level + (1.0 - alpha) * _MIC_METER_SMOOTHED

    # Log-taper curve (like an audio log-pot) expands the quiet/mid range
    # (typical speech rarely hits the raw level's top end) so the bars
    # visibly swing during normal talking instead of hovering near the
    # floor; true silence (level=0) still curves to 0.
    curved = math.log1p(_MIC_METER_LOG_K * _MIC_METER_SMOOTHED) / math.log1p(_MIC_METER_LOG_K)

    # Per-bar multipliers give a mini-equalizer look instead of one solid
    # block moving in lockstep.
    multipliers = (0.55, 1.0, 0.75)
    img = NSImage.alloc().initWithSize_(NSSize(_MIC_METER_IMG_W, _MIC_METER_H))
    img.lockFocus()
    NSColor.blackColor().set()
    for i in range(_MIC_METER_BARS):
        mult = multipliers[i % len(multipliers)]
        bar_h = _MIC_METER_MIN_H + (_MIC_METER_H - _MIC_METER_MIN_H) * min(1.0, curved * mult)
        x = i * (_MIC_METER_BAR_W + _MIC_METER_GAP)
        y = (_MIC_METER_H - bar_h) / 2.0
        NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
            ((x, y), (_MIC_METER_BAR_W, bar_h)), _MIC_METER_BAR_W / 2.0, _MIC_METER_BAR_W / 2.0,
        ).fill()
    img.unlockFocus()
    img.setTemplate_(True)
    return img


def _reset_mic_meter():
    global _MIC_METER_SMOOTHED
    _MIC_METER_SMOOTHED = 0.0


def _update_menubar_meter(status_item, level):
    """Refresh the listening-state bars on every audio_level tick (~20fps,
    same as the pill's waveform) — an earlier 10fps throttle made the bars
    visibly lag behind speech, and a tiny 13x18px NSImage redraw is cheap
    enough that there's no need to cut the rate."""
    status_item.button().setImage_(_mic_level_bars_image(level))


def _brand_hud_image(size=11):
    """Brand glyph rendered white for the dark idle HUD pill.

    The menu bar image is a template (black silhouette tinted by macOS); inside
    a NSTextAttachment the template doesn't auto-tint, so we composite a
    pre-tinted copy via SourceIn over white.
    """
    from AppKit import (
        NSImage, NSColor,
        NSCompositingOperationSourceOver, NSCompositingOperationSourceIn,
        NSRectFillUsingOperation,
    )
    from Foundation import NSSize, NSMakeRect

    src = NSImage.alloc().initWithContentsOfFile_(_MENUBAR_ICON_PATH)
    if src is None:
        return None
    target = NSImage.alloc().initWithSize_(NSSize(size, size))
    target.lockFocus()
    src.drawInRect_fromRect_operation_fraction_(
        NSMakeRect(0, 0, size, size),
        NSMakeRect(0, 0, src.size().width, src.size().height),
        NSCompositingOperationSourceOver,
        1.0,
    )
    NSColor.whiteColor().set()
    NSRectFillUsingOperation(
        NSMakeRect(0, 0, size, size), NSCompositingOperationSourceIn,
    )
    target.unlockFocus()
    return target


def _idle_default_attr_string():
    """Brand glyph + ' HeyVox' as an NSAttributedString for the idle HUD label."""
    from AppKit import NSTextAttachment
    from Foundation import NSAttributedString, NSMutableAttributedString

    s = NSMutableAttributedString.alloc().init()
    icon = _brand_hud_image(11)
    if icon is not None:
        att = NSTextAttachment.alloc().init()
        att.setImage_(icon)
        s.appendAttributedString_(
            NSAttributedString.attributedStringWithAttachment_(att),
        )
        s.appendAttributedString_(NSAttributedString.alloc().initWithString_(" "))
    s.appendAttributedString_(NSAttributedString.alloc().initWithString_("HeyVox"))
    return s


# State → (r, g, b, a) overlay color (semi-transparent so frosted glass shows)
STATE_COLORS = {
    "idle":       (0.35, 0.35, 0.40, 0.65),  # Subtle gray
    "listening":  (1.0, 0.2, 0.2, 0.8),
    "processing": (1.0, 0.7, 0.0, 0.8),
    "speaking":   (0.2, 0.8, 0.3, 0.8),
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _default_position(screen_frame, pill_w, pill_h):
    """Return default (x, y) — top-right of screen, avoiding notch."""
    x = screen_frame.origin.x + screen_frame.size.width - pill_w - PILL_MARGIN_RIGHT
    y = screen_frame.origin.y + screen_frame.size.height - pill_h - PILL_MARGIN_TOP
    return x, y


def _load_position():
    """Load user-dragged position from disk. Returns (x, y) or None."""
    try:
        with open(POSITION_FILE) as f:
            data = json.load(f)
        return float(data["x"]), float(data["y"])
    except (FileNotFoundError, KeyError, ValueError, json.JSONDecodeError):
        return None


def _save_position(x, y):
    """Persist user-dragged position to disk."""
    try:
        with open(POSITION_FILE, "w") as f:
            json.dump({"x": x, "y": y}, f)
    except OSError:
        pass


def _estimate_transcription_secs(audio_secs, warm=True):
    """Return a conservative HUD-only ETA for local STT.

    Whisper/MLX does not expose streaming progress here, so this is an
    expectation based on clip duration and whether the model was already warm.
    The UI caps progress below 100% until the transcript actually arrives.
    """
    try:
        audio_secs = float(audio_secs or 0.0)
    except (TypeError, ValueError):
        audio_secs = 0.0
    if warm is None:
        warm = True
    base = 0.9 if warm else 3.5
    per_second = 0.22 if warm else 0.34
    return max(1.4, min(45.0, base + audio_secs * per_second))


def _processing_progress_snapshot(started_at, estimate_secs, now=None):
    """Return (progress, remaining_secs) for the processing HUD.

    Progress intentionally tops out at 95% until transcription completes, so the
    indicator never claims success while the blocking STT call is still running.
    """
    now = time.time() if now is None else now
    try:
        estimate_secs = float(estimate_secs or 0.0)
    except (TypeError, ValueError):
        estimate_secs = 0.0
    estimate_secs = max(0.1, estimate_secs)
    elapsed = max(0.0, now - started_at)
    progress = min(0.95, elapsed / estimate_secs)
    remaining = max(1, int(round(max(0.0, estimate_secs - elapsed))))
    return progress, remaining


def _processing_progress_label(progress, remaining_secs):
    pct = int(round(max(0.0, min(0.95, progress)) * 100))
    return f"{pct}%  ~{remaining_secs}s"


def _processing_status_title(progress):
    pct = int(round(max(0.0, min(1.0, progress)) * 100))
    return f"\U0001f7e1 {pct}%"


def _set_processing_status_progress(status_item, progress, remaining_secs):
    if status_item is None:
        return
    try:
        status_item.setLength_(64)
        btn = status_item.button()
        btn.setImage_(None)
        btn.setTitle_(_processing_status_title(progress))
        btn.setToolTip_(f"Transcribing, about {remaining_secs}s remaining")
    except Exception:
        pass


def _set_processing_progress(processing_views, progress):
    if not processing_views:
        return
    track_view, fill_view = processing_views
    track_view.setHidden_(False)
    fill_view.setHidden_(False)
    track_frame = track_view.frame()
    fill_w = max(2.0, track_frame.size.width * max(0.0, min(1.0, progress)))
    fill_view.setFrame_(((0, 0), (fill_w, track_frame.size.height)))


def _hide_processing_progress(processing_views):
    if not processing_views:
        return
    track_view, fill_view = processing_views
    track_view.setHidden_(True)
    fill_view.setHidden_(True)


def _stop_processing_progress(processing_views=None):
    global _PROCESSING_TIMER
    if _PROCESSING_TIMER is not None:
        try:
            _PROCESSING_TIMER.invalidate()
        except Exception:
            pass
        _PROCESSING_TIMER = None
    _hide_processing_progress(processing_views)


def _start_processing_progress(
    transcript_label, processing_views, estimate_secs, status_item=None,
):
    global _PROCESSING_TIMER
    from Foundation import NSTimer

    _stop_processing_progress(processing_views)
    started_at = time.time()
    try:
        estimate_secs = float(estimate_secs or 0.1)
    except (TypeError, ValueError):
        estimate_secs = 0.1
    estimate_secs = max(0.1, estimate_secs)

    def _tick(timer):
        if _LAST_STATE != "processing":
            _stop_processing_progress(processing_views)
            return
        progress, remaining = _processing_progress_snapshot(started_at, estimate_secs)
        transcript_label.setStringValue_(
            _processing_progress_label(progress, remaining)
        )
        _set_processing_progress(processing_views, progress)
        _set_processing_status_progress(status_item, progress, remaining)

    _tick(None)
    _PROCESSING_TIMER = NSTimer.scheduledTimerWithTimeInterval_repeats_block_(
        0.2, True, _tick,
    )


# ---------------------------------------------------------------------------
# Custom NSView subclasses (defined inside main() to ensure AppKit is loaded)
# ---------------------------------------------------------------------------

def _make_waveform_view_class():
    from AppKit import NSView, NSColor, NSBezierPath

    _HISTORY_SIZE = 64  # ~3.2 seconds at 20fps

    class WaveformView(NSView):
        """Scrolling waveform — mirrored amplitude history, like Voice Memos.

        Keeps a ring buffer of recent audio levels. New samples push in from
        the right, old ones scroll left. Drawn as a mirrored filled area
        around the vertical center, with a subtle gradient fade on older
        samples.
        """
        _level = 0.0
        _history = None  # Lazily initialized list of floats
        _smoothed = 0.0  # Exponentially smoothed current level

        def setLevel_(self, level):
            level = max(0.0, min(1.0, level))
            # Exponential smoothing: fast attack (0.6), slow release (0.15)
            alpha = 0.6 if level > self._smoothed else 0.15
            self._smoothed = alpha * level + (1.0 - alpha) * self._smoothed
            if self._history is None:
                self._history = [0.0] * _HISTORY_SIZE
            self._history.append(self._smoothed)
            if len(self._history) > _HISTORY_SIZE:
                self._history.pop(0)
            self.setNeedsDisplay_(True)

        def drawRect_(self, rect):
            if self._history is None:
                return

            history = self._history
            n = len(history)
            if n == 0:
                return

            w = rect.size.width
            h = rect.size.height
            ox = rect.origin.x
            oy = rect.origin.y
            cy = oy + h / 2.0  # vertical center
            step = w / max(1, n - 1)
            min_amp = h * 0.04  # minimum visible amplitude

            # Draw mirrored filled waveform
            top_path = NSBezierPath.bezierPath()
            bot_path = NSBezierPath.bezierPath()
            top_path.moveToPoint_((ox, cy))
            bot_path.moveToPoint_((ox, cy))

            for i, val in enumerate(history):
                x = ox + i * step
                amp = max(min_amp, val * (h / 2.0) * 0.9)
                top_path.lineToPoint_((x, cy + amp))
                bot_path.lineToPoint_((x, cy - amp))

            # Close paths back to center
            top_path.lineToPoint_((ox + (n - 1) * step, cy))
            top_path.lineToPoint_((ox, cy))
            bot_path.lineToPoint_((ox + (n - 1) * step, cy))
            bot_path.lineToPoint_((ox, cy))

            # Fill with white, higher opacity on recent samples
            NSColor.whiteColor().colorWithAlphaComponent_(0.85).setFill()
            top_path.fill()
            bot_path.fill()

            # Thin center line
            NSColor.whiteColor().colorWithAlphaComponent_(0.3).setStroke()
            center_line = NSBezierPath.bezierPath()
            center_line.moveToPoint_((ox, cy))
            center_line.lineToPoint_((ox + w, cy))
            center_line.setLineWidth_(0.5)
            center_line.stroke()

    return WaveformView


def _make_content_view_class():
    """Create HUDContentView — draggable background, buttons respond."""
    from AppKit import NSView
    import objc

    class HUDContentView(NSView):
        """Content view that supports dragging and click-to-menu.

        Dragging the background moves the window and saves the position.
        Clicking without drag shows the transcript dropdown menu.
        Clicking on button subviews works normally (TTS controls).

        Requirement: HUD-04
        """
        _drag_origin = None
        _drag_started = False
        _menu_callback = None  # Set from main() — callable(event)

        def mouseDown_(self, event):
            self._drag_origin = event.locationInWindow()
            self._drag_started = False

        def mouseDragged_(self, event):
            if self._drag_origin is None:
                return
            self._drag_started = True
            win = self.window()
            if win is None:
                return
            screen_loc = event.locationInWindow()
            frame = win.frame()
            dx = screen_loc.x - self._drag_origin.x
            dy = screen_loc.y - self._drag_origin.y
            new_x = frame.origin.x + dx
            new_y = frame.origin.y + dy
            win.setFrameOrigin_((new_x, new_y))

        def mouseUp_(self, event):
            if not self._drag_started and self._menu_callback:
                self._menu_callback(event)
            elif self._drag_started:
                win = self.window()
                if win:
                    _save_position(win.frame().origin.x, win.frame().origin.y)
            self._drag_origin = None
            self._drag_started = False

        def hitTest_(self, point):
            hit = objc.super(HUDContentView, self).hitTest_(point)
            if hit is self:
                return self   # Background captures mouse for dragging
            return hit        # Subviews (buttons) respond normally

    return HUDContentView


# ---------------------------------------------------------------------------
# State application
# ---------------------------------------------------------------------------

def _apply_state(
    state_str,
    window,
    content_view,
    waveform_view,
    transcript_label,
    tts_controls,
    color_overlay,
    processing_progress_views=None,
    idle_label=None,
    tts_text=None,
    status_item=None,
    update_status_menu=None,
    processing_audio_secs=None,
    processing_estimate_secs=None,
    processing_warm=True,
):
    """Apply HUD visual state on the main thread.

    Requirements: HUD-01 (pill), HUD-02 (waveform), HUD-03 (transcript),
                  HUD-04 (TTS controls), HUD-05 (colors)
    """
    from AppKit import NSAnimationContext, NSColor, NSScreen

    # DEF-135: remember the last applied state so a runtime overlay-mode toggle
    # can re-apply it through this same path instead of force-showing a blank,
    # un-painted window.
    global _LAST_STATE
    _LAST_STATE = state_str

    if state_str != "processing":
        _stop_processing_progress(processing_progress_views)

    # Update menu bar status icon + label
    _STATUS_LABELS = {
        "idle":       ("\U0001f399", ""),                # 🎙 (icon only)
        "listening":  ("\U0001f534", ""),                   # 🔴 (+ live mic-level bars)
        "processing": ("\U0001f7e1", " Trans"),            # 🟡 Trans
        "speaking":   ("\U0001f7e2", " Speak"),             # 🟢 Speak
    }
    if status_item is not None:
        icon, label = _STATUS_LABELS.get(state_str, _STATUS_LABELS["idle"])
        # When idle, check for crashed daemons and show warning in menu bar
        if state_str == "idle":
            from heyvox.constants import HERALD_ORCH_PID, KOKORO_DAEMON_PID, KOKORO_DAEMON_SOCK
            crashed = []
            for name, pid_path, sock_path in [
                ("TTS", KOKORO_DAEMON_PID, KOKORO_DAEMON_SOCK),
                ("Orch", HERALD_ORCH_PID, None),
            ]:
                has_pid = os.path.exists(pid_path)
                has_sock = os.path.exists(sock_path) if sock_path else False
                pid_alive = False
                if has_pid:
                    try:
                        pid = int(open(pid_path).read().strip())
                        os.kill(pid, 0)
                        pid_alive = True
                    except (ValueError, ProcessLookupError, PermissionError, OSError):
                        pass
                if (has_pid or has_sock) and not pid_alive:
                    crashed.append(name)
            if crashed:
                icon = "\u26a0"  # \u26a0 text-mode, no emoji overflow
                label = f" {'&'.join(crashed)} err"
        # DEF-100: held TTS count badge \u2014 surfaces hold-queue state.
        # Stays at 0 with default config (hold_queue.enabled=false), but if
        # the user opts back into hold-queue behaviour, this makes it visible.
        _held_count = 0
        try:
            from pathlib import Path as _Path
            from heyvox.constants import HERALD_HOLD_DIR
            _held_count = sum(1 for _ in _Path(HERALD_HOLD_DIR).glob("*.wav"))
        except Exception:
            pass
        # HUDSurface banner \u2014 unified read for silent-state-change detectors.
        # Picks the highest-level live record (error > warn > info); falls
        # back to the legacy DEF-101 MIC_WARN_FILE via HUDSurface compat path.
        # Patterns P-new (ux invisibility) + P-detector-without-action.
        #
        # Render policy (revised 2026-05-25 after user feedback): only the
        # level symbol appears in the menu bar title \u2014 full text lives in
        # the menu bar item's tooltip. Earlier full-text override pushed
        # other menu bar items off-screen for the full TTL window.
        _mic_warn = ""
        _banner_level = "info"
        try:
            from heyvox.hud.surface import HUDSurface
            _top = HUDSurface.top_active()
            if _top is not None:
                _mic_warn = _top["text"][:120]
                _banner_level = _top["level"]
        except Exception:
            pass
        _banner_symbol = ""
        if _mic_warn:
            _banner_symbol = {
                "error": "\u2716",   # \u2716 text-mode, fits menu bar height
                "warn": "\u26a0",    # \u26a0 text-mode (no \ufe0f emoji selector)
                "info": "\u2139",    # \u2139 text-mode
            }.get(_banner_level, "\u26a0")
        # Build menu bar title with SF Symbol-style mute indicators.
        # The banner appears as a *suffix symbol*, not a title override \u2014
        # brand glyph + state stay intact, only a small badge gets added.
        _bar_title = f"{icon}{label}"
        if _held_count > 0:
            _bar_title += f"  \U0001f4e5{_held_count}"
        if _banner_symbol:
            _bar_title += f"  {_banner_symbol}"
        # Idle branch renders brand glyph + symbol suffix; non-idle uses text.
        # The earlier "and not _mic_warn" gate forced text-mode whenever a
        # banner was active \u2014 now removed so the brand glyph survives.
        if state_str == "idle" and not crashed:
            from heyvox.constants import MIC_MUTE_FLAG as _MIC_MUTE
            _mic_muted = os.path.exists(_MIC_MUTE)
            _spk_muted = False
            try:
                from heyvox.audio.tts import get_verbosity
                _spk_muted = get_verbosity() == "skip"
            except Exception:
                pass
            from AppKit import NSImage, NSImageSymbolConfiguration, NSVariableStatusItemLength as _NSVarLen
            # Release reserved width so the brand glyph sits compactly.
            status_item.setLength_(_NSVarLen)
            btn = status_item.button()
            if _mic_muted:
                # Muted: keep SF Symbol mic.slash with red palette — clearer
                # affordance than tinting the brand glyph.
                _mic_img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                    "mic.slash", "Microphone muted",
                )
                _cfg = NSImageSymbolConfiguration.configurationWithPaletteColors_([
                    NSColor.systemRedColor(),
                    NSColor.secondaryLabelColor(),
                ])
                _mic_img = _mic_img.imageWithSymbolConfiguration_(_cfg)
            else:
                _mic_img = _brand_menubar_image()
            btn.setImage_(_mic_img)
            _idle_suffix = ""
            if _held_count > 0:
                _idle_suffix += f"  \U0001f4e5{_held_count}"
            if _spk_muted:
                _idle_suffix += " \U0001f507"
            # Mic name is hover-only (tooltip). Title carries icon + suffixes only.
            try:
                from heyvox.constants import ACTIVE_MIC_FILE
                with open(ACTIVE_MIC_FILE) as _f:
                    _active_mic_for_title = _f.read().strip()
            except Exception:
                _active_mic_for_title = ""

            def _friendly_idle(name: str) -> str:
                if not name:
                    return "None"
                n = name
                if "macbook" in n.lower() and "microphone" in n.lower():
                    return "Built-in"
                for _sfx in (" Gaming Headset", " Wireless Gaming Headset", " Microphone",
                             " USB Audio", " Audio Device"):
                    if n.endswith(_sfx):
                        n = n[: -len(_sfx)]
                        break
                return n.strip()

            _friendly = _friendly_idle(_active_mic_for_title)
            # Append the banner symbol (if any) to the idle suffix so it sits
            # next to the brand glyph rather than replacing it.
            _idle_title = _idle_suffix + (f"  {_banner_symbol}" if _banner_symbol else "")
            btn.setTitle_(_idle_title)
            # Tooltip carries the full banner text — hover to read. The mic
            # name is still shown so the user doesn't lose that affordance.
            if _mic_warn:
                btn.setToolTip_(f"{_banner_symbol} {_mic_warn}\nMic: {_friendly}")
            else:
                btn.setToolTip_(f"Mic: {_friendly}")
        else:
            # Non-idle states or crashed: use emoji text, clear image — except
            # "listening", which keeps the dot as text and adds live mic-level
            # bars as the button image (see _mic_level_bars_image).
            # Reserve exact measured width BEFORE setting title so macOS doesn't
            # reflow and hide the item when it expands (NSVariableStatusItemLength
            # can disappear when neighbours like Docker leave no room).
            _meter_w = (_MIC_METER_IMG_W + 6) if state_str == "listening" else 0
            try:
                from AppKit import NSFont, NSFontAttributeName
                from Foundation import NSString as _NSString
                _mfont = NSFont.menuBarFontOfSize_(0)
                _ns = _NSString.stringWithString_(_bar_title)
                _w = int(_ns.sizeWithAttributes_({NSFontAttributeName: _mfont}).width) + 14 + _meter_w
            except Exception:
                _w = 120 + _meter_w
            status_item.setLength_(_w)
            btn = status_item.button()
            btn.setTitle_(_bar_title)
            if state_str == "listening":
                from AppKit import NSImageRight
                _reset_mic_meter()
                btn.setImagePosition_(NSImageRight)
                btn.setImage_(_mic_level_bars_image(0.0))
            else:
                btn.setImage_(None)
        # Refresh menu on state change (updates transcript list, mute state)
        if update_status_menu is not None:
            update_status_menu()

    r, g, b, a = STATE_COLORS.get(state_str, STATE_COLORS["idle"])
    color = NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, a)
    color_overlay.setBackgroundColor_(color)
    color_overlay.setNeedsDisplay_(True)

    screen = NSScreen.mainScreen().frame()
    is_active = state_str in ("listening", "processing", "speaking")

    # In menu-bar-only mode, the pill is never shown.
    # Otherwise, show pill always — idle uses compact size, active uses active size.
    if _MENU_BAR_ONLY:
        window.orderOut_(None)
    else:
        saved = _load_position()
        if saved:
            x, y = saved
        else:
            x, y = _default_position(screen, PILL_W, PILL_H)

        if not window.isVisible():
            window.orderFrontRegardless()

        NSAnimationContext.beginGrouping()
        NSAnimationContext.currentContext().setDuration_(ANIM_DURATION)
        window.animator().setFrame_display_(((x, y), (PILL_W, PILL_H)), True)
        NSAnimationContext.endGrouping()

        content_view.setFrame_(((0, 0), (PILL_W, PILL_H)))
        color_overlay.setFrame_(((0, 0), (PILL_W, PILL_H)))

        ve = window.contentView()
        if ve and ve.layer():
            ve.layer().setCornerRadius_(PILL_H / 2)
        if color_overlay.layer():
            color_overlay.layer().setCornerRadius_(PILL_H / 2)

    # Show/hide idle label
    if idle_label is not None:
        idle_label.setHidden_(is_active)
        # Show temporary status text (e.g. "Sent to Claude") then revert
        if not is_active and tts_text:
            idle_label.setStringValue_(tts_text)
            # Schedule revert to default label after 3 seconds
            from Foundation import NSTimer
            def _revert_label(timer):
                idle_label.setAttributedStringValue_(_idle_default_attr_string())
            NSTimer.scheduledTimerWithTimeInterval_repeats_block_(3.0, False, _revert_label)
        elif not is_active:
            idle_label.setAttributedStringValue_(_idle_default_attr_string())

    if not is_active:
        # Idle: show label, hide active elements
        waveform_view.setHidden_(True)
        transcript_label.setHidden_(True)
        skip_btn, stop_btn = tts_controls
        skip_btn.setHidden_(True)
        stop_btn.setHidden_(True)
        return

    # Waveform (visible only when listening)
    wf_visible = state_str == "listening"
    waveform_view.setHidden_(not wf_visible)

    # Transcript label
    label_visible = state_str in ("processing", "speaking")
    transcript_label.setHidden_(not label_visible)
    if state_str == "processing":
        transcript_label.setFrame_(((4, 10), (PILL_W - 8, 14)))
        estimate_secs = processing_estimate_secs
        if estimate_secs is None:
            estimate_secs = _estimate_transcription_secs(
                processing_audio_secs, warm=processing_warm,
            )
        _start_processing_progress(
            transcript_label,
            processing_progress_views,
            estimate_secs,
            status_item,
        )
    elif state_str == "speaking" and tts_text:
        transcript_label.setFrame_(((4, 1), (PILL_W - 48, PILL_H - 2)))
        snippet = tts_text[:40] + "..." if len(tts_text) > 40 else tts_text
        transcript_label.setStringValue_(snippet)

    # TTS controls
    skip_btn, stop_btn = tts_controls
    tts_visible = state_str == "speaking"
    skip_btn.setHidden_(not tts_visible)
    stop_btn.setHidden_(not tts_visible)

    # Clickable in all states
    window.setIgnoresMouseEvents_(False)


# ---------------------------------------------------------------------------
# NSObject dispatcher for thread-safe UI updates
# ---------------------------------------------------------------------------

def _make_dispatcher_class(
    window,
    content_view,
    waveform_view,
    transcript_label,
    tts_controls,
    color_overlay,
    processing_progress_views=None,
    idle_label=None,
    status_item=None,
    update_status_menu=None,
):
    """Build a _Dispatcher NSObject that applies incoming IPC messages."""
    from Foundation import NSObject

    class _Dispatcher(NSObject):
        """Receives messages from the HUD socket server on the main thread.

        Called via performSelectorOnMainThread_withObject_waitUntilDone_.
        All AppKit mutations happen here, safely on the main thread.

        Requirement: HUD-08
        """

        def applyMessage_(self, msg_dict):
            msg_type = msg_dict.get("type", "")

            if msg_type == "state":
                state = msg_dict.get("state", "idle")
                text = msg_dict.get("text")
                _apply_state(
                    state, window, content_view,
                    waveform_view, transcript_label, tts_controls, color_overlay,
                    processing_progress_views=processing_progress_views,
                    idle_label=idle_label, tts_text=text,
                    status_item=status_item,
                    update_status_menu=update_status_menu,
                    processing_audio_secs=msg_dict.get("audio_secs"),
                    processing_estimate_secs=msg_dict.get("estimate_secs"),
                    processing_warm=msg_dict.get("warm", True),
                )

            elif msg_type == "audio_level":
                level = msg_dict.get("level", 0.0)
                waveform_view.setLevel_(level)
                if status_item is not None and _LAST_STATE == "listening":
                    _update_menubar_meter(status_item, level)

            elif msg_type == "transcript":
                _set_processing_progress(processing_progress_views, 1.0)
                _set_processing_status_progress(status_item, 1.0, 0)
                _stop_processing_progress(processing_progress_views)
                text = msg_dict.get("text", "")
                transcript_label.setStringValue_(text)
                transcript_label.setHidden_(False)

            elif msg_type == "tts_start":
                text = msg_dict.get("text", "")
                _apply_state(
                    "speaking", window, content_view,
                    waveform_view, transcript_label, tts_controls, color_overlay,
                    processing_progress_views=processing_progress_views,
                    idle_label=idle_label, tts_text=text,
                    status_item=status_item, update_status_menu=update_status_menu,
                )

            elif msg_type == "tts_end":
                _apply_state(
                    "idle", window, content_view,
                    waveform_view, transcript_label, tts_controls, color_overlay,
                    processing_progress_views=processing_progress_views,
                    idle_label=idle_label,
                    status_item=status_item, update_status_menu=update_status_menu,
                )

            elif msg_type == "queue_update":
                pass  # v1: ignore; future: show badge count

            elif msg_type == "error":
                # DEF-136: senders (device_manager, recording) put the text in
                # 'text'; the old code read 'message' and logged an empty string
                # on every mic-error event (46 blank lines observed). Read 'text'
                # first, 'message' as fallback.
                _err = msg_dict.get("text") or msg_dict.get("message") or ""
                print(f"[HUD] Error from client: {_err}", file=sys.stderr)

    return _Dispatcher


# ---------------------------------------------------------------------------
# TTS button action handler
# ---------------------------------------------------------------------------

def _make_tts_action_class():
    from Foundation import NSObject

    class _TTSActionHandler(NSObject):
        """Writes TTS control commands to the command file."""

        def skipTTS_(self, sender):
            _write_tts_cmd("skip")

        def stopTTS_(self, sender):
            _write_tts_cmd("stop")

    return _TTSActionHandler


def _write_tts_cmd(cmd: str) -> None:
    """Write a TTS command to the command file (same IPC as CLI heyvox skip/stop)."""
    try:
        # Import inside handler to avoid top-level vox import failure
        # when running overlay.py standalone without full vox package.
        from heyvox.constants import TTS_CMD_FILE
        cmd_path = TTS_CMD_FILE
    except ImportError:
        _t = os.environ.get("TMPDIR", "/tmp").rstrip("/")
        cmd_path = f"{_t}/heyvox-tts-cmd"
    try:
        tmp_path = cmd_path + ".tmp"
        with open(tmp_path, "w") as f:
            f.write(cmd)
        os.rename(tmp_path, cmd_path)
    except OSError as e:
        print(f"[HUD] Failed to write TTS command '{cmd}': {e}", file=sys.stderr)


# ---------------------------------------------------------------------------
# Transcript dropdown menu
# ---------------------------------------------------------------------------

def _make_menu_action_class():
    """NSObject handler for transcript menu item actions."""
    from Foundation import NSObject
    from AppKit import NSPasteboard, NSPasteboardTypeString

    from heyvox.constants import HERALD_MUTE_FLAG, MIC_MUTE_FLAG
    _TTS_MUTE_FLAGS = [HERALD_MUTE_FLAG]

    class _MenuActionHandler(NSObject):
        def copyTranscript_(self, sender):
            text = sender.representedObject()
            if text:
                pb = NSPasteboard.generalPasteboard()
                pb.clearContents()
                pb.setString_forType_(text, NSPasteboardTypeString)

        def toggleMute_(self, sender):
            from heyvox.audio.tts import is_muted, set_muted
            currently_muted = is_muted()
            set_muted(not currently_muted)
            sender.setState_(0 if currently_muted else 1)

        def _refreshMenuBarIcon(self):
            """Update the menu bar icon to reflect current mic/speaker mute state."""
            try:
                from AppKit import NSImage, NSColor, NSImageSymbolConfiguration
                si = getattr(self.__class__, '_status_item_ref', None)
                if si is None:
                    return
                btn = si.button()
                mic_muted = os.path.exists(MIC_MUTE_FLAG)
                spk_muted = False
                try:
                    from heyvox.audio.tts import get_verbosity
                    spk_muted = get_verbosity() == "skip"
                except Exception:
                    pass
                mic_symbol = "mic.slash" if mic_muted else "mic"
                mic_img = NSImage.imageWithSystemSymbolName_accessibilityDescription_(
                    mic_symbol, "Microphone muted" if mic_muted else "Microphone",
                )
                if mic_muted:
                    # Red slash, dimmed mic body
                    cfg = NSImageSymbolConfiguration.configurationWithPaletteColors_([
                        NSColor.systemRedColor(),
                        NSColor.secondaryLabelColor(),
                    ])
                    mic_img = mic_img.imageWithSymbolConfiguration_(cfg)
                btn.setImage_(mic_img)
                btn.setTitle_(" \U0001f507" if spk_muted else "")
            except Exception:
                pass

        def toggleMicMute_(self, sender):
            """Toggle mic mute (pauses wake word detection)."""
            try:
                from pathlib import Path
                flag = Path(MIC_MUTE_FLAG)
                if flag.exists():
                    flag.unlink()
                else:
                    flag.touch()
            except Exception:
                pass
            self._refreshMenuBarIcon()

        def toggleMuteVerbosity_(self, sender):
            """Toggle between muted (skip) and full verbosity."""
            try:
                from heyvox.audio.tts import get_verbosity, set_verbosity
                from heyvox.config import update_config
                currently_muted = get_verbosity() == "skip"
                new_level = "full" if currently_muted else "skip"
                set_verbosity(new_level)
                update_config(**{"tts.verbosity": new_level})
            except Exception:
                pass
            self._refreshMenuBarIcon()

        def setVerbosity_(self, sender):
            """Set verbosity to the level stored in the menu item's representedObject.

            Delegates entirely to set_verbosity() which handles file flags,
            in-memory state, and cross-process sync.
            """
            try:
                level = sender.representedObject()
                if level:
                    from heyvox.audio.tts import set_verbosity
                    set_verbosity(level)
                    # Persist to config so it survives restarts
                    from heyvox.config import update_config
                    update_config(**{"tts.verbosity": level})
            except Exception:
                pass

        def setTTSStyle_(self, sender):
            """Set TTS style. Persists to config for cross-session consistency."""
            try:
                style = sender.representedObject()
                if style:
                    from heyvox.audio.tts import set_tts_style
                    set_tts_style(style)
            except Exception:
                pass

        def setTTSLanguages_(self, sender):
            """Set TTS languages allowlist. 'auto' or comma-list like 'en-us,de'."""
            try:
                val = sender.representedObject()
                if not val:
                    return
                from heyvox.config import update_config
                if val == "auto":
                    update_config(**{"tts.languages": "auto"})
                else:
                    items = [s.strip() for s in val.split(",") if s.strip()]
                    update_config(**{"tts.languages": items})
                if update_status_menu is not None:
                    update_status_menu()
            except Exception:
                pass

        def setTTSVoiceEN_(self, sender):
            """Set Kokoro (English) voice override. Empty string = mood-mapping."""
            try:
                voice = sender.representedObject()
                from heyvox.config import update_config
                update_config(**{"tts.voice_override": voice or None})
                if update_status_menu is not None:
                    update_status_menu()
            except Exception:
                pass

        def setTTSVoiceDE_(self, sender):
            """Set Qwen3 (German) voice override. Empty string = mood-mapping."""
            try:
                voice = sender.representedObject()
                from heyvox.config import update_config
                update_config(**{"tts.qwen_voice_override": voice or None})
                if update_status_menu is not None:
                    update_status_menu()
            except Exception:
                pass

        def switchMic_(self, sender):
            """Write mic switch request file for main.py to pick up (atomic)."""
            device_name = sender.representedObject()
            if device_name:
                try:
                    from heyvox.constants import MIC_SWITCH_REQUEST_FILE
                    tmp_path = MIC_SWITCH_REQUEST_FILE + ".tmp"
                    with open(tmp_path, "w") as f:
                        f.write(device_name)
                    os.rename(tmp_path, MIC_SWITCH_REQUEST_FILE)
                except Exception:
                    pass

        def switchOutput_(self, sender):
            """Switch macOS system default output device via CoreAudio."""
            device_id = sender.representedObject()
            if device_id is not None:
                try:
                    from heyvox.audio.output import set_default_output_device
                    set_default_output_device(device_id)
                except Exception:
                    pass

        def openLog_(self, sender):
            import subprocess
            try:
                from heyvox.constants import LOG_FILE_DEFAULT
                subprocess.run(["open", "-a", "Console", LOG_FILE_DEFAULT])
            except Exception:
                pass

        def openConfig_(self, sender):
            import subprocess
            try:
                from heyvox.config import CONFIG_FILE
                if CONFIG_FILE.exists():
                    subprocess.run(["open", str(CONFIG_FILE)])
            except Exception:
                pass

        def openHelp_(self, sender):
            import webbrowser
            webbrowser.open("https://heyvox.dev")

        def telemetryToggle_(self, sender):
            """Flip telemetry on or off and persist to config.

            Also starts or stops the background sender thread in the running
            daemon so the change takes effect immediately, not just on restart.
            """
            try:
                from heyvox.telemetry.consent import is_enabled, enable, disable
                from heyvox.telemetry.sender import (
                    start_background as _tm_start,
                    stop_background as _tm_stop,
                )
                if is_enabled():
                    disable()
                    _tm_stop(timeout=1.0)
                else:
                    enable()
                    _tm_start()
            except Exception as exc:
                try:
                    from AppKit import NSAlert
                    a = NSAlert.alloc().init()
                    a.setMessageText_("Telemetry toggle failed")
                    a.setInformativeText_(str(exc))
                    a.runModal()
                except Exception:
                    pass

        def telemetryShowSent_(self, sender):
            """Open the telemetry documentation in the system browser."""
            import webbrowser
            # docs/telemetry.md ships on the heyvox.dev landing site too.
            webbrowser.open("https://heyvox.dev/telemetry.html")

        def telemetryResetId_(self, sender):
            """Generate a fresh anonymous ID and show it in an alert."""
            try:
                from heyvox.telemetry.consent import reset_anon_id
                new_id = reset_anon_id()
                from AppKit import NSAlert
                a = NSAlert.alloc().init()
                a.setMessageText_("New anonymous ID")
                a.setInformativeText_(new_id)
                a.runModal()
            except Exception as exc:
                try:
                    from AppKit import NSAlert
                    a = NSAlert.alloc().init()
                    a.setMessageText_("Reset failed")
                    a.setInformativeText_(str(exc))
                    a.runModal()
                except Exception:
                    pass

        def reportIssue_(self, sender):
            """Open the Report Issue dialog → build bundle → open GitHub Issue."""
            try:
                from heyvox.reporting.dialog import prompt_for_report
                from heyvox.reporting.bundle import build_bundle
                from heyvox.reporting.text_report import run_bugreport
                from heyvox.reporting.issue import (
                    build_issue_url,
                    open_in_browser,
                    reveal_in_finder,
                )
            except Exception as exc:
                # Surface the failure to the user via a fallback alert.
                try:
                    from AppKit import NSAlert
                    a = NSAlert.alloc().init()
                    a.setMessageText_("Report Issue unavailable")
                    a.setInformativeText_(f"Could not load reporting module: {exc}")
                    a.runModal()
                except Exception:
                    pass
                return

            opts = prompt_for_report()
            if opts is None:
                return  # Cancelled

            try:
                zip_path = build_bundle(opts)
            except Exception as exc:
                try:
                    from AppKit import NSAlert
                    a = NSAlert.alloc().init()
                    a.setMessageText_("Bundle build failed")
                    a.setInformativeText_(str(exc))
                    a.runModal()
                except Exception:
                    pass
                return

            body = run_bugreport(opts.comment)
            first_line = (opts.comment.splitlines() or [""])[0]
            title = "[Bug] " + (first_line[:80] if first_line else "")
            url = build_issue_url(title, body, bundle_path=zip_path)
            open_in_browser(url)
            reveal_in_finder(zip_path)

        def toggleOverlay_(self, sender):
            """Toggle the floating pill overlay on/off at runtime. Persists to config."""
            global _MENU_BAR_ONLY
            _MENU_BAR_ONLY = not _MENU_BAR_ONLY
            sender.setState_(0 if _MENU_BAR_ONLY else 1)  # checkmark when pill shown
            # DEF-135: re-apply the current state through the canonical path so
            # the window is hidden (menu-bar-only) or shown *and painted* (pill).
            # The old code force-showed an un-painted window, surfacing an empty
            # frosted box until the next state message arrived.
            if _REAPPLY_STATE is not None:
                _REAPPLY_STATE()
            # Persist to config so it survives restarts
            try:
                from heyvox.config import update_config
                update_config(hud_menu_bar_only=_MENU_BAR_ONLY)
            except Exception:
                pass

        def restartHeyVox_(self, sender):
            """Signal heyvox.main to exit non-zero so launchd respawns it (DEF-071).

            Previous approach (Popen relaunch from the HUD) was unreliable:
            DEF-066 bit us via a dead cwd; DEF-071 hit a silent spawn failure
            that left HeyVox fully dead with no respawn since clean SIGTERM
            exits 0 and the plist uses KeepAlive: SuccessfulExit=false.

            New approach: send SIGUSR2 → main's signal handler does
            os._exit(42). Non-zero exit triggers launchd's respawn, which
            reads WorkingDirectory and env fresh from the plist — the HUD
            is no longer in the process-supervision business.
            """
            import subprocess
            import time
            import os
            import signal as _sig

            from heyvox.constants import HEYVOX_PID_FILE
            pid_file = HEYVOX_PID_FILE
            old_pid = 0
            delivered = False
            try:
                with open(pid_file) as f:
                    old_pid = int(f.read().strip())
                os.kill(old_pid, _sig.SIGUSR2)
                delivered = True
            except (FileNotFoundError, ValueError, ProcessLookupError):
                # No PID file or process already dead — use pkill as fallback.
                result = subprocess.run(
                    ["pkill", "-USR2", "-f", "heyvox.main"],
                    capture_output=True,
                )
                delivered = result.returncode == 0

            # Best-effort: wait briefly for the process to exit so launchd
            # reliably sees the non-zero code. If it won't exit, SIGKILL —
            # launchd treats a signal-induced kill as non-success as well.
            if delivered and old_pid:
                for _ in range(30):  # up to 3s
                    time.sleep(0.1)
                    try:
                        os.kill(old_pid, 0)
                    except ProcessLookupError:
                        break
                else:
                    try:
                        os.kill(old_pid, _sig.SIGKILL)
                    except ProcessLookupError:
                        pass

            # Quit this overlay; launchd will respawn heyvox.main, which
            # spawns a fresh overlay on startup.
            from AppKit import NSApplication
            NSApplication.sharedApplication().terminate_(None)

        def quitHeyVox_(self, sender):
            """Send SIGTERM to parent heyvox.main process, then quit overlay."""
            import os
            import signal as _sig
            try:
                from heyvox.constants import HEYVOX_PID_FILE
                with open(HEYVOX_PID_FILE) as f:
                    pid = int(f.read().strip())
                os.kill(pid, _sig.SIGTERM)
            except (FileNotFoundError, ValueError, ProcessLookupError):
                import subprocess
                subprocess.run(["pkill", "-f", "heyvox.main"], capture_output=True)
            from AppKit import NSApplication
            NSApplication.sharedApplication().terminate_(None)

        def drainHeldQueue_(self, sender):
            """DEF-100: drain one held TTS message from the cross-workspace hold queue.

            Touches the orchestrator's play-next flag so the next held WAV plays
            regardless of workspace mismatch. Useful when running parallel
            Conductor sessions where TTS from background workspaces gets parked.
            """
            from pathlib import Path
            from heyvox.constants import HERALD_PLAY_NEXT
            try:
                Path(HERALD_PLAY_NEXT).touch()
            except OSError:
                pass

    return _MenuActionHandler


def _build_transcript_menu(handler):
    """Build a compact NSMenu for the HeyVox menu bar icon.

    Layout (Option B — minimal + settings gear):
    1. Status summary line (mic · verbosity · queue)
    2. Recent transcripts (last 3, click to copy)
    3. Mute TTS toggle
    4. Settings submenu (Verbosity, Microphone, Overlay, Voice Cmds, Status)
    5. Restart / Quit

    Args:
        handler: An instance of _MenuActionHandler for action targets.
    """
    from AppKit import NSMenu, NSMenuItem, NSFont, NSAttributedString, NSColor
    from AppKit import NSFontAttributeName, NSForegroundColorAttributeName
    from Foundation import NSDictionary
    import glob as _glob

    menu = NSMenu.alloc().init()
    menu.setAutoenablesItems_(False)
    menu.setMinimumWidth_(200)


    _font = NSFont.systemFontOfSize_(13)
    _font_small = NSFont.systemFontOfSize_(12)
    _font_bold = NSFont.boldSystemFontOfSize_(12)
    _dimmed = NSColor.secondaryLabelColor()

    def _styled(item, title=None):
        t = title if title else item.title()
        attrs = NSDictionary.dictionaryWithObject_forKey_(_font, "NSFont")
        item.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(t, attrs))
        return item

    def _dimmed_item(title):
        attrs = NSDictionary.dictionaryWithObjects_forKeys_(
            [_font_small, _dimmed],
            [NSFontAttributeName, NSForegroundColorAttributeName],
        )
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
        item.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(title, attrs))
        item.setEnabled_(False)
        return item

    # -- Gather state --
    from heyvox.constants import (
        HERALD_QUEUE_DIR, HERALD_HOLD_DIR, HERALD_MUTE_FLAG,
        HERALD_ORCH_PID, KOKORO_DAEMON_SOCK, KOKORO_DAEMON_PID, HUD_SOCKET_PATH,
    )
    queue_count = len(_glob.glob(HERALD_QUEUE_DIR + "/*.wav"))
    hold_count = len(_glob.glob(HERALD_HOLD_DIR + "/*.wav"))
    try:
        from heyvox.audio.tts import is_muted as _tts_is_muted
        _is_muted = _tts_is_muted()
    except Exception:
        _is_muted = os.path.exists(HERALD_MUTE_FLAG)

    try:
        from heyvox.audio.tts import get_verbosity
        current_verbosity = get_verbosity()
    except Exception:
        current_verbosity = "full"

    _active_mic = ""
    try:
        from heyvox.constants import ACTIVE_MIC_FILE
        with open(ACTIVE_MIC_FILE) as _mf:
            _active_mic = _mf.read().strip()
    except Exception:
        pass

    # Friendly mic name for display
    def _friendly_mic(name):
        if not name:
            return "None"
        n = name
        # "MacBook Pro Microphone" → "Built-in"
        if "macbook" in n.lower() and "microphone" in n.lower():
            return "Built-in"
        # Strip generic suffixes
        for suffix in [" Gaming Headset", " Wireless Gaming Headset", " Microphone",
                       " USB Audio", " Audio Device"]:
            if n.endswith(suffix):
                n = n[:-len(suffix)]
                break
        return n.strip()

    _mic_short = _friendly_mic(_active_mic)

    # ── Section 1: Microphone (top-level with switch submenu) ──
    try:
        import pyaudio as _pa_mod
        _scan = _pa_mod.PyAudio()
        _input_devices = []
        for _di in range(_scan.get_device_count()):
            try:
                _d = _scan.get_device_info_by_index(_di)
                if _d['maxInputChannels'] > 0:
                    _input_devices.append(_d['name'])
            except Exception:
                pass
        _scan.terminate()
    except Exception:
        _input_devices = []

    if not _input_devices and _active_mic:
        _input_devices = [_active_mic]

    from heyvox.constants import MIC_MUTE_FLAG as _MIC_MUTE_FLAG
    _is_mic_muted = os.path.exists(_MIC_MUTE_FLAG)
    if _is_mic_muted:
        # Inline SF Symbol (mic.slash) as text attachment — same position as emoji, no layout shift
        from AppKit import NSTextAttachment, NSImage, NSImageSymbolConfiguration, NSMutableAttributedString
        _att = NSTextAttachment.alloc().init()
        _mic_img = NSImage.imageWithSystemSymbolName_accessibilityDescription_("mic.slash", "Muted")
        _color_cfg = NSImageSymbolConfiguration.configurationWithPaletteColors_([
            NSColor.systemRedColor(), NSColor.secondaryLabelColor(),
        ])
        _mic_img = _mic_img.imageWithSymbolConfiguration_(_color_cfg)
        _mic_img.setSize_((14, 14))
        _att.setImage_(_mic_img)
        _icon_str = NSAttributedString.attributedStringWithAttachment_(_att)
        _text_attrs = NSDictionary.dictionaryWithObject_forKey_(_font, NSFontAttributeName)
        _text_str = NSAttributedString.alloc().initWithString_attributes_(" Mic: Muted", _text_attrs)
        _full = NSMutableAttributedString.alloc().init()
        _full.appendAttributedString_(_icon_str)
        _full.appendAttributedString_(_text_str)
        mic_parent = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Mic: Muted", None, "")
        mic_parent.setAttributedTitle_(_full)
    else:
        mic_parent = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            f"\U0001f399 Mic: {_mic_short}", None, "",
        )
        _styled(mic_parent)
    mic_sub = NSMenu.alloc().init()
    mic_sub.setAutoenablesItems_(False)

    # D-13: append voice_isolation_mode suffix to each entry, reading from the
    # active mic profile registry. Reload config fresh on each rebuild so the
    # submenu never shows stale state after a config.yaml edit (RESEARCH Pitfall 5).
    from heyvox.hud.menu_bar_title import vi_suffix_for_device
    from heyvox.config import load_config
    try:
        _menu_config = load_config()
    except Exception:
        _menu_config = None

    for _dev_name in _input_devices:
        _is_active = _dev_name == _active_mic
        _vi = vi_suffix_for_device(_dev_name, _menu_config) if _menu_config else ""
        _mic_title = f"{_friendly_mic(_dev_name)}{_vi}"
        _mic_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            _mic_title, "switchMic:", "",
        )
        _mic_item.setTarget_(handler)
        _mic_item.setRepresentedObject_(_dev_name)
        _mic_item.setEnabled_(not _is_active)
        if _is_active:
            _mic_item.setState_(1)
        _styled(_mic_item)
        mic_sub.addItem_(_mic_item)

    if not _input_devices:
        _no_mic = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "No devices found", None, "",
        )
        _no_mic.setEnabled_(False)
        _styled(_no_mic)
        mic_sub.addItem_(_no_mic)

    # Mic mute toggle at bottom of mic submenu
    from heyvox.constants import MIC_MUTE_FLAG as _MIC_MUTE_FLAG
    _is_mic_muted = os.path.exists(_MIC_MUTE_FLAG)
    mic_sub.addItem_(NSMenuItem.separatorItem())
    _mic_mute_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Mute Microphone", "toggleMicMute:", "",
    )
    _mic_mute_item.setTarget_(handler)
    _mic_mute_item.setEnabled_(True)
    if _is_mic_muted:
        _mic_mute_item.setState_(1)
    _styled(_mic_mute_item)
    mic_sub.addItem_(_mic_mute_item)

    mic_parent.setSubmenu_(mic_sub)
    menu.addItem_(mic_parent)

    # ── Section 1b: Speaker / Output device (system default switch) ──
    try:
        from heyvox.audio.output import list_output_devices, friendly_output_name
        _output_devices = list_output_devices()
    except Exception:
        _output_devices = []

    if _output_devices:
        _active_output = next((d for d in _output_devices if d.is_default), None)
        _output_short = friendly_output_name(_active_output.name) if _active_output else "System Default"

        _is_verbosity_muted_for_label = current_verbosity == "skip"
        _output_label = "\U0001f507 Output: Muted" if _is_verbosity_muted_for_label else f"\U0001f508 Output: {_output_short}"
        output_parent = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            _output_label, None, "",
        )
        _styled(output_parent)
        output_sub = NSMenu.alloc().init()
        output_sub.setAutoenablesItems_(False)

        for _out_dev in _output_devices:
            _out_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                friendly_output_name(_out_dev.name), "switchOutput:", "",
            )
            _out_item.setTarget_(handler)
            _out_item.setRepresentedObject_(_out_dev.device_id)
            _out_item.setEnabled_(not _out_dev.is_default)
            if _out_dev.is_default:
                _out_item.setState_(1)
            _styled(_out_item)
            output_sub.addItem_(_out_item)

        # Speaker mute toggle at bottom of output submenu
        _is_verbosity_muted = current_verbosity == "skip"
        output_sub.addItem_(NSMenuItem.separatorItem())
        _spk_mute_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            "Mute Speaker", "toggleMuteVerbosity:", "",
        )
        _spk_mute_item.setTarget_(handler)
        _spk_mute_item.setEnabled_(True)
        if _is_verbosity_muted:
            _spk_mute_item.setState_(1)
        _styled(_spk_mute_item)
        output_sub.addItem_(_spk_mute_item)

        output_parent.setSubmenu_(output_sub)
        menu.addItem_(output_parent)

    # ── Section 2: Speech style ──
    from heyvox.audio.tts import get_tts_style
    current_style = get_tts_style()
    _STYLE_LABELS = {
        "detailed": "Detailed", "concise": "Concise",
        "technical": "Technical", "casual": "Casual",
        "briefing": "Briefing",
    }
    style_display = _STYLE_LABELS.get(current_style, "Detailed")
    voice_parent = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        f"\U0001f4ac Style: {style_display}", None, "",
    )
    _styled(voice_parent)
    voice_sub = NSMenu.alloc().init()
    voice_sub.setAutoenablesItems_(False)

    # -- Style --
    for style_key, style_desc in [
        ("detailed", "Detailed"),
        ("concise", "Concise"),
        ("technical", "Technical"),
        ("casual", "Casual"),
        ("briefing", "Briefing"),
    ]:
        s_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            f"  {style_desc}", "setTTSStyle:", "",
        )
        s_item.setTarget_(handler)
        s_item.setRepresentedObject_(style_key)
        s_item.setEnabled_(True)
        if style_key == current_style:
            s_item.setState_(1)
        _styled(s_item)
        voice_sub.addItem_(s_item)

    voice_parent.setSubmenu_(voice_sub)
    menu.addItem_(voice_parent)

    # ── TTS Languages submenu ──
    try:
        from heyvox.config import load_config as _load_cfg
        _cfg = _load_cfg()
        _cfg_langs = getattr(_cfg.tts, "languages", "auto")
        _cfg_voice_en = getattr(_cfg.tts, "voice_override", None)
        _cfg_voice_de = getattr(_cfg.tts, "qwen_voice_override", None)
    except Exception:
        _cfg_langs = "auto"
        _cfg_voice_en = None
        _cfg_voice_de = None

    if isinstance(_cfg_langs, list):
        _cfg_lang_key = ",".join(_cfg_langs)
        _cfg_lang_label = " + ".join(_cfg_langs).upper()
    else:
        _cfg_lang_key = "auto"
        _cfg_lang_label = "Auto"

    lang_parent = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        f"\U0001f30d Languages: {_cfg_lang_label}", None, "",
    )
    _styled(lang_parent)
    lang_sub = NSMenu.alloc().init()
    lang_sub.setAutoenablesItems_(False)
    for key, label in [
        ("auto",       "  Auto (detect + route)"),
        ("en-us",      "  English only"),
        ("en-us,de",   "  English + German"),
        ("de",         "  German only"),
    ]:
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            label, "setTTSLanguages:", "",
        )
        item.setTarget_(handler)
        item.setRepresentedObject_(key)
        item.setEnabled_(True)
        if key == _cfg_lang_key:
            item.setState_(1)
        _styled(item)
        lang_sub.addItem_(item)
    lang_parent.setSubmenu_(lang_sub)
    menu.addItem_(lang_parent)

    # ── Voice (EN) submenu — Kokoro ──
    _en_voices = [
        ("",            "  Auto (mood-based)"),
        ("af_sarah",    "  Sarah (neutral)"),
        ("af_heart",    "  Heart (warm)"),
        ("af_nova",     "  Nova (alert)"),
        ("af_sky",      "  Sky (thoughtful)"),
        ("af_bella",    "  Bella"),
        ("af_nicole",   "  Nicole"),
        ("af_jessica",  "  Jessica"),
        ("af_river",    "  River"),
        ("af_kore",     "  Kore"),
        ("bf_emma",     "  Emma (British)"),
        ("bf_alice",    "  Alice (British)"),
    ]
    _en_current = _cfg_voice_en or ""
    _en_display = next((n.strip() for v, n in _en_voices if v == _en_current), "Auto")
    en_parent = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        f"\U0001f5e3️ Voice (EN): {_en_display}", None, "",
    )
    _styled(en_parent)
    en_sub = NSMenu.alloc().init()
    en_sub.setAutoenablesItems_(False)
    for v, label in _en_voices:
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            label, "setTTSVoiceEN:", "",
        )
        item.setTarget_(handler)
        item.setRepresentedObject_(v)
        item.setEnabled_(True)
        if v == _en_current:
            item.setState_(1)
        _styled(item)
        en_sub.addItem_(item)
    en_parent.setSubmenu_(en_sub)
    menu.addItem_(en_parent)

    # ── Voice (DE) submenu — Qwen3 ──
    _de_voices = [
        ("",          "  Auto (mood-based)"),
        ("Serena",    "  Serena (neutral)"),
        ("Vivian",    "  Vivian (cheerful)"),
        ("Aura",      "  Aura (alert)"),
        ("Aria",      "  Aria (thoughtful)"),
        ("Chelsie",   "  Chelsie"),
        ("Ethan",     "  Ethan ♂"),
        ("Aidan",     "  Aidan ♂"),
        ("Davis",     "  Davis ♂"),
        ("Leo",       "  Leo ♂"),
    ]
    _de_current = _cfg_voice_de or ""
    _de_display = next((n.strip() for v, n in _de_voices if v == _de_current), "Auto")
    de_parent = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        f"\U0001f5e3️ Voice (DE): {_de_display}", None, "",
    )
    _styled(de_parent)
    de_sub = NSMenu.alloc().init()
    de_sub.setAutoenablesItems_(False)
    for v, label in _de_voices:
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
            label, "setTTSVoiceDE:", "",
        )
        item.setTarget_(handler)
        item.setRepresentedObject_(v)
        item.setEnabled_(True)
        if v == _de_current:
            item.setState_(1)
        _styled(item)
        de_sub.addItem_(item)
    de_parent.setSubmenu_(de_sub)
    menu.addItem_(de_parent)

    menu.addItem_(NSMenuItem.separatorItem())

    # ── Section 3: Recent transcripts (last 3, click to copy) ──
    try:
        from heyvox.history import load as _load_history
        entries = _load_history(limit=3)
    except Exception:
        entries = []

    if entries:
        for entry in entries:
            text = entry.get("text", "")
            ts = entry.get("ts", "?")
            time_part = ts[-8:-3] if len(ts) >= 8 else ts
            display = text[:30] + "\u2026" if len(text) > 30 else text
            title = f"  {time_part}  {display}"
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                title, "copyTranscript:", "",
            )
            item.setTarget_(handler)
            item.setRepresentedObject_(text)
            item.setToolTip_(text)
            item.setEnabled_(True)
            _styled(item)
            menu.addItem_(item)
        menu.addItem_(NSMenuItem.separatorItem())

    # ── Section 3: Settings submenu ──
    settings_parent = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Settings", None, "",
    )
    _styled(settings_parent)
    settings_sub = NSMenu.alloc().init()
    settings_sub.setAutoenablesItems_(False)

    # 4a: Show Overlay toggle
    overlay_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Show Overlay", "toggleOverlay:", "",
    )
    overlay_item.setTarget_(handler)
    overlay_item.setEnabled_(True)
    if not _MENU_BAR_ONLY:
        overlay_item.setState_(1)
    _styled(overlay_item)
    settings_sub.addItem_(overlay_item)

    settings_sub.addItem_(NSMenuItem.separatorItem())

    # 4d: Voice Commands reference
    cmds_parent = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Voice Commands", None, "",
    )
    _styled(cmds_parent)
    cmds_sub = NSMenu.alloc().init()
    cmds_sub.setAutoenablesItems_(False)

    def _cmd_item(phrase, desc):
        title = f"  {phrase}  \u2014  {desc}"
        attrs = NSDictionary.dictionaryWithObjects_forKeys_(
            [_font_small, NSColor.labelColor()],
            [NSFontAttributeName, NSForegroundColorAttributeName],
        )
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
        item.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(title, attrs))
        item.setEnabled_(True)
        return item

    def _section_header(title):
        attrs = NSDictionary.dictionaryWithObjects_forKeys_(
            [_font_bold, _dimmed],
            [NSFontAttributeName, NSForegroundColorAttributeName],
        )
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
        item.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(title, attrs))
        item.setEnabled_(True)
        return item

    cmds_sub.addItem_(_section_header("Playback"))
    cmds_sub.addItem_(_cmd_item('"skip"', "Skip current"))
    cmds_sub.addItem_(_cmd_item('"stop"', "Stop all"))
    cmds_sub.addItem_(_cmd_item('"mute"', "Toggle mute"))
    cmds_sub.addItem_(_cmd_item('"replay"', "Replay last"))
    cmds_sub.addItem_(NSMenuItem.separatorItem())
    cmds_sub.addItem_(_section_header("TTS Playback"))
    cmds_sub.addItem_(_cmd_item('"be quiet"', "First sentence only"))
    cmds_sub.addItem_(_cmd_item('"speak normally"', "Speak all"))
    cmds_sub.addItem_(_cmd_item('"shut up"', "Mute"))
    cmds_parent.setSubmenu_(cmds_sub)
    settings_sub.addItem_(cmds_parent)

    settings_sub.addItem_(NSMenuItem.separatorItem())

    # 4e: Status (daemons + queue)
    def _pid_alive(pidfile):
        try:
            with open(pidfile) as _f:
                pid = int(_f.read().strip())
            os.kill(pid, 0)
            return True
        except Exception:
            return False

    def _daemon_state(pid_path, sock_path=None):
        """Return 'running', 'idle', or 'error' for an on-demand daemon.

        'error' = PID file or socket exists but process is dead (crashed).
        'idle'  = no PID file, no socket — daemon simply hasn't started yet.
        'running' = PID alive and (if sock_path given) socket exists.
        """
        has_pid = os.path.exists(pid_path)
        has_sock = os.path.exists(sock_path) if sock_path else False
        pid_alive = _pid_alive(pid_path)

        if pid_alive and (not sock_path or has_sock):
            return "running"
        if has_pid or has_sock:
            # Stale PID or socket — daemon crashed
            return "error"
        return "idle"

    orch_state = _daemon_state(HERALD_ORCH_PID)
    kokoro_state = _daemon_state(KOKORO_DAEMON_PID, KOKORO_DAEMON_SOCK)
    kokoro_ok = kokoro_state == "running"
    hud_ok = os.path.exists(HUD_SOCKET_PATH)

    # Ping Kokoro daemon for engine info
    kokoro_engine = None
    if kokoro_ok:
        try:
            import socket as _sock
            with _sock.socket(_sock.AF_UNIX, _sock.SOCK_STREAM) as _s:
                _s.settimeout(1.0)
                _s.connect(KOKORO_DAEMON_SOCK)
                _s.sendall(b'{"action":"ping"}')
                _s.shutdown(_sock.SHUT_WR)
                _resp = b""
                while True:
                    _chunk = _s.recv(4096)
                    if not _chunk:
                        break
                    _resp += _chunk
                import json as _json
                kokoro_engine = _json.loads(_resp).get("engine")
        except Exception:
            pass

    def _status_label(name, state):
        """Build a status menu item from daemon state ('running'/'idle'/'error')."""
        if state == "running":
            icon = "\U0001f7e2"
            label = "running"
        elif state == "error":
            icon = "\U0001f534"
            label = "crashed"
        else:
            icon = "\u26aa"
            label = "idle"
        title = f"  {icon} {name}: {label}"
        attrs = NSDictionary.dictionaryWithObjects_forKeys_(
            [_font_small, NSColor.labelColor()],
            [NSFontAttributeName, NSForegroundColorAttributeName],
        )
        item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, None, "")
        item.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(title, attrs))
        item.setEnabled_(True)
        return item

    settings_sub.addItem_(_status_label("Orchestrator", orch_state))

    # Kokoro TTS — show engine with warning if on ONNX (CPU fallback)
    if kokoro_ok and kokoro_engine:
        if kokoro_engine == "mlx":
            kokoro_title = "  \U0001f7e2 Kokoro TTS: Metal GPU"
        else:
            kokoro_title = "  \u26a0\ufe0f Kokoro TTS: CPU fallback (slow)"
        _kokoro_attrs = NSDictionary.dictionaryWithObjects_forKeys_(
            [_font_small, NSColor.labelColor()],
            [NSFontAttributeName, NSForegroundColorAttributeName],
        )
        _kokoro_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(kokoro_title, None, "")
        _kokoro_item.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(kokoro_title, _kokoro_attrs))
        _kokoro_item.setEnabled_(True)
        settings_sub.addItem_(_kokoro_item)
    else:
        settings_sub.addItem_(_status_label("Kokoro TTS", kokoro_state))

    settings_sub.addItem_(_status_label("HUD", "running" if hud_ok else "idle"))

    if queue_count > 0 or hold_count > 0:
        q_title = f"  Queue: {queue_count} queued, {hold_count} held"
        settings_sub.addItem_(_dimmed_item(q_title))

    settings_sub.addItem_(NSMenuItem.separatorItem())

    # 4f: Help, Log, Config
    help_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Help", "openHelp:", "",
    )
    help_item.setTarget_(handler)
    help_item.setEnabled_(True)
    _styled(help_item)
    settings_sub.addItem_(help_item)

    log_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Open Log", "openLog:", "",
    )
    log_item.setTarget_(handler)
    log_item.setEnabled_(True)
    _styled(log_item)
    settings_sub.addItem_(log_item)

    # "Report Issue…" — bundles logs + diagnostics, opens pre-filled GitHub Issue.
    report_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Report Issue…", "reportIssue:", "",
    )
    report_item.setTarget_(handler)
    report_item.setEnabled_(True)
    _styled(report_item)
    settings_sub.addItem_(report_item)

    # "Telemetry" submenu — opt-in anonymous usage signals.
    try:
        from heyvox.telemetry.consent import is_enabled as _tm_is_enabled
        _tm_on = _tm_is_enabled()
    except Exception:
        _tm_on = False

    tm_parent = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        f"Telemetry: {'On' if _tm_on else 'Off'}", None, "",
    )
    _styled(tm_parent)
    tm_sub = NSMenu.alloc().init()

    tm_toggle = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Disable telemetry" if _tm_on else "Enable telemetry",
        "telemetryToggle:", "",
    )
    tm_toggle.setTarget_(handler)
    tm_toggle.setEnabled_(True)
    _styled(tm_toggle)
    tm_sub.addItem_(tm_toggle)

    tm_sub.addItem_(NSMenuItem.separatorItem())

    tm_what = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "What's being sent…", "telemetryShowSent:", "",
    )
    tm_what.setTarget_(handler)
    tm_what.setEnabled_(True)
    _styled(tm_what)
    tm_sub.addItem_(tm_what)

    tm_reset = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Reset anonymous ID", "telemetryResetId:", "",
    )
    tm_reset.setTarget_(handler)
    tm_reset.setEnabled_(True)
    _styled(tm_reset)
    tm_sub.addItem_(tm_reset)

    tm_parent.setSubmenu_(tm_sub)
    settings_sub.addItem_(tm_parent)

    # DEF-100: drain held TTS messages from cross-workspace hold queue.
    # One tap drains one held WAV regardless of which workspace it belongs to.
    drain_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Drain held messages", "drainHeldQueue:", "",
    )
    drain_item.setTarget_(handler)
    drain_item.setEnabled_(True)
    _styled(drain_item)
    settings_sub.addItem_(drain_item)

    settings_parent.setSubmenu_(settings_sub)
    menu.addItem_(settings_parent)

    menu.addItem_(NSMenuItem.separatorItem())

    # ── Section 5: Restart / Quit ──
    restart_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        "Restart", "restartHeyVox:", "",
    )
    restart_item.setTarget_(handler)
    restart_item.setEnabled_(True)
    _styled(restart_item)
    menu.addItem_(restart_item)

    try:
        from heyvox import __version__
        ver = __version__
    except Exception:
        ver = "0.1.0"
    quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
        f"Quit HeyVox v{ver}", "quitHeyVox:", "",
    )
    quit_item.setTarget_(handler)
    quit_item.setEnabled_(True)
    _styled(quit_item)
    menu.addItem_(quit_item)

    return menu


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def main(menu_bar_only: bool = False):
    """Launch the HUD overlay NSApplication.

    Builds the frosted-glass pill window, starts the HUDServer on a daemon
    thread, installs SIGTERM/SIGINT handlers, and runs the AppKit event loop.

    Args:
        menu_bar_only: If True, only show the menu bar status icon (no floating pill).

    Requirements: HUD-01 through HUD-08
    """
    # ---- AppKit imports (lazy — must be inside main() for standalone use) ----
    from AppKit import (
        NSApplication, NSWindow, NSColor, NSView,
        NSWindowStyleMaskBorderless, NSScreen, NSBackingStoreBuffered,
        NSStatusWindowLevel, NSVisualEffectView,
        NSTextField, NSButton,
        NSTextAlignmentCenter, NSWindowCollectionBehaviorCanJoinAllSpaces,
        NSWindowCollectionBehaviorFullScreenAuxiliary,
        NSWindowCollectionBehaviorStationary,
        NSWindowCollectionBehaviorIgnoresCycle,
    )
    from Foundation import NSObject, NSTimer

    try:
        from AppKit import NSVisualEffectMaterialHUDWindow as HUD_MATERIAL
    except ImportError:
        HUD_MATERIAL = 23  # Raw enum value, stable since macOS 10.11

    from heyvox.hud.ipc import HUDServer, DEFAULT_SOCKET_PATH

    global _MENU_BAR_ONLY
    _MENU_BAR_ONLY = menu_bar_only

    # ---- Application setup ----
    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(2)  # NSApplicationActivationPolicyProhibited — no dock icon

    # ---- Screen layout ----
    screen = NSScreen.mainScreen().frame()
    saved = _load_position()
    if saved:
        x, y = saved
    else:
        x, y = _default_position(screen, PILL_W, PILL_H)

    # ---- NSWindow (borderless, status level, transparent) ----
    # Starts hidden (idle) — shown on first active state
    window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        ((x, y), (PILL_W, PILL_H)),
        NSWindowStyleMaskBorderless,
        NSBackingStoreBuffered,
        False,
    )
    window.setLevel_(NSStatusWindowLevel + 1)
    window.setOpaque_(False)
    window.setBackgroundColor_(NSColor.clearColor())
    window.setIgnoresMouseEvents_(True)  # Click-through by default (idle state)

    # All Spaces + fullscreen apps (HUD-07)
    window.setCollectionBehavior_(
        NSWindowCollectionBehaviorCanJoinAllSpaces |
        NSWindowCollectionBehaviorFullScreenAuxiliary |
        NSWindowCollectionBehaviorStationary |
        NSWindowCollectionBehaviorIgnoresCycle
    )

    # ---- Frosted glass (NSVisualEffectView as content view) — HUD-06 ----
    ve = NSVisualEffectView.alloc().initWithFrame_(((0, 0), (PILL_W, PILL_H)))
    ve.setMaterial_(HUD_MATERIAL)
    ve.setBlendingMode_(0)   # NSVisualEffectBlendingModeBehindWindow
    ve.setState_(1)          # NSVisualEffectStateActive
    ve.setWantsLayer_(True)
    ve.layer().setCornerRadius_(PILL_H / 2)  # pill shape (HUD-06)
    ve.layer().setMasksToBounds_(True)
    window.setContentView_(ve)

    # ---- Color overlay (semi-transparent tint for state colors) ----
    WaveformView = _make_waveform_view_class()
    HUDContentView = _make_content_view_class()

    color_overlay = NSView.alloc().initWithFrame_(((0, 0), (PILL_W, PILL_H)))
    r, g, b, a = STATE_COLORS["idle"]
    color_overlay.setWantsLayer_(True)
    color_overlay.layer().setCornerRadius_(PILL_H / 2)
    color_overlay.layer().setMasksToBounds_(True)
    idle_color = NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, 0.3)
    color_overlay.setBackgroundColor_(idle_color)
    ve.addSubview_(color_overlay)

    # ---- HUDContentView (selective hit-testing for mixed click-through) ----
    content_view = HUDContentView.alloc().initWithFrame_(((0, 0), (PILL_W, PILL_H)))
    ve.addSubview_(content_view)

    # ---- Waveform view — HUD-02 ----
    # Sized for active state — hidden when idle
    wf_margin = 4
    wf_w = PILL_W - wf_margin * 2 - 40  # leave room for buttons
    wf_h = PILL_H - 4
    wf_x = wf_margin
    wf_y = (PILL_H - wf_h) / 2
    waveform_view = WaveformView.alloc().initWithFrame_(((wf_x, wf_y), (wf_w, wf_h)))
    waveform_view.setHidden_(True)
    content_view.addSubview_(waveform_view)

    # ---- Transcript label — HUD-03 ----
    label_margin = 4
    label_w = PILL_W - label_margin * 2 - 40
    label_h = PILL_H - 2
    label_y = (PILL_H - label_h) / 2
    transcript_label = NSTextField.alloc().initWithFrame_(
        ((label_margin, label_y), (label_w, label_h))
    )
    transcript_label.setEditable_(False)
    transcript_label.setSelectable_(False)
    transcript_label.setDrawsBackground_(False)
    transcript_label.setBezeled_(False)
    transcript_label.setTextColor_(NSColor.whiteColor())
    transcript_label.setFont_(
        __import__("AppKit", fromlist=["NSFont"]).NSFont.boldSystemFontOfSize_(11)
    )
    transcript_label.setAlignment_(NSTextAlignmentCenter)
    transcript_label.setStringValue_("")
    transcript_label.setHidden_(True)
    content_view.addSubview_(transcript_label)

    # ---- Processing progress bar (estimated; hidden outside STT) ----
    progress_track = NSView.alloc().initWithFrame_(((8, 5), (PILL_W - 16, 3)))
    progress_track.setWantsLayer_(True)
    progress_track.layer().setCornerRadius_(1.5)
    progress_track.layer().setMasksToBounds_(True)
    progress_track.setBackgroundColor_(
        NSColor.whiteColor().colorWithAlphaComponent_(0.25)
    )
    progress_track.setHidden_(True)
    progress_fill = NSView.alloc().initWithFrame_(((0, 0), (2, 3)))
    progress_fill.setWantsLayer_(True)
    progress_fill.layer().setCornerRadius_(1.5)
    progress_fill.layer().setMasksToBounds_(True)
    progress_fill.setBackgroundColor_(
        NSColor.whiteColor().colorWithAlphaComponent_(0.9)
    )
    progress_fill.setHidden_(True)
    progress_track.addSubview_(progress_fill)
    content_view.addSubview_(progress_track)
    processing_progress_views = (progress_track, progress_fill)

    # ---- Idle label (brand glyph + "HeyVox" centered in idle pill) ----
    NSFont = __import__("AppKit", fromlist=["NSFont"]).NSFont
    idle_label_h = 18
    idle_label_y = (PILL_H - idle_label_h) / 2
    idle_label = NSTextField.alloc().initWithFrame_(
        ((0, idle_label_y), (PILL_W, idle_label_h))
    )
    idle_label.setEditable_(False)
    idle_label.setSelectable_(False)
    idle_label.setDrawsBackground_(False)
    idle_label.setBezeled_(False)
    idle_label.setTextColor_(NSColor.whiteColor())
    idle_label.setFont_(NSFont.boldSystemFontOfSize_(11))
    idle_label.setAlignment_(NSTextAlignmentCenter)
    idle_label.cell().setWraps_(False)
    idle_label.cell().setScrollable_(False)
    idle_label.setAttributedStringValue_(_idle_default_attr_string())
    idle_label.setHidden_(False)
    content_view.addSubview_(idle_label)

    # ---- TTS control buttons — HUD-04 ----
    TTSActionHandler = _make_tts_action_class()
    tts_handler = TTSActionHandler.alloc().init()

    btn_w = 22
    btn_h = 14
    btn_y = (PILL_H - btn_h) / 2
    btn_gap = 1
    btn_margin_right = 4
    stop_x = PILL_W - btn_margin_right - btn_w
    skip_x = stop_x - btn_gap - btn_w

    skip_btn = NSButton.alloc().initWithFrame_(((skip_x, btn_y), (btn_w, btn_h)))
    skip_btn.setTitle_("Skip")
    skip_btn.setBezelStyle_(0)   # NSBezelStyleSmallSquare / bezel-less
    skip_btn.setBordered_(False)
    skip_btn.setFont_(
        __import__("AppKit", fromlist=["NSFont"]).NSFont.systemFontOfSize_(7)
    )
    skip_btn.setTarget_(tts_handler)
    skip_btn.setAction_("skipTTS:")
    skip_btn.setHidden_(True)

    stop_btn = NSButton.alloc().initWithFrame_(((stop_x, btn_y), (btn_w, btn_h)))
    stop_btn.setTitle_("Stop")
    stop_btn.setBezelStyle_(0)
    stop_btn.setBordered_(False)
    stop_btn.setFont_(
        __import__("AppKit", fromlist=["NSFont"]).NSFont.systemFontOfSize_(7)
    )
    stop_btn.setTarget_(tts_handler)
    stop_btn.setAction_("stopTTS:")
    stop_btn.setHidden_(True)

    content_view.addSubview_(skip_btn)
    content_view.addSubview_(stop_btn)

    tts_controls = (skip_btn, stop_btn)

    # ---- Menu bar status item (lives next to Bluetooth/WiFi icons) ----
    from AppKit import NSStatusBar, NSVariableStatusItemLength
    status_bar = NSStatusBar.systemStatusBar()
    status_item = status_bar.statusItemWithLength_(NSVariableStatusItemLength)
    # DEF-134: persist the menu bar item's position across launches. Without an
    # autosave name macOS re-places the item arbitrarily on every start — so it
    # randomly landed in front (visible) or in the notch/clutter zone (hidden),
    # which read as "the recording indicator doesn't show up". With it, the
    # user's ⌘-drag position sticks. (Does not override notch clipping on a
    # full menu bar — that's a macOS limit, not ours.)
    try:
        status_item.setAutosaveName_("com.heyvox.menubar")
    except Exception:
        pass
    status_button = status_item.button()

    # State icons for menu bar — using Unicode text rendered as the icon
    _STATUS_ICONS = {
        "idle":       "\U0001f399",     # 🎙 mic
        "listening":  "\U0001f534",     # 🔴 red circle
        "processing": "\U0001f7e1",     # 🟡 yellow circle
        "speaking":   "\U0001f7e2",     # 🟢 green circle
    }
    # Initial state: HeyVox brand glyph (template, auto-tinted by macOS)
    status_button.setImage_(_brand_menubar_image())
    status_button.setTitle_("")
    status_button.setToolTip_("Mic: (initializing)")

    MenuActionHandler = _make_menu_action_class()
    MenuActionHandler._status_item_ref = status_item
    menu_handler = MenuActionHandler.alloc().init()

    def _update_status_menu():
        """Rebuild and assign menu to status item (called on state change)."""
        menu = _build_transcript_menu(menu_handler)
        menu.setDelegate_(_menu_delegate)
        status_item.setMenu_(menu)

    def _rebuild_menu_contents(menu):
        """Rebuild menu items in-place (called by delegate on every open)."""
        menu.removeAllItems()
        fresh = _build_transcript_menu(menu_handler)
        for i in range(fresh.numberOfItems()):
            item = fresh.itemAtIndex_(0)
            fresh.removeItemAtIndex_(0)
            menu.addItem_(item)

    MenuDelegateClass = type("MenuDelegate", (NSObject,), {
        "menuNeedsUpdate_": lambda self, m: _rebuild_menu_contents(m),
    })
    _menu_delegate = MenuDelegateClass.alloc().init()

    _update_status_menu()

    # Also keep pill dropdown for floating window (click on pill during recording)
    def _show_dropdown(event):
        menu = _build_transcript_menu(menu_handler)
        loc_in_view = content_view.convertPoint_fromView_(
            event.locationInWindow(), None,
        )
        menu.popUpMenuPositioningItem_atLocation_inView_(
            None, loc_in_view, content_view,
        )

    content_view._menu_callback = _show_dropdown

    # ---- Dispatcher (thread-safe UI updates) ----
    DispatcherClass = _make_dispatcher_class(
        window, content_view, waveform_view, transcript_label, tts_controls, color_overlay,
        processing_progress_views=processing_progress_views,
        idle_label=idle_label,
        status_item=status_item, update_status_menu=_update_status_menu,
    )
    dispatcher = DispatcherClass.alloc().init()

    # DEF-135: expose a hook so the menu-bar overlay toggle can re-apply the
    # current state through the canonical dispatcher path. The menu action runs
    # on the main thread, so applyMessage_ can be called directly here.
    global _REAPPLY_STATE
    def _reapply_current_state():
        dispatcher.applyMessage_({"type": "state", "state": _LAST_STATE})
    _REAPPLY_STATE = _reapply_current_state

    # ---- HUD IPC server ----
    def on_message(msg: dict) -> None:
        """Called on background socket thread — dispatch to main thread."""
        dispatcher.performSelectorOnMainThread_withObject_waitUntilDone_(
            "applyMessage:", msg, False
        )

    hud_server = HUDServer(path=DEFAULT_SOCKET_PATH, on_message=on_message)
    hud_server.start()

    # ---- Show idle state on startup ----
    _apply_state(
        "idle", window, content_view, waveform_view,
        transcript_label, tts_controls, color_overlay,
        processing_progress_views=processing_progress_views,
        idle_label=idle_label,
        status_item=status_item, update_status_menu=_update_status_menu,
    )

    # ---- Clean shutdown: remove status item so menu bar icon disappears ----
    import atexit

    def _cleanup_status_item():
        try:
            NSStatusBar.systemStatusBar().removeStatusItem_(status_item)
        except Exception:
            pass

    atexit.register(_cleanup_status_item)

    # ---- SIGTERM / SIGINT handler ----
    # Two-pronged: (1) immediately remove menu bar icon from signal context,
    # (2) schedule clean shutdown via NSTimer for run loop cleanup.
    class Terminator(NSObject):
        def terminate_(self, timer):
            hud_server.shutdown()
            app.terminate_(None)

    terminator = Terminator.alloc().init()

    def handle_signal(signum, frame):
        _cleanup_status_item()
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.0, terminator, "terminate:", None, False
        )

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    # ---- Run loop ----
    app.run()


if __name__ == "__main__":
    import sys as _sys
    main(menu_bar_only="--menu-bar-only" in _sys.argv)
