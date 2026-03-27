"""
Recording indicator overlay for vox HUD.

Shows a floating red dot in the top-center of the screen while recording is active.
Designed to run as a separate process (killed by parent via SIGKILL on stop).

This will be decoupled from Conductor-specific logic and replaced with a
volume-modulated waveform in Phase 5 (HUD).
"""

import signal
import sys


def main():
    """Run the recording indicator overlay.

    Starts a borderless NSWindow with a red dot view at the top of the screen.
    Installs SIGTERM/SIGINT handlers for clean shutdown via NSTimer.

    The overlay detects which screen the Conductor window is on and positions
    itself there. Phase 5 will replace this with a generic screen detection approach.
    """
    from Foundation import NSObject, NSTimer
    from AppKit import (
        NSApplication, NSWindow, NSColor, NSView, NSBezierPath,
        NSWindowStyleMaskBorderless, NSScreen, NSBackingStoreBuffered,
        NSStatusWindowLevel,
    )

    app = NSApplication.sharedApplication()
    app.setActivationPolicy_(2)  # Prohibited — no dock icon

    # Show on the screen where the Conductor app window is
    from AppKit import NSWorkspace
    target_screen = NSScreen.mainScreen()  # fallback
    for running_app in NSWorkspace.sharedWorkspace().runningApplications():
        if running_app.bundleIdentifier() == "com.conductor.app":
            # Found Conductor — get its windows via CGWindowListCopyWindowInfo
            import Quartz
            window_list = Quartz.CGWindowListCopyWindowInfo(
                Quartz.kCGWindowListOptionOnScreenOnly | Quartz.kCGWindowListExcludeDesktopElements,
                Quartz.kCGNullWindowID
            )
            for win in window_list:
                if win.get("kCGWindowOwnerName") == "conductor":
                    bounds = win.get("kCGWindowBounds", {})
                    win_x = bounds.get("X", 0)
                    win_y = bounds.get("Y", 0)
                    # Find which screen contains this window
                    for s in NSScreen.screens():
                        f = s.frame()
                        # Convert Quartz coords (top-left origin) to AppKit (bottom-left)
                        appkit_y = NSScreen.screens()[0].frame().size.height - win_y
                        if (f.origin.x <= win_x <= f.origin.x + f.size.width and
                                f.origin.y <= appkit_y <= f.origin.y + f.size.height):
                            target_screen = s
                            break
                    break
            break

    screen = target_screen.frame()
    size = 18
    x = screen.origin.x + (screen.size.width - size) / 2  # center on target screen
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
