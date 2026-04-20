"""
Interactive setup wizard for heyvox.

Guides the user through:
  1. Welcome banner + system deps
  2. Permission checks (Accessibility, Microphone, Screen Recording)
  3. Kokoro model download
  4. Microphone level test
  5. Config file initialization
  6. launchd service installation
  7. Herald TTS hooks
  8. Hush Chrome extension (browser media control)
  9. MCP server registration
  10. Setup summary

All heavy imports (rich, huggingface_hub, pyaudio) are deferred to inside
run_setup() to avoid load-time cost and import errors in non-setup contexts.

Requirements: CLI-02, CLI-03, CLI-04
"""

import json
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# MCP agent detection and registration
# ---------------------------------------------------------------------------

# Each entry: name, config path (relative to ~), key path in JSON, scope
_MCP_AGENTS = [
    {
        "name": "Claude Code",
        "config_path": ".claude/settings.json",
        "key": "mcpServers",
    },
    {
        "name": "Claude Desktop",
        "config_path": ".config/claude-code/mcp.json",
        "key": "mcpServers",
    },
    {
        "name": "Cursor",
        "config_path": ".cursor/mcp.json",
        "key": "mcpServers",
    },
    {
        "name": "Windsurf",
        "config_path": ".codeium/windsurf/mcp_config.json",
        "key": "mcpServers",
    },
    {
        "name": "Continue.dev",
        "config_path": ".continue/config.json",
        "key": "mcpServers",
    },
]


def _detect_mcp_agents() -> list[dict]:
    """Detect which AI coding agents are installed by checking config directories."""
    home = Path.home()
    found = []
    for agent in _MCP_AGENTS:
        config_file = home / agent["config_path"]
        # Check if the parent directory exists (agent is installed)
        if config_file.parent.exists():
            found.append({**agent, "resolved_path": config_file})
    return found


def _register_mcp_agent(agent: dict, mcp_entry: dict) -> tuple[bool, str]:
    """Register the HeyVox MCP server with a specific agent's config file.

    Returns (success, message).
    """
    config_file: Path = agent["resolved_path"]
    key = agent["key"]

    try:
        if config_file.exists():
            with open(config_file) as f:
                settings = json.load(f)
        else:
            settings = {}

        if key not in settings:
            settings[key] = {}

        # Check if already registered
        if "heyvox" in settings[key]:
            return True, f"{agent['name']}: already registered ({config_file})"

        # Remove old "vox" key if present (renamed to heyvox)
        settings[key].pop("vox", None)

        settings[key]["heyvox"] = mcp_entry

        # Claude Code: also add to allowedTools if present
        if agent["name"] == "Claude Code" and "allowedTools" in settings:
            allowed = settings["allowedTools"]
            if isinstance(allowed, list) and "heyvox" not in allowed:
                allowed.append("heyvox")

        config_file.parent.mkdir(parents=True, exist_ok=True)
        with open(config_file, "w") as f:
            json.dump(settings, f, indent=2)
            f.write("\n")

        return True, f"{agent['name']}: registered ({config_file})"

    except Exception as e:
        return False, f"{agent['name']}: {e}"


