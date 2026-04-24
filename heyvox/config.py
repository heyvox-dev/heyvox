"""
Pydantic-based configuration system for heyvox.

Loads from ~/Library/Application Support/heyvox/config.yaml on macOS (via platformdirs).
All fields have sensible defaults so a config file is optional.
Invalid configs produce actionable pydantic v2 error messages.

Requirement: CONF-01, CONF-02, CONF-03, CONF-04
"""

import os
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any

import yaml
from platformdirs import user_config_dir
from pydantic import BaseModel, ConfigDict, field_validator, model_validator, ValidationError
from heyvox.constants import LOG_FILE_DEFAULT


# ---------------------------------------------------------------------------
# Config file location
# ---------------------------------------------------------------------------
# Prefer ~/.config/heyvox/ (XDG standard, documented path) over
# ~/Library/Application Support/heyvox/ (macOS-native via platformdirs).
# This avoids the split-brain where users edit ~/.config/ but the code reads
# ~/Library/Application Support/.

_XDG_CONFIG_DIR = Path.home() / ".config" / "heyvox"
_PLATFORM_CONFIG_DIR = Path(user_config_dir("heyvox"))

if (_XDG_CONFIG_DIR / "config.yaml").exists():
    CONFIG_DIR = _XDG_CONFIG_DIR
else:
    CONFIG_DIR = _PLATFORM_CONFIG_DIR

CONFIG_FILE = CONFIG_DIR / "config.yaml"


# ---------------------------------------------------------------------------
# Nested config models
# ---------------------------------------------------------------------------

class WakeWordConfig(BaseModel):
    """Wake word model names for start and stop triggers."""
    start: str = "hey_vox"
    stop: str = ""  # Empty = use same as start
    # Additional models to load as fallback wake words. Ships with
    # hey_jarvis_v0.1 (bundled with openwakeword) so fresh installs get a
    # working fallback before any custom model is trained.
    also_load: list[str] = ["hey_jarvis_v0.1"]
    model_thresholds: dict[str, float] = {}  # Per-model threshold overrides (e.g. hey_vox: 0.95)
    models_dir: str = ""  # Custom models directory (empty = use default locations)
    # Hard negative mining: passively save audio clips that contain speech but
    # are NOT the wake word, for use as training negatives.
    collect_negatives: bool = False  # Enable passive negative collection
    negatives_dir: str = ""  # Empty = ~/.config/heyvox/negatives/
    negatives_max_clips: int = 1000  # Cap on disk (oldest pruned)
    negatives_score_range: list[float] = [0.1, 0.7]  # Only save clips scoring in this range
    negatives_interval_secs: float = 10.0  # Min seconds between saves (avoid flooding)

    @model_validator(mode="after")
    def set_stop_default(self) -> "WakeWordConfig":
        if not self.stop:
            self.stop = self.start
        return self


class STTLocalConfig(BaseModel):
    """Local STT engine configuration (MLX Whisper or sherpa-onnx)."""
    engine: str = "mlx"
    mlx_model: str = "mlx-community/whisper-small-mlx"
    model_dir: str = "models/sherpa-onnx-whisper-small"
    language: str = ""
    threads: int = 4


class STTConfig(BaseModel):
    """Speech-to-text backend selection and configuration."""
    backend: str = "local"
    local: STTLocalConfig = STTLocalConfig()


