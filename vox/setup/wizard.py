"""
Interactive setup wizard for vox.

Guides the user through:
  1. Welcome banner
  2. Permission checks (Accessibility, Microphone, Screen Recording)
  3. Kokoro model download
  4. Microphone level test
  5. Config file initialization
  6. launchd service installation
  7. MCP auto-approve (writes to ~/.claude/settings.json)
  8. Setup summary

All heavy imports (rich, huggingface_hub, pyaudio) are deferred to inside
run_setup() to avoid load-time cost and import errors in non-setup contexts.

Requirements: CLI-02, CLI-03, CLI-04
"""

import json
import sys
from pathlib import Path


def run_setup(config) -> None:
    """Run the interactive Vox setup wizard.

    Args:
        config: VoxConfig instance loaded from the active config file.
    """
    # ---------------------------------------------------------------------------
    # Lazy imports — only needed during setup
    # ---------------------------------------------------------------------------
    from rich.console import Console
    from rich.panel import Panel
    from rich.progress import Progress, SpinnerColumn, TextColumn
    from rich.live import Live
    from rich.table import Table

    from vox import __version__
    from vox.setup.permissions import (
        check_accessibility,
        check_microphone,
        check_screen_recording,
        open_permission_settings,
        PERMISSION_URLS,
    )
    from vox.setup.launchd import write_plist, bootstrap, get_status

    console = Console()

    # ---------------------------------------------------------------------------
    # Step 1: Welcome banner
    # ---------------------------------------------------------------------------
    console.print(Panel(
        f"[bold cyan]Vox v{__version__}[/bold cyan]\n"
        "Voice layer for AI coding agents\n\n"
        "[dim]This wizard checks permissions, downloads the Kokoro TTS model,\n"
        "tests your microphone, and configures Vox as a launchd service.[/dim]",
        title="[bold]Vox Setup[/bold]",
        border_style="cyan",
    ))
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
                from huggingface_hub import snapshot_download  # noqa: lazy import

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
            console.print("  [dim]Skipped. Run `vox setup` again to download later.[/dim]")

    console.print()

    # ---------------------------------------------------------------------------
    # Step 4: Microphone level test
    # ---------------------------------------------------------------------------
    console.print("[bold]Step 4: Microphone Test[/bold]")
    console.print("  Recording for 2 seconds — say something...")

    try:
        import pyaudio  # noqa: lazy import
        import numpy as np  # noqa: lazy import

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
            start_t = time.time()

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
    from vox.config import CONFIG_FILE, ensure_config_dir
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
    from vox.setup.launchd import PLIST_PATH

    install_plist = console.input(
        "  Install vox as a launchd service (starts automatically at login)? [Y/n] "
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
        console.print("  [dim]Skipped — run `vox start --daemon` to start the service later.[/dim]")

    console.print()

    # ---------------------------------------------------------------------------
    # Step 7: MCP auto-approve (write to ~/.claude/settings.json)
    # ---------------------------------------------------------------------------
    console.print("[bold]Step 7: Claude Code MCP Auto-Approve[/bold]")

    claude_settings_path = Path.home() / ".claude" / "settings.json"

    try:
        if claude_settings_path.exists():
            with open(claude_settings_path) as f:
                settings = json.load(f)
        else:
            settings = {}

        # Add/update vox MCP server entry
        if "mcpServers" not in settings:
            settings["mcpServers"] = {}

        settings["mcpServers"]["vox"] = {
            "command": sys.executable,
            "args": ["-m", "vox.mcp.server"],
        }

        # Add vox to allowedTools if that key exists
        if "allowedTools" in settings:
            allowed = settings["allowedTools"]
            if isinstance(allowed, list) and "vox" not in allowed:
                allowed.append("vox")

        # Ensure the .claude directory exists
        claude_settings_path.parent.mkdir(parents=True, exist_ok=True)

        with open(claude_settings_path, "w") as f:
            json.dump(settings, f, indent=2)
            f.write("\n")

        console.print(f"  [green]✓[/green] Vox MCP server added to Claude Code settings: {claude_settings_path}")

    except Exception as e:
        console.print(f"  [yellow]![/yellow] Could not write settings automatically: {e}")
        console.print("  [dim]Add manually to ~/.claude/settings.json:[/dim]")
        console.print(f"""  [dim]{{
    "mcpServers": {{
      "vox": {{
        "command": "{sys.executable}",
        "args": ["-m", "vox.mcp.server"]
      }}
    }}
  }}[/dim]""")

    console.print()

    # ---------------------------------------------------------------------------
    # Step 8: Summary
    # ---------------------------------------------------------------------------
    console.print("[bold]Step 8: Setup Summary[/bold]")
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
        f"Vox service status (PID: {status.get('pid', '-')})",
    )
    summary_table.add_row(
        "[green]✓[/green]" if claude_settings_path.exists() else "[dim]-[/dim]",
        "Claude Code MCP entry",
    )

    console.print(summary_table)
    console.print()
    console.print("[bold cyan]Setup complete![/bold cyan] Run [bold]vox status[/bold] to check service state.")
    console.print()
