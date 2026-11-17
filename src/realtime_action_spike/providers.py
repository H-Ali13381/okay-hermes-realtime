"""Provider identity and transport contracts for the voice daemon.

A contract describes which provider owns a voice session and which transport the
build can actually use. Provider identity is captured when the controller starts;
it is never negotiated or changed inside an established session.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ProviderId = Literal["openai", "gemini"]
ProviderAvailability = Literal["active", "contract_only"]
ProviderTransport = Literal["webrtc", "gemini_live_websocket"]


class UnknownVoiceProviderError(ValueError):
    """Raised when a provider id is not registered."""


@dataclass(frozen=True, slots=True)
class VoiceProviderContract:
    id: ProviderId
    display_name: str
    model: str
    transport: ProviderTransport
    availability: ProviderAvailability
    auth_env: str
    browser_owns_media: bool


OPENAI_REALTIME = VoiceProviderContract(
    id="openai",
    display_name="OpenAI Realtime",
    model="gpt-realtime-2.1",
    transport="webrtc",
    availability="active",
    auth_env="OPENAI_API_KEY",
    browser_owns_media=True,
)

GEMINI_LIVE = VoiceProviderContract(
    id="gemini",
    display_name="Gemini Live",
    model="gemini-3.8-live",
    transport="gemini_live_websocket",
    availability="contract_only",
    auth_env="GEMINI_API_KEY",
    browser_owns_media=False,
)

_PROVIDERS: dict[ProviderId, VoiceProviderContract] = {
    OPENAI_REALTIME.id: OPENAI_REALTIME,
    GEMINI_LIVE.id: GEMINI_LIVE,
}


def get_provider_contract(provider_id: str) -> VoiceProviderContract:
    normalized = provider_id.strip().casefold()
    try:
        return _PROVIDERS[normalized]  # type: ignore[index]
    except KeyError as exc:
        raise UnknownVoiceProviderError(
            f"Unknown voice provider: {provider_id!r}. "
            f"Expected one of: {', '.join(_PROVIDERS)}"
        ) from exc


def provider_contract_snapshot() -> list[dict[str, str | bool]]:
    return [
        {
            "id": contract.id,
            "display_name": contract.display_name,
            "model": contract.model,
            "transport": contract.transport,
            "availability": contract.availability,
            "auth_env": contract.auth_env,
            "browser_owns_media": contract.browser_owns_media,
        }
        for contract in _PROVIDERS.values()
    ]
