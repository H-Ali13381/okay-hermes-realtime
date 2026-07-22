"""Public browser lifecycle facade.

Process teardown, loopback URL policy, and Brave Origin launch details live in
focused leaves under this package. Callers keep the stable runtime.browser API.
"""

from .brave_origin import DedicatedBraveLauncher
from .contracts import BrowserHandle, BrowserLaunchError, NoopBrowserHandle
from .process import DedicatedBrowserHandle

__all__ = [
    "BrowserHandle",
    "BrowserLaunchError",
    "DedicatedBraveLauncher",
    "DedicatedBrowserHandle",
    "NoopBrowserHandle",
]