class TTSConfig(BaseModel):
    """TTS engine configuration for native Kokoro-based TTS output.

    Phase 3 enables native TTS by default (enabled=True). Voice, speed,
    verbosity, volume boost, and audio ducking are all configurable.

    script_path is kept for backward compatibility but deprecated — Phase 1
    used it as a bridge to an external bash script. Native Kokoro TTS
    (Phase 3) does not use it.

    Requirement: DECP-05, TTS-04, AUDIO-12
    """
    enabled: bool = True  # Phase 3: native Kokoro TTS is enabled by default

    # TTS engine: "kokoro" (high quality, ~400MB RAM, Metal GPU) or
    # "piper" (lighter, ~80MB RAM, CPU only, less natural)
    engine: str = "kokoro"

    # Voice name — engine-specific.
    # Kokoro: af_heart, af_sarah, af_nova, af_sky, etc.
    # Piper: en_US-lessac-high, en_US-ljspeech-high, en_US-ryan-high, etc.
    voice: str = "af_heart"

    # Playback speed multiplier (1.0 = normal)
    speed: float = 1.0

    # Verbosity level: full | summary | short | skip
    # Controls how much of each message is spoken.
    verbosity: str = "full"

    # TTS style: controls how Claude formulates spoken output.
    # detailed  — explain what happened and why, 3-5 sentences
    # concise   — key takeaway only, 1-2 sentences
    # technical — include function names, error details, diffs
    # casual    — conversational, like a coworker chatting
    style: str = "detailed"

    # Added to system volume before TTS playback (0-100 points, capped at 100)
    # Requirement: AUDIO-12
    volume_boost: int = 10

    # Reduce system volume to this percentage of original during TTS playback.
    # 0 = mute other sounds, 100 = no ducking, 60 = reduce to 60% of original.
    # Requirement: TTS-04
    ducking_percent: int = 60

    # Pause system media (YouTube, Spotify, etc.) during TTS playback.
    # Uses macOS MediaRemote to send explicit pause/play commands.
    pause_media: bool = False

    # Allowed output languages.
    #   "auto"           — detect + route freely (default; includes Qwen3 for DE etc.)
    #   ["en-us"]        — English only; foreign text gets demoted to en-us voice
    #   ["en-us", "de"]  — bilingual; German routes to Qwen3, others demoted
    # Any lang not in the list is demoted to the first entry (or en-us fallback).
    # Env override: HEYVOX_TTS_LANGS="en-us,de"
    languages: list[str] | str = "auto"

    # Voice overrides — when set, replace the mood-based voice selection.
    # None (default) → mood → voice mapping picks (af_sarah/af_heart/af_nova/af_sky
    # for Kokoro; Serena/Vivian/Aura/Aria for Qwen3).
    voice_override: str | None = None        # Kokoro (English + 8 other langs)
    qwen_voice_override: str | None = None   # Qwen3 (German etc.)

    # DEPRECATED: Path to external TTS control script (Phase 1 bridge).
    # No longer used by the native TTS engine. Kept for backward compatibility.
    script_path: str | None = None

    @field_validator("verbosity")
    @classmethod
    def validate_verbosity(cls, v: str) -> str:
        valid = {"full", "summary", "short", "skip"}
        if v not in valid:
            raise ValueError(f"verbosity must be one of {valid}, got '{v}'")
        return v

    @field_validator("engine")
    @classmethod
    def validate_engine(cls, v: str) -> str:
        valid = {"kokoro", "piper"}
        if v not in valid:
            raise ValueError(f"engine must be one of {valid}, got '{v}'")
        return v

    @field_validator("style")
    @classmethod
    def validate_style(cls, v: str) -> str:
        valid = {"detailed", "concise", "technical", "casual", "briefing"}
        if v not in valid:
            raise ValueError(f"style must be one of {valid}, got '{v}'")
        return v

    @field_validator("ducking_percent")
    @classmethod
    def validate_ducking_percent(cls, v: int) -> int:
        return max(0, min(100, v))

    @field_validator("script_path")
    @classmethod
    def validate_script_path(cls, v: str | None) -> str | None:
        if v is not None and not Path(v).exists():
            raise ValueError(
                f"TTS script not found: {v}. "
                f"Set tts.script_path in config or set tts.enabled: false"
            )
        return v


