"""Central configuration.

All settings come from environment variables (optionally loaded from a local
`.env` file). Secrets are held as `SecretStr` so they never end up in reprs,
logs or API responses.
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field, SecretStr

PROJECT_ROOT = Path(__file__).resolve().parent.parent

GROQ_BASE_URL = "https://api.groq.com/openai/v1"
OPENAI_BASE_URL = "https://api.openai.com/v1"


class RunMode(StrEnum):
    LIVE = "live"
    MOCK = "mock"


def load_dotenv(path: Path) -> None:
    """Minimal `.env` loader. Real environment variables always win."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        else:
            value = value.split(" #", 1)[0].strip()
        os.environ.setdefault(key, value)


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    raw = _env(name)
    if not raw:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = _env(name)
    try:
        return float(raw) if raw else default
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    return int(_env_float(name, default))


def _env_list(name: str, default: list[str], sep: str = ",") -> list[str]:
    raw = _env(name)
    if not raw:
        return list(default)
    return [part.strip() for part in raw.split(sep) if part.strip()]


def _secret(name: str) -> SecretStr | None:
    value = _env(name)
    return SecretStr(value) if value else None


class ModelSettings(BaseModel):
    fast: str = "llama-3.1-8b-instant"
    general: str = "llama-3.3-70b-versatile"
    reasoning: str = "openai/gpt-oss-120b"
    vision: str = "meta-llama/llama-4-scout-17b-16e-instruct"


class LLMSettings(BaseModel):
    provider: str = "groq"
    base_url: str = GROQ_BASE_URL
    api_key: SecretStr | None = None
    models: ModelSettings = Field(default_factory=ModelSettings)
    timeout_s: float = 60.0
    max_retries: int = 2
    temperature: float = 0.4


class VoiceSettings(BaseModel):
    stt_provider: str = "groq"
    stt_model: str = "whisper-large-v3-turbo"
    stt_language: str = ""
    tts_provider: str = "browser"
    tts_model: str = ""
    tts_voice: str = ""
    groq_api_key: SecretStr | None = None
    elevenlabs_api_key: SecretStr | None = None
    elevenlabs_voice_id: str = ""
    openai_api_key: SecretStr | None = None
    wake_words: list[str] = Field(default_factory=lambda: ["jarvis", "hey jarvis"])
    require_wake_word: bool = True
    conversation_window_s: float = 15.0


class PermissionSettings(BaseModel):
    allowed_paths: list[Path] = Field(default_factory=lambda: [Path.home()])
    allow_web: bool = True
    allow_clipboard: bool = True
    allow_input_control: bool = False
    allow_vision: bool = False
    confirm_medium_risk: bool = True
    confirmation_timeout_s: float = 120.0
    disabled_tools: list[str] = Field(default_factory=list)
    allow_private_network_fetch: bool = False


class UserSettings(BaseModel):
    honorific: str = "sir"
    name: str = ""
    location_name: str = ""
    latitude: float | None = None
    longitude: float | None = None


class SearchSettings(BaseModel):
    provider: str = "duckduckgo"
    tavily_api_key: SecretStr | None = None
    brave_api_key: SecretStr | None = None
    max_results: int = 8


class TaskSettings(BaseModel):
    max_concurrent: int = 3
    max_agent_steps: int = 12
    max_tool_calls_per_turn: int = 10


class Settings(BaseModel):
    mode: RunMode = RunMode.LIVE
    host: str = "127.0.0.1"
    port: int = 8765
    data_dir: Path = PROJECT_ROOT / "data"
    web_dir: Path = PROJECT_ROOT / "web"
    log_level: str = "INFO"
    access_token: SecretStr | None = None
    history_window: int = 24
    llm: LLMSettings = Field(default_factory=LLMSettings)
    voice: VoiceSettings = Field(default_factory=VoiceSettings)
    permissions: PermissionSettings = Field(default_factory=PermissionSettings)
    user: UserSettings = Field(default_factory=UserSettings)
    search: SearchSettings = Field(default_factory=SearchSettings)
    tasks: TaskSettings = Field(default_factory=TaskSettings)

    @property
    def mock(self) -> bool:
        return self.mode is RunMode.MOCK

    def secret_values(self) -> list[str]:
        """Every configured secret, for log redaction."""
        secrets = [
            self.llm.api_key,
            self.voice.groq_api_key,
            self.voice.elevenlabs_api_key,
            self.voice.openai_api_key,
            self.search.tavily_api_key,
            self.search.brave_api_key,
            self.access_token,
        ]
        return [s.get_secret_value() for s in secrets if s and s.get_secret_value()]

    def public_summary(self) -> dict[str, object]:
        """Non-secret configuration, safe to show in diagnostics / the HUD."""
        return {
            "mode": self.mode.value,
            "llm_provider": self.llm.provider,
            "llm_configured": bool(self.llm.api_key) or self.llm.provider in {"mock", "openai_compatible"},
            "models": self.llm.models.model_dump(),
            "stt_provider": self.voice.stt_provider,
            "tts_provider": self.voice.tts_provider,
            "wake_words": self.voice.wake_words,
            "require_wake_word": self.voice.require_wake_word,
            "conversation_window_s": self.voice.conversation_window_s,
            "allow_web": self.permissions.allow_web,
            "allow_vision": self.permissions.allow_vision,
            "allow_input_control": self.permissions.allow_input_control,
            "allow_clipboard": self.permissions.allow_clipboard,
            "confirm_medium_risk": self.permissions.confirm_medium_risk,
            "allowed_paths": [str(p) for p in self.permissions.allowed_paths],
            "search_provider": self.search.provider,
            "location": {
                "name": self.user.location_name,
                "latitude": self.user.latitude,
                "longitude": self.user.longitude,
            },
            "honorific": self.user.honorific,
        }


