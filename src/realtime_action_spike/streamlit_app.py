"""Streamlit shell for the browser-native Realtime WebRTC panel."""

from __future__ import annotations

from pathlib import Path

import streamlit as st

from realtime_action_spike.config import Settings

PANEL_PATH = Path(__file__).parent / "web" / "realtime_panel.html"


def render() -> None:
    settings = Settings.from_env()
    gateway_origin = f"http://{settings.gateway_host}:{settings.gateway_port}"
    panel = PANEL_PATH.read_text(encoding="utf-8").replace(
        "__GATEWAY_ORIGIN__", gateway_origin
    )

    st.set_page_config(
        page_title="Realtime action test",
        page_icon="🎙️",
        layout="wide",
        initial_sidebar_state="collapsed",
    )
    st.markdown(
        """
        <style>
          .stApp { background: #101113; }
          [data-testid="stAppViewContainer"] > .main .block-container {
            max-width: 1480px;
            padding: 0.75rem 1rem 1rem;
          }
          [data-testid="stHeader"], footer { display: none; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    if settings.api_key_value() is None:
        st.warning(
            "The gateway has no OpenAI API key yet. Add OPENAI_API_KEY to .env, then restart."
        )

    st.iframe(panel, width="stretch", height=870, tab_index=0)


if __name__ == "__main__":
    render()
