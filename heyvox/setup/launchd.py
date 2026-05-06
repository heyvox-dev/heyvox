"""
launchd service management for heyvox.

Provides functions to write a launchd plist, bootstrap (start), bootout (stop),
query status, and restart the heyvox listener service. Designed for macOS LaunchAgents.

Requirements: CLI-01, PROJ-05
"""

import os
import subprocess
import sys
from pathlib import Path

from heyvox.constants import LAUNCHD_LABEL, LOG_FILE_DEFAULT


PLIST_DIR = Path.home() / "Library" / "LaunchAgents"
PLIST_PATH = PLIST_DIR / f"{LAUNCHD_LABEL}.plist"
GUI_DOMAIN = f"gui/{os.getuid()}"


def write_plist() -> Path:
    """Generate and write the launchd plist file for the heyvox service.

    Uses sys.executable so the plist always points to the current Python
    environment (avoids hardcoded paths and venv pitfalls).

    Returns:
        Path to the written plist file.
    """
    PLIST_DIR.mkdir(parents=True, exist_ok=True)

    # Build PATH: include the Python binary's directory so vox CLI is available
    python_bin_dir = str(Path(sys.executable).parent)
    env_path = f"{python_bin_dir}:/usr/local/bin:/usr/bin:/bin"

    # WorkingDirectory = the repo root so `python -m heyvox.main` resolves correctly
    working_dir = str(Path(__file__).resolve().parent.parent.parent)

    plist_content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{LAUNCHD_LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>{sys.executable}</string>
        <string>-m</string>
        <string>heyvox.main</string>
    </array>
    <key>WorkingDirectory</key>
    <string>{working_dir}</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>5</integer>
    <key>StandardOutPath</key>
    <string>{LOG_FILE_DEFAULT}</string>
    <key>StandardErrorPath</key>
    <string>{LOG_FILE_DEFAULT}</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>{env_path}</string>
    </dict>
</dict>
</plist>
"""
    PLIST_PATH.write_text(plist_content)
    return PLIST_PATH


def bootstrap() -> tuple[bool, str]:
    """Load and start the vox launchd service.

    Writes the plist if it doesn't exist. Returns (True, message) on success
    or if already running, (False, message) on error.

    Returns:
        Tuple of (success: bool, message: str).
    """
    if not PLIST_PATH.exists():
        write_plist()

    result = subprocess.run(
        ["launchctl", "bootstrap", GUI_DOMAIN, str(PLIST_PATH)],
        capture_output=True,
        text=True,
    )

    # returncode 37 = already loaded (Mach error: service already bootstrapped)
    if result.returncode == 0:
        return True, f"HeyVox service started ({LAUNCHD_LABEL})"
    elif result.returncode == 37:
        return True, "Already running"
    else:
        err = result.stderr.strip() or result.stdout.strip()
        return False, f"Failed to start: {err} (exit {result.returncode})"


def _kill_heyvox_processes() -> int:
    """Kill any running heyvox processes (main, HUD, orchestrator, kokoro-daemon).

    Returns the number of processes killed.
    """
    import signal
    import time

    patterns = ["heyvox.main", "heyvox.cli start", "heyvox.hud.overlay",
                "herald/daemon/kokoro-daemon.py",
                "herald/daemon/watcher.py", "heyvox.herald.cli"]
    killed = 0

    for pattern in patterns:
        try:
            result = subprocess.run(
                ["pgrep", "-f", pattern],
                capture_output=True, text=True,
            )
            for line in result.stdout.strip().splitlines():
                pid = int(line.strip())
                if pid == os.getpid():
                    continue  # Don't kill ourselves
                try:
                    os.kill(pid, signal.SIGTERM)
                    killed += 1
                except ProcessLookupError:
                    pass
        except (ValueError, subprocess.SubprocessError):
            continue

    if killed > 0:
        time.sleep(1)
        # Force kill any survivors
        for pattern in patterns:
            try:
                result = subprocess.run(
                    ["pgrep", "-f", pattern],
                    capture_output=True, text=True,
                )
                for line in result.stdout.strip().splitlines():
                    pid = int(line.strip())
                    if pid == os.getpid():
                        continue
                    try:
                        os.kill(pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            except (ValueError, subprocess.SubprocessError):
                continue

    return killed


def bootout() -> tuple[bool, str]:
    """Unload and stop the vox launchd service, then kill any orphan processes.

    Returns (True, message) on success or if not running,
    (False, message) on unexpected error.

    Returns:
        Tuple of (success: bool, message: str).
    """
    # Step 1: Unload from launchd (stops auto-restart before we kill processes)
    if PLIST_PATH.exists():
        result = subprocess.run(
            ["launchctl", "bootout", GUI_DOMAIN, str(PLIST_PATH)],
            capture_output=True,
            text=True,
        )
        # returncode 3 = not loaded, 5 = not loaded — both are fine
        launchd_stopped = result.returncode in (0, 3, 5)
    else:
        launchd_stopped = True

    # Step 2: Kill any remaining heyvox processes (orphans, manual starts)
    killed = _kill_heyvox_processes()

    if launchd_stopped and killed == 0:
        return True, "Not running"
    elif launchd_stopped:
        return True, f"HeyVox service stopped ({killed} processes killed)"
    else:
        err = result.stderr.strip() or result.stdout.strip()
        return False, f"launchd bootout failed: {err} (but killed {killed} processes)"


def get_status() -> dict:
    """Query launchd for the current heyvox service status.

    Parses `launchctl list com.heyvox.listener` output.

    Returns:
        dict with keys:
            loaded (bool): Whether the service is registered with launchd.
            running (bool): Whether a process is currently active (has PID).
            pid (int|None): Current PID if running, else None.
            exit_code (int|None): Last exit code if stopped, else None.
    """
    result = subprocess.run(
        ["launchctl", "list", LAUNCHD_LABEL],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        return {"loaded": False, "running": False, "pid": None, "exit_code": None}

    # Output format: PID\tExitCode\tLabel
    # "-" in PID column means not running
    lines = result.stdout.strip().splitlines()
    if not lines:
        return {"loaded": False, "running": False, "pid": None, "exit_code": None}

    # Skip header line if present
    data_line = lines[-1]
    parts = data_line.split("\t")
    if len(parts) < 3:
        return {"loaded": True, "running": False, "pid": None, "exit_code": None}

    pid_str, exit_str, _label = parts[0], parts[1], parts[2]

    pid = None
    running = False
    if pid_str != "-":
        try:
            pid = int(pid_str)
            running = True
        except ValueError:
            pass

    exit_code = None
    try:
        exit_code = int(exit_str)
    except ValueError:
        pass

    return {
        "loaded": True,
        "running": running,
        "pid": pid,
        "exit_code": exit_code,
    }


def restart() -> tuple[bool, str]:
    """Stop then start the heyvox service.

    Returns:
        Tuple of (success: bool, combined_message: str).
    """
    stop_ok, stop_msg = bootout()
    start_ok, start_msg = bootstrap()

    combined = f"Stop: {stop_msg} | Start: {start_msg}"
    return start_ok, combined
