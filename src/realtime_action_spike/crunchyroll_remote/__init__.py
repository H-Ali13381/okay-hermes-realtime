"""Bounded prototype for phone-assisted login in a dedicated Crunchyroll Brave profile."""

from .browser import BraveRemoteLauncher
from .cdp import CdpInputController, LoopbackCdpTransport
from .contracts import CommandError, RemoteCommand, RemotePrototypeError
from .protocol import EncryptedCommandSession
from .web import create_remote_app

__all__ = [
    "BraveRemoteLauncher",
    "CdpInputController",
    "CommandError",
    "EncryptedCommandSession",
    "LoopbackCdpTransport",
    "RemoteCommand",
    "RemotePrototypeError",
    "create_remote_app",
]
