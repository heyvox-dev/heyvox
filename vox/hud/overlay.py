"""
Recording indicator overlay for vox HUD.

Shows a floating red dot in the top-center of the screen while recording is active.
Designed to run as a separate process (killed by parent via SIGKILL on stop).

Positions on NSScreen.mainScreen() — no app-specific detection needed.

This will be replaced with a volume-modulated waveform in Phase 5 (HUD).

Requirement: DECP-06
"""

import signal
import sys


def main():
    """Run the recording indicator overlay.

    Starts a borderless NSWindow with a red dot view at the top of the main screen.
    Installs SIGTERM/SIGINT handlers for clean shutdown via NSTimer.
    """
    from Foundation import NSObject, NSTimer
    from AppKit import (
        NSApplication, NSWindow, NSColor, NSView, NSBezierPath,
        NSWindowStyleMaskBorderless, NSScreen, NSBackingStoreBuffered,
        NSStatusWindowLevel,
    )

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(2)  # Prohibited — no dock icon

    # Always use the main screen — no app-specific window lookup needed
    # Requirement: DECP-06
    screen = NSScreen.mainScreen().frame()
    size = 18
    x = screen.origin.x + (screen.size.width - size) / 2  # center horizontally
    y = screen.origin.y + screen.size.height - size - 38  # just below top edge

    w = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
        ((x, y), (size, size)),
        NSWindowStyleMaskBorderless,
        NSBackingStoreBuffered,
        False,
    )
    w.setLevel_(NSStatusWindowLevel + 1)
    w.setOpaque_(False)
    w.setBackgroundColor_(NSColor.clearColor())
    w.setIgnoresMouseEvents_(True)
    # canJoinAllSpaces (1<<0) + fullScreenAuxiliary (1<<8)
    w.setCollectionBehavior_((1 << 0) | (1 << 8))

    class DotView(NSView):
        def drawRect_(self, rect):
            NSColor.redColor().setFill()
            NSBezierPath.bezierPathWithOvalInRect_(rect).fill()

    w.setContentView_(DotView.alloc().initWithFrame_(((0, 0), (size, size))))
    w.orderFrontRegardless()

    # Clean exit on SIGTERM (AppKit requires NSTimer to dispatch onto run loop)
    class Terminator(NSObject):
        def terminate_(self, timer):
            app.terminate_(None)

    t = Terminator.alloc().init()

    def handle_signal(signum, frame):
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.0, t, "terminate:", None, False
        )

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    app.run()


if __name__ == "__main__":
    main()
