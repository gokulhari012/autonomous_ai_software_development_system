"""pytest plugin injected by the orchestrator (``-p autodev_capture``) into test runs of the target app.

On test failure it captures evidence for the QA agent:

* Playwright tests (``page`` fixture): browser console errors/warnings, page errors and
  failed / >=400 network requests, written to ``$AUTODEV_ARTIFACTS_DIR/browser-logs/``.
  Screenshots and traces are produced by pytest-playwright itself
  (``--screenshot only-on-failure --tracing retain-on-failure``).
* pytest-qt tests (``qtbot`` fixture): a PNG of every visible top-level window,
  written to ``$AUTODEV_ARTIFACTS_DIR/qt-screenshots/``.

The plugin never changes test outcomes.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest


def _artifacts_dir() -> Path | None:
    value = os.environ.get("AUTODEV_ARTIFACTS_DIR")
    return Path(value) if value else None


def _safe_name(nodeid: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", nodeid)[-150:]


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"rep_{report.when}", report)
    if report.when == "call" and report.failed and "qtbot" in getattr(item, "fixturenames", []):
        _capture_qt_windows(item)


def _capture_qt_windows(item) -> None:
    target = _artifacts_dir()
    if target is None:
        return
    try:
        try:
            from PySide6.QtWidgets import QApplication
        except ImportError:  # pragma: no cover - PySide2 projects
            from PySide2.QtWidgets import QApplication  # type: ignore
        app = QApplication.instance()
        if app is None:
            return
        out = target / "qt-screenshots"
        out.mkdir(parents=True, exist_ok=True)
        for index, widget in enumerate(app.topLevelWidgets()):
            if widget.isVisible():
                widget.grab().save(str(out / f"{_safe_name(item.nodeid)}-{index}.png"))
    except Exception as exc:  # evidence capture must never break the run
        print(f"[autodev_capture] qt screenshot failed: {exc}")


@pytest.fixture(autouse=True)
def _autodev_browser_capture(request):
    if "page" not in request.fixturenames:
        yield
        return
    page = request.getfixturevalue("page")
    console: list[dict] = []
    network: list[dict] = []
    page.on("console", lambda msg: console.append({"type": msg.type, "text": msg.text}) if msg.type in ("error", "warning") else None)
    page.on("pageerror", lambda err: console.append({"type": "pageerror", "text": str(err)}))
    page.on("requestfailed", lambda req: network.append({"url": req.url, "method": req.method, "failure": req.failure}))
    page.on("response", lambda resp: network.append({"url": resp.url, "status": resp.status}) if resp.status >= 400 else None)
    yield
    report = getattr(request.node, "rep_call", None)
    target = _artifacts_dir()
    if target is not None and report is not None and report.failed:
        out = target / "browser-logs"
        out.mkdir(parents=True, exist_ok=True)
        (out / f"{_safe_name(request.node.nodeid)}.json").write_text(
            json.dumps({"nodeid": request.node.nodeid, "console": console, "network": network}, indent=2),
            encoding="utf-8",
        )