class AppProfileConfig(BaseModel):
    """Per-application behavior profile for text injection.

    HeyVox is a generic voice layer that works with ANY app. All app-specific
    behavior (focus shortcuts, enter counts, workspace detection) is defined
    here — never hardcoded in logic branches.

    Users can add profiles for any app in their config.yaml:

        app_profiles:
          - name: Conductor
            focus_shortcut: l
            enter_count: 1
            is_electron: true
            has_workspace_detection: true
            workspace_db: ~/Library/Application Support/com.conductor.app/conductor.db
            workspace_switch_cmd: ~/.local/bin/conductor-switch-workspace
          - name: Cursor
            focus_shortcut: l
            enter_count: 1
            is_electron: true

    Requirement: INPT-06 (app profile system)
    """
    # Application name as reported by macOS (NSRunningApplication.localizedName)
    name: str

    # Keyboard shortcut key (single letter) sent with Cmd to focus input field.
    # Empty = no focus shortcut available for this app.
    focus_shortcut: str = ""

    # Number of Enter presses after pasting (auto-send). 0 = don't auto-send.
    enter_count: int = 2

    # Whether this is an Electron/Tauri app (affects AX tree traversal).
    is_electron: bool = False

    # Delay (seconds) after activating before pasting — Electron apps need more.
    settle_delay: float = 0.3

    # Delay (seconds) between Cmd+V paste and first Enter keystroke.
    # Electron/Tauri apps may need more under CPU pressure for paste to propagate.
    # Terminal apps process paste synchronously and need near-zero delay.
    enter_delay: float = 0.05

    # Whether this app supports workspace/tab detection via AX tree + DB.
    has_workspace_detection: bool = False

    # Path to the app's SQLite DB for workspace name resolution.
    # Only used when has_workspace_detection is True.
    workspace_db: str = ""

    # Path to CLI tool that switches the app to a named workspace/tab.
    # Only used when has_workspace_detection is True.
    workspace_switch_cmd: str = ""

    # SQL query to map a branch name to a workspace display name.
    # Placeholder {branch} is replaced at runtime.
    # Only used when has_workspace_detection is True.
    workspace_branch_query: str = ""

    # SQL query to list all workspace directory_name values.
    workspace_list_query: str = ""

    # Whether this app supports post-paste AXValue verification (Plan 15-05).
    # Off for Terminal/iTerm2 (TTY content readback isn't via AX). Default True.
    # Requirement: PASTE-15-R7
    supports_ax_verify: bool = True

    # Whether the Conductor adapter (or future analog) can enrich the lock with
    # a workspace+session ID at capture time. Currently only Conductor sets this.
    # Requirement: PASTE-15-R3
    has_session_detection: bool = False

    # Delay (seconds) between paste keystroke and AXValue readback for verification.
    # Conductor (Tauri web view) needs slightly more for AXValue commit to land.
    # Requirement: PASTE-15-R7
    ax_settle_before_verify: float = 0.1


# Built-in profiles for common apps. Users can override or add more via config.
_DEFAULT_APP_PROFILES: list[dict] = [
    {
        "name": "Conductor",
        "focus_shortcut": "l",
        "enter_count": 1,
        "is_electron": True,
        "settle_delay": 0.3,
        "enter_delay": 0.15,
        "has_workspace_detection": True,
        "workspace_db": "~/Library/Application Support/com.conductor.app/conductor.db",
        "workspace_switch_cmd": "~/.local/bin/conductor-switch-workspace",
        "workspace_branch_query": (
            "SELECT directory_name FROM workspaces "
            "WHERE branch = '{branch}' AND state = 'ready'"
        ),
        "workspace_list_query": (
            "SELECT directory_name, branch FROM workspaces WHERE state = 'ready'"
        ),
        "has_session_detection": True,
        "ax_settle_before_verify": 0.15,
    },
    {
        "name": "Cursor",
        "focus_shortcut": "l",
        "enter_count": 1,
        "is_electron": True,
        "settle_delay": 0.3,
        "enter_delay": 0.15,
    },
    {
        "name": "Claude",
        "focus_shortcut": "",
        "enter_count": 2,
        "is_electron": False,
    },
    {
        "name": "Terminal",
        "focus_shortcut": "",
        "enter_count": 1,
        "is_electron": False,
        "supports_ax_verify": False,
    },
    {
        "name": "iTerm2",
        "focus_shortcut": "",
        "enter_count": 1,
        "is_electron": False,
        "supports_ax_verify": False,
    },
]


class PushToTalkConfig(BaseModel):
    """Push-to-talk key binding configuration."""
    enabled: bool = True
    key: str = "fn"


class AudioConfig(BaseModel):
    """Audio stream parameters (must match openwakeword requirements)."""
    sample_rate: int = 16000
    chunk_size: int = 1280


class InjectionConfig(BaseModel):
    """Per-app focus settle delays and retry parameters for paste injection.

    Requirement: PASTE-02, PASTE-03
    """
    focus_settle_secs: float = 0.1
    max_retries: int = 2
    app_delays: dict[str, float] = {
        "conductor": 0.3,
        "cursor": 0.15,
        "windsurf": 0.15,
        "visual studio code": 0.15,
        "iterm2": 0.03,
        "terminal": 0.03,
    }


