"""
Pydantic-based configuration system for vox.

Loads from ~/.config/vox/config.yaml (XDG-compliant via platformdirs).
All fields have sensible defaults so a config file is optional.
Invalid configs produce actionable pydantic v2 error messages.

Requirement: CONF-01, CONF-02, CONF-03, CONF-04
"""

import sys
from pathlib import Path
from typing import Any

import yaml
from platformdirs import user_config_dir
from pydantic import BaseModel, field_validator, model_validator, ValidationError


# ---------------------------------------------------------------------------
# Config file location (XDG-compliant)
# ---------------------------------------------------------------------------

CONFIG_DIR = Path(user_config_dir("vox"))
CONFIG_FILE = CONFIG_DIR / "config.yaml"


# ---------------------------------------------------------------------------
# Nested config models
# ---------------------------------------------------------------------------

class WakeWordConfig(BaseModel):
    """Wake word model names for start and stop triggers."""
    start: str = "hey_jarvis_v0.1"
    stop: str = ""  # Empty = use same as start

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
    """TTS control hook configuration.

    When tts.enabled is False (default), voice commands are logged but not
    executed — no crash, just a warning. This allows the package to work
    without any TTS setup.

    Requirement: DECP-05
    """
    enabled: bool = False
    script_path: str | None = None

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


class EchoSuppressionConfig(BaseModel):
    """Echo suppression configuration.

    When enabled and no headset is detected, the wake word detector is
    silenced while the TTS_PLAYING_FLAG file is present (written by the TTS
    process). This prevents the mic from picking up TTS output through
    speakers and triggering a false wake word detection.

    Requirement: AUDIO-09, AUDIO-10
    """
    enabled: bool = True


# ---------------------------------------------------------------------------
# Root config model
# ---------------------------------------------------------------------------

class VoxConfig(BaseModel):
    """Root configuration model for the vox voice layer.

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

    mic_priority: list[str] = ["MacBook Pro Microphone"]

    # Path to cues directory — empty = auto-detect from package location
    cues_dir: str = ""

    log_file: str = "/tmp/vox.log"
    log_max_bytes: int = 1_000_000

    @field_validator("target_mode")
    @classmethod
    def validate_target_mode(cls, v: str) -> str:
        valid = {"always-focused", "pinned-app", "last-agent"}
        if v not in valid:
            raise ValueError(f"target_mode must be one of {valid}, got '{v}'")
        return v

    class Config:
        # Allow extra fields to be ignored (forward compatibility)
        extra = "ignore"


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_config(config_path: Path | None = None) -> VoxConfig:
    """Load VoxConfig from YAML file or return defaults.

    Args:
        config_path: Override the default config file location
            (~/.config/vox/config.yaml). Useful for --config CLI flag
            and testing.

    Returns:
        A validated VoxConfig instance. If no config file exists,
        all fields use their sensible defaults.

    Raises:
        SystemExit(1): If the config file exists but fails pydantic validation.
            The error message includes field paths and expected types.

    Requirement: CONF-02, CONF-03
    """
    path = config_path if config_path is not None else CONFIG_FILE

    if path.exists():
        with open(path) as f:
            raw: Any = yaml.safe_load(f)
        if raw is None:
            raw = {}
        try:
            return VoxConfig(**raw)
        except ValidationError as e:
            print("ERROR: Invalid vox configuration:", file=sys.stderr)
            for err in e.errors():
                loc = " -> ".join(str(p) for p in err["loc"])
                print(f"  Field '{loc}': {err['msg']}", file=sys.stderr)
                if "input" in err:
                    print(f"    Got: {err['input']!r}", file=sys.stderr)
            sys.exit(1)
    else:
        return VoxConfig()


# ---------------------------------------------------------------------------
# Default config generation
# ---------------------------------------------------------------------------

def generate_default_config() -> str:
    """Return a commented YAML string showing all config options with defaults.

    This is written to ~/.config/vox/config.yaml on first run via
    ensure_config_dir().

    Requirement: CONF-04
    """
    return """\
# vox configuration
# Generated by: vox --setup
# Location: ~/.config/vox/config.yaml
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
# Text-to-speech (TTS) control
# ---------------------------------------------------------------------------

tts:
  enabled: false           # Set to true and configure script_path to enable TTS control
  script_path: null        # Absolute path to your TTS control script

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

log_file: /tmp/vox.log
log_max_bytes: 1000000     # 1 MB — rotate to vox.log.1 when exceeded

# ---------------------------------------------------------------------------
# Echo suppression
# ---------------------------------------------------------------------------

# Echo suppression — auto-mutes mic during TTS when no headset detected
echo_suppression:
  enabled: true
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
