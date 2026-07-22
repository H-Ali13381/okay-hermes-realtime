from __future__ import annotations

import ast
from pathlib import Path

from realtime_action_spike.runtime import browser
from realtime_action_spike.runtime.browser.brave_origin import DedicatedBraveLauncher
from realtime_action_spike.runtime.browser.contracts import BrowserLaunchError
from realtime_action_spike.runtime.browser.process import DedicatedBrowserHandle

PACKAGE_ROOT = Path(__file__).parents[1] / "src" / "realtime_action_spike"


def test_browser_runtime_is_a_facade_over_semantic_leaves() -> None:
    runtime_root = PACKAGE_ROOT / "runtime"
    browser_root = runtime_root / "browser"

    assert not (runtime_root / "browser.py").exists()
    assert {
        "__init__.py",
        "brave_origin.py",
        "contracts.py",
        "process.py",
        "urls.py",
    } <= {path.name for path in browser_root.iterdir() if path.is_file()}

    facade_tree = ast.parse((browser_root / "__init__.py").read_text(encoding="utf-8"))
    implementations = [
        node
        for node in facade_tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    assert implementations == []

    assert browser.DedicatedBraveLauncher is DedicatedBraveLauncher
    assert browser.BrowserLaunchError is BrowserLaunchError
    assert browser.DedicatedBrowserHandle is DedicatedBrowserHandle