class EchoSuppressionConfig(BaseModel):
    """Echo suppression configuration.

    When enabled and no headset is detected, the wake word detector is
    silenced while the TTS_PLAYING_FLAG file is present (written by the TTS
    process). This prevents the mic from picking up TTS output through
    speakers and triggering a false wake word detection.

    Requirement: AUDIO-09, AUDIO-10, ECHO-01 through ECHO-06
    """
    enabled: bool = True

    # ECHO-02: Wake word threshold multiplier in speaker mode (no headset).
    speaker_threshold_multiplier: float = 1.4

    # ECHO-03: Enable STT echo filtering (strip recently spoken TTS from transcription).
    stt_echo_filter: bool = True

    # ECHO-05: Enable WebRTC AEC via livekit (requires livekit package).
    aec_enabled: bool = False

    # ECHO-06: AEC stream delay in ms (built-in speakers ~50ms).
    aec_delay_ms: int = 50

    # D-11: Force echo suppression off (bypass speaker mode suppression).
    # When True, wake word detection continues during TTS even without a headset.
    force_disabled: bool = False


class MicProfileEntryConfig(BaseModel):
    """Per-device mic profile override.

    All fields are optional — missing fields fall back to the global config default.
    Use in config.yaml under ``mic_profiles:`` keyed by partial device name.

    Example::

        mic_profiles:
          G435:
            silence_threshold: 300
            echo_safe: true

    Requirement: AUDIO-01, D-02
    """
    noise_floor: int | None = None
    silence_threshold: int | None = None
    buffer_size: int | None = None
    cooldown_tier: int | None = None
    sample_rate: int | None = None
    chunk_size: int | None = None
    gain: float | None = None
    voice_isolation_mode: bool | None = None
    echo_safe: bool | None = None

    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------------------
# Root config model
# ---------------------------------------------------------------------------