def _llm_settings(mode: RunMode) -> LLMSettings:
    provider = _env("JARVIS_LLM_PROVIDER", "mock" if mode is RunMode.MOCK else "groq").lower()
    if provider == "groq":
        base_url, key = GROQ_BASE_URL, _secret("GROQ_API_KEY")
    elif provider == "openai":
        base_url, key = OPENAI_BASE_URL, _secret("OPENAI_API_KEY")
    else:
        base_url, key = _env("JARVIS_LLM_BASE_URL", "http://localhost:11434/v1"), _secret("JARVIS_LLM_API_KEY")
    base_url = _env("JARVIS_LLM_BASE_URL", base_url).rstrip("/")
    defaults = ModelSettings()
    return LLMSettings(
        provider=provider,
        base_url=base_url,
        api_key=key,
        models=ModelSettings(
            fast=_env("JARVIS_MODEL_FAST", defaults.fast),
            general=_env("JARVIS_MODEL_GENERAL", defaults.general),
            reasoning=_env("JARVIS_MODEL_REASONING", defaults.reasoning),
            vision=_env("JARVIS_MODEL_VISION", defaults.vision),
        ),
        timeout_s=_env_float("JARVIS_LLM_TIMEOUT_S", 60.0),
        temperature=_env_float("JARVIS_LLM_TEMPERATURE", 0.4),
    )


def _optional_float(name: str) -> float | None:
    raw = _env(name)
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


def load_settings(env_file: Path | None = None, **overrides: object) -> Settings:
    """Build settings from the environment (and `.env`). Keyword overrides win."""
    load_dotenv(env_file or PROJECT_ROOT / ".env")
    mode = RunMode(_env("JARVIS_MODE", "live").lower() or "live")
    allowed = [Path(p).expanduser() for p in _env_list("JARVIS_ALLOWED_PATHS", [], sep=";")]
    settings = Settings(
        mode=mode,
        host=_env("JARVIS_HOST", "127.0.0.1"),
        port=_env_int("JARVIS_PORT", 8765),
        data_dir=Path(_env("JARVIS_DATA_DIR") or PROJECT_ROOT / "data").expanduser(),
        log_level=_env("JARVIS_LOG_LEVEL", "INFO").upper(),
        access_token=_secret("JARVIS_ACCESS_TOKEN"),
        llm=_llm_settings(mode),
        voice=VoiceSettings(
            stt_provider=_env("JARVIS_STT_PROVIDER", "groq").lower(),
            stt_model=_env("JARVIS_STT_MODEL", "whisper-large-v3-turbo"),
            stt_language=_env("JARVIS_STT_LANGUAGE"),
            tts_provider=_env("JARVIS_TTS_PROVIDER", "browser").lower(),
            tts_model=_env("JARVIS_TTS_MODEL"),
            tts_voice=_env("JARVIS_TTS_VOICE"),
            groq_api_key=_secret("GROQ_API_KEY"),
            elevenlabs_api_key=_secret("ELEVENLABS_API_KEY"),
            elevenlabs_voice_id=_env("ELEVENLABS_VOICE_ID"),
            openai_api_key=_secret("OPENAI_API_KEY"),
            wake_words=[w.lower() for w in _env_list("JARVIS_WAKE_WORDS", ["jarvis", "hey jarvis"])],
            require_wake_word=_env_bool("JARVIS_REQUIRE_WAKE_WORD", True),
            conversation_window_s=_env_float("JARVIS_CONVERSATION_WINDOW_S", 15.0),
        ),
        permissions=PermissionSettings(
            allowed_paths=allowed or [Path.home()],
            allow_web=_env_bool("JARVIS_ALLOW_WEB", True),
            allow_clipboard=_env_bool("JARVIS_ALLOW_CLIPBOARD", True),
            allow_input_control=_env_bool("JARVIS_ALLOW_INPUT_CONTROL", False),
            allow_vision=_env_bool("JARVIS_ALLOW_VISION", False),
            confirm_medium_risk=_env_bool("JARVIS_CONFIRM_MEDIUM_RISK", True),
            confirmation_timeout_s=_env_float("JARVIS_CONFIRMATION_TIMEOUT_S", 120.0),
            disabled_tools=_env_list("JARVIS_DISABLED_TOOLS", []),
            allow_private_network_fetch=_env_bool("JARVIS_ALLOW_PRIVATE_NETWORK_FETCH", False),
        ),
        user=UserSettings(
            honorific=_env("JARVIS_HONORIFIC", "sir"),
            name=_env("JARVIS_USER_NAME"),
            location_name=_env("JARVIS_LOCATION_NAME"),
            latitude=_optional_float("JARVIS_LATITUDE"),
            longitude=_optional_float("JARVIS_LONGITUDE"),
        ),
        search=SearchSettings(
            provider=_env("JARVIS_SEARCH_PROVIDER", "duckduckgo").lower(),
            tavily_api_key=_secret("TAVILY_API_KEY"),
            brave_api_key=_secret("BRAVE_API_KEY"),
        ),
        tasks=TaskSettings(
            max_concurrent=_env_int("JARVIS_MAX_CONCURRENT_TASKS", 3),
            max_agent_steps=_env_int("JARVIS_MAX_AGENT_STEPS", 12),
        ),
    )
    if overrides:
        settings = settings.model_copy(update=overrides)
    return settings