def run_setup(config) -> None:
    """Run the interactive Vox setup wizard.

    Args:
        config: HeyvoxConfig instance loaded from the active config file.
    """
    # ---------------------------------------------------------------------------
    # Lazy imports — only needed during setup
    # ---------------------------------------------------------------------------
    from rich.console import Console
    from rich.panel import Panel
    from rich.progress import Progress, SpinnerColumn, TextColumn
    from rich.live import Live
    from rich.table import Table

    from heyvox import __version__
    from heyvox.setup.permissions import (
        check_accessibility,
        check_microphone,
        check_screen_recording,
        open_permission_settings,
    )
    from heyvox.setup.launchd import write_plist, bootstrap, get_status

    console = Console()

    # ---------------------------------------------------------------------------
    # Step 1: Welcome banner
    # ---------------------------------------------------------------------------
    console.print(Panel(
        f"[bold cyan]HeyVox v{__version__}[/bold cyan]\n"
        "Voice layer for AI coding agents\n\n"
        "[dim]This wizard checks permissions, downloads the Kokoro TTS model,\n"
        "tests your microphone, and configures HeyVox as a launchd service.[/dim]",
        title="[bold]HeyVox Setup[/bold]",
        border_style="cyan",
    ))
    console.print()

    # ---------------------------------------------------------------------------
    # Step 1b: System dependency check (PortAudio)
    # ---------------------------------------------------------------------------
    import shutil
    if not shutil.which("brew"):
        console.print("  [yellow]![/yellow] Homebrew not found — install from https://brew.sh")
    else:
        import subprocess as _sp
        pa_check = _sp.run(["brew", "list", "portaudio"], capture_output=True)
        if pa_check.returncode != 0:
            console.print("  [yellow]![/yellow] PortAudio not found (required by pyaudio)")
            console.print("  [dim]Install with: brew install portaudio[/dim]")
            install_pa = console.input("  Install now? [Y/n] ").strip().lower()
            if install_pa != "n":
                _sp.run(["brew", "install", "portaudio"])
                console.print("  [green]✓[/green] PortAudio installed")
            else:
                console.print("  [dim]Skipped — pyaudio may fail without portaudio.[/dim]")
        else:
            console.print("  [green]✓[/green] PortAudio installed")
    console.print()

    # ---------------------------------------------------------------------------
    # Step 2: Permission checks
    # ---------------------------------------------------------------------------
    console.print("[bold]Step 2: Permission Checks[/bold]")
    console.print()

    permissions_to_check = [
        ("accessibility", "Accessibility", "Required for push-to-talk (Quartz event tap)"),
        ("microphone", "Microphone", "Required for wake word detection and speech recording"),
        ("screen_recording", "Screen Recording", "Required for text injection via osascript"),
    ]

    checkers = {
        "accessibility": check_accessibility,
        "microphone": check_microphone,
        "screen_recording": check_screen_recording,
    }

    all_permissions_ok = True

    for perm_key, perm_name, perm_desc in permissions_to_check:
        checker = checkers[perm_key]
        granted = checker()

        if granted:
            console.print(f"  [green]✓[/green] {perm_name}: Granted")
        else:
            all_permissions_ok = False
            console.print(f"  [red]✗[/red] {perm_name}: [red]MISSING[/red]")
            console.print(f"    [dim]{perm_desc}[/dim]")
            console.print(f"    Opening System Settings > Privacy > {perm_name}...")
            open_permission_settings(perm_key)

            # Re-check up to 3 times
            for attempt in range(1, 4):
                console.print(f"    [dim]Grant the permission in System Settings, then press Enter to re-check (attempt {attempt}/3)...[/dim]")
                try:
                    input()
                except (EOFError, KeyboardInterrupt):
                    break
                if checker():
                    console.print(f"  [green]✓[/green] {perm_name}: Granted")
                    break
                elif attempt == 3:
                    console.print(f"  [yellow]![/yellow] {perm_name}: Still missing after 3 attempts. You can continue setup and grant later.")

    console.print()

    # ---------------------------------------------------------------------------
    # Step 3: Kokoro model download
    # ---------------------------------------------------------------------------
    console.print("[bold]Step 3: Kokoro TTS Model[/bold]")

    kokoro_cache = Path.home() / ".cache" / "huggingface" / "hub" / "models--hexgrad--Kokoro-82M"
    if kokoro_cache.exists():
        console.print("  [green]✓[/green] Kokoro model already downloaded")
    else:
        console.print("  [yellow]![/yellow] Kokoro model not found (~300 MB download required)")
        console.print("  [dim]Requires: PyTorch (torch), kokoro, sounddevice[/dim]")

        download = console.input("  Download now? [y/N] ").strip().lower()
        if download == "y":
            try:
                from huggingface_hub import snapshot_download

                with Progress(
                    SpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    console=console,
                ) as progress:
                    progress.add_task("Downloading hexgrad/Kokoro-82M...", total=None)
                    snapshot_download(repo_id="hexgrad/Kokoro-82M")

                console.print("  [green]✓[/green] Kokoro model downloaded successfully")
            except ImportError:
                console.print("  [red]✗[/red] huggingface_hub not installed. Run: pip install huggingface_hub")
            except Exception as e:
                console.print(f"  [red]✗[/red] Download failed: {e}")
        else:
            console.print("  [dim]Skipped. Run `heyvox setup` again to download later.[/dim]")

    console.print()

    # ---------------------------------------------------------------------------
    # Step 4: Microphone level test
    # ---------------------------------------------------------------------------
    console.print("[bold]Step 4: Microphone Test[/bold]")
    console.print("  Recording for 2 seconds — say something...")

    try:
        import pyaudio
        import numpy as np

        pa = pyaudio.PyAudio()
        try:
            default_index = pa.get_default_input_device_info()["index"]
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=1,
                rate=16000,
                input=True,
                input_device_index=default_index,
                frames_per_buffer=1280,
            )

            chunks_to_read = int(2.0 * 16000 / 1280)  # 2 seconds
            max_level = 0

            import time
            _start_t = time.time()

            with Live(console=console, refresh_per_second=10) as live:
                for _ in range(chunks_to_read):
                    audio = np.frombuffer(
                        stream.read(1280, exception_on_overflow=False),
                        dtype=np.int16,
                    )
                    level = int(np.abs(audio).max())
                    if level > max_level:
                        max_level = level

                    # Simple bar display
                    bar_len = min(40, level // 100)
                    bar = "█" * bar_len + "░" * (40 - bar_len)
                    live.update(f"  Level: [{bar}] {level}")

            stream.stop_stream()
            stream.close()

            console.print(f"  Max detected level: {max_level}")
            if max_level > 100:
                console.print("  [green]✓[/green] Microphone is working")
            elif max_level == 0:
                console.print("  [red]✗[/red] Microphone appears silent — check permissions and device")
            else:
                console.print("  [yellow]![/yellow] Very low signal — check microphone position")

        except Exception as e:
            console.print(f"  [yellow]![/yellow] Mic test skipped: {e}")
        finally:
            pa.terminate()

    except ImportError:
        console.print("  [dim]Skipped (pyaudio or numpy not available)[/dim]")

    console.print()

    # ---------------------------------------------------------------------------
    # Step 5: Config file
    # ---------------------------------------------------------------------------
    console.print("[bold]Step 5: Configuration File[/bold]")
    from heyvox.config import CONFIG_FILE, ensure_config_dir
    if CONFIG_FILE.exists():
        console.print(f"  [green]✓[/green] Config file exists: {CONFIG_FILE}")
    else:
        create_cfg = console.input(
            f"  Config not found. Create default at {CONFIG_FILE}? [Y/n] "
        ).strip().lower()
        if create_cfg != "n":
            ensure_config_dir()
            console.print(f"  [green]✓[/green] Created: {CONFIG_FILE}")
        else:
            console.print("  [dim]Skipped — defaults will be used.[/dim]")

    console.print()

    # ---------------------------------------------------------------------------
    # Step 6: launchd service
    # ---------------------------------------------------------------------------
    console.print("[bold]Step 6: launchd Service[/bold]")
    from heyvox.setup.launchd import PLIST_PATH

    install_plist = console.input(
        "  Install HeyVox as a launchd service (starts automatically at login)? [Y/n] "
    ).strip().lower()

    if install_plist != "n":
        plist_path = write_plist()
        console.print(f"  [green]✓[/green] Plist written: {plist_path}")

        start_svc = console.input("  Start the service now? [Y/n] ").strip().lower()
        if start_svc != "n":
            success, msg = bootstrap()
            if success:
                console.print(f"  [green]✓[/green] {msg}")
            else:
                console.print(f"  [red]✗[/red] {msg}")
    else:
        console.print("  [dim]Skipped — run `heyvox start --daemon` to start the service later.[/dim]")

    console.print()

    # ---------------------------------------------------------------------------
    # Step 7: Herald hooks (TTS voice output for Claude Code)
    # ---------------------------------------------------------------------------
    console.print("[bold]Step 7: Herald TTS Hooks[/bold]")
    console.print("  Herald provides voice output — Claude speaks via <tts> blocks in responses.")
    console.print()

    herald_hooks_installed = False
    install_hooks = console.input(
        "  Install Herald hooks for Claude Code? [Y/n] "
    ).strip().lower()

    if install_hooks != "n":
        try:
            from heyvox.setup.hooks import install_herald_hooks
            results = install_herald_hooks()
            for ok, msg in results:
                console.print(f"  {'[green]✓[/green]' if ok else '[red]✗[/red]'} {msg}")
            herald_hooks_installed = any(ok for ok, _ in results)
        except Exception as e:
            console.print(f"  [red]✗[/red] Failed to install hooks: {e}")
    else:
        console.print("  [dim]Skipped — run `heyvox setup` again to install later.[/dim]")

    console.print()

    # ---------------------------------------------------------------------------
    # Step 8: Hush Chrome extension (browser media control)
    # ---------------------------------------------------------------------------
    console.print("[bold]Step 8: Hush Chrome Extension[/bold]")
    console.print("  Hush pauses/resumes browser media during recording and TTS playback.")
    console.print("  [dim]Optional — skip if you don't use browser audio (YouTube, Spotify web, etc.)[/dim]")
    console.print()

    hush_installed = False
    install_hush = console.input(
        "  Install Hush Chrome extension? [y/N] "
    ).strip().lower()

    if install_hush == "y":
        import subprocess as _sp

        # Locate the extension and host directories within the heyvox package
        from heyvox.hush import HUSH_EXTENSION, HUSH_HOME

        ext_dir = HUSH_EXTENSION
        host_script = str(Path(HUSH_HOME) / "host" / "hush_host.py")

        if not Path(ext_dir).exists():
            console.print(f"  [red]✗[/red] Extension directory not found: {ext_dir}")
        elif not Path(host_script).exists():
            console.print(f"  [red]✗[/red] Native host not found: {host_script}")
        else:
            # Step 8a: Prompt user to side-load the extension
            console.print()
            console.print("  [bold]Load the extension in Chrome:[/bold]")
            console.print(f"    1. Open Chrome → [cyan]chrome://extensions[/cyan]")
            console.print(f"    2. Enable [bold]Developer mode[/bold] (top-right toggle)")
            console.print(f"    3. Click [bold]Load unpacked[/bold] → select:")
            console.print(f"       [dim]{ext_dir}[/dim]")
            console.print(f"    4. Copy the [bold]Extension ID[/bold] (32 lowercase letters)")
            console.print()

            ext_id = ""
            for attempt in range(3):
                raw = console.input("  Paste Extension ID: ").strip().replace(" ", "")
                if len(raw) == 32 and raw.isalpha() and raw.islower():
                    ext_id = raw
                    break
                console.print("  [yellow]![/yellow] Extension IDs are exactly 32 lowercase letters. Try again.")

            if ext_id:
                # Step 8b: Install native messaging host to stable path
                from heyvox.hush import install_hush_host

                ok, msg = install_hush_host(extension_id=ext_id)
                if ok:
                    console.print(f"  [green]✓[/green] {msg}")
                    console.print("  [dim]Reload the extension in Chrome to activate.[/dim]")
                    hush_installed = True
                else:
                    console.print(f"  [red]✗[/red] {msg}")
            else:
                console.print("  [yellow]![/yellow] Skipped — could not get a valid Extension ID.")
                console.print("  [dim]Run the manual install later: bash heyvox/hush/scripts/install.sh[/dim]")
    else:
        console.print("  [dim]Skipped — browser media won't pause during recording/TTS.[/dim]")
        console.print("  [dim]Install later: heyvox setup (or bash heyvox/hush/scripts/install.sh)[/dim]")

    console.print()

    # ---------------------------------------------------------------------------
    # Step 9: Register MCP server with AI coding agents
    # ---------------------------------------------------------------------------
    console.print("[bold]Step 9: MCP Server Registration[/bold]")
    console.print("  Vox exposes voice tools to AI agents via MCP (Model Context Protocol).")
    console.print()

    mcp_entry = {
        "command": sys.executable,
        "args": ["-m", "heyvox.mcp.server"],
    }

    # Detect which agents are installed
    agents_available = _detect_mcp_agents()
    agents_registered = []

    if not agents_available:
        console.print("  [yellow]![/yellow] No supported AI coding agents detected.")
        console.print("  [dim]Supported: Claude Code, Cursor, Windsurf, Continue.dev[/dim]")
        console.print("  [dim]Install one and run `heyvox setup` again, or add manually.[/dim]")
    else:
        agent_names = [a["name"] for a in agents_available]
        console.print(f"  Detected: [bold]{', '.join(agent_names)}[/bold]")
        console.print()

        for agent in agents_available:
            register = console.input(
                f"  Register HeyVox MCP server with {agent['name']}? [Y/n] "
            ).strip().lower()

            if register != "n":
                ok, msg = _register_mcp_agent(agent, mcp_entry)
                if ok:
                    console.print(f"  [green]✓[/green] {msg}")
                    agents_registered.append(agent["name"])
                else:
                    console.print(f"  [red]✗[/red] {msg}")
            else:
                console.print(f"  [dim]Skipped {agent['name']}[/dim]")

    console.print()

    # ---------------------------------------------------------------------------
    # Step 10: Summary
    # ---------------------------------------------------------------------------
    console.print("[bold]Step 10: Setup Summary[/bold]")
    console.print()

    summary_table = Table(show_header=False, box=None, padding=(0, 2))
    summary_table.add_column("Status", style="bold")
    summary_table.add_column("Item")

    status = get_status()

    summary_table.add_row(
        "[green]✓[/green]" if all_permissions_ok else "[yellow]![/yellow]",
        "Permissions",
    )
    summary_table.add_row(
        "[green]✓[/green]" if kokoro_cache.exists() else "[yellow]![/yellow]",
        "Kokoro TTS model",
    )
    summary_table.add_row(
        "[green]✓[/green]" if CONFIG_FILE.exists() else "[yellow]![/yellow]",
        f"Config: {CONFIG_FILE}",
    )
    summary_table.add_row(
        "[green]✓[/green]" if PLIST_PATH.exists() else "[dim]-[/dim]",
        "launchd service installed",
    )
    summary_table.add_row(
        "[green]✓ Running[/green]" if status["running"] else "[dim]Stopped[/dim]",
        f"HeyVox service status (PID: {status.get('pid', '-')})",
    )
    summary_table.add_row(
        "[green]✓[/green]" if herald_hooks_installed else "[dim]-[/dim]",
        "Herald TTS hooks installed",
    )
    summary_table.add_row(
        "[green]✓[/green]" if hush_installed else "[dim]-[/dim]",
        "Hush Chrome extension (browser media pause)",
    )
    if agents_registered:
        summary_table.add_row(
            "[green]✓[/green]",
            f"MCP registered: {', '.join(agents_registered)}",
        )
    else:
        summary_table.add_row(
            "[dim]-[/dim]",
            "MCP: not registered (run `heyvox setup` to add)",
        )

    console.print(summary_table)
    console.print()
    console.print("[bold cyan]Setup complete![/bold cyan] Run [bold]heyvox status[/bold] to check service state.")
    console.print()