class HeyvoxConfig(BaseModel):
    """Root configuration model for the heyvox voice layer.

    All fields have defaults — a config file is completely optional.
    Install and run with zero configuration.

    Requirement: CONF-01
    """
    wake_words: WakeWordConfig = WakeWordConfig()
    threshold: float = 0.5
    cooldown_secs: float = 2.0
    min_recording_secs: float = 1.5
    silence_timeout_secs: float = 2.0
    silence_threshold: int = 200
    max_recording_secs: float = 300.0

    # Target app to focus before typing — empty = paste into whatever is focused
    # Requirement: DECP-01 (decoupling: no hardcoded app default)
    target_app: str = ""

    # Target behavior: how transcribed text reaches the AI agent
    # Requirement: INPT-03, INPT-05
    target_mode: str = "always-focused"  # always-focused | pinned-app | last-agent
    agents: list[str] = ["Claude", "Cursor", "Terminal", "iTerm2"]  # App names for last-agent tracking

    enter_count: int = 1
    transcription_prefix: str = ""

    stt: STTConfig = STTConfig()
    tts: TTSConfig = TTSConfig()
    push_to_talk: PushToTalkConfig = PushToTalkConfig()
    audio: AudioConfig = AudioConfig()
    echo_suppression: EchoSuppressionConfig = EchoSuppressionConfig()
    injection: InjectionConfig = InjectionConfig()

    # Per-device mic profiles — keyed by partial device name (case-insensitive).
    # Values override global silence_threshold, sample_rate, etc. for specific mics.
    # Auto-calibration data is merged in at runtime from ~/.cache/heyvox/mic-profiles.json.
    # Requirement: AUDIO-01, D-02
    mic_profiles: dict[str, MicProfileEntryConfig] = {}

    # Per-app behavior profiles — replaces all hardcoded app logic.
    # Requirement: INPT-06 (app profile system)
    app_profiles: list[AppProfileConfig] = []

    # HUD overlay — floating pill with waveform and state indicator
    hud_enabled: bool = True
    # Show only the menu bar icon (no floating pill)
    hud_menu_bar_only: bool = False

    mic_priority: list[str] = ["MacBook Pro Microphone"]

    # Path to cues directory — empty = auto-detect from package location
    cues_dir: str = ""

    log_file: str = LOG_FILE_DEFAULT
    log_max_bytes: int = 1_000_000

    @field_validator("target_mode")
    @classmethod
    def validate_target_mode(cls, v: str) -> str:
        valid = {"always-focused", "pinned-app", "last-agent"}
        if v not in valid:
            raise ValueError(f"target_mode must be one of {valid}, got '{v}'")
        return v

    @model_validator(mode="after")
    def merge_default_profiles(self) -> "HeyvoxConfig":
        """Merge built-in app profiles with user-defined ones.

        User profiles override built-in profiles with the same name (case-insensitive).
        Built-in profiles not overridden by the user are appended.
        """
        user_names = {p.name.lower() for p in self.app_profiles}
        for default in _DEFAULT_APP_PROFILES:
            if default["name"].lower() not in user_names:
                self.app_profiles.append(AppProfileConfig(**default))
        return self

    def get_app_profile(self, app_name: str) -> AppProfileConfig | None:
        """Look up an app profile by name (case-insensitive substring match).

        Returns the first profile whose name appears in app_name, or None.
        This matches the same substring logic used by LastAgentAdapter.
        """
        app_lower = app_name.lower()
        for profile in self.app_profiles:
            if profile.name.lower() in app_lower:
                return profile
        return None

    model_config = ConfigDict(extra="ignore")


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config(config_path: Path | None = None) -> HeyvoxConfig:
    """Load HeyvoxConfig from YAML file or return defaults.

    Args:
        config_path: Override the default config file location
            (~/.config/heyvox/config.yaml). Useful for --config CLI flag
            and testing.

    Returns:
        A validated HeyvoxConfig instance. If no config file exists,
        all fields use their sensible defaults.

    Raises:
        SystemExit(1): If the config file exists but fails pydantic validation.
            The error message includes field paths and expected types.

    Requirement: CONF-02, CONF-03
    """
    path = config_path if config_path is not None else CONFIG_FILE

    if path.exists():
        try:
            with open(path) as f:
                raw: Any = yaml.safe_load(f)
        except yaml.YAMLError as e:
            try:
                print(f"ERROR: Config file has invalid YAML syntax: {e}", file=sys.stderr)
                print(f"  File: {path}", file=sys.stderr)
                print("  Using defaults. Fix the file or delete it to regenerate.", file=sys.stderr)
            except (BrokenPipeError, OSError):
                pass
            return HeyvoxConfig()
        if raw is None:
            raw = {}
        try:
            return HeyvoxConfig(**raw)
        except ValidationError as e:
            try:
                print("ERROR: Invalid vox configuration:", file=sys.stderr)
                for err in e.errors():
                    loc = " -> ".join(str(p) for p in err["loc"])
                    print(f"  Field '{loc}': {err['msg']}", file=sys.stderr)
                    if "input" in err:
                        print(f"    Got: {err['input']!r}", file=sys.stderr)
            except (BrokenPipeError, OSError):
                pass
            sys.exit(1)
    else:
        return HeyvoxConfig()


_config_lock = threading.Lock()


def _yaml_escape(value: str) -> str:
    """Escape a string value for safe YAML embedding."""
    # Quote if it contains YAML-special characters
    if any(c in value for c in (':', '#', '{', '}', '[', ']', ',', '&', '*',
                                  '?', '|', '-', '<', '>', '=', '!', '%', '@',
                                  '`', '"', "'")):
        # Use double quotes with backslash escaping
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
    if not value or value != value.strip():
        return f'"{value}"'
    return value


