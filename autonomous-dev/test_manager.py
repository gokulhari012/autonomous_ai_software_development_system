"""Test execution for the target application.

The orchestrator - never an agent - produces the authoritative test results.
Layers are selected from the *detected* project profile, so irrelevant tools
are not forced on a project:

==========  ==============================  =====================================
layer       tool                            runs when
==========  ==============================  =====================================
pytest      pytest (unit/API/DB/integration) any Python project
e2e         pytest-playwright (Chromium)     Flask app with a browser UI
qt          pytest-qt (offscreen platform)   PySide2/PySide6 app
gui         PyAutoGUI (real input)           PySide app + real desktop + explicit opt-in
acceptance  pytest (Product Validator tests) PRODUCT_VALIDATION stage
==========  ==============================  =====================================

Statuses: PASS, FAIL, ERROR (runner crashed / timed out), BLOCKED (cannot run in
this environment - never reported as PASS), NOT_APPLICABLE.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from config import CONTROLLER_DIR, REPORTS_DIR, AppConfig
from events import EventBus
from project_inspector import ProjectProfile, gui_session_available
from safe_exec import SafeExecutor
from state_manager import StateManager

log = logging.getLogger("test_manager")

PLUGIN_DIR = CONTROLLER_DIR / "pytest_plugins"
NON_UNIT_MARKERS = "not e2e and not qt and not gui and not acceptance"


@dataclass
class TestLayer:
    __test__ = False
    name: str
    tool: str
    paths: list[str]
    marker_expr: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    extra_args: list[str] = field(default_factory=list)
    required: bool = True
    blocked_reason: str | None = None
    zero_tests_status: str = "FAIL"  # status when no tests are collected


@dataclass
class LayerResult:
    layer: str
    tool: str
    status: str
    total: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    exit_code: int | None = None
    duration_seconds: float = 0.0
    junit_path: str | None = None
    report_path: str | None = None
    artifacts_dir: str | None = None
    artifacts: list[str] = field(default_factory=list)
    failures: list[dict[str, Any]] = field(default_factory=list)
    summary: str = ""
    command: str = ""
    required: bool = True
    test_run_id: int | None = None
    coverage_percent: float | None = None


@dataclass
class TestSuiteResult:
    __test__ = False
    stage: str
    layers: list[LayerResult]

    @property
    def status(self) -> str:
        required = [l for l in self.layers if l.required]
        if any(l.status == "ERROR" for l in required):
            return "ERROR"
        if any(l.status == "FAIL" for l in required):
            return "FAIL"
        if any(l.status == "BLOCKED" for l in required):
            return "BLOCKED"
        if not any(l.status == "PASS" for l in self.layers):
            return "FAIL"
        return "PASS"

    def totals(self) -> dict[str, int]:
        return {
            "total": sum(l.total for l in self.layers),
            "passed": sum(l.passed for l in self.layers),
            "failed": sum(l.failed + l.errors for l in self.layers),
            "blocked": sum(1 for l in self.layers if l.status == "BLOCKED"),
        }

    def failures(self) -> list[dict[str, Any]]:
        return [f for l in self.layers for f in l.failures]


def parse_junit(path: Path) -> tuple[dict[str, int], list[dict[str, Any]]]:
    tree = ET.parse(path)
    root = tree.getroot()
    suites = [root] if root.tag == "testsuite" else list(root.iter("testsuite"))
    counts = {"total": 0, "failed": 0, "errors": 0, "skipped": 0}
    cases: list[dict[str, Any]] = []
    for suite in suites:
        for case in suite.iter("testcase"):
            classname = case.get("classname", "")
            name = case.get("name", "")
            file_attr = case.get("file")
            if file_attr:
                nodeid = f"{file_attr.replace(os.sep, '/')}::{name}"
            else:
                parts = classname.split(".")
                # "tests.test_api.TestClass" -> tests/test_api.py::TestClass::name
                module_idx = max((i for i, p in enumerate(parts) if p.startswith("test")), default=len(parts) - 1)
                module = "/".join(parts[: module_idx + 1]) + ".py"
                rest = parts[module_idx + 1 :]
                nodeid = "::".join([module, *rest, name])
            status = "PASS"
            message = ""
            for child in case:
                if child.tag == "failure":
                    status, message = "FAIL", (child.get("message") or "") + "\n" + (child.text or "")
                elif child.tag == "error":
                    status, message = "ERROR", (child.get("message") or "") + "\n" + (child.text or "")
                elif child.tag == "skipped":
                    status, message = "SKIPPED", child.get("message") or ""
            counts["total"] += 1
            if status == "FAIL":
                counts["failed"] += 1
            elif status == "ERROR":
                counts["errors"] += 1
            elif status == "SKIPPED":
                counts["skipped"] += 1
            cases.append({"nodeid": nodeid, "status": status, "duration_seconds": float(case.get("time") or 0), "message": message.strip()[:4000]})
    return counts, cases


def playwright_browser_installed(browser: str = "chromium") -> bool:
    custom = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if custom and custom != "0":
        roots = [Path(custom)]
    elif sys.platform == "win32":
        roots = [Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"]
    elif sys.platform == "darwin":
        roots = [Path.home() / "Library" / "Caches" / "ms-playwright"]
    else:
        roots = [Path.home() / ".cache" / "ms-playwright"]
    return any(root.exists() and any(p.name.startswith(browser) for p in root.iterdir()) for root in roots)


class TestManager:
    __test__ = False  # not a pytest test class

    def __init__(self, config: AppConfig, state: StateManager, events: EventBus, executor: SafeExecutor, profile: ProjectProfile):
        self.config = config
        self.state = state
        self.events = events
        self.executor = executor
        self.profile = profile
        self.python = config.project.python_executable or sys.executable
        self._module_cache: dict[str, bool] = {}

    # ----------------------------------------------------------- environment
    def has_module(self, distribution: str, workspace: Path) -> bool:
        if distribution not in self._module_cache:
            result = self.executor.run([self.python, "-m", "pip", "show", distribution], workspace, timeout=60)
            self._module_cache[distribution] = result.ok and "Name:" in result.stdout
        return self._module_cache[distribution]

    def tool_availability(self, workspace: Path) -> dict[str, Any]:
        gui_ok, gui_reason = gui_session_available()
        return {
            "pytest": self.has_module("pytest", workspace),
            "pytest_playwright": self.has_module("pytest-playwright", workspace),
            "chromium": playwright_browser_installed("chromium"),
            "pytest_qt": self.has_module("pytest-qt", workspace),
            "pyautogui": self.has_module("PyAutoGUI", workspace),
            "pytest_timeout": self.has_module("pytest-timeout", workspace),
            "pytest_cov": self.has_module("pytest-cov", workspace),
            "gui_session": gui_ok,
            "gui_reason": gui_reason,
        }

    # -------------------------------------------------------------- planning
    def plan_layers(self, workspace: Path, stage: str, base_url: str | None = None) -> list[TestLayer]:
        cfg = self.config.testing
        test_dir = self.config.project.test_dir
        tools = self.tool_availability(workspace)
        layers: list[TestLayer] = []
        app_types = set(self.profile.application_types)
        db_env = {self.config.project.flask.database_env_var: ""}  # filled per run with an isolated DB

        if cfg.python.enabled:
            layers.append(TestLayer("pytest", "pytest", [test_dir], NON_UNIT_MARKERS, env=dict(db_env),
                                    blocked_reason=None if tools["pytest"] else "pytest is not installed in the project interpreter"))

        has_browser_ui = self.config.project.flask.has_browser_ui
        if has_browser_ui is None:
            has_browser_ui = self.profile.has_browser_ui
        if "flask" in app_types and has_browser_ui and cfg.flask.playwright:
            reason = None
            if not tools["pytest_playwright"]:
                reason = "pytest-playwright is not installed (pip install pytest-playwright)"
            elif not tools["chromium"]:
                reason = "Playwright Chromium is not installed (python -m playwright install chromium)"
            elif not base_url:
                reason = "no running application URL was provided for browser tests"
            args = ["--base-url", base_url or "", "--tracing", "retain-on-failure", "--screenshot", "only-on-failure"]
            for browser in cfg.flask.browsers:
                args += ["--browser", browser]
            layers.append(TestLayer("e2e", "playwright", [test_dir], "e2e and not acceptance", env=dict(db_env), extra_args=args, blocked_reason=reason))

        if "pyside" in app_types and cfg.pyside.enabled:
            qt_env = {"QT_QPA_PLATFORM": "offscreen"} if cfg.pyside.offscreen_widget_tests else {}
            if cfg.pyside.pytest_qt:
                reason = None if tools["pytest_qt"] else "pytest-qt is not installed (pip install pytest-qt)"
                layers.append(TestLayer("qt", "pytest-qt", [test_dir], "qt and not acceptance", env=qt_env, blocked_reason=reason))
            if cfg.pyside.pyautogui:
                reason = None
                if not tools["pyautogui"]:
                    reason = "PyAutoGUI is not installed"
                elif not tools["gui_session"]:
                    reason = f"no usable display: {tools['gui_reason']}"
                elif not cfg.pyside.allow_real_input_automation:
                    reason = "real mouse/keyboard automation is not authorised (testing.pyside.allow_real_input_automation=false)"
                layers.append(TestLayer("gui", "pyautogui", [test_dir], "gui and not acceptance", required=False, blocked_reason=reason, zero_tests_status="NOT_APPLICABLE"))
        return layers

    def acceptance_layers(self, workspace: Path, base_url: str | None, node_ids: list[str] | None = None) -> list[TestLayer]:
        test_dir = self.config.project.test_dir
        env = {"APP_BASE_URL": base_url or "", self.config.project.flask.database_env_var: ""}
        if "pyside" in self.profile.application_types and self.config.testing.pyside.offscreen_widget_tests:
            env["QT_QPA_PLATFORM"] = "offscreen"
        paths = node_ids or [f"{test_dir}/acceptance"]
        extra: list[str] = []
        if base_url and self.has_module("pytest-playwright", workspace):
            extra = ["--base-url", base_url, "--tracing", "retain-on-failure", "--screenshot", "only-on-failure"]
        return [TestLayer("acceptance", "pytest", paths, None, env=env, extra_args=extra)]

    # --------------------------------------------------------------- running
    def _artifacts_dir(self, task_id: int | None, stage: str, layer: str) -> Path:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = REPORTS_DIR / "tests" / self.config.project_id / f"task-{task_id or 0}" / f"{stage.lower()}-{layer}-{stamp}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def run_layer(self, layer: TestLayer, workspace: Path, task_id: int | None, stage: str) -> LayerResult:
        result = LayerResult(layer.name, layer.tool, "PENDING", required=layer.required)
        if layer.blocked_reason:
            result.status = "BLOCKED"
            result.summary = layer.blocked_reason
            return self._record(result, task_id, stage)

        artifacts = self._artifacts_dir(task_id, stage, layer.name)
        result.artifacts_dir = str(artifacts)
        junit = artifacts / f"junit-{layer.name}.xml"
        existing_paths = [p for p in layer.paths if (workspace / p.split("::")[0]).exists()]
        if not existing_paths:
            result.status = layer.zero_tests_status
            result.summary = f"no tests found ({', '.join(layer.paths)} does not exist)"
            return self._record(result, task_id, stage)

        argv = [self.python, "-m", "pytest", *existing_paths, "-q", "-rA", f"--junitxml={junit}", "-o", "junit_family=xunit2",
                "-p", "no:cacheprovider", "-p", "autodev_capture"]
        if layer.marker_expr:
            argv += ["-m", layer.marker_expr]
        acceptance_dir = f"{self.config.project.test_dir}/acceptance"
        if layer.name != "acceptance" and (workspace / acceptance_dir).exists():
            argv += ["--ignore", acceptance_dir]  # acceptance tests need staging; run in PRODUCT_VALIDATION
        if self.has_module("pytest-timeout", workspace):
            argv += [f"--timeout={self.config.testing.python.per_test_timeout_seconds}"]
        if layer.name == "e2e" or (layer.name == "acceptance" and "--base-url" in layer.extra_args):
            argv += ["--output", str(artifacts / "playwright")]
        if layer.name == "pytest" and self.has_module("pytest-cov", workspace) and self.profile.source_dirs:
            for src in self.profile.source_dirs:
                argv.append(f"--cov={src}")
            argv += [f"--cov-report=json:{artifacts / 'coverage.json'}"]
        argv += layer.extra_args + self.config.testing.python.extra_args

        env = {k: v for k, v in layer.env.items()}
        db_var = self.config.project.flask.database_env_var
        if db_var in env and not env[db_var]:
            env[db_var] = "sqlite:///" + (artifacts / "isolated-test.db").as_posix()
        env.update({
            "AUTODEV_ARTIFACTS_DIR": str(artifacts),
            "APP_ENV": "testing",
            "PYTHONPATH": os.pathsep.join([str(PLUGIN_DIR), str(workspace)]),
        })
        result.command = " ".join(argv)
        self.events.publish("TEST_STARTED", f"{layer.name} tests started ({layer.tool})", status="IN_PROGRESS", task_id=task_id, metadata={"layer": layer.name, "stage": stage})
        run = self.executor.run(argv, workspace, timeout=self.config.testing.python.timeout_seconds, env_extra=env)
        (artifacts / f"pytest-{layer.name}.log").write_text(run.stdout + "\n--- stderr ---\n" + run.stderr, encoding="utf-8")
        result.exit_code = run.exit_code
        result.duration_seconds = run.duration_seconds

        cases: list[dict[str, Any]] = []
        if junit.exists():
            try:
                counts, cases = parse_junit(junit)
                result.junit_path = str(junit)
                result.total = counts["total"]
                result.failed = counts["failed"]
                result.errors = counts["errors"]
                result.skipped = counts["skipped"]
                result.passed = counts["total"] - counts["failed"] - counts["errors"] - counts["skipped"]
            except ET.ParseError as exc:
                result.summary = f"could not parse JUnit XML: {exc}"

        if run.timed_out:
            result.status, result.summary = "ERROR", f"test run timed out after {self.config.testing.python.timeout_seconds}s"
        elif run.error:
            result.status, result.summary = "ERROR", run.error
        elif run.exit_code == 5 or (run.exit_code == 0 and result.total == 0):
            result.status = layer.zero_tests_status
            result.summary = "no tests collected for this layer"
        elif run.exit_code == 0 and junit.exists() and result.failed == 0 and result.errors == 0:
            result.status = "PASS"
            result.summary = f"{result.passed} passed, {result.skipped} skipped"
        elif run.exit_code == 1 or result.failed or result.errors:
            result.status = "FAIL"
            result.summary = f"{result.failed} failed, {result.errors} errors, {result.passed} passed"
        else:
            result.status = "ERROR"
            result.summary = f"pytest exited with code {run.exit_code} (interrupted, internal or usage error): {run.summary(600)}"

        for case in cases:
            if case["status"] in {"FAIL", "ERROR"}:
                result.failures.append({"layer": layer.name, "nodeid": case["nodeid"], "status": case["status"], "message": case["message"][:2000]})
        result.artifacts = sorted(str(p) for p in artifacts.rglob("*") if p.is_file() and p.suffix in {".png", ".zip", ".json", ".webm"} and p.name != "coverage.json")
        coverage = artifacts / "coverage.json"
        if coverage.exists():
            try:
                result.coverage_percent = round(json.loads(coverage.read_text(encoding="utf-8"))["totals"]["percent_covered"], 1)
            except (KeyError, ValueError):
                pass
        report = artifacts / f"report-{layer.name}.json"
        report.write_text(json.dumps({**asdict(result), "cases": cases}, indent=2, default=str), encoding="utf-8")
        result.report_path = str(report)
        return self._record(result, task_id, stage, cases)

    def _record(self, result: LayerResult, task_id: int | None, stage: str, cases: list[dict[str, Any]] | None = None) -> LayerResult:
        summary = result.summary + (f" | coverage {result.coverage_percent}%" if result.coverage_percent is not None else "")
        result.test_run_id = self.state.add_test_run(
            self.config.project_id, task_id, stage,
            test_type=result.layer, tool_name=result.tool, status=result.status, total=result.total, passed=result.passed,
            failed=result.failed, errors=result.errors, skipped=result.skipped, blocked=1 if result.status == "BLOCKED" else 0,
            exit_code=result.exit_code, duration_seconds=result.duration_seconds, command=result.command[:2000], summary=summary[:2000],
            report_path=result.report_path, junit_path=result.junit_path, artifacts_dir=result.artifacts_dir, cases=cases or [],
        )
        self.events.publish(
            "TEST_COMPLETED", f"{result.layer}: {result.status} - {result.summary}", status=result.status, task_id=task_id,
            metadata={"layer": result.layer, "stage": stage, "total": result.total, "passed": result.passed, "failed": result.failed + result.errors, "test_run_id": result.test_run_id},
        )
        return result

    def run_suite(self, layers: list[TestLayer], workspace: Path, task_id: int | None, stage: str) -> TestSuiteResult:
        results = [self.run_layer(layer, workspace, task_id, stage) for layer in layers]
        suite = TestSuiteResult(stage, results)
        summary_path = REPORTS_DIR / "tests" / self.config.project_id / f"task-{task_id or 0}" / f"{stage.lower()}-summary-{time.strftime('%Y%m%d-%H%M%S')}.json"
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps({"stage": stage, "status": suite.status, "totals": suite.totals(), "layers": [asdict(l) for l in results]}, indent=2, default=str), encoding="utf-8")
        return suite
