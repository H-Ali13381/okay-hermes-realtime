"""Environment and provider session configuration."""

from __future__ import annotations

import os
from typing import Any, Literal

from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, SecretStr

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
- Call a tool when the user explicitly requests current time, a timer, media playback or control,
  ending the voice session, or delegation of substantial work.
- Do not call media tools when the user is merely discussing music.
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
    realtime_reasoning_effort: Literal["minimal", "low", "medium", "high", "xhigh"] = (
        "minimal"
    )
    gateway_host: str = "127.0.0.1"
    gateway_port: int = Field(default=8765, ge=1, le=65_535)
    streamlit_host: str = "127.0.0.1"
    streamlit_port: int = Field(default=8501, ge=1, le=65_535)

    @classmethod
    def from_env(cls) -> Settings:
        load_dotenv()
        key = os.getenv("OPENAI_API_KEY") or None
        return cls.model_validate(
            {
                "openai_api_key": key,
                "realtime_model": os.getenv("REALTIME_MODEL", "gpt-realtime-2.1-mini"),
                "realtime_voice": os.getenv("REALTIME_VOICE", "marin"),
                "realtime_reasoning_effort": os.getenv(
                    "REALTIME_REASONING_EFFORT", "minimal"
                ),
                "gateway_host": os.getenv("GATEWAY_HOST", "127.0.0.1"),
                "gateway_port": os.getenv("GATEWAY_PORT", "8765"),
                "streamlit_host": os.getenv("STREAMLIT_HOST", "127.0.0.1"),
                "streamlit_port": os.getenv("STREAMLIT_PORT", "8501"),
            }
        )

    def api_key_value(self) -> str | None:
        if self.openai_api_key is None:
            return None
        return self.openai_api_key.get_secret_value()


def build_realtime_session(settings: Settings) -> dict[str, Any]:
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
                    "create_response": True,
                    "interrupt_response": True,
                }
            },
            "output": {"voice": settings.realtime_voice},
        },
        "tools": build_openai_tools(),
        "tool_choice": "auto",
    }