def update_config(**kwargs) -> None:
    """Update specific keys in the config file, preserving comments and structure.

    Thread-safe (uses _config_lock). Writes atomically via temp file + rename.
    Uses simple line-based replacement for top-level keys. For nested keys,
    use dot notation (e.g., ``tts.verbosity="short"``).

    Only writes keys that are already present in the file. Appends new
    top-level keys at the end if not found.
    """
    with _config_lock:
        if not CONFIG_FILE.exists():
            return

        content = CONFIG_FILE.read_text()
        if not content.strip():
            return  # Don't clobber an empty/blank config with partial updates

        lines = content.splitlines(keepends=True)

        for key, value in kwargs.items():
            # Convert Python values to YAML scalars
            if value is None:
                yaml_val = "null"
            elif isinstance(value, bool):
                yaml_val = "true" if value else "false"
            elif isinstance(value, str):
                yaml_val = _yaml_escape(value)
            elif isinstance(value, (list, tuple)):
                # Compact flow style for small lists
                items = ", ".join(_yaml_escape(str(v)) for v in value)
                yaml_val = f"[{items}]"
            else:
                yaml_val = str(value)

            parts = key.split(".", 1)
            found = False

            if len(parts) == 1:
                # Top-level key
                for i, line in enumerate(lines):
                    stripped = line.lstrip()
                    if stripped.startswith(f"{key}:") and not stripped.startswith("#"):
                        indent = line[:len(line) - len(stripped)]
                        lines[i] = f"{indent}{key}: {yaml_val}\n"
                        found = True
                        break
                if not found:
                    lines.append(f"{key}: {yaml_val}\n")
            else:
                # Nested key (e.g., tts.verbosity)
                section, subkey = parts
                in_section = False
                section_start_idx = -1
                section_last_idx = -1
                section_indent = ""
                for i, line in enumerate(lines):
                    stripped = line.lstrip()
                    if stripped.startswith(f"{section}:"):
                        in_section = True
                        section_start_idx = i
                        continue
                    if in_section:
                        if stripped and not stripped.startswith("#") and not line[0].isspace():
                            in_section = False  # Left the section
                            continue
                        # Track last indented (section-member) line for insertion point
                        if line.strip() and line[0].isspace():
                            section_last_idx = i
                            if not section_indent:
                                section_indent = line[:len(line) - len(stripped)]
                        if stripped.startswith(f"{subkey}:"):
                            indent = line[:len(line) - len(stripped)]
                            lines[i] = f"{indent}{subkey}: {yaml_val}\n"
                            found = True
                            break
                if not found and section_start_idx >= 0:
                    # Section exists but subkey missing — insert inside the section.
                    insert_idx = (section_last_idx + 1) if section_last_idx >= 0 else (section_start_idx + 1)
                    indent = section_indent or "  "
                    lines.insert(insert_idx, f"{indent}{subkey}: {yaml_val}\n")

        # Atomic write: temp file + rename prevents partial writes on crash
        new_content = "".join(lines)
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=CONFIG_FILE.parent, suffix=".tmp", prefix=".config-"
            )
            try:
                os.write(fd, new_content.encode("utf-8"))
            finally:
                os.close(fd)
            os.replace(tmp_path, CONFIG_FILE)
        except OSError:
            # Fallback: direct write if atomic fails (e.g., cross-device)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            CONFIG_FILE.write_text(new_content)


# ---------------------------------------------------------------------------
# Default config generation
# ---------------------------------------------------------------------------

