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
    start: str = "hey_jarvis_v0.1"
    stop: str = ""  # Empty = use same as start
    also_load: list[str] = []  # Additional models to load as fallback wake words
    model_thresholds: dict[str, float] = {}  # Per-model threshold overrides (e.g. hey_vox: 0.95)
    models_dir: str = ""  # Custom models directory (empty = use default locations)

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
        valid = {"detailed", "concise", "technical", "casual"}
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

    # ECHO-01: Grace period (seconds) after TTS ends before re-enabling wake word.
    grace_after_tts: float = 0.6

    # ECHO-02: Wake word threshold multiplier in speaker mode (no headset).
    speaker_threshold_multiplier: float = 1.4

    # ECHO-03: Enable STT echo filtering (strip recently spoken TTS from transcription).
    stt_echo_filter: bool = True

    # ECHO-05: Enable WebRTC AEC via livekit (requires livekit package).
    aec_enabled: bool = False

    # ECHO-06: AEC stream delay in ms (built-in speakers ~50ms).
    aec_delay_ms: int = 50


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
    silence_timeout_secs: float = 5.0
    silence_threshold: int = 200

    # Target app to focus before typing — empty = paste into whatever is focused
    # Requirement: DECP-01 (decoupling: no hardcoded app default)
    target_app: str = ""

    # Target behavior: how transcribed text reaches the AI agent
    # Requirement: INPT-03, INPT-05
    target_mode: str = "always-focused"  # always-focused | pinned-app | last-agent
    agents: list[str] = ["Claude", "Cursor", "Terminal", "iTerm2"]  # App names for last-agent tracking

    enter_count: int = 2
    transcription_prefix: str = ""

    stt: STTConfig = STTConfig()
    tts: TTSConfig = TTSConfig()
    push_to_talk: PushToTalkConfig = PushToTalkConfig()
    audio: AudioConfig = AudioConfig()
    echo_suppression: EchoSuppressionConfig = EchoSuppressionConfig()
    injection: InjectionConfig = InjectionConfig()

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
            print(f"ERROR: Config file has invalid YAML syntax: {e}", file=sys.stderr)
            print(f"  File: {path}", file=sys.stderr)
            print("  Using defaults. Fix the file or delete it to regenerate.", file=sys.stderr)
            return HeyvoxConfig()
        if raw is None:
            raw = {}
        try:
            return HeyvoxConfig(**raw)
        except ValidationError as e:
            print("ERROR: Invalid vox configuration:", file=sys.stderr)
            for err in e.errors():
                loc = " -> ".join(str(p) for p in err["loc"])
                print(f"  Field '{loc}': {err['msg']}", file=sys.stderr)
                if "input" in err:
                    print(f"    Got: {err['input']!r}", file=sys.stderr)
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
            if isinstance(value, bool):
                yaml_val = "true" if value else "false"
            elif isinstance(value, str):
                yaml_val = _yaml_escape(value)
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
                for i, line in enumerate(lines):
                    stripped = line.lstrip()
                    if stripped.startswith(f"{section}:"):
                        in_section = True
                        continue
                    if in_section:
                        if stripped and not stripped.startswith("#") and not line[0].isspace():
                            in_section = False  # Left the section
                            continue
                        if stripped.startswith(f"{subkey}:"):
                            indent = line[:len(line) - len(stripped)]
                            lines[i] = f"{indent}{subkey}: {yaml_val}\n"
                            found = True
                            break

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
  start: hey_jarvis_v0.1   # Model name (from models/ directory)
  stop: hey_jarvis_v0.1    # Leave same as start to toggle; use different for separate start/stop

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
  grace_after_tts: 0.6            # Seconds of wake word silence after TTS ends (reverb tail)
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
