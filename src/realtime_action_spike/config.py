from __future__ import annotations

import os
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from .capabilities import build_openai_tools

DEFAULT_INSTRUCTIONS = """# Role and objective
You are a concise, natural realtime voice assistant in a local function-calling test.
Hold an ordinary conversation and use the supplied tools for supported assistant actions.

# Conversation
- Respond briefly and naturally. Do not turn simple answers into monologues.
- Do not use a spoken preamble for direct answers or lightweight tools.
- If audio is unclear or a required argument is missing, ask one short clarifying question.
- Let the user interrupt you.

# Tools
- Use only tools supplied in this session and only for actions described by their schemas.
- For complex, multi-step, tool-using, coding, filesystem, web/current-data, automation,
  memory-dependent, or deep reasoning requests, call handoff_to_heavy_agent.
- When the user asks what happened with a handed-off task, call check_heavy_agent_task and
  relay its spoken summary.
- When a background task is blocked, explain the exact requested permission and ask one short
  question. If the user explicitly approves or denies that exact request, call
  resolve_heavy_agent_block with the task id and block event id you were given.
- Never infer approval, broaden its scope, or treat ambiguous speech as permission.
- If no supplied tool can perform a requested side effect—such as timers or media
  playback/control—say briefly that it is unavailable.
- Do not claim an action succeeded before its tool result.
- After a successful lightweight tool result, acknowledge it in one short sentence.
- These tools are a test: most side effects are simulated. Say so if the result says simulated.
- Never invent functions or arguments outside the provided schemas.
"""


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    openai_api_key: SecretStr | None = None
    realtime_model: str = "gpt-realtime-2.1-mini"
    realtime_voice: str = "marin"
    realtime_reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh"] = "minimal"
    gateway_host: str = "127.0.0.1"
    gateway_port: int = Field(default=8765, ge=1, le=65_535)

    activation_socket_path: str = ""

    brave_bin: str = "/opt/brave.com/brave-origin-nightly/brave"
    voice_browser_profile: str = "~/.local/share/okay-hermes-realtime/brave-profile"
    voice_page_url: str = "http://127.0.0.1:8765/voice"
    voice_browser_start_timeout_seconds: float = 20.0
    kde_task_notifications: bool = True

    @staticmethod
    def default_activation_socket_path(xdg_runtime_dir: str | None = None) -> str:
        runtime_dir = xdg_runtime_dir or os.getenv("XDG_RUNTIME_DIR")
        if not runtime_dir:
            runtime_dir = f"/run/user/{os.getuid()}"
        return str(Path(runtime_dir) / "okay-hermes-realtime" / "activation.sock")

    @model_validator(mode="before")
    @classmethod
    def _validate_activation_path(cls, data: object) -> object:
        if isinstance(data, dict):
            value = data.get("activation_socket_path")
            if isinstance(value, str) and value:
                resolved = Path(value)
                if not resolved.is_absolute():
                    raise ValueError("ACTIVATION_SOCKET_PATH must be absolute")
                return data
        return data

    @property
    def resolved_activation_socket_path(self) -> str:
        if self.activation_socket_path:
            return str(Path(self.activation_socket_path).expanduser())
        return self.default_activation_socket_path()

    @field_validator("voice_page_url")
    @classmethod
    def _validate_voice_page_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"}:
            raise ValueError("VOICE_PAGE_URL must use http or https")
        if parsed.username or parsed.password:
            raise ValueError("VOICE_PAGE_URL must not include credentials")
        if parsed.fragment:
            raise ValueError("VOICE_PAGE_URL must not include a fragment")
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise ValueError("VOICE_PAGE_URL must be loopback")
        return value

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        key = os.getenv("OPENAI_API_KEY") or None
        activation_socket_path = os.getenv(
            "ACTIVATION_SOCKET_PATH",
            cls.default_activation_socket_path(),
        )
        return cls.model_validate(
            {
                "openai_api_key": key,
                "realtime_model": os.getenv("REALTIME_MODEL", "gpt-realtime-2.1-mini"),
                "realtime_voice": os.getenv("REALTIME_VOICE", "marin"),
                "realtime_reasoning_effort": os.getenv("REALTIME_REASONING_EFFORT", "minimal"),
                "gateway_host": os.getenv("GATEWAY_HOST", "127.0.0.1"),
                "gateway_port": os.getenv("GATEWAY_PORT", "8765"),
                "activation_socket_path": activation_socket_path,
                "brave_bin": os.getenv(
                    "BRAVE_BIN", "/opt/brave.com/brave-origin-nightly/brave"
                ),
                "voice_browser_profile": os.getenv(
                    "VOICE_BROWSER_PROFILE",
                    "~/.local/share/okay-hermes-realtime/brave-profile",
                ),
                "voice_page_url": os.getenv(
                    "VOICE_PAGE_URL",
                    "http://127.0.0.1:8765/voice",
                ),
                "voice_browser_start_timeout_seconds": os.getenv(
                    "VOICE_BROWSER_START_TIMEOUT_SECONDS", "20.0"
                ),
                "kde_task_notifications": os.getenv(
                    "KDE_TASK_NOTIFICATIONS", "true"
                ),
            }
        )

    def api_key_value(self) -> str | None:
        if self.openai_api_key is None:
            return None
        return self.openai_api_key.get_secret_value()


def build_realtime_session(settings: Settings) -> dict[str, object]:
    """Build the server-owned OpenAI Realtime session contract."""
    return {
        "type": "realtime",
        "model": settings.realtime_model,
        "output_modalities": ["audio"],
        "instructions": DEFAULT_INSTRUCTIONS,
        "reasoning": {"effort": settings.realtime_reasoning_effort},
        "audio": {
            "input": {
                "transcription": {"model": "gpt-4o-mini-transcribe"},
                "turn_detection": {
                    "type": "semantic_vad",
                    "eagerness": "high",
                    "create_response": False,
                    "interrupt_response": True,
                },
            },
            "output": {"voice": settings.realtime_voice},
        },
        "tools": build_openai_tools(),
        "tool_choice": "auto",
    }