def generate_default_config() -> str:
    """Return a commented YAML string showing all config options with defaults.

    This is written to ~/.config/heyvox/config.yaml on first run via
    ensure_config_dir().

    Requirement: CONF-04
    """
    return """\
# heyvox configuration
# Generated by: heyvox --setup
# Location: ~/.config/heyvox/config.yaml
# All values shown are defaults — only override what you need.

# ---------------------------------------------------------------------------
# Wake word detection
# ---------------------------------------------------------------------------

wake_words:
  start: hey_vox                   # Model name (from models/ directory)
  stop: hey_vox                    # Leave same as start to toggle; use different for separate start/stop
  also_load: [hey_jarvis_v0.1]     # Extra fallback wake words loaded alongside start/stop

threshold: 0.5             # Detection confidence threshold (0.0–1.0)
cooldown_secs: 2.0         # Minimum seconds between triggers

# ---------------------------------------------------------------------------
# Recording behavior
# ---------------------------------------------------------------------------

min_recording_secs: 1.5    # Discard recordings shorter than this
silence_timeout_secs: 5.0  # Auto-cancel after this many seconds of silence
silence_threshold: 200     # Audio level below this is considered silence

# ---------------------------------------------------------------------------
# Text injection
# ---------------------------------------------------------------------------

target_app: ""             # App to focus before typing — empty = paste into focused app
target_mode: always-focused  # always-focused | pinned-app | last-agent
agents:                    # App names to track in last-agent mode
  - Claude
  - Cursor
  - Terminal
  - iTerm2
enter_count: 2             # Number of Enter presses after pasting
transcription_prefix: ""   # Prepend this text to every transcription

# ---------------------------------------------------------------------------
# App profiles — per-app injection behavior (focus shortcut, enter count, etc.)
# Built-in profiles: Conductor, Cursor, Claude, Terminal, iTerm2
# Override or add your own here:
# ---------------------------------------------------------------------------

# app_profiles:
#   - name: MyApp            # App name as shown by macOS
#     focus_shortcut: l       # Cmd+key to focus input field (empty = none)
#     enter_count: 1          # Enter presses after pasting (0 = don't auto-send)
#     is_electron: true       # Electron/Tauri app (affects AX tree traversal)
#     settle_delay: 0.3       # Seconds to wait after activating before pasting
#     has_workspace_detection: false  # Supports workspace/tab detection via AX+DB

# ---------------------------------------------------------------------------
# Push-to-talk
# ---------------------------------------------------------------------------

push_to_talk:
  enabled: true
  key: fn                  # Supported: fn, right_cmd, right_alt, right_ctrl, right_shift

# ---------------------------------------------------------------------------
# Speech-to-text (STT)
# ---------------------------------------------------------------------------

stt:
  backend: local           # "local" only for now

  local:
    engine: mlx            # "mlx" (Apple Silicon) or "sherpa-onnx" (CPU)
    mlx_model: mlx-community/whisper-small-mlx
    model_dir: models/sherpa-onnx-whisper-small
    language: ""           # Empty = auto-detect. Set to "en" for faster English-only
    threads: 4

# ---------------------------------------------------------------------------
# Text-to-speech (TTS) — Kokoro native engine
# ---------------------------------------------------------------------------

tts:
  enabled: true            # Phase 3: native Kokoro TTS enabled by default
  voice: af_heart          # Kokoro voice name (af_heart = US English female)
  speed: 1.0               # Playback speed multiplier (0.5–2.0)
  verbosity: full          # full | summary | short | skip
  volume_boost: 10         # Added to system volume during TTS (capped at 100)
  ducking_percent: 60      # Reduce system volume to this % during TTS playback (0=off, 100=no ducking)
  pause_media: false       # Pause YouTube/Spotify/etc. during TTS, resume after

  # Allowed output languages. "auto" = detect + route freely.
  # List form forces a fallback: text in any language not listed is spoken
  # with the first entry's voice. Set to ["en-us"] for strict English-only
  # (skips the Qwen3 German daemon entirely — saves 1.2 GB download + 650 MB RAM).
  # Env override: HEYVOX_TTS_LANGS="en-us,de"
  languages: auto          # auto | [en-us] | [en-us, de] | [de]

  # script_path: null      # DEPRECATED: external TTS script path (Phase 1 bridge, no longer needed)

# ---------------------------------------------------------------------------
# Audio
# ---------------------------------------------------------------------------

audio:
  sample_rate: 16000       # Must match openwakeword requirements
  chunk_size: 1280

mic_priority:
  - MacBook Pro Microphone  # List mic names in preference order (partial match OK)

cues_dir: ""               # Empty = auto-detect from package location

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

log_file: /tmp/heyvox.log
log_max_bytes: 1000000     # 1 MB — rotate to heyvox.log.1 when exceeded

# ---------------------------------------------------------------------------
# Echo suppression
# ---------------------------------------------------------------------------

# Echo suppression — auto-mutes mic during TTS when no headset detected
echo_suppression:
  enabled: true
  speaker_threshold_multiplier: 1.4  # Wake word threshold boost in speaker mode (no headset)
  stt_echo_filter: true           # Strip recently spoken TTS text from STT output
  aec_enabled: false              # WebRTC AEC via livekit (pip install heyvox[aec])
  aec_delay_ms: 50                # Speaker-to-mic delay in ms (50 = built-in speakers)
"""


# ---------------------------------------------------------------------------
# Config directory initialization
# ---------------------------------------------------------------------------

def ensure_config_dir() -> Path:
    """Create config directory and write default config if not present.

    Returns:
        Path to the config file (whether it existed or was just created).
    """
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        CONFIG_FILE.write_text(generate_default_config())
    return CONFIG_FILE
